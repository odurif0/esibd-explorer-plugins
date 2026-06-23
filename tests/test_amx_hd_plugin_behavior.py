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


def _load_hd_plugin_module():
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("amx_hd_plugin_test", PLUGIN_HD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hd_controller_classes():
    module = _load_hd_plugin_module()

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


def test_get_product_info_unpacks_hd_hw_type_and_version(monkeypatch):
    """Regression: HD GetHwType returns 10 values and GetHwVersion returns 3;
    get_product_info must unpack both fully (it previously expected 2, raising
    'too many values to unpack')."""
    controller_cls, base_cls = _load_hd_controller_classes()
    ctrl = _make_controller(controller_cls)

    monkeypatch.setattr(base_cls, "get_product_no", lambda self: (0, 77057))
    monkeypatch.setattr(base_cls, "get_product_id", lambda self: (0, "HV-AMX-CTRL-4EDH"))
    monkeypatch.setattr(base_cls, "get_fw_version", lambda self: (0, 257))
    monkeypatch.setattr(base_cls, "get_fw_date", lambda self: (0, "2024-05-01"))
    # HD GetHwType -> status + hw_type + 8 module counts.
    monkeypatch.setattr(base_cls, "get_hw_type", lambda self: (
        0, 0x0B21, 4, 4, 1, 4, 1, 1, 2, 2,
    ))
    # HD GetHwVersion -> status + board version + fpga version.
    monkeypatch.setattr(base_cls, "get_hw_version", lambda self: (0, 5, 12))

    info = ctrl._get_product_info_unlocked()

    assert info["product_id"] == "HV-AMX-CTRL-4EDH"
    assert info["hardware"]["type"] == 0x0B21
    assert info["hardware"]["version"] == 5
    assert info["hardware"]["fpga_version"] == 12
    modules = info["hardware"]["modules"]
    assert modules["timer"] == 4
    assert modules["oscillator"] == 1
    assert modules["switch_dual_level"] == 2


def test_collect_state_snapshot_is_lightweight(monkeypatch):
    """The startup-readiness poll uses a state-only snapshot: it must read the
    state + enable bit and must NOT pull the full housekeeping (which holds the
    transport lock for ~1-2 s and stalled the ON transition)."""
    controller_cls, base_cls = _load_hd_controller_classes()
    ctrl = _make_controller(controller_cls)

    heavy_calls = {"housekeeping": 0, "oscillator": 0, "timer_count": 0}

    monkeypatch.setattr(base_cls, "get_device_state", lambda self: (
        0, "0x1", "STATE_ON", "0x0", ["DEVST_OK"], "0x0", ["TMPST_OK"]
    ))
    monkeypatch.setattr(base_cls, "get_state", lambda self: (0, "0x401", 0, ["ST_ENABLE"]))
    monkeypatch.setattr(base_cls, "get_device_enable", lambda self: (0, True))
    # Heavy primitives must NOT be touched by the lightweight snapshot.
    monkeypatch.setattr(
        base_cls, "get_housekeeping",
        lambda self: heavy_calls.__setitem__("housekeeping", heavy_calls["housekeeping"] + 1) or (0, 12.0, 11.5, 5.0, 3.3, 3.31, 2.51, 1.2, 42.5),
    )
    monkeypatch.setattr(
        base_cls, "get_oscillator_period",
        lambda self, oscillator=0: heavy_calls.__setitem__("oscillator", heavy_calls["oscillator"] + 1) or (0, 49998),
    )
    monkeypatch.setattr(
        base_cls, "get_timer_count",
        lambda self: heavy_calls.__setitem__("timer_count", heavy_calls["timer_count"] + 1) or (0, 4),
    )

    snap = ctrl._collect_state_unlocked()

    # State-only keys present and correctly mapped (HD STATE_ON = 0x0001).
    assert snap["main_state"]["name"] == "STATE_ON"
    assert snap["device_enabled"] is True
    assert snap["device_state"]["flags"] == ["DEVST_OK"]
    assert snap["controller_state"]["flags"] == ["ST_ENABLE"]
    # Heavy keys absent -> the readiness poll does not pay for them.
    for absent in ("housekeeping", "oscillator", "pulsers"):
        assert absent not in snap
    assert heavy_calls == {"housekeeping": 0, "oscillator": 0, "timer_count": 0}


class _ComParent:
    """Minimal controllerParent exposing only what the guidance / finalize paths read."""

    def __init__(self, com):
        self.com = com

    def getChannels(self):
        return []


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port locks the COM port for the process lifetime: the
    init-failure guidance must tell the operator to restart ESIBD Explorer
    instead of letting retries loop on a confusing '-2 (Error opening port)'."""
    plugin = _load_hd_plugin_module()
    controller = plugin.AMXHDController(controllerParent=_ComParent(com=10))

    # Attempt 1: device OFF, open_port times out and poisons the transport.
    fatal_exc = RuntimeError(
        "AMX HD DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The AMX HD instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10  # recorded for later retries

    # Attempt 2: device now ON (green LED), fresh instance, but the port is still
    # locked in-process -> open_port returns -2 immediately.
    retry_exc = RuntimeError("AMX open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "locked the COM port" in guidance2
    # The non-fatal retry must not clobber the recorded poisoned COM.
    assert controller._poisoned_com == 10


def test_init_failure_guidance_silent_without_prior_poisoning():
    """A garden-variety init failure with no prior poisoning yields no guidance,
    so unrelated cabling / COM-number problems are not mis-attributed."""
    plugin = _load_hd_plugin_module()
    controller = plugin.AMXHDController(controllerParent=_ComParent(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("AMX open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None


def test_init_failure_guidance_forgets_poisoning_on_success():
    """A fresh transport reaching init completion clears any prior poisoning so
    later failures are not mis-attributed (e.g. the operator switched COM port)."""
    plugin = _load_hd_plugin_module()
    controller = plugin.AMXHDController(controllerParent=_ComParent(com=10))

    controller._init_failure_guidance(
        RuntimeError("...The AMX HD instance is now marked unusable.")
    )
    assert controller._poisoned_com == 10

    controller._finalize_transport_initialization()
    assert controller._poisoned_com is None


def test_restore_ui_state_for_device_reflects_real_hardware_state():
    """A failed ON must not strand the ON/OFF button at OFF when the AMX is
    genuinely ON: the button must reflect the real state so the OFF toggle
    (-> shutdown) stays reachable. This is the core of the deadlock fix."""
    plugin = _load_hd_plugin_module()
    restored = []
    parent = _ComParent(com=10)
    parent._set_on_ui_state = lambda on: restored.append(bool(on))
    controller = plugin.AMXHDController(controllerParent=parent)

    controller.main_state = "STATE_ON"
    controller._restore_ui_state_for_device()
    assert restored == [True]  # device genuinely ON -> button stays ON -> OFF reachable

    restored.clear()
    controller.main_state = "STATE_STANDBY"
    controller._restore_ui_state_for_device()
    assert restored == [False]  # device OFF/standby -> restore OFF as before


def test_restore_ui_state_standby_keeps_on_when_standby_is_deliberate():
    """When the operator deliberately selected a standby Operating config, a
    STATE_STANDBY hardware state is the expected outcome, not an OFF condition:
    the ON/OFF button must stay ON so the OFF toggle (-> shutdown) is reachable.
    """
    plugin = _load_hd_plugin_module()
    restored = []
    parent = _ComParent(com=10)
    parent._set_on_ui_state = lambda on: restored.append(bool(on))
    parent.operating_config = 0
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]

    controller.main_state = "STATE_STANDBY"
    controller._restore_ui_state_for_device()
    assert restored == [True]  # deliberate standby -> button stays ON


def test_startup_snapshot_ready_accepts_standby_for_standby_config():
    """A standby Operating config legitimately leaves the device in
    STATE_STANDBY (HV not applied). The startup wait must accept that as
    ready, not time out waiting for STATE_ON."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD", operating_config=0)
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]

    standby_snapshot = {
        "main_state": {"name": "STATE_STANDBY"},
        "device_enabled": False,
    }
    assert controller._startup_snapshot_ready(standby_snapshot) is True

    on_snapshot = {
        "main_state": {"name": "STATE_ON"},
        "device_enabled": True,
    }
    assert controller._startup_snapshot_ready(on_snapshot) is True


