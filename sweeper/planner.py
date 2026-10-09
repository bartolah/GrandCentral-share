"""E100 — the morning planner (Casey's #1 pick from the "organize my day"
conversation; the LLM re-ranker queued since E47, finally built).

Once each morning (07:15–08:10 window, inside the mail_sync cycle, BEFORE
notify's brief window), one Sonnet call reads everything the board knows —
today's calendar, due/overdue todos, needs-reply people mail,
waiting-on ages, yesterday's + today's journal — and produces an ordered
plan: ≤3 must-dos with concrete reasons, a should band, an explicit
can-wait, and a one-line theme.

It lands in three places:
  - `day_plans` table → GET /plan → a card atop the Today view
  - the 07:45 iMessage brief (notify.py prepends the musts)
  - seeded into today's journal note (ONLY if the note is still empty —
    Casey's own words are never clobbered)

Regenerate on demand: GET /plan?refresh=1 (also how we test).
Failure is always soft: no plan means the day works exactly as before.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime

import db
from common import load_env, require_env

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1800
WINDOW = ("07:15", "08:10")
BOARD_JSON = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "dashboard", "grand_central.json")

PROMPT = """You are Casey's chief of staff. Given today's raw signals, produce his
day plan. Be concrete and opinionated — reasons reference value, age,
and the free time between events. Do not invent items.

Return ONLY JSON:
{"theme": "<one sentence for the day>",
 "must": [{"title": "...", "why": "..."}],   // <=3, ordered
 "should": [{"title": "...", "why": "..."}], // <=4
 "wait": ["<short item>", ...]}              // explicitly deferred
"""


try:
    import untrusted  # E258: email text is data, never instructions
except ImportError:  # pragma: no cover - package-relative fallback
    from . import untrusted
PROMPT = PROMPT + "\n\n" + untrusted.RULE


def _board() -> dict:
    try:
        with open(BOARD_JSON, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _gather(conn) -> dict:
    b = _board()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    day = next((d for d in (b.get("calendar") or []) if d.get("date") == today), {})
    events = [{"time": e.get("time"), "title": e.get("title")}
              for e in (day.get("events") or [])]
    todos = db.todo_view(conn)
    due = [{"t": t["text"][:80], "due": t.get("due"), "pri": t.get("priority"),
            "account": t.get("account")}
           for t in todos if t.get("due") and t["due"] <= today and not t.get("done_at")][:25]
    needs = [dict(r) for r in conn.execute(
        "SELECT last_from AS who, subject, last_date FROM mail_threads "
        "WHERE in_inbox=1 AND category='people' AND last_direction='inbound' "
        "AND unread=1 ORDER BY last_date DESC LIMIT 15")]
    waiting = [{"who": w.get("who"), "what": (w.get("what") or "")[:60],
                "days": w.get("age_days")}
               for w in (b.get("waiting_on") or [])[:10]]
    y = db.get_day_note(conn, db_yesterday()).get("text") or ""
    t = db.get_day_note(conn, today).get("text") or ""
    return {"date": today,
            "weekday": datetime.now().astimezone().strftime("%A"),
            "events": events, "due_todos": due, "needs_reply": needs,
            "waiting_on": waiting,
            "yesterday_note": y[:1200], "today_note": t[:1200]}


def db_yesterday() -> str:
    from datetime import timedelta
    return (datetime.now().astimezone() - timedelta(days=1)).strftime("%Y-%m-%d")


def plan_text(plan: dict) -> str:
    """The plan as journal-seed / iMessage lines."""
    out = [f"◎ {plan.get('theme','')}".strip()]
    for i, m in enumerate(plan.get("must") or [], 1):
        out.append(f"{i}. {m.get('title','')} — {m.get('why','')}")
    sh = plan.get("should") or []
    if sh:
        out.append("then: " + " · ".join(s.get("title", "") for s in sh))
    return "\n".join(out)


def generate(conn, env: dict | None = None, log=print) -> dict | None:
    """One Sonnet call → stored plan. Raises nothing; returns None on failure."""
    env = env or load_env()
    try:
        import anthropic
        signals = _gather(conn)
        client = anthropic.Anthropic(api_key=require_env(env, "ANTHROPIC_API_KEY"))
        resp = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            output_config={"effort": "medium"},
            system=PROMPT,
            messages=[{"role": "user", "content": untrusted.fence_json(signals, "day_signals")}])
        raw = "".join(p.text for p in resp.content if p.type == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        plan = json.loads(m.group(0)) if m else None
        if not plan or not isinstance(plan.get("must"), list):
            log(f"planner: unusable response ({raw[:120]!r})")
            return None
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO day_plans (date, plan, created_at) "
                "VALUES (?,?,?)",
                (signals["date"], json.dumps(plan), datetime.now().isoformat()))
        # seed the journal only if Casey hasn't written anything yet
        if not (signals["today_note"] or "").strip():
            db.set_day_note(conn, signals["date"], plan_text(plan))
        log(f"planner: plan stored — {len(plan.get('must') or [])} must, "
            f"theme {plan.get('theme','')[:60]!r}")
        return plan
    except Exception as e:
        log(f"planner: FAILED ({e!r})")
        return None


def save(conn, plan: dict, date: str | None = None) -> dict:
    """E112: Casey edits the plan in place — his words replace Sonnet's.
    Shape-validated and capped; stored under the same date key the morning
    run uses, so brief/UI read one plan."""
    date = date or datetime.now().astimezone().strftime("%Y-%m-%d")
    clean = {
        "theme": str(plan.get("theme") or "").strip()[:200],
        "must": [{"title": str(m.get("title") or "").strip()[:200],
                  "why": str(m.get("why") or "").strip()[:300]}
                 for m in (plan.get("must") or [])[:5]
                 if str(m.get("title") or "").strip()],
        "should": [{"title": str(s.get("title") or "").strip()[:200]}
                   for s in (plan.get("should") or [])[:10]
                   if str(s.get("title") or "").strip()],
        "wait": [str(w).strip()[:200] for w in (plan.get("wait") or [])[:10]
                 if str(w).strip()],
        "edited": True,
    }
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO day_plans (date, plan, created_at) "
            "VALUES (?,?,?)",
            (date, json.dumps(clean), datetime.now().isoformat()))
    return clean


def get(conn, date: str | None = None) -> dict | None:
    date = date or datetime.now().astimezone().strftime("%Y-%m-%d")
    r = conn.execute("SELECT plan FROM day_plans WHERE date=?", (date,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["plan"])
    except Exception:
        return None


def run(conn, env: dict, log=print) -> None:
    """mail_sync hook: once per day, in the morning window."""
    now = datetime.now().astimezone()
    hm = now.strftime("%H:%M")
    if not (WINDOW[0] <= hm <= WINDOW[1]):
        return
    if get(conn) is not None:
        return
    generate(conn, env, log=log)
