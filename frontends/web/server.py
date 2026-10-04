#!/usr/bin/env python3
"""BC250 Control Center - web frontend.

Read parity with the desktop/CLI frontends plus a deliberately narrow write
surface. Every read that the desktop can show without elevation is reachable
here; every write goes through the same typed, root-owned privileged helpers
the desktop reaches through Polkit (`privileged/helpers/`), reached here with
`sudo -n` against an exact-path sudoers rule instead of a graphical agent.

What stays out on purpose:

* No generic command execution. Each endpoint builds one fixed argv for one
  whitelisted helper; the request body never contributes a token that is not
  first type-checked and range-checked against the shared contract.
* Profiles travel their own road: `GET /api/profiles` reads a fresh export,
  and `POST /api/profiles-export|-preview|-import` run the CLI's own
  `profiles export/preview/import`, so bundles keep the repository's sha256,
  transactional replace and automatic pre-import backup. These touch the
  user's own app data only - never root, never a helper.
* No root for reads. Reads touch world-readable state only, so a leaked page
  can observe but never elevate.

Usage:
  BC250_WEB_TOKEN=$(openssl rand -hex 16) python3 frontends/web/server.py

Env:
  BC250_WEB_PORT        listen port (default 8089)
  BC250_WEB_BIND        bind address (default 0.0.0.0)
  BC250_WEB_TOKEN       if set, every /api/ call needs header  X-Auth: <token>
                        or query  ?token=<token>.  Writes accept the *header*
                        only, so a cross-origin form cannot ride a query token.
  BC250_WEB_CLI         CLI binary (default bc250-control-center-cli)
  BC250_WEB_ENABLE_WRITE  1 enables the POST endpoints; with no token set they
                        stay closed, because "authenticated" is the whole gate.
"""
from __future__ import annotations

import configparser
import hashlib
import hmac
import json
import os
import re
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:  # Python 3.11+; the governor file is TOML.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreter
    tomllib = None

PORT = int(os.environ.get("BC250_WEB_PORT", "8089"))
BIND = os.environ.get("BC250_WEB_BIND", "0.0.0.0")
TOKEN = os.environ.get("BC250_WEB_TOKEN", "")
CLI = os.environ.get("BC250_WEB_CLI", "bc250-control-center-cli")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ENABLE_WRITE = os.environ.get("BC250_WEB_ENABLE_WRITE", "0") == "1"

# The one privileged surface: absolute helper paths, matching the polkit
# annotations in privileged/policies/. `sudo -n` may only ever be handed one of
# these as argv[0]; the rules live in /etc/sudoers.d/bc250-web (see README).
HELPER_DIR = "/usr/libexec/bc250-control-center"
FAN_HELPER = HELPER_DIR + "/bc250-fan-pwm-helper"
GOVERNOR_HELPER = HELPER_DIR + "/bc250-governor-config-helper"
CPU_HELPER = HELPER_DIR + "/bc250-cpu-smu-helper"
CU_HELPER = HELPER_DIR + "/bc250-cu-helper"
HELPERS = {"fan": FAN_HELPER, "gpu": GOVERNOR_HELPER, "cpu": CPU_HELPER, "cu": CU_HELPER}

# World-readable state the root helpers publish for unprivileged observers, and
# the configuration files the desktop itself reads.
CPU_LIVE_STATE = "/run/bc250-control-center/cpu-live-state.json"
CU_LIVE_STATE = "/run/bc250-control-center/cu-live-state.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
GOVERNOR_CONFIG = "/etc/cyan-skillfish-governor-smu/config.toml"
OBERON_CONFIG = "/etc/oberon-config.yaml"
CPU_BOOT_CONFIG = "/etc/bc250-smu-oc.conf"
CU_BACKEND_CANDIDATES = (
    HELPER_DIR + "/bc250-cu-live-manager",
    "/var/lib/bc250-control-center/bc250-cu-live-manager",
)
GOVERNOR_SERVICE = "cyan-skillfish-governor-smu.service"
MAX_STATE_BYTES = 64 * 1024

# Profiles (portable config bundles) stage here: unique, 0600 files inside a
# 0700 directory owned by the same unprivileged user the server already runs as.
PROFILES_DIR = os.environ.get("BC250_WEB_PROFILES_DIR", "/tmp/bc250-web-profiles")
MAX_BUNDLE_BYTES = 2 * 1024 * 1024
BUNDLE_APPLICATION = "bc250-control-center"
MAX_STAGED_BUNDLES = 12
PREVIEW_ID_LENGTH = 16


# --------------------------------------------------------------- shared limits

def _load_contract() -> dict:
    """Limits come from bc250cc.shared.contract when it is importable.

    The helpers cannot import it (they run `python3 -I`), so they read a
    generated copy and this module carries the same pinned fallback. Using the
    real module whenever the package is installed keeps the web frontend from
    becoming the fourth copy that drifts; the pinned block keeps the page alive
    on a checkout where the package is not installed.
    """
    try:
        from bc250cc.shared import contract  # type: ignore

        return {
            "source": "bc250cc.shared.contract",
            "cpu_frequency_mhz": tuple(contract.CPU_FREQUENCY_RANGE),
            "cpu_frequency_step_mhz": int(contract.CPU_FREQUENCY_STEP_MHZ),
            "cpu_scale": tuple(contract.CPU_SCALE_RANGE),
            "cpu_temperature_c": tuple(contract.CPU_TEMPERATURE_RANGE),
            "cpu_vid_limit_mv": int(contract.CPU_VID_LIMIT_MV),
            "estimated_vid": contract.estimated_vid,
            "cu_target": tuple(contract.CU_TARGET_RANGE),
            "cu_target_step": int(contract.CU_TARGET_STEP),
            "fan_channels": tuple(contract.DESKTOP_FAN_CHANNEL_RANGE),
            "cu_runtime_schema": int(contract.QUICK_ACCESS_CU_RUNTIME_SCHEMA),
        }
    except Exception:  # pragma: no cover - package absent or half-installed
        return {
            "source": "pinned (mirror of bc250cc/shared/contract.py)",
            "cpu_frequency_mhz": (3100, 4200),
            "cpu_frequency_step_mhz": 50,
            "cpu_scale": (-50, 0),
            "cpu_temperature_c": (70, 90),
            "cpu_vid_limit_mv": 1325,
            "estimated_vid": _estimated_vid_fallback,
            "cu_target": (24, 40),
            "cu_target_step": 2,
            "fan_channels": (1, 12),
            "cu_runtime_schema": 1,
        }


def _estimated_vid_fallback(frequency: int, scale: int) -> int | None:
    """The upstream VID estimate, identical to the contract's coefficients."""
    if type(frequency) is not int or type(scale) is not int or frequency < 3000:
        return None
    p = -1.519 + scale * 0.004325
    q = 2800.0 - (scale * 10.0)
    return round(0.0003 * frequency * frequency + p * frequency + q)


CONTRACT = _load_contract()

# GPU bounds. 500-2400 MHz is the range the desktop treats as Cyan's allowed
# span (domain/gpu/profiles.py: default_cyan_profiles()); the voltage window and
# the 0..6 level ladder are the governor editor's own boundaries.
GPU_ALLOWED_RANGE_MHZ = (500, 2400)
GPU_VOLTAGE_MV_RANGE = (600, 1210)
GPU_VOLTAGE_LEVELS = (0, 1, 2, 3, 4, 5, 6)
GPU_COMPAT_SET_METHODS = ("smu", "kernel")
GPU_COMPAT_USAGE_METHODS = ("busy-flag", "process", "kernel")
MAX_VOLTAGE_POINTS = 40

# The wire names bc250-governor-config-helper actually accepts (its own action
# set, privileged/helpers/bc250-governor-config-helper main()).  The API keeps
# short names for clients, and this map is the only place that knows the
# privileged spelling - an action absent here can never reach argv.
GOVERNOR_ACTIONS = {
    "set-frequency-range": "set-frequency-range",
    "clear-frequency-range": "clear-frequency-range",
    "set-frequency-floor": "set-frequency-floor",
    "set-high-points": {True: "enable-high-points", False: "disable-high-points"},
    "set-voltage-level": "set-cyan-voltage-level",
    "set-custom-voltages": "set-cyan-custom-voltages",
    "ensure-telemetry": "ensure-cyan-telemetry",
    "set-metrics-fix": "set-cyan-metrics-fix",
    "set-compatibility": "set-cyan-compatibility",
}

_cache: dict[str, tuple[float, bytes]] = {}
_lock = threading.Lock()
_audit: deque[dict] = deque(maxlen=200)


# ------------------------------------------------------------------- plumbing

def _cache_get(key: str, ttl: float) -> bytes | None:
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    return None


def _cache_put(key: str, body: bytes) -> bytes:
    with _lock:
        _cache[key] = (time.time(), body)
    return body


def _cache_invalidate(*keys: str) -> None:
    with _lock:
        for key in keys:
            _cache.pop(key, None)


