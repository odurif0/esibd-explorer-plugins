"""MON_RDY acquisition against a consumable monitor simulator; never hardware.

The real 2026-09-28 trace returned NO_ERR/Valid=False with MON_RDY clear,
80 ms after a successful read. Poll cadence below is not an ADC-period claim.
"""
from __future__ import annotations

import math
import threading
from types import SimpleNamespace

import pytest

from test_esi_heater_activation import rig


def invoke(rig, operation, *, timeout=.4, cancel_event=None):
    if operation == 'snapshot':
        return rig.driver.collect_diagnostics(timeout_s=timeout, cancel_event=cancel_event)
    if operation == 'target':
        return rig.driver.set_heater_temperature(30., timeout_s=timeout, cancel_event=cancel_event)
    return rig.driver.set_output_active(0, True, timeout_s=timeout, cancel_event=cancel_event)


def no_positive_command(rig):
    assert not any(call[0] == 'temperature' and call[1] > 0 for call in rig.calls)
    assert ('module', 0, True) not in rig.calls
    assert ('enable', True) not in rig.calls


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('missing_polls', [0, 1, 2])
def test_monitoring_waits_for_bit_zero_and_reads_once(rig, monkeypatch, operation, missing_polls):
    seen = []
    def flags(self, address):
        assert address == 0
        assert self.thread_lock.locked()
        seen.append('flags')
        # Other bits must NOT be mistaken for monitoring-ready.
        return 0, 0x0e if seen.count('flags') <= missing_polls else 0x0f
    def monitor(self):
        assert seen.count('flags') == missing_polls + 1
        assert self.thread_lock.locked()
        seen.append('monitor')
        return 0, True, .43, .005, .002, 21.497
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    result = invoke(rig, operation)
    if operation == 'snapshot':
        assert result['heat']['valid'] and result['heat']['monitor_temperature_c'] == 21.497
    assert seen == ['flags'] * (missing_polls + 1) + ['monitor']
    assert rig.calls.count(('module', 0, True)) == (operation == 'on')
    assert rig.calls.count(('temperature', 30.)) == (operation == 'target')


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
def test_read_after_consumed_datum_waits_for_the_next_one(rig, monkeypatch, operation):
    state = SimpleNamespace(ready=True, polls=0, reads=0)
    def flags(self, address):
        state.polls += 1
        if state.polls >= 2:
            state.ready = True
        return 0, int(state.ready)
    def monitor(self):
        state.reads += 1
        if not state.ready:
            return 0, False, 0., 0., 0., 0.
        state.ready = False
        return 0, True, .430082, .00555, .001852, 21.497
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    assert monitor(rig.driver)[1] is True
    result = invoke(rig, operation)
    if operation == 'snapshot':
        assert result['heat']['valid'] is True
    assert state.polls == 2 and state.reads == 2


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('flags', [0, 2, 4, 0x0e])
def test_absent_data_expires_without_monitor_or_on(rig, monkeypatch, operation, flags):
    clock = SimpleNamespace(now=0., polls=0, sleeps=[])
    monkeypatch.setattr(rig.module.time, 'monotonic', lambda: clock.now)
    def sleep(delay):
        clock.sleeps.append(delay)
        clock.now += delay
    def readiness(self, address):
        clock.polls += 1
        return 0, flags
    monkeypatch.setattr(rig.module.time, 'sleep', sleep)
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', readiness)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', lambda self: pytest.fail('Must not read unavailable data'))
    with pytest.raises(RuntimeError, match='monitoring data not ready'):
        invoke(rig, operation, timeout=.25)
    assert clock.now == .25 and clock.polls == 3
    assert all(delay <= .1 for delay in clock.sleeps)
    assert rig.driver._transport_poisoned is False  # A readiness deadline is not a hung DLL.
    assert not rig.driver.thread_lock.locked()
    no_positive_command(rig)


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('valid,temperature', [(False, 0.), (True, math.nan), (True, 176.)])
def test_ready_invalid_is_not_retried_or_replaced(rig, monkeypatch, operation, valid, temperature):
    count = []
    def monitor(self):
        count.append('monitor')
        return 0, valid, 0., 0., 0., temperature
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    if operation == 'snapshot':
        result = invoke(rig, operation)['heat']
        assert result['valid'] is valid
        assert (math.isnan(result['monitor_temperature_c']) if math.isnan(temperature)
                else result['monitor_temperature_c'] == temperature)
    else:
        with pytest.raises(RuntimeError, match='sensor readback is invalid'):
            invoke(rig, operation)
    assert count == ['monitor']
    no_positive_command(rig)


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('failure', ['flags', 'monitor'])
def test_native_errors_are_not_retried(rig, monkeypatch, operation, failure):
    calls = []
    def flags(self, address):
        calls.append('flags')
        return (-15 if failure == 'flags' else 0), 1
    def monitor(self):
        calls.append('monitor')
        return -15, True, 0., 0., 0., 21.
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    with pytest.raises(RuntimeError, match='failed'):
        invoke(rig, operation)
    assert calls == (['flags'] if failure == 'flags' else ['flags', 'monitor'])
    no_positive_command(rig)


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('poison', [True, None])
@pytest.mark.parametrize('where', ['flags', 'monitor'])
def test_poison_or_unknown_state_after_native_return_blocks_further_calls(rig, monkeypatch, operation, poison, where):
    calls = []
    def flags(self, address):
        calls.append('flags')
        if where == 'flags':
            self._transport_poisoned = poison
        return 0, 1
    def monitor(self):
        calls.append('monitor')
        self._transport_poisoned = poison
        return 0, True, 0., 0., 0., 21.
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    with pytest.raises(RuntimeError):
        invoke(rig, operation)
    assert calls == (['flags'] if where == 'flags' else ['flags', 'monitor'])
    no_positive_command(rig)


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('when', ['before', 'flags', 'monitor'])
def test_stop_cancels_wait_and_prevents_late_positive_command(rig, monkeypatch, operation, when):
    stop = threading.Event()
    calls = []
    if when == 'before':
        stop.set()
    def flags(self, address):
        calls.append('flags')
        if when == 'flags':
            stop.set()
        return 0, 1
    def monitor(self):
        calls.append('monitor')
        stop.set()
        return 0, True, 0., 0., 0., 21.
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    with pytest.raises(InterruptedError, match='cancelled'):
        invoke(rig, operation, cancel_event=stop)
    assert calls == {'before': [], 'flags': ['flags'], 'monitor': ['flags', 'monitor']}[when]
    no_positive_command(rig)
    # The cancelled heating token never prevents explicit OFF.
    assert rig.driver.set_output_active(0, False, timeout_s=.2, cancel_event=stop) is False
    assert ('module', 0, False) in rig.calls


