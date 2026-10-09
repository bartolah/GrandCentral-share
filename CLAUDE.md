# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Grand Central is a single-user (Casey) chief-of-staff board running on a Mac mini: it syncs several Gmail accounts, Google Calendar (ICS) and Granola into SQLite and serves a Shortwave-style web app (inbox, todos, calendar, Today). Python 3.11 stdlib-heavy backend, one large vanilla-JS page, no frameworks, no build step.

**Current direction is "account-first" (2026-09-24, see `ACCOUNT_FIRST.md`).** Accounts are the navigation and the ownership boundary: mail is only ever seen through its own account; a todo belongs to exactly one account and may only hold that account's email. Projects, goals, theaters/operations/objectives, automatic project classification, Attio and the old dashboard are **retired**. `README.md`, `BUILD_SPEC.md` §1–5, `RECONCILIATION.md`, `ToDos.md` and `MISCLASSIFICATIONS.md` still describe that retired design as history; do not rebuild from them. Retired tables/columns are kept frozen for rollback, and retired routes answer 410 (`_RETIRED_GET`/`_RETIRED_POST` in `dashboard/serve.py`). Dropping them is "slice 3" and needs Casey's explicit sign-off plus a DB backup.

**Direction (2026-09-29, see `ABOUT.md`).** Account-first stays for mail and todos: every email and every todo belongs to exactly one account, and a todo only holds its own account's email (no account-less todos). This replaces the 2026-09-28 plan to loosen it. Working across jobs comes through **Projects** (filed by Casey, can span accounts, never move items out of their accounts) and **Notes** (stored in the database; local sync/backup later). Both are being designed; see `ABOUT.md` and BUILD_SPEC §6. One narrow exception, also 2026-09-29: a **read-only Home tab** may show every account at once (each in its own section, one day calendar), and the day note and conflict spotting may read across accounts in a call separate from the per-account `assess.py`. Home never re-owns or acts on anything outside its account; Ask and MCP action tools still require one account. Read that §6 entry before building it.

## Commands

```bash
.venv/bin/python -m unittest discover -s tests -v                    # full offline suite
.venv/bin/python -m unittest tests.test_account_isolation -v         # one file
.venv/bin/python -m unittest tests.test_mail_categories.ListUnsubscribeIsTheWeakestSignal -v  # one class/test
.venv/bin/python sweeper/dev_check.py      # syntax gate: ast on changed .py + `node --check` on every inline <script> in dashboard/*.html and dashboard/*.js
.venv/bin/python sweeper/selftest.py       # API checks against the LIVE board on :8080 (write paths clean up after themselves)
.venv/bin/python sweeper/selftest.py --ui  # + headless Chrome render checks
.venv/bin/python sweeper/uitest.py         # phone-layout invariants (390x844, every skin) in headless Chrome
.venv/bin/python integrations/mcp/gc_mcp.py --selftest   # must pass after any change under integrations/mcp/ (needs data/agent_key.json; reads count against the agent's 2MB/hr budget)
.venv/bin/python sweeper/agent_gate.py --status          # E258: agent key, switched-on accounts, lockout (--enable/--disable ACCT, --reset, --new-key)
.venv/bin/python sweeper/agent_audit.py --days 7         # E258: review every agent call; SUSPECT = a write right after reading injection-shaped mail
.venv/bin/python sweeper/egress.py --report              # E258: outbound hosts seen; WOULD BLOCK = not in config/egress_allow.txt
.venv/bin/python sweeper/tracking.py --check             # E269: open/click tracking — t.<domain> health, per-account switches, recent tracked sends
integrations/tracker/deploy.sh                           # E269: deploy the tracker Worker (needs `wrangler login`; never prints TRACK_SECRET)
launchctl kickstart -k gui/$(id -u)/com.example.grandcentral.dashboard   # restart after serve.py / sweeper changes the server imports
```

