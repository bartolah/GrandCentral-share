# Grand Central MCP server

A Model Context Protocol server that gives OpenClaw (or any MCP client) typed
tools for the board, instead of prose instructions and hand-built curl calls.

Built 2026-09-02 (E160). Replaces the curl recipes that used to live in
`integrations/openclaw/SKILL.md`; that file is now judgment and vocabulary only.

## Why

The old integration worked, but every safety rule was **advice a model had to
remember**: "NEVER call /send", "never loop archive over a list". That is a
prompt, and a prompt is not a control.

Here the rules are **structure**:

- there is no send tool, no trash tool, no bulk tool, no credential tool
- `gc_client.ALLOWED` is a hard allowlist of `(method, path)`; anything else is
  refused before a socket opens
- `gc_client.DENIED` names the forbidden paths explicitly, with the BUILD_SPEC
  decision that forbids each one, so the intent survives refactoring
- `gc_client.ACTION_ALLOW` guards routes that host a dangerous verb beside safe
  ones: `/mail-action` carries `trash`, `/context` carries `delete`

A model that decides to send email finds nothing to call.

## Files

| File | Role |
|---|---|
| `gc_mcp.py` | Tool definitions and the entry point. This is the file to read. |
| `gc_client.py` | HTTP client plus the safety allowlist. |
| `mcp_stdio.py` | Minimal JSON-RPC 2.0 / MCP transport over stdio. |

No third-party dependencies (BUILD_SPEC §7) — stdlib only, so the server cannot
break when a package upgrades. It is a thin client over the dashboard's HTTP
API and never touches SQLite directly, keeping the endpoints the single write
path (§2, §6).

## Running it

```bash
.venv/bin/python integrations/mcp/gc_mcp.py --selftest  # every read path + every guard
.venv/bin/python integrations/mcp/gc_mcp.py --list      # the tool table
.venv/bin/python integrations/mcp/gc_mcp.py             # serve on stdio
```

`--selftest` is the regression test. It exercises all eight read tools against
the live board and asserts that eight forbidden calls are refused. It writes
nothing. Run it after touching any of these three files.

## Registered with OpenClaw

```bash
openclaw mcp add grand-central \
  --command /Users/you/Code/GrandCentral/.venv/bin/python \
  --arg /Users/you/Code/GrandCentral/integrations/mcp/gc_mcp.py \
  --cwd /Users/you/Code/GrandCentral \
  --connect-timeout 20 --timeout 180
```

Check it with `openclaw mcp probe grand-central` (expects 24 tools) and
`openclaw mcp doctor`. `openclaw mcp reload` picks up code changes on the next
turn. Tools appear to the agent as `grand-central__<name>`.

## The tools

**Read (9)** — `get_brief` is the front door and answers most questions in one
compact call. `list_todos`, `read_thread`, `search_mail`, `list_inbox`,
`list_deals`, `list_context`, `list_labels`, `list_projects`.

**Write (15)** — drafts (`draft_reply`, `save_draft`, `compose_draft`, all of
which land in Gmail Drafts and never send), `add_calendar_event` (own calendar,
create-only, attendees rejected), filing (`refile_thread`, `tag_thread`), todos
(`add_todo`, `update_todo`, `set_todo_state`, `snooze_todo`), triage
(`triage_mail`, `mail_action`, `dismiss_thread`), plus `add_context` and
`start_sweep`.

Reads are capped and projected down before they reach the agent — `GET /mail`
is 600 KB, which would bury a conversation. `list_inbox` defaults to 25 threads,
`read_thread` strips HTML bodies and truncates long ones. The caps are constants
at the top of `gc_mcp.py`.

## One honest limitation

This server hard-blocks **its own surface**, and E161 (2026-09-03) closed the
two network holes next to it: the board now refuses any peer that is not
loopback or tailnet, and rejects mutating POSTs carrying a foreign origin,
which killed a live CSRF vector.

What remains unsolvable is a process already running as Bart's uid on the Mini
— including a shell tool in whatever agent is running. It can reach
`POST /send` on loopback directly. No token fixes this, because that process
can read any file Bart can read. What MCP changes is that the agent's
*sanctioned* path cannot do it, and the prose rules in `SKILL.md` govern the
rest.

Note for anything calling these endpoints from a script: requests with no
`Origin` and no `Referer` are allowed through the CSRF check, which is why the
MCP server keeps working unchanged.

