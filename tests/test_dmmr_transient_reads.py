"""Receive faults during real notebook functions and the plugin polling loop."""
from __future__ import annotations

import numpy as np
import pytest

from test_dmmr_zero_notebook import ns, rig  # noqa: F401
from test_dmmr_acquisition_regressions import rig as plugin_rig  # noqa: F401


def inject_stream_fault(device, *, status=-13, persistent=False, sticky=False, malformed=False, after_s=None):
    original = device.get_current
    original_state, original_purge = device.get_state, device.purge
    faulted = False
    desynchronized = False

    def frame(**kw):
        nonlocal faulted, desynchronized
        due = device.clock() >= after_s if after_s is not None else device.frames >= 32
        if device.automatic and due and (not faulted or persistent):
            faulted = True
            desynchronized = sticky
            device.clock.sleep(.01)
            return status, 0, 666., 0, 1e6
        return original(**kw)

    def state(**kw):
        if desynchronized:
            return -13, '0x0000', 'ST_ON'
        if malformed and faulted and device.automatic:
            return 0, '0x0001', 'ST_ERR'
        return original_state(**kw)

    def purge():
        nonlocal desynchronized
        desynchronized = False
        device.clock.sleep(.15)
        return original_purge()

    device.get_current, device.get_state, device.purge = frame, state, purge


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_notebook_transient_receive_error_keeps_original_error_and_continues(rig, status):
    device, cfg, output, run = rig
    inject_stream_fault(device, status=status)
    report, raw, summary, saved = run()
    assert report['status'] == 'complete', report['error']
    assert cfg['duration_s'] <= report['acquisition']['duration_s'] < cfg['duration_s'] + .1
    faults = raw.loc[raw.status.eq(status)]
    assert len(faults) == 1 and faults.selected.eq(0).all()
    assert faults.current_A.isna().all() and faults.device_time_s.isna().all()
    assert not raw.current_A.eq(666.).any()
    assert summary.N.sum() == raw.selected.sum()
    assert len(report['acquisition']['read_recoveries']) == 1
    incident = report['acquisition']['read_recoveries'][0]
    assert incident['status'] == status and incident['resumed'] and not incident['purged']
    assert incident['resumed_elapsed_s'] >= incident['finished_elapsed_s']
    assert raw.loc[raw.selected.eq(1)].host_elapsed_s.max() > faults.host_elapsed_s.iloc[0]
    assert (output / 'report.json').is_file()
    assert saved['cleanup']['shutdown_confirmed']
    assert len([c for c in device.calls if c == ('automatic', True)]) == 1


def test_notebook_parser_error_can_recover_without_accepting_a_fake_frame(rig):
    device, _, _, run = rig
    parse = device.check_auto_input
    failed = False
    def one_bad_parser_reply():
        nonlocal failed
        if device.automatic and device.frames >= 32 and not failed:
            failed = True
            return -13, 1
        return parse()
    device.check_auto_input = one_bad_parser_reply
    report, raw, *_ = run()
    assert report['status'] == 'complete'
    assert raw.status.eq(-13).sum() == 1
    assert report['acquisition']['read_recoveries'][0]['resumed']


def test_notebook_sticky_error_records_purge_and_omits_fit_across_restart(rig):
    device, cfg, _, run = rig
    cfg['baseline_window_s'] = .5
    inject_stream_fault(device, sticky=True)
    report, raw, _, saved = run()
    assert report['status'] == 'complete', report['error']
    incident = report['acquisition']['read_recoveries'][0]
    assert incident['purged'] and incident['resumed']
    assert 'lost sample count unknown' in incident['data_loss']
    assert device.port_baud == 230400
    assert device.calls.count(('purge',)) == 1
    assert device.calls.count(('automatic', True)) == 2
    assert saved['baseline_windows']
    for window in saved['baseline_windows']:
        assert not (window['start_s'] < incident['resumed_elapsed_s']
                    and window['end_s'] > incident['start_elapsed_s'])
    assert raw.loc[raw.status.ne(0), 'current_A'].isna().all()


def test_notebook_persistent_error_stops_without_a_retry_loop(rig):
    device, _, _, run = rig
    inject_stream_fault(device, persistent=True)
    report, raw, *_ = run()
    assert report['status'] == 'failed'
    assert 'Persistent' in report['error']
    assert len(report['acquisition']['read_recoveries']) == 2
    assert raw.status.eq(-13).sum() == 2
    assert report['cleanup']['shutdown_confirmed']
    assert not device.connected


