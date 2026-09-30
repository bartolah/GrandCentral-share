"""HTTP client for the Grand Central dashboard API.

The MCP server is a THIN, TYPED CLIENT over the dashboard's existing HTTP API
(localhost:8080). It deliberately does not import sweeper.db or touch SQLite
directly: the dashboard endpoints are the only write paths in the system
(BUILD_SPEC §6), and adding a second one would break the one-contract
architecture in §2.

Safety is structural here, not advisory. `ALLOWED` is a hard allowlist of
(method, path) pairs; `request()` refuses anything else before a socket is
opened. The forbidden paths in DENIED are never reachable from this process
even if a future tool definition asks for one by mistake.

Zero third-party dependencies (BUILD_SPEC §7: no heavy frameworks) — stdlib
urllib only, so the server cannot break on a dependency upgrade.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("GC_BASE_URL", "http://127.0.0.1:8080").rstrip("/")

# Per-endpoint timeouts in seconds. Drafting calls a model (~10s observed);
# a sweep is ~4 minutes but /sweep returns as soon as it has kicked the job.
DEFAULT_TIMEOUT = 30.0
TIMEOUTS = {
    "/draft": 120.0,
    "/draft/save": 60.0,
    "/draft/compose": 60.0,
    "/mail": 120.0,
    "/mail/search": 90.0,
    "/thread": 90.0,
    "/brief": 90.0,  # ?refresh=1 kicks a sweep before returning
    "/sweep": 60.0,
}

# ---------------------------------------------------------------------------
# The allowlist. Nothing outside this table can be requested.
# ---------------------------------------------------------------------------

ALLOWED = {
    ("GET", "/brief"),
    ("GET", "/todos"),
    ("GET", "/thread"),
    ("GET", "/mail"),
    ("GET", "/mail/search"),
    ("GET", "/context"),
    ("GET", "/labels"),
    ("POST", "/draft"),
    ("POST", "/draft/save"),
    ("POST", "/draft/compose"),
    ("POST", "/calendar"),
    ("POST", "/task"),
    ("POST", "/mail/triage"),
    ("POST", "/mail-action"),
    ("POST", "/dismiss"),
    ("POST", "/context"),
    ("POST", "/sweep"),
}

# Paths this process must never touch, with the reason attached so the intent
# survives refactoring. "*" bans every method; otherwise the ban is per-method.
DENIED: dict[tuple[str, str], str] = {
    ("*", "/send"): (
        "send is a human's Send click in the dashboard composer only (BUILD_SPEC §6, "
        "2026-07-10, widened for new mail 2026-07-29, one click + Undo 2026-09-29 — "
        "all say 'never a command-bar/Claude/OpenClaw tool'). Draft it instead and hand Bart the link."
    ),
    ("*", "/mail/bulk"): "no bulk archive/trash from an agent, ever (§6).",
    ("*", "/draft/replace"): "replacing a draft trashes the old copy — dashboard click only (E224).",
    ("*", "/projects"): "projects are filed by Bart on the board — an agent creating or filing into one needs its own decision (§6, 2026-09-29).",
    ("*", "/calendar/rsvp"): "RSVP tells the organizer — dashboard click only (§6 Calendar RSVP, 2026-09-27).",
    ("*", "/draft/discard"): "removing a draft is a Trash write — dashboard click only (E224).",
    ("*", "/outbox"): "scheduled sends are dashboard-only territory.",
    ("*", "/connections"): "credential writes are explicitly not an agent tool (E51).",
    ("*", "/command"): (
        "that is the dashboard's own LLM command bar with write tools attached; "
        "calling it would nest an agent inside an agent."
    ),
    ("*", "/upload"): "writes files to disk; not an agent path.",
    ("*", "/home"): (
        "the cross-account Home is a view for Bart only; no agent tool gains a "
        "cross-account read (BUILD_SPEC §6, 2026-09-29)."
    ),
    ("*", "/home/note"): "the cross-account day note is for Bart's Home only (§6, 2026-09-29).",
    ("*", "/dev"): (
        "the dev-request queue is executed unattended by a Claude Code worker that "
        "edits code. Nothing that reads email may write into it."
    ),
    ("POST", "/registry"): "retired (projects/goals are gone); kept denied so nothing ever writes config/projects.yaml.",
}

# Some routes host a dangerous verb next to safe ones, so allowlisting the
# route is not enough — /mail-action carries `trash`, /context carries
# `delete`. These are enforced before the request goes out.
ACTION_ALLOW: dict[str, set[str]] = {
    "/mail-action": {"archive", "star", "unstar", "label-add", "label-remove"},
    "/context": {"add"},
    "/task": {"add", "done", "reopen", "drop", "snooze", "unsnooze", "update", "link", "unlink"},
    "/mail/triage": {"pin", "unpin", "snooze", "unsnooze", "done", "undone",
                     "move", "unmove", "note", "my_day"},
}


class GCError(Exception):
    """A tool-visible failure. The message is shown to the agent verbatim."""


def _timeout_for(path: str) -> float:
    return TIMEOUTS.get(path, DEFAULT_TIMEOUT)

def _check(method: str, path: str, body: dict | None) -> None:
    """Refuse anything not on the allowlist, before a socket is opened.

    Deny-list wins over allow-list, and the action guard runs last so a safe
    route cannot smuggle a dangerous verb.
    """
    base = path.split("?", 1)[0].rstrip("/") or "/"

    for key in (("*", base), (method, base)):
        if key in DENIED:
            raise GCError(
                f"Refused: {method} {base} is not available to agents — {DENIED[key]} "
                "This is a hard limit in the MCP server, not a preference."
            )

    if (method, base) not in ALLOWED:
        raise GCError(
            f"Refused: {method} {base} is not an allowed Grand Central endpoint."
        )

    allowed_actions = ACTION_ALLOW.get(base)
    if allowed_actions is not None and body is not None:
        action = body.get("action")
        if action is not None and action not in allowed_actions:
            raise GCError(
                f"Refused: '{action}' is not an allowed action on {base}. "
                f"Allowed: {', '.join(sorted(allowed_actions))}. "
                "Deleting and trashing are human-only in Grand Central."
            )


def request(method: str, path: str, params: dict | None = None,
            body: dict | None = None, retry: bool = True) -> dict:
    """Call one dashboard endpoint and return its decoded JSON.

    Raises GCError with an actionable message on any failure — the agent must
    never be handed a silent empty result it could mistake for real data.
    """
    _check(method, path, body)

    url = BASE_URL + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean)

    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    timeout = _timeout_for(path.split("?", 1)[0])

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:600]
        except Exception:
            pass
        raise GCError(f"Grand Central returned HTTP {exc.code} for {method} {path}. {detail}".strip())
    except urllib.error.URLError as exc:
        # A dropped connection on the first try is usually the dashboard
        # restarting under launchd KeepAlive; one retry covers it.
        if retry:
            time.sleep(1.0)
            return request(method, path, params=params, body=body, retry=False)
        raise GCError(
            f"Cannot reach the Grand Central dashboard at {BASE_URL} ({exc.reason}). "
            "It runs under launchd as com.example.grandcentral.dashboard — check "
            "`launchctl list | grep grandcentral` and logs/dashboard.err.log."
        )
    except TimeoutError:
        raise GCError(f"Grand Central timed out after {timeout:.0f}s on {method} {path}.")

    if not raw:
        raise GCError(f"Grand Central returned an empty response for {method} {path}.")

    try:
        parsed = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        raise GCError(
            f"Grand Central returned non-JSON for {method} {path}: "
            f"{raw[:200]!r}"
        )

    if isinstance(parsed, dict) and parsed.get("ok") is False:
        raise GCError(f"Grand Central refused {method} {path}: {parsed.get('error', 'no reason given')}")

    return parsed


def get(path: str, **params) -> dict:
    return request("GET", path, params=params)


def post(path: str, **body) -> dict:
    return request("POST", path, body=body)
