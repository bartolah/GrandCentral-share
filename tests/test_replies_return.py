"""E317 — a reply brings an email back.

Casey, 2026-11-17: "some replies on emails aren't bringing those emails back
into my inbox". Done and moved threads already came back; two did not:
  * a snoozed thread stayed hidden until its snooze time, though Gmail's own
    snooze (and Gmail's inbox) wakes it as soon as someone replies;
  * an email inside a todo returned only while unread, so a reply read on
    the phone stayed tucked under the todo.
Only INCOMING mail newer than the snooze / the todo link counts; Casey's own
reply never does. Scratch SQLite; the same thread id in two accounts.
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

A, B, TID = "acctone", "accttwo", "1900000000000000042"


def _iso(minutes: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(last_date: str, direction: str = "inbound", tid: str = TID) -> dict:
    return {"thread_id": tid, "subject": "Proposal", "last_date": last_date,
            "last_direction": direction, "last_from": "Pat", "last_from_email": "pat@example.com",
            "message_count": 2, "category": "people"}


class RepliesReturn(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        for acct in (A, B):
            db.upsert_mail_threads(self.conn, acct, [_row(_iso(-120))])

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def triage(self, acct=A):
        return dict(self.conn.execute("SELECT * FROM mail_triage WHERE account=? AND thread_id=?",
                                      (acct, TID)).fetchone())

    def snooze(self, acct=A):
        db.set_mail_triage(self.conn, TID, acct, {"snoozed_until": _iso(60 * 24), "wake_inbox": 1})
        db.upsert_mail_threads(self.conn, acct, [])          # snooze archived it in Gmail

    # ---- snooze ----
    def test_a_reply_wakes_a_snoozed_thread_now(self):
        self.snooze()
        self.assertTrue(self.triage()["snoozed_at"])
        db.upsert_mail_threads(self.conn, A, [_row(_iso(1))])   # Gmail re-inboxed it on the reply
        t = self.triage()
        self.assertLessEqual(t["snoozed_until"], _iso(1))
        self.assertEqual(t["wake_inbox"], 0)

    def test_my_own_reply_does_not_wake_it(self):
        self.snooze()
        db.upsert_mail_threads(self.conn, A, [_row(_iso(1), direction="outbound")])
        self.assertGreater(self.triage()["snoozed_until"], _iso(60))

    def test_mail_from_before_the_snooze_does_not_wake_it(self):
        db.set_mail_triage(self.conn, TID, A, {"snoozed_until": _iso(60 * 24), "wake_inbox": 1})
        db.upsert_mail_threads(self.conn, A, [_row(_iso(-120))])   # archive lagged; nothing new
        self.assertGreater(self.triage()["snoozed_until"], _iso(60))

    def test_the_other_accounts_thread_is_untouched(self):
        self.snooze(A)
        self.snooze(B)
        db.upsert_mail_threads(self.conn, A, [_row(_iso(1))])
        self.assertGreater(self.triage(B)["snoozed_until"], _iso(60))

    # ---- todos ----
    def new_reply(self):
        thr = db.threads_for_todos(self.conn)
        return [t["new_reply"] for t in thr[self.todo]]

    def link(self):
        self.todo = db.add_engagement(self.conn, "Answer Pat", account=A)
        db.link_thread(self.conn, self.todo, A, TID)

    def test_a_reply_after_the_email_joined_the_todo_is_flagged(self):
        self.link()
        self.assertEqual(self.new_reply(), [False])          # the mail that was already there
        db.upsert_mail_threads(self.conn, A, [_row(_iso(1))])
        self.assertEqual(self.new_reply(), [True])

    def test_my_own_reply_or_an_archive_clears_it(self):
        self.link()
        db.upsert_mail_threads(self.conn, A, [_row(_iso(1), direction="outbound")])
        self.assertEqual(self.new_reply(), [False])
        db.upsert_mail_threads(self.conn, A, [_row(_iso(2))])
        db.upsert_mail_threads(self.conn, A, [])             # archived in Gmail
        self.assertEqual(self.new_reply(), [False])


if __name__ == "__main__":
    unittest.main()
