"""E249 — what Home shows for one account, read-only.

Casey, 2026-09-29, after the Home mockup: a new Home tab shows every account at
once, each in its own collapsible section, and the same two-column design is
each account's own Home. BUILD_SPEC §6 (same day) allows that view on one
condition: it is a view, never an owner. So this module only ever reads, and
it builds one account at a time:

    section(conn, "brightline")  ->  that account's counts, up to 5
                                      top-scored emails, up to 5 replies or
                                      follow-ups, the 5 newest real emails,
                                      a 10-entry bundled mini inbox,
                                      todos due today, overdue count

E268 (Casey, 2026-10-02: "two tabs for each Account, the default should just
be the most recent 'real' emails (non bundled), the second tab should be
'Needs You'"): `recent` feeds the first tab; `top` + `follows` are Needs You.

The cross-account Home is simply a list of sections, one call per account.
Nothing here joins across accounts, and every row names the account it
belongs to, so opening or acting on it happens inside that account.

Calendar events belong to a Google calendar, not a mail account.
CAL_ACCOUNT maps the calendar_reader's labels onto the accounts they match;
an event whose calendar maps to no account is drawn without one.

    ../.venv/bin/python home_view.py brightline     # print one section
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import db

TOP_N = 5            # Casey: "no more than 5" of each
TOP_MIN_VALUE = 40   # E217's bands: 70+ solid, 40-69 light; below that is not "top"
# The inbox's bundles (index.html GRP_KIND / JEV_TYPE_GRP): what is not "real" mail
BUNDLED_CATEGORIES = {"notification", "promo", "newsletter", "receipt", "calendar"}
JEV_TYPE_GRP = {"Cold pitch": "pitch", "Spam": "spam", "Update": "notification", "Newsletter": "newsletter"}
JEV_BUNDLED_TYPES = set(JEV_TYPE_GRP)
INBOX_N = 10         # Dev #70: the Recent tab is a small inbox, ~10 entries
BUNDLE_NAME = {"notification": "Updates", "promo": "Promotions", "newsletter": "Newsletters",
               "receipt": "Receipts", "calendar": "Calendar", "pitch": "Cold pitches", "spam": "Spam"}

# calendar_reader._cal_label -> the account that calendar belongs to
CAL_ACCOUNT = {"work": "brightline", "fieldhouse": "fieldhouse", "personal": "personal"}


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _days_since(iso_z: str | None) -> int | None:
    try:
        dt = datetime.strptime(iso_z or "", "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return max(0, (datetime.now(timezone.utc) - dt).days)


def _row(t: dict, c: dict) -> dict:
    """One email as Home draws it: the three-line row's fields plus its score."""
    return {"account": t["account"], "thread_id": t["thread_id"],
            "from": t.get("who") or t.get("last_from") or "",
            "subject": t.get("subject") or "(no subject)",
            "snippet": (t.get("snippet") or "")[:200],
            "last_date": t.get("last_date"), "unread": bool(t.get("unread")),
            "value": c.get("value"), "respond": c.get("respond"),
            "follow_up": c.get("follow_up"), "waiting_on": c.get("waiting_on")}


def _follow_state(t: dict, c: dict, today: str) -> tuple[str, str] | None:
    """('owed'|'chase', label) when this thread wants Casey's move, else None."""
    age = _days_since(t.get("last_date"))
    age_s = f"{age}d" if age else "today"
    if (c.get("respond") == "yes" and t.get("last_direction") != "outbound") or c.get("waiting_on") == "me":
        return "owed", f"Reply owed · {age_s}"
    fu = c.get("follow_up")
    # Dev #59: "me" is Casey's own move (above), never someone to chase
    if fu and fu <= today and c.get("waiting_on", "none") not in ("none", "me"):
        return "chase", f"Follow up · asked {age_s} ago" if age else "Follow up · asked today"
    return None


def _bundle_key(t: dict, c: dict, jev_on: bool, svc_table: dict) -> str | None:
    """The inbox bundle this email folds into, or None when the inbox shows
    it as itself — the same three calls grpKey makes: Casey's own services
    (website RFPs excepted: they are leads), the categorizer's bulk
    categories, and on a Jev account the classifier's bundle types."""
    if svc_table is not None and t.get("service"):
        import services
        k = services.resolve(t.get("service"), svc_table)
        if k and svc_table[k].get("kind") != "sales":
            return "svc:" + k
    cat = t.get("category") or "people"
    if cat in BUNDLED_CATEGORIES:
        return cat
    if jev_on and c.get("type_src") in ("jev", "you") and c.get("type") in JEV_BUNDLED_TYPES:
        return JEV_TYPE_GRP[c["type"]]
    return None


def _is_real(t: dict, c: dict, jev_on: bool, svc_table: dict) -> bool:
    """An email the inbox shows as itself, not folded into a bundle."""
    return _bundle_key(t, c, jev_on, svc_table) is None


def _mini_inbox(newest: list, cls: dict, jev_on: bool, svc_table: dict) -> list:
    """Dev #70 (Casey: "show like the inbox, but just smaller, 10 or so
    emails, bundled"): the newest INBOX_N entries, where a bundle is ONE
    entry at the height of its newest message, as in the inbox. A bundle
    carries its threads (newest first) so it can open in place."""
    out, bundles = [], {}
    for t in newest:
        c = cls.get(t["thread_id"]) or {}
        k = _bundle_key(t, c, jev_on, svc_table)
        if k is None:
            out.append(_row(t, c))
        elif k in bundles:
            bundles[k]["rows"].append(_row(t, c))
        else:
            name = (svc_table[k[4:]]["label"] if k.startswith("svc:") and svc_table
                    else BUNDLE_NAME.get(k, k))
            bundles[k] = {"bundle": k, "name": name, "account": t["account"],
                          "last_date": t.get("last_date"), "rows": [_row(t, c)]}
            out.append(bundles[k])
    out = out[:INBOX_N]
    for e in out:
        if "bundle" in e:
            e["count"] = len(e["rows"])
            e["unread"] = sum(1 for r in e["rows"] if r["unread"])
            e["rows"] = e["rows"][:25]
    return out


