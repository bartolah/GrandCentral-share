"""Run the dev queue now, from the app (dev #82, §6 2026-11-09).

Casey asked for "a button to … queue the triage queue manually" and, asked
whether a tap may also start the unattended build, chose "Yes, with
guardrails". Two buttons on the dev requests page call POST /dev/run, which
only a paired browser reaches (agent_gate.HUMAN_ONLY_POST):

  triage  runs sweeper/dev_triage.py now (labels cards; builds nothing)
  build   starts the scheduled worker job itself (launchd devworker), so it is
          the same run as 07:23/15:23 — same triage, same build-only s/m
          cards, ≤5, same restricted settings and dev_ship — just sooner

Guardrails: one run at a time; at most BUILD_PER_DAY on-demand builds and
TRIAGE_PER_DAY triages a day; no build while the tree has uncommitted
tracked changes (the worker refuses that too). When an on-demand build ends,
dev_worker.sh calls `dev_run.py --done` and Casey gets a push + iMessage with
what shipped and what came back as a question.

    .venv/bin/python sweeper/dev_run.py            # today's on-demand runs + whether one is going
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone

try:
    from .common import DATA_DIR, LOGS_DIR
except ImportError:  # pragma: no cover - direct-run fallback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import DATA_DIR, LOGS_DIR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(DATA_DIR, "dev_runs.json")
MARKER = os.path.join(DATA_DIR, "dev_run_request.json")
LOG = os.path.join(LOGS_DIR, "devworker.log")
BUILD_PER_DAY = 4
TRIAGE_PER_DAY = 10
LABEL = "com.example.grandcentral.devworker"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        with open(STATE) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STATE)


def running() -> bool:
    """The worker or a triage pass is going right now."""
    # A process EXECUTING one of the scripts: an interpreter whose next
    # argument is the script. A plain substring match also caught any shell
    # whose command line merely mentioned the file name.
    r = subprocess.run(["/bin/ps", "-axo", "pid=,command="], capture_output=True, text=True)
    me = os.getpid()
    for line in r.stdout.splitlines():
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or int(pid) == me:
            continue
        argv = cmd.split()
        if len(argv) >= 2 and os.path.basename(argv[0]).startswith(("bash", "sh", "python")) \
                and argv[1].endswith(("sweeper/dev_worker.sh", "sweeper/dev_triage.py")):
            return True
    return False


def _dirty() -> bool:
    r = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                       cwd=ROOT, capture_output=True, text=True)
    return bool(r.stdout.strip())


def status() -> dict:
    d = _load()
    today = [r for r in d.get("runs", []) if r.get("day") == _today()]
    n = {m: sum(1 for r in today if r.get("mode") == m) for m in ("triage", "build")}
    return {"running": running(), "today": today, "last": (d.get("runs") or [None])[-1],
            "left": {"build": max(0, BUILD_PER_DAY - n["build"]),
                     "triage": max(0, TRIAGE_PER_DAY - n["triage"])}}


def start(mode: str) -> dict:
    """Validate and start. Raises ValueError with a sentence for Casey."""
    if mode not in ("triage", "build"):
        raise ValueError("mode must be triage or build")
    st = status()
    if st["running"]:
        raise ValueError("a dev run is already going — it'll answer on the cards when it's done")
    if not st["left"][mode]:
        cap = BUILD_PER_DAY if mode == "build" else TRIAGE_PER_DAY
        raise ValueError(f"that's today's {cap} on-demand {mode} runs — the scheduled ones still happen")
    if mode == "build" and _dirty():
        raise ValueError("a session has uncommitted changes in the code — the worker won't build "
                         "on top of them; commit or stash first")
    os.makedirs(LOGS_DIR, exist_ok=True)
    if mode == "triage":
        with open(LOG, "a") as log:
            log.write(f"[{datetime.now():%F %T}] on-demand triage (dev #82)\n")
            log.flush()
            subprocess.Popen([os.path.join(ROOT, ".venv/bin/python"), "sweeper/dev_triage.py"],
                             cwd=ROOT, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True)
    else:
        with open(MARKER, "w") as f:
            json.dump({"since": _now(), "mode": "build"}, f)
        r = subprocess.run(["/bin/launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            try:
                os.unlink(MARKER)
            except OSError:
                pass
            raise RuntimeError(f"couldn't start the worker: {(r.stderr or r.stdout).strip()[:200]}")
    d = _load()
    runs = [r for r in d.get("runs", []) if r.get("day", "") >= _today()[:8]][-40:]
    runs.append({"day": _today(), "at": _now(), "mode": mode})
    d["runs"] = runs
    _save(d)
    return {"started": mode, **status()}


def done(log=print) -> None:
    """Called by dev_worker.sh as it exits: after an on-demand build, tell
    Casey what happened. A scheduled run leaves no marker and says nothing."""
    try:
        with open(MARKER) as f:
            since = json.load(f).get("since") or _now()
        os.unlink(MARKER)
    except (OSError, ValueError):
        return
    import db
    import notify
    from common import load_env
    conn = db.connect()
    try:
        rows = conn.execute("SELECT id, status, note FROM dev_requests WHERE updated_at >= ? "
                            "ORDER BY id", (since,)).fetchall()
        shipped = [r for r in rows if r["status"] == "shipped"]
        asked = [r for r in rows if r["status"] == "question"]
        if not rows:
            text = "🛠 Dev run finished\nNothing was cleared to build."
        else:
            parts = []
            if shipped:
                parts.append("Shipped: " + "; ".join(f"#{r['id']} {(r['note'] or '')[:80]}" for r in shipped))
            if asked:
                parts.append("Questions for you: " + ", ".join(f"#{r['id']}" for r in asked))
            other = [r for r in rows if r not in shipped and r not in asked]
            if other:
                parts.append("Still open: " + ", ".join(f"#{r['id']}" for r in other))
            text = "🛠 Dev run finished\n" + "\n".join(parts)
        target = load_env().get("NOTIFY_IMESSAGE_TARGET", "you@example.com")
        notify._deliver(conn, "dev", target, text[:900], log=log, url="/?go=dev")
    finally:
        conn.close()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--done"]:
        done()
        return 0
    print(json.dumps(status(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
