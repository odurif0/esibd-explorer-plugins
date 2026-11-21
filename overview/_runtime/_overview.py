"""Overview logic without Qt: the device plugins, their lights, the readings of the cards, All ON and All OFF.

A device is reached only through what its plugin shows in Explorer: its ON button (``onAction`` in the device
bar), the state of its controller and its channels. Nothing here talks to a controller or a worker, and nothing
is ever written to a channel: the Overview v1 only reads, and All ON / All OFF press the devices' own buttons.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math

import numpy as np

# Device plugins by name, in beam order for All ON; All OFF runs the other way: ESI first (no more ions), the RF,
# the DC, and the readers last so that the descent is watched to the end (user, 2026-11-22: All OFF turns
# everything OFF). TPG366 OFF only stops reading the gauges; it never switches a gauge.
KINDS = (("TPG366", "tpg366"), ("DMMR", "dmmr"), ("AMPR", "ampr"), ("PSU", "psu"), ("AMX", "amx"), ("ESI", "esi"))
ON_ORDER = tuple(kind for _, kind in KINDS)
OFF_ORDER = tuple(reversed(ON_ORDER))

# Controller flags during which a device plugin ignores or defers a press of its button.
BUSY = (("initializing", "starting"), ("transitioning", "ON/OFF transition"), ("ramping", "ramping"),
        ("_hv_config_loading", "loading its HV configuration"))
# Words of the plugins' state texts that mean the device is not in a state it can confirm.
FAULT_WORDS = ("unconfirmed", "unverified", "lost", "error", "err_", "pending", "fail")
ESI_STOPPING = "Stopping: checking HV"  # ESI: outputs being verified OFF, then the HV discharge check.
PAIR_LINKS = {"CH0-CH1": "psu_ch01", "CH2-CH3": "psu_ch23"}  # AMX output → the Setting naming its PSU.

# The cards of the lab beamline (channel names of 2026-11-18). A name is followed by its output address once seen,
# so that renaming a channel in its plugin changes nothing here. <config>/Overview.json may replace this layout.
DEFAULT_LAYOUT = (
    {"title": "Source", "rows": (
        {"label": "Spray", "channel": "ESI_HV1_I", "main": True, "window_a": (30e-9, 60e-9)},
        {"label": "HV1", "channel": "ESI_HV1"},
        {"label": "Heater", "channel": "ESI_HEAT"})},
    {"title": "Funnel 1", "pressure": 1, "rows": (
        {"label": "Inlet", "channel": "Inlet_Capillary"},
        {"label": "Entry", "channel": "Entry_IF1"},
        {"label": "Middle", "channel": "Middle_IF1"},
        {"label": "Exit", "channel": "Exit_IF1"})},
    {"title": "Funnel 2", "pressure": 2, "rows": (
        {"label": "Entry", "channel": "Entry_IF2"},
        {"label": "Middle", "channel": "Middle_IF2"},
        {"label": "Exit", "channel": "Exit_IF2"})},
    {"title": "Q1", "pressure": 3, "rows": (
        {"label": "Aperture", "channel": "Ap_IF_Q1"},
        {"label": "DC", "channel": "Q1_DC"})},
    {"title": "Q2", "pressure": 4, "quadrupole": "Q2", "rows": (
        {"label": "RF", "rf": ("AMX_HD", "CH2-CH3"), "main": True},
        {"label": "f", "frequency": "AMX_HD"},
        {"label": "x / U", "rods": ("Q2_DC_1", "Q2_DC_2")},
        {"label": "Aperture", "channel": "Ap_Q1_Q2"},
        {"label": "Stubs", "pair": ("Stub_Entry_Q2", "Stub_Exit_Q2")})},
    # Q3 and Q4 are independent quadrupoles; Q3 is built like Q1, Q4 like Q2 (user, 2026-11-22). Q4's RF: AMX_HD
    # CH0-CH1 → PSU_B (lab, 2026-11-20). Names as MScan and Transmission expect them; a missing one shows "—".
    {"title": "Q3", "pressure": 5, "rows": (
        {"label": "Aperture", "channel": "Ap_Q2_Q3"},
        {"label": "DC", "channel": "Q3_DC"})},
    {"title": "Q4", "pressure": 6, "quadrupole": "Q4", "rows": (
        {"label": "RF", "rf": ("AMX_HD", "CH0-CH1"), "main": True},
        {"label": "f", "frequency": "AMX_HD"},
        {"label": "x / U", "rods": ("Q4_DC_1", "Q4_DC_2")},
        {"label": "Aperture", "channel": "Ap_Q3_Q4"},
        {"label": "Stubs", "pair": ("Stub_Entry_Q4", "Stub_Exit_Q4")})},
    {"title": "Detector", "rows": (
        {"label": "M03", "channel": "DMMR_M03", "main": True, "trend": True},)},
)
PRESSURE_DEVICE = "TPG366"
ROW_KINDS = ("channel", "rf", "frequency", "rods", "pair")


# ---------------------------------------------------------------------------------------------------- devices
def _flag(owner, name):
    value = getattr(owner, name, False)
    return isinstance(value, (bool, np.bool_)) and bool(value)


def kind(name):
    """'ampr', 'psu', 'amx', 'dmmr', 'esi', 'tpg366', or None for a device the Overview does not manage."""
    upper = str(name or "").upper()
    return next((k for prefix, k in KINDS if upper == prefix or upper.startswith(prefix + "_")), None)


def managed(devices, on=True):
    """The device plugins All ON (beam order) or All OFF (reverse) presses, each by its own ON button."""
    order = ON_ORDER if on else OFF_ORDER
    found = [d for d in devices if kind(getattr(d, "name", "")) and getattr(d, "useOnOffLogic", False)]
    return sorted(found, key=lambda d: (order.index(kind(d.name)), d.name))


def is_on(device):
    """What the device's ON button shows."""
    return bool(getattr(getattr(device, "onAction", None), "state", False))


