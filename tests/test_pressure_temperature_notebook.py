"""Execute the delivered one-cell notebook with simulated instruments, never hardware.

Electrical values below are fixtures, NOT recommended heater limits.
"""
from __future__ import annotations

import ast
import csv
import json
import math
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/pressure_temperature.ipynb"


def code():
    notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
    cells = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
    assert len(cells) == 1
    assert cells[0]["execution_count"] is None and cells[0]["outputs"] == []
    return "".join(cells[0]["source"])


@pytest.fixture
def ns():
    tree = ast.parse(code())
    assert ast.unparse(tree.body.pop()) == "main()"
    namespace = {"__file__": str(NOTEBOOK)}
    exec(compile(tree, str(NOTEBOOK), "exec"), namespace)
    namespace["TemperatureStability"] = namespace["load_private"](ROOT / "esi/_heater_stability.py").TemperatureStability
    namespace["bounded_limit"] = namespace["load_private"](ROOT / "esi/_heater_limits.py").bounded_limit
    return namespace


class Clock:
    def __init__(self):
        self.now = 0.
        self.interrupt_at = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.interrupt_at is not None and self.now >= self.interrupt_at:
            self.interrupt_at = None
            raise KeyboardInterrupt("simulated user Stop")


class FakeESI:
    def __init__(self, calls):
        self.calls = calls
        self._transport_poisoned = False
        self.active = self.enabled = False
        self.target = 0.
        self.temperature = 21.
        self.sensor_valid = True
        self.limits = dict(voltage_limit_v=12., current_limit_a=1., power_limit_w=10.)
        self.maxima = dict(max_voltage_v=24., max_current_a=10., max_power_w=200., max_temperature_c=175.)
        self.failures = {}
        self.hv_active, self.hv_target = False, 0.
        self.main_state, self.device_state = "STATE_ON", "0x0"
        self.diag_hook = None
        self.disconnect_hook = None
        self.quantum = 0.
        self.calls_after_poison = []
        self.thread_ids = []

    def call(self, method, *args):
        self.thread_ids.append(threading.get_ident())
        if self._transport_poisoned:
            self.calls_after_poison.append(method)
            raise AssertionError("No ESI call allowed after transport poison")
        self.calls.append((method, *args))
        failure = self.failures.get(method)
        if callable(failure):
            failure = failure()
        if failure is not None:
            raise failure

    def connect(self):
        self.call("connect")
        return True

    def get_heat_configuration(self):
        self.call("configuration")
        return dict(hardware_limits=dict(self.maxima), target_temperature_c=self.target, **self.limits)

    @staticmethod
    def check_cancel(cancel_event):
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("Heater operation cancelled")

    def collect_diagnostics(self, *, cancel_event=None):
        self.check_cancel(cancel_event)
        self.call("diagnostics")
        if self.diag_hook:
            self.diag_hook(self)
        self.check_cancel(cancel_event)
        return {"main_state": {"name": self.main_state}, "device_state": {"hex": self.device_state},
                "modules": {a: {"module_active": self.hv_active, "target_v": self.hv_target} for a in (1, 2)},
                "heat": dict(valid=self.sensor_valid, monitor_temperature_c=self.temperature,
                             target_temperature_c=self.target, module_active=self.active,
                             active=self.active and self.enabled, hardware_limits=dict(self.maxima),
                             interlock_state=0, **self.limits)}

    def configure_heat_limits(self, *, cancel_event=None, **kwargs):
        self.check_cancel(cancel_event)
        self.call("limits", kwargs)
        self.check_cancel(cancel_event)
        mapping = {"voltage_v": "voltage_limit_v", "current_a": "current_limit_a", "power_w": "power_limit_w"}
        applied = {}
        for key, value in kwargs.items():
            if value is not None:
                self.limits[mapping[key]] = value
                applied[key] = value
        return applied

    def set_heater_temperature(self, target, *, cancel_event=None):
        self.check_cancel(cancel_event)
        self.call("target", target)
        self.check_cancel(cancel_event)
        self.target = target + self.quantum
        self.temperature = target - .1
        return self.target

    def set_output_active(self, address, active, *, cancel_event=None):
        assert address == 0, "Notebook must never activate an HV module"
        if active:
            self.check_cancel(cancel_event)
        self.call("heater_on" if active else "heater_off")
        if active:
            self.check_cancel(cancel_event)
        self.active = self.enabled = active
        if not active:
            self.target = 0.
        return active

    def disconnect(self, *, on_discharge=None):
        self.call("disconnect")
        if self.disconnect_hook:
            self.disconnect_hook()
        self.active = self.enabled = False
        self.target = 0.
        return True


class FakePressure:
    def __init__(self, calls, clock):
        self.calls, self.clock = calls, clock
        self.count = 0
        self.failure_at = None
        self.failure = RuntimeError("simulated pressure timeout/NAK")
        self.statuses = (0, 0, 0, 0, 0, 0)
        self.values = (1e-6, 2e-6, 3e-6, 4e-6, 5e-6, 6e-6)
        self.source_unit = "mbar"
        self.read_hook = None
        self.close_failure = None
        self.closed = False
        self.in_read = False
        self.read_on_shutdown = threading.Event()
        self.main_thread = threading.get_ident()
        self.cancelled = threading.Event()
        self.is_shutdown = lambda: False
        self.thread_ids = []

    def open(self):
        self.thread_ids.append(threading.get_ident())
        self.calls.append(("pressure_open",))
        return {"identification": "SIMULATED TPG366", "gauges": ["SIM"] * 6}

    def read(self):
        self.thread_ids.append(threading.get_ident())
        self.count += 1
        self.calls.append(("pressure_read", self.count))
        if self.is_shutdown():
            self.read_on_shutdown.set()
        self.in_read = True
        try:
            if self.failure_at is not None and self.count >= self.failure_at:
                raise self.failure
            if self.read_hook:
                self.read_hook(self)
            return SimpleNamespace(pressures=self.values, statuses=self.statuses, unit=self.source_unit,
                                   received_at=1_780_000_000 + self.clock())
        finally:
            self.in_read = False

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.cancelled.set()
        assert not self.in_read, "No serial Close alongside an active pressure read"
        self.calls.append(("pressure_close",))
        if self.close_failure:
            raise self.close_failure
        self.closed = True


class NoPlots:
    def update(self, rows):
        pass

    def save(self, directory, rows):
        pass


class SimulatedGuard:
    """Component-test lease only; production must use the verified shared helper.

    Real helper/cross-notebook tests run separately in fresh subprocesses.
    """
    def __init__(self, output_dir, **kwargs):
        self.output_dir = Path(output_dir)
        self.marker = self.output_dir / ".pressure_temperature.running.json"
        self.claimed = self.started = self.released = False
        self.events = []

    def check(self):
        assert not self.released

    def authorize_restart(self):
        if self.marker.exists():
            raise RuntimeError("Unfinished/unsafe run")
        return []

    def claim(self, directory, report_path):
        self.check()
        self.events.append("claim")
        assert (Path(directory) / "samples.csv").is_file()
        assert json.loads(Path(report_path).read_text())["shutdown_confirmed"] is False
        try:
            with self.marker.open("x") as stream:
                json.dump({"simulated": True, "run_directory": str(directory)}, stream)
        except FileExistsError:
            raise RuntimeError("Unfinished/unsafe run") from None
        self.claimed = True
        return {"claim_id": "simulated", "operator_restarts": []}

    def mark_hardware_started(self):
        self.check()
        assert self.claimed and not self.started
        self.started = True
        self.events.append("hardware_started")

    def finalize(self, *, shutdown_confirmed, owners_idle, auxiliary_closed, transport_poisoned):
        self.check()
        self.events.append("finalize")
        if not (shutdown_confirmed is True and owners_idle is True and auxiliary_closed is True
                and transport_poisoned is False):
            return False
        self.marker.unlink()
        self.released = True
        return True

    def abandon_before_hardware(self):
        if self.started:
            raise RuntimeError("Cannot abandon after hardware boundary")
        if self.claimed:
            return False
        self.released = True
        return True


@pytest.fixture
def rig(ns, tmp_path):
    calls, clock = [], Clock()
    esi, pressure = FakeESI(calls), FakePressure(calls, clock)
    run = ns["Experiment"](tmp_path / "runs", lambda directory: esi, lambda: pressure, limit_preparer=ns["bounded_limit"],
                           guard=SimulatedGuard(tmp_path / "runs"), baseline_s=3., hold_s=3., cooling_s=3.,
                           stability_factory=lambda: ns["TemperatureStability"](window_s=2.),
                           clock=clock, sleep=clock.sleep, plots_factory=NoPlots)
    pressure.is_shutdown = lambda: run.phase == "shutdown"
    return SimpleNamespace(calls=calls, clock=clock, esi=esi, pressure=pressure, run=run)


def report(run):
    return json.loads((run.directory / "metadata.json").read_text(),
                      parse_constant=lambda token: pytest.fail(f"Invalid JSON value {token}"))


def rows(run):
    with (run.directory / "samples.csv").open(newline="") as stream:
        return list(csv.DictReader(stream))


def test_disarmed_entire_cell_does_not_load_runtime_or_touch_hardware(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    namespace = {}
    exec(compile(code(), str(NOTEBOOK), "exec"), namespace)
    assert "No hardware accessed" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
    namespace["find_plugins"] = lambda *a: pytest.fail("Disarmed must not resolve/load runtimes")
    assert namespace["main"]() is None


def test_defaults_and_notebook_prose():
    notebook = json.loads(NOTEBOOK.read_text())
    assert notebook["nbformat"] == 4
    assert notebook["nbformat_minor"] == 5
    text = "".join(notebook["cells"][0]["source"])
    assert "thermal equilibrium" in text and "175" in text
    tree = ast.parse(code())
    constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                 and isinstance(node.value, (ast.Constant, ast.Tuple))}
    assert constants["ARM_HEATING"] is False
    assert constants["BASELINE_S"] == 60 and constants["HOLD_S"] == 300 and constants["SAMPLE_S"] == 1
    assert constants["TARGETS_C"] == tuple(range(30, 171, 10)) + (175,)
    assert constants["QUALIFICATION_DEADLINE_S"] == 600 and constants["STAGE_DEADLINE_S"] == 900
    assert constants["COOLING_S"] == 900
    assert tuple(constants[key] for key in ("HEATER_VOLTAGE_V", "HEATER_CURRENT_A", "HEATER_POWER_W")) == (22, 10, 50)


def test_complete_program_uses_real_temperature_six_pressures_and_verified_stop(ns, rig):
    result = rig.run.run()
    metadata = report(result)
    assert metadata["outcome"] == "complete" and metadata["shutdown_confirmed"] is True
    assert metadata["program_c"] == list(range(30, 171, 10)) + [175]
    assert [c[1] for c in rig.calls if c[0] == "target"] == metadata["program_c"]
    assert rig.calls.count(("heater_on",)) == 1
    assert rig.calls.index(("heater_off",)) < rig.calls.index(("disconnect",)) < rig.calls.index(("pressure_close",))
    prepare = rig.run.limit_preparer
    assert [c[1] for c in rig.calls if c[0] == "limits"] == [{"voltage_v": prepare(22.), "current_a": prepare(10.), "power_w": prepare(50., power=True)}]
    assert not ns["_PT_GUARD"] and not rig.run.guard_path.exists()
    saved = rows(result)
    heated = [row for row in saved if row["phase"] == "heating"]
    assert all(float(row["temperature_c"]) != float(row["target_temperature_c"]) for row in heated)
    assert all(row["pressure_observed_utc"] and row["temperature_observed_utc"] for row in heated)
    assert float(heated[0]["elapsed_s"]) >= 3
    assert rig.clock.now >= 3 + 16 * (2 + 3) + 3
    assert all(stage["complete"] and stage["qualified_observation_s"] >= 3 for stage in metadata["stages"])
    assert metadata["cooling_complete"]
    assert any(row["phase"] == "cooling" and math.isfinite(float(row["temperature_c"])) for row in saved)
    assert [float(heated[0][f"P{i}_mbar"]) for i in range(1, 7)] == pytest.approx([i * 1e-6 for i in range(1, 7)])
    for row in saved:
        if row["phase"] == "shutdown":
            assert math.isnan(float(row["temperature_c"])) and not row["temperature_observed_utc"]


