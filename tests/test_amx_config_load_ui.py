"""Config loads through a real Qt button, Explorer Parameters and worker thread."""
from __future__ import annotations

import ast
from enum import Enum
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import pytest

from test_amx_config_load import FOLDERS

CASES = [(folder, surface) for folder in FOLDERS
         for surface in ("frequency", "frequency_setting", "table")]
CASES += [(folder, "width") for folder in FOLDERS if folder != "amx_hd"]


@pytest.mark.parametrize("folder,surface", CASES)
def test_config_load_replaces_old_fields_without_sending_them(folder, surface, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, surface, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt / Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(folder, surface, output):
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtTest import QTest
        from importlib.metadata import distribution, PackageNotFoundError
        import numpy as np
        host = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    except (ImportError, PackageNotFoundError):
        return 77
    from test_amx_config_load import make_case
    app = QtWidgets.QApplication([])
    module, parent, controller = make_case(folder)
    core = sys.modules["esibd.core"]
    core.Channel.ACTIVE = "Active"
    core.Channel.DISPLAY = "Display"
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, x: x, Channel=core.Channel,
              Setting=type("Setting", (), {}), Qt=QtCore.Qt, pyqtSignal=QtCore.pyqtSignal,
              CompactComboBox=QtWidgets.QComboBox, LedIndicator=QtWidgets.QLabel,
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    tree = ast.parse(host.read_text())
    for name in ("ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "CheckBox", "Parameter"):
        node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
        exec(compile("from __future__ import annotations\n" + ast.unparse(node), str(host), "exec"), ns)
    host_channel = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Channel")
    for name in ("applyValue", "activeChanged"):
        method = next(n for n in host_channel.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile("from __future__ import annotations\n" + ast.unparse(method), str(host), "exec"), ns)
        setattr(core.Channel, name, ns[name])
    core.Channel.updateColor = lambda _: None
    core.Channel.toggleBackgroundVisible = lambda _: None
    Parameter, Kind = ns["Parameter"], ns["PARAMETERTYPE"]
    window = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget
    parent.main_state, parent.device_enabled_state = "STATE_ON", "ON"
    parent.available_configs = controller.available_configs
    parent.loaded_config_text = "Previously loaded"
    parent._sync_toolbar_communication_controls = lambda: None
    parent._update_channel_column_visibility = lambda: None
    parent._sync_channels = lambda: None
    controller.initializing = False

    # Real Settings use a separate Parameter parent and property-backed access.
    from types import SimpleNamespace
    settings_owner = SimpleNamespace(loading=False, print=parent.print)
    frequency = Parameter("Frequency", settings_owner, parameterType=Kind.FLOAT,
                          minimum=.001, maximum=10000.)
    frequency.value = 2.
    frequency.event = parent.frequencyChanged
    assert frequency.value == 2.
    parent._setting = lambda name: frequency if name == parent.FREQUENCY_KHZ else None
    type(parent).frequency_khz = property(lambda _: frequency.value,
                                         lambda _, value: setattr(frequency, "value", value))
    parent.frequencyWidget = parent._create_frequency_widget()
    parent._connect_frequency_widget(parent.frequencyWidget)
    layout.addWidget(QtWidgets.QLabel("Oscillator frequency — toolbar / Explorer setting"))
    layout.addWidget(parent.frequencyWidget)
    layout.addWidget(frequency.spin)

    channel_cls = module.AMXHDChannel if folder == "amx_hd" else module.AMXChannel
    for attr, name in (("value", "Value"), ("enabled", "Enabled"), ("active", "Active")):
        setattr(channel_cls, attr, property(
            lambda ch, name=name: ch.params[name].value,
            lambda ch, value, name=name: setattr(ch.params[name], "value", value)))
    channels = []
    for p in range(controller.device.count):
        channel = channel_cls.__new__(channel_cls)
        channel.channelParent = parent
        channel.controller = controller
        channel.id, channel.name, channel.real = p, f"P{p}", True
        channel.loading = True
        channel.tree = None
        channel.print = parent.print
        channel.equation = "123"
        channel.display = True
        channel.params = {}
        channel.getParameterByName = channel.params.get
        for name, kind, value, event in (
            ("Value", Kind.FLOAT, 100., channel.valueChanged),
            ("Enabled", Kind.BOOL, True, channel.enabledChanged),
            ("Active", Kind.BOOL, False, channel.activeChanged),
        ):
            parameter = Parameter(name, channel, parameterType=kind,
                                  minimum=0., maximum=1000., event=event)
            channel.params[name] = parameter
            parameter.value = value
        channel.params["Value"].spin.setKeyboardTracking(False)
        channel.loading = False
        channels.append(channel)
    parent.channels = channels
    # The table's actual value widget is shown alongside the production panel.
    layout.addWidget(QtWidgets.QLabel("P0 — Explorer channel value"))
    layout.addWidget(channels[0].params["Value"].spin)
    if folder != "amx_hd":
        parent.advancedAction = SimpleNamespace(state=True)
        parent._ensure_operator_panel()
    button = QtWidgets.QPushButton("Load now")
    button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
    button.clicked.connect(parent.loadOperatingConfigNow)
    layout.addWidget(button)
    parent.loadOperatingConfigButton = button
    window.resize(1150, 850)
    window.show()
    window.activateWindow()
    parent._update_config_controls()
    app.processEvents()

    adopted = []
    real_setter = module._set_config_parameter
    def checked_setter(*args):
        assert QtCore.QThread.currentThread() == app.thread(), "Parameter updated off the GUI thread"
        adopted.append(args[1])
        real_setter(*args)
    module._set_config_parameter = checked_setter

    spin = {"frequency": parent.frequencyWidget, "frequency_setting": frequency.spin,
            "table": channels[0].params["Value"].spin}.get(surface)
    if surface == "width":
        # A card is only editable in manual mode; other channels retain their
        # old equation mode to verify that loading takes precedence over it.
        channels[0].loading = True
        channels[0].active = True
        channels[0].loading = False
        parent._update_operator_panel()
        spin = parent.amxPanelCards[0]["width"]
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "7")
    app.processEvents()
    assert "7" in spin.text()
    assert parent.frequency_khz == 2.
    # Debounced actions from before the load must not survive it.
    parent._schedule_global_settings_apply()
    channels[0]._schedule_value_apply()
    hw = controller.device
    hw.calls.clear()
    QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
    deadline = time.monotonic() + 5
    while (not adopted or controller.transitioning or getattr(controller, "_pending_config_snapshot", None) is not None) and time.monotonic() < deadline:
        QTest.qWait(20)
    QTest.qWait(350)
    assert adopted, "Worker never delivered the config to Qt"
    assert controller.errorCount == 0
    assert hw.timing_writes == [], hw.calls
    assert hw.period == 198 and hw.widths[0] == 98
    assert parent.frequency_khz == 500.
    assert frequency.value == parent.frequencyWidget.value() == 500.
    for p, channel in enumerate(channels):
        assert channel.value == (1. if p == 0 else 0.)
        assert channel.enabled is (p == 0)
        assert channel.active is True
        assert channel.equation == "123"
        assert channel.lastAppliedValue == channel.value
    assert spin.value() == (500. if surface.startswith("frequency") else 1.)
    assert "7" not in spin.text(), (surface, spin.text())
    QTest.keyClick(spin, QtCore.Qt.Key.Key_Tab)
    QTest.qWait(350)
    assert hw.timing_writes == [], ("Old focused draft was applied on focus-out", hw.calls)
    if folder != "amx_hd":
        parent._update_operator_panel()
        assert parent.amxPanelCards[0]["width"].value() == 1.
        assert parent.amxPanelCards[0]["status"].text() == "REGISTERS APPLIED"
        assert parent.amxPanelCards[1]["width"].value() == 0.
    window.grab().save(str(output / f"{folder}-{surface}-loaded.png"))

    # A later explicit edit goes through the real widget callbacks and driver.
    edit = channels[0].params["Value"].spin
    edit.setFocus()
    edit.selectAll()
    QTest.keyClicks(edit, "0.5")
    QTest.keyClick(edit, QtCore.Qt.Key.Key_Return)
    QTest.qWait(400)
    assert hw.widths[0] == 48, hw.calls
    assert hw.period == 198
    parent.frequencyWidget.setFocus()
    parent.frequencyWidget.selectAll()
    QTest.keyClicks(parent.frequencyWidget, "250")
    QTest.keyClick(parent.frequencyWidget, QtCore.Qt.Key.Key_Return)
    QTest.qWait(400)
    assert hw.period == 398, hw.calls
    assert hw.widths[0] == 48
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
