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
from types import SimpleNamespace

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


@pytest.mark.parametrize('name', ['q2_full.toml', 'q2_P1_2h.toml', 'q2_cycles_6h.toml', 'q2_frequency_15h.toml'])
def test_plan_templates_are_valid_and_ready_for_a_nominal_instrument(engine, name):
    plan = RUNNER.load_plan(PLANS / name)
    check = engine('map_check', plan, INSTRUMENT)
    assert check['problems'] == [], check['problems']
    assert check['estimate']['outcome'] in ('done', 'stop')
    hours = check['estimate']['total_s'] / 3600
    expected = {'q2_full.toml': (35, 110), 'q2_P1_2h.toml': (3, 4.5), 'q2_cycles_6h.toml': (6.5, 9),
                'q2_frequency_15h.toml': (11, 16)}[name]
    assert expected[0] < hours < expected[1], f'{name}: {hours:.1f} h simulated'


def test_full_plan_template_lists_every_key_with_the_engine_default(engine):
    plan = RUNNER.load_plan(PLANS / 'q2_full.toml')
    normalized = engine('map_check', {}, INSTRUMENT)['plan']
    assert normalized == engine('map_check', plan, INSTRUMENT)['plan'], 'q2_full.toml differs from the defaults'
    assert set(plan['map']) == set(normalized['map']), 'every key of the engine appears in the template'


def test_the_full_plan_asks_the_operator_for_ilim_330_ma_and_mscan_never_writes_it(engine):
    """The RF current follows from f and A; Ilim is the operator's safeguard, set by hand (2026-11-12)."""
    plan = RUNNER.load_plan(PLANS / 'q2_full.toml')
    lab = dict(INSTRUMENT, ilim_a=0.299998, current_per_hz_v=1.47e-9)
    refused = engine('map_check', plan, lab)
    assert any('set Ilim to at least 330 mA on the PSU yourself' in p for p in refused['problems']), refused['problems']
    assert engine('map_check', plan, dict(lab, ilim_a=0.33))['problems'] == []
    for path in (ROOT / 'mscan/mscan_plugin.py', *sorted((ROOT / 'mscan/_runtime').glob('*.py'))):
        text = path.read_text(encoding='utf-8')
        assert 'current_limit_values' not in text and "'current_limit'" not in text, f'{path.name} writes Ilim'


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
        # The AMX HD oscillator: whole periods of its 100 MHz clock (300 kHz gives 300.300 kHz).
        self.frequency = 100e6 / round(100e6 / float(hz))

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
                               sigma=[2e-15] * len(axis), spray=[self._map_spray_now()] * len(axis),
                               file=f'line{request["id"]}.h5')
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
    folder = run_map(runner, PLANS / 'q2_P1_2h.toml')
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
    # The figures draw the stopping cut and the beam against the spray from map.h5 (2026-11-04: six
    # empty panels after a check map stopped in its ladder). Drawn here again so a fault is not hidden.
    stopping = RUNNER.stored_lines(folder / RUNNER.STORE, 'stopping')
    done_stopping = [r for r in journal['records'] if r['request']['role'] == 'stopping' and r['outcome'] == 'done']
    assert len(stopping) == len(done_stopping) >= 4
    assert all(len(axis) == len(signal) for _, axis, signal, _, _ in stopping)
    assert len(RUNNER.stored_lines(folder / RUNNER.STORE)) == outcomes.count('done')
    RUNNER.write_figures(report, tmp_path / 'figures.png', store=folder / RUNNER.STORE)
    assert (tmp_path / 'figures.png').stat().st_size > 50_000 and (folder / RUNNER.FIGURES).is_file()
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
    folder = runner.start(PLANS / 'q2_P1_2h.toml')
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
    folder = runner.start(PLANS / 'q2_P1_2h.toml')
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


