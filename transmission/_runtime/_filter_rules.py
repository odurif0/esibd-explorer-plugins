"""Transmission's rules for a mass filter: from an m/z to the settings of Q2 and/or Q4, and the finest useful one.

User request (2026-11-24): select one m/z and filter it as finely as possible on Q2 and/or Q4, with settings that
come from internal rules built on the physics of the quadrupole and validated by the data; the RF frequency is
optimized too (the highest signal at equal R). The physics (square-wave stability, m/z ↔ A) is MScan's, shared word
for word (_quadrupole.py). The constants below are fitted on the Q2 campaign of 2–18 November 2026
(~/ESIBD/quadrupole_Q2: report_2026-11-19.md; centre and R checked again on the raw files by
analysis/verify_rules.py, 2026-11-24). Inside their domain they are validated by those data; outside it the rules
extrapolate and say so (``Plan.flags``). Q4 is built like Q2 (user, 2026-11-22): same rules, to be checked on Q4.

Resolution model (review, section 4): 1/R = s / R_ideal(k) + (C47/47²)(47/n)^p with n = f L sqrt(m / 2E) the RF
periods spent in the rods. As k approaches the closing, R_ideal grows without bound but R tends to the transit cap
1/T(n) while the signal collapses: the finest useful filter is found by measurement (``next_k``), starting from
``start_cap_fraction`` of the cap.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

from . import _quadrupole as physics

# The rules' constants (the "recipes" table, user 2026-11-24). Transmission's settings may override any of them.
RULES = dict(
    r0_mm=2.625,  # Effective field radius (Agilent mix, 2026-11-03); MScan's quadrupole profile overrides it.
    attenuation=0.978,  # Rod RF amplitude / commanded A; MScan's quadrupole profile overrides it.
    length_m=0.15,  # Q2 rods; Q4 is built alike.
    transit_p=1.44, transit_s=0.842, transit_c47=35.0,  # Review: free p and s, best held-out prediction.
    energy_ev=5.0,  # Axial energy per charge in the filter: the lowest measured (below 5 eV: not measured).
    stop_points=((922.0098, 35.59), (2121.9332, 34.97)),  # Stopping potential x50 (V) at Q1_DC 36 V, per m/z.
    stop_reference_q1=36.0, stop_slope=0.95,  # x50 follows Q1_DC: +1.90 V for +2 V.
    delta_v=0.0, umin_v=0.5,  # δ and the DC floor, as in every measured file.
    k_min=0.200,  # Floor: the measured domain starts there (user, 2026-11-24).
    k_step=0.0002,  # Step of the upward k ladder (as the closing plan of 2026-11-19).
    rods_k_offset=0.00025,  # The rods read back k + 0.00025 (2026-11-19): the closing comes that much earlier.
    closing_margin=0.0001,  # Commanded k stays at least this far below the effective closing.
    start_cap_fraction=0.5,  # Starting k: R = half of the transit cap.
    r_gain_stop=0.05,  # The k ladder stops when R gains less than 5 % per step.
    noise_factor=10.0,  # ... or when the peak falls under 10 × the noise of its flanks (no longer measurable).
    frequencies_khz=(500.0, 549.451),  # Candidates compared at equal R (measured domain).
    # Measured / predicted centre − 1 (%), median per frequency on the raw files (verify_rules.py, 2026-11-24).
    centre_correction=((299.4, -0.28), (400.0, -0.55), (500.0, -0.08), (549.451, -0.30)),
    load_ma=(15.3, 0.546), load_reference_khz=500.0,  # RF current per rail: (I0 + slope A) × f / 500 kHz.
    ilim_fraction=0.9,  # The predicted RF current stays under 90 % of Ilim (read, never written).
    rf_only_q=0.40,  # Suggested Mathieu q of the m/z in a quadrupole that does not filter (RF only).
    min_points=5,  # A band measured with fewer samples inside its FWHM is swept again, finer.
    confirm_ratio=1.5,  # A measured R above this × the model is measured again before it counts...
    confirm_tolerance=0.15,  # ... and counts if both agree within 15 % (else the lower one).
    clock_hz=100e6,  # AMX HD: frequencies with an even number of clock periods (duty 50 %).
    domain=dict(k=(0.200, 0.20936), f_khz=(299.4, 549.451), e_ev=(5.0, 26.0), mz=(622.0, 2122.0)),
)


@dataclass
class Quadrupole:
    """One quadrupole of the filter: its MScan profile (r0, attenuation) and its limits as read from the PSU."""

    name: str
    r0_mm: float | None = None
    attenuation: float | None = None
    max_amplitude_v: float | None = None  # The PSU's hardware voltage limit.
    ilim_ma: float | None = None  # The PSU's current limit: read only.
    offset_v: float | None = None  # Present DC offset to ground of both rod pairs (their mean, x).
    u_v: float | None = None  # Present resolving DC: half the difference of the rod pairs.
    amplitude_v: float | None = None  # Present RF amplitude A (a quadrupole that does not filter keeps it).


@dataclass
class Setting:
    name: str
    role: str  # "filter" or "rf only"
    amplitude_v: float
    u_v: float
    x_v: float
    load_ma: float
    u_present_v: float | None = None  # The resolving DC before the filter is set (RF only: brought to 0).
    cutoff_mz: float | None = None  # RF only: m/z below this are not transmitted (q > 0.712).

    @property
    def rods(self):
        """(pair 1, pair 2) = x ± U."""
        return self.x_v + self.u_v, self.x_v - self.u_v


@dataclass
class Plan:
    mz: float
    frequency_hz: float
    energy_ev: float
    k: float
    cycles: float
    r_cap: float
    r_predicted: float
    settings: list = field(default_factory=list)
    flags: list = field(default_factory=list)  # Extrapolations and limits, said to the operator and logged.
    refused: str = ""  # Why this plan must not be applied ('' when it may).

    def summary(self):
        quads = "; ".join(f"{s.name} {s.role}: A {s.amplitude_v:.1f} V, U {s.u_v:.2f} V, x {s.x_v:.2f} V"
                          for s in self.settings)
        return (f"m/z {self.mz:g}: {self.frequency_hz / 1e3:.3f} kHz, E {self.energy_ev:g} eV, k {self.k:.5f}, "
                f"n {self.cycles:.0f}, R {self.r_predicted:.0f} of cap {self.r_cap:.0f}; {quads}")


class Rules:
    def __init__(self, overrides=None):
        unknown = set(overrides or {}) - set(RULES)
        if unknown:
            raise ValueError(f"Unknown filter rule(s): {', '.join(sorted(unknown))}")
        self.c = dict(RULES, **(overrides or {}))

    # ---------------------------------------------------------------- model
    def _geometry(self, quad=None):
        r0 = quad.r0_mm if quad is not None and quad.r0_mm else self.c["r0_mm"]
        attenuation = quad.attenuation if quad is not None and quad.attenuation else self.c["attenuation"]
        return r0, attenuation

    def k_max(self, quad=None):
        """Largest commanded k: the closing, less the rods' read-back offset and a margin."""
        return physics.closing_factor(self._geometry(quad)[1]) - self.c["rods_k_offset"] - self.c["closing_margin"]

    def cycles(self, mz, frequency_hz, energy_ev=None):
        return physics.rf_cycles(mz, frequency_hz=frequency_hz, energy_ev=energy_ev or self.c["energy_ev"],
                                 length_m=self.c["length_m"])

    def transit(self, mz, frequency_hz, energy_ev=None):
        """The transit term T(n) of 1/R; 1/T is the resolution cap."""
        n = self.cycles(mz, frequency_hz, energy_ev)
        return self.c["transit_c47"] / 47 ** 2 * (47 / n) ** self.c["transit_p"]

    def resolution_cap(self, mz, frequency_hz, energy_ev=None):
        return 1 / self.transit(mz, frequency_hz, energy_ev)

    def resolution(self, k, mz, frequency_hz, energy_ev=None, quad=None):
        ideal = physics.ideal_resolution(k, self._geometry(quad)[1])
        if not math.isfinite(ideal):
            return math.nan
        return 1 / (self.c["transit_s"] / ideal + self.transit(mz, frequency_hz, energy_ev))

    def k_for_resolution(self, r, mz, frequency_hz, energy_ev=None, quad=None):
        """(k, reachable): the smallest k in [k_min, k_max] whose predicted R is at least ``r``."""
        low, high = self.c["k_min"], self.k_max(quad)
        if self.resolution(low, mz, frequency_hz, energy_ev, quad) >= r:
            return low, True
        if not self.resolution(high, mz, frequency_hz, energy_ev, quad) >= r:
            return high, False
        for _ in range(60):
            middle = (low + high) / 2
            if self.resolution(middle, mz, frequency_hz, energy_ev, quad) >= r:
                high = middle
            else:
                low = middle
        return high, True

    def start_k(self, mz, frequency_hz, energy_ev=None, quad=None):
        cap = self.resolution_cap(mz, frequency_hz, energy_ev)
        return self.k_for_resolution(self.c["start_cap_fraction"] * cap, mz, frequency_hz, energy_ev, quad)[0]

    def next_k(self, k, quad=None):
        """The next k of the upward ladder, or None at the closing."""
        step = k + self.c["k_step"]
        return round(step, 6) if step <= self.k_max(quad) + 1e-12 else None

    def ladder_continues(self, r_previous, r_now, height, noise):
        """(continue, why): R must still gain ``r_gain_stop`` per step and the peak stay measurable."""
        if not (math.isfinite(height) and math.isfinite(noise)) or height < self.c["noise_factor"] * noise:
            return False, f"peak under {self.c['noise_factor']:g} × the noise of its flanks"
        if math.isfinite(r_previous) and r_now < r_previous * (1 + self.c["r_gain_stop"]):
            return False, f"R gains less than {100 * self.c['r_gain_stop']:g} % per step"
        return True, ""

    def stopping_potential(self, mz, q1_dc):
        """x50 (V): where the ions of ``mz`` stop, following Q1_DC; linear in m/z between the measured ions."""
        (m1, x1), (m2, x2) = self.c["stop_points"]
        at_reference = x1 + (mz - m1) * (x2 - x1) / (m2 - m1)
        return at_reference + self.c["stop_slope"] * (q1_dc - self.c["stop_reference_q1"])

    def centre_correction(self, frequency_hz):
        """Measured / predicted centre − 1 (%) at this frequency: interpolated, held at the ends."""
        points = sorted(self.c["centre_correction"])
        f_khz = frequency_hz / 1e3
        if f_khz <= points[0][0]:
            return points[0][1]
        for (f1, c1), (f2, c2) in zip(points, points[1:]):
            if f_khz <= f2:
                return c1 + (f_khz - f1) * (c2 - c1) / (f2 - f1)
        return points[-1][1]

    def centre_amplitude(self, mz, frequency_hz, k, quad=None):
        r0, attenuation = self._geometry(quad)
        predicted = physics.amplitude_from_mz(mz, frequency_hz=frequency_hz, r0_mm=r0, factor=k,
                                              resolution=self.c["delta_v"], minimum=self.c["umin_v"],
                                              attenuation=attenuation)
        return predicted * (1 + self.centre_correction(frequency_hz) / 100)

    def rf_only_amplitude(self, mz, frequency_hz, quad=None):
        r0, attenuation = self._geometry(quad)
        return physics.amplitude_at_q(mz, self.c["rf_only_q"], frequency_hz=frequency_hz, r0_mm=r0,
                                      attenuation=attenuation)

    def load_ma(self, amplitude_v, frequency_hz):
        i0, slope = self.c["load_ma"]
        return (i0 + slope * amplitude_v) * frequency_hz / (self.c["load_reference_khz"] * 1e3)

    def snap_frequency(self, frequency_hz):
        """The AMX frequency nearest ``frequency_hz`` with an even number of clock periods (50 % duty)."""
        periods = max(2, 2 * round(self.c["clock_hz"] / frequency_hz / 2))
        return self.c["clock_hz"] / periods

    # ---------------------------------------------------------------- plans
    def plan(self, mz, quadrupoles, filters, q1_dc, frequency_hz=None, k=None):
        """The settings for ``mz``: each name of ``filters`` filters, every other quadrupole is RF only.

        ``quadrupoles``: name → Quadrupole (profile, limits, present offset). ``frequency_hz``: the rules'
        first candidate when None; ``k``: the starting k when None.
        """
        if not filters:
            raise ValueError("Choose at least one filter (Q2, Q4)")
        missing = [name for name in filters if name not in quadrupoles]
        if missing:
            raise ValueError(f"No quadrupole {', '.join(missing)}")
        f = self.snap_frequency(frequency_hz or self.c["frequencies_khz"][0] * 1e3)
        energy = self.c["energy_ev"]
        first = quadrupoles[filters[0]]
        k = self.start_k(mz, f, energy, first) if k is None else float(k)
        plan = Plan(mz=mz, frequency_hz=f, energy_ev=energy, k=k, cycles=self.cycles(mz, f, energy),
                    r_cap=self.resolution_cap(mz, f, energy), r_predicted=self.resolution(k, mz, f, energy, first))
        x = self.stopping_potential(mz, q1_dc) - energy
        for name, quad in quadrupoles.items():
            if name in filters:
                amplitude = self.centre_amplitude(mz, f, k, quad)
                u = max(self.c["umin_v"], k * amplitude - self.c["delta_v"])
                setting = Setting(name, "filter", amplitude, u, x, self.load_ma(amplitude, f))
            else:
                # Transmits all m/z above its cut-off: U = 0 between the rod pairs, its DC offset to ground (x)
                # and its RF amplitude kept as they are (its stage optimizes them).
                amplitude = quad.amplitude_v if quad.amplitude_v else self.rf_only_amplitude(mz, f, quad)
                offset = quad.offset_v if quad.offset_v is not None else x
                r0, attenuation = self._geometry(quad)
                cutoff = float(physics.mz_from_amplitude([amplitude], frequency_hz=f, r0_mm=r0, attenuation=attenuation)[0])
                setting = Setting(name, "rf only", amplitude, 0.0, offset, self.load_ma(amplitude, f), quad.u_v, cutoff)
            plan.settings.append(setting)
            if not math.isfinite(amplitude):
                plan.refused = f"{name}: no stable band at k {k:.5f}"
            elif quad.max_amplitude_v is not None and amplitude > quad.max_amplitude_v:
                plan.refused = f"{name}: A {amplitude:.1f} V above the PSU limit {quad.max_amplitude_v:.1f} V"
            elif quad.ilim_ma is not None and setting.load_ma > self.c["ilim_fraction"] * quad.ilim_ma:
                plan.refused = (f"{name}: predicted RF current {setting.load_ma:.0f} mA above "
                                f"{100 * self.c['ilim_fraction']:.0f} % of Ilim {quad.ilim_ma:.0f} mA "
                                "(set by the operator on the PSU)")
        plan.flags = self._flags(mz, f, energy, k, filters)
        for setting in plan.settings:
            if setting.role == "rf only" and setting.cutoff_mz and setting.cutoff_mz >= 0.9 * mz:
                plan.flags.append(f"{setting.name} (RF only) cuts m/z below {setting.cutoff_mz:.0f}: m/z {mz:g} hardly "
                                  "passes it; lower its RF amplitude")
        return plan

    def _flags(self, mz, frequency_hz, energy, k, filters):
        domain, flags = self.c["domain"], []
        for value, key, label in ((k, "k", "k"), (frequency_hz / 1e3, "f_khz", "frequency (kHz)"),
                                  (energy, "e_ev", "energy (eV)"), (mz, "mz", "m/z")):
            low, high = domain[key]
            if not low - 1e-9 <= value <= high + 1e-9:
                flags.append(f"{label} {value:g} outside the measured {low:g}–{high:g}: extrapolated")
        if any(name != "Q2" for name in filters):
            flags.append("Q4 not characterized: Q2's rules, to be checked")
        return flags

    def frequency_plans(self, mz, r, quadrupoles, filters, q1_dc):
        """At equal R (user, 2026-11-24: the highest signal at equal R), one plan per candidate frequency:
        each with the k that gives ``r`` there; candidates whose settings break a limit are refused."""
        plans = []
        for f_khz in self.c["frequencies_khz"]:
            f = self.snap_frequency(f_khz * 1e3)
            k, reachable = self.k_for_resolution(r, mz, f, self.c["energy_ev"], quadrupoles[filters[0]])
            plan = self.plan(mz, quadrupoles, filters, q1_dc, frequency_hz=f, k=k)
            if not reachable and not plan.refused:
                plan.refused = f"R {r:.0f} not reachable at {f / 1e3:.3f} kHz (cap {plan.r_cap:.0f})"
            plans.append(plan)
        return plans