def test_notebook_changed_controller_state_is_not_repaired_blindly(rig):
    device, _, _, run = rig
    inject_stream_fault(device, malformed=True)
    report, raw, *_ = run()
    assert report['status'] == 'failed'
    assert 'Controller state is not ST_ON' in report['error']
    assert not any(c[0] == 'purge' for c in device.calls)
    assert raw.status.eq(-13).sum() == 1


def test_notebook_no_new_frame_after_verified_recovery_still_triggers_watchdog(rig):
    device, _, _, run = rig
    inject_stream_fault(device)
    old = device.record_protocol_event
    def stop_producing_frames(event):
        old(event)
        if event['kind'] == 'read_recovery' and event['verified']:
            device.fault = 'stalled'
    device.record_protocol_event = stop_producing_frames
    report, *_ = run()
    assert report['status'] == 'failed'
    assert 'No new valid reading' in report['error']
    assert not report['acquisition']['read_recoveries'][0]['resumed']
    assert report['cleanup']['shutdown_confirmed']


def test_campaign_keeps_recovery_metadata_and_shades_actual_gap(ns, rig):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    device, cfg, output, _ = rig
    inject_stream_fault(device, sticky=True)
    cfg.update(series_count=1)
    ns['run_campaign'](device, output, cfg, clock=device.clock, sleep=device.clock.sleep)
    _, telemetry, report = ns['analyse_campaign'](output)
    assert report['status'] == 'complete'
    assert len(report['series'][0]['read_recoveries']) == 1
    figures = ns['plot_campaign'](report, telemetry)
    assert len(figures['current_vs_time'].axes[0].patches) == 1
    for figure in figures.values():
        figure.canvas.draw()
        plt.close(figure)


def start_plugin(rig):
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    rig.device.connected = True
    rig.device._transport_poisoned = False
    rig.device.calls.clear()
    def disconnect():
        rig.device.calls.append(('disconnect',))
        rig.device.connected = False
        return True
    rig.device.disconnect = disconnect


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_plugin_transient_read_is_nan_then_a_new_verified_cycle(plugin_rig, status):
    rig = plugin_rig
    start_plugin(rig)
    getter = rig.device.get_module_current
    failed = False
    def bad_once(address, **kw):
        nonlocal failed
        if not failed:
            failed = True
            return status, 666., 0
        return getter(address, **kw)
    rig.device.get_module_current = bad_once
    rig.controller.readNumbers()
    assert rig.controller.acquiring
    assert all(np.isnan(v) for v in rig.controller.values.values())
    first_token = rig.controller.measurementSnapshot()[2]
    rig.controller.readNumbers()
    assert rig.controller.values == {0: 1e-12, 3: 4e-12}
    assert rig.controller.measurementSnapshot()[2] is not first_token
    assert rig.controller._read_recovery.last_incident['resumed']
    assert not any('mode was active' in line for line in rig.logs)
    assert not any(call == ('automatic', False) for call in rig.device.calls)


@pytest.mark.parametrize('stop_confirmed', [True, False])
def test_plugin_persistent_error_stops_and_retains_uncertainty(plugin_rig, stop_confirmed):
    rig = plugin_rig
    start_plugin(rig)
    rig.device.get_module_current = lambda *a, **kw: (-13, 666., 0)
    rig.controller.readNumbers()
    assert rig.controller.acquiring
    if not stop_confirmed:
        # Initial verification still succeeds. The failed OFF readback must
        # prevent port closure and preserve the next OFF action.
        rig.device.get_enable = lambda **kw: (0, True)
    rig.controller.readNumbers()
    assert not rig.controller.acquiring
    assert all(np.isnan(v) for v in rig.controller.values.values())
    assert (('disconnect',) in rig.device.calls) is stop_confirmed
    assert rig.controller.initialized is not stop_confirmed
    assert rig.controller.main_state == ('Disconnected' if stop_confirmed else 'Shutdown unconfirmed')
    assert rig.ui_states[-1] is not stop_confirmed


