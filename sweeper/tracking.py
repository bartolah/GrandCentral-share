"""Open and click tracking on Grand Central's own sends — E269.

Bart, 2026-10-06: "What's involved in order to have GrandCentral track when
my emails are opened?" → "Let's do it." BUILD_SPEC §6 entry of that date;
design in ACCOUNT_FIRST.md "Open and click tracking".

A tracked send carries, in its HTML part only, an invisible 1x1 image and
(where the account allows) links that pass through a signed redirect, all on
t.<the sending account's own domain>, served by the Cloudflare Worker in
integrations/tracker/. The Worker keeps only a random token per send, the
time, the kind, a coarse source label and the country — no IPs, no content.
mail_sync pulls its events every run (pull()); this module turns them into
"opened 2× · last Tue 3:14 PM" for the board.

Nothing is tracked unless the account's domain answered /health recently
and TRACK_SECRET is set, so a send never carries a broken image or link.
Bart's own view of his mail in Grand Central strips both (strip()).

    .venv/bin/python sweeper/tracking.py              # settings, health, recent sends
    .venv/bin/python sweeper/tracking.py --check      # re-run the health checks now
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html as htmlmod
import json
import re
import secrets
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone

import db

# Bart: on by default for the sales accounts, off for Personal (and anything
# not listed until he switches it on in Settings → Tracking).
DEFAULT_ON = {"connectblue", "capitol_influence", "crowdblue"}
SIGNALS = ("enabled", "clicks", "ping", "waiting")
AUTO_SRC = {"apple-prefetch", "scanner"}
AUTO_SECONDS = 30            # an open this soon after sending is a scanner or prefetch
HEALTH_TTL = timedelta(minutes=30)
HEALTH_TRUST = timedelta(hours=6)   # a send trusts a health check this old
WAIT_NUDGE_DAYS = 3          # "opened, no reply after 3 days"
_FREEMAIL = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "me.com", "aol.com"}
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(ts: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ settings
def settings(conn, account: str) -> dict:
    row = conn.execute("SELECT * FROM track_settings WHERE account = ?", (account,)).fetchone()
    on = account in DEFAULT_ON
    out = {s: on for s in SIGNALS}
    if row:
        out.update({s: bool(row[s]) for s in SIGNALS if row[s] is not None})
    return out


def set_settings(conn, account: str, **vals) -> dict:
    account = db.clean_account(account)
    cur = settings(conn, account)
    for k, v in vals.items():
        if k not in SIGNALS:
            raise ValueError(f"unknown tracking setting {k!r}")
        cur[k] = bool(v)
    with conn:
        conn.execute("INSERT INTO track_settings (account, enabled, clicks, ping, waiting, updated_at) "
                     "VALUES (?,?,?,?,?,?) ON CONFLICT(account) DO UPDATE SET enabled=excluded.enabled, "
                     "clicks=excluded.clicks, ping=excluded.ping, waiting=excluded.waiting, updated_at=excluded.updated_at",
                     (account, *(int(cur[s]) for s in SIGNALS), _iso(_now())))
    return cur


def domain_for(account: str, env: dict) -> str | None:
    """The sending account's own domain — None for a free-mail address."""
    user = env.get(f"GMAIL_{str(account).upper()}_USER") or ""
    if not user:
        from sources.gmail_reader import ACCOUNTS
        user_key = (ACCOUNTS.get(account) or (None,))[0]
        user = (env.get(user_key) or "") if user_key else ""
    dom = user.rpartition("@")[2].lower()
    return dom if dom and dom not in _FREEMAIL else None


def base_url(domain: str) -> str:
    return f"https://t.{domain}"


def healthy(conn, domain: str | None, within: timedelta = HEALTH_TRUST) -> bool:
    if not domain:
        return False
    row = conn.execute("SELECT ok, checked_at FROM track_health WHERE domain = ?", (domain,)).fetchone()
    at = _parse(row["checked_at"]) if row else None
    return bool(row and row["ok"] and at and _now() - at < within)


def check_health(conn, env: dict, force: bool = False, log=print) -> dict:
    """GET t.<domain>/health for every account domain, at most every 30 min."""
    import requests
    out = {}
    for dom in own_tracking_domains(env):
        row = conn.execute("SELECT checked_at FROM track_health WHERE domain = ?", (dom,)).fetchone()
        at = _parse(row["checked_at"]) if row else None
        if not force and at and _now() - at < HEALTH_TTL:
            continue
        try:
            r = requests.get(base_url(dom) + "/health", timeout=8)
            ok = r.status_code == 200 and r.text.strip() == "ok"
        except Exception:
            ok = False
        out[dom] = ok
        with conn:
            conn.execute("INSERT INTO track_health (domain, ok, checked_at) VALUES (?,?,?) "
                         "ON CONFLICT(domain) DO UPDATE SET ok=excluded.ok, checked_at=excluded.checked_at",
                         (dom, int(ok), _iso(_now())))
    if out:
        log(f"tracking: health {out}")
    return out


