"""E258 — outbound hosts are allowlisted per process (BUILD_SPEC §6 item 10).

Bart, 2026-09-30: "Hard-gate anything that moves data outward … Allowlist by
destination, deny by default. The quiet risk isn't a rogue send — it's data
leaving via a summary, a fetched attachment, or a search result." Chose "Log 3
days, then enforce".

install(proc) wraps socket.getaddrinfo — every Python connection (urllib,
imaplib, smtplib, httpx under the Anthropic SDK) resolves its host through it
— so a host not in config/egress_allow.txt is:
    log mode (default)   recorded in logs/egress.jsonl as would_block
    enforce mode         refused with OSError before a packet leaves
GC_EGRESS=enforce switches a process to enforcing. Loopback always passes.

Honest scope: subprocesses (the gog CLI, OpenClaw's CLI, Chrome in
browser.py) resolve names in their own processes and are not covered; a
per-app firewall (LuLu / Little Snitch) is the layer for those.

Review what the log saw:
    .venv/bin/python sweeper/egress.py --report
"""

from __future__ import annotations

import fnmatch
import json
import os
import socket
import sys
import threading
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOW_PATH = os.environ.get("GC_EGRESS_ALLOW") or os.path.join(ROOT, "config", "egress_allow.txt")
LOG_PATH = os.path.join(ROOT, "logs", "egress.jsonl")

_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0", ""}
_state = {"proc": None, "enforce": False, "rules": [], "seen": set(), "orig": None}
_lock = threading.Lock()


def load_rules(path: str = ALLOW_PATH) -> list:
    rules = []
    try:
        with open(path) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if not line:
                    continue
                parts = line.split()
                rules.append((parts[0].lower(), parts[1] if len(parts) > 1 else None))
    except OSError:
        pass
    return rules


def allowed(host: str, proc: str, rules: list) -> bool:
    host = (host or "").lower().rstrip(".")
    if host in _LOOPBACK or host.startswith("127."):
        return True
    if proc == "mcp":
        return False  # the MCP server talks to the board and nothing else
    for pattern, tag in rules:
        if tag and tag != proc:
            continue
        if host == pattern or (pattern.startswith("*.") and fnmatch.fnmatch(host, pattern)):
            return True
    return False


def _record(host, port, ok: bool) -> None:
    key = (host, ok)
    with _lock:
        if key in _state["seen"]:
            return
        _state["seen"].add(key)
    row = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "proc": _state["proc"], "host": host, "port": port,
           "allowed": ok, "mode": "enforce" if _state["enforce"] else "log"}
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    if not ok:
        verb = "BLOCKED" if _state["enforce"] else "would block"
        print(f"egress: {verb} {host}:{port} from {_state['proc']}", file=sys.stderr, flush=True)


def install(proc: str) -> None:
    """Idempotent. Call once at process start."""
    if _state["orig"] is not None:
        return
    _state.update(proc=proc, rules=load_rules(),
                  enforce=os.environ.get("GC_EGRESS", "").strip().lower() == "enforce",
                  orig=socket.getaddrinfo)
    orig = socket.getaddrinfo

    def guarded(host, port, *args, **kwargs):
        h = host.decode() if isinstance(host, bytes) else str(host or "")
        ok = allowed(h, _state["proc"], _state["rules"])
        _record(h, port, ok)
        if not ok and _state["enforce"]:
            raise OSError(f"egress to {h} is not allowlisted (config/egress_allow.txt, §6 E258)")
        return orig(host, port, *args, **kwargs)

    socket.getaddrinfo = guarded


def report(path: str = LOG_PATH) -> dict:
    out: dict = {}
    try:
        with open(path) as f:
            for line in f:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = (r["host"], r["allowed"])
                e = out.setdefault(k, {"procs": set(), "first": r["ts"], "last": r["ts"], "n": 0})
                e["procs"].add(r["proc"])
                e["last"] = r["ts"]
                e["n"] += 1
    except OSError:
        pass
    return out


if __name__ == "__main__":
    rows = report()
    if not rows:
        print("no egress recorded yet")
    for (host, ok), e in sorted(rows.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        print(f"{'ok         ' if ok else 'WOULD BLOCK'}  {host:<40} "
              f"{','.join(sorted(p or '?' for p in e['procs'])):<22} {e['first']} → {e['last']} ({e['n']} runs)")
