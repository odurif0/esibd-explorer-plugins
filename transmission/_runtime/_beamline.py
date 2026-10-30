"""Simple beamline description -> optimizer configuration (TOML) for the Transmission GUI.

The description maps each element setting (inlet, funnel, Q1-Q4) to Explorer
channels, names the single detector (its current channel and where it sits),
the electrospray current that must stay stable (30-60 nA by default) and
optional gauges with soft pressure limits. Windows and steps come from
defaults per kind of setting, scaled by the exploration level. Amplitudes are
in volts, like MScan; the m/z calibration belongs to MScan.

The criterion is always the detector current. The detector normally sits after
Q4; it can also be placed right behind one of the four apertures (funnel/Q1,
Q1/Q2, Q2/Q3, Q3/Q4) to optimize what passes through it, and then only the
elements before it are optimized. The apertures themselves are not measured.

Every RF amplitude is an AMX output, never free channels. An AMX has two
outputs, CH0-CH1 and CH2-CH3, each feeding one quadrupole: its two lines drive
the two rod pairs and switch, in opposite phase, between the two outputs (rails)
of the PSU linked to it in the AMX Settings: PSU CH0 = Vpos, CH1 = Vneg. Both
rails move together (Vpos = Vneg = A as magnitudes, as in MScan). The stored description keeps the AMX output
(``rf``, named by its channel pair); ``resolve`` turns it into its two rails.
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
# Where the single detector sits, in beam order: behind aperture k (k = 1..4) or after Q4.
DETECTOR_POSITIONS = (
    ("A1", "Behind aperture 1 (funnel → Q1)"),
    ("A2", "Behind aperture 2 (Q1 → Q2)"),
    ("A3", "Behind aperture 3 (Q2 → Q3)"),
    ("A4", "Behind aperture 4 (Q3 → Q4)"),
    ("after_q4", "After Q4 (standard)"),
)
DETECTOR_PLACES = {"A1": "behind aperture 1", "A2": "behind aperture 2", "A3": "behind aperture 3",
                   "A4": "behind aperture 4", "after_q4": "after Q4"}
# key, label, knobs, position (index of the aperture closing it; 4 = Q4, before the standard detector)
STAGES = (
    ("funnel", "Inlet + funnel", ("inlet", "funnel_rf", "funnel_dc", "funnel_exit"), 0),
    ("q1", "Q1", ("q1_offset", "q1_rf"), 1),
    ("q2", "Q2", ("q2_offset", "q2_rf"), 2),
    ("q3", "Q3", ("q3_offset", "q3_rf"), 3),
    ("q4", "Q4", ("q4_offset", "q4_rf"), 4),
    ("final", "Final refinement", ("funnel_exit", "q1_offset", "q2_offset", "q3_offset", "q4_offset"), None),
)
FILTERS = (("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"), ("q4", "Q4"))
FILTER_POSITION = {key: index + 1 for index, (key, _) in enumerate(FILTERS)}
KNOB_POSITION = {knob: position for _, _, knobs, position in STAGES if position is not None for knob in knobs}
LEVELS = {"fine": 0.4, "normal": 1.0, "wide": 2.5}
# kind: (window half-width, max_step, relative). Relative values are fractions of the start amplitude.
KIND_DEFAULTS = {"inlet": (20.0, 2.0, False), "dc": (10.0, 1.0, False), "offset": (10.0, 1.0, False),
                 "rf": (0.15, 0.02, True)}
KIND = {key: kind for key, _, _, kind in KNOBS}
LABEL = {key: f"{element} {label if label.isupper() or label.split()[0].isupper() else label.lower()}"
         for key, element, label, _ in KNOBS}  # "Q2 RF amplitude", "Funnel DC gradient", "Inlet voltage".
RF_KEYS = tuple(key for key, _, _, kind in KNOBS if kind == "rf")
PAIRS = ("CH0-CH1", "CH2-CH3")  # AMX output pairs, each fed by one PSU: CH0 = Vpos, CH1 = Vneg.
SPRAY_RANGE_NA = (30.0, 60.0)  # A stable electrospray for the optimization (nA, magnitude).


def empty():
    # rf: the AMX output (channel pair) of each RF amplitude. devices: the device (plugin) of each mapped channel when it
    # was configured, to name a missing plugin.
    # detector: {"channel", "position"}, the one current measured and maximized.
    # spray: {"channel", "range_na"}, the electrospray current every averaging window waits for.
    return {"polarity": 1, "knobs": {}, "rf": {}, "detector": {}, "spray": {}, "gauges": {}, "devices": {}}


def reach(beamline):
    """Number of stage positions upstream of the detector (5 after Q4, k behind aperture k)."""
    detector = beamline.get("detector") or {}
    keys = [key for key, _ in DETECTOR_POSITIONS]
    return keys.index(detector["position"]) + 1 if detector else 0


def pair_label(amx, pair):
    return f"{amx} {pair}"


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
    for key, entry in (data.get("rf") or {}).items():
        if key not in RF_KEYS:
            raise ValueError(f"{LABEL.get(key, key)} is not an RF amplitude")
        amx, pair = str((entry or {}).get("amx", "")).strip(), str((entry or {}).get("pair", "")).strip()
        if not amx and not pair:
            continue
        if not amx or pair not in PAIRS:
            raise ValueError(f"{LABEL[key]}: choose an AMX output ({' or '.join(PAIRS)})")
        if key in result["knobs"]:
            raise ValueError(f"{LABEL[key]}: mapped both to an AMX output and to channels")
        result["rf"][key] = {"amx": amx, "pair": pair}
    pairs = [pair_label(e["amx"], e["pair"]) for e in result["rf"].values()]
    for label in dict.fromkeys(pairs):
        if pairs.count(label) > 1:
            raise ValueError(f"{label} drives one RF amplitude only; it is assigned to two elements")
    detector = data.get("detector")
    if detector is None and (data.get("collectors") or {}).get("collector"):
        # Saved before the single detector: the collector after Q4 was the end of the beamline.
        detector = {"channel": data["collectors"]["collector"], "position": "after_q4"}
    detector = detector or {}
    channel = str(detector.get("channel") or "").strip()
    position = str(detector.get("position") or "after_q4")
    if position not in DETECTOR_PLACES:
        raise ValueError(f"Unknown detector position {position!r}")
    if channel:
        result["detector"] = {"channel": channel, "position": position}
    spray = data.get("spray") or {}
    channel = str(spray.get("channel") or "").strip()
    if channel:
        lo, hi = (float(v) for v in spray.get("range_na", SPRAY_RANGE_NA))
        if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi):
            raise ValueError("Spray current: need 0 <= minimum < maximum (nA)")
        result["spray"] = {"channel": channel, "range_na": [lo, hi]}
    for channel, limits in (data.get("gauges") or {}).items():
        lo, hi = (float(v) for v in limits)
        if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo < hi):
            raise ValueError(f"Gauge {channel}: need 0 <= minimum < maximum")
        result["gauges"][str(channel)] = [lo, hi]
    used = [c for channels in result["knobs"].values() for c in channels]
    measured = [result["detector"]["channel"]] if result["detector"] else []
    sprayed = [result["spray"]["channel"]] if result["spray"] else []
    mapped = {*used, *measured, *sprayed, *result["gauges"]}
    mapped |= {e["amx"] for e in result["rf"].values()}
    result["devices"] = {str(c): str(d) for c, d in (data.get("devices") or {}).items() if str(c) in mapped and d}
    if set(used) & {*measured, *sprayed, *result["gauges"]}:
        raise ValueError("A channel cannot be both driven and measured")
    if set(sprayed) & {*measured, *result["gauges"]}:
        raise ValueError("The spray current needs its own channel, not the detector or a gauge")
    return result


def dumps(beamline):
    return json.dumps(normalize(beamline), indent=2, ensure_ascii=False)


def loads(text):
    return normalize(json.loads(text))


def summary(beamline):
    beamline = normalize(beamline)
    knobs = len(beamline["knobs"]) + len(beamline["rf"])
    detector = beamline["detector"]
    where = f"detector {DETECTOR_PLACES[detector['position']]}" if detector else "no detector"
    spray = beamline["spray"]
    watched = f"spray {spray['range_na'][0]:g}–{spray['range_na'][1]:g} nA" if spray else "spray not watched"
    return (f"{knobs} setting{'s' * (knobs != 1)} · {where} · {watched} · {len(beamline['gauges'])} gauge"
            f"{'s' * (len(beamline['gauges']) != 1)}")


def migrate(beamline, pairs):
    """An RF amplitude saved as the two rails of a known pair becomes that pair.

    ``pairs``: {(amx, pair): (vpos, vneg)} rail channel names. Other channel mappings of an RF
    amplitude are left for ``resolve`` to refuse; nothing changes if a pair would serve twice."""
    original = normalize(beamline)
    migrated = normalize(beamline)
    owner = {frozenset(rails): key for key, rails in pairs.items()}
    for key in [k for k in RF_KEYS if k in migrated["knobs"]]:
        channels = migrated["knobs"][key]
        found = owner.get(frozenset(channels))
        if found is not None and len(channels) == 2 and all(g == 1.0 for g in channels.values()):
            del migrated["knobs"][key]
            migrated["rf"][key] = {"amx": found[0], "pair": found[1]}
    try:
        return normalize(migrated)
    except ValueError:
        return original


def resolve(beamline, rails, amx_rails=()):
    """(beamline with each RF pair replaced by its rail channels, problems).

    ``rails(amx, pair)`` returns the (Vpos, Vneg) channel names or raises ValueError with the reason.
    ``amx_rails``: every channel that is a rail of an AMX output; such a channel is never a DC setting and
    an RF amplitude never drives free channels. A refused mapping is dropped, never driven."""
    beamline = normalize(beamline)
    problems = []
    knobs = {}
    rail_set = set(amx_rails)
    for key, channels in beamline["knobs"].items():
        if KIND[key] == "rf":
            problems.append(f"{LABEL[key]}: choose its AMX output (Configure…); it is not driven")
            continue
        used = sorted(set(channels) & rail_set)
        if used:
            problems.append(f"{LABEL[key]}: {', '.join(used)} is an AMX rail and moves with its AMX output; it is not driven")
            continue
        knobs[key] = channels
    owners = {}
    for key, entry in beamline["rf"].items():
        try:
            vpos, vneg = rails(entry["amx"], entry["pair"])
        except ValueError as exc:
            problems.append(f"{LABEL[key]}: {exc}")
            continue
        shared = [owners[c] for c in (vpos, vneg) if c in owners]
        if shared:
            problems.append(f"{LABEL[key]}: same rails as {LABEL[shared[0]]} (one PSU feeds both AMX outputs); it is not driven")
            continue
        owners.update({vpos: key, vneg: key})
        knobs[key] = {vpos: 1.0, vneg: 1.0}
    resolved = dict(beamline, knobs={k: knobs[k] for k in KIND if k in knobs}, rf={})
    return normalize(resolved), problems


def stages(beamline, *, mode="total", filter_key=None):
    """[(key, label, knob keys, aperture, downstream, peak)] before the detector, which they all maximize.

    Only the elements upstream of the detector act on its current: with the detector behind aperture k,
    the stages up to that aperture, and the final refinement of their offsets."""
    beamline = normalize(beamline)
    detector = beamline["detector"]
    if not detector:
        return []
    upstream = reach(beamline)
    position_of_filter = FILTER_POSITION.get(filter_key)
    available = []
    for key, label, knobs, position in STAGES:
        if position is not None and position >= upstream:
            continue
        knobs = [k for k in knobs if k in beamline["knobs"] and KNOB_POSITION[k] < upstream]
        if mode == "mass" and filter_key:
            knobs = [k for k in knobs if k != f"{filter_key}_rf"]
        if not knobs:
            continue
        peak = mode == "mass" and position_of_filter is not None and (position is None or position <= position_of_filter)
        available.append((key, label, knobs, None, [detector["channel"]], peak))
    return available


def filters(beamline):
    """Quadrupoles whose RF amplitude is mapped and that sit before the detector."""
    beamline = normalize(beamline)
    return [(key, label) for key, label in FILTERS if f"{key}_rf" in beamline["knobs"] and filter_measure(beamline, key)]


def filter_measure(beamline, filter_key):
    """The detector, when it sits behind the filter quadrupole."""
    beamline = normalize(beamline)
    if not beamline["detector"] or FILTER_POSITION[filter_key] >= reach(beamline):
        return []
    return [beamline["detector"]["channel"]]


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
        if not filter_measure(beamline, filter_key):
            raise ValueError("The mass filter must sit before the detector")
        if center is None or width is None or not (center >= 0 and width > 0):
            raise ValueError("Choose the peak: its amplitude and half-width in volts")
    plan = stages(beamline, mode=mode, filter_key=filter_key)
    if selected is not None:
        plan = [stage for stage in plan if stage[0] in selected]
    if not plan:
        raise ValueError("No stage can run: map the detector and at least one setting before it")
    scale = LEVELS[level]
    lines = ["# Generated by the Transmission GUI. Edit freely in Advanced mode.",
             f"polarity = {beamline['polarity']}"]
    for key, value in (settings or {}).items():
        lines.append(f"{key} = {json.dumps(value) if isinstance(value, str) else value!r}")
    if beamline["gauges"]:
        lines.append("")
        lines.append("[pressure]")
        lines += [f"{_toml_string(channel)} = [{lo!r}, {hi!r}]" for channel, (lo, hi) in beamline["gauges"].items()]
    if beamline["spray"]:
        lo, hi = beamline["spray"]["range_na"]
        lines += ["", "# Each averaging window is measured again until the spray is stable (max_spread, default 0.2);",
                  "# after wait_s (default 120 s) without a stable spray the run stops.", "[spray]",
                  f"channel = {_toml_string(beamline['spray']['channel'])}",
                  f"range = [{lo / 1e9!r}, {hi / 1e9!r}]  # A: {lo:g}–{hi:g} nA"]
    if mode == "mass":
        measure = filter_measure(beamline, filter_key)
        lines += ["", "[target]", 'mode = "mass"', f"filter = {_toml_string(dict(FILTERS)[filter_key])}",
                  f"channels = {_toml_table(beamline['knobs'][f'{filter_key}_rf'])}",
                  f"center = {float(center)!r}", f"width = {float(width)!r}",
                  f"measure = [{', '.join(_toml_string(c) for c in measure)}]"]
    for key, label, knobs, aperture, downstream, peak in plan:
        lines += ["", "[[stage]]", f"name = {_toml_string(label)}"]
        if aperture:
            lines.append(f"aperture = {_toml_string(aperture)}")
        lines.append(f"downstream = [{', '.join(_toml_string(c) for c in downstream)}]  # the detector")
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
