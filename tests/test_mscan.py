"""Deterministic scan protocol and Qt dispatch tests; no drivers or real HV."""
from __future__ import annotations

from threading import Event, Thread
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest
from support.mscan import History


@pytest.mark.parametrize(('args', 'expected'), [((0, 1, .4), [0, .4, .8]), ((1, 0, .4), [1, .6, .2]),
    ((100, 80, 10), [100, 90, 80]), ((0, .3, .1), [0, .1, .2, .3])])


def test_steps_never_overshoot(mscan_module, args, expected):
    assert mscan_module.amplitude_steps(*args) == pytest.approx(expected)
    assert min(mscan_module.amplitude_steps(*args)) >= min(args[:2])
    assert max(mscan_module.amplitude_steps(*args)) <= max(args[:2])


@pytest.mark.parametrize('args', [(0, 0, 1), (-1, 1, 1), (0, 10, 0), (0, 10, -1), (0, 10, 20),
    (0, float('nan'), 1), (0, 1e6, 1e-9)])


def test_invalid_steps(mscan_module, args):
    with pytest.raises(mscan_module.ScanError):
        mscan_module.amplitude_steps(*args)


def test_call_state_is_independent_for_each_gui_request(mscan_module):
    first = mscan_module._Call(lambda: 1, Event())
    second = mscan_module._Call(lambda: 2, None)
    first.done.set()
    first.expired.set()
    first.result = 1
    first.error = mscan_module.ScanError('first request only')
    assert not second.done.is_set()
    assert not second.expired.is_set()
    assert second.result is None
    assert second.error is None
    assert second.cancel is None
    assert second.action() == 2


def test_provider_is_distinct_fork(mscan_module):
    assert mscan_module.providePlugins() == [mscan_module.MScan]
    assert mscan_module.MScan.name == 'MScan'
    assert not hasattr(mscan_module.MScan.Display, 'mzCalc')


def test_run_couples_rails_without_zero_and_restores_distinct_initials(mscan_rig):
    s = mscan_rig.scan
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed', s._validation
    assert s.outputChannels[0].recordingData == pytest.approx([10, 20, 30])
    assert s._validation['rail_v'] == pytest.approx(np.array([[10, 10], [20, 20], [30, 30]]))
    assert mscan_rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20), (0, 30), (1, 30), (0, 40), (1, 50)]
    assert mscan_rig.psus[1].writes == []
    assert (s._validation['samples'] > 0).all()


@pytest.mark.parametrize('change', ['virtual', 'non-dmmr', 'unit', 'module'])
def test_only_real_dmmr_modules_can_be_selected(mscan_rig, change):
    if change == 'virtual': mscan_rig.detector.real = False
    if change == 'non-dmmr': mscan_rig.detector_device.name = 'Other'
    if change == 'unit': mscan_rig.detector.unit = 'V'
    if change == 'module': mscan_rig.detector.module_address = lambda: -1
    with pytest.raises(mscan_rig.module.ScanError):
        mscan_rig.scan._preflight()
    assert mscan_rig.psus[0].writes == []


def test_changed_dmmr_module_cannot_replace_selected_module_mid_scan(mscan_rig):
    mscan_rig.scan._plan = mscan_rig.scan._preflight()
    mscan_rig.detector.module_address = lambda: 5
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation['status'] == 'error'
    assert 'DMMR module identity changed' in mscan_rig.scan._validation['error']
    assert np.isnan(mscan_rig.scan.outputChannels[0].recordingData).all()


def test_one_continuous_settling_duration_at_every_point_and_return(mscan_rig):
    s = mscan_rig.scan
    s._plan = s._preflight()
    times, original = [], s._settle
    def record(seconds):
        times.append(seconds)
        return original(seconds)
    s._settle = record
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed'
    assert times == [s.settling_s] * 4
    assert s._plan['average'] == s.integration_s
    assert s._plan['metadata']['settling_s'] == s.settling_s
    assert s._plan['metadata']['detector']['module'] == 3


def test_second_pair_resolves_other_psu(mscan_rig):
    mscan_rig.scan.amx_outputs = 'CH2-CH3'
    p = mscan_rig.scan._prepare()
    assert [r['device'] for r in p['rails']] == [mscan_rig.psus[1]] * 2


