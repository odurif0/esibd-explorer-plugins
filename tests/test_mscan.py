"""Deterministic scan protocol and Qt dispatch tests; no drivers or real HV."""
from __future__ import annotations

import importlib.util
import math
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace as NS, ModuleType
import sys

import h5py
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module(monkeypatch):
    pytest.importorskip('PyQt6.QtCore')
    core, plugins = ModuleType('esibd.core'), ModuleType('esibd.plugins')
    class Scan:
        Display = type('Display', (), {})
        recording = property(lambda s: s._recording, lambda s, value: setattr(s, '_recording', value))
        def saveData(self, file):
            with h5py.File(file, 'w') as f:
                group = f.create_group(self.name)
                group.create_group('Input Channels').create_dataset('Amplitude', data=self.inputChannels[0].recordingData)
                for c in self.outputChannels:
                    group.require_group('Output Channels').create_dataset(c.name, data=c.recordingData)
        def loadDataInternal(self):
            return True
    plugins.Scan = Scan
    core.INOUT = NS(IN='IN', OUT='OUT')
    core.PRINT = NS(WARNING='warning', ERROR='error')
    core.MetaChannel = type('MetaChannel', (), {})
    core.Parameter, core.PARAMETERTYPE = NS(), NS()
    core.parameterDict = lambda **kw: kw
    core.plotting = lambda fn: fn
    monkeypatch.setitem(sys.modules, 'esibd.core', core)
    monkeypatch.setitem(sys.modules, 'esibd.plugins', plugins)
    spec = importlib.util.spec_from_file_location('mscan_protocol_test', ROOT / 'mscan/mscan_plugin.py')
    result = importlib.util.module_from_spec(spec)
    # Explorer's dynamicImport executes without registering the entrypoint.
    monkeypatch.delitem(sys.modules, spec.name, raising=False)
    spec.loader.exec_module(result)
    assert spec.name not in sys.modules
    return result


class History:
    def __init__(self):
        self.data = []
    def get(self, length=None):
        return np.asarray(self.data[-length:] if length else self.data, dtype=float)


class Rail:
    def __init__(self, parent, index):
        self.parent, self.id, self.number = parent, 100 + index, index
        self.name = f'{parent.name}_CH{index}'
        self._value, self.monitor = 40. + 10 * index, 40. + 10 * index
        self.real = self.enabled = self.active = self.initialized = self.useMonitors = True
        self.unit, self.readback_status = 'V', 'Valid'
        self.min, self.max = 0., 250.
    def channel_number(self):
        return self.number
    @property
    def value(self):
        return self._value
    @value.setter
    def value(self, v):
        self.parent.writes.append((self.number, v))
        self._value = v
        self.parent.controller._manual_apply_worker_running = True
        self.monitor = np.nan


