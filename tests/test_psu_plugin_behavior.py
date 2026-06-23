"""Behavior checks for the standalone ESIBD Explorer PSU plugin."""

from __future__ import annotations

import contextlib
import importlib.util
import sys
import threading
import types
from enum import Enum
from pathlib import Path

import numpy as np
import pytest


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "psu_a"
    / "psu_plugin.py"
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
        or name.startswith("_esibd_bundled_psu_runtime")
        or name == "psu_plugin_behavior_test"
    ]:
        sys.modules.pop(name, None)


def _load_module():
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("psu_plugin_behavior_test", PLUGIN_PATH)
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


def test_bootstrap_config_is_replaced_with_fixed_channels():
    module = _load_module()
    default_item = {
        "CH": "0",
        "Real": True,
        "Enabled": True,
        "Output": "OFF",
    }

    bootstrap_items = [
        {"Name": f"PSU{index}", "CH": 0, "Real": True, "Enabled": True}
        for index in range(1, 5)
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=bootstrap_items,
        device_name="PSU",
        default_item=default_item,
    )

    assert synced_items == [
        {
            "Name": "PSU_CH0",
            "CH": "0",
            "Real": True,
            "Enabled": False,
            "Output": "OFF",
        },
        {
            "Name": "PSU_CH1",
            "CH": "1",
            "Real": True,
            "Enabled": False,
            "Output": "OFF",
        },
    ]
    assert log_entries == [("PSU bootstrap config replaced with fixed hardware channels.", None)]


def test_existing_config_keeps_only_hardware_channels():
    module = _load_module()

    current_items = [
        {"Name": "Keep0", "CH": 0, "Real": True, "Enabled": True},
        {"Name": "Duplicate0", "CH": "0", "Real": True, "Enabled": False},
        {"Name": "Legacy5", "CH": 5, "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        device_name="PSU",
    )

    assert synced_items == [
        {"Name": "Keep0", "CH": 0, "Real": True, "Enabled": True},
        {
            "Name": "PSU_CH1",
            "CH": "1",
            "Real": True,
            "Enabled": False,
        },
    ]
    assert ("Added generic PSU channels: CH1", None) in log_entries
    assert (
        "Removed PSU channels not present on hardware: CH5",
        None,
    ) in log_entries
    assert (
        "Removed duplicate PSU mapping for CH0: Duplicate0",
        module.PRINT.WARNING,
    ) in log_entries


def test_missing_hardware_channel_is_recreated_with_fixed_mapping():
    module = _load_module()

    current_items = [
        {"Name": "Keep1", "CH": 1, "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        device_name="PSU",
    )

    assert synced_items[0] == {
        "Name": "Keep1",
        "CH": 1,
        "Real": True,
        "Enabled": True,
    }
    assert synced_items[1] == {
        "Name": "PSU_CH0",
        "CH": "0",
        "Real": True,
        "Enabled": False,
    }
    assert ("Added generic PSU channels: CH0", None) in log_entries


def test_existing_config_is_merged_and_missing_hardware_channel_is_added():
    module = _load_module()

    current_items = [
        {"Name": "Keep0", "CH": 0, "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        device_name="PSU",
    )

    assert synced_items[0] == {"Name": "Keep0", "CH": 0, "Real": True, "Enabled": True}
    assert synced_items[1] == {
        "Name": "PSU_CH1",
        "CH": "1",
        "Real": True,
        "Enabled": False,
    }
    assert ("Added generic PSU channels: CH1", None) in log_entries


def test_controller_read_numbers_maps_housekeeping_snapshot():
    module = _load_module()

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": True,
                "output_enabled": (True, False),
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": True,
                        "dropout_v": 1.25,
                        "adc": {"temp_adc_c": 36.5},
                        "rails": {
                            "volt_24vp_v": 24.2,
                            "volt_12vp_v": 12.1,
                            "volt_12vn_v": -12.0,
                            "volt_ref_v": 2.5,
                        },
                        "voltage": {"measured_v": 25.0, "set_v": 30.0},
                        "current": {"measured_a": 0.4, "set_a": 0.5},
                    },
                    {
                        "channel": 1,
                        "enabled": False,
                        "dropout_v": 0.0,
                        "adc": {"temp_adc_c": 31.0},
                        "rails": {
                            "volt_24vp_v": 24.0,
                            "volt_12vp_v": 12.0,
                            "volt_12vn_v": -12.0,
                            "volt_ref_v": 2.5,
                        },
                        "voltage": {"measured_v": 0.0, "set_v": 0.0},
                        "current": {"measured_a": 0.0, "set_a": 0.0},
                    },
                ],
            }

    class FakeChannel:
        def __init__(self, channel):
            self._channel = channel
            self.real = True
            self.enabled = True

        def channel_number(self):
            return self._channel

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [FakeChannel(0), FakeChannel(1)],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.readNumbers()

    assert controller.main_state == "ST_ON"
    assert controller.hardware_main_state == "STATE_ON"
    assert controller.device_state_summary == "DEVICE_OK"
    assert controller.output_state_summary == "CH0=ON, CH1=OFF"
    assert controller.values == {0: 25.0, 1: 0.0}
    assert controller.current_values == {0: 0.4, 1: 0.0}
    assert controller.output_enabled_by_channel == {0: True, 1: False}
    assert controller.voltage_setpoints == {0: "30 V", 1: "0 V"}
    assert controller.current_setpoints == {0: "0.5 A", 1: "0 A"}
    assert controller.adc_temperatures == {0: 36.5, 1: 31.0}
    assert controller.dropout_values == {0: 1.25, 1: 0.0}
    assert controller.rail_summaries == {
        0: "24Vp 24.2 V, 12Vp 12.1 V, 12Vn -12 V, Ref 2.5 V",
        1: "24Vp 24 V, 12Vp 12 V, 12Vn -12 V, Ref 2.5 V",
    }
    assert parent.main_state == "ST_ON"
    assert parent.hardware_main_state == "STATE_ON"
    assert parent.output_summary == "CH0=ON, CH1=OFF"