@pytest.mark.parametrize("temperature", [30.0000000001, 30.2, 53., 174.9])
def test_warm_start_refuses_heating_without_skipping_stages(rig, temperature):
    rig.esi.temperature = temperature
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "error" and data["shutdown_confirmed"]
    assert not data["stages"] and "program_c" not in data
    assert not any(call[0] in ("target", "heater_on") for call in rig.calls)
    assert "let the instrument cool" in " ".join(data["errors"])


@pytest.mark.parametrize("temperature", [21., 29.8, 30.])
def test_at_or_below_first_target_keeps_the_complete_ascending_program(rig, temperature):
    rig.esi.temperature = temperature
    rig.run.run()
    assert report(rig.run)["program_c"] == list(range(30, 171, 10)) + [175]
    assert report(rig.run)["outcome"] == "complete"


@pytest.mark.parametrize("key", ["voltage_limit_v", "current_limit_a", "power_limit_w"])
@pytest.mark.parametrize("value", [0., -1., math.nan, math.inf, 999., True])
def test_zero_or_invalid_limits_never_heat_and_save_actionable_preflight(rig, key, value):
    rig.esi.limits[key] = value
    rig.run.overrides = {"voltage_v": None, "current_a": None, "power_w": None}
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "error" and metadata["shutdown_confirmed"]
    assert not any(call[0] in ("target", "heater_on", "limits", "pressure_open") for call in rig.calls)
    assert "positive load-appropriate" in " ".join(metadata["errors"])


def test_only_explicit_overrides_are_written_and_verified(rig):
    rig.esi.limits = dict.fromkeys(rig.esi.limits, 0.)
    rig.run.overrides = {"voltage_v": 12., "current_a": 1., "power_w": 10.}
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"
    assert [c for c in rig.calls if c[0] == "limits"] == [("limits", {key: rig.run.limit_preparer(value, power=key == "power_w") for key, value in rig.run.overrides.items()})]


def test_incorrect_limit_readback_refuses_heating(rig):
    rig.run.overrides = {"voltage_v": 10., "current_a": None, "power_w": None}
    rig.esi.configure_heat_limits = lambda **kwargs: None  # Lost or ignored setting.
    rig.run.run()
    assert "readback mismatch" in " ".join(report(rig.run)["errors"])
    assert ("heater_on",) not in rig.calls


@pytest.mark.parametrize("targets", [(25., 176.), (175., 100.), (math.nan,), (), (-1.,)])
def test_invalid_target_program_has_no_hardware_access_or_output(ns, rig, targets):
    rig.run.targets = targets
    with pytest.raises(ValueError):
        rig.run.run()
    assert rig.calls == [] and rig.run.directory is None and not ns["_PT_GUARD"]


def test_missing_shared_limit_preparer_refuses_before_any_hardware(rig):
    rig.run.limit_preparer = None
    with pytest.raises(ValueError, match="electrical-limit helper"):
        rig.run.run()
    assert not rig.calls and rig.run.directory is None


@pytest.mark.parametrize("value", [True, 0., -1., math.nan, math.inf, 999.])
def test_invalid_prepared_native_limit_refuses_before_any_limit_write(rig, value):
    rig.run.limit_preparer = lambda *args, **kwargs: value
    rig.run.run()
    assert not any(call[0] in {"limits", "target", "heater_on"} for call in rig.calls)
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]


@pytest.mark.parametrize("key", ["max_voltage_v", "max_current_a", "max_power_w"])
def test_hardware_ceiling_with_no_positive_native_code_is_not_rounded_up(rig, key):
    rig.esi.maxima[key] = 1e-10
    rig.run.run()
    assert not any(call[0] in {"limits", "target", "heater_on"} for call in rig.calls)
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]


def test_native_nearest_rounding_is_prepared_below_all_approved_ceilings(ns, rig):
    limits = ns["load_private"](ROOT / "esi/_heater_limits.py")
    assert limits.native_value(limits.native_code(10.)) == pytest.approx(10.000384, abs=1e-12)
    original = rig.esi.configure_heat_limits
    mapping = {"voltage_v": "voltage_limit_v", "current_a": "current_limit_a", "power_w": "power_limit_w"}
    def nearest(**kwargs):
        echoes = original(**kwargs)
        for key, value in echoes.items():
            power = key == "power_w"
            echoes[key] = limits.native_value(limits.native_code(value, power), power)
            rig.esi.limits[mapping[key]] = echoes[key]
        return echoes
    rig.esi.configure_heat_limits = nearest
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "complete"
    assert data["requested_heat_limits"]["current_limit_a"] == 10.
    assert data["prepared_heat_limit_inputs"]["current_a"] == pytest.approx(9.99936, abs=1e-12)
    for key, ceiling in {"voltage_v": 22., "current_a": 10., "power_w": 50.}.items():
        assert 0 < data["prepared_heat_limit_inputs"][key] <= ceiling
        assert 0 < data["applied_heat_limit_echoes"][key] <= ceiling


def test_hardware_tmax_is_authoritative_not_a_175_fallback(rig):
    rig.esi.maxima["max_temperature_c"] = 150.
    rig.run.run()
    assert "Tmax" in " ".join(report(rig.run)["errors"])
    assert not any(c[0] in ("heater_on", "target") for c in rig.calls)


@pytest.mark.parametrize("fault", ["hv_active", "hv_target", "sensor", "overheat", "state", "interlock"])
def test_faults_before_heating_stop_without_hv_activation(rig, fault):
    if fault == "hv_active":
        rig.esi.hv_active = True
    elif fault == "hv_target":
        rig.esi.hv_target = 100.
    elif fault == "sensor":
        rig.esi.sensor_valid = False
    elif fault == "overheat":
        rig.esi.temperature = 175.1
    elif fault == "state":
        rig.esi.main_state = "STATE_ERR_ILOCK"
    else:
        rig.esi.device_state = "0x1"
    rig.run.run()
    assert report(rig.run)["outcome"] == "error"
    assert ("heater_on",) not in rig.calls
    assert ("heater_off",) in rig.calls


@pytest.mark.parametrize("kind", ["pressure", "esi", "sensor", "overheat", "external_target", "external_limits", "interrupt"])
def test_running_faults_stop_and_preserve_partial_csv_json(ns, rig, kind):
    if kind == "pressure":
        rig.pressure.failure_at = 6
    elif kind == "interrupt":
        rig.clock.interrupt_at = 5
    else:
        def hook(device):
            if rig.clock() < 5:
                return
            if kind == "esi":
                raise RuntimeError("ESI read failed")
            if kind == "sensor":
                device.sensor_valid = False
            elif kind == "overheat":
                device.temperature = 175.01
            elif kind == "external_target":
                device.target += 1
            elif kind == "external_limits":
                device.limits["power_limit_w"] += 1
        rig.esi.diag_hook = hook
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == ("interrupted" if kind == "interrupt" else "error")
    assert metadata["shutdown_confirmed"] and rig.pressure.closed and not ns["_PT_GUARD"]
    assert metadata["samples"] == len(rows(rig.run)) > 3
    assert rig.calls.count(("heater_on",)) == 1
    assert len([c for c in rig.calls if c[0] == "target"]) == 1
    assert rig.calls.index(("heater_off",)) < rig.calls.index(("disconnect",))
    if kind == "pressure":
        assert rig.pressure.count == 6, "No pressure retry after an acquisition error"
    if kind == "sensor":
        assert any(math.isnan(float(r["temperature_c"])) and r["error"] for r in rows(rig.run))


@pytest.mark.parametrize("phase", ["connect", "diagnostics", "target", "heater_on", "heater_off", "disconnect"])
def test_poisoned_native_call_is_never_followed_by_any_esi_command(ns, rig, phase):
    def poison():
        rig.esi._transport_poisoned = True
        return RuntimeError("Simulated timed-out DLL call")
    rig.esi.failures[phase] = poison
    rig.run.run()
    assert report(rig.run)["outcome"] == "shutdown_unconfirmed"
    assert not rig.esi.calls_after_poison
    assert rig.run.guard_path.exists() and ns["_PT_GUARD"]["run"] is rig.run
    with pytest.raises(RuntimeError, match="Previous run"):
        rig.run.run()


def test_interrupted_native_call_keeps_guard_across_cell_reexecution(ns, rig):
    def poison():
        rig.esi._transport_poisoned = True
        return KeyboardInterrupt("Interrupt while DLL worker is still active")
    rig.esi.failures["diagnostics"] = poison
    rig.run.run()
    old = ns["_PT_GUARD"]
    tree = ast.parse(code())
    tree.body.pop()
    exec(compile(tree, str(NOTEBOOK), "exec"), ns)
    assert ns["_PT_GUARD"] is old
    ns["ARM_HEATING"] = True
    with pytest.raises(RuntimeError, match="Unfinished run"):
        ns["main"]()
    assert not rig.esi.calls_after_poison


def test_persistent_guard_survives_new_kernel_and_precedes_runtime_import(ns, rig, monkeypatch):
    rig.esi.failures["disconnect"] = RuntimeError("Discharge not confirmed")
    rig.run.run()
    ns["_PT_GUARD"].clear()  # Simulate loss of RAM after a kernel restart.
    def load(path, **kwargs):
        assert Path(path).name == "_experiment_guard.py", "No instrument runtime may load before recovery"
        return SimpleNamespace(ExperimentGuard=lambda **kwargs: SimulatedGuard(**kwargs))
    ns.update(ARM_HEATING=True, OUTPUT_DIR=rig.run.output_dir,
              find_plugins=lambda *a: ROOT, load_private=load)
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="Unfinished"):
        ns["main"]()


def test_pressure_continues_during_discharge_without_stale_temperatures(rig):
    flushed = threading.Event()
    original = rig.run.append
    def append(row):
        original(row)
        if row["phase"] == "shutdown":
            flushed.set()
    rig.run.append = append
    def discharge():
        assert flushed.wait(1)
        assert rig.calls.index(("heater_off",)) < rig.calls.index(("disconnect",))
        assert any(row["phase"] == "shutdown" for row in rig.run.rows)
    rig.esi.disconnect_hook = discharge
    rig.run.run()
    assert report(rig.run)["shutdown_confirmed"]
    assert all(math.isnan(row["temperature_c"]) for row in rig.run.rows if row["phase"] == "shutdown")


def test_pressure_failure_during_discharge_cannot_prevent_esi_off(rig):
    def read_hook(pressure):
        if pressure.is_shutdown():
            raise RuntimeError("Pressure failed during ESI discharge")
    rig.pressure.read_hook = read_hook
    rig.esi.disconnect_hook = lambda: rig.pressure.read_on_shutdown.wait(1)
    rig.run.run()
    assert report(rig.run)["shutdown_confirmed"]
    assert ("disconnect",) in rig.calls and ("heater_off",) in rig.calls


