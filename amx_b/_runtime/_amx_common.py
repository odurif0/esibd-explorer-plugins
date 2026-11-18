"""Code shared word for word by the AMX plugins (amx_a, amx_b, amx_hd).

Each plugin folder holds its own identical copy (tools/sync_family_siblings.py copies it from amx_a;
tests check the copies). The plugin runs it in its own module namespace, as if it were written there,
just before its own Device, Channel and Controller classes, which list the mixins defined here first in
their bases. Names used here come from the plugin (its imports, constants and functions, some of them
specific to AMX or AMX HD), so each plugin stays independent. Not importable on its own, hence in
_runtime/: Explorer imports every .py file at the root of a plugin folder.
Edit it in amx_a only, then run tools/sync_family_siblings.py.
"""

_BUNDLED_RUNTIME_DIRNAME = "runtime"
_AMX_DRIVER_CLASS: type[Any] | None = None
# Serializes the private runtime load and driver-class publish across threads.
_RUNTIME_LOAD_LOCK = RLock()
_GUI_DISPATCH_LOCK = RLock()
_CHANNEL_NAME_KEY = getattr(Parameter, "NAME", getattr(Channel, "NAME", "Name"))
_CHANNEL_ENABLED_KEY = getattr(Channel, "ENABLED", "Enabled")
_CHANNEL_REAL_KEY = getattr(Channel, "REAL", "Real")
_PARAMETER_MIN_KEY = getattr(Parameter, "MIN", "Min")
_PARAMETER_MAX_KEY = getattr(Parameter, "MAX", "Max")
_PARAMETER_ADVANCED_KEY = getattr(Parameter, "ADVANCED", "Advanced")
_PARAMETER_TOOLTIP_KEY = getattr(Parameter, "TOOLTIP", "Tooltip")
_PARAMETER_EVENT_KEY = getattr(Parameter, "EVENT", "Event")
_AMX_PULSER_KEY = "Pulser"
_AMX_PULSER_IDS = (0, 1, 2, 3)
_AMX_POWER_ON_ICON = "switch-medium_on.png"
_AMX_POWER_OFF_ICON = "switch-medium_off.png"
_AMX_PULSER_ON_LABEL = "ON"
_AMX_PULSER_OFF_LABEL = "OFF"
_AMX_PULSER_TOGGLE_MIN_WIDTH = 48
_AMX_MIN_ROW_HEIGHT = 28
_AMX_SHUTDOWN_UNCONFIRMED_STATE = "Shutdown unconfirmed"
_AMX_MONITOR_OK_STYLE = "background-color: #2f855a; color: #ffffff; margin:0px;"
_AMX_MONITOR_WARN_STYLE = "background-color: #dd6b20; color: #ffffff; margin:0px;"
_AMX_MONITOR_ERROR_STYLE = "background-color: #c53030; color: #ffffff; margin:0px;"
_AMX_MONITOR_NEUTRAL_STYLE = ""
_AMX_MONITOR_OK_RELATIVE_TOLERANCE = 0.01
_AMX_MONITOR_WARN_RELATIVE_TOLERANCE = 0.10
_AMX_MONITOR_RELATIVE_FLOOR_DUTY = 1.0
_AMX_NUMERIC_DEBOUNCE_MS = 250
# What a pulser keeps when the oscillator frequency changes (channel "Keep").
# No "%" in stored values: Explorer's INI files use configparser interpolation.
_AMX_KEEP_DUTY = "duty"
_AMX_KEEP_WIDTH = "width"
_AMX_MAX_CONSECUTIVE_POLL_ERRORS = 10
_AMX_SLOW_OPERATION_LOG_THRESHOLD_S = 1.0
_AMX_STARTUP_PROGRESS_LOG_INTERVAL_S = 1.0
# Text colors per Explorer theme, (dark, light). Explorer switches the application
# palette; fixed light text vanished on its light theme (2026-10-13).
_AMX_PANEL_COLORS = {
    "title": ("#f8fafc", "#1a202c"),
    "name": ("#94a3b8", "#4a5568"),
    "value": ("#e2e8f0", "#1a202c"),
    "notice": ("#94a3b8", "#4a5568"),
    "warning": ("#f6ad55", "#b7791f"),
    "error": ("#fc8181", "#c53030"),
    "muted": ("#718096", "#718096"),
    "trace": ("#68d391", "#2f855a"),
    "grid": ("#3f4a5a", "#d5dbe3"),
}
# Signal column colors: inferred waveforms stay plain, anything not inferred is flagged.
_AMX_SIGNAL_ROLES = {
    "Periodic": None, "Static": None, "Hi-Z": "muted",
    "Disconnected": "muted", "OFF": "muted", "Standby": "muted", "Updating": "muted",
    "Awaiting readback": "muted", "Not enabled": "muted",
    _AMX_SHUTDOWN_UNCONFIRMED_STATE: "error", "Error": "error",
}
# Pulser register states (see _pulser_panel_snapshot) as panel text roles.
_AMX_STATUS_ROLES = {"active": "trace", "warning": "warning", "error": "error", "off": "muted",
                     "disconnected": "muted"}
_AMX_OUTPUT_COLUMNS = (
    ("channel", "Output"), ("state", "Signal"), ("frequency", "Frequency"), ("levels", "Levels"),
    ("psu", "PSU"), ("dwell", "Vpos / Vneg\n(µs)"), ("duty", "Vpos\n(%)"), ("relation", "Relative timing"),
)
_AMX_OUTPUT_COLUMN_TIPS = {
    "channel": "Controller switch channel.",
    "state": "Signal inferred from the live routing registers, never from the config name. "
             "Amber: not inferred (external, burst, mapping, software trigger...).",
    "frequency": "Output frequency from the source period register.",
    "levels": "Rails the output switches between; with a linked PSU, its measured voltages "
              "relative to the PSU reference.",
    "psu": "PSU linked to this output pair in Settings. Describes the wiring only; never controls the PSU.",
    "dwell": "Time at Vpos / Vneg in each period, only when the switch edge delays are zero.",
    "duty": "Share of each period at Vpos (≈ when switch edge delays shift the edges). 50 is symmetric.",
    "relation": "Same, opposite or delayed relative to the first periodic output.",
}
_AMX_WAVEFORM_PERIODS = 2
_AMX_PANEL_NOTICE = (
    "Expected signals, not waveform measurements. Voltages from linked PSUs, "
    "relative to their reference (external offset not included)."
)
_AMX_LOAD_PENDING_STYLE = (
    "QPushButton { background-color: #b7791f; color: white; border: 1px solid #f6ad55; "
    "border-radius: 3px; padding: 2px 8px; font-weight: 700; }"
)
_AMX_WAVEFORM_MAX_EDGES = 400
_AMX_PANEL_ENABLE_ACTIVE_STYLE = (
    "QPushButton { background-color: #2f855a; color: white; border: 1px solid #68d391; "
    "border-radius: 4px; padding: 4px 8px; font-weight: 700; }"
)
_AMX_PANEL_ENABLE_OFF_STYLE = (
    "QPushButton { background-color: #334155; color: #cbd5e1; border: 1px solid #64748b; "
    "border-radius: 4px; padding: 4px 8px; font-weight: 600; }"
)
# A call that times out stops the plugin's native worker, which releases the COM
# port, and the next explicit ON starts a fresh worker (2026-10-28 lab: AMX_B
# reopened COM11 a minute after a blocked Open, in the same Explorer session).
# The guidance below says so; it used to ask for an Explorer restart.
_AMX_POISONED_PORT_RECOVERY = (
    "The native worker that held the COM port was stopped, which released the port: "
    "Explorer does not need a restart. Check that the device is powered and connected, "
    "then retry ON."
)
_AMX_POISONED_PORT_RETRY = (
    "An earlier connection attempt on this port timed out and its worker was stopped, "
    "so this Explorer no longer holds the port. Check that the device is powered and "
    "connected, and that no other program has the port open."
)
# While waiting between polls, re-check OFF this often; always leave the
# controller lock free for at least the minimum gap between two cycles.
_POLL_STOP_CHECK_S = 0.05
_POLL_MIN_GAP_S = 0.02
_LIVE_PLOT_MINUTES = "30"


def _amx_transport_failure_is_fatal(exc: Exception) -> bool:
    """Return True when an exception clearly indicates a dead AMX transport."""
    text = str(exc).strip().lower()
    if not text:
        return False
    return any(
        token in text
        for token in (
            "unusable",
            "transport is unusable",
            "transport became unusable",
            "marked unusable",
            "worker became unusable",
            "worker is unavailable",
            "worker exited unexpectedly",
            "worker timed out",
            "worker is no longer running",
        )
    )


def _is_nan(value: Any) -> bool:
    try:
        return bool(np.isnan(value))
    except TypeError:
        return False


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _expected_width_register(
    enabled: bool,
    width_us: float,
    *,
    clock_hz: float = 100e6,
    width_offset: int = 2,
) -> int:
    """Return the width register written by the existing channel apply path."""
    width = float(width_us)
    if not enabled or not np.isfinite(width) or width <= 0.0:
        return 0
    ticks_per_us = float(clock_hz) / 1e6
    return max(round(width * ticks_per_us) - int(width_offset), 1)


def _width_register_us(
    width_register: int,
    *,
    clock_hz: float = 100e6,
    width_offset: int = 2,
) -> float:
    """Convert one nonzero width register to controller timing in microseconds."""
    width = max(int(width_register), 0)
    if width == 0:
        return 0.0
    return (width + int(width_offset)) / (float(clock_hz) / 1e6)


def _period_ticks(frequency_khz: float, *, clock_hz: float = 100e6, oscillator_offset: int = 2) -> int | None:
    """Oscillator period in clock ticks (register + offset) for a requested frequency."""
    period = _expected_oscillator_period(frequency_khz, clock_hz=clock_hz, oscillator_offset=oscillator_offset)
    return None if period is None else period + int(oscillator_offset)


def _rescale_ticks(
    anchor: tuple[int, int, int, int] | None, ticks: int, old_period: int, new_period: int,
    *, minimum: int, maximum: int,
) -> tuple[int, tuple[int, int, int, int]]:
    """Keep a timing's fraction of the period on the clock grid.

    2026-10-22: config 79 (500 kHz, P0 high 100 of 200 ticks) moved to 450 kHz kept
    1.00 µs high in a 2.22 µs period, an asymmetric 45 % switch.
    The anchor is the timing an edit or a config set, so rounding never accumulates:
    it stays valid while the ticks and period are the ones the last rescale wrote.
    """
    origin = anchor[:2] if anchor is not None and anchor[2:] == (ticks, old_period) else (ticks, old_period)
    kept = min(max(round(origin[0] * new_period / origin[1]), minimum), maximum)
    return kept, (*origin, kept, new_period)


def _duty_percent(width_us: float, period: int | None, ticks_per_us: float) -> float:
    """High time of a requested width as a share of the oscillator period, on the clock grid."""
    width = _coerce_float(width_us, 0.0)
    if not period or width <= 0.0:
        return 0.0
    return round(width * ticks_per_us) / period * 100.0


def _keeps_duty(channel: Any) -> bool:
    return getattr(channel, "keep", _AMX_KEEP_DUTY) != _AMX_KEEP_WIDTH


def _requested_duty_percent(parent: Any, width_us: float) -> float:
    controller = getattr(parent, "controller", None)
    device = getattr(controller, "device", None) if controller is not None else None
    clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
    period = _period_ticks(_coerce_float(getattr(parent, "frequency_khz", 2.0), 2.0), clock_hz=clock_hz,
                           oscillator_offset=_coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2))
    return _duty_percent(width_us, period, clock_hz / 1e6)


def _amx_panel_badge_style(kind: str) -> str:
    colors = {
        "active": "#2f855a",
        "warning": "#b7791f",
        "error": "#c53030",
        "off": "#4a5568",
        "disconnected": "#718096",
    }
    background = colors.get(kind, colors["disconnected"])
    return (
        "QLabel {"
        f" background-color: {background};"
        " color: white;"
        " border-radius: 3px;"
        " padding: 2px 6px;"
        " font-weight: 700;"
        " }"
    )


def _amx_panel_color(role: str, dark: bool) -> str:
    return _AMX_PANEL_COLORS[role][0 if dark else 1]


def _amx_panel_stylesheet(dark: bool) -> str:
    """Text styles of the whole operator panel, selected by each label's ``amxRole``."""
    color = lambda role: _amx_panel_color(role, dark)
    return (
        f'QLabel[amxRole="title"] {{ color: {color("title")}; font-size: 14px; font-weight: 700; }}'
        f'QLabel[amxRole="name"] {{ color: {color("name")}; font-weight: 600; }}'
        f'QLabel[amxRole="value"] {{ color: {color("value")}; font-weight: 600; }}'
        f'QLabel[amxRole="notice"] {{ color: {color("notice")}; font-style: italic; }}'
        f'QLabel[amxRole="warning"] {{ color: {color("warning")}; font-weight: 600; }}'
    )


def _color_table_item(item: Any, role: str | None, dark: bool) -> None:
    """Text colour of a panel table cell; None keeps the palette's plain text."""
    if not hasattr(item, "setData"):
        return
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor

    item.setData(Qt.ItemDataRole.ForegroundRole, None if role is None else QColor(_amx_panel_color(role, dark)))


def _amx_dark_theme() -> bool:
    """Follow the application palette, which Explorer switches with its theme."""
    try:
        from PyQt6.QtGui import QGuiApplication, QPalette
    except ImportError:
        return True
    if QGuiApplication.instance() is None:
        return True
    return QGuiApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128


def _amx_set_role(widget: Any, role: str) -> None:
    """Restyle a panel label; a changed dynamic property needs a re-polish."""
    if widget is None or not hasattr(widget, "setProperty") or widget.property("amxRole") == role:
        return
    widget.setProperty("amxRole", role)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _amx_waveform_traces(rows: list[dict[str, Any]] | None, clock_hz: float = 100e6) -> dict[str, Any]:
    """Expected CH0–3 levels over two periods of the reference output.

    Built from the routing summary only. Square waves need exact inferred timing;
    constant rails are flat, Hi-Z is marked, and every other row stays a labelled
    gap rather than a guessed waveform. Time 0 is the reference's switch to Vpos.
    """
    rows = list(rows or [])
    clock_of = lambda timing: _coerce_float(timing.get("clock_hz", clock_hz), clock_hz)
    reference = next((row["timing"] for row in rows if isinstance(row.get("timing"), dict)), None)
    window_us = origin_us = None
    if reference is not None:
        scale = 1e6 / clock_of(reference)
        window_us = _AMX_WAVEFORM_PERIODS * reference["period"] * scale
        origin_us = reference["phase"] * scale
    traces = []
    for row in rows:
        timing = row.get("timing")
        state = str(row.get("state", "Unknown"))
        trace: dict[str, Any] = {"channel": str(row.get("channel", "")), "kind": "none", "label": state}
        if state == "Static" and row.get("levels") in ("Vpos", "Vneg"):
            trace.update(kind="level", high=row["levels"] == "Vpos", label=f"{row['levels']} (constant)")
        elif state == "Hi-Z":
            trace.update(kind="hiz", label="Hi-Z (not 0 V)")
        elif isinstance(timing, dict) and window_us:
            scale = 1e6 / clock_of(timing)
            period, high = timing["period"] * scale, timing["high"] * scale
            aligned = timing["period"] == reference["period"] and clock_of(timing) == clock_of(reference)
            if 2 * window_us / period > _AMX_WAVEFORM_MAX_EDGES:
                trace.update(kind="dense", label=f"{row.get('frequency', '')}: too fast to draw at this scale")
            else:
                start = timing["phase"] * scale - origin_us
                first = int(np.floor(-(start + high) / period))
                segments = []
                for cycle in range(first, int(np.ceil((window_us - start) / period)) + 1):
                    begin, end = start + cycle * period, start + cycle * period + high
                    begin, end = max(begin, 0.0), min(end, window_us)
                    if end > begin:
                        segments.append((begin, end))
                trace.update(kind="wave", segments=segments, label="" if aligned else "own period: phase not related")
        elif state == "Periodic":
            trace["label"] = "Periodic: edge delays adjusted, not drawn"
        traces.append(trace)
    return {"window_us": window_us, "traces": traces}


def _safe_device_attr(device: Any, name: str, default: Any) -> Any:
    """Read a timing constant (CLOCK, *_OFFSET) without a worker round trip.

    The output panel calls this on the GUI thread every 500 ms. On the native
    facade a plain getattr was an IPC request: it waited behind any running
    operation (up to 5 s behind a blocked Open), freezing the GUI, then raised
    TimeoutError "operation queue timed out" (lab log 2026-10-20). The runtime
    class holds these constants; when it lacks one the default applies, as the
    worker's answer did.
    """
    runtime_class = getattr(type(device), "_PROCESS_CONTROLLER_CLASS", None)
    if runtime_class is not None:
        value = getattr(runtime_class, name, default)
        return default if callable(value) else value
    try:
        return getattr(device, name, default)
    except (RuntimeError, TimeoutError):
        return default


