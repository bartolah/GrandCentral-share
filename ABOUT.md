# About Grand Central

> Written 2026-09-28 from the repo and an interview with Bart. Where Bart was
> unsure, it says so. This file is the "why" and the "where next";
> `CLAUDE.md` is the "how", and `ACCOUNT_FIRST.md` records the current build.

## What it is

Grand Central is a personal chief-of-staff board. It runs on gc-host, a Mac
mini at home, and brings Bart's mail accounts, calendar, meetings and todos
into one place he can use from his desk or his phone.

## Why it exists

Bart runs several companies and roles, and each job has its own mail account.
Off-the-shelf tools fell short in two ways:

1. **Too many accounts.** No tool handled several inboxes well, keeping each
   job's work clear while still letting him see across them.
2. **Nothing may fall through.** The real fear is a missed reply or a
   forgotten commitment, and no product tracked what he owes people well
   enough.

Just as important, Bart wants a base he owns and can build on: somewhere to
bring together his inbox, notes and reminders, with AI working across them
as a real assistant.

## Who it is for

Bart, today. Whether it stays personal, extends to his teams, or becomes a
product is **undecided**.

## What success looks like

All four matter:

- **Inbox zero, fast.** Every account triaged in minutes, mostly from the
  phone.
- **No dropped balls.** Every reply owed and every promise made stays visible
  until it is done.
- **One place to start.** The first thing he opens in the morning, and it says
  what needs him today.
- **Less time in email.** AI summaries and drafts cut the hours spent reading
  and writing.

## What it does today

- **Mail, by account.** Each account (one per job) has its own inbox, bundles
  (people, newsletters, receipts, notifications, calendar), todos and triage.
- **Today, per account.** A scored Top 5, reply debt and waiting-on, built
  from an hourly sweep that reads each thread and judges whether a reply is
  owed and how much it matters.
- **Todos that live in Gmail**, as `GC/Todo/<name>` labels synced both ways.
- **Calendar and meetings** from Google Calendar and Granola.
- **Reach.** An installed phone app, and off-site access through Tailscale and
  a Cloudflare tunnel behind an owner-only login.
- **Agents.** An MCP server (used by OpenClaw) and an in-app command bar let AI
  act on the board, but only through the same safe, single-thread actions as
  the buttons.

## Where it is heading

### 1. Working across jobs: through Projects (next)

**Changed 2026-09-29.** Bart: "Todos and Emails should remain in their
accounts … we will handle this through Projects." This replaces the
2026-09-28 plan to loosen account-first for a cross-job Today view and
account-less personal todos.

- **Stays walled off:** every email and every todo belongs to exactly one
  account, and a todo only ever holds its own account's email. No
  account-less todos.
- **The cross-job layer is Projects** (see 3, now moved up): a project,
  filed by Bart, can gather todos, threads and notes from several accounts
  without moving them out of their accounts. A cross-job view of the day
  grows out of projects rather than out of loosening the account rule.
- Recorded in `BUILD_SPEC.md` §6 (2026-09-29).
- **One exception, same day:** after seeing the Home mockup, Bart approved a
  read-only Home across every account (each account in its own section, one
  calendar for the whole day), with AI reading across accounts for the day
  note and for spotting clashes. It shows, it never re-owns, and every action
  still happens inside an account. The same two-column design becomes each
  account's own Home too. Also in `BUILD_SPEC.md` §6.

### 2. Notes

Notes that sit with jobs and todos, and also stand on their own:

- A Notes section in the navigation.
- Notes can attach to a todo, or live alone.
- Clip into a note: an email, copied text, links, media.
- Readable as a plain Markdown folder that Obsidian can open.
- OpenClaw can add notes through the MCP server. (This is a new write for an
  agent, so it needs its own recorded decision.)

**Decided 2026-09-29:** notes live in Grand Central's database, which is the
source of truth. A way to sync or back them up to local files (Markdown, for
Obsidian) comes later. Bart: "For now, let's have Notes live in the
database, and we can set up a way to sync or back them up locally later."
- Apple Reminders and other capture sources were mentioned as part of the
  bigger picture, but have no firm plan yet.

### 3. Projects (moved up, 2026-09-29)

Specified with Bart on 2026-09-29: see `ACCOUNT_FIRST.md` → "Projects — spec" and the design canvas.

A project groups todos, threads and notes, carries its own details (for
example a due date, like a todo), and can span accounts. It is now the way
Bart works across jobs (see 1). Unlike the retired design, a project is filed
by Bart, not guessed by a classifier, and filing something into a project
never moves it out of its account.

### 4. An assistant that earns trust

AI autonomy grows over time. It starts with "suggest, Bart approves", and
each action is widened one at a time as it proves reliable, every widening a
dated decision in `BUILD_SPEC.md`.

## What it will not do

- Send mail on its own. Drafts are the default; there is exactly one send
  path, and a human confirms it.
- Permanently delete anything. Delete means Gmail Trash, recoverable for 30
  days.
- Guess where mail belongs. Automatic project/goal classification was retired
  and is not coming back with projects.
- Widen what it can do without a dated owner decision.

## How it is built and changed

Python sweeper and sync jobs under launchd, one static HTML board, SQLite as
the record. Claude Code sessions do most of the building, in numbered
releases (`E###`), either on gc-host or in the cloud against GitHub. Anything
merged to `main` goes live on the mini within about five minutes.

## Open questions

- Personal tool, team tool, or product?
- Which capture sources come after mail and notes (Apple Reminders, others)?
- Projects: answered 2026-09-29 (account-less containers; Ask reads within
  a project). See `ACCOUNT_FIRST.md`.
