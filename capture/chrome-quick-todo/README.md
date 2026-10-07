# Grand Central — Quick Todo (Chrome extension)

A toolbar popup that adds a todo to the board from any tab: text, due-date
chips (Today / Tomorrow / Next week / picker), an account dropdown (a todo
belongs to exactly one account; pulled live from `/connections`, and the last
one you used is pre-selected), an optional project to file it to (open
projects from `/projects`), notes, and a one-click "add current page" that
drops the tab's title + URL into the todo.

## How it connects

- Tries the saved board URL, then `https://gc.example.com`, then
  `http://localhost:8080` — first one that answers `/version` wins.
- Remote requests ride the browser's Cloudflare Access session cookie. If
  you're logged out, the popup shows an "open it to log in" link instead of
  failing silently.
- Creates todos via the board's existing `POST /task {action:"add"}` path —
  same as adding from the Todos screen (`source: manual`) — then, if you
  picked a project, `POST /projects {action:"add", kind:"todo"}`.
- **Pairing (E264).** The board refuses writes from browser extensions
  (E161). It lets this one through only because the manifest's `key` pins
  its ID (`fidopnnbbmpblalakkbhiibookphdpfg`, wherever the folder is loaded
  from) *and* each request carries the device pairing code. Paste the code
  once under ⚙︎; it stays in this Chrome (`storage.local`), never synced.
  The code is the one the board keeps in its browser as `gc2_dev_code`;
  `sweeper/dev_pairing.py --new` mints a fresh one (every paired device then
  needs it re-entered).

## Install (one time, ~1 minute)

1. Chrome → `chrome://extensions`, toggle **Developer mode** (top right)
2. Remove any older "Grand Central — Quick Todo" (an older copy has a
   different ID and the board will refuse it)
3. **Load unpacked** → pick `~/Code/GrandCentral/capture/chrome-quick-todo`
4. Click the extension → ⚙︎ → paste the pairing code → **Save**
5. Optional: pin it, and check the shortcut at `chrome://extensions/shortcuts`
   (default **⌘⇧Y**)

Updates arrive with auto-pull; click ↻ on the extension in
`chrome://extensions` to pick them up.

## Use

⌘⇧Y (or click the amber ✓) → type → **⌘↵**. Done.