def test_blocked_pressure_does_not_delay_off_or_get_concurrent_close(ns, rig, monkeypatch, capsys):
    release = threading.Event()
    def read_hook(pressure):
        if pressure.is_shutdown():
            release.wait(5)
    rig.pressure.read_hook = read_hook
    original = threading.Thread.join
    monkeypatch.setattr(ns["CallOwner"], "PRESSURE_CLOSE_WAIT_S", .03)
    try:
        rig.run.run()
        assert ("heater_off",) in rig.calls and ("disconnect",) in rig.calls
        assert ("pressure_close",) not in rig.calls
        assert report(rig.run)["outcome"] == "shutdown_unconfirmed"
        assert ns["_PT_GUARD"]
        frozen_csv = (rig.run.directory / "samples.csv").read_bytes()
    finally:
        release.set()
        original(rig.run.pressure_thread, 2)
        original(rig.run.pressure_owner.thread, 2)
        rig.run.stream.close()
    assert (rig.run.directory / "samples.csv").read_bytes() == frozen_csv
    assert report(rig.run)["samples"] == len(rows(rig.run))
    text = capsys.readouterr().out
    assert "ESI OFF/discharge verified; pressure link closure unconfirmed" in text
    assert "heater/HV may remain energized" not in text


@pytest.mark.parametrize("status", range(1, 7))
@pytest.mark.parametrize("valid_index", range(6))
def test_any_single_valid_gauge_permits_heating_despite_other_statuses(rig, status, valid_index):
    rig.pressure.statuses = tuple(0 if index == valid_index else status for index in range(6))
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"
    assert ("heater_on",) in rig.calls
    for row in rows(rig.run):
        for index in range(6):
            assert int(row[f"G{index + 1}_status_code"]) == rig.pressure.statuses[index]
            pressure = float(row[f"P{index + 1}_mbar"])
            if index == valid_index:
                assert pressure == rig.pressure.values[index]
            else:
                assert math.isnan(pressure)


@pytest.mark.parametrize("valid_index", range(6))
def test_gauge_faults_during_heating_do_not_choose_an_implicit_critical_input(rig, valid_index):
    def fault_other_gauges(pressure):
        if rig.esi.active:
            pressure.statuses = tuple(0 if index == valid_index else index + 1 for index in range(6))
    rig.pressure.read_hook = fault_other_gauges
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"
    assert len([call for call in rig.calls if call[0] == "target"]) == len(rig.run.program)
    for row in rows(rig.run):
        if row["phase"] == "heating":
            for index in range(6):
                assert int(row[f"G{index + 1}_status_code"]) == (0 if index == valid_index else index + 1)
                assert math.isfinite(float(row[f"P{index + 1}_mbar"])) == (index == valid_index)


@pytest.mark.parametrize("statuses", [(status,) * 6 for status in range(1, 7)] + [(1, 2, 3, 4, 5, 6)])
@pytest.mark.parametrize("when", ["initial", "heating"])
def test_total_pressure_loss_refuses_or_stops_heating(rig, statuses, when):
    def lose_all(pressure):
        if when == "initial" or rig.esi.active:
            pressure.statuses = statuses
    rig.pressure.read_hook = lose_all
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "error" and metadata["shutdown_confirmed"]
    assert "No valid pressure among the six gauges" in " ".join(metadata["errors"])
    assert (("heater_on",) in rig.calls) == (when == "heating")
    assert len([call for call in rig.calls if call[0] == "target"]) == (1 if when == "heating" else 0)
    row = rows(rig.run)[-1]
    assert tuple(int(row[f"G{index + 1}_status_code"]) for index in range(6)) == statuses
    assert all(math.isnan(float(row[f"P{index + 1}_mbar"])) for index in range(6))


def test_absent_gauges_and_nonpositive_valid_pressure_are_not_fabricated(rig):
    rig.pressure.statuses = (0, 0, 1, 4, 5, 0)
    rig.pressure.values = (-1e-6, 0., 999., 999., 999., 2e-6)
    rig.pressure.source_unit = "Pa"  # Runtime values have ALREADY been converted to mbar.
    rig.run.run()
    saved = rows(rig.run)
    assert report(rig.run)["outcome"] == "complete"
    assert float(saved[0]["P1_mbar"]) == -1e-6 and float(saved[0]["P2_mbar"]) == 0
    assert all(math.isnan(float(saved[0][f"P{i}_mbar"])) for i in (3, 4, 5))
    assert float(saved[0]["P6_mbar"]) == 2e-6 and saved[0]["pressure_source_unit"] == "Pa"


def test_each_sample_is_flushed_before_the_next_read(rig):
    def hook(pressure):
        if pressure.count >= 2:
            assert len(rows(rig.run)) >= pressure.count - 1
    rig.pressure.read_hook = hook
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"


def test_csv_write_failure_requests_off_before_finalization(rig):
    original = rig.run.append
    def fail(row):
        if rig.clock() >= 5:
            raise OSError("Disk full during append")
        original(row)
    rig.run.append = fail
    rig.run.run()
    assert report(rig.run)["shutdown_confirmed"]
    assert "Disk full" in " ".join(report(rig.run)["errors"])
    assert ("heater_off",) in rig.calls and ("disconnect",) in rig.calls


def test_rerun_after_confirmed_shutdown_is_allowed_and_never_overwrites(rig):
    rig.run.run()
    first = rig.run.directory
    prior = (first / "samples.csv").read_bytes()
    # A fresh run, like reexecuting the notebook cell after successful shutdown.
    run = type(rig.run)(rig.run.output_dir, lambda directory: rig.esi, lambda: rig.pressure,
                        guard=SimulatedGuard(rig.run.output_dir), baseline_s=1., hold_s=1., cooling_s=1., stability_factory=rig.run.stability_factory,
                        limit_preparer=rig.run.limit_preparer,
                        clock=rig.clock, sleep=rig.clock.sleep, plots_factory=NoPlots)
    run.run()
    assert run.directory != first and (first / "samples.csv").read_bytes() == prior


@pytest.mark.parametrize("delta", [math.nextafter(30., math.inf) - 30., math.nextafter(30., -math.inf) - 30., .001, 140., -.2, -.001, -.2000001, -29.99, math.nan, math.inf])
def test_out_of_stage_temperature_echo_cannot_authorize_first_on(rig, delta):
    rig.esi.quantum = delta
    rig.run.run()
    assert ("target", 30.) in rig.calls and ("heater_on",) not in rig.calls
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]
    assert not report(rig.run)["stages"][0]["complete"]


def test_exact_applied_temperature_preserves_nominal_qualification(rig):
    rig.esi.quantum = 0.
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "complete"
    assert data["stages"][0]["target_c"] == 30.
    assert data["stages"][0]["applied_target_c"] == 30.


def test_out_of_stage_echo_during_heating_stops_before_any_next_stage(rig):
    def target_call():
        if rig.run.requested == 40.:
            rig.esi.quantum = .001
    rig.esi.failures["target"] = target_call
    rig.run.run()
    assert [call[1] for call in rig.calls if call[0] == "target"] == [30., 40.]
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]
    assert not report(rig.run)["stages"][-1]["complete"]


def test_mismatched_temperature_echo_is_preserved_in_error_report(rig):
    rig.esi.quantum = -.001
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "error"
    assert data["stages"][0]["target_c"] == 30.
    assert data["stages"][0]["applied_target_c"] == 29.999
    assert ("heater_on",) not in rig.calls


def test_loader_discovers_parent_or_plugins_dir_and_preserves_sys_path(ns, tmp_path):
    work = tmp_path / "work/notebooks"
    work.mkdir(parents=True)
    bundle = tmp_path / "work/plugins"
    (bundle / "esi/vendor/runtime").mkdir(parents=True)
    (bundle / "esi/vendor/runtime/__init__.py").write_text("answer = 42\n")
    (bundle / "tpg366/_runtime").mkdir(parents=True)
    (bundle / "tpg366/_runtime/_tpg366.py").write_text("answer = 43\n")
    assert ns["find_plugins"](notebook_dir=work) == bundle
    assert ns["find_plugins"](bundle, notebook_dir=tmp_path) == bundle
    with pytest.raises(FileNotFoundError):
        ns["find_plugins"](tmp_path / "wrong", notebook_dir=ROOT)
    before = sys.path.copy()
    finders_before = sys.meta_path.copy()
    path = bundle / "esi/vendor/runtime/__init__.py"
    assert ns["load_private"](path, package=True).answer == 42
    assert ns["load_private"](path, package=True).__name__.startswith("_esibd_bundled_")
    assert sys.path == before and sys.meta_path == finders_before


def test_real_plot_render_uses_measured_temperature_and_retains_nan_gaps(ns, rig, monkeypatch, tmp_path):
    import matplotlib
    matplotlib.use("Agg", force=True)
    rig.run.plots_factory = ns["LivePlots"]
    rig.pressure.statuses = (0, 0, 0, 0, 0, 5)
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"
    for name in ("pressure_temperature_time.png", "pressure_vs_temperature.png"):
        image = rig.run.directory / name
        assert image.is_file() and image.stat().st_size > 15000
    scatter = rig.run.plots.temperature_axis.lines[0]
    assert list(scatter.get_xdata()) == pytest.approx([row["temperature_c"] for row in rig.run.rows], nan_ok=True)
    assert all(math.isnan(v) for v in rig.run.plots.temperature_axis.lines[5].get_ydata())


@pytest.mark.parametrize("method", ["collect_diagnostics", "set_heater_temperature", "set_output_active"])
@pytest.mark.parametrize("stop_kind", ["request", "sigint"])
def test_stop_during_native_heater_readiness_prevents_late_commands(
        ns, native_esi, tmp_path, monkeypatch, method, stop_kind):
    """Real runtime and (optionally) SIGINT; all native functions are simulated."""
    import os
    import signal
    import time

    native_esi.state.target = 0.
    native_esi.driver._verify_hv_discharge = lambda *a, **k: native_esi.calls.append(("verified_discharge",))
    clock, calls = Clock(), []
    pressure = FakePressure(calls, clock)
    run = ns["Experiment"](tmp_path / "cancel", lambda directory: native_esi.driver, lambda: pressure, limit_preparer=ns["bounded_limit"],
                           guard=SimulatedGuard(tmp_path / "cancel"), baseline_s=1., hold_s=1., targets=(25.,), cooling_s=1.,
                           stability_factory=lambda: ns["TemperatureStability"](window_s=2.), clock=clock,
                           sleep=clock.sleep, plots_factory=NoPlots)
    original = getattr(native_esi.driver, method)
    watching = False
    stopped = False

    def watched(*args, **kwargs):
        nonlocal watching
        watching = method != "set_output_active" or args[1] is True
        try:
            return original(*args, **kwargs)
        finally:
            watching = False

    def flags(self, address):
        nonlocal stopped
        if watching and not stopped:
            stopped = True
            native_esi.calls.append(("stop",))
            if stop_kind == "request":
                run.request_stop()
            else:
                os.kill(os.getpid(), signal.SIGINT)
                deadline = time.monotonic() + 2.
                while run.esi_owner.cleanup is None and time.monotonic() < deadline:
                    time.sleep(.001)
                assert run.esi_owner.cleanup is not None, "SIGINT must queue cleanup"
            return 0, 0  # Not ready at Stop; a later ready bit must not resume the command.
        return 0, 1

    monitoring = native_esi.base.get_heat_ctrl_monitoring
    def monitored(self):
        native_esi.calls.append(("monitor",))
        return monitoring(self)

    monkeypatch.setattr(native_esi.driver, method, watched)
    monkeypatch.setattr(native_esi.base, "get_module_data_ready_flags", flags)
    monkeypatch.setattr(native_esi.base, "get_heat_ctrl_monitoring", monitored)
    run.run()
    assert stopped
    following = native_esi.calls[native_esi.calls.index(("stop",)) + 1:]
    assert ("monitor",) not in following
    assert not any(call[0] == "temperature" and call[1] > 0 for call in following)
    assert ("module", 0, True) not in following and ("enable", True) not in following
    assert ("module", 0, False) in following and ("close",) in following
    assert report(run)["shutdown_confirmed"]
    assert native_esi.driver._transport_poisoned is False
    assert not ns["_PT_GUARD"]


