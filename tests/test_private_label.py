"""E258 — the "Private" label: a thread that carries it does not exist in Grand Central.

BUILD_SPEC §6 E258 item 2 (Casey chose "Gone entirely"). Offline: scratch DB,
fake IMAP, a scratch serve.py; fake accounts acctone/accttwo, and the SAME
thread id in both so the latch provably stays in its own account.
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
import private  # noqa: E402

A, B = "acctone", "accttwo"
T = "1800000000000000001"


def _seed(conn, account, tid, subject="s"):
    conn.execute(
        "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
        "last_from_email, last_direction, snippet, last_date, in_inbox, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (tid, account, subject, "[]", "Sender", "s@example.com", "inbound",
         "privileged snippet", "2026-09-29T12:00:00Z", 1, "2026-09-29T12:00:00Z"))
    conn.execute("INSERT INTO mail_bodies (account, thread_id, fetched_at, payload) "
                 "VALUES (?, ?, ?, ?)", (account, tid, "2026-09-29T12:00:00Z", '{"messages":[]}'))
    conn.commit()


class FakeImap:
    """Answers the three calls scan_imap makes."""

    def __init__(self, thrids):
        self.thrids = thrids

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"']

    def select(self, box, readonly=True):
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            assert args[-1] == '"label:private"', args
            return "OK", [b" ".join(str(i + 1).encode() for i in range(len(self.thrids)))]
        rows = [(f"{i + 1} (X-GM-THRID {t} UID {i + 1})".encode(), b"")
                for i, t in enumerate(self.thrids)]
        return "OK", rows


class Latch(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self._dir.name, "p.db"))
        _seed(self.conn, A, T)
        _seed(self.conn, B, T)
        self.todo = db.add_engagement(self.conn, "follow up", account=A)
        db.link_thread(self.conn, self.todo, A, T, add=True)

    def tearDown(self):
        self.conn.close()
        self._dir.cleanup()

    def count(self, table, account):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE account = ?",
                                 (account,)).fetchone()[0]

    def test_mark_purges_only_that_account(self):
        self.assertTrue(private.mark(self.conn, A, T))
        for table in ("mail_threads", "mail_bodies", "engagement_threads"):
            self.assertEqual(self.count(table, A), 0, table)
        self.assertEqual(self.count("mail_threads", B), 1)   # same id, other account
        self.assertEqual(self.count("mail_bodies", B), 1)
        # the todo is Casey's — it stays, without the thread
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM engagements WHERE id = ?",
                                               (self.todo,)).fetchone())
        self.assertFalse(private.mark(self.conn, A, T))      # already latched

    def test_scan_latches_every_labelled_thread(self):
        n = private.scan_imap(FakeImap([T, "1800000000000000002"]), A, conn=self.conn,
                              log=lambda *a: None)
        self.assertEqual(n, 2)
        self.assertEqual(private.keys(self.conn), {(A, T), (A, "1800000000000000002")})

    def test_guard_says_not_found(self):
        private.mark(self.conn, A, T)
        with self.assertRaisesRegex(ValueError, "thread not found"):
            private.guard(A, T, self.conn)
        private.guard(B, T, self.conn)   # the other account's thread is fine

    def test_unmark(self):
        private.mark(self.conn, A, T)
        self.assertTrue(private.unmark(self.conn, A, T))
        self.assertFalse(private.is_private(A, T, self.conn))

    def test_label_match(self):
        self.assertTrue(private.has_label(["Work", "private"]))
        self.assertTrue(private.has_label(['"Private"']))
        self.assertFalse(private.has_label(["Private stuff", "GC/Private-ish"]))

    def test_scrub(self):
        board = {"reply_debt": [{"account": A, "thread_id": T, "subject": "secret"},
                                {"account": B, "thread_id": T, "subject": "fine"}],
                 "sent": [{"thread_id": T}]}
        out = private.scrub(board, {(A, T)}, default_account=A)
        self.assertEqual([r["subject"] for r in out["reply_debt"]], ["fine"])
        self.assertEqual(out["sent"], [])   # no account on the row: the request's


class Board(unittest.TestCase):
    """Casey's own browser gets not-found too; only a paired browser unmarks."""

    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        d = cls._dir.name
        cls.dbpath = os.path.join(d, "b.db")
        cls.pairpath = os.path.join(d, "pairing.json")
        import dev_pairing
        cls.code = dev_pairing.new_code(cls.pairpath)
        conn = db.connect(cls.dbpath)
        _seed(conn, A, T, "privileged")
        _seed(conn, B, T, "ordinary")
        private.mark(conn, A, T)
        conn.close()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            cls.port = s.getsockname()[1]
        env = dict(os.environ, GC_PORT=str(cls.port), GC_DB_PATH=cls.dbpath,
                   GC_DEV_PAIRING_PATH=cls.pairpath,
                   GC_AGENT_KEY_PATH=os.path.join(d, "no_key.json"))
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "serve.py")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls.req("GET", "/version")
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
    def req(cls, method, path, body=None, headers=None):
        r = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", method=method,
                                   data=json.dumps(body).encode() if body is not None else None,
                                   headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(r, timeout=20) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_thread_is_not_found_even_for_a_paired_browser(self):
        code, out = self.req("GET", f"/thread?account={A}&thread_id={T}",
                             headers={"X-GC-Dev-Code": self.code})
        self.assertEqual(code, 404)
        self.assertNotIn("private", out["error"].lower())

    def test_inbox_never_shows_it(self):
        code, out = self.req("GET", "/mail")
        self.assertEqual(code, 200)
        subjects = [t["subject"] for t in out["threads"]]
        self.assertIn("ordinary", subjects)
        self.assertNotIn("privileged", subjects)

    def test_unmark_is_human_only(self):
        code, _ = self.req("POST", "/private", {"account": A, "thread_id": T})
        self.assertEqual(code, 403)
        code, _ = self.req("POST", "/private", {"account": A, "thread_id": T},
                           headers={"X-GC-Dev-Code": self.code})
        self.assertEqual(code, 200)
        conn = db.connect(self.dbpath)
        try:
            self.assertFalse(private.is_private(A, T, conn))
            private.mark(conn, A, T)   # put it back for the other tests
        finally:
            conn.close()


class TodoLabelsSkipPrivate(unittest.TestCase):
    """A Private thread still wearing its GC/Todo label in Gmail must not be
    linked back into the todo (nor create a todo) by the two-way label sync."""

    def test_reconcile_never_links_a_private_thread(self):
        import todo_labels
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "t.db"))
            try:
                private.mark(conn, A, T)
                orig = (todo_labels._todo_labels, todo_labels._label_threads)
                todo_labels._todo_labels = lambda imap: ["GC/Todo/Case prep"]
                todo_labels._label_threads = lambda imap, label: {T}
                try:
                    stats = {k: 0 for k in ("linked", "unlinked", "created", "written",
                                            "renamed", "failed")}
                    todo_labels._reconcile_account(conn, object(), A, stats, lambda *a: None)
                finally:
                    todo_labels._todo_labels, todo_labels._label_threads = orig
                self.assertEqual((stats["linked"], stats["created"]), (0, 0))
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM engagement_threads")
                                 .fetchone()[0], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
