"""Real Qt input/feedback tests using Explorer's Parameter and spinbox code."""
from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('folder', ['ampr_a', 'ampr_b'])
@pytest.mark.parametrize('action', ['enter', 'tab', 'click', 'feedback', 'background'])
def test_ampr_commit_and_feedback(folder, action, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, action, str(tmp_path)],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        capture_output=True, text=True, timeout=25,
    )
    if result.returncode == 77:
        pytest.skip('Real Qt or Explorer sources unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr


def probe(folder, action, output):
    import re
    import threading
    import time
    from enum import Enum
    from importlib.metadata import PackageNotFoundError, distribution
    from types import SimpleNamespace

    import numpy as np

    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtTest import QTest
        host = Path(distribution('esibd-explorer').locate_file('esibd/core.py'))
    except (ImportError, PackageNotFoundError):
        return 77
    if not host.is_file():
        return 77

    app = QtWidgets.QApplication([])
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, obj: obj,
              INOUT=Enum('INOUT', 'IN OUT'), getTestMode=lambda: False, Thread=threading.Thread,
              PARAMETERTYPE=Enum('PARAMETERTYPE', 'COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH'))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith('Q')})
    tree = ast.parse(host.read_text())

    def extract(name, parent=None):
        nodes = tree.body if parent is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parent).body
        node = next(n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        source = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(source), str(host), 'exec'), ns)
        return ns[name]

    from test_ampr_plugin_packaging import _install_esibd_stubs
    _install_esibd_stubs()
    core = sys.modules['esibd.core']
    core.DeviceController.__init__ = lambda self, controllerParent: setattr(self, 'controllerParent', controllerParent)
    core.DeviceController.applyValueFromThread = extract('applyValueFromThread', 'DeviceController')
    core.Channel.applyValue = extract('applyValue', 'Channel')
    core.Channel.valueChanged = extract('valueChanged', 'Channel')
    ns['Channel'] = core.Channel
    for name in ('ParameterWidget', 'LabviewSpinBox', 'LabviewDoubleSpinBox', 'LabviewSciSpinBox', 'Parameter'):
        extract(name)
    core.Parameter = ns['Parameter']
    core.PARAMETERTYPE = ns['PARAMETERTYPE']
    spec = importlib.util.spec_from_file_location('ampr_ui_probe', ROOT / folder / 'ampr_plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    channels = []
    logs = []
    parent = SimpleNamespace(name=folder, loading=False, isOn=lambda: True, useOnOffLogic=True,
                             getChannels=lambda: channels, getConfiguredModules=lambda: [0],
                             module_voltage_limit=lambda number: 1000.,
                             print=lambda *args, **kwargs: logs.append(args))
    controller = module.AMPRController(parent)
    parent.controller = controller
    controller.initialized = True
    controller.acquiring = True
    controller.main_state = 'ST_ON'
    controller.errorCount = 0
    controller.lock = threading.Lock()
    controller.print = parent.print
    controller._sync_status_to_gui = lambda: None
    controller._update_state = lambda **kwargs: None

    class Device:
        NO_ERR = 0
        status = 0
        def __init__(self):
            self.calls, self.targets = [], {}
        def set_module_voltage(self, module, number, target):
            assert QtCore.QThread.currentThread() != app.thread()
            self.calls.append((number, target))
            if self.status == 0:
                self.targets[number] = target
            return self.status
        def get_module_voltages(self, module):
            return {number: dict(setpoint=target, measured=target) for number, target in self.targets.items()}
        def format_status(self, status): return str(status)

    device = controller.device = Device()
    manager = SimpleNamespace(globalUpdate=lambda **kwargs: [channel.applyValue() for channel in channels])
    module.AMPRChannel.value = property(lambda ch: ch.params['Value'].value,
                                       lambda ch, value: setattr(ch.params['Value'], 'value', value))
    module.AMPRChannel.monitor = property(lambda ch: ch.params['Monitor'].value,
                                         lambda ch, value: setattr(ch.params['Monitor'], 'value', value))
    window = QtWidgets.QTreeWidget()
    window.setColumnCount(3)
    window.setHeaderLabels(['Channel', 'Setpoint (V)', 'Monitor (V)'])
    window.resize(500, 180)

    class CheckedSpin(ns['LabviewDoubleSpinBox']):
        def value(self):
            assert QtCore.QThread.currentThread() == app.thread()
            return super().value()
        def setStyleSheet(self, style):
            assert QtCore.QThread.currentThread() == app.thread()
            super().setStyleSheet(style)
        def setToolTip(self, text):
            assert QtCore.QThread.currentThread() == app.thread()
            super().setToolTip(text)

    for number in (1, 2):
        channel = module.AMPRChannel.__new__(module.AMPRChannel)
        channel.channelParent = parent
        channel.name, channel.module, channel.id = f'CH{number}', '0', str(number)
        channel.enabled = channel.real = channel.active = True
        channel.loading = True
        channel.waitToStabilize = False
        channel.controller = None
        channel.inout = ns['INOUT'].IN
        channel.pluginManager = SimpleNamespace(Device=SimpleNamespace, DeviceManager=manager)
        channel.print = parent.print
        channel.rowHeight = 28
        channel.tree = window
        channel.lastAppliedValue = np.nan
        channel.params = {}
        channel.getParameterByName = channel.params.get
        row = QtWidgets.QTreeWidgetItem(window, [channel.name])
        for index, name in ((1, 'Value'), (2, 'Monitor')):
            widget = CheckedSpin(indicator=name == 'Monitor')
            parameter = ns['Parameter'](name, channel, parameterType=ns['PARAMETERTYPE'].FLOAT,
                                       widget=widget, minimum=-1000., maximum=1000.,
                                       event=channel.valueChanged if name == 'Value' else None)
            parameter.value = 0.
            channel.params[name] = parameter
            window.setItemWidget(row, index, widget)
        channel.parameters = list(channel.params.values())
        channel.initGUI({})
        channel.loading = False
        channels.append(channel)

    window.show()
    window.activateWindow()
    app.processEvents()
    first, second = (ch.params['Value'].spin for ch in channels)

    def flush():
        for _ in range(5):
            app.processEvents()

    def wait_idle():
        deadline = time.monotonic() + 3
        while controller._setpoint_thread is not None and time.monotonic() < deadline:
            flush()
            time.sleep(.002)
        assert controller._setpoint_thread is None
        flush()

    def poll():
        worker = threading.Thread(target=controller.readNumbers)
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        flush()
        controller.updateValues()

    if action in {'enter', 'tab', 'click'}:
        controller.lock.acquire()
        first.setFocus()
        first.selectAll()
        QTest.keyClicks(first, '123.45')
        flush()
        assert channels[0].value == 0., 'intermediate digits escaped the editor'
        assert device.calls == []
        if action == 'enter':
            QTest.keyClick(first, QtCore.Qt.Key.Key_Return)
        elif action == 'tab':
            QTest.keyClick(first, QtCore.Qt.Key.Key_Tab)
        else:
            QTest.mouseClick(second, QtCore.Qt.MouseButton.LeftButton)
        flush()
        assert channels[0].value == 123.45
        assert channels[0]._ampr_setpoint_state == 'pending'
        assert np.isnan(channels[0].lastAppliedValue)
        assert device.calls == []
        controller.lock.release()
        wait_idle()
        assert device.calls.count((1, 123.45)) == 1
        poll()
        assert channels[0]._ampr_setpoint_state == 'confirmed'
        assert channels[0].lastAppliedValue == 123.45
        # Programmatic/equation updates must not wait for a focus event.
        channels[0].value = -75.25
        wait_idle()
        assert device.calls[-1] == (1, -75.25)
    elif action == 'feedback':
        controller.lock.acquire()
        channels[0].value = 20.
        flush()
        assert channels[0]._ampr_setpoint_state == 'pending'
        window.grab().save(str(output / 'ampr-pending.png'))
        controller.lock.release()
        wait_idle()
        assert channels[0]._ampr_setpoint_state == 'sent'
        device.targets[1] = 12.
        poll()
        assert channels[0]._ampr_setpoint_state == 'mismatch'
        assert '12.000' in first.toolTip()
        window.grab().save(str(output / 'ampr-mismatch.png'))
        # Explicitly revalidating the same value retries a failed command.
        first.setFocus()
        QTest.keyClick(first, QtCore.Qt.Key.Key_Return)
        wait_idle()
        poll()
        assert channels[0]._ampr_setpoint_state == 'confirmed'
        assert not first.styleSheet()
        window.grab().save(str(output / 'ampr-confirmed.png'))
        # Regular polling/feedback must not replace text being edited.
        first.setFocus()
        first.selectAll()
        QTest.keyClicks(first, '3')
        for _ in range(3):
            poll()
        assert first.lineEdit().text() == '3'
        assert channels[0].value == 20.
        QTest.keyClicks(first, '2.75')
        QTest.keyClick(first, QtCore.Qt.Key.Key_Tab)
        flush()
        wait_idle()
        assert device.calls[-1] == (1, 32.75)
        # An ACK becoming confirmed while typing must not disturb the text either.
        first.setFocus()
        first.selectAll()
        QTest.keyClicks(first, '4')
        poll()
        assert first.lineEdit().text() == '4'
        assert channels[0].value == 32.75
        QTest.keyClicks(first, '0')
        QTest.keyClick(first, QtCore.Qt.Key.Key_Tab)
        # A queued feedback callback for an old request cannot confirm a new one.
        channels[0].value = 40.
        worker = controller._setpoint_thread
        if worker:
            worker.join(2)  # deliberately do not pump Qt yet
        controller.lock.acquire()
        channels[0].value = 50.
        flush()
        assert channels[0]._ampr_setpoint_state == 'pending'
        assert np.isnan(channels[0].lastAppliedValue)
        assert first.value() == 50.
        controller.lock.release()
        wait_idle()
        poll()
        assert channels[0].lastAppliedValue == 50.
    else:
        parameter = channels[0].params['Value']
        parameter.loading = True
        parameter.value = 7.
        parameter.loading = False
        worker = threading.Thread(target=controller.applyValueFromThread, args=(channels[0],))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
        flush()
        wait_idle()
        assert device.calls == [(1, 7.)]
        poll()
        assert channels[0].lastAppliedValue == 7.
    controller._cancel_setpoints()
    window.close()
    flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
