"""Packaging and ESIBD bridge checks for the standalone ESI plugin."""

from __future__ import annotations

import importlib.util
import sys
import types
import time
from enum import Enum
from pathlib import Path

from PIL import Image
import pytest


PLUGIN_PATH = Path(__file__).resolve().parents[1] / "esi" / "esi_plugin.py"
ICON_PATH = Path(__file__).resolve().parents[1] / "esi" / "esi.png"


def _install_esibd_stubs():
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")

    class PARAMETERTYPE(Enum):
        INT = "INT"
        FLOAT = "FLOAT"
        EXP = "EXP"
        BOOL = "BOOL"
        LABEL = "LABEL"
        COMBO = "COMBO"

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

        def print(self, message, **kwargs):
            pass

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
    plugins.LiveDisplay = type("LiveDisplay", (), {})
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
    assert module.ESIDevice.supportedVersion == "1.0.2"
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

    assert [item["Module"] for item in items] == [1, 2, 0, 1, 2]
    assert [item["Name"] for item in items] == [
        "ESI_HV1", "ESI_HV2", "ESI_HEAT", "ESI_HV1_I", "ESI_HV2_I",
    ]
    assert all(item["Enabled"] is True for item in items[3:])
    items = items[:3]  # Output-control defaults remain unchanged.
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


def test_missing_config_creates_three_controls_and_two_current_channels(tmp_path):
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

    assert [item["Module"] for item in applied] == [1, 2, 0, 1, 2]
    assert [item["Name"] for item in applied] == [
        "ESI_HV1", "ESI_HV2", "ESI_HEAT", "ESI_HV1_I", "ESI_HV2_I",
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

    assert [item["Module"] for item in applied] == [1, 2, 0, 1, 2]
    assert len(applied) == 5
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
        "ESI_HV1", "ESI_HV2", "ESI_HEAT", "ESI_HV1_I", "ESI_HV2_I",
    ]
    assert [item["Value"] for item in applied[:3]] == [250.0, 300.0, 80.0]
    assert all(item["Enabled"] is False for item in applied[:3])


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


def test_initialization_uses_isolated_backend_and_reports_com_on_failure(monkeypatch):
    module = _load_plugin()
    constructor_kwargs = []
    messages = []

    class FakeDriver:
        def __init__(self, **kwargs):
            constructor_kwargs.append(kwargs)
            self._process_backend_disabled_reason = ""

        def connect(self, timeout_s):
            raise RuntimeError("open failed")

        def disconnect(self, timeout_s, *, on_discharge):
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
    assert constructor_kwargs[0]["process_backend"] is True
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

        def select_hv_voltage_adc(self, address, *, negative, timeout_s):
            calls.append(("adc", address, negative, timeout_s))

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
        hv2_adc_connector="NEG",
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
        # The voltage ADC reads each module's electrospray connector (HV1 default POS).
        ("adc", 1, False, 2.0),
        ("adc", 2, True, 2.0),
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


def test_operator_cards_keep_their_width_and_heat_panel_can_shrink():
    module = _load_plugin()
    source = PLUGIN_PATH.read_text(encoding="utf-8")

    assert module._ESI_HV_CARD_WIDTH == 300
    assert module._ESI_HEAT_CARD_WIDTH == (
        2 * module._ESI_HV_CARD_WIDTH + module._ESI_CARD_SPACING
    )
    assert "heat_card.setMaximumWidth(_ESI_HEAT_CARD_WIDTH)" in source
    assert module._ESI_PANEL_STANDBY == "color: #d69e2e; font-weight: 600;"
    assert "else _ESI_PANEL_STANDBY" in source
    assert "_adc_readback_text(adc_readback.get(address))" in source
    assert 'widgets["pwm_set"]' in source and 'widgets["pwm_measured"]' in source


def test_enabled_change_forces_hardware_apply():
    module = _load_plugin()
    channel = module.ESIChannel(
        channelParent=types.SimpleNamespace(loading=False),
        tree=None,
    )
    channel.enabled = True

    channel.enabledChanged()

    assert channel.base_enabled_changed is True
    assert channel.applied is True


