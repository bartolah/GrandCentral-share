# Grand Central Station - Build Spec

Owner: Bart Olah
Target machine: Mac Mini (persistent, home network) with OpenClaw installed
Builder: Claude Code
UI spec: `grand_central_dashboard.html` (the prototype in this folder is the binding visual and interaction spec)
Status: Phase 0 complete (accepted). Phase 1 (email x2) LIVE since 2026-07-03;
acceptance week running through ~2026-07-10. Added during the acceptance week
at Bart's request (2026-07-04): re-file feedback loop, Google Calendar via ICS
secret URLs, and the draft-reply path (a Phase 3 element pulled forward —
**Bart explicitly approved this first mailbox write path**: IMAP APPEND of
Claude-drafted replies to Drafts with the \Draft flag; sending remains
human-only in Gmail, per §6).

Also added 2026-07-04, same day, a second explicit decision: a light email
client inside the dashboard (expand a thread to read it in full, reply —
AI-drafted-then-edited or written from scratch, both still Drafts-only —
and organize: dismiss/archive/star/label) plus a natural-language command
bar. **Bart explicitly approved three more mailbox writes beyond
Drafts**: archive (remove the \Inbox label), star (\Flagged), and
label add/remove (X-GM-LABELS) — still no delete, still no send. The
command bar is Claude with tool-calling restricted to the exact same
validated functions the dashboard's buttons call; there is no tool that
sends mail and no tool that acts without a concrete thread_id (no bulk or
unscoped actions), so it carries no capability the UI doesn't already have.

---

## 1. What this is

A personal chief-of-staff system. A scheduled sweeper reads Bart's communication and work surfaces (email, Slack, Granola, Attio), classifies everything by project, and writes a single JSON state file. A local web dashboard renders that file as a birds-eye command board with one-level-deep project drill-ins.

The system's job is to answer, at a glance: what needs me today, what am I waiting on from others, and what is going stale.

### Non-goals

- No team access. This is for Bart only.
- No auto-sending of anything, ever. Outbound = drafts by default; the sole exception is a single human-confirmed reply via the dashboard's Send confirm dialog (E3-P4, approved by Bart 2026-07-10 — see §6 for the guardrails).
- No deep task management. Detail work stays in Shortwave, Attio, Claude, etc. The dashboard says where to look.

---

## 2. Architecture: one contract, two sides

```
[ Sweeper (scheduled) ]  -->  grand_central.json  -->  [ Dashboard (local web) ]
```

The sweeper and the dashboard NEVER talk to each other directly. The JSON file is the entire interface between them. This is a hard rule. It means:

- Either side can be built, tested, and replaced independently
- The dashboard can be developed against hand-edited sample JSON
- Adding a data source in a later phase only adds fields to the JSON, zero UI risk

### Components

1. **Sweeper** - Python 3, run by launchd on a schedule (06:00 and 12:00 to start). Reads sources, calls the Claude API for classification, writes `grand_central.json` atomically (write temp file, then rename).
2. **Dashboard** - static HTML/JS/CSS (evolve the prototype), served by a minimal local web server (Python http.server or Caddy) on the home network. Reads `grand_central.json` on load and re-polls every 5 minutes.
3. **State directory** - `~/Code/GrandCentral/` on the Mini:

```
~/Code/GrandCentral/
  sweeper/           # Python source
  dashboard/         # HTML/JS/CSS + grand_central.json
  config/            # .env (chmod 600), account list, project registry,
                      #   overrides.json (re-file ground truth), dismissed.json
                      #   (local-only board dismissals — never touches Gmail)
  logs/              # one log per sweep, rotate at 30 days
  capture/           # inbound capture items (Phase 4)
```

4. **Project registry** - `config/projects.yaml`, the master list of initiatives, their aliases, key people, and Slack/Gmail sources. The classifier prompt is built from this file. Bart edits this file, not code, to add or tune projects.

---

## 3. The JSON contract

`grand_central.json` schema. The prototype's `PROJECTS` array in the HTML matches this shape and serves as a worked example.

