"""Baseline windows use raw samples and stay within each continuous run."""
from __future__ import annotations

import io
import json
import shutil

import numpy as np
import pandas as pd
import pytest

from test_dmmr_zero_notebook import Clock, DMMR, ns, rig  # noqa: F401


def test_diagnostic_schedule_has_no_accumulated_delay_or_catchup_burst(ns):
    clock = Clock()
    device = DMMR(clock, ns['CERTIFICATE'])
    report = {}
    recorder = ns['DiagnosticRecorder'](device, device.modules, ns['PROTOCOL'],
                                         io.StringIO(), report, 0., clock)
    recorder.commands = [('controller', None, 'get_state', ['state_hex', 'state_name'])]
    for minute in range(360):
        clock.now = minute*60 + .1 + (minute % 5)*.1
        recorder.poll_one()
        clock.now += .1
        recorder.poll_one()
    assert report['telemetry']['cycles_completed'] == 360
    assert recorder.next_cycle == 21600
    # Restart the virtual schedule and skip two slots: no queued catch-up calls.
    recorder.next_cycle = 0.
    before = report['telemetry']['cycles_completed']
    for t in (.2, 125., 125.1, 179.9, 180.2):
        clock.now = t
        recorder.poll_one()
    assert report['telemetry']['cycles_completed'] - before == 3


def synthetic_run(ns, rig, *, elapsed=3660., complete=False, offset_fA=0., legacy=False):
    _, _, output, run = rig
    _, _, _, report = run()
    report['protocol'].update(duration_s=3600. if complete else 21600., bin_s=60., tail_s=300.)
    if legacy:
        report['protocol'].pop('baseline_window_s', None)
    report['acquisition'].update(duration_s=elapsed, complete=complete)
    report['status'] = 'complete' if complete else 'failed'
    # Irregular per-minute counts: the mean must not be an unweighted mean of bins.
    times = np.arange(1., elapsed, 37.)
    rows = []
    for address, info in report['modules'].items():
        for t in times:
            rows.append(dict(address=int(address), product_no=info['product_no'],
                             actual_range=0, host_elapsed_s=t, device_time_s=10000 + 1.002*t,
                             current_A=(400 + offset_fA + int(address)*20 + 3*1.002*t/60)*1e-15,
                             status=0, selected=1, reason='selected'))
    # This large, explicitly excluded sample must not enter any statistic.
    rows.append(dict(rows[0], host_elapsed_s=600., current_A=1., selected=0, reason='repeated_timestamp'))
    frame = pd.DataFrame(rows)
    frame.to_csv(output / 'raw.csv', index=False)
    ns['save_report'](output, report)
    return output, frame


@pytest.mark.parametrize('legacy', [False, True])
def test_raw_means_device_clock_slopes_and_unfinished_window(ns, rig, legacy):
    output, original = synthetic_run(ns, rig, legacy=legacy)
    before = (output / 'raw.csv').read_bytes()
    _, _, report = ns['analyse_run'](output)
    assert report['status'] == 'failed' and not report['acquisition']['complete']
    assert (output / 'raw.csv').read_bytes() == before
    windows = pd.DataFrame(report['baseline_windows'])
    assert len(windows) == 16  # Two complete windows per module; omit minute 61.
    assert set(windows.start_s) == {0., 1800.}
    assert set(windows.end_s) == {1800., 3600.}
    np.testing.assert_allclose(windows.drift_fA_min, 3., atol=1e-9)
    for row in windows.itertuples(index=False):
        selected = original.loc[(original.address == row.address) & (original.selected == 1)
                                & (original.host_elapsed_s >= row.start_s)
                                & (original.host_elapsed_s < row.end_s)]
        assert row.N == len(selected)
        assert row.mean_pA == pytest.approx(selected.current_A.mean()*1e12, abs=1e-12)
        assert row.std_fA == pytest.approx(selected.current_A.std(ddof=1)*1e15, abs=1e-9)
    assert not report['tail_summary']  # No final-window claim for an incomplete run.


def test_no_baseline_window_is_invented_for_short_old_runs(ns, rig):
    _, _, _, report = rig[-1]()
    assert not report['baseline_windows']
    assert len(report['summary']) == 8 and len(report['time_bins']) == 32


def test_campaign_keeps_restart_step_and_five_minute_gap(ns, rig, tmp_path):
    source, frame = synthetic_run(ns, rig, elapsed=3600., complete=True)
    campaign = tmp_path / 'two_series'
    campaign.mkdir()
    child = json.loads((source / 'report.json').read_text())
    entries = []
    for number, origin in [(1, 0.), (2, 3900.)]:
        target = campaign / f'series_{number:02d}'
        shutil.copytree(source, target)
        if number == 2:
            shifted = frame.copy()
            shifted['current_A'] += 1e-12
            shifted.to_csv(target / 'raw.csv', index=False)
        entries.append(dict(number=number, directory=target.name, status='complete',
                            started_elapsed_s=origin, stream_start_elapsed_s=origin,
                            stream_end_elapsed_s=origin+3600., cleanup=child['cleanup']))
    ns['save_report'](campaign, dict(kind='campaign', protocol=child['protocol'], status='complete',
                                    elapsed_s=7500., completed_series=2, series=entries))
    _, _, report = ns['analyse_campaign'](campaign)
    csv = pd.read_csv(campaign / 'baseline_windows.csv')
    assert len(report['baseline_windows']) == len(csv) == 32
    assert set(csv.start_min) == {0., 30., 65., 95.}
    assert set(csv.end_min) == {30., 60., 95., 125.}
    np.testing.assert_allclose(csv.drift_fA_min, 3., atol=1e-9)
    first = csv.loc[csv.series == 1].sort_values(['P/N', 'start_min'])
    second = csv.loc[csv.series == 2].sort_values(['P/N', 'start_min'])
    np.testing.assert_allclose(second.mean_pA.to_numpy()-first.mean_pA.to_numpy(), 1., atol=1e-12)


@pytest.mark.parametrize('value', [0., -1., float('nan'), float('inf'), True])
def test_invalid_window_never_opens_the_port(ns, rig, value):
    device, cfg, output, _ = rig
    cfg['baseline_window_s'] = value
    with pytest.raises(ValueError, match='baseline_window_s'):
        ns['run_zero_check'](device, output, cfg)
    assert not device.calls and not output.exists()
