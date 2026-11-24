"""Transmission's mass-filter rules: the physics shared with MScan and the constants fitted on Q2 (2026-11-24).

User request (2026-11-24): select one m/z, filter it as finely as possible on Q2 and/or Q4, with settings from
internal rules built on the quadrupole's physics and validated by the data. The expected numbers come from the Q2
review (~/ESIBD/quadrupole_Q2/analysis/report_2026-11-19.md) and from the check on the raw files
(analysis/verify_rules.py, 2026-11-24).
"""
from __future__ import annotations

import importlib
import math
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _runtime():
    root = ROOT / "transmission" / "_runtime"
    name = "_test_transmission_rules_runtime"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{name}._filter_rules"), importlib.import_module(f"{name}._quadrupole")


rules_module, physics = _runtime()
RULES = rules_module.Rules()
Q = rules_module.Quadrupole
MZ = dict(m622=622.0290, m922=922.0098, m1522=1521.9715, m2122=2121.9332)


def test_the_physics_is_one_file_shared_word_for_word_with_mscan():
    assert (ROOT / "mscan" / "_runtime" / "_quadrupole.py").read_bytes() == \
        (ROOT / "transmission" / "_runtime" / "_quadrupole.py").read_bytes()


def test_the_ideal_resolution_and_centre_are_those_of_the_review():
    ideal = [physics.ideal_resolution(k, 0.978) for k in (0.200, 0.20556, 0.20806, 0.20936)]
    assert ideal == pytest.approx([14.59, 30.96, 61.67, 126.5], rel=3e-3)
    assert physics.closing_factor(0.978) == pytest.approx(0.210602, abs=2e-6)
    # 922 at 500 kHz, k 0.200: predicted interval 86.748–92.905 V, midpoint 89.827 V (review, section 2).
    centre = physics.amplitude_from_mz(MZ["m922"], frequency_hz=5e5, r0_mm=2.625, factor=0.200, attenuation=0.978)
    assert centre == pytest.approx(89.827, abs=0.01)
    back = physics.mz_from_amplitude([centre], frequency_hz=5e5, r0_mm=2.625, factor=0.200, attenuation=0.978)[0]
    assert back == pytest.approx(MZ["m922"], rel=2e-4)  # MScan's labels (5e-5 grid) agree with the inverse.


def test_the_transit_model_predicts_the_measured_resolution():
    # 922 at 500 kHz, 5 eV, k 0.20806: measured R 47.4 (13 Nov) and 46.4 (18 Nov).
    assert RULES.resolution(0.20806, MZ["m922"], 5e5) == pytest.approx(45.5, abs=0.3)
    assert RULES.cycles(MZ["m922"], 5e5) == pytest.approx(73.3, abs=0.1)
    assert RULES.resolution_cap(MZ["m922"], 5e5) == pytest.approx(119.7, abs=0.3)
    k, reachable = RULES.k_for_resolution(60.0, MZ["m922"], 5e5)
    assert reachable and RULES.resolution(k, MZ["m922"], 5e5) == pytest.approx(60.0, rel=1e-6)
    assert RULES.start_k(MZ["m922"], 5e5) == pytest.approx(0.20904, abs=2e-5)  # Half of the cap: R ≈ 60.
    assert RULES.k_for_resolution(500.0, MZ["m922"], 5e5) == (pytest.approx(RULES.k_max()), False)  # Beyond the cap.
    assert RULES.k_for_resolution(5.0, MZ["m922"], 5e5) == (0.200, True)  # The floor (user, 2026-11-24).


def test_the_axis_potential_follows_q1_and_the_mass():
    assert RULES.stopping_potential(MZ["m922"], 36.0) == pytest.approx(35.59)
    assert RULES.stopping_potential(MZ["m2122"], 36.0) == pytest.approx(34.97)
    assert RULES.stopping_potential(MZ["m922"], 38.0) - RULES.stopping_potential(MZ["m922"], 36.0) == pytest.approx(1.90)


def test_the_centre_correction_learned_on_the_raw_files():
    assert RULES.centre_correction(5e5) == pytest.approx(-0.08)
    assert RULES.centre_correction(549.451e3) == pytest.approx(-0.30)
    assert RULES.centre_correction(450e3) == pytest.approx((-0.55 - 0.08) / 2)
    assert RULES.centre_correction(700e3) == pytest.approx(-0.30)  # Held at the end of the measured range.


