"""COM_HVPSU2D_GetFanData writes PWM as doubles, whatever the CGC header declares.

2026-10-12: every PSU worker on the lab PC died with 0xC0000374 (heap corruption) right after
its first housekeeping read. The DLL stores PWM/1000.0 into three doubles (24 bytes) while the
header declares WORD PWM[3] (6 bytes), so the native buffer overflowed.
"""
import ctypes
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path

import pytest

from support.psu import load_plugin

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("psu_fan_abi_generator", ROOT / "tools/generate_native_abi.py")
generate_native_abi = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generate_native_abi)

PSU_FOLDERS = ("psu_a", "psu_b", "psu_c", "psu_d", "psu_e")


def test_worker_abi_gives_pwm_a_double_buffer_for_the_verified_dll():
    spec = json.loads((ROOT / "native/worker/src/generated/abi.json").read_text())["psu"]["exports"]["COM_HVPSU2D_GetFanData"]
    assert spec["args"][5] == {"length": 3, "name": "PWM", "pointer": True, "type": "double"}
    assert [arg["type"] for arg in spec["args"][1:5]] == ["BOOL", "BOOL", "WORD", "WORD"]
    for folder in PSU_FOLDERS:
        dll = ROOT / folder / "vendor/runtime/psu/vendor/x64/COM-HVPSU2D.dll"
        assert hashlib.sha256(dll.read_bytes()).hexdigest() == spec["verified_dll_sha256"], folder
    generated = (ROOT / "native/worker/src/generated/psu.rs").read_text()
    assert "*mut u16, *mut u16, *mut f64) -> i32>(b\"COM_HVPSU2D_GetFanData" in generated


def test_generation_refuses_an_unverified_psu_dll(monkeypatch):
    monkeypatch.setattr(generate_native_abi, "PSU_FAN_DATA_DLL_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="Re-verify the COM_HVPSU2D_GetFanData PWM layout"):
        generate_native_abi.declarations("psu", "psu_a")


def test_python_reference_passes_double_pwm_buffers():
    module = load_plugin()
    cls = module._get_psu_driver_class()._PROCESS_CONTROLLER_CLASS
    base = importlib.import_module(cls.__module__).PSUBase
    seen = {}

    def get_fan_data(port, enabled, failed, set_rpm, measured_rpm, pwm):
        seen["pwm"] = type(pwm)
        for index, duty in enumerate((0.25, 0.5, 1.0)):
            pwm[index] = duty
        return 0

    driver = base.__new__(base)
    driver.port = 0
    driver.psu_dll = type("DLL", (), {"COM_HVPSU2D_GetFanData": staticmethod(get_fan_data)})()
    status, *_, pwm = driver.get_fan_data()
    assert status == 0
    assert seen["pwm"] is ctypes.c_double * 3
    assert pwm == [0.25, 0.5, 1.0]
