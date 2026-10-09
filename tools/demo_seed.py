#!/usr/bin/env python3
"""Fill a scratch database with made-up mail, todos and projects, so the
board can be tried (or screenshotted) without connecting a single mailbox.

    .venv/bin/python tools/demo_seed.py --db /tmp/gc-demo.db
    GC_DB_PATH=/tmp/gc-demo.db GC_PORT=8090 .venv/bin/python dashboard/serve.py
    open http://127.0.0.1:8090

Every person, company and address here is invented (example.com and friends).
It refuses to touch the real database: --db must name a new file. Nothing is
sent anywhere; the board only reads what is seeded.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ACCOUNTS = ("work", "studio", "personal")

# (account, from name, from email, subject, snippet, hours ago, unread, starred, category)
THREADS = [
    ("work", "Priya Natarajan", "priya@northwind.example", "Q4 partnership proposal — revised numbers",
     "Attached the revised deck with the new pricing tiers. Can we lock this by Friday?", 2, 1, 1, "people"),
    ("work", "Marcus Webb", "marcus@lumen.example", "Intro: Lumen x your team",
     "Following up on our chat at the summit, I'd love to set up 30 minutes next week.", 5, 1, 0, "people"),
    ("work", "Dana Whitfield", "dana@yourco.example", "Board deck draft v3",
     "Pushed the latest. Slides 7-9 still need the retention chart.", 9, 0, 0, "people"),
    ("work", "Calendar", "calendar-notification@google.example", "Invitation: Weekly pipeline review",
     "Tuesdays 10:00-10:30. Join with Google Meet.", 20, 0, 0, "calendar"),
    ("work", "Stripe", "receipts@stripe.example", "Your receipt from Cloudhost #4417",
     "Amount paid $49.00. Thanks for your business.", 26, 0, 0, "receipt"),
    ("work", "SaaS Weekly", "news@saasweekly.example", "The state of B2B pricing, 2026",
     "This week: usage-based pricing goes mainstream, and three teardown case studies.", 30, 0, 0, "newsletter"),
    ("work", "Jordan Lee", "jordan@acme.example", "Contract redlines",
     "Legal had two small notes on section 4. Otherwise we're good to sign.", 52, 0, 1, "people"),
    ("studio", "Elena Ruiz", "elena@fieldnotes.example", "Photo selects for the spring issue",
     "Here are the 24 selects. Flagged my top six in the folder.", 3, 1, 0, "people"),
    ("studio", "Sam Okafor", "sam@printhaus.example", "Print proof ready",
     "Proof is attached. Please sign off by Thursday to hit the press slot.", 7, 1, 1, "people"),
    ("studio", "GitHub", "noreply@github.example", "[studio-site] Deploy succeeded",
     "main deployed to production in 42s.", 15, 0, 0, "notification"),
    ("studio", "Morgan Ellis", "morgan@clientco.example", "Re: Brand refresh timeline",
     "Works for us. Can we see first concepts the week of the 20th?", 40, 0, 0, "people"),
    ("personal", "Mom", "mom@family.example", "Thanksgiving plans",
     "Are you driving up Wednesday night or Thursday morning?", 4, 1, 0, "people"),
    ("personal", "City Library", "holds@library.example", "Your hold is ready",
     "The Overstory is ready for pickup until Saturday.", 18, 0, 0, "notification"),
    ("personal", "Theo Brand", "theo@friends.example", "Climbing Saturday?",
     "Gym opens at 9. I can grab you on the way.", 28, 0, 0, "people"),
]

# (account, text, due offset in days or None, starred, my_day today)
TODOS = [
    ("work", "Send Priya the signed pricing sheet", 0, 1, 1),
    ("work", "Finish retention chart for board deck", 1, 0, 1),
    ("work", "Reply to Marcus with two intro slots", 0, 0, 0),
    ("work", "Review contract redlines (section 4)", -1, 1, 0),
    ("work", "Draft Q1 hiring plan", 6, 0, 0),
    ("studio", "Sign off on the print proof", 2, 1, 1),
    ("studio", "Pick six photo selects for spring issue", 3, 0, 0),
    ("studio", "Invoice Clientco for October", -2, 0, 0),
    ("personal", "Book Thanksgiving train tickets", 4, 0, 0),
    ("personal", "Pick up library hold", 1, 0, 0),
]

PROJECTS = [
    ("Spring Issue", "#7c5cff", "The studio's spring print issue: photos, copy, proof, press.",
     [("thread", "studio", 2), ("thread", "studio", 1), ("todo", None, 6), ("todo", None, 7)],
     [("Run of show", "Selects due Thu. Proof sign-off Fri. Press slot Mon 8am.")]),
    ("Q4 Partnerships", "#0ea5e9", "Close the Northwind and Lumen partnerships before year end.",
     [("thread", "work", 1), ("thread", "work", 2), ("todo", None, 1), ("todo", None, 3)],
     [("Terms we can offer", "Tiered pricing; 60-day pilot; co-marketing in Q1.")]),
]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def seed(path: str) -> dict:
    sys.path.insert(0, os.path.join(ROOT, "sweeper"))
    os.environ["GC_DB_PATH"] = path
    import db
    import projects

    now = datetime.now(timezone.utc)
    today = datetime.now().date()
    conn = db.connect(path)
    tids = {}
    with conn:
        for i, (acct, who, email, subj, snip, ago, unread, star, cat) in enumerate(THREADS, 1):
            tid = str(1900000000000000000 + i)
            tids[(acct, len([k for k in tids if k[0] == acct]) + 1)] = tid
            conn.execute(
                "INSERT INTO mail_threads (thread_id, account, subject, participants, last_from, "
                "last_from_email, last_direction, snippet, last_date, message_count, unread, "
                "starred, in_inbox, category, synced_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)",
                (tid, acct, subj, json.dumps([who]), who, email, "inbound", snip,
                 _iso(now - timedelta(hours=ago)), 1 + i % 3, unread, star, cat, _iso(now)))
    todo_ids = []
    for acct, text, due, star, my_day in TODOS:
        tid = db.add_engagement(conn, text, account=acct,
                                due=(today + timedelta(days=due)).isoformat() if due is not None else None)
        fields = {}
        if star:
            fields["starred"] = 1
        if my_day:
            fields["my_day"] = today.isoformat()
        if fields:
            db.update_engagement(conn, tid, fields)
        todo_ids.append(tid)
    for name, color, desc, items, notes in PROJECTS:
        pid = projects.create(conn, name, color, desc, (today + timedelta(days=21)).isoformat())
        for kind, acct, n in items:
            if kind == "thread":
                projects.add_item(conn, pid, "thread", acct, tids[(acct, n)])
            else:
                projects.add_item(conn, pid, "todo", None, str(todo_ids[n - 1]))
        for title, body in notes:
            projects.note_add(conn, pid, title, body)
    conn.close()
    return {"accounts": list(ACCOUNTS), "threads": len(THREADS), "todos": len(TODOS),
            "projects": len(PROJECTS)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="a NEW scratch database file")
    a = ap.parse_args(argv)
    path = os.path.abspath(a.db)
    if os.path.exists(path):
        print(f"{path} exists; demo data only goes into a new file", file=sys.stderr)
        return 2
    if path == os.path.abspath(os.path.join(ROOT, "data", "grand_central.db")):
        print("refusing to seed the real database", file=sys.stderr)
        return 2
    out = seed(path)
    print(f"Seeded {path}: {out}")
    print(f"Run:  GC_DB_PATH={path} GC_PORT=8090 .venv/bin/python dashboard/serve.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