def _json_body(payload: object) -> bytes:
    return json.dumps(payload, ensure_ascii=False, default=str).encode()


def _read_bounded_file(path: str, limit: int = MAX_STATE_BYTES) -> tuple[bytes | None, str | None]:
    """Read one regular file without following symlinks or unbounded sizes."""
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        return None, "missing (%s)" % exc.strerror
    # sysfs attribute nodes (pwm*/fan*/temp*) are symlinks onto the device's
    # own attribute objects. Following them is the point of reading hwmon; a
    # symlink this deep inside /sys cannot redirect anything an operator does
    # not already control, so they are read, not refused.
    if stat.S_ISLNK(metadata.st_mode):
        try:
            target = os.stat(path)
        except OSError as exc:
            return None, "missing (%s)" % exc.strerror
        if not stat.S_ISREG(target.st_mode):
            return None, "refused: symlink target is not a regular file"
        try:
            with open(path, "rb") as handle:
                data = handle.read(limit + 1)
        except OSError as exc:
            return None, "unreadable (%s)" % exc.strerror
        if len(data) > limit:
            return None, "refused: larger than %d bytes" % limit
        return data, None
    if not stat.S_ISREG(metadata.st_mode):
        return None, "refused: not a regular file"
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        return None, "unreadable (%s)" % exc.strerror
    if len(data) > limit:
        return None, "refused: larger than %d bytes" % limit
    return data, None


def _boot_id() -> str:
    raw, _ = _read_bounded_file(BOOT_ID_PATH, 4096)
    return raw.decode("ascii", "ignore").strip() if raw else ""


def _app_version() -> str:
    raw, _ = _read_bounded_file(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "VERSION"), 4096
    )
    return raw.decode("utf-8", "ignore").strip() if raw else ""


# -------------------------------------------------------------------- reads

# Read-only CLI commands, with their fixed arguments and cache TTL.
# profiles/* and every mutating action are intentionally absent.
READ_COMMANDS = {
    "telemetry": ([], 2.0),
    "system": ([], 30.0),
    "components": ([], 30.0),
    "quick-access": ([], 30.0),
    "integrations": (["--runtime"], 60.0),
    "release-gates": ([], 120.0),
    # `recovery` needs its action argument; only the read-only listing is wired.
    "recovery": (["list"], 30.0),
    "metrics": (["list", "--limit", "50"], 10.0),
}


def run_cli(command: str) -> bytes:
    extra, ttl = READ_COMMANDS[command]
    cached = _cache_get(command, ttl)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [CLI, "--json", command] + extra,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        # A non-zero exit still carries a real payload for several read commands
        # (`integrations` answers 3 when a manifest has issues), so the parsed
        # body wins over the status whenever there is one.
        if proc.stdout.strip():
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError:
                payload = {"raw": proc.stdout.strip()}
            if proc.returncode != 0 and isinstance(payload, dict):
                payload = dict(payload, cli_exit_code=proc.returncode)
        else:
            payload = {"error": proc.stderr.strip() or "cli exit %d" % proc.returncode}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        payload = {"error": repr(exc)}
    return _cache_put(command, _json_body(payload))


def _hwmon_chips() -> list[str]:
    base = "/sys/class/hwmon"
    chips: list[str] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return chips
    for hw in entries:
        raw, _ = _read_bounded_file(os.path.join(base, hw, "name"), 4096)
        if raw is None:
            continue
        chip = raw.decode("ascii", "ignore").strip()
        if chip.startswith("nct668"):
            chips.append(hw)
    return chips


def read_fan_state() -> bytes:
    cached = _cache_get("fans", 3.0)
    if cached is not None:
        return cached
    channels: list[dict] = []
    chips = _hwmon_chips()
    base = "/sys/class/hwmon"
    for hw in chips[:1]:
        for ch in range(1, 13):
            entry: dict[str, object] = {"channel": ch, "chip": hw}
            raw, _ = _read_bounded_file(os.path.join(base, hw, "pwm%d" % ch), 64)
            if raw is None:
                continue
            try:
                duty = int(raw.decode().strip())
            except ValueError:
                continue
            entry["duty"] = duty
            entry["duty_percent"] = round(duty * 100 / 255)
            for key, node in (("enable", "pwm%d_enable"), ("rpm", "fan%d_input"), ("label", "fan%d_label")):
                raw_node, _ = _read_bounded_file(os.path.join(base, hw, node % ch), 64)
                if raw_node is None:
                    continue
                text = raw_node.decode().strip()
                entry[key] = int(text) if text.isdigit() else text
            channels.append(entry)
    drivers = []
    out_of_tree = False
    if chips:
        try:
            bound = os.path.basename(os.readlink("/sys/class/hwmon/%s/device/driver" % chips[0]))
        except OSError:
            bound = ""
        if bound == "nct6687":
            # In-tree this chip binds as nct6683; a plain "nct6687" binding is
            # the out-of-tree fork (module name is shared), and that is what
            # makes manual duty cycles stick.
            out_of_tree = True
            drivers.append("nct6687d")
    for module in ("nct6683", "nct6687", "it87"):
        if os.path.exists("/sys/module/" + module):
            drivers.append(module)
    # The shipped in-tree nct6686 reports a proprietary EC mode and refuses
    # manual PWM; the out-of-tree nct6687d (installed by "Prepare
    # dependencies") is what makes manual duty cycles stick. Saying so here
    # turns a rejected write into an explanation instead of a mystery.
    manual_ready = "nct6687d" in drivers
    body = _json_body(
        {
            "channels": channels,
            "chips": chips,
            "sensor_drivers": drivers,
            "manual_pwm_ready": manual_ready,
            "channel_range": list(CONTRACT["fan_channels"]),
            "presets": {"quiet": 45, "balanced": 60, "cooling": 70, "maximum": 100},
            "write_enabled": ENABLE_WRITE,
            "note": None
            if manual_ready
            else "Manual PWM needs the out-of-tree nct6687d driver; run \"Prepare dependencies\" from Desktop Mode.",
        }
    )
    return _cache_put("fans", body)


def _shrink_parsed(value: object, depth: int = 0) -> object:
    """Keep the governor parse small: long point tables become counts."""
    if depth > 4:
        return "..."
    if isinstance(value, dict):
        return {str(k): _shrink_parsed(v, depth + 1) for k, v in list(value.items())[:40]}
    if isinstance(value, list):
        if len(value) > 8:
            return {"count": len(value), "sample": _shrink_parsed(value[:2], depth + 1)}
        return [_shrink_parsed(v, depth + 1) for v in value]
    return value


def read_gpu_state() -> bytes:
    """GPU governor / SMU limits, from the config the governor service reads."""
    cached = _cache_get("gpu", 5.0)
    if cached is not None:
        return cached
    payload: dict[str, object] = {
        "config_path": GOVERNOR_CONFIG,
        "oberon_config_present": os.path.exists(OBERON_CONFIG),
        "limits": {
            "frequency_mhz": list(GPU_ALLOWED_RANGE_MHZ),
            "voltage_mv": list(GPU_VOLTAGE_MV_RANGE),
            "voltage_levels": list(GPU_VOLTAGE_LEVELS),
            "compatibility_set_methods": list(GPU_COMPAT_SET_METHODS),
            "compatibility_usage_methods": list(GPU_COMPAT_USAGE_METHODS),
        },
        "write_enabled": ENABLE_WRITE,
    }
    raw, problem = _read_bounded_file(GOVERNOR_CONFIG)
    if raw is None:
        payload["available"] = False
        payload["reason"] = problem
        if problem and "missing" in problem:
            payload["hint"] = "The Cyan governor is not installed or not prepared."
    else:
        payload["available"] = True
        text = raw.decode("utf-8", "ignore")
        payload["size_bytes"] = len(raw)
        payload["mtime"] = int(os.stat(GOVERNOR_CONFIG).st_mtime)
        if tomllib is not None:
            try:
                payload["parsed"] = _shrink_parsed(tomllib.loads(text))
            except Exception as exc:  # a hand-edited file must not kill the page
                payload["parse_error"] = str(exc)
        # A line scan is the fallback (and the cross-check): a commented key is
        # inactive in this file, which is not something TOML will tell you.
        active: dict[str, str] = {}
        commented: dict[str, str] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            is_comment = stripped.startswith("#")
            body_line = stripped.lstrip("#").strip()
            if "=" not in body_line or body_line.startswith("["):
                continue
            key, _, value = body_line.partition("=")
            key = key.strip()
            if not key:
                continue
            (commented if is_comment else active)[key] = value.strip()
        payload["active_keys"] = active
        payload["commented_keys"] = dict(list(commented.items())[:40])
        payload["service"] = _service_state(GOVERNOR_SERVICE)
    return _cache_put("gpu", _json_body(payload))


