"""Behavior checks for the AMX HD controller (HV-AMX-CTRL-4EDH).

Validates the HD-specific adaptation of the high-level controller without real
hardware: ``collect_housekeeping`` must map the HD state encoding
(``STATE_ON = 0x0001``, ``STATE_STANDBY = 0x0000``), the 8-value housekeeping
readback, the indexed oscillator (oscillator 0), and the per-timer snapshot.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import threading
import types
from enum import Enum
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HD_PATH = ROOT / "amx_hd" / "amx_hd_plugin.py"


def _install_esibd_stubs() -> None:
    # Minimal stubs sufficient for the plugin module to import.
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")

    class PARAMETERTYPE(Enum):
        INT = "INT"; FLOAT = "FLOAT"; LABEL = "LABEL"

    class _V:
        def __init__(self, v):
            self.value = v

    class PLUGINTYPE(Enum):
        INPUTDEVICE = _V("INPUTDEVICE")

    class PRINT(Enum):
        WARNING = "WARNING"; ERROR = "ERROR"

    class Parameter:
        VALUE = "Value"; HEADER = "Header"; ADVANCED = "Advanced"
        EVENT = "Event"; TOOLTIP = "Tooltip"; INDICATOR = "Indicator"
        PARAMETER_TYPE = "PARAMETER_TYPE"

    class Channel:
        COLLAPSE = "Collapse"; NAME = "Name"; ACTIVE = "Active"; DISPLAY = "Display"
        REAL = "Real"; ENABLED = "Enabled"; VALUE = "Value"; SCALING = "Scaling"
        MIN = "Min"; MAX = "Max"; OPTIMIZE = "Optimize"

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent

    class ToolButton:
        def __init__(self):
            self.maximum_height = None
            self.minimum_width = None
            self.text = None
            self.checkable = None
            self.auto_raise = None

        def setMaximumHeight(self, h): self.maximum_height = h
        def setMinimumWidth(self, w): self.minimum_width = w
        def setText(self, t): self.text = t
        def setCheckable(self, c): self.checkable = c
        def setAutoRaise(self, a): self.auto_raise = a

    class Device:
        MAXDATAPOINTS = "Max data points"

    class Plugin:
        pass

    def parameterDict(**kw):
        p = {}
        if "value" in kw: p[Parameter.VALUE] = kw["value"]
        if "advanced" in kw: p[Parameter.ADVANCED] = kw["advanced"]
        if "header" in kw: p[Parameter.HEADER] = kw["header"]
        if "toolTip" in kw: p[Parameter.TOOLTIP] = kw["toolTip"]
        if "parameterType" in kw: p[Parameter.PARAMETER_TYPE] = kw["parameterType"]
        p.update(kw)
        return p

    core.PARAMETERTYPE = PARAMETERTYPE; core.PLUGINTYPE = PLUGINTYPE; core.PRINT = PRINT
    core.Channel = Channel; core.DeviceController = DeviceController
    core.ToolButton = ToolButton
    core.Parameter = Parameter; core.parameterDict = parameterDict
    plugins.Device = Device; plugins.Plugin = Plugin
    sys.modules["esibd"] = esibd; sys.modules["esibd.core"] = core; sys.modules["esibd.plugins"] = plugins


def _clear_test_modules() -> None:
    for name in [
        n for n in list(sys.modules)
        if n == "esibd" or n.startswith("esibd.")
        or n.startswith("_esibd_bundled_amx_runtime")
        or n.startswith("_esibd_bundled_amx_hd_runtime")
        or n.startswith("amx_hd_plugin_test")
    ]:
        sys.modules.pop(name, None)


def _load_hd_controller_classes():
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("amx_hd_plugin_test", PLUGIN_HD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    runtime_name = module._bundled_runtime_module_name(PLUGIN_HD_PATH.parent)
    module._get_amx_driver_class()  # ensures the private runtime package is loaded
    ctrl_mod = importlib.import_module(f"{runtime_name}.amx_hd.amx_hd")
    base_mod = importlib.import_module(f"{runtime_name}.amx_hd.amx_hd_base")
    return ctrl_mod._AMXHDController, base_mod.AMXHDBase


def _make_controller(controller_cls):
    """Create an _AMXHDController instance without loading the vendor DLL."""
    ctrl = controller_cls.__new__(controller_cls)
    ctrl.thread_lock = threading.Lock()
    ctrl.logger = logging.getLogger("amx_hd_test")
    ctrl.connected = True
    ctrl._transport_poisoned = False
    ctrl.device_id = "hd_test"
    ctrl.stream = 0
    ctrl.PULSER_LABELS = {0: "timer_0", 1: "timer_1"}
    ctrl._raise_on_status = lambda status, action: None
    return ctrl


def test_collect_housekeeping_maps_hd_state_and_timers(monkeypatch):
    controller_cls, base_cls = _load_hd_controller_classes()
    ctrl = _make_controller(controller_cls)

    # HD GetDeviceState -> (status, main_hex, main_name, dev_hex, dev_names, temp_hex, temp_names)
    monkeypatch.setattr(base_cls, "get_device_state", lambda self: (
        0, "0x1", "STATE_ON", "0x0", ["DEVST_OK"], "0x0", ["TMPST_OK"]
    ))
    # HD GetState -> (status, state_hex, config, state_names)
    monkeypatch.setattr(base_cls, "get_state", lambda self: (0, "0x401", 0, ["ST_ENABLE"]))
    monkeypatch.setattr(base_cls, "get_device_enable", lambda self: (0, True))
    # HD GetHousekeeping -> status + 8 values
    monkeypatch.setattr(base_cls, "get_housekeeping", lambda self: (
        0, 12.0, 11.5, 5.0, 3.3, 3.31, 2.51, 1.2, 42.5
    ))
    monkeypatch.setattr(base_cls, "get_oscillator_period", lambda self, oscillator=0: (0, 49998))
    monkeypatch.setattr(base_cls, "get_timer_count", lambda self: (0, 2))
    monkeypatch.setattr(base_cls, "get_timer_delay", lambda self, t: (0, 3 + t))
    monkeypatch.setattr(base_cls, "get_timer_width", lambda self, t: (0, 100 * (t + 1)))
    monkeypatch.setattr(base_cls, "get_timer_burst", lambda self, t: (0, 16))

    snapshot = ctrl._collect_housekeeping_unlocked()

    # HD state: ON is 0x0001 ("STATE_ON"), not standby.
    assert snapshot["main_state"]["name"] == "STATE_ON"
    assert snapshot["device_enabled"] is True
    assert snapshot["device_state"]["flags"] == ["DEVST_OK"]
    assert snapshot["controller_state"]["flags"] == ["ST_ENABLE"]

    # 8-value housekeeping surfaced.
    assert snapshot["housekeeping"]["volt_12v_v"] == 12.0
    assert snapshot["housekeeping"]["volt_fans_v"] == 11.5
    assert snapshot["housekeeping"]["volt_3v3p_v"] == 3.31
    assert snapshot["housekeeping"]["volt_2v5p_v"] == 2.51
    assert snapshot["housekeeping"]["volt_vc_v"] == 1.2
    assert snapshot["housekeeping"]["temp_cpu_c"] == 42.5

    # Indexed oscillator (oscillator 0): freq = DEF_CLOCK / (period + OSC_OFFSET)
    assert snapshot["oscillator"]["period"] == 49998
    assert snapshot["oscillator"]["frequency_hz"] == 100e6 / (49998 + 2)

    # Timers (not pulsers): count queried at runtime, snapshot keeps "pulsers" key
    # for plugin compatibility.
    pulsers = snapshot["pulsers"]
    assert [p["pulser"] for p in pulsers] == [0, 1]
    assert [p["width_ticks"] for p in pulsers] == [100, 200]
    assert [p["delay_ticks"] for p in pulsers] == [3, 4]
    assert all(p["burst"] == 16 for p in pulsers)


def test_collect_housekeeping_standby_is_not_on(monkeypatch):
    """0x0000 is STATE_STANDBY on HD — the plugin's _state_is_on must not treat it as ON."""
    controller_cls, base_cls = _load_hd_controller_classes()
    ctrl = _make_controller(controller_cls)

    monkeypatch.setattr(base_cls, "get_device_state", lambda self: (
        0, "0x0", "STATE_STANDBY", "0x0", ["DEVST_OK"], "0x0", ["TMPST_OK"]
    ))
    monkeypatch.setattr(base_cls, "get_state", lambda self: (0, "0x0", 0, []))
    monkeypatch.setattr(base_cls, "get_device_enable", lambda self: (0, False))
    monkeypatch.setattr(base_cls, "get_housekeeping", lambda self: (0, 12.0, 11.5, 5.0, 3.3, 3.31, 2.51, 1.2, 40.0))
    monkeypatch.setattr(base_cls, "get_oscillator_period", lambda self, oscillator=0: (0, 49998))
    monkeypatch.setattr(base_cls, "get_timer_count", lambda self: (0, 0))

    snapshot = ctrl._collect_housekeeping_unlocked()
    assert snapshot["main_state"]["name"] == "STATE_STANDBY"
    assert "STATE_ON" not in snapshot["main_state"]["name"]
    assert snapshot["pulsers"] == []


