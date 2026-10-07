"""E249 — Home's note for the day: a few sentences, not a plan.

Bart, on the Home mockup: "You can pull in the Day notes, but they're too
dense right now. Let's just have it be a few sentences." So this is not the
E100 planner (theme, must, should, can-wait); it is two or three plain
sentences about what matters today, and any clash in the calendar.

Two scopes, stored per day in `home_notes`:
  - an account slug: that account's Home. The prompt holds that account's
    data only, same as everything else inside an account.
  - "" (all): the cross-account Home. BUILD_SPEC §6 (2026-09-29) allows this
    one call to read across accounts: today's calendar, todos due and
    overdue, and the assessor's one-line summaries of top-scored threads,
    every item labelled with its account. Never message bodies or snippets.
    It is a separate call; assess.py stays batched strictly per account.
It only produces text for Bart. It writes nothing else and takes no action.

Clashes are found in code (overlapping calendar blocks), not guessed by the
model: the model is told about them so it can mention them in words.

    ../.venv/bin/python day_note.py            # print the gathered signals
    ../.venv/bin/python day_note.py --write    # generate + store (costs a call)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime

import db
import home_view
from assess import DEFAULT_MODEL
from common import load_env, require_env

MAX_TOKENS = 300
MAX_CHARS = 600
BOARD_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dashboard", "grand_central.json")

PROMPT = """You write Bart's note for the day on his Home screen. Given today's
signals, write two or three short, plain sentences: what matters most today
and why, and any calendar clash listed under "clashes". Name people and
meetings as given. No lists, no headings, no greeting, no advice he did not
ask for, and do not invent anything that is not in the signals. When the
signals span several accounts, say which account something is in when it
helps. Return only the sentences."""


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
PROMPT = PROMPT + "\n\n" + untrusted.RULE


def _board() -> dict:
    try:
        with open(BOARD_JSON, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _mins(hhmm: str | None) -> int | None:
    m = re.match(r"^(\d{1,2}):(\d{2})$", hhmm or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def events_today(board: dict, today: str) -> list:
    """Today's timed + all-day events, each tagged with the account its
    calendar belongs to (or None)."""
    day = next((d for d in (board.get("calendar") or []) if d.get("date") == today), {})
    return [{"time": e.get("time"), "end": e.get("end") or "", "title": e.get("title") or "",
             "account": home_view.CAL_ACCOUNT.get(e.get("cal") or "")}
            for e in (day.get("events") or [])]


def clashes(events: list) -> list:
    """Pairs of timed events that overlap. An event with no end counts as
    30 minutes, the same default the Home timeline draws."""
    timed = []
    for e in events:
        s = _mins(e.get("time"))
        if s is None:
            continue
        end = _mins(e.get("end"))
        timed.append((s, end if end and end > s else s + 30, e))
    timed.sort(key=lambda x: x[0])
    out = []
    for i, (s1, e1, a) in enumerate(timed):
        for s2, e2, b in timed[i + 1:]:
            if s2 >= e1:
                break
            out.append({"a": a["title"], "a_time": a["time"], "a_account": a["account"],
                        "b": b["title"], "b_time": b["time"], "b_account": b["account"]})
    return out


def _summary(conn, account: str, thread_id: str) -> str | None:
    r = conn.execute("SELECT model FROM mail_class WHERE account=? AND thread_id=?",
                     (account, thread_id)).fetchone()
    try:
        return (json.loads(r["model"] or "{}").get("summary") or None) if r else None
    except ValueError:
        return None


def gather(conn, scope: str, accounts: list, board: dict | None = None,
           today: str | None = None) -> dict:
    """The signals for one scope. An account scope reads that account only."""
    today = today or home_view._today()
    board = _board() if board is None else board
    if scope:
        scope = db.clean_account(scope)
        accounts = [scope]
    evs = [e for e in events_today(board, today) if not scope or e["account"] == scope]
    per = []
    for a in accounts:
        s = home_view.section(conn, a, today)
        per.append({
            "account": a,
            "todos_due": [t["text"][:80] for t in s["due"]][:8],
            "overdue": s["counts"]["overdue"],
            "top_email": [{"from": r["from"][:40],
                           "about": (_summary(conn, a, r["thread_id"]) or r["subject"])[:140],
                           "value": r["value"]} for r in s["top"]],
            "owed": [{"from": r["from"][:40], "subject": r["subject"][:100], "state": r["state"]}
                     for r in s["follows"]],
        })
    return {"date": today, "weekday": datetime.now().astimezone().strftime("%A"),
            "now": datetime.now().astimezone().strftime("%H:%M"),
            "calendar": evs, "clashes": clashes(evs), "accounts": per}


def get(conn, scope: str, date: str | None = None) -> dict | None:
    date = date or home_view._today()
    r = conn.execute("SELECT text, created_at FROM home_notes WHERE date=? AND scope=?",
                     (date, scope)).fetchone()
    return {"text": r["text"], "created_at": r["created_at"]} if r else None


def generate(conn, scope: str, accounts: list, env: dict | None = None,
             client=None, log=print) -> dict | None:
    """One small model call -> stored note. Returns None on any failure; the
    Home page simply shows no note then."""
    try:
        signals = gather(conn, scope, accounts)
        if client is None:
            import anthropic
            env = env or load_env()
            client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=DEFAULT_MODEL, max_tokens=MAX_TOKENS, system=PROMPT,
            messages=[{"role": "user", "content": untrusted.fence_json(signals, "day_signals")}])
        text = " ".join("".join(p.text for p in resp.content
                                if getattr(p, "type", "") == "text").split())[:MAX_CHARS]
        if not text:
            log("day_note: empty response")
            return None
        now = datetime.now().isoformat(timespec="seconds")
        with conn:
            conn.execute("INSERT OR REPLACE INTO home_notes (date, scope, text, created_at) "
                         "VALUES (?,?,?,?)", (signals["date"], scope, text, now))
        return {"text": text, "created_at": now}
    except Exception as e:
        log(f"day_note: FAILED ({e!r})")
        return None


if __name__ == "__main__":
    from sources.gmail_reader import ACCOUNTS
    c = db.connect()
    try:
        scope = next((a for a in sys.argv[1:] if not a.startswith("-")), "")
        if "--write" in sys.argv:
            print(generate(c, scope, sorted(ACCOUNTS)))
        else:
            print(json.dumps(gather(c, scope, sorted(ACCOUNTS)), indent=2))
    finally:
        c.close()
