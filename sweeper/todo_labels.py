"""E204 — todos live in Gmail's label system (Bart, 2026-09-23).

Shortwave's one flaw, in Bart's words: "the to-dos don't live within the Gmail
labeling system, and I think they should." Here every todo that groups email
owns ONE Gmail label, ``GC/Todo/<name>``, applied to its member threads in
each thread's own account. The link runs both ways:

    Grand Central -> Gmail   link/unlink/rename write through at click time
                             (``apply`` / ``rename``), best-effort — a Gmail
                             hiccup never loses the membership, it just stays
                             ``label_synced = 0`` and ``reconcile`` retries.
    Gmail -> Grand Central   ``reconcile`` (every mail_sync pass) reads each
                             account's GC/Todo/* labels: a labelled thread
                             joins its todo; a label with no todo becomes
                             one; a CONFIRMED member whose label was removed
                             in Gmail leaves the todo.

Only membership that has been confirmed in Gmail (``label_synced = 1``) can be
unlinked by its absence there. That is the guard against the obvious race: a
thread linked here whose label write failed must be retried, not dropped.

Scope (§6): label writes on single explicit threads under the GC/ prefix, the
same class as the E5-P2 project mirror. Linking from Gmail does NOT archive —
Bart may label a thread and keep it in his inbox on purpose.
"""

from __future__ import annotations

import base64
import re
import unicodedata

try:
    from . import db
    from . import mail_actions
    from . import mail_common as mc
except ImportError:  # pragma: no cover - direct-run fallback
    import db
    import mail_actions
    import mail_common as mc

PREFIX = "GC/Todo/"
_STRIP_RE = re.compile(r'[\\"(){}%*\r\n]')
BACKFILL_PER_RUN = 25       # unconfirmed memberships written per pass
MAX_THREADS_PER_LABEL = 500


def clean_name(text: str) -> str:
    """A todo's text as one Gmail label segment: no '/' (it would nest), no
    IMAP-hostile characters, one line, bounded."""
    s = str(text or "").replace("/", "-")
    # ASCII only: IMAP carries non-ASCII label names as modified UTF-7, and
    # an emoji-led title (🤝 commitment todos) reads fine without its emoji
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = _STRIP_RE.sub("", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:60].rstrip() or "Untitled"


def decode_mutf7(name: str) -> str:
    """IMAP modified UTF-7 (RFC 3501 §5.1.3) -> text, for label names Bart
    typed in Gmail with non-ASCII characters. The raw form stays the key."""
    def dec(m):
        b = m.group(1)
        if not b:
            return "&"
        b = b.replace(",", "/")
        b += "=" * (-len(b) % 4)
        try:
            return base64.b64decode(b).decode("utf-16-be")
        except Exception:
            return m.group(0)
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, name)


def _label_owner(conn, label: str):
    return conn.execute(
        "SELECT id FROM engagements WHERE lower(gmail_label) = lower(?)",
        (label,)).fetchone()


def label_for(conn, eng_id: int) -> str:
    """The todo's label, assigned on first need and stable afterwards (a
    rename moves it deliberately via ``rename``). Two todos with the same
    text get distinct labels — the second carries its id."""
    r = conn.execute("SELECT text, gmail_label FROM engagements WHERE id = ?",
                     (int(eng_id),)).fetchone()
    if not r:
        raise ValueError(f"no todo {eng_id}")
    if r["gmail_label"]:
        return r["gmail_label"]
    label = PREFIX + clean_name(r["text"])
    owner = _label_owner(conn, label)
    if owner and owner["id"] != int(eng_id):
        label = PREFIX + clean_name(r["text"])[:52] + f" #{int(eng_id)}"
    with conn:
        conn.execute("UPDATE engagements SET gmail_label = ? WHERE id = ?",
                     (label, int(eng_id)))
    return label