# ------------------------------------------------------------------ the decision
def _company(conn, env: dict) -> tuple[set, set]:
    """Addresses and domains that are 'inside' — teammates, company groups
    and Bart's own domains. Mail only to them is never tracked by default."""
    import mailclass
    people = {r["email"].lower() for r in conn.execute("SELECT email FROM class_people")}
    doms = {r["domain"].lower() for r in conn.execute("SELECT domain FROM class_groups")}
    try:
        from mail_sync import own_domains
        doms |= {d.lower() for d in own_domains(env)}
    except Exception:
        pass
    return people | {a.lower() for a in mailclass.own_addresses(env)}, doms


def _addrs(*fields) -> list:
    from email.utils import getaddresses
    return [a.lower() for _n, a in getaddresses([f for f in fields if f]) if "@" in a]


def decide(conn, account: str, env: dict, to: str, cc: str = "", bcc: str = "",
           override: bool | None = None) -> dict:
    """Whether this send is tracked, and why — the composer shows `why` next
    to the 👁 toggle. Bart's toggle (override) wins over the default, but
    nothing is tracked while the domain is down or the secret is missing."""
    st = settings(conn, account)
    dom = domain_for(account, env)
    out = {"available": bool(dom and (env.get("TRACK_SECRET") or "").strip() and healthy(conn, dom)),
           "domain": dom, "clicks": st["clicks"]}
    if not out["available"]:
        out.update(on=False, why=("Tracking isn't set up for this account yet" if dom
                                  else "Tracking needs a company domain"))
        return out
    if override is not None:
        out.update(on=bool(override), why="you turned it " + ("on" if override else "off"))
        return out
    if not st["enabled"]:
        out.update(on=False, why="off for this account (Settings → Tracking)")
        return out
    people, doms = _company(conn, env)
    outside = [a for a in _addrs(to, cc, bcc) if a not in people and a.rpartition("@")[2] not in doms]
    out.update(on=bool(outside), why=("someone outside your companies is on it" if outside
                                      else "only your team is on it"))
    return out


# ------------------------------------------------------------------ the mail
def sign(secret: str, token: str, n: int, url: str) -> str:
    """Matches the Worker's hmac(): base64url HMAC-SHA256, first 22 chars."""
    mac = hmac.new(secret.encode(), f"{token}|{n}|{url}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).decode().rstrip("=")[:22]


_HREF_RE = re.compile(r'(<a\b[^>]*?\bhref\s*=\s*)(["\'])(.*?)\2', re.I | re.S)


def html_from_plain(body: str) -> str:
    """A scheduled or plain send gets a simple HTML twin to carry the image."""
    esc = htmlmod.escape(body or "")
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    return f'<div style="white-space:pre-wrap">{esc}</div>'


def prepare(body_html: str | None, body_plain: str, token: str, domain: str, secret: str,
            clicks: bool) -> tuple[str, list]:
    """The HTML part with the image added and (if clicks) its http(s) links
    passed through the signed redirect. Returns (html, links). The plain
    part is never touched."""
    base = base_url(domain)
    html = body_html if (body_html or "").strip() else html_from_plain(body_plain)
    links: list = []
    if clicks:
        def wrap(m):
            url = htmlmod.unescape(m.group(3)).strip()
            if not re.match(r"^https?://", url, re.I) or url.startswith(base):
                return m.group(0)
            n = len(links)
            links.append(url)
            red = f"{base}/c/{token}/{n}/{sign(secret, token, n, url)}?u={urllib.parse.quote(url, safe='')}"
            return f"{m.group(1)}{m.group(2)}{htmlmod.escape(red, quote=True)}{m.group(2)}"
        html = _HREF_RE.sub(wrap, html)
    pixel = (f'<img src="{base}/o/{token}.gif" width="1" height="1" alt="" '
             f'style="display:block;width:1px;height:1px;border:0;margin:0;padding:0">')
    html = re.sub(r"(?i)</body>", pixel + "</body>", html, count=1) if re.search(r"(?i)</body>", html) else html + pixel
    return html, links


def new_token() -> str:
    return secrets.token_urlsafe(18)


def record(conn, token: str, account: str, thread_id: str, send_id: int | None, subject: str,
           to: str, cc: str, links: list, message_id: str | None, clicks: bool) -> None:
    with conn:
        conn.execute("INSERT INTO track_sends (token, account, thread_id, send_id, subject, recipients, links, "
                     "message_id, clicks, sent_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (token, account, thread_id or "", send_id, subject, ", ".join(x for x in (to, cc) if x),
                      json.dumps(links), message_id, int(clicks), _iso(_now())))


