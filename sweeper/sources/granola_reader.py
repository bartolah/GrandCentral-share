"""Granola reader (E2-P3, rebuilt E109) — meetings + action-item extraction.

Uses Granola's OFFICIAL public API (docs.granola.ai): GET
https://public-api.granola.ai/v1/notes with a `grn_…` API key from
granola.ai settings — set it as GRANOLA_TOKEN via Configure → Connections.
The original 2026-07 version targeted the app's private endpoint
(api.granola.ai/v2/get-documents) with a supabase.json token; that
endpoint now answers every request "Unsupported client" — HTTP 200 with no
docs — which is why sweeps silently logged 0 meetings for a week.

Flow: list notes created in the lookback window (cursor pagination), then
fetch each note's detail for summary_markdown (the AI notes), attendees,
folder names, and the web link. Notes whose summary isn't processed yet
404 — skipped, they land on a later sweep. Read-only against Granola,
per §6. Extraction is heuristic: checkbox and bullet lines under an
"action items"-ish heading in the summary.

Run standalone:
    cd sweeper
    ../.venv/bin/python -m sources.granola_reader --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone

try:
    from ..common import load_env
except ImportError:  # pragma: no cover - direct-run fallback
    import os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common import load_env

API_URL = "https://public-api.granola.ai/v1/notes"
LOOKBACK_HOURS = 36   # "yesterday's meetings" with margin around midnight
MAX_PAGES = 5         # pagination safety cap (~50 notes/window is plenty)

_ACTION_HEAD = re.compile(r"(action items?|next steps?|to-?dos?|follow-?ups?)", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•]\s*(?:\[[ x]\])?|\[[ x]\]|\d+[.)])\s+(.*)")


def extract_action_items(notes: str, limit: int = 10) -> list:
    """Bullet lines under an action-items-ish heading; plain heuristics."""
    items, in_section = [], False
    for line in (notes or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        is_heading = stripped.startswith("#") or (len(stripped) < 60 and stripped.endswith(":"))
        if is_heading:
            in_section = bool(_ACTION_HEAD.search(stripped))
            continue
        if in_section:
            m = _BULLET.match(line)
            if m:
                text = re.sub(r"\s+", " ", m.group(1)).replace("**", "").strip(" -–")
                if 3 < len(text) < 300:
                    items.append(text)
                    if len(items) >= limit:
                        break
            elif not line[:1].isspace():
                in_section = False  # section ended at the next paragraph
    return items


def read_meetings(env: dict, log=print, hours: int = LOOKBACK_HOURS) -> list:
    """[{id, title, created, notes, action_items[], people[], folders[],
    link}] for recent meetings, via the official public API.

    Raises on missing token / HTTP / auth errors so the orchestrator can
    mark the source absent (spec §3). A note whose AI summary isn't
    processed yet 404s on detail fetch — skipped, caught by a later sweep.
    """
    import requests

    token = env.get("GRANOLA_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GRANOLA_TOKEN not set in config/.env")
    headers = {"Authorization": f"Bearer {token}"}

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    listed, cursor = [], None
    for _ in range(MAX_PAGES):
        params = {"created_after": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(API_URL, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict) or "notes" not in data:
            # the old private endpoint failed this way (200 + {"message":
            # "Unsupported client"}) — surface it, never log a silent 0
            raise RuntimeError(f"granola: unexpected response {str(data)[:120]}")
        listed += data.get("notes") or []
        cursor = data.get("cursor")
        if not data.get("hasMore") or not cursor:
            break

    out = []
    for n in listed:
        nid = str(n.get("id", ""))
        if not nid:
            continue
        try:
            r2 = requests.get(f"{API_URL}/{nid}", headers=headers, timeout=30)
            if r2.status_code == 404:   # summary not processed yet
                continue
            r2.raise_for_status()
            doc = r2.json()
        except requests.RequestException as e:
            log(f"granola: note {nid} detail failed ({e!r}) — skipped")
            continue
        created_raw = doc.get("created_at") or n.get("created_at") or ""
        try:
            created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if created < cutoff:
            continue
        notes = doc.get("summary_markdown") or doc.get("summary_text") or ""
        people = []
        for a in doc.get("attendees") or []:
            p = (a.get("name") or a.get("email") or "").strip()
            if p and p not in people:
                people.append(p)
        out.append({
            "id": nid,
            "title": (doc.get("title") or "Untitled meeting").strip(),
            "created": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "notes": notes,
            "action_items": extract_action_items(notes),
            "people": people[:12],
            "folders": [f.get("name", "") for f in doc.get("folder_membership") or []
                        if f.get("name")],
            "link": doc.get("web_url") or "",
        })
    log(f"granola: {len(out)} meeting(s) in the last {hours}h, "
        f"{sum(len(m['action_items']) for m in out)} action item(s)")
    return out


def get_note(env: dict, note_id: str) -> dict:
    """One meeting's Granola notes — its AI summary (with the action items in
    it), never the transcript. Projects step 5: project Ask reads these for
    meetings filed in the project (BUILD_SPEC §6, 2026-09-30). Read-only."""
    import requests
    token = env.get("GRANOLA_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GRANOLA_TOKEN not set in config/.env")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(note_id or "")):
        raise ValueError("bad Granola note id")
    r = requests.get(f"{API_URL}/{note_id}", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 404:
        raise ValueError("Granola has no processed notes for that meeting yet")
    r.raise_for_status()
    doc = r.json()
    notes = doc.get("summary_markdown") or doc.get("summary_text") or ""
    return {"title": (doc.get("title") or "").strip(), "created": doc.get("created_at") or "",
            "notes": notes[:12000], "action_items": extract_action_items(notes),
            "link": doc.get("web_url") or ""}


def _main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Granola meetings reader (read-only)")
    ap.add_argument("--hours", type=int, default=LOOKBACK_HOURS)
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing (this module never writes)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    meetings = read_meetings(load_env(), hours=args.hours)
    if args.json:
        print(json.dumps(meetings, indent=2, ensure_ascii=False))
        return 0
    for m in meetings:
        print(f"\n[{m['created']}] {m['title']}")
        for a in m["action_items"]:
            print(f"   -> {a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
