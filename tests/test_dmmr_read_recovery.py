"""Real runtime diagnostics and common bounded receive-error recovery."""
from __future__ import annotations

import ctypes
import json
import threading
from types import SimpleNamespace

import pytest

from test_dmmr_plugin_behavior import _load_module
from test_dmmr_startup_diagnostics import NativeFunction, native  # noqa: F401


class Device:
    NO_ERR, NO_DATA = 0, 1

    def __init__(self):
        self.connected = self.enabled = True
        self.automatic = self._transport_poisoned = False
        self.baudrate = self.port_baud = 230400
        self.calls, self.events = [], []
        self.ranges = {0: (0, False), 3: (0, False)}
        self.state_status = 0
        self.state = '0x0000'
        self.sticky = False
        self.purge_status = 0
        self.frames = 0

    def record_protocol_event(self, event):
        self.events.append(json.loads(json.dumps(event)))

    def get_state(self, **kw):
        self.calls.append('state')
        return (-13 if self.sticky else self.state_status), self.state, 'state'

    def get_enable(self, **kw):
        self.calls.append('get_enable')
        return 0, self.enabled

    def get_automatic_current(self, **kw):
        self.calls.append('get_auto')
        return 0, self.automatic

    def get_module_meas_range(self, address, **kw):
        self.calls.append(('range', address))
        return 0, *self.ranges[address]

    def _call_locked_with_timeout(self, method, timeout, label, *args):
        return method(*args)

    def purge(self):
        self.calls.append('purge')
        self.port_baud = 9600
        self.sticky = False
        return self.purge_status

    def set_baud_rate(self, baud):
        self.calls.append(('baud', baud))
        self.port_baud = baud
        return 0, baud

    def set_automatic_current(self, value, **kw):
        self.calls.append(('auto', value))
        self.automatic = value
        return 0

    def get_current(self, **kw):
        self.calls.append('fifo')
        if self.frames:
            self.frames -= 1
            return 0, 0, 1e-12, 0, 3.
        return 1, 0, 0., 0, 0.


@pytest.fixture
def rig():
    device = Device()
    clock = SimpleNamespace(now=0.)
    recovery = _load_module()._get_dmmr_driver_class().ReadRecovery(
        device, dict(device.ranges), automatic=False, timeout_s=.5, clock=lambda: clock.now)
    return device, recovery, clock


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_transient_error_verifies_readbacks_without_changing_any_setting(rig, status):
    device, recovery, _ = rig
    result = recovery.recover(status, 'current 3')
    assert result['verified'] and not result['purged'] and not result['resumed']
    assert device.calls == ['state', 'get_enable', 'get_auto', ('range', 0), ('range', 3)]
    recovery.note_sample(0)
    assert not result['resumed']
    recovery.note_sample(3)
    assert result['resumed']
    assert [e['kind'] for e in device.events] == ['read_recovery', 'read_resumed']
    assert device.events[0]['resumed'] is False  # No retrospective mutation of the log.


@pytest.mark.parametrize('automatic', [False, True])
def test_desynchronization_one_purge_restores_baud_and_verifies_before_resuming(rig, automatic):
    device, recovery, _ = rig
    device.sticky = True
    device.automatic = recovery.automatic = automatic
    device.frames = 3
    result = recovery.recover(-13, 'stream')
    assert result['verified'] and result['purged']
    assert result['discarded_frames'] == 3
    assert device.port_baud == 230400
    assert device.automatic is automatic
    assert device.calls.count('purge') == 1
    assert device.calls.index(('baud', 230400)) < device.calls.index(('auto', False))
    assert device.calls.index(('range', 3)) < device.calls.index('fifo')
    if automatic:
        assert device.calls[-2:] == [('auto', True), 'get_auto']


@pytest.mark.parametrize('fault', ['state', 'enable', 'auto', 'range', 'range_mode', 'malformed_state'])
def test_bad_hardware_readback_is_not_a_license_to_reconfigure(rig, fault):
    device, recovery, _ = rig
    if fault == 'state':
        device.state = '0x0001'
    elif fault == 'malformed_state':
        device.state = 'nonsense'
    elif fault == 'enable':
        device.enabled = False
    elif fault == 'auto':
        device.automatic = True
    elif fault == 'range':
        device.ranges[3] = (1, False)
    else:
        device.ranges[3] = (0, True)
    with pytest.raises((RuntimeError, ValueError)):
        recovery.recover(-12, 'current')
    assert recovery.failed and not recovery.last_incident['verified']
    assert 'purge' not in device.calls
    assert not any(isinstance(c, tuple) and c[0] == 'auto' for c in device.calls)


def test_auto_range_may_move_but_auto_flag_must_still_match(rig):
    device, recovery, _ = rig
    recovery.ranges[0] = (0, True)
    device.ranges[0] = (4, True)
    assert recovery.recover(-13, 'current')['verified']