def test_a_map_stopped_by_its_spray_window_leaves_no_window_to_the_next_one(engine, tmp_path):
    """2026-11-12 lab: a map stopped when the spray fell below its window; then every Start map, whatever its
    plan, waited for a spray within that map's window. The runner, shared by the maps of a session, kept that
    map's spray reference (and its start values to restore)."""
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    runner.SPRAY_WAIT_S = 0.
    spray = [45e-9]
    scan._map_spray_now = lambda: spray[0]
    original = scan._start_map_line

    def spray_falls_after_the_first_line():
        started = original()
        spray[0] = 10e-9
        return started
    scan._start_map_line = spray_falls_after_the_first_line
    first = runner.start(PLANS / 'q2_P1_2h.toml')
    runner.thread.join(300)
    status = json.loads((first / RUNNER.STATUS).read_text())
    assert status['state'] == 'stopped' and 'Spray current outside its window' in status['message'], status
    scan._start_map_line = original
    scan.channels['Q1_DC'].value = 38.  # Set by the operator before the next map.
    done = len(scan.lines_run)
    second = runner.start(PLANS / 'q2_full.toml')  # Another plan; the spray is steady at 10 nA.
    deadline = time.monotonic() + 120
    while len(scan.lines_run) < done + 2 and runner.running and time.monotonic() < deadline:
        time.sleep(.02)
    runner.stop()
    runner.thread.join(300)
    status = json.loads((second / RUNNER.STATUS).read_text())
    assert len(scan.lines_run) >= done + 2 and 'Spray' not in status['message'], status
    assert runner.spray_reference == 10e-9 and runner._failures == []
    assert scan.channels['Q1_DC'].value == 38.  # This map's start value, not the 36 V of the first map.


def test_a_line_that_cannot_start_stops_the_map_at_once_without_counting_it(engine, tmp_path):
    """2026-11-12 lab: PSU_A went OFF after a lost reply; the map then failed two more lines in 4 s on
    "Cannot start: PSU_A_CH0: no valid PSU voltage readback" and stopped on "3 failed lines". A refused
    start is tried again for a while, then stops the map with what to fix; it is not a failed line."""
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    runner.START_RETRY_S, runner.START_RETRY_EVERY_S = .4, .05
    original = scan._start_map_line
    attempts = []

    def psu_off_after_two_lines():
        if len(scan.lines_run) < 2:
            return original()
        attempts.append(time.monotonic())
        scan.scan_status = 'Cannot start: PSU_A_CH0: no valid PSU voltage readback.'
        return False
    scan._start_map_line = psu_off_after_two_lines
    folder = runner.start(PLANS / 'q2_P1_2h.toml')
    runner.thread.join(120)
    status = json.loads((folder / RUNNER.STATUS).read_text())
    assert status['state'] == 'stopped', status
    assert 'the next line cannot start: PSU_A_CH0: no valid PSU voltage readback. Fix it' in status['message']
    assert 'Resume' in status['message']
    records = json.loads((folder / RUNNER.JOURNAL).read_text())['records']
    assert len(records) == 2 and all(r['outcome'] == 'done' for r in records)
    assert len(attempts) >= 3, 'tried again before stopping'
    assert scan.channels['Q1_DC'].value == 36., 'start values restored'


def test_a_fixed_spray_range_of_the_plan_replaces_the_window():
    """User 2026-11-12: a fixed ESI current range in nA, as in Transmission, whatever the spray at Start."""
    scan, said = FakeMScan(), []
    scan.print = lambda text, **kw: said.append(text)
    runner = RUNNER.MapRunner(scan, None, data_path='.')
    runner.spray_reference = 80e-9  # Outside the range: it no longer matters.
    runner.journal = dict(plan=dict(map=dict(spray_window_percent=40, spray_range_na=[30, 60])), records=[])
    assert runner._spray_window() == pytest.approx((30e-9, 60e-9))
    scan._map_spray_now = lambda: -45e-9
    runner._wait_for_spray()
    assert said[-1] == 'Map: spray window 30.0–60.0 nA (fixed range of the plan).'
    with pytest.raises(RUNNER.LineFailed, match='25.0 nA left its window 30.0–60.0 nA'):
        runner._check_spray_during([-25e-9] * 5)


