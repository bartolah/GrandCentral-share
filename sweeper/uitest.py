"""E201 — the phone invariants, asserted in a real browser.

selftest.py checks the API, the guardrails and the source. It cannot check
layout, and layout is where every recent bug came from. This drives headless
Chrome at 390x844 (browser.py) and asserts the things that kept breaking:

  * nothing scrolls sideways
  * the list runs edge to edge — no side gutter
  * rows are thumb-sized (>= 44px, the platform minimum)
  * no flex container is shorter than the children inside it. This is the
    E201 bug in general form: .list-head computed to 19px while holding 32px
    tabs, because the phone rule set nowrap but left .crumb at flex 1 1 100%
    in a column-flex parent. Nothing in the source looked wrong.
  * a todo title gets most of the row width, not the scraps left by chips
  * the view switcher and the account strip are reachable: either fully on
    screen or in a container that actually scrolls

Run: .venv/bin/python sweeper/uitest.py [--url http://localhost:8080]
"""

from __future__ import annotations

import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from browser import Browser                      # noqa: E402

VIEWS = ["everything", "today", "mail", "todos", "calendar", "sent", "starred"]
TOUCH_MIN = 44

# Every skin, because the status-bar collision was never a layout bug — it was
# a skin writing `.top { padding: 8px 16px }` after the media block and
# resetting the inset. Casey runs "inbox"; the fixes all went into "2049".
SKINS = ["neon", "mono", "daylight", "paper", "console", "rain",
         "mission", "compact", "vector", "grid", "phosphor"]

# An iPhone with a notch and a home indicator, in CSS pixels.
FAKE_TOP, FAKE_BOT = 44, 34

PROBE = r"""
const de = document.documentElement, vw = de.clientWidth;
const box = e => e.getBoundingClientRect();

// the list should reach both edges
const card = document.querySelector('.ib-card, .fd-wrap, .list');
const cr = card ? box(card) : null;

// rows big enough to hit
const rows = [...document.querySelectorAll('.row')].slice(0, 15).map(e => Math.round(box(e).height));

// a container shorter than what it holds: the E201 class of bug
const collapsed = [];
for (const e of document.querySelectorAll('.list-head, .ltabs, .acct-tabs, .row, .l1, .body, .top')) {
  const cs = getComputedStyle(e);
  if (cs.display === 'none' || !e.children.length) continue;
  const h = box(e).height;
  if (h < 2) continue;
  const kids = [...e.children].filter(k => getComputedStyle(k).display !== 'none'
                                        && getComputedStyle(k).position === 'static');
  const tallest = Math.max(0, ...kids.map(k => box(k).height));
  const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
  if (cs.flexWrap === 'nowrap' && tallest > h - pad + 2) {
    collapsed.push({ sel: (e.className || e.tagName).toString().slice(0, 28),
                     h: Math.round(h), tallest: Math.round(tallest) });
  }
}

// a todo title should own its line
const titles = [...document.querySelectorAll('.row .subj.todo-title')]
  .slice(0, 8).map(e => Math.round(box(e).width));

// strips: on screen, or in something that scrolls
const strips = [];
for (const sel of ['.ltabs', '.acct-tabs']) {
  const s = document.querySelector(sel);
  if (!s) continue;
  const kids = [...s.querySelectorAll('button')];
  const past = kids.filter(k => box(k).right > vw + 2).length;
  const cs = getComputedStyle(s);
  strips.push({ sel, past, scrolls: (cs.overflowX === 'auto' || cs.overflowX === 'scroll')
                                    && s.scrollWidth > s.clientWidth + 1 });
}

return {
  sideways: de.scrollWidth > vw + 1,
  gutter: cr ? Math.round(cr.left) : null,
  rows, collapsed, titles, strips, vw,
};
"""


