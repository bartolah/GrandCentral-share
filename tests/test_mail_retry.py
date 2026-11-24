"""2026-11-19 (Casey chose "Retry quietly, then explain"): Gmail sometimes
answers a move to Trash (or an archive/star STORE) with "System Error
(Failure)" — a momentary error on its side. mail_actions retries that answer
up to 3 times; any other refusal fails at once. Fake IMAP, no network.

    .venv/bin/python -m unittest tests.test_mail_retry -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import mail_actions as ma  # noqa: E402


class FakeImap:
    def __init__(self, answers):
        self.answers, self.calls, self.noops = list(answers), [], 0

    def uid(self, *args):
        self.calls.append(args)
        return self.answers.pop(0)

    def noop(self):
        self.noops += 1


class Retry(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(ma.time, "sleep")
        self.p.start()

    def tearDown(self):
        self.p.stop()

    def test_gmail_system_error_is_retried_until_it_works(self):
        imap = FakeImap([("NO", [b"System Error (Failure)"]), ("OK", [b"done"])])
        self.assertEqual(ma._uid_retry(imap, "COPY", b"1", '"[Gmail]/Trash"')[0], "OK")
        self.assertEqual(len(imap.calls), 2)
        self.assertEqual(imap.noops, 1)

    def test_any_other_refusal_fails_at_once(self):
        imap = FakeImap([("NO", [b"[TRYCREATE] No folder"]), ("OK", [b"never"])])
        self.assertEqual(ma._uid_retry(imap, "COPY", b"1", '"x"')[0], "NO")
        self.assertEqual(len(imap.calls), 1)

    def test_gives_up_after_three_tries(self):
        imap = FakeImap([("NO", [b"System Error (Failure)"])] * 3)
        typ, resp = ma._uid_retry(imap, "STORE", b"1", "+FLAGS", r"(\Flagged)")
        self.assertEqual((typ, len(imap.calls)), ("NO", 3))
        self.assertTrue(ma._TRANSIENT.search(ma._resp(resp)))


if __name__ == "__main__":
    unittest.main()