def target_parts(plan, wiring, follow_channel=None, follow_value=None, follow_gain=None):
    """The m/z target of ``plan`` for the engine: (gains, offsets, follow, follow_origin, amplitude_channel).

    The coordinate is the m/z. ``wiring``: quadrupole name → dict(rails=(Vpos channel, Vneg channel),
    rods=(pair 1 channel, pair 2 channel)). Each filter's rails follow A = a × m/z and its rods x ± k a × m/z
    (U = k A, δ = 0: exactly linear in m/z above the DC floor); with ``follow_channel`` (Q1_DC) the rods also
    follow it, ``follow_gain`` volts per volt (default: the rules' stop slope, 0.95). An RF-only quadrupole
    is not part of the target: its stage optimizes it.
    """
    gains, offsets, follow = {}, {}, {}
    first = None
    for setting in plan.settings:
        if setting.role != "filter":
            continue
        rails, rods = wiring[setting.name]["rails"], wiring[setting.name]["rods"]
        if setting.u_v <= RULES["umin_v"] + 1e-9:
            raise ValueError(f"{setting.name}: U at its floor ({setting.u_v:g} V): not linear in m/z")
        a = setting.amplitude_v / plan.mz
        for rail in rails:
            gains[rail] = a
        first = first or rails[0]
        u = setting.u_v / plan.mz
        gains[rods[0]], gains[rods[1]] = u, -u
        offsets[rods[0]] = offsets[rods[1]] = setting.x_v
        if follow_channel:
            for rod in rods:
                follow[rod] = {follow_channel: RULES["stop_slope"] if follow_gain is None else follow_gain}
    if first is None:
        raise ValueError("No filter in the plan")
    origin = {follow_channel: float(follow_value)} if follow_channel else {}
    return gains, offsets, follow, origin, first


