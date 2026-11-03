# SPDX-License-Identifier: GPL-2.0-or-later
"""MScan map runner (v0.5.10): the Q2 map of docs/quadrupole_optimization_plan.md, line by line.

Every line of the map is an ordinary MScan scan, started, stopped and saved exactly like one from
the Start button, so it keeps all of MScan's checks (PSU headroom and Ilim, verified U1,2 writes,
DMMR, ESI and TPG366 recording, file naming). The decisions belong to the MScan Rust worker
(``mscan_map``): this runner asks it for the next line, applies the settings a line does not sweep
(AMX frequency, Q1_DC, entry stub and the channels that follow x), runs the line, reports its
arrays and writes the journal the worker returns. The journal is the whole state of the map, so a
crash, a power cut or Stop lose nothing and Resume continues from the last finished line.

At most one native worker exists at a time: the map calls use a short-lived worker between two
lines, MScan's own worker runs each line.

Faults (Section 9 of the plan):
- a line that fails (spray out of its window, a worker or detector fault, a line much slower than
  expected) is reported as failed and the worker asks for it again; three failures of one line,
  or three failed lines within one hour, stop the map;
- a line the PSU or a channel limit cannot reach is reported as unreachable: that setting closes;
- anything else, or Stop, is a safe stop.

Safe stop: the AMX frequency, Q1_DC, the entry stub and the channels following x return to their
values at Start. The PSU rails and the rod pairs are left as the last line left them: restored at
the end of a completed line, held after a line fault, as in MScan (no write after a fault). The
PSU range, Ilim and the ESI are never touched.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import math
import os
import shutil
import sys
import threading
import time
import tomllib
import traceback
from pathlib import Path

import numpy as np

JOURNAL = 'journal.json'
STATUS = 'status.json'
REPORT = 'report.json'
STORE = 'map.h5'
PLAN_COPY = 'plan.toml'
START_VALUES = 'start_values.json'
BEST_SETTINGS = 'best_settings.ini'
FIGURES = 'figures.png'
LOG = 'map.log'

UNREACHABLE_MARKERS = ('headroom', 'ilim', 'exceeds allowed', 'outside allowed', 'current limit',
                       'no admissible', 'cannot reach')


class SafeStop(RuntimeError):
    """The map stops: settings return to their values at Start."""


class LineFailed(RuntimeError):
    """A line to measure again."""


class LineUnreachable(RuntimeError):
    """A line a hardware limit forbids: its setting closes."""


def atomic_json(path: Path, data) -> None:
    """Write JSON so that a crash leaves either the old or the new file, never half of one."""
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as handle:
        json.dump(data, handle, indent=1, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    if path.exists():
        shutil.copyfile(path, path.with_name(path.name + '.bak'))
    os.replace(tmp, path)


def finite(values):
    """JSON has no NaN: the worker reads null as a missing value."""
    return [None if v is None or not math.isfinite(float(v)) else float(v) for v in values]


def load_plan(path) -> dict:
    with Path(path).open('rb') as handle:
        plan = tomllib.load(handle)
    if not isinstance(plan.get('map'), dict):
        raise ValueError('A map plan needs a [map] table.')
    return plan


class KeepAwake:
    """Keep Windows from idle sleep for the whole map (a manual sleep is not prevented). No-op elsewhere."""

    def __init__(self, reason):
        self.reason, self._handle, self._api = str(reason), None, None

    def _kernel(self):
        if sys.platform != 'win32':
            return None
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

        class Context(ctypes.Structure):
            _fields_ = [('Version', ctypes.c_ulong), ('Flags', ctypes.c_ulong), ('SimpleReasonString', ctypes.c_wchar_p)]

        kernel32.PowerCreateRequest.argtypes = [ctypes.POINTER(Context)]
        kernel32.PowerCreateRequest.restype = ctypes.c_void_p
        for name in ('PowerSetRequest', 'PowerClearRequest'):
            getattr(kernel32, name).argtypes = [ctypes.c_void_p, ctypes.c_int]
            getattr(kernel32, name).restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        return kernel32, Context

    def hold(self, held: bool) -> bool:
        try:
            if held and self._handle is None:
                api = self._kernel()
                if api is None:
                    return False
                kernel32, context = api
                handle = kernel32.PowerCreateRequest(ctypes.byref(context(0, 1, self.reason)))
                if handle and kernel32.PowerSetRequest(handle, 0) and kernel32.PowerSetRequest(handle, 1):
                    self._handle, self._api = handle, kernel32  # PowerRequestDisplayRequired, SystemRequired
                elif handle:
                    kernel32.CloseHandle(handle)
            elif not held and self._handle is not None:
                self._api.PowerClearRequest(self._handle, 1)
                self._api.PowerClearRequest(self._handle, 0)
                self._api.CloseHandle(self._handle)
                self._handle = None
        except (AttributeError, OSError):
            return False
        return self._handle is not None


class MapRunner:
    """Runs one map in its own thread; every Explorer access goes through ``scan._gui``."""

    POLL_S = .2
    STATIC_TIMEOUT_S = 90.
    SPRAY_WAIT_S = 1800.
    FAILURES_PER_HOUR = 3
    LINE_MARGIN_S = 180.

    def __init__(self, scan, engine, *, data_path, clock=time.time, monotonic=time.monotonic):
        """``engine(method, *args)`` calls the MScan worker's map methods (one short-lived worker)."""
        self.scan, self.engine = scan, engine
        self.data_path = Path(data_path)
        self.clock, self.monotonic = clock, monotonic
        self.cancel = threading.Event()
        self.thread = None
        self.folder = None
        self.journal = None
        self.status = dict(state='idle', message='')
        self.start_values = {}
        self.spray_reference = None
        self._failures = []
        self._operator_settings = None

    # ------------------------------------------------------------------ public

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def instrument(self, plan) -> dict:
        """Live readings the plan is checked against (GUI thread through ``scan._gui``)."""
        return self.scan._gui(lambda: self.scan._map_instrument(plan['map'], self.data_path), cancel=False)

    def check(self, plan_path) -> dict:
        plan = load_plan(plan_path)
        return self.engine('map_check', plan, self.instrument(plan))

    def simulate(self, plan_path, hypothesis='H1') -> dict:
        plan = load_plan(plan_path)
        return self.engine('map_simulate', plan, self.instrument(plan), dict(hypothesis=hypothesis))

    def start(self, plan_path=None, *, resume=None) -> Path:
        """Start a new map from a plan file, or resume the map of folder ``resume``."""
        if self.running:
            raise RuntimeError('A map is already running.')
        if resume is not None:
            self.folder = Path(resume)
            self.journal = json.loads((self.folder / JOURNAL).read_text(encoding='utf-8'))
        else:
            plan = load_plan(plan_path)
            instrument = self.instrument(plan)
            started = self.clock()
            stamp = time.strftime('%Y-%m-%d_%H-%M-%S', time.localtime(started))
            name = ''.join(ch if ch.isalnum() or ch in '-_' else '-' for ch in str(plan['map'].get('name', 'map')))
            self.folder = self.data_path / self.scan.FOLDER / 'maps' / f'{stamp}_{name}'
            self.folder.mkdir(parents=True, exist_ok=False)
            shutil.copyfile(plan_path, self.folder / PLAN_COPY)
            self.journal = self.engine('map_begin', plan, instrument, started)
            atomic_json(self.folder / JOURNAL, self.journal)
        self.cancel.clear()
        self._set_status('starting', f'Map in {self.folder}')
        self.thread = threading.Thread(target=self._run, name='MScan map', daemon=True)
        self.thread.start()
        return self.folder

    def stop(self):
        """Safe stop: the current line stops like a scan, then the settings return to Start."""
        self.cancel.set()
        with contextlib.suppress(Exception):
            self.scan._gui(lambda: self.scan._stop_map_line(), cancel=False)

    # ------------------------------------------------------------------ the loop

    def _run(self):
        awake = KeepAwake('MScan map: unattended Q2 map in progress')
        awake.hold(True)
        reason, state = 'map complete', 'done'
        try:
            self._capture_start_values()
            reason = self._loop()
        except SafeStop as exc:
            reason, state = str(exc), 'stopped'
        except Exception as exc:  # noqa: BLE001 - any unexpected fault is a safe stop with its trace
            reason, state = f'{type(exc).__name__}: {exc}', 'stopped'
            self._log(traceback.format_exc())
        finally:
            try:
                self._restore_start_values()
            except Exception as exc:  # noqa: BLE001
                reason += f' (restoring the start values failed: {exc})'
                self._log(traceback.format_exc())
            with contextlib.suppress(Exception):
                self._finish_report()
            awake.hold(False)
            self._set_status(state, reason)
            self._log(f'{state}: {reason}')

    def _loop(self) -> str:
        while True:
            if self.cancel.is_set():
                raise SafeStop('Stopped by the operator.')
            decision = self.engine('map_next', self.journal)
            progress = decision.get('progress') or {}
            kind = decision['decision']
            if kind == 'done':
                return decision.get('reason', 'map complete')
            if kind == 'stop':
                raise SafeStop(decision.get('reason', 'stopped by the map engine'))
            request = decision['request']
            self._set_status('running', f'Line {request["id"]}: {request["stage"]} {request["role"]} '
                             f'{request["point"]} {request["variant"]}'.strip(), progress=progress, request=request)
            if kind == 'wait':
                result = self._wait(request)
            else:
                result = self._line(request)
            reply = self.engine('map_record', self.journal, result)
            self.journal = reply['journal']
            atomic_json(self.folder / JOURNAL, self.journal)
            self._store(request, result, reply.get('analysis') or {})
            self._log(f'line {request["id"]} {request["role"]} {request["point"]} {request["variant"]}: '
                      f'{result["outcome"]} {result.get("error", "")}'.strip())
            if result['outcome'] == 'failed':
                now = self.monotonic()
                self._failures = [t for t in self._failures if now - t < 3600.] + [now]
                if len(self._failures) >= self.FAILURES_PER_HOUR:
                    raise SafeStop(f'{len(self._failures)} failed lines within one hour; last: {result["error"]}')

    def _wait(self, request) -> dict:
        started = self.clock()
        deadline = self.monotonic() + float(request.get('seconds', 0.))
        while self.monotonic() < deadline:
            if self.cancel.wait(min(5., max(0., deadline - self.monotonic()))):
                raise SafeStop('Stopped by the operator.')
        return dict(id=request['id'], outcome='done', started=started, duration_s=self.clock() - started)

    def _line(self, request) -> dict:
        started = self.clock()
        result = dict(id=request['id'], outcome='done', error='', started=started)
        try:
            self._wait_for_spray()
            self.scan._gui(lambda: self._apply_static(request), cancel=False)
            self._wait_static(request)
            self.scan._gui(lambda: self.scan._configure_map_line(request, self.journal['plan']['map']), cancel=False)
            data = self._run_scan(request)
            status, error = data.pop('status'), data.pop('error')
            result.update(data)
            if status != 'completed':
                if self.cancel.is_set():
                    raise SafeStop('Stopped by the operator.')
                raise (LineUnreachable if self._unreachable(error) else LineFailed)(error or status)
            self._check_spray_during(data.get('spray') or [])
            result['error'] = ''
        except LineUnreachable as exc:
            result.update(outcome='unreachable', error=str(exc))
        except LineFailed as exc:
            result.update(outcome='failed', error=str(exc))
        finally:
            self.scan._gui(self.scan._end_map_line, cancel=False)
        result['duration_s'] = self.clock() - started
        for key in ('axis', 'signal', 'sigma', 'spray'):
            result[key] = finite(result.get(key) or [])
        return result

    @staticmethod
    def _unreachable(error) -> bool:
        text = str(error).lower()
        return any(marker in text for marker in UNREACHABLE_MARKERS)

    def _run_scan(self, request) -> dict:
        """Start the line like the Start button, wait for its file, read its arrays."""
        started = self.scan._gui(self.scan._start_map_line, cancel=False)
        if not started:
            message = self.scan._gui(lambda: str(getattr(self.scan, 'scan_status', '') or 'MScan did not start'),
                                     cancel=False)
            raise (LineUnreachable if self._unreachable(message) else LineFailed)(message)
        limit = 2. * float(request.get('expected_s', 600.)) + self.LINE_MARGIN_S
        begin = self.monotonic()
        stopped_for_time = False
        while not self.scan._gui(lambda: bool(self.scan.finished), cancel=False):
            if self.cancel.is_set() or (not stopped_for_time and self.monotonic() - begin > limit):
                stopped_for_time = not self.cancel.is_set()
                self.scan._gui(self.scan._stop_map_line, cancel=False)
                if self.monotonic() - begin > limit + 120.:
                    raise SafeStop('The line did not stop within two minutes after Stop.')
            time.sleep(self.POLL_S)
        data = self.scan._gui(self.scan._map_line_result, cancel=False)
        if stopped_for_time and data['status'] != 'completed':
            data['error'] = f'line slower than twice its expected {request.get("expected_s", 0):.0f} s'
        return data

    # ------------------------------------------------------------------ settings a line does not sweep

    def _capture_start_values(self):
        def capture():
            plan = self.journal['plan']['map']
            channels = plan['channels']
            names = [channels['q1_dc'], channels['stub_entry'], *plan.get('follow', {})]
            values = {name: float(self.scan._map_channel(name).value) for name in names}
            values['__frequency_khz__'] = self.scan._map_frequency_hz() / 1e3
            self._operator_settings = self.scan._map_settings_snapshot()
            return values
        if self.start_values:
            return
        saved = self.folder / START_VALUES
        if saved.is_file():
            # Resume after a crash: the values before the first line, not those a line left behind.
            stored = json.loads(saved.read_text(encoding='utf-8'))
            self.start_values, self._operator_settings = stored['channels'], stored['settings']
        else:
            self.start_values = self.scan._gui(capture, cancel=False)
            atomic_json(saved, dict(channels=self.start_values, settings=self._operator_settings))
        self.spray_reference = self.scan._gui(self.scan._map_spray_now, cancel=False)

    def _restore_start_values(self):
        if not self.start_values:
            return

        def restore():
            for name, value in self.start_values.items():
                if name == '__frequency_khz__':
                    continue
                channel = self.scan._map_channel(name)
                if not math.isclose(float(channel.value), value, abs_tol=1e-9):
                    channel.value = value
            frequency = self.start_values['__frequency_khz__'] * 1e3
            if abs(self.scan._map_frequency_hz() - frequency) > 1e-6 * frequency:
                self.scan._set_map_frequency(frequency)
            if self._operator_settings is not None:
                self.scan._map_settings_restore(self._operator_settings)
        self.scan._gui(restore, cancel=False)

    def _static_targets(self, request) -> dict:
        plan = self.journal['plan']['map']
        channels = plan['channels']
        targets = {channels['q1_dc']: float(request['q1_dc_v']), channels['stub_entry']: float(request['stub_v'])}
        for name, offset in (plan.get('follow') or {}).items():
            targets[name] = float(request['x_v']) + float(offset)
        return targets

    def _apply_static(self, request):
        for name, value in self._static_targets(request).items():
            channel = self.scan._map_channel(name)
            low, high = self.scan._offset_bounds(channel, channel.getDevice().controller)
            if not low <= value <= high:
                raise LineUnreachable(f'{name}: {value:g} V outside allowed {low:g}–{high:g} V')
            if not math.isclose(float(channel.value), value, abs_tol=1e-6):
                channel.value = round(value, 3)
        frequency = float(request['f_hz'])
        if abs(self.scan._map_frequency_hz() - frequency) > 1e-6 * frequency:
            self.scan._set_map_frequency(frequency)

    def _wait_static(self, request):
        """Every channel at its target (Monitor within the scan tolerance) and the AMX at its frequency."""
        targets = self._static_targets(request)
        frequency = float(request['f_hz'])
        deadline = self.monotonic() + self.STATIC_TIMEOUT_S

        def ready():
            tolerance = float(getattr(self.scan, 'voltage_tolerance', .5))
            waiting = []
            for name, value in targets.items():
                channel = self.scan._map_channel(name)
                if abs(float(channel.monitor) - value) > tolerance:
                    waiting.append(f'{name} at {float(channel.monitor):.2f} V for {value:g} V')
            try:
                actual = self.scan._map_frequency_hz()
            except Exception:  # noqa: BLE001 - outputs re-decoded after a write
                actual = math.nan
            if not abs(actual - frequency) <= 1e-6 * frequency:
                waiting.append(f'AMX at {actual / 1e3:.3f} kHz for {frequency / 1e3:.3f} kHz')
            return waiting
        while True:
            waiting = self.scan._gui(ready, cancel=False)
            if not waiting:
                return
            if self.cancel.is_set():
                raise SafeStop('Stopped by the operator.')
            if self.monotonic() > deadline:
                raise LineFailed('Settings before the line not reached: ' + '; '.join(waiting))
            time.sleep(self.POLL_S * 2)

    # ------------------------------------------------------------------ spray

    def _spray_window(self):
        if self.spray_reference is None or not math.isfinite(self.spray_reference) or self.spray_reference == 0:
            return None
        percent = float(self.journal['plan']['map'].get('spray_window_percent', 20.))
        return abs(self.spray_reference) * (1 - percent / 100), abs(self.spray_reference) * (1 + percent / 100)

    def _wait_for_spray(self):
        window = self._spray_window()
        if window is None:
            return
        deadline = self.monotonic() + self.SPRAY_WAIT_S
        while True:
            now = self.scan._gui(self.scan._map_spray_now, cancel=False)
            if now is not None and math.isfinite(now) and window[0] <= abs(now) <= window[1]:
                return
            self._set_status('paused', f'Spray current {now} A outside {window[0]:.3g}–{window[1]:.3g} A; waiting')
            if self.monotonic() > deadline:
                raise SafeStop(f'Spray current outside its window for {self.SPRAY_WAIT_S / 60:.0f} min')
            if self.cancel.wait(5.):
                raise SafeStop('Stopped by the operator.')

    def _check_spray_during(self, spray):
        window = self._spray_window()
        values = np.abs(np.asarray([v for v in spray if v is not None], dtype=float))
        values = values[np.isfinite(values)]
        if window is None or not len(values):
            return
        mean = float(np.mean(values))
        if not window[0] <= mean <= window[1]:
            raise LineFailed(f'spray current {mean:.3g} A left its window during the line')

    # ------------------------------------------------------------------ files

    def _set_status(self, state, message, **extra):
        self.status = dict(state=state, message=message, time=self.clock(), folder=str(self.folder or ''), **extra)
        with contextlib.suppress(Exception):
            self.scan._gui(lambda: self.scan._show_map_status(self.status), cancel=False)
        if self.folder is not None:
            with contextlib.suppress(OSError, TypeError, ValueError):
                atomic_json(self.folder / STATUS, self.status)

    def _log(self, text):
        if self.folder is None:
            return
        with contextlib.suppress(OSError), (self.folder / LOG).open('a', encoding='utf-8') as handle:
            handle.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text.rstrip() + '\n')

    def _store(self, request, result, analysis):
        """Every line in one HDF5 file of the map, linked to its own scan file."""
        try:
            import h5py
        except ImportError:
            return
        with contextlib.suppress(OSError, ValueError, TypeError), h5py.File(self.folder / STORE, 'a') as handle:
            name = f'L{int(request["id"]):04d}'
            if name in handle:
                del handle[name]
            group = handle.create_group(name)
            group.attrs['request'] = json.dumps(request, allow_nan=False)
            group.attrs['analysis'] = json.dumps(analysis, allow_nan=False)
            for key in ('outcome', 'error', 'file'):
                group.attrs[key] = str(result.get(key, ''))
            group.attrs['started'] = float(result.get('started') or 0.)
            for key in ('axis', 'signal', 'sigma', 'spray'):
                group.create_dataset(key, data=np.asarray([np.nan if v is None else v for v in result.get(key) or []],
                                                          dtype=float))

    def _finish_report(self):
        if self.folder is None or self.journal is None:
            return
        report = self.engine('map_report', self.journal)
        atomic_json(self.folder / REPORT, report)
        with contextlib.suppress(Exception):
            write_figures(report, self.folder / FIGURES)
        best = report.get('optimum')
        if best:
            with contextlib.suppress(Exception):
                self.scan._gui(lambda: self.scan._save_map_best_settings(best, self.folder / BEST_SETTINGS),
                               cancel=False)


