"""Channel synchronization checks for the standalone ESIBD AMPR plugin."""

from __future__ import annotations

import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path

import numpy as np


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "ampr_plugin.py"
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

    class Channel:
        NAME = "Name"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent

        def initComplete(self):
            self.super_init_complete_called = True

    class ToolButton:
        def setChecked(self, checked):
            self.checked = checked

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
        or name == "cgc"
        or name.startswith("cgc.")
        or name.startswith("esibd_ampr")
        or name.startswith("_esibd_bundled_ampr_runtime")
        or name == "ampr_plugin_sync_test"
    ]:
        sys.modules.pop(name, None)


def _import_plugin_module():
    spec = importlib.util.spec_from_file_location("ampr_plugin_sync_test", PLUGIN_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_module():
    _clear_test_modules()
    _install_esibd_stubs()
    return _import_plugin_module()


def test_bootstrap_config_is_replaced_from_detected_modules():
    module = _load_module()
    default_item = {
        "Module": "0",
        "CH": "1",
        "Real": True,
        "Enabled": True,
        "Min": -50,
        "Max": 50,
        "Collapse": False,
    }

    bootstrap_items = [
        {"Name": f"AMPR{index}", "Module": 0, "CH": 1, "Real": True, "Enabled": True}
        for index in range(1, 13)
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=bootstrap_items,
        detected_modules=[2, 5],
        device_name="AMPR",
        default_item=default_item,
    )

    assert len(synced_items) == 8
    assert synced_items[0]["Name"] == "AMPR_M02_CH1"
    assert synced_items[-1]["Name"] == "AMPR_M05_CH4"
    assert all(item["Enabled"] is False for item in synced_items)
    assert all(item["Real"] is True for item in synced_items)
    assert all(item["Min"] == -50 for item in synced_items)
    assert all(item["Collapse"] is False for item in synced_items)
    assert log_entries == [("AMPR bootstrap config replaced from hardware scan.", None)]


def test_bootstrap_detection_accepts_stringified_numeric_defaults():
    module = _load_module()

    assert module._looks_like_bootstrap_items(
        items=[
            {
                "Name": "AMPR1",
                "Module": "0",
                "CH": "1",
                "Real": "true",
                "Enabled": "true",
                "Value": "0",
                "Min": "-50",
                "Max": "50",
                "Display": "true",
            },
            {
                "Name": "AMPR2",
                "Module": "0",
                "CH": "1",
                "Real": "true",
                "Enabled": "true",
                "Value": "0",
                "Min": "-50",
                "Max": "50",
                "Display": "true",
            },
        ],
        device_name="AMPR",
        default_item={
            "Module": "0",
            "CH": "1",
            "Real": True,
            "Enabled": True,
            "Value": 0.0,
            "Min": -50.0,
            "Max": 50.0,
            "Display": True,
        },
    ) is True


def test_sequential_names_with_user_changes_are_not_treated_as_bootstrap():
    module = _load_module()

    assert module._looks_like_bootstrap_items(
        items=[
            {"Name": "AMPR1", "Module": 0, "CH": 1, "Real": True, "Enabled": False},
            {"Name": "AMPR2", "Module": 0, "CH": 1, "Real": True, "Enabled": True},
        ],
        device_name="AMPR",
        default_item={"Module": 0, "CH": 1, "Real": True, "Enabled": True},
    ) is False


def test_existing_config_is_merged_and_new_channels_are_generic():
    module = _load_module()

    current_items = [
        {
            "Name": "UserKeep",
            "Module": 1,
            "CH": 1,
            "Real": True,
            "Enabled": True,
            "Min": -5,
            "Max": 5,
        },
        {
            "Name": "MissingLater",
            "Module": 3,
            "CH": 2,
            "Real": True,
            "Enabled": True,
            "Color": "#112233",
        },
        {
            "Name": "ComesBack",
            "Module": 2,
            "CH": 4,
            "Real": False,
            "Enabled": True,
        },
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[1, 2],
        device_name="AMPR",
    )

    assert len(synced_items) == 9
    assert synced_items[0]["Name"] == "UserKeep"
    assert synced_items[0]["Real"] is True
    assert synced_items[0]["Enabled"] is True
    assert synced_items[0]["Min"] == -5
    assert synced_items[1]["Name"] == "MissingLater"
    assert synced_items[1]["Real"] is False
    assert synced_items[1]["Color"] == "#112233"
    assert synced_items[2]["Name"] == "ComesBack"
    assert synced_items[2]["Real"] is True

    added_names = {item["Name"] for item in synced_items[3:]}
    assert "AMPR_M01_CH2" in added_names
    assert "AMPR_M02_CH1" in added_names
    assert "AMPR_M02_CH3" in added_names
    assert all(item["Enabled"] is False for item in synced_items[3:])

    log_messages = [message for message, _flag in log_entries]
    assert "Added generic AMPR channels for detected modules: 1, 2" in log_messages
    assert "Marked AMPR channels virtual because modules are absent: 3" in log_messages
    assert "Reactivated AMPR channels for modules: 2" in log_messages


def test_legacy_bootstrap_residue_is_removed_from_mixed_config():
    module = _load_module()

    default_item = {
        "Module": "0",
        "CH": "1",
        "Real": True,
        "Enabled": True,
    }
    current_items = [
        {"Name": f"AMPR{index}", "Module": 0, "CH": 1, "Real": False, "Enabled": True}
        for index in range(1, 10)
    ] + [
        {"Name": "AMPR_M02_CH1", "Module": "2", "CH": "1", "Real": True, "Enabled": False},
        {"Name": "AMPR_M02_CH2", "Module": "2", "CH": "2", "Real": True, "Enabled": False},
        {"Name": "AMPR_M02_CH3", "Module": "2", "CH": "3", "Real": True, "Enabled": False},
        {"Name": "AMPR_M02_CH4", "Module": "2", "CH": "4", "Real": True, "Enabled": False},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[2],
        device_name="AMPR",
        default_item=default_item,
    )

    assert [item["Name"] for item in synced_items] == [
        "AMPR_M02_CH1",
        "AMPR_M02_CH2",
        "AMPR_M02_CH3",
        "AMPR_M02_CH4",
    ]
    assert (
        "Removed legacy AMPR bootstrap channels: AMPR1..AMPR9",
        None,
    ) in log_entries


def test_duplicate_mappings_are_neutralized():
    module = _load_module()

    current_items = [
        {"Name": "First", "Module": 1, "CH": 1, "Real": True, "Enabled": True},
        {"Name": "Duplicate", "Module": "1", "CH": "1", "Real": "true", "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[1],
        device_name="AMPR",
    )

    assert synced_items[0]["Real"] is True
    assert synced_items[1]["Real"] is False
    assert any("Duplicate AMPR mapping detected for module 1 CH1: Duplicate" == message for message, _flag in log_entries)


def test_device_applies_detected_module_voltage_limits_to_channels():
    module = _load_module()
    applied_limits = []

    class FakeChannel:
        def __init__(self, module_address):
            self._module_address = module_address

        def module_address(self):
            return self._module_address

        def applyModuleVoltageLimit(self, limit):
            applied_limits.append((self._module_address, limit))
            return self._module_address == 2

    device = object.__new__(module.AMPRDevice)
    device.module_voltage_limits = {2: 500.0}
    device.getChannels = lambda: [FakeChannel(2), FakeChannel(5)]

    changed = module.AMPRDevice._apply_module_voltage_limits(device)

    assert changed is True
    assert applied_limits == [(2, 500.0), (5, 1000.0)]


def test_controller_refreshes_detected_module_voltage_limits():
    module = _load_module()
    logs = []

    class FakeDevice:
        NO_ERR = 0

        def get_module_capabilities(self):
            return {
                2: {
                    "status": self.NO_ERR,
                    "product_id": "Dual Voltage Source 500V",
                    "voltage_rating": 500,
                    "channel_count": 2,
                },
                5: {
                    "status": self.NO_ERR,
                    "product_id": "Quadruple Unknown Module",
                    "voltage_rating": None,
                    "channel_count": 4,
                },
            }

    controller = object.__new__(module.AMPRController)
    controller.device = FakeDevice()
    controller.controllerParent = types.SimpleNamespace(
        module_voltage_limits={},
        module_channel_counts={},
    )
    controller.print = lambda message, flag=None: logs.append((message, flag))

    module.AMPRController._refresh_module_capabilities(controller)

    assert controller.controllerParent.module_voltage_limits == {2: 500.0}
    assert controller.controllerParent.module_channel_counts == {2: 2, 5: 4}
    assert logs == [
        (
            "Could not determine AMPR module voltage ratings for: 5 (Quadruple Unknown Module). "
            "Falling back to ±1000 V.",
            module.PRINT.WARNING,
        )
    ]


def test_channel_sync_uses_detected_module_channel_counts():
    module = _load_module()

    synced_items, _log_entries = module._plan_channel_sync(
        current_items=[],
        detected_modules=[2, 5],
        device_name="AMPR",
        default_item={"Real": True, "Enabled": True, "Module": "0", "CH": "1"},
        module_channel_counts={2: 2, 5: 4},
    )

    assert [item["Name"] for item in synced_items] == [
        "AMPR_M02_CH1",
        "AMPR_M02_CH2",
        "AMPR_M05_CH1",
        "AMPR_M05_CH2",
        "AMPR_M05_CH3",
        "AMPR_M05_CH4",
    ]


def test_empty_detection_does_not_modify_channels():
    module = _load_module()

    current_items = [{"Name": "UserKeep", "Module": 1, "CH": 1, "Real": True}]
    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[],
        device_name="AMPR",
    )

    assert synced_items == current_items
    assert log_entries == []


def test_init_complete_skips_sync_without_real_device():
    module = _load_module()

    parent = types.SimpleNamespace(main_state="", detected_modules="", sync_calls=[])
    parent._sync_channels_from_detected_modules = lambda modules: parent.sync_calls.append(
        list(modules)
    )

    controller = module.AMPRController(controllerParent=parent)
    controller.detected_module_ids = [1]
    controller.device = None
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))

    controller.initComplete()

    assert parent.sync_calls == []
    assert controller.super_init_complete_called is True
    assert logs == [
        (
            "AMPR initialization simulated because ESIBD Test mode is active. "
            "No hardware communication was attempted.",
            module.PRINT.WARNING,
        )
    ]


