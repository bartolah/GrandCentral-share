#!/usr/bin/env python3
"""Set up Grand Central from the command line (E312).

    .venv/bin/python tools/setup.py           walk through setup; re-run any time
    .venv/bin/python tools/setup.py --check   report what is set and what is missing; changes nothing

Casey, 2026-11-14: "Command line setup is great." It runs on the machine that
hosts the board, so typing a password here never involves a browser, and
the first pairing code can be made here too (the board's own Settings →
Connections only saves from a paired browser).

Every write to config/.env goes through sweeper/connections.py, the one
writer of credentials (atomic, mode 600, a dated backup each time). Every
credential is tested before it is saved, typed with no echo, and never
printed. Re-running skips what is already set and working; nothing is
removed. launchd files are written only where none exists yet.
"""

from __future__ import annotations

import argparse
import getpass
import os
import platform
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "sweeper"))

REQUIRED_PY = (3, 11)
PACKAGES = ("requests", "anthropic", "icalendar", "recurring_ical_events", "yaml")
# The three jobs a board needs. The others in config/ (tunnel, autopull, the
# dev worker) belong to one particular setup and are left to the reader.
JOBS = ("dashboard", "mailsync", "sweep")


class Console:
    """Questions and output. Tests swap in a scripted one."""

    def say(self, text: str = "") -> None:
        print(text)

    def ask(self, prompt: str, default: str = "") -> str:
        hint = f" [{default}]" if default else ""
        try:
            got = input(f"{prompt}{hint}: ").strip()
        except EOFError:
            got = ""
        return got or default

    def secret(self, prompt: str) -> str:
        try:
            return getpass.getpass(f"{prompt} (hidden): ").strip()
        except EOFError:
            return ""

    def yes(self, prompt: str, default: bool = False) -> bool:
        got = self.ask(f"{prompt} {'[Y/n]' if default else '[y/N]'}").lower()
        return default if not got else got.startswith("y")


def _env() -> dict:
    import connections
    from common import load_env
    return load_env(connections.ENV_PATH)


def _accounts(env: dict) -> dict:
    from sources import gmail_reader
    return gmail_reader.scan_accounts(env)


def _set(env: dict, key: str) -> bool:
    import connections
    val = env.get(key) or ""
    return bool(val) and not connections._is_placeholder(val)


# ---------------------------------------------------------------- checks
def check(ui: Console) -> int:
    """The report. Returns 0 when the board can run (one mail account)."""
    env = _env()
    accounts = _accounts(env)
    rows = []

    def row(ok, name, detail, need="optional"):
        rows.append((ok, name, detail, need))

    py = sys.version_info[:2]
    row(py >= REQUIRED_PY, "Python", f"{py[0]}.{py[1]} (need {REQUIRED_PY[0]}.{REQUIRED_PY[1]}+)", "required")
    missing = [p for p in PACKAGES if not _importable(p)]
    row(not missing, "Packages", "installed" if not missing else
        f"missing {', '.join(missing)}: .venv/bin/pip install -r requirements.txt", "required")
    row(bool(accounts), "Mail accounts", ", ".join(sorted(accounts)) or "none yet", "required")
    row(_set(env, "ANTHROPIC_API_KEY"), "Claude key", "set" if _set(env, "ANTHROPIC_API_KEY")
        else "not set: summaries, drafts and Ask stay off", "recommended")
    from sources import calendar_reader
    feeds = calendar_reader.calendar_feeds(env)
    row(bool(feeds), "Calendars", f"{len(feeds)} feed(s)" if feeds else "none", "recommended")
    row(bool(env.get("SENDER_NAME") or any(env.get(f"GMAIL_{a.upper()}_NAME") for a in accounts)),
        "Sender name", env.get("SENDER_NAME") or "not set (mail goes out under a default name)",
        "recommended")
    import dev_pairing
    row(dev_pairing.is_configured(), "Browser pairing", "a code exists" if dev_pairing.is_configured()
        else "none: sending and Settings → Connections stay locked", "recommended")
    row(bool(_which("gog")), "gog (calendar writes)", _which("gog") or "not installed", "optional")
    row(_set(env, "GRANOLA_TOKEN"), "Granola", "set" if _set(env, "GRANOLA_TOKEN") else "not set")
    row(_set(env, "JEV_API_KEY"), "Jev", "set" if _set(env, "JEV_API_KEY") else "not set")
    if platform.system() == "Darwin":
        have = [j for j in JOBS if installed_job(j)]
        row(len(have) == len(JOBS), "Background jobs", ", ".join(have) or "none installed", "recommended")

    ui.say("")
    for ok, name, detail, need in rows:
        mark = "ok " if ok else ("!! " if need == "required" else "-- ")
        ui.say(f"  {mark} {name:<22} {detail}")
    ui.say("")
    blocking = [n for ok, n, _, need in rows if not ok and need == "required"]
    if blocking:
        ui.say("Not ready: " + ", ".join(blocking) + ". Run tools/setup.py to fix.")
        return 1
    ui.say("Ready. Start the board with: .venv/bin/python dashboard/serve.py")
    return 0


