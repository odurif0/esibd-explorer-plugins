"""Real Qt: "Link CH1 to CH0" sends CH0's Vset and Ilim to both channels (user request 2026-10-26)
and sets both outputs and ranges from either channel's ON box or Range (2026-10-28)."""
from __future__ import annotations

from pathlib import Path
import sys
import time
from threading import Lock

import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", [f"psu_{suffix}" for suffix in "abcde"])
def test_linked_channels_follow_ch0_in_real_qt(folder, tmp_path):
    run_probe(__file__, folder, tmp_path, timeout=35)


def probe(folder, output):
    try:
        from PyQt6.QtCore import QPoint, Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget
    except ImportError:
        return 77
    from support.psu import make_psu
    from support.psu_fakes import StatefulPSU

    app = QApplication([])
    module, parent, controller = make_psu(folder)
    controller._sync_status_to_gui = module.PSUController._sync_status_to_gui.__get__(controller)
    controller.lock = Lock()
    controller.errorCount = 0
    messages = []
    controller.print = parent.print = lambda text, **kw: messages.append(text)
    parent.main_state = "ST_ON"
    parent.startup_timeout_s, parent.poll_timeout_s = 1., .1
    parent.interlock_monitoring = True
    parent.link_channels = False
    parent._schedule_delayed_refresh = lambda *_: None
    window = QWidget()
    layout = QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget
    parent.statusBadgeLabel, parent.statusSummaryLabel = QLabel(), QLabel()

    class Hardware(StatefulPSU):
        def collect_housekeeping(self, **kwargs):
            return {
                "main_state": {"name": "ST_ON" if self.enabled else "ST_OFF"},
                "device_enabled": self.enabled, "output_enabled": self.outputs,
                "psu_state": {"psu_enabled_actual": self.enabled, "interlock_active": True},
                "channels": [dict(channel=ch, enabled=self.outputs[ch],
                    voltage={"set_v": self.voltages[ch], "measured_v": self.get_channel_measured_voltage(ch)},
                    current={"set_a": self.currents[ch], "measured_a": .0001},
                    full_range={"enabled": self.ranges[ch], "supported": True}) for ch in (0, 1)],
            }

        def get_channel_measurements(self, ch, **kwargs):
            return self.get_channel_measured_voltage(ch), .0001, 15.

    hw = controller.device = Hardware()
    hw.enabled, hw.outputs, hw.interlocks = True, (True, True), (True, True)
    hw.voltages, hw.currents = {0: 100., 1: 137.}, {0: .002, 1: .0004}
    controller._update_state()
    parent._ensure_channel_panel()
    other = QLineEdit()
    layout.addWidget(other)
    window.resize(920, 600)
    window.show()
    app.processEvents()
    parent._sync_manual_panel_from_controller()
    app.processEvents()
    controls, link = parent.manualPanelControls, parent.linkChannelsBox

    def drain():
        deadline = time.monotonic() + 5
        while controller._manual_apply_worker_running and time.monotonic() < deadline:
            QTest.qWait(10)
        QTest.qWait(40)
        assert not controller._manual_apply_worker_running and controller.errorCount == 0, messages

    def click(box):
        QTest.mouseClick(box, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         QPoint(8, box.height() // 2))
        drain()

    def select(box, text):
        box.setCurrentText(text)
        drain()

    def validate(field, value):
        spin = controls[0][field]
        spin.setFocus()
        spin.selectAll()
        QTest.keyClicks(spin, str(value))
        QTest.keyClick(spin, Qt.Key.Key_Return)
        drain()

    assert not link.isChecked() and controls[1]["voltage"].isEnabled()
    # Ticking the link sends CH0's setpoints to CH1, nothing else.
    hw.calls.clear()
    QTest.mouseClick(link, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(8, link.height() // 2))
    drain()
    assert link.isChecked(), (link.isChecked(), link.isVisible(), link.geometry())
    assert parent.link_channels is True, parent.link_channels
    assert hw.voltages == {0: 100., 1: 100.} and hw.currents == {0: .002, 1: .002}, (hw.voltages, hw.currents)
    assert {call[1] for call in hw.calls} == {1}, hw.calls
    assert any("CH1 linked to CH0" in message and "differ" not in message for message in messages)
    assert not controls[1]["voltage"].isEnabled() and not controls[1]["current_limit"].isEnabled()
    # A value validated on CH0 is sent to both channels.
    validate("voltage", 80)
    assert hw.voltages == {0: 80., 1: 80.}, hw.voltages
    assert controls[1]["voltage"].value() == 80.
    validate("current_limit", "0.003")
    assert hw.currents == {0: .003, 1: .003}, hw.currents
    assert hw.outputs == (True, True) and hw.enabled
    window.grab().save(str(output / f"{folder}-linked.png"))
    # Either ON box switches both outputs, in one transaction.
    hw.calls.clear()
    click(controls[0]["output_enabled"])
    assert hw.outputs == (False, False) and not hw.enabled, (hw.outputs, hw.enabled)
    assert [call for call in hw.calls if call[0] == "outputs"] == [("outputs", False, False)], hw.calls
    assert not controls[1]["output_enabled"].isChecked()
    click(controls[1]["output_enabled"])
    assert hw.outputs == (True, True) and hw.enabled, (hw.outputs, hw.enabled)
    assert hw.voltages == {0: 80., 1: 80.}, hw.voltages
    assert controls[0]["output_enabled"].isChecked()
    # Either Range sets both channels, in one transaction.
    hw.calls.clear()
    select(controls[0]["full_range"], "Full")
    assert hw.ranges == (True, True) and hw.outputs == (True, True), (hw.ranges, hw.outputs)
    assert [call for call in hw.calls if call[0] == "range"] == [("range", True, True)], hw.calls
    assert controls[1]["full_range"].currentText() == "Full"
    select(controls[1]["full_range"], "Half")
    assert hw.ranges == (False, False) and controls[0]["full_range"].currentText() == "Half", hw.ranges
    # Unlinked: CH0 alone again.
    QTest.mouseClick(link, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(8, link.height() // 2))
    app.processEvents()
    assert parent.link_channels is False and controls[1]["voltage"].isEnabled()
    validate("voltage", 90)
    assert hw.voltages == {0: 90., 1: 80.}, hw.voltages
    click(controls[0]["output_enabled"])
    select(controls[0]["full_range"], "Full")
    assert hw.outputs == (False, True) and hw.ranges == (True, False), (hw.outputs, hw.ranges)
    # Relinking with different outputs and ranges switches none of them; the next change sets both.
    hw.calls.clear()
    click(link)
    assert hw.outputs == (False, True) and hw.ranges == (True, False), (hw.outputs, hw.ranges)
    assert not [c for c in hw.calls if c[0] in ("outputs", "global", "range")], hw.calls
    assert any("Outputs differ (CH0 OFF, CH1 ON)" in message and "Ranges differ (CH0 Full, CH1 Half)" in message
               for message in messages), messages
    click(controls[1]["output_enabled"])
    assert hw.outputs == (False, False), hw.outputs
    select(controls[1]["full_range"], "Full")
    assert hw.ranges == (True, True), hw.ranges
    click(link)
    # Leave the field before closing: its focus-out would start an apply during Qt teardown.
    QTest.mouseClick(other, Qt.MouseButton.LeftButton)
    drain()
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
