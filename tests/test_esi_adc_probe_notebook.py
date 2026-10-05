"""The ESI HV ADC probe notebook with the real runtime controller and a simulated ADC, never hardware."""
from __future__ import annotations

import ast
import csv
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_esi_discharge import rig as native_discharge  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks/esi_adc_probe.ipynb"


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
    return namespace


class Guard:
    """Component-test lease; the shared helper itself is tested in test_esi_experiment_guard.py."""
    def __init__(self):
        self.events = []
        self.released = False

    def check(self):
        assert not self.released

    def claim(self, directory, report_path):
        assert (Path(directory) / "samples.csv").is_file()
        report = json.loads(Path(report_path).read_text(encoding="utf-8"))
        assert report["shutdown_confirmed"] is False and report["esi_port"] == 16
        self.events.append("claim")
        return {"claim_id": "simulated"}

    def mark_hardware_started(self):
        self.events.append("hardware_started")

    def finalize(self, *, shutdown_confirmed, owners_idle, auxiliary_closed, transport_poisoned):
        self.events.append(("finalize", shutdown_confirmed, transport_poisoned))
        if shutdown_confirmed is True and owners_idle is True and auxiliary_closed is True and transport_poisoned is False:
            self.released = True
            return True
        return False

    def abandon_before_hardware(self):
        self.events.append("abandon")
        return True


@pytest.fixture
def probe(ns, native_discharge, tmp_path):
    rig = native_discharge
    diagnostic = {"modules": {address: {"module_active": False, "active": False, "target_v": 0.}
                              for address in (1, 2)},
                  "heat": {"module_active": False}}

    class Facade:
        def __init__(self, directory):
            self._backend = rig.driver

        def connect(self):
            rig.calls.append(("connect",))
            return True

        def collect_diagnostics(self):
            return diagnostic

        def disconnect(self, *, on_discharge=None):
            return rig.driver.disconnect(timeout_s=.5, on_discharge=on_discharge)

    guard = Guard()
    run = ns["Probe"](tmp_path / "runs", Facade, guard=guard, base=rig.base, trace_factory=ns["NativeTrace"],
                      repeats=1, conversions=10, baseline_conversions=3, conversion_timeout_s=.5,
                      clock=rig.clock.monotonic, sleep=rig.clock.sleep)
    return SimpleNamespace(run=run, guard=guard, rig=rig, diagnostic=diagnostic, ns=ns)


def metadata(run):
    return json.loads((run.directory / "metadata.json").read_text(encoding="utf-8"))


