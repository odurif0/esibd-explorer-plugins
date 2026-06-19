"""Behavior checks for the standalone ESIBD Explorer DMMR plugin."""

from __future__ import annotations

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
    / "dmmr"
    / "dmmr_plugin.py"
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
        WIDGET = "Widget"

    class Channel:
        COLLAPSE = "Collapse"
        NAME = "Name"
        ACTIVE = "Active"
        DISPLAY = "Display"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"
        SCALING = "Scaling"
        MIN = "Min"
        MAX = "Max"
        OPTIMIZE = "Optimize"

    class _Signal:
        def emit(self, *args, **kwargs):
            self.last_emit = (args, kwargs)

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent
            self.lock = threading.Lock()
            self.signalComm = types.SimpleNamespace(
                initCompleteSignal=_Signal(),
                closeCommunicationSignal=_Signal(),
            )
            self.errorCount = 0
            self.initializing = False
            self.acquiring = False
            self.values = None
            self.print = lambda *args, **kwargs: None

        def startAcquisition(self):
            self.acquiring = True

        def stopAcquisition(self):
            self.acquiring = False

        def toggleOn(self):
            return None

        def closeCommunication(self):
            return None

    class ToolButton:
        pass

    class LabviewDoubleSpinBox:
        def __init__(self, indicator=False, displayDecimals=2):
            self.indicator = indicator
            self.displayDecimals = displayDecimals
            self.NAN = "NaN"
            self._decimals = displayDecimals

        def setDecimals(self, value):
            self._decimals = value

        def decimals(self):
            return self._decimals

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
    core.LabviewDoubleSpinBox = LabviewDoubleSpinBox
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
        or name.startswith("_esibd_bundled_dmmr_runtime")
        or name == "dmmr_plugin_behavior_test"
    ]:
        sys.modules.pop(name, None)