- `tests/` are fully offline: scratch SQLite files, a fake Anthropic client, a scratch `serve.py` on a scratch port (`GC_DB_PATH`, `GC_PORT` env overrides), and fake account slugs `acctone`/`accttwo` that exist in no `.env`. Keep new tests that way. `tests/test_ui_account_scope.py` extracts the real JS from `index.html` and runs it under node (skipped without node).
- `selftest.py`/`uitest.py` hit the live board and live DB; the unit tests never do.
- `sweep.py` and `mail_sync.py` are launchd jobs that re-import code every run, so edits are live for them without a restart. `serve.py` is long-running and needs the kickstart above.
- Mutating CLI verbs (`mail_actions.py`, `mail_sync.py --dry-run`, `backfill_todo_accounts.py`) default to or offer dry-run; keep that convention.

## Architecture

- **`sweeper/`** is the write side. `sweep.py` (hourly 07–19) reads sources (`sources/gmail_reader.py`, `calendar_reader.py`, `granola_reader.py`), runs `assess.py` (Haiku, batched **per account**, only summary/`reply_needed`/`importance`, results keyed `(account, thread_id)`), then `build_state.py` writes `dashboard/grand_central.json` atomically (temp + rename). `mail_sync.py` (every 3 min) syncs Gmail INBOX headers into SQLite, categorizes mail into bundles (`categorize()`: people/newsletter/receipt/notification/calendar), wakes snoozes, and runs `todo_labels.py` (todos ↔ `GC/Todo/<name>` Gmail labels, two-way, same-account only).
- **`sweeper/db.py`** is the SQLite layer over `data/grand_central.db` (system of record: mail threads, engagements = todos, triage, dismissals, snoozes, sends, dev requests…). Schema changes are **additive migrations only** (nullable columns via the existing migration list). Account enforcement lives here: `add_engagement` requires an account, `link_thread` refuses cross-account membership, `todo_view(account=)`/`mail_view(account=)`.
- **Gmail thread ids are not unique across accounts.** Always key by `(account, thread_id)`. Tests deliberately reuse the same thread id in two accounts.
- **`dashboard/serve.py`** is a stdlib `http.server`: serves static files plus every read/write endpoint, each validated before touching `sweeper/*` (it puts `sweeper/` on `sys.path`). `dashboard/index.html` (~8k lines) is the entire app; `settings.html` holds Connections + Context; `sw.js` is the service worker (bump its cache name when the shell changes). The board reads `grand_central.json` for Top 5/reply debt/calendar rails and live endpoints (`/mail?account=`, `/todos?account=`, `/accounts`, …) for everything else. Every active view is scoped to one selected account and filtered again client-side.
- **Context documents** (`context_docs`, E259): pasted/uploaded Markdown briefs scoped `account:<slug>` or `project:<id>`, read by Ask, project Ask and reply drafts, never by MCP. `sweeper/project_seed.py PLAN [--apply]` sets up an account's projects, briefs and todo filing from `config/seeds/<account>/plan.json` (dry run by default).
- **`command.py`** backs the in-app "Ask" bar (Claude tool-calling restricted to the same validated single-thread actions as the buttons); since E265 calendar events, new drafts and triage are `propose_*` tools that only return a card Casey clicks). **`integrations/mcp/`** is the agent surface (OpenClaw): a thin HTTP client with a hard `(method, path)` allowlist (`gc_client.ALLOWED`), explicit `DENIED` routes, and `ACTION_ALLOW` for routes that mix safe and dangerous verbs.
- **Dev queue:** Casey files change requests in-app (`GET/POST /dev`, account-scoped, paired devices only). `sweeper/dev_worker.sh` runs twice daily: `sweeper/dev_triage.py` first labels each new/edited open card (priority p1–p3, size s/m/l, verdict build/ask/session/duplicate/done — one model call, no tools), then a restricted headless Claude session with `sweeper/dev_worker_settings.json` builds only `build` cards of size s/m, priority order, ≤5 a run, and ships each through `sweeper/dev_ship.py` (named files only, never secrets or its own control files; raw git add/commit/push are not allowed to it). It won't build while the tree has uncommitted tracked changes, so commit or stash before leaving a session mid-change; `dev_answer.py ID <building|shipped|declined|open> 'note'` answers a card.
- Runtime: launchd plists in `config/` (sweep, mailsync, dashboard :8080, Cloudflare tunnel, autopull, devworker). `auto_pull.sh` fast-forwards `main` from GitHub every 5 min and restarts the dashboard, so **pushing to `main` deploys**.