def _mark(conn, eng_id, account, thread_id, synced: int) -> None:
    with conn:
        conn.execute(
            "UPDATE engagement_threads SET label_synced = ? "
            "WHERE engagement_id = ? AND account = ? AND thread_id = ?",
            (synced, int(eng_id), account, str(thread_id)))


def apply(conn, eng_id: int, account: str, thread_id: str, add: bool = True,
          env: dict | None = None, log=print) -> str | None:
    """Write one membership change into Gmail. Returns the label, or None
    when Gmail refused (the reconcile pass retries adds)."""
    label = label_for(conn, eng_id)
    try:
        mail_actions.label_thread(thread_id, account, label, add=add, env=env,
                                  log=log)
    except Exception as e:
        log(f"todo_labels: {'add' if add else 'remove'} {label!r} on "
            f"{account}:{thread_id} failed: {e!r}")
        if add and "not found" in str(e):
            _mark(conn, eng_id, account, thread_id, -1)   # gone — stop retrying
        return None
    if add:
        _mark(conn, eng_id, account, thread_id, 1)
    return label


def rename(conn, eng_id: int, env: dict | None = None, log=print) -> str | None:
    """The todo's title changed: move its Gmail label with it (IMAP RENAME
    in every account that has the old one — Gmail keeps the threads)."""
    r = conn.execute("SELECT text, gmail_label FROM engagements WHERE id = ?",
                     (int(eng_id),)).fetchone()
    if not r or not r["gmail_label"]:
        return None                    # never labelled — nothing to move
    new = PREFIX + clean_name(r["text"])
    if new.lower() == r["gmail_label"].lower():
        return r["gmail_label"]
    owner = _label_owner(conn, new)
    if owner and owner["id"] != int(eng_id):
        new = PREFIX + clean_name(r["text"])[:52] + f" #{int(eng_id)}"
    old = r["gmail_label"]
    accounts = [a["account"] for a in conn.execute(
        "SELECT DISTINCT account FROM engagement_threads WHERE engagement_id = ?",
        (int(eng_id),))]
    env = env or mail_actions.load_env()
    for account in accounts:
        try:
            imap, _u = mc.connect(account, env)
            try:
                typ, _ = imap.rename(mail_actions._quote(old), mail_actions._quote(new))
                if typ != "OK":
                    log(f"todo_labels: rename {old!r} -> {new!r} refused in {account}")
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except Exception as e:
            log(f"todo_labels: rename in {account} failed: {e!r}")
    with conn:
        conn.execute("UPDATE engagements SET gmail_label = ? WHERE id = ?",
                     (new, int(eng_id)))
    return new


# ---------------------------------------------------------------- reconcile
_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')


def _unquote(name: str) -> str:
    name = name.strip()
    if name.startswith('"') and name.endswith('"'):
        name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return name


def _todo_labels(imap) -> list:
    """Every GC/Todo/* label in this account (Gmail exposes labels as
    folders; \\Noselect parents are skipped)."""
    typ, data = imap.list('""', '"GC/Todo/*"')
    out = []
    if typ != "OK":
        raise RuntimeError("LIST GC/Todo/* failed")
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        m = _LIST_RE.match(line)
        if not m or "\\Noselect" in m.group("flags"):
            continue
        name = _unquote(m.group("name"))
        if name.startswith(PREFIX) and name[len(PREFIX):].strip():
            out.append(name)
    return out