def test_persistent_error_cannot_retry_before_every_module_has_a_new_sample(rig):
    device, recovery, _ = rig
    recovery.recover(-13, 'current')
    recovery.note_sample(0)
    before = list(device.calls)
    with pytest.raises(RuntimeError, match='Persistent'):
        recovery.recover(-13, 'current')
    assert device.calls == before
    assert recovery.failed


def test_recovery_rate_limit_and_terminal_failure(rig):
    device, recovery, clock = rig
    for _ in range(3):
        recovery.recover(-12, 'current')
        for address in device.ranges:
            recovery.note_sample(address)
        clock.now += 10
    before = list(device.calls)
    with pytest.raises(RuntimeError, match='budget'):
        recovery.recover(-12, 'current')
    clock.now += 120
    with pytest.raises(RuntimeError, match='budget'):
        recovery.recover(-12, 'current')
    assert before == device.calls


def test_separated_incidents_can_recover_during_a_long_run(rig):
    device, recovery, clock = rig
    for _ in range(20):
        assert recovery.recover(-12, 'current')['verified']
        for address in device.ranges:
            recovery.note_sample(address)
        clock.now += 61
    assert recovery.recovery_count == 20


@pytest.mark.parametrize('status', [-200, -7, 1])
def test_non_receive_error_is_not_retried(rig, status):
    device, recovery, _ = rig
    with pytest.raises(RuntimeError, match='non-recoverable'):
        recovery.recover(status, 'current')
    assert device.calls == []


def test_failed_purge_is_not_repeated(rig):
    device, recovery, _ = rig
    device.sticky = True
    device.purge_status = -12
    with pytest.raises(RuntimeError):
        recovery.recover(-13, 'current')
    assert device.calls == ['state', 'purge']
    assert recovery.failed
    assert recovery.last_incident['purge_attempted']
    assert 'may be lost' in recovery.last_incident['data_loss']


def test_stream_restart_requires_readback(rig):
    device, recovery, _ = rig
    device.sticky = True
    device.automatic = recovery.automatic = True
    def no_acknowledged_enable(enabled, **kw):
        device.automatic = False
        return 0
    device.set_automatic_current = no_acknowledged_enable
    with pytest.raises(RuntimeError, match='restart not confirmed'):
        recovery.recover(-13, 'stream')
    assert recovery.failed


def test_fifo_drain_is_bounded(rig):
    device, recovery, _ = rig
    device.sticky, device.frames = True, 2000
    with pytest.raises(RuntimeError, match='FIFO did not empty'):
        recovery.recover(-13, 'current')
    assert device.calls.count('fifo') == 1024
    assert recovery.failed


@pytest.mark.parametrize('phase', ['before', 'during', 'cancelled'])
def test_no_calls_after_blocked_dll_or_user_cancel(rig, phase):
    device, recovery, _ = rig
    if phase == 'before':
        device._transport_poisoned = True
    elif phase == 'cancelled':
        recovery.continue_check = lambda: False
    else:
        def blocked(**kw):
            device.calls.append('state')
            device._transport_poisoned = True
            raise RuntimeError('blocked DLL')
        device.get_state = blocked
    with pytest.raises(RuntimeError):
        recovery.recover(-13, 'current')
    assert device.calls == (['state'] if phase == 'during' else [])


def configure_faulty_native(native, *, blocked=None):
    driver = native.driver
    driver.err_dict = {'-12': 'Error receiving termination character'}
    def current(address, value, meas_range):
        assert driver.thread_lock.locked()
        native.calls.append(('current', threading.get_ident()))
        if blocked is not None:
            blocked.wait(2)
        value._obj.value = 666.  # Invalid payload must not become a measurement.
        return -12
    def io(value):
        assert driver.thread_lock.locked()
        native.calls.append(('io', threading.get_ident()))
        value._obj.value = -12
        return 0
    def comm(value):
        assert driver.thread_lock.locked()
        native.calls.append(('comm', threading.get_ident()))
        value._obj.value = 2
        return 0
    driver.dll.COM_DMMR_8_GetModuleCurent = NativeFunction(current)
    driver.dll.COM_DMMR_8_GetIOState = NativeFunction(io)
    driver.dll.COM_DMMR_8_GetCommError = NativeFunction(comm)
    driver._configure_dll_signatures()
    return driver


def test_real_runtime_captures_clear_on_read_diagnostics_under_original_lock(native):
    driver = configure_faulty_native(native)
    assert driver.get_module_current(3, timeout_s=.5)[0] == -12
    assert [x[0] for x in native.calls] == ['current', 'io', 'comm']
    assert len({x[1] for x in native.calls}) == 1
    assert native.calls[0][1] != threading.get_ident()
    report = driver.protocol_diagnostics()
    rows = [json.loads(line) for line in native.directory.joinpath('dmmr_protocol_com13.jsonl').read_text().splitlines()]
    assert rows == report['events']
    assert rows[0]['status'] == -12
    assert rows[0]['get_io_state'] == [0, -12]
    assert rows[0]['get_comm_error'] == [0, 2]
    assert 'module_current' in rows[0]['action']


