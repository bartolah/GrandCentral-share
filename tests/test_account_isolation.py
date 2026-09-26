"""Account-first isolation tests (2026-09-24).

The product rule: an account is the ownership boundary. Emails are only ever
seen through their own account; a todo lives in exactly one account and only
ever holds that account's emails.

Everything runs against a scratch SQLite file (or a scratch server on a
scratch port with a scratch DB) — never the live data/grand_central.db, never
Gmail. Account slugs are fake on purpose ("acctone"/"accttwo"): they are in no
config/.env, so no code path here can reach a real mailbox.

    .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402
import todo_labels  # noqa: E402

A, B = "acctone", "accttwo"
SAME_THREAD = "1800000000000000001"   # the SAME thread id in both accounts


def _seed_mail(conn, account, thread_id, subject, in_inbox=1):
    conn.execute(
        "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
        "last_from_email, last_direction, snippet, last_date, in_inbox, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, account, subject, "[]", "Sender", "s@example.com", "inbound",
         "snip", "2026-09-24T12:00:00Z", in_inbox, "2026-09-24T12:00:00Z"))
    conn.commit()


class DbCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._dir.name, "t.db")
        self.conn = db.connect(self.path)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()


class TodoAccountRules(DbCase):
    def test_todo_requires_an_account(self):
        with self.assertRaises(ValueError):
            db.add_engagement(self.conn, "no owner")

    def test_todo_account_derived_from_email_ref(self):
        tid = db.add_engagement(self.conn, "from mail", source_ref=f"{A}:{SAME_THREAD}")
        row = self.conn.execute("SELECT account FROM engagements WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["account"], A)

    def test_todo_cannot_contradict_its_email(self):
        with self.assertRaises(ValueError):
            db.add_engagement(self.conn, "x", account=B, source_ref=f"{A}:{SAME_THREAD}")

    def test_bad_account_slug_rejected(self):
        for bad in ("", "  ", "A B", "../x", "x" * 41):
            with self.assertRaises(ValueError, msg=repr(bad)):
                db.add_engagement(self.conn, "x", account=bad)

    def test_dedupe_is_per_account(self):
        a = db.add_engagement(self.conn, "same text", account=A, source="mstodo", dedupe=True)
        b = db.add_engagement(self.conn, "same text", account=B, source="mstodo", dedupe=True)
        again = db.add_engagement(self.conn, "same text", account=A, source="mstodo", dedupe=True)
        self.assertTrue(a and b)
        self.assertIsNone(again)

    def test_recurrence_keeps_the_account(self):
        tid = db.add_engagement(self.conn, "weekly", account=B, due="2026-09-24", recur="weekly")
        out = db.complete_with_recurrence(self.conn, [tid], "done")
        nxt = self.conn.execute("SELECT account FROM engagements WHERE id=?",
                                (out["spawned"][0]["id"],)).fetchone()
        self.assertEqual(nxt["account"], B)


class EmailTodoBoundary(DbCase):
    def test_cannot_link_another_accounts_email(self):
        tid = db.add_engagement(self.conn, "todo in A", account=A)
        with self.assertRaises(ValueError):
            db.link_thread(self.conn, tid, B, SAME_THREAD)
        n = self.conn.execute("SELECT COUNT(*) c FROM engagement_threads").fetchone()["c"]
        self.assertEqual(n, 0)

    def test_same_account_link_works_and_is_idempotent(self):
        tid = db.add_engagement(self.conn, "todo in A", account=A)
        self.assertTrue(db.link_thread(self.conn, tid, A, SAME_THREAD))
        self.assertTrue(db.link_thread(self.conn, tid, A, SAME_THREAD))
        n = self.conn.execute("SELECT COUNT(*) c FROM engagement_threads").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_same_thread_id_in_two_accounts_are_separate_todos(self):
        ta = db.add_engagement(self.conn, "A", account=A)
        tb = db.add_engagement(self.conn, "B", account=B)
        db.link_thread(self.conn, ta, A, SAME_THREAD)
        db.link_thread(self.conn, tb, B, SAME_THREAD)
        with self.assertRaises(ValueError):          # B's copy cannot go under A's todo
            db.link_thread(self.conn, ta, B, SAME_THREAD)
        view = {t["id"]: t for t in db.todo_view(self.conn)}
        self.assertEqual([m["account"] for m in view[ta]["threads"]], [A])
        self.assertEqual([m["account"] for m in view[tb]["threads"]], [B])

    def test_legacy_unassigned_todo_adopts_the_first_emails_account(self):
        tid = db.add_engagement(self.conn, "legacy", account=A)
        self.conn.execute("UPDATE engagements SET account=NULL WHERE id=?", (tid,))
        self.conn.commit()
        db.link_thread(self.conn, tid, B, SAME_THREAD)
        self.assertEqual(self.conn.execute("SELECT account FROM engagements WHERE id=?",
                                           (tid,)).fetchone()["account"], B)
        with self.assertRaises(ValueError):
            db.link_thread(self.conn, tid, A, "1800000000000000002")

    def test_unassigned_todo_holding_two_accounts_will_not_adopt(self):
        tid = db.add_engagement(self.conn, "mixed legacy", account=A)
        with self.conn:   # a pre-existing mixed membership, as todo #409 has live
            self.conn.execute("INSERT INTO engagement_threads (engagement_id, account, thread_id, added_at) "
                              "VALUES (?,?,?,?)", (tid, A, "1", "2026-01-01T00:00:00Z"))
            self.conn.execute("INSERT INTO engagement_threads (engagement_id, account, thread_id, added_at) "
                              "VALUES (?,?,?,?)", (tid, B, "2", "2026-01-01T00:00:00Z"))
            self.conn.execute("UPDATE engagements SET account=NULL WHERE id=?", (tid,))
        with self.assertRaises(ValueError):
            db.link_thread(self.conn, tid, A, "3")

    def test_moving_a_todo_with_foreign_email_is_refused(self):
        tid = db.add_engagement(self.conn, "todo", account=A)
        db.link_thread(self.conn, tid, A, SAME_THREAD)
        with self.assertRaises(ValueError):
            db.update_engagement(self.conn, tid, {"account": B})
        # but an emptied todo can move
        db.link_thread(self.conn, tid, A, SAME_THREAD, add=False)
        self.assertTrue(db.update_engagement(self.conn, tid, {"account": B}))

    def test_a_mixed_membership_never_leaks_into_an_assigned_todos_view(self):
        tid = db.add_engagement(self.conn, "todo", account=A)
        with self.conn:   # simulate legacy contamination underneath an assigned todo
            self.conn.execute("INSERT INTO engagement_threads (engagement_id, account, thread_id, added_at) "
                              "VALUES (?,?,?,?)", (tid, B, "9", "2026-01-01T00:00:00Z"))
        v = [t for t in db.todo_view(self.conn) if t["id"] == tid][0]
        self.assertEqual(v["threads"], [])


class ViewsAreAccountScoped(DbCase):
    def test_todo_view_filters_by_account(self):
        ta = db.add_engagement(self.conn, "a1", account=A)
        tb = db.add_engagement(self.conn, "b1", account=B)
        legacy = db.add_engagement(self.conn, "legacy", account=A)
        self.conn.execute("UPDATE engagements SET account=NULL WHERE id=?", (legacy,))
        self.conn.commit()
        ids = lambda **kw: {t["id"] for t in db.todo_view(self.conn, **kw)}   # noqa: E731
        self.assertEqual(ids(account=A), {ta})
        self.assertEqual(ids(account=B), {tb})
        self.assertEqual(ids(), {ta, tb, legacy})       # unassigned matches no account filter
        self.assertTrue(all("account" in t for t in db.todo_view(self.conn)))

    def test_todo_view_bad_account_is_an_error_not_a_wildcard(self):
        with self.assertRaises(ValueError):
            db.todo_view(self.conn, account="a' OR '1'='1")

    def test_mail_view_filters_by_account_and_keeps_colliding_thread_ids_apart(self):
        _seed_mail(self.conn, A, SAME_THREAD, "in A")
        _seed_mail(self.conn, B, SAME_THREAD, "in B")
        db.set_mail_triage(self.conn, SAME_THREAD, A, {"pinned": 1})
        va = db.mail_view(self.conn, account=A)
        vb = db.mail_view(self.conn, account=B)
        self.assertEqual([r["subject"] for r in va], ["in A"])
        self.assertEqual([r["subject"] for r in vb], ["in B"])
        self.assertEqual(va[0]["pinned"], 1)
        self.assertEqual(vb[0]["pinned"], 0)             # A's triage never bleeds into B
        self.assertEqual({r["account"] for r in db.mail_view(self.conn)}, {A, B})

    def test_mail_view_ignores_legacy_project_and_goal_mappings(self):
        """Old joins matched elements/tags on thread_id alone, so a mapping made
        for account A's thread decorated account B's thread with A's project.
        The joins are gone: rows carry no project/goal keys at all."""
        _seed_mail(self.conn, B, SAME_THREAD, "in B")
        with self.conn:
            self.conn.execute("INSERT INTO elements (source, source_ref, type, title, account) "
                              "VALUES ('gmail', ?, 'email_thread', 't', ?)", (SAME_THREAD, A))
            eid = self.conn.execute("SELECT id FROM elements").fetchone()["id"]
            self.conn.execute("INSERT INTO mappings (element_id, operation_id, mapped_by, ts) "
                              "VALUES (?, 'some-project', 'model', '2026-01-01T00:00:00Z')", (eid,))
            self.conn.execute("INSERT INTO element_tags (source, source_ref, kind, value, ts) "
                              "VALUES ('gmail', ?, 'objective', 'some-goal', '2026-01-01T00:00:00Z')",
                              (SAME_THREAD,))
        row = db.mail_view(self.conn, account=B)[0]
        for key in ("project", "project_name", "goals", "extra_projects"):
            self.assertNotIn(key, row)


class WizardApprovalBoundary(DbCase):
    def test_approved_suggestion_lands_in_its_emails_account(self):
        sid = db.add_wizard_suggestion(self.conn, "mail", "Reply to Pat", account=B, thread_id="55")
        out = db.wizard_decide(self.conn, sid, approve=True)
        acct = self.conn.execute("SELECT account FROM engagements WHERE id=?",
                                 (out["todo_id"],)).fetchone()["account"]
        self.assertEqual(acct, B)

    def test_meeting_only_suggestion_needs_an_explicit_account(self):
        sid = db.add_wizard_suggestion(self.conn, "meeting", "Send deck", meeting_ref="m1",
                                       meeting_title="Sync")
        with self.assertRaises(ValueError):
            db.wizard_decide(self.conn, sid, approve=True)
        # still pending — nothing half-applied
        self.assertEqual(self.conn.execute("SELECT status FROM wizard_suggestions WHERE id=?",
                                           (sid,)).fetchone()["status"], "pending")
        out = db.wizard_decide(self.conn, sid, approve=True, account=A)
        self.assertTrue(out["todo_id"])


class GmailLabelSyncBoundary(DbCase):
    """todo_labels matched a Gmail GC/Todo/<name> label to a todo by NAME ONLY,
    so the same label in two accounts pulled the second account's threads into
    the first account's todo (this is how live todo #409 got two accounts)."""

    def _run(self, account, labels):
        orig = (todo_labels._todo_labels, todo_labels._label_threads)
        todo_labels._todo_labels = lambda imap: list(labels)
        todo_labels._label_threads = lambda imap, label: set(labels[label])
        try:
            stats = {"linked": 0, "unlinked": 0, "created": 0, "written": 0,
                     "renamed": 0, "failed": 0}
            todo_labels._reconcile_account(self.conn, object(), account, stats, lambda *_: None)
            return stats
        finally:
            todo_labels._todo_labels, todo_labels._label_threads = orig

    def test_same_label_in_two_accounts_makes_two_todos(self):
        label = "GC/Todo/Board deck"
        self._run(A, {label: ["101"]})
        self._run(B, {label: ["202"]})
        todos = self.conn.execute("SELECT id, account FROM engagements ORDER BY id").fetchall()
        self.assertEqual([t["account"] for t in todos], [A, B])
        mem = self.conn.execute("SELECT engagement_id, account, thread_id FROM engagement_threads "
                                "ORDER BY account").fetchall()
        self.assertEqual([(m["account"], m["thread_id"]) for m in mem], [(A, "101"), (B, "202")])
        for m in mem:   # each membership sits under a todo of its own account
            acct = self.conn.execute("SELECT account FROM engagements WHERE id=?",
                                     (m["engagement_id"],)).fetchone()["account"]
            self.assertEqual(acct, m["account"])

    def test_gmail_created_todo_is_assigned_to_the_account_it_came_from(self):
        self._run(B, {"GC/Todo/Only in B": ["303"]})
        self.assertEqual(self.conn.execute("SELECT account FROM engagements").fetchone()["account"], B)


