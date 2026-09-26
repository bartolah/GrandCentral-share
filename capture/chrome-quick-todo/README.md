# Grand Central — Quick Todo (Chrome extension)

A toolbar popup that adds a todo to the board from any tab: text, due-date
chips (Today / Tomorrow / Next week / picker), an account dropdown (a todo
belongs to exactly one account; pulled live from `/connections`, and the last
one you used is pre-selected), notes, and a one-click
"add current page" that drops the tab's title + URL into the todo.

## How it connects

- Tries the saved board URL, then `https://gc.example.com`, then
  `http://localhost:8080` — first one that answers `/version` wins.
- Remote requests ride the browser's Cloudflare Access session cookie. If
  you're logged out, the popup shows an "open it to log in" link instead of
  failing silently.
- Creates todos via the board's existing `POST /task {action:"add"}` path —
  same as adding from the Todos screen (`source: manual`).

## Install (one time, ~30 seconds)

1. Chrome → `chrome://extensions`
2. Toggle **Developer mode** (top right)
3. **Load unpacked** → pick this folder (`capture/chrome-quick-todo`)
4. Optional: pin it (puzzle-piece icon → pin), and check the keyboard
   shortcut at `chrome://extensions/shortcuts` — default is **⌘⇧Y**.

## Use

⌘⇧Y (or click the amber ✓) → type → **⌘↵**. Done.