INSET_PROBE = r"""
const de = document.documentElement, box = e => e.getBoundingClientRect();
const top = document.querySelector('.top');
const list = document.querySelector('#list');
const dockbar = document.querySelector('.dockbar');
// The first thing a thumb or an eye meets in the bar. Menus that live inside
// .top are positioned out of flow and parked at 0 while closed — they are not
// what the clock can collide with, so only in-flow, visible controls count.
const first = top ? [...top.querySelectorAll('button, input, .brand')]
  .filter(e => {
    const cs = getComputedStyle(e);
    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
    if (cs.position !== 'static' && cs.position !== 'relative') return false;
    for (let p = e.parentElement; p && p !== top; p = p.parentElement) {
      const pcs = getComputedStyle(p);
      if (pcs.display === 'none' || (pcs.position !== 'static' && pcs.position !== 'relative')) return false;
    }
    const r = box(e);
    return r.width > 0 && r.height > 0;
  })
  .map(e => box(e).top).sort((a, b) => a - b)[0] : null;
const cs = top ? getComputedStyle(top) : null;
const lcs = list ? getComputedStyle(list) : null;
return {
  topPad: cs ? Math.round(parseFloat(cs.paddingTop)) : null,
  firstControlTop: first === undefined || first === null ? null : Math.round(first),
  listPadBottom: lcs ? Math.round(parseFloat(lcs.paddingBottom)) : null,
  dockbarShown: !!dockbar && getComputedStyle(dockbar).display !== 'none',
  bodyH: Math.round(box(document.body).height),
  winH: window.innerHeight,
  appH: getComputedStyle(de).getPropertyValue('--app-h').trim(),
};
"""


def test_insets(b, fails: list[str]) -> int:
    """The insets, forced to an iPhone's, under every skin.

    Headless Chrome reports env(safe-area-inset-*) as 0, which is why this
    collision survived four attempts with a browser in the loop. The insets
    are custom properties now, so the test can hand them real values and
    assert what actually moves.
    """
    checks = 0
    print("\n  status-bar and home-indicator insets, forced to "
          f"{FAKE_TOP}/{FAKE_BOT}px:")
    for skin in SKINS:
        b.js(f"""
          document.documentElement.dataset.skin = {skin!r};
          document.documentElement.style.setProperty('--safe-top', '{FAKE_TOP}px');
          document.documentElement.style.setProperty('--safe-bot', '{FAKE_BOT}px');
          return 1;
        """.replace("'", '"'), wait=0.5)
        r = b.js(INSET_PROBE)
        bad = []
        checks += 2
        if r["topPad"] is None or r["topPad"] < FAKE_TOP:
            bad.append(f"top bar padding {r['topPad']}px — the clock sits on the nav")
        if r["firstControlTop"] is not None and r["firstControlTop"] < FAKE_TOP:
            bad.append(f"first control at y={r['firstControlTop']}, inside the status bar")
        # E219: no bottom bar on a phone — the list runs to the edge, and its
        # last row must still clear the home indicator.
        checks += 2
        if r["listPadBottom"] is None or r["listPadBottom"] < FAKE_BOT:
            bad.append(f"list bottom padding {r['listPadBottom']}px — last row under the home indicator")
        if r["dockbarShown"]:
            bad.append("the bottom tab bar is back on a phone")
        print(f"    {'FAIL' if bad else 'ok  '}  {skin}")
        for m in bad:
            print(f"            {m}")
            fails.append(f"{skin}: {m}")
    # put the board back the way it was found
    b.js("""document.documentElement.style.removeProperty('--safe-top');
            document.documentElement.style.removeProperty('--safe-bot');
            document.documentElement.dataset.skin = 'neon'; return 1;""", wait=0.4)
    return checks


