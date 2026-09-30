"""Answer a calendar invitation from its email: Yes / Maybe / No (Dev #16).

Approved by Bart 2026-09-27 (BUILD_SPEC §6 "Calendar RSVP"): "Yes, I approve
of the decision. Go with whatever Google Calendar supports. I'm fine with
RSVP anywhere." It amends E21's create-only rule by exactly one verb:

  - RSVP only: Google Calendar's accepted / tentative / declined on Bart's
    OWN response. No edits, no moves, no deletes, no attendee changes, and
    no note to the organizer (gog offers --comment; it is never passed).
  - one event per click, human click only — not an Ask tool, and
    `/calendar/rsvp` is in gc_client.DENIED
  - every RSVP audited in `calendar_writes` (action = 'rsvp:<status>')

How an email becomes an event: the invite carries a text/calendar part whose
UID is the event's iCalUID. We read that part (read-only IMAP), then list
the account's primary calendar around the invite's start and match iCalUID —
which works for Google and Outlook organizers alike, where deriving the id
from the UID only works for Google. A series invite answers the series; an
invite for one occurrence (RECURRENCE-ID) answers that occurrence.

    cd sweeper
    ../.venv/bin/python -m calendar_rsvp --account personal --thread 1877... [--yes|--maybe|--no] [--dry-run]
"""

from __future__ import annotations

import argparse
import email
import json
import os
import re
import shutil
import subprocess
from datetime import date, timedelta

try:
    from . import db
    from . import mail_common as mc
    from .common import load_env
except ImportError:  # pragma: no cover - direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db
    import mail_common as mc
    from common import load_env

GOG_TIMEOUT = 45
RESPONSES = {"yes": "accepted", "maybe": "tentative", "no": "declined"}


def _email_for(account: str, env: dict) -> str:
    """Every account's address is GMAIL_<SLUG>_USER (same convention as
    gmail_reader.scan_accounts); gog's grant is keyed by that address."""
    addr = env.get(f"GMAIL_{account.upper()}_USER", "")
    if not addr:
        raise ValueError(f"unknown account {account!r}")
    return addr


def _gog(env: dict, *args: str) -> dict:
    gog = (shutil.which("gog")
           or next((p for p in ("/opt/homebrew/bin/gog", "/usr/local/bin/gog")
                    if os.path.exists(p)), None))
    if gog is None:
        raise RuntimeError("gog CLI not installed (brew install steipete/tap/gog)")
    # Same file-keyring arrangement as calendar_writer: the login keychain is
    # locked headless, so gog's keyring password comes from config/.env.
    proc_env = dict(os.environ)
    if env.get("GOG_KEYRING_PASSWORD"):
        proc_env["GOG_KEYRING_PASSWORD"] = env["GOG_KEYRING_PASSWORD"]
    proc = subprocess.run([gog, *args, "-j", "--no-input"], capture_output=True,
                          text=True, timeout=GOG_TIMEOUT, env=proc_env)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:400]
        acct = args[args.index("-a") + 1] if "-a" in args else "this account"
        if "refresh token" in err.lower() or "auth add" in err.lower() or "no token" in err.lower():
            raise PermissionError(
                f"Calendar isn't connected for {acct} yet — on the Mini run: "
                f"gog auth add {acct} --services calendar")
        raise RuntimeError(f"gog: {err}")
    try:
        return json.loads(proc.stdout or "{}")
    except ValueError:
        return {}


# ---------------------------------------------------------------- the invite

def _unfold_ics(text: str) -> list:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def parse_ics(text: str) -> dict | None:
    """The first VEVENT of an iCalendar body: {method, uid, summary, start,
    recurrence_id, organizer}. `start`/`recurrence_id` are YYYY-MM-DD — only
    used to bound the calendar lookup, so the time of day doesn't matter."""
    method, ev, inside = "", {}, False
    for line in _unfold_ics(text):
        name, _, value = line.partition(":")
        key = name.split(";")[0].upper()
        if key == "METHOD" and not inside:
            method = value.strip().upper()
        elif key == "BEGIN" and value.strip().upper() == "VEVENT":
            inside = True
        elif key == "END" and value.strip().upper() == "VEVENT":
            break
        elif inside and key in ("UID", "SUMMARY", "DTSTART", "RECURRENCE-ID", "ORGANIZER"):
            ev.setdefault(key, value.strip())
    if not ev.get("UID"):
        return None
    day = lambda v: (f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
                     if (m := re.match(r"(\d{4})(\d{2})(\d{2})", v or "")) else "")
    return {"method": method, "uid": ev["UID"],
            "summary": ev.get("SUMMARY", "").replace("\\,", ",").replace("\\;", ";"),
            "start": day(ev.get("DTSTART")), "recurrence_id": day(ev.get("RECURRENCE-ID")),
            "organizer": re.sub(r"^mailto:", "", ev.get("ORGANIZER", ""), flags=re.I)}


