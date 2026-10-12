"""Activation-state confirmation on simulated DLL calls, never hardware.

0x4100 is the captured initial state; delayed 0xc100 convergence is synthetic.
The recorded post-OFF 0x8100 must never be treated as successful activation.
"""
from types import SimpleNamespace
import threading

import pytest


@pytest.fixture
def clock(esi_heater_rig, monkeypatch):
    c = SimpleNamespace(now=0., sleeps=[])
    def sleep(delay):
        c.sleeps.append(delay)
        c.now += delay
    monkeypatch.setattr(esi_heater_rig.module, 'time', SimpleNamespace(monotonic=lambda: c.now, sleep=sleep))
    return c


def states(rig, monkeypatch, sequence):
    observed = []
    def complete(self):
        assert self.thread_lock.locked()
        value = sequence[min(len(observed), len(sequence) - 1)]
        observed.append(value)
        return 0, 0, 0, 0, 0, 0, 0, 0, [0] * 5, [value, 0, 0, 0, 0]
    monkeypatch.setattr(rig.base, 'get_complete_state', complete)
    return observed


@pytest.mark.parametrize('sequence', [[0xc100], [0x4100, 0x4100, 0xc100]])
def test_on_returns_only_after_complete_activation(esi_heater_rig, monkeypatch, clock, sequence):
    observed = states(esi_heater_rig, monkeypatch, sequence)
    assert esi_heater_rig.driver.set_output_active(0, True, timeout_s=.4) is True
    assert observed == sequence
    assert len(clock.sleeps) == len(sequence) - 1
    assert esi_heater_rig.calls.count(('module', 0, True)) == 1
    assert esi_heater_rig.calls.count(('enable', True)) == 1
    assert esi_heater_rig.driver.collect_diagnostics(timeout_s=.4)['heat']['active'] is True


@pytest.mark.parametrize('missing', [0x0100, 0x4000, 0x8000, 0xc100])
def test_missing_state_expires_without_acceptance_or_replay(esi_heater_rig, monkeypatch, clock, missing):
    value = 0xc100 & ~missing
    observed = states(esi_heater_rig, monkeypatch, [value])
    with pytest.raises(RuntimeError, match='activation.*not confirmed.*state=0x'):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.25)
    assert observed == [value] * 3
    assert clock.now == .25 and max(clock.sleeps) <= .1
    assert esi_heater_rig.calls.count(('module', 0, True)) == esi_heater_rig.calls.count(('enable', True)) == 1
    assert esi_heater_rig.driver._transport_poisoned is False
    assert not esi_heater_rig.driver.thread_lock.locked()
    assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.25) is False


@pytest.mark.parametrize('kind', ['native', 'device', 'main', 'unknown_main', 'malformed'])
def test_explicit_faults_are_not_retried_as_transitions(esi_heater_rig, monkeypatch, clock, kind):
    seen = []
    def complete(self):
        seen.append('state')
        if kind == 'malformed':
            return 0,
        return (-15 if kind == 'native' else 0), 0, (1 if kind == 'device' else 0), 0, 0, 0, 0, (999 if kind == 'unknown_main' else 1 if kind == 'main' else 0), [0]*5, [0x4100]*5
    monkeypatch.setattr(esi_heater_rig.base, 'get_complete_state', complete)
    with pytest.raises((RuntimeError, ValueError)):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.25)
    assert seen == ['state'] and clock.sleeps == []
    assert esi_heater_rig.driver._transport_poisoned is False


@pytest.mark.parametrize('getter', ['get_enable', 'get_module_activation_state'])
def test_lost_requested_gate_fails_without_wait_or_reactivation(esi_heater_rig, monkeypatch, clock, getter):
    states(esi_heater_rig, monkeypatch, [0xc100])
    original = getattr(esi_heater_rig.base, getter)
    seen = []
    def get(self, *args):
        seen.append(getter)
        if len(seen) > 1:
            return 0, False
        return original(self, *args)
    monkeypatch.setattr(esi_heater_rig.base, getter, get)
    with pytest.raises(RuntimeError, match='activation.*readback'):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.25)
    assert clock.sleeps == []
    assert esi_heater_rig.calls.count(('enable', True)) == esi_heater_rig.calls.count(('module', 0, True)) == 1