def test_startup_snapshot_ready_rejects_standby_for_non_standby_config():
    """When the Operating config is NOT standby, STATE_STANDBY is not ready:
    the device must reach STATE_ON."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD", operating_config=1)
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]

    standby_snapshot = {
        "main_state": {"name": "STATE_STANDBY"},
        "device_enabled": False,
    }
    assert controller._startup_snapshot_ready(standby_snapshot) is False


def test_device_active_predicate():
    """_device_active() is the single source of truth for whether the device is
    in its expected operating state. STATE_ON is always active. STATE_STANDBY
    is active only when the Operating config is a standby slot."""
    plugin = _load_hd_plugin_module()
    configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]

    # Standby Operating config: STATE_STANDBY is active.
    parent = types.SimpleNamespace(name="AMX_HD", operating_config=0)
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = configs

    controller.main_state = "STATE_ON"
    assert controller._device_active() is True
    controller.main_state = "STATE_STANDBY"
    assert controller._device_active() is True
    controller.main_state = "Disconnected"
    assert controller._device_active() is False

    # Normal Operating config: STATE_STANDBY is NOT active.
    parent = types.SimpleNamespace(name="AMX_HD", operating_config=1)
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = configs

    controller.main_state = "STATE_ON"
    assert controller._device_active() is True
    controller.main_state = "STATE_STANDBY"
    assert controller._device_active() is False


def _make_device_stub(plugin):
    device = object.__new__(plugin.AMXHDDevice)
    device.name = "AMX_HD"
    device.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operating A", "active": True, "valid": True},
    ]
    state = {"operating_config": 0}

    device._setting = lambda name: None
    device._config_setting_value = lambda attr: state.get(attr, -1)

    written = []
    def _set(attr, val):
        written.append((attr, val))
        state[attr] = val
        return True
    device._set_config_setting_value = _set
    device._update_config_controls = lambda: None
    device._update_status_widgets = lambda: None
    logs = []
    device.print = lambda msg, flag=None: logs.append(msg)
    device._combo_current_value = lambda combo: state.get("operating_config", -1)
    return device, written, logs, state


def test_config_selector_changed_allows_standby_operating_selection():
    """Selecting a standby slot as Operating config is persisted as-is. The
    operator is trusted to know a standby config does not apply HV; a
    non-blocking warning is emitted at ON time, not at selection time."""
    plugin = _load_hd_plugin_module()
    device, written, logs, _state = _make_device_stub(plugin)
    device.operatingConfigCombo = object()  # truthy placeholder

    plugin.AMXHDDevice._config_selector_changed(device, "operating_config")

    # The standby choice (0) is persisted, NOT reverted to -1.
    assert ("operating_config", 0) in written
    assert all(v != -1 for _k, v in written)
    assert not any("standby slot" in m for m in logs)


def test_warn_if_standby_operating_does_not_emit():
    """A standby-named Operating config is a normal operator choice. The plugin
    must NOT emit a warning — the operator knows HV will not be applied."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD")
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]
    logs = []
    controller.print = lambda msg, flag=None: logs.append(msg)

    controller._warn_if_standby_operating(0)
    assert logs == []

    controller._warn_if_standby_operating(1)
    assert logs == []