def _compact_status_text(value: Any, default: str = "n/a") -> str:
    """Return a short one-line representation for toolbar status widgets."""
    if value is None:
        return default
    text = str(value).strip()
    if not text:
        return default
    normalized = text.replace(";", ",")
    parts = [part.strip() for part in normalized.split(",") if part.strip()]
    if len(parts) <= 1:
        return text
    return f"{parts[0]} +{len(parts) - 1}"


def _normalize_runtime_state(state: Any) -> str:
    """Normalize fallback transport states into operator-facing labels."""
    text = str(state or "").strip()
    if not text:
        return "Disconnected"
    normalized = text.lower()
    if normalized in {"false", "disconnected"}:
        return "Disconnected"
    if normalized in {"true", "connected"}:
        return "Connected"
    return text


def _status_tokens(value: Any) -> set[str]:
    """Split comma/semicolon separated status text into normalized tokens."""
    if value is None:
        return set()
    normalized = str(value).replace(";", ",")
    return {
        token.strip()
        for token in normalized.split(",")
        if token.strip()
    }


def _status_requires_operator_attention(state: Any) -> bool:
    """Return True when the raw state describes a fault or uncertain condition."""
    normalized = str(state or "").strip().lower()
    return any(
        token in normalized
        for token in ("err", "error", "fail", "fault", "lost", "overload", "timeout", "unknown")
    )


def _finish_spinbox_edit(widget: Any) -> bool:
    """Read the focused editor on the GUI thread, without sending commands."""
    if widget is None or not getattr(widget, "hasFocus", lambda: False)():
        return False
    blocked = widget.blockSignals(True)
    try:
        widget.interpretText()
    finally:
        widget.blockSignals(blocked)
    return True


def _invoke_gui_callback(callback: Any) -> None:
    """Run on the GUI thread; use a direct fallback only without a Qt app."""
    if not callable(callback):
        return
    try:
        from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        callback()
        return

    app = QApplication.instance()
    if app is None:
        callback()
        return

    try:
        on_gui_thread = QThread.currentThread() == app.thread()
    except Exception:
        on_gui_thread = False
    if on_gui_thread:
        try:
            callback()
        except Exception:
            logging.getLogger(__name__).exception("GUI update failed.")
        return

    try:
        with _GUI_DISPATCH_LOCK:
            dispatcher = getattr(_invoke_gui_callback, "_dispatcher", None)
            if dispatcher is None:
                class _CallbackDispatcher(QObject):
                    callbackRequested = pyqtSignal(object)

                    def __init__(self) -> None:
                        super().__init__()
                        self.callbackRequested.connect(
                            self._run,
                            Qt.ConnectionType.QueuedConnection,
                        )

                    # A real Qt slot follows QObject affinity after moveToThread;
                    # a Python callable proxy may remain on the creating thread.
                    @pyqtSlot(object)
                    def _run(self, queued_callback: Any) -> None:
                        try:
                            if callable(queued_callback):
                                queued_callback()
                        except Exception:
                            logging.getLogger(__name__).exception("GUI update failed.")

                dispatcher = _CallbackDispatcher()
                dispatcher.moveToThread(app.thread())
                setattr(_invoke_gui_callback, "_dispatcher", dispatcher)
        dispatcher.callbackRequested.emit(callback)
    except Exception:
        # Never run a GUI callback directly from a worker thread when the
        # dispatcher fails; drop the update and log instead.
        logging.getLogger(__name__).exception(
            "Failed to queue a GUI update on the Qt thread; update dropped."
        )


def _set_config_spinbox(widget: Any, value: float) -> None:
    """Replace even a focused draft on explicit config load, without commands."""
    if widget is None:
        return
    blocked = widget.blockSignals(True)
    try:
        # A saved config may exceed the previous editor bounds (e.g. external
        # triggering). Display its actual value rather than silently clamping it.
        widget.setRange(min(widget.minimum(), value), max(widget.maximum(), value))
        widget.setValue(value)
    finally:
        widget.blockSignals(blocked)


def _set_config_parameter(owner: Any, attr: str, parameter: Any, value: Any) -> None:
    """Adopt a readback without Parameter events sending it back to hardware."""
    loading = getattr(parameter, "loading", False)
    if parameter is not None:
        parameter.loading = True
    try:
        if parameter is not None:
            _set_config_spinbox(getattr(parameter, "spin", None), value)
            parameter.setValueWithoutEvents(value)
        setattr(owner, attr, value)
    finally:
        if parameter is not None:
            parameter.loading = loading


def _disable_spinbox_wheel(widget: Any) -> None:
    """Prevent accidental mouse-wheel edits on hardware-facing spinboxes."""
    install_filter = getattr(widget, "installEventFilter", None)
    if not callable(install_filter) or getattr(widget, "_esibd_wheel_event_blocker", None):
        return
    try:
        from PyQt6.QtCore import QObject, QEvent
    except ImportError:
        return

    class _WheelEventBlocker(QObject):
        def eventFilter(self, watched: Any, event: Any) -> bool:
            event_type = getattr(event, "type", None)
            if callable(event_type) and event_type() == QEvent.Type.Wheel:
                ignore = getattr(event, "ignore", None)
                if callable(ignore):
                    ignore()
                return True
            return False

    blocker = _WheelEventBlocker(widget)
    install_filter(blocker)
    setattr(widget, "_esibd_wheel_event_blocker", blocker)


def _state_is_on(state: Any) -> bool:
    normalized = _normalize_runtime_state(state).strip().upper()
    return normalized in {"ST_ON", "STATE_ON"}


def _action_label(action: Any) -> str:
    """Extract a stable label from QAction-like objects and test doubles."""
    for attr_name in ("toolTip", "text", "objectName"):
        attr = getattr(action, attr_name, None)
        value = attr() if callable(attr) else attr
        if isinstance(value, str) and value:
            return value
    return ""


def _combo_popup_open(combo: Any) -> bool:
    view = getattr(combo, "view", None)
    return callable(view) and bool(getattr(view(), "isVisible", lambda: False)())


def _format_available_configs(configs: list[dict[str, Any]]) -> str:
    if not configs:
        return "None"

    formatted = []
    for config in configs:
        index = _coerce_int(config.get("index"), -1)
        name = str(config.get("name", "") or "").strip() or "<unnamed>"
        suffixes: list[str] = []
        if not _coerce_bool(config.get("valid"), True):
            suffixes.append("invalid")
        if not _coerce_bool(config.get("active"), True):
            suffixes.append("inactive")
        label = f"{index}:{name}" if index >= 0 else name
        if suffixes:
            label = f"{label} ({', '.join(suffixes)})"
        formatted.append(label)
    return "; ".join(formatted)


def _format_config_option(
    index: int,
    name: str = "",
    *,
    active: bool = True,
    valid: bool = True,
    unavailable: bool = False,
) -> str:
    """Return one human-readable AMX config entry."""
    if index < 0:
        return "None: connect only (-1)"

    label = f"{index}:{str(name or '').strip() or '<unnamed>'}"
    suffixes: list[str] = []
    if unavailable:
        suffixes.append("not listed")
    else:
        if not valid:
            suffixes.append("invalid")
        if not active:
            suffixes.append("inactive")
    if suffixes:
        label = f"{label} ({', '.join(suffixes)})"
    return label


def _format_loaded_config_text(status: dict[str, Any]) -> str:
    """Format the config currently reported in AMX controller memory."""
    index = _coerce_int(status.get("memory_config"), -1)
    if index < 0:
        return "None"

    label = _format_config_option(
        index,
        str(status.get("memory_config_name", "") or "").strip(),
    )
    source = str(status.get("memory_config_source", "") or "").strip()
    if source:
        return f"{label} [{source}]"
    return label


def _channel_key_from_item(item: dict[str, Any]) -> int:
    return _coerce_int(item.get(_AMX_PULSER_KEY), 0)


def _generic_channel_name(device_name: str, pulser: int) -> str:
    return f"{device_name}_P{pulser}"


def _build_generic_channel_item(
    device_name: str,
    pulser: int,
    default_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = dict(default_item or {})
    item[_CHANNEL_NAME_KEY] = _generic_channel_name(device_name, pulser)
    item[_AMX_PULSER_KEY] = str(pulser)
    item[_CHANNEL_REAL_KEY] = True
    item[_CHANNEL_ENABLED_KEY] = False
    return item


def _looks_like_bootstrap_items(
    items: list[dict[str, Any]],
    device_name: str,
    default_item: dict[str, Any] | None = None,
) -> bool:
    if not items:
        return False

    expected_names = [f"{device_name}{index}" for index in range(1, len(items) + 1)]
    item_names = [str(item.get(_CHANNEL_NAME_KEY, "")) for item in items]
    if item_names != expected_names:
        return False

    if default_item is None:
        return all(_channel_key_from_item(item) == 0 for item in items)

    for item in items:
        for key, default_value in default_item.items():
            if key == _CHANNEL_NAME_KEY:
                continue
            item_value = item.get(key, default_value)
            if key == _AMX_PULSER_KEY:
                if _coerce_int(item_value, _coerce_int(default_value, 0)) != _coerce_int(
                    default_value,
                    0,
                ):
                    return False
                continue
            if isinstance(default_value, bool):
                if _coerce_bool(item_value, default=default_value) != default_value:
                    return False
                continue
            if _is_nan(default_value):
                if not _is_nan(item_value):
                    return False
                continue
            if isinstance(default_value, int) and not isinstance(default_value, bool):
                if _coerce_int(item_value, default_value) != default_value:
                    return False
                continue
            if isinstance(default_value, float):
                if _coerce_float(item_value, default_value) != default_value:
                    return False
                continue
            if item_value != default_value:
                return False
    return True


def _strip_legacy_bootstrap_residue(
    items: list[dict[str, Any]],
    device_name: str,
    default_item: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, PRINT | None]]]:
    if not items or default_item is None:
        return items, []

    default_key = _channel_key_from_item(default_item)
    residue_indices: list[int] = []
    residue_count = 0
    indexed_names = {
        str(item.get(_CHANNEL_NAME_KEY, "")): index
        for index, item in enumerate(items)
    }

    while True:
        residue_count += 1
        index = indexed_names.get(f"{device_name}{residue_count}")
        if index is None:
            residue_count -= 1
            break
        residue_indices.append(index)

    if residue_count < 2 or residue_count == len(items):
        return items, []

    residue_items = [items[index] for index in residue_indices]
    if any(_channel_key_from_item(item) != default_key for item in residue_items):
        return items, []

    cleaned_items = [
        item for index, item in enumerate(items) if index not in set(residue_indices)
    ]
    if not cleaned_items:
        return items, []

    return cleaned_items, [
        (
            f"Removed legacy AMX bootstrap channels: "
            f"{device_name}1..{device_name}{residue_count}",
            None,
        )
    ]


def _plan_channel_sync(
    current_items: list[dict[str, Any]],
    device_name: str,
    default_item: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, PRINT | None]]]:
    if _looks_like_bootstrap_items(current_items, device_name, default_item=default_item):
        return [
            _build_generic_channel_item(device_name, pulser, default_item=default_item)
            for pulser in _AMX_PULSER_IDS
        ], [("AMX bootstrap config replaced with fixed pulser channels.", None)]

    current_items, cleanup_logs = _strip_legacy_bootstrap_residue(
        current_items,
        device_name=device_name,
        default_item=default_item,
    )

    target_ids = set(_AMX_PULSER_IDS)
    kept_keys: set[int] = set()
    added_pulsers: list[int] = []
    virtualized_pulsers: list[int] = []
    reactivated_pulsers: list[int] = []
    duplicate_entries: list[tuple[str, int]] = []
    synced_items: list[dict[str, Any]] = []

    for item in current_items:
        synced_item = dict(item)
        pulser = _channel_key_from_item(synced_item)
        if pulser in kept_keys:
            duplicate_entries.append(
                (str(synced_item.get(_CHANNEL_NAME_KEY, "")), pulser)
            )
            synced_item[_CHANNEL_REAL_KEY] = False
            synced_items.append(synced_item)
            continue

        kept_keys.add(pulser)
        if pulser in target_ids:
            if not _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                reactivated_pulsers.append(pulser)
            synced_item[_CHANNEL_REAL_KEY] = True
        else:
            if _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                virtualized_pulsers.append(pulser)
            synced_item[_CHANNEL_REAL_KEY] = False
        synced_items.append(synced_item)

    for pulser in _AMX_PULSER_IDS:
        if pulser in kept_keys:
            continue
        synced_items.append(
            _build_generic_channel_item(
                device_name,
                pulser,
                default_item=default_item,
            )
        )
        added_pulsers.append(pulser)

    log_entries: list[tuple[str, PRINT | None]] = list(cleanup_logs)
    if added_pulsers:
        log_entries.append(
            (
                "Added generic AMX pulser channels: "
                + ", ".join(f"P{pulser}" for pulser in added_pulsers),
                None,
            )
        )
    if virtualized_pulsers:
        log_entries.append(
            (
                "Marked AMX pulser channels virtual because they do not exist on hardware: "
                + ", ".join(f"P{pulser}" for pulser in virtualized_pulsers),
                None,
            )
        )
    if reactivated_pulsers:
        log_entries.append(
            (
                "Reactivated AMX pulser channels: "
                + ", ".join(f"P{pulser}" for pulser in reactivated_pulsers),
                None,
            )
        )
    for channel_name, pulser in duplicate_entries:
        log_entries.append(
            (
                f"Duplicate AMX mapping detected for P{pulser}: {channel_name}",
                PRINT.WARNING,
            )
        )
    return synced_items, log_entries


def _bundled_runtime_module_name(plugin_dir: Path | None = None) -> str:
    resolved_plugin_dir = Path(__file__).resolve().parent if plugin_dir is None else plugin_dir
    plugin_key = resolved_plugin_dir.name.replace("-", "_")
    return f"{_BUNDLED_RUNTIME_NAMESPACE_PREFIX}_{plugin_key}"


def _load_private_runtime_package(module_name: str, package_dir: Path) -> None:
    with _RUNTIME_LOAD_LOCK:
        if module_name in sys.modules:
            return
        _load_private_runtime_package_unlocked(module_name, package_dir)


def _load_private_runtime_package_unlocked(module_name: str, package_dir: Path) -> None:

    init_file = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"Could not create an import spec for bundled AMX runtime at {package_dir}."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise


def _wait_for_next_poll(controller: Any, started: float) -> None:
    """Hold the configured cadence (not interval + read time); return promptly on OFF."""
    deadline = max(started + controller.controllerParent.interval / 1000,
                   time.monotonic() + _POLL_MIN_GAP_S)
    while controller.acquiring:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, _POLL_STOP_CHECK_S))


def _native_backend(device):
    """The device's native-worker proxy, inspected without queuing a lifecycle RPC (None without one)."""
    try:
        backend = object.__getattribute__(device, "_backend")
        mode = object.__getattribute__(device, "_backend_mode")
    except (AttributeError, TypeError):
        return None
    if mode == "process" and isinstance(getattr(backend, "session", None), str):
        return backend
    return None


def _configure_panel_table(table: Any) -> None:
    """Read-only, unselectable table sized to its rows; narrow docks scroll it sideways."""
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QAbstractItemView, QHeaderView, QSizePolicy

    table.verticalHeader().hide()
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
    table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    table.setAlternatingRowColors(True)
    table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    table.setMinimumWidth(0)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    table.resizeRowsToContents()
    # A narrow dock scrolls the table sideways: make room for that bar only then.
    table.horizontalScrollBar().rangeChanged.connect(lambda *_: _fit_table_height(table))
    _fit_table_height(table)


def _fit_table_height(table: Any) -> None:
    """Show every visible row without a vertical scroll bar."""
    bar = table.horizontalScrollBar()
    table.setFixedHeight(
        table.horizontalHeader().sizeHint().height()
        + sum(table.rowHeight(row) for row in range(table.rowCount()) if not table.isRowHidden(row))
        + (bar.sizeHint().height() if bar.maximum() > 0 else 0) + 2 * table.frameWidth() + 2
    )


def _scrollable_panel(panel: Any) -> Any:
    """Do not propagate the content's minimum size to Explorer's dock area."""
    from PyQt6.QtCore import QEvent, Qt
    from PyQt6.QtWidgets import QAbstractSpinBox, QApplication, QComboBox, QFrame, QScrollArea

    class PanelScrollArea(QScrollArea):
        def mousePressEvent(self, event):
            # Empty panel space validates an edit; output buttons accept their
            # own clicks without letting this ancestor steal editor focus.
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            super().mousePressEvent(event)

        def eventFilter(self, watched, event):
            if event.type() == QEvent.Type.Wheel and isinstance(watched, (QAbstractSpinBox, QComboBox)):
                # Scrolling the panel must never edit an output setpoint/range,
                # even when the editor has focus. Keep arrows/keyboard available.
                QApplication.sendEvent(self.viewport(), event)
                return True
            return super().eventFilter(watched, event)

    scroll = PanelScrollArea()
    scroll.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    for editor in panel.findChildren(QAbstractSpinBox) + panel.findChildren(QComboBox):
        editor.installEventFilter(scroll)
    return scroll


