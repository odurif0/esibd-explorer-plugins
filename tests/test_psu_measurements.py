"""Exercise grouped PSU reads through the real ctypes call boundary."""

import ctypes
import logging
import threading
import types
from pathlib import Path

import pytest

import test_psu_plugin_behavior as helpers


@pytest.fixture(params=[f"psu_{suffix}" for suffix in "abcde"])
def setup(request, monkeypatch):
    path = Path(__file__).resolve().parents[1] / request.param / "psu_plugin.py"
    monkeypatch.setattr(helpers, "PLUGIN_PATH", path)
    module = helpers._load_module()
    cls = module._get_psu_driver_class()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.connected = True
    driver._transport_poisoned = False
    driver.thread_lock = threading.Lock()
    driver.logger = logging.getLogger("psu-measurements-regression")
    driver.port = 0
    driver.err_dict = {}
    calls = []
    frames = []
    response_status = [0]
    pointer = ctypes.POINTER

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, pointer(driver.WIN_BOOL))
    def get_enable(port, enabled):
        calls.append("GetDeviceEnable")
        enabled[0] = True
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, pointer(driver.WIN_BOOL), pointer(driver.WIN_BOOL))
    def get_outputs(port, positive, negative):
        calls.append("GetPSUEnable")
        positive[0] = negative[0] = True
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ctypes.c_uint, *([pointer(ctypes.c_double)] * 3))
    def get_data(port, channel, voltage, current, dropout):
        calls.append(("GetPSUData", channel))
        frame = len(frames) + 1
        frames.append(frame)
        voltage[0], current[0], dropout[0] = frame, frame / 1000.0, frame + 10.0
        return response_status[0]

    driver.psu_dll = types.SimpleNamespace(
        COM_HVPSU2D_GetDeviceEnable=get_enable,
        COM_HVPSU2D_GetPSUEnable=get_outputs,
        COM_HVPSU2D_GetPSUData=get_data,
    )
    channels = [types.SimpleNamespace(real=True, enabled=True, active=True, monitor=float("nan"),
                                     value=0., channel_number=lambda ch=ch: ch) for ch in (0, 1)]
    parent = types.SimpleNamespace(getChannels=lambda: channels, poll_timeout_s=0.5)
    controller = module.PSUController(parent)
    controller.device = driver
    controller.initialized = True
    return controller, driver, calls, response_status


def test_live_readbacks_use_four_dll_calls_and_consistent_frames(setup):
    controller, _driver, calls, _status = setup
    result = controller._read_live_readbacks(timeout_s=0.5)
    assert calls == ["GetDeviceEnable", "GetPSUEnable", ("GetPSUData", 0), ("GetPSUData", 1)]
    assert result["values"] == {0: 1.0, 1: 2.0}
    assert result["current_values"] == {0: 0.001, 1: 0.002}
    assert result["dropout_values"] == {0: 11.0, 1: 12.0}
    controller._apply_live_readbacks(result)
    assert controller.dropout_values == result["dropout_values"]


def test_grouped_read_rejects_error_response(setup):
    _controller, driver, calls, status = setup
    status[0] = -11
    with pytest.raises(RuntimeError):
        driver.get_channel_measurements(0, timeout_s=0.5)
    assert calls == [("GetPSUData", 0)]


def test_scalar_getters_remain_available(setup):
    _controller, driver, calls, _status = setup
    assert driver.get_channel_measured_voltage(0, timeout_s=0.5) == 1.0
    assert driver.get_channel_measured_current(1, timeout_s=0.5) == 0.002
    assert calls == [("GetPSUData", 0), ("GetPSUData", 1)]
