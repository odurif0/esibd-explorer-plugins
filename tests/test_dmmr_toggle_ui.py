"""Real Explorer buttons plus the DMMR controller on a Python worker thread."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("outcome", ["success", "stopped", "unconfirmed", "lost_ack", "close_failure"])
def test_dmmr_failed_start_button_and_next_click(outcome, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), outcome, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real PyQt6 or Explorer source unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(outcome, output):
    from importlib.metadata import PackageNotFoundError
    from types import MethodType, SimpleNamespace
    import threading
    import time

    import numpy as np
    try:
        from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
        from PyQt6.QtGui import QAction, QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QToolBar, QVBoxLayout, QWidget
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77
    from test_dmmr_plugin_behavior import _load_module
    from test_dmmr_toggle_failures import FaultingDMMR

    app = QApplication([])
    module = _load_module()
    parent = QWidget()
    parent.name = "DMMR"
    parent.useOnOffLogic = True
    parent.loading = False
    parent.connect_timeout_s = parent.poll_timeout_s = 0.1
    parent.titleBar = QToolBar(parent)
    layout = QVBoxLayout(parent)
    layout.addWidget(parent.titleBar)
    parent.statusBadgeLabel = QLabel()
    parent.statusSummaryLabel = QLabel()
    parent.titleBar.addWidget(parent.statusBadgeLabel)
    layout.addWidget(parent.statusSummaryLabel)
    tooltip_label = QLabel()
    layout.addWidget(tooltip_label)
    root = Path(__file__).resolve().parents[1] / "dmmr"
    parent.makeIcon = lambda filename: QIcon(str(root / filename))
    parent.onAction = StateAction(
        parentPlugin=parent, restore=False, toolTipFalse="Global ON", toolTipTrue="Global OFF",
        iconFalse=parent.makeIcon(module._DMMR_POWER_ON_ICON),
        iconTrue=parent.makeIcon(module._DMMR_POWER_OFF_ICON),
    )
    parent.closeCommunicationAction = QAction(parent)
    parent.titleBar.addAction(parent.closeCommunicationAction)
    parent.addStateAction = lambda **kwargs: StateAction(parentPlugin=parent, **kwargs)
    parent.isOn = lambda: parent.onAction.state
    parent._sync_toolbar_communication_controls = lambda: None
    parent._sync_acquisition_controls = lambda: None
    parent._update_channel_panel = lambda: None
    parent.main_state = "OFF"
    parent.print = lambda *args, **kwargs: None
    channel = SimpleNamespace(real=True, enabled=True, monitor=12e-12, module_address=lambda: 5)
    parent.getChannels = lambda: [channel]
    parent.getConfiguredModules = lambda: list(range(8))
    for name in ("_ensure_local_on_action", "_sync_local_on_action", "_set_on_ui_state", "setOn",
                 "_display_main_state", "_status_summary_text", "_status_tooltip_text",
                 "_status_badge_style", "_acquisition_readiness"):
        setattr(parent, name, MethodType(getattr(module.DMMRDevice, name), parent))

    def refresh_widgets():
        assert QThread.currentThread() == app.thread(), "Worker touched status widgets"
        module.DMMRDevice._update_status_widgets(parent)
    parent._update_status_widgets = refresh_widgets
    controller = module.DMMRController(parent)
    parent.controller = controller
    controller.device = hardware = FaultingDMMR()
    hardware.fail_start = None if outcome in ("success", "close_failure") else "enable_ack" if outcome == "lost_ack" else "range"
    hardware.reject_cleanup = outcome in ("unconfirmed", "lost_ack")
    hardware.disconnect_result = outcome != "close_failure"
    controller.initialized = True
    controller.main_state = "OFF"
    controller.detected_module_ids = list(range(8))
    controller.detected_modules_text = "0–7"
    controller.values = {5: 12e-12}
    logs, requests, workers = [], [], []
    controller.print = lambda message, **kwargs: logs.append(message)

    class Signals(QObject):
        updateValuesSignal = pyqtSignal()
    controller.signalComm = Signals(parent)
    controller.signalComm.updateValuesSignal.connect(controller.updateValues)

    def start_worker(parallel=False):
        assert parallel is True
        requests.append(parent.isOn())
        worker = threading.Thread(target=controller.toggleOn)
        workers.append(worker)
        worker.start()
    controller.toggleOnFromThread = start_worker
    parent._ensure_local_on_action()
    button = parent.titleBar.widgetForAction(parent.deviceOnAction)
    parent.onAction.triggered.connect(lambda checked: parent.setOn(on=checked))
    parent.resize(570, 125)
    parent.show()

    def settle():
        deadline = time.monotonic() + 3
        while any(worker.is_alive() for worker in workers):
            app.processEvents()
            assert time.monotonic() < deadline, "Toggle worker did not finish"
            time.sleep(0.002)
        for _ in range(10):
            app.processEvents()
        assert not controller.transitioning

    def check(on):
        action = parent.deviceOnAction
        assert parent.isOn() == action.state == button.isChecked() == on
        expected_icon = action.iconTrue if on else action.iconFalse
        assert action.icon().cacheKey() == button.icon().cacheKey() == expected_icon.cacheKey()
        expected_tip = action.toolTipTrue if on else action.toolTipFalse
        assert action.toolTip() == button.toolTip() == expected_tip
        tooltip_label.setText(expected_tip)
        assert len(requests) == len(workers), "Presentation emitted another command"

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle()
    expected_on = outcome != "stopped"
    check(expected_on)
    assert requests == [True]
    if outcome in ("success", "close_failure"):
        assert controller.acquiring and hardware.enabled
        assert parent.statusBadgeLabel.text() == "ST_ON"
    else:
        assert not controller.acquiring
        assert np.isnan(channel.monitor), "Failed startup left a stale displayed sample"
        assert not parent._acquisition_readiness()[0]
        if expected_on:
            assert parent.statusBadgeLabel.text() == "Shutdown unconfirmed"
            assert "#c53030" in parent.statusBadgeLabel.styleSheet()
        else:
            assert parent.statusBadgeLabel.text() == "OFF"
        assert any("-12" in message for message in logs), "Original error was hidden"
    parent.grab().save(str(output / f"dmmr-{outcome}.png"))

    # Following a stopped failure, retry ON. Otherwise the next click is OFF,
    # including a second unsuccessful OFF and a later verified recovery.
    enable_count = hardware.calls.count(("enable", True))
    if outcome in ("unconfirmed", "lost_ack", "close_failure"):
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        settle()
        assert requests == [True, False]
        check(True)
        assert parent.statusBadgeLabel.text() == "Shutdown unconfirmed"
        assert hardware.connected and controller.initialized
        assert hardware.calls.count(("enable", True)) == enable_count
    hardware.reject_cleanup = False
    hardware.fail_start = None
    hardware.disconnect_result = True
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle()
    check(not expected_on)
    assert requests[-1] == (not expected_on)
    if expected_on:
        assert hardware.calls.count(("enable", True)) == enable_count
        assert not hardware.enabled and not controller.acquiring
        assert not hardware.connected and hardware.port_closes == 1
        assert controller.device is None and not controller.initialized
        assert parent.statusBadgeLabel.text() == "Disconnected"
        parent.grab().save(str(output / f"dmmr-{outcome}-after-off.png"))
        initialize_requests = []
        parent.initializeCommunication = lambda: initialize_requests.append(True)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert initialize_requests == [True], "ON must reopen communication after OFF"
    else:
        assert hardware.enabled and controller.acquiring
        assert parent.statusBadgeLabel.text() == "ST_ON"
    parent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
