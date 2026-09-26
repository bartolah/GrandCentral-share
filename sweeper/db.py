"""SQLite store — the system of record.

data/grand_central.db holds engagements (todos), decisions, mail threads and
triage state, dismissals, drafts/outbox and the rest of the runtime state.
Two processes write it: the hourly sweeper and dashboard/serve.py (via
feedback.py) — WAL mode + busy_timeout make that safe. The dashboard renders
grand_central.json, generated from this DB + the readers each sweep; sweeper
and dashboard never talk directly.

Account-first: mail is keyed (account, thread_id) and a todo belongs to exactly
one account (engagements.account). The registry/mapping tables (theaters,
operations, objectives, mappings, corrections, element_tags, op_notes,
metric_history, entity_*, scratch) are RETIRED: no code reads or writes them.
Their DDL stays below so an existing database keeps its history and the change
can be rolled back; dropping them is a separate, irreversible step.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

try:
    from .common import DB_PATH
except ImportError:  # pragma: no cover - direct-run fallback
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from common import DB_PATH

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- ---- registry-owned (rebuilt from YAML each sweep; YAML wins) -----------
CREATE TABLE IF NOT EXISTS theaters (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  role       TEXT,
  small      TEXT,
  warn_days  INTEGER,          -- per-theater override; role default if NULL
  alert_days INTEGER,
  sort       INTEGER           -- registry order, for stable display
);

CREATE TABLE IF NOT EXISTS operations (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  small        TEXT,
  priority     TEXT,            -- now | next | later | NULL
  deadline     TEXT,            -- ISO date, nullable
  sizing_hours INTEGER,
  status       TEXT NOT NULL DEFAULT 'active',  -- active|paused|done|killed
  catch_all    INTEGER NOT NULL DEFAULT 0,
  sort         INTEGER
);

CREATE TABLE IF NOT EXISTS operation_theaters (
  operation_id TEXT NOT NULL,
  theater_id   TEXT NOT NULL,
  PRIMARY KEY (operation_id, theater_id)
);

CREATE TABLE IF NOT EXISTS objectives (
  id             TEXT PRIMARY KEY,
  theater_id     TEXT NOT NULL,
  statement      TEXT NOT NULL,
  horizon        TEXT,
  status         TEXT NOT NULL DEFAULT 'active',  -- active|achieved|abandoned
  metric         TEXT,           -- all NULL => UNMEASURED badge
  unit           TEXT,
  target         REAL,
  current        REAL,
  metric_source  TEXT,           -- attio | manual | computed
  metric_updated TEXT
);

CREATE TABLE IF NOT EXISTS operation_objectives (
  operation_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  PRIMARY KEY (operation_id, objective_id)
);

-- ---- runtime state (DB is the system of record) --------------------------
CREATE TABLE IF NOT EXISTS engagements (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  operation_id TEXT,
  text         TEXT NOT NULL,
  due          TEXT,
  state        TEXT NOT NULL DEFAULT 'open',   -- open|done|dropped
  source       TEXT NOT NULL DEFAULT 'manual', -- manual|granola|email|capture
  created_at   TEXT NOT NULL,
  done_at      TEXT
);

CREATE TABLE IF NOT EXISTS elements (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  source        TEXT NOT NULL,   -- gmail | gcal | slack | granola | attio | ...
  source_ref    TEXT NOT NULL,   -- thread_id for gmail; source-native id
  type          TEXT NOT NULL,   -- email_thread | meeting | file | deal | ...
  title         TEXT,
  people        TEXT,            -- JSON array
  last_activity TEXT,
  link          TEXT,
  account       TEXT,
  UNIQUE (source, source_ref)
);

CREATE TABLE IF NOT EXISTS mappings (
  element_id   INTEGER PRIMARY KEY,
  operation_id TEXT NOT NULL,
  mapped_by    TEXT NOT NULL,    -- rule | model | bart
  confidence   REAL,
  ts           TEXT NOT NULL
);

-- E19: Bart-authored context — the seed corpus for future automations,
-- classifier/draft prompt grounding, and reply suggestions. Each entry is
-- one statement scoped to what it describes (a label, a person, a project,
-- a goal, or global). Nothing consumes these automatically yet; consumers
-- opt in deliberately, entry by kind.
CREATE TABLE IF NOT EXISTS context_notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL DEFAULT 'global',  -- global | label:X | person:X | project:X | goal:X
  kind       TEXT NOT NULL DEFAULT 'note',    -- note | rule | voice | automation
  text       TEXT NOT NULL,
  active     INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  text         TEXT NOT NULL,
  operation_id TEXT,
  blocking     TEXT,
  raised_at    TEXT NOT NULL,
  decided_at   TEXT,
  outcome      TEXT
);

-- Append-only log of Bart's re-file corrections (ground truth; the newest
-- row per thread_id wins — same semantics overrides.json had).
CREATE TABLE IF NOT EXISTS corrections (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id   TEXT NOT NULL,
  account     TEXT,
  project     TEXT NOT NULL,     -- theater id in E2-P1; operation id from E2-P2
  old_project TEXT,
  who         TEXT,
  what        TEXT,
  note        TEXT,
  ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_corrections_thread ON corrections (thread_id, id);

CREATE TABLE IF NOT EXISTS dismissals (
  thread_id TEXT PRIMARY KEY,
  account   TEXT,
  ts        TEXT NOT NULL
);

-- Owner notes per operation (spec §2.3 owner_notes). Runtime-owned: lives
-- here, NOT in the registry tables that get rebuilt from YAML each sweep.
CREATE TABLE IF NOT EXISTS op_notes (
  operation_id TEXT PRIMARY KEY,
  note         TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- Snapshot of each measured objective's `current` per sweep — feeds the
-- strategic screen's trend arrow (latest vs ~7 days ago).
CREATE TABLE IF NOT EXISTS metric_history (
  objective_id TEXT NOT NULL,
  current      REAL NOT NULL,
  ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metric_hist ON metric_history (objective_id, ts);

-- E37: manual progress + a running status log at ANY altitude — a theater
-- (project), an operation, or an objective (goal). `scope` is "<kind>:<id>"
-- with kind in {theater, operation, objective} — the same scope convention
-- context_notes uses. Runtime-owned: the YAML->DB sweep rebuilds the
-- registry tables but never touches these. `progress` is a 0..100 the owner
-- sets by hand (no automation writes it); entity_notes is an append log,
-- rendered newest-id-first.
CREATE TABLE IF NOT EXISTS entity_progress (
  scope      TEXT PRIMARY KEY,     -- "<kind>:<id>"
  progress   INTEGER NOT NULL,     -- 0..100, owner-set
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entity_notes (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT NOT NULL,
  text       TEXT NOT NULL,
  ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_entity_notes ON entity_notes (scope, id);

-- E134: the Braindump — ideas parked OUT of the day-to-day until promoted
-- to a goal/todo (promoted_ref records where one went). Freeform text.
CREATE TABLE IF NOT EXISTS braindump (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  text         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'idea',   -- idea | promoted | archived
  promoted_ref TEXT,                           -- "goal:<id>" | "todo:<id>"
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

-- E144: the Dev request queue — Bart files change requests from the app;
-- Claude Code sessions read the open ones, build/test/ship, and write the
-- outcome back (status + note carry the answer, e.g. the episode id).
CREATE TABLE IF NOT EXISTS dev_requests (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  account    TEXT,                           -- additive; legacy rows stay unassigned and hidden
  text       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'open',   -- open | building | shipped | declined | archived
  note       TEXT,                           -- Claude's response: episode id, reason, questions
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- E188: Web Push subscriptions — one row per device Bart subscribed from.
-- `subscription` is the browser's own JSON (endpoint + keys); we never mint
-- one, and a row is deleted the moment the push service reports it dead.
CREATE TABLE IF NOT EXISTS push_subs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  label        TEXT NOT NULL,          -- "Mac Safari", "iPhone" — Bart's words
  endpoint     TEXT NOT NULL UNIQUE,   -- dedupe: one row per browser endpoint
  subscription TEXT NOT NULL,          -- the full PushSubscription JSON
  created_at   TEXT NOT NULL,
  last_ok      TEXT
);

-- E188: which channel each kind of alert uses. Absent row = the defaults in
-- notify.py, so this table only ever records Bart's deviations.
CREATE TABLE IF NOT EXISTS notify_prefs (
  kind     TEXT PRIMARY KEY,           -- brief | event | wrap | alert
  imessage INTEGER NOT NULL DEFAULT 1,
  push     INTEGER NOT NULL DEFAULT 1
);

-- E134: one freeform scratch pad per project/goal — journal space beside
-- the structured fields, upsert like op_notes.
CREATE TABLE IF NOT EXISTS scratch_pads (
  kind       TEXT NOT NULL,        -- theater | objective
  ref_id     TEXT NOT NULL,
  text       TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (kind, ref_id)
);

-- ---- Mail screen (Epoch 3) ------------------------------------------------
-- mail_threads is owned by sweeper/mail_sync.py (every ~3 min, blind upsert);
-- mail_triage is owned by dashboard/serve.py (Bart's pin/snooze/done state).
-- Split on purpose: the sync job can never clobber triage state.
CREATE TABLE IF NOT EXISTS mail_threads (
  thread_id       TEXT NOT NULL,
  account         TEXT NOT NULL,
  subject         TEXT,
  participants    TEXT,               -- JSON array, same shape as elements.people
  last_from       TEXT,
  last_from_email TEXT,
  last_direction  TEXT,               -- inbound | outbound
  snippet         TEXT,               -- plain text, latest message
  last_date       TEXT,               -- ISO Z
  message_count   INTEGER DEFAULT 1,
  unread          INTEGER DEFAULT 0,  -- any INBOX message lacks \Seen
  starred         INTEGER DEFAULT 0,  -- any INBOX message has \Flagged
  in_inbox        INTEGER DEFAULT 1,  -- still present in the INBOX scan
  bulk            INTEGER DEFAULT 0,
  category        TEXT DEFAULT 'other',  -- people|newsletter|notification|calendar|receipt|other
  attachments     INTEGER NOT NULL DEFAULT 0,  -- messages-with-attachments count (existence only)
  link            TEXT,
  synced_at       TEXT,
  PRIMARY KEY (account, thread_id)
);
CREATE INDEX IF NOT EXISTS idx_mail_threads_date ON mail_threads (last_date DESC);

CREATE TABLE IF NOT EXISTS mail_triage (
  thread_id     TEXT NOT NULL,
  account       TEXT NOT NULL,
  pinned        INTEGER DEFAULT 0,
  snoozed_until TEXT,
  wake_inbox    INTEGER NOT NULL DEFAULT 0,  -- 1 = return to Gmail INBOX when snoozed_until passes (E5-P1)
  done_at       TEXT,
  updated_at    TEXT NOT NULL,
  PRIMARY KEY (account, thread_id)
);

-- Goal tags on elements (E4-P2). Keyed by thread identity like corrections/
-- dismissals — mail_sync never creates elements rows, so a Mail-screen
-- thread can be tagged before the hourly sweep materializes it. `value` is
-- a registry objective id — a SOFT reference (mappings convention): stale
-- ids surface raw in the UI, never crash anything.
CREATE TABLE IF NOT EXISTS element_tags (
  source     TEXT NOT NULL DEFAULT 'gmail',
  source_ref TEXT NOT NULL,           -- gmail thread_id
  kind       TEXT NOT NULL DEFAULT 'objective',
  value      TEXT NOT NULL,           -- objective id
  tagged_by  TEXT NOT NULL DEFAULT 'bart',
  ts         TEXT NOT NULL,
  PRIMARY KEY (source, source_ref, kind, value)
);

-- Scheduled sends (E9, approved by Bart 2026-07-17): a human-confirmed
-- reply waiting for its send time. mail_sync fires due rows through the
-- same send_mail.send_reply path (same caps, same mail_sends audit row).
-- Cancellable while pending; a row stuck in 'sending' means the process
-- died mid-SMTP and is deliberately NOT retried (no double-send risk).
CREATE TABLE IF NOT EXISTS mail_outbox (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  TEXT NOT NULL,
  account    TEXT NOT NULL,
  to_addrs   TEXT NOT NULL,
  cc_addrs   TEXT,
  bcc_addrs  TEXT,
  subject    TEXT,
  body       TEXT NOT NULL,
  send_at    TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending|sending|sent|failed|canceled
  error      TEXT,
  created_at TEXT NOT NULL,
  sent_at    TEXT
);

-- Append-only audit log of every SMTP send (E3-P4; sweeper/send_mail.py).
CREATE TABLE IF NOT EXISTS mail_sends (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  thread_id  TEXT,
  account    TEXT NOT NULL,
  to_addrs   TEXT NOT NULL,
  cc_addrs   TEXT,
  bcc_addrs  TEXT,
  subject    TEXT,
  body_chars INTEGER,
  ts         TEXT NOT NULL
);

-- Ask Grand Central conversations (E25). The panel used to hold the whole
-- transcript in a JS variable, so a reload lost the thread. Now every turn
-- is persisted and past conversations can be reopened. `title` is the first
-- question, trimmed — good enough to recognise a chat in a list.
CREATE TABLE IF NOT EXISTS ask_chats (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  title      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ask_messages (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id INTEGER NOT NULL REFERENCES ask_chats(id) ON DELETE CASCADE,
  role    TEXT NOT NULL,           -- user | assistant
  text    TEXT NOT NULL,
  actions TEXT,                    -- JSON array of tools the turn actually ran
  ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ask_messages_chat ON ask_messages(chat_id, id);

-- Bart-curated senders that are apps/bots, not people (E34) — forces the
-- bundle category so they land in Notifications instead of People. Keyed by
-- lowercased email; category kept for future kinds but is 'notification'
-- today. mail_sync consults this every pass.
CREATE TABLE IF NOT EXISTS sender_categories (
  email    TEXT PRIMARY KEY,
  category TEXT NOT NULL DEFAULT 'notification',
  ts       TEXT NOT NULL
);

-- E69: free-form daily notes — Bart's plan/journal for a day, one row per
-- date. Editable all day from the Today view; later a context source for
-- the LLM layers (steward/re-ranker/brief consumers), by design.
CREATE TABLE IF NOT EXISTS day_notes (
  date       TEXT PRIMARY KEY,   -- YYYY-MM-DD (Bart's local day)
  text       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- E81: thread-body cache — the exact /thread payload for threads active in
-- the last week, so opens are instant (and work offline) instead of a live
-- ~3s IMAP fetch. Warmed by mail_sync, write-through on live opens, pruned
-- after 14 days. last_date is mail_threads.last_date COPIED AT STORE TIME —
-- the staleness token (string equality; sync refreshing it invalidates).
-- Attachments are never stored (existence flags only, as everywhere);
-- oversized payloads drop inline images first, then body_html.
CREATE TABLE IF NOT EXISTS mail_bodies (
  account    TEXT NOT NULL,
  thread_id  TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  last_date  TEXT,
  payload    TEXT NOT NULL,   -- JSON: {messages: [...], labels: [...]}
  PRIMARY KEY (account, thread_id)
);

-- E100: the morning planner's output — one JSON plan per day.
CREATE TABLE IF NOT EXISTS day_plans (
  date       TEXT PRIMARY KEY,
  plan       TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- E101: sent messages already mined for commitments (one look, ever).
CREATE TABLE IF NOT EXISTS commit_scan (
  account TEXT NOT NULL,
  uid     TEXT NOT NULL,
  PRIMARY KEY (account, uid)
);

-- E99: iMessage alert dedup — one row per notification ever sent.
CREATE TABLE IF NOT EXISTS notifications (
  kind    TEXT NOT NULL,
  ref     TEXT NOT NULL,
  sent_at TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);

-- E60: emails grouped under a todo (Shortwave-style "promote to Todo").
-- Many threads to one engagement; completing the todo can then treat the
-- group (archive all) in one gesture. source_ref stays for back-compat as
-- "the email that spawned this"; this table is the real membership.
CREATE TABLE IF NOT EXISTS engagement_threads (
  engagement_id INTEGER NOT NULL,
  account       TEXT NOT NULL,
  thread_id     TEXT NOT NULL,
  added_at      TEXT NOT NULL,
  PRIMARY KEY (engagement_id, account, thread_id)
);

-- Inbox steward (E59): LLM-proposed sender→bundle suggestions, awaiting
-- Bart's one-click verdict. The steward NEVER acts alone — approving a row
-- writes sender_categories exactly like a manual 🤖 click; 'skipped' rows
-- (judged person/unsure) are kept so a sender is never re-audited.
CREATE TABLE IF NOT EXISTS steward_suggestions (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  email      TEXT NOT NULL UNIQUE,   -- lowercased sender address
  who        TEXT,                   -- display name seen
  category   TEXT NOT NULL,          -- suggested bundle (or 'people' for skips)
  why        TEXT,                   -- steward's one-line reason
  evidence   TEXT,                   -- JSON: subjects/snippet/thread count
  status     TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|rejected|skipped
  created_at TEXT NOT NULL,
  decided_at TEXT
);

-- E111: the 🧙 Wizard — every agent-proposed todo waits here for Bart's
-- verdict (steward principle: the agent NEVER acts alone). kinds: 'email'
-- (suggested todo from recent mail), 'granola-item' (explicit action item
-- from a meeting's notes), 'granola' (LLM-implied follow-up from a meeting).
CREATE TABLE IF NOT EXISTS wizard_suggestions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL,
  why           TEXT,
  due           TEXT,
  project       TEXT,               -- suggested operation id
  account       TEXT,               -- source email (with thread_id)
  thread_id     TEXT,
  meeting_ref   TEXT,               -- source Granola note id
  meeting_title TEXT,
  status        TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|dismissed|expired
  todo_id       INTEGER,            -- engagement created on approve
  created_at    TEXT NOT NULL,
  decided_at    TEXT
);

-- One look ever per source item (like commit_scan): a thread or meeting the
-- wizard has already considered is never re-sent to the model.
CREATE TABLE IF NOT EXISTS wizard_scan (
  kind       TEXT NOT NULL,
  ref        TEXT NOT NULL,
  scanned_at TEXT NOT NULL,
  PRIMARY KEY (kind, ref)
);

-- E209: email classification (plan: "Email classification with Jev").
-- Rules, model and Bart's corrections are stored apart and combined on read
-- (correction > model > rule), so no re-run can overwrite his answer. Every
-- row is (account, thread_id): the same Gmail thread id in two accounts is two
-- classifications.
CREATE TABLE IF NOT EXISTS mail_class (
  account       TEXT NOT NULL,
  thread_id     TEXT NOT NULL,
  fingerprint   TEXT NOT NULL,          -- last_date|message_count when classified
  version       TEXT NOT NULL,          -- ruleset version
  roster        TEXT NOT NULL,          -- team-roster hash it was classified against
  rules         TEXT NOT NULL,          -- JSON: the rule fields, reasons, confidence
  model         TEXT,                   -- JSON: respond/priority/summary from a model
  model_source  TEXT,                   -- 'haiku' today; 'jev' from phase 2
  model_at      TEXT,
  classified_at TEXT NOT NULL,
  PRIMARY KEY (account, thread_id)
);
CREATE TABLE IF NOT EXISTS class_feedback (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  account   TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  field     TEXT NOT NULL,
  old_value TEXT,
  new_value TEXT NOT NULL,
  ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_class_feedback ON class_feedback (account, thread_id);
-- The team roster: who someone IS, shared by every account (no mail crosses).
CREATE TABLE IF NOT EXISTS class_people (
  email    TEXT PRIMARY KEY,            -- lowercased
  name     TEXT,
  added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS class_groups (
  domain   TEXT PRIMARY KEY,            -- lowercased, e.g. connectblue.com
  grp      TEXT NOT NULL,               -- e.g. ConnectBlue group
  added_at TEXT NOT NULL
);
-- §6 (2026-09-26): Jev sees an account's mail only while that account's
-- switch is on. No row = off.
CREATE TABLE IF NOT EXISTS class_accounts (
  account    TEXT PRIMARY KEY,
  jev        INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL
);

-- Append-only audit of calendar event creates (E21, approved by Bart
-- 2026-07-19; sweeper/calendar_writer.py via the gog CLI). Create-only:
-- there is deliberately no update/delete verb and no attendee support.
CREATE TABLE IF NOT EXISTS calendar_writes (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  account   TEXT NOT NULL,           -- personal|connectblue nickname
  calendar  TEXT NOT NULL,           -- Google calendarId (usually 'primary')
  event_id  TEXT,                    -- Google event id (null if parse failed)
  summary   TEXT NOT NULL,
  start_at  TEXT NOT NULL,
  end_at    TEXT,
  ts        TEXT NOT NULL
);
"""