def test_init_complete_logs_success_for_real_initialization():
    module = _load_module()

    parent = types.SimpleNamespace(com=5, main_state="", detected_modules="", sync_calls=[])
    parent._sync_channels_from_detected_modules = lambda modules: parent.sync_calls.append(
        list(modules)
    )

    controller = module.AMPRController(controllerParent=parent)
    controller.detected_module_ids = [1, 3]
    controller.detected_modules_text = "1, 3"
    controller.main_state = "ST_STBY"
    controller.device = object()
    acquisition_calls = []
    controller.startAcquisition = lambda: acquisition_calls.append("start")
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))

    controller.initComplete()

    assert parent.sync_calls == [[1, 3]]
    assert acquisition_calls == []
    assert controller.super_init_complete_called is True
    assert logs == [
        ("AMPR initialized on COM5. State: ST_STBY. Detected modules: 1, 3.", None)
    ]


def test_init_complete_starts_acquisition_only_when_device_is_st_on():
    module = _load_module()

    parent = types.SimpleNamespace(com=5, main_state="", detected_modules="", sync_calls=[])
    parent._sync_channels_from_detected_modules = lambda modules: parent.sync_calls.append(
        list(modules)
    )

    controller = module.AMPRController(controllerParent=parent)
    controller.detected_module_ids = [2]
    controller.detected_modules_text = "2"
    controller.main_state = "ST_ON"
    controller.device = object()
    acquisition_calls = []
    controller.startAcquisition = lambda: acquisition_calls.append("start")
    controller.print = lambda message, flag=None: None

    controller.initComplete()

    assert parent.sync_calls == [[2]]
    assert acquisition_calls == ["start"]


