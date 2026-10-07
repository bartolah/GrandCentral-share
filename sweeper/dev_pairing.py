"""Device pairing for the Dev request queue (E162).

The dev queue is executed unattended: sweeper/dev_worker.sh hands open request
text to a headless Claude Code session that edits, commits and pushes this
repo. So whoever can put text in that queue can change the code that runs next
to every credential. Before E162 that was anything able to POST /dev — any
local process, any tailnet device, any agent with a shell tool.

Now the three writes that put text in front of the worker — filing a request,
editing its text, and setting a request back to 'open' — need the pairing
code. Bart enters it once per device in the Dev requests panel; the browser
keeps it in localStorage and sends it as the X-GC-Dev-Code header. Only a
SHA-256 of the code is stored here (data/dev_pairing.json, chmod 600), so the
file cannot be replayed as the code.

Scope, same as BUILD_SPEC §6 E161: this does not defend against a process
already running as Bart's uid on the Mini, which can read config/.env or the
SQLite file directly. It closes the queue to everything that only reaches the
board over HTTP.

CLI:
    .venv/bin/python sweeper/dev_pairing.py --new     # mint a code (replaces any old one)
    .venv/bin/python sweeper/dev_pairing.py --status  # is a code set?
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from datetime import datetime, timezone

try:
    from .common import DATA_DIR
except ImportError:  # run as a script / imported with sweeper/ on sys.path
    from common import DATA_DIR

# GC_DEV_PAIRING_PATH: tests point this at a scratch file (E258 made the code
# the human credential for send, so the scratch server needs one it controls).
PAIRING_PATH = os.environ.get("GC_DEV_PAIRING_PATH") or os.path.join(DATA_DIR, "dev_pairing.json")


def _normalize(code: str) -> str:
    """Case- and separator-insensitive, so a code typed on a phone still matches."""
    return "".join(ch for ch in str(code or "").upper() if ch.isalnum())


def _digest(code: str) -> str:
    return hashlib.sha256(_normalize(code).encode()).hexdigest()


def is_configured(path: str = PAIRING_PATH) -> bool:
    return os.path.exists(path)


def verify(code: str | None, path: str = PAIRING_PATH) -> bool:
    """True only if a pairing exists and `code` matches it (constant-time)."""
    if not code or not _normalize(code):
        return False
    try:
        with open(path) as f:
            stored = json.load(f).get("sha256", "")
    except (OSError, ValueError):
        return False
    return bool(stored) and hmac.compare_digest(stored, _digest(code))


def new_code(path: str = PAIRING_PATH) -> str:
    """Mint a fresh 80-bit code, store its hash (0600), return the code once."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode()  # 16 chars
    code = "-".join(raw[i:i + 4] for i in range(0, 16, 4))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"sha256": _digest(code),
                   "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}, f)
    os.replace(tmp, path)
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--new", action="store_true",
                   help="mint a new code; every paired device must re-enter it")
    g.add_argument("--status", action="store_true", help="report whether a code is set")
    args = ap.parse_args()
    if args.status:
        print("paired" if is_configured() else "no pairing code set")
        return 0
    code = new_code()
    print(f"Dev pairing code: {code}")
    print("Enter it in Grand Central → ⚙ → Developer → 🛠 Dev requests on each device.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
