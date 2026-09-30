"""E209 — email classification, phase 1: the rules.

Bart (2026-09-26): classify each email — what type it is, who the sender is to
him (teammate, the one-company group, someone he writes to a lot), whether he
should respond, whether he is waiting on someone, and when to follow up — and
let him tune it. Plan: the "Email classification with Jev" doc.

Phase 1 decides everything a rule can decide from facts already in the DB,
with no model call:

  type          from mail_sync's category; person-to-person mail splits into
                Team collaboration (teammate / company group) or Correspondence
  relationship  teammate > company > frequent > known > new, from the team
                roster (Settings → Classification) and this account's own
                history — never another account's
  respond       'no' for bulk and machine mail and when Bart wrote last;
                person mail he hasn't answered stays 'unsure' until a model
                judges it (the sweep's assessor today, Jev in phase 2)
  waiting_on    Bart wrote last: 'teammate' or 'outside', naming who
  follow_up     a date: waiting on a teammate +2 business days, outside +4
                days; owing a reply +1 business day to a teammate, +2 to others

Rows are keyed (account, thread_id) and re-classified only when the thread's
fingerprint (last message date + count), the ruleset version or the roster
changes, or the row is a day old (frequent-contact counts drift slowly).

Corrections (class_feedback) win: the latest per field is applied over
whatever a rule or a model says, so a re-run never overwrites Bart's answer.

Jev (phase 2) may see an account's mail only when that account's switch is on
AND a Jev key is set — jev_allowed() is the one gate. §6, 2026-09-26: subject,
sender/recipients, direction and a <=400-char snippet only, per account.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import db

VERSION = "rules-4"   # rules-4: website RFPs (services kind "sales") are Prospects

TYPES = ("Correspondence", "Team collaboration", "Sales/outreach", "Update",
         "Newsletter", "Receipt", "Calendar", "Promo", "Other")
RELATIONSHIPS = ("teammate", "company", "frequent", "known", "new", "self", "machine")
RESPONDS = ("yes", "no", "optional", "unsure")
WAITING = ("none", "teammate", "outside")
FIELDS = ("type", "relationship", "respond", "priority", "waiting_on", "follow_up")

# mail_sync.categorize() → type. 'people' is split by relationship below.
_CATEGORY_TYPE = {"newsletter": "Newsletter", "notification": "Update",
                  "receipt": "Receipt", "calendar": "Calendar", "promo": "Promo"}

# Follow-up defaults (the plan's table; editable later in Settings).
WAIT_TEAMMATE_BDAYS = 2
WAIT_OUTSIDE_DAYS = 4
OWE_TEAMMATE_BDAYS = 1
OWE_OTHER_BDAYS = 2
WAIT_MAX_DAYS = 30          # older than this, stop tracking (matches waiting_on)

FREQUENT_MIN = 3            # threads Bart wrote last on, this account, 90 days
LOOKBACK_DAYS = 30          # classify threads active in this window
STALE_AFTER = timedelta(days=1)

_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


# ---------------------------------------------------------------- helpers
def _addr(s: str) -> str:
    m = _EMAIL_RE.search(s or "")
    return m.group(0).lower() if m else ""


def _domain(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat((ts or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def add_business_days(start: datetime, n: int) -> datetime:
    d = start
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def own_addresses(env: dict) -> set:
    """Every mailbox address Grand Central reads is Bart's."""
    from sources import gmail_reader
    return {(env.get(uk) or "").strip().lower()
            for uk, _ in gmail_reader.scan_accounts(env).values() if env.get(uk)}


# ---------------------------------------------------------------- roster
def roster(conn) -> dict:
    """{'teammates': {email: name}, 'members': {email: group},
    'domains': {domain: group}} + a hash of all of it."""
    people = conn.execute("SELECT email, name, grp, teammate FROM class_people").fetchall()
    mates = {r["email"]: r["name"] or "" for r in people if r["teammate"]}
    # 2026-09-30: a person can be in several groups (Chris owns three of the
    # companies); each account shows the one that is its own.
    members = db.class_memberships(conn)
    for r in people:
        if r["grp"] and r["email"] not in members:
            members[r["email"]] = [r["grp"]]
    doms = {r["domain"]: r["grp"] for r in conn.execute(
        "SELECT domain, grp FROM class_groups")}
    h = hashlib.sha1(json.dumps([sorted(mates.items()), sorted(members.items()),
                                 sorted(doms.items())]).encode()).hexdigest()[:10]
    return {"teammates": mates, "members": members, "domains": doms, "hash": h}


