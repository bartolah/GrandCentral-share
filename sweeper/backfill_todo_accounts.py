#!/usr/bin/env python3
"""Assign legacy todos to their account, from evidence only.

Account-first (2026-09-24): every todo belongs to exactly one mail account.
Todos created before engagements.account existed have account NULL. This
script fills it in ONLY where the data proves it:

  * the todo's source_ref is "<account>:<thread_id>", and/or
  * every email grouped under it (engagement_threads) is in one account.

A todo whose evidence is missing or contradictory (emails from two accounts,
or a source_ref that disagrees with its members) is LEFT NULL and reported —
nothing is guessed and nothing is deleted or moved. Only rows whose account
is still NULL are touched, so it is safe to re-run.

    .venv/bin/python sweeper/backfill_todo_accounts.py              # dry run
    .venv/bin/python sweeper/backfill_todo_accounts.py --apply      # write
    .venv/bin/python sweeper/backfill_todo_accounts.py --db copy.db # other DB
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db  # noqa: E402


def plan(conn) -> dict:
    """{'assign': [(id, account, why)], 'ambiguous': [(id, why)], 'unknown': [id]}"""
    out = {"assign": [], "ambiguous": [], "unknown": []}
    members: dict[int, set] = {}
    for r in conn.execute("SELECT engagement_id, account FROM engagement_threads"):
        members.setdefault(r["engagement_id"], set()).add(r["account"])
    for r in conn.execute(
            "SELECT id, source_ref FROM engagements WHERE account IS NULL ORDER BY id"):
        ref = db._ref_account(r["source_ref"])
        accts = members.get(r["id"], set())
        evidence = set(accts) | ({ref} if ref else set())
        if not evidence:
            out["unknown"].append(r["id"])
        elif len(evidence) == 1:
            why = ("source_ref + linked emails" if ref and accts
                   else "source_ref" if ref else "linked emails")
            out["assign"].append((r["id"], next(iter(evidence)), why))
        else:
            out["ambiguous"].append((r["id"], sorted(evidence)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write (default is a dry run)")
    ap.add_argument("--db", default=db.DB_PATH, help="database path")
    args = ap.parse_args()

    conn = db.connect(args.db)
    try:
        p = plan(conn)
        by_acct: dict[str, int] = {}
        for _id, acct, _why in p["assign"]:
            by_acct[acct] = by_acct.get(acct, 0) + 1
        print(f"{'APPLY' if args.apply else 'DRY RUN'} on {args.db}")
        print(f"  assignable: {len(p['assign'])}  {by_acct}")
        print(f"  ambiguous (left NULL): {len(p['ambiguous'])}")
        for eid, evid in p["ambiguous"]:
            print(f"    todo #{eid}: evidence spans accounts {evid}")
        print(f"  no evidence (left NULL, needs a human): {len(p['unknown'])}")
        if args.apply:
            with conn:
                for eid, acct, _why in p["assign"]:
                    conn.execute("UPDATE engagements SET account = ? "
                                 "WHERE id = ? AND account IS NULL", (acct, eid))
            print("  written.")
        else:
            print("  (dry run — nothing written; pass --apply)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
