"""Compact DMMR cards, elastic columns and label editing under real Qt."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scale", ["1", "1.5"])
@pytest.mark.parametrize("case", ["grid", "labels", "parameter", "save_failure", "states"])
def test_dmmr_cards(case, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real PyQt6 unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def use_real_label_parameters(module, device):
    """Use Explorer's real Parameter widget/getter/setter, without its app shell."""
    import ast
    from enum import Enum
    from importlib.metadata import distribution
    import re
    import numpy as np
    from PyQt6 import QtCore, QtGui, QtWidgets

    path = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    tree = ast.parse(path.read_text())
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, value: value, Channel=module.Channel, pyqtSignal=QtCore.pyqtSignal,
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    ns.update(CheckBox=QtWidgets.QCheckBox, CompactComboBox=QtWidgets.QComboBox,
              LedIndicator=QtWidgets.QAbstractButton, PRINT=module.PRINT)
    for name in ("ParameterWidget", "Label", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "LineEdit", "Parameter", "parameterDict"):
        node = next(n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(unit), str(path), "exec"), ns)
    module.Parameter = ns["Parameter"]
    module.PARAMETERTYPE = ns["PARAMETERTYPE"]
    module.parameterDict = ns["parameterDict"]
    module.Channel.getDefaultChannel = lambda self: {key: {} for key in ("Value", "Enabled", "Display", "Active")}
    module.Channel.displayChanged = lambda self: None
    declaration = module.DMMRChannel.__new__(module.DMMRChannel).getDefaultChannel()["Label"]
    module.DMMRChannel.label = property(lambda ch: ch.label_parameter.value,
                                       lambda ch, value: setattr(ch.label_parameter, "value", value))
    for i, previous in enumerate(device.channels):
        channel = module.DMMRChannel.__new__(module.DMMRChannel)
        channel.__dict__.update({k: v for k, v in vars(previous).items() if k != "label"})
        channel.channelParent = device
        channel.loading = True
        channel.print = device.print
        channel.tree, channel.rowHeight = None, 28
        parameter = ns["Parameter"]("Label", channel, parameterType=declaration[ns["Parameter"].PARAMETER_TYPE])
        channel.label_parameter = parameter
        parameter.value = previous.label
        channel.loading = False
        device.channels[i] = channel


