"""Projects — the cross-job layer (spec: ACCOUNT_FIRST.md "Projects — spec";
BUILD_SPEC §6, 2026-09-29).

Bart: "Todos and Emails should remain in their accounts … we will handle this
through Projects." A project belongs to no account and only POINTS at things:
threads, todos, calendar events and contacts, each named by the account it
lives in and its id there — keyed (account, ref), never by id alone, because
Gmail thread ids repeat across accounts. Filing never moves, labels, archives
or re-owns anything; acting on an item still happens in its own account.
Notes written in a project belong to it, and outlive it: deleting a project
drops the project and its links and moves its notes to Notes.

Everything here is local SQLite. Nothing in this module talks to Gmail.

    .venv/bin/python sweeper/projects.py            # list projects
    .venv/bin/python sweeper/projects.py 3          # one project, resolved
"""

from __future__ import annotations

import json
import re
import sys

import db

KINDS = ("thread", "todo", "event", "contact")
MAX_PINS = 3                       # Bart: "Up to 3"
_THREAD_RE = re.compile(r"^\d{1,25}$")
_EMAIL_RE = re.compile(r"^[^@\s]{1,120}@[^@\s]{1,200}\.[^@\s]{1,40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# A calendar event has no stable id in the ICS feeds we read, so it is named
# the way calendar_reader already de-duplicates it: date|time|title (step 4).
_EVENT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\|(all-day|\d{2}:\d{2})\|(.{1,280})$")
PALETTE = ("#d97757", "#6a9bcc", "#788c5d", "#8b6ac8", "#c9a82d", "#3d7be0", "#b05e43")


def _clean_text(v, limit: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:limit]


def _get(conn, pid: int) -> dict:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (int(pid),)).fetchone()
    if row is None:
        raise ValueError("no such project")
    return dict(row)


def _touch(conn, pid: int, **extra) -> None:
    sets = ["updated_at = ?"] + [f"{k} = ?" for k in extra]
    conn.execute(f"UPDATE projects SET {', '.join(sets)} WHERE id = ?",
                 [db._now_iso(), *extra.values(), int(pid)])


# ------------------------------------------------------------------ projects
def create(conn, name: str, color: str | None = None, description: str | None = None,
           target_date: str | None = None) -> int:
    name = _clean_text(name, 120)
    if not name:
        raise ValueError("a project needs a name")
    if target_date and not _DATE_RE.match(target_date):
        raise ValueError("target date must be YYYY-MM-DD")
    if color and not _COLOR_RE.match(color):
        raise ValueError("colour must be #rrggbb")
    n = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    now = db._now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO projects (name, color, description, target_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, color or PALETTE[n % len(PALETTE)], _clean_text(description, 2000) or None,
             target_date or None, now, now))
    return cur.lastrowid


def update(conn, pid: int, name=None, color=None, description=None, target_date=None) -> dict:
    _get(conn, pid)
    fields = {}
    if name is not None:
        if not _clean_text(name, 120):
            raise ValueError("a project needs a name")
        fields["name"] = _clean_text(name, 120)
    if color is not None:
        if color and not _COLOR_RE.match(color):
            raise ValueError("colour must be #rrggbb")
        fields["color"] = color or None
    if description is not None:
        fields["description"] = _clean_text(description, 2000) or None
    if target_date is not None:
        if target_date and not _DATE_RE.match(target_date):
            raise ValueError("target date must be YYYY-MM-DD")
        fields["target_date"] = target_date or None
    with conn:
        _touch(conn, pid, **fields)
    return _get(conn, pid)


def pin(conn, pid: int, on: bool) -> dict:
    p = _get(conn, pid)
    with conn:
        if on and p["pin_rank"] is None:
            used = conn.execute("SELECT COUNT(*) FROM projects WHERE pin_rank IS NOT NULL").fetchone()[0]
            if used >= MAX_PINS:
                raise ValueError(f"up to {MAX_PINS} projects can be pinned — unpin one first")
            _touch(conn, pid, pin_rank=used + 1)
        elif not on and p["pin_rank"] is not None:
            _touch(conn, pid, pin_rank=None)
            # close the gap so the pins stay 1..n in order
            rows = conn.execute("SELECT id FROM projects WHERE pin_rank IS NOT NULL ORDER BY pin_rank").fetchall()
            for i, r in enumerate(rows, 1):
                conn.execute("UPDATE projects SET pin_rank = ? WHERE id = ?", (i, r["id"]))
    return _get(conn, pid)


