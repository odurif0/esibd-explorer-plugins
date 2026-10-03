"""Activation-state confirmation on simulated DLL calls, never hardware.

0x4100 is the captured initial state; delayed 0xc100 convergence is synthetic.
The recorded post-OFF 0x8100 must never be treated as successful activation.
"""
from types import SimpleNamespace
import threading

import pytest

from test_esi_heater_activation import rig


@pytest.fixture
def clock(rig, monkeypatch):
    c = SimpleNamespace(now=0., sleeps=[])
    def sleep(delay):
        c.sleeps.append(delay)
        c.now += delay
    monkeypatch.setattr(rig.module, 'time', SimpleNamespace(monotonic=lambda: c.now, sleep=sleep))
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
def test_on_returns_only_after_complete_activation(rig, monkeypatch, clock, sequence):
    observed = states(rig, monkeypatch, sequence)
    assert rig.driver.set_output_active(0, True, timeout_s=.4) is True
    assert observed == sequence
    assert len(clock.sleeps) == len(sequence) - 1
    assert rig.calls.count(('module', 0, True)) == 1
    assert rig.calls.count(('enable', True)) == 1
    assert rig.driver.collect_diagnostics(timeout_s=.4)['heat']['active'] is True


@pytest.mark.parametrize('missing', [0x0100, 0x4000, 0x8000, 0xc100])
def test_missing_state_expires_without_acceptance_or_replay(rig, monkeypatch, clock, missing):
    value = 0xc100 & ~missing
    observed = states(rig, monkeypatch, [value])
    with pytest.raises(RuntimeError, match='activation.*not confirmed.*state=0x'):
        rig.driver.set_output_active(0, True, timeout_s=.25)
    assert observed == [value] * 3
    assert clock.now == .25 and max(clock.sleeps) <= .1
    assert rig.calls.count(('module', 0, True)) == rig.calls.count(('enable', True)) == 1
    assert rig.driver._transport_poisoned is False
    assert not rig.driver.thread_lock.locked()
    assert rig.driver.set_output_active(0, False, timeout_s=.25) is False


@pytest.mark.parametrize('kind', ['native', 'device', 'main', 'unknown_main', 'malformed'])
def test_explicit_faults_are_not_retried_as_transitions(rig, monkeypatch, clock, kind):
    seen = []
    def complete(self):
        seen.append('state')
        if kind == 'malformed':
            return 0,
        return (-15 if kind == 'native' else 0), 0, (1 if kind == 'device' else 0), 0, 0, 0, 0, (999 if kind == 'unknown_main' else 1 if kind == 'main' else 0), [0]*5, [0x4100]*5
    monkeypatch.setattr(rig.base, 'get_complete_state', complete)
    with pytest.raises((RuntimeError, ValueError)):
        rig.driver.set_output_active(0, True, timeout_s=.25)
    assert seen == ['state'] and clock.sleeps == []
    assert rig.driver._transport_poisoned is False


@pytest.mark.parametrize('getter', ['get_enable', 'get_module_activation_state'])
def test_lost_requested_gate_fails_without_wait_or_reactivation(rig, monkeypatch, clock, getter):
    states(rig, monkeypatch, [0xc100])
    original = getattr(rig.base, getter)
    seen = []
    def get(self, *args):
        seen.append(getter)
        if len(seen) > 1:
            return 0, False
        return original(self, *args)
    monkeypatch.setattr(rig.base, getter, get)
    with pytest.raises(RuntimeError, match='activation.*readback'):
        rig.driver.set_output_active(0, True, timeout_s=.25)
    assert clock.sleeps == []
    assert rig.calls.count(('enable', True)) == rig.calls.count(('module', 0, True)) == 1


@pytest.mark.parametrize('where', ['get_complete_state', 'get_enable', 'get_module_activation_state'])
@pytest.mark.parametrize('failure', ['stop', 'poison', 'unknown', 'late'])
def test_abort_at_native_boundary_never_succeeds_or_continues(rig, monkeypatch, clock, where, failure):
    states(rig, monkeypatch, [0xc100])
    stop = threading.Event()
    original = getattr(rig.base, where)
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
    monkeypatch.setattr(rig.base, where, native)
    with pytest.raises((RuntimeError, InterruptedError)):
        rig.driver.set_output_active(0, True, timeout_s=.25, cancel_event=stop)
    assert len(calls) == (1 if where == 'get_complete_state' else 2)
    assert clock.sleeps == []
    if failure in ('stop', 'late'):
        assert rig.driver._transport_poisoned is False
        assert rig.driver.set_output_active(0, False, timeout_s=.25, cancel_event=stop) is False


@pytest.mark.parametrize('when', ['before_dispatch', 'after_worker_return'])
def test_stop_at_confirmation_dispatch_boundary_cannot_report_success(rig, monkeypatch, when):
    seen = states(rig, monkeypatch, [0xc100])
    stop = threading.Event()
    original = rig.driver._call_locked_with_timeout
    def dispatch(method, budget, action, *args, **kwargs):
        if action == 'confirm_heat_activation' and when == 'before_dispatch':
            stop.set()
        result = original(method, budget, action, *args, **kwargs)
        if action == 'confirm_heat_activation' and when == 'after_worker_return':
            stop.set()
        return result
    monkeypatch.setattr(rig.driver, '_call_locked_with_timeout', dispatch)
    with pytest.raises(InterruptedError):
        rig.driver.set_output_active(0, True, timeout_s=.3, cancel_event=stop)
    assert seen == ([] if when == 'before_dispatch' else [0xc100])
    assert rig.driver.set_output_active(0, False, timeout_s=.3, cancel_event=stop) is False


def test_stop_in_poll_wait_is_immediate_and_off_remains_possible(rig, monkeypatch):
    states(rig, monkeypatch, [0x4100])
    stop = threading.Event()
    waits = []
    def wait(delay):
        waits.append(delay)
        stop.set()
        return True
    monkeypatch.setattr(stop, 'wait', wait)
    with pytest.raises(InterruptedError):
        rig.driver.set_output_active(0, True, timeout_s=.3, cancel_event=stop)
    assert len(waits) == 1
    assert rig.driver.set_output_active(0, False, timeout_s=.3, cancel_event=stop) is False


def test_timed_out_native_confirmation_never_continues_on_late_return(rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    original = rig.base.get_complete_state
    def blocked(self):
        calls.append('state')
        entered.set()
        release.wait(3)
        return original(self)
    monkeypatch.setattr(rig.base, 'get_complete_state', blocked)
    original_enable = rig.base.get_enable
    monkeypatch.setattr(rig.base, 'get_enable', lambda self: calls.append('enable') or original_enable(self))
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            rig.driver.set_output_active(0, True, timeout_s=.02)
        assert entered.is_set() and rig.driver._transport_poisoned
        before = list(calls)
        with pytest.raises(RuntimeError):
            rig.driver.set_output_active(0, False, timeout_s=.02)
        assert calls == before
    finally:
        release.set()
        pending = getattr(rig.driver, '_pending_dll_call', None)
        if pending:
            pending['thread'].join(2)
            assert not pending['thread'].is_alive()
    assert calls == before
