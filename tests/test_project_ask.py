"""Projects step 5: Ask on a project page (BUILD_SPEC §6, 2026-09-30).

Bart's interview answers: fenced to the project — its emails in any account,
its todos and their emails, notes, meetings (with Granola notes) and people,
nothing else — and read + draft only. These tests pin the fence in code, not
in the prompt: the context carries only the project, every tool refuses a
thread or meeting that isn't filed there, and the tool list holds no write
beyond Drafts. Scratch SQLite and a fake Anthropic client; nothing live.

    .venv/bin/python -m unittest tests.test_project_ask -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import projects as P  # noqa: E402
import project_ask as PA  # noqa: E402

A, B = "acctone", "accttwo"


class ProjectAsk(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        self.conn = db.connect(self.path)
        for acct, tid, subj in ((A, "555", "Flight dates"), (B, "555", "Mailer proof"),
                                (A, "556", "Not in the project"), (B, "557", "Inside the todo")):
            self.conn.execute("INSERT INTO mail_threads (thread_id, account, subject, last_from, last_date, "
                              "in_inbox, category) VALUES (?, ?, ?, 'x@y.com', '2026-09-30T10:00:00Z', 1, 'people')",
                              (tid, acct, subj))
        self.conn.commit()
        self.pid = P.create(self.conn, "Fall Launch")
        self.other = P.create(self.conn, "Other")
        P.add_item(self.conn, self.pid, "thread", A, "555")
        P.add_item(self.conn, self.pid, "thread", B, "555")
        todo = db.add_engagement(self.conn, "Approve the proof", account=B)
        db.link_thread(self.conn, todo, B, "557")
        P.add_item(self.conn, self.pid, "todo", None, str(todo))
        self.meeting = P.add_item(self.conn, self.pid, "event", A, "2026-10-01|09:00|Kickoff")["id"]
        other_meeting = P.add_item(self.conn, self.other, "event", A, "2026-10-02|09:00|Elsewhere")["id"]
        self.other_meeting = other_meeting
        P.note_add(self.conn, self.pid, "Pitch", "Why fall matters")
        P.note_add(self.conn, self.other, "Secret", "not for this project")
        self.ctx = PA.context(self.conn, self.pid)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_the_context_is_the_project_and_nothing_else(self):
        text = json.dumps(self.ctx)
        self.assertIn("Flight dates", text)
        self.assertIn("Mailer proof", text)
        self.assertIn("Inside the todo", text)              # a todo's emails come with it
        self.assertIn("Why fall matters", text)
        self.assertNotIn("Not in the project", text)
        self.assertNotIn("not for this project", text)
        self.assertEqual(PA.allowed_threads(self.ctx), {(A, "555"), (B, "555"), (B, "557")})

    def test_tools_refuse_anything_outside_the_project(self):
        env = {}
        with mock.patch("mail_actions.fetch_thread", return_value=[{"body": "hi"}]) as ft:
            self.assertEqual(PA.run_tool("read_thread", {"account": B, "thread_id": "557"}, env, self.ctx, self.pid),
                             {"messages": [{"body": "hi"}]})
            ft.assert_called_once()
            for acct, tid in ((A, "556"), (A, "557"), ("", "555")):     # outside; wrong account; none
                with self.assertRaises(ValueError):
                    PA.run_tool("read_thread", {"account": acct, "thread_id": tid}, env, self.ctx, self.pid)
        with self.assertRaises(ValueError):
            PA.run_tool("draft_save", {"account": A, "thread_id": "556", "body": "x"}, env, self.ctx, self.pid)
        with self.assertRaises(ValueError):
            PA.run_tool("meeting_notes", {"item_id": self.other_meeting}, env, self.ctx, self.pid)
        for name in ("mail_action", "add_task", "search_mail", "send"):
            with self.assertRaises(ValueError):
                PA.run_tool(name, {}, env, self.ctx, self.pid)

    def test_read_and_draft_only(self):
        self.assertEqual({t["name"] for t in PA.TOOLS},
                         {"read_thread", "meeting_notes", "draft_preview", "draft_save"})

    def test_a_meeting_without_granola_notes_says_so(self):
        out = PA.run_tool("meeting_notes", {"item_id": self.meeting}, {}, self.ctx, self.pid)
        self.assertIsNone(out["notes"])

    def test_run_command_uses_the_project_prompt_context_and_tools(self):
        seen = {}

        class FakeMessages:
            def create(self, **kw):
                seen.update(kw, messages=[dict(m) for m in kw["messages"]])
                return types.SimpleNamespace(stop_reason="end_turn",
                                             content=[types.SimpleNamespace(type="text", text="Two emails.")])

        fake = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=FakeMessages()))
        import command
        real = db.connect
        with mock.patch.dict(sys.modules, {"anthropic": fake}), \
                mock.patch.object(db, "connect", lambda *a, **k: real(self.path)):
            out = command.run_command("what's open?", selection={"subject": "Not in the project"},
                                      env={"ANTHROPIC_API_KEY": "x"}, project_id=self.pid)
        self.assertEqual(out["reply"], "Two emails.")
        self.assertEqual(seen["system"], PA.SYSTEM_PROMPT)
        self.assertEqual({t["name"] for t in seen["tools"]}, {t["name"] for t in PA.TOOLS})
        prompt = seen["messages"][-1]["content"]
        self.assertIn("Flight dates", prompt)
        self.assertNotIn("Board context", prompt)
        self.assertNotIn("Not in the project", prompt)        # the open email stays outside the fence

    def test_chats_are_kept_with_their_project(self):
        c1 = db.ask_start_chat(self.conn, "status?", self.pid)
        db.ask_start_chat(self.conn, "anything else")
        self.assertEqual([c["id"] for c in db.ask_chats(self.conn, project_id=self.pid)], [c1])
        self.assertEqual([c["id"] for c in P.project_view(self.conn, self.pid)["chats"]], [c1])
        self.assertEqual(len(db.ask_chats(self.conn)), 2)
        P.delete(self.conn, self.pid)                  # the chat outlives its project, unfenced
        self.assertEqual([c["project_id"] for c in db.ask_chats(self.conn)], [None, None])


if __name__ == "__main__":
    unittest.main()
