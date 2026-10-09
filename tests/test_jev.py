"""E210 — Jev (TypeSafe) classification, phase 2.

Offline: a fake `post` stands in for api.typesafe.ai and records every request
body, so the tests can assert exactly what would have left the host. Scratch
SQLite, fake account slugs, no network.

    .venv/bin/python -m unittest tests.test_jev -v
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
import jev  # noqa: E402
import mailclass as mc  # noqa: E402

A, B = "acctone", "accttwo"
ME = "me@example.com"
TID = "1800000000000000001"
NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
KEY = {"JEV_API_KEY": "ts-test"}
LONG = "x" * 1000


def seed(conn, account, thread_id=TID, **kw):
    r = {"subject": "Q4 flight dates", "participants": json.dumps(["Pat <pat@outside.org>", ME]),
         "last_from": "Pat", "last_from_email": "pat@outside.org", "last_direction": "inbound",
         "snippet": "Can you confirm the start date?", "last_date": "2026-09-25T12:00:00Z",
         "message_count": 1, "bulk": 0, "category": "people"}
    r.update(kw)
    conn.execute(
        "INSERT OR REPLACE INTO mail_threads (thread_id, account, subject, participants, last_from, "
        "last_from_email, last_direction, snippet, last_date, message_count, bulk, category, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, account, r["subject"], r["participants"], r["last_from"], r["last_from_email"],
         r["last_direction"], r["snippet"], r["last_date"], r["message_count"], r["bulk"],
         r["category"], r["last_date"]))
    conn.commit()


def answer(typ="Sales/outreach", respond="yes", prio=2.2, conf=0.8, expects=None):
    a = {"type": {"type": "choice", "choice": typ, "confidence": conf},
         "priority": {"type": "score", "score": prio, "confidence": conf}}
    if expects is None:
        a["respond"] = {"type": "choice", "choice": respond, "confidence": conf}
    else:
        a["expects_reply"] = {"type": "noul", "noul": expects}
    return {"model": "jev-1.13.0", "answers": a, "usage": {"input_tokens": 300, "output_tokens": 20}}


class JevCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        p = mock.patch.object(mc, "own_addresses", return_value={ME})
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        self.reply = answer()

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def post(self, key, body):
        self.sent.append(body)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply

    def classify(self):
        mc.run(self.conn, {}, [A, B], log=lambda *_: None, now=NOW)

    def ask(self, env=KEY):
        return jev.run(self.conn, env, [A, B], log=lambda *_: None, post=self.post, now=NOW)


class Gate(JevCase):
    def test_nothing_is_sent_while_the_switch_is_off(self):
        seed(self.conn, A)
        self.classify()
        self.ask()
        self.assertEqual(self.sent, [])

    def test_nothing_is_sent_without_a_key(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask(env={})
        self.assertEqual(self.sent, [])

    def test_only_the_switched_on_account_is_sent(self):
        seed(self.conn, A)
        seed(self.conn, B, subject="B's private mail")
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        self.assertEqual(len(self.sent), 1)
        self.assertNotIn("B's private mail", json.dumps(self.sent))

    def test_machine_mail_never_leaves(self):
        seed(self.conn, A, thread_id="n1", category="newsletter", bulk=1)
        seed(self.conn, A, thread_id="n2", category="receipt")
        seed(self.conn, A, thread_id="n3", category="people", bulk=1)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        self.assertEqual(self.sent, [])


class Payload(JevCase):
    def test_the_request_carries_only_what_section_6_allows(self):
        seed(self.conn, A, snippet=LONG)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        body = self.sent[0]
        self.assertEqual(set(body), {"state", "model", "questions"})
        self.assertEqual(set(body["state"]), {"subject", "sender", "people_on_thread", "who_wrote_last",
                                              "messages_in_thread", "sender_relationship", "latest_message"})
        self.assertEqual(len(body["state"]["latest_message"]), 400)
        self.assertEqual(set(body["questions"]), {"type", "priority", "respond"})

    def test_when_the_owner_wrote_last_jev_is_asked_if_a_reply_is_expected(self):
        seed(self.conn, A, last_direction="outbound", last_from_email=ME)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.reply = answer(expects=0.1)
        self.ask()
        self.assertEqual(set(self.sent[0]["questions"]), {"type", "priority", "expects_reply"})
        self.assertEqual(self.sent[0]["state"]["who_wrote_last"], "the owner")

    def test_a_thread_is_asked_once_until_new_mail_arrives(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        self.ask()
        self.assertEqual(len(self.sent), 1)
        seed(self.conn, A, last_date="2026-09-25T18:00:00Z", message_count=2)
        self.classify()
        self.ask()
        self.assertEqual(len(self.sent), 2)


class Answers(JevCase):
    def test_confident_answers_win_and_switching_off_restores_the_rest(self):
        seed(self.conn, A)
        self.classify()
        db.class_merge_model(self.conn, {(A, TID): {"reply_needed": False, "importance": 1}}, "haiku")
        db.class_set_jev(self.conn, A, True)
        self.ask()
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["type"], got["respond"], got["priority"]), ("Sales/outreach", "yes", 2))
        self.assertEqual(got["source"]["type"], "jev")
        self.assertEqual(got["jev_model"], "jev-1.13.0")
        db.class_set_jev(self.conn, A, False)
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["type"], got["respond"]), ("Correspondence", "no"))   # rules + Haiku

    def test_an_unsure_answer_does_not_override(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.reply = answer(conf=0.2)
        self.ask()
        got = db.class_get(self.conn, A, TID)
        self.assertEqual(got["type"], "Correspondence")
        self.assertEqual(got["source"]["type"], "rules")

    def test_your_correction_still_beats_jev(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        db.class_correct(self.conn, A, TID, "type", "Correspondence")
        self.assertEqual(db.class_get(self.conn, A, TID)["type"], "Correspondence")

    def test_nobody_to_chase_when_your_last_message_asked_nothing(self):
        seed(self.conn, A, last_direction="outbound", last_from_email=ME)
        self.classify()
        self.assertEqual(db.class_get(self.conn, A, TID)["waiting_on"], "outside")
        db.class_set_jev(self.conn, A, True)
        self.reply = answer(expects=0.1)
        self.ask()
        got = db.class_get(self.conn, A, TID)
        self.assertEqual((got["waiting_on"], got["follow_up"]), ("none", None))


class SalesSplit(JevCase):
    def test_jev_is_asked_to_tell_prospects_from_cold_pitches_and_spam(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.ask()
        opts = set(self.sent[0]["questions"]["type"]["criteria"])
        self.assertTrue({"Prospect", "Cold pitch", "Spam"} <= opts)
        self.assertNotIn("Sales/outreach", opts)

    def test_new_questions_re_ask_threads_answered_under_the_old_ones(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        fp = self.conn.execute("SELECT fingerprint FROM mail_class").fetchone()[0]
        db.class_write_jev(self.conn, A, TID, {"type": "Sales/outreach", "confidence": {"type": .9}}, fp)
        self.ask()
        self.assertEqual(len(self.sent), 1)          # answered at qv 1 → asked again
        self.ask()
        self.assertEqual(len(self.sent), 1)          # now current → left alone


class ValueScore(unittest.TestCase):
    def v(self, **kw):
        base = {"type": "Correspondence", "relationship": "new", "respond": "unsure", "priority": None}
        base.update(kw)
        return db.class_value(base, today="2026-09-26")[0]

    def test_it_ranks_what_matters_above_what_doesnt(self):
        prospect = self.v(type="Prospect", relationship="frequent", respond="yes", priority=3)
        mate = self.v(type="Team collaboration", relationship="teammate", respond="yes", priority=2)
        pitch = self.v(type="Cold pitch", respond="optional", priority=1)
        spam = self.v(type="Spam", relationship="machine", respond="no", priority=0)
        self.assertGreater(prospect, pitch)
        self.assertGreater(mate, pitch)
        self.assertGreater(pitch, spam)
        self.assertEqual(spam, 0)
        self.assertLessEqual(prospect, 100)

    def test_a_due_follow_up_raises_it_and_the_parts_say_why(self):
        due = db.class_value({"type": "Correspondence", "relationship": "known", "respond": "no",
                              "follow_up": "2026-09-25"}, today="2026-09-26")
        later = db.class_value({"type": "Correspondence", "relationship": "known", "respond": "no",
                                "follow_up": "2026-10-10"}, today="2026-09-26")
        self.assertEqual(due[0] - later[0], 10)
        self.assertIn("follow-up due", [p for p, _ in due[1]])


class Failures(JevCase):
    def test_a_rejected_key_stops_the_run(self):
        for i in range(3):
            seed(self.conn, A, thread_id=f"t{i}")
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.reply = jev.JevError(401, "HTTP 401")
        logs = []
        jev.run(self.conn, KEY, [A], log=logs.append, post=self.post, now=NOW)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("key rejected", logs[0])

    def test_a_malformed_thread_is_not_retried_forever(self):
        seed(self.conn, A)
        self.classify()
        db.class_set_jev(self.conn, A, True)
        self.reply = jev.JevError(422, "HTTP 422")
        self.ask()
        self.ask()
        self.assertEqual(len(self.sent), 1)


class SettingsPage(unittest.TestCase):
    def test_connections_tab_draws_every_key_the_server_offers(self):
        # E210 shipped with the key registered server-side but a hardcoded
        # list in settings.html, so the Jev field never appeared.
        with open(os.path.join(ROOT, "dashboard", "settings.html"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("Object.keys(S).filter(n => !known.includes(n))", src)
        sys.path.insert(0, os.path.join(ROOT, "sweeper"))
        import connections
        self.assertIn("jev", connections.SINGLE)
        self.assertIn("jev", connections._SINGLE_TESTS)


if __name__ == "__main__":
    unittest.main()