# ------------------------------------------------------------------ Bart's own view
_STRIP_IMG = r'<img\b[^>]*\bsrc\s*=\s*["\']https?://t\.(?:{doms})/o/[^"\']+["\'][^>]*>'
_STRIP_A = r'https?://t\.(?:{doms})/c/[A-Za-z0-9_-]+/\d+/[A-Za-z0-9_-]+\?u=([^"\'\s>]+)'


def strip(html: str | None, domains) -> str | None:
    """Grand Central showing Bart his own sent mail must never count as an
    open or a click: drop the image, unwrap the links."""
    if not html or not domains:
        return html
    alt = "|".join(re.escape(d) for d in domains)
    html = re.sub(_STRIP_IMG.format(doms=alt), "", html, flags=re.I)
    return re.sub(_STRIP_A.format(doms=alt),
                  lambda m: htmlmod.escape(urllib.parse.unquote(htmlmod.unescape(m.group(1))), quote=True),
                  html, flags=re.I)


def own_tracking_domains(env: dict) -> list:
    accts = [k[6:-5].lower() for k in env if k.startswith("GMAIL_") and k.endswith("_USER")]
    return sorted({d for d in (domain_for(a, env) for a in accts) if d})


# ------------------------------------------------------------------ events
def is_auto(src: str | None, ts: str | None, sent_at: str | None) -> bool:
    if src in AUTO_SRC:
        return True
    a, b = _parse(ts), _parse(sent_at)
    return bool(a and b and (a - b).total_seconds() < AUTO_SECONDS)


def ingest(conn, events: list) -> int:
    """Store pulled Worker events; unknown tokens are dropped."""
    n = 0
    with conn:
        for e in events:
            tok = str(e.get("token") or "")
            row = conn.execute("SELECT sent_at FROM track_sends WHERE token = ?", (tok,)).fetchone()
            if not row or not _TOKEN_RE.match(tok):
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO track_events (remote_id, token, kind, link, src, country, ts, auto) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (int(e["id"]), tok, e.get("kind"), e.get("link"), e.get("src"), e.get("country"), e.get("ts"),
                 int(is_auto(e.get("src"), e.get("ts"), row["sent_at"]))))
            n += cur.rowcount
    return n


def pull(conn, env: dict, log=print) -> int:
    """mail_sync, every run: health checks, then new events from the Worker
    (any healthy domain serves all of them), then first-open pings."""
    secret = (env.get("TRACK_SECRET") or "").strip()
    if not secret:
        return 0
    check_health(conn, env, log=log)
    dom = next((d for d in own_tracking_domains(env) if healthy(conn, d, HEALTH_TTL * 2)), None)
    if not dom:
        return 0
    import requests
    after = conn.execute("SELECT COALESCE(MAX(remote_id), 0) FROM track_events").fetchone()[0]
    got = 0
    for _ in range(10):
        r = requests.get(base_url(dom) + "/events", params={"after": after},
                         headers={"Authorization": f"Bearer {secret}"}, timeout=15)
        r.raise_for_status()
        evs = r.json().get("events") or []
        if not evs:
            break
        got += ingest(conn, evs)
        after = max(int(e["id"]) for e in evs)
        if len(evs) < 500:
            break
    resolve_threads(conn, env, log=log)
    ping(conn, env, log=log)
    if got:
        log(f"tracking: {got} new event(s)")
    return got


def resolve_threads(conn, env: dict, limit: int = 5, log=print) -> None:
    """A NEW email has no thread id until Gmail assigns one; find it by the
    Message-ID we set, so its opens land on the right thread."""
    import mail_common as mc
    rows = conn.execute("SELECT token, account, message_id FROM track_sends WHERE thread_id = '' "
                        "AND message_id IS NOT NULL AND sent_at > ? ORDER BY sent_at DESC LIMIT ?",
                        (_iso(_now() - timedelta(days=7)), limit)).fetchall()
    for r in rows:
        try:
            imap, _user = mc.connect(r["account"], env)
            try:
                box = mc.find_folder(imap, "\\All", "[Gmail]/All Mail")
                imap.select(f'"{box}"', readonly=True)
                typ, data = imap.uid("SEARCH", None, "HEADER", "Message-ID", r["message_id"])
                uids = data[0].split() if typ == "OK" and data and data[0] else []
                if not uids:
                    continue
                typ, data = imap.uid("FETCH", uids[-1], "(X-GM-THRID)")
                m = re.search(rb"X-GM-THRID (\d+)", data[0] if isinstance(data[0], bytes) else data[0][0])
                if m:
                    with conn:
                        conn.execute("UPDATE track_sends SET thread_id = ? WHERE token = ?",
                                     (m.group(1).decode(), r["token"]))
            finally:
                imap.logout()
        except Exception as e:
            log(f"tracking: thread lookup for a new send failed ({e!r})")


