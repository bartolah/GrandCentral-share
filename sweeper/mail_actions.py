"""Gmail thread actions beyond drafting: read the full thread, archive, star,
and label. Approved by Bart 2026-07-04 alongside the light-email-client work
(BUILD_SPEC.md records the decision) — these are new mailbox writes beyond
the existing Drafts-only APPEND, but still narrowly scoped: no delete, no
send, and every action takes an explicit thread_id (no bulk/unscoped verbs).

    fetch_thread()      read-only, powers "expand" in the dashboard
    archive_thread()    removes the \\Inbox label (Gmail's real Archive verb)
    unarchive_thread()  re-adds \\Inbox — the snooze wake pass (E5-P1,
                        §6 amendment approved by Bart 2026-07-15)
    star_thread()       sets/clears the \\Flagged flag
    label_thread()      adds/removes an arbitrary user label
    thread_labels()     read-only: the thread's Gmail label set
    set_project_label() swaps the thread's GC/* mirror label (E5-P2)

Run standalone (archive/star/label default to --dry-run: they print the IMAP
command instead of sending it, so you can sanity-check against a real
thread-id before it does anything):
    cd sweeper
    ../.venv/bin/python -m mail_actions --account personal --thread-id ... --action star
    ../.venv/bin/python -m mail_actions --account personal --thread-id ... --action archive --live
"""

from __future__ import annotations

import argparse
import email
import re

try:
    from . import mail_common as mc
    from .common import load_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mail_common as mc
    from common import load_env

# Gmail label names: letters/digits/spaces/punctuation, "/" for nesting.
# Excludes characters that would break IMAP quoting or aren't real labels.
_LABEL_RE = re.compile(r'\A[^\\"(){}%*\r\n]{1,100}\Z')


def _quote(label: str) -> str:
    """IMAP-quote a label/flag name for use inside a STORE parenthesized list."""
    return '"' + label.replace("\\", "\\\\").replace('"', '\\"') + '"'


def validate_label(label: str) -> str:
    label = (label or "").strip()
    if not _LABEL_RE.match(label):
        raise ValueError(f"bad label {label!r}")
    return label


def fetch_thread(thread_id: str, account: str, env: dict | None = None,
                 with_labels: bool = False):
    """All messages in a thread, oldest -> newest, full body. Read-only.
    with_labels=True returns {"messages": [...], "labels": [...]} — the
    thread's Gmail label union from the same IMAP session (E5-P4, powers
    the thread view's label chips)."""
    thread_id = mc.validate_thread_id(thread_id)
    import private
    private.guard(account, thread_id)  # E258: a Private thread does not exist here
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=True)
        if not uids:
            raise RuntimeError("thread not found in All Mail")
        if _thread_is_labelled_private(imap, uids):
            import db
            c = db.connect()
            try:
                private.mark(c, account, thread_id)
            finally:
                c.close()
            raise ValueError("thread not found")
        messages = mc.fetch_messages(imap, uids, cap=6000, keep_newlines=True,
                                     want_html=True)
        # E269: Bart reading his own tracked mail here must never count as
        # an open or a click — drop the image, unwrap the links
        try:
            import tracking
            _tdoms = tracking.own_tracking_domains(env)
        except Exception:
            tracking, _tdoms = None, []
        _untrack = (lambda h: tracking.strip(h, _tdoms)) if _tdoms else (lambda h: h)
        out = [{
            "from": m["from"], "to": m["to"], "cc": m["cc"], "bcc": m["bcc"],
            "subject": m["subject"], "date": m["dt"].isoformat() if m["dt"] else None,
            "body": m["body"], "body_html": _untrack(m["body_html"]),
            "attachments": m["attachments"], "inline_images": m["inline_images"],
            "uid": m.get("uid"),
        } for m in messages]
        if not with_labels:
            return out
        labels: set = set()
        for uid in uids:
            typ, data = imap.uid("FETCH", uid, "(X-GM-LABELS)")
            if typ != "OK" or not data:
                continue
            for part in data:
                raw = part[0] if isinstance(part, tuple) else part
                if isinstance(raw, bytes):
                    labels |= _parse_labels(raw.decode("utf-8", "replace"))
        return {"messages": out, "labels": sorted(labels)}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _thread_is_labelled_private(imap, uids) -> bool:
    """E258: the label on ANY message makes the whole thread Private."""
    import private
    for uid in uids:
        typ, data = imap.uid("FETCH", uid, "(X-GM-LABELS)")
        for part in data or []:
            raw = part[0] if isinstance(part, tuple) else part
            if isinstance(raw, bytes) and private.has_label(
                    _parse_labels(raw.decode("utf-8", "replace"))):
                return True
    return False


