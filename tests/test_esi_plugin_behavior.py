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
            self.acquiring = False

        def toggleOn(self):
            self.super_toggle_called = True

        def startAcquisition(self):
            self.acquiring = True

        def stopAcquisition(self):
            self.acquiring = False

        def initComplete(self):
            self.initialized = True
            self.startAcquisition()
            if self.controllerParent.isOn():
                self.toggleOnFromThread(parallel=True)

    class Device:
        MAXDATAPOINTS = "Max data points"

        def getDefaultSettings(self):
            return {
                f"{self.name}/Interval": {Parameter.VALUE: 0},
                f"{self.name}/{self.MAXDATAPOINTS}": {Parameter.VALUE: 0},
            }

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
    assert [item["Name"] for item in items] == [
        "ESI_HV1",
        "ESI_HV2",
        "ESI_HEAT",
    ]
    assert all(item["Enabled"] is False for item in items)
    assert [item["Value"] for item in items] == [0.0, 0.0, 20.0]
    assert all(item["Min"] == 0.0 for item in items)
    assert [item["Max"] for item in items] == [
        3000.0,
        3000.0,
        175.0,
    ]
    assert [item["Function"] for item in items] == [
        "HVPS-3kB (+/- pair)",
        "HVPS-3kB (+/- pair)",
        "HEAT-CTRL-2410",
    ]
    assert all("Polarity" not in item for item in items)
    assert all("Unit" not in item for item in items)


def test_default_com_is_generic_and_operator_configurable():
    module = _load_plugin()
    device = module.ESIDevice()

    settings = device.getDefaultSettings()

    assert settings["ESI/COM"][module.Parameter.VALUE] == 1


def test_missing_config_creates_only_three_fixed_channels(tmp_path):
    module = _load_plugin()
    device = object.__new__(module.ESIDevice)
    config_file = tmp_path / "ESI.ini"
    applied = []
    exported = []
    device.channels = []
    device.confINI = "ESI.ini"
    device.getChannels = lambda: device.channels
    device.customConfigFile = lambda _name: config_file
    device.print = lambda *args, **kwargs: None

    def update(items, file):
        applied.extend(items)
        assert file == config_file
        device.channels = [
            types.SimpleNamespace(module=item["Module"], name=item["Name"])
            for item in items
        ]

    device.updateChannelConfig = update
    device.exportConfiguration = lambda **kwargs: exported.append(kwargs)

    device.loadConfiguration(useDefaultFile=True)

    assert [item["Module"] for item in applied] == [1, 2, 0]
    assert [item["Name"] for item in applied] == [
        "ESI_HV1",
        "ESI_HV2",
        "ESI_HEAT",
    ]
    assert exported == [{"useDefaultFile": True}]


def test_generic_nine_channel_config_is_migrated_to_fixed_layout(tmp_path):
    module = _load_plugin()
    device = object.__new__(module.ESIDevice)
    config_file = tmp_path / "ESI.ini"
    applied = []
    exported = []
    device.channels = [
        types.SimpleNamespace(module=2, name=f"ESI{index}")
        for index in range(1, 10)
    ]
    device.confINI = "ESI.ini"
    device.getChannels = lambda: device.channels
    device.customConfigFile = lambda _name: config_file

    def update(items, file):
        applied.extend(items)
        assert file == config_file

    device.updateChannelConfig = update
    device.exportConfiguration = lambda **kwargs: exported.append(kwargs)

    device.ensureFixedChannels(persist=True)

    assert [item["Module"] for item in applied] == [1, 2, 0]
    assert len(applied) == 3
    assert exported == [{"useDefaultFile": True}]