def test_on_sequence_forces_every_output_off_and_energizes_nothing():
    # 2026-10-07: ON restarted a saved HV1 selection at 1000 V. ON now opens the global gate only.
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(("target", address, value))

        def set_heater_temperature(self, value, timeout_s, *, cancel_event=None):
            calls.append(("heat_target", value))

        def set_global_active(self, active, timeout_s):
            calls.append(("global", active))

        def set_output_active(self, address, active, timeout_s, *, cancel_event=None):
            calls.append(("module", address, active))

    channels = [
        types.SimpleNamespace(module_address=lambda: 1, is_heat_channel=lambda: False, enabled=True, value=1000.0),
        types.SimpleNamespace(module_address=lambda: 2, is_heat_channel=lambda: False, enabled=False, value=2300.0),
        types.SimpleNamespace(module_address=lambda: 0, is_heat_channel=lambda: True, enabled=True, value=90.0),
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
    controller.applyValue = lambda channel: calls.append(("apply", channel.module_address()))

    controller.toggleOn()

    assert calls == [
        ("module", 0, False),
        ("module", 1, False),
        ("module", 2, False),
        ("global", True),
    ]
    assert controller.module_active == {1: False, 2: False} and controller.targets == {1: 0.0, 2: 0.0}
    assert controller.acquiring is True


def test_init_complete_never_reapplies_saved_targets_or_selections():
    # Explorer's DeviceController.initComplete calls updateValues(apply=True) when the device is ON.
    module = _load_plugin()
    calls = []
    parent = types.SimpleNamespace(
        ensureFixedChannels=lambda **kwargs: calls.append("channels"),
        isOn=lambda: True,
        updateValues=lambda **kwargs: calls.append(("updateValues", kwargs)),
    )
    controller = module.ESIController(parent)
    controller.device = object()
    controller.print = lambda *args, **kwargs: calls.append("print")
    controller.toggleOnFromThread = lambda parallel=True: calls.append(("toggle", parallel))
    module.DeviceController.initComplete = lambda self: calls.append("explorer initComplete")

    controller.initComplete()

    assert calls == ["channels", ("toggle", True), "print"]
    assert controller.acquiring is True and controller.initialized is True


class _ExplorerLikeChannel:
    """Explorer 1.0.2: Channel.loading is a read-only view of the device's loading counter."""

    def __init__(self, device, module_number, enabled, value, heat=False):
        self.device, self.module, self.name = device, module_number, f"ESI_HV{module_number}"
        self._enabled, self._value, self.heat = enabled, value, heat
        self.events = []
        self.VALUE, self.ENABLED = "Value", "Enabled"

    @property
    def loading(self):
        return self.device.loading

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        self.events.append(("value", value, self.device.loading))

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        self.events.append(("enabled", value, self.device.loading))

    def module_address(self):
        return self.module

    def is_heat_channel(self):
        return self.heat

    def getParameterByName(self, name):
        return None  # Explorer returns None for a parameter this stand-in does not model.


def _counting_device(module, channels):
    class CountingESI(module.ESIDevice):
        _count = 0

        @property
        def loading(self):
            return self._count != 0

        @loading.setter
        def loading(self, value):  # Explorer's Plugin.loading counter
            self._count += 1 if value else -1

    device = object.__new__(CountingESI)
    device.getChannels = lambda: channels
    device.name = "ESI"
    device.printed = []
    device.print = lambda text, **kwargs: device.printed.append(text)
    return device


def test_draft_target_commit_works_with_explorer_read_only_channel_loading():
    # 2026-10-07: ten panel actions failed with "property 'loading' of 'ESIChannel' object has no setter".
    module = _load_plugin()
    channels = []
    device = _counting_device(module, channels)
    channel = _ExplorerLikeChannel(device, 1, enabled=True, value=1000.0)
    channels.append(channel)

    class FocusedSpin:  # The operator typed 0 and clicked elsewhere.
        def hasFocus(self):
            return True

        def blockSignals(self, value):
            return False

        def interpretText(self):
            pass

        def value(self):
            return 0.0

    device.esiHVCards = {1: {"target": FocusedSpin()}}

    device._finish_setpoint_edits(channel)

    assert channel.value == 0.0 and channel.events == [("value", 0.0, True)]  # Committed, event suppressed.
    assert device._count == 0


def test_saved_and_on_output_selections_start_off_with_targets_kept(monkeypatch):
    module = _load_plugin()
    channels = []
    device = _counting_device(module, channels)
    hv1 = _ExplorerLikeChannel(device, 1, enabled=True, value=1000.0)
    hv2 = _ExplorerLikeChannel(device, 2, enabled=False, value=500.0)
    heat = _ExplorerLikeChannel(device, 0, enabled=True, value=90.0, heat=True)
    channels.extend([hv1, hv2, heat])
    monkeypatch.setattr(module.Device, "loadConfiguration", lambda self, **kwargs: None, raising=False)

    device.loadConfiguration(useDefaultFile=False)

    assert [ch.enabled for ch in channels] == [False, False, False]
    assert [ch.value for ch in channels] == [1000.0, 500.0, 90.0]
    assert hv1.events == [("enabled", False, True)] and heat.events == [("enabled", False, True)]
    assert device._count == 0
    assert device.printed == ["Outputs start OFF at start: ESI_HV1 (target 1000 V kept), "
                              "ESI_HV0 (target 90 °C kept). Select an output to energize it."]
    hv1.enabled = True  # Selected again, then the plugin is switched ON.
    toggles = []
    device.controller = types.SimpleNamespace(initialized=True, initializing=False, transitioning=False,
                                              toggleOnFromThread=lambda parallel=True: toggles.append(parallel))
    device.onAction = types.SimpleNamespace(state=False)
    device._sync_local_on_action = lambda: None
    device.isOn = lambda: device.onAction.state

    device.setOn(True)

    assert hv1.enabled is False and hv1.value == 1000.0 and toggles == [True]


def test_off_sequence_uses_driver_confirmed_disconnect():
    module = _load_plugin()
    calls = []

    class FakeDevice:
        def disconnect(self, timeout_s, *, on_discharge):
            assert callable(on_discharge)
            calls.append(timeout_s)
            self.connected = False
            return True

        def close(self):
            pass

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


def test_ramp_wait_yields_to_off_without_another_step():
    module = _load_plugin()
    calls = []
    parent = types.SimpleNamespace(ramp_rate_v_s=10.0, poll_timeout_s=2.0)
    controller = module.ESIController(parent)

    class FakeDevice:
        def set_hv_module_target(self, address, value, timeout_s):
            calls.append(value)
            controller._output_cancel.set()  # OFF arrives during the first step

    controller.device = FakeDevice()
    started = time.monotonic()

    assert controller._ramp_target(1, 0.0, 100.0) is False

    assert calls == [1.0]
    assert time.monotonic() - started < 0.05, "OFF must not wait for the step interval"


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
        def set_output_active(self, address, active, timeout_s, *, cancel_event=None):
            assert cancel_event is controller._output_cancel
            calls.append(("active", address, active, timeout_s))

        def set_heater_temperature(self, target, timeout_s, *, cancel_event=None):
            assert cancel_event is controller._output_cancel
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


@pytest.mark.parametrize('operation', ['target', 'on'])
def test_heat_ready_wait_uses_the_existing_output_cancellation_token(operation):
    module = _load_plugin()
    channel = types.SimpleNamespace(module_address=lambda: 0, is_heat_channel=lambda: True,
                                    enabled=True, value=30., name='HEAT')
    parent = types.SimpleNamespace(poll_timeout_s=1., isOn=lambda: True)
    controller = module.ESIController(parent)
    controller.initialized = True
    controller.heat_readback_valid = True
    controller.errorCount = 0
    controller.print = lambda *args, **kwargs: None
    writes = []
    class Device:
        def set_heater_temperature(self, target, timeout_s, *, cancel_event=None):
            assert cancel_event is controller._output_cancel
            if operation == 'target':
                controller._output_cancel.set()  # Stop while acquiring a ready datum.
                if cancel_event.is_set():
                    raise InterruptedError('Stopped before target')
            writes.append(('target', target))
        def set_output_active(self, address, active, timeout_s, *, cancel_event=None):
            if active and operation == 'on':
                assert cancel_event is controller._output_cancel
                controller._output_cancel.set()
                if cancel_event.is_set():
                    raise InterruptedError('Stopped before ON')
            writes.append(('module', active))
            return active
    controller.device = Device()
    controller.applyValue(channel)
    if operation == 'target':
        assert ('target', 30.) not in writes
    assert ('module', True) not in writes
    assert ('module', False) in writes  # Rollback remains possible after Stop.


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


def test_snapshot_rejects_disconnected_heat_sensor_readback(monkeypatch):
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

    queued = []
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    controller._apply_snapshot(snapshot)
    # All four ESIBD settings are widget-backed: no writes before dispatch.
    assert vars(parent) == dict.fromkeys(
        ("main_state", "interlock_state", "detected_modules", "heat_status"), ""
    )
    assert len(queued) == 1
    queued[0]()

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

        def disconnect(self, timeout_s, *, on_discharge):
            calls.append(("disconnect", timeout_s))
            self.connected = False
            return True

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
        ("module", 0, False),
        ("module", 1, False),
        ("module", 2, False),
        ("global", True),
        ("disconnect", 5.0),
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
        def disconnect(self, timeout_s, *, on_discharge):
            calls.append(("disconnect", timeout_s))
            return True

        def close(self):
            calls.append(("close",))

    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=4.0)
    )
    controller.device = FakeDevice()

    controller._dispose_device()

    assert calls == [("disconnect", 4.0), ("close",)]
    assert controller.device is None


