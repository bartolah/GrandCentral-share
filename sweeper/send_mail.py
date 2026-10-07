"""Send a single human-confirmed email over SMTP — the system's ONE send path.

Approved by Bart 2026-07-10 (E3-P4), amending BUILD_SPEC §6's drafts-only
rule. Extended to NEW emails (not just replies) by Bart's explicit decision
2026-07-29 (E53): "New email and emails should be able to be sent entirely
from within Grand Central." Same guardrails, same audit, same single path.
The guardrails are non-negotiable:

  - Send exists ONLY behind the dashboard's confirm dialog (POST /send with
    confirm:true). It is never exposed as a tool to the command bar, to any
    Claude tool-calling surface, or to OpenClaw skills.
  - One thread per call; explicit recipients required (the exact list the
    human saw in the confirm dialog); at most MAX_RECIPIENTS across To+Cc;
    no list-shaped or bulk anything.
  - Every send is logged to the mail_sends table.

There is deliberately NO CLI runner here — an intentional exception to the
spec §7 "independently runnable" convention, so no script, cron job, or
agent can send by shelling out. Sending happens through a human click or
not at all.

SMTP is smtp.gmail.com:465 with the same per-account app passwords the IMAP
readers use; Gmail automatically saves the sent message to that account's
Sent folder (no APPEND needed; any later IMAP echo dedupes by Message-ID).
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from email.utils import getaddresses

try:
    from . import db
    from . import mail_common as mc
    from .common import load_env, require_env
    from .sources.gmail_reader import ACCOUNTS
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import db
    import mail_common as mc
    from common import load_env, require_env
    from sources.gmail_reader import ACCOUNTS

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
MAX_BODY_CHARS = 20000
MAX_RECIPIENTS = 10


def _tracking(account: str, env: dict, to: str, cc: str, bcc: str, body: str,
              body_html: str | None, track: bool | None, log=print) -> dict | None:
    """E269 (BUILD_SPEC §6, 2026-10-06): if this send is tracked, the HTML
    part with its image and signed links, plus the token and a Message-ID to
    find a new email's thread later. Any failure here means the email goes
    out untracked — tracking never blocks or changes a send otherwise."""
    try:
        import tracking
        from email.utils import make_msgid
        conn = db.connect()
        try:
            d = tracking.decide(conn, account, env, to, cc, bcc, override=track)
        finally:
            conn.close()
        if not d["on"]:
            return None
        token = tracking.new_token()
        html, links = tracking.prepare(body_html, body, token, d["domain"],
                                       env["TRACK_SECRET"].strip(), d["clicks"])
        return {"token": token, "html": html, "links": links, "clicks": d["clicks"],
                "message_id": make_msgid(domain=d["domain"])}
    except Exception as e:
        log(f"send_mail: tracking skipped ({e!r}) — sending untracked")
        return None


def _track_record(trk: dict | None, account: str, thread_id: str, send_id, subject: str,
                  to: str, cc: str, log=print) -> None:
    if not trk:
        return
    try:
        import tracking
        conn = db.connect()
        try:
            tracking.record(conn, trk["token"], account, thread_id, send_id, subject, to, cc,
                            trk["links"], trk["message_id"], trk["clicks"])
        finally:
            conn.close()
    except Exception as e:
        log(f"send_mail: tracked send not recorded ({e!r})")


def _parse_addrs(raw: str) -> list:
    """'Name <a@b>, c@d' -> [(name, addr), ...], validated."""
    out = []
    for name, addr in getaddresses([raw or ""]):
        if not addr:
            continue
        if "@" not in addr or " " in addr:
            raise ValueError(f"bad address {addr!r}")
        out.append((name, addr))
    return out


def _apply_attachments(msg, attachments):
    """E82/E104 (§6 amended): ≤3 files + ≤4 inline images, 5MB total,
    validated/decoded in serve.py; riding the SAME human-confirmed send.
    Inline entries (cid set) attach RELATED to the HTML alternative so
    <img src="cid:…"> renders in place; never stored server-side."""
    atts = attachments or []
    inline = [a for a in atts if a.get("cid")]
    regular = [a for a in atts if not a.get("cid")]
    if inline:
        html_part = None
        if msg.get_content_type() == "multipart/alternative":
            html_part = msg.get_payload()[-1]
        for a in inline:
            mt, _, st = str(a.get("mime") or "image/png").partition("/")
            if html_part is not None:
                html_part.add_related(a["data"], maintype=mt or "image",
                                      subtype=st or "png",
                                      cid=f"<{a['cid']}>")
            else:            # no HTML body to relate to — plain attach
                regular.append(a)
    for a in regular[:3]:
        mime = str(a.get("mime") or "application/octet-stream")
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(a["data"], maintype=maintype or "application",
                           subtype=subtype or "octet-stream",
                           filename=str(a.get("name") or "file")[:120])


def send_reply(thread_id: str, account: str, body: str, to: str,  # noqa: E501
               *, attachments: list | None = None,
               cc: str | None = None, subject: str | None = None,
               bcc: str | None = None, body_html: str | None = None,
               track: bool | None = None,
               env: dict | None = None, log=print) -> dict:
    """Send one reply on `thread_id`. `to` is REQUIRED and authoritative —
    it must be the recipient list the human confirmed; nothing is derived
    silently except threading headers and a missing subject. Bcc counts
    toward the recipient cap; smtplib.send_message delivers to it but
    strips the header from the transmitted copy."""
    import private
    private.guard(account, thread_id)  # E258: a Private thread does not exist here
    body = (body or "").strip()
    if not body:
        raise ValueError("empty body")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body over {MAX_BODY_CHARS} chars")
    thread_id = mc.validate_thread_id(thread_id)

    to_pairs = _parse_addrs(to)
    cc_pairs = _parse_addrs(cc or "")
    bcc_pairs = _parse_addrs(bcc or "")
    if not to_pairs:
        raise ValueError("no valid To recipient")
    if len(to_pairs) + len(cc_pairs) + len(bcc_pairs) > MAX_RECIPIENTS:
        raise ValueError(f"more than {MAX_RECIPIENTS} recipients")

    env = env or load_env()
    imap, user = mc.connect(account, env)
    try:
        headers = mc.reply_headers(imap, user, thread_id)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    if not subject:
        subject = headers["subject"]
    subject = " ".join(subject.split())

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(f"{n} <{a}>" if n else a for n, a in to_pairs)
    if cc_pairs:
        msg["Cc"] = ", ".join(f"{n} <{a}>" if n else a for n, a in cc_pairs)
    if bcc_pairs:
        msg["Bcc"] = ", ".join(f"{n} <{a}>" if n else a for n, a in bcc_pairs)
    msg["Subject"] = subject
    if headers["in_reply_to"]:
        msg["In-Reply-To"] = headers["in_reply_to"]
        msg["References"] = headers["references"]
    trk = _tracking(account, env, msg["To"], msg.get("Cc", ""), msg.get("Bcc", ""), body, body_html, track, log)
    if trk:
        body_html = trk["html"]
        msg["Message-ID"] = trk["message_id"]
    msg.set_content(body)
    # E65: Bart-authored rich text rides as the HTML alternative; the plain
    # part above stays authoritative (and is what the audit measures).
    if body_html and body_html.strip():
        msg.add_alternative(body_html[:60000], subtype="html")
    _apply_attachments(msg, attachments)

    # audit values captured BEFORE send_message strips Bcc from its copy
    to_log, cc_log, bcc_log = msg["To"], msg.get("Cc", ""), msg.get("Bcc", "")

    _user_key, pass_key = ACCOUNTS[account]
    password = require_env(env, pass_key)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

    conn = db.connect()
    try:
        send_id = db.record_send(conn, thread_id=thread_id, account=account,
                                 to_addrs=to_log, cc_addrs=cc_log, bcc_addrs=bcc_log,
                                 subject=subject, body_chars=len(body))
    finally:
        conn.close()
    _track_record(trk, account, thread_id, send_id, subject, to_log, cc_log, log)

    log(f"send_mail: sent to {to_log} on {account} ({len(body)} chars)")
    return {"to": to_log, "cc": cc_log, "subject": subject, "sent": True, "tracked": bool(trk)}


def send_new(account: str, body: str, to: str, subject: str,
             cc: str | None = None, bcc: str | None = None,
             body_html: str | None = None, attachments: list | None = None,
             track: bool | None = None,
             env: dict | None = None, log=print) -> dict:
    """Send one NEW email (no thread) — E53, Bart's explicit §6 widening
    2026-07-29. Identical guardrails to send_reply: explicit human-confirmed
    recipients, MAX_RECIPIENTS cap, body cap, audited in mail_sends
    (thread_id='' marks a fresh send; Gmail assigns the real thread id and
    the next mail_sync sweep picks it up from Sent). No threading headers —
    this is a fresh conversation, subject REQUIRED."""
    body = (body or "").strip()
    if not body:
        raise ValueError("empty body")
    if len(body) > MAX_BODY_CHARS:
        raise ValueError(f"body over {MAX_BODY_CHARS} chars")
    subject = " ".join((subject or "").split())
    if not subject:
        raise ValueError("subject required for a new email")

    to_pairs = _parse_addrs(to)
    cc_pairs = _parse_addrs(cc or "")
    bcc_pairs = _parse_addrs(bcc or "")
    if not to_pairs:
        raise ValueError("no valid To recipient")
    if len(to_pairs) + len(cc_pairs) + len(bcc_pairs) > MAX_RECIPIENTS:
        raise ValueError(f"more than {MAX_RECIPIENTS} recipients")

    env = env or load_env()
    if account not in ACCOUNTS:
        raise ValueError(f"unknown account {account!r}")
    user_key, pass_key = ACCOUNTS[account]
    user = require_env(env, user_key)
    password = require_env(env, pass_key)

    msg = EmailMessage()
    msg["From"] = user
    msg["To"] = ", ".join(f"{n} <{a}>" if n else a for n, a in to_pairs)
    if cc_pairs:
        msg["Cc"] = ", ".join(f"{n} <{a}>" if n else a for n, a in cc_pairs)
    if bcc_pairs:
        msg["Bcc"] = ", ".join(f"{n} <{a}>" if n else a for n, a in bcc_pairs)
    msg["Subject"] = subject
    trk = _tracking(account, env, msg["To"], msg.get("Cc", ""), msg.get("Bcc", ""), body, body_html, track, log)
    if trk:
        body_html = trk["html"]
        msg["Message-ID"] = trk["message_id"]
    msg.set_content(body)
    if body_html and body_html.strip():
        msg.add_alternative(body_html[:60000], subtype="html")
    _apply_attachments(msg, attachments)

    to_log, cc_log, bcc_log = msg["To"], msg.get("Cc", ""), msg.get("Bcc", "")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)

    conn = db.connect()
    try:
        send_id = db.record_send(conn, thread_id="", account=account,
                                 to_addrs=to_log, cc_addrs=cc_log, bcc_addrs=bcc_log,
                                 subject=subject, body_chars=len(body))
    finally:
        conn.close()
    _track_record(trk, account, "", send_id, subject, to_log, cc_log, log)

    log(f"send_mail: sent NEW '{subject}' to {to_log} on {account} ({len(body)} chars)")
    return {"to": to_log, "cc": cc_log, "subject": subject, "sent": True, "tracked": bool(trk)}
