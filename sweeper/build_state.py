"""Assemble grand_central.json — account-first schema.

The dashboard reads this one file (the sweeper and the dashboard still never
talk directly — INVARIANT). Everything here is per mail account: every thread
row carries its `account`, and every per-thread lookup (assessments,
dismissals, categories) is keyed by (account, thread_id) so the same Gmail
thread id in two accounts can never bleed across.

Projects, goals/objectives, operations, the unassigned tray and the CRM
pipeline are gone from the board. Rules that carry over: every list sorted by
urgency; a failed source is absent from sources_swept, never silently stale.
"""

from __future__ import annotations

import re
from datetime import datetime

_ADDR_RE = re.compile(r"<([^>]+)>")


def _addr_of(p: str) -> str:
    """'Name <a@b.c>' or 'a@b.c' -> lowercased address."""
    m = _ADDR_RE.search(p or "")
    return ((m.group(1) if m else p) or "").lower().strip()


def _display_name(addr: str) -> str:
    """'Alex R. <chris@x.com>' -> 'Alex R.'; bare address -> local part."""
    addr = (addr or "").strip()
    m = re.match(r"^(.*?)\s*<([^>]+)>$", addr)
    if m and m.group(1).strip():
        return m.group(1).strip().strip('"')
    if m:
        return m.group(2).split("@")[0]
    return addr.split("@")[0] if "@" in addr else addr


def _gmail_link(r: dict) -> str:
    """Deep link that opens this thread in the right Gmail account."""
    thrid = r.get("thread_id", "")
    email_addr = r.get("account_email", "")
    if not thrid or r.get("source", "gmail") != "gmail":
        return ""
    try:
        hex_id = format(int(thrid), "x")
    except ValueError:
        return ""
    auth = f"?authuser={email_addr}" if email_addr else ""
    return f"https://mail.google.com/mail/{auth}#all/{hex_id}"


