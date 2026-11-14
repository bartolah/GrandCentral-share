"""Dev #85 (§6 2026-11-03, Casey: "If I reply to an email, it shouldn't still
show up as unread" → "Yes, in Gmail too"). Pins that a sent reply adds
\\Seen to that one thread in Gmail and clears unread on the board, and that a
Gmail failure never surfaces as a failed send. Scratch SQLite, no IMAP.

    .venv/bin/python -m unittest tests.test_replied_read -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import mail_actions  # noqa: E402

A = "acctone"
T = "1800000000000000001"


class RepliedRead(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        # db.connect's default path is bound at import, so point it here
        real = db.connect
        self._env = mock.patch.object(db, "connect", lambda path=self.path: real(path))
        self._env.start()
        conn = db.connect()
        with conn:
            for acct in (A, "accttwo"):   # same thread id in another account
                conn.execute(
                    "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
                    "last_from_email, last_direction, snippet, last_date, unread, in_inbox, synced_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1,1,?)",
                    (T, acct, "s", "[]", "x", "x@example.com", "inbound", "", "2026-11-03T00:00:00Z",
                     "2026-11-03T00:00:00Z"))
        conn.close()

    def tearDown(self):
        self._env.stop()
        self._dir.cleanup()

    def _unread(self, acct):
        conn = db.connect(self.path)
        try:
            return conn.execute("SELECT unread FROM mail_threads WHERE account=? AND thread_id=?",
                                (acct, T)).fetchone()[0]
        finally:
            conn.close()

    def test_reply_marks_that_thread_seen_in_gmail_and_on_the_board(self):
        with mock.patch.object(mail_actions, "_store_all_mail") as store:
            mail_actions.replied_read(T, A, env={}, log=lambda *_: None)
        store.assert_called_once()
        args = store.call_args.args
        self.assertEqual(args[:4], (T, A, "+FLAGS", r"(\Seen)"))
        self.assertEqual(self._unread(A), 0)
        self.assertEqual(self._unread("accttwo"), 1)      # the other account's thread is untouched

    def test_a_gmail_failure_only_logs(self):
        logs = []
        with mock.patch.object(mail_actions, "_store_all_mail", side_effect=RuntimeError("imap down")):
            mail_actions.replied_read(T, A, env={}, log=logs.append)   # must not raise
        self.assertTrue(any("stayed unread in Gmail" in l for l in logs))
        self.assertEqual(self._unread(A), 0)


if __name__ == "__main__":
    unittest.main()
