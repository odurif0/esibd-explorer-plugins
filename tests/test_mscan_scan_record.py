"""What an MScan file keeps besides the currents (user request 2026-11-03).

"rajouter les données manquantes dans le h5; je pense en effet (entre autres) que le suivi de l'ESI
est important": the spray and the pressures with each point, the DMMR readings behind each mean with
their range, the RF frequency and duty as numbers, the code that ran the scan, and free notes.
"""
from collections import deque
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest

from support.mscan import History


def context_device(r, name, channels, acquiring=True):
    """A plugin as MScan sees it: live channels only, no Explorer history (it need not be recording)."""
    device = NS(name=name)
    r.scan.pluginManager.plugins.append(device)
    made = []
    for channel, unit, reading in channels:
        c = NS(name=channel, unit=unit, real=True, enabled=True, acquiring=acquiring, useMonitors=name == 'ESI',
               value=np.nan, monitor=np.nan, getDevice=lambda d=device: d, reading=reading)
        made.append(c)
    r.all_channels.extend(made)
    on_pause = r.on_pause
    def live():
        on_pause()
        for c in made:  # The plugin's own poll: the ESI shows its measurement as monitor, its setpoint as value.
            value = c.reading() if callable(c.reading) else c.reading
            if c.useMonitors:
                c.monitor, c.value = value, 3000.
            else:
                c.value = value
    r.on_pause = live
    return device, made


def with_validation(s):
    n, rails = len(s.inputChannels[0].recordingData), len(s._plan['rails'])
    s._validation.update(rail_dropout=np.full((n, rails), np.nan), low_headroom=np.zeros(n, dtype=bool),
                         context=np.full((n, len(s._plan['context'])), np.nan), raw_samples={})


def scan(r):
    s = r.scan
    s._plan = s._preflight()
    s._plan['detectors'] = [r.detector]
    with_validation(s)
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed', s._validation['error']
    return s._validation


def test_the_spray_and_the_pressures_are_recorded_with_each_point(mscan_rig, tmp_path):
    r = mscan_rig
    # The spray current follows the RF amplitude here, so each point shows its own window.
    _, (current, hv, off) = context_device(r, 'ESI', [('ESI_I', 'A', lambda: 1e-9 * r.current()),
                                                      ('ESI_HV1', 'V', 2500.), ('ESI_HV2', 'V', 0.)])
    off.enabled = False
    virtual = context_device(r, 'ESI', [('ESI_sum', 'A', 0.)])[1][0]
    virtual.real = False
    context_device(r, 'TPG366', [('P_source', 'mbar', 2e-3), ('P_lost', 'mbar', np.nan)])
    context_device(r, 'AMPR', [('Q_lens', 'V', 12.)])  # Not a context device.
    r.on_pause()  # The plugins have polled once before Start.
    v = scan(r)
    assert r.scan._plan['context_warning'] is None
    info = r.scan._plan['metadata']['context']
    assert [(c['device'], c['channel'], c['unit']) for c in info] == [
        ('ESI', 'ESI_HV1', 'V'), ('ESI', 'ESI_I', 'A'), ('TPG366', 'P_lost', 'mbar'), ('TPG366', 'P_source', 'mbar')]
    assert v['context'][:, 1] == pytest.approx([1e-8, 2e-8, 3e-8])
    assert v['context'][:, 0] == pytest.approx([2500.] * 3)  # The measured HV, not its 3000 V setpoint.
    assert v['context'][:, 3] == pytest.approx([2e-3] * 3)
    assert np.isnan(v['context'][:, 2]).all()  # A gauge without a reading stays NaN.
    path = tmp_path / 'scan.h5'
    r.scan.saveData(path)
    with h5py.File(path) as f:
        group = f['MScan/Validation/Context']
        assert sorted(group) == ['ESI', 'TPG366'] and sorted(group['ESI']) == ['ESI_HV1', 'ESI_I']
        assert group['ESI/ESI_I'][:] == pytest.approx([1e-8, 2e-8, 3e-8])
        assert group['ESI/ESI_I'].attrs['Unit'] == 'A' and group['TPG366/P_source'].attrs['Unit'] == 'mbar'
        assert 'Independent of Explorer recording' in group.attrs['Description']
        setup = json.loads(f['MScan/Validation'].attrs['setup'])
        assert [c['channel'] for c in setup['context']] == [c['channel'] for c in info]


def test_a_device_that_is_not_acquiring_is_said_at_start_and_gives_nan(mscan_rig):
    r = mscan_rig
    context_device(r, 'ESI', [('ESI_I', 'A', 4e-8)])
    gauge = context_device(r, 'TPG366', [('P_source', 'mbar', 2e-3)], acquiring=False)[1][0]
    gauge.value = 2e-3  # Its last value is still shown: never recorded while not acquiring.
    r.on_pause()
    assert r.scan._preflight()['context_warning'] == (
        'TPG366 not acquiring: NaN for its readings in the scan file. Turn it on to record them with each point.')
    v = scan(r)
    assert v['context'][:, 0] == pytest.approx([4e-8] * 3) and np.isnan(v['context'][:, 1]).all()