def test_read_numbers_skips_acquisition_before_successful_initialization():
    module = _load_module()

    class FakeChannel:
        real = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 3

    class FakeDevice:
        def get_module_voltages(self, module):
            assert module == 2
            return {
                1: {"measured": 11.0},
                2: {"measured": 22.0},
                3: {"measured": 33.0},
                4: {"measured": 44.0},
            }

    controller = object.__new__(module.AMPRController)
    controller.device = FakeDevice()
    controller.controllerParent = types.SimpleNamespace(
        getChannels=lambda: [FakeChannel()],
        getConfiguredModules=lambda: [2],
    )
    controller.initialized = False
    controller.detected_module_ids = [2]
    controller.errorCount = 0
    controller.main_state = "Disconnected"
    controller.values = {(2, 3): 123.0}
    controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")

    module.AMPRController.readNumbers(controller)

    assert list(controller.values) == [(2, 3)]
    assert np.isnan(controller.values[(2, 3)])


def test_read_numbers_blocks_channel_acquisition_until_st_on():
    module = _load_module()

    class FakeChannel:
        real = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 3

    class FakeDevice:
        def get_module_voltages(self, module):
            assert module == 2
            return {
                1: {"measured": 11.0},
                2: {"measured": 22.0},
                3: {"measured": 33.0},
                4: {"measured": 44.0},
            }

    controller = object.__new__(module.AMPRController)
    controller.device = FakeDevice()
    controller.controllerParent = types.SimpleNamespace(
        getChannels=lambda: [FakeChannel()],
        getConfiguredModules=lambda: [2],
    )
    controller.initialized = True
    controller.detected_module_ids = [2]
    controller.errorCount = 0
    controller.main_state = "Disconnected"
    controller.values = {(2, 3): 123.0}
    controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")

    module.AMPRController.readNumbers(controller)

    assert list(controller.values) == [(2, 3)]
    assert np.isnan(controller.values[(2, 3)])


def test_read_numbers_reads_voltages_only_once_st_on_is_reached():
    module = _load_module()

    class FakeChannel:
        real = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 3

    class FakeDevice:
        def get_module_voltages(self, module):
            assert module == 2
            return {
                1: {"measured": 11.0},
                2: {"measured": 22.0},
                3: {"measured": 33.0},
                4: {"measured": 44.0},
            }

    controller = object.__new__(module.AMPRController)
    controller.device = FakeDevice()
    controller.controllerParent = types.SimpleNamespace(
        getChannels=lambda: [FakeChannel()],
        getConfiguredModules=lambda: [2],
    )
    controller.initialized = True
    controller.detected_module_ids = [2]
    controller.errorCount = 0
    controller.main_state = "Disconnected"
    controller.values = {(2, 3): 123.0}
    controller._update_state = lambda: setattr(controller, "main_state", "ST_ON")

    module.AMPRController.readNumbers(controller)

    assert controller.values == {
        (2, 1): 11.0,
        (2, 2): 22.0,
        (2, 3): 33.0,
        (2, 4): 44.0,
    }