def test_controller_maps_disabled_psu_state_to_standby():
    module = _load_module()

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": False,
                "output_enabled": (False, False),
                "main_state": {"name": "STATE_ERR_PSU_DIS"},
                "device_state": {"flags": ["DEVST_PSU_DIS"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": False,
                        "voltage": {"measured_v": 0.0, "set_v": 0.0},
                        "current": {"measured_a": 0.0, "set_a": 0.0},
                    },
                    {
                        "channel": 1,
                        "enabled": False,
                        "voltage": {"measured_v": 0.0, "set_v": 0.0},
                        "current": {"measured_a": 0.0, "set_a": 0.0},
                    },
                ],
            }

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.readNumbers()

    assert controller.main_state == "ST_STBY"
    assert controller.hardware_main_state == "STATE_ERR_PSU_DIS"
    assert controller.device_state_summary == "DEVST_PSU_DIS"
    assert parent.main_state == "ST_STBY"
    assert parent.hardware_main_state == "STATE_ERR_PSU_DIS"


def test_controller_read_numbers_reports_snapshot_apply_failures_explicitly():
    module = _load_module()

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            return {"channels": []}

    printed = []
    resets = []
    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))
    controller.initializeValues = lambda reset=False: resets.append(reset)
    controller._apply_snapshot = lambda snapshot, **_kwargs: (_ for _ in ()).throw(
        IndexError("list index out of range")
    )

    controller.readNumbers()

    assert controller.errorCount == 1
    assert printed == [
        (
            "Failed to apply PSU housekeeping snapshot: IndexError: list index out of range",
            module.PRINT.ERROR,
        )
    ]
    assert resets == [True]


def test_controller_exposes_available_psu_configs_in_gui_state():
    module = _load_module()

    class FakeDevice:
        def list_configs(self, timeout_s=None):
            assert timeout_s == 2.5
            return [
                {"index": 1, "name": "Standby", "active": True, "valid": True},
                {"index": 7, "name": "Operate 5 kV", "active": True, "valid": True},
            ]

    parent = types.SimpleNamespace(
        connect_timeout_s=2.5,
        main_state="",
        output_summary="",
        available_configs_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()

    controller._refresh_available_configs()
    controller._sync_status_to_gui()

    assert controller.available_configs == [
        {"index": 1, "name": "Standby", "active": True, "valid": True},
        {"index": 7, "name": "Operate 5 kV", "active": True, "valid": True},
    ]
    assert controller.available_configs_text == "1:Standby; 7:Operate 5 kV"
    assert parent.available_configs == controller.available_configs
    assert parent.available_configs_text == controller.available_configs_text


def test_psu_config_selector_change_updates_operating_slot():
    module = _load_module()

    class FakeCombo:
        def __init__(self):
            self.items = [("Skip (-1)", -1), ("7:Operate 5 kV", 7)]
            self._current_index = 1

        def currentIndex(self):
            return self._current_index

        def itemData(self, index):
            return self.items[index][1]

    updates = []
    device = object.__new__(module.PSUDevice)
    device.name = "PSU"
    device.operating_config = -1
    device.operatingConfigCombo = FakeCombo()
    device._update_config_selectors = lambda: updates.append("selectors")
    device._update_status_widgets = lambda: updates.append("status")

    module.PSUDevice._config_selector_changed(device, "operating_config")

    assert device.operating_config == 7
    assert updates == ["selectors"]


def test_psu_controller_lock_section_uses_raw_lock_and_propagates_errors():
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

    controller = module.PSUController(types.SimpleNamespace())
    controller.lock = FakeLock()

    with pytest.raises(IndexError, match="boom"):
        with controller._controller_lock_section("lock failed"):
            raise IndexError("boom")

    assert controller.lock.acquire_calls == [1]
    assert controller.lock.release_calls == 1
    assert controller.lock.acquire_timeout_calls == []


def test_psu_controller_read_numbers_acquires_lock_for_housekeeping():
    module = _load_module()

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            return {
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "output_enabled": (False, False),
                "channels": [],
            }

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        available_configs_text="",
    )
    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    lock_calls = []

    @contextlib.contextmanager
    def fake_lock_section(timeout_message, *, already_acquired=False):
        lock_calls.append((timeout_message, already_acquired))
        yield

    controller._controller_lock_section = fake_lock_section
    controller.readNumbers()

    assert lock_calls == [("Could not acquire lock to read PSU housekeeping.", False)]


