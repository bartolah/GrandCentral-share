"""E200 — a real browser in the loop, over the DevTools Protocol.

The gap ToDos.md recorded: selftest.py could check the API, the guardrails and
the source, but nothing drove a browser — so every layout bug reached Casey
before it reached a test, and one of them survived three fixes because it only
appears at phone width.

Headless Chrome's convenience flags were dead ends here: --dump-dom prints
nothing in this build, and --screenshot never returns on a page with live
timers. CDP has neither problem: launch Chrome with a debugging port, drive it
explicitly, and close it.

    from browser import Browser
    with Browser(390, 844) as b:          # iPhone-ish
        b.open("http://localhost:8080/?nopoll=1")
        b.js("setScope('home',null)")
        print(b.js("document.querySelectorAll('.row').length"))
        b.shot("/tmp/home.png")
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

import websocket

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Browser:
    def __init__(self, width: int = 1440, height: int = 900, mobile: bool = None, scale: float = 2.0):
        self.w, self.h = width, height
        self.mobile = (width <= 500) if mobile is None else mobile
        self.scale = scale
        self.profile = tempfile.mkdtemp(prefix="gc-cdp-")
        self.port = _free_port()
        self.proc = None
        self.ws = None
        self._id = 0

    # ---------------------------------------------------------------- life
    def __enter__(self):
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
             f"--user-data-dir={self.profile}", f"--remote-debugging-port={self.port}",
             f"--window-size={self.w},{self.h}", "--remote-allow-origins=*",
             # avatars reach gravatar/google; without this every page waits on them
             "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        target = None
        for _ in range(100):                      # Chrome needs a moment to listen
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=1) as r:
                    pages = [t for t in json.load(r) if t.get("type") == "page"]
                if pages:
                    target = pages[0]
                    break
            except Exception:
                time.sleep(0.1)
        if not target:
            raise RuntimeError("Chrome never opened a debugging target")
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
        self.send("Page.enable")
        self.send("Runtime.enable")
        self.send("Emulation.setDeviceMetricsOverride", width=self.w, height=self.h,
                  deviceScaleFactor=self.scale, mobile=self.mobile)
        if self.mobile:
            self.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
        return self

    def __exit__(self, *exc):
        try:
            if self.ws:
                self.ws.close()
        finally:
            if self.proc:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            shutil.rmtree(self.profile, ignore_errors=True)

    # ---------------------------------------------------------------- calls
    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    def open(self, url: str, settle: float = 3.5):
        """Navigate and give the board time to fetch and paint."""
        self.send("Page.navigate", url=url)
        time.sleep(settle)
        return self

    def js(self, expr: str, wait: float = 0.0):
        """Evaluate in the page and return the value (awaits promises)."""
        statements = ";" in expr.strip().rstrip(";") or expr.strip().startswith(
            ("const", "let", "var", "if", "for", "return", "document.querySelectorAll("))
        r = self.send("Runtime.evaluate",
                      expression=f"(()=>{{ {expr} }})()" if statements else expr,
                      returnByValue=True, awaitPromise=True, userGesture=True)
        if wait:
            time.sleep(wait)
        res = r.get("result", {})
        if res.get("subtype") == "error":
            raise RuntimeError(res.get("description", "")[:300])
        return res.get("value")

    def shot(self, path: str, full: bool = False):
        r = self.send("Page.captureScreenshot", format="png", captureBeyondViewport=full)
        import base64
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
        return path

    def tap(self, selector: str):
        box = self.js(f"""const e=document.querySelector({json.dumps(selector)});
            if(!e) return null; const r=e.getBoundingClientRect();
            return {{x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)}};""")
        if not box:
            raise RuntimeError(f"no element for {selector}")
        for t in ("touchStart", "touchEnd"):
            self.send("Input.dispatchTouchEvent", type=t,
                      touchPoints=[{"x": box["x"], "y": box["y"]}] if t == "touchStart" else [])
        time.sleep(0.4)
        return self
