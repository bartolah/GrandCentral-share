"""Natural-language command interface for the dashboard's prompt bar.

Bart types a request ("archive the oldest ConnectBlue reply-debt thread",
"what's waiting in the connectblue account", "add a todo to my personal account...")
and this runs it through Claude with tool-calling. Every tool is a thin,
validated wrapper around the exact same functions the dashboard's buttons
call (feedback.py, mail_actions.py, draft_reply.py) — the command bar has no
capability a button doesn't already have, with one deliberate carve-out: NO
send tool. Sending exists only as a human-clicked confirm dialog in the
dashboard (E3-P4, spec §6) and must never appear here. No tool acts without
a concrete thread_id — there is no bulk/unscoped action a bad prompt could
trigger (the Mail screen's bundle archive is likewise button-only).
Approved by Bart 2026-07-04 alongside the rest of the light-email-client
work (BUILD_SPEC.md records the decision).
"""

from __future__ import annotations

import json
import os
from datetime import datetime

try:
    from . import db, draft_reply, feedback, mail_actions
    from . import mail_common as mc
    from .common import OUTPUT_PATH, load_env, require_env
except ImportError:  # pragma: no cover - direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db, draft_reply, feedback, mail_actions
    import mail_common as mc
    from common import OUTPUT_PATH, load_env, require_env

COMMAND_MODEL = "claude-sonnet-5"

# Models Bart can pick from in the Ask panel (E26). Sonnet 5 is the default:
# near-Opus quality on this kind of tool-calling work at a fraction of the
# cost. Opus 5.5 and Fable 5.1 are there for the hard questions (Dev #35,
# 2026-09-28: "update the models … to be the latest including Opus 5.5").
#   NOTE on Opus 5.5 / Fable 5.1: thinking can't be turned off and an
#   explicit disabled/budget `thinking` param is REJECTED, as is a forced
#   tool_choice — this module sends neither. Opus 5.5's effort default is
#   medium, one below the others, so effort is always sent explicitly. Both
#   can decline a request outright (stop_reason "refusal"), which is handled
#   in run_command rather than surfacing as an empty answer.
ALLOWED_MODELS = {"claude-sonnet-5", "claude-opus-5-5", "claude-fable-5-1"}
# effort rides in output_config; higher = deeper thinking, slower, pricier
ALLOWED_EFFORT = {"low", "medium", "high", "xhigh", "max"}
DEFAULT_EFFORT = "high"   # the API's own default for these models
# Analytical questions ("which deals have gone quiet?") chain several reads
# before answering, so the ceiling is higher than the original command bar's.
MAX_TOOL_ROUNDS = 10
# These models think adaptively by default, and thinking shares this budget
# with the visible answer — 2000 risked truncating a long analysis mid-sentence.
MAX_TOKENS = 6000
# tool results are never surfaced as "actions" in the reply — they're reads
READ_ONLY_TOOLS = {"get_thread", "draft_preview", "query_todos",
                   "query_mail", "search_mail", "query_context", "query_calendar"}

SYSTEM_PROMPT = (
    "You are Grand Central's command interface, working for Bart Olah on his "
    "personal email and todo board. Act only through the tools provided — "
    "you cannot send email (sending exists only as a human-clicked button in "
    "the dashboard, never as a tool) and no tool acts without a "
    "specific thread_id, so never guess at bulk or unscoped actions. If a "
    "request is ambiguous, underspecified, or looks destructive/irreversible "
    "beyond what was clearly asked, ask a short clarifying question instead "
    "of acting.\n\n"
    "You answer questions as well as act. Read tools (query_todos, "
    "query_mail, search_mail, query_context, query_calendar) "
    "let you look things up — prefer looking up over guessing, and chain "
    "several reads when a question needs them. The board snapshot you are "
    "given is a TRUNCATED convenience summary, not a source of counts: never "
    "say how many of anything there are by counting its rows. Any question "
    "about how many, which ones, or what is overdue must come from a read "
    "tool — those results are complete and carry an explicit `count`.\n\n"
    "Length follows the request: confirm an action in one or two sentences, "
    "but answer a real question properly — lead with the direct answer, then "
    "the specifics that justify it. Name actual people, deals, amounts, and "
    "ages rather than talking in generalities, and say plainly when the data "
    "does not support an answer instead of padding. Never invent a thread, "
    "deal, or task that a tool did not return. When you propose follow-ups, "
    "offer to create them as todos rather than creating them unasked. When "
    "Bart states something durable about his business — what a brand or "
    "product is, how he wants something handled, or positioning language "
    "he'd reuse — offer to save it with add_context so it grounds future "
    "answers instead of living only in one conversation."
)