def test_read_numbers_lock_timeout_is_not_a_transport_failure(monkeypatch):
    """Regression: a controller-lock timeout while polling PSU housekeeping is
    transient congestion (e.g. an in-flight range switch holding the lock while
    the HV discharges), NOT a communication fault. It must not count toward the
    transport-failure threshold (which would, after 3 strikes, falsely declare
    the PSU lost and raise the HV 'outputs may remain energized' alarm) and must
    not wipe the live readbacks."""
    module = _load_module()

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        getChannels=lambda: [],
        main_state="ST_ON",
        output_summary="12 V",
        available_configs_text="",
    )
    controller = module.PSUController(parent)
    controller.device = types.SimpleNamespace()
    controller.initialized = True
    controller.values = {"seed": 1.0}  # prior readback; must survive a skip

    transport_notes = []
    transport_losses = []
    monkeypatch.setattr(
        controller, "_note_transport_failure",
        lambda: transport_notes.append(1) or 0,
    )
    monkeypatch.setattr(controller, "_handle_transport_loss", lambda: transport_losses.append(1))

    @contextlib.contextmanager
    def blocking_lock_section(timeout_message, *, already_acquired=False):
        raise TimeoutError(timeout_message)

    controller._controller_lock_section = blocking_lock_section
    controller.readNumbers()

    assert controller.errorCount == 0          # not counted as an error
    assert transport_notes == []               # not recorded as a transport fault
    assert transport_losses == []              # PSU not declared lost
    assert controller.values == {"seed": 1.0}  # readbacks preserved, not wiped


def test_read_numbers_skips_polling_during_manual_apply(monkeypatch):
    module = _load_module()

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        getChannels=lambda: [],
        main_state="ST_ON",
        output_summary="12 V",
        available_configs_text="",
    )
    controller = module.PSUController(parent)
    controller.device = types.SimpleNamespace()
    controller.initialized = True
    controller._manual_apply_active = True
    controller.values = {"seed": 1.0}

    def fail_lock_section(*_args, **_kwargs):
        raise AssertionError("polling should not try to acquire the PSU lock")

    monkeypatch.setattr(controller, "_controller_lock_section", fail_lock_section)

    controller.readNumbers()

    assert controller.values == {"seed": 1.0}


def test_controller_read_numbers_refreshes_live_readbacks_between_housekeeping_polls(
    monkeypatch,
):
    module = _load_module()
    monotonic_values = iter([10.0, 10.7])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(monotonic_values))

    calls = []

    class FakeDevice:
        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": True,
                "output_enabled": (True, False),
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": True,
                        "voltage": {"measured_v": 25.0, "set_v": 30.0},
                        "current": {"measured_a": 0.4, "set_a": 0.5},
                    },
                    {
                        "channel": 1,
                        "enabled": False,
                        "voltage": {"measured_v": 0.0, "set_v": 0.0},
                        "current": {"measured_a": 0.0, "set_a": 0.0},
                    },
                ],
            }

        def get_device_enabled(self, timeout_s=None):
            calls.append(("get_device_enabled", timeout_s))
            return True

        def get_output_enabled(self, timeout_s=None):
            calls.append(("get_output_enabled", timeout_s))
            return (False, True)

        def get_channel_measured_voltage(self, channel, timeout_s=None):
            calls.append(("get_channel_measured_voltage", channel, timeout_s))
            return {0: 22.0, 1: 4.5}[channel]

        def get_channel_measured_current(self, channel, timeout_s=None):
            calls.append(("get_channel_measured_current", channel, timeout_s))
            return {0: 0.22, 1: 0.045}[channel]

    class FakeChannel:
        def __init__(self, channel):
            self._channel = channel
            self.real = True

        def channel_number(self):
            return self._channel

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        getChannels=lambda: [FakeChannel(0), FakeChannel(1)],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.readNumbers()
    controller.readNumbers()

    assert calls == [
        ("collect_housekeeping", 2.5),
        ("get_device_enabled", 2.5),
        ("get_output_enabled", 2.5),
        ("get_channel_measured_voltage", 0, 2.5),
        ("get_channel_measured_current", 0, 2.5),
        ("get_channel_measured_voltage", 1, 2.5),
        ("get_channel_measured_current", 1, 2.5),
        ("get_device_enabled", 2.5),
        ("get_output_enabled", 2.5),
        ("get_channel_measured_voltage", 0, 2.5),
        ("get_channel_measured_current", 0, 2.5),
        ("get_channel_measured_voltage", 1, 2.5),
        ("get_channel_measured_current", 1, 2.5),
    ]
    assert controller.values == {0: 22.0, 1: 4.5}
    assert controller.current_values == {0: 0.22, 1: 0.045}
    assert controller.output_enabled_by_channel == {0: False, 1: True}
    assert controller.voltage_setpoints == {0: "30 V", 1: "0 V"}
    assert controller.current_setpoints == {0: "0.5 A", 1: "0 A"}
    assert parent.output_summary == "CH0=OFF, CH1=ON"


def test_channel_panel_diagnostics_snapshot_formats_compact_summary():
    module = _load_module()

    device = object.__new__(module.PSUDevice)
    device.controller = types.SimpleNamespace(
        device_state_summary="DEVICE_OK",
        adc_temperatures={0: 35.0, 1: 31.5},
        dropout_values={0: 1.2, 1: 0.4},
        rail_summaries={
            0: "24Vp 24.1 V, 12Vp 12 V",
            1: "24Vp 24 V, 12Vp 12.1 V",
        },
    )

    snapshot = module.PSUDevice._channel_panel_diagnostics_snapshot(device)

    assert snapshot["text"] == "CH0 Tadc 35 C, Dropout 1.2 V | CH1 Tadc 31.5 C, Dropout 0.4 V"
    assert "Device flags: DEVICE_OK" in snapshot["tooltip"]
    assert "CH0 rails: 24Vp 24.1 V, 12Vp 12 V" in snapshot["tooltip"]
    assert "CH1 rails: 24Vp 24 V, 12Vp 12.1 V" in snapshot["tooltip"]


def test_format_current_text_handles_nan():
    module = _load_module()

    assert module._format_current_text(np.nan) == "n/a"
    assert module._format_current_text(0.125) == "0.125 A"


