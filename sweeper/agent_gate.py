"""E258 — who is calling the board, and what they may do (BUILD_SPEC §6, 2026-09-30).

Casey, 2026-09-30: "an LLM will be reading my email and then acting … I want
the protections enforced in the server, because that's the layer I control
regardless of what the model decides." Before this, the only thing between an
agent and POST /send was the MCP client's own deny list: the board itself
accepted `confirm: true` from any local process as if it were Casey's click.

Every request is one of three principals:

  human  — carries the device pairing code (X-GC-Dev-Code, the E162 code a
           paired browser already holds). The only caller that may send,
           trash, RSVP, send invites, write credentials, switch agent
           accounts, approve agent Context or reset a lockout.
  agent  — carries the MCP key (X-GC-Agent-Key, data/agent_key.json, 0600).
           Everything it may do is enumerated in AGENT_GET / AGENT_POST /
           AGENT_ACTIONS below; every request is checked against the account
           switches, rate-limited and audited, and every response is filtered
           to switched-on accounts.
  local  — neither. Keeps today's access EXCEPT the human-only routes, and its
           writes are audited. Honest scope, same as E161/E162: a process
           running as Casey's uid can read config/.env and the database, so the
           real boundary for OpenClaw is its exec allowlist (§6 E258 item 11).

If both headers arrive, the request is treated as the agent — the narrower one.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

try:
    from .common import DATA_DIR
except ImportError:  # run with sweeper/ on sys.path
    from common import DATA_DIR

KEY_PATH = os.environ.get("GC_AGENT_KEY_PATH") or os.path.join(DATA_DIR, "agent_key.json")
# E260: the Claude desktop app's own key. Same header, narrower caller.
DESKTOP_KEY_PATH = (os.environ.get("GC_DESKTOP_KEY_PATH")
                    or os.path.join(DATA_DIR, "agent_key_desktop.json"))

# ---------------------------------------------------------------------------
# Human-only: refused to every caller but a paired browser.
# ---------------------------------------------------------------------------

HUMAN_ONLY_POST = {
    "/send": "sending is Casey's click in a paired browser (§6 E258 item 1)",
    "/outbox": "scheduled sends are a paired browser's call",
    "/mail/bulk": "bulk mail actions are a paired browser's call",
    "/draft/replace": "replacing a draft trashes the old copy",
    "/draft/discard": "discarding a draft is a Trash write",
    "/calendar/rsvp": "an RSVP tells the organizer",
    "/calendar/invite": "sending invites emails the attendees (§6 E258 item 6)",
    "/connections": "credential writes",
    "/signatures": "what goes at the bottom of every email Casey sends is his to set (2026-10-08)",
    "/tracking": "what gets tracked in Casey's outgoing mail is his switch (§6 2026-10-06)",
    "/upload": "writes files to disk",
    "/agent/accounts": "which accounts agents may touch is Casey's switch",
    "/agent/reset": "only Casey lifts an agent lockout",
    "/private": "unmarking a Private thread is Casey's call (§6 E258 item 2)",
    "/agent/proposals/decide": "approving what the desktop app asked for is Casey's click (§6 E260)",
    # E259: a context document grounds Ask and every reply draft, so writing
    # one is as sensitive as approving agent Context
    "/context/docs": "context documents ground Ask and drafts; only Casey writes them (E259)",
}

# Routes that mix safe verbs with human-only ones.
HUMAN_ONLY_ACTIONS = {
    "/mail-action": {"trash", "untrash"},
    # update covers switching an entry on — which is how an agent-written
    # entry gets approved — so it and delete are Casey's.
    "/context": {"update", "delete"},
}

# The Private label may not be added or removed by anyone but a paired
# browser: an injected "remove the Private label" must not re-expose a thread.
PRIVATE_LABEL = "Private"

# ---------------------------------------------------------------------------
# The agent's whole world. Anything not listed here is refused.
# ---------------------------------------------------------------------------

AGENT_GET = {
    "/brief": "the compact digest the MCP starts from",
    "/todos": "todos, filtered to switched-on accounts",
    "/thread": "one thread, in a switched-on account",
    "/mail": "the inbox, filtered to switched-on accounts",
    "/mail/search": "Gmail search, only across switched-on accounts",
    "/context": "Casey's Context notes, filtered to switched-on accounts",
    "/labels": "one account's labels",
    "/calendar": "Casey's calendar, for finding free time (scheduling)",
}

AGENT_POST = {
    "/draft": "Claude writes a reply into Drafts; never sends",
    "/draft/save": "save exact reply text into Drafts; never sends",
    "/draft/compose": "save a new email into Drafts; never sends",
    "/calendar": "create an event on Casey's own calendar; attendees become an invite draft",
    "/task": "todos",
    "/mail/triage": "board triage state for one thread",
    "/mail-action": "archive / star / label one thread",
    "/dismiss": "drop a thread off the board's rails",
    "/context": "a 'remember that…' note, held inactive until Casey approves",
    "/sweep": "kick a sweep (2 a day)",
}

AGENT_ACTIONS = {
    "/mail-action": {"archive", "star", "unstar", "label-add", "label-remove"},
    "/context": {"add"},
    "/task": {"add", "done", "reopen", "drop", "snooze", "unsnooze", "update", "link", "unlink"},
    "/mail/triage": {"pin", "unpin", "snooze", "unsnooze", "done", "undone",
                     "move", "unmove", "note", "my_day"},
}

# ---------------------------------------------------------------------------
# E260: the Claude desktop app (BUILD_SPEC §6, 2026-09-30). Casey: "Option A
# first, one account per read, approve all actions." A caller with the desktop
# key gets every agent rule above (switches, Private, limits, lockout, audit,
# filtering) and three more: every read names ONE account and sees only that
# account's rows; it never acts; its one write is a proposal that runs only
# after Casey approves it from a paired browser, as an ordinary agent call.
# ---------------------------------------------------------------------------

# /calendar is left out: it is one calendar across every account, and each
# desktop read names one account (the brief carries that account's day).
DESKTOP_GET = {**{r: why for r, why in AGENT_GET.items() if r != "/calendar"},
               "/agent/proposals": "the status of its own proposals"}
# Routes a proposal may carry: the agent's writes minus notes and sweeps
# (a note feeds the Ask bar's prompt; a sweep spends tokens on Casey's mail).
PROPOSABLE = {r for r in AGENT_POST if r not in ("/context", "/sweep")}
# BUILD_SPEC §6, 2026-10-06 (Casey: "An agent creating a draft shouldn't
# require an approval", then "Desktop: everything direct"): the desktop app
# now makes those same writes itself, under every agent rule (switches,
# Private, allowed actions, limits, lockout, audit). /agent/propose stays for
# compatibility; nothing requires it any more.
DESKTOP_POST = {**{r: AGENT_POST[r] for r in PROPOSABLE},
                "/agent/propose": "ask Casey to approve one agent action"}
ONE_ACCOUNT_GET = {"/brief", "/todos", "/thread", "/mail", "/mail/search", "/labels",
                   "/context", "/agent/proposals"}

# ---------------------------------------------------------------------------
# Limits (§6 E258 item 9), per rolling window, counted from the audit table so
# a restart does not reset them.
# ---------------------------------------------------------------------------

HOUR, DAY = 3600, 86400
LIMITS = [
    # (bucket, window seconds, max, label)
    ("thread", HOUR, 40, "40 thread reads an hour"),
    ("thread", DAY, 150, "150 thread reads a day"),
    ("search", HOUR, 30, "30 searches an hour"),
    ("write", HOUR, 20, "20 writes an hour"),
    # §6 2026-10-06 (Casey: "there shouldn't be a cap of 10"): drafts never
    # send, so they're outside the hourly write limit; this ceiling only
    # catches a runaway loop
    ("draft", DAY, 200, "200 drafts a day (a runaway-loop ceiling)"),
    ("sweep", DAY, 2, "2 sweeps a day"),
]
BYTES_PER_HOUR = 2 * 1024 * 1024
# §6 2026-10-19 (Casey: "I'm ok with expanding the limit, as long as it's not a
# major issue for our capacity. I'm the only one querying it through my Claude
# instance." then confirmed 10 MB an hour with writes exempt): the desktop app
# may receive 10 MB an hour, and the budget never refuses its writes (a draft
# save returns a few hundred bytes; a busy reading hour shouldn't stop it saving
# the drafts it read for). OpenClaw keeps 2 MB, writes included.
DESKTOP_BYTES_PER_HOUR = 10 * 1024 * 1024
BREAKER_THREADS = (20, 600)   # > 20 distinct threads in 10 minutes
# §6 2026-10-15 (Casey chose "Raise the limit for your Claude session only (for
# example 60 threads in 10 minutes), since you're directing it. OpenClaw stays
# at 20."): the desktop app tripped the breaker drafting ~35 personal MarCom
# notes, reading each person's thread first. Its hourly thread cap rises with
# it, or 60 in 10 minutes could never happen; the daily cap, the lockout
# itself and everything else stay the same for both agents.
DESKTOP_BREAKER_THREADS = (60, 600)
DESKTOP_LIMITS = {("thread", HOUR): (60, "60 thread reads an hour")}
BREAKER_REFUSALS = (3, 300)   # 3 refusals in 5 minutes

_ACCT_RE = re.compile(r"^[a-z0-9_-]{1,40}$")


class Refused(Exception):
    """The gate said no. status is the HTTP code to answer with."""

    def __init__(self, message: str, status: int = 403, ctx=None, **extra):
        super().__init__(message)
        self.status = status
        self.ctx = ctx
        self.extra = extra


# ---------------------------------------------------------------------------
# principals
# ---------------------------------------------------------------------------

def load_key(path: str = KEY_PATH) -> str:
    try:
        with open(path) as f:
            return str(json.load(f).get("key") or "")
    except (OSError, ValueError):
        return ""


def new_key(path: str = KEY_PATH) -> str:
    """Mint the MCP key (0600). The MCP server reads the same file."""
    key = secrets.token_urlsafe(32)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"key": key, "created_at": _now()}, f)
    os.replace(tmp, path)
    return key


def principal(headers) -> str:
    agent_key = headers.get("X-GC-Agent-Key")
    if agent_key:
        desk = load_key(DESKTOP_KEY_PATH)
        if desk and hmac.compare_digest(desk, agent_key):
            return "desktop"
        real = load_key()
        # A wrong key is still an agent — the narrow policy, never a fallback
        # to "local" — so guessing cannot widen access.
        return "agent" if real and hmac.compare_digest(real, agent_key) else "agent-badkey"
    import dev_pairing
    if dev_pairing.verify(headers.get("X-GC-Dev-Code")):
        return "human"
    return "local"


# ---------------------------------------------------------------------------
# account switches
# ---------------------------------------------------------------------------

def enabled_accounts(conn) -> set:
    return {r[0] for r in conn.execute(
        "SELECT account FROM agent_accounts WHERE enabled = 1")}


def set_account(conn, account: str, enabled: bool) -> None:
    if not _ACCT_RE.match(account or ""):
        raise ValueError(f"bad account {account!r}")
    with conn:
        conn.execute(
            "INSERT INTO agent_accounts (account, enabled, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(account) DO UPDATE SET enabled = excluded.enabled, "
            "updated_at = excluded.updated_at",
            (account, 1 if enabled else 0, _now()))


def account_rows(conn, known: list) -> list:
    on = enabled_accounts(conn)
    return [{"account": a, "enabled": a in on} for a in known]


# ---------------------------------------------------------------------------
# new recipients (§6 E258 item 5: agent drafts may go to anyone, flagged)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def emails_in(*fields) -> set:
    return {m.lower() for f in fields if f for m in _EMAIL_RE.findall(str(f))}


def new_recipients(conn, account: str, *fields) -> list:
    """Addresses in to/cc/bcc that this account has never had on a synced
    thread and never sent to from Grand Central."""
    wanted = emails_in(*fields)
    if not wanted:
        return []
    known: set = set()
    for (parts,) in conn.execute("SELECT participants FROM mail_threads WHERE account = ?",
                                 (account,)):
        known |= emails_in(parts)
        if wanted <= known:
            return []
    for row in conn.execute("SELECT to_addrs, cc_addrs, bcc_addrs FROM mail_sends "
                            "WHERE account = ?", (account,)):
        known |= emails_in(*row)
    return sorted(wanted - known)


# ---------------------------------------------------------------------------
# lockout
# ---------------------------------------------------------------------------

def lock_state(conn) -> dict | None:
    row = conn.execute("SELECT value FROM agent_state WHERE key = 'lock'").fetchone()
    return json.loads(row[0]) if row else None


def _lock(conn, reason: str) -> None:
    with conn:
        conn.execute("INSERT OR REPLACE INTO agent_state (key, value) VALUES ('lock', ?)",
                     (json.dumps({"at": _now(), "reason": reason}),))
    try:  # tell Casey; never let the push break the gate
        import push
        push.send("Grand Central agent locked out",
                  f"{reason}. Review the audit, then reset it in Settings.",
                  url="/settings.html", tag="gc-agent-lock")
    except Exception:
        pass


def reset_lock(conn) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM agent_state WHERE key = 'lock'")
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# the request context
# ---------------------------------------------------------------------------

class Ctx:
    """One request's gate state, carried from check() to finish()."""

    def __init__(self, who, method, route, args, tool):
        self.who = who
        self.method = method
        self.route = route
        self.args = args
        self.tool = tool
        self.accounts: set = set()
        self.threads: set = set()
        self.refusal: str | None = None
        self.audit = who != "human" and (who != "local" or method == "POST")
        self.allowed_accounts: set | None = None  # agent only