_ACCOUNT_PROP = {"type": "string", "enum": list(mc.ACCOUNTS)}

TOOLS = [
    {
        "name": "get_thread",
        "description": "Read the full messages in one email thread (read-only).",
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}, "account": _ACCOUNT_PROP},
            "required": ["thread_id", "account"],
        },
    },
    {
        "name": "dismiss",
        "description": ("Clear a thread from the Reply Debt / Unclassified panels. "
                        "Local-only — never touches Gmail. Takes effect on the next sweep."),
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}, "account": _ACCOUNT_PROP},
            "required": ["thread_id"],
        },
    },
    {
        "name": "mail_action",
        "description": "Archive, star/unstar, or add/remove a Gmail label on a thread. Writes to Gmail.",
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "account": _ACCOUNT_PROP,
                "action": {"type": "string",
                          "enum": ["archive", "star", "unstar", "label-add", "label-remove"]},
                "label": {"type": "string", "description": "required for label-add / label-remove"},
            },
            "required": ["thread_id", "account", "action"],
        },
    },
    {
        "name": "add_task",
        "description": "Add a todo to ONE account (required). Grand Central is the task system "
                       "of record. If the task stems from an email, also pass its thread_id so "
                       "the email is pulled into the todo; the email must be in that account.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "due": {"type": "string", "description": "ISO date YYYY-MM-DD (optional)"},
                "account": _ACCOUNT_PROP,
                "thread_id": {"type": "string", "description": "Gmail thread id of the spawning email (optional; it must be in `account`)"},
            },
            "required": ["text", "account"],
        },
    },
    {
        "name": "complete_task",
        "description": "Mark a task done (or drop it). Task ids are in the board context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "drop": {"type": "boolean", "description": "true to drop instead of complete"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "update_task",
        "description": ("Edit an existing todo: due date, star, My Day, "
                        "wording, or snooze. Set my_day to today's date to put it on "
                        "Bart's home-screen plan for today (it self-clears at midnight); "
                        "pass an empty string to clear any field."),
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "text": {"type": "string"},
                "due": {"type": "string", "description": "YYYY-MM-DD, or '' to clear"},
                "my_day": {"type": "string",
                           "description": "today's date YYYY-MM-DD to add to My Day, '' to remove"},
                "starred": {"type": "boolean"},
                "snoozed_until": {"type": "string", "description": "ISO timestamp, or '' to unsnooze"},
            },
            "required": ["id"],
        },
    },
    {
        "name": "add_decision",
        "description": ("Add a decision only Bart can make to the Decision Queue "
                        "(kept separate from tasks by design)."),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "blocking": {"type": "string", "description": "what stalls until decided (optional)"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "decide_decision",
        "description": "Record the outcome of a Decision Queue entry, closing it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "outcome": {"type": "string"},
            },
            "required": ["id", "outcome"],
        },
    },
    {
        "name": "draft_preview",
        "description": ("Have Claude draft a reply to a thread WITHOUT saving it anywhere "
                        "yet — use to see or refine draft text before draft_save."),
        "input_schema": {
            "type": "object",
            "properties": {"thread_id": {"type": "string"}, "account": _ACCOUNT_PROP},
            "required": ["thread_id", "account"],
        },
    },
    {
        "name": "draft_save",
        "description": ("Save reply text (from draft_preview, edited, or written from scratch) "
                        "into that account's Gmail Drafts. Your only outbound write, and it "
                        "is Drafts only — it never sends; Bart sends from Gmail or the "
                        "dashboard's own confirm dialog."),
        "input_schema": {
            "type": "object",
            "properties": {
                "thread_id": {"type": "string"},
                "account": _ACCOUNT_PROP,
                "body": {"type": "string"},
            },
            "required": ["thread_id", "account", "body"],
        },
    },
    # ---------- read tools (E22): look things up instead of guessing ----------
    {
        "name": "query_todos",
        "description": ("Bart's todos with their account, due dates, stars, and "
                        "My Day state. Read-only."),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string",
                          "enum": ["open", "overdue", "due_today", "my_day", "starred", "snoozed", "done"],
                          "description": "default 'open'"},
                "account": {"type": "string", "description": "only this account's todos"},
                "limit": {"type": "integer", "description": "default 50"},
            },
        },
    },
    {
        "name": "query_mail",
        "description": ("The current inbox as the Mail screen sees it — subjects, who, "
                        "age, unread/starred, and the account each thread lives in. "
                        "Use `view` to narrow: owed = needs a reply. Read-only."),
        "input_schema": {
            "type": "object",
            "properties": {
                "view": {"type": "string", "enum": ["inbox", "owed"],
                         "description": "default 'inbox'"},
                "account": {"type": "string", "description": "only this account's threads"},
                "limit": {"type": "integer", "description": "default 40"},
            },
        },
    },
    {
        "name": "search_mail",
        "description": ("Search all mail (not just the inbox) with Gmail's own query "
                        "syntax — from:, subject:, older_than:, quoted phrases. Use to "
                        "find history on a person or deal. Read-only."),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "Gmail-syntax query"},
                "account": {"type": "string", "enum": list(mc.ACCOUNTS) + ["all"],
                            "description": "default 'all'"},
                "limit": {"type": "integer", "description": "default 15 per account"},
            },
            "required": ["q"],
        },
    },
    {
        "name": "query_context",
        "description": ("Bart's own durable notes and rules about how he wants things "
                        "handled — what a label means, who a person is, his voice. "
                        "Consult before assuming intent. Read-only."),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string",
                          "description": "optional prefix filter, e.g. 'label:' or 'person:'"},
            },
        },
    },
    {
        "name": "add_context",
        "description": ("Save a durable fact, rule, or piece of Bart's own "
                        "language into his standing context corpus, where it "
                        "grounds every future answer. Use for definitions "
                        "(what a brand or product IS), how he wants something "
                        "handled, or positioning language worth reusing — NOT "
                        "for one-off todos, which are tasks. Same store the "
                        "Configure → Context tab writes to."),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "the statement itself, self-contained"},
                "scope": {"type": "string",
                          "description": ("what it is about: 'global', or "
                                          "'account:<slug>', 'person:<name>', "
                                          "'label:<gmail label>'")},
                "kind": {"type": "string",
                         "enum": ["note", "rule", "voice", "automation"],
                         "description": ("note = a fact; rule = how to handle "
                                         "something; voice = his phrasing to "
                                         "reuse; automation = something to build")},
            },
            "required": ["text"],
        },
    },
    {
        "name": "query_calendar",
        "description": "Bart's meetings for the next several days. Read-only.",
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "description": "how many days ahead (default 7)"},
            },
        },
    },
]


