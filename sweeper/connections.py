"""Connections — read config/.env status and write credentials from the board.

This is the ONLY module that writes secrets to config/.env. Guardrails
(spec §6, credentials non-negotiable):

  * Secret VALUES are never returned to a caller or logged. `status()` reports
    presence + a masked hint (last 4) only — never the value.
  * Writes are atomic (temp + rename), chmod 600, and the prior .env is copied
    to config/backups/ first.
  * Credentials are validated against the live service before saving where a
    cheap check exists (IMAP login, Anthropic/Granola auth ping).

Gmail accounts are modelled as pairs of env keys (GMAIL_<SLUG>_USER /
GMAIL_<SLUG>_APP_PASSWORD); see sources/gmail_reader.scan_accounts. Adding an
account just writes those two keys; the sweeper derives its account list from
the .env each run, and refresh_accounts() updates the running dashboard in
place so the new account is usable immediately.
"""

from __future__ import annotations

import imaplib
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from datetime import datetime

try:
    from .common import CONFIG_DIR, ENV_PATH, load_env
    from .sources import gmail_reader
except ImportError:  # pragma: no cover - direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import CONFIG_DIR, ENV_PATH, load_env
    from sources import gmail_reader

BACKUP_DIR = os.path.join(CONFIG_DIR, "backups")
IMAP_TIMEOUT = 15
HTTP_TIMEOUT = 15

# Single-value credentials the UI manages (Gmail accounts are handled apart).
SINGLE = {
    "anthropic": {"key": "ANTHROPIC_API_KEY", "label": "Anthropic API key",
                  "note": "Classifier + Ask. The board is useless without it.",
                  "testable": True},
    "granola":   {"key": "GRANOLA_TOKEN", "label": "Granola token",
                  "note": "Meeting notes → tasks. Reader is unverified; save even if the test can't confirm.",
                  "testable": True},
    # E209/E210: nothing is sent to Jev unless that account's switch is on
    # (Settings → Classification). §6, 2026-09-26.
    "jev":       {"key": "JEV_API_KEY", "label": "Jev API key",
                  "note": "TypeSafe (typesafe.ai) key for email classification. Used only for accounts switched on in Settings → Classification.",
                  "testable": True},
    # E269 (§6 2026-10-06): shared with the tracker Worker — it signs click
    # redirects and authorizes Grand Central's pull of open/click events
    "tracker":   {"key": "TRACK_SECRET", "label": "Tracking secret",
                  "note": "Shared secret with the gc-tracker Cloudflare Worker (wrangler secret put SECRET). Opens and clicks stay off until it's set.",
                  "testable": False},
    # 2026-10-08: lets Grand Central set up t.<domain> for accounts whose DNS
    # isn't on Cloudflare (Cloudflare for SaaS custom hostnames on brightline.example)
    "cloudflare": {"key": "CLOUDFLARE_API_TOKEN", "label": "Cloudflare API token",
                   "note": "brightline.example only: Zone → SSL and Certificates (Edit), DNS (Edit), Workers Routes (Edit). Used to serve t.<domain> for the tracker.",
                   "testable": False},
    "calendar":  {"key": "GCAL_ICS_URLS", "label": "Calendar ICS URLs",
                  "note": "Comma-separated secret iCal URLs. Replace-only; treat as secrets.",
                  "testable": False, "islist": True},
}

_PLACEHOLDERS = ("PASTE_", "CHANGE_", "sk-ant-xxx", "xxxx")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _is_placeholder(val: str) -> bool:
    v = (val or "").strip()
    return (not v) or v.startswith(_PLACEHOLDERS) or "xxxx" in v


def _mask(val: str) -> str:
    v = (val or "").strip()
    if not v:
        return ""
    return ("…" + v[-4:]) if len(v) > 4 else "····"


def _slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_").upper()


def _clean_pw(pw: str) -> str:
    # Google app passwords are shown with spaces but entered without them.
    return re.sub(r"\s+", "", pw or "")


