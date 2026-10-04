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
FAN_HELPER = "/usr/libexec/bc250-control-center/bc250-fan-pwm-helper"
ENABLE_WRITE = os.environ.get("BC250_WEB_ENABLE_WRITE", "0") == "1"

# Read-only commands exposed, with their fixed CLI arguments.
# profiles/recovery/import actions are intentionally absent: they are not read-only.
READ_COMMANDS = {
    "telemetry": ([], 2.0),
    "system": ([], 30.0),
    "components": ([], 30.0),
    "quick-access": ([], 30.0),
    "metrics": (["list", "--limit", "50"], 10.0),
}

_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()


def run_cli(command: str) -> bytes:
    extra, ttl = READ_COMMANDS[command]
    now = time.time()
    with _lock:
        hit = _cache.get(command)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        proc = subprocess.run(
            [CLI, "--json", command] + extra,
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


def read_fan_state() -> bytes:
    now = time.time()
    with _lock:
        hit = _cache.get("fans")
        if hit and now - hit[0] < 3:
            return hit[1]
    channels = []
    base = "/sys/class/hwmon"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []
    for hw in names:
        try:
            with open(os.path.join(base, hw, "name")) as handle:
                chip = handle.read().strip()
        except OSError:
            continue
        if not chip.startswith("nct668"):
            continue
        for ch in range(1, 13):
            entry = {"channel": ch, "chip": chip}
            try:
                with open(os.path.join(base, hw, "pwm%d" % ch)) as handle:
                    entry["duty"] = int(handle.read().strip())
            except (OSError, ValueError):
                continue
            for key, node in (("enable", "pwm%d_enable"), ("rpm", "fan%d_input"), ("label", "fan%d_label")):
                try:
                    with open(os.path.join(base, hw, node % ch)) as handle:
                        raw = handle.read().strip()
                        entry[key] = int(raw) if key != "label" and raw.isdigit() else raw
                except (OSError, ValueError):
                    pass
            channels.append(entry)
        break
    body = json.dumps({"channels": channels, "write_enabled": ENABLE_WRITE}, ensure_ascii=False).encode()
    with _lock:
        _cache["fans"] = (time.time(), body)
    return body


def fan_control(payload: dict) -> tuple[int, bytes]:
    if not ENABLE_WRITE:
        return 503, b'{"ok":false,"detail":"writes disabled (BC250_WEB_ENABLE_WRITE)"}'
    op = payload.get("op")
    channel = payload.get("channel")
    if not isinstance(channel, int) or not 1 <= channel <= 12:
        return 400, b'{"ok":false,"detail":"channel must be an integer 1..12"}'
    if op == "set":
        value = payload.get("value")
        if not isinstance(value, int) or not 0 <= value <= 255:
            return 400, b'{"ok":false,"detail":"value must be an integer 0..255"}'
        line = "%d %d" % (channel, value)
    elif op == "auto":
        line = "AUTO %d" % channel
    else:
        return 400, b'{"ok":false,"detail":"op must be set|auto"}'
    try:
        proc = subprocess.run(
            ["sudo", "-n", FAN_HELPER],
            input=line + "\nEXIT\n",
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 500, json.dumps({"ok": False, "detail": repr(exc)}).encode()
    lines = [l for l in proc.stdout.splitlines() if l not in ("READY", "BYE")]
    detail = proc.stderr.strip() or " ".join(lines)
    ok = proc.returncode == 0 and any(l.startswith("OK") for l in lines)
    with _lock:
        _cache.pop("fans", None)
    out = json.dumps({"ok": ok, "detail": detail, "sent": line}, ensure_ascii=False).encode()
    return (200 if ok else 500), out


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
            elif command == "fans":
                self._send(200, read_fan_state(), "application/json")
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

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != "/api/fan":
            self._send(404, b'{"error":"unknown endpoint"}', "application/json")
            return
        if not self._authorized(query) and self.headers.get("X-Auth", "") != TOKEN:
            self._send(401, b'{"error":"unauthorized"}', "application/json")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, b'{"ok":false,"detail":"invalid json"}', "application/json")
            return
        code, body = fan_control(payload if isinstance(payload, dict) else {})
        self._send(code, body, "application/json")

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    print("bc250 web frontend on http://%s:%d (token: %s)" % (BIND, PORT, "on" if TOKEN else "OFF"))
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
