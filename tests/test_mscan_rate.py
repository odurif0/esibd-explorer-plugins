"""Sweep speed, actual command spacing and fresh cadence after a DMMR edit."""
import math
from types import SimpleNamespace as NS

import numpy as np
import pytest

from test_mscan import module, rig  # noqa: F401


@pytest.mark.parametrize('start, stop, expected', [
    (10., 14., [10., 11., 12., 13., 14.]),
    (14., 10., [14., 13., 12., 11., 10.]),
    (10., 13.5, [10., 11., 12., 13.]),
])
def test_continuous_step_is_speed_times_time_not_hidden_stepped_value(rig, start, stop, expected):
    s = rig.scan
    s.start, s.stop = start, stop
    s.scan_mode, s.sweep_rate, s.time_step_s = s.CONTINUOUS, 5., .2
    s.step = np.nan  # inactive stepped setting must not control the sweep
    assert s._scan_steps() == pytest.approx(expected)
    plan = s._prepare()
    assert plan['metadata']['command_step_v'] == pytest.approx(1.)
    assert plan['metadata']['requested_rate_v_s'] == math.copysign(5., stop - start)
    s.time_step_s = .4
    assert abs(np.diff(s._scan_steps())) == pytest.approx([2.] * (len(s._scan_steps()) - 1))
    assert s.sweep_rate == 5.  # changing timing retains speed, not old voltage step


def test_aligned_end_point_survives_float_roundoff_in_rate_times_time(rig):
    s = rig.scan
    s.start, s.stop = 10., 20.
    s.scan_mode, s.time_step_s = s.CONTINUOUS, .4
    s.sweep_rate = np.nextafter(25., np.inf)
    assert s._command_step() > 10.
    assert s._scan_steps() == pytest.approx([10., 20.])


def test_stepped_ignores_hidden_speed_and_time_step(rig):
    s = rig.scan
    s.scan_mode, s.sweep_rate, s.time_step_s = s.STEPPED, np.nan, -1.
    assert s._scan_steps() == pytest.approx([10., 20., 30.])
    assert s._prepare()['metadata']['requested_rate_v_s'] is None


@pytest.mark.parametrize('rate', [0., -1., np.nan, np.inf])
def test_invalid_speed_fails_before_any_command(rig, rate):
    s = rig.scan
    s.scan_mode, s.sweep_rate, s.time_step_s = s.CONTINUOUS, rate, .2
    with pytest.raises(rig.module.ScanError, match='Sweep rate'):
        s._prepare()
    assert not any(p.writes for p in rig.psus)


@pytest.mark.parametrize('rate, dt', [(1e308, 1e308), (1e-308, 1e-308), (1000., 1.)])
def test_derived_step_still_obeys_point_count_and_bounds(rig, rate, dt):
    s = rig.scan
    s.scan_mode, s.sweep_rate, s.time_step_s = s.CONTINUOUS, rate, dt
    with pytest.raises(rig.module.ScanError):
        s._prepare()
    assert not any(p.writes for p in rig.psus)


def test_interval_change_uses_only_fresh_samples_without_erasing_history(rig, monkeypatch):
    s, d, c = rig.scan, rig.detector_device, rig.detector
    wall = [10.]
    monkeypatch.setattr(rig.module, 'time', NS(time=lambda: wall[0]))
    d.interval = 1000
    d.time.data = [8., 9., 10.]
    c.values.data = [2., 2., 2.]
    assert s._detector_cadence(c)['minimum_time_step_s'] == 1.
    d.interval = 100
    with pytest.raises(rig.module.ScanError, match='two.*samples.*interval change'):
        s._detector_cadence(c)
    d.time.data.append(10.1)
    c.values.data.append(2.)
    wall[0] = 10.1
    with pytest.raises(rig.module.ScanError, match='two.*samples.*interval change'):
        s._detector_cadence(c)
    d.time.data.append(10.2)
    c.values.data.append(2.)
    wall[0] = 10.2
    cadence = s._detector_cadence(c)
    assert cadence['minimum_time_step_s'] == .1
    assert cadence['sample_count'] == 2
    assert d.time.data[:3] == [8., 9., 10.]
    # A second module shares the edit epoch, not the first module's speed.
    other = NS(**vars(c))
    other.name = 'Detector5'
    other.module_address = lambda: 5
    other.getValues = lambda **kw: np.array([1., 1., 1., np.nan, 1.])
    with pytest.raises(rig.module.ScanError, match='two.*samples.*interval change'):
        s._detector_cadence(other)


def test_nan_recorder_ticks_after_interval_edit_are_not_new_cadence(rig, monkeypatch):
    s, d, c = rig.scan, rig.detector_device, rig.detector
    wall = [10.]
    monkeypatch.setattr(rig.module, 'time', NS(time=lambda: wall[0]))
    d.interval = 1000
    d.time.data, c.values.data = [8., 9., 10.], [1., 1., 1.]
    s._detector_cadence(c)
    d.interval = 100
    with pytest.raises(rig.module.ScanError):
        s._detector_cadence(c)
    wall[0] = 10.3
    d.time.data += [10.1, 10.2, 10.3]
    c.values.data += [np.nan, 1., np.nan]
    with pytest.raises(rig.module.ScanError, match='two.*samples'):
        s._detector_cadence(c)
    assert len(d.time.data) == 6


def test_recovery_gap_stays_in_cadence_until_replaced_by_real_finite_replies(rig, monkeypatch):
    s, d, c = rig.scan, rig.detector_device, rig.detector
    wall = [10.]
    monkeypatch.setattr(rig.module, 'time', NS(time=lambda: wall[0]))
    d.interval = 100
    d.time.data = [9.6, 9.7, 9.8, 9.9, 10.]
    c.values.data = [1., np.nan, np.nan, np.nan, 2.]
    assert s._detector_cadence(c)['minimum_time_step_s'] == .4
    with pytest.raises(rig.module.ScanError, match='Time step must be at least'):
        s._require_time_step(c, .1)
    original = list(c.values.data)
    for index in range(1, 22):
        wall[0] = 10. + index * .1
        d.time.data.append(wall[0])
        c.values.data.append(2.)
    assert s._detector_cadence(c)['minimum_time_step_s'] == .1
    np.testing.assert_equal(c.values.data[:5], original)
    assert not any(p.writes for p in rig.psus)


def test_missing_reply_after_recovery_cannot_advance_a_scan_window(rig, monkeypatch):
    s, d, c = rig.scan, rig.detector_device, rig.detector
    monkeypatch.setattr(rig.module, 'time', NS(time=lambda: 10.2))
    d.interval = 100
    d.time.data, c.values.data = [9.9, 10., 10.1, 10.2], [1., 1., np.nan, np.nan]
    with pytest.raises(rig.module.ScanError, match='no new valid DMMR sample'):
        s._require_time_step(c, .1, window_start=10.)
    assert not any(p.writes for p in rig.psus)
