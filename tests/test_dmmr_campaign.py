"""Execute the canonical notebook: campaign timing, diagnostics and safe stopping."""
from __future__ import annotations

import collections
import contextlib
import io
import json
import math
import threading
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from test_dmmr_zero_notebook import Clock, DMMR, ROOT, assert_restored_ranges, ns  # noqa: F401


@pytest.fixture
def campaign(ns, tmp_path):
    clock = Clock()
    device = DMMR(clock, ns['CERTIFICATE'])
    cfg = dict(ns['PROTOCOL'], series_count=3, duration_s=5., bin_s=1., tail_s=2.,
               stall_timeout_s=1., io_timeout_s=.1, pause_s=2., diagnostics_interval_s=2.)
    output = tmp_path / 'campaign'

    def run():
        return ns['run_campaign'](device, output, cfg, clock=clock, sleep=clock.sleep)

    return device, cfg, output, run


def inject_reconnect_range_failure(device):
    """Replay g2 reply loss, then g30 malformed ACK and desynchronization."""
    connects, lost_read, sticky, failed = 0, False, False, False
    connect, getter = device.connect, device.get_module_meas_range
    setter, state, purge = device.set_module_meas_range, device.get_state, device.purge

    def open_series(**kwargs):
        nonlocal connects, lost_read
        connects += 1
        lost_read = False
        return connect(**kwargs)

    def read_range(address, **kwargs):
        nonlocal lost_read
        result = getter(address, **kwargs)
        if address == 2 and not lost_read:
            lost_read = True
            return -10, 0, False
        return result

    def write_range(address, value, **kwargs):
        nonlocal sticky, failed
        result = setter(address, value, **kwargs)
        if connects == 2 and address == 3 and value == 0 and not failed:
            sticky = failed = True
            return -12
        return result

    def read_state(**kwargs):
        result = state(**kwargs)
        return (-13, *result[1:]) if sticky else result

    def resynchronize():
        nonlocal sticky
        result = purge()
        sticky = False
        return result

    device.connect, device.get_module_meas_range = open_series, read_range
    device.set_module_meas_range, device.get_state, device.purge = write_range, read_state, resynchronize


@pytest.mark.parametrize('scenario', ['overnight', 'overnight_receive_error',
    'overnight_range_setup_error', 'overnight_matching_ranges', 'twelve_short_runs'])
