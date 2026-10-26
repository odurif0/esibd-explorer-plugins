"""Continuous scheduling, truthful data and fail-closed control, with a fake clock."""
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest
from support.mscan import run


@pytest.mark.parametrize('descending', [False, True])
def test_continuous_uses_fixed_cadence_and_only_initial_final_settling(mscan_continuous, descending):
    r, s = mscan_continuous, mscan_continuous.scan
    if descending:
        s.start, s.stop = s.stop, s.start
        s.inputChannels[0].recordingData = s.inputChannels[0].recordingData[::-1].copy()
        s._plan = s._preflight()
    calls, settle = [], s._settle
    def tracked(seconds):
        calls.append(seconds)
        return settle(seconds)
    s._settle = tracked
    v = run(r)
    assert v['status'] == 'completed', v['error']
    assert calls == [s.settling_s, s.settling_s]
    assert s.outputChannels[0].recordingData == pytest.approx(s.inputChannels[0].recordingData)
    # The first command is the approach, before the acquisition origin.
    assert np.diff(v['continuous']['command_start'][1:]) == pytest.approx([s.time_step_s], abs=1e-6)
    assert v['window_end'] - v['window_start'] == pytest.approx(np.full(3, s.time_step_s), abs=1e-6)
    assert v['window_end'][:-1] == pytest.approx(v['window_start'][1:])
    assert s._plan['metadata']['requested_rate_v_s'] == pytest.approx((-1 if descending else 1) * s.step / s.time_step_s)
    assert s._axis_label() == 'PSU A, commanded (V)'
    assert r.rig.psus[0].writes[-2:] == [(0, 40), (1, 50)]
    assert r.rig.psus[1].writes == []


def test_multipass_ends_each_sweep_then_settles_at_the_start_again(mscan_continuous):
    # User request 2026-10-26: Continuous repeats the sweep like Step by step does.
    r, s = mscan_continuous, mscan_continuous.scan
    s.passes = 2
    s.inputChannels[0].recordingData = s._scan_steps()
    s.outputChannels[0].recordingData = np.full(6, np.nan)
    s._plan = s._preflight()
    s._validation.update(point_status=['not acquired'] * 6, rail_v=np.full((6, 2), np.nan), rail_i=np.full((6, 2), np.nan),
                         window_start=np.full(6, np.nan), window_end=np.full(6, np.nan),
                         samples=np.zeros((6, 1), dtype=int), finite_samples=np.zeros((6, 1), dtype=int))
    calls, settle = [], s._settle
    def tracked(seconds):
        calls.append(seconds)
        return settle(seconds)
    s._settle = tracked
    shown = []
    s._bridge.status.emit = shown.append
    v = run(r)
    assert v['status'] == 'completed', v['error']
    assert calls == [s.settling_s] * 3  # before each pass, then after the restore
    assert s.outputChannels[0].recordingData == pytest.approx([10., 20., 30.] * 2)
    assert v['point_status'] == ['acquired during sweep'] * 6
    assert v['window_end'][[0, 1, 3, 4]] == pytest.approx(v['window_start'][[1, 2, 4, 5]])
    # The approach and settling between passes belong to no point.
    assert v['window_start'][3] - v['window_end'][2] >= s.settling_s - 1e-9
    assert np.isfinite(v['continuous']['command_start']).all()
    assert [w for w in r.rig.psus[0].writes if w[0] == 0] == [(0, a) for a in (10, 20, 30, 10, 20, 30, 40)]
    assert shown[:2] == ['pass 1/2, Initial settling at 10 V', 'pass 1/2, 2/6: continuous at 20 V'], shown
    assert 'pass 2/2, Initial settling at 10 V' in shown and 'pass 2/2, 6/6: continuous at 30 V' in shown, shown


def test_measurement_duration_does_not_silently_control_continuous_cadence(mscan_continuous):
    r = mscan_continuous
    r.scan.integration_s = -10  # unused, hidden stepped-only field
    r.scan._plan = r.scan._preflight()
    assert run(r)['status'] == 'completed'
    assert r.scan._plan['average'] == .04


def test_transition_samples_are_kept_not_discarded_or_called_settled(mscan_continuous):
    r, s = mscan_continuous, mscan_continuous.scan
    command = s._continuous_command
    def moving(amplitude, latest=None, **kwargs):
        result = command(amplitude, latest, **kwargs)
        r.clock.now += .0001
        r.rig.detector_device.time.data.append(r.clock.wall())
        r.rig.detector.values.data.append(1000.)
        return result
    s._continuous_command = moving
    v = run(r)
    assert v['status'] == 'completed', v['error']
    assert 1000. in v['continuous']['detector_current']
    assert s.outputChannels[0].recordingData[1] > 20.
    assert s.outputChannels[0].recordingData[2] > 30.
    assert v['point_status'] == ['acquired during sweep'] * 3