def test_polarity_channel_config_migrates_to_safe_module_pairs(tmp_path):
    module = _load_plugin()
    device = object.__new__(module.ESIDevice)
    config_file = tmp_path / "ESI.ini"
    applied = []
    device.channels = [
        types.SimpleNamespace(
            module=1, name="ESI_HV1+", value=100.0, enabled=False
        ),
        types.SimpleNamespace(
            module=1, name="ESI_HV1-", value=250.0, enabled=True
        ),
        types.SimpleNamespace(
            module=2, name="ESI_HV2+", value=300.0, enabled=False
        ),
        types.SimpleNamespace(
            module=2, name="ESI_HV2-", value=450.0, enabled=False
        ),
        types.SimpleNamespace(
            module=0, name="ESI_HEAT", value=80.0, enabled=True
        ),
    ]
    device.confINI = "ESI.ini"
    device.getChannels = lambda: device.channels
    device.customConfigFile = lambda _name: config_file
    device.updateChannelConfig = lambda items, _file: applied.extend(items)

    device.ensureFixedChannels()

    assert [item["Name"] for item in applied] == [
        "ESI_HV1",
        "ESI_HV2",
        "ESI_HEAT",
    ]
    assert [item["Value"] for item in applied] == [250.0, 300.0, 80.0]
    assert all(item["Enabled"] is False for item in applied)


def test_panel_controls_one_target_and_one_output_state_per_module():
    module = _load_plugin()
    updates = []

    class ParameterValue:
        def __init__(self, channel, attribute):
            self.channel = channel
            self.attribute = attribute

        @property
        def value(self):
            return getattr(self.channel, self.attribute)

        @value.setter
        def value(self, value):
            setattr(self.channel, self.attribute, value)
            updates.append((self.attribute, value))

    channel = types.SimpleNamespace(
        module=1,
        enabled=False,
        value=10.0,
        VALUE=module.Channel.VALUE,
        ENABLED=module.Channel.ENABLED,
        module_address=lambda: 1,
        is_heat_channel=lambda: False,
    )
    parameters = {
        module.Channel.VALUE: ParameterValue(channel, "value"),
        module.Channel.ENABLED: ParameterValue(channel, "enabled"),
    }
    channel.getParameterByName = lambda name: parameters[name]
    device = object.__new__(module.ESIDevice)
    device.loading = False
    device.getChannels = lambda: [channel]
    device._update_operator_panel = lambda: None

    device._panel_target_changed(1, 125.0)
    device._panel_output_selected(1, 1)
    device._panel_output_selected(1, 0)

    assert updates == [
        ("value", 125.0),
        ("enabled", True),
        ("enabled", False),
    ]


def test_initialization_uses_inline_backend_and_reports_com_on_failure(monkeypatch):
    module = _load_plugin()
    constructor_kwargs = []
    messages = []

    class FakeDriver:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self._process_backend_disabled_reason = "inline backend selected"

        def connect(self, timeout_s):
            raise RuntimeError("open failed")

        def disconnect(self, timeout_s):
            return True

        def close(self):
            return None

    parent = types.SimpleNamespace(
        com=16,
        baudrate=230400,
        connect_timeout_s=5.0,
    )
    controller = module.ESIController(parent)
    controller.print = lambda message, **kwargs: messages.append(message)
    controller.initializing = True
    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: FakeDriver)

    controller.runInitialization()

    assert constructor_kwargs[0]["com"] == 16
    assert constructor_kwargs[0]["process_backend"] is False
    assert "allow_negative" not in constructor_kwargs[0]
    assert any("initialization failed on COM16" in message for message in messages)
    assert controller.device is None
    assert controller.initializing is False


def test_initialization_configures_verified_hv_steps_while_outputs_are_off(
    monkeypatch,
):
    module = _load_plugin()
    calls = []

    class FakeDriver:
        _process_backend_disabled_reason = ""

        def __init__(self, **_kwargs):
            pass

        def connect(self, timeout_s):
            calls.append(("connect", timeout_s))

        def set_global_active(self, active, timeout_s):
            calls.append(("global", active, timeout_s))

        def collect_identity(self, timeout_s):
            calls.append(("identity", timeout_s))
            return {"modules": {}}

        def force_safe_off(self, timeout_s):
            calls.append(("safe_off", timeout_s))

        def configure_hv_max_voltage_steps(self, value, timeout_s):
            calls.append(("max_steps", value, timeout_s))
            return {1: value, 2: value}

        def collect_diagnostics(self, timeout_s):
            calls.append(("diagnostics", timeout_s))
            return {"verified": True}

    emitted = []
    parent = types.SimpleNamespace(
        com=16,
        baudrate=230400,
        connect_timeout_s=5.0,
        poll_timeout_s=2.0,
        heat_voltage_limit_v=0.0,
        heat_current_limit_a=0.0,
        heat_power_limit_w=0.0,
    )
    controller = module.ESIController(parent)
    controller.signalComm = types.SimpleNamespace(
        initCompleteSignal=types.SimpleNamespace(emit=lambda: emitted.append(True))
    )
    controller._apply_snapshot = lambda snapshot: calls.append(("snapshot", snapshot))
    controller.initializing = True
    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: FakeDriver)

    controller.runInitialization()

    assert calls == [
        ("connect", 5.0),
        ("global", True, 5.0),
        ("identity", 2.0),
        ("safe_off", 5.0),
        ("max_steps", 10.008, 5.0),
        ("diagnostics", 2.0),
        ("snapshot", {"verified": True}),
    ]
    assert emitted == [True]
    assert controller.initializing is False