def test_full_campaign_timing_and_disconnected_pauses(ns, campaign, scenario):
    device, cfg, output, run = campaign
    cfg.update(ns['PROTOCOL'])
    if scenario == 'twelve_short_runs':
        cfg.update(series_count=12, duration_s=1200, pause_s=60)
    recovered = scenario == 'overnight_receive_error'
    setup_recovered = scenario == 'overnight_range_setup_error'
    if scenario in ('overnight_range_setup_error', 'overnight_matching_ranges'):
        inject_reconnect_range_failure(device)
    if setup_recovered:
        device.original[3] = device.ranges[3] = (4, False)
    if scenario == 'overnight_matching_ranges':
        device.original = dict.fromkeys(range(8), (0, False))
        device.ranges = dict(device.original)
    if recovered:
        from test_dmmr_transient_reads import inject_stream_fault
        inject_stream_fault(device, sticky=True, after_s=4 * 3600 + 43 * 60)
    count, duration, pause_s = cfg['series_count'], cfg['duration_s'], cfg['pause_s']
    minutes = int(duration / 60)
    total = count * duration + (count - 1) * pause_s
    device.frame_interval = .1  # Accelerated wall clock, >1,000 points/module/series.
    report = run()
    assert report['status'] == 'complete', report['error']
    assert report['completed_series'] == report['recorded_series'] == count
    assert report['planned_acquisition_s'] == count * duration
    assert report['planned_disconnected_pause_s'] == (count - 1) * pause_s
    assert total <= report['elapsed_s'] < total + 4
    assert report['link_released']
    assert len(report['pauses']) == count - 1
    for pause in report['pauses']:
        assert pause['port_closed'] and pause['complete']
        assert pause['finished_elapsed_s'] - pause['started_elapsed_s'] == pytest.approx(pause_s)
    connects = [i for i, call in enumerate(device.calls) if call == ('connect',)]
    shutdowns = [i for i, call in enumerate(device.calls) if call == ('disconnect',)]
    assert len(connects) == len(shutdowns) == count
    assert all(shutdowns[i] < connects[i + 1] for i in range(count - 1))
    assert device.calls.count(('automatic', True)) == count + int(recovered)
    if recovered:
        incident, = report['series'][0]['read_recoveries']
        assert incident['resumed'] and incident['purged']
        assert incident['start_elapsed_s'] == pytest.approx(4 * 3600 + 43 * 60, abs=1)
        assert not report['series'][1]['read_recoveries']
    assert_restored_ranges(device)
    for entry in report['series']:
        directory = output / entry['directory']
        raw = pd.read_csv(directory / 'raw.csv')
        selected = raw.loc[raw.selected == 1]
        assert set(selected.actual_range) == {0}
        assert set(selected.product_no) == set(ns['CERTIFICATE'])
        assert (selected.groupby('product_no').size() > 1000).all()
        assert 0 < selected.host_elapsed_s.min() < 1
        assert selected.host_elapsed_s.max() < duration
        assert entry['cleanup']['ranges_restored'] and entry['cleanup']['shutdown_confirmed']
        child = json.loads((directory / 'report.json').read_text())
        assert child['telemetry']['cycles_completed'] == minutes
        if setup_recovered and entry['number'] == 2:
            incident, = child['range_recoveries']
            assert incident['status'] == -12 and incident['verified'] and incident['purged']
        else:
            assert not child['range_recoveries']
    if setup_recovered:
        assert device.calls.count(('range', 3, 0)) == count  # Once per series, never replayed.
        assert device.calls.count(('purge',)) == 1
    if scenario == 'overnight_matching_ranges':
        assert not any(c[0] in ('range', 'autorange', 'purge') for c in device.calls)
    for left, right in zip(report['series'], report['series'][1:]):
        assert right['stream_start_elapsed_s'] - left['stream_end_elapsed_s'] >= pause_s
    summary, telemetry, analysed = ns['analyse_campaign'](output)
    assert len(summary) == 8 * count
    assert len(analysed['time_bins']) == 8 * count * minutes
    assert len(analysed['tail_summary']) == 8 * count
    expected_windows = count * 8 * int(duration // 1800) - 8 * int(recovered)
    assert len(analysed['baseline_windows']) == expected_windows
    windows = pd.read_csv(output / 'baseline_windows.csv', float_precision='round_trip')
    assert len(windows) == expected_windows
    if expected_windows:
        np.testing.assert_allclose(windows.end_min - windows.start_min, 30)
        for entry in report['series']:
            rows = windows.loc[windows.series == entry['number']]
            assert (rows.start_min >= entry['stream_start_elapsed_s'] / 60).all()
            assert (rows.end_min <= entry['stream_end_elapsed_s'] / 60).all()
    assert summary.N.sum() == sum(e['selected_points'] for e in analysed['series'])
    assert telemetry.status.eq(0).all() and telemetry.error.isna().all()
    assert set(telemetry.scope) == {'controller', 'module', 'base'}
    for field, expected in [('temp_cpu_c', 30), ('temp_cpui_c', 32), ('volt_36vn_v', -36)]:
        rows = telemetry.loc[(telemetry.scope == 'module') & (telemetry.field == field)]
        assert len(rows) == count * minutes * 8
        np.testing.assert_allclose(pd.to_numeric(rows.value), expected)
    assert len(pd.read_csv(output / 'series_summary.csv')) == count * 8


@pytest.mark.parametrize('fault', ['interrupt', 'poisoned', 'wrong_fixed_range', 'parser'])
def test_failure_of_third_series_never_connects_a_fourth(campaign, fault):
    device, cfg, output, run = campaign
    cfg['series_count'] = 12
    connect = device.connect
    attempts = []

    def connect_and_fail(**kwargs):
        attempts.append(1)
        if len(attempts) == 3:
            device.fault = fault
        return connect(**kwargs)

    device.connect = connect_and_fail
    report = run()
    assert report['status'] == ('interrupted' if fault == 'interrupt' else 'failed')
    assert report['completed_series'] == 2
    assert len(report['series']) == 3 and len(report['pauses']) == 2
    assert len(attempts) == 3
    assert not (output / 'series_04').exists()
    assert report['link_released'] == (fault != 'poisoned')
    if fault == 'poisoned':
        assert device.calls[-1] == ('frame',)


def test_unconfirmed_shutdown_never_reconnects(campaign):
    device, _, _, run = campaign
    device.shutdown_fails = True
    report = run()
    assert report['status'] == 'failed'
    assert report['completed_series'] == 0
    assert len(report['series']) == 1 and not report['pauses']
    assert not report['link_released']
    assert device.calls.count(('connect',)) == 1
    assert device.calls.count(('disconnect',)) == 0
    assert ('enable', False) in device.calls
    assert ('get_enable',) in device.calls


def test_interrupt_during_disconnected_pause_preserves_completed_run(ns, campaign):
    device, _, _, run = campaign
    sleep = device.clock.sleep

    def interrupt_pause(seconds):
        if not device.connected and seconds == 1:
            raise KeyboardInterrupt()
        sleep(seconds)

    device.clock.sleep = interrupt_pause
    report = run()
    assert report['status'] == 'interrupted'
    assert report['completed_series'] == 1 and len(report['series']) == 1
    assert len(report['pauses']) == 1 and not report['pauses'][0]['complete']
    assert report['link_released']
    assert_restored_ranges(device)
    assert device.calls.count(('disconnect',)) == 1


@pytest.mark.parametrize('key,value', [('series_count', 0), ('series_count', True),
    ('series_count', 1.5), ('pause_s', -1), ('pause_s', math.nan),
    ('diagnostics_interval_s', 0), ('diagnostics_interval_s', True)])
def test_invalid_campaign_cannot_open_the_port(ns, campaign, key, value):
    device, cfg, output, _ = campaign
    cfg[key] = value
    with pytest.raises(ValueError):
        ns['run_campaign'](device, output, cfg)
    assert not device.calls
    assert not output.exists()


def test_windows_sleep_guard_restores_previous_thread_state(ns):
    calls = []

    def set_state(flags):
        calls.append(flags)
        return 0x80000003  # System AND display had already been requested.

    with pytest.raises(KeyboardInterrupt):
        with ns['prevent_sleep'](set_state) as state:
            assert state['active'] and not state['manual_sleep_prevented']
            raise KeyboardInterrupt()
    assert calls == [0x80000001, 0x80000003]


def test_refused_sleep_inhibition_stops_before_any_connection(ns, campaign):
    device, _, output, run = campaign
    original = ns['prevent_sleep']
    ns['prevent_sleep'] = lambda: original(lambda _: 0)
    report = run()
    assert report['status'] == 'failed' and 'sleep' in report['error']
    assert not device.calls and not report['series']
    assert (output / 'report.json').is_file()


def test_sleep_guard_is_released_on_acquisition_failure(ns, campaign):
    device, _, _, run = campaign
    active = []

    @contextlib.contextmanager
    def guard():
        active.append('acquire')
        try:
            yield {'active': True}
        finally:
            active.append('release')

    ns['prevent_sleep'] = guard
    device.fault = 'parser'
    assert run()['status'] == 'failed'
    assert active == ['acquire', 'release']


@pytest.mark.parametrize('fault', ['status', 'nan', 'poisoned', 'interrupt'])
def test_diagnostic_failure_is_not_a_zero_reading(ns, campaign, fault):
    device, _, output, run = campaign
    device.diagnostic_fault = fault
    report = run()
    telemetry = pd.read_csv(output / 'series_01/telemetry.csv')
    failed = telemetry.loc[telemetry.error.notna()]
    assert len(failed) > 0
    assert failed.value.isna().all()
    if fault in ('poisoned', 'interrupt'):
        assert report['status'] == ('interrupted' if fault == 'interrupt' else 'failed')
        assert len(report['series']) == 1 and not report['pauses']
        if fault == 'poisoned':
            assert device.calls[-1][0] == 'diagnostic'
            assert not report['link_released']
    else:
        assert report['status'] == 'complete'  # Diagnostic gaps are explicitly logged.
        first = json.loads((output / 'series_01/report.json').read_text())
        assert first['telemetry']['failed_calls' if fault == 'status' else 'invalid_fields'] > 0


def test_diagnostic_reply_error_requires_a_healthy_controller(campaign):
    device, _, _, run = campaign
    device.diagnostic_fault = 'status'
    getter = device.get_state

    def get_state(**kwargs):
        if any(call[0] == 'diagnostic' for call in device.calls):
            return -12, '0x0000', 'ST_ON'
        return getter(**kwargs)

    device.get_state = get_state
    report = run()
    assert report['status'] == 'failed' and len(report['series']) == 1
    assert not report['pauses']


def test_real_base_getters_use_one_transport_lock_and_exact_ctypes_fields(ns):
    """Real DMMRBase + runtime timeout guard, only the vendor DLL is simulated."""
    cls = ns['load_driver'](ROOT / 'dmmr')._PROCESS_CONTROLLER_CLASS
    backend = cls.__new__(cls)
    backend.thread_lock = threading.Lock()
    backend._transport_poisoned = False
    backend.NO_ERR = 0
    calls = []

    class DLL:
        def __getattr__(self, name):
            assert name.startswith('COM_DMMR_8_Get')

            def getter(*args):
                calls.append(name)
                pointers = [arg._obj for arg in args if hasattr(arg, '_obj')]
                for index, pointer in enumerate(pointers):
                    pointer.value = 100. + index if type(pointer).__name__ == 'c_double' else 0
                return 0
            return getter

    backend.dll = DLL()
    # These public methods take the lock again; the recorder must bypass them.
    def nested_lock_forbidden(**kwargs):
        raise AssertionError('Public locked wrapper was called inside transport lock')
    backend.get_device_state = nested_lock_forbidden
    backend.get_voltage_state = nested_lock_forbidden
    backend.get_temperature_state = nested_lock_forbidden
    clock, stream, report = Clock(), io.StringIO(), {}
    modules = {0: {'product_no': 132310}}
    recorder = ns['DiagnosticRecorder'](backend, modules, ns['PROTOCOL'], stream, report, 0, clock)
    for _ in recorder.commands:
        recorder.poll_one()
    data = pd.read_csv(io.StringIO(stream.getvalue()))
    assert report['telemetry']['cycles_completed'] == 1
    assert not report['telemetry']['failed_calls']
    assert len(calls) == len(recorder.commands)
    module = data.loc[(data.scope == 'module') & (data.command == 'get_module_housekeeping')]
    assert list(module.field) == ns['MODULE_HK_FIELDS']
    np.testing.assert_allclose(pd.to_numeric(module.value), np.arange(100, 116))
    assert backend.thread_lock.acquire(blocking=False), 'Lock not released'
    backend.thread_lock.release()


def test_interleaved_diagnostics_keep_buffered_current_frames(ns, campaign):
    device, cfg, output, run = campaign
    cfg.update(series_count=1, duration_s=10, tail_s=2, diagnostics_interval_s=3)
    device.diagnostic_delay = .03
    pending = collections.deque()
    last_conversion = 0
    locked_call = device._call_locked_with_timeout

    def guarded_call(method, timeout, label, *args):
        if label.startswith('zero_check_'):
            assert not pending, 'Diagnostic started before the current FIFO drained'
        return locked_call(method, timeout, label, *args)

    device._call_locked_with_timeout = guarded_call

    def get_current(**kwargs):
        nonlocal last_conversion
        device.note('frame')
        device.clock.sleep(.001)
        if device.automatic:
            due = int(device.clock() / .1)
            for index in range(last_conversion + 1, due + 1):
                for address in device.modules:
                    pending.append((0, address, (100 + index + address) * 1e-15, 0, index * .1))
            last_conversion = due
        return pending.popleft() if pending else (1, 0, 0., 0, 0.)

    device.get_current = get_current
    report = run()
    assert report['status'] == 'complete', report['error']
    raw = pd.read_csv(output / 'series_01/raw.csv')
    valid = raw.loc[raw.selected == 1]
    for _, group in valid.groupby('address'):
        assert len(group) > 90
        np.testing.assert_allclose(np.diff(group.device_time_s), .1, atol=1e-12)
    child = json.loads((output / 'series_01/report.json').read_text())
    assert child['telemetry']['cycles_completed'] >= 2
    assert (valid.host_elapsed_s - valid.device_time_s).max() > .02  # Real simulated buffering.


@pytest.mark.parametrize('pn_order', [
    (132307, 132310, 132308, 132306, 132304, 132305, 132303, 132309),
    tuple(range(132303, 132311)),
], ids=['recorded_40min_order', 'proposed_reordered_modules'])
def test_plot_titles_use_recorded_addresses_not_pn_order(ns, campaign, pn_order):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    device, cfg, output, run = campaign
    cfg['series_count'] = 1
    for address, pn in enumerate(pn_order):
        device.modules[address]['product_no'] = pn
    assert run()['status'] == 'complete'
    _, data, report = ns['analyse_campaign'](output)
    raw = pd.read_csv(output / 'series_01/raw.csv')
    recorded = raw.loc[raw.selected == 1, ['product_no', 'address']].drop_duplicates()
    addresses = dict(recorded.itertuples(index=False, name=None))
    assert addresses == {pn: address for address, pn in enumerate(pn_order)}
    figures = ns['plot_campaign'](report, data)
    try:
        expected = [f'P/N {pn} · DMMR address {addresses[pn]}' for pn in sorted(addresses)]
        for name in ['current_vs_time', 'module_temperatures']:
            assert [ax.get_title() for ax in figures[name].axes] == expected
            figures[name].canvas.draw()
    finally:
        for figure in figures.values():
            plt.close(figure)


@pytest.mark.parametrize('addresses,label', [
    ([None, None, None], 'address unknown'),
    ([6, 0, 0], 'addresses 0, 6'),
])
def test_plot_titles_do_not_invent_a_unique_address(ns, campaign, addresses, label):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    _, _, output, run = campaign
    assert run()['status'] == 'complete'
    _, data, report = ns['analyse_campaign'](output)
    for row in report['summary']:
        if row['P/N'] == 132303:
            row['address'] = addresses[row['series'] - 1]
    figures = ns['plot_campaign'](report, data)
    try:
        for name in ['current_vs_time', 'module_temperatures']:
            assert figures[name].axes[0].get_title() == f'P/N 132303 · DMMR {label}'
    finally:
        for figure in figures.values():
            plt.close(figure)


def test_plots_preserve_diagnostic_gaps_and_series_boundaries(ns, campaign):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    _, _, output, run = campaign
    assert run()['status'] == 'complete'
    _, data, report = ns['analyse_campaign'](output)
    data['error'] = data['error'].astype(object)
    target = data.index[(data.field == 'temp_cpu_c') & (data.product_no == 132310)]
    data.loc[target[1], ['status', 'value', 'error']] = [-10, np.nan, 'status -10']
    figures = ns['plot_campaign'](report, data)
    assert len(figures) == 3
    ax = next(ax for ax in figures['module_temperatures'].axes
              if ax.get_title() == 'P/N 132310 · DMMR address 0')
    assert any(np.isnan(line.get_ydata()).any() for line in ax.lines)
    assert len(ax.patches) == 2  # Three independent series, two actual gaps.
    for name, figure in figures.items():
        figure.savefig(output / (name + '.png'))
        assert 'Elapsed time (h, host clock)' in figure._supxlabel.get_text()
        plt.close(figure)
    # All missing readings must not look like zero Celsius.
    data.loc[data.scope == 'module', ['status', 'value', 'error']] = [-10, np.nan, 'status -10']
    figures = ns['plot_campaign'](report, data)
    assert all(not ax.lines for ax in figures['module_temperatures'].axes)
    assert all('No valid temperature readings' in [t.get_text() for t in ax.texts]
               for ax in figures['module_temperatures'].axes)
    for figure in figures.values():
        plt.close(figure)
