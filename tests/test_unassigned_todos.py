"""Legacy todos with no account (2026-09-26).

Bart: "Where did the old todos go? How do I bring them back in each of the
right accounts with their mapped emails?" Evidence-backed ones are placed by
sweeper/backfill_todo_accounts.py; the rest are listed with hints from
tools/suggest_todo_accounts.py and moved by Bart. Scratch SQLite only.

    .venv/bin/python -m unittest tests.test_unassigned_todos -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db  # noqa: E402
import suggest_todo_accounts as sta  # noqa: E402

A, B = "acctone", "accttwo"


class Unassigned(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        c = self.conn
        with c:
            # legacy rows: written straight to the table, as pre-account-first code did
            c.execute("INSERT INTO theaters (id, name) VALUES ('t1', 'Acctone')")
            c.execute("INSERT INTO operations (id, name) VALUES ('o1', 'Launch')")
            c.execute("INSERT INTO operation_theaters (operation_id, theater_id) VALUES ('o1', 't1')")
            now = "2026-08-01T00:00:00Z"
            for i, (text, op) in enumerate((("Ship the deck", "o1"), ("Call the printer about accttwo", None),
                                            ("Buy milk", None)), 1):
                c.execute("INSERT INTO engagements (id, text, operation_id, created_at) "
                          "VALUES (?,?,?,?)", (i, text, op, now))
            c.execute("INSERT INTO engagements (id, text, account, created_at) "
                      "VALUES (4, 'Already placed', 'acctone', ?)", (now,))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_hints_come_from_the_old_project_or_the_text(self):
        hints = {tid: (acct, why) for tid, acct, why in sta.plan(self.conn, [A, B])}
        self.assertEqual(hints[1], (A, "was in the Acctone project"))
        self.assertEqual(hints[2][0], B)
        self.assertNotIn(3, hints)                   # no evidence, no guess

    def test_the_list_shows_only_todos_without_an_account_with_their_hint(self):
        db.todo_hints_replace(self.conn, sta.plan(self.conn, [A, B]))
        rows = db.todo_unassigned(self.conn)
        self.assertEqual({r["id"] for r in rows}, {1, 2, 3})
        by = {r["id"]: r for r in rows}
        self.assertEqual((by[1]["hint"], by[3]["hint"]), (A, None))

    def test_moving_one_takes_it_out_of_the_list(self):
        db.update_engagement(self.conn, 3, {"account": B})
        self.assertEqual({r["id"] for r in db.todo_unassigned(self.conn)}, {1, 2})

    def test_runtime_code_never_reads_the_old_project_tables(self):
        # the hint tool lives in tools/, outside the runtime guard, on purpose
        self.assertTrue(os.path.exists(os.path.join(ROOT, "tools", "suggest_todo_accounts.py")))
        with open(os.path.join(ROOT, "sweeper", "db.py"), encoding="utf-8") as f:
            self.assertNotIn("JOIN theaters", f.read())


if __name__ == "__main__":
    unittest.main()