def test_a_resumed_map_centres_the_spray_window_on_the_current_it_finds(tmp_path):
    """2026-11-12 lab: a Resume compared the spray with a reference line of the earlier run (36 nA) and waited
    for a current the ESI no longer gave; the window is that run's, until a reference line of this one."""
    scan, said = FakeMScan(), []
    scan.print = lambda text, **kw: said.append(text)
    scan._map_spray_now = lambda: -60e-9
    runner = RUNNER.MapRunner(scan, None, data_path=tmp_path)
    runner.folder = tmp_path

    def reference(spray, line):
        return dict(request=dict(role='reference', id=line), outcome='done',
                    analysis=dict(spray_mean=spray, peaks=[dict(mz=922.01, found=True)]))
    plan = json.loads(json.dumps(dict(map=dict(spray_window_percent=40, channels=dict(q1_dc='Q1_DC', stub_entry='Stub_Entry_Q2'),
                                                follow={}))))
    runner.journal = dict(plan=plan, records=[reference(-36e-9, 1)])
    runner._capture_start_values()  # Resume: the ESI now gives 60 nA.
    assert runner._spray_window() == pytest.approx((36e-9, 84e-9))
    runner._wait_for_spray()  # Not blocked.
    assert said[-1] == 'Map: spray window 36.0–84.0 nA (±40 % of 60.0 nA, ESI current at Resume).'
    runner.journal['records'].append(reference(-50e-9, 7))  # This run's first reference line.
    assert runner._spray_window() == pytest.approx((30e-9, 70e-9)) and runner.spray_source == 'reference line 7'
    with pytest.raises(RUNNER.LineFailed, match=r'20.0 nA left its window 30.0–70.0 nA'):
        runner._check_spray_during([-20e-9] * 5)


def test_resume_plans_with_the_ilim_the_operator_set_on_the_psu_since_start(engine, tmp_path):
    """Ilim is the operator's safeguard, set by hand (user, 2026-11-12): Resume follows the PSU's Ilim, the
    other readings stay those of Start."""
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    original = scan._start_map_line

    def stop_at_two():
        started = original()
        if len(scan.lines_run) == 2:
            threading.Thread(target=runner.stop).start()
            time.sleep(.3)
        return started
    scan._start_map_line = stop_at_two
    folder = runner.start(PLANS / 'q2_P1_2h.toml')
    runner.thread.join(300)
    assert runner.notice == ''
    frozen = json.loads((folder / RUNNER.JOURNAL).read_text())['instrument']
    scan._start_map_line = original
    live = dict(INSTRUMENT, q1_dc_v=37., f_hz=500e3, ilim_a=.33)
    scan._map_instrument = lambda config, data_path: dict(live)
    resumed = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    resumed.start(resume=folder)
    resumed.stop()
    resumed.thread.join(300)
    instrument = json.loads((folder / RUNNER.JOURNAL).read_text())['instrument']
    assert instrument['ilim_a'] == .33 and instrument['q1_dc_v'] == frozen['q1_dc_v'] == 36.
    assert '330 mA (500 mA at Start)' in resumed.notice and 'skipped' in resumed.notice
    assert resumed.notice in (folder / RUNNER.LOG).read_text(encoding='utf-8')
    again = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    again.start(resume=folder)  # Unchanged since the last Resume: nothing to say.
    again.stop()
    again.thread.join(300)
    assert again.notice == ''


def test_unreachable_point_closes_and_the_map_goes_on(engine, tmp_path):
    scan = FakeMScan(unreachable_points={'P1'})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_cycles_6h.toml')
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    p1 = [r for r in journal['records'] if r['request']['point'] == 'P1']
    assert [r['outcome'] for r in p1] == ['unreachable'], 'P1 closes after its first line'
    assert any(r['request']['point'] == 'P2' and r['outcome'] == 'done' for r in journal['records'])
    assert json.loads((folder / RUNNER.STATUS).read_text())['state'] == 'done'