def test_read_numbers_acquires_lock_for_ampr_module_polling():
    module = _load_module()

    class FakeTimeoutLock:
        def __init__(self):
            self.calls = []

        class _Section:
            def __init__(self, owner, timeout, timeout_message):
                self.owner = owner
                self.payload = (timeout, timeout_message)

            def __enter__(self):
                self.owner.calls.append(self.payload)
                return True

            def __exit__(self, exc_type, exc, tb):
                return False

        def acquire_timeout(self, timeout, timeoutMessage=""):
            return self._Section(self, timeout, timeoutMessage)

    class FakeChannel:
        real = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    class FakeDevice:
        def get_module_voltages(self, module):
            assert module == 2
            return {1: {"measured": 11.0}}

    controller = module.AMPRController(
        types.SimpleNamespace(
            getChannels=lambda: [FakeChannel()],
            getConfiguredModules=lambda: [2],
        )
    )
    controller.lock = FakeTimeoutLock()
    controller.device = FakeDevice()
    controller.initialized = True
    controller.detected_module_ids = [2]
    controller.errorCount = 0
    controller.main_state = "Disconnected"
    controller._update_state = lambda: setattr(controller, "main_state", "ST_ON")

    module.AMPRController.readNumbers(controller)

    assert controller.lock.calls == [
        (1, "Could not acquire lock to read AMPR module 2.")
    ]
    assert controller.values == {(2, 1): 11.0}


def test_update_state_marks_communication_lost_after_repeated_generic_failures():
    module = _load_module()
    ui_states = []
    close_emits = []

    class FakeDevice:
        def get_state(self):
            raise RuntimeError("serial timeout while reading state")

        def get_device_state(self):
            raise RuntimeError("serial timeout")

        def get_voltage_state(self):
            raise RuntimeError("serial timeout")

        def get_interlock_state(self):
            raise RuntimeError("serial timeout")

        def disconnect(self):
            return True

        def close(self):
            return None

    parent = types.SimpleNamespace(
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        interlock_state_summary="",
        voltage_state_summary="",
    )

    controller = module.AMPRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.transitioning = True
    controller.transition_target_on = True
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None
    controller.signalComm = types.SimpleNamespace(
        closeCommunicationSignal=types.SimpleNamespace(emit=lambda: close_emits.append(True))
    )

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()

    assert controller.main_state == module._AMPR_COMMUNICATION_LOST_STATE
    assert controller.device is None
    assert controller.acquiring is False
    assert close_emits == [True]
    assert ui_states == [False]


def test_fake_numbers_does_not_invent_ampr_monitors():
    module = _load_module()

    class FakeChannel:
        enabled = True
        real = True
        value = 42.0

        def module_address(self):
            return 1

        def channel_number(self):
            return 4

    controller = object.__new__(module.AMPRController)
    controller.controllerParent = types.SimpleNamespace(
        getChannels=lambda: [FakeChannel()],
        isOn=lambda: True,
    )
    controller.values = None

    module.AMPRController.fakeNumbers(controller)

    assert list(controller.values) == [(1, 4)]
    assert np.isnan(controller.values[(1, 4)])


def test_run_initialization_logs_explicit_failure():
    module = _load_module()

    controller = object.__new__(module.AMPRController)
    controller.device = None
    controller.detected_module_ids = []
    controller.detected_modules_text = ""
    controller.main_state = "Disconnected"
    controller.initializing = True
    ui_states = []
    controller.controllerParent = types.SimpleNamespace(
        com=7,
        baudrate=230400,
        connect_timeout_s=5.0,
        name="AMPR",
        _set_on_ui_state=lambda on: ui_states.append(on),
    )
    controller._dispose_device = lambda: None
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))

    original = module._get_ampr_driver_class
    module._get_ampr_driver_class = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        module.AMPRController.runInitialization(controller)
    finally:
        module._get_ampr_driver_class = original

    assert controller.initializing is False
    assert ui_states == [False]
    assert logs == [
        (
            "AMPR initialization failed on COM7: RuntimeError: boom",
            module.PRINT.ERROR,
        )
    ]


def test_run_initialization_logs_process_backend_fallback_warning():
    module = _load_module()

    class FakeDriver:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._process_backend_disabled_reason = (
                "AMPR process isolation startup failed; "
                "falling back to inline controller: "
                "worker timed out during worker startup"
            )

        def connect(self, timeout_s):
            self.timeout_s = timeout_s

    emitted = []
    controller = object.__new__(module.AMPRController)
    controller.device = None
    controller.detected_module_ids = []
    controller.detected_modules_text = ""
    controller.main_state = "Disconnected"
    controller.initialized = False
    controller.initializing = True
    controller.signalComm = types.SimpleNamespace(
        initCompleteSignal=types.SimpleNamespace(emit=lambda: emitted.append(True))
    )
    controller.controllerParent = types.SimpleNamespace(
        com=5,
        baudrate=230400,
        connect_timeout_s=5.0,
        name="AMPR",
    )
    controller._dispose_device = lambda: None
    controller._refresh_module_scan = lambda: None
    controller._update_state = lambda: None
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))

    original = module._get_ampr_driver_class
    module._get_ampr_driver_class = lambda: FakeDriver
    try:
        module.AMPRController.runInitialization(controller)
    finally:
        module._get_ampr_driver_class = original

    assert emitted == [True]
    assert controller.initializing is False
    assert logs == [
        (
            "AMPR process isolation startup failed; "
            "falling back to inline controller: "
            "worker timed out during worker startup",
            module.PRINT.WARNING,
        )
    ]


