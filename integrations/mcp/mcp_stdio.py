"""A minimal, dependency-free MCP server over stdio.

MCP's stdio transport is newline-delimited JSON-RPC 2.0: one message per line
on stdin, one response per line on stdout. That is small enough to implement
directly, which keeps BUILD_SPEC §7 ("no heavy frameworks") intact and means
this server cannot break when a third-party SDK changes.

The one hard rule: stdout carries JSON-RPC and NOTHING else. Every diagnostic
goes to stderr or the log file, because a stray print corrupts the stream and
the client drops the connection.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Callable

# Versions this server is happy to speak. tools/list and tools/call are stable
# across all of them, so we echo whatever the client asks for when we know it
# and otherwise answer with our preferred version.
KNOWN_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
    "2025-11-25",
}
PREFERRED_PROTOCOL_VERSION = "2025-06-18"

# JSON-RPC error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Tool:
    def __init__(self, name: str, description: str, schema: dict,
                 handler: Callable[..., Any], read_only: bool = False):
        self.name = name
        self.description = description
        self.schema = schema
        self.handler = handler
        self.read_only = read_only

    def spec(self) -> dict:
        # `annotations` lets a client show which tools only read. Bart's board
        # is a live mailbox; knowing read from write matters.
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.schema,
            "annotations": {
                "readOnlyHint": self.read_only,
                "destructiveHint": False,
                "title": self.name.replace("_", " ").title(),
            },
        }


class Server:
    def __init__(self, name: str, version: str, log=None):
        self.name = name
        self.version = version
        self.tools: dict[str, Tool] = {}
        self._log = log or (lambda *a, **k: None)
        self._initialized = False

    # -- registration -------------------------------------------------------

    def tool(self, name: str, description: str, schema: dict, read_only: bool = False):
        def deco(fn):
            self.tools[name] = Tool(name, description, schema, fn, read_only)
            return fn
        return deco

    # -- protocol -----------------------------------------------------------

    def _handle(self, msg: dict) -> dict | None:
        """Return a response dict, or None for notifications."""
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        is_notification = "id" not in msg

        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in KNOWN_PROTOCOL_VERSIONS else PREFERRED_PROTOCOL_VERSION
            self._initialized = True
            self._log(f"initialize from {params.get('clientInfo', {})} -> protocol {version}")
            return _result(msg_id, {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            })

        if method in ("notifications/initialized", "initialized"):
            return None

        if method == "ping":
            return _result(msg_id, {})

        if method == "tools/list":
            return _result(msg_id, {"tools": [t.spec() for t in self.tools.values()]})

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            tool = self.tools.get(name)
            if tool is None:
                return _error(msg_id, INVALID_PARAMS, f"No such tool: {name}")
            try:
                out = tool.handler(**args)
                return _result(msg_id, {"content": [_text(out)], "isError": False})
            except TypeError as exc:
                # Bad/missing arguments from the model — a tool-level error so
                # the agent can read it and correct itself, not a transport error.
                return _result(msg_id, {
                    "content": [_text(f"Bad arguments for {name}: {exc}")],
                    "isError": True,
                })
            except Exception as exc:
                self._log(f"tool {name} failed: {exc}\n{traceback.format_exc()}")
                return _result(msg_id, {
                    "content": [_text(f"{type(exc).__name__}: {exc}")],
                    "isError": True,
                })

        if is_notification:
            return None
        if method in ("resources/list", "prompts/list", "resources/templates/list"):
            # We advertise neither; answer cleanly rather than crashing probes.
            return _error(msg_id, METHOD_NOT_FOUND, f"{self.name} exposes tools only")
        return _error(msg_id, METHOD_NOT_FOUND, f"Unknown method: {method}")

    # -- main loop ----------------------------------------------------------

    def run(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        self._log(f"{self.name} {self.version} starting on stdio")
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                _write(stdout, _error(None, PARSE_ERROR, "Invalid JSON"))
                continue

            # A client may batch requests in one array.
            batch = msg if isinstance(msg, list) else [msg]
            replies = []
            for item in batch:
                if not isinstance(item, dict):
                    replies.append(_error(None, INVALID_REQUEST, "Not a JSON-RPC object"))
                    continue
                try:
                    reply = self._handle(item)
                except Exception as exc:
                    self._log(f"handler crashed: {exc}\n{traceback.format_exc()}")
                    reply = _error(item.get("id"), INTERNAL_ERROR, str(exc))
                if reply is not None:
                    replies.append(reply)

            for reply in replies:
                _write(stdout, reply)
        self._log("stdin closed, exiting")


# -- helpers ----------------------------------------------------------------

def _text(value: Any) -> dict:
    if isinstance(value, str):
        body = value
    else:
        # Compact separators: every byte here is agent context.
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return {"type": "text", "text": body}


def _result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _write(stdout, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    stdout.flush()
