"""Setpoint typing and focus-out with real Qt and Explorer Parameter widgets.

No device connection is needed: command entry points capture the committed
requests, while panel construction, refresh and signal wiring are production code.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = [(folder, surface) for folder in ("amx_a", "amx_b", "amx_hd")
         for surface in ("table", "frequency", "frequency_setting")]
CASES += [(folder, "width") for folder in ("amx_a", "amx_b")]
CASES += [("esi", surface) for surface in ("table", "heater", "target")]
CASES += [(f"psu_{suffix}", surface) for suffix in "abcde" for surface in ("voltage", "current_limit")]


@pytest.mark.parametrize("folder,surface", CASES)
def test_setpoint_entry_waits_for_validation(folder, surface, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, surface, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt / Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(folder, surface, output, *, actions=False):
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtTest import QTest
    except ImportError:
        return 77
    app = QtWidgets.QApplication([])
    family = folder if folder in ("esi", "amx_hd") else folder.rsplit("_", 1)[0]
    stub = "test_amx_hd_plugin_packaging" if family == "amx_hd" else f"test_{family}_plugin_behavior"
    importlib.import_module(stub)._install_esibd_stubs()
    core = sys.modules["esibd.core"]
    core.Channel.initGUI = lambda self, item: None
    core.Channel.ACTIVE = "Active"
    spec = importlib.util.spec_from_file_location("entry_probe", ROOT / folder / f"{family}_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, "AMXHDDevice" if family == "amx_hd" else f"{family.upper()}Device")
    device = cls.__new__(cls)
    device.loading = False
    device.main_state = "ST_ON"
    device.device_enabled_state = "ON"
    device.isOn = lambda: True
    device.frequency_khz = 2.0
    device.print = lambda *args, **kw: None
    device.controller = SimpleNamespace(device=object(), initialized=True, initializing=False,
                                        transitioning=False, global_enabled=True)
    calls = []
    device.channels = [SimpleNamespace(
        name=f"CH{i}", real=True, enabled=True, active=True, display=True, value=0.,
        color="#3182ce", id=i, module=i, function="HV",
        module_address=lambda i=i: i, channel_number=lambda i=i: i,
        pulser_number=lambda i=i: i, is_heat_channel=lambda: False,
        valueChanged=lambda i=i: calls.append(device.channels[i].value),
    ) for i in range(4 if family == "amx" else 3 if family == "esi" else 2)]
    device.getChannels = lambda: device.channels
    window = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(window)
    device.addContentWidget = layout.addWidget
    device.titleBar = QtWidgets.QToolBar()
    device.titleBarLabel = QtWidgets.QLabel(device.name)
    device.titleBar.addWidget(device.titleBarLabel)
    layout.addWidget(device.titleBar)
    device.tree = QtWidgets.QTreeWidget()
    device.tree.hide()
    device._schedule_delayed_refresh = lambda *_: None
    refresh = lambda: None
    get_committed = None
    set_programmatic = None

    if surface in ("table", "heater", "frequency_setting"):
        # Real Explorer numeric widgets + Parameter signal wiring. initGUI is
        # the plugin's real implementation; unrelated table cosmetics are inert.
        import re
        from enum import Enum
        from importlib.metadata import PackageNotFoundError, distribution
        import numpy as np
        try:
            host = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
        except PackageNotFoundError:
            return 77
        ns = dict(np=np, re=re, Path=Path, cast=lambda _, x: x, Channel=core.Channel,
                  Setting=type("Setting", (), {}), Qt=QtCore.Qt,
                  PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
        for qt in (QtCore, QtGui, QtWidgets):
            ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
        tree = ast.parse(host.read_text())
        for name in ("ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "Parameter"):
            node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
            exec(compile("from __future__ import annotations\n" + ast.unparse(node), str(host), "exec"), ns)
        # The frequency has a second editor in Explorer's global settings.
        if surface == "frequency_setting":
            parameter = ns["Parameter"]("Frequency", device, parameterType=ns["PARAMETERTYPE"].FLOAT,
                                         minimum=.001, maximum=10000.)
            parameter.value = device.frequency_khz
            def frequency_changed():
                device.frequency_khz = parameter.value
                calls.append(parameter.value)
            parameter.event = frequency_changed
            device.pluginManager = SimpleNamespace(Settings=SimpleNamespace(
                settings={f"{device.name}/{device.FREQUENCY_KHZ}": parameter}))
            device._update_config_controls()
            spin = parameter.spin
            layout.addWidget(spin)
            get_committed = lambda: parameter.value
            set_programmatic = lambda value: setattr(parameter, "value", value)
            refresh = device._update_config_controls

    if surface in ("table", "heater"):
        channel_type = getattr(module, "AMXHDChannel" if family == "amx_hd" else f"{family.upper()}Channel")
        channel = channel_type.__new__(channel_type)
        channel.channelParent = device
        channel.id = 0
        channel.module = 0 if surface == "heater" else 1
        channel.function = "Heater" if surface == "heater" else "HV"
        channel.real = channel.enabled = channel.active = True
        channel.loading = True
        channel.min, channel.max = 0., 3000.
        channel.name = "Setpoint"
        channel.rowHeight = 28
        channel.print = device.print
        channel.tree = None
        for name in ("_upgrade_toggle_widget", "_sync_enabled_toggle_widget", "_sync_monitor_feedback", "scalingChanged", "_configure_current_readout"):
            setattr(channel, name, lambda *a, **kw: None)
        channel_type.value = property(lambda ch: ch.params["Value"].value,
                                      lambda ch, value: setattr(ch.params["Value"], "value", value))
        parameter = ns["Parameter"]("Value", channel, parameterType=ns["PARAMETERTYPE"].FLOAT,
                                     minimum=0., maximum=3000., event=lambda: calls.append(channel.value))
        channel.params = {"Value": parameter, "Monitor": SimpleNamespace(unit="V"),
                          "Min": SimpleNamespace(unit="V"), "Max": SimpleNamespace(unit="V")}
        channel.parameters = [parameter]
        channel.getParameterByName = channel.params.get
        parameter.value = 0.
        channel.initGUI({})
        channel.loading = False
        device.channels = [channel]
        spin = parameter.spin
        layout.addWidget(spin)
        get_committed = lambda: channel.value
        set_programmatic = lambda value: setattr(channel, "value", value)
    elif surface == "frequency_setting":
        pass
    elif family == "esi":
        class ValueParameter:
            def __init__(self, channel, name):
                self.channel, self.name = channel, name
            @property
            def value(self):
                return getattr(self.channel, self.name)
            @value.setter
            def value(self, value):
                if self.value != value:
                    setattr(self.channel, self.name, value)
                    if self.name == "value":
                        calls.append(value)
                    else:
                        getattr(self.channel, "enabledChanged", lambda: None)()
        for channel in device.channels:
            channel.VALUE, channel.ENABLED = "Value", "Enabled"
            params = {"Value": ValueParameter(channel, "value"), "Enabled": ValueParameter(channel, "enabled")}
            channel.getParameterByName = params.get
        device._ensure_operator_panel()
        spin = device.esiHVCards[1]["target"]
        refresh = device._update_operator_panel
        get_committed = lambda: device.channels[1].value
    elif surface == "frequency":
        spin = device._create_frequency_widget()
        device.frequencyWidget = spin
        device._connect_frequency_widget(spin)
        device.frequencyChanged = lambda **kw: calls.append(device.frequency_khz)
        device._update_status_widgets = lambda: None
        layout.addWidget(spin)
        refresh = device._update_config_controls
        get_committed = lambda: device.frequency_khz
    elif surface == "width":
        device._ensure_operator_panel()
        spin = device.amxPanelCards[0]["width"]
        refresh = device._update_operator_panel
        get_committed = lambda: device.channels[0].value
    else:
        device._ensure_channel_panel()
        spin = device.manualPanelControls[0][surface]
        key = "voltage_values" if surface == "voltage" else "current_limit_values"
        device.controller.applyManualStateFromThread = lambda state, **kw: calls.append(state[key][0])
        refresh = device._sync_manual_panel_from_controller
        get_committed = spin.value

    other = QtWidgets.QLineEdit()
    other.setPlaceholderText("Click outside to validate")
    layout.addWidget(other)
    window.resize(1150, 700)
    window.show()
    window.activateWindow()
    app.processEvents()
    refresh()
    app.processEvents()
    calls.clear()
    if actions:
        from test_setpoint_actions_ui import action_probe
        return action_probe(module, device, window, spin, get_committed, calls, folder, surface, output,
                            local=actions == "local")
    actions = ["enter", "tab", "click"]
    panel = next((getattr(device, attr) for attr in ("esiPanel", "amxPanel", "channelPanel")
                  if getattr(device, attr, None) is not None), None)
    if surface in ("target", "width", "voltage", "current_limit"):
        assert panel is not None
        actions.append("blank")
    for index, action in enumerate(actions):
        # Each intermediate spelling is valid numerically, hence dangerous with
        # keyboardTracking=True. Wait beyond the old AMX debounce as well.
        old = get_committed()
        spin.setFocus()
        spin.selectAll()
        prefix = str(12 + index)
        QTest.keyClicks(spin, prefix)
        QTest.qWait(300)
        assert calls == [], (folder, surface, action, "sent during typing", calls)
        assert get_committed() == old, ("partial draft leaked to other commands", get_committed(), old)
        for _ in range(3):
            refresh()
            app.processEvents()
        assert prefix in spin.lineEdit().text(), ("poll overwrote draft", spin.text())
        assert get_committed() == old
        QTest.keyClicks(spin, "5.5")
        # Paste is another common way to replace a setpoint.
        if action == "click":
            spin.selectAll()
            app.clipboard().setText("225.5")
            QTest.keyClick(spin, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
        target = 225.5 if action == "click" else float(prefix + "5.5")
        app.processEvents()
        assert calls == []
        if action == "enter":
            QTest.keyClick(spin, QtCore.Qt.Key.Key_Return)
        elif action == "tab":
            QTest.keyClick(spin, QtCore.Qt.Key.Key_Tab)
        elif action == "blank":
            QTest.mouseClick(panel, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(4, 4))
        else:
            QTest.mouseClick(other, QtCore.Qt.MouseButton.LeftButton)
        QTest.qWait(300)
        assert calls and all(value == target for value in calls), (folder, surface, action, calls)
        assert get_committed() == target
        calls.clear()
        if family == "psu":
            # A delayed refresh returns the value the controller just accepted.
            attr = "voltage_setpoint_values" if surface == "voltage" else "current_limit_values"
            setattr(device.controller, attr, {0: target})
        # Use a different initial value on the next edit to exercise valueChanged.
        if set_programmatic:
            set_programmatic(20.)
            assert calls == [20.], "programmatic updates must remain immediate"
        # Complete any focus-only editingFinished signal of the next field
        # before starting a new independent edit (PSU submits on focus-out).
        other.setFocus()
        app.processEvents()
        calls.clear()
    window.grab().save(str(output / f"{folder}-{surface}.png"))
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
