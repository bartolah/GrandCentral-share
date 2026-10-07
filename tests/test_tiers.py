"""Dev #66: VIPs and Top Prospects (2026-10-06 interview).

Bart tags; Grand Central only suggests. A tag holds in every account. These
pin: tagging and clearing, suggestions that skip his team, his own addresses,
automated senders and anyone he waved off, and the 5-quiet-days nudge.
Scratch SQLite, fake accounts.

    .venv/bin/python -m unittest tests.test_tiers -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import tiers as T  # noqa: E402

A, B = "acctone", "accttwo"
ME = {"bart@bluenet.org"}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Tiers(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        self.conn.execute("INSERT INTO class_people (email, name, grp, teammate, added_at) "
                          "VALUES ('chris@parentco.com', 'Chris', NULL, 1, 'x')")
        self.conn.commit()
        self.n = 0

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def thread(self, acct, who, days_ago=1, direction="inbound"):
        self.n += 1
        self.conn.execute(
            "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, last_from_email, "
            "last_date, in_inbox, category, last_direction) VALUES (?,?,?,?,?,?,?,1,'people',?)",
            (str(1000 + self.n), acct, "Hi", json.dumps([f"{who.split('@')[0].title()} <{who}>", "bart@bluenet.org"]),
             f"{who.split('@')[0].title()} <{who}>", who, iso(datetime.now(timezone.utc) - timedelta(days=days_ago)),
             direction))
        self.conn.commit()

    def sends(self, who, n):
        for _ in range(n):
            db.record_send(self.conn, "1", A, who)

    def test_tag_and_clear_everywhere(self):
        T.set_tier(self.conn, "Gina@Client.com", "vip")
        self.assertEqual(T.tiers(self.conn), {"gina@client.com": "vip"})
        T.set_tier(self.conn, "gina@client.com", None)
        self.assertEqual(T.tiers(self.conn), {})
        with self.assertRaises(ValueError):
            T.set_tier(self.conn, "gina@client.com", "boss")

    def test_vip_suggestions_skip_team_self_automated_and_waved_off(self):
        for who in ("gina@client.com", "brett@parentco.com", "no-reply@client.com", "pat@other.com"):
            for _ in range(5):
                self.thread(A, who)
            self.sends(who, 2)
        self.thread(B, "gina@client.com")
        got = [c["email"] for c in T.suggest(self.conn, ME)["vip"]]
        self.assertEqual(sorted(got), ["gina@client.com", "pat@other.com"])   # parentco = Chris's company
        T.decline(self.conn, "pat@other.com", "vip")
        T.set_tier(self.conn, "gina@client.com", "vip")
        self.assertEqual(T.suggest(self.conn, ME)["vip"], [])

    def test_someone_you_never_wrote_to_is_not_a_vip(self):
        for _ in range(9):
            self.thread(A, "list@club.org")
        self.assertEqual(T.suggest(self.conn, ME)["vip"], [])

    def test_prospect_suggestions_come_from_mail_read_as_a_prospect(self):
        self.thread(A, "lead@buyer.com")
        tid = str(1000 + self.n)
        self.conn.execute("INSERT INTO mail_class (account, thread_id, fingerprint, version, roster, rules, classified_at) "
                          "VALUES (?, ?, 'f', 'v', 'r', ?, 'x')", (A, tid, json.dumps({"type": "Prospect"})))
        self.conn.commit()
        self.assertEqual([p["email"] for p in T.suggest(self.conn, ME)["prospect"]], ["lead@buyer.com"])
        T.decline(self.conn, "lead@buyer.com", "prospect")
        self.assertEqual(T.suggest(self.conn, ME)["prospect"], [])

    def test_a_quiet_top_prospect_is_nudged_per_account(self):
        self.thread(A, "lead@buyer.com", days_ago=7)
        self.thread(B, "lead@buyer.com", days_ago=1)
        T.set_tier(self.conn, "lead@buyer.com", "prospect")
        self.assertEqual([q["days"] for q in T.quiet_prospects(self.conn, A)], [7])
        self.assertEqual(T.quiet_prospects(self.conn, B), [])              # in touch there yesterday
        T.set_tier(self.conn, "lead@buyer.com", "vip")
        self.assertEqual(T.quiet_prospects(self.conn, A), [])


if __name__ == "__main__":
    unittest.main()
