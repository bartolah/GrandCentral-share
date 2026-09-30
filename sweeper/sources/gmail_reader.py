"""Gmail IMAP reader (Phase 1).

Read-only. Connects to imap.gmail.com over TLS using an app password from
config/.env, sweeps the last N days of INBOX + Sent for one account, groups
messages into Gmail threads (via the X-GM-THRID extension), and returns one
record per thread with the facts the classifier and state-builder need:

    - subject, participants, message count
    - the LATEST message: who it's from, inbound vs outbound (relative to
      Bart's own address), its timestamp, age in days, and a text snippet

Reply-debt is computed structurally downstream from `last_direction` /
`last_from` rather than guessed by the model — the reader just reports facts.

Nothing here sends, deletes, flags, or moves mail. The only IMAP verbs used
are SELECT (readonly), SEARCH, and FETCH with BODY.PEEK (never marks \\Seen).

Run standalone:
    cd sweeper
    ../.venv/bin/python -m sources.gmail_reader --account personal --dry-run
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime

try:  # allow both `python -m sources.gmail_reader` and direct execution
    from ..common import load_env, require_env, iso_z
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common import load_env, require_env, iso_z

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

# Map short account name -> the env keys holding its credentials. Derived from
# config/.env by convention: any GMAIL_<SLUG>_USER paired with a matching
# GMAIL_<SLUG>_APP_PASSWORD defines an account whose slug is <SLUG> lowercased.
# This is what lets the Connections UI add a Gmail by writing two .env keys —
# no code edit per account. `personal`/`connectblue` derive unchanged, so every
# existing DB row, override, and UI chip keeps working.
_ACCT_RE = re.compile(r"^GMAIL_([A-Z0-9_]+)_USER$")


def scan_accounts(env: dict) -> dict:
    """Build {slug: (user_key, pass_key)} from the env's GMAIL_*_USER keys.

    Only pairs with a matching *_APP_PASSWORD key are included. Order is
    deterministic (personal/connectblue first, then the rest sorted) so the UI
    and account chips stay stable across restarts.
    """
    found = {}
    for key in env:
        m = _ACCT_RE.match(key)
        if not m:
            continue
        stem = m.group(1)
        pass_key = f"GMAIL_{stem}_APP_PASSWORD"
        if pass_key in env:
            found[stem.lower()] = (key, pass_key)
    order = ["personal", "connectblue"]
    ranked = sorted(found, key=lambda s: (order.index(s) if s in order else len(order), s))
    return {s: found[s] for s in ranked}


# Populated at import from config/.env; mutated in place by refresh_accounts()
# so long-lived processes (dashboard/serve.py) see a newly added account
# without a restart — importers hold the SAME dict object.
ACCOUNTS: dict = {}
ACCOUNTS.update(scan_accounts(load_env()))


def refresh_accounts(env: dict | None = None) -> dict:
    """Re-derive ACCOUNTS from config/.env, mutating the shared dict in place."""
    ACCOUNTS.clear()
    ACCOUNTS.update(scan_accounts(env if env is not None else load_env()))
    return ACCOUNTS

SNIPPET_CHARS = 600
MAX_THREADS = 250  # safety bound on how many threads we hydrate per account


# --------------------------------------------------------------------------
# small parsing helpers
# --------------------------------------------------------------------------
def _decode(value: str) -> str:
    """Decode a possibly RFC2047-encoded header into a plain string."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def _addr_list(raw: str) -> list:
    """['Name <a@b.com>', ...] from a raw address header."""
    out = []
    for name, addr in getaddresses([raw or ""]):
        name = _decode(name)
        if name and addr:
            out.append(f"{name} <{addr.lower()}>")
        elif addr:
            out.append(addr.lower())
    return out


def _first_email(raw: str) -> str:
    for _name, addr in getaddresses([raw or ""]):
        if addr:
            return addr.lower()
    return ""