def test_format_voltage_text_handles_nan():
    module = _load_module()

    assert module._format_voltage_text(np.nan) == "n/a"
    assert module._format_voltage_text(25.0) == "25 V"


def test_readback_feedback_keeps_disabled_outputs_visually_checked():
    module = _load_module()

    assert module._psu_feedback_style("default")
    assert (
        module._voltage_feedback_state(enabled=False, measured_v=0.0, set_v=5000.0)
        == "ok"
    )
    assert (
        module._voltage_feedback_state(enabled=False, measured_v=0.5, set_v=5000.0)
        == "warn"
    )
    assert (
        module._voltage_feedback_state(enabled=False, measured_v=2.0, set_v=5000.0)
        == "error"
    )
    assert (
        module._current_limit_feedback_state(
            enabled=False,
            measured_a=0.0,
            limit_a=1.0,
        )
        == "ok"
    )
    assert (
        module._current_limit_feedback_state(
            enabled=False,
            measured_a=0.005,
            limit_a=1.0,
        )
        == "ok"
    )
    assert (
        module._current_limit_feedback_state(
            enabled=False,
            measured_a=0.03,
            limit_a=1.0,
        )
        == "warn"
    )
    assert (
        module._current_limit_feedback_state(
            enabled=False,
            measured_a=0.1,
            limit_a=1.0,
        )
        == "error"
    )


def test_manual_panel_sync_updates_controls_without_live_apply():
    module = _load_module()
    emitted = []

    class FakeTimer:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class FakeControl:
        def __init__(self, *, checked=False, value=0.0, combo_index=0):
            self.checked = checked
            self._value = value
            self._combo_index = combo_index
            self.blocked = False

        def blockSignals(self, blocked):
            self.blocked = bool(blocked)

        def setChecked(self, checked):
            self.checked = bool(checked)
            if not self.blocked:
                emitted.append(("checked", checked))

        def isChecked(self):
            return self.checked

        def setCurrentIndex(self, index):
            self._combo_index = index

        def currentIndex(self):
            return self._combo_index

        def setValue(self, value):
            self._value = float(value)
            if not self.blocked:
                emitted.append(("value", value))

        def value(self):
            return self._value

    device = object.__new__(module.PSUDevice)
    device._manualPanelApplyTimer = FakeTimer()
    device.controller = types.SimpleNamespace(
        output_enabled_by_channel={0: True, 1: False},
        full_range_by_channel={0: True, 1: False},
        voltage_setpoint_values={0: 12.5, 1: 22.5},
        current_limit_values={0: 0.125, 1: 0.25},
    )
    device.manualPanelControls = {
        0: {
            "output_enabled": FakeControl(),
            "full_range": FakeControl(combo_index=1),
            "voltage": FakeControl(),
            "current_limit": FakeControl(),
        },
        1: {
            "output_enabled": FakeControl(checked=True),
            "full_range": FakeControl(combo_index=0),
            "voltage": FakeControl(),
            "current_limit": FakeControl(),
        },
    }

    module.PSUDevice._sync_manual_panel_from_controller(device)

    assert device._manualPanelApplyTimer.stopped is True
    assert emitted == []
    assert device.manualPanelControls[0]["output_enabled"].isChecked() is True
    assert device.manualPanelControls[1]["output_enabled"].isChecked() is False
    assert device.manualPanelControls[0]["full_range"].currentIndex() == 0
    assert device.manualPanelControls[1]["full_range"].currentIndex() == 1
    assert device.manualPanelControls[0]["voltage"].value() == 12.5
    assert device.manualPanelControls[1]["voltage"].value() == 22.5
    assert device.manualPanelControls[0]["current_limit"].value() == 0.125
    assert device.manualPanelControls[1]["current_limit"].value() == 0.25


def test_manual_panel_change_applies_values_immediately():
    module = _load_module()
    calls = []

    class FakeTimer:
        def __init__(self):
            self.stopped = False

        def stop(self):
            self.stopped = True

    class FakeControl:
        def __init__(self, *, checked=False, value=0.0, combo_index=0):
            self.checked = checked
            self._value = value
            self._combo_index = combo_index

        def isChecked(self):
            return self.checked

        def currentIndex(self):
            return self._combo_index

        def value(self):
            return self._value

    controller = types.SimpleNamespace(
        device=object(),
        initialized=True,
        initializing=False,
        transitioning=False,
        full_range_supported_by_channel={0: True, 1: True},
        applyManualStateFromThread=lambda state, parallel=True: calls.append(
            (state, parallel)
        ),
    )
    device = object.__new__(module.PSUDevice)
    device.controller = controller
    device._manualPanelApplyTimer = FakeTimer()
    device.manualPanelControls = {
        0: {
            "output_enabled": FakeControl(checked=True),
            "full_range": FakeControl(combo_index=0),
            "voltage": FakeControl(value=10.0),
            "current_limit": FakeControl(value=0.1),
        },
        1: {
            "output_enabled": FakeControl(checked=False),
            "full_range": FakeControl(combo_index=1),
            "voltage": FakeControl(value=20.0),
            "current_limit": FakeControl(value=0.2),
        },
    }

    module.PSUDevice._manual_panel_changed(device)

    assert device._manualPanelApplyTimer.stopped is True
    assert calls == [
        (
            {
                "output_enabled": {0: True, 1: False},
                "full_range_enabled": {0: True, 1: False},
                "voltage_values": {0: 10.0, 1: 20.0},
                "current_limit_values": {0: 0.1, 1: 0.2},
            },
            True,
        )
    ]


