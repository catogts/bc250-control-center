"""The dashboard's telemetry button when there is no boot argument to apply.

linux-cachyos-bc250 dropped amdgpu.cs_legacy_8core_metrics on 2026-09-17, so
on current kernels the "Repair BC250 telemetry" button either ran a repair the
kernel ignores or sat disabled. With 8 cores and unreadable metrics the fix is
in the BIOS (MeiMeiDXE v3: "SMU Reporting Patch"), so the button explains it.
"""

from __future__ import annotations

from frontends.desktop.core.error_diagnostics import diagnose_error, diagnosis_for
from frontends.desktop.core.state import DashboardState
from frontends.desktop.i18n import tr
from frontends.desktop.pages import dashboard as dashboard_module
from frontends.desktop.pages.dashboard import DashboardPage


class _Controller:
    pass


def test_without_a_repair_to_apply_the_button_explains_the_bios_setting(qtbot, monkeypatch):
    shown = []

    class Dialog:
        def __init__(self, title, message, **options):
            shown.append((title, message, options))

        def exec(self):
            return 0

    monkeypatch.setattr(dashboard_module, "InfoDialog", Dialog)
    page = DashboardPage(_Controller())
    qtbot.addWidget(page)
    repairs = []
    page.controller.reparar_telemetria_8core = lambda: repairs.append(True)
    page.apply_state(DashboardState(gpu_metrics_layout_mismatch=True, gpu_telemetry_repair_available=False))

    button = page.telemetry_repair_button
    assert not button.isHidden() and button.isEnabled()
    assert button.text() == tr("How to fix BC250 telemetry")
    button.click()

    assert repairs == []
    title, message, options = shown[0]
    assert options["eyebrow"] == "BC250-GPU-003"
    # Plain steps: the recommended BIOS, where to find it and what to set.
    assert "MeiMeiDXE v3" in message and "Firmware (BIOS)" in message
    assert "SMU Reporting Patch to Enabled" in message and "{page}" not in message
    assert title == "GPU readings are wrong with 8 cores"


def test_the_diagnostic_names_the_bios_option_and_not_the_removed_argument():
    diagnosis = diagnosis_for("BC250-GPU-003")
    assert "SMU Reporting Patch" in diagnosis.cause and "MeiMeiDXE v3" in diagnosis.cause
    assert "linux-cachyos-bc250" in diagnosis.action
    assert "cs_legacy_8core_metrics=1" not in diagnosis.action
    # The helper's refusal on such a kernel lands on the same explanation.
    refusal = "This kernel has no amdgpu.cs_legacy_8core_metrics; linux-cachyos-bc250 removed it"
    assert diagnose_error(refusal).code == "BC250-GPU-003"


def _state(parameters: dict, repair: dict) -> DashboardState:
    class Cache:
        def performance(self):
            return {}

        def gpu(self):
            return {"apu_telemetry": {"status": "invalid", "layout_mismatch_suspected": True, "parameters": parameters}}

        def fans(self):
            return {}

        def cu_cache(self):
            return {}

        def cpu_boot_tuning(self):
            return {}

        def events(self, _limit):
            return []

        def pump_fan_fallback(self):
            return 0, "Not detected"

        def tools(self):
            return {"system_setup": {"telemetry": repair}}

    return DashboardState.from_controller(object(), cache=Cache())


def test_a_kernel_without_the_parameter_gets_no_repair_even_from_an_old_helper():
    # An older installed helper still reports the repair as available and a
    # configured argument as waiting for a reboot; the running kernel decides.
    old_helper = {"available": True, "reboot_required": True, "active": False}
    state = _state({"cs_eight_core_map": "N", "cs_legacy_8core_metrics": None}, old_helper)
    assert state.gpu_metrics_layout_mismatch
    assert not state.gpu_telemetry_repair_available
    assert not state.gpu_telemetry_repair_pending


def test_a_kernel_that_still_has_the_parameter_keeps_the_repair():
    state = _state({"cs_legacy_8core_metrics": "N"}, {"available": True})
    assert state.gpu_telemetry_repair_available


def _guide(qtbot, monkeypatch, recommendation: str) -> str:
    shown = []

    class Dialog:
        def __init__(self, title, message, **options):
            shown.append(message)

        def exec(self):
            return 0

    monkeypatch.setattr(dashboard_module, "InfoDialog", Dialog)
    page = DashboardPage(_Controller())
    qtbot.addWidget(page)
    page.apply_state(DashboardState(
        gpu_metrics_layout_mismatch=True,
        gpu_telemetry_repair_available=False,
        gpu_metrics_recommendation=recommendation,
    ))
    page.telemetry_repair_button.click()
    return shown[0]


def test_bazzite_is_told_to_turn_the_smu_patch_off(qtbot, monkeypatch):
    """Bazzite's kernel (7.2.4-ogc3.1) has no cs_* parameters: stock layout only."""
    message = _guide(qtbot, monkeypatch, "disable_smu_reporting_patch")
    assert "Bazzite" in message and "SMU Reporting Patch to Disabled" in message
    assert "SMU Reporting Patch to Enabled" not in message


def test_linux_cachyos_bc250_is_told_to_turn_it_on(qtbot, monkeypatch):
    message = _guide(qtbot, monkeypatch, "enable_smu_reporting_patch")
    assert "SMU Reporting Patch to Enabled" in message and "Bazzite" not in message


def test_an_unknown_layout_gets_both_cases(qtbot, monkeypatch):
    message = _guide(qtbot, monkeypatch, "")
    assert "SMU Reporting Patch to Enabled" in message
    assert "On Bazzite, set SMU Reporting Patch to Disabled" in message


def test_the_state_carries_the_recommendation_for_the_running_kernel():
    state = _state({}, {"available": False})
    assert state.gpu_metrics_recommendation == ""

    class Cache:
        def __getattr__(self, name):
            return {
                "gpu": lambda: {"apu_telemetry": {
                    "status": "invalid", "layout_mismatch_suspected": True,
                    "parameters": {}, "recommendation": "disable_smu_reporting_patch",
                }},
                "events": lambda _limit: [],
                "pump_fan_fallback": lambda: (0, "Not detected"),
            }.get(name, lambda: {})

    state = DashboardState.from_controller(object(), cache=Cache())
    assert state.gpu_metrics_recommendation == "disable_smu_reporting_patch"