def open_todos(conn, pid: int) -> list:
    """The project's todos that are still open — what Complete asks about."""
    return [dict(r) for r in conn.execute(
        "SELECT e.id, e.text, e.account FROM project_items i JOIN engagements e "
        "ON i.kind = 'todo' AND e.id = CAST(i.ref AS INTEGER) AND e.account = i.account "
        "WHERE i.project_id = ? AND e.state = 'open' ORDER BY e.id", (int(pid),))]


def complete(conn, pid: int) -> dict:
    """Marks the project done and records when. Its todos are NOT touched:
    the board asks Bart, and completes any he chooses in their own account."""
    _get(conn, pid)
    with conn:
        _touch(conn, pid, status="done", completed_at=db._now_iso(), pin_rank=None)
    return _get(conn, pid)


def reopen(conn, pid: int) -> dict:
    _get(conn, pid)
    with conn:
        _touch(conn, pid, status="open", completed_at=None)
    return _get(conn, pid)


def delete(conn, pid: int) -> dict:
    """Deletes the project and its links only. Its emails, todos, events and
    contacts are untouched; its notes move to Notes, marked with its name."""
    p = _get(conn, pid)
    with conn:
        kept = conn.execute("UPDATE notes SET project_id = NULL, from_project = ?, updated_at = ? "
                            "WHERE project_id = ?", (p["name"], db._now_iso(), int(pid))).rowcount
        links = conn.execute("DELETE FROM project_items WHERE project_id = ?", (int(pid),)).rowcount
        conn.execute("DELETE FROM projects WHERE id = ?", (int(pid),))
        if p["pin_rank"] is not None:
            rows = conn.execute("SELECT id FROM projects WHERE pin_rank IS NOT NULL ORDER BY pin_rank").fetchall()
            for i, r in enumerate(rows, 1):
                conn.execute("UPDATE projects SET pin_rank = ? WHERE id = ?", (i, r["id"]))
    return {"deleted": int(pid), "links_removed": links, "notes_kept": kept}


# ------------------------------------------------------------------ items
def add_item(conn, pid: int, kind: str, account: str | None, ref, title: str | None = None) -> dict:
    """Point the project at one item. Idempotent. The item's account is its
    own — for a todo it is read from the todo, never taken from the caller."""
    _get(conn, pid)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    ref = str(ref or "").strip()
    if kind == "thread":
        account = db.clean_account(account)
        if not _THREAD_RE.match(ref):
            raise ValueError("bad thread id")
        if not title:
            row = conn.execute("SELECT subject FROM mail_threads WHERE account = ? AND thread_id = ?",
                               (account, ref)).fetchone()
            title = row["subject"] if row else None
    elif kind == "todo":
        if not ref.isdigit():
            raise ValueError("bad todo id")
        row = conn.execute("SELECT account, text FROM engagements WHERE id = ?", (int(ref),)).fetchone()
        if row is None:
            raise ValueError("no such todo")
        if not row["account"]:
            raise ValueError("give the todo an account first")
        if account and db.clean_account(account) != row["account"]:
            raise ValueError("that todo belongs to another account")
        account, title = row["account"], title or row["text"]
    elif kind == "event":
        # an event keeps the account its calendar belongs to; a calendar that
        # maps to no account (a shared or subscribed one) leaves it without one
        account = db.clean_account(account) if account else None
        m = _EVENT_RE.match(ref)
        if not m:
            raise ValueError("bad event — expected date|time|title")
        title = title or m.group(3)
    else:                                   # contact: shared across accounts
        ref = ref.lower()
        if not _EMAIL_RE.match(ref):
            raise ValueError("bad email address")
        account = None
    title = _clean_text(title, 300) or None
    # UNIQUE treats NULL accounts as distinct, so a contact (or an event with
    # no account) is checked here rather than trusted to the index
    have = conn.execute("SELECT * FROM project_items WHERE project_id = ? AND kind = ? "
                        "AND account IS ? AND ref = ?", (int(pid), kind, account, ref)).fetchone()
    if have:
        return {**dict(have), "added": False}
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO project_items (project_id, kind, account, ref, title, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)", (int(pid), kind, account, ref, title, db._now_iso()))
        extra = {"last_account": account} if account and kind in ("thread", "todo") else {}
        _touch(conn, pid, **extra)
    row = conn.execute("SELECT * FROM project_items WHERE project_id = ? AND kind = ? "
                       "AND account IS ? AND ref = ?", (int(pid), kind, account, ref)).fetchone()
    return {**dict(row), "added": cur.rowcount == 1}


