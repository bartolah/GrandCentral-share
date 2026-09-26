"""E81 — last-week thread-body cache (Bart: "cache the last week of emails
content? Not necessarily attachments?").

Opening a thread used to be a live IMAP fetch (~3s). This module keeps the
exact /thread payload in SQLite (db: mail_bodies) for threads whose
last_date is within CACHE_DAYS:

    warm()    called by mail_sync every ~3 min — fetches up to WARM_CAP
              missing/stale threads, newest first, then prunes rows older
              than PRUNE_DAYS. Failures never break the sync.
    cached()  serve.py's read path — returns the payload only when the
              stored staleness token equals mail_threads.last_date (which
              mail_sync refreshes), so a thread that grew a message goes
              back to a live fetch within one sync cycle.
    store()   write-through after any live fetch.

Attachments are never stored anywhere in GC (existence flags only); if a
payload is oversized we drop inline images first (they ARE attachment
content), then body_html — the plain-text body always survives.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import db
import mail_actions

CACHE_DAYS = 7
PRUNE_DAYS = 14
WARM_CAP = 8            # per 3-min sync run — a week backfills in hours
MAX_JSON = 400_000      # bytes; above this, shed inline images / html


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slim(payload: dict) -> str:
    # default=str: parse_msg occasionally hands back email.header.Header
    # objects (undecoded To/Cc) — stringify rather than dying (seen live
    # on a crl thread, 2026-08-02).
    js = json.dumps(payload, default=str)
    if len(js) <= MAX_JSON:
        return js
    slim = {**payload, "messages": [{**m, "inline_images": []}
                                    for m in payload.get("messages", [])]}
    js = json.dumps(slim, default=str)
    if len(js) <= MAX_JSON:
        return js
    slim = {**slim, "messages": [{**m, "body_html": None}
                                 for m in slim["messages"]]}
    return json.dumps(slim, default=str)


def _thread_last_date(conn: sqlite3.Connection, account: str,
                      thread_id: str) -> str | None:
    r = conn.execute(
        "SELECT last_date FROM mail_threads WHERE account=? AND thread_id=?",
        (account, thread_id)).fetchone()
    return r["last_date"] if r else None


def store(conn: sqlite3.Connection, account: str, thread_id: str,
          payload: dict) -> None:
    """Write-through: stamp the CURRENT mail_threads.last_date as the
    staleness token (None if the thread isn't tracked — freshness then
    falls back to fetched_at age in cached())."""
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO mail_bodies "
            "(account, thread_id, fetched_at, last_date, payload) "
            "VALUES (?,?,?,?,?)",
            (account, thread_id, _now(),
             _thread_last_date(conn, account, thread_id), _slim(payload)))


def cached(conn: sqlite3.Connection, account: str, thread_id: str) -> dict | None:
    row = conn.execute(
        "SELECT fetched_at, last_date, payload FROM mail_bodies "
        "WHERE account=? AND thread_id=?", (account, thread_id)).fetchone()
    if not row:
        return None
    live = _thread_last_date(conn, account, thread_id)
    if live is not None:
        if row["last_date"] != live:
            return None
    else:
        # untracked thread (e.g. a search hit): trust for 15 minutes
        try:
            age = datetime.now(timezone.utc) - datetime.strptime(
                row["fetched_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if age > timedelta(minutes=15):
                return None
        except Exception:
            return None
    try:
        out = json.loads(row["payload"])
        return None if out.get("missing") else out
    except Exception:
        return None


def warm(env: dict, log=print, cap: int = WARM_CAP) -> None:
    """Fetch bodies for recent threads that are missing or stale, newest
    first, then prune old rows. Bounded so the 3-min sync stays fast."""
    conn = db.connect()
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = conn.execute(
            "SELECT t.account, t.thread_id FROM mail_threads t "
            "LEFT JOIN mail_bodies b "
            "  ON b.account = t.account AND b.thread_id = t.thread_id "
            "WHERE t.last_date >= ? "
            "  AND (b.thread_id IS NULL OR b.last_date IS NOT t.last_date) "
            "ORDER BY t.last_date DESC LIMIT ?", (cutoff, cap)).fetchall()
        done = 0
        for r in rows:
            try:
                payload = mail_actions.fetch_thread(
                    r["thread_id"], r["account"], env=env, with_labels=True)
                store(conn, r["account"], r["thread_id"], payload)
                done += 1
            except Exception as e:
                # Tombstone permanently-gone threads (purged spam/trash) so
                # they stop consuming a warm slot every cycle; cached()
                # treats the tombstone as a miss, so a live open still
                # surfaces the real error.
                if "not found" in str(e):
                    store(conn, r["account"], r["thread_id"],
                          {"messages": [], "labels": [], "missing": True})
                log(f"mail_cache: {r['account']}:{r['thread_id']} skipped ({e!r})")
        prune_cutoff = (datetime.now(timezone.utc)
                        - timedelta(days=PRUNE_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            pruned = conn.execute(
                "DELETE FROM mail_bodies WHERE last_date < ? "
                "OR (last_date IS NULL AND fetched_at < ?)",
                (prune_cutoff, prune_cutoff)).rowcount
        if done or pruned:
            log(f"mail_cache: warmed {done}/{len(rows)} thread(s), pruned {pruned}")
    finally:
        conn.close()
