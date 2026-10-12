"""Exercise grouped PSU reads through the real ctypes call boundary."""


import pytest



def test_live_readbacks_use_four_dll_calls_and_consistent_frames(psu_dll_case):
    controller, _driver, calls, _status = psu_dll_case
    result = controller._read_live_readbacks(timeout_s=0.5)
    assert calls == ["GetDeviceEnable", "GetPSUEnable", ("GetPSUData", 0), ("GetPSUData", 1)]
    assert result["values"] == {0: 1.0, 1: 2.0}
    assert result["current_values"] == {0: 0.001, 1: 0.002}
    assert result["dropout_values"] == {0: 11.0, 1: 12.0}
    controller._apply_live_readbacks(result)
    assert controller.dropout_values == result["dropout_values"]


def test_grouped_read_rejects_error_response(psu_dll_case):
    _controller, driver, calls, status = psu_dll_case
    status[0] = -11
    with pytest.raises(RuntimeError):
        driver.get_channel_measurements(0, timeout_s=0.5)
    assert calls == [("GetPSUData", 0)]


def test_scalar_getters_remain_available(psu_dll_case):
    _controller, driver, calls, _status = psu_dll_case
    assert driver.get_channel_measured_voltage(0, timeout_s=0.5) == 1.0
    assert driver.get_channel_measured_current(1, timeout_s=0.5) == 0.002
    assert calls == [("GetPSUData", 0), ("GetPSUData", 1)]
