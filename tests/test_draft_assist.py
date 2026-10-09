"""The composer's ✨ (Casey 2026-10-06): draft or rewrite from his prompt, and
read his history with the recipients only when he ticks the box. Fake model,
fake mail; nothing is sent or saved anywhere.

    .venv/bin/python -m unittest tests.test_draft_assist -v
"""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import draft_reply  # noqa: E402

ENV = {"ANTHROPIC_API_KEY": "x"}


class Assist(unittest.TestCase):
    def run_assist(self, **kw):
        seen = {}

        class Msgs:
            def create(self, **k):
                seen.update(k)
                return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text="Hi Pat — yes, Tuesday works.")])
        fake = types.SimpleNamespace(Anthropic=lambda api_key: types.SimpleNamespace(messages=Msgs()))
        searches = []
        import mail_actions
        with mock.patch.dict(sys.modules, {"anthropic": fake}), \
                mock.patch.object(draft_reply, "_account_brief", lambda a: ""), \
                mock.patch.object(mail_actions, "search_threads",
                                  lambda q, a, **k: searches.append(q) or [{"thread_id": "9", "subject": "Kickoff",
                                                                            "last_date": "2026-09-01", "snippet": "great call"}]), \
                mock.patch.object(mail_actions, "fetch_thread", lambda *a, **k: [{"from": "casey@x.com", "date": "2026-09-02", "body": "Thanks Pat!"}]):
            out = draft_reply.assist("acctone", env=ENV, log=lambda *a: None, **kw)
        return out, seen["messages"][0]["content"], searches

    def test_rewrites_casey_s_draft_by_his_instruction_without_history(self):
        out, prompt, searches = self.run_assist(instruction="make it warmer", draft="tuesday ok",
                                                to="Pat <pat@client.com>", subject="Meeting")
        self.assertEqual(out["body"], "Hi Pat — yes, Tuesday works.")
        self.assertIn("tuesday ok", prompt)
        self.assertIn("make it warmer", prompt)
        self.assertEqual(searches, [])                    # not asked → never read
        self.assertFalse(out["used_history"])

    def test_history_is_read_only_for_these_recipients_when_asked(self):
        with mock.patch("mailclass.own_addresses", lambda env: {"casey@x.com"}):
            out, prompt, searches = self.run_assist(instruction="reply yes", use_history=True,
                                                    to="Pat <pat@client.com>, Casey <casey@x.com>")
        self.assertTrue(searches and all("pat@client.com" in q for q in searches))
        self.assertFalse(any("casey@x.com" in q for q in searches))     # never Casey himself
        self.assertIn("Kickoff", prompt)
        self.assertIn("Thanks Pat!", prompt)
        self.assertTrue(out["used_history"])

    def test_nothing_to_go_on_is_refused(self):
        with self.assertRaises(ValueError):
            draft_reply.assist("acctone", "", env=ENV)


if __name__ == "__main__":
    unittest.main()