def test_refresh_module_scan_ignores_bootstrap_module_zero_warning():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0

        def get_scanned_module_state(self):
            return self.NO_ERR, False, False

        def scan_modules(self):
            return [2]

    controller = object.__new__(module.AMPRController)
    controller.device = FakeDevice()
    controller.detected_module_ids = []
    controller.detected_modules_text = ""
    controller.controllerParent = types.SimpleNamespace(
        name="AMPR",
        getConfiguredModules=lambda: [0],
        _current_channel_items=lambda: [
            {"Name": f"AMPR{index}", "Module": "0", "CH": "1", "Real": True, "Enabled": True}
            for index in range(1, 10)
        ],
        _default_channel_item=lambda: {"Module": "0", "CH": "1", "Real": True, "Enabled": True},
    )
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))

    module.AMPRController._refresh_module_scan(controller)

    assert controller.detected_module_ids == [2]
    assert logs == []


def test_apply_value_ignores_device_disposal_race():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self, controller):
            self.controller = controller

        def set_module_voltage(self, module, channel_id, target_voltage):
            self.controller.device = None
            return self.NO_ERR

    class FakeChannel:
        value = 12.5
        enabled = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 4

    controller = object.__new__(module.AMPRController)
    controller.lock = FakeLock()
    controller.errorCount = 0
    controller.initialized = True
    controller.main_state = "ST_ON"
    controller.ramping = False
    controller.transitioning = False
    controller.controllerParent = types.SimpleNamespace(isOn=lambda: True)
    controller.print = lambda *args, **kwargs: None
    controller.device = FakeDevice(controller)

    module.AMPRController.applyValue(controller, FakeChannel())

    assert controller.errorCount == 0


def test_apply_value_skips_hardware_writes_until_psu_is_on():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def set_module_voltage(self, module, channel_id, target_voltage):
            self.calls.append((module, channel_id, target_voltage))
            return self.NO_ERR

    class FakeChannel:
        value = 10.0
        enabled = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    controller = object.__new__(module.AMPRController)
    controller.lock = FakeLock()
    controller.errorCount = 0
    controller.device = FakeDevice()
    controller.initialized = True
    controller.main_state = "ST_STBY"
    controller.ramping = False
    controller.transitioning = False
    controller.controllerParent = types.SimpleNamespace(isOn=lambda: False)
    controller.print = lambda *args, **kwargs: None

    module.AMPRController.applyValue(controller, FakeChannel())

    assert controller.device.calls == []


def test_toggle_on_runs_full_initialize_sequence_when_switching_on():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def initialize(self, timeout_s):
            self.calls.append(("initialize", timeout_s))

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        state_calls = []
        logs = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.acquiring = False
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: True,
            connect_timeout_s=7.5,
            startup_timeout_s=12.0,
        )
        acquisition_calls = []
        controller.startAcquisition = lambda: acquisition_calls.append("start")
        controller._refresh_module_scan = lambda: state_calls.append("scan")
        controller._update_state = lambda: (
            state_calls.append("state"),
            setattr(controller, "main_state", "ST_ON"),
        )
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == [("initialize", 12.0)]
    assert state_calls == ["scan", "state"]
    assert acquisition_calls == ["start"]
    assert logs == [
        ("Starting AMPR PSU. Waiting up to 12.0 s for ST_ON.", None),
        ("AMPR PSU turned ON. State: ST_ON.", None),
    ]


def test_toggle_on_runs_shutdown_when_switching_off():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        def __init__(self):
            self.calls = []

        def shutdown(self):
            self.calls.append("shutdown")

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        state_calls = []
        logs = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.acquiring = True
        stop_calls = []
        controller.stopAcquisition = lambda: stop_calls.append("stop")
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: False,
            connect_timeout_s=7.5,
            ramp_rate_v_s=0.0,
        )
        controller._refresh_module_scan = lambda: state_calls.append("scan")
        controller._update_state = lambda: (
            state_calls.append("state"),
            setattr(controller, "main_state", "ST_STBY"),
        )
        controller._sync_status_to_gui = lambda: state_calls.append("sync")
        controller._dispose_device = lambda: state_calls.append("dispose")
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == ["shutdown"]
    assert state_calls == ["sync", "dispose"]
    assert stop_calls == ["stop"]
    assert logs == [
        ("Starting AMPR shutdown sequence.", None),
        ("AMPR shutdown sequence completed.", None),
    ]


def test_toggle_on_does_not_log_success_outside_st_on():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def initialize(self, timeout_s):
            self.calls.append(("initialize", timeout_s))

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        logs = []
        ui_states = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.acquiring = False
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: True,
            connect_timeout_s=7.5,
            startup_timeout_s=12.0,
            _set_on_ui_state=lambda on: ui_states.append(on),
        )
        controller._refresh_module_scan = lambda: None
        controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == [("initialize", 12.0)]
    assert controller.errorCount == 1
    assert ui_states == [False]
    assert logs == [
        ("Starting AMPR PSU. Waiting up to 12.0 s for ST_ON.", None),
        (
            "AMPR PSU ON sequence ended in an unexpected state: ST_STBY.",
            module.PRINT.ERROR,
        ),
    ]


