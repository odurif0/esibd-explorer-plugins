"""Packaging and ESIBD bridge checks for the standalone ESI plugin."""

from __future__ import annotations

import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path

from PIL import Image


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "esi" / "esi_plugin.py"
ICON_PATH = Path(__file__).resolve().parents[1] / "esi" / "esi.png"


def _install_esibd_stubs():
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")

    class PARAMETERTYPE(Enum):
        INT = "INT"
        FLOAT = "FLOAT"
        BOOL = "BOOL"
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
        MIN = "Min"
        MAX = "Max"
        ADVANCED = "Advanced"

    class Channel:
        NAME = "Name"
        ACTIVE = "Active"
        DISPLAY = "Display"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"
        MONITOR = "Monitor"
        MIN = "Min"
        MAX = "Max"

        def __init__(self, channelParent=None, tree=None):
            self.channelParent = channelParent
            self.tree = tree
            self._parameters = {
                self.VALUE: types.SimpleNamespace(unit=""),
                self.MONITOR: types.SimpleNamespace(unit=""),
            }

        def getDefaultChannel(self):
            return {
                self.VALUE: {},
                self.ENABLED: {},
                self.ACTIVE: {},
                self.DISPLAY: {},
                self.REAL: {},
                self.MIN: {},
                self.MAX: {},
            }

        def setDisplayedParameters(self):
            self.displayedParameters = []

        def initGUI(self, item):
            self.module = item.get("Module", 2)

        def getParameterByName(self, name):
            return self._parameters[name]

        def enabledChanged(self):
            self.base_enabled_changed = True

        def applyValue(self, apply=False):
            self.applied = apply

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent

        def toggleOn(self):
            self.super_toggle_called = True

    class Device:
        MAXDATAPOINTS = "Max data points"

    class Plugin:
        pass

    def parameterDict(**kwargs):
        result = dict(kwargs)
        if "value" in kwargs:
            result[Parameter.VALUE] = kwargs["value"]
        if "header" in kwargs:
            result[Parameter.HEADER] = kwargs["header"]
        return result

    core.PARAMETERTYPE = PARAMETERTYPE
    core.PLUGINTYPE = PLUGINTYPE
    core.PRINT = PRINT
    core.Channel = Channel
    core.DeviceController = DeviceController
    core.Parameter = Parameter
    core.parameterDict = parameterDict
    plugins.Device = Device
    plugins.Plugin = Plugin
    sys.modules["esibd"] = esibd
    sys.modules["esibd.core"] = core
    sys.modules["esibd.plugins"] = plugins


def _load_plugin():
    for name in tuple(sys.modules):
        if (
            name == "esi_plugin_test"
            or name == "esibd"
            or name.startswith("esibd.")
            or name.startswith("_esibd_bundled_esi_runtime")
        ):
            sys.modules.pop(name, None)
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("esi_plugin_test", PLUGIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_esi_plugin_metadata_and_private_runtime():
    module = _load_plugin()

    assert module.providePlugins() == [module.ESIDevice]
    assert module.ESIDevice.name == "ESI"
    assert module.ESIDevice.supportedVersion == "1.0.1"
    assert module.ESIDevice.unit == "V"
    assert module.ESIDevice.useMonitors is True
    assert module.ESIDevice.useOnOffLogic is True
    driver = module._get_esi_driver_class()
    assert driver.__name__ == "ESI"
    assert driver.__module__.startswith("_esibd_bundled_esi_runtime_")
    with Image.open(ICON_PATH) as icon:
        assert icon.size == (128, 128)


def test_fixed_channel_layout_is_safe_and_stable():
    module = _load_plugin()

    items = module._fixed_channel_items("ESI")

    assert [item["Module"] for item in items] == [1, 2, 0]
    assert [item["Name"] for item in items] == ["ESI_HV1", "ESI_HV2", "ESI_HEAT"]
    assert all(item["Enabled"] is False for item in items)
    assert [item["Value"] for item in items] == [0.0, 0.0, 20.0]
    assert all(item["Min"] == 0.0 for item in items)
    assert [item["Max"] for item in items] == [3000.0, 3000.0, 175.0]
    assert [item["Function"] for item in items] == [
        "HVPS-3kB",
        "HVPS-3kB",
        "HEAT-CTRL-2410",
    ]
    assert all("Unit" not in item for item in items)


def test_channel_defaults_enforce_3kv_positive_range():
    module = _load_plugin()
    parent = types.SimpleNamespace()
    channel = module.ESIChannel(channelParent=parent, tree=None)

    defaults = channel.getDefaultChannel()

    assert defaults[channel.VALUE][module.Parameter.MIN] == 0.0
    assert defaults[channel.VALUE][module.Parameter.MAX] == 3000.0
    assert defaults[channel.MODULE][module.Parameter.VALUE] == 2
    assert defaults[channel.MODULE]["minimum"] == 0
    assert defaults[channel.MODULE]["maximum"] == 3
    assert defaults[channel.MODULE]["indicator"] is True
    channel.module = 2
    assert channel.unit == "V"
    channel.module = 0
    assert channel.unit == "degC"
    assert channel.getDisplayUnit() == "degC"
    channel.initGUI({"Module": 0})
    assert channel.getParameterByName(channel.VALUE).unit == "degC"
    assert channel.getParameterByName(channel.MONITOR).unit == "degC"


def test_enabled_change_forces_hardware_apply():
    module = _load_plugin()
    channel = module.ESIChannel(
        channelParent=types.SimpleNamespace(loading=False),
        tree=None,
    )

    channel.enabledChanged()

    assert channel.base_enabled_changed is True
    assert channel.applied is True


def test_on_sequence_starts_at_zero_then_activates_enabled_modules():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("target", address, value))

        def set_global_active(self, active, timeout_s):
            calls.append(("global", active))

        def set_output_active(self, address, active, timeout_s):
            calls.append(("module", address, active))

    channels = [
        types.SimpleNamespace(
            module_address=lambda: 1,
            is_heat_channel=lambda: False,
            enabled=True,
            value=1200.0,
        ),
        types.SimpleNamespace(
            module_address=lambda: 2,
            is_heat_channel=lambda: False,
            enabled=False,
            value=2300.0,
        ),
    ]
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0,
        poll_timeout_s=3.0,
        ramp_rate_v_s=0.0,
        getChannels=lambda: channels,
        isOn=lambda: True,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.heat_readback_valid = True
    controller.applyValue = lambda channel: calls.append(
        ("apply", channel.module_address(), channel.value if channel.enabled else 0.0)
    )

    controller.toggleOn()

    assert calls == [
        ("global", True),
        ("target", 1, 0.0),
        ("target", 2, 0.0),
        ("module", 1, True),
        ("module", 2, False),
        ("apply", 1, 1200.0),
        ("apply", 2, 0.0),
    ]


