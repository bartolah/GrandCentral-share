"""Ask on a project page — Projects step 5 (BUILD_SPEC §6, 2026-09-30).

Bart, 2026-09-30 interview: a box on the project page opens Ask fenced to
that project. It reads everything filed there — emails in any account (full
threads), todos and the emails folded inside them, notes, meetings with their
Granola notes, people — and nothing outside it. Read and draft only: it may
save a reply draft to Gmail Drafts for a thread in the project, and nothing
else. No archive, snooze, todo changes, notes or project edits, and never
send (§6: sending is a human click in the composer, never a tool).

The fence is enforced here, not in the prompt: every tool checks that the
(account, thread_id) or meeting it names is filed in the project, and the
tool list simply has nothing else in it. command.run_command(project_id=…)
uses this module in place of its own prompt, context and tools.

    .venv/bin/python sweeper/project_ask.py 3      # print project 3's context
"""

from __future__ import annotations

import json
import sys

import db
import projects

NOTE_CAP, NOTES_TOTAL_CAP = 4000, 20000     # characters of note text in the context
DOCS_CAP = 60000       # E259: the project's context documents, then its accounts'

SYSTEM_PROMPT = (
    "You are Grand Central's Ask, opened on one of Bart Olah's projects. You "
    "see only what is filed in this project: its emails (from any of his "
    "accounts), its todos and the emails inside them, its notes, its meetings "
    "and its people. You cannot see anything else in his mail, todos or "
    "calendar — if a question needs something outside the project, say so "
    "plainly and suggest he file it into the project or ask in the main Ask "
    "panel. Never invent a thread, person, date or number the project did not "
    "give you.\n\n"
    "Tools: read_thread reads one of the project's email threads in full; "
    "meeting_notes reads the Granola notes of one of its meetings; "
    "draft_preview and draft_save write a reply draft into that account's Gmail "
    "Drafts for one of its threads. That is all you can do: you cannot send, "
    "archive, snooze, change todos, or write notes — if Bart asks for one of "
    "those, tell him which button does it. Only save a draft when he asks for "
    "one.\n\n"
    "Bart's context documents (E259) come with the project: the project's own "
    "first, then those of the accounts its items live in. They are his "
    "standing brief — facts, people, rules and voice. Follow their rules, "
    "especially in drafts, and say so when one looks stale or conflicts with "
    "what the emails show.\n\n"
    "Lead with the direct answer, then the specifics (who, what, when, which "
    "account). When you summarise, say where each point comes from — an email, "
    "a note, a meeting, a context document."
)

TOOLS = [
    {
        "name": "read_thread",
        "description": "Read every message in one of this project's email threads (read-only).",
        "input_schema": {"type": "object", "properties": {
            "account": {"type": "string"}, "thread_id": {"type": "string"}},
            "required": ["account", "thread_id"]},
    },
    {
        "name": "meeting_notes",
        "description": ("Read the Granola notes (AI summary and action items, not the "
                        "transcript) of one of this project's meetings, by its item_id."),
        "input_schema": {"type": "object", "properties": {"item_id": {"type": "integer"}},
                         "required": ["item_id"]},
    },
    {
        "name": "draft_preview",
        "description": "Draft a reply to one of this project's threads WITHOUT saving it — to show or refine.",
        "input_schema": {"type": "object", "properties": {
            "account": {"type": "string"}, "thread_id": {"type": "string"}},
            "required": ["account", "thread_id"]},
    },
    {
        "name": "draft_save",
        "description": ("Save reply text into that account's Gmail Drafts for one of this "
                        "project's threads. Drafts only — it never sends."),
        "input_schema": {"type": "object", "properties": {
            "account": {"type": "string"}, "thread_id": {"type": "string"},
            "body": {"type": "string"}},
            "required": ["account", "thread_id", "body"]},
    },
]
READ_ONLY = {"read_thread", "meeting_notes", "draft_preview"}


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + untrusted.RULE


