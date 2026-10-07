"""Ion-transmission optimizer: configuration, surrogate model and stage runner.

No Qt, no Explorer and no device access. An ``Instrument`` supplies setpoints,
readbacks and timestamped averages; the plugin provides one backed by Explorer
channels and ``_simulator.py`` a simulated beamline. The pressure interlock that
protects people and hardware belongs in the hardware (TPG366 relays wired to the
CGC interlock inputs); the soft interlocks here only stop the optimization.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import math
import time
import tomllib
import traceback

import numpy as np

STRATEGIES = ("bayesian", "coordinate")
KINDS = ("initial", "probe", "explore", "reference", "verify")


class ConfigError(ValueError):
    """The configuration cannot be used; nothing was moved."""


class Stopped(RuntimeError):
    """The operator stopped the optimization."""


class SoftInterlock(RuntimeError):
    """A pressure or current left its configured range."""


class InstrumentError(RuntimeError):
    """A device stopped, a readback timed out or data are missing."""


# --------------------------------------------------------------------------- configuration

class Knob:
    """One optimized quantity. Each channel follows ``start + gain * delta``.

    ``delta`` is the displacement from the stage start in knob units (the units
    of a channel with gain 1, usually volts). Symmetric quadrupole rails use the
    gains +1 and -1; a common offset of several electrodes uses gain 1 on each.
    """

    def __init__(self, name, channels, window, max_step, relative=False, floor=10.0):
        self.name = name
        self.channels = dict(channels)
        self.window = (float(window[0]), float(window[1]))
        self.max_step = float(max_step)
        self.relative = bool(relative)
        self.floor = float(floor)

    def resolve(self, start):
        """(window, max_step) in knob units; a relative knob scales with its start value.

        For an RF amplitude, ``window = [-0.15, 0.15]`` means ±15 % of the start
        amplitude (at least ``floor`` volts as the reference)."""
        if not self.relative:
            return self.window, self.max_step
        channel, gain = next(iter(self.channels.items()))
        scale = max(abs(start[channel] / gain), self.floor)
        return (self.window[0] * scale, self.window[1] * scale), self.max_step * scale

    def __repr__(self):
        return f"Knob({self.name!r}, {self.channels}, window={self.window}, max_step={self.max_step:g})"


class Stage:
    """Knobs that move ions through ``aperture`` towards the ``downstream`` collectors."""

    def __init__(self, name, knobs, aperture, downstream, normalize_by=(), budget=None, peak=False):
        self.name = name
        self.knobs = tuple(knobs)
        self.aperture = aperture
        self.downstream = tuple(downstream)
        self.normalize_by = tuple(normalize_by)
        self.peak = bool(peak)
        self.budget = int(budget) if budget is not None else (8 if peak else 12) * len(self.knobs) + 16

    @property
    def channels(self):
        return {channel for knob in self.knobs for channel in knob.channels}

    @property
    def currents(self):
        names = [*([self.aperture] if self.aperture else []), *self.downstream, *self.normalize_by]
        return tuple(dict.fromkeys(names))


class Target:
    """A mass selected by a quadrupole filter, in volts of filter amplitude (no m/z calibration).

    Every filter channel follows ``gain * amplitude``: MScan's amplitude A sets
    both rails of an AMX-driven quadrupole (Vpos = +A, Vneg = -A as magnitudes,
    gains 1 and 1). The criterion of a peak stage is the height of the peak,
    measured by a short amplitude sweep around its tracked centre, so a peak
    drifting in amplitude is never taken for a change of transmission.
    """

    def __init__(self, channels, center, width, measure, points=5, track=None, max_step=None,
                 spectrum_points=21, spectrum_span=3.0, filter_name=""):
        self.channels = dict(channels)
        self.center = float(center)
        self.width = float(width)
        self.measure = tuple(measure)
        self.points = int(points)
        self.track = float(track) if track is not None else 2.0 * self.width
        self.max_step = float(max_step) if max_step is not None else self.width / 2.0
        self.spectrum_points = int(spectrum_points)
        self.spectrum_span = float(spectrum_span)
        self.filter_name = filter_name

    def values(self, amplitude):
        return {channel: gain * amplitude for channel, gain in self.channels.items()}


class Config:
    DEFAULTS = dict(polarity=1, strategy="bayesian", settle_s=1.0, average_s=2.0, readback_tolerance=0.5,
                    settle_timeout_s=20.0, min_current=1e-13, lost_fraction=0.2, verify_pairs=3,
                    significance=2.0, max_current=1e-6, reference_every=8, poll_s=0.1, seed=None)

    def __init__(self, stages, pressure=None, target=None, **options):
        for key, value in self.DEFAULTS.items():
            setattr(self, key, options.pop(key, value))
        if options:
            raise ConfigError(f"Unknown setting(s): {', '.join(sorted(options))}")
        self.stages = tuple(stages)
        self.pressure = {name: (float(lo), float(hi)) for name, (lo, hi) in (pressure or {}).items()}
        self.target = target

    @property
    def knob_channels(self):
        return tuple(dict.fromkeys(c for stage in self.stages for knob in stage.knobs for c in knob.channels))

    @property
    def driven_channels(self):
        target = tuple(self.target.channels) if self.target else ()
        return tuple(dict.fromkeys([*self.knob_channels, *target]))

    @property
    def current_channels(self):
        measure = self.target.measure if self.target else ()
        return tuple(dict.fromkeys([*(c for stage in self.stages for c in stage.currents), *measure]))

    @property
    def pressure_channels(self):
        return tuple(self.pressure)

    def stage(self, name):
        for stage in self.stages:
            if stage.name == name:
                return stage
        raise ConfigError(f"No stage named {name!r}")


def _finite(value, what, *, positive=False, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{what} must be a finite number, got {value!r}")
    if positive and value <= 0:
        raise ConfigError(f"{what} must be positive, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{what} must be at least {minimum:g}, got {value!r}")
    return float(value)


def _names(value, what, *, allow_empty=False):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ConfigError(f"{what} must be a channel name or a list of channel names")
    if not value and not allow_empty:
        raise ConfigError(f"{what} needs at least one channel")
    if len(set(value)) != len(value):
        raise ConfigError(f"{what} lists a channel twice")
    return [v.strip() for v in value]


def parse_config(text):
    """Parse and validate a TOML configuration; never touches an instrument."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML syntax: {exc}") from None
    stages_data = data.pop("stage", None)
    if not isinstance(stages_data, list) or not stages_data:
        raise ConfigError("Define at least one [[stage]]")
    target_data = data.pop("target", None)
    pressure_data = data.pop("pressure", {})
    if not isinstance(pressure_data, dict):
        raise ConfigError("[pressure] must map pressure channels to [minimum, maximum]")
    pressure = {}
    for name, pair in pressure_data.items():
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConfigError(f"pressure.{name} must be [minimum, maximum] in mbar")
        lo, hi = (_finite(v, f"pressure.{name}") for v in pair)
        if not 0 <= lo < hi:
            raise ConfigError(f"pressure.{name}: need 0 <= minimum < maximum")
        pressure[name] = (lo, hi)
    options = {}
    for key, value in data.items():
        if key not in Config.DEFAULTS:
            raise ConfigError(f"Unknown setting {key!r}")
        options[key] = value
    if options.get("polarity", 1) not in (1, -1):
        raise ConfigError("polarity must be 1 or -1")
    if options.get("strategy", "bayesian") not in STRATEGIES:
        raise ConfigError(f"strategy must be one of {', '.join(STRATEGIES)}")
    for key in ("settle_s", "average_s", "readback_tolerance", "settle_timeout_s", "min_current",
                "max_current", "significance", "poll_s"):
        if key in options:
            options[key] = _finite(options[key], key, positive=key not in ("settle_s",), minimum=0)
    if "lost_fraction" in options:
        options["lost_fraction"] = _finite(options["lost_fraction"], "lost_fraction", minimum=0)
        if options["lost_fraction"] >= 1:
            raise ConfigError("lost_fraction must be below 1")
    for key, minimum in (("verify_pairs", 2), ("reference_every", 2)):
        if key in options:
            value = options[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ConfigError(f"{key} must be an integer >= {minimum}")
    if "seed" in options and options["seed"] is not None and (isinstance(options["seed"], bool) or not isinstance(options["seed"], int)):
        raise ConfigError("seed must be an integer")
    stages, stage_names = [], set()
    for index, item in enumerate(stages_data, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"stage {index} must be a table")
        name = item.get("name", f"Stage {index}")
        if not isinstance(name, str) or not name.strip() or name in stage_names:
            raise ConfigError(f"stage {index}: name must be unique text")
        stage_names.add(name)
        known = {"name", "knob", "aperture", "downstream", "normalize_by", "budget", "peak"}
        unknown = set(item) - known
        if unknown:
            raise ConfigError(f"stage {name!r}: unknown key(s) {', '.join(sorted(unknown))}")
        aperture = item.get("aperture")
        if aperture is not None and (not isinstance(aperture, str) or not aperture.strip()):
            raise ConfigError(f"stage {name!r}: aperture must be a channel name")
        downstream = _names(item.get("downstream"), f"stage {name!r} downstream")
        normalize_by = _names(item.get("normalize_by", []), f"stage {name!r} normalize_by", allow_empty=True)
        if aperture in downstream:
            raise ConfigError(f"stage {name!r}: the aperture cannot also be downstream")
        knobs_data = item.get("knob")
        if not isinstance(knobs_data, list) or not knobs_data:
            raise ConfigError(f"stage {name!r}: define at least one [[stage.knob]]")
        knobs, knob_names = [], set()
        for j, entry in enumerate(knobs_data, 1):
            if not isinstance(entry, dict):
                raise ConfigError(f"stage {name!r} knob {j} must be a table")
            unknown = set(entry) - {"name", "channels", "window", "max_step", "relative", "floor"}
            if unknown:
                raise ConfigError(f"stage {name!r} knob {j}: unknown key(s) {', '.join(sorted(unknown))}")
            channels = entry.get("channels")
            if isinstance(channels, str):
                channels = {channels: 1.0}
            if not isinstance(channels, dict) or not channels:
                raise ConfigError(f"stage {name!r} knob {j}: channels must map channel names to gains")
            gains = {}
            for channel, gain in channels.items():
                gain = _finite(gain, f"gain of {channel}")
                if gain == 0:
                    raise ConfigError(f"stage {name!r} knob {j}: gain of {channel} must not be zero")
                # A later stage may drive channels again, e.g. a final global refinement.
                if any(channel in k.channels for k in knobs):
                    raise ConfigError(f"stage {name!r}: channel {channel} is driven by two knobs")
                gains[channel] = gain
            knob_name = entry.get("name", next(iter(gains)))
            if not isinstance(knob_name, str) or knob_name in knob_names:
                raise ConfigError(f"stage {name!r}: knob names must be unique text")
            knob_names.add(knob_name)
            window = entry.get("window")
            if not isinstance(window, list) or len(window) != 2:
                raise ConfigError(f"knob {knob_name!r}: window must be [below, above] relative to the start value")
            lo, hi = (_finite(v, f"knob {knob_name!r} window") for v in window)
            if not lo <= 0 <= hi or lo == hi:
                raise ConfigError(f"knob {knob_name!r}: window must contain 0 (the start value), e.g. [-20, 20]")
            max_step = _finite(entry.get("max_step"), f"knob {knob_name!r} max_step", positive=True)
            relative = entry.get("relative", False)
            if not isinstance(relative, bool):
                raise ConfigError(f"knob {knob_name!r}: relative must be true or false")
            floor = _finite(entry.get("floor", 10.0), f"knob {knob_name!r} floor", positive=True)
            if relative and (hi - lo > 4 or max_step > 1):
                raise ConfigError(f"knob {knob_name!r}: a relative window and step are fractions of the start value, e.g. [-0.15, 0.15] and 0.02")
            knobs.append(Knob(knob_name, gains, (lo, hi), max_step, relative, floor))
        peak = item.get("peak", False)
        if not isinstance(peak, bool):
            raise ConfigError(f"stage {name!r}: peak must be true or false")
        budget = item.get("budget")
        # Two initial points, two probes per knob, four searches, then the A/B verification.
        minimum_budget = 2 * len(knobs) + 6 + 2 * options.get("verify_pairs", Config.DEFAULTS["verify_pairs"])
        if budget is not None and (isinstance(budget, bool) or not isinstance(budget, int) or budget < minimum_budget):
            raise ConfigError(f"stage {name!r}: budget must be an integer >= {minimum_budget} (initial, probes, search)")
        stage = Stage(name, knobs, aperture, downstream, normalize_by, budget, peak)
        overlap = stage.channels & set(stage.currents)
        if overlap:
            raise ConfigError(f"stage {name!r}: {', '.join(sorted(overlap))} cannot be both driven and measured")
        stages.append(stage)
    target = _parse_target(target_data) if target_data is not None else None
    if any(stage.peak for stage in stages) and target is None:
        raise ConfigError("Stages with peak = true need a [target] (the selected mass)")
    if target is not None:
        for stage in stages:
            overlap = stage.channels & set(target.channels)
            if overlap:
                raise ConfigError(f"stage {stage.name!r} drives the mass filter {', '.join(sorted(overlap))}; "
                                  "the target tracks it by itself")
    config = Config(stages, pressure, target, **options)
    measured = set(config.current_channels) | set(config.pressure_channels)
    driven = set(config.knob_channels) & measured
    if driven:
        raise ConfigError(f"{', '.join(sorted(driven))} cannot be both driven and measured")
    if set(config.current_channels) & set(config.pressure_channels):
        raise ConfigError("A channel cannot be both a current and a pressure")
    return config


def _parse_target(data):
    if not isinstance(data, dict):
        raise ConfigError("[target] must be a table")
    unknown = set(data) - {"mode", "filter", "channels", "center", "width", "measure", "points", "track", "max_step",
                           "spectrum_points", "spectrum_span"}
    if unknown:
        raise ConfigError(f"[target]: unknown key(s) {', '.join(sorted(unknown))}")
    if data.get("mode", "mass") != "mass":
        raise ConfigError('[target] mode must be "mass"')
    channels = data.get("channels")
    if not isinstance(channels, dict) or not channels:
        raise ConfigError("[target] channels must map the filter amplitude channels to gains (1.0 for MScan rails)")
    gains = {}
    for channel, gain in channels.items():
        gain = _finite(gain, f"[target] gain of {channel}")
        if gain == 0:
            raise ConfigError(f"[target] gain of {channel} must not be zero")
        gains[channel] = gain
    center = _finite(data.get("center"), "[target] center", minimum=0)
    width = _finite(data.get("width"), "[target] width", positive=True)
    measure = _names(data.get("measure"), "[target] measure")
    points = data.get("points", 5)
    if isinstance(points, bool) or not isinstance(points, int) or points < 3 or points > 15 or points % 2 == 0:
        raise ConfigError("[target] points must be an odd integer from 3 to 15")
    track = _finite(data.get("track", 2 * width), "[target] track", positive=True)
    max_step = _finite(data.get("max_step", width / 2), "[target] max_step", positive=True)
    spectrum_points = data.get("spectrum_points", 21)
    if isinstance(spectrum_points, bool) or not isinstance(spectrum_points, int) or (spectrum_points and spectrum_points < 5):
        raise ConfigError("[target] spectrum_points must be 0 (no spectrum) or an integer >= 5")
    span = _finite(data.get("spectrum_span", 3.0), "[target] spectrum_span", positive=True)
    name = data.get("filter", "")
    if not isinstance(name, str):
        raise ConfigError("[target] filter must be text")
    if set(gains) & set(measure):
        raise ConfigError("[target]: a filter channel cannot also be measured")
    return Target(gains, center, width, measure, points, track, max_step, spectrum_points, span, name)


def template(knob_channels=(), current_channels=(), pressure_channels=()):
    """A commented starting configuration, using real channel names when given."""
    knobs = list(knob_channels) or ["Funnel_exit"]
    currents = list(current_channels) or ["A1", "A2", "A3", "A4", "Collector"]
    pressures = list(pressure_channels)
    lines = [
        "# Transmission optimizer configuration (TOML). Lines starting with # are comments.",
        "# Driven channels follow: start + gain * delta, delta limited to the knob window.",
        "polarity = 1              # 1: collected ions read as positive currents; -1 otherwise",
        'strategy = "bayesian"     # "bayesian" (trust-region Gaussian process) or "coordinate"',
        "settle_s = 1.0            # wait after the readbacks reach their setpoints (s)",
        "average_s = 2.0           # averaging window for currents and pressures (s)",
        "readback_tolerance = 0.5  # |readback - setpoint| accepted as settled (V)",
        "settle_timeout_s = 20.0   # a readback that never settles stops the run",
        "min_current = 1e-13       # A: below this, no beam reaches the aperture",
        "lost_fraction = 0.2       # beam lost below this fraction of the flux at the stage start",
        "max_current = 1e-6        # A: a larger |current| is treated as a discharge (soft interlock)",
        "verify_pairs = 3          # A/B comparisons before adopting the best settings",
        "significance = 2.0        # adopt only if the gain exceeds this many standard errors",
        "",
        "# Soft interlock only. The protective interlock belongs in the hardware.",
        "[pressure]",
    ]
    if pressures:
        lines += [f"# {name} = [minimum, maximum]   # mbar" for name in pressures]
    else:
        lines += ["# P_funnel = [1.0, 6.0]   # mbar"]
    lines += [
        "",
        "[[stage]]",
        'name = "Funnel to Q1"',
        f'aperture = "{currents[0]}"          # the aperture closing this stage',
        f"downstream = {currents[1:] or ['Collector']}   # their sum is maximized",
        "normalize_by = []          # optional: channels summing to the incoming flux",
        "# budget = 40              # evaluations, including initial and verification points",
        "",
        "[[stage.knob]]",
        f'name = "{knobs[0]}"',
        f'channels = {{ "{knobs[0]}" = 1.0 }}',
        "window = [-20.0, 20.0]      # relative to the start value",
        "max_step = 2.0              # largest change per move",
    ]
    if len(knobs) > 1:
        lines += ["", "# Available driven channels: " + ", ".join(knobs[1:])]
    if len(currents) > 1:
        lines += ["# Available current channels: " + ", ".join(currents)]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- instrument

class Instrument:
    """What the optimizer needs from the beamline. All values are plain floats."""

    def now(self):
        """Wall-clock seconds, on the same scale as the data timestamps."""
        raise NotImplementedError

    def wait(self, seconds, cancellable=True):
        """Let time pass; raise Stopped when the operator stops the run (if cancellable)."""
        raise NotImplementedError

    def setpoints(self, channels):
        """Current requested values of driven channels."""
        raise NotImplementedError

    def limits(self, channel):
        """(minimum, maximum) allowed by the device plugin."""
        raise NotImplementedError

    def command(self, values):
        """Request new setpoints (already step-limited and within limits)."""
        raise NotImplementedError

    def readbacks(self, channels):
        """Measured values of driven channels; NaN when a channel has no readback."""
        raise NotImplementedError

    def latest(self, channels):
        """Most recent measured value of currents and pressures; NaN when unknown."""
        raise NotImplementedError

    def window(self, channels, start, end):
        """{channel: (mean, standard error, samples)} over (start, end]; None until closed."""
        raise NotImplementedError

    def check(self):
        """Raise InstrumentError when a device stopped, a channel changed or data stopped."""

    def diagnostics(self):
        """Optional details for the log after each averaging window (sample timing, latencies)."""
        return {}

    def measuring(self, active):
        """An averaging window opens (True) or closes (False).

        The Explorer instrument waits for the GUI to be idle before a window opens and
        keeps the plugin's redraws out of it: Explorer timestamps device samples on its
        GUI thread, so a redraw during a window would cost samples.
        """


# --------------------------------------------------------------------------- Gaussian process

def _matern52(a, b, lengthscales):
    diff = (a[:, None, :] - b[None, :, :]) / lengthscales
    r = np.sqrt(np.maximum(np.sum(diff * diff, axis=-1), 0.0))
    s5 = math.sqrt(5.0) * r
    return (1.0 + s5 + s5 * s5 / 3.0) * np.exp(-s5)


class GaussianProcess:
    """Matérn 5/2 ARD regression with known per-point noise plus a learned floor."""

    def __init__(self, lengthscale_bounds):
        self.bounds = np.asarray(lengthscale_bounds, dtype=float)
        self.fitted = False

    def _nll(self, theta, x, y, noise):
        from scipy.linalg import cho_factor, cho_solve
        dims = x.shape[1]
        lengthscales, signal, floor = np.exp(theta[:dims]), math.exp(theta[dims]), math.exp(theta[dims + 1])
        k = signal * _matern52(x, x, lengthscales) + np.diag(noise + floor + 1e-10)
        try:
            factor = cho_factor(k, lower=True, check_finite=False)
        except np.linalg.LinAlgError:
            return 1e25
        alpha = cho_solve(factor, y, check_finite=False)
        return float(0.5 * y @ alpha + np.sum(np.log(np.diag(factor[0]))) + 0.5 * len(y) * math.log(2 * math.pi))

    def fit(self, x, y, noise, rng, start=None, restarts=2):
        from scipy.optimize import minimize
        x, y, noise = (np.asarray(v, dtype=float) for v in (x, y, noise))
        self.y_mean = float(np.mean(y))
        self.y_scale = float(np.std(y)) or 1.0
        yn = (y - self.y_mean) / self.y_scale
        nn = noise / self.y_scale ** 2
        dims = x.shape[1]
        bounds = [(math.log(lo), math.log(hi)) for lo, hi in self.bounds] + [(math.log(.05), math.log(20.)), (math.log(1e-6), math.log(1.))]
        default = np.array([math.log(math.sqrt(lo * hi)) for lo, hi in self.bounds] + [0.0, math.log(1e-2)])
        starts = [np.clip(start, [b[0] for b in bounds], [b[1] for b in bounds]) if start is not None else default]
        for _ in range(restarts):
            starts.append(np.array([rng.uniform(lo, hi) for lo, hi in bounds]))
        best = None
        for start in starts:
            result = minimize(self._nll, start, args=(x, yn, nn), method="L-BFGS-B", bounds=bounds)
            if best is None or result.fun < best.fun:
                best = result
        theta = best.x
        self.theta = theta
        self.lengthscales = np.exp(theta[:dims])
        self.signal = math.exp(theta[dims])
        self.floor = math.exp(theta[dims + 1])
        from scipy.linalg import cho_factor, cho_solve
        k = self.signal * _matern52(x, x, self.lengthscales) + np.diag(nn + self.floor + 1e-10)
        self._factor = cho_factor(k, lower=True, check_finite=False)
        self._alpha = cho_solve(self._factor, yn, check_finite=False)
        self._cho_solve = cho_solve
        self.x = x
        self.fitted = True
        return self

    def predict(self, xs):
        xs = np.atleast_2d(np.asarray(xs, dtype=float))
        ks = self.signal * _matern52(xs, self.x, self.lengthscales)
        mean = ks @ self._alpha
        v = self._cho_solve(self._factor, ks.T, check_finite=False)
        var = np.maximum(self.signal - np.sum(ks * v.T, axis=1), 1e-12)
        return mean * self.y_scale + self.y_mean, np.sqrt(var) * self.y_scale


# --------------------------------------------------------------------------- strategies

class Strategy:
    """Proposes knob displacements; works in the unit cube of the knob windows."""

    def __init__(self, lower, upper, max_step, rng):
        self.lower, self.upper = np.asarray(lower, float), np.asarray(upper, float)
        self.span = self.upper - self.lower
        self.step = np.asarray(max_step, float) / self.span
        self.rng = rng
        self.converged = False

    def to_unit(self, delta):
        return (np.asarray(delta, float) - self.lower) / self.span

    def from_unit(self, unit):
        return self.lower + np.clip(unit, 0.0, 1.0) * self.span

    def ask(self, history, now):
        raise NotImplementedError

    def tell(self, record, history, now):
        pass

    def diagnostics(self):
        """Internal state for the log."""
        return dict(converged=self.converged)

    def incumbent(self, history, now):
        """Best displacement so far, robust to single noisy readings."""
        valid = [r for r in history if r["valid"]]
        if not valid:
            return np.zeros_like(self.lower)
        groups = {}
        for r in valid:
            groups.setdefault(tuple(np.round(r["delta"], 9)), []).append(r["objective"])
        key = max(groups, key=lambda k: np.mean(groups[k]))
        return np.asarray(key, float)


class TrustRegionBayesian(Strategy):
    """Gaussian-process surrogate with an upper-confidence-bound search inside a trust region.

    The region is centred on the best predicted point and measured in units of
    each knob's max_step: successes widen it, repeated failures shrink it, and
    the stage converges when it becomes smaller than half a step.
    """

    TIME_SCALE_S = 3600.0

    def __init__(self, lower, upper, max_step, rng, beta=2.0, candidates=512):
        super().__init__(lower, upper, max_step, rng)
        self.beta, self.candidates = beta, candidates
        self.radius, self.radius_min, self.radius_max = 2.0, 0.5, 8.0
        self.successes = self.failures = 0
        self.success_tolerance, self.failure_tolerance = 3, max(3, len(self.lower))
        self.model = None
        self.t0 = None
        self.fits = 0
        self.last_ask = {}

    def diagnostics(self):
        result = dict(converged=self.converged, radius=self.radius, successes=self.successes,
                      failures=self.failures, fits=self.fits, **self.last_ask)
        if self.model is not None:
            # Length scales: one per knob (unit window) then time (hours); signal and noise floor normalized.
            result.update(lengthscales=self.model.lengthscales, signal=self.model.signal, noise_floor=self.model.floor,
                          y_mean=self.model.y_mean, y_scale=self.model.y_scale)
        return result

    def _features(self, delta, times):
        unit = self.to_unit(np.atleast_2d(delta))
        t = (np.atleast_1d(times) - self.t0)[:, None] / self.TIME_SCALE_S
        return np.hstack([unit, t])

    def _fit(self, history):
        if self.t0 is None:
            self.t0 = history[0]["time"]
        x = self._features(np.array([r["delta"] for r in history]), np.array([r["time"] for r in history]))
        y = np.array([r["model_objective"] for r in history])
        noise = np.array([max(r["objective_sem"], 0.0) ** 2 if math.isfinite(r["objective_sem"]) else 0.0 for r in history])
        bounds = [(0.03, 3.0)] * len(self.lower) + [(0.05, 100.0)]
        # Warm start from the previous hyperparameters; explore new ones every fifth fit.
        previous = self.model.theta if self.model is not None else None
        restarts = 2 if previous is None or self.fits % 5 == 0 else 0
        self.model = GaussianProcess(bounds).fit(x, y, noise, self.rng, start=previous, restarts=restarts)
        self.fits += 1

    def incumbent(self, history, now):
        valid = [r for r in history if r["valid"]]
        if not valid or self.model is None:
            return super().incumbent(history, now)
        deltas = np.array([r["delta"] for r in valid])
        mean, _ = self.model.predict(self._features(deltas, np.full(len(deltas), now)))
        return deltas[int(np.argmax(mean))]

    def ask(self, history, now):
        from scipy.stats import qmc
        started = time.perf_counter()
        self._fit(history)
        fitted = time.perf_counter()
        center = self.to_unit(self.incumbent(history, now))
        half = np.minimum(self.radius * self.step, 0.5)
        lo, hi = np.clip(center - half, 0, 1), np.clip(center + half, 0, 1)
        sobol = qmc.Sobol(len(center), scramble=True, seed=self.rng.integers(2 ** 31)).random(self.candidates)
        box = lo + sobol * (hi - lo)
        local = np.clip(center + self.rng.normal(0, 0.35, (self.candidates // 2, len(center))) * half, lo, hi)
        candidates = np.vstack([box, local])
        mean, std = self.model.predict(self._features(self.from_unit(candidates), np.full(len(candidates), now)))
        score = mean + self.beta * std
        best = int(np.argmax(score))
        self.last_ask = dict(fit_s=fitted - started, ask_s=time.perf_counter() - started, incumbent=self.from_unit(center),
                             predicted=float(mean[best]), predicted_std=float(std[best]), ucb=float(score[best]))
        return self.from_unit(candidates[best])

    def tell(self, record, history, now):
        before = [r for r in history[:-1] if r["valid"]]
        if not before or self.model is None:
            return
        deltas = np.array([r["delta"] for r in before])
        mean, _ = self.model.predict(self._features(deltas, np.full(len(deltas), now)))
        reference = float(np.max(mean))
        margin = max(record["objective_sem"] if math.isfinite(record["objective_sem"]) else 0.0, 1e-3 * abs(reference))
        if record["valid"] and record["model_objective"] > reference + margin:
            self.successes, self.failures = self.successes + 1, 0
        else:
            self.successes, self.failures = 0, self.failures + 1
        if self.successes >= self.success_tolerance:
            self.radius, self.successes = min(2 * self.radius, self.radius_max), 0
        elif self.failures >= self.failure_tolerance:
            self.radius, self.failures = self.radius / 2, 0
            self.converged = self.radius < self.radius_min


class CoordinateSearch(Strategy):
    """Classic tuning: one knob at a time, five-point line and quadratic fit."""

    OFFSETS = (-2, -1, 1, 2)

    def __init__(self, lower, upper, max_step, rng):
        super().__init__(lower, upper, max_step, rng)
        self.center = self.to_unit(np.zeros_like(self.lower))
        self.scale = 1.0
        self.axis = 0
        self.queue = []
        self.line = []
        self.improved = False
        self.cycle_best = -math.inf

    def diagnostics(self):
        return dict(converged=self.converged, axis=self.axis, scale=self.scale, center=self.from_unit(self.center),
                    queued=len(self.queue), improved=self.improved)

    def _plan(self):
        delta = self.scale * self.step[self.axis]
        self.queue = []
        for k in self.OFFSETS:
            point = self.center.copy()
            point[self.axis] += k * delta
            if 0 <= point[self.axis] <= 1:
                self.queue.append(point)
        self.line = []

    def ask(self, history, now):
        if not self.queue:
            self._plan()
            if not self.queue:  # Axis pinned at both window edges.
                self._advance(history)
                self._plan()
        return self.from_unit(self.queue.pop(0))

    def _advance(self, history):
        self.axis += 1
        if self.axis == len(self.lower):
            self.axis = 0
            if not self.improved:
                self.scale /= 2
                self.converged = self.scale < 0.25
            self.improved = False

    def tell(self, record, history, now):
        self.line.append(record)
        if self.queue:
            return
        axis = self.axis
        center_delta = self.from_unit(self.center)
        same = [r for r in history if r["valid"] and np.allclose(np.delete(r["delta"], axis), np.delete(center_delta, axis))]
        if len(same) >= 3:
            x = np.array([r["delta"][axis] for r in same])
            y = np.array([r["objective"] for r in same])
            if np.ptp(x) > 0:
                a, b, _ = np.polyfit(x, y, 2) if len(np.unique(x)) >= 3 else (0.0, *np.polyfit(x, y, 1))
                reach = 2 * self.scale * self.step[axis] * self.span[axis]
                if a < 0:
                    best = -b / (2 * a)
                else:
                    best = x[int(np.argmax(y))]
                best = float(np.clip(best, center_delta[axis] - reach, center_delta[axis] + reach))
                best = float(np.clip(best, self.lower[axis], self.upper[axis]))
                if abs(best - center_delta[axis]) > 1e-12:
                    self.center[axis] = self.to_unit(np.where(np.arange(len(self.lower)) == axis, best, center_delta))[axis]
                    self.improved = True
        self._advance(history)

    def incumbent(self, history, now):
        return self.from_unit(self.center)


def make_strategy(name, lower, upper, max_step, rng):
    if name == "bayesian":
        return TrustRegionBayesian(lower, upper, max_step, rng)
    if name == "coordinate":
        return CoordinateSearch(lower, upper, max_step, rng)
    raise ConfigError(f"Unknown strategy {name!r}")


# --------------------------------------------------------------------------- runner

class Optimizer:
    """Optimize stages in beam order on an Instrument.

    Every move is split into steps no larger than each knob's max_step and waits
    for the readbacks. A point counts only if the flux reaching the stage
    aperture (aperture + downstream) stays above ``lost_fraction`` of the stage
    start: a low aperture current caused by a lost beam is never mistaken for
    transmission. The best point is adopted only after paired A/B measurements
    against the stage start show a significant gain.
    """

    def __init__(self, config, instrument, *, stages=None, strategy=None, on_event=None, log=None):
        self.config = config
        self.instrument = instrument
        self.stages = [config.stage(name) for name in stages] if stages else list(config.stages)
        self.strategy_name = strategy or config.strategy
        if self.strategy_name not in STRATEGIES:
            raise ConfigError(f"Unknown strategy {self.strategy_name!r}")
        self.rng = np.random.default_rng(config.seed)
        self.on_event = on_event or (lambda kind, data: None)
        self.history = []
        self.commanded = {}
        self.initial = {}
        self.max_step = {}
        self.results = []
        self.status = "ready"
        self.reason = ""
        self._restoring = False
        self._stage_start = {}
        self.peak_center = config.target.center if config.target is not None else None
        self.spectra = {}
        self.checks = {}
        # log(event, data): detailed JSON-safe trace for troubleshooting and tuning (see _log.py).
        self.log = log
        self.counts = dict(steps=0, measures=0, empty_windows=0)

    # ---- events and checks
    def _emit(self, kind, **data):
        self.on_event(kind, data)

    def _log(self, event, **data):
        if self.log is not None:
            try:
                self.log(event, data)
            except Exception:  # noqa: BLE001, S110 - logging never stops the optimizer
                pass

    def _options(self):
        return {key: getattr(self.config, key) for key in Config.DEFAULTS}

    def _check(self, *, interlocks=True):
        instrument, config = self.instrument, self.config
        instrument.check()
        if not interlocks:
            return
        channels = (*config.pressure_channels, *config.current_channels)
        latest = instrument.latest(channels) if channels else {}
        for name, (lo, hi) in config.pressure.items():
            value = latest.get(name, math.nan)
            if not math.isfinite(value):
                raise SoftInterlock(f"Pressure {name} unavailable")
            if not lo <= value <= hi:
                raise SoftInterlock(f"Pressure {name} = {value:.3g} mbar outside [{lo:g}, {hi:g}] mbar")
        for name in config.current_channels:
            value = latest.get(name, math.nan)
            if math.isfinite(value) and abs(value) > config.max_current:
                raise SoftInterlock(f"Current {name} = {value:.3g} A exceeds {config.max_current:g} A (discharge?)")

    def _wait(self, seconds):
        # Restoring the stage start must finish even after a Stop request.
        self.instrument.wait(seconds, cancellable=not self._restoring)

    def _pause(self, seconds, *, interlocks=True):
        end = self.instrument.now() + seconds
        while True:
            self._check(interlocks=interlocks)
            remaining = end - self.instrument.now()
            if remaining <= 0:
                return
            self._wait(min(self.config.poll_s, remaining))

    # ---- moves
    def _settle(self, targets, dwell, *, interlocks=True):
        begin = self.instrument.now()
        deadline = begin + self.config.settle_timeout_s
        tolerance = self.config.readback_tolerance
        polls = 0
        while True:
            self._check(interlocks=interlocks)
            readbacks = self.instrument.readbacks(list(targets))
            off = {c: (v, readbacks.get(c, math.nan)) for c, v in targets.items()
                   if math.isfinite(readbacks.get(c, math.nan)) and abs(readbacks[c] - v) > tolerance}
            if not off:
                break
            if self.instrument.now() >= deadline:
                worst = ", ".join(f"{c}: set {v:g}, read {r:g}" for c, (v, r) in off.items())
                raise InstrumentError(f"Readback did not reach the setpoint within {self.config.settle_timeout_s:g} s ({worst})")
            polls += 1
            self._wait(self.config.poll_s)
        settled = self.instrument.now() - begin
        self._pause(dwell, interlocks=interlocks)
        return dict(settle_s=settled, polls=polls, readbacks=readbacks)

    def _move(self, targets, *, interlocks=True):
        targets = {c: float(v) for c, v in targets.items()}
        start = {c: self.commanded[c] for c in targets}
        count = max([1] + [math.ceil(abs(targets[c] - start[c]) / self.max_step[c] - 1e-9) for c in targets])
        for i in range(1, count + 1):
            point = {c: start[c] + (targets[c] - start[c]) * i / count for c in targets}
            for c, v in point.items():
                lo, hi = self.instrument.limits(c)
                if not lo <= v <= hi:
                    raise InstrumentError(f"{c}: {v:g} outside the device limits [{lo:g}, {hi:g}]")
            self._check(interlocks=interlocks)
            self.instrument.command(point)
            self.commanded.update(point)
            self.counts["steps"] += 1
            settled = self._settle(point, self.config.settle_s if i == count else 0.0, interlocks=interlocks)
            self._log("step", step=i, of=count, setpoints=point, restoring=self._restoring, **settled)

    def _targets(self, stage, start, delta):
        return {c: start[c] + gain * d for knob, d in zip(stage.knobs, delta) for c, gain in knob.channels.items()}

    # ---- measurement
    MEASURE_ATTEMPTS = 3

    def _measure(self, channels):
        """Mean, standard error and samples of each channel over a fresh averaging window.

        A window is measured again (no command in between, never filled) when a channel has
        no valid sample, or when a criterion current has fewer than two: one sample gives no
        noise estimate. Three such windows in a row stop the run.
        """
        config, instrument = self.config, self.instrument
        criterion = list(dict.fromkeys(channels))
        channels = list(dict.fromkeys([*criterion, *config.pressure_channels]))
        for attempt in range(1, self.MEASURE_ATTEMPTS + 1):
            instrument.measuring(True)
            try:
                begin = instrument.now()
                end = begin + config.average_s
                self._pause(config.average_s)
                deadline = instrument.now() + config.settle_timeout_s
                while True:
                    self._check()
                    data = instrument.window(channels, begin, end)
                    if data is not None:
                        break
                    if instrument.now() >= deadline:
                        raise InstrumentError("No fresh data closed the averaging window; check acquisition and recording")
                    self._wait(config.poll_s)
            finally:
                instrument.measuring(False)
            empty = [c for c in channels if data[c][2] == 0 or not math.isfinite(data[c][0])]
            thin = [c for c in criterion if c not in empty and data[c][2] < 2]
            self.counts["measures"] += 1
            self._log("measure", begin=begin, end=end, closed_after_s=instrument.now() - end, attempt=attempt,
                      data=data, empty=empty, thin=thin, instrument=self._diagnostics())
            if not empty and not thin:
                return begin, end, data
            # A gap in the histories (acquisition or GUI stall) leaves no value: measure again, never invent one.
            self.counts["empty_windows"] += 1
            missing = ", ".join([*empty, *thin])
            self._emit("status", text=f"Too few samples for {missing} in the averaging window; measuring again")
        if empty:
            raise InstrumentError(f"No valid samples in {self.MEASURE_ATTEMPTS} averaging windows in a row for {', '.join(empty)}")
        raise InstrumentError(f"Fewer than 2 samples in {self.MEASURE_ATTEMPTS} averaging windows in a row for "
                              f"{', '.join(thin)}: lengthen average_s or shorten the device interval")

    def _diagnostics(self):
        try:
            return self.instrument.diagnostics()
        except Exception as exc:  # noqa: BLE001 - diagnostics are optional
            return dict(error=repr(exc))

    def _filtered(self, data, measure):
        pol = self.config.polarity
        value = sum(pol * data[c][0] for c in measure)
        sem = math.sqrt(sum((data[c][1] if math.isfinite(data[c][1]) else 0.0) ** 2 for c in measure))
        return value, sem

    # ---- mass filter
    def _filter_amplitude(self):
        target = self.config.target
        channel, gain = next(iter(target.channels.items()))
        return self.commanded[channel] / gain

    def _prepare_filter(self):
        """Record the filter channels (for undo and step limits) without moving them."""
        target = self.config.target
        start = self.instrument.setpoints(list(target.channels))
        for channel, value in start.items():
            if not math.isfinite(value):
                raise InstrumentError(f"{channel}: no valid setpoint")
            self.initial.setdefault(channel, value)
            self.commanded[channel] = value
            self.max_step[channel] = abs(target.channels[channel]) * target.max_step
        return start

    def _start_target(self):
        target = self.config.target
        start = self._prepare_filter()
        half = max(target.track + target.width, target.spectrum_span * target.width if target.spectrum_points else 0.0)
        reach = (max(0.0, target.center - half), target.center + half)
        for channel, gain in target.channels.items():
            lo, hi = self.instrument.limits(channel)
            if not all(lo <= gain * amplitude <= hi for amplitude in reach):
                raise ConfigError(f"{channel}: the target {target.center:g} V ± {half:g} V (peak tracking and spectra) "
                                  f"leaves the device limits [{lo:g}, {hi:g}]")
        self._stage_start = dict(start)
        self._emit("status", text=f"Mass filter to the selected peak ({target.center:g} V)")
        self._move(target.values(target.center))
        self.peak_center = target.center

    def sweep(self, amplitudes, measure=None, label="spectrum"):
        """Filtered current over filter amplitudes, then back to the amplitude before the sweep."""
        target = self.config.target
        measure = list(measure or target.measure)
        back = self._filter_amplitude()
        result = dict(amplitude=[], current=[], sem=[])
        started = self.instrument.now()
        for amplitude in amplitudes:
            self._move(target.values(float(amplitude)))
            _, _, data = self._measure(measure)
            value, sem = self._filtered(data, measure)
            result["amplitude"].append(float(amplitude))
            result["current"].append(value)
            result["sem"].append(sem)
            self._emit("sweep", label=label, amplitude=float(amplitude), current=value)
        self._move(target.values(back))
        self._log("sweep", label=label, measure=measure, filter=target.channels, duration_s=self.instrument.now() - started,
                  **result)
        return result

    def _spectrum_amplitudes(self):
        target = self.config.target
        half = target.spectrum_span * target.width
        return np.linspace(max(0.0, target.center - half), target.center + half, target.spectrum_points)

    def peak_measure(self, stage):
        """Currents giving the peak height of a stage: behind the filter *and* after the stage aperture.

        Ions of the selected mass landing on the stage's own exit aperture are not
        transmitted by that stage, so they must not raise its criterion.
        """
        measure = [c for c in self.config.target.measure if c in stage.downstream]
        return measure or list(self.config.target.measure)

    def _peak(self, stage):
        """Height and centre of the selected peak from a short sweep around its tracked centre."""
        target = self.config.target
        lo, hi = target.center - target.track, target.center + target.track
        center = self.peak_center
        measure = self.peak_measure(stage)
        amplitudes, currents, sems, center_data = [], [], [], None
        for u in np.linspace(-1.0, 1.0, target.points):
            amplitude = max(0.0, center + u * target.width)
            self._move(target.values(amplitude))
            begin, end, data = self._measure([*stage.currents, *target.measure])
            value, sem = self._filtered(data, measure)
            amplitudes.append(amplitude)
            currents.append(value)
            sems.append(sem)
            if abs(u) < 1e-12:
                center_data = (begin, end, data)
        height, height_sem, fitted = fit_peak(np.array(amplitudes), np.array(currents), np.array(sems))
        self.peak_center = float(np.clip(fitted, lo, hi))
        self._move(target.values(self.peak_center))
        return dict(amplitudes=amplitudes, currents=currents, sems=sems, height=height, height_sem=height_sem,
                    center=self.peak_center, fitted_center=fitted, measure=measure, center_data=center_data)

    # ---- scoring
    def _score(self, stage, delta, kind, begin, end, data, reference, peak=None):
        config, pol = self.config, self.config.polarity
        mean = {c: pol * data[c][0] for c in stage.currents}
        sem = {c: data[c][1] if math.isfinite(data[c][1]) else 0.0 for c in stage.currents}
        down = sum(mean[c] for c in stage.downstream)
        down_sem = math.sqrt(sum(sem[c] ** 2 for c in stage.downstream))
        incident = down + (mean[stage.aperture] if stage.aperture else 0.0)
        objective, objective_sem = down, down_sem
        if peak is not None:
            objective, objective_sem = peak["height"], peak["height_sem"]
        elif stage.normalize_by:
            norm = sum(mean[c] for c in stage.normalize_by)
            norm_sem = math.sqrt(sum(sem[c] ** 2 for c in stage.normalize_by))
            if norm > config.min_current:
                objective = down / norm
                objective_sem = math.hypot(down_sem / norm, down * norm_sem / norm ** 2)
            else:
                objective, objective_sem = math.nan, math.nan
        reason = ""
        if not math.isfinite(objective):
            reason = "no incoming flux to normalize by"
        elif incident <= config.min_current:
            reason = "no beam reaches the aperture"
        elif reference is not None and incident < config.lost_fraction * reference:
            reason = f"beam lost: flux {incident:.3g} A < {config.lost_fraction:g} x stage start"
        valid = not reason
        record = dict(stage=stage.name, kind=kind, time=end, begin=begin, delta=np.asarray(delta, float).copy(),
                      knobs={k.name: float(d) for k, d in zip(stage.knobs, delta)},
                      setpoints={c: self.commanded[c] for c in stage.channels},
                      currents={c: data[c] for c in stage.currents},
                      pressures={c: data[c] for c in self.config.pressure_channels},
                      objective=objective, objective_sem=objective_sem, incident=incident, transmitted=down,
                      valid=valid, reason=reason, model_objective=objective if valid else 0.0, peak=peak)
        return record

    def _evaluate(self, stage, start, delta, kind, reference, history):
        self._move(self._targets(stage, start, delta))
        peak = None
        if stage.peak:
            peak = self._peak(stage)
            begin, end, data = peak.pop("center_data")
        else:
            begin, end, data = self._measure(stage.currents)
        record = self._score(stage, delta, kind, begin, end, data, reference, peak)
        record["index"] = len(self.history)
        self.history.append(record)
        history.append(record)
        self._log("evaluation", **record)
        self._emit("evaluation", record=record)
        return record

    # ---- stages
    def _bounds(self, stage, start, resolved):
        lower, upper = [], []
        for knob, (window, step) in zip(stage.knobs, resolved):
            lo, hi = window
            for channel, gain in knob.channels.items():
                cmin, cmax = self.instrument.limits(channel)
                if not cmin <= start[channel] <= cmax:
                    raise ConfigError(f"{channel}: start value {start[channel]:g} outside its limits [{cmin:g}, {cmax:g}]")
                a, b = sorted(((cmin - start[channel]) / gain, (cmax - start[channel]) / gain))
                lo, hi = max(lo, a), min(hi, b)
            if hi - lo < step:
                raise ConfigError(f"knob {knob.name!r}: the window inside the device limits is narrower than max_step")
            lower.append(lo)
            upper.append(hi)
        return np.array(lower), np.array(upper)

    def _verify(self, stage, start, best, reference, history):
        zero = np.zeros(len(stage.knobs))
        gains, initials, absolute, best_valid = [], [], [], True
        for j in range(self.config.verify_pairs):
            order = ((best, "best"), (zero, "initial")) if j % 2 == 0 else ((zero, "initial"), (best, "best"))
            values = {}
            for delta, label in order:
                record = self._evaluate(stage, start, delta, "verify", reference, history)
                record["verify_role"] = label
                values[label] = record
            best_valid &= values["best"]["valid"]
            gains.append(values["best"]["model_objective"] - values["initial"]["model_objective"])
            initials.append(values["initial"]["model_objective"])
            absolute.append(values["best"]["transmitted"] - values["initial"]["transmitted"])
        gain = float(np.mean(gains))
        sem = float(np.std(gains, ddof=1) / math.sqrt(len(gains)))
        baseline = float(np.mean(initials))
        absolute_gain = float(np.mean(absolute))
        adopt = bool(best_valid and gain > 0 and gain > self.config.significance * max(sem, 1e-300))
        guarded = bool(stage.normalize_by and not stage.peak and absolute_gain <= 0)
        if guarded:
            # A better ratio must also carry more ions: ions lost on the rods are never measured,
            # so losing them before the normalizing collectors would otherwise raise the ratio.
            adopt = False
        self._log("verify", stage=stage.name, best=best, gains=gains, initials=initials, absolute=absolute, gain=gain,
                  gain_sem=sem, absolute_gain=absolute_gain, best_valid=best_valid, refused_fewer_ions=guarded, adopt=adopt)
        return dict(gain=gain, gain_sem=sem, baseline=baseline, absolute_gain=absolute_gain,
                    relative_gain=gain / baseline if baseline > 0 else math.nan, adopt=adopt)

    def _run_stage(self, stage):
        instrument = self.instrument
        channels = sorted(stage.channels)
        start = instrument.setpoints(channels)
        for channel in channels:
            if not math.isfinite(start[channel]):
                raise InstrumentError(f"{channel}: no valid setpoint")
            self.initial.setdefault(channel, start[channel])
            self.commanded[channel] = start[channel]
        resolved = [knob.resolve(start) for knob in stage.knobs]
        steps = [step for _, step in resolved]
        for knob, step in zip(stage.knobs, steps):
            for channel, gain in knob.channels.items():
                self.max_step[channel] = abs(gain) * step
        lower, upper = self._bounds(stage, start, resolved)
        self._stage_start = dict(start)
        if self.config.target is not None:
            self._stage_start.update({c: self.commanded[c] for c in self.config.target.channels})
        strategy = make_strategy(self.strategy_name, lower, upper, steps, self.rng)
        stage_began = instrument.now()
        self._log("stage_start", stage=stage.name, knobs={k.name: k.channels for k in stage.knobs}, lower=lower, upper=upper,
                  start=start, steps=steps, budget=stage.budget, aperture=stage.aperture, downstream=stage.downstream,
                  normalize_by=stage.normalize_by, peak=stage.peak, strategy=self.strategy_name,
                  peak_measure=self.peak_measure(stage) if stage.peak else None)
        self._emit("stage", stage=stage, lower=lower, upper=upper, start=dict(start), steps=list(steps))
        history = []
        zero = np.zeros(len(stage.knobs))
        first = self._evaluate(stage, start, zero, "initial", None, history)
        second = self._evaluate(stage, start, zero, "initial", None, history)
        reference = 0.5 * (first["incident"] + second["incident"])
        self._log("reference", stage=stage.name, incident=reference, lost_below=self.config.lost_fraction * reference)
        if not reference > self.config.min_current:
            raise InstrumentError(f"{stage.name}: no beam reaches {stage.aperture or 'the collectors'} at the start; tune upstream first")
        for record in (first, second):
            record["valid"] = True
            record["reason"] = ""
            record["model_objective"] = record["objective"] if math.isfinite(record["objective"]) else 0.0
        for i in range(len(stage.knobs)):
            for sign in (1, -1):
                delta = zero.copy()
                delta[i] = sign * steps[i]
                if lower[i] <= delta[i] <= upper[i]:
                    self._evaluate(stage, start, delta, "probe", reference, history)
        budget = stage.budget - 2 * self.config.verify_pairs
        since_reference = 0
        while len(history) < budget and not strategy.converged:
            now = instrument.now()
            if since_reference >= self.config.reference_every:
                self._evaluate(stage, start, strategy.incumbent(history, now), "reference", reference, history)
                since_reference = 0
                continue
            proposed = strategy.ask(history, now)
            delta = np.clip(proposed, lower, upper)
            self._log("ask", stage=stage.name, proposed=proposed, delta=delta, **strategy.diagnostics())
            record = self._evaluate(stage, start, delta, "explore", reference, history)
            strategy.tell(record, history, instrument.now())
            self._log("tell", stage=stage.name, index=record["index"], valid=record["valid"], **strategy.diagnostics())
            since_reference += 1
        best = np.clip(strategy.incumbent(history, instrument.now()), lower, upper)
        if np.allclose(best, zero):
            verdict = dict(gain=0.0, gain_sem=math.nan, baseline=math.nan, relative_gain=0.0, absolute_gain=0.0, adopt=False)
        else:
            verdict = self._verify(stage, start, best, reference, history)
        final = best if verdict["adopt"] else zero
        self._move(self._targets(stage, start, final))
        result = dict(stage=stage.name, start=dict(start), best={k.name: float(d) for k, d in zip(stage.knobs, best)},
                      adopted=verdict["adopt"], final=dict(self._targets(stage, start, final)), evaluations=len(history),
                      converged=bool(strategy.converged),
                      **{k: verdict[k] for k in ("gain", "gain_sem", "baseline", "relative_gain", "absolute_gain")})
        if stage.peak:
            result["peak_center"] = self.peak_center
        self.results.append(result)
        self._log("stage_done", duration_s=instrument.now() - stage_began, strategy_state=strategy.diagnostics(), **result)
        self._emit("stage_done", result=result)
        return result

    def _restore(self):
        targets = self._stage_start
        if not targets:
            return
        self._emit("status", text="Restoring the settings of the stage start")
        self._log("restore", targets=targets, commanded={c: self.commanded.get(c) for c in targets})
        self._restoring = True
        try:
            self._move(targets, interlocks=False)
        finally:
            self._restoring = False

    def _snapshot(self, label):
        """Every current at the present settings: the before/after summary of a run."""
        channels = list(self.config.current_channels)
        if channels:
            _, end, data = self._measure(channels)
            self.checks[label] = dict(time=end, currents={c: data[c][0] * self.config.polarity for c in channels})
            self._log("snapshot", label=label, **self.checks[label])
            self._emit("check", label=label, data=self.checks[label])

    def _summary(self):
        return dict(status=self.status, reason=self.reason, stages=self.results, initial=dict(self.initial),
                    final=dict(self.commanded), evaluations=len(self.history), spectra=dict(self.spectra),
                    checks=dict(self.checks),
                    target=None if self.config.target is None else dict(
                        center=self.config.target.center, width=self.config.target.width,
                        final_center=self.peak_center, filter=self.config.target.filter_name))

    def _start_log(self, kind):
        target = self.config.target
        self._log(f"{kind}_start", strategy=self.strategy_name, options=self._options(),
                  stages=[dict(name=s.name, budget=s.budget, peak=s.peak, knobs=[k.name for k in s.knobs]) for s in self.stages],
                  pressure=self.config.pressure,
                  target=None if target is None else dict(channels=target.channels, center=target.center, width=target.width,
                                                          measure=target.measure, points=target.points, track=target.track,
                                                          max_step=target.max_step, filter=target.filter_name))
        return self.instrument.now()

    def _end_log(self, kind, began, failure):
        self._log(f"{kind}_end", status=self.status, reason=self.reason, duration_s=self.instrument.now() - began,
                  evaluations=len(self.history), counts=self.counts, commanded=self.commanded, initial=self.initial,
                  traceback=failure)

    def run(self):
        self.status = "running"
        target = self.config.target
        began = self._start_log("run")
        failure = None
        try:
            if target is not None:
                self._start_target()
                if target.spectrum_points:
                    self.spectra["before"] = self.sweep(self._spectrum_amplitudes(), label="before")
                    self._emit("spectrum", phase="before", data=self.spectra["before"])
            self._snapshot("before")
            for stage in self.stages:
                self._run_stage(stage)
            self._snapshot("after")
            if target is not None and target.spectrum_points:
                self.spectra["after"] = self.sweep(self._spectrum_amplitudes(), label="after")
                self._emit("spectrum", phase="after", data=self.spectra["after"])
            self.status = "completed"
        except Stopped:
            self.status, self.reason = "stopped", "Stopped by the operator"
            self._restore_after_abort()
        except SoftInterlock as exc:
            failure = traceback.format_exc()
            self.status, self.reason = "interlock", str(exc)
            self._restore_after_abort()
        except ConfigError as exc:
            failure = traceback.format_exc()
            self.status, self.reason = "configuration error", str(exc)
        except InstrumentError as exc:
            # A device that stopped or diverged receives no further command.
            failure = traceback.format_exc()
            self.status, self.reason = "instrument error", str(exc)
        except BaseException:
            self._end_log("run", began, traceback.format_exc())
            raise
        self._end_log("run", began, failure)
        self._emit("done", status=self.status, reason=self.reason)
        return self._summary()

    def spectrum(self, amplitudes, measure, channels=None, max_step=None):
        """Standalone amplitude sweep of a filter (peak picking); returns to the start amplitude."""
        if channels is not None:
            step = float(max_step) if max_step else max(np.diff(np.sort(amplitudes)).max(initial=1.0), 0.1)
            self.config.target = Target(channels, float(amplitudes[0]), step, measure, max_step=step, spectrum_points=0)
        self.status = "running"
        data = dict(amplitude=[], current=[], sem=[])
        began = self._start_log("sweep")
        failure = None
        try:
            self._stage_start = dict(self._prepare_filter())
            target = self.config.target
            for channel, gain in target.channels.items():  # Refuse before any move, never stop half-way.
                lo, hi = self.instrument.limits(channel)
                outside = [float(a) for a in amplitudes if not lo <= gain * a <= hi]
                if outside:
                    raise ConfigError(f"{channel}: sweep {min(outside):g}–{max(outside):g} V leaves the device limits "
                                      f"[{lo:g}, {hi:g}]")
            data = self.sweep(amplitudes, measure)
            self.status = "completed"
        except Stopped:
            self.status, self.reason = "stopped", "Stopped by the operator"
            self._restore_after_abort()
        except SoftInterlock as exc:
            failure = traceback.format_exc()
            self.status, self.reason = "interlock", str(exc)
            self._restore_after_abort()
        except (ConfigError, InstrumentError) as exc:
            failure = traceback.format_exc()
            self.status, self.reason = ("configuration error" if isinstance(exc, ConfigError) else "instrument error"), str(exc)
        self._end_log("sweep", began, failure)
        return dict(status=self.status, reason=self.reason, **data)

    def restore_initial(self):
        """Return every driven channel to its value before the run, step by step (undo)."""
        if not self.initial:
            return dict(status="nothing to undo", reason="")
        self._stage_start = dict(self.initial)
        began = self.instrument.now()
        self._log("revert_start", initial=self.initial, commanded=self.commanded)
        failure = None
        try:
            self._move(dict(self.initial))
            outcome = dict(status="restored", reason="")
        except Stopped:
            outcome = dict(status="stopped", reason="Undo stopped by the operator")
        except (SoftInterlock, InstrumentError) as exc:
            failure = traceback.format_exc()
            outcome = dict(status="error", reason=str(exc))
        self._log("revert_end", duration_s=self.instrument.now() - began, commanded=self.commanded, traceback=failure, **outcome)
        return outcome

    def _restore_after_abort(self):
        try:
            self._restore()
        except (SoftInterlock, InstrumentError) as exc:
            self._log("restore_failed", reason=str(exc), traceback=traceback.format_exc())
            self.reason += f"; restoring the stage start failed: {exc}"


def fit_peak(amplitudes, currents, sems):
    """(height, standard error, centre) of a peak sampled around its maximum.

    A downward parabola whose vertex lies inside the sampled range gives the
    height and centre; otherwise the highest sample is used (peak at an edge).
    """
    amplitudes, currents, sems = (np.asarray(v, float) for v in (amplitudes, currents, sems))
    floor = float(np.sqrt(np.mean(np.square(np.nan_to_num(sems))))) if len(sems) else 0.0
    i = int(np.argmax(currents))
    if len(amplitudes) >= 3 and np.ptp(amplitudes) > 0:
        mid = float(np.mean(amplitudes))
        x = amplitudes - mid
        a, b, c = np.polyfit(x, currents, 2)
        if a < 0 and x.min() <= -b / (2 * a) <= x.max():
            residual = currents - np.polyval([a, b, c], x)
            spread = math.sqrt(float(np.sum(residual ** 2)) / max(len(x) - 3, 1))
            return float(c - b * b / (4 * a)), math.hypot(floor, spread / math.sqrt(len(x))), float(mid - b / (2 * a))
    return float(currents[i]), floor, float(amplitudes[i])


def pick_peak(amplitudes, currents, near):
    """(centre, half-width) of the peak nearest to ``near`` in a spectrum.

    The spectrum is lightly smoothed and only maxima standing out by at least
    5 % of its range count as peaks, so noise near a click is not taken for one.
    The centre is refined by a parabola through the maximum and its neighbours;
    the half-width is half the full width at half prominence (interpolated), from
    the uncut side alone when the spectrum edge cuts the peak.
    """
    amplitudes, currents = (np.asarray(v, float) for v in (amplitudes, currents))
    keep = np.isfinite(amplitudes) & np.isfinite(currents)
    order = np.argsort(amplitudes[keep])
    a, raw = amplitudes[keep][order], currents[keep][order]
    if len(a) < 3:
        raise ValueError("The spectrum needs at least three points")
    k = 3 if len(a) >= 7 else 1  # Light smoothing: a wider window would broaden narrow peaks.
    padded = np.pad(raw, k // 2, mode="edge")
    c = np.convolve(padded, np.ones(k) / k, mode="valid")
    maxima = [i for i in range(len(a)) if (i == 0 or c[i] >= c[i - 1]) and (i == len(a) - 1 or c[i] >= c[i + 1]) and c[i] > 0]

    def base(i):
        # Prominence base: the higher of the lowest points before a higher maximum on each side.
        sides = []
        for direction in (-1, 1):
            if not 0 <= i + direction < len(a):
                continue  # A maximum at the spectrum edge: only its inner side counts.
            j, low = i, c[i]
            while 0 <= j + direction < len(a) and c[j + direction] <= c[i]:
                j += direction
                low = min(low, c[j])
            sides.append(low)
        return max(sides)
    span = float(np.ptp(c))
    peaks = [i for i in maxima if span > 0 and c[i] - base(i) >= 0.05 * span]
    if not peaks:
        raise ValueError("No positive peak in the spectrum")
    i = min(peaks, key=lambda m: abs(a[m] - near))
    center = a[i]
    if 0 < i < len(a) - 1:
        x, y = a[i - 1:i + 2], c[i - 1:i + 2]
        curvature, slope, _ = np.polyfit(x - a[i], y, 2)
        if curvature < 0:
            center = a[i] - slope / (2 * curvature)
            center = float(np.clip(center, a[i - 1], a[i + 1]))
    half = 0.5 * (c[i] + max(base(i), 0.0))

    def edge(direction):
        """Distance from the centre to the half-prominence crossing, or None when the edge cuts it."""
        j = i
        while 0 <= j + direction < len(a) and c[j + direction] >= half:
            j += direction
        if not 0 <= j + direction < len(a):
            return None
        x0, x1, y0, y1 = a[j], a[j + direction], c[j], c[j + direction]
        crossing = x0 + (half - y0) * (x1 - x0) / (y1 - y0) if y1 != y0 else x1
        return abs(crossing - center)
    sides = [d for d in (edge(-1), edge(1)) if d is not None]
    width = float(np.mean(sides)) if sides else 0.5 * float(np.ptp(a))
    step = float(np.median(np.diff(a)))
    return float(center), float(max(width, step))