# Real high-level ESI implementation above a stateful fake native DLL.
from test_esi_heater_activation import rig as _native_esi  # noqa: E402,F401


@pytest.fixture
def native_esi(_native_esi, monkeypatch):
    # Simulate a thermally following load and native limit echoes; never operate an instrument.
    for key in ("voltage", "current", "power"):
        def setter(self, value, key=key):
            _native_esi.state.limits[key] = value
            return 0, value
        monkeypatch.setattr(_native_esi.base, f"set_heat_ctrl_{key}_limit", setter)
    def monitoring(self):
        if _native_esi.state.target > 0:
            _native_esi.state.temperature = _native_esi.state.target - .1
        return 0, _native_esi.state.sensor_valid, 0., 0., 0., _native_esi.state.temperature
    monkeypatch.setattr(_native_esi.base, "get_heat_ctrl_monitoring", monitoring)
    return _native_esi


@pytest.mark.parametrize("stop_after", ["voltage", "current", "power"])
def test_stop_during_real_native_limit_write_prevents_remaining_writes(ns, native_esi, tmp_path, monkeypatch, stop_after):
    clock, calls = Clock(), []
    pressure = FakePressure(calls, clock)
    native_esi.state.target = 0.
    native_esi.driver._verify_hv_discharge = lambda *a, **k: native_esi.calls.append(("verified_discharge",))
    run = ns["Experiment"](tmp_path / "limits_stop", lambda directory: native_esi.driver, lambda: pressure, limit_preparer=ns["bounded_limit"],
                           guard=SimulatedGuard(tmp_path / "limits_stop"), baseline_s=1., hold_s=1., cooling_s=1.,
                           stability_factory=lambda: ns["TemperatureStability"](window_s=2.),
                           clock=clock, sleep=clock.sleep, plots_factory=NoPlots)
    writes = []
    for key in ("voltage", "current", "power"):
        def setter(self, value, key=key):
            writes.append(key)
            native_esi.state.limits[key] = value
            if key == stop_after:
                run.request_stop()
            return 0, value
        monkeypatch.setattr(native_esi.base, f"set_heat_ctrl_{key}_limit", setter)
    run.run()
    assert writes == ["voltage", "current", "power"][:["voltage", "current", "power"].index(stop_after) + 1]
    assert ("module", 0, True) not in native_esi.calls
    assert ("close",) in native_esi.calls and report(run)["shutdown_confirmed"]
    assert native_esi.driver._transport_poisoned is False


@pytest.mark.parametrize("use_facade", [False, True])
def test_real_esi_runtime_contract_with_simulated_native_calls(ns, native_esi, tmp_path, use_facade):
    native_esi.state.target = 0.
    native_esi.state.temperature = 21.08
    native_esi.driver._verify_hv_discharge = lambda *a, **k: native_esi.calls.append(("verified_discharge",))
    clock, calls = Clock(), []
    pressure = FakePressure(calls, clock)
    device = native_esi.driver
    if use_facade:
        facade_type = sys.modules[type(device).__module__].ESI
        facade = object.__new__(facade_type)
        object.__setattr__(facade, "_backend", device)
        object.__setattr__(facade, "_backend_mode", "inline")
        assert facade._transport_poisoned is False
        device = facade
    run = ns["Experiment"](tmp_path / "native", lambda directory: device, lambda: pressure, limit_preparer=ns["bounded_limit"],
                           guard=SimulatedGuard(tmp_path / "native"), baseline_s=1., hold_s=1., cooling_s=1.,
                           stability_factory=lambda: ns["TemperatureStability"](window_s=2.),
                           clock=clock, sleep=clock.sleep, plots_factory=NoPlots)
    run.run()
    assert report(run)["outcome"] == "complete"
    assert ("module", 0, True) in native_esi.calls
    assert not any(call[:2] in (("module", 1), ("module", 2)) and call[2] is True for call in native_esi.calls)
    assert ("verified_discharge",) in native_esi.calls
    assert native_esi.calls.index(("verified_discharge",)) < native_esi.calls.index(("close",))
    assert native_esi.state.enabled is False and native_esi.state.active[0] is False


from test_esi_discharge import rig as native_discharge  # noqa: E402,F401
from test_tpg366_protocol import FakeSerial  # noqa: E402


@pytest.mark.parametrize("fault", [None, "sticky", "high_voltage", "invalid"])
def test_real_discharge_verifier_requires_fresh_low_values(ns, rig, native_discharge, fault):
    if fault == "sticky":
        native_discharge.state.sticky = True
    elif fault == "high_voltage":
        native_discharge.values[2, True] = [-100.]
    elif fault == "invalid":
        native_discharge.state.invalid = "voltage"
    def disconnect(**kwargs):
        rig.esi.call("disconnect")
        return native_discharge.driver.disconnect(**kwargs)
    rig.esi.disconnect = disconnect
    rig.run.run()
    metadata = report(rig.run)
    if fault is None:
        assert metadata["shutdown_confirmed"] and ("close",) in native_discharge.calls
        assert metadata["last_discharge"]["consecutive"] == 3
        assert metadata["last_discharge"]["limit_v"] == 1
        assert all(abs(values[key]) <= 1 for values in metadata["last_discharge"]["modules"].values()
                   for key in ("positive_v", "negative_v"))
    else:
        assert not metadata["shutdown_confirmed"] and ("close",) not in native_discharge.calls
        assert ns["_PT_GUARD"] and rig.run.guard_path.exists()
    assert not any(call == ("gate", True) for call in native_discharge.calls)


@pytest.mark.parametrize("fault", [None, "partial_gauges", "all_invalid", "nak", "unit_change", "initialize", "open", "close"])
def test_real_pressure_protocol_and_serial_settings(ns, rig, monkeypatch, fault):
    statuses = (0, 2, 3, 4, 5, 6) if fault == "partial_gauges" else (1, 2, 3, 4, 5, 6) if fault == "all_invalid" else (0,) * 6
    serial = FakeSerial(unit=2, statuses=statuses)  # Instrument reports Pa; runtime converts to mbar.
    settings = {}
    serial.is_open = False
    def open_serial():
        settings["opened_port"] = serial.port
        serial.is_open = True
        if fault == "open":
            raise OSError("Simulated OS opening error after handle creation")
    serial.open = open_serial
    def open_port(port, **kwargs):
        assert port is None, "Own the serial object BEFORE the potentially blocked OS open"
        settings.update(**kwargs)
        return serial
    monkeypatch.setitem(sys.modules, "serial", SimpleNamespace(Serial=open_port))
    protocol = ns["load_private"](ROOT / "tpg366/_runtime/_tpg366.py")
    pressure = ns["PressurePort"](protocol)
    rig.run.pressure_factory = lambda: pressure
    if fault == "nak":
        serial.on_write = lambda data: setattr(serial, "ack", b"\x15\r\n") if data == b"PRX\r" else None
    elif fault == "unit_change":
        serial.units.extend(["2", "0"])
    elif fault == "initialize":
        serial.responses["AYT"] = "Wrong device"
    elif fault == "close":
        serial.fail_close = True
    rig.run.run()
    assert settings == {"opened_port": "COM21", "baudrate": 9600, "bytesize": 8, "parity": "N", "stopbits": 1,
                        "timeout": .05, "write_timeout": 1., "xonxoff": False, "rtscts": False, "dsrdtr": False}
    assert serial.close_count == 1
    if fault in (None, "partial_gauges"):
        assert report(rig.run)["outcome"] == "complete"
        assert float(rows(rig.run)[0]["P1_mbar"]) == pytest.approx(.12345 * .01)
        assert rows(rig.run)[0]["pressure_source_unit"] == "Pa"
        if fault == "partial_gauges":
            for row in rows(rig.run):
                assert tuple(int(row[f"G{index + 1}_status_code"]) for index in range(6)) == statuses
                assert all(math.isnan(float(row[f"P{index}_mbar"])) for index in range(2, 7))
    else:
        assert report(rig.run)["outcome"] != "complete"
        if fault != "close":
            assert ("heater_on",) not in rig.calls
            assert serial.writes.count(b"AYT\r") == (0 if fault == "open" else 1)  # No replay/reconnect.
        else:
            assert ns["_PT_GUARD"]


@pytest.mark.parametrize("field", ["baseline_s", "hold_s", "sample_s"])
@pytest.mark.parametrize("value", [0., -1., math.nan, True])
def test_invalid_durations_are_refused_before_access(rig, field, value):
    setattr(rig.run, field, value)
    with pytest.raises(ValueError):
        rig.run.run()
    assert not rig.calls


def test_slow_reads_keep_actual_timestamps_without_catchup_bursts(rig):
    def slow(pressure):
        if not pressure.is_shutdown():
            rig.clock.sleep(1.2)
    rig.pressure.read_hook = slow
    rig.run.run()
    observed = [row["elapsed_s"] for row in rig.run.rows if row["phase"] != "shutdown"]
    assert all(b - a >= 1.19 for a, b in zip(observed, observed[1:]))


def test_each_instrument_keeps_one_owner_including_shutdown(rig):
    main_thread = threading.get_ident()
    rig.esi.disconnect_hook = lambda: rig.pressure.read_on_shutdown.wait(1)
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete"
    assert set(rig.esi.thread_ids) == {rig.run.esi_owner.thread.ident}
    assert set(rig.pressure.thread_ids) == {rig.run.pressure_owner.thread.ident}
    assert main_thread not in rig.esi.thread_ids + rig.pressure.thread_ids
    assert set(rig.esi.thread_ids).isdisjoint(rig.pressure.thread_ids)


def test_second_interrupt_during_shutdown_still_saves_json_and_blocks_rerun(ns, rig):
    rig.clock.interrupt_at = 5
    rig.esi.failures["disconnect"] = KeyboardInterrupt("Second interruption")
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "shutdown_unconfirmed"
    assert sum("KeyboardInterrupt" in error for error in metadata["errors"]) == 2
    assert ns["_PT_GUARD"] and len(rows(rig.run)) >= 3


def test_old_esi_runtime_refused_before_constructor(ns):
    class OldESI:
        _PROCESS_CONTROLLER_CLASS = object
        def __init__(self, *a, **k):
            pytest.fail("An old runtime must not be constructed")
    with pytest.raises(RuntimeError, match="heater safety"):
        ns["require_safe_esi"](SimpleNamespace(ESI=OldESI))
    runtime = ns["load_private"](ROOT / "esi/vendor/runtime/__init__.py", package=True)
    ns["require_safe_esi"](runtime)


def test_changed_or_failed_package_is_not_silently_reused(ns, tmp_path):
    package = tmp_path / "package"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("value = 42\n")
    ns["load_private"](init, package=True)
    (package / "child.py").write_text("value = 43\n")
    with pytest.raises(RuntimeError, match="restart the kernel"):
        ns["load_private"](init, package=True)
    failed = tmp_path / "failed.py"
    failed.write_text("raise ImportError('missing dependency')\n")
    finders_before = sys.meta_path.copy()
    with pytest.raises(ImportError, match="missing dependency"):
        ns["load_private"](failed)
    assert sys.meta_path == finders_before
    with pytest.raises(RuntimeError, match="restart the kernel"):
        ns["load_private"](failed)