@pytest.mark.parametrize('where', ['get_complete_state', 'get_enable', 'get_module_activation_state'])
@pytest.mark.parametrize('failure', ['stop', 'poison', 'unknown', 'late'])
def test_abort_at_native_boundary_never_succeeds_or_continues(esi_heater_rig, monkeypatch, clock, where, failure):
    states(esi_heater_rig, monkeypatch, [0xc100])
    stop = threading.Event()
    original = getattr(esi_heater_rig.base, where)
    calls = []
    def native(self, *args):
        calls.append(where)
        result = original(self, *args)
        # Getter calls during the initial command acknowledgement are unchanged.
        if where == 'get_complete_state' or len(calls) == 2:
            if failure == 'stop':
                stop.set()
            elif failure in ('poison', 'unknown'):
                self._transport_poisoned = True if failure == 'poison' else None
            else:
                clock.now = 1.
        return result
    monkeypatch.setattr(esi_heater_rig.base, where, native)
    with pytest.raises((RuntimeError, InterruptedError)):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.25, cancel_event=stop)
    assert len(calls) == (1 if where == 'get_complete_state' else 2)
    assert clock.sleeps == []
    if failure in ('stop', 'late'):
        assert esi_heater_rig.driver._transport_poisoned is False
        assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.25, cancel_event=stop) is False


@pytest.mark.parametrize('when', ['before_dispatch', 'after_worker_return'])
def test_stop_at_confirmation_dispatch_boundary_cannot_report_success(esi_heater_rig, monkeypatch, when):
    seen = states(esi_heater_rig, monkeypatch, [0xc100])
    stop = threading.Event()
    original = esi_heater_rig.driver._call_locked_with_timeout
    def dispatch(method, budget, action, *args, **kwargs):
        if action == 'confirm_heat_activation' and when == 'before_dispatch':
            stop.set()
        result = original(method, budget, action, *args, **kwargs)
        if action == 'confirm_heat_activation' and when == 'after_worker_return':
            stop.set()
        return result
    monkeypatch.setattr(esi_heater_rig.driver, '_call_locked_with_timeout', dispatch)
    with pytest.raises(InterruptedError):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.3, cancel_event=stop)
    assert seen == ([] if when == 'before_dispatch' else [0xc100])
    assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.3, cancel_event=stop) is False


def test_stop_in_poll_wait_is_immediate_and_off_remains_possible(esi_heater_rig, monkeypatch):
    states(esi_heater_rig, monkeypatch, [0x4100])
    stop = threading.Event()
    waits = []
    def wait(delay):
        waits.append(delay)
        stop.set()
        return True
    monkeypatch.setattr(stop, 'wait', wait)
    with pytest.raises(InterruptedError):
        esi_heater_rig.driver.set_output_active(0, True, timeout_s=.3, cancel_event=stop)
    assert len(waits) == 1
    assert esi_heater_rig.driver.set_output_active(0, False, timeout_s=.3, cancel_event=stop) is False


def test_timed_out_native_confirmation_never_continues_on_late_return(esi_heater_rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = esi_heater_rig.base.get_complete_state
    def blocked(self):
        calls.append('state')
        entered.set()
        release.wait(3)
        return original(self)
    monkeypatch.setattr(esi_heater_rig.base, 'get_complete_state', blocked)
    original_enable = esi_heater_rig.base.get_enable
    monkeypatch.setattr(esi_heater_rig.base, 'get_enable', lambda self: calls.append('enable') or original_enable(self))
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            esi_heater_rig.driver.set_output_active(0, True, timeout_s=.02)
        assert entered.is_set() and esi_heater_rig.driver._transport_poisoned
        before = list(calls)
        with pytest.raises(RuntimeError):
            esi_heater_rig.driver.set_output_active(0, False, timeout_s=.02)
        assert calls == before
    finally:
        release.set()
        pending = getattr(esi_heater_rig.driver, '_pending_dll_call', None)
        if pending:
            pending['thread'].join(2)
            assert not pending['thread'].is_alive()
    assert calls == before
