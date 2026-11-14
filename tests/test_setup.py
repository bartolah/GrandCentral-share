"""E312 — the command-line setup (tools/setup.py) and the settings it writes.

Casey, 2026-11-14: "Command line setup is great." What must hold: every write
goes through sweeper/connections.py (mode 600, backed up), a credential is
tested before it is saved and never printed, a failed test saves nothing,
the wizard may write only its narrow list of plain settings, and the new
per-account calendars, calendar-write accounts and tracking default fall
back to today's behaviour when unset. Offline: a scratch config dir, no
network, scripted answers.
"""

from __future__ import annotations

import io
import os
import stat
import sys
import tempfile
import unittest
from datetime import date
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import common  # noqa: E402
import connections  # noqa: E402
from sources import gmail_reader  # noqa: E402

PW = "abcdabcdabcdabcd"


class Scripted:
    """A Console that answers from a list and records what it showed."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.out = io.StringIO()

    def _next(self):
        return self.answers.pop(0) if self.answers else ""

    def say(self, text=""):
        self.out.write(text + "\n")

    def ask(self, prompt, default=""):
        self.out.write(prompt + "\n")
        return self._next() or default

    def secret(self, prompt):
        self.out.write(prompt + "\n")
        return self._next()

    def yes(self, prompt, default=False):
        got = self.ask(prompt).lower()
        return default if not got else got.startswith("y")


class Wizard(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.env_path = os.path.join(d, ".env")
        real_load = common.load_env
        load = lambda path=None: real_load(path or self.env_path)  # noqa: E731
        self._p = [mock.patch.object(connections, "ENV_PATH", self.env_path),
                   mock.patch.object(connections, "BACKUP_DIR", os.path.join(d, "backups")),
                   mock.patch.object(connections, "load_env", load),
                   mock.patch.object(gmail_reader, "load_env", load)]
        for p in self._p:
            p.start()
        import setup
        self.setup = setup

    def tearDown(self):
        for p in self._p:
            p.stop()
        gmail_reader.refresh_accounts({})
        self._dir.cleanup()

    def env(self):
        return common.load_env(self.env_path)

    def test_an_account_is_tested_then_saved_privately_and_never_echoed(self):
        ui = Scripted(["y", "jo@example.com", "work", PW, "n"])
        with mock.patch.object(connections, "test_imap", return_value=(True, "signed in")) as t:
            self.setup.step_accounts(ui)
        t.assert_called_once_with("jo@example.com", PW)
        env = self.env()
        self.assertEqual((env["GMAIL_WORK_USER"], env["GMAIL_WORK_APP_PASSWORD"]), ("jo@example.com", PW))
        self.assertEqual(stat.S_IMODE(os.stat(self.env_path).st_mode), 0o600)
        self.assertNotIn(PW, ui.out.getvalue())

    def test_a_failed_login_saves_nothing(self):
        ui = Scripted(["y", "jo@example.com", "work", "wrong", "n"])
        with mock.patch.object(connections, "test_imap", return_value=(False, "login rejected")):
            self.setup.step_accounts(ui)
        self.assertNotIn("GMAIL_WORK_USER", self.env())

    def test_calendars_are_tied_to_their_account(self):
        connections._write_env({"GMAIL_WORK_USER": "jo@example.com", "GMAIL_WORK_APP_PASSWORD": PW})
        ui = Scripted(["https://calendar.example/work.ics"])
        with mock.patch.object(self.setup, "_test_ics", return_value=(True, "read it")):
            self.setup.step_calendars(ui)
        self.assertEqual(self.env()["GCAL_ICS_WORK"], "https://calendar.example/work.ics")
        self.assertNotIn("work.ics", ui.out.getvalue())     # a secret address is never shown

    def test_the_wizard_writes_only_its_own_settings(self):
        for key in ("ANTHROPIC_API_KEY", "GMAIL_WORK_APP_PASSWORD", "PATH", "GC_PORT"):
            with self.assertRaises(ValueError):
                connections.set_value(key, "x")
        with self.assertRaises(ValueError):
            connections.set_value("SENDER_NAME", "two\nlines")
        connections.set_value("SENDER_NAME", "Jo Example")
        self.assertEqual(self.env()["SENDER_NAME"], "Jo Example")

    def test_check_is_ready_only_with_a_mail_account(self):
        ui = Scripted([])
        self.assertEqual(self.setup.check(ui), 1)
        self.assertIn("Mail accounts", ui.out.getvalue())
        connections._write_env({"GMAIL_WORK_USER": "jo@example.com", "GMAIL_WORK_APP_PASSWORD": PW})
        self.assertEqual(self.setup.check(Scripted([])), 0)

    def test_an_existing_job_under_any_name_is_never_doubled(self):
        d = os.path.join(self._dir.name, "LaunchAgents")
        os.makedirs(d)
        open(os.path.join(d, "com.someoneelse.grandcentral.mailsync.plist"), "w").close()
        self.assertTrue(self.setup.installed_job("mailsync", d).endswith("someoneelse.grandcentral.mailsync.plist"))
        self.assertIsNone(self.setup.installed_job("dashboard", d))

    def test_launchd_files_point_at_this_checkout_and_user(self):
        src = ("<key>Label</key><string>com.example.grandcentral.mailsync</string>"
               "<string>/Users/you/Code/GrandCentral/.venv/bin/python</string>"
               "<key>WorkingDirectory</key>\n    <string>/Users/you/Code/GrandCentral</string>")
        out = self.setup.render_plist(src, "/opt/gc", "com.jo.grandcentral.")
        self.assertIn("com.jo.grandcentral.mailsync", out)
        self.assertIn("/opt/gc/.venv/bin/python", out)
        self.assertNotIn("/Users/you", out)


class ConfigurableSettings(unittest.TestCase):
    def test_per_account_calendars_come_first_and_the_old_list_still_works(self):
        from sources import calendar_reader as cr
        env = {"GCAL_ICS_URLS": "https://c/a.ics,https://c/b.ics", "GCAL_ICS_WORK": "https://c/b.ics,https://c/w.ics"}
        self.assertEqual(cr.calendar_feeds(env),
                         [("https://c/b.ics", "work"), ("https://c/w.ics", "work"), ("https://c/a.ics", None)])
        self.assertEqual(cr.calendar_feeds({"GCAL_ICS_URLS": "https://c/a.ics"}), [("https://c/a.ics", None)])

    def test_an_account_calendar_labels_its_events_with_the_account(self):
        from sources import calendar_reader as cr
        day = date.today()
        ics = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Anything\r\nBEGIN:VEVENT\r\nUID:1\r\n"
               f"DTSTART:{day:%Y%m%d}T150000\r\nDTEND:{day:%Y%m%d}T160000\r\nSUMMARY:Standup\r\n"
               "END:VEVENT\r\nEND:VCALENDAR\r\n").encode()
        resp = mock.Mock(content=ics)
        with mock.patch.object(cr.requests, "get", return_value=resp):
            out = cr.read_range({"GCAL_ICS_STUDIO": "https://c/s.ics"}, day, 1, log=lambda *_: None)
        self.assertEqual([e["cal"] for e in out[0]["events"]], ["studio"])

    def test_any_configured_account_may_create_on_its_own_calendar(self):
        import calendar_writer as cw
        env = {"GMAIL_STUDIO_USER": "s@example.com"}
        self.assertEqual(cw.email_key("studio", env), "GMAIL_STUDIO_USER")
        self.assertIsNone(cw.email_key("nowhere", env))
        self.assertIsNone(cw.email_key("../x", {"GMAIL_../X_USER": "a@b"}))
        self.assertEqual(cw.email_key("personal", {}), "GMAIL_PERSONAL_USER")   # the old table still wins

    def test_tracking_default_can_be_set_and_falls_back(self):
        import tracking
        with mock.patch("common.load_env", return_value={}):
            self.assertEqual(tracking.default_on(), tracking.DEFAULT_ON)
        with mock.patch("common.load_env", return_value={"TRACK_DEFAULT_ON": "Work, studio"}):
            self.assertEqual(tracking.default_on(), {"work", "studio"})
        with mock.patch("common.load_env", return_value={"TRACK_DEFAULT_ON": ""}):
            self.assertEqual(tracking.default_on(), set())


if __name__ == "__main__":
    unittest.main()