def test_full_armed_main_uses_ports_runtime_guards_and_real_implementation(ns, rig, monkeypatch):
    class ControllerFeatures:
        def _set_heat_module_active_unlocked(self):
            pass
        def _validate_heat_operating_state_unlocked(self):
            pass
    class ESIFactory:
        _PROCESS_CONTROLLER_CLASS = ControllerFeatures
        def __new__(cls, device_id, com, **kwargs):
            assert com == 16 and kwargs["baudrate"] == 230400 and kwargs["process_backend"] is False
            return rig.esi
    checks = []
    def lease(**kwargs):
        assert kwargs["kernel_globals"] is ns and not ns["_PT_GUARD"]
        assert kwargs["plugin_dir"] == ROOT / "esi" and kwargs["com"] == 16
        checks.append(("lease",))
        return SimulatedGuard(**kwargs)
    def load(path, package=False, fresh=False):
        if package:
            assert fresh is True
        checks.append(("load", Path(path).name))
        if package:
            return SimpleNamespace(ESI=ESIFactory)
        if Path(path).name == "_experiment_guard.py":
            return SimpleNamespace(ExperimentGuard=lease)
        if Path(path).name == "_heater_stability.py":
            return SimpleNamespace(TemperatureStability=ns["TemperatureStability"])
        if Path(path).name == "_heater_limits.py":
            return SimpleNamespace(bounded_limit=ns["bounded_limit"])
        return SimpleNamespace()
    def require(runtime):
        assert runtime.ESI is ESIFactory
        checks.append(("runtime_checked",))
    original = ns["Experiment"]
    def experiment(output, esi_factory, pressure_factory, **kwargs):
        kwargs["stability_factory"] = rig.run.stability_factory
        return original(output, esi_factory, pressure_factory, baseline_s=1., hold_s=1., cooling_s=1.,
                        clock=rig.clock, sleep=rig.clock.sleep, plots_factory=NoPlots, **kwargs)
    ns.update(ARM_HEATING=True, OUTPUT_DIR=rig.run.output_dir, find_plugins=lambda *a: ROOT,
              load_private=load, require_safe_esi=require, PressurePort=lambda *a: rig.pressure, Experiment=experiment)
    monkeypatch.setattr(sys, "platform", "win32")
    run = ns["main"]()
    assert report(run)["outcome"] == "complete"
    assert report(run)["runtime_sources"] == ns["runtime_fingerprints"](ROOT)
    assert set(report(run)["runtime_sources"]) == set(ns["_RUNTIME_FILES"])
    assert checks == [("load", "_experiment_guard.py"), ("lease",), ("load", "__init__.py"),
                      ("runtime_checked",), ("load", "_tpg366.py"), ("load", "_heater_stability.py"), ("load", "_heater_limits.py")]
    assert rig.calls.count(("heater_on",)) == 1


def test_disarmed_cell_warns_that_previous_unconfirmed_heating_is_not_switched_off(ns, capsys):
    ns["_PT_GUARD"]["run"] = object()
    ns["main"]()
    assert "disarming this cell does not switch hardware OFF" in capsys.readouterr().out


def test_false_disconnect_result_cannot_clear_uncertainty(ns, rig):
    rig.esi.disconnect = lambda **kwargs: False
    rig.run.run()
    assert report(rig.run)["outcome"] == "shutdown_unconfirmed"
    assert ns["_PT_GUARD"] and rig.run.guard_path.exists()


@pytest.mark.parametrize("error", [KeyboardInterrupt("Before hardware"), OSError("Storage unavailable")])
def test_storage_preflight_failure_or_interrupt_does_not_leave_a_false_hardware_guard(ns, rig, error):
    original = ns["save_json"]
    attempts = []
    def save(path, data):
        attempts.append(path)
        if len(attempts) == 1:
            raise error
        return original(path, data)
    ns["save_json"] = save
    with pytest.raises(type(error)):
        rig.run.run()
    assert not rig.calls and not ns["_PT_GUARD"] and not rig.run.guard_path.exists()
    metadata = report(rig.run)
    assert metadata["esi_instance_created"] is False and metadata["shutdown_confirmed"] is False
    assert metadata["ended_utc"]


def test_zero_previous_limits_are_replaced_by_approved_values_and_reported(rig, capsys):
    rig.esi.limits = dict.fromkeys(rig.esi.limits, 0.)
    rig.run.run()
    text = capsys.readouterr().out
    assert "0 V / 0 A / 0 W" in text
    assert report(rig.run)["outcome"] == "complete"
    prepare = rig.run.limit_preparer
    assert rig.esi.limits == dict(voltage_limit_v=prepare(22.), current_limit_a=prepare(10.), power_limit_w=prepare(50., power=True))


def test_local_off_failure_still_attempts_verified_global_shutdown(ns, rig):
    rig.esi.failures["heater_off"] = RuntimeError("Local heater OFF rejected")
    rig.run.run()
    assert ("disconnect",) in rig.calls
    assert report(rig.run)["shutdown_confirmed"] and not ns["_PT_GUARD"]
    assert report(rig.run)["outcome"] == "error"


def test_final_plot_error_is_reported_after_outputs_are_off(rig):
    class BrokenSave(NoPlots):
        def save(self, directory, data):
            assert not rig.esi.active and rig.pressure.closed
            raise OSError("PNG could not be saved")
    rig.run.plots_factory = BrokenSave
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "error" and metadata["shutdown_confirmed"]
    assert "PNG could not be saved" in " ".join(metadata["errors"])


@pytest.mark.parametrize("condition", ["zero_maximum", "all_nonpositive", "all_missing"])
def test_real_figures_preserve_empty_or_unplottable_data_without_fabrication(ns, rig, condition):
    import matplotlib
    matplotlib.use("Agg", force=True)
    rig.run.plots_factory = ns["LivePlots"]
    if condition == "zero_maximum":
        rig.esi.maxima["max_power_w"] = 0.
    elif condition == "all_nonpositive":
        rig.pressure.values = (0., -1., 0., -2., 0., -3.)
    else:
        rig.pressure.statuses = (5,) * 6
    rig.run.run()
    assert (rig.run.directory / "pressure_temperature_time.png").is_file()
    assert (rig.run.directory / "pressure_vs_temperature.png").is_file()
    assert all(not len(line.get_ydata()) or all(math.isnan(y) for y in line.get_ydata())
               for line in rig.run.plots.axes[0].lines)
    assert report(rig.run)["shutdown_confirmed"]


def test_native_applied_limit_echoes_then_independent_getters(rig):
    requested = {"voltage_v": 5., "current_a": 1., "power_w": 10.}
    applied = {"voltage_v": 5.000064, "current_a": .999936, "power_w": 9.999872}
    mapping = {"voltage_v": "voltage_limit_v", "current_a": "current_limit_a", "power_w": "power_limit_w"}
    def configure(*, cancel_event=None, **kwargs):
        rig.esi.check_cancel(cancel_event)
        rig.esi.call("limits", kwargs)
        rig.esi.limits.update({mapping[key]: value for key, value in applied.items()})
        return dict(applied)
    rig.esi.configure_heat_limits = configure
    rig.run.overrides = requested
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "complete"
    assert metadata["requested_heat_limits"] == {mapping[key]: value for key, value in requested.items()}
    assert metadata["applied_heat_limit_echoes"] == applied
    assert all(metadata["applied_heat_configuration"][mapping[key]] == value for key, value in applied.items())


@pytest.mark.parametrize("key", ["voltage_v", "current_a", "power_w"])
@pytest.mark.parametrize("value", [0., -1., math.nan, math.inf, 999., True, None])
def test_invalid_applied_echo_never_authorizes_heating(rig, key, value):
    rig.run.overrides = {"voltage_v": 5., "current_a": 1., "power_w": 10.}
    rig.esi.configure_heat_limits = lambda cancel_event=None, **kwargs: {**kwargs, key: value}
    rig.run.run()
    assert report(rig.run)["outcome"] == "error"
    assert report(rig.run)["shutdown_confirmed"]
    assert ("heater_on",) not in rig.calls


def test_boolean_getter_cannot_confirm_a_numeric_applied_limit(rig):
    rig.run.overrides = {"voltage_v": None, "current_a": 1., "power_w": None}
    def configure(*, cancel_event=None, **kwargs):
        rig.esi.check_cancel(cancel_event)
        rig.esi.limits["current_limit_a"] = True
        return {"current_a": 1.}
    rig.esi.configure_heat_limits = configure
    rig.run.run()
    assert report(rig.run)["outcome"] == "error"
    assert ("heater_on",) not in rig.calls


def test_applied_echo_without_independent_matching_getter_is_refused(rig):
    rig.run.overrides = {"voltage_v": 5., "current_a": None, "power_w": None}
    rig.esi.configure_heat_limits = lambda **kwargs: {"voltage_v": 5.000064}
    rig.run.run()
    assert "readback mismatch" in " ".join(report(rig.run)["errors"])
    assert ("heater_on",) not in rig.calls


@pytest.mark.parametrize("fault", ["absent", "none", "raises"])
def test_unknown_poison_state_never_allows_automatic_cleanup(ns, rig, fault):
    def corrupt():
        if fault == "absent":
            del rig.esi._transport_poisoned
        elif fault == "none":
            rig.esi._transport_poisoned = None
        else:
            class BrokenState(type(rig.esi)):
                @property
                def _transport_poisoned(self):
                    raise RuntimeError("Unknown transport state")
            rig.esi.__class__ = BrokenState
        raise RuntimeError("Acquisition failed before shutdown")
    rig.esi.failures["diagnostics"] = corrupt
    rig.run.run()
    assert ("heater_off",) not in rig.calls and ("disconnect",) not in rig.calls
    assert "transport unknown" in " ".join(report(rig.run)["errors"])
    assert report(rig.run)["outcome"] == "shutdown_unconfirmed" and ns["_PT_GUARD"]


@pytest.mark.parametrize("value,expected", [(21, "COM21"), ("com021", "COM21"), (" 021 ", "COM21"),
                                            ("/dev/ttyUSB0", "/dev/ttyUSB0")])
def test_tpg_port_normalization(ns, value, expected):
    assert ns["serial_port_name"](value) == expected


def test_pressure_timestamp_is_prx_time_not_delayed_return_time(ns, rig):
    rig.run.run()
    first = rows(rig.run)[0]
    assert first["pressure_observed_utc"] == ns["utc"](1_780_000_000)
    assert first["pressure_observed_utc"] != first["temperature_observed_utc"]


@pytest.mark.parametrize("changed_source", ["esi/vendor/runtime/esi/esi.py", "esi/_experiment_guard.py", "esi/_heater_limits.py"])
def test_updated_sources_are_recorded_not_refused(ns, tmp_path, changed_source):
    # Plugins and notebooks evolve independently; the run records what it used.
    import hashlib
    import shutil
    root = tmp_path / "plugins"
    for relative in ns["_RUNTIME_FILES"]:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    original = ns["runtime_fingerprints"](root)
    (root / changed_source).write_text("# updated dependency\n")
    updated = ns["runtime_fingerprints"](root)
    assert updated[changed_source] == hashlib.sha256(b"# updated dependency\n").hexdigest()
    assert {k: v for k, v in updated.items() if k != changed_source} == {
        k: v for k, v in original.items() if k != changed_source}