def test_init_complete_resumes_pending_on_toggle():
    module = _load_plugin()
    calls = []
    parent = types.SimpleNamespace(
        ensureFixedChannels=lambda **kwargs: calls.append(("channels", kwargs)),
        isOn=lambda: True,
    )
    controller = module.ESIController(parent)
    controller.device = object()
    controller.print = lambda *args, **kwargs: None
    controller.toggleOnFromThread = (
        lambda parallel=True: calls.append(("toggle", parallel))
    )

    controller.initComplete()

    assert controller.initialized is True
    assert calls == [
        ("channels", {"persist": True}),
        ("toggle", True),
    ]


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


def test_operator_panel_widths_are_fixed_and_aligned():
    module = _load_plugin()
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert module._ESI_HV_CARD_WIDTH == 300
    assert module._ESI_HEAT_CARD_WIDTH == (
        2 * module._ESI_HV_CARD_WIDTH + module._ESI_CARD_SPACING
    )
    assert module._ESI_PANEL_STANDBY == "color: #d69e2e; font-weight: 600;"
    assert "else _ESI_PANEL_STANDBY" in source
    assert "polarity = measurement_polarity.get(address)" in source
    assert 'if polarity == "negative"' in source
    assert 'if polarity == "positive"' in source
    assert "({polarity_code} ADC)" in source
    assert "{polarity_code} {measured:.1f} V" in source


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
        def set_hv_module_target(self, address, value, timeout_s):
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
        ("module", 1, False),
        ("module", 2, False),
        ("global", True),
        ("apply", 1, 1200.0),
    ]
    assert controller.acquiring is True


def test_on_sequence_programs_heat_target_before_activation():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
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
        ("module", 1, False),
        ("module", 2, False),
        ("global", True),
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
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append((address, value, timeout_s))

    parent = types.SimpleNamespace(ramp_rate_v_s=1000.0, poll_timeout_s=2.0)
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    controller._ramp_target(1, 0.0, 250.0)

    assert calls == [
        (1, 250.0 / 3.0, 2.0),
        (1, 500.0 / 3.0, 2.0),
        (1, 250.0, 2.0),
    ]


def test_active_hv_target_change_uses_configured_ramp(monkeypatch):
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(("target", address, value, timeout_s))
            return value

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))
            return active

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        ramp_rate_v_s=100.0,
        isOn=lambda: True,
    )
    channel = types.SimpleNamespace(
        module_address=lambda: 1,
        is_heat_channel=lambda: False,
        enabled=True,
        name="ESI_HV1",
        value=30.0,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.targets = {1: 10.0}
    controller.module_active = {1: True}
    controller.global_enabled = True
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)

    controller.applyValue(channel)

    assert calls == [
        ("target", 1, 20.0, 2.0),
        ("target", 1, 30.0, 2.0),
        ("active", 1, True, 2.0),
    ]
    assert controller.targets[1] == 30.0


