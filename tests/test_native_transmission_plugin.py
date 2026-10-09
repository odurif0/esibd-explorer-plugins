"""Native lifecycle through a fake Explorer host and real packaged scan workers.

The gated transport tests isolate cancellation races; packaged-worker tests use
the reference simulated instrument, not vendor hardware or synthetic OFF claims.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from threading import Event, Thread
import time
from types import ModuleType, SimpleNamespace as NS

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
CONFIG = '''
settle_s = 0.1
average_s = 0.3
poll_s = 0.05
settle_timeout_s = 3.0
verify_pairs = 2
reference_every = 4
strategy = "coordinate"
seed = 2
[pressure]
P_funnel = [2.0, 6.0]
[[stage]]
name = "Funnel"
aperture = "A1"
downstream = ["A2", "A3", "A4", "Collector"]
budget = 40
[[stage.knob]]
name = "Inlet"
channels = { Inlet = 1.0 }
window = [-110.0, 270.0]
max_step = 10.0
[[stage.knob]]
name = "Funnel exit"
channels = { Funnel_exit = 1.0 }
window = [-15.0, 15.0]
max_step = 1.5
'''


class Signal:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


class Log:
    path = None
    error = None

    def __init__(self):
        self.rows = []
        self.closed = False

    def write(self, event, data=None, **fields):
        if not self.closed:
            self.rows.append((event, dict(data or {}, **fields)))

    def close(self):
        self.closed = True


@pytest.fixture
def module(monkeypatch):
    pytest.importorskip("PyQt6.QtCore")
    core, plugins = ModuleType("esibd.core"), ModuleType("esibd.plugins")

    class Scan:
        Display = type("Display", (), {})
        recording = property(lambda s: s._recording, lambda s, value: setattr(s, "_recording", bool(value)))
        finished = property(lambda s: s._finished, lambda s, value: setattr(s, "_finished", bool(value)))

        def __init__(self, pluginManager):
            self.pluginManager = pluginManager
            self._finished, self._recording = True, False
            self._dummy_initialization = False
            self.file = "mock-transmission.h5"
            self.host_toggles = self.host_closes = 0

        def toggleRecording(self):
            self.host_toggles += 1

        def close(self):
            self.host_closes += 1
            self.recording = False

    plugins.Scan, plugins.Plugin, plugins.SettingsManager = Scan, type("Plugin", (), {}), type("SettingsManager", (), {})
    core.TreeWidget = type("TreeWidget", (), {})
    core.INOUT, core.PRINT, core.PARAMETERTYPE = NS(IN="IN", OUT="OUT"), NS(WARNING="warning", ERROR="error"), NS()
    core.getTestMode = lambda: False
    core.parameterDict = lambda **kwargs: kwargs
    core.plotting = lambda function: function
    monkeypatch.setitem(sys.modules, "esibd.core", core)
    monkeypatch.setitem(sys.modules, "esibd.plugins", plugins)
    spec = importlib.util.spec_from_file_location("transmission_native_plugin_test", ROOT / "transmission/transmission_plugin.py")
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)  # Explorer does not register this entrypoint in sys.modules.
    return result


@pytest.fixture
def rig(module, tmp_path):
    scan = module.Transmission(pluginManager=NS(Settings=NS(dataPath=tmp_path, configPath=tmp_path)))
    scan._bridge = NS(**{name: Signal() for name in ("status", "refresh", "watch", "unwatch", "plot", "measuring")})
    scan.signalComm = NS(updateRecordingSignal=Signal(), scanUpdateSignal=Signal())
    sessions, messages, logs, progress = [], [], [], []
    scan.session = lambda event, **data: sessions.append((event, data)) if not scan._closing or event in (
        "plugin_close", "native_force_close") else None
    scan.print = lambda message, **kwargs: messages.append(message)
    scan.show_progress = progress.append
    scan.toggleDisplay = lambda **kwargs: None
    scan.updateFile = lambda: None
    scan._watch = lambda log: None
    scan._unwatch = lambda: None
    scan._gui_call = lambda action: action()
    scan.state.update(simulation=True)
    config = module._engine.parse_config(CONFIG)
    instrument = module._simulator.SimulatedBeamline(seed=0, noise=0.0, floor=0.0, drift_per_sqrt_hour=0.0,
                                                    cancel=scan._cancel)
    instrument.pace = 0
    scan._instrument = lambda config, cancel: (setattr(instrument, "cancel", cancel), instrument)[1]
    scan._configuration = lambda: (CONFIG, config)
    scan._config_used = CONFIG
    scan._run_log = Log()

    def open_log(*args, **kwargs):
        log = Log()
        logs.append(log)
        return log

    scan._open_run_log = open_log
    result = NS(module=module, scan=scan, config=config, instrument=instrument, sessions=sessions,
                messages=messages, logs=logs, progress=progress, children=[])
    yield result
    scan.close()
    for child in result.children:
        child.close(grace_s=0)


class GatedWorker:
    """Deterministic pending-fit seam; actions still execute in the real facade."""

    def __init__(self):
        self.closed = False
        self.fitting, self.release = Event(), Event()
        self.calls = []
        self.cancels = self.closes = 0
        self.initial = 120.0
        self.current = self.initial
        self.gate_configure = False

    def state(self):
        return dict(status="running", reason="", initial={"Inlet": self.initial}, commanded={"Inlet": self.current},
                    max_step={"Inlet": 10.0}, results=[], peak_center=None, counts={})

    def response(self, action=None, complete=False):
        state = self.state()
        result = None
        if complete:
            state["status"], state["reason"] = "stopped", "Stopped by the operator"
            result = dict(status="stopped", reason=state["reason"], initial=state["initial"], final=state["commanded"],
                          stages=[], evaluations=0, spectra={}, checks={})
        return dict(state=state, action=action, complete=complete, result=result)

    @staticmethod
    def command(identity, value, cancellable):
        return dict(id=identity, op="command", args=dict(values={"Inlet": value}, interlocks=cancellable),
                    cancellable=cancellable, timeout_s=3.0)

    def call(self, method, *args):
        self.calls.append((method, args))
        if self.closed:
            raise RuntimeError("Worker stopped; output safety is not confirmed")
        if method == "configure":
            if self.gate_configure:
                self.fitting.set()
                assert self.release.wait(5), "Configuration gate was not released"
                if self.closed:
                    raise RuntimeError("Worker stopped during configuration")
            return self.response()
        if method == "run":
            return self.response(self.command(1, self.initial + 10.0, True))
        if method == "observe":
            observation = args[0]
            assert "error" not in observation, observation
            if observation["id"] == 1:
                self.current += 10.0
                self.fitting.set()
                assert self.release.wait(5), "Fit gate was not cancelled/reaped"
                if self.closed:
                    raise RuntimeError("Worker stopped during fit; restoration not confirmed")
                return self.response(self.command(2, self.initial, False))
            assert observation["id"] == 2
            self.current = self.initial
            return self.response(complete=True)
        raise AssertionError(f"Unexpected native RPC: {method}")

    def cancel(self):
        self.cancels += 1
        self.release.set()
        return True

    def close(self, *, grace_s=0):
        self.closes += 1
        self.closed = True
        self.release.set()


def use_gated(rig, monkeypatch, *, configure_gate=False):
    child = GatedWorker()
    child.gate_configure = configure_gate
    rig.children.append(child)
    monkeypatch.setattr(rig.module._native_worker, "NativeWorkerProxy", lambda *args, **kwargs: child)
    return child


def attach(rig):
    optimizer = rig.scan._new_native_optimizer(rig.config, rig.instrument)
    rig.scan._optimizer = optimizer
    return optimizer


def join(thread):
    thread.join(5)
    assert not thread.is_alive(), "Transmission's operation did not drain after stop/close"


@pytest.mark.parametrize("via_property", [False, True])
def test_stop_interrupts_fit_and_drains_recovery(rig, monkeypatch, via_property):
    child = use_gated(rig, monkeypatch)
    attach(rig)
    rig.scan.recording = True
    rig.scan._finished = False
    thread = Thread(target=rig.scan.runScan, args=(lambda: rig.scan.recording,), daemon=True)
    thread.start()
    assert child.fitting.wait(3)
    if via_property:
        rig.scan.recording = False  # DeviceManager, not the panel button.
    else:
        rig.scan.stop_optimization()
    join(thread)
    assert child.cancels == 1  # Duplicate GUI stop paths cannot cancel a recovery RPC.
    assert rig.scan._result["status"] == "stopped"
    assert [command[1]["Inlet"] for command in rig.instrument.commands] == [130.0, 120.0]
    assert child.closed and not rig.scan._native_workers
    assert not any(event == "native_force_close" for event, _ in rig.sessions)


def test_force_close_pending_fit_reaps_without_restore_or_off_claim(rig, monkeypatch):
    child = use_gated(rig, monkeypatch)
    attach(rig)
    rig.scan.recording = True
    thread = Thread(target=rig.scan.runScan, args=(lambda: rig.scan.recording,), daemon=True)
    thread.start()
    assert child.fitting.wait(3)
    started = time.monotonic()
    rig.scan.close()
    assert time.monotonic() - started < 2.0
    join(thread)
    assert child.closed and rig.scan.host_closes == 1
    assert rig.scan._result["status"] == "error"
    assert "restoration not confirmed" in rig.scan._result["reason"]
    assert [command[1]["Inlet"] for command in rig.instrument.commands] == [130.0]
    force = next(data for event, data in rig.sessions if event == "native_force_close")
    assert force["outputs_confirmed_off"] is force["restoration_confirmed"] is False
    assert not rig.scan.signalComm.scanUpdateSignal.calls  # No queued success/GUI completion after close.
    assert rig.instrument.devices_on  # Reaping is never represented as device OFF.
    rig.scan.close()
    assert rig.scan.host_closes == 1


def test_closing_refuses_late_gui_action(rig):
    bridge = rig.module._GuiBridge(rig.scan)
    writes = []
    call = rig.module._Call(lambda: writes.append(42))
    rig.scan.close()
    bridge.execute(call)
    assert call.done.is_set() and not writes
    assert isinstance(call.error, rig.module._engine.InstrumentError)


@pytest.mark.parametrize("instrument_call", [False, True])
def test_close_wakes_pending_explorer_request_without_qt_drain(rig, instrument_call):
    bridge = rig.module._GuiBridge(rig.scan)
    queued, errors, writes = [], [], []
    posted = Event()

    class Queue:
        def emit(self, call):
            queued.append(call)
            posted.set()

    rig.scan._bridge = NS(thread=bridge.thread, execute=bridge.execute, request=Queue())
    if instrument_call:
        instrument = rig.module.ExplorerInstrument.__new__(rig.module.ExplorerInstrument)
        instrument.plugin, instrument.bridge, instrument.log, instrument._latency = rig.scan, rig.scan._bridge, None, []
        dispatch = instrument._gui
    else:
        dispatch = lambda action: rig.module.Transmission._gui_call(rig.scan, action)

    def request():
        try:
            dispatch(lambda: writes.append(42))
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=request, daemon=True)
    thread.start()
    assert posted.wait(3)
    assert len(rig.scan._gui_calls) == 1
    started = time.monotonic()
    rig.scan.close()
    join(thread)
    assert time.monotonic() - started < 1.0
    assert errors and isinstance(errors[0], rig.module._engine.InstrumentError)
    bridge.execute(queued[0])  # Late Qt dispatch cannot apply the abandoned setpoint.
    assert not writes and not rig.scan._gui_calls


def test_close_reaps_worker_being_configured(rig, monkeypatch):
    child = use_gated(rig, monkeypatch, configure_gate=True)
    errors = []

    def create():
        try:
            attach(rig)
        except Exception as exc:
            errors.append(exc)

    thread = Thread(target=create, daemon=True)
    thread.start()
    assert child.fitting.wait(3)
    rig.scan.close()
    join(thread)
    assert child.closed and not rig.scan._native_workers
    assert errors and rig.scan._optimizer is None


def test_new_scan_refuses_active_child(rig, monkeypatch):
    child = use_gated(rig, monkeypatch)
    optimizer = attach(rig)
    optimizer._active = True
    assert rig.scan.initScan() is False
    assert rig.scan._optimizer is optimizer and not child.closed
    assert len([method for method, _ in child.calls if method == "configure"]) == 1
    rig.scan.keep()
    assert not child.closed
    optimizer._active = False


def test_failed_replacement_preserves_previous_undo_session(rig, monkeypatch):
    child = use_gated(rig, monkeypatch)
    previous = attach(rig)
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("Native executable integrity failure")

    monkeypatch.setattr(rig.module._native_worker, "NativeWorkerProxy", fail)
    assert rig.scan.initScan() is False
    assert calls == [1] and rig.scan._optimizer is previous and not child.closed
    assert "integrity failure" in rig.progress[-1]


def test_configuration_failure_reaps_new_child_no_retry(rig, monkeypatch):
    child = use_gated(rig, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("Malformed native configure reply")

    monkeypatch.setattr(rig.module._native_engine, "Optimizer", fail)
    assert rig.scan.initScan() is False
    assert child.closed and not rig.scan._native_workers and rig.scan._optimizer is None


def packaged(rig, monkeypatch):
    real_factory = rig.module._native_worker.NativeWorkerProxy

    def create(*args, **kwargs):
        child = real_factory(*args, **kwargs)
        rig.children.append(child)
        return child

    monkeypatch.setattr(rig.module._native_worker, "NativeWorkerProxy", create)


def assert_reaped(child):
    assert child.closed and child._process.poll() is not None
    for thread in child._threads:
        thread.join(1)
    assert all(not thread.is_alive() for thread in child._threads)


def test_actual_keep_retires_child_and_keeps_cached_result(rig, monkeypatch):
    packaged(rig, monkeypatch)
    assert rig.scan.initScan()
    optimizer = rig.scan._optimizer
    rig.scan.runScan(lambda: True)
    assert rig.scan._result["status"] == "completed"
    assert any(stage["adopted"] for stage in rig.scan._result["stages"])
    assert not rig.scan._decided and not optimizer.worker.closed
    cached, initial, commands = rig.scan._result, dict(optimizer.initial), len(rig.instrument.commands)
    rig.scan.keep()
    assert_reaped(optimizer.worker)
    assert rig.scan._result is cached and optimizer.initial == initial
    assert len(rig.instrument.commands) == commands and not rig.scan._native_workers
    rig.scan.undo()
    assert "will not be retried" in rig.progress[-1]
    assert len(rig.children) == 1 and len(rig.instrument.commands) == commands


def test_actual_undo_drains_then_reaps_without_new_child(rig, monkeypatch):
    packaged(rig, monkeypatch)
    assert rig.scan.initScan()
    optimizer = rig.scan._optimizer
    rig.scan.runScan(lambda: True)
    assert not optimizer.worker.closed and not rig.scan._decided
    rig.scan.undo()
    join(rig.scan._worker)
    assert_reaped(optimizer.worker)
    assert not rig.scan._reverting and not rig.scan._native_workers
    assert len(rig.children) == 1
    assert all(rig.instrument.setpoint[channel] == value for channel, value in optimizer.initial.items())
    assert next(data for event, data in rig.sessions if event == "revert")["status"] == "restored"


def test_actual_failed_undo_is_not_retried(rig, monkeypatch):
    packaged(rig, monkeypatch)
    assert rig.scan.initScan()
    optimizer = rig.scan._optimizer
    rig.scan.runScan(lambda: True)
    rig.instrument.devices_on = False
    commands = len(rig.instrument.commands)
    rig.scan.undo()
    join(rig.scan._worker)
    assert_reaped(optimizer.worker)
    assert len(rig.instrument.commands) == commands
    assert next(data for event, data in rig.sessions if event == "revert")["status"] != "restored"
    rig.scan.undo()
    assert "will not be retried" in rig.progress[-1]
    assert len(rig.children) == 1 and len(rig.instrument.commands) == commands


def test_actual_new_scan_replaces_idle_worker_only_after_success(rig, monkeypatch):
    packaged(rig, monkeypatch)
    assert rig.scan.initScan()
    previous = rig.scan._optimizer
    rig.scan.runScan(lambda: True)
    assert not previous.worker.closed
    commands = len(rig.instrument.commands)
    assert rig.scan.initScan()
    assert_reaped(previous.worker)
    assert rig.scan._optimizer is not previous
    assert not rig.scan._optimizer.worker.closed and len(rig.children) == 2
    assert len(rig.instrument.commands) == commands  # Replacement never restores or repeats writes.
    rig.scan.close()
    assert_reaped(rig.children[-1])


@pytest.mark.parametrize("force_close", [False, True])
def test_actual_pending_gp_observation_stop_or_close_is_bounded(rig, monkeypatch, force_close):
    packaged(rig, monkeypatch)
    engine = rig.module._engine
    channels = [f"X{i}" for i in range(32)]
    knobs = [engine.Knob(name, {name: 1.0}, (-5.0, 5.0), 1.0) for name in channels]
    config = engine.Config([engine.Stage("Dense", knobs, "A", ["D"], budget=90)],
                           settle_s=0.0, average_s=1.0, poll_s=0.5, verify_pairs=2, seed=3)

    class Instrument(engine.Instrument):
        def __init__(self):
            self.values = dict.fromkeys(channels, 0.0)
            self.cancel, self.time = Event(), 1000.0
            self.commands, self.windows = [], 0
            self.gp_pending = Event()

        def check(self):
            pass  # This fixture has no device-availability faults.

        def now(self):
            return self.time

        def wait(self, seconds, *, cancellable=True):
            self.time += seconds

        def limits(self, channel):
            return -6.0, 6.0

        def setpoints(self, names):
            return {name: self.values[name] for name in names}

        readbacks = setpoints

        def command(self, values):
            self.commands.append(dict(values))
            self.values.update(values)

        def latest(self, names):
            transmitted = 4e-10 - 1e-11 * sum((value - 2.0)**2 for value in self.values.values()) / len(channels)
            return {name: transmitted if name == "D" else 1e-9 - transmitted for name in names}

        def window(self, names, begin, end):
            self.windows += 1
            return {name: (value, 0.0, 3) for name, value in self.latest(names).items()}

        def measuring(self, active):
            # One before-check, two initial references, then two probes per knob.
            # Acknowledging this close drives the first native GP fit on 66 points.
            if not active and self.windows == 67:
                self.gp_pending.set()

    instrument = Instrument()
    optimizer = rig.scan._new_native_optimizer(config, instrument)
    rig.scan._optimizer = optimizer
    rig.scan.recording = True
    worker, errors, elapsed, commands_at_stop = optimizer.worker, [], [], []

    def stop():
        try:
            assert instrument.gp_pending.wait(5), "Native plan did not reach its GP observation"
            deadline = time.monotonic() + 3.0
            while worker._active is None and time.monotonic() < deadline:
                time.sleep(0.0001)
            assert worker._active is not None and worker._active_method == "observe"
            time.sleep(0.005)  # Let the pending request enter the numerical solver.
            commands_at_stop.append(len(instrument.commands))
            started = time.monotonic()
            rig.scan.close() if force_close else rig.scan.stop_optimization()
            elapsed.append(time.monotonic() - started)
        except Exception as exc:
            errors.append(exc)

    stopper = Thread(target=stop, daemon=True)
    thread = Thread(target=rig.scan.runScan, args=(lambda: rig.scan.recording,), daemon=True)
    stopper.start()
    thread.start()
    join(stopper)
    join(thread)
    assert not errors and elapsed and max(elapsed) < 2.0
    assert_reaped(worker)
    if force_close:
        assert rig.scan._result["status"] == "error"
        assert len(instrument.commands) == commands_at_stop[0]
        assert any(value != 0.0 for value in instrument.values.values())
    else:
        assert rig.scan._result["status"] == "stopped", rig.scan._result["reason"]
        assert all(value == 0.0 for value in instrument.values.values())


@pytest.mark.parametrize("mode", ["complete", "stop", "force_close"])
def test_actual_local_quick_sweep_is_tracked_and_reaped(rig, monkeypatch, mode):
    packaged(rig, monkeypatch)
    points = []
    initial = rig.instrument.setpoint["Q2_RF"]
    cancel = Event()

    def point(amplitude, current):
        points.append((amplitude, current))
        if len(points) == 2:
            if mode == "stop":
                rig.scan.stop_optimization()
            elif mode == "force_close":
                rig.scan.close()

    outcome = rig.scan.quick_sweep("q2", np.linspace(260.0, 320.0, 7), point, cancel)
    assert len(rig.children) == 1
    assert_reaped(rig.children[0])
    assert not rig.scan._native_workers and not rig.scan._sweeping and rig.scan._sweep_cancel is None
    assert rig.logs[-1].closed
    assert outcome["status"] == {"complete": "completed", "stop": "stopped", "force_close": "error"}[mode]
    if mode == "force_close":
        assert rig.instrument.setpoint["Q2_RF"] != initial
        assert "restored" not in outcome["reason"].lower()
    else:
        assert rig.instrument.setpoint["Q2_RF"] == initial
    if mode == "stop":
        assert cancel.is_set() and len(points) == 2