```json
{
  "generated_at": "2026-07-03T06:00:12Z",
  "next_sweep_at": "2026-07-03T12:00:00Z",
  "sources_swept": ["gmail:personal", "gmail:connectblue", "slack:cb", "granola", "attio"],
  "summary": {
    "replies_owed": 7,
    "waiting_on": 5,
    "stale_projects": 3,
    "meetings_today": 4,
    "deals_advancing": 2
  },
  "top5": [
    { "text": "Sign or kill the CRL venue contract", "project": "crl", "done": false }
  ],
  "projects": [
    {
      "id": "connectblue",
      "name": "ConnectBlue",
      "small": "Ad Network / DMP / MegaFeed / Newsbot / SalesDB",
      "role": "Core",
      "health": "warn",              // ok | warn | alert
      "touch_days": 0,               // days since last activity involving Bart
      "prio": "one-line top priority",
      "subs": [
        { "n": "Newsbot", "h": "warn", "s": "Josh waiting on source list approval" }
      ],
      "tasks": [
        { "t": "Approve Newsbot source list", "o": "today" }
      ],
      "waiting": [
        { "who": "Josh", "what": "FB video resubmission", "age_days": 3, "channel": "slack" }
      ],
      "comms": [
        { "src": "gmail", "who": "Alex R.", "what": "PMP partner terms", "age": "2d", "link": "message deep link if available" }
      ],
      "meeting": "HTML string, last Granola meeting summary + action items",
      "note": "HTML string, latest brain doc note"
    }
  ],
  "reply_debt": [
    { "src": "gmail", "who": "Alex R.", "what": "PMP partner terms", "age_days": 2, "project": "connectblue", "link": "" }
  ],
  "waiting_on": [
    { "who": "Josh", "what": "FB video resubmission", "age_days": 3, "project": "connectblue", "nudge_state": "none" }
  ],
  "meetings_today": [
    { "time": "09:00", "title": "ConnectBlue team standup", "granola": true }
  ],
  "yesterday_actions": [
    { "text": "action item extracted from Granola", "project": "connectblue" }
  ],
  "pipeline": [
    { "deal": "Media partner PMP - Tier 1 pub", "amount": "$48k", "stale_days": 0, "project": "connectblue" }
  ]
}
```

Rules:

- Health lamps: `alert` if touch_days > 7 for advisor projects or > 3 for core projects, or if any waiting/reply item exceeds 7 days. `warn` at half those thresholds. Otherwise `ok`. Thresholds live in `projects.yaml` per project, these are defaults.
- Every list is sorted by urgency (age descending) before writing.
- If a source fails during a sweep, still write the file, mark the source absent from `sources_swept`, and the dashboard shows a stale-source warning in the footer. Never write a partial file that looks complete.

---

## 4. The initiatives (seed for projects.yaml)

| id | name | role | notes |
|---|---|---|---|
| connectblue | ConnectBlue | Core | subs: Ad Network/PMP, Audience DMP, MegaFeed, Newsbot, SalesDB, Mixpost. Attio = CRM, Brevo = newsletters |
| crowdblue | CrowdBlue | Core | subs: Warner page, user acquisition, agentic news rethink, local platform build |
| capitol | Capitol Influence | Core | shares ConnectBlue team |
| crl | CRL (Centre for Responsible Leadership) | Team of 4 | NYC event August, highest near-term urgency |
| … | partner and advisor organisations | | omitted from the shareable copy |

Key people to weight as high-priority senders: see `key_people` in config/projects.yaml.

---

## 5. Phases

Build strictly in order. Do not start a phase until the previous phase's acceptance test passes and Bart has used it.

### Phase 0 - Skeleton (target: one session)

- Create the directory structure above
- Convert the dashboard prototype: delete the hardcoded PROJECTS array, fetch `grand_central.json`, render everything from it, poll every 5 min
- Write `sample.json` by hand matching the schema, covering all 10 projects
- Stub sweeper that copies sample.json to grand_central.json and stamps generated_at
- launchd plist running the stub at 06:00 and 12:00
- Serve dashboard on http://[mini].local:8080

**Accept:** Bart opens the dashboard from his laptop, edits sample.json, sees the change after refresh. launchd log shows scheduled runs.

