"""E259: setting up ConnectBlue's projects from a plan (sweeper/project_seed.py).

Bart asked to reorganize the pending ConnectBlue todos under no more than 10
projects. Automatic classification stays retired, so this is a reviewed,
one-off filing: a dry run writes nothing, --apply files only that account's
open todos that are in no project yet, a todo never leaves its account, and
running it twice changes nothing. Scratch SQLite and a scratch plan.

    .venv/bin/python -m unittest tests.test_project_seed -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import projects as P  # noqa: E402
import project_seed as S  # noqa: E402

A, B = "acctone", "accttwo"


def _plan(dirname: str, **over) -> str:
    os.makedirs(os.path.join(dirname, "projects"), exist_ok=True)
    with open(os.path.join(dirname, "acct.md"), "w") as f:
        f.write("# Account brief\nNever say exclusive.")
    with open(os.path.join(dirname, "projects", "amp.md"), "w") as f:
        f.write("Robin clears figures.")
    plan = {"account": A,
            "docs": [{"scope": "account", "title": "Brief", "file": "acct.md"}],
            "projects": [
                {"name": "Amendment", "keywords": ["amendment", "ios"], "exclude": ["farm bill"]},
                {"name": "Causes Amplify", "brief": "projects/amp.md",
                 "keywords": ["causes amplify", "amplify", "Robin"]},
                {"name": "Newsbot", "keywords": ["news ?bot"]},
                {"name": "Marketing", "broad": True, "keywords": ["newsletter", "post"]},
                {"name": "Sales", "broad": True, "keywords": ["proposal", "deck"]},
            ]}
    plan.update(over)
    path = os.path.join(dirname, "plan.json")
    with open(path, "w") as f:
        json.dump(plan, f)
    return path


class Seed(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "t.db"))
        self.plan = S.load_plan(_plan(self._dir.name))
        add = lambda text, acct=A: db.add_engagement(self.conn, text, account=acct)
        self.t = {
            "ios": add("Check the Amendment iOS rollout"),
            "Robin": add("Send Robin the Causes Amplify numbers"),
            "both": add("Newsbot post for Amendment"),          # 1 hit each: tie, both files
            "broad": add("Amplify newsletter"),                # specific beats broad on a tie
            "farm": add("Farm Bill amendment recap deck"),     # excluded from Amendment -> Sales
            "none": add("Call the dentist"),
            "other": add("Amendment call", B),                 # another account: never touched
            "done": add("Amendment wrap-up"),
            "filed": add("Newsbot scoring"),
        }
        db.set_engagement_state(self.conn, self.t["done"], "done")
        self.keep = P.create(self.conn, "Bart's own")
        P.add_item(self.conn, self.keep, "todo", None, str(self.t["filed"]))
        self.lines = []

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def run_seed(self, apply, **kw):
        return S.run(self.conn, self.plan, apply, out=self.lines.append, **kw)

    def test_a_dry_run_writes_nothing(self):
        before = self.conn.total_changes
        out = self.run_seed(False)
        self.assertEqual(self.conn.total_changes, before)
        self.assertEqual(out["filed"]["Amendment"], [self.t["ios"], self.t["both"]])
        self.assertEqual(out["filed"]["Causes Amplify"], [self.t["Robin"], self.t["broad"]])
        self.assertEqual(out["filed"]["Newsbot"], [self.t["both"]])
        self.assertEqual(out["filed"]["Sales"], [self.t["farm"]])
        self.assertEqual(out["filed"]["Marketing"], [])
        self.assertEqual(out["left"], [self.t["none"]])
        self.assertTrue(self.lines[0].startswith("DRY RUN"))

    def test_apply_files_only_this_accounts_open_unfiled_todos_and_is_idempotent(self):
        out = self.run_seed(True)
        ids = out["project_ids"]
        view = P.project_view(self.conn, ids["Amendment"])
        self.assertEqual(sorted(i["todo"]["id"] for i in view["items"]), sorted([self.t["ios"], self.t["both"]]))
        self.assertTrue(all(i["account"] == A for i in view["items"]))
        self.assertEqual(db.list_context_docs(self.conn, [f"account:{A}"])[0]["title"], "Brief")
        self.assertEqual(db.list_context_docs(self.conn, [f"project:{ids['Causes Amplify']}"])[0]["body"],
                         "Robin clears figures.")
        # Bart's own filing stands, and the other account is untouched
        self.assertEqual([p["id"] for p in P.projects_for_item(self.conn, "todo", A, self.t["filed"])], [self.keep])
        self.assertEqual(P.projects_for_item(self.conn, "todo", B, self.t["other"]), [])
        before = self.conn.total_changes
        again = self.run_seed(True)
        self.assertEqual(self.conn.total_changes, before)          # nothing new the second time
        self.assertEqual(again["project_ids"], ids)
        self.assertTrue(all(v == [] for v in again["filed"].values()))

    def test_move_and_skip_correct_a_line(self):
        out = self.run_seed(False, moves={self.t["none"]: "newsbot"}, skips={self.t["ios"]})
        self.assertEqual(out["filed"]["Newsbot"], [self.t["both"], self.t["none"]])
        self.assertNotIn(self.t["ios"], out["filed"]["Amendment"])
        with self.assertRaises(ValueError):
            self.run_seed(False, moves={self.t["none"]: "No such project"})

    def test_no_more_than_ten_projects(self):
        many = [{"name": f"P{i}", "keywords": ["x"]} for i in range(11)]
        with self.assertRaises(ValueError):
            S.load_plan(_plan(self._dir.name, projects=many))


PLAN = os.path.join(ROOT, "config", "seeds", "connectblue", "plan.json")


# config/seeds/ holds Bart's business briefs and stays out of the shareable
# copy (tools/make_shareable.py DROP_DIRS), so this check runs only at home.
@unittest.skipUnless(os.path.exists(PLAN), "no ConnectBlue seed plan in this copy")
class TheConnectBluePlan(unittest.TestCase):
    def test_it_loads_within_the_limit_and_keeps_pricing_out(self):
        plan = S.load_plan(os.path.join(ROOT, "config", "seeds", "connectblue", "plan.json"))
        self.assertEqual(plan["account"], "connectblue")
        self.assertLessEqual(len(plan["projects"]), 10)
        for name in ("Amendment", "Causes Amplify", "Northstar Labs", "Capitol Influence",
                     "Grand Central", "ConnectBlue Partner Platform", "NewsBot", "SalesDB"):
            self.assertIn(name, [p["name"] for p in plan["projects"]])
        texts = [d["body"] for d in plan["docs"]] + [p["brief_body"] for p in plan["projects"]]
        for t in texts:
            self.assertNotRegex(t, r"\$\d[^\n]{0,12}CPM")    # the internal rate card stays out
            self.assertNotIn("—", t)                # Bart: no em-dashes
        self.assertEqual(S.score(plan, "Send Robin the Causes Amplify deck")[0][0], "Causes Amplify")
        self.assertEqual(S.score(plan, "Farm Bill amendment recap for a client")[0][0], "Sales & Proposals")


if __name__ == "__main__":
    unittest.main()
