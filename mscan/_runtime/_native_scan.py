# SPDX-License-Identifier: GPL-2.0-or-later
# Based on mscan/mscan_plugin.py, Copyright (C) 2021-2026 Tim Esser.
"""Privately loaded Explorer adapter for the native MScan action/observation engine.

The entrypoint loads this module privately. The caller first runs the existing
``MScan.initScan`` preflight, then constructs ``NativeScanAdapter(scan, rpc)`` and
calls ``run`` from the scan thread. ``rpc(method, *args)`` is the supervised native
worker's ``call_method`` function; process startup/reaping belong to that supervisor.
For a raw JSON function such as ``call_json``, pass ``raw_json=True``.

All Explorer channel access, including history copies, runs through ``scan._gui``. The
existing ``_command`` is a conservative, GUI-time pre-write guard and the only
channel-write path; scan timing, window selection, averaging, progress, stop and
successful restoration decisions are native. No device plugin is imported.
"""
from __future__ import annotations

import copy
import math
import time
from threading import Event


def _encode(value):
    """Encode non-finite telemetry for an explicitly raw-JSON RPC."""
    if isinstance(value, float) and not math.isfinite(value):
        return {'$float': 'nan' if math.isnan(value) else 'inf' if value > 0 else '-inf'}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {'$float'}:
            return float(value['$float'])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _identity(*objects):
    return ':'.join(str(id(obj)) for obj in objects)


def _busy(controller, fields):
    return any(bool(getattr(controller, field, False)) for field in fields)


_PSU_BUSY = ('initializing', 'transitioning', '_manual_apply_active',
             '_manual_apply_worker_running', '_hv_config_loading')
_AMPR_BUSY = ('initializing', 'transitioning', 'ramping')


