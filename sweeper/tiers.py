"""VIPs and Top Prospects — Dev #66 (2026-10-06).

Casey: "The Jev ratings for users should include a new user type - VIP. These
are folk who we need to respond to and stay in touch with frequently. Also,
Top Prospects." Interview answers: Casey tags people by hand, Grand Central
suggests (in Contacts and a weekly Home card) and never tags on its own; a
tag holds in every account, like company groups; a VIP's mail goes to the
top of the inbox; Top Prospects get their own section too, plus a nudge when
he hasn't been in touch for 5 days.

The tag lives on the shared contacts row (contacts.tier); a suggestion Casey
waves off is remembered (contacts.tier_no) so it isn't offered again.

    .venv/bin/python sweeper/tiers.py           # tagged people + current suggestions
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone

import db

TIERS = ("vip", "prospect")
LABEL = {"vip": "⭐ VIP", "prospect": "🎯 Top prospect"}
QUIET_DAYS = 5                 # Top Prospects: nudge after this long without contact
SUGGEST_MIN_THREADS = 6        # someone you correspond with this often may be a VIP
_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "me.com", "aol.com"}
_AUTO = re.compile(r"^(no-?reply|noreply|notifications?|mailer-daemon|support|info|hello|team|news|updates?|billing|sales)@", re.I)


def tiers(conn) -> dict:
    """{email: 'vip' | 'prospect'} — every tagged person."""
    return {r["email"]: r["tier"] for r in conn.execute(
        "SELECT email, tier FROM contacts WHERE tier IN ('vip', 'prospect')")}


def set_tier(conn, email: str, tier: str | None) -> dict:
    """Casey's tag (None clears it). Never touches name, notes or mail."""
    email = (email or "").strip().lower()
    if not _EMAIL.fullmatch(email):
        raise ValueError("a full email address is required")
    if tier not in TIERS + (None, ""):
        raise ValueError("tier must be vip, prospect or empty")
    db.contact_set(conn, email)                    # make sure the row exists
    with conn:
        conn.execute("UPDATE contacts SET tier = ?, updated_at = ? WHERE email = ?",
                     (tier or None, db._now_iso(), email))
    return {"email": email, "tier": tier or None}


def decline(conn, email: str, tier: str) -> None:
    """'Not now' on a suggestion — don't offer that person for that tier again."""
    if tier not in TIERS:
        raise ValueError("tier must be vip or prospect")
    email = db.contact_set(conn, email)
    row = conn.execute("SELECT tier_no FROM contacts WHERE email = ?", (email,)).fetchone()
    no = set(filter(None, (row["tier_no"] or "").split(","))) | {tier}
    with conn:
        conn.execute("UPDATE contacts SET tier_no = ? WHERE email = ?", (",".join(sorted(no)), email))


def _people(raw_list) -> list:
    out = []
    for p in raw_list:
        m = _EMAIL.search(p or "")
        if m:
            nm = p.split("<")[0].strip().strip('"').strip() if "<" in (p or "") else ""
            out.append((m.group(0).lower(), nm if "@" not in nm else ""))
    return out


