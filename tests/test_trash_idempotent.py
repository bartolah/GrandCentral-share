"""Trash is idempotent (2026-09-29): 17 "500 /mail-action" errors were the
same thread trashed twice — a delete racing a bundle's Delete all. The mail
was in Trash, but the board put the row back. A fake IMAP only.

    .venv/bin/python -m unittest tests.test_trash_idempotent -v
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import mail_actions  # noqa: E402
import mail_common as mc  # noqa: E402


class FakeImap:
    """Folders map to {uid: thread_id}; COPY to Trash moves (Gmail's model)."""
    def __init__(self, allmail, trash, vanish=()):
        self.box = {"[Gmail]/All Mail": dict(allmail), "[Gmail]/Trash": dict(trash)}
        self.sel = None
        self.vanish = set(vanish)      # uids another action trashes mid-way

    def select(self, name, readonly=True):
        self.sel = name.strip('"'); return "OK", [b""]

    def uid(self, cmd, *args):
        box = self.box[self.sel]
        if cmd == "SEARCH" and args[1] == "X-GM-THRID":
            return "OK", [b" ".join(u for u, t in box.items() if t == args[2])]
        if cmd == "SEARCH" and args[1] == "UID":
            return "OK", [args[2] if args[2] in box else b""]
        if cmd == "COPY":
            u = args[0]
            if u in self.vanish:
                self.box["[Gmail]/Trash"][u] = box.pop(u); return "NO", [b"gone"]
            if u not in box:
                return "NO", [b"no such message"]
            self.box["[Gmail]/Trash"][u] = box.pop(u); return "OK", [b""]
        raise AssertionError(cmd)

    def logout(self): pass


def run(imap):
    with mock.patch.object(mc, "connect", return_value=(imap, "me@x")), \
         mock.patch.object(mc, "find_folder", side_effect=lambda i, flag, fb: fb):
        return mail_actions.trash_thread("123", "acctone", env={}, log=lambda *a: None)


class Trash(unittest.TestCase):
    def test_a_normal_trash_moves_every_message(self):
        imap = FakeImap({b"1": "123", b"2": "123", b"9": "999"}, {})
        self.assertEqual(run(imap), {"messages": 2})
        self.assertEqual(set(imap.box["[Gmail]/Trash"]), {b"1", b"2"})

    def test_already_in_trash_is_success(self):
        imap = FakeImap({b"9": "999"}, {b"1": "123"})
        self.assertEqual(run(imap), {"messages": 0, "already": True})

    def test_a_message_trashed_mid_way_by_another_action_is_skipped(self):
        imap = FakeImap({b"1": "123", b"2": "123"}, {}, vanish={b"2"})
        self.assertEqual(run(imap), {"messages": 1})

    def test_a_thread_that_is_nowhere_still_fails(self):
        imap = FakeImap({b"9": "999"}, {})
        with self.assertRaises(RuntimeError):
            run(imap)


if __name__ == "__main__":
    unittest.main()
