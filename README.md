# Grand Central

A personal chief-of-staff dashboard, built for one user (Bart) on a Mac mini.
It sweeps two Gmail accounts, Google Calendar, and Attio into a single command
board that answers three questions at three altitudes: *what needs me today*
(Operational), *is the work advancing the goals* (Strategic), and *what's in
the inbox* (Mail). Outbound is drafts-by-default; the system has exactly one
human-confirmed send path and no destructive verbs beyond a recoverable
move-to-Trash.

Built incrementally by Claude Code sessions working against `BUILD_SPEC.md`,
with every capability widening recorded as an explicit owner decision.
The running log of what exists and what's next lives in `ToDos.md`.

## Architecture

One rule holds everything together: **the sweeper and the dashboard never talk
directly**. A scheduled sweeper writes `dashboard/grand_central.json`; the
static dashboard renders it. Adding a data source adds JSON fields — it can't
break the UI. The Mail screen is the single sanctioned exception: it reads
`GET /mail` from SQLite, fed by a lighter 3-minute sync job.

```
                 hourly 07:00–19:00                     every 3 min
  Gmail ×2 ──┐                                     Gmail INBOX headers
  gcal ICS ──┼─→ sweeper/sweep.py ─→ grand_central.json      │
  Attio   ──┘        │                    │            mail_sync.py
                     ▼                    ▼                  │
             data/grand_central.db   dashboard (static HTML/JS,
             (SQLite system of       serve.py on :8080)
              record: mappings,           ▲
              tasks, decisions,           └── POST endpoints for actions
              triage, tags, sends)            (validated in sweeper/*)
```

**The mapping engine** files every email thread under an operation with strict
precedence: Bart's ✎ corrections (ground truth, permanent) > deterministic
registry rules (aliases/people/domains) > a Haiku classifier (with a
confidence floor — below it, threads land in an Unassigned tray rather than
being guessed). The registry of theaters → operations → objectives lives in
`config/projects.yaml`, editable from the dashboard's Configure screen and
inline on the Strategic screen.

**Today's Top 5** is scored, not chronological: the classifier judges
`reply_needed` and `importance` per thread; the score adds operation priority,
theater role, key-person boosts, and capped staleness; auto-replies are
structurally excluded; max two entries per theater.

## The screens

- **Strategic** — objectives grouped by theater, each with victory metrics and
  progress (editable inline via ✎), trend arrows, insight chips (email
  threads, tasks, decisions, meetings tied to each goal through its feeding
  operations — all click-through), and the read-only Attio pipeline sorted
  stalest-first.
- **Operational** — theaters and operations with touch/staleness lamps, reply
  debt, Decision Queue, native tasks, Top 5, Signals In, unassigned tray, and
  per-operation drill-ins (engagements / people / comms / decisions / notes).
- **Mail** — a Shortwave-style bundled inbox: People/Calendar/Notifications/
  Newsletters/Receipts bundles, or grouped by Projects (rebuilt from the
  mapping engine) or Goals; keyboard triage (`j/k/e/s/#/p/x`); per-bundle
  archive-all and delete-all behind two-step confirms; account chips (CB/P);
  HTML email rendering in sanitized, sandboxed iframes.

## Email actions and what they really do

| Action | Gmail effect |
|---|---|
| Done / Archive | Real archive (`-X-GM-LABELS \Inbox`, issued from All Mail) |
| Snooze | Real archive now; the sync job re-adds `\Inbox` at the wake time |
| Delete | Move to Gmail Trash — recoverable 30 days; **no purge verb exists** |
| Star / Labels | Real Gmail writes |
| ✎ Re-file | Mirrors a `GC/<Theater>/<Op>` label swap onto the thread |
| Dismiss / Pin | Local board state only — Gmail untouched (by design) |

Threads can carry one *primary* project (what the mapping engine learns from)
plus any number of extra project tags and goal tags; a thread appears under
each of its projects in the Mail groupings, and goal-tagged mail surfaces on
all three altitudes.

## Automation (launchd)

| Job | Label | Cadence |
|---|---|---|
| Sweep | `com.example.grandcentral.sweep` | hourly, 07:00–19:00 |
| Mail sync | `com.example.grandcentral.mailsync` | every 3 min |
| Dashboard | `com.example.grandcentral.dashboard` | always on, :8080 |
| Cloudflare tunnel | `com.example.grandcentral.tunnel` | always on |
| GitHub auto-pull | `com.example.grandcentral.autopull` | every 5 min (ff-only) |

## Access

- **On the Mini**: `http://localhost:8080`
- **Tailscale**: `http://gc-host.your-tailnet.ts.net:8080` (tailnet-private) —
  this is the everyday off-Mini URL now
- **LAN**: closed as of E161. `http://<mini>.local:8080` is refused, because
  the board has no password of its own and an open LAN listener let any device
  on the network send mail as Bart. Set `GC_ALLOW_LAN=1` on the dashboard
  service to reopen it if Tailscale is ever down.
- **Anywhere**: `https://gc.example.com` — Cloudflare Tunnel,
  outbound-only, fronted at all times by Cloudflare Access (email OTP,
  owner only). Setup and the never-run-without-Access rule: `TUNNEL_SETUP.md`.

## Security model (BUILD_SPEC §6)

- Credentials only in `config/.env` (chmod 600) or Keychain; never in source,
  logs, or the repo. Generated JSON, logs, and the DB are gitignored.
- Read-only against every source except an enumerated list of writes:
  the JSON/DB, Gmail Drafts, archive/star/label (2026-07-04), one
  human-confirmed SMTP send (2026-07-10), snooze re-inbox (2026-07-15),
  move-to-Trash (2026-07-17), and logs. Each widening was an explicit,
  dated owner decision recorded in the spec.
- **One send path**: `POST /send` behind a confirm dialog showing the exact
  recipients — never a command-bar or agent tool, one thread, ≤10 recipients,
  every send logged to `mail_sends`.
- Delete means Gmail Trash. There is deliberately no empty-trash/purge verb
  anywhere in the system.
- The `Ask ›` command bar runs Claude with tool-calling restricted to the same
  validated single-thread actions as the buttons — no send, no bulk, no delete.

## Repo layout

```
sweeper/            the write side: sweep orchestrator, readers (sources/),
                    classifier, mapping engine, mail sync + actions, send path,
                    SQLite layer, registry editor
dashboard/          the read side: index.html (board), mail.js (Mail screen),
                    serve.py (static + validated action endpoints)
config/             projects.yaml registry, launchd plists, .env (untracked)
BUILD_SPEC.md       the phased spec + §6 security decisions (the contract)
ToDos.md            living ledger: built / in-flight / remaining
TUNNEL_SETUP.md     Cloudflare Tunnel + Access runbook
MISCLASSIFICATIONS.md  append-only ✎ correction log
RECONCILIATION.md   early model/architecture decisions
```

## Running it

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config/.env.example config/.env   # Gmail app passwords, ANTHROPIC_API_KEY,
                                     # ATTIO_API_KEY…  chmod 600 config/.env
.venv/bin/python sweeper/sweep.py    # one sweep, writes the board JSON
.venv/bin/python dashboard/serve.py  # board on :8080
```

Every module is independently runnable with `--dry-run` for testing
(`python -m sources.gmail_reader --account personal --dry-run`); mutating
CLI verbs in `mail_actions.py` default to dry-run on purpose.


## Before first run

This copy ships examples in place of two personal files:

    cp config/.env.example config/.env                    # accounts + app passwords
    cp config/projects.example.yaml config/projects.yaml  # reply thresholds, key people

Nothing else is required. `data/` and `logs/` are created on first run.
