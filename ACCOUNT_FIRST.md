# Account-first simplification

Direction (Casey, 2026-09-24): accounts are the primary navigation and the
ownership boundary. Emails are only ever seen through their own account. Todos
belong to exactly one account and show in that account's view. Projects, goals,
automatic classification, Attio, and the old dashboard go. Cross-account global
todos are deferred (not built).

Amended 2026-09-29 (BUILD_SPEC §6): ownership stays exactly as above, but a
read-only Home tab may show every account at once, each in its own section,
and the day note and conflict spotting may read across accounts. Nothing is
re-owned or acted on outside its account. The account-boundary code and tests
stay; the new Home needs its own tests that prove it only reads.

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
and Casey lost the ability to delete from the email view. The six-button hover
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

**Fixed 2026-11-03 (Casey: "Yes please fix").** Label names now go to Gmail
in modified UTF-7 (`mail_actions._qlabel`: `&` → `&-`, non-ASCII → `&…-`) on
STORE, SELECT, RENAME and `label_thread`. `todo_labels` matches labels by
their decoded text (`_key`), because GC stores the label it made as text and
Gmail lists it back in wire form; without that, the first successful write
would have spawned a duplicate todo on the next pass. Tests:
`tests.test_account_isolation.LabelAmpersand`.

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

## Jev-led Smart grouping (E226, 2026-09-28)

Casey: "switch our sorting logic to use the Jev context. Jev should help us with filtering, grouping, etc." Start with Brightline, add the others once he likes it.