# Schema evolution (E4-P1, the repo's first ALTERs): executescript's CREATE
# TABLE IF NOT EXISTS can't add columns to existing tables, so connect()
# also walks this append-only list. Idempotent via PRAGMA table_info.
_MIGRATIONS = [
    # E108: filing to a project can also MOVE the email there — archived in
    # Gmail, out of the Inbox scope, but kept in the feed so project/label
    # views still show it. moved_at is that keep-alive marker.
    ("mail_triage", "moved_at",
     "ALTER TABLE mail_triage ADD COLUMN moved_at TEXT"),
    # E106: subject hint captured at link time — sent-only threads (e.g.
    # commitment mining) may never appear in mail_threads, and without this
    # the todo's email chip reads "(no longer synced)".
    ("engagement_threads", "subject",
     "ALTER TABLE engagement_threads ADD COLUMN subject TEXT"),
    # E91: "Add to my day" on EMAILS (todos had it since E15) — stores the
    # add DATE; only "in My Day" while that date is today, self-resets.
    ("mail_triage", "my_day",
     "ALTER TABLE mail_triage ADD COLUMN my_day TEXT"),
    ("mail_threads", "attachments",
     "ALTER TABLE mail_threads ADD COLUMN attachments INTEGER NOT NULL DEFAULT 0"),
    ("mail_sends", "bcc_addrs",
     "ALTER TABLE mail_sends ADD COLUMN bcc_addrs TEXT"),
    ("mail_triage", "wake_inbox",
     "ALTER TABLE mail_triage ADD COLUMN wake_inbox INTEGER NOT NULL DEFAULT 0"),
    # E10 — first-class todos: attach to a goal, snooze like email, link
    # back to the email that spawned them ("account:thread_id")
    ("engagements", "objective_id",
     "ALTER TABLE engagements ADD COLUMN objective_id TEXT"),
    ("engagements", "snoozed_until",
     "ALTER TABLE engagements ADD COLUMN snoozed_until TEXT"),
    ("engagements", "source_ref",
     "ALTER TABLE engagements ADD COLUMN source_ref TEXT"),
    # E15 — starred (priority flag) + My Day (stores the DATE it was added;
    # it only counts as "today's plan" while that date is today, so My Day
    # resets itself at midnight like MS To-Do)
    ("engagements", "starred",
     "ALTER TABLE engagements ADD COLUMN starred INTEGER NOT NULL DEFAULT 0"),
    ("engagements", "my_day",
     "ALTER TABLE engagements ADD COLUMN my_day TEXT"),
    # E18 — real Gmail user labels per thread (JSON array), synced every pass
    ("mail_threads", "labels",
     "ALTER TABLE mail_threads ADD COLUMN labels TEXT"),
    # E30 — freeform detail on a todo. `text` is the one-line title (300 char
    # cap, what every list renders); `notes` is everything else Bart wants to
    # keep with it — context, links, what "done" means.
    ("engagements", "notes",
     "ALTER TABLE engagements ADD COLUMN notes TEXT"),
    # E168 — recurring todos: completing one spawns its next occurrence.
    # The rule is a small closed vocabulary (see RECUR_RULES), not an RRULE —
    # everything Bart asked for fits, and a closed set can be validated.
    ("engagements", "recur",
     "ALTER TABLE engagements ADD COLUMN recur TEXT"),
    # E52 — labels on todos (JSON array, same convention as mail_threads.labels)
    # so todos organize/sort by label exactly like email in the unified shell.
    ("engagements", "labels",
     "ALTER TABLE engagements ADD COLUMN labels TEXT"),
    # E55 — a short todo-style note stuck on an email thread ("waiting on
    # contract", "reply after Thu"). Local-only like the rest of triage.
    ("mail_triage", "note",
     "ALTER TABLE mail_triage ADD COLUMN note TEXT"),
    # E73 — optional High/Medium/Low priority on todos.
    ("engagements", "priority",
     "ALTER TABLE engagements ADD COLUMN priority TEXT"),
    # E204 — todos live in Gmail's label system (Bart, 2026-09-23): each todo
    # owns one GC/Todo/<name> label; label_synced marks a membership that has
    # been confirmed IN Gmail (written by us or seen there), which is what
    # lets a label removed in Gmail unlink here without racing a failed write.
    ("engagements", "gmail_label",
     "ALTER TABLE engagements ADD COLUMN gmail_label TEXT"),
    ("engagement_threads", "label_synced",
     "ALTER TABLE engagement_threads ADD COLUMN label_synced INTEGER NOT NULL DEFAULT 0"),
    # Account-first (Bart, 2026-09-24): a todo belongs to exactly one mail
    # account. Nullable and additive — rows that predate it stay NULL
    # ("unassigned") until sweeper/backfill_todo_accounts.py assigns them
    # from evidence; nothing is ever guessed and nothing is dropped.
    ("engagements", "account",
     "ALTER TABLE engagements ADD COLUMN account TEXT"),
    # Account-first triage (Bart, 2026-09-25): DevRequests are intake inside
    # the selected account. Historical rows are preserved as NULL and do not
    # appear in any account queue; new rows always carry an explicit owner.
    ("dev_requests", "account",
     "ALTER TABLE dev_requests ADD COLUMN account TEXT"),
    # E210: Jev's answers, kept beside the rules and Haiku so switching an
    # account's Jev off puts the old answers straight back.
    ("mail_class", "jev", "ALTER TABLE mail_class ADD COLUMN jev TEXT"),
    ("mail_class", "jev_fp", "ALTER TABLE mail_class ADD COLUMN jev_fp TEXT"),
    ("mail_class", "jev_at", "ALTER TABLE mail_class ADD COLUMN jev_at TEXT"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, col, ddl in _MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if cols and col not in cols:
            conn.execute(ddl)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the store with WAL + sane concurrency."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(DDL)
    _migrate(conn)
    conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
                 (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


# --------------------------------------------------------------------------
# Dismissals (account-scoped; the table is keyed by thread_id alone, kept as-is
# for rollback, so the account column decides — see is_dismissed)
# --------------------------------------------------------------------------
def load_dismissed(conn: sqlite3.Connection) -> dict:
    return {r["thread_id"]: {"account": r["account"] or "", "ts": r["ts"]}
            for r in conn.execute("SELECT * FROM dismissals")}


def is_dismissed(conn: sqlite3.Connection, account: str, thread_id: str) -> bool:
    """Was this thread dismissed *in this account*? The dismissals table is
    keyed by thread_id alone (legacy, kept for rollback), so its account
    column decides: a row scoped to another account never suppresses this
    one. Legacy rows with no account still match any account."""
    row = conn.execute("SELECT account FROM dismissals WHERE thread_id = ?",
                       (thread_id,)).fetchone()
    return bool(row) and (not row["account"] or row["account"] == account)


def dismissed_keys(conn: sqlite3.Connection, records: list) -> set:
    """{(account, thread_id)} of `records` that are dismissed in their own
    account (see is_dismissed)."""
    dm = load_dismissed(conn)
    return {(r.get("account", ""), r["thread_id"]) for r in records
            if r.get("thread_id") in dm
            and (not dm[r["thread_id"]]["account"]
                 or dm[r["thread_id"]]["account"] == r.get("account", ""))}


def snoozed_keys(conn: sqlite3.Connection) -> set:
    """{(account, thread_id)} with an active snooze (mail_triage is keyed
    by account already)."""
    return {(r["account"], r["thread_id"]) for r in conn.execute(
        "SELECT thread_id, account FROM mail_triage "
        "WHERE snoozed_until IS NOT NULL AND snoozed_until > ?", (_now_iso(),))}


def record_dismissal(conn: sqlite3.Connection, row: dict) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO dismissals (thread_id, account, ts) VALUES (?, ?, ?)",
            (row["thread_id"], row.get("account", ""), row.get("ts") or _now_iso()))


# --------------------------------------------------------------------------
# Engagements (tasks) — Grand Central is the task system of record (§2.4)
# --------------------------------------------------------------------------
_ACCOUNT_RE = re.compile(r"^[a-z0-9_]{1,40}$")


def clean_account(account) -> str:
    """Normalise an account slug (the GMAIL_<STEM>_ stem, lowercased) or raise."""
    a = str(account or "").strip().lower()
    if not _ACCOUNT_RE.match(a):
        raise ValueError("a valid account is required")
    return a


def _ref_account(source_ref: str | None) -> str | None:
    """The account named by a "<account>:<thread_id>" source_ref, if any."""
    head, sep, tail = str(source_ref or "").partition(":")
    return head.lower() if sep and tail.isdigit() and _ACCOUNT_RE.match(head.lower()) else None


def add_engagement(conn: sqlite3.Connection, text: str,
                   due: str | None = None, source: str = "manual",
                   dedupe: bool = False,
                   source_ref: str | None = None,
                   notes: str | None = None, recur: str | None = None,
                   account: str | None = None) -> int | None:
    """Insert a todo in ONE account; returns its id. With dedupe=True
    (imports), an existing non-dropped todo with the same account+source+text
    is not re-created (returns None) — this is what makes the MS To-Do import
    idempotent. The account is required; when the todo is spawned by an email
    (source_ref "account:thread_id") it is derived from it, and a mismatch is
    an error — a todo can never be filed under an account its email isn't in.
    (The legacy operation_id/objective_id columns are never written.)"""
    text = " ".join(str(text or "").split())[:300]
    if not text:
        raise ValueError("empty task text")
    ref_acct = _ref_account(source_ref)
    if account:
        account = clean_account(account)
        if ref_acct and ref_acct != account:
            raise ValueError(f"todo account {account!r} does not match its email's "
                             f"account {ref_acct!r}")
    else:
        account = ref_acct
    if not account:
        raise ValueError("a todo needs an account")
    with conn:
        if dedupe:
            row = conn.execute(
                "SELECT id FROM engagements WHERE account = ? AND source = ? "
                "AND text = ? AND state != 'dropped'",
                (account, source, text)).fetchone()
            if row:
                return None
        cur = conn.execute(
            "INSERT INTO engagements (account, text, due, state, "
            "source, source_ref, notes, recur, created_at) "
            "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)",
            (account, text, due, source, source_ref,
             (notes or None), (recur if recur in RECUR_RULES else None), _now_iso()))
        return cur.lastrowid


def update_engagement(conn: sqlite3.Connection, eng_id: int, fields: dict) -> bool:
    """Edit a todo in place (E10): any subset of text, due, snoozed_until,
    starred, my_day, notes, priority, recur, labels, account. Empty string
    clears a nullable field; starred is coerced to 0/1. Project/goal fields
    are retired (account-first) and ignored. `account` may only be set when
    every email already grouped under the todo lives in that account."""
    allowed = {k: (fields[k] or None) for k in
               ("text", "due", "snoozed_until", "my_day", "notes")
               if k in fields}
    if "account" in fields:
        acct = clean_account(fields["account"])
        foreign = conn.execute(
            "SELECT 1 FROM engagement_threads WHERE engagement_id = ? AND account != ? "
            "LIMIT 1", (int(eng_id), acct)).fetchone()
        if foreign:
            raise ValueError("this todo has emails from another account; "
                             "unlink them before moving it")
        allowed["account"] = acct
    if "starred" in fields:
        allowed["starred"] = 1 if fields["starred"] in (1, "1", True, "true") else 0
    if "priority" in fields:
        # E73: high|medium|low; anything else clears.
        p=str(fields["priority"] or "").lower()
        allowed["priority"]=p if p in ("high","medium","low") else None
    if "recur" in fields:
        # E168: one of RECUR_RULES; anything else stops the repetition.
        r = str(fields["recur"] or "").lower()
        allowed["recur"] = r if r in RECUR_RULES else None
    if "labels" in fields:
        # E52: stored as a JSON array like mail_threads.labels; [] clears.
        labs = fields["labels"] if isinstance(fields["labels"], list) else []
        labs = [" ".join(str(l).split())[:60] for l in labs if str(l).strip()][:20]
        allowed["labels"] = json.dumps(labs) if labs else None
    if "text" in allowed and allowed["text"]:
        allowed["text"] = " ".join(str(allowed["text"]).split())[:300]
    if not allowed:
        raise ValueError("no fields to update")
    assignments = ", ".join(f"{k} = ?" for k in allowed)
    with conn:
        cur = conn.execute(
            f"UPDATE engagements SET {assignments} WHERE id = ?",
            list(allowed.values()) + [int(eng_id)])
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# E168 — recurring todos
#
# A repeat is a rule on the todo, not a generated series: only ONE open
# occurrence exists at a time, and completing it writes the next. That keeps
# the Todos list honest (no calendar of phantom future rows) and makes
# "stop repeating" a single field edit.
#
# The next date counts from the todo's DUE date when it has one, so a weekly
# Monday task stays on Mondays even when ticked off on Wednesday; an undated
# repeat counts from the day it was completed. Either way the result is rolled
# forward past today, so finishing a 45-day-late daily chore gives you
# tomorrow, not another overdue row.
# ---------------------------------------------------------------------------
RECUR_RULES = ("daily", "weekdays", "weekly", "biweekly", "monthly", "yearly")
RECUR_LABEL = {"daily": "every day", "weekdays": "every weekday",
               "weekly": "every week", "biweekly": "every 2 weeks",
               "monthly": "every month", "yearly": "every year"}


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    last = [31, 29 if (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)) else 28,
            31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return date(y, m, min(d.day, last))


def _step(d: date, rule: str) -> date:
    if rule == "daily":
        return d + timedelta(days=1)
    if rule == "weekdays":
        nxt = d + timedelta(days=1)
        while nxt.weekday() >= 5:          # Sat/Sun
            nxt += timedelta(days=1)
        return nxt
    if rule == "weekly":
        return d + timedelta(days=7)
    if rule == "biweekly":
        return d + timedelta(days=14)
    if rule == "monthly":
        return _add_months(d, 1)
    if rule == "yearly":
        return _add_months(d, 12)
    raise ValueError(f"bad recur rule {rule!r}")


def next_due(base: str | None, rule: str, today: str | None = None) -> str | None:
    """The next occurrence's due date: one step past `base`, rolled forward
    past today. Returns None when the rule is unknown."""
    if rule not in RECUR_RULES:
        return None
    today_d = date.fromisoformat(today or datetime.now().strftime("%Y-%m-%d"))
    try:
        d = date.fromisoformat((base or "")[:10])
    except ValueError:
        d = today_d
    if rule in ("monthly", "yearly"):
        # Count months from the ORIGINAL date, never from the clamped result:
        # stepping Jan 31 one month at a time would land on Feb 28 and then
        # stay on the 28th forever. The 31st is the anchor; short months
        # borrow their last day and the next long month restores it.
        step = 1 if rule == "monthly" else 12
        n = 1
        while n < 500:
            nxt = _add_months(d, step * n)
            if nxt > today_d:
                return nxt.isoformat()
            n += 1
        return _add_months(d, step).isoformat()
    d = _step(d, rule)
    guard = 0
    while d <= today_d and guard < 500:     # a long-late repeat resumes ahead
        d = _step(d, rule)
        guard += 1
    return d.isoformat()


def complete_with_recurrence(conn: sqlite3.Connection, ids: list, state: str = "done") -> dict:
    """Mark todos done/dropped and, for any that repeat, open the next one.

    Only a todo that was actually OPEN spawns a successor, so ticking an
    already-done row (or a double click) can never fan out into duplicates.
    """
    ids = [int(i) for i in ids]
    if not 1 <= len(ids) <= 50:
        raise ValueError("ids must be a list of 1-50 task ids")
    marks = ",".join("?" * len(ids))
    rows = [dict(r) for r in conn.execute(
        f"SELECT id, account, text, due, notes, labels, priority, "
        f"recur, state, source FROM engagements WHERE id IN ({marks})", ids)]
    changed = set_engagements_state(conn, ids, state)
    spawned = []
    if state == "done":
        for r in rows:
            if r["state"] != "open" or r["recur"] not in RECUR_RULES:
                continue
            nd = next_due(r["due"], r["recur"])
            with conn:
                cur = conn.execute(
                    "INSERT INTO engagements (account, text, due, state, "
                    "source, notes, labels, priority, recur, created_at) "
                    "VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                    (r["account"], r["text"], nd,
                     r["source"] or "manual", r["notes"], r["labels"], r["priority"],
                     r["recur"], _now_iso()))
            spawned.append({"id": cur.lastrowid, "text": r["text"], "due": nd,
                            "recur": r["recur"]})
    return {"changed": changed, "spawned": spawned}


# ---------------------------------------------------------------------------
# E188 — push subscriptions and per-alert channels
# ---------------------------------------------------------------------------
def push_subs(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, label, endpoint, subscription, created_at, last_ok "
        "FROM push_subs ORDER BY id")]


