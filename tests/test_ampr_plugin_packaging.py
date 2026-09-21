"""Packaging checks for the standalone ESIBD Explorer AMPR plugin."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import types
from enum import Enum
from pathlib import Path

import numpy as np
import pytest


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "ampr_plugin.py"
)
PLUGIN_A_PATH = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "ampr_plugin.py"
)
PLUGIN_B_PATH = (
    Path(__file__).resolve().parents[1]
    / "ampr_b"
    / "ampr_plugin.py"
)
VENDOR_ROOT = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "vendor"
    / "runtime"
)
VENDOR_A_ROOT = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "vendor"
    / "runtime"
)
VENDOR_B_ROOT = (
    Path(__file__).resolve().parents[1]
    / "ampr_b"
    / "vendor"
    / "runtime"
)
ICON_PATH = (
    Path(__file__).resolve().parents[1]
    / "ampr_a"
    / "ampr.png"
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
        ADVANCED = "Advanced"
        MAX = "Max"
        EVENT = "Event"
        TOOLTIP = "Tooltip"
        INDICATOR = "Indicator"
        PARAMETER_TYPE = "PARAMETER_TYPE"

    class Channel:
        real = True
        COLLAPSE = "Collapse"
        NAME = "Name"
        ACTIVE = "Active"
        DISPLAY = "Display"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"
        MONITOR = "Monitor"
        SCALING = "Scaling"
        MIN = "Min"
        MAX = "Max"
        OPTIMIZE = "Optimize"

        def __init__(self, channelParent=None, tree=None):
            self.channelParent = channelParent
            self.tree = tree

        def setDisplayedParameters(self):
            self.displayedParameters = [
                self.NAME,
                self.VALUE,
                self.MONITOR,
                self.MIN,
                self.MAX,
                self.ACTIVE,
                self.REAL,
                self.OPTIMIZE,
                self.DISPLAY,
            ]

        def getDefaultChannel(self):
            return {
                self.ACTIVE: {Parameter.HEADER: "A", Parameter.ADVANCED: True},
                self.DISPLAY: {Parameter.HEADER: "D", Parameter.ADVANCED: False},
                self.REAL: {Parameter.HEADER: "R", Parameter.ADVANCED: True},
                self.ENABLED: {Parameter.HEADER: "E", Parameter.ADVANCED: True},
                self.VALUE: {Parameter.HEADER: "Unit"},
                self.SCALING: {Parameter.VALUE: "normal"},
                self.MIN: {},
                "Max": {},
            }

        def getSortedDefaultChannel(self):
            return self.getDefaultChannel()

        def initGUI(self, item):
            self.super_init_gui_called = item

        def scalingChanged(self):
            self.rowHeight = 18

        def nameChanged(self):
            self.super_name_changed_called = True

        def valueChanged(self):
            self.super_value_changed_called = True

        def equationChanged(self):
            self.super_equation_changed_called = True

        def activeChanged(self):
            self.super_active_changed_called = True

        def enabledChanged(self):
            self.super_enabled_changed_called = True

        def updateDisplay(self):
            self.super_update_display_called = True

        def updateMin(self):
            self.super_update_min_called = True

        def updateMax(self):
            self.super_update_max_called = True

    class DeviceController:
        pass

    class ToolButton:
        def __init__(self):
            self.maximum_height = None
            self.minimum_width = None
            self.text = None
            self.checkable = None
            self.auto_raise = None
            self.checked = None

        def setMaximumHeight(self, height):
            self.maximum_height = height

        def setMinimumWidth(self, width):
            self.minimum_width = width

        def setText(self, text):
            self.text = text

        def setCheckable(self, checkable):
            self.checkable = checkable

        def setAutoRaise(self, auto_raise):
            self.auto_raise = auto_raise

        def setChecked(self, checked):
            self.checked = checked

    class Device:
        def toggleAdvanced(self, advanced=False):
            self.super_toggle_advanced_called = advanced

    class Plugin:
        pass

    def parameterDict(**kwargs):
        parameter = {}
        if "value" in kwargs:
            parameter[Parameter.VALUE] = kwargs["value"]
        if "advanced" in kwargs:
            parameter[Parameter.ADVANCED] = kwargs["advanced"]
        if "header" in kwargs:
            parameter[Parameter.HEADER] = kwargs["header"]
        if "toolTip" in kwargs:
            parameter[Parameter.TOOLTIP] = kwargs["toolTip"]
        if "indicator" in kwargs:
            parameter[Parameter.INDICATOR] = kwargs["indicator"]
        if "parameterType" in kwargs:
            parameter[Parameter.PARAMETER_TYPE] = kwargs["parameterType"]
        parameter.update(kwargs)
        return parameter

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
        or name in {"ampr_plugin_test", "ampr_a_plugin_test", "ampr_b_plugin_test"}
    ]:
        sys.modules.pop(name, None)


def _import_plugin_module_from_path(module_name: str, plugin_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_plugin_module():
    return _import_plugin_module_from_path("ampr_plugin_test", PLUGIN_PATH)


def test_plugin_import_is_lazy_and_does_not_load_runtime(monkeypatch):
    _clear_test_modules()
    _install_esibd_stubs()
    monkeypatch.syspath_prepend("/tmp/nonexistent-sentinel")

    runtime_dir_str = str(VENDOR_ROOT)
    assert runtime_dir_str not in sys.path

    module = _import_plugin_module()
    runtime_module_name = module._bundled_runtime_module_name()

    assert callable(module._get_ampr_driver_class)
    assert runtime_dir_str not in sys.path
    assert "cgc" not in sys.modules
    assert runtime_module_name not in sys.modules


def test_plugin_prefers_private_bundled_runtime(monkeypatch):
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()
    driver_class = module._get_ampr_driver_class()
    runtime_module_name = module._bundled_runtime_module_name()

    assert driver_class.__module__.startswith(f"{runtime_module_name}.ampr")
    assert str(VENDOR_ROOT) not in sys.path
    assert (
        sys.modules[runtime_module_name].__file__
        == str(VENDOR_ROOT / "__init__.py")
    )


def test_plugin_fails_explicitly_when_bundled_runtime_is_missing(monkeypatch):
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()
    monkeypatch.setattr(module, "_BUNDLED_RUNTIME_DIRNAME", "missing_runtime")
    module._AMPR_DRIVER_CLASS = None

    with pytest.raises(ModuleNotFoundError, match="vendor/runtime; plugin installation is incomplete"):
        module._get_ampr_driver_class()


def test_plugin_runtime_forces_inline_backend():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()
    module._get_ampr_driver_class()
    runtime_module_name = module._bundled_runtime_module_name()

    runtime_driver_common = sys.modules[f"{runtime_module_name}._driver_common"]

    assert runtime_driver_common.supports_process_backend() is False


def test_plugin_runtime_shutdown_attempts_all_outputs_before_raising():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()
    driver_class = module._get_ampr_driver_class()
    backend = object.__new__(driver_class._PROCESS_CONTROLLER_CLASS)
    backend.connected = True
    backend._transport_poisoned = False
    calls = []

    backend.scan_modules = lambda timeout_s=None: {
        2: {"product_no": 132401, "hw_type": 222308},
        3: {"product_id": "Dual Voltage Source 500V"},
    }

    def fake_set_module_voltage(module_id, channel_id, voltage, timeout_s=None):
        calls.append(("set_module_voltage", module_id, channel_id, voltage, timeout_s))
        if module_id == 2 and channel_id == 2:
            return 99
        return backend.NO_ERR

    def fake_enable_psu(enabled, timeout_s=None):
        calls.append(("enable_psu", enabled, timeout_s))
        return backend.NO_ERR, enabled

    backend.set_module_voltage = fake_set_module_voltage
    backend.enable_psu = fake_enable_psu
    backend.disconnect = lambda: calls.append(("disconnect",)) or True
    backend.format_status = lambda status: f"ERR{status}"

    with pytest.raises(RuntimeError, match=r"set_module_voltage\(2, 2, 0.0\): ERR99"):
        backend.shutdown(timeout_s=1.5)

    assert calls == [
        ("set_module_voltage", 2, 1, 0.0, 1.5),
        ("set_module_voltage", 2, 2, 0.0, 1.5),
        ("set_module_voltage", 2, 3, 0.0, 1.5),
        ("set_module_voltage", 2, 4, 0.0, 1.5),
        ("set_module_voltage", 3, 1, 0.0, 1.5),
        ("set_module_voltage", 3, 2, 0.0, 1.5),
        ("enable_psu", False, 1.5),
    ]


@pytest.mark.parametrize("plugin_path", [PLUGIN_A_PATH, PLUGIN_B_PATH])
def test_plugin_process_backend_calls_methods_with_rpc_timeout(monkeypatch, plugin_path):
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module_from_path(
        f"{plugin_path.parent.name}_plugin_test",
        plugin_path,
    )
    driver_class = module._get_ampr_driver_class()
    runtime_root = driver_class.__module__.rsplit(".", 2)[0]
    driver_common = importlib.import_module(f"{runtime_root}._driver_common")
    controller_process = importlib.import_module(f"{runtime_root}._controller_process")
    calls = []

    signature = inspect.signature(controller_process.ControllerProcessProxy.call_method)
    assert "rpc_timeout_s" in signature.parameters
    assert "timeout_s" not in signature.parameters

    class FakeProxy:
        def __init__(self, *args, **kwargs):
            return None

        def call_method(self, method_name, *args, rpc_timeout_s, **kwargs):
            calls.append((method_name, args, rpc_timeout_s, kwargs))
            return "ok"

        def close(self):
            return None

    class FakeController:
        def __init__(self, **kwargs):
            return None

        def connect(self, timeout_s=None):
            return True

    monkeypatch.setattr(driver_common, "supports_process_backend", lambda *_args: True)
    monkeypatch.setattr(driver_common, "ControllerProcessProxy", FakeProxy)
    monkeypatch.setattr(driver_class, "_PROCESS_CONTROLLER_CLASS", FakeController)

    ampr = driver_class("ampr_process", com=8)
    result = ampr.connect(timeout_s=2.0)

    assert result == "ok"
    assert calls == [("connect", (), 15.0, {"timeout_s": 2.0})]


def test_plugin_icon_is_a_valid_png_asset():
    assert ICON_PATH.exists()
    with ICON_PATH.open("rb") as handle:
        assert handle.read(8) == b"\x89PNG\r\n\x1a\n"


def test_plugin_enables_monitors_and_hides_optimize_column():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    assert module.AMPRDevice.useMonitors is True

    channel = object.__new__(module.AMPRChannel)
    module.AMPRChannel.setDisplayedParameters(channel)

    assert "Optimize" not in channel.displayedParameters
    assert channel.displayedParameters[-3:] == ["Module", "CH", "Display"]

    channel.channelParent = types.SimpleNamespace(ramp_rate_v_s=10.)
    channel_defaults = module.AMPRChannel.getDefaultChannel(channel)
    assert channel_defaults["Enabled"][module.Parameter.ADVANCED] is False
    assert channel_defaults["Enabled"][module.Parameter.HEADER] == "Status"
    tooltip = channel_defaults["Enabled"][module.Parameter.TOOLTIP]
    assert tooltip.startswith("Enable this AMPR output channel. Disabled channels are held at 0 V.")
    assert "reference floor 1 V" in tooltip
    assert "not confirmation of safe discharge" in tooltip
    assert channel_defaults["Active"][module.Parameter.HEADER] == "Manual"
    assert channel_defaults["Active"][module.Parameter.TOOLTIP] == (
        "If enabled, this channel uses its manual voltage setpoint. "
        "If disabled, ESIBD will drive it from the channel equation."
    )
    assert channel_defaults["Display"][module.Parameter.HEADER] == "Display"
    assert channel_defaults["Display"][module.Parameter.EVENT].__name__ == "displayChanged"
    assert channel_defaults["Scaling"][module.Parameter.VALUE] == "large"
    assert channel_defaults["Value"][module._PARAMETER_MIN_KEY] == -1000.0
    assert channel_defaults["Value"][module._PARAMETER_MAX_KEY] == 1000.0
    assert channel_defaults["Min"][module.Parameter.VALUE] == -1000.0
    assert channel_defaults["Min"][module.Parameter.ADVANCED] is False
    assert channel_defaults["Min"][module._PARAMETER_MIN_KEY] == -1000.0
    assert channel_defaults["Min"][module._PARAMETER_MAX_KEY] == 1000.0
    assert channel_defaults["Max"][module.Parameter.VALUE] == 1000.0
    assert channel_defaults["Max"][module.Parameter.ADVANCED] is False
    assert channel_defaults["Max"][module._PARAMETER_MIN_KEY] == -1000.0
    assert channel_defaults["Max"][module.Parameter.MAX] == 1000.0
    assert channel_defaults["Min"][module.Parameter.EVENT].__name__ == "minChanged"
    assert channel_defaults["Max"][module.Parameter.EVENT].__name__ == "maxChanged"
    assert channel_defaults["Module"][module.Parameter.ADVANCED] is False
    assert channel_defaults["CH"][module.Parameter.ADVANCED] is False
    assert channel_defaults["Module"][module.Parameter.INDICATOR] is True
    assert channel_defaults["CH"][module.Parameter.INDICATOR] is True
    assert (
        channel_defaults["Module"][module.Parameter.PARAMETER_TYPE]
        is module.PARAMETERTYPE.LABEL
    )
    assert (
        channel_defaults["CH"][module.Parameter.PARAMETER_TYPE]
        is module.PARAMETERTYPE.LABEL
    )


def test_plugin_hides_framework_collapse_column():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeTree:
        def __init__(self):
            self.calls = []

        def setColumnHidden(self, index, hidden):
            self.calls.append((index, hidden))

    class FakeChannel:
        def getSortedDefaultChannel(self):
            return {
                "Collapse": {},
                "Real": {},
                "Name": {},
                "Value": {},
                "Module": {},
                "CH": {},
            }

    device = object.__new__(module.AMPRDevice)
    device.tree = FakeTree()
    device.channels = [FakeChannel()]

    module.AMPRDevice._update_channel_column_visibility(device)

    assert device.tree.calls == [(0, True), (1, True)]


def test_missing_default_config_keeps_ampr_empty_until_first_initialization(tmp_path):
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeAction:
        def __init__(self):
            self.visible = None
            self.state = False

        def setVisible(self, visible):
            self.visible = visible

    class FakeWidget:
        def __init__(self):
            self.tooltips = []

        def setToolTip(self, tooltip):
            self.tooltips.append(tooltip)

    class FakeTree:
        def __init__(self):
            self.updates = []
            self.root_decorated = []
            self.headers = []
            self.hidden = []
            self.layouts = 0

        def setUpdatesEnabled(self, enabled):
            self.updates.append(enabled)

        def setRootIsDecorated(self, value):
            self.root_decorated.append(value)

        def setHeaderLabels(self, labels):
            self.headers.append(labels)

        def setColumnHidden(self, index, hidden):
            self.hidden.append((index, hidden))

        def scheduleDelayedItemsLayout(self):
            self.layouts += 1

    global_updates = []
    logs = []
    settings_widget = FakeWidget()
    device = object.__new__(module.AMPRDevice)
    device.name = "AMPR"
    device.MAXDATAPOINTS = "Max data points"
    device.confINI = "AMPR.ini"
    device.inout = "IN"
    device.channels = []
    device.loading = False
    device.tree = FakeTree()
    device.advancedAction = FakeAction()
    device.importAction = FakeAction()
    device.exportAction = FakeAction()
    device.duplicateChannelAction = FakeAction()
    device.deleteChannelAction = FakeAction()
    device.moveChannelUpAction = FakeAction()
    device.moveChannelDownAction = FakeAction()
    device.channelType = module.AMPRChannel
    device.pluginManager = types.SimpleNamespace(
        DeviceManager=types.SimpleNamespace(
            globalUpdate=lambda inout: global_updates.append(inout)
        ),
        Settings=types.SimpleNamespace(
            settings={
                "AMPR/Max data points": types.SimpleNamespace(
                    getWidget=lambda: settings_widget
                )
            }
        ),
    )
    device.customConfigFile = lambda _name: tmp_path / "AMPR.ini"
    device.print = lambda message, flag=None: logs.append((message, flag))

    module.AMPRDevice.loadConfiguration(device, useDefaultFile=True)

    assert device.channels == []
    assert logs == [
        (
            f"AMPR config file {tmp_path / 'AMPR.ini'} not found. "
            "Channels will be created after successful hardware initialization.",
            None,
        )
    ]
    assert device.tree.headers
    assert device.tree.updates == [False, True]
    assert global_updates == ["IN"]
    assert settings_widget.tooltips == []


def test_estimate_storage_handles_empty_pre_initialization_state():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeWidget:
        def __init__(self):
            self.tooltips = []

        def setToolTip(self, tooltip):
            self.tooltips.append(tooltip)

    widget = FakeWidget()
    device = object.__new__(module.AMPRDevice)
    device.channels = []
    device.name = "AMPR"
    device.MAXDATAPOINTS = "Max data points"
    device.pluginManager = types.SimpleNamespace(
        Settings=types.SimpleNamespace(
            settings={
                "AMPR/Max data points": types.SimpleNamespace(getWidget=lambda: widget)
            }
        )
    )

    module.AMPRDevice.estimateStorage(device)

    assert device.maxDataPoints == 100_000
    assert widget.tooltips == [
        "Storage estimate will be available after the first successful "
        "AMPR hardware initialization."
    ]


def test_toggle_advanced_keeps_ampr_channels_visible():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeChannel:
        def __init__(self):
            self.hidden = []

        def setHidden(self, hidden):
            self.hidden.append(hidden)

        def getSortedDefaultChannel(self):
            return {
                "Collapse": {},
                "Real": {},
                "Enabled": {},
                "Module": {},
                "CH": {},
            }

    class FakeTree:
        def __init__(self):
            self.hidden = []

        def setColumnHidden(self, index, hidden):
            self.hidden.append((index, hidden))

    device = object.__new__(module.AMPRDevice)
    device.channels = [FakeChannel(), FakeChannel()]
    device.tree = FakeTree()
    device.advancedAction = types.SimpleNamespace(state=False)
    device.getChannels = lambda: device.channels

    module.AMPRDevice.toggleAdvanced(device, advanced=False)

    assert device.super_toggle_advanced_called is False
    assert device.channels[0].hidden == [False]
    assert device.channels[1].hidden == [False]


def test_set_on_ui_state_uses_thread_safe_action_signals():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeSignal:
        def __init__(self):
            self.values = []

        def emit(self, value):
            self.values.append(value)

    class FakeAction:
        def __init__(self):
            self.state = None
            self.signalComm = types.SimpleNamespace(
                setValueFromThreadSignal=FakeSignal()
            )

    sync_calls = []
    status_calls = []
    device = object.__new__(module.AMPRDevice)
    device.useOnOffLogic = True
    device.onAction = FakeAction()
    device.deviceOnAction = FakeAction()
    device._sync_local_on_action = lambda: sync_calls.append(True)
    device._update_status_widgets = lambda: status_calls.append(True)

    module.AMPRDevice._set_on_ui_state(device, False)

    assert device.onAction.signalComm.setValueFromThreadSignal.values == [False]
    assert device.deviceOnAction.signalComm.setValueFromThreadSignal.values == [False]
    assert sync_calls == [True]
    assert status_calls == [True]


def test_channel_events_are_logged_for_user_changes():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    logs = []
    channel = object.__new__(module.AMPRChannel)
    channel.channelParent = types.SimpleNamespace(
        loading=False,
        print=lambda message, flag=None: logs.append((message, flag)),
    )
    channel.name = "AMPR_M02_CH3"
    channel.module = "2"
    channel.id = "3"
    channel.value = 123.4
    channel.enabled = True
    channel.active = False
    channel.display = True
    channel.min = -25.0
    channel.max = 250.0
    channel.equation = "2*x"

    module.AMPRChannel.enabledChanged(channel)
    module.AMPRChannel.valueChanged(channel)
    module.AMPRChannel.activeChanged(channel)
    module.AMPRChannel.displayChanged(channel)
    module.AMPRChannel.minChanged(channel)
    module.AMPRChannel.maxChanged(channel)
    module.AMPRChannel.equationChanged(channel)

    assert logs == [
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Output switched ON.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Voltage setpoint changed to 123.400 V.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Control mode changed to equation.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Display switched ON.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Minimum changed to -25.000 V.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Maximum changed to 250.000 V.",
            None,
        ),
        (
            "AMPR channel AMPR_M02_CH3 (module 2 CH3): Equation changed to '2*x'.",
            None,
        ),
    ]


def test_channel_off_immediately_clears_monitor():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    channel = object.__new__(module.AMPRChannel)
    channel.channelParent = types.SimpleNamespace(
        loading=False,
        print=lambda message, flag=None: None,
    )
    channel.name = "AMPR_M02_CH3"
    channel.module = "2"
    channel.id = "3"
    channel.enabled = False
    channel.monitor = 42.0

    module.AMPRChannel.enabledChanged(channel)

    assert np.isnan(channel.monitor)


def test_channel_enabled_change_forces_apply_even_without_setpoint_change():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    applies = []
    channel = object.__new__(module.AMPRChannel)
    channel.channelParent = types.SimpleNamespace(
        loading=False,
        print=lambda message, flag=None: None,
    )
    channel.name = "AMPR_M02_CH3"
    channel.module = "2"
    channel.id = "3"
    channel.enabled = True
    channel.monitor = 0.0
    channel.applyValue = lambda apply=False: applies.append(apply)

    module.AMPRChannel.enabledChanged(channel)

    assert applies == [True]


def test_channel_keeps_display_checkbox_and_explicit_toggle_buttons():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeParameter:
        def __init__(self, value):
            self.value = value
            self.widget = None
            self.check = None
            self.rowHeight = 18
            self.heights = []

        def applyWidget(self):
            self.check = self.widget

        def setHeight(self, height):
            self.rowHeight = height
            self.heights.append(height)

    class FakeTree:
        def __init__(self):
            self.layouts = 0

        def scheduleDelayedItemsLayout(self):
            self.layouts += 1

    original_display_widget = object()
    channel = object.__new__(module.AMPRChannel)
    parameters = {
        "Enabled": FakeParameter(True),
        "Active": FakeParameter(False),
        "Display": FakeParameter(True),
    }
    parameters["Display"].widget = original_display_widget
    parameters["Display"].check = original_display_widget
    channel.parameters = list(parameters.values())
    channel.rowHeight = 18
    channel.loading = False
    channel.tree = FakeTree()
    channel.useDisplays = True
    channel.getParameterByName = parameters.get

    module.AMPRChannel.initGUI(channel, {"Name": "dummy"})

    assert channel.super_init_gui_called == {"Name": "dummy"}
    assert channel.rowHeight == 28
    assert parameters["Enabled"].check.text == "HV ON"
    assert parameters["Enabled"].check.minimum_width == 58
    assert parameters["Enabled"].check.maximum_height == 28
    assert parameters["Enabled"].check.checkable is True
    assert parameters["Active"].check.text == "Manual"
    assert parameters["Display"].check is original_display_widget
    assert channel.tree.layouts == 1


def test_channel_enabled_toggle_text_becomes_explicitly_off():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeWidget:
        def __init__(self):
            self.text = None

        def setText(self, text):
            self.text = text

    class FakeParameter:
        def __init__(self, widget):
            self.check = widget

    enabled_widget = FakeWidget()
    channel = object.__new__(module.AMPRChannel)
    channel.channelParent = types.SimpleNamespace(
        loading=False,
        print=lambda message, flag=None: None,
    )
    channel.name = "AMPR_M02_CH3"
    channel.module = "2"
    channel.id = "3"
    channel.enabled = False
    channel.monitor = 42.0
    channel.getParameterByName = lambda name: {"Enabled": FakeParameter(enabled_widget)}.get(name)

    module.AMPRChannel.enabledChanged(channel)

    assert enabled_widget.text == "HV OFF"
    assert np.isnan(channel.monitor)


def test_channel_monitor_feedback_uses_relative_color_bands():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeWidget:
        def __init__(self):
            self.styles = []

        def setStyleSheet(self, style):
            self.styles.append(style)

    class FakeParameter:
        def __init__(self, widget):
            self.widget = widget

        def getWidget(self):
            return self.widget

    monitor_widget = FakeWidget()
    status_widget = FakeWidget()
    channel = object.__new__(module.AMPRChannel)
    channel.channelParent = types.SimpleNamespace(
        controller=types.SimpleNamespace(acquiring=True, initialized=True, main_state="ST_ON"),
        onAction=types.SimpleNamespace(state=True),
        isOn=lambda: True,
    )
    channel.enabled = True
    channel.real = True
    channel.waitToStabilize = False
    channel.value = 100.0
    channel.monitor = 100.5
    channel.warningState = False
    channel.getParameterByName = lambda name: {
        "Monitor": FakeParameter(monitor_widget), "Enabled": FakeParameter(status_widget)
    }.get(name)

    module.AMPRChannel.monitorChanged(channel)
    assert "#2f855a" in status_widget.styles[-1]

    channel.monitor = 108.0
    module.AMPRChannel.monitorChanged(channel)
    assert "#dd6b20" in status_widget.styles[-1]

    channel.monitor = 125.0
    module.AMPRChannel.monitorChanged(channel)
    assert "#c53030" in status_widget.styles[-1]

    channel.monitor = np.nan
    module.AMPRChannel.monitorChanged(channel)
    assert status_widget.styles[-1] == ""
    assert set(monitor_widget.styles) == {""}


def test_init_gui_rewires_close_action_to_shutdown():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeSignal:
        def __init__(self):
            self.connected = []
            self.disconnected = False

        def connect(self, callback):
            self.connected.append(callback)

        def disconnect(self):
            self.disconnected = True

    class FakeAction:
        def __init__(self):
            self.triggered = FakeSignal()
            self.tooltips = []
            self.texts = []
            self.visible = None

        def setToolTip(self, tooltip):
            self.tooltips.append(tooltip)

        def setText(self, text):
            self.texts.append(text)

        def setVisible(self, visible):
            self.visible = visible

    device = object.__new__(module.AMPRDevice)
    device.initAction = FakeAction()
    device.closeCommunicationAction = FakeAction()
    controller_args = []
    original_init_gui = getattr(module.Device, "initGUI", None)
    module.Device.initGUI = lambda self: None
    original_controller = module.AMPRController
    module.AMPRController = lambda controllerParent: controller_args.append(controllerParent) or "controller"
    try:
        module.AMPRDevice.initGUI(device)
    finally:
        if original_init_gui is None:
            delattr(module.Device, "initGUI")
        else:
            module.Device.initGUI = original_init_gui
        module.AMPRController = original_controller

    assert device.initAction.visible is False
    assert device.closeCommunicationAction.triggered.disconnected is True
    assert device.closeCommunicationAction.triggered.connected == [device.shutdownCommunication]
    assert device.closeCommunicationAction.tooltips == ["Shutdown AMPR_A and disconnect."]
    assert device.closeCommunicationAction.texts == ["Shutdown AMPR_A and disconnect."]
    assert device.closeCommunicationAction.visible is False
    assert controller_args == [device]
    assert device.controller == "controller"


def test_finalize_init_adds_local_on_action_and_set_on_keeps_it_synced():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeStateAction:
        def __init__(self):
            self._state = None
            self.blocked = []

        @property
        def state(self):
            return self._state

        @state.setter
        def state(self, value):
            self._state = value

        def blockSignals(self, blocked):
            self.blocked.append(blocked)

    added = []
    device = object.__new__(module.AMPRDevice)
    device.useOnOffLogic = True
    device.closeCommunicationAction = object()
    device._update_channel_column_visibility = lambda: None
    device.getIcon = lambda desaturate=False: f"icon-{desaturate}"
    device.makeCoreIcon = lambda name: f"core:{name}"
    device.makeIcon = lambda name, path=None, desaturate=False: f"local:{name}"
    device.onAction = types.SimpleNamespace(state=True)
    device.isOn = lambda: device.onAction.state
    device.addStateAction = lambda **kwargs: added.append(kwargs) or FakeStateAction()
    set_on_calls = []
    device.controller = types.SimpleNamespace(
        toggleOnFromThread=lambda parallel=True: set_on_calls.append(parallel)
    )
    device.channels = []
    device.loading = False

    original_finalize_init = getattr(module.Device, "finalizeInit", None)
    module.Device.finalizeInit = lambda self: None
    try:
        module.AMPRDevice.finalizeInit(device)
        device.initialized = True
        module.AMPRDevice.setOn(device, on=False)
    finally:
        if original_finalize_init is None:
            delattr(module.Device, "finalizeInit")
        else:
            module.Device.finalizeInit = original_finalize_init

    assert len(added) == 1
    assert added[0]["toolTipFalse"] == "Turn AMPR_A ON."
    assert added[0]["toolTipTrue"] == "Turn AMPR_A OFF and disconnect."
    assert added[0]["iconFalse"] == "local:switch-medium_on.png"
    assert added[0]["iconTrue"] == "local:switch-medium_off.png"
    assert added[0]["before"] is device.closeCommunicationAction
    assert added[0]["restore"] is False
    assert added[0]["defaultState"] is False
    assert isinstance(device.deviceOnAction, FakeStateAction)
    assert device.deviceOnAction.state is False
    assert device.deviceOnAction.blocked == [], "StateAction needs toggled for its icon and tooltip"
    assert set_on_calls == [True]


def test_set_on_ignores_reentrant_request_while_transition_is_running():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    device = object.__new__(module.AMPRDevice)
    device.name = "AMPR"
    device.onAction = types.SimpleNamespace(state=True)
    device.isOn = lambda: device.onAction.state
    device.loading = False
    device.printed = []
    device.print = lambda message, flag=None: device.printed.append((message, flag))
    device.controller = types.SimpleNamespace(
        initializing=False,
        transitioning=True,
        transition_target_on=True,
    )

    module.AMPRDevice.setOn(device, on=True)

    assert device.onAction.state is True
    assert device.printed == [
        (
            "AMPR ON/OFF transition already in progress; ignoring additional request.",
            module.PRINT.WARNING,
        )
    ]


def test_close_communication_uses_controller_initialized_for_shutdown():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    shutdown_calls = []
    close_calls = []
    device = object.__new__(module.AMPRDevice)
    device.useOnOffLogic = True
    device.onAction = types.SimpleNamespace(state=True)
    device.initialized = False
    device.shutdownCommunication = lambda: shutdown_calls.append(True)
    device.controller = types.SimpleNamespace(
        initialized=True,
        _forced_close_state=None,
        closeCommunication=lambda **_kwargs: close_calls.append(True),
    )

    module.AMPRDevice.closeCommunication(device)

    assert shutdown_calls == [True]
    assert close_calls == []


def test_shutdown_communication_keeps_ui_on_when_shutdown_is_unconfirmed():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    device = object.__new__(module.AMPRDevice)
    device.useOnOffLogic = True
    device.onAction = types.SimpleNamespace(state=False)
    device.recording = True
    device.controller = types.SimpleNamespace(shutdownCommunication=lambda: False)
    sync_states = []
    warnings = []
    device.stopAcquisition = lambda: None
    device._sync_local_on_action = lambda: sync_states.append(device.onAction.state)
    device._sync_acquisition_controls = lambda: None
    device.print = lambda message, flag=None: warnings.append((message, flag))

    module.AMPRDevice.shutdownCommunication(device)

    assert device.onAction.state is True
    assert sync_states == [True]
    assert any("shutdown could not be confirmed" in message for message, _ in warnings)
    assert device.recording is False


def test_status_widgets_summarize_global_ampr_state():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeLabel:
        def __init__(self, text=""):
            self.text = text
            self.tooltips = []
            self.styles = []
            self.object_names = []

        def setObjectName(self, name):
            self.object_names.append(name)

        def setText(self, text):
            self.text = text

        def setToolTip(self, tooltip):
            self.tooltips.append(tooltip)

        def setStyleSheet(self, style):
            self.styles.append(style)

    class FakeTitleBar:
        def __init__(self):
            self.inserted = []

        def insertWidget(self, before, widget):
            self.inserted.append((before, widget))

    device = object.__new__(module.AMPRDevice)
    device.name = "AMPR"
    device.titleBar = FakeTitleBar()
    device.titleBarLabel = FakeLabel()
    device.stretchAction = object()
    device.main_state = "ST_STBY"
    device.detected_modules = "2"
    device.interlock_state_summary = "OK"
    device.device_state_summary = "DS_HV_FAIL"
    device.voltage_state_summary = "VS_LINE_ON, VS_HVP_OK"

    module.AMPRDevice._ensure_status_widgets(device)

    assert len(device.titleBar.inserted) == 2
    assert device.statusBadgeLabel.text == "ST_STBY"
    assert (
        device.statusSummaryLabel.text
        == "Modules: 2 | Interlock: OK | Faults: DS_HV_FAIL"
    )
    tooltip = device.statusBadgeLabel.tooltips[-1]
    assert "State: ST_STBY" in tooltip
    assert "Modules: 2" in tooltip
    assert "Interlock: OK" in tooltip
    assert "Faults: DS_HV_FAIL" in tooltip
    assert "Voltage rails: VS_LINE_ON, VS_HVP_OK" in tooltip
    assert "#b7791f" in device.statusBadgeLabel.styles[-1]


def test_finalize_init_relabels_advanced_action_for_ampr():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeAdvancedAction:
        def __init__(self):
            self.toolTipFalse = ""
            self.toolTipTrue = ""
            self.tooltips = []

        def setToolTip(self, tooltip):
            self.tooltips.append(tooltip)

    device = object.__new__(module.AMPRDevice)
    device.name = "AMPR"
    device.advancedAction = FakeAdvancedAction()
    device._ensure_local_on_action = lambda: None
    device._ensure_status_widgets = lambda: None
    device._update_channel_column_visibility = lambda: None

    original_finalize_init = getattr(module.Device, "finalizeInit", None)
    module.Device.finalizeInit = lambda self: None
    try:
        module.AMPRDevice.finalizeInit(device)
    finally:
        if original_finalize_init is None:
            delattr(module.Device, "finalizeInit")
        else:
            module.Device.finalizeInit = original_finalize_init

    assert device.advancedAction.toolTipFalse == (
        "Show expert columns and channel layout actions for AMPR."
    )
    assert device.advancedAction.toolTipTrue == (
        "Hide expert columns and channel layout actions for AMPR."
    )
    assert device.advancedAction.tooltips == [
        "Show expert columns and channel layout actions for AMPR."
    ]


def test_ampr_a_and_ampr_b_load_as_distinct_autonomous_plugins(monkeypatch):
    _clear_test_modules()
    _install_esibd_stubs()
    monkeypatch.syspath_prepend("/tmp/nonexistent-sentinel")

    module_a = _import_plugin_module_from_path("ampr_a_plugin_test", PLUGIN_A_PATH)
    module_b = _import_plugin_module_from_path("ampr_b_plugin_test", PLUGIN_B_PATH)
    runtime_module_name_a = module_a._bundled_runtime_module_name()
    runtime_module_name_b = module_b._bundled_runtime_module_name()

    assert module_a.AMPRDevice.name == "AMPR_A"
    assert module_b.AMPRDevice.name == "AMPR_B"
    assert module_a.AMPRDevice.supportedVersion == "1.0.1"
    assert module_b.AMPRDevice.supportedVersion == "1.0.1"
    assert module_a._BUNDLED_RUNTIME_DIRNAME == "runtime"
    assert module_b._BUNDLED_RUNTIME_DIRNAME == "runtime"
    assert VENDOR_A_ROOT.name == "runtime"
    assert VENDOR_B_ROOT.name == "runtime"
    assert runtime_module_name_a != runtime_module_name_b

    driver_class_a = module_a._get_ampr_driver_class()
    driver_class_b = module_b._get_ampr_driver_class()

    assert driver_class_a.__module__.startswith(f"{runtime_module_name_a}.ampr")
    assert driver_class_b.__module__.startswith(f"{runtime_module_name_b}.ampr")
    assert str(VENDOR_A_ROOT) not in sys.path
    assert str(VENDOR_B_ROOT) not in sys.path
    assert (
        sys.modules[runtime_module_name_a].__file__
        == str(VENDOR_A_ROOT / "__init__.py")
    )
    assert (
        sys.modules[runtime_module_name_b].__file__
        == str(VENDOR_B_ROOT / "__init__.py")
    )


def test_ampr_recording_action_reflects_device_readiness():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()

    class FakeAction:
        def __init__(self):
            self.state = False
            self.enabled = None
            self.blocks = []

        def setEnabled(self, enabled):
            self.enabled = enabled

        def blockSignals(self, blocked):
            self.blocks.append(blocked)

    class FakeDisplayAction(FakeAction):
        def __init__(self, tooltip):
            super().__init__()
            self._tooltip = tooltip

        def toolTip(self):
            return self._tooltip

    class FakeTitleBar:
        def __init__(self, actions):
            self._actions = actions

        def actions(self):
            return list(self._actions)

    device = object.__new__(module.AMPRDevice)
    device.recording = False
    device.recordingAction = FakeAction()
    display_close = FakeDisplayAction("Close AMPR_A communication.")
    display_init = FakeDisplayAction("Initialize AMPR_A communication.")
    device.liveDisplay = types.SimpleNamespace(
        recordingAction=FakeAction(),
        titleBar=FakeTitleBar([display_close, display_init]),
    )
    device.controller = types.SimpleNamespace(
        device=object(),
        initializing=False,
        initialized=False,
        transitioning=False,
        main_state="ST_STBY",
    )
    device.isOn = lambda: True

    module.AMPRDevice._sync_acquisition_controls(device)
    assert device.recordingAction.enabled is False
    assert device.liveDisplay.recordingAction.enabled is False
    assert display_close.enabled is True
    assert display_init.enabled is False

    device.controller.initialized = True
    device.controller.main_state = "ST_ON"
    module.AMPRDevice._sync_acquisition_controls(device)
    assert device.recordingAction.enabled is True
    assert device.liveDisplay.recordingAction.enabled is True
    assert display_close.enabled is True
    assert display_init.enabled is False


@pytest.mark.parametrize(
    ("module_name", "plugin_path"),
    [
        ("ampr_a_plugin_test", PLUGIN_A_PATH),
        ("ampr_b_plugin_test", PLUGIN_B_PATH),
    ],
)
def test_ampr_partial_startup_exposes_toolbar_disconnect_action(module_name, plugin_path):
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module_from_path(module_name, plugin_path)

    class FakeAction:
        def __init__(self):
            self.state = False
            self.enabled = None
            self.visible = None
            self.blocks = []

        def setEnabled(self, enabled):
            self.enabled = enabled

        def setVisible(self, visible):
            self.visible = visible

        def blockSignals(self, blocked):
            self.blocks.append(blocked)

    device = object.__new__(module.AMPRDevice)
    device.name = module.AMPRDevice.name
    device.recording = False
    device.recordingAction = FakeAction()
    device.closeCommunicationAction = FakeAction()
    device.onAction = types.SimpleNamespace(state=False)
    device.liveDisplay = types.SimpleNamespace(recordingAction=FakeAction())
    device.controller = types.SimpleNamespace(
        device=object(),
        initializing=False,
        initialized=False,
        transitioning=False,
        main_state="Connected",
    )
    device.isOn = lambda: False

    module.AMPRDevice._sync_acquisition_controls(device)
    assert device.closeCommunicationAction.enabled is True
    assert device.closeCommunicationAction.visible is True
    assert device.recordingAction.enabled is False
    assert device.liveDisplay.recordingAction.enabled is False

    device.controller.device = None
    device.controller.initialized = False
    module.AMPRDevice._sync_acquisition_controls(device)
    assert device.closeCommunicationAction.enabled is False
    assert device.closeCommunicationAction.visible is False


def test_ampr_toggle_recording_rejects_unready_device():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module()
    super_calls = []

    def fake_super_toggle(self, on=None, manual=True):
        super_calls.append((on, manual))

    module.Device.toggleRecording = fake_super_toggle

    class FakeAction:
        def __init__(self):
            self.state = True
            self.enabled = None
            self.blocks = []

        def setEnabled(self, enabled):
            self.enabled = enabled

        def blockSignals(self, blocked):
            self.blocks.append(blocked)

    device = object.__new__(module.AMPRDevice)
    device.name = "AMPR"
    device.recording = False
    device.recordingAction = FakeAction()
    device.controller = types.SimpleNamespace(
        device=object(),
        initializing=False,
        initialized=True,
        transitioning=False,
        main_state="ST_STBY",
    )
    device.isOn = lambda: True
    device.printed = []
    device.print = lambda message, flag=None: device.printed.append((message, flag))

    module.AMPRDevice.toggleRecording(device, on=True, manual=True)

    assert super_calls == []
    assert device.recordingAction.state is False
    assert device.recordingAction.enabled is False
    assert device.printed == [
        ("Cannot start AMPR data acquisition: state is ST_STBY.", module.PRINT.WARNING)
    ]
