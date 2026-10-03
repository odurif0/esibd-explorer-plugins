"""Replay framing errors at series boundaries; never talk to real hardware."""
from __future__ import annotations

import json
import types

import pandas as pd
import pytest

from test_dmmr_zero_notebook import Clock, DMMR, ROOT, ns  # noqa: F401


class FramingFaultDMMR(DMMR):
    """A lost terminator leaves stale replies until the port buffers are cleared."""

    def __init__(self, clock, certificate, runtime):
        super().__init__(clock, certificate)
        self.ranges = {a: (0, True) for a in self.modules}
        self.original = dict(self.ranges)
        self.unsynced = self.injected = False
        self.persistent = self.block_purge = self.close_fails = False
        self.inject_at = 'restore'
        self.capture_active = False
        self.native_calls = []
        self.purge_calls = 0
        self.port_baud = 230400
        self._transport_error = ''
        self.format_status = str
        # Exercise the real runtime's old shutdown path in the red reproduction.
        for name in ('_raise_if_transport_poisoned', '_resolve_io_timeout', '_raise_on_status'):
            setattr(self, name, types.MethodType(getattr(runtime, name), self))
        self.runtime = runtime

    def connect(self, **kwargs):
        self.port_baud = 230400
        return super().connect(**kwargs)

    def get_current(self, **kwargs):
        assert self.port_baud == 230400, 'Recording at the baud reset by Purge'
        return super().get_current(**kwargs)

    def begin_startup_diagnostics(self, **kwargs):
        assert not self.capture_active
        self.capture_active = True
        self.native_calls = []
        return super().begin_startup_diagnostics(**kwargs)

    def note(self, name, *args):
        super().note(name, *args)
        if getattr(self, 'capture_active', False):
            self.native_calls.append((name, *args))

    def end_startup_diagnostics(self, **kwargs):
        super().end_startup_diagnostics(**kwargs)
        self.capture_active = False
        return repr(self.native_calls)

    def reply(self, result, operation):
        if self.unsynced:
            return (-13, *result[1:]) if isinstance(result, tuple) else -13
        if self.frames and operation == self.inject_at and not self.injected:
            self.injected = self.unsynced = True
            self.note('lost_terminator', operation)
            return (-12, *result[1:]) if isinstance(result, tuple) else -12
        return result

    def set_module_auto_range(self, address, enabled, **kwargs):
        result = super().set_module_auto_range(address, enabled, **kwargs)
        return self.reply(result, 'restore' if address == 6 else 'other')

    def set_module_meas_range(self, address, value, **kwargs):
        return self.reply(super().set_module_meas_range(address, value, **kwargs), 'other')

    def get_module_meas_range(self, address, **kwargs):
        return self.reply(super().get_module_meas_range(address, **kwargs), 'other')

    def set_automatic_current(self, enabled, **kwargs):
        return self.reply(super().set_automatic_current(enabled, **kwargs),
                          'stop' if not enabled else 'other')

    def get_automatic_current(self, **kwargs):
        return self.reply(super().get_automatic_current(**kwargs), 'other')

    def set_enable(self, enabled, **kwargs):
        return self.reply(super().set_enable(enabled, **kwargs),
                          'disable' if not enabled else 'other')

    def get_enable(self, **kwargs):
        return self.reply(super().get_enable(**kwargs), 'other')

    def get_state(self, **kwargs):
        return self.reply(super().get_state(**kwargs), 'other')

    def _call_locked_with_timeout(self, method, timeout, label, *args):
        if label == 'cleanup_purge':
            assert method.__name__ == 'purge'
            return method(*args)
        return super()._call_locked_with_timeout(method, timeout, label, *args)

    def purge(self):
        self.note('purge')
        self.purge_calls += 1
        if self.block_purge:
            self._transport_poisoned = True
            self.connected = False
            raise RuntimeError('blocked purge DLL')
        if not self.persistent:
            self.unsynced = False
        self.port_baud = 9600  # Verified in the bundled x64 DLL, Purge RVA 0x3ef0.
        return 0

    def disconnect(self):
        self.note('disconnect')
        if self.close_fails:
            return False
        self.connected = False
        return True

    def shutdown(self, **kwargs):
        self.note('shutdown')
        return self.runtime.shutdown(self, **kwargs)