def test_a_design_frequency_the_amx_cannot_make_runs_at_the_nearest_one(engine, tmp_path):
    """2026-11-13 lab: q2_cycles_6h asked for 300 kHz; the AMX makes whole 10 ns periods, 300.300 kHz, and
    the map stopped on "Settings before the line not reached: AMX at 300.300 kHz for 300.000 kHz"."""
    scan = FakeMScan()
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_cycles_6h.toml')
    assert json.loads((folder / RUNNER.STATUS).read_text())['state'] == 'done'
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    p3 = [r for r in journal['records'] if r['request']['point'] == 'P3']
    assert p3 and all(r['outcome'] == 'done' for r in p3)
    assert {r['request']['f_hz'] for r in p3} == {100e6 / 333}
    assert scan.frequency == 500e3, 'back to the frequency at Start'


def test_resume_gives_a_line_that_failed_three_times_new_attempts(engine, tmp_path):
    """2026-11-13 lab: one line failed three times and the map stopped; both Resumes then stopped at once
    on "line BP3-ladder failed 3 times". A Resume is the operator asking to try again."""
    scan = FakeMScan(fail_lines={2, 3, 4})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_P1_2h.toml')
    status = json.loads((folder / RUNNER.STATUS).read_text())
    assert status['state'] == 'stopped' and 'within one hour' in status['message'], status
    resumed = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    resumed.start(resume=folder)
    resumed.thread.join(600)
    assert json.loads((folder / RUNNER.STATUS).read_text())['state'] == 'done'
    journal = json.loads((folder / RUNNER.JOURNAL).read_text())
    assert journal['session_start'] == 4
    first = journal['records'][1:5]
    assert [r['outcome'] for r in first] == ['failed', 'failed', 'failed', 'done']
    assert len({r['request']['label'] for r in first}) == 1, 'the same line, measured after the Resume'


def test_failed_lines_within_an_hour_stop_the_map(engine, tmp_path):
    scan = FakeMScan(fail_lines={2, 4, 6})
    runner = RUNNER.MapRunner(scan, engine, data_path=tmp_path)
    folder = run_map(runner, PLANS / 'q2_P1_2h.toml')
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
    scan._line, scan._line_map = map_line(), dict(name='q2-full', detector='Detector')
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
    assert settings['adaptive']['noise_floor'] == 5e-14  # Noise between points of a flat M03 tail (2026-11-04).
    scan._line = map_line('frequency', amplitude_v=110., axis=[335e3, 340e3], adaptive=False)
    settings = adapter._line_settings()
    assert settings['axis'] == 'frequency' and settings['amplitude'] == 110. and 'adaptive' not in settings
    scan._line = None
    assert adapter._line_settings() == {}


def test_check_text_lists_problems_warnings_and_the_estimate(mscan_module):
    text = mscan_module.MScan._map_check_text(dict(problems=['raise Ilim to 450 mA'], warnings=['F1: coarse steps'],
                                                   estimate=dict(lines=210, total_s=36000.)))
    assert 'not started; change this first' in text and '- raise Ilim' in text and '! F1' in text and '10.0 h' in text


class _Action:
    def __init__(self):
        self.state, self.toolTip, self.toolTipFalse = False, '', ''

    def setToolTip(self, text):
        self.toolTip = text


