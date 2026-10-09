"""Drafts can be scheduled (Casey 2026-10-06: "I also need a way to schedule a
draft email to send it"). A scheduled row may be a NEW email (thread_id ''),
keeps its HTML, and once sent its Gmail draft goes to Trash. Replies still go
through send_reply. Scratch SQLite; send_mail and mail_actions are faked —
nothing is sent.

    .venv/bin/python -m unittest tests.test_draft_schedule -v
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
import mail_sync  # noqa: E402

A = "acctone"


class Schedule(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_new_and_reply_drafts_fire_and_their_drafts_are_trashed(self):
        past = "2020-01-01T00:00:00Z"
        db.add_outbox(self.conn, "", A, "pat@client.com", "Hi Pat", past, subject="Proposal",
                      body_html="<p>Hi <b>Pat</b></p>", draft_uid="41", draft_msgid="999")
        db.add_outbox(self.conn, "123", A, "sam@client.com", "Re", past, subject="Re: x")
        calls = []
        import send_mail, mail_actions
        with mock.patch.object(send_mail, "send_new", lambda *a, **k: calls.append(("new", a, k))), \
                mock.patch.object(send_mail, "send_reply", lambda *a, **k: calls.append(("reply", a, k))), \
                mock.patch.object(mail_actions, "trash_draft", lambda *a, **k: calls.append(("trash", a, k))):
            n = mail_sync.send_due_outbox(self.conn, {}, log=lambda *a: None)
        self.assertEqual(n, 2)
        kinds = [c[0] for c in calls]
        self.assertEqual(sorted(kinds), ["new", "reply", "trash"])
        new = next(c for c in calls if c[0] == "new")
        self.assertEqual((new[1][0], new[1][2], new[1][3], new[2]["body_html"]),
                         (A, "pat@client.com", "Proposal", "<p>Hi <b>Pat</b></p>"))
        self.assertEqual(next(c for c in calls if c[0] == "trash")[1][:3], (A, "41", "999"))
        self.assertEqual({r["status"] for r in self.conn.execute("SELECT status FROM mail_outbox")}, {"sent"})

    def test_a_failed_send_keeps_the_draft(self):
        db.add_outbox(self.conn, "", A, "pat@client.com", "Hi", "2020-01-01T00:00:00Z", subject="S", draft_uid="41")
        trashed = []
        import send_mail, mail_actions

        def boom(*a, **k):
            raise RuntimeError("smtp down")
        with mock.patch.object(send_mail, "send_new", boom), \
                mock.patch.object(mail_actions, "trash_draft", lambda *a, **k: trashed.append(a)):
            mail_sync.send_due_outbox(self.conn, {}, log=lambda *a: None)
        self.assertEqual(trashed, [])
        self.assertEqual(self.conn.execute("SELECT status FROM mail_outbox").fetchone()[0], "failed")


if __name__ == "__main__":
    unittest.main()