@pytest.fixture
def boundary(ns, tmp_path):
    runtime = ns['load_driver'](ROOT / 'dmmr')._PROCESS_CONTROLLER_CLASS
    device = FramingFaultDMMR(Clock(), ns['CERTIFICATE'], runtime)
    cfg = dict(ns['PROTOCOL'], series_count=2, duration_s=2., bin_s=.5, tail_s=1.,
               stall_timeout_s=.5, io_timeout_s=.1, pause_s=1., diagnostics_interval_s=1.)
    output = tmp_path / 'campaign'

    def run():
        return ns['run_campaign'](device, output, cfg, clock=device.clock, sleep=device.clock.sleep)

    return device, cfg, output, run


@pytest.mark.parametrize('stage', ['restore', 'stop', 'disable'])
def test_returned_termination_error_has_one_verified_cleanup_recovery(boundary, stage):
    device, _, output, run = boundary
    device.inject_at = stage
    report = run()
    assert report['status'] == 'complete', report['error']
    assert report['completed_series'] == 2 and report['recorded_series'] == 2
    assert device.purge_calls == 1
    assert device.ranges == device.original
    assert not device.connected and not device.enabled and not device.automatic
    cleanup = report['series'][0]['cleanup']
    assert cleanup['ranges_restored'] and cleanup['shutdown_confirmed']
    assert any(e['status'] == -12 for e in cleanup['operations'])
    assert cleanup['recovery']['attempted'] and cleanup['recovery']['succeeded']
    assert cleanup['errors'] == []
    assert 'lost_terminator' in (output / 'series_01/native_cleanup.log').read_text()
    assert 'lost_terminator' not in (output / 'series_01/native_startup.log').read_text()
    first = device.calls.index(('purge',))
    assert device.calls[first + 1] == ('state',)
    # No purge in acquisition: all recorded points precede the cleanup failure.
    raw = pd.read_csv(output / 'series_01/raw.csv')
    assert len(raw) > 100 and raw.status.eq(0).all()
    assert set(raw.reason) <= {'selected', 'after_duration'}
    assert raw.loc[raw.selected.eq(1), 'host_elapsed_s'].max() < 2
    assert raw.actual_range.eq(0).all()


def test_persistent_framing_error_stops_without_reconnect_or_close(boundary):
    device, _, output, run = boundary
    device.persistent = True
    report = run()
    assert report['status'] == 'failed'
    assert report['completed_series'] == 0 and report['recorded_series'] == 1
    assert device.purge_calls == 1
    assert device.connected and not report['link_released']
    assert not report['series'][0]['cleanup']['shutdown_confirmed']
    assert device.calls.count(('connect',)) == 1
    assert ('disconnect',) not in device.calls
    assert not (output / 'series_02').exists()


def test_blocked_purge_never_makes_a_later_dll_call(boundary):
    device, _, _, run = boundary
    device.block_purge = True
    report = run()
    assert report['status'] == 'failed' and not report['link_released']
    assert device.calls[-1] == ('purge',)
    assert device.purge_calls == 1
    assert device.calls.count(('connect',)) == 1


def test_close_failure_keeps_the_link_reserved_and_stops(boundary):
    device, _, _, run = boundary
    device.inject_at = None
    device.close_fails = True
    report = run()
    assert report['status'] == 'failed' and not report['link_released']
    assert device.connected and not device.enabled and not device.automatic
    assert device.purge_calls == 0
    assert device.calls.count(('disconnect',)) == 1
    assert device.calls.count(('connect',)) == 1


def test_autorange_restoration_does_not_force_an_obsolete_fixed_range(boundary):
    device, _, _, run = boundary
    device.inject_at = None
    assert run()['status'] == 'complete'
    first_on = device.calls.index(('automatic', True))
    first_close = device.calls.index(('disconnect',))
    cleanup = device.calls[first_on + 1:first_close]
    assert not any(c[0] == 'range' for c in cleanup)
    assert not any(c[0] == 'autorange' and c[2] is False for c in cleanup)
    assert sum(c[0] == 'autorange' and c[2] is True for c in cleanup) == 8