def test_manual_panel_numeric_change_is_debounced_until_timer_fires():
    module = _load_module()
    calls = []

    class FakeTimer:
        def __init__(self):
            self.starts = []

        def start(self, timeout_ms):
            self.starts.append(timeout_ms)

    class FakeControl:
        def __init__(self, *, checked=False, value=0.0, combo_index=0):
            self.checked = checked
            self._value = value
            self._combo_index = combo_index

        def isChecked(self):
            return self.checked

        def currentIndex(self):
            return self._combo_index

        def value(self):
            return self._value

    controller = types.SimpleNamespace(
        device=object(),
        initialized=True,
        initializing=False,
        transitioning=False,
        full_range_supported_by_channel={0: True, 1: True},
        applyManualStateFromThread=lambda state, parallel=True: calls.append(
            (state, parallel)
        ),
    )
    device = object.__new__(module.PSUDevice)
    device.controller = controller
    device._manualPanelApplyTimer = FakeTimer()
    device.manualPanelControls = {
        0: {
            "output_enabled": FakeControl(checked=True),
            "full_range": FakeControl(combo_index=0),
            "voltage": FakeControl(value=10.0),
            "current_limit": FakeControl(value=0.1),
        },
        1: {
            "output_enabled": FakeControl(checked=False),
            "full_range": FakeControl(combo_index=1),
            "voltage": FakeControl(value=20.0),
            "current_limit": FakeControl(value=0.2),
        },
    }

    module.PSUDevice._manual_panel_changed(device, debounce=True)

    assert calls == []
    assert device._manualPanelApplyTimer.starts == [
        module._PSU_MANUAL_NUMERIC_DEBOUNCE_MS,
    ]

    module.PSUDevice._apply_manual_panel_state(device)

    assert calls == [
        (
            {
                "output_enabled": {0: True, 1: False},
                "full_range_enabled": {0: True, 1: False},
                "voltage_values": {0: 10.0, 1: 20.0},
                "current_limit_values": {0: 0.1, 1: 0.2},
            },
            True,
        )
    ]


def test_manual_apply_queue_keeps_only_latest_pending_state(monkeypatch):
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

    parent = types.SimpleNamespace(name="PSU", getChannels=lambda: [])
    controller = module.PSUController(parent)
    controller.applyManualState = lambda state: applied.append(state)
    monkeypatch.setattr(module, "Thread", FakeThread)

    controller.applyManualStateFromThread(
        {"voltage_values": {0: 1.0}},
        parallel=True,
    )
    controller.applyManualStateFromThread(
        {"voltage_values": {0: 2.0}},
        parallel=True,
    )
    controller.applyManualStateFromThread(
        {"voltage_values": {0: 3.0}},
        parallel=True,
    )

    assert len(created_threads) == 1

    created_threads[0].target()

    assert applied == [{"voltage_values": {0: 3.0}}]
    assert controller._manual_apply_worker_running is False


def test_toggle_on_uses_config_startup_only():
    module = _load_module()
    calls = []

    class FakeDevice:
        def initialize(self, timeout_s=None, **kwargs):
            calls.append(("initialize", timeout_s, kwargs))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": True,
                "output_enabled": (True, True),
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": True,
                        "voltage": {"measured_v": 10.0, "set_v": 10.0},
                        "current": {"measured_a": 0.1, "set_a": 0.1},
                    },
                    {
                        "channel": 1,
                        "enabled": True,
                        "voltage": {"measured_v": 20.0, "set_v": 20.0},
                        "current": {"measured_a": 0.2, "set_a": 0.2},
                    },
                ],
            }

        def __getattr__(self, name):
            if name.startswith("set_channel_") or name in {
                "set_output_enabled",
                "set_device_enabled",
            }:
                raise AssertionError(f"Unexpected live override call: {name}")
            raise AttributeError(name)

    parent = types.SimpleNamespace(
        startup_timeout_s=9.0,
        standby_config=1,
        operating_config=2,
        isOn=lambda: True,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller._begin_transition(True)
    controller.toggleOn()

    assert calls == [
        ("initialize", 9.0, {"standby_config": 1, "operating_config": 2}),
        ("collect_housekeeping", 5.0),
    ]


def test_toggle_on_without_startup_configs_enters_manual_standby():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": False,
                "output_enabled": (False, False),
                "main_state": {"name": "STATE_ERR_PSU_DIS"},
                "device_state": {"flags": ["DEVST_PSU_DIS"]},
                "channels": [],
            }

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        standby_config=-1,
        operating_config=-1,
        isOn=lambda: True,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))
    controller._begin_transition(True)

    controller.toggleOn()

    assert calls == [
        ("set_output_enabled", False, False, 9.0),
        ("set_device_enabled", True, 9.0),
        ("collect_housekeeping", 5.0),
        ("collect_housekeeping", 5.0),
    ]
    assert controller.loaded_state_text == "Manual outputs OFF"
    assert parent.loaded_state_text == "Manual outputs OFF"
    assert printed == [
        (
            "PSU communication initialized without a startup config. "
            "Outputs remain OFF until manual values are applied.",
            None,
        )
    ]


