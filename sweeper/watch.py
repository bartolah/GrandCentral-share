"""Following up on emails "of note" — E276 (Casey 2026-10-07).

Casey: "help me keep track of and follow up on emails 'of note' — assignments
to my team members that I want them to do, or sales or partner emails, or
things I need to remember to do. I don't want it to be too heavy handed, or
crazy from a processing and token standpoint. perhaps after I send an email,
part of the after send menu can ask me for context?" — then "Go for it" on
the plan (defaults: 3 business days for the team, 5 days for deals, Remind me
= a todo, ask when a teammate or someone outside is on it).

A watch is something Casey chose to keep an eye on — never created by itself:
  assigned  a teammate owes him something on this thread
  deal      a prospect or partner owes him a reply
  remind    "remind me": a todo holding the email (this row only exists until
            a NEW email's thread id is known, so the todo can hold it)
No model runs here. check() (every mail_sync) closes a watch when the reply
it waits for arrives, finds new emails' threads by Message-ID, and marks the
rest due when their date passes. Nudging (the only AI) happens in the
composer, on Casey's click.

    .venv/bin/python sweeper/watch.py              # open watches, all accounts
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta, timezone

import db

KINDS = ("assigned", "deal", "remind")
DUE_DAYS = {"assigned": ("bdays", 3), "deal": ("days", 5)}
ASK_MODES = ("smart", "always", "never")
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_AUTO = re.compile(r"^(no-?reply|noreply|notifications?|mailer-daemon|updates?|news(letter)?)@", re.I)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def default_due(kind: str, start: date | None = None) -> str | None:
    rule = DUE_DAYS.get(kind)
    if not rule:
        return None
    d = start or datetime.now().astimezone().date()
    unit, n = rule
    if unit == "days":
        return (d + timedelta(days=n)).isoformat()
    while n:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d.isoformat()


# ------------------------------------------------------------------ settings
def ask_mode(conn, account: str) -> str:
    row = conn.execute("SELECT ask FROM watch_settings WHERE account = ?", (account,)).fetchone()
    return row["ask"] if row and row["ask"] in ASK_MODES else "smart"


def set_ask_mode(conn, account: str, mode: str) -> str:
    account = db.clean_account(account)
    if mode not in ASK_MODES:
        raise ValueError(f"ask must be one of {', '.join(ASK_MODES)}")
    with conn:
        conn.execute("INSERT INTO watch_settings (account, ask) VALUES (?, ?) "
                     "ON CONFLICT(account) DO UPDATE SET ask = excluded.ask", (account, mode))
    return mode


# ------------------------------------------------------------------ the after-send suggestion
def _addrs(*fields) -> list:
    from email.utils import getaddresses
    return [(n.strip().strip('"'), a.lower()) for n, a in getaddresses([f for f in fields if f]) if "@" in a]


def suggest(conn, account: str, env: dict, to: str = "", cc: str = "", thread_id: str | None = None) -> dict:
    """What the after-send row offers, from rules only: whether to ask at all,
    the likely kind, and the teammates on the email to assign to."""
    import mailclass
    own = {a.lower() for a in mailclass.own_addresses(env)}
    people = [(n, a) for n, a in _addrs(to, cc) if a not in own and not _AUTO.search(a)]
    roster = {r["email"].lower(): r for r in conn.execute("SELECT email, name, teammate FROM class_people")}
    groups = {r["domain"] for r in conn.execute("SELECT domain FROM class_groups")}
    try:
        from mail_sync import own_domains
        groups |= {d.lower() for d in own_domains(env)}
    except Exception:
        pass
    mates = [{"email": a, "name": (roster.get(a) and roster[a]["name"]) or n or a.split("@")[0]}
             for n, a in people if (roster.get(a) and roster[a]["teammate"]) or a.rpartition("@")[2] in groups]
    outside = [(n, a) for n, a in people if a not in {m["email"] for m in mates}]
    mode = ask_mode(conn, account)
    ask = mode == "always" or (mode == "smart" and bool(mates or outside))
    kind = None
    if mates and not outside:
        kind = "assigned"
    else:
        try:
            import tiers
            tagged = tiers.tiers(conn)
        except Exception:
            tagged = {}
        dealish = any(tagged.get(a) in ("vip", "prospect") for _n, a in outside)
        if not dealish and thread_id:
            c = db.class_for_threads(conn, account).get(thread_id) or {}
            dealish = c.get("type") in ("Prospect", "Sales/outreach")
        if dealish:
            kind = "deal"
    main = outside[0] if outside else None
    return {"ask": ask, "mode": mode, "kind": kind, "teammates": mates,
            "contact": {"email": main[1], "name": main[0] or main[1].split("@")[0]} if main else None,
            "due": {k: default_due(k) for k in ("assigned", "deal")}}


# ------------------------------------------------------------------ writing
def add(conn, account: str, kind: str, thread_id: str = "", message_id: str | None = None,
        who: str | None = None, who_name: str | None = None, note: str | None = None,
        due: str | None = None, subject: str | None = None, todo_id: int | None = None) -> int:
    account = db.clean_account(account)
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    thread_id = str(thread_id or "").strip()
    if thread_id and not thread_id.isdigit():
        raise ValueError("bad thread id")
    if not thread_id and not message_id:
        raise ValueError("a thread or the sent message is required")
    if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise ValueError("due must be YYYY-MM-DD")
    who = (who or "").strip().lower() or None
    if kind == "assigned" and not who:
        raise ValueError("who is it assigned to?")
    if who and not _EMAIL.fullmatch(who):
        raise ValueError("bad email address")
    with conn:
        if thread_id:   # one open watch of a kind per thread: a second click updates it
            conn.execute("UPDATE watches SET status = 'replaced', closed_at = ? WHERE account = ? "
                         "AND thread_id = ? AND kind = ? AND status = 'open'", (_now(), account, thread_id, kind))
        cur = conn.execute(
            "INSERT INTO watches (account, thread_id, message_id, kind, who, who_name, note, due, subject, "
            "todo_id, status, sent_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?, 'open', ?, ?)",
            (account, thread_id, message_id, kind, who, (who_name or "").strip()[:80] or None,
             (note or "").strip()[:300] or None, due or default_due(kind), (subject or "").strip()[:200] or None,
             todo_id, _now(), _now()))
    return cur.lastrowid


def _get(conn, wid: int):
    row = conn.execute("SELECT * FROM watches WHERE id = ?", (int(wid),)).fetchone()
    if row is None:
        raise ValueError("no such follow-up")
    return row


def done(conn, wid: int, reason: str = "done") -> None:
    _get(conn, wid)
    with conn:
        conn.execute("UPDATE watches SET status = 'closed', closed_reason = ?, closed_at = ? WHERE id = ?",
                     (reason, _now(), int(wid)))


def edit(conn, wid: int, note: str | None = None, due: str | None = None) -> None:
    """The note or date Casey adds after one tap saved the defaults."""
    _get(conn, wid)
    if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
        raise ValueError("due must be YYYY-MM-DD")
    with conn:
        if note is not None:
            conn.execute("UPDATE watches SET note = ? WHERE id = ?", (note.strip()[:300] or None, int(wid)))
        if due:
            conn.execute("UPDATE watches SET due = ? WHERE id = ?", (due, int(wid)))


def push(conn, wid: int, days: int = 2) -> str:
    """'+2 days': move the date on from today (or from the date, if later)."""
    w = _get(conn, wid)
    base = max(_today(), w["due"] or _today())
    new = (date.fromisoformat(base) + timedelta(days=max(1, min(int(days), 60)))).isoformat()
    with conn:
        conn.execute("UPDATE watches SET due = ?, status = 'open' WHERE id = ?", (new, int(wid)))
    return new


# ------------------------------------------------------------------ the check (every mail_sync)
def _replied(conn, w, own: set) -> bool:
    """Did the reply this watch waits for arrive after Casey's send?"""
    since = w["sent_at"] or ""
    want = (lambda a: a == w["who"]) if w["kind"] == "assigned" else (lambda a: a not in own)
    t = conn.execute("SELECT last_from_email, last_direction, last_date FROM mail_threads "
                     "WHERE account = ? AND thread_id = ?", (w["account"], w["thread_id"])).fetchone()
    if t and t["last_direction"] == "inbound" and (t["last_date"] or "") >= since \
            and want((t["last_from_email"] or "").lower()):
        return True
    try:   # an earlier reply, if the thread's bodies are cached
        import mail_cache
        hit = mail_cache.cached(conn, w["account"], w["thread_id"]) or {}
        for m in hit.get("messages") or []:
            frm = (_EMAIL.search(str(m.get("from") or "")) or [None])[0]
            stamp = str(m.get("date") or "").replace("+00:00", "Z")
            if frm and want(frm.lower()) and stamp[:19] >= since[:19]:
                return True
    except Exception:
        pass
    return False