def test_a_window_without_a_poll_takes_a_recent_reading_only(mscan_rig):
    s = mscan_rig.scan
    s._plan = dict(s._plan, context=['ESI_I'])
    s._validation['context'] = np.full((3, 1), np.nan)
    s._context_samples = deque([(100., [1.]), (104., [2.]), (104.5, [np.nan]), (130., [5.])])
    s._store_context(0, 103., 105.)
    s._store_context(1, 110., 114.)  # No poll inside: the 104.5 s reading is NaN, so nothing within 10 s.
    s._store_context(2, 120., 125.)  # 20 s after the last reading before it.
    assert s._validation['context'][0, 0] == 2. and np.isnan(s._validation['context'][1:, 0]).all()
    s._store_context(1, 99., 105.)
    assert s._validation['context'][1, 0] == pytest.approx(1.5)


def test_without_esi_or_gauge_the_file_has_no_context(mscan_rig, tmp_path):
    scan(mscan_rig)
    path = tmp_path / 'scan.h5'
    mscan_rig.scan.saveData(path)
    with h5py.File(path) as f:
        assert 'Context' not in f['MScan/Validation']


def test_every_dmmr_reading_is_kept_with_its_point_and_range(mscan_rig, tmp_path):
    r, s = mscan_rig, mscan_rig.scan
    r.detector.range_history = History()
    r.detector.range_history.data = [2.]  # Aligned with the record the rig starts with.
    pause = s._pause
    def tick():
        pause()
        r.detector.range_history.data.append(3. if r.current() > 20 else 2.)
    s._pause = tick
    v = scan(r)
    raw = v['raw_samples']
    assert sorted(raw) == [0, 1, 2]
    for index, (times, currents, ranges) in raw.items():
        assert currents.shape == ranges.shape == (len(times), 1)
        assert len(times) == v['samples'][index, 0]
        assert np.mean(currents) == pytest.approx(s.outputChannels[0].recordingData[index])
        assert (v['window_start'][index] < times).all() and (times <= v['window_end'][index] + 1e-6).all()
    assert set(raw[2][2][:, 0]) == {3.} and set(raw[0][2][:, 0]) == {2.}
    before = len(raw[1][0])
    s._store_samples(1, v['window_start'][1], v['window_end'][1])  # A point updated again replaces its records.
    assert len(v['raw_samples'][1][0]) == before
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path) as f:
        samples = f['MScan/Validation/Samples']
        total = int(np.sum(v['samples']))
        assert samples['time'].shape == (total,) and samples['current'].shape == samples['range'].shape == (total, 1)
        assert (np.diff(samples['time'][:]) > 0).all()
        assert np.bincount(samples['point'][:]).tolist() == v['samples'][:, 0].tolist()
        for index in range(3):
            mine = samples['point'][:] == index
            assert np.mean(samples['current'][:][mine]) == pytest.approx(s.outputChannels[0].recordingData[index])


def test_a_module_without_range_history_gives_nan_ranges(mscan_rig):
    v = scan(mscan_rig)
    assert all(np.isnan(ranges).all() and np.isfinite(currents).all() for _, currents, ranges in v['raw_samples'].values())


def test_the_file_says_which_code_ran_the_scan_and_the_rf_numbers(mscan_rig, mscan_module, tmp_path):
    s = mscan_rig.scan
    for row in mscan_rig.amx.controller.output_rows:
        row['frequency'] = '500 kHz'
    s.scan_notes = 'Agilent G2421A tuning mix, 1:10 in ACN'
    scan(mscan_rig)
    metadata = s._plan['metadata']
    assert metadata['rf'] == dict(frequency_hz=5e5, duty_percent=[50., 50.])
    software = metadata['software']
    root = Path(mscan_module.__file__).parent
    assert software['plugin_version'] == s.version
    assert software['mscan_plugin_sha256'] == hashlib.sha256((root / 'mscan_plugin.py').read_bytes()).hexdigest()
    assert software['native_scan_sha256'] == hashlib.sha256((root / '_runtime/_native_scan.py').read_bytes()).hexdigest()
    manifest = json.loads((root / 'native/manifest.json').read_text(encoding='utf-8'))['targets']['linux-x86_64']
    assert software['worker'] == dict(file=manifest['file'], sha256=manifest['sha256'])
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path) as f:
        assert f['MScan'].attrs['notes'] == 'Agilent G2421A tuning mix, 1:10 in ACN'
        setup = json.loads(f['MScan/Validation'].attrs['setup'])
        assert setup['notes'] == 'Agilent G2421A tuning mix, 1:10 in ACN'
        assert setup['rf']['frequency_hz'] == 5e5 and setup['software']['worker']['sha256'] == manifest['sha256']


def test_duty_comes_from_the_source_timing_else_from_the_displayed_text(mscan_module):
    duty = mscan_module.MScan._duty_percent
    assert duty([dict(waveform=dict(source=dict(period=200, high=60)))]) == [30.]
    assert duty([dict(timing=dict(period=200, high=100)), dict(duty='≈40')]) == [50., 40.]
    assert duty([dict(duty='Unknown')]) == [None]


def test_unknown_frequency_is_recorded_as_unknown(mscan_rig):
    scan(mscan_rig)  # The rig's rows give no clock and no frequency text.
    assert mscan_rig.scan._plan['metadata']['rf'] == dict(frequency_hz=None, duty_percent=[50., 50.])
