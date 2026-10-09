"""E258 — invite drafts: an agent proposes attendees, Casey's click sends the invites.

BUILD_SPEC §6 E258 item 6 (Casey, 2026-09-30, chose "Invite drafts" over
letting an agent invite directly). An invite is email Google sends on Casey's
behalf — the title and description go with it — so it is outbound, and
outbound is a human click. This amends the E21 "no attendees" rule by exactly
one verb, fenced as tightly as it can be:

  * an event is still created with NO attendees (calendar_writer.create,
    unchanged); the attendees an agent names are only stored here, as a
    proposal, with which of them are new to the account
  * send() — reachable only from POST /calendar/invite, which the gate makes
    paired-browser-only — adds exactly those attendees to exactly that event
    and lets Google email them (gog `calendar update --add-attendee
    --send-updates all`)
  * only events Grand Central itself created (a calendar_writes row for the
    same account and event id), ≤10 attendees, one proposal at a time,
    audited in calendar_writes (action 'invite:<n>')
  * nothing else about the event can change here: no summary, time,
    description or removal verbs

Run standalone to list what is waiting:
    cd sweeper && ../.venv/bin/python calendar_invite.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

try:
    from . import db
    from .common import load_env, require_env
    from .calendar_writer import ACCOUNT_EMAIL_KEYS, GOG_TIMEOUT
except ImportError:  # run with sweeper/ on sys.path
    import db
    from common import load_env, require_env
    from calendar_writer import ACCOUNT_EMAIL_KEYS, GOG_TIMEOUT

MAX_ATTENDEES = 10
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def clean_attendees(value) -> list:
    raw = value if isinstance(value, list) else str(value or "").split(",")
    out = []
    for a in raw:
        a = str(a).strip().lower()
        m = re.search(r"<([^>]+)>", a)
        a = m.group(1) if m else a
        if not _EMAIL.match(a):
            raise ValueError(f"bad attendee {a!r}")
        if a not in out:
            out.append(a)
    if not out:
        raise ValueError("no attendees")
    if len(out) > MAX_ATTENDEES:
        raise ValueError(f"at most {MAX_ATTENDEES} attendees")
    return out


def propose(conn, account: str, event: dict, attendees: list, new: list, by: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO invite_drafts (account, event_id, summary, start_at, link, attendees, "
            "new_attendees, status, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (account, event.get("event_id"), event.get("summary"), event.get("start"),
             event.get("link"), json.dumps(attendees), json.dumps(new), "pending", by,
             db._now_iso()))
    return cur.lastrowid


def pending(conn) -> list:
    rows = []
    for r in conn.execute("SELECT * FROM invite_drafts WHERE status = 'pending' "
                          "ORDER BY id DESC LIMIT 50"):
        d = dict(r)
        d["attendees"] = json.loads(d["attendees"] or "[]")
        d["new_attendees"] = json.loads(d["new_attendees"] or "[]")
        rows.append(d)
    return rows


def drop(conn, invite_id: int) -> bool:
    with conn:
        return conn.execute("UPDATE invite_drafts SET status = 'dropped' WHERE id = ? "
                            "AND status = 'pending'", (int(invite_id),)).rowcount > 0


def send(conn, invite_id: int, env: dict | None = None, log=print, runner=subprocess.run) -> dict:
    """Casey clicked Send invites. Adds the proposed attendees and lets Google
    email them. Raises on anything unexpected; nothing is retried."""
    row = conn.execute("SELECT * FROM invite_drafts WHERE id = ?", (int(invite_id),)).fetchone()
    if not row or row["status"] != "pending":
        raise ValueError("no pending invite with that id")
    account, event_id = row["account"], row["event_id"]
    if not event_id or not conn.execute(
            "SELECT 1 FROM calendar_writes WHERE account = ? AND event_id = ? "
            "AND (action IS NULL OR action = '')", (account, event_id)).fetchone():
        raise ValueError("invites only go on events Grand Central created")
    attendees = clean_attendees(json.loads(row["attendees"] or "[]"))

    env = env or load_env()
    email = require_env(env, ACCOUNT_EMAIL_KEYS[account])
    gog = (shutil.which("gog")
           or next((p for p in ("/opt/homebrew/bin/gog", "/usr/local/bin/gog")
                    if os.path.exists(p)), None))
    if gog is None:
        raise RuntimeError("gog CLI not installed (brew install steipete/tap/gog)")
    cmd = [gog, "calendar", "update", "primary", event_id,
           "--add-attendee", ",".join(attendees), "--send-updates", "all",
           "-a", email, "-j", "--no-input"]
    proc_env = dict(os.environ)
    if env.get("GOG_KEYRING_PASSWORD"):
        proc_env["GOG_KEYRING_PASSWORD"] = env["GOG_KEYRING_PASSWORD"]
    # Claim the row BEFORE Google emails anyone: a double click, or a failure
    # after the send, must never leave it 'pending' and sendable again.
    with conn:
        if conn.execute("UPDATE invite_drafts SET status = 'sending' WHERE id = ? "
                        "AND status = 'pending'", (int(invite_id),)).rowcount != 1:
            raise ValueError("no pending invite with that id")
    try:
        proc = runner(cmd, capture_output=True, text=True, timeout=GOG_TIMEOUT, env=proc_env)
    except Exception:
        # unknown whether Google sent: stay 'sending' (not re-sendable) and say so
        log(f"calendar_invite: #{invite_id} gog did not answer — left as 'sending'; "
            "check the event in Google Calendar before retrying")
        raise
    if proc.returncode != 0:
        with conn:
            conn.execute("UPDATE invite_drafts SET status = 'pending' WHERE id = ?",
                         (int(invite_id),))
        raise RuntimeError(f"gog calendar update failed: {(proc.stderr or proc.stdout or '')[:400]}")
    with conn:
        conn.execute("UPDATE invite_drafts SET status = 'sent', sent_at = ? WHERE id = ?",
                     (db._now_iso(), int(invite_id)))
    try:
        with conn:
            conn.execute(
                "INSERT INTO calendar_writes (account, calendar, event_id, summary, start_at, "
                "end_at, ts, action) VALUES (?,?,?,?,?,?,?,?)",
                (account, "primary", event_id, row["summary"] or "", row["start_at"] or "",
                 row["start_at"] or "", db._now_iso(), f"invite:{len(attendees)}"))
    except Exception as e:  # the invites went out; the audit row is secondary
        log(f"calendar_invite: #{invite_id} sent but not audited ({e!r})")
    log(f"calendar_invite: {len(attendees)} invite(s) sent for {event_id} on {account}")
    return {"id": int(invite_id), "event_id": event_id, "sent_to": attendees}


if __name__ == "__main__":
    c = db.connect()
    for p in pending(c):
        print(f"#{p['id']} {p['account']} {p['summary']} @ {p['start_at']} -> "
              f"{', '.join(p['attendees'])}"
              + (f"  (new: {', '.join(p['new_attendees'])})" if p["new_attendees"] else ""))
    sys.exit(0)
