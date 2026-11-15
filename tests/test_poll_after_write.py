"""A verified write is read back by a poll at once, not at the end of the poll interval.

The 11/04 map (2.02 s per point for a 0.8 s window and 0.3 s of settling) spent about 1 s per point
outside the measurement. The timeline of its lines (HDF5 windows against the PSU and AMPR logs)
showed the same gap whether the AMPR was written or not: after each write, MScan waited for the next
1 s poll to publish a fresh readback within tolerance.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from support import ampr, explorer, psu


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
    c.lock, c.acquiring = explorer.TimeoutLock(), True
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
    c.lock, c.acquiring = explorer.TimeoutLock(), True
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


def _housekeeping_psu():
    """A PSU controller on Explorer's lock, with a device that answers housekeeping reads."""
    _, parent, c = psu.make_psu()
    reads, messages = [], []
    c.print = parent.print = lambda text, **kw: messages.append(text)
    parent.poll_timeout_s = 5.
    c.device = SimpleNamespace(connected=True, _transport_poisoned=False,
                               collect_housekeeping=lambda timeout_s: reads.append(time.monotonic()) or psu.snapshot())
    c.main_state, c.lock = "ST_ON", explorer.TimeoutLock()
    c.signalComm = SimpleNamespace(updateValuesSignal=SimpleNamespace(emit=lambda: None))
    return parent, c, reads, messages


def test_a_recording_psu_is_read_by_the_loop_that_holds_the_lock():
    """Found 2026-11-14: the acquisition loop holds Explorer's lock, which is not reentrant, and the
    read took it again. With PSU recording ON every poll waited 1 s, failed with "Could not acquire
    lock to read PSU housekeeping" and invalidated the readbacks."""
    _, c, reads, messages = _housekeeping_psu()
    c.getDevice = lambda: SimpleNamespace(interval=100)
    c.acquiring = True
    thread = threading.Thread(target=c.runAcquisition, daemon=True)
    thread.start()
    time.sleep(.7)
    c.acquiring = False
    thread.join(3)
    assert len(reads) >= 3, (reads, messages)
    assert not [text for text in messages if "Could not acquire lock" in text]
    assert c._hv_readback is not None and c._hv_readback["values"] == {0: 123., 1: 234.}


def test_a_psu_that_is_not_recording_is_read_at_once_after_a_write():
    """2026-11-13 lab, PSU not recording: nothing was woken after a write and each MScan point waited
    for the next 1 s tick of the device's refresh timer."""
    parent, c, reads, _ = _housekeeping_psu()
    c._manual_apply_pending_state, c._manual_apply_worker_running = None, True
    c._sync_status_to_gui = lambda **kw: None
    c._manual_state_apply_worker()  # The apply worker ends: nothing more is queued.
    deadline = time.monotonic() + 2
    while not reads and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(reads) == 1


def test_the_refresh_timer_leaves_a_recording_psu_to_its_loop_and_follows_the_interval():
    parent, c, reads, _ = _housekeeping_psu()
    intervals = []
    parent._refreshTimer = SimpleNamespace(interval=lambda: 1000, setInterval=intervals.append)
    parent.interval = 250
    stop = threading.Event()
    c.acquisitionThread = threading.Thread(target=stop.wait, daemon=True)
    c.acquisitionThread.start()
    c.acquiring = True
    parent._refresh_tick()
    time.sleep(.2)
    stop.set()
    c.acquisitionThread.join(2)
    assert not reads, "one reader: the acquisition loop"
    assert intervals == [250], "the Interval setting reaches the timer without an Explorer restart"
    parent._refresh_tick()  # The loop has ended (recording OFF): the timer reads again.
    deadline = time.monotonic() + 2
    while not reads and time.monotonic() < deadline:
        time.sleep(.01)
    assert len(reads) == 1