def busy(device):
    """What the device plugin is busy with ('' when idle): a press then waits."""
    controller = getattr(device, "controller", None)
    return next((what for flag, what in BUSY if _flag(controller, flag)), "")


def connected(device):
    return _flag(getattr(device, "controller", None), "initialized")


def main_state(device):
    controller = getattr(device, "controller", None)
    for owner in (controller, device):
        state = getattr(owner, "main_state", "")
        if isinstance(state, str) and state:
            return state
    return ""


def fault(device):
    """The plugin's own state text when it cannot confirm its state (lost, unconfirmed, error…), else ''."""
    state = main_state(device)
    if any(word in state.lower() for word in FAULT_WORDS):
        return state
    if _flag(device, "shutdown_unconfirmed"):  # ESI
        return "previous HV/heater shutdown unconfirmed"
    return ""


def _esi_discharging(device):
    """ESI outputs verified OFF; its HV discharge check continues in the background."""
    return main_state(device) == ESI_STOPPING and _flag(getattr(device, "controller", None), "_discharge_outputs_off")


def light(device):
    """(light, words): 'on', 'off', 'busy' or 'fault', as the device plugin reports it."""
    problem = fault(device)
    if problem:
        return "fault", problem
    if main_state(device) == ESI_STOPPING:
        return "busy", "OFF: HV discharging" if _esi_discharging(device) else ESI_STOPPING
    what = busy(device)
    if what:
        return "busy", what[0].upper() + what[1:]
    if is_on(device):
        return ("on", "ON") if connected(device) else ("busy", "Connecting")
    return "off", "OFF"


def reached(device, on):
    """The device is ON (connected) or OFF, and its plugin has finished the transition."""
    if busy(device):
        return False
    if fault(device):  # E.g. "Shutdown unconfirmed": its plugin cannot vouch for OFF, nor for ON.
        return False
    if on:
        return is_on(device) and connected(device)
    if is_on(device):
        return False
    return main_state(device) != ESI_STOPPING or _esi_discharging(device)


def press(device, on):
    """Click the device's ON button, as the operator would, when it does not show ``on`` already."""
    action = getattr(device, "onAction", None)
    if action is None or bool(action.state) == on:
        return False
    action.trigger()
    return True


# ---------------------------------------------------------------------------------------------------- All ON / OFF
FINAL = ("done", "skipped", "failed", "not started", "cancelled")
READERS = ("dmmr", "tpg366")  # All OFF: pressed once every supply is OFF (or failed), so the descent is watched.


@dataclass
class Step:
    device: object
    name: str
    state: str = "waiting"  # waiting, pressed, then one of FINAL
    started: float | None = None  # When this device's turn came (its timeout counts from there).
    pressed_at: float | None = None
    presses: int = 0
    note: str = ""


