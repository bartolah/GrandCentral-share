# Account-first simplification

Direction (Bart, 2026-09-24): accounts are the primary navigation and the
ownership boundary. Emails are only ever seen through their own account. Todos
belong to exactly one account and show in that account's view. Projects, goals,
automatic classification, Attio, and the old dashboard go. Cross-account global
todos are deferred (not built).

Nothing here deletes historical data or makes an irreversible schema change.
Retired behaviour is *stopped*, not erased: tables and rows stay for rollback.

## Dependency map (state before slice 1)

| Concept | Where it lived |
|---|---|
| Todo ownership | `engagements` had **no account column** — only `operation_id`/`objective_id`; the account link was `engagement_threads` |
| Cross-account leaks found | `link_thread` + `POST /task` never checked account; `todo_labels` matched Gmail `GC/Todo/<name>` labels by name only (one live todo, #409, spans two accounts because of it); `mail_view` joined `elements`/`mappings`/`element_tags` on `thread_id` alone; UI "account" tabs were really *theaters* (projects); reply-owed set keyed by `thread_id` only |
| Projects/goals/operations | tables `theaters`, `operations`, `objectives`, `mappings`, `corrections`, `element_tags`, `op_notes`, `metric_history`, `entity_*`; `config/projects.yaml`; `mapping.py`, `classify.py`, `registry_edit.py`, `migrate_workstreams.py`; sweep steps 2/2b + `build_state`; routes `/registry /feedback /tag /dash /opnote /bizdev`; `/task` `operation`/`objective`; UI rail Projects + Goals, group-by Project/Goal, Goals dock, re-file dialog; MCP `list_projects refile_thread tag_thread list_deals`; Ask-bar `reclassify query_bizdev`; Chrome capture extension dropdowns |
| Classification | `classify.py` (Haiku call every sweep) → `mapping.py` precedence → `elements`/`mappings` writes; the same call also judges `reply_needed`/`importance`, which feed Top 5 and reply debt — **that half is not project classification and needs a replacement before it can go** |
| Attio | `sources/attio_reader.py`, `bizdev.py`, sweep step 1d, `/bizdev`, `list_deals`, `query_bizdev`, `/brief` hot deals, `connections.py` key + test |
| Old dashboard | `dashboard/classic.html` (+`mail.js`, `todos.js`), root `grand_central_dashboard.html`, `/dash` entity feed, `sample.json` |
| Runtime | launchd: sweep (hourly 07–19), mailsync (3 min), dashboard (KeepAlive), tunnel, autopull, devworker. `sweep.py`/`mail_sync.py` re-import code every run, so edits are live for them without a restart |

## Phases

1. **Slice 1 (done).** Account boundary enforced in the DB layer, HTTP routes,
   Gmail-label sync, UI and MCP; project/goal/Attio/old-board entry points and
   write paths stopped; additive `engagements.account`.
2. **Slice 2 (done, this change).** Automatic project/goal classification
   removed from the sweep; the classifier's useful half (reply_needed /
   importance) replaced by a registry-free, per-account assessor; `build_state`
   emits an account-only board; the old dashboard removed (Connections +
   Context moved to a small `settings.html`); every remaining project/goal
   route, tag, filter, wizard field and MCP/command-bar parameter stopped.
3. **Slice 3 (irreversible, needs explicit sign-off).** See "Deferred schema
   and file cleanup" below. Take a DB backup first.

## Slice 1: what changed

* `engagements.account` (nullable, additive). `db.add_engagement` requires an
  account (or derives it from `source_ref "account:thread"`; a mismatch raises).
  `link_thread` refuses cross-account membership; an unassigned legacy todo
  adopts the first email's account unless it already holds another's.
  `update_engagement(account=)` is refused while the todo holds foreign email.
  Recurrence keeps the account. Dedupe is per account.
* `todo_view(account=)` / `mail_view(account=)`; `GET /todos?account=` and
  `GET /mail?account=`.
* `todo_labels`: Gmail labels only match/create todos of the same account.
* `POST /task add` requires `account`; wizard approval takes/derives one;
  `import_mstodo --account`; Ask bar `add_task` requires `account`.
* UI: tabs are the real mail accounts (+ Unassigned); rail Projects/Goals,
  Goals dock, group-by Project/Goal, re-file entries and pickers gone; todo
  pickers only offer same-account todos.
