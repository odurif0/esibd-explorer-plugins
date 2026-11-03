"""MScan map (v0.5.10): plan templates, and the runner against the real map engine and a simulated Q2.

The engine is the MScan Rust worker built for this test (``--features mscan``); the instrument is a
fake MScan whose lines return spectra of the Agilent tuning mix computed with MScan's own m/z
conversion. No Explorer, no hardware.
"""
from __future__ import annotations

import importlib.util
import json
import math
from pathlib import Path
import shutil
import subprocess
import threading
import time
import tomllib

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
PLANS = ROOT / 'mscan' / 'plans'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUNNER = _load('mscan_map_runner_test', ROOT / 'mscan/_runtime/_map_runner.py')
TRANSPORT = _load('mscan_map_transport_test', ROOT / 'native/python/_native_worker.py')


@pytest.fixture(scope='module')
def engine(tmp_path_factory):
    if shutil.which('cargo') is None:
        pytest.skip('The map engine tests need the local Rust toolchain')
    target = ROOT / 'native/target/map-tests'
    subprocess.run(['cargo', 'build', '--offline', '--release', '--manifest-path', str(ROOT / 'native/Cargo.toml'),
                    '--target-dir', str(target), '--no-default-features', '--features', 'mscan'],
                   check=True, capture_output=True, timeout=900)
    binary = target / 'release' / 'esibd-native-worker'
    worker = TRANSPORT.NativeWorkerProxy(ROOT / 'mscan', 'mscan', {}, log_dir=tmp_path_factory.mktemp('logs'),
                                         command=[str(binary), '--family', 'mscan'], stop_grace_s=.05)
    calls = []

    def call(method, *args):
        calls.append(method)
        return worker.call_method(method, *args, rpc_timeout_s=600.)
    call.calls = calls
    yield call
    worker.close(grace_s=0)


INSTRUMENT = dict(q1_dc_v=36.0, ap_q1_q2_v=34.0, x_v=10.0, ilim_a=0.5, psu_max_v=350.0, full_range=True,
                  pair_min_v=-200.0, pair_max_v=200.0, stub_min_v=-200.0, stub_max_v=200.0, q1_max_v=200.0,
                  clock_hz=100e6, f_hz=500e3, frequency_writable=True, free_disk_bytes=1e12)


@pytest.mark.parametrize('name', ['q2_map.toml', 'q2_check_1h.toml', 'q2_check_4h.toml'])
def test_plan_templates_are_valid_and_ready_for_a_nominal_instrument(engine, name):
    plan = RUNNER.load_plan(PLANS / name)
    check = engine('map_check', plan, INSTRUMENT)
    assert check['problems'] == [], check['problems']
    assert check['estimate']['outcome'] in ('done', 'stop')
    hours = check['estimate']['total_s'] / 3600
    expected = {'q2_map.toml': (15, 70), 'q2_check_1h.toml': (.3, 2.5), 'q2_check_4h.toml': (1.5, 8)}[name]
    assert expected[0] < hours < expected[1], f'{name}: {hours:.1f} h simulated'


def test_full_plan_template_lists_every_key_with_the_engine_default(engine):
    plan = RUNNER.load_plan(PLANS / 'q2_map.toml')
    normalized = engine('map_check', {}, INSTRUMENT)['plan']
    assert normalized == engine('map_check', plan, INSTRUMENT)['plan'], 'q2_map.toml differs from the defaults'
    assert set(plan['map']) == set(normalized['map']), 'every key of the engine appears in the template'


class Channel:
    def __init__(self, name, value):
        self.name, self.value, self.monitor = name, value, value
        self.writes = []

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if key == 'value':
            super().__setattr__('monitor', value)  # An AMPR that reaches every target at once.
            if hasattr(self, 'writes'):
                self.writes.append(value)

    def getDevice(self):
        return type('Device', (), {'controller': None, 'name': 'AMPR_A'})()


