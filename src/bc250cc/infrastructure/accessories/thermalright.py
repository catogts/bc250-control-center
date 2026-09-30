"""Thermalright cooler LCDs, driven by TRCC Linux in every session.

A Thermalright cooler with a screen shows nothing by itself: a program on the
board has to render every frame and send it over USB. TRCC Linux is that
program. Installed the usual way it starts from the desktop's autostart, which
Game Mode never runs, so the screen stayed dark there.

This installs the pinned TRCC package for the distribution and adds a small
user service that plays the saved theme without a window. A user service
starts with the session in Desktop and Game Mode alike. TRCC ships the same
idea as an optional extra (packaging/systemd in its repository) that no
package installs; the unit below is the equivalent, owned by Control Center.

SteamOS is left out on purpose: TRCC has no package for it, and its pip route
does not survive a SteamOS update.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path

from ._shared import (
    UNIT_DIRECTORY,
    command_output,
    download_and_verify,
    present_usb_ids,
    shell_header,
    user_service_state,
)

REPOSITORY = "https://github.com/Lexonight1/thermalright-trcc-linux"
VERSION = "9.10.4"
TAG = f"v{VERSION}"
#: The tag's commit, for the external tools manifest.
REVIEWED_REVISION = "72457586c82d879e1864b3fc03f0d3844774e998"
PACKAGE = "trcc-linux"
BINARY = Path("/usr/bin/trcc")

#: Release asset and the SHA-256 GitHub published for it, per package format.
ASSETS = {
    "pacman": (
        "trcc-linux-9.10.4-1-any.pkg.tar.zst",
        "9d412d9712182d429f2c7a82f93a2aa57e89f3c65e781ed739ce9b79bec4317f",
    ),
    "rpm": (
        "trcc-linux-9.10.4-1.fc44.noarch.rpm",
        "3d3ddd915ba625e74b355e1c81231e58886afbc3e443de7791b6fc12616a7719",
    ),
    "deb": (
        "trcc-linux_9.10.4-1_all.deb",
        "bc25685024e7cfafb6a8be9b13a83ac384c3da21443beffc9fc1a2018b558e8c",
    ),
}
#: How each distribution family installs it.
ROUTES = {
    "arch": "pacman",
    "cachyos": "pacman",
    "manjaro": "pacman",
    "fedora": "dnf",
    "bazzite": "rpm-ostree",
    "debian": "apt",
    "ubuntu": "apt",
}
_FORMAT = {"pacman": "pacman", "dnf": "rpm", "rpm-ostree": "rpm", "apt": "deb"}

#: Every device TRCC's own udev rule registers (packaging/udev/99-trcc-lcd.rules).
#: Vendor 0416 alone is Winbond and matches unrelated hardware.
DEVICE_IDS = frozenset({
    "0402:3922", "0416:5302", "0416:5406", "0416:8001",
    "0418:5303", "0418:5304", "87cd:70db",
})

SERVICE = "bc250-thermalright-display.service"
# ``$$`` is systemd's escape for a literal ``$``. TRCC_DEVICE in
# ~/.trcc/ticker.env pins one panel, as TRCC's own ticker allows; otherwise the
# first device ``trcc detect`` lists is used. A panel that is not enumerated
# yet at login is normal, so the unit retries instead of giving up.
UNIT = f"""[Unit]
Description=Thermalright LCD in every session (BC250 Control Center)
Documentation={REPOSITORY}
ConditionPathExists={BINARY}
StartLimitIntervalSec=0

[Service]
Type=simple
Environment=QT_QPA_PLATFORM=offscreen
EnvironmentFile=-%h/.trcc/ticker.env
ExecStart=/bin/sh -c 'key="$${{TRCC_DEVICE:-}}"; [ -n "$$key" ] || key="$$({BINARY} detect 2>/dev/null | grep -oE "[0-9a-fA-F]{{4}}:[0-9a-fA-F]{{4}}" | head -n 1)"; [ -n "$$key" ] || {{ echo "No Thermalright display detected yet" >&2; exit 1; }}; exec {BINARY} display play "$$key"'
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


def route_for(family: str) -> str:
    return ROUTES.get(str(family or "").strip().lower(), "")


def _installed_version(route: str) -> str:
    if route == "pacman":
        answer = command_output(["pacman", "-Q", PACKAGE]).split()
        return answer[1] if len(answer) == 2 else ""
    if route in {"dnf", "rpm-ostree"}:
        return command_output(["rpm", "-q", "--qf", "%{VERSION}-%{RELEASE}", PACKAGE])
    if route == "apt":
        return command_output(["dpkg-query", "-W", "-f", "${Version}", PACKAGE])
    return ""


def _pending_layer() -> bool:
    """Bazzite: installed into the next deployment, not the running one."""
    status = command_output(["rpm-ostree", "status", "--json"], timeout=8)
    if not status:
        return False
    try:
        deployments = json.loads(status).get("deployments") or []
    except ValueError:
        return False
    pending = next((item for item in deployments if not item.get("booted")), None)
    if not pending or deployments.index(pending) != 0:
        return False
    names = list(pending.get("requested-local-packages") or []) + list(pending.get("requested-packages") or [])
    return any(name == PACKAGE or name.startswith(f"{PACKAGE}-") for name in names)