def test_toggle_on_ramps_enabled_channels_after_startup(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class FakeChannel:
        real = True
        enabled = True
        value = 2.0

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def set_module_voltages(self, module, voltages):
            self.calls.append(("set_module_voltages", module, dict(voltages)))
            return {channel_id: self.NO_ERR for channel_id in voltages}

        def initialize(self, timeout_s):
            self.calls.append(("initialize", timeout_s))

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        logs = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.ramping = False
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: True,
            connect_timeout_s=7.5,
            startup_timeout_s=12.0,
            ramp_rate_v_s=10.0,
            getChannels=lambda: [FakeChannel()],
            updateValues=lambda apply=False: logs.append((f"update:{apply}", None)),
        )
        controller._refresh_module_scan = lambda: logs.append(("scan", None))
        controller._update_state = lambda: setattr(controller, "main_state", "ST_ON")
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == [
        ("initialize", 12.0),
        ("set_module_voltages", 2, {1: 0.0}),
        ("set_module_voltages", 2, {1: 1.0}),
        ("set_module_voltages", 2, {1: 2.0}),
    ]
    assert logs == [
        ("update:False", None),
        ("Starting AMPR PSU. Waiting up to 12.0 s for ST_ON.", None),
        ("scan", None),
        ("Starting AMPR ramp-up at 10.0 V/s (estimated 0.2 s).", None),
        ("AMPR ramp-up completed.", None),
        ("AMPR PSU turned ON. State: ST_ON.", None),
    ]


def test_toggle_off_ramps_down_before_shutdown(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class FakeChannel:
        real = True
        enabled = True
        value = 2.0

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def set_module_voltages(self, module, voltages):
            self.calls.append(("set_module_voltages", module, dict(voltages)))
            return {channel_id: self.NO_ERR for channel_id in voltages}

        def shutdown(self):
            self.calls.append("shutdown")

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        logs = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.ramping = False
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: False,
            connect_timeout_s=7.5,
            ramp_rate_v_s=10.0,
            getChannels=lambda: [FakeChannel()],
        )
        controller._refresh_module_scan = lambda: logs.append(("scan", None))
        controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")
        controller._sync_status_to_gui = lambda: logs.append(("sync", None))
        controller._dispose_device = lambda: logs.append(("dispose", None))
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == [
        ("set_module_voltages", 2, {1: 1.0}),
        ("set_module_voltages", 2, {1: 0.0}),
        "shutdown",
    ]
    assert logs == [
        ("Starting AMPR ramp-down at 10.0 V/s (estimated 0.2 s).", None),
        ("AMPR ramp-down completed.", None),
        ("Starting AMPR shutdown sequence.", None),
        ("AMPR shutdown sequence completed.", None),
        ("sync", None),
        ("dispose", None),
    ]


def test_toggle_on_cleans_up_psu_after_ramp_failure(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class FakeChannel:
        real = True
        enabled = True
        value = 2.0

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []
            self.ramp_calls = 0

        def set_module_voltages(self, module, voltages):
            self.calls.append(("set_module_voltages", module, dict(voltages)))
            if dict(voltages) == {1: 0.0} and self.ramp_calls == 0:
                return {1: self.NO_ERR}
            if dict(voltages) == {1: 1.0}:
                self.ramp_calls += 1
                return {1: 123}
            return {1: self.NO_ERR}

        def initialize(self, timeout_s):
            self.calls.append(("initialize", timeout_s))

        def enable_psu(self, enabled):
            self.calls.append(("enable_psu", enabled))
            return self.NO_ERR, enabled

        def format_status(self, status):
            return f"STATUS_{status}"

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        device = FakeDevice()
        logs = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.ramping = False
        controller.transitioning = True
        controller.transition_target_on = True
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: True,
            connect_timeout_s=7.5,
            startup_timeout_s=12.0,
            ramp_rate_v_s=10.0,
            getChannels=lambda: [FakeChannel()],
            updateValues=lambda apply=False: None,
        )
        controller._refresh_module_scan = lambda: None
        controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert device.calls == [
        ("initialize", 12.0),
        ("set_module_voltages", 2, {1: 0.0}),
        ("set_module_voltages", 2, {1: 1.0}),
        ("set_module_voltages", 2, {1: 0.0}),
        ("enable_psu", False),
    ]
    assert controller.transitioning is False
    assert logs == [
        ("Starting AMPR PSU. Waiting up to 12.0 s for ST_ON.", None),
        ("Starting AMPR ramp-up at 10.0 V/s (estimated 0.2 s).", None),
        ("AMPR startup cleanup disabled the PSU after failure.", module.PRINT.WARNING),
        (
            "Failed to toggle AMPR PSU: RuntimeError: AMPR rejected 1.000 V for module 2 CH1: STATUS_123",
            module.PRINT.ERROR,
        ),
    ]


def test_ramp_replays_updated_targets_changed_during_transition(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)

    class FakeChannel:
        real = True
        enabled = True
        value = 2.0

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    channel = FakeChannel()

    class FakeDevice:
        NO_ERR = 0

        def __init__(self):
            self.calls = []

        def set_module_voltages(self, module, voltages):
            self.calls.append(("set_module_voltages", module, dict(voltages)))
            if dict(voltages) == {1: 1.0}:
                channel.value = 3.0
            return {1: self.NO_ERR}

    controller = object.__new__(module.AMPRController)
    controller.lock = FakeLock()
    controller.device = FakeDevice()
    controller.errorCount = 0
    controller.ramping = False
    controller.transitioning = True
    logs = []
    controller.controllerParent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [channel],
    )
    controller.print = lambda message, flag=None: logs.append((message, flag))

    module.AMPRController._ramp_target_voltages(
        controller,
        start_targets={(2, 1): 0.0},
        end_targets={(2, 1): 2.0},
        rate_v_s=10.0,
        label="up",
    )

    assert controller.device.calls == [
        ("set_module_voltages", 2, {1: 1.0}),
        ("set_module_voltages", 2, {1: 2.0}),
        ("set_module_voltages", 2, {1: 3.0}),
    ]
    assert logs == [
        ("Starting AMPR ramp-up at 10.0 V/s (estimated 0.2 s).", None),
        ("Applying updated AMPR targets queued during ramp.", None),
        ("AMPR ramp-up completed.", None),
    ]