def measure_band(coordinates, currents):
    """The band of a spectrum swept across it: dict(center, fwhm, r, height, noise, points, spikes).

    Background: median of the outer third on each side; noise: their median absolute deviation (×1.4826).
    An isolated point, whose two neighbours are both under half of it, is a spike, not a band: it is left out
    and the maximum taken again (2026-11-24: on the raw Q2 files, single-point spikes gave R up to 27 × the
    model; this rule changed 2 of the other 259 bands by more than 5 %). Outer half-maximum edges, linearly
    interpolated (the review's outer FWHM). R = centre / FWHM; ``points``: samples inside the FWHM.
    NaN where not measurable.
    """
    import numpy as np
    x = np.asarray(coordinates, dtype=float)
    y = np.asarray(currents, dtype=float)
    keep = np.isfinite(x) & np.isfinite(y)
    x, y = x[keep], y[keep]
    nan = dict(center=math.nan, fwhm=math.nan, r=math.nan, height=math.nan, noise=math.nan)
    if len(x) < 7:
        return nan
    order = np.argsort(x)
    x, y = x[order], y[order]
    third = max(2, len(x) // 6)
    flanks = np.concatenate([y[:third], y[-third:]])
    background = float(np.median(flanks))
    noise = float(1.4826 * np.median(np.abs(flanks - background)))
    y = y - background
    spikes = 0
    while True:
        peak = int(np.argmax(y))
        neighbours = [y[i] for i in (peak - 1, peak + 1) if 0 <= i < len(y)]
        if y[peak] > 0 and neighbours and max(neighbours) < y[peak] / 2:
            y = np.delete(y, peak)
            x = np.delete(x, peak)
            spikes += 1
            continue
        break
    height = float(y[peak])
    result = dict(nan, height=height, noise=noise, points=0, spikes=spikes)
    if not height > 0:
        return result
    half = height / 2
    above = np.where(y >= half)[0]
    lo_i, hi_i = int(above[0]), int(above[-1])
    if lo_i == 0 or hi_i == len(x) - 1:
        return result  # The band runs off the sweep: no edge.

    def cross(i, j):
        return x[i] + (half - y[i]) * (x[j] - x[i]) / (y[j] - y[i])
    lo, hi = cross(lo_i - 1, lo_i), cross(hi_i + 1, hi_i)
    center, fwhm = (lo + hi) / 2, hi - lo
    return dict(center=float(center), fwhm=float(fwhm), r=float(center / fwhm) if fwhm > 0 else math.nan,
                height=height, noise=noise, points=int(((x >= lo) & (x <= hi)).sum()), spikes=spikes)


def climb(rules, start_k, plan_at, measure_at, report=None):
    """The finest-filter ladder: from ``start_k``, k climbs in the rules' steps; ``measure_at(plan)`` sets the filter
    and returns its measured band. Stops when R gains less than ``r_gain_stop`` per step, when the peak is no longer
    measurable, or at the closing. Returns (best (plan, band) or None, steps [(plan, band)], why)."""
    k, previous, best, steps, why = start_k, math.nan, None, [], ""
    while True:
        plan = plan_at(k)
        band = measure_at(plan)
        steps.append((plan, band))
        if report is not None:
            report(plan, band)
        go, why = rules.ladder_continues(previous, band["r"], band["height"], band["noise"])
        measurable = math.isfinite(band["r"]) and band["height"] >= rules.c["noise_factor"] * band["noise"]
        if measurable and (best is None or band["r"] > best[1]["r"]):
            best = (plan, band)
        if not go:
            return best, steps, why
        previous, k = band["r"], rules.next_k(k)
        if k is None:
            return best, steps, "closing reached"


def choose_frequency(found, r_reference, tolerance=(0.8, 1.25)):
    """The highest signal at equal R (user, 2026-11-24): among {frequency: (plan, band)} those whose measured R lies
    within ``tolerance`` × ``r_reference``, the highest peak; when none does, the highest peak of all."""
    low, high = tolerance
    equal = {f: pb for f, pb in found.items() if math.isfinite(pb[1]["r"]) and low * r_reference <= pb[1]["r"] <= high * r_reference}
    return max((equal or found).items(), key=lambda item: item[1][1]["height"])


def measured(rules, plan, sweep):
    """The band at ``plan``, measured until it can be trusted. ``sweep(center, half_span)`` sets nothing but sweeps
    the filter across [center − half_span, center + half_span] and returns ``measure_band`` of it.

    First across ± 3 predicted half-widths. Fewer than ``min_points`` samples inside the measured FWHM: swept again,
    finer, across ± 1.5 FWHM. A measured R above ``confirm_ratio`` × the model: measured again; both count if they
    agree within ``confirm_tolerance`` (the second is kept, ``confirmed``), else the lower R is kept. The band gets
    ``r_model`` and ``checks`` (what was done), for the log.
    """
    half_width = plan.mz / (2 * plan.r_predicted)
    band, checks = sweep(plan.mz, 3 * half_width), []
    if math.isfinite(band["r"]) and band["points"] < rules.c["min_points"]:
        band = sweep(band["center"], 1.5 * band["fwhm"])
        checks.append("finer")
    if math.isfinite(band["r"]) and band["r"] > rules.c["confirm_ratio"] * plan.r_predicted:
        again = sweep(band["center"], 2.0 * band["fwhm"])
        agree = math.isfinite(again["r"]) and abs(again["r"] / band["r"] - 1) <= rules.c["confirm_tolerance"]
        checks.append("confirmed" if agree else "not confirmed")
        band = again if agree else min((band, again), key=lambda b: b["r"] if math.isfinite(b["r"]) else -1)
    return dict(band, r_model=plan.r_predicted, checks=checks)


def u_zero_parts(rods, offset):
    """(gains, offsets, amplitude channel) of a target whose coordinate is U, the rods being x ± U: going to 0 sets
    U = 0 between the rod pairs and keeps their DC offset to ground x."""
    return {rods[0]: 1.0, rods[1]: -1.0}, {rods[0]: float(offset), rods[1]: float(offset)}, rods[0]
