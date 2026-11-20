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

_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_amx_runtime"
_AMX_KEEP_TOOLTIP = (
    "What this pulser keeps when the oscillator frequency changes: duty keeps its duty cycle "
    "and delay as a share of the period (square waves, RF); width keeps its width and delay "
    "in microseconds (fixed-length pulses)."
)
_AMX_TIMING_COLUMNS = (
    ("pulser", "Pulser"), ("drives", "Drives"), ("enable", "On"), ("width", "Width\n(µs)"),
    ("duty", "Duty\n(%)"), ("keep", "Osc change\nkeeps"), ("status", "Registers"),
)
_AMX_TIMING_COLUMN_TIPS = {
    "pulser": "Pulser number; pulser numbers are not output channel numbers. Colour: register state.",
    "drives": "Outputs switched by this pulser, from the live routing registers.",
    "enable": "Run this pulser while the AMX is ON.",
    "width": "Pulse width. Width and duty are one setting, on the 10 ns clock grid. "
             "Read-only while the channel follows its equation (Advanced).",
    "duty": "Pulse width as a share of the oscillator period.",
    "keep": _AMX_KEEP_TOOLTIP,
    "status": "Whether the controller registers hold the request; not a waveform measurement.",
}


def _amx_poisoned_port_guidance(
    exc: Exception,
    *,
    poisoned_com: int | None,
    current_com: int | None,
) -> str:
    """Return operator guidance for a failed AMX init, or "" when none applies.

    A timed-out call stops the native worker, which releases the port. Say what
    to check instead of leaving the operator on a bare 'Error opening port'.
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
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _amx_output_rows(snapshot: dict[str, Any], clock_hz: float = 100e6) -> list[dict[str, Any]]:
    """Describe dual-level CH0–3 from live registers, never a config name/index.

    CGC AMX-CTRL-4ED manual: pulse sources pp. 14–15, dual-level switches
    pp. 20–25. Arbitrary mapping, bursts and chained triggers are deliberately
    not flattened into an oscillator frequency. Vneg/Vpos are rail names, not
    voltage measurements; trilevel hardware combines pairs of these channels.
    """
    flags = snapshot.get("controller_state", {}).get("flags")
    mapping = snapshot.get("switch_mapping", {})
    period = _optional_nonnegative_int(snapshot.get("oscillator", {}).get("period"))
    period = period + 2 if period is not None and period > 0 else None
    pulsers = {p.get("pulser"): p for p in snapshot.get("pulsers", [])}
    switches = {s.get("switch"): s for s in snapshot.get("switches", [])}

    def unknown(reason: str, kind: str = "Unknown") -> dict[str, Any]:
        return {"kind": kind, "reason": reason}

    def constant(value: bool) -> dict[str, Any]:
        return {"kind": "Static", "value": value, "reason": "Constant trigger level."}

    def source(config: Any) -> dict[str, Any]:
        code = _optional_nonnegative_int(config)
        if code is None or code > 63 or code & 31 > 17:
            return unknown("Signal selection is missing or unsupported.")
        selected, inverted = code & 31, bool(code & 32)
        if selected == 0:
            return constant(inverted)
        if flags is None:
            return unknown("Controller enable flags are unavailable.")
        if selected == 1:
            return unknown("Software-triggered signal; no fixed frequency inferred.", "Software")
        if 3 <= selected <= 9:
            return unknown(f"External input DIN{selected - 3}; frequency is not measured.", "External")
        if selected >= 14:
            return unknown(f"P{selected - 14} running-state signal; waveform not inferred.")
        if "CLRN" not in flags:
            return constant(inverted)  # FPGA timing generators held in reset.
        if selected == 2:
            if "ENB_OSC" not in flags:
                return constant(inverted)
            if period is None:
                return unknown("Oscillator period is missing or invalid.")
            high, phase = 1, 0  # Oscillator emits one 10 ns tick each period.
        else:
            pulser = pulsers.get(selected - 10, {})
            width = _optional_nonnegative_int(pulser.get("width_ticks"))
            delay = _optional_nonnegative_int(pulser.get("delay_ticks"))
            trigger = _optional_nonnegative_int(pulser.get("trigger_config"))
            if "ENB_PULSER" not in flags or width == 0 or delay == 0 or trigger == 0:
                return constant(inverted)
            if width is None or delay is None or trigger is None:
                return unknown(f"Incomplete timing/routing readback for P{selected - 10}.")
            burst = pulser.get("burst")
            if selected < 12 and burst is None:
                return unknown("Burst register is unavailable.")
            if burst not in (None, 0):
                return unknown(f"P{selected - 10}: burst of {burst} pulses; not a continuous square wave.", "Burst")
            if 3 <= (trigger & 31) <= 9 and trigger <= 63:
                return unknown(f"P{selected - 10} is externally triggered; frequency is not measured.", "External")
            if trigger not in (2, 34):
                return unknown(f"P{selected - 10}: software, chained or unsupported trigger ({trigger}).")
            if "ENB_OSC" not in flags:
                return constant(inverted)
            if period is None:
                return unknown("Oscillator period is missing or invalid.")
            high = width + 2
            phase = delay + 3 + (1 if trigger == 34 else 0)
            # Non-retriggerable monoflops can skip triggers. Do not guess the
            # resulting division ratio when a cycle cannot finish in one period.
            if delay + 3 + high >= period:
                return unknown("Pulser delay + width reaches the trigger period; triggers may be skipped.")
        if inverted:
            phase, high = phase + high, period - high
        return {"kind": "Periodic", "period": period, "high": high,
                "phase": phase % period, "reason": "Periodic timing inferred from controller registers."}

    def selection_text(value: Any) -> str:
        code = _optional_nonnegative_int(value)
        if code is None or code > 63 or (code & 31) > 17:
            return "unknown"
        names = ["0", "software", "oscillator", *[f"DIN{i}" for i in range(7)],
                 *[f"P{i}" for i in range(4)], *[f"P{i} running" for i in range(4)]]
        if code == 32:
            return "1"
        return ("inverted " if code & 32 else "") + names[code & 31]

    rows = []
    for channel in range(4):
        switch = switches.get(channel, {})
        row = {"channel": f"CH{channel}", "state": "Unknown", "frequency": "Unknown",
               "levels": "Unknown", "dwell": "Unknown", "duty": "Unknown", "relation": "—", "detail": ""}
        trigger_code = _optional_nonnegative_int(switch.get("trigger_config"))
        if trigger_code is not None and trigger_code <= 63 and 10 <= trigger_code & 31 <= 13:
            row.update(source_pulser=(trigger_code & 31) - 10, source_inverted=bool(trigger_code & 32))
        if snapshot.get("device_enabled") is not True or snapshot.get("main_state", {}).get("name") != "STATE_ON":
            state_name = snapshot.get("main_state", {}).get("name") or "unknown"
            enabled_text = "enabled" if snapshot.get("device_enabled") is True else "not enabled"
            signal = unknown(f"Device is not confirmed enabled and in STATE_ON ({state_name}, device "
                             f"{enabled_text}).", "Not enabled")
        elif mapping.get("trigger_enabled") is not False or mapping.get("enable_enabled") is not False:
            signal = unknown("Switch mapping is active or unavailable; direct source routing cannot be assumed.")
        elif flags is None:
            signal = unknown("Controller enable flags are unavailable.")
        else:
            enable = constant(False) if "ENB" not in flags else source(switch.get("enable_config"))
            if enable["kind"] == "Static" and not enable["value"]:
                signal = unknown("Switch enable is low: both branches are off. Hi-Z does not imply 0 V.", "Hi-Z")
                row.update(frequency="—", levels="Hi-Z", dwell="—", duty="—")
            elif enable["kind"] != "Static":
                signal = unknown("Switch enable is variable or unknown. " + enable["reason"], "Gated")
            else:
                signal = source(switch.get("trigger_config"))
                if signal["kind"] == "Static":
                    row.update(frequency="—", levels="Vpos" if signal["value"] else "Vneg", dwell="Continuous",
                               duty="100" if signal["value"] else "0")
                elif signal["kind"] == "Periodic":
                    row.update(frequency=f"{clock_hz / signal['period'] / 1000:.6g} kHz", levels="Vneg ↔ Vpos",
                               duty=f"{100 * signal['high'] / signal['period']:.3g}")
                    delays = switch.get("trigger_delay", {})
                    if all(isinstance(delays.get(edge), int) and 0 <= delays[edge] < 16 for edge in ("rise", "fall")):
                        # Edge compensation preserves the source period. Its
                        # hardware-dependent ns step is NOT an exact phase
                        # measurement. Export registers separately so scans
                        # can freeze/check the waveform without inventing one.
                        row["waveform"] = dict(source=dict(signal), clock_hz=clock_hz,
                            trigger_config=switch.get("trigger_config"), enable_config=switch.get("enable_config"),
                            trigger_delay=dict(delays), enable_delay=switch.get("enable_delay"))
                    if delays.get("rise") == 0 and delays.get("fall") == 0:
                        row["dwell"] = f"{signal['high'] / clock_hz * 1e6:.6g} / {(signal['period'] - signal['high']) / clock_hz * 1e6:.6g}"
                        row["timing"] = signal
                    else:
                        row["dwell"] = "Delay adjusted"
                        row["duty"] = "≈" + row["duty"]
                        signal["reason"] += " Switch edge delays are nonzero or unavailable; exact dwell/phase not inferred."
        detail = (signal["reason"] + " Selected sources before mapping: trigger = "
                  + selection_text(switch.get("trigger_config")) + "; enable = "
                  + selection_text(switch.get("enable_config")) + ".")
        row.update(state=signal["kind"], detail=detail)
        rows.append(row)

    reference = next((row for row in rows if "timing" in row), None)
    if reference is not None:
        ref = reference["timing"]
        for row in rows:
            timing = row.get("timing")
            if timing is None or timing["period"] != ref["period"]:
                continue
            offset = (timing["phase"] - ref["phase"]) % ref["period"]
            if row is reference:
                row["relation"] = "Reference"
            elif offset == 0 and timing["high"] == ref["high"]:
                row["relation"] = f"Same as {reference['channel']}"
            elif offset == ref["high"] and timing["high"] == ref["period"] - ref["high"]:
                row["relation"] = f"Opposite to {reference['channel']}"
            else:
                row["relation"] = f"+{offset / clock_hz * 1e6:.6g} µs vs {reference['channel']}"
    return rows


def _expected_oscillator_period(
    frequency_khz: float,
    *,
    clock_hz: float = 100e6,
    oscillator_offset: int = 2,
) -> int | None:
    """Return the period register written by the bundled AMX driver."""
    frequency_hz = float(frequency_khz) * 1000.0
    if not np.isfinite(frequency_hz) or frequency_hz <= 0.0:
        return None
    period = round((float(clock_hz) / frequency_hz) - int(oscillator_offset))
    return period if 1 <= period <= 0xFFFFFFFF else None


def _pulser_follows_oscillator(controller: Any, pulser: int) -> bool:
    """Only an oscillator-triggered pulser runs at the oscillator period.

    Unknown routing (disconnected) counts as the oscillator: ON reloads the config anyway.
    """
    triggers = getattr(controller, "pulser_triggers", None) or {}
    trigger = _optional_nonnegative_int(triggers.get(pulser))
    return trigger is None or trigger & 31 == 2


def _state_is_standby(state: Any) -> bool:
    normalized = _normalize_runtime_state(state).strip().upper()
    return "STBY" in normalized or "STANDBY" in normalized


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
        module = importlib.import_module(f"{runtime_module_name}.amx")
        _AMX_DRIVER_CLASS = cast(type[Any], module.AMX)
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
    """The AMX is driven by its own Rust worker only; there is no Python DLL path in the plugin."""
    backend = _native_backend(device)
    if backend is None:
        raise RuntimeError("AMX communication requires its native worker.")
    return backend


def providePlugins() -> "list[type[Plugin]]":
    return [AMXDevice]


# The code shared word for word with the other AMX plugins, from this plugin folder's own copy of
# _runtime/_amx_common.py, run in this module as if written here (tools/sync_family_siblings.py keeps the
# copies equal). It is not at the folder root: Explorer imports every .py file there on its own.
_AMX_COMMON = Path(__file__).parent / "_runtime" / "_amx_common.py"
if not _AMX_COMMON.is_file():
    raise ModuleNotFoundError(f"Missing _runtime/{_AMX_COMMON.name} in {_AMX_COMMON.parent.parent}: "
                              "plugin installation is incomplete.")
exec(compile(_AMX_COMMON.read_text(encoding="utf-8"), str(_AMX_COMMON), "exec"), globals())  # noqa: S102


class AMXDevice(_AMXDeviceCommon, Device):
    """Drive AMX oscillator and pulser timing from ESIBD Explorer."""

    LiveDisplay = _LiveDisplay  # Last 30 min by default.

    documentation = (
        "Drives AMX frequency and pulser timing while monitoring pulser readbacks."
    )

    name = "AMX_B"
    version = "0.1.0"
    supportedVersion = "1.0.2"
    pluginType = PLUGINTYPE.INPUTDEVICE
    log = False  # Explorer's per-device Logging switch is removed: nothing here reads it.
    unit = "%"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "amx.png"
    channels: "list[AMXChannel]"

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
        self.channelType = AMXChannel

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
        self.controller = AMXController(controllerParent=self)

    def finalizeInit(self) -> None:
        super().finalizeInit()
        self._timing_basis_khz = _coerce_float(getattr(self, "frequency_khz", 2.0), 2.0)
        _session_start(self)  # Resumes a device left ON by an Explorer crash.
        if hasattr(self, "advancedAction"):
            self.advancedAction.toolTipFalse = (
                f"Show the advanced channel table, equations, and layout actions for {self.name}."
            )
            self.advancedAction.toolTipTrue = (
                f"Hide the advanced channel table and return to the {self.name} operator panel."
            )
            self.advancedAction.setToolTip(self.advancedAction.toolTipFalse)
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_config_controls()
        self._ensure_operator_panel()
        self._update_channel_column_visibility()
        self._update_channel_table_visibility()
        self._sync_acquisition_controls()

    def getChannels(self) -> "list[AMXChannel]":
        return cast("list[AMXChannel]", super().getChannels())

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
            return _coerce_int(getattr(self, f"_{attr_name}_backup", -1), -1)
        backup = getattr(self, f"_{attr_name}_backup", None)
        if backup is not None:
            return _coerce_int(backup, -1)
        return _coerce_int(getattr(self, attr_name, -1), -1)

    def _set_config_setting_value(self, attr_name: str, value: int) -> bool:
        value = _coerce_int(value, -1)
        backup_attr = f"_{attr_name}_backup"
        previous = _coerce_int(getattr(self, backup_attr, None), None)
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
        self.frequencyLabel = label_type("Osc:")
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
                "Internal oscillator frequency, not necessarily the frequency of each output. "
                "See CH0–CH3 for the signal inferred from routing. "
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
            )
        )
        return "\n".join(lines)

    def _channel_by_pulser(self, pulser: int) -> "AMXChannel | None":
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

        delay = str(delay_values.get(pulser, "n/a") or "n/a") if connected else "n/a"
        delay_text = f"{delay} µs" if delay != "n/a" else delay
        burst_text = str(burst_values.get(pulser, "n/a") or "n/a") if connected else "n/a"
        expected_period_text = "n/a" if expected_period is None else str(expected_period)
        observed_period_text = "n/a" if observed_period is None else str(observed_period)
        observed_width_text = "n/a" if observed_width is None else str(observed_width)
        channel_name = str(getattr(channel, "name", f"P{pulser}") or f"P{pulser}")
        tooltip = "\n".join(
            (
                f"{channel_name} register verification",
                f"Requested: {'enabled' if requested_enabled else 'disabled'}, "
                f"width {requested_width_us:.3f} µs",
                f"Expected / observed width register: {expected_width} / {observed_width_text}",
                f"Expected / observed oscillator period: {expected_period_text} / {observed_period_text}",
                "Readbacks come from AMX controller registers; they do not prove the physical waveform.",
            )
        )
        controls_enabled = channel is not None and not busy
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
            "title": f"Pulser P{pulser}",
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
            "tooltip": tooltip,
        }

    def _ensure_operator_panel(self) -> None:
        """Outputs, then one timing row per used pulser; diagnostics in Advanced."""
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
        self.amxPanelFrequencyValue = role_label("Oscillator: n/a", "value")
        layout.addWidget(self.amxPanelSignalValue)
        layout.addWidget(self.amxPanelPendingValue)
        layout.addWidget(self.amxPanelFrequencyValue)

        table = QTableWidget(4, len(_AMX_OUTPUT_COLUMNS))
        table.setHorizontalHeaderLabels([header for _, header in _AMX_OUTPUT_COLUMNS])
        for column, (key, _header) in enumerate(_AMX_OUTPUT_COLUMNS):
            table.horizontalHeaderItem(column).setToolTip(_AMX_OUTPUT_COLUMN_TIPS[key])
        for row in range(4):
            for column in range(len(_AMX_OUTPUT_COLUMNS)):
                table.setItem(row, column, QTableWidgetItem(f"CH{row}" if column == 0 else "—"))
        _configure_panel_table(table)
        table.setToolTip(
            "CH0–CH3 are the controller's dual-level switch channels. "
            "On trilevel hardware, CH0/1 and CH2/3 each control one physical output. "
            "Levels below assume dual-level switches. Hi-Z never implies a discharged output.\n"
            + _AMX_PANEL_NOTICE
        )
        self.amxOutputTable = table
        layout.addWidget(table)

        # One row per pulser; the operator view hides the unused ones.
        timing = QTableWidget(len(_AMX_PULSER_IDS), len(_AMX_TIMING_COLUMNS))
        timing.setHorizontalHeaderLabels([header for _, header in _AMX_TIMING_COLUMNS])
        for column, (key, _header) in enumerate(_AMX_TIMING_COLUMNS):
            timing.horizontalHeaderItem(column).setToolTip(_AMX_TIMING_COLUMN_TIPS[key])
        self.amxTimingRows: dict[int, dict[str, Any]] = {}
        for row, pulser in enumerate(_AMX_PULSER_IDS):
            items = {key: QTableWidgetItem("—") for key in ("name", "drives", "status")}
            items["name"].setText(f"P{pulser}")
            for key, column in (("name", 0), ("drives", 1), ("status", 6)):
                timing.setItem(row, column, items[key])
            enable_button = QPushButton(_AMX_PULSER_OFF_LABEL)
            enable_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            enable_button.setCheckable(True)
            enable_button.setToolTip(
                "Enable or stop this pulser while AMX is ON. Loading a config replaces this request. "
                "Pulser numbers are not output channel numbers."
            )
            width_widget = _amx_number_field()
            width_widget.setRange(0.0, 1_000_000.0)
            width_widget.setDecimals(3)
            width_widget.setSingleStep(0.01)
            width_widget.setSuffix(" µs")
            duty_widget = _amx_number_field()
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
            signal_label.setText(f"Loaded config: {loaded_config}{edited}")
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
            clock_hz, osc_offset, _, _ = self._timing_constants()
            expected_period = _expected_oscillator_period(
                frequency_request, clock_hz=clock_hz, oscillator_offset=osc_offset)
            observed_period = _optional_nonnegative_int(getattr(controller, "oscillator_period", None))
            differs = known and observed_period is not None and expected_period != observed_period
            parts = [f"Oscillator: requested {frequency_request:.3f} kHz"]
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
            frequency_label.setText(" · ".join(parts))
            _amx_set_role(frequency_label, "warning" if differs else "value")
            frequency_label.setToolTip(
                "Requested oscillator frequency, the frequency the controller runs on its clock "
                "grid, and whether the controller period register holds it."
            )

        advanced = bool(getattr(getattr(self, "advancedAction", None), "state", False))
        snapshots = {pulser: self._pulser_panel_snapshot(pulser) for pulser in rows}
        used = {pulser for pulser, snapshot in snapshots.items()
                if snapshot.get("drives_outputs") or snapshot["requested_enabled"]}
        shown = set(rows) if advanced or not used else used
        for pulser, widgets in rows.items():
            snapshot = snapshots[pulser]
            tooltip = f"{snapshot['status_text']} · {snapshot['mode_text']}\n{snapshot['tooltip']}"
            for key, text in (("name", f"P{pulser}"), ("drives", snapshot.get("drives_text", "")),
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
            hidden = [f"P{pulser}" for pulser in rows if pulser not in shown]
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
            maximum=255,
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
                    "PSU physically feeding this AMX output pair. Read-only association: "
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
        main_state = str(getattr(controller, "main_state", "Disconnected") or "Disconnected")
        if not _state_is_on(main_state):
            return False, f"state is {main_state}"
        return True, ""


class AMXChannel(_AMXChannelCommon, Channel):
    """AMX pulser channel definition."""

    ID = "Pulser"
    DELAY_US = "Delay us"
    WIDTH_TICKS = "Width ticks"
    BURST = "Burst"
    DUTY = "Duty"
    FREQ_KHZ = "Freq kHz"
    KEEP = "Keep"
    channelParent: AMXDevice

    def _apply_value_now(self) -> None:
        apply_value = getattr(self, "applyValue", None)
        if not callable(apply_value) or getattr(self.channelParent, "loading", False):
            return
        controller = getattr(getattr(self, "channelParent", None), "controller", None)
        is_on = getattr(getattr(self, "channelParent", None), "isOn", None)
        if (
            controller is None
            or not getattr(controller, "initialized", False)
            or not callable(is_on)
            or not bool(is_on())
        ):
            return
        apply_thread = getattr(controller, "applyValueFromThread", None)
        if callable(apply_thread):
            apply_thread(self, parallel=True)
            return
        apply_value(apply=True)


class AMXController(_AMXControllerCommon, DeviceController):
    """AMX hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: AMXDevice

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
        self.initialized = False
        self._initial_open_close_requested = False
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._global_apply_state_lock = Lock()
        self._global_apply_pending = False
        self._global_apply_worker_running = False
        self._channel_apply_state_lock = Lock()
        self._channel_apply_pending: dict[int, AMXChannel] = {}
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
        self.pulser_triggers: dict[int, int | None] = {}
        # Live routing for the timing cards; timing writes do not change it.
        self.pulser_outputs: dict[int, list[tuple[int, bool]]] = {}
        self._delay_anchors: dict[int, tuple[int, int, int, int]] = {}
        self.frequency_readback_khz = np.nan
        self.output_rows: list[dict[str, Any]] | None = None

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
            self.pulser_triggers = {}
            self.pulser_outputs = {}
            self.frequency_readback_khz = np.nan
            self.output_rows = None

    def runInitialization(self) -> None:
        if self._initial_open_close_requested:
            self.initializing = False
            return
        # False without a device only means that the last OFF was not confirmed: no port is held, so
        # the operator may reconnect (2026-11-02 lab: ON did nothing after "Communication lost").
        if self._dispose_device() is False and self.device is not None:
            self.initializing = False
            self.print("AMX connection refused: the previous connection still owns its port.", flag=PRINT.WARNING)
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
        # standby, no need to reload it. Just disconnect.
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
            # Crash resume: no config load, no enable, no timing change; the AMX is read as it runs.
            Thread(target=self._resume_without_commands, name=f"{self.controllerParent.name} resume",
                   daemon=True).start()
            return
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
                snapshot = self._collect_startup_snapshot()
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

        try:
            configs = self._device_call(device, "list_configs",
                timeout_s=float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
            )
        except Exception as exc:  # noqa: BLE001
            if self._retire_failed_native_worker() or self._native_cancel_event.is_set():
                raise
            self.available_configs = []
            self.available_configs_text = "Unavailable"
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
        frequency_khz = clock_hz / (period + _safe_device_attr(device, "OSC_OFFSET", 2)) / 1000.0
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
        return self._device_call(device, "collect_housekeeping", timeout_s=self._startup_snapshot_timeout_s())

    def _startup_snapshot_ready(self, snapshot: dict[str, Any]) -> bool:
        state = str(snapshot.get("main_state", {}).get("name", "") or "")
        return bool(snapshot.get("device_enabled", False)) and _state_is_on(state)

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
        return (
            f"AMX did not reach STATE_ON after loading config {config_index}: "
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
            if self._startup_snapshot_ready(snapshot):
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

        # DeviceEnable only allows a subsequent config load to restore Enb.
        started_s = time.monotonic()
        self._device_call(device, "set_device_enabled", True, timeout_s=timeout_s)
        self._print_if_slow("AMX set_device_enabled(ON)", started_s)
        try:
            if load_config_first:
                started_s = time.monotonic()
                self._device_call(device, "load_config", config_index, timeout_s=timeout_s)
                self._print_if_slow(f"AMX load_config({config_index})", started_s)
                self._loaded_config_index = config_index
                started_s = time.monotonic()
                self._refresh_loaded_config_status()
                self._print_if_slow("AMX loaded-config refresh", started_s)
            snapshot = self._wait_for_startup_ready_snapshot(
                config_index=config_index,
                settle_timeout_s=timeout_s,
            )
            restored = None
            if restore_timing and load_config_first:
                snapshot, restored = self._restore_last_timing(device, snapshot, config_index, timeout_s)
            self._sync_loaded_config_to_gui(snapshot, restored=restored)
            return snapshot
        except Exception:
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

        # Guard against concurrent Load now calls.
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
                # OFF during the load ("Operation cancelled"): not a failure.
                self.print(f"AMX config {config_index} not loaded: OFF was requested.")
                return
            self.errorCount += 1
            self.print(
                f"Failed to load AMX config {config_index}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            if self._initial_open_close_requested and self.device is not None:
                self.shutdownCommunication()
            self._end_transition()
            self._resume_after_load(recording)
            self._sync_status_to_gui()

    def _apply_channel_timing(self, channel: AMXChannel, timeout_s: float) -> None:
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

    def applyValueFromThread(self, channel: AMXChannel, parallel: bool = True) -> None:
        def start() -> None:
            finish = getattr(self.controllerParent, "_finish_setpoint_edits", None)
            if callable(finish) and channel.enabled:
                finish(channel)
            if parallel:
                self._queue_channel_timing_apply(channel)
            else:
                self.applyValue(channel)
        _invoke_gui_callback(start)

    def _queue_channel_timing_apply(self, channel: AMXChannel) -> None:
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
        """Restart acquisition after a transition without joining the old thread."""
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
            snapshot_frequency_hz / 1000.0
            if np.isfinite(snapshot_frequency_hz)
            else clock_hz / total_ticks / 1000.0
            if total_ticks > 0
            else np.nan
        )

        new_values: dict[int, float] = {}
        new_width_ticks: dict[int, str] = {}
        new_delay_us: dict[int, str] = {}
        new_bursts: dict[int, str] = {}
        new_triggers: dict[int, int | None] = {}

        for pulser_snapshot in snapshot.get("pulsers", []):
            pulser = _coerce_int(pulser_snapshot.get("pulser"), -1)
            if pulser < 0:
                continue
            new_triggers[pulser] = _optional_nonnegative_int(pulser_snapshot.get("trigger_config"))
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

        self.output_rows = _amx_output_rows(snapshot, clock_hz)
        pulser_outputs: dict[int, list[tuple[int, bool]]] = {}
        for index, row in enumerate(self.output_rows):
            if row["state"] == "Periodic" and "source_pulser" in row:
                pulser_outputs.setdefault(row["source_pulser"], []).append((index, row["source_inverted"]))
        self.pulser_outputs = pulser_outputs
        self.values = new_values
        self.width_values = new_width_ticks
        self.delay_values = new_delay_us
        self.burst_values = new_bursts
        self.pulser_triggers = new_triggers
        self._sync_status_to_gui()

    def applyValue(self, channel: AMXChannel) -> None:
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
                        if _state_is_on(getattr(self, "main_state", ""))
                        else " Use Close communication to disconnect."
                    )
                    self.print(
                        f"Cannot start {self.controllerParent.name}: {reason}."
                        f" Select a valid Operating config.{state_note}",
                        flag=PRINT.WARNING,
                    )
                    return
                self._start_operating_mode(
                    config_index=config_index,
                    timeout_s=timeout_s,
                    lock_message="Could not acquire lock to start the AMX.",
                    restore_timing=True,
                    success_message="AMX timing enabled.",
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
            if target_on and not (self._initial_open_close_requested and self.device is not None):
                self._restore_ui_state_for_device()
            self.print(
                f"Failed to toggle AMX: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            if target_on and self._initial_open_close_requested and self.device is not None:
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
        self.pulser_outputs = {}
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
        genuinely STATE_ON we keep the button ON (so the next click toggles to
        OFF -> shutdownCommunication); otherwise we restore OFF as before.
        ``main_state`` is the authoritative controller value, refreshed by the
        transport-init ``_update_state``; it never crosses the controller/device
        boundary, and defaults to a non-ON string (no None risk).
        """
        if self._retire_failed_native_worker():
            return
        if _state_is_on(getattr(self, "main_state", "")):
            self._restore_on_ui_state()
        else:
            self._restore_off_ui_state()

    def _begin_transition(self, target_on: bool) -> bool:
        with self._transition_guard():
            if self.transitioning:
                return False
            self.transitioning = True
            self.output_rows = None
            self.pulser_outputs = {}
            self.transition_target_on = bool(target_on)
            return True