def push_sub_add(conn: sqlite3.Connection, label: str, subscription: dict) -> int:
    endpoint = str((subscription or {}).get("endpoint") or "").strip()
    if not endpoint.startswith("https://"):
        raise ValueError("a push subscription needs an https endpoint")
    label = " ".join(str(label or "this device").split())[:60]
    with conn:
        cur = conn.execute(
            "INSERT INTO push_subs (label, endpoint, subscription, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(endpoint) DO UPDATE SET "
            "label = excluded.label, subscription = excluded.subscription",
            (label, endpoint, json.dumps(subscription), _now_iso()))
        row = conn.execute("SELECT id FROM push_subs WHERE endpoint = ?", (endpoint,)).fetchone()
    return int(row["id"]) if row else int(cur.lastrowid or 0)


def push_sub_delete(conn: sqlite3.Connection, sub_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM push_subs WHERE id = ?", (int(sub_id),))
    return cur.rowcount > 0


NOTIFY_KINDS = ("brief", "event", "wrap", "alert")


def notify_prefs(conn: sqlite3.Connection) -> dict:
    """{kind: {imessage: bool, push: bool}} — defaults to both channels on."""
    out = {k: {"imessage": True, "push": True} for k in NOTIFY_KINDS}
    for r in conn.execute("SELECT kind, imessage, push FROM notify_prefs"):
        if r["kind"] in out:
            out[r["kind"]] = {"imessage": bool(r["imessage"]), "push": bool(r["push"])}
    return out


def notify_pref_set(conn: sqlite3.Connection, kind: str, imessage: bool, push: bool) -> bool:
    if kind not in NOTIFY_KINDS:
        raise ValueError(f"unknown alert kind {kind!r}")
    with conn:
        conn.execute(
            "INSERT INTO notify_prefs (kind, imessage, push) VALUES (?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET imessage = excluded.imessage, push = excluded.push",
            (kind, 1 if imessage else 0, 1 if push else 0))
    return True


def todo_view(conn: sqlite3.Connection, done_days: int = 3,
              account: str | None = None) -> list:
    """The Todos screen's list (E10): every open todo (snoozed included —
    the UI sections them) plus the last few days of done ones. DB-backed like
    the Mail screen. Account-first: each row carries its `account`, and
    `account=` returns only that account's todos (a legacy unassigned todo has
    account None and matches no account filter). A todo's emails are only ever
    the members from its own account."""
    now = _now_iso()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=done_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT g.id, g.text, g.due, g.state, "
        "  g.source, g.source_ref, g.created_at, g.done_at, g.snoozed_until, "
        "  g.starred, g.my_day, g.notes, g.labels, g.priority, g.recur, g.gmail_label, "
        "  g.account "
        "FROM engagements g "
        "WHERE (g.state = 'open' OR (g.state = 'done' AND g.done_at >= ?)) "
        + ("AND g.account = ? " if account else "") +
        "ORDER BY g.state = 'done', g.due IS NULL, g.due, g.id",
        [cutoff] + ([clean_account(account)] if account else []))
    thr = threads_for_todos(conn)   # E60: grouped emails per todo
    out = []
    for r in rows:
        d = dict(r)
        d["snoozed_active"] = bool(d["snoozed_until"] and d["snoozed_until"] > now)
        d["snooze_returned"] = bool(d["snoozed_until"] and d["snoozed_until"] <= now)
        try:
            d["labels"] = json.loads(d.get("labels") or "[]")
        except ValueError:
            d["labels"] = []
        # boundary: only members from the todo's own account are ever shown
        d["threads"] = [t for t in thr.get(d["id"], [])
                        if not d["account"] or t["account"] == d["account"]]
        out.append(d)
    return out


