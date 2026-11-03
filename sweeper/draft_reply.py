"""Draft a reply to a Gmail thread into that account's Drafts folder.

This is the ONLY mailbox write in the system that existed before 2026-07-04,
and it is IMAP APPEND to the Drafts folder with the \\Draft flag — there is
no code path that sends (spec §6; Casey explicitly approved pulling this
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
    "You draft email replies on behalf of Casey Doe, writing as Casey in the "
    "first person. Concise, direct, warm; no corporate filler, no apologies "
    "for delay unless the thread clearly calls for one. Match the thread's "
    "language and formality. Only commit to facts, dates, or promises that "
    "the thread itself supports — where Casey must decide something, either "
    "propose the decision plainly or ask the single question that unblocks "
    "it. Sign off simply as 'Casey'. Return ONLY the reply body text: no "
    "subject line, no quoted thread, no preamble or commentary."
)


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
SYSTEM_PROMPT = SYSTEM_PROMPT + "\n\n" + untrusted.RULE


DOCS_CAP = 20000        # E259: characters of the account's context documents


def _account_brief(account: str) -> str:
    """E259: the account's context documents (Casey's voice and naming rules
    live there), for the system prompt. Best-effort: a draft never fails for
    want of its brief."""
    try:
        try:
            from . import db
        except ImportError:  # pragma: no cover - direct-run fallback
            import db
        conn = db.connect()
        try:
            docs = db.context_docs_text(conn, [f"account:{db.clean_account(account)}"], DOCS_CAP)
        finally:
            conn.close()
    except Exception:
        return ""
    if not docs:
        return ""
    return ("\n\nCasey's standing brief for this account follows. Follow its voice "
            "and naming rules; never put anything it marks internal-only (such as "
            "pricing) into the reply.\n\n" +
            "\n\n".join(f"## {d['title']}\n{d['text']}" for d in docs))


def _draft_body(messages: list, subject: str, env: dict, model: str,
                account: str | None = None) -> str:
    """Ask Claude for the reply body, given the thread transcript."""
    import anthropic

    transcript = []
    for m in messages:  # oldest -> newest
        stamp = m["dt"].strftime("%Y-%m-%d %H:%M") if m["dt"] else "?"
        transcript.append(f"[{stamp}] From: {m['from']}\n{m['body'] or '(no text)'}")
    prompt = (
        "The thread, oldest to newest:\n\n"
        + untrusted.wrap(f"Subject: {subject}\n\n" + "\n\n---\n\n".join(transcript), "thread")
        + "\n\nDraft Casey's reply to the latest message."
    )
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model, max_tokens=1000,
        system=SYSTEM_PROMPT + (_account_brief(account) if account else ""),
        messages=[{"role": "user", "content": prompt}],
    )
    body = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not body:
        raise RuntimeError("model returned an empty draft")
    return body


OPTIONS_SYSTEM = (
    "You draft email reply OPTIONS on behalf of Casey Doe, writing as Casey "
    "in the first person. Concise, direct, warm; no corporate filler. Match "
    "the thread's language and formality. Produce 2-3 GENUINELY DIFFERENT "
    "responses — different moves, not rephrasings: e.g. commit/agree with a "
    "concrete next step; redirect or delegate; decline or defer gracefully; "
    "or ask the single question that unblocks Casey. Only commit to facts "
    "the thread supports. Sign off simply as 'Casey'. Return ONLY JSON: "
    '{"options":[{"label":"<1-3 words>","body":"<the full reply text>"}]}'
)


OPTIONS_SYSTEM = OPTIONS_SYSTEM + "\n\n" + untrusted.RULE


def generate_options(thread_id: str, account: str, env: dict | None = None,
                     model: str = DRAFT_MODEL) -> dict:
    """E157: 2-3 distinct reply options for one thread — text only, nothing
    written anywhere. On-demand from the ✍️ button; Casey is testing the
    feel before any automation is considered."""
    import private
    private.guard(account, thread_id)  # E259: a Private thread does not exist here
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
    prompt = ("The thread, oldest to newest:\n\n"
              + untrusted.wrap(f"Subject: {headers['subject']}\n\n"
                               + "\n\n---\n\n".join(transcript), "thread")
              + "\n\nDraft Casey's reply OPTIONS to the latest message.")
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    resp = client.messages.create(
        model=model, max_tokens=2000, system=OPTIONS_SYSTEM + _account_brief(account),
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


ASSIST_SYSTEM = (
    "You write and rewrite emails on behalf of Casey Doe, as Casey, in the first "
    "person. Concise, direct, warm; no corporate filler. Follow Casey's instruction "
    "exactly. If he gave you his own draft, keep his meaning, facts and any names, "
    "numbers and dates he wrote unless he asks you to change them; improve it as he "
    "asks. Only commit to facts the thread, his draft or his history supports. Do "
    "not add a signature block (his is added separately); sign off simply as 'Casey' "
    "only if his draft does or there is no draft. Return ONLY the email body text: "
    "no subject line, no quoted thread, no commentary."
) + "\n\n" + untrusted.RULE
HISTORY_THREADS = 10    # recent threads with these recipients, when Casey asks
HISTORY_SENT = 3        # and up to this many of his own messages to them, for tone


def _history(account: str, recipients: list, env: dict, log=print) -> str:
    """Casey ticked "Use my email history" (2026-10-06 interview: "with these
    recipients"): his recent threads with them in THIS account, and a few of
    his own messages to them for tone. Private threads never appear
    (search_threads excludes them)."""
    import mail_actions
    rows, seen = [], set()
    for em in recipients[:4]:
        try:
            for t in mail_actions.search_threads(f"from:{em} OR to:{em}", account, env=env,
                                                 limit=HISTORY_THREADS, log=log):
                if t.get("thread_id") not in seen:
                    seen.add(t.get("thread_id"))
                    rows.append(t)
        except Exception as e:
            log(f"assist: history search for {em} failed ({e!r})")
    rows.sort(key=lambda t: str(t.get("last_date") or ""), reverse=True)
    lines = [f"[{str(t.get('last_date') or '')[:10]}] {t.get('subject') or ''} — "
             f"{t.get('last_from') or t.get('who') or ''}: {(t.get('snippet') or '')[:240]}"
             for t in rows[:HISTORY_THREADS]]
    mine = []
    for em in recipients[:2]:
        try:
            for t in mail_actions.search_threads(f"in:sent to:{em}", account, env=env, limit=HISTORY_SENT, log=log)[:HISTORY_SENT]:
                msgs = mail_actions.fetch_thread(t["thread_id"], account, env=env)
                own = [m for m in msgs if em not in str(m.get("from") or "").lower()]
                if own:
                    mine.append(f"To {em}, {str(own[-1].get('date') or '')[:10]}:\n{(own[-1].get('body') or '')[:1200]}")
        except Exception as e:
            log(f"assist: sent examples for {em} failed ({e!r})")
    out = ""
    if lines:
        out += "Recent threads with these people (newest first):\n" + "\n".join(lines)
    if mine:
        out += "\n\nHow Casey has written to them before:\n\n" + "\n\n---\n\n".join(mine[:HISTORY_SENT])
    return out


def assist(account: str, instruction: str, draft: str = "", thread_id: str | None = None,
           to: str = "", cc: str = "", subject: str = "", use_history: bool = False,
           env: dict | None = None, model: str = DRAFT_MODEL, log=print) -> dict:
    """The composer's ✨ (Casey 2026-10-06: "add a prompt or have it [polish] an
    email I'm already writing … and if I ask it to, to look at my email
    history"). Returns text only — nothing is written anywhere; Casey previews
    it and chooses Replace or Insert."""
    import anthropic
    from email.utils import getaddresses
    env = env or load_env()
    instruction = str(instruction or "").strip()[:2000]
    draft = str(draft or "").strip()[:20000]
    if not instruction and not draft and not thread_id:
        raise ValueError("tell it what to write")
    parts = []
    if thread_id:
        import private
        private.guard(account, thread_id)
        thread_id = mc.validate_thread_id(thread_id)
        imap, user = mc.connect(account, env)
        try:
            headers = _reply_headers(imap, user, thread_id)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
        transcript = []
        for m in headers["messages"][-CONTEXT_MSGS:]:
            stamp = m["dt"].strftime("%Y-%m-%d %H:%M") if m["dt"] else "?"
            transcript.append(f"[{stamp}] From: {m['from']}\n{m['body'] or '(no text)'}")
        subject = subject or headers["subject"]
        parts.append("The thread Casey is replying to, oldest to newest:\n\n" + untrusted.wrap(
            f"Subject: {subject}\n\n" + "\n\n---\n\n".join(transcript), "thread"))
    elif subject or to:
        parts.append(f"A new email. To: {to or '(not chosen yet)'}. Subject: {subject or '(none yet)'}.")
    if use_history:
        own = set()
        try:
            import mailclass
            own = {a.lower() for a in mailclass.own_addresses(env)}
        except Exception:
            pass
        rcpts = [a.lower() for _n, a in getaddresses([to, cc]) if "@" in a and a.lower() not in own]
        hist = _history(account, rcpts, env, log=log) if rcpts else ""
        parts.append(untrusted.wrap(hist, "history") if hist else "(No earlier mail with these people was found.)")
    if draft:
        parts.append("Casey's own draft so far (his words — rewrite it as he asks):\n\n" + draft)
    ask = instruction or ("Polish Casey's draft." if draft else "Draft Casey's reply to the latest message.")
    parts.append(f"Casey's instruction: {ask}")
    client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
    resp = client.messages.create(model=model, max_tokens=1500,
                                  system=ASSIST_SYSTEM + _account_brief(account),
                                  messages=[{"role": "user", "content": "\n\n".join(parts)}])
    body = "".join(b.text for b in resp.content if b.type == "text").strip()
    if not body:
        raise RuntimeError("the model returned nothing")
    return {"body": body[:20000], "used_history": bool(use_history)}


def _reply_headers(imap: imaplib.IMAP4_SSL, user: str, thread_id: str) -> dict:
    """Moved to mail_common.reply_headers (E3-P4) so draft_reply and
    send_mail derive recipients/threading identically; thin alias kept."""
    return mc.reply_headers(imap, user, thread_id, context_msgs=CONTEXT_MSGS)


def generate_draft(thread_id: str, account: str, env: dict | None = None,
                   model: str = DRAFT_MODEL) -> dict:
    """Read the thread and have Claude write a reply body. No mailbox write.

    Returns {"to", "cc", "subject", "body"}. Raises on any failure.
    """
    import private
    private.guard(account, thread_id)  # E259: a Private thread does not exist here
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        headers = _reply_headers(imap, user, thread_id)
        body = _draft_body(headers["messages"], headers["subject"], env, model, account)
        return {"to": headers["to"], "cc": headers["cc"],
                "subject": headers["subject"], "body": body}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# E258 (BUILD_SPEC §6 item 5, Casey chose "Allow + flag"): an agent may draft
# to anyone, but every agent draft says so, and one addressed to someone new
# to the account says that too. The headers are what GC's composer reads for
# its warning banner (they survive until the draft is edited in Gmail); the
# labels are what Casey sees in Gmail itself. Labelling is best-effort — a
# failed STORE never loses the draft.
AGENT_LABEL = "GC/Agent-Draft"
AGENT_NEW_LABEL = "GC/Agent-Draft-New"


def _mark_agent(msg, agent: dict | None) -> None:
    if agent is None:
        return
    msg["X-GC-Agent-Draft"] = "1"
    new = [a for a in (agent.get("new") or []) if a][:20]
    if new:
        msg["X-GC-New-Recipients"] = ", ".join(new)


def _label_agent(imap, drafts: str, appended, agent: dict | None, account: str, log) -> None:
    if agent is None:
        return
    labels = [AGENT_LABEL] + ([AGENT_NEW_LABEL] if agent.get("new") else [])
    try:
        raw = b" ".join(x for x in (appended or []) if isinstance(x, bytes)).decode("ascii", "replace")
        m = re.search(r"APPENDUID \d+ (\d+)", raw)
        if not m:
            raise RuntimeError(f"no APPENDUID in {raw[:80]!r}")
        imap.select(f'"{drafts}"')
        value = "(" + " ".join('"' + l + '"' for l in labels) + ")"
        typ, _ = imap.uid("STORE", m.group(1), "+X-GM-LABELS", value)
        if typ != "OK":
            raise RuntimeError(f"STORE {value} failed")
    except Exception as e:
        log(f"draft_reply: agent draft saved in {account} but not labelled ({e!r}) — "
            "the X-GC-Agent-Draft header still marks it")


def save_draft(thread_id: str, account: str, body: str, to: str = None,
              cc: str = None, subject: str = None, bcc: str = None,
              env: dict | None = None, log=print, agent: dict | None = None) -> dict:
    """Compose an RFC-822 reply from `body` and APPEND it to Drafts.

    `body` may be AI-generated, AI-generated-then-edited, or typed from
    scratch — this is the one mailbox write regardless of its origin. If
    `to`/`cc`/`subject` aren't given, they're re-derived from the thread the
    same way `generate_draft` does, so a purely manual reply only needs a
    `thread_id` and a `body`.

    Returns {"to", "cc", "subject", "preview"}. Raises on any failure —
    nothing is retried against the mailbox.
    """
    import private
    private.guard(account, thread_id)  # E259: a Private thread does not exist here
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
        reply["From"] = mc.from_header(account, user, env)
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
        _mark_agent(reply, agent)

        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, appended = imap.append(f'"{drafts}"', r"(\Draft)",
                                    imaplib.Time2Internaldate(time.time()),
                                    reply.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed")
        _label_agent(imap, drafts, appended, agent, account, log)

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
                  env: dict | None = None, log=print, agent: dict | None = None) -> dict:
    """Compose a NEW email (no thread, no reply headers) and APPEND it to the
    account's Drafts folder (E21). Same one-mailbox-write guarantee as
    save_draft — nothing here can send; Casey reviews and sends from Gmail
    or the dashboard.

    Returns {"to", "cc", "subject", "preview"}. Raises on any failure.
    """
    to = (to or "").strip()
    subject = " ".join((subject or "").split())
    if not (to or cc or bcc):
        raise ValueError("add someone to To, Cc or Bcc")
    if not subject:
        raise ValueError("subject is required")
    if not (body or "").strip():
        raise ValueError("empty draft body")

    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        msg = EmailMessage()
        msg["From"] = mc.from_header(account, user, env)
        if to:
            msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Subject"] = subject
        msg.set_content(body)
        _mark_agent(msg, agent)

        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, appended = imap.append(f'"{drafts}"', r"(\Draft)",
                                    imaplib.Time2Internaldate(time.time()),
                                    msg.as_bytes())
        if typ != "OK":
            raise RuntimeError(f"APPEND to {drafts} failed")
        _label_agent(imap, drafts, appended, agent, account, log)

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
    to Trash (Casey 2026-09-27: "it's ok to delete and resave"). A reply keeps
    its In-Reply-To/References so Gmail keeps it on its thread. If the old
    draft can't be removed, the new one is already safe and the caller hears
    about the leftover copy."""
    import mail_actions
    if not (body or "").strip():
        raise ValueError("empty draft body")
    if not ((to or "").strip() or (cc or "").strip() or (bcc or "").strip()):
        raise ValueError("add someone to To, Cc or Bcc")
    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        msg = EmailMessage()
        msg["From"] = mc.from_header(account, user, env)
        if to:
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
               log=print, model: str = DRAFT_MODEL, agent: dict | None = None) -> dict:
    """AI-draft a reply and save it straight to Drafts (the one-click path).

    Thin wrapper: generate_draft() -> save_draft(). Kept for the existing
    single "Draft" button; the dashboard's edit-before-saving flow calls the
    two steps separately instead.
    """
    env = env or load_env()
    draft = generate_draft(thread_id, account, env=env, model=model)
    return save_draft(thread_id, account, draft["body"], to=draft["to"],
                      cc=draft["cc"], subject=draft["subject"], env=env, log=log,
                      agent=agent)


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