def test_a_plan_for_922_on_q2_with_q4_rf_only():
    quads = {"Q2": Q("Q2", max_amplitude_v=350.0, ilim_ma=300.0), "Q4": Q("Q4", offset_v=-12.0)}
    plan = RULES.plan(MZ["m922"], quads, ["Q2"], q1_dc=36.0)
    assert plan.refused == "" and plan.frequency_hz == 5e5 and plan.energy_ev == 5.0
    assert plan.k == pytest.approx(0.20904, abs=2e-5) and plan.r_predicted == pytest.approx(60, abs=0.5)
    q2, q4 = plan.settings
    assert (q2.name, q2.role, q4.name, q4.role) == ("Q2", "filter", "Q4", "rf only")
    assert q2.amplitude_v == pytest.approx(91.5 * (1 - 0.0008), abs=0.05)
    assert q2.u_v == pytest.approx(plan.k * q2.amplitude_v) and q2.x_v == pytest.approx(30.59)
    assert q2.rods == pytest.approx((30.59 + q2.u_v, 30.59 - q2.u_v))
    assert q4.amplitude_v == pytest.approx(66.4, abs=0.1) and q4.u_v == 0 and q4.x_v == -12.0
    assert q2.load_ma == pytest.approx(15.3 + 0.546 * q2.amplitude_v)
    assert plan.flags == []  # k 0.20904 lies inside the measured 0.200–0.20936.
    assert "k 0.20904" in plan.summary() and "Q2 filter: A 91.5 V" in plan.summary()


def test_limits_refuse_a_plan_and_q4_or_extrapolation_is_flagged():
    tight = {"Q2": Q("Q2", max_amplitude_v=80.0)}
    assert RULES.plan(MZ["m922"], tight, ["Q2"], 36.0).refused.startswith("Q2: A 91.5 V above the PSU limit 80.0 V")
    low_ilim = {"Q2": Q("Q2", ilim_ma=60.0)}
    assert "(set by the operator on the PSU)" in RULES.plan(MZ["m922"], low_ilim, ["Q2"], 36.0).refused
    plan = RULES.plan(MZ["m2122"], {"Q4": Q("Q4")}, ["Q4"], 36.0, k=0.2100)
    assert "k 0.21 outside the measured 0.2–0.20936: extrapolated" in plan.flags
    assert "Q4 not characterized: Q2's rules, to be checked" in plan.flags
    with pytest.raises(ValueError):
        RULES.plan(MZ["m922"], {"Q2": Q("Q2")}, [], 36.0)


def test_the_upward_ladder_stops_before_the_closing_or_when_r_stops_gaining():
    k_max = RULES.k_max()
    assert k_max == pytest.approx(0.210602 - 0.00025 - 0.0001, abs=2e-6)
    assert RULES.next_k(0.20904) == pytest.approx(0.20924)
    assert RULES.next_k(k_max - 0.0001) is None
    assert RULES.ladder_continues(60.0, 66.0, 10e-12, 50e-15) == (True, "")
    assert RULES.ladder_continues(60.0, 62.0, 10e-12, 50e-15)[1] == "R gains less than 5 % per step"
    assert RULES.ladder_continues(60.0, 80.0, 0.4e-12, 50e-15)[1] == "peak under 10 × the noise of its flanks"


def test_frequency_candidates_at_equal_resolution():
    quads = {"Q2": Q("Q2", max_amplitude_v=350.0, ilim_ma=300.0)}
    plans = RULES.frequency_plans(MZ["m922"], 60.0, quads, ["Q2"], 36.0)
    assert [round(p.frequency_hz / 1e3, 3) for p in plans] == [500.0, 549.451]
    assert all(p.r_predicted == pytest.approx(60.0, rel=1e-5) and not p.refused for p in plans)
    assert plans[1].k < plans[0].k  # More cycles: the same R at a lower k.
    assert RULES.snap_frequency(300.3e3) == pytest.approx(100e6 / 334)  # Even periods only.


def test_unknown_rules_are_refused():
    with pytest.raises(ValueError, match="Unknown filter rule"):
        rules_module.Rules({"energy": 4.0})
    assert rules_module.Rules({"energy_ev": 6.0}).plan(MZ["m922"], {"Q2": Q("Q2")}, ["Q2"], 36.0).energy_ev == 6.0


