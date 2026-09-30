"""Shipped profile names follow the interface language, whatever was saved.

Reported by the Russian community moderator (2026-09-29): after a few updates
the CPU profile cards showed "Punto medio" and "Max seguro" in a Russian
panel, and Reset brought the Spanish names back. The shipped names were the
Spanish translation sources, "Max seguro" had no translation in 27 languages,
and the editor and Reset used the raw source, so saving froze it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QSettings

from frontends.desktop.core.preferences import application_settings
from frontends.desktop.i18n import COMPLETE_LOCALES, set_language, shipped_name, tr
from frontends.desktop.pages.cpu_control_integration import (
    _load_profiles,
    _persist_profile,
)
from frontends.desktop.pages.cpu_control_view import (
    DEFAULT_CPU_PROFILES,
    CpuProfileCard,
    cpu_profile_name,
)
from frontends.desktop.pages.fans import DEFAULT_FAN_PROFILES, load_fan_profiles
from frontends.desktop.pages.gpu_governor import (
    _load_custom_gpu_profile,
    _save_custom_gpu_profile,
)
from frontends.desktop.pages.gpu_governor_view import (
    DEFAULT_PROFILES,
    ProfileCardEditable,
    gpu_profile_name,
)

LOCALES = Path(__file__).resolve().parents[3] / "frontends" / "desktop" / "i18n" / "locales"


@pytest.fixture
def russian():
    set_language("ru")
    yield
    set_language("en")


def test_every_shipped_profile_name_is_a_key_in_every_locale():
    names = {profile.name for profile in (*DEFAULT_CPU_PROFILES, *DEFAULT_PROFILES, *DEFAULT_FAN_PROFILES)}
    for path in LOCALES.glob("*.json"):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        assert names <= set(catalog), (path.name, sorted(names - set(catalog)))


def test_the_cpu_tiers_are_translated_everywhere_but_english():
    for profile in DEFAULT_CPU_PROFILES:
        untranslated = [lang for lang in COMPLETE_LOCALES if lang != "en" and tr(profile.name, lang) == profile.name]
        assert untranslated == [], profile.name
    assert [tr(profile.name, "en") for profile in DEFAULT_CPU_PROFILES] == [
        "Average board", "Mid point", "Safe maximum",
    ]


@pytest.mark.parametrize(
    ("key", "saved"),
    [
        ("board_average", "Placa media"),
        ("mid_point", "Punto medio"),
        ("safe_maximum", "Max seguro"),
        ("safe_maximum", "Max seguro UI"),
        ("mid_point", "Средняя точка"),
        ("board_average", "Середня плата"),
        ("safe_maximum", "máximo seguro"),
    ],
)
def test_a_default_saved_in_any_language_is_the_shipped_name_again(key, saved):
    shipped = next(profile.name for profile in DEFAULT_CPU_PROFILES if profile.key == key)
    assert cpu_profile_name(key, saved) == shipped


def test_an_owners_own_name_is_kept_as_typed():
    assert cpu_profile_name("mid_point", "  Mi perfil  ") == "Mi perfil"
    assert gpu_profile_name("gaming", "Ночной") == "Ночной"
    assert shipped_name("", "Quiet") == "Quiet"


def test_the_moderators_saved_slots_load_in_the_panel_language(russian):
    settings = application_settings()
    for index, (key, name) in enumerate(
        (("board_average", "Placa media"), ("mid_point", "Punto medio"), ("safe_maximum", "Max seguro"))
    ):
        settings.setValue(f"cpu/profile_{index}/key", key)
        settings.setValue(f"cpu/profile_{index}/name", name)
        settings.setValue(f"cpu/profile_{index}/frequency", 3600 + index)
    settings.sync()

    profiles = _load_profiles()
    assert [profile.name for profile in profiles] == ["Average board", "Mid point", "Safe maximum"]
    # The owner's numbers are kept; only the name is read back as the source.
    assert [profile.frequency_mhz for profile in profiles] == [3600, 3601, 3602]
    assert [profile.shown_name() for profile in profiles] == [
        "Средняя плата", "Средняя точка", "Безопасный максимум",
    ]

    _persist_profile(profiles[2])
    assert application_settings().value("cpu/profile_2/name") == "Safe maximum"


def test_the_cpu_editor_and_reset_speak_the_panel_language(qtbot, russian):
    default = DEFAULT_CPU_PROFILES[2]
    card = CpuProfileCard(default)
    qtbot.addWidget(card)
    card.set_profile(default.__class__(default.key, "Max seguro", 4000, 1275, 90))

    card.begin_edit()
    assert card._name_edit.text() == "Max seguro"  # a saved legacy name, shown as saved…
    card._restore_default()
    assert card._name_edit.text() == "Безопасный максимум"  # …Reset gives the translation
    card._commit()
    assert card.profile.name == "Safe maximum"
    assert card._name_label.text() == "Безопасный максимум"

    set_language("de")
    card.retranslate()
    assert card._name_label.text() == "Sicheres Maximum"


def test_the_gpu_editor_keeps_a_default_as_its_source(qtbot, russian):
    card = ProfileCardEditable(DEFAULT_PROFILES[0])
    qtbot.addWidget(card)
    card.begin_edit()
    assert card._name_edit.text() == tr("Balanced")
    card._commit()
    assert card.profile.name == "Balanced"


def test_a_gpu_slot_saved_translated_reads_back_as_the_source(tmp_path):
    settings = QSettings(str(tmp_path / "ui.conf"), QSettings.Format.IniFormat)
    profile = {"name": "Сбалансированный", "min": 500, "max": 1500, "frequency": 1500, "voltage": 900}
    _save_custom_gpu_profile(settings, 0, profile)
    assert settings.value("gpu/cyan_profile_0/name") == "Balanced"
    settings.setValue("gpu/cyan_profile_1/name", "Игровой")
    for key, value in (("min", 1000), ("max", 1850), ("frequency", 1850), ("voltage", 1000)):
        settings.setValue(f"gpu/cyan_profile_1/{key}", value)
    assert _load_custom_gpu_profile(settings, 1)["name"] == "Gaming"


def test_fan_profiles_saved_in_another_language_follow_it_again(tmp_path):
    settings = QSettings(str(tmp_path / "ui.conf"), QSettings.Format.IniFormat)
    for index, (key, name) in enumerate((("quiet", "Тихий"), ("balanced", "Equilibrado"), ("maximum", "Full"))):
        settings.setValue(f"fans/profile_{index}/key", key)
        settings.setValue(f"fans/profile_{index}/name", name)
    assert [profile.name for profile in load_fan_profiles(settings)] == ["Quiet", "Balanced", "Full"]
