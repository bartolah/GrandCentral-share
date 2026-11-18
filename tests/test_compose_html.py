"""E313 — what the composer sends: paragraph spacing survives, and is never doubled.

Casey, 2026-11-16: a sent email showed "extra spaces" between paragraphs. Text
pasted from a document arrives as <p>…</p>\\n\\n<p>…</p>; E285's newline-to-<br>
step turned those layout newlines into blank lines on top of each paragraph's
own spacing. This runs the real rbHtml from dashboard/index.html in a
headless browser on four kinds of body and compares the rendered height of
what would be sent. Skipped when node + Playwright (or a browser) are absent.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIG = '<div class="gc-sig">--<br>Casey</div>'
CASES = {
    "typed": "<div>Team,</div><div><br></div><div>Getting started.</div><div><br></div><div>Thanks.</div>" + SIG,
    "plain": "Team,\n\nGetting started.\n\nThanks." + SIG,
    "doc": "<p>Team,</p>\n\n<p>Getting started.</p>\n\n<p>Thanks.</p>\n" + SIG,
    "divs": "<div>Team,\n\n</div><div>Getting started.\n\n</div><div>Thanks.</div>" + SIG,
    "pre": "<pre>a\nb</pre>" + SIG,
}

RUNNER = r"""
const [fn, cases] = [process.argv[2], JSON.parse(process.argv[3])];
let pw;
try { pw = require(require('child_process').execSync('npm root -g').toString().trim() + '/playwright'); }
catch (e) { console.log('SKIP no playwright'); process.exit(0); }
(async () => {
  let b;
  try { b = await pw.chromium.launch(); } catch (e) { console.log('SKIP no browser'); process.exit(0); }
  const p = await b.newPage(), out = {};
  for (const [name, html] of Object.entries(cases)) {
    out[name] = await p.evaluate(([fn, html]) => {
      document.body.innerHTML = '<div id="x" contenteditable style="white-space:pre-wrap"></div>';
      document.getElementById('x').innerHTML = html;
      window.$ = s => document.querySelector(s); eval(fn + '; window.rbHtml = rbHtml;');
      const sent = rbHtml('x');
      const f = document.createElement('div');
      f.style.cssText = 'width:600px;font:16px/1.15 Arial'; f.innerHTML = sent.replace(/<div class="gc-sig">[\s\S]*$/, '');
      document.body.appendChild(f);
      return {sent, height: Math.round(f.getBoundingClientRect().height)};
    }, [fn, html]);
  }
  console.log(JSON.stringify(out));
  await b.close();
})();
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class SentSpacing(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        fn = re.search(r"function rbHtml\(id\)\{.*?\n\}\n", src, re.S).group(0)
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(RUNNER)
            path = f.name
        try:
            res = subprocess.run(["node", path, fn, json.dumps(CASES)],
                                 capture_output=True, text=True, timeout=120)
        finally:
            os.unlink(path)
        line = (res.stdout.strip().splitlines() or [""])[-1]
        if line.startswith("SKIP") or not line.startswith("{"):
            raise unittest.SkipTest(line or res.stderr[-200:])
        cls.out = json.loads(line)

    def test_a_pasted_document_is_not_spaced_wider_than_typed_text(self):
        typed, doc = self.out["typed"]["height"], self.out["doc"]["height"]
        self.assertLessEqual(doc, typed * 1.25, (typed, doc, self.out["doc"]["sent"]))
        self.assertNotIn("</p><br>", self.out["doc"]["sent"])

    def test_typed_pasted_and_divided_text_keep_their_blank_lines(self):
        for name in ("typed", "plain", "divs"):
            self.assertGreaterEqual(self.out[name]["sent"].count("<br>"), 3, name)
        self.assertEqual(self.out["plain"]["height"], self.out["typed"]["height"])

    def test_preformatted_text_is_left_alone(self):
        self.assertIn("<pre>a\nb</pre>", self.out["pre"]["sent"])


if __name__ == "__main__":
    unittest.main()