# --------------------------------------------------------------------------
# status (no secret values ever leave here)
# --------------------------------------------------------------------------
def status() -> dict:
    env = load_env()
    accounts = []
    for slug, (uk, pk) in gmail_reader.scan_accounts(env).items():
        user = env.get(uk, "")
        pw = env.get(pk, "")
        accounts.append({
            "slug": slug,
            "user": user,                       # the email address (not a secret)
            "set": bool(user) and not _is_placeholder(pw),
        })

    singles = {}
    for name, meta in SINGLE.items():
        val = env.get(meta["key"], "")
        live = bool(val) and not _is_placeholder(val)
        if meta.get("islist"):
            n = len([u for u in val.split(",") if u.strip()]) if live else 0
            hint = f"{n} URL(s)" if n else ""
        else:
            hint = _mask(val) if live else ""
        singles[name] = {
            "label": meta["label"], "note": meta.get("note", ""),
            "testable": meta.get("testable", False), "islist": meta.get("islist", False),
            "set": live, "hint": hint,
        }
    return {"accounts": accounts, "singles": singles}


# --------------------------------------------------------------------------
# .env writer — atomic, chmod 600, backed up, comments/order preserved
# --------------------------------------------------------------------------
_KEYLINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=)")


def _backup_env() -> None:
    if os.path.exists(ENV_PATH):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(ENV_PATH, os.path.join(BACKUP_DIR, f".env.{stamp}"))


def _write_env(updates: dict, removals=()) -> None:
    updates = dict(updates or {})
    removals = set(removals or ())
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()

    out, seen = [], set()
    for line in lines:
        m = _KEYLINE.match(line)
        if m:
            key = m.group(2)
            if key in removals:
                continue
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)

    while out and not out[-1].strip():   # tidy any trailing blank lines first
        out.pop()
    new = [k for k in updates if k not in seen]
    if new:
        out.append("")                    # one blank line before appended keys
        out.extend(f"{k}={updates[k]}" for k in new)

    _backup_env()
    tmp = ENV_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, ENV_PATH)
    os.chmod(ENV_PATH, 0o600)
    gmail_reader.refresh_accounts()   # running dashboard sees changes at once


# --------------------------------------------------------------------------
# mutations
# --------------------------------------------------------------------------
def add_gmail(email: str, password: str, label: str = "") -> dict:
    email = (email or "").strip()
    password = _clean_pw(password)
    if "@" not in email:
        raise ValueError("enter a valid email address")
    if not password:
        raise ValueError("app password required")
    slug = _slugify(label or email.split("@")[0])
    if not slug:
        raise ValueError("could not derive an account name — add a label")
    if slug.lower() in gmail_reader.scan_accounts(load_env()):
        raise ValueError(f"an account named '{slug.lower()}' already exists")
    _write_env({f"GMAIL_{slug}_USER": email, f"GMAIL_{slug}_APP_PASSWORD": password})
    return status()


def remove_gmail(slug: str) -> dict:
    slug = (slug or "").strip().lower()
    if slug not in gmail_reader.scan_accounts(load_env()):
        raise ValueError("no such account")
    up = slug.upper()
    _write_env({}, removals=[f"GMAIL_{up}_USER", f"GMAIL_{up}_APP_PASSWORD"])
    return status()


def set_single(name: str, value: str) -> dict:
    meta = SINGLE.get(name)
    if not meta:
        raise ValueError("unknown credential")
    _write_env({meta["key"]: (value or "").strip()})
    return status()


def clear_single(name: str) -> dict:
    meta = SINGLE.get(name)
    if not meta:
        raise ValueError("unknown credential")
    _write_env({}, removals=[meta["key"]])
    return status()


