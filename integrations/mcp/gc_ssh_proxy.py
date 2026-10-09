#!/usr/bin/env python3
"""Keep the Claude desktop app connected to Grand Central over SSH (E275).

Runs on the laptop, between the desktop app and ssh:

    claude desktop  <-stdio->  gc_ssh_proxy.py  <-stdio->  ssh casey@gc-host (forced command)

Casey, 2026-10-02: the MacBook's connection showed "Server disconnected" after
it slept. ssh exits when the host stops answering, and the app does not
restart a dead MCP server on its own. This process never exits while the app
is open: when ssh dies it notes it, and the next message from the app starts
ssh again and quietly replays the MCP handshake (initialize + initialized)
first. Grand Central's tools are stateless, so nothing is lost. A request
that was in flight when the link dropped gets an error saying to try again,
rather than hanging.

It forwards bytes; it adds no capability. What the desktop app may do is
still decided by the board (sweeper/agent_gate.py) and the forced command on
the host. Stdlib only, so it runs with the Mac's own /usr/bin/python3.

    /usr/bin/python3 gc_ssh_proxy.py -- /usr/bin/ssh -T -i KEY ... casey@gc-host
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time

REINIT_ID = "gc-proxy-reinit"     # our replayed initialize; its answer is swallowed
RETRY_DELAYS = (0, 2, 5)          # seconds before each reconnect attempt


class Proxy:
    def __init__(self, argv: list):
        self.argv = argv
        self.child = None
        self.lock = threading.Lock()        # the child and the pending table
        self.out_lock = threading.Lock()    # one writer to the app at a time
        self.init_msg = None
        self.initialized_msg = None
        self.pending = {}                   # request id -> the child it went to

    def log(self, msg: str) -> None:
        print(f"gc-ssh-proxy: {msg}", file=sys.stderr, flush=True)

    def emit(self, line: bytes) -> None:
        with self.out_lock:
            sys.stdout.buffer.write(line if line.endswith(b"\n") else line + b"\n")
            sys.stdout.buffer.flush()

    def error(self, req_id, message: str) -> None:
        self.emit(json.dumps({"jsonrpc": "2.0", "id": req_id,
                              "error": {"code": -32000, "message": message}}).encode())

    # -- the ssh side --------------------------------------------------------

    def _write(self, child, msg: dict) -> None:
        child.stdin.write((json.dumps(msg) + "\n").encode())
        child.stdin.flush()

    def _start(self, replay: bool):
        child = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        threading.Thread(target=self._pump, args=(child,), daemon=True).start()
        if replay and self.init_msg:
            # the app already did the handshake with the old connection
            self._write(child, dict(self.init_msg, id=REINIT_ID))
            if self.initialized_msg:
                self._write(child, self.initialized_msg)
            self.log("reconnected to the host")
        return child

    def _pump(self, child) -> None:
        for raw in child.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                if msg.get("id") == REINIT_ID:
                    continue
                if "id" in msg and ("result" in msg or "error" in msg):
                    with self.lock:
                        self.pending.pop(msg["id"], None)
            self.emit(raw)
        child.wait()
        with self.lock:
            if self.child is child:
                self.child = None
            lost = [i for i, c in self.pending.items() if c is child]
            for i in lost:
                del self.pending[i]
        self.log(f"connection to the host closed (exit {child.returncode})")
        for i in lost:
            self.error(i, "The connection to Grand Central on the host dropped (the laptop "
                          "probably slept). It reconnects on the next call: try again.")

    # -- the app side --------------------------------------------------------

    def forward(self, msg: dict) -> None:
        method = msg.get("method")
        if method == "initialize":
            self.init_msg = msg
        elif method in ("notifications/initialized", "initialized"):
            self.initialized_msg = msg
        req_id = msg.get("id") if "method" in msg else None
        for delay in RETRY_DELAYS:
            with self.lock:
                try:
                    if self.child is None or self.child.poll() is not None:
                        time.sleep(delay)
                        self.child = self._start(replay=method != "initialize")
                    if req_id is not None:
                        self.pending[req_id] = self.child
                    self._write(self.child, msg)
                    return
                except (OSError, ValueError) as e:   # broken pipe, closed stdin
                    self.log(f"send failed ({e}); reconnecting")
                    self.pending.pop(req_id, None)
                    self.child = None
        if req_id is not None:
            self.error(req_id, "Cannot reach Grand Central on the host. Check that the "
                               "MacBook is on Tailscale and the host is awake, then try again.")

    def run(self) -> int:
        for raw in sys.stdin.buffer:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.forward(msg)
        with self.lock:      # the app closed: take ssh down with us
            if self.child is not None and self.child.poll() is None:
                self.child.terminate()
        return 0


def main(argv: list) -> int:
    if "--" not in argv or not argv[argv.index("--") + 1:]:
        print("usage: gc_ssh_proxy.py -- ssh -T -i KEY ... user@host", file=sys.stderr)
        return 2
    return Proxy(argv[argv.index("--") + 1:]).run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