def section(conn, account: str, today: str | None = None) -> dict:
    """One account's Home section. Reads only this account's rows."""
    account = db.clean_account(account)
    today = today or _today()

    todos = [t for t in db.todo_view(conn, account=account)
             if t.get("account") == account and t.get("state") == "open"]
    # a thread filed under a todo lives with the todo, not in the inbox
    attached = {th.get("thread_id") for t in todos for th in (t.get("threads") or [])}

    threads = [t for t in db.mail_view(conn, account=account)
               if t.get("account") == account and t.get("in_inbox")
               and not t.get("moved_at") and not t.get("done_at")
               and t.get("thread_id") not in attached
               and not db.is_dismissed(conn, account, t["thread_id"])]
    cls = db.class_for_threads(conn, account)

    scored = sorted(((t, cls.get(t["thread_id"]) or {}) for t in threads),
                    key=lambda tc: (-(tc[1].get("value") or 0), tc[0].get("last_date") or ""))
    top = [_row(t, c) for t, c in scored
           if (c.get("value") or 0) >= TOP_MIN_VALUE][:TOP_N]
    shown = {r["thread_id"] for r in top}

    # E269: "opened, no reply after 3 days" (tracking, if that account feeds
    # Waiting on) is the strongest reason to follow up
    try:
        import tracking
        trk = tracking.summary(conn, account)
    except Exception:
        trk = {}
    follows = []
    for t, c in scored:
        if t["thread_id"] in shown:
            continue
        st = _follow_state(t, c, today)
        tk = trk.get(t["thread_id"])
        if tk and tk.get("nudge"):
            d = _days_since(tk.get("last_sent"))
            st = ("chase", f"Opened, no reply · sent {d}d ago" if d else "Opened, no reply")
        if st:
            follows.append({**_row(t, c), "kind": st[0], "state": st[1]})
        if len(follows) == TOP_N:
            break
    # E276: follow-ups Casey chose that are due now come first among the
    # reasons to reach out (and show on the all-accounts Home too)
    try:
        import watch
        have = {f["thread_id"] for f in follows} | shown
        due = [w for w in watch.listing(conn, account) if w["due_now"]]
        for w in due:
            t = conn.execute("SELECT * FROM mail_threads WHERE account = ? AND thread_id = ?",
                             (account, w["thread_id"])).fetchone() if w["thread_id"] else None
            row = _row(dict(t), cls.get(w["thread_id"]) or {}) if t else {
                "account": account, "thread_id": w["thread_id"], "from": w.get("who_name") or w.get("who") or "",
                "subject": w.get("subject") or "", "snippet": "", "last_date": w["sent_at"], "unread": False,
                "value": None, "respond": None, "follow_up": w["due"], "waiting_on": None}
            follows.insert(0, {**row, "kind": "chase", "watch_id": w["id"],
                               "state": f"{watch.label(w)[:70]} · due {w['due'][5:]}"})
            have.add(w["thread_id"])
    except Exception:
        pass
    # Dev #66: a Top Prospect gone quiet for 5+ days is a reason to reach out
    try:
        import tiers
        have = {f["thread_id"] for f in follows} | shown
        for q in tiers.quiet_prospects(conn, account):
            t = q["thread"]
            if t["thread_id"] in have or len(follows) >= TOP_N + 2:
                continue
            follows.append({**_row(t, cls.get(t["thread_id"]) or {}), "kind": "chase",
                            "state": f"🎯 Top prospect · no contact in {q['days']}d"})
    except Exception:
        pass

    try:
        import services
        svc_table = services.table(conn)
    except Exception:
        svc_table = None
    jev_on = db.class_account_jev(conn, account)
    newest = sorted(threads, key=lambda t: t.get("last_date") or "", reverse=True)
    recent = [_row(t, cls.get(t["thread_id"]) or {}) for t in newest
              if _is_real(t, cls.get(t["thread_id"]) or {}, jev_on, svc_table)][:TOP_N]
    inbox = _mini_inbox(newest, cls, jev_on, svc_table)

    live_todos = [t for t in todos if not t.get("snoozed_active")]
    due = [{"id": t["id"], "text": t.get("text") or "", "account": account}
           for t in live_todos if t.get("due") == today or (t.get("my_day") or "")[:10] == today]
    overdue = [t for t in live_todos if t.get("due") and t["due"] < today]

    return {"account": account,
            "counts": {"emails": len(threads), "todos_due": len(due), "overdue": len(overdue)},
            "top": top, "follows": follows, "recent": recent, "inbox": inbox, "due": due,
            "overdue_oldest": min((t["due"] for t in overdue), default=None)}


def home(conn, accounts: list, today: str | None = None) -> dict:
    """The cross-account Home: one independent section per account."""
    today = today or _today()
    return {"date": today, "cal_account": CAL_ACCOUNT,
            "sections": [section(conn, a, today) for a in accounts]}


if __name__ == "__main__":
    c = db.connect()
    try:
        print(json.dumps(section(c, sys.argv[1] if len(sys.argv) > 1 else "personal"), indent=2))
    finally:
        c.close()