def _label_threads(imap, label: str) -> set:
    """Thread ids carrying the label (read-only EXAMINE of its folder)."""
    typ, _ = imap.select(mail_actions._quote(label), readonly=True)
    if typ != "OK":
        raise RuntimeError(f"cannot open {label!r}")
    typ, data = imap.uid("SEARCH", None, "ALL")
    uids = data[0].split() if typ == "OK" and data and data[0] else []
    if len(uids) > MAX_THREADS_PER_LABEL:
        # too big to read whole — treat as unreadable rather than partial,
        # so members past the cap are never mistaken for "removed"
        raise RuntimeError(f"{len(uids)} messages — over the read cap")
    thr = set()
    for at in range(0, len(uids), 200):
        chunk = b",".join(uids[at:at + 200]).decode()
        typ, rows = imap.uid("FETCH", chunk, "(X-GM-THRID)")
        for part in rows or []:
            raw = part[0] if isinstance(part, tuple) else part
            if isinstance(raw, bytes):
                m = re.search(rb"X-GM-THRID (\d+)", raw)
                if m:
                    thr.add(m.group(1).decode())
    return thr


def reconcile(conn, env: dict, accounts: list | None = None, log=print) -> dict:
    """Bring GC todo membership and Gmail GC/Todo/* labels into agreement.
    Returns counts for the mail_sync log line."""
    stats = {"linked": 0, "unlinked": 0, "created": 0, "written": 0,
             "renamed": 0, "failed": 0}
    accounts = accounts or list(mc.ACCOUNTS)
    for account in accounts:
        try:
            imap, _u = mc.connect(account, env)
        except Exception as e:
            log(f"todo_labels:{account} connect failed: {e!r}")
            stats["failed"] += 1
            continue
        try:
            _reconcile_account(conn, imap, account, stats, log)
        except Exception as e:
            stats["failed"] += 1
            log(f"todo_labels:{account} pass failed: {e!r} (nothing unlinked)")
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    return stats