def test_the_target_of_a_plan_moves_rails_and_rods_with_the_mz_and_rods_with_q1():
    quads = {"Q2": Q("Q2"), "Q4": Q("Q4")}
    wiring = {"Q2": dict(rails=("PSU_A_CH0", "PSU_A_CH1"), rods=("Q2_DC_1", "Q2_DC_2")),
              "Q4": dict(rails=("PSU_B_CH0", "PSU_B_CH1"), rods=("Q4_DC_1", "Q4_DC_2"))}
    plan = RULES.plan(MZ["m922"], quads, ["Q2"], q1_dc=36.0)
    gains, offsets, follow, origin, amplitude = rules_module.target_parts(plan, wiring, "Q1_DC", 36.0)
    q2 = plan.settings[0]
    mz = MZ["m922"]
    assert amplitude == "PSU_A_CH0" and set(gains) == {"PSU_A_CH0", "PSU_A_CH1", "Q2_DC_1", "Q2_DC_2"}  # Q4: RF only.
    assert gains["PSU_A_CH0"] * mz == pytest.approx(q2.amplitude_v)
    assert offsets["Q2_DC_1"] + gains["Q2_DC_1"] * mz == pytest.approx(q2.rods[0])
    assert offsets["Q2_DC_2"] + gains["Q2_DC_2"] * mz == pytest.approx(q2.rods[1])
    assert follow == {"Q2_DC_1": {"Q1_DC": 0.95}, "Q2_DC_2": {"Q1_DC": 0.95}} and origin == {"Q1_DC": 36.0}
    both = RULES.plan(MZ["m922"], quads, ["Q2", "Q4"], q1_dc=36.0)
    gains, *_ = rules_module.target_parts(both, wiring)
    assert set(gains) == {"PSU_A_CH0", "PSU_A_CH1", "Q2_DC_1", "Q2_DC_2", "PSU_B_CH0", "PSU_B_CH1", "Q4_DC_1", "Q4_DC_2"}


def test_a_band_is_measured_by_its_outer_half_maximum_on_a_flat_background():
    import numpy as np
    x = np.linspace(900.0, 944.0, 45)
    band = np.clip(1 - np.abs(x - 922.0) / 11.5, 0, None)  # Triangle, FWHM 11.5, centre 922.
    noise = np.random.default_rng(3).normal(0, 2e-15, x.size)
    found = rules_module.measure_band(x, 8e-12 + 40e-12 * band + noise)
    assert found["center"] == pytest.approx(922.0, abs=0.2) and found["fwhm"] == pytest.approx(11.5, abs=0.4)
    assert found["r"] == pytest.approx(922.0 / 11.5, rel=0.04) and found["height"] == pytest.approx(40e-12, rel=0.05)
    assert 1e-15 < found["noise"] < 4e-15
    cut = rules_module.measure_band(x[:20], (8e-12 + 40e-12 * band)[:20])  # The band runs off the sweep.
    assert math.isnan(cut["r"]) and cut["height"] > 0
    assert math.isnan(rules_module.measure_band(x[:5], band[:5])["height"])


def _fake_instrument(cap=90.0, noise=50e-15):
    """R saturates at ``cap`` near the closing while the peak height falls: the measured counterpart of the model."""
    def measure(plan):
        r_ideal = physics.ideal_resolution(plan.k, 0.978)
        r = 1 / (1 / r_ideal + 1 / cap)
        height = 40e-12 * max(0.0, (0.2106 - plan.k) / 0.0106) ** 0.5
        return dict(center=plan.mz, fwhm=plan.mz / r, r=r, height=height, noise=noise)
    return measure


def test_the_ladder_climbs_while_r_gains_and_stops_at_the_saturation():
    quads = {"Q2": Q("Q2")}
    plans, reports = [], []

    def plan_at(k):
        plans.append(k)
        return RULES.plan(MZ["m922"], quads, ["Q2"], 36.0, k=k)
    best, steps, why = rules_module.climb(RULES, RULES.start_k(MZ["m922"], 5e5), plan_at, _fake_instrument(cap=30.0),
                                          report=lambda plan, band: reports.append(round(band["r"])))
    assert why == "R gains less than 5 % per step"
    assert all(b > a for a, b in zip(plans, plans[1:])) and len(steps) == len(reports) >= 2
    _, far, far_why = rules_module.climb(RULES, 0.2090, plan_at, _fake_instrument(cap=1e4))
    assert far_why == "closing reached" and far[-1][0].k <= RULES.k_max()
    assert best[1]["r"] == max(band["r"] for _, band in steps)
    assert best[0].k < RULES.k_max()


def test_the_ladder_stops_when_the_peak_sinks_into_the_noise():
    quads = {"Q2": Q("Q2")}
    best, steps, why = rules_module.climb(RULES, 0.2090, lambda k: RULES.plan(MZ["m922"], quads, ["Q2"], 36.0, k=k),
                                          _fake_instrument(cap=1e6, noise=1.5e-12))
    assert why == "peak under 10 × the noise of its flanks"
    assert best is None or best[1]["height"] >= 10 * 1.5e-12  # Never a step that was not measurable.