def fetch_attachment(thread_id: str, account: str, uid: str, idx: int,
                     env: dict | None = None):
    """E82: decode ONE attachment part on demand — read-only, never stored.
    The uid must belong to the thread (checked against the thread's uid
    set) and idx follows parse_msg's attachment ordering exactly, so the
    chips the pane shows address the right part. Returns
    (filename, content_type, bytes)."""
    import private
    private.guard(account, thread_id)  # E258
    thread_id = mc.validate_thread_id(thread_id)
    idx = int(idx)
    if idx < 0 or idx > 19:
        raise ValueError("bad attachment index")
    wanted = str(int(str(uid)))          # digits only, or ValueError
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=True)
        have = {u.decode() if isinstance(u, bytes) else str(u) for u in uids}
        if wanted not in have:
            raise ValueError("uid not in thread")
        typ, fetched = imap.uid("FETCH", wanted, "(BODY.PEEK[])")
        if typ != "OK" or not fetched or not isinstance(fetched[0], tuple):
            raise RuntimeError("message fetch failed")
        msg = email.message_from_bytes(fetched[0][1])
        k = -1
        for part in msg.walk():
            if not mc.is_attachment_part(part):   # same rule as parse_msg (E267)
                continue
            k += 1
            if k == idx:
                data = part.get_payload(decode=True) or b""
                if len(data) > 25 * 1024 * 1024:
                    raise ValueError("attachment over 25MB — open it in Gmail")
                name = re.sub(r"\s+", " ", str(part.get_filename() or "attachment"))
                return name, part.get_content_type(), data
        raise ValueError("attachment not found")
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# One LIST response line: (\Flags) "delim" "Name" — name quoted or atom.
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"[^"]*"\s+(?:"(?P<q>(?:[^"\\]|\\.)*)"|(?P<atom>\S+))')


def _decode_mutf7(name: str) -> str:
    """Best-effort decode of IMAP modified UTF-7 folder names (&XXX- runs).
    ASCII names pass through untouched; a bad encoding returns the raw name."""
    if "&" not in name:
        return name
    try:
        return re.sub(
            r"&([^-]*)-",
            lambda m: "&" if not m.group(1) else
            ("+" + m.group(1).replace(",", "/") + "-").encode("ascii").decode("utf-7"),
            name)
    except Exception:
        return name


def list_account_labels(account: str, env: dict | None = None) -> list:
    """Read-only: every user-created label in the account (IMAP LIST),
    system [Gmail]/* folders and INBOX excluded — powers the thread view's
    label type-ahead."""
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        typ, data = imap.list('""', '"*"')
        if typ != "OK":
            raise RuntimeError("LIST failed")
        out = []
        for line in data or []:
            if isinstance(line, tuple):  # literal-form name; rare, skip
                continue
            m = _LIST_RE.match(line.decode("utf-8", "replace"))
            if not m:
                continue
            if r"\Noselect" in m.group("flags"):
                continue
            name = m.group("q") or m.group("atom") or ""
            name = _decode_mutf7(name.replace('\\"', '"').replace("\\\\", "\\"))
            if not name or name.upper() == "INBOX" or name.startswith("[Gmail]"):
                continue
            out.append(name)
        return sorted(out, key=str.lower)
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _store_all_mail(thread_id: str, account: str, item: str, value: str,
                    env: dict | None, log, dry_run: bool) -> dict:
    """SELECT All Mail (writable) and STORE `item value` on every message in
    the thread. Shared by star_thread and label_thread."""
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=dry_run)
        if not uids:
            raise RuntimeError("thread not found in All Mail")
        if dry_run:
            log(f"[dry-run] STORE {item} {value} on {len(uids)} message(s) "
                f"in {account}:{all_mail} for thread {thread_id}")
            return {"dry_run": True, "messages": len(uids)}
        for uid in uids:
            typ, _ = imap.uid("STORE", uid, item, value)
            if typ != "OK":
                raise RuntimeError(f"STORE {item} {value} failed on uid {uid!r}")
        log(f"mail_actions: {item} {value} on {len(uids)} message(s) in "
            f"{account}:{all_mail} for thread {thread_id}")
        return {"messages": len(uids)}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def star_thread(thread_id: str, account: str, star: bool = True,
                env: dict | None = None, log=print, dry_run: bool = False) -> dict:
    item = "+FLAGS" if star else "-FLAGS"
    return _store_all_mail(thread_id, account, item, r"(\Flagged)", env, log, dry_run)


def label_thread(thread_id: str, account: str, label: str, add: bool = True,
                 env: dict | None = None, log=print, dry_run: bool = False) -> dict:
    label = validate_label(label)
    item = "+X-GM-LABELS" if add else "-X-GM-LABELS"
    return _store_all_mail(thread_id, account, item, f"({_quote(label)})", env, log, dry_run)


def archive_thread(thread_id: str, account: str, env: dict | None = None,
                   log=print, dry_run: bool = False) -> dict:
    """Remove the \\Inbox label — Gmail's actual "Archive" over IMAP.

    MUST be issued from All Mail: Gmail silently ignores
    `STORE -X-GM-LABELS (\\Inbox)` while INBOX is the selected mailbox
    (returns OK, changes nothing — found the hard way, E5-P1 2026-07-15;
    every E3 archive before this fix only *looked* archived on the board).
    Already-archived threads are a harmless no-op."""
    return _store_all_mail(thread_id, account, "-X-GM-LABELS", r"(\Inbox)",
                           env, log, dry_run)


def search_threads(query: str, account: str, env: dict | None = None,
                   limit: int = 30, log=print) -> list:
    """Search one account's All Mail with Gmail's own query syntax (E9.5,
    X-GM-RAW — from:, subject:, older_than:, phrases, everything Gmail's
    search box accepts). Read-only. Returns newest-first thread rows the
    Mail screen can render (and ✎-map to a project)."""
    query = re.sub(r"\s+", " ", str(query or "")).strip()[:200]
    if not query:
        raise ValueError("empty query")
    env = env or load_env()
    try:
        from sources.gmail_reader import _fetch_headers, _dt_from
    except ImportError:  # pragma: no cover - package-relative fallback
        from .sources.gmail_reader import _fetch_headers, _dt_from
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        typ, _sel = imap.select(f'"{all_mail}"', readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open All Mail")
        # X-GM-RAW takes one quoted string; non-ASCII is dropped rather than
        # fighting IMAP literals (Gmail search is forgiving about the rest)
        safe = query.encode("ascii", "ignore").decode()
        # E258: Private threads never come back from a search, whoever asks
        safe = f"({safe}) -label:private"
        typ, data = imap.uid("SEARCH", "X-GM-RAW", _quote(safe))
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        headers = _fetch_headers(imap, uids[-300:])
        threads: dict[str, dict] = {}
        for _uid, meta in headers.items():
            thrid = meta["thrid"]
            dt = _dt_from(meta)
            t = threads.get(thrid)
            if not t or dt > t["dt"]:
                threads[thrid] = {"dt": dt, "meta": meta,
                                  "count": (t["count"] + 1) if t else 1}
            else:
                t["count"] += 1
        import private
        priv = private.keys()
        rows = sorted(((k, v) for k, v in threads.items() if (account, str(k)) not in priv),
                      key=lambda kv: kv[1]["dt"], reverse=True)[:limit]
        out = []
        for thrid, t in rows:
            m = t["meta"]
            out.append({
                "thread_id": thrid,
                "account": account,
                "subject": m["subject"] or "(no subject)",
                "last_from": m["from"],
                "last_date": t["dt"].isoformat(),
                "message_count": t["count"],
            })
        log(f"search:{account} {query!r} -> {len(threads)} thread(s)")
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def trash_thread(thread_id: str, account: str, env: dict | None = None,
                 log=print, dry_run: bool = False) -> dict:
    """Move the whole thread to Gmail's Trash (E7-P3, §6 amended by Bart
    2026-07-17). UID COPY to the \\Trash folder — Gmail's label model makes
    that a MOVE (the messages leave All Mail/INBOX). Recoverable from Trash
    for 30 days; there is deliberately NO purge/empty-trash verb anywhere in
    this system, and trash is never exposed to the command bar or any
    agent tool surface — human click only."""
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        trash = mc.find_folder(imap, "\\Trash", "[Gmail]/Trash")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=dry_run)
        if not uids:
            # 2026-09-29: a thread trashed twice (a single delete racing a
            # bundle's Delete all) used to fail here, and the board then put the
            # row back although the mail was in Trash. Already there = done.
            if mc.search_thread_uids(imap, trash, thread_id, readonly=True):
                log(f"mail_actions: thread {thread_id} in {account} was already in Trash")
                return {"messages": 0, "already": True}
            raise RuntimeError("thread not found in All Mail")
        if dry_run:
            log(f"[dry-run] COPY {len(uids)} message(s) to {account}:{trash} "
                f"for thread {thread_id}")
            return {"dry_run": True, "messages": len(uids)}
        moved = 0
        for uid in uids:
            typ, _ = imap.uid("COPY", uid, f'"{trash}"')
            if typ == "OK":
                moved += 1
                continue
            # the message left All Mail between SEARCH and COPY — another
            # action trashed it; only a message still here is a real failure
            typ, data = imap.uid("SEARCH", None, "UID", uid)
            if typ == "OK" and data and data[0] and uid in data[0].split():
                raise RuntimeError(f"COPY to Trash failed on uid {uid!r}")
        log(f"mail_actions: trashed {moved} message(s) in {account} "
            f"for thread {thread_id} (recoverable from Trash 30d)")
        return {"messages": moved}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def unarchive_thread(thread_id: str, account: str, env: dict | None = None,
                     log=print, dry_run: bool = False) -> dict:
    """Re-add the \\Inbox label — returns a snoozed thread to the INBOX.
    The write half of Shortwave-style snooze (E5-P1): archive_thread() at
    snooze time, this at wake time. Operates on All Mail because the thread
    is by definition not in INBOX anymore."""
    item = "+X-GM-LABELS"
    return _store_all_mail(thread_id, account, item, r"(\Inbox)", env, log, dry_run)


# Mirror labels live under one Gmail prefix so re-filing can swap the old
# one out without touching Bart's own labels (E5-P2).
GC_LABEL_PREFIX = "GC/"

# One X-GM-LABELS list item: a quoted string (backslash escapes) or an atom.
_LABEL_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"|([^\s()"]+)')


