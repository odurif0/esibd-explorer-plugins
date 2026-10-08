"""Quadrupole offset on an AMPR channel, driven by MScan as coefficient × amplitude.

User request (2026-10-08): select the AMPR module/channel of the quadrupole offset, a
proportionality coefficient (default 0.2), and make sure the applied offset equals A × coefficient.
"""
import json
from threading import Event, Lock
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest

from test_mscan import module, rig  # noqa: F401 — shared fixtures
from test_mscan_continuous import continuous, run  # noqa: F401


class Request:
    def __init__(self, channel, target):
        self.channel, self.target, self.state, self.detail = channel, target, 'pending', ''


class OffsetChannel:
    """AMPR channel: the value cell rounds to 2 decimals and queues a request, like the AMPR plugin."""

    def __init__(self, device, module=2, number=3):
        self.device, self.module, self.number = device, module, number
        self.name, self.unit = 'Q_offset', 'V'
        self.real = self.enabled = self.active = True
        self.min, self.max = -100., 100.
        self._value = self.monitor = 5.
        self.writes = []

    def getDevice(self):
        return self.device

    def module_address(self):
        return self.module

    def channel_number(self):
        return self.number

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        value = round(float(value), 2)
        self.writes.append(value)
        if value != self._value:
            self._value = value
            ctrl = self.device.controller
            ctrl._latest_setpoints[(self.module, self.number)] = Request(self, value)


def add_offset(r, factor=.2, monitor_error=0., confirm=True):
    """Add an AMPR offset channel to the shared rig; each pause confirms its latest request."""
    s = r.scan
    ctrl = NS(initialized=True, device=object(), _setpoint_lock=Lock(), _setpoint_cancel=Event(),
              _latest_setpoints={}, transitioning=False, ramping=False, initializing=False,
              _module_voltage_limit=lambda module: 500.)
    device = NS(name='AMPR_A', controller=ctrl, isOn=lambda: True)
    channel = OffsetChannel(device)
    r.all_channels.append(channel)
    s.pluginManager.plugins.append(device)
    s.offset_channel, s.offset_factor = s._offset_label(channel), factor

    def settle_ampr():
        for request in ctrl._latest_setpoints.values():
            if confirm:
                request.state = 'confirmed'
                channel.monitor = request.target + monitor_error
    r.offset = NS(channel=channel, controller=ctrl, device=device, settle=settle_ampr)
    return channel


def prepare(r, n=3):
    s = r.scan
    s._plan = s._prepare()
    s._plan['detectors'] = [r.detector]
    s._validation.update(offset_v=np.full(n, np.nan), offset_target=np.full(n, np.nan))


def test_offset_is_coefficient_times_amplitude_at_every_point_then_restored(rig):
    channel = add_offset(rig)
    rig.on_pause = rig.offset.settle
    prepare(rig)
    rig.scan.runScan(lambda: True)
    v = rig.scan._validation
    assert v['status'] == 'completed', v
    assert channel.writes == [2., 4., 6., 5.]  # 0.2 × 10, 20, 30 V, then the initial offset
    assert v['offset_target'] == pytest.approx([2., 4., 6.])
    assert v['offset_v'] == pytest.approx([2., 4., 6.])
    # The rails are commanded first, then the offset, at each point.
    assert rig.psus[0].writes == [(0, 10), (1, 10), (0, 20), (1, 20), (0, 30), (1, 30), (0, 40), (1, 50)]
    metadata = rig.scan._plan['metadata']['offset']
    assert (metadata['channel'], metadata['ampr'], metadata['module'], metadata['ampr_channel']) == ('Q_offset', 'AMPR_A', 2, 3)
    assert (metadata['coefficient'], metadata['initial_v'], metadata['final_v']) == (.2, 5., 5.)


def test_default_coefficient_is_0_2(module, monkeypatch):
    monkeypatch.setattr(module, 'PARAMETERTYPE', NS(COMBO='COMBO', FLOAT='FLOAT', INT='INT', LABEL='LABEL'))
    settings = module.MScan.getDefaultSettings(NS(**{k: getattr(module.MScan, k) for k in dir(module.MScan) if k.isupper()},
                                                   _offset_changed=None, _signal_changed=None, _setup_changed=None,
                                                   _mode_changed=None, estimateScanTime=None, _dmmr_interval_changed=None))
    assert settings[module.MScan.OFFSET_FACTOR]['value'] == .2
    assert settings[module.MScan.OFFSET]['value'] == 'None'
    keys = list(settings)
    assert keys.index(module.MScan.RATE) < keys.index(module.MScan.OFFSET) < keys.index(module.MScan.OFFSET_FACTOR) \
        < keys.index(module.MScan.OFFSET_READBACK) < keys.index(module.MScan.FINAL_VOLTAGES)


def test_not_driven_by_default(rig):
    plan = rig.scan._prepare()
    assert plan['offset'] is None and plan['metadata']['offset'] is None


def test_rounded_command_is_what_must_be_confirmed(rig):
    channel = add_offset(rig, factor=1 / 3)
    rig.on_pause = rig.offset.settle
    prepare(rig)
    rig.scan.runScan(lambda: True)
    v = rig.scan._validation
    assert v['status'] == 'completed', v
    assert channel.writes == [3.33, 6.67, 10., 5.]
    assert v['offset_target'] == pytest.approx([3.33, 6.67, 10.])


