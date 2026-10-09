"""E276: following up on emails "of note" (Casey 2026-10-07).

Casey picks what to watch; Grand Central never watches anything by itself and
runs no model. These pin: due dates (3 business days for the team, 5 for
deals), a watch closing on the reply it waits for (the teammate for an
assignment, anyone outside for a deal), never on Casey's own mail, the
after-send suggestion's rules, +2 days, and the ask setting. Scratch SQLite,
fake accounts; nothing reaches Gmail.

    .venv/bin/python -m unittest tests.test_watch -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import watch as W  # noqa: E402

A = "acctone"
ENV = {"GMAIL_ACCTONE_USER": "casey@bluenet.org"}
ME = {"casey@bluenet.org"}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Watch(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        self.conn.execute("INSERT INTO class_people (email, name, grp, teammate, added_at) "
                          "VALUES ('gina@bluenet.org', 'Gina', NULL, 1, 'x')")
        self.conn.commit()
        self.p = mock.patch("mailclass.own_addresses", lambda env: ME)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.conn.close()
        self._dir.cleanup()

    def thread(self, tid, last_from, direction, minutes_ago=0):
        self.conn.execute("INSERT OR REPLACE INTO mail_threads (thread_id, account, subject, last_from_email, "
                          "last_direction, last_date, in_inbox, category) VALUES (?,?,?,?,?,?,1,'people')",
                          (tid, A, "IO revisions", last_from, direction,
                           iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))))
        self.conn.commit()

    def test_due_dates_follow_casey_s_defaults(self):
        fri = date(2026, 10, 9)
        self.assertEqual(W.default_due("assigned", fri), "2026-10-14")      # 3 business days, over the weekend
        self.assertEqual(W.default_due("deal", fri), "2026-10-14")          # 5 calendar days
        self.assertIsNone(W.default_due("remind", fri))

    def test_an_assignment_closes_only_when_that_teammate_replies(self):
        self.thread("700", "casey@bluenet.org", "outbound", minutes_ago=5)
        wid = W.add(self.conn, A, "assigned", "700", who="gina@bluenet.org", who_name="Gina", note="revise the IO")
        self.thread("700", "pat@client.com", "inbound")                     # someone else wrote
        W.check(self.conn, ENV)
        self.assertEqual(W._get(self.conn, wid)["status"], "open")
        self.thread("700", "gina@bluenet.org", "inbound")
        W.check(self.conn, ENV)
        w = W._get(self.conn, wid)
        self.assertEqual((w["status"], w["closed_reason"]), ("closed", "replied"))

    def test_a_deal_closes_on_any_outside_reply_but_not_casey_s_own(self):
        wid = W.add(self.conn, A, "deal", "701", who="pat@client.com")
        self.thread("701", "casey@bluenet.org", "inbound")                   # a copy of Casey's own mail
        W.check(self.conn, ENV)
        self.assertEqual(W._get(self.conn, wid)["status"], "open")
        self.thread("701", "assistant@client.com", "inbound")
        W.check(self.conn, ENV)
        self.assertEqual(W._get(self.conn, wid)["status"], "closed")

    def test_a_reply_from_before_the_send_does_not_count(self):
        self.thread("702", "gina@bluenet.org", "inbound", minutes_ago=60)
        wid = W.add(self.conn, A, "assigned", "702", who="gina@bluenet.org")
        W.check(self.conn, ENV)
        self.assertEqual(W._get(self.conn, wid)["status"], "open")

    def test_due_listing_push_and_done(self):
        wid = W.add(self.conn, A, "deal", "703", who="pat@client.com",
                    due=(date.today() - timedelta(days=1)).isoformat())
        rows = W.listing(self.conn, A)
        self.assertTrue(rows[0]["due_now"])
        self.assertIn("Waiting on pat", W.label(rows[0]))
        W.push(self.conn, wid, 2)
        self.assertFalse(W.listing(self.conn, A)[0]["due_now"])
        W.done(self.conn, wid)
        self.assertEqual(W.listing(self.conn, A), [])
        self.assertEqual(len(W.listing(self.conn, A, include_closed=True)), 1)

    def test_one_open_watch_of_a_kind_per_thread(self):
        W.add(self.conn, A, "deal", "704", who="pat@client.com")
        W.add(self.conn, A, "deal", "704", who="pat@client.com", note="second thought")
        self.assertEqual([w["note"] for w in W.listing(self.conn, A)], ["second thought"])

    def test_suggestion_rules_and_the_ask_setting(self):
        s = W.suggest(self.conn, A, ENV, to="Gina <gina@bluenet.org>")
        self.assertEqual((s["ask"], s["kind"], [m["email"] for m in s["teammates"]]),
                         (True, "assigned", ["gina@bluenet.org"]))
        s = W.suggest(self.conn, A, ENV, to="Pat <pat@client.com>", cc="gina@bluenet.org")
        self.assertEqual((s["ask"], s["kind"], s["contact"]["email"]), (True, None, "pat@client.com"))
        import tiers
        tiers.set_tier(self.conn, "pat@client.com", "prospect")
        self.assertEqual(W.suggest(self.conn, A, ENV, to="pat@client.com")["kind"], "deal")
        self.assertFalse(W.suggest(self.conn, A, ENV, to="no-reply@service.com")["ask"])
        W.set_ask_mode(self.conn, A, "never")
        self.assertFalse(W.suggest(self.conn, A, ENV, to="pat@client.com")["ask"])
        with self.assertRaises(ValueError):
            W.set_ask_mode(self.conn, A, "sometimes")

    def test_a_due_follow_up_leads_home_s_follow_ups(self):
        import home_view
        self.thread("706", "casey@bluenet.org", "outbound", minutes_ago=60 * 24 * 5)
        W.add(self.conn, A, "assigned", "706", who="gina@bluenet.org", who_name="Gina", note="send the IO",
              due=(date.today() - timedelta(days=1)).isoformat())
        follows = home_view.section(self.conn, A)["follows"]
        self.assertTrue(follows and follows[0]["state"].startswith("👤 Gina owes you: send the IO"), follows[:1])

    def test_validation(self):
        with self.assertRaises(ValueError):
            W.add(self.conn, A, "assigned", "705")                         # assigned to whom?
        with self.assertRaises(ValueError):
            W.add(self.conn, A, "deal", "")                                # no thread, no message
        with self.assertRaises(ValueError):
            W.add(self.conn, A, "spy", "705")
        self.assertTrue(W.add(self.conn, A, "deal", "", message_id="<abc@bluenet.org>", who="pat@client.com"))


if __name__ == "__main__":
    unittest.main()