def _action(args: dict) -> str | None:
    a = args.get("action")
    return str(a) if a is not None else None


def check(conn, headers, method: str, route: str, args: dict) -> Ctx:
    """Decide before any handler runs. Raises Refused (carrying .ctx); the
    caller answers it and still calls finish() so the refusal is audited.
    `conn` may be a zero-argument callable, so human and local GETs — the
    board's own tSamc — never open the database here."""
    who = principal(headers)
    ctx = Ctx(who, method, route, args, (headers.get("X-GC-Tool") or "")[:60] or None)

    # E258 item 2: a Private thread does not exist here — for Casey's own
    # browser too ("Gone entirely"). Only POST /private (unmark) may name one.
    tid = str(args.get("thread_id") or args.get("forward_of") or "").strip()
    acct = str(args.get("account") or "").strip()
    if tid and acct and route != "/private":
        import private
        if (acct, tid) in private.keys_cached():
            raise _refuse(ctx, "thread not found", status=404)

    if who == "human":
        return ctx

    # ---- human-only, for agent and local alike ----
    if method == "POST":
        if route in HUMAN_ONLY_POST:
            raise _refuse(ctx, f"{route} is human-only: {HUMAN_ONLY_POST[route]}. "
                               "Use a paired browser.", pairing_required=True)
        act = _action(args)
        if act in HUMAN_ONLY_ACTIONS.get(route, ()):
            raise _refuse(ctx, f"'{act}' on {route} is human-only. Use a paired browser.",
                          pairing_required=True)
        if route == "/mail-action" and _is_private_label(args.get("label")):
            raise _refuse(ctx, f"the {PRIVATE_LABEL} label is set and cleared by Casey only.",
                          pairing_required=True)

    if who == "local":
        return ctx

    # ---- agent ----
    if who == "agent-badkey":
        raise _refuse(ctx, "unknown agent key")

    conn = conn() if callable(conn) else conn
    lock = lock_state(conn)
    if lock:
        raise _refuse(ctx, f"the agent is locked out since {lock['at']} ({lock['reason']}). "
                           "Casey resets it in Settings after reviewing the audit.",
                      status=423, count=False)

    if who == "desktop":
        table = DESKTOP_GET if method == "GET" else DESKTOP_POST if method == "POST" else {}
    else:
        table = AGENT_GET if method == "GET" else AGENT_POST if method == "POST" else {}
    if route not in table:
        raise _refuse(ctx, f"{method} {route} is not available to "
                           + ("the desktop app." if who == "desktop" else "agents."))
    allowed = AGENT_ACTIONS.get(route)
    if method == "POST" and allowed is not None:
        act = _action(args)
        if act not in allowed:
            raise _refuse(ctx, f"'{act}' is not an allowed action on {route}. "
                               f"Allowed: {', '.join(sorted(allowed))}.")

    on = enabled_accounts(conn)
    ctx.allowed_accounts = on
    if who == "desktop" and method == "GET" and route in ONE_ACCOUNT_GET:
        acct = str(args.get("account") or "").strip()
        if not acct or acct == "all":
            raise _refuse(ctx, "the desktop app reads one account per call: pass account.",
                          status=400)
        if acct in on:
            # it sees that account's rows and nothing else, whatever the route returns
            ctx.allowed_accounts = {acct}
    if who == "desktop" and route == "/agent/propose":
        check_proposal(conn, ctx, args, on)
    _check_accounts(conn, ctx, on)
    _check_limits(conn, ctx)
    return ctx


