"""Offline tests only: no vendor DLL, COM port or heater is opened."""
from __future__ import annotations

import ast
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from test_esi_heater_activation import rig as real_driver_rig  # simulated vendor backend only

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / 'notebooks/esi_heater_characterization.ipynb'


def source():
    return ''.join(json.loads(NOTEBOOK.read_text())['cells'][1]['source'])


def namespace():
    tree = ast.parse(source())
    assert isinstance(tree.body[-1], ast.Expr)
    tree.body.pop()  # Never execute the hardware-armed main during offline tests.
    ns = {'__file__': str(NOTEBOOK), '__name__': 'heater_characterization_test'}
    exec(compile(tree, str(NOTEBOOK), 'exec'), ns)
    # Direct Experiment tests bypass main(), so bind the same verified pure helper.
    limits = ns['load_pure_helper'](ROOT / 'esi', '_heater_limits.py')
    for name in ('native_code', 'native_value', 'bounded_limit'):
        ns[name] = getattr(limits, name)
    return ns


@pytest.mark.parametrize('damage', ['missing', 'cached_hash'])
def test_pure_limit_import_refuses_missing_or_stale_helper(tmp_path, monkeypatch, damage):
    ns = namespace()
    path = tmp_path / '_heater_limits.py'
    if damage != 'missing':
        path.write_bytes((ROOT / 'esi/_heater_limits.py').read_bytes())
    if damage == 'cached_hash':
        module = ns['load_pure_helper'](tmp_path, path.name)
        assert module.bounded_limit(10.) <= 10.
        assert not module.__name__.startswith('_esibd_bundled_')
        monkeypatch.setattr(module, '_source_sha256', 'wrong')
    with pytest.raises((FileNotFoundError, RuntimeError)):
        ns['load_pure_helper'](tmp_path, path.name)


def test_updated_pure_helper_is_loaded_not_refused(tmp_path):
    # Plugins and notebooks evolve independently: no version pin on helper files.
    ns = namespace()
    source = (ROOT / 'esi/_heater_limits.py').read_text(encoding='utf-8')
    (tmp_path / '_heater_limits.py').write_text(source + '\nUPDATED = True\n', encoding='utf-8')
    module = ns['load_pure_helper'](tmp_path, '_heater_limits.py')
    assert module.UPDATED is True and module.bounded_limit(10.) <= 10.


def test_pure_helper_loader_cannot_load_instrument_runtime():
    ns = namespace()
    with pytest.raises(ValueError, match='Unknown pure helper'):
        ns['load_pure_helper'](ROOT / 'esi', 'vendor/runtime/__init__.py')


_TEST_GUARDS = []


