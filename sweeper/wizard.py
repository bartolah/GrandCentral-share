"""E111 — the 🧙 Wizard: agent-proposed todos awaiting Bart's verdict.

Two scans per sweep, both bounded and one-look-ever (wizard_scan):

  email_scan    — recent people mail (unread or needs-reply, last 48h)
                  → one Haiku call proposing todos for today/tomorrow.
                  Conservative by prompt: only clear, concrete actions
                  BART must take; existing open todos ride along so the
                  model doesn't re-suggest what's already on the board.
  granola_scan  — meetings from this sweep's Granola pull → one Haiku
                  call proposing follow-up actions IMPLIED by each
                  summary, beyond the explicit action items (those are
                  already captured as 'granola-item' suggestions by
                  record_granola_meetings).

Nothing here creates a todo. Suggestions land in wizard_suggestions
('pending') and wait on the 🧙 Wizard page; approve creates the todo and
links the source email (E106 membership). Steward principle throughout:
the agent NEVER acts alone. No new messages/meetings → no model call.
"""

from __future__ import annotations

import json
import re
from datetime import datetime

import db
from common import require_env

MODEL = "claude-haiku-4-5-20251001"
MAX_EMAILS = 20
MAX_MEETINGS = 8

_EMAIL_PROMPT = """You are the Wizard for Bart's task board. From these
recent emails Bart received, suggest todos he should add for today or
tomorrow. Be CONSERVATIVE — only clear, concrete actions BART himself must
take (reply substantively, send something, decide, schedule). Skip
newsletters, FYIs, receipts, other people's work, and anything already
covered by the existing open todos provided. today={today}.
Return ONLY JSON: {{"suggestions":[{{"i":<email index>,"title":"<imperative,
≤80 chars>","why":"<≤100 chars citing the email>","due":"YYYY-MM-DD or
null"}}]}} — an empty list is a fine answer."""

_MEETING_PROMPT = """You are the Wizard for Bart's task board. For each of
these meeting summaries, suggest follow-up actions for BART (the board's
owner) that are clearly IMPLIED by the discussion but NOT in the explicit
action-item list provided per meeting. Think: promised intros, decisions
needing follow-through, threads left dangling. Be conservative — at most 3
per meeting, none is fine. today={today}.
Return ONLY JSON: {{"suggestions":[{{"i":<meeting index>,"title":
"<imperative, ≤80 chars>","why":"<≤100 chars citing the meeting>",
"due":"YYYY-MM-DD or null"}}]}}."""


def _ask(env: dict, system: str, payload) -> list:
    import anthropic
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    resp = client.messages.create(
        model=MODEL, max_tokens=1200,
        system=system.format(today=today),
        messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
    raw = "".join(p.text for p in resp.content if p.type == "text")
    m = re.search(r"\{.*\}", raw, re.S)
    return (json.loads(m.group(0)) if m else {}).get("suggestions") or []


def _clean_sug(s: dict) -> tuple:
    title = str(s.get("title") or "").strip()[:200]
    why = str(s.get("why") or "").strip()[:200]
    due = s.get("due")
    if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)):
        due = None
    return title, why, due


def email_scan(conn, env: dict, log=print) -> None:
    rows = [dict(r) for r in conn.execute(
        "SELECT mt.account, mt.thread_id, mt.subject, mt.last_from, mt.snippet, "
        "  mt.last_date "
        "FROM mail_threads mt "
        "LEFT JOIN mail_triage t ON t.account = mt.account AND t.thread_id = mt.thread_id "
        "WHERE mt.in_inbox = 1 AND mt.category = 'people' "
        "  AND (mt.unread = 1 OR mt.last_direction = 'inbound') "
        "  AND mt.last_date >= datetime('now', '-2 days') "
        "  AND t.done_at IS NULL AND t.snoozed_until IS NULL "
        "ORDER BY mt.last_date DESC LIMIT 60")]
    fresh_refs = db.wizard_unscanned(
        conn, "email", [f"{r['account']}:{r['thread_id']}" for r in rows])
    fresh = [r for r in rows
             if f"{r['account']}:{r['thread_id']}" in set(fresh_refs)][:MAX_EMAILS]
    if not fresh:
        return
    # one look ever, whatever the model says
    db.wizard_mark_scanned(conn, "email",
                           [f"{r['account']}:{r['thread_id']}" for r in fresh])
    open_todos = [r["text"][:80] for r in conn.execute(
        "SELECT text FROM engagements WHERE state = 'open' "
        "ORDER BY id DESC LIMIT 50")]
    payload = {"emails": [{"i": i, "from": r["last_from"], "subject": r["subject"],
                           "snippet": (r["snippet"] or "")[:300], "date": r["last_date"]}
                          for i, r in enumerate(fresh)],
               "existing_open_todos": open_todos}
    made = 0
    for s in _ask(env, _EMAIL_PROMPT, payload):
        try:
            src = fresh[int(s["i"])]
        except Exception:
            continue
        title, why, due = _clean_sug(s)
        if title and db.add_wizard_suggestion(
                conn, "email", title, why=why, due=due,
                account=src["account"], thread_id=src["thread_id"]):
            made += 1
    log(f"wizard: {len(fresh)} email(s) read, {made} todo suggestion(s)")


def granola_scan(conn, env: dict, meetings: list, log=print) -> None:
    fresh_ids = set(db.wizard_unscanned(conn, "granola",
                                        [m["id"] for m in meetings if m.get("id")]))
    fresh = [m for m in meetings if m.get("id") in fresh_ids][:MAX_MEETINGS]
    if not fresh:
        return
    db.wizard_mark_scanned(conn, "granola", [m["id"] for m in fresh])
    payload = [{"i": i, "title": m["title"],
                "attendees": (m.get("people") or [])[:10],
                "explicit_action_items": m.get("action_items") or [],
                "summary": (m.get("notes") or "")[:3000]}
               for i, m in enumerate(fresh)]
    made = 0
    for s in _ask(env, _MEETING_PROMPT, payload):
        try:
            m = fresh[int(s["i"])]
        except Exception:
            continue
        title, why, due = _clean_sug(s)
        if not title:
            continue
        if db.add_wizard_suggestion(conn, "granola", title, why=why, due=due,
                                    meeting_ref=m["id"],
                                    meeting_title=m["title"]):
            made += 1
    log(f"wizard: {len(fresh)} meeting(s) read, {made} follow-up suggestion(s)")


def run(env: dict, meetings: list | None = None, log=print) -> None:
    conn = db.connect()
    try:
        try:
            email_scan(conn, env, log=log)
        except Exception as e:
            log(f"wizard: email scan FAILED ({e!r})")
        if meetings:
            try:
                granola_scan(conn, env, meetings, log=log)
            except Exception as e:
                log(f"wizard: granola scan FAILED ({e!r})")
    finally:
        conn.close()