def write_figures(report, path):
    """Convergence, 1/R in k, R_cap in N, signal in E and f, predicted against measured, Pareto front."""
    import matplotlib
    matplotlib.use('Agg', force=False)
    from matplotlib.figure import Figure
    fig = Figure(figsize=(13, 9.5), layout='constrained')
    axes = fig.subplots(2, 3).ravel()
    points = [p for p in report.get('points', []) if p.get('ions')]
    ax = axes[0]
    conv = report.get('convergence', [])
    ax.plot(range(1, len(conv) + 1), [c['merit'] * 1e12 for c in conv], 'o', label='each point')
    ax.plot(range(1, len(conv) + 1), [c['best'] * 1e12 for c in conv], '-', label='best so far')
    ax.set(title='Convergence', xlabel='points measured', ylabel='Σ S at R_target (pA)')
    ax.legend(fontsize='small')
    ax = axes[1]
    for p in points[:8]:
        for ion in p['ions']:
            k = [t[0] for t in ion['table'] if t[1] > 0]
            inv = [1 / t[1] for t in ion['table'] if t[1] > 0]
            if k:
                ax.plot(k, inv, '.-', linewidth=.6, markersize=3)
    ax.set(title='1/R along the ladders', xlabel='k', ylabel='1/R')
    ax = axes[2]
    caps = (report.get('laws') or {}).get('cap', {}).get('points', [])
    finite_caps = [c for c in caps if 0 < c[1] < 1e5]
    if finite_caps:
        ax.loglog([c[0] for c in finite_caps], [c[1] for c in finite_caps], 'o')
    law = (report.get('laws') or {}).get('cap', {})
    if law.get('valid') and not law.get('none') and law.get('a'):
        n = np.geomspace(5, 200, 50)
        ax.loglog(n, law['a'] * n ** law['b'], '-', label=f"R_cap = {law['a']:.3g} N^{law['b']:.2f}")
        ax.legend(fontsize='small')
    ax.set(title='R cap against RF cycles', xlabel='N', ylabel='R_cap')
    ax = axes[3]
    amp = [p for p in points if p.get('mode') == 'amplitude' and p.get('merit', 0) > 0]
    if amp:
        sc = ax.scatter([p['f_hz'] / 1e3 for p in amp], [p['energy_ev'] for p in amp],
                        c=[p['merit'] * 1e12 for p in amp], s=60)
        fig.colorbar(sc, ax=ax, label='Σ S (pA)')
    optimum = report.get('optimum')
    if optimum:
        ax.plot([optimum['f_hz'] / 1e3], [optimum['energy_ev']], 'r*', markersize=14, label='best')
        ax.legend(fontsize='small')
    ax.set(title='Merit in (f, E)', xlabel='f (kHz)', ylabel='E (eV)', yscale='log')
    ax = axes[4]
    val = report.get('validation', [])
    if val:
        ax.plot([v['predicted'] * 1e12 for v in val], [v['measured'] * 1e12 for v in val], 'o')
        top = max([v['predicted'] * 1e12 for v in val] + [v['measured'] * 1e12 for v in val] + [1e-3])
        ax.plot([0, top], [0, top], 'k--', linewidth=.8)
    ax.set(title=f"Out-of-sample check ({report.get('validation_passed', 0)} of {len(val)} within noise)",
           xlabel='predicted Σ S (pA)', ylabel='measured Σ S (pA)')
    ax = axes[5]
    front = report.get('pareto', [])
    if front:
        ax.plot([f['min_r'] for f in front], [f['merit'] * 1e12 for f in front], 'o-')
    ax.axvline(report.get('target', 40), color='k', linewidth=.8, linestyle='--')
    ax.set(title='Pareto front', xlabel='lowest R of the range', ylabel='Σ S (pA)')
    fig.suptitle(f"Q2 map {report.get('name', '')}: {len(report.get('points', []))} points, "
                 f"{report.get('lines', 0)} lines", fontsize=12)
    fig.savefig(path, dpi=110)
