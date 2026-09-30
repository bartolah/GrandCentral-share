"""E249 — the ◫ Day Home and the read-only ⌂ Home tab across accounts.

BUILD_SPEC §6 (2026-09-29) allows a Home that shows every account at once on
the condition that it is a view, never an owner. These tests pin that:
each section is built from its own account only (the same thread id in two
accounts stays two rows), building Home writes nothing, the caps Bart asked
for hold ("no more than 5"), the day note for one account never sees
another account's mail, and agents cannot reach the new routes.

Offline and isolated like the rest of tests/: scratch SQLite files, a fake
model client, fake accounts (acctone/accttwo), a scratch server that is
never asked to write a note (that would call a model).

    .venv/bin/python -m unittest tests.test_home -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
sys.path.insert(0, os.path.join(ROOT, "integrations", "mcp"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import day_note  # noqa: E402
import db  # noqa: E402
import home_view  # noqa: E402
from test_account_isolation import _free_port, _seed_mail  # noqa: E402

A, B = "acctone", "accttwo"
TID = "1800000000000000001"          # the SAME Gmail thread id in both accounts
TODAY = datetime.now().astimezone().strftime("%Y-%m-%d")
YESTERDAY = (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")


def _classify(conn, account, thread_id, respond="yes", priority=5,
              relationship="frequent", summary=None, **extra):
    fields = {"type": "Correspondence", "relationship": relationship,
              "respond": respond, "priority": priority, "waiting_on": "none",
              "follow_up": None, **extra}
    db.class_write_rules(conn, account, thread_id, fields, "fp", "rules-3", "r", "2026-09-29T00:00:00Z")
    conn.commit()
    if summary:
        db.class_merge_model(conn, {(account, thread_id): {
            "summary": summary, "reply_needed": respond == "yes", "importance": priority}}, "haiku")


class HomeCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "h.db"))
        _seed_mail(self.conn, A, TID, "RFP in A")
        _seed_mail(self.conn, B, TID, "School form in B")
        _classify(self.conn, A, TID, summary="Dana wants a call about the RFP")
        _classify(self.conn, B, TID, summary="Field trip form due")

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()


class SectionsStayInTheirAccount(HomeCase):
    def test_same_thread_id_in_two_accounts_is_two_sections(self):
        out = home_view.home(self.conn, [A, B], TODAY)
        by = {s["account"]: s for s in out["sections"]}
        self.assertEqual([r["subject"] for r in by[A]["top"]], ["RFP in A"])
        self.assertEqual([r["subject"] for r in by[B]["top"]], ["School form in B"])
        for s in out["sections"]:
            for r in s["top"] + s["follows"]:
                self.assertEqual(r["account"], s["account"])

    def test_one_account_section_never_reads_another(self):
        s = home_view.section(self.conn, A, TODAY)
        self.assertEqual(s["account"], A)
        self.assertNotIn("School form in B", json.dumps(s))

    def test_building_home_writes_nothing(self):
        tid = db.add_engagement(self.conn, "due today", account=A, due=TODAY)
        self.assertTrue(tid)
        before = self.conn.total_changes
        home_view.home(self.conn, [A, B], TODAY)
        day_note.gather(self.conn, "", [A, B], board={}, today=TODAY)
        self.assertEqual(self.conn.total_changes, before)

    def test_bad_account_refused(self):
        with self.assertRaises(ValueError):
            home_view.section(self.conn, "../x", TODAY)


class WhatASectionHolds(HomeCase):
    def test_top_is_capped_at_five_scored_and_ordered(self):
        for n in range(8):
            t = f"19000000000000000{n:02d}"
            _seed_mail(self.conn, A, t, f"mail {n}")
            _classify(self.conn, A, t, respond="no", priority=1 + n % 5)
        s = home_view.section(self.conn, A, TODAY)
        self.assertLessEqual(len(s["top"]), 5)
        vals = [r["value"] for r in s["top"]]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertTrue(all(v >= home_view.TOP_MIN_VALUE for v in vals))

    def test_follow_ups_are_capped_and_never_repeat_a_top_row(self):
        for n in range(9):
            t = f"19100000000000000{n:02d}"
            _seed_mail(self.conn, A, t, f"owed {n}")
            _classify(self.conn, A, t, respond="yes", priority=1, relationship="new")
        s = home_view.section(self.conn, A, TODAY)
        self.assertLessEqual(len(s["follows"]), 5)
        top_ids = {r["thread_id"] for r in s["top"]}
        self.assertFalse(top_ids & {r["thread_id"] for r in s["follows"]})
        self.assertTrue(all(r["state"].startswith("Reply owed") for r in s["follows"]))

    def test_todos_due_and_overdue_are_per_account(self):
        db.add_engagement(self.conn, "A today", account=A, due=TODAY)
        db.add_engagement(self.conn, "A late", account=A, due=YESTERDAY)
        db.add_engagement(self.conn, "B today", account=B, due=TODAY)
        a = home_view.section(self.conn, A, TODAY)
        self.assertEqual([t["text"] for t in a["due"]], ["A today"])
        self.assertEqual((a["counts"]["todos_due"], a["counts"]["overdue"]), (1, 1))
        self.assertEqual(a["overdue_oldest"], YESTERDAY)
        b = home_view.section(self.conn, B, TODAY)
        self.assertEqual([t["text"] for t in b["due"]], ["B today"])
        self.assertEqual(b["counts"]["overdue"], 0)

    def test_archived_mail_is_not_counted(self):
        _seed_mail(self.conn, A, "1800000000000000999", "archived", in_inbox=0)
        self.assertEqual(home_view.section(self.conn, A, TODAY)["counts"]["emails"], 1)


BOARD = {"calendar": [{"date": TODAY, "events": [
    {"time": "16:30", "end": "17:15", "title": "Retreat planning", "cal": "work"},
    {"time": "16:45", "end": "17:15", "title": "School pickup", "cal": "personal"},
    {"time": "09:00", "end": "", "title": "Standup", "cal": "work"},
    {"time": "all-day", "end": "", "title": "Offsite", "cal": "crowdblue"},
]}]}


class DayNote(HomeCase):
    def test_clashes_are_found_in_code(self):
        evs = day_note.events_today(BOARD, TODAY)
        cl = day_note.clashes(evs)
        self.assertEqual([(c["a"], c["b"]) for c in cl], [("Retreat planning", "School pickup")])
        self.assertEqual((cl[0]["a_account"], cl[0]["b_account"]), ("connectblue", "personal"))

    def test_an_event_with_no_end_counts_as_thirty_minutes(self):
        evs = [{"time": "09:00", "end": "", "title": "a", "account": None},
               {"time": "09:20", "end": "", "title": "b", "account": None},
               {"time": "09:30", "end": "", "title": "c", "account": None}]
        self.assertEqual([(c["a"], c["b"]) for c in day_note.clashes(evs)], [("a", "b"), ("b", "c")])

    def test_account_scope_holds_only_that_account(self):
        sig = day_note.gather(self.conn, A, [A, B], board=BOARD, today=TODAY)
        text = json.dumps(sig)
        self.assertEqual([p["account"] for p in sig["accounts"]], [A])
        self.assertIn("Dana wants a call", text)
        self.assertNotIn("Field trip", text)
        self.assertNotIn("School form in B", text)
        # A maps to no calendar here, so its scope carries no events at all
        self.assertEqual(sig["calendar"], [])

    def test_cross_account_scope_labels_every_item_and_sends_no_snippets(self):
        sig = day_note.gather(self.conn, "", [A, B], board=BOARD, today=TODAY)
        self.assertEqual([p["account"] for p in sig["accounts"]], [A, B])
        self.assertEqual(len(sig["calendar"]), 4)
        self.assertNotIn("snip", json.dumps(sig))      # the seeded snippet never ships

    def test_generate_stores_one_note_per_scope_with_a_fake_model(self):
        seen = []

        class Fake:
            class messages:
                @staticmethod
                def create(**kw):
                    seen.append(kw["messages"][0]["content"])
                    return SimpleNamespace(content=[SimpleNamespace(type="text", text="  Call Dana first.\n")])
        with mock.patch.object(day_note, "_board", return_value=BOARD):
            a = day_note.generate(self.conn, A, [A, B], client=Fake, log=lambda *_: None)
            both = day_note.generate(self.conn, "", [A, B], client=Fake, log=lambda *_: None)
        self.assertEqual(a["text"], "Call Dana first.")
        self.assertEqual(day_note.get(self.conn, A)["text"], "Call Dana first.")
        self.assertEqual(day_note.get(self.conn, "")["text"], both["text"])
        self.assertIsNone(day_note.get(self.conn, B))
        self.assertNotIn("Field trip", seen[0])            # A's prompt never saw B
        self.assertIn("Field trip", seen[1])               # the Home-tab prompt reads both

    def test_a_failed_call_stores_nothing(self):
        class Boom:
            class messages:
                @staticmethod
                def create(**kw):
                    raise RuntimeError("no network")
        self.assertIsNone(day_note.generate(self.conn, A, [A], client=Boom, log=lambda *_: None))
        self.assertIsNone(day_note.get(self.conn, A))


class CalendarEndTimes(unittest.TestCase):
    def test_events_carry_a_same_day_end_time(self):
        from sources import calendar_reader
        day = datetime.now().astimezone().date()
        d = day.strftime("%Y%m%d")
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:ConnectBlue\r\n"
               f"BEGIN:VEVENT\r\nUID:1\r\nSUMMARY:Standup\r\nDTSTART:{d}T090000\r\nDTEND:{d}T093000\r\nEND:VEVENT\r\n"
               f"BEGIN:VEVENT\r\nUID:2\r\nSUMMARY:Red-eye\r\nDTSTART:{d}T230000\r\n"
               f"DTEND:{(day + timedelta(days=1)).strftime('%Y%m%d')}T060000\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n").encode()
        resp = SimpleNamespace(content=ics, raise_for_status=lambda: None)
        with mock.patch.object(calendar_reader.requests, "get", return_value=resp):
            out = calendar_reader.read_range({"GCAL_ICS_URLS": "https://x/cal.ics"}, day, 1, log=lambda *_: None)
        evs = {e["title"]: e for e in out[0]["events"]}
        self.assertEqual((evs["Standup"]["time"], evs["Standup"]["end"]), ("09:00", "09:30"))
        self.assertEqual(evs["Standup"]["cal"], "work")
        self.assertEqual(evs["Red-eye"]["end"], "")           # ends tomorrow: no same-day end


class HomeRoutes(unittest.TestCase):
    """A scratch dashboard on a scratch port with its own scratch DB: A and B
    each hold one email under the same thread id."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        import time
        cls._dir = tempfile.TemporaryDirectory()
        path = os.path.join(cls._dir.name, "http.db")
        conn = db.connect(path)
        _seed_mail(conn, A, TID, "mail in A")
        _seed_mail(conn, B, TID, "mail in B")
        conn.close()
        cls.port = _free_port()
        env = dict(os.environ, GC_PORT=str(cls.port), GC_DB_PATH=path)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "serve.py")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.get("/version")
                return
            except Exception:
                time.sleep(0.1)
        cls.proc.kill()
        raise RuntimeError("scratch dashboard did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls._dir.cleanup()

    @classmethod
    def get(cls, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cls.port}{path}", timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_home_route_builds_each_account_on_its_own(self):
        code, out = self.get("/home")
        self.assertEqual(code, 200, out)
        subjects = {s["account"]: s["counts"]["emails"] for s in out["sections"]}
        self.assertEqual(subjects, {A: 1, B: 1})
        code, out = self.get(f"/home?account={A}")
        self.assertEqual([s["account"] for s in out["sections"]], [A])

    def test_unknown_accounts_are_refused(self):
        self.assertEqual(self.get("/home?account=nosuchacct")[0], 400)
        # refused before any note is written, so no model is ever called here
        self.assertEqual(self.get("/home/note?account=nosuchacct")[0], 400)


class AgentsCannotReachHome(unittest.TestCase):
    def test_mcp_client_refuses_the_home_routes(self):
        import gc_client
        for path in ("/home", "/home/note"):
            with self.assertRaises(gc_client.GCError):
                gc_client._check("GET", path, None)


if __name__ == "__main__":
    unittest.main()
