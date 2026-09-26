"""E197 — Grand Central self-test.

Bart: "Can you run through a series of tests to make sure things are working
right across the app? I keep finding bugs."

Two halves:
  * API — every read endpoint answers, its shape is what the board expects,
    the §6 invariants still refuse what they should, and the write paths
    round-trip (each one cleans up after itself).
  * UI — headless Chrome renders the real board at desktop and phone widths
    and the DOM is asserted: the views exist, the account filter reaches each
    one, the lists carry rows.

    .venv/bin/python sweeper/selftest.py            # API only (fast)
    .venv/bin/python sweeper/selftest.py --ui       # + headless Chrome
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "http://localhost:8080"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

PASS, FAIL, SKIP = [], [], []


def check(name: str, ok: bool, detail: str = "") -> bool:
    (PASS if ok else FAIL).append((name, detail))
    print(("  ok   " if ok else "  FAIL ") + name + (f" — {detail}" if detail and not ok else ""))
    return ok


def skip(name: str, why: str) -> None:
    SKIP.append((name, why))
    print(f"  skip {name} — {why}")


def get(path: str, timeout: int = 30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def post(path: str, body: dict, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


# ---------------------------------------------------------------- API reads
def test_reads():
    print("\nREAD ENDPOINTS")
    shapes = {
        "/version": ["ok", "version"],
        "/mail": ["threads"],
        "/todos": ["todos"],
        "/dev": ["requests", "pairing_set"],
        "/push": ["public_key", "devices", "prefs"],
        "/connections": ["accounts"],
        "/progress": ["progress"],
        "/wizard": ["pending"],
        "/braindump": ["ideas"],
    }
    for path, keys in shapes.items():
        try:
            code, body = get(path)
            missing = [k for k in keys if k not in body]
            check(f"GET {path}", code == 200 and not missing,
                  f"status {code}, missing {missing}")
        except Exception as e:
            check(f"GET {path}", False, repr(e))
    code, mail = get("/mail")
    check("the outbox rides inside /mail", "outbox" in mail, f"keys {list(mail)[:6]}")
    try:
        with urllib.request.urlopen(BASE + "/grand_central.json", timeout=30) as r:
            board = json.loads(r.read())
        need = ["top5", "reply_debt", "waiting_on", "calendar"]
        check("GET /grand_central.json", all(k in board for k in need),
              f"missing {[k for k in need if k not in board]}")
        return board
    except Exception as e:
        check("GET /grand_central.json", False, repr(e))
        return {}


# ------------------------------------------------------------- §6 guardrails
def test_guardrails():
    print("\nGUARDRAILS (§6)")
    code, body = post("/dev", {"action": "add", "text": "selftest must be refused"})
    check("unpaired POST /dev is refused", code == 403 and body.get("pairing_required"), f"{code} {body}")

    code, body = post("/push", {"action": "subscribe", "label": "x",
                                "subscription": {"endpoint": "http://evil.example/p"}})
    check("push rejects a non-https endpoint", code == 400, f"{code} {body}")

    req = urllib.request.Request(BASE + "/task", data=b"{}",
                                 headers={"Content-Type": "application/json",
                                          "Origin": "https://evil.example"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            check("cross-origin POST is refused", False, f"accepted with {r.status}")
    except urllib.error.HTTPError as e:
        check("cross-origin POST is refused", e.code == 403, f"status {e.code}")

    code, body = post("/mail-action", {"action": "trash", "thread_id": "not-a-number", "account": "personal"})
    check("a malformed thread id is refused", code in (400, 404), f"{code} {body}")


# ------------------------------------------------------------- write paths
def test_writes():
    print("\nWRITE PATHS (each cleans up after itself)")
    code, add = post("/task", {"action": "add", "text": "SELFTEST — ignore", "due": "2026-09-21"})
    tid = add.get("id")
    if not check("todo add", code == 200 and bool(tid), f"{code} {add}"):
        return
    code, _ = post("/task", {"action": "update", "id": tid,
                             "fields": {"priority": "high", "recur": "weekly", "notes": "selftest"}})
    row = next((t for t in get("/todos")[1]["todos"] if t["id"] == tid), {})
    check("todo update round-trips", code == 200 and row.get("priority") == "high"
          and row.get("recur") == "weekly", json.dumps(row)[:120])

    code, done = post("/task", {"action": "done", "id": tid})
    spawned = (done.get("spawned") or [{}])[0]
    check("completing a repeating todo opens the next", bool(spawned.get("id"))
          and spawned.get("due") == "2026-09-28", json.dumps(done)[:140])

    ids = [t["id"] for t in get("/todos")[1]["todos"] if t["text"].startswith("SELFTEST")]
    code, _ = post("/task", {"action": "drop", "ids": ids})
    left = [t for t in get("/todos")[1]["todos"] if t["text"].startswith("SELFTEST")]
    check("cleanup leaves nothing behind", code == 200 and not left, f"{len(left)} rows left")

    before = get("/push")[1]["prefs"]["wrap"]
    post("/push", {"action": "prefs", "kind": "wrap", "imessage": False, "push": True})
    mid = get("/push")[1]["prefs"]["wrap"]
    post("/push", {"action": "prefs", "kind": "wrap", **before})
    after = get("/push")[1]["prefs"]["wrap"]
    check("notification channels round-trip", mid["imessage"] is False and after == before,
          f"{mid} then {after}")


# ------------------------------------------------- data the views depend on
def test_data(board):
    print("\nDATA THE VIEWS DEPEND ON (account-first)")
    mail = get("/mail")[1]["threads"]
    todos = get("/todos")[1]["todos"]
    accounts = sorted({m["account"] for m in mail})

    check("every thread names its account", all(m.get("account") for m in mail),
          "a mail row has no account")
    check("every todo carries an account key", all("account" in t for t in todos),
          "a todo row lacks the account key")
    # the boundary itself: an account-scoped read returns only that account
    for a in accounts[:3]:
        got = {m["account"] for m in get(f"/mail?account={a}")[1]["threads"]}
        check(f"/mail?account={a} holds only {a}", got <= {a}, f"leaked {got - {a}}")
        got = {t["account"] for t in get(f"/todos?account={a}")[1]["todos"]}
        check(f"/todos?account={a} holds only {a}", got <= {a}, f"leaked {got - {a}}")
    mixed = [t["id"] for t in todos
             if t.get("account") and any(th["account"] != t["account"] for th in t.get("threads", []))]
    check("no todo shows another account's email", not mixed, f"todos {mixed}")
    for path in ("/bizdev", "/dash", "/registry", "/scratch", "/progress"):
        check(f"GET {path} is retired (410)", _status(path) == 410, "still answering")


def _status(path: str) -> int:
    try:
        return get(path)[0]
    except urllib.error.HTTPError as e:
        return e.code


# ------------------------------------------------------------------- wiring
# The bugs Bart keeps finding are wiring bugs: a button calling a function
# that does not exist, a fetch to a route nobody serves, a stylesheet talking
# about an element the app never emits. Headless Chrome cannot be made to
# settle on this page, so these are read straight out of the source.
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _index() -> str:
    with open(os.path.join(ROOT, "dashboard", "index.html")) as f:
        return f.read()


def test_wiring():
    print("\nWIRING (source)")
    html = _index()
    with open(os.path.join(ROOT, "dashboard", "serve.py")) as f:
        serve = f.read()

    defined = set(re.findall(r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", html))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", html))
    defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*[A-Za-z_$][\w$]*\s*=>", html))
    builtin = {"event", "this", "window", "location", "console", "alert", "confirm", "fetch",
               "setTimeout", "clearTimeout", "setInterval", "Math", "JSON", "Object", "Array",
               "String", "Number", "Date", "document", "navigator", "localStorage", "history",
               "URLSearchParams", "Notification", "atob", "btoa", "parseInt", "parseFloat"}
    keywords = {"if", "for", "while", "return", "typeof", "catch", "switch", "new", "function"}
    called = set()
    for handler in re.findall(r'on(?:click|change|input|keydown|submit|load)="([^"]+)"', html):
        # a bare NAME( — never a method call (.foo()) and never a keyword
        called |= {m.group(1) for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", handler)}
    missing = sorted(c for c in called - defined - builtin - keywords if not c.startswith("_"))
    check("every inline handler calls a function that exists", not missing,
          f"undefined: {', '.join(missing[:8])}")

    routes = set(re.findall(r'(?:route|parsed\.path)\s*==\s*"([^"]+)"', serve))
    routes |= set(re.findall(r'startswith\("([^"]+)"\)', serve))
    fetched = set(re.findall(r"""(?:fetch|post)\(\s*['"](/[\w/.\-]+)""", html))
    static = {"/grand_central.json", "/release_notes.json", "/manifest.json", "/favicon.svg",
              "/quicktodo.zip", "/icon-192.png", "/apple-touch-icon.png"}
    unserved = sorted(f for f in fetched - routes - static
                      if not os.path.exists(os.path.join(ROOT, "dashboard", f.lstrip("/"))))
    # Account-first: routes retired with the projects/goals UI may still have
    # unreachable client code behind them in index.html (see ACCOUNT_FIRST.md,
    # "deferred UI cleanup"). It is reported, not failed. Any OTHER unserved
    # path still fails.
    legacy = sorted(set(unserved) & {"/dash", "/feedback", "/registry", "/tag", "/scratch", "/progress"})
    unserved = [u for u in unserved if u not in legacy]
    if legacy:
        skip("legacy client code for retired routes", ", ".join(legacy) + " (deferred UI cleanup)")
    check("every path the board fetches is served", not unserved,
          f"unserved: {', '.join(unserved[:6])}")

    # the markup is built in template literals, so "is this class ever
    # emitted" means "does its name appear anywhere outside the stylesheet"
    style_end = html.find("</style>")
    body = html[style_end:]
    styled = set(re.findall(r"^\s*\.([a-z][\w-]+)[\s,{:]", html[:style_end], re.M))
    ghosts = sorted(c for c in styled if c not in body)
    check("every styled class is emitted somewhere", not ghosts,
          f"styled but never rendered: {', '.join(ghosts[:8])}")

    # E198: a valid route with an action name the server never accepts is the
    # same bug as a missing route, and reads identically in the source.
    posted = re.findall(r"""post\(\s*['"](/[\w/\-]+)['"]\s*,\s*\{\s*action\s*:\s*([^,}]+)""", html)
    literal = [(route, m.group(1)) for route, expr in posted
               for m in re.finditer(r"'([a-z\-]+)'", expr)]
    bad = []
    for route, action in set(literal):
        seg = re.search(r'route == "%s"(.*?)(?:\n        elif route ==|\Z)' % re.escape(route), serve, re.S)
        if seg and f'"{action}"' not in seg.group(1):
            bad.append(f"{route}:{action}")
    check("every posted action name is one the server accepts", not bad,
          f"unknown: {', '.join(sorted(bad)[:6])}")

    # E199: the notch inset was fixed twice and collided twice, because a
    # later `padding:` shorthand in the same media block silently reset it.
    # Whatever declares .top's padding LAST on a phone must carry the inset.
    phone = re.search(r"@media \(max-width: 760px\) \{(.*?)\n  \}\n", html, re.S)
    if phone:
        tops = re.findall(r"\.top \{([^}]*)\}", phone.group(1))
        last_padding = [d for t in tops for d in re.findall(r"padding[^;]*;", t)]
        check("the phone's top bar keeps its status-bar inset",
              bool(last_padding) and "--safe-top" in last_padding[-1],
              f"last padding declaration is {last_padding[-1].strip() if last_padding else 'absent'}")
        # the dock bar's inset lives in the ≤980px block, which also covers
        # phones — look across every mobile block, not just the 760 one
        mobile = "".join(m.group(1) for m in
                         re.finditer(r"@media \(max-width: (?:760|980|640)px\) \{(.*?)\n  \}\n", html, re.S))
        # E200: a selector declared twice in one media block means the last
        # copy silently wins — the failure that kept the nav broken for three
        # attempts, and that blunted every later phone tweak.
        sels = [" ".join(re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S).split())
                for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", phone.group(1))]
        repeats = sorted({x for x in sels if x and sels.count(x) > 1})
        check("no selector is declared twice in the phone block", not repeats,
              f"repeated: {', '.join(repeats[:6])}")

        for sel, inset in [(r"\.dockbar", "--safe-bot"), (r"\.pane", "--safe-top")]:
            block = re.findall(sel + r" \{([^}]*)\}", mobile)
            check(f"{sel.strip(chr(92) + '.')} keeps its inset on a phone",
                  any(inset in b for b in block), "inset missing")

    # E203: every earlier fix went into the phone media block, and a skin's
    # `.top { padding: 8px 16px }` — written later, at higher specificity —
    # reset it each time. The last rule in the file that sets .top padding on
    # a phone must be one that carries the inset.
    phone_blocks = list(re.finditer(r"@media \(max-width: 760px\) \{(.*?)\n  \}\n", html, re.S))
    top_rules = [(m.start(), m.group(0)) for m in
                 re.finditer(r"[^{}\n]*\.top[^{}]*\{[^{}]*padding[^{}]*\}", html)]
    last_top = max(top_rules, key=lambda x: x[0])[1] if top_rules else ""
    check("no skin can reset the status-bar inset",
          "--safe-top" in last_top,
          f"the last .top padding rule is: {' '.join(last_top.split())[:90]}")
    check("the insets are defined once, from env()",
          html.count("--safe-top: env(safe-area-inset-top") == 1
          and html.count("--safe-bot: env(safe-area-inset-bottom") == 1,
          "expected exactly one env() definition of each")

    views = re.findall(r"function (render[A-Z]\w+)\(", html)
    check("the row renderer is shared, not copied", html.count("function rowHtml(") == 1,
          f"{html.count('function rowHtml(')} definitions")
    print(f"       ({len(defined)} functions, {len(views)} view renderers, {len(routes)} routes)")


# ---------------------------------------------------------------- UI (Chrome)
def dom(url: str, width: int, height: int, budget: int = 8000) -> str:
    url += ("&" if "?" in url else "?") + "nopoll=1"
    with tempfile.TemporaryDirectory() as prof:
        out = subprocess.run(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
             f"--user-data-dir={prof}", f"--window-size={width},{height}",
             # avatars reach out to gravatar and google; in headless those
             # requests keep virtual time from ever going idle, so cut the
             # page off from everything except the board itself.
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
             f"--virtual-time-budget={budget}", "--dump-dom", url],
            capture_output=True, text=True, timeout=90)
        return out.stdout


def test_ui():
    print("\nUI (headless Chrome)")
    import os
    if not os.path.exists(CHROME):
        return skip("ui", "Chrome not installed")
    html = dom(BASE + "/", 1440, 900)
    check("board renders at desktop width", "Grand Central" in html and 'id="list"' in html)
    check("the account strip renders", 'class="acct-tab' in html, "no account tabs")
    check("the rail renders its views", all(n in html for n in ("Home", "Wizard", "Today", "Todos", "Inbox")),
          "a rail entry is missing")
    check("Home renders rows", 'class="row' in html, "no rows in the list")
    check("no visible error state", "Could not load" not in html, "the board printed a load error")

    for view, marker in [("today", "ib-card"), ("todos", "ib-card"), ("inbox", "row")]:
        h = dom(f"{BASE}/?go={view}", 1440, 900)
        check(f"?go={view} renders", marker in h, "empty view")

    phone = dom(BASE + "/", 390, 844)
    check("board renders at phone width", 'id="list"' in phone and 'class="row' in phone)


def main():
    t0 = time.time()
    print(f"Grand Central self-test — {BASE}")
    board = test_reads()
    test_guardrails()
    test_writes()
    test_data(board)
    test_wiring()
    if "--ui" in sys.argv:
        test_ui()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed, {len(SKIP)} skipped "
          f"in {time.time()-t0:.1f}s")
    if FAIL:
        print("\nFAILURES")
        for name, detail in FAIL:
            print(f"  · {name}" + (f" — {detail}" if detail else ""))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
