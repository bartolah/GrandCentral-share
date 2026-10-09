"""Grand Central's inbox matches Gmail's (Casey 2026-10-08: "I'm missing some
emails in my inbox, that are in my Gmail inbox" → "Inbox always matches
Gmail"). A thread hidden as done that Gmail still has in its inbox after a
grace period comes back on the next sync; one Gmail archived stays gone.
Scratch SQLite, fake account.

    .venv/bin/python -m unittest tests.test_inbox_matches_gmail -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402

A = "acctone"


def row(tid, last="2026-10-01T10:00:00Z"):
    return {"thread_id": tid, "subject": "s", "participants": [], "last_from": "pat@x.com",
            "last_from_email": "pat@x.com", "last_direction": "inbound", "snippet": "hi",
            "last_date": last, "message_count": 1, "unread": 0, "starred": 0, "bulk": 0,
            "category": "people", "attachments": 0, "link": "", "labels": []}


class InboxMatchesGmail(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def done(self, tid, at):
        self.conn.execute("INSERT OR REPLACE INTO mail_triage (account, thread_id, done_at, updated_at) "
                          "VALUES (?, ?, ?, ?)", (A, tid, at, at))
        self.conn.commit()

    def done_at(self, tid):
        return self.conn.execute("SELECT done_at FROM mail_triage WHERE account = ? AND thread_id = ?",
                                 (A, tid)).fetchone()[0]

    def test_hidden_but_still_in_gmail_comes_back(self):
        db.upsert_mail_threads(self.conn, A, [row("1"), row("2")])
        self.done("1", "2026-10-02T00:00:00Z")            # old board-only Handled
        self.done("2", "2026-10-02T00:00:00Z")
        db.upsert_mail_threads(self.conn, A, [row("1")])   # Gmail archived 2; 1 is still in its inbox
        self.assertIsNone(self.done_at("1"))
        self.assertEqual(self.done_at("2"), "2026-10-02T00:00:00Z")
        self.assertEqual([r["thread_id"] for r in db.mail_view(self.conn, account=A)], ["1"])

    def test_a_fresh_done_gets_a_grace_period_while_its_archive_lands(self):
        db.upsert_mail_threads(self.conn, A, [row("3")])
        self.done("3", db._now_iso())
        db.upsert_mail_threads(self.conn, A, [row("3")])
        self.assertIsNotNone(self.done_at("3"))


if __name__ == "__main__":
    unittest.main()