def test_on_sequence_programs_heat_target_before_activation():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("hv_target", address, value))

        def set_heater_temperature(self, value, timeout_s):
            calls.append(("heat_target", value))

        def set_global_active(self, active, timeout_s):
            calls.append(("global", active))

        def set_output_active(self, address, active, timeout_s):
            calls.append(("module", address, active))

    heat = types.SimpleNamespace(
        module_address=lambda: 0,
        is_heat_channel=lambda: True,
        enabled=True,
        value=90.0,
    )
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0,
        poll_timeout_s=3.0,
        ramp_rate_v_s=0.0,
        getChannels=lambda: [heat],
        isOn=lambda: True,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.heat_readback_valid = True

    controller.toggleOn()

    assert calls == [
        ("global", True),
        ("hv_target", 1, 0.0),
        ("hv_target", 2, 0.0),
        ("heat_target", 90.0),
        ("module", 0, True),
    ]


def test_off_sequence_uses_driver_safe_off():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def force_safe_off(self, timeout_s):
            calls.append(timeout_s)

    parent = types.SimpleNamespace(
        connect_timeout_s=4.0,
        poll_timeout_s=3.0,
        ramp_rate_v_s=0.0,
        getChannels=lambda: [],
        isOn=lambda: False,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()

    controller.toggleOn()

    assert calls == [4.0]


def test_normal_target_change_is_ramped_in_bounded_steps(monkeypatch):
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append((address, value, timeout_s))

    parent = types.SimpleNamespace(ramp_rate_v_s=1000.0, poll_timeout_s=2.0)
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    controller._ramp_target(3, 0.0, 250.0)

    assert calls == [
        (3, 250.0 / 3.0, 2.0),
        (3, 500.0 / 3.0, 2.0),
        (3, 250.0, 2.0),
    ]


def test_heat_channel_sets_temperature_without_using_hv_voltage_path():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))

        def set_heater_temperature(self, target, timeout_s):
            calls.append(("temperature", target, timeout_s))

        def set_target_voltage(self, *args, **kwargs):
            raise AssertionError("Heat channel must not use the HV voltage setter")

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        isOn=lambda: True,
    )
    channel = types.SimpleNamespace(
        module_address=lambda: 0,
        is_heat_channel=lambda: True,
        enabled=True,
        value=95.0,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.heat_readback_valid = True

    controller.applyValue(channel)

    assert calls == [
        ("temperature", 95.0, 2.0),
        ("active", 0, True, 2.0),
    ]


def test_invalid_heat_readback_blocks_nonzero_target_and_forces_off():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_heater_temperature(self, target, timeout_s):
            calls.append(("temperature", target))

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active))

    parent = types.SimpleNamespace(poll_timeout_s=2.0, isOn=lambda: True)
    channel = types.SimpleNamespace(
        module_address=lambda: 0,
        is_heat_channel=lambda: True,
        enabled=True,
        value=95.0,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.heat_readback_valid = False
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.applyValue(channel)

    assert calls == [("active", 0, False)]
    assert controller.errorCount == 1


def test_snapshot_rejects_disconnected_heat_sensor_readback():
    module = _load_plugin()
    parent = types.SimpleNamespace(
        main_state="",
        interlock_state="",
        detected_modules="",
        heat_status="",
    )
    controller = module.ESIController(parent)
    controller.identity = {"modules": {}}
    snapshot = {
        "main_state": {"name": "STATE_ON"},
        "interlock_state": {"flags": []},
        "modules": {
            1: {
                "voltage_valid": True,
                "measured_v": 0.0,
                "current_valid": True,
                "measured_a": 0.0,
            },
            2: {
                "voltage_valid": True,
                "measured_v": 0.0,
                "current_valid": True,
                "measured_a": 0.0,
            },
        },
        "heat": {
            "valid": True,
            "monitor_temperature_c": 521.975,
            "monitor_current_a": 0.0,
            "heater_power_w": 0.0,
            "interlock_state": 0,
            "hardware_limits": {"max_temperature_c": 175.0},
        },
    }

    controller._apply_snapshot(snapshot)

    assert controller.heat_readback_valid is False
    assert module.np.isnan(controller.values[0])
    assert parent.heat_status == (
        "INVALID T=522.0 degC; check temperature sensor"
    )


def test_disabling_hv_zeros_target_before_deactivation():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("target", address, value, timeout_s))

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))

    parent = types.SimpleNamespace(poll_timeout_s=2.0, isOn=lambda: True)
    channel = types.SimpleNamespace(
        module_address=lambda: 2,
        is_heat_channel=lambda: False,
        enabled=False,
        value=1200.0,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.applyValue(channel)

    assert calls == [
        ("target", 2, 0.0, 2.0),
        ("active", 2, False, 2.0),
    ]


def test_failed_on_transition_forces_global_safe_off_and_restores_ui():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("target", address, value))

        def set_global_active(self, active, timeout_s):
            calls.append(("global", active))
            raise RuntimeError("activation failed")

        def force_safe_off(self, timeout_s):
            calls.append(("safe_off", timeout_s))

    on_action = types.SimpleNamespace(state=True)
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0,
        poll_timeout_s=2.0,
        getChannels=lambda: [],
        isOn=lambda: True,
        onAction=on_action,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.toggleOn()

    assert calls == [
        ("global", True),
        ("safe_off", 5.0),
    ]
    assert on_action.state is False