def test_the_plan_is_chosen_from_the_loaded_one_and_named_in_the_map_tooltip(mscan_module, monkeypatch, tmp_path):
    """Lab 2026-11-04: after a map, the same plan ran again and another plan could not be loaded."""
    first, second = tmp_path / 'first.toml', tmp_path / 'second.toml'
    first.write_text('', encoding='utf-8')
    second.write_text('', encoding='utf-8')
    scan = mscan_module.MScan.__new__(mscan_module.MScan)
    scan._map_runner, scan.map_plan, printed = None, '', []
    scan.print = lambda text, **kw: printed.append(text)
    scan.mapAction = _Action()
    asked, answers = [], [str(first), str(second), '', str(first)]

    def ask(parent, title, directory, filters):
        asked.append(directory)
        return answers.pop(0), filters
    monkeypatch.setattr(mscan_module, 'QFileDialog', type('Dialog', (), dict(getOpenFileName=staticmethod(ask))))
    scan._map_plan_changed()
    assert 'No plan loaded' in scan.mapAction.toolTip
    assert scan._map_load_plan() == first and Path(asked[0]) == ROOT / 'mscan' / 'plans'  # The templates.
    assert 'first.toml' in scan.mapAction.toolTip
    assert scan._map_load_plan() == second and Path(asked[1]) == first  # Opened on the loaded plan.
    assert scan.map_plan == str(second) and printed[-1].startswith('Map plan loaded: second.toml')
    scan.mapAction.state, scan.mapAction.toolTip = True, 'stop'  # A map runs: its Stop tooltip stays.
    scan._map_runner = type('Runner', (), dict(running=True))()
    assert scan._map_load_plan() is None and scan.map_plan == str(second)  # Cancelled: nothing changes.
    assert scan._map_load_plan() == first and scan.mapAction.toolTip == 'stop'
    assert 'first.toml' in scan.mapAction.toolTipFalse and 'for the next map' in printed[-1]
    first.unlink()  # A loaded plan whose file is gone is not used.
    assert scan._map_loaded_plan() is None


def test_the_map_dialog_proposes_the_newest_map_that_did_not_complete(mscan_module, tmp_path):
    scan = mscan_module.MScan.__new__(mscan_module.MScan)
    scan.pluginManager = SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path))
    maps = tmp_path / 'MScan' / 'maps'

    def folder(name, state, lines):
        path = maps / name
        path.mkdir(parents=True)
        (path / 'journal.json').write_text(json.dumps(dict(records=[{}] * lines)), encoding='utf-8')
        (path / 'status.json').write_text(json.dumps(dict(state=state, time=time.mktime((2026, 11, 12, 13, 40, 0, 0, 0, -1)))),
                                          encoding='utf-8')
        return path
    assert scan._map_last_unfinished() is None
    stopped = folder('2026-11-12_13-10-00_q2-P1-2h', 'stopped', 5)
    assert scan._map_last_unfinished() == (stopped, 'stopped at 13:40 after 5 lines')
    folder('2026-11-12_14-00-00_q2-P1-2h', 'done', 16)
    assert scan._map_last_unfinished() is None  # The newest map completed: nothing proposed.


def _map_scan(mscan_module, monkeypatch, runner, choice):
    scan = mscan_module.MScan.__new__(mscan_module.MScan)
    scan.map_plan, scan._finished, scan._map_runner, scan.printed = '', True, runner, []
    scan._queue_completion = lambda action: action()
    scan.print = lambda text, **kw: scan.printed.append(text)
    scan.mapAction, scan.map_status = _Action(), ''
    scan._map_choice = lambda: choice
    monkeypatch.setattr(mscan_module, 'PRINT', type('PRINT', (), dict(MESSAGE='message', WARNING='warning')))
    return scan


def _wait_for(condition, seconds=5):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(.01)
    return condition()


def test_map_checks_a_new_plan_and_starts_it_only_if_nothing_must_change(mscan_module, monkeypatch, tmp_path):
    """User 2026-11-12: one Map… button; the check runs before every new map instead of being a button."""
    plan, stopped = tmp_path / 'plan.toml', tmp_path / 'maps' / 'stopped'
    plan.write_text('', encoding='utf-8')
    calls, problems = [], [['set Ilim to at least 330 mA on the PSU yourself']]

    class Runner:
        running, notice = False, ''

        def check(self, path):
            calls.append(('check', path))
            return dict(problems=problems[0], warnings=[], estimate=dict(lines=16, total_s=7560.))

        def start(self, path, resume=None):
            calls.append(('start', path, resume))
            return tmp_path / 'maps' / 'new'
    refused = _map_scan(mscan_module, monkeypatch, Runner(), ('new', plan))
    refused._map_action()
    assert _wait_for(lambda: not refused._map_starting)
    assert calls == [('check', plan)] and refused.mapAction.state is False
    assert 'not started' in refused.map_status and 'set Ilim' in refused.printed[-1]
    problems[0] = []
    started = _map_scan(mscan_module, monkeypatch, Runner(), ('new', plan))
    started._map_action()
    assert _wait_for(lambda: len(calls) == 3)
    assert calls[1:] == [('check', plan), ('start', plan, None)] and started.mapAction.state is True
    assert any('16 lines, about 2.1 h' in text for text in started.printed)
    resumed = _map_scan(mscan_module, monkeypatch, Runner(), ('resume', stopped))
    resumed._map_action()
    assert _wait_for(lambda: len(calls) == 4)
    assert calls[3] == ('start', None, stopped)  # A resumed map keeps its own checked plan.
    cancelled = _map_scan(mscan_module, monkeypatch, Runner(), None)
    cancelled.mapAction.state = True  # The click toggled the button.
    cancelled._map_action()
    assert cancelled.mapAction.state is False and len(calls) == 4