### Phase 1 - Email, two accounts

- IMAP readers for personal Gmail + ConnectBlue Gmail via app passwords (2FA must be on; passwords into .env)
- Sweep last 7 days of INBOX + Sent
- Classifier: one Claude API call per batch of threads. Input: thread metadata + snippets + the project registry. Output: project id, reply_owed (bool + who + age), touch signal, one-line summary. Use the cheapest model that is reliable (start with Haiku, escalate to Sonnet only if classification quality demands it)
- Compute per-project touch_days, reply_debt, comms, summary counts
- Write real grand_central.json

**Accept:** reply debt panel shows real threads with correct ages. Bart lives with it for one week and confirms classifications are mostly right before Phase 2. Track misclassifications in a notes file to tune the registry aliases.

### Phase 2 - Slack, Granola, Attio (three independent jobs)

- Slack: one workspace at a time (start with ConnectBlue). Bot or user token, read scopes only. Detect: direct mentions, DMs unanswered by Bart, threads where Bart was asked something and went quiet
- Granola: pull yesterday's meetings, extract action items assigned to Bart and commitments Bart made, feed yesterday_actions and per-project meeting fields
- Attio: API key, list open deals, compute stale_days from last activity, feed pipeline panel
- Source chips (gmail/slack/signal/wa/granola) render per the prototype

**Accept:** each panel shows real data with correct source chips. A meeting held yesterday produces action items today.

### Phase 3 - Waiting-on and nudges

- Detect waiting-on: Bart's sent messages (email or Slack) containing a request, with no reply from the recipient after N days. Also allow manual entries via a `waiting.yaml` file as a stopgap
- Nudge button: POST to a tiny local endpoint on the Mini; sweeper-side code writes a draft follow-up into the correct Gmail account's Drafts folder (via IMAP APPEND to Drafts). NEVER send. Update nudge_state to "drafted" in the JSON
- Aging clocks and color thresholds per section 3

**Accept:** a known unanswered ask appears in Waiting On within one sweep. Clicking Nudge produces a sensible draft in the right account's Drafts folder.

### Phase 4 - Scale-out

- Remaining Google accounts, one at a time (registry entry + .env credentials, no new code)
- Capture inbox: a dedicated email address the sweeper reads; anything sent there gets classified and lands in the right project (this is the entry path for Signal/WhatsApp obligations and stray ideas)
- Tailscale on the Mini; dashboard reachable from iPhone/iPad anywhere; iPad full-screen mode as ambient board
- Migrate IMAP app passwords to a proper Google Cloud OAuth project if app passwords prove fragile (candidate to hand to Morgan Ellis)

**Accept:** an email to the capture address appears on the board, correctly classified, within one sweep. Dashboard loads on the iPhone over Tailscale.

---

## 6. Security requirements (all phases, non-negotiable)

