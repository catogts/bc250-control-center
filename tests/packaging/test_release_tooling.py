"""What a release needs to agree on, and the tools that produce it.

The gallery went blank at #compute-monitor after compute-units-monitor.png
was replaced by monitor-1.png: the page named its pictures by hand and the
workflow copied them by hand, so a new screenshot in the folder never showed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
GALLERY = (ROOT / "assets/gallery/index.html").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github/workflows/deploy-gallery.yml").read_text(encoding="utf-8")


def test_every_screenshot_the_gallery_names_exists():
    named = re.findall(r"\['[\w-]+', '([^']+\.(?:png|jpe?g|webp))', '", GALLERY)
    assert named, "the gallery lists no screenshots"
    missing = [name for name in named if not (ROOT / "assets/screenshots" / name).is_file()]
    assert missing == []


def test_the_workflow_publishes_the_whole_folder_and_its_listing():
    assert "photos.json" in WORKFLOW and "photos.json" in GALLERY
    assert "source.iterdir()" in WORKFLOW
    # No picture is named in the workflow any more: the folder is the list.
    assert not re.search(r"assets/screenshots/[\w-]+\.png", WORKFLOW)


def test_links_shared_before_the_rename_keep_working():
    assert "'compute-monitor': 'monitor'" in GALLERY
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    keys = set(re.findall(r"\['([\w-]+)', '[^']+', '", GALLERY))
    for anchor in re.findall(r"gallery/#([\w-]+)", readme):
        assert anchor in keys, anchor


def test_every_place_that_carries_the_version_agrees():
    package = json.loads((ROOT / "integrations/decky/bc250-quick-access/package.json").read_text(encoding="utf-8"))
    assert package["version"] == VERSION
    metainfo = (ROOT / "packaging/common/io.github.movacx.bc250-control-center.metainfo.xml").read_text(encoding="utf-8")
    assert re.search(r'<release version="([^"]+)"', metainfo).group(1) == VERSION
    pkgbuild = (ROOT / "packaging/arch/aur/PKGBUILD").read_text(encoding="utf-8")
    assert re.search(r"^pkgver=(\S+)$", pkgbuild, re.M).group(1).startswith(VERSION)


@pytest.mark.parametrize("script", [
    "packaging/scripts/build-release.sh",
    "packaging/arch/aur/publish-aur.sh",
])
def test_release_scripts_are_valid_bash(script):
    result = subprocess.run(["bash", "-n", str(ROOT / script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_the_aur_script_pushes_only_when_asked():
    script = (ROOT / "packaging/arch/aur/publish-aur.sh").read_text(encoding="utf-8")
    push = script.index("git -C \"$WORK\" push")
    assert "if ((PUSH)); then" in script[:push]
    assert "PUSH=0" in script