def set_engagement_state(conn: sqlite3.Connection, eng_id: int, state: str) -> bool:
    if state not in ("open", "done", "dropped"):
        raise ValueError(f"bad state {state!r}")
    with conn:
        cur = conn.execute(
            "UPDATE engagements SET state = ?, done_at = ? WHERE id = ?",
            (state, _now_iso() if state == "done" else None, int(eng_id)))
    return cur.rowcount > 0


def set_engagements_state(conn: sqlite3.Connection, ids: list, state: str) -> int:
    """Bulk state change (E5-P3) — one transaction, returns rows changed.
    Mirrors the Mail screen's bundle archive: explicit ids only, never a
    query-shaped 'all matching' verb."""
    if state not in ("open", "done", "dropped"):
        raise ValueError(f"bad state {state!r}")
    ids = [int(i) for i in ids]
    if not 1 <= len(ids) <= 50:
        raise ValueError("ids must be a list of 1-50 task ids")
    marks = ",".join("?" * len(ids))
    with conn:
        cur = conn.execute(
            f"UPDATE engagements SET state = ?, done_at = ? WHERE id IN ({marks})",
            [state, _now_iso() if state == "done" else None] + ids)
    return cur.rowcount


def open_engagements(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, account, text, due, source, created_at FROM engagements "
        "WHERE state = 'open' ORDER BY due IS NULL, due, id")]


# --------------------------------------------------------------------------
# Decisions — choices only Bart can make, never mixed into task lists (§2.6)
# --------------------------------------------------------------------------
def add_decision(conn: sqlite3.Connection, text: str,
                 blocking: str | None = None) -> int:
    text = " ".join(str(text or "").split())[:300]
    if not text:
        raise ValueError("empty decision text")
    with conn:
        cur = conn.execute(
            "INSERT INTO decisions (text, blocking, raised_at) VALUES (?, ?, ?)",
            (text, " ".join(str(blocking or "").split())[:300] or None, _now_iso()))
        return cur.lastrowid


def decide_decision(conn: sqlite3.Connection, dec_id: int, outcome: str) -> bool:
    outcome = " ".join(str(outcome or "").split())[:300]
    if not outcome:
        raise ValueError("empty outcome")
    with conn:
        cur = conn.execute(
            "UPDATE decisions SET decided_at = ?, outcome = ? WHERE id = ? AND decided_at IS NULL",
            (_now_iso(), outcome, int(dec_id)))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Context notes (E19) — Bart's rules/notes corpus for future consumers
# --------------------------------------------------------------------------
def list_context(conn: sqlite3.Connection, scope_prefix: str | None = None,
                 active_only: bool = False) -> list:
    sql = "SELECT * FROM context_notes"
    where, params = [], []
    if scope_prefix:
        where.append("scope LIKE ?")
        params.append(scope_prefix + "%")
    if active_only:
        where.append("active = 1")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scope, id"
    return [dict(r) for r in conn.execute(sql, params)]


