"""Optional case hardware: pieces a BC-250 build may carry beside the board.

Each accessory module pins one upstream release and exposes the same four
things: ``inventory`` (read-only state), ``install_command`` and
``remove_command`` (terminal workflows) and, where it has one, a way to open
its own configuration.
"""

from __future__ import annotations

from pathlib import Path

from . import openlinkhub, thermalright

ACCESSORIES = ("thermalright", "corsair")


def accessory_inventory(family: str, tool_dir: Path) -> dict:
    return {
        "thermalright": thermalright.inventory(family),
        "corsair": openlinkhub.inventory(tool_dir),
    }


def accessory_command(component: str, action: str, *, family: str, tool_dir: Path) -> str:
    """The terminal workflow for ``action`` (``install`` or ``remove``)."""
    if action not in {"install", "remove"}:
        raise ValueError(f"Unsupported accessory action: {action or '--'}")
    if component == "thermalright":
        return (thermalright.install_command if action == "install" else thermalright.remove_command)(family)
    if component == "corsair":
        return (openlinkhub.install_command if action == "install" else openlinkhub.remove_command)(tool_dir)
    raise ValueError(f"Unsupported accessory: {component or '--'}")