def check_proposal(conn, ctx: "Ctx", args: dict, on: set) -> None:
    """E260: a proposal must be something the agent itself could do, in a
    switched-on account; checked again, as the agent, when Casey approves."""
    path, body = args.get("path"), args.get("body")
    if path not in PROPOSABLE or not isinstance(body, dict):
        raise _refuse(ctx, f"{path!r} cannot be proposed.")
    allowed = AGENT_ACTIONS.get(path)
    if allowed is not None and body.get("action") not in allowed:
        raise _refuse(ctx, f"'{body.get('action')}' is not an allowed action on {path}.")
    if path == "/mail-action" and _is_private_label(body.get("label")):
        raise _refuse(ctx, f"the {PRIVATE_LABEL} label is set and cleared by Casey only.")
    inner = Ctx("agent", "POST", path, body, None)
    try:
        _check_accounts(conn, inner, on)
    except Refused as e:   # record it against the desktop caller, not the agent
        raise _refuse(ctx, str(e), status=e.status)
    if str(args.get("account") or "") not in inner.accounts:
        raise _refuse(ctx, "the proposal's account must be the account it acts in.", status=400)


def _is_private_label(label) -> bool:
    return str(label or "").strip().lower() == PRIVATE_LABEL.lower()


def _check_accounts(conn, ctx: Ctx, on: set) -> None:
    args = ctx.args
    acct = str(args.get("account") or "").strip()
    if acct and acct != "all":
        if acct not in on:
            raise _refuse(ctx, f"account {acct!r} is not switched on for agents. "
                               "Casey switches accounts on in Settings.")
        ctx.accounts.add(acct)
    elif ctx.route in ("/thread", "/labels", "/draft", "/draft/save", "/draft/compose",
                       "/calendar", "/mail/triage", "/mail-action", "/dismiss") \
            and not (ctx.route == "/calendar" and ctx.method == "GET"):
        raise _refuse(ctx, "account is required", status=400)

    # Todos addressed by id carry their account in the database.
    if ctx.route == "/task":
        ids = []
        if args.get("id") is not None:
            ids.append(args.get("id"))
        if isinstance(args.get("ids"), list):
            ids.extend(args["ids"])
        for tid in ids:
            try:
                row = conn.execute("SELECT account FROM engagements WHERE id = ?",
                                   (int(tid),)).fetchone()
            except (TypeError, ValueError):
                raise _refuse(ctx, f"bad todo id {tid!r}", status=400)
            owner = row[0] if row else None
            if row and owner not in on:
                raise _refuse(ctx, f"todo {tid} belongs to an account that is not "
                                   "switched on for agents.")
            if owner:
                ctx.accounts.add(owner)

    # A Context note scoped to an account needs that account on.
    if ctx.route == "/context" and ctx.method == "POST":
        scope = str(args.get("scope") or "")
        if scope.startswith("account:") and scope[8:] not in on:
            raise _refuse(ctx, f"account {scope[8:]!r} is not switched on for agents.")

    tid = str(args.get("thread_id") or "").strip()
    if tid and acct:
        ctx.threads.add(f"{acct}:{tid}")
        if conn.execute("SELECT 1 FROM private_threads WHERE account = ? AND thread_id = ?",
                        (acct, tid)).fetchone():
            raise _refuse(ctx, "that thread does not exist in Grand Central.", status=404)


