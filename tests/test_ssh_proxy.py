"""E275 — the laptop's SSH link to Grand Central survives sleep.

integrations/mcp/gc_ssh_proxy.py sits between the Claude desktop app and
ssh. When ssh dies (the MacBook slept), the next message restarts it and
replays the MCP handshake; a request caught in flight gets an error, never a
hang. A tiny fake MCP server stands in for ssh + the host. Offline.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROXY = os.path.join(ROOT, "integrations", "mcp", "gc_ssh_proxy.py")

# Answers initialize and tools/list; records each start; "die" exits without
# answering (the link dropping mid-request).
FAKE = r'''
import json, sys
open(sys.argv[1], "a").write("start\n")
inited = False
for line in sys.stdin:
    m = json.loads(line)
    if m.get("method") == "initialize":
        inited = True
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"serverInfo": {"name": "fake"}}}), flush=True)
    elif m.get("method") == "die":
        sys.exit(1)
    elif "id" in m:
        print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"inited": inited}}), flush=True)
'''


class SshProxy(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self._dir.name, "fake.py")
        self.starts = os.path.join(self._dir.name, "starts")
        with open(self.fake, "w") as f:
            f.write(FAKE)
        self.p = subprocess.Popen([sys.executable, PROXY, "--", sys.executable, self.fake, self.starts],
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)

    def tearDown(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            self.p.wait(timeout=10)
        self.p.stdout.close()
        self._dir.cleanup()

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def recv(self):
        return json.loads(self.p.stdout.readline())

    def test_reconnects_and_replays_the_handshake(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(self.recv()["result"]["serverInfo"]["name"], "fake")
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        self.send({"jsonrpc": "2.0", "id": 2, "method": "die"})     # the laptop slept
        r = self.recv()
        self.assertEqual(r["id"], 2)
        self.assertIn("try again", r["error"]["message"])

        self.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        r = self.recv()                       # the replayed initialize is swallowed
        self.assertEqual((r["id"], r["result"]["inited"]), (3, True))
        with open(self.starts) as f:
            self.assertEqual(f.read().count("start"), 2)

    def test_closing_the_app_ends_the_proxy(self):
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.recv()
        self.p.stdin.close()
        self.assertEqual(self.p.wait(timeout=10), 0)


if __name__ == "__main__":
    unittest.main()
