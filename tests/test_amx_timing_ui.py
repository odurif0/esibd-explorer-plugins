"""Real Qt timing rows: width and duty are one setting, Keep is per pulser, unused rows are Advanced."""
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", ("amx_a", "amx_b", "amx_hd"))
def test_timing_rows_in_real_qt(folder, tmp_path):
    run_probe(__file__, folder, tmp_path, timeout=30)


def probe(folder, output):
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QTreeWidget, QVBoxLayout, QWidget
    except ImportError:
        return 77
    from support.amx import config79_snapshot, config273, make_case
    hd = folder == "amx_hd"
    app = QApplication([])
    module, parent, controller = make_case(folder)
    module._invoke_gui_callback = lambda callback: callback()
    host = QWidget()
    layout = QVBoxLayout(host)
    parent.addContentWidget = layout.addWidget
    parent.tree = QTreeWidget()
    layout.addWidget(parent.tree)
    parent.advancedAction = SimpleNamespace(state=False)
    parent._update_channel_column_visibility = lambda: None
    for channel in parent.channels:
        channel.setHidden = lambda _: None
    controller.loadOperatingConfigNow()  # 500 kHz, P0 high 100 of 200 ticks
    if hd:
        state, routing = config273()
        state["oscillator"]["period"] = routing["oscillator_periods"][0] = 198
        state["pulsers"][0].update(width_ticks=98)
        controller.hd_routing = routing
        controller._apply_snapshot(state)
    else:
        controller._apply_snapshot(config79_snapshot())
    parent._ensure_operator_panel()
    host.resize(1150, 1300)
    host.show()
    QTest.qWait(50)
    parent._update_operator_panel()
    card = parent.amxTimingRows[0]
    timing = parent.amxTimingTable
    assert card["drives"].text() == "CH0, CH2 · inverted CH1, CH3", card["drives"].text()
    assert card["width"].value() == 1. and card["duty"].value() == 50.
    assert "→ 500 kHz (200 × 10 ns)" in parent.amxPanelFrequencyValue.text()
    # Only P0 is used: the other rows and the register column wait in Advanced.
    assert [timing.isRowHidden(row) for row in range(timing.rowCount())] == [False] + [True] * (timing.rowCount() - 1)
    assert timing.isColumnHidden(timing.columnCount() - 1)
    assert timing.horizontalScrollBar().maximum() == 0

    # A duty edit is a width edit on the 10 ns grid.
    duty = card["duty"]
    duty.setFocus()
    duty.selectAll()
    QTest.keyClicks(duty, "25")
    QTest.keyClick(duty, Qt.Key.Key_Return)
    QTest.qWait(50)
    assert parent.channels[0].value == .5, parent.channels[0].value
    parent.tree.setFocus()
    parent._update_operator_panel()
    assert card["width"].value() == .5

    # Keep is stored per channel; a width-keeping pulser ignores frequency edits.
    card["keep"].setCurrentIndex(card["keep"].findData("width"))
    assert parent.channels[0].keep == "width"
    parent.frequency_khz = 250.
    parent._follow_timing_basis()
    parent._update_operator_panel()
    assert parent.channels[0].value == .5
    assert "timing edited since load" in parent.amxPanelSignalValue.text()

    parent.advancedAction.state = True
    parent._update_channel_table_visibility()
    assert not timing.isColumnHidden(timing.columnCount() - 1)
    assert not any(timing.isRowHidden(row) for row in range(timing.rowCount()))
    assert card["status"].text() in ("REGISTERS APPLIED", "REGISTER MISMATCH", "WAITING READBACK"), card["status"].text()
    host.grab().save(str(output / f"{folder}-timing.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
