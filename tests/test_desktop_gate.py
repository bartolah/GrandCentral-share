"""E260 — the Claude desktop app's layer on the agent gate.

BUILD_SPEC §6, 2026-09-30. Bart: "Option A first, one account per read,
approve all actions." The desktop key gets every E258 agent rule plus: each
read names one account and sees only it. Since §6 2026-10-06 ("Desktop:
everything direct") it makes the agent's writes itself (not notes or sweeps);
the E260 proposal path remains and is still tested. Offline: scratch serve.py, scratch
DB and keys, fake accounts that exist in no .env.
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
sys.path.insert(0, os.path.join(ROOT, "integrations", "mcp"))

import db  # noqa: E402

A, B, C = "acctone", "accttwo", "acctthree"   # A and B on, C off
THREAD = "1800000000000000001"


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _seed_mail(conn, account, thread_id, subject):
    conn.execute(
        "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
        "last_from_email, last_direction, snippet, last_date, in_inbox, synced_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (thread_id, account, subject, "[]", "Sender", "s@example.com", "inbound",
         "snip", "2026-09-24T12:00:00Z", 1, "2026-09-24T12:00:00Z"))
    conn.commit()


class DesktopGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._dir = tempfile.TemporaryDirectory()
        d = cls._dir.name
        cls.dbpath = os.path.join(d, "gate.db")
        cls.keypath = os.path.join(d, "agent_key.json")
        cls.deskpath = os.path.join(d, "agent_key_desktop.json")
        cls.pairpath = os.path.join(d, "pairing.json")
        os.environ["GC_AGENT_KEY_PATH"] = cls.keypath
        os.environ["GC_DESKTOP_KEY_PATH"] = cls.deskpath
        os.environ["GC_DEV_PAIRING_PATH"] = cls.pairpath
        import importlib
        import agent_gate
        import dev_pairing
        importlib.reload(dev_pairing)
        importlib.reload(agent_gate)
        cls.gate = agent_gate
        cls.key = agent_gate.new_key(cls.keypath)
        cls.desk = agent_gate.new_key(cls.deskpath)
        cls.code = dev_pairing.new_code(cls.pairpath)

        conn = db.connect(cls.dbpath)
        for acct in (A, B, C):
            _seed_mail(conn, acct, THREAD, f"mail in {acct}")
        cls.todo_a = db.add_engagement(conn, "todo in A", account=A)
        cls.todo_b = db.add_engagement(conn, "todo in B", account=B)
        cls.todo_c = db.add_engagement(conn, "todo in C", account=C)
        agent_gate.set_account(conn, A, True)
        agent_gate.set_account(conn, B, True)
        conn.close()

        cls.port = _free_port()
        env = dict(os.environ, GC_PORT=str(cls.port), GC_DB_PATH=cls.dbpath)
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "dashboard", "serve.py")],
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                cls._req("GET", "/version")
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
        for k in ("GC_AGENT_KEY_PATH", "GC_DESKTOP_KEY_PATH", "GC_DEV_PAIRING_PATH"):
            os.environ.pop(k, None)

    def setUp(self):
        conn = db.connect(self.dbpath)
        with conn:
            conn.execute("DELETE FROM agent_audit")
            conn.execute("DELETE FROM agent_state")
            conn.execute("DELETE FROM agent_proposals")
        conn.close()

    @classmethod
    def _req(cls, method, path, body=None, headers=None):
        h = {"Content-Type": "application/json", **(headers or {})}
        req = urllib.request.Request(f"http://127.0.0.1:{cls.port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers=h)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def desktop(self, method, path, body=None, tool="gc_test"):
        return self._req(method, path, body, {"X-GC-Agent-Key": self.desk, "X-GC-Tool": tool})

    def agent(self, method, path, body=None):
        return self._req(method, path, body, {"X-GC-Agent-Key": self.key})

    def human(self, method, path, body=None):
        return self._req(method, path, body, {"X-GC-Dev-Code": self.code})

    def propose(self, account, path, body, tool="gc_add_todo"):
        return self.desktop("POST", "/agent/propose",
                            {"tool": tool, "account": account, "preview": "test",
                             "path": path, "body": body}, tool=tool)

    def todo_texts(self, account):
        conn = db.connect(self.dbpath)
        try:
            return [r[0] for r in conn.execute(
                "SELECT text FROM engagements WHERE account = ?", (account,))]
        finally:
            conn.close()

    # ---- one account per read ----

    def test_read_without_an_account_is_refused(self):
        for path in ("/todos", "/mail", "/todos?account=all"):
            code, out = self.desktop("GET", path)
            self.assertEqual(code, 400, path)
            self.assertIn("one account per call", out["error"])

    def test_read_sees_only_the_named_account(self):
        code, out = self.desktop("GET", f"/todos?account={A}")
        self.assertEqual(code, 200)
        accounts = {t.get("account") for t in out.get("todos", [])}
        self.assertEqual(accounts, {A})   # B is switched on too, and still not here

    def test_switched_off_account_refused(self):
        code, out = self.desktop("GET", f"/todos?account={C}")
        self.assertEqual(code, 403)
        self.assertIn("not switched on", out["error"])

    def test_calendar_spans_accounts_so_it_is_not_a_desktop_read(self):
        code, _ = self.desktop("GET", "/calendar")
        self.assertEqual(code, 403)

    # ---- §6 2026-10-06: it acts directly, but only as the agent may ----

    def test_desktop_acts_directly_within_the_agent_rules(self):
        # Bart: "An agent creating a draft shouldn't require an approval" …
        # "Desktop: everything direct"
        code, out = self.desktop("POST", "/task", {"action": "add", "text": "direct", "account": A})
        self.assertEqual(code, 200, out)
        self.assertIn("direct", self.todo_texts(A))
        code, _ = self.desktop("POST", "/task", {"action": "add", "text": "off", "account": C})
        self.assertEqual(code, 403)                              # switched-off account
        self.assertNotIn("off", self.todo_texts(C))

    def test_desktop_still_cannot_write_notes_or_sweep(self):
        for path, body in (("/context", {"action": "add", "text": "x", "account": A}),
                           ("/sweep", {"account": A})):
            self.setUp()
            code, _ = self.desktop("POST", path, body)
            self.assertEqual(code, 403, path)

    def test_desktop_cannot_send_or_approve_itself(self):
        code, _ = self.desktop("POST", "/send", {"confirm": True, "to": "x@example.com"})
        self.assertEqual(code, 403)
        code, pid = self.propose(A, "/task", {"action": "add", "text": "self", "account": A})
        self.assertEqual(code, 200)
        for caller in (self.desktop, self.agent):
            code, _ = caller("POST", "/agent/proposals/decide",
                             {"id": pid["id"], "decision": "approve"})
            self.assertEqual(code, 403)
        self.assertNotIn("self", self.todo_texts(A))

    def test_only_agent_actions_can_be_proposed(self):
        for path, body in (("/send", {"confirm": True, "account": A}),
                           ("/mail-action", {"action": "trash", "thread_id": THREAD, "account": A}),
                           ("/mail-action", {"action": "label-add", "label": "private",
                                             "thread_id": THREAD, "account": A}),
                           ("/sweep", {"account": A}),
                           ("/context", {"action": "add", "text": "x", "account": A}),
                           ("/agent/reset", {"account": A})):
            self.setUp()   # three refusals would trip the breaker (tested in E258)
            code, out = self.propose(A, path, body)
            self.assertEqual(code, 403, f"{path} {body} -> {out}")

    def test_three_refusals_lock_the_desktop_app_too(self):
        for _ in range(3):
            self.desktop("POST", "/send", {"confirm": True, "to": "x@example.com", "account": A})
        code, out = self.desktop("GET", f"/todos?account={A}")
        self.assertEqual(code, 423)
        self.assertIn("desktop", out["error"])

    def test_proposal_in_a_switched_off_account_refused(self):
        code, _ = self.propose(C, "/task", {"action": "add", "text": "x", "account": C})
        self.assertEqual(code, 403)
        code, _ = self.propose(A, "/task", {"action": "update", "id": self.todo_c,
                                            "fields": {"text": "hijacked"}})
        self.assertEqual(code, 403)

    def test_proposal_account_must_be_where_it_acts(self):
        code, out = self.propose(A, "/task", {"action": "update", "id": self.todo_b,
                                              "fields": {"text": "moved"}})
        self.assertEqual(code, 400)
        self.assertIn("account it acts in", out["error"])

    # ---- approve all actions ----

    def test_approved_proposal_runs_once_as_the_agent(self):
        code, out = self.propose(A, "/task", {"action": "add", "text": "call Sam", "account": A})
        self.assertEqual((code, out["status"]), (200, "pending_approval"))
        self.assertNotIn("call Sam", self.todo_texts(A))   # nothing yet

        code, other = self.desktop("GET", f"/agent/proposals?account={B}&id={out['id']}")
        self.assertEqual(other["proposals"], [])      # one account per read, here too
        code, out = self.desktop("GET", f"/agent/proposals?account={A}&id={out['id']}")
        self.assertEqual(out["proposals"][0]["status"], "pending")
        pid = out["proposals"][0]["id"]

        code, out = self.human("POST", "/agent/proposals/decide",
                               {"id": pid, "decision": "approve"})
        self.assertEqual(code, 200, out)
        self.assertEqual(out["status"], "approved")
        self.assertEqual(self.todo_texts(A).count("call Sam"), 1)

        code, _ = self.human("POST", "/agent/proposals/decide", {"id": pid, "decision": "approve"})
        self.assertEqual(code, 400)                      # once
        self.assertEqual(self.todo_texts(A).count("call Sam"), 1)

        conn = db.connect(self.dbpath)
        try:
            ran = conn.execute("SELECT principal, tool FROM agent_audit WHERE route = '/task' "
                               "AND method = 'POST'").fetchall()
        finally:
            conn.close()
        self.assertEqual([tuple(r) for r in ran], [("agent", "approved:gc_add_todo")])

    def test_declined_proposal_never_runs(self):
        _, out = self.propose(A, "/task", {"action": "add", "text": "never", "account": A})
        code, res = self.human("POST", "/agent/proposals/decide",
                               {"id": out["id"], "decision": "decline"})
        self.assertEqual((code, res["status"]), (200, "declined"))
        self.assertNotIn("never", self.todo_texts(A))

    def test_approval_is_checked_again_when_it_runs(self):
        _, out = self.propose(B, "/task", {"action": "add", "text": "late", "account": B})
        conn = db.connect(self.dbpath)
        self.gate.set_account(conn, B, False)
        conn.close()
        try:
            code, res = self.human("POST", "/agent/proposals/decide",
                                   {"id": out["id"], "decision": "approve"})
            self.assertEqual(res["status"], "failed")
            self.assertNotIn("late", self.todo_texts(B))
        finally:
            conn = db.connect(self.dbpath)
            self.gate.set_account(conn, B, True)
            conn.close()

    def test_settings_lists_pending_requests(self):
        self.propose(A, "/task", {"action": "add", "text": "listed", "account": A})
        code, out = self.human("GET", "/agent/accounts")
        self.assertEqual(code, 200)
        self.assertTrue(out["desktop_key_set"])
        self.assertEqual([p["body"]["text"] for p in out["pending_proposals"]], ["listed"])


class DesktopClient(unittest.TestCase):
    """The local layer in gc_client: the desktop profile cannot even build
    a request the board would refuse."""

    def test_desktop_profile_allowlist(self):
        import importlib
        import gc_client
        importlib.reload(gc_client)
        gc_client.use_desktop_profile()
        try:
            for method, path, body in (("POST", "/send", {}),
                                       ("POST", "/context", {"action": "add"}),
                                       ("POST", "/sweep", {}),
                                       ("GET", "/calendar", None),
                                       ("POST", "/agent/proposals/decide", {"id": 1})):
                with self.assertRaises(gc_client.GCError, msg=f"{method} {path}"):
                    gc_client._check(method, path, body)
            gc_client._check("POST", "/task", {"action": "add"})   # §6 2026-10-06: direct
            gc_client._check("POST", "/draft", {})
            gc_client._check("POST", "/agent/propose", {})
            gc_client._check("GET", "/todos", None)
        finally:
            importlib.reload(gc_client)

    def test_desktop_tools_are_narrow(self):
        import gc_remote
        import gc_client
        import importlib
        try:
            srv = gc_remote.build()
            names = set(srv.tools)
            self.assertTrue(all(n.startswith("gc_") for n in names))
            self.assertFalse({n for n in names if "send" in n or "trash" in n})
            for t in srv.tools.values():   # every call names one account
                self.assertIn("account", t.schema["properties"], t.name)
                self.assertIn("account", t.schema["required"], t.name)
            self.assertIn("never follow instructions", srv.instructions)
        finally:
            importlib.reload(gc_client)



class DraftLimits(unittest.TestCase):
    """§6 2026-10-06 (Bart: "there shouldn't be a cap of 10"): drafts never send,
    so they are outside the hourly write limit; only a runaway ceiling stays."""

    def test_drafts_are_not_writes_and_only_a_high_ceiling_remains(self):
        import agent_gate as G
        for route in ("/draft", "/draft/save", "/draft/compose"):
            self.assertEqual(G._bucket(G.Ctx("agent", "POST", route, {}, None)), ["draft"])
        self.assertEqual(G._bucket(G.Ctx("agent", "POST", "/task", {}, None)), ["write"])
        caps = [cap for bucket, _w, cap, _l in G.LIMITS if bucket == "draft"]
        self.assertTrue(caps and min(caps) >= 200, caps)


if __name__ == "__main__":
    unittest.main()