def test_hd_controller_exposes_port_num_for_shared_mixin(monkeypatch):
    """Regression: the shared DllPortClaimRegistryMixin reads self.port_num; the
    HD controller must expose it (it slipped through the port->stream rename)."""
    import ctypes as _ctypes
    controller_cls, _base_cls = _load_hd_controller_classes()

    class _FakeDll:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(_ctypes, "WinDLL", _FakeDll, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")

    ctrl = controller_cls("hd_reg_test", com=10, stream=3, baudrate=230400)
    assert ctrl.stream == 3
    assert ctrl.port_num == 3          # mixin port-claim id == stream channel
    assert ctrl.com == 10
    # The mixin method that previously raised AttributeError must now run cleanly.
    ctrl._warn_on_other_process_ports()


def test_err_open_hint_flags_high_com_ports():
    """The -2 (ERR_OPEN) hint must warn about the COM>=10 Windows naming gotcha,
    and always remind about a port held by another process."""
    controller_cls, _base_cls = _load_hd_controller_classes()
    hint_hi = controller_cls._err_open_hint(10)
    assert "COM1-COM9" in hint_hi
    assert "holds the port" in hint_hi

    hint_lo = controller_cls._err_open_hint(5)
    assert "COM1-COM9" not in hint_lo   # below the COM10 boundary
    assert "holds the port" in hint_lo  # but the port-held reminder still applies