def test_apply_manual_state_updates_outputs_ranges_and_limits():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_output_full_range(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_full_range", ch0, ch1, timeout_s))

        def set_channel_voltage(self, channel, value, timeout_s=None):
            calls.append(("set_channel_voltage", channel, value, timeout_s))

        def set_channel_current(self, channel, value, timeout_s=None):
            calls.append(("set_channel_current", channel, value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": True,
                "output_enabled": (True, False),
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": True,
                        "full_range": {"enabled": True, "supported": True},
                        "voltage": {"measured_v": 10.0, "set_v": 10.0},
                        "current": {"measured_a": 0.1, "set_a": 0.25},
                    },
                    {
                        "channel": 1,
                        "enabled": False,
                        "full_range": {"enabled": False, "supported": True},
                        "voltage": {"measured_v": 20.0, "set_v": 20.0},
                        "current": {"measured_a": 0.0, "set_a": 0.5},
                    },
                ],
            }

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=5.0,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.applyManualState(
        {
            "output_enabled": {0: True, 1: False},
            "full_range_enabled": {0: True, 1: False},
            "voltage_values": {0: 10.0, 1: 20.0},
            "current_limit_values": {0: 0.25, 1: 0.5},
        }
    )

    assert calls == [
        ("set_output_enabled", False, False, 9.0),
        ("set_output_full_range", True, False, 9.0),
        ("set_channel_voltage", 0, 10.0, 9.0),
        ("set_channel_current", 0, 0.25, 9.0),
        ("set_channel_voltage", 1, 20.0, 9.0),
        ("set_channel_current", 1, 0.5, 9.0),
        ("set_device_enabled", True, 9.0),
        ("set_output_enabled", True, False, 9.0),
        ("collect_housekeeping", 5.0),
    ]
    assert controller.loaded_state_text == "Manual (unsaved)"
    assert parent.loaded_state_text == "Manual (unsaved)"
    assert printed == [("Applied PSU manual values.", None)]