def remove_item(conn, pid: int, item_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM project_items WHERE id = ? AND project_id = ?",
                           (int(item_id), int(pid)))
        if cur.rowcount:
            _touch(conn, pid)
    return cur.rowcount > 0


def projects_for_item(conn, kind: str, account: str | None, ref) -> list:
    """Which projects point at this item — for the chip on a row."""
    return [dict(r) for r in conn.execute(
        "SELECT p.id, p.name, p.color FROM project_items i JOIN projects p ON p.id = i.project_id "
        "WHERE i.kind = ? AND i.account IS ? AND i.ref = ? ORDER BY p.name",
        (kind, account, str(ref)))]


# ------------------------------------------------------------------ notes
def note_add(conn, pid: int | None, title: str | None, body: str) -> int:
    if pid is not None:
        _get(conn, pid)
    body = str(body or "")[:50000]
    if not body.strip() and not _clean_text(title, 200):
        raise ValueError("an empty note")
    now = db._now_iso()
    with conn:
        cur = conn.execute("INSERT INTO notes (project_id, title, body, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?)", (pid, _clean_text(title, 200) or None, body, now, now))
        if pid is not None:
            _touch(conn, pid)
    return cur.lastrowid


def note_update(conn, note_id: int, title=None, body=None) -> dict:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone()
    if row is None:
        raise ValueError("no such note")
    sets, vals = ["updated_at = ?"], [db._now_iso()]
    if title is not None:
        sets.append("title = ?"); vals.append(_clean_text(title, 200) or None)
    if body is not None:
        sets.append("body = ?"); vals.append(str(body)[:50000])
    with conn:
        conn.execute(f"UPDATE notes SET {', '.join(sets)} WHERE id = ?", vals + [int(note_id)])
    return dict(conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone())


def note_move(conn, note_id: int, pid: int | None) -> dict:
    """Step 4 (Bart: notes "can also be promoted"): a note sits in at most one
    project; moving it in or back out to Notes never changes its text."""
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone()
    if row is None:
        raise ValueError("no such note")
    if pid is not None:
        _get(conn, pid)
    with conn:
        conn.execute("UPDATE notes SET project_id = ?, updated_at = ? WHERE id = ?",
                     (int(pid) if pid else None, db._now_iso(), int(note_id)))
        for p in {row["project_id"], pid} - {None}:
            _touch(conn, p)
    return dict(conn.execute("SELECT * FROM notes WHERE id = ?", (int(note_id),)).fetchone())


def notes_list(conn, q: str | None = None) -> list:
    """The Notes page: every note, loose or in a project, newest first."""
    sql = ("SELECT n.*, p.name AS project_name, p.color AS project_color FROM notes n "
           "LEFT JOIN projects p ON p.id = n.project_id")
    args: tuple = ()
    q = _clean_text(q, 100)
    if q:
        sql += " WHERE n.title LIKE ? OR n.body LIKE ?"
        args = (f"%{q}%", f"%{q}%")
    return [dict(r) for r in conn.execute(sql + " ORDER BY n.updated_at DESC LIMIT 500", args)]