def test_detector_slower_than_time_step_aborts_instead_of_advancing_blindly(mscan_continuous):
    r = mscan_continuous
    last = [0.]
    def occasional():
        if r.clock.now - last[0] >= .06:
            last[0] = r.clock.now
            return True
        return False
    r.record = occasional
    v = run(r)
    assert v['status'] == 'error' and 'no new valid DMMR sample' in v['error']
    assert r.rig.psus[0].writes == [(0, 10), (1, 10)]
    assert np.isnan(v['continuous']['command_start'][1:]).all()
    assert np.isnan(r.scan.outputChannels[0].recordingData).all()


def test_rolling_history_is_safe_once_older_samples_have_been_copied(mscan_continuous):
    r = mscan_continuous
    def roll():
        if r.rig.current() == 20:
            r.rig.detector_device.time.data = r.rig.detector_device.time.data[-3:]
            r.rig.detector.values.data = r.rig.detector.values.data[-3:]
    r.tick = roll
    v = run(r)
    assert v['status'] == 'completed', v['error']
    assert r.scan.outputChannels[0].recordingData == pytest.approx([10., 20., 30.])
    assert len(v['continuous']['detector_time']) > 3


def test_loss_of_not_yet_copied_detector_history_still_aborts(mscan_continuous):
    r = mscan_continuous
    def lose():
        if r.rig.current() == 20:
            r.rig.detector_device.time.data.clear()
            r.rig.detector.values.data.clear()
    r.tick = lose
    v = run(r)
    assert v['status'] == 'error' and 'history buffer truncated' in v['error']
    assert r.rig.psus[0].writes[-2:] == [(0, 20), (1, 20)]
    assert np.isnan(r.scan.outputChannels[0].recordingData[1:]).all()


def test_missing_detector_aborts_without_zero_or_restoration(mscan_continuous):
    r = mscan_continuous
    r.record = lambda: False
    v = run(r)
    assert v['status'] == 'error' and 'no new valid DMMR sample' in v['error']
    assert np.isnan(r.scan.outputChannels[0].recordingData).all()
    assert r.rig.psus[0].writes == [(0, 10), (1, 10)]


def test_stop_retains_raw_partial_window_but_not_a_fictitious_point(mscan_continuous):
    r, s = mscan_continuous, mscan_continuous.scan
    def stop():
        if r.rig.current() == 20 and r.clock.now > .07:
            s._cancel.set()
    r.tick = stop
    v = run(r)
    assert v['status'] == 'stopped'
    assert r.rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20)]
    assert s.outputChannels[0].recordingData[0] == 10
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert 20. in v['continuous']['detector_current']
    assert np.isnan(v['continuous']['command_start'][2])


@pytest.mark.parametrize('failure', ['off', 'ilim', 'amx', 'module', 'setpoint', 'clock'])
def test_continuous_preserves_live_scan_guards(mscan_continuous, failure):
    r, s = mscan_continuous, mscan_continuous.scan
    def fail():
        if r.rig.current() != 20:
            return
        if failure == 'off': r.rig.psus[0].controller._output_cancel.set()
        if failure == 'ilim': r.rig.psus[0].channels[0].current_readback = .01
        if failure == 'amx': r.rig.amx.psu_ch01 = 'PSU_B'
        if failure == 'module': r.rig.detector.module_address = lambda: 5
        if failure == 'setpoint':
            r.rig.psus[0].channels[0]._value = 21.
            r.rig.psus[0].channels[0].voltage_request_revision += 1
        if failure == 'clock': r.clock.jump = 2.
    r.tick = fail
    v = run(r)
    assert v['status'] == 'error', v
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert r.rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20)]


@pytest.mark.parametrize('failure', ['busy', 'not-regulated'])
def test_psu_must_reach_target_before_next_command(mscan_continuous, failure):
    r, s = mscan_continuous, mscan_continuous.scan
    if failure == 'busy':
        r.busy = lambda: r.rig.current() == 20
    else:
        original = s._observation
        def foldback():
            if r.rig.current() == 20:
                r.rig.psus[0].channels[0].monitor = 17.
            return original()
        s._observation = foldback
    v = run(r)
    assert v['status'] == 'error' and 'within the continuous time step' in v['error']
    assert r.rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20)]
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()


