"""Behavior checks for the standalone ESIBD Explorer AMX plugin."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import threading
import types
from enum import Enum
from pathlib import Path

import pytest


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "amx"
    / "amx_plugin.py"
)


def _install_esibd_stubs() -> None:
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")

    class PARAMETERTYPE(Enum):
        INT = "INT"
        FLOAT = "FLOAT"
        LABEL = "LABEL"

    class _PluginTypeValue:
        def __init__(self, value):
            self.value = value

    class PLUGINTYPE(Enum):
        INPUTDEVICE = _PluginTypeValue("INPUTDEVICE")

    class PRINT(Enum):
        WARNING = "WARNING"
        ERROR = "ERROR"

    class Parameter:
        VALUE = "Value"
        HEADER = "Header"
        NAME = "Name"
        ADVANCED = "Advanced"
        EVENT = "Event"
        TOOLTIP = "Tooltip"

    class Channel:
        NAME = "Name"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"

    class _Signal:
        def emit(self, *args, **kwargs):
            self.last_emit = (args, kwargs)

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent
            self.lock = threading.Lock()
            self.signalComm = types.SimpleNamespace(initCompleteSignal=_Signal())
            self.errorCount = 0
            self.initializing = False
            self.acquiring = False
            self.print = lambda *args, **kwargs: None

    class ToolButton:
        pass

    class Device:
        pass

    class Plugin:
        pass

    def parameterDict(**kwargs):
        return kwargs

    core.PARAMETERTYPE = PARAMETERTYPE
    core.PLUGINTYPE = PLUGINTYPE
    core.PRINT = PRINT
    core.Channel = Channel
    core.DeviceController = DeviceController
    core.Parameter = Parameter
    core.ToolButton = ToolButton
    core.parameterDict = parameterDict
    plugins.Device = Device
    plugins.Plugin = Plugin

    sys.modules["esibd"] = esibd
    sys.modules["esibd.core"] = core
    sys.modules["esibd.plugins"] = plugins


def _clear_test_modules() -> None:
    for name in [
        name
        for name in list(sys.modules)
        if name == "esibd"
        or name.startswith("esibd.")
        or name.startswith("_esibd_bundled_amx_runtime")
        or name == "amx_plugin_behavior_test"
    ]:
        sys.modules.pop(name, None)


def _load_module():
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("amx_plugin_behavior_test", PLUGIN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _install_fake_qtcore(monkeypatch):
    pyqt = types.ModuleType("PyQt6")
    pyqt.__path__ = []
    qtcore = types.ModuleType("PyQt6.QtCore")

    class FakeQObject:
        def __init__(self, parent=None):
            self.parent = parent

    class FakeQEvent:
        class Type:
            Wheel = "wheel"

    qtcore.QObject = FakeQObject
    qtcore.QEvent = FakeQEvent
    pyqt.QtCore = qtcore
    monkeypatch.setitem(sys.modules, "PyQt6", pyqt)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    return FakeQEvent


def test_spinbox_wheel_events_are_blocked(monkeypatch):
    module = _load_module()
    fake_event_class = _install_fake_qtcore(monkeypatch)

    class FakeWidget:
        def __init__(self):
            self.filters = []

        def installEventFilter(self, event_filter):
            self.filters.append(event_filter)

    class FakeEvent:
        def __init__(self, event_type):
            self.event_type = event_type
            self.ignored = False

        def type(self):
            return self.event_type

        def ignore(self):
            self.ignored = True

    widget = FakeWidget()

    module._disable_spinbox_wheel(widget)
    module._disable_spinbox_wheel(widget)

    assert len(widget.filters) == 1
    wheel_event = FakeEvent(fake_event_class.Type.Wheel)
    assert widget.filters[0].eventFilter(widget, wheel_event) is True
    assert wheel_event.ignored is True
    other_event = FakeEvent("other")
    assert widget.filters[0].eventFilter(widget, other_event) is False


def test_amx_channel_width_widget_disables_wheel(monkeypatch):
    module = _load_module()
    disabled_widgets = []
    widget = object()

    class FakeParameter:
        def getWidget(self):
            return widget

    channel = object.__new__(module.AMXChannel)
    channel.getParameterByName = lambda name: FakeParameter() if name == channel.VALUE else None
    monkeypatch.setattr(module, "_disable_spinbox_wheel", disabled_widgets.append)

    module.AMXChannel._disable_value_wheel(channel)

    assert disabled_widgets == [widget]


def test_bootstrap_config_is_replaced_with_fixed_pulsers():
    module = _load_module()
    default_item = {
        "Pulser": "0",
        "Real": True,
        "Enabled": True,
        "Delay ticks": 0,
    }

    bootstrap_items = [
        {"Name": f"AMX{index}", "Pulser": 0, "Real": True, "Enabled": True}
        for index in range(1, 5)
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=bootstrap_items,
        device_name="AMX",
        default_item=default_item,
    )

    assert [item["Name"] for item in synced_items] == [
        "AMX_P0",
        "AMX_P1",
        "AMX_P2",
        "AMX_P3",
    ]
    assert all(item["Enabled"] is False for item in synced_items)
    assert all(item["Real"] is True for item in synced_items)
    assert log_entries == [("AMX bootstrap config replaced with fixed pulser channels.", None)]


def test_existing_config_is_merged_and_duplicates_are_neutralized():
    module = _load_module()

    current_items = [
        {"Name": "Keep0", "Pulser": 0, "Real": True, "Enabled": True},
        {"Name": "Duplicate0", "Pulser": "0", "Real": True, "Enabled": False},
        {"Name": "Legacy6", "Pulser": 6, "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        device_name="AMX",
    )

    assert synced_items[0]["Real"] is True
    assert synced_items[1]["Real"] is False
    assert synced_items[2]["Real"] is False
    added_names = {item["Name"] for item in synced_items[3:]}
    assert added_names == {"AMX_P1", "AMX_P2", "AMX_P3"}
    assert ("Added generic AMX pulser channels: P1, P2, P3", None) in log_entries
    assert (
        "Marked AMX pulser channels virtual because they do not exist on hardware: P6",
        None,
    ) in log_entries
    assert (
        "Duplicate AMX mapping detected for P0: Duplicate0",
        module.PRINT.WARNING,
    ) in log_entries


def test_controller_read_numbers_maps_pulser_snapshot():
    module = _load_module()

    class FakeDevice:
        OSC_OFFSET = 2
        PULSER_WIDTH_OFFSET = 2

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": ["CTRLST_OK"]},
                "oscillator": {"period": 99998},
                "pulsers": [
                    {"pulser": 0, "width_ticks": 49998, "burst": 3},
                    {"pulser": 1, "width_ticks": 24998, "burst": None},
                ],
            }

    class FakeChannel:
        def __init__(self, pulser):
            self._pulser = pulser
            self.real = True
            self.enabled = True

        def pulser_number(self):
            return self._pulser

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [FakeChannel(0), FakeChannel(1)],
        main_state="",
        device_enabled_state="",
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.readNumbers()

    assert controller.main_state == "ST_ON"
    assert controller.device_enabled_state == "ON"
    assert controller.device_state_summary == "DEVST_OK"
    assert controller.controller_state_summary == "CTRLST_OK"
    assert controller.values[0] == pytest.approx(50.0)
    assert controller.values[1] == pytest.approx(25.0)
    assert controller.width_values == {0: "49998", 1: "24998"}
    assert controller.burst_values == {0: "3", 1: "n/a"}
    assert parent.main_state == "ST_ON"
    assert parent.device_enabled_state == "ON"


def test_controller_exposes_available_amx_configs_in_gui_state():
    module = _load_module()

    class FakeDevice:
        def list_configs(self, timeout_s=None):
            assert timeout_s == 2.5
            return [
                {"index": 0, "name": "Standby", "active": True, "valid": True},
                {"index": 9, "name": "Static:Out0-3=Hi-Z", "active": True, "valid": True},
            ]

    parent = types.SimpleNamespace(
        connect_timeout_s=2.5,
        main_state="",
        device_enabled_state="",
        available_configs_text="",
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()

    controller._refresh_available_configs()
    controller._sync_status_to_gui()

    assert controller.available_configs_text == (
        "0:Standby; 9:Static:Out0-3=Hi-Z"
    )
    assert controller.available_configs == [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 9, "name": "Static:Out0-3=Hi-Z", "active": True, "valid": True},
    ]
    assert parent.available_configs_text == controller.available_configs_text


def test_controller_exposes_loaded_amx_config_in_gui_state():
    module = _load_module()

    class FakeDevice:
        def get_status(self):
            return {
                "memory_config": 9,
                "memory_config_name": "Static:Out0-3=Hi-Z",
                "memory_config_source": "memory",
            }

    parent = types.SimpleNamespace(
        main_state="",
        device_enabled_state="",
        available_configs_text="",
        loaded_config_text="",
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()

    controller._refresh_loaded_config_status()
    controller._sync_status_to_gui()

    assert controller.loaded_config_text == "9:Static:Out0-3=Hi-Z [memory]"
    assert parent.loaded_config_text == controller.loaded_config_text


def test_controller_toggle_on_refreshes_loaded_config_after_initialize():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.connected = True
            self.connect_calls = []
            self.frequency_calls = []
            self.enable_calls = []
            self.load_calls = []

        def connect(self, timeout_s=None):
            self.connect_calls.append(timeout_s)
            self.connected = True

        def load_config(self, config_index, timeout_s=None):
            self.load_calls.append((config_index, timeout_s))

        def set_frequency_khz(self, value, timeout_s=None):
            self.frequency_calls.append((value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            self.enable_calls.append((enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": ["CTRLST_OK"]},
                "oscillator": {"period": 100000},
                "pulsers": [],
            }

        def get_status(self):
            return {
                "memory_config": 9,
                "memory_config_name": "Operate",
                "memory_config_source": "memory",
            }

    sync_calls = []
    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        poll_timeout_s=2.5,
        frequency_khz=2.0,
        operating_config=9,
        standby_config=-1,
        getChannels=lambda: [],
        isOn=lambda: True,
        main_state="",
        device_enabled_state="",
        available_configs_text="",
        loaded_config_text="",
        _update_config_controls=lambda: sync_calls.append("config"),
        _update_status_widgets=lambda: sync_calls.append("status"),
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.available_configs = [
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]
    controller._restart_acquisition_after_transition = lambda: None

    controller.toggleOn()

    assert controller.device.connect_calls == []
    assert controller.device.load_calls == [(9, 7.5)]
    assert controller.device.frequency_calls == [(2.0, 7.5)]
    assert controller.device.enable_calls == [(True, 7.5)]
    assert controller.loaded_config_text == "9:Operate [memory]"
    assert parent.loaded_config_text == "9:Operate [memory]"
    assert controller.main_state == "ST_ON"
    assert parent.main_state == "ST_ON"
    assert sync_calls


def test_controller_toggle_on_waits_for_state_on_after_enable():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.connected = True
            self.connect_calls = []
            self.load_calls = []
            self.frequency_calls = []
            self.enable_calls = []
            self.snapshot_calls = 0

        def connect(self, timeout_s=None):
            self.connect_calls.append(timeout_s)
            self.connected = True

        def load_config(self, config_index, timeout_s=None):
            self.load_calls.append((config_index, timeout_s))

        def set_frequency_khz(self, value, timeout_s=None):
            self.frequency_calls.append((value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            self.enable_calls.append((enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            self.snapshot_calls += 1
            if self.snapshot_calls == 1:
                return {
                    "device_enabled": False,
                    "main_state": {"name": "STATE_ERR_FPGA_DIS"},
                    "device_state": {"flags": ["DEVST_FPGA_DIS"]},
                    "controller_state": {"flags": []},
                    "oscillator": {"period": 100000},
                    "pulsers": [],
                }
            return {
                "device_enabled": True,
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": []},
                "oscillator": {"period": 100000},
                "pulsers": [],
            }

        def get_status(self):
            return {
                "memory_config": 9,
                "memory_config_name": "Operate",
                "memory_config_source": "memory",
            }

    messages = []
    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        poll_timeout_s=2.5,
        frequency_khz=2.0,
        operating_config=9,
        standby_config=-1,
        getChannels=lambda: [],
        isOn=lambda: True,
        main_state="",
        device_enabled_state="",
        available_configs_text="",
        loaded_config_text="",
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.available_configs = [
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]
    controller.print = lambda message, flag=None: messages.append((message, flag))
    controller._restart_acquisition_after_transition = lambda: None
    original_sleep = module.time.sleep
    module.time.sleep = lambda _seconds: None

    try:
        controller.toggleOn()
    finally:
        module.time.sleep = original_sleep

    assert controller.device.connect_calls == []
    assert controller.device.load_calls == [(9, 7.5)]
    assert controller.device.frequency_calls == [(2.0, 7.5)]
    assert controller.device.enable_calls == [(True, 7.5)]
    assert messages == [("AMX timing enabled.", None)]
    assert controller.main_state == "STATE_ON"
    assert controller.device_enabled_state == "ON"


def test_controller_startup_wait_logs_progress_when_state_is_slow(monkeypatch):
    module = _load_module()
    now = {"value": 0.0}
    snapshots = [
        {
            "device_enabled": False,
            "main_state": {"name": "STATE_ERR_FPGA_DIS"},
            "device_state": {"flags": ["DEVST_FPGA_DIS"]},
            "controller_state": {"flags": []},
            "oscillator": {"period": 100000},
            "pulsers": [],
        },
        {
            "device_enabled": False,
            "main_state": {"name": "STATE_ERR_FPGA_DIS"},
            "device_state": {"flags": ["DEVST_FPGA_DIS"]},
            "controller_state": {"flags": []},
            "oscillator": {"period": 100000},
            "pulsers": [],
        },
        {
            "device_enabled": True,
            "main_state": {"name": "STATE_ON"},
            "device_state": {"flags": ["DEVST_OK"]},
            "controller_state": {"flags": []},
            "oscillator": {"period": 100000},
            "pulsers": [],
        },
    ]

    def _collect_snapshot():
        now["value"] += 0.6
        return snapshots.pop(0)

    monkeypatch.setattr(module.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(
        module.time,
        "sleep",
        lambda seconds: now.__setitem__("value", now["value"] + seconds),
    )

    printed = []
    controller = module.AMXController(types.SimpleNamespace(getChannels=lambda: []))
    controller._collect_startup_snapshot = _collect_snapshot
    controller._apply_snapshot = lambda snapshot: None
    controller.print = lambda message, flag=None: printed.append((message, flag))

    snapshot = controller._wait_for_startup_ready_snapshot(
        config_index=9,
        settle_timeout_s=3.0,
    )

    assert snapshot["main_state"]["name"] == "STATE_ON"
    assert any(
        message.startswith("Waiting for AMX startup after config 9:")
        and flag == module.PRINT.WARNING
        for message, flag in printed
    )
    assert any(
        message == "AMX startup reached state=STATE_ON, device=ON took 2.0 s."
        and flag == module.PRINT.WARNING
        for message, flag in printed
    )


def test_controller_toggle_on_reconnects_transport_before_loading_operating_config():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.connected = False
            self.connect_calls = []
            self.load_calls = []
            self.frequency_calls = []
            self.enable_calls = []

        def connect(self, timeout_s=None):
            self.connect_calls.append(timeout_s)
            self.connected = True

        def load_config(self, config_index, timeout_s=None):
            self.load_calls.append((config_index, timeout_s))

        def set_frequency_khz(self, value, timeout_s=None):
            self.frequency_calls.append((value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            self.enable_calls.append((enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": []},
                "oscillator": {"period": 100000},
                "pulsers": [],
            }

        def get_status(self):
            return {
                "memory_config": 9,
                "memory_config_name": "Operate",
                "memory_config_source": "memory",
            }

    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        poll_timeout_s=2.5,
        frequency_khz=2.0,
        operating_config=9,
        standby_config=0,
        getChannels=lambda: [],
        isOn=lambda: True,
        main_state="",
        device_enabled_state="",
        available_configs_text="",
        loaded_config_text="",
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]
    controller._restart_acquisition_after_transition = lambda: None

    controller.toggleOn()

    assert controller.device.connect_calls == [7.5]
    assert controller.device.load_calls == [(9, 7.5)]


def test_controller_toggle_on_requires_operating_config():
    module = _load_module()

    class FakeDevice:
        def initialize(self, timeout_s=None, **kwargs):
            raise AssertionError("initialize should not run without an operating config")

    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        poll_timeout_s=2.5,
        frequency_khz=2.0,
        operating_config=-1,
        standby_config=-1,
        getChannels=lambda: [],
        isOn=lambda: True,
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )

    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    restored = []
    messages = []
    controller._restore_off_ui_state = lambda: restored.append(True)
    controller.print = lambda message, flag=None: messages.append((message, flag))

    controller.toggleOn()

    assert restored == [True]
    assert messages == [
        (
            "Cannot start AMX: select an AMX config first. Select a valid "
            "Operating config. Use Close communication to disconnect.",
            module.PRINT.WARNING,
        )
    ]


def test_controller_defaults_standby_to_slot_zero_when_available():
    module = _load_module()

    parent = types.SimpleNamespace(
        standby_config=-1,
        operating_config=9,
    )
    controller = module.AMXController(parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]

    assert controller._resolved_safety_config("standby_config") == 0
    assert controller._shutdown_kwargs() == {
        "standby_config": 0,
        "disable_device": False,
    }


def test_controller_shutdown_kwargs_use_explicit_standby_slot_when_valid():
    module = _load_module()

    parent = types.SimpleNamespace(
        standby_config=0,
        operating_config=9,
    )
    controller = module.AMXController(parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]

    assert controller._shutdown_kwargs() == {
        "standby_config": 0,
        "disable_device": False,
    }


def test_controller_ignores_slot_zero_when_it_is_not_a_standby_config():
    module = _load_module()

    parent = types.SimpleNamespace(
        standby_config=-1,
        operating_config=9,
    )
    controller = module.AMXController(parent)
    controller.available_configs = [
        {"index": 0, "name": "Static:Out0-3=Hi-Z", "active": True, "valid": True},
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]

    assert controller._resolved_safety_config("standby_config") == -1
    assert controller._shutdown_kwargs() == {}


def test_controller_skips_implicit_slot_zero_when_unavailable():
    module = _load_module()

    parent = types.SimpleNamespace(
        standby_config=-1,
        operating_config=9,
    )
    controller = module.AMXController(parent)
    controller.available_configs = [
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]

    assert controller._resolved_safety_config("standby_config") == -1
    assert controller._shutdown_kwargs() == {}


def test_controller_load_now_is_rejected_while_amx_is_off():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.load_calls = []

        def load_config(self, config_index, timeout_s=None):
            self.load_calls.append((config_index, timeout_s))

    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        operating_config=9,
        isOn=lambda: False,
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    messages = []
    controller.print = lambda message, flag=None: messages.append((message, flag))

    controller.loadOperatingConfigNow()

    assert controller.device.load_calls == []
    assert messages == [
        ("Cannot load AMX config while the AMX is OFF.", module.PRINT.WARNING)
    ]


def test_controller_load_now_requires_operating_config():
    module = _load_module()

    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        operating_config=-1,
        isOn=lambda: True,
    )
    controller = module.AMXController(parent)
    controller.device = object()
    controller.initialized = True
    messages = []
    controller.print = lambda message, flag=None: messages.append((message, flag))

    controller.loadOperatingConfigNow()

    assert messages == [
        (
            "Cannot load AMX config: select an AMX config first.",
            module.PRINT.WARNING,
        )
    ]


def test_amx_acquisition_readiness_accepts_state_on():
    module = _load_module()

    device = object.__new__(module.AMXDevice)
    device.isOn = lambda: True
    device.controller = types.SimpleNamespace(
        device=object(),
        initializing=False,
        initialized=True,
        transitioning=False,
        main_state="STATE_ON",
    )

    assert module.AMXDevice._acquisition_readiness(device) == (True, "")


def test_controller_close_communication_syncs_after_device_is_disposed():
    module = _load_module()

    class FakeDevice:
        def disconnect(self):
            return None

        def close(self):
            return None

    sync_states = []
    parent = types.SimpleNamespace(
        _update_config_controls=lambda: sync_states.append(
            (controller.device is None, controller.initialized)
        )
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.loaded_config_text = "9:Operate [memory]"
    controller.available_configs = [{"index": 9, "name": "Operate"}]
    controller.available_configs_text = "9:Operate"

    controller.closeCommunication()

    assert controller.device is None
    assert controller.initialized is False
    assert controller.loaded_config_text == "n/a"
    assert controller.available_configs == []
    assert sync_states == [(True, False)]


def test_controller_init_complete_resumes_pending_on_request():
    module = _load_module()

    toggle_calls = []
    sync_calls = []
    parent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [],
        _sync_channels=lambda: sync_calls.append("channels"),
        _update_config_controls=lambda: sync_calls.append("config"),
        _update_status_widgets=lambda: sync_calls.append("status"),
    )

    controller = module.AMXController(parent)
    controller.device = object()
    controller.toggleOnFromThread = lambda parallel=True: toggle_calls.append(parallel)

    controller.initComplete()

    assert controller.initialized is True
    assert sync_calls
    assert toggle_calls == [True]


def test_controller_init_complete_does_not_toggle_when_ui_is_off():
    module = _load_module()

    toggle_calls = []
    parent = types.SimpleNamespace(
        isOn=lambda: False,
        getChannels=lambda: [],
        _sync_channels=lambda: None,
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )

    controller = module.AMXController(parent)
    controller.device = object()
    controller.toggleOnFromThread = lambda parallel=True: toggle_calls.append(parallel)

    controller.initComplete()

    assert controller.initialized is True
    assert toggle_calls == []


def test_controller_shutdown_uses_full_software_shutdown():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, timeout_s=None, **kwargs):
            self.shutdown_calls.append((timeout_s, kwargs))
            return True

        def disconnect(self):
            return None

        def close(self):
            return None

    parent = types.SimpleNamespace(
        startup_timeout_s=7.5,
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )
    controller = module.AMXController(parent)
    device = FakeDevice()
    controller.device = device
    controller.initialized = True
    controller.shutdownCommunication()

    assert controller.device is None
    assert device.shutdown_calls == [(7.5, {})]


def test_controller_shutdown_failure_marks_state_unconfirmed():
    module = _load_module()

    class FakeDevice:
        def shutdown(self, timeout_s=None, **kwargs):
            raise RuntimeError("boom")

        def disconnect(self):
            return None

        def close(self):
            return None

    messages = []
    parent = types.SimpleNamespace(
        startup_timeout_s=7.5,
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: messages.append((message, flag))

    shutdown_confirmed = controller.shutdownCommunication()

    assert shutdown_confirmed is False
    assert controller.device is None
    assert controller.main_state == module._AMX_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.device_enabled_state == "Unknown"
    assert messages == [
        ("Starting AMX shutdown sequence.", None),
        ("AMX shutdown failed: boom", module.PRINT.ERROR),
        (
            "AMX shutdown could not be confirmed before disconnect: boom.",
            module.PRINT.ERROR,
        ),
    ]


def test_device_shutdown_keeps_ui_on_when_shutdown_is_unconfirmed():
    module = _load_module()

    device = object.__new__(module.AMXDevice)
    device.useOnOffLogic = True
    device.onAction = types.SimpleNamespace(state=False)
    device.controller = types.SimpleNamespace(shutdownCommunication=lambda: False)
    sync_states = []
    acquisition_sync_calls = []
    warnings = []
    device._sync_local_on_action = lambda: sync_states.append(device.onAction.state)
    device._sync_acquisition_controls = lambda: acquisition_sync_calls.append(True)
    device._update_status_widgets = lambda: None
    device.print = lambda message, flag=None: warnings.append((message, flag))
    device.recording = True

    module.AMXDevice.shutdownCommunication(device)

    assert device.onAction.state is True
    assert sync_states == [True]
    assert device.recording is False
    assert acquisition_sync_calls == [True]
    assert any("shutdown could not be confirmed" in message for message, _ in warnings)


def test_device_close_communication_stops_recording_when_disconnected():
    module = _load_module()

    close_calls = []
    device = object.__new__(module.AMXDevice)
    device.useOnOffLogic = True
    device.onAction = types.SimpleNamespace(state=True)
    device.controller = types.SimpleNamespace(
        initialized=False,
        closeCommunication=lambda: close_calls.append(True),
    )
    sync_states = []
    acquisition_sync_calls = []
    update_calls = []
    device._sync_local_on_action = lambda: sync_states.append(device.onAction.state)
    device._sync_acquisition_controls = lambda: acquisition_sync_calls.append(True)
    device._update_status_widgets = lambda: update_calls.append(True)
    device.recording = True

    module.AMXDevice.closeCommunication(device)

    assert close_calls == [True]
    assert device.onAction.state is False
    assert sync_states == [False]
    assert device.recording is False
    assert acquisition_sync_calls == [True]
    assert update_calls == [True]


def test_controller_shutdown_parks_standby_before_disconnect_when_available():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, timeout_s=None, **kwargs):
            self.shutdown_calls.append((timeout_s, kwargs))
            return True

        def disconnect(self):
            return None

        def close(self):
            return None

    messages = []
    parent = types.SimpleNamespace(
        name="AMX",
        startup_timeout_s=7.5,
        standby_config=-1,
        _update_config_controls=lambda: None,
        _update_status_widgets=lambda: None,
    )
    controller = module.AMXController(parent)
    controller.available_configs = [
        {"index": 0, "name": "Standby", "active": True, "valid": True},
        {"index": 9, "name": "Operate", "active": True, "valid": True},
    ]
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: messages.append((message, flag))

    controller.shutdownCommunication()

    assert controller.device is None
    assert messages[:2] == [
        ("Starting AMX shutdown sequence.", None),
        ("Parking AMX in standby config 0 before disconnect.", None),
    ]
    assert messages[-1] == ("AMX shutdown sequence completed.", None)
    assert controller.device is None
    assert controller.initialized is False


def test_amx_controller_lock_section_uses_raw_lock_and_propagates_errors():
    module = _load_module()

    class FakeLock:
        def __init__(self):
            self.acquire_calls = []
            self.release_calls = 0
            self.acquire_timeout_calls = []

        def acquire(self, timeout=-1):
            self.acquire_calls.append(timeout)
            return True

        def release(self):
            self.release_calls += 1

        @contextlib.contextmanager
        def acquire_timeout(self, timeout, timeoutMessage="", already_acquired=False):
            self.acquire_timeout_calls.append((timeout, timeoutMessage, already_acquired))
            yield True

    controller = module.AMXController(types.SimpleNamespace())
    controller.lock = FakeLock()

    with pytest.raises(IndexError, match="boom"):
        with controller._controller_lock_section("lock failed"):
            raise IndexError("boom")

    assert controller.lock.acquire_calls == [1]
    assert controller.lock.release_calls == 1
    assert controller.lock.acquire_timeout_calls == []


def test_amx_controller_lock_section_logs_slow_lock_acquisition(monkeypatch):
    module = _load_module()
    now = {"value": 0.0}
    printed = []

    class FakeLock:
        def __init__(self):
            self.release_calls = 0

        def acquire(self, timeout=-1):
            now["value"] = 1.3
            return True

        def release(self):
            self.release_calls += 1

    monkeypatch.setattr(module.time, "monotonic", lambda: now["value"])
    controller = module.AMXController(types.SimpleNamespace())
    controller.lock = FakeLock()
    controller.print = lambda message, flag=None: printed.append((message, flag))

    with controller._controller_lock_section("Could not acquire lock to start the AMX."):
        pass

    assert controller.lock.release_calls == 1
    assert printed == [
        (
            "AMX controller lock acquired after 1.3 s: "
            "Could not acquire lock to start the AMX.",
            module.PRINT.WARNING,
        )
    ]


def test_amx_controller_read_numbers_acquires_lock_for_housekeeping():
    module = _load_module()

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": ["CTRLST_OK"]},
                "oscillator": {"period": 100000},
                "pulsers": [],
            }

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        device_enabled_state="",
        available_configs_text="",
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    lock_calls = []

    @contextlib.contextmanager
    def fake_lock_section(
        timeout_message,
        *,
        already_acquired=False,
        timeout_s=1.0,
        log_timeout=True,
    ):
        lock_calls.append((timeout_message, already_acquired, timeout_s, log_timeout))
        yield

    controller._controller_lock_section = fake_lock_section
    controller.readNumbers()

    assert lock_calls == [
        ("Could not acquire lock to read AMX housekeeping.", False, 0.0, False)
    ]


def test_amx_controller_read_numbers_skips_busy_poll_without_error():
    module = _load_module()

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        device_enabled_state="",
        available_configs_text="",
    )
    controller = module.AMXController(parent)
    controller.device = object()
    controller.initialized = True
    controller.errorCount = 0
    printed = []
    initialize_calls = []
    lock_timeout_message = "Could not acquire lock to read AMX housekeeping."
    controller.print = lambda message, flag=None: printed.append((message, flag))
    controller.initializeValues = lambda reset=False: initialize_calls.append(reset)

    @contextlib.contextmanager
    def busy_lock_section(*_args, **_kwargs):
        raise TimeoutError(lock_timeout_message)
        yield

    controller._controller_lock_section = busy_lock_section

    controller.readNumbers()

    assert controller.errorCount == 0
    assert printed == []
    assert initialize_calls == []


def test_controller_read_numbers_runs_while_framework_holds_the_lock():
    """Regression: readNumbers must read housekeeping when the acquisition loop
    already holds the non-reentrant controller lock.

    Forwarding already_acquired=True keeps the duty monitors and status badge live
    during operation; without it every poll silently aborted.
    """
    module = _load_module()

    class FakeDevice:
        OSC_OFFSET = 2
        PULSER_WIDTH_OFFSET = 2

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": ["CTRLST_OK"]},
                "oscillator": {"period": 99998},
                "pulsers": [
                    {"pulser": 0, "width_ticks": 49998, "delay_ticks": 0, "burst": 3},
                ],
            }

    class FakeChannel:
        def __init__(self, pulser):
            self.id = pulser
            self.real = True
            self.enabled = True

        def pulser_number(self):
            return self.id

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [FakeChannel(0)],
        main_state="",
        device_enabled_state="",
        available_configs_text="",
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    # Hold the non-reentrant lock the same way the framework acquisition loop does.
    assert controller.lock.acquire()
    try:
        controller.readNumbers(already_acquired=True)
    finally:
        controller.lock.release()

    assert controller.main_state == "ST_ON"
    assert controller.values[0] == pytest.approx(50.0)


def test_controller_run_acquisition_reads_and_emits_under_lock(monkeypatch):
    """runAcquisition forwards already_acquired=True so each tick actually reads."""
    module = _load_module()

    class FakeDevice:
        OSC_OFFSET = 2
        PULSER_WIDTH_OFFSET = 2

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVST_OK"]},
                "controller_state": {"flags": ["CTRLST_OK"]},
                "oscillator": {"period": 99998},
                "pulsers": [
                    {"pulser": 0, "width_ticks": 49998, "delay_ticks": 0, "burst": 3},
                ],
            }

    class FakeChannel:
        def __init__(self, pulser):
            self.id = pulser
            self.real = True
            self.enabled = True

        def pulser_number(self):
            return self.id

    emitted = []

    class FakeSignal:
        def emit(self):
            emitted.append(True)

    parent = types.SimpleNamespace(
        interval=1000,
        poll_timeout_s=2.5,
        getChannels=lambda: [FakeChannel(0)],
        main_state="",
        device_enabled_state="",
        available_configs_text="",
    )
    controller = module.AMXController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.signalComm.updateValuesSignal = FakeSignal()

    # Stop the loop after one completed iteration.
    monkeypatch.setattr(module.time, "sleep", lambda *_args: setattr(controller, "acquiring", False))

    controller.acquiring = True
    controller.runAcquisition()

    assert controller.values[0] == pytest.approx(50.0)
    assert emitted == [True]


def test_frequency_widget_change_is_debounced_until_timer_fires():
    module = _load_module()
    calls = []

    class FakeTimer:
        def __init__(self):
            self.starts = []

        def start(self, timeout_ms):
            self.starts.append(timeout_ms)

        def stop(self):
            calls.append("timer_stopped")

    controller = types.SimpleNamespace(
        initialized=True,
        applyGlobalSettingsFromThread=lambda parallel=True: calls.append(
            ("apply_global", parallel)
        ),
    )
    device = object.__new__(module.AMXDevice)
    device.name = "AMX"
    device.frequency_khz = 2.0
    device.controller = controller
    device.loading = False
    device.isOn = lambda: True
    device.getChannels = lambda: []
    device._globalSettingsApplyTimer = FakeTimer()
    device._update_status_widgets = lambda: calls.append("status")

    module.AMXDevice._frequency_widget_changed(device, 4.0)

    assert device.frequency_khz == 4.0
    assert device._globalSettingsApplyTimer.starts == [
        module._AMX_NUMERIC_DEBOUNCE_MS,
    ]
    assert calls == ["status"]

    module.AMXDevice._apply_global_settings_now(device)

    assert calls == ["status", ("apply_global", True)]


def test_amx_channel_value_change_is_debounced_until_timer_fires():
    module = _load_module()
    calls = []

    class FakeTimer:
        def __init__(self):
            self.starts = []

        def start(self, timeout_ms):
            self.starts.append(timeout_ms)

        def stop(self):
            calls.append("timer_stopped")

    channel = object.__new__(module.AMXChannel)
    channel.channelParent = types.SimpleNamespace(
        loading=False,
        controller=types.SimpleNamespace(
            applyValueFromThread=lambda channel, parallel=True: calls.append(
                ("apply_value", channel.pulser_number(), parallel)
            )
        ),
    )
    channel.id = 2
    channel._valueApplyTimer = FakeTimer()
    channel._update_duty_label = lambda: calls.append("duty")
    channel._sync_monitor_feedback = lambda: calls.append("feedback")
    channel.applyValue = lambda apply=True: calls.append(("legacy_apply", apply))

    module.AMXChannel.valueChanged(channel)

    assert channel._valueApplyTimer.starts == [module._AMX_NUMERIC_DEBOUNCE_MS]
    assert calls == ["duty", "feedback"]

    module.AMXChannel._apply_value_now(channel)

    assert calls == ["duty", "feedback", ("apply_value", 2, True)]


def test_amx_global_settings_queue_keeps_latest_pending_request(monkeypatch):
    module = _load_module()
    created_threads = []
    applied = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target
            self.name = name
            self.daemon = daemon
            created_threads.append(self)

        def start(self):
            return None

    parent = types.SimpleNamespace(name="AMX")
    controller = module.AMXController(parent)
    controller.applyGlobalSettings = lambda: applied.append("global")
    monkeypatch.setattr(module, "Thread", FakeThread)

    controller.applyGlobalSettingsFromThread(parallel=True)
    controller.applyGlobalSettingsFromThread(parallel=True)
    controller.applyGlobalSettingsFromThread(parallel=True)

    assert len(created_threads) == 1

    created_threads[0].target()

    assert applied == ["global"]
    assert controller._global_apply_worker_running is False


def test_amx_channel_timing_queue_keeps_latest_per_pulser(monkeypatch):
    module = _load_module()
    created_threads = []
    applied = []

    class FakeThread:
        def __init__(self, *, target, name=None, daemon=None):
            self.target = target
            self.name = name
            self.daemon = daemon
            created_threads.append(self)

        def start(self):
            return None

    class FakeChannel:
        def __init__(self, pulser, value):
            self.id = pulser
            self.value = value

        def pulser_number(self):
            return self.id

    parent = types.SimpleNamespace(name="AMX")
    controller = module.AMXController(parent)
    controller.applyValue = lambda channel: applied.append(
        (channel.pulser_number(), channel.value)
    )
    monkeypatch.setattr(module, "Thread", FakeThread)

    first = FakeChannel(1, 1.0)
    latest = FakeChannel(1, 2.0)
    other = FakeChannel(2, 3.0)
    controller.applyValueFromThread(first, parallel=True)
    controller.applyValueFromThread(latest, parallel=True)
    controller.applyValueFromThread(other, parallel=True)

    assert len(created_threads) == 1

    created_threads[0].target()

    assert applied == [(1, 2.0), (2, 3.0)]
    assert controller._channel_apply_worker_running is False


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port locks the COM port for the process lifetime; the
    init-failure guidance must tell the operator to restart ESIBD Explorer
    instead of letting retries loop on a confusing '-2 (Error opening port)'."""
    module = _load_module()
    controller = module.AMXController(types.SimpleNamespace(com=10))

    # Attempt 1: device OFF, open_port times out and poisons the transport.
    fatal_exc = RuntimeError(
        "AMX DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The AMX instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10  # recorded for later retries

    # Attempt 2: device now ON, fresh instance, but the port is still locked
    # in-process -> open_port returns -2 immediately.
    retry_exc = RuntimeError("AMX open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "locked the COM port" in guidance2
    assert controller._poisoned_com == 10  # not clobbered by the non-fatal retry


def test_init_failure_guidance_silent_without_prior_poisoning():
    """A garden-variety init failure with no prior poisoning yields no guidance,
    so unrelated cabling / COM-number problems are not mis-attributed."""
    module = _load_module()
    controller = module.AMXController(types.SimpleNamespace(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("AMX open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None


def test_restore_ui_state_for_device_reflects_real_hardware_state():
    """Mirror of the AMX HD test: a failed ON must keep the button ON when the
    AMX is genuinely ON, so the OFF toggle stays reachable (deadlock fix)."""
    module = _load_module()
    restored = []
    parent = types.SimpleNamespace(com=10)
    parent._set_on_ui_state = lambda on: restored.append(bool(on))
    controller = module.AMXController(parent)

    controller.main_state = "STATE_ON"
    controller._restore_ui_state_for_device()
    assert restored == [True]

    restored.clear()
    controller.main_state = "STATE_STANDBY"
    controller._restore_ui_state_for_device()
    assert restored == [False]


def test_warn_if_standby_operating_does_not_emit():
    """A standby-named Operating config is a normal operator choice. The plugin
    must NOT emit a warning — the operator knows HV will not be applied."""
    module = _load_module()
    parent = types.SimpleNamespace(name="AMX")
    controller = module.AMXController(parent)
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


def test_stop_acquisition_for_transition_does_not_call_base_stopAcquisition():
    """_stop_acquisition_for_transition must stop recording and set acquiring=False
    directly, without calling the base stopAcquisition (which uses a 1s lock timeout
    and logs spurious 'Could not acquire lock to stop acquisition' errors when
    runAcquisition holds the lock for collect_housekeeping)."""
    module = _load_module()
    parent = types.SimpleNamespace(name="AMX")
    controller = module.AMXController(parent)

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
