"""Shared IMAP helpers for thread-scoped mailbox operations.

Anything that starts by locating one of Bart's Gmail threads by its
X-GM-THRID and reading its messages lives here: `draft_reply.py` (compose +
APPEND to Drafts) and `mail_actions.py` (archive/star/label/fetch-full-thread)
both build on this instead of duplicating the connect/search/parse logic.

Nothing in this module writes to a mailbox. Callers do their own SELECT
(readonly or not) and STORE/APPEND; this module only connects, searches, and
parses.
"""

from __future__ import annotations

import email
import imaplib
import re
from email.utils import getaddresses, parsedate_to_datetime

try:
    from .common import require_env
    from .sources.gmail_reader import (
        ACCOUNTS, IMAP_HOST, IMAP_PORT, _decode, _payload_text, _strip_html,
    )
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import require_env
    from sources.gmail_reader import (
        ACCOUNTS, IMAP_HOST, IMAP_PORT, _decode, _payload_text, _strip_html,
    )

CONTEXT_CHARS = 1500
_THRID_RE = re.compile(r"\A\d{1,25}\Z")


def validate_thread_id(thread_id) -> str:
    """Confirm `thread_id` looks like a Gmail X-GM-THRID (decimal digits)."""
    s = str(thread_id)
    if not _THRID_RE.match(s):
        raise ValueError(f"bad thread_id {thread_id!r}")
    return s


def connect(account: str, env: dict) -> tuple:
    """Login and return (imap, user_email). Raises on bad account/creds."""
    if account not in ACCOUNTS:
        raise ValueError(f"unknown account {account!r}")
    user_key, pass_key = ACCOUNTS[account]
    user = require_env(env, user_key)
    password = require_env(env, pass_key)
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    imap.login(user, password)
    return imap, user


def find_folder(imap: imaplib.IMAP4_SSL, flag: str, fallback: str) -> str:
    """Locate a special-use mailbox (\\Drafts, \\All, ...) by its LIST flag."""
    typ, data = imap.list()
    if typ == "OK":
        for raw in data:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            if flag in line:
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    return m.group(1)
    return fallback


def search_thread_uids(imap: imaplib.IMAP4_SSL, mailbox: str, thread_id: str,
                       readonly: bool = True) -> list:
    """SELECT `mailbox` and SEARCH for X-GM-THRID; return matching UIDs (bytes)."""
    typ, _ = imap.select(f'"{mailbox}"', readonly=readonly)
    if typ != "OK":
        raise RuntimeError(f"cannot open {mailbox}")
    typ, data = imap.uid("SEARCH", None, "X-GM-THRID", str(thread_id))
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _unfold(value: str) -> str:
    """Collapse RFC 5322 header folding (CRLF + leading whitespace) to single
    spaces — EmailMessage refuses header values containing CR/LF, so a folded
    References from a long thread would otherwise 400 every reply."""
    return re.sub(r"\s+", " ", value or "").strip()


def body_text(msg: email.message.Message, cap: int = CONTEXT_CHARS,
              keep_newlines: bool = False) -> str:
    """Plain-text body of a parsed message, HTML stripped, bounded.

    keep_newlines=False collapses to one line (prompt snippets);
    keep_newlines=True preserves paragraph structure (the reading view):
    spaces/tabs collapse within lines, blank runs collapse to one blank line.
    """
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and \
               not part.get("Content-Disposition", "").lower().startswith("attachment"):
                text = _payload_text(part)
                if text:
                    break
        if not text:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    text = _strip_html(_payload_text(part))
                    if text:
                        break
    else:
        text = _payload_text(msg)
        if msg.get_content_type() == "text/html":
            text = _strip_html(text)

    if not keep_newlines:
        return re.sub(r"\s+", " ", text).strip()[:cap]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:cap]


HTML_CAP = 300_000  # cap per message; bigger than any sane newsletter

# Server-side HTML sanitize (E7-P1). Defense in depth: the client ALSO
# renders inside a sandboxed iframe with scripts disabled, so this strip is
# the second lock, not the only one. Removes script/style-adjacent active
# content, event handlers, and javascript: URLs; keeps layout/images.
_HTML_STRIP_TAGS_RE = re.compile(
    r"<\s*(script|iframe|object|embed|form|base|meta)\b[^>]*>.*?<\s*/\s*\1\s*>|"
    r"<\s*(script|iframe|object|embed|form|base|meta)\b[^>]*/?\s*>",
    re.I | re.S)
_HTML_ON_ATTR_RE = re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_HTML_JS_URL_RE = re.compile(r"(href|src|action)\s*=\s*([\"']?)\s*javascript:[^\"'>\s]*\2", re.I)


def sanitize_html(html: str) -> str:
    html = _HTML_STRIP_TAGS_RE.sub("", html)
    html = _HTML_ON_ATTR_RE.sub("", html)
    html = _HTML_JS_URL_RE.sub(r'\1="#"', html)
    return html[:HTML_CAP]