def test_start_scan_is_locked_while_the_map_starts(mscan_module, monkeypatch, tmp_path):
    """2026-11-11 lab: Start map waited 30 s for the first launch of its worker; Start scan was clicked
    meanwhile and the map stopped at its first engine call ("A scan is running")."""
    plan = tmp_path / 'plan.toml'
    plan.write_text('', encoding='utf-8')
    entered, release, stopped = threading.Event(), threading.Event(), []

    class Runner:
        running = False

        def start(self, path, resume=None):
            entered.set()
            assert release.wait(5)  # map_begin: the worker's first launch
            Runner.running = True
            return tmp_path / 'map'

        def stop(self):
            stopped.append(True)
    scan = _map_scan(mscan_module, monkeypatch, Runner(), ('resume', tmp_path / 'map'))
    scan._recording, scan._line, scan._cancel = False, None, threading.Event()
    scan._map_action()
    assert entered.wait(5) and scan._map_owns_scans()
    scan.recording = True  # Start scan clicked during the start
    scan.toggleRecording()
    assert scan.recording is False and 'starts its own lines' in scan.printed[-1]
    scan._map_action()  # Map… clicked again during the start: stop once started
    release.set()
    deadline = time.monotonic() + 5
    while scan._map_starting and time.monotonic() < deadline:
        time.sleep(.01)
    assert not scan._map_starting and Runner.running and stopped == [True]
    assert scan._map_owns_scans()  # the running map keeps the lock
    scan._line = {'id': 1}  # its own line starts like the Start button
    monkeypatch.setattr(type(scan).__mro__[1], 'toggleRecording', lambda self: setattr(self, 'started', True),
                        raising=False)
    scan.recording, scan._finished, scan.settingsTree = True, True, None
    scan.toggleRecording()
    assert getattr(scan, 'started', False)


class _LineScan:
    """A running line for the runner's guard: a point every ``every`` polls, finished after ``points``."""

    def __init__(self, points, every=1):
        self.points, self.every, self.polls, self.finished, self.stops = points, every, 0, False, 0
        self._validation = dict(window_end=np.full(min(max(points, 1), 1000), np.nan))

    def _gui(self, action, cancel=True):
        return action()

    def _start_map_line(self):
        return True

    def _stop_map_line(self):
        self.stops += 1
        self.finished = True

    def _map_line_result(self):
        done = int(np.isfinite(self._validation['window_end']).sum())
        return dict(status='completed' if done >= self.points else 'stopped', error='')

    def tick(self):
        self.polls += 1
        done = int(np.isfinite(self._validation['window_end']).sum())
        if self.every and self.polls % self.every == 0 and done < min(self.points, 1000):
            self._validation['window_end'][done] = float(self.polls)
        self.finished = self.finished or done >= self.points


def _guard_runner(scan, monkeypatch, **plan):
    runner = RUNNER.MapRunner(scan, None, data_path='.')
    runner.journal = dict(plan=dict(map=dict(dict(point_s=.8, settle_s=.3), **plan)))
    runner.POLL_S, runner.LINE_MARGIN_S = .001, 0.
    monkeypatch.setattr(RUNNER.time, 'sleep', lambda s: scan.tick())
    return runner


