"""Real Qt checks: plugin panels must not pin the shared Explorer dock area.

Each probe runs in isolation from the suite's Qt stubs. No hardware or user
settings are loaded; only ESIBD's base classes are replaced with test doubles.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = (
    "ampr_a", "ampr_b", "amx_a", "amx_b", "amx_hd", "dmmr", "esi",
    "psu_a", "psu_b", "psu_c", "psu_d", "psu_e",
)


@pytest.mark.parametrize("scale", ["1", "1.5"])
@pytest.mark.parametrize("folder", PLUGINS)
def test_plugin_dock_can_shrink_and_controls_remain_reachable(folder, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", folder, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True, text=True, timeout=45,
    )
    if result.returncode == 77:
        pytest.skip("Real PyQt6 unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(folder, output):
    try:
        from PyQt6.QtCore import QPoint, QPointF, Qt
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtTest import QSignalSpy
        from PyQt6.QtWidgets import (
            QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDockWidget, QDoubleSpinBox,
            QLabel, QMainWindow, QPushButton, QScrollArea, QSizePolicy,
            QToolBar, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
        )
    except ImportError:
        return 77

    app = QApplication([])
    family = folder if folder in ("dmmr", "esi", "amx_hd") else folder.rsplit("_", 1)[0]
    stub_file = {
        "ampr": "test_ampr_plugin_channel_sync",
        "amx_hd": "test_amx_hd_plugin_packaging",
    }.get(family, f"test_{family}_plugin_behavior")
    importlib.import_module(stub_file)._install_esibd_stubs()
    spec = importlib.util.spec_from_file_location(
        "panel_resize_probe", ROOT / folder / f"{family}_plugin.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = getattr(module, {"amx_hd": "AMXHDDevice"}.get(family, f"{family.upper()}Device"))
    device = cls.__new__(cls)
    device.loading = False
    device.main_state = "Disconnected"
    device.device_enabled_state = "OFF"
    device.controller = None
    device.isOn = lambda: False
    device.frequency_khz = 2.0
    device.print = lambda *args, **kwargs: None
    device.channelType = object
    device.channels = [
        SimpleNamespace(
            name=f"CH{i}", real=True, enabled=True, active=True, display=True,
            value=0.0, color="#3182ce", id=i,
            module_address=lambda i=i: i,
            channel_number=lambda i=i: i,
            pulser_number=lambda i=i: i,
            is_heat_channel=lambda: False,
        )
        for i in range(8 if family == "dmmr" else 4 if family == "amx" else 2)
    ]
    device.getChannels = lambda: device.channels

    # Same widget hierarchy as Plugin.initGUI/addContentWidget and DockWidget.
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    device.addContentWidget = layout.addWidget
    device.tree = QTreeWidget()
    device.tree.setColumnCount(4)
    item = QTreeWidgetItem(device.tree, ["Channel", "Value", "Monitor", "Display"])
    wide_cell = QLabel("A deliberately wide channel setting")
    wide_cell.setMinimumWidth(650)
    device.tree.setItemWidget(item, 0, wide_cell)
    layout.addWidget(device.tree)
    device.titleBar = QToolBar()
    device.titleBarLabel = QLabel(device.name)
    device.titleBar.addWidget(device.titleBarLabel)
    spacer = QWidget()
    spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
    device.stretchAction = device.titleBar.addWidget(spacer)
    if hasattr(device, "_ensure_status_widgets"):
        device._ensure_status_widgets()
    if family in ("amx", "amx_hd"):
        device._ensure_config_controls()
    elif family == "psu":
        device._ensure_config_selectors()

    cards = []
    panel = None
    if family in ("dmmr", "psu"):
        device._ensure_channel_panel()
        panel = device.channelPanel
        cards = [entry["card"] for entry in device.channelPanelCards.values()]
    elif family == "amx":
        # Output rows are the default view; exercise the existing pulser controls
        # in Advanced as well (default/narrow output view has dedicated Qt tests).
        device.advancedAction = SimpleNamespace(state=True)
        device._ensure_operator_panel()
        panel = device.amxPanel
        cards = [entry["card"] for entry in device.amxPanelCards.values()]
    elif family == "esi":
        device._ensure_operator_panel()
        panel = device.esiPanel
        cards = [entry["card"] for entry in device.esiHVCards.values()]
    if family in ("psu", "dmmr"):
        device.tree.hide()

    window = QMainWindow()
    window.resize(1300, 800)
    control = QDockWidget(device.name, window)
    control.setWidget(host)
    control.setTitleBarWidget(device.titleBar)
    graph = QDockWidget("Live display", window)
    graph.setWidget(QLabel("Plot area"))
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, control)
    window.splitDockWidget(control, graph, Qt.Orientation.Horizontal)
    window.show()

    def flush():
        for _ in range(12):
            app.processEvents()

    def resize(width):
        window.resizeDocks([control, graph], [width, 1300 - width], Qt.Orientation.Horizontal)
        flush()

    def columns():
        return len({card.mapTo(panel, QPoint()).x() for card in cards})

    flush()
    # Resizing must not recreate controls or emit commands, even for spinboxes.
    spies = []
    for widget in host.findChildren(QWidget):
        if isinstance(widget, QDoubleSpinBox):
            spies.append(QSignalSpy(widget.valueChanged))
        elif isinstance(widget, QComboBox):
            spies.append(QSignalSpy(widget.currentIndexChanged))
        elif isinstance(widget, (QCheckBox, QPushButton)):
            spies.append(QSignalSpy(widget.toggled))

    resize(900)
    if cards:
        assert columns() == (4 if family == "dmmr" else 2), (folder, columns())
    window.grab().save(str(output / f"{folder}-wide.png"))
    if family == "dmmr":
        resize(530)
        assert columns() == 2
    resize(370)
    assert control.width() <= 380, (folder, control.width(), control.minimumSizeHint().width())
    if cards:
        assert columns() == 1, (folder, columns())
        scrolls = host.findChildren(QScrollArea)
        assert len(scrolls) == 1
        scroll = scrolls[0]
        assert scroll.widget() is panel and scroll.widgetResizable()
        # At one-card width, scrolling sideways should not be necessary.
        assert scroll.horizontalScrollBar().maximum() == 0, (folder, panel.minimumSizeHint().width())
        window.grab().save(str(output / f"{folder}-narrow.png"))
        for card in cards:
            scroll.ensureWidgetVisible(card, 0, 0)
            flush()
            center = card.mapTo(scroll.viewport(), card.rect().center())
            assert scroll.viewport().rect().contains(center), (folder, center)
            # No overlap with another card in the scrollable virtual content.
            for other in cards:
                if other is not card:
                    assert not card.geometry().intersects(other.geometry())
        if family == "psu":
            device.channelPanelAdvancedSection.show()
            flush()
            assert control.width() <= 380
            for widget in (device.manualPanelSaveNameEdit, device.manualPanelSaveButton):
                scroll.ensureWidgetVisible(widget, 0, 0)
                flush()
                assert scroll.viewport().rect().contains(widget.mapTo(scroll.viewport(), widget.rect().center()))
            device.channelPanelAdvancedSection.hide()
        # Height as well as width must be reducible; scrollbars keep all content.
        window.resize(1300, 240)
        flush()
        assert window.height() <= 240, (folder, window.minimumSizeHint().height())
        assert scroll.verticalScrollBar().maximum() > 0
        # Scrolling over an editor must scroll, NOT change an HV setpoint/range.
        for editor in panel.findChildren(QWidget):
            if not isinstance(editor, (QAbstractSpinBox, QComboBox)):
                continue
            if editor.isHidden():
                continue
            old_enabled = editor.isEnabled()
            editor.setEnabled(True)
            editor.blockSignals(True)
            get_value = editor.value if isinstance(editor, QAbstractSpinBox) else editor.currentIndex
            set_value = editor.setValue if isinstance(editor, QAbstractSpinBox) else editor.setCurrentIndex
            original = get_value()
            set_value(1)
            before = get_value()
            scroll.ensureWidgetVisible(editor, 0, 0)
            editor.setFocus()
            bar = scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
            old_scroll = bar.value()
            wheel = QWheelEvent(QPointF(editor.rect().center()),
                                QPointF(editor.mapToGlobal(editor.rect().center())),
                                QPoint(0, 0), QPoint(0, 120), Qt.MouseButton.NoButton,
                                Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            app.sendEvent(editor, wheel)
            flush()
            assert get_value() == before, (folder, type(editor).__name__, before, get_value())
            assert bar.value() < old_scroll, (folder, type(editor).__name__, "wheel did not scroll")
            set_value(original)
            editor.blockSignals(False)
            editor.setEnabled(old_enabled)
        window.resize(1300, 800)
        for _ in range(3):
            resize(900)
            assert columns() == (4 if family == "dmmr" else 2)
            resize(370)
            assert columns() == 1
        assert all(len(spy) == 0 for spy in spies)

    resize(160)
    assert control.width() <= 200, (folder, control.width())
    assert graph.width() >= 1090
    # An inactive plugin tab must not keep its old wide minimum either.
    alternative = QDockWidget("Other plugin", window)
    alternative.setWidget(QLabel("Compact panel"))
    window.tabifyDockWidget(control, alternative)
    alternative.show()
    alternative.raise_()
    flush()
    window.resizeDocks([alternative, graph], [160, 1140], Qt.Orientation.Horizontal)
    flush()
    assert alternative.width() <= 200, (folder, alternative.width())
    assert graph.width() >= 1090
    window.grab().save(str(output / f"{folder}-inactive.png"))
    window.resize(1300, 160)
    flush()
    assert window.height() <= 160, (folder, window.minimumSizeHint().height())
    window.resize(1300, 800)
    print(f"{folder}: dock {alternative.width()} px, plot {graph.width()} px; controls preserved")

    if family == "dmmr":
        control.raise_()
        for count in (0, 1, 2, 3, 8):
            device.channels = device.channels[:count] if count == 0 else [
                SimpleNamespace(name=f"M{i}", real=True, enabled=True, display=True,
                                color="#3182ce", module_address=lambda i=i: i)
                for i in range(count)
            ]
            device._ensure_channel_panel()
            flush()
            cards[:] = [entry["card"] for entry in device.channelPanelCards.values()]
            assert len(cards) == count
            resize(370)
            if cards:
                assert columns() == 1
            resize(900)
            if cards:
                assert columns() == min(count, 4)
        assert len(host.findChildren(QScrollArea)) == 1
    window.close()
    return 0


if __name__ == "__main__":
    assert sys.argv[1] == "--probe"
    destination = Path(sys.argv[3])
    destination.mkdir(parents=True, exist_ok=True)
    raise SystemExit(probe(sys.argv[2], destination))