def note_delete(conn, note_id: int) -> bool:
    """Deleting a note is Bart deleting his own writing, on purpose (the board
    confirms first). It never touches mail."""
    with conn:
        return conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),)).rowcount > 0


# ------------------------------------------------------------------ views
def _counts(conn, pid: int) -> dict:
    c = {k: 0 for k in KINDS}
    for r in conn.execute("SELECT kind, COUNT(*) n FROM project_items WHERE project_id = ? GROUP BY kind", (pid,)):
        c[r["kind"]] = r["n"]
    c["note"] = conn.execute("SELECT COUNT(*) FROM notes WHERE project_id = ?", (pid,)).fetchone()[0]
    t = conn.execute(
        "SELECT COUNT(*) total, SUM(e.state = 'done') done FROM project_items i JOIN engagements e "
        "ON e.id = CAST(i.ref AS INTEGER) AND e.account = i.account "
        "WHERE i.project_id = ? AND i.kind = 'todo' AND e.state != 'dropped'", (pid,)).fetchone()
    c["todos_done"], c["todos_total"] = (t["done"] or 0), (t["total"] or 0)
    return c


def list_projects(conn) -> list:
    out = []
    for r in conn.execute("SELECT * FROM projects"):
        p = dict(r)
        p["counts"] = _counts(conn, p["id"])
        p["accounts"] = [a["account"] for a in conn.execute(
            "SELECT DISTINCT account FROM project_items WHERE project_id = ? AND account IS NOT NULL "
            "ORDER BY account", (p["id"],))]
        out.append(p)
    # open first — pinned in pin order, then by target date, then name — and
    # done last, most recently completed first
    live = sorted((p for p in out if p["status"] == "open"),
                  key=lambda p: (p["pin_rank"] or 99, p["target_date"] or "9999", p["name"].lower()))
    done = sorted((p for p in out if p["status"] == "done"),
                  key=lambda p: p["completed_at"] or "", reverse=True)
    return live + done


def _granola_for(conn, date: str, time: str, title: str) -> dict | None:
    """The Granola notes of a meeting: the same local day, a matching title,
    started within three hours of the calendar time. Only a link and a title
    — the notes themselves stay in Granola."""
    want = title.lower().strip()
    best = None
    for r in conn.execute("SELECT title, link, last_activity FROM elements "
                          "WHERE source = 'granola' AND last_activity >= ? AND last_activity < ?",
                          (_utc_bound(date, -1), _utc_bound(date, 2))):
        from datetime import datetime
        try:
            local = datetime.fromisoformat(r["last_activity"].replace("Z", "+00:00")).astimezone()
        except ValueError:
            continue
        if local.strftime("%Y-%m-%d") != date:
            continue
        got = str(r["title"] or "").lower().strip()
        if not got or not (got == want or got in want or want in got):
            continue
        gap = 0 if time == "all-day" else abs(local.hour * 60 + local.minute
                                              - int(time[:2]) * 60 - int(time[3:]))
        if gap <= 180 and (best is None or gap < best[0]):
            best = (gap, {"title": r["title"], "link": r["link"]})
    return best[1] if best else None


def _utc_bound(date: str, days: int) -> str:
    from datetime import date as _d, timedelta
    return (_d.fromisoformat(date) + timedelta(days=days)).isoformat()