def test_selected_pair_only_commands_its_two_psu_rails(mscan_rig):
    mscan_rig.amx.psu_ch23 = 'PSU_A'
    assert len(mscan_rig.scan._prepare()['rails']) == 2
    mscan_rig.scan.amx_outputs = 'CH2-CH3'
    assert len(mscan_rig.scan._prepare()['rails']) == 2


@pytest.mark.parametrize(('attr', 'value'), [('enabled', False), ('active', False), ('initialized', False),
    ('unit', 'A'), ('monitor', np.nan), ('max', 25)])


def test_preflight_rejects_invalid_rail(mscan_rig, attr, value):
    setattr(mscan_rig.psus[0].channels[1], attr, value)
    with pytest.raises(mscan_rig.module.ScanError):
        mscan_rig.scan._prepare()
    assert mscan_rig.psus[0].writes == []


def test_command_prevalidates_both_targets(mscan_rig):
    mscan_rig.psus[0].channels[1].max = 45
    with pytest.raises(mscan_rig.module.ScanError):
        mscan_rig.scan._command([60, 60])
    assert mscan_rig.psus[0].writes == []


@pytest.mark.parametrize('change', ['waveform', 'mapping', 'cancel', 'manual', 'rename', 'duplicate', 'backend'])
def test_configuration_or_identity_change_aborts_before_write(mscan_rig, change):
    s, p = mscan_rig.scan, mscan_rig.psus[0]
    if change == 'waveform': mscan_rig.amx.controller.output_rows[0]['timing']['high'] += 1
    if change == 'mapping': mscan_rig.amx.psu_ch01 = 'PSU_B'
    if change == 'cancel': p.controller._output_cancel.set()
    if change == 'manual':
        p.channels[1]._value = 49
        p.channels[1].voltage_request_revision += 1
    if change == 'rename': p.channels[0].name = 'Renamed'
    if change == 'duplicate': mscan_rig.all_channels.append(NS(name=p.channels[0].name))
    if change == 'backend': p.controller.device = object()
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert np.isnan(s.outputChannels[0].recordingData).all()
    assert p.writes == []


def test_busy_psu_is_not_ready_even_if_old_value_matches(mscan_rig):
    p = mscan_rig.psus[0]
    p.controller._manual_apply_worker_running = True
    ready, _, _, _ = mscan_rig.scan._observation()
    assert not ready


def test_stop_after_first_point_does_not_restore_or_write_later(mscan_rig):
    s = mscan_rig.scan
    s.signalComm.scanUpdateSignal.emit = lambda done: s._cancel.set() if not done else None
    s.runScan(lambda: True)
    assert s._validation['status'] == 'stopped'
    assert s.outputChannels[0].recordingData[0] == pytest.approx(10)
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert mscan_rig.psus[0].writes == [(0, 10), (1, 10)]


def test_invalid_detector_sample_is_not_zero_or_nanmean(mscan_rig):
    mscan_rig.current = lambda: np.nan if mscan_rig.psus[0].channels[0].value == 10 else 0.
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation['status'] == 'completed'
    assert np.isnan(mscan_rig.scan.outputChannels[0].recordingData[0])
    assert mscan_rig.scan.outputChannels[0].recordingData[1:].tolist() == [0, 0]
    assert mscan_rig.scan._validation['point_status'][0] == 'invalid detector data'


def test_history_validation_rejects_reset_misalignment_and_lost_window(mscan_rig):
    s = mscan_rig.scan
    start, baseline = s._history_start()
    mscan_rig.detector.values = History()
    with pytest.raises(mscan_rig.module.ScanError, match='reset|misaligned'):
        s._read_window(start, start + .1, baseline)


def test_hdf5_preserves_missing_points_measurements_metadata_and_status(mscan_rig, tmp_path):
    s = mscan_rig.scan
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


def test_external_recording_setter_cancels_scan(mscan_rig):
    mscan_rig.scan.recording = False
    assert mscan_rig.scan._cancel.is_set()


def test_queued_gui_command_is_discarded_after_stop(mscan_module, monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    s = mscan_module.MScan.__new__(mscan_module.MScan)
    s._cancel = Event()
    s._bridge = mscan_module._GuiBridge(s)
    queued, result = Event(), []
    def worker():
        queued.set()
        try:
            s._gui(lambda: result.append('write'))
        except mscan_module.ScanStopped:
            pass
    t = Thread(target=worker)
    t.start()
    assert queued.wait(1)
    s._cancel.set()
    t.join(1)
    assert not t.is_alive()
    app.processEvents()
    assert result == []
