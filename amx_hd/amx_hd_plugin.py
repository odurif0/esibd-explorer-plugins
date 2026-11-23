"""Drive AMX timing from ESIBD Explorer and monitor pulser readbacks."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import sys
import time
from pathlib import Path
from threading import Event, Lock, RLock, Thread, current_thread
from typing import Any, cast

import numpy as np

from esibd.core import (
    PARAMETERTYPE,
    PLUGINTYPE,
    PRINT,
    Channel,
    DeviceController,
    Parameter,
    ToolButton,
    parameterDict,
)
from esibd.plugins import Device, LiveDisplay, Plugin

_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_amx_hd_runtime"
_AMX_KEEP_TOOLTIP = (
    "What this pulser keeps when the oscillator frequency changes: duty keeps its duty cycle "
    "and delay as a share of the period (square waves, RF); width keeps its width and delay "
    "in microseconds (fixed-length pulses)."
)
_AMX_TIMING_COLUMNS = (
    ("pulser", "Timer"), ("drives", "Drives"), ("enable", "On"), ("width", "Width\n(µs)"),
    ("duty", "Duty\n(%)"), ("keep", "Osc change\nkeeps"), ("status", "Registers"),
)
_AMX_TIMING_COLUMN_TIPS = {
    "pulser": "Timer number; timer numbers are not output channel numbers. Colour: register state.",
    "drives": "Outputs switched by this timer, from the live routing registers.",
    "enable": "Run this timer while the AMX HD is ON.",
    "width": "Pulse width. Width and duty are one setting, on the 10 ns clock grid. "
             "Read-only while the channel follows its equation (Advanced).",
    "duty": "Pulse width as a share of the oscillator-0 period.",
    "keep": _AMX_KEEP_TOOLTIP,
    "status": "Whether the controller registers hold the request; not a waveform measurement.",
}
# Routing registers change only through a config load or a write of this plugin (the port is
# exclusive): they are re-read after every transition, the poll after any write, and then at
# this cadence. 2026-11-11 lab: at 5 s, 7 of the day's 11 lost AMX replies hit these reads
# (GetTimerTriggerSource); the worker already keeps its timer registers for 30 s.
_HD_ROUTING_REFRESH_S = 30.0


def _amx_poisoned_port_guidance(
    exc: Exception,
    *,
    poisoned_com: int | None,
    current_com: int | None,
) -> str:
    """Return operator guidance for a failed AMX init, or "" when none applies.

    Mirrors the two poisoning scenarios recorded across retries by the
    controller: a transport just poisoned by this failure, or a transport that
    was poisoned earlier in the session and is still locking the port. Returning
    "" keeps unrelated init failures (genuine cabling, wrong COM number, ...)
    free of misleading "restart the app" advice.
    """
    if _amx_transport_failure_is_fatal(exc):
        return _AMX_POISONED_PORT_RECOVERY
    if (
        poisoned_com is not None
        and current_com is not None
        and int(poisoned_com) == int(current_com)
    ):
        return _AMX_POISONED_PORT_RETRY
    return ""


def _optional_nonnegative_int(value: Any) -> int | None:
    """Return a non-negative register value, or None for unavailable readback."""
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


# HV-AMX-CTRL-4EDH signal bus (SIGNAL_MASK 0x3F, SIGNAL_INVERT 0x80). The order and
# names are CGC's own: COM-HVAMX4EDH-Control 1-20 signal tables, the names used in
# its configuration files ("Timer00=7,4998,Osc0,Pos", "SwitchTrigger0=TimerOut00,Pos").
_HD_SIGNALS: tuple[tuple[str, str], ...] = (
    ("0", "Logic 0"),
    ("SoftTrig0", "Software trigger #0"),
    ("SoftTrig1", "Software trigger #1"),
    ("Enb", "Device enable"),
    *((f"TimerOut{t:02d}", f"Timer #{t} output") for t in range(16)),
    *((f"TimerRun{t:02d}", f"Timer #{t} running") for t in range(16)),
    *((f"Osc{k}", f"Oscillator #{k} output") for k in range(4)),
    *((f"Mapping{e}-Out{o}", f"Mapping engine #{e} output #{o}") for e in range(4) for o in range(4)),
    *((f"Din{i}", f"Digital input #{i}") for i in range(8)),
)
_HD_SIGNAL_TIMER_OUT = 4
_HD_SIGNAL_TIMER_RUN = 20
_HD_SIGNAL_OSCILLATOR = 36
_HD_SIGNAL_MAPPING = 40
_HD_SIGNAL_DIN = 56
_HD_CLOCK_SOURCES = (
    "0", "CLK2M", "CLK10M", "CLK100M", "PLL0", "PLL1", "PLL2", "PLL3",
    "Cnt0", "Cnt1", "Cnt2", "Cnt3", "Div0", "Div1", "Div2", "Div3",
)
# Only the fixed quartz clocks have a frequency known without decoding a PLL,
# counter or divider configuration.
_HD_CLOCK_SOURCE_HZ = {1: 2e6, 2: 10e6, 3: 100e6}
_HD_CLOCK_100M = 3
_HD_BURST_TIMERS_DEFAULT = 8
# COM_HVAMX4EDH_MAX_CONFIG slots. The setting clamps silently: at 255, choosing a config above it
# loaded config 255 (2026-11-02 lab).
_HD_MAX_CONFIG_INDEX = 499


def _hd_signal_text(value: Any) -> str:
    code = _optional_nonnegative_int(value)
    if code is None or code > 0xFF or code & 0x40:
        return "unknown"
    if code == 0x80:
        return "1"
    name = _HD_SIGNALS[code & 0x3F][0]
    return ("inverted " if code & 0x80 else "") + name


def _hd_clock_text(value: Any) -> str:
    code = _optional_nonnegative_int(value)
    if code is None or code & ~0x8F:
        return "unknown"
    return ("inverted " if code & 0x80 else "") + _HD_CLOCK_SOURCES[code & 0x0F]


def _hd_timer_clock(timer: int) -> int:
    """Clock feeding one timer: CGC configs pair timers 0/1 and 8/9 with Clock0, etc."""
    return (int(timer) % 8) // 2


def _amx_hd_output_rows(snapshot: dict[str, Any], routing: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Describe CH0–3 from live HD registers, never from a config name or index.

    ``routing`` holds the source/clock registers that ``collect_housekeeping`` does
    not return. Semantics follow CGC's 4EDH test configurations: enable low gives
    Hi-Z, enable high with trigger low/high gives Vneg/Vpos; oscillator k runs on
    Clock k and timer t on Clock ((t mod 8) // 2); periods are register + 2 ticks,
    widths + 2 and delays + 3. Mapping engines, bursts, chained or external
    triggers and PLL/counter/divider clocks are deliberately not flattened into a
    frequency. Vneg/Vpos are rail names, not voltage measurements.
    """
    raw_flags = snapshot.get("controller_state", {}).get("flags")
    flags = set(raw_flags) if isinstance(raw_flags, (list, tuple)) else None
    routing = routing if isinstance(routing, dict) else {}
    timers = {p.get("pulser"): p for p in snapshot.get("pulsers", []) if isinstance(p, dict)}
    clocks = list(routing.get("clock_sources") or [])
    periods = list(routing.get("oscillator_periods") or [])
    triggers = routing.get("timer_triggers") or {}
    stops = routing.get("timer_stops") or {}
    switches = {s.get("switch"): s for s in routing.get("switches", []) if isinstance(s, dict)}
    burst_timers = _coerce_int(routing.get("burst_timers"), _HD_BURST_TIMERS_DEFAULT)

    def unknown(reason: str, kind: str = "Unknown") -> dict[str, Any]:
        return {"kind": kind, "reason": reason}

    def constant(value: bool) -> dict[str, Any]:
        return {"kind": "Static", "value": value, "reason": "Constant trigger level."}

    def clock(index: int) -> tuple[int | None, float | None, str]:
        code = _optional_nonnegative_int(clocks[index]) if 0 <= index < len(clocks) else None
        if code is None or code & ~0x8F:
            return None, None, f"Clock {index} source is missing or invalid."
        hz = _HD_CLOCK_SOURCE_HZ.get(code & 0x0F)
        if hz is None:
            return None, None, f"Clock {index} uses {_hd_clock_text(code)}; its frequency is not inferred."
        return code, hz, ""

    def oscillator(k: int) -> tuple[int | None, int | None, float | None, str]:
        period = _optional_nonnegative_int(periods[k]) if 0 <= k < len(periods) else None
        if period is None or period == 0:
            return None, None, None, f"Oscillator #{k} period is missing or invalid."
        code, hz, reason = clock(k)
        if code is None:
            return None, None, None, reason
        return period + 2, code, hz, ""

    def source(config: Any) -> dict[str, Any]:
        code = _optional_nonnegative_int(config)
        if code is None or code > 0xFF or code & 0x40:
            return unknown("Signal selection is missing or unsupported.")
        selected, inverted = code & 0x3F, bool(code & 0x80)
        if selected == 0:
            return constant(inverted)
        if flags is None:
            return unknown("Controller enable flags are unavailable.")
        if selected in (1, 2):
            return unknown("Software-triggered signal; no fixed frequency inferred.", "Software")
        if selected == 3:
            # CGC's "Enb" test configs show this signal following the pulse-generator enable.
            return constant(("ST_ENB_PULS_GEN" in flags) != inverted)
        if selected >= _HD_SIGNAL_DIN:
            return unknown(f"External input Din{selected - _HD_SIGNAL_DIN}; frequency is not measured.",
                           "External")
        if selected >= _HD_SIGNAL_MAPPING:
            engine, output = divmod(selected - _HD_SIGNAL_MAPPING, 4)
            return unknown(f"Mapping engine #{engine} output #{output}; mapped logic is not decoded.")
        if _HD_SIGNAL_TIMER_RUN <= selected < _HD_SIGNAL_OSCILLATOR:
            return unknown(f"Timer #{selected - _HD_SIGNAL_TIMER_RUN} running-state signal; "
                           "waveform not inferred.")
        if "ST_CLRN" not in flags or "ST_ENB_PULS_GEN" not in flags:
            return constant(inverted)  # Timing generators cleared or disabled.
        if selected >= _HD_SIGNAL_OSCILLATOR:
            k = selected - _HD_SIGNAL_OSCILLATOR
            if f"ST_ENB_OSC{k}" not in flags:
                return constant(inverted)
            period, clock_code, clock_hz, reason = oscillator(k)
            if period is None:
                return unknown(reason)
            high, phase = 1, 0  # An oscillator emits one clock tick each period.
        else:
            t = selected - _HD_SIGNAL_TIMER_OUT
            if "ST_ENB_TIMER" not in flags:
                return constant(inverted)
            timer = timers.get(t, {})
            width = _optional_nonnegative_int(timer.get("width_ticks"))
            delay = _optional_nonnegative_int(timer.get("delay_ticks"))
            trigger = _optional_nonnegative_int(triggers.get(t))
            if width == 0 or delay == 0 or trigger == 0:
                return constant(inverted)
            if width is None or delay is None or trigger is None:
                return unknown(f"Incomplete timing/routing readback for timer #{t}.")
            if trigger > 0xFF or trigger & 0x40:
                return unknown(f"Timer #{t} trigger source is unsupported.")
            if t < burst_timers:
                burst = timer.get("burst")
                if burst is None:
                    return unknown(f"Timer #{t} burst register is unavailable.")
                if burst != 0:
                    return unknown(f"Timer #{t}: bursts of {_coerce_int(burst, 0) + 1} pulses per trigger; "
                                   "not a continuous square wave.", "Burst")
                stop = _optional_nonnegative_int(stops.get(t))
                if stop is None:
                    return unknown(f"Timer #{t} stop-source register is unavailable.")
                if stop != 0:
                    return unknown(f"Timer #{t} stop source is {_hd_signal_text(stop)}; waveform not inferred.")
            trigger_selected = trigger & 0x3F
            if trigger_selected >= _HD_SIGNAL_DIN:
                return unknown(f"Timer #{t} is externally triggered ({_hd_signal_text(trigger)}); "
                               "frequency is not measured.", "External")
            if not _HD_SIGNAL_OSCILLATOR <= trigger_selected < _HD_SIGNAL_MAPPING:
                return unknown(f"Timer #{t}: software, chained or unsupported trigger "
                               f"({_hd_signal_text(trigger)}).")
            k = trigger_selected - _HD_SIGNAL_OSCILLATOR
            if f"ST_ENB_OSC{k}" not in flags:
                return constant(inverted)
            period, clock_code, clock_hz, reason = oscillator(k)
            if period is None:
                return unknown(reason)
            timer_code, _, timer_reason = clock(_hd_timer_clock(t))
            if timer_code is None:
                return unknown(timer_reason)
            if timer_code != clock_code:
                return unknown(f"Timer #{t} (Clock {_hd_timer_clock(t)}) and oscillator #{k} (Clock {k}) "
                               "use different clock sources; timing not inferred.")
            high = width + 2
            phase = delay + 3 + (1 if trigger & 0x80 else 0)
            # Non-retriggerable timers can skip triggers. Do not guess the
            # resulting division ratio when a cycle cannot finish in one period.
            if delay + 3 + high >= period:
                return unknown(f"Timer #{t} delay + width reaches the trigger period; triggers may be skipped.")
        if inverted:
            phase, high = phase + high, period - high
        return {"kind": "Periodic", "period": period, "high": high, "phase": phase % period,
                "clock_hz": clock_hz, "clock_source": clock_code,
                "reason": "Periodic timing inferred from controller registers."}

    rows = []
    for channel in range(4):
        switch = switches.get(channel, {})
        row = {"channel": f"CH{channel}", "state": "Unknown", "frequency": "Unknown",
               "levels": "Unknown", "dwell": "Unknown", "duty": "Unknown", "relation": "—", "detail": ""}
        trigger_code = _optional_nonnegative_int(switch.get("trigger_source"))
        if (trigger_code is not None and trigger_code <= 0xFF and not trigger_code & 0x40
                and _HD_SIGNAL_TIMER_OUT <= trigger_code & 0x3F < _HD_SIGNAL_TIMER_RUN):
            row.update(source_pulser=(trigger_code & 0x3F) - _HD_SIGNAL_TIMER_OUT,
                       source_inverted=bool(trigger_code & 0x80))
        if snapshot.get("device_enabled") is not True or snapshot.get("main_state", {}).get("name") != "STATE_ON":
            state_name = snapshot.get("main_state", {}).get("name") or "unknown"
            enabled_text = "enabled" if snapshot.get("device_enabled") is True else "not enabled"
            signal = unknown(f"Device is not confirmed enabled and in STATE_ON ({state_name}, device "
                             f"{enabled_text}).", "Not enabled")
        elif flags is None:
            signal = unknown("Controller enable flags are unavailable.")
        elif not switch:
            signal = unknown("Switch routing readback is unavailable.")
        else:
            enable = constant(False) if "ST_ENB_SWITCH" not in flags else source(switch.get("enable_source"))
            if enable["kind"] == "Static" and not enable["value"]:
                signal = unknown("Switch enable is low: both branches are off. Hi-Z does not imply 0 V.", "Hi-Z")
                row.update(frequency="—", levels="Hi-Z", dwell="—", duty="—")
            elif enable["kind"] != "Static":
                signal = unknown("Switch enable is variable or unknown. " + enable["reason"], "Gated")
            else:
                signal = source(switch.get("trigger_source"))
                if signal["kind"] == "Static":
                    row.update(frequency="—", levels="Vpos" if signal["value"] else "Vneg", dwell="Continuous",
                               duty="100" if signal["value"] else "0")
                elif signal["kind"] == "Periodic":
                    clock_hz = signal["clock_hz"]
                    row.update(frequency=f"{clock_hz / signal['period'] / 1000:.6g} kHz", levels="Vneg ↔ Vpos",
                               duty=f"{100 * signal['high'] / signal['period']:.3g}")
                    coarse = switch.get("delay") if isinstance(switch.get("delay"), dict) else {}
                    fine = switch.get("fine_delay") if isinstance(switch.get("fine_delay"), dict) else {}
                    registers = [coarse.get("rise"), coarse.get("fall"), fine.get("rise"), fine.get("fall")]
                    valid_delays = (
                        all(isinstance(v, int) and not isinstance(v, bool) for v in registers)
                        and all(0 <= v < 16 for v in registers[:2])
                        and all(0 <= v < 512 for v in registers[2:])
                    )
                    if valid_delays:
                        # Edge compensation preserves the source period. Its
                        # ns/ps steps are NOT an exact phase measurement. Export
                        # registers so scans can freeze/check the waveform.
                        row["waveform"] = dict(
                            source=dict(signal), clock_hz=clock_hz,
                            trigger_source=switch.get("trigger_source"), enable_source=switch.get("enable_source"),
                            trigger_delay=dict(rise=registers[0], fall=registers[1]),
                            fine_delay=dict(rise=registers[2], fall=registers[3]),
                        )
                    if valid_delays and not any(registers):
                        row["dwell"] = (f"{signal['high'] / clock_hz * 1e6:.6g} / "
                                        f"{(signal['period'] - signal['high']) / clock_hz * 1e6:.6g}")
                        row["timing"] = signal
                    else:
                        row["dwell"] = "Delay adjusted"
                        row["duty"] = "≈" + row["duty"]
                        signal["reason"] += (" Switch edge delays are nonzero or unavailable; "
                                             "exact dwell/phase not inferred.")
        detail = (signal["reason"] + " Selected sources: trigger = "
                  + _hd_signal_text(switch.get("trigger_source")) + "; enable = "
                  + _hd_signal_text(switch.get("enable_source")) + ".")
        row.update(state=signal["kind"], detail=detail)
        rows.append(row)

    reference = next((row for row in rows if "timing" in row), None)
    if reference is not None:
        ref = reference["timing"]
        for row in rows:
            timing = row.get("timing")
            if timing is None or timing["period"] != ref["period"] or timing["clock_hz"] != ref["clock_hz"]:
                continue
            offset = (timing["phase"] - ref["phase"]) % ref["period"]
            if row is reference:
                row["relation"] = "Reference"
            elif offset == 0 and timing["high"] == ref["high"]:
                row["relation"] = f"Same as {reference['channel']}"
            elif offset == ref["high"] and timing["high"] == ref["period"] - ref["high"]:
                row["relation"] = f"Opposite to {reference['channel']}"
            else:
                row["relation"] = f"+{offset / ref['clock_hz'] * 1e6:.6g} µs vs {reference['channel']}"
    return rows


