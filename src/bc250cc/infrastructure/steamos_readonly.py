"""SteamOS' read-only root, shown and switched on request.

SteamOS mounts its root filesystem read-only so an OS update can replace it
whole. Installing packages (the application's own, its dependencies, drivers)
needs it writable, which is ``steamos-readonly disable``. Workflows that need
it already unlock and restore it themselves; this is the explicit switch a
person asked for, the SteamOS counterpart of Bazzite's ``mitigations=off``
card.

Nothing here runs as root. The probe reads ``steamos-readonly status`` as the
desktop user, and the command is a closed terminal workflow.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Callable

_ACTIONS = frozenset({"disable", "enable", "status"})

# Before the first sudo on SteamOS: the deck account has no password until the
# owner sets one, and sudo then fails every prompt without saying why. An
# account that sudo already trusts without a password passes straight through.
STEAMOS_PASSWORD_GUARD = r"""bc250_require_password() {
  sudo -n true 2>/dev/null && return 0
  bc250_account="$(id -un 2>/dev/null || true)"
  case "$(passwd -S "$bc250_account" 2>/dev/null | awk '{print $2}')" in
    NP|L|LK|NL)
      echo "ERROR: The $bc250_account account has no password yet, so sudo cannot authorize this change."
      echo "Set one first: open Konsole in Desktop Mode, run passwd, then try again."
      exit 77 ;;
  esac
}"""


def _state_from(output: str) -> str:
    text = str(output or "")
    if re.search(r"(^|[^A-Za-z])disabled([^A-Za-z]|$)", text, re.IGNORECASE):
        return "disabled"
    if re.search(r"(^|[^A-Za-z])enabled([^A-Za-z]|$)", text, re.IGNORECASE):
        return "enabled"
    return "unknown"


def probe_steamos_readonly(
    *,
    which: Callable[[str], str | None] = shutil.which,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, object]:
    """``enabled`` (protected), ``disabled`` (writable) or ``unknown``."""
    if not which("steamos-readonly"):
        return {"available": False, "state": "unavailable"}
    try:
        result = runner(
            ("steamos-readonly", "status"),
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            env={**os.environ, "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": True, "state": "unknown"}
    return {"available": True, "state": _state_from(f"{result.stdout}\n{result.stderr}")}


def build_steamos_readonly_command(action: str) -> str:
    """A closed workflow around ``steamos-readonly``; it never reboots."""
    action = str(action or "").strip().lower()
    if action not in _ACTIONS:
        raise ValueError("Unsupported SteamOS read-only action.")
    commands = [
        "set -u",
        'command -v steamos-readonly >/dev/null 2>&1 || { echo "ERROR: steamos-readonly was not found; this workflow is for SteamOS."; exit 64; }',
    ]
    if action == "status":
        commands.append("steamos-readonly status")
        return "\n".join(commands)
    commands.append(STEAMOS_PASSWORD_GUARD)
    commands.append("bc250_require_password")
    if action == "disable":
        commands.extend((
            "sudo steamos-readonly disable || exit $?",
            "steamos-readonly status",
            'echo "OK: the SteamOS root filesystem is writable. A SteamOS update turns the protection back on and replaces what was installed into it."',
        ))
    else:
        commands.extend((
            "sudo steamos-readonly enable || exit $?",
            "steamos-readonly status",
            'echo "OK: SteamOS read-only protection is on again."',
        ))
    return "\n".join(commands)
