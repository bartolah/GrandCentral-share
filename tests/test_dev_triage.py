"""Dev queue triage (2026-09-28, Bart: "Do it").

A triage pass labels each open request — priority, size, verdict — before the
unattended worker runs, and the worker builds only clear, small-or-medium
cards, highest priority first. Scratch SQLite and canned model output only.

    .venv/bin/python -m unittest tests.test_dev_triage -v
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
import dev_triage  # noqa: E402

A = "acctone"


def res(rid, verdict="build", priority="p2", size="s", dup_of=None, question=None, note="why"):
    return {"id": rid, "priority": priority, "size": size, "verdict": verdict,
            "dup_of": dup_of, "question": question, "note": note}


class Triage(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        self.a = db.dev_add(self.conn, "Tooltips on the rail", A)
        self.b = db.dev_add(self.conn, "Inbox button should reset", A)
        self.c = db.dev_add(self.conn, "Send from the Ask bar", A)
        self.log = []

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def cards(self):
        return dev_triage._open_cards(self.conn)

    def row(self, rid):
        return dict(self.conn.execute("SELECT * FROM dev_requests WHERE id=?", (rid,)).fetchone())

    def test_labels_are_written_and_text_is_untouched(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a, priority="p3")], False, self.log.append)
        r = self.row(self.a)
        self.assertEqual((r["priority"], r["size"], r["verdict"], r["triage_by"]), ("p3", "s", "build", "claude"))
        self.assertEqual(r["text"], "Tooltips on the rail")
        self.assertEqual(r["status"], "open")

    def test_worker_builds_only_clear_small_or_medium_cards_by_priority(self):
        dev_triage.apply(self.conn, self.cards(), [
            res(self.a, priority="p3", size="s"),
            res(self.b, priority="p1", size="m"),
            res(self.c, verdict="session", priority="p1", size="l"),
        ], False, self.log.append)
        self.assertEqual([c["id"] for c in dev_triage.build_queue(self.conn)], [self.b, self.a])

    def test_large_build_waits_for_a_session(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a, size="l")], False, self.log.append)
        self.assertEqual(dev_triage.build_queue(self.conn), [])

    def test_untriaged_and_edited_cards_are_not_built(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a)], False, self.log.append)
        self.assertEqual(len(dev_triage.build_queue(self.conn)), 1)
        db.dev_update(self.conn, self.a, A, text="Tooltips on the rail, and bigger icons")
        self.assertEqual(dev_triage.build_queue(self.conn), [])        # label is stale
        self.assertTrue(dev_triage.needs_triage(
            next(c for c in self.cards() if c["id"] == self.a)))

    def test_a_duplicate_of_nothing_becomes_a_session_card(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a, verdict="duplicate", dup_of=None)],
                         False, self.log.append)
        self.assertEqual(self.row(self.a)["verdict"], "session")

    def test_ask_posts_the_question(self):
        with mock.patch.object(dev_triage, "_ask") as ask:
            dev_triage.apply(self.conn, self.cards(),
                             [res(self.a, verdict="ask", question="Slim rail, wide, or both?")],
                             False, self.log.append)
        ask.assert_called_once()
        self.assertEqual(ask.call_args[0][:2], (self.a, "Slim rail, wide, or both?"))

    def test_unknown_ids_from_the_model_are_ignored(self):
        dev_triage.apply(self.conn, self.cards(), [res(9999)], False, self.log.append)
        self.assertEqual(self.log, [])

    def test_dry_run_writes_nothing(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a)], True, self.log.append)
        self.assertIsNone(self.row(self.a)["verdict"])

    def test_bad_labels_are_refused(self):
        with self.assertRaises(ValueError):
            db.dev_set_triage(self.conn, self.a, priority="urgent")

    def test_queue_text_carries_the_markers_the_worker_expects(self):
        dev_triage.apply(self.conn, self.cards(), [res(self.a, priority="p1")], False, self.log.append)
        t = dev_triage.queue_text(dev_triage.build_queue(self.conn))
        self.assertIn(f"--- REQUEST {self.a} (acctone, p1, size s) ---", t)
        self.assertIn(f"--- END REQUEST {self.a} ---", t)


if __name__ == "__main__":
    unittest.main()