def _state() -> dict:
    """The last sweep's grand_central.json, or {} if unreadable."""
    try:
        with open(OUTPUT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _context_notes() -> list:
    """Bart's active context corpus (E19) — his standing rules about how he
    wants things handled. Injected into every request so the assistant is
    grounded in them without having to think to look."""
    try:
        conn = db.connect()
        try:
            return [{"scope": n["scope"], "kind": n["kind"], "text": n["text"]}
                    for n in db.list_context(conn, active_only=True)]
        finally:
            conn.close()
    except Exception:
        return []  # grounding is best-effort; never break the request


def _board_context() -> dict:
    """A compact read-only snapshot of the current board for the model."""
    state = _state()
    open_tasks, open_decs = [], []
    try:
        conn = db.connect()
        try:
            open_tasks = db.open_engagements(conn)[:40]
            open_decs = db.open_decisions(conn)[:20]
        finally:
            conn.close()
    except Exception:
        pass  # context is best-effort; tools will surface real errors
    return {
        "open_tasks": open_tasks,
        "open_decisions": open_decs,
        "reply_debt": state.get("reply_debt", [])[:40],
        "waiting_on": state.get("waiting_on", [])[:20],
    }


def _run_tool(name: str, tool_input: dict, env: dict) -> dict:
    if name == "get_thread":
        return {"messages": mail_actions.fetch_thread(tool_input["thread_id"], tool_input["account"], env=env)}
    if name == "dismiss":
        return feedback.record_dismissed(tool_input)
    if name == "mail_action":
        action = tool_input.get("action")
        thread_id, account = tool_input["thread_id"], tool_input["account"]
        if action == "archive":
            return mail_actions.archive_thread(thread_id, account, env=env)
        if action == "star":
            return mail_actions.star_thread(thread_id, account, star=True, env=env)
        if action == "unstar":
            return mail_actions.star_thread(thread_id, account, star=False, env=env)
        if action == "label-add":
            return mail_actions.label_thread(thread_id, account, tool_input.get("label", ""), add=True, env=env)
        if action == "label-remove":
            return mail_actions.label_thread(thread_id, account, tool_input.get("label", ""), add=False, env=env)
        raise ValueError(f"unknown mail_action {action!r}")
    if name == "update_task":
        fields = {}
        for key, col in (("text", "text"), ("due", "due"), ("my_day", "my_day"),
                         ("snoozed_until", "snoozed_until")):
            if key in tool_input:
                fields[col] = tool_input[key]
        if "starred" in tool_input:
            fields["starred"] = tool_input["starred"]
        conn = db.connect()
        try:
            return {"updated": db.update_engagement(conn, tool_input["id"], fields),
                    "fields": fields}
        finally:
            conn.close()
    if name in ("add_task", "complete_task", "add_decision", "decide_decision"):
        conn = db.connect()
        try:
            if name == "add_task":
                acct = str(tool_input.get("account") or "").strip()
                thrid = str(tool_input.get("thread_id") or "").strip()
                tid = db.add_engagement(conn, tool_input["text"],
                                        account=acct or None,
                                        due=tool_input.get("due"),
                                        source_ref=(f"{acct}:{thrid}"
                                                    if acct and thrid else None))
                if tid and acct and thrid and thrid.isdigit():
                    # E106: pull the spawning email into the todo
                    db.link_thread(conn, tid, acct, thrid)
                return {"id": tid, "added": True}
            if name == "complete_task":
                state = "dropped" if tool_input.get("drop") else "done"
                return {"updated": db.set_engagement_state(conn, tool_input["id"], state),
                        "state": state}
            if name == "add_decision":
                did = db.add_decision(conn, tool_input["text"],
                                      blocking=tool_input.get("blocking"))
                return {"id": did, "added": True}
            return {"decided": db.decide_decision(conn, tool_input["id"], tool_input["outcome"])}
        finally:
            conn.close()
    if name == "query_todos":
        scope = tool_input.get("scope") or "open"
        acct = str(tool_input.get("account") or "").strip()
        conn = db.connect()
        try:
            rows = db.todo_view(conn, account=db.clean_account(acct) if acct else None)
        finally:
            conn.close()
        today = datetime.now().strftime("%Y-%m-%d")
        if scope == "done":
            rows = [r for r in rows if r.get("state") == "done"]
        else:
            rows = [r for r in rows if r.get("state") == "open"]
            if scope == "overdue":
                rows = [r for r in rows if (r.get("due") or "9999") < today]
            elif scope == "due_today":
                rows = [r for r in rows if (r.get("due") or "")[:10] == today]
            elif scope == "my_day":
                rows = [r for r in rows if (r.get("my_day") or "")[:10] == today]
            elif scope == "starred":
                rows = [r for r in rows if r.get("starred")]
            elif scope == "snoozed":
                rows = [r for r in rows if r.get("snoozed_until")]
        return {"scope": scope, "count": len(rows),
                "todos": rows[:int(tool_input.get("limit") or 50)]}
    if name == "query_mail":
        view = tool_input.get("view") or "inbox"
        acct = str(tool_input.get("account") or "").strip()
        conn = db.connect()
        try:
            rows = db.mail_view(conn, account=db.clean_account(acct) if acct else None)
        finally:
            conn.close()
        if view == "owed":
            owed = {(r.get("thread_id"), r.get("account"))
                    for r in (_state().get("reply_debt") or [])}
            rows = [r for r in rows
                    if (r.get("thread_id"), r.get("account")) in owed]
        # trim the heavy fields — the model needs identity, not full bodies
        slim = [{k: r.get(k) for k in
                 ("thread_id", "account", "subject", "who", "last_date",
                  "age_h", "unread", "starred", "labels", "link")} for r in rows]
        return {"view": view, "count": len(slim),
                "threads": slim[:int(tool_input.get("limit") or 40)]}
    if name == "search_mail":
        acct = tool_input.get("account") or "all"
        accounts = list(mc.ACCOUNTS) if acct == "all" else [acct]
        limit = int(tool_input.get("limit") or 15)
        rows, errors = [], []
        for a in accounts:
            try:
                rows.extend(mail_actions.search_threads(
                    tool_input["q"], a, env=env, limit=limit))
            except Exception as e:  # one bad account shouldn't kill the search
                errors.append(f"{a}: {e}")
        return {"query": tool_input["q"], "count": len(rows),
                "threads": rows, "errors": errors}
    if name == "query_context":
        conn = db.connect()
        try:
            notes = db.list_context(conn, scope_prefix=tool_input.get("scope"),
                                    active_only=True)
        finally:
            conn.close()
        return {"context": notes}
    if name == "add_context":
        conn = db.connect()
        try:
            nid = db.add_context(conn, tool_input.get("scope") or "global",
                                 tool_input.get("kind") or "note",
                                 tool_input["text"])
            return {"id": nid, "saved": True,
                    "scope": tool_input.get("scope") or "global"}
        finally:
            conn.close()
    if name == "query_calendar":
        days = int(tool_input.get("days") or 7)
        return {"calendar": (_state().get("calendar") or [])[:days]}
    if name == "draft_preview":
        return draft_reply.generate_draft(tool_input["thread_id"], tool_input["account"], env=env)
    if name == "draft_save":
        return draft_reply.save_draft(tool_input["thread_id"], tool_input["account"],
                                      tool_input.get("body", ""), env=env)
    raise ValueError(f"unknown tool {name!r}")


def run_command(text: str, selection: dict | None = None, env: dict | None = None,
                model: str = COMMAND_MODEL, history: list | None = None,
                effort: str | None = None) -> dict:
    """Run one natural-language command, optionally continuing a conversation.

    `history` is prior turns as [{"role": "user"|"assistant", "text": str}] —
    the Ask panel keeps the transcript client-side and posts it back, so the
    server stays stateless (no sessions table, nothing to expire or clean up).
    Only the plain text of past turns is replayed, not their tool tSamc:
    the model re-reads live data rather than trusting a stale tool result
    from three questions ago.

    Returns {"reply": str, "actions_taken": [{"tool", "input", "result"}]}.
    Raises only on a bad ANTHROPIC_API_KEY / API-level failure; per-tool
    errors are fed back to the model as a tool_result so it can recover or
    explain, not raised to the caller.
    """
    import anthropic

    model = model if model in ALLOWED_MODELS else COMMAND_MODEL
    effort = effort if effort in ALLOWED_EFFORT else DEFAULT_EFFORT
    env = env or load_env()
    user_content = f"Board context (read-only snapshot):\n{json.dumps(_board_context(), ensure_ascii=False)}\n\n"
    notes = _context_notes()
    if notes:
        user_content += ("Bart's standing context (his own rules — follow them):\n"
                         f"{json.dumps(notes, ensure_ascii=False)}\n\n")
    user_content += f"Today is {datetime.now().strftime('%A %Y-%m-%d')}.\n\n"
    if selection:
        user_content += f"Currently open in the dashboard: {json.dumps(selection, ensure_ascii=False)}\n\n"
    user_content += f"Bart says: {text}"

    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    messages = []
    for turn in (history or [])[-12:]:  # bounded: the last few turns of talk
        role = "assistant" if turn.get("role") == "assistant" else "user"
        body = str(turn.get("text") or "").strip()[:4000]
        if body:
            messages.append({"role": role, "content": body})
    if messages and messages[-1]["role"] == "user":
        messages.append({"role": "assistant", "content": "(continuing)"})
    messages.append({"role": "user", "content": user_content})
    actions_taken = []

    for _ in range(MAX_TOOL_ROUNDS):
        resp = client.messages.create(
            model=model, max_tokens=MAX_TOKENS, system=SYSTEM_PROMPT,
            tools=TOOLS, messages=messages,
            output_config={"effort": effort},
        )
        # Fable 5's safety classifiers can decline a request outright: HTTP
        # 200, empty content. Say so rather than returning a blank answer.
        if getattr(resp, "stop_reason", None) == "refusal":
            return {"reply": f"{model} declined to answer that one. Try a "
                             f"different model in the picker, or rephrase.",
                    "actions_taken": actions_taken}
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        if not tool_uses:
            reply = "".join(b.text for b in resp.content if b.type == "text").strip()
            return {"reply": reply or "(done)", "actions_taken": actions_taken}

        tool_results = []
        for tu in tool_uses:
            try:
                result = _run_tool(tu.name, tu.input, env)
                if tu.name not in READ_ONLY_TOOLS:
                    actions_taken.append({"tool": tu.name, "input": tu.input, "result": result})
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": json.dumps(result, ensure_ascii=False, default=str)})
            except Exception as e:
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": f"error: {e}", "is_error": True})
        messages.append({"role": "user", "content": tool_results})

    return {"reply": "Stopped after several steps without a final answer — try a narrower request.",
            "actions_taken": actions_taken}