@dataclass
class Sequence:
    """All ON or All OFF, advanced by ``tick`` on the Qt thread, never blocking.

    All ON presses one ON button at a time, in beam order, once the device is idle, waits until it is connected,
    and stops at the first failure. All OFF (user, 2026-11-22: in parallel) first stops scans and maps (5 s at
    most), then presses the OFF button of every supply at once (ESI, AMX, PSU, AMPR, in that order within the same
    instant) and, once they are all OFF or failed, those of the readers (DMMR, TPG366). A device that fails or
    hangs never holds the others; it is reported. The ESI counts as OFF once its outputs are verified OFF: its HV
    discharge check goes on in the background.
    """

    on: bool
    devices: list
    now: float
    stop_activity: object = None  # All OFF: stops scans and maps (called once).
    activity: object = None  # All OFF: what still runs ('' when stopped).
    log: object = print
    steps: list = field(init=False)
    TIMEOUT_S = 180.0  # Per device: a worker's first launch takes ~30 s, ramps take their time.
    ACTIVITY_S = 5.0  # Scans and maps get this long to stop before the devices go OFF anyway.
    RETRY_S = 3.0  # An OFF ignored during a transition is pressed again, PRESSES times at most.
    PRESSES = 3
    FAULT_S = 10.0  # A fault state still shown this long after the press, the device idle: the press failed.

    def __post_init__(self):
        self.steps = [Step(device, device.name) for device in managed(self.devices, self.on)]
        if self.on:
            self.groups = [[step] for step in self.steps]
        else:
            supplies = [step for step in self.steps if kind(step.name) not in READERS]
            readers = [step for step in self.steps if kind(step.name) in READERS]
            self.groups = [group for group in (supplies, readers) if group]
        self.group = 0
        self.started = self.now
        self.phase = "activity" if not self.on and callable(self.stop_activity) else "devices"
        self.activity_stopped = False
        self.finished = False
        self.summary = ""

    @property
    def word(self):
        return "All ON" if self.on else "All OFF"

    def pending(self):
        """The devices being waited for now."""
        if self.finished or self.phase != "devices" or self.group >= len(self.groups):
            return []
        return [step for step in self.groups[self.group] if step.state not in FINAL]

    def tick(self, now):
        if self.finished:
            return
        if self.phase == "activity":
            if not self.activity_stopped:
                self.activity_stopped = True
                self.log("All OFF: stopping scans and maps.")
                self.stop_activity()
            running = self.activity() if callable(self.activity) else ""
            if running and now - self.started < self.ACTIVITY_S:
                return
            if running:
                self.log(f"All OFF: {running} still running after {self.ACTIVITY_S:.0f} s; the devices go OFF anyway.")
            self.phase = "devices"
        while self.group < len(self.groups):
            steps = self.groups[self.group]
            for step in steps:
                if step.started is None:
                    step.started = now
            for step in steps:  # All OFF: every supply of the group in the same tick.
                if step.state not in FINAL:
                    self._advance(step, now)
            if self.on and any(step.state == "failed" for step in steps):
                for rest in self.steps:
                    if rest.state == "waiting":
                        rest.state = "not started"
                self.group = len(self.groups)
                break
            if any(step.state not in FINAL for step in steps):
                return
            self.group += 1
        self._finish()

    def _advance(self, step, now):
        """Press, check, or close this device's step."""
        device, on = step.device, self.on
        target = "ON" if on else "OFF"
        if reached(device, on):
            step.state = "done" if step.state == "pressed" else "skipped"
            step.note = f"{target}" if step.state == "done" else f"already {target}"
            if on:
                self._record(device)
            self.log(f"{self.word}: {step.name} {step.note}.")
            return
        problem = fault(device)
        if problem and step.pressed_at is not None and now - step.pressed_at > self.FAULT_S and not busy(device):
            return self._fail(step, problem)
        if on:
            if step.pressed_at is None and not busy(device):
                if press(device, True):
                    step.presses += 1
                    self.log(f"All ON: {step.name} ON pressed.")
                step.state, step.pressed_at = "pressed", now  # Or it showed ON already and is connecting.
            elif (step.pressed_at is not None and not is_on(device) and not busy(device)
                  and now - step.pressed_at > self.RETRY_S):
                return self._fail(step, "its ON button went back OFF")
        elif is_on(device) and (step.presses == 0 or (not busy(device) and step.presses < self.PRESSES
                                                       and now - step.pressed_at > self.RETRY_S)):
            press(device, False)
            step.presses += 1
            step.state, step.pressed_at = "pressed", now
            self.log(f"All OFF: {step.name} OFF pressed" + (f" again ({step.presses})" if step.presses > 1 else "") + ".")
        elif step.pressed_at is None:
            step.state, step.pressed_at = "pressed", now  # Shows OFF already; its plugin finishes the transition.
        if now - step.started > self.TIMEOUT_S:
            return self._fail(step, f"not {target} after {self.TIMEOUT_S:.0f} s ({light(device)[1]})")
        return

    def _fail(self, step, note):
        step.state, step.note = "failed", note
        self.log(f"{self.word}: {step.name} failed: {note}.")

    @staticmethod
    def _record(device):
        """Start the device's recording once it is ON (refused by its plugin until it really is)."""
        if getattr(device, "recording", True) is False:
            try:
                device.toggleRecording(on=True, manual=False)
            except Exception:  # noqa: BLE001 - recording is the plugin's; a refusal is not a failure of the ON
                pass

    def cancel(self, reason):
        if self.finished:
            return
        for step in self.steps:
            if step.state not in FINAL:
                step.state, step.note = "cancelled", reason
        self.group = len(self.groups)
        self._finish(cancelled=reason)

    def _finish(self, cancelled=""):
        self.finished = True
        counts = {}
        for step in self.steps:
            counts.setdefault(step.state, []).append(step.name)
        if not self.steps:
            self.summary = f"{self.word}: no device to turn {'ON' if self.on else 'OFF'}."
        else:
            parts = []
            for state, word in (("done", "turned " + ("ON" if self.on else "OFF")), ("skipped", "already " + ("ON" if self.on else "OFF")),
                                ("failed", "failed"), ("not started", "not started"), ("cancelled", "cancelled")):
                if state in counts:
                    parts.append(f"{word}: {', '.join(counts[state])}")
            failed = "; ".join(f"{s.name}: {s.note}" for s in self.steps if s.state == "failed")
            self.summary = f"{self.word} {'cancelled (' + cancelled + ')' if cancelled else 'finished'}. " + "; ".join(parts) + "."
            if failed:
                self.summary += f" Failed: {failed}."
        self.log(self.summary)

    @property
    def ok(self):
        return self.finished and all(step.state in ("done", "skipped") for step in self.steps)


