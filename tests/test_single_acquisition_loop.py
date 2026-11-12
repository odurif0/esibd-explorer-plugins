"""One acquisition loop per device.

On 2026-11-11 two loops polled the ESI after an OFF/ON. Explorer 1.0.2 checks for a running loop and then
starts one, without a lock. AMPR and DMMR also start their loop from the ON thread, and AMX and AMX HD
restart theirs after a transition. Each of them serializes its starts and ends a loop that a newer one
replaced. On 11/11 these four devices polled at their own rate all day; the guard is preventive.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from support import ampr, amx, dmmr

CONTROLLERS = {
    'ampr_a': (lambda: ampr.load_plugin(module_name='ampr_single_loop_test'), 'AMPRController'),
    'amx_a': (lambda: amx.load_plugin('amx_a', 'amx_a_single_loop_test'), 'AMXController'),
    'amx_hd': (lambda: amx.load_plugin('amx_hd', 'amx_hd_single_loop_test'), 'AMXHDController'),
    'dmmr': (lambda: dmmr.load_plugin(module_name='dmmr_single_loop_test'), 'DMMRController'),
}


def _controller(folder):
    load, name = CONTROLLERS[folder]
    cls = getattr(load(), name)
    c = cls.__new__(cls)  # Only the acquisition start and loop are exercised.
    c.__dict__.update(initialized=True, acquiring=False, acquisitionThread=None, _native_session_token=object(),
                      controllerParent=SimpleNamespace(name=folder), print=lambda *a, **k: None)
    return cls, c


def _explorer_start_acquisition(self):
    """Explorer 1.0.2 DeviceController.startAcquisition, with the gap that let two callers both start."""
    if self.acquisitionThread and self.acquisitionThread.is_alive():
        self.acquiring = False
        self.acquisitionThread.join(timeout=5)
    time.sleep(.05)
    self.acquisitionThread = threading.Thread(target=self.runAcquisition, daemon=True)
    self.acquiring = True
    self.acquisitionThread.start()


def _waiting_thread():
    stop = threading.Event()
    thread = threading.Thread(target=stop.wait, daemon=True)
    thread.start()
    return thread, stop


@pytest.mark.parametrize('folder', CONTROLLERS)
def test_only_a_newer_live_loop_supersedes_one(folder):
    _, c = _controller(folder)
    assert not c._superseded_loop()
    c.acquisitionThread = threading.current_thread()
    assert not c._superseded_loop()
    c.acquisitionThread = SimpleNamespace(is_alive=lambda: True)  # A test fake is not a loop.
    assert not c._superseded_loop()
    newer, stop = _waiting_thread()
    c.acquisitionThread = newer
    assert c._superseded_loop()
    stop.set()
    newer.join(2)
    assert not c._superseded_loop()


@pytest.mark.parametrize('folder', CONTROLLERS)
def test_a_superseded_loop_ends_without_polling(folder):
    _, c = _controller(folder)
    polled = []
    c.readNumbers = c.fakeNumbers = lambda *a, **k: polled.append(threading.current_thread())
    newer, stop = _waiting_thread()
    c.acquiring, c.acquisitionThread = True, newer
    old = threading.Thread(target=c.runAcquisition, daemon=True)
    old.start()
    old.join(2)
    stop.set()
    assert not old.is_alive() and not polled and c.acquiring


@pytest.mark.parametrize('folder', CONTROLLERS)
def test_concurrent_starts_poll_the_device_once(folder, monkeypatch):
    cls, c = _controller(folder)
    base = next(k for k in cls.__mro__[1:] if 'startAcquisition' in vars(k))
    monkeypatch.setattr(base, 'startAcquisition', _explorer_start_acquisition)
    polls = []

    def loop():  # The loop body of each plugin is tested above; its exit condition is the real one.
        me = threading.current_thread()
        while c.acquiring and not c._superseded_loop():
            polls.append((me, time.monotonic()))
            time.sleep(.01)
    c.runAcquisition = loop
    c.startAcquisition()  # The loop of the previous connection.
    time.sleep(.05)
    barrier = threading.Barrier(2)
    starters = [threading.Thread(target=lambda: (barrier.wait(), c.startAcquisition())) for _ in range(2)]
    for thread in starters:
        thread.start()
    for thread in starters:
        thread.join(5)
    time.sleep(.2)
    since = time.monotonic() - .1
    pollers = {thread for thread, at in polls if at > since}
    c.acquiring = False
    assert pollers == {c.acquisitionThread}, pollers


@pytest.mark.parametrize('folder', ['amx_a', 'amx_hd'])
def test_a_restart_after_a_transition_waits_for_a_start_in_progress(folder):
    _, c = _controller(folder)
    c.runAcquisition = lambda: None
    with c.__dict__.setdefault('_acquisition_start_lock', threading.Lock()):
        restart = threading.Thread(target=c._restart_acquisition_after_transition, daemon=True)
        restart.start()
        restart.join(.2)
        assert restart.is_alive() and c.acquisitionThread is None
    restart.join(2)
    assert not restart.is_alive() and c.acquisitionThread is not None
