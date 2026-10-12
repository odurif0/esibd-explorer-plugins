"""Setpoint-entry real-Qt probes shared by the setpoint UI tests."""
from __future__ import annotations

import ast
import importlib
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
CASES = [(folder, surface) for folder in ("amx_a", "amx_b", "amx_hd")
         for surface in ("table", "frequency", "frequency_setting")]
CASES += [(folder, "width") for folder in ("amx_a", "amx_b")]
CASES += [("esi", surface) for surface in ("table", "heater", "target")]
CASES += [(f"psu_{suffix}", surface) for suffix in "abcde" for surface in ("voltage", "current_limit")]


def probe(folder, surface, output, *, actions=False):
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtTest import QTest
    except ImportError:
        return 77
    app = QtWidgets.QApplication([])
    family = folder if folder in ("esi", "amx_hd") else folder.rsplit("_", 1)[0]
    from support import esi, explorer
    (esi.install if family == "esi" else explorer.install)()
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
                                        transitioning=False, global_enabled=True, main_state="STATE_ON",
                                        heat_max_temperature_c=float('nan'), heat_max_power_w=float('nan'),
                                        heat_temperature_error="", heat_power_error="", heat_power_limit_w=50.,
                                        heat_stability_status=lambda: dict(state="Unavailable", slope_c_min=None,
                                                                          span_s=0., expires_at_s=None))
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
        from enum import Enum
        from importlib.metadata import PackageNotFoundError, distribution
        import re

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
        ns.update(pyqtSignal=QtCore.pyqtSignal, pyqtSlot=QtCore.pyqtSlot)  # Explorer 1.0.1 Parameter
        from explorer_host import add_const_names
        add_const_names(host.parent, ns)
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
        device.loading = True  # Channel.loading is read-only: it follows the device.
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
        device.loading = False
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
        device.advancedAction = SimpleNamespace(state=True)
        device._ensure_operator_panel()
        spin = device.amxPanelCards[0]["width"]
        refresh = device._update_operator_panel
        get_committed = lambda: device.channels[0].value
    else:
        device.controller.current_limit_values = {0: 0., 1: 0.}
        device._ensure_channel_panel()
        spin = device.manualPanelControls[0][surface]
        key = "voltage_values" if surface == "voltage" else "current_limit_values"
        # Focus-out on another editor may submit its own (unchanged) setting.
        # Unlike reconfiguration requests, per-field edits have no other keys.
        device.controller.applyManualStateFromThread = lambda state, **kw: (
            calls.append(state[key][0]) if 0 in state.get(key, {}) else None
        )
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


def local_probe(device, window, spin, current_value, calls, folder, surface, output):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    commands = []
    if folder.startswith('psu'):
        button_on = button_off = device.manualPanelControls[0]['output_enabled']
        key = 'voltage_values' if surface == 'voltage' else 'current_limit_values'
        device.controller.applyManualStateFromThread = lambda state, **_: commands.append((state['output_enabled'][0], state[key][0]))
    else:
        channel = device.channels[1 if folder == 'esi' else 0]
        channel.enabled = False
        channel.enabledChanged = lambda: commands.append((channel.enabled, channel.value))
        if folder == 'esi':
            button_on, button_off = (device.esiHVCards[1][name] for name in ('btn_on', 'btn_off'))
        else:
            button_on = button_off = device.amxPanelCards[0]['enable']
    for button, checked in ((button_on, False), (button_off, folder == 'esi')):
        blocked = button.blockSignals(True)
        button.setChecked(checked)
        button.blockSignals(blocked)
    spin.setValue(100.)
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, '250')
    app.processEvents()
    calls.clear()
    QTest.mouseClick(button_on, Qt.MouseButton.LeftButton, pos=QPoint(10, button_on.height() // 2))
    app.processEvents()
    assert commands == [(True, 250.)], (commands, spin.text())
    assert calls == [], ('An extra setpoint was sent before enabling', calls)
    assert current_value() == 250.
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, '350')
    app.processEvents()
    commands.clear()
    calls.clear()
    QTest.mouseClick(button_off, Qt.MouseButton.LeftButton, pos=QPoint(10, button_off.height() // 2))
    app.processEvents()
    assert commands == [(False, 250.)], (commands, spin.text())
    assert calls == [], ('An extra setpoint was sent before disabling', calls)
    assert current_value() == 250.
    window.grab().save(str(output / f'{folder}-{surface}-local.png'))
    spin.blockSignals(True)
    window.close()
    return 0


def action_probe(module, device, window, spin, current_value, calls, folder, surface, output, *, local=False):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    from support.ui import explorer_state_action

    if local:
        return local_probe(device, window, spin, current_value, calls, folder, surface, output)
    app = QApplication.instance()
    window.name = device.name
    window.titleBar = device.titleBar
    controller = device.controller
    controller._begin_transition = lambda *_: True
    controller.controllerParent = device
    commands = []
    controller.toggleOnFromThread = lambda **_: commands.append((device.isOn(), current_value()))
    device._update_status_widgets = lambda: None
    device._sync_onoff_actions = lambda: None
    # The action owns no focus, exactly like Explorer's real toolbar buttons.
    StateAction = explorer_state_action()
    def action(checked):
        if folder.startswith("psu") and checked:
            device._apply_manual_panel_state()
        else:
            device.setOn(checked)
    button_action = StateAction(parentPlugin=window, restore=False, iconFalse=QIcon(), iconTrue=QIcon(),
                                toolTipFalse="Turn ON", toolTipTrue="Turn OFF", event=action)
    device.onAction = button_action
    device.isOn = lambda: button_action.state
    button = window.titleBar.widgetForAction(button_action)
    assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    app.processEvents()

    if folder.startswith("psu"):
        device.manualPanelControls[0]["output_enabled"].setChecked(True)
    spin.setValue(100.)
    calls.clear()
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "250")
    app.processEvents()
    assert spin.value() == 100.
    assert "250" in spin.text()
    calls.clear()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    app.processEvents()
    if folder.startswith("psu"):
        assert calls == [250.], (folder, surface, calls)
    else:
        assert commands == [(True, 250.)], (folder, surface, commands, spin.text())
        assert calls == [], "Reading the editor must not send an extra output command"
    assert current_value() == 250.
    # A second click is OFF. Do not submit a higher target before stopping.
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "350")
    app.processEvents()
    commands.clear()
    calls.clear()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert commands == [(False, 250.)], (folder, surface, commands)
    assert calls == [], ("OFF submitted a voltage increase", calls)
    assert spin.value() == 250.
    if not folder.startswith("psu"):
        # The next ON may need to reconnect. It still uses the text left in
        # the editor by OFF, without forcing a preliminary value command.
        controller.initialized = False
        device.initializeCommunication = lambda: commands.append(("initialize", current_value()))
        commands.clear()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert commands == [("initialize", 350.)], (folder, surface, commands)
        assert current_value() == 350.
        assert calls == []
    window.grab().save(str(output / f"{folder}-{surface}-action.png"))
    # Do not validate the unfinished field while closing this test fixture.
    spin.blockSignals(True)
    window.close()
    return 0