def add_context(conn: sqlite3.Connection, scope: str, kind: str, text: str) -> int:
    if kind not in ("note", "rule", "voice", "automation"):
        raise ValueError(f"bad kind {kind!r}")
    text = " ".join(str(text or "").split())[:500]
    if not text:
        raise ValueError("empty context text")
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO context_notes (scope, kind, text, active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (" ".join(str(scope or "global").split())[:80] or "global", kind, text, now, now))
    return cur.lastrowid


def update_context(conn: sqlite3.Connection, note_id: int, fields: dict) -> bool:
    allowed = {}
    if "text" in fields:
        allowed["text"] = " ".join(str(fields["text"] or "").split())[:500]
        if not allowed["text"]:
            raise ValueError("empty context text")
    if "scope" in fields:
        allowed["scope"] = " ".join(str(fields["scope"] or "").split())[:80] or "global"
    if "kind" in fields and fields["kind"] in ("note", "rule", "voice", "automation"):
        allowed["kind"] = fields["kind"]
    if "active" in fields:
        allowed["active"] = 1 if fields["active"] in (1, "1", True, "true") else 0
    if not allowed:
        raise ValueError("no fields to update")
    allowed["updated_at"] = _now_iso()
    sets = ", ".join(f"{k} = ?" for k in allowed)
    with conn:
        cur = conn.execute(f"UPDATE context_notes SET {sets} WHERE id = ?",
                           list(allowed.values()) + [int(note_id)])
    return cur.rowcount > 0


def delete_context(conn: sqlite3.Connection, note_id: int) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM context_notes WHERE id = ?", (int(note_id),))
    return cur.rowcount > 0


def open_decisions(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT id, text, blocking, raised_at FROM decisions "
        "WHERE decided_at IS NULL ORDER BY raised_at")]


# ---- E134: braindump + scratch pads ---------------------------------------
def braindump_list(conn: sqlite3.Connection) -> list:
    """Everything but archived, ideas first, newest first within status."""
    return [dict(r) for r in conn.execute(
        "SELECT id, text, status, promoted_ref, created_at, updated_at "
        "FROM braindump WHERE status != 'archived' "
        "ORDER BY CASE status WHEN 'idea' THEN 0 ELSE 1 END, id DESC")]


def braindump_add(conn: sqlite3.Connection, text: str) -> int:
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO braindump (text, created_at, updated_at) VALUES (?, ?, ?)",
            (text, now, now))
    return cur.lastrowid


def braindump_update(conn: sqlite3.Connection, bid: int, text: str | None = None,
                     status: str | None = None, promoted_ref: str | None = None) -> bool:
    sets, vals = [], []
    if text is not None:
        sets.append("text = ?"); vals.append(text)
    if status in ("idea", "promoted", "archived"):
        sets.append("status = ?"); vals.append(status)
    if promoted_ref is not None:
        sets.append("promoted_ref = ?"); vals.append(promoted_ref)
    if not sets:
        return False
    sets.append("updated_at = ?"); vals.append(_now_iso())
    with conn:
        cur = conn.execute(
            f"UPDATE braindump SET {', '.join(sets)} WHERE id = ?", vals + [int(bid)])
    return cur.rowcount > 0


def dev_list(conn: sqlite3.Connection, account: str) -> list:
    """One account's non-archived requests; never return ownerless legacy rows."""
    account = clean_account(account)
    return [dict(r) for r in conn.execute(
        "SELECT id, account, text, status, note, created_at, updated_at "
        "FROM dev_requests WHERE status != 'archived' AND account = ? "
        "ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'building' THEN 1 ELSE 2 END, id DESC",
        (account,))]


def dev_add(conn: sqlite3.Connection, text: str, account: str) -> int:
    account = clean_account(account)
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO dev_requests (account, text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)", (account, text, now, now))
    return cur.lastrowid


def dev_update(conn: sqlite3.Connection, rid: int, account: str,
               text: str | None = None, status: str | None = None,
               note: str | None = None) -> bool:
    account = clean_account(account)
    sets, vals = [], []
    if text is not None:
        sets.append("text = ?"); vals.append(text)
    if status in ("open", "building", "shipped", "declined", "archived"):
        sets.append("status = ?"); vals.append(status)
    if note is not None:
        sets.append("note = ?"); vals.append(note)
    if not sets:
        return False
    sets.append("updated_at = ?"); vals.append(_now_iso())
    with conn:
        cur = conn.execute(
            f"UPDATE dev_requests SET {', '.join(sets)} WHERE id = ? AND account = ?",
            vals + [int(rid), account])
    return cur.rowcount > 0


def record_granola_meetings(conn: sqlite3.Connection, meetings: list,
                            log=print) -> None:
    """Store meetings as elements and their action items as wizard
    suggestions (idempotent by text). Nothing is filed under a project or
    account here: suggestions wait for Bart's approval, which requires an
    account."""
    created = 0
    with conn:
        for m in meetings:
            conn.execute(
                "INSERT INTO elements (source, source_ref, type, title, people, "
                "  link, last_activity) "
                "VALUES ('granola', ?, 'meeting', ?, ?, ?, ?) "
                "ON CONFLICT (source, source_ref) DO UPDATE SET "
                "  title = excluded.title, people = excluded.people, "
                "  link = excluded.link, last_activity = excluded.last_activity",
                (m["id"] or m["title"], m["title"],
                 json.dumps(m.get("people") or [], ensure_ascii=False),
                 m.get("link") or "", m["created"]))
            for item in m.get("action_items", []):
                # E109: Granola attributes items "(Name)" — only Bart's own
                # (or unattributed) reach his board; other people's balls
                # stay in the meeting notes. E111: they land as WIZARD
                # suggestions awaiting his ✓, not as silent todos.
                owner = re.search(r"\(([^()]{2,60})\)\s*$", item)
                if owner and "bart" not in owner.group(1).lower():
                    continue
                if add_wizard_suggestion(
                        conn, "granola-item", item,
                        why="Listed as an action item in the meeting notes",
                        meeting_ref=m.get("id"),
                        meeting_title=m["title"]):
                    created += 1
    if created:
        log(f"granola: {created} action item(s) → wizard suggestions")


# --------------------------------------------------------------------------
# Mail screen (Epoch 3): mail_threads upserts + the /mail view
# --------------------------------------------------------------------------
_ADDR_RE = re.compile(r"^(.*?)\s*<([^>]+)>$")


def _display_name(addr: str) -> str:
    """'Jane Doe <jd@x.com>' -> 'Jane Doe'; bare address -> local part.
    Same behavior as build_state's helper, inlined to keep db.py
    dependency-light."""
    addr = (addr or "").strip()
    m = _ADDR_RE.match(addr)
    if m and m.group(1).strip():
        return m.group(1).strip().strip('"')
    if m:
        return m.group(2).split("@")[0]
    return addr.split("@")[0] if "@" in addr else addr


def upsert_mail_threads(conn: sqlite3.Connection, account: str, rows: list) -> None:
    """Blind-upsert one account's INBOX picture from mail_sync (single txn).

    - A row with snippet=None keeps the previously stored snippet (the sync
      job only re-fetches bodies for new/changed threads).
    - Rows of this account NOT seen this run flip in_inbox=0 (archived, or
      aged out of the lookback scan — indistinguishable, both leave the view).
    - New mail on a done thread resurrects it (done_at cleared).
    """
    now = _now_iso()
    seen = [r["thread_id"] for r in rows]
    with conn:
        for r in rows:
            conn.execute(
                "INSERT INTO mail_threads (thread_id, account, subject, participants, "
                "  last_from, last_from_email, last_direction, snippet, last_date, "
                "  message_count, unread, starred, in_inbox, bulk, category, "
                "  attachments, link, labels, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (account, thread_id) DO UPDATE SET "
                "  subject = excluded.subject, participants = excluded.participants, "
                "  last_from = excluded.last_from, last_from_email = excluded.last_from_email, "
                "  last_direction = excluded.last_direction, "
                "  snippet = COALESCE(excluded.snippet, mail_threads.snippet), "
                "  last_date = excluded.last_date, message_count = excluded.message_count, "
                "  unread = excluded.unread, starred = excluded.starred, in_inbox = 1, "
                "  bulk = excluded.bulk, category = excluded.category, "
                "  attachments = excluded.attachments, "
                "  link = excluded.link, labels = excluded.labels, "
                "  synced_at = excluded.synced_at",
                (r["thread_id"], account, r.get("subject", ""),
                 json.dumps(r.get("participants", [])[:12], ensure_ascii=False),
                 r.get("last_from", ""), r.get("last_from_email", ""),
                 r.get("last_direction", ""), r.get("snippet"), r.get("last_date", ""),
                 r.get("message_count", 1), 1 if r.get("unread") else 0,
                 1 if r.get("starred") else 0, 1 if r.get("bulk") else 0,
                 r.get("category", "other"), r.get("attachments", 0),
                 r.get("link", ""),
                 json.dumps(r.get("labels") or [], ensure_ascii=False), now))
        if seen:
            marks = ",".join("?" * len(seen))
            conn.execute(
                f"UPDATE mail_threads SET in_inbox = 0, synced_at = ? "
                f"WHERE account = ? AND in_inbox = 1 AND thread_id NOT IN ({marks})",
                [now, account] + seen)
        else:
            conn.execute(
                "UPDATE mail_threads SET in_inbox = 0, synced_at = ? "
                "WHERE account = ? AND in_inbox = 1", (now, account))
        conn.execute(
            "UPDATE mail_triage SET done_at = NULL, updated_at = ? "
            "WHERE account = ? AND done_at IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM mail_threads mt WHERE mt.account = mail_triage.account "
            "  AND mt.thread_id = mail_triage.thread_id AND mt.last_date > mail_triage.done_at)",
            (now, account))
        # E108: new INCOMING mail on a filed-and-moved thread brings it back
        # to the Inbox — Gmail re-inboxes it, and clearing moved_at lets the
        # client show it there again. in_inbox=1 is the guard: Bart's own
        # reply bumps last_date via the Sent merge WITHOUT re-inboxing, and
        # must not clear the flag (the thread would drop out of the feed).
        conn.execute(
            "UPDATE mail_triage SET moved_at = NULL, updated_at = ? "
            "WHERE account = ? AND moved_at IS NOT NULL AND EXISTS ("
            "  SELECT 1 FROM mail_threads mt WHERE mt.account = mail_triage.account "
            "  AND mt.thread_id = mail_triage.thread_id AND mt.in_inbox = 1 "
            "  AND mt.last_date > mail_triage.moved_at)",
            (now, account))


def set_mail_triage(conn: sqlite3.Connection, thread_id: str, account: str,
                    fields: dict) -> None:
    """Upsert Bart's local triage state for one thread. fields may set
    pinned (0/1), snoozed_until (ISO Z or None), wake_inbox (0/1), done_at
    (ISO Z or None). Local-only — Gmail writes (archive on snooze, re-inbox
    on wake) are composed by the caller."""
    sets = {k: fields[k] for k in ("pinned", "snoozed_until", "wake_inbox", "done_at", "note", "my_day", "moved_at") if k in fields}
    if not sets:
        raise ValueError("no triage fields to set")
    now = _now_iso()
    with conn:
        conn.execute(
            "INSERT INTO mail_triage (thread_id, account, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (account, thread_id) DO NOTHING",
            (thread_id, account, now))
        assignments = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(
            f"UPDATE mail_triage SET {assignments}, updated_at = ? "
            "WHERE account = ? AND thread_id = ?",
            list(sets.values()) + [now, account, thread_id])