def resolve(conn, env: dict, log=print, limit: int = 5) -> int:
    """A NEW email gets its thread id from Gmail after it's sent: find it by
    the Message-ID Grand Central set. A Remind-me todo then holds the email."""
    import mail_common as mc
    rows = conn.execute("SELECT * FROM watches WHERE thread_id = '' AND message_id IS NOT NULL "
                        "AND status = 'open' ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    found = 0
    for w in rows:
        try:
            imap, _u = mc.connect(w["account"], env)
            try:
                box = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
                imap.select(f'"{box}"', readonly=True)
                typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", w["message_id"])
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                if not uids:
                    continue
                typ, data = imap.uid("FETCH", uids[-1], "(X-GM-THRID)")
                raw = data[0] if isinstance(data[0], bytes) else data[0][0]
                m = re.search(rb"X-GM-THRID (\d+)", raw)
            finally:
                imap.logout()
        except Exception as e:
            log(f"watch: thread lookup failed ({e!r})")
            continue
        if not m:
            continue
        tid = m.group(1).decode()
        with conn:
            conn.execute("UPDATE watches SET thread_id = ? WHERE id = ?", (tid, w["id"]))
        if w["kind"] == "remind" and w["todo_id"]:
            try:
                db.link_thread(conn, int(w["todo_id"]), w["account"], tid, add=True)
                done(conn, w["id"], "linked")
            except Exception as e:
                log(f"watch: linking the remind-me todo failed ({e!r})")
        found += 1
    return found


def check(conn, env: dict, log=print) -> dict:
    """Close what got its reply; report what's due. No model, no network
    except the Message-ID lookups for brand-new emails (a few a day)."""
    import mailclass
    own = {a.lower() for a in mailclass.own_addresses(env)}
    try:
        resolve(conn, env, log=log)
    except Exception as e:
        log(f"watch: resolve failed ({e!r})")
    closed = 0
    for w in conn.execute("SELECT * FROM watches WHERE status = 'open' AND kind != 'remind' AND thread_id != ''").fetchall():
        if _replied(conn, w, own):
            done(conn, w["id"], "replied")
            closed += 1
    if closed:
        log(f"watch: {closed} follow-up(s) closed by a reply")
    return {"closed": closed}


# ------------------------------------------------------------------ reading
def listing(conn, account: str | None = None, include_closed: bool = False) -> list:
    """Open watches (and recently closed ones if asked), each with its thread
    row and whether it's due, newest-due first."""
    sql = "SELECT * FROM watches WHERE kind != 'remind'"
    args: list = []
    if account:
        sql += " AND account = ?"
        args.append(db.clean_account(account))
    sql += " AND (status = 'open'" + (" OR (status = 'closed' AND closed_at > ?)" if include_closed else "") + ")"
    if include_closed:
        args.append((datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    today = _today()
    out = []
    for w in conn.execute(sql + " ORDER BY due", args):
        d = dict(w)
        t = conn.execute("SELECT subject, last_from, last_date, last_direction, unread, in_inbox FROM mail_threads "
                         "WHERE account = ? AND thread_id = ?", (w["account"], w["thread_id"])).fetchone() \
            if w["thread_id"] else None
        d["thread"] = dict(t) if t else None
        d["due_now"] = w["status"] == "open" and bool(w["due"]) and w["due"] <= today
        d["age_days"] = (datetime.now(timezone.utc) - datetime.fromisoformat(w["sent_at"].replace("Z", "+00:00"))).days \
            if w["sent_at"] else None
        out.append(d)
    return out


def open_assignments(conn, who: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM watches WHERE kind = 'assigned' AND status = 'open' AND who = ?",
                        ((who or "").lower(),)).fetchone()[0]


def label(w: dict) -> str:
    """One line for Home and the morning brief."""
    who = w.get("who_name") or (w.get("who") or "").split("@")[0] or "them"
    what = w.get("note") or (w.get("thread") or {}).get("subject") or w.get("subject") or "this"
    if w["kind"] == "assigned":
        return f"👤 {who} owes you: {what}"
    return f"💼 Waiting on {who}: {what}" if w.get("who") else f"💼 Waiting on a reply: {what}"


def main() -> int:
    conn = db.connect()
    try:
        for w in listing(conn):
            flag = "DUE " if w["due_now"] else "    "
            print(f"{flag}{w['due']}  {w['account']:<18} {label(w)[:80]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