def _importable(mod: str) -> bool:
    try:
        __import__(mod)
        return True
    except Exception:
        return False


def _which(tool: str) -> str | None:
    return shutil.which(tool) or next((p for p in (f"/opt/homebrew/bin/{tool}", f"/usr/local/bin/{tool}")
                                       if os.path.exists(p)), None)


# ---------------------------------------------------------------- steps
def step_accounts(ui: Console) -> None:
    import connections
    ui.say("\n== Gmail accounts ==")
    ui.say("Each account needs an app password: turn on 2-Step Verification, then create one at")
    ui.say("https://myaccount.google.com/apppasswords (16 characters).")
    while True:
        accounts = _accounts(_env())
        ui.say("Accounts so far: " + (", ".join(sorted(accounts)) or "none"))
        if not ui.yes("Add a Gmail account?", default=not accounts):
            return
        email = ui.ask("Email address")
        if "@" not in email:
            ui.say("That isn't an email address.")
            continue
        name = ui.ask("Short name for this account (letters, numbers, _)",
                      connections._slugify(email.split("@")[0]).lower())
        password = ui.secret("App password")
        ok, msg = connections.test_imap(email, password)
        ui.say(("  ✓ " if ok else "  ✗ ") + msg)
        if not ok:
            continue
        try:
            connections.add_gmail(email, password, name)
            ui.say(f"  Saved account '{connections._slugify(name).lower()}'.")
        except ValueError as e:
            ui.say(f"  Not saved: {e}")


def step_sender(ui: Console) -> None:
    import connections
    env = _env()
    if env.get("SENDER_NAME"):
        return
    ui.say("\n== Your name ==")
    name = ui.ask("The name your email is sent under (blank to skip)")
    if name:
        connections.set_value("SENDER_NAME", name)
        ui.say("  Saved.")