# ---------------------------------------------------------------------------------------------------- channels
def address(channel):
    """The output a channel stands for (device, module, channel), unchanged by a rename; None when unknown."""
    try:
        device = channel.getDevice().name
    except Exception:  # noqa: BLE001 - a channel without its device
        return None

    def number(method):
        function = getattr(channel, method, None)
        try:
            return int(function()) if callable(function) else None
        except Exception:  # noqa: BLE001
            return None

    module, output = number("module_address"), number("channel_number")
    if module is not None and output is not None:
        return f"{device} M{module} CH{output}"  # As MScan writes an AMPR output.
    if output is not None:
        return f"{device} CH{output}"
    if module is not None:
        function = getattr(channel, "function", "")
        return f"{device} M{module}" + (f" {function}" if isinstance(function, str) and function else "")
    gauge = getattr(channel, "input", None)
    if isinstance(gauge, (int, np.integer)) and not isinstance(gauge, bool):
        return f"{device} input {int(gauge)}"
    return None


class Resolver:
    """Finds the channels the layout names; a name seen once is then followed by its output address."""

    def __init__(self, bindings=None):
        self.bindings = dict(bindings or {})
        self.changed = False
        self.channels, self.by_name, self.by_address = [], {}, {}

    def index(self, channels):
        self.channels = [c for c in channels if getattr(c, "real", True)]
        self.by_name, self.by_address = {}, None  # Addresses of all channels only when a name has moved.
        for channel in self.channels:
            self.by_name.setdefault(str(channel.name).strip().lower(), []).append(channel)

    def _addresses(self):
        if self.by_address is None:
            self.by_address = {}
            for channel in self.channels:
                where = address(channel)
                if where:
                    self.by_address.setdefault(where, channel)
        return self.by_address

    def find(self, name):
        bound = self.bindings.get(name)
        found = self.by_name.get(str(name).strip().lower(), [])
        if len(found) == 1 and (not bound or address(found[0]) == bound):
            channel = found[0]  # The usual case: the name is still on its output.
        elif bound and bound in self._addresses():
            return self.by_address[bound]  # Renamed: followed by its output; never the new holder of the name.
        elif len(found) == 1:
            channel = found[0]  # Its output is gone (rewired): the name is followed to its new output.
        else:
            return None
        where = address(channel)
        if where and bound != where:
            self.bindings[name] = where
            self.changed = True
        return channel