def _load_module():
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("dmmr_plugin_behavior_test", PLUGIN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_config_is_replaced_from_detected_modules():
    module = _load_module()
    default_item = {
        "Module": "0",
        "Real": True,
        "Enabled": True,
    }

    bootstrap_items = [
        {"Name": f"DMMR{index}", "Module": 0, "Real": True, "Enabled": True}
        for index in range(1, 7)
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=bootstrap_items,
        detected_modules=[2, 5],
        device_name="DMMR",
        default_item=default_item,
    )

    assert synced_items == [
        {"Name": "DMMR_M02", "Module": "2", "Real": True, "Enabled": True},
        {"Name": "DMMR_M05", "Module": "5", "Real": True, "Enabled": True},
    ]
    assert log_entries == [("DMMR bootstrap config replaced from hardware scan.", None)]


def test_existing_config_is_merged_and_duplicates_are_neutralized():
    module = _load_module()

    current_items = [
        {"Name": "Keep", "Module": 1, "Real": True, "Enabled": True},
        {"Name": "MissingLater", "Module": 3, "Real": True, "Enabled": False},
        {"Name": "Duplicate", "Module": "1", "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[1, 2],
        device_name="DMMR",
    )

    assert synced_items[0]["Real"] is True
    assert synced_items[1]["Real"] is False
    assert synced_items[2]["Real"] is False
    assert synced_items[3] == {
        "Name": "DMMR_M02",
        "Module": "2",
        "Real": True,
        "Enabled": True,
    }
    assert ("Added generic DMMR channels for detected modules: 2", None) in log_entries
    assert ("Marked DMMR channels virtual because modules are absent: 3", None) in log_entries
    assert (
        "Duplicate DMMR mapping detected for module 1: Duplicate",
        module.PRINT.WARNING,
    ) in log_entries


def test_controller_read_numbers_polls_module_currents():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def get_module_current(self, module, timeout_s=None):
            return self.NO_ERR, module * 1e-12, module + 10

    class FakeChannel:
        def __init__(self, module):
            self._module = module
            self.real = True
            self.enabled = True

        def module_address(self):
            return self._module

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        isOn=lambda: True,
        getChannels=lambda: [FakeChannel(1), FakeChannel(2)],
        getConfiguredModules=lambda: [1, 2],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.detected_module_ids = [1, 2]

    controller.readNumbers()

    assert controller.main_state == "ST_ON"
    assert controller.values == {1: 1e-12, 2: 2e-12}
    assert controller.device_state_summary == "DEVICE_OK"
    assert controller.voltage_state_summary == "VS_3V3_OK, VS_5V0_OK, VS_12V_OK"


def test_controller_read_numbers_does_not_block_on_zero_ready_flags():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0
        MEAS_CUR_RDY = 1

        def __init__(self):
            self.current_calls = []

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def get_module_ready_flags(self, module, timeout_s=None):
            return self.NO_ERR, 0

        def get_module_current(self, module, timeout_s=None):
            self.current_calls.append((module, timeout_s))
            return self.NO_ERR, module * 1e-12, module + 10

    class FakeChannel:
        def __init__(self, module):
            self._module = module
            self.real = True
            self.enabled = True

        def module_address(self):
            return self._module

    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        isOn=lambda: True,
        getChannels=lambda: [FakeChannel(1), FakeChannel(2)],
        getConfiguredModules=lambda: [1, 2],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.detected_module_ids = [1, 2]

    controller.readNumbers()

    assert controller.values[1] == 1e-12
    assert controller.values[2] == 2e-12
    assert controller.device.current_calls == [(1, 2.5), (2, 2.5)]


def test_controller_read_numbers_recovers_from_automatic_current_mode():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0
        ERR_COMMAND_WRONG = -13

        def __init__(self):
            self.current_calls = []
            self.automatic_current = True

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def set_automatic_current(self, enabled, timeout_s=None):
            assert enabled is False
            self.automatic_current = False
            return self.NO_ERR

        def get_module_current(self, module, timeout_s=None):
            self.current_calls.append((module, timeout_s, self.automatic_current))
            if self.automatic_current:
                return self.ERR_COMMAND_WRONG, np.nan, 0
            return self.NO_ERR, module * 1e-12, module + 10

    class FakeChannel:
        def __init__(self, module):
            self._module = module
            self.real = True
            self.enabled = True

        def module_address(self):
            return self._module

    logs = []
    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.5,
        isOn=lambda: True,
        getChannels=lambda: [FakeChannel(3)],
        getConfiguredModules=lambda: [3],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.detected_module_ids = [3]
    controller.print = lambda message, flag=None: logs.append((message, flag))

    controller.readNumbers()

    assert controller.values == {3: 3e-12}
    assert controller.errorCount == 0
    assert controller.device.current_calls == [
        (3, 2.5, True),
        (3, 2.5, False),
    ]
    assert logs == [
        (
            "DMMR automatic current mode was active; switched back to manual module polling.",
            module.PRINT.WARNING,
        )
    ]


def test_controller_toggle_on_enables_measurement():
    module = _load_module()
    calls = []

    class FakeDevice:
        NO_ERR = 0

        def set_enable(self, enabled, timeout_s=None):
            calls.append(("set_enable", enabled, timeout_s))
            return self.NO_ERR

        def set_automatic_current(self, enabled, timeout_s=None):
            calls.append(("set_automatic_current", enabled, timeout_s))
            return self.NO_ERR

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def format_status(self, status):
            return str(status)

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: True,
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()

    controller.toggleOn()

    assert calls == [
        ("set_enable", True, 7.0),
        ("set_automatic_current", False, 7.0),
    ]
    assert controller.acquiring is True


def test_controller_toggle_on_enables_module_auto_range_for_active_modules():
    module = _load_module()
    calls = []

    class FakeDevice:
        NO_ERR = 0

        def set_enable(self, enabled, timeout_s=None):
            calls.append(("set_enable", enabled, timeout_s))
            return self.NO_ERR

        def set_module_auto_range(self, module, enabled, timeout_s=None):
            calls.append(("set_module_auto_range", module, enabled, timeout_s))
            return self.NO_ERR

        def set_automatic_current(self, enabled, timeout_s=None):
            calls.append(("set_automatic_current", enabled, timeout_s))
            return self.NO_ERR

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def format_status(self, status):
            return str(status)

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: True,
        getConfiguredModules=lambda: [1, 3],
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.detected_module_ids = [1, 2, 3]

    controller.toggleOn()

    assert calls == [
        ("set_enable", True, 7.0),
        ("set_module_auto_range", 1, True, 7.0),
        ("set_module_auto_range", 3, True, 7.0),
        ("set_automatic_current", False, 7.0),
    ]
    assert controller.acquiring is True


def test_controller_toggle_off_syncs_status_back_to_gui():
    module = _load_module()
    calls = []
    sync_calls = []

    class FakeDevice:
        NO_ERR = 0

        def set_enable(self, enabled, timeout_s=None):
            calls.append(("set_enable", enabled, timeout_s))
            return self.NO_ERR

        def set_automatic_current(self, enabled, timeout_s=None):
            calls.append(("set_automatic_current", enabled, timeout_s))
            return self.NO_ERR

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def format_status(self, status):
            return str(status)

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: False,
        _update_status_widgets=lambda: sync_calls.append("sync"),
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.detected_modules_text = "3"

    controller.toggleOn()

    assert calls == [
        ("set_automatic_current", False, 7.0),
        ("set_enable", False, 7.0),
    ]
    assert parent.main_state == "ST_ON"
    assert sync_calls == ["sync"]


def test_controller_toggle_off_failure_restores_on_ui_state():
    module = _load_module()
    ui_states = []
    log_messages = []

    class FakeDevice:
        NO_ERR = 0

        def set_automatic_current(self, enabled, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def format_status(self, status):
            return str(status)

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: False,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.print = lambda message, flag=None: log_messages.append((message, flag))

    controller.toggleOn()

    assert ui_states == [True]
    assert any(
        "Failed to toggle DMMR acquisition:" in message
        for message, _flag in log_messages
    )


def test_controller_marks_communication_lost_and_requests_forced_close_when_transport_is_unusable():
    module = _load_module()
    ui_states = []

    class FakeDevice:
        def get_state(self, timeout_s=None):
            raise RuntimeError(
                "DMMR DLL call timed out during 'get_state'. "
                "The DMMR instance is now marked unusable."
            )

        def get_device_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_voltage_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_temperature_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.transitioning = True
    controller.transition_target_on = True

    controller._update_state()

    assert controller.main_state == module._DMMR_COMMUNICATION_LOST_STATE
    assert controller._forced_close_state == module._DMMR_COMMUNICATION_LOST_STATE
    assert controller.acquiring is False
    assert controller.initialized is False
    assert controller.device is None
    assert controller.transitioning is False
    assert controller.transition_target_on is None
    assert ui_states == [False]
    assert controller.signalComm.closeCommunicationSignal.last_emit == ((), {})


def test_controller_read_numbers_acquires_lock_for_state_and_module_polling():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def get_module_current(self, module, timeout_s=None):
            self.calls.append((module, timeout_s))
            return self.NO_ERR, 3.2e-12, 13

    class FakeTimeoutLock:
        def __init__(self):
            self.calls = []

        class _Section:
            def __init__(self, owner, timeout, timeout_message, already_acquired):
                self.owner = owner
                self.payload = (timeout, timeout_message, already_acquired)

            def __enter__(self):
                self.owner.calls.append(self.payload)
                return True

            def __exit__(self, exc_type, exc, tb):
                return False

        def acquire_timeout(self, timeout, timeoutMessage="", already_acquired=False):
            return self._Section(self, timeout, timeoutMessage, already_acquired)

    class FakeChannel:
        def __init__(self, module):
            self._module = module
            self.real = True
            self.enabled = True

        def module_address(self):
            return self._module

    device = FakeDevice()
    lock = FakeTimeoutLock()
    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        isOn=lambda: True,
        getChannels=lambda: [FakeChannel(3)],
        getConfiguredModules=lambda: [3],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.device = device
    controller.lock = lock
    controller.initialized = True
    controller.acquiring = True
    controller.detected_module_ids = [3]

    controller.readNumbers()

    assert lock.calls == [
        (1, "Could not acquire lock to refresh the DMMR state.", False),
        (1, "Could not acquire lock to read DMMR module 3.", False),
    ]
    assert device.calls == [(3, 2.5)]
    assert controller.values == {3: 3.2e-12}


def test_controller_marks_communication_lost_after_repeated_generic_state_failures():
    module = _load_module()
    ui_states = []

    class FakeDevice:
        def get_state(self, timeout_s=None):
            raise RuntimeError("serial timeout while reading state")

        def get_device_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def get_voltage_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def get_temperature_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def disconnect(self):
            return True

        def close(self):
            return None

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.transitioning = True
    controller.transition_target_on = True

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()

    assert controller.main_state == module._DMMR_COMMUNICATION_LOST_STATE
    assert controller.device is None
    assert controller.acquiring is False
    assert controller.signalComm.closeCommunicationSignal.last_emit == ((), {})
    assert ui_states == [False]


def test_close_communication_preserves_forced_communication_lost_state():
    module = _load_module()

    parent = types.SimpleNamespace(
        isOn=lambda: False,
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller._forced_close_state = module._DMMR_COMMUNICATION_LOST_STATE

    controller.closeCommunication()

    assert controller.main_state == module._DMMR_COMMUNICATION_LOST_STATE
    assert controller.device_state_summary == "Unknown"
    assert controller.voltage_state_summary == "Unknown"
    assert controller.temperature_state_summary == "Unknown"


def test_controller_read_numbers_keeps_partial_results_on_timeout():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def get_module_current(self, module, timeout_s=None):
            self.calls.append((module, timeout_s))
            if module == 2:
                raise TimeoutError("module read timed out")
            return self.NO_ERR, module * 1e-12, module + 10

    class FakeChannel:
        def __init__(self, module):
            self._module = module
            self.real = True
            self.enabled = True

        def module_address(self):
            return self._module

    logs = []
    parent = types.SimpleNamespace(
        poll_timeout_s=2.5,
        isOn=lambda: True,
        getChannels=lambda: [FakeChannel(1), FakeChannel(2), FakeChannel(3)],
        getConfiguredModules=lambda: [1, 2, 3],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.detected_module_ids = [1, 2, 3]
    controller.print = lambda message, flag=None: logs.append((message, flag))

    controller.readNumbers()

    # A TimeoutError here means transient controller-lock contention (in
    # production a real device-call timeout raises RuntimeError and is handled
    # by except-Exception). It is not a fault: skip this module silently,
    # keep its last-good value (NaN here on the first poll), and still read
    # the remaining modules.
    assert controller.device.calls == [(1, 2.5), (2, 2.5), (3, 2.5)]
    assert controller.values[1] == 1e-12
    assert np.isnan(controller.values[2])
    assert controller.values[3] == 3e-12
    assert controller.errorCount == 0
    assert logs == []


def test_controller_lock_section_rejects_unsupported_lock_type():
    module = _load_module()

    controller = module.DMMRController(types.SimpleNamespace())
    controller.lock = object()

    with pytest.raises(TypeError, match="must provide either acquire_timeout"):
        with controller._controller_lock_section("boom"):
            pass


def test_controller_formats_open_port_timeout_with_operator_hint():
    module = _load_module()

    parent = types.SimpleNamespace(com=3)
    controller = module.DMMRController(parent)

    message = controller._format_exception(
        RuntimeError(
            "DMMR DLL call timed out during 'open_port'. "
            "The device may be powered off or unresponsive. "
            "The DMMR instance is now marked unusable."
        )
    )

    assert "RuntimeError:" in message
    assert "Selected COM3 did not respond." in message
    assert "configured COM port is correct" in message


def test_controller_formats_open_port_error_with_operator_hint():
    module = _load_module()

    parent = types.SimpleNamespace(com=3)
    controller = module.DMMRController(parent)

    message = controller._format_exception(
        RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    )

    assert "RuntimeError:" in message
    assert "Windows could not open COM3." in message
    assert "already in use" in message


def test_controller_update_values_clears_monitor_when_channel_is_disabled():
    module = _load_module()

    class FakeChannel:
        def __init__(self, module, enabled):
            self._module = module
            self.real = True
            self.enabled = enabled
            self.monitor = 99.0

        def module_address(self):
            return self._module

    channel_enabled = FakeChannel(1, True)
    channel_disabled = FakeChannel(2, False)
    parent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [channel_enabled, channel_disabled],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.values = {1: 1.5e-12, 2: 2.5e-12}

    controller.updateValues()

    assert channel_enabled.monitor == 1.5e-12
    assert np.isnan(channel_disabled.monitor)


def test_controller_update_values_resyncs_monitor_widgets():
    module = _load_module()

    class FakeChannel:
        def __init__(self, module, enabled):
            self._module = module
            self.real = True
            self.enabled = enabled
            self.monitor = 99.0
            self.sync_calls = 0

        def module_address(self):
            return self._module

        def _sync_monitor_widget(self):
            self.sync_calls += 1

    channel_enabled = FakeChannel(1, True)
    channel_disabled = FakeChannel(2, False)
    parent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [channel_enabled, channel_disabled],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.values = {1: 1.5e-12, 2: 2.5e-12}

    controller.updateValues()

    assert channel_enabled.monitor == 1.5e-12
    assert np.isnan(channel_disabled.monitor)
    assert channel_enabled.sync_calls == 1
    assert channel_disabled.sync_calls == 1


def test_controller_shutdown_failure_marks_state_unconfirmed():
    module = _load_module()

    class FakeDevice:
        def shutdown(self, timeout_s=None):
            raise RuntimeError("boom")

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: False,
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    shutdown_confirmed = controller.shutdownCommunication()

    assert shutdown_confirmed is False
    assert controller.main_state == module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.device is None
    assert controller.initialized is False


def test_controller_shutdown_success_marks_state_disconnected():
    module = _load_module()

    class FakeDevice:
        def shutdown(self, timeout_s=None):
            return None

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: True,
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    shutdown_confirmed = controller.shutdownCommunication()

    assert shutdown_confirmed is True
    assert controller.main_state == "Disconnected"
    assert controller.device is None
    assert controller.initialized is False


def test_dmmr_monitor_widget_formats_currents_with_si_prefixes():
    module = _load_module()

    widget = module._DMMRCurrentMonitorSpinBox(indicator=True, displayDecimals=3)

    assert widget.decimals() == 1000
    assert widget.textFromValue(1.5e-12) == "1.5 pA"
    assert widget.textFromValue(-2.0e-9) == "-2 nA"
    assert widget.textFromValue(np.nan) == "NaN"


def test_upgrade_monitor_widget_replaces_default_numeric_widget():
    module = _load_module()

    class FakeParameter:
        def __init__(self):
            self.widget = object()
            self.value = 1.5e-12
            self.apply_calls = 0

        def applyWidget(self):
            self.apply_calls += 1

        def getWidget(self):
            return self.widget

    parameter = FakeParameter()
    sync_calls = []
    fake_channel = types.SimpleNamespace(
        MONITOR="Monitor",
        monitor=1.5e-12,
        getParameterByName=lambda name: parameter if name == "Monitor" else None,
        _set_monitor_widget_minimum_width=lambda *args, **kwargs: None,
        _sync_monitor_widget=lambda: sync_calls.append(True),
    )

    module.DMMRChannel._upgrade_monitor_widget(fake_channel)

    assert isinstance(parameter.widget, module._DMMRCurrentMonitorSpinBox)
    assert parameter.apply_calls == 1
    assert parameter.value == 1.5e-12
    assert sync_calls == [True]


def test_sync_monitor_widget_does_not_overwrite_numeric_spinbox_text():
    module = _load_module()

    class FakeLineEdit:
        def __init__(self):
            self.text_calls = []
            self.tooltip = None

        def setText(self, text):
            self.text_calls.append(text)

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    class FakeSpinWidget:
        def __init__(self):
            self.line_edit = FakeLineEdit()
            self.tooltip = None
            self.update_calls = 0

        def lineEdit(self):
            return self.line_edit

        def setValue(self, _value):
            return None

        def textFromValue(self, _value):
            return "unused"

        def update(self):
            self.update_calls += 1

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    parameter = types.SimpleNamespace(
        toolTip="Measured DMMR module current.",
        getWidget=lambda: FakeSpinWidget(),
    )
    widget = parameter.getWidget()
    parameter.getWidget = lambda: widget
    fake_channel = types.SimpleNamespace(
        MONITOR="Monitor",
        monitor=1.5e-12,
        getParameterByName=lambda name: parameter if name == "Monitor" else None,
        _set_monitor_widget_minimum_width=lambda *args, **kwargs: None,
    )

    module.DMMRChannel._sync_monitor_widget(fake_channel)

    assert widget.line_edit.text_calls == []
    assert widget.update_calls == 1
    assert "1.5 pA" in widget.tooltip
    assert "1.5 pA" in widget.line_edit.tooltip


def test_set_monitor_widget_minimum_width_keeps_fixed_width():
    module = _load_module()

    class FakeMetrics:
        def horizontalAdvance(self, text):
            return len(text) * 7

    class FakeLineEdit:
        def fontMetrics(self):
            return FakeMetrics()

    class FakeWidget:
        def __init__(self):
            self._minimum_width = 50
            self.line_edit = FakeLineEdit()

        def lineEdit(self):
            return self.line_edit

        def fontMetrics(self):
            return FakeMetrics()

        def minimumWidth(self):
            return self._minimum_width

        def setMinimumWidth(self, width):
            self._minimum_width = width

    widget = FakeWidget()
    fake_channel = types.SimpleNamespace()

    module.DMMRChannel._set_monitor_widget_minimum_width(
        fake_channel,
        widget,
    )

    assert widget.minimumWidth() > 60


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port (inside initialize()) locks the COM port for the
    process lifetime; the init-failure guidance must tell the operator to
    restart ESIBD Explorer instead of letting retries loop on a confusing
    '-2 (Error opening port)'."""
    module = _load_module()
    controller = module.DMMRController(types.SimpleNamespace(com=10))

    fatal_exc = RuntimeError(
        "DMMR DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The DMMR instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10

    retry_exc = RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "locked the COM port" in guidance2
    assert controller._poisoned_com == 10


def test_init_failure_guidance_silent_without_prior_poisoning():
    module = _load_module()
    controller = module.DMMRController(types.SimpleNamespace(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None


def test_safe_disable_after_toggle_failure_disables_acquisition():
    """Alignment with the AMPR pattern: on a failed ON, DMMR best-effort
    disables acquisition so the device is not left enabled while the ON/OFF
    button is forced OFF (which would strand the operator)."""
    module = _load_module()
    parent = types.SimpleNamespace(connect_timeout_s=5.0, getChannels=lambda: [])
    controller = module.DMMRController(parent)
    controller.lock = threading.Lock()
    controller.print = lambda message, flag=None: None
    calls = []

    class FakeDevice:
        NO_ERR = 0

        def set_automatic_current(self, value, timeout_s=None):
            calls.append(("automatic", value))
            return self.NO_ERR

        def set_enable(self, value, timeout_s=None):
            calls.append(("enable", value))
            return self.NO_ERR

    controller.device = FakeDevice()
    controller._safe_disable_after_toggle_failure()
    assert ("automatic", False) in calls
    assert ("enable", False) in calls


def test_safe_disable_after_toggle_failure_never_raises():
    """Cleanup must never raise: if the device is unresponsive, issues are
    reported as a warning instead of propagating (mirrors AMPR)."""
    module = _load_module()
    parent = types.SimpleNamespace(connect_timeout_s=5.0, getChannels=lambda: [])
    controller = module.DMMRController(parent)
    controller.lock = threading.Lock()
    logs = []
    controller.print = lambda message, flag=None: logs.append(message)

    class BrokenDevice:
        NO_ERR = 0

        def set_automatic_current(self, value, timeout_s=None):
            raise RuntimeError("comm down")

        def set_enable(self, value, timeout_s=None):
            raise OSError("device gone")

    controller.device = BrokenDevice()
    controller._safe_disable_after_toggle_failure()  # must not raise
    assert any("cleanup encountered issues" in m for m in logs)