def test_capability_guard_checks_actual_imported_class_not_a_valid_candidate_file(ns, tmp_path):
    # Both method names existed in an unsafe intermediate version without the contract.
    source = tmp_path / "old.py"
    source.write_text("class Controller:\n    def _set_heat_module_active_unlocked(self): pass\n"
                      "    def _validate_heat_operating_state_unlocked(self): pass\n")
    init = tmp_path / "__init__.py"
    init.write_text("from .old import Controller\n")
    runtime = ns["load_private"](init, package=True)
    runtime.ESI = SimpleNamespace(_PROCESS_CONTROLLER_CLASS=runtime.Controller)
    assert runtime.Controller.__module__.startswith(runtime.__name__ + ".")
    with pytest.raises(RuntimeError, match="heater safety"):
        ns["require_safe_esi"](runtime)
    runtime.Controller.HEATER_SAFETY_CONTRACT = 1  # a later version keeping the contract
    ns["require_safe_esi"](runtime)


@pytest.mark.parametrize("orphan", [False, True])
def test_disk_replacement_cannot_legitimize_a_cached_old_namespace(ns, tmp_path, orphan):
    package = tmp_path / "runtime"
    package.mkdir()
    path = package / "__init__.py"
    path.write_text("value = 'old code'\n")
    old = ns["load_private"](path, package=True)
    path.write_text("value = 'verified replacement on disk'\n")
    if orphan:
        sys.modules[old.__name__ + ".orphan"] = SimpleNamespace()
        del sys.modules[old.__name__]
    try:
        with pytest.raises(RuntimeError, match="already loaded.*restart the kernel"):
            ns["load_private"](path, package=True, fresh=True)
    finally:
        sys.modules.pop(old.__name__ + ".orphan", None)
        sys.modules.pop(old.__name__, None)


def test_fresh_import_executes_verified_source_not_stale_bytecode(ns, tmp_path):
    import os
    import py_compile
    init = tmp_path / "__init__.py"
    init.write_text("from .child import value\n")
    source = tmp_path / "child.py"
    source.write_text("value = 1\n")
    stamp = source.stat()
    py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.TIMESTAMP)
    source.write_text("value = 2\n")  # Same byte count and preserved timestamp, as with copied/extracted files.
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    runtime = ns["load_private"](init, package=True, fresh=True)
    assert runtime.value == 2, "A source hash cannot authenticate stale bytecode executed instead"


def _run_signal_probe(tmp_path, mode, notebook=NOTEBOOK):
    """Real process SIGINT; all serial/native functions remain simulated."""
    import subprocess
    import textwrap
    target = tmp_path / f"{mode}.json"
    script = textwrap.dedent(r'''
        import ast, json, os, signal, sys, threading, time
        from pathlib import Path
        import pytest
        from test_pressure_temperature_notebook import Clock, FakeESI, FakePressure, NoPlots, SimulatedGuard, report, rows
        from test_esi_heater_activation import rig as native_rig
        from test_tpg366_protocol import FakeSerial
        mode, target, notebook = sys.argv[1:]
        target = Path(target)
        tree = ast.parse(''.join(
            next(c for c in json.loads(Path(notebook).read_text())['cells'] if c['cell_type'] == 'code')['source']))
        tree.body.pop()
        ns = {'__file__': notebook}
        exec(compile(tree, notebook, 'exec'), ns)
        clock, calls = Clock(), []
        pressure = FakePressure(calls, clock)
        entered, release, returned = threading.Event(), threading.Event(), threading.Event()
        run_returned = threading.Event()
        observer_errors = []
        mp = pytest.MonkeyPatch()
        generator = None
        serial = None
        signal.signal(signal.SIGINT, signal.default_int_handler)
        if mode in ('esi_read', 'native_timeout'):
            generator = native_rig.__wrapped__(mp)
            native = next(generator)
            device = native.driver
            native.state.target = 0.
            device._verify_hv_discharge = lambda *a, **k: native.calls.append(('verified_discharge',))
            original = native.base.get_heat_ctrl_monitoring
            def held(self):
                if native.state.active[0] and not entered.is_set():
                    native.calls.append(('held_read',))
                    entered.set()
                    if not release.wait(6):
                        raise RuntimeError('Probe release missing')
                    native.calls.append(('native_return',))
                    returned.set()
                return original(self)
            mp.setattr(native.base, 'get_heat_ctrl_monitoring', held)
            if mode == 'native_timeout':
                diagnostics = device.collect_diagnostics
                device.collect_diagnostics = lambda **kwargs: diagnostics(timeout_s=.01, **kwargs)
        else:
            device = FakeESI(calls)
        stability = ns['load_private'](Path.cwd() / 'esi/_heater_stability.py')
        limits = ns['load_private'](Path.cwd() / 'esi/_heater_limits.py')
        run = ns['Experiment'](target.parent / ('runs_' + mode), lambda directory: device, lambda: pressure,
                               guard=SimulatedGuard(target.parent / ('runs_' + mode)), baseline_s=1., hold_s=2., targets=(25.,), clock=clock, cooling_s=1.,
                               stability_factory=lambda: stability.TemperatureStability(window_s=2.), limit_preparer=limits.bounded_limit,
                               overrides={'voltage_v': None, 'current_a': None, 'power_w': None},
                               sleep=clock.sleep, plots_factory=NoPlots)
        pressure.is_shutdown = lambda: run.phase == 'shutdown'
        if mode == 'esi_connect':
            connect = device.connect
            def held_connect():
                entered.set()
                if not release.wait(6):
                    raise RuntimeError('Probe release missing')
                calls.append(('connect_return',))
                returned.set()
                return connect()
            device.connect = held_connect
        if mode == 'esi_shutdown':
            def held_disconnect():
                entered.set()
                if not release.wait(6):
                    raise RuntimeError('Probe release missing')
                returned.set()
            device.disconnect_hook = held_disconnect
        if mode.startswith('tpg_'):
            serial = FakeSerial()
            serial.is_open = False
            protocol = ns['load_private'](Path.cwd() / 'tpg366/_runtime/_tpg366.py')
            port = ns['PressurePort'](protocol)
            run.pressure_factory = lambda: port
            ns['CallOwner'].PRESSURE_CLOSE_WAIT_S = .05
            def hold():
                entered.set()
                if not release.wait(6):
                    raise RuntimeError('Probe release missing')
                returned.set()
            def open_serial():
                if mode == 'tpg_open':
                    hold()
                serial.is_open = True
            serial.open = open_serial
            if mode == 'tpg_read':
                serial.on_write = lambda data: hold() if data == b'PRX\r' and not entered.is_set() else None
            if mode == 'tpg_close':
                close = serial.close
                def held_close():
                    hold()
                    close()
                serial.close = held_close
            mp.setitem(sys.modules, 'serial', type('SerialModule', (), {'Serial': staticmethod(lambda **kwargs: serial)}))
        def observe():
            try:
                assert entered.wait(4), 'Probe never reached held I/O'
                if mode != 'native_timeout':
                    os.kill(os.getpid(), signal.SIGINT)
                if mode.startswith('tpg_') or mode == 'native_timeout':
                    assert run_returned.wait(4), 'Notebook did not return an uncertainty report'
                    if serial is not None:
                        assert serial.close_count == 0, 'Concurrent/repeated serial Close'
                        assert ('disconnect',) in calls, 'Pressure blocked ESI OFF'
                else:
                    time.sleep(.10)
                    assert not returned.is_set()
                    if mode == 'esi_read':
                        tail = native.calls[native.calls.index(('held_read',)) + 1:]
                        assert not any(c[0] in ('module', 'enable', 'temperature', 'close') for c in tail), tail
                    elif mode == 'esi_shutdown':
                        os.kill(os.getpid(), signal.SIGINT)
                        time.sleep(.05)
                        # One verified OFF enters cooling, a second precedes final discharge.
                        assert calls.count(('heater_off',)) == 2 and calls.count(('disconnect',)) == 1
                    else:
                        assert ('heater_off',) not in calls and ('disconnect',) not in calls
                    release.set()
            except BaseException as error:
                observer_errors.append(repr(error))
                release.set()
        observer = threading.Thread(target=observe, daemon=True)
        observer.start()
        try:
            run.run()
            saved = report(run)
            before_csv = (run.directory / 'samples.csv').read_bytes()
            before_json = (run.directory / 'metadata.json').read_bytes()
            run_returned.set()
            observer.join(5)
            assert not observer.is_alive(), 'Observer did not finish'
            release.set()
            returned.wait(2)
            if getattr(run, 'pressure_owner', None) is not None:
                run.pressure_owner.thread.join(2)
            time.sleep(.05)
            result = {'mode': mode, 'simulated': True, 'report': saved,
                      'poisoned': device._transport_poisoned, 'observer_errors': observer_errors,
                      'guard_retained': bool(ns['_PT_GUARD']),
                      'late_csv_unchanged': before_csv == (run.directory / 'samples.csv').read_bytes(),
                      'late_json_unchanged': before_json == (run.directory / 'metadata.json').read_bytes(),
                      'calls': native.calls if generator else calls,
                      'signal_handler_restored': signal.getsignal(signal.SIGINT) is signal.default_int_handler}
            if serial is not None:
                result.update(serial_closed=not serial.is_open, serial_close_count=serial.close_count,
                              writes=[data.hex() for data in serial.writes])
            target.write_text(json.dumps(result, indent=2))
            assert not observer_errors, observer_errors
            assert result['late_csv_unchanged'] and result['late_json_unchanged']
            assert result['signal_handler_restored']
            if mode == 'native_timeout':
                assert result['poisoned'] and not saved['shutdown_confirmed'] and result['guard_retained']
                assert ('close',) not in native.calls
                index = native.calls.index(('held_read',))
                assert native.calls[index + 1:] == [('native_return',)], native.calls[index + 1:]
            elif mode.startswith('tpg_'):
                assert saved['shutdown_confirmed'] is True and saved['pressure_closed'] is False
                assert saved['outcome'] == 'shutdown_unconfirmed' and result['guard_retained']
                assert result['serial_closed'] and result['serial_close_count'] == 1
                if mode == 'tpg_open':
                    assert serial.writes == [], 'Late open after Stop issued a protocol query'
            else:
                assert not result['poisoned'], result
                assert saved['shutdown_confirmed'] is True and saved['outcome'] == 'interrupted'
                assert not result['guard_retained']
                if mode == 'esi_read':
                    index = native.calls.index(('native_return',))
                    assert ('module', 0, False) in native.calls[index + 1:]
                    assert ('close',) in native.calls[index + 1:]
                elif mode == 'esi_connect':
                    assert calls.index(('connect_return',)) < calls.index(('heater_off',)) < calls.index(('disconnect',))
                else:
                    assert sum('KeyboardInterrupt' in error for error in saved['errors']) == 2
        finally:
            run_returned.set()
            release.set()
            if generator is not None:
                generator.close()
            mp.undo()
    ''')
    env = dict(__import__('os').environ, PYTHONPATH=str(ROOT / 'tests'))
    completed = subprocess.run([sys.executable, '-c', script, mode, str(target), str(notebook)],
                               cwd=ROOT, env=env, capture_output=True, text=True, timeout=15)
    assert completed.returncode == 0, completed.stdout + '\n' + completed.stderr
    return json.loads(target.read_text())


@pytest.mark.parametrize("mode", ["esi_read", "esi_connect", "esi_shutdown", "native_timeout", "tpg_open", "tpg_read", "tpg_close"])
def test_real_sigint_and_late_returns_never_overlap_hardware_calls(tmp_path, mode):
    result = _run_signal_probe(tmp_path, mode)
    assert result["simulated"] is True


