
import pytest

from bc250cc.domain.telemetry import voltage_mv
from bc250cc.infrastructure.apu_telemetry import collect_apu_telemetry, dpm_reading
from bc250cc.infrastructure.sistema_repository import SistemaRepository
from frontends.desktop.core.state import _voltage_millivolts


@pytest.mark.parametrize("raw", [40058, 45344, 4063234, 960000, 0.039, -1, float("nan"), float("inf"), True])
def test_corrupt_voltage_is_not_reinterpreted_as_another_unit(raw):
    assert voltage_mv(raw) is None
    assert _voltage_millivolts(raw) == 0


def test_live_voltage_validation_has_no_overdrive_fallback(tmp_path):
    sensor = tmp_path / "hwmon/hwmon1"
    sensor.mkdir(parents=True)
    (sensor / "name").write_text("amdgpu")
    (sensor / "in0_label").write_text("vddgfx")
    (sensor / "in0_input").write_text("45344")
    (tmp_path / "pp_od_clk_voltage").write_text("OD_VDDC:\n0: 45344mV *\n")
    repo = object.__new__(SistemaRepository)
    repo.hwmons = [("amdgpu", sensor)]
    repo._gpu_busy_percent = lambda _gpu: None
    assert repo.voltaje_chip("amdgpu", "vddgfx") is None
    assert repo._gpu_device_evidence(tmp_path).voltage_actual is None


@pytest.mark.parametrize("payload", ["0: 0Mhz *", "0: 2Mhz *", "0: 17411Mhz *", "0: 1000Mhz", "0: 800Mhz *\n1: 900Mhz *"])
def test_invalid_memory_clock_never_becomes_live_telemetry(tmp_path, payload):
    path = tmp_path / "pp_dpm_mclk"
    path.write_text(payload)
    assert dpm_reading(path)["value"] is None
    assert dpm_reading(path)["status"] == "invalid"
    assert object.__new__(SistemaRepository)._parse_dpm_actual(payload) is None


def test_eight_core_capture_preserves_raw_evidence_and_suspects_layout(tmp_path):
    files = {
        "sys/bus/pci/devices/0000:01:00.0/vendor": "0x1002",
        "sys/bus/pci/devices/0000:01:00.0/device": "0x13fe",
        "sys/bus/pci/devices/0000:01:00.0/pp_dpm_mclk": "0: 0Mhz *",
        "sys/bus/pci/devices/0000:01:00.0/hwmon/hwmon1/name": "amdgpu",
        "sys/bus/pci/devices/0000:01:00.0/hwmon/hwmon1/in0_label": "vddgfx",
        "sys/bus/pci/devices/0000:01:00.0/hwmon/hwmon1/in0_input": "45344",
        "sys/bus/pci/devices/0000:01:00.0/hwmon/hwmon1/temp1_input": "0",
        "sys/module/amdgpu/parameters/cs_legacy_8core_metrics": "N",
    }
    for cpu in range(16):
        files[f"sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id"] = "0"
        files[f"sys/devices/system/cpu/cpu{cpu}/topology/core_id"] = str(cpu % 8)
    for name, payload in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    report = collect_apu_telemetry(root=tmp_path)
    assert report["physical_cores"] == 8
    assert report["layout_mismatch_suspected"] is True
    assert report["metrics"]["voltage"]["raw"] == "45344"
    assert report["metrics"]["voltage"]["value"] is None
    assert report["metrics"]["temperature"]["status"] == "invalid"
    assert (tmp_path / "sys/module/amdgpu/parameters/cs_legacy_8core_metrics").read_text() == "N"