def _single(ui: Console, name: str, title: str, why: str, tester) -> None:
    import connections
    meta = connections.SINGLE[name]
    env = _env()
    ui.say(f"\n== {title} ==")
    if _set(env, meta["key"]):
        ui.say("  Already set.")
        return
    ui.say(why)
    for _ in range(3):
        val = ui.secret(f"{title} (blank to skip)")
        if not val:
            return
        ok, msg = tester(val)
        ui.say(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            connections.set_single(name, val)
            ui.say("  Saved.")
            return


def step_claude(ui: Console) -> None:
    import connections
    _single(ui, "anthropic", "Claude API key",
            "Summaries, reply drafts, Ask and the day note. Get one at https://console.anthropic.com",
            connections.test_anthropic)


def step_calendars(ui: Console) -> None:
    import connections
    from sources import calendar_reader
    ui.say("\n== Calendars ==")
    ui.say("For each account, paste its calendar's \"Secret address in iCal format\"")
    ui.say("(Google Calendar → Settings → your calendar → Integrate calendar). Treat it like a password.")
    env = _env()
    for acct in sorted(_accounts(env)):
        key = f"GCAL_ICS_{acct.upper()}"
        if env.get(key):
            ui.say(f"  {acct}: already set.")
            continue
        url = ui.secret(f"iCal address for {acct} (blank to skip)")
        if not url:
            continue
        ok, msg = _test_ics(url, calendar_reader)
        ui.say(("  ✓ " if ok else "  ✗ ") + msg)
        if ok:
            connections.set_value(key, url)
            ui.say("  Saved.")


def _test_ics(url: str, calendar_reader) -> tuple:
    if not url.startswith("https://"):
        return False, "that should start with https://"
    try:
        import requests
        import icalendar
        r = requests.get(url, timeout=calendar_reader.FETCH_TIMEOUT)
        r.raise_for_status()
        cal = icalendar.Calendar.from_ical(r.content)
        n = sum(1 for c in cal.walk() if c.name == "VEVENT")
        return True, f"read the calendar ({n} events)"
    except Exception as e:
        return False, f"could not read it ({type(e).__name__})"


def step_gog(ui: Console) -> None:
    ui.say("\n== Creating events and RSVPs (optional) ==")
    gog = _which("gog")
    if not gog:
        ui.say("Install gog to let Grand Central create events and RSVP on your own calendars:")
        ui.say("  brew install steipete/tap/gog   then re-run this setup.")
        return
    env = _env()
    for acct, _ in sorted(_accounts(env).items()):
        email = env.get(f"GMAIL_{acct.upper()}_USER")
        if email and ui.yes(f"Grant gog calendar access for {email}? (opens a browser sign-in)"):
            subprocess.run([gog, "auth", "add", email, "--services", "calendar"])


def step_optional(ui: Console) -> None:
    import connections
    _single(ui, "granola", "Granola token (optional)",
            "Meeting notes become todo suggestions. An API key from Granola's settings (grn_…).",
            connections.test_granola)
    _single(ui, "jev", "Jev API key (optional)",
            "Classifies person-to-person mail. Off per account until you switch it on in Settings.",
            connections.test_jev)


def step_pairing(ui: Console) -> None:
    import dev_pairing
    ui.say("\n== Pair your browser ==")
    if dev_pairing.is_configured() and not ui.yes(
            "A pairing code exists. Make a new one? (browsers paired with the old one must pair again)"):
        return
    if not dev_pairing.is_configured() and not ui.yes("Make a pairing code now?", default=True):
        return
    code = dev_pairing.new_code()
    ui.say(f"  Your pairing code: {code}")
    ui.say("  The board asks for it the first time you send, delete or change a setting.")
    ui.say("  It is shown once; run this again for a new one.")


# ---------------------------------------------------------------- jobs
def _label_prefix() -> str:
    """'com.<you>.grandcentral.' from this machine's user name."""
    user = re.sub(r"[^a-z0-9]", "", getpass.getuser().lower()) or "user"
    return f"com.{user}.grandcentral."


def _agent_path(label: str) -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def installed_job(job: str, agents_dir: str | None = None) -> str | None:
    """Any Grand Central job of this kind already installed, whatever its
    label prefix: a board set up by hand (or under another name) must never
    get a second dashboard or a second mail sync beside it."""
    agents_dir = agents_dir or os.path.expanduser("~/Library/LaunchAgents")
    try:
        names = sorted(os.listdir(agents_dir))
    except OSError:
        return None
    hit = next((n for n in names if n.endswith(f".grandcentral.{job}.plist")), None)
    return os.path.join(agents_dir, hit) if hit else None


def render_plist(text: str, root: str, prefix: str) -> str:
    """Point a config/ plist at this checkout and this user's labels."""
    m = re.search(r"<key>WorkingDirectory</key>\s*<string>([^<]+)</string>", text)
    if m:
        text = text.replace(m.group(1).rstrip("/"), root.rstrip("/"))
    return re.sub(r"com\.[A-Za-z0-9_-]+\.grandcentral\.", prefix, text)


def step_jobs(ui: Console) -> None:
    ui.say("\n== Keep it running ==")
    py = os.path.join(ROOT, ".venv", "bin", "python")
    if platform.system() != "Darwin":
        ui.say("Not macOS: run the board as a service and add these to your crontab (crontab -e):")
        ui.say(f"  */3 * * * *  cd {ROOT} && {py} sweeper/mail_sync.py")
        ui.say(f"  0 7-19 * * *  cd {ROOT} && {py} sweeper/sweep.py")
        ui.say(f"  and keep running:  cd {ROOT} && {py} dashboard/serve.py")
        return
    prefix = _label_prefix()
    for job in JOBS:
        src = next((os.path.join(ROOT, "config", n) for n in sorted(os.listdir(os.path.join(ROOT, "config")))
                    if n.endswith(f".grandcentral.{job}.plist")), None)
        dst = _agent_path(prefix + job)
        if src is None:
            ui.say(f"  {job}: no template in config/.")
            continue
        existing = installed_job(job)
        if existing:
            ui.say(f"  {job}: already installed ({existing}); left alone.")
            continue
        if not ui.yes(f"Install and start the {job} job?", default=True):
            continue
        with open(src, encoding="utf-8") as f:
            text = render_plist(f.read(), ROOT, prefix)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text)
        r = subprocess.run(["launchctl", "load", dst], capture_output=True, text=True)
        ui.say(f"  {job}: installed" + ("" if r.returncode == 0 else f" (launchctl: {r.stderr.strip()})"))


# ---------------------------------------------------------------- main
def run(ui: Console) -> int:
    ui.say("Grand Central setup. Re-run any time; what is set and working is skipped.")
    ui.say(f"Settings are saved in {os.path.join(ROOT, 'config', '.env')} (only you can read it).")
    if sys.version_info[:2] < REQUIRED_PY:
        ui.say("Python 3.11 or newer is needed. Create the environment with:")
        ui.say("  python3.11 -m venv .venv && .venv/bin/pip install -r requirements.txt")
        return 1
    missing = [p for p in PACKAGES if not _importable(p)]
    if missing:
        ui.say(f"Missing packages ({', '.join(missing)}). Run:  .venv/bin/pip install -r requirements.txt")
        return 1
    for step in (step_accounts, step_sender, step_claude, step_calendars, step_gog,
                 step_optional, step_pairing, step_jobs):
        step(ui)
    ui.say("\n== Where things stand ==")
    return check(ui)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Set up Grand Central.")
    ap.add_argument("--check", action="store_true", help="report only; change nothing")
    a = ap.parse_args(argv)
    ui = Console()
    try:
        return check(ui) if a.check else run(ui)
    except KeyboardInterrupt:
        ui.say("\nStopped. Anything already saved stays saved; run again to continue.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
