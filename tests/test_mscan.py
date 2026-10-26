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
    if attr == 'active':
        mscan_rig.psus[0].channels[1].equation = 'PSU_A_CH0'  # Explorer drives it, not the operator
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


def test_recorder_ticks_without_a_reading_are_skipped(mscan_rig):
    # 2026-10-26 lab: DMMR records NaN at each tick without a new reading (about 5 of 66
    # samples per point); requiring every sample finite left every point of every scan NaN.
    ticks = iter(range(10_000))
    mscan_rig.current = lambda: np.nan if next(ticks) % 3 == 0 else 2e-12
    mscan_rig.scan.runScan(lambda: True)
    validation = mscan_rig.scan._validation
    assert validation['status'] == 'completed'
    assert mscan_rig.scan.outputChannels[0].recordingData == pytest.approx([2e-12] * 3)
    assert validation['point_status'] == ['acquired'] * 3
    assert (validation['finite_samples'] < validation['samples']).any()


def test_an_infinite_reading_invalidates_its_point(mscan_rig):
    mscan_rig.current = lambda: np.inf if mscan_rig.psus[0].channels[0].value == 10 else 0.
    mscan_rig.scan.runScan(lambda: True)
    assert np.isnan(mscan_rig.scan.outputChannels[0].recordingData[0])
    assert mscan_rig.scan._validation['point_status'][0] == 'invalid detector data'


def test_other_enabled_dmmr_modules_are_recorded_over_the_reference_windows(mscan_rig):
    # 2026-10-26: record every active DMMR module, not only the selected one.
    s, r = mscan_rig.scan, mscan_rig
    extra = NS(name='DMMR_M05', real=True, unit='A', module_address=lambda: 5, enabled=True, initialized=True,
               acquiring=True, values=History(), getDevice=lambda: r.detector_device)
    extra.getValues = lambda **kw: extra.values.get()
    extra.values.data.append(np.nan)  # Same recorder ticks as the reference module.
    r.on_pause = lambda: extra.values.data.append(np.nan if len(extra.values.data) % 4 == 0 else 3 * r.current())
    s._plan['extra_detectors'] = [extra]
    s.outputChannels.append(NS(name='DMMR_M05', recordingData=np.full(3, np.nan)))
    for key in ('samples', 'finite_samples'):
        s._validation[key] = np.zeros((3, 2), dtype=int)
    s.runScan(lambda: True)
    v = s._validation
    assert v['status'] == 'completed'
    assert s.outputChannels[1].recordingData == pytest.approx(3 * s.outputChannels[0].recordingData)
    assert (v['finite_samples'][:, 1] > 0).all() and (v['finite_samples'][:, 1] <= v['samples'][:, 1]).all()
    assert v['point_status'] == ['acquired'] * 3


def test_an_extra_module_without_readings_never_invalidates_the_reference(mscan_rig):
    s, r = mscan_rig.scan, mscan_rig
    silent = NS(name='DMMR_M06', getDevice=lambda: r.detector_device, getValues=lambda **kw: np.array([]))
    s._plan['extra_detectors'] = [silent]
    s.outputChannels.append(NS(name='DMMR_M06', recordingData=np.full(3, np.nan)))
    for key in ('samples', 'finite_samples'):
        s._validation[key] = np.zeros((3, 2), dtype=int)
    s.runScan(lambda: True)
    assert s._validation['point_status'] == ['acquired'] * 3
    assert np.isnan(s.outputChannels[1].recordingData).all()
    assert (s._validation['samples'][:, 1] == 0).all()


def test_an_inactive_rail_without_equation_is_operator_set(mscan_rig):
    # Explorer evaluates only inactive channels that have an equation (AMPR shows the flag as
    # "Manual"); on 2026-10-26 every AMPR channel had it unticked with no equation.
    mscan_rig.psus[0].channels[1].active = False
    mscan_rig.psus[0].channels[1].equation = ''
    assert len(mscan_rig.scan._prepare()['rails']) == 2


def test_passes_repeat_the_sweep_in_both_modes(mscan_rig):
    # User requests 2026-10-26: the same sweep N times in one file, Continuous included.
    s = mscan_rig.scan
    s.passes = 3
    assert s._scan_steps().tolist() == [10., 20., 30.] * 3
    s.scan_mode, s.time_step_s, s.sweep_rate = s.CONTINUOUS, .04, 250.
    assert s._passes() == 3 and s._scan_steps().tolist() == [10., 20., 30.] * 3
    s.scan_mode, s.passes = s.STEPPED, 0
    with pytest.raises(mscan_rig.module.ScanError, match='Passes'):
        s._scan_steps()


def test_a_multipass_reference_scan_measures_every_pass(mscan_rig):
    s = mscan_rig.scan
    s.passes = 2
    s._plan = s._prepare()
    s._plan['detectors'] = [mscan_rig.detector]
    s.inputChannels[0].recordingData = s._scan_steps()
    s.outputChannels[0].recordingData = np.full(6, np.nan)
    s._validation.update(point_status=['not acquired'] * 6, rail_v=np.full((6, 2), np.nan), rail_i=np.full((6, 2), np.nan),
                         window_start=np.full(6, np.nan), window_end=np.full(6, np.nan),
                         samples=np.zeros((6, 1), dtype=int), finite_samples=np.zeros((6, 1), dtype=int))
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed'
    assert s.outputChannels[0].recordingData.tolist() == [10., 20., 30.] * 2
    assert s._plan['metadata']['passes'] == 2 and s._plan['metadata']['points_per_pass'] == 3