def _service_state(unit: str) -> dict:
    systemctl = shutil.which("systemctl")
    if not systemctl:
        return {"unit": unit, "active": "unknown", "detail": "systemctl is not available"}
    try:
        proc = subprocess.run(
            [systemctl, "is-active", unit], capture_output=True, text=True, timeout=5, check=False
        )
        state = (proc.stdout or "").strip()
        return {
            "unit": unit,
            "active": state or "unknown",
            "enabled": _unit_enabled(systemctl, unit),
            "detail": (proc.stderr or "").strip() or None,
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"unit": unit, "active": "unknown", "detail": repr(exc)}


def _unit_enabled(systemctl: str, unit: str) -> str | None:
    try:
        proc = subprocess.run(
            [systemctl, "is-enabled", unit], capture_output=True, text=True, timeout=5, check=False
        )
        return (proc.stdout or "").strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_QAM_PROBE = {"ts": 0.0, "result": None}


def _cpu_observed():
    """Root-free live CPU readings: cpufreq policies and k10temp."""
    out = {}
    try:
        policies = sorted(n for n in os.listdir("/sys/devices/system/cpu/cpufreq")
                          if n.startswith("policy"))
    except OSError:
        policies = []
    best = None
    plist = []
    for policy in policies[:4]:
        base = "/sys/devices/system/cpu/cpufreq/" + policy
        entry = {"policy": policy}
        for key, node in (("min_mhz", "scaling_min_freq"), ("cur_mhz", "scaling_cur_freq"),
                          ("max_mhz", "scaling_max_freq")):
            raw, _ = _read_bounded_file(os.path.join(base, node), 64)
            if raw is not None and raw.decode(errors="replace").strip().isdigit():
                value = int(raw.decode().strip()) // 1000
                entry[key] = value
                if key == "cur_mhz" and (best is None or value > best):
                    best = value
        plist.append(entry)
    if plist:
        out["policies"] = plist
    if best is not None:
        out["current_mhz"] = best
    else:
        # BC-250 with the SMU path exposes no cpufreq policies at all; the
        # world-readable per-core aggregate in /proc/cpuinfo is the fallback.
        raw, _ = _read_bounded_file("/proc/cpuinfo", 256 * 1024)
        vals = []
        if raw is not None:
            for line in raw.decode(errors="replace").splitlines():
                if line.startswith("cpu MHz"):
                    try:
                        vals.append(float(line.split(":", 1)[1].strip()))
                    except ValueError:
                        pass
        if vals:
            out["current_mhz"] = round(max(vals))
            out["cores_reported"] = len(vals)
            out["source"] = "proc-cpuinfo"
    try:
        hwmons = sorted(os.listdir("/sys/class/hwmon"))
    except OSError:
        hwmons = []
    for hw in hwmons:
        raw, _ = _read_bounded_file("/sys/class/hwmon/%s/name" % hw, 64)
        if raw is None or raw.decode(errors="replace").strip() != "k10temp":
            continue
        raw, _ = _read_bounded_file("/sys/class/hwmon/%s/temp1_input" % hw, 64)
        if raw is not None and raw.decode(errors="replace").strip().lstrip("-").isdigit():
            out["temp_c"] = round(int(raw.decode().strip()) / 1000.0, 1)
            break
    return out


