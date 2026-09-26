"""E210 — email classification, phase 2: Jev (TypeSafe's System One model).

Docs: https://docs.typesafe.ai — POST https://api.typesafe.ai/v1/systemone with
a `state` and a map of typed questions (choice / score / noul); answers come
back typed, with probabilities and a confidence. Priced per input token;
Jev is not trained on customer requests (docs.typesafe.ai/models#data-handling).

What leaves the Mini (§6, Bart 2026-09-26 — "I'm ok with the limited
sharing ... toggle it on for each account and try it"):
  * only accounts whose Jev switch is on, and only with a key set
    (mailclass.jev_allowed is the single gate, checked per account per run)
  * only person-to-person mail — newsletters, receipts, notifications,
    calendar and bulk mail are decided by rules and never sent
  * per thread: subject, the sender and other people on it, who wrote last,
    the rule-derived relationship, and the latest snippet capped at 400 chars
    — never full bodies, never attachments, never another account's mail

Jev only answers questions. It has no tools, and nothing it says archives,
sends, labels or files anything; its answers are stored beside the rules and
the Haiku assessor, and win only when their confidence clears CONF_FLOOR. Dates
stay in code (Jev's own docs: it reads dates as text, not quantities).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import db
import mailclass

API_URL = "https://api.typesafe.ai/v1/systemone"
MODELS_URL = "https://api.typesafe.ai/v1/models"
MODEL = "jev-latest"          # the response names the versioned id; we store it
SNIPPET_MAX = 400
PER_RUN = 40                  # threads per mail_sync pass, across accounts
WINDOW_DAYS = 14
TIMEOUT = 15
CONF_FLOOR = db.JEV_CONF_FLOOR   # below this, keep the rules / Haiku answer

# E217 — Bart: "a delineation between an outbound sales prospect, versus
# someone spamming me", and Spam. Jev reads criteria literally (its docs), so
# each option says exactly which side of the line it covers.
QVERSION = 2      # bump when the questions change: every answered thread is re-asked
_TYPE_CRITERIA = {
    "Correspondence": "A real person writing to the owner one-to-one or in a small group, outside the owner's team, about anything that is not selling.",
    "Team collaboration": "Colleagues coordinating work: the sender is a teammate or at the owner's company (see `sender_relationship`), or the thread is about shared internal work.",
    "Prospect": "The OWNER is selling or partnering: a lead, customer, advertiser or partner the owner is pursuing or pitched — the owner started or keeps the thread going, or the other side is answering the owner's pitch.",
    "Cold pitch": "Someone is selling TO the owner without being asked: an unsolicited vendor, agency, SEO, lead-generation, recruiting or partnership pitch, or a follow-up bump to one, from someone the owner has not worked with.",
    "Spam": "Unwanted or deceptive mail: scams, phishing, fake invoices or security alerts, crypto or prize offers, or mass mail with no real connection to the owner.",
    "Update": "An automated or FYI status message: notifications, alerts, reports, confirmations, no conversation.",
    "Newsletter": "A publication or mailing-list message sent to many people that the owner chose to receive.",
}
_RESPOND_CRITERIA = {
    "yes": "The latest message asks the owner a question, a request, or a decision, and a reply is expected.",
    "optional": "A reply would be polite or useful, but nothing is asked of the owner.",
    "no": "Nothing to answer: FYI, thanks, confirmation, automated, or the conversation is finished.",
}
_PRIORITY_LEVELS = [
    "Ignorable noise",
    "Routine: handle whenever",
    "A real person waiting on the owner about active work",
    "Urgent or high-stakes: deadlines, money, commitments, key relationships",
]
_REL_TEXT = {"teammate": "a teammate of the owner", "company": "someone at the owner's company group",
             "frequent": "someone the owner writes to often", "known": "someone the owner has written to before",
             "new": "someone new to the owner"}


def build_request(row: dict, rel: str) -> dict:
    """One thread → the request body. Nothing beyond what §6 allows."""
    try:
        people = json.loads(row.get("participants") or "[]")
    except ValueError:
        people = []
    inbound = (row.get("last_direction") or "inbound") == "inbound"
    state = {
        "subject": (row.get("subject") or "")[:300],
        "sender": row.get("last_from") or row.get("last_from_email") or "",
        "people_on_thread": [str(p)[:120] for p in people[:8]],
        "who_wrote_last": "the other person" if inbound else "the owner",
        "messages_in_thread": int(row.get("message_count") or 1),
        "sender_relationship": _REL_TEXT.get(rel, "unknown"),
        "latest_message": (row.get("snippet") or "")[:SNIPPET_MAX],
    }
    q = {
        "type": {"type": "choice", "instructions": "What kind of email is this?",
                 "criteria": _TYPE_CRITERIA},
        "priority": {"type": "score", "instructions": "How much does this email matter to the owner right now?",
                     "criteria": _PRIORITY_LEVELS},
    }
    if inbound:
        q["respond"] = {"type": "choice",
                        "instructions": "Does the owner need to reply to `latest_message`?",
                        "criteria": _RESPOND_CRITERIA}
    else:
        q["expects_reply"] = {"type": "noul",
                              "instructions": "Did the owner's `latest_message` ask something or hand over work that needs an answer from the other person?"}
    return {"state": state, "model": MODEL, "questions": q}


def parse(resp: dict) -> dict:
    """Answers → the fields we store, with each answer's confidence."""
    a = resp.get("answers") or {}
    out = {"model_id": resp.get("model"), "confidence": {}, "qv": QVERSION}
    for f in ("type", "respond"):
        if f in a and a[f].get("choice"):
            out[f] = a[f]["choice"]
            out["confidence"][f] = a[f].get("confidence")
    if "priority" in a and a["priority"].get("score") is not None:
        out["priority"] = max(0, min(3, int(round(a["priority"]["score"]))))
        out["confidence"]["priority"] = a["priority"].get("confidence")
    if "expects_reply" in a and a["expects_reply"].get("noul") is not None:
        out["expects_reply"] = a["expects_reply"]["noul"]
    u = resp.get("usage") or {}
    out["tokens"] = u.get("input_tokens")
    return out