class NativeScanAdapter:
    """An explicit, single-scan adapter; it never restarts/retries a worker write.

    ``prepare`` and ``start`` return native plan/status dictionaries. ``step``
    executes at most one pending action and consumes its acknowledgement.
    ``run`` drains to a terminal state, updates the existing data model and
    returns the paginated result assembled into the reference validation shape.
    Production sets ``emit_completion=False`` so its owner notifies Explorer
    only after closing and reaping the worker.
    ``cancel`` is thread-safe and only sets the existing scan cancellation Event;
    the scan thread subsequently notifies native without sending voltage writes.
    """

    def __init__(self, scan, rpc, *, raw_json=False, emit_completion=True):
        if not callable(rpc):
            raise TypeError('rpc must be callable as rpc(method, *args)')
        self.scan, self.rpc = scan, rpc
        self._raw_json = bool(raw_json)
        self._emit_completion = bool(emit_completion)
        self._sequence = 0
        self._status = None
        self._executed = set()
        self._running = Event()
        self._origin_plan = scan._plan
        self._cancel_token = scan._cancel
        self._plan = None

    def _is_current(self):
        return self.scan._plan is self._origin_plan and self.scan._cancel is self._cancel_token

    def _require_current(self, phase):
        if self.scan._plan is not self._origin_plan:
            raise RuntimeError('MScan plan was replaced ' + phase)
        if self.scan._cancel is not self._cancel_token:
            raise RuntimeError('MScan cancellation session was replaced ' + phase)

    def _call(self, method, *args):
        payload = tuple(_encode(arg) for arg in args) if self._raw_json else args
        return _decode(self.rpc(method, *payload))

    def _settings(self):
        self._require_current('before native settings')
        scan = self.scan
        return dict(start=float(scan.start), stop=float(scan.stop),
                    step=float(getattr(scan, 'step', math.nan)),
                    mode=getattr(scan, 'scan_mode', scan.STEPPED),
                    settling_s=float(scan.settling_s), settle_timeout=float(scan.settle_timeout),
                    voltage_tolerance=float(scan.voltage_tolerance),
                    integration_s=float(getattr(scan, 'integration_s', math.nan)),
                    time_step_s=float(getattr(scan, 'time_step_s', math.nan)),
                    sweep_rate=float(getattr(scan, 'sweep_rate', math.nan)),
                    offset_factor=float(getattr(scan, 'offset_factor', math.nan)),
                    metadata=copy.deepcopy(self._plan.get('metadata', {})))

    def _bind(self):
        if self._running.is_set():
            raise RuntimeError('Native MScan is already active')
        self._require_current('before native binding')
        if self._origin_plan is None:
            raise RuntimeError('MScan.initScan/preflight must succeed before native start')
        self._plan = self._origin_plan

    def _registered(self, channel):
        try:
            self.scan._registered(channel)
            return True
        except Exception:
            return False

    def _source_identity(self, channel, device, controller, token):
        # Names/addresses are values, not object identities. Include both so a
        # rename, replacement, controller reconnect or cancellation-token swap
        # changes the native frozen identity even if all readings look valid.
        return (_identity(channel, device, controller, controller.device, token)
                + ':' + channel.name + ':' + device.name)

    def _snapshot_on_gui(self):
        self._require_current('during native acquisition')
        scan, plan = self.scan, self._plan
        if plan is None or scan._plan is not plan:
            raise RuntimeError('MScan plan was replaced during native acquisition')
        amx = plan['amx']
        controller = amx.controller
        # Resolve current registrations, rather than trusting only stale object
        # references retained by the plan. The reference helper never writes.
        amx_registered = scan._plugin(amx.name) is amx
        selected = list(plan['selected'])
        links = [[key, str(getattr(amx, key))] for key, _ in plan['links']]
        rows = controller.output_rows
        chosen = [rows[i] for i in selected] if rows and len(rows) == 4 else []
        amx_data = dict(identity=_identity(amx, controller, controller.device) + ':' + amx.name,
                        initialized=bool(controller.initialized and controller.device is not None and amx_registered),
                        on=bool(amx.isOn()), busy=_busy(controller, ('initializing', 'transitioning')),
                        selected=selected, links=links, rows=copy.deepcopy(chosen))
        rails = []
        for rail in plan['rails']:
            channel, device = rail['channel'], rail['device']
            ctrl = device.controller
            token = ctrl._output_cancel
            registered = self._registered(channel) and scan._plugin(device.name) is device
            rails.append(dict(
                identity=self._source_identity(channel, device, ctrl, token),
                device_identity=_identity(device), name=channel.name, number=channel.channel_number(),
                registered=registered, real=bool(channel.real), enabled=bool(channel.enabled),
                active=bool(channel.active), initialized=bool(channel.initialized and ctrl.initialized and ctrl.device is not None),
                on=bool(device.isOn()), busy=_busy(ctrl, _PSU_BUSY), shutdown=bool(token.is_set()),
                unit=channel.unit, use_monitors=bool(channel.useMonitors),
                readback_status=str(channel.readback_status), revision=int(channel.voltage_request_revision),
                value=float(channel.value), monitor=float(channel.monitor), min=float(channel.min), max=float(channel.max),
                hardware_voltage_limit=float(channel.hardware_voltage_limit),
                hardware_current_limit=float(channel.hardware_current_limit),
                current_limit_readback=float(channel.current_limit_readback),
                voltage_setpoint_readback=float(channel.voltage_setpoint_readback),
                current_readback=float(channel.current_readback)))
        if len(plan['detectors']) != 1:
            raise RuntimeError('Native MScan requires exactly one selected DMMR module')
        source = plan['detectors'][0]
        device = source.getDevice()
        _, since = scan._cadence_epoch(device)
        detector = dict(identity=_identity(source, device) + ':' + source.name + ':' + device.name,
                        name=source.name, device_name=device.name, module=source.module_address(),
                        registered=self._registered(source), real=bool(source.real), unit=source.unit,
                        enabled=bool(source.enabled), initialized=bool(source.initialized),
                        acquiring=bool(source.acquiring), recording=bool(device.recording),
                        history_id=_identity(source.values, device.time), interval_ms=float(device.interval),
                        cadence_since=since if math.isfinite(since) else None,
                        times=[float(v) for v in device.time.get()],
                        values=[float(v) for v in source.getValues(subtractBackground=False)])
        offset = None
        if plan.get('offset') is not None:
            o = plan['offset']
            channel, device = o['channel'], o['device']
            ctrl = device.controller
            low, high = scan._offset_bounds(channel, ctrl)
            state, _ = scan._offset_request_state(o)
            offset = dict(identity=self._source_identity(channel, device, ctrl, ctrl._setpoint_cancel)
                          + ':' + str(channel.module_address()) + ':' + str(channel.channel_number()),
                          name=channel.name, registered=self._registered(channel) and scan._plugin(device.name) is device,
                          real=bool(channel.real), enabled=bool(channel.enabled), active=bool(channel.active),
                          initialized=bool(ctrl.initialized and ctrl.device is not None), on=bool(device.isOn()),
                          busy=_busy(ctrl, _AMPR_BUSY), shutdown=bool(ctrl._setpoint_cancel.is_set()),
                          state=state, value=float(channel.value), monitor=float(channel.monitor), min=low, max=high)
        self._sequence += 1
        return dict(sequence=self._sequence, wall=time.time(), mono=time.monotonic(), amx=amx_data,
                    rails=rails, detector=detector, offset=offset,
                    other_scan_running=any(p is not scan and hasattr(p, 'finished') and not p.finished
                                           and hasattr(p, 'runScan') for p in scan.pluginManager.plugins))

    def snapshot(self, *, cancellable=True):
        return self.scan._gui(self._snapshot_on_gui, cancel=cancellable)

    def prepare(self):
        self._bind()
        settings, observation = self.scan._gui(lambda: (self._settings(), self._snapshot_on_gui()))
        return self._call('prepare', settings, observation)

    def start(self):
        self._bind()
        if self._cancel_token.is_set():
            raise RuntimeError('Scan stopped before native start')
        settings, observation = self.scan._gui(lambda: (self._settings(), self._snapshot_on_gui()))
        prepared = self._call('prepare', settings, observation)
        self.scan._gui(lambda: self._validate_data_model(prepared['steps'], observation))
        self._status = self._call('start', settings, observation)
        self._running.set()
        self._apply_updates(self._status)
        return self._status

    def _validate_data_model(self, steps, observation):
        self._require_current('before native data validation')
        scan = self.scan
        if len(scan.inputChannels) != 1 or len(scan.outputChannels) != 1:
            raise RuntimeError('MScan requires one amplitude axis and one DMMR output')
        actual = scan.inputChannels[0].recordingData
        if len(actual) != len(steps) or any(not math.isclose(float(a), b, rel_tol=1e-12, abs_tol=1e-12)
                                          for a, b in zip(actual, steps)):
            raise RuntimeError('Explorer amplitude axis differs from the native plan; repeat initScan')
        if scan.outputChannels[0].name != observation['detector']['name']:
            raise RuntimeError('Explorer output channel differs from the selected DMMR module')
        keys = ['point_status', 'rail_v', 'rail_i', 'window_start', 'window_end', 'samples', 'finite_samples']
        if observation['offset'] is not None:
            keys += ['offset_v', 'offset_target']
        if len(scan.outputChannels[0].recordingData) != len(steps) or any(len(scan._validation[key]) != len(steps) for key in keys):
            raise RuntimeError('Explorer validation/data arrays differ from the native point count')
        if any(len(row) != len(observation['rails']) for key in ('rail_v', 'rail_i') for row in scan._validation[key]):
            raise RuntimeError('Explorer voltage/current arrays differ from the native rail count')
        if any(len(row) != 1 for key in ('samples', 'finite_samples') for row in scan._validation[key]):
            raise RuntimeError('Explorer sample arrays differ from the native detector count')

    def cancel(self):
        self._cancel_token.set()

    close = cancel

    def reset(self):
        if self._running.is_set():
            raise RuntimeError('Stop the native scan before reset')
        result = self._call('reset')
        self._status = None
        self._executed.clear()
        self._plan = None
        self._continuous_begin = None
        return result

    def _command_on_gui(self, action):
        self._require_current('before the queued action')
        key = (action['session'], action['action_id'])
        if key in self._executed:
            raise RuntimeError('Duplicate native MScan action; voltage writes are not retried')
        if self._cancel_token.is_set():
            raise RuntimeError('Scan stopped before the queued action')
        if self.scan._plan is not self._plan:
            raise RuntimeError('Scan plan was replaced before the queued action')
        revisions = [int(r['channel'].voltage_request_revision) for r in self._plan['rails']]
        expected = action['expected_revisions']
        if len(revisions) != len(expected):
            raise RuntimeError('Native PSU request revisions differ from the scan rail count')
        for rail, revision, target_revision in zip(self._plan['rails'], revisions, expected):
            if revision != target_revision:
                raise RuntimeError(f'{rail["name"]}: setpoint changed outside the scan '
                                   '(request revision before the queued action)')
        status = self._status
        if status is None or status['session'] != action['session']:
            raise RuntimeError('Stale native MScan action session')
        confirmations, acknowledged = status['confirmed_vset'], status['revisions']
        if len(confirmations) != len(self._plan['rails']) or len(acknowledged) != len(self._plan['rails']):
            raise RuntimeError('Native PSU confirmations differ from the scan rail count')
        # The GUI prewrite guard must retain the echo already verified by Rust.
        # Never turn a later hardware change into a first confirmation.
        for rail, revision, confirmed in zip(self._plan['rails'], acknowledged, confirmations):
            if revision == rail['request_revision'] and confirmed is not None:
                rail['confirmed_vset'] = confirmed
        self._executed.add(key)
        # _command validates all current sources/limits/interlocks before the
        # first write, on Qt, and checks latest immediately before that write.
        # In continuous mode cadence is also revalidated after queueing delay.
        if self._plan['mode'] == self.scan.CONTINUOUS and not action['restore']:
            window_start = getattr(self, '_continuous_begin', None) if action['index'] > 0 else None
            self.scan._require_time_step(self._plan['detectors'][0], self._plan['average'], window_start)
        started, ended = self.scan._command(action['targets'], latest=action['latest_mono'], offset=action['offset'])
        end_mono = time.monotonic()
        if self._plan['mode'] == self.scan.CONTINUOUS and not action['restore']:
            self._continuous_begin = started
        o = self._plan.get('offset')
        return dict(session=action['session'], action_id=action['action_id'], start_wall=started,
                    end_wall=ended, end_mono=end_mono,
                    revisions=[int(r['channel'].voltage_request_revision) for r in self._plan['rails']],
                    values=[float(r['channel'].value) for r in self._plan['rails']],
                    offset_value=float(o['channel'].value) if o is not None else None)

    def _apply_updates(self, status):
        def apply():
            scan = self.scan
            self._require_current('before native updates')
            if scan._plan is not self._plan:
                raise RuntimeError('MScan plan was replaced before native updates')
            validation = scan._validation
            validation['status'], validation['error'] = status['status'], status.get('error', '')
            for point in status.get('updates', []):
                index = point['index']
                scan.outputChannels[0].recordingData[index] = point['mean']
                for key in ('rail_v', 'rail_i', 'window_start', 'window_end'):
                    validation[key][index] = point[key]
                validation['samples'][index, 0] = point['samples']
                validation['finite_samples'][index, 0] = point['finite_samples']
                validation['point_status'][index] = point['status']
                if point.get('offset') is not None:
                    validation['offset_v'][index], validation['offset_target'][index] = point['offset']
            scan.scan_status = status['state'].replace('_', ' ').capitalize()
            if status.get('error'):
                scan.scan_status += ': ' + status['error']
            if status.get('updates'):
                scan.signalComm.scanUpdateSignal.emit(False)
        self.scan._gui(apply, cancel=False)

    def step(self):
        self._require_current('before native progress')
        if not self._running.is_set():
            raise RuntimeError('Native scan is not running')
        if self._cancel_token.is_set():
            try:
                observation = self.snapshot(cancellable=False)
            except Exception:
                observation = None
            self._status = self._call('cancel', 'Scan stopped.', observation)
        else:
            action = self._status['action']
            if action is None:
                self._running.clear()
                return self._status
            ack = None
            if action['kind'] == 'command':
                try:
                    ack = self.scan._gui(lambda: self._command_on_gui(action))
                except Exception as exc:
                    method = 'cancel' if self._cancel_token.is_set() else 'action_failed'
                    args = ('Scan stopped.',) if method == 'cancel' else (action['session'], action['action_id'], str(exc))
                    self._status = self._call(method, *args)
                    self._apply_updates(self._status)
                    self._running.clear()
                    return self._status
            elif action['kind'] == 'wait':
                try:
                    self.scan._pause(max(0.0, min(float(action['seconds']), self.scan.POLL_S)))
                except Exception:
                    if not self._cancel_token.is_set():
                        raise
                if self._cancel_token.is_set():
                    return self.step()
            else:
                raise RuntimeError('Unknown native MScan action: ' + str(action['kind']))
            try:
                self._status = self._call('advance', self.snapshot(), ack)
            except Exception:
                # A native semantic fault persists its status. A transport fault
                # is not retried: the supervising caller must retire the worker.
                self._running.clear()
                raise
        self._apply_updates(self._status)
        if self._status['state'] == 'finished':
            self._running.clear()
        return self._status

    def export_result(self, page_size=1000):
        if self._running.is_set():
            raise RuntimeError('Complete or stop the scan before exporting its archived data')
        if not isinstance(page_size, int) or isinstance(page_size, bool) or not 1 <= page_size <= 4096:
            raise ValueError('page_size must be an integer in 1..=4096')
        first = self._call('result', 0, page_size)
        result = dict(first)
        result['validation'] = dict(first['validation'])
        for start in range(page_size, first['total'], page_size):
            page = self._call('result', start, page_size)
            if page['session'] != first['session'] or page['total'] != first['total']:
                raise RuntimeError('Native result session changed during export')
            result['steps'].extend(page['steps'])
            result['data'].extend(page['data'])
            for key, value in page['validation'].items():
                if isinstance(value, list):
                    result['validation'][key].extend(value)
        if first['continuous']:
            raw = self._call('raw', 0, page_size)
            data = raw['data']
            for start in range(page_size, max(raw['lengths'].values(), default=0), page_size):
                page = self._call('raw', start, page_size)
                if page['lengths'] != raw['lengths']:
                    raise RuntimeError('Native raw data changed during export')
                for key, value in page['data'].items():
                    data[key].extend(value)
            result['validation']['continuous'] = data
        result['count'] = first['total']
        return result

    def run(self):
        """Run once; faults never restore, switch HV OFF, or restart the worker."""
        error = None
        try:
            if not self._running.is_set():
                self.start()
            while self._running.is_set():
                self.step()
        except Exception as exc:
            error = exc
            try:
                self._call('cancel', 'Scan stopped.') if self._cancel_token.is_set() else self._call('abort', str(exc))
            except Exception:
                pass  # a poisoned supervisor refuses RPCs; writes are never retried
        finally:
            self._running.clear()
        if error is not None:
            # Fetching persisted scientific data is permitted after a *semantic*
            # fault. A poisoned transport refuses these reads; propagate that
            # original error and let the supervisor handle process recovery.
            try:
                result = self.export_result()
            except Exception:
                self._finish_error(error)
                raise error
            if result['validation']['status'] == 'running':
                self._finish_error(error)
                raise error
        else:
            try:
                result = self.export_result()
            except Exception as exc:
                self._finish_error(exc)
                raise
        def finish():
            self._require_current('before native completion')
            import numpy as np
            self.scan.outputChannels[0].recordingData[:] = result['data']
            validation = result['validation']
            for key, value in list(validation.items()):
                if isinstance(value, list) and key != 'point_status':
                    validation[key] = np.asarray(value)
            if 'continuous' in validation:
                for key, value in validation['continuous'].items():
                    validation['continuous'][key] = np.asarray(value)
            self.scan._validation = validation
            self.scan._plan['metadata'] = result['metadata']
            self.scan.scan_status = validation['status'].capitalize()
            if validation['error']:
                self.scan.scan_status += ': ' + validation['error']
            if self._emit_completion:
                self.scan.signalComm.updateRecordingSignal.emit(False)
                self.scan.signalComm.scanUpdateSignal.emit(True)
        self.scan._gui(finish, cancel=False)
        return result

    def _finish_error(self, error):
        def finish():
            if not self._is_current():
                return False
            scan = self.scan
            validation = scan._validation
            validation['status'] = 'stopped' if self._cancel_token.is_set() else 'error'
            validation['error'] = str(error)
            for index, status in enumerate(validation['point_status']):
                if status == 'not acquired':
                    validation['point_status'][index] = validation['status']
                    break
            scan.scan_status = validation['status'].capitalize() + ': ' + str(error)
            if self._emit_completion:
                scan.signalComm.updateRecordingSignal.emit(False)
                scan.signalComm.scanUpdateSignal.emit(True)
            return True
        try:
            return self.scan._gui(finish, cancel=False)
        except Exception:
            # The original worker fault remains primary if Qt cannot publish it.
            return False
