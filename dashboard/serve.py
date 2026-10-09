#!/usr/bin/env python3
"""
Serve the Grand Central dashboard on the home network.

Listens on 8080 but answers only loopback and tailnet peers (E161): the host
itself, the MCP server, cloudflared (fronted by Cloudflare Access), and
Tailscale devices. Plain-LAN clients are refused — the board has no password
of its own, so it relies on Access and Tailscale, both of which authenticate.
GC_ALLOW_LAN=1 reopens the LAN if Tailscale is down. Mutating POSTs also
reject a foreign Origin, which closes a live CSRF hole. See BUILD_SPEC §6.
Serves this dashboard/ directory, including grand_central.json.

Endpoints (all validated in sweeper/{feedback,draft_reply,mail_actions,
command}.py — this file only parses/bounds the HTTP request and dispatches):

    GET  /mail              Mail screen thread list from the DB (E3-P1;
                            written by sweeper/mail_sync.py every ~3 min)
    GET  /thread            {thread_id, account} -> full thread (read-only)
    POST /dismiss           clear a thread from Reply Debt/Unclassified
                            (local-only; never touches Gmail)
    POST /draft             AI-draft a reply and save it straight to Drafts
                            (the Reply Debt panel's one-click "Draft" button)
    POST /draft/preview     AI-draft a reply WITHOUT saving it (edit-before-send)
    POST /draft/save        save reply text (AI-drafted, edited, or typed from
                            scratch) to that account's Drafts
    POST /draft/compose     save a NEW email (to/subject/body, no thread) to
                            that account's Drafts (E21; never sends)
    POST /calendar          create an event on the account's own primary
                            Google calendar (E21, approved 2026-07-19;
                            create-only, no attendees/invites; gog CLI OAuth;
                            audited in calendar_writes)
    GET  /calendar          read any date range live from the ICS feeds
                            (E76: calendar dock day-nav + month view; the
                            baked board only carries 7 days from the last
                            sweep). Cached 10 min per range.
    GET  /daynotes          day notes over a range (E76: the journal shown
                            on every calendar view)
    POST /mail-action       {action: archive|star|unstar|label-add|label-remove}
                            on one thread — writes to Gmail
    POST /mail/triage       local pin / snooze / done state for the Mail
                            screen (E3-P3; never touches Gmail)
    POST /mail/bulk         archive up to 50 explicit thread_ids (bundle
                            archive; per-id validation; not a command tool)
    POST /task              add / done / drop / reopen a todo (E2-P3; the DB
                            is the task system of record). A todo belongs to
                            exactly one account and only links that account's
                            email. GET /todos?account= and GET /mail?account=
                            scope the lists to one account.
    POST /decision          add / decide a Decision Queue entry (E2-P3)
    POST /command           natural-language command bar; runs Claude with
                            tool-calling restricted to the actions above
    GET  /reply-meta        read-only: derived reply recipients/subject
                            (powers the Send confirm dialog)
    POST /send              the system's ONE send path (E3-P4, approved by
                            Casey 2026-07-10): a single human-confirmed
                            reply. Requires confirm:true + the explicit
                            recipient list the human saw; single thread,
                            ≤10 recipients; logged to mail_sends. NEVER a
                            command-bar/OpenClaw tool.

Outbound is drafts-by-default; /send above is the sole, human-clicked
exception (spec §6 as amended 2026-07-10). Archive/star/label are mailbox
writes beyond Drafts, approved by Casey 2026-07-04 (BUILD_SPEC.md records
both decisions).

Run:  python3 dashboard/serve.py
"""

import http.server
import ipaddress
import json
import os
import re
import socket
import socketserver
import subprocess
import threading
import sys
import time
import urllib.parse

PORT = int(os.environ.get("GC_PORT") or 8080)   # GC_PORT: tests run a scratch instance
DIRECTORY = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# Who may connect (E161, 2026-09-03)
#
# The board used to answer anyone who could route to 0.0.0.0:8080, with no
# authentication at all — so any device on whatever LAN the host sat on could
# POST /send and mail as Casey. Rather than invent a bespoke password layer, we
# lean on the two real auth systems already in front of this box:
#
#   loopback  — the host itself, the MCP server, and cloudflared, which is
#               fronted by Cloudflare Access (email OTP) for remote users
#   tailnet   — 100.64.0.0/10, device-authenticated by Tailscale
#
# Plain LAN clients get the connection refused before a byte of HTTP is
# parsed. Escape hatch: GC_ALLOW_LAN=1 restores the old behaviour without a
# code change, in case Tailscale is down and Casey needs the board from a
# laptop on the same network.
# ---------------------------------------------------------------------------

ALLOW_LAN = os.environ.get("GC_ALLOW_LAN", "").strip().lower() in ("1", "true", "yes")

_ALLOWED_CLIENT_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),    # loopback (incl. cloudflared)
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100.64.0.0/10"),  # Tailscale CGNAT range
]

_REFUSED_SEEN: set = set()


def client_allowed(addr: str) -> bool:
    """True if this peer may talk to the board at all."""
    if ALLOW_LAN:
        return True
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in _ALLOWED_CLIENT_NETS)


# ---------------------------------------------------------------------------
# Cross-site request forgery (E161, 2026-09-03)
#
# do_POST reads the body and json.loads it without ever checking Content-Type,
# so a page on any other site could fire a POST here as a CORS "simple
# request" — no preflight, no way for the browser to block it, and the write
# would execute. Verified 2026-09-03 against /mail-action. The response is
# unreadable cross-origin, but a send does not need a readable response.
#
# Fix: mutating requests must not carry a foreign Origin. Requests with no
# Origin and no Referer are allowed — those are curl, the MCP server and other
# non-browser clients, which are not the CSRF vector. GETs are left alone so
# ordinary links into the board keep working.
# ---------------------------------------------------------------------------

def _origin_hosts() -> set:
    hosts = {"localhost", "127.0.0.1", "::1", "gc.example.com"}
    try:
        name = socket.gethostname().lower().rstrip(".")
        hosts.update({name, name.split(".")[0], f"{name.split('.')[0]}.local"})
    except Exception:
        pass
    hosts.update({h.strip().lower() for h in
                  os.environ.get("GC_ALLOWED_ORIGINS", "").split(",") if h.strip()})
    return hosts


ALLOWED_ORIGIN_HOSTS = _origin_hosts()


# E264 (dev #63, BUILD_SPEC §6 2026-10-01): the Quick Todo Chrome extension
# posts with Origin chrome-extension://<id>, which E161 rightly refuses. One
# extension gets through: the ID its manifest key pins, and only on a request
# carrying the device pairing code — so it is Casey's paired device, not just
# any page or extension that knows the ID.
def _quicktodo_ext_id() -> str:
    try:
        import base64, hashlib
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "capture", "chrome-quick-todo", "manifest.json")
        with open(path, encoding="utf-8") as f:
            der = base64.b64decode(json.load(f)["key"])
        return "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
    except Exception:
        return ""  # no manifest key: no extension gets in


QUICKTODO_EXT_ID = _quicktodo_ext_id()

# GET /labels cache: {account: (fetched_at, [label, ...])} — labels change
# rarely; 5-min TTL keeps thread-opens from hammering IMAP LIST.
_LABEL_CACHE: dict = {}

# GET /calendar cache: {(start, days): (fetched_at, payload)} — ICS feeds
# take seconds and Google refreshes them lazily anyway; 10-min TTL.
_CAL_CACHE: dict = {}
ROOT = os.path.dirname(DIRECTORY)
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

MAX_BODY = 32 * 1024
MAX_DOC_BODY = 640 * 1024         # E259: a 100k-character context document, JSON-escaped
MAX_SEND_BODY = 12 * 1024 * 1024  # E82: /send may carry ≤3 base64 attachments
                                  # (5MB binary cap enforced post-decode; the
                                  # transport cap just needs to exceed b64 bloat)
UPLOAD_DIR = os.path.join(ROOT, "data", "uploads")
MAX_UPLOAD = 20 * 1024 * 1024
UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".heic"}


def _clean(s, limit=200):
    """One line of plain text, bounded. Deeper validation happens in the
    sweeper modules each route calls into."""
    import re
    return re.sub(r"\s+", " ", str(s or "")).strip()[:limit]


_THRID_RE = re.compile(r"^\d{1,25}$")

# Account-first (Casey, 2026-09-24): projects, goals, automatic classification,
# Attio and the old entity dashboard are retired. The routes answer 410 Gone
# instead of vanishing so a stale browser tab or extension fails loudly and
# legibly; every table and file behind them is left untouched (rollback = delete
# these two sets). Nothing here reads or writes the retired data any more.
_RETIRED_GET = {"/bizdev", "/dash", "/registry", "/scratch", "/progress"}
_RETIRED_POST = {"/feedback", "/tag", "/registry", "/dash", "/opnote", "/scratch"}
_RETIRED_MSG = ("retired: Grand Central is account-first — projects, goals, "
                "classification, Attio and the old dashboard are gone")


_PREVIEW_MAGIC = ((b"%PDF-", "application/pdf"), (b"\x89PNG\r\n\x1a\n", "image/png"),
                  (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"))


def _preview_type(data: bytes) -> str | None:
    """The type to show an attachment as, judged from its first bytes —
    PDF, PNG, JPEG, GIF or WebP only; None means download it."""
    head = bytes(data[:16])
    for magic, typ in _PREVIEW_MAGIC:
        if head.startswith(magic):
            return typ
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


def _track_choice(payload: dict):
    """E269: the composer's 👁 Track toggle — True/False when Casey touched
    it, None to let tracking.decide() apply the account's default."""
    v = payload.get("track")
    return v if isinstance(v, bool) else None


def _account_known(conn, account: str) -> bool:
    """A real mail account: configured in config/.env or already synced."""
    try:
        from sources import gmail_reader
        if account in gmail_reader.ACCOUNTS:
            return True
    except Exception:
        pass
    return conn.execute("SELECT 1 FROM mail_threads WHERE account = ? LIMIT 1",
                        (account,)).fetchone() is not None


# E260: the desktop app may keep this many requests waiting at once.
PROPOSALS_PENDING_MAX = 20


def _run_as_agent(path: str, body: dict, tool: str) -> tuple:
    """E260: run one approved desktop request as the agent, over loopback, so
    the board's own gate judges it again at the moment it runs. Returns
    (ok, decoded JSON answer)."""
    import urllib.request
    import urllib.error
    import agent_gate
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}", data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json",
                 "X-GC-Agent-Key": agent_gate.load_key(), "X-GC-Tool": tool[:60]})
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            out = json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            out = json.loads(e.read() or b"{}")
        except ValueError:
            out = {"ok": False, "error": f"HTTP {e.code}"}
        return False, out
    except Exception as e:
        return False, {"ok": False, "error": repr(e)}
    return bool(isinstance(out, dict) and out.get("ok", True) is not False), out


def _top5_resolved(conn, t: dict, generated_at: str, now: str) -> bool:
    """Has Casey resolved this Top 5 thread since the sweep wrote the JSON?
    Resolved = dismissed, done, actively snoozed, or replied through the
    board (a mail_sends row newer than the sweep). Every lookup is scoped to
    the thread's own account."""
    import db
    tid = t.get("thread_id")
    if not tid:
        return False
    if db.is_dismissed(conn, t.get("account") or "", tid):
        return True
    row = conn.execute(
        "SELECT done_at, snoozed_until FROM mail_triage "
        "WHERE thread_id = ? AND account = ?",
        (tid, t.get("account") or "")).fetchone()
    if row and (row["done_at"] or
                (row["snoozed_until"] and row["snoozed_until"] > now)):
        return True
    if generated_at and conn.execute(
            "SELECT 1 FROM mail_sends WHERE thread_id = ? AND account = ? "
            "AND ts > ?", (tid, t.get("account") or "", generated_at)).fetchone():
        return True
    if conn.execute(
            "SELECT 1 FROM mail_outbox WHERE thread_id = ? AND account = ? "
            "AND status IN ('pending', 'sending')",
            (tid, t.get("account") or "")).fetchone():
        return True  # a queued scheduled reply counts as handled
    # E32: a reply Casey sent from Gmail or his phone leaves no mail_sends
    # row, so the board used to keep asking for it until the next hourly
    # sweep. mail_sync refreshes last_direction every ~3 min, so an
    # outbound-last thread whose newest message postdates the sweep means
    # the reply is already gone — whoever sent it.
    row = conn.execute(
        "SELECT last_direction, last_date FROM mail_threads "
        "WHERE thread_id = ? AND account = ?",
        (tid, t.get("account") or "")).fetchone()
    if (row and (row["last_direction"] or "") == "outbound"
            and generated_at and (row["last_date"] or "") > generated_at):
        return True
    return False


def _fetch_forward_atts(payload, account):
    """E153: forwarding carries the ORIGINAL attachments. The client names
    them as {uid, idx} refs into the source thread (forward_of); each is
    fetched from Gmail server-side on the same confirmed send — never
    stored, never crosses HTTP as payload. 20MB total cap (Gmail's own
    send limit is 25MB)."""
    refs = payload.get("forward_atts") or []
    thread_id = _clean(payload.get("forward_of"), 25)
    if not refs or not thread_id:
        return []
    if not isinstance(refs, list) or len(refs) > 10:
        raise ValueError("too many forwarded attachments")
    import mail_actions
    out, total = [], 0
    for r in refs:
        if not isinstance(r, dict):
            raise ValueError("bad forward_atts entry")
        name, ctype, data = mail_actions.fetch_attachment(
            thread_id, account, str(r.get("uid")), int(r.get("idx", -1)))
        total += len(data)
        if total > 20 * 1024 * 1024:
            raise ValueError("forwarded attachments over 20MB — forward from Gmail")
        out.append({"name": name, "mime": ctype, "data": data})
    return out


def _parse_attachments(payload):
    """E82 (§6 amended 2026-08-02, Casey: "let's add basic attachment
    support"): ≤3 files, ≤5MB total, base64 in the /send JSON, decoded
    here and passed to send_mail on the SAME human-confirmed send.
    Never written to disk. Returns [] when absent."""
    atts = payload.get("attachments") or []
    if not atts:
        return []
    # E104: entries carrying a cid are INLINE images (referenced from the
    # HTML body); ≤4 of those, plus ≤3 regular files, ≤5MB all together.
    if not isinstance(atts, list) or len(atts) > 7:
        raise ValueError("too many attachments")
    import base64
    out, total, files, inline = [], 0, 0, 0
    for a in atts:
        if not isinstance(a, dict):
            raise ValueError("bad attachment entry")
        try:
            data = base64.b64decode(str(a.get("data") or ""), validate=True)
        except Exception:
            raise ValueError("bad attachment encoding")
        total += len(data)
        if total > 5 * 1024 * 1024:
            raise ValueError("attachments over 5MB total — use 📎 via Gmail")
        cid = _clean(a.get("cid"), 40)
        if cid and not re.match(r"^[A-Za-z0-9._-]{1,40}$", cid):
            raise ValueError("bad inline image id")
        if cid:
            inline += 1
        else:
            files += 1
        if files > 3:
            raise ValueError("up to 3 attachments")
        if inline > 4:
            raise ValueError("up to 4 inline images")
        out.append({"name": _clean(a.get("name"), 120) or "file",
                    "mime": _clean(a.get("mime"), 100) or "application/octet-stream",
                    "data": data, "cid": cid or None})
    return out


