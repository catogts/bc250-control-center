"""A way back after a SteamOS update removes Control Center.

A SteamOS update replaces the whole root filesystem, and with it everything
pacman installed there: this application, its PyQt6 runtime and its helpers.
Only ``/home`` survives. So on SteamOS the application keeps a small
reinstaller in the user's data folder and a menu entry that starts it in a
terminal. When the next SteamOS update has removed Control Center, the entry
is still in the Desktop Mode menu.

The reinstaller reads the latest release, downloads its Arch package, checks
it against the SHA-256 GitHub publishes, and installs it with pacman inside the
same read-only switch the in-app updater uses. Writing these two files needs
no network and no root; only running the reinstaller does.

Only the package needs it. A copy installed with ``install-local.sh`` lives in
``~/.local`` and survives the update; installing the package next to it would
leave two copies behind one menu entry, the one in ``~/.local`` winning.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from bc250cc.infrastructure.persistence.config_paths import app_data_dir, xdg_data_home
from bc250cc.infrastructure.steamos_readonly import STEAMOS_PASSWORD_GUARD

logger = logging.getLogger(__name__)

RESCUE_SCRIPT_NAME = "reinstall-after-steamos-update.sh"
RESCUE_DESKTOP_ID = "io.github.movacx.bc250-control-center-reinstall.desktop"
LATEST_RELEASE_API = "https://api.github.com/repos/movacx/bc250-control-center/releases/latest"

# Runs as root with the verified package as $1: writable root, pacman keyring
# and databases (a SteamOS update resets them), the package with whatever
# dependencies it needs, then the protection back on.
_ROOT_REINSTALL = r"""set -u
bc250_package="$1"
bc250_restore=0
if steamos-readonly status 2>&1 | grep -Eqi '(^|[^a-z])enabled'; then
  echo "Temporarily disabling SteamOS read-only protection."
  steamos-readonly disable || exit 70
  bc250_restore=1
fi
pacman-key --init
pacman-key --populate archlinux holo 2>/dev/null || pacman-key --populate
pacman -Syy --noconfirm
pacman -U --noconfirm -- "$bc250_package"
bc250_rc=$?
if [ "$bc250_restore" = 1 ]; then
  echo "Restoring SteamOS read-only protection."
  steamos-readonly enable || { echo "ERROR: run: sudo steamos-readonly enable"; [ "$bc250_rc" -eq 0 ] && bc250_rc=71; }
fi
exit "$bc250_rc"
"""

_PICK_PACKAGE = r"""import json, sys
release = json.load(open(sys.argv[1], encoding="utf-8"))
for asset in release.get("assets") or ():
    name = str(asset.get("name") or "")
    url = str(asset.get("browser_download_url") or "")
    digest = str(asset.get("digest") or "")
    if name.startswith("bc250-control-center") and name.endswith(".pkg.tar.zst") and "/" not in name:
        sha = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
        print(name, url, sha)
        break
"""


def rescue_script() -> str:
    """The reinstaller, as the user runs it from the Desktop Mode menu."""
    root = _ROOT_REINSTALL.replace("'", "'\"'\"'")
    pick = _PICK_PACKAGE.replace("'", "'\"'\"'")
    return f"""#!/usr/bin/env bash
# BC250 Control Center: reinstall after a SteamOS update removed it.
# Written by Control Center into your home folder, which SteamOS updates keep.
set -u
bc250_done() {{ echo; read -r -p "Press Enter to close." _; exit "$1"; }}
echo "== Reinstall BC250 Control Center after a SteamOS update =="
command -v steamos-readonly >/dev/null 2>&1 || {{ echo "This reinstaller is for SteamOS."; bc250_done 64; }}
{STEAMOS_PASSWORD_GUARD}
bc250_require_password
bc250_work="$(mktemp -d)"
trap 'rm -rf -- "$bc250_work"' EXIT
echo "Reading the latest release from GitHub..."
curl --fail --location --silent --show-error -H "Accept: application/vnd.github+json" \\
  -o "$bc250_work/release.json" {LATEST_RELEASE_API} || {{ echo "ERROR: GitHub could not be reached."; bc250_done 1; }}
read -r bc250_name bc250_url bc250_sha < <(python3 -c '{pick}' "$bc250_work/release.json")
case "${{bc250_url:-}}" in https://*) ;; *) echo "ERROR: The latest release has no Arch package."; bc250_done 1 ;; esac
[ -n "${{bc250_sha:-}}" ] || {{ echo "ERROR: The package has no published SHA-256; it will not be installed."; bc250_done 1; }}
echo "Downloading $bc250_name..."
curl --fail --location --silent --show-error -o "$bc250_work/$bc250_name" "$bc250_url" || {{ echo "ERROR: The download failed."; bc250_done 1; }}
printf '%s  %s\\n' "$bc250_sha" "$bc250_work/$bc250_name" | sha256sum -c - || {{ echo "ERROR: The package does not match its published SHA-256."; bc250_done 1; }}
sudo bash -c '{root}' bc250-reinstall "$bc250_work/$bc250_name" || {{ echo "ERROR: The reinstall did not finish; the output above says why."; bc250_done 1; }}
echo
echo "OK: BC250 Control Center is installed again."
echo "Open it and run Prepare dependencies: the SteamOS update also removed the tools it had prepared."
bc250_done 0
"""


def rescue_desktop_entry(script: Path) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=Reinstall BC250 Control Center\n"
        "Comment=Use this after a SteamOS update removed BC250 Control Center\n"
        f'Exec=bash "{script}"\n'
        "Terminal=true\n"
        "Icon=system-software-update\n"
        "Categories=System;\n"
    )


def is_steamos(os_release: Path = Path("/etc/os-release")) -> bool:
    try:
        text = os_release.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        key, _sep, value = line.partition("=")
        if key.strip() == "ID":
            return value.strip().strip("\"'") == "steamos"
    return False


def runs_from_package(installed_at: Path | None = None) -> bool:
    """Whether this copy is the package in ``/usr`` rather than a script install."""
    location = installed_at if installed_at is not None else Path(__file__).resolve()
    return location.is_relative_to("/usr") and not location.is_relative_to("/usr/local")


def ensure_steamos_rescue(
    *,
    os_release: Path = Path("/etc/os-release"),
    data_dir: Path | None = None,
    applications_dir: Path | None = None,
    installed_at: Path | None = None,
) -> bool:
    """Write the reinstaller and its menu entry on SteamOS; True if written."""
    if not is_steamos(os_release) or not runs_from_package(installed_at):
        return False
    folder = (data_dir if data_dir is not None else app_data_dir()) / "steamos-rescue"
    applications = applications_dir if applications_dir is not None else xdg_data_home() / "applications"
    script = folder / RESCUE_SCRIPT_NAME
    entry = applications / RESCUE_DESKTOP_ID
    wanted = {script: rescue_script(), entry: rescue_desktop_entry(script)}
    changed = False
    try:
        for path, content in wanted.items():
            try:
                if path.read_text(encoding="utf-8") == content:
                    continue
            except OSError:
                pass
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_text(content, encoding="utf-8")
            os.replace(temporary, path)
            changed = True
        script.chmod(0o755)
    except OSError:
        logger.debug("Could not write the SteamOS reinstaller", exc_info=True)
        return False
    return changed