def test_failed_global_rollback_keeps_off_action_reachable():
    module = _load_plugin()

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            raise RuntimeError("transition failed")

        def force_safe_off(self, timeout_s):
            raise RuntimeError("rollback failed")

    on_action = types.SimpleNamespace(state=False)
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0,
        poll_timeout_s=2.0,
        getChannels=lambda: [],
        isOn=lambda: False,
        onAction=on_action,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.toggleOn()

    assert on_action.state is True


def test_failed_hv_apply_zeros_and_deactivates_affected_output():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active))

        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("target", address, value))
            if value != 0.0:
                raise RuntimeError("setpoint failed")

    channel = types.SimpleNamespace(
        module_address=lambda: 2,
        is_heat_channel=lambda: False,
        enabled=True,
        value=1000.0,
    )
    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        ramp_rate_v_s=0.0,
        isOn=lambda: True,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.values = {2: 0.0}
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.applyValue(channel)

    assert calls == [
        ("active", 2, True),
        ("target", 2, 1000.0),
        ("target", 2, 0.0),
        ("active", 2, False),
    ]


def test_failed_hv_zero_still_attempts_deactivation():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_target_voltage(self, address, value, timeout_s):
            calls.append(("target", address, value))
            raise RuntimeError("zero failed")

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active))

    channel = types.SimpleNamespace(
        module_address=lambda: 2,
        is_heat_channel=lambda: False,
        enabled=False,
        value=1000.0,
    )
    parent = types.SimpleNamespace(poll_timeout_s=2.0, isOn=lambda: True)
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.applyValue(channel)

    assert calls == [
        ("target", 2, 0.0),
        ("target", 2, 0.0),
        ("active", 2, False),
    ]


def test_dispose_disconnects_before_closing_backend():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def disconnect(self, timeout_s):
            calls.append(("disconnect", timeout_s))

        def close(self):
            calls.append(("close",))

    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=4.0)
    )
    controller.device = FakeDevice()

    controller._dispose_device()

    assert calls == [("disconnect", 4.0), ("close",)]
    assert controller.device is None