def test_apply_manual_state_does_not_switch_range_when_range_is_unchanged(monkeypatch):
    module = _load_module()
    calls = []

    class FakeDevice:
        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_output_full_range(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_full_range", ch0, ch1, timeout_s))

        def set_channel_voltage(self, channel, value, timeout_s=None):
            calls.append(("set_channel_voltage", channel, value, timeout_s))

        def set_channel_current(self, channel, value, timeout_s=None):
            calls.append(("set_channel_current", channel, value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=5.0,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        loaded_state_text="",
    )
    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.full_range_by_channel = {0: False, 1: False}
    monkeypatch.setattr(
        controller,
        "_await_discharge_before_range_switch",
        lambda *_args, **_kwargs: calls.append(("await_discharge",)),
    )
    monkeypatch.setattr(controller, "_verify_manual_state_unlocked", lambda **_kwargs: [])
    monkeypatch.setattr(controller, "_verify_output_enable_state_unlocked", lambda **_kwargs: None)
    monkeypatch.setattr(controller, "_update_state", lambda: None)

    controller.applyManualState(
        {
            "output_enabled": {0: False, 1: False},
            "full_range_enabled": {0: False, 1: False},
            "voltage_values": {0: 0.0, 1: 0.0},
            "current_limit_values": {0: 0.0, 1: 0.0},
        }
    )

    assert ("await_discharge",) not in calls
    assert not any(call[0] == "set_output_full_range" for call in calls)


def test_apply_manual_state_waits_before_switching_changed_range(monkeypatch):
    module = _load_module()
    calls = []

    class FakeDevice:
        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_output_full_range(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_full_range", ch0, ch1, timeout_s))

        def set_channel_voltage(self, channel, value, timeout_s=None):
            calls.append(("set_channel_voltage", channel, value, timeout_s))

        def set_channel_current(self, channel, value, timeout_s=None):
            calls.append(("set_channel_current", channel, value, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=5.0,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        loaded_state_text="",
    )
    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.full_range_by_channel = {0: False, 1: False}
    monkeypatch.setattr(
        controller,
        "_await_discharge_before_range_switch",
        lambda *_args, **_kwargs: calls.append(("await_discharge",)),
    )
    monkeypatch.setattr(controller, "_verify_manual_state_unlocked", lambda **_kwargs: [])
    monkeypatch.setattr(controller, "_verify_output_enable_state_unlocked", lambda **_kwargs: None)
    monkeypatch.setattr(controller, "_update_state", lambda: None)

    controller.applyManualState(
        {
            "output_enabled": {0: False, 1: False},
            "full_range_enabled": {0: True, 1: False},
            "voltage_values": {0: 0.0, 1: 0.0},
            "current_limit_values": {0: 0.0, 1: 0.0},
        }
    )

    assert ("await_discharge",) in calls
    assert ("set_output_full_range", True, False, 9.0) in calls


def test_apply_manual_state_warns_but_continues_on_setpoint_readback_lag():
    module = _load_module()
    printed = []

    class FakeDevice:
        def set_output_enabled(self, _ch0, _ch1, timeout_s=None):
            return None

        def set_channel_voltage(self, _channel, _value, timeout_s=None):
            return None

        def set_channel_current(self, _channel, _value, timeout_s=None):
            return None

        def get_channel_current_limits(self, _channel, timeout_s=None):
            return 0.0, 1.0

        def set_device_enabled(self, _enabled, timeout_s=None):
            return None

        def collect_housekeeping(self, timeout_s=None):
            return {
                "device_enabled": False,
                "output_enabled": (False, False),
                "main_state": {"name": "ST_STBY"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [],
            }

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=5.0,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        loaded_state_text="",
    )
    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.applyManualState(
        {
            "output_enabled": {0: False, 1: False},
            "voltage_values": {0: 0.0, 1: 0.0},
            "current_limit_values": {0: 0.01, 1: 0.0},
        }
    )

    assert printed[0][1] == module.PRINT.WARNING
    assert "current limit readback 0 A does not match requested 0.01 A" in printed[0][0]
    assert printed[-1] == ("Applied PSU manual values.", None)


def test_shutdown_with_config_still_forces_safe_disable_before_disconnect():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def load_config(self, config_index, timeout_s=None):
            calls.append(("load_config", config_index, timeout_s))

        def set_channel_current(self, channel, value, timeout_s=None):
            calls.append(("set_channel_current", channel, value, timeout_s))

        def set_channel_voltage(self, channel, value, timeout_s=None):
            calls.append(("set_channel_voltage", channel, value, timeout_s))

        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": False,
                "output_enabled": (False, False),
                "main_state": {"name": "STATE_ERR_PSU_DIS"},
                "device_state": {"flags": ["DEVST_PSU_DIS"]},
                "channels": [],
            }

        def disconnect(self):
            calls.append(("disconnect",))

        def close(self):
            calls.append(("close",))

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=2.5,
        shutdown_config=5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        available_configs_text="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    result = controller.shutdownCommunication()

    assert result is True
    assert calls == [
        ("load_config", 5, 9.0),
        ("set_channel_current", 0, 0.0, 9.0),
        ("set_channel_voltage", 0, 0.0, 9.0),
        ("set_channel_current", 1, 0.0, 9.0),
        ("set_channel_voltage", 1, 0.0, 9.0),
        ("set_output_enabled", False, False, 9.0),
        ("set_device_enabled", False, 9.0),
        ("collect_housekeeping", 2.5),
        ("disconnect",),
        ("close",),
    ]
    assert controller.main_state == "Disconnected"
    assert controller.device is None
    assert printed == [
        ("Starting PSU shutdown sequence.", None),
        ("PSU shutdown sequence completed.", None),
    ]


def test_shutdown_unconfirmed_keeps_attention_state_after_disconnect():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def set_channel_current(self, channel, value, timeout_s=None):
            calls.append(("set_channel_current", channel, value, timeout_s))

        def set_channel_voltage(self, channel, value, timeout_s=None):
            calls.append(("set_channel_voltage", channel, value, timeout_s))

        def set_output_enabled(self, ch0, ch1, timeout_s=None):
            calls.append(("set_output_enabled", ch0, ch1, timeout_s))

        def set_device_enabled(self, enabled, timeout_s=None):
            calls.append(("set_device_enabled", enabled, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": True,
                "output_enabled": (True, False),
                "main_state": {"name": "STATE_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [],
            }

        def disconnect(self):
            calls.append(("disconnect",))

        def close(self):
            calls.append(("close",))

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        poll_timeout_s=2.5,
        shutdown_config=-1,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        available_configs_text="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    result = controller.shutdownCommunication()

    assert result is False
    assert calls == [
        ("set_channel_current", 0, 0.0, 9.0),
        ("set_channel_voltage", 0, 0.0, 9.0),
        ("set_channel_current", 1, 0.0, 9.0),
        ("set_channel_voltage", 1, 0.0, 9.0),
        ("set_output_enabled", False, False, 9.0),
        ("set_device_enabled", False, 9.0),
        ("collect_housekeeping", 2.5),
        ("disconnect",),
        ("close",),
    ]
    assert controller.main_state == module._PSU_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.hardware_main_state == module._PSU_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.output_state_summary == "Unknown"
    assert controller.device is None
    assert printed == [
        ("Starting PSU shutdown sequence.", None),
        (
            "PSU shutdown could not be confirmed before disconnect: "
            "outputs still enabled (CH0=ON, CH1=OFF).",
            module.PRINT.ERROR,
        ),
    ]


def test_save_current_config_refreshes_available_list_and_loaded_state():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def __init__(self):
            self.saved = False

        def save_config(
            self,
            config_number,
            *,
            name=None,
            active=None,
            valid=None,
            timeout_s=None,
        ):
            calls.append(
                ("save_config", config_number, name, active, valid, timeout_s)
            )
            self.saved = True

        def list_configs(self, timeout_s=None):
            calls.append(("list_configs", timeout_s))
            if not self.saved:
                return []
            return [
                {"index": 7, "name": "Manual 10 V / 1 A", "active": True, "valid": True},
            ]

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        connect_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        available_configs_text="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.saveCurrentConfig(
        config_index=7,
        config_name="Manual 10 V / 1 A",
        active=True,
        valid=True,
    )

    assert calls == [
        ("list_configs", 2.5),
        ("save_config", 7, "Manual 10 V / 1 A", True, True, 9.0),
        ("list_configs", 2.5),
    ]
    assert controller.available_configs == [
        {"index": 7, "name": "Manual 10 V / 1 A", "active": True, "valid": True},
    ]
    assert controller.loaded_state_text == "Config 7: Manual 10 V / 1 A"
    assert parent.available_configs_text == "7:Manual 10 V / 1 A"
    assert parent.loaded_state_text == "Config 7: Manual 10 V / 1 A"
    assert printed == [("Saved PSU config 7.", None)]


def test_save_current_config_refuses_existing_config_slot():
    module = _load_module()
    calls = []
    printed = []

    class FakeDevice:
        def save_config(
            self,
            config_number,
            *,
            name=None,
            active=None,
            valid=None,
            timeout_s=None,
        ):
            calls.append(
                ("save_config", config_number, name, active, valid, timeout_s)
            )

        def list_configs(self, timeout_s=None):
            calls.append(("list_configs", timeout_s))
            return [
                {"index": 7, "name": "Existing", "active": True, "valid": True},
            ]

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        connect_timeout_s=2.5,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
        available_configs_text="",
        loaded_state_text="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.saveCurrentConfig(
        config_index=7,
        config_name="Overwrite attempt",
        active=True,
        valid=True,
    )

    assert calls == [("list_configs", 2.5)]
    assert parent.available_configs_text == "7:Existing"
    assert printed == [
        (
            "Cannot save PSU config 7: this slot already exists. Choose an empty slot.",
            module.PRINT.WARNING,
        )
    ]


def test_load_operating_config_now_loads_selected_psu_config():
    module = _load_module()
    calls = []
    printed = []
    sync_calls = []

    class FakeDevice:
        def load_config(self, config_number, timeout_s=None):
            calls.append(("load_config", config_number, timeout_s))

        def collect_housekeeping(self, timeout_s=None):
            calls.append(("collect_housekeeping", timeout_s))
            return {
                "device_enabled": True,
                "output_enabled": (True, True),
                "main_state": {"name": "ST_ON"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "channels": [
                    {
                        "channel": 0,
                        "enabled": True,
                        "full_range": {"enabled": True, "supported": True},
                        "voltage": {"measured_v": 12.0, "set_v": 12.5},
                        "current": {"measured_a": 0.05, "set_a": 0.125},
                    },
                    {
                        "channel": 1,
                        "enabled": True,
                        "full_range": {"enabled": False, "supported": True},
                        "voltage": {"measured_v": 22.0, "set_v": 22.5},
                        "current": {"measured_a": 0.1, "set_a": 0.25},
                    },
                ],
            }

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        operating_config=7,
        isOn=lambda: True,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.available_configs = [
        {"index": 1, "name": "Standby", "active": True, "valid": True},
        {"index": 7, "name": "Operate 5 kV", "active": True, "valid": True},
    ]
    controller._manual_apply_pending_state = {"voltage_values": {0: 99.0}}
    parent._sync_manual_panel_from_controller = lambda: sync_calls.append(
        (
            dict(controller.voltage_setpoint_values),
            dict(controller.current_limit_values),
        )
    )
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.loadOperatingConfigNow()

    assert calls == [
        ("load_config", 7, 9.0),
        ("collect_housekeeping", 5.0),
    ]
    assert sync_calls == [
        ({0: 12.5, 1: 22.5}, {0: 0.125, 1: 0.25}),
        ({0: 12.5, 1: 22.5}, {0: 0.125, 1: 0.25}),
        ({0: 12.5, 1: 22.5}, {0: 0.125, 1: 0.25}),
        ({0: 12.5, 1: 22.5}, {0: 0.125, 1: 0.25}),
    ]
    assert controller._manual_apply_pending_state is None
    assert printed == [("Loaded PSU config 7.", None)]


def test_load_operating_config_now_rejects_invalid_config_slot():
    module = _load_module()
    printed = []

    parent = types.SimpleNamespace(
        name="PSU",
        startup_timeout_s=9.0,
        operating_config=7,
        isOn=lambda: True,
        getChannels=lambda: [],
        main_state="",
        output_summary="",
    )

    controller = module.PSUController(parent)
    controller.device = object()
    controller.initialized = True
    controller.available_configs = [
        {"index": 7, "name": "Operate 5 kV", "active": True, "valid": False},
    ]
    controller.print = lambda message, flag=None: printed.append((message, flag))

    controller.loadOperatingConfigNow()

    assert printed == [
        (
            "Cannot load PSU config: config 7 is marked invalid on the controller.",
            module.PRINT.WARNING,
        )
    ]


def test_run_initialization_disables_process_backend_for_plugin_runtime(monkeypatch):
    module = _load_module()
    captured_kwargs = {}

    class FakeDevice:
        def __init__(self, **kwargs):
            captured_kwargs.update(kwargs)
            self._process_backend_disabled_reason = ""

        def connect(self, timeout_s=None):
            captured_kwargs["connect_timeout_s"] = timeout_s
            return True

        def list_configs(self, timeout_s=None):
            captured_kwargs["list_configs_timeout_s"] = timeout_s
            return []

        def collect_housekeeping(self, timeout_s=None):
            captured_kwargs["collect_housekeeping_timeout_s"] = timeout_s
            return {
                "main_state": {"name": "ST_STBY"},
                "device_state": {"flags": ["DEVICE_OK"]},
                "device_enabled": False,
                "output_enabled": (False, False),
                "channels": [],
            }

    monkeypatch.setattr(module, "_get_psu_driver_class", lambda: FakeDevice)

    parent = types.SimpleNamespace(
        name="PSU",
        com=3,
        baudrate=230400,
        connect_timeout_s=5.0,
        getChannels=lambda: [],
        _sync_channels=lambda: None,
        main_state="",
        output_summary="",
        available_configs_text="",
    )

    controller = module.PSUController(parent)
    controller.runInitialization()

    assert captured_kwargs["device_id"] == "psu_com3"
    assert captured_kwargs["logger"] is not None
    assert captured_kwargs["allow_process_backend"] is False
    assert captured_kwargs["connect_timeout_s"] == 5.0


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port locks the COM port for the process lifetime; the
    init-failure guidance must tell the operator to restart ESIBD Explorer
    instead of letting retries loop on a confusing '-2 (Error opening port)'."""
    module = _load_module()
    controller = module.PSUController(types.SimpleNamespace(com=10))

    fatal_exc = RuntimeError(
        "PSU DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The PSU instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10

    retry_exc = RuntimeError("PSU open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "locked the COM port" in guidance2
    assert controller._poisoned_com == 10


def test_init_failure_guidance_silent_without_prior_poisoning():
    module = _load_module()
    controller = module.PSUController(types.SimpleNamespace(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("PSU open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None