@pytest.mark.parametrize('problem', ['unconfirmed', 'monitor'])
def test_no_point_is_acquired_until_the_ampr_confirms_the_offset(rig, problem):
    channel = add_offset(rig, confirm=problem != 'unconfirmed', monitor_error=2. if problem == 'monitor' else 0.)
    rig.on_pause = rig.offset.settle
    prepare(rig)
    rig.scan.runScan(lambda: True)
    v = rig.scan._validation
    assert v['status'] == 'error' and 'Quadrupole offset Q_offset' in v['error'], v
    assert np.isnan(rig.scan.outputChannels[0].recordingData).all()
    assert channel.writes == [2.]  # No restoration after an error; the offset is held.


@pytest.mark.parametrize('change', ['external-edit', 'setpoint-error', 'ampr-off', 'equation'])
def test_offset_changed_or_failed_outside_the_scan_aborts(rig, change):
    channel = add_offset(rig)

    def pause_hook():
        rig.offset.settle()
        if rig.current() != 20:
            return
        if change == 'external-edit':
            channel._value = 9.
        if change == 'setpoint-error':
            for request in rig.offset.controller._latest_setpoints.values():
                request.state, request.detail = 'error', 'AMPR rejected the command'
        if change == 'ampr-off':
            rig.offset.controller._setpoint_cancel.set()
        if change == 'equation':
            channel.active = False
    rig.on_pause = pause_hook
    prepare(rig)
    rig.scan.runScan(lambda: True)
    v = rig.scan._validation
    assert v['status'] == 'error', v
    assert np.isnan(rig.scan.outputChannels[0].recordingData[1:]).all()
    assert channel.writes == [2., 4.]


@pytest.mark.parametrize(('change', 'message'), [
    ('range', 'exceeds allowed'), ('rating', 'exceeds allowed'), ('restoration', 'restoration'),
    ('off', 'switch the AMPR offset channel ON'), ('equation', 'manual'), ('ampr', 'turn the AMPR ON'),
    ('unconfirmed', 'not confirmed'), ('monitor', 'Monitor'), ('missing', 'unavailable'), ('old-ampr', 'update MScan and AMPR'),
])
def test_preflight_blocks_before_any_command(rig, change, message):
    channel = add_offset(rig)
    ctrl = rig.offset.controller
    if change == 'range':
        rig.scan.offset_factor = 4.  # 4 × 30 V = 120 V > channel max 100 V
    if change == 'rating':
        channel.max, ctrl._module_voltage_limit = 1000., lambda module: 5.
    if change == 'restoration':
        channel._value = -150.
    if change == 'off':
        channel.enabled = False
    if change == 'equation':
        channel.active = False
    if change == 'ampr':
        rig.offset.device.isOn = lambda: False
    if change == 'unconfirmed':
        ctrl._latest_setpoints[(2, 3)] = Request(channel, 5.)
    if change == 'monitor':
        channel.monitor = np.nan
    if change == 'missing':
        rig.scan.offset_channel = 'AMPR_A M9 CH9 — gone'
    if change == 'old-ampr':
        del ctrl._latest_setpoints
    with pytest.raises(rig.module.ScanError, match=message):
        rig.scan._prepare()
    assert channel.writes == [] and all(not p.writes for p in rig.psus)


def test_negative_coefficient_is_allowed_within_the_channel_range(rig):
    channel = add_offset(rig, factor=-.5)
    rig.on_pause = rig.offset.settle
    prepare(rig)
    rig.scan.runScan(lambda: True)
    assert rig.scan._validation['status'] == 'completed'
    assert channel.writes == [-5., -10., -15., 5.]


def test_continuous_mode_drives_and_confirms_the_offset(continuous):
    r = continuous
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


def test_continuous_mode_aborts_when_the_offset_lags(continuous):
    r = continuous
    channel = add_offset(r.rig, confirm=False)
    r.scan._plan = r.scan._preflight()
    r.scan._validation.update(offset_v=np.full(3, np.nan), offset_target=np.full(3, np.nan))
    v = run(r)
    assert v['status'] == 'error' and 'Quadrupole offset' in v['error'], v
    assert channel.writes == [2.]


def test_hdf5_keeps_offset_readback_target_and_metadata(rig, tmp_path):
    add_offset(rig)
    rig.on_pause = rig.offset.settle
    prepare(rig)
    rig.scan.runScan(lambda: True)
    path = tmp_path / 'scan.h5'
    rig.scan.saveData(path)
    with h5py.File(path) as f:
        group = f['MScan/Validation']
        assert group['offset_v'][:] == pytest.approx([2., 4., 6.])
        assert group['offset_target'][:] == pytest.approx([2., 4., 6.])
        assert 'AMPR Monitor' in group['offset_v'].attrs['Unit']
        assert json.loads(group.attrs['setup'])['offset']['coefficient'] == .2
    rig.scan.file = path
    assert rig.scan.loadDataInternal()
    assert rig.scan._validation['offset_target'] == pytest.approx([2., 4., 6.])


def test_readback_and_completion_texts(rig):
    add_offset(rig)
    s = rig.scan
    assert s._offset_readback_text() == 'Q_offset: Monitor +5.000 V, set +5 V\nScan: 0.2 × A = +2 … +6 V'
    prepare(rig)
    assert s._offset_readback_text(running=True).startswith('Q_offset: target +5 V')
    assert s._completion_text(s._plan['rails'], running=True).splitlines()[-1] == 'Q_offset: +5 V (quadrupole offset)'
    s.offset_channel = s.NO_OFFSET
    assert s._offset_readback_text() == 'Not driven'
