"""Projects, step 1: the data layer (spec: ACCOUNT_FIRST.md "Projects — spec";
BUILD_SPEC §6, 2026-09-29).

A project belongs to no account and only points at things, each keyed by the
account it lives in. These tests pin the rules Bart set in the interview:
up to 3 pins, completing never touches todos, deleting keeps the content and
moves notes to Notes, a todo's emails show once — and the account boundary
itself stays exactly as it was. Scratch SQLite only.

    .venv/bin/python -m unittest tests.test_projects -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import projects as P  # noqa: E402

A, B = "acctone", "accttwo"


class Projects(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        now = db._now_iso()
        # the same thread id in two accounts — a real Gmail property
        for acct, subj in ((A, "Flight dates"), (B, "Mailer proof")):
            self.conn.execute(
                "INSERT INTO mail_threads (thread_id, account, subject, last_from, last_date, "
                "in_inbox, category) VALUES ('555', ?, ?, 'x@y.com', ?, 1, 'people')", (acct, subj, now))
        self.conn.commit()
        self.todo_a = db.add_engagement(self.conn, "Approve the proof", account=A)
        self.todo_b = db.add_engagement(self.conn, "Book flights", account=B)
        self.pid = P.create(self.conn, "Fall Launch", target_date="2026-10-20")

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_a_project_holds_items_from_several_accounts_each_keyed_by_its_own(self):
        P.add_item(self.conn, self.pid, "thread", A, "555")
        P.add_item(self.conn, self.pid, "thread", B, "555")
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_b))
        v = P.project_view(self.conn, self.pid)
        threads = {(i["account"], i["mail"]["subject"]) for i in v["items"] if i["kind"] == "thread"}
        self.assertEqual(threads, {(A, "Flight dates"), (B, "Mailer proof")})
        self.assertEqual(v["accounts"], [A, B])

    def test_a_todo_s_account_comes_from_the_todo_not_the_caller(self):
        it = P.add_item(self.conn, self.pid, "todo", None, str(self.todo_a))
        self.assertEqual(it["account"], A)
        with self.assertRaises(ValueError):
            P.add_item(self.conn, self.pid, "todo", B, str(self.todo_a))

    def test_adding_is_idempotent_and_validated(self):
        self.assertTrue(P.add_item(self.conn, self.pid, "thread", A, "555")["added"])
        self.assertFalse(P.add_item(self.conn, self.pid, "thread", A, "555")["added"])
        for kind, acct, ref in (("thread", None, "555"), ("thread", A, "abc"), ("todo", None, "999"),
                                ("contact", None, "not-an-email"), ("deal", A, "1"), ("event", A, "e1"),
                                ("event", A, "2026-10-01|9am|Kickoff")):
            with self.assertRaises(ValueError, msg=(kind, acct, ref)):
                P.add_item(self.conn, self.pid, kind, acct, ref)

    def test_a_contact_is_shared_not_owned(self):
        it = P.add_item(self.conn, self.pid, "contact", A, "Maya@Example.com")
        self.assertIsNone(it["account"])
        self.assertEqual(it["ref"], "maya@example.com")

    def test_a_todo_s_emails_show_once_folded_in_the_todo(self):
        db.link_thread(self.conn, self.todo_a, A, "555")
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_a))
        P.add_item(self.conn, self.pid, "thread", A, "555")
        P.add_item(self.conn, self.pid, "thread", B, "555")      # same id, other account
        v = P.project_view(self.conn, self.pid)
        folded = {(i["account"], i["in_todo"]) for i in v["items"] if i["kind"] == "thread"}
        self.assertEqual(folded, {(A, True), (B, False)})

    def test_up_to_three_pins_in_order(self):
        ids = [self.pid] + [P.create(self.conn, f"P{i}") for i in range(3)]
        for pid in ids[:3]:
            P.pin(self.conn, pid, True)
        with self.assertRaises(ValueError):
            P.pin(self.conn, ids[3], True)
        P.pin(self.conn, ids[0], False)
        ranks = {p["id"]: p["pin_rank"] for p in P.list_projects(self.conn)}
        self.assertEqual((ranks[ids[1]], ranks[ids[2]], ranks[ids[0]]), (1, 2, None))

    def test_completing_never_touches_todos_but_reports_the_open_ones(self):
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_a))
        self.assertEqual([t["id"] for t in P.open_todos(self.conn, self.pid)], [self.todo_a])
        p = P.complete(self.conn, self.pid)
        self.assertEqual(p["status"], "done")
        self.assertTrue(p["completed_at"])
        state = self.conn.execute("SELECT state FROM engagements WHERE id = ?", (self.todo_a,)).fetchone()[0]
        self.assertEqual(state, "open")
        self.assertIsNone(P.reopen(self.conn, self.pid)["completed_at"])

    def test_deleting_keeps_everything_and_moves_notes_to_notes(self):
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_a))
        P.add_item(self.conn, self.pid, "thread", A, "555")
        nid = P.note_add(self.conn, self.pid, "Pitch outline", "1. Why fall matters")
        out = P.delete(self.conn, self.pid)
        self.assertEqual((out["links_removed"], out["notes_kept"]), (2, 1))
        note = self.conn.execute("SELECT project_id, from_project FROM notes WHERE id = ?", (nid,)).fetchone()
        self.assertEqual((note[0], note[1]), (None, "Fall Launch"))
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM engagements WHERE id = ?", (self.todo_a,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM mail_threads WHERE account = ? AND thread_id = '555'", (A,)).fetchone())

    def test_the_account_boundary_is_unchanged(self):
        # §6 2026-09-29: projects span accounts; todos still don't.
        P.add_item(self.conn, self.pid, "thread", B, "555")
        with self.assertRaises(ValueError):
            db.link_thread(self.conn, self.todo_a, B, "555")
        with self.assertRaises(ValueError):
            db.add_engagement(self.conn, "no account", account=None)

    def test_a_contact_is_added_once_even_with_no_account(self):
        self.assertTrue(P.add_item(self.conn, self.pid, "contact", None, "maya@example.com")["added"])
        self.assertFalse(P.add_item(self.conn, self.pid, "contact", A, "Maya@Example.com")["added"])
        self.assertEqual(P.project_view(self.conn, self.pid)["counts"]["contact"], 1)

    def test_an_event_keeps_its_calendar_s_account_and_finds_its_granola_notes(self):
        from datetime import datetime, timezone
        local = datetime(2026, 10, 1, 9, 5).astimezone()          # 9:05 local, like Granola's start
        self.conn.execute("INSERT INTO elements (source, source_ref, type, title, link, last_activity) "
                          "VALUES ('granola', 'not_1', 'meeting', 'Fall kickoff', 'https://notes.example/1', ?)",
                          (local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),))
        P.add_item(self.conn, self.pid, "event", A, "2026-10-01|09:00|Fall kickoff")
        P.add_item(self.conn, self.pid, "event", None, "2026-10-02|all-day|Offsite")   # a calendar with no account
        ev = {i["ref"]: i for i in P.project_view(self.conn, self.pid)["items"] if i["kind"] == "event"}
        k = ev["2026-10-01|09:00|Fall kickoff"]
        self.assertEqual((k["account"], k["date"], k["time"], k["title"]), (A, "2026-10-01", "09:00", "Fall kickoff"))
        self.assertEqual(k["granola"]["link"], "https://notes.example/1")
        self.assertIsNone(ev["2026-10-02|all-day|Offsite"]["account"])
        self.assertIsNone(ev["2026-10-02|all-day|Offsite"]["granola"])

    def test_people_on_the_project_s_emails_are_suggested_never_bart(self):
        self.conn.execute("UPDATE mail_threads SET participants = ?, last_from = ? WHERE account = ?",
                          ('["Maya Lin <maya@example.com>", "maya@example.com", "Bart <bart@acctone.com>"]',
                           "Maya Lin <maya@example.com>", A))
        P.add_item(self.conn, self.pid, "thread", A, "555")
        v = P.project_view(self.conn, self.pid, mine={"bart@acctone.com"})
        self.assertEqual([(x["email"], x["name"], x["threads"]) for x in v["suggest_people"]],
                         [("maya@example.com", "Maya Lin", 1)])
        P.add_item(self.conn, self.pid, "contact", None, "maya@example.com")
        self.assertEqual(P.project_view(self.conn, self.pid, mine={"bart@acctone.com"})["suggest_people"], [])

    def test_a_note_moves_into_a_project_and_back_out_to_notes(self):
        nid = P.note_add(self.conn, None, "Loose idea", "call Maya")
        self.assertIsNone(P.notes_list(self.conn)[0]["project_name"])
        P.note_move(self.conn, nid, self.pid)
        self.assertEqual(P.notes_list(self.conn, "maya")[0]["project_name"], "Fall Launch")
        self.assertEqual(P.note_move(self.conn, nid, None)["body"], "call Maya")
        self.assertEqual(P.notes_list(self.conn, "nothing-matches"), [])
        with self.assertRaises(ValueError):
            P.note_move(self.conn, nid, 999)

    def test_progress_counts_done_todos(self):
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_a))
        P.add_item(self.conn, self.pid, "todo", None, str(self.todo_b))
        self.conn.execute("UPDATE engagements SET state = 'done' WHERE id = ?", (self.todo_b,))
        c = P.list_projects(self.conn)[0]["counts"]
        self.assertEqual((c["todos_done"], c["todos_total"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
