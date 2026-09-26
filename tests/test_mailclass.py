"""E209 — email classification, phase 1 (the rules).

Offline and isolated like the other suites: scratch SQLite files, fake account
slugs ("acctone"/"accttwo") that exist in no config/.env, fixed dates. No model
call is made anywhere in phase 1, and nothing here reaches Gmail or Jev.

    .venv/bin/python -m unittest tests.test_mailclass -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import mailclass as mc  # noqa: E402

A, B = "acctone", "accttwo"
ME = "me@example.com"
TID = "1800000000000000001"              # the SAME Gmail thread id in both accounts
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)   # a Saturday


def row(**kw):
    base = {"thread_id": TID, "account": A, "subject": "s", "category": "people", "bulk": 0,
            "last_direction": "inbound", "last_from_email": "pat@outside.org",
            "participants": json.dumps(["Pat <pat@outside.org>", ME]),
            "last_date": "2026-09-24T12:00:00Z", "message_count": 1}
    base.update(kw)
    return base


def roster(mates=None, doms=None):
    return {"teammates": mates or {}, "domains": doms or {}, "hash": "h"}


class Rules(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(mc, "datetime", wraps=datetime)
        self.dt = p.start()
        self.dt.now.return_value = NOW
        self.addCleanup(p.stop)

    def c(self, r, ros=None, freq=None):
        return mc.classify(r, ros or roster(), freq or {}, {ME})

    def test_newsletter_is_machine_mail_that_needs_no_reply(self):
        out = self.c(row(category="newsletter", bulk=1))
        self.assertEqual((out["type"], out["respond"], out["relationship"]),
                         ("Newsletter", "no", "machine"))
        self.assertIsNone(out["follow_up"])

    def test_every_category_maps_to_a_type(self):
        for cat, typ in (("notification", "Update"), ("receipt", "Receipt"),
                         ("calendar", "Calendar"), ("promo", "Promo")):
            self.assertEqual(self.c(row(category=cat))["type"], typ, cat)

    def test_teammate_mail_is_team_collaboration_owed_in_one_business_day(self):
        ros = roster(mates={"sam@team.com": "Sam"})
        # Thursday 2026-09-24 + 1 business day = Friday
        out = self.c(row(last_from_email="sam@team.com",
                         participants=json.dumps(["Sam <sam@team.com>", ME])), ros)
        self.assertEqual((out["type"], out["relationship"], out["respond"]),
                         ("Team collaboration", "teammate", "unsure"))
        self.assertEqual(out["follow_up"], "2026-09-25")
        self.assertLess(out["confidence"], 0.9)

    def test_company_group_domain_counts_as_team(self):
        out = self.c(row(last_from_email="lee@sister.co"), roster(doms={"sister.co": "Group"}))
        self.assertEqual((out["type"], out["relationship"]), ("Team collaboration", "company"))

    def test_outside_mail_is_correspondence_owed_in_two_business_days(self):
        out = self.c(row(last_date="2026-09-25T12:00:00Z"))   # Friday → Tuesday
        self.assertEqual((out["type"], out["relationship"]), ("Correspondence", "new"))
        self.assertEqual(out["follow_up"], "2026-09-29")

    def test_waiting_on_a_teammate_skips_the_weekend(self):
        ros = roster(mates={"sam@team.com": "Sam"})
        out = self.c(row(last_direction="outbound", last_from_email=ME,
                         last_date="2026-09-25T12:00:00Z",
                         participants=json.dumps(["Sam <sam@team.com>", ME])), ros)
        self.assertEqual((out["waiting_on"], out["waiting_who"], out["respond"]),
                         ("teammate", "Sam", "no"))
        self.assertEqual(out["follow_up"], "2026-09-29")        # Fri + 2 business days

    def test_waiting_on_someone_outside_follows_up_in_four_days(self):
        out = self.c(row(last_direction="outbound", last_from_email=ME))
        self.assertEqual((out["waiting_on"], out["waiting_who"]), ("outside", "Pat"))
        self.assertEqual(out["follow_up"], "2026-09-28")

    def test_waiting_stops_after_thirty_days(self):
        out = self.c(row(last_direction="outbound", last_from_email=ME,
                         last_date="2026-08-01T12:00:00Z"))
        self.assertEqual(out["waiting_on"], "none")
        self.assertIsNone(out["follow_up"])

    def test_frequent_contact(self):
        out = self.c(row(), freq={"pat@outside.org": 3})
        self.assertEqual(out["relationship"], "frequent")
        self.assertEqual(self.c(row(), freq={"pat@outside.org": 1})["relationship"], "known")


def seed(conn, account, thread_id=TID, **kw):
    r = row(account=account, thread_id=thread_id, **kw)
    conn.execute(
        "INSERT OR REPLACE INTO mail_threads (thread_id, account, subject, participants, last_from, "
        "last_from_email, last_direction, snippet, last_date, message_count, bulk, category, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (r["thread_id"], account, r["subject"], r["participants"], "x", r["last_from_email"],
         r["last_direction"], "snip", r["last_date"], r["message_count"], r["bulk"],
         r["category"], r["last_date"]))
    conn.commit()


class Pass(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        p = mock.patch.object(mc, "own_addresses", return_value={ME})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def run_pass(self):
        return mc.run(self.conn, {}, [A, B], log=lambda *_: None, now=NOW)

    def test_only_changed_threads_are_reclassified(self):
        seed(self.conn, A)
        self.assertEqual(self.run_pass()[A], 1)
        self.assertEqual(self.run_pass()[A], 0)
        seed(self.conn, A, last_date="2026-09-25T09:00:00Z", message_count=2)
        self.assertEqual(self.run_pass()[A], 1)

    def test_a_roster_change_reclassifies(self):
        seed(self.conn, A)
        self.run_pass()
        db.class_person_set(self.conn, "pat@outside.org", "Pat")
        self.assertEqual(self.run_pass()[A], 1)
        self.assertEqual(db.class_get(self.conn, A, TID)["relationship"], "teammate")

    def test_same_thread_id_in_two_accounts_stays_two_classifications(self):
        seed(self.conn, A, category="newsletter", bulk=1)
        seed(self.conn, B)
        self.run_pass()
        self.assertEqual(db.class_get(self.conn, A, TID)["type"], "Newsletter")
        self.assertEqual(db.class_get(self.conn, B, TID)["type"], "Correspondence")

    def test_frequent_contact_is_counted_per_account(self):
        for i in range(3):
            seed(self.conn, A, thread_id=f"t{i}", last_direction="outbound", last_from_email=ME,
                 last_date="2026-09-20T12:00:00Z")
        with mock.patch.object(mc, "datetime", wraps=datetime) as dt:
            dt.now.return_value = NOW
            self.assertEqual(mc.frequent_counts(self.conn, A).get("pat@outside.org"), 3)
            self.assertEqual(mc.frequent_counts(self.conn, B), {})

    def test_the_model_judges_respond_and_a_correction_beats_both(self):
        seed(self.conn, A)
        self.run_pass()
        db.class_merge_model(self.conn, {(A, TID): {"summary": "asks for dates",
                                                    "reply_needed": True, "importance": 2}}, "haiku")
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["respond"], got["priority"], got["source"]["respond"]),
                         ("yes", 2, "haiku"))
        db.class_correct(self.conn, A, TID, "respond", "no")
        db.class_correct(self.conn, A, TID, "type", "Sales/outreach")
        seed(self.conn, A, last_date="2026-09-25T09:00:00Z", message_count=2)
        self.run_pass()
        db.class_merge_model(self.conn, {(A, TID): {"reply_needed": True, "importance": 3}}, "haiku")
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["respond"], got["type"]), ("no", "Sales/outreach"))
        self.assertEqual(got["source"]["respond"], "you")
        self.assertIsNone(got["follow_up"])          # nothing owed, nobody to chase
        self.assertIsNone(db.class_get(self.conn, B, TID))   # never leaked to B

    def test_corrections_are_validated(self):
        seed(self.conn, A)
        self.run_pass()
        for field, value in (("type", "Spam"), ("respond", "maybe"), ("colour", "x"),
                             ("follow_up", "Tuesday")):
            with self.assertRaises(ValueError, msg=field):
                db.class_correct(self.conn, A, TID, field, value)
        with self.assertRaises(ValueError):
            db.class_correct(self.conn, B, TID, "respond", "no")   # no row in B

    def test_a_person_can_belong_to_a_group_on_any_domain(self):
        # E212: Pat writes from outside.org but works with the group
        seed(self.conn, A)
        db.class_group_set(self.conn, "sister.co", "CB group")
        db.class_person_set(self.conn, "pat@outside.org", "Pat", grp="CB group", teammate=False)
        self.run_pass()
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["relationship"], got["group"], got["type"]),
                         ("company", "CB group", "Team collaboration"))
        db.class_person_set(self.conn, "pat@outside.org", "", grp="CB group", teammate=True)
        self.run_pass()
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["relationship"], got["group"]), ("teammate", "CB group"))
        roster = db.class_roster(self.conn)["people"]
        self.assertEqual(roster[0]["name"], "Pat")          # a blank name keeps the old one

    def test_domain_membership_names_the_group(self):
        seed(self.conn, A, last_from_email="lee@sister.co",
             participants=json.dumps(["Lee <lee@sister.co>", ME]))
        db.class_group_set(self.conn, "sister.co", "CB group")
        self.run_pass()
        self.assertEqual(db.class_get(self.conn, A, TID)["group"], "CB group")

    def test_roster_validation(self):
        with self.assertRaises(ValueError):
            db.class_person_set(self.conn, "x@y.com", "X", grp=None, teammate=False)
        with self.assertRaises(ValueError):
            db.class_person_set(self.conn, "not-an-address")
        with self.assertRaises(ValueError):
            db.class_group_set(self.conn, "no dots", "G")
        self.assertEqual(db.class_group_set(self.conn, "@ConnectBlue.com", "CB group"), "connectblue.com")


class JevGate(unittest.TestCase):
    """§6, 2026-09-26: Jev sees an account only while its switch is on AND a key is set."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_off_by_default_and_per_account(self):
        key = {"JEV_API_KEY": "k-123"}
        self.assertFalse(mc.jev_allowed(self.conn, A, key))
        db.class_set_jev(self.conn, A, True)
        self.assertTrue(mc.jev_allowed(self.conn, A, key))
        self.assertFalse(mc.jev_allowed(self.conn, B, key))
        self.assertFalse(mc.jev_allowed(self.conn, A, {}))          # switch on, no key
        db.class_set_jev(self.conn, A, False)
        self.assertFalse(mc.jev_allowed(self.conn, A, key))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class Wiring(unittest.TestCase):
    def test_sync_and_sweep_run_the_classifier(self):
        self.assertIn("mailclass.run(conn, env, accounts", _read("sweeper", "mail_sync.py"))
        src = _read("sweeper", "sweep.py")
        self.assertIn('class_merge_model(conn, assessed, "haiku")', src)

    def test_agents_cannot_reach_the_classify_writes(self):
        sys.path.insert(0, os.path.join(ROOT, "integrations", "mcp"))
        import gc_client
        self.assertNotIn(("POST", "/classify"), set(gc_client.ALLOWED))


if __name__ == "__main__":
    unittest.main()