def test_a_line_slower_than_its_estimate_is_not_stopped(monkeypatch):
    """2026-11-04: three good P1 lines were stopped at twice an estimate that counted 1.1 s per point."""
    scan = _LineScan(points=40, every=3)
    runner = _guard_runner(scan, monkeypatch)
    data = runner._run_scan(dict(expected_s=1e-6, axis=list(range(40))))
    assert data['status'] == 'completed' and data['error'] == '' and scan.stops == 0


def test_a_line_without_new_points_is_stopped_as_hung(monkeypatch):
    scan = _LineScan(points=40, every=0)
    runner = _guard_runner(scan, monkeypatch)
    runner.STALL_S = .05
    data = runner._run_scan(dict(expected_s=1e3, axis=list(range(40))))
    assert scan.stops >= 1 and data['error'].startswith('no new point for')


def test_a_line_longer_than_every_point_of_its_axis_is_stopped(monkeypatch):
    scan = _LineScan(points=10 ** 9, every=1)
    runner = _guard_runner(scan, monkeypatch, point_s=0., settle_s=0.)
    runner.POINT_OVERHEAD_S = .01  # 3 points: 30 ms at most.
    data = runner._run_scan(dict(expected_s=0., axis=[1., 2., 3.]))
    assert scan.stops >= 1 and data['error'].startswith('line longer than')


def test_a_final_map_status_says_when_and_after_how_many_lines(mscan_module, monkeypatch):
    NS = SimpleNamespace
    """2026-11-11 lab: "stopped: ScanError ..." of the 14:35 map, still shown while a scan of the operator
    ran at 15:50, was read as that run failing."""
    scan = mscan_module.MScan.__new__(mscan_module.MScan)
    scan.settingsMgr = NS(settings={scan.MAP_STATUS: None})
    scan.mapAction = _Action()
    printed = []
    scan.print = lambda text, **kw: printed.append(text)
    monkeypatch.setattr(mscan_module, 'PRINT', NS(MESSAGE='message', WARNING='warning'))
    stopped = time.mktime((2026, 11, 11, 14, 35, 35, 0, 0, -1))
    scan._show_map_status(dict(state='stopped', message='ScanError: A scan is running.', time=stopped, lines=0))
    assert scan.map_status == 'stopped at 14:35 after 0 lines: ScanError: A scan is running.'
    assert printed[-1] == 'Map stopped at 14:35 after 0 lines: ScanError: A scan is running.'
    scan._show_map_status(dict(state='running', message='Line 3: B ladder P1', time=stopped))
    assert scan.map_status == 'running: Line 3: B ladder P1'


def test_the_spray_window_follows_the_last_reference_that_found_its_peak():
    """2026-11-04 lab: the ESI current went 42 -> 48 -> 36 nA while the M03 peaks held within 2 %; a window
    around the value at Start would have stopped a long map for a spray that still gave the same beam."""
    runner = RUNNER.MapRunner(FakeMScan(), None, data_path='.')
    runner.spray_reference = 42e-9

    def reference(spray, found=True, outcome='done'):
        return dict(request=dict(role='reference'), outcome=outcome,
                    analysis=dict(spray_mean=spray, peaks=[dict(mz=922.01, found=found)]))
    ladder = dict(request=dict(role='ladder'), outcome='done', analysis=dict(spray_mean=30e-9, peaks=[]))
    runner.journal = dict(plan=dict(map=dict(spray_window_percent=40)), records=[ladder])
    assert runner._spray_window() == pytest.approx((25.2e-9, 58.8e-9))  # before any reference: Start
    runner.journal['records'] += [reference(-36e-9), ladder]
    assert runner._spray_window() == pytest.approx((21.6e-9, 50.4e-9))  # the last verified beam
    runner._check_spray_during([-23e-9] * 5)  # inside ±40 % of 36 nA, outside ±40 % of the 42 nA at Start
    runner.journal['records'] += [reference(-20e-9, found=False), reference(-15e-9, outcome='failed')]
    assert runner._spray_window() == pytest.approx((21.6e-9, 50.4e-9)), 'only a reference that saw the beam'
    with pytest.raises(RUNNER.LineFailed):
        runner._check_spray_during([-15e-9] * 5)
