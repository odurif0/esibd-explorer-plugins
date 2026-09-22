"""PSU -> Explorer Channel/Parameter -> UCM/AMX, with real Qt and driver workers.

Explorer's unrelated matplotlib GUI dependencies are optional in the test env.
Extract its unmodified classes/methods from the installed sources, not substitutes
for the Channel setters, callbacks, global update, history or UCM relay logic.
"""
from __future__ import annotations

import ast
import configparser
import __future__
from enum import Enum, auto
from importlib.metadata import distribution
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock, Thread
import time
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest


@pytest.mark.parametrize("folder", [f"psu_{s}" for s in "abcde"])
@pytest.mark.parametrize("configuration", ["bootstrap", "saved_bootstrap", "legacy_dict", "legacy_ini"])
def test_standard_channel_integration(folder, configuration, tmp_path):
    result = subprocess.run([sys.executable, __file__, folder, str(tmp_path), configuration],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            text=True, capture_output=True, timeout=45)
    if result.returncode == 77:
        pytest.skip("Qt or Explorer unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def real_framework():
    from PyQt6 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
    from asteval import Interpreter
    root = Path(distribution("esibd-explorer").locate_file("esibd"))
    trees = {name: ast.parse((root / name).read_text()) for name in ("core.py", "const.py", "plugins.py")}
    ns = dict(np=np, pg=pg, re=re, time=time, Path=Path, cast=cast, Enum=Enum, auto=auto,
              pyqtSignal=QtCore.pyqtSignal, pyqtSlot=QtCore.pyqtSlot, Qt=QtCore.Qt,
              Setting=type("Setting", (), {}), getDarkMode=lambda: True,
              colors=SimpleNamespace(fg="#eeeeee", bg="#20232a", highlight="#308cc6"),
              aeval=Interpreter(), CompactComboBox=QtWidgets.QComboBox,
              LedIndicator=QtWidgets.QLabel)
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})

    def extract(file, name, parent=None):
        nodes = trees[file].body if parent is None else parent.body
        node = next(n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(root / file), "exec",
                     flags=__future__.annotations.compiler_flag), ns)
        return ns[name]

    for name in ("INOUT", "PARAMETERTYPE", "PRINT", "PLUGINTYPE", "makeWrapper"):
        extract("const.py", name)
    for name in ("DynamicNp", "ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox",
                 "Label", "ColorButton", "CheckBox", "ToolButton", "LineEdit", "Parameter", "parameterDict", "RelayChannel", "Channel"):
        extract("core.py", name)
    ns["ScanChannel"] = type("ScanChannel", (ns["Channel"],), {})
    from test_psu_plugin_behavior import _install_esibd_stubs
    _install_esibd_stubs()
    core = sys.modules["esibd.core"]
    for name in ("Channel", "Parameter", "PARAMETERTYPE", "PRINT", "PLUGINTYPE", "parameterDict", "ToolButton"):
        setattr(core, name, ns[name])
    Device = sys.modules["esibd.plugins"].Device
    ns["Device"] = ns["ChannelManager"] = Device
    Device.getChannels = lambda self: self.channels
    Device.initialized = property(lambda self: bool(self.controller and self.controller.initialized))
    Device.subtractBackgroundActive = lambda self: False
    Device.getIcon = lambda self: QtGui.QIcon()
    device_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "Device")
    for name in ("updateValues", "applyValues"):
        setattr(Device, name, extract("plugins.py", name, device_node))
    dm_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "DeviceManager")
    manager = type("DeviceManager", (), {})
    for name in ("channels", "getChannelByName", "getDevices", "getInputDevices", "getOutputDevices", "getRelays", "globalUpdate"):
        setattr(manager, name, extract("plugins.py", name, dm_node))
    ucm_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "UCM")
    UCMChannel = extract("plugins.py", "UCMChannel", ucm_node)
    ns["UCMChannel"], ns["DeviceManager"] = UCMChannel, manager
    return ns


