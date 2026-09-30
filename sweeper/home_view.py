"""E249 — what Home shows for one account, read-only.

Bart, 2026-09-29, after the Home mockup: a new Home tab shows every account at
once, each in its own collapsible section, and the same two-column design is
each account's own Home. BUILD_SPEC §6 (same day) allows that view on one
condition: it is a view, never an owner. So this module only ever reads, and
it builds one account at a time:

    section(conn, "connectblue")  ->  that account's counts, up to 5
                                      top-scored emails, up to 5 replies or
                                      follow-ups, todos due today, overdue count

The cross-account Home is simply a list of sections, one call per account.
Nothing here joins across accounts, and every row names the account it
belongs to, so opening or acting on it happens inside that account.

Calendar events belong to a Google calendar, not a mail account.
CAL_ACCOUNT maps the calendar_reader's labels onto the accounts they match;
an event whose calendar maps to no account is drawn without one.

    ../.venv/bin/python home_view.py connectblue     # print one section
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import db

TOP_N = 5            # Bart: "no more than 5" of each
TOP_MIN_VALUE = 40   # E217's bands: 70+ solid, 40-69 light; below that is not "top"

# calendar_reader._cal_label -> the account that calendar belongs to
CAL_ACCOUNT = {"work": "connectblue", "crowdblue": "crowdblue", "personal": "personal"}


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _days_since(iso_z: str | None) -> int | None:
    try:
        dt = datetime.strptime(iso_z or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _row(t: dict, c: dict) -> dict:
    """One email as Home draws it: the three-line row's fields plus its score."""
    return {"account": t["account"], "thread_id": t["thread_id"],
            "from": t.get("who") or t.get("last_from") or "",
            "subject": t.get("subject") or "(no subject)",
            "snippet": (t.get("snippet") or "")[:200],
            "last_date": t.get("last_date"), "unread": bool(t.get("unread")),
            "value": c.get("value"), "respond": c.get("respond"),
            "follow_up": c.get("follow_up"), "waiting_on": c.get("waiting_on")}


def _follow_state(t: dict, c: dict, today: str) -> tuple[str, str] | None:
    """('owed'|'chase', label) when this thread wants Bart's move, else None."""
    age = _days_since(t.get("last_date"))
    age_s = f"{age}d" if age else "today"
    if c.get("respond") == "yes" and t.get("last_direction") != "outbound":
        return "owed", f"Reply owed · {age_s}"
    fu = c.get("follow_up")
    if fu and fu <= today and c.get("waiting_on", "none") != "none":
        return "chase", f"Follow up · asked {age_s} ago" if age else "Follow up · asked today"
    return None


def section(conn, account: str, today: str | None = None) -> dict:
    """One account's Home section. Reads only this account's rows."""
    account = db.clean_account(account)
    today = today or _today()

    todos = [t for t in db.todo_view(conn, account=account)
             if t.get("account") == account and t.get("state") == "open"]
    # a thread filed under a todo lives with the todo, not in the inbox
    attached = {th.get("thread_id") for t in todos for th in (t.get("threads") or [])}

    threads = [t for t in db.mail_view(conn, account=account)
               if t.get("account") == account and t.get("in_inbox")
               and not t.get("moved_at") and not t.get("done_at")
               and t.get("thread_id") not in attached
               and not db.is_dismissed(conn, account, t["thread_id"])]
    cls = db.class_for_threads(conn, account)

    scored = sorted(((t, cls.get(t["thread_id"]) or {}) for t in threads),
                    key=lambda tc: (-(tc[1].get("value") or 0), tc[0].get("last_date") or ""))
    top = [_row(t, c) for t, c in scored
           if (c.get("value") or 0) >= TOP_MIN_VALUE][:TOP_N]
    shown = {r["thread_id"] for r in top}

    follows = []
    for t, c in scored:
        if t["thread_id"] in shown:
            continue
        st = _follow_state(t, c, today)
        if st:
            follows.append({**_row(t, c), "kind": st[0], "state": st[1]})
        if len(follows) == TOP_N:
            break

    live_todos = [t for t in todos if not t.get("snoozed_active")]
    due = [{"id": t["id"], "text": t.get("text") or "", "account": account}
           for t in live_todos if t.get("due") == today or (t.get("my_day") or "")[:10] == today]
    overdue = [t for t in live_todos if t.get("due") and t["due"] < today]

    return {"account": account,
            "counts": {"emails": len(threads), "todos_due": len(due), "overdue": len(overdue)},
            "top": top, "follows": follows, "due": due,
            "overdue_oldest": min((t["due"] for t in overdue), default=None)}


def home(conn, accounts: list, today: str | None = None) -> dict:
    """The cross-account Home: one independent section per account."""
    today = today or _today()
    return {"date": today, "cal_account": CAL_ACCOUNT,
            "sections": [section(conn, a, today) for a in accounts]}


if __name__ == "__main__":
    c = db.connect()
    try:
        print(json.dumps(section(c, sys.argv[1] if len(sys.argv) > 1 else "personal"), indent=2))
    finally:
        c.close()