def _create_waveform_view() -> Any:
    """Paint the model of ``_amx_waveform_traces``: expected levels, never a measurement."""
    from PyQt6.QtCore import QPointF, QRectF, QSize, Qt
    from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen
    from PyQt6.QtWidgets import QSizePolicy, QWidget

    class WaveformView(QWidget):
        ROW_HEIGHT = 34
        LEFT, RIGHT, TOP, AXIS = 58, 14, 4, 22

        def __init__(self) -> None:
            super().__init__()
            self.model: dict[str, Any] = {"window_us": None, "traces": []}
            self.message = ""
            self.dark = True
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.setFixedHeight(4 * self.ROW_HEIGHT + self.TOP + self.AXIS)

        def sizeHint(self) -> QSize:
            return QSize(480, self.height())

        def minimumSizeHint(self) -> QSize:
            return QSize(160, self.height())

        def set_model(self, model: dict[str, Any], message: str, dark: bool) -> None:
            if (model, message, dark) != (self.model, self.message, self.dark):
                self.model, self.message, self.dark = model, message, dark
                self.update()

        def paintEvent(self, _event) -> None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            color = lambda role: QColor(_amx_panel_color(role, self.dark))
            plot = QRectF(self.LEFT, self.TOP, max(self.width() - self.LEFT - self.RIGHT, 1),
                          4 * self.ROW_HEIGHT)
            window = self.model.get("window_us")
            traces = self.model.get("traces") or []
            if self.message or not traces:
                painter.setPen(color("muted"))
                painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, self.message or "No expected waveform.")
                painter.end()
                return
            x = lambda t: plot.left() + plot.width() * t / window
            if window:
                painter.setPen(QPen(color("grid"), 1, Qt.PenStyle.DotLine))
                for tick in range(2 * _AMX_WAVEFORM_PERIODS + 1):
                    t = window * tick / (2 * _AMX_WAVEFORM_PERIODS)
                    painter.drawLine(QPointF(x(t), plot.top()), QPointF(x(t), plot.bottom()))
                    painter.setPen(color("name"))
                    # Keep the end labels inside the widget, flush with their tick.
                    left = min(max(x(t) - 40, 0.0), self.width() - 80.0)
                    align = (Qt.AlignmentFlag.AlignHCenter if left == x(t) - 40
                             else Qt.AlignmentFlag.AlignRight if left < x(t) - 40 else Qt.AlignmentFlag.AlignLeft)
                    painter.drawText(QRectF(left, plot.bottom() + 3, 80, self.AXIS - 3),
                                     align | Qt.AlignmentFlag.AlignTop, f"{t:.3g} µs")
                    painter.setPen(QPen(color("grid"), 1, Qt.PenStyle.DotLine))
            for index, trace in enumerate(traces[:4]):
                top = plot.top() + index * self.ROW_HEIGHT
                high, low = top + 7, top + self.ROW_HEIGHT - 9
                painter.setPen(color("value"))
                painter.drawText(QRectF(0, top, self.LEFT - 22, self.ROW_HEIGHT),
                                 Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, trace["channel"])
                painter.setPen(color("name"))
                for y, sign in ((high, "+"), (low, "−")):
                    painter.drawText(QRectF(self.LEFT - 18, y - 8, 12, 16), Qt.AlignmentFlag.AlignCenter, sign)
                kind = trace["kind"]
                pen = QPen(color("trace"), 2)
                if kind == "wave" and window:
                    level = bool(trace["segments"]) and trace["segments"][0][0] <= 0.0
                    path = QPainterPath(QPointF(x(0.0), high if level else low))
                    for begin, end in trace["segments"]:
                        if begin > 0.0:
                            path.lineTo(x(begin), low)
                            path.lineTo(x(begin), high)
                        path.lineTo(x(end), high)
                        if end < window:
                            path.lineTo(x(end), low)
                    path.lineTo(x(window), low if not trace["segments"] or trace["segments"][-1][1] < window else high)
                    painter.setPen(pen)
                    painter.drawPath(path)
                elif kind == "level":
                    painter.setPen(pen)
                    y = high if trace["high"] else low
                    painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
                elif kind == "hiz":
                    painter.setPen(QPen(color("muted"), 2, Qt.PenStyle.DashLine))
                    middle = (high + low) / 2
                    painter.drawLine(QPointF(plot.left(), middle), QPointF(plot.right(), middle))
                elif kind == "dense":
                    fill = color("trace")
                    fill.setAlpha(70)
                    painter.fillRect(QRectF(plot.left(), high, plot.width(), low - high), fill)
                if trace["label"]:
                    painter.setPen(color("warning") if kind in ("none", "wave") else color("name"))
                    painter.drawText(QRectF(plot.left() + 6, top, plot.width() - 12, self.ROW_HEIGHT),
                                     Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, trace["label"])
            painter.end()

    return WaveformView()


def _offer_thirty_minutes(display) -> None:
    """Live plot: offer 30 min in its length menu and select it once (user, 2026-11-12).

    Explorer offers 0.2 to 1440 min without 30 and starts at 2 min. The menu is saved with its items,
    so once 30 is there the operator's later choice stays.
    """
    combo = getattr(display, "displayTimeComboBox", None)
    setting = getattr(combo, "setting", None)
    if setting is None or combo.findText(_LIVE_PLOT_MINUTES) != -1:
        return
    values = [float(text) for text in setting.items]
    combo.insertItem(next((i for i, value in enumerate(values) if value > 30), len(values)), _LIVE_PLOT_MINUTES)
    setting.loading = True  # As Explorer restores it: saved with the items, no replot before the display exists.
    try:
        setting.value = _LIVE_PLOT_MINUTES
    finally:
        setting.loading = False


def _zoom_time_axis_with_wheel(display) -> None:
    """The wheel over the time axis zooms it, as it does over a y axis (user, 2026-11-13).

    Explorer keeps the time axis following the clock with the mouse disabled on x, so the wheel did
    nothing there. Its first turn now does what Explorer's "Scale x manually" button does, then
    zooms; the same button ("Scale x automatically") brings the following back.
    """
    for widget in getattr(display, "livePlotWidgets", []) or []:
        get_axis = getattr(widget, "getAxis", None)
        if not callable(get_axis):
            continue  # Stacked layout: the extra ViewBoxes share the first plot's time axis.
        for side in ("bottom", "top"):
            try:
                axis = get_axis(side)
            except KeyError:
                continue
            if axis is None or getattr(axis, "_zooms_time", False):
                continue
            wheel = axis.wheelEvent

            def zoom(event, axis=axis, wheel=wheel) -> None:
                view = axis.linkedView()
                if (view is not None and not view.mouseEnabled()[0]
                        and not view.sceneBoundingRect().contains(event.scenePos())):
                    display.updateMouseEnabled(x=True)
                wheel(event)

            axis.wheelEvent = zoom
            axis._zooms_time = True


class _LiveDisplay(LiveDisplay):
    """Explorer's live plot: the last 30 min by default, the wheel zooms the time axis."""

    def initGUI(self) -> None:
        super().initGUI()
        _offer_thirty_minutes(self)

    def initFig(self) -> None:
        super().initFig()
        _zoom_time_axis_with_wheel(self)


