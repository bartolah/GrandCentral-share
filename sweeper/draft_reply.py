"""Draft a reply to a Gmail thread into that account's Drafts folder.

This is the ONLY mailbox write in the system that existed before 2026-07-04,
and it is IMAP APPEND to the Drafts folder with the \\Draft flag — there is
no code path that sends (spec §6; Bart explicitly approved pulling this
Phase 3 element forward on 2026-07-04). `mail_actions.py` adds a few more
narrowly-scoped writes (archive/star/label) alongside this one, approved the
same way.

Split in two so the dashboard can offer both an AI-drafted reply (editable
before saving) and a fully hand-written one through the same save path:

  - generate_draft(): reads the thread, asks Claude for reply text. No write.
  - save_draft(): given a body (AI-written, edited, or typed from scratch),
    composes the RFC-822 reply and does the one mailbox write (APPEND).
  - compose_draft() (E21): same APPEND for a NEW email with explicit
    to/subject/body — no thread, no reply headers, still never sends.

Run standalone:
    cd sweeper
    ../.venv/bin/python -m draft_reply --account personal --thread-id 18690...
"""

from __future__ import annotations

import argparse
import imaplib
import re
import time
from email.message import EmailMessage

try:
    from . import mail_common as mc
    from .common import load_env, require_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mail_common as mc
    from common import load_env, require_env

# Drafting quality matters more than classification throughput; use Sonnet.
DRAFT_MODEL = "claude-sonnet-5"
CONTEXT_MSGS = 4        # newest N messages given to the model

SYSTEM_PROMPT = (
    "You draft email replies on behalf of Bart Olah, writing as Bart in the "
    "first person. Concise, direct, warm; no corporate filler, no apologies "
    "for delay unless the thread clearly calls for one. Match the thread's "
    "language and formality. Only commit to facts, dates, or promises that "
    "the thread itself supports — where Bart must decide something, either "
    "propose the decision plainly or ask the single question that unblocks "
    "it. Sign off simply as 'Bart'. Return ONLY the reply body text: no "
    "subject line, no quoted thread, no preamble or commentary."
)


def _draft_body(messages: list, subject: str, env: dict, model: str) -> str:
    """Ask Claude for the reply body, given the thread transcript."""
    import anthropic

    transcript = []
    for m in messages:  # oldest -> newest
        stamp = m["dt"].strftime("%Y-%m-%d %H:%M") if m["dt"] else "?"
        transcript.append(f"[{stamp}] From: {m['from']}\n{m['body'] or '(no text)'}")
    prompt = (
        f"Subject: {subject}\n\n"
        f"Thread, oldest to newest:\n\n" + "\n\n---\n\n".join(transcript) +
        "\n\nDraft Bart's reply to the latest message."
    )
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model, max_tokens=1000, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    body = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not body:
        raise RuntimeError("model returned an empty draft")
    return body


OPTIONS_SYSTEM = (
    "You draft email reply OPTIONS on behalf of Bart Olah, writing as Bart "
    "in the first person. Concise, direct, warm; no corporate filler. Match "
    "the thread's language and formality. Produce 2-3 GENUINELY DIFFERENT "
    "responses — different moves, not rephrasings: e.g. commit/agree with a "
    "concrete next step; redirect or delegate; decline or defer gracefully; "
    "or ask the single question that unblocks Bart. Only commit to facts "
    "the thread supports. Sign off simply as 'Bart'. Return ONLY JSON: "
    '{"options":[{"label":"<1-3 words>","body":"<the full reply text>"}]}'
)


def generate_options(thread_id: str, account: str, env: dict | None = None,
                     model: str = DRAFT_MODEL) -> dict:
    """E157: 2-3 distinct reply options for one thread — text only, nothing
    written anywhere. On-demand from the ✍️ button; Bart is testing the
    feel before any automation is considered."""
    import json as _json
    import anthropic
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        headers = _reply_headers(imap, user, thread_id)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    transcript = []
    for m in headers["messages"]:
        stamp = m["dt"].strftime("%Y-%m-%d %H:%M") if m["dt"] else "?"
        transcript.append(f"[{stamp}] From: {m['from']}\n{m['body'] or '(no text)'}")
    prompt = (f"Subject: {headers['subject']}\n\nThread, oldest to newest:\n\n"
              + "\n\n---\n\n".join(transcript)
              + "\n\nDraft Bart's reply OPTIONS to the latest message.")
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model, max_tokens=2000, system=OPTIONS_SYSTEM,
        messages=[{"role": "user", "content": prompt}])
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("model returned no options")
    opts = (_json.loads(m.group(0)).get("options") or [])[:3]
    opts = [{"label": str(o.get("label") or "option")[:40],
             "body": str(o.get("body") or "").strip()[:6000]}
            for o in opts if str(o.get("body") or "").strip()]
    if not opts:
        raise RuntimeError("model returned no usable options")
    return {"subject": headers["subject"], "options": opts}


def _reply_headers(imap: imaplib.IMAP4_SSL, user: str, thread_id: str) -> dict:
    """Moved to mail_common.reply_headers (E3-P4) so draft_reply and
    send_mail derive recipients/threading identically; thin alias kept."""
    return mc.reply_headers(imap, user, thread_id, context_msgs=CONTEXT_MSGS)