def inventory(family: str) -> dict:
    """Read-only state for the Drivers tab."""
    route = route_for(family)
    devices = present_usb_ids(DEVICE_IDS)
    base = {
        "supported": bool(route),
        "version": VERSION,
        "device": bool(devices),
        "device_id": devices[0] if devices else "",
    }
    if not route:
        return {**base, "state": "unsupported", "installed_version": "", "service": "missing"}
    installed_version = _installed_version(route) if BINARY.exists() else ""
    service = user_service_state(SERVICE)
    if not installed_version:
        state = "reboot-required" if route == "rpm-ostree" and _pending_layer() else "not-installed"
    elif not installed_version.startswith(f"{VERSION}-"):
        state = "update-available"
    elif service == "active":
        state = "active"
    else:
        state = "installed"
    return {**base, "state": state, "installed_version": installed_version, "service": service}


def _enable_service_steps(*, start_now: bool) -> list[str]:
    unit_path = f'"{UNIT_DIRECTORY}/{SERVICE}"'
    steps = [
        f'mkdir -p "{UNIT_DIRECTORY}"',
        f"printf '%s' {shlex.quote(UNIT)} > {unit_path}",
        "systemctl --user daemon-reload",
        f"systemctl --user enable {SERVICE}",
    ]
    if start_now:
        steps += [
            # The desktop autostart and this service would fight over the USB
            # interface; only one process may hold it.
            f"{BINARY} system autostart disable >/dev/null 2>&1 || true",
            f"systemctl --user restart {SERVICE}",
        ]
    return steps


def install_command(family: str) -> str:
    route = route_for(family)
    if not route:
        raise RuntimeError(
            "TRCC has no package for this distribution. Its install guide covers the manual routes."
        )
    asset, sha256 = ASSETS[_FORMAT[route]]
    url = f"{REPOSITORY}/releases/download/{TAG}/{asset}"
    commands = shell_header(f"Thermalright LCD · TRCC Linux {VERSION}")
    commands += download_and_verify(url, asset, sha256)
    package = f'"$bc250_stage/{asset}"'
    reboot = False
    if route == "pacman":
        commands.append(f"sudo pacman -U --needed --noconfirm {package}")
    elif route == "dnf":
        commands.append(f"sudo dnf install -y {package}")
    elif route == "apt":
        # apt reads a local file only through a path, which this is.
        commands.append(f"sudo apt-get install -y {package}")
    else:
        # A layered older copy has to be swapped in one transaction: a plain
        # install next to it fails with "cannot install both".
        commands += [
            f"if rpm -q {PACKAGE} >/dev/null 2>&1; then",
            f"  sudo rpm-ostree uninstall {PACKAGE} --install {package}",
            "else",
            f"  sudo rpm-ostree install {package}",
            "fi",
        ]
        reboot = True
    commands += _enable_service_steps(start_now=not reboot)
    if reboot:
        commands += [
            "echo 'BC250_REBOOT_REQUIRED=1'",
            "echo 'OK: TRCC is in the next deployment. Restart, open the configuration once and save a theme; the display service starts by itself.'",
        ]
    else:
        commands.append(
            "echo 'OK: TRCC installed. Open the configuration once and save a theme; the display then follows it in Desktop and Game Mode.'"
        )
    return "\n".join(commands)


def remove_command(family: str) -> str:
    route = route_for(family)
    if not route:
        raise RuntimeError("TRCC was not installed by Control Center on this distribution.")
    commands = shell_header("Thermalright LCD · remove TRCC Linux")
    commands += [
        f"systemctl --user disable --now {SERVICE} 2>/dev/null || true",
        f'rm -f "{UNIT_DIRECTORY}/{SERVICE}"',
        "systemctl --user daemon-reload",
    ]
    remove = {
        "pacman": f"sudo pacman -R --noconfirm {PACKAGE}",
        "dnf": f"sudo dnf remove -y {PACKAGE}",
        "apt": f"sudo apt-get remove -y {PACKAGE}",
        "rpm-ostree": f"sudo rpm-ostree uninstall {PACKAGE}",
    }[route]
    query = "pacman -Q" if route == "pacman" else "dpkg-query -W" if route == "apt" else "rpm -q"
    commands.append(f"if {query} {PACKAGE} >/dev/null 2>&1; then {remove}; fi")
    if route == "rpm-ostree":
        commands.append("echo 'BC250_REBOOT_REQUIRED=1'")
    commands.append("echo 'OK: TRCC removed. Your own themes in ~/.trcc-user were kept.'")
    return "\n".join(commands)


def configure_argv() -> list[str]:
    """TRCC's window, with the display service paused while it holds the USB."""
    if not (shutil.which("trcc") or BINARY.exists()):
        raise RuntimeError("TRCC is not installed yet.")
    if user_service_state(SERVICE) == "missing":
        return [str(BINARY), "gui"]
    script = (
        f"systemctl --user stop {SERVICE} 2>/dev/null; "
        f"{BINARY} gui; "
        # The window may switch its own autostart back on; the service is
        # what keeps the display running, in both modes.
        f"{BINARY} system autostart disable >/dev/null 2>&1; "
        f"systemctl --user start {SERVICE} 2>/dev/null"
    )
    return ["/bin/sh", "-c", script]
