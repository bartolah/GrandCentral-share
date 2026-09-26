"""Contacts / address book and recipient typeahead (dev #32 #33 #34, 2026-09-26).

Scratch SQLite, fake accounts. The address book is per account (built from
that account's own mail) with Bart's edits applied everywhere.

    .venv/bin/python -m unittest tests.test_contacts -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402

A, B = "acctone", "accttwo"
ME = "me@example.com"


def seed(conn, account, tid, people, direction="inbound", date="2026-09-20T12:00:00Z", last_from=""):
    conn.execute(
        "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, last_direction, "
        "last_date, synced_at) VALUES (?,?,?,?,?,?,?,?)",
        (tid, account, "s", json.dumps(people), last_from, direction, date, date))
    conn.commit()


class AddressBook(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        seed(self.conn, A, "t1", ["Pat Lee <pat@out.org>", ME])
        seed(self.conn, A, "t2", ["pat@out.org", "Ann <ann@out.org>", ME])
        seed(self.conn, A, "t3", ["NewsBot <no-reply@news.org>"])
        seed(self.conn, B, "t9", ["Zed <zed@else.org>", ME])

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def book(self, acct=A):
        return {c["email"]: c for c in db.contacts_for(self.conn, acct, {ME})}

    def test_built_from_this_accounts_mail_only(self):
        a, b = self.book(A), self.book(B)
        self.assertEqual(set(a), {"pat@out.org", "ann@out.org", "no-reply@news.org"})
        self.assertEqual(set(b), {"zed@else.org"})
        self.assertNotIn(ME, a)
        self.assertEqual((a["pat@out.org"]["name"], a["pat@out.org"]["threads"]), ("Pat Lee", 2))

    def test_automated_senders_are_flagged(self):
        a = self.book()
        self.assertTrue(a["no-reply@news.org"]["auto"])
        self.assertFalse(a["pat@out.org"]["auto"])

    def test_bart_edits_win_and_hidden_ones_leave(self):
        db.contact_set(self.conn, "pat@out.org", name="Patricia Lee", notes="met at the summit")
        db.contact_set(self.conn, "ann@out.org", hidden=True)
        a = self.book()
        self.assertEqual((a["pat@out.org"]["name"], a["pat@out.org"]["notes"], a["pat@out.org"]["edited"]),
                         ("Patricia Lee", "met at the summit", True))
        self.assertNotIn("ann@out.org", a)
        db.contact_set(self.conn, "pat@out.org", notes="")      # a notes-only edit keeps the name
        self.assertEqual(self.book()["pat@out.org"]["name"], "Patricia Lee")

    def test_a_name_cannot_smuggle_address_syntax(self):
        db.contact_set(self.conn, "pat@out.org", name='Evil <x@y.com>, "z"')
        self.assertNotIn("<", self.book()["pat@out.org"]["name"])
        with self.assertRaises(ValueError):
            db.contact_set(self.conn, "not-an-address", name="x")

    def test_addresses_split_on_commas_outside_quotes_and_brackets(self):
        self.assertEqual(db._split_addrs('A <a@x.com>, "Smith, John" <j@y.com>, bob@z.com'),
                         ["A <a@x.com>", '"Smith, John" <j@y.com>', "bob@z.com"])


class ClientGuards(unittest.TestCase):
    def test_typeahead_and_poll_fixes_stay_in(self):
        with open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("map(resolveContact)", src)                         # #34
        self.assertIn('oninput="chipInput(', src)                        # #34
        self.assertIn("ae.matches('input,textarea,select,[contenteditable]')", src)   # #31
        self.assertNotIn("},{once:true,passive:true}));", src)            # #30


if __name__ == "__main__":
    unittest.main()
