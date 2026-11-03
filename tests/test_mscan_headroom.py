"""PSU regulator headroom (dropout) in MScan.

Lab 2026-11-03: PSU_A in Half range could not regulate above about 248 V at 150 mA (dropout
0.05 V at the 250 V point); the scan waited for its whole settle timeout and ended on a generic
message. User request: detect it. Start is refused above output + dropout − 2 V, a rail out of
tolerance without headroom stops the scan at once, and points measured with less than 2 V of
headroom are flagged in the file.
"""
import time

import h5py
import numpy as np
import pytest


def headroom(r, dropouts, full=None, psu=0):
    ctrl = r.psus[psu].controller
    ctrl.dropout_values = dict(dropouts)
    if full is not None:
        ctrl.full_range_by_channel = {0: full, 1: full}
    return ctrl


def with_validation(s):
    n, rails = len(s.inputChannels[0].recordingData), len(s._plan['rails'])
    s._validation.update(rail_dropout=np.full((n, rails), np.nan), low_headroom=np.zeros(n, dtype=bool))


def test_start_is_refused_above_what_the_psu_can_reach_now(mscan_rig):
    s = mscan_rig.scan
    headroom(mscan_rig, {0: 15., 1: 100.}, full=False)  # CH0 at 40 V: at most 55 V at this load.
    s.stop = 52.
    assert s._prepare()['rails']  # 52 V ≤ 55 − 2 V
    s.stop = 60.
    with pytest.raises(mscan_rig.module.ScanError, match=r'PSU_A_CH0: at most about 55 V .*Half range.*Switch PSU_A to Full range'):
        s._prepare()
    headroom(mscan_rig, {0: 15., 1: 100.}, full=True)
    with pytest.raises(mscan_rig.module.ScanError, match='Lower Amplitude to'):
        s._prepare()
    headroom(mscan_rig, {0: np.nan, 1: np.nan})  # A PSU that does not report its dropout is not judged.
    assert s._prepare()['rails']
    assert mscan_rig.psus[0].writes == []


def test_a_scan_close_to_the_ceiling_starts_with_a_warning(mscan_rig):
    # Ceiling 100 V (40 V + 60 V of dropout). Refused above 98 V; warned above 100 − (2 + 3 % × rise).
    s = mscan_rig.scan
    headroom(mscan_rig, {0: 60., 1: 200.}, full=False)
    s.stop = 95.
    assert s._prepare()['headroom_warning'] is None  # 95 < 100 − (2 + 0.03 × 55)
    s.stop = 97.
    assert s._prepare()['headroom_warning'] == ('PSU_A_CH0: about 100 V reachable now (Half range), less under load: '
                                                'Full range recommended.')
    headroom(mscan_rig, {0: 60., 1: 200.}, full=True)
    assert s._prepare()['headroom_warning'].endswith('Amplitude to is close to the PSU ceiling.')
    s.stop = 99.
    with pytest.raises(mscan_rig.module.ScanError, match='at most about 100 V'):
        s._prepare()
    assert mscan_rig.psus[0].writes == []


def test_the_readback_shows_headroom_and_range(mscan_rig):
    s = mscan_rig.scan
    headroom(mscan_rig, {0: 212.5, 1: 210.}, full=False)
    rails = s._prepare()['rails']
    lines = s._readback_text(rails).splitlines()
    assert lines[0] == 'PSU_A_CH0: +40.00 V (set +40 V), headroom 212.5 V, Half range'
    ceiling, rail, full = s._headroom_ceiling(rails)
    assert (ceiling, rail['name'], full) == (252.5, 'PSU_A_CH0', False)


def cap(r, limit, dropout_at_limit=.05, wobble=True):
    """PSU_A CH0 cannot exceed ``limit``: above it the output stays there, out of tolerance, with no headroom."""
    s, pause = r.scan, r.scan._pause
    ctrl, c = r.psus[0].controller, r.psus[0].channels[0]
    ctrl.full_range_by_channel = {0: False, 1: False}
    ticks = [0]
    def capped(*a, **kw):
        pause(*a, **kw)
        ticks[0] += 1
        if c.value > limit:
            c.monitor = limit - (.4 if wobble and ticks[0] % 2 else .5)  # Two different readings.
            ctrl.dropout_values = {0: dropout_at_limit, 1: 100.}
        else:
            ctrl.dropout_values = {0: limit - c.value, 1: 100.}
    s._pause = capped


def test_a_rail_without_headroom_stops_the_scan_at_once_with_the_cause(mscan_rig):
    s = mscan_rig.scan
    s.settle_timeout = 5.
    s._plan = s._prepare()
    with_validation(s)
    s._plan['detectors'] = [mscan_rig.detector]
    cap(mscan_rig, 25.)
    started = time.monotonic()
    s.runScan(lambda: True)
    v = s._validation
    assert time.monotonic() - started < 2., 'stopped by the guard, not by the 5 s settle timeout'
    assert v['status'] == 'error'
    assert 'PSU_A_CH0 cannot reach 30 V: PSU_A has no headroom left (dropout 0.05 V' in v['error'], v['error']
    assert 'Half range' in v['error'] and 'Switch PSU_A to Full range' in v['error']
    assert v['point_status'][:2] == ['acquired'] * 2 and v['point_status'][2] == 'error'
    assert mscan_rig.psus[0].writes[-1] == (1, 30)  # Stop holds the last values: no restoration.


def test_a_single_reading_never_stops_the_scan(mscan_rig):
    s = mscan_rig.scan
    s._plan = s._prepare()
    with_validation(s)
    s._plan['detectors'] = [mscan_rig.detector]
    cap(mscan_rig, 25., wobble=False)  # The same reading repeated: a stale value, not a second reading.
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert 'settling timed out' in s._validation['error'] and 'headroom' not in s._validation['error']


def test_points_measured_with_little_headroom_are_flagged_in_the_file(mscan_rig, tmp_path):
    s = mscan_rig.scan
    s._plan = s._prepare()
    with_validation(s)
    s._plan['detectors'] = [mscan_rig.detector]
    ctrl, c, pause = mscan_rig.psus[0].controller, mscan_rig.psus[0].channels[0], s._pause
    ctrl.full_range_by_channel = {0: False, 1: False}
    def tight(*a, **kw):
        pause(*a, **kw)
        ctrl.dropout_values = {0: 31.5 - c.value if c.value <= 30 else 20., 1: 100.}  # 30 V with 1.5 V of headroom.
    s._pause = tight
    printed = []
    s.print = lambda message, flag=None: printed.append(message)
    s.runScan(lambda: True)
    v = s._validation
    assert v['status'] == 'completed', v['error']
    assert v['low_headroom'].tolist() == [False, False, True]
    assert v['rail_dropout'][:, 0] == pytest.approx([21.5, 11.5, 1.5])
    assert any('1 point(s) measured with less than 2 V of PSU headroom (A = 30 V)' in m for m in printed), printed
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path) as f:
        group = f['MScan/Validation']
        assert group['low_headroom'][:].tolist() == [False, False, True]
        assert 'dropout' in group['rail_dropout'].attrs['Unit']
    s.file = path
    assert s.loadDataInternal()
    assert s._validation['low_headroom'].tolist() == [False, False, True]
