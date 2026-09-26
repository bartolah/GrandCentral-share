"""Answer a Dev request card from the unattended worker (E162).

The worker has no curl; this is its one way to write to POST /dev, and it can
only set status + note — never request text, which needs the pairing code.

    .venv/bin/python sweeper/dev_answer.py ID STATUS "note text"

STATUS is building | question | shipped | declined | open. Use `question`
with the question as the note when a request is unclear: the card shows it
to Bart with an answer box, the worker skips it until he answers, and his
answer comes back appended to the request text. (`open` still hands a
request back without a question.)
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

DB = os.environ.get("GC_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "grand_central.db")


def _account_of(rid: int) -> str | None:
    """Account-first (2026-09-25): POST /dev needs the card's own account.
    Read it from the card itself, so the caller can't aim at another account."""
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        row = conn.execute("SELECT account FROM dev_requests WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else None

STATUSES = ("building", "question", "shipped", "declined", "open")


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or not argv[1].isdigit() or argv[2] not in STATUSES:
        print(__doc__)
        return 2
    account = _account_of(int(argv[1]))
    if not account:
        print(f"no request {argv[1]} with an account")
        return 1
    body = {"action": "update", "id": int(argv[1]), "status": argv[2], "account": account}
    if len(argv) == 4:
        body["note"] = argv[3]
    req = urllib.request.Request("http://localhost:8080/dev", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.read().decode()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