def test_stop_at_global_enable_worker_dispatch_never_enables(rig, monkeypatch):
    stop = threading.Event()
    original = rig.driver._call_locked_with_timeout
    def dispatch(method, timeout, action, *args, **kwargs):
        if action == 'set_global_active':
            stop.set()  # Before that native worker starts, not an in-flight write.
        return original(method, timeout, action, *args, **kwargs)
    monkeypatch.setattr(rig.driver, '_call_locked_with_timeout', dispatch)
    with pytest.raises(InterruptedError):
        rig.driver.set_output_active(0, True, timeout_s=.4, cancel_event=stop)
    assert ('enable', True) not in rig.calls
    assert rig.driver.set_global_active(False, timeout_s=.4, cancel_event=stop) is False
    assert ('enable', False) in rig.calls
    assert rig.driver.set_output_active(0, False, timeout_s=.4, cancel_event=stop) is False


def test_inline_public_facade_preserves_cancellation_event(rig, monkeypatch):
    # Exercise the actual public facade without constructing a vendor backend.
    monkeypatch.setattr(type(rig.driver), '__init__', lambda self, **kw: self.__dict__.update(rig.driver.__dict__))
    facade = rig.module.ESI('simulated', 16, process_backend=False)
    stop = threading.Event()
    stop.set()
    with pytest.raises(InterruptedError):
        facade.set_output_active(0, True, cancel_event=stop)
    no_positive_command(rig)
    stop.clear()
    assert facade.collect_diagnostics(cancel_event=stop)['heat']['valid'] is True


@pytest.mark.parametrize('operation', ['snapshot', 'target', 'on'])
@pytest.mark.parametrize('where', ['flags', 'monitor'])
def test_blocked_native_late_return_cannot_resume_sequence(rig, monkeypatch, operation, where):
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    calls = []
    def block():
        entered.set()
        assert release.wait(3)
        exited.set()
    def flags(self, address):
        calls.append('flags')
        if where == 'flags':
            block()
        return 0, 1
    def monitor(self):
        calls.append('monitor')
        if where == 'monitor':
            block()
        return 0, True, 0., 0., 0., 21.
    monkeypatch.setattr(rig.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(rig.base, 'get_heat_ctrl_monitoring', monitor)
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            invoke(rig, operation, timeout=.01)
        assert entered.is_set()
        assert rig.driver._transport_poisoned is True
        assert rig.driver.thread_lock.locked()
        with pytest.raises(RuntimeError):
            rig.driver.disconnect(timeout_s=.01)
        assert ('close',) not in rig.calls
    finally:
        release.set()
        assert exited.wait(2)
        rig.driver._pending_dll_call['thread'].join(2)
    assert calls == (['flags'] if where == 'flags' else ['flags', 'monitor'])
    assert rig.driver.thread_lock.locked()
    no_positive_command(rig)
    assert ('close',) not in rig.calls