def _expected_oscillator_period(
    frequency_khz: float,
    *,
    clock_hz: float = 100e6,
    oscillator_offset: int = 2,
) -> int | None:
    """Return the period register written by the bundled AMX HD driver."""
    frequency_hz = float(frequency_khz) * 1000.0
    if not np.isfinite(frequency_hz) or frequency_hz <= 0.0:
        return None
    period = round((float(clock_hz) / frequency_hz) - int(oscillator_offset))
    return period if 1 <= period <= 0xFFFFFFFF else None


def _pulser_follows_oscillator(controller: Any, pulser: int) -> bool:
    """Only a timer triggered by oscillator #0, the frequency setting, runs at its period.

    Unknown routing (disconnected) counts as oscillator #0: ON reloads the config anyway.
    """
    routing = getattr(controller, "hd_routing", None) or {}
    trigger = _optional_nonnegative_int((routing.get("timer_triggers") or {}).get(pulser))
    return trigger is None or (trigger <= 0xFF and not trigger & 0x40 and trigger & 0x3F == _HD_SIGNAL_OSCILLATOR)


def _state_is_standby(state: Any) -> bool:
    normalized = _normalize_runtime_state(state).strip().upper()
    return normalized in {"ST_STANDBY", "STATE_STANDBY"}


def _interlock_function_text(function: bool | None) -> str:
    if function is None:
        return "n/a"
    return "ON" if function else "OFF (interlock input ignored)"


def _get_amx_driver_class() -> type[Any]:
    global _AMX_DRIVER_CLASS

    if _AMX_DRIVER_CLASS is not None:
        return _AMX_DRIVER_CLASS
    with _RUNTIME_LOAD_LOCK:
        if _AMX_DRIVER_CLASS is not None:
            return _AMX_DRIVER_CLASS

        plugin_dir = Path(__file__).resolve().parent
        bundled_runtime_dir = plugin_dir / "vendor" / _BUNDLED_RUNTIME_DIRNAME
        bundled_runtime_init = bundled_runtime_dir / "__init__.py"
        if not bundled_runtime_init.exists():
            raise ModuleNotFoundError(
                "Bundled AMX runtime not found in vendor/runtime; "
                "plugin installation is incomplete."
            )

        runtime_module_name = _bundled_runtime_module_name(plugin_dir)
        _load_private_runtime_package(runtime_module_name, bundled_runtime_dir)
        module = importlib.import_module(f"{runtime_module_name}.amx_hd")
        _AMX_DRIVER_CLASS = cast(type[Any], module.AMXHD)
        return _AMX_DRIVER_CLASS


# ---- crash resume: identical in every device plugin (tests/test_crash_resume.py) ----
# <Explorer config path>/<device>.session.json exists while the device is ON. A normal OFF or
# Explorer close removes it; after a crash, the next Explorer reconnects the device in resume
# mode: nothing is commanded, the hardware state is adopted and recording restarts.


def _session_token() -> str:
    """One token per Explorer process (a PID may be reused after a crash)."""
    token = getattr(sys, "_esibd_explorer_session_token", None)
    if token is None:
        import uuid
        token = uuid.uuid4().hex
        sys._esibd_explorer_session_token = token
    return token


def _session_file(device: Any) -> "Path | None":
    try:
        folder = device.pluginManager.Settings.configPath
        name = device.name
    except AttributeError:
        return None
    return Path(folder) / f"{name}.session.json" if folder else None


def _session_request(device: Any, on: bool) -> None:
    """An operator ON or OFF request. After an OFF or a disconnection (Explorer closing included),
    nothing is resumed, even if an unconfirmed shutdown brings the ON state back."""
    device._session_closed = not on
    _session_sync(device)


def _session_sync(device: Any) -> None:
    """Record the device as ON (with its port and recording state), or forget it. Never raises."""
    if not getattr(device, "_session_ready", False):
        return  # Until the resume decision at the end of finalizeInit, a crash record stays intact.
    path = _session_file(device)
    if path is None:
        return
    try:
        on = bool(device.isOn()) if hasattr(device, "onAction") else False
        if not on or getattr(device, "_session_closed", True):
            path.unlink(missing_ok=True)
            return
        import json
        import os
        record = dict(device=device.name, com=str(getattr(device, "com", "")), token=_session_token(),
                      recording=bool(getattr(device, "recording", False)), time=time.time())
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:  # noqa: BLE001 - a missing record only disables the resume
        pass


def _session_clear(device: Any) -> None:
    """Disconnection or Explorer closing: never resumed."""
    _session_request(device, False)


def _session_left_on(device: Any) -> "dict | None":
    """The record of an earlier Explorer process that stopped while this device was ON on this port."""
    path = _session_file(device)
    if path is None or not path.is_file():
        return None
    try:
        import json
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (not isinstance(record, dict) or record.get("token") == _session_token()
            or record.get("com") != str(getattr(device, "com", ""))):
        return None
    return record


def _session_start(device: Any) -> None:
    """End of finalizeInit: resume a device left ON by a crash, else start recording sessions."""
    record = _session_left_on(device)
    device._session_ready = True
    if record is None:
        _session_sync(device)  # Removes a stale record (other port, unreadable).
        return
    try:
        from PyQt6.QtCore import QTimer
    except ImportError:
        return
    QTimer.singleShot(1500, lambda: _session_resume(device, record))


def _session_resume(device: Any, record: dict) -> None:
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(record.get("time", 0))))
    device.print(f"Explorer stopped while {device.name} was ON (last record {when}). Reconnecting to resume: "
                 "the hardware state is adopted unchanged, nothing is switched or re-applied.", flag=PRINT.WARNING)
    controller = getattr(device, "controller", None)
    if controller is not None and hasattr(controller, "resume_session"):
        controller.resume_session = True  # Controllers that declare it connect without any command.
    device.setOn(True)
    if record.get("recording"):
        _session_restart_recording(device, time.monotonic() + 120.0)


def _session_restart_recording(device: Any, deadline: float) -> None:
    """Restart recording once the device accepts it (the plugins refuse it until really ON)."""
    if getattr(device, "recording", False) or time.monotonic() > deadline or not device.isOn():
        return
    controller = getattr(device, "controller", None)
    if getattr(controller, "initialized", False) and not getattr(controller, "resume_session", False):
        toggle = getattr(device, "toggleRecording", None)
        if callable(toggle):
            toggle(on=True, manual=False)
        if getattr(device, "recording", False):
            _session_sync(device)
            return
    try:
        from PyQt6.QtCore import QTimer
    except ImportError:
        return
    QTimer.singleShot(1000, lambda: _session_restart_recording(device, deadline))
# ---- end crash resume ----


def _require_native_backend(device):
    """The AMX HD is driven by its own Rust worker only; there is no Python DLL path in the plugin."""
    backend = _native_backend(device)
    if backend is None:
        raise RuntimeError("AMX HD communication requires its native worker.")
    return backend


def providePlugins() -> "list[type[Plugin]]":
    return [AMXHDDevice]


# The code shared word for word with the other AMX plugins, from this plugin folder's own copy of
# _runtime/_amx_common.py, run in this module as if written here (tools/sync_family_siblings.py keeps the
# copies equal). It is not at the folder root: Explorer imports every .py file there on its own.
_AMX_COMMON = Path(__file__).parent / "_runtime" / "_amx_common.py"
if not _AMX_COMMON.is_file():
    raise ModuleNotFoundError(f"Missing _runtime/{_AMX_COMMON.name} in {_AMX_COMMON.parent.parent}: "
                              "plugin installation is incomplete.")
exec(compile(_AMX_COMMON.read_text(encoding="utf-8"), str(_AMX_COMMON), "exec"), globals())  # noqa: S102