def generate_draft(thread_id: str, account: str, env: dict | None = None,
                   model: str = DRAFT_MODEL) -> dict:
    """Read the thread and have Claude write a reply body. No mailbox write.

    Returns {"to", "cc", "subject", "body"}. Raises on any failure.
    """
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        headers = _reply_headers(imap, user, thread_id)
        body = _draft_body(headers["messages"], headers["subject"], env, model)
        return {"to": headers["to"], "cc": headers["cc"],
                "subject": headers["subject"], "body": body}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def save_draft(thread_id: str, account: str, body: str, to: str = None,
              cc: str = None, subject: str = None, bcc: str = None,
              env: dict | None = None, log=print) -> dict:
    """Compose an RFC-822 reply from `body` and APPEND it to Drafts.

    `body` may be AI-generated, AI-generated-then-edited, or typed from
    scratch — this is the one mailbox write regardless of its origin. If
    `to`/`cc`/`subject` aren't given, they're re-derived from the thread the
    same way `generate_draft` does, so a purely manual reply only needs a
    `thread_id` and a `body`.

    Returns {"to", "cc", "subject", "preview"}. Raises on any failure —
    nothing is retried against the mailbox.
    """
    if not (body or "").strip():
        raise ValueError("empty draft body")
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        in_reply_to = references = ""
        if to is None or cc is None or subject is None:
            headers = _reply_headers(imap, user, thread_id)
            to = headers["to"] if to is None else to
            cc = headers["cc"] if cc is None else cc
            subject = headers["subject"] if subject is None else subject
            in_reply_to = headers["in_reply_to"]
            references = headers["references"]

        reply = EmailMessage()
        reply["From"] = user
        reply["To"] = to
        if cc:
            reply["Cc"] = cc
        if bcc:
            reply["Bcc"] = bcc
        reply["Subject"] = subject
        if in_reply_to:
            reply["In-Reply-To"] = in_reply_to
            reply["References"] = references
        reply.set_content(body)

        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, _ = imap.append(f'"{drafts}"', r"(\Draft)",
                             imaplib.Time2Internaldate(time.time()),
                             reply.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed")

        log(f"draft_reply: saved to {reply['To']} in {account}:{drafts} "
            f"({len(body)} chars)")
        return {"to": reply["To"], "cc": reply.get("Cc", ""),
                "subject": subject, "preview": body[:200]}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def compose_draft(account: str, to: str, subject: str, body: str,
                  cc: str = None, bcc: str = None,
                  env: dict | None = None, log=print) -> dict:
    """Compose a NEW email (no thread, no reply headers) and APPEND it to the
    account's Drafts folder (E21). Same one-mailbox-write guarantee as
    save_draft — nothing here can send; Bart reviews and sends from Gmail
    or the dashboard.

    Returns {"to", "cc", "subject", "preview"}. Raises on any failure.
    """
    to = (to or "").strip()
    subject = " ".join((subject or "").split())
    if not to:
        raise ValueError("'to' is required")
    if not subject:
        raise ValueError("subject is required")
    if not (body or "").strip():
        raise ValueError("empty draft body")

    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Subject"] = subject
        msg.set_content(body)

        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, _ = imap.append(f'"{drafts}"', r"(\Draft)",
                             imaplib.Time2Internaldate(time.time()),
                             msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed")

        log(f"draft_reply: composed new draft to {to} in {account}:{drafts} "
            f"({len(body)} chars)")
        return {"to": to, "cc": cc or "", "subject": subject,
                "preview": body[:200]}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def replace_draft(account: str, draft_uid: str, gm_msgid: str, body: str,
                  to: str, subject: str, cc: str = None, bcc: str = None,
                  in_reply_to: str = "", references: str = "",
                  env: dict | None = None, log=print) -> dict:
    """Save an edited Gmail draft (E224, Dev #19). IMAP has no edit-in-place,
    so the new version is APPENDed first and only then is the old one moved
    to Trash (Bart 2026-09-27: "it's ok to delete and resave"). A reply keeps
    its In-Reply-To/References so Gmail keeps it on its thread. If the old
    draft can't be removed, the new one is already safe and the caller hears
    about the leftover copy."""
    import mail_actions
    if not (body or "").strip():
        raise ValueError("empty draft body")
    if not (to or "").strip():
        raise ValueError("'to' is required")
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        msg = EmailMessage()
        msg["From"] = user
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Subject"] = " ".join((subject or "").split())
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = references or in_reply_to
        msg.set_content(body)
        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, _ = imap.append(f'"{drafts}"', r"(\Draft)",
                             imaplib.Time2Internaldate(time.time()),
                             msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    out = {"to": to, "cc": cc or "", "subject": msg["Subject"], "preview": body[:200]}
    try:
        mail_actions.trash_draft(account, draft_uid, gm_msgid, env=env, log=log)
        out["replaced"] = True
    except Exception as e:
        out["replaced"] = False
        out["warning"] = f"saved, but the old copy is still in Drafts ({e})"
    log(f"draft_reply: replaced draft {draft_uid} in {account} ({len(body)} chars)")
    return out


def draft_reply(thread_id: str, account: str, env: dict | None = None,
               log=print, model: str = DRAFT_MODEL) -> dict:
    """AI-draft a reply and save it straight to Drafts (the one-click path).

    Thin wrapper: generate_draft() -> save_draft(). Kept for the existing
    single "Draft" button; the dashboard's edit-before-saving flow calls the
    two steps separately instead.
    """
    env = env or load_env()
    draft = generate_draft(thread_id, account, env=env, model=model)
    return save_draft(thread_id, account, draft["body"], to=draft["to"],
                      cc=draft["cc"], subject=draft["subject"], env=env, log=log)


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Draft a reply into Gmail Drafts (never sends)")
    ap.add_argument("--account", required=True, choices=list(mc.ACCOUNTS))
    ap.add_argument("--thread-id", required=True)
    ap.add_argument("--model", default=DRAFT_MODEL)
    args = ap.parse_args(argv)
    out = draft_reply(args.thread_id, args.account, model=args.model)
    print(f'\nDrafted: {out["subject"]}\nTo: {out["to"]}\nCc: {out["cc"]}\n\n{out["preview"]}…')
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