def _suggest_people(conn, items: list, mine=frozenset()) -> list:
    """Bart: a People list "with suggestions" — whoever writes on the
    project's own emails and isn't on it yet. Never Bart himself."""
    have = {i["ref"] for i in items if i["kind"] == "contact"}
    keys = {(i["account"], i["ref"]) for i in items if i["kind"] == "thread"}
    for i in items:
        keys.update((t["account"], t["thread_id"]) for t in i.get("threads") or [])
    seen: dict[str, dict] = {}
    for acct, tid in keys:
        r = conn.execute("SELECT participants, last_from FROM mail_threads WHERE account = ? AND thread_id = ?",
                         (acct, tid)).fetchone()
        if not r:
            continue
        try:
            parts = json.loads(r["participants"] or "[]")
        except ValueError:
            parts = []
        counted = set()                 # once per thread, however often listed
        for raw in parts + [r["last_from"] or ""]:
            m = re.search(r"([^<>\s\"']+@[^<>\s\"']+)", raw)
            if not m:
                continue
            em = m.group(1).lower()
            if em in have or em in mine or not _EMAIL_RE.match(em) or re.match(r"(no-?reply|notifications?|mailer-daemon)@", em):
                continue
            name = re.sub(r"\s*<.*$", "", raw).strip().strip("\"'")
            e = seen.setdefault(em, {"email": em, "name": "", "threads": 0})
            if em not in counted:
                counted.add(em)
                e["threads"] += 1
            if name and "@" not in name and " via " not in name:
                e["name"] = e["name"] or name
    for em, e in seen.items():
        c = conn.execute("SELECT name, hidden FROM contacts WHERE email = ?", (em,)).fetchone()
        if c and c["name"]:
            e["name"] = c["name"]
    return sorted(seen.values(), key=lambda e: (-e["threads"], e["name"] or e["email"]))[:8]


def project_view(conn, pid: int, mine=frozenset()) -> dict:
    """One project with every item resolved from where it lives."""
    p = _get(conn, pid)
    p["counts"] = _counts(conn, pid)
    items = [dict(r) for r in conn.execute(
        "SELECT * FROM project_items WHERE project_id = ? ORDER BY added_at DESC", (int(pid),))]
    todo_threads = set()
    for it in items:
        if it["kind"] == "todo":
            e = conn.execute("SELECT id, text, state, due, account, done_at FROM engagements WHERE id = ?",
                             (int(it["ref"]),)).fetchone()
            it["todo"] = dict(e) if e else None
            # Bart: a todo's emails show folded inside it — all of them, with
            # enough to recognise each (subject, sender, date)
            it["threads"] = [dict(t) for t in conn.execute(
                "SELECT et.account, et.thread_id, m.subject, m.last_from, m.last_date FROM engagement_threads et "
                "LEFT JOIN mail_threads m ON m.account = et.account AND m.thread_id = et.thread_id "
                "WHERE et.engagement_id = ? ORDER BY m.last_date DESC", (int(it["ref"]),))]
            todo_threads.update((t["account"], t["thread_id"]) for t in it["threads"])
    for it in items:
        if it["kind"] == "thread":
            m = conn.execute(
                "SELECT subject, last_from, last_from_email, snippet, last_date, unread, starred, "
                "in_inbox, message_count, last_direction FROM mail_threads WHERE account = ? AND thread_id = ?",
                (it["account"], it["ref"])).fetchone()
            it["mail"] = dict(m) if m else None
            # Bart: a todo's emails show once, folded inside the todo
            it["in_todo"] = (it["account"], it["ref"]) in todo_threads
        elif it["kind"] == "contact":
            c = conn.execute("SELECT name, notes FROM contacts WHERE email = ?", (it["ref"],)).fetchone()
            it["contact"] = dict(c) if c else None
        elif it["kind"] == "event":
            m = _EVENT_RE.match(it["ref"])
            if m:
                it["date"], it["time"] = m.group(1), m.group(2)
                it["granola"] = _granola_for(conn, m.group(1), m.group(2), m.group(3))
    p["items"] = items
    p["suggest_people"] = _suggest_people(conn, items, mine)
    p["notes"] = [dict(r) for r in conn.execute(
        "SELECT * FROM notes WHERE project_id = ? ORDER BY updated_at DESC", (int(pid),))]
    p["accounts"] = sorted({i["account"] for i in items if i["account"]})
    p["open_todos"] = open_todos(conn, pid)
    return p


def main(argv) -> int:
    conn = db.connect()
    try:
        if len(argv) > 1:
            print(json.dumps(project_view(conn, int(argv[1])), indent=1, default=str))
        else:
            for p in list_projects(conn):
                pin_mark = f"📌{p['pin_rank']} " if p["pin_rank"] else ""
                print(f"{p['id']:>3} {pin_mark}{p['name']} [{p['status']}] {p['counts']} {p['accounts']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