def test_late_dll_return_never_starts_a_diagnostic_or_recovery_call(native):
    release = threading.Event()
    driver = configure_faulty_native(native, blocked=release)
    finished = threading.Event()
    original = driver._protocol_call_unlocked
    def observed(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()
    driver._protocol_call_unlocked = observed
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            driver.get_module_current(3, timeout_s=.02)
        assert driver._transport_poisoned
        recovery = driver.new_read_recovery({3: (0, False)}, automatic=False, timeout_s=.1)
        with pytest.raises(RuntimeError):
            recovery.recover(-12, 'current')
    finally:
        release.set()
        assert finished.wait(2)
    assert driver.thread_lock.locked()  # Poisoning deliberately retains the lock.
    assert [x[0] for x in native.calls] == ['current']


def test_unavailable_port_diagnostics_do_not_replace_original_error(native):
    driver = configure_faulty_native(native)
    del driver.dll.COM_DMMR_8_GetCommError
    assert driver.get_module_current(0, timeout_s=.5)[0] == -12
    event = driver.protocol_diagnostics()['events'][0]
    assert 'unavailable' in event['get_comm_error']
    assert event['status'] == -12


@pytest.mark.parametrize('failure', ['open', 'write'])
def test_unavailable_protocol_log_keeps_diagnostics_without_poisoning_transport(native, monkeypatch, failure):
    driver = configure_faulty_native(native)
    if failure == 'open':
        target = native.directory / 'not_a_directory'
        target.write_text('occupied')
        driver.configure_protocol_log(target)
    else:
        driver.configure_protocol_log()
        handler = driver._protocol_handler
        handler.stream.close()
        handler.stream = None
        def fail_open():
            raise OSError('protocol log disk full')
        monkeypatch.setattr(handler, '_open', fail_open)
    result = driver.get_module_current(0, timeout_s=.5)
    assert result[0] == -12
    report = driver.protocol_diagnostics()
    assert report['log_error']
    assert report['events'][0]['status'] == -12
    assert report['events'][0]['get_io_state'] == [0, -12]
    assert not driver._transport_poisoned


def test_protocol_event_memory_and_log_files_are_bounded(native):
    driver = native.driver
    driver.configure_protocol_log()
    driver._protocol_handler.maxBytes = 1000
    for _ in range(180):
        driver.record_protocol_event({'kind': 'test', 'message': 'x' * 100})
    report = driver.protocol_diagnostics()
    assert report['event_count'] == 180 and len(report['events']) == 128
    files = list(native.directory.glob('dmmr_protocol_com13.jsonl*'))
    assert len(files) == 4 and all(path.stat().st_size < 1300 for path in files)


def test_each_series_has_separate_protocol_events_without_overwriting_old_log(native):
    driver = configure_faulty_native(native)
    driver.configure_protocol_log(native.directory / 'first')
    driver.get_module_current(0, timeout_s=.5)
    previous = driver.protocol_diagnostics()
    from pathlib import Path
    original = Path(previous['file']).read_bytes()
    driver.configure_protocol_log(native.directory / 'second')
    assert driver.protocol_diagnostics()['event_count'] == 0
    driver.get_module_current(0, timeout_s=.5)
    assert driver.protocol_diagnostics()['event_count'] == 1
    assert driver.protocol_diagnostics()['file'] != previous['file']
    assert Path(previous['file']).read_bytes() == original


def test_slow_nonempty_fifo_is_bounded_by_time_not_1024_timeouts(rig):
    device, recovery, clock = rig
    device.sticky = True
    def slow(**kw):
        assert 0 < kw['timeout_s'] <= recovery.timeout_s
        device.calls.append('fifo')
        clock.now += .1
        return 0, 0, 1e-12, 0, clock.now
    device.get_current = slow
    with pytest.raises(TimeoutError, match='within the read timeout'):
        recovery.recover(-13, 'current')
    assert device.calls.count('fifo') == 5
    assert recovery.failed


def test_blocking_io_diagnostic_does_not_start_comm_diagnostic_after_timeout(native):
    driver = configure_faulty_native(native)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    def io(value):
        native.calls.append(('io', threading.get_ident()))
        entered.set()
        release.wait(2)
        value._obj.value = -12
        return 0
    driver.dll.COM_DMMR_8_GetIOState = NativeFunction(io)
    driver._configure_dll_signatures()
    original = driver._protocol_call_unlocked
    def observed(*args, **kw):
        try:
            return original(*args, **kw)
        finally:
            finished.set()
    driver._protocol_call_unlocked = observed
    try:
        with pytest.raises((RuntimeError, TimeoutError)):
            driver.get_module_current(0, timeout_s=.05)
        assert entered.is_set() and driver._transport_poisoned
    finally:
        release.set()
        assert finished.wait(2)
    assert [c[0] for c in native.calls] == ['current', 'io']
    assert driver.protocol_diagnostics()['events'][-1]['transport_poisoned'] is True