class FakeMScan:
    """The MScan methods the runner calls; each line returns a spectrum of the tuning mix at once."""

    FOLDER = 'MScan'
    voltage_tolerance = .5

    def __init__(self, fail_lines=(), unreachable_points=()):
        self.channels = {name: Channel(name, value) for name, value in
                         (('Q1_DC', 36.), ('Ap_Q1_Q2', 34.), ('Stub_Entry_Q2', 20.), ('Stub_Exit_Q2', -5.))}
        self.frequency = 500e3
        self.finished = True
        self.line = None
        self.result = None
        self.fail_lines, self.unreachable_points = set(fail_lines), set(unreachable_points)
        self.lines_run = []
        self.status_seen = []
        self.settings = dict(offset_factor=.2, offset_resolution=0., offset_fixed=10.)

    def _gui(self, action, cancel=True):
        return action()

    def _map_instrument(self, config, data_path):
        return dict(INSTRUMENT, q1_dc_v=self.channels['Q1_DC'].value, f_hz=self.frequency)

    def _map_channel(self, name):
        return self.channels[name]

    @staticmethod
    def _offset_bounds(channel, controller):
        return -200., 200.

    def _map_frequency_hz(self):
        return self.frequency

    def _set_map_frequency(self, hz):
        self.frequency = float(hz)

    def _map_settings_snapshot(self):
        return dict(self.settings)

    def _map_settings_restore(self, snapshot):
        self.settings = dict(snapshot)

    def _map_spray_now(self):
        return 45e-9

    def _configure_map_line(self, request, config):
        self.line = request
        self.settings.update(offset_factor=request['k'], offset_resolution=request['delta_v'], offset_fixed=request['x_v'])

    def _start_map_line(self):
        request = self.line
        self.lines_run.append(request)
        axis = np.asarray(request['axis'], dtype=float)
        if request['id'] in self.fail_lines:
            self.fail_lines.discard(request['id'])
            self.result = dict(status='error', error='DMMR: no new valid sample (simulated)', axis=list(axis),
                               signal=[math.nan] * len(axis), sigma=[math.nan] * len(axis), spray=[], file='')
        elif request['point'] in self.unreachable_points:
            self.result = dict(status='error', error='PSU_A_CH0: no regulator headroom (simulated)', axis=list(axis),
                               signal=[math.nan] * len(axis), sigma=[math.nan] * len(axis), spray=[], file='')
        else:
            signal = spectrum(request, self.channels['Q1_DC'].value)
            self.result = dict(status='completed', error='', axis=list(axis), signal=list(signal),
                               sigma=[2e-15] * len(axis), spray=[45e-9] * len(axis), file=f'line{request["id"]}.h5')
        self.finished = True
        return True

    def _stop_map_line(self):
        self.finished = True

    def _end_map_line(self):
        self.line = None

    def _map_line_result(self):
        return dict(self.result)

    def _show_map_status(self, status):
        self.status_seen.append(status['state'])

    def _save_map_best_settings(self, best, path):
        Path(path).write_text(json.dumps(best))


_MSCAN = None


def _mz(a, request, attenuation=.978):
    global _MSCAN
    if _MSCAN is None:
        import os
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        pytest.importorskip('PyQt6.QtCore')
        pytest.importorskip('esibd')
        _MSCAN = _load('mscan_map_physics_test', ROOT / 'mscan/mscan_plugin.py')
    return _MSCAN.mz_from_amplitude(a, frequency_hz=request['f_hz'], r0_mm=2.625, factor=request['k'],
                                    resolution=request['delta_v'], minimum=.5, pairs=2, attenuation=attenuation)


MIX = {322.05: .6, 622.03: 1., 922.01: .9, 1521.97: .7, 2121.93: .5, 2721.89: .3}


