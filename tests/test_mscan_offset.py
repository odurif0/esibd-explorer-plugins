"""Quadrupole offset on an AMPR channel, driven by MScan as coefficient × amplitude.

User request (2026-10-08): select the AMPR module/channel of the quadrupole offset, a
proportionality coefficient (default 0.2), and make sure the applied offset equals A × coefficient.
"""
import json
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest

from support.mscan import Request, add_offset, run


def prepare(r, n=3):
    s = r.scan
    s._plan = s._prepare()
    s._plan['detectors'] = [r.detector]
    s._validation.update(offset_v=np.full(n, np.nan), offset_target=np.full(n, np.nan))


def test_offset_is_coefficient_times_amplitude_at_every_point_then_restored(mscan_rig):
    channel = add_offset(mscan_rig)
    mscan_rig.on_pause = mscan_rig.offset.settle
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    v = mscan_rig.scan._validation
    assert v['status'] == 'completed', v
    assert channel.writes == [2., 4., 6., 5.]  # 0.2 × 10, 20, 30 V, then the initial offset
    assert v['offset_target'] == pytest.approx([2., 4., 6.])
    assert v['offset_v'] == pytest.approx([2., 4., 6.])
    # The rails are commanded first, then the offset, at each point.
    assert mscan_rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20), (0, 30), (1, 30), (0, 40), (1, 50)]
    metadata = mscan_rig.scan._plan['metadata']['offset']
    assert (metadata['channel'], metadata['ampr'], metadata['module'], metadata['ampr_channel']) == ('Q_offset', 'AMPR_A', 2, 3)
    assert (metadata['coefficient'], metadata['initial_v'], metadata['final_v']) == (.2, 5., 5.)


def test_default_coefficient_is_0_2(mscan_module, monkeypatch):
    monkeypatch.setattr(mscan_module, 'PARAMETERTYPE', NS(COMBO='COMBO', FLOAT='FLOAT', INT='INT', LABEL='LABEL', TEXT='TEXT'))
    settings = mscan_module.MScan.getDefaultSettings(NS(**{k: getattr(mscan_module.MScan, k) for k in dir(mscan_module.MScan) if k.isupper()},
                                                   _offset_changed=None, _signal_changed=None, _setup_changed=None,
                                                   _mode_changed=None, estimateScanTime=None, _dmmr_interval_changed=None))
    assert settings[mscan_module.MScan.OFFSET_FACTOR]['value'] == .2
    assert settings[mscan_module.MScan.OFFSET]['value'] == 'None'
    keys = list(settings)
    assert keys.index(mscan_module.MScan.RATE) < keys.index(mscan_module.MScan.OFFSET) < keys.index(mscan_module.MScan.OFFSET_FACTOR) \
        < keys.index(mscan_module.MScan.OFFSET_READBACK) < keys.index(mscan_module.MScan.FINAL_VOLTAGES)


def test_not_driven_by_default(mscan_rig):
    plan = mscan_rig.scan._prepare()
    assert plan['offset'] is None and plan['metadata']['offset'] is None


def test_rounded_command_is_what_must_be_confirmed(mscan_rig):
    channel = add_offset(mscan_rig, factor=1 / 3)
    mscan_rig.on_pause = mscan_rig.offset.settle
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    v = mscan_rig.scan._validation
    assert v['status'] == 'completed', v
    assert channel.writes == [3.33, 6.67, 10., 5.]
    assert v['offset_target'] == pytest.approx([3.33, 6.67, 10.])


@pytest.mark.parametrize('problem', ['unconfirmed', 'monitor'])
def test_no_point_is_acquired_until_the_ampr_confirms_the_offset(mscan_rig, problem):
    channel = add_offset(mscan_rig, confirm=problem != 'unconfirmed', monitor_error=2. if problem == 'monitor' else 0.)
    mscan_rig.on_pause = mscan_rig.offset.settle
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    v = mscan_rig.scan._validation
    assert v['status'] == 'error' and 'Quadrupole offset Q_offset' in v['error'], v
    assert np.isnan(mscan_rig.scan.outputChannels[0].recordingData).all()
    assert channel.writes == [2.]  # No restoration after an error; the offset is held.


@pytest.mark.parametrize('change', ['external-edit', 'setpoint-error', 'ampr-off', 'equation'])
def test_offset_changed_or_failed_outside_the_scan_aborts(mscan_rig, change):
    channel = add_offset(mscan_rig)

    def pause_hook():
        mscan_rig.offset.settle()
        if mscan_rig.current() != 20:
            return
        if change == 'external-edit':
            channel._value = 9.
        if change == 'setpoint-error':
            for request in mscan_rig.offset.controller._latest_setpoints.values():
                request.state, request.detail = 'error', 'AMPR rejected the command'
        if change == 'ampr-off':
            mscan_rig.offset.controller._setpoint_cancel.set()
        if change == 'equation':
            channel.active, channel.equation = False, '2 * 3'
    mscan_rig.on_pause = pause_hook
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    v = mscan_rig.scan._validation
    assert v['status'] == 'error', v
    assert np.isnan(mscan_rig.scan.outputChannels[0].recordingData[1:]).all()
    assert channel.writes == [2., 4.]