def test_the_frequency_kept_is_the_highest_signal_at_equal_r():
    band = lambda r, h: dict(center=922.0, fwhm=922.0 / r, r=r, height=h, noise=1e-14)
    found = {500e3: ("p500", band(60, 10e-12)), 549.451e3: ("p549", band(63, 12e-12)), 400e3: ("p400", band(40, 30e-12))}
    assert rules_module.choose_frequency(found, 60)[0] == 549.451e3  # 400 kHz: more signal, but R 40 is not equal.
    assert rules_module.choose_frequency({400e3: found[400e3]}, 60)[0] == 400e3


def test_an_isolated_spike_is_not_taken_for_a_band():
    """2026-11-24, raw Q2 files: a single point 2–6 × its neighbours gave R up to 27 × the model."""
    import numpy as np
    x = np.linspace(1480.0, 1560.0, 41)
    band = 0.9e-12 * np.clip(1 - np.abs(x - 1522.0) / 25.0, 0, None)
    noise = np.random.default_rng(5).normal(0, 0.05e-12, x.size)
    clean = rules_module.measure_band(x, 8e-12 + band + noise)
    y = 8e-12 + band + noise
    y[22] += 2.0e-12  # One spike on the flank of the band.
    spiked = rules_module.measure_band(x, y)
    assert spiked["spikes"] == 1 and spiked["r"] == pytest.approx(clean["r"], rel=0.1) and spiked["points"] >= 10


def _sweep(widths):
    """A fake sweep returning successive bands of the given relative FWHM at m/z 922."""
    calls = []

    def sweep(center, half_span):
        fwhm = 922.0 * widths[min(len(calls), len(widths) - 1)]
        calls.append((center, half_span))
        points = max(1, int(round(fwhm / (2 * half_span / 20))))
        return dict(center=922.0, fwhm=fwhm, r=922.0 / fwhm, height=10e-12, noise=50e-15, points=points, spikes=0)
    return sweep, calls


def test_a_band_with_too_few_points_is_swept_again_finer_and_a_record_is_confirmed():
    plan = RULES.plan(MZ["m922"], {"Q2": Q("Q2")}, ["Q2"], 36.0)  # R model ≈ 60: ± 3 half-widths ≈ ± 23 m/z.
    sweep, calls = _sweep([1 / 60])
    band = rules_module.measured(RULES, plan, sweep)
    assert band["checks"] == [] and len(calls) == 1 and band["r_model"] == pytest.approx(plan.r_predicted)
    sweep, calls = _sweep([1 / 200, 1 / 200, 1 / 190])  # Narrow: 3 points, then 2 more sweeps.
    band = rules_module.measured(RULES, plan, sweep)
    assert band["checks"] == ["finer", "confirmed"] and len(calls) == 3 and band["r"] == pytest.approx(190, rel=0.01)
    assert calls[1][1] == pytest.approx(1.5 * 922 / 200)  # Finer: ± 1.5 measured FWHM.
    sweep, calls = _sweep([1 / 150, 1 / 150, 1 / 95])  # A record not repeated: the lower R counts.
    band = rules_module.measured(RULES, plan, sweep)
    assert band["checks"][-1] == "not confirmed" and band["r"] == pytest.approx(95, rel=0.01)


def test_the_quadrupole_that_does_not_filter_shows_its_real_setting():
    quads = {"Q2": Q("Q2"), "Q4": Q("Q4", offset_v=-12.0, u_v=18.7, amplitude_v=66.0)}
    plan = RULES.plan(MZ["m922"], quads, ["Q2"], 36.0)
    q4 = plan.settings[1]
    assert (q4.role, q4.amplitude_v, q4.u_v, q4.x_v, q4.u_present_v) == ("rf only", 66.0, 0.0, -12.0, 18.7)
    assert q4.cutoff_mz == pytest.approx(922.0098 * 0.4 / 0.712, rel=0.01)  # Its A puts 922 at q ≈ 0.40.
    high = RULES.plan(MZ["m922"], {"Q2": Q("Q2"), "Q4": Q("Q4", amplitude_v=150.0)}, ["Q2"], 36.0)
    assert any(flag.startswith("Q4 (RF only) cuts m/z below") for flag in high.flags)
    gains, offsets, amplitude = rules_module.u_zero_parts(("Q4_DC_1", "Q4_DC_2"), -12.0)
    assert gains == {"Q4_DC_1": 1.0, "Q4_DC_2": -1.0} and offsets == {"Q4_DC_1": -12.0, "Q4_DC_2": -12.0}
