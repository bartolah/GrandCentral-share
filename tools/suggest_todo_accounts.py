#!/usr/bin/env python3
"""Suggest an account for each legacy todo that has none (2026-09-26).

Casey: "I have a lot of todos that used to exist unassigned to a project ...
How do I bring them back in each of the right accounts with their mapped
emails?" sweeper/backfill_todo_accounts.py placed every todo the email
evidence proves. This covers the rest with hints, never assignments:

  1. the old project the todo was filed under, when that project was named
     for one of the accounts ("Brightline" → brightline)
  2. otherwise, an account named in the todo's own text

Hints go to todo_account_hints; the Unassigned todos view pre-selects them
and Casey confirms. This is a one-off tool, not runtime code: it is the only
place allowed to read the retired project tables, and only reads them.

    .venv/bin/python tools/suggest_todo_accounts.py            # dry run
    .venv/bin/python tools/suggest_todo_accounts.py --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

import db  # noqa: E402

norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())


def match_account(name: str, accounts: list) -> str | None:
    n = norm(name)
    for a in accounts:
        if n and (n == norm(a) or n.startswith(norm(a))):
            return a
    return None


def plan(conn, accounts: list) -> list:
    """[(todo_id, account, why)] for open todos with no account."""
    out = []
    rows = conn.execute(
        "SELECT e.id, e.text, e.operation_id FROM engagements e "
        "WHERE e.account IS NULL AND e.done_at IS NULL ORDER BY e.id").fetchall()
    theater_of = {}
    for r in conn.execute(
            "SELECT ot.operation_id, t.name FROM operation_theaters ot "
            "JOIN theaters t ON t.id = ot.theater_id"):
        theater_of.setdefault(r["operation_id"], r["name"])
    for r in rows:
        proj = theater_of.get(r["operation_id"])
        acct = match_account(proj, accounts) if proj else None
        if acct:
            out.append((r["id"], acct, f"was in the {proj} project"))
            continue
        named = [a for a in accounts
                 if re.search(r"\b" + re.escape(a.replace("_", " ")) + r"\b", (r["text"] or "").lower())]
        if len(named) == 1:
            out.append((r["id"], named[0], "mentions " + named[0].replace("_", " ").title()))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--db", default=db.DB_PATH)
    args = ap.parse_args(argv)
    conn = db.connect(args.db)
    try:
        accounts = sorted({r[0] for r in conn.execute(
            "SELECT DISTINCT account FROM mail_threads WHERE account IS NOT NULL")})
        hints = plan(conn, accounts)
        by = {}
        for _, a, _ in hints:
            by[a] = by.get(a, 0) + 1
        print(("APPLY" if args.apply else "DRY RUN") + f": {len(hints)} hints {by}")
        if args.apply:
            db.todo_hints_replace(conn, hints)
            print("  written.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
