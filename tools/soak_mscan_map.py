#!/usr/bin/env python3
"""Soak test of the MScan map: the runner and the deployed MScan worker on a simulated Q2, in real time.

Section 9 of docs/quadrupole_optimization_plan.md: before the first full map on the instrument, run the
whole plan for at least as long as the longest map planned, on the lab PC, and check that memory,
handles and the runner's timing stay flat. No Explorer and no hardware: a fake MScan answers each
line with a spectrum of the tuning mix after the line's expected duration divided by ``--speed``.

    python3 tools/soak_mscan_map.py mscan/plans/q2_full.toml --speed 1 --out soak
    python3 tools/soak_mscan_map.py mscan/plans/q2_P1_2h.toml --speed 60   # a quick rehearsal

Faults are injected (``--fail-every``): a failed line every n lines, which the map measures again.
``soak.csv`` in the output folder gets one row per line: time, line, outcome, the process memory
(RSS) of this Python process and the number of its threads and open files when available.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import math
import os
from pathlib import Path
import sys
import threading
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MIX = {322.05: .6, 622.03: 1., 922.01: .9, 1521.97: .7, 2121.93: .5, 2721.89: .3}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _band_center(k):
    """Centre of the stable q band of the 50 % square wave on a = 2kq (coarse, enough for a soak test)."""
    q = np.linspace(1e-4, .8, 4001)
    a = 2 * k * q

    def trace(a, q):
        w1, w2 = np.sqrt((a + 2 * q).astype(complex)) + 1e-16, np.sqrt((a - 2 * q).astype(complex)) + 1e-16
        h = np.pi / 2
        return (2 * np.cos(w1 * h) * np.cos(w2 * h) - (w1 / w2 + w2 / w1) * np.sin(w1 * h) * np.sin(w2 * h)).real
    stable = (np.abs(trace(a, q)) < 2) & (np.abs(trace(-a, -q)) < 2)
    return (q[stable][0] + q[stable][-1]) / 2 if stable.any() else math.nan


def spectrum(request, rng):
    """Currents (A) of the mix along the line: an ideal filter capped at R = 45, c = 0.978."""
    axis = np.asarray(request['axis'], dtype=float)
    noise = 2e-15 * rng.standard_normal(len(axis))
    if request['kind'] == 'offset':
        return 1e-12 * .5 * np.array([math.erfc((x - (request['q1_dc_v'] - .3)) / .85) for x in axis]) - 3e-13 + noise
    if request['kind'] == 'extra':
        optimum = request['x_v'] + .42 * (34 - request['x_v'])
        return 1e-12 * np.clip(1 - .1 * ((axis - optimum) / 5) ** 2, .3, 1) - 3e-13 + noise
    if request['kind'] == 'frequency':
        return np.full(len(axis), -3e-13) + noise
    k_eff = (request['k'] * axis - request['delta_v']) / (.978 * axis)
    q = _band_center(float(np.median(k_eff)))
    scale = 4 * 1.602176634e-19 / 1.66053906660e-27 / ((2.625e-3) ** 2 * (2 * np.pi * request['f_hz']) ** 2)
    mz = scale * .978 * axis / q
    ideal = np.where(k_eff < .2153, .157 / np.maximum(.2153 - k_eff, 1e-9), np.inf)
    resolution = 1 / (1 / ideal + 1 / 45)
    transmission = np.clip((.2153 - k_eff) / (.2153 - .2 / .978), 0, 3)
    y = np.full(len(axis), -3e-13)
    for m, abundance in MIX.items():
        y += np.nan_to_num(2e-12 * abundance * transmission * np.exp(-4 * math.log(2) * ((mz - m) / (m / resolution)) ** 2))
    return y + noise


class Channel:
    def __init__(self, name, value):
        self.name, self.value, self.monitor = name, value, value

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        if key == 'value':
            super().__setattr__('monitor', value)

    def getDevice(self):
        return type('Device', (), {'controller': None, 'name': 'AMPR'})()


class SimulatedMScan:
    FOLDER = 'MScan'
    voltage_tolerance = .5

    def __init__(self, speed, fail_every, seed=1):
        self.channels = {n: Channel(n, v) for n, v in (('Q1_DC', 36.), ('Ap_Q1_Q2', 34.), ('Stub_Entry_Q2', 20.),
                                                       ('Stub_Exit_Q2', -5.))}
        self.frequency, self.finished, self.line, self.result = 500e3, True, None, None
        self.speed, self.fail_every, self.count = float(speed), int(fail_every), 0
        self.rng = np.random.default_rng(seed)
        self.settings = {}
        self.stop_event = threading.Event()

    def _gui(self, action, cancel=True):
        return action()

    def _map_instrument(self, config, data_path):
        return dict(q1_dc_v=36., ap_q1_q2_v=34., x_v=10., ilim_a=.5, psu_max_v=350., full_range=True,
                    pair_min_v=-200., pair_max_v=200., stub_min_v=-200., stub_max_v=200., q1_max_v=200.,
                    clock_hz=100e6, f_hz=self.frequency, frequency_writable=True, free_disk_bytes=1e12)

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

    def _start_map_line(self):
        self.count += 1
        self.finished = False
        threading.Thread(target=self._measure, args=(self.line, self.count), daemon=True).start()
        return True

    def _measure(self, request, count):
        duration = float(request.get('expected_s', 60.)) / self.speed
        self.stop_event.wait(duration)
        axis = list(request['axis'])
        if self.fail_every and count % self.fail_every == 0:
            self.result = dict(status='error', error='simulated spray drop-out', axis=axis,
                               signal=[math.nan] * len(axis), sigma=[math.nan] * len(axis), spray=[], file='')
        else:
            self.result = dict(status='completed', error='', axis=axis, signal=list(spectrum(request, self.rng)),
                               sigma=[2e-15] * len(axis), spray=[45e-9] * len(axis), file='')
        self.finished = True

    def _stop_map_line(self):
        self.stop_event.set()
        self.stop_event = threading.Event()

    def _end_map_line(self):
        self.line = None

    def _map_line_result(self):
        return dict(self.result)

    def _show_map_status(self, status):
        print(time.strftime('%H:%M:%S'), status.get('state'), status.get('message'), flush=True)

    def _save_map_best_settings(self, best, path):
        Path(path).write_text(repr(best))


def usage():
    row = dict(rss_mb=math.nan, threads=threading.active_count(), files=math.nan)
    try:
        import psutil
        process = psutil.Process(os.getpid())
        row.update(rss_mb=process.memory_info().rss / 1e6, files=len(process.open_files()))
    except Exception:  # noqa: BLE001 - psutil is optional
        try:
            import resource
            row['rss_mb'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e3
        except Exception:  # noqa: BLE001
            pass
    return row


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('plan', type=Path)
    parser.add_argument('--speed', type=float, default=1., help='1 = real time; 60 = a minute per hour')
    parser.add_argument('--fail-every', type=int, default=17)
    parser.add_argument('--out', type=Path, default=Path('soak'))
    args = parser.parse_args(argv)
    runner_module = _load('_soak_map_runner', ROOT / 'mscan/_runtime/_map_runner.py')
    transport = _load('_soak_native_worker', ROOT / 'mscan/_runtime/_native_worker.py')
    args.out.mkdir(parents=True, exist_ok=True)

    def engine(method, *params):
        worker = transport.NativeWorkerProxy(ROOT / 'mscan', 'mscan', {}, log_dir=args.out / 'logs')
        try:
            return worker.call_method(method, *params, rpc_timeout_s=900.)
        finally:
            worker.close(grace_s=0)
    scan = SimulatedMScan(args.speed, args.fail_every)
    # Accelerated runs keep the runner's own time in simulated seconds (watchdogs, failures per hour).
    start = time.time()
    runner = runner_module.MapRunner(scan, engine, data_path=args.out,
                                     clock=lambda: start + (time.time() - start) * args.speed,
                                     monotonic=lambda: time.monotonic() * args.speed)
    folder = runner.start(args.plan)
    with (args.out / 'soak.csv').open('w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=['time', 'lines', 'state', 'rss_mb', 'threads', 'files'])
        writer.writeheader()
        while runner.running:
            journal = runner.journal or {}
            writer.writerow(dict(time=time.time(), lines=len(journal.get('records', [])),
                                 state=runner.status.get('state'), **usage()))
            handle.flush()
            time.sleep(max(1., 60. / args.speed))
    print('Map folder:', folder, 'final state:', runner.status.get('state'), runner.status.get('message'))
    return 0 if runner.status.get('state') == 'done' else 1


if __name__ == '__main__':
    raise SystemExit(main())