def suggest(conn, own: set, limit: int = 6) -> dict:
    """Candidates Casey confirms with one click. VIP: people he corresponds with
    most (threads + his sends), outside his companies. Top prospect: people
    whose mail is classified Prospect. Never already tagged, waved off, hidden,
    a teammate or company, or an automated address."""
    tagged = tiers(conn)
    over = {r["email"]: r for r in conn.execute("SELECT email, name, hidden, tier_no FROM contacts")}
    team = {r["email"] for r in conn.execute("SELECT email FROM class_people")}
    team_doms = {r["domain"] for r in conn.execute("SELECT domain FROM class_groups")}
    # a teammate's own company domain counts as inside too (Chris is on the
    # roster at northwindmedia.com, so his colleagues there aren't "VIPs")
    team_doms |= {e.rpartition("@")[2] for e in team if e.rpartition("@")[2] not in _FREEMAIL}
    own = {a.lower() for a in own}
    own_doms = {a.rpartition("@")[2] for a in own}

    def ok(em, tier):
        o = over.get(em)
        dom = em.rpartition("@")[2]
        return (em not in tagged and em not in own and em not in team and dom not in team_doms
                and dom not in own_doms and not _AUTO.search(em)
                and not (o and (o["hidden"] or tier in (o["tier_no"] or "").split(","))))

    count: dict[str, dict] = {}
    for r in conn.execute("SELECT participants, last_from FROM mail_threads WHERE COALESCE(category,'people') = 'people'"):
        try:
            parts = json.loads(r["participants"] or "[]")
        except ValueError:
            parts = []
        for em, nm in dict(_people(parts + [r["last_from"] or ""])).items():
            c = count.setdefault(em, {"email": em, "name": nm, "threads": 0, "sends": 0})
            c["threads"] += 1
            c["name"] = c["name"] or nm
    for r in conn.execute("SELECT to_addrs, cc_addrs FROM mail_sends"):
        for em, nm in dict(_people(re.split(r",(?![^<]*>)", f"{r['to_addrs'] or ''},{r['cc_addrs'] or ''}"))).items():
            c = count.setdefault(em, {"email": em, "name": nm, "threads": 0, "sends": 0})
            c["sends"] += 1
    for em, c in count.items():
        o = over.get(em)
        if o and o["name"]:
            c["name"] = o["name"]

    vip = sorted((c for c in count.values()
                  if ok(c["email"], "vip") and c["threads"] + 2 * c["sends"] >= SUGGEST_MIN_THREADS and c["sends"] >= 2),
                 key=lambda c: -(c["threads"] + 2 * c["sends"]))[:limit]
    for c in vip:
        c["why"] = f"{c['threads']} threads, you wrote {c['sends']}×"

    pros: dict[str, dict] = {}
    for r in conn.execute("SELECT m.last_from_email AS em, m.last_from, mc.rules, mc.jev FROM mail_class mc "
                          "JOIN mail_threads m ON m.account = mc.account AND m.thread_id = mc.thread_id "
                          "WHERE m.last_direction = 'inbound'"):
        typ = None
        for blob in (r["jev"], r["rules"]):
            try:
                typ = typ or (json.loads(blob or "{}") or {}).get("type")
            except ValueError:
                pass
        em = (r["em"] or "").lower()
        if typ == "Prospect" and em and ok(em, "prospect"):
            p = pros.setdefault(em, {"email": em, "name": (count.get(em) or {}).get("name") or "", "threads": 0})
            p["threads"] += 1
    prospect = sorted(pros.values(), key=lambda p: -p["threads"])[:limit]
    for p in prospect:
        p["why"] = f"{p['threads']} thread{'s' if p['threads'] != 1 else ''} read as a prospect"
    return {"vip": vip, "prospect": prospect}


def quiet_prospects(conn, account: str, now: datetime | None = None) -> list:
    """Top Prospects this account corresponds with and hasn't been in touch
    with for QUIET_DAYS: their latest thread here, and how long it's been."""
    now = now or datetime.now(timezone.utc)
    out = []
    for em, tier in tiers(conn).items():
        if tier != "prospect":
            continue
        like = f'%{em}%'
        t = conn.execute("SELECT * FROM mail_threads WHERE account = ? AND (lower(last_from_email) = ? "
                         "OR lower(participants) LIKE ?) ORDER BY last_date DESC LIMIT 1",
                         (account, em, like)).fetchone()
        if not t or not t["last_date"]:
            continue
        try:
            last = datetime.fromisoformat(t["last_date"].replace("Z", "+00:00"))
        except ValueError:
            continue
        days = (now - last).days
        if days >= QUIET_DAYS:
            out.append({"email": em, "days": days, "thread": dict(t)})
    return sorted(out, key=lambda x: -x["days"])


def main() -> int:
    from common import load_env
    import mailclass
    conn = db.connect()
    try:
        for em, t in sorted(tiers(conn).items()):
            print(f"{LABEL[t]:<16} {em}")
        s = suggest(conn, mailclass.own_addresses(load_env()))
        for k in TIERS:
            print(f"\nsuggested {k}:")
            for c in s[k]:
                print(f"  {c['email']:<36} {c['name'][:24]:<24} {c['why']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
