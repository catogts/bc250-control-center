"""One OptiScaler launch option for the whole Steam library (asked on Reddit).

With it every installed game already lets Proton load OptiScaler, so the
client only copies files into a new game. dxgi=n,b is harmless without
OptiScaler: Proton falls back to its own dxgi.dll.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from test_steam_launch_options import LOCALCONFIG, _proc  # noqa: E402

from bc250cc.infrastructure.steam_launch_options import (  # noqa: E402
    SteamConfigError,
    add_dll_override_to_library,
    installed_steam_games,
    read_launch_options,
)


def _library(tmp_path: Path, localconfig: str = LOCALCONFIG) -> tuple[Path, Path]:
    steam = tmp_path / ".local/share/Steam"
    config = steam / "userdata/1094057967/config"
    config.mkdir(parents=True)
    (config / "localconfig.vdf").write_text(localconfig, encoding="utf-8")
    second = tmp_path / "games/SteamLibrary"
    (steam / "steamapps").mkdir(parents=True)
    (second / "steamapps").mkdir(parents=True)
    (steam / "steamapps/libraryfolders.vdf").write_text(
        f'"libraryfolders"\n{{\n\t"0"\n\t{{\n\t\t"path"\t\t"{steam}"\n\t}}\n'
        f'\t"1"\n\t{{\n\t\t"path"\t\t"{second}"\n\t}}\n}}\n', encoding="utf-8")
    manifests = {
        steam / "steamapps/appmanifest_990080.acf": ("990080", "Hogwarts Legacy"),
        second / "steamapps/appmanifest_814380.acf": ("814380", "Sekiro"),
        second / "steamapps/appmanifest_2358720.acf": ("2358720", "Black Myth: Wukong"),
        steam / "steamapps/appmanifest_1493710.acf": ("1493710", "Proton Experimental"),
        steam / "steamapps/appmanifest_1628350.acf": ("1628350", "Steam Linux Runtime 3.0 (sniper)"),
    }
    for path, (appid, name) in manifests.items():
        path.write_text(f'"AppState"\n{{\n\t"appid"\t\t"{appid}"\n\t"name"\t\t"{name}"\n}}\n', encoding="utf-8")
    return tmp_path, config / "localconfig.vdf"


def test_every_library_is_read_and_steams_own_tools_are_skipped(tmp_path):
    home, _config = _library(tmp_path)
    assert [game["appid"] for game in installed_steam_games(home)] == ["2358720", "990080", "814380"]


def test_every_game_gets_the_option_and_keeps_what_it_had(tmp_path):
    home, config = _library(tmp_path)
    result = add_dll_override_to_library("dxgi", home=home, proc=_proc(tmp_path, "bash"))
    text = config.read_text(encoding="utf-8")
    assert read_launch_options(text, "990080") == 'WINEDLLOVERRIDES="dxgi=n,b" mangohud %command%'
    assert read_launch_options(text, "814380") == 'WINEDLLOVERRIDES="dxgi=n,b" %command%'
    # Never started, so no block yet: one is added where Steam keeps them.
    assert read_launch_options(text, "2358720") == 'WINEDLLOVERRIDES="dxgi=n,b" %command%'
    assert result["changed"] == 3 and result["games"] == 3 and len(result["backups"]) == 1
    assert Path(result["backups"][0]).read_text(encoding="utf-8") == LOCALCONFIG
    # Everything outside the apps block is untouched.
    assert '"990080"\t\t"not an app block"' in text

    again = add_dll_override_to_library("dxgi", home=home, proc=_proc(tmp_path / "p2", "bash"))
    assert again["changed"] == 0 and again["already"] == 3 and again["backups"] == []


def test_steam_must_be_closed(tmp_path):
    home, config = _library(tmp_path)
    with pytest.raises(RuntimeError, match="Close Steam"):
        add_dll_override_to_library("dxgi", home=home, proc=_proc(tmp_path, "steamwebhelper"))
    assert config.read_text(encoding="utf-8") == LOCALCONFIG


def test_a_profile_it_cannot_read_is_left_alone(tmp_path):
    home, config = _library(tmp_path, LOCALCONFIG.replace('"apps"', "apps"))
    add_dll_override_to_library("dxgi", home=home, proc=_proc(tmp_path, "bash"))
    assert config.read_text(encoding="utf-8") == LOCALCONFIG.replace('"apps"', "apps")


def test_no_games_says_so(tmp_path):
    home, _config = _library(tmp_path)
    for manifest in home.rglob("appmanifest_*.acf"):
        manifest.unlink()
    with pytest.raises(RuntimeError, match="No installed Steam game"):
        add_dll_override_to_library("dxgi", home=home, proc=_proc(tmp_path, "bash"))


def test_the_dll_name_is_checked():
    with pytest.raises(ValueError):
        add_dll_override_to_library("dxgi;rm", home=Path("/nonexistent"))
    assert SteamConfigError


def test_the_button_confirms_once_and_reports_the_count(monkeypatch):
    from PyQt6.QtWidgets import QDialog

    from bc250cc.infrastructure import steam_launch_options
    from frontends.desktop.pages import gpu_governor
    from frontends.desktop.pages.gpu_governor import GpuGovernorPage

    shown, recorded = [], []

    class Dialog:
        def __init__(self, title, message, summary=(), **_):
            shown.append((title, dict(summary)))

        def exec(self):
            return QDialog.DialogCode.Accepted

    monkeypatch.setattr(gpu_governor, "ConfirmDialog", Dialog)
    monkeypatch.setattr(steam_launch_options, "installed_steam_games", lambda: [{"appid": "1"}, {"appid": "2"}])
    monkeypatch.setattr(GpuGovernorPage, "_record_preparation_result",
                        staticmethod(lambda _page, _title, message: recorded.append(message)))
    calls = []
    page = type("Page", (), {
        "_add_fsr4_steam_option_to_library": GpuGovernorPage._add_fsr4_steam_option_to_library,
        "controller": type("C", (), {"gestionar_fsr4_bc250": lambda self, action: calls.append(action) or {"changed": 2, "already": 0}})(),
        "_run_backend_action": lambda self, operation, success, *_a, **_k: success(operation()),
    })()
    page._add_fsr4_steam_option_to_library(dialog_parent=None)

    assert shown[0][0] == "Add the launch option to every Steam game"
    assert shown[0][1]["Games"] == "2"
    assert calls == ["steam_option_all"]
    assert recorded == ["Added to 2 Steam games; 0 already had it."]