def _parse_labels(fetch_line: str) -> set:
    """Label names out of one FETCH response line like
    b'1 (X-GM-LABELS ("\\\\Inbox" "GC/Partner Network" Work) UID 9)'."""
    m = re.search(r"X-GM-LABELS \(((?:[^()\\]|\\.)*)\)", fetch_line)
    if not m:
        return set()
    out = set()
    for quoted, atom in _LABEL_ITEM_RE.findall(m.group(1)):
        label = quoted.replace('\\"', '"').replace("\\\\", "\\") if quoted else atom
        if label:
            out.add(label)
    return out


def thread_labels(thread_id: str, account: str, env: dict | None = None) -> set:
    """Read-only: the union of Gmail labels across the thread's messages
    (system labels like \\Inbox included — callers filter)."""
    thread_id = mc.validate_thread_id(thread_id)
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=True)
        if not uids:
            raise RuntimeError("thread not found in All Mail")
        labels: set = set()
        for uid in uids:
            typ, data = imap.uid("FETCH", uid, "(X-GM-LABELS)")
            if typ != "OK" or not data:
                continue
            for part in data:
                raw = part[0] if isinstance(part, tuple) else part
                if isinstance(raw, bytes):
                    labels |= _parse_labels(raw.decode("utf-8", "replace"))
        return labels
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def set_project_label(thread_id: str, account: str, project_name: str | None,
                      env: dict | None = None, log=print, dry_run: bool = False) -> dict:
    """Mirror a ✎ re-file into Gmail (E5-P2): remove every existing GC/*
    label from the thread, then add GC/<project_name> (None = unfile, remove
    only). One IMAP session for the whole swap. Best-effort by design —
    callers must not let a label failure break the correction itself."""
    thread_id = mc.validate_thread_id(thread_id)
    new = None
    if project_name:
        new = GC_LABEL_PREFIX + validate_label(project_name)[:80]
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
        uids = mc.search_thread_uids(imap, all_mail, thread_id, readonly=dry_run)
        if not uids:
            raise RuntimeError("thread not found in All Mail")
        current: set = set()
        for uid in uids:
            typ, data = imap.uid("FETCH", uid, "(X-GM-LABELS)")
            if typ != "OK" or not data:
                continue
            for part in data:
                raw = part[0] if isinstance(part, tuple) else part
                if isinstance(raw, bytes):
                    current |= _parse_labels(raw.decode("utf-8", "replace"))
        # E204: GC/Todo/* labels are todo membership, not a project — a
        # re-file must never strip them
        old = sorted(l for l in current if l.startswith(GC_LABEL_PREFIX)
                     and not l.startswith(GC_LABEL_PREFIX + "Todo/") and l != new)
        add = new if (new and new not in current) else None
        if dry_run:
            log(f"[dry-run] project label on {account}:{thread_id} — "
                f"would remove {old or 'nothing'}, add {add or 'nothing'}")
            return {"dry_run": True, "removed": old, "added": add}
        for uid in uids:
            for l in old:
                typ, _ = imap.uid("STORE", uid, "-X-GM-LABELS", f"({_quote(l)})")
                if typ != "OK":
                    raise RuntimeError(f"label remove failed on uid {uid!r}")
            if add:
                typ, _ = imap.uid("STORE", uid, "+X-GM-LABELS", f"({_quote(add)})")
                if typ != "OK":
                    raise RuntimeError(f"label add failed on uid {uid!r}")
        log(f"mail_actions: project label on {account}:{thread_id} — "
            f"removed {old or 'nothing'}, added {add or 'nothing'}")
        return {"removed": old, "added": add}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gmail thread actions (archive/star/label/fetch)")
    ap.add_argument("--account", required=True, choices=list(mc.ACCOUNTS))
    ap.add_argument("--thread-id", required=True)
    ap.add_argument("--action", required=True,
                    choices=["fetch", "archive", "unarchive", "trash", "star",
                             "unstar", "label-add", "label-remove", "labels"])
    ap.add_argument("--label", help="required for label-add / label-remove")
    ap.add_argument("--live", action="store_true",
                    help="actually perform the write; default is --dry-run for safety")
    args = ap.parse_args(argv)

    dry_run = not args.live
    if args.action == "fetch":
        for m in fetch_thread(args.thread_id, args.account):
            print(f"\n[{m['date']}] {m['from']} -> {m['to']}\n{m['subject']}\n{m['body'][:300]}")
    elif args.action == "archive":
        print(archive_thread(args.thread_id, args.account, dry_run=dry_run))
    elif args.action == "unarchive":
        print(unarchive_thread(args.thread_id, args.account, dry_run=dry_run))
    elif args.action == "trash":
        print(trash_thread(args.thread_id, args.account, dry_run=dry_run))
    elif args.action == "labels":
        print(sorted(thread_labels(args.thread_id, args.account)))
    elif args.action == "star":
        print(star_thread(args.thread_id, args.account, star=True, dry_run=dry_run))
    elif args.action == "unstar":
        print(star_thread(args.thread_id, args.account, star=False, dry_run=dry_run))
    elif args.action in ("label-add", "label-remove"):
        if not args.label:
            ap.error("--label is required for label-add / label-remove")
        print(label_thread(args.thread_id, args.account, args.label,
                           add=args.action == "label-add", dry_run=dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


def list_drafts(account: str, env: dict | None = None,
                limit: int = 50, log=print) -> list:
    """List what is sitting in one account's Gmail Drafts folder (E23).

    Read-only (BODY.PEEK, readonly SELECT). Drafts are the system's default
    outbound path — draft_reply.save_draft / compose_draft and the Ask
    assistant all APPEND here — but until now nothing in Grand Central could
    SEE them, so a drafted reply effectively vanished until Bart went
    looking in Gmail.

    Returns newest-first rows: {draft_uid, thread_id, account, to, subject,
    snippet, date, link}. `link` opens Gmail's Drafts view for that account;
    Gmail has no stable per-draft permalink, so the thread link is used when
    the draft is a reply and the drafts folder otherwise.
    """
    env = env or load_env()
    try:
        from sources.gmail_reader import _decode, _dt_from, _payload_text
    except ImportError:  # pragma: no cover - package-relative fallback
        from .sources.gmail_reader import _decode, _dt_from, _payload_text

    imap, user = mc.connect(account, env)
    try:
        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, _sel = imap.select(f'"{drafts}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot open {drafts}")
        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        out = []
        for uid in reversed(uids[-limit:]):          # newest first
            typ, d = imap.uid("FETCH", uid,
                              "(X-GM-THRID X-GM-MSGID INTERNALDATE BODY.PEEK[])")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                continue
            head = d[0][0].decode("utf-8", "replace")
            msg = email.message_from_bytes(d[0][1])
            thr_m = re.search(r"X-GM-THRID (\d+)", head)
            dt_m = re.search(r'INTERNALDATE "([^"]+)"', head)
            body = (_payload_text(msg) or "").strip()
            out.append({
                "draft_uid": uid.decode(),
                "thread_id": thr_m.group(1) if thr_m else "",
                "account": account,
                "to": _decode(msg.get("To", "")),
                "subject": _decode(msg.get("Subject", "")) or "(no subject)",
                "snippet": " ".join(body.split())[:200],
                "date": dt_m.group(1) if dt_m else "",
                "link": f"https://mail.google.com/mail/?authuser={user}#drafts",
                # E224: opening a draft in GC needs to name it exactly. Gmail's
                # own message id — drafts APPENDed over IMAP carry no Message-ID.
                "gm_msgid": (re.search(r"X-GM-MSGID (\d+)", head) or [None, ""])[1],
            })
        log(f"drafts:{account} -> {len(out)}")
        return out
    finally:
        try:
            imap.logout()
        except Exception:
            pass


_UID_RE = re.compile(r"^\d{1,12}$")


def _draft_msg(imap, drafts: str, uid: str, gm_msgid: str):
    """Fetch one draft by UID and confirm it is still the draft Bart opened.
    UIDs can be reused after a mailbox rebuild, so Gmail's own message id
    (X-GM-MSGID — unique and fixed; IMAP-saved drafts have no Message-ID)
    must match before anything reads or removes it."""
    if not _UID_RE.match(str(uid)):
        raise ValueError("bad draft uid")
    if not re.match(r"^\d{1,25}$", str(gm_msgid or "")):
        raise ValueError("gm_msgid is required to name a draft")
    typ, d = imap.uid("FETCH", str(uid), "(X-GM-THRID X-GM-MSGID BODY.PEEK[])")
    if typ != "OK" or not d or not isinstance(d[0], tuple):
        raise RuntimeError("draft not found — it may have been sent or deleted in Gmail")
    head = d[0][0].decode("utf-8", "replace")
    got = re.search(r"X-GM-MSGID (\d+)", head)
    if not got or got.group(1) != str(gm_msgid):
        raise RuntimeError("that draft changed in Gmail — refresh Drafts and open it again")
    thr = re.search(r"X-GM-THRID (\d+)", head)
    return email.message_from_bytes(d[0][1]), (thr.group(1) if thr else "")


def get_draft(account: str, uid: str, gm_msgid: str,
              env: dict | None = None) -> dict:
    """One Gmail draft, whole, so GC's composer can open it (E224, Dev #19).
    Read-only. Plain-text body: formatting from Gmail is not carried over."""
    env = env or load_env()
    try:
        from sources.gmail_reader import _decode, _payload_text
    except ImportError:  # pragma: no cover
        from .sources.gmail_reader import _decode, _payload_text
    imap, _user = mc.connect(account, env)
    try:
        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        typ, _ = imap.select(f'"{drafts}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot open {drafts}")
        msg, thread_id = _draft_msg(imap, drafts, uid, gm_msgid)
        return {
            "account": account, "draft_uid": str(uid), "thread_id": thread_id,
            "gm_msgid": str(gm_msgid),
            "to": _decode(msg.get("To", "")), "cc": _decode(msg.get("Cc", "")),
            "bcc": _decode(msg.get("Bcc", "")),
            "subject": _decode(msg.get("Subject", "")),
            "in_reply_to": (msg.get("In-Reply-To") or "").strip(),
            "references": " ".join((msg.get("References") or "").split()),
            "body": (_payload_text(msg) or "").rstrip(),
            # E258: an agent wrote this; the composer warns before Send
            "agent": (msg.get("X-GC-Agent-Draft") or "").strip() == "1",
            "agent_new": [a.strip() for a in (msg.get("X-GC-New-Recipients") or "").split(",")
                          if a.strip()],
        }
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def trash_draft(account: str, uid: str, gm_msgid: str,
                env: dict | None = None, log=print) -> dict:
    """Move ONE superseded draft to Gmail Trash (E224, Bart 2026-09-27: "it's
    ok to delete and resave"). Only ever called after its replacement was
    saved, or after it was sent. Works only inside Drafts, only on the draft
    whose Gmail message id matches, and only by COPY to Trash — recoverable 30 days,
    like every delete here. Human click only: not an Ask or agent tool."""
    env = env or load_env()
    imap, _user = mc.connect(account, env)
    try:
        drafts = mc.find_folder(imap, "\\Drafts", "[Gmail]/Drafts")
        trash = mc.find_folder(imap, "\\Trash", "[Gmail]/Trash")
        typ, _ = imap.select(f'"{drafts}"', readonly=False)
        if typ != "OK":
            raise RuntimeError(f"cannot open {drafts}")
        _draft_msg(imap, drafts, uid, gm_msgid)
        typ, _ = imap.uid("COPY", str(uid), f'"{trash}"')
        if typ != "OK":
            raise RuntimeError("COPY to Trash failed")
        log(f"mail_actions: old draft {uid} in {account} moved to Trash")
        return {"trashed": 1}
    finally:
        try:
            imap.logout()
        except Exception:
            pass


SENT_PAGE = 50
SENT_PREVIEW_BYTES = 8000     # enough of a message for its first lines
_QUOTE_START = re.compile(r"^(On .{4,200}wrote:|-{2,} ?Original Message|From: .+)$", re.I)


def _sent_preview(msg) -> str:
    """What Bart wrote, one line: the body up to the quoted history."""
    text = mc.body_text(msg, cap=4000, keep_newlines=True)
    keep = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith(">") or _QUOTE_START.match(s):
            break
        keep.append(s)
    return " ".join(x for x in keep if x)[:220]


def list_sent(account: str, env: dict | None = None,
              limit: int = SENT_PAGE, before_uid: int | None = None, log=print) -> dict:
    """One page of an account's Sent folder, newest first (E73; Dev #41).

    Read-only (readonly SELECT, BODY.PEEK). Each row: thread_id, uid, to,
    cc, subject, date (ISO UTC, from the Date header — the old rows asked
    for a `date` key the header fetch never had, so nothing was dated and
    the newest-first sort did nothing), a one-line preview of what Bart
    wrote (quoted history cut), and a link. `before_uid` pages further back.
    Returns {"rows": [...], "next_before": uid or None}.
    """
    env = env or load_env()
    try:
        from sources.gmail_reader import _decode, _dt_from, _fetch_headers
    except ImportError:  # pragma: no cover - package-relative fallback
        from .sources.gmail_reader import _decode, _dt_from, _fetch_headers

    limit = max(1, min(int(limit or SENT_PAGE), 100))
    imap, user = mc.connect(account, env)
    try:
        sent = mc.find_folder(imap, "\\Sent", "[Gmail]/Sent Mail")
        typ, _sel = imap.select(f'"{sent}"', readonly=True)
        if typ != "OK":
            raise RuntimeError(f"cannot open {sent}")
        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        if before_uid:
            uids = [u for u in uids if int(u) < int(before_uid)]
        take = uids[-limit:]
        headers = _fetch_headers(imap, take)
        previews = {}
        for i in range(0, len(take), 25):
            chunk = take[i:i + 25]
            typ, d = imap.uid("FETCH", b",".join(chunk), f"(BODY.PEEK[]<0.{SENT_PREVIEW_BYTES}>)")
            if typ != "OK" or not d:
                continue
            for item in d:
                if not isinstance(item, tuple):
                    continue
                m = re.search(rb"UID (\d+)", item[0])
                if not m:
                    continue
                try:
                    previews[m.group(1).decode()] = _sent_preview(email.message_from_bytes(item[1]))
                except Exception:
                    pass
        rows = []
        for uid, m in headers.items():
            dt = _dt_from(m)
            rows.append({
                "thread_id": m.get("thrid", ""),
                "uid": int(uid),
                "account": account,
                "to": " ".join(_decode(m.get("to", "")).split()),     # unfold long headers
                "cc": " ".join(_decode(m.get("cc", "")).split()),
                "subject": m.get("subject", "") or "(no subject)",
                "date": dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt.year > 1970 else "",
                "snippet": previews.get(uid, ""),
                "link": ("https://mail.google.com/mail/?authuser=%s#sent" % user),
            })
        rows.sort(key=lambda r: (r["date"], r["uid"]), reverse=True)
        more = len(uids) > len(take)
        return {"rows": rows, "next_before": (int(take[0]) if more and take else None)}
    finally:
        try:
            imap.logout()
        except Exception:
            pass