def _bucket(ctx: Ctx) -> list:
    b = []
    if ctx.route == "/thread":
        b.append("thread")
    if ctx.route == "/mail/search":
        b.append("search")
    if ctx.method == "POST":
        if ctx.route.startswith("/draft"):
            b.append("draft")          # §6 2026-10-06: not a "write" for the hourly limit
        else:
            b.append("write")
        if ctx.route == "/sweep":
            b.append("sweep")
    return b


def _since(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _check_limits(conn, ctx: Ctx) -> None:
    buckets = _bucket(ctx)
    for bucket, window, cap, label in LIMITS:
        if _who(ctx) == "desktop":
            cap, label = DESKTOP_LIMITS.get((bucket, window), (cap, label))
        if bucket not in buckets:
            continue
        n = sum(1 for r in conn.execute(
            "SELECT route, method FROM agent_audit WHERE principal = ? "
            "AND refusal IS NULL AND ts >= ?", (_who(ctx), _since(window)))
            if bucket in _bucket(Ctx("agent", r[1], r[0], {}, None)))
        if n >= cap:
            raise _refuse(ctx, f"rate limit: {label}. Casey can raise it; bulk reading "
                               "is a sign something is wrong.", status=429)
    desktop = _who(ctx) == "desktop"
    if desktop and ctx.method == "POST":
        return
    cap = DESKTOP_BYTES_PER_HOUR if desktop else BYTES_PER_HOUR
    used = conn.execute(
        "SELECT COALESCE(SUM(result_bytes), 0) FROM agent_audit "
        "WHERE principal = ? AND ts >= ?", (_who(ctx), _since(HOUR))).fetchone()[0]
    if used >= cap:
        raise _refuse(ctx, f"rate limit: {cap // (1024 * 1024)} MB returned an hour.", status=429)


def _who(ctx: Ctx) -> str:
    return "desktop" if ctx.who == "desktop" else "agent"


def _refuse(ctx: Ctx, message: str, status: int = 403, count: bool = True, **extra) -> Refused:
    ctx.refusal = message if count else f"(locked) {message}"
    return Refused(message, status, ctx=ctx, **extra)


# ---------------------------------------------------------------------------
# after the handler: filter, measure, audit, trip the breaker
# ---------------------------------------------------------------------------

def filter_response(ctx: Ctx, obj, conn=None):
    """Drop every row that belongs to an account the agent may not see, and
    every Private thread. Rows without an account key pass through."""
    if ctx.allowed_accounts is None:
        return obj
    private = set()
    if conn is not None:
        conn = conn() if callable(conn) else conn
        private = {f"{a}:{t}" for a, t in conn.execute(
            "SELECT account, thread_id FROM private_threads")}
    on = ctx.allowed_accounts

    def keep(row) -> bool:
        if not isinstance(row, dict):
            return True
        acct = row.get("account")
        if isinstance(acct, str) and acct and acct not in on:
            return False
        scope = row.get("scope")  # Context notes scope by "account:X"
        if isinstance(scope, str) and scope.startswith("account:") and scope[8:] not in on:
            return False
        tid = row.get("thread_id")
        if acct and tid and f"{acct}:{tid}" in private:
            return False
        return True

    def walk(o):
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(x) for x in o if keep(x)]
        return o

    out = walk(obj)
    if ctx.route == "/thread" and isinstance(out, dict):
        # 2026-10-19: Casey's Claude session hit the 2 MB-an-hour budget reading
        # about 38 threads; each message came twice, as text and as its HTML
        # (~4x the size), and no agent tool reads the HTML. Agents get the text.
        for m in out.get("messages") or []:
            if isinstance(m, dict):
                m.pop("body_html", None)
    if ctx.who == "desktop" and ctx.route == "/brief" and isinstance(out, dict):
        # E260: one account's brief; the board-wide counts and Casey's own day
        # note span accounts, so the desktop app does not get them
        out = {k: v for k, v in out.items() if k not in ("summary", "day_note", "owed_count")}
    _collect(ctx, out)
    return out


