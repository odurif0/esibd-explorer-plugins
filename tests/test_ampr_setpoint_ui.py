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
@pytest.mark.parametrize('action', ['enter', 'tab', 'click', 'blank', 'paste', 'feedback', 'background',
                                    'toolbar', 'toolbar_pending', 'toolbar_off',
                                    'cross_update', 'header', 'off_colors', 'off_error', 'ramp_column', 'channel_toggle'])
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
    module.AMPRChannel.ramp_rate_v_s = property(lambda ch: ch.params['Ramp rate'].value)
    window.setColumnCount(4)
    window.setHeaderLabels(['Channel', 'Setpoint (V)', 'Ramp (V/s)', 'Monitor (V)'])
    window.resize(620, 180)

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
        for index, name in ((1, 'Value'), (2, 'Ramp rate'), (3, 'Monitor')):
            widget = CheckedSpin(indicator=name == 'Monitor')
            parameter = ns['Parameter'](name, channel, parameterType=ns['PARAMETERTYPE'].FLOAT,
                                       widget=widget, minimum=-1000., maximum=1000.,
                                       event=channel.valueChanged if name == 'Value' else None)
            parameter.value = 10. if name == 'Ramp rate' else 0.
            if name == 'Ramp rate':
                widget.setRange(0., 1000.)
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

    if action == 'channel_toggle':
        channel = channels[0]
        ns['pyqtSignal'] = QtCore.pyqtSignal
        for name in ('CheckBox', 'CompactComboBox', 'LedIndicator'):
            extract(name)
        check = ns['CheckBox']()
        check.setParent(window)
        channel.loading = True
        def changed():
            channel.enabled = enabled.value
            channel.enabledChanged()
            channel.applyValue(apply=True)
        enabled = ns['Parameter']('Enabled', channel, parameterType=ns['PARAMETERTYPE'].BOOL,
                                  widget=check, event=changed)
        enabled.value = True
        channel.params['Enabled'] = enabled
        channel.parameters.append(enabled)
        channel.loading = False
        channel._sync_enabled_toggle_widget()
        assert enabled.check is check
        assert check.focusPolicy() == QtCore.Qt.FocusPolicy.TabFocus, check.focusPolicy()
        window.setItemWidget(window.topLevelItem(0), 0, check)
        parameter = channel.params['Value']
        parameter.loading = True
        parameter.value = 100.
        parameter.loading = False
        for text, expected in [('250', 0.), ('350', 350.)]:
            device.calls.clear()
            first.setFocus()
            first.selectAll()
            QTest.keyClicks(first, text)
            flush()
            QTest.mouseClick(check, QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(10, check.height() // 2))
            wait_idle()
            assert [item for item in device.calls if item[0] == 1] == [(1, expected)], device.calls
            if expected == 0.:
                assert channel.value == 100., 'Channel OFF must not validate a new voltage first'
        assert channel.value == 350.
    elif action == 'ramp_column':
        rate = channels[0].params['Ramp rate'].spin
        rate.setFocus()
        rate.selectAll()
        QTest.keyClicks(rate, '7.5')
        assert channels[0].ramp_rate_v_s == 10.
        QTest.keyClick(rate, QtCore.Qt.Key.Key_Return)
        assert channels[0].ramp_rate_v_s == 7.5
        first.setFocus()
        rate.setFocus()
        rate.selectAll()
        QTest.keyClicks(rate, '2.5')
        assert channels[0].ramp_rate_v_s == 7.5
        parent.initialized = True
        parent.onAction = SimpleNamespace(state=True)
        parent._sync_local_on_action = lambda: None
        snapshots = []
        controller.toggleOnFromThread = lambda **kw: snapshots.append(dict(controller._transition_rates))
        button = QtWidgets.QPushButton('ON', window)
        button.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        button.clicked.connect(lambda: module.AMPRDevice.setOn(parent, on=True))
        button.show()
        QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
        assert snapshots == [{(0, 1): 2.5, (0, 2): 10.}]
        export = extract('asDict', 'Channel')
        channels[0].tempParameters = lambda: ['Monitor']
        saved = export(channels[0], formatValue=True)
        assert float(saved['Ramp rate']) == 2.5
        channels[0].params['Ramp rate'].value = 99.
        channels[0].params['Ramp rate'].value = saved['Ramp rate']
        assert channels[0].ramp_rate_v_s == 2.5
        assert not device.calls, 'Rate entry alone is not a voltage command'
        button.hide()
        rate.clearFocus()
        window.clearSelection()
        flush()
        window.grab().save(str(output / f'{folder}-ramp-column.png'))
        controller._end_transition()
    elif action in {'cross_update', 'header'}:
        channels[0].value = 100.
        wait_idle()
        poll()
        device.calls.clear()
        first.setFocus()
        first.selectAll()
        QTest.keyClicks(first, '25')
        flush()
        if action == 'cross_update':
            channels[1].value = 12.
            wait_idle()
            assert first.hasFocus()
            assert first.lineEdit().text() == '25'
            assert channels[0].value == 100., 'Another channel must not validate this edit'
            assert not [call for call in device.calls if call[0] == 1], device.calls
            QTest.keyClicks(first, '0')
            QTest.keyClick(first, QtCore.Qt.Key.Key_Return)
            expected = 250.
        else:
            QTest.mouseClick(window.header().viewport(), QtCore.Qt.MouseButton.LeftButton,
                             pos=QtCore.QPoint(240, 8))
            expected = 25.
        wait_idle()
        assert channels[0].value == expected
        assert [call for call in device.calls if call[0] == 1] == [(1, expected)]
    elif action in {'off_colors', 'off_error'}:
        ns.update(getDarkMode=lambda: False, colors=SimpleNamespace(fg='#202020'),
                  PLUGINTYPE=SimpleNamespace(INPUTDEVICE=object(), OUTPUTDEVICE=object()))
        parent.pluginType = None
        core.Channel.updateColor = extract('updateColor', 'Channel')
        for index, channel in enumerate(channels):
            channel.color = '#63b3ed'
            channel.updateDisplay = lambda: None
            channel.setBackground = window.topLevelItem(index).setBackground
            for parameter in channel.parameters:
                parameter.getWidget().container = QtWidgets.QWidget()
        parent.loading = True  # suppress the host's plot-reconnect side effect only
        channels[0].updateColor()
        parent.loading = False
        channels[0].value = 100.
        wait_idle()
        poll()
        if action == 'off_colors':
            channels[0].enabled = False
            channels[0].enabledChanged()
            wait_idle()
        else:
            device.status = -12
            channels[0].value = 250.
            wait_idle()
            assert channels[0]._ampr_setpoint_state == 'error'
            parent.isOn = lambda: False
            controller.acquiring = False
            controller.initialized = False
            controller._cancel_setpoints()
            controller.updateValues()
        channels[0]._sync_monitor_feedback()
        assert window.topLevelItem(0).background(1).style() == QtCore.Qt.BrushStyle.NoBrush
        for parameter in channels[0].parameters:
            assert not parameter.getWidget().styleSheet(), parameter.getWidget().styleSheet()
            assert not parameter.getWidget().container.styleSheet()
        assert channels[0].color == '#63b3ed', 'The saved plot colour must not change'
        first.clearFocus()
        window.clearSelection()
        window.topLevelItem(0).setText(0, 'CH1 — OFF')
        flush()
        window.grab().save(str(output / f'{folder}-{action}.png'))
    elif action.startswith('toolbar'):
        from test_on_off_action_ui import explorer_state_action
        StateAction = explorer_state_action()
        window.name = folder
        window.titleBar = QtWidgets.QToolBar(window)
        window.setViewportMargins(0, 36, 0, 0)
        window.titleBar.setGeometry(0, 0, 490, 32)
        window.titleBar.show()
        parent.initialized = True
        parent._sync_local_on_action = lambda: None
        controller._begin_transition = lambda *_: True
        def toggle(**_):
            if parent.isOn():
                channels[0].applyValue(apply=True)
            else:
                controller._cancel_setpoints()
        controller.toggleOnFromThread = toggle
        on = StateAction(parentPlugin=window, restore=False, iconFalse=QtGui.QIcon(), iconTrue=QtGui.QIcon(),
                         toolTipFalse='Turn ON', toolTipTrue='Turn OFF',
                         event=lambda checked: module.AMPRDevice.setOn(parent, checked))
        apply_action = StateAction(parentPlugin=window, restore=False, iconFalse=QtGui.QIcon(), iconTrue=QtGui.QIcon(),
                                   toolTipFalse='Apply', toolTipTrue='Apply',
                                   event=lambda _: controller.applyValueFromThread(channels[0], force=True))
        parent.onAction = on
        parent.isOn = lambda: on.state
        on_button = window.titleBar.widgetForAction(on)
        apply_button = window.titleBar.widgetForAction(apply_action)
        assert on_button.focusPolicy() == QtCore.Qt.FocusPolicy.NoFocus
        parameter = channels[0].params['Value']
        parameter.loading = True
        parameter.value = 100.
        parameter.loading = False
        controller.lock.acquire()
        first.setFocus()
        first.selectAll()
        QTest.keyClicks(first, '250')
        flush()
        assert channels[0].value == 100.
        QTest.mouseClick(on_button, QtCore.Qt.MouseButton.LeftButton)
        flush()
        assert channels[0].value == 250.
        assert device.calls == []
        if action != 'toolbar':
            first.setFocus()
            first.selectAll()
            QTest.keyClicks(first, '350')
            assert '350' in first.lineEdit().text(), first.lineEdit().text()
            QTest.mouseClick(apply_button if action == 'toolbar_pending' else on_button,
                             QtCore.Qt.MouseButton.LeftButton)
            flush()
            if action == 'toolbar_pending':
                assert apply_action.state, 'The toolbar apply action was not clicked'
                assert channels[0].value == 350., (first.hasFocus(), first.lineEdit().text(), channels[0].value)
        controller.lock.release()
        wait_idle()
        expected = [] if action == 'toolbar_off' else [(1, 350. if action == 'toolbar_pending' else 250.)]
        assert [call for call in device.calls if call[0] == 1] == expected, device.calls
        if action == 'toolbar_off':
            assert device.calls == [], device.calls
        window.grab().save(str(output / f'{folder}-{action}.png'))
    elif action in {'enter', 'tab', 'click', 'blank', 'paste'}:
        controller.lock.acquire()
        first.setFocus()
        first.selectAll()
        if action == 'paste':
            app.clipboard().setText('123.45')
            QTest.keyClick(first, QtCore.Qt.Key.Key_V, QtCore.Qt.KeyboardModifier.ControlModifier)
        else:
            QTest.keyClicks(first, '123.45')
        flush()
        assert channels[0].value == 0., 'intermediate digits escaped the editor'
        assert device.calls == []
        if action in {'enter', 'paste'}:
            QTest.keyClick(first, QtCore.Qt.Key.Key_Return)
        elif action == 'tab':
            QTest.keyClick(first, QtCore.Qt.Key.Key_Tab)
        elif action == 'blank':
            QTest.mouseClick(window.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(20, 130))
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
    controller._latest_setpoints.clear()
    controller.device = None
    controller.initialized = False
    window.close()
    flush()
    return 0


if __name__ == '__main__':
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