@pytest.mark.parametrize('operation', ['state', 'current'])
def test_plugin_blocked_dll_never_queries_or_closes_the_same_transport(plugin_rig, operation):
    rig = plugin_rig
    start_plugin(rig)
    def blocked(*args, **kw):
        rig.device._transport_poisoned = True
        raise RuntimeError('DMMR instance is now marked unusable after DLL call timed out')
    if operation == 'state':
        rig.device.get_state = blocked
    else:
        rig.device.get_module_current = blocked
    def forbidden(*a, **kw):
        pytest.fail('Another device call was made after poisoning')
    for name in ['get_device_state', 'get_voltage_state', 'get_temperature_state']:
        setattr(rig.device, name, forbidden if operation == 'state' else lambda **kw: (0, 0, []))
    rig.device.disconnect = forbidden
    rig.controller.readNumbers()
    assert not rig.controller.acquiring
    assert rig.controller.device is rig.device and rig.controller.initialized
    assert rig.controller.main_state == 'Shutdown unconfirmed'
    assert rig.ui_states[-1] is True
    assert not any(c[0] in ('automatic', 'enable', 'disconnect') for c in rig.device.calls)


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_plugin_range_read_at_on_retries_read_only_not_writes(plugin_rig, status):
    rig = plugin_rig
    recovery_class = rig.module._get_dmmr_driver_class().CommandRecovery
    rig.device.new_command_recovery = lambda **kw: recovery_class(rig.device, **kw)
    getter = rig.device.get_module_meas_range
    failed = False
    def bad_once(address, **kw):
        nonlocal failed
        if not failed:
            failed = True
            return status, 0, False
        return getter(address, **kw)
    rig.device.get_module_meas_range = bad_once
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    assert len([c for c in rig.device.calls if c[0] == 'auto_range']) == 2
    assert rig.controller._verified_read_ranges == {0: (0, True), 3: (0, True)}


def test_notebook_preserves_reset_device_time_in_a_new_segment(rig):
    device, cfg, _, run = rig
    cfg['baseline_window_s'] = .5
    inject_stream_fault(device, sticky=True)
    purge, get_frame = device.purge, device.get_current
    reset_at = None
    def reset():
        nonlocal reset_at
        status = purge()
        reset_at = device.clock()
        return status
    def frame(**kw):
        reply = list(get_frame(**kw))
        if reply[0] == 0 and reset_at is not None:
            reply[4] -= reset_at
        return tuple(reply)
    device.purge, device.get_current = reset, frame
    report, raw, summary, saved = run()
    assert report['status'] == 'complete', report['error']
    selected = raw.loc[raw.selected.eq(1)]
    assert set(selected.segment) == {0, 1}
    assert selected.groupby('address').device_time_s.diff().lt(0).any()
    assert selected.groupby(['address', 'segment']).device_time_s.diff().dropna().gt(0).all()
    assert summary.drift_fA_min.isna().all()  # No fit through the restart.
    assert set(summary.time_source) == {'host_elapsed_s'}
    assert report['acquisition']['read_recoveries'][0]['segment_after'] == 1
    assert saved['baseline_windows']


@pytest.mark.parametrize('bad', [(0, np.nan, 0), (0, 1e-12, 2)])
def test_plugin_recovery_requires_valid_values_in_the_verified_range(plugin_rig, bad):
    rig = plugin_rig
    start_plugin(rig)
    rig.controller._verified_read_ranges = {0: (0, False), 3: (0, False)}
    rig.device.get_module_meas_range = lambda *a, **kw: (0, 0, False)
    rig.device.get_module_current = lambda *a, **kw: (-13, np.nan, 0)
    rig.controller.readNumbers()
    assert rig.controller.acquiring
    rig.device.get_module_current = lambda *a, **kw: bad
    rig.controller.readNumbers()
    assert not rig.controller.acquiring and not rig.controller.initialized
    assert rig.controller.main_state == 'Disconnected'
    assert not rig.controller._read_recovery.last_incident['resumed']
    assert all(np.isnan(v) for v in rig.controller.values.values())


def test_explicit_off_finished_during_recovery_is_not_relabelled_unconfirmed(plugin_rig):
    from contextlib import contextmanager
    rig = plugin_rig
    start_plugin(rig)
    section = rig.controller._controller_lock_section
    @contextmanager
    def finish_off_before_failure_handler(message, **kw):
        with section(message, **kw) as acquired:
            if message == 'Could not acquire DMMR stop lock.':
                # Another caller has completed and verified OFF/close before
                # the failure handler acquired the lock. No extra DLL call.
                rig.device.connected = False
                rig.controller.device = None
                rig.controller.initialized = False
                rig.controller.main_state = 'Disconnected'
            yield acquired
    rig.controller._controller_lock_section = finish_off_before_failure_handler
    rig.controller._stop_after_read_failure(RuntimeError('cancelled'))
    assert rig.controller.main_state == 'Disconnected'
    assert not rig.controller.initialized and rig.controller.device is None
    assert rig.ui_states[-1] is False
    assert rig.device.calls == []
