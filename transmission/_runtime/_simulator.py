"""Simulated ESIBD beamline for trying the optimizer without hardware.

Inlet, ion funnel and four quadrupoles, with an aperture between chambers and a
collector after Q4. Three species (amplitude scale ~ m/z 300, 520 and 780) share
the beam; Q2 is a narrow mass filter (its amplitude selects one species, as in
MScan) and the RF optima of the funnel and guides depend on the species. Voltages follow their setpoints at a finite slew rate;
currents follow the transmission with a short lag, source drift and noise. A
large voltage difference at the funnel exit simulates a discharge (current
spike and pressure rise) to exercise the soft interlocks. This is a test bench,
not a physical model of the instrument.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import math
import time

import numpy as np

from ._engine import Instrument, InstrumentError, Stopped

# name: (minimum, maximum, start)
KNOBS = {
    "Inlet": (0.0, 400.0, 120.0),
    "Funnel_RF": (0.0, 1000.0, 150.0),
    "Funnel_DC": (0.0, 100.0, 15.0),
    "Funnel_exit": (0.0, 200.0, 20.0),
    "Q1_offset": (0.0, 200.0, 16.0),
    "Q1_RF": (0.0, 1000.0, 180.0),
    "Q2_offset": (0.0, 200.0, 13.0),
    "Q2_RF": (0.0, 1000.0, 285.0),
    "Q3_offset": (0.0, 200.0, 11.0),
    "Q3_RF": (0.0, 1000.0, 170.0),
    "Q4_offset": (0.0, 200.0, 8.0),
    "Q4_RF": (0.0, 1000.0, 220.0),
}
CURRENTS = ("A1", "A2", "A3", "A4", "Collector")
PRESSURES = {"P_inlet": 4.0, "P_funnel": 4.0, "P_Q1": 5e-2, "P_Q2": 1e-4, "P_Q3": 5e-4, "P_Q4": 1e-6}
HIT_FRACTION = 0.8  # Blocked ions landing on the aperture; the rest are lost on rods and walls.
DISCHARGE_V = 60.0  # |Funnel_exit - Q1_offset| above this discharges at funnel pressure.


def _g(x, mu, sigma):
    return math.exp(-0.5 * ((x - mu) / sigma) ** 2)


SPECIES = ((300.0, 1.0), (520.0, 0.4), (780.0, 0.2))  # (amplitude scale ~ m/z, abundance)


def transport(volts, source=2e-9, species=SPECIES):
    """Noise-free currents (A) for the actual voltages, summed over the species.

    In each quadrupole, ions outside its RF transmission (a mass filter rejects
    every other species) are lost on the rods and never measured. At each exit
    aperture, the injection energy into the next element decides which ions pass;
    80 % of the others land on the aperture.
    """
    currents = dict.fromkeys(CURRENTS, 0.0)
    for mass, abundance in species:
        flux = (source * abundance * _g(volts["Inlet"], 160.0, 70.0)
                * 0.85 * _g(volts["Funnel_RF"], 0.6 * mass, 0.3 * mass) * _g(volts["Funnel_DC"], 22.0, 14.0))
        rods = (1.0,
                0.9 * _g(volts["Q1_RF"], 0.75 * mass, 0.4 * mass),
                0.85 * _g(volts["Q2_RF"], mass, 0.04 * mass + 6.0),
                0.9 * _g(volts["Q3_RF"], 0.7 * mass, 0.4 * mass))
        apertures = (0.9 * _g(volts["Funnel_exit"] - volts["Q1_offset"], 6.0, 4.0),
                     _g(volts["Q1_offset"] - volts["Q2_offset"], 4.0, 3.0),
                     _g(volts["Q2_offset"] - volts["Q3_offset"], 3.0, 2.5),
                     _g(volts["Q3_offset"] - volts["Q4_offset"], 4.0, 3.0))
        for name, transmitted, passing in zip(CURRENTS[:4], rods, apertures):
            flux *= transmitted
            currents[name] += flux * (1.0 - passing) * HIT_FRACTION
            flux *= passing
        currents["Collector"] += flux * 0.9 * _g(volts["Q4_RF"], 0.87 * mass, 0.45 * mass) * _g(volts["Q4_offset"], 5.0, 4.0)
    return currents


def discharging(volts):
    return abs(volts["Funnel_exit"] - volts["Q1_offset"]) > DISCHARGE_V


class SimulatedBeamline(Instrument):
    """Instrument backed by the simulation; time advances only inside wait().

    ``pace`` real seconds are slept per simulated second, so a run can be
    watched in Explorer (0 runs as fast as possible).
    """

    def __init__(self, seed=0, *, start=None, poll_s=0.1, slew_v_per_s=200.0, tau_s=0.3, noise=0.01,
                 floor=2e-14, source=2e-9, drift_per_sqrt_hour=0.05, pace=0.0, cancel=None, t0=1.7e9):
        self.rng = np.random.default_rng(seed)
        self.poll_s, self.slew, self.tau, self.noise, self.floor = poll_s, slew_v_per_s, tau_s, noise, floor
        self.source, self.drift, self.pace, self.cancel = source, drift_per_sqrt_hour, pace, cancel
        self.t = float(t0)
        self.setpoint = {name: float(spec[2]) for name, spec in KNOBS.items()}
        self.setpoint.update(start or {})
        self.actual = dict(self.setpoint)
        self.current = transport(self.actual, source)
        self.log_source = 0.0
        self.samples = {name: ([], []) for name in (*CURRENTS, *PRESSURES)}
        self.devices_on = True
        self.fault = None  # Test hook: "discharge" forces a discharge from now on.
        self.commands = []
        self._advance(self.poll_s)

    # ---- Instrument
    def now(self):
        return self.t

    def wait(self, seconds, cancellable=True):
        if cancellable and self.cancel is not None and self.cancel.is_set():
            raise Stopped("Stopped by the operator")
        end = self.t + max(0.0, seconds)
        while end - self.t > 1e-9:
            dt = min(self.poll_s, end - self.t)
            self._advance(dt)
            if self.pace:
                time.sleep(dt * self.pace)

    def setpoints(self, channels):
        return {c: self.setpoint[c] for c in channels}

    def limits(self, channel):
        if channel not in KNOBS:
            raise InstrumentError(f"Unknown simulated channel {channel}")
        return KNOBS[channel][:2]

    def command(self, values):
        for channel, value in values.items():
            lo, hi = self.limits(channel)
            if not (math.isfinite(value) and lo <= value <= hi):
                raise InstrumentError(f"{channel}: {value!r} outside [{lo:g}, {hi:g}]")
        self.commands.append((self.t, dict(values)))
        self.setpoint.update(values)

    def readbacks(self, channels):
        return {c: self.actual[c] for c in channels}

    def latest(self, channels):
        return {c: (self.samples[c][1][-1] if self.samples[c][1] else math.nan) for c in channels}

    def window(self, channels, start, end):
        result = {}
        for c in channels:
            times, values = self.samples[c]
            if not times or times[-1] < end:
                return None
            t = np.asarray(times)
            lo, hi = np.searchsorted(t, (start, end), side="right")
            chunk = np.asarray(values[lo:hi])
            n = len(chunk)
            mean = float(np.mean(chunk)) if n else math.nan
            sem = float(np.std(chunk, ddof=1) / math.sqrt(n)) if n > 1 else math.nan
            result[c] = (mean, sem, n)
        return result

    def check(self):
        if not self.devices_on:
            raise InstrumentError("Simulated device switched off")

    # ---- simulation
    def _record(self, channel, value):
        times, values = self.samples[channel]
        times.append(self.t)
        values.append(value)

    def _advance(self, dt):
        step = self.slew * dt
        for name, target in self.setpoint.items():
            actual = self.actual[name]
            self.actual[name] = target if abs(target - actual) <= step else actual + math.copysign(step, target - actual)
        self.log_source += self.drift * math.sqrt(dt / 3600.0) * self.rng.normal()
        target = transport(self.actual, self.source * math.exp(self.log_source))
        alpha = 1.0 - math.exp(-dt / self.tau)
        for name in CURRENTS:
            self.current[name] += alpha * (target[name] - self.current[name])
        self.t += dt
        spark = discharging(self.actual) or self.fault == "discharge"
        for name in CURRENTS:
            value = self.current[name] + (5e-6 if spark and name == "A1" else 0.0)
            value += self.rng.normal(0.0, math.hypot(self.noise * abs(self.current[name]), self.floor))
            self._record(name, value)
        for name, base in PRESSURES.items():
            factor = 3.0 if spark and name == "P_funnel" else 1.0
            self._record(name, base * factor * (1.0 + 0.005 * self.rng.normal()))


CONFIG = """\
# Simulated beamline (Transmission plugin, simulation mode).
polarity = 1
strategy = "bayesian"
settle_s = 0.5
average_s = 1.5
readback_tolerance = 0.5
max_current = 1e-6
seed = 1

