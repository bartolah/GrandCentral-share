"""Every inline onclick in settings.html must reach a function on window.

settings.html keeps its code inside one closure and hands the handlers its
markup calls to window by name. E260 added Approve/Decline for desktop-app
requests (agentDecide) without adding it to that list, so the buttons did
nothing (Casey, 2026-10-02: "I tried to approve an Agent action … and nothing
happened"). This catches the next one offline.
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SettingsHandlersAreExported(unittest.TestCase):
    def test_every_onclick_handler_is_on_window(self):
        html = open(os.path.join(ROOT, "dashboard", "settings.html"), encoding="utf-8").read()
        called = set(re.findall(r'on(?:click|change|input)="([A-Za-z_$][\w$]*)\(', html))
        m = re.search(r"Object\.assign\(window,\s*\{([^}]*)\}", html)
        self.assertIsNotNone(m, "settings.html no longer exports its handlers to window")
        exported = {n.strip() for n in m.group(1).split(",") if n.strip()}
        self.assertIn("agentDecide", called)
        self.assertEqual(sorted(called - exported), [],
                         "onclick handlers in settings.html that are not on window")


if __name__ == "__main__":
    unittest.main()