def _body_snippet(msg: email.message.Message) -> str:
    """Best-effort plain-text snippet from a parsed email message."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get("Content-Disposition", "").lower().startswith("attachment"):
                continue
            if part.get_content_type() == "text/plain":
                text = _payload_text(part)
                if text:
                    break
        if not text:  # fall back to any text/html, stripped
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    text = _strip_html(_payload_text(part))
                    if text:
                        break
    else:
        text = _payload_text(msg)
        if msg.get_content_type() == "text/html":
            text = _strip_html(text)

    text = re.sub(r"\s+", " ", text).strip()
    return text[:SNIPPET_CHARS]


def _payload_text(part) -> str:
    try:
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    except Exception:
        return ""


def _strip_html(html: str) -> str:
    """HTML -> text, keeping block structure as newlines (callers that want
    a one-line snippet collapse whitespace afterwards anyway)."""
    import html as html_mod
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|blockquote|table)>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return html_mod.unescape(text)


# --------------------------------------------------------------------------
# IMAP session
# --------------------------------------------------------------------------
def _find_sent_folder(imap: imaplib.IMAP4_SSL) -> str:
    """Locate the Sent mailbox by its \\Sent special-use flag; fall back to
    the Gmail default name."""
    typ, data = imap.list()
    if typ == "OK":
        for raw in data:
            line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
            if "\\Sent" in line:
                # mailbox name is the quoted string at the end of the LIST line
                m = re.search(r'"([^"]+)"\s*$', line)
                if m:
                    return m.group(1)
    return "[Gmail]/Sent Mail"


def _search_since(imap: imaplib.IMAP4_SSL, mailbox: str, since_date: str | None) -> list:
    """Return UIDs in `mailbox` newer than since_date (readonly select);
    since_date=None means the whole mailbox (E17 backfill)."""
    typ, _ = imap.select(f'"{mailbox}"', readonly=True)
    if typ != "OK":
        return []
    args = ("ALL",) if since_date is None else ("SINCE", since_date)
    typ, data = imap.uid("SEARCH", None, *args)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def _fetch_headers(imap: imaplib.IMAP4_SSL, uids: list, include_flags: bool = False) -> dict:
    """Fetch lightweight header + thread-id + internaldate for each UID.

    Returns {uid: {thrid, from, to, cc, subject, date, dt}} using BODY.PEEK so
    messages are never marked \\Seen. With include_flags=True (mail_sync),
    also FETCHes FLAGS + BODYSTRUCTURE and adds `flags`, `list_unsub`,
    `content_type`, and `attach_count` keys — the sweep's call sites are
    unaffected.
    """
    out: dict[str, dict] = {}
    if not uids:
        return out
    flags_item = "FLAGS X-GM-LABELS BODYSTRUCTURE " if include_flags else ""
    fields = (f"(X-GM-THRID {flags_item}INTERNALDATE BODY.PEEK[HEADER.FIELDS "
              "(FROM TO CC SUBJECT DATE MESSAGE-ID LIST-UNSUBSCRIBE LIST-ID "
              "PRECEDENCE CONTENT-TYPE AUTO-SUBMITTED X-AUTOREPLY X-AUTORESPOND "
              "FEEDBACK-ID)])")
    # chunk to keep FETCH command sizes sane
    for i in range(0, len(uids), 200):
        chunk = uids[i : i + 200]
        typ, data = imap.uid("FETCH", b",".join(chunk), fields)
        if typ != "OK" or not data:
            continue
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta = item[0].decode(errors="replace")
            uid_m = re.search(r"UID (\d+)", meta)
            thr_m = re.search(r"X-GM-THRID (\d+)", meta)
            if not uid_m or not thr_m:
                continue
            hdr = email.message_from_bytes(item[1])
            rec = {
                "thrid": thr_m.group(1),
                "from": _decode(hdr.get("From", "")),
                "to": hdr.get("To", ""),
                "cc": hdr.get("Cc", ""),
                "subject": _decode(hdr.get("Subject", "")),
                "date_hdr": hdr.get("Date", ""),
                "bulk": _is_bulk(hdr),
                "auto_reply": _is_auto_reply(hdr),
            }
            if include_flags:
                fl_m = re.search(r"FLAGS \(([^)]*)\)", meta)
                rec["flags"] = fl_m.group(1) if fl_m else ""
                rec["labels"] = _labels_from_meta(meta)
                rec["list_unsub"] = bool(hdr.get("List-Unsubscribe"))
                # E223: the sending platform, for spotting fundraising blasts.
                rec["list_unsub_url"] = str(hdr.get("List-Unsubscribe", "") or "")[:500]
                rec["feedback_id"] = str(hdr.get("Feedback-ID", "") or "")[:200]
                rec["content_type"] = (hdr.get("Content-Type", "") or "").lower()
                rec["attach_count"] = _attach_count(meta)
            out[uid_m.group(1)] = rec
    return out


# Attachment EXISTENCE from BODYSTRUCTURE (E4-P1): a flat count of
# "attachment" disposition tokens — no nested-paren parsing needed, and
# ignoring inline image/* parts so newsletters' embedded logos don't count.
# If the server orders BODYSTRUCTURE after the header literal or literalizes
# a filename param, the count is just 0: a missing 📎, never an error.
_ATTACH_DISP_RE = re.compile(r'\("attachment"', re.I)

# E18: user labels from the FETCH meta line — X-GM-LABELS ("Label A" Atom …).
# System labels (\Inbox, \Sent, …) are dropped; quoted-string escapes undone.
_LABEL_TOKEN_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


def _labels_from_meta(meta: str) -> list:
    m = re.search(r"X-GM-LABELS \(([^)]*)\)", meta)
    if not m:
        return []
    out = []
    for q, a in _LABEL_TOKEN_RE.findall(m.group(1)):
        lab = (q or a).replace("\\\\", "\\").replace('\\"', '"')
        if lab and not lab.startswith("\\"):
            out.append(lab)
    return out


def _attach_count(meta: str) -> int:
    m = re.search(r"BODYSTRUCTURE\s+\(", meta)
    if not m:
        return 0
    return len(_ATTACH_DISP_RE.findall(meta, m.start()))


# Senders that categorically don't expect a reply. Conservative on purpose —
# the List-Unsubscribe / List-Id / Precedence headers do the heavy lifting,
# this only catches bulk senders that omit them. The noreply tokens match
# anywhere in the local part (drive-shares-noreply@, no_reply@); the broader
# tokens (notifications, newsletter…) only at the start, to avoid false
# positives on real people's addresses.
_NOREPLY_ANY_RE = re.compile(
    r"(^|[._+-])(no[-_]?reply|do[-_]?not[-_]?reply|donotreply)([._+-]|$)", re.I)
_BULK_START_RE = re.compile(
    r"^(notifications?|newsletters?|mailer(-daemon)?|bounces?|alerts?|digest)"
    r"([._+-]|$)", re.I)


# Structural auto-reply markers (E6): the Auto-Submitted header (RFC 3834,
# any value except "no"), the common X-Autoreply/X-Autorespond headers, and
# the subject prefixes every mail client stamps on OOO replies. These never
# belong in Top 5 — a robot saying "I'm away" owes nothing.
_AUTO_SUBJ_RE = re.compile(
    r"^\s*(automatic reply|auto[ -]?reply|autoreply|out of office|ooo)\b", re.I)


def _is_auto_reply(hdr) -> bool:
    auto = (hdr.get("Auto-Submitted", "") or "").lower().strip()
    if auto and auto != "no":
        return True
    if hdr.get("X-Autoreply") or hdr.get("X-Autorespond"):
        return True
    return bool(_AUTO_SUBJ_RE.match(_decode(hdr.get("Subject", ""))))


def _is_bulk(hdr) -> bool:
    """Structural bulk/marketing signals on one message's headers."""
    if hdr.get("List-Unsubscribe") or hdr.get("List-Id"):
        return True
    if (hdr.get("Precedence", "") or "").lower().strip() in ("bulk", "list", "junk"):
        return True
    sender = _first_email(hdr.get("From", ""))
    if not sender:
        return False
    local = sender.split("@")[0]
    return bool(_NOREPLY_ANY_RE.search(local) or _BULK_START_RE.match(local))


