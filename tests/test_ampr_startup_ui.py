"""AMPR must load saved channels before Explorer creates its ON/OFF action.

Run Explorer's actual Channel/Parameter widgets and configuration loader in a
subprocess. Only unrelated application services are stubbed, not isOn(), channel
construction, saved parameter restoration, colour updates, or Qt actions.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
@pytest.mark.parametrize("legacy", [False, True])
def test_saved_channels_before_on_action(folder, legacy, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, str(int(legacy)), str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt or Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
@pytest.mark.parametrize("state,active", [
    ("ready", True), ("unconfirmed", True), ("read_error", True),
    ("ramp_up", True), ("ramp_down", True),
    ("no_controller", False), ("no_action", False), ("action_none", False),
    ("no_initialized", False), ("not_initialized", False),
    ("no_state", False), ("empty_state", False), ("state_none", False),
    ("standby", False), ("disconnected", False), ("device_off", False),
    ("channel_off", False), ("virtual", False),
])
def test_colour_state_requires_initialized_controls(folder, state, active):
    import importlib.util
    from types import MethodType, SimpleNamespace
    from test_ampr_plugin_packaging import _clear_test_modules, _install_esibd_stubs

    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("ampr_colour_state", ROOT / folder / "ampr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    controller = SimpleNamespace(initialized=True, acquiring=True, main_state="ST_ON")
    parent = SimpleNamespace(controller=controller, onAction=SimpleNamespace(state=True))
    parent.isOn = MethodType(lambda self: self.onAction.state, parent)
    channel = module.AMPRChannel(parent)
    channel.enabled = channel.real = True
    channel.value = channel.monitor = 250.
    if state in {"ramp_up", "ramp_down"}:
        controller.ramping = controller.transitioning = True
        controller.acquiring = False
        controller.transition_target_on = parent.onAction.state = state == "ramp_up"
    elif state == "no_controller":
        del parent.controller
    elif state == "no_action":
        del parent.onAction
    elif state == "action_none":
        parent.onAction = None
    elif state == "no_initialized":
        del controller.initialized
    elif state == "not_initialized":
        controller.initialized = False
    elif state == "no_state":
        del controller.main_state
    elif state in {"standby", "disconnected", "empty_state", "state_none", "unconfirmed", "read_error"}:
        controller.main_state = {"standby": "ST_STBY", "disconnected": "Disconnected", "empty_state": "",
                                 "state_none": None, "unconfirmed": "Shutdown unconfirmed", "read_error": "State error"}[state]
    elif state == "device_off":
        parent.onAction.state = False
    elif state == "channel_off":
        channel.enabled = False
    elif state == "virtual":
        channel.real = False
    assert channel._output_enabled() is active
    if not active:
        assert channel._monitor_feedback_state() == "default"


def probe(folder, legacy, output, *, exercise=None):
    import configparser
    from contextlib import nullcontext
    from functools import wraps
    from enum import Enum
    import importlib.util
    from importlib.metadata import PackageNotFoundError, distribution
    import re
    import threading
    from types import SimpleNamespace

    import numpy as np

    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        import pyqtgraph as pg
        host = Path(distribution("esibd-explorer").locate_file("esibd"))
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77
    if not (host / "core.py").is_file():
        return 77

    app = QtWidgets.QApplication([])
    ns = dict(np=np, pg=pg, re=re, Path=Path, Enum=Enum, configparser=configparser,
              cast=lambda _, obj: obj, Thread=threading.Thread,
              pyqtSignal=QtCore.pyqtSignal, pyqtSlot=QtCore.pyqtSlot,
              getTestMode=lambda: False, getDarkMode=lambda: False,
              colors=SimpleNamespace(fg="#202020", bg="#ffffff"),
              ScanChannel=type("ScanChannel", (), {}), Setting=type("Setting", (), {}),
              FILE_INI=".ini", INFO="Info", VERSION="Version",
              wraps=wraps, getLogLevel=lambda: 0)
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    sources = {filename: ast.parse((host / filename).read_text())
               for filename in ("const.py", "core.py", "plugins.py")}

    def extract(filename, name, *, methods=None):
        node = next(n for n in sources[filename].body
                    if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        if methods is not None:
            node = ast.ClassDef(name=name, bases=[ast.Name(id="Plugin", ctx=ast.Load())],
                                keywords=[], decorator_list=[],
                                body=[n for n in node.body if isinstance(n, ast.FunctionDef) and n.name in methods])
        code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node],
                          type_ignores=[])
        exec(compile(ast.fix_missing_locations(code), str(host / filename), "exec"), ns)
        return ns[name]

    from test_ampr_plugin_packaging import _install_esibd_stubs
    _install_esibd_stubs()
    core, plugins = sys.modules["esibd.core"], sys.modules["esibd.plugins"]
    for name in ("INOUT", "PRINT", "PLUGINTYPE", "PARAMETERTYPE", "makeWrapper", "synchronized"):
        extract("const.py", name)
    for name in ("ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox",
                 "Label", "PushButton", "ColorButton", "CheckBox", "ToolButton", "LedIndicator",
                 "CompactComboBox", "LineEdit", "TreeWidget", "Parameter", "parameterDict", "DynamicNp", "Channel"):
        extract("core.py", name)
    for name in ("Channel", "Parameter", "ToolButton", "parameterDict", "INOUT", "PLUGINTYPE", "PARAMETERTYPE", "PRINT"):
        setattr(core, name, ns[name])

    class Plugin(QtCore.QObject):
        def initGUI(self):
            pass
        def finalizeInit(self):
            pass
        def toggleAdvanced(self, advanced=False):
            pass

    ns["Plugin"] = Plugin
    manager_type = extract("plugins.py", "ChannelManager", methods={
        "initGUI", "finalizeInit", "isOn", "loadConfiguration", "updateChannelConfig",
        "addChannel", "getChannels", "getChannelByName", "intervalChanged", "toggleAdvanced",
    })
    plugins.Device = ns["Device"] = manager_type
    core.DeviceController.__init__ = lambda self, controllerParent: setattr(self, "controllerParent", controllerParent)
    spec = importlib.util.spec_from_file_location("ampr_startup_probe", ROOT / folder / "ampr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config["Info"] = {"name": folder}
    for index, enabled in enumerate((True, False)):
        config[f"Channel_{index:03}"] = {
            "Name": "Inlet_Capillary" if enabled else "Disabled_output",
            "Enabled": str(enabled), "Real": "True", "Active": "True", "Value": "250",
            "Module": "0", "CH": str(index + 1), "Color": "#63b3ed",
        }
        if not legacy:
            config[f"Channel_{index:03}"]["Ramp rate"] = "7.5"
    path = output / f"{folder}.ini"
    with path.open("w") as stream:
        config.write(stream)
    original_config = path.read_bytes()
    logs, commands = [], []
    window = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(window)
    toolbar = QtWidgets.QToolBar(window)
    layout.addWidget(toolbar)
    parent = module.AMPRDevice.__new__(module.AMPRDevice)
    QtCore.QObject.__init__(parent)
    parent.name = folder.upper()
    parent.lock = SimpleNamespace(acquire_timeout=lambda **kwargs: nullcontext(True))
    parent.loading = True
    parent.channels, parent.channelsChanged = [], False
    parent.channelType = module.AMPRChannel
    parent.inout = ns["INOUT"].IN
    parent.useMonitors = parent.useDisplays = parent.useOnOffLogic = True
    parent.useBackgrounds = parent.logY = parent.recording = False
    parent.liveDisplay = None
    parent.convertDataDisplay = lambda value: value
    parent.maxDataPoints = 100_000
    parent.ramp_rate_v_s = 10.
    parent.module_channel_counts, parent.module_voltage_limits = {}, {}
    parent.confINI = path.name
    parent.customConfigFile = lambda _: path
    parent.print = lambda *a, **kw: logs.append(a)
    parent.processEvents = app.processEvents
    parent.makeCoreIcon = lambda *_: QtGui.QIcon()
    parent.getIcon = lambda **_: QtGui.QIcon()
    parent.aboutAction = parent.imageClipboardIcon = None
    parent.addContentWidget = layout.addWidget
    parent.interval = 100
    parent.titleBar = toolbar

    def state_action(**kwargs):
        kwargs = {key: val for key, val in kwargs.items() if key not in {"before", "attr", "defaultState"}}
        kwargs.setdefault("iconFalse", QtGui.QIcon())
        action = StateAction(parentPlugin=parent, restore=False, **kwargs)
        toolbar.addAction(action)
        return action

    def add_action(**kwargs):
        action = QtGui.QAction(kwargs.get("toolTip", ""), toolbar)
        toolbar.addAction(action)
        if kwargs.get("attr"):
            setattr(parent, kwargs["attr"], action)
        return action

    parent.addStateAction = state_action
    parent.addAction = add_action
    parent.advancedAction = state_action(toolTipFalse="Show advanced", toolTipTrue="Hide advanced")
    parent.pluginManager = SimpleNamespace(
        loading=True, ChannelManager=manager_type, Device=manager_type,
        DeviceManager=SimpleNamespace(addStateAction=state_action, aboutAction=None,
                                      globalUpdate=lambda **kw: None, updateStaticPlot=lambda: None),
        reconnectSource=lambda *_: None,
    )
    for name in ("saveConfiguration", "duplicateChannel", "deleteChannel", "moveChannel", "showChannelPlot",
                 "toggleLiveDisplay", "copyClipboard", "channelSelection", "updateDisplay"):
        setattr(parent, name, lambda *a, **kw: None)
    parent._sync_acquisition_controls = lambda: None
    parent._update_status_widgets = lambda: None
    # Any accidental hardware creation must fail, not silently connect on this host.
    module._get_ampr_driver_class = lambda: commands.append("hardware") or pytest.fail("GUI loading must not access hardware")

    # Real loader -> addChannel -> Channel.__init__/initGUI -> AMPR colour logic.
    # The controller is created only AFTER the saved channels; the action even later.
    assert not hasattr(parent, "onAction") and not hasattr(parent, "controller")
    parent.initGUI()
    assert not hasattr(parent, "onAction")
    assert parent.controller.initialized is False
    assert len(parent.channels) == 2
    channel, disabled = parent.channels
    assert channel.name == "Inlet_Capillary" and channel.enabled and channel.real
    assert channel.value == 250.
    assert channel.ramp_rate_v_s == (10. if legacy else 7.5)
    assert not disabled.enabled
    columns = channel.displayedParameters
    assert parent.tree.headerItem().text(columns.index(channel.ENABLED)) == "Status"
    index = columns.index(channel.MONITOR)
    assert columns[index:index + 3] == [channel.MONITOR, channel.RAMP_RATE, channel.MIN]

    def neutral(ch):
        assert ch.background(0).style() == QtCore.Qt.BrushStyle.NoBrush
        assert ch.getParameterByName(ch.VALUE).spin.styleSheet() == ""
        assert ch.getParameterByName(ch.MONITOR).getWidget().styleSheet() == ""
        assert ch.getParameterByName(ch.ENABLED).getWidget().styleSheet() == ""
        assert ch.color == "#63b3ed", "Plot colour must remain saved"

    for ch in parent.channels:
        neutral(ch)
    # Execute Explorer's finalizeInit, including creation of the real StateAction.
    manager_type.finalizeInit(parent)
    assert parent.onAction.state is False
    parent.loading = False
    controller = parent.controller
    controller.acquiring = False
    controller._sync_status_to_gui()
    for ch in parent.channels:
        neutral(ch)

    window.resize(1100, 200)
    window.show()
    app.processEvents()
    window.grab().save(str(output / f"{folder}-startup.png"))
    parent.onAction.state = True
    controller._sync_status_to_gui()
    neutral(channel)  # Requesting ON without an initialized controller is not active.
    controller.initialized, controller.acquiring = True, True
    controller.main_state = "ST_ON"
    channel.monitor = 250.
    controller._sync_status_to_gui()
    assert channel.background(0).color().name() == "#63b3ed"
    assert "#2f855a" in channel.getParameterByName(channel.ENABLED).getWidget().styleSheet()
    assert channel.getParameterByName(channel.MONITOR).getWidget().styleSheet() == ""
    neutral(disabled)
    app.processEvents()
    window.grab().save(str(output / f"{folder}-on.png"))

    controller.acquiring = False
    controller.main_state = "Shutdown unconfirmed"
    channel._setpoint_feedback("error", 250., "Shutdown unconfirmed; retry OFF")
    controller._sync_status_to_gui()
    assert parent.main_state == "Shutdown unconfirmed"
    assert parent.onAction.state is True
    assert channel.background(0).style() != QtCore.Qt.BrushStyle.NoBrush
    assert "#c53030" in channel.getParameterByName(channel.VALUE).spin.styleSheet()
    assert "#c53030" in channel.getParameterByName(channel.ENABLED).getWidget().styleSheet()
    neutral(disabled)
    app.processEvents()
    window.grab().save(str(output / f"{folder}-unconfirmed.png"))

    parent.onAction.state = False
    controller.main_state, controller.initialized = "Disconnected", False
    controller._sync_status_to_gui()
    for ch in parent.channels:
        neutral(ch)
    assert path.read_bytes() == original_config
    assert not commands
    if exercise is not None:
        exercise(module, parent, app, window, output)
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], bool(int(sys.argv[2])), sys.argv[3]))