def body_html(msg: email.message.Message) -> str:
    """The message's text/html part, sanitized; '' when there is none."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html" and \
               not (part.get("Content-Disposition", "") or "").lower().startswith("attachment"):
                html = _payload_text(part)
                if html:
                    return sanitize_html(html)
        return ""
    if msg.get_content_type() == "text/html":
        return sanitize_html(_payload_text(msg))
    return ""


def is_attachment_part(part) -> bool:
    """One rule for which parts are attachments, shared by parse_msg (the
    chips) and mail_actions.fetch_attachment (the download), whose idx must
    agree. E267: an embedded image (Content-ID, often no filename) counts too,
    so the pane can show it in place of its cid: reference."""
    if part.get_content_maintype() == "multipart":
        return False
    if part.get_content_disposition() == "attachment":
        return True
    if part.get_filename() and not part.get_content_type().startswith("text/"):
        return True
    return part.get_content_maintype() == "image" and bool(part.get("Content-ID"))


def content_id(part) -> str:
    """'<image001.png@01DB>' -> 'image001.png@01DB' ('' when absent)."""
    return str(part.get("Content-ID") or "").strip().strip("<>").strip()


def parse_msg(raw: bytes, cap: int = CONTEXT_CHARS, keep_newlines: bool = False,
              want_html: bool = False) -> dict:
    """A fetched RFC-822 message -> the fields draft-reply and expand need.

    Attachment metadata is existence-only (E4-P1): filenames/types/sizes are
    reported, content is decoded solely to measure its length and discarded.
    want_html=True (the thread view, E7-P1) adds a sanitized `body_html`.
    """
    msg = email.message_from_bytes(raw)
    dt = None
    try:
        dt = parsedate_to_datetime(msg.get("Date", ""))
    except Exception:
        pass

    attachments, inline_images = [], 0
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        fname = part.get_filename()  # email pkg decodes RFC2231/encoded words
        if is_attachment_part(part):
            if len(attachments) < 20:
                payload = part.get_payload(decode=True) or b""
                attachments.append({
                    # collapse header line-folds inside long filenames
                    "filename": re.sub(r"\s+", " ", _decode(fname or "(unnamed)")),
                    "content_type": part.get_content_type(),
                    "size": len(payload),
                    "cid": content_id(part),   # E267: matches <img src="cid:…">
                })
        elif part.get_content_maintype() == "image":
            inline_images += 1

    return {
        "from": _decode(msg.get("From", "")),
        "to": msg.get("To", ""),
        "cc": msg.get("Cc", ""),
        "bcc": msg.get("Bcc", ""),   # present only on own sent copies
        "subject": _unfold(_decode(msg.get("Subject", ""))),
        "message_id": _unfold(msg.get("Message-ID", "")),
        "references": _unfold(msg.get("References", "")),
        "dt": dt,
        "body": body_text(msg, cap=cap, keep_newlines=keep_newlines),
        "body_html": body_html(msg) if want_html else "",
        "attachments": attachments,
        "inline_images": inline_images,
    }


def addrs(*headers: str) -> list:
    """Deduped [(name, addr), ...] across several raw address headers."""
    seen, out = set(), []
    for name, addr in getaddresses([h or "" for h in headers]):
        a = addr.lower()
        if a and a not in seen:
            seen.add(a)
            out.append((name, addr))
    return out


def fetch_messages(imap: imaplib.IMAP4_SSL, uids: list, cap: int = CONTEXT_CHARS,
                   keep_newlines: bool = False, want_html: bool = False) -> list:
    """BODY.PEEK[] fetch + parse each uid; returns messages oldest -> newest."""
    messages = []
    for uid in uids:
        typ, fetched = imap.uid("FETCH", uid, "(BODY.PEEK[])")
        if typ == "OK" and fetched and isinstance(fetched[0], tuple):
            m = parse_msg(fetched[0][1], cap=cap,
                          keep_newlines=keep_newlines, want_html=want_html)
            # E82: the uid rides along so /attachment can address one part
            m["uid"] = uid.decode() if isinstance(uid, bytes) else str(uid)
            messages.append(m)
    messages.sort(key=lambda m: (m["dt"] is None, m["dt"]))
    return messages


def reply_headers(imap: imaplib.IMAP4_SSL, user: str, thread_id: str,
                  context_msgs: int = 4) -> dict:
    """Read a thread from All Mail (read-only) and derive reply headers +
    the transcript, without writing anything. Shared by draft_reply.py
    (Drafts APPEND) and send_mail.py (the E3-P4 confirmed-send path).

    Returns {"to", "cc", "subject", "in_reply_to", "references", "messages"}.
    """
    all_mail = find_folder(imap, "\\All", "[Gmail]/All Mail")
    uids = search_thread_uids(imap, all_mail, thread_id, readonly=True)
    if not uids:
        raise RuntimeError("thread not found in All Mail")

    messages = fetch_messages(imap, uids[-context_msgs:])
    if not messages:
        raise RuntimeError("could not read any thread messages")
    last = messages[-1]

    # Reply target: the newest message NOT from Bart (reply-debt case);
    # fall back to the newest message.
    me = user.lower()
    target = next((m for m in reversed(messages)
                   if me not in [a.lower() for _n, a in addrs(m["from"])]),
                  last)

    to_pairs = addrs(target["from"])
    cc_pairs = [(n, a) for n, a in addrs(target["to"], target["cc"])
                if a.lower() != me and a.lower() not in
                {ad.lower() for _n, ad in to_pairs}]

    subject = target["subject"] or last["subject"] or "(no subject)"
    if not subject.lower().startswith("re:"):
        subject = "Re: " + subject

    return {
        "to": ", ".join(f"{n} <{a}>" if n else a for n, a in to_pairs),
        "cc": ", ".join(f"{n} <{a}>" if n else a for n, a in cc_pairs),
        "subject": subject,
        "in_reply_to": last["message_id"],
        "references": (last["references"] + " " + last["message_id"]).strip(),
        "messages": messages,
    }