def test_default_settings_include_config_management_keys():
    module = _load_plugin()
    device = module.ESIDevice()

    settings = device.getDefaultSettings()

    assert "ESI/Operating config" in settings
    assert settings["ESI/Operating config"][module.Parameter.VALUE] == -1
    assert "ESI/Available configs" in settings
    assert settings["ESI/Available configs"][module.Parameter.VALUE] == "n/a"
    assert "ESI/Loaded config" in settings
    assert settings["ESI/Loaded config"][module.Parameter.VALUE] == "n/a"


def test_controller_has_config_state_attributes():
    module = _load_plugin()
    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=5.0)
    )
    assert controller.available_configs == []
    assert controller.available_configs_text == "n/a"
    assert controller.loaded_config_text == "n/a"


def test_refresh_available_configs_populates_list():
    module = _load_plugin()

    class FakeDevice:
        def list_configs(self, timeout_s=5.0):
            return [
                {"index": 5, "name": "Spray", "active": True, "valid": True},
                {"index": 10, "name": "Idle", "active": False, "valid": True},
            ]

    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=5.0)
    )
    controller.device = FakeDevice()
    controller._refresh_available_configs()
    assert len(controller.available_configs) == 2
    assert "5: Spray" in controller.available_configs_text
    assert "10: Idle" in controller.available_configs_text