def test_missed_deadline_never_skips_or_catches_up(mscan_continuous):
    r = mscan_continuous
    fired = [False]
    def delay():
        if not fired[0] and r.clock.now > .02:
            fired[0] = True
            r.clock.now += .1
    r.tick = delay
    v = run(r)
    assert v['status'] == 'error' and 'time step missed' in v['error']
    assert r.rig.psus[0].writes == [(0, 10), (1, 10)]
    assert np.isnan(r.scan.outputChannels[0].recordingData).all()


def test_small_lateness_does_not_create_a_short_catchup_interval(mscan_continuous):
    r = mscan_continuous
    fired = [False]
    def delay():
        if not fired[0] and r.clock.now > .02:
            fired[0] = True
            r.clock.now += .03  # less than a full missed interval
    r.tick = delay
    v = run(r)
    assert v['status'] == 'completed', v['error']
    commands = v['continuous']['command_start']
    assert commands[2] - commands[1] >= r.scan.time_step_s - 1e-6


def test_ready_only_after_deadline_does_not_excuse_slow_transition(mscan_continuous):
    r = mscan_continuous
    fired = [False]
    def late_completion():
        if not fired[0] and r.rig.psus[0].channels[0].value == 20:
            fired[0] = True
            r.clock.now += r.scan.time_step_s + .002
    r.tick = late_completion
    v = run(r)
    assert v['status'] == 'error'
    assert 'time step' in v['error']
    assert r.rig.psus[0].writes[-2:] == [(0, 20), (1, 20)]
    assert np.isnan(r.scan.outputChannels[0].recordingData[1:]).all()


def test_deadline_is_checked_after_validation_before_any_channel_write(mscan_continuous):
    r, s = mscan_continuous, mscan_continuous.scan
    observed = s._observation
    def slow_validation():
        r.clock.now += .1
        return observed()
    s._observation = slow_validation
    with pytest.raises(r.rig.module.ScanError, match='time step missed'):
        s._continuous_command(20., latest=.05)
    assert r.rig.psus[0].writes == []


def test_late_qt_callback_cannot_send_command(mscan_continuous):
    r = mscan_continuous
    with pytest.raises(r.rig.module.ScanError, match='time step missed'):
        r.scan._continuous_command(20, latest=r.clock.now)
    assert r.rig.psus[0].writes == []


def test_nan_remains_in_raw_data_and_invalidates_its_window(mscan_continuous):
    r = mscan_continuous
    r.sample = lambda: np.nan if r.rig.current() == 20 else r.rig.current()
    v = run(r)
    assert v['status'] == 'error' and 'no new valid DMMR sample' in v['error']
    assert np.isnan(r.scan.outputChannels[0].recordingData[1:]).all()
    assert np.isnan(v['continuous']['detector_current']).any()
    assert np.isnan(v['window_end'][1:]).all()
    assert r.rig.psus[0].writes[-2:] == [(0, 20), (1, 20)]


@pytest.mark.parametrize('mode,duration', [('Continuous', 0.), ('Continuous', np.nan), ('Unknown', .1)])
def test_invalid_mode_or_time_step_is_rejected_without_commands(mscan_continuous, mode, duration):
    r = mscan_continuous
    r.scan.scan_mode, r.scan.time_step_s = mode, duration
    with pytest.raises(r.rig.module.ScanError):
        r.scan._preflight()
    assert r.rig.psus[0].writes == []


def set_cadence(r, times, values, interval_ms=1):
    r.scan.sweep_rate = 10.  # allow cadence tests to vary Time step without exceeding the scan span
    r.rig.detector_device.time.data = [r.clock.wall() + t for t in times]
    r.rig.detector.values.data = values
    r.rig.detector_device.interval = interval_ms
    # This helper replaces the acquisition history, rather than editing a live
    # interval. Runtime edits have their own tests (including fresh-sample waits).
    r.scan._dmmr_cadence_epoch = None


@pytest.mark.parametrize('duration,allowed', [(.099, False), (.1, True), (.2, True)])
def test_time_step_must_cover_valid_module_interval_not_recorder_ticks(mscan_continuous, duration, allowed):
    r = mscan_continuous
    # Recording runs at 20 Hz, but only every other slot contains a new result.
    # Equal currents from distinct polling cycles remain separate samples.
    set_cadence(r, [-.2, -.15, -.1, -.05, 0.], [7., np.nan, 7., np.nan, 7.])
    r.scan.time_step_s = duration
    if allowed:
        plan = r.scan._preflight()
        assert plan['metadata']['detector_cadence']['minimum_time_step_s'] == pytest.approx(.1)
    else:
        with pytest.raises(r.rig.module.ScanError, match='Time step.*at least 0.1 s'):
            r.scan._preflight()
    assert r.rig.psus[0].writes == []


