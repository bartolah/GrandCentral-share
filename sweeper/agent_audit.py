"""E258 — the first-week review of what agents did (BUILD_SPEC §6 item 8).

Casey, 2026-09-30: "I want to review the first week and specifically look for
calls I didn't intend — that's how injection shows up." Reads agent_audit
(written by the board for every agent request and every unidentified local
write) and prints:

  * calls per tool and per account
  * every write, with its full arguments
  * every refusal (the gate said no — often the first sign)
  * SUSPECT: a write within WINDOW minutes after the agent read something
    whose content carried injection signals — the shape an injection takes
  * the lockouts and the Private/agent-account state

    .venv/bin/python sweeper/agent_audit.py              # last 7 days
    .venv/bin/python sweeper/agent_audit.py --days 1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

try:
    from . import db
except ImportError:  # run with sweeper/ on sys.path
    import db

WINDOW = 2  # minutes


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def review(conn, days: int = 7) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM agent_audit WHERE ts >= ? ORDER BY id", (since,))]
    tools, accounts = Counter(), Counter()
    writes, refusals, suspects = [], [], []
    last_signal = None  # (ts, row) of the latest read that carried signals
    for r in rows:
        tools[r["tool"] or f"{r['method']} {r['route']}"] += 1
        for a in json.loads(r["accounts"] or "[]"):
            accounts[a] += 1
        if r["refusal"]:
            refusals.append(r)
        agent = r["principal"] == "agent"
        if agent and r["method"] == "GET" and (r["signals"] or 0) > 0 and not r["refusal"]:
            last_signal = (_ts(r["ts"]), r)
        if r["method"] == "POST" and not r["refusal"]:
            writes.append(r)
            if agent and last_signal and _ts(r["ts"]) - last_signal[0] <= timedelta(minutes=WINDOW):
                suspects.append((r, last_signal[1]))
    return {"rows": rows, "tools": tools, "accounts": accounts, "writes": writes,
            "refusals": refusals, "suspects": suspects}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Review agent calls (E258)")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    conn = db.connect()
    out = review(conn, a.days)
    print(f"Agent audit, last {a.days} day(s): {len(out['rows'])} calls\n")
    print("By tool:")
    for t, n in out["tools"].most_common():
        print(f"  {n:>5}  {t}")
    print("\nBy account touched:")
    for t, n in out["accounts"].most_common():
        print(f"  {n:>5}  {t}")
    print(f"\nSUSPECT — a write within {WINDOW} min of reading injection-shaped content "
          f"({len(out['suspects'])}):")
    for w, read in out["suspects"]:
        print(f"  {w['ts']} {w['tool'] or w['route']} {w['args'][:300]}")
        print(f"      after {read['ts']} {read['tool'] or read['route']} "
              f"({read['signals']} signal(s)) {read['args'][:160]}")
    print(f"\nRefused ({len(out['refusals'])}):")
    for r in out["refusals"]:
        print(f"  {r['ts']} [{r['principal']}] {r['tool'] or ''} {r['method']} {r['route']} "
              f"— {r['refusal'][:140]}")
    print(f"\nWrites ({len(out['writes'])}):")
    for r in out["writes"]:
        print(f"  {r['ts']} [{r['principal']}] {r['tool'] or r['route']} "
              f"{', '.join(json.loads(r['accounts'] or '[]'))}  {r['args'][:300]}")
    lock = conn.execute("SELECT value FROM agent_state WHERE key = 'lock'").fetchone()
    print("\nLockout:", lock[0] if lock else "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
