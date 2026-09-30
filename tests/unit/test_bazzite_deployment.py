"""Bazzite reads as waiting for a reboot only for packages Prepare layered."""

from __future__ import annotations

import json
import subprocess

from bc250cc.infrastructure.bazzite_deployment import (
    pending_prepared_packages,
    probe_bazzite_reboot_pending,
)


def _status(*deployments: dict) -> dict:
    return {"deployments": list(deployments)}


def test_a_prepared_package_queued_for_next_boot_is_pending():
    status = _status(
        {"booted": False, "requested-packages": ["dkms", "umr", "htop"]},
        {"booted": True, "requested-packages": []},
    )
    assert pending_prepared_packages(status) == ("dkms", "umr")


def test_an_automatic_update_carrying_the_same_packages_is_not_pending():
    layered = ["dkms", "umr"]
    status = _status(
        {"booted": False, "requested-packages": layered},
        {"booted": True, "requested-packages": layered},
    )
    assert pending_prepared_packages(status) == ()


def test_rollbacks_and_packages_of_the_owners_own_are_ignored():
    rollback = _status(
        {"booted": True, "requested-packages": []},
        {"booted": False, "requested-packages": ["dkms"]},
    )
    assert pending_prepared_packages(rollback) == ()
    own = _status(
        {"booted": False, "requested-packages": ["htop"]},
        {"booted": True, "requested-packages": []},
    )
    assert pending_prepared_packages(own) == ()
    assert pending_prepared_packages({}) == ()
    assert pending_prepared_packages(None) == ()


def _runner(returncode: int, stdout: str):
    def run(*args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout, "")
    return run


def test_the_probe_reads_rpm_ostree_and_fails_closed():
    payload = json.dumps(_status(
        {"booted": False, "requested-packages": ["lm_sensors"]},
        {"booted": True},
    ))
    assert probe_bazzite_reboot_pending(runner=_runner(0, payload)) == {
        "available": True, "pending": True, "packages": ["lm_sensors"],
    }
    for runner in (_runner(1, payload), _runner(0, "not json")):
        assert probe_bazzite_reboot_pending(runner=runner)["pending"] is False

    def missing(*args, **kwargs):
        raise FileNotFoundError("rpm-ostree")

    assert probe_bazzite_reboot_pending(runner=missing)["available"] is False


def test_the_package_list_follows_the_bazzite_preparation_script():
    import re
    from pathlib import Path

    from bc250cc.infrastructure.bazzite_deployment import PREPARED_PACKAGES

    script = (
        Path(__file__).resolve().parents[2]
        / "packaging/common/os-scripts/bazzite/prepare-dependencies.sh"
    ).read_text(encoding="utf-8")
    runtime = re.search(r"runtime_packages=\(\s*(.*?)\)", script, re.S).group(1).split()
    extra = re.search(r'layer_packages "\$\{runtime_packages\[@\]\}" (.*)', script).group(1).split()
    assert PREPARED_PACKAGES == frozenset(runtime + extra)
