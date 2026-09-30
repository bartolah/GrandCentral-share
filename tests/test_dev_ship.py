"""The dev worker's one way to ship (2026-09-29): sweeper/dev_ship.py.

It must refuse secrets, runtime data and the worker's own control files,
and take only the paths the worker may edit. Path checks only — no git.

    .venv/bin/python -m unittest tests.test_dev_ship -v
"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import dev_ship  # noqa: E402


class ShipPaths(unittest.TestCase):
    def test_the_worker_s_own_paths_are_allowed(self):
        for p in ("dashboard/index.html", "sweeper/mail_actions.py", "tests/test_x.py",
                  "integrations/mcp/gc_client.py", "ToDos.md", "./dashboard/release_notes.json"):
            dev_ship.check_path(p)

    def test_secrets_and_runtime_data_are_refused(self):
        for p in ("config/.env", "config/backups/.env.bak", "data/grand_central.db",
                  "logs/devworker.log", ".git/config", "../elsewhere.txt", ".env",
                  "dashboard/../config/.env", "/etc/passwd"):
            with self.assertRaises(ValueError, msg=p):
                dev_ship.check_path(p)

    def test_the_worker_s_control_files_are_refused(self):
        for p in ("sweeper/dev_worker.sh", "sweeper/dev_worker_settings.json",
                  "sweeper/dev_triage.py", "sweeper/dev_ship.py", "sweeper/send_mail.py",
                  "sweeper/connections.py"):
            with self.assertRaises(ValueError, msg=p):
                dev_ship.check_path(p)

    def test_everything_else_is_refused(self):
        for p in ("BUILD_SPEC.md", "CLAUDE.md", "config/com.x.plist", "design/a.png"):
            with self.assertRaises(ValueError, msg=p):
                dev_ship.check_path(p)

    def test_a_subject_and_files_are_required(self):
        with self.assertRaises(ValueError):
            dev_ship.ship("", "", ["README.md"], push=False)
        with self.assertRaises(ValueError):
            dev_ship.ship("E1: x", "", [], push=False)


if __name__ == "__main__":
    unittest.main()