def test_refresh_available_configs_handles_no_configs():
    module = _load_plugin()

    class FakeDevice:
        def list_configs(self, timeout_s=5.0):
            return []

    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=5.0)
    )
    controller.device = FakeDevice()
    controller._refresh_available_configs()
    assert controller.available_configs == []
    assert controller.available_configs_text == "No saved configs"


def test_refresh_available_configs_handles_error():
    module = _load_plugin()

    class FakeDevice:
        def list_configs(self, timeout_s=5.0):
            raise RuntimeError("timeout")

    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=5.0)
    )
    controller.device = FakeDevice()
    controller.print = lambda *a, **k: None
    controller._refresh_available_configs()
    assert controller.available_configs == []
    assert controller.available_configs_text == "Unavailable"


def test_operating_config_ready_rejects_unselected():
    module = _load_plugin()
    controller = module.ESIController(
        types.SimpleNamespace(connect_timeout_s=5.0, operating_config=-1)
    )
    ready, reason, idx = controller._operating_config_ready()
    assert not ready
    assert "select" in reason
    assert idx == -1


def test_operating_config_ready_rejects_invalid_slot():
    module = _load_plugin()
    parent = types.SimpleNamespace(connect_timeout_s=5.0, operating_config=3)
    controller = module.ESIController(parent)
    controller.available_configs = [
        {"index": 3, "name": "Bad", "active": True, "valid": False}
    ]
    ready, reason, idx = controller._operating_config_ready()
    assert not ready
    assert "invalid" in reason
    assert idx == 3