# E208: an overlay has to be opaque. Shortwave's transparent rail and
# Daylight's frosted one are right on the desktop and unreadable as the ☰
# drawer, where the list shows straight through the menu. Every overlay a
# phone opens, under every skin, must paint a solid background.
OPAQUE_PROBE = r"""
const alpha = el => { if (!el) return null;
  const m = (getComputedStyle(el).backgroundColor.match(/[\d.]+/g) || []);
  return m.length > 3 ? +m[3] : (m.length ? 1 : 0); };
const out = {};
document.body.classList.add('nav-open');
out['☰ drawer'] = alpha(document.querySelector('.rail'));
document.body.classList.remove('nav-open');
for (const id of ['me-menu', 'nf-menu', 'skin-menu']) {
  const e = document.getElementById(id); if (!e) continue;
  const was = e.hidden; e.hidden = false; out['#' + id] = alpha(e); e.hidden = was; }
ctxOpen(40, 200, '<div>probe</div>'); out['row menu'] = alpha(document.getElementById('ctx')); ctxClose();
return out;
"""


def test_overlays_opaque(b, fails: list[str]) -> int:
    checks = 0
    print("\n  overlays paint a solid background:")
    for skin in SKINS:
        b.js(f"document.documentElement.dataset.skin = {skin!r}; return 1;".replace("'", '"'), wait=0.3)
        r = b.js(OPAQUE_PROBE)
        bad = [f"{k} background is {round(a * 100)}% opaque — the page reads through it"
               for k, a in r.items() if a is not None and a < 0.99]
        checks += len(r)
        print(f"    {'FAIL' if bad else 'ok  '}  {skin}")
        for m in bad:
            print(f"            {m}")
            fails.append(f"{skin}: {m}")
    b.js("document.documentElement.dataset.skin = 'neon'; return 1;".replace("'", '"'), wait=0.3)
    return checks


def test_height(b, fails: list[str]) -> int:
    """The app is exactly as tall as the window — no strip left at the bottom."""
    r = b.js(INSET_PROBE)
    ok = r["appH"] and abs(r["bodyH"] - r["winH"]) <= 1
    print(f"\n  {'ok  ' if ok else 'FAIL'}  the app fills the window "
          f"(body {r['bodyH']}px, window {r['winH']}px, --app-h {r['appH'] or 'unset'})")
    if not ok:
        fails.append(f"body {r['bodyH']}px vs window {r['winH']}px (--app-h {r['appH'] or 'unset'})")
    return 1


def main() -> int:
    url = "http://localhost:8080"
    if "--url" in sys.argv:
        url = sys.argv[sys.argv.index("--url") + 1]

    fails: list[str] = []
    checks = 0
    print(f"phone layout — 390x844 against {url}\n")
    with Browser(390, 844, mobile=True, scale=2.0) as b:
        b.open(f"{url}/?nopoll=1", settle=5.0)
        for v in VIEWS:
            b.js(f"try{{setScope({v!r})}}catch(e){{}}".replace("'", '"'), wait=1.5)
            r = b.js(PROBE)
            bad = []

            checks += 1
            if r["sideways"]:
                bad.append("scrolls sideways")

            checks += 1
            if r["gutter"] not in (None, 0) and r["gutter"] > 8:
                bad.append(f"side gutter {r['gutter']}px")

            checks += 1
            small = [h for h in r["rows"] if h < TOUCH_MIN]
            if small:
                bad.append(f"{len(small)} rows under {TOUCH_MIN}px (smallest {min(small)})")

            checks += 1
            for c in r["collapsed"]:
                bad.append(f".{c['sel']} is {c['h']}px around {c['tallest']}px children")

            checks += 1
            thin = [w for w in r["titles"] if w < r["vw"] * 0.5]
            if thin:
                bad.append(f"todo title only {min(thin)}px wide")

            checks += 1
            for s in r["strips"]:
                if s["past"] and not s["scrolls"]:
                    bad.append(f"{s['sel']}: {s['past']} buttons off screen, no scroll")

            print(f"  {'FAIL' if bad else 'ok  '}  {v}")
            for msg in bad:
                print(f"          {msg}")
                fails.append(f"{v}: {msg}")

        checks += test_insets(b, fails)
        checks += test_overlays_opaque(b, fails)
        checks += test_height(b, fails)

    print(f"\n{checks - len(fails)} passed, {len(fails)} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