def samples(run):
    with (run.directory / "samples.csv").open(encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def summary(run, phase, module, channel):
    return next(entry for entry in run.metadata["summary"]
                if (entry["phase"], entry["module"], entry["channel"]) == (phase, module, channel))


def test_low_channels_confirm_shutdown_restore_channels_and_release_the_guard(probe):
    original = dict(probe.rig.ranges)
    run = probe.run.run()
    assert run.metadata["outcome"] == "complete" and run.metadata["probe_complete"]
    assert run.shutdown_confirmed and probe.guard.released and not probe.ns["_ADC_GUARD"]
    assert probe.guard.events[:2] == ["claim", "hardware_started"]
    assert probe.rig.ranges == original and run.metadata["channels_restored"] is True
    assert all(entry["pattern"] == "low" for entry in run.metadata["summary"])
    # 2 baseline sequences of 1 + 3 rows, then 2 modules x 2 channels of 1 + 10 rows.
    rows = samples(run)
    assert len(rows) == 2 * 4 + 4 * 11 == metadata(run)["samples"]
    assert {(row["phase"], row["module"], row["negative_channel"]) for row in rows} == {
        ("baseline", "1", "True"), ("baseline", "2", "False"),
        *{("switch", str(a), str(n)) for a in (1, 2) for n in (False, True)}}
    assert all(row["fresh"] == "True" for row in rows if row["index"] != "0")
    trace = [json.loads(line) for line in (run.directory / "native_calls.jsonl").read_text().splitlines()]
    assert any(row["method"] == "set_hv_supply_meas_ranges" for row in trace)
    assert metadata(run)["native_trace"]["writer_closed"] is True
    assert metadata(run)["discharge_observations"][-1]["consecutive"] == 3


def test_settling_after_selection_is_shown_while_the_runtime_check_still_fails(probe, monkeypatch):
    # Lab pattern: HV1's negative channel reads 1324 V for the first conversions after a
    # selection, near 0 V otherwise; the runtime check reads the first fresh conversion.
    rig = probe.rig
    select = rig.base.set_hv_supply_meas_ranges

    def settling(self, address, negative, high):
        status = select(self, address, negative, high)
        rig.counts[address, bool(negative)] = 0
        return status

    monkeypatch.setattr(rig.base, "set_hv_supply_meas_ranges", settling)
    rig.values[1, True] = [1324., 1324., 1324., -.06]
    rig.counts[1, True] = 99  # Untouched since connection: the settled value.
    run = probe.run.run()
    assert run.metadata["probe_complete"] and run.metadata["channels_restored"] is True
    assert summary(run, "baseline", 1, "negative")["pattern"] == "low"
    settled = summary(run, "switch", 1, "negative")
    assert settled["pattern"] == "settles" and settled["first_fresh_v"] == 1324.
    assert settled["tail_median_v"] == -.06 and settled["last_above_index"] <= 3
    assert summary(run, "switch", 1, "positive")["pattern"] == "low"
    assert any("settling" in hint for hint in run.metadata["hints"])
    # The unchanged runtime criterion still refuses: shutdown and the guard stay unconfirmed.
    assert not run.shutdown_confirmed and run.metadata["outcome"] == "shutdown_unconfirmed"
    assert ("finalize", False, False) in probe.guard.events and not probe.guard.released
    assert probe.ns["_ADC_GUARD"]
    saved = metadata(run)
    assert saved["discharge_observations"] and saved["last_discharge"]["modules"]["1"]["negative_v"] == 1324.
    assert ("close",) not in rig.calls


def test_hv_not_confirmed_off_refuses_any_channel_selection(probe):
    probe.diagnostic["modules"][2]["module_active"] = True
    run = probe.run.run()
    assert any("probe refused" in error for error in run.errors)
    assert run.rows == [] and not run.metadata["probe_complete"]
    first_off = probe.rig.calls.index(("disable",))
    assert not any(call[0] == "select" for call in probe.rig.calls[:first_off])


def test_stop_ends_the_sequence_restores_channels_then_verified_shutdown(probe):
    original = dict(probe.rig.ranges)
    clock = probe.rig.clock
    sleep = clock.sleep

    def stop_later(seconds):
        sleep(seconds)
        if clock.now >= 1.:
            probe.run.stop.set()

    probe.run.sleep = stop_later
    run = probe.run.run()
    assert run.metadata["outcome"] == "stopped" and not run.metadata["probe_complete"]
    assert probe.rig.ranges == original and run.shutdown_confirmed and probe.guard.released
    assert 0 < len(run.rows) < 2 * 4 + 4 * 11


def test_stuck_ready_flag_ends_each_sequence_without_hanging(probe):
    probe.rig.state.sticky = True
    run = probe.run.run()
    assert all(entry["pattern"] == "no fresh conversion" and entry["timed_out"] for entry in run.metadata["summary"])
    assert all(row["note"] for row in run.rows if row["index"] != 0)
    assert not run.shutdown_confirmed and not probe.guard.released


def test_settings_are_validated_before_any_claim(probe):
    probe.run.conversions = 5
    with pytest.raises(ValueError, match="conversions"):
        probe.run.run()
    assert probe.guard.events == []


def test_summary_patterns():
    namespace = {}
    tree = ast.parse(code())
    tree.body.pop()
    exec(compile(tree, str(NOTEBOOK), "exec"), namespace)
    def rows(values):
        return [dict(phase="switch", repeat=1, module=1, negative_channel=True, index=i, fresh=i > 0,
                     since_selection_s=.1 * i, voltage_valid=True, voltage_v=v, v_overflow=False, note="")
                for i, v in enumerate(values)]
    patterns = [namespace["summarize"](rows(values))[0]["pattern"] for values in (
        [0., .1, -.2, .3, .1, 0., .2], [900., 900., 1.5, .1, .1, .1, .1, .1], [0., 0., 0., 2., 0., 0., 0.])]
    assert patterns == ["low", "settles", "stays high"]


def test_runtime_check_accepts_the_bundled_runtime_only(ns):
    runtime = ns["load_private"](ROOT / "esi/vendor/runtime/__init__.py", package=True)
    ns["require_discharge_runtime"](runtime)
    with pytest.raises(RuntimeError, match="discharge"):
        ns["require_discharge_runtime"](SimpleNamespace(__name__="other", ESI=SimpleNamespace()))


def test_main_wires_the_shared_guard_inline_runtime_and_trace(ns, monkeypatch, tmp_path):
    created, events = {}, []

    class ExperimentGuard:
        def __init__(self, **kwargs):
            created.update(kwargs)

        def authorize_restart(self):
            events.append("authorize")
            return []

    class Base:
        pass

    def load(path, package=False, fresh=False):
        events.append(("load", Path(path).name, fresh))
        if Path(path).name == "_experiment_guard.py":
            return SimpleNamespace(ExperimentGuard=ExperimentGuard)
        runtime = SimpleNamespace(__name__="_fake_adc_runtime", ESI=lambda *a, **kw: ("esi", a, kw))
        monkeypatch.setitem(sys.modules, "_fake_adc_runtime.esi.esi_base", SimpleNamespace(ESIBase=Base))
        return runtime

    class Probe:
        def __init__(self, output_dir, factory, **kwargs):
            self.metadata, self.factory, self.kwargs = {}, factory, kwargs

        def run(self):
            return self

    monkeypatch.setattr(sys, "platform", "win32")
    ns.update(find_plugins=lambda *a: ROOT, load_private=load, require_discharge_runtime=lambda runtime: None,
              Probe=Probe, OUTPUT_DIR=tmp_path / "runs")
    probe = ns["main"]()
    assert created["kind"] == "adc_probe" and created["com"] == 16 and created["plugin_dir"] == ROOT / "esi"
    assert created["kernel_globals"] is ns and created["output_dir"] == tmp_path / "runs"
    # Guard and operator recovery before the runtime is imported, in a fresh private namespace.
    assert events == [("load", "_experiment_guard.py", False), "authorize", ("load", "__init__.py", True)]
    assert probe.kwargs["base"] is Base and probe.kwargs["trace_factory"] is ns["NativeTrace"]
    _, args, kwargs = probe.factory(tmp_path)
    assert args == ("adc-probe", 16) and kwargs == {"baudrate": 230400, "log_dir": tmp_path, "process_backend": False}
    assert set(probe.metadata["runtime_sources"]) <= set(ns["_RUNTIME_FILES"])


def test_whole_cell_refuses_off_windows_before_any_access(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.chdir(tmp_path)
    namespace = {"__file__": str(tmp_path / "esi_adc_probe.ipynb")}
    with pytest.raises(RuntimeError, match="requires Windows"):
        exec(compile(code(), str(NOTEBOOK), "exec"), namespace)
    assert not (tmp_path / "esi_adc_probe_runs").exists()


def test_plot_is_saved_when_matplotlib_is_available(probe):
    run = probe.run.run()
    assert (run.directory / "adc_probe.png").exists() == (importlib.util.find_spec("matplotlib") is not None)