* MCP: removed `list_deals list_projects refile_thread tag_thread`; `add_todo`
  requires `account`; `list_todos` filters by `account`.
* Chrome capture extension: account dropdown replaces project/goal (**re-install
  from `/quicktodo.zip`**; the old build gets a 400).
* `sweeper/backfill_todo_accounts.py` — dry-run by default; assigns legacy todos
  from evidence only. Ambiguous and no-evidence todos stay Unassigned.
  `GC_DB_PATH` / `GC_PORT` env overrides for tests.

## Slice 2: what changed

Sweep and board
* `sweeper/assess.py` (new) replaces `classify.py`. It asks the model only for a
  one-line summary, `reply_needed` and `importance` per thread. The prompt has no
  registry, no operation list, no correction few-shot. Threads are batched **per
  account** (a prompt never mixes two mailboxes) and results are keyed
  `(account, thread_id)`. A failed/unparsable batch is skipped; the board falls
  back to the subject, importance 1, reply_needed true.
* `sweep.py`: no `mapping`/`classify`, no `sync_registry` (the registry tables
  are no longer rebuilt from YAML, so the two `*-notifications` operations
  simply stay as inert rows in the live DB — nothing reads them), no
  `snapshot_metrics`, op notes, goal tags, corrections/overrides, or legacy
  file migration. Dismissals and snoozes are applied per `(account, thread_id)`
  (`db.dismissed_keys`, `db.snoozed_keys`).
* `build_state.py` rewritten: rails only (`top5`, `top5_pool`, `reply_debt`,
  `waiting_on`, `decisions`, calendar, meetings, summary). Gone from
  `grand_central.json`: `theaters operations objectives unassigned
  rule_suggestions pipeline tasks_unfiled`, and `stale_projects`,
  `deals_advancing`, `unassigned` from `summary`; rows no longer carry
  `project`/`operation`. Top 5 = importance + key person + sender engagement +
  age, diversity cap per **account**.
* Dismissal is account-scoped even though `dismissals` is keyed by `thread_id`
  alone (kept for rollback): `db.is_dismissed` lets the row's `account` decide;
  legacy rows with no account still match every account. `/dismiss` and the
  Ask-bar `dismiss` tool now require an account. Top 5 "resolved since the
  sweep" checks (`serve._top5_resolved`) scope dismissals, sends and the
  outbox to the thread's own account.
* `wizard.py` / `record_granola_meetings`: suggestions no longer carry a
  project; a meeting-only suggestion needs an account at approval.

Routes, tools, DB layer
* Also retired (410): `GET/POST /scratch`, `GET /progress` (project/goal pads and
  progress overlays). `/decision add` ignores `operation`. `/mail/search` no
  longer joins mappings and validates `account`. `/calendar` no longer
  inherits a project onto events.
* `feedback.py` reduced to `record_dismissed`.
* Command bar: `query_todos`/`query_mail` take `account` (not `project`);
  `unfiled` view, project/theater/objective grounding and `add_decision.operation`
  removed. MCP context tools describe `account:` scopes.
* `db.py`: deleted 24 dead functions (registry sync, corrections, mappings,
  tags, op notes, entity progress/notes, scratch, metrics, legacy file
  migration). `add_engagement`/`add_decision` no longer accept or write
  `operation_id`/`objective_id`; recurrence copies don't propagate them;
  `mail_view` rows have no `project/project_name/goals/extra_projects`.
* `common.py`: `load_registry()` is now a plain YAML read; only `defaults:` and
  `key_people:` are consumed. Legacy `overrides/dismissed` file helpers removed.
