"""Real Qt output view: live routing, Advanced, stale data and dock resizing."""
import os
from pathlib import Path
import subprocess
import sys
from threading import Thread
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("folder", ("amx_a", "amx_b"))
@pytest.mark.parametrize("scale", ("1", "1.5"))
def test_output_view_in_real_qt(folder, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(folder, output):
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QDockWidget, QHBoxLayout,
            QLabel, QMainWindow, QPushButton, QScrollArea, QTreeWidget, QVBoxLayout, QWidget)
    except ImportError:
        return 77
    from test_amx_config_load import make_case
    from test_amx_outputs import config79_snapshot
    app = QApplication([])
    app.setStyle("Fusion")
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, "#20232b"), (QPalette.ColorRole.Base, "#20232b"),
                        (QPalette.ColorRole.AlternateBase, "#292d36"), (QPalette.ColorRole.Button, "#343944"),
                        (QPalette.ColorRole.WindowText, "#e2e8f0"), (QPalette.ColorRole.Text, "#e2e8f0"),
                        (QPalette.ColorRole.ButtonText, "#e2e8f0")):
        palette.setColor(role, QColor(color))
    app.setPalette(palette)
    module, parent, controller = make_case(folder)
    host = QWidget()
    layout = QVBoxLayout(host)
    parent.addContentWidget = layout.addWidget
    parent.tree = QTreeWidget()
    parent.tree.setHeaderLabels(["Pulser", "Width (µs)", "Equation"])
    layout.addWidget(parent.tree)
    parent.advancedAction = SimpleNamespace(state=False)
    # Host-framework visibility hook; the AMX override and Qt widgets are real.
    module.Device.toggleAdvanced = lambda self, advanced: setattr(self.advancedAction, "state", advanced)
    parent._update_channel_column_visibility = lambda: None
    for channel in parent.channels:
        channel.setHidden = lambda _: None
    controller._apply_snapshot(config79_snapshot())
    parent.loaded_config_text = "79:500kHz->SwitchSym+DIO0,Osc->DIO1 [memory]"
    parent.frequency_khz = 500.
    toolbar = QHBoxLayout()
    toolbar.addWidget(QLabel(parent.name))
    toolbar.addWidget(QLabel("Oscillator"))
    parent.frequencyWidget = parent._create_frequency_widget()
    parent.frequencyWidget.setValue(500.)
    toolbar.addWidget(parent.frequencyWidget)
    toolbar.addStretch()
    advanced = QPushButton("Advanced")
    advanced.setCheckable(True)
    advanced.toggled.connect(parent.toggleAdvanced)
    toolbar.addWidget(advanced)
    layout.insertLayout(0, toolbar)
    parent._ensure_operator_panel()
    window = QMainWindow()
    window.resize(1260, 700)
    dock = QDockWidget(parent.name, window)
    dock.setWidget(host)
    graph = QDockWidget("Live display", window)
    graph.setWidget(QLabel("Plot area"))
    window.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
    window.splitDockWidget(dock, graph, Qt.Orientation.Horizontal)
    window.show()
    window.activateWindow()
    QTest.qWait(50)
    window.resizeDocks([dock, graph], [880, 380], Qt.Orientation.Horizontal)
    QTest.qWait(50)
    table = parent.amxOutputTable
    text = lambda row, col: table.item(row, col).text()
    assert table.isVisible() and table.rowCount() == 4
    assert not parent.amxPulserControls.isVisible()
    assert not parent.tree.isVisible()
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert all(text(i, 2) == "500 kHz" and text(i, 4) == "1 / 1" for i in range(4))
    assert [text(i, 5) for i in range(4)] == ["Reference", "Opposite to CH0", "Same as CH0", "Opposite to CH0"]
    assert "Voltages from linked PSUs" in " ".join(label.text() for label in host.findChildren(QLabel) if label.isVisible())
    assert all(text(i, 6) == "—" and text(i, 3) == "Vneg ↔ Vpos" for i in range(4))
    assert "trilevel" in table.toolTip()
    assert table.verticalScrollBar().maximum() == 0
    assert table.horizontalScrollBar().maximum() == 0
    window.grab().save(str(output / f"{folder}-outputs.png"))

    # Real PSU controller publication -> public plugin API -> actual AMX widgets.
    # Only the device transport is simulated. Refresh must not need AMX polling.
    from test_psu_channels import make_psu, snapshot
    from test_amx_psu_links import register_sources
    psu_module, psu, psu_controller = make_psu("psu_a")
    other_module, other, other_controller = make_psu("psu_e")
    clock = SimpleNamespace(now=100.)
    for source_module in (psu_module, other_module):
        source_module.time = SimpleNamespace(monotonic=lambda: clock.now)
    psu_controller._apply_snapshot(snapshot(123.4, 120.5), refreshed_at=clock.now)
    other_controller._apply_snapshot(snapshot(50., 45.), refreshed_at=clock.now)
    register_sources(parent, parent, psu, other)
    from test_amx_psu_links import install_real_settings
    settings_window = QWidget()
    settings_window.setWindowTitle("AMX PSU settings")
    settings = install_real_settings(module, parent, QVBoxLayout(settings_window))
    positive_pair = settings.settings[f"{parent.name}/{parent.PSU_CH01}"]
    negative_pair = settings.settings[f"{parent.name}/{parent.PSU_CH23}"]
    positive_pair.combo.setCurrentText(psu.name)
    negative_pair.combo.setCurrentText(other.name)
    assert parent.psu_ch01 == psu.name and parent.psu_ch23 == other.name
    assert text(0, 3) == "-120.5 V ↔ +123.4 V", "Settings change was not immediate"
    saved = {key: parameter.value for key, parameter in settings.settings.items()}
    parent.psu_ch01 = parent.psu_ch23 = "None"
    for key, value in saved.items():
        settings.settings[key].value = value
    assert parent.psu_ch01 == psu.name and parent.psu_ch23 == other.name
    QTest.qWait(650)
    assert [text(i, 6) for i in range(4)] == ["PSU_A", "PSU_A", "PSU_E", "PSU_E"]
    assert [text(i, 3) for i in range(4)] == ["-120.5 V ↔ +123.4 V"] * 2 + ["-45 V ↔ +50 V"] * 2
    assert "relative to its reference" in table.item(0, 3).toolTip()
    assert "external offset not included" in table.item(0, 3).toolTip()
    assert all(row["levels"] == "Vneg ↔ Vpos" for row in controller.output_rows)
    window.grab().save(str(output / f"{folder}-linked-psu.png"))
    clock.now += 2.1
    psu_controller.updateValues()
    other_controller.updateValues()
    QTest.qWait(650)
    assert all(text(i, 3) == "Vneg ↔ Vpos" for i in range(4)), "Old PSU readings remained visible"
    assert "expired" in table.item(0, 3).toolTip()
    psu_controller._apply_snapshot(snapshot(0., 120.5, (True, False)), refreshed_at=clock.now)
    other_controller._apply_snapshot(snapshot(50., 45.), refreshed_at=clock.now)
    QTest.qWait(650)
    assert text(0, 3) == "Vneg ↔ 0 V", "Disabled rail became zero or used its previous value"
    assert text(2, 3) == "-45 V ↔ +50 V"
    parent.psu_ch01 = other.name  # Remapping does not reconnect or control a PSU.
    QTest.qWait(650)
    assert text(0, 3) == text(2, 3) == "-45 V ↔ +50 V"
    other_controller._cancel_output_commands()
    QTest.qWait(650)
    assert all(text(i, 3) == "Vneg ↔ Vpos" for i in range(4))
    parent.psu_ch01, parent.psu_ch23 = "None", "None"
    parent._update_output_table()

    # Unknown custom routing arrives from the real worker-to-Qt callback.
    external = config79_snapshot()
    external["pulsers"][0]["trigger_config"] = 3
    thread = Thread(target=controller._apply_snapshot, args=(external,))
    thread.start(); thread.join()
    QTest.qWait(50)
    assert all(text(i, 1) == "External" and text(i, 2) == "Unknown" for i in range(4))
    parent.frequency_khz = 123.
    parent._update_operator_panel()
    assert text(0, 2) == "Unknown", "GUI oscillator setpoint was substituted for output frequency"
    controller._apply_snapshot(config79_snapshot())

    # Merely viewing outputs / Advanced must never cause hardware writes.
    controller.device.calls.clear()
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    assert parent.tree.isVisible() and parent.amxPulserControls.isVisible()
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    assert not parent.tree.isVisible() and not parent.amxPulserControls.isVisible()
    assert controller.device.timing_writes == []
    window.resizeDocks([dock, graph], [370, 890], Qt.Orientation.Horizontal)
    QTest.qWait(50)
    assert dock.width() <= 380, (dock.width(), dock.minimumSizeHint().width())
    assert table.horizontalScrollBar().maximum() > 0
    assert table.verticalScrollBar().maximum() == 0
    table.horizontalScrollBar().setValue(table.horizontalScrollBar().maximum())
    assert table.visualItemRect(table.item(3, table.columnCount() - 1)).intersects(table.viewport().rect())
    window.resize(1260, 260)
    QTest.qWait(50)
    scroll = host.findChild(QScrollArea)
    assert scroll is not None and scroll.widgetResizable()
    window.grab().save(str(output / f"{folder}-outputs-narrow.png"))
    # An inactive dock must not force another tab wide.
    window.tabifyDockWidget(graph, dock)
    graph.raise_()
    QTest.qWait(50)
    assert window.minimumSizeHint().width() < 600

    for state in ("OFF", "Standby", "Shutdown unconfirmed"):
        parent.main_state = state
        parent._update_operator_panel()
        assert text(0, 1) == state and text(0, 2) == "—" and text(0, 3) == "Unknown"
    parent.main_state = "STATE_ON"
    controller.transitioning = True
    parent._update_operator_panel()
    assert text(0, 1) == "Updating" and text(0, 2) == "—"
    controller.transitioning = False
    controller.output_rows = None
    parent._update_operator_panel()
    assert text(0, 1) == "Awaiting readback"
    controller.initialized = False
    parent._update_operator_panel()
    assert text(0, 1) == "Disconnected"
    assert controller.device.timing_writes == []
    settings_window.close()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
