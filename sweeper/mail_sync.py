#!/usr/bin/env python3
"""Grand Central mail sync — E3-P1 (the Mail screen's data feed).

A lightweight IMAP poll that runs every ~3 minutes under launchd
(com.example.grandcentral.mailsync), entirely separate from the hourly
sweep: it fetches INBOX headers + flags for both accounts, groups them into
threads, categorizes each thread deterministically (no model, no token
cost), and upserts data/grand_central.db's mail_threads table. The Mail
screen reads the result via GET /mail on dashboard/serve.py.

Read-only against Gmail (SELECT readonly, SEARCH, FETCH with BODY.PEEK —
same verbs as the sweep readers) EXCEPT the snooze wake pass (E5-P1, §6
amendment approved by Bart 2026-07-15): threads Grand Central archived at
snooze time (mail_triage.wake_inbox=1) get their \\Inbox label re-added
once snoozed_until passes — Shortwave-style snooze. Beyond that it writes
only mail_threads (+ resurrecting done_at in mail_triage when new mail
lands) and its own rolling log.

Categories (deterministic, first match wins, no model). These sort mail by
KIND within its own account (the Mail screen's bundles); they never file mail
under a project or goal:
    calendar     invites/replies (text/calendar, Google calendar sender)
    receipt      billing/order senders or subjects
    newsletter   says so in the sender (newsletters@, digest@)
    promo        bulk mail from a brand/marketing sender, or a bulk subject
                 that is plainly selling something (E205)
    notification app/no-reply/alert senders, then any remaining bulk
    newsletter   bulk with List-Unsubscribe that nothing above claimed —
                 the weakest signal, tested LAST (E205)
    people       everything else — real humans

Snippet economy: message bodies are fetched only for threads that are new
or whose last_date changed since the previous run — a steady-state run
fetches roughly zero bodies.

Run standalone:
    cd sweeper && ../.venv/bin/python mail_sync.py --dry-run

Exit codes: 0 ok, 2 every account failed (existing rows preserved),
3 another run holds the lock.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import DATA_DIR, LOGS_DIR, iso_z, load_env, load_registry  # noqa: E402
import db  # noqa: E402
from build_state import _gmail_link  # noqa: E402
from sources.gmail_reader import (  # noqa: E402
    ACCOUNTS, IMAP_HOST, IMAP_PORT, _addr_list, _body_snippet, _dt_from,
    _fetch_headers, _first_email,
)

import email as email_mod  # noqa: E402
import imaplib  # noqa: E402

LOCK_PATH = os.path.join(DATA_DIR, "mail_sync.lock")
LOCK_STALE_S = 10 * 60          # a run takes seconds; anything older crashed
LOG_PATH = os.path.join(LOGS_DIR, "mail_sync.log")
MAX_LOG_BYTES = 1 << 20         # rolling single file — 480 runs/day
MAX_THREADS = 1500              # safety bound per account (E17: full-inbox scans)
MAX_SNIPPET_FETCHES = 60        # per account per run (first run hits this)

_RECEIPT_LOCAL_RE = re.compile(
    r"^(receipts?|invoice(s)?|billing|orders?|payments?|statements?)([._+-]|$)", re.I)
_RECEIPT_SUBJ_RE = re.compile(
    r"\b(receipt|invoice|order confirm\w*|payment (received|confirmation)|your order)\b", re.I)
_NEWSLETTER_LOCAL_RE = re.compile(r"^(newsletters?|digest)([._+-]|$)", re.I)
# E205: brand/marketing senders and the subject lines they write. Both are
# only consulted for mail that is already structurally bulk (see categorize),
# so a real person writing "last chance!" is never filed as a promo.
_PROMO_LOCAL_RE = re.compile(
    r"^(promo(tions?)?|offers?|deals?|marketing|sales?|shop|store|specials?)"
    r"([._+-]|$)", re.I)
_PROMO_SUBJ_RE = re.compile(
    r"(\d+\s*%\s*off|\b%\s*off\b|\bon sale\b|\bsale\b|\bsave (up to )?\$?\d|"
    r"\bdiscount\b|\bcoupon\b|\bpromo code\b|\bfree shipping\b|\bbogo\b|"
    r"\bbuy one\b|\blimited time\b|\blast chance\b|\bclearance\b|"
    r"\bends (today|tonight|tomorrow|soon)\b|\bdeal of the\b|\bshop now\b|"
    r"\border now\b|\bblack friday\b|\bcyber monday\b)", re.I)
# App / bot / role senders that slip past List-Id detection but aren't a
# person — they belong in Notifications, not People (E34, Bart: "NewsBot
# and Change Network are apps"). Matches the local part before @; the
# curated set below handles ones whose address gives nothing away.
_APP_LOCAL_RE = re.compile(
    # E58: role tokens match at any [._-] segment boundary of the local part
    # ("salesforce_customercare", "no-replies"), not just its start — the
    # anchored version let exactly those through into "people" (and from
    # there into reply debt).
    r"(^|[._-])(no[._-]?repl(y|ies)|do[._-]?not[._-]?reply|donotreply|"
    r"notifications?|notify|alerts?|updates?|mailer(-daemon)?|automated|auto|"
    r"bounces?|postmaster|bot|system|customer[._-]?(care|service)|custcare|"
    r"support|helpdesk|billing|invoic(e|ing)|offers?|deals?|promo(tions?)?|"
    r"marketing|newsletters?|digest|feedback|surveys?|receipts?)([._+-]|$)", re.I)
_CAL_SUBJ_PREFIXES = ("invitation:", "accepted:", "declined:", "tentative:",
                      "updated invitation", "canceled event", "cancelled event")


def categorize(meta: dict, sender_map: dict | None = None) -> str:
    """Deterministic bundle category from one message's headers (the
    thread's latest message decides). First match wins.

    `sender_map` is Bart's curated {address: category} (E34, widened E58) —
    the strongest signal, checked FIRST and in BOTH directions: it names
    apps whose address gives nothing away (app@example.org) AND
    rescues real people the structural rules would bundle (category
    'people' beats every rule below)."""
    subj = (meta.get("subject") or "").lower()
    ct = meta.get("content_type") or ""
    sender = _first_email(meta.get("from", ""))
    local = sender.split("@")[0] if sender else ""
    override = (sender_map or {}).get(sender)
    if override:
        return override
    if ("text/calendar" in ct or sender == "calendar-notification@google.com"
            or subj.startswith(_CAL_SUBJ_PREFIXES)):
        return "calendar"
    if _RECEIPT_LOCAL_RE.match(local) or _RECEIPT_SUBJ_RE.search(subj):
        return "receipt"
    # E205: List-Unsubscribe used to be tested HERE, before everything below,
    # and it swallowed the inbox — 61% of all threads landed in 'newsletter'.
    # Gmail and Yahoo have required that header on bulk mail since 2024, so a
    # store's sale blast, an app's no-reply alert and a fundraising solicitation
    # all carry it and all looked like editorial digests. Bart's own product
    # mail (no-reply@notice.example-app.com, 265 threads) matched _APP_LOCAL_RE
    # perfectly and never reached it. So the header is now the WEAKEST signal,
    # not the strongest: the specific rules get first refusal and it only
    # catches what none of them claimed.
    if _NEWSLETTER_LOCAL_RE.match(local):
        return "newsletter"          # newsletters@, digest@ — says so itself
    # Content sniffing is gated on structural bulk-ness so it can only ever
    # reclassify mail that is already machine-sent.
    is_bulk = bool(meta.get("bulk") or meta.get("list_unsub"))
    if is_bulk and (_PROMO_LOCAL_RE.match(local) or _PROMO_SUBJ_RE.search(subj)):
        return "promo"
    if _APP_LOCAL_RE.search(local):
        return "notification"        # no-reply@, alerts@, updates@, bot@
    if meta.get("list_unsub"):
        return "newsletter"          # bulk, but nothing more specific fit
    if meta.get("bulk"):
        return "notification"
    return "people"


def sync_account(account: str, env: dict, lookback_days: int,
                 known_dates: dict, log=print,
                 sender_map: dict | None = None) -> list:
    """One account's INBOX picture as a list of mail_threads rows.
    known_dates ({thread_id: last_date}) lets us skip unchanged snippets.
    Raises on connect/login failure so main() can count the account failed."""
    user_key, pass_key = ACCOUNTS[account]
    user = env.get(user_key, "")
    password = env.get(pass_key, "")
    if not user or not password:
        raise RuntimeError(f"{user_key}/{pass_key} not set in config/.env")

    since_date = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        imap.login(user, password)
        typ, _ = imap.select("INBOX", readonly=True)
        if typ != "OK":
            raise RuntimeError("could not SELECT INBOX")
        # E17: scan the WHOLE inbox, not a SINCE window — the inbox is small
        # and bounded, and a window made in_inbox reconciliation flip old
        # inbox threads to 0 (they looked archived on the board). Snippet
        # fetches still skip unchanged threads, so steady-state cost is flat.
        typ, data = imap.uid("SEARCH", None, "ALL")
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        headers = _fetch_headers(imap, uids, include_flags=True)

        # group by Gmail thread id, tracking the latest message per thread
        threads: dict[str, dict] = {}
        for uid, meta in headers.items():
            thrid = meta["thrid"]
            dt = _dt_from(meta)
            flags = meta.get("flags", "")
            t = threads.setdefault(thrid, {
                "participants": set(), "count": 0, "unread": False,
                "starred": False, "attach": 0, "labels": set(),
                "latest_dt": datetime(1970, 1, 1, tzinfo=timezone.utc),
                "latest_uid": None, "latest_meta": None,
            })
            t["count"] += 1
            if "\\Seen" not in flags:
                t["unread"] = True
            if "\\Flagged" in flags:
                t["starred"] = True
            if meta.get("attach_count"):
                t["attach"] += 1  # messages-with-attachments, not total parts
            t["labels"].update(meta.get("labels") or [])
            for a in _addr_list(meta["from"]) + _addr_list(meta["to"]) + _addr_list(meta["cc"]):
                t["participants"].add(a)
            if dt >= t["latest_dt"]:
                t["latest_dt"] = dt
                t["latest_uid"] = uid
                t["latest_meta"] = meta

        # E94 (Bart's "Capitol Influence Deck" report): a reply HE sends
        # lives in Sent, not INBOX — so the row kept the inbound date and
        # direction, and an answered thread looked unanswered for good.
        # Merge a cheap header-only scan of recent Sent into known threads:
        # a newer sent message drives last_date + last_direction=outbound.
        try:
            typ, _ = imap.select('"[Gmail]/Sent Mail"', readonly=True)
            if typ == "OK":
                s_since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%d-%b-%Y")
                typ, data = imap.uid("SEARCH", None, f"(SINCE {s_since})")
                s_uids = data[0].split() if typ == "OK" and data and data[0] else []
                s_headers = _fetch_headers(imap, s_uids)
                merged = 0
                for _uid, s_meta in s_headers.items():
                    t = threads.get(s_meta["thrid"])
                    if not t:
                        continue
                    s_dt = _dt_from(s_meta)
                    if s_dt > t.get("outbound_dt", datetime(1970, 1, 1, tzinfo=timezone.utc)):
                        t["outbound_dt"] = s_dt
                        merged += 1
                if merged:
                    log(f"mail_sync:{account} sent-merge: {merged} thread(s) "
                        "credited with an outbound reply")
            imap.select("INBOX", readonly=True)   # back for snippet fetches
        except Exception as e:
            log(f"mail_sync:{account} sent-merge skipped ({e!r})")
            try:
                imap.select("INBOX", readonly=True)
            except Exception:
                pass

        eff = lambda t: max(t["latest_dt"], t.get("outbound_dt") or t["latest_dt"])
        ordered = sorted(threads.items(), key=lambda kv: eff(kv[1]), reverse=True)
        if len(ordered) > MAX_THREADS:
            log(f"mail_sync:{account} capping {len(ordered)} threads at {MAX_THREADS}")
            ordered = ordered[:MAX_THREADS]

        rows, fetched = [], 0
        for thrid, t in ordered:
            meta = t["latest_meta"]
            outb = t.get("outbound_dt")
            replied = bool(outb and outb > t["latest_dt"])
            last_date = iso_z(outb) if replied else iso_z(t["latest_dt"])
            last_from_email = user.lower() if replied else _first_email(meta["from"])
            snippet = None  # None => keep whatever the DB already has
            # replied: skip the snippet refetch — latest_uid is the older
            # INBOX message; keeping the stored snippet beats mislabeling.
            if not replied and known_dates.get(thrid) != last_date and fetched < MAX_SNIPPET_FETCHES:
                typ, data = imap.uid("FETCH", t["latest_uid"], "(BODY.PEEK[])")
                if typ == "OK" and data and isinstance(data[0], tuple):
                    snippet = _body_snippet(email_mod.message_from_bytes(data[0][1]))
                    fetched += 1
            rows.append({
                "thread_id": thrid,
                "subject": meta["subject"] or "(no subject)",
                "participants": sorted(t["participants"]),
                "last_from": user if replied else meta["from"],
                "last_from_email": last_from_email,
                "last_direction": "outbound" if last_from_email == user.lower() else "inbound",
                "snippet": snippet,
                "last_date": last_date,
                "message_count": t["count"],
                "unread": t["unread"],
                "starred": t["starred"],
                "bulk": bool(meta.get("bulk")),
                "category": categorize(meta, sender_map),
                "labels": sorted(t["labels"]),
                "attachments": t["attach"],
                "link": _gmail_link({"thread_id": thrid, "account_email": user.lower(),
                                     "source": "gmail"}),
            })
        log(f"mail_sync:{account} {len(rows)} threads, {fetched} snippet fetch(es)")
        return rows
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def wake_snoozes(conn, env: dict, log=print) -> int:
    """Return due snoozed threads to the Gmail INBOX (E5-P1). Runs after
    the account sync so a wake never fights the same run's in_inbox sweep.
    Failures are logged and retried next run — except a thread that has
    vanished from All Mail entirely, whose wake promise is dropped so it
    cannot retry forever."""
    from mail_actions import unarchive_thread
    woken = 0
    for row in db.due_snoozes(conn):
        thread_id, account = row["thread_id"], row["account"]
        try:
            unarchive_thread(thread_id, account, env=env, log=log)
        except Exception as e:
            log(f"wake:{account}:{thread_id} FAILED: {e!r}"
                + (" — dropping wake" if "not found in All Mail" in str(e) else " — will retry"))
            if "not found in All Mail" not in str(e):
                continue
        with conn:
            # keep snoozed_until: it powers the Mail screen's "⏰ back" chip
            conn.execute(
                "UPDATE mail_triage SET wake_inbox = 0, updated_at = ? "
                "WHERE account = ? AND thread_id = ?",
                (iso_z(datetime.now(timezone.utc)), account, thread_id))
            conn.execute(
                "UPDATE mail_threads SET in_inbox = 1 "
                "WHERE account = ? AND thread_id = ?", (account, thread_id))
        woken += 1
    if woken:
        log(f"wake: {woken} snoozed thread(s) returned to INBOX")
    return woken


def send_due_outbox(conn, env: dict, log=print) -> int:
    """Fire due scheduled sends (E9, approved by Bart 2026-07-17). Each row
    was human-confirmed in the send dialog at schedule time; it goes through
    send_mail.send_reply — the same caps and mail_sends audit as an
    immediate send. A row is marked 'sending' BEFORE the SMTP attempt: if
    the process dies mid-send the row stays 'sending' and is never retried
    (surfaced in the Mail screen instead) — no double-send is possible."""
    rows = db.due_outbox(conn)
    if not rows:
        return 0
    from send_mail import send_reply
    sent = 0
    for row in rows:
        db.set_outbox_status(conn, row["id"], "sending")
        try:
            send_reply(row["thread_id"], row["account"], row["body"],
                       to=row["to_addrs"], cc=row["cc_addrs"] or None,
                       subject=row["subject"] or None,
                       bcc=row["bcc_addrs"] or None, env=env, log=log)
            db.set_outbox_status(conn, row["id"], "sent")
            sent += 1
            log(f"outbox: sent #{row['id']} to {row['to_addrs']} "
                f"(scheduled {row['send_at']})")
        except Exception as e:
            db.set_outbox_status(conn, row["id"], "failed", error=str(e)[:300])
            log(f"outbox: #{row['id']} FAILED: {e!r} (will NOT retry — "
                "cancel or reschedule from the board)")
    return sent


def _acquire_lock() -> bool:
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    if os.path.exists(LOCK_PATH):
        try:
            if time.time() - os.path.getmtime(LOCK_PATH) < LOCK_STALE_S:
                return False
        except OSError:
            pass
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


STATUS_PATH = os.path.join(DATA_DIR, "mail_sync_status.json")


def _write_status(ok: int, total: int, started: float) -> None:
    """E204: when the inbox was last made current, for the board's freshness
    line. Written atomically; best-effort."""
    import json
    try:
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "ok_accounts": ok, "accounts": total,
                       "elapsed_s": round(time.monotonic() - started, 1)}, f)
        os.replace(tmp, STATUS_PATH)
    except OSError:
        pass


def _append_log(line: str) -> None:
    """One summary line per run into a single rolling file (480 runs/day —
    per-run files like the sweep's would be noise)."""
    try:
        os.makedirs(LOGS_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-200:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Grand Central mail sync (read-only vs Gmail)")
    ap.add_argument("--account", choices=list(ACCOUNTS), help="sync just one account")
    ap.add_argument("--days", type=int, default=None, help="lookback window override")
    ap.add_argument("--quick", action="store_true",
                    help="E204 refresh-now: sync the inbox + todo labels only, "
                         "skip the planner/commitments/notify passes")
    ap.add_argument("--dry-run", action="store_true",
                    help="read and summarize; writes nothing (no DB, no lock, no log)")
    args = ap.parse_args(argv)

    started = time.monotonic()
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    lines: list[str] = []

    def log(msg):
        print(f"[{stamp}] {msg}", flush=True)
        lines.append(str(msg))

    if not args.dry_run and not _acquire_lock():
        log("another mail_sync run holds data/mail_sync.lock — exiting")
        return 3

    try:
        env = load_env()
        reg = load_registry()
        lookback = args.days or reg.get("defaults", {}).get("sweep_lookback_days", 7)
        accounts = [args.account] if args.account else list(ACCOUNTS)

        conn = None if args.dry_run else db.connect()
        try:
            # E34: apps/bots Bart has marked as "not a person" — forced out
            # of the People bundle. Loaded once for the whole run.
            sender_map = db.sender_categories(conn) if conn else {}
            ok_accounts = 0
            for account in accounts:
                try:
                    known = db.mail_thread_dates(conn, account) if conn else {}
                    rows = sync_account(account, env, lookback, known, log=log,
                                        sender_map=sender_map)
                    if args.dry_run:
                        cats: dict[str, int] = {}
                        unread = attach = 0
                        for r in rows:
                            cats[r["category"]] = cats.get(r["category"], 0) + 1
                            unread += 1 if r["unread"] else 0
                            attach += 1 if r["attachments"] else 0
                        log(f"mail_sync:{account} DRY RUN — {unread} unread; "
                            f"{attach} thread(s) w/ attachments; "
                            + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
                    else:
                        db.upsert_mail_threads(conn, account, rows)
                    ok_accounts += 1
                except Exception as e:
                    log(f"mail_sync:{account} FAILED: {e!r} (existing rows preserved)")
            if conn is not None:
                # E204: the moment the inbox is current, say so — the board's
                # "Updated 40s ago" reads this, not the slower passes below
                _write_status(ok_accounts, len(accounts), started)
                try:
                    # E204: todos <-> Gmail GC/Todo/* labels, both directions
                    import todo_labels
                    st = todo_labels.reconcile(conn, env, accounts, log=log)
                    if any(st.values()):
                        log("todo labels: " + ", ".join(f"{k}={v}" for k, v in st.items() if v))
                except Exception as e:
                    log(f"todo-label pass FAILED: {e!r}")
            if conn is not None and not args.quick:
                try:
                    wake_snoozes(conn, env, log=log)
                except Exception as e:
                    log(f"wake pass FAILED: {e!r} (will retry next run)")
                try:
                    send_due_outbox(conn, env, log=log)
                except Exception as e:
                    log(f"outbox pass FAILED: {e!r}")
                try:
                    # E81: warm the last-week thread-body cache (bounded)
                    import mail_cache
                    mail_cache.warm(env, log=log)
                except Exception as e:
                    log(f"body-cache pass FAILED: {e!r}")
                try:
                    # E100: morning planner — must run BEFORE notify so the
                    # 07:45 brief can carry the plan
                    import planner
                    planner.run(conn, env, log=log)
                except Exception as e:
                    log(f"planner pass FAILED: {e!r}")
                try:
                    # E101: mine new sent mail for commitments → 🤝 todos
                    import commitments
                    commitments.run(conn, env, log=log)
                except Exception as e:
                    log(f"commitments pass FAILED: {e!r}")
                try:
                    # E99: iMessage alerts (morning brief, event reminders)
                    import notify
                    notify.run(conn, env, log=log)
                except Exception as e:
                    log(f"notify pass FAILED: {e!r}")
            elapsed = time.monotonic() - started
            summary = (f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')} "
                       f"accounts={ok_accounts}/{len(accounts)} in {elapsed:.1f}s")
            if not args.dry_run:
                _append_log(summary + ("" if ok_accounts else " ALL FAILED"))
            log(f"mail_sync finished in {elapsed:.1f}s")
            return 0 if ok_accounts else 2
        finally:
            if conn:
                conn.close()
    finally:
        if not args.dry_run:
            _release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
