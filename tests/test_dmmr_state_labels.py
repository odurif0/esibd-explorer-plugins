"""Do not turn commented-out CGC states into claimed hardware faults."""
import ctypes
from pathlib import Path
import re

import pytest

from test_dmmr_startup_diagnostics import native, NativeFunction  # noqa: F401


@pytest.mark.parametrize('value,label', [
    (0, 'ST_ON'), (1, 'UNKNOWN_STATE_0x0001'), (2, 'UNKNOWN_STATE_0x0002'),
    (0x8000, 'ST_ERROR'), (0x8001, 'ST_ERR_MODULE'), (0x8002, 'ST_ERR_VSUP'),
    (0x8003, 'UNKNOWN_STATE_0x8003'),
])
def test_real_runtime_preserves_state_code_without_inventing_meaning(native, value, label):
    def state(pointer):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ushort))[0] = value
        return 0
    driver = native.driver
    driver.dll.COM_DMMR_8_GetState = NativeFunction(state)
    driver._configure_dll_signatures()
    assert driver.get_state(timeout_s=.5) == (0, f'0x{value:04X}', label)


def test_main_state_names_match_active_cgc_definitions(native):
    header = Path(__file__).resolve().parents[1] / 'dmmr/vendor/runtime/dmmr/vendor/COM-DMMR-8.h'
    active_names = set(re.findall(r'^#define\s+COM_DMMR_8_(ST_\w+)\s', header.read_text(), re.M))
    assert set(native.driver.MAIN_STATE.values()) == active_names
