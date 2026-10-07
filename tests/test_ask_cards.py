"""E265 — Ask proposes, Bart clicks (BUILD_SPEC §6, 2026-10-02).

Bart: the ✨ Ask panel should add calendar events (and invite drafts), draft
new emails and triage threads — "Show a card, I click". These tests pin that
the three propose_ tools never act: they only build a card with the exact
request for the board's own route, validated first, and run_command returns
cards apart from actions_taken. Scratch SQLite, fake Anthropic client.

    .venv/bin/python -m unittest tests.test_ask_cards -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import command  # noqa: E402

A, B = "acctone", "accttwo"


class AskCards(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        conn = db.connect(self.path)
        for acct, tid, subj in ((A, "555", "Flight dates"), (B, "556", "Mailer proof")):
            conn.execute("INSERT INTO mail_threads (thread_id, account, subject, last_from, last_date, "
                         "in_inbox, category) VALUES (?, ?, ?, 'x@y.com', '2026-09-30T10:00:00Z', 1, 'people')",
                         (tid, acct, subj))
        conn.commit()
        conn.close()
        real = db.connect
        self._p = mock.patch.object(db, "connect", lambda *a, **k: real(self.path))
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._dir.cleanup()

    def card(self, name, **inp):
        out = command._run_tool(name, inp, env={})
        self.assertIn("nothing happens until he clicks", out["status"])
        return out["card"]

    def test_calendar_card_targets_the_board_route_and_creates_nothing(self):
        with mock.patch("calendar_writer.create", side_effect=AssertionError("acted")):
            c = self.card("propose_calendar_event", account=A, summary="Call Sam",
                          start="2026-10-03T10:00")
        self.assertEqual((c["path"], c["kind"], c["account"]), ("/calendar", "calendar", A))
        self.assertEqual(c["body"], {"account": A, "summary": "Call Sam",
                                     "start": "2026-10-03T10:00", "all_day": False})

    def test_attendees_become_an_invite_draft_proposal(self):
        c = self.card("propose_calendar_event", account=A, summary="Kickoff",
                      start="2026-10-03T10:00", attendees=["Sam <sam@example.com>"])
        self.assertEqual(c["body"]["attendees"], ["sam@example.com"])
        self.assertTrue(any("nobody emailed yet" in l for l in c["lines"]))
        with self.assertRaises(ValueError):
            self.card("propose_calendar_event", account=A, summary="x", start="2026-10-03",
                      attendees=["not-an-email"])

    def test_new_draft_card(self):
        c = self.card("propose_new_draft", account=B, to="jo@example.com",
                      subject="Proof", body="Looks good.")
        self.assertEqual(c["path"], "/draft/compose")
        self.assertEqual(c["body"]["to"], "jo@example.com")
        with self.assertRaises(ValueError):
            self.card("propose_new_draft", account=B, to="nobody", subject="x", body="y")

    def test_triage_card_needs_the_thread_in_that_account(self):
        c = self.card("propose_triage", account=A, thread_id="555", action="pin")
        self.assertEqual((c["path"], c["button"]), ("/mail/triage", "Pin"))
        self.assertIn("Flight dates", c["lines"][0])
        with self.assertRaises(ValueError):          # 556 lives in B, not A
            self.card("propose_triage", account=A, thread_id="556", action="pin")
        with self.assertRaises(ValueError):          # no trash, no move
            self.card("propose_triage", account=A, thread_id="555", action="trash")
        with self.assertRaises(ValueError):
            self.card("propose_triage", account=A, thread_id="555", action="snooze")

    def test_unknown_account_refused(self):
        with self.assertRaises(ValueError):
            self.card("propose_new_draft", account="nowhere", to="a@b.co", subject="", body="")

    def test_run_command_returns_cards_not_actions(self):
        calls = []

        class FakeMessages:
            def create(self, **kw):
                calls.append(kw)
                if len(calls) == 1:
                    return types.SimpleNamespace(stop_reason="tool_use", content=[types.SimpleNamespace(
                        type="tool_use", id="t1", name="propose_calendar_event",
                        input={"account": A, "summary": "Dentist", "start": "2026-10-03T09:00"})])
                return types.SimpleNamespace(stop_reason="end_turn", content=[
                    types.SimpleNamespace(type="text", text="It's waiting on your click.")])

        fake = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=FakeMessages()))
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            out = command.run_command("put the dentist on my calendar", env={"ANTHROPIC_API_KEY": "x"})
        self.assertEqual(out["actions_taken"], [])
        self.assertEqual([c["body"]["summary"] for c in out["cards"]], ["Dentist"])
        self.assertIn("propose_calendar_event", {t["name"] for t in calls[0]["tools"]})

    def test_no_send_or_trash_tool_appeared(self):
        names = {t["name"] for t in command.TOOLS}
        self.assertFalse({n for n in names if "send" in n or "trash" in n or "bulk" in n})


if __name__ == "__main__":
    unittest.main()