def _reconcile_account(conn, imap, account: str, stats: dict, log) -> None:
    # Membership is read BEFORE Gmail, so a link made here mid-pass is simply
    # unknown to this pass (never "confirmed but missing"), and an unlink made
    # here mid-pass cannot be undone by a label list read before it landed.
    # Fresh links (<10 min) are also never unlinked by absence.
    members: dict[int, dict] = {}
    for r in conn.execute(
            "SELECT engagement_id, thread_id, label_synced, "
            "  (added_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-10 minutes')) AS fresh "
            "FROM engagement_threads WHERE account = ?", (account,)):
        members.setdefault(r["engagement_id"], {})[r["thread_id"]] = (
            2 if r["fresh"] else r["label_synced"])
    gmail: dict[str, set] = {}
    unreadable: set = set()       # labels we could not read — never
    import private  # E258: a Private thread never links back in via its label
    priv = {t for a, t in private.keys(conn) if a == account}
    for label in _todo_labels(imap):   # mistake those for "removed"
        try:
            gmail[label] = {t for t in _label_threads(imap, label) if str(t) not in priv}
        except Exception as e:
            unreadable.add(label.lower())
            log(f"todo_labels:{account} {label!r}: {e!r}")
    # Account boundary: a label is only ever matched to a todo of THIS
    # account (or a legacy unassigned one holding no other account's mail).
    # Matching by label text alone let a same-named label in another account
    # pull that account's threads into this account's todo.
    by_label = {r["gmail_label"].lower(): r["id"] for r in conn.execute(
        "SELECT g.id, g.gmail_label FROM engagements g "
        "WHERE g.gmail_label IS NOT NULL AND g.state != 'dropped' "
        "AND (g.account = ? OR (g.account IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM engagement_threads x WHERE x.engagement_id = g.id "
        "  AND x.account != ?)))", (account, account))}

    # a label renamed IN Gmail shows up as one unknown label plus one
    # vanished label holding the same threads — follow the rename
    # rather than orphaning the old todo and spawning a new one
    known_here = {eid for eid, m in members.items() if 1 in m.values()}
    present = {l.lower() for l in gmail} | unreadable
    vanished = {eid for eid in known_here
                if (lbl := next((k for k, v in by_label.items() if v == eid), None))
                and lbl not in present}
    for label, thr in gmail.items():
        if label.lower() in by_label or not thr:
            continue
        for eid in list(vanished):
            confirmed = {t for t, s in members.get(eid, {}).items() if s == 1}
            if confirmed and len(confirmed & thr) * 2 >= len(confirmed):
                new_text = decode_mutf7(label[len(PREFIX):]).strip()
                with conn:
                    conn.execute("UPDATE engagements SET gmail_label = ?, text = ? "
                                 "WHERE id = ?", (label, new_text[:300], eid))
                by_label = {k: v for k, v in by_label.items() if v != eid}
                by_label[label.lower()] = eid
                vanished.discard(eid)
                stats["renamed"] += 1
                log(f"todo_labels:{account} followed Gmail rename -> {label!r}")
                break

    for label, thr in gmail.items():
        eid = by_label.get(label.lower())
        if eid is None:
            if not thr:
                continue          # an empty stray label is not a todo
            eid = db.add_engagement(conn, decode_mutf7(label[len(PREFIX):]).strip(),
                                    source="gmail", account=account)
            with conn:
                conn.execute("UPDATE engagements SET gmail_label = ? WHERE id = ?",
                             (label, eid))
            by_label[label.lower()] = eid
            stats["created"] += 1
            log(f"todo_labels:{account} new todo from Gmail label {label!r}")
        have = members.setdefault(eid, {})
        for tid in thr:
            if tid not in have:
                try:
                    db.link_thread(conn, eid, account, tid, add=True)
                except ValueError as e:
                    log(f"todo_labels:{account} refused cross-account link: {e}")
                    continue
                stats["linked"] += 1
            if have.get(tid) != 1:
                _mark(conn, eid, account, tid, 1)
            have[tid] = 1
        # confirmed members whose label Bart removed in Gmail
        for tid, synced in list(have.items()):
            if synced == 1 and tid not in thr:
                db.link_thread(conn, eid, account, tid, add=False)
                stats["unlinked"] += 1
                del have[tid]

    # a label deleted outright in Gmail: its confirmed members leave
    for eid in vanished:
        for tid, synced in list(members.get(eid, {}).items()):
            if synced == 1:
                db.link_thread(conn, eid, account, tid, add=False)
                stats["unlinked"] += 1

    # GC -> Gmail: memberships never confirmed there get written — OPEN
    # todos only; a finished todo keeps a label it already had, but closed
    # history is never pushed into Gmail's label list after the fact
    pending = conn.execute(
        "SELECT et.engagement_id, et.thread_id FROM engagement_threads et "
        "JOIN engagements g ON g.id = et.engagement_id "
        "WHERE et.account = ? AND et.label_synced = 0 AND g.state = 'open' "
        "ORDER BY et.added_at DESC LIMIT ?", (account, BACKFILL_PER_RUN)).fetchall()
    all_mail = mc.find_folder(imap, "\\All", "[Gmail]/All Mail") if pending else None
    for r in pending:
        if not mc._THRID_RE.match(str(r["thread_id"])):
            continue
        label = label_for(conn, r["engagement_id"])
        try:
            uids = mc.search_thread_uids(imap, all_mail, r["thread_id"], readonly=False)
            if not uids:
                _mark(conn, r["engagement_id"], account, r["thread_id"], -1)
                continue          # gone from Gmail — stop retrying
            for uid in uids:
                typ, _ = imap.uid("STORE", uid, "+X-GM-LABELS",
                                  f"({mail_actions._quote(label)})")
                if typ != "OK":
                    raise RuntimeError(f"STORE refused on uid {uid!r}")
            _mark(conn, r["engagement_id"], account, r["thread_id"], 1)
            stats["written"] += 1
        except Exception as e:
            stats["failed"] += 1
            log(f"todo_labels:{account} write {label!r} on {r['thread_id']}: {e!r}")


if __name__ == "__main__":
    import sys
    from common import load_env
    c = db.connect()
    try:
        print(reconcile(c, load_env(), sys.argv[1:] or None))
    finally:
        c.close()