def _mail_sync_status() -> dict:
    """E204: when the inbox was last made current (mail_sync writes the file
    as soon as the accounts are synced) and whether a sync is running now."""
    out = {}
    try:
        with open(os.path.join(ROOT, "data", "mail_sync_status.json")) as f:
            out = json.load(f)
    except (OSError, ValueError):
        pass
    lock = os.path.join(ROOT, "data", "mail_sync.lock")
    try:
        out["running"] = (time.time() - os.path.getmtime(lock)) < 10 * 60
    except OSError:
        out["running"] = False
    return out


def _todo_label_async(eng_id: int, account: str, thread_id: str, add: bool) -> None:
    """E204: mirror one todo membership change into Gmail in the background."""
    def run():
        try:
            import db
            import todo_labels
            c = db.connect()
            try:
                todo_labels.apply(c, eng_id, account, thread_id, add=add,
                                  log=lambda m: print(m, flush=True))
            finally:
                c.close()
        except Exception as e:
            print(f"todo label mirror failed: {e!r}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def _home_accounts(conn) -> list:
    """The accounts the tabs show, in Casey's saved order (same source as
    GET /accounts): accounts with synced mail or todos."""
    rows = conn.execute(
        "SELECT account FROM mail_threads WHERE account IS NOT NULL "
        "UNION SELECT account FROM engagements WHERE account IS NOT NULL").fetchall()
    have = sorted({r[0] for r in rows if r[0]})
    import db
    order = [a for a in db.acct_order(conn) if a in have]
    return order + [a for a in have if a not in order]


_HOME_NOTE_BUSY: set = set()


def _home_note_async(scope: str) -> bool:
    """E249: write today's Home note in the background (one small model
    call). False when one for this scope is already being written."""
    if scope in _HOME_NOTE_BUSY:
        return False
    _HOME_NOTE_BUSY.add(scope)

    def run():
        try:
            import db
            import day_note
            c = db.connect()
            try:
                day_note.generate(c, scope, _home_accounts(c),
                                  log=lambda m: print(m, flush=True))
            finally:
                c.close()
        except Exception as e:
            print(f"home note failed: {e!r}", flush=True)
        finally:
            _HOME_NOTE_BUSY.discard(scope)
    threading.Thread(target=run, daemon=True).start()
    return True


def _todo_rename_async(eng_id: int) -> None:
    """E204: a todo's title changed — move its Gmail label with it."""
    def run():
        try:
            import db
            import todo_labels
            c = db.connect()
            try:
                todo_labels.rename(c, eng_id, log=lambda m: print(m, flush=True))
            finally:
                c.close()
        except Exception as e:
            print(f"todo label rename failed: {e!r}", flush=True)
    threading.Thread(target=run, daemon=True).start()


def _now_z():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _future_iso(s):
    """Parse a snooze target (ISO, Z or offset or naive-local) and return it
    as UTC ISO Z; reject anything not in the future."""
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(s or "").strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("bad snooze time")
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive input means Casey's local time
    dt = dt.astimezone(timezone.utc)
    if dt <= datetime.now(timezone.utc):
        raise ValueError("snooze time must be in the future")
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        # State file changes every sweep; never let a stale copy be cached.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code: int, obj: dict):
        if code >= 400:
            # error responses were invisible in the log (only the status
            # code) — surface route + reason for debugging (E8 diagnostics)
            try:
                print(f"ERR {self.path} -> {code}: {obj.get('error', '')}",
                      file=sys.stderr, flush=True)
            except Exception:
                pass
        gctx = getattr(self, "_gctx", None)
        if gctx is not None and gctx.allowed_accounts is not None and code < 400:
            import agent_gate  # E258: an agent only ever sees switched-on accounts
            obj = agent_gate.filter_response(gctx, obj, self._gate_conn)
        if code < 400:
            import private  # E258: Private threads are gone from every view
            priv = private.keys_cached()
            if priv:
                obj = private.scrub(obj, priv, getattr(self, "_gargs", {}).get("account"))
        body = json.dumps(obj).encode()
        if gctx is not None:
            signals = 0
            if gctx.allowed_accounts is not None and code < 400:
                import untrusted  # injection-shaped text the agent was handed
                signals = untrusted.neutralize_obj(obj)[1]
            self._gate_finish(code, len(body), signals)
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- E258: the agent gate (sweeper/agent_gate.py, BUILD_SPEC §6) ----
    def _gate_conn(self):
        if getattr(self, "_gconn", None) is None:
            import db
            self._gconn = db.connect()
        return self._gconn

    def _gate(self, method: str, route: str, args: dict) -> bool:
        """True = go on. False = refused and already answered."""
        import agent_gate
        self._gctx = None
        self._gconn = None
        self._gargs = args if isinstance(args, dict) else {}
        try:
            self._gctx = agent_gate.check(self._gate_conn, self.headers, method, route, args)
            if not self._gctx.audit and self._gconn is not None:
                self._gconn.close()
                self._gconn = None
            return True
        except agent_gate.Refused as e:
            self._gctx = e.ctx
            print(f"GATE refused {self._gctx.who if e.ctx else '?'} {method} {route}: {e}",
                  file=sys.stderr, flush=True)
            self._json(e.status, {"ok": False, "error": f"Refused: {e}", **e.extra})
            return False

    def _is_agent(self) -> bool:
        g = getattr(self, "_gctx", None)
        return g is not None and g.who.startswith("agent")

    def _agent_draft(self, account, to, cc, bcc):
        """E258 item 5: None for Casey; for an agent, who on the draft is new."""
        if not self._is_agent():
            return None
        import agent_gate
        return {"new": agent_gate.new_recipients(self._gate_conn(), account, to, cc, bcc)}

    @staticmethod
    def _agent_note(agent):
        if not agent:
            return {}
        return {"flagged": "GC/Agent-Draft-New" if agent["new"] else "GC/Agent-Draft",
                "new_recipients": agent["new"],
                "reminder": ("Saved to Drafts, NOT sent. Flagged for Casey: new to this "
                             "account — " + ", ".join(agent["new"])) if agent["new"]
                            else "Saved to Drafts, NOT sent."}

    def _gate_finish(self, code: int, nbytes: int, signals: int = 0):
        gctx, self._gctx = self._gctx, None
        if gctx is None or not gctx.audit:
            return
        try:
            import agent_gate
            agent_gate.finish(self._gate_conn, gctx, code, nbytes, signals)
        except Exception as e:  # the audit must never break the answer
            print(f"agent audit failed: {e!r}", file=sys.stderr, flush=True)
        finally:
            if getattr(self, "_gconn", None) is not None:
                self._gconn.close()
                self._gconn = None

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if not self._gate("GET", parsed.path, {k: v[0] for k, v in
                          urllib.parse.parse_qs(parsed.query).items()}):
            return
        if parsed.path in _RETIRED_GET:
            self._json(410, {"ok": False, "error": _RETIRED_MSG})
            return
        if parsed.path == "/grand_central.json":
            # The contract file, with Top 5 filtered through live triage on
            # the way out: rows Casey has resolved since the sweep (done /
            # dismissed / snoozed / replied / queued a reply) drop instantly
            # instead of at the next hourly sweep; top5_pool backfills to 5.
            try:
                with open(os.path.join(DIRECTORY, "grand_central.json"),
                          encoding="utf-8") as f:
                    data = json.load(f)
                pool = data.get("top5_pool") or data.get("top5") or []
                if pool:
                    import db
                    gen = data.get("generated_at") or ""
                    now = _now_z()
                    conn = db.connect()
                    try:
                        data["top5"] = [
                            t for t in pool
                            if not _top5_resolved(conn, t, gen, now)][:5]
                    finally:
                        conn.close()
                self._json(200, data)
            except Exception:
                super().do_GET()  # never let the filter break the board
            return
        if parsed.path == "/mail":
            # The Mail screen's thread list (E3-P1): mail_threads (written by
            # sweeper/mail_sync.py every ~3 min) joined with triage state and
            # scoped with ?account=. This view is DB-backed like /thread and
            # /task; grand_central.json carries the sweep's rails.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                include_done = (qs.get("all") or ["0"])[0] in ("1", "true")
                acct = _clean((qs.get("account") or [""])[0], 40) or None
                conn = db.connect()
                try:
                    threads = db.mail_view(conn, include_done=include_done,
                                           account=acct)
                    if acct:
                        # E217: each row carries its classification summary
                        # (type, respond, value) for chips, sorting and needs
                        cls = db.class_for_threads(conn, acct)
                        for t in threads:
                            t["cls"] = cls.get(t.get("thread_id"))
                        jev_on = db.class_account_jev(conn, acct)
                    else:
                        jev_on = False
                    # E253: your own services (services.py) — each thread's
                    # bundle, after Casey's merges and off-switches
                    import services
                    stbl = services.table(conn)
                    svc_map = {}
                    for t in threads:
                        k = services.resolve(t.get("service"), stbl)
                        t["svc"] = k
                        if k:
                            svc_map[k] = {"label": stbl[k]["label"], "kind": stbl[k]["kind"]}
                    outbox = db.open_outbox(conn)
                    row = conn.execute("SELECT MAX(synced_at) AS s FROM mail_threads").fetchone()
                    synced_at = row["s"] if row else None
                finally:
                    conn.close()
                gctx = getattr(self, "_gctx", None)
                if gctx is not None and gctx.allowed_accounts is not None:
                    # E258: an agent gets one page, not the whole board — the
                    # whole board is ~1.4MB, which alone would spend the 2MB/hr
                    # budget. Filters run before the page is cut.
                    unread = (qs.get("unread") or ["0"])[0] == "1"
                    starred = (qs.get("starred") or ["0"])[0] == "1"
                    try:
                        limit = max(1, min(100, int((qs.get("limit") or ["25"])[0])))
                    except ValueError:
                        limit = 25
                    threads = [t for t in threads
                               if t.get("account") in gctx.allowed_accounts
                               and (not unread or t.get("unread"))
                               and (not starred or t.get("starred"))]
                    self._json(200, {"ok": True, "synced_at": synced_at,
                                     "total": len(threads), "threads": threads[:limit]})
                    return
                # Dev #66: who Casey tagged VIP / Top prospect (shared by every account)
                try:
                    import tiers as _tiers
                    _c = db.connect()
                    try:
                        tier_map = _tiers.tiers(_c)
                    finally:
                        _c.close()
                except Exception:
                    tier_map = {}
                self._json(200, {"ok": True, "synced_at": synced_at, "tiers": tier_map,
                                 "jev": bool(jev_on),     # Jev-led Smart grouping
                                 "sync": _mail_sync_status(),
                                 "services": svc_map,
                                 "threads": threads, "outbox": outbox})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/signatures":
            # 2026-10-08: each account's HTML signature (and its text twin)
            try:
                import db, signatures
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "signatures": signatures.get_all(conn)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path in ("/watch", "/watch/suggest", "/watch/person"):
            # E276: follow-ups Casey chose — one account's list, the after-send
            # suggestion (rules only), or a person's open assignments.
            try:
                import db, watch
                from common import load_env as _le
                qs = urllib.parse.parse_qs(parsed.query)
                conn = db.connect()
                try:
                    acct = _clean((qs.get("account") or [""])[0], 40)
                    if parsed.path == "/watch/person":
                        out = {"assigned": watch.open_assignments(conn, _clean((qs.get("email") or [""])[0], 120))}
                    else:
                        if not acct or not _account_known(conn, acct):
                            raise ValueError("known account required")
                        if parsed.path == "/watch":
                            out = {"watches": watch.listing(conn, acct, include_closed=(qs.get("closed") or ["0"])[0] == "1"),
                                   "ask": watch.ask_mode(conn, acct)}
                        else:
                            out = watch.suggest(conn, acct, _le(), _clean((qs.get("to") or [""])[0], 500),
                                                _clean((qs.get("cc") or [""])[0], 500),
                                                _clean((qs.get("thread_id") or [""])[0], 25) or None)
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path in ("/tracking", "/tracking/decide", "/tracking/summary"):
            # E269 (§6 2026-10-06): open/click tracking — the per-account
            # switches for Settings, the composer's 👁 default, and one
            # account's opens/clicks keyed by thread. Reads only.
            try:
                import db, tracking
                from common import load_env as _le
                qs = urllib.parse.parse_qs(parsed.query)
                env = _le()
                conn = db.connect()
                try:
                    acct = _clean((qs.get("account") or [""])[0], 40)
                    if parsed.path == "/tracking":
                        from sources.gmail_reader import scan_accounts
                        out = {"secret_set": bool((env.get("TRACK_SECRET") or "").strip()),
                               "accounts": [{"account": a, "domain": tracking.domain_for(a, env),
                                             "healthy": tracking.healthy(conn, tracking.domain_for(a, env)),
                                             **tracking.settings(conn, a)} for a in scan_accounts(env)]}
                    else:
                        if not acct or not _account_known(conn, acct):
                            raise ValueError("known account required")
                        if parsed.path == "/tracking/decide":
                            out = tracking.decide(conn, acct, env, _clean((qs.get("to") or [""])[0], 500),
                                                  _clean((qs.get("cc") or [""])[0], 500),
                                                  _clean((qs.get("bcc") or [""])[0], 500))
                        else:
                            out = {"threads": tracking.summary(conn, acct)}
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/sent":
            # E73: recent Sent mail for exactly one selected account, fetched
            # live like /drafts (read-only; Sent changes outside the board).
            try:
                import mail_actions
                from common import load_env as _le
                qs = urllib.parse.parse_qs(parsed.query)
                account = _clean((qs.get("account") or [""])[0], 40)
                from sources.gmail_reader import ACCOUNTS as _A
                if account not in _A:
                    raise ValueError("known account required")
                env = _le()
                rows, errors, nxt = [], [], None
                before = _clean((qs.get("before") or [""])[0], 12)
                try:
                    page = mail_actions.list_sent(account, env,
                                                  before_uid=int(before) if before.isdigit() else None)
                    rows, nxt = page["rows"], page["next_before"]
                except Exception as e:
                    errors.append(f"{account}: {e}")
                self._json(200, {"ok": True, "sent": rows, "next_before": nxt, "errors": errors})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/calendar":
            # E76: live ICS range read — the baked board window is only 7
            # days from the last sweep, so day-nav/month views fetch here.
            # Attendees/organizer never ship (E43).
            try:
                import time   # the /labels branch's inner import makes
                              # `time` local to all of do_GET — bind it here
                from datetime import date as _date
                from common import load_env as _le
                from sources import calendar_reader
                qs = urllib.parse.parse_qs(parsed.query)
                start_s = _clean((qs.get("start") or [""])[0], 10)
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", start_s or ""):
                    raise ValueError("bad start date")
                days = max(1, min(70, int((qs.get("days") or ["42"])[0])))
                key = (start_s, days)
                hit = _CAL_CACHE.get(key)
                if (hit and (qs.get("refresh") or ["0"])[0] != "1"
                        and time.time() - hit[0] < 600):
                    self._json(200, hit[1])
                    return
                out = calendar_reader.read_range(
                    _le(), _date.fromisoformat(start_s), days,
                    log=lambda *a: None)
                for d in out:
                    for e in d["events"]:
                        e.pop("attendees", None)
                        e.pop("organizer", None)
                from home_view import CAL_ACCOUNT
                payload = {"ok": True, "start": start_s, "days": days,
                           "cal_account": CAL_ACCOUNT,   # 📁 files an event under its own account
                           "calendar": out}
                _CAL_CACHE[key] = (time.time(), payload)
                self._json(200, payload)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/daynotes":
            # E76: non-empty notes over [start, end] — the journal layer on
            # calendar views (📓 markers on the month grid).
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                start_s = _clean((qs.get("start") or [""])[0], 10)
                end_s = _clean((qs.get("end") or [""])[0], 10)
                if not (re.match(r"^\d{4}-\d{2}-\d{2}$", start_s or "")
                        and re.match(r"^\d{4}-\d{2}-\d{2}$", end_s or "")):
                    raise ValueError("bad date range")
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "notes":
                                     db.list_day_notes(conn, start_s, end_s)})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/plan":
            # E100: today's plan (?date=, ?refresh=1 regenerates now).
            try:
                import db
                import planner
                from common import load_env as _le
                qs = urllib.parse.parse_qs(parsed.query)
                date = _clean((qs.get("date") or [""])[0], 10) or None
                conn = db.connect()
                try:
                    if (qs.get("refresh") or ["0"])[0] == "1":
                        plan = planner.generate(conn, _le())
                    else:
                        plan = planner.get(conn, date)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "plan": plan})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/daynote":
            # E69: the day's free-form note (Today view journal). ?date=
            # comes from the CLIENT's local day so timezones never fight.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                date = _clean((qs.get("date") or [_now_z()[:10]])[0], 10)
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, **db.get_day_note(conn, date)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/braindump":
            # E134: parked ideas (idea + promoted; archived stay out).
            try:
                import db
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "ideas": db.braindump_list(conn)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/push":
            # E188: everything the notifications screen needs in one read —
            # the VAPID public key, the devices subscribed, and which channel
            # each kind of alert uses. No secrets: the private key stays put.
            try:
                import db, push
                conn = db.connect()
                try:
                    self._json(200, {
                        "ok": True,
                        "public_key": push.public_key(),
                        "configured": push.configured(),
                        "devices": [{"id": r["id"], "label": r["label"],
                                     "created_at": r["created_at"], "last_ok": r["last_ok"],
                                     "host": urllib.parse.urlsplit(r["endpoint"]).hostname}
                                    for r in db.push_subs(conn)],
                        "prefs": db.notify_prefs(conn),
                    })
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/accounts":
            # Account tabs bootstrap from ownership already present in the
            # scratch/live DB. No message or todo content crosses this route.
            try:
                import db
                conn = db.connect()
                try:
                    rows = conn.execute(
                        "SELECT account FROM mail_threads WHERE account IS NOT NULL "
                        "UNION SELECT account FROM engagements WHERE account IS NOT NULL"
                    ).fetchall()
                    accounts = sorted({r[0] for r in rows if r[0]})
                    order = db.acct_order(conn)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "accounts": accounts, "order": order})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/home":
            # E249: Home's sections. BUILD_SPEC §6 (2026-09-29): a read-only
            # view — each account is built on its own (home_view.section) and
            # every row names its account; nothing here writes. ?account=
            # narrows to that account's own Home.
            try:
                import db
                import home_view
                qs = urllib.parse.parse_qs(parsed.query)
                acct = _clean((qs.get("account") or [""])[0], 40)
                conn = db.connect()
                try:
                    known = _home_accounts(conn)
                    if acct and acct not in known:
                        raise ValueError("unknown account")
                    self._json(200, {"ok": True, **home_view.home(
                        conn, [acct] if acct else known)})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/home/note":
            # E249: today's few-sentence note. Stored once per day per scope
            # ('' = every account, else one account); the first read of the
            # day, or ?refresh=1, starts writing it in the background.
            try:
                import db
                import day_note
                qs = urllib.parse.parse_qs(parsed.query)
                scope = _clean((qs.get("account") or [""])[0], 40)
                conn = db.connect()
                try:
                    if scope and scope not in _home_accounts(conn):
                        raise ValueError("unknown account")
                    note = day_note.get(conn, scope)
                finally:
                    conn.close()
                pending = False
                if note is None or (qs.get("refresh") or ["0"])[0] == "1":
                    pending = _home_note_async(scope) or scope in _HOME_NOTE_BUSY
                self._json(200, {"ok": True, "scope": scope, "note": note,
                                 "pending": pending})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/dev":
            # DevRequests are intake inside the selected account, never a
            # cross-account queue. Ownerless historical rows remain stored
            # but are intentionally invisible in every account view.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                account = _clean((qs.get("account") or [""])[0], 40)
                conn = db.connect()
                try:
                    if not account or not _account_known(conn, account):
                        raise ValueError("known account required")
                    import dev_pairing
                    self._json(200, {"ok": True,
                                     "requests": db.dev_list(conn, account),
                                     "pairing_set": dev_pairing.is_configured()})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path in ("/classify", "/classify/settings"):
            # E209: one email's classification (account-scoped), or the
            # roster + per-account Jev switches for Settings. Never a key value.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                conn = db.connect()
                try:
                    if parsed.path == "/classify":
                        account = _clean((qs.get("account") or [""])[0], 40)
                        thread_id = _clean((qs.get("thread_id") or [""])[0], 40)
                        if not account or not _account_known(conn, account) or not thread_id:
                            raise ValueError("known account and thread_id required")
                        self._json(200, {"ok": True,
                                         "classification": db.class_get(conn, account, thread_id)})
                    else:
                        from common import load_env
                        from sources import gmail_reader
                        accts = list(gmail_reader.scan_accounts(load_env()))
                        import services
                        self._json(200, {"ok": True, **db.class_roster(conn),
                                         "services": services.listing(conn),
                                         "jev_key_set": bool((load_env().get("JEV_API_KEY") or "").strip()),
                                         "accounts": [{"account": a, "jev": db.class_account_jev(conn, a)}
                                                      for a in accts]})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/steward":
            # E59: pending sender suggestions awaiting Casey's verdict.
            try:
                import db
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "pending": db.steward_pending(conn)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/wizard":
            # E111: pending 🧙 suggestions awaiting Casey's verdict.
            try:
                import db
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "pending": db.wizard_pending(conn)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/connections":
            # E51: source/credential status for the Connections tab. Presence +
            # a masked hint only — never a secret value (connections.status()).
            try:
                import connections
                self._json(200, {"ok": True, **connections.status()})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/version":
            # E28: the board is a long-lived single page — Casey leaves a tab
            # open for days. Without this, a deploy lands on the server while
            # his browser keeps running the JS it loaded hours ago, and new
            # features look "not implemented". The page polls this and offers
            # a reload when the served bundle changes.
            try:
                stamp = 0.0
                for name in ("index.html",):
                    p = os.path.join(DIRECTORY, name)
                    if os.path.exists(p):
                        stamp = max(stamp, os.path.getmtime(p))
                self._json(200, {"ok": True, "version": str(int(stamp))})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return

        if parsed.path == "/quicktodo.zip":
            # The Chrome quick-capture extension (capture/chrome-quick-todo),
            # zipped fresh on each request so the download never goes stale.
            # Install: unzip → chrome://extensions → Developer mode →
            # Load unpacked (steps are in the README inside the zip).
            try:
                import io
                import zipfile
                src = os.path.join(ROOT, "capture", "chrome-quick-todo")
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                    for dirpath, _dirs, files in os.walk(src):
                        for f in sorted(files):
                            if f.startswith("."):
                                continue
                            p = os.path.join(dirpath, f)
                            z.write(p, os.path.join("chrome-quick-todo",
                                                    os.path.relpath(p, src)))
                data = buf.getvalue()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 'attachment; filename="chrome-quick-todo.zip"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/brief":
            # E36: a compact status pull for agents (OpenClaw). The full
            # board is ~800KB across grand_central.json + /mail;
            # an agent that ingests all of it on every run burns enormous
            # input tokens. This returns a few KB — the hot list, owed
            # replies (capped), today's plan and today's calendar —
            # everything a "what's up"
            # answer needs, nothing it doesn't. Read-only.
            try:
                import db
                with open(os.path.join(DIRECTORY, "grand_central.json"),
                          encoding="utf-8") as f:
                    d = json.load(f)
                today = _now_z()[:10]
                conn = db.connect()
                try:
                    todos = db.todo_view(conn)
                    # E69: Casey's free-form note for the day rides along as
                    # agent context (no interpretation — just the text).
                    day_note = db.get_day_note(conn, today).get("text") or None
                finally:
                    conn.close()

                def slim_todo(t):
                    return {k: t.get(k) for k in
                            ("id", "text", "due", "account", "starred")}
                plan = [slim_todo(t) for t in todos
                        if t.get("state") == "open" and
                        ((t.get("my_day") or "")[:10] == today
                         or (t.get("due") or "9999") <= today)][:25]
                owed = [{k: r.get(k) for k in
                         ("who", "what", "thread_id", "account")}
                        for r in (d.get("reply_debt") or [])][:15]

                # E66 (OpenClaw feedback): meetings_today is FROZEN at sweep
                # time — overnight it's yesterday's calendar. Resolve today's
                # events by REAL date from the multi-day calendar instead;
                # fall back to the baked list only if no row matches.
                cal_today = d.get("meetings_today") or []
                cal_date = None
                for day in (d.get("calendar") or []):
                    if day.get("date") == today:
                        cal_today = day.get("events") or []
                        cal_date = today
                        break

                # Staleness is explicit now — an agent should never have to
                # infer it from a timestamp. Sweeps run 07:00-19:00, so
                # overnight age is normal; stale=true past 90 min.
                age_min = None
                try:
                    from datetime import datetime as _dt
                    gen = _dt.strptime(d.get("generated_at", ""),
                                       "%Y-%m-%dT%H:%M:%SZ")
                    age_min = int((_dt.utcnow() - gen).total_seconds() // 60)
                except Exception:
                    pass
                stale = age_min is None or age_min > 90

                # ?refresh=1 — an agent may explicitly kick a sweep when
                # stale (same lock/subprocess as the Sweep button; ~4 min).
                # Never automatic: sweeps cost classifier tokens and Casey
                # chose the 07:00-19:00 schedule deliberately.
                refresh_started = False
                qs = urllib.parse.parse_qs(parsed.query)
                if stale and (qs.get("refresh") or ["0"])[0] == "1":
                    lock = os.path.join(ROOT, "data", "sweep.lock")
                    if not os.path.exists(lock):
                        venv_python = os.path.join(ROOT, ".venv", "bin", "python")
                        subprocess.Popen(
                            [venv_python, os.path.join(ROOT, "sweeper", "sweep.py")],
                            cwd=os.path.join(ROOT, "sweeper"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        refresh_started = True

                self._json(200, {
                    "ok": True,
                    "generated_at": d.get("generated_at"),
                    "age_minutes": age_min,
                    "stale": stale,
                    "stale_note": ("board data is %s min old; mail/todos are live, "
                                   "calendar_today is date-resolved; pass ?refresh=1 "
                                   "to start a sweep (~4 min)" % age_min) if stale else None,
                    "refresh_started": refresh_started,
                    "next_sweep_at": d.get("next_sweep_at"),
                    "summary": d.get("summary"),
                    "needs_reply": d.get("top5") or [],
                    "owed_count": len(d.get("reply_debt") or []),
                    "owed_sample": owed,
                    "plan_today": plan,
                    "day_note": day_note,
                    "calendar_today": cal_today,
                    "calendar_today_date": cal_date or "baked-at-sweep",
                })
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/ask":
            # E25: Ask conversation history. No id -> the list of past chats;
            # ?id=N -> that chat's full transcript.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                chat_id = (qs.get("id") or [""])[0]
                conn = db.connect()
                try:
                    if chat_id:
                        chat = db.ask_chat(conn, int(chat_id))
                        if not chat:
                            self._json(404, {"ok": False, "error": "no such chat"})
                            return
                        self._json(200, {"ok": True, "chat": chat})
                    else:
                        # step 5: ?project=N lists only that project's chats
                        pj = int((qs.get("project") or ["0"])[0] or 0) or None
                        self._json(200, {"ok": True, "chats": db.ask_chats(conn, project_id=pj)})
                finally:
                    conn.close()
            except ValueError:
                self._json(400, {"ok": False, "error": "bad chat id"})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/drafts":
            # E23: what's waiting in Gmail Drafts. Drafts are the default
            # outbound path (draft_reply + the Ask assistant both APPEND
            # there) but nothing here could see them, so a drafted reply
            # effectively vanished. Read-only; sending stays the dashboard's
            # one human-confirmed path (§6).
            try:
                import mail_actions
                qs = urllib.parse.parse_qs(parsed.query)
                acct = _clean((qs.get("account") or ["all"])[0], 40)
                accounts = (["personal", "brightline"]
                            if acct == "all" else [acct])
                rows, errors = [], []
                for a in accounts:
                    try:
                        rows.extend(mail_actions.list_drafts(a, limit=50))
                    except Exception as e:   # one bad account != no drafts
                        errors.append(f"{a}: {e}")
                rows.sort(key=lambda r: r.get("date") or "", reverse=True)
                self._json(200, {"ok": True, "drafts": rows, "errors": errors})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path in ("/projects", "/project", "/notes", "/projects/suggest"):
            # Projects (BUILD_SPEC §6, 2026-09-29): account-less containers
            # that point at items in their own accounts. Reads only.
            try:
                import db, projects
                qs = urllib.parse.parse_qs(parsed.query)
                conn = db.connect()
                try:
                    if parsed.path == "/projects":
                        out = {"ok": True, "projects": projects.list_projects(conn),
                               "max_pins": projects.MAX_PINS}
                        kind = _clean((qs.get("kind") or [""])[0], 10)
                        if kind:
                            # the 📁 picker: which projects already hold this item
                            # ({project_id: item_id}, so a second click removes it)
                            acct = _clean((qs.get("account") or [""])[0], 40) or None
                            ref = _clean((qs.get("ref") or [""])[0], 300)
                            if kind == "contact":
                                acct, ref = None, ref.lower()
                            out["in"] = {r[0]: r[1] for r in conn.execute(
                                "SELECT project_id, id FROM project_items WHERE kind = ? AND account IS ? AND ref = ?",
                                (kind, acct, ref))}
                    elif parsed.path == "/projects/suggest":
                        # step 6: "Add to <project>?" for one account's rows
                        # and reading pane — new mail from a project person
                        acct = _clean((qs.get("account") or [""])[0], 40)
                        if not acct or not _account_known(conn, acct):
                            raise ValueError("known account required")
                        out = {"ok": True, "suggest": [
                            {k: r[k] for k in ("project_id", "name", "color", "thread_id", "person", "person_name")}
                            for r in projects.suggestions(conn, account=acct)]}
                    elif parsed.path == "/notes":
                        # step 4: the Notes page — every note, loose or in a project
                        out = {"ok": True, "notes": projects.notes_list(
                            conn, _clean((qs.get("q") or [""])[0], 100) or None)}
                    else:
                        import mailclass
                        from common import load_env as _le
                        pid = int(_clean((qs.get("id") or ["0"])[0], 12) or 0)
                        out = {"ok": True, "project": projects.project_view(
                            conn, pid, mine=mailclass.own_addresses(_le()))}
                finally:
                    conn.close()
                self._json(200, out)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if parsed.path == "/calendar/invite":
            # Dev #16: is this thread an invitation Casey can answer, and what
            # did he already say? Read-only (IMAP + a calendar list).
            try:
                import calendar_rsvp
                qs = urllib.parse.parse_qs(parsed.query)
                out = calendar_rsvp.status(
                    _clean((qs.get("account") or [""])[0], 40),
                    _clean((qs.get("thread_id") or [""])[0], 25))
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if parsed.path == "/draft":
            # E224 (Dev #19): one draft, whole, for GC's composer. Read-only.
            try:
                import mail_actions
                qs = urllib.parse.parse_qs(parsed.query)
                out = mail_actions.get_draft(
                    _clean((qs.get("account") or [""])[0], 40),
                    _clean((qs.get("uid") or [""])[0], 12),
                    _clean((qs.get("gm_msgid") or [""])[0], 25))
                self._json(200, {"ok": True, "draft": out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
            return
        if parsed.path == "/context/docs":
            # E259: context documents per account/project. The list carries
            # no bodies; ?id= returns one whole. Not an agent route (DENIED).
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                conn = db.connect()
                try:
                    did = int(_clean((qs.get("id") or ["0"])[0], 12) or 0)
                    if did:
                        d = db.get_context_doc(conn, did)
                        if not d:
                            raise ValueError("no such document")
                        out = {"ok": True, "doc": d}
                    else:
                        scope = _clean((qs.get("scope") or [""])[0], 60)
                        out = {"ok": True, "max_chars": db.CONTEXT_DOC_MAX,
                               "docs": db.list_context_docs(conn, [db._doc_scope(scope)] if scope else None,
                                                            with_body=False)}
                finally:
                    conn.close()
                self._json(200, out)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/context":
            # E19: Casey's context corpus — rules/notes that future
            # automations, prompts, and reply suggestions will draw on.
            try:
                import db
                conn = db.connect()
                try:
                    notes = db.list_context(conn)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "context": notes})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/contacts/tiers":
            # Dev #66: suggested VIPs and Top Prospects (Casey confirms each)
            try:
                import db, tiers, mailclass
                from common import load_env
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "tiers": tiers.tiers(conn),
                                     **tiers.suggest(conn, mailclass.own_addresses(load_env()))})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/contacts":
            # Dev #32-#34: one account's address book — names and addresses
            # only, from its own mail (never another account's), plus Casey's edits.
            try:
                import db
                from common import load_env
                from sources import gmail_reader
                qs = urllib.parse.parse_qs(parsed.query)
                account = _clean((qs.get("account") or [""])[0], 40)
                conn = db.connect()
                try:
                    if not account or not _account_known(conn, account):
                        raise ValueError("known account required")
                    env = load_env()
                    own = {(env.get(uk) or "").strip().lower()
                           for uk, _ in gmail_reader.scan_accounts(env).values()}
                    self._json(200, {"ok": True, "contacts": db.contacts_for(conn, account, own)})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/todos/unassigned":
            # 2026-09-26: legacy todos that belong to no account yet. They
            # belong to no account, so listing them crosses no boundary; each
            # is moved into one account by an explicit /task update.
            try:
                import db
                conn = db.connect()
                try:
                    self._json(200, {"ok": True, "todos": db.todo_unassigned(conn)})
                finally:
                    conn.close()
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/todos":
            # The Todos screen (E10) — DB-backed like /mail: every open todo
            # with its project/goal names, snooze state, and email link.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                acct = _clean((qs.get("account") or [""])[0], 40) or None
                conn = db.connect()
                try:
                    todos = db.todo_view(conn, account=acct)
                finally:
                    conn.close()
                gctx = getattr(self, "_gctx", None)
                if gctx is not None and gctx.allowed_accounts is not None:
                    # E258: an agent gets the filtered page and only the fields
                    # it reads (the whole list is ~90KB of the 2MB/hr budget)
                    q = {k: (qs.get(k) or [""])[0] for k in
                         ("state", "starred", "due_before", "search", "limit")}
                    state = q["state"] or "open"
                    keep = ("id", "text", "due", "state", "starred", "my_day", "priority",
                            "account", "labels", "notes", "snoozed_until")
                    rows = [t for t in todos
                            if t.get("account") in gctx.allowed_accounts
                            and (state == "all" or t.get("state") == state)
                            and (q["starred"] != "1" or t.get("starred"))
                            and (not q["due_before"] or (t.get("due") and t["due"] <= q["due_before"]))
                            and (not q["search"] or q["search"].lower() in (t.get("text") or "").lower())]
                    try:
                        limit = max(1, min(200, int(q["limit"] or 50)))
                    except ValueError:
                        limit = 50
                    self._json(200, {"ok": True, "total": len(rows),
                                     "todos": [{k: t[k] for k in keep if t.get(k) not in (None, "", [])}
                                               for t in rows[:limit]]})
                    return
                self._json(200, {"ok": True, "todos": todos})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/agent/proposals":
            # E260: the desktop app checks on what it asked for; Settings lists
            # them too. The gate trims rows to switched-on accounts for agents.
            try:
                import db
                qs = urllib.parse.parse_qs(parsed.query)
                conn = db.connect()
                try:
                    pid = (qs.get("id") or [""])[0]
                    if pid:
                        row = db.proposal_get(conn, int(pid))
                        rows = [row] if row else []
                    else:
                        status = _clean((qs.get("status") or ["pending"])[0], 12)
                        if status not in ("pending", "approved", "declined", "failed", "expired"):
                            raise ValueError(f"unknown status {status!r}")
                        rows = db.proposal_list(conn, status, limit=50)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "proposals": rows})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/agent/accounts":
            # E258: the switches + lockout state for Settings (not an agent route).
            try:
                import agent_gate
                import db
                from sources.gmail_reader import ACCOUNTS as _KNOWN
                conn = db.connect()
                try:
                    rows = agent_gate.account_rows(conn, list(_KNOWN))
                    lock = agent_gate.lock_state(conn)
                    pending = [dict(r) for r in conn.execute(
                        "SELECT id, scope, kind, text, created_at FROM context_notes "
                        "WHERE source = 'agent' AND active = 0 ORDER BY id DESC LIMIT 50")]
                    # the first-week review, on the phone: newest 100 calls
                    private_rows = [dict(r) for r in conn.execute(
                        # aliased so the Private scrub doesn't remove the list itself
                        "SELECT account AS acct, thread_id AS tid, marked_at FROM private_threads "
                        "ORDER BY marked_at DESC LIMIT 200")]
                    import calendar_invite
                    invites = calendar_invite.pending(conn)
                    recent = [dict(r) for r in conn.execute(
                        "SELECT ts, principal, tool, method, route, substr(args, 1, 400) AS args, "
                        "accounts, status, result_bytes, refusal, signals FROM agent_audit "
                        "ORDER BY id DESC LIMIT 100")]
                    proposals = db.proposal_list(conn, "pending", limit=50)   # E260
                finally:
                    conn.close()
                self._json(200, {"ok": True, "accounts": rows, "lock": lock,
                                 "key_set": bool(agent_gate.load_key()),
                                 "desktop_key_set": bool(agent_gate.load_key(agent_gate.DESKTOP_KEY_PATH)),
                                 "pending_context": pending, "pending_proposals": proposals,
                                 "audit": recent, "private": private_rows, "invites": invites})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/mail/search":
            # E9.5: Gmail-syntax search across All Mail (read-only) so any
            # email — archived, ancient, either account — can be found. Every
            # row carries its own `account`.
            try:
                import db
                import mail_actions
                qs = urllib.parse.parse_qs(parsed.query)
                q = _clean((qs.get("q") or [""])[0], 200)
                acct = _clean((qs.get("account") or ["all"])[0], 40)
                from sources.gmail_reader import ACCOUNTS as _KNOWN
                if acct != "all" and acct not in _KNOWN:
                    raise ValueError(f"unknown account {acct!r}")
                accounts = list(_KNOWN) if acct == "all" else [acct]
                gctx = getattr(self, "_gctx", None)
                if gctx is not None and gctx.allowed_accounts is not None:
                    # E258: an agent's "all" never even searches a switched-off account
                    accounts = [a for a in accounts if a in gctx.allowed_accounts]
                rows, errors = [], []
                for a in accounts:
                    try:
                        rows.extend(mail_actions.search_threads(q, a, limit=30))
                    except Exception as e:
                        errors.append(f"{a}: {e}")
                rows.sort(key=lambda r: r["last_date"], reverse=True)
                rows = rows[:40]
                # account emails power the "open this search in Gmail" links
                from common import load_env
                from sources.gmail_reader import ACCOUNTS as _ACCTS
                env = load_env()
                emails = {a: env.get(_ACCTS[a][0], "") for a in _ACCTS if a in accounts}
                self._json(200, {"ok": True, "threads": rows,
                                 "emails": emails,
                                 "errors": errors or None})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/reply-meta":
            # Read-only: the recipients/subject a reply to this thread would
            # carry — powers the Send confirm dialog for manually-typed
            # replies (AI drafts already return to/cc/subject).
            try:
                import mail_common as mc
                from common import load_env
                qs = urllib.parse.parse_qs(parsed.query)
                thread_id = mc.validate_thread_id(_clean((qs.get("thread_id") or [""])[0], 25))
                account = _clean((qs.get("account") or [""])[0], 40)
                imap, user = mc.connect(account, load_env())
                try:
                    h = mc.reply_headers(imap, user, thread_id)
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
                self._json(200, {"ok": True, "to": h["to"], "cc": h["cc"],
                                 "subject": h["subject"]})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/labels":
            # Read-only: the account's user-created Gmail labels (IMAP LIST),
            # cached 5 min per account — powers the label type-ahead.
            try:
                import time
                import mail_actions
                qs = urllib.parse.parse_qs(parsed.query)
                account = _clean((qs.get("account") or [""])[0], 40)
                cached = _LABEL_CACHE.get(account)
                if cached and time.time() - cached[0] < 300:
                    labels = cached[1]
                else:
                    labels = mail_actions.list_account_labels(account)
                    _LABEL_CACHE[account] = (time.time(), labels)
                self._json(200, {"ok": True, "labels": labels})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/attachment":
            # E82: decode one attachment part straight off IMAP — read-only,
            # never stored, forced download (nosniff + attachment disposition
            # so a hostile HTML part can't render same-origin).
            try:
                import mail_actions
                qs = urllib.parse.parse_qs(parsed.query)
                name, ctype, data = mail_actions.fetch_attachment(
                    _clean((qs.get("thread_id") or [""])[0], 25),
                    _clean((qs.get("account") or [""])[0], 40),
                    _clean((qs.get("uid") or [""])[0], 12),
                    int((qs.get("idx") or ["0"])[0]))
                safe = re.sub(r'[^\w. ()\[\]-]', "_", name)[:120] or "attachment"
                # 2026-10-07 (Casey: "preview PDFs and image attachments, before
                # downloading them"): ?view=1 shows a PDF or a plain image in
                # the browser — only when the bytes really are that type (magic
                # number), typed by us, never the sender's label; SVG/HTML and
                # everything else still download.
                view = _preview_type(data) if (qs.get("view") or ["0"])[0] == "1" else None
                self.send_response(200)
                self.send_header("Content-Type", view or ctype or "application/octet-stream")
                self.send_header("Content-Disposition",
                                 f'{"inline" if view else "attachment"}; filename="{safe}"')
                if view and view.startswith("image/"):
                    self.send_header("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "private, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        if parsed.path == "/thread":
            try:
                import db
                import mail_actions
                import mail_cache
                import mail_common as mc
                qs = urllib.parse.parse_qs(parsed.query)
                thread_id = _clean((qs.get("thread_id") or [""])[0], 25)
                account = _clean((qs.get("account") or [""])[0], 40)
                # E81: last-week bodies are cached in SQLite (warmed by
                # mail_sync, staleness = mail_threads.last_date token) —
                # a hit skips the ~3s live IMAP fetch. ?fresh=1 forces live.
                mc.validate_thread_id(thread_id)
                conn = db.connect()
                try:
                    if (qs.get("fresh") or ["0"])[0] != "1":
                        hit = mail_cache.cached(conn, account, thread_id)
                        if hit:
                            self._json(200, {"ok": True, "cached": True, **hit})
                            return
                    out = mail_actions.fetch_thread(thread_id, account,
                                                    with_labels=True)
                    try:
                        mail_cache.store(conn, account, thread_id, out)
                    except Exception:
                        pass
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return
        super().do_GET()

    def _origin_ok(self) -> bool:
        """False when a browser is posting here from someone else's page.

        Same-origin fetches from the board send an Origin (or at least a
        Referer) matching the host the page was served from, so comparing
        against the request's own Host header self-configures for every name
        Casey reaches the board by. The extra allowlist covers the tunnel, in
        case cloudflared is ever configured to rewrite Host.
        """
        source = self.headers.get("Origin") or self.headers.get("Referer")
        if not source:
            return True  # curl, the MCP server, other non-browser clients
        try:
            host = (urllib.parse.urlsplit(source).hostname or "").lower()
        except ValueError:
            return False
        if not host:
            return False  # includes Origin: null (sandboxed iframe)
        if source.lower().startswith("chrome-extension://"):
            if not QUICKTODO_EXT_ID or host != QUICKTODO_EXT_ID:
                return False
            import dev_pairing
            return dev_pairing.verify(self.headers.get("X-GC-Dev-Code"))
        if host in ALLOWED_ORIGIN_HOSTS:
            return True
        own = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return bool(own) and host == own

    def do_POST(self):
        route = self.path.split("?")[0]

        if not self._origin_ok():
            print(f"BLOCKED cross-site POST {route} from "
                  f"{self.headers.get('Origin') or self.headers.get('Referer')}",
                  file=sys.stderr, flush=True)
            self._json(403, {"ok": False, "error": "cross-site request blocked"})
            return

        if route in _RETIRED_POST:
            self._json(410, {"ok": False, "error": _RETIRED_MSG})
            return

        if route == "/diag":
            # E206: the phone reports its own viewport numbers.
            #
            # The bottom-gap bug (E187/E196/E199/E200/E202/E203) has been
            # "fixed" six times and keeps coming back, because it only happens
            # in the installed iOS app and uitest.py's headless Chrome passes
            # every check. This endpoint is the missing half: the real device
            # posts what it actually measured, so the seventh fix can be made
            # from evidence instead of inference. Diagnostic only — it stores
            # numbers in a log and changes no behaviour.
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 16384:
                    self._json(400, {"ok": False, "error": "bad diag payload size"})
                    return
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                from datetime import datetime
                line = json.dumps({"at": datetime.now().isoformat(timespec="seconds"),
                                   "ua": self.headers.get("User-Agent", "")[:200],
                                   "diag": payload}, ensure_ascii=False)
                logs = os.path.join(ROOT, "logs")
                os.makedirs(logs, exist_ok=True)
                with open(os.path.join(logs, "viewport-diag.log"), "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                print(f"diag: viewport report received ({len(line)}B)", flush=True)
                self._json(200, {"ok": True})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return

        if route in ("/upload", "/diag") and not self._gate("POST", route, {}):
            return

        if route == "/upload":
            # Raw-body image upload from /upload.html (LAN only, like the
            # whole board). Saved under data/uploads/ for Claude to read —
            # e.g. Shortwave screenshots for design inspiration.
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                name = os.path.basename((qs.get("name") or ["upload"])[0])
                ext = os.path.splitext(name)[1].lower()
                if ext not in UPLOAD_EXTS:
                    self._json(400, {"ok": False, "error": f"file type {ext or '(none)'} not accepted"})
                    return
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= MAX_UPLOAD:
                    self._json(400, {"ok": False, "error": f"size must be 1B-{MAX_UPLOAD>>20}MB"})
                    return
                os.makedirs(UPLOAD_DIR, exist_ok=True)
                from datetime import datetime
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
                path = os.path.join(UPLOAD_DIR, f"{stamp}-{safe}")
                remaining, chunks = length, []
                while remaining > 0:
                    chunk = self.rfile.read(min(65536, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                with open(path, "wb") as f:
                    f.write(b"".join(chunks))
                self._json(200, {"ok": True, "path": path, "name": os.path.basename(path)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})
            return

        try:
            cap = (MAX_SEND_BODY if route == "/send" else
                   MAX_DOC_BODY if route == "/context/docs" else MAX_BODY)
            length = min(int(self.headers.get("Content-Length", 0)), cap)
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError as e:
            self._json(400, {"ok": False, "error": f"bad JSON: {e}"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "error": "body must be a JSON object"})
            return
        if not self._gate("POST", route, payload):
            return

        if route == "/connections":
            # E51: write a credential to config/.env (add Gmail, set/clear a
            # key) or test one live. Secrets in, never out. Allowed from
            # anywhere behind Cloudflare Access (Casey's call) — the write path
            # itself never echoes a value and always backs up + chmod 600.
            try:
                import connections
                self._json(200, connections.handle(payload))
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/accounts/order":
            # Dev #42: the dragged account-tab order, saved server-side so it
            # follows Casey across browsers and sessions.
            try:
                import db
                conn = db.connect()
                try:
                    order = db.acct_order_set(conn, payload.get("order"))
                finally:
                    conn.close()
                self._json(200, {"ok": True, "order": order})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/agent/accounts":
            # E258: Casey's per-account switch for agents (human-only in the gate).
            try:
                import agent_gate
                import db
                acct = _clean(payload.get("account"), 40)
                conn = db.connect()
                try:
                    if not _account_known(conn, acct):
                        raise ValueError(f"unknown account {acct!r}")
                    agent_gate.set_account(conn, acct, payload.get("enabled") is True)
                    on = sorted(agent_gate.enabled_accounts(conn))
                finally:
                    conn.close()
                self._json(200, {"ok": True, "enabled": on})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/agent/propose":
            # E260: the Claude desktop app's only write. The gate has already
            # checked that the action is one an agent may take, in a switched-on
            # account; here it is only filed. Nothing runs until Casey approves.
            try:
                import db
                gctx = getattr(self, "_gctx", None)
                if gctx is None or gctx.who != "desktop":
                    raise PermissionError("only the desktop app files proposals")
                conn = db.connect()
                try:
                    if len(db.proposal_list(conn, "pending", limit=PROPOSALS_PENDING_MAX)) \
                            >= PROPOSALS_PENDING_MAX:
                        raise ValueError(f"{PROPOSALS_PENDING_MAX} requests are already "
                                         "waiting for Casey; ask him to clear some first")
                    pid = db.proposal_add(conn, _clean(payload.get("tool"), 60) or "?",
                                          payload.get("account"),
                                          _clean(payload.get("preview"), 600) or "",
                                          payload["path"], payload["body"])
                finally:
                    conn.close()
                self._json(200, {"ok": True, "status": "pending_approval", "id": pid,
                                 "note": "Filed. Nothing has happened yet: it runs only if "
                                         "Casey approves it on the board (expires in 24h)."})
            except PermissionError as e:
                self._json(403, {"ok": False, "error": str(e)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/calendar/invite":
            # E258 item 6: Casey sends (or drops) an invite draft. Human-only
            # in the gate — the one place attendees ever reach Google.
            try:
                import calendar_invite
                import db
                iid = int(payload.get("id") or 0)
                action = _clean(payload.get("action"), 10) or "send"
                conn = db.connect()
                try:
                    if action == "send":
                        out = calendar_invite.send(conn, iid)
                    elif action == "drop":
                        out = {"dropped": calendar_invite.drop(conn, iid)}
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except (ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/private":
            # E258: let a Private thread back in (paired browser only). It
            # reappears on the next sync only if the Private label is gone in
            # Gmail too — otherwise the next scan latches it again.
            try:
                import db
                import private
                acct = _clean(payload.get("account"), 40)
                tid = _clean(payload.get("thread_id"), 25)
                if not _THRID_RE.match(tid):
                    raise ValueError("bad thread_id")
                conn = db.connect()
                try:
                    was = private.unmark(conn, acct, tid)
                finally:
                    conn.close()
                private._FAST["at"] = 0.0
                self._json(200 if was else 404, {"ok": was})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/agent/proposals/decide":
            # E260: Casey's click (human-only in the gate). Approving runs the
            # request as an ordinary agent call over loopback, so every E258
            # check (switches, Private, limits, lockout, audit) applies again
            # at the moment it runs, not only when it was filed.
            try:
                import agent_gate
                import db
                pid = int(payload.get("id"))
                decision = payload.get("decision")
                if decision not in ("approve", "decline"):
                    raise ValueError("decision must be approve or decline")
                conn = db.connect()
                try:
                    prop = db.proposal_get(conn, pid)
                    if not prop:
                        raise ValueError(f"no request {pid}")
                    if decision == "approve" and not agent_gate.load_key():
                        raise ValueError("approved requests run as the agent, and there is no "
                                         "agent key: run `.venv/bin/python sweeper/agent_gate.py "
                                         "--new-key` on the host first")
                    if not db.proposal_claim(conn, pid, "approved" if decision == "approve"
                                             else "declined"):
                        raise ValueError(f"request {pid} is already {prop['status']}")
                    if decision == "decline":
                        self._json(200, {"ok": True, "id": pid, "status": "declined"})
                        return
                    ok, result = _run_as_agent(prop["path"], prop["body"],
                                               "approved:" + prop["tool"])
                    db.proposal_result(conn, pid, ok, result)
                finally:
                    conn.close()
                self._json(200, {"ok": ok, "id": pid,
                                 "status": "approved" if ok else "failed", "result": result,
                                 **({} if ok else {"error": str((result or {}).get("error")
                                                                or "the action failed")})})
            except (TypeError, ValueError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/agent/reset":
            # E258: lift an agent lockout after reviewing the audit (human-only).
            try:
                import agent_gate
                import db
                conn = db.connect()
                try:
                    was = agent_gate.reset_lock(conn)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "was_locked": was})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/dismiss":
            try:
                import feedback
                row = feedback.record_dismissed(payload)
                self._json(200, {"ok": True, **row})
            except (ValueError, KeyError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/draft":
            try:
                import draft_reply  # lazy: pulls in anthropic
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                # E258: an agent's draft is marked (recipients come from the
                # thread here, so nobody on it is new)
                agent = {"new": []} if self._is_agent() else None
                out = draft_reply.draft_reply(thread_id, account, agent=agent)
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/draft/assist":
            # 2026-10-06: the composer's ✨ — draft or rewrite from Casey's
            # prompt, optionally reading his history with these recipients.
            # Text only; not an agent route (agent_gate allowlists exclude it).
            try:
                import draft_reply
                account = _clean(payload.get("account"), 40)
                import db
                conn = db.connect()
                try:
                    if not account or not _account_known(conn, account):
                        raise ValueError("known account required")
                finally:
                    conn.close()
                out = draft_reply.assist(
                    account, str(payload.get("instruction") or "")[:2000],
                    draft=str(payload.get("draft") or "")[:20000],
                    thread_id=_clean(payload.get("thread_id"), 25) or None,
                    to=_clean(payload.get("to"), 500), cc=_clean(payload.get("cc"), 500),
                    subject=_clean(payload.get("subject"), 300),
                    use_history=payload.get("history") is True)
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif route == "/reply_options":
            # E157: 2-3 distinct reply options for a thread — pure text back
            # to the composer, nothing written to any mailbox. On-demand
            # only (Casey is evaluating before any automation).
            try:
                import draft_reply  # lazy: pulls in anthropic
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                out = draft_reply.generate_options(thread_id, account)
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/draft/preview":
            try:
                import draft_reply
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                out = draft_reply.generate_draft(thread_id, account)
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/draft/save":
            try:
                import draft_reply
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                body = str(payload.get("body") or "")[:20000]
                to = payload.get("to")
                cc = payload.get("cc")
                subject = payload.get("subject")
                agent = self._agent_draft(account, to, cc, payload.get("bcc"))
                out = draft_reply.save_draft(
                    thread_id, account, body,
                    to=_clean(to, 500) if to else None,
                    cc=_clean(cc, 500) if cc else None,
                    subject=_clean(subject, 300) if subject else None,
                    bcc=_clean(payload.get("bcc"), 500) or None,
                    agent=agent,
                )
                self._json(200, {"ok": True, **out, **self._agent_note(agent)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route in ("/draft/replace", "/draft/discard"):
            # E224 (Dev #19, Casey 2026-09-27: "it's ok to delete and resave"):
            # an edited draft is saved as a new one and the old copy goes to
            # Gmail Trash; after a draft is sent from GC, its copy goes too.
            # Drafts folder only, checked by Gmail message id, recoverable. Human click
            # only — not an Ask tool and not in gc_client.ALLOWED.
            try:
                account = _clean(payload.get("account"), 40)
                uid = _clean(payload.get("draft_uid"), 12)
                mid = _clean(payload.get("gm_msgid"), 25)
                if route == "/draft/discard":
                    import mail_actions
                    out = mail_actions.trash_draft(account, uid, mid)
                else:
                    import draft_reply
                    out = draft_reply.replace_draft(
                        account, uid, mid, str(payload.get("body") or "")[:20000],
                        to=_clean(payload.get("to"), 500),
                        subject=_clean(payload.get("subject"), 300),
                        cc=_clean(payload.get("cc"), 500) or None,
                        bcc=_clean(payload.get("bcc"), 500) or None,
                        in_reply_to=_clean(payload.get("in_reply_to"), 300),
                        references=_clean(payload.get("references"), 2000))
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif route == "/draft/compose":
            try:
                import draft_reply
                account = _clean(payload.get("account"), 40)
                agent = self._agent_draft(account, payload.get("to"), payload.get("cc"),
                                          payload.get("bcc"))
                out = draft_reply.compose_draft(
                    account,
                    _clean(payload.get("to"), 500),
                    _clean(payload.get("subject"), 300),
                    str(payload.get("body") or "")[:20000],
                    cc=_clean(payload.get("cc"), 500) if payload.get("cc") else None,
                    bcc=_clean(payload.get("bcc"), 500) or None,
                    agent=agent,
                )
                self._json(200, {"ok": True, **out, **self._agent_note(agent)})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/projects":
            # Projects: every write is a click on the board — not an Ask tool,
            # and in gc_client.DENIED (agents creating or filing into projects
            # needs its own decision, §6 2026-09-29). Nothing here touches Gmail.
            try:
                import db, projects
                a = _clean(payload.get("action"), 20)
                pid = int(payload.get("id", 0) or 0)
                conn = db.connect()
                try:
                    if a == "create":
                        out = {"id": projects.create(conn, payload.get("name"), payload.get("color") or None,
                                                     payload.get("description"), payload.get("target_date") or None)}
                    elif a == "update":
                        f = payload.get("fields") or {}
                        out = {"project": projects.update(conn, pid, f.get("name"), f.get("color"),
                                                          f.get("description"), f.get("target_date"))}
                    elif a == "pin":
                        out = {"project": projects.pin(conn, pid, payload.get("on") is True)}
                    elif a == "complete":
                        out = {"project": projects.complete(conn, pid)}
                    elif a == "reopen":
                        out = {"project": projects.reopen(conn, pid)}
                    elif a == "delete":
                        out = projects.delete(conn, pid)
                    elif a == "add":
                        out = {"item": projects.add_item(conn, pid, _clean(payload.get("kind"), 10),
                                                         _clean(payload.get("account"), 40) or None,
                                                         _clean(payload.get("ref"), 300),
                                                         _clean(payload.get("title"), 300) or None)}
                    elif a == "remove":
                        out = {"removed": projects.remove_item(conn, pid, int(payload.get("item_id", 0) or 0))}
                    elif a == "note-add":
                        out = {"id": projects.note_add(conn, pid or None, payload.get("title"),
                                                       str(payload.get("body") or ""))}
                    elif a == "note-update":
                        out = {"note": projects.note_update(conn, int(payload.get("note_id", 0) or 0),
                                                            payload.get("title"), payload.get("body"))}
                    elif a == "decline":
                        # step 6: ✕ "not this one", or stop suggesting a person
                        out = projects.decline(conn, pid, _clean(payload.get("account"), 40) or None,
                                               _clean(payload.get("thread_id"), 40) or None,
                                               _clean(payload.get("person"), 120) or None)
                    elif a == "note-move":
                        out = {"note": projects.note_move(conn, int(payload.get("note_id", 0) or 0), pid or None)}
                    elif a == "note-delete":
                        out = {"deleted": projects.note_delete(conn, int(payload.get("note_id", 0) or 0))}
                    else:
                        raise ValueError(f"unknown action {a!r}")
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif route == "/calendar/rsvp":
            # §6 Calendar RSVP (Casey 2026-09-27, Dev #16): Yes / Maybe / No on
            # an invitation in one thread — Casey's own response only, nothing
            # else about the event, audited. Human click only: in
            # gc_client.DENIED and not an Ask tool.
            try:
                import calendar_rsvp
                out = calendar_rsvp.rsvp(
                    _clean(payload.get("account"), 40),
                    _clean(payload.get("thread_id"), 25),
                    _clean(payload.get("response"), 10))
                self._json(200, {"ok": True, **out})
            except (ValueError, PermissionError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif route == "/calendar":
            try:
                import calendar_writer
                # E258 item 6: attendees are a PROPOSAL (an invite draft). The
                # event is created without them, exactly as before; Google
                # emails nobody until Casey clicks Send invites.
                attendees = None
                if payload.get("attendees"):
                    import calendar_invite
                    attendees = calendar_invite.clean_attendees(payload.get("attendees"))
                out = calendar_writer.create(
                    _clean(payload.get("account"), 40),
                    _clean(payload.get("summary"), 300),
                    _clean(payload.get("start"), 40),
                    end=_clean(payload.get("end"), 40) if payload.get("end") else None,
                    description=str(payload.get("description") or "")[:2000] or None,
                    location=_clean(payload.get("location"), 300) if payload.get("location") else None,
                    all_day=bool(payload.get("all_day")),
                )
                if attendees:
                    import agent_gate
                    import calendar_invite
                    import db
                    conn = db.connect()
                    try:
                        new = agent_gate.new_recipients(conn, out["account"], ",".join(attendees))
                        iid = calendar_invite.propose(
                            conn, out["account"], out, attendees, new,
                            "agent" if self._is_agent() else "board")
                    finally:
                        conn.close()
                    out["invite_draft"] = {
                        "id": iid, "attendees": attendees, "new": new,
                        "note": "Event created WITHOUT attendees. Nobody is invited until "
                                "Casey clicks Send invites (Settings → Agents)."}
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/daynote":
            # E69: save the day's note. Casey-authored free text — length
            # capped in db, newlines preserved (no _clean on the body).
            try:
                import db
                date = _clean(payload.get("date"), 10)
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", date or ""):
                    raise ValueError("bad date")
                conn = db.connect()
                try:
                    db.set_day_note(conn, date, str(payload.get("text") or ""))
                finally:
                    conn.close()
                self._json(200, {"ok": True})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/braindump":
            # E134: add / edit / archive / mark-promoted an idea. Casey-authored
            # free text — newlines preserved, bounded length only.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if action == "add":
                        text = str(payload.get("text") or "").strip()[:8000]
                        if not text:
                            raise ValueError("empty idea")
                        bid = db.braindump_add(conn, text)
                        self._json(200, {"ok": True, "id": bid})
                    elif action == "update":
                        ok = db.braindump_update(
                            conn, int(payload.get("id", 0)),
                            text=(str(payload.get("text"))[:8000]
                                  if payload.get("text") is not None else None),
                            status=_clean(payload.get("status"), 10) or None,
                            promoted_ref=_clean(payload.get("promoted_ref"), 80) or None)
                        self._json(200 if ok else 404, {"ok": ok})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/push":
            # E188: subscribe this browser, forget a device, set which channel
            # an alert uses, or fire a test. Sends go only to rows created
            # here — the board never invents an endpoint.
            try:
                import db, push
                action = _clean(payload.get("action"), 12)
                conn = db.connect()
                try:
                    if action == "subscribe":
                        sub = payload.get("subscription")
                        if not isinstance(sub, dict):
                            raise ValueError("subscription must be an object")
                        sid = db.push_sub_add(conn, payload.get("label"), sub)
                        self._json(200, {"ok": True, "id": sid})
                    elif action == "forget":
                        ok = db.push_sub_delete(conn, int(payload.get("id", 0)))
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action == "prefs":
                        kind = _clean(payload.get("kind"), 10)
                        db.notify_pref_set(conn, kind,
                                           bool(payload.get("imessage")), bool(payload.get("push")))
                        self._json(200, {"ok": True, "prefs": db.notify_prefs(conn)})
                    elif action == "test":
                        out = push.send("Grand Central",
                                        "Test notification — this is how alerts will look.",
                                        url="/", tag="test")
                        self._json(200, {"ok": True, **out})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/dev":
            # E144: add / edit / archive a dev request. Status writes are
            # open to the API on purpose — Claude Code sessions use the same
            # endpoint to mark building/shipped/declined with a note.
            # E162: anything that puts request text in front of the unattended
            # worker (add, a text edit, or a status set back to 'open') needs
            # the device pairing code — see sweeper/dev_pairing.py.
            try:
                import db
                import dev_pairing
                action = _clean(payload.get("action"), 10)
                status = _clean(payload.get("status"), 10) or None
                account = _clean(payload.get("account"), 40)
                gated = (action in ("add", "check", "newcode", "answer", "triage")
                         or (action == "update" and (payload.get("text") is not None
                                                     or status == "open")))
                if gated and action == "update" and payload.get("text") is None:
                    # A session handing a request it picked up back to Casey
                    # with a question (building -> open) re-queues text that
                    # was already paired; only reopening an answered one is gated.
                    c0 = db.connect()
                    try:
                        cur = c0.execute(
                            "SELECT status FROM dev_requests WHERE id = ? AND account = ?",
                            (int(payload.get("id", 0) or 0), account)).fetchone()
                    finally:
                        c0.close()
                    gated = not (cur and cur[0] == "building")
                if gated and not dev_pairing.verify(self.headers.get("X-GC-Dev-Code")):
                    print(f"BLOCKED unpaired POST /dev action={action!r}",
                          file=sys.stderr, flush=True)
                    self._json(403, {"ok": False, "pairing_required": True,
                                     "error": ("this device isn't paired for dev requests — enter the pairing code"
                                               if dev_pairing.is_configured() else
                                               "no dev pairing code is set yet — run sweeper/dev_pairing.py --new on the host")})
                    return
                if action == "check":
                    self._json(200, {"ok": True})
                    return
                if action == "newcode":
                    # E189: a paired device mints the next code and is shown it
                    # once, so a second Mac or phone can be paired without the
                    # terminal. Unpaired callers never reach here (gated above).
                    code = dev_pairing.new_code()
                    print("dev pairing code rotated by a paired device", file=sys.stderr, flush=True)
                    self._json(200, {"ok": True, "code": code})
                    return
                conn = db.connect()
                try:
                    if action == "answer":
                        # the worker asked; Casey's paired answer re-queues it
                        if not account or not _account_known(conn, account):
                            raise ValueError("known account required")
                        db.dev_answer_question(conn, int(payload.get("id", 0) or 0), account,
                                               str(payload.get("answer") or ""))
                        self._json(200, {"ok": True})
                        return
                    if action == "add":
                        text = str(payload.get("text") or "").strip()[:8000]
                        if not text:
                            raise ValueError("empty request")
                        if not account or not _account_known(conn, account):
                            raise ValueError("known account required")
                        rid = db.dev_add(conn, text, account)
                        self._json(200, {"ok": True, "id": rid,
                                         "account": account})
                    elif action == "triage":
                        # Casey relabels a card (paired only): its priority, or
                        # "build" to clear a card triage held back. Text and
                        # status are untouched; his label sticks until the text
                        # changes. The worker's own rules still apply to it.
                        if not account or not _account_known(conn, account):
                            raise ValueError("known account required")
                        rid = int(payload.get("id", 0) or 0)
                        row = conn.execute("SELECT text FROM dev_requests WHERE id = ? AND account = ?",
                                           (rid, account)).fetchone()
                        if row is None:
                            raise ValueError("no such request")
                        verdict = _clean(payload.get("verdict"), 10) or None
                        if verdict not in (None, "build", "session"):
                            raise ValueError("verdict can only be set to build or session")
                        size = _clean(payload.get("size"), 2) or None
                        if verdict == "build" and size is None:
                            cur = conn.execute("SELECT size FROM dev_requests WHERE id = ?", (rid,)).fetchone()
                            if cur and cur[0] == "l":
                                size = "m"      # "build it" means build it, even if it looked large
                        db.dev_set_triage(conn, rid, priority=_clean(payload.get("priority"), 3) or None,
                                          size=size, verdict=verdict,
                                          note=("Set by you" if verdict or payload.get("priority") else None),
                                          text_hash=db.dev_text_hash(row[0]), by="casey")
                        self._json(200, {"ok": True})
                    elif action == "update":
                        if not account or not _account_known(conn, account):
                            raise ValueError("known account required")
                        prev = conn.execute("SELECT status FROM dev_requests WHERE id = ? AND account = ?",
                                            (int(payload.get("id", 0) or 0), account)).fetchone()
                        ok = db.dev_update(
                            conn, int(payload.get("id", 0)), account,
                            text=(str(payload.get("text"))[:8000]
                                  if payload.get("text") is not None else None),
                            status=status,
                            note=(str(payload.get("note"))[:2000]
                                  if payload.get("note") is not None else None))
                        if ok and status == "open" and prev and prev[0] == "building":
                            # The worker handed it back (a denied permission, or
                            # more than it should do alone): don't hand it the
                            # same card again next run — it waits for Casey.
                            db.dev_set_triage(conn, int(payload.get("id", 0)), verdict="session",
                                              note="Handed back by the worker — needs a session with you.",
                                              by="claude")
                        self._json(200 if ok else 404, {"ok": ok})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/classify":
            # E209: correct a field, edit the team roster, or flip an
            # account's Jev switch. The switch can't turn on without a key;
            # agents can't reach this route (not in gc_client.ALLOWED).
            try:
                import db
                action = _clean(payload.get("action"), 20)
                conn = db.connect()
                try:
                    if action == "correct":
                        account = _clean(payload.get("account"), 40)
                        if not account or not _account_known(conn, account):
                            raise ValueError("known account required")
                        out = db.class_correct(conn, account, _clean(payload.get("thread_id"), 40),
                                               _clean(payload.get("field"), 20),
                                               _clean(payload.get("value"), 40))
                        self._json(200, {"ok": True, "classification": out})
                        return
                    if action in ("service-rename", "service-merge", "service-off"):
                        # E253: Casey's edits to his own services. Only names,
                        # bundles and on/off — nothing here touches Gmail.
                        import services
                        key = _clean(payload.get("key"), 120)
                        if action == "service-rename":
                            services.edit(conn, key, name=_clean(payload.get("name"), 60))
                        elif action == "service-merge":
                            services.edit(conn, key, merged_into=_clean(payload.get("into"), 120))
                        else:
                            services.edit(conn, key, off=payload.get("off") is True)
                        self._json(200, {"ok": True})
                        return
                    if action == "person-add":
                        db.class_person_set(conn, _clean(payload.get("email"), 120),
                                            _clean(payload.get("name"), 80),
                                            _clean(payload.get("group"), 60) or None,
                                            payload.get("teammate") is not False,
                                            also=payload.get("also") is True,
                                            from_grp=_clean(payload.get("from"), 60) or None)
                    elif action == "person-mate":
                        db.class_person_teammate(conn, _clean(payload.get("email"), 120),
                                                 payload.get("teammate") is True)
                    elif action == "person-leave":
                        # 2026-09-30: out of one group; their other groups stay
                        db.class_person_leave(conn, _clean(payload.get("email"), 120),
                                              _clean(payload.get("group"), 60))
                    elif action == "group-add":
                        db.class_group_set(conn, _clean(payload.get("domain"), 120),
                                           _clean(payload.get("group"), 60))
                    elif action in ("person-remove", "group-remove"):
                        db.class_roster_remove(conn, action.split("-")[0],
                                               _clean(payload.get("key"), 120))
                    elif action == "jev":
                        from common import load_env
                        from sources import gmail_reader
                        account = _clean(payload.get("account"), 40)
                        if account not in gmail_reader.scan_accounts(load_env()):
                            raise ValueError("known account required")
                        on = payload.get("on") is True
                        if on and not (load_env().get("JEV_API_KEY") or "").strip():
                            raise ValueError("add a Jev API key in Connections first")
                        db.class_set_jev(conn, account, on)
                    else:
                        raise ValueError(f"unknown action {action!r}")
                    if action != "jev":
                        # a roster change re-classifies now, not at the next
                        # mail_sync — the pane shows the new answer at once
                        from common import load_env
                        from sources import gmail_reader
                        import mailclass
                        env = load_env()
                        known = [r[0] for r in conn.execute("SELECT DISTINCT account FROM mail_class")]
                        mailclass.run(conn, env, known or list(gmail_reader.scan_accounts(env)),
                                      log=lambda *_: None)
                    self._json(200, {"ok": True, **db.class_roster(conn)})
                finally:
                    conn.close()
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/contacts":
            # Dev #32: edit a contact — name, notes, or keep out of typeahead
            try:
                import db
                conn = db.connect()
                try:
                    if "tier" in payload or payload.get("tier_decline"):
                        # Dev #66: Casey tags (or waves off a suggested) VIP / Top prospect
                        import tiers
                        if payload.get("tier_decline"):
                            tiers.decline(conn, _clean(payload.get("email"), 120), _clean(payload.get("tier_decline"), 10))
                            self._json(200, {"ok": True})
                        else:
                            self._json(200, {"ok": True, **tiers.set_tier(
                                conn, _clean(payload.get("email"), 120), _clean(payload.get("tier"), 10) or None)})
                        return
                    em = db.contact_set(conn, _clean(payload.get("email"), 120),
                                        name=None if payload.get("name") is None else _clean(payload.get("name"), 80),
                                        notes=None if payload.get("notes") is None else str(payload.get("notes"))[:2000],
                                        hidden=None if payload.get("hidden") is None else bool(payload.get("hidden")))
                finally:
                    conn.close()
                self._json(200, {"ok": True, "email": em})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/steward":
            # E59: Casey's verdict on a steward suggestion. Approve writes
            # sender_categories (the manual-🤖 store); reject just closes it.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                if action not in ("approve", "reject"):
                    raise ValueError(f"unknown action {action!r}")
                conn = db.connect()
                try:
                    row = db.steward_decide(conn, int(payload.get("id", 0)),
                                            approve=(action == "approve"))
                finally:
                    conn.close()
                if row is None:
                    self._json(404, {"ok": False, "error": "no such pending suggestion"})
                else:
                    self._json(200, {"ok": True, "email": row["email"],
                                     "category": row["category"], "action": action})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/plan":
            # E112: Casey's edit of the day plan (client sends the whole
            # structured plan; planner.save validates and caps).
            try:
                import db
                import planner
                plan = payload.get("plan")
                if not isinstance(plan, dict):
                    raise ValueError("plan object required")
                date = _clean(payload.get("date"), 10) or None
                conn = db.connect()
                try:
                    saved = planner.save(conn, plan, date)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "plan": saved})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/wizard":
            # E111: Casey's verdict on a 🧙 suggestion. Approve creates the
            # todo (source email linked per E106); dismiss just closes it.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                if action not in ("approve", "dismiss"):
                    raise ValueError(f"unknown action {action!r}")
                conn = db.connect()
                try:
                    row = db.wizard_decide(conn, int(payload.get("id", 0)),
                                           approve=(action == "approve"),
                                           account=_clean(payload.get("account"), 40).lower() or None)
                    if (row and action == "approve" and row.get("todo_id")
                            and row.get("account") and row.get("thread_id")):
                        # E113: the approved todo owns its email — archive
                        # it out of the inbox (moved_at keeps it in the feed)
                        try:
                            import mail_actions
                            mail_actions.archive_thread(row["thread_id"],
                                                        row["account"])
                        except Exception:
                            pass
                        db.set_mail_triage(conn, row["thread_id"],
                                           row["account"], {"moved_at": _now_z()})
                finally:
                    conn.close()
                if row is None:
                    self._json(404, {"ok": False, "error": "no such pending suggestion"})
                else:
                    self._json(200, {"ok": True, "action": action,
                                     "todo_id": row.get("todo_id")})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/sender":
            # E34: mark a sender as an app/bot (or clear it). E58 widens it
            # to any bundle category AND to 'people' (rescue a real person
            # the structural rules bundle). Applies on the next mail_sync.
            try:
                import db
                email = _clean(payload.get("email"), 200)
                action = _clean(payload.get("action"), 10) or "set"
                category = _clean(payload.get("category"), 15) or "notification"
                if category not in ("people", "notification", "newsletter",
                                    "promo", "receipt", "calendar"):
                    raise ValueError(f"unknown category {category!r}")
                conn = db.connect()
                try:
                    if action == "clear":
                        db.clear_sender_category(conn, email)
                    else:
                        email = db.set_sender_category(conn, email, category)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "email": email, "action": action,
                                 "category": category})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/mail-action":
            try:
                import mail_actions
                action = _clean(payload.get("action"), 20)
                if action not in {"archive", "trash", "star", "unstar", "label-add", "label-remove"}:
                    raise ValueError(f"unknown action {action!r}")
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                label = _clean(payload.get("label"), 100)
                # dev #69 (2026-10-08): bursts of archives saw occasional 500s
                # from Gmail connection hiccups. Every action here is safe to
                # repeat (archive/star/label/trash leave the same end state),
                # so a transient IMAP/network failure gets one retry.
                def _once(fn, *a, **k):
                    import imaplib, ssl, time as _t
                    try:
                        return fn(*a, **k)
                    except (imaplib.IMAP4.abort, ssl.SSLError, OSError, TimeoutError) as e:
                        self.log_message("RETRY mail-action %s %s/%s after %r", action, account, thread_id, e)
                        _t.sleep(1.5)
                        return fn(*a, **k)
                if action == "archive":
                    out = _once(mail_actions.archive_thread, thread_id, account)
                elif action == "trash":
                    # E7-P3 (§6 amended 2026-07-17): move to Gmail Trash —
                    # recoverable 30d, never purged, human click only (the
                    # command bar's tool list deliberately excludes this).
                    # Local done+dismissal ride along so nothing resurfaces.
                    out = _once(mail_actions.trash_thread, thread_id, account)
                    import db
                    conn = db.connect()
                    try:
                        now = _now_z()
                        db.set_mail_triage(conn, thread_id, account, {"done_at": now})
                        db.record_dismissal(conn, {"thread_id": thread_id,
                                                   "account": account, "ts": now})
                        conn.execute("UPDATE mail_threads SET in_inbox = 0 "
                                     "WHERE account = ? AND thread_id = ?",
                                     (account, thread_id))
                        conn.commit()
                    finally:
                        conn.close()
                elif action == "star":
                    out = _once(mail_actions.star_thread, thread_id, account, star=True)
                elif action == "unstar":
                    out = _once(mail_actions.star_thread, thread_id, account, star=False)
                elif action == "label-add":
                    out = _once(mail_actions.label_thread, thread_id, account, label, add=True)
                else:
                    out = _once(mail_actions.label_thread, thread_id, account, label, add=False)
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self.log_message("ERR mail-action %r", e)   # dev #69: say why, next time
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/send":
            # The system's ONE send path (E3-P4, approved by Casey 2026-07-10,
            # BUILD_SPEC §6 amendment). Guardrails enforced here and in
            # sweeper/send_mail.py: confirm:true required (a replayed
            # /draft/save can never send), explicit recipients required (the
            # list the human saw in the confirm dialog), single thread, no
            # list-shaped fields, ≤10 recipients, every send logged. NOT a
            # command.py tool; excluded from the OpenClaw skill.
            try:
                if payload.get("confirm") is not True:
                    raise ValueError("confirmation required")
                for k in ("thread_id", "account", "to", "cc", "bcc", "subject", "body"):
                    if isinstance(payload.get(k), (list, dict)):
                        raise ValueError(f"{k} must be a plain string")
                to = _clean(payload.get("to"), 500)
                # dev #73: explicit recipients in To, Cc or Bcc — the list the
                # human saw — and at least one of them
                if not (to or _clean(payload.get("cc"), 500) or _clean(payload.get("bcc"), 500)):
                    raise ValueError("explicit recipients are required (To, Cc or Bcc)")
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                if payload.get("new") is True:
                    # E53 (Casey's explicit §6 widening, 2026-07-29): a NEW
                    # email — no thread — through the same confirmed path.
                    # send_new enforces the identical caps + audit. Since
                    # 2026-10-06 (Casey: "a way to schedule a draft email") a new
                    # email can be scheduled too: same queue, fired by mail_sync
                    # through send_new.
                    if payload.get("send_at"):
                        import db
                        if payload.get("attachments"):
                            raise ValueError("attachments send now only — scheduling stores text")
                        body_new = str(payload.get("body") or "")
                        subj_new = _clean(payload.get("subject"), 300)
                        if not subj_new:
                            raise ValueError("subject required for a new email")
                        if not body_new.strip() or len(body_new) > 20000:
                            raise ValueError("write a message (under 20000 chars)")
                        conn = db.connect()
                        try:
                            ob_id = db.add_outbox(conn, "", account, to, body_new, _future_iso(payload.get("send_at")),
                                                  cc_addrs=_clean(payload.get("cc"), 500), bcc_addrs=_clean(payload.get("bcc"), 500),
                                                  subject=subj_new, track=_track_choice(payload),
                                                  body_html=str(payload.get("body_html") or "")[:60000] or None,
                                                  draft_uid=_clean(payload.get("draft_uid"), 20) or None,
                                                  draft_msgid=_clean(payload.get("draft_msgid"), 40) or None)
                        finally:
                            conn.close()
                        self._json(200, {"ok": True, "scheduled": _future_iso(payload.get("send_at")), "id": ob_id, "to": to})
                        return
                    import send_mail
                    out = send_mail.send_new(
                        account,
                        str(payload.get("body") or "")[:20001],
                        to,
                        _clean(payload.get("subject"), 300),
                        cc=_clean(payload.get("cc"), 500) or None,
                        bcc=_clean(payload.get("bcc"), 500) or None,
                        body_html=str(payload.get("body_html") or "")[:60000] or None,
                        attachments=_parse_attachments(payload)
                                    + _fetch_forward_atts(payload, account),
                        track=_track_choice(payload),
                    )
                    self._json(200, {"ok": True, **out})
                    return
                body = str(payload.get("body") or "")[: 20000 + 1]
                cc = _clean(payload.get("cc"), 500)
                bcc = _clean(payload.get("bcc"), 500)
                subject = _clean(payload.get("subject"), 300)
                send_at = payload.get("send_at")
                if send_at:
                    if payload.get("attachments"):
                        raise ValueError("attachments send now only — "
                                         "scheduling stores text")
                    # Scheduled send (E9, approved by Casey 2026-07-17): the
                    # human confirmed these exact recipients in the dialog;
                    # the queue holds them verbatim and mail_sync fires the
                    # row through send_mail.send_reply (same caps + audit).
                    # Recipient/body limits are re-enforced at fire time.
                    import db
                    import mail_common as mc
                    mc.validate_thread_id(thread_id)
                    if len(body) > 20000:
                        raise ValueError("body over 20000 chars")
                    when = _future_iso(send_at)
                    conn = db.connect()
                    try:
                        ob_id = db.add_outbox(conn, thread_id, account, to, body,
                                              when, cc_addrs=cc, bcc_addrs=bcc,
                                              subject=subject, track=_track_choice(payload),
                                              body_html=str(payload.get("body_html") or "")[:60000] or None,
                                              draft_uid=_clean(payload.get("draft_uid"), 20) or None,
                                              draft_msgid=_clean(payload.get("draft_msgid"), 40) or None)
                    finally:
                        conn.close()
                    self._json(200, {"ok": True, "scheduled": when, "id": ob_id,
                                     "to": to})
                    return
                import send_mail
                out = send_mail.send_reply(
                    thread_id, account, body,
                    to=to,
                    attachments=_parse_attachments(payload),
                    cc=cc or None,
                    subject=subject or None,
                    bcc=bcc or None,
                    # E65: optional rich-text alternative (plain part stays
                    # authoritative); scheduled sends remain plain-only.
                    body_html=str(payload.get("body_html") or "")[:60000] or None,
                    track=_track_choice(payload),
                )
                try:   # the stored copy predates this reply — read it fresh next time
                    import db, mail_cache
                    _c = db.connect()
                    try:
                        mail_cache.forget(_c, account, thread_id)
                    finally:
                        _c.close()
                except Exception:
                    pass
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/signatures":
            # 2026-10-08: Casey sets an account's signature (HTML, cleaned on
            # the way in). Paired browser only (agent_gate.HUMAN_ONLY_POST).
            try:
                import db, signatures
                account = _clean(payload.get("account"), 40)
                conn = db.connect()
                try:
                    if not account or not _account_known(conn, account):
                        raise ValueError("known account required")
                    out = signatures.set_sig(conn, account, str(payload.get("html") or ""))
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/watch":
            # E276: Casey's own follow-ups — never an agent route (not in the
            # agent allowlists). Touches no mail.
            try:
                import db, watch
                a = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if a == "add":
                        acct = _clean(payload.get("account"), 40)
                        if not acct or not _account_known(conn, acct):
                            raise ValueError("known account required")
                        out = {"id": watch.add(conn, acct, _clean(payload.get("kind"), 10),
                                               _clean(payload.get("thread_id"), 25),
                                               _clean(payload.get("message_id"), 200) or None,
                                               _clean(payload.get("who"), 120) or None,
                                               _clean(payload.get("who_name"), 80) or None,
                                               str(payload.get("note") or "")[:300] or None,
                                               _clean(payload.get("due"), 10) or None,
                                               _clean(payload.get("subject"), 200) or None,
                                               int(payload.get("todo_id") or 0) or None)}
                    elif a == "done":
                        watch.done(conn, int(payload.get("id") or 0)); out = {}
                    elif a == "edit":
                        watch.edit(conn, int(payload.get("id") or 0),
                                   None if payload.get("note") is None else str(payload.get("note"))[:300],
                                   _clean(payload.get("due"), 10) or None); out = {}
                    elif a == "push":
                        out = {"due": watch.push(conn, int(payload.get("id") or 0), int(payload.get("days") or 2))}
                    elif a == "ask":
                        out = {"ask": watch.set_ask_mode(conn, _clean(payload.get("account"), 40), _clean(payload.get("ask"), 10))}
                    else:
                        raise ValueError(f"unknown action {a!r}")
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/tracking":
            # E269: Casey's per-account tracking switches. Paired browser only
            # (agent_gate.HUMAN_ONLY_POST); touches no mail.
            try:
                import db, tracking
                account = _clean(payload.get("account"), 40)
                conn = db.connect()
                try:
                    if not account or not _account_known(conn, account):
                        raise ValueError("known account required")
                    vals = {k: payload[k] for k in tracking.SIGNALS if isinstance(payload.get(k), bool)}
                    out = tracking.set_settings(conn, account, **vals)
                finally:
                    conn.close()
                self._json(200, {"ok": True, "settings": out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/mail/triage":
            # Pin/snooze/done state for the Mail screen (E3-P3). The Gmail
            # half of "done" (archive) is a separate /mail-action call
            # composed by the client. Snooze is Shortwave-style since E5-P1
            # (§6 amendment, Casey 2026-07-15): it ALSO archives the thread
            # in Gmail now, and mail_sync's wake pass re-inboxes it when the
            # time passes (wake_inbox=1 marks that promise). Unsnooze
            # re-inboxes immediately. Both Gmail writes are best-effort —
            # the local triage state is what must never be lost.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                thread_id = _clean(payload.get("thread_id"), 25)
                account = _clean(payload.get("account"), 40)
                if not _THRID_RE.match(thread_id):
                    raise ValueError("bad thread_id")
                now = _now_z()
                fields = {
                    "pin": {"pinned": 1}, "unpin": {"pinned": 0},
                    "snooze": {"snoozed_until": _future_iso(payload.get("until")),
                               "wake_inbox": 1} if action == "snooze" else None,
                    "unsnooze": {"snoozed_until": None, "wake_inbox": 0},
                    "done": {"done_at": now}, "undone": {"done_at": None},
                    # E108: filed to a project AND moved out of the Inbox —
                    # the archive itself is the client's /mail-action call.
                    "move": {"moved_at": now}, "unmove": {"moved_at": None},
                    # E55: a short sticky note on the thread; empty clears.
                    # Whitespace collapsed by _clean is fine for a one-liner.
                    "note": {"note": _clean(payload.get("note"), 300) or None},
                    # E91: pull an email into Today; value = client's local
                    # date (self-expires like todo my_day), empty clears.
                    "my_day": {"my_day": _clean(payload.get("value"), 10) or None},
                }.get(action)
                if fields is None:
                    raise ValueError(f"unknown action {action!r}")
                gmail_note = None
                conn = db.connect()
                try:
                    if action == "unsnooze":
                        row = conn.execute(
                            "SELECT wake_inbox FROM mail_triage "
                            "WHERE account = ? AND thread_id = ?",
                            (account, thread_id)).fetchone()
                        if row and row["wake_inbox"]:
                            try:
                                import mail_actions
                                mail_actions.unarchive_thread(thread_id, account)
                                conn.execute(
                                    "UPDATE mail_threads SET in_inbox = 1 "
                                    "WHERE account = ? AND thread_id = ?",
                                    (account, thread_id))
                                conn.commit()
                            except Exception as e:
                                gmail_note = f"re-inbox failed: {e}"
                    db.set_mail_triage(conn, thread_id, account, fields)
                    if action == "snooze":
                        try:
                            import mail_actions
                            mail_actions.archive_thread(thread_id, account)
                            # optimistic: correct before the next 3-min sync
                            conn.execute(
                                "UPDATE mail_threads SET in_inbox = 0 "
                                "WHERE account = ? AND thread_id = ?",
                                (account, thread_id))
                            conn.commit()
                        except Exception as e:
                            # a thread trashed elsewhere still gets the local
                            # snooze — anything else is worth surfacing
                            if "not found in All Mail" not in str(e):
                                gmail_note = f"archive failed: {e}"
                    if action == "done":
                        # keep the board rails consistent with the Mail screen
                        db.record_dismissal(conn, {"thread_id": thread_id,
                                                   "account": account, "ts": now})
                finally:
                    conn.close()
                self._json(200, {"ok": True, "gmail": gmail_note})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/outbox":
            # Cancel a scheduled send while it is still pending (E9).
            try:
                import db
                if _clean(payload.get("action"), 10) != "cancel":
                    raise ValueError("only action 'cancel' is supported")
                ob_id = int(payload.get("id", 0))
                conn = db.connect()
                try:
                    ok = db.cancel_outbox(conn, ob_id)
                finally:
                    conn.close()
                self._json(200 if ok else 404,
                           {"ok": ok, "error": None if ok else "not pending"})
            except (ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/mail/bulk":
            # Bundle-level archive/trash (E3-P3, trash added E7-P3): loops
            # the existing single-thread verb with per-id validation,
            # continuing past failures. Deliberately NOT exposed to
            # command.py — the command bar keeps its no-bulk-tool invariant.
            try:
                import db
                import mail_actions
                action = _clean(payload.get("action"), 10)
                if action not in ("archive", "trash"):
                    raise ValueError("only actions 'archive' and 'trash' are supported")
                items = payload.get("items")
                if not isinstance(items, list) or not 1 <= len(items) <= 50:
                    raise ValueError("items must be a list of 1-50 threads")
                now = _now_z()
                results, archived = [], 0
                conn = db.connect()
                try:
                    for it in items:
                        it = it if isinstance(it, dict) else {}
                        thread_id = _clean(it.get("thread_id"), 25)
                        account = _clean(it.get("account"), 40)
                        try:
                            if not _THRID_RE.match(thread_id):
                                raise ValueError("bad thread_id")
                            if action == "trash":
                                mail_actions.trash_thread(thread_id, account)
                            else:
                                mail_actions.archive_thread(thread_id, account)
                            db.set_mail_triage(conn, thread_id, account, {"done_at": now})
                            db.record_dismissal(conn, {"thread_id": thread_id,
                                                       "account": account, "ts": now})
                            # optimistic: correct before the next 3-min sync
                            with conn:
                                conn.execute(
                                    "UPDATE mail_threads SET in_inbox = 0 "
                                    "WHERE account = ? AND thread_id = ?",
                                    (account, thread_id))
                            results.append({"thread_id": thread_id, "ok": True})
                            archived += 1
                        except Exception as e:
                            results.append({"thread_id": thread_id, "ok": False,
                                            "error": str(e)})
                finally:
                    conn.close()
                self._json(200, {"ok": True, "archived": archived, "results": results})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/task":
            try:
                import db
                action = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if action == "add":
                        # account-first: the todo belongs to ONE account —
                        # given, or derived from its spawning email. Never
                        # defaulted; projects/goals are retired.
                        acct = _clean(payload.get("account"), 40).lower() or None
                        if acct and not _account_known(conn, acct):
                            raise ValueError(f"unknown account {acct!r}")
                        tid = db.add_engagement(conn, payload.get("text"),
                                                account=acct,
                                                due=_clean(payload.get("due"), 10) or None,
                                                source=_clean(payload.get("source"), 10) or "manual",
                                                source_ref=_clean(payload.get("source_ref"), 70) or None,
                                                # notes keep their newlines — not _clean()ed
                                                notes=(str(payload.get("notes") or "")[:4000] or None),
                                                # E168: daily|weekdays|weekly|biweekly|monthly|yearly
                                                recur=_clean(payload.get("recur"), 10) or None)
                        self._json(200, {"ok": True, "id": tid})
                    elif action in ("link", "unlink"):
                        # E60: attach/detach a Gmail thread to a todo —
                        # Shortwave-style "promote to Todo" membership.
                        tid = _clean(payload.get("thread_id"), 25)
                        acct = _clean(payload.get("account"), 40)
                        if not _THRID_RE.match(tid):
                            raise ValueError("bad thread_id")
                        ok = db.link_thread(conn, int(payload.get("id", 0)),
                                            acct, tid, add=(action == "link"))
                        if ok:
                            # E204: the todo's GC/Todo/<name> label follows
                            # the membership into Gmail — off the request
                            # thread; a failure leaves label_synced = 0 and
                            # mail_sync's reconcile pass retries it.
                            _todo_label_async(int(payload.get("id", 0)), acct, tid,
                                              action == "link")
                        if ok and action == "link":
                            # E113: email→todo stays COUPLED and the inbox
                            # unclutters — archive in Gmail, moved_at (E108)
                            # keeps it in the feed under its todo/project.
                            # Best-effort: already-archived is success.
                            try:
                                import mail_actions
                                mail_actions.archive_thread(tid, acct)
                            except Exception:
                                pass
                            db.set_mail_triage(conn, tid, acct,
                                               {"moved_at": _now_z()})
                        if ok and action == "unlink" and payload.get("return_inbox"):
                            # E115: "return to inbox" undoes the E113 couple —
                            # back into Gmail's INBOX, moved_at cleared so the
                            # inbox scope owns it again. Gmail write is
                            # best-effort; the local state is what must hold.
                            try:
                                import mail_actions
                                mail_actions.unarchive_thread(tid, acct)
                            except Exception:
                                pass
                            db.set_mail_triage(conn, tid, acct,
                                               {"moved_at": None})
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action == "snooze":
                        # todo snooze (E10): local-only, sections the todo
                        # under Snoozed until the time passes
                        ok = db.update_engagement(conn, int(payload.get("id", 0)),
                                                  {"snoozed_until": _future_iso(payload.get("until"))})
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action == "unsnooze":
                        ok = db.update_engagement(conn, int(payload.get("id", 0)),
                                                  {"snoozed_until": ""})
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action == "update":
                        fields = payload.get("fields")
                        fields = fields if isinstance(fields, dict) else {}
                        clean = {}
                        if "text" in fields:
                            clean["text"] = _clean(fields.get("text"), 300)
                        if "due" in fields:
                            clean["due"] = _clean(fields.get("due"), 10)
                        if "account" in fields:
                            clean["account"] = _clean(fields.get("account"), 40).lower()
                            if not _account_known(conn, clean["account"]):
                                raise ValueError(f"unknown account {clean['account']!r}")
                        if "starred" in fields:
                            clean["starred"] = fields.get("starred")
                        if "my_day" in fields:
                            # E15: '' clears; the client sends today's date to add
                            clean["my_day"] = _clean(fields.get("my_day"), 10)
                        if "notes" in fields:
                            # E30: freeform detail — keep newlines, bound length
                            clean["notes"] = str(fields.get("notes") or "")[:4000]
                        if "labels" in fields:
                            # E52: labels on todos — a JSON list; [] clears.
                            # db.update_engagement validates/serializes.
                            clean["labels"] = fields.get("labels") if isinstance(fields.get("labels"), list) else []
                        if "priority" in fields:
                            # E73: high|medium|low; else clears (validated in db).
                            clean["priority"] = _clean(fields.get("priority"), 6)
                        if "recur" in fields:
                            # E168: a repeat rule, or '' to stop repeating.
                            clean["recur"] = _clean(fields.get("recur"), 10)
                        ok = db.update_engagement(conn, int(payload.get("id", 0)), clean)
                        if ok and clean.get("text"):
                            _todo_rename_async(int(payload.get("id", 0)))   # E204
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action in ("done", "drop", "reopen"):
                        state = {"done": "done", "drop": "dropped", "reopen": "open"}[action]
                        ids = payload.get("ids")
                        if ids is not None:
                            # bulk (E5-P3): explicit id list, 1-50, one txn
                            if not isinstance(ids, list):
                                raise ValueError("ids must be a list")
                            # E168: completing a repeating todo opens its next one
                            out = db.complete_with_recurrence(conn, ids, state)
                            self._json(200, {"ok": True, "changed": out["changed"],
                                             "spawned": out["spawned"]})
                        else:
                            tid = int(payload.get("id", 0))
                            out = db.complete_with_recurrence(conn, [tid], state)
                            self._json(200 if out["changed"] else 404,
                                       {"ok": bool(out["changed"]), "spawned": out["spawned"]})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/context/docs":
            # E259: Casey pastes or uploads a context document for an account
            # or a project. Board clicks only: not an Ask tool, and denied to
            # agents in gc_client. Nothing here touches Gmail.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if action == "add":
                        scope = db._doc_scope(_clean(payload.get("scope"), 60))
                        kind, _, ref = scope.partition(":")
                        if kind == "account" and not _account_known(conn, ref):
                            raise ValueError("unknown account")
                        if kind == "project":
                            import projects
                            projects._get(conn, int(ref))
                        out = {"id": db.add_context_doc(conn, scope, payload.get("title"),
                                                        str(payload.get("body") or ""),
                                                        _clean(payload.get("filename"), 200) or None)}
                    elif action == "update":
                        f = payload.get("fields")
                        f = {k: v for k, v in (f if isinstance(f, dict) else {}).items()
                             if k in ("title", "body", "active")}
                        out = {"updated": db.update_context_doc(conn, int(payload.get("id", 0) or 0), f)}
                    elif action == "delete":
                        out = {"deleted": db.delete_context_doc(conn, int(payload.get("id", 0) or 0))}
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
                self._json(200, {"ok": True, **out})
            except (ValueError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/context":
            # E19: add/update/toggle/delete one context entry. Data-plane
            # only — nothing here changes classifier or send behavior;
            # consumers of the corpus are wired in deliberately, later.
            try:
                import db
                action = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if action == "add":
                        by_agent = getattr(self, "_gctx", None) is not None \
                            and self._gctx.who.startswith("agent")
                        nid = db.add_context(conn, _clean(payload.get("scope"), 80),
                                             _clean(payload.get("kind"), 12) or "note",
                                             payload.get("text"),
                                             source="agent" if by_agent else None)
                        self._json(200, {"ok": True, "id": nid, **(
                            {"held": True, "note": "saved switched off — it counts once "
                             "Casey approves it in Settings → Context"} if by_agent else {})})
                    elif action == "update":
                        fields = payload.get("fields")
                        ok = db.update_context(conn, int(payload.get("id", 0)),
                                               fields if isinstance(fields, dict) else {})
                        self._json(200 if ok else 404, {"ok": ok})
                    elif action == "delete":
                        ok = db.delete_context(conn, int(payload.get("id", 0)))
                        self._json(200 if ok else 404, {"ok": ok})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/decision":
            try:
                import db
                action = _clean(payload.get("action"), 10)
                conn = db.connect()
                try:
                    if action == "add":
                        did = db.add_decision(conn, payload.get("text"),
                                              blocking=payload.get("blocking"))
                        self._json(200, {"ok": True, "id": did})
                    elif action == "decide":
                        ok = db.decide_decision(conn, int(payload.get("id", 0)),
                                                payload.get("outcome"))
                        self._json(200 if ok else 404, {"ok": ok})
                    else:
                        raise ValueError(f"unknown action {action!r}")
                finally:
                    conn.close()
            except (ValueError, KeyError, TypeError) as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/mail/refresh":
            # E204 (Casey: "always able to see the latest emails… an easy way
            # to refresh"): run a quick mail_sync now — inbox + todo labels,
            # no planner/notify passes. mail_sync's own lock refuses overlap;
            # a sync already underway just means "wait for that one".
            try:
                st = _mail_sync_status()
                if st.get("running"):
                    self._json(200, {"ok": True, "already": True, "sync": st})
                    return
                venv_python = os.path.join(ROOT, ".venv", "bin", "python")
                subprocess.Popen(
                    [venv_python, os.path.join(ROOT, "sweeper", "mail_sync.py"), "--quick"],
                    cwd=os.path.join(ROOT, "sweeper"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self._json(200, {"ok": True, "started": True, "sync": st})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/sweep":
            # Fire a sweep now instead of waiting for the top of the hour.
            # sweep.py's own lockfile refuses to overlap the hourly run; the
            # board picks the fresh JSON up on its normal poll (or reload).
            try:
                lock = os.path.join(ROOT, "data", "sweep.lock")
                if os.path.exists(lock) and (time.time() - os.path.getmtime(lock)) < 15 * 60:
                    self._json(409, {"ok": False, "error": "a sweep is already running"})
                    return
                venv_python = os.path.join(ROOT, ".venv", "bin", "python")
                subprocess.Popen(
                    [venv_python, os.path.join(ROOT, "sweeper", "sweep.py")],
                    cwd=os.path.join(ROOT, "sweeper"),
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
                self._json(200, {"ok": True, "eta_s": 300})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        elif route == "/command":
            try:
                import command as command_mod  # lazy: pulls in anthropic
                text = str(payload.get("text") or "").strip()[:2000]
                if not text:
                    raise ValueError("empty command")
                selection = payload.get("selection")
                selection = selection if isinstance(selection, dict) else None
                # E259: the account Casey is looking at, so its context
                # documents ground the answer
                if _clean(payload.get("account"), 40):
                    selection = {**(selection or {}), "account": _clean(payload.get("account"), 40)}
                # E22: the Ask panel replays its transcript so the server
                # stays stateless; bound it so a long chat can't grow the
                # request without limit.
                history = payload.get("history")
                history = history[-12:] if isinstance(history, list) else None
                # E26: Casey picks model + effort in the Ask panel; both are
                # allow-listed inside command.py, which falls back to the
                # defaults on anything unrecognised.
                # E136: persist the QUESTION before the model runs (it can
                # take 80s+, and a dropped tunnel connection used to leave
                # zero trace in history — Casey hit this). The answer lands
                # as a second write when it exists; a crash leaves the
                # question standing as evidence the ask happened.
                chat_id = payload.get("chat_id")
                # Projects step 5: Ask on a project page is fenced to it
                project_id = int(payload.get("project_id") or 0) or None
                if project_id:
                    import db, projects
                    conn = db.connect()
                    try:
                        projects._get(conn, project_id)          # raises on an unknown id
                    finally:
                        conn.close()
                try:
                    import db
                    conn = db.connect()
                    try:
                        if not chat_id:
                            chat_id = db.ask_start_chat(conn, text, project_id)
                        db.ask_add_message(conn, chat_id, "user", text)
                    finally:
                        conn.close()
                except Exception as e:
                    self.log_message("ERR ask-history %r", e)
                out = command_mod.run_command(
                    text, selection=selection, history=history,
                    model=_clean(payload.get("model"), 40) or command_mod.COMMAND_MODEL,
                    effort=_clean(payload.get("effort"), 10) or None,
                    project_id=project_id)
                # Best-effort — a storage failure must never cost Casey the
                # answer he just waited for.
                try:
                    import db
                    conn = db.connect()
                    try:
                        if chat_id:
                            db.ask_add_message(conn, chat_id, "assistant",
                                               out.get("reply", ""),
                                               [a.get("tool") for a in
                                                (out.get("actions_taken") or [])])
                    finally:
                        conn.close()
                except Exception as e:
                    self.log_message("ERR ask-history %r", e)
                self._json(200, {"ok": True, "chat_id": chat_id, **out})
            except ValueError as e:
                self._json(400, {"ok": False, "error": str(e)})
            except Exception as e:
                self._json(500, {"ok": False, "error": repr(e)})

        else:
            self._json(404, {"ok": False, "error": "unknown endpoint"})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True

    def verify_request(self, request, client_address):
        """Refuse non-loopback, non-tailnet peers before any HTTP is parsed."""
        if client_allowed(client_address[0]):
            return True
        peer = client_address[0]
        if peer not in _REFUSED_SEEN:
            _REFUSED_SEEN.add(peer)
            print(f"REFUSED connection from {peer} — not loopback or tailnet. "
                  f"Turn Tailscale on, or set GC_ALLOW_LAN=1 to allow the LAN again.",
                  file=sys.stderr, flush=True)
        return False


def _tailnet_addrs() -> list:
    """The host's own tailnet addresses, for the startup banner only."""
    found = []
    try:
        out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True,
                             timeout=5).stdout
        cgnat = ipaddress.ip_network("100.64.0.0/10")
        for addr in re.findall(r"inet (\d+\.\d+\.\d+\.\d+)", out):
            try:
                if ipaddress.ip_address(addr) in cgnat and addr not in found:
                    found.append(addr)
            except ValueError:
                continue
    except Exception:
        pass
    return found


if __name__ == "__main__":
    import egress  # E258: outbound hosts allowlisted (log-only until enforced)
    egress.install("serve")
    host = socket.gethostname()
    # Still bound to every interface: the guard is verify_request, so the
    # allowlist keeps working when Tailscale's address appears or disappears
    # without needing the service restarted.
    with Server(("0.0.0.0", PORT), Handler) as httpd:
        print(f"Grand Central dashboard serving {DIRECTORY}")
        print(f"  http://localhost:{PORT}/")
        for addr in _tailnet_addrs():
            print(f"  http://{addr}:{PORT}/   (tailnet)")
        if ALLOW_LAN:
            print(f"  http://{host}:{PORT}/   (LAN — GC_ALLOW_LAN is set)")
        else:
            print("  LAN clients refused; loopback and tailnet only "
                  "(GC_ALLOW_LAN=1 to reopen)")
        httpd.serve_forever()