def probe(folder, output, configuration="bootstrap"):
    try:
        from PyQt6.QtCore import QThread, Qt
        from PyQt6.QtGui import QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QLabel, QLineEdit
    except ImportError:
        return 77
    app = QApplication([])
    app.setStyleSheet("QWidget { background: #20232a; color: #eeeeee; }"
                     "QHeaderView::section { background: #41464f; color: #eeeeee; }"
                     "QAbstractItemView { alternate-background-color: #30343c; }")
    ns = real_framework()
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(f"native_{folder}", root / folder / "psu_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parent = module.PSUDevice.__new__(module.PSUDevice)
    parent.channels = []
    parent.channelType = module.PSUChannel
    parent.loading = True
    parent.interval = 1000
    parent.updating = False
    parent.unit = "V"
    parent.inout = ns["INOUT"].IN
    parent.useDisplays, parent.useMonitors = True, True
    parent.useBackgrounds, parent.logY = False, False
    parent.convertDataDisplay, parent.liveDisplay = None, None
    parent.recording, parent.maxDataPoints = False, 100000
    parent.isOn = lambda: True
    parent.makeCoreIcon = lambda *a: QIcon()
    parent.print = lambda *a, **kw: None
    parent.startup_timeout_s, parent.poll_timeout_s = 1., .1
    parent.interlock_monitoring = True
    parent._schedule_delayed_refresh = lambda *a: None
    manager = ns["DeviceManager"]()
    plugin_manager = SimpleNamespace(
        Device=ns["Device"], ChannelManager=ns["Device"], DeviceManager=manager,
        loading=True, closing=False, plugins=[parent], connectAllSources=lambda: None,
        reconnectSource=lambda *a: None,
    )
    plugin_manager.getPluginsByClass = lambda cls: [p for p in plugin_manager.plugins if isinstance(p, cls)]
    plugin_manager.getPluginsByType = lambda kind: [p for p in plugin_manager.plugins if getattr(p, "pluginType", None) == kind]
    manager.pluginManager = parent.pluginManager = plugin_manager
    manager.print = parent.print
    manager.updateStaticPlot = lambda: None
    window = QWidget()
    layout = QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget
    toolbar = QHBoxLayout()
    parent.statusBadgeLabel, parent.statusSummaryLabel = QLabel(), QLabel()
    toolbar.addWidget(parent.statusBadgeLabel)
    toolbar.addWidget(parent.statusSummaryLabel)
    layout.addLayout(toolbar)

    class Tree(QTreeWidget):
        pass

    tree = parent.tree = Tree()
    layout.addWidget(QLabel("PSU standard Channels"))
    layout.addWidget(tree)
    tree.setMaximumHeight(105)
    for ch in (0, 1):
        channel = module.PSUChannel(parent, tree)
        tree.setColumnCount(len(channel.parameters))
        tree.addTopLevelItem(channel)
        parent.channels.append(channel)
        if configuration == "bootstrap":
            # The production factory used to read uninitialized widgets via
            # asDict(): Max=0, Active=False, instead of the declared defaults.
            config = parent._bootstrap_channel_items()[ch]
        else:
            config = {"Name": f"{parent.name}_CH{ch}", "CH": str(ch)}
            if configuration == "saved_bootstrap":
                config.update({"Value": 0., "Enabled": True, "Active": False,
                               "Min": 0., "Max": 0., "Equation": ""})
            else:
                config.update({"Enabled": False, "Value": 888., "Voltage set": "old readback"})
            if configuration != "legacy_dict":
                parser = configparser.ConfigParser()
                parser.read_dict({"Channel": config})
                config = parser["Channel"]  # Explorer passes case-insensitive INI sections.
        channel.initGUI(config)
        assert channel.enabled and channel.active and channel.value == 0., channel.asDict()
        assert (channel.min, channel.max) == (0., 10000.), channel.asDict()
        assert channel.name == f"{parent.name}_CH{ch}" and channel.channel_number() == ch
        assert "Voltage set" not in channel.asDict()
        # A repaired channel remains correct after native serialization/reload.
        channel.initGUI(channel.asDict())
        assert channel.active and channel.max == 10000.
    # Repair only the malformed factory signature, not intentional settings.
    for active, equation, maximum in ((True, "", 700.), (True, "", 0.), (False, "42", 0.)):
        config_tree = Tree()
        configured = module.PSUChannel(parent, config_tree)
        config_tree.setColumnCount(len(configured.parameters))
        config_tree.addTopLevelItem(configured)
        parser = configparser.ConfigParser()
        parser.read_dict({"Channel": {"Name": "My rail", "CH": "1", "Enabled": False,
                                     "Active": active, "Equation": equation, "Min": 0., "Max": maximum}})
        configured.initGUI(parser["Channel"])
        assert configured.name == "My rail" and configured.channel_number() == 1
        assert not configured.enabled and configured.active is active
        assert configured.equation == equation and (configured.min, configured.max) == (0., maximum)
    tree.setHeaderLabels([p.name for p in parent.channels[0].parameters])
    for i, p in enumerate(parent.channels[0].parameters):
        tree.setColumnHidden(i, p.name not in ("Name", "Value", "Monitor", "Output"))
    controller = parent.controller = module.PSUController(parent)
    controller.lock = Lock()
    controller.initialized = True
    controller.print = parent.print
    from psu_fakes import StatefulPSU

    class Hardware(StatefulPSU):
        def collect_housekeeping(self, **kw):
            return dict(main_state={"name": "ST_ON" if self.enabled else "ST_OFF"},
                device_enabled=self.enabled, output_enabled=self.outputs,
                psu_state={"psu_enabled_actual": self.enabled, "interlock_active": True,
                           "interlock_out_disabled": False, "interlock_bnc_disabled": False},
                device_state={"flags": []},
                channels=[dict(channel=i, enabled=self.outputs[i],
                    voltage={"measured_v": self.voltages[i] - .25, "set_v": self.voltages[i]},
                    current={"measured_a": self.currents[i] / 2, "set_a": self.currents[i]},
                    full_range={"enabled": self.ranges[i], "supported": True}) for i in (0, 1)])

        def get_channel_measurements(self, ch, **kw):
            return self.voltages[ch] - .25, self.currents[ch] / 2, 15.

    hw = controller.device = Hardware()
    hw.enabled, hw.outputs, hw.interlocks = True, (True, True), (True, True)
    hw.voltages, hw.currents = {0: 100., 1: 137.1234}, {0: .0014, 1: .0004}
    parent.loading, plugin_manager.loading = False, False
    controller._update_state()
    parent._ensure_channel_panel()
    other_edit = QLineEdit()
    layout.addWidget(other_edit)
    window.resize(980, 950)
    window.show()
    window.activateWindow()
    app.processEvents()
    channels = parent.channels
    assert [c.value for c in channels] == [100., 137.1234], [(c.value, c.getParameterByName(c.VALUE).spin.decimals()) for c in channels]
    assert [c.monitor for c in channels] == [99.75, 136.8734], [c.monitor for c in channels]
    assert all(c.enabled and c.initialized and c.unit == "V" for c in channels)
    assert all(not c.getParameterByName(c.VALUE).spin.keyboardTracking() for c in channels)
    assert hw.calls == [], hw.calls

    def drain():
        deadline = time.monotonic() + 5
        while controller._manual_apply_worker_running and time.monotonic() < deadline:
            QTest.qWait(10)
        QTest.qWait(50)
        assert not controller._manual_apply_worker_running
        assert controller.errorCount == 0

    def voltage_edit(edit, expected):
        hw.calls.clear()
        edit()
        drain()
        assert hw.calls == [("voltage", 0, expected)], hw.calls
        assert hw.voltages == {0: expected, 1: 137.1234}
        assert hw.outputs == (True, True)
        assert channels[0].value == expected
        assert channels[0].monitor == expected - .25
        assert parent.manualPanelControls[0]["voltage"].value() == expected

    # Real panel + real hidden Channels. Stale fields on the other card must
    # never become commands when this card's Vset or Ilim is validated.
    parent._hide_channel_table()
    controls = parent.manualPanelControls

    def stale_other_card(ch):
        for key, value in (("voltage", 0.), ("current_limit", 0.)):
            widget = controls[ch][key]
            blocked = widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(blocked)
        widget = controls[ch]["output_enabled"]
        blocked = widget.blockSignals(True)
        widget.setChecked(False)
        widget.blockSignals(blocked)
        widget = controls[ch]["full_range"]
        blocked = widget.blockSignals(True)
        widget.setCurrentIndex(0)
        widget.blockSignals(blocked)

    def panel_edit(ch, key, target, action):
        previous_v, previous_i = dict(hw.voltages), dict(hw.currents)
        hw.calls.clear()
        spin = controls[ch][key]
        spin.setFocus()
        spin.selectAll()
        QTest.keyClicks(spin, str(target))
        stale_other_card(1 - ch)
        if action == "enter":
            QTest.keyClick(spin, Qt.Key.Key_Return)
        elif action == "tab":
            QTest.keyClick(spin, Qt.Key.Key_Tab)
        else:
            QTest.mouseClick(other_edit, Qt.MouseButton.LeftButton)
        drain()
        command = "voltage" if key == "voltage" else "current"
        assert hw.calls == [(command, ch, float(target))], hw.calls
        (previous_v if key == "voltage" else previous_i)[ch] = float(target)
        assert hw.voltages == previous_v and hw.currents == previous_i
        assert hw.outputs == (True, True) and hw.ranges == (False, False)
        controller._update_state()
        parent._sync_manual_panel_from_controller()
        app.processEvents()
        for i in (0, 1):
            assert channels[i].value == hw.voltages[i]
            assert controls[i]["voltage"].value() == round(hw.voltages[i], 3)
        assert hw.calls == [(command, ch, float(target))], hw.calls

    panel_edit(1, "voltage", 110., "enter")
    panel_edit(0, "voltage", 80., "tab")
    panel_edit(1, "current_limit", .002, "click")
    panel_edit(0, "voltage", 100., "click")
    window.grab().save(str(output / f"{folder}-independent-vset.png"))
    # Return to the precise hardware setpoints used by the UCM tests below.
    hw.voltages[1], hw.currents[1] = 137.1234, .0004
    controller._update_state()
    parent._sync_manual_panel_from_controller()
    tree.show()
    other_edit.hide()
    app.processEvents()

    voltage_edit(lambda: setattr(channels[0], "value", 80.), 80.)
    # Real Channel.applyValue caches lastAppliedValue: globalUpdate is idempotent.
    hw.calls.clear()
    manager.globalUpdate()
    drain()
    assert hw.calls == []

    ucm_parent = ns["Device"]()
    for key, value in vars(parent).items():
        if key not in ("controller", "channels", "tree"):
            setattr(ucm_parent, key, value)
    ucm_parent.name, ucm_parent.inout = "UCM", ns["INOUT"].NONE
    ucm_parent.pluginType = ns["PLUGINTYPE"].CHANNELMANAGER
    ucm_parent.loading, ucm_parent.channels, ucm_parent.controller = True, [], None
    ucm_tree = ucm_parent.tree = Tree()
    layout.addWidget(QLabel("Explorer UCM"))
    layout.addWidget(ucm_tree)
    ucm_tree.setMaximumHeight(105)
    plugin_manager.plugins.append(ucm_parent)
    relay = ns["UCMChannel"](ucm_parent, ucm_tree)
    ucm_tree.setColumnCount(len(relay.parameters))
    ucm_tree.addTopLevelItem(relay)
    ucm_parent.channels.append(relay)
    relay.initGUI({"Name": channels[0].name})
    ucm_parent.loading = False
    relay.connectSource()
    assert relay.sourceChannel is channels[0]
    assert relay.monitor == channels[0].monitor and relay.value == 80.
    for i, p in enumerate(relay.parameters):
        ucm_tree.setColumnHidden(i, p.name not in ("Name", "Value", "Monitor", "Unit"))
    ucm_tree.setHeaderLabels([p.name for p in relay.parameters])
    voltage_edit(lambda: setattr(relay, "value", 70.), 70.)
    assert relay.monitor == 69.75

    # Real scan relay writes the same source field; no custom PSU API.
    scan = ns["RelayChannel"]()
    scan.sourceChannel = channels[0]
    voltage_edit(lambda: setattr(scan, "value", 65.), 65.)
    assert relay.value == 65.
    # Explorer itself evaluates an equation and dispatches the resulting value.
    channels[0].equation = "60"
    voltage_edit(lambda: setattr(channels[0], "active", False), 60.)
    assert channels[0].value == relay.value == 60.
    channels[0].active = True

    # AMX resolves these actual Channels through the actual DeviceManager.
    amx_folder = "amx_b" if folder in ("psu_b", "psu_d") else "amx_a"
    amx_spec = importlib.util.spec_from_file_location(
        f"native_{amx_folder}", root / amx_folder / "amx_plugin.py")
    amx_module = importlib.util.module_from_spec(amx_spec)
    amx_spec.loader.exec_module(amx_module)
    amx = amx_module.AMXDevice.__new__(amx_module.AMXDevice)
    from test_amx_outputs import config79_snapshot
    amx.channels, amx.loading, amx.updating = [], False, False
    amx.pluginManager = plugin_manager
    amx.inout = ns["INOUT"].IN
    amx.main_state, amx.frequency_khz = "STATE_ON", 500.
    amx.isOn = lambda: True
    amx.controller = SimpleNamespace(
        initialized=True, device=object(), output_rows=amx_module._amx_output_rows(config79_snapshot()))
    amx.psu_ch01, amx.psu_ch23 = parent.name, "None"
    amx.addContentWidget = layout.addWidget
    plugin_manager.plugins.append(amx)
    amx._ensure_operator_panel()
    window.resize(1050, 1150)
    assert amx._linked_psu_readback(parent.name)["vpos"] == 59.75
    assert amx.amxOutputTable.item(0, 3).text() == "-136.873 V ↔ +59.75 V"
    assert amx.amxOutputTable.item(2, 3).text() == "Vneg ↔ Vpos"

    # A worker updates actual Parameter widgets via the GUI dispatcher, and UCM
    # extraEvents are delivered. Thread-safe standard monitor publication.
    observed_threads = []
    channels[0].getParameterByName(channels[0].MONITOR).extraEvents.append(
        lambda: observed_threads.append(QThread.currentThread()))
    hw.voltages[0] = 55.
    worker = Thread(target=controller._update_state)
    worker.start()
    worker.join(3)
    assert not worker.is_alive()
    QTest.qWait(70)
    assert observed_threads and all(t is app.thread() for t in observed_threads)
    assert channels[0].monitor == relay.monitor == 54.75
    assert channels[0].value == relay.value == 55.
    amx._update_output_table()
    assert amx._linked_psu_readback(parent.name)["vpos"] == relay.monitor
    assert amx.amxOutputTable.item(0, 3).text() == "-136.873 V ↔ +54.75 V"
    hw.calls.clear()
    channels[0].appendValue(lenT=1)
    assert channels[0].values.get()[-1] == 54.75
    controller._invalidate_hv_readback()
    assert np.isnan(channels[0].monitor) and np.isnan(relay.monitor)
    amx._update_output_table()
    assert amx.amxOutputTable.item(0, 3).text() == "Vneg ↔ Vpos"
    channels[0].appendValue(lenT=2)
    assert np.isnan(channels[0].values.get()[-1])
    assert hw.calls == []

    controller._update_state()
    app.processEvents()
    amx._update_output_table()
    assert parent.channelPanelCards[0]["voltage_monitor"].text() == "54.75 V"
    assert "54.75 V" in parent.statusSummaryLabel.text()
    assert "Vget: 54.75 V" in parent.channelPanelCards[0]["card"].toolTip()
    for visible_tree in (tree, ucm_tree):
        for i in range(visible_tree.columnCount()):
            if not visible_tree.isColumnHidden(i):
                visible_tree.resizeColumnToContents(i)
                visible_tree.setColumnWidth(i, max(visible_tree.columnWidth(i), 130))
    app.processEvents()
    window.grab().save(str(output / f"{folder}-standard-channels.png"))
    # A narrower user limit can coerce an observed setpoint. That observation
    # must not become a CH1 write at the next globalUpdate or CH0 edit.
    parent.loading = True
    channels[1].max = 120.
    channels[1].updateMax()
    parent.loading = False
    hw.calls.clear()
    controller._update_state()
    manager.globalUpdate()
    drain()
    assert channels[1].value == channels[1].lastAppliedValue == 120., (
        channels[1].value, channels[1].lastAppliedValue, channels[1].max,
        channels[1].getParameterByName(channels[1].VALUE).spin.maximum(), hw.calls)
    assert hw.voltages[1] == 137.1234 and hw.calls == []
    voltage_edit(lambda: setattr(channels[0], "value", 50.), 50.)
    parent.loading = True
    channels[1].max = 10000.
    channels[1].updateMax()
    parent.loading = False
    hw.voltages[0] = 55.
    hw.calls.clear()
    controller._update_state()
    app.processEvents()
    # Expiry works even without recording or another poll/AMX read.
    QTest.qWait(2150)
    assert np.isnan(channels[0].monitor) and np.isnan(relay.monitor)
    assert "expired" in channels[0].readback_status
    assert parent.channelPanelCards[0]["voltage_monitor"].text() == "n/a"
    assert "54.75" not in parent.statusSummaryLabel.text()
    assert "Vget: n/a" in parent.channelPanelCards[0]["card"].toolTip()
    amx._update_output_table()
    assert amx.amxOutputTable.item(0, 3).text() == "Vneg ↔ Vpos"
    assert hw.calls == [], "Expiry or AMX access must not trigger a hardware call"
    # A standard-channel write must never turn an HV gate on.
    hw.outputs = (False, True)
    controller._update_state()
    channels[0].value = 40.
    drain()
    assert hw.outputs == (False, True)
    assert hw.calls == [("voltage", 0, 40.)]
    assert channels[0].enabled and np.isnan(channels[0].monitor)
    assert np.isnan(relay.monitor)
    hw.calls.clear()
    channels[0].enabled = False
    channels[0].value = 30.
    drain()
    assert hw.calls == [], "Explorer-disabled channels must not dispatch setpoints"
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "bootstrap"))