def test_aliased_fclk_is_not_advertised_as_an_independent_clock(tmp_path):
    device = tmp_path / "sys/bus/pci/devices/test"
    device.mkdir(parents=True)
    for name, content in {"vendor": "0x1002", "device": "0x13fe", "pp_dpm_mclk": "0: 875Mhz *", "pp_dpm_fclk": "0: 875Mhz *"}.items():
        (device / name).write_text(content)
    report = collect_apu_telemetry(root=tmp_path)
    assert report["metrics"]["mclk"]["value"] == 875
    assert report["metrics"]["fclk"]["status"] == "unverified"
    assert report["metrics"]["fclk"]["value"] is None
    assert report["metrics"]["uclk"]["status"] == "missing"


# ------------------------------------------------ 8-core layout vs SMU patch
#
# Readings from a real board (2026-09-29): MeiMeiDXE v3 with Core Unlock on
# All Cores, on linux-cachyos-bc250 7.2.8-1.219. With the BIOS "SMU Reporting
# Patch" off, every SMU-table reading was garbage; with it on, all were right.


def _board(tmp_path, *, cores=8, parameters=(), vddgfx="799", edge="53000", sclk="1: 1000Mhz *"):
    device = "sys/bus/pci/devices/0000:01:00.0"
    files = {
        f"{device}/vendor": "0x1002",
        f"{device}/device": "0x13fe",
        f"{device}/pp_dpm_sclk": sclk,
        f"{device}/hwmon/hwmon1/name": "amdgpu",
        f"{device}/hwmon/hwmon1/in0_label": "vddgfx",
        f"{device}/hwmon/hwmon1/in0_input": vddgfx,
        f"{device}/hwmon/hwmon1/temp1_input": edge,
        **{f"sys/module/amdgpu/parameters/{name}": value for name, value in dict(parameters).items()},
    }
    for cpu in range(cores * 2):
        files[f"sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id"] = "0"
        files[f"sys/devices/system/cpu/cpu{cpu}/topology/core_id"] = str(cpu % cores)
    for name, payload in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload)
    return collect_apu_telemetry(root=tmp_path)


MASTAG_KERNEL = {"cs_eight_core_map": "N", "cs_metrics_cache_ms": "25", "cs_activity_cache_ms": "25"}
GARBAGE = {"vddgfx": "44587", "edge": "0", "sclk": "0: 350Mhz\n1: 4764Mhz *\n2: 2230Mhz"}


def test_an_unpatched_smu_on_linux_cachyos_bc250_is_caught(tmp_path):
    """The detector used to require cs_legacy_8core_metrics, removed on 2026-09-17."""
    report = _board(tmp_path, parameters=MASTAG_KERNEL, **GARBAGE)
    assert report["layout_mismatch_suspected"] is True
    assert report["metrics_decode"] == "patched-eight-core"
    assert report["recommendation"] == "enable_smu_reporting_patch"
    assert report["metrics"]["sclk"]["status"] == "invalid"


def test_the_same_board_with_the_smu_patch_reads_clean(tmp_path):
    report = _board(tmp_path, parameters=MASTAG_KERNEL)
    assert report["layout_mismatch_suspected"] is False
    assert report["recommendation"] is None
    assert (report["metrics"]["voltage"]["value"], report["metrics"]["temperature"]["value"]) == (799, 53.0)
    assert report["metrics"]["sclk"]["value"] == 1000


def test_a_kernel_reading_only_the_stock_layout_is_told_to_turn_the_patch_off(tmp_path):
    report = _board(tmp_path, **GARBAGE)
    assert report["metrics_decode"] == "stock"
    assert report["recommendation"] == "disable_smu_reporting_patch"


def test_six_cores_are_never_a_layout_mismatch(tmp_path):
    assert _board(tmp_path, cores=6, parameters=MASTAG_KERNEL, **GARBAGE)["layout_mismatch_suspected"] is False


@pytest.mark.parametrize(("raw", "expected"), [("350", 350), ("2230", 2230), ("3882", None), ("4764", None)])
def test_a_gpu_clock_past_any_bc250_range_is_not_a_reading(raw, expected):
    from bc250cc.domain.telemetry import gpu_clock_mhz

    assert gpu_clock_mhz(raw) == expected
