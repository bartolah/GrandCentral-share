"""Account-first tests for the sweep path, dismissals, the command bar, and the
"retired concepts stay retired" guards (slice 2, 2026-09-24).

Offline and isolated: scratch SQLite files only, a fake Anthropic client, fake
account slugs ("acctone"/"accttwo") that exist in no config/.env. Nothing here
touches the live DB, Gmail, the network, or grand_central.json.

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
import tempfile
import unittest
from functools import partial
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

import assess  # noqa: E402
import build_state as bs  # noqa: E402
import db  # noqa: E402

def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


A, B = "acctone", "accttwo"
TID = "1800000000000000001"            # the SAME Gmail thread id in both accounts
_real_connect = db.connect


def _rec(account, subject, thread_id=TID, direction="inbound", age=5, **kw):
    r = {"account": account, "account_email": f"{account}@example.com",
         "thread_id": thread_id, "subject": subject, "last_from": "Pat <pat@example.org>",
         "last_from_email": "pat@example.org", "last_direction": direction,
         "participants": ["Pat <pat@example.org>", f"{account}@example.com"],
         "snippet": f"snippet for {subject}", "age_days": age, "source": "gmail"}
    r.update(kw)
    return r


class FakeAnthropic:
    """Answers each assess prompt from its own thread list and records the
    prompts so a test can see exactly what the 'model' was shown."""

    def __init__(self, garble_first=False):
        self.prompts = []
        self.garble_first = garble_first
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        user = kw["messages"][0]["content"]
        self.prompts.append(user)
        if self.garble_first and len(self.prompts) == 1:
            text = "not json at all"
        else:
            body = user[user.index("Threads (JSON):") + len("Threads (JSON):"):user.index("Respond with ONLY")]
            rows = [{"n": t["n"], "summary": "sum:" + t["subject"], "reply_needed": True,
                     "importance": 3 if "urgent" in t["subject"] else 1}
                    for t in json.loads(body)]
            text = json.dumps(rows)[1:]          # the caller prefixes the "[" prefill
        return SimpleNamespace(usage=SimpleNamespace(input_tokens=1, output_tokens=1),
                               content=[SimpleNamespace(type="text", text=text)])


class AssessIsolation(unittest.TestCase):
    def test_prompts_never_mix_accounts_and_results_are_keyed_by_account(self):
        fake = FakeAnthropic()
        recs = [_rec(A, "SECRET-A urgent"), _rec(B, "SECRET-B routine")]
        out = assess.assess_threads(recs, {}, log=lambda *_: None, client=fake)
        self.assertEqual(len(fake.prompts), 2)
        for p in fake.prompts:
            self.assertFalse("SECRET-A" in p and "SECRET-B" in p, "one prompt saw both mailboxes")
        # the same thread id in two accounts never collides
        self.assertEqual(out[(A, TID)]["summary"], "sum:SECRET-A urgent")
        self.assertEqual(out[(B, TID)]["summary"], "sum:SECRET-B routine")
        self.assertEqual(out[(A, TID)]["importance"], 3)
        self.assertEqual(out[(B, TID)]["importance"], 1)

    def test_prompt_carries_no_registry_or_project_vocabulary(self):
        fake = FakeAnthropic()
        assess.assess_threads([_rec(A, "hello")], {}, log=lambda *_: None, client=fake)
        blob = (assess.SYSTEM_PROMPT + fake.prompts[0]).lower()
        for word in ("operation", "theater", "objective", "project", "registry"):
            self.assertNotIn(word, blob)

    def test_unparsable_batch_is_skipped_not_guessed(self):
        fake = FakeAnthropic(garble_first=True)
        recs = [_rec(A, "first"), _rec(B, "second")]
        out = assess.assess_threads(recs, {}, log=lambda *_: None, client=fake)
        # exactly one account survived; the other simply has no entry
        self.assertEqual(len(out), 1)

    def test_normalize_clamps_model_output(self):
        self.assertEqual(assess.normalize({"importance": 99})["importance"], 3)
        self.assertEqual(assess.normalize({"importance": "x"})["importance"], 1)
        self.assertTrue(assess.normalize({})["reply_needed"])   # fail open


class BuildStateIsolation(unittest.TestCase):
    def _state(self, records, assessed=None, dismissed=None, **kw):
        return bs.build_state(records, assessed or {}, {}, generated_at="2026-09-24T12:00:00Z",
                              next_sweep_at="2026-09-24T13:00:00Z", sources_swept=[f"gmail:{A}", f"gmail:{B}"],
                              dismissed=dismissed, log=lambda *_: None, **kw)

    def test_dismissing_in_one_account_does_not_hide_the_same_thread_in_another(self):
        recs = [_rec(A, "in A"), _rec(B, "in B")]
        st = self._state(recs, dismissed={(A, TID)})
        self.assertEqual([d["account"] for d in st["reply_debt"]], [B])
        self.assertEqual([d["account"] for d in st["top5"]], [B])

    def test_waiting_on_respects_account_scoped_dismissals(self):
        recs = [_rec(A, "ask A", direction="outbound", age=5),
                _rec(B, "ask B", direction="outbound", age=5)]
        st = self._state(recs, dismissed={(B, TID)})
        self.assertEqual([w["account"] for w in st["waiting_on"]], [A])

    def test_assessment_and_category_are_looked_up_per_account(self):
        recs = [_rec(A, "in A"), _rec(B, "in B")]
        assessed = {(A, TID): {"summary": "A summary", "reply_needed": True, "importance": 3},
                    (B, TID): {"summary": "B summary", "reply_needed": False, "importance": 0}}
        st = self._state(recs, assessed, mail_categories={(B, TID): "newsletter"})
        by_acct = {d["account"]: d for d in st["reply_debt"]}
        self.assertEqual(list(by_acct), [A])           # B's newsletter category suppressed only B
        self.assertEqual(by_acct[A]["what"], "A summary")
        self.assertEqual(by_acct[A]["importance"], 3)

    def test_reply_needed_false_only_excludes_its_own_accounts_thread(self):
        recs = [_rec(A, "in A"), _rec(B, "in B")]
        assessed = {(A, TID): {"summary": "a", "reply_needed": False, "importance": 3}}
        st = self._state(recs, assessed)
        self.assertEqual([t["account"] for t in st["top5"]], [B])   # A excluded, B unaffected
        self.assertEqual(len(st["reply_debt"]), 2)

    def test_top5_diversity_cap_is_per_account_and_rows_carry_their_account(self):
        recs = [_rec(A, f"a{i}", thread_id=str(100 + i)) for i in range(4)] + [_rec(B, "b0", thread_id="200")]
        st = self._state(recs)
        accounts = [t["account"] for t in st["top5"]]
        self.assertLessEqual(accounts.count(A), 2)
        self.assertIn(B, accounts)
        self.assertTrue(all(t["account"] in (A, B) for t in st["top5_pool"]))

    def test_board_has_no_project_goal_pipeline_or_unassigned_keys(self):
        st = self._state([_rec(A, "x")])
        for key in ("theaters", "operations", "objectives", "unassigned", "rule_suggestions",
                    "pipeline", "tasks_unfiled"):
            self.assertNotIn(key, st)
        for row in st["reply_debt"] + st["top5"] + st["waiting_on"]:
            self.assertNotIn("project", row)
            self.assertNotIn("operation", row)
        for key in ("stale_projects", "deals_advancing", "unassigned"):
            self.assertNotIn(key, st["summary"])


class DismissalScoping(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def test_dismissal_applies_only_to_its_own_account(self):
        db.record_dismissal(self.conn, {"thread_id": TID, "account": A})
        self.assertTrue(db.is_dismissed(self.conn, A, TID))
        self.assertFalse(db.is_dismissed(self.conn, B, TID))
        keys = db.dismissed_keys(self.conn, [_rec(A, "a"), _rec(B, "b")])
        self.assertEqual(keys, {(A, TID)})

    def test_legacy_dismissal_without_account_still_matches(self):
        db.record_dismissal(self.conn, {"thread_id": TID, "account": ""})
        self.assertTrue(db.is_dismissed(self.conn, A, TID))
        self.assertTrue(db.is_dismissed(self.conn, B, TID))

    def test_snoozes_are_keyed_by_account(self):
        db.set_mail_triage(self.conn, TID, A, {"snoozed_until": "2999-01-01T00:00:00Z"})
        self.assertEqual(db.snoozed_keys(self.conn), {(A, TID)})

    def test_record_dismissed_requires_a_valid_account(self):
        import feedback
        with mock.patch.object(db, "connect", partial(_real_connect, self.path)):
            for bad in (None, "", "two words", "../x"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    feedback.record_dismissed({"thread_id": TID, "account": bad})
            with self.assertRaises(ValueError):
                feedback.record_dismissed({"thread_id": "abc", "account": A})
        with mock.patch.object(db, "connect", partial(_real_connect, self.path)):
            feedback.record_dismissed({"thread_id": TID, "account": A})
        self.assertTrue(db.is_dismissed(self.conn, A, TID))
        self.assertFalse(db.is_dismissed(self.conn, B, TID))

    def test_top5_resolution_is_account_scoped(self):
        import serve
        db.record_dismissal(self.conn, {"thread_id": TID, "account": B})
        gen, now = "2026-09-24T12:00:00Z", "2026-09-24T13:00:00Z"
        self.assertTrue(serve._top5_resolved(self.conn, {"thread_id": TID, "account": B}, gen, now))
        self.assertFalse(serve._top5_resolved(self.conn, {"thread_id": TID, "account": A}, gen, now))
        # done in B never resolves A's copy either
        db.set_mail_triage(self.conn, "77", B, {"done_at": now})
        self.assertTrue(serve._top5_resolved(self.conn, {"thread_id": "77", "account": B}, gen, now))
        self.assertFalse(serve._top5_resolved(self.conn, {"thread_id": "77", "account": A}, gen, now))


class CommandBarIsAccountScoped(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        conn = db.connect(self.path)
        db.add_engagement(conn, "todo in A", account=A)
        db.add_engagement(conn, "todo in B", account=B)
        for acct in (A, B):
            conn.execute(
                "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
                "last_from_email, last_direction, snippet, last_date, in_inbox, synced_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (TID, acct, f"mail in {acct}", "[]", "S", "s@example.com", "inbound", "",
                 "2026-09-24T12:00:00Z", 1, "2026-09-24T12:00:00Z"))
        conn.commit()
        conn.close()
        import command
        self.command = command
        self.patch = mock.patch.object(db, "connect", partial(_real_connect, self.path))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self._dir.cleanup()

    def test_query_todos_and_mail_scope_by_account(self):
        out = self.command._run_tool("query_todos", {"account": A}, {})
        self.assertEqual([t["text"] for t in out["todos"]], ["todo in A"])
        out = self.command._run_tool("query_mail", {"account": B}, {})
        self.assertEqual([t["subject"] for t in out["threads"]], ["mail in accttwo"])
        self.assertEqual(len(self.command._run_tool("query_mail", {}, {})["threads"]), 2)

    def test_bad_account_is_rejected_and_project_tools_are_gone(self):
        with self.assertRaises(ValueError):
            self.command._run_tool("query_todos", {"account": "../x"}, {})
        names = {t["name"] for t in self.command.TOOLS} if hasattr(self.command, "TOOLS") else set()
        for gone in ("reclassify", "query_bizdev", "refile", "tag_thread"):
            self.assertNotIn(gone, names)

    def test_add_task_from_the_command_bar_requires_an_account(self):
        with self.assertRaises(ValueError):
            self.command._run_tool("add_task", {"text": "orphan"}, {})


class WizardIsAccountFirst(unittest.TestCase):
    def test_suggestions_have_no_project_and_meeting_ones_need_an_account(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "t.db"))
            self.assertNotIn("project", db.add_wizard_suggestion.__code__.co_varnames)
            sid = db.add_wizard_suggestion(conn, "granola", "follow up", meeting_ref="m1")
            with self.assertRaises(ValueError):
                db.wizard_decide(conn, sid, approve=True)        # no account anywhere
            out = db.wizard_decide(conn, sid, approve=True, account=A)
            row = conn.execute("SELECT account FROM engagements WHERE id=?", (out["todo_id"],)).fetchone()
            self.assertEqual(row["account"], A)
            conn.close()


# --------------------------------------------------------------------------
# Retired concepts stay retired (static guards over the runtime source)
# --------------------------------------------------------------------------
RUNTIME_PY = [p for p in (glob.glob(os.path.join(ROOT, "sweeper", "*.py"))
                          + glob.glob(os.path.join(ROOT, "sweeper", "sources", "*.py"))
                          + glob.glob(os.path.join(ROOT, "integrations", "mcp", "*.py"))
                          + [os.path.join(ROOT, "dashboard", "serve.py")])
              if os.path.basename(p) != "selftest.py"]
RETIRED_TABLES = ("theaters", "operations", "objectives", "operation_theaters",
                  "operation_objectives", "mappings", "corrections", "element_tags",
                  "op_notes", "metric_history", "entity_notes", "entity_progress")


class RetiredConceptsStayRetired(unittest.TestCase):
    def test_retired_modules_are_deleted(self):
        for rel in ("sweeper/classify.py", "sweeper/mapping.py", "sweeper/registry_edit.py",
                    "sweeper/migrate_workstreams.py", "sweeper/backfill_inbox.py",
                    "sweeper/bizdev.py", "sweeper/sources/attio_reader.py",
                    "grand_central_dashboard.html"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, rel)), rel)

    def test_no_runtime_module_imports_retired_code(self):
        pat = re.compile(r"^\s*(?:from|import)\s+(?:\.|sources\.)?(classify|mapping|registry_edit|"
                         r"migrate_workstreams|backfill_inbox|bizdev|attio_reader)\b", re.M)
        for path in RUNTIME_PY:
            self.assertIsNone(pat.search(_read(path)), path)

    def test_no_runtime_sql_touches_retired_tables(self):
        pat = re.compile(r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(" + "|".join(RETIRED_TABLES) + r")\b")
        for path in RUNTIME_PY:
            hit = pat.search(_read(path))
            self.assertIsNone(hit, f"{path}: {hit and hit.group(0)}")

    def test_no_runtime_python_reads_attio(self):
        for path in RUNTIME_PY:
            src = _read(path).lower()
            self.assertNotIn("attio_api_key", src, path)
            self.assertNotIn("api.attio.com", src, path)

    def test_the_sweep_wires_account_scoped_pieces_only(self):
        src = _read(os.path.join(ROOT, "sweeper", "sweep.py"))
        for needle in ("assess_threads", "dismissed_keys", "snoozed_keys"):
            self.assertIn(needle, src)
        for banned in ("sync_registry", "classify_threads", "mapping_engine", "load_overrides",
                       "snapshot_metrics", "op_notes", "tags_for_threads", "attio"):
            self.assertNotIn(banned, src)

    def test_old_dashboard_is_gone_and_settings_replaces_its_only_live_use(self):
        for name in ("classic.html", "mail.js", "todos.js", "sample.json"):
            self.assertFalse(os.path.exists(os.path.join(ROOT, "dashboard", name)), name)
        self.assertTrue(os.path.exists(os.path.join(ROOT, "dashboard", "settings.html")))
        for name in ("index.html", "sw.js", "serve.py"):
            src = _read(os.path.join(ROOT, "dashboard", name))
            for dead in ("classic.html", "/mail.js", "/todos.js", "sample.json"):
                self.assertNotIn(dead, src, f"{name} still mentions {dead}")
        self.assertIn("/settings.html", _read(os.path.join(ROOT, "dashboard", "index.html")))
        # every file the service worker precaches must exist
        shell = re.search(r"const SHELL = \[(.*?)\];", _read(os.path.join(ROOT, "dashboard", "sw.js")), re.S).group(1)
        for path in re.findall(r"'(/[^']*)'", shell):
            if path != "/":
                self.assertTrue(os.path.exists(os.path.join(ROOT, "dashboard", path.lstrip("/"))), path)

    def test_settings_page_offers_only_connections_and_context(self):
        src = _read(os.path.join(ROOT, "dashboard", "settings.html"))
        self.assertIn("renderConnectionsTab", src)
        self.assertIn("renderContextTab", src)
        for word in ("attio", "theater", "objective", "registry", "operation"):
            self.assertNotIn(word, src.lower())

    def test_dashboard_no_longer_serves_the_old_board_shell_entry_points(self):
        src = _read(os.path.join(ROOT, "dashboard", "serve.py"))
        for route in ('"/scratch"', '"/progress"', '"/bizdev"', '"/dash"', '"/registry"'):
            self.assertIn(route, src)   # present only in the 410 sets
        self.assertNotIn("import registry_edit", src)
        self.assertNotIn("import bizdev", src)


if __name__ == "__main__":
    unittest.main()
