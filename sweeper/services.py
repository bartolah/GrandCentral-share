"""Bart's own services get their own bundles (2026-09-30).

Bart: "Let's make Jev smarter about email notifications from our own services
and properties and websites … ConnectBlue Notifications should be a bundle,
ConnectBlue Sales inquiries (RFPs) from sales@connectblue, etc."

Agreed in chat: automatic and editable; one bundle per service, keyed by its
subdomain (or, on a bare domain, by its address); website RFPs through the
sales@ group are "Sales inquiries", kept prominent and still read by Jev.
These are facts about who sent the mail, so they are rules, not a model's
judgement — and nothing here sends Jev anything new (BUILD_SPEC §6).

"Own domains" are each account's own domain plus the company-group domains
Bart keeps in Settings → Classification.

    .venv/bin/python sweeper/services.py        # the services table
"""

from __future__ import annotations

import re
import sys

import db

# the original sender of a Google Group relay: "'Dave' via Sales <sales@x.org>"
_RELAY_RE = re.compile(r"""^\s*["']*(.+?)["']*\s+via\s+(.+?)["'\s]*<""", re.I)
_EMAIL_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
# a tool posting through a group, told by its name
_TOOL_RE = re.compile(r"no[\s._-]?reply|mailchimp|digest|spam|notification|socialbee|instagram|linkedin|"
                      r"facebook|google|hubspot|calendly|zoom|slack|stripe|support|alerts?\b|\bbot\b|system|"
                      r"automat|newsletter", re.I)
# the account's own mail system: the same role words mail_sync uses
_AUTO_LOCAL_RE = re.compile(
    r"(^|[._-])(no[._-]?repl(y|ies)|do[._-]?not[._-]?reply|donotreply|notifications?|notify|alerts?|"
    r"updates?|mailer|automated|auto|bounces?|bot|system|support|billing|newsletters?|digest|events?|"
    r"partners?|hello|info|news|welcome|admin)([._+-]|$)", re.I)
_SITE_RE = re.compile(r"\b(site|website|web form|form)\b", re.I)


def _own_domain(host: str, own: set) -> str | None:
    for d in own:
        if host == d or host.endswith("." + d):
            return d
    return None


def detect(meta: dict, own: set, people: set, groups: dict, own_addresses: set = frozenset()) -> dict | None:
    """One message's headers → {key, name, kind} when it came from one of
    Bart's own services, else None. People are never services."""
    frm = meta.get("from") or ""
    m = _EMAIL_RE.search(frm)
    if not m:
        return None
    sender = m.group(0).lower()
    if sender in own_addresses:
        return None                                    # Bart writing, not a service
    local, _, host = sender.partition("@")
    base = _own_domain(host, own)
    if not base:
        return None
    company = groups.get(base) or base.split(".")[0].title()
    relay = _RELAY_RE.match(frm)
    if relay:
        orig, gname = relay.group(1).strip(), relay.group(2).strip()
        if local == "sales":
            # website RFPs arrive as "'<Company> Site' via Sales" / "New RFP: …"
            subj = (meta.get("subject") or "").strip().lower()
            if _SITE_RE.search(orig) or subj.startswith("new rfp"):
                return {"key": sender, "name": "Sales inquiries", "kind": "sales"}
            return None                                # a teammate or a customer
        if _TOOL_RE.search(orig):
            gname = gname.strip("\"' ")
            name = gname if gname.lower().startswith(company.lower()) else f"{company} {gname}"
            return {"key": sender, "name": name.strip(), "kind": "service"}
        return None                                    # a person writing through a group
    if sender in people:
        return None
    subdomain = host != base
    automated = subdomain or _AUTO_LOCAL_RE.search(local) or meta.get("bulk") or meta.get("list_unsub")
    if not automated:
        return None
    display = frm.split("<")[0].strip().strip('"').strip("'").strip()
    # Bart's own mailings (outreach campaigns that land in his inbox too) are
    # a service, but not one called "Your Name"
    if local == "bart" or display.lower().startswith("bart"):
        # one bundle per company, whichever address they went out from
        return {"key": f"mailings@{base}", "name": f"{company} mailings", "kind": "service"}
    name = display or (host.split(".")[0].title() if subdomain else f"{company} {local}")
    return {"key": host if subdomain else sender, "name": name[:60], "kind": "service"}