class JevError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status


def _post(key: str, body: dict) -> dict:
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise JevError(e.code, f"HTTP {e.code}") from None
    except (urllib.error.URLError, TimeoutError) as e:
        raise JevError(None, f"unreachable: {e}") from None


def run(conn, env: dict, accounts: list, log=print, post=None,
        now: datetime | None = None) -> dict:
    """Ask Jev about changed person-mail threads in switched-on accounts."""
    post = post or _post
    key = (env.get("JEV_API_KEY") or "").strip()
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    done, budget, tokens = {}, PER_RUN, 0
    for account in accounts:
        if budget <= 0 or not mailclass.jev_allowed(conn, account, env):
            continue
        rows = conn.execute(
            "SELECT mt.*, mc.rules AS rules, mc.fingerprint AS fp FROM mail_class mc "
            "JOIN mail_threads mt ON mt.account = mc.account AND mt.thread_id = mc.thread_id "
            "WHERE mc.account = ? AND mt.last_date >= ? AND mt.bulk = 0 "
            "AND mt.category IN ('people', 'other') "
            "AND (mc.jev_fp IS NULL OR mc.jev_fp != mc.fingerprint "
            f"     OR COALESCE(json_extract(mc.jev, '$.qv'), 1) != {QVERSION}) "
            "ORDER BY mt.last_date DESC LIMIT ?", (account, since, budget)).fetchall()
        n = 0
        for r in rows:
            row = dict(r)
            rel = json.loads(row["rules"] or "{}").get("relationship", "new")
            try:
                ans = parse(post(key, build_request(row, rel)))
            except JevError as e:
                if e.status == 422:          # this thread's request is malformed; don't loop on it
                    db.class_write_jev(conn, account, row["thread_id"], {"error": str(e), "qv": QVERSION}, row["fp"])
                    continue
                log(f"jev: {account} stopped — {e}" + (" (key rejected; check Connections)"
                                                       if e.status in (401, 403) else ""))
                done[account] = n
                return done
            db.class_write_jev(conn, account, row["thread_id"], ans, row["fp"])
            tokens += ans.get("tokens") or 0
            n += 1
        done[account] = n
        budget -= n
    if any(done.values()):
        log("jev: " + ", ".join(f"{a}={n}" for a, n in done.items() if n) + f" | input tokens={tokens}")
    return done
