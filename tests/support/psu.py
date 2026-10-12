"""PSU plugin loading, rigs and the real-ctypes driver fixture shared by the PSU tests."""
from __future__ import annotations

import ctypes
import logging
import threading
from threading import Lock
import types
from types import SimpleNamespace

import numpy as np
import pytest

from support import explorer
from support.psu_fakes import StatefulPSU

FOLDERS = tuple(f"psu_{suffix}" for suffix in "abcde")


def load_plugin(folder: str = "psu_a", module_name: str | None = None):
    """Import a PSU entry point against a fresh fake Explorer."""
    return explorer.load_plugin(f"{folder}/psu_plugin.py", module_name or f"{folder}_plugin_test", runtimes=("psu",))


def snapshot(positive=123., negative=234., enabled=(True, True)):
    return {
        "main_state": {"name": "ST_ON"}, "device_enabled": True,
        "output_enabled": enabled,
        "channels": [dict(channel=ch, enabled=enabled[ch],
                          voltage={"measured_v": voltage, "set_v": 999.})
                     for ch, voltage in enumerate((positive, negative))],
    }


def make_psu(folder="psu_a"):
    module = load_plugin(folder, f"channel_{folder}")
    parent = module.PSUDevice.__new__(module.PSUDevice)
    parent.interval = 1000
    parent.loading = False
    parent.print = lambda *a, **kw: None
    parent.isOn = lambda: True
    parent.channels = [SimpleNamespace(
        id=ch, name=f"{parent.name}_CH{ch}", channelParent=parent,
        real=True, enabled=True, active=True, useMonitors=True, initialized=True,
        value=0., monitor=np.nan, unit="V", readback_status="PSU disconnected.",
        channel_number=lambda ch=ch: ch, getDevice=lambda: parent,
    ) for ch in (0, 1)]
    parent.getChannels = lambda: parent.channels
    controller = parent.controller = module.PSUController(parent)
    controller.device = SimpleNamespace(connected=True, _transport_poisoned=False)
    controller.initialized = True
    # Omit only unrelated toolbars/settings, not production Channel publication.
    controller._sync_status_to_gui = lambda sync_manual_panel=False: module._invoke_gui_callback(
        lambda: controller._update_channel_values(sync_setpoints=sync_manual_panel))
    return module, parent, controller


@pytest.fixture(params=FOLDERS)
def psu_dll_case(request):
    """A PSU controller whose driver goes through the real ctypes call boundary."""
    module = load_plugin(request.param)
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


@pytest.fixture(params=FOLDERS)
def psu_live_rig(request, monkeypatch):
    """A PSU controller on a stateful fake driver, both outputs ON, for live setpoint edits."""
    module = load_plugin(request.param)
    parent = SimpleNamespace(
        name=request.param.upper(), getChannels=lambda: [], isOn=lambda: True,
        startup_timeout_s=1., poll_timeout_s=.1, interlock_monitoring=True,
    )
    controller = module.PSUController(parent)
    controller.lock = Lock()
    controller.initialized = True
    controller.errorCount = 0
    controller._update_state = lambda: None
    controller._sync_status_to_gui = lambda **kw: None
    messages = []
    controller.print = lambda msg, **kw: messages.append(msg)
    device = controller.device = StatefulPSU()
    device.enabled = True
    device.outputs = (True, True)
    device.interlocks = (True, True)
    device.voltages = {0: 100., 1: 240.}
    device.currents = {0: .01, 1: .02}
    # A stale housekeeping cache must not supply the start of a live ramp.
    controller.voltage_setpoint_values = {0: 0., 1: 500.}
    controller.full_range_by_channel = {0: True, 1: True}
    state = dict(output_enabled={0: True, 1: True}, full_range_enabled={0: False, 1: False},
                 voltage_values=dict(device.voltages), current_limit_values=dict(device.currents))
    monkeypatch.setattr(controller._output_cancel, "wait", lambda _: controller._output_cancel.is_set())
    return module, controller, device, state, messages