def test_healthy_run_also_records_cleanup_native_trace(boundary):
    device, _, output, run = boundary
    device.inject_at = None
    assert run()['status'] == 'complete'
    for directory in sorted(output.glob('series_*')):
        child = json.loads((directory / 'report.json').read_text())
        assert child['cleanup_diagnostics']['ended']
        trace = (directory / 'native_cleanup.log').read_text()
        assert "('automatic', False)" in trace
        assert "('disconnect',)" in trace
        assert "('automatic', True)" not in trace


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_cleanup_recovery_is_limited_to_returned_receive_errors(boundary, status):
    device, _, _, run = boundary
    original = device.reply

    def reply(result, operation):
        value = original(result, operation)
        if value == -12:
            return status
        return value

    device.reply = reply
    report = run()
    assert report['status'] == 'complete'
    assert report['series'][0]['cleanup']['recovery']['first_status'] == status
    assert device.purge_calls == 1


@pytest.mark.parametrize('status', [-2, -14, -15, -200])
def test_other_errors_are_not_blindly_retried(boundary, status):
    device, _, _, run = boundary
    device.inject_at = None
    setter = device.set_module_auto_range

    def reject(address, enabled, **kwargs):
        if device.frames and address == 6:
            return status
        return setter(address, enabled, **kwargs)

    device.set_module_auto_range = reject
    report = run()
    assert report['status'] == 'failed'
    assert not report['series'][0]['cleanup']['ranges_restored']
    assert report['series'][0]['cleanup']['shutdown_confirmed']
    assert device.purge_calls == 0 and device.calls.count(('connect',)) == 1


@pytest.mark.parametrize('value', [True, 0, None])
def test_shutdown_needs_explicit_false_not_a_falsy_or_enabled_value(boundary, value):
    device, _, _, run = boundary
    device.inject_at = None
    getter = device.get_enable

    def wrong_readback(**kwargs):
        result = getter(**kwargs)
        return (0, value) if device.frames else result

    device.get_enable = wrong_readback
    report = run()
    assert report['status'] == 'failed' and not report['link_released']
    assert not report['series'][0]['cleanup']['off_confirmed']
    assert ('disconnect',) not in device.calls
    assert device.purge_calls == 0


def test_recovery_budget_is_shared_by_all_cleanup_commands(boundary):
    device, _, _, run = boundary
    setter = device.set_enable

    def second_error(enabled, **kwargs):
        value = setter(enabled, **kwargs)
        return -12 if device.purge_calls and not enabled else value

    device.set_enable = second_error
    report = run()
    assert report['status'] == 'failed' and not report['link_released']
    assert device.purge_calls == 1
    assert ('disconnect',) not in device.calls


def test_no_retry_of_original_command_without_a_healthy_controller_reply(boundary):
    device, _, _, run = boundary
    getter = device.get_state

    def bad_state(**kwargs):
        value = getter(**kwargs)
        return (0, '0x8000', 'device fault') if device.purge_calls else value

    device.get_state = bad_state
    report = run()
    assert report['status'] == 'failed' and device.purge_calls == 1
    cleanup = report['series'][0]['cleanup']
    assert not cleanup['recovery']['succeeded'] and not cleanup['ranges_restored']
    assert sum(c[0] == 'autorange' and c[1:] == (6, True) for c in device.calls) == 1
    assert cleanup['shutdown_confirmed']  # Both OFF gates remain verifiable.
    assert device.calls.count(('connect',)) == 1