@pytest.fixture
def rig(module):
    scan = module.MScan.__new__(module.MScan)
    scan._cancel, scan._recording = Event(), True
    scan.amx_name, scan.amx_outputs = 'AMX_A', 'CH0-CH1'
    scan.start, scan.stop, scan.step = 10., 30., 10.
    scan.wait = scan.waitLong = 3
    scan.average, scan.settle_timeout, scan.voltage_tolerance, scan.largestep = 12, .15, 1., 5.
    rows = [dict(state='Periodic', timing={'period': 200, 'high': 100, 'phase': i % 2 * 100}) for i in range(4)]
    amx = NS(name='AMX_A', psu_ch01='PSU_A', psu_ch23='PSU_B', isOn=lambda: True,
        controller=NS(initialized=True, device=object(), initializing=False, transitioning=False, output_rows=rows))
    psus = []
    for name in ('PSU_A', 'PSU_B'):
        p = NS(name=name, writes=[], controller=NS(initialized=True, device=object(), _output_cancel=Event(),
               _manual_apply_worker_running=False), isOn=lambda: True)
        p.channels = [Rail(p, i) for i in (0, 1)]
        p.getChannels = lambda p=p: p.channels
        psus.append(p)
    detector_device = NS(initialized=True, acquiring=True, recording=True, time=History())
    detector = NS(name='Detector', enabled=True, initialized=True, acquiring=True,
                  values=History(), getDevice=lambda: detector_device)
    detector.getValues = lambda **kw: detector.values.get()
    all_channels = [c for p in psus for c in p.channels] + [detector]
    manager = NS(plugins=[amx, *psus, scan])
    manager.DeviceManager = NS(channels=lambda: all_channels,
        getChannelByName=lambda name: next((c for c in all_channels if c.name.lower() == name.lower()), None))
    scan.pluginManager = manager
    scan._gui = lambda action, **kw: action()
    scan._bridge = NS(status=NS(emit=lambda text: setattr(scan, 'scan_status', text)))
    scan.signalComm = NS(scanUpdateSignal=NS(emit=lambda done: None), updateRecordingSignal=NS(emit=lambda value: None))
    scan.print = lambda *a, **kw: None
    scan.POLL_S = .002
    scan._plan = scan._prepare()
    scan._plan['detectors'] = [detector]
    scan.inputChannels = [NS(recordingData=np.asarray([10., 20., 30.]))]
    scan.outputChannels = [NS(name='Detector', recordingData=np.full(3, np.nan))]
    scan._validation = dict(status='running', error='', point_status=['not acquired'] * 3,
        rail_v=np.full((3, len(scan._plan['rails'])), np.nan), window_start=np.full(3, np.nan), window_end=np.full(3, np.nan),
        samples=np.zeros((3, 1), dtype=int), finite_samples=np.zeros((3, 1), dtype=int))
    r = NS(module=module, scan=scan, psus=psus, amx=amx, detector=detector,
           detector_device=detector_device, all_channels=all_channels,
           current=lambda: psus[0].channels[0].value, on_pause=lambda: None)
    def pause():
        r.on_pause()
        if scan._cancel.wait(scan.POLL_S):
            raise module.ScanStopped('Scan stopped.')
        for p in psus:
            p.controller._manual_apply_worker_running = False
            for c in p.channels:
                c.monitor = c.value
        detector_device.time.data.append(time.time())
        detector.values.data.append(r.current())
    scan._pause = pause
    detector_device.time.data.append(time.time() - .01)
    detector.values.data.append(40.)
    return r


@pytest.mark.parametrize(('args', 'expected'), [((0, 1, .4), [0, .4, .8]), ((1, 0, .4), [1, .6, .2]),
    ((100, 80, 10), [100, 90, 80]), ((0, .3, .1), [0, .1, .2, .3])])
def test_steps_never_overshoot(module, args, expected):
    assert module.amplitude_steps(*args) == pytest.approx(expected)
    assert min(module.amplitude_steps(*args)) >= min(args[:2])
    assert max(module.amplitude_steps(*args)) <= max(args[:2])


@pytest.mark.parametrize('args', [(0, 0, 1), (-1, 1, 1), (0, 10, 0), (0, 10, -1), (0, 10, 20),
    (0, float('nan'), 1), (0, 1e6, 1e-9)])
def test_invalid_steps(module, args):
    with pytest.raises(module.ScanError):
        module.amplitude_steps(*args)


def test_call_state_is_independent_for_each_gui_request(module):
    first = module._Call(lambda: 1, Event())
    second = module._Call(lambda: 2, None)
    first.done.set()
    first.expired.set()
    first.result = 1
    first.error = module.ScanError('first request only')
    assert not second.done.is_set()
    assert not second.expired.is_set()
    assert second.result is None
    assert second.error is None
    assert second.cancel is None
    assert second.action() == 2


def test_provider_is_distinct_fork(module):
    assert module.providePlugins() == [module.MScan]
    assert module.MScan.name == 'MScan'
    assert not hasattr(module.MScan.Display, 'mzCalc')


def test_run_couples_rails_without_zero_and_restores_distinct_initials(rig):
    s = rig.scan
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed', s._validation
    assert s.outputChannels[0].recordingData == pytest.approx([10, 20, 30])
    assert s._validation['rail_v'] == pytest.approx(np.array([[10, 10], [20, 20], [30, 30]]))
    assert rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20), (0, 30), (1, 30), (0, 40), (1, 50)]
    assert rig.psus[1].writes == []
    assert (s._validation['samples'] > 0).all()


def test_second_pair_resolves_other_psu(rig):
    rig.scan.amx_outputs = 'CH2-CH3'
    p = rig.scan._prepare()
    assert [r['device'] for r in p['rails']] == [rig.psus[1]] * 2


def test_all_outputs_deduplicate_shared_psu(rig):
    rig.scan.amx_outputs = 'CH0-CH3'
    assert len(rig.scan._prepare()['rails']) == 4
    rig.amx.psu_ch23 = 'PSU_A'
    assert len(rig.scan._prepare()['rails']) == 2