- Per account, following the existing Jev switch (`class_accounts.jev`); `/mail` returns `jev` so the page knows. Accounts without it keep E204's date-banded Smart grouping exactly.
- A thread's type is the classifier's effective answer (Casey's correction > Jev when confident > rules). Only a type decided by Jev or Casey moves a thread; rule-only types keep today's placement.
- Bulk mail never reaches Jev (§6 2026-09-26), so newsletter/promo/receipt/calendar/update bundles still come from `categorize()`. Jev adds two bundles for person-looking mail: Cold pitches and Spam (and can send misfiled machine mail to Updates/Newsletters). Those two offer Archive all, not Delete all.
- Sections: Needs you (respond = yes, or priority 3) → Team → Prospects → Correspondence, each by priority, then value, then newest; then one "Bundled" section.
- Filters `type:` `priority:` `needs:` are GC-only and stripped before a Gmail search.
- Nothing here writes to Gmail; classification stays advisory.

## Projects — spec (interview with Casey, 2026-09-29)

Mockups: Grand Central Design canvas, artboards "Project page" and "Projects page + Add to project" (`design/Project.dc.html`, `design/Projects.dc.html`). Not built yet.

**What a project is.** A standalone page, a hybrid of a todo page and Smart Inbox. It points at things; it never moves them. Anything can be added from anywhere with one verb, 📁 Add to project: emails (threads), todos, notes, calendar events (past or upcoming, with their Granola notes) and contacts. A project can hold many of each.

**Ownership (BUILD_SPEC §6, 2026-09-29).** Emails, todos and events stay in their own accounts, and acting on them happens there. A project, and a note written inside it, belong to no account. Each item on the page shows its account chip.

| Question | Casey's answer |
|---|---|
| An email added to a project | Stays in its inbox; the project is a second view onto it |
| A todo's emails on the project page | Folded inside the todo row, shown once |
| Dates | Optional target date, plus completed-on recorded when marked complete |
| Completing with open todos | Ask: "3 todos are still open — complete them too?" |
| Contacts | A People list; a new email from one of them prompts "add to <project>?" and Casey decides (no auto-filing) |
| Ask Grand Central on a project page | Reads everything filed in that project, across its accounts, and nothing outside it |
| Gmail labels for projects | Not now (would be a live write in every account involved) |
| Pinned projects | Up to 3, as tabs after the account tabs behind a divider (phone: in the ☰ drawer, assumed) |
| Deleting a project | Keeps all content; notes written inside it move to Notes, marked with the project's name |
| A new todo created inside a project | Account picker, defaulting to the last account used in that project |
| Calendar events | Any event, past or upcoming |

**Also from Casey's brief:** projects can be completed and reopened; created from a Projects page, the sidebar or a Projects section in the right-hand panel; administered (rename, colour, target, pin, complete, delete) on the Projects page.

**Build order (proposed):** (1) tables + API: projects, project_items (kind, account, ref), project notes; (2) Projects page, project page and pins; (3) 📁 Add to project on emails and todos; (4) notes, events and contacts; (5) project-scoped Ask; (6) the People suggestion on new mail. Each item is keyed by (account, id), never by id alone.

**Steps 5 and 6 — agreed with Casey 2026-09-30 (interview; BUILD_SPEC §6 entry of the same date).**

| Question | Casey's answer |
|---|---|
| Where project Ask lives | A box at the top of the project page; it opens the ✨ panel fenced to the project, and those chats are kept with the project |
| What it may do | Read and draft only: answers, summaries, reply drafts saved to Gmail Drafts for threads in the project. No archive/snooze/todo changes, no writing notes |
| Meetings | It reads the Granola notes (summary + action items) of meetings filed in the project, fetched when needed — not transcripts |
| Model | Whatever the ✨ panel is set to (Sonnet 5 by default), switchable |
| What triggers "Add to <project>?" | A thread whose latest message is from someone on the project's People list (not a CC; not their whole company) |
| Where it shows | All three: a "📁 <project>?" chip on the inbox row (✓ file / ✕ not this one), a banner in the open email, and a "Suggested" strip on the project page |
| Mail already there when a person is added | Their threads from the last 30 days still in an inbox are suggested once, on the project page |
| ✕ "Not this one" | Remembers that thread for that project only; the banner also offers "Stop suggesting <person> for <project>" |

Build notes: suggestions are computed, not stored, from project contacts × mail_threads (latest message inbound from that address), minus threads already in the project and minus a small `project_declines` table (project_id, account, thread_id | person). Bulk mail never suggests. Project Ask runs `command.py` with a project scope: its context is the project view, `get_thread`/`search`/`draft_*` refuse anything not filed in the project, and every other tool is absent.

**Steps 5 and 6 as built (E257, E256, 2026-09-30).** Step 6: `projects.suggestions()` is computed on read; ✕ writes `project_declines` (a thread, or a whole person for that project). Step 5: `sweeper/project_ask.py` holds the project prompt, context and four tools (`read_thread`, `meeting_notes`, `draft_preview`, `draft_save`). Each tool checks that its thread or meeting is filed in the project, so the fence doesn't depend on the prompt. `command.run_command(project_id=…)` swaps them in and drops the "currently open" selection. `ask_chats.project_id` keeps a project's chats with it; deleting a project turns them into ordinary chats. Granola notes are fetched on demand with `granola_reader.get_note()` (summary only).

**Step 4 as built (E254, 2026-09-30).**
- **Events.** The ICS feeds carry no stable event id, so an event is keyed by calendar_reader's own dedupe key, `date|time|title`. It's filed under the account its calendar maps to (`home_view.CAL_ACCOUNT`, sent with `/calendar`); a calendar with no account leaves the event without one. A rescheduled or renamed event therefore reads as a different event. Its Granola notes are matched from `elements` (same local day, matching title, within 3 hours), and only the link and title are shown.
- **People** are shared across accounts. The project page suggests up to 8 people from the participants of the project's own emails, never Casey's addresses and never no-reply senders. A contact can't be added twice: `add_item` checks first, because the UNIQUE index treats NULL accounts as distinct.
- **Notes.** A note sits in at most one project (`note_move`). The Notes page is outside the accounts, like Projects (`GET /notes`, `POST /projects` `note-move`). Neither route is in `gc_client.ALLOWED`.

## E259: context documents, and an Ask that knows Projects and Notes (2026-09-30)

Casey: "The Grand Central AI that's built in isn't aware of the changes that we've made to projects and notes … I would like to be able to upload additional context (like an MD file) or just paste in the context associated with any of my accounts or projects." And: reorganize the pending Brightline todos under no more than 10 Brightline projects.

- **Context documents** (`context_docs`: scope `account:<slug>` or `project:<id>`, title, body up to 100k characters, filename, on/off). Unlike `context_notes` (one statement, 500 characters), these are whole briefs. Pasted, or a .md/.txt file read in the browser (only its text is stored). Added from the project page (＋ Context) and from Settings → Context. `GET/POST /context/docs`; the POST body cap is 640 KB for this route only.
- **Who reads them.** The main Ask: the documents of the account on screen (the client sends `account`; not on ⌂ Home), plus a catalog of the rest and `read_context_doc`. Project Ask: the project's documents, then those of the accounts its items live in (60k characters). Reply drafts and reply options: the thread's account's documents (20k). Agents never: `/context/docs` is in `gc_client.DENIED` and outside the E258 agent gate's lists, because a brief can hold internal-only material. Writing one is human-only (`agent_gate.HUMAN_ONLY_POST`, a paired browser), since a document grounds every Ask and draft for its account. Deleting a project turns its documents into Notes marked with its name, like its notes.
- **The main Ask and Projects/Notes.** Its snapshot lists the open projects; `query_projects` (list, or one project resolved through `project_ask.context`), `query_notes` (search, or one whole note) and `read_context_doc` are read-only. Filing, creating projects and writing notes stay board clicks (§6 2026-09-29/30); the prompt says so. No §6 entry: nothing it may do was widened, only what it can read, and it could already read every account's mail.
- **Brightline set-up** (`config/seeds/brightline/`, `sweeper/project_seed.py`). A plan of 10 projects (Amendment, Signal Boost, Northstar Labs, Consulting, Grand Central, Brightline Partner Platform, NewsDesk, SalesBase, Brightline Marketing, Sales & Proposals), each with a short brief, plus two account documents: the Sep 30 Brightline context as given, and the older memory export with its internal rate card removed (the Sep 30 doc says pricing belongs in its own internal-only file) and em-dashes made plain dashes. Keywords sort the account's open todos that are in no project yet; a tie between two projects files into both, a tie of three or more is left out, and the two broad projects (Marketing, Sales) lose a tie to a specific one. Dry run by default, `--move ID="Project"` and `--skip ID` to correct a line, `--apply` to write. This is a one-off, reviewed filing on Casey's request, not a return of automatic classification. `config/seeds/` stays out of the shareable copy.

## E249: the ◫ Day Home and the ⌂ Home tab (2026-09-30)

Built under the 2026-09-29 §6 exception. What keeps it a view, not an owner:
* `home_view.section(conn, account)` reads one account (mail_view, todo_view,
  class_for_threads, all `account=`-scoped); `/home` is a list of those. No
  write path was added; `tests/test_home.py` asserts `total_changes` is
  unchanged after building Home and the note's signals.
* The client never loads another account's items into `ITEMS`. `HOME_ALL`
  only swaps what `#list` draws; `ACCT` stays the last real account, and
  opening a row on ⌂ Home calls `setAcct(its account)` first. Todos there have
  no checkbox: done happens inside the account.
* The cross-account note is one Haiku call per day (`day_note`, scope '')
  with titles, times, todo text and the assessor's one-line summaries,
  never snippets or bodies. Per-account notes see that account only.
* `/home` and `/home/note` are in `gc_client.DENIED`; Ask is unchanged.

## Teammates are never cold outreach; people in several companies (E252, 2026-09-30)

Casey: "Alex Rivera … is my boss and the owner of Fieldhouse, Brightline and Consulting … He's technically part of all three teams. We should add him to all teams as a teammate. His emails are never cold outreach. We have several people who are part of multiple accounts - companies."

- **The guard.** Who sent a message is a fact from the roster; what kind of message it is is Jev's judgement. Jev read what teammates forwarded and called 51 of Chris's 67 threads (and 139 of Jaime's) a cold pitch. `db.class_effective` now refuses Jev's `Cold pitch` and `Spam` when the rules say the sender is a teammate or in a company group (`JEV_NEVER_FROM_TEAM`); the rules' type stands, `jev_overruled` records what Jev said, and a reason line says so. Casey's correction still beats it. Jev's other answers (Prospect, Correspondence, respond, priority) are untouched. The criteria text in `jev.py` now says the same, without a `QVERSION` bump.
- **Several groups per person.** `class_memberships (email, grp)` holds any number of groups; `class_people.grp` mirrors the first for rollback. `mailclass.group_of(email, ros, prefer)` names the group of the account being read (`account_group`: the group of the account's own address's domain) when the person is in it. `/classify` gains `also` and `from` on `person-add`, plus `person-leave` and `person-mate`. Nothing about this sends Jev anything new.

## Your own services and website RFPs (E253, 2026-09-30)

Casey: "Let's make Jev smarter about email notifications from our own services and properties and websites … Brightline Notifications should be a bundle, Brightline Sales inquiries (RFPs) from sales@brightline." Interview answers: RFPs get their own bundle, kept prominent; services are found automatically and editable; one bundle per service (subdomain).

- **Detection** (`sweeper/services.py`, called from `mail_sync.py`). "Own domains" are every account's domains plus the class_groups domains. An automated sender on one of them becomes a service: on a subdomain it's keyed by host (`newsdesk.brightline.example`), on the bare domain by address (`no-reply@brightline.example`). A sender counts as automated if it writes from a subdomain, from a role address, or sends bulk mail. Roster people, Casey's own addresses and threads he replied to are never services. A Google Group relay carrying a tool ("'No Reply - Mailchimp' via Admin") becomes a service named after the group; one carrying a person stays people mail. Casey's own bulk sends become one "<Company> mailings" service per domain.
- **Sales inquiries.** Website forms relayed through sales@ ("'Brightline Site' via Sales", or a "New RFP" subject) are kind `sales`. They stay `people` mail, so Jev still reads them. The rules type them as Prospect, and `class_effective` refuses Jev's Cold pitch or Spam for them, the same guard as for teammates. In the classic Inbox their bundle sorts first while any is unread or last-inbound, and it has no Delete all. In Smart Inbox they're listed under Prospects.
- **Jev and §6.** Any other service's people/other mail becomes `notification`, so it stops reaching Jev. Nothing is widened.
- **Storage.** A nullable `mail_threads.service` column (the detected key, kept even when switched off so Settings can count it) and a `services` table (auto_name, Casey's name, merged_into, off). Casey's edits survive re-detection. `/mail` returns each thread's resolved `svc` plus a `services` map; a switched-off service keeps its old category at the next sync.
- **Settings → Classification → Your services** lists each account's services with thread counts. Rename (blank restores the found name), Merge into, and Switch off go through `POST /classify` actions `service-rename`, `service-merge` and `service-off`. The route isn't in `gc_client.ALLOWED`, and none of these actions touches Gmail.


## Open and click tracking (E269, agreed 2026-10-06)

Casey: "What's involved in order to have Grand Central track when my emails are opened?" → "Let's do it. Yes, this is through our Google Workspace for sales and campaign emails." BUILD_SPEC §6 entry of the same date.

| Question | Casey's answer |
|---|---|
| Where the image and links load from | Each account's own domain: `t.<account domain>` (keeps Brightline and Consulting separate) |
| When tracking is on | On for work accounts when a recipient is outside the company groups; off for teammates and Personal; 👁 Track toggle per send |
| What it does with the signal | All four: opens on the thread, link clicks, a ping on the first real open, and feeding Waiting on. **Each switchable per account** |
| Campaigns | Composer mail only; bulk campaigns keep their sender's own stats |

**Shape.**
- `integrations/tracker/` holds a Cloudflare Worker (`worker.js`, `wrangler.toml`) with a D1 table of events. Routes:
  - `GET /o/<token>.gif`: logs an open and returns a 1×1 GIF.
  - `GET /c/<token>/<n>/<sig>?u=<url>`: logs a click, then 302s to `u` only if `sig` = HMAC(secret, token|n|u).
  - `GET /events?after=<id>`: needs a bearer secret; Grand Central pulls with it.
  - `GET /health`.

  Stored per event: token, kind, link index, time, a source label (`proxy-google`, `apple-prefetch`, `scanner`, `client`) from user agent, ASN org and timing, and country. No IP, no content.
- `sweeper/tracking.py`: per-account settings, the decision whether a given send is tracked, token minting, adding the image and rewriting links in the HTML part (plain text untouched), the event pull (run by mail_sync), stripping tracking from GC's own reading pane, and summaries.
- Tables: `track_sends` (token, account, thread_id, mail_sends id, subject, recipients, opts), `track_events` (pulled), `track_settings` (account → on/clicks/ping/waiting).
- **Real vs automatic opens:**
  - automatic: Apple's prefetch, known security scanners (by ASN), and anything within 30 seconds of sending
  - real: Gmail's image proxy (it fetches when the recipient opens) and ordinary mail clients

  "Opened" counts only real opens. Automatic ones are shown greyed as "probably automatic".
- **Limits, said plainly in the UI:**
  - With several recipients, an open is the email's, not a person's.
  - Gmail's proxy caches images, so repeat opens undercount.
  - Opening your own Sent copy in Gmail can count; Grand Central's own pane never does.
- **Deploy needs Casey:**
  - `wrangler login` (or a Cloudflare API token)
  - a DNS route `t.<domain>` per account domain
  - the shared secret, stored in `config/.env` as `TRACK_SECRET` via Connections

  Until `/health` answers on a domain, sends from that account aren't tracked.


## Follow-ups: "Keep an eye on this" (E276, 2026-10-07)

Casey: "help me keep track of and follow up on emails 'of note' … I don't want it to be too heavy handed, or crazy from a processing and token standpoint. perhaps after I send an email, part of the after send menu can ask me for context?" He approved the plan ("Love this. Go for it"), and the recommended defaults were used: 3 business days for the team, 5 days for deals; Remind me = a todo; ask when a teammate or someone outside is on the email; due items on both Home views.

- **Model (`sweeper/watch.py`, table `watches`).** There are three kinds: `assigned` (a teammate owes Casey), `deal` (a prospect or partner owes a reply), and `remind`. A `remind` row exists only until a new email's Gmail thread is known, so its todo can hold the email. Casey creates every watch; nothing is watched automatically.
- **Capture.**
  - A "Keep an eye on this?" row in the Sent menu (replies), and a small Sent ✓ prompt after compose-window sends.
  - ⋯ → 👀 on any email.
  - One tap saves with the default date; a note and date can follow (`edit`).
  - `suggest()` pre-picks by rules: teammates only → assigned; a VIP/Top prospect or a Prospect classification → deal.
  - Per-account ask mode (smart/always/never), set in 👀 Following up.
- **Closing.** `check()` runs every mail_sync with no model call.
  - An assignment closes when that teammate's reply arrives after the send. A deal closes when anyone but Casey replies. Both are read from `mail_threads`, or from cached bodies for earlier replies in the thread.
  - New emails now always carry a Message-ID Grand Central set (`send_new`), which `resolve()` uses to find their thread.
- **Surfacing.**
  - 👀 Following up in the rail: Due, Team owes me, Waiting on prospects & partners, plus closed this week.
  - Due items go first in Home's follow-ups (`home_view.section`, so on the all-accounts Home too) and get a line in the morning brief (`notify.py`).
  - The hover card shows "N open assignments from you".
- **AI.** Only ✨ Nudge, on click: the composer's ✨ box pre-filled with a follow-up prompt that Casey reviews.
- **Not built:** letting OpenClaw read the list. That needs its own §6 decision.
