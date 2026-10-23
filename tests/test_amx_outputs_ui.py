"""Real Qt output view: live routing, Advanced, stale data and dock resizing."""
from pathlib import Path
import sys
from threading import Thread
from types import SimpleNamespace

import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", ("amx_a", "amx_b", "amx_hd"))
@pytest.mark.parametrize("scale", ("1", "1.5"))
def test_output_view_in_real_qt(folder, scale, tmp_path):
    run_probe(__file__, folder, tmp_path, timeout=30, scale=scale)


def probe(folder, output):
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QColor, QPalette
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import (QAbstractItemView, QApplication, QDockWidget, QHBoxLayout,
            QLabel, QMainWindow, QPushButton, QScrollArea, QTreeWidget, QVBoxLayout, QWidget)
    except ImportError:
        return 77
    from support.amx import make_case
    from support.amx import config79_snapshot
    from support.amx import config273
    hd = folder == "amx_hd"
    # Both references drive all four outputs from one timer: 500 kHz (AMX 4ED
    # Config 80) or 10 kHz (CGC 4EDH Configuration273 "10kHz->SwitchSym").
    frequency, dwell, oscillator_khz = ("10 kHz", "50 / 50", 10.) if hd else ("500 kHz", "1 / 1", 500.)

    def reference(external=False):
        if hd:
            state, routing = config273()
            if external:
                routing["timer_triggers"][0] = 56  # Din0
            controller.hd_routing = routing
            return state
        state = config79_snapshot()
        if external:
            state["pulsers"][0]["trigger_config"] = 3
        return state
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
    controller._apply_snapshot(reference())
    for channel in parent.channels[1:]:
        channel.enabled = False  # As a config load leaves the unused pulsers.
    parent.loaded_config_text = "79:500kHz->SwitchSym+DIO0,Osc->DIO1 [memory]"
    parent.frequency_khz = oscillator_khz
    toolbar = QHBoxLayout()
    toolbar.addWidget(QLabel(parent.name))
    toolbar.addWidget(QLabel("Oscillator"))
    parent.frequencyWidget = parent._create_frequency_widget()
    parent.frequencyWidget.setValue(oscillator_khz)
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
    column = {key: index for index, (key, _) in enumerate(module._AMX_OUTPUT_COLUMNS)}
    text = lambda row, key: table.item(row, column[key]).text()
    cell = lambda row, key: table.item(row, column[key])
    assert table.isVisible() and table.rowCount() == 4
    # Operator view: outputs and one timing row per used pulser (P0 drives all four).
    timing = parent.amxTimingTable
    status_column = len(module._AMX_TIMING_COLUMNS) - 1
    assert timing.isVisible() and timing.isColumnHidden(status_column)
    assert [timing.isRowHidden(row) for row in range(timing.rowCount())] == [False] + [True] * (timing.rowCount() - 1)
    assert parent.amxTimingHiddenNote.isVisible()
    assert not parent.amxWaveformView.isVisible() and not parent.amxPanelNotice.isVisible()
    assert not parent.tree.isVisible()
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    assert all(text(i, "frequency") == frequency and text(i, "dwell") == dwell for i in range(4))
    assert [text(i, "relation") for i in range(4)] == ["Reference", "Opposite to CH0", "Same as CH0", "Opposite to CH0"]
    assert "Voltages from linked PSUs" in table.toolTip()
    assert all(text(i, "psu") == "—" and text(i, "levels") == "Vneg ↔ Vpos" for i in range(4))
    # Without a linked PSU those two columns only repeat rail names.
    assert all(table.isColumnHidden(column[key]) for key in ("levels", "psu"))
    assert "Hi-Z never implies a discharged output" in table.toolTip()
    assert all(table.horizontalHeaderItem(c).toolTip() for c in range(table.columnCount()))
    if hd:
        notice = parent.amxPanelClockNotice
        assert not notice.isVisible()
        controller.hd_routing["clock_sources"][0] = 4  # PLL0
        parent._update_operator_panel()
        assert notice.isVisible() and "PLL0" in notice.text()
        controller.hd_routing["clock_sources"][0] = 3
        parent._update_operator_panel()
        assert not notice.isVisible()
    assert table.verticalScrollBar().maximum() == 0
    assert table.horizontalScrollBar().maximum() == 0
    # Inferred outputs keep plain text; the sketch (Advanced) draws the same four square waves.
    assert all(cell(i, "state").data(Qt.ItemDataRole.ForegroundRole) is None for i in range(4))
    view = parent.amxWaveformView
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    QTest.qWait(50)
    assert view.isVisible() and view.message == ""
    assert not timing.isColumnHidden(status_column) and not any(timing.isRowHidden(row) for row in range(4))
    assert [trace["kind"] for trace in view.model["traces"]] == ["wave"] * 4
    trace_color = QColor(module._amx_panel_color("trace", True))
    image = view.grab().toImage()
    drawn = sum(image.pixelColor(x, y) == trace_color
                for x in range(0, image.width(), 2) for y in range(image.height()))
    assert drawn > 200, drawn
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    QTest.qWait(50)
    assert not view.isVisible() and timing.isRowHidden(1)
    # A selected config that is not in controller memory asks for Load now.
    pending = parent.amxPanelPendingValue
    assert not pending.isVisible()
    controller._loaded_config_index = 0
    parent._update_operator_panel()
    assert pending.isVisible() and "press Load now" in pending.text()
    controller._loaded_config_index = parent.operating_config
    parent._update_operator_panel()
    assert not pending.isVisible()
    QTest.qWait(50)  # Let the layout close the hidden notice before the screenshot.
    window.grab().save(str(output / f"{folder}-outputs.png"))

    # Explorer's light theme: the panel follows the application palette, text stays dark.
    def luminance_range(widget):
        image = widget.grab().toImage()
        values = [image.pixelColor(x, y).lightness() for x in range(image.width()) for y in range(image.height())]
        return min(values), max(values)
    dark_palette = app.palette()
    light = QPalette(dark_palette)
    for role, color in ((QPalette.ColorRole.Window, "#ffffff"), (QPalette.ColorRole.Base, "#ffffff"),
                        (QPalette.ColorRole.WindowText, "#000000"), (QPalette.ColorRole.Text, "#000000")):
        light.setColor(role, QColor(color))
    app.setPalette(light)
    parent.updateTheme()
    darkest, lightest = luminance_range(parent.amxPanelSignalValue)
    assert lightest > 200 and darkest < 80, (darkest, lightest)
    assert view.dark is False
    window.grab().save(str(output / f"{folder}-outputs-light.png"))
    app.setPalette(dark_palette)
    parent.updateTheme()
    assert view.dark is True

    # Real PSU controller publication -> public plugin API -> actual AMX widgets.
    # Only the device transport is simulated. Refresh must not need AMX polling.
    from support.psu import make_psu, snapshot
    from support.amx import register_sources
    psu_module, psu, psu_controller = make_psu("psu_a")
    other_module, other, other_controller = make_psu("psu_e")
    clock = SimpleNamespace(now=100.)
    for source_module in (psu_module, other_module):
        source_module.time = SimpleNamespace(monotonic=lambda: clock.now)
    psu_controller._apply_snapshot(snapshot(123.4, 120.5), refreshed_at=clock.now)
    other_controller._apply_snapshot(snapshot(50., 45.), refreshed_at=clock.now)
    register_sources(parent, parent, psu, other)
    from support.amx import install_real_settings
    settings_window = QWidget()
    settings_window.setWindowTitle("AMX PSU settings")
    settings = install_real_settings(module, parent, QVBoxLayout(settings_window))
    positive_pair = settings.settings[f"{parent.name}/{parent.PSU_CH01}"]
    negative_pair = settings.settings[f"{parent.name}/{parent.PSU_CH23}"]
    positive_pair.combo.setCurrentText(psu.name)
    negative_pair.combo.setCurrentText(other.name)
    assert parent.psu_ch01 == psu.name and parent.psu_ch23 == other.name
    assert text(0, "levels") == "-120.5 V ↔ +123.4 V", "Settings change was not immediate"
    assert not table.isColumnHidden(column["levels"]) and not table.isColumnHidden(column["psu"])
    saved = {key: parameter.value for key, parameter in settings.settings.items()}
    parent.psu_ch01 = parent.psu_ch23 = "None"
    for key, value in saved.items():
        settings.settings[key].value = value
    assert parent.psu_ch01 == psu.name and parent.psu_ch23 == other.name
    QTest.qWait(650)
    assert [text(i, "psu") for i in range(4)] == ["PSU_A", "PSU_A", "PSU_E", "PSU_E"]
    assert [text(i, "levels") for i in range(4)] == ["-120.5 V ↔ +123.4 V"] * 2 + ["-45 V ↔ +50 V"] * 2
    assert "relative to its reference" in cell(0, "levels").toolTip()
    assert "external offset not included" in cell(0, "levels").toolTip()
    assert all(row["levels"] == "Vneg ↔ Vpos" for row in controller.output_rows)
    window.grab().save(str(output / f"{folder}-linked-psu.png"))
    clock.now += 2.1
    psu_controller.updateValues()
    other_controller.updateValues()
    QTest.qWait(650)
    assert all(text(i, "levels") == "Vneg ↔ Vpos" for i in range(4)), "Old PSU readings remained visible"
    assert "expired" in cell(0, "levels").toolTip()
    psu_controller._apply_snapshot(snapshot(0., 120.5, (True, False)), refreshed_at=clock.now)
    other_controller._apply_snapshot(snapshot(50., 45.), refreshed_at=clock.now)
    QTest.qWait(650)
    assert text(0, "levels") == "Vneg ↔ 0 V", "Disabled rail became zero or used its previous value"
    assert text(2, "levels") == "-45 V ↔ +50 V"
    parent.psu_ch01 = other.name  # Remapping does not reconnect or control a PSU.
    QTest.qWait(650)
    assert text(0, "levels") == text(2, "levels") == "-45 V ↔ +50 V"
    other_controller._cancel_output_commands()
    QTest.qWait(650)
    assert all(text(i, "levels") == "Vneg ↔ Vpos" for i in range(4))
    parent.psu_ch01, parent.psu_ch23 = "None", "None"
    parent._update_output_table()

    # Unknown custom routing arrives from the real worker-to-Qt callback.
    external = reference(external=True)
    thread = Thread(target=controller._apply_snapshot, args=(external,))
    thread.start(); thread.join()
    QTest.qWait(50)
    assert all(text(i, "state") == "External" and text(i, "frequency") == "Unknown" for i in range(4))
    warning = QColor(module._amx_panel_color("warning", True))
    assert all(cell(i, "state").data(Qt.ItemDataRole.ForegroundRole) == warning for i in range(4))
    assert [trace["kind"] for trace in parent.amxWaveformView.model["traces"]] == ["none"] * 4
    parent.frequency_khz = 123.
    parent._update_operator_panel()
    assert text(0, "frequency") == "Unknown", "GUI oscillator setpoint was substituted for output frequency"
    controller._apply_snapshot(reference())

    # Merely viewing outputs / Advanced must never cause hardware writes.
    controller.device.calls.clear()
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    assert parent.tree.isVisible() and not timing.isColumnHidden(status_column)
    QTest.mouseClick(advanced, Qt.MouseButton.LeftButton)
    assert not parent.tree.isVisible() and timing.isColumnHidden(status_column)
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
        assert text(0, "state") == state and text(0, "frequency") == "—" and text(0, "levels") == "Unknown"
    parent.main_state = "STATE_ON"
    controller.transitioning = True
    parent._update_operator_panel()
    assert text(0, "state") == "Updating" and text(0, "frequency") == "—"
    controller.transitioning = False
    controller.output_rows = None
    parent._update_operator_panel()
    assert text(0, "state") == "Awaiting readback"
    controller.initialized = False
    parent._update_operator_panel()
    assert text(0, "state") == "Disconnected"
    assert parent.amxWaveformView.message.startswith("No expected waveform: Disconnected")
    assert controller.device.timing_writes == []
    settings_window.close()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
