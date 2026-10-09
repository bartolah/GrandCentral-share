"""The Dev queue's question loop (2026-09-26).

Casey: "set up a scheduled job to process triage and feedback requests daily
and to ask me questions if anything is unclear, in the Dev dashboard queue."

The worker marks an unclear request `question` with its question as the note;
Casey is notified once; the worker skips it; his (paired) answer is appended to
the request text and puts it back in the queue. Scratch SQLite only.

    .venv/bin/python -m unittest tests.test_dev_queue -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import dev_answer  # noqa: E402
import notify  # noqa: E402

A = "acctone"


class QuestionLoop(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        self.rid = db.dev_add(self.conn, "Make the Home feed denser", A)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def status(self):
        return self.conn.execute("SELECT status, text, note FROM dev_requests WHERE id=?",
                                 (self.rid,)).fetchone()

    def test_a_question_waits_on_casey_and_his_answer_requeues_it(self):
        db.dev_update(self.conn, self.rid, A, status="question",
                      note="Denser on phone, desktop, or both?")
        self.assertEqual(self.status()["status"], "question")
        # questions sort first, so they're the first thing Casey sees
        db.dev_add(self.conn, "another", A)
        self.assertEqual(db.dev_list(self.conn, A)[0]["id"], self.rid)
        db.dev_answer_question(self.conn, self.rid, A, "Both, phone first.")
        row = self.status()
        self.assertEqual(row["status"], "open")
        self.assertIn("— Claude asked: Denser on phone, desktop, or both?", row["text"])
        self.assertIn("— Casey answered", row["text"])
        self.assertIn("Both, phone first.", row["text"])

    def test_only_a_question_can_be_answered(self):
        with self.assertRaises(ValueError):
            db.dev_answer_question(self.conn, self.rid, A, "hello")         # still open
        db.dev_update(self.conn, self.rid, A, status="question", note="?")
        with self.assertRaises(ValueError):
            db.dev_answer_question(self.conn, self.rid, A, "   ")           # empty
        with self.assertRaises(ValueError):
            db.dev_answer_question(self.conn, self.rid, "accttwo", "x")     # other account

    def test_the_worker_can_ask(self):
        self.assertIn("question", dev_answer.STATUSES)
        with open(os.path.join(ROOT, "sweeper", "dev_worker.sh"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("dev_answer.py ID question", src)
        # questions aren't re-picked: since triage (2026-09-28) the worker
        # builds only what dev_triage.py --queue hands it, which reads open
        # cards alone
        self.assertIn("dev_triage.py --queue", src)
        import dev_triage
        db.dev_update(self.conn, self.rid, A, status="question", note="Which screen?")
        self.assertEqual(dev_triage._open_cards(self.conn), [])

    def test_casey_is_told_once_per_question(self):
        db.dev_update(self.conn, self.rid, A, status="question", note="Which screen?")
        sent = []
        fixed = datetime(2026, 9, 26, 15, 30).astimezone()
        with mock.patch.object(notify, "_deliver", side_effect=lambda *a, **k: sent.append((a, k))), \
             mock.patch.object(notify, "_today_events", return_value=[]), \
             mock.patch.object(notify, "datetime", wraps=datetime) as dt:
            dt.now.return_value = fixed
            notify.run(self.conn, {})
            notify.run(self.conn, {})
        qs = [s for s in sent if s[0][1] == "dev"]
        self.assertEqual(len(qs), 1)
        self.assertIn("Which screen?", qs[0][0][3])
        self.assertIn("go=dev", qs[0][1]["url"])


class MoveToGroup(unittest.TestCase):
    def test_a_teammate_moves_into_a_group_and_stays_a_teammate(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "t.db"))
            db.class_person_set(conn, "sam@team.com", "Sam")                       # no group
            db.class_person_set(conn, "sam@team.com", "", grp="CB group", teammate=True)
            (p,) = db.class_roster(conn)["people"]
            self.assertEqual((p["name"], p["grp"], p["teammate"]), ("Sam", "CB group", True))
            conn.close()


if __name__ == "__main__":
    unittest.main()
