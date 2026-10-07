"""E258 — the "Private" label: a thread that carries it does not exist in Grand Central.

Bart, 2026-09-30: "What if we create a label that any account can use that is
ignored automatically, like 'private', and those emails, once added are removed
from Grand Central?" — then chose "Gone entirely" (never stored, never shown
on the board, never sent to Claude, Jev or any agent) over hiding it from
models only, and chose the label over walling whole accounts. BUILD_SPEC §6
E258 item 2.

How it holds:
  * scan_imap() runs one Gmail search per account (`label:private`, All Mail)
    on every mail_sync (~3 min) and every sweep, so the label counts wherever
    and whenever it is applied — inbox, sent, archived, by hand or by filter.
  * mark() latches the (account, thread_id) in private_threads — ids only —
    and purges everything Grand Central stored about it. Gmail labels are per
    message, so a later reply without the label stays out: the latch is what
    counts, not the label on the newest message.
  * Every reader checks the latch: mail_sync, the sweep reader, commitment
    mining, fetch_thread / reply paths (so the Ask bar, drafts and send), search,
    the todo-label sync, and every JSON response the board serves (scrub()).
  * Only a paired browser can unmark (POST /private, human-only in the gate).
    Removing the label in Gmail does not bring a thread back by itself, and
    nothing an agent can call touches the label.

What this cannot do: anything sent to Anthropic or Jev before the label was
applied is not recalled. Apply the label with Gmail filters on arrival.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

LABEL = "Private"
QUERY = "label:private"   # Gmail label search is case-insensitive


def has_label(labels) -> bool:
    return any(str(l).strip().strip('"').lower() == LABEL.lower() for l in (labels or ()))


def _conn(conn):
    if conn is not None:
        return conn, False
    import db
    return db.connect(), True


def keys(conn=None) -> set:
    c, own = _conn(conn)
    try:
        return {(a, t) for a, t in c.execute("SELECT account, thread_id FROM private_threads")}
    finally:
        if own:
            c.close()


_FAST = {"at": 0.0, "keys": set()}


def keys_cached(max_age: float = 10.0) -> set:
    """keys() for the long-running board, re-read at most every max_age
    seconds on a bare connection (no DDL/migrations per request). A thread
    latched by mail_sync disappears from every view within max_age."""
    import sqlite3
    import time
    now = time.monotonic()
    if now - _FAST["at"] < max_age:
        return _FAST["keys"]
    try:
        from common import DB_PATH
    except ImportError:  # pragma: no cover
        from .common import DB_PATH
    try:
        c = sqlite3.connect(DB_PATH, timeout=5)
        try:
            _FAST["keys"] = {(a, t) for a, t in c.execute(
                "SELECT account, thread_id FROM private_threads")}
        finally:
            c.close()
    except sqlite3.Error:
        pass  # table not created yet: nothing is Private
    _FAST["at"] = now
    return _FAST["keys"]


def is_private(account: str, thread_id: str, conn=None) -> bool:
    c, own = _conn(conn)
    try:
        return c.execute("SELECT 1 FROM private_threads WHERE account = ? AND thread_id = ?",
                         (account, str(thread_id))).fetchone() is not None
    finally:
        if own:
            c.close()


def guard(account: str, thread_id: str, conn=None) -> None:
    """Raise the same not-found a missing thread gets — a Private thread does
    not exist here, and saying 'it is private' would itself be a signal."""
    if account and thread_id and is_private(account, str(thread_id), conn):
        raise ValueError("thread not found")


# (table, where-clause) — every stored row derived from a thread. Todos stay
# (they are Bart's), but lose their link to the thread. Sends and scheduled
# replies stay: they are Bart's own words and the send log is an audit trail.
_PURGE = [
    ("mail_threads", "account = :a AND thread_id = :t"),
    ("mail_triage", "account = :a AND thread_id = :t"),
    ("mail_bodies", "account = :a AND thread_id = :t"),
    ("mail_class", "account = :a AND thread_id = :t"),
    ("class_feedback", "account = :a AND thread_id = :t"),
    ("engagement_threads", "account = :a AND thread_id = :t"),
    ("dismissals", "account = :a AND thread_id = :t"),
    ("corrections", "account = :a AND thread_id = :t"),
    ("wizard_suggestions", "account = :a AND thread_id = :t"),
    ("wizard_scan", "ref = :ref"),
    ("project_items", "kind = 'thread' AND account = :a AND ref = :t"),
    ("project_declines", "account = :a AND thread_id = :t"),
]


def purge(conn, account: str, thread_id: str) -> int:
    removed = 0
    params = {"a": account, "t": str(thread_id), "ref": f"{account}:{thread_id}"}
    cols_of = {}
    for table, where in _PURGE:
        if table not in cols_of:
            cols_of[table] = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        cols = cols_of[table]
        needed = set(re.findall(r"\b([a-z_]+)\s*=", where))
        if not cols or not needed <= cols:
            continue  # table or column absent in this database
        removed += conn.execute(f"DELETE FROM {table} WHERE {where}", params).rowcount
    return removed


def mark(conn, account: str, thread_id: str) -> bool:
    """Latch and purge. True if it was newly marked."""
    thread_id = str(thread_id)
    with conn:
        new = conn.execute(
            "INSERT OR IGNORE INTO private_threads (account, thread_id, marked_at) VALUES (?, ?, ?)",
            (account, thread_id, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        ).rowcount > 0
        purge(conn, account, thread_id)
    return new


def unmark(conn, account: str, thread_id: str) -> bool:
    with conn:
        return conn.execute("DELETE FROM private_threads WHERE account = ? AND thread_id = ?",
                            (account, str(thread_id))).rowcount > 0


def scan_imap(imap, account: str, conn=None, log=print) -> int:
    """Find every thread labelled Private in this account (All Mail) and latch
    it. Leaves the mailbox selection changed — callers re-SELECT after. Never
    raises: a failed scan must not stop a sync, and the per-reader checks
    still hold the threads already latched."""
    try:
        import mail_common as mc
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        typ, _ = imap.select(f'"{all_mail}"', readonly=True)
        if typ != "OK":
            return 0
        typ, data = imap.uid("SEARCH", "X-GM-RAW", f'"{QUERY}"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if not uids:
            return 0
        thrids = set()
        for i in range(0, len(uids), 200):
            chunk = b",".join(uids[i:i + 200])
            typ, rows = imap.uid("FETCH", chunk, "(X-GM-THRID)")
            for part in rows or []:
                raw = part[0] if isinstance(part, tuple) else part
                if isinstance(raw, bytes):
                    m = re.search(rb"X-GM-THRID (\d+)", raw)
                    if m:
                        thrids.add(m.group(1).decode())
        c, own = _conn(conn)
        try:
            new = sum(1 for t in thrids if mark(c, account, t))
        finally:
            if own:
                c.close()
        if new:
            log(f"private:{account} {new} thread(s) newly marked Private and purged")
        return new
    except Exception as e:
        log(f"private:{account} scan failed ({e!r}) — latched threads stay out")
        return 0


def scrub(obj, private: set, default_account: str | None = None):
    """Drop every row (dict in a list) that names a Private thread. A row
    without its own account is matched against the request's account."""
    if not private:
        return obj

    def gone(row) -> bool:
        if not isinstance(row, dict):
            return False
        tid = row.get("thread_id") or row.get("thread")
        if not tid:
            return False
        acct = row.get("account") or default_account
        return (acct, str(tid)) in private

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o if not gone(x)]
        return o

    return walk(obj)