- All credentials in `config/.env` with chmod 600, or macOS Keychain. Never in source, never in logs, never echoed
- FileVault on for the Mini
- Dashboard binds to local network only until Tailscale; never port-forward to the open internet
- Claude API calls: send thread snippets and metadata, not full mailbox dumps. Log token usage per sweep
- Outbound: **drafts by default; sending exists behind a single human-confirmed button** (approved by Bart 2026-07-10, E3-P4). `sweeper/send_mail.py` + `POST /send` may transmit one reply per explicit human click in the dashboard's compose confirm dialog. Guardrails, non-negotiable: send is never exposed as a tool to the command bar, to any Claude tool-calling surface, or to OpenClaw skills; one thread per request, ≤10 recipients, no bulk send of any kind; the server requires `confirm:true` plus the recipient list the human saw in the dialog; `send_mail.py` deliberately has no CLI runner (§7 exception); every send is logged to the `mail_sends` table. Any widening of this (scheduled send, send-without-confirm, agent-initiated send) requires a new explicit decision by Bart, not a spec reinterpretation
- The sweeper/dashboard run read-only against every source except: writing grand_central.json, writing to Drafts folders (Phase 3), archive/star/label writes to Gmail (approved 2026-07-04, `sweeper/mail_actions.py` — no delete), SMTP send of a single human-confirmed reply (`sweeper/send_mail.py`, approved 2026-07-10), local dismiss/triage state in the DB, and their own logs
- **Delete = Trash (approved by Bart 2026-07-17, E7-P3):** the 2026-07-04 "no delete" rule is amended to allow moving a thread to Gmail's Trash — recoverable for 30 days, human click only behind a two-step confirm, single-thread or explicit-id bundle (≤50), never a command-bar/agent tool. There is deliberately NO purge/empty-trash verb anywhere in the system, and none may be added without a new explicit decision
- **Snooze re-inbox (approved by Bart 2026-07-15, E5-P1):** snoozing a thread on the Mail screen archives it in Gmail, and `mail_sync.py`'s wake pass re-adds `\Inbox` when the snooze time passes (`mail_triage.wake_inbox` marks the promise) — the one scheduled mailbox write in the system, restricted to threads Bart explicitly snoozed. ✎ re-files also mirror a `GC/*` label swap onto the thread (E5-P2, single-thread, within the 2026-07-04 label scope). Bulk auto-labeling by the sweeper remains NOT approved
- **Calendar create (approved by Bart 2026-07-19, E21):** the system may create events on Bart's OWN Google calendars via `POST /calendar` → `sweeper/calendar_writer.py` → the `gog` CLI (per-account OAuth refresh token in Keychain; the ICS feeds remain the read path). Scope, non-negotiable: create-only — no update/delete/RSVP verbs exist and none may be added without a new explicit Bart decision; NO attendees ever (an attendee list emails invites and writes to other people's calendars — that is outbound, §6); every create audited in `calendar_writes`. This endpoint IS available to OpenClaw/agent surfaces — it writes only to Bart's own calendar, unlike send
- **New-email send (approved by Bart 2026-07-29, E53):** the 2026-07-10 send amendment is widened from replies-only to NEW emails — Bart's explicit words: "New email and emails should be able to be sent entirely from within Grand Central." `send_mail.send_new` + `POST /send {new:true}`. Same non-negotiable guardrails as send_reply: confirm:true + the exact recipient list the human saw, ≤10 recipients, ≤20k chars, subject required, every send audited in `mail_sends` (thread_id='' marks a fresh send), never a command-bar/Claude/OpenClaw tool, no CLI runner. Scheduling remains replies-only (the outbox is thread-keyed); widening that needs a new decision. Live-verified self-addressed 2026-07-29
- **Attachments (approved by Bart 2026-08-02, E82 — "let's add basic attachment support"):** the confirmed send (reply, forward, and new) may carry **≤3 files, ≤5MB total**, picked by the human in the composer, base64 in the same `POST /send` the human confirms, decoded server-side and attached via `EmailMessage.add_attachment` — never written to disk, never on scheduled sends (outbox stores text only). Read side: `GET /attachment` decodes ONE part per request straight off IMAP (read-only, uid verified against the thread, forced-download + nosniff, ≤25MB) — attachment CONTENT is still never stored anywhere in GC (the E81 body cache keeps metadata only). Bigger files keep the 📎-via-Gmail hatch (E65). Widened E104 (2026-08-03, Bart: image/GIF support): additionally ≤4 INLINE images per send — cid-related to the HTML part, same 5MB total, same confirmed path; scheduled sends remain plain-only
- **Credential-write UI (approved by Bart 2026-07-28, E51):** the "never echoed" credential rule is amended to allow *writing* (never reading back) credentials from the dashboard. The Connections tab → `POST /connections` (`sweeper/connections.py`) is the sole path that writes `config/.env`: it adds/removes Gmail accounts (as `GMAIL_<SLUG>_USER`/`_APP_PASSWORD` pairs — accounts are now derived from `.env` by `sources/gmail_reader.scan_accounts`, no hardcoded list) and sets/clears the Attio key, Granola token, Anthropic key, and Calendar ICS URLs. Guardrails, non-negotiable: writes are atomic + chmod 600 + backed up to `config/backups/`; secret VALUES are never returned to the browser or logged (status is presence + a masked last-4 hint only); credentials are validated against the live service before an account is added (IMAP login) where a cheap check exists. Per Bart's explicit decision, this endpoint is reachable from anywhere **behind Cloudflare Access** (not LAN-only) so accounts can be added while traveling; it is NOT exposed as a command-bar/Claude/OpenClaw tool. Any widening (reading a value back, an agent-callable credential tool) requires a new explicit Bart decision
- **Network trust + CSRF (built 2026-09-03, E161, Bart: "Should we add some authentication?" -> "implement 1 and 2"):** the board had no authentication of any kind and bound every interface, so any device on the Mini's LAN could `POST /send` unauthenticated; and `do_POST` never checked Content-Type, so a page on any other site could fire a write here as a CORS "simple request" with no preflight (verified against `/mail-action` 2026-09-03 before the fix). Rather than add a bespoke password layer -- which duplicates Cloudflare Access, and which a login cookie would have made *worse* against CSRF, since the browser attaches it to forged requests -- the board now leans on the two authenticating systems already in front of it. (1) `Server.verify_request` refuses any peer that is not loopback (`127.0.0.0/8`, `::1`) or tailnet (`100.64.0.0/10`) before a byte of HTTP is parsed; cloudflared connects over loopback, so the Access-fronted tunnel is unaffected. Escape hatch `GC_ALLOW_LAN=1` reopens the LAN without a code change if Tailscale is down. (2) mutating POSTs are rejected with 403 when they carry a foreign `Origin`/`Referer`; requests with neither header are allowed, since non-browser clients (curl, the MCP server) are not the CSRF vector, and GETs are untouched so ordinary links into the board keep working. Allowed origins self-configure from the request's own `Host`, plus an explicit allowlist (`GC_ALLOWED_ORIGINS`) that names the tunnel hostname in case cloudflared is ever set to rewrite Host. Consequence for Bart: `gc-host.local:8080` no longer works -- use `gc-host.your-tailnet.ts.net:8080` or the tunnel. Note the limit of this: neither control defends against a process already running as Bart's uid on the Mini (including a shell tool in an agent), which is not solvable with tokens
- **Agent surface via MCP (built 2026-09-02, E160):** OpenClaw and other MCP clients reach Grand Central through `integrations/mcp/gc_mcp.py`, a stdio MCP server, instead of prose instructions plus hand-built curl. This does not widen what an agent may do — the tool set is exactly the surface `integrations/openclaw/SKILL.md` already sanctioned — it makes the limits structural rather than advisory. The server is a thin client over the dashboard HTTP API (no direct SQLite, so the endpoints remain the single write path); `gc_client.ALLOWED` is a hard `(method, path)` allowlist checked before any socket opens; `gc_client.DENIED` refuses `/send`, `/mail/bulk`, `/outbox`, `/connections`, `/command`, `/upload`, `/dev` and `POST /registry` by name with the governing decision attached; `gc_client.ACTION_ALLOW` blocks the dangerous verbs that share a route with safe ones (`trash` on `/mail-action`, `delete` on `/context`). There is no send tool, no trash tool, no bulk tool and no credential tool for a model to call. `--selftest` asserts all of this against the live board and must pass after any change to that directory. Note the scope of the guarantee: the dashboard still has no auth, so a shell tool in any agent can reach `POST /send` directly — MCP closes the sanctioned path, not the network. Closing that gap would need a loopback token on the mutating endpoints, which is a new decision, not a reinterpretation
- **Dev queue pairing (built 2026-09-14, E162, Bart: "Let's start with the critical step" -> chose device pairing code):** the E144 dev queue is executed unattended by `sweeper/dev_worker.sh`, which used to hand queue text to `claude -p --dangerously-skip-permissions`. Because `POST /dev` accepted any request without an Origin, anything reaching the board over HTTP (a local process, a tailnet device, an agent shell tool steered by injected email) could turn text into committed, pushed, auto-pulled code running beside every credential. Two controls now. (1) Provenance: `add`, a `text` edit, and a status set back to `open` require the `X-GC-Dev-Code` header to match the pairing code (`sweeper/dev_pairing.py`; only a SHA-256 is stored, `data/dev_pairing.json`, 0600). Bart enters the code once per device in the Dev requests panel (kept in localStorage); `--new` rotates it and unpairs every device. Status/note writes stay open so sessions can answer cards, and `building -> open` (a session handing a request back with a question) needs no code because the text was already paired. (2) Blast radius: the worker runs `--restricted --permission-mode dontAsk` with the allowlist in `sweeper/dev_worker_settings.json` -- file tools confined to the repo, edits limited to dashboard/sweeper/integrations/capture and docs, no reads of `config/.env*`, `config/backups/`, `data/`, `logs/`, no edits to its own files, `send_mail.py`, `connections.py` or `auto_pull.sh`, no WebFetch, and Bash only for git (no `add -f/-A`, no force push), `sweeper/dev_check.py`, `sweeper/dev_answer.py` and the dashboard restart. The request text is passed in the prompt, so the session never reads notes others wrote. Verified 2026-09-14 by a probe session against a decoy `.env` file. Scope as E161: a process already running as Bart's uid can still read the DB or `.env` directly; this closes the queue to everything that only reaches the board over HTTP. `gc_client.DENIED` keeps refusing `/dev` for MCP.
- **Browser push (built 2026-09-19, E188, Bart: "I'd also like it to use browser notifications in my mac and on my iphone" -> chose real Web Push + per-alert channels):** a new outbound path, and therefore a §6 decision. Grand Central now signs an encrypted payload with a VAPID key (`config/.env`, private key never served) and posts it to the push endpoint the BROWSER chose — Apple for Safari, Google for Chrome. Scope: the payload carries the same one-line alert text the iMessage path already sends (brief, event reminder, day's-end wrap, system alert) and a URL back into the board; never mail bodies, credentials, or anything about third parties. Destinations are not ours to invent: a row in `push_subs` exists only because Bart clicked Turn on in a browser, and it is deleted the moment the push service answers 404/410. `notify_prefs` decides per alert kind whether it goes to iMessage, the browser, or both (default both). The private key stays in `config/.env` (chmod 600, gitignored); `GET /push` returns only the PUBLIC key, the device labels and the prefs. Requires HTTPS, so push exists on the tunnel and not on the tailnet HTTP address — and on iPhone only once the board is on the home screen (E187).
- The Mail screen (E3) renders from `GET /mail` (SQLite, fed by `sweeper/mail_sync.py` every ~3 min); `grand_central.json` remains the sole contract for the three board altitudes. The Mail screen's UI is specced by the E3 plan + the Shortwave screenshots in `data/uploads/`, not the legacy prototype HTML

---

## 7. Conventions for Claude Code

- Python 3.11+, requests/imaplib/slack_sdk, no heavy frameworks. Keep the sweeper a set of small source modules behind one `sweep.py` entry point
- Every module independently runnable for testing: `python -m sources.gmail_reader --account personal --dry-run`
- Atomic JSON writes (temp + rename). The dashboard must never read a half-written file
- One log file per sweep in logs/, plain text, include per-source timings and counts
- When in doubt about a product/UI decision, match the prototype HTML exactly
- Commit after each working milestone with a message naming the phase step

## 8. Open questions for Bart (answer before or during Phase 1)

1. Which two Slack workspaces after ConnectBlue matter most?
2. Confirm 06:00 and 12:00 sweep times, or prefer hourly during work hours? *(Answered: hourly 07:00–19:00.)*
3. Should CRL's thresholds be tightened until the August event (e.g. alert at 2 days)?
4. Capture address: create a new dedicated Gmail, or use plus-addressing on personal?

## 9. Later candidates (noted, not scheduled)

- **Shortwave** (2026-07-03): Bart uses and likes Shortwave — especially its
  email bundles and todo groupings, which he uses heavily. Two angles, in order:
  (a) borrow its UX ideas (bundling related threads, todo-style groupings) for
  the dashboard's email panels; (b) possibly integrate it as a source/interface
  later — its public API is limited, so re-evaluate when we get there. Any
  integration stays read-only per §6.
