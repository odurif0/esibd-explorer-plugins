"""Transmission facade for an injected, independently supervised native worker.

The worker owns plans, search, scoring and recovery. This module executes only
its explicit Instrument actions, through the existing Explorer adapter (or the
simulator). It does not import device plugins, open DLLs or retry native requests.
Use ``Optimizer(config, instrument, worker=proxy)``; ``proxy.call(name, *args)``
must return decoded values or raise on connection/protocol/deadline failure.
The parent owns worker creation, integrity checking, shutdown and reaping.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import math
from threading import Event, Lock
import traceback

from . import _engine

ConfigError = _engine.ConfigError
Stopped = _engine.Stopped
SoftInterlock = _engine.SoftInterlock
InstrumentError = _engine.InstrumentError


def export_config(config, *, stages=None, strategy=None):
    """Export validated Python settings; preserve the first channel of grouped knobs."""
    selected = [config.stage(name).name for name in stages] if stages else None
    options = {name: getattr(config, name) for name in config.DEFAULTS}
    options["strategy"] = strategy or config.strategy
    target = config.target
    return dict(
        options=options, pressure={name: list(pair) for name, pair in config.pressure.items()},
        stages=[dict(name=s.name, aperture=s.aperture, downstream=list(s.downstream),
                     normalize_by=list(s.normalize_by), budget=s.budget, peak=s.peak,
                     knobs=[dict(name=k.name, channels=dict(k.channels), window=list(k.window),
                                 max_step=k.max_step, relative=k.relative, floor=k.floor,
                                 scale_channel=next(iter(k.channels))) for k in s.knobs]) for s in config.stages],
        selected_stages=selected,
        target=None if target is None else dict(
            channels=dict(target.channels), center=target.center, width=target.width,
            measure=list(target.measure), points=target.points, track=target.track,
            max_step=target.max_step, spectrum_points=target.spectrum_points,
            spectrum_span=target.spectrum_span, filter_name=target.filter_name,
            amplitude_channel=next(iter(target.channels))),
    )


def _plain(value):
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)) or hasattr(value, "tolist"):
        return [_plain(item) for item in (value.tolist() if hasattr(value, "tolist") else value)]
    if hasattr(value, "item"):
        return _plain(value.item())
    return value


def _decode(value):
    if isinstance(value, dict):
        if set(value) == {"$float"}:
            return {"nan": math.nan, "inf": math.inf, "-inf": -math.inf}[value["$float"]]
        if set(value) == {"$tuple"}:
            return tuple(_decode(item) for item in value["$tuple"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_decode(item) for item in value)
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


class Optimizer:
    """Blocking public scan facade; run it on the existing scan thread, never Qt."""

    MAX_ACTIONS = 2_000_000

    def __init__(self, config, instrument, *, worker, stages=None, strategy=None,
                 on_event=None, log=None, configure=True):
        self.config, self.instrument, self.worker = config, instrument, worker
        self.stages = [config.stage(name) for name in stages] if stages else list(config.stages)
        self.strategy_name = strategy or config.strategy
        self.on_event = on_event or (lambda kind, data: None)
        self.log = log
        self.history, self.results = [], []
        self.initial, self.commanded, self.max_step = {}, {}, {}
        self.status, self.reason = "ready", ""
        self.peak_center = config.target.center if config.target is not None else None
        self.spectra, self.checks = {}, {}
        self.counts = dict(steps=0, measures=0, empty_windows=0)
        self._stop = Event()
        self._lock = Lock()
        self._failed = self._closed = False
        self._active = self._measuring = False
        self._last_action_id = 0
        self._fault_traceback = None
        self._selected = stages
        if configure:
            self._update(self._rpc("configure", export_config(config, stages=stages, strategy=strategy)))

    def _rpc(self, method, *args):
        try:
            return self.worker.call(method, *args)
        except Exception as exc:
            kind = getattr(exc, "native_error_kind", None)
            if kind in ("ConfigError", "Stopped", "SoftInterlock", "InstrumentError"):
                raise {"ConfigError": ConfigError, "Stopped": Stopped,
                       "SoftInterlock": SoftInterlock, "InstrumentError": InstrumentError}[kind](str(exc)) from exc
            raise

    def _record(self, event, data):
        if self.log is not None:
            try:
                self.log(event, data)
            except Exception:  # Logging is observational, as in the reference engine.
                pass

    def _update(self, response):
        response = _decode(response)
        if not isinstance(response, dict) or not isinstance(response.get("state"), dict):
            raise InstrumentError("Malformed native Transmission response")
        for name in ("status", "reason", "initial", "commanded", "max_step", "results", "peak_center", "counts"):
            if name not in response["state"]:
                raise InstrumentError(f"Native Transmission response lacks {name}")
            setattr(self, name, response["state"][name])
        for entry in response.get("logs", []):
            data = entry["data"]
            if (self._fault_traceback and entry["event"] in ("run_end", "sweep_end", "revert_end", "restore_failed")
                    and data.get("traceback")):
                data = dict(data, traceback=self._fault_traceback)
            self._record(entry["event"], data)
        for entry in response.get("events", []):
            kind, data = entry["kind"], entry["data"]
            if kind == "stage":
                data["stage"] = self.config.stage(data["stage"])
            elif kind == "evaluation":
                record = data["record"]
                if record["index"] != len(self.history):
                    raise InstrumentError("Native evaluation sequence is discontinuous")
                self.history.append(record)
            elif kind == "history_update":
                for record in data["records"]:
                    self.history[record["index"]].update(record)
                continue
            elif kind == "spectrum":
                self.spectra[data["phase"]] = data["data"]
            elif kind == "check":
                self.checks[data["label"]] = data["data"]
            self.on_event(kind, data)
        return response

    def _cancelled(self):
        cancel = getattr(self.instrument, "cancel", None)
        return self._stop.is_set() or (cancel is not None and cancel.is_set())

    @staticmethod
    def _names(args, name, allowed):
        names = args.get(name, [])
        if not isinstance(names, list) or not all(isinstance(n, str) and n in allowed for n in names):
            raise InstrumentError(f"Native action requested unconfigured {name}")
        if len(set(names)) != len(names):
            raise InstrumentError(f"Native action duplicated {name}")
        return names

    def _guard_command(self, values, interlocks):
        self.instrument.check()
        for name, value in values.items():
            if name not in self.config.driven_channels or isinstance(value, bool) or not math.isfinite(value):
                raise InstrumentError("Native command contains an invalid channel or value")
            lo, hi = self.instrument.limits(name)
            if not lo <= value <= hi:
                raise InstrumentError(f"{name}: {value:g} outside device limits [{lo:g}, {hi:g}]")
            previous, step = self.commanded.get(name), self.max_step.get(name)
            if previous is None or step is None or abs(value - previous) > step + 1e-8 * max(1.0, step):
                raise InstrumentError(f"{name}: native command exceeds its acknowledged step limit")
        if interlocks:
            channels = list(dict.fromkeys([*self.config.pressure_channels, *self.config.current_channels]))
            latest = self.instrument.latest(channels) if channels else {}
            for name, (lo, hi) in self.config.pressure.items():
                value = latest.get(name, math.nan)
                if not math.isfinite(value):
                    raise SoftInterlock(f"Pressure {name} unavailable")
                if not lo <= value <= hi:
                    raise SoftInterlock(f"Pressure {name} = {value:g} mbar outside [{lo:g}, {hi:g}] mbar")
            for name in self.config.current_channels:
                value = latest.get(name, math.nan)
                if math.isfinite(value) and abs(value) > self.config.max_current:
                    raise SoftInterlock(f"Current {name} = {value:g} A exceeds {self.config.max_current:g} A (discharge?)")

    def _execute(self, action):
        if self._closed or self._failed or getattr(self.worker, "closed", False):
            raise InstrumentError("Native session is retired; Explorer action refused, restoration not confirmed")
        args, op = action["args"], action["op"]
        if action["cancellable"] and self._cancelled() and not (op == "measuring" and args.get("active") is False):
            raise Stopped("Stopped by the operator")
        driven = set(self.config.driven_channels)
        measured = set(self.config.current_channels) | set(self.config.pressure_channels)
        if op == "now":
            return None
        if op == "inspect":
            self.instrument.check()
            latest = self._names(args, "latest", measured)
            limits = self._names(args, "limits", driven)
            readbacks = self._names(args, "readbacks", driven)
            setpoints = self._names(args, "setpoints", driven)
            return dict(latest=self.instrument.latest(latest) if latest else {},
                        limits={n: list(self.instrument.limits(n)) for n in limits},
                        readbacks=self.instrument.readbacks(readbacks) if readbacks else {},
                        setpoints=self.instrument.setpoints(setpoints) if setpoints else {})
        if op == "command":
            values = args.get("values")
            if not isinstance(values, dict):
                raise InstrumentError("Native command must contain channel values")
            self._guard_command(values, args["interlocks"])
            if self._closed or getattr(self.worker, "closed", False):
                raise InstrumentError("Native session was retired before its command; restoration not confirmed")
            if action["cancellable"] and self._cancelled():
                raise Stopped("Stopped by the operator")
            self.instrument.command(values)
            return None
        if op == "wait":
            seconds = args["seconds"]
            if isinstance(seconds, bool) or not math.isfinite(seconds) or not 0 <= seconds <= self.config.poll_s:
                raise InstrumentError("Native wait exceeds the configured polling interval")
            self.instrument.wait(seconds, cancellable=action["cancellable"])
            return None
        if op == "measuring":
            if not isinstance(args.get("active"), bool):
                raise InstrumentError("Invalid native averaging-window state")
            if args["active"]:
                self._measuring = True
            self.instrument.measuring(args["active"])
            self._measuring = args["active"]
            return None
        if op == "window":
            channels = self._names(args, "channels", measured)
            begin, end = args["begin"], args["end"]
            if not (math.isfinite(begin) and math.isfinite(end) and begin < end):
                raise InstrumentError("Invalid native averaging window")
            data = self.instrument.window(channels, begin, end)
            try:
                diagnostics = self.instrument.diagnostics() if data is not None else {}
            except Exception as exc:
                diagnostics = dict(error=repr(exc))
            return dict(data=data, diagnostics=diagnostics)
        raise InstrumentError(f"Unsupported native Explorer action: {op}")

    def _drive(self, method, *args):
        if self._failed or self._closed or getattr(self.worker, "closed", False):
            raise InstrumentError("Native Transmission session is unavailable; writes will not be retried")
        if not self._lock.acquire(blocking=False):
            raise InstrumentError("A Transmission operation is already active")
        self._active = True
        self._fault_traceback = None
        try:
            response = self._update(self._rpc(method, *args))
            for _ in range(self.MAX_ACTIONS):
                if response.get("complete") is True:
                    if response.get("action") is not None or not isinstance(response.get("result"), dict):
                        raise InstrumentError("Native operation completed without a valid outcome")
                    return response["result"]
                action = response.get("action")
                if not isinstance(action, dict) or not isinstance(action.get("id"), int) or isinstance(action["id"], bool):
                    raise InstrumentError("Native operation has no identified action")
                if action["id"] <= self._last_action_id:
                    raise InstrumentError("Native action was repeated or retired")
                if not isinstance(action.get("args"), dict) or not isinstance(action.get("cancellable"), bool):
                    raise InstrumentError("Malformed native action")
                self._last_action_id = action["id"]
                observation = dict(id=action["id"])
                try:
                    observation["value"] = _plain(self._execute(action))
                except Exception as exc:
                    observation["error"] = dict(kind=type(exc).__name__, message=str(exc))
                    self._fault_traceback = traceback.format_exc()
                    self._record("instrument_exception", dict(action=action, error=observation["error"],
                                                              traceback=self._fault_traceback))
                observation["time"] = float(self.instrument.now())
                response = self._update(self._rpc("observe", observation))
            raise InstrumentError("Native action budget exceeded")
        except BaseException as exc:
            self._failed = True
            self.status, self.reason = "instrument error", f"Native session failed: {exc}; no write or restoration was retried"
            self._record("native_session_failed", dict(reason=self.reason, commanded=dict(self.commanded),
                                                       traceback=traceback.format_exc()))
            raise
        finally:
            try:
                if self._measuring:
                    self.instrument.measuring(False)
                    self._measuring = False
            except Exception:
                pass
            self._active = False
            self._lock.release()

    def run(self):
        return self._drive("run")

    def spectrum(self, amplitudes, measure, channels=None, max_step=None):
        amplitudes = [float(value) for value in amplitudes]
        if channels is not None:
            if not amplitudes:
                raise ConfigError("A spectrum needs at least one amplitude")
            ordered = sorted(amplitudes)
            step = float(max_step) if max_step else max([1.0, *(b - a for a, b in zip(ordered, ordered[1:]))])
            self.config.target = _engine.Target(channels, amplitudes[0], step, measure, max_step=step, spectrum_points=0)
            self._update(self._rpc("configure", export_config(self.config, stages=self._selected, strategy=self.strategy_name)))
        return self._drive("spectrum", dict(amplitudes=amplitudes, measure=list(measure)))

    def restore_initial(self):
        self._stop.clear()
        return self._drive("restore_initial")

    def request_stop(self):
        already_requested = self._stop.is_set()
        self._stop.set()
        cancel = getattr(self.instrument, "cancel", None)
        if cancel is not None:
            cancel.set()
        # Interrupt a currently fitting GP; Explorer actions remain on the scan thread.
        if not already_requested and hasattr(self.worker, "cancel"):
            self.worker.cancel()

    def close(self, *, close_worker=False):
        """Retire an idle session, or request recovery if an operation is active.

        Reaping an idle child needs no final RPC and confirms no hardware state.
        The parent must explicitly force-reap an active child if recovery cannot
        be drained (for example, when Explorer's Qt event loop is closing).
        """
        if self._active:
            self.request_stop()
            return False
        if close_worker:
            self._closed = True
            self.worker.close(grace_s=0)
            return True
        if not self._closed:
            if not self._failed:
                self._rpc("shutdown")
            self._closed = True
        return True


def fit_peak(worker, amplitudes, currents, sems):
    return _decode(worker.call("fit_peak", _plain(dict(amplitudes=amplitudes, currents=currents, sems=sems))))


def pick_peak(worker, amplitudes, currents, near):
    return _decode(worker.call("pick_peak", _plain(dict(amplitudes=amplitudes, currents=currents, near=near))))