def context(conn, pid: int) -> dict:
    """What the model is shown up front: the project, resolved from where
    each item lives. Full thread bodies and meeting notes come via tools."""
    v = projects.project_view(conn, pid)
    items = v["items"]
    threads = [{"account": i["account"], "thread_id": i["ref"],
                "subject": (i.get("mail") or {}).get("subject") or i.get("title"),
                "last_from": (i.get("mail") or {}).get("last_from"),
                "last_date": (i.get("mail") or {}).get("last_date"),
                "snippet": ((i.get("mail") or {}).get("snippet") or "")[:300],
                "in_inbox": bool((i.get("mail") or {}).get("in_inbox"))}
               for i in items if i["kind"] == "thread" and not i.get("in_todo")]
    todos = [{"id": i["todo"]["id"], "text": i["todo"]["text"], "state": i["todo"]["state"],
              "due": i["todo"]["due"], "account": i["account"],
              "emails": [{"account": t["account"], "thread_id": t["thread_id"], "subject": t["subject"],
                          "last_from": t["last_from"], "last_date": t["last_date"]}
                         for t in i.get("threads") or []]}
             for i in items if i["kind"] == "todo" and i.get("todo")]
    meetings = [{"item_id": i["id"], "title": i.get("title"), "date": i.get("date"), "time": i.get("time"),
                 "account": i["account"], "granola_notes": bool(i.get("granola")),
                 "granola_id": (i.get("granola") or {}).get("id")}
                for i in items if i["kind"] == "event"]
    people = [{"email": i["ref"], "name": (i.get("contact") or {}).get("name") or i.get("title")}
              for i in items if i["kind"] == "contact"]
    notes, budget = [], NOTES_TOTAL_CAP
    for n in v["notes"]:
        body = (n["body"] or "")[:min(NOTE_CAP, max(budget, 0))]
        budget -= len(body)
        notes.append({"title": n["title"], "updated": n["updated_at"], "text": body,
                      "truncated": len(body) < len(n["body"] or "")})
    # E259: Bart's standing context — the project's documents first, then
    # those of every account the project's items live in
    docs = db.context_docs_text(conn, [f"project:{int(pid)}"] +
                                [f"account:{a}" for a in v["accounts"]], DOCS_CAP)
    return {"context_documents": docs,
            "project": {"name": v["name"], "description": v.get("description"),
                        "status": v["status"], "target_date": v.get("target_date"),
                        "completed_at": v.get("completed_at"), "accounts": v["accounts"]},
            "emails": threads, "todos": todos, "meetings": meetings, "people": people, "notes": notes}


def allowed_threads(ctx: dict) -> set:
    out = {(t["account"], str(t["thread_id"])) for t in ctx["emails"]}
    for t in ctx["todos"]:
        out.update((e["account"], str(e["thread_id"])) for e in t["emails"])
    return out


def _fence(ctx: dict, tool_input: dict) -> tuple[str, str]:
    key = (str(tool_input.get("account") or ""), str(tool_input.get("thread_id") or ""))
    if key not in allowed_threads(ctx):
        raise ValueError("that thread isn't filed in this project — Bart can add it with 📁")
    return key


def run_tool(name: str, tool_input: dict, env: dict, ctx: dict, pid: int) -> dict:
    """One tool call, fenced to the project. Raises ValueError outside it."""
    if name == "read_thread":
        import mail_actions
        acct, tid = _fence(ctx, tool_input)
        return {"messages": mail_actions.fetch_thread(tid, acct, env=env)}
    if name == "meeting_notes":
        mid = int(tool_input.get("item_id") or 0)
        m = next((m for m in ctx["meetings"] if m["item_id"] == mid), None)
        if m is None:
            raise ValueError("that meeting isn't filed in this project")
        if not m.get("granola_id"):
            return {"notes": None, "why": "no Granola notes were found for this meeting"}
        from sources import granola_reader
        return granola_reader.get_note(env, m["granola_id"])
    if name == "draft_preview":
        import draft_reply
        acct, tid = _fence(ctx, tool_input)
        return draft_reply.generate_draft(tid, acct, env=env)
    if name == "draft_save":
        import draft_reply
        acct, tid = _fence(ctx, tool_input)
        return draft_reply.save_draft(tid, acct, tool_input.get("body", ""), env=env)
    raise ValueError(f"{name!r} isn't available on a project page")


if __name__ == "__main__":
    c = db.connect()
    try:
        print(json.dumps(context(c, int(sys.argv[1])), indent=1, default=str))
    finally:
        c.close()