def test_quantized_limits_through_real_esi_setter_and_getters(ns, native_esi, tmp_path, monkeypatch):
    native_esi.state.target = 0.
    native_esi.driver._verify_hv_discharge = lambda *a, **k: None
    applied = {"voltage": 5.000064, "current": .999936, "power": 9.999872}
    for key in applied:
        def setter(self, value, key=key):
            native_esi.state.limits[key] = applied[key]
            return 0, applied[key]
        monkeypatch.setattr(native_esi.base, f"set_heat_ctrl_{key}_limit", setter)
    clock, calls = Clock(), []
    pressure = FakePressure(calls, clock)
    run = ns["Experiment"](tmp_path / "native_quantized", lambda directory: native_esi.driver, lambda: pressure, limit_preparer=ns["bounded_limit"],
                           guard=SimulatedGuard(tmp_path / "native_quantized"), baseline_s=1., hold_s=1., targets=(25.,), clock=clock, sleep=clock.sleep, cooling_s=1.,
                           stability_factory=lambda: ns["TemperatureStability"](window_s=2.),
                           plots_factory=NoPlots, overrides={"voltage_v": 5., "current_a": 1., "power_w": 10.})
    run.run()
    metadata = report(run)
    assert metadata["outcome"] == "complete" and metadata["shutdown_confirmed"]
    assert metadata["applied_heat_limit_echoes"] == {
        "voltage_v": applied["voltage"], "current_a": applied["current"], "power_w": applied["power"]}
    assert ("module", 0, True) in native_esi.calls


def test_recorder_completion_uses_its_event_not_thread_liveness(ns, rig, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    append = rig.run.append
    def blocked(row):
        if row["phase"] == "shutdown":
            # Model misleading Thread.is_alive() after an interrupted Python join.
            rig.run.pressure_thread.is_alive = lambda: False
            entered.set()
            assert release.wait(3)
        append(row)
    rig.run.append = blocked
    rig.esi.disconnect_hook = lambda: entered.wait(1)
    monkeypatch.setattr(ns["CallOwner"], "PRESSURE_CLOSE_WAIT_S", .05)
    try:
        rig.run.run()
        metadata = report(rig.run)
        assert metadata["shutdown_confirmed"] is True and metadata["pressure_closed"] is True
        assert metadata["pressure_recording_finished"] is False
        assert metadata["outcome"] == "shutdown_unconfirmed" and ns["_PT_GUARD"]
        frozen = (rig.run.directory / "samples.csv").read_bytes()
    finally:
        release.set()
        assert rig.run.pressure_recording_done.wait(2)
        rig.run.stream.close()
    assert (rig.run.directory / "samples.csv").read_bytes() == frozen


@pytest.mark.parametrize("kind", ["esi", "pressure"])
def test_constructor_uncertainty_cannot_abandon_the_shared_guard(ns, rig, kind):
    def failed(*args):
        raise RuntimeError("Constructor may have opened a transport")
    if kind == "esi":
        rig.run.esi_factory = failed
    else:
        rig.run.pressure_factory = failed
    rig.run.run()
    assert rig.run.guard.started and not rig.run.guard.released
    assert ns["_PT_GUARD"] and rig.run.guard_path.exists()
    assert report(rig.run)["outcome"] == "shutdown_unconfirmed"


def test_guard_finalize_failure_preserves_physical_shutdown_report(ns, rig):
    def fail(**kwargs):
        assert kwargs["shutdown_confirmed"] is True and kwargs["auxiliary_closed"] is True
        assert report(rig.run)["shutdown_confirmed"] is True
        raise OSError("Cannot persist guard release")
    rig.run.guard.finalize = fail
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["shutdown_confirmed"] is True and metadata["pressure_closed"] is True
    assert metadata["outcome"] == "shutdown_unconfirmed" and "guard_release_error" in metadata
    assert ns["_PT_GUARD"] and rig.run.guard_path.exists()


def test_shared_claim_and_hardware_boundary_precede_both_constructors(rig):
    original_esi, original_pressure = rig.run.esi_factory, rig.run.pressure_factory
    def pressure():
        assert rig.run.guard.events == ["claim", "hardware_started"]
        return original_pressure()
    def esi(directory):
        assert rig.run.guard.events == ["claim", "hardware_started"]
        assert report(rig.run)["experiment_claim"]["claim_id"] == "simulated"
        return original_esi(directory)
    rig.run.pressure_factory, rig.run.esi_factory = pressure, esi
    rig.run.run()
    assert rig.run.guard.events == ["claim", "hardware_started", "finalize"]


@pytest.mark.parametrize("mode", ["clean", "heater_refused", "pt_refused", "heater_recovery", "pressure_unconfirmed"])
def test_real_shared_guard_wired_through_main_in_fresh_process(tmp_path, mode):
    """Real shared OS locks/markers and notebook main; instruments are strictly simulated."""
    import os
    import subprocess
    import textwrap
    script = textwrap.dedent(r'''
        import ast, builtins, json, re, shutil, sys
        from pathlib import Path
        from types import SimpleNamespace
        from test_pressure_temperature_notebook import Clock, FakeESI, FakePressure, NoPlots, code, report
        root, work, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
        kit = work / 'kit'
        (kit / 'esi').mkdir(parents=True)
        for name in ('_experiment_guard.py', '_heater_stability.py', '_heater_limits.py'):
            shutil.copyfile(root / 'esi' / name, kit / 'esi' / name)
        tree = ast.parse(code()); tree.body.pop()
        ns = {'__file__': str(kit / 'notebooks/pressure_temperature.ipynb')}
        exec(compile(tree, str(root / 'notebooks/pressure_temperature.ipynb'), 'exec'), ns)
        calls, clock = [], Clock()
        esi, pressure = FakeESI(calls), FakePressure(calls, clock)
        output = work / 'different_output'
        original_report = original_marker = original_report_bytes = original_marker_bytes = None
        if mode in ('heater_refused', 'heater_recovery', 'pt_refused'):
            heater = mode.startswith('heater')
            legacy = kit / ('esi/logs/esi_heater_characterization' if heater else 'notebooks/pressure_temperature_runs')
            old = legacy / 'old'; old.mkdir(parents=True)
            stamp = '2026-01-01T00:00:00+00:00'
            original_report = old / ('report.json' if heater else 'metadata.json')
            original_report_bytes = json.dumps(dict(started_utc=stamp, shutdown_confirmed=False, esi_port=16,
                outcome='shutdown_unconfirmed', ended_utc='2026-01-01T00:01:00+00:00')).encode()
            original_report.write_bytes(original_report_bytes)
            (old / 'samples.csv').write_text('temperature_c\n42\n')
            original_marker = legacy / ('.heater_characterization.running.json' if heater else '.pressure_temperature.running.json')
            original_marker_bytes = json.dumps(dict(started_utc=stamp, run_directory='old')).encode()
            original_marker.write_bytes(original_marker_bytes)
        builtins.input = lambda prompt: re.search(r'RESTART [0-9a-f]+', prompt).group(0) if mode == 'heater_recovery' else 'NO'
        load, experiment = ns['load_private'], ns['Experiment']
        events, lease = [], []
        def load_for_test(path, package=False, fresh=False):
            if package:
                events.append('runtime_import')
                assert lease and not calls
                return SimpleNamespace(ESI=lambda *a, **k: esi)
            if Path(path).name == '_tpg366.py':
                return SimpleNamespace()
            module = load(path, package=package, fresh=fresh)
            if Path(path).name == '_experiment_guard.py':
                def create_guard(**kwargs):
                    events.append('guard_acquire')
                    guard = module.ExperimentGuard(**kwargs, state_root=work / 'state')
                    lease.append(guard)
                    return guard
                return SimpleNamespace(ExperimentGuard=create_guard)
            return module
        def short_experiment(*args, **kwargs):
            real_stability = kwargs['stability_factory']
            kwargs['stability_factory'] = lambda: real_stability(window_s=2.)
            run = experiment(*args, **kwargs, baseline_s=1., hold_s=1., cooling_s=1., targets=(30.,),
                             clock=clock, sleep=clock.sleep, plots_factory=NoPlots)
            pressure.is_shutdown = lambda: run.phase == 'shutdown'
            return run
        ns.update(ARM_HEATING=True, OUTPUT_DIR=output, find_plugins=lambda *a: kit,
                  runtime_fingerprints=lambda root: {'simulated': 'guard integration only'},
                  load_private=load_for_test, require_safe_esi=lambda runtime: None,
                  PressurePort=lambda protocol: pressure, Experiment=short_experiment)
        if mode == 'pressure_unconfirmed':
            pressure.close_failure = OSError('Simulated pressure Close failure')
        sys.platform = 'win32'  # Main's platform gate only; no native driver/serial is imported.
        refused = mode.endswith('refused')
        if refused:
            try:
                ns['main']()
            except RuntimeError as error:
                assert 'Unfinished' in str(error), str(error)
            else:
                raise AssertionError('An unresolved legacy notebook was bypassed')
            assert calls == [] and events == ['guard_acquire']
            assert original_report.read_bytes() == original_report_bytes
            assert original_marker.read_bytes() == original_marker_bytes
        else:
            run = ns['main']()
            data = report(run)
            assert events == ['guard_acquire', 'runtime_import']
            assert data['shutdown_confirmed'] is True
            if mode == 'pressure_unconfirmed':
                assert data['outcome'] == 'shutdown_unconfirmed' and data['pressure_closed'] is False
                assert ns['_PT_GUARD'] and lease[0].registry.exists()
            else:
                assert data['outcome'] == 'complete' and not ns['_PT_GUARD']
                assert not lease[0].registry.exists()
            if mode == 'heater_recovery':
                assert original_report.read_bytes() == original_report_bytes
                assert json.loads(original_report_bytes)['shutdown_confirmed'] is False
                record = data['operator_restarts'][0]
                assert record['shutdown_confirmed'] is False
                assert record['process_termination_verified_by_software'] is False
                archived = next(name for name, path in record['original_paths'].items() if path == str(original_marker))
                assert (Path(record['archive']) / archived).read_bytes() == original_marker_bytes
        print('REAL_GUARD_SIMULATED_INSTRUMENTS_OK')
    ''')
    completed = subprocess.run([sys.executable, "-c", script, str(ROOT), str(tmp_path), mode],
                               cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / "tests")),
                               capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "REAL_GUARD_SIMULATED_INSTRUMENTS_OK" in completed.stdout


def prepare_stage(ns, rig, target=30.):
    run = rig.run
    run.started = 0.
    run.requested, run.heat_requested, run.phase = target, True, "heating"
    run.hold_s = 300.
    run.stability = ns["TemperatureStability"]()
    run.stage = {"target_c": target, "commanded_s": 0., "complete": False,
                 "first_qualified_s": None, "observation_started_s": None, "qualified_observation_s": 0.}
    def observe(timestamp, temperature=None):
        row = {"temperature_c": target if temperature is None else temperature}
        run.qualify(row, timestamp)
        return row
    return observe


def test_real_60_second_window_then_300_continuous_seconds(ns, rig):
    observe = prepare_stage(ns, rig)
    for timestamp in range(360):
        row = observe(timestamp)
        assert row["thermal_stable"] == (timestamp >= 60)
        assert not row["stage_complete"]
    assert observe(360)["stage_complete"]
    assert rig.run.stage["first_qualified_s"] == 60
    assert rig.run.stage["qualified_observation_s"] == 300