def _cpu_qam_probe():
    """bc250-cpu-smu-helper qam-status: the app's own root-side CPU read.

    Read-only, takes no arguments, and publishes the public live snapshot when
    detection evidence exists, so the panel converges on the same numbers the
    desktop shows instead of "missing" forever after a reboot.
    """
    now = time.time()
    if now - _QAM_PROBE["ts"] < 30.0 and _QAM_PROBE["result"] is not None:
        return dict(_QAM_PROBE["result"])
    result = {"attempted": False}
    if os.path.exists(CPU_HELPER):
        result["attempted"] = True
        try:
            proc = subprocess.run(["sudo", "-n", CPU_HELPER, "qam-status"],
                                  capture_output=True, text=True, timeout=15)
            payload = None
            for line in reversed((proc.stdout or "").splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        payload = None
                    break
            if isinstance(payload, dict):
                result["ok"] = proc.returncode == 0
                result.update(payload)
            else:
                result["ok"] = False
                result["error"] = ((proc.stderr or proc.stdout or "").strip() or "no json")[:200]
        except (OSError, subprocess.SubprocessError) as exc:
            result["ok"] = False
            result["error"] = str(exc)[:200]
    _QAM_PROBE["ts"] = now
    _QAM_PROBE["result"] = result
    return dict(result)


def read_cpu_state() -> bytes:
    """CPU OC state: the helper's public live snapshot plus the boot config."""
    cached = _cache_get("cpu", 4.0)
    if cached is not None:
        return cached
    limits = {
        "frequency_mhz": list(CONTRACT["cpu_frequency_mhz"]),
        "frequency_step_mhz": CONTRACT["cpu_frequency_step_mhz"],
        "scale": list(CONTRACT["cpu_scale"]),
        "temperature_c": list(CONTRACT["cpu_temperature_c"]),
        "vid_limit_mv": CONTRACT["cpu_vid_limit_mv"],
    }
    payload: dict[str, object] = {"limits": limits, "write_enabled": ENABLE_WRITE}
    payload["observed"] = _cpu_observed()

    raw, problem = _read_bounded_file(CPU_LIVE_STATE)
    live: dict[str, object] = {"path": CPU_LIVE_STATE}
    if raw is None:
        live["available"] = False
        live["reason"] = problem
        live["hint"] = (
            "No live CPU OC snapshot for this boot. The snapshot is published by "
            "bc250-cpu-smu-helper when a profile is applied or its status is read."
        )
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            parsed = None
            live["error"] = "invalid json: %s" % exc
        if isinstance(parsed, dict):
            boot_id = _boot_id()
            mismatch = bool(boot_id) and parsed.get("boot_id") not in (None, boot_id)
            live["available"] = not mismatch
            live["boot_id_matches_current_boot"] = not mismatch
            live["schema"] = parsed.get("schema")
            live["producer"] = parsed.get("producer")
            live["helper_protocol"] = parsed.get("helper_protocol")
            live["observed_at_unix_ms"] = parsed.get("observed_at_unix_ms")
            profile = parsed.get("active_profile")
            live["active_profile"] = profile if isinstance(profile, dict) else None
            if mismatch:
                live["reason"] = "snapshot belongs to a previous boot"
    payload["live"] = live
    if not live.get("available"):
        payload["qam_status"] = _cpu_qam_probe()

    raw_conf, conf_problem = _read_bounded_file(CPU_BOOT_CONFIG)
    boot: dict[str, object] = {"path": CPU_BOOT_CONFIG}
    if raw_conf is None:
        boot["available"] = False
        boot["reason"] = conf_problem
    else:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(raw_conf.decode("utf-8", "ignore"))
            section = parser["overclock"] if parser.has_section("overclock") else {}
            persisted = {}
            for key, value in dict(section).items():
                try:
                    persisted[key] = int(value)
                except ValueError:
                    persisted[key] = value
            boot["available"] = True
            boot["persisted"] = persisted
        except (configparser.Error, ValueError, UnicodeDecodeError) as exc:
            boot["available"] = False
            boot["reason"] = "unparsable: %s" % exc
    payload["boot_config"] = boot
    return _cache_put("cpu", _json_body(payload))


CU_TOKENS = frozenset({"D+", "S+", "D!", "--"})


def _validate_cu_masks(value: object) -> list[int] | None:
    if not isinstance(value, list) or len(value) != 4 or any(type(item) is not int for item in value):
        return None
    if any(mask < 0 or mask > 0x1F for mask in value):
        return None
    return [int(mask) for mask in value]


def _cu_active_count(masks: list[int]) -> int:
    return sum(bin(mask).count("1") * 2 for mask in masks)


def _validate_cu_tokens(value: object) -> list[list[str]] | None:
    if not isinstance(value, list) or len(value) != 4:
        return None
    rows: list[list[str]] = []
    for row in value:
        if not isinstance(row, list) or len(row) != 5 or any(not isinstance(t, str) for t in row):
            return None
        upper = [token.upper() for token in row]
        if any(token not in CU_TOKENS for token in upper):
            return None
        rows.append(upper)
    return rows


def read_cu_state() -> bytes:
    """Compute Units: the Quick Access runtime snapshot, re-validated, not trusted."""
    cached = _cache_get("cu", 4.0)
    if cached is not None:
        return cached
    targets = list(range(CONTRACT["cu_target"][0], CONTRACT["cu_target"][1] + 1, CONTRACT["cu_target_step"]))
    payload: dict[str, object] = {
        "targets": targets,
        "row_wgps": 5,
        "cu_per_wgp": 2,
        "backend_ready": any(os.path.exists(path) for path in CU_BACKEND_CANDIDATES),
        "backend_paths": [path for path in CU_BACKEND_CANDIDATES if os.path.exists(path)],
        "prerequisites": {
            "umr": shutil.which("umr") is not None,
            "staged_backend": any(os.path.exists(path) for path in CU_BACKEND_CANDIDATES),
            "desktop_resource_tools": os.path.isdir(os.path.expanduser(
                "~/.local/share/bc250-control-center/ResourceTools")),
        },
        "write_enabled": ENABLE_WRITE,
    }
    state: dict[str, object] = {"path": CU_LIVE_STATE}
    raw, problem = _read_bounded_file(CU_LIVE_STATE)
    if raw is None:
        state.update({"available": False, "reason": problem})
        state["hint"] = (
            "No live CU topology snapshot for this boot. Live CU readback lives in "
            "the manager that the Desktop Mode dependency preparation stages (umr "
            "alone is not enough); once it exists, any CU change from Desktop or "
            "Game Mode publishes the table here, and web writes follow it."
        )
    else:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            parsed = None
            state["error"] = "invalid json: %s" % exc
        if isinstance(parsed, dict):
            boot_id = _boot_id()
            mismatch = bool(boot_id) and parsed.get("boot_id") not in (None, boot_id)
            masks = _validate_cu_masks(parsed.get("masks"))
            tokens = _validate_cu_tokens(parsed.get("tokens"))
            count = _cu_active_count(masks) if masks else None
            consistent = (
                masks is not None
                and parsed.get("schema") == CONTRACT["cu_runtime_schema"]
                and parsed.get("producer") == "bc250-quick-access-helper"
                and not mismatch
            )
            state["available"] = bool(masks)
            state["schema"] = parsed.get("schema")
            state["producer"] = parsed.get("producer")
            state["boot_id_matches_current_boot"] = not mismatch
            state["observed_at_unix_ms"] = parsed.get("observed_at_unix_ms")
            state["masks"] = masks
            state["tokens"] = tokens
            state["active_cus"] = count
            state["count_in_contract"] = count in targets if count is not None else False
            state["internally_consistent"] = bool(consistent)
            if parsed.get("active") is not None and count is not None:
                state["reported_active_mismatch"] = parsed.get("active") != count
            if masks is None:
                state["reason"] = "snapshot masks are missing or out of range"
            elif mismatch:
                state["reason"] = "snapshot belongs to a previous boot"
    payload["live"] = state
    dashboard, dashboard_error = _cu_dashboard()
    if dashboard is not None:
        payload["dashboard"] = dashboard
    if dashboard_error:
        payload["dashboard_error"] = dashboard_error
    return _cache_put("cu", _json_body(payload))


def _cu_dashboard() -> tuple[dict | None, str]:
    """Read the staged CU manager once; its status table is the live topology.

    The manager prints the same dashboard Desktop Mode shows: per-row WGP
    dispatch states, the SPI mask and amdgpu's booted CU count. Parsing is
    deliberately forgiving: unknown rows are skipped, never guessed.
    """
    try:
        proc = subprocess.run(
            ["sudo", "-n", CU_HELPER, "status"],
            capture_output=True, text=True, timeout=45, check=False,
        )
    except Exception as exc:
        return None, "backend status unavailable: %s" % exc
    if proc.returncode != 0:
        return None, (proc.stderr or proc.stdout or "helper exited %d" % proc.returncode).strip()[:300]
    out = proc.stdout
    dashboard: dict[str, object] = {}
    match = re.search(r"ASIC\s*:\s*(\S+)", out)
    if match:
        dashboard["asic"] = match.group(1)
    match = re.search(r"active_cu_number=(\d+)", out)
    if match:
        dashboard["amdgpu_active_cu_number"] = int(match.group(1))
    match = re.search(r"CUs active & routed\s*:\s*(\d+)\s*/\s*(\d+)", out)
    if match:
        dashboard["active"] = int(match.group(1))
        dashboard["total"] = int(match.group(2))
    match = re.search(r"Legend\s*:\s*(.+)", out)
    if match:
        dashboard["legend"] = match.group(1).strip()
    rows: list[dict] = []
    for line in out.splitlines():
        stripped = line.strip()
        if not (stripped.startswith("|") and "SE" in stripped and ".SH" in stripped):
            continue
        cells = [cell.strip() for cell in stripped.split("|")]
        head = cells[1] if len(cells) > 1 else ""
        se_sh = re.match(r"SE(\d+)\.SH(\d+)$", head)
        if not se_sh or len(cells) < 10:
            continue
        states = cells[2:7]
        mask = 0
        for index, state in enumerate(states):
            if state.startswith("D+") or state.startswith("S+"):
                mask |= 1 << index
        rows.append({
            "se": int(se_sh.group(1)),
            "sh": int(se_sh.group(2)),
            "wgps": states,
            "spi": cells[7],
            "cus": cells[9],
            "mask": mask,
        })
    if rows:
        dashboard["rows"] = rows
    if not dashboard:
        return None, "backend status did not contain a parsable dashboard"
    dashboard["observed_at_unix_ms"] = int(time.time() * 1000)
    return dashboard, ""



def read_capabilities() -> bytes:
    cached = _cache_get("capabilities", 15.0)
    if cached is not None:
        return cached
    payload = {
        "app_version": _app_version(),
        "contract_source": CONTRACT["source"],
        "auth": "token" if TOKEN else "none",
        "write_enabled": ENABLE_WRITE,
        "sudo_available": bool(shutil.which("sudo")),
        "cli": CLI,
        "cli_available": bool(shutil.which(CLI)),
        "helpers": {name: {"path": path, "installed": os.path.exists(path)} for name, path in HELPERS.items()},
        "limits": {
            "cpu": {
                "frequency_mhz": list(CONTRACT["cpu_frequency_mhz"]),
                "frequency_step_mhz": CONTRACT["cpu_frequency_step_mhz"],
                "scale": list(CONTRACT["cpu_scale"]),
                "temperature_c": list(CONTRACT["cpu_temperature_c"]),
                "vid_limit_mv": CONTRACT["cpu_vid_limit_mv"],
            },
            "gpu": {
                "frequency_mhz": list(GPU_ALLOWED_RANGE_MHZ),
                "voltage_mv": list(GPU_VOLTAGE_MV_RANGE),
                "voltage_levels": list(GPU_VOLTAGE_LEVELS),
            },
            "cu": {"targets": list(range(CONTRACT["cu_target"][0], CONTRACT["cu_target"][1] + 1, CONTRACT["cu_target_step"]))},
            "fan": {"channels": list(CONTRACT["fan_channels"]), "duty": [0, 255]},
        },
        "reads": sorted(READ_COMMANDS) + sorted(LOCAL_READS),
        "writes": sorted(WRITE_PLANNERS) + sorted(PROFILES_ACTIONS),
        "hardware": _hardware_identity(),
    }
    return _cache_put("capabilities", _json_body(payload))


def _hardware_identity() -> dict:
    identity: dict[str, object] = {}
    for key, path in (
        ("sys_vendor", "/sys/class/dmi/id/sys_vendor"),
        ("product_name", "/sys/class/dmi/id/product_name"),
        ("bios_version", "/sys/class/dmi/id/bios_version"),
    ):
        raw, _ = _read_bounded_file(path, 4096)
        if raw is not None:
            identity[key] = raw.decode("utf-8", "ignore").strip()
    identity["cpu_model"] = ""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                if line.startswith("model name"):
                    identity["cpu_model"] = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    return identity


def read_audit() -> bytes:
    with _lock:
        entries = list(_audit)
    return _json_body({"entries": entries[-100:]})


def _profiles_cli(args: list[str], timeout: float = 30.0) -> tuple[int, object, str]:
    """Fixed argv into the CLI's own `profiles` verb; the client adds no token."""
    try:
        proc = subprocess.run(
            [CLI, "--json", "profiles", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, None, repr(exc)
    payload: object = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = {"raw": proc.stdout.strip()}
    return proc.returncode, payload, (proc.stderr or "").strip()


def read_profiles_state() -> bytes:
    """The live config + profile sections as one fresh, checksummed bundle.

    The CLI's exporter is the only front end that assembles exactly the
    document the desktop would compare an import against, so the web reads
    through it instead of guessing at config file layouts. The stage file is
    this user's own file under /tmp; no elevation is involved.
    """
    cached = _cache_get("profiles", 10.0)
    if cached is not None:
        return cached
    try:
        os.makedirs(PROFILES_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        return _json_body({"error": "profiles stage dir: %r" % exc})
    path = os.path.join(PROFILES_DIR, "read.json")
    code, _payload, stderr = _profiles_cli(["export", path])
    if code != 0:
        return _cache_put(
            "profiles",
            _json_body({"error": stderr or "profiles export failed", "cli_exit_code": code}),
        )
    summary: dict[str, object] = {"exported": path}
    raw, _ = _read_bounded_file(path, MAX_BUNDLE_BYTES)
    document = None
    if raw is not None:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = None
    if isinstance(document, dict):
        summary["bundle"] = document
        summary["config_keys"] = sorted(document.get("config", {}))
        summary["profile_sections"] = sorted(document.get("profiles", {}))
        summary["config_count"] = len(document.get("config", {}))
        summary["profile_counts"] = {
            name: (len(value) if isinstance(value, (dict, list)) else 1)
            for name, value in sorted((document.get("profiles") or {}).items())
        }
        summary["exported_at"] = document.get("exported_at")
    else:
        summary["error"] = "export file could not be read"
    summary["stage_dir"] = PROFILES_DIR
    summary["application"] = BUNDLE_APPLICATION
    summary["max_bundle_bytes"] = MAX_BUNDLE_BYTES
    summary["write_enabled"] = ENABLE_WRITE
    return _cache_put("profiles", _json_body(summary))


LOCAL_READS = {
    "fans": read_fan_state,
    "gpu": read_gpu_state,
    "cpu": read_cpu_state,
    "cu": read_cu_state,
    "capabilities": read_capabilities,
    "audit": read_audit,
    "profiles": read_profiles_state,
}


# ------------------------------------------------------------------- writes

class WriteError(Exception):
    """A request the server refuses before it ever reaches a helper."""

    def __init__(self, detail: str, code: int = 400) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


def _int_field(payload: dict, key: str, low: int, high: int) -> int:
    value = payload.get(key)
    if type(value) is not int:
        raise WriteError("%s must be an integer" % key)
    if not low <= value <= high:
        raise WriteError("%s must be between %d and %d" % (key, low, high))
    return value


def _bool_field(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise WriteError("%s must be true or false" % key)
    return value


def _flag_field(payload: dict, key: str) -> str:
    return "1" if _bool_field(payload, key) else "0"


def _choice_field(payload: dict, key: str, choices: tuple[str, ...]) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or value not in choices:
        raise WriteError("%s must be one of %s" % (key, ", ".join(choices)))
    return value


def _lookup(current: object, dotted: str) -> tuple[bool, object]:
    """Resolve `channels.3.duty` against a freshly read snapshot."""
    node = current
    for part in dotted.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, node


# Which local read backs each writable surface. The stale check re-reads through
# this map, and the reverse map stamps a snapshot hash onto the read itself.
STATE_READ = {"fan": "fans", "gpu": "gpu", "cpu": "cpu", "cu": "cu", "profiles": "profiles"}
READ_SURFACE = {read_key: kind for kind, read_key in STATE_READ.items()}


def _projection(kind: str, data: dict) -> dict:
    """The part of a snapshot an operator actually confirmed.

    RPM, timestamps and `elapsed` fields are dropped on purpose: they move every
    second, so hashing them would reject a fan write that follows an unrelated
    fan write five seconds earlier. What stays is exactly what the confirm modal
    displayed - duties, the governor file's contents and mtime, the CPU profile,
    the CU table.
    """
    if kind == "fan":
        return {
            "channels": [
                {"channel": row.get("channel"), "duty": row.get("duty"), "enable": row.get("enable")}
                for row in data.get("channels", [])
            ],
            "chips": data.get("chips"),
            "sensor_drivers": data.get("sensor_drivers"),
        }
    if kind == "gpu":
        return {
            key: data.get(key)
            for key in (
                "available",
                "reason",
                "mtime",
                "size_bytes",
                "parsed",
                "active_keys",
                "oberon_config_present",
            )
        }
    if kind == "cpu":
        live = data.get("live") or {}
        boot = data.get("boot_config") or {}
        return {
            "available": live.get("available"),
            "active_profile": live.get("active_profile"),
            "boot_available": boot.get("available"),
            "persisted": boot.get("persisted"),
        }
    if kind == "cu":
        live = data.get("live") or {}
        return {
            "available": live.get("available"),
            "masks": live.get("masks"),
            "tokens": live.get("tokens"),
            "backend_ready": data.get("backend_ready"),
        }
    if kind == "profiles":
        # The bundle carries an `exported_at` stamp that changes on every read;
        # what an operator confirms is the content, so only that is hashed.
        bundle = data.get("bundle") or {}
        return {"config": bundle.get("config"), "profiles": bundle.get("profiles")}
    return {}


def _state_hash(kind: str, data: dict) -> str:
    canonical = json.dumps(_projection(kind, data), sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _fresh_snapshot(kind: str) -> dict:
    read_key = STATE_READ[kind]
    _cache_invalidate(read_key)
    return json.loads(LOCAL_READS[read_key]())


def _check_stale(kind: str, payload: dict) -> None:
    """Refuse to apply a change onto state the page never showed.

    The panel reads, renders old -> new, and echoes the hash of exactly what it
    displayed. If anything in that projection moved in between - a second
    operator, Desktop Mode, Game Mode, a thermal policy - the operator would be
    confirming a different change than the one being applied, so the write stops
    with 409 and the panel re-reads.
    """
    expected_state = payload.get("expected_state")
    if not isinstance(expected_state, str) or not 8 <= len(expected_state) <= 64:
        raise WriteError(
            "expected_state is required: reload the panel so the change is confirmed against the values it showed"
        )
    fresh = _fresh_snapshot(kind)
    if _state_hash(kind, fresh) != expected_state:
        raise WriteError(
            "the device moved since this panel read it, so the confirmed old values are no longer current",
            409,
        )
    expected = payload.get("expected")
    if expected is None:
        return
    if not isinstance(expected, dict) or len(expected) > 16:
        raise WriteError("expected must be a small object of dotted paths to values")
    for path, wanted in expected.items():
        if not isinstance(path, str) or not path or len(path) > 96:
            raise WriteError("expected contains an invalid path")
        found, actual = _lookup(fresh, path)
        if not found:
            raise WriteError("expected path %s is not present on the device" % path)
        if actual != wanted:
            raise WriteError(
                "stale confirmation for %s: page showed %r, device is %r" % (path, wanted, actual),
                409,
            )


def plan_fan_write(payload: dict) -> tuple[dict, tuple[str, str]]:
    """The fan helper is a stdin session: `<ch> <val>`, `AUTO <ch>`, `EXIT`."""
    op = _choice_field(payload, "op", ("set", "auto", "preset"))
    low, high = CONTRACT["fan_channels"]
    channel = _int_field(payload, "channel", low, high)
    if op == "auto":
        lines = ["AUTO %d" % channel, "EXIT"]
        return {"op": op, "channel": channel}, ("\n".join(lines) + "\n", "; ".join(lines))
    if op == "preset":
        percent = {
            "quiet": 45,
            "balanced": 60,
            "cooling": 70,
            "maximum": 100,
        }
        name = _choice_field(payload, "preset", tuple(percent))
        duty = round(percent[name] * 255 / 100)
    else:
        if "percent" in payload:
            percent_value = _int_field(payload, "percent", 0, 100)
            duty = round(percent_value * 255 / 100)
        else:
            duty = _int_field(payload, "value", 0, 255)
    lines = ["%d %d" % (channel, duty), "EXIT"]
    return {"op": op, "channel": channel, "duty": duty}, ("\n".join(lines) + "\n", "; ".join(lines))


def plan_gpu_write(payload: dict) -> tuple[dict, list[str]]:
    """Typed mapping onto bc250-governor-config-helper's argv protocol."""
    action = _choice_field(
        payload,
        "action",
        (
            "set-frequency-range",
            "clear-frequency-range",
            "set-frequency-floor",
            "set-high-points",
            "set-voltage-level",
            "set-custom-voltages",
            "ensure-telemetry",
            "set-metrics-fix",
            "set-compatibility",
        ),
    )
    low_mhz, high_mhz = GPU_ALLOWED_RANGE_MHZ
    if action == "set-frequency-range":
        minimum = _int_field(payload, "min_mhz", low_mhz, high_mhz)
        maximum = _int_field(payload, "max_mhz", low_mhz, high_mhz)
        if minimum > maximum:
            raise WriteError("min_mhz cannot exceed max_mhz")
        return {"action": action, "min_mhz": minimum, "max_mhz": maximum}, [action, str(minimum), str(maximum)]
    if action == "set-frequency-floor":
        minimum = _int_field(payload, "min_mhz", 0, high_mhz)
        return {"action": action, "min_mhz": minimum}, [action, str(minimum)]
    if action == "set-voltage-level":
        level = _int_field(payload, "level", min(GPU_VOLTAGE_LEVELS), max(GPU_VOLTAGE_LEVELS))
        return {"action": action, "level": level}, [GOVERNOR_ACTIONS[action], str(level)]
    if action == "set-high-points":
        enabled = _bool_field(payload, "enabled")
        return {"action": action, "enabled": enabled}, [GOVERNOR_ACTIONS[action][enabled]]
    if action == "ensure-telemetry":
        fix = _flag_field(payload, "fix_frequency")
        return {"action": action, "fix_frequency": fix == "1"}, [GOVERNOR_ACTIONS[action], fix]
    if action == "set-metrics-fix":
        enabled_flag = _flag_field(payload, "enabled")
        return {"action": action, "enabled": enabled_flag == "1"}, [GOVERNOR_ACTIONS[action], enabled_flag]
    if action == "set-compatibility":
        set_method = _choice_field(payload, "set_method", GPU_COMPAT_SET_METHODS)
        usage_method = _choice_field(payload, "usage_method", GPU_COMPAT_USAGE_METHODS)
        fix_metrics = _flag_field(payload, "fix_metrics")
        fix_frequency = _flag_field(payload, "fix_frequency")
        return {
            "action": action,
            "set_method": set_method,
            "usage_method": usage_method,
            "fix_metrics": fix_metrics == "1",
            "fix_frequency": fix_frequency == "1",
        }, [GOVERNOR_ACTIONS[action], set_method, usage_method, fix_metrics, fix_frequency]
    if action == "clear-frequency-range":
        # The helper accepts no bounds here, so neither do we: an argument that
        # silently reached argv would be an argument the editor never expected.
        return {"action": action}, [action]
    if action != "set-custom-voltages":
        # The allowlist above owns every branch; reaching here means that tuple and
        # this planner drifted apart.
        raise WriteError("unsupported governor action %s" % action)
    # set-custom-voltages: {"2000": 1050, ...} -> "2000=1050"
    points = payload.get("voltages")
    if not isinstance(points, dict) or not 1 <= len(points) <= MAX_VOLTAGE_POINTS:
        raise WriteError("voltages must be a non-empty object of frequency to millivolts")
    pairs: dict[int, int] = {}
    voltage_low, voltage_high = GPU_VOLTAGE_MV_RANGE
    for raw_frequency, raw_voltage in points.items():
        try:
            frequency = int(str(raw_frequency))
        except ValueError:
            raise WriteError("voltage point frequency must be an integer") from None
        if type(raw_voltage) is not int:
            raise WriteError("voltage point millivolts must be an integer")
        if frequency <= 0 or frequency > 3000:
            raise WriteError("voltage point frequency is out of range")
        if not voltage_low <= raw_voltage <= voltage_high:
            raise WriteError(
                "voltage for %d MHz must be between %d and %d mV" % (frequency, voltage_low, voltage_high)
            )
        pairs[frequency] = raw_voltage
    return {"action": action, "voltages": {str(k): v for k, v in sorted(pairs.items())}}, [
        GOVERNOR_ACTIONS[action], *("%d=%d" % item for item in sorted(pairs.items()))
    ]


def plan_cpu_write(payload: dict) -> tuple[dict, list[str]]:
    """Typed mapping onto bc250-cpu-smu-helper, refusing values outside contract limits."""
    action = _choice_field(
        payload, "action", ("apply-live", "apply-qam-scale", "install-boot", "disable-boot")
    )
    if action == "disable-boot":
        return {"action": action}, [action]
    freq_low, freq_high = CONTRACT["cpu_frequency_mhz"]
    scale_low, scale_high = CONTRACT["cpu_scale"]
    temp_low, temp_high = CONTRACT["cpu_temperature_c"]
    frequency = _int_field(payload, "frequency", freq_low, freq_high)
    scale = _int_field(payload, "scale", scale_low, scale_high)
    temperature = _int_field(payload, "temperature", temp_low, temp_high)
    if frequency % CONTRACT["cpu_frequency_step_mhz"]:
        raise WriteError(
            "frequency must be a multiple of %d MHz" % CONTRACT["cpu_frequency_step_mhz"]
        )
    estimate = CONTRACT["estimated_vid"](frequency, scale)
    if estimate is not None and estimate > CONTRACT["cpu_vid_limit_mv"]:
        raise WriteError(
            "estimated VID ~%d mV exceeds the %d mV ceiling" % (estimate, CONTRACT["cpu_vid_limit_mv"])
        )
    return {
        "action": action,
        "frequency": frequency,
        "scale": scale,
        "temperature": temperature,
        "estimated_vid": estimate,
    }, [action, str(frequency), str(scale), str(temperature)]


def _cu_masks_for_target(current_masks: list[int], target: int) -> list[int]:
    """Reach a CU count by adding or removing WGPs deterministically.

    Filling enables the lowest still-disabled WGP; draining removes the highest
    enabled one. Both orders are stable, so two operators on the same snapshot
    always plan the same change.
    """
    masks = list(current_masks)
    count = _cu_active_count(masks)
    while count < target:
        for row in range(4):
            for wgp in range(5):
                bit = 1 << wgp
                if not masks[row] & bit:
                    masks[row] |= bit
                    count += 2
                    break
            else:
                continue
            break
        else:
            break
    while count > target:
        for row in range(3, -1, -1):
            for wgp in range(4, -1, -1):
                bit = 1 << wgp
                if masks[row] & bit:
                    masks[row] &= ~bit
                    count -= 2
                    break
            else:
                continue
            break
        else:
            break
    return masks


def plan_cu_write(payload: dict) -> tuple[dict, list[str]]:
    """Typed mapping onto bc250-cu-helper; the CU count is the only number accepted."""
    op = _choice_field(payload, "op", ("set", "table", "enable-all", "stock-dispatch"))
    persist = payload.get("persist", False)
    if not isinstance(persist, bool):
        raise WriteError("persist must be true or false")
    targets = list(
        range(CONTRACT["cu_target"][0], CONTRACT["cu_target"][1] + 1, CONTRACT["cu_target_step"])
    )
    if op in {"enable-all", "stock-dispatch"}:
        if persist:
            raise WriteError("persist only applies to set and table")
        argv = ["--yes", "enable", "all"] if op == "enable-all" else ["--yes", "stock-dispatch"]
        return {"op": op}, argv

    if op == "set":
        target = _int_field(payload, "cu", CONTRACT["cu_target"][0], CONTRACT["cu_target"][1])
        if target not in targets:
            raise WriteError("cu must be one of %s" % ", ".join(str(t) for t in targets))
    else:
        masks = _validate_cu_masks(payload.get("masks"))
        if masks is None:
            raise WriteError("masks must be four integers 0..31")
        if _cu_active_count(masks) not in targets:
            raise WriteError(
                "that table routes %d CUs; only %s are allowed"
                % (_cu_active_count(masks), ", ".join(str(t) for t in targets))
            )

    live = json.loads(read_cu_state())
    current = _validate_cu_masks(live.get("live", {}).get("masks"))
    if current is None:
        # No Quick Access snapshot yet: the staged manager reads the dispatch
        # registers directly, so its four verified rows are the hardware truth
        # and a legitimate confirm-basis. Anything less stays a guess, refused.
        rows = (live.get("dashboard") or {}).get("rows")
        if isinstance(rows, list) and len(rows) == 4:
            current = _validate_cu_masks([row.get("mask") for row in rows])
        if current is None:
            raise WriteError(
                "live CU topology is unavailable or stale; read the staged backend "
                "dashboard or publish a snapshot from Desktop/Game Mode - the web "
                "write refuses to guess",
                409,
            )

    requested = _cu_masks_for_target(current, target) if op == "set" else list(masks)

    enabled: list[str] = []
    disabled: list[str] = []
    for row, mask in enumerate(requested):
        se, sh = divmod(row, 2)
        for wgp in range(5):
            token = "%d.%d.%d" % (se, sh, wgp)
            bit = 1 << wgp
            if mask & bit and not current[row] & bit:
                enabled.append(token)
            elif current[row] & bit and not mask & bit:
                disabled.append(token)

    # Removal first: an interrupted transition must never leave more CUs routed
    # than the table the operator confirmed (same order as Quick Access).
    operations: list[list[str]] = []
    if disabled:
        operations.append(["--yes", "disable-wgp", *disabled])
    if enabled:
        operations.append(["--yes", "enable-wgp", *enabled])
    if persist:
        operations.append(["--yes", "write-service-table"])
    if not operations:
        return {
            "op": op,
            "masks": requested,
            "active_cus": _cu_active_count(requested),
            "unchanged": True,
        }, []
    if len(operations) == 1:
        argv = operations[0]
    else:
        argv = ["batch", json.dumps(operations)]
    return {
        "op": op,
        "masks": requested,
        "from_masks": current,
        "from_cus": _cu_active_count(current),
        "active_cus": _cu_active_count(requested),
        "enabled_wgps": enabled,
        "disabled_wgps": disabled,
        "persist": persist,
    }, argv


WRITE_PLANNERS = {
    "fan": plan_fan_write,
    "gpu": plan_gpu_write,
    "cpu": plan_cpu_write,
    "cu": plan_cu_write,
}


def _run_helper(kind: str, argv: list[str], timeout: float) -> dict:
    """One exact-path sudo invocation; argv was built by a planner, never by the client."""
    helper = HELPERS[kind]
    started = time.time()
    try:
        proc = subprocess.run(
            ["sudo", "-n", helper, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "outcome": "timeout", "detail": "helper did not answer within %ss" % timeout}
    except OSError as exc:
        return {"ok": False, "outcome": "unreachable", "detail": repr(exc)}
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    # A non-zero exit is the helper's own safety refusal (identity check, staged
    # backend missing, read-back mismatch). It is a legitimate answer, so it is
    # reported as `rejected` with its sentence, not as an HTTP failure.
    detail = stderr or stdout or "helper produced no output"
    ok = proc.returncode == 0
    result: dict[str, object] = {
        "ok": bool(ok),
        "outcome": "applied" if ok else "rejected",
        "detail": detail,
        "helper": helper,
        "exit_code": proc.returncode,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if not ok and ("password is required" in stderr.lower() or "not in the sudoers file" in stderr.lower()):
        result["hint"] = (
            "sudo refused this helper: add the exact-path NOPASSWD rule from "
            "frontends/web/README.md to /etc/sudoers.d/bc250-web"
        )
    return result


def _refused(kind: str, payload: dict, outcome: str, error: WriteError) -> tuple[int, dict]:
    """A refused write still belongs in the audit trail.

    "The operator tried to route 42 CUs and the server said no" is exactly the
    row an operator asks about later, so refusals are recorded even though no
    helper was invoked.
    """
    body = {"ok": False, "outcome": outcome, "detail": error.detail}
    _audit_write(kind, {k: v for k, v in payload.items() if k not in {"confirm", "expected_state"}}, body)
    return error.code, body


def apply_write(kind: str, payload: dict) -> tuple[int, dict]:
    """One write: gate -> type check -> stale check -> sudo helper -> audit."""
    if not ENABLE_WRITE:
        return 503, {"ok": False, "outcome": "disabled", "detail": "writes disabled (BC250_WEB_ENABLE_WRITE)"}
    if not TOKEN:
        # "Authenticated" is the entire authorization model for a network
        # listener, so an unauthenticated server never elevates at all.
        return 503, {"ok": False, "outcome": "disabled", "detail": "writes require BC250_WEB_TOKEN to be set"}
    if payload.get("confirm") is not True:
        return 400, {"ok": False, "outcome": "confirm-required", "detail": "confirm must be true"}
    try:
        _check_stale(kind, payload)
    except WriteError as exc:
        return _refused(kind, payload, "stale", exc)
    try:
        summary, argv = WRITE_PLANNERS[kind](payload)
    except WriteError as exc:
        return _refused(kind, payload, "rejected-by-server", exc)
    except (TypeError, ValueError) as exc:  # defensive: planners must not leak 500s
        return _refused(kind, payload, "rejected-by-server", WriteError(str(exc)))

    if kind == "cu" and not argv:
        unchanged = {
            "ok": True,
            "outcome": "unchanged",
            "detail": "requested table already live",
            "helper": HELPERS[kind],
            "exit_code": 0,
        }
        _audit_write(kind, summary, unchanged)
        return 200, dict(unchanged, request=summary)

    if kind == "fan":
        stdin_text, session_lines = argv
        result = _run_helper_fan(stdin_text)
        result["session"] = session_lines
    else:
        timeout = 200.0 if kind in {"cpu", "cu"} else 60.0
        result = _run_helper(kind, argv, timeout)
        result["argv"] = [os.path.basename(HELPERS[kind]), *argv]
    result["request"] = summary
    _cache_invalidate("fans", "gpu", "cpu", "cu", "capabilities")
    _audit_write(kind, summary, result)
    return 200, result


def _run_helper_fan(stdin_text: str) -> dict:
    helper = FAN_HELPER
    started = time.time()
    try:
        proc = subprocess.run(
            ["sudo", "-n", helper], input=stdin_text, capture_output=True, text=True, timeout=30, check=False
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "outcome": "timeout", "detail": "fan helper did not answer within 30s"}
    except OSError as exc:
        return {"ok": False, "outcome": "unreachable", "detail": repr(exc)}
    lines = [line for line in (proc.stdout or "").splitlines() if line.strip() not in ("READY", "BYE", "")]
    rejected = [line for line in lines if line.startswith("ERR")]
    accepted = [line for line in lines if line.startswith("OK")]
    stderr = (proc.stderr or "").strip()
    detail = " | ".join(rejected or accepted or lines) or stderr or "fan helper produced no response"
    ok = proc.returncode == 0 and bool(accepted) and not rejected
    result: dict[str, object] = {
        "ok": bool(ok),
        "outcome": "applied" if ok else "rejected",
        "detail": detail,
        "helper": helper,
        "exit_code": proc.returncode,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
    if not ok and "password is required" in (proc.stderr or "").lower():
        result["hint"] = (
            "sudo refused the fan helper: add the exact-path NOPASSWD rule from "
            "frontends/web/README.md to /etc/sudoers.d/bc250-web"
        )
    return result


PROFILES_ACTIONS = {"profiles-export", "profiles-preview", "profiles-import"}


def _new_stage_id() -> str:
    """16 hex characters, generated here and named back to the client."""
    seed = "%s|%s|%s" % (time.time(), os.getpid(), threading.get_ident())
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:PREVIEW_ID_LENGTH]


def _prune_staged() -> None:
    """Stage files are single-use; keep the newest few and drop the oldest."""
    try:
        names = [name for name in os.listdir(PROFILES_DIR) if name.endswith(".json")]
    except OSError:
        return
    if len(names) <= MAX_STAGED_BUNDLES:
        return
    entries = []
    for name in names:
        try:
            entries.append((os.path.getmtime(os.path.join(PROFILES_DIR, name)), name))
        except OSError:
            continue
    entries.sort()
    for _, name in entries[: len(entries) - MAX_STAGED_BUNDLES]:
        try:
            os.unlink(os.path.join(PROFILES_DIR, name))
        except OSError:
            pass


def _discard(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _stage_bundle(payload: dict, prefix: str) -> tuple[str, bytes]:
    """Validate what the client sent, then write one 0600 stage file.

    `bundle_text` (preferred) is the file the operator picked, verbatim: a
    bundle's own sha256 covers its exact payload, so re-serialising a parsed
    object is not safe - `1.0` becomes `1`, and the CLI would then reject the
    file the desktop itself produced. The object form stays accepted for
    programmatic clients, but it is re-serialised here, never by the caller.

    The name is generated here and only ever handed back to the client, so a
    request can name nothing this server did not write in this directory.
    """
    raw_text = payload.get("bundle_text")
    if raw_text is not None and not isinstance(raw_text, str):
        raise WriteError("bundle_text must be the bundle's JSON text")
    if isinstance(raw_text, str):
        try:
            encoded = raw_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise WriteError("bundle_text is not valid text: %s" % exc) from exc
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise WriteError("bundle exceeds the %d byte limit" % MAX_BUNDLE_BYTES)
        try:
            bundle = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise WriteError("bundle_text is not json: %s" % exc) from exc
        if not isinstance(bundle, dict):
            raise WriteError("bundle_text must contain a JSON object")
    else:
        bundle = payload.get("bundle")
    if not isinstance(bundle, dict):
        raise WriteError("bundle must be a JSON object")
    if bundle.get("application") != BUNDLE_APPLICATION:
        raise WriteError("bundle application must be %s" % BUNDLE_APPLICATION)
    if not isinstance(bundle.get("config"), dict) or not isinstance(bundle.get("profiles"), dict):
        raise WriteError("bundle is missing its config or profiles section")
    if not isinstance(raw_text, str):
        try:
            encoded = json.dumps(bundle, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise WriteError("bundle is not serialisable: %s" % exc) from exc
        if len(encoded) > MAX_BUNDLE_BYTES:
            raise WriteError("bundle exceeds the %d byte limit" % MAX_BUNDLE_BYTES)
    _prune_staged()
    path = os.path.join(PROFILES_DIR, "%s%s.json" % (prefix, _new_stage_id()))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
    except OSError as exc:
        raise WriteError("could not stage the bundle: %s" % exc) from exc
    return path, encoded


def _stage_id_of(path: str) -> str:
    return os.path.basename(path)[len("prev-"):-len(".json")]


def _staged_path(preview_id: object) -> str:
    """Resolve a preview id to its staged file, or refuse."""
    if not isinstance(preview_id, str) or len(preview_id) != PREVIEW_ID_LENGTH:
        raise WriteError(
            "preview_id must be the %d characters the preview returned" % PREVIEW_ID_LENGTH
        )
    if any(char not in "0123456789abcdef" for char in preview_id):
        raise WriteError("preview_id must be hexadecimal")
    path = os.path.join(PROFILES_DIR, "prev-%s.json" % preview_id)
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise WriteError("that preview is no longer staged (%s)" % exc.strerror, 409) from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise WriteError("staged preview must be a regular file", 409)
    return path


def _bundle_result(code: int, cli_payload: object, stderr: str, extra: dict) -> dict:
    result = dict(extra)
    if code == 0:
        spoken = (
            json.dumps(cli_payload, ensure_ascii=False, default=str)[:400]
            if cli_payload is not None
            else "cli accepted"
        )
        result.update({"ok": True, "outcome": "applied", "detail": spoken, "cli": cli_payload})
    else:
        result.update(
            {
                "ok": False,
                "outcome": "rejected",
                "detail": stderr or str(cli_payload) or ("cli exit %d" % code),
            }
        )
    result["exit_code"] = code
    return result


def _profiles_export(payload: dict) -> tuple[int, dict]:
    name = "exp-%s.json" % _new_stage_id()
    path = os.path.join(PROFILES_DIR, name)
    code, cli_payload, stderr = _profiles_cli(["export", path])
    raw, _problem = _read_bounded_file(path, MAX_BUNDLE_BYTES + 4096)
    document = None
    if raw is not None:
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            document = None
    extra: dict[str, object] = {"filename": name}
    if raw is not None:
        extra.update(
            {
                "size_bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bundle": document,
                # Byte-exact text, so the file the browser saves is the file the
                # repository wrote and the checksum inside it still verifies.
                "bundle_text": raw.decode("utf-8", "replace"),
            }
        )
    if code != 0:
        _discard(path)
    result = _bundle_result(code, cli_payload, stderr, extra)
    summary: dict[str, object] = {"op": "export", "filename": name}
    if isinstance(document, dict):
        summary["config_keys"] = len(document.get("config") or {})
        summary["profile_sections"] = len(document.get("profiles") or {})
    _audit_write("profiles-export", summary, result)
    _cache_invalidate("profiles")
    return 200, result


def _profiles_preview(payload: dict) -> tuple[int, dict]:
    path, encoded = _stage_bundle(payload, "prev-")
    code, cli_payload, stderr = _profiles_cli(["preview", path])
    result = _bundle_result(
        code, cli_payload, stderr, {"preview_id": _stage_id_of(path), "size_bytes": len(encoded)}
    )
    if code != 0:
        _discard(path)
    else:
        # The confirm modal shows old -> new for a settings replace, so the
        # comparison is computed here against the same snapshot the caller's
        # expected_state was checked against.
        current = json.loads(LOCAL_READS["profiles"]())
        incoming = cli_payload if isinstance(cli_payload, dict) else {}
        have = set(current.get("config_keys") or [])
        want = set(incoming.get("config_keys") or [])
        result["diff"] = {
            "config_keys_now": len(have),
            "config_keys_incoming": len(want),
            "added": sorted(want - have)[:60],
            "removed": sorted(have - want)[:60],
            "sections_now": current.get("profile_sections") or [],
            "sections_incoming": incoming.get("profile_sections") or [],
        }
    _audit_write(
        "profiles-preview",
        {"op": "preview", "preview_id": _stage_id_of(path), "size_bytes": len(encoded)},
        result,
    )
    return 200, result


def _profiles_import(payload: dict) -> tuple[int, dict]:
    path = _staged_path(payload.get("preview_id"))
    wanted = payload.get("checksum")
    if not isinstance(wanted, str) or not 8 <= len(wanted) <= 128:
        raise WriteError("checksum is required: import the preview this panel displayed")
    code, cli_payload, stderr = _profiles_cli(["preview", path])
    if code != 0:
        raise WriteError(
            "the staged bundle no longer validates: %s" % (stderr or "cli exit %d" % code), 409
        )
    found = cli_payload.get("checksum") if isinstance(cli_payload, dict) else None
    if not found or not hmac.compare_digest(str(found), str(wanted)):
        raise WriteError("the staged bundle is not the one this panel previewed", 409)
    code, cli_payload, stderr = _profiles_cli(["import", path, "--yes"], timeout=90.0)
    result = _bundle_result(code, cli_payload, stderr, {"preview_id": _stage_id_of(path)})
    if code == 0:
        _discard(path)  # single use: the same preview can never be imported twice
    summary: dict[str, object] = {"op": "import", "preview_id": _stage_id_of(path)}
    if isinstance(cli_payload, dict):
        summary["backup"] = cli_payload.get("backup")
    _audit_write("profiles-import", summary, result)
    _cache_invalidate("profiles")
    return 200, result


def apply_profiles(kind: str, payload: dict) -> tuple[int, dict]:
    """Profiles export/preview/import: the CLI repository does all the work.

    These write the user's own app data - the same files the desktop writes -
    so there is no sudo hop and no helper. Import keeps every guarantee the
    repository already enforces (sha256, schema, transactional replace, an
    automatic backup under backups/), and the server adds two of its own: the
    import must name a preview this server produced, and that staged bundle's
    checksum must still match what the preview reported - so what the modal
    displayed is what lands, and it lands once.
    """
    if not ENABLE_WRITE:
        return 503, {"ok": False, "outcome": "disabled", "detail": "writes disabled (BC250_WEB_ENABLE_WRITE)"}
    if not TOKEN:
        return 503, {"ok": False, "outcome": "disabled", "detail": "writes require BC250_WEB_TOKEN to be set"}
    if payload.get("confirm") is not True:
        return 400, {"ok": False, "outcome": "confirm-required", "detail": "confirm must be true"}
    try:
        os.makedirs(PROFILES_DIR, mode=0o700, exist_ok=True)
    except OSError as exc:
        return 500, {"ok": False, "outcome": "error", "detail": repr(exc)}
    try:
        _check_stale("profiles", payload)
    except WriteError as exc:
        return _refused(kind, payload, "stale", exc)
    try:
        if kind == "profiles-export":
            return _profiles_export(payload)
        if kind == "profiles-preview":
            return _profiles_preview(payload)
        return _profiles_import(payload)
    except WriteError as exc:
        return _refused(kind, payload, "rejected-by-server", exc)


def _audit_write(kind: str, summary: dict, result: dict) -> None:
    with _lock:
        _audit.append(
            {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "kind": kind,
                "request": summary,
                "ok": bool(result.get("ok")),
                "outcome": result.get("outcome"),
                "detail": str(result.get("detail", ""))[:400],
                "exit_code": result.get("exit_code"),
            }
        )


# --------------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "bc250-web/3"

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        # Liveness probes (curl -I, monitor shells) deserve headers-only
        # answers instead of a 501; no body, no state, no auth needed.
        self.send_response(200)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _authorized(self, query, header_only: bool = False) -> bool:
        if not TOKEN:
            return True
        if self.headers.get("X-Auth", "") == TOKEN:
            return True
        return not header_only and query.get("token", [""])[0] == TOKEN

    def _json(self, code: int, payload: object) -> None:
        self._send(code, _json_body(payload), "application/json")

    def _local_read(self, kind: str) -> bytes:
        """Local reads carry the hash of their own confirmed projection.

        The panel echoes that hash back as `expected_state` on the next write, so
        "I confirmed these old values" is a fact the server can check instead of
        a claim it has to take on faith.
        """
        body = LOCAL_READS[kind]()
        surface = READ_SURFACE.get(kind)
        if surface is None:
            return body
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(data, dict):
            data["_state_hash"] = _state_hash(surface, data)
            return _json_body(data)
        return body

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/api/health":
            self._json(200, {"ok": True, "auth": "token" if TOKEN else "none"})
            return
        if path.startswith("/api/"):
            command = path[len("/") + len("api/"):].strip("/")
            if not self._authorized(query):
                self._json(401, {"error": "unauthorized"})
                return
            if command in READ_COMMANDS:
                self._send(200, run_cli(command), "application/json")
                return
            if command in LOCAL_READS:
                try:
                    self._send(200, self._local_read(command), "application/json")
                except Exception as exc:  # a broken sysfs node must not kill the request
                    self._json(500, {"error": repr(exc)})
                return
            self._json(404, {"error": "unknown command", "known": sorted(READ_COMMANDS) + sorted(LOCAL_READS)})
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
        kind = parsed.path[len("/api/"):] if parsed.path.startswith("/api/") else ""
        if kind not in WRITE_PLANNERS and kind not in PROFILES_ACTIONS:
            self._json(404, {"error": "unknown write endpoint",
                             "known": sorted(WRITE_PLANNERS) + sorted(PROFILES_ACTIONS)})
            return
        query = parse_qs(parsed.query)
        if not self._authorized(query, header_only=True):
            self._json(401, {"error": "unauthorized", "detail": "writes require the X-Auth header"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            length = 0
        # A 2 MiB bundle is also a JSON string inside the envelope once, and JSON
        # escaping can expand it, so the profiles routes get twice the limit and
        # the real bound is still checked on the decoded bundle bytes.
        max_body = (MAX_BUNDLE_BYTES * 2 + 8192) if kind in PROFILES_ACTIONS else 64 * 1024
        if length <= 0 or length > max_body:
            self._json(400, {"ok": False, "detail": "body must be a small JSON object (max %d)" % max_body})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"ok": False, "detail": "invalid json"})
            return
        if not isinstance(payload, dict):
            self._json(400, {"ok": False, "detail": "body must be a JSON object"})
            return
        if kind in PROFILES_ACTIONS:
            code, result = apply_profiles(kind, payload)
        else:
            code, result = apply_write(kind, payload)
        self._json(code, result)

    def log_message(self, *args) -> None:
        pass


if __name__ == "__main__":
    if ENABLE_WRITE and not TOKEN:
        print("BC250_WEB_ENABLE_WRITE is set but BC250_WEB_TOKEN is empty: writes stay closed.")
    print(
        "bc250 web frontend on http://%s:%d (token: %s, writes: %s, contract: %s)"
        % (BIND, PORT, "on" if TOKEN else "OFF", "on" if ENABLE_WRITE else "off", CONTRACT["source"])
    )
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