class AMXHDDevice(_AMXDeviceCommon, Device):
    """Drive AMX HD oscillator and timer timing from ESIBD Explorer."""

    LiveDisplay = _LiveDisplay  # Last 30 min by default.

    documentation = (
        "Drives AMX HD frequency and timer timing while monitoring timer readbacks."
    )

    name = "AMX_HD"
    version = "0.6.1"
    supportedVersion = "1.0.2"
    pluginType = PLUGINTYPE.INPUTDEVICE
    log = False  # Explorer's per-device Logging switch is removed: nothing here reads it.
    unit = "%"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "amx_hd.png"
    channels: "list[AMXHDChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    STARTUP_TIMEOUT = "Startup timeout (s)"
    POLL_TIMEOUT = "Poll timeout (s)"
    STANDBY_CONFIG = "Standby config"
    OPERATING_CONFIG = "Operating config"
    FREQUENCY_KHZ = "Frequency (kHz)"
    PSU_CH01 = "PSU for CH0-CH1"
    PSU_CH23 = "PSU for CH2-CH3"
    STATE = "State"
    DEVICE_ENABLED = "Device enabled"
    AVAILABLE_CONFIGS = "Available configs"
    LOADED_CONFIG = "Loaded config"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = AMXHDChannel

    def initGUI(self) -> None:
        super().initGUI()
        if hasattr(self, "initAction"):
            self.initAction.setVisible(False)
        if hasattr(self, "closeCommunicationAction"):
            shutdown_tooltip = f"Shutdown {self.name} and disconnect."
            with contextlib.suppress(TypeError):
                self.closeCommunicationAction.triggered.disconnect()
            self.closeCommunicationAction.triggered.connect(self.shutdownCommunication)
            self.closeCommunicationAction.setToolTip(shutdown_tooltip)
            self.closeCommunicationAction.setText(shutdown_tooltip)
            self.closeCommunicationAction.setVisible(False)
        self.controller = AMXHDController(controllerParent=self)

    def finalizeInit(self) -> None:
        super().finalizeInit()
        self._timing_basis_khz = _coerce_float(getattr(self, "frequency_khz", 2.0), 2.0)
        _session_start(self)  # Resumes a device left ON by an Explorer crash.
        if hasattr(self, "advancedAction"):
            self.advancedAction.toolTipFalse = (
                f"Show the timer cards, advanced channel table, equations, and layout actions for {self.name}."
            )
            self.advancedAction.toolTipTrue = (
                f"Hide the timer cards and advanced channel table and return to the {self.name} operator panel."
            )
            self.advancedAction.setToolTip(self.advancedAction.toolTipFalse)
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_config_controls()
        self._ensure_operator_panel()
        self._update_channel_column_visibility()
        self._update_channel_table_visibility()
        self._sync_acquisition_controls()

    def getChannels(self) -> "list[AMXHDChannel]":
        return cast("list[AMXHDChannel]", super().getChannels())

    com: int
    baudrate: int
    connect_timeout_s: float
    startup_timeout_s: float
    poll_timeout_s: float
    standby_config: int
    operating_config: int
    frequency_khz: float
    psu_ch01: str
    psu_ch23: str
    main_state: str
    device_enabled_state: str
    available_configs: list[dict[str, Any]]
    available_configs_text: str
    loaded_config_text: str

    def _config_setting_value(self, attr_name: str) -> int:
        setting_name = self._config_setting_name(attr_name)
        setting = self._setting(setting_name)
        if setting is not None:
            val = getattr(setting, "value", None)
            if val is not None:
                return _coerce_int(val, -1)
            # Setting exists but widget returned None: use backup, NOT the
            # property (which would read the same widget and be circular).
            return _coerce_int(getattr(self, f"_{attr_name}_backup", -1), -1)
        # No setting: use backup, then raw attribute (for tests).
        backup = getattr(self, f"_{attr_name}_backup", None)
        if backup is not None:
            return _coerce_int(backup, -1)
        return _coerce_int(getattr(self, attr_name, -1), -1)

    def _set_config_setting_value(self, attr_name: str, value: int) -> bool:
        value = _coerce_int(value, -1)
        backup_attr = f"_{attr_name}_backup"
        previous = _coerce_int(getattr(self, backup_attr, None), None)
        # Always store a backup so the value survives even if the setting
        # widget is unavailable or returns None on read.
        setattr(self, backup_attr, value)
        setting_name = self._config_setting_name(attr_name)
        setting = self._setting(setting_name)
        if setting is not None:
            if _coerce_int(getattr(setting, "value", None), value) == value:
                return previous != value
            setting.value = value
            return True
        if previous == value:
            return False
        return True

    def _loaded_config_tooltip_text(self) -> str:
        lines = [
            "AMX config currently reported in controller memory.",
            f"Loaded: {getattr(self, 'loaded_config_text', '') or 'n/a'}",
            "Interlock function: "
            f"{_interlock_function_text(getattr(getattr(self, 'controller', None), 'interlock_function', None))}"
            " (InterlockFunct of the loaded config, read only; pins 3 and 5 of the output connectors)",
        ]
        available = str(getattr(self, "available_configs_text", "") or "").strip()
        if available:
            lines.append(f"Available: {available}")
        return "\n".join(lines)

    def _ensure_config_controls(self) -> None:
        if (
            getattr(self, "titleBar", None) is None
            or getattr(self, "titleBarLabel", None) is None
            or hasattr(self, "operatingConfigCombo")
        ):
            return

        label_type = type(self.titleBarLabel)
        insert_before = getattr(self, "stretchAction", None)

        self.operatingConfigLabel = label_type("Signal:")
        self.operatingConfigCombo = self._create_config_selector_widget()
        self.loadOperatingConfigButton = self._create_config_button_widget("Load now")
        self.frequencyLabel = label_type("Freq:")
        self.frequencyWidget = self._create_frequency_widget()

        self._connect_config_selector(self.operatingConfigCombo, "operating_config")
        self._connect_config_button(self.loadOperatingConfigButton, self.loadOperatingConfigNow)
        self._connect_frequency_widget(self.frequencyWidget)

        widgets = (
            self.operatingConfigLabel,
            self.operatingConfigCombo,
            self.loadOperatingConfigButton,
            self.frequencyLabel,
            self.frequencyWidget,
        )
        for widget in widgets:
            if insert_before is not None and hasattr(self.titleBar, "insertWidget"):
                self.titleBar.insertWidget(insert_before, widget)
            elif hasattr(self.titleBar, "addWidget"):
                self.titleBar.addWidget(widget)

        self._update_config_controls()

    def _update_config_controls(self) -> None:
        setting = self._setting(self.FREQUENCY_KHZ)
        spin = getattr(setting, "spin", None)
        if spin is not None:
            spin.setKeyboardTracking(False)
        self._update_config_selector(getattr(self, "standbyConfigCombo", None), "standby_config")
        self._update_config_selector(getattr(self, "operatingConfigCombo", None), "operating_config")
        signal_label = getattr(self, "operatingConfigLabel", None)
        signal_tooltip = self._config_selector_tooltip_text("operating_config")
        if signal_label is not None and hasattr(signal_label, "setToolTip"):
            signal_label.setToolTip(signal_tooltip)

        button = getattr(self, "loadOperatingConfigButton", None)
        ready, reason = self._load_operating_now_ready()
        pending = self._config_load_pending()
        self._set_action_enabled(button, ready)
        if button is not None and hasattr(button, "setToolTip"):
            tooltip = (
                "Load the selected AMX config and adopt its saved timing in the interface. "
                "This action is only available while the AMX is ON."
            )
            if not ready and reason:
                tooltip = f"{tooltip}\nCurrently unavailable: {reason}."
            if pending:
                tooltip = f"{tooltip}\nThe selected config is not the loaded one: {self.loaded_config_text}."
            button.setToolTip(tooltip)
        if button is not None and hasattr(button, "setStyleSheet"):
            button.setStyleSheet(_AMX_LOAD_PENDING_STYLE if pending else "")

        frequency_widget = getattr(self, "frequencyWidget", None)
        if frequency_widget is not None:
            block_signals = getattr(frequency_widget, "blockSignals", None)
            if callable(block_signals):
                block_signals(True)
            try:
                set_value = getattr(frequency_widget, "setValue", None)
                if callable(set_value) and not getattr(frequency_widget, "hasFocus", lambda: False)():
                    set_value(float(getattr(self, "frequency_khz", 2.0)))
            finally:
                if callable(block_signals):
                    block_signals(False)
            frequency_tooltip = (
                "Oscillator frequency applied to the selected AMX signal. "
                "Confirm a typed value with Enter, Tab, or by leaving the field."
            )
            if hasattr(frequency_widget, "setToolTip"):
                frequency_widget.setToolTip(frequency_tooltip)
            frequency_label = getattr(self, "frequencyLabel", None)
            if frequency_label is not None and hasattr(frequency_label, "setToolTip"):
                frequency_label.setToolTip(frequency_tooltip)

    def _status_tooltip_text(self) -> str:
        """Return the full AMX status tooltip for the toolbar widgets."""
        controller = getattr(self, "controller", None)
        display_state = self._display_main_state()
        hardware_state = _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
        display_faults = self._display_device_state_summary()
        hardware_faults = self._raw_device_state_summary() or "n/a"
        lines = [f"State: {display_state}"]
        if display_state != hardware_state:
            lines.append(f"Hardware state: {hardware_state}")
        lines.append(f"Device enabled: {getattr(self, 'device_enabled_state', '') or 'Unknown'}")
        lines.append(f"Faults: {display_faults}")
        if display_faults != hardware_faults:
            lines.append(f"Hardware flags: {hardware_faults}")
        lines.extend(
            (
                f"Controller: {getattr(controller, 'controller_state_summary', '') or 'n/a'}",
                f"Configs: {getattr(self, 'available_configs_text', '') or 'n/a'}",
                f"Loaded: {getattr(self, 'loaded_config_text', '') or 'n/a'}",
                f"Interlock function: {_interlock_function_text(getattr(controller, 'interlock_function', None))}",
            )
        )
        return "\n".join(lines)

    def _channel_by_pulser(self, pulser: int) -> "AMXHDChannel | None":
        for channel in self.getChannels():
            if channel.real and channel.pulser_number() == int(pulser):
                return channel
        return None

    def _pulser_panel_snapshot(self, pulser: int) -> dict[str, Any]:
        """Return one operator-card snapshot without touching hardware or Qt."""
        channel = self._channel_by_pulser(pulser)
        controller = getattr(self, "controller", None)
        device = getattr(controller, "device", None) if controller is not None else None
        connected = bool(
            controller is not None
            and device is not None
            and getattr(controller, "initialized", False)
        )
        busy = bool(
            getattr(self, "loading", False)
            or getattr(controller, "initializing", False)
            or getattr(controller, "transitioning", False)
        )

        requested_enabled = bool(
            self._channel_parameter_value(
                channel,
                getattr(channel, "ENABLED", "Enabled"),
                "enabled",
                False,
            )
        ) if channel is not None else False
        manual = bool(
            self._channel_parameter_value(
                channel,
                getattr(channel, "ACTIVE", "Active"),
                "active",
                True,
            )
        ) if channel is not None else True
        display = bool(
            self._channel_parameter_value(
                channel,
                getattr(channel, "DISPLAY", "Display"),
                "display",
                False,
            )
        ) if channel is not None else False
        requested_width_us = _coerce_float(
            self._channel_parameter_value(
                channel,
                getattr(channel, "VALUE", "Value"),
                "value",
                0.0,
            ) if channel is not None else 0.0,
            0.0,
        )

        clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
        oscillator_offset = _coerce_int(
            _safe_device_attr(device, "OSC_OFFSET", 2),
            2,
        )
        width_offset = _coerce_int(
            _safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2),
            2,
        )
        frequency_khz = _coerce_float(getattr(self, "frequency_khz", 2.0), 2.0)
        expected_period = _expected_oscillator_period(
            frequency_khz,
            clock_hz=clock_hz,
            oscillator_offset=oscillator_offset,
        )
        expected_width = _expected_width_register(
            requested_enabled,
            requested_width_us,
            clock_hz=clock_hz,
            width_offset=width_offset,
        )
        minimum_width_clamped = bool(
            requested_enabled
            and requested_width_us > 0.0
            and round(requested_width_us * clock_hz / 1e6) - width_offset < 1
        )

        observed_period = (
            _optional_nonnegative_int(getattr(controller, "oscillator_period", None))
            if connected
            else None
        )
        width_values = getattr(controller, "width_values", {}) or {}
        observed_width = (
            _optional_nonnegative_int(width_values.get(pulser))
            if connected
            else None
        )
        duty_values = getattr(controller, "values", {}) or {}
        observed_duty = _coerce_float(duty_values.get(pulser, np.nan), np.nan)
        delay_values = getattr(controller, "delay_values", {}) or {}
        burst_values = getattr(controller, "burst_values", {}) or {}

        hardware_on = bool(
            connected
            and _state_is_on(getattr(controller, "main_state", ""))
            and str(getattr(controller, "device_enabled_state", "")).upper() == "ON"
        )
        display_state = self._display_main_state()
        if channel is None and connected:
            status_text = "UNAVAILABLE"
            status_kind = "error"
        elif not connected:
            status_text = "DISCONNECTED"
            status_kind = "disconnected"
        elif busy:
            status_text = "BUSY"
            status_kind = "warning"
        elif _status_requires_operator_attention(display_state):
            status_text = "DEVICE FAULT"
            status_kind = "error"
        elif not hardware_on:
            normalized_state = display_state.lower()
            status_text = (
                "STANDBY"
                if "standby" in normalized_state or "stby" in normalized_state
                else "OFF"
            )
            status_kind = "warning" if status_text == "STANDBY" else "off"
        elif observed_width is None:
            status_text = "WAITING READBACK"
            status_kind = "warning"
        elif expected_width == 0:
            if observed_width != 0:
                status_text = "REGISTER MISMATCH"
                status_kind = "warning"
            elif requested_enabled:
                status_text = "ZERO WIDTH"
                status_kind = "warning"
            else:
                status_text = "OFF CONFIRMED"
                status_kind = "off"
        elif observed_period is None:
            status_text = "WAITING READBACK"
            status_kind = "warning"
        elif observed_width == expected_width and observed_period == expected_period:
            if minimum_width_clamped:
                status_text = "MINIMUM WIDTH"
                status_kind = "warning"
            else:
                status_text = "REGISTERS APPLIED"
                status_kind = "active"
        else:
            status_text = "REGISTER MISMATCH"
            status_kind = "warning"

        requested_duty = _requested_duty_percent(self, requested_width_us)
        if observed_width is None:
            width_register_text = "n/a"
            duty_register_text = "n/a"
        elif observed_width == 0:
            width_register_text = "0 ticks (stopped)"
            duty_register_text = "0.000 % (stopped)"
        else:
            observed_width_us = _width_register_us(
                observed_width,
                clock_hz=clock_hz,
                width_offset=width_offset,
            )
            width_register_text = f"{observed_width_us:.3f} µs ({observed_width} ticks)"
            duty_register_text = (
                f"{observed_duty:.3f} %"
                if np.isfinite(observed_duty)
                else "n/a"
            )

        routing = getattr(controller, "hd_routing", None) if connected else None
        routing = routing if isinstance(routing, dict) else {}
        trigger_register_text = (
            _hd_signal_text((routing.get("timer_triggers") or {}).get(pulser))
            if pulser in (routing.get("timer_triggers") or {})
            else "n/a"
        )
        clock_index = _hd_timer_clock(pulser)
        clock_sources = list(routing.get("clock_sources") or [])
        clock_register_text = (
            f"Clock {clock_index} = {_hd_clock_text(clock_sources[clock_index])}"
            if clock_index < len(clock_sources)
            else f"Clock {clock_index} = n/a"
        )
        delay = str(delay_values.get(pulser, "n/a") or "n/a") if connected else "n/a"
        delay_text = f"{delay} µs" if delay != "n/a" else delay
        burst_text = str(burst_values.get(pulser, "n/a") or "n/a") if connected else "n/a"
        expected_period_text = "n/a" if expected_period is None else str(expected_period)
        observed_period_text = "n/a" if observed_period is None else str(observed_period)
        observed_width_text = "n/a" if observed_width is None else str(observed_width)
        channel_name = str(getattr(channel, "name", f"T{pulser}") or f"T{pulser}")
        tooltip = "\n".join(
            (
                f"{channel_name} register verification",
                f"Requested: {'enabled' if requested_enabled else 'disabled'}, "
                f"width {requested_width_us:.3f} µs",
                f"Expected / observed width register: {expected_width} / {observed_width_text}",
                f"Expected / observed oscillator period: {expected_period_text} / {observed_period_text}",
                f"Trigger / clock: {trigger_register_text}; {clock_register_text}",
                "Width/duty conversions assume this timer's clock and oscillator 0 run at 100 MHz.",
                "Readbacks come from AMX HD controller registers; they do not prove the physical waveform.",
            )
        )
        controls_enabled = channel is not None and not busy and not (
            connected and controller.timing_block_reason(pulser))
        outputs = (getattr(controller, "pulser_outputs", None) or {}) if connected else {}
        if not connected or (not outputs and getattr(controller, "output_rows", None) is None):
            drives_text = "n/a"
        elif pulser not in outputs:
            drives_text = "none"
        else:
            direct = ", ".join(f"CH{index}" for index, inverted in outputs[pulser] if not inverted)
            inverted = ", ".join(f"CH{index}" for index, inverted in outputs[pulser] if inverted)
            drives_text = " · ".join(part for part in (direct, f"inverted {inverted}" if inverted else "") if part)
        return {
            "title": f"Timer {pulser}",
            "drives_text": drives_text,
            "drives_outputs": pulser in outputs,
            "status_text": status_text,
            "status_kind": status_kind,
            "status_style": _amx_panel_badge_style(status_kind),
            "requested_enabled": requested_enabled,
            "enable_text": "ENABLED" if requested_enabled else "DISABLED",
            "enable_style": (
                _AMX_PANEL_ENABLE_ACTIVE_STYLE
                if requested_enabled
                else _AMX_PANEL_ENABLE_OFF_STYLE
            ),
            "requested_width_us": requested_width_us,
            "maximum_width_us": max(self._maximum_width_us(), requested_width_us),
            "requested_duty": requested_duty,
            "requested_duty_text": f"{requested_duty:.3f} %",
            "keep": getattr(channel, "keep", _AMX_KEEP_DUTY) if channel is not None else _AMX_KEEP_DUTY,
            "mode_text": "MANUAL" if manual else "EQUATION",
            "display_checked": display,
            "controls_enabled": controls_enabled,
            "width_enabled": controls_enabled and manual,
            "display_enabled": channel is not None,
            "width_register_text": width_register_text,
            "duty_register_text": duty_register_text,
            "delay_register_text": delay_text,
            "burst_register_text": burst_text,
            "trigger_register_text": trigger_register_text,
            "clock_register_text": clock_register_text,
            "tooltip": tooltip,
        }

    def _ensure_operator_panel(self) -> None:
        """Outputs, then one timing row per used timer; diagnostics in Advanced."""
        if hasattr(self, "amxPanel"):
            self._update_operator_panel()
            return
        if not callable(getattr(self, "addContentWidget", None)):
            return
        try:
            from PyQt6.QtCore import Qt, QTimer
            from PyQt6.QtWidgets import (
                QAbstractSpinBox,
                QComboBox,
                QHeaderView,
                QTableWidget,
                QTableWidgetItem,
                QDoubleSpinBox,
                QLabel,
                QPushButton,
                QVBoxLayout,
                QWidget,
            )
        except ImportError:
            return

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        def role_label(text: str, role: str, *, wrap: bool = True) -> QLabel:
            widget = QLabel(text)
            widget.setProperty("amxRole", role)
            widget.setWordWrap(wrap)
            return widget

        self.amxPanelSignalValue = role_label("Loaded config: n/a", "value")
        self.amxPanelPendingValue = role_label("", "warning")
        self.amxPanelPendingValue.setVisible(False)
        self.amxPanelFrequencyValue = role_label("Oscillator 0: n/a", "value")
        layout.addWidget(self.amxPanelSignalValue)
        layout.addWidget(self.amxPanelPendingValue)
        layout.addWidget(self.amxPanelFrequencyValue)
        self.amxPanelClockNotice = role_label("", "warning")
        self.amxPanelClockNotice.setVisible(False)
        layout.addWidget(self.amxPanelClockNotice)

        table = QTableWidget(4, len(_AMX_OUTPUT_COLUMNS))
        table.setHorizontalHeaderLabels([header for _, header in _AMX_OUTPUT_COLUMNS])
        for column, (key, _header) in enumerate(_AMX_OUTPUT_COLUMNS):
            table.horizontalHeaderItem(column).setToolTip(_AMX_OUTPUT_COLUMN_TIPS[key])
        for row in range(4):
            for column in range(len(_AMX_OUTPUT_COLUMNS)):
                table.setItem(row, column, QTableWidgetItem(f"CH{row}" if column == 0 else "—"))
        _configure_panel_table(table)
        table.setToolTip(
            "CH0–CH3 are the controller's switch channels, decoded from the live trigger/enable "
            "sources, timers, oscillators and clocks (CGC 4EDH signal table). "
            "Levels below assume dual-level switches. Hi-Z never implies a discharged output.\n"
            + _AMX_PANEL_NOTICE
        )
        self.amxOutputTable = table
        layout.addWidget(table)

        # One row per timer; the operator view hides the unused ones.
        timing = QTableWidget(len(_AMX_PULSER_IDS), len(_AMX_TIMING_COLUMNS))
        timing.setHorizontalHeaderLabels([header for _, header in _AMX_TIMING_COLUMNS])
        for column, (key, _header) in enumerate(_AMX_TIMING_COLUMNS):
            timing.horizontalHeaderItem(column).setToolTip(_AMX_TIMING_COLUMN_TIPS[key])
        self.amxTimingRows: dict[int, dict[str, Any]] = {}
        for row, pulser in enumerate(_AMX_PULSER_IDS):
            items = {key: QTableWidgetItem("—") for key in ("name", "drives", "status")}
            items["name"].setText(f"T{pulser}")
            for key, column in (("name", 0), ("drives", 1), ("status", 6)):
                timing.setItem(row, column, items[key])
            enable_button = QPushButton(_AMX_PULSER_OFF_LABEL)
            enable_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            enable_button.setCheckable(True)
            enable_button.setToolTip(
                "Enable or stop this timer while AMX HD is ON. Loading a config replaces this request. "
                "Timer numbers are not output channel numbers."
            )
            width_widget = QDoubleSpinBox()
            width_widget.setRange(0.0, 1_000_000.0)
            width_widget.setDecimals(3)
            width_widget.setSingleStep(0.01)
            width_widget.setSuffix(" µs")
            duty_widget = QDoubleSpinBox()
            duty_widget.setRange(0.0, 100.0)
            duty_widget.setDecimals(2)
            duty_widget.setSingleStep(0.5)
            duty_widget.setSuffix(" %")
            for spin in (width_widget, duty_widget):
                spin.setKeyboardTracking(False)
                spin.setAccelerated(True)
                spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)  # Like Explorer's own fields; ↑/↓ keys still step.
                _disable_spinbox_wheel(spin)
            keep_widget = QComboBox()
            keep_widget.addItem("duty %", _AMX_KEEP_DUTY)
            keep_widget.addItem("width µs", _AMX_KEEP_WIDTH)
            keep_widget.setToolTip(_AMX_KEEP_TOOLTIP)
            _disable_spinbox_wheel(keep_widget)
            for column, widget in ((2, enable_button), (3, width_widget), (4, duty_widget), (5, keep_widget)):
                timing.setCellWidget(row, column, widget)
            self.amxTimingRows[pulser] = {
                "row": row, **items, "enable": enable_button, "width": width_widget,
                "duty": duty_widget, "keep": keep_widget,
            }
            enable_button.toggled.connect(
                lambda checked, pulser=pulser: self._panel_enabled_toggled(pulser, checked))
            width_widget.valueChanged.connect(
                lambda value, pulser=pulser: self._panel_width_changed(pulser, value))
            duty_widget.valueChanged.connect(
                lambda value, pulser=pulser: self._panel_duty_changed(pulser, value))
            keep_widget.currentIndexChanged.connect(
                lambda _index, pulser=pulser, combo=keep_widget: self._panel_keep_changed(
                    pulser, combo.currentData()))
        _configure_panel_table(timing)
        # The routing text takes the spare width; the editors keep their size.
        timing.horizontalHeader().setStretchLastSection(False)
        timing.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.amxTimingTable = self.amxPulserControls = timing
        layout.addWidget(timing)
        self.amxTimingHiddenNote = role_label("", "notice")
        layout.addWidget(self.amxTimingHiddenNote)

        # Advanced: the notice, the expected-switching sketch and the channel table.
        self.amxPanelNotice = role_label(_AMX_PANEL_NOTICE, "notice")
        layout.addWidget(self.amxPanelNotice)
        self.amxWaveformLabel = role_label("Expected switching over two periods (from controller registers)", "name")
        layout.addWidget(self.amxWaveformLabel)
        self.amxWaveformView = _create_waveform_view()
        self.amxWaveformView.setToolTip(
            "Expected levels from the controller registers, not a measurement: edge delays, "
            "rise times, loads and external offsets are not shown. + is Vpos, − is Vneg."
        )
        layout.addWidget(self.amxWaveformView)
        # PSU changes and stale readings must reach the view even if AMX polling
        # has stopped. This only reads cached data; the panel owns the Qt timer.
        self.amxOutputRefreshTimer = QTimer(panel)
        self.amxOutputRefreshTimer.setInterval(500)
        self.amxOutputRefreshTimer.timeout.connect(self._update_output_table)
        self.amxOutputRefreshTimer.start()
        # Keep the Advanced table in the same scroll area: two separate views
        # would impose their combined minimum height on the entire dock group.
        tree = getattr(self, "tree", None)
        if isinstance(tree, QWidget):
            layout.addWidget(tree)
        layout.addStretch(1)

        self.amxPanel = panel
        self.addContentWidget(_scrollable_panel(panel))
        self._update_channel_table_visibility()
        self._apply_panel_theme()

    def _update_operator_panel(self) -> None:
        self._update_output_table()
        rows = getattr(self, "amxTimingRows", None)
        if not isinstance(rows, dict):
            return
        controller = getattr(self, "controller", None)
        connected = bool(
            controller is not None
            and getattr(controller, "device", None) is not None
            and getattr(controller, "initialized", False)
        )
        dark = getattr(self, "_amx_panel_dark", True)
        loaded_config = str(getattr(self, "loaded_config_text", "") or "n/a")
        signal_label = getattr(self, "amxPanelSignalValue", None)
        if signal_label is not None:
            edited = " · timing edited since load" if getattr(self, "_timing_edited", False) else ""
            function = getattr(controller, "interlock_function", None) if connected else None
            interlock = "" if function is None else f" · interlock function {'ON' if function else 'OFF'}"
            signal_label.setText(f"Loaded config: {loaded_config}{interlock}{edited}")
            signal_label.setToolTip(self._loaded_config_tooltip_text())
        pending_label = getattr(self, "amxPanelPendingValue", None)
        if pending_label is not None:
            pending = self._config_load_pending()
            if pending:
                selected = self._config_setting_value("operating_config")
                names = dict(self._config_selector_entries("operating_config"))
                pending_label.setText(
                    f"Selected {names.get(selected, selected)} is not loaded: press Load now to apply it."
                )
            pending_label.setVisible(pending)

        frequency_request = _coerce_float(getattr(self, "frequency_khz", 2.0), 2.0)
        frequency_readback = _coerce_float(
            getattr(controller, "frequency_readback_khz", np.nan),
            np.nan,
        )
        frequency_label = getattr(self, "amxPanelFrequencyValue", None)
        if frequency_label is not None:
            known = connected and np.isfinite(frequency_readback)
            blocked = controller.timing_block_reason() if connected else ""
            frequency_widget = getattr(self, "frequencyWidget", None)
            if frequency_widget is not None and hasattr(frequency_widget, "setEnabled"):
                frequency_widget.setEnabled(not blocked)
            clock_hz, osc_offset, _, _ = self._timing_constants()
            expected_period = _expected_oscillator_period(
                frequency_request, clock_hz=clock_hz, oscillator_offset=osc_offset)
            observed_period = _optional_nonnegative_int(getattr(controller, "oscillator_period", None))
            differs = known and observed_period is not None and expected_period != observed_period
            parts = [f"Oscillator 0: requested {frequency_request:.3f} kHz"]
            if expected_period is not None:
                ticks = expected_period + osc_offset
                # The period is a whole number of clock ticks: show what the hardware runs.
                parts[0] += f" → {clock_hz / ticks / 1000:.7g} kHz ({ticks} × {1e9 / clock_hz:g} ns)"
                if ticks % 2:
                    parts.append(f"odd period: 50 % is {ticks // 2} or {ticks // 2 + 1} ticks")
            if differs:
                parts.append(f"register {frequency_readback:.3f} kHz (not applied)")
            elif known:
                parts.append("applied")
            if blocked:
                parts = [f"Oscillator 0: {blocked}; Freq has no effect"]
            frequency_label.setText(" · ".join(parts))
            _amx_set_role(frequency_label, "warning" if differs or blocked else "value")
            frequency_label.setToolTip(
                "Requested oscillator-0 frequency, the frequency it runs on the clock grid and "
                "whether the controller period register holds it, assuming Clock 0 = 100 MHz."
            )
        clock_notice = getattr(self, "amxPanelClockNotice", None)
        if clock_notice is not None:
            routing = getattr(controller, "hd_routing", None) if connected else None
            clock_sources = list((routing or {}).get("clock_sources") or []) if isinstance(routing, dict) else []
            clock0 = _optional_nonnegative_int(clock_sources[0]) if clock_sources else None
            mismatch = clock0 is not None and clock0 & 0x0F != _HD_CLOCK_100M
            clock_notice.setVisible(mismatch)
            clock_notice.setText(
                f"Clock 0 is {_hd_clock_text(clock0)}, not CLK100M: Freq and the widths on this clock "
                "cannot be edited here. Use the CH0–CH3 table for the decoded timing."
                if mismatch else ""
            )

        advanced = bool(getattr(getattr(self, "advancedAction", None), "state", False))
        snapshots = {pulser: self._pulser_panel_snapshot(pulser) for pulser in rows}
        used = {pulser for pulser, snapshot in snapshots.items()
                if snapshot.get("drives_outputs") or snapshot["requested_enabled"]}
        shown = set(rows) if advanced or not used else used
        for pulser, widgets in rows.items():
            snapshot = snapshots[pulser]
            tooltip = (f"{snapshot['status_text']} · {snapshot['mode_text']}\n{snapshot['tooltip']}\n"
                       f"Trigger: {snapshot['trigger_register_text']} · clock: {snapshot['clock_register_text']}")
            for key, text in (("name", f"T{pulser}"), ("drives", snapshot.get("drives_text", "")),
                              ("status", snapshot["status_text"])):
                item = widgets.get(key)
                if item is not None and hasattr(item, "setText"):
                    item.setText(str(text))
                    item.setToolTip(tooltip)
            for key in ("name", "status"):
                _color_table_item(widgets.get(key), _AMX_STATUS_ROLES.get(snapshot["status_kind"]), dark)
            enable = widgets.get("enable")
            self._set_panel_checked(
                enable,
                checked=bool(snapshot["requested_enabled"]),
                enabled=bool(snapshot["controls_enabled"]),
            )
            if enable is not None:
                enable.setText(_AMX_PULSER_ON_LABEL if snapshot["requested_enabled"] else _AMX_PULSER_OFF_LABEL)
                enable.setStyleSheet(snapshot["enable_style"])
            equation = "" if snapshot["mode_text"] == "MANUAL" else " Set by the channel equation (Advanced)."
            for key, value, maximum in (
                ("width", snapshot["requested_width_us"], snapshot["maximum_width_us"]),
                ("duty", snapshot.get("requested_duty", 0.0), 100.0),
            ):
                self._set_panel_width(widgets.get(key), value=float(value), maximum=float(maximum),
                                      enabled=bool(snapshot["width_enabled"]))
                widget = widgets.get(key)
                if widget is not None and hasattr(widget, "setToolTip"):
                    widget.setToolTip(_AMX_TIMING_COLUMN_TIPS[key] + equation)
            keep = widgets.get("keep")
            if keep is not None and hasattr(keep, "findData"):
                blocked = keep.blockSignals(True)
                try:
                    keep.setCurrentIndex(max(keep.findData(snapshot.get("keep", _AMX_KEEP_DUTY)), 0))
                    keep.setEnabled(bool(snapshot["controls_enabled"]))
                finally:
                    keep.blockSignals(blocked)
        table = getattr(self, "amxTimingTable", None)
        if table is not None:
            for pulser, widgets in rows.items():
                table.setRowHidden(widgets["row"], pulser not in shown)
            table.setColumnHidden(len(_AMX_TIMING_COLUMNS) - 1, not advanced)
            _fit_table_height(table)
        note = getattr(self, "amxTimingHiddenNote", None)
        if note is not None:
            hidden = [f"T{pulser}" for pulser in rows if pulser not in shown]
            note.setText(f"{', '.join(hidden)}: off and driving no output; see Advanced." if hidden else "")
            note.setVisible(bool(hidden))

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings.pop(f"{self.name}/Logging", None)  # Logs always go to <Data path>/logs.
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=1,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the AMX controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Baud rate passed to cgc.amx.AMX.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=1.0,
            maximum=60.0,
            toolTip="Timeout in seconds used to connect and validate the AMX transport.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.STARTUP_TIMEOUT}"] = parameterDict(
            value=10.0,
            minimum=1.0,
            maximum=120.0,
            toolTip="Timeout in seconds used for AMX startup and shutdown sequences.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="startup_timeout_s",
        )
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=0.5,
            maximum=30.0,
            toolTip="Timeout in seconds used to poll AMX housekeeping.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
        )
        settings[f"{self.name}/{self.OPERATING_CONFIG}"] = parameterDict(
            value=-1,
            minimum=-1,
            maximum=_HD_MAX_CONFIG_INDEX,
            toolTip=(
                "AMX config used for normal operation. Use -1 to connect "
                "without enabling the AMX until a valid config is selected."
            ),
            parameterType=PARAMETERTYPE.INT,
            attr="operating_config",
            event=self._update_config_controls,
        )
        settings[f"{self.name}/{self.FREQUENCY_KHZ}"] = parameterDict(
            value=2.0,
            minimum=0.001,
            maximum=10000.0,
            toolTip="Oscillator frequency in kilohertz. Load now replaces this value with the config's saved frequency; ON brings back the last one set.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="frequency_khz",
            event=self.frequencyChanged,
        )
        for label, attr in ((self.PSU_CH01, "psu_ch01"), (self.PSU_CH23, "psu_ch23")):
            settings[f"{self.name}/{label}"] = parameterDict(
                value="None",
                items="None,PSU_A,PSU_B,PSU_C,PSU_D,PSU_E",
                fixedItems=True,
                parameterType=PARAMETERTYPE.COMBO,
                attr=attr,
                event=self._update_output_table,
                toolTip=(
                    "PSU physically feeding this AMX HD output pair. Read-only association: "
                    "PSU CH0 (+) supplies Vpos; CH1 (−) supplies Vneg. "
                    "Uses measured voltages relative to the PSU reference, not to earth; "
                    "never connects or controls the PSU. Missing, OFF or stale PSU readings "
                    "leave the rail voltage unavailable."
                ),
            )
        settings[f"{self.name}/{self.STATE}"] = parameterDict(
            value="Disconnected",
            toolTip="Latest AMX controller state reported by the driver.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="main_state",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.DEVICE_ENABLED}"] = parameterDict(
            value="OFF",
            toolTip="Latest AMX device-enable state.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="device_enabled_state",
            indicator=True,
            internal=True,
            advanced=True,
            restore=False,
        )
        settings[f"{self.name}/{self.AVAILABLE_CONFIGS}"] = parameterDict(
            value="n/a",
            toolTip=(
                "AMX configuration slots reported by the controller after connect. "
                "Use these indices to select the operator-facing Signal config."
            ),
            parameterType=PARAMETERTYPE.LABEL,
            attr="available_configs_text",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.LOADED_CONFIG}"] = parameterDict(
            value="n/a",
            toolTip="AMX config currently reported in controller memory.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="loaded_config_text",
            indicator=True,
            internal=True,
            advanced=True,
            restore=False,
        )
        settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

    def _follow_timing_basis(self) -> None:
        """Pulsers keeping % follow a frequency edit; the widths refer to the previous one."""
        current = _coerce_float(getattr(self, "frequency_khz", np.nan), np.nan)
        if not np.isfinite(current) or current <= 0.0:
            return
        previous = getattr(self, "_timing_basis_khz", None)
        self._timing_basis_khz = current
        if previous is None or previous == current or getattr(self, "loading", False):
            return  # Loaded settings and configs already belong together.
        controller = getattr(self, "controller", None)
        if controller is not None and controller.timing_block_reason():
            return  # Refused at the apply: the widths must not follow it.
        self._timing_edited = True
        self._keep_duty_cycles(previous, current)

    def _acquisition_readiness(self) -> tuple[bool, str]:
        """Return whether manual recording can start and, if not, why."""
        controller = getattr(self, "controller", None)
        if controller is None:
            return False, "controller unavailable"
        if getattr(controller, "device", None) is None:
            return False, "device disconnected"
        if getattr(controller, "initializing", False):
            return False, "initialization in progress"
        if not getattr(controller, "initialized", False):
            return False, "communication not initialized"
        if getattr(controller, "transitioning", False):
            return False, "ON/OFF transition in progress"
        is_on = getattr(self, "isOn", None)
        if not callable(is_on) or not bool(is_on()):
            return False, "device is OFF"
        if not controller._device_active():
            main_state = str(getattr(controller, "main_state", "Disconnected") or "Disconnected")
            return False, f"state is {main_state}"
        return True, ""