[pressure]
P_funnel = [2.0, 6.0]
P_Q4 = [0.0, 1e-5]

[[stage]]
name = "Inlet and funnel"
aperture = "A1"
downstream = ["A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Inlet"
channels = { Inlet = 1.0 }
window = [-100.0, 100.0]
max_step = 10.0

[[stage.knob]]
name = "Funnel RF"
channels = { Funnel_RF = 1.0 }
window = [-120.0, 120.0]
max_step = 10.0

[[stage.knob]]
name = "Funnel DC"
channels = { Funnel_DC = 1.0 }
window = [-15.0, 30.0]
max_step = 3.0

[[stage.knob]]
name = "Funnel exit"
channels = { Funnel_exit = 1.0 }
window = [-15.0, 15.0]
max_step = 1.5

[[stage]]
name = "Q1"
aperture = "A2"
downstream = ["A3", "A4", "Collector"]
normalize_by = ["A1", "A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Q1 RF"
channels = { Q1_RF = 1.0 }
window = [-120.0, 120.0]
max_step = 10.0

[[stage.knob]]
name = "Q1 offset"
channels = { Q1_offset = 1.0 }
window = [-10.0, 10.0]
max_step = 1.0

[[stage]]
name = "Q2 mass window"
aperture = "A3"
downstream = ["A4", "Collector"]
normalize_by = ["A1", "A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Q2 RF"
channels = { Q2_RF = 1.0 }
window = [-40.0, 40.0]
max_step = 4.0

[[stage.knob]]
name = "Q2 offset"
channels = { Q2_offset = 1.0 }
window = [-8.0, 8.0]
max_step = 1.0

[[stage]]
name = "Q3"
aperture = "A4"
downstream = ["Collector"]
normalize_by = ["A1", "A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Q3 RF"
channels = { Q3_RF = 1.0 }
window = [-120.0, 120.0]
max_step = 10.0

[[stage.knob]]
name = "Q3 offset"
channels = { Q3_offset = 1.0 }
window = [-8.0, 8.0]
max_step = 1.0

[[stage]]
name = "Q4 to collector"
downstream = ["Collector"]
normalize_by = ["A1", "A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Q4 RF"
channels = { Q4_RF = 1.0 }
window = [-150.0, 150.0]
max_step = 10.0

[[stage.knob]]
name = "Q4 offset"
channels = { Q4_offset = 1.0 }
window = [-8.0, 8.0]
max_step = 1.0

# Coupled injection energies: refine all offsets together at the end.
[[stage]]
name = "Offsets (global)"
downstream = ["Collector"]
normalize_by = ["A1", "A2", "A3", "A4", "Collector"]

[[stage.knob]]
name = "Q1 offset"
channels = { Q1_offset = 1.0 }
window = [-4.0, 4.0]
max_step = 0.5

[[stage.knob]]
name = "Q2 offset"
channels = { Q2_offset = 1.0 }
window = [-4.0, 4.0]
max_step = 0.5

[[stage.knob]]
name = "Q3 offset"
channels = { Q3_offset = 1.0 }
window = [-4.0, 4.0]
max_step = 0.5

[[stage.knob]]
name = "Q4 offset"
channels = { Q4_offset = 1.0 }
window = [-4.0, 4.0]
max_step = 0.5
"""


# The same beamline for the simple GUI: element knobs, aperture collectors and gauges.
BEAMLINE = {
    "polarity": 1,
    "knobs": {
        "inlet": {"Inlet": 1.0}, "funnel_rf": {"Funnel_RF": 1.0}, "funnel_dc": {"Funnel_DC": 1.0},
        "funnel_exit": {"Funnel_exit": 1.0},
        "q1_offset": {"Q1_offset": 1.0}, "q1_rf": {"Q1_RF": 1.0},
        "q2_offset": {"Q2_offset": 1.0}, "q2_rf": {"Q2_RF": 1.0},
        "q3_offset": {"Q3_offset": 1.0}, "q3_rf": {"Q3_RF": 1.0},
        "q4_offset": {"Q4_offset": 1.0}, "q4_rf": {"Q4_RF": 1.0},
    },
    "collectors": {"A1": "A1", "A2": "A2", "A3": "A3", "A4": "A4", "collector": "Collector"},
    "gauges": {"P_funnel": [2.0, 6.0], "P_Q4": [0.0, 1e-5]},
}