def test_toggle_on_failure_logs_runtime_diagnostics():
    module = _load_module()

    class FakeContext:
        def __enter__(self):
            return True

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeLock:
        def acquire_timeout(self, *args, **kwargs):
            return FakeContext()

    class FakeDevice:
        NO_ERR = 0

        def initialize(self, timeout_s):
            raise RuntimeError("AMPR did not reach ST_ON")

        def get_state(self, **kwargs):
            return self.NO_ERR, "0x2", "ST_STBY"

        def get_device_state(self, **kwargs):
            return self.NO_ERR, "0x1", ["DS_PSU_ENB"]

        def get_voltage_state(self, **kwargs):
            return self.NO_ERR, "0x0", ["VOLTAGE_OK"]

        def get_interlock_state(self, **kwargs):
            return self.NO_ERR, "0x0", []

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        logs = []
        ui_states = []
        controller = object.__new__(module.AMPRController)
        controller.lock = FakeLock()
        controller.device = FakeDevice()
        controller.errorCount = 0
        controller.main_state = "Disconnected"
        controller.controllerParent = types.SimpleNamespace(
            isOn=lambda: True,
            connect_timeout_s=7.5,
            startup_timeout_s=15.0,
            poll_timeout_s=1.0,
            _set_on_ui_state=lambda on: ui_states.append(on),
        )
        controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")
        controller.print = lambda message, flag=None: logs.append((message, flag))

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert controller.errorCount == 1
    assert ui_states == [False]
    assert logs == [
        ("Starting AMPR PSU. Waiting up to 15.0 s for ST_ON.", None),
        (
            "Failed to toggle AMPR PSU: RuntimeError: AMPR did not reach ST_ON "
            "(main state: ST_STBY; device state: DS_PSU_ENB; voltage state: VOLTAGE_OK; interlock state: OK)",
            module.PRINT.ERROR,
        ),
    ]


def test_shutdown_communication_runs_full_device_shutdown():
    module = _load_module()

    class FakeDevice:
        def __init__(self):
            self.calls = []

        def shutdown(self):
            self.calls.append("shutdown")

    original_close = getattr(module.DeviceController, "closeCommunication", None)
    module.DeviceController.closeCommunication = lambda self: setattr(
        self, "super_close_called", True
    )
    try:
        device = FakeDevice()
        logs = []
        synced = []
        disposed = []
        controller = object.__new__(module.AMPRController)
        controller.device = device
        controller.errorCount = 0
        controller.main_state = "ST_ON"
        controller.detected_module_ids = [2]
        controller.detected_modules_text = "2"
        controller._sync_status_to_gui = lambda: synced.append(
            (controller.main_state, controller.detected_module_ids, controller.detected_modules_text)
        )
        controller._dispose_device = lambda: disposed.append(True)
        controller.print = lambda message, flag=None: logs.append((message, flag))

        shutdown_confirmed = module.AMPRController.shutdownCommunication(controller)
    finally:
        if original_close is None:
            delattr(module.DeviceController, "closeCommunication")
        else:
            module.DeviceController.closeCommunication = original_close

    assert device.calls == ["shutdown"]
    assert shutdown_confirmed is True
    assert controller.super_close_called is True
    assert logs == [
        ("Starting AMPR shutdown sequence.", None),
        ("AMPR shutdown sequence completed.", None),
    ]
    assert synced == [("Disconnected", [], "")]
    assert disposed == [True]
    assert controller.initialized is False


def test_shutdown_communication_logs_diagnostics_on_failure():
    module = _load_module()

    class FakeDevice:
        NO_ERR = 0

        def shutdown(self):
            raise RuntimeError("boom")

        def get_state(self, **kwargs):
            return self.NO_ERR, "0x8006", "ST_ERR_PSU_DIS"

        def get_device_state(self, **kwargs):
            return self.NO_ERR, "0x0", ["DEVICE_OK"]

        def get_voltage_state(self, **kwargs):
            return self.NO_ERR, "0x0", ["VOLTAGE_OK"]

        def get_interlock_state(self, **kwargs):
            return self.NO_ERR, "0x0", []

    original_close = getattr(module.DeviceController, "closeCommunication", None)
    module.DeviceController.closeCommunication = lambda self: None
    try:
        logs = []
        disposed = []
        controller = object.__new__(module.AMPRController)
        controller.device = FakeDevice()
        controller.errorCount = 0
        controller.main_state = "ST_ON"
        controller.detected_module_ids = [2]
        controller.detected_modules_text = "2"
        controller.controllerParent = types.SimpleNamespace(poll_timeout_s=1.0)
        controller._update_state = lambda: setattr(controller, "main_state", "ST_ERR_PSU_DIS")
        controller._sync_status_to_gui = lambda: None
        controller._dispose_device = lambda: disposed.append(True)
        controller.print = lambda message, flag=None: logs.append((message, flag))

        shutdown_confirmed = module.AMPRController.shutdownCommunication(controller)
    finally:
        if original_close is None:
            delattr(module.DeviceController, "closeCommunication")
        else:
            module.DeviceController.closeCommunication = original_close

    assert controller.errorCount == 1
    assert shutdown_confirmed is False
    assert controller.main_state == module._AMPR_SHUTDOWN_UNCONFIRMED_STATE
    assert logs == [
        ("Starting AMPR shutdown sequence.", None),
        (
            "AMPR shutdown failed: RuntimeError: boom "
            "(main state: ST_ERR_PSU_DIS; device state: DEVICE_OK; voltage state: VOLTAGE_OK; interlock state: OK)",
            module.PRINT.ERROR,
        ),
        (
            "AMPR shutdown could not be confirmed before disconnect: RuntimeError: boom.",
            module.PRINT.ERROR,
        ),
    ]
    assert disposed == [True]
    assert controller.initialized is False


