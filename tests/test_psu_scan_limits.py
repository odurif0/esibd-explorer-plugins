"""Numeric PSU limits/current on Explorer Channels; no extra driver access."""
from types import SimpleNamespace as NS

import numpy as np
import pytest

from test_psu_channels import make_psu

FIELDS = ('hardware_voltage_limit', 'hardware_current_limit',
          'voltage_setpoint_readback', 'current_limit_readback')


def snapshot():
    return dict(main_state={'name': 'ST_ON'}, device_enabled=True, output_enabled=(True, True),
        channels=[dict(channel=i, enabled=True,
            voltage={'measured_v': 100. + i, 'set_v': 100. + i, 'limit_v': 350. - i * 150},
            current={'measured_a': .002 + i * .001, 'set_a': .01 + i * .01, 'limit_a': .5 - i * .1})
            for i in (0, 1)])


@pytest.fixture(params=[f'psu_{s}' for s in 'abcde'])
def rig(request, monkeypatch):
    m, p, ctrl = make_psu(request.param)
    clock = NS(now=100.)
    monkeypatch.setattr(m, 'time', NS(monotonic=lambda: clock.now))
    ctrl._apply_snapshot(snapshot(), refreshed_at=clock.now)
    ctrl._read_live_readbacks = lambda **kw: pytest.fail('Consumer caused a hardware read')
    ctrl._update_state = lambda **kw: pytest.fail('Consumer caused a hardware read')
    return m, p, ctrl, clock


def test_limits_and_current_are_numeric_per_physical_channel(rig):
    _, p, ctrl, _ = rig
    p.channels.reverse()
    ctrl.updateValues()
    c1, c0 = p.channels
    assert (c0.hardware_voltage_limit, c1.hardware_voltage_limit) == (350., 200.)
    assert (c0.hardware_current_limit, c1.hardware_current_limit) == (.5, .4)
    assert (c0.voltage_setpoint_readback, c1.voltage_setpoint_readback) == (100., 101.)
    assert (c0.current_limit_readback, c1.current_limit_readback) == (.01, .02)
    assert (c0.current_readback, c1.current_readback) == (.002, .003)


@pytest.mark.parametrize('missing', ('voltage', 'current'))
def test_failed_housekeeping_field_is_unknown_not_an_old_limit(rig, missing):
    _, p, ctrl, clock = rig
    data = snapshot()
    del data['channels'][1][missing]
    ctrl._apply_snapshot(data, refreshed_at=clock.now)
    field = 'hardware_voltage_limit' if missing == 'voltage' else 'hardware_current_limit'
    assert np.isnan(getattr(p.channels[1], field))
    assert np.isfinite(getattr(p.channels[0], field))


def test_current_from_live_read_never_falls_back_to_stale_value(rig):
    _, p, ctrl, clock = rig
    ctrl._apply_live_readbacks(dict(device_enabled=True, output_enabled=(True, True),
        values={0: 101., 1: 102.}, current_values={0: .004}), refreshed_at=clock.now)
    assert p.channels[0].current_readback == .004
    assert np.isnan(p.channels[1].current_readback)
    assert np.isnan(ctrl.current_values.get(1, np.nan))
    assert p.channels[1].monitor == 102.
    assert p.channels[1].hardware_voltage_limit == 200.


@pytest.mark.parametrize('flag', ('initializing', 'transitioning', '_manual_apply_active',
                                  '_manual_apply_worker_running', '_hv_config_loading'))
def test_pending_operation_hides_limits_and_current(rig, flag):
    _, p, ctrl, _ = rig
    setattr(ctrl, flag, True)
    ctrl.updateValues()
    assert all(np.isnan(getattr(c, field)) for c in p.channels for field in (*FIELDS, 'current_readback'))


@pytest.mark.parametrize('case', ('off', 'reconnect', 'disabled', 'virtual', 'timeout'))
def test_unusable_or_stopped_source_hides_limits_and_current(rig, case):
    _, p, ctrl, clock = rig
    if case == 'off': ctrl._cancel_output_commands()
    elif case == 'reconnect': ctrl.device = NS(connected=True)
    elif case == 'disabled':
        for c in p.channels: c.enabled = False
    elif case == 'virtual':
        for c in p.channels: c.real = False
    elif case == 'timeout': clock.now += 10
    ctrl.updateValues()
    assert all(np.isnan(getattr(c, field)) for c in p.channels for field in (*FIELDS, 'current_readback'))


def test_housekeeping_limits_expire_independently_of_fresh_live_voltage(rig):
    _, p, ctrl, clock = rig
    clock.now += 4.01  # two 2-second housekeeping periods
    ctrl._apply_live_readbacks(dict(device_enabled=True, output_enabled=(True, True),
        values={0: 101., 1: 102.}, current_values={0: .004, 1: .005}), refreshed_at=clock.now)
    assert p.channels[0].monitor == 101.
    assert p.channels[0].current_readback == .004
    assert all(np.isnan(getattr(c, field)) for c in p.channels for field in FIELDS)
    ctrl._apply_snapshot(snapshot(), refreshed_at=clock.now)
    assert p.channels[0].hardware_voltage_limit == 350.


def test_early_qt_expiry_also_reschedules_limit_sample(rig, monkeypatch):
    import sys
    _, p, ctrl, clock = rig
    callbacks = []
    monkeypatch.setitem(sys.modules, 'PyQt6.QtCore', NS(
        QTimer=NS(singleShot=lambda ms, cb: callbacks.append((ms, cb)))))
    monkeypatch.setitem(sys.modules, 'PyQt6.QtWidgets', NS(
        QApplication=NS(instance=lambda: True)))
    ctrl.updateValues()
    limit_expiry = next(cb for delay, cb in callbacks if delay == 4001)
    callbacks.clear()
    clock.now = 103.91
    limit_expiry()
    # Old voltage has expired independently, but the limits timer must finish.
    assert any(80 < delay < 100 for delay, _ in callbacks)
    clock.now = 104.01
    for _, cb in callbacks[:]: cb()
    assert all(np.isnan(getattr(c, field)) for c in p.channels for field in FIELDS)