## Security guardrails (BUILD_SPEC §6, non-negotiable)

Every widening of what the system may do is a dated, explicit Casey decision recorded in `BUILD_SPEC.md` §6. Don't reinterpret it; propose a new decision instead.
- **One send path:** `POST /send` → `send_mail.py`, only from a human's Send click in the composer (since 2026-09-29 one click + a 10-second Undo hold replaces the second confirming click; the client then posts `confirm:true` + the exact recipient chips shown, ≤10 recipients, one thread, logged to `mail_sends`). Never expose send, trash, bulk, or credential writes to the Ask bar, MCP/OpenClaw, or any Claude tool. `send_mail.py` intentionally has no CLI.
- Delete means Gmail Trash (recoverable). There is no purge/empty-trash verb, and none may be added.
- Calendar writes are create-only on Casey's own calendars, never with attendees — plus, since 2026-09-27, RSVP (Casey's own Yes/Maybe/No on an invite, `calendar_rsvp.py`, human click only).
- Credentials live only in `config/.env` (chmod 600) or Keychain. `connections.py` is the sole writer and never returns values. **`config/backups/.env*` hold live secrets:** stage files by name, never `git add -A`/`-f`. `data/`, `logs/`, `grand_central.json` are gitignored runtime output.
- The server only accepts loopback/tailnet peers and rejects mutating POSTs with a foreign Origin/Referer (E161). Keep both checks.
- **Agent hardening (E258, §6 2026-09-30):** `sweeper/agent_gate.py` decides per request who is calling — a paired browser (`X-GC-Dev-Code`), the MCP (`X-GC-Agent-Key`, `data/agent_key.json`) or an unidentified local caller. Send/trash/RSVP/invites/credentials/agent switches are paired-browser only, whatever `confirm:true` says. Agents get only the routes in `AGENT_GET`/`AGENT_POST`, in switched-on accounts, within `LIMITS`, audited in `agent_audit`. Threads labelled **Private** in Gmail do not exist anywhere in GC (`sweeper/private.py`; never bypass `private.guard`/`keys`). Mail content reaching any model goes through `sweeper/untrusted.py` (fenced + neutralized). New agent capability = a new dated §6 decision.
- Agents are enforced in the board, not the MCP client (E258, `sweeper/agent_gate.py`): paired browser = human; `data/agent_key.json` = agent (OpenClaw), limited to its tables, switched-on accounts, limits and audit. The Claude desktop app (E260, `gc_mcp.py --profile desktop`, key `data/agent_key_desktop.json`) reads one account per call; since E270 (§6 2026-10-06) it makes the agent's writes directly (not `/context` or `/sweep`) under every agent rule; the E260 `/agent/propose` → Approve path remains but nothing requires it. Drafts are outside the hourly write limit (a 200/day runaway ceiling only). A route an agent must never reach goes in `HUMAN_ONLY_POST`; mirror any change in `gc_client`.

## Conventions

- Work is numbered `E<n>` (sub-releases `E<n>.<m>`). Commit subjects look like `E207: put trash back where deleting is a deliberate act`. A user-visible change gets a `dashboard/release_notes.json` entry (next E number, same shape as existing entries, update `latest`), and a design/decision write-up goes in `ACCOUNT_FIRST.md` or `ToDos.md`.
- Keep Python modules small and each runnable on its own. Code comments explain *why* and often cite the E number and Casey's words; match that.
- Live Gmail writes (labels, archive, trash) have real consequences. When a fix would change live-write behaviour, flag it and don't just ship it (e.g. the known `&` label-encoding bug in `todo_labels.py` noted in `ACCOUNT_FIRST.md`).
