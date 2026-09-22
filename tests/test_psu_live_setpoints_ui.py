"""Real Qt Vset entry, production PSU controller/worker, simulated hardware."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time
from threading import Lock

import pytest


@pytest.mark.parametrize("folder", [f"psu_{suffix}" for suffix in "abcde"])
def test_vset_edit_keeps_both_channels_on_in_real_qt(folder, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        text=True, capture_output=True, timeout=35,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(folder, output):
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLineEdit, QLabel
    except ImportError:
        return 77
    from test_psu_channels import make_psu
    from psu_fakes import StatefulPSU

    app = QApplication([])
    module, parent, controller = make_psu(folder)
    controller._sync_status_to_gui = module.PSUController._sync_status_to_gui.__get__(controller)
    controller.lock = Lock()
    controller.errorCount = 0
    messages = []
    controller.print = lambda text, **kw: messages.append(text)
    parent.main_state = "ST_ON"
    parent.startup_timeout_s = 1.
    parent.poll_timeout_s = .1
    parent.interlock_monitoring = True
    parent._schedule_delayed_refresh = lambda *_: None
    window = QWidget()
    window.setWindowTitle(f"{parent.name} – live Vset verification")
    layout = QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget
    header = QHBoxLayout()
    header.addWidget(QLabel(parent.name))
    parent.statusBadgeLabel = QLabel()
    parent.statusSummaryLabel = QLabel()
    header.addWidget(parent.statusBadgeLabel)
    header.addWidget(parent.statusSummaryLabel)
    layout.addLayout(header)

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
    hw.enabled = True
    hw.outputs = (True, True)
    hw.interlocks = (True, True)
    # Untouched fields may have more digits than the spinbox can display. A Vset
    # edit must not round the other rail or its Ilim (here displayed as 0.000 A).
    hw.voltages = {0: 100., 1: 137.1234}
    hw.currents = {0: .0014, 1: .0004}
    controller._update_state()
    parent._ensure_channel_panel()
    other = QLineEdit()
    layout.addWidget(other)
    window.resize(920, 570)
    window.show()
    window.activateWindow()
    app.processEvents()
    parent._sync_manual_panel_from_controller()
    app.processEvents()
    controls = parent.manualPanelControls
    spin = controls[0]["voltage"]
    assert all(controls[ch]["output_enabled"].isChecked() for ch in (0, 1))

    def drain():
        deadline = time.monotonic() + 5
        while controller._manual_apply_worker_running and time.monotonic() < deadline:
            QTest.qWait(10)
        QTest.qWait(40)
        assert not controller._manual_apply_worker_running
        assert controller.errorCount == 0, messages

    def edit(target, action):
        hw.calls.clear()
        spin.setFocus()
        spin.selectAll()
        QTest.keyClicks(spin, str(target))
        QTest.qWait(70)
        # A poll during editing must not apply the partial number or lose it.
        controller._update_state()
        app.processEvents()
        assert hw.calls == [], hw.calls
        assert str(target) in spin.lineEdit().text()
        if action == "enter":
            QTest.keyClick(spin, Qt.Key.Key_Return)
        elif action == "tab":
            QTest.keyClick(spin, Qt.Key.Key_Tab)
        else:
            QTest.mouseClick(other, Qt.MouseButton.LeftButton)
        drain()
        assert hw.voltages == {0: float(target), 1: 137.1234}
        assert hw.currents == {0: .0014, 1: .0004}
        assert all(call[0] == "voltage" and call[1] == 0 for call in hw.calls), hw.calls
        assert hw.outputs == (True, True) and hw.enabled
        assert all(controls[ch]["output_enabled"].isChecked() for ch in (0, 1))
        assert controller.psu_enabled_actual is True and controller.interlock_active is True
        for ch in (0, 1):
            assert parent.channelPanelCards[ch]["voltage_monitor"].text() == module._format_voltage_text(hw.voltages[ch])
        return [call[2] for call in hw.calls]

    assert edit(80, "enter") == [80.]
    window.grab().save(str(output / f"{folder}-100-to-80.png"))
    assert edit(380, "tab") == [180., 280., 380.]
    assert edit(80, "click") == [280., 180., 80.]
    assert edit(80, "enter") == [], "Revalidating an unchanged value must not send commands"
    # Switching editing focus can emit editingFinished again: still no writes.
    hw.calls.clear()
    QTest.mouseClick(other, Qt.MouseButton.LeftButton)
    drain()
    assert hw.calls == []
    # Preserve an untouched sub-mA limit, but do not swallow an explicit zero
    # just because both values have the same three-decimal display.
    current_spin = controls[0]["current_limit"]
    for action in ("enter", "tab", "click"):
        QTest.mouseClick(other, Qt.MouseButton.LeftButton)
        drain()
        hw.currents[0] = .0004
        controller._update_state()
        parent._sync_manual_panel_from_controller()
        app.processEvents()
        current_spin.setFocus()
        current_spin.selectAll()
        QTest.keyClicks(current_spin, "0")
        hw.calls.clear()
        if action == "enter":
            QTest.keyClick(current_spin, Qt.Key.Key_Return)
        elif action == "tab":
            QTest.keyClick(current_spin, Qt.Key.Key_Tab)
        else:
            QTest.mouseClick(other, Qt.MouseButton.LeftButton)
        drain()
        assert hw.calls == [("current", 0, 0.)], (action, hw.calls)
        assert hw.currents == {0: 0., 1: .0004}
        assert hw.outputs == (True, True)
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
