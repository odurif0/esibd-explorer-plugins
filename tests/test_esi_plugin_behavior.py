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
        MIN = "Min"
        MAX = "Max"

        def __init__(self, channelParent=None, tree=None):
            self.channelParent = channelParent
            self.tree = tree

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

    assert [item["Module"] for item in items] == [2, 3]
    assert [item["Name"] for item in items] == ["ESI_HV2", "ESI_HV3"]
    assert all(item["Enabled"] is False for item in items)
    assert all(item["Value"] == 0.0 for item in items)
    assert all(item["Min"] == 0.0 for item in items)
    assert all(item["Max"] == 3000.0 for item in items)


def test_channel_defaults_enforce_3kv_positive_range():
    module = _load_plugin()
    parent = types.SimpleNamespace()
    channel = module.ESIChannel(channelParent=parent, tree=None)

    defaults = channel.getDefaultChannel()

    assert defaults[channel.VALUE][module.Parameter.MIN] == 0.0
    assert defaults[channel.VALUE][module.Parameter.MAX] == 3000.0
    assert defaults[channel.MODULE][module.Parameter.VALUE] == 2


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
            module_address=lambda: 2,
            enabled=True,
            value=1200.0,
        ),
        types.SimpleNamespace(
            module_address=lambda: 3,
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
    controller.applyValue = lambda channel: calls.append(
        ("apply", channel.module_address(), channel.value if channel.enabled else 0.0)
    )

    controller.toggleOn()

    assert calls == [
        ("target", 2, 0.0),
        ("target", 3, 0.0),
        ("global", True),
        ("module", 2, True),
        ("module", 3, False),
        ("apply", 2, 1200.0),
        ("apply", 3, 0.0),
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