def spectrum(request, q1):
    """Currents (A) of the tuning mix along the line, for an ideal filter capped at R = 45 (H1)."""
    axis = np.asarray(request['axis'], dtype=float)
    rng = np.random.default_rng(int(request['id']))
    noise = 2e-15 * rng.standard_normal(len(axis))
    if request['kind'] == 'offset':
        a = request['amplitude_v']
        height = 1e-12 * .5 * np.array([math.erfc((x - (q1 - .3)) / .85) for x in axis])
        return height - 3e-13 + noise
    if request['kind'] == 'extra':
        optimum = request['x_v'] + .42 * (34 - request['x_v'])
        return 1e-12 * np.clip(1 - .1 * ((axis - optimum) / 5) ** 2, .3, 1) - 3e-13 + noise
    mz = _mz(axis, request)
    k_eff = (request['k'] * axis - request['delta_v']) / (.978 * axis)
    ideal = np.where(k_eff < .2153, .157 / np.maximum(.2153 - k_eff, 1e-9), np.inf)
    resolution = 1 / (1 / ideal + 1 / 45)
    transmission = np.clip((.2153 - k_eff) / (.2153 - .2 / .978), 0, 3)
    y = np.full(len(axis), -3e-13)
    for m, abundance in MIX.items():
        width = m / resolution
        y += np.nan_to_num(2e-12 * abundance * transmission * np.exp(-4 * math.log(2) * ((mz - m) / width) ** 2))
    return y + noise


def run_map(runner, plan, timeout=600):
    folder = runner.start(plan)
    runner.thread.join(timeout)
    assert not runner.running, 'the map did not finish'
    return folder


def test_runner_completes_the_one_hour_plan_and_restores_the_start_values(engine, tmp_path):
    scan = FakeMScan(fail_lines={3})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_check_1h.toml')
    status = json.loads((folder / RUNNER.STATUS).read_text())
    assert status['state'] == 'done', status
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    outcomes = [r['outcome'] for r in journal['records']]
    assert 'failed' in outcomes and outcomes.count('done') >= 8
    failed = next(r for r in journal['records'] if r['outcome'] == 'failed')
    assert any(r['request']['variant'] == failed['request']['variant'] and r['request']['role'] == failed['request']['role']
               and r['outcome'] == 'done' for r in journal['records']), 'a failed line is measured again'
    roles = {r['request']['role'] for r in journal['records']}
    assert {'reference', 'stopping', 'ladder'} <= roles
    for name in (RUNNER.REPORT, RUNNER.STORE, RUNNER.PLAN_COPY, RUNNER.LOG):
        assert (folder / name).is_file(), name
    report = json.loads((folder / RUNNER.REPORT).read_text())
    assert abs(report['stopping']['x_stop'] - 35.7) < .4
    # Back to Start: the channels the lines set and the AMX frequency.
    assert scan.channels['Q1_DC'].value == 36. and scan.channels['Stub_Entry_Q2'].value == 20.
    assert scan.channels['Stub_Exit_Q2'].value == -5. and scan.frequency == 500e3
    assert scan.settings == dict(offset_factor=.2, offset_resolution=0., offset_fixed=10.)
    assert 38. in scan.channels['Q1_DC'].writes, 'the stopping cut repeats with Q1_DC + 2 V'
    assert scan.status_seen[-1] == 'done'


def test_safe_stop_then_resume_continues_from_the_journal(engine, tmp_path):
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    original = scan._start_map_line
    stop_after = 4

    def counting():
        started = original()
        if len(scan.lines_run) == stop_after:
            threading.Thread(target=runner.stop).start()
            time.sleep(.3)
        return started
    scan._start_map_line = counting
    folder = runner.start(PLANS / 'q2_check_1h.toml')
    runner.thread.join(300)
    status = json.loads((folder / RUNNER.STATUS).read_text())
    assert status['state'] == 'stopped' and 'operator' in status['message']
    done = len(json.loads((folder / RUNNER.JOURNAL).read_text())['records'])
    assert stop_after - 1 <= done <= stop_after
    assert scan.channels['Q1_DC'].value == 36. and scan.frequency == 500e3
    scan._start_map_line = original
    resumed = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    resumed.start(resume=folder)
    resumed.thread.join(600)
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    assert json.loads((folder / RUNNER.STATUS).read_text())['state'] == 'done'
    assert len(journal['records']) > done
    ids = [r['request']['id'] for r in journal['records']]
    assert ids == list(range(1, len(ids) + 1)), 'Resume continues the same numbering'