def probe(case, output):
    try:
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QSignalSpy, QTest
        from PyQt6.QtWidgets import QApplication, QVBoxLayout, QWidget
    except ImportError:
        return 77
    app = QApplication([])
    from test_dmmr_plugin_behavior import _install_esibd_stubs
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("dmmr_cards_probe", ROOT / "dmmr/dmmr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.loading = False
    device.main_state = "ST_ON"
    device.isOn = lambda: True
    device.controller = SimpleNamespace(values={i: (i + 1) * 1.5e-12 for i in range(8)})
    colors = ("#3182ce", "#d97706", "#48a999", "#b56191")
    device.channels = [SimpleNamespace(name=f"DMMR_M{i:02}", label="", real=True, enabled=True,
                                      display=True, color=colors[i % 4], module_address=lambda i=i: i)
                       for i in range(8)]
    device.channels[0].label = "Collecteur"
    device.channels[1].label = "Entrée"
    device.getChannels = lambda: device.channels
    exports = []
    device.exportConfiguration = lambda **kwargs: exports.append(kwargs)
    device.print = lambda *args, **kwargs: None
    if case == "parameter":
        use_real_label_parameters(module, device)
    window = QWidget()
    layout = QVBoxLayout(window)
    layout.setContentsMargins(0, 0, 0, 0)
    device.addContentWidget = layout.addWidget
    device._ensure_channel_panel()
    window.resize(1100, 390)
    window.show()

    def flush():
        for _ in range(12):
            app.processEvents()

    def columns():
        return len({row["card"].mapTo(device.channelPanel, QPoint()).x() for row in device.channelPanelCards.values()})

    def save(name):
        window.grab().save(str(output / f"dmmr-{name}.png"))

    flush()
    if case == "grid":
        cards = [row["card"] for row in device.channelPanelCards.values()]
        spies = [QSignalSpy(row[key].toggled) for row in device.channelPanelCards.values()
                 for key in ("read_button", "display_box")]
        for width, count in ((370, 1), (600, 3), (1100, 5), (1650, 8), (370, 1), (1100, 5)):
            window.resize(width, 390)
            flush()
            assert columns() == count, (width, columns())
            assert cards == [row["card"] for row in device.channelPanelCards.values()]
            assert all(card.width() <= 210 for card in cards)
            assert all(not a.geometry().intersects(b.geometry()) for a in cards for b in cards if a is not b)
            save(str(width))
        # Labels can be arbitrarily long without widening cards/docks.
        device.channels[0].label = "A very long electrode label " * 20
        device._update_channel_panel()
        flush()
        assert columns() == 5
        assert all(not spy for spy in spies), "Resizing changed acquisition/display controls"
        assert exports == []
    elif case in {"labels", "parameter"}:
        rows = device.channelPanelCards
        editor = rows[0]["label_edit"]
        assert editor.text() == "Collecteur"
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "Faraday")
        # Acquisition refresh must not overwrite partially edited text.
        device._update_channel_panel()
        flush()
        assert editor.text() == "Faraday"
        assert device.channels[0].label == "Collecteur" and not exports
        QTest.keyClick(editor, Qt.Key.Key_Return)
        flush()
        assert device.channels[0].label == "Faraday"
        assert exports == [{"useDefaultFile": True}]
        assert device.channels[0].name == "DMMR_M00"  # No renaming data/equation identifiers.
        editor.selectAll()
        QApplication.clipboard().setText("Électrode µA — 50%")
        QTest.keyClick(editor, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        flush()
        assert device.channels[0].label == "Électrode µA — 50%"
        assert len(exports) == 2
        # Unchanged text and repeated focus changes must not write files.
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        flush()
        assert len(exports) == 2
        # Escape cancels an edit without discarding the last saved label.
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "discard this")
        QTest.keyClick(editor, Qt.Key.Key_Escape)
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        flush()
        assert device.channels[0].label == "Électrode µA — 50%" and len(exports) == 2
        assert editor.text() == "Électrode µA — 50%"
        # Clearing is valid and affects only the requested module.
        editor.setFocus()
        editor.selectAll()
        QTest.keyClick(editor, Qt.Key.Key_Backspace)
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        flush()
        assert device.channels[0].label == "" and device.channels[1].label == "Entrée"
        assert len(exports) == 3
        device.channels[0].label = "Collecteur"
        device._rebuild_channel_panel_cards()
        device._update_channel_panel()
        flush()
        assert device.channelPanelCards[0]["label_edit"].text() == "Collecteur"
        # Reloading a config can replace a channel without changing addresses.
        # An old draft must not overwrite its replacement, and the same card
        # must then allow editing the newly loaded channel.
        editor = device.channelPanelCards[0]["label_edit"]
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "Old unsaved draft")
        replacement = SimpleNamespace(**vars(device.channels[0]))
        replacement.label = "Imported label"
        device.channels[0] = replacement
        before = len(exports)
        QTest.keyClick(editor, Qt.Key.Key_Return)
        flush()
        assert replacement.label == "Imported label" and len(exports) == before
        device._update_channel_panel()
        assert editor.text() == "Imported label"
        editor.selectAll()
        QTest.keyClicks(editor, "After reload")
        QTest.keyClick(editor, Qt.Key.Key_Return)
        flush()
        assert replacement.label == "After reload" and len(exports) == before + 1
        save("labels")
    elif case == "save_failure":
        editor = device.channelPanelCards[0]["label_edit"]
        def fail(**kwargs):
            raise PermissionError("read-only config")
        device.exportConfiguration = fail
        editor.setFocus()
        editor.selectAll()
        QTest.keyClicks(editor, "Changed label")
        QTest.keyClick(editor, Qt.Key.Key_Tab)
        flush()
        for _ in range(5):
            device._update_channel_panel()
            flush()
        assert editor.text() == "Changed label" and editor.isModified()
        assert device.channels[0].label == "Collecteur"
        assert "Label not saved" in editor.toolTip()
        assert "#f87171" in editor.styleSheet()
        save("label-save-error")
        device.exportConfiguration = lambda **kwargs: exports.append(kwargs)
        editor.setFocus()
        QTest.keyClick(editor, Qt.Key.Key_Return)
        flush()
        assert device.channels[0].label == "Changed label"
        assert not editor.isModified() and len(exports) == 1
        assert "Label not saved" not in editor.toolTip() and "#f87171" not in editor.styleSheet()
    else:
        for state in ("ST_ON", "Disconnected", "Shutdown unconfirmed"):
            device.main_state = state
            device.channels[2].enabled = False
            device.controller.values[1] = -999.9e-12
            device._update_channel_panel()
            flush()
            assert columns() == 5
            for row in device.channelPanelCards.values():
                card = row["card"]
                assert card.width() <= 210
                for key in ("title", "current_value", "read_button", "display_box", "color_button"):
                    widget = row[key]
                    assert card.rect().contains(widget.mapTo(card, QPoint()))
                    assert card.rect().contains(widget.mapTo(card, widget.rect().bottomRight()))
                    if key != "color_button":  # Empty swatch has an intentionally fixed size.
                        assert widget.width() >= widget.minimumSizeHint().width(), (key, widget.width(), widget.minimumSizeHint().width())
                badge = row["state_badge"]
                if badge.isVisible():
                    assert badge.width() >= badge.sizeHint().width()
            save(state.replace(" ", "-"))
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