def _collect(ctx: Ctx, o, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(o, dict):
        acct = o.get("account")
        if isinstance(acct, str) and acct and acct != "all":
            ctx.accounts.add(acct)
            if o.get("thread_id"):
                ctx.threads.add(f"{acct}:{o['thread_id']}")
        for v in o.values():
            _collect(ctx, v, depth + 1)
    elif isinstance(o, list):
        for v in o:
            _collect(ctx, v, depth + 1)


def finish(conn, ctx: Ctx, status: int, result_bytes: int, signals: int = 0) -> None:
    if not ctx.audit:
        return
    conn = conn() if callable(conn) else conn
    who = "agent" if ctx.who.startswith("agent") else ctx.who   # agent | desktop | local
    with conn:
        conn.execute(
            "INSERT INTO agent_audit (ts, principal, tool, method, route, args, accounts, "
            "thread_ids, status, result_bytes, refusal, signals) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), who, ctx.tool, ctx.method, ctx.route,
             json.dumps(ctx.args, ensure_ascii=False, default=str)[:20000],
             json.dumps(sorted(ctx.accounts)), json.dumps(sorted(ctx.threads)[:200]),
             status, result_bytes, ctx.refusal, signals))
    if who in ("agent", "desktop"):
        _breaker(conn, who)


