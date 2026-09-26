"""Syntax gate for the unattended dev worker (E162).

The worker runs with a narrow tool allowlist and no free-form shell, so the
checks it used to improvise (python -c, temp files, node --check on extracted
inline scripts) live here as one allowed command:

    .venv/bin/python sweeper/dev_check.py

Checks every changed or untracked .py file with ast, and every inline <script>
block of dashboard/*.html plus dashboard/*.js with `node --check`. Exit 0 = all
clean; non-zero prints what failed. Safe to run by hand too.
"""

from __future__ import annotations

import ast
import glob
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)(?![^>]*type=\"(?:application/json|module)\")[^>]*>(.*?)</script>",
                       re.S | re.I)


def _changed_py() -> list[str]:
    out = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    paths = [line[3:].split(" -> ")[-1] for line in out.splitlines()]
    return [p for p in paths if p.endswith(".py") and os.path.exists(os.path.join(ROOT, p))]


def _node_check(label: str, source: str) -> str | None:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(source)
        path = f.name
    try:
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True)
        return None if r.returncode == 0 else f"{label}: {r.stderr.strip()}"
    finally:
        os.unlink(path)


def main() -> int:
    failures = []
    for rel in _changed_py():
        try:
            with open(os.path.join(ROOT, rel)) as f:
                ast.parse(f.read(), filename=rel)
        except SyntaxError as e:
            failures.append(f"{rel}:{e.lineno}: {e.msg}")
    for path in sorted(glob.glob(os.path.join(ROOT, "dashboard", "*.html"))):
        with open(path) as f:
            html = f.read()
        for i, m in enumerate(SCRIPT_RE.finditer(html)):
            line = html.count("\n", 0, m.start(1)) + 1
            err = _node_check(f"{os.path.relpath(path, ROOT)} script #{i + 1} (line {line})", m.group(1))
            if err:
                failures.append(err)
    for path in sorted(glob.glob(os.path.join(ROOT, "dashboard", "*.js"))):
        with open(path) as f:
            err = _node_check(os.path.relpath(path, ROOT), f.read())
        if err:
            failures.append(err)
    if failures:
        print("SYNTAX CHECK FAILED:\n" + "\n".join(failures))
        return 1
    print("syntax check clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