def test_resume_pending_on_auto_selects_standby_when_device_parked():
    """When the device is already in STATE_STANDBY after init (parked by a
    previous shutdown) and no Operating config is selected, the resume path
    auto-selects the standby config so the first ON completes without requiring
    the operator to manually pick a config first."""
    plugin = _load_hd_plugin_module()
    configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]
    state = {"operating_config": -1, "standby_config": 0}
    writes = []

    def _get(attr):
        return state.get(attr, -1)

    def _set(attr, val):
        writes.append((attr, val))
        state[attr] = val
        return True

    parent = types.SimpleNamespace(
        name="AMX_HD",
        isOn=lambda: True,
        _config_setting_value=_get,
        _set_config_setting_value=_set,
    )
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = configs
    controller.main_state = "STATE_STANDBY"
    toggle_calls = []
    controller.toggleOnFromThread = lambda parallel=True: toggle_calls.append(parallel)
    controller._begin_transition = lambda target_on: True

    controller._resume_pending_on_request_after_transport_ready()

    assert ("operating_config", 0) in writes
    assert toggle_calls == [True]


def test_resume_pending_on_does_not_auto_select_when_config_already_chosen():
    """If an Operating config is already selected (>= 0), the resume path must
    not override it — even if the device happens to be in STATE_STANDBY."""
    plugin = _load_hd_plugin_module()
    configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 1, "name": "Operate", "active": True, "valid": True},
    ]
    state = {"operating_config": 1, "standby_config": 0}
    writes = []

    def _get(attr):
        return state.get(attr, -1)

    def _set(attr, val):
        writes.append((attr, val))
        state[attr] = val
        return True

    parent = types.SimpleNamespace(
        name="AMX_HD",
        isOn=lambda: True,
        _config_setting_value=_get,
        _set_config_setting_value=_set,
    )
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = configs
    controller.main_state = "STATE_STANDBY"
    controller.toggleOnFromThread = lambda parallel=True: None
    controller._begin_transition = lambda target_on: True

    controller._resume_pending_on_request_after_transport_ready()

    assert all(attr != "operating_config" for attr, _val in writes)


