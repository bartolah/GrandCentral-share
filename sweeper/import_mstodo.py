"""One-time Microsoft To-Do import (E2-P3, decision #3: export file, no creds).

Reads an export file Casey hands over — CSV or JSON, headers matched loosely —
and creates engagements with source='mstodo'. Idempotent: a task with the
same text that already exists under that source is skipped, so re-running
the import (or importing an overlapping export) never duplicates.

CSV columns recognized (case-insensitive, first match wins):
    text:  subject | title | task | name
    due:   due date | duedate | due
    notes: notes | body  (appended to the text in parentheses, trimmed)

JSON: a list of objects with the same loose keys, or plain strings.

Run:
    cd sweeper
    ../.venv/bin/python import_mstodo.py --file ~/Desktop/todo-export.csv --dry-run
    ../.venv/bin/python import_mstodo.py --file ~/Desktop/todo-export.csv
    ../.venv/bin/python import_mstodo.py --file x.csv --account civic
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import db  # noqa: E402

TEXT_KEYS = ("subject", "title", "task", "name")
DUE_KEYS = ("due date", "duedate", "due")
NOTE_KEYS = ("notes", "body")


def _pick(row: dict, keys: tuple) -> str:
    low = {str(k).lower().strip(): v for k, v in row.items() if v is not None}
    for k in keys:
        if low.get(k):
            return str(low[k]).strip()
    return ""


def _normalize_due(raw: str) -> str | None:
    """Accept YYYY-MM-DD or M/D/YYYY; anything else is dropped (logged)."""
    raw = (raw or "").strip()
    if not raw:
        return None
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def load_items(path: str) -> list:
    """[{text, due}] from a CSV or JSON export."""
    items = []
    if path.lower().endswith(".json"):
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        rows = data if isinstance(data, list) else data.get("tasks", data.get("value", []))
        for r in rows:
            if isinstance(r, str):
                items.append({"text": r.strip(), "due": None})
            elif isinstance(r, dict):
                text = _pick(r, TEXT_KEYS)
                note = _pick(r, NOTE_KEYS)[:120]
                if text:
                    items.append({"text": text + (f"  ({note})" if note else ""),
                                  "due": _normalize_due(_pick(r, DUE_KEYS))})
    else:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                text = _pick(r, TEXT_KEYS)
                note = _pick(r, NOTE_KEYS)[:120]
                if text:
                    items.append({"text": text + (f"  ({note})" if note else ""),
                                  "due": _normalize_due(_pick(r, DUE_KEYS))})
    return items


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Import a Microsoft To-Do export as engagements (idempotent)")
    ap.add_argument("--file", required=True, help="CSV or JSON export file")
    ap.add_argument("--account", required=True,
                    help="mail account every imported todo belongs to (e.g. personal)")
    ap.add_argument("--dry-run", action="store_true", help="parse and report; write nothing")
    args = ap.parse_args(argv)

    items = load_items(args.file)
    if not items:
        print("no tasks recognized in the file — check the column headers")
        return 1

    conn = db.connect()
    try:
        try:
            args.account = db.clean_account(args.account)
        except ValueError as e:
            print(e)
            return 1
        created = skipped = 0
        for it in items:
            if args.dry_run:
                print(f'  {"due " + it["due"] if it["due"] else "      ":<14} {it["text"][:90]}')
                continue
            new_id = db.add_engagement(conn, it["text"], account=args.account,
                                       due=it["due"], source="mstodo", dedupe=True)
            if new_id:
                created += 1
            else:
                skipped += 1
        if args.dry_run:
            print(f"\n[dry-run] {len(items)} task(s) parsed; nothing written")
        else:
            print(f"imported {created} task(s), skipped {skipped} already-imported")
            print("they appear on the board after the next sweep (or add via ✎ now)")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