def _breaker(conn, who: str = "agent") -> None:
    if lock_state(conn):
        return
    n, window = BREAKER_REFUSALS
    refusals = conn.execute(
        "SELECT COUNT(*) FROM agent_audit WHERE principal = ? AND refusal IS NOT NULL "
        "AND status = 403 AND ts >= ?",
        (who, _since(window))).fetchone()[0]
    if refusals >= n:
        _lock(conn, f"{refusals} refused {who} calls in {window // 60} minutes")
        return
    n, window = DESKTOP_BREAKER_THREADS if who == "desktop" else BREAKER_THREADS
    threads = set()
    for (tids,) in conn.execute(
            "SELECT thread_ids FROM agent_audit WHERE principal = ? AND route = '/thread' "
            "AND refusal IS NULL AND ts >= ?", (who, _since(window))):
        threads.update(json.loads(tids or "[]"))
    if len(threads) > n:
        _lock(conn, f"{len(threads)} different threads read in {window // 60} minutes")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# CLI — run on the host
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    import argparse
    import db
    ap = argparse.ArgumentParser(description="Grand Central agent gate (E258)")
    ap.add_argument("--new-key", action="store_true",
                    help="mint the MCP key (the MCP server reads data/agent_key.json)")
    ap.add_argument("--new-desktop-key", action="store_true",
                    help="mint the Claude desktop app's key (data/agent_key_desktop.json, E260)")
    ap.add_argument("--enable", nargs="+", metavar="ACCOUNT")
    ap.add_argument("--disable", nargs="+", metavar="ACCOUNT")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset", action="store_true", help="lift an agent lockout")
    a = ap.parse_args(argv)
    conn = db.connect()
    if a.new_key:
        new_key()
        print(f"New agent key written to {KEY_PATH} (0600). Restart the MCP client.")
    if a.new_desktop_key:
        new_key(DESKTOP_KEY_PATH)
        print(f"New desktop key written to {DESKTOP_KEY_PATH} (0600). Restart the Claude desktop app.")
    for acct in a.enable or []:
        set_account(conn, acct, True)
    for acct in a.disable or []:
        set_account(conn, acct, False)
    if a.reset:
        print("lockout lifted" if reset_lock(conn) else "not locked")
    if a.status or a.enable or a.disable:
        print("agent key:", "set" if load_key() else "NOT SET")
        print("desktop key:", "set" if load_key(DESKTOP_KEY_PATH) else "not set")
        print("switched on:", ", ".join(sorted(enabled_accounts(conn))) or "(none)")
        lock = lock_state(conn)
        print("lockout:", f"{lock['at']} — {lock['reason']}" if lock else "none")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
