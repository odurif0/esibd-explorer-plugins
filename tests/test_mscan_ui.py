"""Real Explorer Scan/Channel/Parameter + PSU controller under Qt, isolated process."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(('scenario', 'scale'), [
    ('complete', '1'), ('stop', '1'), ('off', '1'), ('invalid', '1'), ('interface', '1'),
    ('interface', '1.5'), ('legacy-ini', '1'), ('legacy-hdf', '1'), ('native-start', '1'),
])
def test_mscan_in_real_explorer_and_qt(tmp_path, scenario, scale):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUNBUFFERED='1', QT_SCALE_FACTOR=scale)
    result = subprocess.run([env.get('ESIBD_QT_PYTHON', sys.executable), str(Path(__file__)),
                             scenario, str(tmp_path)], env=env, text=True, capture_output=True, timeout=45)
    if result.returncode == 77:
        pytest.skip(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def run(scenario, target):
    host = os.environ.get('ESIBD_EXPLORER_SOURCE')
    if host:
        sys.path.insert(0, host)
    # Only unrelated desktop automation is stubbed; every Explorer/Qt class is real.
    sys.modules['pyautogui'] = ModuleType('pyautogui')
    try:
        from PyQt6.QtCore import QTimer, QThread, QObject, Qt, QRect
        from PyQt6.QtTest import QTest
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication, QWidget, QHBoxLayout, QVBoxLayout, QTreeWidget, QLabel
        from esibd import core, plugins
        import h5py
        import numpy as np
    except ImportError as exc:
        print(f'Full Explorer/Qt dependencies unavailable: {exc}. Set ESIBD_QT_PYTHON and optionally ESIBD_EXPLORER_SOURCE.')
        return 77
    from threading import Lock, Event
    import time
    from psu_fakes import StatefulPSU

    app = QApplication([])
    app.setStyleSheet('QWidget { background: #20232a; color: #eeeeee; }'
                     'QHeaderView::section { background: #41464f; color: #eeeeee; }'
                     'QAbstractItemView { alternate-background-color: #30343c; }')
    errors, actions = [], []
    def log(**kw):
        print('LOG', kw)
        if kw.get('flag') == core.PRINT.ERROR:
            errors.append(kw.get('message'))
    class Manager(NS):
        def __getattr__(self, name):
            return getattr(plugins, name, None)
    manager = Manager(plugins=[], loading=True, closing=False, testing=False, logger=NS(print=log),
        connectAllSources=lambda: None, reconnectSource=lambda *a: None)
    manager.getPluginsByClass = lambda cls: [p for p in manager.plugins if isinstance(p, cls)]
    manager.getPluginsByType = lambda kind: [p for p in manager.plugins if getattr(p, 'pluginType', None) == kind]
    dm = manager.DeviceManager = plugins.DeviceManager.__new__(plugins.DeviceManager)
    QObject.__init__(dm)
    dm.pluginManager = manager
    dm.updateStaticPlot = lambda: None
    manager.Settings = NS(loading=True, configPath=target / 'config', dependencyPath=ROOT / 'mscan',
        sourceCodePath=ROOT / 'mscan/mscan_plugin.py', sessionPath=target,
        measurementNumber=1, getFullSessionPath=lambda: target, saveSettings=lambda **kw: None)
    manager.Settings.configPath.mkdir(exist_ok=True)
    manager.Explorer = NS(root=None, populateTree=lambda: None, activeFileFullPath=None)
    manager.Text = NS(setText=lambda *a, **kw: None)

    def load(slug, file):
        # Match PluginManager.loadPluginsFromPath, including no sys.modules entry.
        name = Path(file).stem
        assert name not in sys.modules
        module = core.dynamicImport(name, ROOT / slug / file)
        assert module is not None
        assert name not in sys.modules
        return module
    psum = load('psu_a', 'psu_plugin.py')
    mscan = load('mscan', 'mscan_plugin.py')
    window = QWidget()
    outer = QHBoxLayout(window)
    sources_widget, scan_widget, plot_widget = QWidget(), QWidget(), QWidget()
    sources_layout, scan_layout, plot_layout = QVBoxLayout(sources_widget), QVBoxLayout(scan_widget), QVBoxLayout(plot_widget)
    outer.addWidget(sources_widget, 1)
    outer.addWidget(scan_widget, 1)
    outer.addWidget(plot_widget, 2)

    def device_fields(parent, name, unit, inout, monitors):
        QObject.__init__(parent)
        parent.name, parent._loading, parent.channels = name, 0, []
        parent.loading = True
        parent.pluginManager = manager
        parent.interval = 10
        parent.updating = False
        parent.unit, parent.inout = unit, inout
        parent.useDisplays, parent.useMonitors = True, monitors
        parent.useBackgrounds, parent.logY = False, False
        parent.convertDataDisplay, parent.liveDisplay = None, None
        parent.recordingAction = NS(state=True)
        parent.recording = True
        parent.maxDataPoints = 100000
        parent.time = core.DynamicNp(max_size=100000, dtype=np.float64)
        parent.isOn = lambda: True
        parent.makeCoreIcon = lambda *a, **kw: QIcon()
        parent.print = lambda message, flag=core.PRINT.MESSAGE: log(message=message, flag=flag)
        parent._schedule_delayed_refresh = lambda *a: None
        parent.tree = QTreeWidget()
        sources_layout.addWidget(QLabel(name))
        sources_layout.addWidget(parent.tree)
        manager.plugins.append(parent)

    psu = psum.PSUDevice.__new__(psum.PSUDevice)
    device_fields(psu, 'PSU_A', 'V', core.INOUT.IN, True)
    psu.startup_timeout_s, psu.poll_timeout_s, psu.interlock_monitoring = 1., .1, True
    psu.controller = controller = psum.PSUController(psu)
    controller.lock, controller.initialized = Lock(), True
    controller.print = psu.print
    for i in (0, 1):
        c = psum.PSUChannel(psu, psu.tree)
        psu.tree.setColumnCount(len(c.parameters))
        psu.tree.addTopLevelItem(c)
        psu.channels.append(c)
        c.initGUI({'Name': f'PSU_A_CH{i}', 'CH': str(i)})
    class Hardware(StatefulPSU):
        def collect_housekeeping(self, **kw):
            return dict(main_state={'name': 'ST_ON'}, device_enabled=self.enabled, output_enabled=self.outputs,
                psu_state={'psu_enabled_actual': self.enabled, 'interlock_active': True,
                           'interlock_out_disabled': False, 'interlock_bnc_disabled': False},
                device_state={'flags': []}, channels=[dict(channel=i, enabled=self.outputs[i],
                    voltage={'measured_v': self.voltages[i] - .1, 'set_v': self.voltages[i]},
                    current={'measured_a': self.currents[i] / 2, 'set_a': self.currents[i]},
                    full_range={'enabled': self.ranges[i], 'supported': True}) for i in (0, 1)])
        def get_channel_measurements(self, channel, **kw):
            return self.voltages[channel] - .1, self.currents[channel] / 2, 15.
    hw = controller.device = Hardware()
    hw.enabled, hw.outputs, hw.interlocks = True, (True, True), (True, True)
    hw.voltages, hw.currents = {0: 100., 1: 110.}, {0: .001, 1: .001}
    psu.loading = False
    controller._update_state()

    detector_device = plugins.Device.__new__(plugins.Device)
    device_fields(detector_device, 'DetectorDevice', 'A', core.INOUT.OUT, False)
    detector_device.controller = NS(initialized=True, acquiring=True)
    class CurrentChannel(core.Channel):
        def getDefaultChannel(self):
            channel = super().getDefaultChannel()
            channel[self.VALUE][core.Parameter.PARAMETER_TYPE] = core.PARAMETERTYPE.EXP
            return channel
    detector = CurrentChannel(detector_device, detector_device.tree)
    detector_device.tree.setColumnCount(len(detector.parameters))
    detector_device.tree.addTopLevelItem(detector)
    detector_device.channels.append(detector)
    detector.initGUI({'Name': 'DMMR_M03'})
    detector.values = core.DynamicNp(max_size=100000)
    detector_device.loading = False
    # No real AMX driver is needed: these are the decoded live-register fields.
    amx = NS(name='AMX_A', psu_ch01='PSU_A', psu_ch23='None', isOn=lambda: True,
        controller=NS(initialized=True, initializing=False, transitioning=False, device=object(),
            output_rows=[dict(state='Periodic', frequency='500 kHz',
                timing=dict(period=200, high=100, phase=i % 2 * 100)) for i in range(4)]))
    manager.plugins.append(amx)
    scan = mscan.MScan(pluginManager=manager, dependencyPath=ROOT / 'mscan', sourceCodePath=ROOT / 'mscan/mscan_plugin.py')
    manager.plugins.append(scan)
    scan.addContentWidget = scan_layout.addWidget
    scan.initGUI()
    scan.statusAction = NS(setVisible=lambda *a: None)
    manager.loading = False
    manager.Settings.loading = False
    scan.loading = True
    scan.amx_name, scan.amx_outputs = 'AMX_A', 'CH0-CH1'
    scan.start, scan.stop, scan.step = 60., 80., 10.
    scan.wait = scan.waitLong = 25
    scan.average, scan.settle_timeout, scan.voltage_tolerance = 55, 1., 1.
    scan.loading = False
    scan._refresh_interface()
    measured = scan.settingsMgr.settings[scan.DISPLAY]
    assert measured.text(0) == 'Measured signal'
    assert measured.fixedItems
    assert measured.value == scan.NO_SIGNAL
    assert 'DMMR_M03' in measured.items
    assert 'Detector' not in measured.items
    # Actual keyboard selection, without adding/editing an item by hand.
    measured.combo.setFocus()
    QTest.keyClick(measured.combo, Qt.Key.Key_Home)
    QTest.keyClick(measured.combo, Qt.Key.Key_Down)
    assert measured.value == 'DMMR_M03'
    for p in (psu, detector_device):
        p.tree.setHeaderLabels([a.name for a in p.channels[0].parameters])
        for i, parameter in enumerate(p.channels[0].parameters):
            p.tree.setColumnHidden(i, parameter.name not in ('Name', 'Value', 'Monitor'))
        p.tree.setMaximumHeight(130)
    # Full native figure and data-copy action, but not the surrounding dock manager.
    scan.display = scan.Display(scan=scan, pluginManager=manager)
    scan.display.addContentWidget = plot_layout.addWidget
    scan.display.initGUI()
    scan.display.displayComboBox = core.CompactComboBox()
    scan.display.displayComboBox.currentIndexChanged.connect(scan.updateDisplayChannel)
    plot_layout.addWidget(scan.display.displayComboBox)
    scan.display.raiseDock = lambda *a, **kw: None
    scan.toggleDisplay = lambda *a, **kw: None
    scan.updateFile = lambda: None
    scan.displayActive = lambda: True
    scan.file = target / 'native.mscan.h5'
    window.resize(1420, 660)
    window.show()
    app.processEvents()

    before_calls = list(hw.calls)
    if scenario.startswith('legacy-'):
        legacy = target / ('old.ini' if scenario == 'legacy-ini' else 'old.h5')
        scan.saveSettings(file=legacy)
        if scenario == 'legacy-ini':
            import configparser
            config = configparser.ConfigParser()
            config.read(legacy)
            config['Display']['Value'] = 'DMMR_M03'
            config['Display']['Items'] = 'Detector,DMMR_M03,MissingOldDetector'
            with legacy.open('w') as f:
                config.write(f)
        else:
            with h5py.File(legacy, 'a') as f:
                display = f['MScan/Settings/Display']
                display.attrs['Value'] = 'DMMR_M03'
                display.attrs['Items'] = 'Detector,DMMR_M03,MissingOldDetector'
        scan.loadSettings(file=legacy)
        measured = scan.settingsMgr.settings[scan.DISPLAY]
        assert measured.value == 'DMMR_M03'
        assert measured.fixedItems and measured.text(0) == 'Measured signal'
        assert not {'Detector', 'MissingOldDetector'}.intersection(measured.items)
        assert scan.start == 60. and scan.stop == 80. and scan.step == 10.
        assert len(scan.outputChannels) == 1
        assert scan.outputChannels[0].sourceChannel is detector
        assert scan.settingsMgr.settings[scan.START].text(0) == 'Amplitude from (V)'
    if scenario == 'interface':
        # Placeholder, removed channel, duplicate name, and recording stopped:
        # each must block, with no PSU command and no fallback detector.
        measured.value = scan.NO_SIGNAL
        assert not scan.initScan() and 'Select a measured signal' in scan.scan_status
        measured.value = 'DMMR_M03'
        detector_device.channels.remove(detector)
        scan._refresh_interface()
        assert measured.value == 'DMMR_M03'
        assert 'unavailable' in measured.combo.itemData(measured.combo.currentIndex(), Qt.ItemDataRole.ToolTipRole)
        assert not scan.initScan() and 'unavailable' in scan.scan_status
        detector_device.channels.append(detector)
        detector_device.loading = True
        other = CurrentChannel(detector_device, detector_device.tree)
        detector_device.tree.addTopLevelItem(other)
        other.initGUI({'Name': 'DMMR_M03'})
        detector_device.channels.append(other)
        detector_device.loading = False
        scan._refresh_interface()
        assert not scan.initScan() and 'ambiguous' in scan.scan_status
        other.name = 'DMMR_M05'
        scan._refresh_interface()
        assert 'DMMR_M05' in measured.items, (measured.items, other.name, scan.finished, scan.recording,
            scan.loading, scan.settingsMgr.loading, manager.loading, [c.name for c in dm.channels()])
        assert measured.value == 'DMMR_M03'
        # The second *available* detector deliberately has no sample history.
        # It must not be acquired just because it is a choice in the combo.
        assert len(other.values.get()) == 0
        detector_device.recording = False
        assert not scan.initScan() and 'recording' in scan.scan_status
        detector_device.recording = True
        amx.psu_ch23 = 'PSU_A'
        scan._refresh_interface()
        assert 'Also affects CH2-CH3' in scan.settingsMgr.settings[scan.SUPPLIES].value
        amx.psu_ch23 = 'None'
        amx.controller.output_rows = None
        scan._refresh_interface()
        assert scan.settingsMgr.settings[scan.FREQUENCY].value == 'Unavailable'
        amx.controller.output_rows = [dict(state='Periodic', frequency='500 kHz',
            timing=dict(period=200, high=100, phase=i % 2 * 100)) for i in range(4)]
        amx.psu_ch01 = 'PSU_B'
        scan._refresh_interface()
        assert scan.settingsMgr.settings[scan.SUPPLIES].value == 'PSU_B: unavailable'
        amx.psu_ch01 = 'PSU_A'
        scan._refresh_interface()
    assert hw.calls == before_calls  # discovery, restoring settings and failed preflight never touch hardware

    def sample():
        assert QThread.currentThread() is app.thread()
        with controller.lock:
            controller._update_state()
        value = math_value(hw.voltages[0])
        if scenario == 'invalid' and hw.voltages[0] == 70.:
            value = np.nan
        detector.value = value
        detector.appendValue(lenT=len(detector_device.time.get()))
        detector_device.time.add(time.time())
    def math_value(v):
        return float(np.exp(-((v - 70) / 9) ** 2) * 1e-12)
    sample()
    timer = QTimer()
    timer.timeout.connect(sample)
    timer.start(8)
    # The protocol drives the actual PSU Parameter/worker path; original Scan plotting remains live.
    scan.signalComm.scanUpdateSignal.disconnect()
    scan.signalComm.scanUpdateSignal.connect(lambda done: scan.plot(update=not done, done=done))
    if scenario == 'native-start':
        scan.step = 3.
        spin = scan.settingsMgr.settings[scan.STEP].spin
        spin.setFocus()
        spin.lineEdit().selectAll()
        QTest.keyClicks(spin.lineEdit(), '10')
        assert spin.value() == 3. and spin.lineEdit().text() == '10'
        # The real start action consumes the text even without focus loss/Enter.
        scan.recording = True
        scan.toggleRecording()
        assert scan.step == 10.
        thread = scan.runThread
        assert thread is not None
    else:
        scan.recording = True
        scan.initData()
        assert scan.initScan(), f'initScan failed: {scan.scan_status}'
        scan.finished = False
        from threading import Thread
        thread = Thread(target=scan.runScan, args=(lambda: scan.recording,))
        thread.start()
    assert [c.name for c in scan.outputChannels] == ['DMMR_M03']
    assert not scan.inputChannelGroupItem.isHidden()
    assert scan.inputChannelGroupItem.childCount() == 1
    assert 'PSU_A' in scan.settingsMgr.settings[scan.SUPPLIES].value
    assert 'CH0 = CH1 = A' in scan.settingsMgr.settings[scan.SUPPLIES].value
    assert '500 kHz' in scan.settingsMgr.settings[scan.FREQUENCY].value
    for key in (scan.DISPLAY, scan.AMX, scan.OUTPUTS):
        assert not scan.settingsMgr.settings[key].getWidget().isEnabled()
    for key in (scan.START, scan.AVERAGE):
        assert scan.settingsMgr.settings[key].getWidget().isReadOnly()
    if scenario == 'native-start':
        before_settings = dict(scan.settingsMgr.settings)
        scan.loadSettings(useDefaultFile=True)
        assert scan.settingsMgr.settings == before_settings
        assert scan.step == 10.
    interrupted = False
    deadline = time.monotonic() + 12
    while thread.is_alive() and time.monotonic() < deadline:
        app.processEvents()
        if scenario in ('stop', 'off') and not interrupted and np.isfinite(scan.outputChannels[0].recordingData[0]):
            if scenario == 'stop':
                scan.recording = False  # Global Explorer stop path, not just the local button.
            else:
                controller._cancel_output_commands()
                psu.isOn = lambda: False
            interrupted = True
            actions = list(hw.calls)
        time.sleep(.001)
    thread.join(.5)
    assert not thread.is_alive(), 'Scan worker hung'
    timer.stop()
    app.processEvents()
    print('RESULT', scan._validation, 'HW', hw.calls)
    if scenario in ('stop', 'off'):
        assert interrupted
        assert scan._validation['status'] == ('stopped' if scenario == 'stop' else 'error')
        assert np.isnan(scan.outputChannels[0].recordingData[1:]).any()
        assert not any(call[0] == 'voltage' and call[2] in (100., 110.) for call in hw.calls), hw.calls
    else:
        assert scan._validation['status'] == 'completed', scan._validation
        assert hw.voltages == {0: 100., 1: 110.}, hw.voltages
        if scenario == 'invalid':
            assert np.isnan(scan.outputChannels[0].recordingData[1])
            assert scan._validation['point_status'][1] == 'invalid detector data'
        else:
            assert scan.outputChannels[0].recordingData == pytest.approx(
                [math_value(v) for v in (60, 70, 80)], rel=1e-6, abs=1e-20)
        assert (scan._validation['samples'] > 0).all()
    assert not any(call[0] in ('outputs', 'device', 'range') for call in hw.calls), hw.calls
    scan.saveData(scan.file)
    with h5py.File(scan.file) as f:
        assert f['MScan/Input Channels/Amplitude'][:].tolist() == [60, 70, 80]
        assert np.array_equal(f['MScan/Output Channels/DMMR_M03'][:], scan.outputChannels[0].recordingData, equal_nan=True)
        assert f['MScan/Validation'].attrs['status'] == scan._validation['status']
    scan.plot(done=True)
    app.processEvents()
    assert scan.display.ms.get_marker() == '.'  # isolated valid points must remain visible beside NaNs
    assert np.array_equal(scan.display.ms.get_ydata(), scan.outputChannels[0].recordingData, equal_nan=True)
    window.grab().save(str(target / f'mscan-{scenario}.png'))
    if scenario not in ('stop', 'off', 'invalid'):
        original = scan.outputChannels[0].recordingData.copy()
        scan.finished = True
        scan.loadData(scan.file)
        assert np.array_equal(scan.outputChannels[0].recordingData, original)
        assert scan._validation['status'] == 'completed'
        if scenario == 'legacy-hdf':
            # Existing files with multiple recorded outputs still load and plot
            # even if one of their original devices is no longer installed.
            with h5py.File(scan.file, 'a') as f:
                group = f['MScan/Output Channels']
                group.copy('DMMR_M03', 'Former_detector')
                group['Former_detector'][:] = original * 2
                for name in ('samples', 'finite_samples'):
                    group = f['MScan/Validation']
                    data = group[name][:]
                    del group[name]
                    group.create_dataset(name, data=np.column_stack((data, data)))
            scan.loadData(scan.file)
            assert [c.name for c in scan.outputChannels] == ['DMMR_M03', 'Former_detector']
            scan.plot(done=True)
            scan.display.displayComboBox.setCurrentText('Former_detector')
            app.processEvents()
            assert np.array_equal(scan.display.ms.get_ydata(), original * 2), (
                scan.getOutputIndex(), scan.display.displayComboBox.currentText(),
                scan.recording, scan.loading, scan.initializing, scan.settingsMgr.loading,
                scan.display.ms.get_ydata(), scan.outputChannels[1].recordingData, original * 2)
            scan._refresh_interface()
            assert scan.displayDefault == 'DMMR_M03'
            assert len(scan.outputChannels) == 2
    if scenario == 'off':
        assert len(errors) == 1 and 'source changed or was stopped' in errors[0], errors
    else:
        assert not errors, errors
    scan.finished = True
    assert scan.settingsMgr.settings[scan.DISPLAY].getWidget().isEnabled()
    if scenario == 'interface':
        # A late device/name update must not clear an acquired/loaded spectrum.
        original_channels = list(scan.outputChannels)
        original = scan.outputChannels[0].recordingData.copy()
        before_calls = list(hw.calls)
        other.name = 'DMMR_Renamed'
        QTest.qWait(650)  # real refresh timer, not only a direct helper call
        assert 'DMMR_Renamed' in measured.items and 'DMMR_M05' not in measured.items
        assert measured.value == 'DMMR_M03'
        assert scan.outputChannels == original_channels
        assert np.array_equal(scan.outputChannels[0].recordingData, original)
        assert hw.calls == before_calls
        sources_widget.hide()
        plot_widget.hide()
        window.resize(400, 600)
        QTest.qWait(50)
        window.grab().save(str(target / 'mscan-compact.png'))
        # Multiline supply summaries must remain visible at compact width / high DPI.
        amx.psu_ch23 = 'PSU_A'
        scan._refresh_interface()
        QTest.qWait(30)
        for case in ('shared', 'two-psus'):
            if case == 'two-psus':
                manager.plugins.append(NS(name='PSU_B'))
                amx.psu_ch23 = 'PSU_B'
                scan.amx_outputs = 'CH0-CH3'
                scan._refresh_interface()
                QTest.qWait(30)
            setting = scan.settingsMgr.settings[scan.SUPPLIES]
            label = setting.label
            bounds = label.fontMetrics().boundingRect(QRect(0, 0, label.width(), 10000),
                Qt.TextFlag.TextWordWrap, label.text())
            assert label.height() >= bounds.height(), (case, label.text(), label.size(), bounds)
            window.grab().save(str(target / f'mscan-compact-{case}.png'))
        amx.psu_ch23 = 'None'
        scan.amx_outputs = 'CH0-CH1'
        # Labels remain human-readable after another real settings reconstruction.
        scan.saveSettings(file=target / 'roundtrip.ini')
        scan.loadSettings(file=target / 'roundtrip.ini')
        assert scan.displayDefault == 'DMMR_M03'
        assert scan.settingsMgr.settings[scan.DISPLAY].text(0) == 'Measured signal'
        assert not scan.inputChannelGroupItem.isHidden()
    scan.closeGUI()
    assert not scan._interface_timer.isActive()
    window.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(run(sys.argv[1], Path(sys.argv[2])))
