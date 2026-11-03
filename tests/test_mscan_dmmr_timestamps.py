"""A repeated or earlier DMMR timestamp is skipped, never fatal to later scans.

Lab 2026-11-03, v0.5.8: at 15:57:31 a record with a repeated or earlier time.time() stamp entered the
DMMR history (Explorer stamps each record; a clock adjustment or two records within the Windows clock
resolution). The scan stopped with `Invalid or nonmonotonic DMMR timestamps`, and every later one too,
even after restarting the DMMR plugin, which keeps its history. User: "à fixer".
"""
import numpy as np
import pytest

from support.mscan import run


def test_only_records_later_than_every_earlier_one_are_kept(mscan_module):
    keep = mscan_module.MScan._increasing([1., 2., 2., 3., 1.5, 4., np.nan, 5., np.inf, 6.])
    assert keep.tolist() == [True, True, False, True, False, True, False, True, False, True]
    assert mscan_module.MScan._increasing([]).tolist() == []


def test_a_repeated_stamp_in_the_history_no_longer_blocks_start(mscan_continuous):
    r = mscan_continuous
    t = r.rig.detector_device.time.data
    t[:] = [t[-1] - .008, t[-1] - .006, t[-1] - .006, t[-1] - .009, t[-1] - .004, t[-1] - .002, t[-1]]
    r.rig.detector.values.data = [40.] * 7
    plan = r.scan._preflight()
    assert plan['metadata']['detector_cadence']['skipped_records'] == 2
    device, times, valid, skipped = r.scan._dmmr_clock(plan['detectors'])
    assert (np.diff(times) > 0).all() and skipped == 2 and valid.all()
    r.scan._acquisition_info = lambda: None
    v = run(r)
    assert v['status'] == 'completed', v['error']


def test_a_repeated_stamp_during_a_scan_is_skipped_with_its_value(mscan_rig):
    s, r = mscan_rig.scan, mscan_rig
    pause, ticks = s._pause, [0]
    def repeat_one_stamp():
        pause()
        ticks[0] += 1
        if ticks[0] == 3:  # One record stamped like the previous one, with a wrong value.
            r.detector_device.time.data.append(r.detector_device.time.data[-1])
            r.detector.values.data.append(1e6)
    s._pause = repeat_one_stamp
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed', s._validation['error']
    assert s.outputChannels[0].recordingData == pytest.approx([10., 20., 30.])  # 1e6 was skipped
    assert 1e6 not in s._module_values(0., 1e12)


def test_the_engine_receives_increasing_times_only(mscan_rig):
    s, r = mscan_rig.scan, mscan_rig
    t = r.detector_device.time.data
    t.extend([t[-1], t[-1] - .5, t[-1] + .01])  # Repeated, earlier, then later again.
    r.detector.values.data.extend([1., 2., 3.])
    s._plan = s._preflight()
    s._plan['detectors'] = [r.detector]
    adapter = r.module._load_native_runtime('_native_scan').NativeScanAdapter(s, lambda *a: None, emit_completion=False)
    adapter._bind()
    detector = adapter._snapshot_on_gui()['detector']
    assert (np.diff(detector['times']) > 0).all()
    assert len(detector['times']) == len(detector['values']) == len(t) - 2
