"""Hardware-bound amplitude scans, fixed Ilim and no implicit current commands."""
import json

import h5py
import numpy as np
import pytest

from test_mscan import module, rig  # noqa: F401 — shared fixtures


def test_allowed_amplitude_is_intersection_not_the_gui_spin_maximum(rig):
    s, a, b = rig.scan, *rig.psus
    s.amx_outputs = 'CH0-CH1'
    a.channels[0].min = 5.
    a.channels[1].hardware_voltage_limit = 180.
    b.channels[0].min = 15.
    b.channels[1].max = 120.
    assert s._amplitude_range(s._associated_rails(rig.amx, s.PAIRS[s.amx_outputs])) == (5., 180.)
    s.amx_outputs = 'CH2-CH3'
    assert s._amplitude_range(s._associated_rails(rig.amx, s.PAIRS[s.amx_outputs])) == (15., 120.)
    assert not a.writes and not b.writes


@pytest.mark.parametrize('attr', ('hardware_voltage_limit', 'hardware_current_limit',
                                 'voltage_setpoint_readback', 'current_limit_readback', 'current_readback', 'voltage_request_revision'))
def test_old_psu_plugin_requires_bundle_update_not_an_endless_readback_wait(rig, attr):
    delattr(rig.psus[0].channels[1], attr)
    with pytest.raises(rig.module.ScanError, match='update MScan and PSU plugins together'):
        rig.scan._prepare()
    assert all(not p.writes for p in rig.psus)


@pytest.mark.parametrize(('attr', 'value'), [
    ('hardware_voltage_limit', np.nan), ('hardware_voltage_limit', -1.), ('hardware_voltage_limit', 29.),
    ('hardware_current_limit', np.nan), ('hardware_current_limit', 0.), ('hardware_current_limit', .009),
    ('current_limit_readback', np.nan), ('current_limit_readback', 0.), ('current_limit_readback', .6),
    ('current_readback', np.nan), ('current_readback', -.001), ('current_readback', .01), ('current_readback', .0101),
    ('voltage_setpoint_readback', np.nan), ('voltage_setpoint_readback', 49.),
])
def test_unverified_or_invalid_limits_block_before_any_command(rig, attr, value):
    setattr(rig.psus[0].channels[1], attr, value)
    with pytest.raises(rig.module.ScanError):
        rig.scan._prepare()
    assert all(not p.writes for p in rig.psus)


@pytest.mark.parametrize(('start', 'stop', 'step'), [(10., 210., 10.), (210., 10., 10.), (10., 210., 120.)])
def test_requested_span_beyond_hardware_limit_is_rejected_even_when_end_not_sampled(rig, start, stop, step):
    s = rig.scan
    s.start, s.stop, s.step = start, stop, step
    for p in rig.psus:
        for c in p.channels: c.max = 10000.  # historic broad Explorer default is not the hardware limit
    with pytest.raises(rig.module.ScanError, match='allowed .*200'):
        s._prepare()
    assert all(not p.writes for p in rig.psus)


def test_restoration_is_prevalidated_even_outside_scan_interval(rig):
    s, c = rig.scan, rig.psus[0].channels[1]
    c._value = c.voltage_setpoint_readback = 220.
    with pytest.raises(rig.module.ScanError, match='restoration.*220.*200'):
        s._prepare()
    assert all(not p.writes for p in rig.psus)


def test_busy_psu_cannot_supply_initial_restoration_targets(rig):
    rig.psus[0].controller._manual_apply_worker_running = True
    with pytest.raises(rig.module.ScanError, match='transition'):
        rig.scan._prepare()
    assert not rig.psus[0].writes


def test_boundaries_including_zero_and_hardware_ceiling_are_allowed(rig):
    s = rig.scan
    s.start, s.stop, s.step = 0., 200., 100.
    plan = s._prepare()
    assert plan['requested_range'] == (0., 200.)
    assert [r['initial'] for r in plan['rails']] == [40., 50.]
    assert [r['ilim'] for r in plan['rails']] == [.01, .01]
    assert not rig.psus[0].writes


@pytest.mark.parametrize(('attr', 'value'), [
    ('hardware_voltage_limit', 20.), ('current_limit_readback', .008),
    ('hardware_current_limit', .009), ('current_readback', .01),
])
def test_limit_or_current_changes_stop_before_the_next_voltage(rig, attr, value):
    s, c = rig.scan, rig.psus[0].channels[1]
    def after_point(done):
        if not done: setattr(c, attr, value)
    s.signalComm.scanUpdateSignal.emit = after_point
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error', s._validation
    assert s.outputChannels[0].recordingData[0] == pytest.approx(10.)
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]  # no later step or restoration
    assert rig.psus[0].isOn()  # scan abort is not a PSU OFF


def test_pending_psu_edit_blocks_the_next_voltage_even_before_new_limits_are_published(rig):
    s, psu = rig.scan, rig.psus[0]
    def after_point(done):
        if not done: psu.controller._manual_apply_worker_running = True
    s.signalComm.scanUpdateSignal.emit = after_point
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert 'transition' in s._validation['error']
    assert psu.writes == [(0, 10.), (1, 10.)]
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()


@pytest.mark.parametrize('phase', ('settling', 'acquiring'))
def test_measured_current_at_ilim_invalidates_point_and_does_not_raise_ilim(rig, phase):
    s, c = rig.scan, rig.psus[0].channels[1]
    def trip():
        if phase in s.scan_status: c.current_readback = c.current_limit_readback
    rig.on_pause = trip
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert 'reached/exceeded Ilim' in s._validation['error']
    assert np.isnan(s.outputChannels[0].recordingData).all()
    assert np.isnan(s._validation['rail_i']).all()
    assert c.current_limit_readback == .01
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]


def test_voltage_out_of_tolerance_below_ilim_is_still_rejected(rig):
    s = rig.scan
    observe = s._observation
    def dropped_voltage():
        ready, volts, currents, now = observe()
        if rig.psus[0].writes:
            return False, volts - 5., currents, now
        return ready, volts, currents, now
    s._observation = dropped_voltage
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error'
    assert 'settling' in s._validation['error'].lower()
    assert np.isnan(s.outputChannels[0].recordingData).all()
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]


def test_psu_inrush_flag_is_not_hv_current_limit_evidence(rig):
    rig.psus[0].controller.current_limit_active = True
    plan = rig.scan._prepare()
    assert plan['rails'][0]['ilim'] == .01  # valid per-channel readbacks still authoritative


def test_hdf5_records_currents_and_limit_context_and_reads_legacy_file(rig, tmp_path):
    s = rig.scan
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed'
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path, 'r+') as f:
        group = f['MScan/Validation']
        assert np.all(group['rail_i'][:] == .002)
        assert 'not detector current' in group['rail_i'].attrs['Unit']
        setup = json.loads(group.attrs['setup'])
        assert setup['rails'][0]['voltage_limit_v'] == 200.
        assert setup['rails'][1]['final_vset'] == 50.
        assert setup['rails'][0]['ilim_a'] == .01
        assert setup['rails'][0]['current_limit_a'] == .5
        del group['rail_i']  # legacy file before PSU current observations
    s.file = path
    assert s.loadDataInternal()
    assert np.isnan(s._validation['rail_i']).all()
    assert s._validation['rail_v'][:, 0] == pytest.approx([10, 20, 30])