def install_test_guard(ns, tmp_path):
    """Real guard/filesystem with an isolated kernel and per-test state root."""
    import importlib.util
    spec = importlib.util.spec_from_file_location('_isolated_esi_guard', ROOT / 'esi/_experiment_guard.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.sys = ns['sys'] if isinstance(ns['sys'], SimpleNamespace) else SimpleNamespace(modules={})
    original = helper.ExperimentGuard
    class Guard(original):
        def __init__(self, **kwargs):
            kwargs['state_root'] = tmp_path / 'guard-state'
            super().__init__(**kwargs)
            _TEST_GUARDS.append(self)
    helper.ExperimentGuard = Guard
    ns['load_experiment_guard'] = lambda root: helper
    pure_loader = ns['load_pure_helper']
    ns['load_pure_helper'] = lambda root, name: pure_loader(ROOT / 'esi', name)
    return helper


def make_test_guard(ns, tmp_path, output_dir):
    helper = install_test_guard(ns, tmp_path)
    guard = helper.ExperimentGuard(plugin_dir=tmp_path / 'esi', com=16, kind='heater',
                                  output_dir=output_dir, kernel_globals=ns)
    guard.authorize_restart()
    return guard


@pytest.fixture(autouse=True)
def release_test_guard_handles():
    yield
    for guard in _TEST_GUARDS:
        if not guard._released:
            guard._release_unused()  # Test teardown, never a production recovery path.
    _TEST_GUARDS.clear()


def test_notebook_has_one_canonical_location_and_finds_sibling_plugin():
    assert NOTEBOOK.parent == ROOT / 'notebooks'
    assert not (ROOT / 'esi/esi_heater_characterization.ipynb').exists()
    assert namespace()['find_plugin']() == ROOT / 'esi'


@pytest.mark.parametrize('configured', [False, True])
@pytest.mark.parametrize('locked', [False, True])
def test_moved_notebook_preserves_legacy_plugin_guard(tmp_path, configured, locked):
    ns = namespace()
    notebook_dir = tmp_path / 'notebooks'
    notebook_dir.mkdir()
    root = tmp_path / ('external/esi' if configured else 'esi')
    runtime = root / 'vendor/runtime'
    runtime.mkdir(parents=True)
    (runtime / '__init__.py').write_text('# discovery only; never imported')
    ns['PLUGIN_DIR'] = str(root) if configured else None
    directory = root / 'logs/esi_heater_characterization'
    directory.mkdir(parents=True)
    marker = directory / '.heater_characterization.running.json'
    marker.write_bytes(b'{}')  # Incomplete old evidence must remain blocking.
    ns.update(NOTEBOOK_DIR=notebook_dir, OUTPUT_DIR=notebook_dir / 'logs/esi_heater_characterization',
              sys=SimpleNamespace(platform='win32', maxsize=2**63 - 1, modules={}))
    ns['runtime_fingerprints'] = lambda path: {}  # Fake plugin folder, no DLL access.
    ns['load_private'] = lambda *args, **kwargs: pytest.fail('Guard must block before runtime import')
    ns['input'] = lambda *args: pytest.fail('Missing evidence must not prompt')
    helper = install_test_guard(ns, tmp_path)
    lock = helper.RunLock(directory) if locked else None
    try:
        with pytest.raises(RuntimeError, match='Unfinished|owner|locking'):
            ns['main']()
    finally:
        if lock is not None:
            lock.close()
    assert marker.read_bytes() == b'{}'
    assert not ns['OUTPUT_DIR'].exists()


class Clock:
    def __init__(self):
        self.now = 0.

    def __call__(self):
        return self.now

    def sleep(self, duration):
        self.now += duration


class Plots:
    def update(self, rows):
        pass

    def save(self, directory, rows):
        pass


class FakeESI:
    def __init__(self):
        self._transport_poisoned = False
        self.connected = self.active = False
        self.target = 0.
        self.temperature = 21.
        self.limit = {'voltage_limit_v': 0., 'current_limit_a': 0., 'power_limit_w': 0.}
        self.maximum = {'max_voltage_v': 21.999616, 'max_current_a': 12.000256,
                        'max_power_w': 179.999931891712, 'max_temperature_c': 175.}
        self.calls, self.threads = [], []
        self.hooks = {}
        self.saturation_temperature = math.inf
        self.last_diagnostic = None

    def record(self, name, *args):
        assert self._transport_poisoned is False, 'No call after poison/unknown state'
        self.calls.append((name, args))
        self.threads.append(threading.get_ident())
        callback = self.hooks.get(name)
        if callback:
            callback(*args)

    def connect(self):
        self.record('connect')
        self.connected = True
        return True

    def get_heat_configuration(self):
        self.record('get_heat_configuration')
        return dict(self.limit, hardware_limits=dict(self.maximum), target_temperature_c=self.target)

    def configure_heat_limits(self, *, cancel_event=None, **kwargs):
        self.record('configure_heat_limits', kwargs)
        result = {}
        # Independent native arithmetic: nearest code, then echoed physical value.
        for key, value in kwargs.items():
            raw = value * 1e6 / 1024
            if key == 'power_w':
                raw = raw * 1e6 / 1024 / 1024
            raw = int(raw + .5)
            applied = raw / 1e6 * 1024
            if key == 'power_w':
                applied = applied / 1e6 * 1024 * 1024
            name, unit = key.split('_')
            self.limit[f'{name}_limit_{unit}'] = applied
            result[key] = applied
        return result

    def set_heater_temperature(self, value, *, cancel_event=None):
        self.record('set_heater_temperature', value)
        self.target = value
        return value

    def set_output_active(self, address, active, *, cancel_event=None):
        self.record('set_output_active', address, active)
        assert address == 0, 'No HV activation'
        self.active = active
        if not active:
            self.target = 0.
        return active

    def collect_diagnostics(self, *, cancel_event=None):
        self.record('collect_diagnostics')
        if self.active:
            self.temperature = min(self.target, self.temperature + 5, self.saturation_temperature)
        else:
            self.temperature = max(21., self.temperature - .5)
        heat = dict(self.limit, hardware_limits=dict(self.maximum), valid=True,
                    monitor_temperature_c=self.temperature, monitor_output_v=4. if self.active else .03,
                    monitor_voltage_v=3.9 if self.active else .003, monitor_current_a=1. if self.active else .002,
                    output_voltage_v=4. if self.active else -1., heater_power_w=10. if self.active else 0.,
                    target_temperature_c=self.target, module_active=self.active, active=self.active,
                    module_gate_active=self.active, device_gate_active=True, control_active=self.active)
        result = {'main_state': {'name': 'STATE_ON'}, 'device_state': {'hex': '0x0'}, 'heat': heat,
                  'modules': {i: {'module_active': False, 'module_gate_active': False, 'target_v': 0.} for i in (1, 2)}}
        if self.hooks.get('diagnostic'):
            self.hooks['diagnostic'](result)
        self.last_diagnostic = result
        return result

    def disconnect(self, on_discharge):
        self.record('disconnect')
        on_discharge({'state': 'confirmed', 'samples': 3})
        self.active = self.connected = False
        return True


@pytest.fixture
def rig(tmp_path):
    ns = namespace()
    fake, clock = FakeESI(), Clock()
    run = ns['Experiment'](tmp_path / 'runs', lambda _: fake, baseline_s=2, observation_s=2,
                           stage_s=8, cycle_s=2, cooling_s=2, sample_s=1,
                           clock=clock, sleep=clock.sleep, plots_factory=Plots,
                           run_guard=make_test_guard(ns, tmp_path, tmp_path / 'runs'))
    return SimpleNamespace(ns=ns, fake=fake, clock=clock, run=run)


def test_stop_during_limit_write_cancels_remaining_setters(real_driver_rig, monkeypatch, tmp_path):
    ns = namespace()
    run = ns['Experiment'](tmp_path / 'never-opened', lambda _: pytest.fail('No constructor'))
    calls, queued = [], []
    run.esi = real_driver_rig.driver
    run.trace = SimpleNamespace(record=lambda *a, **kw: None)
    run.owner = SimpleNamespace(thread=SimpleNamespace(ident=-1), call=lambda label, fn: fn(),
                                begin_close=lambda cleanup: queued.append(cleanup))
    monkeypatch.setattr(run.esi, '_call_locked_with_timeout', lambda fn, *a, **kw: fn())
    for kind in ('voltage', 'current', 'power'):
        def setter(self, value, kind=kind):
            calls.append((kind, value))
            real_driver_rig.state.limits[kind] = value
            if kind == 'voltage':
                run.request_stop()
            return 0, value
        monkeypatch.setattr(real_driver_rig.base, f'set_heat_ctrl_{kind}_limit', setter)
    with pytest.raises(KeyboardInterrupt, match='cancelled'):
        run.call('configure_heat_limits', voltage_v=22., current_a=10., power_w=50., timeout_s=.1)
    assert run.heat_cancel.is_set() and queued
    assert calls == [('voltage', 22.)]


def trace_rows(directory):
    return [json.loads(line) for line in (directory / 'native_calls.jsonl').read_text().splitlines()]


@pytest.fixture
def trace_rig(tmp_path):
    ns = namespace()
    calls = []
    payload = (0, False, 0., 0., 0., 0.)
    class Base:
        pass
    def method(name):
        def call(self, *args, **kwargs):
            calls.append((self, name, args, kwargs))
            return payload
        return call
    for name in ns['NativeTrace'].METHODS:
        setattr(Base, name, method(name))
    backend = Base()
    trace = ns['NativeTrace'](tmp_path)
    trace.install(Base, backend)
    yield SimpleNamespace(ns=ns, trace=trace, base=Base, backend=backend,
                          calls=calls, payload=payload, directory=tmp_path)
    trace.finish(True)


def test_trace_preserves_result_identity_arguments_and_other_instances(trace_rig):
    r = trace_rig
    for base, name, original, observed in r.trace.hooks:
        assert observed.__name__ == original.__name__
        assert observed.__wrapped__ is original
    assert r.base.get_heat_ctrl_monitoring(r.backend, 2, flag=False) is r.payload
    other = r.base()
    assert other.get_heat_ctrl_monitoring() is r.payload
    assert len(r.calls) == 2
    status = r.trace.finish(True)
    rows = trace_rows(r.directory)
    assert [x['event'] for x in rows] == ['native_start', 'native_return']
    assert rows[0]['args'] == [2] and rows[0]['kwargs'] == {'flag': False}
    assert rows[1]['result'] == list(r.payload)
    assert rows[0]['call_id'] == rows[1]['call_id']
    assert rows[0]['monotonic_ns'] <= rows[1]['monotonic_ns']
    assert rows[0]['utc_ns'] <= rows[1]['utc_ns']
    assert status['errors'] == [] and status['writer_closed']


def test_trace_snapshots_mutable_results_and_nonfinite_values(trace_rig):
    r = trace_rig
    value = {'raw': [False, float('nan'), float('inf'), -float('inf'), -0.0]}
    r.trace.record('test', result=value)
    value['raw'][0] = True
    r.trace.finish(True)
    saved = trace_rows(r.directory)[0]['result']['raw']
    assert saved[0] is False
    assert saved[1:4] == [{'nonfinite_float': v} for v in ('nan', 'inf', '-inf')]
    assert math.copysign(1., saved[-1]) == -1


def test_trace_records_late_return_without_cleanup_or_extra_call(tmp_path):
    ns = namespace()
    entered, release = threading.Event(), threading.Event()
    calls = []
    class Base:
        def monitor(self):
            calls.append('monitor')
            entered.set()
            assert release.wait(2)
            return (0, False, 0., 0., 0., 0.)
    trace = ns['NativeTrace'](tmp_path)
    trace.METHODS = ('monitor',)
    backend = Base()
    trace.install(Base, backend)
    worker = threading.Thread(target=backend.monitor)
    worker.start()
    assert entered.wait(1)
    status = trace.finish(False)  # Python timeout/unknown state: no concurrent instrument cleanup.
    assert not status['writer_closed'] and status['late_return_possible']
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    trace.finish(True)
    assert calls == ['monitor']
    assert [r['event'] for r in trace_rows(tmp_path)] == ['native_start', 'native_return']


@pytest.mark.parametrize('exception', [RuntimeError('native error'), KeyboardInterrupt('native interruption')])
def test_trace_preserves_exact_exception(tmp_path, exception):
    ns = namespace()
    class Base:
        def monitor(self):
            raise exception
    backend = Base()
    trace = ns['NativeTrace'](tmp_path)
    trace.METHODS = ('monitor',)
    trace.install(Base, backend)
    try:
        with pytest.raises(type(exception)) as caught:
            backend.monitor()
        assert caught.value is exception
    finally:
        trace.finish(True)
    row = trace_rows(tmp_path)[-1]
    assert row['event'] == 'native_exception' and row['exception_type'] == type(exception).__name__


def test_logging_failure_never_changes_native_result(trace_rig, monkeypatch):
    r = trace_rig
    monkeypatch.setattr(r.trace, 'encode', lambda value: (_ for _ in ()).throw(OSError('capture unavailable')))
    assert r.backend.get_heat_ctrl_monitoring() is r.payload
    status = r.trace.finish(True)
    assert len(r.calls) == 1 and status['errors']


def test_trace_disk_failure_is_reported_without_changing_native_result(trace_rig):
    r = trace_rig
    original_stream = r.trace.stream
    class FailedDisk:
        def write(self, value):
            raise OSError('disk full')
        def close(self):
            original_stream.close()
    r.trace.stream = FailedDisk()
    assert r.backend.get_heat_ctrl_monitoring() is r.payload
    status = r.trace.finish(True)
    assert len(r.calls) == 1 and any('disk full' in error for error in status['errors'])


def test_blocked_disk_writer_does_not_block_native_call(trace_rig):
    r = trace_rig
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    original_stream = r.trace.stream
    class SlowDisk:
        def write(self, value):
            entered.set()
            assert release.wait(3)
            return original_stream.write(value)
        def flush(self):
            original_stream.flush()
        def close(self):
            original_stream.close()
    r.trace.stream = SlowDisk()
    r.trace.record('test')
    assert entered.wait(1)
    results = []
    def native_call():
        results.append(r.backend.get_heat_ctrl_monitoring())
        returned.set()
    worker = threading.Thread(target=native_call)
    worker.start()
    try:
        assert returned.wait(1), 'Native worker must not wait for trace disk I/O'
        assert results == [r.payload] and len(r.calls) == 1
    finally:
        release.set()
        worker.join(2)
        r.trace.finish(True)


def test_all_discharge_callbacks_are_saved_without_instrument_calls(rig):
    expected = []
    def disconnect(on_discharge):
        rig.fake.record('disconnect')
        state = {'modules': {1: {'positive_v': 441.325}}, 'consecutive': 0}
        on_discharge(state)
        expected.append(copy.deepcopy(state))
        state['modules'][1]['positive_v'] = .05
        state['consecutive'] = 3
        on_discharge(state)
        expected.append(copy.deepcopy(state))
        return True
    rig.fake.disconnect = disconnect
    result = rig.run.run()
    states = [r['state'] for r in trace_rows(result.directory) if r['event'] == 'discharge_observation']
    assert states == [json.loads(json.dumps(x)) for x in expected]
    assert result.metadata['last_discharge'] == expected[-1]
    assert rig.fake.calls[-1][0] == 'disconnect'
    assert len([name for name, _ in rig.fake.calls if name == 'disconnect']) == 1


# Real ESI controller with simulated base methods, never a vendor DLL.
from test_esi_heater_activation import rig as heater_driver_rig


@pytest.mark.parametrize('valid,temperature', [(False, 0.), (True, float('nan')), (True, 176.)])
def test_raw_failed_temperature_precheck_is_saved(heater_driver_rig, tmp_path, monkeypatch, valid, temperature):
    r = heater_driver_rig
    ns = namespace()
    returned = (0, valid, .01, .02, .03, temperature)
    monkeypatch.setattr(r.base, 'get_heat_ctrl_monitoring', lambda self: returned)
    trace = ns['NativeTrace'](tmp_path)
    trace.install(r.base, r.driver)
    try:
        with pytest.raises(RuntimeError, match='sensor'):
            r.driver.set_heater_temperature(30., timeout_s=.2)
        assert r.state.target_calls == 0 and not any(r.state.active.values())
    finally:
        trace.finish(True)
    rows = [x for x in trace_rows(tmp_path) if x['event'] == 'native_return' and x['method'] == 'get_heat_ctrl_monitoring']
    assert len(rows) == 1
    assert rows[0]['result'] == ns['NativeTrace'].encode(returned)


def test_driver_call_sequence_is_unchanged_by_tracing(heater_driver_rig, tmp_path):
    r = heater_driver_rig
    ns = namespace()
    def sequence():
        r.driver.collect_diagnostics(timeout_s=.2)
        r.driver.set_heater_temperature(30., timeout_s=.2)
        r.driver.set_output_active(0, True, timeout_s=.2)
        r.driver.set_output_active(0, False, timeout_s=.2)
    r.state.target = 0.
    sequence()
    expected = list(r.calls)
    r.calls.clear()
    r.state.enabled = False
    trace = ns['NativeTrace'](tmp_path)
    trace.install(r.base, r.driver)
    try:
        sequence()
        assert r.calls == expected
    finally:
        trace.finish(True)
    assert not trace.errors


def test_approved_100c_50w_protocol_is_armed_but_not_executed_offline():
    ns = namespace()
    assert ns['ARM_HEATING'] is True
    assert ns['TARGETS_C'] == (30., 40., 50., 60., 70., 80., 90., 100.)
    assert ns['FINAL_TARGET_C'] == 100.
    assert ns['ENVELOPE'] == {'voltage_v': 22., 'current_a': 10., 'power_w': 50.}
    assert ns['Experiment'].__init__.__kwdefaults__['stage_s'] == 300.
    assert ns['Experiment'].__init__.__kwdefaults__['observation_s'] == 60.


def test_late_approach_has_a_separate_observation_budget(rig):
    """Reproduce the hardware near-50 C cutoff with real 300/60 s timing."""
    rig.run.stage_s, rig.run.observation_s = 300., 60.
    started = {}
    def target(value):
        if value == 50:
            started['at'] = rig.clock()
    def diagnostic(d):
        if rig.fake.active and rig.fake.target == 50:
            d['heat']['monitor_temperature_c'] = 47.9 if rig.clock() - started['at'] < 258 else 49.5
    rig.fake.hooks.update(set_heater_temperature=target, diagnostic=diagnostic)
    result = rig.run.run()
    assert result.metadata['outcome'] == 'reached_100_no_hold', result.metadata
    stage = next(s for s in result.metadata['observed_stages'] if s['target_c'] == 50)
    assert stage['elapsed_s'] - started['at'] == 318
    assert len([1 for method, _ in rig.fake.calls if method == 'configure_heat_limits']) == 1


@pytest.mark.parametrize('case', ['no_entry', 'entry_at_deadline', 'repeated_excursions'])
def test_approach_and_observation_remain_bounded(rig, case):
    started = {}
    def target(value):
        if value == 50:
            started['at'] = rig.clock()
    def diagnostic(d):
        if rig.fake.active and rig.fake.target == 50:
            elapsed = rig.clock() - started['at']
            if case == 'no_entry':
                temperature = 47.9
            elif case == 'entry_at_deadline':
                # The getter only returns at the approach deadline: no extension.
                rig.clock.now = started['at'] + rig.run.stage_s
                temperature = 49.5
            else:
                temperature = 49.5 if int(elapsed) % 2 else 47.9
            d['heat']['monitor_temperature_c'] = temperature
    rig.fake.hooks.update(set_heater_temperature=target, diagnostic=diagnostic)
    result = rig.run.run()
    assert result.metadata['outcome'] == 'not_reached', result.metadata
    assert [a[0] for m, a in rig.fake.calls if m == 'set_heater_temperature'] == [30., 30., 40., 50.]
    end = next(r['elapsed_s'] for r in result.rows if r['phase'] == 'cooling')
    expected = rig.run.stage_s + (rig.run.observation_s if case == 'repeated_excursions' else 0)
    assert end - started['at'] == expected
    assert result.shutdown_confirmed


@pytest.mark.parametrize('maximum', [99., 99.999, 100., 175., math.nan])
def test_hardware_temperature_limit_must_cover_100(rig, maximum):
    rig.fake.maximum['max_temperature_c'] = maximum
    result = rig.run.run()
    if maximum in (100., 175.):
        assert result.metadata['outcome'] == 'reached_100_no_hold', result.metadata
    else:
        assert not any(m in ('configure_heat_limits', 'set_heater_temperature') for m, _ in rig.fake.calls)


def test_storage_latency_does_not_complete_an_unobserved_interval(rig):
    rig.run.stage_s, rig.run.observation_s = 300., 60.
    append = rig.run.append
    seen, delayed = [], []
    def slow_append(row):
        append(row)
        if row['phase'] == 'heating' and row['requested_target_c'] == 30 and 29 <= row['temperature_c'] <= 31:
            seen.append(row['call_finished_s'])
            if not delayed and row['call_finished_s'] - seen[0] >= 59:
                delayed.append(True)
                rig.clock.now += 2.
    rig.run.append = slow_append
    result = rig.run.run()
    assert delayed and result.metadata['outcome'] == 'reached_100_no_hold', result.metadata
    stage = next(s for s in result.metadata['observed_stages'] if s['target_c'] == 30 and not s['repeat'])
    assert stage['elapsed_s'] - seen[0] >= 60


def test_stop_during_extended_observation_prevents_next_target(rig):
    started = {}
    def target(value):
        if value == 50:
            started['at'] = rig.clock()
    def diagnostic(d):
        if rig.fake.active and rig.fake.target == 50:
            elapsed = rig.clock() - started['at']
            if elapsed >= rig.run.stage_s:
                raise KeyboardInterrupt('simulated Stop in extra observation time')
            d['heat']['monitor_temperature_c'] = 49.5 if elapsed >= 7 else 47.9
    rig.fake.hooks.update(set_heater_temperature=target, diagnostic=diagnostic)
    result = rig.run.run()
    assert result.metadata['outcome'] == 'interrupted', result.metadata
    assert result.shutdown_confirmed and result.heat_cancel.is_set()
    assert [a[0] for m, a in rig.fake.calls if m == 'set_heater_temperature'] == [30., 30., 40., 50.]


def test_final_stage_near_100_never_gets_observation_extension(rig):
    rig.fake.saturation_temperature = 99.5
    starts = []
    rig.fake.hooks['set_heater_temperature'] = lambda value: starts.append((value, rig.clock()))
    result = rig.run.run()
    assert result.metadata['outcome'] == 'not_reached', result.metadata
    assert starts[-1][0] == 100.
    end = next(r['elapsed_s'] for r in result.rows if r['phase'] == 'cooling')
    assert end - starts[-1][1] == rig.run.stage_s
    assert not any(s['target_c'] == 100 for s in result.metadata['observed_stages'])


def test_complete_sequence_and_retained_limits(rig):
    result = rig.run.run()
    assert result.metadata['outcome'] == 'reached_100_no_hold', result.metadata
    assert result.metadata['shutdown_confirmed'] is True
    assert result.metadata['restart_30_confirmed'] is True
    assert [args[0] for method, args in rig.fake.calls if method == 'set_heater_temperature'] == [30., 30., 40., 50., 60., 70., 80., 90., 100.]
    assert len([1 for method, args in rig.fake.calls if method == 'configure_heat_limits']) == 1
    assert set(rig.fake.threads) == {result.owner.thread.ident}
    assert not rig.fake.active and not rig.fake.connected
    assert all(0 < value <= top for value, top in zip(rig.fake.limit.values(), (22, 10, 50)))
    assert result.metadata['initial_configuration']['power_limit_w'] == 0
    assert result.metadata['final_configuration']['power_limit_w'] > 0
    assert not result.guard_path.exists() and not rig.ns['_HC_GUARD']
    rows = list(csv.DictReader((result.directory / 'samples.csv').open()))
    assert len(rows) == len(result.rows)
    assert {'baseline', 'heating', 'restart_30', 'cycle_off', 'cooling'} <= {r['phase'] for r in rows}
    assert json.loads((result.directory / 'report.json').read_text())['shutdown_confirmed'] is True
    assert result.rows[-1]['temperature_c'] > 30  # Final OFF is NOT a claim of cold metal.
    final_rows = [r for r in result.rows if r['phase'] == 'heating' and r['requested_target_c'] == 100]
    assert final_rows[-1]['temperature_c'] >= 100
    assert sum(r['temperature_c'] >= 100 for r in final_rows) == 1  # OFF at first observation, no final hold.


@pytest.mark.parametrize('maximum', [21.999616, 22., .0001, 0., -1., math.nan, math.inf, True])
def test_voltage_hardware_limits(rig, maximum):
    rig.fake.maximum['max_voltage_v'] = maximum
    rig.run.run()
    if maximum in (21.999616, 22.):
        assert rig.run.metadata['outcome'] == 'reached_100_no_hold'
        assert rig.fake.limit['voltage_limit_v'] <= min(22, maximum)
    else:
        assert not any(method == 'set_heater_temperature' for method, _ in rig.fake.calls)


@pytest.mark.parametrize('cap,power', [(22., False), (10., False), (50., True), (21.999616, False), (.0001, False)])
def test_quantization_stays_inside_cap(cap, power):
    ns = namespace()
    if cap < .001:
        with pytest.raises(ValueError):
            ns['bounded_limit'](cap, power)
    else:
        value = ns['bounded_limit'](cap, power)
        assert 0 < value <= cap
        assert ns['native_value'](ns['native_code'](value, power), power) == value
        assert ns['native_value'](ns['native_code'](value, power) + 1, power) > cap


def test_native_quantization_evidence_is_from_matched_dll():
    """Read PE bytes, never execute the vendor binary."""
    ns = namespace()
    path = ROOT / 'esi/vendor/runtime/esi/vendor/x64/COM-ESI-CTRL.dll'
    blob = path.read_bytes()
    pe = struct.unpack_from('<I', blob, 0x3c)[0]
    sections, optional_size = struct.unpack_from('<H', blob, pe + 6)[0], struct.unpack_from('<H', blob, pe + 20)[0]
    def offset(rva):
        for i in range(sections):
            entry = pe + 24 + optional_size + 40 * i
            _, size, base, raw_size, raw = struct.unpack_from('<8sIIII', blob, entry)
            if base <= rva < base + max(size, raw_size):
                return raw + rva - base
        raise AssertionError(rva)
    assert [struct.unpack_from('<d', blob, offset(rva))[0] for rva in (0x280e0, 0x280f0, 0x28110, 0x28128)] == [1/1024, .5, 1024., 1e6]
    # Nearest-integer conversion in actual setters, not a synthetic configuration decoder.
    assert blob[offset(0xa4b5):offset(0xa4b5)+5] == bytes.fromhex('f2480f2cc1')
    assert blob[offset(0x9fa5):offset(0x9fa5)+5] == bytes.fromhex('f2480f2cc1')
    assert blob[offset(0xa2fe):offset(0xa2fe)+5] == bytes.fromhex('f2480f2cc1')


@pytest.mark.parametrize('saturation', [28., 39., 49., 59., 69., 79., 89., 99.])
def test_non_attainment_including_100_has_deadline_no_escalation(rig, saturation):
    rig.fake.saturation_temperature = saturation
    rig.run.run()
    assert rig.run.metadata['outcome'] == 'not_reached'
    assert rig.run.shutdown_confirmed
    assert len([1 for method, _ in rig.fake.calls if method == 'configure_heat_limits']) == 1
    assert rig.clock.now < 100


@pytest.mark.parametrize('field,value', [('valid', False), ('monitor_temperature_c', math.nan),
    ('monitor_output_v', math.nan), ('monitor_voltage_v', math.inf), ('monitor_current_a', math.nan),
    ('target_temperature_c', 90.), ('module_active', True)])
def test_invalid_initial_diagnostics_never_heat(rig, field, value):
    rig.fake.hooks['diagnostic'] = lambda d: d['heat'].update({field: value})
    rig.run.run()
    assert not any(method == 'set_heater_temperature' for method, _ in rig.fake.calls)
    assert rig.run.metadata['outcome'] != 'reached_100_no_hold'


@pytest.mark.parametrize('address', [1, 2])
@pytest.mark.parametrize('field,value', [('module_active', True), ('module_gate_active', True), ('target_v', 100.), ('target_v', math.nan)])
def test_hv_fault_refuses_heating(rig, address, field, value):
    rig.fake.hooks['diagnostic'] = lambda d: d['modules'][address].update({field: value})
    rig.run.run()
    assert not any(method == 'set_heater_temperature' for method, _ in rig.fake.calls)
    assert rig.run.shutdown_confirmed


def test_off_reset_of_limits_prevents_restart_without_rewrite(rig):
    def reset(address, active):
        if not active:
            rig.fake.limit['power_limit_w'] = 0.
    rig.fake.hooks['set_output_active'] = reset
    rig.run.run()
    assert [args for name, args in rig.fake.calls if name == 'set_output_active' and args[1]] == [(0, True)]
    assert len([1 for name, _ in rig.fake.calls if name == 'configure_heat_limits']) == 1
    assert rig.run.shutdown_confirmed
    assert not rig.run.metadata.get('restart_30_confirmed')


@pytest.mark.parametrize('method', ['connect', 'get_heat_configuration', 'configure_heat_limits', 'collect_diagnostics', 'set_heater_temperature', 'set_output_active'])
@pytest.mark.parametrize('state', [True, None, 'unknown'])
def test_poison_or_unknown_state_prevents_all_following_calls(rig, method, state):
    def poison(*args):
        rig.fake._transport_poisoned = state
        raise TimeoutError('simulated native timeout/unknown state')
    rig.fake.hooks[method] = poison
    rig.run.run()
    assert rig.fake.calls[-1][0] == method
    assert rig.run.metadata['outcome'] == 'shutdown_unconfirmed'
    assert rig.run.guard_path.exists() and rig.ns['_HC_GUARD']
    with pytest.raises(RuntimeError, match='Previous run'):
        rig.run.run()


def test_overtemperature_before_final_stage_stops_before_next_target(rig):
    def hot(d):
        if rig.fake.active:
            d['heat']['monitor_temperature_c'] = 101.
    rig.fake.hooks['diagnostic'] = hot
    rig.run.run()
    assert [args[0] for name, args in rig.fake.calls if name == 'set_heater_temperature'] == [30.]
    assert rig.run.metadata['outcome'] == 'error'
    assert rig.run.shutdown_confirmed


def test_shutdown_wait_warns_once_without_a_progress_callback(capsys):
    ns = namespace()
    clock = Clock()
    ns['time'] = SimpleNamespace(monotonic=clock)
    class Finished:
        def is_set(self):
            return clock.now >= 245
        def wait(self, _):
            clock.sleep(5)
    owner = SimpleNamespace(name='ESI', finished=Finished())
    assert ns['CallOwner'].wait_closed(owner)
    assert capsys.readouterr().out.count('shutdown pending:') == 1
    assert clock.now == 245


@pytest.mark.parametrize('plot_fails', [False, True])
def test_cleanup_progress_reports_transitions_without_native_calls(capsys, plot_fails):
    ns = namespace()
    run = object.__new__(ns['Experiment'])
    run.phase, run.cooling_s, run.errors = 'heating', 180., []
    run.owner = SimpleNamespace(finished=threading.Event())
    def render():
        if plot_fails:
            raise ValueError('render failed')
    run.refresh_plot = render
    run.call = lambda *a, **kw: pytest.fail('Display must not query the device')
    for phase in ('heating', 'cooling', 'shutdown'):
        run.phase = phase
        for _ in range(100):
            run.cleanup_progress()
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3
    assert 'owned operation' in lines[0]
    assert 'Cooling observation' in lines[1] and '180 s' in lines[1]
    assert 'HV discharge' in lines[2]
    assert len(run.errors) == (1 if plot_fails else 0)
    run.owner.finished.set()
    run.phase = 'cooling'
    run.cleanup_progress()
    assert capsys.readouterr().out == ''


def test_cleanup_display_failure_is_recorded_once_and_does_not_stop_plotting():
    ns = namespace()
    run = object.__new__(ns['Experiment'])
    run.phase, run.cooling_s, run.errors = 'cooling', 180., []
    run.owner = SimpleNamespace(finished=threading.Event())
    plotted = []
    run.refresh_plot = lambda: plotted.append(True)
    def broken_print(*args, **kwargs):
        raise OSError('output unavailable')
    ns['print'] = broken_print
    for phase in ('cooling', 'shutdown', 'shutdown'):
        run.phase = phase
        run.cleanup_progress()
    assert len(plotted) == 3
    assert run.errors == ['Cleanup display: OSError: output unavailable']
    assert not run.owner.finished.is_set()


def test_shutdown_wait_preserves_interrupt_and_timeout_handling():
    ns = namespace()
    clock = Clock()
    ns['time'] = SimpleNamespace(monotonic=clock)
    class Finished:
        def is_set(self):
            return False
        def wait(self, _):
            clock.sleep(5)
            raise KeyboardInterrupt('repeated Stop')
    owner = SimpleNamespace(name='ESI', finished=Finished())
    interrupts = []
    assert not ns['CallOwner'].wait_closed(owner, timeout=15, on_interrupt=interrupts.append)
    assert len(interrupts) == 3
    assert clock.now == 15


def test_progress_callback_replaces_repeated_generic_warning(capsys):
    ns = namespace()
    clock = Clock()
    ns['time'] = SimpleNamespace(monotonic=clock)
    class Finished:
        def is_set(self):
            return clock.now >= 245
        def wait(self, _):
            clock.sleep(5)
    owner = SimpleNamespace(name='ESI', finished=Finished())
    progress = []
    assert ns['CallOwner'].wait_closed(owner, on_progress=lambda: progress.append(clock.now))
    assert len(progress) == 49  # Every progress callback is retained.
    assert capsys.readouterr().out == ''


def test_approved_heater_experiment_is_armed_by_default():
    assert namespace()['ARM_HEATING'] is True


def test_disarmed_main_never_imports_or_constructs(monkeypatch, tmp_path, capsys):
    ns = namespace()
    ns['ARM_HEATING'] = False
    ns['OUTPUT_DIR'] = tmp_path
    ns['load_private'] = lambda *a, **kw: pytest.fail('No import when disarmed')
    assert ns['main']() is None
    assert 'NOT armed' in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('operation,args', [('collect_diagnostics', ()), ('set_heater_temperature', (30.,)), ('set_output_active', (0, True))])
def test_notebook_stop_cancels_real_readiness_wait_before_heating(heater_driver_rig, tmp_path, monkeypatch, operation, args):
    r = heater_driver_rig
    ns = namespace()
    run = ns['Experiment'](tmp_path / 'runs', lambda _: r.driver, plots_factory=Plots,
                           run_guard=make_test_guard(ns, tmp_path, tmp_path / 'runs'))
    run.started = run.clock()
    run.claim()
    run.esi = r.driver
    run.trace = ns['NativeTrace'](run.directory)
    run.owner = ns['CallOwner']('cancellation-test')
    entered = threading.Event()
    polls = []
    def flags(self, address):
        polls.append(address)
        entered.set()
        return 0, 0
    monkeypatch.setattr(r.base, 'get_module_data_ready_flags', flags)
    monkeypatch.setattr(r.base, 'get_heat_ctrl_monitoring', lambda self: pytest.fail('No unavailable datum may be read'))
    run.cleanup = lambda: run.call('set_output_active', 0, False, cleanup=True)
    def stop():
        assert entered.wait(2)
        run.request_stop()
    stopper = threading.Thread(target=stop)
    stopper.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            run.call(operation, *args)
    finally:
        stopper.join(2)
        run.request_stop()
        assert run.owner.wait_closed(timeout=2)
        run.trace.finish(True)
        run.stream.close()
    assert run.heat_cancel.is_set()
    assert polls and len(polls) <= 2
    assert ('module', 0, True) not in r.calls and ('enable', True) not in r.calls
    assert not any(c[0] == 'temperature' and c[1] > 0 for c in r.calls)
    assert ('module', 0, False) in r.calls and ('temperature', 0.) in r.calls
    assert r.driver._transport_poisoned is False


@pytest.mark.parametrize('stop_during_transition', [False, True])
def test_notebook_waits_for_real_runtime_activation_or_stops(heater_driver_rig, tmp_path, monkeypatch, stop_during_transition):
    r = heater_driver_rig
    ns = namespace()
    run = ns['Experiment'](tmp_path / 'runs', lambda _: r.driver, plots_factory=Plots,
                           run_guard=make_test_guard(ns, tmp_path, tmp_path / 'runs'))
    run.started = run.clock()
    run.claim()
    run.esi = r.driver
    run.trace = ns['NativeTrace'](run.directory)
    run.owner = ns['CallOwner']('activation-confirmation-test')
    run.cleanup = lambda: run.call('set_output_active', 0, False, cleanup=True)
    complete = r.base.get_complete_state
    observed = []
    entered = threading.Event()
    def delayed(self):
        result = list(complete(self))
        if r.state.active[0]:
            result[-1][0] = 0x4100 if not observed or stop_during_transition else 0xc100
            observed.append(result[-1][0])
            entered.set()
        return tuple(result)
    monkeypatch.setattr(r.base, 'get_complete_state', delayed)
    def stop():
        assert entered.wait(2)
        run.request_stop()
    stopper = threading.Thread(target=stop) if stop_during_transition else None
    try:
        run.applied_target = run.call('set_heater_temperature', 30.)
        run.requested, run.heat_requested = 30., True
        if stopper:
            stopper.start()
            with pytest.raises(KeyboardInterrupt):
                run.call('set_output_active', 0, True)
        else:
            assert run.call('set_output_active', 0, True) is True
            assert run.sample()['temperature_c'] == r.state.temperature
            assert observed[:2] == [0x4100, 0xc100]
        assert r.calls.count(('module', 0, True)) == r.calls.count(('enable', True)) == 1
    finally:
        if stopper:
            stopper.join(2)
        run.request_stop()
        assert run.owner.wait_closed(timeout=2)
        run.trace.finish(True)
        run.stream.close()
    assert ('module', 0, False) in r.calls
    assert r.driver._transport_poisoned is False


def test_armed_default_never_resolves_the_old_marker(tmp_path):
    ns = namespace()
    ns['OUTPUT_DIR'] = tmp_path
    ns['sys'] = SimpleNamespace(platform='win32', maxsize=sys.maxsize, modules={})
    ns['find_plugin'] = lambda _: tmp_path / 'esi'
    marker = tmp_path / '.heater_characterization.running.json'
    original = b'{"warning":"original uncertain shutdown"}'
    marker.write_bytes(original)
    ns['load_private'] = lambda *a, **kw: pytest.fail('No import or native access')
    with pytest.raises(RuntimeError, match='Unfinished run'):
        ns['main']()
    assert marker.read_bytes() == original
    assert list(tmp_path.iterdir()) == [marker]


def test_stale_namespace_is_refused_before_import():
    ns = namespace()
    name = '_test_stale_esi_heater_child'
    sys.modules[name] = SimpleNamespace(__file__=str(ROOT / 'esi/vendor/runtime/esi/esi.py'))
    try:
        with pytest.raises(RuntimeError, match='already present'):
            ns['load_private'](ROOT / 'esi/vendor/runtime/__init__.py', package=True, fresh=True)
    finally:
        sys.modules.pop(name)


def test_runtime_fingerprints_record_any_version(tmp_path):
    ns = namespace()
    recorded = ns['runtime_fingerprints'](ROOT / 'esi')
    assert set(recorded) == set(ns['_RUNTIME_FILES'])
    for path in ns['_RUNTIME_FILES']:
        out = tmp_path / path
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes((ROOT / 'esi' / path).read_bytes())
    (tmp_path / 'vendor/runtime/esi/esi.py').write_text('# updated runtime')
    updated = ns['runtime_fingerprints'](tmp_path)  # recorded, never refused
    assert updated['vendor/runtime/esi/esi.py'] == hashlib.sha256(b'# updated runtime').hexdigest()
    assert updated['vendor/runtime/esi/esi_base.py'] == recorded['vendor/runtime/esi/esi_base.py']


@pytest.mark.skipif(os.name == 'nt', reason='POSIX real SIGINT injection; simulated calls only')
@pytest.mark.parametrize('phase', ['connect', 'configure_heat_limits', 'collect_diagnostics', 'set_output_active', 'cycle_off', 'disconnect'])
def test_real_sigint_during_instrument_call_in_subprocess(tmp_path, phase):
    command = [sys.executable, str(Path(__file__).resolve()), 'interrupt', phase, str(tmp_path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def interrupt_probe(phase, directory):
    ns = namespace()
    clock, fake = Clock(), FakeESI()
    calls = threading.Event()
    def blocked(*args):
        if calls.is_set() or (phase == 'cycle_off' and args[1]):
            return
        calls.set()
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(.1)  # Simulate a native call that returns only after Stop was requested.
    fake.hooks['set_output_active' if phase == 'cycle_off' else phase] = blocked
    run = ns['Experiment'](Path(directory)/phase, lambda _: fake, baseline_s=2, observation_s=2,
                           stage_s=8, cycle_s=2, cooling_s=2, sample_s=1,
                           clock=clock, sleep=clock.sleep, plots_factory=Plots,
                           run_guard=make_test_guard(ns, Path(directory), Path(directory)/phase))
    run.run()
    assert calls.is_set()
    assert run.shutdown_confirmed, run.metadata
    assert set(fake.threads) == {run.owner.thread.ident}
    index = next(i for i, (name, args) in enumerate(fake.calls)
                 if (name == phase if phase != 'cycle_off' else name == 'set_output_active' and not args[1]))
    assert not any(name == 'set_heater_temperature' or (name == 'set_output_active' and args[1])
                   for name, args in fake.calls[index+1:])
    assert fake.calls[-1][0] == 'disconnect'
    assert not run.guard_path.exists()


def test_final_target_classification_uses_the_observation_not_cleanup_modified_state(rig):
    original = rig.run.request_stop
    def cleanup_first():
        is_final_stage = rig.run.phase == 'heating' and rig.run.requested == 100
        original()
        if is_final_stage:
            assert rig.run.owner.wait_closed(timeout=5)
    rig.run.request_stop = cleanup_first
    rig.run.run()
    assert rig.run.metadata['outcome'] == 'reached_100_no_hold', rig.run.metadata
    assert rig.run.shutdown_confirmed


@pytest.mark.parametrize('key', ['output_voltage_v', 'heater_power_w'])
@pytest.mark.parametrize('bad', [math.nan, math.inf, -math.inf])
def test_nonfinite_electrical_target_telemetry_blocks_heating(rig, key, bad):
    rig.fake.hooks['diagnostic'] = lambda d: d['heat'].update({key: bad})
    rig.run.run()
    assert not any(name == 'set_heater_temperature' for name, _ in rig.fake.calls)
    assert rig.run.metadata['outcome'] == 'error'
    assert rig.run.shutdown_confirmed


@pytest.mark.parametrize('key', ['voltage_v', 'current_a', 'power_w'])
@pytest.mark.parametrize('bad', [None, True, math.nan, math.inf, 0., -1., 999., 'value'])
def test_bad_applied_echo_never_activates(rig, key, bad):
    original = rig.fake.configure_heat_limits
    def wrong(**kwargs):
        result = original(**kwargs)
        result[key] = bad
        return result
    rig.fake.configure_heat_limits = wrong
    rig.run.run()
    assert not any(name == 'set_heater_temperature' for name, _ in rig.fake.calls)
    assert rig.run.shutdown_confirmed


@pytest.mark.parametrize('key', ['voltage_limit_v', 'current_limit_a', 'power_limit_w'])
def test_independent_readback_disagreement_prevents_on(rig, key):
    original = rig.fake.get_heat_configuration
    def wrong():
        result = original()
        if rig.fake.limit[key] > 0:
            result[key] *= .9
        return result
    rig.fake.get_heat_configuration = wrong
    rig.run.run()
    assert not any(name == 'set_heater_temperature' for name, _ in rig.fake.calls)
    assert rig.run.shutdown_confirmed


@pytest.mark.parametrize('method', ['collect_diagnostics', 'configure_heat_limits', 'set_heater_temperature'])
def test_late_native_completion_after_poison_never_resumes_or_closes(rig, method):
    released, finished = threading.Event(), threading.Event()
    def timeout(*args):
        def native():
            released.wait(5)
            finished.set()
        threading.Thread(target=native, daemon=True).start()
        rig.fake._transport_poisoned = True
        raise TimeoutError('Python wait expired; native worker still active')
    rig.fake.hooks[method] = timeout
    rig.run.run()
    before = list(rig.fake.calls)
    assert not finished.is_set()
    assert rig.run.guard_path.exists()
    released.set()
    assert finished.wait(2)
    time.sleep(.02)
    assert rig.fake.calls == before
    assert before[-1][0] == method


@pytest.mark.parametrize('state', [True, None])
def test_successful_return_cannot_hide_poison(rig, state):
    original = rig.fake.configure_heat_limits
    def late(**kwargs):
        result = original(**kwargs)
        rig.fake._transport_poisoned = state
        return result
    rig.fake.configure_heat_limits = late
    rig.run.run()
    assert rig.fake.calls[-1][0] == 'configure_heat_limits'
    assert not rig.run.shutdown_confirmed


def test_disconnect_failure_keeps_guard(rig):
    def fails(on_discharge):
        rig.fake.record('disconnect')
        return False
    rig.fake.disconnect = fails
    rig.run.run()
    assert rig.run.metadata['outcome'] == 'shutdown_unconfirmed'
    assert rig.run.guard_path.exists() and rig.ns['_HC_GUARD']


def test_storage_failure_during_heating_still_shuts_down(rig):
    original = rig.run.append
    def failure(row):
        if row['heater_requested']:
            raise OSError('simulated disk full')
        return original(row)
    rig.run.append = failure
    rig.run.run()
    assert rig.run.shutdown_confirmed
    assert rig.fake.calls[-1][0] == 'disconnect'
    assert rig.run.metadata['outcome'] == 'error'


def test_plot_failure_during_heating_still_shuts_down(rig):
    class Failing(Plots):
        def update(self, rows):
            if any(r['heater_requested'] for r in rows):
                raise RuntimeError('simulated rendering failure')
    rig.run.plots_factory = Failing
    rig.run.run()
    assert rig.run.shutdown_confirmed
    assert rig.fake.calls[-1][0] == 'disconnect'


def test_existing_disk_guard_blocks_before_construction(rig):
    rig.run.output_dir.mkdir()
    (rig.run.run_guard.heater_dir / '.heater_characterization.running.json').write_text('{}')
    with pytest.raises(RuntimeError, match='Unfinished'):
        rig.run.run()
    assert not rig.fake.calls


def test_start_hot_never_skips_30_and_restart(rig):
    rig.fake.temperature = 40.
    rig.run.run()
    assert not any(name == 'configure_heat_limits' for name, _ in rig.fake.calls)
    assert rig.run.shutdown_confirmed


def test_cross_notebook_uncertain_shutdown_guard(tmp_path):
    ns = namespace()
    ns['ARM_HEATING'] = True
    ns['sys'] = SimpleNamespace(platform='win32', maxsize=sys.maxsize, modules={})
    ns['OUTPUT_DIR'] = tmp_path / 'unrelated'
    root = tmp_path / 'esi'
    (root / 'vendor/runtime').mkdir(parents=True)
    (root / 'vendor/runtime/__init__.py').write_text('')
    ns['PLUGIN_DIR'] = str(root)
    guard = tmp_path / 'notebooks/pressure_temperature_runs/.pressure_temperature.running.json'
    guard.parent.mkdir(parents=True)
    guard.write_text('{}')
    ns['load_private'] = lambda *a, **kw: pytest.fail('No import or hardware access')
    ns['runtime_fingerprints'] = lambda _: {}
    install_test_guard(ns, tmp_path)
    with pytest.raises(RuntimeError, match='Unfinished'):
        ns['main']()


@pytest.fixture
def restart_case(tmp_path):
    ns = namespace()
    ns['sys'] = SimpleNamespace(modules={}, platform='win32', maxsize=sys.maxsize)
    root = tmp_path / 'esi'
    directory = root / 'logs/esi_heater_characterization'
    old = directory / '20260928T135158Z_oldrun00'
    old.mkdir(parents=True)
    report = {'started_utc': '2026-09-28T13:51:58.713014+00:00',
              'ended_utc': '2026-09-28T13:53:00+00:00', 'shutdown_confirmed': False, 'esi_com': 16,
              'errors': ['HV discharge unconfirmed'], 'outcome': 'shutdown_unconfirmed'}
    (old / 'report.json').write_text(json.dumps(report))
    (old / 'samples.csv').write_text('temperature_c\n21.5\n')
    (old / 'native_calls.jsonl').write_text('{"Valid":false}\n')
    marker = directory / '.heater_characterization.running.json'
    raw = json.dumps({'started_utc': '2026-09-28T13:51:58.713611+00:00'}).encode()
    marker.write_bytes(raw)
    helper = install_test_guard(ns, tmp_path)
    guard = helper.ExperimentGuard(plugin_dir=root, com=16, kind='heater', output_dir=directory, kernel_globals=ns)
    ns['input'] = lambda prompt: 'RESTART ' + prompt.split('RESTART ')[1].split(':')[0]
    case = SimpleNamespace(ns=ns, root=root, directory=directory, old=old,
                           marker=marker, raw=raw, helper=helper, guard=guard)
    try:
        yield case
    finally:
        for run in ns['_HC_GUARD'].values():
            if getattr(run, 'stream', None) is not None:
                run.stream.close()
        if not guard._released:
            guard._release_unused()


def make_restart_run(case, restart):
    run = case.ns['Experiment'](case.directory, lambda _: pytest.fail('No construction in claim'),
                                plots_factory=Plots, run_guard=case.guard, operator_restarts=restart)
    run.started = run.clock()
    return run


def test_operator_restart_preserves_originals_before_atomic_claim(restart_case):
    c = restart_case
    before = {p.name: p.read_bytes() for p in c.old.iterdir()}
    restart = c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw  # Authorization alone never clears the guard.
    record = restart[0]
    archive = Path(record['archive'])
    assert (archive / 'markers/000.json').read_bytes() == c.raw
    assert record['shutdown_confirmed'] is False
    assert record['process_termination_verified_by_software'] is False
    for name, value in before.items():
        assert (archive / 'runs/000' / name).read_bytes() == value
        assert (c.old / name).read_bytes() == value
    run = make_restart_run(c, restart)
    run.claim()
    marker = json.loads(c.marker.read_bytes())
    assert marker['run_directory'] == str(run.directory)
    assert run.metadata['operator_restarts'] == [record]
    assert json.loads((run.directory / 'report.json').read_bytes())['shutdown_confirmed'] is False
    c.guard.check()
    with pytest.raises(RuntimeError, match='owner|locking'):
        c.helper.RunLock(c.directory)


@pytest.mark.parametrize('answer', ['', 'yes', 'RESTART wrong-run', ' RESTART'])
def test_restart_requires_exact_new_run_specific_declaration(restart_case, answer):
    c = restart_case
    c.ns['input'] = lambda _: answer
    with pytest.raises(RuntimeError, match='declaration'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw
    assert not (c.guard.state_dir / 'operator_restarts').exists()


@pytest.mark.parametrize('missing', ['report.json', 'samples.csv'])
def test_restart_missing_required_evidence_never_prompts(restart_case, missing):
    c = restart_case
    (c.old / missing).unlink()
    c.ns['input'] = lambda _: pytest.fail('No confirmation without required evidence')
    with pytest.raises(RuntimeError, match='Unfinished|evidence'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw


@pytest.mark.parametrize('kind', ['memory', 'namespace', 'source'])
def test_restart_old_kernel_or_namespace_refused_before_prompt(restart_case, kind):
    c = restart_case
    if kind == 'memory':
        c.ns['_HC_GUARD']['owner'] = object()
    elif kind == 'namespace':
        c.ns['sys'].modules['_esibd_bundled_esi_old.child'] = SimpleNamespace()
    else:
        c.ns['sys'].modules['unexpected_alias'] = SimpleNamespace(__file__=str(c.root / 'vendor/runtime/esi/esi.py'))
    c.ns['input'] = lambda _: pytest.fail('No prompt in the old kernel')
    with pytest.raises(RuntimeError, match='kernel|namespace|runtime'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw


@pytest.mark.parametrize('stage', ['copy', 'declaration', 'fsync'])
def test_restart_archive_failure_preserves_blocking_marker(restart_case, monkeypatch, stage):
    c = restart_case
    def fail(*args, **kwargs):
        raise OSError('simulated archive failure')
    if stage == 'copy':
        monkeypatch.setattr(c.helper, 'durable_new_file', fail)
    elif stage == 'declaration':
        monkeypatch.setattr(c.helper, 'save_json', fail)
    else:
        monkeypatch.setattr(c.ns['os'], 'fsync', fail)
    with pytest.raises(OSError, match='archive'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw
    assert json.loads((c.old / 'report.json').read_bytes())['shutdown_confirmed'] is False


@pytest.mark.parametrize('changed', ['marker', 'trace'])
def test_restart_change_during_confirmation_aborts(restart_case, changed):
    c = restart_case
    def confirm(prompt):
        if changed == 'marker':
            c.marker.write_bytes(c.raw + b' ')
        else:
            (c.old / 'native_calls.jsonl').write_text('late native return')
        return 'RESTART ' + prompt.split('RESTART ')[1].split(':')[0]
    c.ns['input'] = confirm
    with pytest.raises(RuntimeError, match='changed'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.exists()
    assert not (c.guard.state_dir / 'operator_restarts').exists()


def test_restart_changed_guard_after_archive_never_constructs(restart_case):
    c = restart_case
    restart = c.guard.authorize_restart(input_fn=c.ns['input'])
    c.marker.write_bytes(c.raw + b' ')
    with pytest.raises(RuntimeError, match='changed'):
        make_restart_run(c, restart).run()
    assert c.marker.read_bytes() == c.raw + b' '


def test_restart_declaration_cannot_be_reused(restart_case):
    c = restart_case
    restart = c.guard.authorize_restart(input_fn=c.ns['input'])
    with pytest.raises(RuntimeError, match='already used'):
        c.guard.authorize_restart(input_fn=c.ns['input'])
    assert c.marker.read_bytes() == c.raw


@pytest.mark.parametrize('after_replace', [False, True])
def test_restart_interrupted_transition_never_opens_and_keeps_evidence(restart_case, after_replace):
    c = restart_case
    restart = c.guard.authorize_restart(input_fn=c.ns['input'])
    save = c.helper.save_json
    def interrupted(path, data):
        if path == c.marker:
            if after_replace:
                save(path, data)
            raise KeyboardInterrupt('transition interruption')
        return save(path, data)
    c.helper.save_json = interrupted
    run = make_restart_run(c, restart)
    with pytest.raises(KeyboardInterrupt):
        run.run()
    assert run.esi is None and run.owner is None
    assert c.marker.exists()
    assert (Path(restart[0]['archive']) / 'markers/000.json').read_bytes() == c.raw
    assert json.loads((c.old / 'report.json').read_bytes())['shutdown_confirmed'] is False
    if not after_replace:
        assert c.marker.read_bytes() == c.raw
    else:
        assert json.loads((run.directory / 'report.json').read_bytes())['shutdown_confirmed'] is False


def test_restart_competitor_process_cannot_acquire_ownership(restart_case):
    c = restart_case
    script = '''import sys,importlib.util\nfrom pathlib import Path\ns=importlib.util.spec_from_file_location('guard',sys.argv[2]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)\ntry:\n m.RunLock(Path(sys.argv[1]))\nexcept RuntimeError:\n sys.exit(0)\nsys.exit(1)\n'''
    env = dict(os.environ, PYTHONPATH=str(NOTEBOOK.parents[1] / 'tests'))
    result = subprocess.run([sys.executable, '-c', script, str(c.directory), str(ROOT / 'esi/_experiment_guard.py')], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert c.marker.read_bytes() == c.raw


def test_armed_main_operator_restart_runs_complete_simulated_protocol(restart_case):
    c = restart_case
    c.guard.abandon_before_hardware()  # Main obtains its own ownership; original marker stays.
    c.ns.update(OUTPUT_DIR=c.directory, find_plugin=lambda _: c.root, runtime_fingerprints=lambda _: {})
    fake, clock = FakeESI(), Clock()
    original = c.ns['Experiment']
    c.ns['Experiment'] = lambda *a, **kw: original(*a, **kw, clock=clock, sleep=clock.sleep,
        baseline_s=2, observation_s=2, stage_s=8, cycle_s=2, cooling_s=2, plots_factory=Plots)
    def construct(*args, **kwargs):
        declarations = list((c.guard.state_dir / 'operator_restarts').glob('*/operator-declaration.json'))
        assert len(declarations) == 1
        assert json.loads(declarations[0].read_bytes())['shutdown_confirmed'] is False
        assert c.marker.read_bytes() != c.raw
        return fake
    runtime = SimpleNamespace(__name__='fake_runtime', ESI=construct)
    c.ns['load_private'] = lambda *a, **kw: runtime
    c.ns['require_safe_esi'] = lambda _: None  # Fake runtime; the real capability check has separate tests.
    c.ns['sys'].modules['fake_runtime.esi.esi_base'] = SimpleNamespace(ESIBase=None)
    run = c.ns['main']()
    assert run.metadata['outcome'] == 'reached_100_no_hold'
    assert run.shutdown_confirmed and not c.marker.exists()
    assert run.run_guard._released
    assert [args[0] for name, args in fake.calls if name == 'set_heater_temperature' and args[0] > 0] == [30., 30., 40., 50., 60., 70., 80., 90., 100.]
    assert json.loads((c.old / 'report.json').read_bytes())['shutdown_confirmed'] is False


def test_windows_lock_branch_locks_first_byte_and_closes_stream(restart_case, monkeypatch, tmp_path):
    c = restart_case
    calls = []
    def locking(fd, mode, count):
        calls.append((mode, count, os.lseek(fd, 0, os.SEEK_CUR)))
    monkeypatch.setitem(sys.modules, 'msvcrt', SimpleNamespace(LK_NBLCK=2, locking=locking))
    c.helper.os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os) if name != 'name'}, name='nt')
    lock = c.helper.RunLock(tmp_path / 'windows-branch-simulation')
    assert calls == [(2, 1, 0)]
    lock.check()
    lock.close()
    assert lock.stream.closed


def test_report_persistence_failure_keeps_shared_claim_and_ownership(rig):
    save = rig.ns['save_json']
    def fail_final(path, data):
        if 'ended_utc' in data:
            raise OSError('final report could not persist')
        return save(path, data)
    rig.ns['save_json'] = fail_final
    with pytest.raises(OSError, match='persist'):
        rig.run.run()
    assert rig.run.shutdown_confirmed  # Fake OFF succeeded, but durable proof did not.
    assert rig.run.run_guard.registry.exists() and rig.run.guard_path.exists()
    assert not rig.run.run_guard._released and rig.ns['_HC_GUARD']


def test_failed_constructor_never_becomes_safe_via_none_instance(rig):
    rig.run.factory = lambda _: (_ for _ in ()).throw(RuntimeError('constructor may have opened transport'))
    rig.run.run()
    assert rig.run.constructor_attempted and rig.run.esi is None
    assert rig.run.shutdown_confirmed is False
    assert rig.run.run_guard.registry.exists() and rig.run.guard_path.exists()
    assert not rig.run.run_guard._released
    assert not rig.fake.calls


def test_preconstruction_plot_failure_abandons_only_fresh_claim(rig):
    rig.run.plots_factory = lambda: (_ for _ in ()).throw(RuntimeError('plot initialization failed'))
    rig.run.run()
    assert not rig.run.constructor_attempted and not rig.fake.calls
    assert not rig.run.run_guard.registry.exists() and not rig.run.guard_path.exists()
    assert not rig.ns['_HC_GUARD']


def test_missing_shared_guard_never_constructs(tmp_path):
    ns = namespace()
    run = ns['Experiment'](tmp_path / 'runs', lambda _: pytest.fail('No unguarded factory'))
    with pytest.raises(RuntimeError, match='Shared ESI experiment guard'):
        run.run()


if __name__ == '__main__' and len(sys.argv) > 1 and sys.argv[1] == 'interrupt':
    interrupt_probe(sys.argv[2], sys.argv[3])


def test_capability_guard_refuses_an_old_runtime_without_the_safety_contract(tmp_path):
    ns = namespace()
    # Both method names existed in an unsafe intermediate version without the contract.
    (tmp_path / "old.py").write_text("class Controller:\n    def _set_heat_module_active_unlocked(self): pass\n"
                                     "    def _validate_heat_operating_state_unlocked(self): pass\n")
    init = tmp_path / "__init__.py"
    init.write_text("from .old import Controller\n")
    runtime = ns['load_private'](init, package=True)
    runtime.ESI = SimpleNamespace(_PROCESS_CONTROLLER_CLASS=runtime.Controller)
    with pytest.raises(RuntimeError, match='heater safety'):
        ns['require_safe_esi'](runtime)
    runtime.Controller.HEATER_SAFETY_CONTRACT = 1  # a later version keeping the contract
    ns['require_safe_esi'](runtime)
