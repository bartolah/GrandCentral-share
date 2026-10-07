"""E101 — commitment mining (#3 of Bart's organize-my-day list).

Your own sent mail is where dropped balls hide: "I'll send the deck by
Friday" leaves the inbox the moment you hit send. Each mail_sync cycle
this scans NEW sent messages (per-account, tracked in commit_scan so each
uid is read once), and when there are any, ONE Haiku call extracts clear,
first-person commitments → each becomes a todo prefixed 🤝 with the due
date when one was stated. Marked source='commitment' and easy to drop;
no messages → no model call, so steady-state cost is zero.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import db
import mail_common as mc
from common import require_env
from sources.gmail_reader import ACCOUNTS, IMAP_HOST, IMAP_PORT

MODEL = "claude-haiku-4-5-20251001"
MAX_PER_RUN = 8

PROMPT = """From these emails BART sent, extract only CLEAR first-person
commitments Bart made to the recipient ("I'll send X", "I'll get back to
you by Friday", "we'll have it over Monday"). Ignore pleasantries, vague
intentions, questions, and things the OTHER person owes. today={today}.
Return ONLY JSON: {{"commitments":[{{"i":<msg index>,"promise":"<what, ≤80
chars>","due":"YYYY-MM-DD or null"}}]}} — empty list if none."""


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
PROMPT = PROMPT + "\n\n" + untrusted.RULE


def run(conn, env: dict, log=print) -> None:
    import imaplib
    import private
    priv = private.keys(conn)
    new_msgs = []
    for account, (ukey, pkey) in ACCOUNTS.items():
        user, pw = env.get(ukey, ""), env.get(pkey, "")
        if not user or not pw:
            continue
        try:
            imap = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            imap.login(user, pw)
            sent = mc.find_folder(imap, "\\Sent", "[Gmail]/Sent Mail")
            imap.select(f'"{sent}"', readonly=True)
            since = (datetime.now() - timedelta(days=1)).strftime("%d-%b-%Y")
            typ, data = imap.uid("SEARCH", None, f"(SINCE {since})")
            uids = data[0].split() if typ == "OK" and data and data[0] else []
            for uid in uids[-MAX_PER_RUN:]:
                u = uid.decode() if isinstance(uid, bytes) else str(uid)
                if conn.execute("SELECT 1 FROM commit_scan WHERE account=? AND uid=?",
                                (account, u)).fetchone():
                    continue
                typ, fetched = imap.uid("FETCH", uid, "(X-GM-THRID BODY.PEEK[])")
                if typ == "OK" and fetched and isinstance(fetched[0], tuple):
                    m = mc.parse_msg(fetched[0][1], cap=1500, keep_newlines=True)
                    meta = fetched[0][0]
                    meta = meta.decode("ascii", "replace") if isinstance(meta, bytes) else str(meta)
                    thrid = re.search(r"X-GM-THRID (\d+)", meta)
                    if thrid and (account, thrid.group(1)) in priv:
                        continue  # E258: Private threads never reach a model
                    new_msgs.append({"account": account, "uid": u,
                                     "thread_id": thrid.group(1) if thrid else None,
                                     "to": m["to"], "subject": m["subject"],
                                     "body": m["body"]})
            imap.logout()
        except Exception as e:
            log(f"commitments: {account} scan failed ({e!r})")
    if not new_msgs:
        return
    # mark scanned regardless of outcome — one look per message, ever
    with conn:
        conn.executemany("INSERT OR IGNORE INTO commit_scan (account, uid) VALUES (?,?)",
                         [(m["account"], m["uid"]) for m in new_msgs])
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        payload = [{"i": i, "to": m["to"], "subject": m["subject"],
                    "body": m["body"][:1200]} for i, m in enumerate(new_msgs)]
        resp = client.messages.create(
            model=MODEL, max_tokens=800,
            system=PROMPT.format(today=today),
            messages=[{"role": "user", "content": untrusted.fence_json(payload, "sent_mail")}])
        raw = "".join(p.text for p in resp.content if p.type == "text")
        mjs = re.search(r"\{.*\}", raw, re.S)
        found = (json.loads(mjs.group(0)) if mjs else {}).get("commitments") or []
        made = 0
        for c in found:
            try:
                src_msg = new_msgs[int(c["i"])]
            except Exception:
                continue
            promise = str(c.get("promise") or "").strip()[:200]
            if not promise:
                continue
            due = c.get("due")
            if due and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(due)):
                due = None
            to_short = (src_msg["to"] or "").split(",")[0].strip()[:60]
            thrid = src_msg.get("thread_id")
            # E151 (Bart: "auto todos are too aggressive"): mined promises
            # become WIZARD SUGGESTIONS now, never todos directly — ✓ in the
            # 🧙 view is what creates the todo (and couples the email).
            sid = db.add_wizard_suggestion(
                conn, "commitment", f"🤝 {promise}",
                why=f"You promised this to {to_short} in “{src_msg['subject'][:80]}” — mined from your sent mail.",
                due=due, account=src_msg["account"], thread_id=thrid)
            if sid:
                made += 1
        if made:
            log(f"commitments: {made} promise(s) → wizard suggestions "
                f"(from {len(new_msgs)} sent message(s))")
    except Exception as e:
        log(f"commitments: extraction failed ({e!r})")
