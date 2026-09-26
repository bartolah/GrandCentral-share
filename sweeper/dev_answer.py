"""Answer a Dev request card from the unattended worker (E162).

The worker has no curl; this is its one way to write to POST /dev, and it can
only set status + note — never request text, which needs the pairing code.

    .venv/bin/python sweeper/dev_answer.py ID STATUS "note text"

STATUS is building | shipped | declined | open (open only works on a request
the worker marked building — handing it back to Bart with a question).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

STATUSES = ("building", "shipped", "declined", "open")


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4) or not argv[1].isdigit() or argv[2] not in STATUSES:
        print(__doc__)
        return 2
    body = {"action": "update", "id": int(argv[1]), "status": argv[2]}
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
