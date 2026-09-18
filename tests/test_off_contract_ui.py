"""A failed OFF must stay retryable through Explorer's actual Qt actions."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import MethodType, SimpleNamespace

import pytest

FAMILIES = ("ampr", "amx", "amx_hd", "dmmr", "esi", "psu")


@pytest.mark.parametrize("family", FAMILIES)
def test_failed_off_then_retry_and_reconnect(family, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), family, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=40,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt/Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(family, output):
    from importlib.metadata import PackageNotFoundError
    import threading
    import time

    try:
        from PyQt6.QtCore import QThread, Qt
        from PyQt6.QtGui import QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QToolBar, QVBoxLayout, QWidget
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77

    from test_shutdown_confirmation_regressions import controller_for
    app = QApplication([])
    module, controller, _ = controller_for(family)
    # Explorer's base toggleOn only logs; this loader leaves that hook undefined.
    module.DeviceController.toggleOn = lambda self: None
    controller.stopAcquisition = lambda: setattr(controller, "acquiring", False)
    cls = getattr(module, type(controller).__name__.replace("Controller", "Device"))

    class Parent(QWidget):
        @property
        def initialized(self):
            return self.controller.initialized

    parent = Parent()
    parent.channels = []
    parent.getChannels = lambda: parent.channels
    parent._setting = lambda _name: None
    parent.FREQUENCY_KHZ = getattr(cls, "FREQUENCY_KHZ", "")
    for name, value in vars(controller.controllerParent).items():
        setattr(parent, name, value)
    controller.controllerParent = parent
    parent.controller = controller
    parent.loading = False
    parent.main_state = "ST_ON"
    parent.useOnOffLogic = True
    parent.print = lambda *a, **kw: None
    parent.stopAcquisition = lambda: None
    parent.updateValues = lambda **kw: None
    parent._sync_toolbar_communication_controls = lambda: None
    parent._sync_acquisition_controls = lambda: None
    parent._update_config_controls = lambda: None
    parent._update_manual_mode_action = lambda: None
    parent._update_manual_panel = lambda: None
    parent._refresh_device_controls = lambda: None
    layout = QVBoxLayout(parent)
    parent.titleBar = QToolBar(parent)
    layout.addWidget(parent.titleBar)
    status, tooltip = QLabel(), QLabel()
    layout.addWidget(status)
    layout.addWidget(tooltip)

    def show_status():
        assert QThread.currentThread() == app.thread(), "status touched from a worker"
        status.setText(parent.main_state)

    parent._update_status_widgets = show_status
    for method in ("_sync_local_on_action", "_set_on_ui_state", "setOn", "_finish_setpoint_edits"):
        if hasattr(cls, method):
            setattr(parent, method, MethodType(getattr(cls, method), parent))
    path = Path(module.__file__).parent
    icons = [QIcon(str(path / f"switch-medium_{state}.png")) for state in ("on", "off")]
    kwargs = dict(parentPlugin=parent, iconFalse=icons[0], iconTrue=icons[1], restore=False)
    parent.onAction = StateAction(**kwargs, toolTipFalse="ON", toolTipTrue="OFF")
    parent.deviceOnAction = StateAction(
        **kwargs, toolTipFalse=f"Turn {family.upper()} ON.",
        toolTipTrue=f"Turn {family.upper()} OFF and disconnect.",
        event=lambda checked: parent.setOn(checked),
    )
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state(True)
    controller.initialized = True
    controller.acquiring = True
    controller.main_state = "ST_ON"
    calls, threads, failures, reconnects = [], [], [], []
    parent.initializeCommunication = lambda: reconnects.append(True)
    backend = SimpleNamespace(connected=True, recover=False, NO_ERR=0)
    checking, continue_shutdown = threading.Event(), threading.Event()

    def shutdown(**kw):
        calls.append("shutdown")
        if family == "esi":
            kw["on_discharge"]({"modules": {a: {"positive_v": 15., "negative_v": -12., "measured_a": 1e-9}
                                           for a in (1, 2)}, "consecutive": 0, "limit_v": 1.})
            checking.set()
            assert continue_shutdown.wait(5), "GUI failed to observe discharge check"
        if backend.recover:
            backend.connected = False
        return backend.recover

    backend.shutdown = backend.disconnect = shutdown
    backend.close = lambda: calls.append("dispose")
    backend.set_enable = backend.set_automatic_current = lambda *a, **kw: 0
    backend.get_enable = backend.get_automatic_current = lambda **kw: (0, not backend.recover)
    controller.device = backend
    if family == "psu":
        controller._perform_shutdown_sequence_unlocked = lambda **kw: []
        controller._confirm_shutdown_unlocked = lambda **kw: (backend.recover, "simulated failure")

    def launch(**kw):
        def run():
            try:
                controller.toggleOn()
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=run, daemon=True)
        threads.append(worker)
        worker.start()

    controller.toggleOnFromThread = launch
    button = parent.titleBar.widgetForAction(parent.deviceOnAction)
    parent.resize(560, 125)
    parent.show()

    def settle():
        deadline = time.monotonic() + 8
        while any(worker.is_alive() for worker in threads) and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.002)
        assert not any(worker.is_alive() for worker in threads), "OFF worker did not finish"
        for _ in range(20):
            app.processEvents()
        assert not failures, failures

    def check(state, diagnosis):
        assert parent.onAction.state == parent.deviceOnAction.state == button.isChecked() == state
        assert button.icon().cacheKey() == icons[int(state)].cacheKey()
        expected = parent.deviceOnAction.toolTipTrue if state else parent.deviceOnAction.toolTipFalse
        assert button.toolTip() == expected
        assert status.text() == diagnosis, status.text()
        tooltip.setText(expected)
        parent.grab().save(str(output / f"{family}-{diagnosis.replace(' ', '-')}.png"))

    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    if family == "esi":
        deadline = time.monotonic() + 4
        while not checking.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.002)
        assert checking.is_set()
        for _ in range(20):
            app.processEvents()
        check(True, module._ESI_STOPPING)
        assert controller.device is backend and controller.initialized
        assert controller.discharge_readings[1]["negative_v"] == -12.
        assert "dispose" not in calls
        continue_shutdown.set()
    settle()
    check(True, "Shutdown unconfirmed")
    assert controller.initialized and controller.device is backend
    if family in ("amx", "amx_hd"):
        assert controller.device_enabled_state == "Unknown"
    if family == "esi":
        assert controller.global_enabled is None
    assert not controller.acquiring
    assert "dispose" not in calls and not reconnects
    backend.recover = True
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle()
    check(False, "Disconnected")
    assert not controller.initialized and controller.device is None
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle()
    assert reconnects == [True], "ON after a successful OFF must reconnect"
    parent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
