"""E269: open and click tracking on Grand Central's sends (BUILD_SPEC §6, 2026-10-06).

Pins the promises in the §6 entry: tracking is inert until the domain is
healthy and the secret set; teammates-only mail and switched-off accounts
aren't tracked by default but Casey's 👁 toggle wins; only the HTML part
changes; click redirects are signed exactly as the Worker checks them; Casey's
own view strips everything; automatic opens (Apple prefetch, scanners, the
first 30 seconds) never count; and a tracking failure never stops a send.
Scratch SQLite, fake accounts, no network, no SMTP.

    .venv/bin/python -m unittest tests.test_tracking -v
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import tracking as T  # noqa: E402

A = "acctone"
ENV = {"GMAIL_ACCTONE_USER": "casey@bluenet.org", "GMAIL_ACCTTWO_USER": "casey@gmail.com",
       "TRACK_SECRET": "s3cret"}


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        self.conn = db.connect(self.path)
        self.conn.execute("INSERT INTO class_people (email, name, grp, teammate, added_at) "
                          "VALUES ('gina@team.org', 'Gina', NULL, 1, 'x')")
        self.conn.execute("INSERT INTO class_groups (domain, grp, added_at) VALUES ('bluenet.org', 'BlueNet', 'x')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def healthy(self, dom="bluenet.org"):
        self.conn.execute("INSERT OR REPLACE INTO track_health (domain, ok, checked_at) VALUES (?, 1, ?)",
                          (dom, iso(datetime.now(timezone.utc))))
        self.conn.commit()


class Decide(Base):
    def test_inert_until_the_domain_is_healthy_and_the_secret_set(self):
        d = T.decide(self.conn, A, ENV, "pat@client.com")
        self.assertEqual((d["available"], d["on"]), (False, False))
        self.healthy()
        self.assertFalse(T.decide(self.conn, A, {**ENV, "TRACK_SECRET": ""}, "pat@client.com")["on"])
        self.assertFalse(T.decide(self.conn, "accttwo", ENV, "pat@client.com")["available"])  # gmail.com

    def test_outside_recipients_on_teammates_off_and_the_toggle_wins(self):
        self.healthy()
        with mock.patch.object(T, "DEFAULT_ON", {A}):
            self.assertTrue(T.decide(self.conn, A, ENV, "Pat <pat@client.com>", cc="gina@team.org")["on"])
            self.assertFalse(T.decide(self.conn, A, ENV, "gina@team.org", cc="sam@bluenet.org")["on"])
            # a teammate's own company domain is inside too (Brett at Chris's company)
            self.assertFalse(T.decide(self.conn, A, ENV, "brett@team.org")["on"])
            self.assertTrue(T.decide(self.conn, A, ENV, "gina@team.org", override=True)["on"])
            self.assertFalse(T.decide(self.conn, A, ENV, "pat@client.com", override=False)["on"])
            T.set_settings(self.conn, A, enabled=False)
            self.assertFalse(T.decide(self.conn, A, ENV, "pat@client.com")["on"])
            self.assertTrue(T.decide(self.conn, A, ENV, "pat@client.com", override=True)["on"])

    def test_accounts_not_listed_default_off_and_signals_are_per_account(self):
        self.assertFalse(T.settings(self.conn, "accttwo")["enabled"])
        with mock.patch.object(T, "DEFAULT_ON", {A}):
            T.set_settings(self.conn, A, clicks=False, ping=False)
            self.assertEqual(T.settings(self.conn, A), {"enabled": True, "clicks": False, "ping": False, "waiting": True})
        with self.assertRaises(ValueError):
            T.set_settings(self.conn, A, spy=True)


class Mail(Base):
    HTML = ('<p>Hi — see <a href="https://site.com/kit?a=1&amp;b=2">the kit</a>, '
            '<a href="mailto:x@y.com">mail</a> and <a href=\'http://old.org\'>old</a>.</p>')

    def test_image_added_links_signed_and_mailto_left_alone(self):
        html, links = T.prepare(self.HTML, "plain", "TOKEN_abcdefghijklmn", "bluenet.org", "s3cret", True)
        self.assertEqual(links, ["https://site.com/kit?a=1&b=2", "http://old.org"])
        self.assertIn('src="https://t.bluenet.org/o/TOKEN_abcdefghijklmn.gif"', html)
        self.assertIn('href="mailto:x@y.com"', html)
        hrefs = re.findall(r'href=["\']([^"\']+)', html)
        red = [h for h in hrefs if "/c/" in h][0].replace("&amp;", "&")
        m = re.match(r"https://t\.bluenet\.org/c/(\w+)/(\d+)/([\w-]+)\?u=(.+)", red)
        url = urllib.parse.unquote(m.group(4))
        self.assertEqual(url, "https://site.com/kit?a=1&b=2")
        self.assertEqual(m.group(3), T.sign("s3cret", m.group(1), int(m.group(2)), url))

    def test_clicks_off_means_only_the_image(self):
        html, links = T.prepare(self.HTML, "plain", "TOKEN_abcdefghijklmn", "bluenet.org", "s", False)
        self.assertEqual(links, [])
        self.assertIn('href="https://site.com/kit?a=1&amp;b=2"', html)

    def test_a_plain_send_gets_an_html_twin(self):
        html, links = T.prepare(None, "Hi <Pat>\nhttps://site.com", "TOKEN_abcdefghijklmn", "bluenet.org", "s", True)
        self.assertIn("Hi &lt;Pat&gt;", html)
        self.assertEqual(links, ["https://site.com"])
        # Outlook ignores white-space:pre-wrap — line breaks must be real <br>s
        self.assertNotIn("pre-wrap", html)
        self.assertIn("Hi &lt;Pat&gt;<br>", html)

    def test_casey_s_own_view_strips_the_image_and_unwraps_links(self):
        html, _ = T.prepare(self.HTML, "", "TOKEN_abcdefghijklmn", "bluenet.org", "s3cret", True)
        clean = T.strip(html, ["bluenet.org"])
        self.assertNotIn("t.bluenet.org", clean)
        self.assertIn('href="https://site.com/kit?a=1&amp;b=2"', clean)
        self.assertEqual(T.strip(self.HTML, ["bluenet.org"]), self.HTML)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_the_worker_checks_the_same_signature(self):
        with open(os.path.join(ROOT, "integrations", "tracker", "worker.js")) as f:
            js = f.read()
        fn = js[js.index("async function hmac"):js.index("async function log")]
        out = subprocess.run(["node", "-e", fn + "hmac('s3cret','TOKEN_abcdefghijklmn|0|https://a.com/?x=1&y=2')"
                              ".then(s=>console.log(s))"], capture_output=True, text=True, timeout=20)
        self.assertEqual(out.stdout.strip(), T.sign("s3cret", "TOKEN_abcdefghijklmn", 0, "https://a.com/?x=1&y=2"))


class Events(Base):
    def send(self, token="TOKEN_abcdefghijklmn", sent=None, tid="900"):
        T.record(self.conn, token, A, tid, 1, "Proposal", "Pat <pat@client.com>", "", ["https://site.com"], "<m@x>", True)
        if sent:
            self.conn.execute("UPDATE track_sends SET sent_at = ? WHERE token = ?", (sent, token))
            self.conn.commit()

    def test_automatic_opens_never_count(self):
        sent = datetime.now(timezone.utc) - timedelta(days=4)
        self.send(sent=iso(sent))
        T.ingest(self.conn, [
            {"id": 1, "token": "TOKEN_abcdefghijklmn", "kind": "open", "src": "proxy-google", "ts": iso(sent + timedelta(seconds=5))},
            {"id": 2, "token": "TOKEN_abcdefghijklmn", "kind": "open", "src": "apple-prefetch", "ts": iso(sent + timedelta(hours=1))},
            {"id": 3, "token": "TOKEN_abcdefghijklmn", "kind": "open", "src": "proxy-google", "ts": iso(sent + timedelta(hours=2))},
            {"id": 4, "token": "TOKEN_abcdefghijklmn", "kind": "click", "link": 0, "src": "client", "ts": iso(sent + timedelta(hours=3))},
            {"id": 5, "token": "UNKNOWN_abcdefghijklm", "kind": "open", "src": "client", "ts": iso(sent)},
        ])
        T.ingest(self.conn, [{"id": 3, "token": "TOKEN_abcdefghijklmn", "kind": "open", "src": "client", "ts": "x"}])  # re-pulled
        with mock.patch.object(T, "DEFAULT_ON", {A}):
            s = T.summary(self.conn, A)["900"]
        self.assertEqual((s["opens"], s["auto"], s["clicks"], s["clicked"]), (1, 2, 1, ["https://site.com"]))
        self.assertTrue(s["nudge"])                     # opened, 3+ days, no reply

    def test_no_nudge_after_a_reply_or_with_waiting_off(self):
        sent = datetime.now(timezone.utc) - timedelta(days=4)
        self.send(sent=iso(sent))
        T.ingest(self.conn, [{"id": 1, "token": "TOKEN_abcdefghijklmn", "kind": "open", "src": "client",
                              "ts": iso(sent + timedelta(hours=2))}])
        with mock.patch.object(T, "DEFAULT_ON", {A}):
            T.set_settings(self.conn, A, waiting=False)
            self.assertFalse(T.summary(self.conn, A)["900"]["nudge"])
            T.set_settings(self.conn, A, waiting=True)
            self.conn.execute("INSERT INTO mail_threads (thread_id, account, subject, last_direction, last_date, in_inbox) "
                              "VALUES ('900', ?, 'Re: Proposal', 'inbound', ?, 1)", (A, iso(datetime.now(timezone.utc))))
            self.conn.commit()
            s = T.summary(self.conn, A)["900"]
            self.assertEqual((s["replied"], s["nudge"]), (True, False))


class SendPath(Base):
    def _send(self, track_env):
        import send_mail
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def send_message(self, msg): sent["msg"] = msg

        real = db.connect
        env = {**ENV, "GMAIL_ACCTONE_PASS": "x", **track_env}
        with mock.patch.object(send_mail.smtplib, "SMTP_SSL", FakeSMTP), \
                mock.patch.object(db, "connect", lambda *a, **k: real(self.path)), \
                mock.patch.dict(send_mail.ACCOUNTS, {A: ("GMAIL_ACCTONE_USER", "GMAIL_ACCTONE_PASS")}), \
                mock.patch.object(T, "DEFAULT_ON", {A}):
            out = send_mail.send_new(A, "Hello https://site.com", "pat@client.com", "Proposal",
                                     body_html="<p>Hello <a href='https://site.com'>site</a></p>", env=env, log=lambda *a: None)
        return out, sent["msg"]

    def test_a_tracked_new_send_carries_the_image_and_is_recorded(self):
        self.healthy()
        out, msg = self._send({})
        self.assertTrue(out["tracked"])
        self.assertEqual(msg["From"], "Your Name <casey@bluenet.org>")     # a name, not a bare address
        html = msg.get_body(("html",)).get_content()
        plain = msg.get_body(("plain",)).get_content()
        self.assertIn("t.bluenet.org/o/", html)
        self.assertNotIn("t.bluenet.org", plain)          # the plain part is untouched
        row = self.conn.execute("SELECT * FROM track_sends").fetchone()
        self.assertEqual((row["account"], row["thread_id"], row["message_id"]), (A, "", msg["Message-ID"]))
        self.assertEqual(json.loads(row["links"]), ["https://site.com"])

    def test_cc_only_is_enough_and_there_is_no_empty_to_line(self):
        # dev #73: "To shouldn't be required, if a CC or BCC is in place"
        import send_mail
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def send_message(self, msg): sent["msg"] = msg
        real = db.connect
        env = {**ENV, "GMAIL_ACCTONE_PASS": "x"}
        with mock.patch.object(send_mail.smtplib, "SMTP_SSL", FakeSMTP), \
                mock.patch.object(db, "connect", lambda *a, **k: real(self.path)), \
                mock.patch.dict(send_mail.ACCOUNTS, {A: ("GMAIL_ACCTONE_USER", "GMAIL_ACCTONE_PASS")}):
            send_mail.send_new(A, "Hi", "", "Note", cc="gina@team.org", env=env, log=lambda *a: None)
            self.assertIsNone(sent["msg"]["To"])
            self.assertEqual(sent["msg"]["Cc"], "gina@team.org")
            with self.assertRaises(ValueError):
                send_mail.send_new(A, "Hi", "", "Note", env=env, log=lambda *a: None)   # nobody at all

    def test_a_tracking_failure_still_sends_untracked(self):
        self.healthy()
        with mock.patch.object(T, "prepare", side_effect=RuntimeError("boom")):
            out, msg = self._send({})
        self.assertFalse(out["tracked"])
        self.assertNotIn("t.bluenet.org", msg.get_body(("html",)).get_content())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM track_sends").fetchone())


if __name__ == "__main__":
    unittest.main()
