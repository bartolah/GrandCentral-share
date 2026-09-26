"""Shared helpers for the Grand Central sweeper.

Path resolution, the tiny .env loader, the project-registry loader, atomic
JSON writes, and a small run logger. Kept dependency-light on purpose; the only
third-party import here is PyYAML for the registry.

Nothing in this module talks to a network or a live source, so it is safe to
import from any module (including the independently-runnable readers).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import yaml

# Repo root = parent of sweeper/. Everything else is relative to it so the
# sweeper behaves identically under launchd or when run by hand.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")
DASHBOARD_DIR = os.path.join(ROOT, "dashboard")
LOGS_DIR = os.path.join(ROOT, "logs")
DATA_DIR = os.path.join(ROOT, "data")
# GC_DB_PATH lets tests (and a rehearsal copy) point at a scratch database.
DB_PATH = os.environ.get("GC_DB_PATH") or os.path.join(DATA_DIR, "grand_central.db")

ENV_PATH = os.path.join(CONFIG_DIR, ".env")
REGISTRY_PATH = os.path.join(CONFIG_DIR, "projects.yaml")
OUTPUT_PATH = os.path.join(DASHBOARD_DIR, "grand_central.json")


# --------------------------------------------------------------------------
# .env
# --------------------------------------------------------------------------
def load_env(path: str = ENV_PATH) -> dict:
    """Parse a simple KEY=value .env file into a dict.

    Ignores blank lines and #-comments. Does NOT export into os.environ, so
    secrets never leak into the process environment of child processes.
    Values are taken verbatim after the first '='.
    """
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip()
    return env


def require_env(env: dict, key: str) -> str:
    """Fetch a required credential, with a clear error if it's missing/unset."""
    val = env.get(key, "")
    placeholder = (not val) or val.startswith(("PASTE_", "CHANGE_", "sk-ant-xxx"))
    if placeholder:
        raise RuntimeError(
            f"{key} is not set in config/.env (found: {val!r}). "
            f"Fill it in and re-run."
        )
    return val


# --------------------------------------------------------------------------
# Settings (config/projects.yaml)
# --------------------------------------------------------------------------
# Account-first: the theaters/operations/objectives/rules in this file are
# retired and never read. Only `defaults:` and `key_people:` are consumed. The
# file stays on disk (with config/backups/) until the irreversible cleanup.
def load_registry(path: str = REGISTRY_PATH) -> dict:
    """Load config/projects.yaml as a plain dict; a missing file -> {}."""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------
def iso_z(dt: datetime) -> str:
    """UTC ISO-8601 with a trailing Z, matching the JSON contract."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------
# Atomic JSON write (temp + rename); dashboard never sees a half file.
# --------------------------------------------------------------------------
def atomic_write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on the same filesystem


# --------------------------------------------------------------------------
# Small run logger: collects lines, prints them, flushes to a file at the end.
# --------------------------------------------------------------------------
class RunLog:
    def __init__(self):
        self.lines: list[str] = []

    def __call__(self, msg: str) -> None:
        stamp = datetime.now().astimezone().strftime("%H:%M:%S")
        line = f"[{stamp}] {msg}"
        self.lines.append(line)
        print(line, flush=True)

    def write_to(self, path: str) -> None:
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.lines) + "\n")
        except OSError as e:
            print(f"warning: could not write log file: {e!r}", file=sys.stderr)