# --------------------------------------------------------------------------
# live validators
# --------------------------------------------------------------------------
def test_imap(user: str, password: str):
    user = (user or "").strip()
    password = _clean_pw(password)
    if "@" not in user or not password:
        return False, "enter an email and app password first"
    try:
        m = imaplib.IMAP4_SSL(gmail_reader.IMAP_HOST, gmail_reader.IMAP_PORT,
                              timeout=IMAP_TIMEOUT)
    except Exception as e:
        return False, f"could not reach Gmail: {e}"
    try:
        m.login(user, password)
        try:
            m.logout()
        except Exception:
            pass
        return True, "signed in — IMAP works"
    except imaplib.IMAP4.error as e:
        msg = str(e)
        if any(s in msg for s in ("AUTHENTICATIONFAILED", "Invalid credentials",
                                  "Application-specific password")):
            return False, "login rejected — check the app password (2-Step Verification must be on)"
        return False, f"login failed: {msg}"
    except Exception as e:
        return False, f"login failed: {e}"


def _http(method: str, url: str, headers: dict, data=None):
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return getattr(r, "status", r.getcode()), None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return None, str(e)


def _auth_result(code, err, noun, tail=""):
    # These are auth pings whose only variable is the credential, so any 4xx
    # means the service rejected it (some services answer a bad token with 400, not 401).
    if code == 200:
        return True, f"{noun} works"
    if code is None:
        return False, f"could not reach the service ({err}){tail}"
    if 400 <= code < 500:
        return False, f"{noun} rejected ({code}){tail}"
    return False, f"unexpected response ({code}){tail}"


def test_anthropic(key: str):
    key = (key or "").strip()
    if not key:
        return False, "paste a key first"
    code, err = _http("GET", "https://api.anthropic.com/v1/models",
                      {"x-api-key": key, "anthropic-version": "2023-06-01"})
    return _auth_result(code, err, "key")


def test_granola(token: str):
    token = (token or "").strip()
    if not token:
        return False, "paste a token first"
    body = json.dumps({"limit": 1}).encode()
    code, err = _http("POST", "https://api.granola.ai/v2/get-documents",
                      {"Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}, body)
    # Reader is unverified against the real service — a save is allowed even if
    # this can't confirm the token.
    return _auth_result(code, err, "token", tail=" — reader is unverified; you can still save")


def test_jev(key: str):
    # E210: TypeSafe's model list — an auth ping that sends no mail content
    key = (key or "").strip()
    if not key:
        return False, "paste a key first"
    code, err = _http("GET", "https://api.typesafe.ai/v1/models",
                      {"Authorization": f"Bearer {key}"})
    return _auth_result(code, err, "key")


_SINGLE_TESTS = {"anthropic": test_anthropic, "granola": test_granola, "jev": test_jev}


# --------------------------------------------------------------------------
# dispatch — one entry point for dashboard/serve.py POST /connections
# --------------------------------------------------------------------------
def handle(payload: dict) -> dict:
    action = (payload.get("action") or "").strip()

    if action == "status":
        return {"ok": True, **status()}

    if action == "test_gmail":
        ok, msg = test_imap(payload.get("email"), payload.get("password"))
        return {"ok": ok, "message": msg}

    if action == "add_gmail":
        if payload.get("test", True):
            ok, msg = test_imap(payload.get("email"), payload.get("password"))
            if not ok:
                return {"ok": False, "error": msg}
        st = add_gmail(payload.get("email"), payload.get("password"),
                       payload.get("label", ""))
        return {"ok": True, "message": "account added — it joins the next mail sync (~3 min)", **st}

    if action == "remove_gmail":
        st = remove_gmail(payload.get("slug"))
        return {"ok": True, "message": "account removed", **st}

    if action == "test_single":
        fn = _SINGLE_TESTS.get(payload.get("name"))
        if not fn:
            return {"ok": False, "error": "no live test for this credential"}
        ok, msg = fn(payload.get("value"))
        return {"ok": ok, "message": msg}

    if action == "set_single":
        st = set_single(payload.get("name"), payload.get("value"))
        return {"ok": True, "message": "saved — takes effect on the next sweep", **st}

    if action == "clear_single":
        st = clear_single(payload.get("name"))
        return {"ok": True, "message": "cleared", **st}

    raise ValueError(f"unknown action {action!r}")
