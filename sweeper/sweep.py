#!/usr/bin/env python3
"""Grand Central sweeper — Phase 1 (email, two Gmail accounts).

Orchestrates the sweep and is the single entry point launchd runs:

    readers (Gmail IMAP)  ->  assess (Claude, per account)  ->  build_state  ->  atomic write

Design rules honored here (spec §3, §6):
  - If a source fails, still write the file, and mark that source absent from
    sources_swept (the dashboard shows a stale-source warning). Never crash
    without writing, never write a partial file that looks complete.
  - Read-only against every source; the only writes are grand_central.json and
    this run's log.
  - Atomic JSON write (temp + rename) so the dashboard never sees a half file.

Run manually:
    cd sweeper && ../.venv/bin/python sweep.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta

# When launchd runs `python sweep.py`, the script's dir is on sys.path, so the
# sibling modules import cleanly.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import (  # noqa: E402
    LOGS_DIR, OUTPUT_PATH, RunLog, atomic_write_json, iso_z,
    load_env, load_registry,
)
import db  # noqa: E402
from assess import assess_threads  # noqa: E402
from build_state import build_state  # noqa: E402
from sources.calendar_reader import read_events  # noqa: E402
from sources.gmail_reader import read_account  # noqa: E402
from sources import gmail_reader  # noqa: E402

# Hourly sweeps across work hours (spec §8 q2: Casey chose hourly).
SWEEP_HOURS = tuple(range(7, 20))  # 07:00 .. 19:00 inclusive
LOG_RETENTION_DAYS = 30


def next_sweep_after(now_local):
    """Next scheduled sweep at or after now (local tz)."""
    candidates = []
    for day_offset in (0, 1):
        day = now_local + timedelta(days=day_offset)
        for h in SWEEP_HOURS:
            c = day.replace(hour=h, minute=0, second=0, microsecond=0)
            if c > now_local:
                candidates.append(c)
    return min(candidates)


def prune_old_logs(now):
    cutoff = now.timestamp() - LOG_RETENTION_DAYS * 86400
    for name in os.listdir(LOGS_DIR):
        if not name.startswith("sweep-") or not name.endswith(".log"):
            continue
        p = os.path.join(LOGS_DIR, name)
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


LOCK_PATH = os.path.join(os.path.dirname(LOGS_DIR), "data", "sweep.lock")
LOCK_STALE_S = 15 * 60   # a sweep takes ~4 min; anything older is a crash


def _acquire_lock() -> bool:
    """One sweep at a time (the hourly launchd run and the dashboard's
    Sweep-now button share this). Stale locks from crashed runs are broken."""
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            if time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE_S:
                return False
        except OSError:
            pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


def main():
    os.makedirs(LOGS_DIR, exist_ok=True)
    started = time.monotonic()
    now_local = datetime.now().astimezone()
    log = RunLog()

    if not _acquire_lock():
        log("another sweep is already running (data/sweep.lock is fresh) — exiting")
        return 3

    log("Grand Central sweep starting (Phase 1: email x2)")

    env = load_env()
    reg = load_registry()
    lookback = reg.get("defaults", {}).get("sweep_lookback_days", 7)

    # ---- 0. SQLite store ----------------------------------------------------
    # The DB is the system of record; grand_central.json is GENERATED from
    # sweep data each run. A DB failure must not stop the board from
    # updating — degrade loudly. The registry is no longer synced into the
    # DB (projects/goals are retired; their tables are kept, frozen).
    conn = None
    try:
        conn = db.connect()
    except Exception as e:
        log(f"db: ERROR ({e!r}) — sweeping without the store this run "
            f"(dismissals may be missing until it recovers)")
        conn = None

    # ---- 1. read each account; a failure marks the source absent -----------
    # Accounts are derived from config/.env each run, so a Gmail added via the
    # Connections UI is swept on the next cycle with no code change.
    records = []
    sources_swept = []
    for account in gmail_reader.scan_accounts(env):
        src = f"gmail:{account}"
        t0 = time.monotonic()
        try:
            recs = read_account(account, env, lookback_days=lookback, log=log)
            # E258: a Private thread never reaches the assessor or the board
            import private
            priv = private.keys()
            recs = [r for r in recs if (account, str(r.get("thread_id"))) not in priv]
            records.extend(recs)
            sources_swept.append(src)
            log(f"{src}: {len(recs)} threads in {time.monotonic()-t0:.1f}s")
        except Exception as e:
            log(f"{src}: FAILED ({e!r}) — marking source absent")

    # ---- 1b. calendar (ICS secret URLs); failure marks the source absent ---
    meetings = []
    calendar_days = []
    try:
        calendar_days = read_events(env, log=log)
        meetings = [{"time": e["time"], "title": e["title"], "granola": False,
                     **({"location": e["location"]} if e.get("location") else {}),
                     **({"detail": e["detail"]} if e.get("detail") else {})}
                    for e in calendar_days[0]["events"]]
        sources_swept.append("gcal")
    except Exception as e:
        log(f"gcal: FAILED ({e!r}) — marking source absent")

    # ---- 1c. granola (E2-P3) — only attempted when a token exists ----------
    # Without GRANOLA_TOKEN the source is not tried and not listed in
    # sources_swept; the reader is UNVERIFIED against the real service until
    # Casey provides access (see sources/granola_reader.py header).
    granola_meetings = []
    if env.get("GRANOLA_TOKEN", "").strip():
        try:
            from sources.granola_reader import read_meetings
            granola_meetings = read_meetings(env, log=log)
            if conn:
                db.record_granola_meetings(conn, granola_meetings, log=log)
            sources_swept.append("granola")
        except Exception as e:
            log(f"granola: FAILED ({e!r}) — marking source absent")

    # ---- 2. assess: summary / reply_needed / importance per thread ---------
    # NOT project classification. Threads are assessed per account and keyed
    # by (account, thread_id); a failure degrades to subject-only summaries.
    assessed = {}
    if records:
        try:
            assessed = assess_threads(records, env, log=log)
        except Exception as e:
            log(f"assess: FAILED ({e!r}) — board uses subjects, importance defaults")
    if conn and assessed:
        # E209: keep the assessor's judgement with the classification, so the
        # Classified view shows respond / priority / summary between sweeps
        try:
            n = db.class_merge_model(conn, assessed, "haiku")
            log(f"classify: stored assessor judgement on {n} thread(s)")
        except Exception as e:
            log(f"classify: storing assessor judgement FAILED ({e!r})")

    # ---- 3. assemble + atomic write ----------------------------------------
    # Guard: a run that swept NO source and gathered NO threads (e.g. before
    # credentials are filled in) must not blank a previously-good dashboard.
    # Partial failures still write and mark the source absent (spec §3); only a
    # total no-op preserves the last-known-good file.
    if not sources_swept and not records and os.path.exists(OUTPUT_PATH):
        log("no sources swept and no threads — preserving existing grand_central.json "
            "(fill in config/.env). Not overwriting.")
        log(f"sweep finished in {time.monotonic()-started:.1f}s exit=2")
        log_path = os.path.join(LOGS_DIR, "sweep-" + now_local.strftime("%Y%m%d-%H%M%S") + ".log")
        log.write_to(log_path)
        _release_lock()
        return 2

    dismissed = db.dismissed_keys(conn, records) if conn else set()
    if dismissed:
        log(f"dismissed: {len(dismissed)} thread(s) suppressed from this sweep's board")
    if conn:
        # E5.1: an active snooze suppresses the thread from the rails like a
        # dismissal, until its wake time passes.
        snoozed = db.snoozed_keys(conn)
        if snoozed:
            log(f"snoozed: {len(snoozed)} thread(s) suppressed until wake")
            dismissed = dismissed | snoozed

    engagements = db.open_engagements(conn) if conn else []
    decisions = db.open_decisions(conn) if conn else []
    mail_cats = db.mail_categories(conn) if conn else {}
    sender_priors = db.sender_priors(conn) if conn else {}   # E47
    if engagements or decisions:
        log(f"store: {len(engagements)} open task(s), {len(decisions)} open decision(s)")

    exit_code = 0
    try:
        state = build_state(
            records, assessed, reg,
            generated_at=iso_z(now_local),
            next_sweep_at=iso_z(next_sweep_after(now_local)),
            sources_swept=sources_swept,
            meetings=meetings,
            calendar_days=calendar_days,
            dismissed=dismissed,
            engagements=engagements,
            decisions=decisions,
            mail_categories=mail_cats,
            sender_priors=sender_priors,
            log=log,
        )
        atomic_write_json(OUTPUT_PATH, state)
        log(f"wrote {OUTPUT_PATH} atomically | sources_swept={sources_swept} "
            f"| reply_debt={len(state['reply_debt'])} threads={len(records)}")
        if not sources_swept:
            log("WARNING: no sources swept — dashboard will show a stale-source warning")
    except Exception as e:
        log(f"ERROR assembling/writing state: {e!r}")
        exit_code = 1

    # ---- inbox steward (E59) — audit unvouched senders, suggest bundles ----
    # Non-critical: runs after the board is written, fails soft, bounded to
    # 25 never-before-audited senders per sweep (one cheap Haiku call).
    if env.get("ANTHROPIC_API_KEY", "").strip():
        try:
            from steward import audit_senders
            audit_senders(env, log=log)
        except Exception as e:
            log(f"steward: FAILED ({e!r}) — suggestions skipped this sweep")
        # ---- 🧙 wizard (E111) — propose todos from mail + meeting follow-ups.
        # Suggestions only; every todo waits for Casey's ✓ on the Wizard page.
        try:
            import wizard
            wizard.run(env, meetings=granola_meetings, log=log)
        except Exception as e:
            log(f"wizard: FAILED ({e!r}) — suggestions skipped this sweep")

    if conn:
        try:
            conn.close()
        except Exception:
            pass

    elapsed = time.monotonic() - started
    log(f"sweep finished in {elapsed:.1f}s exit={exit_code}")

    log_path = os.path.join(LOGS_DIR, "sweep-" + now_local.strftime("%Y%m%d-%H%M%S") + ".log")
    try:
        prune_old_logs(now_local)
    except OSError:
        pass
    log.write_to(log_path)
    _release_lock()
    return exit_code


if __name__ == "__main__":
    import egress  # E258: outbound hosts allowlisted (log-only until enforced)
    egress.install("sweep")
    sys.exit(main())