class _AMXDeviceCommon:
    """The AMXDevice methods every AMX plugin shares; its own class lists it first in its bases."""

    def exportConfigurationIfChanged(self) -> None:
        """Explorer 1.0.2 also runs this periodic save from a worker thread, but the
        export refreshes Explorer's file tree (Qt GUI work): run it on the GUI thread."""
        base = getattr(super(), "exportConfigurationIfChanged", None)
        if callable(base):
            _invoke_gui_callback(base)

    def _current_channel_items(self) -> list[dict[str, Any]]:
        return [channel.asDict() for channel in self.getChannels()]

    def _default_channel_template(self) -> dict[str, dict[str, Any]]:
        return self.channelType(channelParent=self, tree=None).getSortedDefaultChannel()

    def _default_channel_item(self) -> dict[str, Any]:
        return self.channelType(channelParent=self, tree=None).asDict()

    def _setting(self, setting_name: str) -> Any | None:
        plugin_manager = getattr(self, "pluginManager", None)
        settings_plugin = getattr(plugin_manager, "Settings", None)
        settings = getattr(settings_plugin, "settings", None)
        if not isinstance(settings, dict):
            return None
        return settings.get(f"{self.name}/{setting_name}")

    def _config_setting_name(self, attr_name: str) -> str:
        return {
            "standby_config": self.STANDBY_CONFIG,
            "operating_config": self.OPERATING_CONFIG,
        }[attr_name]

    def _available_config_entries(self) -> list[dict[str, Any]]:
        configs = list(getattr(self, "available_configs", []) or [])
        return sorted(configs, key=lambda config: _coerce_int(config.get("index"), 10_000))

    def _config_selector_entries(self, attr_name: str) -> list[tuple[int, str]]:
        entries: list[tuple[int, str]] = [(-1, _format_config_option(-1))]
        seen = {-1}
        for config in self._available_config_entries():
            index = _coerce_int(config.get("index"), -1)
            if index < 0 or index in seen:
                continue
            entries.append(
                (
                    index,
                    _format_config_option(
                        index,
                        str(config.get("name", "") or "").strip(),
                        active=_coerce_bool(config.get("active"), True),
                        valid=_coerce_bool(config.get("valid"), True),
                    ),
                )
            )
            seen.add(index)
        current_value = self._config_setting_value(attr_name)
        if current_value >= 0 and current_value not in seen:
            entries.append(
                (
                    current_value,
                    _format_config_option(current_value, "<saved>", unavailable=True),
                )
            )
        return entries

    def _config_selector_tooltip_text(self, attr_name: str) -> str:
        setting_name = self._config_setting_name(attr_name)
        setting = self._setting(setting_name)
        tooltip = str(getattr(setting, "toolTip", "") or "").strip() if setting is not None else ""
        lines = [tooltip] if tooltip else []
        lines.append("Load a saved AMX configuration, including its frequency and pulse widths.")
        lines.append(f"Loaded: {getattr(self, 'loaded_config_text', '') or 'n/a'}")
        lines.append("Available AMX configs:")
        available = str(getattr(self, "available_configs_text", "") or "n/a")
        for entry in available.split(";"):
            entry = entry.strip()
            if entry:
                lines.append(f"- {entry}")
        return "\n".join(lines)

    def _create_config_selector_widget(self) -> Any:
        from PyQt6.QtWidgets import QComboBox

        combo = QComboBox()
        combo.setMinimumWidth(180)
        combo.setMaxVisibleItems(32)
        with contextlib.suppress(AttributeError):
            combo.setSizeAdjustPolicy(type(combo).SizeAdjustPolicy.AdjustToContents)
        return combo

    def _create_config_button_widget(self, text: str) -> Any:
        from PyQt6.QtWidgets import QPushButton

        button = QPushButton(text)
        button.setMinimumWidth(96)
        return button

    def _create_frequency_widget(self) -> Any:
        from PyQt6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox

        widget = QDoubleSpinBox()
        widget.setKeyboardTracking(False)
        widget.setRange(0.001, 10000.0)
        widget.setDecimals(3)
        widget.setSingleStep(0.1)
        widget.setSuffix(" kHz")
        widget.setMinimumWidth(112)
        widget.setAccelerated(True)
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)  # Like Explorer's own fields; ↑/↓ keys still step.
        _disable_spinbox_wheel(widget)
        return widget

    def _combo_clear(self, combo: Any) -> None:
        clear = getattr(combo, "clear", None)
        if callable(clear):
            clear()

    def _combo_add_item(self, combo: Any, text: str, value: int) -> None:
        add_item = getattr(combo, "addItem", None)
        if callable(add_item):
            try:
                add_item(text, value)
            except TypeError:
                add_item(text)

    def _combo_find_data(self, combo: Any, value: int) -> int:
        find_data = getattr(combo, "findData", None)
        if callable(find_data):
            return int(find_data(value))
        items = getattr(combo, "items", None)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, tuple) and len(item) >= 2 and item[1] == value:
                    return index
        return -1

    def _combo_item_data(self, combo: Any, index: int) -> Any:
        item_data = getattr(combo, "itemData", None)
        if callable(item_data):
            return item_data(index)
        items = getattr(combo, "items", None)
        if isinstance(items, list) and 0 <= index < len(items):
            item = items[index]
            if isinstance(item, tuple) and len(item) >= 2:
                return item[1]
        return None

    def _combo_current_value(self, combo: Any, default: int = -1) -> int:
        current_index = getattr(combo, "currentIndex", None)
        if not callable(current_index):
            return default
        return _coerce_int(self._combo_item_data(combo, current_index()), default)

    def _connect_config_selector(self, combo: Any, attr_name: str) -> None:
        signal = getattr(combo, "currentIndexChanged", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(lambda *_args, attr_name=attr_name: self._config_selector_changed(attr_name))

    def _connect_config_button(self, button: Any, callback: Any) -> None:
        signal = getattr(button, "clicked", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(callback)

    def _connect_frequency_widget(self, widget: Any) -> None:
        signal = getattr(widget, "valueChanged", None)
        connect = getattr(signal, "connect", None)
        if callable(connect):
            connect(lambda value: self._frequency_widget_changed(value))

    def _update_config_selector(self, combo: Any | None, attr_name: str) -> None:
        if combo is None or _combo_popup_open(combo):
            return  # Rebuilt under the mouse, an open list jumps back to its top at every poll.
        block_signals = getattr(combo, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            entries = self._config_selector_entries(attr_name)
            if getattr(combo, "_config_entries", None) != entries:
                self._combo_clear(combo)
                for value, label in entries:
                    self._combo_add_item(combo, label, value)
                combo._config_entries = entries
            selected_index = self._combo_find_data(combo, self._config_setting_value(attr_name))
            if selected_index < 0:
                selected_index = 0
            set_current_index = getattr(combo, "setCurrentIndex", None)
            if callable(set_current_index):
                set_current_index(selected_index)
            tooltip = self._config_selector_tooltip_text(attr_name)
            if hasattr(combo, "setToolTip"):
                combo.setToolTip(tooltip)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _load_operating_now_ready(self) -> tuple[bool, str]:
        controller = getattr(self, "controller", None)
        if controller is None:
            return False, "controller unavailable"
        if getattr(controller, "initializing", False):
            return False, "initialization in progress"
        if getattr(controller, "transitioning", False):
            return False, "ON/OFF transition in progress"
        if getattr(controller, "device", None) is None:
            return False, "device disconnected"
        if not getattr(controller, "initialized", False):
            return False, "communication not initialized"
        is_on = getattr(self, "isOn", None)
        if not callable(is_on) or not bool(is_on()):
            return False, "AMX is OFF"
        if self._config_setting_value("operating_config") < 0:
            return False, "select a config first"
        return True, ""

    def _config_load_pending(self) -> bool:
        """True while ON with a selected config that differs from the one in controller memory."""
        selected = self._config_setting_value("operating_config")
        loaded = _coerce_int(getattr(getattr(self, "controller", None), "_loaded_config_index", -1), -1)
        return self._load_operating_now_ready()[0] and 0 <= loaded != selected

    def _config_selector_changed(self, attr_name: str) -> None:
        combo = getattr(
            self,
            {
                "standby_config": "standbyConfigCombo",
                "operating_config": "operatingConfigCombo",
            }[attr_name],
            None,
        )
        if combo is None:
            return
        value = self._combo_current_value(combo)
        if self._set_config_setting_value(attr_name, value):
            self._update_config_controls()
            self._update_status_widgets()

    def _set_frequency_setting_value(self, value: float) -> bool:
        value = float(value)
        setting = self._setting(self.FREQUENCY_KHZ)
        if setting is not None:
            current = _coerce_float(getattr(setting, "value", value), value)
            if current == value:
                self.frequency_khz = value
                return False
            setting.value = value
            self.frequency_khz = value
            return True
        current = _coerce_float(getattr(self, "frequency_khz", value), value)
        if current == value:
            return False
        self.frequency_khz = value
        return True

    def _frequency_widget_changed(self, value: float) -> None:
        if self._set_frequency_setting_value(float(value)):
            self.frequencyChanged(debounce=True)
            self._update_status_widgets()

    def loadOperatingConfigNow(self) -> None:
        self._cancel_runtime_apply_timers()
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        # Sync the combo selection into the setting before starting the load
        # thread.  If the operator changed the dropdown and immediately clicked
        # "Load now", the currentIndexChanged signal may not have been processed
        # yet, and the controller would read the old value.
        combo = getattr(self, "operatingConfigCombo", None)
        if combo is not None:
            value = self._combo_current_value(combo)
            if value >= 0:
                self._set_config_setting_value("operating_config", value)
        load_now = getattr(controller, "loadOperatingConfigNowFromThread", None)
        if callable(load_now):
            load_now(parallel=True)
            return
        controller.loadOperatingConfigNow()

    def _ensure_local_on_action(self) -> None:
        """Expose the global AMX ON/OFF control directly in the plugin toolbar."""
        if (
            not self.useOnOffLogic
            or hasattr(self, "deviceOnAction")
            or not hasattr(self, "closeCommunicationAction")
        ):
            return

        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(on=checked),
            toolTipFalse=f"Turn {self.name} ON.",
            iconFalse=self.makeIcon(_AMX_POWER_ON_ICON),
            toolTipTrue=f"Turn {self.name} OFF and disconnect.",
            iconTrue=self.makeIcon(_AMX_POWER_OFF_ICON),
            before=self.closeCommunicationAction,
            restore=False,
            defaultState=False,
        )
        self._sync_local_on_action()

    def _sync_local_on_action(self) -> None:
        """Keep the local toolbar ON/OFF button synchronized with the device state."""
        _session_sync(self)  # Crash resume record follows the ON state.
        action = getattr(self, "deviceOnAction", None)
        if action is None:
            return
        # StateAction.toggled updates the icon/tooltip; only triggered sends commands.
        action.state = self.isOn()

    def _display_main_state(self) -> str:
        """Return the operator-facing state shown in the toolbar badge."""
        raw_state = _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
        if self._fpga_disabled_standby_state(raw_state=raw_state):
            return "Standby"
        normalized = raw_state.lower()
        if "stby" in normalized or "standby" in normalized:
            return "Standby"
        is_on = getattr(self, "isOn", None)
        if (
            raw_state != "Disconnected"
            and not _status_requires_operator_attention(raw_state)
            and callable(is_on)
            and not bool(is_on())
        ):
            return "OFF"
        return raw_state

    def _raw_device_state_summary(self) -> str:
        controller = getattr(self, "controller", None)
        return str(getattr(controller, "device_state_summary", "") or "").strip()

    def _fpga_disabled_standby_state(self, *, raw_state: str | None = None) -> bool:
        """Return True when the AMX reports FPGA disabled as a standby indication.

        On this hardware the vendor state often remains ``STATE_ERR_FPGA_DIS``
        together with ``DEVST_FPGA_DIS`` while a valid standby/off config keeps
        the device disabled. Treat that combination as a standby indication in
        the plugin UI when the operator currently keeps the AMX OFF, so it is
        not mistaken for a runtime fault while parked in standby.
        """
        raw_state = (
            _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
            if raw_state is None
            else raw_state
        )
        if raw_state != "STATE_ERR_FPGA_DIS":
            return False

        # If device state flags are empty or only DEVST_FPGA_DIS, this is
        # a benign standby state -- not a real hardware fault.
        flags = {
            token.upper()
            for token in _status_tokens(self._raw_device_state_summary())
        }
        if not flags or flags <= {"DEVST_FPGA_DIS"}:
            is_on = getattr(self, "isOn", None)
            if callable(is_on):
                return not bool(is_on())
            return (
                str(getattr(self, "device_enabled_state", "")).strip().upper()
                in {"OFF", "FALSE", "0"}
            )

        return False

    def _display_device_state_summary(self) -> str:
        raw_summary = self._raw_device_state_summary()
        if self._fpga_disabled_standby_state():
            return "Standby / FPGA off"
        return raw_summary or "n/a"

    def _ensure_status_widgets(self) -> None:
        """Add compact global AMX status labels to the plugin toolbar."""
        if (
            getattr(self, "titleBar", None) is None
            or getattr(self, "titleBarLabel", None) is None
            or hasattr(self, "statusBadgeLabel")
        ):
            return

        label_type = type(self.titleBarLabel)
        self.statusBadgeLabel = label_type("")
        self.statusSummaryLabel = label_type("")

        if hasattr(self.statusBadgeLabel, "setObjectName"):
            self.statusBadgeLabel.setObjectName(f"{self.name}StatusBadge")
        if hasattr(self.statusSummaryLabel, "setObjectName"):
            self.statusSummaryLabel.setObjectName(f"{self.name}StatusSummary")
        if hasattr(self.statusSummaryLabel, "setStyleSheet"):
            self.statusSummaryLabel.setStyleSheet("QLabel { padding-left: 6px; }")

        insert_before = getattr(self, "stretchAction", None)
        if insert_before is not None and hasattr(self.titleBar, "insertWidget"):
            self.titleBar.insertWidget(insert_before, self.statusBadgeLabel)
            self.titleBar.insertWidget(insert_before, self.statusSummaryLabel)
        elif hasattr(self.titleBar, "addWidget"):
            self.titleBar.addWidget(self.statusBadgeLabel)
            self.titleBar.addWidget(self.statusSummaryLabel)

        self._update_status_widgets()

    def _status_badge_style(self) -> str:
        """Return a compact badge style that reflects the AMX main state."""
        state = self._display_main_state()
        normalized = state.lower()
        is_on = bool(getattr(self, "isOn", lambda: False)())
        if state == "Disconnected":
            background = "#718096"
        elif state == "OFF":
            background = "#4a5568"
        elif _status_requires_operator_attention(state):
            background = "#c53030"
        elif "stby" in normalized or "standby" in normalized:
            background = "#b7791f"
        elif is_on:
            background = "#2f855a"
        else:
            background = "#4a5568"
        return (
            "QLabel {"
            f" background-color: {background};"
            " color: white;"
            " border-radius: 3px;"
            " padding: 2px 6px;"
            " font-weight: 600;"
            " }"
        )

    def _status_summary_text(self) -> str:
        """Return the compact AMX runtime summary displayed in the toolbar."""
        device_enabled = str(getattr(self, "device_enabled_state", "OFF") or "OFF")
        faults = _compact_status_text(
            self._display_device_state_summary(),
            default="n/a",
        )
        return f"Device: {device_enabled} | Faults: {faults}"

    def _update_status_widgets(self) -> None:
        """Refresh the global AMX status labels in the toolbar."""
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        self._sync_acquisition_controls()
        if badge is None or summary is None:
            self._update_operator_panel()
            return

        badge_text = self._display_main_state()
        summary_text = self._status_summary_text()
        tooltip = self._status_tooltip_text()

        if hasattr(badge, "setText"):
            badge.setText(badge_text)
        if hasattr(badge, "setToolTip"):
            badge.setToolTip(tooltip)
        if hasattr(badge, "setStyleSheet"):
            badge.setStyleSheet(self._status_badge_style())

        if hasattr(summary, "setText"):
            summary.setText(summary_text)
        if hasattr(summary, "setToolTip"):
            summary.setToolTip(tooltip)
        self._update_operator_panel()

    def _channel_parameter_value(
        self,
        channel: Any,
        parameter_name: str,
        attr_name: str,
        default: Any,
    ) -> Any:
        getter = getattr(channel, "getParameterByName", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(parameter_name)
                if parameter is not None:
                    return getattr(
                        parameter,
                        "value",
                        getattr(channel, attr_name, default),
                    )
        return getattr(channel, attr_name, default)

    def _set_channel_parameter(
        self,
        channel: Any,
        parameter_name: str,
        attr_name: str,
        value: Any,
        event_name: str,
    ) -> None:
        getter = getattr(channel, "getParameterByName", None)
        parameter = None
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(parameter_name)
        if parameter is not None:
            setter = getattr(parameter, "setValueWithoutEvents", None)
            if callable(setter):
                setter(value)
            else:
                parameter.value = value
        setattr(channel, attr_name, value)
        event = getattr(channel, event_name, None)
        if callable(event):
            event()

    def _maximum_width_us(self) -> float:
        frequency_khz = _coerce_float(getattr(self, "frequency_khz", 2.0), 2.0)
        if frequency_khz <= 0.0:
            return 0.0
        controller = getattr(self, "controller", None)
        device = getattr(controller, "device", None) if controller is not None else None
        clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
        width_offset = _coerce_int(
            _safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2),
            2,
        )
        return max(
            (1000.0 / frequency_khz) - width_offset / (clock_hz / 1e6),
            0.0,
        )

    def _apply_panel_theme(self) -> None:
        """Restyle the panel for Explorer's current light or dark palette."""
        panel = getattr(self, "amxPanel", None)
        if panel is None:
            return
        self._amx_panel_dark = _amx_dark_theme()
        panel.setStyleSheet(_amx_panel_stylesheet(self._amx_panel_dark))
        self._update_operator_panel()

    def updateTheme(self) -> None:
        """Extend Explorer's theme switch to the operator panel's own colors."""
        update_theme = getattr(super(), "updateTheme", None)
        if callable(update_theme):
            update_theme()
        self._apply_panel_theme()

    def _panel_enabled_toggled(self, pulser: int, checked: bool) -> None:
        if getattr(self, "loading", False):
            return
        channel = self._channel_by_pulser(pulser)
        controller = getattr(self, "controller", None)
        if channel is None or bool(
            getattr(controller, "initializing", False)
            or getattr(controller, "transitioning", False)
        ):
            self._update_operator_panel()
            return
        if checked:
            self._finish_setpoint_edits(channel)
        self._set_channel_parameter(
            channel,
            getattr(channel, "ENABLED", "Enabled"),
            "enabled",
            bool(checked),
            "enabledChanged",
        )
        self._update_operator_panel()

    def _panel_width_changed(self, pulser: int, value: float) -> None:
        if getattr(self, "loading", False):
            return
        channel = self._channel_by_pulser(pulser)
        controller = getattr(self, "controller", None)
        if (
            channel is None
            or not bool(
                self._channel_parameter_value(
                    channel,
                    getattr(channel, "ACTIVE", "Active"),
                    "active",
                    True,
                )
            )
            or bool(
                getattr(controller, "initializing", False)
                or getattr(controller, "transitioning", False)
            )
        ):
            self._update_operator_panel()
            return
        self._set_channel_parameter(
            channel,
            getattr(channel, "VALUE", "Value"),
            "value",
            float(value),
            "valueChanged",
        )
        self._update_operator_panel()

    def _duty_width_us(self, duty: float) -> float | None:
        """Width of a duty cycle in the requested period, on the clock grid."""
        clock_hz, osc_offset, width_offset, _ = self._timing_constants()
        period = _period_ticks(_coerce_float(getattr(self, "frequency_khz", 2.0), 2.0),
                               clock_hz=clock_hz, oscillator_offset=osc_offset)
        if not period:
            return None
        high = 0 if duty <= 0.0 else min(max(round(duty / 100.0 * period), width_offset + 1),
                                         max(period - width_offset, width_offset + 1))
        return high / (clock_hz / 1e6)

    def _panel_duty_changed(self, pulser: int, duty: float) -> None:
        """A duty edit is a width edit: same channel value, same apply path."""
        width = self._duty_width_us(duty)
        if width is None:
            self._update_operator_panel()
            return
        self._panel_width_changed(pulser, width)

    def _panel_keep_changed(self, pulser: int, keep: Any) -> None:
        channel = self._channel_by_pulser(pulser)
        if channel is None or keep not in (_AMX_KEEP_DUTY, _AMX_KEEP_WIDTH):
            self._update_operator_panel()
            return
        self._set_channel_parameter(
            channel, getattr(channel, "KEEP", "Keep"), "keep", keep, "keepChanged")

    def _set_panel_checked(
        self,
        widget: Any,
        *,
        checked: bool,
        enabled: bool,
    ) -> None:
        if widget is None:
            return
        blocker = getattr(widget, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        try:
            setter = getattr(widget, "setChecked", None)
            if callable(setter):
                setter(bool(checked))
            if hasattr(widget, "setEnabled"):
                widget.setEnabled(bool(enabled))
        finally:
            if callable(blocker):
                blocker(False)

    def _set_panel_width(
        self,
        widget: Any,
        *,
        value: float,
        maximum: float,
        enabled: bool,
    ) -> None:
        if widget is None:
            return
        blocker = getattr(widget, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        try:
            if not getattr(widget, "hasFocus", lambda: False)():
                if hasattr(widget, "setMaximum"):
                    widget.setMaximum(max(float(maximum), 0.0))
                if hasattr(widget, "setValue"):
                    widget.setValue(max(float(value), 0.0))
            if hasattr(widget, "setEnabled"):
                widget.setEnabled(bool(enabled))
        finally:
            if callable(blocker):
                blocker(False)

    def _linked_psu_readback(self, name: str) -> dict[str, Any]:
        """Read Explorer Channels only; never poll or command another device."""
        unavailable = {"vpos": None, "vneg": None, "detail": "No PSU assigned; voltages unavailable."}
        if name == "None":
            return unavailable
        manager = getattr(self, "pluginManager", None)
        matches = [p for p in getattr(manager, "plugins", []) if getattr(p, "name", None) == name]
        if len(matches) != 1:
            return {**unavailable, "detail": f"{name} unavailable (plugin absent or ambiguous)."}
        device_manager = getattr(manager, "DeviceManager", None)
        lookup = getattr(device_manager, "getChannelByName", None)
        channels = getattr(matches[0], "getChannels", None)
        if not callable(lookup) or not callable(channels):
            return {**unavailable, "detail": f"{name}: Explorer channels unavailable."}
        result = dict(unavailable)
        reasons = []
        try:
            sources = list(channels())
            registered = list(device_manager.channels())
            for ch, key, sign in ((0, "vpos", 1), (1, "vneg", -1)):
                candidates = [c for c in sources if _coerce_int(getattr(c, "id", -1), -1) == ch]
                if len(candidates) != 1:
                    reasons.append(f"{name} CH{ch} missing or ambiguous.")
                    continue
                source = candidates[0]
                # Resolve the current name, not a hard-coded PSU_A_CH0 name:
                # user renames/reordering must not change physical rail mapping.
                same_name = [c for c in registered
                             if c.name.strip().lower() == source.name.strip().lower()]
                if len(same_name) != 1 or lookup(source.name) is not source:
                    reasons.append(f"{name} CH{ch} not uniquely registered in Explorer.")
                    continue
                # Old PSU plugins did not maintain monitor validity. Do not
                # silently accept their stale/incorrect monitor values.
                if not hasattr(source, "readback_status"):
                    reasons.append(f"{name}: update the PSU plugin for Channel readbacks.")
                    continue
                value = _coerce_float(source.monitor, np.nan)
                if (source.real and source.enabled and source.initialized
                        and source.useMonitors and source.unit == "V"
                        and np.isfinite(value) and value >= 0):
                    result[key] = sign * value  # PSU_POS / PSU_NEG magnitudes.
                else:
                    reasons.append(f"{name} CH{ch}: {source.readback_status}")
            result["detail"] = " ".join(reasons) or "Measured PSU voltages from Explorer Channels."
            return result
        except (AttributeError, TypeError, ValueError, RuntimeError):
            return {**unavailable, "detail": f"{name} Channel readback unavailable."}

    def _update_output_table(self) -> None:
        table = getattr(self, "amxOutputTable", None)
        if table is None:
            return
        controller = getattr(self, "controller", None)
        connected = bool(getattr(controller, "initialized", False) and getattr(controller, "device", None) is not None)
        busy = bool(getattr(controller, "initializing", False) or getattr(controller, "transitioning", False))
        state = self._display_main_state()
        rows = getattr(controller, "output_rows", None)
        waveform_message = ""
        if not connected or busy or state != "STATE_ON" or not rows:
            if busy:
                text = "Updating"
            elif state == _AMX_SHUTDOWN_UNCONFIRMED_STATE:
                text = state
            elif not connected:
                text = "Disconnected"
            else:
                text = state if state != "STATE_ON" else "Awaiting readback"
            rows = [dict(channel=f"CH{i}", state=text, frequency="—", levels="Unknown", dwell="—", duty="—", relation="—",
                         detail="No current output waveform inferred. This is not confirmation of zero voltage.") for i in range(4)]
            waveform_message = f"No expected waveform: {text}. This does not mean 0 V."
        dark = getattr(self, "_amx_panel_dark", True)
        view = getattr(self, "amxWaveformView", None)
        if view is not None:
            device = getattr(controller, "device", None)
            clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
            view.set_model(_amx_waveform_traces(rows, clock_hz), waveform_message, dark)
        sources = [str(getattr(self, attr, "None") or "None") for attr in ("psu_ch01", "psu_ch23")]
        notice = getattr(self, "amxPanelNotice", None)
        if notice is not None:
            hint = " Assign each pair's PSU in Settings to show its voltages." if "None" in sources else ""
            notice.setText(_AMX_PANEL_NOTICE + hint)
        # Without a linked PSU, Levels only repeats the rail names: Vpos (%) already says 100 / 0.
        for column, (key, _header) in enumerate(_AMX_OUTPUT_COLUMNS):
            if key in ("levels", "psu"):
                table.setColumnHidden(column, sources == ["None", "None"])
        readbacks = {name: self._linked_psu_readback(name) for name in set(sources)}
        for index, original in enumerate(rows):
            row = dict(original)  # Never alter the controller's routing summary.
            name = sources[index // 2]
            readback = readbacks[name]
            levels = {}
            for rail, key in (("Vpos", "vpos"), ("Vneg", "vneg")):
                value = readback[key]
                levels[rail] = rail if value is None else (f"{value:+.6g} V" if value else "0 V")
            row["levels"] = row["levels"].replace("Vpos", levels["Vpos"]).replace("Vneg", levels["Vneg"])
            row["psu"] = "—" if name == "None" else name
            detail = row["detail"] + "\n" + readback["detail"]
            if name != "None":
                detail += (f"\n{name}: Vpos = {levels['Vpos']}, Vneg = {levels['Vneg']}. "
                           "Measured at the PSU, relative to its reference; external offset not included. "
                           "The association describes wiring only: it never controls the PSU.")
            for column, (key, _header) in enumerate(_AMX_OUTPUT_COLUMNS):
                item = table.item(index, column)
                item.setText(row[key])
                item.setToolTip(detail)
            self._color_signal_item(table.item(index, 1), row["state"], dark)

    @staticmethod
    def _color_signal_item(item: Any, state: str, dark: bool) -> None:
        """Flag a signal the panel could not infer; plain text means an inferred waveform or level."""
        _color_table_item(item, _AMX_SIGNAL_ROLES.get(state, "warning"), dark)

    def _update_channel_table_visibility(self) -> None:
        tree = getattr(self, "tree", None)
        advanced_action = getattr(self, "advancedAction", None)
        advanced = bool(getattr(advanced_action, "state", False))
        for name in ("amxPanelNotice", "amxWaveformLabel", "amxWaveformView"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setVisible(advanced)
        if hasattr(self, "amxTimingRows"):
            self._update_operator_panel()  # Advanced shows every pulser and its register state.
        if tree is None:
            return
        show_table = not hasattr(self, "amxPanel") or advanced
        setter = getattr(tree, "setVisible", None)
        if callable(setter):
            setter(show_table)

    def _apply_channel_items(self, items: list[dict[str, Any]]) -> None:
        update_channel_config = getattr(self, "updateChannelConfig", None)
        export_config = getattr(self, "exportConfiguration", None)
        custom_config_file = getattr(self, "customConfigFile", None)
        config_name = getattr(self, "confINI", None)
        if not callable(update_channel_config) or not callable(custom_config_file):
            return

        config_file = custom_config_file(config_name)
        self.loading = True
        if self.tree is not None:
            self.tree.setUpdatesEnabled(False)
        try:
            update_channel_config(items, config_file)
            if self.channels and self.tree is not None:
                self.tree.setHeaderLabels(
                    [
                        parameter_dict.get(Parameter.HEADER, "") or name.title()
                        for name, parameter_dict in self.channels[0].getSortedDefaultChannel().items()
                    ]
                )
                header = self.tree.header()
                if header is not None:
                    header.setStretchLastSection(False)
                    header.setMinimumSectionSize(0)
                    header.setSectionResizeMode(
                        type(header).ResizeMode.ResizeToContents
                    )
                for channel in self.getChannels():
                    collapse_changed = getattr(channel, "collapseChanged", None)
                    if callable(collapse_changed):
                        collapse_changed(toggle=False)
                self.tree.scheduleDelayedItemsLayout()
            if hasattr(self, "advancedAction"):
                self.toggleAdvanced(advanced=self.advancedAction.state)
            self._update_channel_column_visibility()
            estimate_storage = getattr(self, "estimateStorage", None)
            if callable(estimate_storage):
                estimate_storage()
            plugin_manager = getattr(self, "pluginManager", None)
            device_manager = getattr(plugin_manager, "DeviceManager", None)
            global_update = getattr(device_manager, "globalUpdate", None)
            if callable(global_update):
                global_update(inout=self.inout)
        finally:
            if self.tree is not None:
                self.tree.setUpdatesEnabled(True)
                self.tree.scheduleDelayedItemsLayout()
                self.tree.viewport().update()
            process_events = getattr(self, "processEvents", None)
            if callable(process_events):
                process_events()
            self.loading = False
        if callable(export_config):
            export_config(useDefaultFile=True)
        self._update_operator_panel()

    def _set_channel_headers_from_template(self) -> None:
        if self.tree is None:
            return
        self.tree.setHeaderLabels(
            [
                parameter_dict.get(Parameter.HEADER, "") or name.title()
                for name, parameter_dict in self._default_channel_template().items()
            ]
        )

    def _update_channel_column_visibility(self) -> None:
        """Hide framework columns and configure key columns as user-resizable."""
        if self.tree is None:
            return
        set_root_decorated = getattr(self.tree, "setRootIsDecorated", None)
        if callable(set_root_decorated):
            set_root_decorated(False)
        if not self.channels:
            return

        parameter_names = list(self.channels[0].getSortedDefaultChannel())
        for hidden_name in (
            getattr(Channel, "COLLAPSE", "Collapse"),
            getattr(Channel, "REAL", "Real"),
        ):
            if hidden_name in parameter_names:
                self.tree.setColumnHidden(parameter_names.index(hidden_name), True)

        # Make key data columns user-resizable with sensible defaults.
        header_getter = getattr(self.tree, "header", None)
        header = header_getter() if callable(header_getter) else None
        if header is not None:
            for parameter_name, default_width in (
                (Channel.VALUE, 90),         # Width (us)
                (self.channelType.DUTY, 65),  # Duty (%)
                (Channel.MONITOR, 100),       # Duty (Monitor)
                (self.channelType.FREQ_KHZ, 80),  # Freq (kHz)
            ):
                if parameter_name in parameter_names:
                    col_index = parameter_names.index(parameter_name)
                    header.setSectionResizeMode(
                        col_index, type(header).ResizeMode.Interactive
                    )
                    header.resizeSection(col_index, default_width)

    def _sync_channels(self) -> bool:
        current_items = self._current_channel_items()
        target_items, log_entries = _plan_channel_sync(
            current_items=current_items,
            device_name=self.name,
            default_item=self._default_channel_item(),
        )
        if target_items == current_items:
            return False
        self._apply_channel_items(target_items)
        for message, flag in log_entries:
            if flag is None:
                self.print(message)
            else:
                self.print(message, flag=flag)
        return True

    def loadConfiguration(
        self,
        file: "Path | None" = None,
        useDefaultFile: bool = False,
        append: bool = False,
    ) -> None:
        if useDefaultFile:
            file = self.customConfigFile(self.confINI)

        if (
            useDefaultFile
            and file not in {None, Path()}
            and cast(Path, file).suffix.lower() == ".ini"
            and not cast(Path, file).exists()
            and not self.channels
        ):
            self.loading = True
            if self.tree is not None:
                self.tree.setUpdatesEnabled(False)
                self.tree.setRootIsDecorated(False)
            try:
                self.print(
                    f"AMX config file {file} not found. "
                    "Channels will be created after successful hardware initialization."
                )
                self._set_channel_headers_from_template()
                if hasattr(self, "advancedAction"):
                    self.toggleAdvanced(advanced=self.advancedAction.state)
                if self.tree is not None:
                    self.tree.scheduleDelayedItemsLayout()
                plugin_manager = getattr(self, "pluginManager", None)
                device_manager = getattr(plugin_manager, "DeviceManager", None)
                global_update = getattr(device_manager, "globalUpdate", None)
                if callable(global_update):
                    global_update(inout=self.inout)
            finally:
                if self.tree is not None:
                    self.tree.setUpdatesEnabled(True)
                self.loading = False
            self._update_operator_panel()
            self._update_channel_table_visibility()
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)
        self._update_operator_panel()
        self._update_channel_table_visibility()

    def toggleAdvanced(self, advanced: "bool | None" = False) -> None:
        if self.channels:
            super().toggleAdvanced(advanced=advanced)
            for channel in self.getChannels():
                channel.setHidden(False)
            self._update_channel_column_visibility()
            self._update_channel_table_visibility()
            self._update_operator_panel()
            return

        if advanced is not None:
            self.advancedAction.state = advanced
        for action_name in (
            "importAction",
            "exportAction",
            "duplicateChannelAction",
            "deleteChannelAction",
            "moveChannelUpAction",
            "moveChannelDownAction",
        ):
            action = getattr(self, action_name, None)
            if action is not None:
                action.setVisible(self.advancedAction.state)
        if self.tree is None:
            return
        for index, item in enumerate(self._default_channel_template().values()):
            if item.get(_PARAMETER_ADVANCED_KEY, False):
                self.tree.setColumnHidden(index, not self.advancedAction.state)
        self._update_channel_table_visibility()

    def estimateStorage(self) -> None:
        """Keep timestamps and channel histories on the same nonzero capacity."""
        if self.channels:
            super().estimateStorage()
        else:
            # Channel.__init__ captures this limit before hardware discovery.
            self.maxDataPoints = 100_000
            widget = self.pluginManager.Settings.settings[
                f"{self.name}/{self.MAXDATAPOINTS}"
            ].getWidget()
            if widget:
                widget.setToolTip(
                    "Storage estimate will be available after the first successful "
                    "AMX hardware initialization."
                )

        limit = self.maxDataPoints
        time_buffer = getattr(self, "time", None)
        if time_buffer is not None:
            if time_buffer.size and time_buffer.max_size and time_buffer.max_size > 0:
                # Storage edits apply to the next history, not an active recording.
                limit = time_buffer.max_size
            time_buffer.max_size = limit
        for channel in self.channels:
            for name in ("values", "backgrounds"):
                buffer = getattr(channel, name, None)
                if buffer is not None:
                    buffer.max_size = limit

    def frequencyChanged(self, *, debounce: bool = False) -> None:
        self._follow_timing_basis()
        self._update_width_bounds()
        self._update_operator_panel()
        controller = getattr(self, "controller", None)
        if (
            controller is None
            or getattr(self, "loading", False)
            or not getattr(controller, "initialized", False)
            or not getattr(self, "isOn", lambda: False)()
        ):
            return
        if debounce:
            self._schedule_global_settings_apply()
            return
        self._cancel_global_settings_apply()
        self._apply_global_settings_now()

    def _timing_constants(self) -> tuple[float, int, int, int]:
        controller = getattr(self, "controller", None)
        device = getattr(controller, "device", None) if controller is not None else None
        return (
            _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6),
            _coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2),
            _coerce_int(_safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2), 2),
            _coerce_int(_safe_device_attr(device, "PULSER_DELAY_OFFSET", 3), 3),
        )

    def _keep_duty_cycles(self, old_khz: float, new_khz: float) -> None:
        """Rescale the Manual widths of pulsers keeping %; the next apply writes them."""
        clock_hz, osc_offset, width_offset, _ = self._timing_constants()
        old_period = _period_ticks(old_khz, clock_hz=clock_hz, oscillator_offset=osc_offset)
        new_period = _period_ticks(new_khz, clock_hz=clock_hz, oscillator_offset=osc_offset)
        if not old_period or not new_period or old_period == new_period:
            return
        ticks_per_us = clock_hz / 1e6
        controller = getattr(self, "controller", None)
        for channel in self.getChannels():
            if (not channel.real or not _keeps_duty(channel)
                    or not self._channel_parameter_value(channel, getattr(channel, "ACTIVE", "Active"), "active", True)
                    or not _pulser_follows_oscillator(controller, channel.pulser_number())):
                continue  # Equation widths and other triggers do not follow the oscillator.
            width_us = _coerce_float(getattr(channel, "value", 0.0), 0.0)
            if width_us <= 0.0:
                continue
            high, channel._duty_anchor = _rescale_ticks(
                getattr(channel, "_duty_anchor", None), round(width_us * ticks_per_us), old_period, new_period,
                minimum=width_offset + 1, maximum=max(new_period - width_offset, width_offset + 1))
            getter = getattr(channel, "getParameterByName", None)
            parameter = getter(getattr(channel, "VALUE", "Value")) if callable(getter) else None
            _set_config_parameter(channel, "value", parameter, high / ticks_per_us)
            channel.lastAppliedValue = channel.value
            update_duty = getattr(channel, "_update_duty_label", None)
            if callable(update_duty):
                update_duty()

    def _apply_global_settings_now(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        apply_global = getattr(controller, "applyGlobalSettingsFromThread", None)
        if callable(apply_global):
            apply_global(parallel=True)
            return
        apply_global = getattr(controller, "applyGlobalSettings", None)
        if callable(apply_global):
            apply_global()

    def _global_settings_apply_timer(self) -> Any | None:
        timer = getattr(self, "_globalSettingsApplyTimer", None)
        if timer is not None:
            return timer
        try:
            from PyQt6.QtCore import QTimer
        except Exception:
            return None
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._apply_global_settings_now)
        self._globalSettingsApplyTimer = timer
        return timer

    def _schedule_global_settings_apply(self) -> None:
        timer = self._global_settings_apply_timer()
        if timer is None:
            self._apply_global_settings_now()
            return
        timer.start(_AMX_NUMERIC_DEBOUNCE_MS)

    def _cancel_global_settings_apply(self) -> None:
        timer = getattr(self, "_globalSettingsApplyTimer", None)
        stop = getattr(timer, "stop", None)
        if callable(stop):
            stop()

    def _cancel_runtime_apply_timers(self) -> None:
        self._cancel_global_settings_apply()
        channels = []
        with contextlib.suppress(Exception):
            channels = list(self.getChannels() or [])
        for channel in channels:
            cancel_apply = getattr(channel, "_cancel_value_apply", None)
            if callable(cancel_apply):
                cancel_apply()

    def _update_width_bounds(self) -> None:
        """Update Width (us) max bound on all channels based on current frequency."""
        freq_khz = float(getattr(self, "frequency_khz", 2.0))
        if freq_khz <= 0:
            return
        period_us = 1000.0 / freq_khz
        controller = getattr(self, "controller", None)
        device = getattr(controller, "device", None) if controller is not None else None
        width_offset = _safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2) if device is not None else 2
        ticks_per_us = _safe_device_attr(device, "CLOCK", 100e6) / 1e6 if device is not None else 100.0
        max_width_us = max(period_us - width_offset / ticks_per_us, 0.0)
        for channel in self.getChannels():
            param = channel.getParameterByName(channel.VALUE)
            if param is None:
                continue
            setattr(param, _PARAMETER_MAX_KEY, max_width_us)
            current = _coerce_float(getattr(channel, "value", 0.0), 0.0)
            if current > max_width_us:
                setter = getattr(param, "setValueWithoutEvents", None)
                if callable(setter):
                    setter(max_width_us)
                channel.value = max_width_us
            channel._update_duty_label()

    def _set_on_ui_state(self, on: bool) -> None:
        def _update_gui() -> None:
            state = bool(on)
            for action_name in ("onAction", "deviceOnAction"):
                action = getattr(self, action_name, None)
                if action is None:
                    continue
                signal_comm = getattr(action, "signalComm", None)
                thread_signal = getattr(signal_comm, "setValueFromThreadSignal", None)
                if thread_signal is not None:
                    thread_signal.emit(state)
                else:
                    action.state = state
            self._sync_local_on_action()
            self._sync_toolbar_communication_controls()
            self._update_status_widgets()

        _invoke_gui_callback(_update_gui)

    def _set_action_enabled(self, action: Any | None, enabled: bool) -> None:
        """Update QAction-like enabled state while tolerating lightweight test doubles."""
        if action is None:
            return
        if hasattr(action, "setEnabled"):
            action.setEnabled(enabled)
            return
        with contextlib.suppress(AttributeError):
            setattr(action, "enabled", enabled)

    def _set_action_visible(self, action: Any | None, visible: bool) -> None:
        """Update QAction-like visibility while tolerating lightweight test doubles."""
        if action is None:
            return
        if hasattr(action, "setVisible"):
            action.setVisible(visible)
            return
        with contextlib.suppress(AttributeError):
            setattr(action, "visible", visible)

    def _force_recording_action_state(self, state: bool) -> None:
        """Force the acquisition action state without re-entering its callbacks."""
        for action in (
            getattr(self, "recordingAction", None),
            getattr(getattr(self, "liveDisplay", None), "recordingAction", None),
        ):
            if action is None:
                continue
            blocker = getattr(action, "blockSignals", None)
            if callable(blocker):
                blocker(True)
            try:
                if hasattr(action, "state"):
                    action.state = bool(state)
                elif hasattr(action, "setChecked"):
                    action.setChecked(bool(state))
            finally:
                if callable(blocker):
                    blocker(False)

    def _display_communication_actions(self) -> tuple[Any | None, Any | None]:
        """Return the Live Display init/close actions when available."""
        live_display = getattr(self, "liveDisplay", None)
        if live_display is None:
            return None, None

        close_action = getattr(live_display, "closeCommunicationAction", None)
        init_action = getattr(live_display, "initCommunicationAction", None)
        if close_action is not None and init_action is not None:
            return close_action, init_action

        title_bar = getattr(live_display, "titleBar", None)
        get_actions = getattr(title_bar, "actions", None)
        if not callable(get_actions):
            return close_action, init_action

        close_label = f"Close {self.name} communication."
        init_label = f"Initialize {self.name} communication."
        for action in get_actions():
            label = _action_label(action)
            if close_action is None and label == close_label:
                close_action = action
                setattr(live_display, "closeCommunicationAction", action)
            elif init_action is None and label == init_label:
                init_action = action
                setattr(live_display, "initCommunicationAction", action)
        return close_action, init_action

    def _communication_open(self) -> bool:
        """Return whether a transport/driver exists, even after a partial startup failure."""
        controller = getattr(self, "controller", None)
        if controller is None:
            return False
        if getattr(controller, "device", None) is not None:
            return True
        return bool(getattr(controller, "initialized", False))

    def _sync_display_communication_controls(self) -> None:
        """Enable display-side communication actions only when applicable."""
        close_action, init_action = self._display_communication_actions()
        controller = getattr(self, "controller", None)
        busy = bool(getattr(controller, "initializing", False)) or bool(
            getattr(controller, "transitioning", False)
        )
        communication_open = self._communication_open()
        self._set_action_enabled(close_action, communication_open and not busy)
        self._set_action_enabled(init_action, (not communication_open) and (not busy))

    def _sync_toolbar_communication_controls(self) -> None:
        """Expose a disconnect action when communication is open but the local ON action is OFF."""
        close_action = getattr(self, "closeCommunicationAction", None)
        if close_action is None:
            return
        controller = getattr(self, "controller", None)
        busy = bool(getattr(controller, "initializing", False)) or bool(
            getattr(controller, "transitioning", False)
        )
        can_close = self._communication_open() and not busy
        is_on = False
        is_on_fn = getattr(self, "isOn", None)
        if callable(is_on_fn):
            is_on = bool(is_on_fn())
        has_local_on_action = getattr(self, "onAction", None) is not None
        self._set_action_enabled(close_action, can_close)
        self._set_action_visible(close_action, can_close and (not has_local_on_action or not is_on))

    def _sync_acquisition_controls(self) -> None:
        """Disable manual acquisition controls until the AMX is actually ready."""
        ready, _reason = self._acquisition_readiness()
        self._sync_display_communication_controls()
        self._sync_toolbar_communication_controls()
        self._set_action_enabled(getattr(self, "recordingAction", None), ready)
        self._set_action_enabled(
            getattr(getattr(self, "liveDisplay", None), "recordingAction", None),
            ready,
        )
        if not ready and not bool(getattr(self, "recording", False)):
            self._force_recording_action_state(False)

    def toggleRecording(self, on: "bool | None" = None, manual: bool = True) -> None:
        """Only allow data recording when the AMX is initialized and in ST_ON."""
        requested_on = (not bool(getattr(self, "recording", False))) if on is None else bool(on)
        ready, reason = self._acquisition_readiness()
        if requested_on and not ready:
            self._force_recording_action_state(False)
            self._sync_acquisition_controls()
            if manual:
                self.print(
                    f"Cannot start {self.name} data acquisition: {reason}.",
                    flag=PRINT.WARNING,
                )
            return

        super().toggleRecording(on=on, manual=manual)
        self._sync_acquisition_controls()
        _session_sync(self)

    def closeCommunication(self) -> None:
        _session_clear(self)  # A disconnection or Explorer closing is never resumed.
        controller = getattr(self, "controller", None)
        if self.useOnOffLogic and not hasattr(self, "onAction"):
            if controller:
                controller.closeCommunication()
            self.recording = False
            self._sync_acquisition_controls()
            self._update_status_widgets()
            return
        if controller and getattr(controller, "initialized", False):
            self.shutdownCommunication()
            return
        if hasattr(self, "onAction"):
            self.onAction.state = False
            self._sync_local_on_action()
        if controller:
            controller.closeCommunication()
        self.recording = False
        self._sync_acquisition_controls()
        self._update_status_widgets()

    def shutdownCommunication(self) -> None:
        shutdown_confirmed = True
        controller = getattr(self, "controller", None)
        if controller:
            shutdown_confirmed = bool(controller.shutdownCommunication())
        connection_pending = bool(
            controller and getattr(controller, "_initial_open_incomplete", lambda: False)()
        )
        native_lost = bool(controller and getattr(controller, "_native_transport_lost", False))
        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = not shutdown_confirmed and not connection_pending and not native_lost
            self._sync_local_on_action()
        if not shutdown_confirmed and not connection_pending:
            self.print(
                "AMX native communication was lost; the OFF button is not hardware "
                "OFF confirmation. Make the hardware safe before reconnecting."
                if native_lost else
                "AMX shutdown could not be confirmed; UI remains ON until "
                "the hardware state is verified.",
                flag=PRINT.WARNING,
            )
        self.recording = False
        self._sync_acquisition_controls()
        self._update_status_widgets()

    def _finish_setpoint_edits(self, channel: Any = None) -> None:
        setting = self._setting(self.FREQUENCY_KHZ)
        for widget in (getattr(self, "frequencyWidget", None), getattr(setting, "spin", None)):
            if _finish_spinbox_edit(widget):
                loading = getattr(setting, "loading", False)
                if setting is not None:
                    setting.loading = True
                try:
                    self._set_frequency_setting_value(float(widget.value()))
                finally:
                    if setting is not None:
                        setting.loading = loading
                save = getattr(setting, "settingEvent", None)
                if callable(save):
                    save()
        for ch in [channel] if channel is not None else self.getChannels():
            row = getattr(self, "amxTimingRows", {}).get(ch.pulser_number(), {}) or {}
            width = row.get("width")
            draft = float(width.value()) if _finish_spinbox_edit(width) else None
            duty = row.get("duty")
            if draft is None and _finish_spinbox_edit(duty):
                draft = self._duty_width_us(float(duty.value()))
            if draft is not None:
                # Suppress the value event while committing the draft; the caller applies it.
                # Explorer 1.0.2: Channel.loading is read-only and reflects this device counter.
                self.loading = True
                try:
                    ch.value = draft
                finally:
                    self.loading = False
            else:
                getter = getattr(ch, "getParameterByName", None)
                parameter = getter(getattr(ch, "VALUE", "Value")) if callable(getter) else None
                _finish_spinbox_edit(getattr(parameter, "spin", None))

    def setOn(self, on: "bool | None" = None) -> None:
        _session_request(self, bool(on) if on is not None
                         else bool(self.isOn()) if hasattr(self, "onAction") else False)  # Crash resume record.
        controller = getattr(self, "controller", None)
        current_state = self.isOn() if hasattr(self, "onAction") else False
        transition_target = getattr(controller, "transition_target_on", None)
        requested_on = current_state if on is None else bool(on)
        if (controller and not requested_on
                and getattr(controller, "_native_backend", lambda _: None)(getattr(controller, "device", None)) is not None
                and (getattr(controller, "initializing", False)
                     or getattr(controller, "transitioning", False))):
            controller._initial_open_close_requested = True
            controller._cancel_native_operation()
            self._set_on_ui_state(False)
            return
        if controller and getattr(controller, "initializing", False) and not requested_on:
            # The initialization worker owns Open and will perform verified
            # shutdown if it succeeds. Never start a competing output worker.
            controller._initial_open_close_requested = True
            self._set_on_ui_state(False)
            return
        if controller and (
            getattr(controller, "initializing", False)
            or getattr(controller, "transitioning", False)
        ):
            restored_state = current_state if transition_target is None else bool(transition_target)
            if hasattr(self, "onAction"):
                self.onAction.state = restored_state
            self._sync_local_on_action()
            self.print(
                f"{self.name} ON/OFF transition already in progress; ignoring additional request.",
                flag=PRINT.WARNING,
            )
            return

        if on is not None and hasattr(self, "onAction") and self.onAction.state is not on:
            self.onAction.state = on
        self._sync_local_on_action()
        self._update_status_widgets()
        if getattr(self, "loading", False):
            return

        if (controller and not self.isOn()
                and getattr(controller, "_initial_open_incomplete", lambda: False)()):
            self.shutdownCommunication()
            return
        if self.isOn():
            self._finish_setpoint_edits()
        if controller and getattr(controller, "initialized", False):
            begin_transition = getattr(controller, "_begin_transition", None)
            can_start = not callable(begin_transition) or begin_transition(self.isOn())
            if can_start:
                toggle_thread = getattr(controller, "toggleOnFromThread", None)
                if callable(toggle_thread):
                    toggle_thread(parallel=True)
                else:
                    controller.toggleOn()
        elif hasattr(self, "onAction") and self.isOn():
            initialize_communication = getattr(self, "initializeCommunication", None)
            if callable(initialize_communication):
                initialize_communication()


class _AMXChannelCommon:
    """The AMXChannel methods every AMX plugin shares; its own class lists it first in its bases."""

    def nameChanged(self) -> None:
        super().nameChanged()
        self.updateDisplay()  # Explorer only refreshes renamed OUT channels by default.

    def getDefaultChannel(self) -> dict[str, dict]:
        self.id: int
        self.delay_us: str
        self.width_ticks: str
        self.burst: str

        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Width (us)"
        channel[self.VALUE][_PARAMETER_MIN_KEY] = 0.0
        channel[self.VALUE][_PARAMETER_EVENT_KEY] = self.valueChanged
        channel[self.ENABLED][_PARAMETER_ADVANCED_KEY] = False
        channel[self.ENABLED][Parameter.HEADER] = "On"
        channel[self.ENABLED][_PARAMETER_TOOLTIP_KEY] = (
            "Apply this pulser timing to the AMX when the device is ON."
        )
        channel[self.ACTIVE][Parameter.HEADER] = "Manual"
        channel[self.DISPLAY][Parameter.HEADER] = "Display"
        channel[self.DISPLAY][_PARAMETER_EVENT_KEY] = self.displayChanged
        channel[self.SCALING][Parameter.VALUE] = "large"
        monitor_name = getattr(self, "MONITOR", "Monitor")
        if monitor_name in channel:
            channel[monitor_name][Parameter.HEADER] = "Duty (Monitor)"
        channel[self.DUTY] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Duty (%)",
            attr="duty_text",
        )
        channel[self.FREQ_KHZ] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Freq (kHz)",
            attr="freq_khz_text",
        )
        channel[self.KEEP] = parameterDict(
            value=_AMX_KEEP_DUTY,
            parameterType=PARAMETERTYPE.COMBO,
            items=f"{_AMX_KEEP_DUTY},{_AMX_KEEP_WIDTH}",
            fixedItems=True,
            advanced=False,
            header="Keep",
            toolTip=_AMX_KEEP_TOOLTIP,
            attr="keep",
            event=self.keepChanged,
        )
        channel[self.ID] = parameterDict(
            value="0",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Pulser",
            attr="id",
        )
        channel[self.DELAY_US] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=True,
            indicator=True,
            header="Delay (us)",
            attr="delay_us",
        )
        channel[self.WIDTH_TICKS] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=True,
            indicator=True,
            header="Width ticks",
            attr="width_ticks",
        )
        channel[self.BURST] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=True,
            indicator=True,
            header="Burst",
            attr="burst",
        )
        return channel

    def setDisplayedParameters(self) -> None:
        super().setDisplayedParameters()
        # Remove unused columns.
        for name in (self.OPTIMIZE, self.MIN, self.MAX):
            if name in getattr(self, "displayedParameters", []):
                self.displayedParameters.remove(name)
        # Move display-related columns to the end.
        for name in (self.DISPLAY, self.ACTIVE):
            if name in self.displayedParameters:
                self.displayedParameters.remove(name)
        # AMX-specific columns before display controls.
        self.displayedParameters.extend([
            self.DUTY, self.KEEP, self.FREQ_KHZ, self.ID,
            self.DELAY_US, self.WIDTH_TICKS, self.BURST,
            self.ACTIVE, self.DISPLAY,
        ])

    def initGUI(self, item: dict) -> None:
        # Legacy AMX channel configs may reach core.Channel.updateMin()/updateMax()
        # before framework attributes are hydrated from the stored config.
        if not hasattr(self, "active"):
            self.active = _coerce_bool(item.get(getattr(self, "ACTIVE", "Active")), True)
        if not hasattr(self, "enabled"):
            self.enabled = _coerce_bool(
                item.get(getattr(self, "ENABLED", "Enabled")),
                False,
            )
        if not hasattr(self, "real"):
            self.real = _coerce_bool(item.get(getattr(self, "REAL", "Real")), True)
        if not hasattr(self, "value"):
            self.value = _coerce_float(item.get(getattr(self, "VALUE", "Value")), 0.0)
        if not hasattr(self, "min"):
            self.min = _coerce_float(item.get(getattr(self, "MIN", "Min")), 0.0)
        if not hasattr(self, "max"):
            max_value = item.get(getattr(self, "MAX", "Max"))
            channel_parent = getattr(self, "channelParent", None)
            freq_khz = _coerce_float(getattr(channel_parent, "frequency_khz", 2.0), 2.0)
            default_max = 1000.0 / freq_khz if freq_khz > 0 else 500.0
            self.max = _coerce_float(max_value, default_max)
        super().initGUI(item)
        self._upgrade_toggle_widget(
            self.ENABLED,
            _AMX_PULSER_ON_LABEL,
            _AMX_PULSER_TOGGLE_MIN_WIDTH,
        )
        self._upgrade_toggle_widget(self.ACTIVE, "Manual", 72)
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        self._configure_value_editor()
        self.scalingChanged()

    def _configure_value_editor(self) -> None:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        try:
            parameter = getter(getattr(self, "VALUE", "Value"))
        except Exception:
            return
        if parameter is None:
            return
        get_widget = getattr(parameter, "getWidget", None)
        widget = get_widget() if callable(get_widget) else getattr(parameter, "check", None)
        if hasattr(widget, "setKeyboardTracking"):
            widget.setKeyboardTracking(False)
        _disable_spinbox_wheel(widget)

    def scalingChanged(self) -> None:
        super().scalingChanged()
        if self.rowHeight >= _AMX_MIN_ROW_HEIGHT:
            return
        self.rowHeight = _AMX_MIN_ROW_HEIGHT
        for parameter in self.parameters:
            parameter.setHeight(self.rowHeight)
        if not self.loading and self.tree:
            self.tree.scheduleDelayedItemsLayout()

    def _upgrade_toggle_widget(
        self,
        parameter_name: str,
        label: str,
        minimum_width: int,
    ) -> None:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(parameter_name)
        if parameter is None:
            return

        initial_value = bool(getattr(parameter, "value", False))
        parameter.widget = ToolButton()
        parameter.applyWidget()
        if getattr(parameter, "check", None):
            parameter.check.setMaximumHeight(
                max(getattr(parameter, "rowHeight", _AMX_MIN_ROW_HEIGHT), _AMX_MIN_ROW_HEIGHT)
            )
            parameter.check.setMinimumWidth(minimum_width)
            parameter.check.setText(label)
            parameter.check.setCheckable(True)
            if hasattr(parameter.check, "setAutoRaise"):
                parameter.check.setAutoRaise(False)
        parameter.value = initial_value

    def _enabled_toggle_label(self) -> str:
        return _AMX_PULSER_ON_LABEL if bool(getattr(self, "enabled", False)) else _AMX_PULSER_OFF_LABEL

    def _sync_enabled_toggle_widget(self) -> None:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(getattr(self, "ENABLED", "Enabled"))
        widget = getattr(parameter, "check", None) if parameter is not None else None
        if widget is not None and hasattr(widget, "setText"):
            widget.setText(self._enabled_toggle_label())

    def pulser_number(self) -> int:
        return _coerce_int(self.id, 0)

    def _set_parameter_value_without_events(self, parameter_name: str, value: Any) -> bool:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return False
        parameter = getter(parameter_name)
        if parameter is None:
            return False
        current_value = getattr(parameter, "value", None)
        if current_value == value:
            return False
        setter = getattr(parameter, "setValueWithoutEvents", None)
        if callable(setter):
            setter(value)
        else:
            parameter.value = value
        return True

    def displayChanged(self) -> None:
        update_display = getattr(super(), "updateDisplay", None)
        if callable(update_display):
            update_display()
        self._refresh_operator_panel()

    def activeChanged(self) -> None:
        base_handler = getattr(super(), "activeChanged", None)
        if callable(base_handler):
            base_handler()
        self._refresh_operator_panel()

    def updateColor(self):
        """Keep the Display checkbox centered in its column."""
        color = super().updateColor()
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QCheckBox, QSizePolicy
        except Exception:
            return color

        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return color
        display_param = getter(self.DISPLAY)
        if display_param is None:
            return color
        display_widget = display_param.getWidget()
        if not isinstance(display_widget, QCheckBox):
            return color
        display_widget.setSizePolicy(
            QSizePolicy.Policy.Maximum,
            display_widget.sizePolicy().verticalPolicy(),
        )
        if hasattr(display_widget, "container") and display_widget.container.layout():
            display_widget.container.layout().setAlignment(
                display_widget, Qt.AlignmentFlag.AlignCenter
            )
        return color

    def realChanged(self) -> None:
        getter = getattr(self, "getParameterByName", None)
        if callable(getter):
            for parameter_name in (self.ID, self.DUTY, self.FREQ_KHZ, self.DELAY_US, self.WIDTH_TICKS, self.BURST):
                parameter = getter(parameter_name)
                if parameter is not None and hasattr(parameter, "setVisible"):
                    parameter.setVisible(self.real)
        real_changed = getattr(super(), "realChanged", None)
        if callable(real_changed):
            real_changed()

    def monitorChanged(self) -> None:
        self._sync_monitor_feedback()

    def keepChanged(self) -> None:
        self._refresh_operator_panel()

    def valueChanged(self) -> None:
        base_handler = getattr(super(), "valueChanged", None)
        if callable(base_handler):
            base_handler()
        self._update_duty_label()
        self._sync_monitor_feedback()
        if not getattr(self.channelParent, "loading", False):
            self.channelParent._timing_edited = True
            self._schedule_value_apply()
        self._refresh_operator_panel()

    def enabledChanged(self) -> None:
        base_handler = getattr(super(), "enabledChanged", None)
        if callable(base_handler):
            base_handler()
        if not getattr(self, "enabled", False):
            self.monitor = np.nan
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        self._cancel_value_apply()
        if not getattr(self.channelParent, "loading", False):
            self.channelParent._timing_edited = True
        self._apply_value_now()
        self._refresh_operator_panel()

    def _refresh_operator_panel(self) -> None:
        refresh = getattr(self.channelParent, "_update_operator_panel", None)
        if callable(refresh):
            refresh()

    def _value_apply_timer(self) -> Any | None:
        timer = getattr(self, "_valueApplyTimer", None)
        if timer is not None:
            return timer
        try:
            from PyQt6.QtCore import QTimer
        except Exception:
            return None
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._apply_value_now)
        self._valueApplyTimer = timer
        return timer

    def _schedule_value_apply(self) -> None:
        timer = self._value_apply_timer()
        if timer is None:
            self._apply_value_now()
            return
        timer.start(_AMX_NUMERIC_DEBOUNCE_MS)

    def _cancel_value_apply(self) -> None:
        timer = getattr(self, "_valueApplyTimer", None)
        stop = getattr(timer, "stop", None)
        if callable(stop):
            stop()

    def _update_duty_label(self) -> None:
        """Duty (%) of the requested width, on the clock grid of the requested frequency."""
        self.setDutyText(f"{_requested_duty_percent(self.channelParent, getattr(self, 'value', 0.0)):.1f}")

    def _monitor_feedback_state(self) -> str:
        """Classify duty monitor accuracy relative to the current duty setpoint."""
        if not getattr(self, "enabled", False) or not getattr(self, "real", True):
            return "default"

        channel_parent = getattr(self, "channelParent", None)
        controller = getattr(channel_parent, "controller", None)
        if controller is None or not getattr(controller, "acquiring", False):
            return "default"
        if not callable(getattr(channel_parent, "isOn", None)) or not channel_parent.isOn():
            return "default"

        monitor_value = _coerce_float(getattr(self, "monitor", np.nan), np.nan)
        target_value = _coerce_float(getattr(self, "duty_text", np.nan), np.nan)
        if _is_nan(monitor_value) or _is_nan(target_value):
            return "default"

        reference = max(abs(target_value), _AMX_MONITOR_RELATIVE_FLOOR_DUTY)
        relative_error = abs(monitor_value - target_value) / reference
        if relative_error <= _AMX_MONITOR_OK_RELATIVE_TOLERANCE:
            return "ok"
        if relative_error <= _AMX_MONITOR_WARN_RELATIVE_TOLERANCE:
            return "warn"
        return "error"

    def _sync_monitor_feedback(self) -> None:
        """Apply green/orange/red background on Duty (Monitor) like AMPR."""
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        try:
            parameter = getter(getattr(self, "MONITOR", "Monitor"))
        except Exception:  # noqa: BLE001
            return
        widget_getter = getattr(parameter, "getWidget", None)
        widget = widget_getter() if callable(widget_getter) else getattr(parameter, "widget", None)
        if widget is None or not hasattr(widget, "setStyleSheet"):
            return

        state = self._monitor_feedback_state()
        if state == "ok":
            style = _AMX_MONITOR_OK_STYLE
        elif state == "warn":
            style = _AMX_MONITOR_WARN_STYLE
        elif state == "error":
            style = _AMX_MONITOR_ERROR_STYLE
        else:
            style = _AMX_MONITOR_NEUTRAL_STYLE

        widget.setStyleSheet(style)
        self.warningState = state in {"warn", "error"}

    def setWidthText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.WIDTH_TICKS, text)

    def setDelayText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.DELAY_US, text)

    def setDutyText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.DUTY, text)

    def setFreqText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.FREQ_KHZ, text)

    def setBurstText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.BURST, text)