def finite(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def device_of(channel):
    try:
        return channel.getDevice()
    except Exception:  # noqa: BLE001
        return None


def output_off(channel):
    """A supply output reported OFF: its readback is then NaN and would not show it."""
    state = getattr(channel, "output_state", None)
    return isinstance(state, str) and state.strip().upper() == "OFF"


def reading(channel):
    """The value the device reads back (monitor, or the value of a measured channel); None when not connected."""
    device = device_of(channel)
    if device is None or not connected(device) or output_off(channel):
        return None
    return finite(channel.monitor if getattr(channel, "useMonitors", False) else channel.value)


def setpoint(channel):
    """The value asked of a supply (AMPR, PSU) for a channel that also has a readback; None for a measurement."""
    device = device_of(channel)
    if not getattr(channel, "useMonitors", False) or kind(getattr(device, "name", "")) not in ("ampr", "psu"):
        return None
    return finite(channel.value)


# ---------------------------------------------------------------------------------------------------- formats
SCALES = {"A": 1.0, "mA": 1e-3, "µA": 1e-6, "nA": 1e-9, "pA": 1e-12, "fA": 1e-15}


def volts(value):
    return "—" if value is None else f"{value:.1f} V"


def current(value):
    """A current with its SI prefix, three digits: 32.3 nA, 412 pA."""
    if value is None:
        return "—"
    if value == 0:
        return "0 A"
    for unit, scale in SCALES.items():
        if abs(value) >= scale * 0.9995 or unit == "fA":
            number = value / scale
            digits = 0 if abs(number) >= 99.95 else 1 if abs(number) >= 9.995 else 2
            return f"{number:.{digits}f} {unit}"
    return f"{value:g} A"


def _window_text(low, high):
    """window 30–60 nA."""
    unit = current(high).split()[-1]
    return f"window {low / SCALES[unit]:g}–{high / SCALES[unit]:g} {unit}"


def pressure(value):
    """1.2e-3 mbar."""
    if value is None or value <= 0:
        return "—" if value is None else f"{value:g} mbar"
    exponent = math.floor(math.log10(value))
    mantissa = value / 10 ** exponent
    if round(mantissa, 1) >= 10:
        mantissa, exponent = mantissa / 10, exponent + 1
    return f"{mantissa:.1f}e{exponent:+d} mbar"


def kilohertz(value):
    return "—" if value is None else f"{value:.1f} kHz"


def celsius(value):
    return "—" if value is None else f"{value:.0f} °C"


# ---------------------------------------------------------------------------------------------------- cards
@dataclass
class RowView:
    label: str
    text: str = "—"
    small: str = ""
    tooltip: str = ""
    main: bool = False
    warn: bool = False
    trend: float | None = None  # The value plotted by a trend row.


@dataclass
class CardView:
    title: str
    pressure: str = ""
    pressure_tip: str = ""
    owner: str = ""  # MScan when it scans this quadrupole.
    rows: list = field(default_factory=list)


def check_layout(layout):
    """The layout of Overview.json, or ValueError: cards with a title and rows of a known kind."""
    cards = []
    for card in layout:
        if not isinstance(card, dict) or not isinstance(card.get("title"), str) or not isinstance(card.get("rows"), (list, tuple)):
            raise ValueError(f"a card needs a title and rows: {card!r}")
        for row in card["rows"]:
            if not isinstance(row, dict) or not isinstance(row.get("label"), str) or not any(k in row for k in ROW_KINDS):
                raise ValueError(f"{card['title']}: a row needs a label and one of {', '.join(ROW_KINDS)}: {row!r}")
        cards.append(card)
    if not cards:
        raise ValueError("no card")
    return tuple(cards)


class Board:
    """What the cards show, from the Explorer devices and channels; read-only."""

    def __init__(self, layout=DEFAULT_LAYOUT, bindings=None):
        self.layout = check_layout(layout)
        self.resolver = Resolver(bindings)
        self.devices = {}

    def update(self, devices, channels):
        self.devices = {getattr(d, "name", ""): d for d in devices}
        self.resolver.index(channels)

    def cards(self, quadrupole_owner=None):
        """``quadrupole_owner``: (quadrupole name, 'MScan scan' or 'MScan map') while MScan runs one."""
        views = []
        for card in self.layout:
            view = CardView(card["title"])
            if card.get("pressure") is not None:
                view.pressure, view.pressure_tip = self._pressure(card["pressure"])
            if quadrupole_owner and card.get("quadrupole") == quadrupole_owner[0]:
                view.owner = quadrupole_owner[1]
            for row in card["rows"]:
                try:
                    view.rows.append(self._row(row))
                except Exception as exc:  # noqa: BLE001 - one row never hides the others
                    view.rows.append(RowView(row.get("label", "?"), "—", tooltip=f"Not available: {exc}"))
            views.append(view)
        return views

    # -- rows
    def _row(self, row):
        view = RowView(row["label"], main=bool(row.get("main")))
        if "channel" in row:
            self._channel_row(view, row)
        elif "rf" in row:
            self._rf_row(view, *row["rf"])
        elif "frequency" in row:
            self._frequency_row(view, row["frequency"])
        elif "rods" in row:
            self._rods_row(view, row["rods"])
        elif "pair" in row:
            self._pair_row(view, row["pair"])
        return view

    def _missing(self, view, name):
        view.text, view.tooltip = "—", f"{name}: not found (plugin not loaded, or no such channel)"

    def _named(self, view, name, channel):
        renamed = f" (now named {channel.name})" if str(channel.name).strip().lower() != str(name).strip().lower() else ""
        where = address(channel)
        view.tooltip = f"{name}{renamed}" + (f" — {where}" if where else "")

    def _channel_row(self, view, row):
        name = row["channel"]
        channel = self.resolver.find(name)
        if channel is None:
            return self._missing(view, name)
        self._named(view, name, channel)
        device = device_of(channel)
        unit = str(getattr(channel, "unit", "") or "")
        measured = callable(getattr(channel, "is_current_channel", None)) and channel.is_current_channel()
        if getattr(device, "name", "") == "ESI" and callable(getattr(channel, "is_heat_channel", None)) and not measured:
            return self._esi_row(view, channel, device)
        value = reading(channel)
        if unit == "A":
            view.text = current(value)
            window = row.get("window_a")
            if window and value is not None:
                low, high = window
                inside = low <= abs(value) <= high
                view.warn = not inside
                view.small = _window_text(low, high) + ("" if inside else (" · below" if abs(value) < low else " · above"))
            elif window:
                view.small = _window_text(*window)
        else:
            view.text = volts(value) if unit in ("V", "") else ("—" if value is None else f"{value:.3g} {unit}")
        asked = setpoint(channel)
        if asked is not None and (value is None or abs(asked - value) >= 0.05):
            view.small = (view.small + " · " if view.small else "") + f"set {asked:.1f}"
        if output_off(channel):
            view.small = (view.small + " · " if view.small else "") + "output OFF"
        if not connected(device):
            view.small = (view.small + " · " if view.small else "") + f"{device.name} {light(device)[1]}"
        if row.get("trend"):
            view.trend = value

    def _esi_row(self, view, channel, device):
        """ESI HV: the electrospray connector's ADC readback; heater: its target. Each with its selection."""
        selected = bool(getattr(channel, "enabled", False))
        target = finite(channel.value)
        if channel.is_heat_channel():
            view.text = celsius(target)
            view.small = "set · " + ("selected" if selected else "deselected")
            return
        readback = None
        controller = getattr(device, "controller", None)
        module = getattr(channel, "module_address", None)
        if connected(device) and callable(module):
            entry = (getattr(controller, "adc_readback", {}) or {}).get(module())
            if isinstance(entry, dict) and entry.get("state") == "ok":
                readback = finite(entry.get("value"))
        view.text = volts(readback)
        view.small = ("ON" if selected else "OFF") + ("" if target is None else f" · set {target:.0f} V")
        if not connected(device):
            view.small += f" · ESI {light(device)[1]}"

    def _device(self, name):
        return self.devices.get(name)

    def _rf_row(self, view, amx_name, pair):
        amx = self._device(amx_name)
        if amx is None:
            return self._missing(view, amx_name)
        link = PAIR_LINKS.get(pair)
        psu_name = str(getattr(amx, link, "None") or "None") if link else "None"
        psu = self._device(psu_name)
        if psu is None:
            view.tooltip = f"{amx_name} {pair}: no PSU linked in its Settings" if psu_name in ("", "None") else \
                f"{amx_name} {pair}: {psu_name} not loaded"
            return
        rails = {}
        for channel in psu.getChannels():
            number = getattr(channel, "channel_number", None)
            if getattr(channel, "real", True) and callable(number) and number() in (0, 1):
                rails[number()] = channel
        if set(rails) != {0, 1}:
            view.tooltip = f"{psu_name}: CH0 or CH1 missing"
            return
        values = [reading(rails[n]) for n in (0, 1)]
        asked = [setpoint(rails[n]) for n in (0, 1)]
        if None not in values:
            view.text = volts((abs(values[0]) + abs(values[1])) / 2)
        if any(output_off(rails[n]) for n in (0, 1)):
            view.small = "output OFF"
        elif None not in asked and (None in values or abs((abs(asked[0]) + abs(asked[1])) / 2 - (abs(values[0]) + abs(values[1])) / 2) >= 0.05):
            view.small = f"set {(abs(asked[0]) + abs(asked[1])) / 2:.1f}"
        if not connected(psu):
            view.small = (view.small + " · " if view.small else "") + f"{psu_name} {light(psu)[1]}"
        view.tooltip = (f"{amx_name} {pair} → {psu_name}: CH0 {volts(values[0])}, CH1 {volts(values[1])}"
                        f"; {amx_name} {light(amx)[1]}")

    def _frequency_row(self, view, amx_name):
        amx = self._device(amx_name)
        if amx is None:
            return self._missing(view, amx_name)
        asked = finite(getattr(amx, "frequency_khz", None))
        running = is_on(amx) and connected(amx)
        view.text = kilohertz(asked if running else None)
        if not running:
            view.small = ("" if asked is None else f"set {asked:.1f} · ") + f"{amx_name} {light(amx)[1]}"
        view.tooltip = f"{amx_name} frequency"

    def _rods_row(self, view, names):
        rods = [self.resolver.find(name) for name in names]
        if None in rods:
            return self._missing(view, names[rods.index(None)])
        values = [reading(rod) for rod in rods]
        if None not in values:
            x, u = (values[0] + values[1]) / 2, (values[0] - values[1]) / 2
            view.text = f"x {x:.1f}  U {u:.1f}"
        view.tooltip = (f"x = mean, U = half difference of {rods[0].name} ({volts(values[0])}) and "
                        f"{rods[1].name} ({volts(values[1])})")
        device = device_of(rods[0])
        if not connected(device):
            view.small = f"{device.name} {light(device)[1]}"

    def _pair_row(self, view, names):
        channels = [self.resolver.find(name) for name in names]
        if None in channels:
            return self._missing(view, names[channels.index(None)])
        values = [reading(channel) for channel in channels]
        view.text = " / ".join("—" if v is None else f"{v:.0f}" for v in values) + ("" if None in values else " V")
        view.tooltip = ", ".join(f"{c.name} {volts(v)}" for c, v in zip(channels, values))
        device = device_of(channels[0])
        if not connected(device):
            view.small = f"{device.name} {light(device)[1]}"

    def _pressure(self, number):
        tpg = self._device(PRESSURE_DEVICE)
        if tpg is None:
            return "", f"P{number}: {PRESSURE_DEVICE} not loaded"
        for channel in tpg.getChannels():
            if getattr(channel, "input", None) == number:
                value = reading(channel)
                status = str(getattr(channel, "status", "") or "")
                tip = f"P{number} ({channel.name})" + (f": {status}" if status else "")
                if not connected(tpg):
                    tip += f"; {PRESSURE_DEVICE} {light(tpg)[1]}"
                return pressure(value), tip
        return "—", f"P{number}: no {PRESSURE_DEVICE} channel on input {number}"
