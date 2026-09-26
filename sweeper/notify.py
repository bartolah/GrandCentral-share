"""E99 — iMessage alerts via OpenClaw (Bart: "use OpenClaw's imessage
integration as a communication and alert system? Similar to an app?").

Runs inside every mail_sync cycle (~3 min, 24/7), wrapped so failures
never break the sync. Sends go through OpenClaw's WORKING unsolicited
path (`openclaw message send`, the usage-watchdog's proven deliver():
launchd's bare PATH needs the Homebrew + node@22 bins injected; the CLI
is heavy (~2 min doctor/migrations) so we Popen detached and log to
logs/notify.log instead of blocking the sync).

Alerts (v1, deterministic, deliberately quiet):
  brief     one morning digest (~07:45 local): events, due todos, needs
  event     ~15 minutes before each calendar event
Dedup via the `notifications` table (kind+ref primary key). Quiet hours
22:00–07:30 suppress event pings (the brief window sits just outside).

§6 note: messages go ONLY to Bart's own handle (NOTIFY_IMESSAGE_TARGET,
default you@example.com) — this is the system talking to its owner,
never outbound to third parties. Replies land in OpenClaw (gc-host),
which already carries the Grand Central skill — so texting back
"what's my day?" just works; nothing here reads or processes replies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta

import db
from common import LOGS_DIR

OPENCLAW = "/opt/homebrew/bin/openclaw"
# E165: OpenClaw gained a second agent (main + guest), and `openclaw agent`
# refuses an unowned turn once more than one exists — "Multiple agents are
# configured, but agent turn has no explicit owner." Every send since
# 2026-09-06 17:21 died on that line, unseen, because the spawn is detached
# and nothing ever read its exit code. Name the agent, and check the rc.
OPENCLAW_AGENT = os.environ.get("NOTIFY_OPENCLAW_AGENT", "main")
NOTIFY_STATUS = os.path.join(LOGS_DIR, "notify_status.json")
BOARD_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dashboard", "grand_central.json")
NOTIFY_LOG = os.path.join(LOGS_DIR, "notify.log")
BRIEF_WINDOW = ("07:40", "08:15")
QUIET = ("22:00", "07:30")
EVENT_LEAD_MIN = 16


def _send(target: str, text: str, log=print) -> None:
    """Fire-and-forget: the openclaw CLI can take ~2 min; never block sync."""
    env = dict(os.environ)
    env["PATH"] = "/opt/homebrew/bin:/opt/homebrew/opt/node@22/bin:" + env.get("PATH", "/usr/bin:/bin")
    try:
        # the AppleScript send needs Messages.app alive — nudge it up front
        if subprocess.run(["pgrep", "-x", "Messages"], capture_output=True).returncode != 0:
            subprocess.run(["open", "-a", "Messages"], capture_output=True, timeout=15)
    except Exception:
        pass
    try:
        with open(NOTIFY_LOG, "a") as lf:
            lf.write(f"{datetime.now().isoformat()} SEND {text[:120]!r}\n")
            # E99.2 (Bart: "OpenClaw has another way that IS working"):
            # route through the RUNNING gateway via an agent turn — the
            # same machinery behind gc-host's daily 07:02 texts. The
            # standalone `message send` CLI spawns a fresh node whose TCC
            # context lacks the Messages automation grant (AppleEvent
            # -1712 with no way to show the consent prompt headlessly).
            proc = subprocess.Popen([OPENCLAW, "agent", "--agent", OPENCLAW_AGENT,
                                     "--message",
                                     ("Send exactly this iMessage to " + target +
                                      " and do nothing else — no commentary, no"
                                      " extra text:\n\n" + text)],
                                    stdout=lf, stderr=lf, env=env,
                                    start_new_session=True)
        # A real agent turn runs ~2 min; a rejected command dies at once. Wait
        # just long enough to tell those apart, so a broken CLI contract can
        # never go quiet for ten days again.
        try:
            rc = proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            rc = None            # still running = accepted, delivering
        _status(ok=(rc in (None, 0)), rc=rc, text=text)
        if rc not in (None, 0):
            log(f"notify: iMessage REJECTED by openclaw (rc={rc}) — see {NOTIFY_LOG}")
            with open(NOTIFY_LOG, "a") as lf:
                lf.write(f"{datetime.now().isoformat()} SEND-REJECTED rc={rc}\n")
        else:
            log(f"notify: queued iMessage ({text[:60]!r}…)")
    except Exception as e:
        _status(ok=False, rc=None, text=text, error=repr(e))
        log(f"notify: send failed to spawn ({e!r})")


def _status(ok: bool, rc, text: str, error: str = "") -> None:
    """Last delivery outcome, for anything that wants to check on this."""
    try:
        with open(NOTIFY_STATUS, "w") as f:
            json.dump({"ok": bool(ok), "rc": rc, "at": datetime.now().isoformat(),
                       "preview": text[:80], "error": error}, f)
    except Exception:
        pass


def _deliver(conn, kind: str, target: str, text: str, log=print, url: str = "/") -> None:
    """E188: one alert, whichever channels that kind is set to use.

    Defaults are both; notify_prefs only records deviations. A push failure
    can never stop the iMessage, or the other way round — these are two
    independent paths to the same person.
    """
    try:
        prefs = db.notify_prefs(conn).get(kind, {"imessage": True, "push": True})
    except Exception:
        prefs = {"imessage": True, "push": True}
    if prefs.get("imessage", True):
        try:
            _send(target, text, log=log)
        except Exception as e:
            log(f"notify: iMessage path failed ({e!r})")
    if prefs.get("push", True):
        try:
            import push as _push
            if _push.configured():
                head, _, rest = text.partition("\n")
                out = _push.send(head[:90] or "Grand Central", rest.strip()[:300] or head,
                                 url=url, tag=kind, log=log)
                log(f"notify: push {kind} → sent {out.get('sent',0)}, dropped {out.get('dropped',0)}")
        except Exception as e:
            log(f"notify: push path failed ({e!r})")


def _sent(conn, kind: str, ref: str) -> bool:
    return conn.execute("SELECT 1 FROM notifications WHERE kind=? AND ref=?",
                        (kind, ref)).fetchone() is not None


def _mark(conn, kind: str, ref: str) -> None:
    with conn:
        conn.execute("INSERT OR IGNORE INTO notifications (kind, ref, sent_at) "
                     "VALUES (?,?,?)", (kind, ref, datetime.now().isoformat()))


def _today_events():
    try:
        with open(BOARD_JSON, encoding="utf-8") as f:
            cal = json.load(f).get("calendar") or []
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        day = next((d for d in cal if d.get("date") == today), None)
        return (day or {}).get("events") or []
    except Exception:
        return []


def run(conn, env: dict, log=print) -> None:
    target = env.get("NOTIFY_IMESSAGE_TARGET", "you@example.com")
    now = datetime.now().astimezone()
    hm = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    quiet = hm >= QUIET[0] or hm < QUIET[1]

    # ---- morning brief, once per day ----
    if BRIEF_WINDOW[0] <= hm <= BRIEF_WINDOW[1] and not _sent(conn, "brief", today):
        evs = [e for e in _today_events()]
        todos = [t for t in db.todo_view(conn)
                 if t.get("due") == today and not t.get("done_at")]
        needs = conn.execute(
            "SELECT COUNT(*) AS n FROM mail_threads WHERE in_inbox=1 AND unread=1 "
            "AND category='people'").fetchone()["n"]
        lines = [f"🚂 Morning, Bart — {now.strftime('%A %b %-d')}."]
        try:
            import planner
            p = planner.get(conn)
            if p:
                lines.append("◎ " + (p.get("theme") or "")[:120])
                for i, mst in enumerate((p.get("must") or [])[:3], 1):
                    lines.append(f"{i}. {mst.get('title','')[:70]}")
        except Exception:
            pass
        if evs:
            lines.append("📅 " + " · ".join(f"{e['time']} {e['title'][:28]}" for e in evs[:4])
                         + (f" (+{len(evs)-4})" if len(evs) > 4 else ""))
        else:
            lines.append("📅 a clear track today")
        if todos:
            lines.append("✓ due: " + " · ".join(t["text"][:30] for t in todos[:3])
                         + (f" (+{len(todos)-3})" if len(todos) > 3 else ""))
        lines.append(f"✉ {needs} unread from people. Reply here to ask anything.")
        _deliver(conn, "brief", target, "\n".join(lines), log=log, url="/")
        _mark(conn, "brief", today)

    # ---- event reminders, ~15 min ahead ----
    if not quiet:
        soon = (now + timedelta(minutes=EVENT_LEAD_MIN)).strftime("%H:%M")
        for e in _today_events():
            t = e.get("time") or ""
            if t == "all-day" or not (hm <= t <= soon):
                continue
            ref = f"{today}:{t}:{e.get('title','')[:40]}"
            if _sent(conn, "event", ref):
                continue
            loc = f" — 📍 {e['location'][:40]}" if e.get("location") else ""
            _deliver(conn, "event", target,
                     f"📅 {t}: {e.get('title','')[:60]}{loc}{_prep(conn, e)}", log=log, url="/?go=today")
            _mark(conn, "event", ref)

    # ---- a dev request needs Bart's answer (2026-09-26), once per question ----
    if not quiet:
        for r in conn.execute("SELECT id, account, note, updated_at FROM dev_requests "
                              "WHERE status = 'question'").fetchall():
            ref = f"{r['id']}:{r['updated_at']}"
            if _sent(conn, "devq", ref):
                continue
            _deliver(conn, "dev", target,
                     f"❓ Claude has a question about dev request #{r['id']}\n{(r['note'] or '')[:280]}",
                     log=log, url=f"/?go=dev&account={r['account']}")
            _mark(conn, "devq", ref)

    # ---- evening shutdown (E101 #4), once per day ----
    if "17:20" <= hm <= "18:10" and not _sent(conn, "shutdown", today):
        _deliver(conn, "wrap", target, _shutdown_text(conn), log=log, url="/?go=todos")
        _mark(conn, "shutdown", today)


def _prep(conn, e) -> str:
    """E101 #2: one line of walking-in context for an event reminder —
    open todos + owed replies matched by name tokens from the event's title
    (attendees never leave the server, per E43)."""
    try:
        bits = []
        toks = [w for w in re.findall(r"[A-Za-z]{4,}", e.get("title") or "")
                if w.lower() not in ("with", "sync", "call", "meeting", "weekly",
                                     "daily", "standup", "quick")][:4]
        todos = db.todo_view(conn)
        tmatch = [t for t in todos if not t.get("done_at") and
                  any(w.lower() in (t.get("text") or "").lower() for w in toks)]
        if tmatch:
            bits.append(f"✓{len(tmatch)} open todo(s)")
        rows = conn.execute(
            "SELECT last_from, last_date FROM mail_threads WHERE in_inbox=1 "
            "AND category='people' AND last_direction='inbound' AND unread=1").fetchall()
        owed = [r for r in rows
                if any(w.lower() in (r["last_from"] or "").lower() for w in toks)]
        if owed:
            who = (owed[0]["last_from"] or "").split("<")[0].strip()[:20]
            bits.append(f"✉ you owe {who}" + (f" +{len(owed)-1}" if len(owed) > 1 else ""))
        return ("\n" + " · ".join(bits)) if bits else ""
    except Exception:
        return ""


def _shutdown_text(conn) -> str:
    """E101 #4: the day's closeout."""
    now = datetime.now().astimezone()
    today = now.strftime("%Y-%m-%d")
    todos = db.todo_view(conn)
    done = [t for t in todos if (t.get("done_at") or "").startswith(today)]
    still = [t for t in todos if t.get("due") and t["due"] <= today and not t.get("done_at")]
    mined = conn.execute(
        "SELECT COUNT(*) AS n FROM engagements WHERE source='commitment' "
        "AND created_at LIKE ?", (today + "%",)).fetchone()["n"]
    try:
        with open(BOARD_JSON, encoding="utf-8") as f:
            cal = json.load(f).get("calendar") or []
        tom = (now + timedelta(days=1)).strftime("%Y-%m-%d")
        tomorrow = next((d for d in cal if d.get("date") == tom), {}).get("events") or []
    except Exception:
        tomorrow = []
    lines = [f"🌇 Day's end, Bart."]
    lines.append(f"✓ {len(done)} done" + (": " + " · ".join(t["text"][:26] for t in done[:3]) if done else ""))
    if still:
        lines.append(f"⚠ still due: " + " · ".join(t["text"][:26] for t in still[:3])
                     + (f" (+{len(still)-3})" if len(still) > 3 else ""))
    if mined:
        lines.append(f"🤝 {mined} promise(s) from your sent mail became todos — check they're real.")
    if tomorrow:
        lines.append("🌅 tomorrow: " + " · ".join(f"{e['time']} {e['title'][:22]}" for e in tomorrow[:3]))
    lines.append("Reply with a line for the journal, or 'plan tomorrow' to get a head start.")
    return "\n".join(lines)