def test_heat_channel_sets_temperature_without_using_hv_voltage_path():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))

        def set_heater_temperature(self, target, timeout_s):
            calls.append(("temperature", target, timeout_s))

        def set_hv_module_target(self, *args, **kwargs):
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
        "enabled": True,
        "modules": {
            1: {
                "module_active": True,
                "control_active": True,
                "measurement": {"voltage_polarity": "positive"},
                "target_v": 10.0,
                "voltage_valid": True,
                "measured_v": 0.0,
                "current_valid": True,
                "measured_a": 0.0,
                "led": {"red": True, "green": False, "blue": False},
                "pwm": {"voltage_set_v": 10.0, "voltage_measured_v": 0.0},
            },
            2: {
                "module_active": False,
                "control_active": False,
                "measurement": {"voltage_polarity": "negative"},
                "target_v": 0.0,
                "voltage_valid": True,
                "measured_v": 0.0,
                "current_valid": True,
                "measured_a": 0.0,
                "led": {"red": True, "green": True, "blue": False},
                "pwm": {"voltage_set_v": 0.0, "voltage_measured_v": 0.0},
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
    assert controller.targets == {1: 10.0, 2: 0.0}
    assert controller.module_active == {1: True, 2: False}
    assert controller.module_control_active == {1: True, 2: False}
    assert controller.module_led_rgb == {
        1: (True, False, False),
        2: (True, True, False),
    }
    assert controller.pwm_voltage_set == {1: 10.0, 2: 0.0}
    assert controller.pwm_voltage_measured == {1: 0.0, 2: 0.0}
    assert controller.measurement_polarity == {1: "positive", 2: "negative"}
    assert controller.global_enabled is True
    assert parent.heat_status == (
        "INVALID T=522.0 degC; check temperature sensor"
    )


def test_disabling_hv_uses_module_output_gate():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        isOn=lambda: True,
        getChannels=lambda: [],
    )
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

    assert calls == [("active", 2, False, 2.0)]


def test_hv_pair_applies_one_unsigned_module_target_without_adc_selection():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(("target", address, value, timeout_s))
            return value

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active, timeout_s))
            return active

        def select_hv_measurement(self, *args, **kwargs):
            raise AssertionError("ADC selection must not control the physical outputs")

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        isOn=lambda: True,
    )
    channel = types.SimpleNamespace(
        module_address=lambda: 1,
        is_heat_channel=lambda: False,
        enabled=True,
        name="ESI_HV1",
        value=10.0,
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True

    controller.applyValue(channel)

    assert calls == [
        ("target", 1, 10.0, 2.0),
        ("active", 1, True, 2.0),
    ]


def test_failed_on_transition_forces_global_safe_off_and_restores_ui():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("module", address, active))

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
        ("module", 1, False),
        ("module", 2, False),
        ("global", True),
        ("safe_off", 5.0),
    ]
    assert on_action.state is False


def test_failed_global_rollback_keeps_off_action_reachable():
    module = _load_plugin()

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
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

        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(("target", address, value))
            if value != 0.0:
                raise RuntimeError("setpoint failed")

    channel = types.SimpleNamespace(
        module_address=lambda: 2,
        is_heat_channel=lambda: False,
        name="ESI_HV2",
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
        ("target", 2, 1000.0),
        ("target", 2, 0.0),
        ("active", 2, False),
    ]


def test_failed_hv_gate_activation_rolls_target_back_to_zero():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(("target", address, value))
            return value

        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active))
            if active:
                raise RuntimeError("enable verification failed")
            return False

    channel = types.SimpleNamespace(
        module_address=lambda: 1,
        is_heat_channel=lambda: False,
        name="ESI_HV1",
        enabled=True,
        value=10.0,
    )
    parent = types.SimpleNamespace(poll_timeout_s=2.0, isOn=lambda: True)
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.applyValue(channel)

    assert calls == [
        ("target", 1, 10.0),
        ("active", 1, True),
        ("target", 1, 0.0),
        ("active", 1, False),
    ]
    assert controller.errorCount == 1


def test_failed_hv_disable_is_reported():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_output_active(self, address, active, timeout_s):
            calls.append(("active", address, active))
            raise RuntimeError("zero failed")

    channel = types.SimpleNamespace(
        module_address=lambda: 2,
        is_heat_channel=lambda: False,
        name="ESI_HV2",
        enabled=False,
        value=1000.0,
    )
    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        isOn=lambda: True,
        getChannels=lambda: [],
    )
    controller = module.ESIController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None

    controller.applyValue(channel)

    assert calls == [("active", 2, False)]
    assert controller.errorCount == 1


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