def test_native_trace_write_error_cannot_bypass_shutdown(boundary, monkeypatch):
    from pathlib import Path

    device, _, _, run = boundary
    device.inject_at = None
    write = Path.write_text

    def failing_write(path, *args, **kwargs):
        if path.name in ('native_startup.log', 'native_cleanup.log'):
            raise OSError('trace volume write failed')
        return write(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', failing_write)
    report = run()
    assert report['status'] == 'complete' and report['link_released']
    assert device.calls.count(('disconnect',)) == 2
    assert not device.enabled and not device.automatic


@pytest.mark.parametrize('exception', [RuntimeError, KeyboardInterrupt])
def test_native_capture_start_failure_cannot_bypass_shutdown(boundary, exception):
    device, _, _, run = boundary
    device.inject_at = None
    begin = device.begin_startup_diagnostics

    def failing_begin(**kwargs):
        if device.frames and device.connected:
            raise exception('capture unavailable')
        return begin(**kwargs)

    device.begin_startup_diagnostics = failing_begin
    report = run()
    assert report['link_released'] and not device.connected
    assert not device.enabled and not device.automatic
    if exception is KeyboardInterrupt:
        assert report['status'] == 'interrupted'
        assert device.calls.count(('connect',)) == 1
    else:
        assert report['status'] == 'complete'


def test_recovery_does_not_change_recorded_measurements(ns, boundary):
    device, cfg, output, run = boundary
    assert run()['status'] == 'complete'
    healthy = FramingFaultDMMR(Clock(), ns['CERTIFICATE'], device.runtime)
    healthy.inject_at = None
    control = output.parent / 'control'
    report = ns['run_campaign'](healthy, control, cfg, clock=healthy.clock, sleep=healthy.clock.sleep)
    assert report['status'] == 'complete'
    for index in (1, 2):
        before = pd.read_csv(control / f'series_{index:02d}/raw.csv').drop(columns=['host_utc'])
        after = pd.read_csv(output / f'series_{index:02d}/raw.csv').drop(columns=['host_utc'])
        pd.testing.assert_frame_equal(before, after)


def test_purge_uses_the_real_ctypes_runtime_with_one_lock_at_a_time(ns):
    import logging
    import threading

    cls = ns['load_driver'](ROOT / 'dmmr')._PROCESS_CONTROLLER_CLASS
    backend = cls.__new__(cls)
    backend.thread_lock = threading.Lock()
    backend._transport_poisoned = False
    backend.logger = logging.getLogger(__name__)
    backend.err_dict = {'-12': 'Error receiving termination character'}
    calls = []

    class DLL:
        def COM_DMMR_8_SetEnable(self, enabled):
            assert backend.thread_lock.locked()
            calls.append(('enable', enabled.value))
            return -12 if len(calls) == 1 else 0

        def COM_DMMR_8_Purge(self):
            assert backend.thread_lock.locked()
            calls.append(('purge',))
            return 0

        def COM_DMMR_8_GetState(self, pointer):
            assert backend.thread_lock.locked()
            calls.append(('state',))
            pointer._obj.value = 0
            return 0

    backend.dll = DLL()
    report = {}
    command = ns['CleanupCommands'](backend, .2, report)
    assert command.call('disable', backend.set_enable, False) == ()
    assert calls == [('enable', False), ('purge',), ('state',), ('enable', False)]
    assert not backend.thread_lock.locked() and not backend._transport_poisoned
    assert report['recovery']['succeeded']


def test_blocked_cleanup_log_prevents_the_next_series(boundary):
    device, _, _, run = boundary
    device.inject_at = None
    end = device.end_startup_diagnostics

    def blocked_end(**kwargs):
        text = end(**kwargs)
        if not device.connected:
            device._transport_poisoned = True
            raise RuntimeError('DLL debug-log close timed out')
        return text

    device.end_startup_diagnostics = blocked_end
    report = run()
    assert report['status'] == 'failed' and report['recorded_series'] == 1
    assert report['series'][0]['status'] == 'cleanup_failed'
    assert report['series'][0]['cleanup']['shutdown_confirmed']
    assert not report['link_released'] and device.calls.count(('connect',)) == 1


def test_old_reports_keep_acquisition_success_separate_from_failed_cleanup(ns, boundary):
    device, _, output, run = boundary
    device.close_fails = True
    report = run()
    assert report['status'] == 'failed'
    report.pop('recorded_series')
    for entry in report['series']:
        entry.pop('acquisition_complete')
    (output / 'report.json').write_text(json.dumps(report))
    _, _, analysed = ns['analyse_campaign'](output)
    assert analysed['recorded_series'] == 1 and analysed['completed_series'] == 0
    assert analysed['status'] == 'failed'
    assert analysed['series'][0]['cleanup'] == json.loads(json.dumps(report['series'][0]['cleanup']))
    assert len(analysed['tail_summary']) == 8