def mail_categories(conn: sqlite3.Connection) -> dict:
    """{(account, thread_id): category} for every inbox thread mail_sync has
    categorized (people|calendar|receipt|newsletter|notification). E39: the
    hourly sweep reads this so reply_debt can owe replies to people only —
    the same category the Mail screen shows, honoring Bart's sender overrides,
    so there's one categorization, not two."""
    return {(r["account"], r["thread_id"]): r["category"]
            for r in conn.execute(
                "SELECT account, thread_id, category FROM mail_threads "
                "WHERE in_inbox = 1")}


def due_snoozes(conn: sqlite3.Connection) -> list:
    """Snoozed threads whose wake time has passed and that Grand Central
    archived at snooze time (wake_inbox=1) — mail_sync's wake pass returns
    these to the Gmail INBOX (E5-P1, §6 amendment 2026-07-15)."""
    return [dict(r) for r in conn.execute(
        "SELECT thread_id, account, snoozed_until FROM mail_triage "
        "WHERE wake_inbox = 1 AND snoozed_until IS NOT NULL AND snoozed_until <= ?",
        (_now_iso(),))]


_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def sender_priors(conn: sqlite3.Connection) -> dict:
    """E47: learn who Bart engages vs ignores, from his own actions.

    Two clean, opposite signals — the strongest expressions of preference the
    board captures:
      • replies  (mail_sends.to_addrs): people he wrote to → he cares.
      • dismisses (dismissals → the thread's sender): senders he clears off
        the board as noise → he doesn't.
    Returned per exact email AND rolled up per domain (the scorer falls back to
    the domain when it hasn't seen the exact address). Counts only — the scorer
    owns the weights.
    """
    def _dom(e):
        return e.rsplit("@", 1)[-1] if "@" in e else ""

    reply_email, reply_dom = {}, {}
    for r in conn.execute("SELECT to_addrs FROM mail_sends"):
        for e in {m.group(0).lower() for m in _EMAIL_RE.finditer(r["to_addrs"] or "")}:
            reply_email[e] = reply_email.get(e, 0) + 1
            reply_dom[_dom(e)] = reply_dom.get(_dom(e), 0) + 1

    dis_email, dis_dom = {}, {}
    for r in conn.execute(
            "SELECT mt.last_from_email AS e FROM dismissals d "
            "JOIN mail_threads mt ON d.thread_id = mt.thread_id "
            "WHERE mt.last_from_email IS NOT NULL AND mt.last_from_email != ''"):
        e = (r["e"] or "").lower()
        if not e:
            continue
        dis_email[e] = dis_email.get(e, 0) + 1
        dis_dom[_dom(e)] = dis_dom.get(_dom(e), 0) + 1

    return {"reply_email": reply_email, "reply_dom": reply_dom,
            "dismiss_email": dis_email, "dismiss_dom": dis_dom}


def record_send(conn: sqlite3.Connection, thread_id: str, account: str,
                to_addrs: str, cc_addrs: str = "", subject: str = "",
                body_chars: int = 0, bcc_addrs: str = "") -> None:
    """Append one row to the send audit log (every SMTP send, no exceptions)."""
    with conn:
        conn.execute(
            "INSERT INTO mail_sends (thread_id, account, to_addrs, cc_addrs, "
            "bcc_addrs, subject, body_chars, ts) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, account, to_addrs, cc_addrs, bcc_addrs, subject,
             int(body_chars), _now_iso()))


# --------------------------------------------------------------------------
# Scheduled sends (E9) — the outbox queue
# --------------------------------------------------------------------------
def add_outbox(conn: sqlite3.Connection, thread_id: str, account: str,
               to_addrs: str, body: str, send_at: str,
               cc_addrs: str = "", bcc_addrs: str = "", subject: str = "") -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO mail_outbox (thread_id, account, to_addrs, cc_addrs, "
            "bcc_addrs, subject, body, send_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (thread_id, account, to_addrs, cc_addrs, bcc_addrs, subject,
             body, send_at, _now_iso()))
        return cur.lastrowid


def due_outbox(conn: sqlite3.Connection) -> list:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM mail_outbox WHERE status = 'pending' AND send_at <= ? "
        "ORDER BY send_at", (_now_iso(),))]


def open_outbox(conn: sqlite3.Connection) -> list:
    """Pending + recently-failed/stuck rows for the Mail screen banner."""
    return [dict(r) for r in conn.execute(
        "SELECT id, thread_id, account, to_addrs, subject, send_at, status, error "
        "FROM mail_outbox WHERE status IN ('pending', 'sending', 'failed') "
        "ORDER BY send_at")]


def set_outbox_status(conn: sqlite3.Connection, ob_id: int, status: str,
                      error: str | None = None) -> None:
    with conn:
        conn.execute(
            "UPDATE mail_outbox SET status = ?, error = ?, "
            "sent_at = CASE WHEN ? = 'sent' THEN ? ELSE sent_at END WHERE id = ?",
            (status, error, status, _now_iso(), int(ob_id)))


def cancel_outbox(conn: sqlite3.Connection, ob_id: int) -> bool:
    """Cancel a scheduled send — only while still pending."""
    with conn:
        cur = conn.execute(
            "UPDATE mail_outbox SET status = 'canceled' "
            "WHERE id = ? AND status = 'pending'", (int(ob_id),))
    return cur.rowcount > 0


def mail_thread_dates(conn: sqlite3.Connection, account: str) -> dict:
    """{thread_id: last_date} for one account — lets mail_sync skip snippet
    fetches for threads that haven't changed since the last run."""
    return {r["thread_id"]: r["last_date"] for r in conn.execute(
        "SELECT thread_id, last_date FROM mail_threads WHERE account = ?", (account,))}


