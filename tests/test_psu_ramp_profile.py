"""PSU software ramp: tunable step profile and one verified limit read per ramp."""
from __future__ import annotations

import ctypes
from threading import Event

import pytest

from test_psu_live_setpoints import rig  # noqa: F401  (pytest fixture)
from test_psu_measurements import setup as dll_case  # noqa: F401  (pytest fixture)


def _limit_dll(driver):
    """Real driver over fake ctypes entry points; counts limit reads."""
    reads, writes, ranges = [], [], []
    ptr = ctypes.POINTER

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ctypes.c_uint, ptr(ctypes.c_double), ptr(ctypes.c_double))
    def get_voltage(port, channel, value, limit):
        reads.append(channel)
        value[0], limit[0] = 0., 500.
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ctypes.c_uint, ctypes.c_double)
    def set_voltage(port, channel, value):
        writes.append((channel, value))
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, driver.WIN_BOOL, driver.WIN_BOOL)
    def set_range(port, ch0, ch1):
        ranges.append((ch0, ch1))
        return 0

    driver.psu_dll.COM_HVPSU2D_GetPSUSetOutputVoltage = get_voltage
    driver.psu_dll.COM_HVPSU2D_SetPSUOutputVoltage = set_voltage
    driver.psu_dll.COM_HVPSU2D_SetPSUFullRange = set_range
    return reads, writes


def test_limit_is_read_once_per_cached_block_and_still_bounds_setpoints(dll_case):
    _controller, driver, _calls, _status = dll_case
    reads, writes = _limit_dll(driver)

    with driver.cached_setpoint_limits():
        for value in (100., 200., 700.):
            driver.set_channel_voltage(0, value, timeout_s=1.)
        driver.set_channel_voltage(1, 50., timeout_s=1.)
    assert reads == [0, 1]
    assert writes == [(0, 100.), (0, 200.), (0, 500.), (1, 50.)], "the cached limit must still clamp"

    driver.set_channel_voltage(0, 300., timeout_s=1.)
    driver.set_channel_voltage(0, 300., timeout_s=1.)
    assert reads == [0, 1, 0, 0], "outside a block every setpoint re-reads its limit"


def test_range_change_discards_cached_limits(dll_case):
    _controller, driver, _calls, _status = dll_case
    reads, _writes = _limit_dll(driver)
    with driver.cached_setpoint_limits():
        driver.set_channel_voltage(0, 100., timeout_s=1.)
        driver.set_output_full_range(True, False, timeout_s=1.)
        driver.set_channel_voltage(0, 200., timeout_s=1.)
    assert reads == [0, 0]


def test_ramp_uses_the_configured_step(rig):
    module, controller, device, _state, _messages = rig
    controller.controllerParent.ramp_step_v = 25.
    assert controller._ramp_channel_voltage(device, 0, 100., timeout_s=1., cancel=Event(), start_v=0.)
    assert [call[2] for call in device.calls] == [25., 50., 75., 100.]


@pytest.mark.parametrize("bad", [0., -5., float("nan"), "x", None])
def test_invalid_profile_falls_back_to_the_defaults(rig, bad):
    module, controller, _device, _state, _messages = rig
    controller.controllerParent.ramp_step_v = bad
    controller.controllerParent.ramp_step_interval_s = bad
    assert controller._ramp_profile() == (module._PSU_VOLTAGE_RAMP_STEP_V, module._PSU_VOLTAGE_RAMP_STEP_S)
