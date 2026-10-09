"""HTML email signatures, one per account (2026-10-08).

Casey: "Yes, fix the HTML signature" — his Shortwave mail carried the
Brightline logo and real links; Grand Central's signature was plain text
kept in the Context corpus, where a note is capped at 500 collapsed
characters and agents may add notes (so an agent could have planted one).
Signatures now live in their own table that only a paired browser writes
(agent_gate.HUMAN_ONLY_POST), and the HTML is cleaned here on the way in:
an allowlist of tags and attributes, links only to http(s)/mailto/tel,
images only from https or small inline data, no scripts, no event handlers,
no CSS url()s.

    .venv/bin/python sweeper/signatures.py          # each account's signature, as text
"""

from __future__ import annotations

import html as htmlmod
import re
import sys
from html.parser import HTMLParser

import db

MAX_HTML = 60000
TAGS = {"a", "b", "strong", "i", "em", "u", "s", "br", "div", "p", "span", "img", "table", "tbody",
        "thead", "tr", "td", "th", "font", "hr", "small", "sub", "sup", "ul", "ol", "li", "blockquote", "center"}
VOID = {"br", "img", "hr"}
ATTRS = {"href", "src", "alt", "title", "width", "height", "style", "color", "face", "size", "align",
         "valign", "border", "cellpadding", "cellspacing", "bgcolor", "colspan", "rowspan", "target"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "head", "title", "noscript", "svg", "math"}
_DATA_IMG = re.compile(r"^data:image/(png|jpe?g|gif|webp);base64,[A-Za-z0-9+/=\s]{1,400000}$", re.I)
_BAD_CSS = re.compile(r"url\s*\(|expression\s*\(|javascript:|behavior\s*:|@import|-moz-binding", re.I)


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.out, self.skip, self.stack = [], 0, []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in TAGS:
            return
        keep = []
        for k, v in attrs:
            k = (k or "").lower()
            v = v or ""
            if k not in ATTRS or k.startswith("on"):
                continue
            if k == "href" and not re.match(r"^(https?:|mailto:|tel:)", v.strip(), re.I):
                continue
            if k == "src" and not (v.strip().lower().startswith("https://") or _DATA_IMG.match(v.strip())):
                continue
            if k == "style" and _BAD_CSS.search(v):
                continue
            if k == "target":
                v = "_blank"
            keep.append(f'{k}="{htmlmod.escape(v, quote=True)}"')
        if tag == "a":
            keep.append('rel="noopener"')
        self.out.append(f"<{tag}{' ' + ' '.join(keep) if keep else ''}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in DROP_WITH_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in TAGS or tag in VOID or tag not in self.stack:
            return
        while self.stack:                     # close anything left open inside it
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(htmlmod.escape(data, quote=False))

    def handle_entityref(self, name):
        if not self.skip:
            self.out.append(f"&{name};")

    def handle_charref(self, name):
        if not self.skip:
            self.out.append(f"&#{name};")


def clean(html: str) -> str:
    p = _Clean()
    p.feed(str(html or "")[:MAX_HTML])
    p.close()
    out = "".join(p.out) + "".join(f"</{t}>" for t in reversed(p.stack))
    # whitespace between tags shows as blank lines in the composer
    return re.sub(r">\s+<", "><", out).strip()


def to_text(html: str) -> str:
    """The plain-text twin: line breaks where the HTML breaks lines."""
    t = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h\d)>", "\n", html or "")
    t = re.sub(r"(?i)<a [^>]*href=\"(https?:[^\"]+)\"[^>]*>(.*?)</a>",
               lambda m: m.group(2) if m.group(1) in m.group(2) else f"{m.group(2)} ({m.group(1)})", t)
    t = htmlmod.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(line.strip() for line in t.splitlines())).strip()


def from_text(text: str) -> str:
    esc = htmlmod.escape(text or "")
    esc = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', esc)
    return re.sub(r"\r?\n", "<br>", esc)


def get_all(conn) -> dict:
    """{account: {html, text}} — importing a legacy plain signature from the
    Context corpus the first time, but only one Casey wrote (no source)."""
    have = {r["account"]: dict(r) for r in conn.execute("SELECT * FROM signatures")}
    for r in conn.execute("SELECT text, source FROM context_notes WHERE text LIKE '[signature:%'"):
        m = re.match(r"^\[signature:([a-z0-9_-]+)\]\s?([\s\S]*)$", r["text"] or "")
        if not m or m.group(1) in have or (r["source"] or "") not in ("", "manual", "casey"):
            continue
        text = m.group(2).replace("⏎", "\n").strip()
        if text:
            set_sig(conn, m.group(1), from_text(text))
            have[m.group(1)] = {"account": m.group(1), "html": from_text(text)}
    return {a: {"html": r["html"], "text": to_text(r["html"])} for a, r in have.items() if r.get("html")}


def set_sig(conn, account: str, html: str) -> dict:
    account = db.clean_account(account)
    html = clean(html)
    with conn:
        if not to_text(html) and "<img" not in html:
            conn.execute("DELETE FROM signatures WHERE account = ?", (account,))
            return {"account": account, "html": "", "text": ""}
        conn.execute("INSERT INTO signatures (account, html, updated_at) VALUES (?, ?, ?) "
                     "ON CONFLICT(account) DO UPDATE SET html = excluded.html, updated_at = excluded.updated_at",
                     (account, html, db._now_iso()))
    return {"account": account, "html": html, "text": to_text(html)}


def main() -> int:
    conn = db.connect()
    try:
        for a, s in get_all(conn).items():
            print(f"== {a}\n{s['text']}\n")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
