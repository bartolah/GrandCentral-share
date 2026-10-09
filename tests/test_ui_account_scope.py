"""The board's account-scoping predicates (dashboard/index.html), run under node.

Extracts the real "accounts" block from index.html — no copy to drift — and
asserts that an account tab only ever admits items of that account, that
Unassigned admits only ownerless items, and that Sent/Drafts follow the same
rule. Skipped when node is not installed.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HARNESS = r"""
const localStorage = {getItem: () => null, setItem() {}};
let MAIL = [], TODOS = [];
%BLOCK%
const assert = require('assert');
ACCOUNTS = ['personal', 'brightline', 'civic'];
assert.deepStrictEqual(acctIds(), ['personal', 'brightline', 'civic']);
const mk = a => ({account: a, project: a});
const items = [mk('personal'), mk('brightline'), mk('civic'), mk(null)];
const at = id => { ACCT = id; return items.filter(inAcct).map(i => i.account); };
assert.deepStrictEqual(at('personal'), ['personal']);
assert.deepStrictEqual(at('brightline'), ['brightline']);
assert.deepStrictEqual(at('civic'), ['civic']);
assert.deepStrictEqual(at('__all'), []);
ACCT = 'personal';
assert(mailAcctInAcct('personal') && !mailAcctInAcct('brightline'));
assert(!sentInAcct({account: 'civic'}) && sentInAcct({account: 'personal'}));
assert(inAcctId('personal') && !inAcctId('brightline') && !inAcctId(null));
assert.strictEqual(curAcct(), 'personal');
assert.strictEqual(acctLabel('consulting'), 'Consulting');
// todos may only join a todo of their own account (or an unassigned legacy one)
assert(todoFits({account: null}, 'civic') && todoFits({account: 'civic'}, 'civic'));
assert(!todoFits({account: 'personal'}, 'civic'));
"""


@unittest.skipUnless(shutil.which("node"), "node not installed")
class UiAccountScope(unittest.TestCase):
    def test_compact_skin_has_square_corners(self):
        html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        self.assertIn(
            'html[data-skin="compact"] * { border-radius: 0 !important; }',
            html,
        )

    def test_scoping_predicates(self):
        html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        block = html[html.index("/* ================= accounts (account-first"):html.index("async function setAcct(id){")]
        pick = html[html.index("const todoFits"):html.index("const acctOptions")]
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
            f.write(HARNESS.replace("%BLOCK%", block + "\n" + pick))
        try:
            out = subprocess.run(["node", f.name], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
        finally:
            os.unlink(f.name)

    def test_active_fetches_and_dev_intake_carry_the_selected_account(self):
        html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        self.assertIn("fetch('/mail?account='+q)", html)
        self.assertIn("fetch('/todos?account='+q)", html)
        self.assertIn("fetch('/dev?account='+encodeURIComponent(ACCT))", html)
        self.assertIn("post('/dev',{action:'add',text,account:ACCT}", html)
        self.assertIn("post('/dev',{action:'update',id,text,account:ACCT}", html)
        self.assertIn("post('/dev',{action:'update',id,status:'archived',account:ACCT}", html)

    def test_an_open_email_keeps_trash_and_more(self):
        # Casey, 2026-09-26: "Why do I keep losing the trash icon and more
        # menu on individual emails?" E205-E206 dropped the reading pane's ⋯
        # (paneMore stayed, orphaned) and nothing noticed. Pin both here.
        html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        start = html.index('<div class="pane-tools">')
        block = html[start:html.index("</div>", start)]
        self.assertIn("trashItem(", block)
        self.assertIn("paneMore(", block)
        # and nothing defines a menu that no button opens
        self.assertGreater(html.count("paneMore("), 1)

    def test_active_mail_rows_expose_the_triage_strip(self):
        # Six fast verbs + trash and "…" (Casey, dev #27, 2026-09-26).
        # Star replaced pin (2026-09-26). Label lives behind "…"; retired project/goal verbs never come back.
        html = open(os.path.join(ROOT, "dashboard", "index.html"), encoding="utf-8").read()
        start = html.index("// Bounded account triage: six fast verbs")
        block = html[start:html.index("const when =", start)]
        # Dev #62 (2026-10-01): one ✓ that archives in Gmail, no separate GC-only Handled.
        for action in ("replyOptions", "archiveItem", "snoozeDlg", "starItem",
                       "todoPick", "trashItem", "rowMore"):
            self.assertIn(action, block)
        for retired in ("labelDlg", "refileDlg", "tagGoal", "dismissItem", "handledItem"):
            self.assertNotIn(retired, block)


if __name__ == "__main__":
    unittest.main()