# ---------------------------------------------------------------------------
# HTTP layer — a scratch dashboard on a scratch port with a scratch DB.
# ---------------------------------------------------------------------------
def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HttpBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        cls.dbpath = os.path.join(cls._dir.name, "http.db")
        conn = db.connect(cls.dbpath)
        _seed_mail(conn, A, SAME_THREAD, "mail in A")
        _seed_mail(conn, B, SAME_THREAD, "mail in B")
        conn.close()
        cls.port = _free_port()
        env = dict(os.environ, GC_PORT=str(cls.port), GC_DB_PATH=cls.dbpath)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "serve.py")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.get("/version")
                return
            except Exception:
                time.sleep(0.1)
        cls.proc.kill()
        raise RuntimeError("scratch dashboard did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        cls._dir.cleanup()

    @classmethod
    def _req(cls, method, path, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    @classmethod
    def get(cls, path):
        return cls._req("GET", path)

    @classmethod
    def post(cls, path, body):
        return cls._req("POST", path, body)

    def test_classify_routes_are_account_scoped_and_jev_needs_a_known_account(self):
        # E209: reads need a known account; nothing is classified yet here
        code, out = self.get(f"/classify?account={A}&thread_id={SAME_THREAD}")
        self.assertEqual((code, out["classification"]), (200, None), out)
        code, out = self.get(f"/classify?account=nosuchacct&thread_id={SAME_THREAD}")
        self.assertEqual(code, 400, out)
        code, out = self.post("/classify", {"action": "correct", "account": A,
                                            "thread_id": SAME_THREAD, "field": "respond", "value": "no"})
        self.assertEqual(code, 400, out)              # no classification to correct yet
        # the Jev switch only exists for configured mail accounts
        code, out = self.post("/classify", {"action": "jev", "account": A, "on": True})
        self.assertEqual(code, 400, out)
        # the roster round-trips; settings never carry a key value
        code, out = self.post("/classify", {"action": "person-add", "email": "Sam@Team.com", "name": "Sam"})
        self.assertEqual(code, 200, out)
        code, out = self.get("/classify/settings")
        self.assertEqual(code, 200, out)
        self.assertIn({"email": "sam@team.com", "name": "Sam", "grp": None, "teammate": True}, out["people"])
        self.assertIsInstance(out["jev_key_set"], bool)
        self.assertNotIn("JEV_API_KEY", json.dumps(out))
        code, out = self.post("/classify", {"action": "person-remove", "key": "sam@team.com"})
        self.assertEqual((code, out["people"]), (200, []), out)

    def test_answering_a_dev_question_needs_the_pairing_code(self):
        code, out = self.post("/dev", {"action": "answer", "id": 1, "account": A, "answer": "yes"})
        self.assertEqual(code, 403, out)

    def test_a_todo_cannot_be_moved_to_an_unknown_account(self):
        conn = db.connect(self.dbpath)
        tid = db.add_engagement(conn, "legacy", account=A)
        conn.close()
        code, out = self.post("/task", {"action": "update", "id": tid, "fields": {"account": "nosuchacct"}})
        self.assertEqual(code, 400, out)
        code, out = self.get("/todos/unassigned")
        self.assertEqual(code, 200, out)

    def test_contacts_need_a_known_account(self):
        code, out = self.get("/contacts?account=nosuchacct")
        self.assertEqual(code, 400, out)
        code, out = self.get(f"/contacts?account={A}")
        self.assertEqual(code, 200, out)

    def test_task_add_without_account_is_refused(self):
        code, out = self.post("/task", {"action": "add", "text": "orphan"})
        self.assertEqual(code, 400, out)

    def test_task_add_for_an_unknown_account_is_refused(self):
        code, out = self.post("/task", {"action": "add", "text": "x", "account": "nosuchacct"})
        self.assertEqual(code, 400, out)

    def test_accounts_bootstrap_and_dev_queue_are_account_scoped(self):
        code, out = self.get("/accounts")
        self.assertEqual(code, 200, out)
        self.assertEqual(set(out["accounts"]), {A, B})
        conn = db.connect(self.dbpath)
        try:
            db.dev_add(conn, "request visible only in A", A)
            db.dev_add(conn, "request visible only in B", B)
        finally:
            conn.close()
        self.assertEqual(self.get("/dev")[0], 400)
        ca, qa = self.get(f"/dev?account={A}")
        cb, qb = self.get(f"/dev?account={B}")
        self.assertEqual((ca, cb), (200, 200), (qa, qb))
        self.assertIn("request visible only in A", [r["text"] for r in qa["requests"]])
        self.assertNotIn("request visible only in B", [r["text"] for r in qa["requests"]])
        self.assertIn("request visible only in B", [r["text"] for r in qb["requests"]])
        self.assertNotIn("request visible only in A", [r["text"] for r in qb["requests"]])

    def test_todos_and_mail_endpoints_scope_by_account(self):
        c1, a = self.post("/task", {"action": "add", "text": "http todo A", "account": A})
        c2, b = self.post("/task", {"action": "add", "text": "http todo B", "account": B})
        self.assertEqual((c1, c2), (200, 200), (a, b))
        _, ta = self.get(f"/todos?account={A}")
        _, tb = self.get(f"/todos?account={B}")
        self.assertIn("http todo A", [t["text"] for t in ta["todos"]])
        self.assertNotIn("http todo B", [t["text"] for t in ta["todos"]])
        self.assertIn("http todo B", [t["text"] for t in tb["todos"]])
        self.assertNotIn("http todo A", [t["text"] for t in tb["todos"]])
        _, ma = self.get(f"/mail?account={A}")
        self.assertEqual({t["account"] for t in ma["threads"]}, {A})
        self.assertEqual(self.get("/todos?account=bad%27x")[0], 400)

    def test_linking_another_accounts_email_over_http_is_refused(self):
        _, todo = self.post("/task", {"action": "add", "text": "boundary", "account": A})
        code, out = self.post("/task", {"action": "link", "id": todo["id"], "account": B,
                                        "thread_id": SAME_THREAD})
        self.assertEqual(code, 400, out)
        _, tv = self.get(f"/todos?account={A}")
        mine = [t for t in tv["todos"] if t["id"] == todo["id"]][0]
        self.assertEqual(mine["threads"], [])

    def test_task_add_from_an_email_in_another_account_is_refused(self):
        code, out = self.post("/task", {"action": "add", "text": "x", "account": A,
                                        "source": "email", "source_ref": f"{B}:{SAME_THREAD}"})
        self.assertEqual(code, 400, out)

    def test_retired_routes_answer_410(self):
        for path in ("/bizdev", "/dash", "/registry", "/scratch", "/progress"):
            self.assertEqual(self.get(path)[0], 410, path)
        for path in ("/feedback", "/tag", "/registry", "/dash", "/opnote", "/scratch"):
            self.assertEqual(self.post(path, {})[0], 410, path)

    def test_settings_page_is_served_and_the_old_dashboard_is_not(self):
        def status(path):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=20) as r:
                    return r.status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(status("/settings.html"), 200)
        for gone in ("/classic.html", "/mail.js", "/todos.js", "/sample.json"):
            self.assertEqual(status(gone), 404, gone)

    def test_dismiss_requires_an_account(self):
        self.assertEqual(self.post("/dismiss", {"thread_id": SAME_THREAD})[0], 400)
        self.assertEqual(self.post("/dismiss", {"thread_id": SAME_THREAD, "account": "bad slug"})[0], 400)
        self.assertEqual(self.post("/dismiss", {"thread_id": SAME_THREAD, "account": A})[0], 200)

    def test_decision_add_ignores_project_fields(self):
        code, out = self.post("/decision", {"action": "add", "text": "pick one",
                                            "operation": "some-project"})
        self.assertEqual(code, 200, out)

    def test_allowed_triage_actions_are_wired_and_account_scoped(self):
        """The account-first triage queue's six allowed actions — reply/draft,
        archive, snooze, pin, mark handled, create an account-owned todo —
        each exist behind an HTTP route and each is account-scoped. The
        Gmail-touching ones (archive, reply/draft) are asserted with a fake
        account, which fails validation in mail_common.connect() BEFORE any
        socket is opened (mc.ACCOUNTS has no 'acctone'/'accttwo'), so this
        never reaches a real mailbox."""
        # archive: /mail-action validates the account before any IMAP call.
        code, out = self.post("/mail-action", {"action": "archive", "thread_id": SAME_THREAD,
                                                "account": A})
        self.assertEqual(code, 400, out)
        self.assertIn("account", out.get("error", ""))

        # reply/draft: /draft/compose validates the account the same way.
        code, out = self.post("/draft/compose", {"account": A, "to": "x@example.com",
                                                  "subject": "hi", "body": "hello"})
        self.assertEqual(code, 400, out)
        self.assertIn("account", out.get("error", ""))

        # pin, snooze, mark handled: local-only writes via /mail/triage,
        # each scoped to (account, thread_id) and never bleeding into the
        # other account holding the SAME thread id. A dedicated thread id is
        # seeded here (not the class's shared SAME_THREAD) so this test's
        # mutations can't disturb other tests sharing the scratch DB.
        tid = "1800000000000000099"
        conn = db.connect(self.dbpath)
        try:
            _seed_mail(conn, A, tid, "triage action test A")
            _seed_mail(conn, B, tid, "triage action test B")
        finally:
            conn.close()
        for action, extra in (("pin", {}), ("snooze", {"until": "2026-12-31"})):
            code, out = self.post("/mail/triage",
                                  {"action": action, "thread_id": tid, "account": A, **extra})
            self.assertEqual(code, 200, (action, out))
        # pin/snooze checked with all=1 (a snoozed thread is hidden from the
        # default inbox filter by design; that's the mail screen's own rule,
        # not an account-scoping question).
        code, out = self.get(f"/mail?account={A}&all=1")
        rowA = [r for r in out["threads"] if r["thread_id"] == tid][0]
        self.assertEqual(rowA["pinned"], 1)
        self.assertIsNotNone(rowA["snoozed_until"])
        code, out = self.get(f"/mail?account={B}&all=1")
        rowB = [r for r in out["threads"] if r["thread_id"] == tid][0]
        self.assertFalse(rowB.get("pinned"))
        self.assertIsNone(rowB.get("snoozed_until"))
        # mark handled (done): scoped the same way, and hides it from A's
        # default (undone) view without touching B's copy at all.
        code, out = self.post("/mail/triage", {"action": "done", "thread_id": tid, "account": A})
        self.assertEqual(code, 200, out)
        code, out = self.get(f"/mail?account={A}")
        self.assertNotIn(tid, [r["thread_id"] for r in out["threads"]])
        code, out = self.get(f"/mail?account={B}&all=1")
        rowB = [r for r in out["threads"] if r["thread_id"] == tid][0]
        self.assertIsNone(rowB.get("done_at"))

        # create an account-owned todo: never global/unowned.
        code, out = self.post("/task", {"action": "add", "text": "triage queue todo", "account": B})
        self.assertEqual(code, 200, out)
        _, tb = self.get(f"/todos?account={B}")
        self.assertIn("triage queue todo", [t["text"] for t in tb["todos"]])
        _, ta = self.get(f"/todos?account={A}")
        self.assertNotIn("triage queue todo", [t["text"] for t in ta["todos"]])


class DevRequestsAreAccountScoped(DbCase):
    """DevRequests are intake owned and listed by exactly one account."""

    def test_dev_requests_table_has_additive_account_column(self):
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(dev_requests)")}
        self.assertIn("account", cols)

    def test_dev_request_is_only_listed_in_its_account(self):
        ra = db.dev_add(self.conn, "ship A", A)
        rb = db.dev_add(self.conn, "ship B", B)
        rows_a = db.dev_list(self.conn, A)
        rows_b = db.dev_list(self.conn, B)
        self.assertEqual([r["id"] for r in rows_a], [ra])
        self.assertEqual([r["id"] for r in rows_b], [rb])
        self.assertTrue(all(r["account"] == A for r in rows_a))
        self.assertTrue(all(r["account"] == B for r in rows_b))

    def test_dev_request_cannot_be_updated_from_another_account(self):
        rid = db.dev_add(self.conn, "owned by A", A)
        self.assertFalse(db.dev_update(self.conn, rid, B, text="cross-account edit"))
        self.assertEqual(db.dev_list(self.conn, A)[0]["text"], "owned by A")
        self.assertTrue(db.dev_update(self.conn, rid, A, text="edited by owner"))
        self.assertEqual(db.dev_list(self.conn, A)[0]["text"], "edited by owner")

    def test_ownerless_historical_request_is_preserved_but_never_displayed(self):
        now = "2026-09-25T00:00:00Z"
        with self.conn:
            self.conn.execute(
                "INSERT INTO dev_requests (account, text, created_at, updated_at) VALUES (NULL,?,?,?)",
                ("legacy request", now, now))
        self.assertEqual(db.dev_list(self.conn, A), [])
        self.assertEqual(db.dev_list(self.conn, B), [])
        self.assertEqual(
            self.conn.execute("SELECT text FROM dev_requests").fetchone()["text"],
            "legacy request",
        )

    def test_a_dev_request_never_appears_in_todo_or_mail_views(self):
        db.dev_add(self.conn, "a dev-only request, never a todo", A)
        db.add_engagement(self.conn, "real todo A", account=A)
        db.add_engagement(self.conn, "real todo B", account=B)
        for acct in (A, B):
            texts = {t["text"] for t in db.todo_view(self.conn, account=acct)}
            self.assertNotIn("a dev-only request, never a todo", texts)


if __name__ == "__main__":
    unittest.main()