* Deleted modules: `classify.py mapping.py registry_edit.py migrate_workstreams.py
  backfill_inbox.py` (plus slice 1's `bizdev.py`, `attio_reader.py`).
* `planner.py`, `notify.py`: no deals, no project matching.

UI
* Old dashboard removed: `classic.html`, `mail.js`, `todos.js`, `sample.json`.
  Connections and Context (the only live parts) live in `dashboard/settings.html`
  (`/settings.html?tab=connections|context`); the avatar menu links there.
  Service-worker cache bumped to `gc-cache-v4` and stops precaching the old shell.
* `index.html`: 8 unreachable re-file/goal-tagging functions removed
  (`refileDlg doRefile rfNewGoal rfNewLane doNewLane laneOptions fileStarTodo
  tagGoal`) plus their orphaned CSS; `setScope` refuses `goal`/`goals`/`objective`.

## Tests

Offline only: scratch SQLite files, a fake Anthropic client, a scratch dashboard
process on a scratch port + scratch DB, fake account slugs (`acctone`,
`accttwo`) that exist in no `config/.env`. Nothing touches the live DB, Gmail,
launchd or the live server.

    .venv/bin/python -m unittest discover -s tests -v      # 58 tests
    cd sweeper && ../.venv/bin/python -c "import selftest; selftest.test_wiring()"   # source-only
    node --check on every inline <script> in dashboard/{index,settings}.html and dashboard/{sw}.js

Account-boundary invariants covered: todos need an account and can't hold
another account's email; same thread id in two accounts stays separate through
mail/todo views, triage, label sync, dismissals, snoozes, Top 5 resolution,
the assessor (per-account prompts, per-account result keys), `build_state`
(reply debt, waiting-on, categories, Top 5 cap) and the command-bar reads;
retired routes answer 410; retired modules/tables/Attio are not referenced by
runtime code (static guards); `/settings.html` is served and the old dashboard
files 404.

## Remaining legacy (intentionally preserved)

* **Data, all frozen and unread:** tables `theaters operations
  operation_theaters objectives operation_objectives mappings corrections
  element_tags op_notes metric_history entity_progress entity_notes
  scratch_pads`, and the `elements` rows. The DDL stays in `db.py` so an old DB
  keeps its history and `connect()` recreates the tables on a fresh one.
* **Columns, never written now:** `engagements.operation_id`,
  `engagements.objective_id`, `decisions.operation_id`,
  `wizard_suggestions.project`.
* **Files:** `config/projects.yaml` (only `defaults:` and `key_people:` are read),
  `config/backups/`, `config/overrides.json.imported`, `MISCLASSIFICATIONS.md`,
  `data/` rows above. One `ATTIO_*` key may still sit in `config/.env`; nothing
  reads it — remove it when convenient.
* **Context notes** keep their `project:`/`goal:` scopes as written.
* **Mail categories** (`people/newsletter/receipt/notification/calendar`,
  deterministic, per account) stay: they sort mail by *kind* inside an account
  for the Mail bundles; they never file mail under a project.
* **UI names:** `SCOPE.kind === 'project'` and `BOARD.theaters` are now internal
  names for an *account* view (its id is always an account id). ~326 lines in
  `index.html` still say project/goal/theater/objective/operation; almost all
  are unreachable (`BOARD.operations/objectives/pipeline` are forced empty; the
  goals page, goal dock, scratch pads and registry client have no entry point).
* **Narrative docs** (`README.md`, `BUILD_SPEC.md`, `RECONCILIATION.md`,
  `ToDos.md`, `dashboard/release_notes.json`) describe the retired design as
  history.
* `integrations/mcp/gc_client.py` keeps `POST /registry` in its DENIED table on
  purpose: nothing an agent does may ever write `config/projects.yaml`.

## Deferred schema and file cleanup (slice 3 — irreversible)

Only after a `data/grand_central.db` backup and explicit sign-off:

1. `DROP TABLE` the 13 tables above (and delete their DDL + the two
   `_RETIRED_*` route sets' client code).
2. `ALTER TABLE ... DROP COLUMN` the four legacy columns above (SQLite >= 3.35).
3. Delete `config/projects.yaml` after moving `defaults:` / `key_people:` to a
   small settings file; delete `config/backups/`, `overrides.json.imported`.
4. Remove the dead project/goal code in `index.html` (goals page, goal dock,
   scratch pads, registry client, project overlays/chips, `feedSuggestions`)
   and rename the internal `project`/`theater` scope names to `account`.
   `selftest.py` reports the leftover client code as "legacy … deferred UI
   cleanup"; it reaches zero when this is done.
5. Retire `/bizdev /dash /registry /feedback /tag /opnote /scratch /progress`
   from `serve.py` entirely (they answer 410 today).
6. Re-word the narrative docs; drop the `ATTIO_*` key from `config/.env`.

## E207: trash comes back to the row menu (2026-09-25)

The bounded-triage pass above took trash off *every* mail-row surface at once,
and Bart lost the ability to delete from the email view. The six-button hover
strip stays as it is — that strip is for verbs fired without thinking, and a
destructive one does not belong there. Trash returns to the row's right-click
and long-press menu (`mailCtxHtml`), where it lived before and where the act is
deliberate. It was already still reachable from the reading pane's overflow
menu (`paneMore`) and for whole bundles (`bundleTrash`); both unchanged. Still
Gmail Trash: recoverable 30 days, undoable through `queueUndo`, and there is
still no purge verb anywhere in this system.

`test_active_mail_rows_expose_only_the_six_triage_actions` guards the hover
strip only, so it is untouched and still passing.

## Known issue found (not caused by this work)

`todo_labels` writes to Gmail fail with `BAD Could not parse command` for the
label `GC/Todo/Social Media & Marketing` (3 threads, every mail_sync
run, since at least 20:13 on 2026-09-24): a label containing `&` is not
IMAP modified-UTF-7 encoded (`&` must be sent as `&-`) on the STORE path
(`todo_labels.py`). Left alone: fixing it changes live Gmail writes.

## Bounded triage queue (2026-09-25)

Changes
* `dashboard/index.html`: removed the global All/Unassigned account modes from
  active navigation. `/accounts` bootstraps real tabs; every active mail/todo
  load calls `/mail?account=<selected>` and `/todos?account=<selected>`, then
  applies a second exact-account client filter. Account switches invalidate
  Drafts, Sent, DevRequests and selected-row caches; a stale prior-account
  response is discarded.
* `dashboard/index.html`: the active mail row, context menu, reading pane,
  keyboard and bulk surfaces expose only reply/draft, archive, snooze, pin,
  handled, and create/add-to-account-todo. Archive only changes Gmail;
  handled only changes local triage. Removed compose, label, star, trash,
  dismiss, project/goal filing and registry controls from those surfaces.
* `dashboard/index.html`: Gmail deep search, Drafts and Sent request only the
  selected account. Every email-created, bulk-created, quick-created and
  manually created todo carries that account explicitly. The Shortwave
  square-corner override and mobile `--app-h`/safe-viewport code are unchanged.
* `sweeper/db.py`: added nullable `dev_requests.account` through the existing
  additive migration list. Historical rows remain untouched with NULL and are
  invisible in all account queues. `dev_add` now requires an account and
  `dev_list` requires/exactly filters one account.
* `dashboard/serve.py`: added read-only `GET /accounts`; scoped `GET /dev` and
  DevRequest creation to a known account; scoped live Sent reads to one account.
  No historical project/goal tables, columns or rows were dropped.
* `tests/test_ui_account_scope.py`: updated the real-JavaScript predicate
  harness for exact single-account behavior; added static guards for selected-
  account fetches/DevRequest intake and the six allowed mail-row actions.
* `tests/test_account_isolation.py`: replaced the old global-DevRequests
  assertions with temp-DB ownership, cross-account invisibility and preserved-
  legacy-row tests; added scratch-server coverage for `/accounts` and scoped
  `/dev` reads.

Verification attempted
* `git diff --check` — PASS (no output).
* `.venv/bin/python -m unittest discover -s tests -v` — BLOCKED before process
  start by the execution host: `SYSTEM_RUN_DENIED: approval cannot safely bind
  this interpreter/runtime command`.
* `.venv/bin/python -m py_compile dashboard/serve.py sweeper/db.py
  tests/test_ui_account_scope.py tests/test_account_isolation.py` — BLOCKED by
  the same host policy before process start.
* `/usr/bin/python3 -m unittest discover -s tests -v`, direct
  `./sweeper/selftest.py`, and `/opt/homebrew/bin/node --check dashboard/sw.js`
  were also blocked before process start by the same interpreter/runtime
  policy. No test process, dashboard process, live DB, sweep, Gmail call,
  restart, deploy, stage or commit was run.

Blocker
* The implementation is not runtime-verified in this session because the node
  execution policy rejects every Python/Node interpreter invocation. Static
  diff validation passed, but the focused Node/temp-DB suite must still be run
  in an execution lane that permits the repository's existing interpreters.