def _fetch_snippet(imap: imaplib.IMAP4_SSL, mailbox: str, uid: str) -> str:
    typ, _ = imap.select(f'"{mailbox}"', readonly=True)
    if typ != "OK":
        return ""
    typ, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return ""
    return _body_snippet(email.message_from_bytes(data[0][1]))


def _dt_from(meta: dict) -> datetime:
    """Parse a message Date header to an aware UTC datetime (epoch on failure)."""
    try:
        dt = parsedate_to_datetime(meta.get("date_hdr", ""))
        if dt is None:
            raise ValueError
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------
def read_account(account: str, env: dict, lookback_days: int = 7,
                 log=print, limit: int = MAX_THREADS,
                 inbox_only: bool = False, all_time: bool = False) -> list:
    """Return a list of thread records for one Gmail account.

    Raises on connection/login failure so the orchestrator can mark the source
    absent and still write the JSON (spec §3).
    """
    if account not in ACCOUNTS:
        raise ValueError(f"unknown account {account!r}; known: {list(ACCOUNTS)}")
    user_key, pass_key = ACCOUNTS[account]
    user = require_env(env, user_key)
    password = require_env(env, pass_key)

    since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    now = datetime.now(timezone.utc)

    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, password)
        log(f"gmail:{account} logged in as {user}")

        if inbox_only:
            folders = [("INBOX", "INBOX")]   # E17 backfill: no Sent scan
        else:
            sent_folder = _find_sent_folder(imap)
            folders = [("INBOX", "INBOX"), ("Sent", sent_folder)]
        if all_time:
            since_date = None

        # uid -> (label, mailbox) so we can re-fetch snippets from the right box
        meta_by_uid: dict[str, dict] = {}
        loc_by_uid: dict[str, tuple] = {}
        for label, mailbox in folders:
            uids = _search_since(imap, mailbox, since_date)
            log(f"gmail:{account} {label}: {len(uids)} messages in last {lookback_days}d")
            headers = _fetch_headers(imap, uids)
            for uid, meta in headers.items():
                key = f"{label}:{uid}"
                meta["label"] = label
                meta_by_uid[key] = meta
                loc_by_uid[key] = (label, mailbox, uid)

        # group by Gmail thread id, tracking the latest message per thread
        threads: dict[str, dict] = {}
        for key, meta in meta_by_uid.items():
            thrid = meta["thrid"]
            dt = _dt_from(meta)
            t = threads.setdefault(thrid, {
                "participants": set(),
                "subjects": set(),
                "count": 0,
                "latest_dt": datetime(1970, 1, 1, tzinfo=timezone.utc),
                "latest_key": None,
            })
            t["count"] += 1
            if meta["subject"]:
                t["subjects"].add(meta["subject"])
            for a in _addr_list(meta["from"]):
                t["participants"].add(a)
            for a in _addr_list(meta["to"]) + _addr_list(meta["cc"]):
                t["participants"].add(a)
            if dt >= t["latest_dt"]:
                t["latest_dt"] = dt
                t["latest_key"] = key

        # sort threads by recency, bound the count, hydrate snippets
        ordered = sorted(threads.items(), key=lambda kv: kv[1]["latest_dt"], reverse=True)
        if len(ordered) > limit:
            log(f"gmail:{account} capping {len(ordered)} threads at {limit}")
            ordered = ordered[:limit]

        records = []
        for thrid, t in ordered:
            key = t["latest_key"]
            meta = meta_by_uid[key]
            label, mailbox, uid = loc_by_uid[key]
            last_from_email = _first_email(meta["from"])
            direction = "outbound" if last_from_email == user.lower() else "inbound"
            latest_dt = t["latest_dt"]
            age_days = max(0, (now - latest_dt).days)
            snippet = _fetch_snippet(imap, mailbox, uid)

            subject = meta["subject"] or (next(iter(t["subjects"]), "") if t["subjects"] else "(no subject)")
            records.append({
                "account": account,
                "account_email": user.lower(),
                "source": "gmail",
                "thread_id": thrid,
                "subject": subject,
                "participants": sorted(t["participants"]),
                "message_count": t["count"],
                "last_from": meta["from"],
                "last_from_email": last_from_email,
                "last_direction": direction,
                "last_folder": label,
                "last_date": iso_z(latest_dt),
                "age_days": age_days,
                "snippet": snippet,
                "bulk": bool(meta.get("bulk")),
                "auto_reply": bool(meta.get("auto_reply")),
            })
        log(f"gmail:{account} built {len(records)} thread records")
        return records
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gmail IMAP reader (read-only)")
    ap.add_argument("--account", required=True, choices=list(ACCOUNTS))
    ap.add_argument("--days", type=int, default=7, help="lookback window")
    ap.add_argument("--limit", type=int, default=25 if "--dry-run" in (argv or sys.argv) else MAX_THREADS)
    ap.add_argument("--dry-run", action="store_true",
                    help="read and summarize a small sample; makes no writes anywhere")
    ap.add_argument("--json", action="store_true", help="dump records as JSON")
    args = ap.parse_args(argv)

    env = load_env()
    records = read_account(args.account, env, lookback_days=args.days, limit=args.limit)

    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 0

    owed = [r for r in records if r["last_direction"] == "inbound"]
    print(f"\n{len(records)} threads  |  {len(owed)} with an inbound latest message (candidate reply debt)\n")
    for r in records[: args.limit]:
        arrow = "<-" if r["last_direction"] == "inbound" else "->"
        who = r["last_from"][:38]
        print(f"  {r['age_days']:>2}d {arrow} {who:<40} {r['subject'][:60]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
