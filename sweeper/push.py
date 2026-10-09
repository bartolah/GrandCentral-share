"""E188 — Web Push to Casey's own browsers (Mac Safari, iPhone home screen).

Companion to notify.py, which speaks iMessage. Same rule as §6: these
messages go ONLY to devices Casey himself subscribed from — a subscription is
created by a click in the board, stored here, and removed the moment the push
service says it is gone.

What leaves the host: an encrypted payload to the browser vendor's push
endpoint (Apple for Safari, Google for Chrome), signed with the VAPID key in
config/.env. The payload carries the same one-line alert text the iMessage
path already sends — no mail bodies, no credentials, no third-party
recipients. Outbound only, and only to the endpoint the device chose.

A subscription that answers 404 or 410 is dead (the browser dropped it, or
the app was deleted); those rows delete themselves on the next send.
"""

from __future__ import annotations

import json
import os

import db
from common import load_env

_ENV = None


def _env(key: str) -> str:
    global _ENV
    if _ENV is None:
        _ENV = load_env()
    return (_ENV.get(key) or os.environ.get(key) or "").strip()


def public_key() -> str:
    """The key a browser needs to create a subscription for us."""
    return _env("VAPID_PUBLIC_KEY")


def configured() -> bool:
    return bool(_env("VAPID_PRIVATE_KEY") and public_key())


def send(title: str, body: str, url: str = "/", tag: str = "gc", log=print) -> dict:
    """Push one alert to every subscribed device. Returns a small report."""
    if not configured():
        log("push: no VAPID keys in config/.env — skipping")
        return {"sent": 0, "dropped": 0, "error": "not configured"}
    try:
        from pywebpush import webpush, WebPushException
    except ImportError:
        log("push: pywebpush is not installed — skipping")
        return {"sent": 0, "dropped": 0, "error": "pywebpush missing"}

    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag})
    conn = db.connect()
    try:
        subs = db.push_subs(conn)
        sent = dropped = 0
        for s in subs:
            try:
                webpush(
                    subscription_info=json.loads(s["subscription"]),
                    data=payload,
                    vapid_private_key=_env("VAPID_PRIVATE_KEY"),
                    vapid_claims={"sub": _env("VAPID_SUBJECT") or "mailto:you@example.com"},
                    timeout=10,
                )
                sent += 1
            except WebPushException as e:
                code = getattr(e.response, "status_code", None)
                if code in (404, 410):          # the device is gone for good
                    db.push_sub_delete(conn, s["id"])
                    dropped += 1
                    log(f"push: dropped dead subscription {s['id']} ({s['label']})")
                else:
                    log(f"push: send failed for {s['label']} — {code or e}")
            except Exception as e:              # a malformed row must not stop the rest
                log(f"push: send error for {s['label']} — {e!r}")
        return {"sent": sent, "dropped": dropped}
    finally:
        conn.close()


if __name__ == "__main__":       # .venv/bin/python sweeper/push.py "title" "body"
    import sys
    t = sys.argv[1] if len(sys.argv) > 1 else "Grand Central"
    b = sys.argv[2] if len(sys.argv) > 2 else "Test push from the host."
    print(send(t, b, tag="test"))
