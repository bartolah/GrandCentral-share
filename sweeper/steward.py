"""Inbox steward (E59) — LLM-audited sender bundling, Bart-approved.

The deterministic categorizer (mail_sync.categorize) can't judge brand
addresses that carry no role token — todaytix@, hello@substack — so they
sit in People and can accrue reply debt. Once per sweep this module asks
Haiku to look at the senders nobody has vouched for (never replied to, no
engagement prior, no sender_categories entry, never audited before) and
propose a bundle for the ones that are clearly services.

Guardrails:
  * The steward NEVER acts alone. It writes steward_suggestions rows;
    Bart approves or rejects each in the shell. Approval writes
    sender_categories — the exact store a manual 🤖 click uses.
  * A sender is audited ONCE ever ('skipped' rows persist for verdicts of
    person/unsure), so tokens stay bounded and people aren't re-litigated.
  * Bart's Context corpus (kind 'rule'/'note', active) rides in the prompt,
    so plain-English instructions steer the verdicts.
  * Fails soft: any error logs and skips — the sweep never breaks on it.

Run standalone:
    cd sweeper && ../.venv/bin/python steward.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys

try:
    from . import db
    from .common import load_env, require_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db
    from common import load_env, require_env

MODEL = "claude-haiku-4-5-20251001"
MAX_SENDERS_PER_RUN = 25
CATEGORIES = ("newsletter", "notification", "promo", "receipt")

_PROMPT = """You are the inbox steward for Your Name' Grand Central board.
Below are email SENDERS currently treated as people. For each, decide if it
is actually a SERVICE (automated/bulk/brand mail) or a real PERSON.

Categories for services:
  newsletter    — editorial digests, substacks, news blasts
  notification  — app/system updates, alerts, confirmations, case bots
  promo         — marketing, offers, sales outreach blasts from brands
  receipt       — billing, orders, payments

Bart's own rules (follow these over your own judgment):
{rules}

Judge conservatively: if the sender might be a real human who would expect
a reply from Bart, answer "person". If genuinely unsure, answer "unsure".
Never mark someone person→service merely because they write rarely.

Senders:
{senders}

Answer with ONLY a JSON array, one object per sender, no prose:
[{{"email": "...", "verdict": "person|unsure|newsletter|notification|promo|receipt", "why": "one short line"}}]"""


def _corpus_rules(conn) -> str:
    rows = conn.execute(
        "SELECT text FROM context_notes WHERE active = 1 "
        "AND kind IN ('rule','note') ORDER BY id DESC LIMIT 20").fetchall()
    lines = [r["text"] for r in rows if not str(r["text"]).startswith("[signature:")]
    return "\n".join(f"- {t}" for t in lines) or "- (none yet)"


def audit_senders(env: dict | None = None, log=print, dry_run: bool = False,
                  limit: int = MAX_SENDERS_PER_RUN) -> dict:
    env = env or load_env()
    conn = db.connect()
    try:
        own = set()
        for k, v in env.items():
            if k.startswith("GMAIL_") and k.endswith("_USER") and "@" in v:
                own.add(v.strip().lower())
        cands = db.steward_candidates(conn, own, limit=limit)
        if not cands:
            log("steward: no unaudited senders — nothing to do")
            return {"audited": 0, "suggested": 0}

        blocks = []
        for c in cands:
            blocks.append(f'- {c["email"]} (display "{c["who"]}", {c["n"]} thread(s))\n'
                          f'  subjects: {c["subjects"][:300]}\n'
                          f'  snippet: {c["snippet"][:160]}')
        prompt = _PROMPT.format(rules=_corpus_rules(conn),
                                senders="\n".join(blocks))

        import anthropic
        client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        m = re.search(r"\[.*\]", text, re.S)
        verdicts = json.loads(m.group(0)) if m else []
        by_email = {str(v.get("email", "")).lower(): v for v in verdicts
                    if isinstance(v, dict)}

        suggested = 0
        for c in cands:
            v = by_email.get(c["email"])
            verdict = str((v or {}).get("verdict", "unsure")).lower()
            why = str((v or {}).get("why", ""))[:200]
            evidence = json.dumps({"n": c["n"], "subjects": c["subjects"][:300],
                                   "snippet": c["snippet"][:160]})
            if verdict in CATEGORIES:
                status = "pending"
                suggested += 1
            else:
                status = "skipped"   # person/unsure/unparsed — never re-asked
                verdict = "people"
            if dry_run:
                log(f"steward[dry]: {c['email']} -> {verdict} ({status}) — {why}")
            else:
                db.steward_record(conn, c["email"], c["who"], verdict, why,
                                  evidence, status=status)
        log(f"steward: audited {len(cands)} sender(s), {suggested} suggestion(s)"
            f"{' [dry-run]' if dry_run else ''}")
        return {"audited": len(cands), "suggested": suggested}
    finally:
        conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print verdicts without writing suggestions")
    ap.add_argument("--limit", type=int, default=MAX_SENDERS_PER_RUN)
    args = ap.parse_args()
    audit_senders(dry_run=args.dry_run, limit=args.limit)