def test_first_qualification_at_600_and_completion_at_900_are_accepted(ns, rig):
    observe = prepare_stage(ns, rig)
    for timestamp in range(901):
        row = observe(timestamp, 29. if timestamp < 540 else 30.)
        assert row["stage_complete"] == (timestamp == 900)
    assert rig.run.stage["first_qualified_s"] == 600
    assert rig.run.stage["completed_s"] == 900


@pytest.mark.parametrize("late", [600., 600.001])
def test_no_first_qualification_by_600_refuses_extension(ns, rig, late):
    observe = prepare_stage(ns, rig)
    for timestamp in range(600):
        observe(timestamp, 29.)
    with pytest.raises(RuntimeError, match="First thermal qualification deadline"):
        observe(late, 30.)
    assert not rig.run.stage["complete"]


@pytest.mark.parametrize("loss", ["gap", "band"])
def test_loss_resets_observation_without_resetting_absolute_deadline(ns, rig, loss):
    observe = prepare_stage(ns, rig)
    for timestamp in range(101):
        observe(timestamp)
    row = observe(104 if loss == "gap" else 101, 30. if loss == "gap" else 30.21)
    assert not row["thermal_stable"] and row["qualified_observation_s"] == 0
    assert rig.run.stage["first_qualified_s"] == 60
    assert rig.run.stage["commanded_s"] == 0
    # Repeated losses never purchase a new 900 s budget.
    start = 105 if loss == "gap" else 102
    for timestamp in range(start, 900):
        observe(timestamp, 30.21 if timestamp % 100 == 0 else 30.)
    with pytest.raises(RuntimeError, match="Absolute stage deadline"):
        observe(900)
    assert not rig.run.stage["complete"]


@pytest.mark.parametrize("drift", [-.11, .11])
def test_drift_prevents_qualification_even_inside_temperature_band(ns, rig, drift):
    observe = prepare_stage(ns, rig)
    for timestamp in range(61):
        row = observe(timestamp, 30. + drift * (timestamp - 30) / 60)
    assert abs(row["slope_c_min"]) == pytest.approx(.11)
    assert not row["thermal_stable"]


def test_no_stability_stops_before_next_target_or_cooling(rig):
    rig.run.qualification_deadline_s = 4.
    rig.esi.diag_hook = lambda device: setattr(device, "temperature", device.target - 1.) if device.active else None
    rig.run.run()
    assert report(rig.run)["outcome"] == "error"
    assert len([c for c in rig.calls if c[0] == "target"]) == 1
    assert not any(row["phase"] == "cooling" for row in rows(rig.run))
    assert ("heater_off",) in rig.calls and report(rig.run)["shutdown_confirmed"]


@pytest.mark.parametrize("temperature,complete", [(174.8, True), (175., True), (175.000001, False)])
def test_final_175_band_and_strict_cutoff(rig, temperature, complete):
    rig.run.targets = (175.,)
    rig.esi.diag_hook = lambda device: setattr(device, "temperature", temperature) if device.active else None
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["stages"][0]["complete"] is complete
    assert (metadata["outcome"] == "complete") is complete
    assert metadata["shutdown_confirmed"]
    assert any(row["phase"] == "cooling" for row in rows(rig.run)) is complete


def test_cooling_keeps_fresh_temperature_pressure_after_verified_off(rig):
    rig.run.targets = (30.,)
    def cooling(device):
        if rig.run.phase == "cooling":
            assert not device.active and ("heater_off",) in rig.calls
            device.temperature -= .01
    rig.esi.diag_hook = cooling
    rig.run.run()
    cooled = [row for row in rows(rig.run) if row["phase"] == "cooling"]
    assert len(cooled) >= 2
    assert len({row["temperature_c"] for row in cooled}) == len(cooled)
    assert all(float(row["target_temperature_c"]) == 0 and float(row["P1_mbar"]) > 0 for row in cooled)
    assert report(rig.run)["cooling_complete"]


@pytest.mark.parametrize("failure", ["stop", "pressure", "sensor"])
def test_cooling_faults_and_stop_preempt_observation(rig, failure):
    rig.run.targets = (30.,)
    def interrupt_cooling(pressure):
        if rig.run.phase == "cooling":
            if failure == "stop":
                raise KeyboardInterrupt("Stop during cooling")
            if failure == "pressure":
                raise OSError("Cooling pressure failure")
            rig.esi.sensor_valid = False
    rig.pressure.read_hook = interrupt_cooling
    rig.run.run()
    metadata = report(rig.run)
    assert not metadata["cooling_complete"] and metadata["shutdown_confirmed"]
    assert metadata["outcome"] == ("interrupted" if failure == "stop" else "error")
    assert metadata.get("cooling_finished_s") is None


def test_approved_ceilings_are_bounded_to_fresh_hardware_maxima(rig):
    rig.esi.maxima.update(max_voltage_v=12., max_current_a=6., max_power_w=30.)
    rig.run.run()
    metadata = report(rig.run)
    assert metadata["outcome"] == "complete"
    assert metadata["requested_heat_limits"] == dict(voltage_limit_v=12., current_limit_a=6., power_limit_w=30.)
    assert metadata["limit_overrides"] == dict(voltage_v=22., current_a=10., power_w=50.)


@pytest.mark.parametrize("kind,unit,ceiling", [("voltage", "v", 22.), ("current", "a", 10.), ("power", "w", 50.)])
@pytest.mark.parametrize("source", ["echo_and_getter", "getter_only"])
def test_applied_limits_above_approved_ceiling_are_refused_even_within_hardware(rig, kind, unit, ceiling, source):
    short, key = f"{kind}_{unit}", f"{kind}_limit_{unit}"
    rig.esi.maxima[f"max_{kind}_{unit}"] = ceiling * 2
    too_high = math.nextafter(ceiling, math.inf)  # Even one ULP above the ceiling cannot be tolerated.
    original_configure, original_get = rig.esi.configure_heat_limits, rig.esi.get_heat_configuration
    if source == "echo_and_getter":
        def configure(*, cancel_event=None, **kwargs):
            applied = original_configure(cancel_event=cancel_event, **kwargs)
            applied[short] = rig.esi.limits[key] = too_high
            return applied
        rig.esi.configure_heat_limits = configure
    else:
        def get_configuration():
            config = original_get()
            if any(call[0] == "limits" for call in rig.calls):
                config[key] = too_high
            return config
        rig.esi.get_heat_configuration = get_configuration
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "error" and data["shutdown_confirmed"]
    assert not any(call[0] in ("target", "heater_on") for call in rig.calls)
    assert data["approved_heat_ceilings"][key] == ceiling


@pytest.mark.parametrize("kind,unit", [("voltage", "v"), ("current", "a"), ("power", "w")])
def test_lower_fresh_hardware_maximum_cannot_leave_an_excessive_limit_running(rig, kind, unit):
    def diagnostic(device):
        if device.active:
            device.maxima[f"max_{kind}_{unit}"] = device.limits[f"{kind}_limit_{unit}"] / 2
    rig.esi.diag_hook = diagnostic
    rig.run.run()
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]
    assert [call[1] for call in rig.calls if call[0] == "target"] == [30.]


def test_full_default_175_program_uses_real_60_and_300_second_windows(ns, rig):
    rig.run.baseline_s = 60.
    rig.run.hold_s = 300.
    rig.run.cooling_s = 900.
    rig.run.stability_factory = ns["TemperatureStability"]
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "complete" and data["shutdown_confirmed"]
    assert data["program_c"] == list(range(30, 171, 10)) + [175]
    assert len(data["stages"]) == 16
    for stage in data["stages"]:
        assert stage["first_qualified_s"] - stage["commanded_s"] == 60
        assert stage["completed_s"] - stage["commanded_s"] == 360
        assert stage["qualified_observation_s"] == 300 and stage["complete"]
    assert data["cooling_finished_s"] - data["cooling_started_s"] == 900
    assert rig.clock.now == 60 + 16 * 360 + 900


def test_storage_stall_after_genuine_observation_cannot_hide_absolute_deadline(rig):
    append = rig.run.append
    def stalled(row):
        append(row)
        if row["phase"] == "heating" and row["stage_complete"]:
            rig.clock.now += 901
    rig.run.append = stalled
    rig.run.run()
    data = report(rig.run)
    assert data["outcome"] == "error" and data["shutdown_confirmed"]
    assert data["stages"][0]["qualified_observation_s"] == 3
    assert not data["stages"][0]["complete"]
    assert [call[1] for call in rig.calls if call[0] == "target"] == [30.]
    assert "deadline" in " ".join(data["errors"])


def test_owner_rechecks_previous_deadline_before_the_next_positive_target(rig):
    check = rig.run.guard.check
    def delayed_check():
        check()
        if rig.run.commanded_stage is not None and rig.run.commanded_stage["complete"]:
            rig.clock.now += 901
    rig.run.guard.check = delayed_check
    rig.run.run()
    assert [call[1] for call in rig.calls if call[0] == "target"] == [30.]
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]


def test_target_call_exceeding_deadline_cannot_be_followed_by_first_on(rig):
    rig.esi.failures["target"] = lambda: rig.clock.sleep(901)
    rig.run.run()
    assert ("target", 30.) in rig.calls and ("heater_on",) not in rig.calls
    assert report(rig.run)["outcome"] == "error" and report(rig.run)["shutdown_confirmed"]


def test_expired_final_stage_still_sends_off_before_refusing_cooling(rig):
    check = rig.run.guard.check
    def delayed_check():
        check()
        stage = rig.run.commanded_stage
        if stage is not None and stage["target_c"] == 175 and stage["complete"]:
            rig.clock.now += 901
    rig.run.guard.check = delayed_check
    rig.run.run()
    data = report(rig.run)
    assert ("heater_off",) in rig.calls and data["shutdown_confirmed"]
    assert data["outcome"] == "error" and not data.get("cooling_complete", False)
    assert not data["stages"][-1]["complete"]


def test_temperature_timestamp_precedes_caller_delay_and_csv(rig):
    rig.run.targets = (30.,)
    original = rig.run.esi_call
    def delayed(method, *args, **kwargs):
        value = original(method, *args, **kwargs)
        if method == "collect_diagnostics":
            rig.clock.now += .25
        return value
    rig.run.esi_call = delayed
    append = rig.run.append
    seen = []
    def persisted(row):
        if math.isfinite(row["temperature_observed_s"]):
            seen.append(row["temperature_observed_s"])
            assert row["temperature_observed_s"] + .25 <= rig.clock.now - rig.run.started + 1e-12
        append(row)
    rig.run.append = persisted
    rig.run.run()
    assert report(rig.run)["outcome"] == "complete" and seen
    stage = report(rig.run)["stages"][0]
    last = next(row for row in rows(rig.run) if row["stage_complete"] == "True")
    assert stage["completed_s"] == float(last["temperature_observed_s"]) + rig.run.started


@pytest.mark.parametrize("offset", [2., 2.0001])
def test_sampling_gap_boundary_does_not_manufacture_stability(ns, rig, offset):
    observe = prepare_stage(ns, rig)
    for timestamp in range(61):
        observe(timestamp)
    row = observe(60 + offset)
    assert row["thermal_stable"] is (offset == 2.)
    if offset > 2.:
        assert row["stability_span_s"] == 0 and row["qualified_observation_s"] == 0


def test_175_capability_required_even_if_subset_would_fit(rig):
    rig.run.targets = (30.,)
    rig.esi.maxima["max_temperature_c"] = 174.99
    rig.run.run()
    assert not any(call[0] in {"target", "heater_on", "limits"} for call in rig.calls)
    assert report(rig.run)["outcome"] == "error"