def test_resume_after_a_crash_restores_the_values_from_before_the_map(engine, tmp_path):
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    original = scan._start_map_line

    def crash():
        started = original()
        if len(scan.lines_run) == 3:
            threading.Thread(target=runner.stop).start()
            time.sleep(.3)
        return started
    scan._start_map_line = crash
    folder = runner.start(PLANS / 'q2_check_1h.toml')
    runner.thread.join(300)
    stored = json.loads((folder / RUNNER.START_VALUES).read_text())
    assert stored['channels']['Q1_DC'] == 36. and stored['channels']['__frequency_khz__'] == 500.
    # Explorer crashed instead: the channels hold what the last line set.
    scan._start_map_line = original
    scan.channels['Q1_DC'].value, scan.channels['Stub_Entry_Q2'].value, scan.frequency = 38., 28.8, 480e3
    resumed = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    resumed.start(resume=folder)
    resumed.thread.join(600)
    assert scan.channels['Q1_DC'].value == 36. and scan.channels['Stub_Entry_Q2'].value == 20.
    assert scan.frequency == 500e3


def test_unreachable_point_closes_and_the_map_goes_on(engine, tmp_path):
    scan = FakeMScan(unreachable_points={'P1'})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_check_4h.toml')
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    p1 = [r for r in journal['records'] if r['request']['point'] == 'P1']
    assert [r['outcome'] for r in p1] == ['unreachable'], 'P1 closes after its first line'
    assert any(r['request']['point'] == 'P2' and r['outcome'] == 'done' for r in journal['records'])
    assert json.loads((folder / RUNNER.STATUS).read_text())['state'] == 'done'


def test_failed_lines_within_an_hour_stop_the_map(engine, tmp_path):
    scan = FakeMScan(fail_lines={2, 4, 6})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_check_1h.toml')
    status = json.loads((folder / RUNNER.STATUS).read_text())
    assert status['state'] == 'stopped' and 'within one hour' in status['message']
    assert scan.channels['Q1_DC'].value == 36.


def test_atomic_journal_keeps_the_previous_version(tmp_path):
    path = tmp_path / 'journal.json'
    RUNNER.atomic_json(path, {'a': 1})
    RUNNER.atomic_json(path, {'a': 2})
    assert json.loads(path.read_text()) == {'a': 2}
    assert json.loads((tmp_path / 'journal.json.bak').read_text()) == {'a': 1}
    with pytest.raises(ValueError):
        RUNNER.atomic_json(path, {'a': math.nan})
    assert json.loads(path.read_text()) == {'a': 2}, 'a refused write leaves the journal intact'


def test_plan_files_parse_with_tomllib():
    for path in PLANS.glob('*.toml'):
        with path.open('rb') as handle:
            assert isinstance(tomllib.load(handle)['map'], dict)


# ---------------------------------------------------------------- MScan's hooks for map lines

from support.mscan import OffsetChannel, add_offset  # noqa: E402  (fixtures of the shared MScan rig)


def map_line(kind='amplitude', **extra):
    request = dict(id=7, stage='B', role='ladder', point='P1', variant='', kind=kind, f_hz=500e3, k=.2093,
                   delta_v=.02, energy_ev=26., x_v=10., q1_dc_v=36., stub_v=20., amplitude_v=0.,
                   axis=[20., 22.5, 25.], adaptive=kind == 'amplitude', coarse=4, ions=[322.05], expected_s=60.,
                   seconds=0., predicted=0., label='B-P1-ladder')
    request.update(extra)
    return request


