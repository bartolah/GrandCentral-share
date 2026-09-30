"""Commit and push the dev worker's change, by name (2026-09-29).

The unattended worker (sweeper/dev_worker.sh) had not managed to ship a
single change since E162 tightened it on 2026-09-14: its commits were
refused at the permission layer, most visibly because a multi-line message
built with $(...) — the usual way to add the co-author line — is shell
substitution the allowlist rightly won't run. This helper is the worker's
one way to ship: a single allowlisted command, no shell in between.

    .venv/bin/python sweeper/dev_ship.py "E238: subject line" "Body text…" FILE [FILE …]

It refuses anything outside the paths the worker may edit (and every secret
or runtime path), stages exactly the named files — never -A, never -f —
commits with the co-author line appended, and pushes main. The worker can't
edit this file (dev_worker_settings.json).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWED_PREFIXES = ("dashboard/", "sweeper/", "integrations/", "capture/", "tests/")
ALLOWED_FILES = ("README.md", "ToDos.md")
NEVER = ("config/", "data/", "logs/", ".git/", ".venv/", "design/")
LOCKED = {"sweeper/dev_worker.sh", "sweeper/dev_worker_settings.json", "sweeper/dev_answer.py",
          "sweeper/dev_check.py", "sweeper/dev_triage.py", "sweeper/dev_ship.py",
          "sweeper/dev_pairing.py", "sweeper/auto_pull.sh", "sweeper/send_mail.py",
          "sweeper/connections.py"}
CO_AUTHOR = "Co-Authored-By: Claude <noreply@anthropic.com>"


def check_path(p: str) -> str:
    rel = os.path.relpath(os.path.abspath(os.path.join(ROOT, p)), ROOT).replace(os.sep, "/")
    if rel.startswith("..") or rel.startswith(NEVER) or "/.env" in rel or rel.startswith(".env"):
        raise ValueError(f"not a file the worker may ship: {p}")
    if rel in LOCKED:
        raise ValueError(f"the worker's own control files are not shipped by the worker: {rel}")
    if not (rel.startswith(ALLOWED_PREFIXES) or rel in ALLOWED_FILES):
        raise ValueError(f"outside the worker's paths: {rel}")
    return rel


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {(out.stderr or out.stdout).strip()[:400]}")
    return out.stdout


def ship(subject: str, body: str, files: list, push: bool = True) -> str:
    subject = " ".join((subject or "").split())
    if not subject:
        raise ValueError("a commit subject is required")
    if not files:
        raise ValueError("name the files to commit")
    rels = [check_path(f) for f in files]
    if _git("diff", "--cached", "--name-only").strip():
        raise RuntimeError("something is already staged — not mixing it into this commit")
    _git("add", "--", *rels)
    msg = f"{subject}\n\n{(body or '').strip()}\n\n{CO_AUTHOR}\n".replace("\n\n\n\n", "\n\n")
    with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt") as f:
        f.write(msg)
        path = f.name
    try:
        _git("commit", "-F", path)
    finally:
        os.unlink(path)
    sha = _git("rev-parse", "--short", "HEAD").strip()
    if push:
        _git("push", "origin", "main")
    return sha


def main(argv: list) -> int:
    if len(argv) < 4:
        print(__doc__)
        return 2
    try:
        sha = ship(argv[1], argv[2], argv[3:])
    except Exception as e:
        print(f"not shipped: {e}")
        return 1
    print(f"shipped {sha}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
