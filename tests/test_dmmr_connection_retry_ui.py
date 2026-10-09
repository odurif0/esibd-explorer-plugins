"""Real Explorer ON/OFF buttons through a late native open and reconnection."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize('late_status', ['0', '-5'])
@pytest.mark.parametrize('scale', ['1', '1.5'])
def test_power_on_retry_with_real_explorer_buttons(late_status, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), late_status, str(tmp_path)],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen', 'QT_SCALE_FACTOR': scale},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip('Real PyQt6 or Explorer source unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr


def probe(late_status, output, shutdown_action=None):
    from importlib.metadata import PackageNotFoundError
    import threading
    import time
    from types import MethodType, SimpleNamespace

    try:
        from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
        from PyQt6.QtGui import QAction, QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QToolBar, QVBoxLayout, QWidget
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77
    from test_dmmr_connection_retry import connection, finish_open
    from test_dmmr_toggle_failures import FaultingDMMR

    app = QApplication([])
    patch = pytest.MonkeyPatch()
    fixture = connection.__wrapped__(patch, output)
    hw = next(fixture)
    hw.late_status = late_status
    module, controller = hw.plugin, hw.controller
    parent = QWidget()
    parent.name, parent.com, parent.baudrate = 'DMMR', 15, 230400
    parent.pluginManager = SimpleNamespace(Settings=SimpleNamespace(dataPath=output))
    parent.useOnOffLogic, parent.loading = True, False
    parent.connect_timeout_s = parent.poll_timeout_s = 0.03
    parent.main_state = 'Disconnected'
    parent.titleBar = QToolBar(parent)
    layout = QVBoxLayout(parent)
    layout.addWidget(parent.titleBar)
    parent.statusBadgeLabel, parent.statusSummaryLabel = QLabel(), QLabel()
    parent.titleBar.addWidget(parent.statusBadgeLabel)
    layout.addWidget(parent.statusSummaryLabel)
    root = Path(__file__).resolve().parents[1] / 'dmmr'
    parent.makeIcon = lambda name: QIcon(str(root / name))
    parent.onAction = StateAction(
        parentPlugin=parent, restore=False, toolTipFalse='Global ON', toolTipTrue='Global OFF',
        iconFalse=parent.makeIcon(module._DMMR_POWER_ON_ICON),
        iconTrue=parent.makeIcon(module._DMMR_POWER_OFF_ICON),
    )
    parent.closeCommunicationAction = QAction(parent)
    parent.titleBar.addAction(parent.closeCommunicationAction)
    parent.addStateAction = lambda **kwargs: StateAction(parentPlugin=parent, **kwargs)
    parent.isOn = lambda: parent.onAction.state
    parent.getChannels = lambda: []
    parent.getConfiguredModules = lambda: list(range(8))
    parent._sync_channels_from_detected_modules = lambda modules: None
    parent._sync_toolbar_communication_controls = lambda: None
    parent._sync_acquisition_controls = lambda: None
    parent._update_channel_panel = lambda: None
    parent.updateValues = lambda **kwargs: None
    parent.print = lambda *args, **kwargs: None
    for name in ('_ensure_local_on_action', '_sync_local_on_action', '_set_on_ui_state',
                 'setOn', '_display_main_state', '_status_summary_text', '_status_tooltip_text',
                 '_status_badge_style', '_acquisition_readiness'):
        setattr(parent, name, MethodType(getattr(module.DMMRDevice, name), parent))

    def refresh_widgets():
        assert QThread.currentThread() == app.thread(), 'Worker touched status widgets'
        module.DMMRDevice._update_status_widgets(parent)
    parent._update_status_widgets = refresh_widgets
    if shutdown_action is not None:
        parent.stopAcquisition = controller.stopAcquisition
        for name in ('_set_action_visible', '_set_action_enabled', '_communication_open',
                     '_sync_toolbar_communication_controls', 'closeCommunication', 'shutdownCommunication'):
            setattr(parent, name, MethodType(getattr(module.DMMRDevice, name), parent))
        parent.closeCommunicationAction.setText('Disconnect')
        parent.closeCommunicationAction.triggered.connect(parent.shutdownCommunication)
    parent.controller = controller
    controller.controllerParent = parent
    for name in ('_restore_off_ui_state', '_sync_status_to_gui', '_update_state'):
        setattr(controller, name, MethodType(getattr(module.DMMRController, name), controller))

    # Keep the real native-open/close simulator and runtime. Only the module
    # gate/range answers are simulated here, as in test_dmmr_toggle_ui.
    factory = hw.factory
    def native_driver(**kwargs):
        assert kwargs['native_backend'] is True
        assert Path(kwargs['log_dir']) == output / 'logs' / 'dmmr'
        # Inject the Python reference explicitly; this probe does not load the Rust DLL worker.
        driver = factory(**kwargs)
        gates = FaultingDMMR()
        gates.fail_start = None
        for name in ('set_enable', 'get_enable', 'set_automatic_current', 'get_automatic_current',
                     'set_module_auto_range', 'get_module_meas_range', 'get_state'):
            setattr(driver, name, getattr(gates, name))
        for name in ('get_device_state', 'get_voltage_state', 'get_temperature_state'):
            setattr(driver, name, lambda **kwargs: (0, '0x0000', 'OK'))
        driver.simulated_gates = gates
        return driver
    patch.setattr(module, '_get_dmmr_driver_class', lambda: native_driver)

    class Signals(QObject):
        initCompleteSignal = pyqtSignal()
        updateValuesSignal = pyqtSignal()
    controller.signalComm = Signals(parent)
    controller.signalComm.initCompleteSignal.connect(lambda: hw.successes.append(True))
    controller.signalComm.initCompleteSignal.connect(controller.initComplete)
    controller.signalComm.updateValuesSignal.connect(controller.updateValues)
    workers, requests = [], []
    def launch(callback):
        worker = threading.Thread(target=callback)
        workers.append(worker)
        worker.start()
    from test_dmmr_initialization_cancel import hold_host_initialization
    queued = hold_host_initialization(hw, patch)
    queued_mode = bool(shutdown_action and shutdown_action.startswith('queued_'))
    def initialize():
        assert not controller.initializing
        requests.append('connect')
        controller.initializeCommunication()
        if not queued_mode:
            launch(queued.pop())
    def toggle(parallel=False):
        assert parallel is True
        requests.append('ON' if parent.isOn() else 'OFF')
        launch(controller.toggleOn)
    parent.initializeCommunication = initialize
    controller.toggleOnFromThread = toggle
    parent._ensure_local_on_action()
    parent.onAction.triggered.connect(lambda checked: parent.setOn(on=checked))
    button = parent.titleBar.widgetForAction(parent.deviceOnAction)
    parent.resize(560, 110)
    parent.show()

    def settle():
        deadline = time.monotonic() + 4
        while True:
            for _ in range(5):
                app.processEvents()
            if not any(worker.is_alive() for worker in workers) and not controller.transitioning:
                break
            assert time.monotonic() < deadline, 'Initialization/toggle worker did not finish'
            time.sleep(0.002)
        for _ in range(10):
            app.processEvents()
    def check(on, state):
        assert parent.isOn() == parent.deviceOnAction.state == button.isChecked() == on
        action = parent.deviceOnAction
        icon = action.iconTrue if on else action.iconFalse
        tooltip = action.toolTipTrue if on else action.toolTipFalse
        assert action.icon().cacheKey() == button.icon().cacheKey() == icon.cacheKey()
        assert action.toolTip() == button.toolTip() == tooltip
        assert parent.statusBadgeLabel.text() == state

    try:
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        if queued_mode:
            from test_dmmr_failed_open_shutdown_ui import host_device_close
            assert controller.initializing and controller.device is None
            assert hw.calls == [] and len(queued) == 1
            if shutdown_action == 'queued_local_off':
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            elif shutdown_action == 'queued_global_off':
                parent.onAction.trigger()
            else:
                host_device_close(parent)
            controller.initializeCommunication()  # Duplicate ON must not rearm the queued request.
            assert len(queued) == 1
            launch(queued.pop())
            settle()
            controller.initComplete()  # A delayed success callback cannot undo Close.
            settle()
            check(False, 'Disconnected')
            assert not controller.initialized and not controller.acquiring
            assert hw.calls == [] and hw.drivers == [] and hw.successes == []
            parent.grab().save(str(output / 'cancelled-before-open.png'))
            # A fresh explicit ON can still open and activate normally.
            hw.block, hw.powered = False, True
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            assert len(queued) == 1
            launch(queued.pop())
            settle()
            check(True, 'ST_ON')
            assert hw.calls == ['open'] and hw.successes == [True]
            parent.grab().save(str(output / 'fresh-on.png'))
            QTest.mouseClick(button, Qt.MouseButton.LeftButton)
            settle()
            check(False, 'Disconnected')
            assert hw.calls == ['open', 'close'] and controller.device is None
            assert requests == ['connect', 'connect', 'ON', 'OFF']
            return 0
        settle()
        old = controller.device
        check(False, 'Connection pending')
        assert not controller.initialized and not controller.acquiring
        assert hw.calls == ['open'] and hw.successes == []
        parent.grab().save(str(output / 'pending-open.png'))
        if shutdown_action is not None:
            from test_dmmr_failed_open_shutdown_ui import host_device_close
            assert parent.closeCommunicationAction.isVisible()
            # First exercise the real Disconnect button while the Open is blocked.
            disconnect_button = parent.titleBar.widgetForAction(parent.closeCommunicationAction)
            QTest.mouseClick(disconnect_button, Qt.MouseButton.LeftButton)
            settle()
            check(False, 'Connection pending')
            assert not controller.initialized and not controller.acquiring
            assert controller.device is old and old._dll_port_claimed
            assert hw.calls == ['open'] and hw.successes == []
            assert old.simulated_gates.calls == []
            parent.grab().save(str(output / 'cleanup-pending.png'))

            def close_again():
                if shutdown_action == 'local_off':
                    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                elif shutdown_action == 'global_off':
                    parent.onAction.trigger()
                elif shutdown_action == 'host_close':
                    host_device_close(parent)
                else:
                    parent.closeCommunicationAction.trigger()
                settle()
            close_again()
            check(False, 'Connection pending')
            assert controller.device is old and hw.calls == ['open']
            finish_open(hw)
            close_again()
            assert not old._dll_port_claimed and old._transport_poisoned
            assert old.simulated_gates.calls == []
            if shutdown_action in ('local_off', 'global_off'):
                # The button remains OFF while pending: a new click is explicit ON,
                # not an implicit cleanup/reconnection by the failed OFF request.
                check(True, 'ST_ON')
                assert controller.initialized and controller.acquiring
                assert hw.calls == ['open', 'close', 'open'] and hw.successes == [True]
                assert requests == ['connect', 'connect', 'connect', 'ON']
                parent.grab().save(str(output / 'explicit-reconnected.png'))
                QTest.mouseClick(button, Qt.MouseButton.LeftButton)
                settle()
                check(False, 'Disconnected')
            else:
                check(False, 'Disconnected')
                assert controller.device is None and not controller.initialized
                assert not controller.acquiring
                assert hw.calls == ['open', 'close'] and hw.successes == []
                assert requests == ['connect'], 'Disconnect/host Close must never reconnect'
                parent.grab().save(str(output / 'cleanup-confirmed.png'))
            return 0
        # A second explicit click may inspect the attempt, never access a busy DLL.
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        settle()
        check(False, 'Connection pending')
        assert controller.device is old and hw.calls == ['open']
        assert requests == ['connect', 'connect']
        finish_open(hw)
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        settle()
        check(True, 'ST_ON')
        assert hw.calls == ['open', 'close', 'open']
        assert hw.successes == [True]
        assert requests == ['connect', 'connect', 'connect', 'ON']
        assert controller.initialized and controller.acquiring
        assert controller.device.simulated_gates.enabled
        assert old._transport_poisoned and not old._dll_port_claimed
        parent.grab().save(str(output / 'reconnected.png'))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        settle()
        check(False, 'Disconnected')
        assert hw.calls == ['open', 'close', 'open', 'close']
        assert requests[-1] == 'OFF'
        assert controller.device is None and not controller.initialized
    finally:
        parent.close()
        try:
            next(fixture)
        except StopIteration:
            pass
        patch.undo()
    return 0


if __name__ == '__main__':
    raise SystemExit(probe(int(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None))