@pytest.mark.parametrize(('attr', 'value'), [('enabled', False), ('active', False), ('initialized', False),
    ('unit', 'A'), ('monitor', np.nan), ('max', 25)])
def test_preflight_rejects_invalid_rail(rig, attr, value):
    setattr(rig.psus[0].channels[1], attr, value)
    with pytest.raises(rig.module.ScanError):
        rig.scan._prepare()
    assert rig.psus[0].writes == []


def test_command_prevalidates_both_targets(rig):
    rig.psus[0].channels[1].max = 45
    with pytest.raises(rig.module.ScanError):
        rig.scan._command([60, 60])
    assert rig.psus[0].writes == []


@pytest.mark.parametrize('change', ['waveform', 'mapping', 'cancel', 'manual', 'rename', 'duplicate', 'backend'])
def test_configuration_or_identity_change_aborts_before_write(rig, change):
    s, p = rig.scan, rig.psus[0]
    if change == 'waveform': rig.amx.controller.output_rows[0]['timing']['high'] += 1
    if change == 'mapping': rig.amx.psu_ch01 = 'PSU_B'
    if change == 'cancel': p.controller._output_cancel.set()
    if change == 'manual': p.channels[1]._value = 49
    if change == 'rename': p.channels[0].name = 'Renamed'
    if change == 'duplicate': rig.all_channels.append(NS(name=p.channels[0].name))
    if change == 'backend': p.controller.device = object()
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert np.isnan(s.outputChannels[0].recordingData).all()
    assert p.writes == []


def test_busy_psu_is_not_ready_even_if_old_value_matches(rig):
    p = rig.psus[0]
    p.controller._manual_apply_worker_running = True
    ready, _, _ = rig.scan._observation()
    assert not ready


def test_stop_after_first_point_does_not_restore_or_write_later(rig):
    s = rig.scan
    s.signalComm.scanUpdateSignal.emit = lambda done: s._cancel.set() if not done else None
    s.runScan(lambda: True)
    assert s._validation['status'] == 'stopped'
    assert s.outputChannels[0].recordingData[0] == pytest.approx(10)
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert rig.psus[0].writes == [(0, 10), (1, 10)]


def test_invalid_detector_sample_is_not_zero_or_nanmean(rig):
    rig.current = lambda: np.nan if rig.psus[0].channels[0].value == 10 else 0.
    rig.scan.runScan(lambda: True)
    assert rig.scan._validation['status'] == 'completed'
    assert np.isnan(rig.scan.outputChannels[0].recordingData[0])
    assert rig.scan.outputChannels[0].recordingData[1:].tolist() == [0, 0]
    assert rig.scan._validation['point_status'][0] == 'invalid detector data'


def test_history_validation_rejects_reset_misalignment_and_lost_window(rig):
    s = rig.scan
    start, baseline = s._history_start()
    rig.detector.values = History()
    with pytest.raises(rig.module.ScanError, match='reset|misaligned'):
        s._read_window(start, start + .1, baseline)


def test_hdf5_preserves_missing_points_measurements_metadata_and_status(rig, tmp_path):
    s = rig.scan
    s.signalComm.scanUpdateSignal.emit = lambda done: s._cancel.set() if not done else None
    s.runScan(lambda: True)
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path) as f:
        assert f['MScan/Validation'].attrs['status'] == 'stopped'
        assert np.isnan(f['MScan/Output Channels/Detector'][1:]).all()
        assert np.isnan(f['MScan/Validation/rail_v'][1:]).all()
        assert 'not m/z' in f['MScan/Validation'].attrs['setup']
    s.file = path
    assert s.loadDataInternal()
    assert s._validation['status'] == 'stopped'
    assert s._validation['point_status'][-1] == 'not acquired'


def test_external_recording_setter_cancels_scan(rig):
    rig.scan.recording = False
    assert rig.scan._cancel.is_set()


def test_queued_gui_command_is_discarded_after_stop(module, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    s = module.MScan.__new__(module.MScan)
    s._cancel = Event()
    s._bridge = module._GuiBridge(s)
    queued, result = Event(), []
    def worker():
        queued.set()
        try:
            s._gui(lambda: result.append('write'))
        except module.ScanStopped:
            pass
    t = Thread(target=worker)
    t.start()
    assert queued.wait(1)
    s._cancel.set()
    t.join(1)
    assert not t.is_alive()
    app.processEvents()
    assert result == []
