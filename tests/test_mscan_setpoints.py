"""A new voltage request is not the same event as a quantized PSU echo."""
import numpy as np
import pytest

from test_mscan import module, rig  # noqa: F401
from test_mscan_continuous import continuous  # noqa: F401


def confirm(rig, value):
    psu = rig.psus[0]
    psu.controller._manual_apply_worker_running = False
    for c in psu.channels:
        # Housekeeping publishes without going through a command setter.
        c._value = c.voltage_setpoint_readback = c.monitor = value


def test_quantized_confirmation_does_not_change_the_requested_amplitude(rig):
    s = rig.scan
    s._command([10., 10.])
    revisions = [c.voltage_request_revision for c in rig.psus[0].channels]
    confirm(rig, 9.999)
    assert s._observation()[0]
    assert s._observation()[0]  # repeated housekeeping is still only an observation
    assert [c.voltage_request_revision for c in rig.psus[0].channels] == revisions
    assert s._plan['expected'] == [10., 10.]
    assert [r['confirmed_vset'] for r in s._plan['rails']] == [9.999, 9.999]
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]


def test_later_hardware_setpoint_change_is_not_hidden_by_voltage_tolerance(rig):
    s = rig.scan
    s._command([10., 10.])
    confirm(rig, 9.999)
    assert s._observation()[0]
    confirm(rig, 9.998)  # far inside the 1 V regulation tolerance
    with pytest.raises(rig.module.ScanError, match='PSU setpoint changed outside the scan'):
        s._command([20., 20.])
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]


def test_noop_return_keeps_the_last_confirmed_setpoint(rig, monkeypatch):
    channel_type = type(rig.psus[0].channels[0])
    value = channel_type.value
    def only_changes(c, target):
        if value.fget(c) != target:  # real Qt does not emit valueChanged on a no-op
            value.fset(c, target)
    monkeypatch.setattr(channel_type, 'value', property(value.fget, only_changes))
    rig.scan._command([40., 50.])
    assert rig.psus[0].writes == []
    assert [r['confirmed_vset'] for r in rig.scan._plan['rails']] == [40., 50.]
    rig.psus[0].channels[0].voltage_setpoint_readback = 40.001
    with pytest.raises(rig.module.ScanError, match='PSU setpoint changed outside the scan'):
        rig.scan._observation()


def test_missing_setpoint_readback_is_not_a_valid_confirmation(rig):
    s = rig.scan
    s._command([10., 10.])
    confirm(rig, 10.)
    rig.psus[0].channels[0].voltage_setpoint_readback = np.nan
    assert not s._observation()[0]
    assert s._plan['rails'][0]['confirmed_vset'] is None


@pytest.mark.parametrize('mode', ['stepped', 'continuous'])
@pytest.mark.parametrize('kind', ['request', 'roundtrip', 'hardware'])
def test_external_changes_abort_in_either_mode(rig, continuous, mode, kind):
    s, c = rig.scan, rig.psus[0].channels[0]
    if mode == 'stepped':
        s.scan_mode = s.STEPPED
        s._plan = s._preflight()
    command = s._command
    def changed_before_next_command(targets, **kwargs):
        if targets == [20., 20.]:
            if kind == 'hardware':
                c.voltage_setpoint_readback += .001
            else:
                c.voltage_request_revision += 2 if kind == 'roundtrip' else 1
                if kind == 'request':
                    c._value += .001
        return command(targets, **kwargs)
    s._command = changed_before_next_command
    s.runScan(lambda: True)
    assert s._validation['status'] == 'error', s._validation
    assert 'setpoint changed outside the scan' in s._validation['error']
    if mode == 'stepped':
        assert np.isfinite(s.outputChannels[0].recordingData[0])
    assert np.isnan(s.outputChannels[0].recordingData[1:]).all()
    assert rig.psus[0].writes == [(0, 10.), (1, 10.)]
    assert rig.psus[0].isOn()  # no later point, restoration or automatic OFF