def category_for(svc: dict | None, category: str) -> str:
    """A service's mail leaves people mail — so it bundles, and Jev (which
    only ever sees person-to-person mail) never gets it. A sales inquiry is a
    lead written by a person: people mail, which Jev reads as before."""
    if not svc:
        return category
    if svc["kind"] == "sales":
        return "people"
    return "notification" if category in ("people", "other") else category


# ------------------------------------------------------------------ store
def register(conn, found: list) -> None:
    """Record services seen this run; Bart's edits are never overwritten."""
    now = db._now_iso()
    with conn:
        for s in found:
            conn.execute("INSERT INTO services (key, kind, auto_name, first_seen) VALUES (?,?,?,?) "
                         "ON CONFLICT(key) DO UPDATE SET auto_name = excluded.auto_name, kind = excluded.kind",
                         (s["key"], s["kind"], s["name"], now))


def table(conn) -> dict:
    """{key: {kind, name, auto_name, merged_into, off, label}} — label is the
    name shown, with the subdomain added where two services share a name."""
    rows = {r["key"]: dict(r) for r in conn.execute("SELECT * FROM services")}
    for r in rows.values():
        r["name"] = r["name"] or r["auto_name"] or r["key"]
        r["off"] = bool(r["off"])
    seen: dict[str, int] = {}
    for r in rows.values():
        seen[r["name"]] = seen.get(r["name"], 0) + 1
    for k, r in rows.items():
        tag = k.split("@")[0] if "@" in k else k.split(".")[0]
        r["label"] = f"{r['name']} · {tag}" if seen[r["name"]] > 1 else r["name"]
    return rows


def resolve(key: str | None, tbl: dict) -> str | None:
    """The bundle a service's mail goes to: follows merges; None when off."""
    hops = 0
    while key and key in tbl and tbl[key].get("merged_into") and hops < 5:
        key, hops = tbl[key]["merged_into"], hops + 1
    if not key or key not in tbl or tbl[key].get("off"):
        return None
    return key


def edit(conn, key: str, name=None, merged_into=None, off=None) -> dict:
    tbl = table(conn)
    if key not in tbl:
        raise ValueError("no such service")
    sets, vals = [], []
    if name is not None:
        sets.append("name = ?"); vals.append(re.sub(r"\s+", " ", str(name)).strip()[:60] or None)
    if merged_into is not None:
        if merged_into and (merged_into == key or merged_into not in tbl):
            raise ValueError("merge into another existing service")
        if merged_into and resolve(merged_into, {**tbl, key: {**tbl[key], "merged_into": None}}) == key:
            raise ValueError("that would merge a service into itself")
        sets.append("merged_into = ?"); vals.append(merged_into or None)
    if off is not None:
        sets.append("off = ?"); vals.append(1 if off else 0)
    if sets:
        with conn:
            conn.execute(f"UPDATE services SET {', '.join(sets)} WHERE key = ?", vals + [key])
    return table(conn)[key]


def listing(conn) -> dict:
    """Settings → Your services: {account: [service…]}, each with how many of
    that account's threads it holds. A service lives under every account its
    mail arrives in; merged ones list under the service they joined."""
    tbl = table(conn)
    out: dict[str, list] = {}
    for acct, key, n in conn.execute(
            "SELECT account, service, COUNT(*) FROM mail_threads WHERE service IS NOT NULL "
            "GROUP BY account, service ORDER BY account"):
        if key not in tbl:
            continue
        out.setdefault(acct, []).append({"key": key, "count": n, **{
            f: tbl[key][f] for f in ("kind", "name", "auto_name", "label", "merged_into", "off")}})
    for rows in out.values():
        rows.sort(key=lambda r: (r["kind"] != "sales", -r["count"], r["label"].lower()))
    return out


def main() -> int:
    conn = db.connect()
    try:
        counts = {r[0]: r[1] for r in conn.execute(
            "SELECT service, COUNT(*) FROM mail_threads WHERE service IS NOT NULL GROUP BY service")}
        for k, r in sorted(table(conn).items(), key=lambda kv: -counts.get(kv[0], 0)):
            flag = " (off)" if r["off"] else f" → {r['merged_into']}" if r["merged_into"] else ""
            print(f"{counts.get(k, 0):>5}  {r['kind']:<7} {r['label']:<36} {k}{flag}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