def _age_days(raised_iso: str, now_iso: str) -> int:
    try:
        a = datetime.strptime(raised_iso, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ")
        return max(0, (b - a).days)
    except (ValueError, TypeError):
        return 0


def _engagement(email: str, priors: dict) -> tuple:
    """E47: a −8..+8 preference boost for a sender, learned from Bart's own
    actions — replies lift, dismisses sink. Exact-address signal wins; the
    domain is a weaker fallback when the address hasn't been seen. Returns
    (boost, why) or (0, None)."""
    if not priors or not email:
        return 0, None
    e = email.lower()
    dom = e.rsplit("@", 1)[-1] if "@" in e else ""
    ex_rep = priors.get("reply_email", {}).get(e, 0)
    ex_dis = priors.get("dismiss_email", {}).get(e, 0)
    if ex_rep or ex_dis:
        rep, dis = ex_rep, ex_dis            # exact history wins outright
    else:                                    # unseen address: lean on the domain
        rep = 0.4 * priors.get("reply_dom", {}).get(dom, 0)
        dis = 0.4 * priors.get("dismiss_dom", {}).get(dom, 0)
    raw = max(-8, min(8, 5 * rep - 3 * dis))
    if raw >= 1:
        return round(raw), "you engage them"
    if raw <= -1:
        return round(raw), "you usually skip them"
    return 0, None


def _build_top5(reply_debt: list, reg: dict, limit: int = 5,
                priors: dict | None = None) -> list:
    """Top 5 = the highest-SCORING reply-debt threads, not the oldest (E6).

    score = assessed importance (0-3, x10 — the dominant signal)
          + sender in registry key_people (+5)
          + learned sender engagement (E47)
          + staleness tiebreaker capped at 7 (age can rank, never dominate)
    Excluded outright: structural auto-replies and reply_needed=false.
    Diversity: at most 2 entries per account so one noisy mailbox can't own
    the list. Unassessed threads default to importance 1 / reply_needed true.
    Each entry carries thread_id/account/link so the board can open, draft
    and dismiss straight from Top 5, plus a `why` for the tooltip."""
    key_people = {str(p).strip().lower() for p in reg.get("key_people", [])}

    scored = []
    for d in reply_debt:
        if d.get("auto_reply") or d.get("reply_needed") is False:
            continue
        imp = d.get("importance")
        imp = 1 if imp is None else int(imp)
        score, why = imp * 10, [f"importance {imp}"]
        if (d.get("who") or "").strip().lower() in key_people:
            score += 5
            why.append("key person")
        eb, ebwhy = _engagement(d.get("last_from_email"), priors)
        if eb:
            score += eb
            why.append(ebwhy)
        age = int(d.get("age_days") or 0)
        score += min(age, 7)
        why.append(f"{age}d old")
        scored.append((score, d["account"], " · ".join(why), d))

    scored.sort(key=lambda s: (-s[0], -int(s[3].get("age_days") or 0)))
    out: list = []
    per_account: dict[str, int] = {}
    for score, account, why, d in scored:
        if per_account.get(account, 0) >= 2:
            continue
        per_account[account] = per_account.get(account, 0) + 1
        out.append({
            "text": f'{d["who"]}: {d["what"]}',
            "who": d["who"],
            "what": d["what"],
            "thread_id": d["thread_id"],
            "account": d["account"],
            "link": d["link"],
            "score": score,
            "why": why,
            "done": False,
        })
        if len(out) >= limit:
            break
    return out


def build_state(records: list, assessed: dict, reg: dict,
                generated_at: str, next_sweep_at: str,
                sources_swept: list, meetings: list | None = None,
                calendar_days: list | None = None, dismissed: set | None = None,
                engagements: list | None = None, decisions: list | None = None,
                mail_categories: dict | None = None,
                sender_priors: dict | None = None, log=print) -> dict:
    """Build the full grand_central.json dict.

    `assessed` is {(account, thread_id): {summary, reply_needed, importance}};
    `dismissed` is a set of (account, thread_id) — see db.dismissed_keys.
    """
    dismissed = dismissed or set()
    engagements = engagements or []
    decisions = decisions or []
    mail_categories = mail_categories or {}
    sender_priors = sender_priors or {}   # E47: learned engagement per sender
    defaults = reg.get("defaults", {})

    def _key(r):
        return (r.get("account", ""), r.get("thread_id", ""))

    own_addresses = {r.get("account_email", "").lower() for r in records if r.get("account_email")}

    # ---- calendar: attendee emails never ship in the board JSON -------------
    for _day in (calendar_days or []):
        for _ev in _day.get("events", []):
            _ev.pop("attendees", None)
            _ev.pop("organizer", None)

    # ---- reply_debt ---------------------------------------------------------
    # Bulk/marketing mail (List-Unsubscribe / List-Id / Precedence / no-reply
    # senders) categorically isn't owed a reply — suppressed here with a
    # visible count. Automated mail (receipts, newsletters, notifications,
    # calendar traffic) is suppressed via the Mail screen's deterministic
    # category; only person-to-person threads accrue reply debt. Fail open: a
    # thread mail_sync hasn't categorized yet is treated as a person.
    reply_debt = []
    bulk_suppressed = 0
    auto_suppressed = 0
    for r in records:
        if r.get("last_direction") != "inbound" or _key(r) in dismissed:
            continue
        if r.get("bulk"):
            bulk_suppressed += 1
            continue
        cat = mail_categories.get(_key(r))
        if cat and cat != "people":
            auto_suppressed += 1
            continue
        c = assessed.get(_key(r), {})
        reply_debt.append({
            "src": "gmail",
            "who": _display_name(r.get("last_from", "")),
            "what": c.get("summary", "") or r.get("subject", ""),
            "age_days": r.get("age_days", 0),
            "link": _gmail_link(r),
            "thread_id": r.get("thread_id", ""),
            "account": r.get("account", ""),
            "last_from_email": (r.get("last_from_email") or "").lower(),  # E47 prior key
            "auto_reply": bool(r.get("auto_reply")),
            "reply_needed": c.get("reply_needed"),
            "importance": c.get("importance"),
        })
    reply_debt.sort(key=lambda x: x["age_days"], reverse=True)
    if bulk_suppressed:
        log(f"reply_debt: {bulk_suppressed} bulk/marketing thread(s) suppressed "
            f"(List-Unsubscribe/List-Id/Precedence/no-reply)")
    if auto_suppressed:
        log(f"reply_debt: {auto_suppressed} automated thread(s) suppressed "
            f"(receipt/newsletter/notification/calendar — people only)")

    # ---- E46: waiting-on — asks Bart sent that went quiet -------------------
    # The mirror of reply_debt: BART wrote last, a few days passed with no
    # reply, and a real person is on the other end. A 30-day ceiling keeps
    # abandoned threads out of the nudge list.
    WAIT_MIN_DAYS, WAIT_MAX_DAYS = 3, 30
    _NONPERSON_RE = re.compile(
        r"^(no[-_.]?reply|donotreply|notifications?|alerts?|updates?|mailer|"
        r"bounces?|postmaster|bot|system|automated|auto|receipts?|billing|"
        r"invoices?|orders?|payments?|statements?|support|help|info|rewards?|"
        r"email-[0-9a-f]{6})", re.I)
    waiting_on = []
    for r in records:
        if r.get("last_direction") != "outbound":
            continue
        if r.get("bulk") or r.get("auto_reply") or _key(r) in dismissed:
            continue
        age = r.get("age_days", 0)
        if age < WAIT_MIN_DAYS or age > WAIT_MAX_DAYS:
            continue
        others = [p for p in r.get("participants", [])
                  if p and _addr_of(p) not in own_addresses]
        if not others:
            continue
        if _NONPERSON_RE.match(_addr_of(others[0]).split("@")[0]):
            continue
        c = assessed.get(_key(r), {})
        waiting_on.append({
            "who": _display_name(others[0]),
            "recipients": len(others),
            "what": c.get("summary", "") or r.get("subject", ""),
            "age_days": age,
            "link": _gmail_link(r),
            "thread_id": r.get("thread_id", ""),
            "account": r.get("account", ""),
            "nudge_state": None,
        })
    waiting_on.sort(key=lambda x: x["age_days"], reverse=True)
    waiting_on = waiting_on[:15]
    if waiting_on:
        log(f"waiting_on: {len(waiting_on)} outbound thread(s) gone quiet "
            f"({WAIT_MIN_DAYS}-{WAIT_MAX_DAYS}d, people)")

    # ---- decisions -----------------------------------------------------------
    decisions_out = [{"id": d["id"], "text": d["text"],
                      "blocking": d.get("blocking") or "",
                      "raised_at": d.get("raised_at", ""),
                      "age_days": _age_days(d.get("raised_at", ""), generated_at)}
                     for d in decisions]
    decisions_out.sort(key=lambda x: x["age_days"], reverse=True)

    # ---- top5 + summary -------------------------------------------------------
    # A deeper pool than the board shows: serve.py filters out threads Bart
    # has since resolved (done/dismissed/snoozed/replied) and backfills from
    # the pool, so Top 5 stays truthful between hourly sweeps.
    top5_pool = _build_top5(reply_debt, reg, limit=12, priors=sender_priors)
    top5 = top5_pool[:5]
    meetings = meetings or []
    summary = {
        "replies_owed": len(reply_debt),
        "waiting_on": len(waiting_on),
        "meetings_today": len(meetings),
        "decisions_pending": len(decisions_out),
        "tasks_open": len(engagements),
        "bulk_suppressed": bulk_suppressed,
        "auto_suppressed": auto_suppressed,   # E39: receipts/notices, people only
    }

    return {
        "generated_at": generated_at,
        "next_sweep_at": next_sweep_at,
        "sources_swept": sources_swept,
        "summary": summary,
        "top5": top5,
        "top5_pool": top5_pool,
        "reply_debt": reply_debt,
        "decisions": decisions_out,
        "waiting_on": waiting_on,
        "meetings_today": meetings,
        "calendar": calendar_days or [],
        "yesterday_actions": [],   # arrives with Granola
    }