class AMXHDChannel(_AMXChannelCommon, Channel):
    """AMX pulser channel definition."""

    ID = "Pulser"
    DELAY_US = "Delay us"
    WIDTH_TICKS = "Width ticks"
    BURST = "Burst"
    DUTY = "Duty"
    FREQ_KHZ = "Freq kHz"
    KEEP = "Keep"
    channelParent: AMXHDDevice

    def _apply_value_now(self) -> None:
        apply_value = getattr(self, "applyValue", None)
        if not callable(apply_value) or getattr(self.channelParent, "loading", False):
            return
        controller = getattr(getattr(self, "channelParent", None), "controller", None)
        apply_thread = getattr(controller, "applyValueFromThread", None)
        if callable(apply_thread):
            apply_thread(self, parallel=True)
            return
        apply_value(apply=True)


class AMXHDController(_AMXControllerCommon, DeviceController):
    """AMX hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: AMXHDDevice

    # Set by a crash-resume record: connect without any command and adopt the hardware state.
    resume_session = False

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.main_state = "Disconnected"
        self.device_enabled_state = "OFF"
        self.device_state_summary = "n/a"
        self.controller_state_summary = "n/a"
        self.available_configs: list[dict[str, Any]] = []
        self.available_configs_text = "n/a"
        self.loaded_config_text = "n/a"
        self.interlock_function: bool | None = None
        self.initialized = False
        self._initial_open_close_requested = False
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._global_apply_state_lock = Lock()
        self._global_apply_pending = False
        self._global_apply_worker_running = False
        self._channel_apply_state_lock = Lock()
        self._channel_apply_pending: dict[int, AMXHDChannel] = {}
        self._channel_apply_worker_running = False
        self._consecutive_poll_errors = 0
        self._native_cancel_event = Event()
        self._native_transport_lost = False
        # COM port (if any) whose transport was poisoned by a timed-out DLL call
        # earlier in this session.
        self._poisoned_com: int | None = None
        self.values: dict[int, float] = {}
        self.width_values: dict[int, str] = {}
        self.delay_values: dict[int, str] = {}
        self.burst_values: dict[int, str] = {}
        self.oscillator_period: int | None = None
        # Live routing for the timing cards; timing writes do not change it.
        self.pulser_outputs: dict[int, list[tuple[int, bool]]] = {}
        self._delay_anchors: dict[int, tuple[int, int, int, int]] = {}
        self.frequency_readback_khz = np.nan
        self.output_rows: list[dict[str, Any]] | None = None
        # Source/clock registers read in addition to collect_housekeeping; see
        # _collect_hd_routing. None means "not read since the last transition".
        self.hd_routing: dict[str, Any] | None = None
        self._hd_routing_time = 0.0
        self._controller_flags: list[str] | None = None
        self._hd_hardware: tuple[Any, ...] | None = None

    def initializeValues(self, reset: bool = False) -> None:
        if getattr(self, "values", None) is None or reset:
            self.values = {
                channel.pulser_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.width_values = {
                channel.pulser_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.delay_values = {
                channel.pulser_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.burst_values = {
                channel.pulser_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.oscillator_period = None
            self.pulser_outputs = {}
            self.frequency_readback_khz = np.nan
            self.output_rows = None
            self.hd_routing = None
            self._controller_flags = None

    def runInitialization(self) -> None:
        if self._initial_open_close_requested:
            self.initializing = False
            return
        # False without a device only means that the last OFF was not confirmed: no port is held, so
        # the operator may reconnect (2026-11-02 lab: ON did nothing after "Communication lost").
        if self._dispose_device() is False and self.device is not None:
            self.initializing = False
            self.print("AMX HD connection refused: the previous connection still owns its port.", flag=PRINT.WARNING)
            return
        self.initialized = False
        try:
            if self._initial_open_close_requested:
                return
            self._initialize_transport_session()
            if self._initial_open_close_requested:
                self.shutdownCommunication()
            else:
                device, token = self.device, self._native_cancel_event
                _invoke_gui_callback(
                    lambda: self.initComplete(native_device=device, native_token=token)
                )
        except Exception as exc:  # noqa: BLE001
            self._retire_failed_native_worker()
            self._restore_off_ui_state()
            if self._native_cancel_event.is_set():
                # OFF during the connection (2026-11-02 lab: "Operation cancelled"): not a failure.
                self.print("AMX connection stopped: OFF was requested.")
            else:
                guidance = self._init_failure_guidance(exc)
                message = (
                    f"AMX initialization failed on COM{int(self.controllerParent.com)}: "
                    f"{self._format_exception(exc)}"
                )
                if guidance:
                    message = f"{message}\n{guidance}"
                self.print(message, flag=PRINT.ERROR)
            if (self._initial_open_close_requested
                    and self.device is not None
                    and not self._initial_open_incomplete()):
                self.shutdownCommunication()
            else:
                self._dispose_device()
        finally:
            self.initializing = False
            if self._native_transport_lost or self.device is not None:
                self._sync_status_to_gui()

    def _initialize_transport_session(self) -> None:
        """Create the AMX driver and open transport-only communication."""
        driver_class = _get_amx_driver_class()
        self.device = driver_class(
            device_id=f"{self.controllerParent.name.lower()}_com{int(self.controllerParent.com)}",
            com=int(self.controllerParent.com),
            baudrate=int(self.controllerParent.baudrate),
            native_backend=True,
            log_dir=Path(self.controllerParent.pluginManager.Settings.dataPath) / "logs" / self.controllerParent.name.lower(),
        )
        _require_native_backend(self.device)
        self._native_ready_device = None
        self._native_cancel_event = Event()
        self.main_state = "Connecting"
        self.device_enabled_state = "Unknown"
        self._native_transport_lost = False
        if self._initial_open_close_requested:
            self._dispose_device()  # Construction finished, but Open was never sent.
            return
        self._device_call(self.device, "connect", timeout_s=float(self.controllerParent.connect_timeout_s))
        if self._initial_open_close_requested:
            return  # The worker must verify shutdown, not continue initialization.
        self._refresh_available_configs()
        self._refresh_loaded_config_status()
        self._update_state()
        self._native_ready_device = self.device

    def _shutdown_kwargs(self) -> dict[str, Any]:
        standby_config = self._resolved_safety_config("standby_config")
        if standby_config < 0:
            return {}

        entry = self._config_entry_by_index(standby_config)
        if entry is not None:
            if not _coerce_bool(entry.get("valid"), True):
                return {}
            if not self._config_entry_is_standby_like(entry):
                return {}
        elif self.available_configs:
            return {}

        # If the standby config is already loaded, or the state is already
        # STATE_STANDBY, no need to reload it. Just disconnect.
        if (
            _coerce_int(getattr(self, "_loaded_config_index", -1), -1) == standby_config
            or _state_is_standby(getattr(self, "main_state", ""))
        ):
            return {"disable_device": False}

        return {
            "standby_config": standby_config,
            "disable_device": False,
        }

    def _resume_pending_on_request_after_transport_ready(self) -> None:
        """Continue a pending ON request once communication is initialized.

        When the operator clicks ON while the AMX is disconnected, the device UI
        first goes through initializeCommunication(). Once the transport is
        ready, resume the actual config-load/enable sequence automatically so
        the first ON click reaches STATE_ON without requiring a manual Load now.
        """
        is_on = getattr(self.controllerParent, "isOn", None)
        if not callable(is_on) or not bool(is_on()):
            return
        if self.resume_session:
            # Crash resume: no config selection or load, no enable, no timing change; read as it runs.
            Thread(target=self._resume_without_commands, name=f"{self.controllerParent.name} resume",
                   daemon=True).start()
            return
        # If no Operating config is selected but the device is already in
        # STATE_STANDBY (parked there by a previous shutdown), auto-select the
        # standby config so the first ON can complete without requiring the
        # operator to manually pick a config in the dropdown first. The operator
        # can switch to a real config afterwards via "Load now".
        if self._selected_operating_config_index() < 0:
            standby_config = self._resolved_safety_config("standby_config")
            if standby_config >= 0 and _state_is_standby(getattr(self, "main_state", "")):
                self.controllerParent._set_config_setting_value("operating_config", standby_config)
        if self.transitioning or not self._begin_transition(True):
            return
        toggle_thread = getattr(self, "toggleOnFromThread", None)
        if callable(toggle_thread):
            toggle_thread(parallel=True)
            return
        self.toggleOn()

    def _resume_without_commands(self) -> None:
        """Crash resume: read the AMX as it runs and show it; nothing is loaded, enabled or applied."""
        try:
            with self._controller_lock_section("Could not acquire lock to resume the AMX."):
                snapshot = self._collect_resume_snapshot()
                self._apply_snapshot(snapshot)
                self._refresh_loaded_config_status()
            self._sync_loaded_config_to_gui(snapshot)
            self._restart_acquisition_after_transition()
            self.print("Resumed after an Explorer crash: AMX adopted as it runs "
                       f"({self._startup_snapshot_summary(snapshot)}); no config was loaded, nothing was enabled.")
        except Exception as exc:  # noqa: BLE001 - reported; the connection stays open, nothing commanded
            retired = self._retire_failed_native_worker()
            self.errorCount += 1
            outcome = ("Native communication was lost; output safety is not confirmed."
                       if retired else "Communication stays open; nothing was commanded.")
            self.print(f"Resume after an Explorer crash: could not read the AMX state: "
                       f"{self._format_exception(exc)}. {outcome}",
                       flag=PRINT.ERROR)
        finally:
            self.resume_session = False
            self._sync_status_to_gui()

    def _refresh_available_configs(self) -> None:
        device = self.device
        if device is None:
            self.available_configs = []
            self.available_configs_text = "n/a"
            return

        list_configs = getattr(device, "list_configs", None)
        if not callable(list_configs):
            self.available_configs = []
            self.available_configs_text = "Unavailable"
            return

        # About 180 names at 30 ms each, 5.6 s on the instrument, with nothing moving on the panel.
        self.available_configs_text = "Reading names..."
        self._sync_status_to_gui()
        self.print("Reading the AMX HD config names, this takes a few seconds.")
        try:
            configs = self._device_call(device, "list_configs",
                timeout_s=float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
            )
        except Exception as exc:  # noqa: BLE001
            self.available_configs_text = "Unavailable"
            if self._retire_failed_native_worker() or self._native_cancel_event.is_set():
                raise
            self.available_configs = []
            self.print(
                f"Could not read AMX config list: {self._format_exception(exc)}",
                flag=PRINT.WARNING,
            )
            return

        self.available_configs = list(configs)
        self.available_configs_text = _format_available_configs(configs)

    def _refresh_loaded_config_status(self) -> None:
        device = self.device
        if device is None:
            self.loaded_config_text = "n/a"
            self._loaded_config_index = -1
            return

        get_status = getattr(device, "get_status", None)
        if not callable(get_status):
            self.loaded_config_text = "Unavailable"
            return

        try:
            status = self._device_call(device, "get_status")
        except Exception as exc:  # noqa: BLE001
            if self._retire_failed_native_worker() or self._native_cancel_event.is_set():
                raise
            self.loaded_config_text = "Unavailable"
            self.print(
                f"Could not read loaded AMX config: {self._format_exception(exc)}",
                flag=PRINT.WARNING,
            )
            return

        self._loaded_config_index = _coerce_int(status.get("memory_config"), -1)
        self.loaded_config_text = _format_loaded_config_text(status)
        self._refresh_interlock_function(device)

    def _refresh_interlock_function(self, device: Any) -> None:
        """Read the loaded config's InterlockFunct (GetInterlockFunct); the plugin never writes it.

        It belongs to each config. OFF: the controller ignores the interlock input
        (pins 3 and 5 of the output connectors).
        """
        try:
            result = self._device_call(device, "get_interlock_funct")
        except (AttributeError, NotImplementedError, TypeError, ValueError):
            result = None  # A getter missing from an older facade: unknown, not a transport fault.
        except Exception as exc:  # noqa: BLE001
            if self._retire_failed_native_worker() or self._native_cancel_event.is_set():
                raise
            self.print(f"Could not read the AMX HD interlock function: {self._format_exception(exc)}",
                       flag=PRINT.WARNING)
            result = None
        function = (bool(result[1]) if isinstance(result, (tuple, list)) and len(result) == 2
                    and _coerce_int(result[0], -1) == 0 else None)
        if function is not None and function != self.interlock_function:
            self.print("AMX HD interlock function ON in the loaded config." if function else
                       "AMX HD interlock function OFF in the loaded config: the interlock input "
                       "(pins 3 and 5 of the output connectors) is ignored.")
        self.interlock_function = function

    def timing_block_reason(self, pulser: int | None = None) -> str:
        """Why a frequency (no pulser) or width edit cannot act on the loaded config, or "".

        Frequency and widths are computed for the 100 MHz clock. Decoded from the last readback;
        before the first one nothing is known and the edit goes through, ON reloads the config.
        """
        flags = self._controller_flags
        if flags is None:
            return ""
        if pulser is None:
            if "ST_ENB_OSC0" not in flags or not self.oscillator_period:
                return "the loaded config keeps oscillator 0 off"
            clock = 0
        else:
            if "ST_ENB_TIMER" not in flags:
                return "the loaded config keeps the timers off"
            clock = _hd_timer_clock(pulser)
        sources = (self.hd_routing or {}).get("clock_sources") or []
        code = _optional_nonnegative_int(sources[clock]) if clock < len(sources) else None
        if code is None or code & 0x0F == _HD_CLOCK_100M:
            return ""
        return f"Clock {clock} is {_hd_clock_text(code)}, not CLK100M"

    def _operating_config_is_standby(self, config_index: int | None = None) -> bool:
        """Return True if this config, by default the selected Operating config, is a standby slot."""
        index = self._selected_operating_config_index() if config_index is None else config_index
        return self._config_entry_is_standby_like(self._config_entry_by_index(index))

    def _device_active(self) -> bool:
        """Return True when the device is in its expected operating state.

        For a normal Operating config, that is STATE_ON. For a standby
        Operating config, STATE_STANDBY is the expected outcome (HV not
        applied) and is treated as active so the ON/OFF button stays ON
        and the OFF toggle remains reachable for shutdown.
        """
        state = getattr(self, "main_state", "")
        if _state_is_on(state):
            return True
        # The selector may already show the next config: a standby in memory is expected too.
        return _state_is_standby(state) and (self._operating_config_is_standby()
                                             or self._operating_config_is_standby(
                                                 getattr(self, "_loaded_config_index", -1)))

    def _sync_loaded_config_to_gui(self, snapshot: dict[str, Any], restored: "dict[str, Any] | None" = None) -> None:
        """The loaded hardware config owns the timing; ``restored`` is the operator's last timing just written back."""
        device = self.device
        period = _coerce_int(snapshot.get("oscillator", {}).get("period"), -1)
        widths = {
            _coerce_int(p.get("pulser"), -1): _coerce_int(p.get("width_ticks"), -1)
            for p in snapshot.get("pulsers", [])
        }
        channels = [ch for ch in self.controllerParent.getChannels() if ch.real]
        if period < 0 or any(widths.get(ch.pulser_number(), -1) < 0 for ch in channels):
            raise RuntimeError("Loaded AMX config timing readback is incomplete.")
        clock_hz = float(_safe_device_attr(device, "CLOCK", 100e6))
        # Without a running oscillator on the 100 MHz clock, the config has no frequency to adopt.
        frequency_khz = (None if self.timing_block_reason()
                         else clock_hz / (period + _safe_device_attr(device, "OSC_OFFSET", 2)) / 1000.0)
        width_offset = _safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2)
        self._pending_config_snapshot = snapshot
        period_ticks = period + _coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2)
        if restored is None:
            self._delay_anchors.clear()  # The config's delays are the new origin.
            if frequency_khz is not None:
                self._remember_timing(frequency_khz, {
                    str(ch.pulser_number()): {
                        "width_us": ((widths[ch.pulser_number()] + width_offset) / clock_hz * 1e6
                                     if widths[ch.pulser_number()] else 0.0),
                        "enabled": widths[ch.pulser_number()] > 0, "width_origin": None}
                    for ch in channels})
        else:
            frequency_khz = restored["frequency_khz"]  # As the operator typed it.

        def adopt() -> None:
            if self.device is not device or getattr(self, "_pending_config_snapshot", None) is not snapshot:
                return  # OFF, disconnect or a newer config invalidated this readback.
            parent = self.controllerParent
            loading = getattr(parent, "loading", False)
            parent.loading = True
            setting = None
            try:
                cancel = getattr(parent, "_cancel_runtime_apply_timers", None)
                if callable(cancel):
                    cancel()
                getter = getattr(parent, "_setting", None)
                if callable(getter):
                    setting = getter(parent.FREQUENCY_KHZ)
                if frequency_khz is not None:
                    _set_config_parameter(parent, "frequency_khz", setting, frequency_khz)
                    _set_config_spinbox(getattr(parent, "frequencyWidget", None), frequency_khz)
                    parent._timing_basis_khz = frequency_khz  # The adopted widths belong to it.
                parent._timing_edited = restored is not None
                for channel in channels:
                    ticks = widths[channel.pulser_number()]
                    width_us = (ticks + width_offset) / clock_hz * 1e6 if ticks else 0.0
                    enabled = ticks > 0
                    entry = (restored or {}).get("pulsers", {}).get(channel.pulser_number())
                    if entry is not None:  # The operator's own values, also the width of a stopped pulser.
                        width_us, enabled = entry["width_us"], entry["enabled"]
                    getter = getattr(channel, "getParameterByName", None)
                    for attr, name, value in (
                        ("value", getattr(channel, "VALUE", "Value"), width_us),
                        ("enabled", getattr(channel, "ENABLED", "Enabled"), enabled),
                        ("active", getattr(channel, "ACTIVE", "Active"), True),
                    ):
                        parameter = getter(name) if callable(getter) else None
                        _set_config_parameter(channel, attr, parameter, value)
                    # Keep the equation text, but require an explicit return to
                    # equation mode instead of immediately undoing the config.
                    channel.lastAppliedValue = channel.value
                    origin = entry["width_origin"] if entry is not None else None
                    channel._duty_anchor = None if origin is None else (
                        *origin, round(width_us * clock_hz / 1e6), period_ticks)
                    for name in ("activeChanged", "_update_duty_label", "_sync_enabled_toggle_widget"):
                        refresh = getattr(channel, name, None)
                        if callable(refresh):
                            refresh()
                    row = getattr(parent, "amxTimingRows", {}).get(channel.pulser_number(), {})
                    _set_config_spinbox(row.get("width"), width_us)
                    _set_config_spinbox(row.get("duty"), _requested_duty_percent(parent, width_us))
            finally:
                parent.loading = loading
            save = getattr(setting, "settingEvent", None)
            if callable(save):
                save()
            self._pending_config_snapshot = None
            self._sync_status_to_gui()

        _invoke_gui_callback(adopt)

    def _collect_startup_snapshot(self) -> dict[str, Any]:
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")
        # Lightweight state-only read: startup polling only needs to detect
        # STATE_ON + device_enabled. The full collect_housekeeping (~21 DLL
        # calls) would hold the controller lock for ~1-2 s per iteration and
        # stall the ON transition for many seconds.
        return self._device_call(device, "collect_state_snapshot", timeout_s=self._startup_snapshot_timeout_s())

    def _collect_resume_snapshot(self) -> dict[str, Any]:
        """Crash resume adopts the running timing: full housekeeping and routing, read only.

        The light state read has no oscillator or timers, so the resume always failed with
        "Loaded AMX config timing readback is incomplete" (2026-11-02 lab, 13:09).
        """
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")
        snapshot = self._device_call(device, "collect_housekeeping", timeout_s=self._startup_snapshot_timeout_s())
        self._refresh_hd_routing(device, snapshot)
        return snapshot

    def _startup_snapshot_ready(self, snapshot: dict[str, Any], config_index: int | None = None) -> bool:
        """The config being loaded decides, not the selector, which may change during the wait."""
        state = str(snapshot.get("main_state", {}).get("name", "") or "")
        if bool(snapshot.get("device_enabled", False)) and _state_is_on(state):
            return True
        # A standby operating config keeps the device in STATE_STANDBY (HV not
        # applied); that is the expected outcome, not a startup failure.
        if self._operating_config_is_standby(config_index) and _state_is_standby(state):
            return True
        return False

    def _startup_failure_message(
        self,
        *,
        config_index: int,
        snapshot: dict[str, Any],
    ) -> str:
        state = str(snapshot.get("main_state", {}).get("name", "Unknown") or "Unknown")
        flags = snapshot.get("device_state", {}).get("flags", [])
        flags_text = ", ".join(str(flag) for flag in flags) if flags else "n/a"
        device_enabled = "ON" if bool(snapshot.get("device_enabled", False)) else "OFF"
        expected = "STATE_STANDBY" if self._operating_config_is_standby(config_index) else "STATE_ON"
        return (
            f"AMX did not reach {expected} after loading config {config_index}: "
            f"state={state}, device={device_enabled}, flags={flags_text}."
        )

    def _wait_for_startup_ready_snapshot(
        self,
        *,
        config_index: int,
        settle_timeout_s: float,
    ) -> dict[str, Any]:
        settle_timeout_s = max(float(settle_timeout_s), 0.1)
        started_s = time.monotonic()
        deadline = started_s + settle_timeout_s
        next_progress_s = started_s + _AMX_STARTUP_PROGRESS_LOG_INTERVAL_S
        latest_snapshot: dict[str, Any] | None = None
        while True:
            snapshot = self._collect_startup_snapshot()
            latest_snapshot = snapshot
            self._apply_snapshot(snapshot)
            if self._startup_snapshot_ready(snapshot, config_index):
                self._print_if_slow(
                    f"AMX startup reached {self._startup_snapshot_summary(snapshot)}",
                    started_s,
                )
                return snapshot
            now_s = time.monotonic()
            if now_s >= deadline:
                raise RuntimeError(
                    self._startup_failure_message(
                        config_index=config_index,
                        snapshot=latest_snapshot,
                    )
                )
            if now_s >= next_progress_s:
                self.print(
                    f"Waiting for AMX startup after config {config_index}: "
                    f"{now_s - started_s:.1f}/{settle_timeout_s:.1f} s elapsed "
                    f"({self._startup_snapshot_summary(snapshot)}).",
                    flag=PRINT.WARNING,
                )
                next_progress_s = now_s + _AMX_STARTUP_PROGRESS_LOG_INTERVAL_S
            time.sleep(0.1)

    def _load_operating_config_and_enable_device(
        self,
        *,
        config_index: int,
        timeout_s: float,
        load_config_first: bool,
        restore_timing: bool = False,
    ) -> dict[str, Any]:
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")

        if load_config_first:
            started_s = time.monotonic()
            self._device_call(device, "load_config", config_index, timeout_s=timeout_s)
            self._print_if_slow(f"AMX load_config({config_index})", started_s)
            self._loaded_config_index = config_index
            started_s = time.monotonic()
            self._refresh_loaded_config_status()
            self._print_if_slow("AMX loaded-config refresh", started_s)
        is_standby = self._config_entry_is_standby_like(
            self._config_entry_by_index(config_index)
        )
        # A standby operating config keeps the device in STATE_STANDBY (HV not
        # applied); do not force-enable it. The operator knowingly selected standby.
        if not is_standby:
            started_s = time.monotonic()
            self._device_call(device, "set_device_enabled", True, timeout_s=timeout_s)
            self._print_if_slow("AMX set_device_enabled(ON)", started_s)
        try:
            self._wait_for_startup_ready_snapshot(
                config_index=config_index,
                settle_timeout_s=timeout_s,
            )
            # Keep the readiness poll lightweight, then read timing once.
            snapshot = self._device_call(device, "collect_housekeeping", timeout_s=self._startup_snapshot_timeout_s())
            self.hd_routing = None  # A poll may have read the previous config just before this load.
            self._refresh_hd_routing(device, snapshot)
            self._apply_snapshot(snapshot)
            restored = None
            if restore_timing and load_config_first and not is_standby:
                snapshot, restored = self._restore_last_timing(device, snapshot, config_index, timeout_s)
            self._sync_loaded_config_to_gui(snapshot, restored=restored)
            return snapshot
        except Exception:
            if not is_standby:
                with contextlib.suppress(Exception):
                    self._device_call(device, "set_device_enabled", False, timeout_s=timeout_s, cancellable=False)
            raise

    def loadOperatingConfigNow(self) -> None:
        device = self.device
        if device is None or not getattr(self, "initialized", False):
            self.print(
                f"Cannot load {self.controllerParent.name} config: communication not initialized.",
                flag=PRINT.WARNING,
            )
            return
        is_on = getattr(self.controllerParent, "isOn", None)
        if not callable(is_on) or not bool(is_on()):
            self.print(
                f"Cannot load {self.controllerParent.name} config while the AMX is OFF.",
                flag=PRINT.WARNING,
            )
            return

        ready, reason, config_index = self._operating_config_ready()
        if not ready:
            self.print(
                f"Cannot load {self.controllerParent.name} config: {reason}.",
                flag=PRINT.WARNING,
            )
            return

        # Guard against concurrent Load now calls — e.g. double-click or
        # a stale thread from a previous load.  The guard uses transitioning
        # so it also blocks while an ON/OFF toggle is in progress.
        if getattr(self, "transitioning", False):
            self.print(
                f"Cannot load {self.controllerParent.name} config: a transition is already in progress.",
                flag=PRINT.WARNING,
            )
            return
        if not self._begin_transition(True):
            self.print(
                f"Cannot load {self.controllerParent.name} config: a load is already in progress.",
                flag=PRINT.WARNING,
            )
            return

        self._discard_pending_runtime_applies()
        recording = bool(getattr(self.controllerParent, "recording", False))
        self._stop_acquisition_for_transition()
        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        try:
            self._start_operating_mode(
                config_index=config_index,
                timeout_s=timeout_s,
                lock_message="Could not acquire lock to load the AMX config.",
                success_message=f"Loaded AMX config {config_index}.",
                restart_acquisition=True,
            )
        except TimeoutError:
            self._retire_failed_native_worker()
        except Exception as exc:  # noqa: BLE001
            self._retire_failed_native_worker()
            if self._native_cancel_event.is_set():
                # OFF during the load (2026-11-02 lab: "Operation cancelled"): not a failure.
                self.print(f"AMX config {config_index} not loaded: OFF was requested.")
                return
            self.errorCount += 1
            self.print(
                f"Failed to load AMX config {config_index}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            if (self._initial_open_close_requested
                    and self.device is not None):
                self.shutdownCommunication()
            self._end_transition()
            self._resume_after_load(recording)
            self._sync_status_to_gui()

    def _apply_channel_timing(self, channel: AMXHDChannel, timeout_s: float) -> None:
        device = self.device
        if device is None or not channel.real:
            return

        self.output_rows = None
        self._sync_status_to_gui()
        pulser = channel.pulser_number()
        width_us = _coerce_float(getattr(channel, "value", 0.0), 0.0)
        width_offset = _safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2)
        if channel.enabled and width_us > 0.0:
            width_ticks = round(width_us * _safe_device_attr(device, "CLOCK", 100e6) / 1e6) - width_offset
            if width_ticks < 1:
                # The requested width is below the minimum representable width;
                # surface it instead of silently driving a 1-tick pulse.
                self.print(
                    f"AMX pulser {pulser} width {width_us:g} us is below the minimum "
                    "representable width; driving the hardware minimum (1 tick).",
                    flag=PRINT.WARNING,
                )
                width_ticks = 1
            self._device_call(device, "set_pulser_width_ticks", pulser, width_ticks, timeout_s=timeout_s)
        else:
            self._device_call(device, "set_pulser_width_ticks", pulser, 0, timeout_s=timeout_s)

    def applyGlobalSettings(self) -> None:
        device = self.device
        if (
            device is None
            or not getattr(self, "initialized", False)
            or getattr(self, "transitioning", False)
            or not getattr(self.controllerParent, "isOn", lambda: False)()
        ):
            return

        timeout_s = float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
        try:
            with self._controller_lock_section(
                "Could not acquire lock to apply AMX frequency."
            ):
                device = self.device
                if (device is None or getattr(self, "transitioning", False)
                        or getattr(self, "_pending_config_snapshot", None) is not None
                        or not self.controllerParent.isOn()):
                    return
                reason = self.timing_block_reason()
                if reason:
                    self.print(f"AMX frequency not applied: {reason}.", flag=PRINT.WARNING)
                    return
                self.output_rows = None
                self._sync_status_to_gui()
                frequency_khz = float(getattr(self.controllerParent, "frequency_khz", 2.0))
                shorter, delays = self._delays_kept_in_new_period(device, frequency_khz, timeout_s)

                def write_frequency() -> None:
                    self._device_call(device, "set_frequency_khz", frequency_khz, timeout_s=timeout_s)

                def write_widths() -> None:
                    for channel in self.controllerParent.getChannels():
                        self._apply_channel_timing(channel, timeout_s)

                def write_delays() -> None:
                    for pulser, delay in delays:
                        self._device_call(device, "set_pulser_delay_ticks", pulser, delay, timeout_s=timeout_s)

                # Delay + width stay inside the period at every step.
                steps = (write_delays, write_widths, write_frequency) if shorter else (
                    write_frequency, write_widths, write_delays)
                for step in steps:
                    step()
                self._remember_gui_timing()
        except TimeoutError:
            self._retire_failed_native_worker()
            return
        except Exception as exc:  # noqa: BLE001
            self._retire_failed_native_worker()
            self.errorCount += 1
            self.print(
                f"Failed to apply AMX global settings: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )

    def applyValueFromThread(self, channel: AMXHDChannel, parallel: bool = True) -> None:
        def start() -> None:
            finish = getattr(self.controllerParent, "_finish_setpoint_edits", None)
            if callable(finish) and channel.enabled:
                finish(channel)
            if parallel:
                self._queue_channel_timing_apply(channel)
            else:
                self.applyValue(channel)
        _invoke_gui_callback(start)

    def _queue_channel_timing_apply(self, channel: AMXHDChannel) -> None:
        pulser = channel.pulser_number()
        with self._channel_apply_state_lock:
            self._channel_apply_pending[pulser] = channel
            if self._channel_apply_worker_running:
                return
            self._channel_apply_worker_running = True
            Thread(
                target=self._channel_timing_apply_worker,
                name=f"{self.controllerParent.name} applyChannelTimingThread",
                daemon=True,
            ).start()

    def _restart_acquisition_after_transition(self) -> None:
        """Restart acquisition after a transition without joining the old thread.

        The base ``startAcquisition`` joins the old acquisition thread with a 5s
        timeout, and a poll may still be running.  Instead, just set ``acquiring = True`` and start a new
        thread only if the old one is dead.  If the old thread is still alive
        (in ``collect_housekeeping``), it will see ``acquiring = True`` at the
        top of the next iteration and continue polling.
        """
        with self.__dict__.setdefault("_acquisition_start_lock", Lock()):
            self.acquiring = True
            old_thread = getattr(self, "acquisitionThread", None)
            if old_thread is None or not old_thread.is_alive():
                self.acquisitionThread = Thread(
                    target=self.runAcquisition,
                    name=f"{self.controllerParent.name} acquisitionThread",
                )
                self.acquisitionThread.daemon = True
                self.acquisitionThread.start()

    def readNumbers(self, *, already_acquired: bool = False) -> None:
        if self.device is None or not getattr(self, "initialized", False):
            self.initializeValues(reset=True)
            return

        timeout_s = float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
        lock_timeout_message = "Could not acquire lock to read AMX housekeeping."
        try:
            with self._controller_lock_section(
                lock_timeout_message,
                already_acquired=already_acquired,
                timeout_s=0.0,
                log_timeout=False,
            ):
                device = self.device
                if device is None:
                    return
                snapshot = self._device_call(device, "collect_housekeeping", timeout_s=timeout_s)
                if (
                    self.device is not device
                    or not getattr(self, "initialized", False)
                ):
                    return
                self._refresh_hd_routing(device, snapshot)
                if (
                    self.device is not device
                    or not getattr(self, "initialized", False)
                ):
                    return
                self._apply_snapshot(snapshot)
                self._consecutive_poll_errors = 0
        except TimeoutError as exc:
            if str(exc) == lock_timeout_message:
                return
            self._poll_error("Timed out while polling AMX housekeeping.", exc=exc)
            return
        except Exception as exc:  # noqa: BLE001
            if self._native_cancel_event.is_set():
                return  # OFF cancelled this poll (2026-10-28 lab: "Operation cancelled"): not a fault.
            self._poll_error(
                f"Failed to read AMX housekeeping: {self._format_exception(exc)}",
                exc=exc,
            )
            return

    def _refresh_hd_routing(self, device: Any, snapshot: dict[str, Any]) -> None:
        """Keep the decoded-output routing current; read it only while running.

        Called with the controller lock held, from the housekeeping poll. A
        stopped or standby controller has no output to decode, so its routing is
        dropped and read again right after the next successful start.
        """
        running = (
            snapshot.get("device_enabled") is True
            and snapshot.get("main_state", {}).get("name") == "STATE_ON"
        )
        if not running:
            self.hd_routing = None
            return
        if (
            self.hd_routing is not None
            and time.monotonic() - self._hd_routing_time < _HD_ROUTING_REFRESH_S
        ):
            return
        routing = self._collect_hd_routing(device, snapshot)
        self.hd_routing = routing
        self._hd_routing_time = time.monotonic()

    def _collect_hd_routing(self, device: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Read the HD source, clock and edge-delay registers (read-only DLL getters).

        A nonzero vendor status leaves that register unknown; the decoder then
        reports the affected outputs as Unknown instead of guessing.
        """

        def read(method: str, *args: Any, count: int = 1) -> Any:
            try:
                result = self._device_call(device, method, *args)
            except (AttributeError, NotImplementedError, TypeError, ValueError):
                # A getter missing from an older facade/worker is not a transport
                # fault; its register stays unknown. Transport errors propagate.
                return None
            if (
                not isinstance(result, (tuple, list))
                or len(result) != count + 1
                or _coerce_int(result[0], -1) != 0
            ):
                return None
            return result[1] if count == 1 else tuple(result[1:])

        if self._hd_hardware is None:
            self._hd_hardware = read("get_hw_type", count=9)
        hardware = self._hd_hardware
        burst_timers = (
            _optional_nonnegative_int(hardware[3]) if hardware is not None else None
        )
        if burst_timers is None:
            burst_timers = _HD_BURST_TIMERS_DEFAULT
        routing: dict[str, Any] = {
            "clock_sources": [read("get_clock_source", clock) for clock in range(4)],
            "oscillator_periods": [read("get_oscillator_period", oscillator) for oscillator in range(4)],
            "burst_timers": burst_timers,
        }
        timer_count = len([p for p in snapshot.get("pulsers", []) if isinstance(p, dict)])
        # The operator cards show each channel timer's trigger; outputs add theirs.
        referenced = {
            channel.pulser_number()
            for channel in self.controllerParent.getChannels()
            if channel.real and channel.pulser_number() < timer_count
        }
        switches = []
        for channel in range(4):
            trigger = read("get_switch_trigger_source", channel)
            enable = read("get_switch_enable_source", channel)
            delays = read("get_switch_delay", channel, count=2)
            switches.append({
                "switch": channel,
                "trigger_source": trigger,
                "enable_source": enable,
                "delay": None if delays is None else {"rise": delays[0], "fall": delays[1]},
                "fine_delay": {
                    "rise": read("get_switch_rise_delay_fine", channel),
                    "fall": read("get_switch_fall_delay_fine", channel),
                },
            })
            for code in (trigger, enable):
                code = _optional_nonnegative_int(code)
                if code is None or code & 0x40:
                    continue
                selected = code & 0x3F
                if _HD_SIGNAL_TIMER_OUT <= selected < _HD_SIGNAL_OSCILLATOR:
                    timer = (selected - _HD_SIGNAL_TIMER_OUT) % 16
                    if timer < timer_count:
                        referenced.add(timer)
        routing["switches"] = switches
        routing["timer_triggers"] = {
            timer: read("get_timer_trigger_source", timer) for timer in sorted(referenced)
        }
        routing["timer_stops"] = {
            timer: read("get_timer_stop_source", timer)
            for timer in sorted(referenced)
            if timer < burst_timers
        }
        return routing

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        if self.main_state == _AMX_SHUTDOWN_UNCONFIRMED_STATE:
            return  # A late status poll must not erase an unconfirmed shutdown.
        device = self.device
        self.main_state = str(snapshot.get("main_state", {}).get("name", "Unknown"))
        self.device_enabled_state = (
            "ON" if bool(snapshot.get("device_enabled", False)) else "OFF"
        )
        flags = snapshot.get("device_state", {}).get("flags", [])
        self.device_state_summary = ", ".join(str(flag) for flag in flags) if flags else "OK"
        controller_flags = snapshot.get("controller_state", {}).get("flags", [])
        if snapshot.get("read_recovery"):
            self.print(f"AMX reply lost, port resynchronized and read again: {snapshot['read_recovery']}",
                       flag=PRINT.WARNING)
        self.controller_state_summary = (
            ", ".join(str(flag) for flag in controller_flags) if controller_flags else "OK"
        )
        self._controller_flags = list(controller_flags) if isinstance(controller_flags, (list, tuple)) else None

        oscillator_period = _coerce_int(
            snapshot.get("oscillator", {}).get("period"),
            0,
        )
        osc_offset = _coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2)
        width_offset = _coerce_int(_safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2), 2)
        delay_offset = _coerce_int(_safe_device_attr(device, "PULSER_DELAY_OFFSET", 3), 3)
        total_ticks = oscillator_period + osc_offset
        clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
        ticks_per_us = clock_hz / 1e6
        self.oscillator_period = oscillator_period
        snapshot_frequency_hz = _coerce_float(
            snapshot.get("oscillator", {}).get("frequency_hz"),
            np.nan,
        )
        self.frequency_readback_khz = (
            np.nan  # Period 0 would read 50 MHz (2026-11-02 lab: 86 of the 178 CGC configs).
            if self.timing_block_reason()
            else snapshot_frequency_hz / 1000.0
            if np.isfinite(snapshot_frequency_hz)
            else clock_hz / total_ticks / 1000.0
            if total_ticks > 0
            else np.nan
        )

        new_values: dict[int, float] = {}
        new_width_ticks: dict[int, str] = {}
        new_delay_us: dict[int, str] = {}
        new_bursts: dict[int, str] = {}

        for pulser_snapshot in snapshot.get("pulsers", []):
            pulser = _coerce_int(pulser_snapshot.get("pulser"), -1)
            if pulser < 0:
                continue
            width_ticks = _coerce_int(pulser_snapshot.get("width_ticks"), 0)
            delay_ticks = _coerce_int(pulser_snapshot.get("delay_ticks"), 0)
            if total_ticks > width_offset:
                duty_percent = ((width_ticks + width_offset) / total_ticks) * 100.0
            else:
                duty_percent = np.nan
            new_values[pulser] = duty_percent
            new_width_ticks[pulser] = str(width_ticks)
            new_delay_us[pulser] = f"{(delay_ticks + delay_offset) / ticks_per_us:.2f}"
            burst = pulser_snapshot.get("burst")
            new_bursts[pulser] = "n/a" if burst is None else str(burst)

        running = (snapshot.get("device_enabled") is True
                   and snapshot.get("main_state", {}).get("name") == "STATE_ON")
        # Stopped, each output reads "Not enabled", as on the AMX A/B; running, the rows need the
        # routing (2026-11-02 lab: MScan only said "AMX waveform readback is unavailable").
        self.output_rows = (
            _amx_hd_output_rows(snapshot, self.hd_routing)
            if self.hd_routing is not None or not running
            else None
        )
        if self.output_rows is not None:
            pulser_outputs: dict[int, list[tuple[int, bool]]] = {}
            for index, row in enumerate(self.output_rows):
                if row["state"] == "Periodic" and "source_pulser" in row:
                    pulser_outputs.setdefault(row["source_pulser"], []).append((index, row["source_inverted"]))
            self.pulser_outputs = pulser_outputs
        self.values = new_values
        self.width_values = new_width_ticks
        self.delay_values = new_delay_us
        self.burst_values = new_bursts
        self._sync_status_to_gui()

    def applyValue(self, channel: AMXHDChannel) -> None:
        device = self.device
        if (
            device is None
            or not getattr(self, "initialized", False)
            or getattr(self, "transitioning", False)
            or not getattr(self.controllerParent, "isOn", lambda: False)()
        ):
            return

        timeout_s = float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
        try:
            with self._controller_lock_section(
                f"Could not acquire lock to apply AMX P{channel.pulser_number()}."
            ):
                device = self.device
                if (device is None or getattr(self, "transitioning", False)
                        or getattr(self, "_pending_config_snapshot", None) is not None
                        or not self.controllerParent.isOn()):
                    return
                reason = self.timing_block_reason(channel.pulser_number())
                if reason:
                    self.print(f"AMX P{channel.pulser_number()} not applied: {reason}.", flag=PRINT.WARNING)
                    return
                self._apply_channel_timing(channel, timeout_s)
                self._remember_gui_timing()
        except TimeoutError:
            self._retire_failed_native_worker()
            return
        except Exception as exc:  # noqa: BLE001
            self._retire_failed_native_worker()
            self.errorCount += 1
            self.print(
                f"Failed to apply AMX P{channel.pulser_number()}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )

    def toggleOn(self) -> None:
        target_on = bool(getattr(self.controllerParent, "isOn", lambda: False)())
        device = self.device
        if device is None:
            if target_on:
                self._restore_off_ui_state()
            self._end_transition()
            self._sync_status_to_gui()
            return

        self._discard_pending_runtime_applies()
        self._stop_acquisition_for_transition()

        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))

        try:
            if target_on:
                ready, reason, config_index = self._operating_config_ready()
                if not ready:
                    self._restore_ui_state_for_device()
                    state_note = (
                        " The AMX is currently ON — click OFF to shut down and reset."
                        if self._device_active()
                        else " Use Close communication to disconnect."
                    )
                    self.print(
                        f"Cannot start {self.controllerParent.name}: {reason}."
                        f" Select a valid Operating config.{state_note}",
                        flag=PRINT.WARNING,
                    )
                    return
                is_standby_cfg = self._config_entry_is_standby_like(
                    self._config_entry_by_index(config_index)
                )
                self._start_operating_mode(
                    config_index=config_index,
                    timeout_s=timeout_s,
                    lock_message="Could not acquire lock to start the AMX.",
                    restore_timing=True,
                    success_message=(
                        "AMX operating in standby (HV not applied)."
                        if is_standby_cfg
                        else "AMX timing enabled."
                    ),
                    restart_acquisition=True,
                )
            else:
                shutdown_confirmed = self.shutdownCommunication()
                if not shutdown_confirmed:
                    self._restore_on_ui_state()
                return
        except Exception as exc:  # noqa: BLE001
            self._retire_failed_native_worker()
            self.errorCount += 1
            if (target_on and not (self._initial_open_close_requested
                                  and self.device is not None)):
                self._restore_ui_state_for_device()
            self.print(
                f"Failed to toggle AMX: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            if (target_on and self._initial_open_close_requested
                    and self.device is not None):
                self.shutdownCommunication()
            self._end_transition()
            self._sync_status_to_gui()

    def shutdownCommunication(self) -> bool:
        self._initial_open_close_requested = True
        self._cancel_native_operation()
        if self._retire_failed_native_worker():
            return False
        device = self.device
        if device is None:
            self.closeCommunication()
            return self.main_state == "Disconnected"

        self._discard_pending_runtime_applies()
        self._stop_acquisition_for_transition()
        if self._initial_open_incomplete():
            self.closeCommunication()
            return self.device is None and self.main_state == "Disconnected"
        self.print("Starting AMX shutdown sequence.")
        shutdown_kwargs = self._shutdown_kwargs()
        standby_config = shutdown_kwargs.get("standby_config")
        if isinstance(standby_config, int) and standby_config >= 0:
            self.print(
                f"Parking AMX in standby config {standby_config} before disconnect."
            )
        shutdown_confirmed = False
        confirmation_reason = "shutdown confirmation was not completed"
        try:
            with self._controller_lock_section(
                "Could not acquire lock to shut down the AMX.",
                # The periodic state refresh holds this lock for the duration of
                # a full collect_housekeeping (~poll_timeout_s).  Use a timeout
                # strictly greater than that so the shutdown waits for the
                # in-flight poll to release the lock instead of racing it.
                timeout_s=(
                    float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
                    + float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
                ),
            ):
                device = self.device
                if device is None:
                    shutdown_confirmed = self.main_state == "Disconnected"
                else:
                    started_s = time.monotonic()
                    shutdown_result = self._device_call(device, "shutdown", cancellable=False,
                        timeout_s=float(getattr(self.controllerParent, "startup_timeout_s", 10.0)),
                        **shutdown_kwargs,
                    )
                    self._print_if_slow("AMX shutdown command", started_s)
                    shutdown_confirmed = shutdown_result is True
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            confirmation_reason = self._format_exception(exc)
            self.print(
                f"AMX shutdown failed: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        else:
            if shutdown_confirmed:
                self.print("AMX shutdown sequence completed.")
        finally:
            if not shutdown_confirmed:
                self.print(
                    "AMX shutdown could not be confirmed before disconnect: "
                    f"{confirmation_reason}.",
                    flag=PRINT.ERROR,
                )
            self.closeCommunication(
                final_state=(
                    "Disconnected"
                    if shutdown_confirmed
                    else _AMX_SHUTDOWN_UNCONFIRMED_STATE
                )
            )
        return shutdown_confirmed

    def closeCommunication(self, *, final_state: str | None = None) -> None:
        self._initial_open_close_requested = True
        self._cancel_native_operation()
        if self._retire_failed_native_worker(force=final_state is None):
            return
        if self._initial_open_incomplete():
            # Release only the failed opening, never claim a hardware shutdown.
            if self._dispose_device(initial_open_only=True) is False:
                return
            self._restore_off_ui_state()
            final_state = "Disconnected"
        # Resolve the outcome before closing anything: Open may just have
        # finished, but that does not confirm a hardware shutdown.
        if final_state is None:
            final_state = self.main_state
            if self.device is not None or final_state not in (
                "Disconnected", _AMX_SHUTDOWN_UNCONFIRMED_STATE
            ):
                final_state = _AMX_SHUTDOWN_UNCONFIRMED_STATE
        if final_state == _AMX_SHUTDOWN_UNCONFIRMED_STATE and self.device is not None:
            self.acquiring = False
            self.initialized = True
            self.main_state = _AMX_SHUTDOWN_UNCONFIRMED_STATE
            self.device_enabled_state = "Unknown"
            self.device_state_summary = self.controller_state_summary = "Unknown"
            self._restore_on_ui_state()
            self._sync_status_to_gui()
            return
        base_close = getattr(super(), "closeCommunication", None)
        if callable(base_close):
            base_close()
        resolved_final_state = str(final_state)
        is_disconnected = resolved_final_state == "Disconnected"
        self.main_state = resolved_final_state
        self.device_enabled_state = "OFF" if is_disconnected else "Unknown"
        self.device_state_summary = "n/a" if is_disconnected else "Unknown"
        self.controller_state_summary = "n/a" if is_disconnected else "Unknown"
        self.available_configs = []
        self.available_configs_text = "n/a"
        self.loaded_config_text = "n/a"
        self._loaded_config_index = -1
        self.interlock_function = None
        self._discard_pending_runtime_applies()
        if self._dispose_device() is False:
            return
        self.initialized = False
        self._sync_status_to_gui()

    def _device_call(self, device, method: str, *args, cancellable: bool = True, **kwargs):
        backend = _require_native_backend(device)
        if backend.closed:
            raise RuntimeError("AMX native transport is unusable; output safety is not confirmed.")
        token = self._native_cancel_event if cancellable else None
        if token is not None and token.is_set():
            raise RuntimeError("AMX native operation cancelled before dispatch.")
        # The Event belongs to the supervisor, not the serialized facade.
        result = backend.call_method(
            method, *args,
            rpc_timeout_s=device._native_rpc_timeout_for(method, kwargs),
            _io_timeout_s=kwargs.get("timeout_s"), _cancel_event=token, **kwargs,
        )
        if token is not None and token.is_set():
            raise RuntimeError("AMX native operation cancelled; shutdown must be verified.")
        if method.startswith(("set_", "load_")):
            # Kept for the rest of this write sequence (which reads it); the next poll reads it again.
            self._hd_routing_time = float("-inf")
        return result

    def _retire_failed_native_worker(self, *, force: bool = False) -> bool:
        device = self.device
        backend = self._native_backend(device)
        if backend is None or (not backend.closed and not force):
            return False
        first_loss = not self._native_transport_lost
        self._native_transport_lost = True
        self._initial_open_close_requested = True
        self._native_cancel_event.set()
        self.acquiring = self.initialized = False
        self.main_state = _AMX_SHUTDOWN_UNCONFIRMED_STATE
        self.device_enabled_state = "Unknown"
        self.device_state_summary = self.controller_state_summary = "Unknown"
        self.available_configs = []
        self.available_configs_text = self.loaded_config_text = "n/a"
        self._loaded_config_index = -1
        self.interlock_function = None
        self._discard_pending_runtime_applies()
        self.initializeValues(reset=True)
        try:
            reaped = backend.close(grace_s=0) if force else backend.close()
            if reaped is True and self.device is device:
                self.device = None
        except Exception as exc:
            self.print(f"AMX native worker retirement failed: {exc}", flag=PRINT.ERROR)
        self._restore_off_ui_state()
        token = self._native_cancel_event

        def invalidate_readbacks() -> None:
            if token is self._native_cancel_event and self._native_transport_lost:
                self.controllerParent.recording = False
                self.updateValues()

        _invoke_gui_callback(invalidate_readbacks)
        self._sync_status_to_gui()
        if first_loss:
            self.print("AMX native communication lost. Output safety is not confirmed; "
                       "make the hardware safe before an explicit reconnect.", flag=PRINT.ERROR)
        return True

    def _dispose_device(self, *, initial_open_only: bool = False) -> bool:
        self.output_rows = None
        self.hd_routing = None
        self._controller_flags = None
        self.pulser_outputs = {}
        self._hd_hardware = None
        device = self.device
        if device is None:
            return self._native_transport_lost or self.main_state != _AMX_SHUTDOWN_UNCONFIRMED_STATE
        if self._retire_failed_native_worker():
            return self.device is None
        if (self.main_state == _AMX_SHUTDOWN_UNCONFIRMED_STATE
                and not self._initial_open_incomplete()):
            self._restore_on_ui_state()
            self._sync_status_to_gui()
            return False  # A live worker still needs verified OFF, not bare Close.
        # A connect that just succeeded needs verified shutdown, not bare Close.
        # Its initializer retains the pending stop request and owns that path.
        initial_cleanup = (getattr(device, "_open_failed", False)
                           or getattr(device, "_failed_open_released", False))
        can_disconnect = (not getattr(device, "_opening_in_progress", False)
                          and (not initial_open_only or initial_cleanup))
        released = (can_disconnect and getattr(device, "connected", None) is False
                    and getattr(device, "_dll_port_claimed", None) is False)
        try:
            if can_disconnect and not released:
                released = device.disconnect() is True
        except Exception as exc:
            self.print(f"AMX connection cleanup remains unconfirmed: {exc}", flag=PRINT.ERROR)
            if self._retire_failed_native_worker():
                return self.device is None
        if not released:
            initial_open = self._initial_open_incomplete()
            self.initialized = not initial_open
            self.acquiring = False
            self.main_state = "Connection pending" if initial_open else _AMX_SHUTDOWN_UNCONFIRMED_STATE
            if initial_open:
                self._restore_off_ui_state()
            else:
                self._restore_on_ui_state()
            self._sync_status_to_gui()
            return False
        if self._initial_open_incomplete():
            self.main_state = "Disconnected"
        self.device = None
        self.initialized = False
        with contextlib.suppress(Exception):
            device.close()
        return True

    def _restore_ui_state_for_device(self) -> None:
        """Restore the ON/OFF button to the device's real state after a
        transition that did not complete, so the operator can always reach OFF
        to reset.

        A failed/aborted ON must not strand the UI: when the hardware is
        genuinely STATE_ON — or deliberately STATE_STANDBY because the operator
        selected a standby Operating config — we keep the button ON (so the next
        click toggles to OFF -> shutdownCommunication); otherwise we restore OFF
        as before.  ``main_state`` is the authoritative controller value,
        refreshed by the transport-init ``_update_state``; it never crosses the
        controller/device boundary, and defaults to a non-ON string (no None
        risk).
        """
        if self._retire_failed_native_worker():
            return
        if self._device_active():
            self._restore_on_ui_state()
        else:
            self._restore_off_ui_state()

    def _begin_transition(self, target_on: bool) -> bool:
        with self._transition_guard():
            if self.transitioning:
                return False
            self.transitioning = True
            # A transition can load another config: its routing must be re-read.
            self.output_rows = None
            self.hd_routing = None
            self.pulser_outputs = {}
            self.transition_target_on = bool(target_on)
            return True


