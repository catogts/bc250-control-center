#!/usr/bin/env python3
"""BC250 Control Center - web frontend (phase 1: read-only API + dashboard).

Wraps the headless CLI (bc250-control-center-cli --json <cmd>) as a small
HTTP API plus a static dashboard. Hardware write operations are NOT exposed
here on purpose: they must keep going through the typed privileged helpers
and their subsystem safety workflows, exactly as the desktop frontend does.

Usage:
  BC250_WEB_TOKEN=$(openssl rand -hex 16) python3 frontends/web/server.py

Env:
  BC250_WEB_PORT   listen port (default 8089)
  BC250_WEB_BIND   bind address (default 0.0.0.0)
  BC250_WEB_TOKEN  if set, every /api/ call needs header  X-Auth: <token>
                   or query  ?token=<token>
  BC250_WEB_CLI    CLI binary (default bc250-control-center-cli)
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("BC250_WEB_PORT", "8089"))
BIND = os.environ.get("BC250_WEB_BIND", "0.0.0.0")
TOKEN = os.environ.get("BC250_WEB_TOKEN", "")
CLI = os.environ.get("BC250_WEB_CLI", "bc250-control-center-cli")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Commands exposed read-only; mirrors the headless dispatch_safe table.
READ_COMMANDS = {
    "telemetry": 2.0,
    "system": 30.0,
    "components": 30.0,
    "profiles": 5.0,
    "quick-access": 30.0,
    "metrics": 10.0,
}

_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def run_cli(command: str) -> bytes:
    ttl = READ_COMMANDS[command]
    now = time.time()
    with _lock:
        hit = _cache.get(command)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        proc = subprocess.run(
            [CLI, "--json", command],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if proc.returncode != 0:
            payload = {"error": proc.stderr.strip() or "cli exit %d" % proc.returncode}
        else:
            payload = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        payload = {"error": repr(exc)}
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    with _lock:
        _cache[command] = (time.time(), body)
    return body


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, query) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("X-Auth", "")
        return header == TOKEN or query.get("token", [""])[0] == TOKEN

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path.startswith("/api/"):
            command = path[len("/api/"):].strip("/")
            if not self._authorized(query):
                self._send(401, b'{"error":"unauthorized"}', "application/json")
                return
            if command in READ_COMMANDS:
                self._send(200, run_cli(command), "application/json")
            else:
                self._send(404, b'{"error":"unknown command"}', "application/json")
            return
        if path == "/api/write":
            self._send(
                501,
                b'{"error":"write operations are not exposed by the web frontend"}',
                "application/json",
            )
            return
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(STATIC, "index.html"), "rb") as handle:
                    self._send(200, handle.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, b"missing static/index.html", "text/plain")
            return
        self._send(404, b"not found", "text/plain")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    print("bc250 web frontend on http://%s:%d (token: %s)" % (BIND, PORT, "on" if TOKEN else "OFF"))
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