def group_of(email: str, ros: dict, prefer: str | None = None) -> str | None:
    """The company group an address belongs to: its own entry, else its
    domain. Someone in several groups is named by `prefer` — the group of the
    account being read — when they are in it, else by their first."""
    gs = ros.get("members", {}).get(email)
    if isinstance(gs, str):
        gs = [gs]
    if gs:
        return prefer if prefer in gs else gs[0]
    return ros["domains"].get(_domain(email))


def account_group(account: str, env: dict, ros: dict) -> str | None:
    """The group an account belongs to: the group of its own address's domain."""
    addr = (env.get(f"GMAIL_{str(account).upper()}_USER") or "").lower()
    return ros["domains"].get(_domain(addr)) if addr else None


def frequent_counts(conn, account: str, days: int = 90) -> dict:
    """{email: threads in THIS account where Bart wrote last and they were on it}.

    mail_sends only sees sends made from Grand Central; a thread Bart answered
    last in Gmail is the broader signal. Per account on purpose.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    counts: dict[str, int] = {}
    for r in conn.execute(
            "SELECT participants FROM mail_threads WHERE account = ? AND "
            "last_direction = 'outbound' AND last_date >= ?", (account, since)):
        try:
            people = json.loads(r["participants"] or "[]")
        except ValueError:
            continue
        for e in {_addr(p) for p in people}:
            if e:
                counts[e] = counts.get(e, 0) + 1
    return counts


def relationship_of(email: str, ros: dict, freq: dict, own: set) -> str:
    if not email:
        return "new"
    if email in own:
        return "self"
    if email in ros["teammates"]:
        return "teammate"
    if group_of(email, ros):
        return "company"
    n = freq.get(email, 0)
    if n >= FREQUENT_MIN:
        return "frequent"
    return "known" if n else "new"


_REL_RANK = {r: i for i, r in enumerate(("teammate", "company", "frequent", "known", "new"))}


# ---------------------------------------------------------------- the rules
def classify(row: dict, ros: dict, freq: dict, own: set, acct_group: str | None = None,
             svc_kinds: dict | None = None) -> dict:
    """One mail_threads row → the rule fields, reasons and a confidence."""
    cat = row.get("category") or "other"
    direction = row.get("last_direction") or "inbound"
    bulk = bool(row.get("bulk"))
    last = _parse(row.get("last_date")) or datetime.now(timezone.utc)
    try:
        people = json.loads(row.get("participants") or "[]")
    except ValueError:
        people = []
    others = [a for a in (_addr(p) for p in people) if a and a not in own]
    names = {_addr(p): p.split("<")[0].strip().strip('"') for p in people if "<" in p}
    sender = (row.get("last_from_email") or "").lower()
    reasons: list[str] = []

    # who: the closest relationship among the other people on the thread
    if direction == "inbound":
        rel = relationship_of(sender, ros, freq, own)
        if rel == "self":
            rel = "new"
    else:
        rels = [relationship_of(a, ros, freq, own) for a in others] or ["new"]
        rel = min(rels, key=lambda r: _REL_RANK.get(r, 9))
    machine = bulk or cat not in ("people", "other")
    if machine and rel in ("known", "new"):
        rel = "machine"

    # type
    if cat in _CATEGORY_TYPE:
        typ = _CATEGORY_TYPE[cat]
        reasons.append(f"sorted as {cat} by the inbox rules")
    elif cat == "people" and not bulk:
        typ = "Team collaboration" if rel in ("teammate", "company") else "Correspondence"
        reasons.append("person-to-person mail" + (" within your team" if typ != "Correspondence" else ""))
    else:
        typ = "Other"

    # 2026-09-30: a website RFP through the sales@ group is a lead — a
    # Prospect, never a teammate's note (sales@ itself is on the roster)
    sales = (svc_kinds or {}).get(row.get("service")) == "sales"
    if sales:
        typ, rel = "Prospect", "new"
        reasons.insert(0, "a sales inquiry from your website")

    who = sender if direction == "inbound" else next(
        (a for a in others if relationship_of(a, ros, freq, own) == rel), "")
    out = {"type": typ, "relationship": rel, "group": group_of(who, ros, acct_group) if who else None,
           "sales_inquiry": sales,
           "respond": "unsure", "priority": None,
           "waiting_on": "none", "waiting_who": None, "follow_up": None}
    conf = 0.9

    if machine:
        out["respond"] = "no"
        reasons.append("bulk or automated mail doesn't need a reply")
    elif direction == "outbound":
        out["respond"] = "no"
        age = (datetime.now(timezone.utc) - last).days
        target = [a for a in others if relationship_of(a, ros, freq, own) == "teammate"]
        if age <= WAIT_MAX_DAYS and others:
            if target:
                out["waiting_on"], out["waiting_who"] = "teammate", (ros["teammates"].get(target[0])
                                                                      or names.get(target[0]) or target[0])
                due = add_business_days(last, WAIT_TEAMMATE_BDAYS)
            else:
                out["waiting_on"], out["waiting_who"] = "outside", names.get(others[0]) or others[0]
                due = last + timedelta(days=WAIT_OUTSIDE_DAYS)
            out["follow_up"] = due.strftime("%Y-%m-%d")
            reasons.append(f"you wrote last — waiting on {out['waiting_who']}")
    else:
        # a person wrote last and Bart hasn't answered: owed, pending judgement
        conf = 0.5
        due = add_business_days(last, OWE_TEAMMATE_BDAYS if rel == "teammate" else OWE_OTHER_BDAYS)
        out["follow_up"] = due.strftime("%Y-%m-%d")
        reasons.append("they wrote last; whether it needs an answer is a judgement call")

    if rel in ("teammate", "company"):
        reasons.append("teammate" if rel == "teammate" else "from your company group")
    elif rel == "frequent":
        reasons.append("someone you write to often in this account")
    out["reasons"] = reasons[:3]
    out["confidence"] = conf
    return out


def _fingerprint(row: dict) -> str:
    return f"{row.get('last_date') or ''}|{row.get('message_count') or 0}"


# ---------------------------------------------------------------- the pass
def run(conn, env: dict, accounts: list, log=print, now: datetime | None = None) -> dict:
    """Classify changed threads in each account. Returns counts per account."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = (now - STALE_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
    ros = roster(conn)
    own = own_addresses(env)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    done: dict[str, int] = {}
    for account in accounts:
        freq = frequent_counts(conn, account)
        acct_group = account_group(account, env, ros)
        svc_kinds = {r["key"]: r["kind"] for r in conn.execute("SELECT key, kind FROM services")}
        have = {r["thread_id"]: r for r in conn.execute(
            "SELECT thread_id, fingerprint, version, roster, classified_at FROM mail_class "
            "WHERE account = ?", (account,))}
        n = 0
        rows = conn.execute(
            "SELECT * FROM mail_threads WHERE account = ? AND last_date >= ?",
            (account, since)).fetchall()
        with conn:
            for r in rows:
                row = dict(r)
                cur = have.get(row["thread_id"])
                if (cur and cur["fingerprint"] == _fingerprint(row) and cur["version"] == VERSION
                        and cur["roster"] == ros["hash"] and cur["classified_at"] >= stale):
                    continue
                fields = classify(row, ros, freq, own, acct_group, svc_kinds)
                db.class_write_rules(conn, account, row["thread_id"], fields,
                                     _fingerprint(row), VERSION, ros["hash"], stamp)
                n += 1
        done[account] = n
    if any(done.values()):
        log("classify: " + ", ".join(f"{a}={n}" for a, n in done.items() if n))
    return done


# ---------------------------------------------------------------- Jev gate
def jev_allowed(conn, account: str, env: dict) -> bool:
    """§6 (2026-09-26): Jev may see an account's mail only when that
    account's switch is on AND a Jev key is set. Off by default."""
    return bool((env.get("JEV_API_KEY") or "").strip()) and db.class_account_jev(conn, account)
