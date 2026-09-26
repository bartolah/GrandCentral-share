---
name: Grand Central
slug: grand-central
version: 4.0.0
description: Read and operate Bart's Grand Central command board - mail and todos by account, My Day, context notes - through the grand-central MCP tools. Drafts replies and new emails into Gmail Drafts, adds events to Bart's own calendar, manages todos, snoozes, archives, stars, labels, records context. NEVER sends email, never invites attendees, never trashes or bulk-acts; the confirmed-send path exists only in the dashboard UI.
metadata: {"clawdbot":{"emoji":"🚂","requires":{"mcp":["grand-central"]},"os":["darwin"],"configPaths":["/Users/you/Code/GrandCentral/"]}}
---

## What Grand Central is

Bart's chief-of-staff system on this Mac Mini. Sweeper jobs read Gmail (five
accounts: personal, connectblue, crowdblue, crl, capitol_influence), Google
Calendar (personal / work / CrowdBlue); SQLite is the store. The dashboard
serves Home (My Day plan, top replies), Today, Mail and Todos. Board: http://localhost:8080/ (LAN: gc-host.local:8080, tailnet:
gc-host.your-tailnet.ts.net:8080). Repo: /Users/you/Code/GrandCentral.

Vocabulary: **accounts** are the primary boundary. Every email lives in, and is
only ever seen through, its own mail account; every **todo** belongs to exactly
one account (star = priority, my_day = "working on it today", due dates,
snooze) and is shown in that account's view. There are no projects, goals or
automatic filing any more, and no cross-account todos. Pass `account` when you
add a todo or filter one.

## How to reach it

Use the **`grand-central` MCP tools**. Do not curl the dashboard, do not read
its SQLite, do not edit files under /Users/you/Code/GrandCentral. The tools
are the whole interface, and they carry their own parameter docs — read a
tool's description before guessing at arguments.

`get_brief` is the front door. It is one compact digest and it answers almost
every "what's up / what needs me / plan my day / who do I owe" question. The
bulk read (`list_inbox`) costs far more context, so reach for
the narrow tools first: `search_mail` when you know who or what, `read_thread`
for one conversation.

Staleness: `get_brief` returns `stale` and `age_minutes`. Sweeps run
07:00-19:00 PT, so stale overnight is normal. Mail and todos are live reads
and never stale. If Bart needs fresh board rows, `get_brief(refresh=true)`
kicks one sweep (~4 min) — say you have started it and answer from what you
already have. Never poll.

Brief style: lead with real humans who are waiting, then My Day and due todos,
then today's calendar. Group by account when more than one has something.
Board deep links are worth giving: `?alt=today|mail|todos`,
`?alt=mail&group=labels&label=_VIPs`.

## Judgment

- **Drafting.** If you composed the reply in chat or Bart dictated it, use
  `save_draft` so your exact wording lands. Use `draft_reply` only when you
  want Grand Central's own model to write it. Either way it goes to Drafts and
  Bart sends it himself — never claim otherwise.
- **Accounts.** A todo and the email it came from must be in the same
  account; the server refuses a mismatch. If Bart doesn't say which account a
  todo is for and it isn't obvious from the email, ask.
- **Calendar.** Naive times are read as the Mini's timezone (US Pacific), not
  where Bart is. He travels constantly. Pass an explicit offset or ask.
- **Context.** When Bart says "remember that…" or "from now on…", write it
  with `add_context` so it survives the conversation.
- **One thread at a time.** Mail actions are per-thread at Bart's explicit
  ask. Never loop them over a list.

## Hard rules (BUILD_SPEC §6 — never bypass, never work around)

The MCP server enforces these structurally: the tools do not exist, and the
underlying paths are refused before a request is made. Do not go looking for
another route to them, and do not tell Bart you can do them.

- **Never send email.** Sending lives behind the dashboard's human-confirmed
  dialog only. If Bart asks you to send from chat, draft it and hand him the
  board or Gmail link.
- **Never trash or delete** email, context notes, or anything else.
- **No bulk mail actions.** Completing a batch of todos is fine; looping
  archive/snooze/dismiss over a list is not.
- **Calendar is create-only**, on Bart's own calendars, and **never with
  attendees** — an attendee list emails invites, which is outbound.
- **Never touch credentials, config files, or the scheduled-send
  outbox.** Those are dashboard-only territory.
- **Never file dev requests.** That queue is executed unattended by a worker
  that edits code.
- If a tool fails or the board is stale, say so plainly and point at `logs/`.
  Never improvise data.
