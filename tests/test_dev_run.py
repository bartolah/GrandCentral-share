"""Dev #82 (§6 2026-11-09): run the dev queue from the app. Pins the guards
in sweeper/dev_run.py — one run at a time, the daily caps, no build over
uncommitted work — and that the route is a paired browser's alone (agent
gate + MCP client). Nothing here launches a process: subprocess is stubbed.

    .venv/bin/python -m unittest tests.test_dev_run -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
sys.path.insert(0, os.path.join(ROOT, "integrations", "mcp"))

import dev_run  # noqa: E402


class Guards(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        d = self._dir.name
        self.patches = [
            mock.patch.object(dev_run, "STATE", os.path.join(d, "runs.json")),
            mock.patch.object(dev_run, "MARKER", os.path.join(d, "marker.json")),
            mock.patch.object(dev_run, "LOG", os.path.join(d, "worker.log")),
            mock.patch.object(dev_run, "LOGS_DIR", d),
            mock.patch.object(dev_run, "DATA_DIR", d),
            mock.patch.object(dev_run, "running", return_value=False),
            mock.patch.object(dev_run, "_dirty", return_value=False),
            mock.patch.object(dev_run.subprocess, "Popen"),
            mock.patch.object(dev_run.subprocess, "run",
                              return_value=mock.Mock(returncode=0, stdout="", stderr="")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self._dir.cleanup()

    def test_build_kickstarts_the_scheduled_worker_and_leaves_a_marker(self):
        dev_run.start("build")
        args = dev_run.subprocess.run.call_args.args[0]
        self.assertEqual(args[:2], ["/bin/launchctl", "kickstart"])
        self.assertTrue(args[2].endswith("/com.example.grandcentral.devworker"))
        self.assertNotIn("-k", args)                    # never kills a run in progress
        self.assertTrue(os.path.exists(dev_run.MARKER))

    def test_triage_runs_only_the_triage_pass(self):
        dev_run.start("triage")
        self.assertIn("sweeper/dev_triage.py", dev_run.subprocess.Popen.call_args.args[0])
        self.assertFalse(os.path.exists(dev_run.MARKER))

    def test_one_at_a_time(self):
        dev_run.running.return_value = True
        with self.assertRaisesRegex(ValueError, "already going"):
            dev_run.start("build")

    def test_four_builds_a_day(self):
        for _ in range(dev_run.BUILD_PER_DAY):
            dev_run.start("build")
        with self.assertRaisesRegex(ValueError, "today's 4"):
            dev_run.start("build")
        dev_run.start("triage")                          # triage has its own allowance

    def test_never_over_uncommitted_work(self):
        dev_run._dirty.return_value = True
        with self.assertRaisesRegex(ValueError, "uncommitted"):
            dev_run.start("build")

    def test_unknown_mode(self):
        with self.assertRaises(ValueError):
            dev_run.start("deploy")

    def test_done_is_silent_after_a_scheduled_run(self):
        with mock.patch.dict(sys.modules, {"notify": mock.Mock()}) as mods:
            dev_run.done()                               # no marker: nothing sent
            mods["notify"]._deliver.assert_not_called()


class RunningMeansExecuting(unittest.TestCase):
    """A shell that merely mentions the script (a grep, an editor, a Claude
    session's command) must not read as a run in progress."""

    def _ps(self, lines):
        out = "\n".join(lines)
        return mock.patch.object(dev_run.subprocess, "run", return_value=mock.Mock(stdout=out))

    def test_the_worker_and_triage_count(self):
        with self._ps(["  101 /bin/bash /Users/you/Code/GrandCentral/sweeper/dev_worker.sh"]):
            self.assertTrue(dev_run.running())
        with self._ps(["  102 .venv/bin/python sweeper/dev_triage.py"]):
            self.assertTrue(dev_run.running())

    def test_a_mention_does_not(self):
        with self._ps(["  103 /bin/zsh -c grep -n x sweeper/dev_worker.sh",
                       "  104 /usr/bin/vim sweeper/dev_triage.py",
                       "  105 .venv/bin/python sweeper/dev_run.py"]):
            self.assertFalse(dev_run.running())


class OnlyAPairedBrowser(unittest.TestCase):
    def test_agents_can_never_reach_it(self):
        import agent_gate
        import gc_client
        self.assertIn("/dev/run", agent_gate.HUMAN_ONLY_POST)
        self.assertIn(("*", "/dev/run"), gc_client.DENIED)
        self.assertNotIn("/dev/run", getattr(agent_gate, "AGENT_POST", {}))

    def test_the_worker_cannot_edit_or_ship_it(self):
        import json
        import dev_ship
        self.assertIn("sweeper/dev_run.py", dev_ship.LOCKED)
        with open(os.path.join(ROOT, "sweeper", "dev_worker_settings.json")) as f:
            self.assertIn("Edit(./sweeper/dev_run.py)", json.load(f)["permissions"]["deny"])


if __name__ == "__main__":
    unittest.main()
