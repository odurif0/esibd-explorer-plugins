"""Transmission optimizer engine on the simulated beamline (no Qt, no Explorer)."""
from __future__ import annotations

import importlib
import importlib.util
import math
import sys
import threading
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "transmission" / "_runtime"
NAME = "_transmission_engine_test_runtime"


@pytest.fixture(scope="module")
def rt():
    for key in [k for k in sys.modules if k == NAME or k.startswith(NAME + ".")]:
        sys.modules.pop(key)
    spec = importlib.util.spec_from_file_location(NAME, RUNTIME / "__init__.py", submodule_search_locations=[str(RUNTIME)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[NAME] = package
    spec.loader.exec_module(package)
    engine = importlib.import_module(f"{NAME}._engine")
    simulator = importlib.import_module(f"{NAME}._simulator")
    return engine, simulator


def collector(simulator, beamline):
    return simulator.transport(beamline.actual)["Collector"]


def optimum(simulator):
    from scipy.optimize import minimize
    names = list(simulator.KNOBS)
    start = [simulator.KNOBS[n][2] for n in names]
    result = minimize(lambda x: -simulator.transport(dict(zip(names, x)))["Collector"], start, method="Nelder-Mead",
                      options=dict(maxiter=20000, maxfev=20000, xatol=1e-4, fatol=1e-22))
    return -result.fun


FUNNEL = """
settle_s = 0.5
average_s = 1.0
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
"""


def run(rt, text, *, strategy=None, seed=0, hook=None, beamline=None, **simulation):
    engine, simulator = rt
    config = engine.parse_config(text)
    beamline = beamline or simulator.SimulatedBeamline(seed=seed, **simulation)
    events = []

    def on_event(kind, data):
        events.append((kind, data))
        if hook:
            hook(kind, data, beamline)
    optimizer = engine.Optimizer(config, beamline, strategy=strategy, on_event=on_event)
    return optimizer, beamline, optimizer.run(), events


def assert_step_limited(rt, beamline, config_text):
    engine, simulator = rt
    config = engine.parse_config(config_text)
    previous = {c: simulator.KNOBS[c][2] for c in simulator.KNOBS}
    steps = {}
    for stage in config.stages:
        for knob in stage.knobs:
            _, step = knob.resolve(previous)  # Relative knobs scale with the start amplitude.
            for channel, gain in knob.channels.items():
                # Each stage has its own max_step; a channel may be driven in several stages.
                steps[channel] = max(steps.get(channel, 0.0), abs(gain) * step)
    if config.target is not None:
        for channel, gain in config.target.channels.items():
            steps[channel] = abs(gain) * config.target.max_step
    for _, command in beamline.commands:
        for channel, value in command.items():
            assert abs(value - previous[channel]) <= steps[channel] + 1e-9, (channel, previous[channel], value)
            lo, hi = simulator.KNOBS[channel][:2]
            assert lo <= value <= hi
            previous[channel] = value


# ---------------------------------------------------------------- configuration

def test_simulator_configuration_and_template_parse(rt):
    engine, simulator = rt
    config = engine.parse_config(simulator.CONFIG)
    assert [s.name for s in config.stages][0] == "Inlet and funnel" and len(config.stages) == 6
    assert config.pressure["P_funnel"] == (2.0, 6.0)
    assert set(config.knob_channels) == set(simulator.KNOBS)
    template = engine.template(["PSU_A Ch1", "PSU_A Ch2"], ["DMMR A1", "DMMR A2"], ["P funnel"])
    parsed = engine.parse_config(template)
    assert parsed.stages[0].aperture == "DMMR A1" and parsed.stages[0].downstream == ("DMMR A2",)
    assert "P funnel" in template and parsed.pressure == {}


@pytest.mark.parametrize("snippet, message", [
    ("[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[1.0, 2.0]\nmax_step=1.0", "window must contain 0"),
    ("[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=0.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "must not be zero"),
    ("[[stage]]\nname='a'\ndownstream=['X']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "both driven and measured"),
    ("[[stage]]\nname='a'\ndownstream=['B']\nbudget=5\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "budget"),
    ("speed = 3\n[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "Unknown setting"),
    ("polarity = 2\n[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "polarity"),
    ("[pressure]\nP = [5.0, 1.0]\n[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "minimum < maximum"),
    ("[[stage]]\nname='a'\naperture='B'\ndownstream=['B']\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0", "aperture cannot also"),
    ("[[stage]]\nname='a'\ndownstream=['B']", "at least one [[stage.knob]]"),
    ("x = ", "TOML syntax"),
    ("", "at least one [[stage]]"),
])
def test_invalid_configurations_are_refused_before_any_move(rt, snippet, message):
    engine, _ = rt
    with pytest.raises(engine.ConfigError, match=message.replace("[", r"\[").replace("]", r"\]")):
        engine.parse_config(snippet)


def test_knob_window_is_clipped_to_device_limits(rt):
    engine, simulator = rt
    text = FUNNEL.replace("window = [-110.0, 270.0]", "window = [-500.0, 500.0]")
    optimizer, beamline, result, _ = run(rt, text, strategy="coordinate")
    assert result["status"] == "completed"
    assert all(0 <= cmd["Inlet"] <= 400 for _, cmd in beamline.commands if "Inlet" in cmd)


# ---------------------------------------------------------------- model

def test_gaussian_process_interpolates_and_is_uncertain_away_from_data(rt):
    engine, _ = rt
    rng = np.random.default_rng(0)
    x = np.linspace(0, 1, 12)[:, None]
    y = np.sin(6 * x[:, 0])
    gp = engine.GaussianProcess([(0.03, 3.0)]).fit(x, y, np.full(12, 1e-6), rng)
    mean, std = gp.predict(np.array([[0.5], [3.0]]))
    assert abs(mean[0] - math.sin(3.0)) < 0.05
    assert std[1] > 10 * std[0]


# ---------------------------------------------------------------- optimization

@pytest.mark.slow
def test_full_simulated_beamline_reaches_the_optimum_with_bounded_steps(rt):
    _, simulator = rt
    optimizer, beamline, result, events = run(rt, simulator.CONFIG, seed=0)
    assert result["status"] == "completed", result["reason"]
    assert all(stage["adopted"] for stage in result["stages"])
    assert collector(simulator, beamline) > 0.95 * optimum(simulator)
    assert_step_limited(rt, beamline, simulator.CONFIG)
    kinds = {record["kind"] for kind, data in events if kind == "evaluation" for record in [data["record"]]}
    assert kinds == {"initial", "probe", "explore", "reference", "verify"}


@pytest.mark.parametrize("strategy", ["bayesian", "coordinate"])
def test_one_stage_improves_and_never_chases_a_lost_beam(rt, strategy):
    # Inlet can drive the beam away entirely: the A1 current then vanishes too.
    # Minimizing the aperture current alone would pick that; transmission must not.
    _, simulator = rt
    start = collector(simulator, simulator.SimulatedBeamline())
    optimizer, beamline, result, events = run(rt, FUNNEL, strategy=strategy)
    assert result["status"] == "completed", result["reason"]
    stage = result["stages"][0]
    assert stage["adopted"] and stage["relative_gain"] > 0.3
    assert 120 < beamline.actual["Inlet"] < 200  # true optimum 160 V; lost beam below ~0 and above ~350
    assert collector(simulator, beamline) > 1.3 * start
    records = [data["record"] for kind, data in events if kind == "evaluation"]
    lost = [r for r in records if not r["valid"]]
    assert all(r["model_objective"] == 0 for r in lost)
    assert_step_limited(rt, beamline, FUNNEL)


def test_scoring_marks_a_vanished_flux_as_lost_beam_not_as_transmission(rt):
    engine, _ = rt
    config = engine.parse_config(FUNNEL)
    optimizer = engine.Optimizer(config, None)
    optimizer.commanded = {"Inlet": 0.0, "Funnel_exit": 0.0}
    stage = config.stages[0]
    data = {c: (1e-12, 1e-14, 10) for c in stage.currents}  # 5 pA: above noise, 0.5 % of the start
    data["P_funnel"] = (4.0, 0.01, 10)
    record = optimizer._score(stage, [0.0, 0.0], "explore", 0.0, 1.0, data, reference=1e-9)
    assert not record["valid"] and "beam lost" in record["reason"] and record["model_objective"] == 0
    data["A1"] = (8e-10, 1e-12, 10)
    record = optimizer._score(stage, [0.0, 0.0], "explore", 0.0, 1.0, data, reference=1e-9)
    assert record["valid"] and record["incident"] > 0.2e-9


@pytest.mark.parametrize("best_current, adopted", [(50e-12, False), (95e-12, True)])
def test_a_better_ratio_is_adopted_only_if_it_carries_more_ions(rt, best_current, adopted):
    # Ions lost on the rods are never measured: losing them before the normalizing collectors
    # raises the ratio. The A/B verification also requires more ions after the aperture.
    engine, _ = rt
    config = engine.parse_config(FUNNEL.replace('budget = 40', 'budget = 40\nnormalize_by = ["A1", "A2", "A3", "A4", "Collector"]'))
    optimizer = engine.Optimizer(config, None)

    def evaluate(stage, start, delta, kind, reference, history):
        best = bool(np.any(delta))
        record = dict(valid=True, model_objective=0.6 if best else 0.5, transmitted=best_current if best else 80e-12)
        history.append(record)
        return record
    optimizer._evaluate = evaluate
    verdict = optimizer._verify(config.stages[0], {}, np.array([5.0, 0.0]), 1e-10, [])
    assert verdict["gain"] == pytest.approx(0.1) and verdict["absolute_gain"] == pytest.approx(best_current - 80e-12)
    assert verdict["adopt"] is adopted


def test_gain_below_significance_keeps_the_start(rt):
    # Q3 RF cannot change the current on A1: any apparent gain is noise.
    text = """
settle_s = 0.2
average_s = 1.0
seed = 4
[[stage]]
name = "Flat"
downstream = ["A1"]
budget = 24
[[stage.knob]]
channels = { Q3_RF = 1.0 }
window = [-20.0, 20.0]
max_step = 5.0
"""
    optimizer, beamline, result, _ = run(rt, text, strategy="coordinate", noise=0.05)
    assert result["status"] == "completed"
    stage = result["stages"][0]
    assert not stage["adopted"]
    assert beamline.setpoint["Q3_RF"] == stage["start"]["Q3_RF"]


# ---------------------------------------------------------------- safety

def _after(count, action):
    def hook(kind, data, beamline):
        if kind == "evaluation" and data["record"]["index"] == count:
            action(beamline)
    return hook


def test_discharge_trips_the_soft_interlock_and_restores_the_stage_start_stepwise(rt):
    _, simulator = rt
    optimizer, beamline, result, events = run(rt, FUNNEL, strategy="coordinate",
                                              hook=_after(5, lambda b: setattr(b, "fault", "discharge")))
    assert result["status"] == "interlock"
    assert "P_funnel" in result["reason"] or "discharge" in result["reason"]
    start = {c: simulator.KNOBS[c][2] for c in ("Inlet", "Funnel_exit")}
    assert {c: beamline.setpoint[c] for c in start} == start
    assert_step_limited(rt, beamline, FUNNEL)
    assert ("status", {"text": "Restoring the settings of the stage start"}) in events


def test_device_switched_off_gets_no_further_command(rt):
    seen = {}

    def off(beamline):
        beamline.devices_on = False
        seen["commands"] = len(beamline.commands)
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", hook=_after(4, off))
    assert result["status"] == "instrument error" and "switched off" in result["reason"]
    assert len(beamline.commands) == seen["commands"]


def test_stop_restores_the_stage_start_even_though_waits_are_cancelled(rt):
    _, simulator = rt
    cancel = threading.Event()
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", cancel=cancel,
                                         hook=_after(6, lambda b: cancel.set()))
    assert result["status"] == "stopped"
    assert beamline.setpoint["Inlet"] == simulator.KNOBS["Inlet"][2]
    assert beamline.setpoint["Funnel_exit"] == simulator.KNOBS["Funnel_exit"][2]
    assert_step_limited(rt, beamline, FUNNEL)


def test_readback_that_never_settles_is_an_instrument_error(rt):
    optimizer, beamline, result, _ = run(rt, FUNNEL.replace("settle_s = 0.5", "settle_s = 0.5\nsettle_timeout_s = 3.0"),
                                         strategy="coordinate", slew_v_per_s=0.01)
    assert result["status"] == "instrument error" and "Readback did not reach" in result["reason"]


def test_missing_data_is_an_instrument_error(rt):
    _, simulator = rt
    beamline = simulator.SimulatedBeamline()
    beamline.window = lambda channels, start, end: None
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "instrument error" and "No fresh data" in result["reason"]
    assert beamline.commands == [] or all(cmd == {} for _, cmd in beamline.commands) or len(beamline.commands) <= 1


def test_an_acquisition_gap_is_measured_again_never_filled(rt):
    _, simulator = rt
    beamline = simulator.SimulatedBeamline()
    window, calls = beamline.window, []

    def gap(channels, start, end):
        data = window(channels, start, end)
        calls.append(len(beamline.commands))
        if data is not None and len(calls) in (3, 4):  # Two empty windows in a row, then samples again.
            data = {c: (math.nan, math.nan, 0) for c in data}
        return data
    beamline.window = gap
    optimizer, beamline, result, events = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "completed", result["reason"]
    assert calls[2] == calls[3] == calls[4], "no command between the repeated windows"
    assert sum(1 for kind, data in events if kind == "status" and "measuring again" in data["text"]) == 2
    assert all(math.isfinite(r["objective"]) for r in optimizer.history)

    beamline = simulator.SimulatedBeamline()
    window = beamline.window
    beamline.window = lambda channels, start, end: (lambda d: d and {c: (math.nan, math.nan, 0) for c in d})(
        window(channels, start, end))
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "instrument error" and "3 averaging windows in a row" in result["reason"], result
    assert optimizer.history == []


def test_a_criterion_current_needs_two_samples_per_window(rt):
    _, simulator = rt
    beamline = simulator.SimulatedBeamline()
    window, calls = beamline.window, []

    def thin(channels, start, end):
        data = window(channels, start, end)
        calls.append(1)
        if data is not None and len(calls) == 2:
            data["Collector"] = (data["Collector"][0], math.nan, 1)  # One sample: no noise estimate.
            data["P_funnel"] = (data["P_funnel"][0], math.nan, 1)    # Enough for a pressure.
        return data
    beamline.window = thin
    optimizer, beamline, result, events = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "completed", result["reason"]
    assert [d["text"] for k, d in events if k == "status" and "measuring again" in d["text"]] == [
        "Too few samples for Collector in the averaging window; measuring again"]
    assert all(math.isfinite(r["objective_sem"]) for r in optimizer.history)

    beamline = simulator.SimulatedBeamline()
    window = beamline.window
    beamline.window = lambda channels, start, end: (lambda d: d and {c: (v[0], math.nan, 1) for c, v in d.items()})(
        window(channels, start, end))
    _, _, result, _ = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "instrument error" and "Fewer than 2 samples" in result["reason"], result


def test_every_averaging_window_is_announced_and_closed(rt):
    _, simulator = rt
    beamline = simulator.SimulatedBeamline()
    marks, calls = [], []
    beamline.measuring = marks.append
    window = beamline.window

    def failing(channels, start, end):
        calls.append(1)
        if len(calls) == 6:
            beamline.devices_on = False  # The next poll, inside the sixth window, finds the device off.
            return None
        return window(channels, start, end)
    beamline.window = failing
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "instrument error" and optimizer.counts["measures"] == 5
    assert marks[0::2] == [True] * (len(marks) // 2) and marks[1::2] == [False] * (len(marks) // 2)
    assert len(marks) == 2 * (optimizer.counts["measures"] + 1)  # The failed window was closed too.


def test_no_beam_at_the_stage_start_refuses_the_stage(rt):
    _, simulator = rt
    beamline = simulator.SimulatedBeamline(source=1e-17)  # Spray off: only amplifier noise remains.
    optimizer, beamline, result, _ = run(rt, FUNNEL, strategy="coordinate", beamline=beamline)
    assert result["status"] == "instrument error" and "no beam reaches" in result["reason"]


# ---------------------------------------------------------------- simple settings (beamline description)

SIM_SETTINGS = dict(settle_s=0.5, average_s=1.0, seed=1)


@pytest.fixture(scope="module")
def bl(rt):
    return importlib.import_module(f"{NAME}._beamline")


def test_builder_turns_the_simple_settings_into_a_valid_configuration(rt, bl):
    engine, simulator = rt
    text = bl.build(simulator.BEAMLINE, settings=SIM_SETTINGS)
    config = engine.parse_config(text)
    assert [s.name for s in config.stages] == ["Inlet + funnel", "Q1", "Q2", "Q3", "Q4", "Final refinement"]
    funnel, q1, *_, q4, final = config.stages
    assert funnel.aperture == "A1" and funnel.downstream == ("A2", "A3", "A4", "Collector") and funnel.normalize_by == ()
    assert q1.aperture == "A2" and q1.normalize_by == ("A1", "A2", "A3", "A4", "Collector")
    assert q4.aperture is None and q4.downstream == ("Collector",)
    assert final.downstream == ("Collector",) and {c for k in final.knobs for c in k.channels} == {
        "Funnel_exit", "Q1_offset", "Q2_offset", "Q3_offset", "Q4_offset"}
    rf = next(k for k in q1.knobs if "Q1_RF" in k.channels)
    window, step = rf.resolve({"Q1_RF": 180.0})  # Relative: a fraction of the start amplitude.
    assert rf.relative and window == pytest.approx((-27.0, 27.0)) and step == pytest.approx(3.6)
    offset = next(k for k in final.knobs if "Q1_offset" in k.channels)
    assert offset.window == (-4.0, 4.0) and offset.max_step == pytest.approx(0.4)  # final stage: at most "fine"
    assert config.pressure == {"P_funnel": (2.0, 6.0), "P_Q4": (0.0, 1e-5)} and config.target is None
    assert (config.settle_s, config.average_s, config.seed) == (0.5, 1.0, 1)
    wide = engine.parse_config(bl.build(simulator.BEAMLINE, selected=["funnel"], level="wide"))
    inlet = wide.stages[0].knobs[0]
    assert len(wide.stages) == 1 and inlet.window == (-50.0, 50.0) and inlet.max_step == 5.0


def test_builder_mass_mode_keeps_the_filter_off_the_knobs_and_marks_peak_stages(rt, bl):
    engine, simulator = rt
    for key in ("q1", "q2", "q3", "q4"):  # Any quadrupole can be the filter.
        config = engine.parse_config(bl.build(simulator.BEAMLINE, mode="mass", filter_key=key, center=300.0, width=20.0))
        rf = f"Q{key[1]}_RF"
        assert config.target.channels == {rf: 1.0} and config.target.filter_name == f"Q{key[1]}"
        assert rf not in config.knob_channels and rf in config.driven_channels
        position = int(key[1])
        assert config.target.measure == tuple(["A1", "A2", "A3", "A4", "Collector"][position:])
        peaks = {s.name: s.peak for s in config.stages}
        assert peaks["Inlet + funnel"] and peaks["Final refinement"]
        assert all(peaks[f"Q{i}"] == (i <= position) for i in range(1, 5) if f"Q{i}" in peaks)
    # The filter's own stage keeps its offset when its amplitude is the filter.
    plan = {key: knobs for key, _, knobs, *_ in bl.stages(simulator.BEAMLINE, mode="mass", filter_key="q2")}
    assert plan["q2"] == ["q2_offset"]


@pytest.mark.parametrize("kwargs, message", [
    (dict(mode="mass", filter_key="q2", center=None, width=20.0), "Choose the peak"),
    (dict(mode="mass", filter_key="q2", center=300.0, width=0.0), "Choose the peak"),
    (dict(mode="mass", filter_key="q5", center=300.0, width=20.0), "quadrupole"),
    (dict(selected=[]), "No stage can run"),
    (dict(level="huge"), "level"),
    (dict(mode="peak"), "mode"),
])
def test_builder_refuses_incomplete_choices(rt, bl, kwargs, message):
    _, simulator = rt
    with pytest.raises(ValueError, match=message):
        bl.build(simulator.BEAMLINE, **kwargs)


def test_beamline_description_round_trips_and_rejects_conflicts(rt, bl):
    _, simulator = rt
    assert bl.loads(bl.dumps(simulator.BEAMLINE)) == bl.normalize(simulator.BEAMLINE)
    assert bl.summary(simulator.BEAMLINE) == "12 settings · 5 currents · 2 gauges"
    for broken, message in (
        (dict(knobs={"q1_rf": {"A1": 1.0}}, collectors={"A1": "A1"}), "both driven and measured"),
        (dict(collectors={"A1": "X", "A2": "X"}), "two apertures"),
        (dict(knobs={"q1_rf": {"X": 0.0}}), "nonzero"),
        (dict(knobs={"q9_rf": {"X": 1.0}}), "Unknown setting"),
        (dict(gauges={"P": [5.0, 1.0]}), "minimum < maximum"),
        (dict(polarity=0), "polarity"),
    ):
        with pytest.raises(ValueError, match=message):
            bl.normalize(broken)


def test_partial_beamline_runs_only_what_is_measured(rt, bl):
    engine, _ = rt
    beamline = dict(knobs={"funnel_exit": {"PSU Exit": 1.0}, "q2_offset": {"PSU Q2": 1.0},
                           "q2_rf": {"AMX Vpos": 1.0, "AMX Vneg": 1.0}},
                    collectors={"A1": "DMMR 1", "collector": "DMMR 5"})
    assert bl.filters(beamline) == [("q2", "Q2")] and bl.filter_measure(beamline, "q2") == ["DMMR 5"]
    config = engine.parse_config(bl.build(beamline, mode="mass", filter_key="q2", center=250.0, width=15.0))
    assert [s.name for s in config.stages] == ["Inlet + funnel", "Q2", "Final refinement"]
    assert config.stages[0].aperture == "DMMR 1" and config.stages[0].downstream == ("DMMR 5",)
    assert config.target.channels == {"AMX Vpos": 1.0, "AMX Vneg": 1.0}  # MScan rails: Vpos = Vneg = A.
    assert bl.stages(dict(knobs={"q4_rf": {"X": 1.0}}, collectors={})) == []


# ---------------------------------------------------------------- peaks

def _gaussian(x, mu, sigma):
    return np.exp(-0.5 * ((np.asarray(x) - mu) / sigma) ** 2)


def test_pick_peak_finds_the_clicked_peak_despite_noise(rt):
    engine, _ = rt
    x = np.linspace(100.0, 900.0, 81)
    rng = np.random.default_rng(5)
    clean = 3e-11 * _gaussian(x, 300.0, 20.0) + 1.2e-11 * _gaussian(x, 520.0, 30.0)
    noisy = clean + 6e-13 * rng.standard_normal(len(x))
    for near, mu, sigma in ((280.0, 300.0, 20.0), (560.0, 520.0, 30.0), (420.0, 520.0, 30.0)):
        center, width = engine.pick_peak(x, noisy, near)
        assert center == pytest.approx(mu, abs=4.0)
        assert width == pytest.approx(1.1774 * sigma, rel=0.25)  # Half width at half maximum.
    # A peak cut by the spectrum edge stays usable; the order of the samples does not matter.
    center, width = engine.pick_peak(x[::-1][:39], clean[::-1][:39], 850.0)  # 900 V down to the top at 520 V.
    assert center == pytest.approx(520.0) and width == pytest.approx(1.1774 * 30.0, rel=0.1)
    with pytest.raises(ValueError, match="three points"):
        engine.pick_peak([1.0, 2.0], [1.0, 2.0], 1.5)
    with pytest.raises(ValueError, match="No positive peak"):
        engine.pick_peak(x, np.zeros_like(x), 300.0)


def test_fit_peak_uses_the_vertex_inside_the_samples_and_the_maximum_otherwise(rt):
    engine, _ = rt
    x = np.linspace(-2.0, 2.0, 5) + 500.0
    y = 5.0 - (x - 500.4) ** 2
    height, sem, center = engine.fit_peak(x, y, np.full(5, 0.01))
    assert (height, center) == (pytest.approx(5.0), pytest.approx(500.4)) and sem >= 0.01
    rising = x - 490.0
    height, _, center = engine.fit_peak(x, rising, np.zeros(5))
    assert (height, center) == (pytest.approx(12.0), pytest.approx(502.0))


@pytest.mark.parametrize("snippet, message", [
    ("[target]\nchannels={}\ncenter=1.0\nwidth=1.0\nmeasure=['B']", "channels must map"),
    ("[target]\nchannels={F=1.0}\ncenter=1.0\nwidth=1.0\nmeasure=['B']\npoints=4", "odd integer"),
    ("[target]\nchannels={F=1.0}\ncenter=1.0\nwidth=0.0\nmeasure=['B']", "width must be positive"),
    ("[target]\nchannels={F=1.0}\ncenter=1.0\nwidth=1.0\nmeasure=['F']", "filter channel cannot also be measured"),
    ("[target]\nmode='total'\nchannels={F=1.0}\ncenter=1.0\nwidth=1.0\nmeasure=['B']", 'mode must be "mass"'),
    ("[target]\nchannels={F=1.0}\ncenter=1.0\nwidth=1.0\nmeasure=['B']\ncolour=1", "unknown key"),
])
def test_invalid_targets_are_refused(rt, snippet, message):
    engine, _ = rt
    stage = "\n[[stage]]\nname='a'\ndownstream=['B']\npeak=true\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0"
    with pytest.raises(engine.ConfigError, match=message):
        engine.parse_config(snippet + stage)


def test_peak_stage_needs_a_target_and_never_drives_the_filter(rt):
    engine, _ = rt
    stage = "[[stage]]\nname='a'\ndownstream=['B']\npeak=true\n[[stage.knob]]\nchannels={X=1.0}\nwindow=[-1.0, 2.0]\nmax_step=1.0"
    with pytest.raises(engine.ConfigError, match="target"):
        engine.parse_config(stage)
    target = "[target]\nchannels={X=1.0}\ncenter=1.0\nwidth=1.0\nmeasure=['B']\n"
    with pytest.raises(engine.ConfigError, match="X"):
        engine.parse_config(target + stage)


def test_quick_sweep_returns_the_filter_to_its_amplitude_with_small_steps(rt):
    engine, simulator = rt
    beamline = simulator.SimulatedBeamline(seed=4)
    amplitudes = np.linspace(150.0, 700.0, 45)
    step = float(amplitudes[1] - amplitudes[0])
    target = engine.Target({"Q2_RF": 1.0}, 150.0, step, ["A3", "A4", "Collector"], max_step=step, spectrum_points=0)
    config = engine.Config([], None, target, **SIM_SETTINGS)
    points = []
    optimizer = engine.Optimizer(config, beamline, on_event=lambda kind, data: points.append(data) if kind == "sweep" else None)
    outcome = optimizer.spectrum(amplitudes, target.measure)
    assert outcome["status"] == "completed" and len(outcome["current"]) == len(points) == 45
    assert beamline.setpoint["Q2_RF"] == simulator.KNOBS["Q2_RF"][2]
    previous = simulator.KNOBS["Q2_RF"][2]
    for _, command in beamline.commands:
        assert abs(command["Q2_RF"] - previous) <= step + 1e-9
        previous = command["Q2_RF"]
    center, width = engine.pick_peak(outcome["amplitude"], outcome["current"], 500.0)
    assert center == pytest.approx(520.0, abs=8.0) and width == pytest.approx(31.5, rel=0.3)


def test_a_sweep_or_target_beyond_the_device_limits_is_refused_before_any_move(rt, bl):
    engine, simulator = rt
    beamline = simulator.SimulatedBeamline(seed=4)
    target = engine.Target({"Q2_RF": 1.0}, 900.0, 50.0, ["Collector"], max_step=50.0, spectrum_points=0)
    optimizer = engine.Optimizer(engine.Config([], None, target, **SIM_SETTINGS), beamline)
    outcome = optimizer.spectrum(np.linspace(800.0, 1100.0, 7), ["Collector"])  # Q2_RF Max is 1000 V.
    assert outcome["status"] == "configuration error" and "800–1100 V" not in outcome["reason"]
    assert "1050–1100 V leaves the device limits [0, 1000]" in outcome["reason"] and beamline.commands == []
    text = bl.build(simulator.BEAMLINE, selected=["q1"], mode="mass", filter_key="q2", center=520.0, width=30.0,
                    settings=SIM_SETTINGS).replace('width = 30.0', 'width = 30.0\nspectrum_span = 20.0')
    optimizer, beamline, result, _ = run(rt, text)
    assert result["status"] == "configuration error" and "± 600 V" in result["reason"] and beamline.commands == []


def test_mass_mode_tracks_the_selected_peak_and_raises_it_at_the_end_of_the_beamline(rt, bl):
    engine, simulator = rt
    text = bl.build(simulator.BEAMLINE, selected=["funnel", "q1", "q2"], mode="mass", filter_key="q2",
                    center=520.0, width=30.0, settings=SIM_SETTINGS)
    optimizer, beamline, result, events = run(rt, text, seed=1)
    assert result["status"] == "completed", result["reason"]
    assert result["target"]["final_center"] == pytest.approx(520.0, abs=10.0)
    assert all(stage["peak_center"] == pytest.approx(520.0, abs=10.0) for stage in result["stages"])
    assert beamline.setpoint["Q2_RF"] == pytest.approx(result["target"]["final_center"])
    # The selected mass is what the collector receives at the end, at the final filter amplitude.
    before, after = (result["checks"][k]["currents"]["Collector"] for k in ("before", "after"))
    assert after > 1.5 * before
    spectra = result["spectra"]
    assert len(spectra["before"]["amplitude"]) == 21 and max(spectra["after"]["current"]) > max(spectra["before"]["current"])
    assert [kind for kind, _ in events if kind == "spectrum"] == ["spectrum", "spectrum"]
    # The filter stage's criterion excludes its own exit aperture.
    assert optimizer.peak_measure(optimizer.config.stage("Q2")) == ["A4", "Collector"]
    assert optimizer.peak_measure(optimizer.config.stage("Q1")) == ["A3", "A4", "Collector"]
    assert_step_limited(rt, beamline, text)


def test_restore_initial_returns_the_filter_and_the_knobs(rt, bl):
    engine, simulator = rt
    text = bl.build(simulator.BEAMLINE, selected=["q1"], mode="mass", filter_key="q2", center=520.0, width=30.0,
                    settings=SIM_SETTINGS)
    optimizer, beamline, result, _ = run(rt, text, seed=2)
    assert result["status"] == "completed" and beamline.setpoint["Q2_RF"] != simulator.KNOBS["Q2_RF"][2]
    assert optimizer.restore_initial() == dict(status="restored", reason="")
    for channel in ("Q1_RF", "Q1_offset", "Q2_RF"):
        assert beamline.setpoint[channel] == simulator.KNOBS[channel][2]


# ---------------------------------------------------------------- logs

@pytest.fixture(scope="module")
def lg(rt):
    return importlib.import_module(f"{NAME}._log")


def test_log_lines_are_strict_json_with_sequence_and_time(lg, tmp_path):
    import json
    log = lg.JsonlLog(tmp_path / "a" / "x.jsonl")
    log.write("first", dict(value=np.float64(1.5), array=np.array([1.0, np.nan]), bad=math.inf, path=tmp_path, items=(1, 2)))
    log.write("second", note="é")
    log.close()
    log.write("ignored")  # After close: dropped, never raises.
    lines = (tmp_path / "a" / "x.jsonl").read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)  # Strict JSON: no NaN/Infinity tokens.
    assert "NaN" not in lines[0] and "Infinity" not in lines[0]
    assert (first["seq"], first["event"], first["value"], first["array"], first["bad"]) == (1, "first", 1.5, [1.0, None], None)
    assert first["path"] == str(tmp_path) and first["items"] == [1, 2] and second["note"] == "é" and second["t"] >= first["t"]
    assert lg.read(tmp_path / "a" / "x.jsonl")[1]["seq"] == 2


def test_session_log_rotates_and_run_logs_keep_the_newest(lg, tmp_path):
    log = lg.JsonlLog(tmp_path / "s.jsonl", max_bytes=400, backups=2)
    for i in range(40):
        log.write("event", i=i, padding="x" * 40)
    log.close()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["s.jsonl", "s.jsonl.1", "s.jsonl.2"]
    assert all(p.stat().st_size < 400 + 120 for p in tmp_path.iterdir())
    assert lg.read(tmp_path / "s.jsonl")[-1]["i"] == 39
    paths = []
    for _ in range(5):
        run = lg.run_log(tmp_path / "logs", "run", keep=3)
        run.write("header")
        run.close()
        paths.append(run.path)
    assert sorted(p.name for p in (tmp_path / "logs" / "runs").iterdir()) == sorted(p.name for p in paths[-3:])
    for path in paths[-3:]:
        path.write_text("x" * 100)
    lg.run_log(tmp_path / "logs", "run", max_total_bytes=250).close()  # Room for the two newest only.
    assert sorted(p.name for p in (tmp_path / "logs" / "runs").iterdir()) == sorted(p.name for p in paths[-2:])


def test_an_unwritable_log_folder_never_stops_anything(lg, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    log = lg.JsonlLog(blocker / "x.jsonl")
    log.write("event")
    log.write("event")
    assert log.error and "x.jsonl" not in [p.name for p in tmp_path.iterdir()]
    run = lg.run_log(blocker, "run")
    run.write("header")
    assert run.error
    null = lg.NullLog("no data path")
    null.write("event", value=1)
    null.close()
    assert null.path is None and null.error == "no data path"


def test_engine_log_traces_every_move_window_decision_and_outcome(rt, lg):
    import json
    engine, simulator = rt
    events = []
    config = engine.parse_config(FUNNEL)
    optimizer = engine.Optimizer(config, simulator.SimulatedBeamline(seed=3), log=lambda event, data: events.append((event, data)))
    result = optimizer.run()
    assert result["status"] == "completed"
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert {"stage_start", "reference", "step", "measure", "evaluation", "ask", "tell", "verify", "stage_done", "snapshot"} <= set(kinds)
    for kind, data in events:
        json.dumps(lg.clean(data), allow_nan=False)  # Everything the engine logs can be written.
    start = dict(events[0][1])
    assert start["strategy"] == "bayesian" and start["options"]["settle_s"] == 0.5 and start["stages"][0]["budget"] == 40
    steps = [data for kind, data in events if kind == "step"]
    assert len(steps) == optimizer.counts["steps"] and all(s["settle_s"] >= 0 and "readbacks" in s for s in steps)
    measures = [data for kind, data in events if kind == "measure"]
    assert len(measures) == optimizer.counts["measures"] and measures[0]["attempt"] == 1 and measures[0]["empty"] == []
    asks = [data for kind, data in events if kind == "ask"]
    assert all({"radius", "fits", "predicted", "predicted_std", "lengthscales", "fit_s"} <= set(a) for a in asks)
    evaluations = [data for kind, data in events if kind == "evaluation"]
    assert len(evaluations) == len(optimizer.history) and {"objective", "incident", "transmitted", "valid"} <= set(evaluations[0])
    verify = next(data for kind, data in events if kind == "verify")
    assert len(verify["gains"]) == config.verify_pairs and verify["adopt"] == result["stages"][0]["adopted"]
    end = events[-1][1]
    assert end["status"] == "completed" and end["traceback"] is None and end["counts"] == optimizer.counts
    done = next(data for kind, data in events if kind == "stage_done")
    assert done["duration_s"] > 0 and done["strategy_state"]["fits"] > 0


def test_engine_log_keeps_the_traceback_and_a_failing_log_changes_nothing(rt):
    engine, simulator = rt
    events = []
    beamline = simulator.SimulatedBeamline()

    def off(kind, data):
        if kind == "evaluation" and data["record"]["index"] == 3:
            beamline.devices_on = False
    optimizer = engine.Optimizer(engine.parse_config(FUNNEL), beamline, strategy="coordinate", on_event=off,
                                 log=lambda event, data: events.append((event, data)))
    result = optimizer.run()
    assert result["status"] == "instrument error"
    end = events[-1][1]
    assert events[-1][0] == "run_end" and "InstrumentError" in end["traceback"] and "switched off" in end["reason"]

    def broken(event, data):
        raise OSError("disk full")
    reference = run(rt, FUNNEL, strategy="coordinate", seed=5)[2]
    optimizer = engine.Optimizer(engine.parse_config(FUNNEL), simulator.SimulatedBeamline(seed=5), strategy="coordinate",
                                 log=broken)
    assert optimizer.run()["stages"] == reference["stages"]


def test_engine_log_of_a_selected_mass_records_sweeps_and_peaks(rt, bl):
    engine, simulator = rt
    text = bl.build(simulator.BEAMLINE, selected=["q1"], mode="mass", filter_key="q2", center=520.0, width=30.0,
                    settings=SIM_SETTINGS)
    events = []
    optimizer = engine.Optimizer(engine.parse_config(text), simulator.SimulatedBeamline(seed=1),
                                 log=lambda event, data: events.append((event, data)))
    assert optimizer.run()["status"] == "completed"
    sweeps = [data for kind, data in events if kind == "sweep"]
    assert [s["label"] for s in sweeps] == ["before", "after"] and len(sweeps[0]["amplitude"]) == 21
    peaks = [data["peak"] for kind, data in events if kind == "evaluation"]
    assert all(p is not None and {"fitted_center", "height", "measure"} <= set(p) for p in peaks)
    assert next(data for kind, data in events if kind == "stage_start")["peak_measure"] == ["A3", "A4", "Collector"]
    events.clear()
    optimizer.restore_initial()
    assert [kind for kind, _ in events][0] == "revert_start" and events[-1][0] == "revert_end"
    assert events[-1][1]["status"] == "restored"
