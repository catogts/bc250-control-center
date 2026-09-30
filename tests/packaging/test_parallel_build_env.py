"""Source builds must use every core, but never override the user's choice."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "packaging/common/os-scripts"
COMMON = SCRIPTS / "common/common.sh"


def _build_env(**overrides):
    env = {key: value for key, value in os.environ.items()
           if key not in {"MAKEFLAGS", "CMAKE_BUILD_PARALLEL_LEVEL"}}
    env.update(overrides)
    result = subprocess.run(
        ["bash", "-c",
         f"set -Eeuo pipefail; source {COMMON}; export_parallel_build_env >/dev/null; "
         'printf "%s|%s" "$CMAKE_BUILD_PARALLEL_LEVEL" "$MAKEFLAGS"'],
        check=True, capture_output=True, text=True, env=env,
    )
    return result.stdout.split("|")


def test_builds_default_to_every_online_core():
    jobs = subprocess.run(["nproc"], capture_output=True, text=True).stdout.strip()

    assert _build_env() == [jobs, f"-j{jobs}"]


def test_values_the_user_already_set_are_kept():
    assert _build_env(MAKEFLAGS="-j3", CMAKE_BUILD_PARALLEL_LEVEL="3") == ["3", "-j3"]


def test_every_aur_build_path_asks_for_parallel_jobs():
    aur = (SCRIPTS / "common/aur.sh").read_text(encoding="utf-8")

    for function in ("ensure_aur_helper", "install_aur_package_direct", "install_aur_package"):
        body = aur.split(f"{function}() {{", 1)[1].split("\n}\n", 1)[0]
        assert "export_parallel_build_env" in body, function


def test_alpine_keeps_apk_progress_visible():
    for name in ("prepare-dependencies.sh", "prepare-fan-pwm.sh"):
        assert "--no-progress" not in (SCRIPTS / "alpine" / name).read_text(encoding="utf-8")