def ping(conn, env: dict, log=print) -> None:
    """The first REAL open of a send, once, if that account has pings on."""
    import notify
    target = env.get("NOTIFY_IMESSAGE_TARGET", "you@example.com")
    rows = conn.execute(
        "SELECT s.token, s.account, s.thread_id, s.subject, s.recipients, MIN(e.ts) AS first "
        "FROM track_sends s JOIN track_events e ON e.token = s.token AND e.kind = 'open' AND e.auto = 0 "
        "WHERE e.ts > ? GROUP BY s.token", (_iso(_now() - timedelta(days=2)),)).fetchall()
    for r in rows:
        if not settings(conn, r["account"])["ping"] or notify._sent(conn, "open", r["token"]):
            continue
        who = (r["recipients"] or "").split(",")[0].split("<")[0].strip().strip('"') or "Someone"
        notify._mark(conn, "open", r["token"])
        notify._deliver(conn, "open", target, f"👁 {who} opened “{(r['subject'] or '')[:70]}”\n"
                        f"First open {r['first'][11:16]} UTC · {r['account']}", log=log,
                        url=f"/?account={r['account']}")


# ------------------------------------------------------------------ the board
def summary(conn, account: str) -> dict:
    """{thread_id: {...}} for one account's tracked sends: real opens, the
    automatic ones, clicks, first and last open, and whether it's waiting."""
    out: dict = {}
    for s in conn.execute("SELECT * FROM track_sends WHERE account = ? AND thread_id != '' "
                          "ORDER BY sent_at", (account,)):
        ev = conn.execute("SELECT kind, link, auto, ts FROM track_events WHERE token = ? ORDER BY ts",
                          (s["token"],)).fetchall()
        real = [e for e in ev if e["kind"] == "open" and not e["auto"]]
        clicks = [e for e in ev if e["kind"] == "click" and not e["auto"]]
        links = json.loads(s["links"] or "[]")
        t = out.setdefault(s["thread_id"], {"sends": 0, "opens": 0, "auto": 0, "clicks": 0,
                                            "first_open": None, "last_open": None, "clicked": [],
                                            "last_sent": None})
        t["sends"] += 1
        t["opens"] += len(real)
        t["auto"] += sum(1 for e in ev if e["auto"])
        t["clicks"] += len(clicks)
        t["clicked"] += [links[e["link"]] for e in clicks if e["link"] is not None and e["link"] < len(links)]
        t["clicked"] = list(dict.fromkeys(t["clicked"]))[:5]
        if real:
            t["first_open"] = t["first_open"] or real[0]["ts"]
            t["last_open"] = real[-1]["ts"]
        t["last_sent"] = s["sent_at"]
    st = settings(conn, account)
    for tid, t in out.items():
        # a reply lands in the inbox as an inbound last message — then
        # there's nobody to nudge
        m = conn.execute("SELECT last_direction, last_date FROM mail_threads WHERE account = ? AND thread_id = ?",
                         (account, tid)).fetchone()
        replied = bool(m and m["last_direction"] == "inbound" and (m["last_date"] or "") > (t["last_sent"] or ""))
        t["replied"] = replied
        t["nudge"] = bool(st["waiting"] and t["last_open"] and t["last_sent"] and not replied
                          and _now() - _parse(t["last_sent"]) >= timedelta(days=WAIT_NUDGE_DAYS))
    return out


def main(argv) -> int:
    from common import load_env
    env = load_env()
    conn = db.connect()
    try:
        if "--check" in argv:
            print(check_health(conn, env, force=True))
        for d in own_tracking_domains(env):
            print(f"{d:<24} healthy={healthy(conn, d)}")
        from sources.gmail_reader import ACCOUNTS
        for a in ACCOUNTS:
            print(f"{a:<20} {settings(conn, a)}")
        for r in conn.execute("SELECT account, subject, sent_at, (SELECT COUNT(*) FROM track_events e "
                              "WHERE e.token = s.token AND e.auto = 0) AS n FROM track_sends s "
                              "ORDER BY sent_at DESC LIMIT 10"):
            print(f"  {r['sent_at']} {r['account']:<18} {r['n']:>3} real  {r['subject'][:50]}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
