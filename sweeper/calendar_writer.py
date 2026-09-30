"""Create events on Bart's OWN Google calendars via the gog CLI (E21).

Approved by Bart 2026-07-19 ("make sure the OpenClaw integration can add
calendar items"). The ICS feeds in sources/calendar_reader.py stay the
read path; this module is the ONE calendar write, scoped deliberately:

  - create-only: no update, no delete — none may be added without a new
    explicit Bart decision (same pattern as the no-purge rule). RSVP was
    added by exactly such a decision (2026-09-27) and lives on its own in
    calendar_rsvp.py
  - own calendars only, NO attendees: an attendee list would put the event
    on other people's calendars and email them invites — that is outbound
    territory (spec §6) and is rejected here outright
  - every create is audited in the `calendar_writes` table

Mechanism: shells out to `gog` (https://github.com/steipete/gogcli, Homebrew)
with the account's email. gog holds a per-account OAuth refresh token in the
macOS Keychain — one-time interactive grant per account:

    gog auth add you@example.com --services calendar

Until that grant exists, create() raises with that exact instruction; the
IMAP app passwords in config/.env cannot talk to the Calendar API.

Times: RFC3339. Naive datetimes ("2026-07-21T15:00") are localized to the
Mini's timezone. `end` defaults to start + 1h; all-day events pass
date-only strings with all_day=True.

Run standalone (dry-run prints gog's intended action, writes nothing):
    cd sweeper
    ../.venv/bin/python -m calendar_writer --account personal \
        --summary "Test" --start 2026-07-21T15:00 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta

try:
    from . import db
    from .common import load_env, require_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db
    from common import load_env, require_env

# Same nickname -> env-key convention as gmail_reader.ACCOUNTS; the gog
# grant is keyed by the account's email address.
ACCOUNT_EMAIL_KEYS = {
    "personal": "GMAIL_PERSONAL_USER",
    "connectblue": "GMAIL_CONNECTBLUE_USER",
}

GOG_TIMEOUT = 30
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_when(value: str, all_day: bool, field: str) -> str:
    """Validate/normalize a start|end value. Date-only for all-day events,
    RFC3339 otherwise (naive input gets the Mini's local offset)."""
    value = (value or "").strip()
    if all_day:
        if not _DATE_ONLY.match(value):
            raise ValueError(f"{field}: all-day events take YYYY-MM-DD, got {value!r}")
        return value
    if _DATE_ONLY.match(value):
        raise ValueError(f"{field}: date-only {value!r} needs all_day=true")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field}: not RFC3339: {value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt.isoformat()


def create(account: str, summary: str, start: str, end: str = None,
           description: str = None, location: str = None,
           all_day: bool = False, env: dict | None = None,
           dry_run: bool = False, log=print) -> dict:
    """Create one event on the account's primary calendar. Returns
    {account, calendar, event_id, summary, start, end, link}. Raises with
    the auth instruction when the gog grant is missing."""
    if account not in ACCOUNT_EMAIL_KEYS:
        raise ValueError(f"unknown account {account!r} "
                         f"(expected {'|'.join(ACCOUNT_EMAIL_KEYS)})")
    summary = (summary or "").strip()
    if not summary:
        raise ValueError("summary is required")

    env = env or load_env()
    email = require_env(env, ACCOUNT_EMAIL_KEYS[account])

    start = _parse_when(start, all_day, "start")
    if end:
        end = _parse_when(end, all_day, "end")
    elif all_day:
        end = start  # gog treats a same-day all-day range as one day
    else:
        end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()

    # launchd jobs get a bare PATH — resolve Homebrew's install dirs explicitly
    gog = (shutil.which("gog")
           or next((p for p in ("/opt/homebrew/bin/gog", "/usr/local/bin/gog")
                    if os.path.exists(p)), None))
    if gog is None:
        raise RuntimeError("gog CLI not installed (brew install steipete/tap/gog)")

    cmd = [gog, "calendar", "create", "primary",
           "--summary", summary, "--from", start, "--to", end,
           "-a", email, "-j", "--no-input"]
    if all_day:
        cmd.append("--all-day")
    if description:
        cmd += ["--description", str(description)[:2000]]
    if location:
        cmd += ["--location", str(location)[:300]]
    if dry_run:
        cmd.append("--dry-run")

    # gog uses the FILE keyring backend here: the Mini's login keychain is
    # locked in every headless context (no TTY for its password prompt), the
    # same reason git needs the SSH deploy key. The file keyring is unlocked
    # by GOG_KEYRING_PASSWORD, which lives in config/.env (chmod 600, §6).
    proc_env = dict(os.environ)
    keyring_pw = env.get("GOG_KEYRING_PASSWORD")
    if keyring_pw:
        proc_env["GOG_KEYRING_PASSWORD"] = keyring_pw

    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=GOG_TIMEOUT, env=proc_env)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:500]
        if "refresh token missing" in err or "auth add" in err.lower():
            raise RuntimeError(
                f"gog is not authorized for {email} — Bart must run "
                f"once, in a session with a browser: "
                f"gog auth add {email} --services calendar")
        raise RuntimeError(f"gog calendar create failed: {err}")

    event_id = link = None
    try:
        out = json.loads(proc.stdout)
        ev = out.get("event") or out
        event_id = ev.get("id")
        link = ev.get("htmlLink")
    except (ValueError, AttributeError):
        pass  # dry-run and older gog builds print non-JSON; the create stood

    if not dry_run:
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO calendar_writes (account, calendar, event_id,"
                    " summary, start_at, end_at, ts) VALUES (?,?,?,?,?,?,?)",
                    (account, "primary", event_id, summary, start, end,
                     db._now_iso()))
        finally:
            conn.close()
        log(f"calendar_writer: created {event_id or '(id unparsed)'} "
            f"on {account}: {summary} @ {start}")

    return {"account": account, "calendar": "primary", "event_id": event_id,
            "summary": summary, "start": start, "end": end, "link": link,
            "dry_run": dry_run}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Create a calendar event (own calendar, no attendees)")
    ap.add_argument("--account", required=True, choices=sorted(ACCOUNT_EMAIL_KEYS))
    ap.add_argument("--summary", required=True)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end")
    ap.add_argument("--description")
    ap.add_argument("--location")
    ap.add_argument("--all-day", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    out = create(args.account, args.summary, args.start, end=args.end,
                 description=args.description, location=args.location,
                 all_day=args.all_day, dry_run=args.dry_run)
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