class _AMXControllerCommon:
    """The AMXController methods every AMX plugin shares; its own class lists it first in its bases."""

    def initializeCommunication(self) -> None:
        if getattr(self, "initializing", False):
            return
        self._retire_failed_native_worker()
        acquisition_thread = getattr(self, "acquisitionThread", None)
        if acquisition_thread is not None and acquisition_thread.is_alive():
            self.closeCommunication()
        # Only a new explicit request may forget a previous OFF/close. Arm it
        # before scheduling the worker, never when that worker eventually runs.
        self._initial_open_close_requested = False
        super().initializeCommunication()

    def _init_failure_guidance(self, exc: Exception) -> str:
        """Operator guidance appended to an init-failure message, or "" if none.

        Tracks whether a transport was poisoned by a timed-out DLL call earlier
        in this session so that a later failed retry says what to check.
        """
        if self._native_transport_lost:
            return (
                "The native worker was retired; output safety is not confirmed. "
                "Make the hardware safe, then retry ON explicitly to start a fresh worker. "
                "No outputs are automatically re-enabled."
            )
        if self._initial_open_incomplete():
            return (
                "The failed initial connection is retained until its native call finishes "
                "and port closure is confirmed. Power the device on and retry ON explicitly. "
                "If the call remains blocked or closure cannot be confirmed, restart Explorer "
                "after making the hardware safe. No outputs are automatically re-enabled."
            )
        current_com = _coerce_int(getattr(self.controllerParent, "com", None), -1)
        guidance = _amx_poisoned_port_guidance(
            exc,
            poisoned_com=getattr(self, "_poisoned_com", None),
            current_com=current_com,
        )
        if _amx_transport_failure_is_fatal(exc) and current_com >= 0:
            self._poisoned_com = current_com
        return guidance

    def initComplete(self, *, native_device=None, native_token=None) -> None:
        if native_device is not None:
            if (native_device is not self.device
                    or native_token is not self._native_cancel_event
                    or native_token.is_set()
                    or getattr(self, "_native_ready_device", None) is not native_device):
                return
        elif self.device is not None:
            self._retire_failed_native_worker()
            return  # A parameterless signal cannot identify its native session.
        if self._retire_failed_native_worker():
            return
        if self.device is None or self._initial_open_close_requested:
            return  # A queued success must not undo an explicit OFF/close.
        self._finalize_transport_initialization()
        self._resume_pending_on_request_after_transport_ready()

    def _finalize_transport_initialization(self) -> None:
        """Sync transport state to the GUI after communication becomes ready."""
        if self.device is not None:
            self.controllerParent._sync_channels()
        self.initializeValues(reset=True)
        self.initialized = True
        # A fresh transport reached this far, so any earlier in-process port
        # poisoning is no longer relevant for this COM port.
        self._poisoned_com = None
        self._consecutive_poll_errors = 0
        self.super_init_complete_called = True
        self._sync_status_to_gui()

    def _resolved_safety_config(self, attr_name: str) -> int:
        config_index = _coerce_int(getattr(self.controllerParent, attr_name, -1), -1)
        if config_index >= 0:
            return config_index
        exact_matches = [
            _coerce_int(config.get("index"), -1)
            for config in self.available_configs
            if _coerce_bool(config.get("valid"), True)
            and str(config.get("name", "")).strip().lower() == "standby"
        ]
        if exact_matches:
            return exact_matches[0]
        partial_matches = [
            _coerce_int(config.get("index"), -1)
            for config in self.available_configs
            if _coerce_bool(config.get("valid"), True)
            and "standby" in str(config.get("name", "")).strip().lower()
        ]
        if len(partial_matches) == 1:
            return partial_matches[0]
        return -1

    def _selected_operating_config_index(self) -> int:
        parent = self.controllerParent
        getter = getattr(parent, "_config_setting_value", None)
        if callable(getter):
            return getter("operating_config")
        return _coerce_int(getattr(parent, "operating_config", -1), -1)

    def _config_entry_by_index(self, config_index: int) -> dict[str, Any] | None:
        for entry in self.available_configs:
            if _coerce_int(entry.get("index"), -1) == config_index:
                return entry
        return None

    def _config_entry_is_standby_like(self, entry: dict[str, Any] | None) -> bool:
        if not isinstance(entry, dict):
            return False
        name = str(entry.get("name", "") or "").strip().lower()
        return bool(name) and "standby" in name

    def _operating_config_ready(self) -> tuple[bool, str, int]:
        config_index = self._selected_operating_config_index()
        if config_index < 0:
            return False, "select an AMX config first", config_index

        entry = self._config_entry_by_index(config_index)
        if entry is not None:
            if not _coerce_bool(entry.get("valid"), True):
                return (
                    False,
                    f"config {config_index} is marked invalid on the controller",
                    config_index,
                )
            if not _coerce_bool(entry.get("active"), True):
                return (
                    False,
                    f"config {config_index} is inactive on the controller",
                    config_index,
                )
        elif self.available_configs:
            return (
                False,
                f"config {config_index} is not reported by the controller",
                config_index,
            )

        return True, "", config_index

    # ---- last timing: what the operator last set on a config comes back at the next ON
    def _timing_memory_file(self) -> "Path | None":
        try:
            folder = self.controllerParent.pluginManager.Settings.configPath
            name = self.controllerParent.name
        except AttributeError:
            return None
        return Path(folder) / f"{name}_last_timing.json" if folder else None

    def _load_timing_memory(self, config_index: int) -> "dict[str, Any] | None":
        """The timing last applied on this config through this plugin and port, else None."""
        path = self._timing_memory_file()
        if path is None or config_index < 0 or not path.is_file():
            return None
        try:
            import json
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if (not isinstance(data, dict) or data.get("com") != str(getattr(self.controllerParent, "com", ""))
                or _coerce_int(data.get("config"), -1) != config_index or not isinstance(data.get("pulsers"), dict)):
            return None  # Another AMX on this plugin, or another config: nothing to bring back.
        frequency = _coerce_float(data.get("frequency_khz"), np.nan)
        if not np.isfinite(frequency) or frequency <= 0:
            return None
        pulsers: dict[int, dict[str, Any]] = {}
        for key, entry in data["pulsers"].items():
            pulser = _coerce_int(key, -1)
            width = _coerce_float(entry.get("width_us"), np.nan) if isinstance(entry, dict) else np.nan
            if pulser < 0 or not np.isfinite(width) or width < 0:
                return None
            origin = entry.get("width_origin")
            if not (isinstance(origin, list) and len(origin) == 2
                    and all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in origin)):
                origin = None
            pulsers[pulser] = dict(width_us=width, enabled=bool(entry.get("enabled")), width_origin=origin)
        return dict(frequency_khz=frequency, pulsers=pulsers, time=_coerce_float(data.get("time"), 0.0))

    def _remember_timing(self, frequency_khz: float, pulsers: "dict[str, dict[str, Any]]") -> None:
        """Record the timing now applied on the loaded config. Never raises."""
        path = self._timing_memory_file()
        config = _coerce_int(getattr(self, "_loaded_config_index", -1), -1)
        if path is None or config < 0:
            return
        record = {"com": str(getattr(self.controllerParent, "com", "")), "config": config,
                  "frequency_khz": float(frequency_khz), "pulsers": pulsers}
        if record == getattr(self, "_timing_memory_written", None):
            return
        try:
            import json
            import os
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps({**record, "time": time.time()}), encoding="utf-8")
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError):
            return
        self._timing_memory_written = record

    def _remember_gui_timing(self) -> None:
        """After a frequency or width was written: the operator's timing, with the origin of each width."""
        parent = self.controllerParent
        frequency = _coerce_float(getattr(parent, "frequency_khz", np.nan), np.nan)
        if not np.isfinite(frequency) or frequency <= 0:
            return
        clock_hz, osc_offset, _, _ = parent._timing_constants()
        period = _period_ticks(frequency, clock_hz=clock_hz, oscillator_offset=osc_offset)
        pulsers = {}
        for channel in parent.getChannels():
            if not channel.real:
                continue
            width = max(_coerce_float(getattr(channel, "value", 0.0), 0.0), 0.0)
            anchor = getattr(channel, "_duty_anchor", None)
            valid = anchor is not None and tuple(anchor[2:]) == (round(width * clock_hz / 1e6), period)
            pulsers[str(channel.pulser_number())] = {
                "width_us": width, "enabled": bool(channel.enabled),
                "width_origin": [int(anchor[0]), int(anchor[1])] if valid else None}
        self._remember_timing(frequency, pulsers)

    def _read_timing_snapshot(self, device: Any) -> dict[str, Any]:
        snapshot = self._device_call(device, "collect_housekeeping", timeout_s=self._startup_snapshot_timeout_s())
        refresh = getattr(self, "_refresh_hd_routing", None)
        if callable(refresh):
            self.hd_routing = None
            refresh(device, snapshot)
        self._apply_snapshot(snapshot)
        return snapshot

    def _restore_last_timing(
        self, device: Any, snapshot: dict[str, Any], config_index: int, timeout_s: float,
    ) -> "tuple[dict[str, Any], dict[str, Any] | None]":
        """ON: after the config is loaded, write back the frequency and widths last applied on it.

        User, 2026-11-14: a frequency or width set by hand was lost at every start, the config's own
        came back. Called with the controller lock held. Load now does not call it: it returns to
        the config as saved. A restore that fails reloads the config.
        """
        memory = self._load_timing_memory(config_index)
        block = getattr(self, "timing_block_reason", None)
        if memory is None or (callable(block) and block()):
            return snapshot, None
        clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
        osc_offset = _coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2)
        width_offset = _coerce_int(_safe_device_attr(device, "PULSER_WIDTH_OFFSET", 2), 2)
        frequency_khz = memory["frequency_khz"]
        period = _expected_oscillator_period(frequency_khz, clock_hz=clock_hz, oscillator_offset=osc_offset)

        def registers(reading: dict[str, Any]) -> "tuple[int, dict[int, int]]":
            return (_coerce_int(reading.get("oscillator", {}).get("period"), -1),
                    {_coerce_int(p.get("pulser"), -1): _coerce_int(p.get("width_ticks"), -1)
                     for p in reading.get("pulsers", [])})

        loaded_period, loaded = registers(snapshot)
        real = {ch.pulser_number() for ch in self.controllerParent.getChannels() if ch.real}
        if period is None or set(memory["pulsers"]) != real or not real <= set(loaded):
            return snapshot, None  # Recorded for another channel layout.
        targets = {pulser: _expected_width_register(entry["enabled"], entry["width_us"],
                                                    clock_hz=clock_hz, width_offset=width_offset)
                   for pulser, entry in memory["pulsers"].items()}
        if loaded_period == period and all(loaded[pulser] == ticks for pulser, ticks in targets.items()):
            return snapshot, None  # The config holds it already.
        self._delay_anchors.clear()  # The delays of the config just loaded are the origin.
        try:
            shorter, delays = self._delays_kept_in_new_period(device, frequency_khz, timeout_s)

            def write_frequency() -> None:
                self._device_call(device, "set_frequency_khz", frequency_khz, timeout_s=timeout_s)

            def write_widths() -> None:
                for pulser, ticks in sorted(targets.items()):
                    self._device_call(device, "set_pulser_width_ticks", pulser, ticks, timeout_s=timeout_s)

            def write_delays() -> None:
                for pulser, delay in delays:
                    self._device_call(device, "set_pulser_delay_ticks", pulser, delay, timeout_s=timeout_s)

            # Delay + width stay inside the period at every step, as in applyGlobalSettings.
            for write in ((write_delays, write_widths, write_frequency) if shorter
                          else (write_frequency, write_widths, write_delays)):
                write()
            restored = self._read_timing_snapshot(device)
            read_period, read = registers(restored)
            if read_period != period or any(read.get(pulser) != ticks for pulser, ticks in targets.items()):
                raise RuntimeError("the registers do not hold it after the write")
        except Exception as exc:  # noqa: BLE001
            if self._retire_failed_native_worker() or self._native_cancel_event.is_set():
                raise
            self.print(f"Last AMX timing not restored on config {config_index}: {self._format_exception(exc)}. "
                       "The config is loaded again as saved.", flag=PRINT.WARNING)
            self._delay_anchors.clear()
            self._device_call(device, "load_config", config_index, timeout_s=timeout_s)
            self._wait_for_startup_ready_snapshot(config_index=config_index, settle_timeout_s=timeout_s)
            return self._read_timing_snapshot(device), None
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(memory["time"])) if memory["time"] else "earlier"
        widths = ", ".join(f"P{pulser} {entry['width_us']:g} µs" if entry["enabled"] else f"P{pulser} off"
                           for pulser, entry in sorted(memory["pulsers"].items()))
        self.print(f"Last timing set on config {config_index} ({when}) restored: oscillator {frequency_khz:g} kHz "
                   f"(the config holds {clock_hz / (loaded_period + osc_offset) / 1000:g} kHz); {widths}. "
                   "Load now returns to the config as saved.")
        return restored, memory

    def _startup_snapshot_timeout_s(self) -> float:
        return float(getattr(self.controllerParent, "poll_timeout_s", 5.0))

    def _startup_snapshot_summary(self, snapshot: dict[str, Any]) -> str:
        state = str(snapshot.get("main_state", {}).get("name", "Unknown") or "Unknown")
        device_enabled = "ON" if bool(snapshot.get("device_enabled", False)) else "OFF"
        return f"state={state}, device={device_enabled}"

    def _print_if_slow(
        self,
        message: str,
        started_s: float,
        *,
        threshold_s: float = _AMX_SLOW_OPERATION_LOG_THRESHOLD_S,
    ) -> None:
        elapsed_s = time.monotonic() - started_s
        if elapsed_s >= threshold_s:
            self.print(f"{message} took {elapsed_s:.1f} s.", flag=PRINT.WARNING)

    def _print_lock_wait_if_slow(
        self,
        timeout_message: str,
        started_s: float,
        *,
        log_timeout: bool,
    ) -> None:
        if not log_timeout:
            return
        elapsed_s = time.monotonic() - started_s
        if elapsed_s >= _AMX_SLOW_OPERATION_LOG_THRESHOLD_S:
            self.print(
                f"AMX controller lock acquired after {elapsed_s:.1f} s: "
                f"{timeout_message}",
                flag=PRINT.WARNING,
            )

    def _ensure_transport_connected(self, timeout_s: float) -> Any:
        """Ensure the AMX transport is connected before runtime commands."""
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")
        if getattr(device, "connected", False):
            return device

        connect = getattr(device, "connect", None)
        if not callable(connect):
            raise RuntimeError(
                "AMX communication is not connected and the driver does not expose "
                "connect()."
            )
        started_s = time.monotonic()
        self._device_call(device, "connect", timeout_s=timeout_s)
        self._print_if_slow("AMX transport reconnect", started_s)
        self._refresh_available_configs()
        self._refresh_loaded_config_status()
        return device

    def _start_operating_mode(
        self,
        *,
        config_index: int,
        timeout_s: float,
        lock_message: str,
        success_message: str | None = None,
        restart_acquisition: bool = False,
        lock_timeout_s: float | None = None,
        restore_timing: bool = False,
    ) -> None:
        """Load the selected config and enable AMX; ON (``restore_timing``) then brings back the last timing set on it."""
        if lock_timeout_s is None:
            lock_timeout_s = (
                float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
                + float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
            )
        with self._controller_lock_section(lock_message, timeout_s=lock_timeout_s):
            self._ensure_transport_connected(timeout_s)
            self._load_operating_config_and_enable_device(
                config_index=config_index,
                timeout_s=timeout_s,
                load_config_first=True,
                restore_timing=restore_timing,
            )
        if restart_acquisition:
            self._restart_acquisition_after_transition()
        if success_message:
            self.print(success_message)

    def loadOperatingConfigNowFromThread(self, parallel: bool = True) -> None:
        if parallel:
            Thread(
                target=self.loadOperatingConfigNow,
                name=f"{self.controllerParent.name} loadOperatingConfigThread",
                daemon=True,
            ).start()
            return
        self.loadOperatingConfigNow()

    def _delays_kept_in_new_period(
        self, device: Any, frequency_khz: float, timeout_s: float,
    ) -> tuple[bool, list[tuple[int, int]]]:
        """Delays of pulsers keeping %, rescaled from the period the hardware runs now."""
        clock_hz = _coerce_float(_safe_device_attr(device, "CLOCK", 100e6), 100e6)
        delay_offset = _coerce_int(_safe_device_attr(device, "PULSER_DELAY_OFFSET", 3), 3)
        new_period = _period_ticks(frequency_khz, clock_hz=clock_hz,
                                   oscillator_offset=_coerce_int(_safe_device_attr(device, "OSC_OFFSET", 2), 2))
        old_khz = _coerce_float(self._device_call(device, "get_frequency_khz", timeout_s=timeout_s), np.nan)
        if not new_period or not np.isfinite(old_khz) or old_khz <= 0.0:
            return False, []
        old_period = round(clock_hz / (old_khz * 1000.0))
        if old_period == new_period:
            return False, []
        delays = []
        for channel in self.controllerParent.getChannels():
            pulser = channel.pulser_number()
            if not channel.real or not _keeps_duty(channel) or not _pulser_follows_oscillator(self, pulser):
                continue
            delay = _coerce_int(self._device_call(device, "get_pulser_delay_ticks", pulser, timeout_s=timeout_s), 0)
            if delay <= 0:
                continue  # 0 stops the pulser.
            kept, self._delay_anchors[pulser] = _rescale_ticks(
                self._delay_anchors.get(pulser), delay + delay_offset, old_period, new_period,
                minimum=delay_offset + 1, maximum=max(new_period, delay_offset + 1))
            if kept - delay_offset != delay:
                delays.append((pulser, kept - delay_offset))
        return new_period < old_period, delays

    def applyGlobalSettingsFromThread(self, parallel: bool = True) -> None:
        def start() -> None:
            finish = getattr(self.controllerParent, "_finish_setpoint_edits", None)
            if callable(finish):
                finish()
            if parallel:
                self._queue_global_settings_apply()
            else:
                self.applyGlobalSettings()
        _invoke_gui_callback(start)

    def _queue_global_settings_apply(self) -> None:
        with self._global_apply_state_lock:
            self._global_apply_pending = True
            if self._global_apply_worker_running:
                return
            self._global_apply_worker_running = True
            Thread(
                target=self._global_settings_apply_worker,
                name=f"{self.controllerParent.name} applyGlobalSettingsThread",
                daemon=True,
            ).start()

    def _global_settings_apply_worker(self) -> None:
        while True:
            with self._global_apply_state_lock:
                if not self._global_apply_pending:
                    self._global_apply_worker_running = False
                    return
                self._global_apply_pending = False
            self.applyGlobalSettings()

    def _channel_timing_apply_worker(self) -> None:
        while True:
            with self._channel_apply_state_lock:
                pending = dict(self._channel_apply_pending)
                self._channel_apply_pending.clear()
                if not pending:
                    self._channel_apply_worker_running = False
                    return
            for _pulser, channel in sorted(pending.items()):
                self.applyValue(channel)

    def _discard_pending_runtime_applies(self) -> None:
        self._pending_config_snapshot = None
        with self._global_apply_state_lock:
            self._global_apply_pending = False
        with self._channel_apply_state_lock:
            self._channel_apply_pending.clear()

    def _resume_after_load(self, recording: bool) -> None:
        """A load stops polling and recording: both resume, after a failed load too, unless OFF came."""
        if self.device is None or not getattr(self, "initialized", False) or not self.controllerParent.isOn():
            return
        if not self.acquiring:
            self._restart_acquisition_after_transition()  # 2026-11-02 lab: 30 s frozen after a failed load.
        if recording:
            _invoke_gui_callback(lambda: self.controllerParent.toggleRecording(on=True, manual=False))

    def _stop_acquisition_for_transition(self) -> None:
        """Stop acquisition before an ON/OFF transition without spurious lock errors.

        The base ``stopAcquisition`` uses a 1s lock timeout, but ``runAcquisition``
        holds the controller lock for ``collect_housekeeping`` (~1-2 s).  The
        timeout fires, logs ``Could not acquire lock to stop acquisition``, yet
        still sets ``acquiring = False``.  During a transition the shutdown
        sequence reacquires the lock with a longer timeout, so the spurious error
        is misleading.  Here we stop recording and set ``acquiring = False``
        directly; the acquisition thread checks this flag at the top of each
        iteration and the lock will be reacquired properly by the transition.
        """
        get_device = getattr(self, "getDevice", None)
        if callable(get_device):
            with contextlib.suppress(Exception):
                device = get_device()
                if getattr(device, "recording", False):
                    device.recording = False
        self.acquiring = False

    def startAcquisition(self) -> None:
        """Explorer's start, one caller at a time.

        Explorer checks for a running loop, then starts one, without a lock. On 2026-11-11 the ESI's ON
        thread and Explorer's recording start ran it at once and two loops polled the device (lock
        timeouts, five times more lost replies). This device also restarts its loop from a worker thread.
        """
        with self.__dict__.setdefault("_acquisition_start_lock", Lock()):
            super().startAcquisition()

    def _superseded_loop(self) -> bool:
        """A newer acquisition loop runs: this one ends, so the device is polled by one loop only."""
        owner = getattr(self, "acquisitionThread", None)
        return isinstance(owner, Thread) and owner is not current_thread() and owner.is_alive()

    def runAcquisition(self) -> None:
        """Poll AMX housekeeping and push readbacks to the GUI from the acquisition thread.

        The framework runs this loop in ``acquisitionThread`` and expects
        ``readNumbers`` to perform the hardware read. The controller lock is not
        reentrant, so we acquire it once here and forward ``already_acquired=True``;
        without that, every poll silently aborted on the held lock and left the duty
        monitors and the status badge frozen at their startup snapshot.
        """
        while self.acquiring and not self._superseded_loop():
            started = time.monotonic()
            try:
                with self._controller_lock_section(
                    "Could not acquire lock to acquire AMX data.",
                    timeout_s=1.0,
                    log_timeout=False,
                ):
                    self.readNumbers(already_acquired=True)
                    self.signalComm.updateValuesSignal.emit()
            except TimeoutError:
                # The controller lock is held by a hardware write (e.g. a
                # frequency change re-applying all pulsers); reads are skipped
                # until it completes. Surface this occasionally so the operator
                # understands why the duty monitors are paused.
                self._acquisition_lock_timeouts = (
                    getattr(self, "_acquisition_lock_timeouts", 0) + 1
                )
                if self._acquisition_lock_timeouts % 10 == 1:
                    self.print(
                        "AMX monitors paused: controller lock busy (a hardware "
                        "write is in progress). Readings resume when it completes.",
                        flag=PRINT.WARNING,
                    )
            _wait_for_next_poll(self, started)

    def _poll_error(self, message: str, *, exc: Exception | None) -> None:
        self.errorCount += 1
        self._consecutive_poll_errors += 1
        self.print(message, flag=PRINT.ERROR)
        self.initializeValues(reset=True)
        if self._retire_failed_native_worker():
            return
        if self._consecutive_poll_errors >= _AMX_MAX_CONSECUTIVE_POLL_ERRORS:
            self.print(
                f"Too many consecutive AMX polling errors ({self._consecutive_poll_errors}). "
                "Closing communication.",
                flag=PRINT.ERROR,
            )
            # Stop the acquisition loop immediately and route the close through
            # the GUI-thread signal so it does not run synchronously on the
            # acquisition thread (which can re-enter and freeze the monitors).
            self.acquiring = False
            close_signal = getattr(
                getattr(self, "signalComm", None), "closeCommunicationSignal", None
            )
            emit = getattr(close_signal, "emit", None)
            if callable(emit):
                emit()
            else:
                self.closeCommunication()

    def updateValues(self) -> None:
        if self.values is None:
            return

        self._sync_status_to_gui()
        device_is_on = getattr(self.controllerParent, "isOn", lambda: False)()
        freq_khz = float(getattr(self.controllerParent, "frequency_khz", 2.0))

        for channel in self.controllerParent.getChannels():
            pulser = channel.pulser_number()
            if channel.real and device_is_on:
                channel.monitor = self.values.get(pulser, np.nan)
                channel.setWidthText(self.width_values.get(pulser, "n/a"))
                channel.setDelayText(self.delay_values.get(pulser, "n/a"))
                channel.setBurstText(self.burst_values.get(pulser, "n/a"))
                # Software-calculated duty from user width setting.
                channel.setDutyText(
                    f"{_requested_duty_percent(self.controllerParent, getattr(channel, 'value', 0.0)):.1f}")
                channel.setFreqText(f"{freq_khz:.1f}")
            else:
                channel.monitor = np.nan
                channel.setWidthText("n/a")
                channel.setDelayText("n/a")
                channel.setBurstText("n/a")
                channel.setDutyText("n/a")
                channel.setFreqText("n/a")

    def _update_state(self) -> None:
        if self.main_state == _AMX_SHUTDOWN_UNCONFIRMED_STATE:
            return  # Only a new explicit, confirmed OFF may clear this diagnosis.
        device = self.device
        if device is None:
            return  # Preserve the last shutdown/transport-loss diagnosis.

        try:
            with self._controller_lock_section(
                "Could not acquire lock to refresh the AMX state."
            ):
                device = self.device
                if device is None:
                    return
                snapshot = self._device_call(device, "collect_housekeeping",
                    timeout_s=float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
                )
                self._apply_snapshot(snapshot)
        except Exception:
            self._retire_failed_native_worker()  # A lost worker is retired before the error goes up.
            raise

    def _sync_status_to_gui(self) -> None:
        # ESIBD setting attributes are widget-backed properties, not plain data.
        def _refresh_gui() -> None:
            self.controllerParent.main_state = self.main_state
            self.controllerParent.device_enabled_state = self.device_enabled_state
            self.controllerParent.available_configs = list(self.available_configs)
            self.controllerParent.available_configs_text = self.available_configs_text
            self.controllerParent.loaded_config_text = self.loaded_config_text
            sync_acquisition_controls = getattr(
                self.controllerParent,
                "_sync_acquisition_controls",
                None,
            )
            if callable(sync_acquisition_controls):
                sync_acquisition_controls()
            update_config_controls = getattr(self.controllerParent, "_update_config_controls", None)
            if callable(update_config_controls):
                update_config_controls()
            update_status_widgets = getattr(self.controllerParent, "_update_status_widgets", None)
            if callable(update_status_widgets):
                update_status_widgets()

        _invoke_gui_callback(_refresh_gui)

    def _transition_guard(self) -> Lock:
        lock = getattr(self, "_transition_lock", None)
        if lock is None:
            lock = Lock()
            self._transition_lock = lock
        return lock

    def _initial_open_incomplete(self) -> bool:
        return self.device is not None and any(bool(getattr(self.device, name, False)) for name in (
            "_open_failed", "_opening_in_progress", "_failed_open_released",
        ))

    @staticmethod
    def _native_backend(device):
        """Inspect local fields without queuing a lifecycle RPC."""
        return _native_backend(device)

    def _cancel_native_operation(self) -> None:
        if self.device is not None:
            self._native_cancel_event.set()

    def _restore_off_ui_state(self) -> None:
        self.resume_session = False
        token = self._native_cancel_event
        def _update_gui() -> None:
            if token is not self._native_cancel_event:
                return
            sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_on_state):
                sync_on_state(False)
                return
            if hasattr(self.controllerParent, "onAction"):
                self.controllerParent.onAction.state = False

        _invoke_gui_callback(_update_gui)

    def _restore_on_ui_state(self) -> None:
        token = self._native_cancel_event
        def _update_gui() -> None:
            if token is not self._native_cancel_event:
                return
            state = not self._native_transport_lost
            sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_on_state):
                sync_on_state(state)
                return
            if hasattr(self.controllerParent, "onAction"):
                self.controllerParent.onAction.state = state

        _invoke_gui_callback(_update_gui)

    @contextlib.contextmanager
    def _controller_lock_section(
        self,
        timeout_message: str,
        *,
        already_acquired: bool = False,
        timeout_s: float = 1.0,
        log_timeout: bool = True,
    ):
        lock = getattr(self, "lock", None)
        acquire = getattr(lock, "acquire", None)
        release = getattr(lock, "release", None)
        if callable(acquire) and callable(release):
            if already_acquired:
                yield
                return
            started_s = time.monotonic()
            if not acquire(timeout=float(timeout_s)):
                if log_timeout:
                    self.print(timeout_message, flag=PRINT.ERROR)
                raise TimeoutError(timeout_message)
            self._print_lock_wait_if_slow(
                timeout_message,
                started_s,
                log_timeout=log_timeout,
            )
            try:
                yield
            finally:
                release()
            return

        acquire_timeout = getattr(lock, "acquire_timeout", None)
        if callable(acquire_timeout):
            started_s = time.monotonic()
            with acquire_timeout(
                float(timeout_s),
                timeoutMessage=timeout_message if log_timeout else "",
                already_acquired=already_acquired,
            ) as lock_acquired:
                if not lock_acquired:
                    if log_timeout:
                        self.print(timeout_message, flag=PRINT.ERROR)
                    raise TimeoutError(timeout_message)
                self._print_lock_wait_if_slow(
                    timeout_message,
                    started_s,
                    log_timeout=log_timeout,
                )
                yield
            return

        raise TypeError(
            "AMX controller lock must provide either acquire_timeout() or acquire()/release()."
        )

    def _end_transition(self) -> None:
        with self._transition_guard():
            self.transitioning = False
            self.transition_target_on = None

    def _format_exception(self, exc: Exception) -> str:
        return str(exc).strip()