## The Claude desktop app: `--profile desktop` (E260, 2026-09-30)

Bart: "Option A first, one account per read, approve all actions." The desktop
app starts this same server over stdio, with `--profile desktop`: directly when
the app runs on the Mini, over SSH across Tailscale from any other Mac. Nothing
is exposed to the internet. The reasoning is in `REMOTE_PLAN.md`; the tools are
in `gc_remote.py`; the rules live in the board (`sweeper/agent_gate.py`), not
here.

The desktop profile has its own key (`data/agent_key_desktop.json`). The board
gives that key every E258 agent rule (account switches, Private, limits,
lockout, audit, wrapped results) and three more:

- **Read, one account per call.** `gc_get_brief`, `gc_list_todos`,
  `gc_read_thread`, `gc_search_mail`, `gc_list_inbox`, `gc_list_context`,
  `gc_list_labels`, `gc_approval_status`. A read without an account, or with
  `all`, is refused; a read returns only the named account's rows. The
  cross-account calendar, day note and board-wide counts are left out.
- **Ask, never act.** `gc_generate_reply_draft`, `gc_save_reply_draft`,
  `gc_save_new_draft`, `gc_add_calendar_event`, `gc_add_todo`,
  `gc_update_todo`, `gc_set_todo_state`, `gc_snooze_todo`,
  `gc_triage_thread`, `gc_mail_action`, `gc_dismiss_thread` each file one
  request (`POST /agent/propose`). It shows as ✋ in the board's top bar and
  under Settings → Agents with a preview and the exact payload. Only Approve
  there, from a paired browser, runs it: once, as an ordinary agent call, so
  every check applies again at that moment. Requests expire after 24 hours; at
  most 20 wait at once.
- **Never** send, trash, forward, attach, RSVP, write Context, start a sweep,
  or touch projects, Home or the dev queue.

Approved requests run as the agent, so `data/agent_key.json` must exist too
(`agent_gate.py --new-key`), and the account must still be switched on.

Every tool call is a line in `logs/mcp_audit.jsonl`; every request the board
sees is a row in `agent_audit` (principal `desktop`, or `agent` with tool
`approved:<tool>` for an approved one), listed in Settings → Agents.

### Setup on the Mini itself

1. Mint the key: `.venv/bin/python sweeper/agent_gate.py --new-desktop-key`
   (and `--new-key` if there is no agent key yet; `--status` shows both).
2. Add to `~/Library/Application Support/Claude/claude_desktop_config.json`
   (merge into an existing `mcpServers`), then quit and reopen the app:

       {
         "mcpServers": {
           "grand-central": {
             "command": "/Users/you/Code/GrandCentral/.venv/bin/python",
             "args": ["/Users/you/Code/GrandCentral/integrations/mcp/gc_mcp.py",
                      "--profile", "desktop"]
           }
         }
       }

3. Check: `.venv/bin/python integrations/mcp/gc_mcp.py --profile desktop --list`
   prints 8 read and 11 request tools.

### Setup from another Mac (SSH over Tailscale)

1. On the laptop, a key used only for this:

       ssh-keygen -t ed25519 -f ~/.ssh/gc_mcp_ed25519 -C gc-mcp -N ''

2. On the Mini, pin that key to the one command. Append one line to
   `~/.ssh/authorized_keys` (paste the laptop's `~/.ssh/gc_mcp_ed25519.pub`
   after the options). Remote Login must be on (System Settings → General →
   Sharing).

       command="cd /Users/you/Code/GrandCentral && exec .venv/bin/python integrations/mcp/gc_mcp.py --profile desktop",no-pty,no-port-forwarding,no-agent-forwarding,no-X11-forwarding ssh-ed25519 AAAA... gc-mcp

3. On the laptop, in `claude_desktop_config.json`:

       {
         "mcpServers": {
           "grand-central": {
             "command": "/usr/bin/ssh",
             "args": ["-T", "-i", "/Users/you/.ssh/gc_mcp_ed25519",
                      "-o", "BatchMode=yes", "bart@gc-host"]
           }
         }
       }

   `gc-host` is the Mini's Tailscale name; use the Mini's short user name if it
   is not `bart`. The forced command decides what runs, so this key cannot open
   a shell. The desktop key never leaves the Mini.
