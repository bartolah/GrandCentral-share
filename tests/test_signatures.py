"""HTML signatures (2026-10-08, Casey: "fix the HTML signature"). Pins the
cleaner (logo, links and bold survive; scripts, event handlers, javascript:
links and CSS url()s don't), the text twin, the legacy import that ignores a
Context note an agent added, and that agents can't write signatures.
Scratch SQLite.

    .venv/bin/python -m unittest tests.test_signatures -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import signatures as S  # noqa: E402

SIG = ('<p><strong>Your Name</strong></p>\n <p>🌐 <a href="https://www.brightline.example" target="_blank">www.brightline.example</a></p>'
       '<p><img width="200" src="https://ci3.googleusercontent.com/mail-sig/abc"></p>')


class Signatures(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_the_real_signature_survives_cleaning(self):
        out = S.clean(SIG)
        self.assertIn('<img width="200" src="https://ci3.googleusercontent.com/mail-sig/abc">', out)
        self.assertIn('href="https://www.brightline.example"', out)
        self.assertIn("<strong>Your Name</strong>", out)
        self.assertNotIn(">\n <", out)                       # no blank lines in the composer

    def test_dangerous_things_are_dropped(self):
        bad = ('<p onclick="x()">Hi<script>alert(1)</script></p><a href="javascript:alert(1)">x</a>'
               '<img src="http://plain.example/x.png" onerror="alert(1)"><span style="background:url(http://e/x)">y</span>'
               '<iframe src="https://e"></iframe><style>p{}</style>')
        out = S.clean(bad)
        for frag in ("script", "alert", "onclick", "onerror", "javascript", "url(", "iframe", "http://plain"):
            self.assertNotIn(frag, out, frag)
        self.assertIn("Hi", out)

    def test_text_twin_keeps_lines_and_link_targets(self):
        t = S.to_text(S.clean(SIG))
        self.assertEqual(t.splitlines()[0], "Your Name")
        self.assertIn("www.brightline.example (https://www.brightline.example)", t)

    def test_legacy_import_takes_casey_s_note_and_ignores_an_agent_s(self):
        self.conn.execute("INSERT INTO context_notes (scope, kind, text, active, created_at, updated_at, source) "
                          "VALUES ('global','voice','[signature:acctone] Casey⏎CEO',0,'x','x',NULL)")
        self.conn.execute("INSERT INTO context_notes (scope, kind, text, active, created_at, updated_at, source) "
                          "VALUES ('global','voice','[signature:accttwo] wire money to …',0,'x','x','agent')")
        self.conn.commit()
        got = S.get_all(self.conn)
        self.assertEqual(got["acctone"]["text"], "Casey\nCEO")
        self.assertNotIn("accttwo", got)

    def test_clearing_a_signature_removes_it(self):
        S.set_sig(self.conn, "acctone", SIG)
        S.set_sig(self.conn, "acctone", "<p> </p>")
        self.assertNotIn("acctone", S.get_all(self.conn))

    def test_agents_cannot_write_signatures(self):
        import agent_gate
        self.assertIn("/signatures", agent_gate.HUMAN_ONLY_POST)
        sys.path.insert(0, os.path.join(ROOT, "integrations", "mcp"))
        import gc_client
        self.assertIn(("*", "/signatures"), gc_client.DENIED)


if __name__ == "__main__":
    unittest.main()