def mail_view(conn: sqlite3.Connection, include_done: bool = False,
              lookback_days: int = 7, account: str | None = None) -> list:
    """The Mail screen's thread list: mail_threads joined with triage state.
    Account-first: a thread is only ever seen through its own account —
    triage is joined on (account, thread_id) and `account=` narrows the list
    to one account. Project/goal mapping is retired and rows carry no
    project/goal keys (the old joins matched elements/tags on thread_id
    alone, which could bleed across accounts). Default filter hides done + snoozed
    (until snoozed_until passes) and anything gone from the INBOX scan;
    snoozed threads stay visible past both filters once they return."""
    now = _now_iso()
    now_dt = datetime.now(timezone.utc)
    cutoff = (now_dt - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = (
        "SELECT mt.*, COALESCE(t.pinned, 0) AS pinned, t.snoozed_until, t.done_at, t.note, t.my_day, t.moved_at "
        "FROM mail_threads mt "
        "LEFT JOIN mail_triage t ON t.account = mt.account AND t.thread_id = mt.thread_id "
        # E17: an in-inbox thread SHOWS, whatever its age — mail_sync now
        # scans the whole inbox so in_inbox is exact. (cutoff retired.)
        "WHERE (mt.in_inbox = 1 "
        "    OR t.snoozed_until IS NOT NULL "
        # E108: moved threads stay in the feed after archiving so label
        # scopes keep them; the client keeps them out of Inbox.
        "    OR t.moved_at IS NOT NULL) ")
    params: list = []
    if not include_done:
        sql += ("AND t.done_at IS NULL "
                "AND (t.snoozed_until IS NULL OR t.snoozed_until <= ?) ")
        params.append(now)
    if account:
        sql += "AND mt.account = ? "
        params.append(clean_account(account))
    sql += "ORDER BY COALESCE(t.pinned, 0) DESC, mt.last_date DESC"

    out = []
    for r in conn.execute(sql, params):
        d = dict(r)
        d["who"] = _display_name(d.get("last_from") or "")
        try:
            dt = datetime.strptime(d["last_date"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            d["age_h"] = max(0, int((now_dt - dt).total_seconds() // 3600))
        except (ValueError, TypeError):
            d["age_h"] = None
        d["snooze_returned"] = bool(d["snoozed_until"] and d["snoozed_until"] <= now)
        try:
            d["participants"] = json.loads(d["participants"] or "[]")
        except ValueError:
            d["participants"] = []
        try:
            d["labels"] = json.loads(d["labels"] or "[]")
        except (ValueError, TypeError):
            d["labels"] = []
        out.append(d)
    return out


# --------------------------------------------------------------------------
# Ask Grand Central conversations (E25)
# --------------------------------------------------------------------------
def ask_start_chat(conn: sqlite3.Connection, title: str) -> int:
    """Open a conversation. `title` is the first question, trimmed."""
    now = _now_iso()
    title = " ".join(str(title or "").split())[:120] or "(untitled)"
    with conn:
        cur = conn.execute(
            "INSERT INTO ask_chats (title, created_at, updated_at) VALUES (?,?,?)",
            (title, now, now))
    return cur.lastrowid


def ask_add_message(conn: sqlite3.Connection, chat_id: int, role: str,
                    text: str, actions: list | None = None) -> int:
    """Append one turn and bump the conversation's updated_at."""
    now = _now_iso()
    with conn:
        cur = conn.execute(
            "INSERT INTO ask_messages (chat_id, role, text, actions, ts)"
            " VALUES (?,?,?,?,?)",
            (int(chat_id), role, str(text or ""),
             json.dumps(actions) if actions else None, now))
        conn.execute("UPDATE ask_chats SET updated_at = ? WHERE id = ?",
                     (now, int(chat_id)))
    return cur.lastrowid


def ask_chats(conn: sqlite3.Connection, limit: int = 40) -> list:
    """Recent conversations, newest activity first, with turn counts."""
    rows = conn.execute(
        "SELECT c.id, c.title, c.created_at, c.updated_at,"
        "       COUNT(m.id) AS turns"
        "  FROM ask_chats c LEFT JOIN ask_messages m ON m.chat_id = c.id"
        " GROUP BY c.id ORDER BY c.updated_at DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]


def ask_chat(conn: sqlite3.Connection, chat_id: int) -> dict | None:
    """One conversation with its full transcript, oldest turn first."""
    head = conn.execute("SELECT * FROM ask_chats WHERE id = ?",
                        (int(chat_id),)).fetchone()
    if not head:
        return None
    msgs = []
    for r in conn.execute(
            "SELECT role, text, actions, ts FROM ask_messages"
            " WHERE chat_id = ? ORDER BY id", (int(chat_id),)):
        m = dict(r)
        m["actions"] = json.loads(m["actions"]) if m["actions"] else []
        msgs.append(m)
    return {**dict(head), "messages": msgs}


def ask_delete_chat(conn: sqlite3.Connection, chat_id: int) -> bool:
    with conn:
        conn.execute("DELETE FROM ask_messages WHERE chat_id = ?", (int(chat_id),))
        cur = conn.execute("DELETE FROM ask_chats WHERE id = ?", (int(chat_id),))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Sender category overrides (E34) — apps/bots Bart says aren't people
# --------------------------------------------------------------------------
def sender_categories(conn: sqlite3.Connection) -> dict:
    """{lowercased email: category} — the curated app-sender overrides."""
    return {r["email"]: r["category"]
            for r in conn.execute("SELECT email, category FROM sender_categories")}


def set_sender_category(conn: sqlite3.Connection, email: str,
                        category: str = "notification") -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        raise ValueError("a full email address is required")
    with conn:
        conn.execute(
            "INSERT INTO sender_categories (email, category, ts) VALUES (?,?,?) "
            "ON CONFLICT(email) DO UPDATE SET category=excluded.category, ts=excluded.ts",
            (email, category, _now_iso()))
        # E69: apply to the existing rows RIGHT NOW, not at the next
        # mail_sync — the UI's optimistic update was being clobbered by the
        # next /mail poll and looked like a revert (Bart hit it on a 🗂
        # recategorize; the override IS what the next sync would compute).
        conn.execute(
            "UPDATE mail_threads SET category = ? WHERE lower(last_from_email) = ?",
            (category, email))
    return email


def clear_sender_category(conn: sqlite3.Connection, email: str) -> bool:
    with conn:
        cur = conn.execute("DELETE FROM sender_categories WHERE email = ?",
                           ((email or "").strip().lower(),))
    return cur.rowcount > 0


# --------------------------------------------------------------------------
# Inbox steward (E59) — candidates, suggestions, verdicts
# --------------------------------------------------------------------------
def steward_candidates(conn: sqlite3.Connection, own_addresses: set,
                       limit: int = 25) -> list:
    """Senders the deterministic rules judged 'people' but nobody has ever
    vouched for: in the inbox now, no sender_categories entry, never
    suggested before (any status), never replied to by Bart (mail_sends),
    and no positive engagement prior. Grouped per address with evidence."""
    replied = set()
    for r in conn.execute("SELECT DISTINCT lower(to_addrs) AS t FROM mail_sends"):
        for part in re.findall(r"[\w.+-]+@[\w.-]+", r["t"] or ""):
            replied.add(part.lower())
    pri = sender_priors(conn)
    engaged = set(pri.get("reply_email", {}))   # anyone Bart has written to
    decided = {r["email"] for r in conn.execute("SELECT email FROM steward_suggestions")}
    known = set(sender_categories(conn))

    rows = conn.execute(
        "SELECT last_from_email AS email, MAX(last_from) AS who, "
        "  COUNT(*) AS n, GROUP_CONCAT(subject, ' | ') AS subjects, "
        "  MAX(snippet) AS snippet, MAX(last_date) AS last_date "
        "FROM mail_threads "
        "WHERE in_inbox = 1 AND category = 'people' "
        "  AND last_direction = 'inbound' AND last_from_email != '' "
        "GROUP BY last_from_email ORDER BY MAX(last_date) DESC").fetchall()
    out = []
    for r in rows:
        em = (r["email"] or "").lower()
        if (not em or em in own_addresses or em in known or em in decided
                or em in replied or em in engaged):
            continue
        out.append({"email": em, "who": r["who"] or "",
                    "n": r["n"], "subjects": (r["subjects"] or "")[:400],
                    "snippet": (r["snippet"] or "")[:200]})
        if len(out) >= limit:
            break
    return out


def steward_record(conn: sqlite3.Connection, email: str, who: str,
                   category: str, why: str, evidence: str,
                   status: str = "pending") -> None:
    with conn:
        conn.execute(
            "INSERT INTO steward_suggestions (email, who, category, why, "
            "evidence, status, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(email) DO NOTHING",
            ((email or "").lower(), who, category, why, evidence, status,
             _now_iso()))


def steward_pending(conn: sqlite3.Connection) -> list:
    """E204.1: each suggestion carries the mail ACCOUNTS its sender writes to,
    so the board can show every inbox its own steward (Bart's ⚑ #15)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT s.id, s.email, s.who, s.category, s.why, s.evidence, s.created_at, "
        "  (SELECT group_concat(DISTINCT mt.account) FROM mail_threads mt "
        "     WHERE lower(mt.last_from_email) = s.email) AS accounts "
        "FROM steward_suggestions s WHERE s.status = 'pending' ORDER BY s.id")]
    for r in rows:
        r["accounts"] = [a for a in (r["accounts"] or "").split(",") if a]
    return rows


def steward_decide(conn: sqlite3.Connection, sid: int, approve: bool) -> dict | None:
    """Bart's verdict. Approve writes sender_categories (the same store a
    manual 🤖 click uses) so mail_sync applies it next pass."""
    row = conn.execute("SELECT * FROM steward_suggestions WHERE id = ? "
                       "AND status = 'pending'", (int(sid),)).fetchone()
    if not row:
        return None
    status = "approved" if approve else "rejected"
    with conn:
        conn.execute("UPDATE steward_suggestions SET status = ?, decided_at = ? "
                     "WHERE id = ?", (status, _now_iso(), int(sid)))
    if approve:
        set_sender_category(conn, row["email"], row["category"])
    return dict(row)


# --------------------------------------------------------------------------
# E111 — the 🧙 Wizard: agent-proposed todos awaiting Bart's verdict
# --------------------------------------------------------------------------
def add_wizard_suggestion(conn: sqlite3.Connection, kind: str, title: str,
                          why: str | None = None, due: str | None = None,
                          account: str | None = None,
                          thread_id: str | None = None,
                          meeting_ref: str | None = None,
                          meeting_title: str | None = None) -> int | None:
    """Insert one pending suggestion; returns id, or None when a duplicate
    (same kind+title, any status — a dismissed suggestion is never re-made)
    or an equivalent todo already exists."""
    title = " ".join(str(title or "").split())[:200]
    if not title:
        return None
    if conn.execute("SELECT 1 FROM wizard_suggestions WHERE kind = ? AND title = ?",
                    (kind, title)).fetchone():
        return None
    # a live todo that already starts with this text (e.g. today's directly-
    # created granola items) — don't re-suggest what's already on the board
    if conn.execute("SELECT 1 FROM engagements WHERE state != 'dropped' "
                    "AND text LIKE ? || '%'", (title,)).fetchone():
        return None
    with conn:
        cur = conn.execute(
            "INSERT INTO wizard_suggestions (kind, title, why, due, "
            "  account, thread_id, meeting_ref, meeting_title, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, title, why, due, account, thread_id,
             meeting_ref, meeting_title, _now_iso()))
        return cur.lastrowid


def wizard_purge_stale(conn: sqlite3.Connection, days: int = 7) -> int:
    """E158: a pending suggestion Bart hasn't touched in `days` days expires
    quietly. Status 'expired' rather than DELETE so the kind+title dedupe in
    add_wizard_suggestion still holds — an expired suggestion never comes
    back, same as a dismissed one."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        cur = conn.execute(
            "UPDATE wizard_suggestions SET status = 'expired', decided_at = ? "
            "WHERE status = 'pending' AND created_at < ?",
            (_now_iso(), cutoff))
    return cur.rowcount


def wizard_pending(conn: sqlite3.Connection) -> list:
    wizard_purge_stale(conn)   # the page can never show a >7-day-old ask
    return [dict(r) for r in conn.execute(
        "SELECT * FROM wizard_suggestions WHERE status = 'pending' "
        "ORDER BY kind, id DESC")]


def wizard_decide(conn: sqlite3.Connection, sid: int, approve: bool,
                  account: str | None = None) -> dict | None:
    """Bart's verdict. Approve creates the todo, source email linked (E106
    membership) when there is one.
    Account-first: the todo lands in the suggestion's email account; a
    meeting-only suggestion has none, so the caller must pass `account` —
    otherwise this raises ValueError and the suggestion stays pending."""
    row = conn.execute("SELECT * FROM wizard_suggestions WHERE id = ? "
                       "AND status = 'pending'", (int(sid),)).fetchone()
    if not row:
        return None
    todo_id = None
    if approve:
        r = dict(row)
        src_ref = (f"{r['account']}:{r['thread_id']}"
                   if r.get("account") and r.get("thread_id")
                   else (f"granola:{r['meeting_ref']}" if r.get("meeting_ref") else None))
        notes = (r.get("why") or "")
        if r.get("meeting_title"):
            notes = (notes + f"  (mtg: {r['meeting_title'][:60]})").strip()
        todo_id = add_engagement(conn, r["title"],
                                 due=r.get("due"), source="wizard",
                                 source_ref=src_ref, notes=notes or None,
                                 account=r.get("account") or account)
        if todo_id and r.get("account") and r.get("thread_id"):
            link_thread(conn, todo_id, r["account"], r["thread_id"])
    with conn:
        conn.execute("UPDATE wizard_suggestions SET status = ?, decided_at = ?, "
                     "todo_id = ? WHERE id = ?",
                     ("approved" if approve else "dismissed", _now_iso(),
                      todo_id, int(sid)))
    out = dict(row)
    out["todo_id"] = todo_id
    return out


def wizard_mark_scanned(conn: sqlite3.Connection, kind: str, refs: list) -> None:
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO wizard_scan (kind, ref, scanned_at) VALUES (?,?,?)",
            [(kind, str(x), _now_iso()) for x in refs])


def wizard_unscanned(conn: sqlite3.Connection, kind: str, refs: list) -> list:
    seen = {r["ref"] for r in conn.execute(
        "SELECT ref FROM wizard_scan WHERE kind = ?", (kind,))}
    return [x for x in refs if str(x) not in seen]


# --------------------------------------------------------------------------
# E60 — emails grouped under a todo
# --------------------------------------------------------------------------
def link_thread(conn: sqlite3.Connection, eng_id: int, account: str,
                thread_id: str, add: bool = True,
                subject: str | None = None) -> bool:
    """Attach/detach one Gmail thread to a todo. Idempotent both ways.
    subject is an optional display hint for threads mail_threads may never
    sync (sent-only threads from commitment mining).

    Account boundary: a thread joins a todo only if the todo is in the same
    account. An unassigned (legacy NULL) todo adopts the thread's account
    provided it holds no emails from a different one. Anything else raises
    ValueError — emails and todos never cross accounts."""
    if add:
        account = clean_account(account)
        row = conn.execute("SELECT account FROM engagements WHERE id = ?",
                           (int(eng_id),)).fetchone()
        if row is None:
            return False
        if row["account"] and row["account"] != account:
            raise ValueError(f"todo #{int(eng_id)} belongs to account "
                             f"{row['account']!r}; cannot link a {account!r} email")
        if not row["account"]:
            foreign = conn.execute(
                "SELECT 1 FROM engagement_threads WHERE engagement_id = ? "
                "AND account != ? LIMIT 1", (int(eng_id), account)).fetchone()
            if foreign:
                raise ValueError(f"todo #{int(eng_id)} already holds emails from "
                                 f"another account")
            with conn:
                conn.execute("UPDATE engagements SET account = ? WHERE id = ?",
                             (account, int(eng_id)))
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO engagement_threads "
                "(engagement_id, account, thread_id, added_at, subject) "
                "VALUES (?,?,?,?,?)",
                (int(eng_id), account, str(thread_id), _now_iso(),
                 (str(subject)[:200] if subject else None)))
        return True
    with conn:
        cur = conn.execute(
            "DELETE FROM engagement_threads WHERE engagement_id = ? "
            "AND account = ? AND thread_id = ?",
            (int(eng_id), account, str(thread_id)))
    return cur.rowcount > 0


def threads_for_todos(conn: sqlite3.Connection) -> dict:
    """{engagement_id: [{account, thread_id, subject, who, in_inbox}]} for
    todo_view — joined with mail_threads so archived members still show a
    subject in the todo's email list."""
    out: dict[int, list] = {}
    for r in conn.execute(
            "SELECT et.engagement_id, et.account, et.thread_id, "
            "  COALESCE(mt.subject, et.subject) AS subject, "
            "  mt.last_from AS who, COALESCE(mt.in_inbox, 0) AS in_inbox, "
            "  mt.last_from_email AS who_email, mt.last_date AS last_date, "
            "  COALESCE(mt.unread, 0) AS unread, mt.snippet AS snippet, "
            "  COALESCE(mt.message_count, 1) AS n, "
            "  (SELECT tr.snoozed_until FROM mail_triage tr WHERE tr.account = et.account "
            "     AND tr.thread_id = et.thread_id) AS snoozed_until, "
            "  et.added_at AS added_at "
            "FROM engagement_threads et "
            "LEFT JOIN mail_threads mt ON mt.account = et.account "
            "  AND mt.thread_id = et.thread_id "
            "ORDER BY et.added_at"):
        out.setdefault(r["engagement_id"], []).append({
            "account": r["account"], "thread_id": r["thread_id"],
            "subject": r["subject"] or "(no longer synced)",
            "who": r["who"] or "", "in_inbox": bool(r["in_inbox"]),
            # E204: enough to render a member as a full Shortwave row and to
            # sort a todo by its latest activity, even once archived
            "who_email": r["who_email"] or "", "n": r["n"],
            "last_date": r["last_date"] or r["added_at"],
            "unread": bool(r["unread"]), "snippet": (r["snippet"] or "")[:200],
            "snooze_returned": bool(r["snoozed_until"] and r["snoozed_until"] <= _now_iso()),
            "snoozed": bool(r["snoozed_until"] and r["snoozed_until"] > _now_iso())})
    return out


# --------------------------------------------------------------------------
# E69 — daily notes (Today view journal)
# --------------------------------------------------------------------------
def get_day_note(conn: sqlite3.Connection, date: str) -> dict:
    r = conn.execute("SELECT date, text, updated_at FROM day_notes "
                     "WHERE date = ?", (date,)).fetchone()
    return dict(r) if r else {"date": date, "text": "", "updated_at": None}


def list_day_notes(conn: sqlite3.Connection, start: str, end: str) -> list:
    """Non-empty notes with date in [start, end], for calendar views."""
    return [dict(r) for r in conn.execute(
        "SELECT date, text, updated_at FROM day_notes "
        "WHERE date >= ? AND date <= ? AND text != '' ORDER BY date",
        (start, end))]


def set_day_note(conn: sqlite3.Connection, date: str, text: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO day_notes (date, text, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET text=excluded.text, "
            "updated_at=excluded.updated_at",
            (date, str(text or "")[:20000], _now_iso()))


# --------------------------------------------------------------------------
# E209 — email classification
# --------------------------------------------------------------------------
CLASS_FIELDS = ("type", "relationship", "respond", "priority", "waiting_on", "follow_up")
_CLASS_VALUES = {
    "type": ("Correspondence", "Team collaboration", "Sales/outreach", "Update",
             "Newsletter", "Receipt", "Calendar", "Promo", "Other"),
    "relationship": ("teammate", "company", "frequent", "known", "new", "machine"),
    "respond": ("yes", "no", "optional"),
    "priority": ("0", "1", "2", "3"),
    "waiting_on": ("none", "teammate", "outside"),
}
_DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def class_write_rules(conn, account, thread_id, fields, fingerprint, version,
                      roster_hash, stamp) -> None:
    """Upsert the rule half of a classification; the model half is kept."""
    conn.execute(
        "INSERT INTO mail_class (account, thread_id, fingerprint, version, roster, rules, "
        "classified_at) VALUES (?,?,?,?,?,?,?) ON CONFLICT(account, thread_id) DO UPDATE SET "
        "fingerprint=excluded.fingerprint, version=excluded.version, roster=excluded.roster, "
        "rules=excluded.rules, classified_at=excluded.classified_at",
        (account, thread_id, fingerprint, version, roster_hash,
         json.dumps(fields), stamp))


def class_merge_model(conn, results: dict, source: str) -> int:
    """{(account, thread_id): {summary, reply_needed, importance}} → the model
    half of rows that exist. Returns rows updated."""
    stamp, n = _now_iso(), 0
    with conn:
        for (account, thread_id), a in results.items():
            model = {"summary": a.get("summary") or None,
                     "respond": "yes" if a.get("reply_needed") else "no",
                     "priority": a.get("importance")}
            cur = conn.execute(
                "UPDATE mail_class SET model=?, model_source=?, model_at=? "
                "WHERE account=? AND thread_id=?",
                (json.dumps(model), source, stamp, account, thread_id))
            n += cur.rowcount
    return n


def class_pins(conn, account: str, thread_id: str | None = None) -> dict:
    """{thread_id: {field: latest corrected value}} for one account."""
    q = "SELECT thread_id, field, new_value FROM class_feedback WHERE account = ?"
    args = [account]
    if thread_id is not None:
        q += " AND thread_id = ?"
        args.append(thread_id)
    out: dict = {}
    for r in conn.execute(q + " ORDER BY id", args):
        out.setdefault(r["thread_id"], {})[r["field"]] = r["new_value"]
    return out


JEV_CONF_FLOOR = 0.35     # a Jev answer less confident than this doesn't override


def class_write_jev(conn, account, thread_id, answers: dict, fingerprint: str) -> None:
    with conn:
        conn.execute("UPDATE mail_class SET jev=?, jev_fp=?, jev_at=? WHERE account=? AND thread_id=?",
                     (json.dumps(answers), fingerprint, _now_iso(), account, thread_id))


def class_effective(row, pins: dict, use_jev: bool = False) -> dict:
    """correction > Jev (switch on, confident) > Haiku > rule, plus where each
    value came from."""
    rules = json.loads(row["rules"] or "{}")
    model = json.loads(row["model"] or "{}") if row["model"] else {}
    out, src = dict(rules), {f: "rules" for f in CLASS_FIELDS}
    for f in ("respond", "priority", "summary"):
        if model.get(f) is not None:
            out[f], src[f] = model[f], row["model_source"] or "model"
    jev = json.loads(row["jev"] or "{}") if use_jev and row["jev"] else {}
    conf = jev.get("confidence") or {}
    for f in ("type", "respond", "priority"):
        c = conf.get(f)
        if jev.get(f) is not None and c is not None and c >= JEV_CONF_FLOOR:
            out[f], src[f] = jev[f], "jev"
    if jev.get("expects_reply") is not None and out.get("waiting_on", "none") != "none" \
            and jev["expects_reply"] < 0.3:
        # Bart wrote last but asked nothing — there's nobody to chase
        out["waiting_on"], out["waiting_who"], src["waiting_on"] = "none", None, "jev"
    if jev:
        out["jev_model"] = jev.get("model_id")
    for f, v in pins.items():
        out[f], src[f] = (int(v) if f == "priority" else v), "you"
    # nothing owed and nobody to chase → nothing to follow up
    if out.get("respond") == "no" and out.get("waiting_on", "none") == "none" \
            and src.get("follow_up") != "you":
        out["follow_up"] = None
    out["source"] = src
    return out


def class_get(conn, account: str, thread_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM mail_class WHERE account=? AND thread_id=?",
                       (account, thread_id)).fetchone()
    if row is None:
        return None
    eff = class_effective(row, class_pins(conn, account, thread_id).get(thread_id, {}),
                          use_jev=class_account_jev(conn, account))
    eff.update(account=account, thread_id=thread_id, version=row["version"],
               classified_at=row["classified_at"])
    return eff


def class_correct(conn, account: str, thread_id: str, field: str, value: str) -> dict:
    """Bart's answer for one field. Pinned for good; logged for tuning."""
    if field not in CLASS_FIELDS:
        raise ValueError(f"unknown field {field!r}")
    value = str(value).strip()
    if field == "follow_up":
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("follow_up must be YYYY-MM-DD or empty")
    elif value not in _CLASS_VALUES[field]:
        raise ValueError(f"{field} must be one of {', '.join(_CLASS_VALUES[field])}")
    cur = class_get(conn, account, thread_id)
    if cur is None:
        raise ValueError("that email has no classification in this account")
    with conn:
        conn.execute(
            "INSERT INTO class_feedback (account, thread_id, field, old_value, new_value, ts) "
            "VALUES (?,?,?,?,?,?)",
            (account, thread_id, field, None if cur.get(field) is None else str(cur.get(field)),
             value, _now_iso()))
    return class_get(conn, account, thread_id)


def class_roster(conn) -> dict:
    return {"teammates": [dict(r) for r in conn.execute(
                "SELECT email, name FROM class_people ORDER BY name, email")],
            "groups": [dict(r) for r in conn.execute(
                "SELECT domain, grp FROM class_groups ORDER BY grp, domain")]}


def class_person_set(conn, email: str, name: str = "") -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("a full email address is required")
    with conn:
        conn.execute("INSERT INTO class_people (email, name, added_at) VALUES (?,?,?) "
                     "ON CONFLICT(email) DO UPDATE SET name=excluded.name",
                     (email, (name or "").strip()[:80], _now_iso()))
    return email


def class_group_set(conn, domain: str, grp: str) -> str:
    domain = (domain or "").strip().lower().lstrip("@")
    grp = (grp or "").strip()[:60]
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("a domain like example.com is required")
    if not grp:
        raise ValueError("a group name is required")
    with conn:
        conn.execute("INSERT INTO class_groups (domain, grp, added_at) VALUES (?,?,?) "
                     "ON CONFLICT(domain) DO UPDATE SET grp=excluded.grp",
                     (domain, grp, _now_iso()))
    return domain


def class_roster_remove(conn, kind: str, key: str) -> bool:
    table, col = {"person": ("class_people", "email"),
                  "group": ("class_groups", "domain")}.get(kind, (None, None))
    if not table:
        raise ValueError(f"unknown roster kind {kind!r}")
    with conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE {col} = ?", ((key or "").strip().lower(),))
    return cur.rowcount > 0


def class_account_jev(conn, account: str) -> bool:
    r = conn.execute("SELECT jev FROM class_accounts WHERE account = ?", (account,)).fetchone()
    return bool(r and r["jev"])


def class_set_jev(conn, account: str, on: bool) -> None:
    with conn:
        conn.execute("INSERT INTO class_accounts (account, jev, updated_at) VALUES (?,?,?) "
                     "ON CONFLICT(account) DO UPDATE SET jev=excluded.jev, updated_at=excluded.updated_at",
                     (account, 1 if on else 0, _now_iso()))