def _calendar_parts(msg) -> list:
    out = []
    for part in msg.walk():
        ct = part.get_content_type()
        if ct in ("text/calendar", "application/ics"):
            raw = part.get_payload(decode=True) or b""
            out.append(raw.decode(part.get_content_charset() or "utf-8", "replace"))
    return out


def invite_for_thread(account: str, thread_id: str, env: dict) -> dict | None:
    """The newest REQUEST invitation in a thread, or None. Read-only IMAP."""
    thread_id = mc.validate_thread_id(thread_id)
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=True)
        for uid in reversed(uids[-10:]):
            typ, d = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            for ics in _calendar_parts(email.message_from_bytes(d[0][1])):
                inv = parse_ics(ics)
                # REQUEST is an invitation; REPLY/CANCEL are not askable.
                if inv and inv["method"] in ("REQUEST", ""):
                    return inv
        return None
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- the event

def find_event(addr: str, inv: dict, env: dict) -> dict | None:
    """The calendar event the invite is about, matched by iCalUID in a
    window around its start. Returns {event_id, my_status, summary, link}."""
    anchor = inv.get("recurrence_id") or inv.get("start")
    if not anchor:
        return None
    d0 = date.fromisoformat(anchor)
    out = _gog(env, "calendar", "events", "primary", "-a", addr,
               "--from", (d0 - timedelta(days=1)).isoformat(),
               "--to", (d0 + timedelta(days=2)).isoformat(), "--max", "250")
    for ev in out.get("events") or []:
        if ev.get("iCalUID") != inv["uid"]:
            continue
        # A series invite answers the series; a one-occurrence invite, just it.
        eid = ev.get("id")
        if ev.get("recurringEventId") and not inv.get("recurrence_id"):
            eid = ev["recurringEventId"]
        me = next((a for a in ev.get("attendees") or [] if a.get("self")), None)
        return {"event_id": eid, "summary": ev.get("summary") or inv["summary"],
                "my_status": (me or {}).get("responseStatus"),
                "is_attendee": bool(me), "link": ev.get("htmlLink"),
                "organizer_is_me": bool((ev.get("organizer") or {}).get("self"))}
    return None


def status(account: str, thread_id: str, env: dict | None = None) -> dict:
    """What the invite bar shows: is this an answerable invite, and what did
    Bart already say? {invite: bool, summary, start, my_status, reason}."""
    env = env or load_env()
    inv = invite_for_thread(account, thread_id, env)
    if not inv:
        return {"invite": False}
    base = {"invite": True, "summary": inv["summary"], "start": inv["start"],
            "organizer": inv["organizer"]}
    try:
        ev = find_event(_email_for(account, env), inv, env)
    except PermissionError as e:
        return {**base, "can_rsvp": False, "reason": str(e)}
    if not ev:
        return {**base, "can_rsvp": False,
                "reason": "Not on your calendar — it may have been cancelled or moved."}
    if not ev["is_attendee"] or ev["organizer_is_me"]:
        return {**base, "can_rsvp": False, "reason": "You're not an invitee on this one."}
    return {**base, "can_rsvp": True, "my_status": ev["my_status"], "link": ev["link"]}


def rsvp(account: str, thread_id: str, response: str, env: dict | None = None,
         dry_run: bool = False, log=print) -> dict:
    """Set Bart's response on the invite in this thread. response: yes|maybe|no."""
    if response not in RESPONSES:
        raise ValueError("response must be yes, maybe or no")
    env = env or load_env()
    addr = _email_for(account, env)
    inv = invite_for_thread(account, thread_id, env)
    if not inv:
        raise ValueError("no invitation in this thread")
    ev = find_event(addr, inv, env)
    if not ev:
        raise RuntimeError("that invitation isn't on your calendar")
    if not ev["is_attendee"] or ev["organizer_is_me"]:
        raise ValueError("you're not an invitee on this event")
    st = RESPONSES[response]
    args = ["calendar", "respond", "primary", ev["event_id"], "--status", st, "-a", addr]
    if dry_run:
        args.append("--dry-run")
    _gog(env, *args)
    if not dry_run:
        conn = db.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO calendar_writes (account, calendar, event_id,"
                    " summary, start_at, end_at, ts, action) VALUES (?,?,?,?,?,?,?,?)",
                    (account, "primary", ev["event_id"], ev["summary"] or "(invite)",
                     inv["start"] or "", None, db._now_iso(), f"rsvp:{st}"))
        finally:
            conn.close()
    log(f"calendar_rsvp: {'[dry-run] ' if dry_run else ''}{st} on {ev['event_id']} "
        f"({account}): {ev['summary']}")
    return {"account": account, "event_id": ev["event_id"], "status": st,
            "summary": ev["summary"], "dry_run": dry_run}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--account", required=True)
    ap.add_argument("--thread", required=True)
    g = ap.add_mutually_exclusive_group()
    for r in RESPONSES:
        g.add_argument(f"--{r}", dest="response", action="store_const", const=r)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    if not a.response:
        print(json.dumps(status(a.account, a.thread), indent=1))
        return 0
    print(json.dumps(rsvp(a.account, a.thread, a.response, dry_run=a.dry_run), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