def test_stop_acquisition_for_transition_does_not_call_base_stopAcquisition():
    """_stop_acquisition_for_transition must stop recording and set acquiring=False
    directly, without calling the base stopAcquisition (which uses a 1s lock timeout
    and logs spurious 'Could not acquire lock to stop acquisition' errors when
    runAcquisition holds the lock for collect_housekeeping)."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD")
    controller = plugin.AMXHDController(controllerParent=parent)

    stop_calls = []
    controller.stopAcquisition = lambda: stop_calls.append(True)
    controller.acquiring = True

    recording_stops = []

    class _FakeDevice:
        def __init__(self):
            self._recording = True

        @property
        def recording(self):
            return self._recording

        @recording.setter
        def recording(self, val):
            self._recording = val
            recording_stops.append(val)

    fake_device = _FakeDevice()
    controller.getDevice = lambda: fake_device

    controller._stop_acquisition_for_transition()

    assert stop_calls == []
    assert controller.acquiring is False
    assert recording_stops == [False]


def test_stop_acquisition_for_transition_tolerates_missing_getDevice():
    """If getDevice is unavailable or raises, the helper must still set
    acquiring=False without crashing."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD")
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.acquiring = True
    controller.getDevice = lambda: (_ for _ in ()).throw(RuntimeError("no device"))

    controller._stop_acquisition_for_transition()

    assert controller.acquiring is False


def test_shutdown_kwargs_skips_standby_parking_when_already_in_standby():
    """If the device is already in STATE_STANDBY, _shutdown_kwargs must not
    return a standby_config — the config is already loaded, just disconnect."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(name="AMX_HD", standby_config=0)
    controller = plugin.AMXHDController(controllerParent=parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
    ]
    controller.main_state = "STATE_STANDBY"
    assert controller._shutdown_kwargs() == {"disable_device": False}

    controller.main_state = "STATE_ON"
    assert controller._shutdown_kwargs() == {
        "standby_config": 0,
        "disable_device": False,
    }


def test_start_operating_mode_uses_extended_lock_timeout():
    """_start_operating_mode must use a lock timeout >= poll_timeout_s +
    startup_timeout_s so that an in-flight collect_housekeeping does not
    cause a spurious 'Could not acquire lock' error."""
    plugin = _load_hd_plugin_module()
    parent = types.SimpleNamespace(
        name="AMX_HD",
        poll_timeout_s=5.0,
        startup_timeout_s=10.0,
    )
    controller = plugin.AMXHDController(controllerParent=parent)
    lock_timeouts = []

    class _FakeLock:
        def acquire(self, blocking=True, timeout=-1):
            lock_timeouts.append(timeout)
            return True

        def release(self):
            pass

    controller.lock = _FakeLock()
    controller._ensure_transport_connected = lambda timeout_s: None
    controller._load_operating_config_and_enable_device = lambda **kw: None
    controller._restart_acquisition_after_transition = lambda: None

    controller._start_operating_mode(
        config_index=0,
        timeout_s=10.0,
        lock_message="test",
        success_message=None,
        restart_acquisition=True,
    )

    assert lock_timeouts == [15.0]
