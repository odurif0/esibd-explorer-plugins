"""Simple beamline description -> optimizer configuration (TOML) for the Transmission GUI.

The description maps each element setting (inlet, funnel, Q1-Q4) to Explorer
channels, each aperture to a current channel and optional gauges to soft
pressure limits. Windows and steps come from defaults per kind of setting,
scaled by the exploration level. Amplitudes are in volts, like MScan; the m/z
calibration belongs to MScan.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import math

# key, element, label, kind
KNOBS = (
    ("inlet", "Inlet", "Voltage", "inlet"),
    ("funnel_rf", "Funnel", "RF amplitude", "rf"),
    ("funnel_dc", "Funnel", "DC gradient", "dc"),
    ("funnel_exit", "Funnel", "Exit", "offset"),
    ("q1_offset", "Q1", "Offset", "offset"),
    ("q1_rf", "Q1", "RF amplitude", "rf"),
    ("q2_offset", "Q2", "Offset", "offset"),
    ("q2_rf", "Q2", "RF amplitude", "rf"),
    ("q3_offset", "Q3", "Offset", "offset"),
    ("q3_rf", "Q3", "RF amplitude", "rf"),
    ("q4_offset", "Q4", "Offset", "offset"),
    ("q4_rf", "Q4", "RF amplitude", "rf"),
)
# The aperture closing stage p is COLLECTORS[p]; the collector after Q4 ends the beamline.
COLLECTORS = (
    ("A1", "Aperture funnel → Q1"),
    ("A2", "Aperture Q1 → Q2"),
    ("A3", "Aperture Q2 → Q3"),
    ("A4", "Aperture Q3 → Q4"),
    ("collector", "Collector after Q4"),
)
# key, label, knobs, position (index of the aperture closing it)
STAGES = (
    ("funnel", "Inlet + funnel", ("inlet", "funnel_rf", "funnel_dc", "funnel_exit"), 0),
    ("q1", "Q1", ("q1_offset", "q1_rf"), 1),
    ("q2", "Q2", ("q2_offset", "q2_rf"), 2),
    ("q3", "Q3", ("q3_offset", "q3_rf"), 3),
    ("q4", "Q4", ("q4_offset", "q4_rf"), 4),
    ("final", "Final refinement", ("funnel_exit", "q1_offset", "q2_offset", "q3_offset", "q4_offset"), None),
)
FILTERS = (("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4"))
LEVELS = {"fine": 0.4, "normal": 1.0, "wide": 2.5}
# kind: (window half-width, max_step, relative). Relative values are fractions of the start amplitude.
KIND_DEFAULTS = {"inlet": (20.0, 2.0, False), "dc": (10.0, 1.0, False), "offset": (10.0, 1.0, False),
                 "rf": (0.15, 0.02, True)}
KIND = {key: kind for key, _, _, kind in KNOBS}
LABEL = {key: f"{element} {label.lower()}" for key, element, label, _ in KNOBS}


def empty():
    # devices: the device (plugin) of each mapped channel when it was configured, to name a missing plugin.
    return {"polarity": 1, "knobs": {}, "collectors": {}, "gauges": {}, "devices": {}}


def normalize(beamline):
    """Drop unmapped entries and check the types; raise ValueError on nonsense."""
    result = empty()
    data = beamline or {}
    polarity = data.get("polarity", 1)
    if polarity not in (1, -1):
        raise ValueError("polarity must be 1 or -1")
    result["polarity"] = polarity
    for key, channels in (data.get("knobs") or {}).items():
        if key not in KIND:
            raise ValueError(f"Unknown setting {key!r}")
        channels = {str(c): float(g) for c, g in (channels or {}).items() if str(c).strip()}
        if any(not math.isfinite(g) or g == 0 for g in channels.values()):
            raise ValueError(f"{LABEL[key]}: gains must be finite and nonzero")
        if channels:
            result["knobs"][key] = channels
    for key, channel in (data.get("collectors") or {}).items():
        if key not in dict(COLLECTORS):
            raise ValueError(f"Unknown collector {key!r}")
        if channel and str(channel).strip():
            result["collectors"][key] = str(channel)
    for channel, limits in (data.get("gauges") or {}).items():
        lo, hi = (float(v) for v in limits)
        if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi):
            raise ValueError(f"Gauge {channel}: need 0 <= minimum < maximum")
        result["gauges"][str(channel)] = [lo, hi]
    used = [c for channels in result["knobs"].values() for c in channels]
    measured = list(result["collectors"].values())
    mapped = {*used, *measured, *result["gauges"]}
    result["devices"] = {str(c): str(d) for c, d in (data.get("devices") or {}).items() if str(c) in mapped and d}
    if len(set(measured)) != len(measured):
        raise ValueError("A current channel is assigned to two apertures")
    if set(used) & set(measured):
        raise ValueError("A channel cannot be both driven and measured")
    return result


def dumps(beamline):
    return json.dumps(normalize(beamline), indent=2, ensure_ascii=False)


def loads(text):
    return normalize(json.loads(text))


def summary(beamline):
    beamline = normalize(beamline)
    knobs = len(beamline["knobs"])
    return (f"{knobs} setting{'s' * (knobs != 1)} · {len(beamline['collectors'])} current"
            f"{'s' * (len(beamline['collectors']) != 1)} · {len(beamline['gauges'])} gauge"
            f"{'s' * (len(beamline['gauges']) != 1)}")


def _collector_keys():
    return [key for key, _ in COLLECTORS]


def _stage_currents(beamline, position):
    mapped = beamline["collectors"]
    keys = _collector_keys()
    if position is None:  # Final refinement: the last collector of the beamline.
        last = [k for k in keys if k in mapped]
        return None, [mapped[last[-1]]] if last else []
    if position >= len(keys) - 1:
        return None, [mapped["collector"]] if "collector" in mapped else []
    aperture = mapped.get(keys[position])
    downstream = [mapped[k] for k in keys[position + 1:] if k in mapped]
    return aperture, downstream


def stages(beamline, *, mode="total", filter_key=None):
    """[(key, label, knob keys, aperture, downstream, peak)] that can run with this mapping."""
    beamline = normalize(beamline)
    available = []
    position_of_filter = dict((k, i + 1) for i, (k, _) in enumerate(FILTERS)).get(filter_key)
    for key, label, knobs, position in STAGES:
        knobs = [k for k in knobs if k in beamline["knobs"]]
        if mode == "mass" and filter_key:
            knobs = [k for k in knobs if k != f"{filter_key}_rf"]
        aperture, downstream = _stage_currents(beamline, position)
        if not knobs or not downstream:
            continue
        peak = mode == "mass" and position_of_filter is not None and (position is None or position <= position_of_filter)
        available.append((key, label, knobs, aperture, downstream, peak))
    return available


def filters(beamline):
    """Quadrupoles whose RF amplitude is mapped and whose filtered beam is measured."""
    beamline = normalize(beamline)
    return [(key, label) for key, label in FILTERS if f"{key}_rf" in beamline["knobs"] and filter_measure(beamline, key)]


def filter_measure(beamline, filter_key):
    """Current channels behind the filter: from its exit aperture to the end of the beamline."""
    beamline = normalize(beamline)
    position = dict((k, i + 1) for i, (k, _) in enumerate(FILTERS))[filter_key]
    return [beamline["collectors"][k] for k in _collector_keys()[position:] if k in beamline["collectors"]]


def _toml_string(text):
    return json.dumps(str(text), ensure_ascii=False)


def _toml_table(mapping):
    return "{ " + ", ".join(f"{_toml_string(k)} = {float(v)!r}" for k, v in mapping.items()) + " }"


def build(beamline, *, selected=None, level="normal", mode="total", filter_key=None, center=None, width=None,
          settings=None):
    """TOML configuration for the engine. ``selected``: stage keys to run (default: all available)."""
    beamline = normalize(beamline)
    if level not in LEVELS:
        raise ValueError(f"level must be one of {', '.join(LEVELS)}")
    if mode not in ("total", "mass"):
        raise ValueError('mode must be "total" or "mass"')
    if mode == "mass":
        if filter_key not in dict(FILTERS) or f"{filter_key}_rf" not in beamline["knobs"]:
            raise ValueError("Choose a quadrupole whose RF amplitude is mapped as the mass filter")
        if center is None or width is None or not (center >= 0 and width > 0):
            raise ValueError("Choose the peak: its amplitude and half-width in volts")
    plan = stages(beamline, mode=mode, filter_key=filter_key)
    if selected is not None:
        plan = [stage for stage in plan if stage[0] in selected]
    if not plan:
        raise ValueError("No stage can run: map at least one setting and a current after it")
    scale = LEVELS[level]
    lines = ["# Generated by the Transmission GUI. Edit freely in Advanced mode.",
             f"polarity = {beamline['polarity']}"]
    for key, value in (settings or {}).items():
        lines.append(f"{key} = {json.dumps(value) if isinstance(value, str) else value!r}")
    if beamline["gauges"]:
        lines.append("")
        lines.append("[pressure]")
        lines += [f"{_toml_string(channel)} = [{lo!r}, {hi!r}]" for channel, (lo, hi) in beamline["gauges"].items()]
    if mode == "mass":
        measure = filter_measure(beamline, filter_key)
        lines += ["", "[target]", 'mode = "mass"', f"filter = {_toml_string(dict(FILTERS)[filter_key])}",
                  f"channels = {_toml_table(beamline['knobs'][f'{filter_key}_rf'])}",
                  f"center = {float(center)!r}", f"width = {float(width)!r}",
                  f"measure = [{', '.join(_toml_string(c) for c in measure)}]"]
    all_collectors = [beamline["collectors"][k] for k in _collector_keys() if k in beamline["collectors"]]
    for key, label, knobs, aperture, downstream, peak in plan:
        lines += ["", "[[stage]]", f"name = {_toml_string(label)}"]
        if aperture:
            lines.append(f"aperture = {_toml_string(aperture)}")
        lines.append(f"downstream = [{', '.join(_toml_string(c) for c in downstream)}]")
        first = STAGES[0][0] == key
        if not peak and not first and len(all_collectors) >= 2:
            lines.append(f"normalize_by = [{', '.join(_toml_string(c) for c in all_collectors)}]")
        if peak:
            lines.append("peak = true")
        stage_scale = min(scale, LEVELS["fine"]) if key == "final" else scale
        for knob in knobs:
            window, step, relative = KIND_DEFAULTS[KIND[knob]]
            lines += ["", "[[stage.knob]]", f"name = {_toml_string(LABEL[knob])}",
                      f"channels = {_toml_table(beamline['knobs'][knob])}",
                      f"window = [{-window * stage_scale!r}, {window * stage_scale!r}]",
                      f"max_step = {step * stage_scale!r}"]
            if relative:
                lines.append("relative = true")
    return "\n".join(lines) + "\n"
