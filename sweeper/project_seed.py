"""Set up an account's projects from a plan file (E259, Bart 2026-09-30).

Bart: "reorganize many of the pending ConnectBlue tasks under projects
associated with ConnectBlue … no more than 10 projects". A plan names the
projects, the context documents to load (for the account and for each
project), and the words that sort the account's open todos into them.

This is Bart filing, done once and reviewed, not a classifier: automatic
project classification stays retired (ABOUT.md). So the default is a dry
run that prints every proposed filing; nothing is written until --apply, and
--move / --skip correct any line first. Filing only points a project at a
todo (projects.add_item); the todo stays in its account and nothing touches
Gmail. Todos already in a project are left where Bart put them.

    .venv/bin/python sweeper/project_seed.py config/seeds/connectblue/plan.json
    .venv/bin/python sweeper/project_seed.py PLAN --move 412="SalesDB" --skip 398
    .venv/bin/python sweeper/project_seed.py PLAN --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

import db
import projects

MAX_PROJECTS = 10           # Bart: "no more than 10 projects"
MAX_TIED = 2                # a todo that fits two projects equally goes in both


def load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        plan = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    if len(plan["projects"]) > MAX_PROJECTS:
        raise ValueError(f"{len(plan['projects'])} projects — the plan allows {MAX_PROJECTS} at most")
    db.clean_account(plan["account"])

    def read(rel):
        with open(os.path.join(base, rel), encoding="utf-8") as f:
            return f.read()

    for d in plan.get("docs", []):
        d["body"] = read(d["file"])
    for p in plan["projects"]:
        p["brief_body"] = read(p["brief"]) if p.get("brief") else None
        p["_res"] = [re.compile(rf"(?<![A-Za-z0-9]){k}(?![A-Za-z0-9])", re.I) for k in p.get("keywords", [])]
        p["_ex"] = [re.compile(k, re.I) for k in p.get("exclude", [])]
    return plan


def todo_texts(conn, account: str) -> list:
    """The account's open todos not yet in any project, with everything a
    reader would sort them by: text, notes, labels, email subjects."""
    rows = [dict(r) for r in conn.execute(
        "SELECT id, text, notes, labels, due FROM engagements WHERE account = ? AND state = 'open' "
        "AND id NOT IN (SELECT CAST(ref AS INTEGER) FROM project_items WHERE kind = 'todo') ORDER BY id",
        (account,))]
    for r in rows:
        subj = [s[0] or "" for s in conn.execute(
            "SELECT m.subject FROM engagement_threads et LEFT JOIN mail_threads m "
            "ON m.account = et.account AND m.thread_id = et.thread_id WHERE et.engagement_id = ?", (r["id"],))]
        r["haystack"] = " \n".join([r["text"] or "", r["notes"] or "", r["labels"] or "", *subj])
    return rows


def score(plan: dict, text: str) -> list:
    """[(project name, matched words)], best first; ties kept, broad ones
    dropped when a specific project matches as strongly."""
    hits = []
    for p in plan["projects"]:
        if any(x.search(text) for x in p["_ex"]):
            continue
        words = sorted({m.group(0).lower() for r in p["_res"] for m in [r.search(text)] if m})
        if words:
            hits.append((p, words))
    if not hits:
        return []
    top = max(len(w) for _, w in hits)
    best = [(p, w) for p, w in hits if len(w) == top]
    specific = [(p, w) for p, w in best if not p.get("broad")]
    best = specific or best
    return [(p["name"], w) for p, w in best]


def propose(conn, plan: dict, moves: dict, skips: set) -> tuple[dict, list]:
    """{project name: [(todo, words)]} and the todos left out."""
    names = {p["name"].lower(): p["name"] for p in plan["projects"]}
    filed, left = {p["name"]: [] for p in plan["projects"]}, []
    for t in todo_texts(conn, plan["account"]):
        if t["id"] in skips:
            continue
        if t["id"] in moves:
            name = names.get(moves[t["id"]].lower())
            if not name:
                raise ValueError(f"--move {t['id']}: no project named {moves[t['id']]!r} in the plan")
            filed[name].append((t, ["(moved by hand)"]))
            continue
        best = score(plan, t["haystack"])
        if not best or len(best) > MAX_TIED:
            left.append((t, [n for n, _ in best]))
            continue
        for name, words in best:
            filed[name].append((t, words))
    return filed, left


def _existing_project(conn, name: str):
    return conn.execute("SELECT * FROM projects WHERE lower(name) = lower(?)", (name,)).fetchone()


def _upsert_doc(conn, scope: str, title: str, body: str, filename: str | None, apply: bool) -> str:
    have = conn.execute("SELECT id, body FROM context_docs WHERE scope = ? AND title = ?", (scope, title)).fetchone()
    if have and have["body"] == body:
        return "unchanged"
    if apply:
        if have:
            db.update_context_doc(conn, have["id"], {"body": body})
        else:
            db.add_context_doc(conn, scope, title, body, filename)
    return "update" if have else "add"


def run(conn, plan: dict, apply: bool, moves: dict | None = None, skips: set | None = None, out=print) -> dict:
    acct = plan["account"]
    out(f"{'APPLYING' if apply else 'DRY RUN (nothing written — add --apply)'}: account {acct}\n")
    for d in plan.get("docs", []):
        st = _upsert_doc(conn, f"account:{acct}", d["title"], d["body"], d["file"], apply)
        out(f"  context [{acct}] {d['title']}: {st} ({len(d['body'])} chars)")
    ids = {}
    for p in plan["projects"]:
        row = _existing_project(conn, p["name"])
        if row:
            ids[p["name"]] = row["id"]
        elif apply:
            ids[p["name"]] = projects.create(conn, p["name"], p.get("color"), p.get("description"))
        if p.get("brief_body") and p["name"] in ids:
            st = _upsert_doc(conn, f"project:{ids[p['name']]}", f"{p['name']} brief", p["brief_body"],
                             os.path.basename(p["brief"]), apply)
        else:
            st = "add" if p.get("brief_body") else "none"
        out(f"  project {p['name']}: {'exists' if row else 'create'}; brief {st}")

    filed, left = propose(conn, plan, moves or {}, skips or set())
    n = 0
    for name, rows in filed.items():
        out(f"\n📁 {name} — {len(rows)} todo{'s' * (len(rows) != 1)}")
        for t, words in rows:
            out(f"   #{t['id']:<5} {t['text'][:90]}   [{', '.join(words)}]")
            if apply:
                projects.add_item(conn, ids[name], "todo", acct, str(t["id"]))
            n += 1
    out(f"\nLeft out — no clear project ({len(left)}). File by hand, or --move ID=\"Project\":")
    for t, tied in left:
        out(f"   #{t['id']:<5} {t['text'][:90]}" + (f"   (ties: {', '.join(tied)})" if tied else ""))
    out(f"\n{n} filing{'s' * (n != 1)} {'made' if apply else 'proposed'}; {len(left)} left out.")
    return {"filed": {k: [t["id"] for t, _ in v] for k, v in filed.items()},
            "left": [t["id"] for t, _ in left], "project_ids": ids}


def _moves(pairs) -> dict:
    out = {}
    for s in pairs or []:
        tid, _, name = s.partition("=")
        if not tid.strip().isdigit() or not name.strip():
            raise SystemExit(f"--move wants ID=Project name, got {s!r}")
        out[int(tid)] = name.strip()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("plan")
    ap.add_argument("--apply", action="store_true", help="write; without it nothing changes")
    ap.add_argument("--move", action="append", metavar='ID="Project"', help="file this todo here instead")
    ap.add_argument("--skip", action="append", type=int, metavar="ID", help="leave this todo out")
    a = ap.parse_args(argv)
    plan = load_plan(a.plan)
    conn = db.connect()
    try:
        run(conn, plan, a.apply, _moves(a.move), set(a.skip or []))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
