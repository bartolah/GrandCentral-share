"""Google Calendar reader via secret ICS URLs (Phase 1.5, Casey-approved).

Read-only and OAuth-free: each Google calendar exposes a "Secret address in
iCal format" (Calendar settings → Integrate calendar). Those URLs go in
config/.env as GCAL_ICS_URLS, comma-separated. We fetch each feed once,
expand recurring events over the next week, and return a per-day structure:
today's slice feeds the dashboard's Today panel, the full week feeds the
Week view.

Known trade-off (spec §9 pattern: simple first, OAuth in Phase 4 if needed):
Google refreshes secret ICS feeds lazily, so a just-added event can take a
few hours to appear. Multi-day events currently show on their start day only.

Run standalone:
    cd sweeper
    ../.venv/bin/python -m sources.calendar_reader
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, time as dtime, timedelta

import icalendar
import recurring_ical_events
import requests


def _emails(prop) -> list:
    """Pull lowercased email addresses from an ATTENDEE/ORGANIZER property,
    which icalendar hands back as a single value or a list of 'mailto:x@y'."""
    if prop is None:
        return []
    items = prop if isinstance(prop, list) else [prop]
    out = []
    for it in items:
        m = re.search(r"mailto:([^;>\s]+)", str(it), re.I)
        addr = (m.group(1) if m else str(it)).strip().lower()
        if "@" in addr:
            out.append(addr)
    return out

try:
    from ..common import load_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common import load_env

FETCH_TIMEOUT = 30
WINDOW_DAYS = 7


def _cal_label(cal: icalendar.Calendar) -> str:
    """Short chip label for which calendar an event came from.

    Pragmatic, Casey-specific mapping keyed off the feed's calendar name;
    extend as more calendars join.
    """
    name = str(cal.get("X-WR-CALNAME", "")).lower()
    if "brightline" in name:
        return "work"
    if "fieldhouse" in name:
        return "fieldhouse"
    return "personal"


def read_events(env: dict, log=print, days: int = WINDOW_DAYS) -> list:
    """Return the next `days` days starting today (the sweep contract)."""
    tz = datetime.now().astimezone().tzinfo
    return read_range(env, datetime.now(tz).date(), days, log=log)


def read_range(env: dict, start, days: int, log=print) -> list:
    """Return `days` days from `start` (a datetime.date, past or future) as
    [{date, label, today, events: [{time, title, cal}]}], events sorted.
    Recurring events expand over any window, so history works too.

    Raises when GCAL_ICS_URLS is unset or a feed fails, so the orchestrator
    can mark the source absent (spec §3) — same contract as the Gmail reader.
    """
    urls = [u.strip() for u in env.get("GCAL_ICS_URLS", "").split(",") if u.strip()]
    if not urls:
        raise RuntimeError("GCAL_ICS_URLS is not set in config/.env")

    tz = datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
    window_start = datetime.combine(start, dtime.min, tzinfo=tz)
    window_end = window_start + timedelta(days=days)

    per_day: dict = {start + timedelta(i): [] for i in range(days)}
    seen = set()
    total = 0
    for url in urls:
        resp = requests.get(url, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
        cal = icalendar.Calendar.from_ical(resp.content)
        label = _cal_label(cal)
        for ev in recurring_ical_events.of(cal).between(window_start, window_end):
            if str(ev.get("STATUS", "")).upper() == "CANCELLED":
                continue
            title = str(ev.get("SUMMARY", "")).strip() or "(untitled)"
            dtstart = ev.get("DTSTART").dt
            end = ""
            if isinstance(dtstart, datetime):
                local = dtstart.astimezone(tz)
                day, when, sort_key = local.date(), local.strftime("%H:%M"), local.strftime("%H%M")
                # E249: Home's day timeline draws real blocks, so an event
                # carries its end time too (same day only; "" when unknown).
                dtend = ev.get("DTEND")
                if dtend is not None and isinstance(dtend.dt, datetime):
                    end_local = dtend.dt.astimezone(tz)
                    if end_local.date() == day and end_local > local:
                        end = end_local.strftime("%H:%M")
            else:  # date-only VEVENT = all-day
                day, when, sort_key = dtstart, "all-day", "0000"
            if day not in per_day:
                continue
            key = (day, when, title)
            if key in seen:  # same event mirrored across calendars
                continue
            seen.add(key)
            # E43: attendees + organizer stay server-side; build_state drops
            # them from the board JSON.
            # E66 (OpenClaw feedback): location + a description snippet ride
            # along — "Flight NY > SF" with nothing else forced an agent into
            # a fruitless Gmail dig for the airline/flight number.
            per_day[day].append({"time": when, "end": end, "title": title, "cal": label,
                                 "location": str(ev.get("LOCATION", "")).strip()[:200],
                                 "detail": " ".join(str(ev.get("DESCRIPTION", "")).split())[:300],
                                 "attendees": _emails(ev.get("ATTENDEE")),
                                 "organizer": (_emails(ev.get("ORGANIZER")) or [""])[0],
                                 "_sort": sort_key})
            total += 1

    out = []
    for i in range(days):
        day = start + timedelta(i)
        events = sorted(per_day[day], key=lambda e: e["_sort"])
        for e in events:
            e.pop("_sort")
        out.append({
            "date": day.isoformat(),
            "label": f'{day.strftime("%a")} {day.strftime("%b")} {day.day}',
            "today": day == today,
            "events": events,
        })
    log(f"gcal: {total} events over {days}d from {start.isoformat()} "
        f"across {len(urls)} calendar(s)")
    return out


def read_meetings_today(env: dict, log=print) -> list:
    """Today's events in the Today-panel shape (kept for standalone use)."""
    day0 = read_events(env, log=log, days=1)[0]
    return [{"time": e["time"], "title": e["title"], "granola": False}
            for e in day0["events"]]


if __name__ == "__main__":
    for d in read_events(load_env()):
        print(f'\n{d["label"]}{"  ← today" if d["today"] else ""}')
        for e in d["events"]:
            print(f'  {e["time"]:>7}  [{e["cal"]}] {e["title"]}')
        if not d["events"]:
            print("   —")