@pytest.mark.parametrize(('change', 'message'), [
    ('range', 'exceeds allowed'), ('rating', 'exceeds allowed'), ('restoration', 'restoration'),
    ('off', 'switch the AMPR offset channel ON'), ('equation', 'driven by its equation'), ('ampr', 'turn the AMPR ON'),
    ('unconfirmed', 'not confirmed'), ('monitor', 'Monitor'), ('missing', 'unavailable'), ('old-ampr', 'update MScan and AMPR'),
])


def test_preflight_blocks_before_any_command(mscan_rig, change, message):
    channel = add_offset(mscan_rig)
    ctrl = mscan_rig.offset.controller
    if change == 'range':
        mscan_rig.scan.offset_factor = 4.  # 4 × 30 V = 120 V > channel max 100 V
    if change == 'rating':
        channel.max, ctrl._module_voltage_limit = 1000., lambda module: 5.
    if change == 'restoration':
        channel._value = -150.
    if change == 'off':
        channel.enabled = False
    if change == 'equation':
        channel.active, channel.equation = False, '2 * 3'
    if change == 'ampr':
        mscan_rig.offset.device.isOn = lambda: False
    if change == 'unconfirmed':
        ctrl._latest_setpoints[(2, 3)] = Request(channel, 5.)
    if change == 'monitor':
        channel.monitor = np.nan
    if change == 'missing':
        mscan_rig.scan.offset_channel = 'AMPR_A M9 CH9 — gone'
    if change == 'old-ampr':
        del ctrl._latest_setpoints
    with pytest.raises(mscan_rig.module.ScanError, match=message):
        mscan_rig.scan._prepare()
    assert channel.writes == [] and all(not p.writes for p in mscan_rig.psus)


def test_negative_coefficient_is_allowed_within_the_channel_range(mscan_rig):
    channel = add_offset(mscan_rig, factor=-.5)
    mscan_rig.on_pause = mscan_rig.offset.settle
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation['status'] == 'completed'
    assert channel.writes == [-5., -10., -15., 5.]


def test_continuous_mode_drives_and_confirms_the_offset(mscan_continuous):
    r = mscan_continuous
    channel = add_offset(r.rig)
    r.tick = r.rig.offset.settle
    r.scan._plan = r.scan._preflight()
    r.scan._validation.update(offset_v=np.full(3, np.nan), offset_target=np.full(3, np.nan))
    v = run(r)
    assert v['status'] == 'completed', v
    assert channel.writes[-1] == 5. and channel.writes[:3] == [2., 4., 6.]
    assert v['offset_target'] == pytest.approx([2., 4., 6.])
    raw = v['continuous']
    assert len(raw['offset_v']) == len(raw['psu_time']) == len(raw['offset_target'])


def test_continuous_mode_aborts_when_the_offset_lags(mscan_continuous):
    r = mscan_continuous
    channel = add_offset(r.rig, confirm=False)
    r.scan._plan = r.scan._preflight()
    r.scan._validation.update(offset_v=np.full(3, np.nan), offset_target=np.full(3, np.nan))
    v = run(r)
    assert v['status'] == 'error' and 'Quadrupole offset' in v['error'], v
    assert channel.writes == [2.]


def test_hdf5_keeps_offset_readback_target_and_metadata(mscan_rig, tmp_path):
    add_offset(mscan_rig)
    mscan_rig.on_pause = mscan_rig.offset.settle
    prepare(mscan_rig)
    mscan_rig.scan.runScan(lambda: True)
    path = tmp_path / 'scan.h5'
    mscan_rig.scan.saveData(path)
    with h5py.File(path) as f:
        group = f['MScan/Validation']
        assert group['offset_v'][:] == pytest.approx([2., 4., 6.])
        assert group['offset_target'][:] == pytest.approx([2., 4., 6.])
        assert 'AMPR Monitor' in group['offset_v'].attrs['Unit']
        assert json.loads(group.attrs['setup'])['offset']['coefficient'] == .2
    mscan_rig.scan.file = path
    assert mscan_rig.scan.loadDataInternal()
    assert mscan_rig.scan._validation['offset_target'] == pytest.approx([2., 4., 6.])


def test_readback_and_completion_texts(mscan_rig):
    add_offset(mscan_rig)
    s = mscan_rig.scan
    assert s._offset_readback_text() == 'Q_offset: Monitor +5.000 V, set +5 V\nScan: U = 0.2 × A = +2 … +6 V'
    prepare(mscan_rig)
    assert s._offset_readback_text(running=True).startswith('Q_offset: target +5 V')
    assert s._completion_text(s._plan['rails'], running=True).splitlines()[-1] == 'Q_offset: +5 V (quadrupole offset)'
    s.offset_channel = s.NO_OFFSET
    assert s._offset_readback_text() == 'Not driven'
