"""Local-only dismissals written from the dashboard. Both `dashboard/serve.py`'s
/dismiss handler and `command.py`'s tool-calling loop call this one function —
one persistence path whether Casey clicked a button or typed a request into the
command bar.

Project re-file corrections (record_feedback) were retired with projects/goals;
the `corrections` table and MISCLASSIFICATIONS.md are kept, frozen, for rollback.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone

try:
    from . import db
except ImportError:  # pragma: no cover - direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db

_THRID_RE = re.compile(r"^\d{1,25}$")


def _clean(s, limit=200) -> str:
    return " ".join(str(s or "").split())[:limit]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_dismissed(payload: dict) -> dict:
    """Validate + persist one local-only dismissal. Returns the stored row.

    Never touches Gmail — just suppresses this thread from Reply Debt starting
    the next sweep (build_state honors it). An account is required: the
    dismissal only ever applies to that account's copy of the thread.
    """
    thread_id = _clean(payload.get("thread_id"), 25)
    if not _THRID_RE.match(thread_id):
        raise ValueError("bad thread_id")
    account = db.clean_account(payload.get("account"))

    row = {"thread_id": thread_id, "account": account, "ts": _now_iso()}
    conn = db.connect()
    try:
        db.record_dismissal(conn, row)
    finally:
        conn.close()
    return {"account": row["account"], "ts": row["ts"]}