def test_operating_config_ready_accepts_valid_slot():
    module = _load_plugin()
    parent = types.SimpleNamespace(connect_timeout_s=5.0, operating_config=7)
    controller = module.ESIController(parent)
    controller.available_configs = [
        {"index": 7, "name": "Good", "active": True, "valid": True}
    ]
    ready, reason, idx = controller._operating_config_ready()
    assert ready
    assert idx == 7


def test_load_config_now_requires_initialized_device():
    module = _load_plugin()
    parent = types.SimpleNamespace(connect_timeout_s=5.0, name="ESI")
    controller = module.ESIController(parent)
    controller.initialized = False
    controller.print = lambda *a, **k: None
    controller.loadOperatingConfigNow()
    assert controller.device is None


def test_load_config_now_requires_device_on():
    module = _load_plugin()
    parent = types.SimpleNamespace(
        connect_timeout_s=5.0, name="ESI",
        isOn=lambda: False, operating_config=5,
    )
    controller = module.ESIController(parent)
    controller.initialized = True
    controller.device = object()
    controller.available_configs = [
        {"index": 5, "name": "Test", "active": True, "valid": True}
    ]
    printed = []
    controller.print = lambda msg, flag=None: printed.append(msg)
    controller.loadOperatingConfigNow()
    assert any("OFF" in msg for msg in printed)


def test_invoke_gui_callback_drops_update_when_dispatcher_fails(monkeypatch):
    """A failed Qt emit must never run the GUI callback on the worker thread."""
    module = _load_plugin()

    pyqt = types.ModuleType("PyQt6")
    pyqt.__path__ = []
    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QObject = object
    qtcore.pyqtSignal = lambda *a, **k: None
    qtcore.pyqtSlot = lambda *a, **k: lambda function: function

    class _FakeQt:
        class ConnectionType:
            QueuedConnection = "queued"

    qtcore.Qt = _FakeQt

    class _FakeQThread:
        @staticmethod
        def currentThread():
            return "worker-thread"

    qtcore.QThread = _FakeQThread
    pyqt.QtCore = qtcore

    qtwidgets = types.ModuleType("PyQt6.QtWidgets")

    class _FakeApp:
        @staticmethod
        def thread():
            return "gui-thread"

    class _FakeQApplication:
        @staticmethod
        def instance():
            return _FakeApp()

    qtwidgets.QApplication = _FakeQApplication
    pyqt.QtWidgets = qtwidgets

    monkeypatch.setitem(sys.modules, "PyQt6", pyqt)
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", qtcore)
    monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", qtwidgets)

    class _ExplodingDispatcher:
        def emit(self, callback):
            raise RuntimeError("dispatch failed")

    previous = getattr(module._invoke_gui_callback, "_dispatcher", None)
    module._invoke_gui_callback._dispatcher = _ExplodingDispatcher()
    try:
        called = []
        module._invoke_gui_callback(lambda: called.append(1))
        assert called == []
    finally:
        if previous is None:
            del module._invoke_gui_callback._dispatcher
        else:
            module._invoke_gui_callback._dispatcher = previous