def test_update_values_clears_monitor_when_channel_or_device_is_off():
    module = _load_module()

    channel_enabled = types.SimpleNamespace(
        enabled=True,
        real=True,
        waitToStabilize=False,
        monitor=11.0,
        module_address=lambda: 2,
        channel_number=lambda: 1,
    )
    channel_disabled = types.SimpleNamespace(
        enabled=False,
        real=True,
        waitToStabilize=False,
        monitor=22.0,
        module_address=lambda: 2,
        channel_number=lambda: 2,
    )

    controller = object.__new__(module.AMPRController)
    controller.values = {(2, 1): 100.0, (2, 2): 200.0}
    controller.controllerParent = types.SimpleNamespace(
        isOn=lambda: False,
        getChannels=lambda: [channel_enabled, channel_disabled],
    )
    controller._sync_status_to_gui = lambda: None

    module.AMPRController.updateValues(controller)

    assert np.isnan(channel_enabled.monitor)
    assert np.isnan(channel_disabled.monitor)


def test_read_numbers_clears_existing_values_when_state_leaves_st_on():
    module = _load_module()

    class FakeChannel:
        real = True

        def module_address(self):
            return 2

        def channel_number(self):
            return 1

    controller = object.__new__(module.AMPRController)
    controller.device = types.SimpleNamespace()
    controller.initialized = True
    controller.values = {(2, 1): 10.0, (2, 2): 20.0}
    controller.main_state = "ST_ON"
    controller.controllerParent = types.SimpleNamespace(getChannels=lambda: [FakeChannel()])
    controller._update_state = lambda: setattr(controller, "main_state", "ST_STBY")

    module.AMPRController.readNumbers(controller)

    assert list(controller.values) == [(2, 1)]
    assert np.isnan(controller.values[(2, 1)])


def test_toggle_on_clears_transition_when_device_is_missing():
    module = _load_module()

    original_toggle_on = getattr(module.DeviceController, "toggleOn", None)
    module.DeviceController.toggleOn = lambda self: None
    try:
        restored_states = []
        controller = object.__new__(module.AMPRController)
        controller.device = None
        controller.transitioning = True
        controller.transition_target_on = True
        controller._restore_off_ui_state = lambda: restored_states.append(False)

        module.AMPRController.toggleOn(controller)
    finally:
        if original_toggle_on is None:
            delattr(module.DeviceController, "toggleOn")
        else:
            module.DeviceController.toggleOn = original_toggle_on

    assert restored_states == [False]
    assert controller.transitioning is False
    assert controller.transition_target_on is None


def test_controller_formats_open_port_timeout_with_operator_hint():
    module = _load_module()

    controller = module.AMPRController(types.SimpleNamespace(com=5))

    message = controller._format_exception(
        RuntimeError(
            "AMPR DLL call timed out during 'open_port'. "
            "The device may be powered off or unresponsive."
        )
    )

    assert "RuntimeError:" in message
    assert "Selected COM5 did not respond." in message
    assert "configured COM port is correct" in message


def test_controller_formats_open_port_error_with_operator_hint():
    module = _load_module()

    controller = module.AMPRController(types.SimpleNamespace(com=5))

    message = controller._format_exception(
        RuntimeError("AMPR open_port failed: -2 (Error opening port)")
    )

    assert "RuntimeError:" in message
    assert "Windows could not open COM5." in message
    assert "already in use" in message


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port locks the COM port for the process lifetime; the
    init-failure guidance must tell the operator to restart ESIBD Explorer
    instead of letting retries loop on a confusing '-2 (Error opening port)'.
    AMPR's _format_exception already hints 'already in use' for the -2 case but
    never recommends a restart, so the poisoned-port guidance complements it."""
    module = _load_module()
    controller = module.AMPRController(types.SimpleNamespace(com=10))

    fatal_exc = RuntimeError(
        "AMPR DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The AMPR instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10

    retry_exc = RuntimeError("AMPR open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "locked the COM port" in guidance2
    assert controller._poisoned_com == 10


def test_init_failure_guidance_silent_without_prior_poisoning():
    module = _load_module()
    controller = module.AMPRController(types.SimpleNamespace(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("AMPR open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None
