"""Heater power control regressions; all instruments are simulated."""
import inspect
from threading import Event

import pytest


@pytest.mark.parametrize('when', ['dispatch', 'max_return', 'setter_return'])
def test_power_stop_inside_worker(esi_heater_rig, monkeypatch, when):
    cancel = Event()
    calls = []

    def maxima(self):
        calls.append('max')
        if when == 'max_return':
            cancel.set()
        return 0, 22., 12., 180., 175.

    def power(self, value):
        calls.append('power')
        if when == 'setter_return':
            cancel.set()
        return 0, value

    def dispatch(function, timeout, action, *args, **kwargs):
        if when == 'dispatch':
            cancel.set()
        return function()

    monkeypatch.setattr(esi_heater_rig.base, 'get_heat_ctrl_hw_limits', maxima)
    monkeypatch.setattr(esi_heater_rig.base, 'set_heat_ctrl_power_limit', power)
    monkeypatch.setattr(esi_heater_rig.driver, '_call_locked_with_timeout', dispatch)
    # Also reproduces the old method's actual write after Stop, not just a signature mismatch.
    kwargs = {'cancel_event': cancel} if 'cancel_event' in inspect.signature(esi_heater_rig.driver.configure_heat_limits).parameters else {}
    with pytest.raises(InterruptedError, match='cancel'):
        esi_heater_rig.driver.configure_heat_limits(power_w=50., **kwargs)
    assert calls == {'dispatch': [], 'max_return': ['max'], 'setter_return': ['max', 'power']}[when]


@pytest.mark.parametrize('maximum', [float('nan'), float('inf'), 0., -1.])
def test_power_rejects_unknown_hardware_maximum(esi_heater_rig, monkeypatch, maximum):
    esi_heater_rig.state.maxima['power'] = maximum
    with pytest.raises(ValueError):
        esi_heater_rig.driver.configure_heat_limits(power_w=50.)


@pytest.mark.parametrize('echo', [float('nan'), float('inf'), 0., -1., 181.])
def test_power_rejects_invalid_applied_echo(esi_heater_rig, monkeypatch, echo):
    monkeypatch.setattr(esi_heater_rig.base, 'set_heat_ctrl_power_limit', lambda self, value: (0, echo))
    with pytest.raises(ValueError):
        esi_heater_rig.driver.configure_heat_limits(power_w=50.)
