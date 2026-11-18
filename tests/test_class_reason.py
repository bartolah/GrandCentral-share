"""E314 — "Not classified" says why.

Casey, 2026-11-16: a sent email from three days earlier was labelled "Not
classified — older than 30 days". Only threads synced from the inbox in the
last 30 days are classified; a sent email nobody has answered is not in the
inbox at all. The board now gets the reason from db.class_missing_reason.
Scratch SQLite; the same thread id in two accounts on purpose.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402

A, B = "acctone", "accttwo"


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class MissingReason(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        for acct, tid, ago in ((A, "101", 3), (A, "102", 45), (B, "103", 3)):
            self.conn.execute("INSERT INTO mail_threads (thread_id, account, subject, last_date, in_inbox) "
                              "VALUES (?,?,?,?,1)", (tid, acct, "s", _iso(ago)))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_each_reason(self):
        self.assertEqual(db.class_missing_reason(self.conn, A, "101"), "pending")
        self.assertEqual(db.class_missing_reason(self.conn, A, "102"), "too_old")
        self.assertEqual(db.class_missing_reason(self.conn, A, "999"), "not_synced")   # e.g. a sent email

    def test_the_reason_is_read_in_the_threads_own_account(self):
        self.assertEqual(db.class_missing_reason(self.conn, A, "103"), "not_synced")
        self.assertEqual(db.class_missing_reason(self.conn, B, "103"), "pending")


if __name__ == "__main__":
    unittest.main()