def test_amplitude_line_replaces_the_steps_and_records_its_request(mscan_rig):
    scan = mscan_rig.scan
    add_offset(mscan_rig)
    scan._line, scan._line_map = map_line(), dict(name='q2-map', detector='Detector')
    assert list(scan._scan_steps()) == [20., 22.5, 25.]
    plan = scan._prepare()
    assert plan['requested_range'] == (20., 25.)
    assert plan['metadata']['map_line']['id'] == 7
    assert scan._line_axis() == ('Amplitude', 'V') and scan._line_span() == '20-25V'
    assert scan._map_signal_index([mscan_rig.detector]) == 0
    with pytest.raises(mscan_rig.module.ScanError, match='not a recorded DMMR module'):
        scan._line_map = dict(detector='M07')
        scan._map_signal_index([mscan_rig.detector])


def test_extra_line_appends_the_entry_stub_after_the_rod_pairs(mscan_rig):
    scan = mscan_rig.scan
    add_offset(mscan_rig)
    stub = OffsetChannel(mscan_rig.offset.device, module=4, number=2, name='Stub_Entry_Q2', value=20.)
    mscan_rig.all_channels.append(stub)
    scan._line = map_line('extra', amplitude_v=89.8, axis=[18., 19., 20.], adaptive=False)
    scan._line_map = dict(channels=dict(stub_entry='Stub_Entry_Q2'))
    plan = scan._prepare()
    assert [o['name'] for o in plan['offsets']] == ['Q_offset', 'Stub_Entry_Q2']
    assert plan['requested_range'] == (89.8, 89.8)
    assert scan._line_axis() == ('Stub_Entry_Q2', 'V') and scan._line_span() == 'stub18-20V'
    stub.max = 19.
    with pytest.raises(mscan_rig.module.ScanError, match='exceeds allowed'):
        scan._prepare()


def test_offset_line_checks_the_rod_pairs_at_every_x(mscan_rig):
    scan = mscan_rig.scan
    channel = add_offset(mscan_rig)
    scan._line = map_line('offset', amplitude_v=89.8, axis=[25., 35., 45.], adaptive=False)
    scan.offset_fixed = 25.
    scan._prepare()
    channel.max = 50.  # 45 V + U = 45 + 0.2093 × 89.8 − 0.02 is above it.
    with pytest.raises(mscan_rig.module.ScanError, match='at x = 45'):
        scan._prepare()


def test_frequency_command_only_on_a_frequency_line(mscan_rig):
    scan = mscan_rig.scan
    with pytest.raises(mscan_rig.module.ScanError, match='frequency command outside a frequency line'):
        scan._command([10., 10.], frequency_hz=450e3)


def test_adapter_sends_the_line_points_axis_and_step(mscan_rig):
    scan = mscan_rig.scan
    adapter = mscan_rig.module._load_native_runtime('_native_scan').NativeScanAdapter(scan, lambda *a: None)
    scan._line = map_line()
    settings = adapter._line_settings()
    assert settings['points'] == [20., 22.5, 25.] and settings['axis'] == 'amplitude'
    assert math.isnan(settings['amplitude']) and settings['adaptive']['coarse'] == 4
    scan._line = map_line('frequency', amplitude_v=110., axis=[335e3, 340e3], adaptive=False)
    settings = adapter._line_settings()
    assert settings['axis'] == 'frequency' and settings['amplitude'] == 110. and 'adaptive' not in settings
    scan._line = None
    assert adapter._line_settings() == {}


def test_check_text_lists_problems_warnings_and_the_estimate(mscan_module):
    text = mscan_module.MScan._map_check_text(dict(problems=['raise Ilim to 450 mA'], warnings=['F1: coarse steps'],
                                                   estimate=dict(lines=210, total_s=36000.)))
    assert 'change this before Start' in text and '- raise Ilim' in text and '! F1' in text and '10.0 h' in text