def test_time_step_uses_longest_recent_interval_not_median(mscan_continuous):
    r = mscan_continuous
    set_cadence(r, [-.45, -.35, -.25, 0.], [7.] * 4)
    r.scan.time_step_s = .1
    with pytest.raises(r.rig.module.ScanError, match='Time step.*at least 0.25 s'):
        r.scan._preflight()
    assert r.rig.psus[0].writes == []


def test_slower_configured_polling_cannot_use_old_fast_history(mscan_continuous):
    r = mscan_continuous
    set_cadence(r, [-.02, -.01, 0.], [1., 2., 3.], interval_ms=500)
    with pytest.raises(r.rig.module.ScanError, match='Time step.*at least 0.5 s'):
        r.scan._preflight()
    assert r.rig.psus[0].writes == []


@pytest.mark.parametrize('problem', ['no-history', 'one-value', 'nan', 'infinity', 'misaligned',
                                    'timestamps', 'stale', 'no-interval'])


def test_unavailable_module_cadence_blocks_continuous_before_commands(mscan_continuous, problem):
    r = mscan_continuous
    if problem == 'no-history': set_cadence(r, [], [])
    if problem == 'one-value': set_cadence(r, [-.02, -.01, 0.], [np.nan, np.nan, 1.])
    if problem == 'nan': r.rig.detector.values.data = [np.nan] * 3
    if problem == 'infinity': r.rig.detector.values.data = [np.inf] * 3
    if problem == 'misaligned': r.rig.detector.values.data.pop()
    if problem == 'timestamps': r.rig.detector_device.time.data[-1] = r.rig.detector_device.time.data[-2]
    if problem == 'stale': r.clock.now += 3.
    if problem == 'no-interval': r.rig.detector_device.interval = np.nan
    with pytest.raises(r.rig.module.ScanError):
        r.scan._preflight()
    assert r.rig.psus[0].writes == []


def test_cadence_is_specific_to_selected_module(mscan_continuous):
    r = mscan_continuous
    set_cadence(r, [-.2, -.15, -.1, -.05, 0.], [7.] * 5)
    slow = NS(**vars(r.rig.detector))
    slow.name, slow.module_address = 'Detector5', lambda: 5
    slow.getValues = lambda **kw: np.array([7., np.nan, 7., np.nan, 7.])
    r.rig.all_channels.append(slow)
    r.scan.time_step_s = .05
    assert r.scan._preflight()['detectors'] == [r.rig.detector]
    r.scan.detector_module = 'Module 5 — Detector5'
    with pytest.raises(r.rig.module.ScanError, match='Detector5.*Time step.*at least 0.1 s'):
        r.scan._preflight()
    assert r.rig.psus[0].writes == []


@pytest.mark.parametrize('mode', ['Continuous', 'Step by step'])
@pytest.mark.parametrize('interval_ms', [1, 100])
def test_live_polling_change_aborts_before_next_voltage_command(mscan_continuous, mode, interval_ms):
    r = mscan_continuous
    r.scan.scan_mode = mode
    r.scan._plan = r.scan._preflight()
    def change_interval():
        if r.rig.current() == 20:
            r.rig.detector_device.interval = interval_ms
    r.tick = change_interval
    v = run(r)
    assert v['status'] == 'error' and 'DMMR interval changed during the scan' in v['error']
    assert r.rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20)]
    assert np.isnan(r.scan.outputChannels[0].recordingData[1:]).all()
    if mode == 'Continuous':
        assert len(v['continuous']['detector_time']) > 0


def test_continuous_archive_roundtrip_preserves_raw_data_and_axis(mscan_continuous, tmp_path):
    r, s = mscan_continuous, mscan_continuous.scan
    v = run(r)
    assert v['status'] == 'completed', v['error']
    s.file = tmp_path / 'continuous.mscan.h5'
    s.saveData(s.file)
    with h5py.File(s.file) as f:
        raw = f['MScan/Validation/Continuous']
        assert raw['psu_v'].shape[1] == 2
        assert raw['psu_i'].shape == raw['psu_v'].shape
        assert raw['detector_current'][:] == pytest.approx(v['continuous']['detector_current'])
        assert 'not hardware-synchronized' in raw.attrs['timestamps']
        assert 'no background' in raw['detector_current'].attrs['Unit']
    original = v['continuous']['command_start'].copy()
    s.loadDataInternal()
    assert s._axis_label() == 'PSU A, commanded (V)'
    assert 'commanded (V)' in s.pythonPlotCode()
    assert s._validation['continuous']['command_start'] == pytest.approx(original)
