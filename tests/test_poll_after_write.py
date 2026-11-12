"""A verified write is read back by a poll at once, not at the end of the poll interval.

The 11/04 map (2.02 s per point for a 0.8 s window and 0.3 s of settling) spent about 1 s per point
outside the measurement. The timeline of its lines (HDF5 windows against the PSU and AMPR logs)
showed the same gap whether the AMPR was written or not: after each write, MScan waited for the next
1 s poll to publish a fresh readback within tolerance.
"""
from __future__ import annotations

import contextlib
import threading
import time
from types import SimpleNamespace

from support import ampr, psu


class _TimeoutLock:
    def __init__(self):
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def acquire_timeout(self, timeout, timeoutMessage=''):
        acquired = self._lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()


def _second_poll_after(loop_owner, polls, request):
    """Start the loop, let it poll once and fall asleep, then request a poll; seconds until the next one."""
    thread = threading.Thread(target=loop_owner.runAcquisition, daemon=True)
    thread.start()
    deadline = time.monotonic() + 2
    while not polls and time.monotonic() < deadline:
        time.sleep(.005)
    time.sleep(.1)
    asked = time.monotonic()
    request()
    while len(polls) < 2 and time.monotonic() < asked + 3:
        time.sleep(.005)
    loop_owner.acquiring = False
    thread.join(2)
    return polls[1] - asked if len(polls) > 1 else float('inf')


def test_a_finished_psu_write_is_read_back_at_once():
    _, _, c = psu.make_psu()
    polls = []
    c.lock, c.acquiring = _TimeoutLock(), True
    c.getDevice = lambda: SimpleNamespace(interval=10_000)  # 10 s: only the request can explain a poll.
    c.readNumbers = lambda **kw: polls.append(time.monotonic())
    c.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=lambda: None))
    c._sync_status_to_gui = lambda **kw: None
    c._manual_apply_pending_state, c._manual_apply_worker_running = None, True
    # The apply worker ends (nothing more queued): the poll that follows publishes the written values.
    assert _second_poll_after(c, polls, c._manual_state_apply_worker) < .5
    assert not c._manual_apply_worker_running


def test_a_finished_ampr_write_refreshes_the_monitors_at_once(monkeypatch):
    module = ampr.load_plugin(module_name='ampr_poll_after_write_test')
    cls = module.AMPRController
    c = cls.__new__(cls)
    c.__dict__.update(acquiring=True, _native_session_token=object(), acquisitionThread=None,
                      _setpoint_lock=threading.Lock(), _pending_setpoints={}, _setpoint_thread=object(),
                      transitioning=False, ramping=False)
    polls = []
    c.readNumbers = lambda **kw: polls.append(time.monotonic())
    c.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=lambda: None))
    monkeypatch.setattr(module, '_AMPR_MONITOR_INTERVAL_S', 10.)
    # The setpoint worker has sent everything and releases its ownership.
    assert _second_poll_after(c, polls, c._write_pending_setpoints) < .5
    assert c._setpoint_thread is None


def test_without_a_write_the_poll_keeps_its_interval():
    _, _, c = psu.make_psu()
    polls = []
    c.lock, c.acquiring = _TimeoutLock(), True
    c.getDevice = lambda: SimpleNamespace(interval=300)
    c.readNumbers = lambda **kw: polls.append(time.monotonic())
    c.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=lambda: None))
    thread = threading.Thread(target=c.runAcquisition, daemon=True)
    thread.start()
    time.sleep(1.)
    c.acquiring = False
    thread.join(2)
    gaps = [b - a for a, b in zip(polls, polls[1:])]
    assert 2 <= len(gaps) <= 4 and min(gaps) > .25, gaps


def test_the_two_rails_written_in_one_gui_call_make_one_apply(monkeypatch):
    """MScan writes CH0 then CH1 in one GUI call: one apply carries both (2026-11-12 lab: two per point)."""
    module, _, c = psu.make_psu()
    turns = []
    monkeypatch.setattr(module, '_start_on_next_gui_turn', lambda start: turns.append(start) or True)
    applied = []
    c.applyManualState = lambda state, cancel=None: applied.append(state)
    c._sync_status_to_gui = lambda **kw: None
    c._queue_manual_state_apply({'setpoints_only': True, 'voltage_values': {0: 80.5}})
    assert c._manual_apply_worker_running, 'busy at once: MScan must not see a stale readback as fresh'
    c._queue_manual_state_apply({'setpoints_only': True, 'voltage_values': {1: 80.5}})
    assert len(turns) == 1 and not applied
    turns[0]()
    deadline = time.monotonic() + 2
    while c._manual_apply_worker_running and time.monotonic() < deadline:
        time.sleep(.005)
    assert [state['voltage_values'] for state in applied] == [{0: 80.5, 1: 80.5}]
    assert not c._manual_apply_worker_running
