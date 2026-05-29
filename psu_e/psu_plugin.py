"""Drive PSU outputs from ESIBD Explorer and monitor live readbacks."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import math
import sys
import time
from pathlib import Path
from threading import Lock, Thread
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
from esibd.plugins import Device, Plugin

_BUNDLED_RUNTIME_DIRNAME = "runtime"
_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_psu_runtime"
_PSU_DRIVER_CLASS: type[Any] | None = None
_CHANNEL_NAME_KEY = getattr(Parameter, "NAME", getattr(Channel, "NAME", "Name"))
_CHANNEL_ENABLED_KEY = getattr(Channel, "ENABLED", "Enabled")
_CHANNEL_REAL_KEY = getattr(Channel, "REAL", "Real")
_PARAMETER_MIN_KEY = getattr(Parameter, "MIN", "Min")
_PARAMETER_MAX_KEY = getattr(Parameter, "MAX", "Max")
_PARAMETER_ADVANCED_KEY = getattr(Parameter, "ADVANCED", "Advanced")
_PARAMETER_TOOLTIP_KEY = getattr(Parameter, "TOOLTIP", "Tooltip")
_PARAMETER_EVENT_KEY = getattr(Parameter, "EVENT", "Event")
_PSU_CHANNEL_KEY = "CH"
_PSU_CHANNEL_IDS = (0, 1)
_PSU_POWER_ON_ICON = "switch-medium_on.png"
_PSU_POWER_OFF_ICON = "switch-medium_off.png"
_PSU_CHANNEL_ON_LABEL = "HV ON"
_PSU_CHANNEL_OFF_LABEL = "HV OFF"
_PSU_MIN_ROW_HEIGHT = 28
_PSU_TABLE_SCALING = "normal"
_PSU_LEGACY_OVERSIZED_SCALINGS = {"large", "larger", "huge"}
_PSU_NEUTRAL_WIDGET_STYLE = "background: transparent;"
_PSU_OUTPUT_ON_STYLE = (
    "background-color: #1f2933; color: #ffffff; margin:0px; padding:0px 6px;"
)
_PSU_OUTPUT_OFF_STYLE = (
    "background-color: #4a5568; color: #ffffff; margin:0px; padding:0px 6px;"
)
_PSU_PANEL_CARD_ON_STYLE = (
    "QFrame {"
    " background-color: #162433;"
    " border: 1px solid #3182ce;"
    " border-radius: 8px;"
    " color: #f7fafc;"
    "}"
)
_PSU_PANEL_CARD_OFF_STYLE = (
    "QFrame {"
    " background-color: #202938;"
    " border: 1px solid #64748b;"
    " border-radius: 8px;"
    " color: #f7fafc;"
    "}"
)
_PSU_PANEL_CARD_DISCONNECTED_STYLE = (
    "QFrame {"
    " background-color: #151b26;"
    " border: 1px solid #475569;"
    " border-radius: 8px;"
    " color: #e2e8f0;"
    "}"
)
_PSU_PANEL_TITLE_STYLE = "color: #f8fafc; font-weight: 700; font-size: 14px;"
_PSU_PANEL_METRIC_NAME_STYLE = "color: #cbd5e1; font-weight: 600;"
_PSU_PANEL_METRIC_VALUE_STYLE = "color: #f8fafc; font-weight: 600;"
_PSU_PANEL_DIAGNOSTICS_STYLE = (
    "QFrame {"
    " background-color: #111827;"
    " border: 1px solid #334155;"
    " border-radius: 8px;"
    " color: #e2e8f0;"
    "}"
)
_PSU_PANEL_DIAGNOSTICS_TITLE_STYLE = "color: #cbd5e1; font-weight: 700;"
_PSU_PANEL_DIAGNOSTICS_TEXT_STYLE = "color: #e2e8f0;"
_PSU_PANEL_SECTION_HEADER_STYLE = "color: #cbd5e1; font-weight: 700; font-size: 13px;"
_PSU_PANEL_CARD_MIN_WIDTH = 220
_PSU_PANEL_CARD_MAX_WIDTH = 280
_PSU_PANEL_DIAGNOSTICS_MAX_WIDTH = 400
_PSU_PANEL_OPERATOR_MAX_WIDTH = 900
_PSU_LIVE_READBACK_REFRESH_PERIOD_S = 0.0
_PSU_HOUSEKEEPING_REFRESH_PERIOD_S = 2.0
_PSU_MANUAL_NUMERIC_DEBOUNCE_MS = 250
_PSU_FEEDBACK_OK_STYLE = "background-color: #2f855a; color: #ffffff; margin:0px; padding:0px 4px;"
_PSU_FEEDBACK_WARN_STYLE = "background-color: #dd6b20; color: #ffffff; margin:0px; padding:0px 4px;"
_PSU_FEEDBACK_ERROR_STYLE = "background-color: #c53030; color: #ffffff; margin:0px; padding:0px 4px;"
_PSU_FEEDBACK_NEUTRAL_STYLE = (
    "background-color: #334155; color: #e2e8f0; margin:0px; padding:0px 4px;"
)
_PSU_MANUAL_SPINBOX_STYLE = (
    "QDoubleSpinBox, QSpinBox {"
    " background-color: #0f172a;"
    " color: #f8fafc;"
    " border: 1px solid #64748b;"
    " border-radius: 4px;"
    " padding: 2px 20px 2px 6px;"
    " selection-background-color: #2563eb;"
    "}"
    "QDoubleSpinBox:disabled, QSpinBox:disabled {"
    " background-color: #1f2937;"
    " color: #94a3b8;"
    " border-color: #475569;"
    "}"
    "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button,"
    " QSpinBox::up-button, QSpinBox::down-button {"
    " background-color: #475569;"
    " border: 1px solid #94a3b8;"
    " width: 18px;"
    "}"
)
_PSU_RANGE_BADGE_STYLE = (
    "background-color: #334155; color: #e2e8f0; margin:0px; padding:0px 6px;"
)
_PSU_OPERATOR_STATUS_STYLE = "color: #e2e8f0; font-weight: 600;"
_PSU_OPERATOR_NOTE_STYLE = "color: #94a3b8;"
_PSU_OPERATOR_HEADER_STYLE = "color: #cbd5e1; font-weight: 700;"
_PSU_VOLTAGE_OK_ABS_TOLERANCE_V = 0.25
_PSU_VOLTAGE_WARN_ABS_TOLERANCE_V = 1.0
_PSU_VOLTAGE_OK_RELATIVE_TOLERANCE = 0.02
_PSU_VOLTAGE_WARN_RELATIVE_TOLERANCE = 0.05
_PSU_CURRENT_LIMIT_WARN_RATIO = 0.95
_PSU_CURRENT_LIMIT_ERROR_RATIO = 1.0
_PSU_CURRENT_ZERO_OK_ABS_TOLERANCE_A = 0.001
_PSU_CURRENT_ZERO_WARN_ABS_TOLERANCE_A = 0.01
_PSU_DROPOUT_WARN_V = 10.0
_PSU_DROPOUT_ERROR_V = 5.0
_PSU_TEMPERATURE_WARN_C = 55.0
_PSU_TEMPERATURE_ERROR_C = 70.0
_PSU_SETPOINT_VERIFY_ABS_TOLERANCE_V = 0.01
_PSU_SETPOINT_VERIFY_ABS_TOLERANCE_A = 0.001
_PSU_SETPOINT_VERIFY_REL_TOLERANCE = 0.01
_PSU_FLOAT_SENTINEL = -1
_PSU_SHUTDOWN_UNCONFIRMED_STATE = "Shutdown unconfirmed"
_PSU_MAIN_STATE_ALIASES = {
    "state_on": "ST_ON",
    "state_error": "ST_ERROR",
    "state_err_vsup": "ST_ERR_VSUP",
    "state_err_temp_low": "ST_ERR_TEMP_LOW",
    "state_err_temp_high": "ST_ERR_TEMP_HIGH",
    "state_err_ilock": "ST_ERR_ILOCK",
    "state_err_psu_dis": "ST_ERR_PSU_DIS",
    "st_on": "ST_ON",
    "st_error": "ST_ERROR",
    "st_err_vsup": "ST_ERR_VSUP",
    "st_err_temp_low": "ST_ERR_TEMP_LOW",
    "st_err_temp_high": "ST_ERR_TEMP_HIGH",
    "st_err_ilock": "ST_ERR_ILOCK",
    "st_err_psu_dis": "ST_ERR_PSU_DIS",
    "st_stby": "ST_STBY",
}


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
    harmonized = _PSU_MAIN_STATE_ALIASES.get(normalized)
    if harmonized is not None:
        return harmonized
    return text


def _harmonize_psu_main_state(
    state: Any,
    *,
    device_enabled: Any = None,
    output_enabled: Any = None,
) -> str:
    """Map raw PSU backend states onto the UI convention used by other plugins."""
    harmonized = _normalize_runtime_state(state)
    if harmonized != "ST_ERR_PSU_DIS":
        return harmonized

    device_is_enabled = _coerce_bool(device_enabled, default=True)
    output_bits = tuple(bool(value) for value in output_enabled or ())
    outputs_disabled = bool(output_bits) and not any(output_bits)
    if (device_enabled is False or not device_is_enabled) and outputs_disabled:
        return "ST_STBY"
    return harmonized


def _status_requires_operator_attention(state: Any) -> bool:
    """Return True when the raw state describes a fault or uncertain condition."""
    normalized = str(state or "").strip().lower()
    return any(
        token in normalized
        for token in (
            "err",
            "error",
            "fail",
            "fault",
            "lost",
            "overload",
            "timeout",
            "unknown",
            "unconfirmed",
        )
    )


def _invoke_gui_callback(callback: Any) -> None:
    """Run GUI updates directly in tests and queue them on the Qt GUI thread."""
    if not callable(callback):
        return
    try:
        from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        callback()
        return

    app = QApplication.instance()
    if app is None:
        callback()
        return

    try:
        if QThread.currentThread() == app.thread():
            callback()
            return

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

                def _run(self, queued_callback: Any) -> None:
                    if callable(queued_callback):
                        queued_callback()

            dispatcher = _CallbackDispatcher()
            dispatcher.moveToThread(app.thread())
            setattr(_invoke_gui_callback, "_dispatcher", dispatcher)
        dispatcher.callbackRequested.emit(callback)
    except Exception:
        callback()


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


def _setpoint_matches(
    actual: Any,
    expected: Any,
    *,
    abs_tolerance: float,
    rel_tolerance: float = _PSU_SETPOINT_VERIFY_REL_TOLERANCE,
) -> bool:
    actual_value = _coerce_float(actual, np.nan)
    expected_value = _coerce_float(expected, np.nan)
    if _is_nan(actual_value) or _is_nan(expected_value):
        return False
    return math.isclose(
        actual_value,
        expected_value,
        rel_tol=float(rel_tolerance),
        abs_tol=float(abs_tolerance),
    )


def _action_label(action: Any) -> str:
    """Extract a stable label from QAction-like objects and test doubles."""
    for attr_name in ("toolTip", "text", "objectName"):
        attr = getattr(action, attr_name, None)
        value = attr() if callable(attr) else attr
        if isinstance(value, str) and value:
            return value
    return ""


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
    """Return one human-readable PSU config entry."""
    if index < 0:
        return "Skip (-1)"

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


def _psu_output_state_badge_style(state: Any) -> str:
    normalized = str(state or "").strip().upper()
    if normalized == "ON":
        return _PSU_OUTPUT_ON_STYLE
    if normalized == "OFF":
        return _PSU_OUTPUT_OFF_STYLE
    return _PSU_NEUTRAL_WIDGET_STYLE


def _psu_panel_card_style(*, connected: bool, output_state: Any) -> str:
    if not connected:
        return _PSU_PANEL_CARD_DISCONNECTED_STYLE
    if str(output_state or "").strip().upper() == "ON":
        return _PSU_PANEL_CARD_ON_STYLE
    return _PSU_PANEL_CARD_OFF_STYLE


def _psu_feedback_style(state: str) -> str:
    if state == "ok":
        return _PSU_FEEDBACK_OK_STYLE
    if state == "warn":
        return _PSU_FEEDBACK_WARN_STYLE
    if state == "error":
        return _PSU_FEEDBACK_ERROR_STYLE
    return _PSU_FEEDBACK_NEUTRAL_STYLE


def _format_current_text(current_a: Any) -> str:
    value = _coerce_float(current_a, np.nan)
    if _is_nan(value):
        return "n/a"
    return f"{value:.6g} A"


def _format_voltage_text(voltage_v: Any) -> str:
    value = _coerce_float(voltage_v, np.nan)
    if _is_nan(value):
        return "n/a"
    return f"{value:.6g} V"


def _format_temperature_text(temp_c: Any) -> str:
    value = _coerce_float(temp_c, np.nan)
    if _is_nan(value):
        return "n/a"
    return f"{value:.6g} C"


def _format_full_range_text(*, enabled: Any, supported: Any = True) -> str:
    if not _coerce_bool(supported, default=True):
        return "n/a"
    return "Full" if _coerce_bool(enabled, default=False) else "Half"


def _temperature_feedback_state(temp_c: Any) -> str:
    value = _coerce_float(temp_c, np.nan)
    if _is_nan(value):
        return "default"
    if value > _PSU_TEMPERATURE_ERROR_C:
        return "error"
    if value > _PSU_TEMPERATURE_WARN_C:
        return "warn"
    return "ok"


def _dropout_feedback_state(dropout_v: Any) -> str:
    value = _coerce_float(dropout_v, np.nan)
    if _is_nan(value):
        return "default"
    if value < _PSU_DROPOUT_ERROR_V:
        return "error"
    if value < _PSU_DROPOUT_WARN_V:
        return "warn"
    return "ok"


def _voltage_feedback_state(
    *,
    enabled: Any,
    measured_v: Any,
    set_v: Any,
) -> str:
    measured = _coerce_float(measured_v, np.nan)
    target = (
        _coerce_float(set_v, np.nan)
        if _coerce_bool(enabled, default=False)
        else 0.0
    )
    if _is_nan(measured) or _is_nan(target):
        return "default"
    reference = max(abs(target), 1.0)
    absolute_error = abs(measured - target)
    relative_error = absolute_error / reference
    if (
        absolute_error <= _PSU_VOLTAGE_OK_ABS_TOLERANCE_V
        or relative_error <= _PSU_VOLTAGE_OK_RELATIVE_TOLERANCE
    ):
        return "ok"
    if (
        absolute_error <= _PSU_VOLTAGE_WARN_ABS_TOLERANCE_V
        or relative_error <= _PSU_VOLTAGE_WARN_RELATIVE_TOLERANCE
    ):
        return "warn"
    return "error"


def _current_limit_feedback_state(
    *,
    enabled: Any,
    measured_a: Any,
    limit_a: Any,
    current_limit_active: Any = False,
) -> str:
    enabled_bool = _coerce_bool(enabled, default=False)
    if enabled_bool and _coerce_bool(current_limit_active, default=False):
        return "warn"
    measured = _coerce_float(measured_a, np.nan)
    if _is_nan(measured):
        return "default"
    if not enabled_bool:
        absolute_current = abs(measured)
        if absolute_current <= _PSU_CURRENT_ZERO_OK_ABS_TOLERANCE_A:
            return "ok"
        if absolute_current <= _PSU_CURRENT_ZERO_WARN_ABS_TOLERANCE_A:
            return "warn"
        return "error"
    limit = _coerce_float(limit_a, np.nan)
    if _is_nan(limit) or limit <= 0:
        return "default"
    ratio = measured / limit
    if ratio >= _PSU_CURRENT_LIMIT_ERROR_RATIO:
        return "error"
    if ratio >= _PSU_CURRENT_LIMIT_WARN_RATIO:
        return "warn"
    return "ok"


def _format_rail_summary(rails: Any) -> str:
    if not isinstance(rails, dict):
        return "n/a"
    parts: list[str] = []
    for label, key in (
        ("24Vp", "volt_24vp_v"),
        ("12Vp", "volt_12vp_v"),
        ("12Vn", "volt_12vn_v"),
        ("Ref", "volt_ref_v"),
    ):
        value_text = _format_voltage_text(rails.get(key))
        if value_text != "n/a":
            parts.append(f"{label} {value_text}")
    return ", ".join(parts) if parts else "n/a"


def _format_channel_diagnostics_summary(
    channel_index: int,
    *,
    temp_c: Any = None,
    dropout_v: Any = None,
    full_range_enabled: Any = None,
    full_range_supported: Any = True,
) -> str:
    parts: list[str] = []
    full_range_text = _format_full_range_text(
        enabled=full_range_enabled,
        supported=full_range_supported,
    )
    if full_range_text != "n/a":
        parts.append(full_range_text)
    temp_text = _format_temperature_text(temp_c)
    if temp_text != "n/a":
        parts.append(f"Tadc {temp_text}")
    dropout_text = _format_voltage_text(dropout_v)
    if dropout_text != "n/a":
        parts.append(f"Dropout {dropout_text}")
    if not parts:
        return f"CH{channel_index} n/a"
    return f"CH{channel_index} " + ", ".join(parts)


def _format_channel_runtime_summary(
    channel_index: int,
    *,
    enabled: Any = None,
    voltage_v: Any = None,
    current_a: Any = None,
) -> str:
    output_state = "ON" if _coerce_bool(enabled, default=False) else "OFF"
    voltage_text = _format_voltage_text(voltage_v)
    current_text = _format_current_text(current_a)
    if voltage_text == "n/a" and current_text == "n/a":
        return f"CH{channel_index} {output_state}"
    return f"CH{channel_index} {output_state} {voltage_text} / {current_text}"


def _format_channel_temperature_summary(channel_index: int, temp_c: Any) -> str:
    """Return a compact toolbar summary for one PSU ADC temperature."""
    return f"CH{channel_index} {_format_temperature_text(temp_c)}"


def _set_widget_visible(widget: Any, visible: bool) -> None:
    """Toggle visibility on Qt widgets and lightweight test doubles."""
    if widget is None:
        return
    set_visible = getattr(widget, "setVisible", None)
    if callable(set_visible):
        set_visible(bool(visible))
        return
    if visible:
        show = getattr(widget, "show", None)
        if callable(show):
            show()
            return
    hide = getattr(widget, "hide", None)
    if callable(hide):
        hide()
        return
    widget.visible = bool(visible)


def _channel_key_from_item(item: dict[str, Any]) -> int:
    return _coerce_int(item.get(_PSU_CHANNEL_KEY), 0)


def _generic_channel_name(device_name: str, channel_id: int) -> str:
    return f"{device_name}_CH{channel_id}"


def _build_generic_channel_item(
    device_name: str,
    channel_id: int,
    default_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = dict(default_item or {})
    item[_CHANNEL_NAME_KEY] = _generic_channel_name(device_name, channel_id)
    item[_PSU_CHANNEL_KEY] = str(channel_id)
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
            if key == _PSU_CHANNEL_KEY:
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
            f"Removed legacy PSU bootstrap channels: "
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
            _build_generic_channel_item(device_name, channel_id, default_item=default_item)
            for channel_id in _PSU_CHANNEL_IDS
        ], [("PSU bootstrap config replaced with fixed hardware channels.", None)]

    current_items, cleanup_logs = _strip_legacy_bootstrap_residue(
        current_items,
        device_name=device_name,
        default_item=default_item,
    )

    target_ids = set(_PSU_CHANNEL_IDS)
    kept_keys: set[int] = set()
    added_channels: list[int] = []
    removed_channels: list[int] = []
    duplicate_entries: list[tuple[str, int]] = []
    synced_items: list[dict[str, Any]] = []

    for item in current_items:
        synced_item = dict(item)
        channel_id = _channel_key_from_item(synced_item)
        if channel_id not in target_ids:
            removed_channels.append(channel_id)
            continue
        if channel_id in kept_keys:
            duplicate_entries.append(
                (str(synced_item.get(_CHANNEL_NAME_KEY, "")), channel_id)
            )
            continue

        kept_keys.add(channel_id)
        synced_item[_CHANNEL_REAL_KEY] = True
        synced_items.append(synced_item)

    for channel_id in _PSU_CHANNEL_IDS:
        if channel_id in kept_keys:
            continue
        synced_items.append(
            _build_generic_channel_item(
                device_name,
                channel_id,
                default_item=default_item,
            )
        )
        added_channels.append(channel_id)

    log_entries: list[tuple[str, PRINT | None]] = list(cleanup_logs)
    if added_channels:
        log_entries.append(
            (
                "Added generic PSU channels: "
                + ", ".join(f"CH{channel_id}" for channel_id in added_channels),
                None,
            )
        )
    if removed_channels:
        log_entries.append(
            (
                "Removed PSU channels not present on hardware: "
                + ", ".join(f"CH{channel_id}" for channel_id in removed_channels),
                None,
            )
        )
    for channel_name, channel_id in duplicate_entries:
        log_entries.append(
            (
                f"Removed duplicate PSU mapping for CH{channel_id}: {channel_name}",
                PRINT.WARNING,
            )
        )
    return synced_items, log_entries


def _bundled_runtime_module_name(plugin_dir: Path | None = None) -> str:
    resolved_plugin_dir = Path(__file__).resolve().parent if plugin_dir is None else plugin_dir
    plugin_key = resolved_plugin_dir.name.replace("-", "_")
    return f"{_BUNDLED_RUNTIME_NAMESPACE_PREFIX}_{plugin_key}"


def _load_private_runtime_package(module_name: str, package_dir: Path) -> None:
    if module_name in sys.modules:
        return

    init_file = package_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        module_name,
        init_file,
        submodule_search_locations=[str(package_dir)],
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError(
            f"Could not create an import spec for bundled PSU runtime at {package_dir}."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise


def _get_psu_driver_class() -> type[Any]:
    global _PSU_DRIVER_CLASS

    if _PSU_DRIVER_CLASS is not None:
        return _PSU_DRIVER_CLASS

    plugin_dir = Path(__file__).resolve().parent
    bundled_runtime_dir = plugin_dir / "vendor" / _BUNDLED_RUNTIME_DIRNAME
    bundled_runtime_init = bundled_runtime_dir / "__init__.py"
    if not bundled_runtime_init.exists():
        raise ModuleNotFoundError(
            "Bundled PSU runtime not found in vendor/runtime; "
            "plugin installation is incomplete."
        )

    runtime_module_name = _bundled_runtime_module_name(plugin_dir)
    _load_private_runtime_package(runtime_module_name, bundled_runtime_dir)
    module = importlib.import_module(f"{runtime_module_name}.psu")
    _PSU_DRIVER_CLASS = cast(type[Any], module.PSU)
    return _PSU_DRIVER_CLASS


def providePlugins() -> "list[type[Plugin]]":
    return [PSUDevice]


class PSUDevice(Device):
    """Drive the PSU through stored configs or manual setpoints and monitor readbacks."""

    documentation = (
        "Loads PSU configurations or applies manual setpoints and monitors live voltage/current readbacks."
    )

    name = "PSU_E"
    version = "0.1.0"
    supportedVersion = "1.0.1"
    pluginType = PLUGINTYPE.INPUTDEVICE
    unit = "V"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "psu.png"
    channels: "list[PSUChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    STARTUP_TIMEOUT = "Startup timeout (s)"
    POLL_TIMEOUT = "Poll timeout (s)"
    STANDBY_CONFIG = "Standby config"
    OPERATING_CONFIG = "Operating config"
    SHUTDOWN_CONFIG = "Shutdown config"
    STATE = "State"
    OUTPUTS = "Outputs"
    AVAILABLE_CONFIGS = "Available configs"
    INTERLOCK_MONITORING = "Interlock monitoring"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = PSUChannel
        self.maxDataPoints = 0
        self.maxStorage = 0
        self.interval = 1000

    def initGUI(self) -> None:
        super().initGUI()
        self.available_configs = []
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
        self.controller = PSUController(controllerParent=self)
        self._ensure_bootstrap_channels_present()
        self._hide_channel_table()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()
        self._update_channel_column_visibility()

    def finalizeInit(self) -> None:
        super().finalizeInit()
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_config_selectors()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()
        self._update_channel_column_visibility()
        self._sync_acquisition_controls()

    def getChannels(self) -> "list[PSUChannel]":
        return cast("list[PSUChannel]", super().getChannels())

    def _interlock_monitoring_changed(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None or not getattr(controller, "initialized", False):
            return
        controller._interlock_monitoring_changed()

    def estimateStorage(self) -> None:
        """Handle the no-channel bootstrap state used before PSU hardware sync."""
        channels = list(getattr(self, "channels", []) or [])
        if channels:
            base_estimate_storage = getattr(super(), "estimateStorage", None)
            if callable(base_estimate_storage):
                try:
                    base_estimate_storage()
                except (KeyError, AttributeError):
                    pass
            return

        self.maxDataPoints = 0
        plugin_manager = getattr(self, "pluginManager", None)
        settings_plugin = getattr(plugin_manager, "Settings", None)
        settings = getattr(settings_plugin, "settings", None)
        if not isinstance(settings, dict):
            return
        max_points_setting = settings.get(f"{self.name}/{self.MAXDATAPOINTS}")
        widget = (
            max_points_setting.getWidget()
            if max_points_setting is not None and hasattr(max_points_setting, "getWidget")
            else None
        )
        if widget is not None and hasattr(widget, "setToolTip"):
            widget.setToolTip(
                "Storage estimate unavailable until PSU channels are synchronized with hardware."
            )

    com: int
    baudrate: int
    connect_timeout_s: float
    startup_timeout_s: float
    poll_timeout_s: float
    standby_config: int
    operating_config: int
    shutdown_config: int
    main_state: str
    output_summary: str
    available_configs_text: str
    available_configs: list[dict[str, Any]]
    loaded_state_text: str
    interlock_monitoring: bool

    def _current_channel_items(self) -> list[dict[str, Any]]:
        return [channel.asDict() for channel in self.getChannels()]

    def _default_channel_item(self) -> dict[str, Any]:
        return self.channelType(channelParent=self, tree=None).asDict()

    def _default_channel_template(self) -> dict[str, dict[str, Any]]:
        return self.channelType(channelParent=self, tree=None).getSortedDefaultChannel()

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
            "shutdown_config": self.SHUTDOWN_CONFIG,
        }[attr_name]

    def _config_setting_value(self, attr_name: str) -> int:
        setting_name = self._config_setting_name(attr_name)
        setting = self._setting(setting_name)
        if setting is not None:
            return _coerce_int(getattr(setting, "value", None), getattr(self, attr_name, -1))
        return _coerce_int(getattr(self, attr_name, -1), -1)

    def _set_config_setting_value(self, attr_name: str, value: int) -> bool:
        value = _coerce_int(value, -1)
        setting_name = self._config_setting_name(attr_name)
        setting = self._setting(setting_name)
        if setting is not None:
            if _coerce_int(getattr(setting, "value", None), value) == value:
                return False
            setting.value = value
            return True
        if _coerce_int(getattr(self, attr_name, value), value) == value:
            return False
        setattr(self, attr_name, value)
        return True

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
        lines.append("Available PSU configs:")
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

    def _create_manual_numeric_widget(
        self,
        *,
        suffix: str,
        decimals: int,
        step: float,
        maximum: float,
    ) -> Any:
        from PyQt6.QtWidgets import QAbstractSpinBox, QDoubleSpinBox

        widget = QDoubleSpinBox()
        widget.setRange(0.0, float(maximum))
        widget.setDecimals(int(decimals))
        widget.setSingleStep(float(step))
        widget.setSuffix(f" {suffix}")
        widget.setMinimumWidth(110)
        widget.setAccelerated(True)
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        widget.setStyleSheet(_PSU_MANUAL_SPINBOX_STYLE)
        _disable_spinbox_wheel(widget)
        return widget

    def _create_manual_slot_widget(self) -> Any:
        from PyQt6.QtWidgets import QAbstractSpinBox, QSpinBox

        widget = QSpinBox()
        widget.setRange(0, 167)
        widget.setMinimumWidth(80)
        widget.setValue(max(self._config_setting_value("operating_config"), 0))
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        widget.setStyleSheet(_PSU_MANUAL_SPINBOX_STYLE)
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

    def _manual_controls_ready(self) -> tuple[bool, str]:
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
        return True, ""

    def _manual_state_from_panel(self) -> dict[str, Any] | None:
        controls = getattr(self, "manualPanelControls", None)
        if not isinstance(controls, dict):
            return None
        controller = getattr(self, "controller", None)
        full_range_supported = getattr(controller, "full_range_supported_by_channel", {}) or {}

        output_enabled: dict[int, bool] = {}
        full_range_enabled: dict[int, bool] = {}
        voltage_values: dict[int, float] = {}
        current_limit_values: dict[int, float] = {}
        for channel_index, widgets in controls.items():
            output_widget = widgets.get("output_enabled")
            range_widget = widgets.get("full_range")
            voltage_widget = widgets.get("voltage")
            current_widget = widgets.get("current_limit")
            output_enabled[channel_index] = bool(output_widget.isChecked())
            full_range_enabled[channel_index] = (
                bool(range_widget.isChecked())
                if _coerce_bool(full_range_supported.get(channel_index, False), False)
                else False
            )
            voltage_values[channel_index] = float(voltage_widget.value())
            current_limit_values[channel_index] = float(current_widget.value())
        return {
            "output_enabled": output_enabled,
            "full_range_enabled": full_range_enabled,
            "voltage_values": voltage_values,
            "current_limit_values": current_limit_values,
        }

    def _manual_panel_changed(self, *_args: Any, debounce: bool = False) -> None:
        if getattr(self, "_manualPanelSyncing", False):
            return
        ready, _reason = self._manual_controls_ready()
        if not ready:
            return
        if debounce:
            self._schedule_manual_panel_apply()
            return
        self._cancel_manual_panel_apply()
        self._apply_manual_panel_state()

    def _apply_manual_panel_state(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        state = self._manual_state_from_panel()
        if state is None:
            return
        apply_now = getattr(controller, "applyManualStateFromThread", None)
        if callable(apply_now):
            apply_now(state, parallel=True)
            self._schedule_delayed_refresh(1.5)
            return
        controller.applyManualState(state)

    def _manual_panel_apply_timer(self) -> Any | None:
        timer = getattr(self, "_manualPanelApplyTimer", None)
        if timer is not None:
            return timer
        try:
            from PyQt6.QtCore import QTimer
        except Exception:
            return None
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(self._apply_manual_panel_state)
        self._manualPanelApplyTimer = timer
        return timer

    def _schedule_manual_panel_apply(self) -> None:
        timer = self._manual_panel_apply_timer()
        if timer is None:
            self._apply_manual_panel_state()
            return
        timer.start(_PSU_MANUAL_NUMERIC_DEBOUNCE_MS)

    def _cancel_manual_panel_apply(self) -> None:
        timer = getattr(self, "_manualPanelApplyTimer", None)
        stop = getattr(timer, "stop", None)
        if callable(stop):
            stop()

    def _sync_manual_panel_from_controller(self) -> None:
        def _sync() -> None:
            self._cancel_manual_panel_apply()
            controls = getattr(self, "manualPanelControls", None)
            if not isinstance(controls, dict):
                return
            controller = getattr(self, "controller", None)
            if controller is None:
                return
            output_enabled = getattr(controller, "output_enabled_by_channel", {}) or {}
            full_range_enabled = getattr(controller, "full_range_by_channel", {}) or {}
            voltage_values = getattr(controller, "voltage_setpoint_values", {}) or {}
            current_limit_values = getattr(controller, "current_limit_values", {}) or {}
            self._manualPanelSyncing = True
            try:
                for channel_index, widgets in controls.items():
                    self._set_control_checked(
                        widgets.get("output_enabled"),
                        bool(output_enabled.get(channel_index, False)),
                    )
                    self._set_control_checked(
                        widgets.get("full_range"),
                        bool(full_range_enabled.get(channel_index, False)),
                    )
                    self._set_control_value(
                        widgets.get("voltage"),
                        _coerce_float(voltage_values.get(channel_index), 0.0),
                    )
                    self._set_control_value(
                        widgets.get("current_limit"),
                        _coerce_float(current_limit_values.get(channel_index), 0.0),
                    )
            finally:
                self._manualPanelSyncing = False

        _invoke_gui_callback(_sync)

    def _set_control_checked(self, widget: Any, checked: bool) -> None:
        if widget is None:
            return
        block_signals = getattr(widget, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            set_checked = getattr(widget, "setChecked", None)
            if callable(set_checked):
                set_checked(bool(checked))
            else:
                widget.checked = bool(checked)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _set_control_value(self, widget: Any, value: float) -> None:
        if widget is None:
            return
        block_signals = getattr(widget, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            set_value = getattr(widget, "setValue", None)
            if callable(set_value):
                set_value(float(value))
            else:
                widget.value = float(value)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _manual_save_slot_entry(self, config_index: int) -> dict[str, Any] | None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return None
        lookup = getattr(controller, "_config_entry_by_index", None)
        if callable(lookup):
            return lookup(config_index)
        for entry in list(getattr(controller, "available_configs", []) or []):
            if _coerce_int(entry.get("index"), -1) == config_index:
                return entry
        return None

    def _manual_save_slot_exists(self, config_index: int) -> bool:
        return self._manual_save_slot_entry(config_index) is not None

    def _save_manual_panel_config(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        slot_widget = getattr(self, "manualPanelSaveSlotSpin", None)
        name_widget = getattr(self, "manualPanelSaveNameEdit", None)
        active_widget = getattr(self, "manualPanelSaveActiveBox", None)
        valid_widget = getattr(self, "manualPanelSaveValidBox", None)
        if (
            slot_widget is None
            or name_widget is None
            or active_widget is None
            or valid_widget is None
        ):
            return
        config_index = int(slot_widget.value())
        if self._manual_save_slot_exists(config_index):
            self.print(
                f"Cannot save PSU config {config_index}: this slot already exists. "
                "Choose an empty slot.",
                flag=PRINT.WARNING,
            )
            self._update_manual_panel()
            return
        config_name = str(name_widget.text() or "").strip() or None
        save_now = getattr(controller, "saveCurrentConfigFromThread", None)
        if callable(save_now):
            save_now(
                config_index,
                config_name=config_name,
                active=bool(active_widget.isChecked()),
                valid=bool(valid_widget.isChecked()),
                parallel=True,
            )
            return
        controller.saveCurrentConfig(
            config_index,
            config_name=config_name,
            active=bool(active_widget.isChecked()),
            valid=bool(valid_widget.isChecked()),
        )

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
            return False, "PSU is OFF"

        config_ready = getattr(controller, "_operating_config_ready", None)
        if callable(config_ready):
            ready, reason, _config_index = config_ready()
            return ready, reason

        if self._config_setting_value("operating_config") < 0:
            return False, "select a config first"
        return True, ""

    def _bootstrap_channel_items(self) -> list[dict[str, Any]]:
        default_item = self._default_channel_item()
        return [
            _build_generic_channel_item(
                self.name,
                channel_id,
                default_item=default_item,
            )
            for channel_id in _PSU_CHANNEL_IDS
        ]

    def _ensure_bootstrap_channels_present(self) -> bool:
        """Recreate transient CH0/CH1 rows when the table stayed empty.

        Some Explorer startup paths can still leave the PSU table empty even
        after the missing-INI bootstrap message was emitted. Keep a final local
        guard so the fixed two-row PSU layout is always visible before the
        hardware sync runs.
        """
        if list(getattr(self, "channels", []) or []):
            return False
        self._apply_channel_items(self._bootstrap_channel_items(), persist=False)
        return True

    def _ensure_local_on_action(self) -> None:
        """Expose the global PSU ON/OFF control directly in the plugin toolbar."""
        if (
            not self.useOnOffLogic
            or hasattr(self, "deviceOnAction")
            or not hasattr(self, "closeCommunicationAction")
        ):
            return

        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(on=checked),
            toolTipFalse=f"Turn {self.name} ON.",
            iconFalse=self.makeIcon(_PSU_POWER_ON_ICON),
            toolTipTrue=f"Turn {self.name} OFF and disconnect.",
            iconTrue=self.makeIcon(_PSU_POWER_OFF_ICON),
            before=self.closeCommunicationAction,
            restore=False,
            defaultState=False,
        )
        self._sync_local_on_action()

    def _sync_local_on_action(self) -> None:
        """Keep the local toolbar ON/OFF button synchronized with the device state."""
        action = getattr(self, "deviceOnAction", None)
        if action is None:
            return
        action.blockSignals(True)
        try:
            action.state = self.isOn()
        finally:
            action.blockSignals(False)

    def _hide_channel_table(self) -> None:
        tree = getattr(self, "tree", None)
        if tree is None:
            return
        hide = getattr(tree, "hide", None)
        if callable(hide):
            hide()
            return
        set_visible = getattr(tree, "setVisible", None)
        if callable(set_visible):
            set_visible(False)

    def _hide_channel_table_actions(self) -> None:
        """Hide generic channel-table actions superseded by the PSU panel."""
        for action_name in (
            "advancedAction",
            "importAction",
            "exportAction",
            "saveAction",
            "duplicateChannelAction",
            "deleteChannelAction",
            "moveChannelUpAction",
            "moveChannelDownAction",
            "copyAction",
        ):
            action = getattr(self, action_name, None)
            if action is not None and hasattr(action, "setVisible"):
                action.setVisible(False)

    def _display_main_state(self) -> str:
        """Return the operator-facing state shown in the toolbar badge."""
        raw_state = _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
        is_on = getattr(self, "isOn", None)
        if (
            raw_state != "Disconnected"
            and not _status_requires_operator_attention(raw_state)
            and callable(is_on)
            and not bool(is_on())
        ):
            return "OFF"
        return raw_state

    def _ensure_status_widgets(self) -> None:
        """Add compact global PSU status labels to the plugin toolbar."""
        if (
            getattr(self, "titleBar", None) is None
            or getattr(self, "titleBarLabel", None) is None
            or hasattr(self, "statusBadgeLabel")
        ):
            return

        label_type = type(self.titleBarLabel)
        self.statusBadgeLabel = label_type("")
        self.statusSummaryLabel = label_type("")
        self.diagnosticsSummaryLabel = label_type("")

        if hasattr(self.statusBadgeLabel, "setObjectName"):
            self.statusBadgeLabel.setObjectName(f"{self.name}StatusBadge")
        if hasattr(self.statusSummaryLabel, "setObjectName"):
            self.statusSummaryLabel.setObjectName(f"{self.name}StatusSummary")
        if hasattr(self.diagnosticsSummaryLabel, "setObjectName"):
            self.diagnosticsSummaryLabel.setObjectName(f"{self.name}DiagnosticsSummary")
        if hasattr(self.statusSummaryLabel, "setStyleSheet"):
            self.statusSummaryLabel.setStyleSheet("QLabel { padding-left: 6px; }")
        if hasattr(self.diagnosticsSummaryLabel, "setStyleSheet"):
            self.diagnosticsSummaryLabel.setStyleSheet("QLabel { padding-left: 6px; color: #cbd5e1; }")

        insert_before = getattr(self, "stretchAction", None)
        if insert_before is not None and hasattr(self.titleBar, "insertWidget"):
            self.statusBadgeAction = self.titleBar.insertWidget(
                insert_before,
                self.statusBadgeLabel,
            )
            self.statusSummaryAction = self.titleBar.insertWidget(
                insert_before,
                self.statusSummaryLabel,
            )
            self.diagnosticsSummaryAction = self.titleBar.insertWidget(
                insert_before,
                self.diagnosticsSummaryLabel,
            )
        elif hasattr(self.titleBar, "addWidget"):
            self.statusBadgeAction = self.titleBar.addWidget(self.statusBadgeLabel)
            self.statusSummaryAction = self.titleBar.addWidget(self.statusSummaryLabel)
            self.diagnosticsSummaryAction = self.titleBar.addWidget(self.diagnosticsSummaryLabel)
        else:
            self.statusBadgeAction = None
            self.statusSummaryAction = None
            self.diagnosticsSummaryAction = None

        self._update_status_widgets()

    def _ensure_channel_panel(self) -> None:
        """Replace the generic channel table with a compact PSU operator panel."""
        if hasattr(self, "channelPanelCards"):
            self._update_channel_panel()
            return

        from PyQt6.QtWidgets import (
            QCheckBox,
            QDoubleSpinBox,
            QFrame,
            QGridLayout,
            QHBoxLayout,
            QLabel,
            QLineEdit,
            QPushButton,
            QSizePolicy,
            QSpinBox,
            QVBoxLayout,
            QWidget,
        )

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        cards_row = QWidget()
        cards_layout = QHBoxLayout(cards_row)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(12)

        self.channelPanel = panel
        self.channelPanelCards: dict[int, dict[str, Any]] = {}
        self.manualPanelControls: dict[int, dict[str, Any]] = {}
        cards_layout.addStretch(1)
        for channel_index in _PSU_CHANNEL_IDS:
            card = QFrame()
            card.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Fixed,
            )
            card.setMinimumWidth(_PSU_PANEL_CARD_MIN_WIDTH)
            card.setMaximumWidth(_PSU_PANEL_CARD_MAX_WIDTH)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 12, 12, 12)
            card_layout.setSpacing(8)

            header_layout = QHBoxLayout()
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)

            title_label = QLabel(f"CH{channel_index}")
            title_label.setStyleSheet(_PSU_PANEL_TITLE_STYLE)
            display_box = QCheckBox("Display")
            display_box.toggled.connect(
                lambda checked, channel_index=channel_index: self._channel_panel_display_toggled(
                    channel_index,
                    checked,
                )
            )

            header_layout.addWidget(title_label)
            header_layout.addStretch(1)
            header_layout.addWidget(display_box)
            card_layout.addLayout(header_layout)

            control_layout = QGridLayout()
            control_layout.setContentsMargins(0, 0, 0, 0)
            control_layout.setHorizontalSpacing(10)
            control_layout.setVerticalSpacing(6)

            output_label = QLabel("Output")
            output_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
            output_box = QCheckBox("ON")
            output_box.toggled.connect(
                lambda _checked: self._manual_panel_changed(debounce=False)
            )
            range_label = QLabel("Range")
            range_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
            range_box = QCheckBox("Full")
            range_box.setToolTip(
                "Full range enables maximum voltage. Half range lowers voltage capability and allows higher current."
            )
            range_box.toggled.connect(
                lambda _checked: self._manual_panel_changed(debounce=False)
            )
            voltage_label = QLabel("Vset")
            voltage_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
            voltage_widget = self._create_manual_numeric_widget(
                suffix="V",
                decimals=3,
                step=0.1,
                maximum=10000.0,
            )
            voltage_widget.editingFinished.connect(
                lambda: self._manual_panel_changed(debounce=False)
            )
            current_label = QLabel("Ilim")
            current_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
            current_widget = self._create_manual_numeric_widget(
                suffix="A",
                decimals=3,
                step=0.01,
                maximum=10000.0,
            )
            current_widget.editingFinished.connect(
                lambda: self._manual_panel_changed(debounce=False)
            )

            control_layout.addWidget(output_label, 0, 0)
            control_layout.addWidget(output_box, 0, 1)
            control_layout.addWidget(range_label, 1, 0)
            control_layout.addWidget(range_box, 1, 1)
            control_layout.addWidget(voltage_label, 2, 0)
            control_layout.addWidget(voltage_widget, 2, 1)
            control_layout.addWidget(current_label, 3, 0)
            control_layout.addWidget(current_widget, 3, 1)
            card_layout.addLayout(control_layout)

            readback_layout = QGridLayout()
            readback_layout.setContentsMargins(0, 0, 0, 0)
            readback_layout.setHorizontalSpacing(10)
            readback_layout.setVerticalSpacing(6)

            metric_widgets: dict[str, Any] = {}
            for row, (label_text, key) in enumerate(
                (
                    ("Vget", "voltage_monitor"),
                    ("Iget", "current_monitor"),
                )
            ):
                name_label = QLabel(label_text)
                name_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
                value_label = QLabel("n/a")
                value_label.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
                readback_layout.addWidget(name_label, row, 0)
                readback_layout.addWidget(value_label, row, 1)
                metric_widgets[key] = value_label

            card_layout.addLayout(readback_layout)
            cards_layout.addWidget(card)
            self.channelPanelCards[channel_index] = {
                "card": card,
                "title": title_label,
                "display_box": display_box,
                "output_enabled": output_box,
                "full_range": range_box,
                "voltage": voltage_widget,
                "current_limit": current_widget,
                **metric_widgets,
            }
            self.manualPanelControls[channel_index] = {
                "output_enabled": output_box,
                "full_range": range_box,
                "voltage": voltage_widget,
                "current_limit": current_widget,
            }
        cards_layout.addStretch(0)
        layout.addWidget(cards_row)

        diag_frame = QFrame()
        diag_frame.setStyleSheet(_PSU_PANEL_DIAGNOSTICS_STYLE)
        diag_frame.setFixedWidth(_PSU_PANEL_DIAGNOSTICS_MAX_WIDTH)
        diag_frame.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        diag_layout = QGridLayout(diag_frame)
        diag_layout.setContentsMargins(12, 10, 12, 10)
        diag_layout.setHorizontalSpacing(10)
        diag_layout.setVerticalSpacing(6)

        diag_widgets: dict[str, Any] = {}
        channel_numbers = sorted(ch.channel_number() for ch in self.getChannels() if ch.real)
        col_offset = 1
        for ch_no in channel_numbers:
            ch_header = QLabel(f"CH{ch_no}")
            ch_header.setStyleSheet(_PSU_PANEL_SECTION_HEADER_STYLE)
            diag_layout.addWidget(ch_header, 0, col_offset)

            for row, (label_text, key) in enumerate(
                (
                    ("Tadc", f"temperature_monitor_ch{ch_no}"),
                    ("Dropout", f"dropout_monitor_ch{ch_no}"),
                    ("Range", f"range_monitor_ch{ch_no}"),
                    ("Rails", f"rails_monitor_ch{ch_no}"),
                ),
                start=1,
            ):
                if ch_no == channel_numbers[0]:
                    name_label = QLabel(label_text)
                    name_label.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
                    diag_layout.addWidget(name_label, row, 0)
                value_label = QLabel("n/a")
                value_label.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
                diag_layout.addWidget(value_label, row, col_offset)
                diag_widgets[key] = value_label
            col_offset += 1

        flags_row = col_offset + 1
        flags_name = QLabel("Flags")
        flags_name.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
        flags_value = QLabel("n/a")
        flags_value.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
        diag_layout.addWidget(flags_name, flags_row, 0)
        diag_layout.addWidget(flags_value, flags_row, 1, 1, col_offset - 1)
        diag_widgets["flags"] = flags_value

        ilim_row = flags_row + 1
        ilim_name = QLabel("Ilim active")
        ilim_name.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
        ilim_value = QLabel("n/a")
        ilim_value.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
        diag_layout.addWidget(ilim_name, ilim_row, 0)
        diag_layout.addWidget(ilim_value, ilim_row, 1, 1, col_offset - 1)
        diag_widgets["ilim_active"] = ilim_value

        ilock_row = ilim_row + 1
        ilock_name = QLabel("Interlock")
        ilock_name.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
        ilock_value = QLabel("n/a")
        ilock_value.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
        diag_layout.addWidget(ilock_name, ilock_row, 0)
        diag_layout.addWidget(ilock_value, ilock_row, 1, 1, col_offset - 1)
        diag_widgets["interlock"] = ilock_value

        psu_enb_row = ilock_row + 1
        psu_enb_name = QLabel("PSU enabled")
        psu_enb_name.setStyleSheet(_PSU_PANEL_METRIC_NAME_STYLE)
        psu_enb_value = QLabel("n/a")
        psu_enb_value.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
        diag_layout.addWidget(psu_enb_name, psu_enb_row, 0)
        diag_layout.addWidget(psu_enb_value, psu_enb_row, 1, 1, col_offset - 1)
        diag_widgets["psu_enabled"] = psu_enb_value

        self.channelPanelGlobalDiagnostics = diag_widgets

        cards_layout.addWidget(diag_frame)
        cards_layout.addStretch(1)

        advanced_section = QWidget()
        advanced_section_layout = QVBoxLayout(advanced_section)
        advanced_section_layout.setContentsMargins(0, 0, 0, 0)
        advanced_section_layout.setSpacing(12)

        manual_row = QWidget()
        manual_layout = QHBoxLayout(manual_row)
        manual_layout.setContentsMargins(0, 0, 0, 0)
        manual_layout.setSpacing(0)
        manual_layout.addStretch(1)

        manual_frame = QFrame()
        manual_frame.setStyleSheet(_PSU_PANEL_DIAGNOSTICS_STYLE)
        manual_frame.setMaximumWidth(_PSU_PANEL_OPERATOR_MAX_WIDTH)
        manual_frame.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        manual_frame_layout = QVBoxLayout(manual_frame)
        manual_frame_layout.setContentsMargins(12, 10, 12, 10)
        manual_frame_layout.setSpacing(8)

        save_grid = QGridLayout()
        save_grid.setContentsMargins(0, 0, 0, 0)
        save_grid.setHorizontalSpacing(10)
        save_grid.setVerticalSpacing(6)
        save_header = QLabel("Save as Config")
        save_header.setStyleSheet(_PSU_OPERATOR_HEADER_STYLE)
        save_grid.addWidget(save_header, 0, 0, 1, 6)
        save_slot_label = QLabel("Slot")
        save_name_label = QLabel("Name")
        save_slot_label.setStyleSheet(_PSU_OPERATOR_HEADER_STYLE)
        save_name_label.setStyleSheet(_PSU_OPERATOR_HEADER_STYLE)
        save_slot_widget = self._create_manual_slot_widget()
        save_slot_widget.valueChanged.connect(lambda _value: self._update_manual_panel())
        save_name_widget = QLineEdit()
        save_name_widget.setPlaceholderText("Optional config name")
        save_active_box = QCheckBox("Active")
        save_active_box.setChecked(True)
        save_valid_box = QCheckBox("Valid")
        save_valid_box.setChecked(True)
        save_button = QPushButton("Save config")
        save_button.clicked.connect(self._save_manual_panel_config)
        save_grid.addWidget(save_slot_label, 1, 0)
        save_grid.addWidget(save_slot_widget, 1, 1)
        save_grid.addWidget(save_name_label, 1, 2)
        save_grid.addWidget(save_name_widget, 1, 3)
        save_grid.addWidget(save_active_box, 1, 4)
        save_grid.addWidget(save_valid_box, 1, 5)
        save_grid.addWidget(save_button, 1, 6)
        manual_frame_layout.addLayout(save_grid)

        manual_layout.addWidget(manual_frame)
        manual_layout.addStretch(1)
        advanced_section_layout.addWidget(manual_row)

        self.manualPanelFrame = manual_frame
        self.manualPanelSaveSlotSpin = save_slot_widget
        self.manualPanelSaveNameEdit = save_name_widget
        self.manualPanelSaveActiveBox = save_active_box
        self.manualPanelSaveValidBox = save_valid_box
        self.manualPanelSaveButton = save_button
        self.channelPanelAdvancedSection = advanced_section

        layout.addWidget(advanced_section)
        _set_widget_visible(advanced_section, False)

        self.addContentWidget(panel)
        self._sync_manual_panel_from_controller()
        self._update_channel_panel()

    def _channel_by_number(self, channel_index: int) -> Any | None:
        for channel in list(getattr(self, "channels", []) or []):
            channel_no_getter = getattr(channel, "channel_number", None)
            channel_no = (
                channel_no_getter()
                if callable(channel_no_getter)
                else _coerce_int(getattr(channel, "id", -1), -1)
            )
            if channel_no == channel_index and _coerce_bool(getattr(channel, "real", True), True):
                return channel
        return None

    def _channel_display_checked(self, channel: Any | None) -> bool:
        if channel is None:
            return False
        getter = getattr(channel, "getParameterByName", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(getattr(channel, "DISPLAY", "Display"))
                if parameter is not None:
                    return _coerce_bool(
                        getattr(parameter, "value", getattr(channel, "display", False)),
                        _coerce_bool(getattr(channel, "display", False), False),
                    )
        return _coerce_bool(getattr(channel, "display", False), False)

    def _channel_panel_snapshot(self, channel_index: int) -> dict[str, Any]:
        controller = getattr(self, "controller", None)
        channel = self._channel_by_number(channel_index)
        connected = (
            _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
            != "Disconnected"
        )
        output_enabled = getattr(controller, "output_enabled_by_channel", {}) or {}
        voltage_monitors = getattr(controller, "values", {}) or {}
        current_monitors = getattr(controller, "current_values", {}) or {}
        voltage_setpoints = getattr(controller, "voltage_setpoints", {}) or {}
        current_setpoints = getattr(controller, "current_setpoints", {}) or {}

        output_state = (
            "ON"
            if output_enabled.get(channel_index, False)
            else "OFF"
            if connected or output_enabled
            else "n/a"
        )

        psu_actually_enabled = _coerce_bool(
            getattr(controller, "psu_enabled_actual", None), default=None
        )
        readback_available = psu_actually_enabled is True

        return {
            "title": f"CH{channel_index}",
            "output_state": output_state,
            "output_style": _psu_output_state_badge_style(output_state),
            "card_style": _psu_panel_card_style(
                connected=connected,
                output_state=output_state,
            ),
            "display_enabled": channel is not None,
            "display_checked": self._channel_display_checked(channel),
            "full_range_text": _format_full_range_text(
                enabled=getattr(controller, "full_range_by_channel", {}).get(channel_index, False),
                supported=getattr(controller, "full_range_supported_by_channel", {}).get(
                    channel_index,
                    False,
                ),
            ),
            "voltage_set": str(voltage_setpoints.get(channel_index, "n/a") or "n/a"),
            "voltage_monitor": (
                _format_voltage_text(voltage_monitors.get(channel_index, np.nan))
                if readback_available
                else "n/a"
            ),
            "current_set": str(current_setpoints.get(channel_index, "n/a") or "n/a"),
            "current_monitor": (
                _format_current_text(current_monitors.get(channel_index, np.nan))
                if readback_available
                else "n/a"
            ),
            "voltage_monitor_style": (
                _psu_feedback_style(
                    _voltage_feedback_state(
                        enabled=output_enabled.get(channel_index, False),
                        measured_v=voltage_monitors.get(channel_index, np.nan),
                        set_v=getattr(controller, "voltage_setpoint_values", {}).get(
                            channel_index,
                            np.nan,
                        ),
                    )
                )
                if readback_available
                else _PSU_PANEL_METRIC_VALUE_STYLE
            ),
            "current_monitor_style": (
                _psu_feedback_style(
                    _current_limit_feedback_state(
                        enabled=output_enabled.get(channel_index, False),
                        measured_a=current_monitors.get(channel_index, np.nan),
                        limit_a=getattr(controller, "current_limit_values", {}).get(
                            channel_index,
                            np.nan,
                        ),
                        current_limit_active=getattr(controller, "current_limit_active", False),
                    )
                )
                if readback_available
                else _PSU_PANEL_METRIC_VALUE_STYLE
            ),
            "temperature_monitor": _format_temperature_text(
                getattr(controller, "adc_temperatures", {}).get(channel_index, np.nan)
            ),
            "temperature_monitor_style": _psu_feedback_style(
                _temperature_feedback_state(
                    getattr(controller, "adc_temperatures", {}).get(channel_index, np.nan)
                )
            ),
            "dropout_monitor": _format_voltage_text(
                getattr(controller, "dropout_values", {}).get(channel_index, np.nan)
            ),
            "dropout_monitor_style": _psu_feedback_style(
                _dropout_feedback_state(
                    getattr(controller, "dropout_values", {}).get(channel_index, np.nan)
                )
            ),
            "range_monitor": _format_full_range_text(
                enabled=getattr(controller, "full_range_by_channel", {}).get(channel_index, False),
                supported=getattr(controller, "full_range_supported_by_channel", {}).get(
                    channel_index, False
                ),
            ),
            "range_monitor_style": _PSU_PANEL_METRIC_VALUE_STYLE,
            "rails_monitor": getattr(controller, "rail_summaries", {}).get(channel_index, "n/a") or "n/a",
            "rails_monitor_style": _PSU_PANEL_METRIC_VALUE_STYLE,
        }

    def _channel_panel_diagnostics_snapshot(self) -> dict[str, str]:
        controller = getattr(self, "controller", None)
        adc_temperatures = getattr(controller, "adc_temperatures", {}) or {}
        dropout_values = getattr(controller, "dropout_values", {}) or {}
        rail_summaries = getattr(controller, "rail_summaries", {}) or {}
        full_range_by_channel = getattr(controller, "full_range_by_channel", {}) or {}
        full_range_supported = getattr(controller, "full_range_supported_by_channel", {}) or {}

        summaries = [
            _format_channel_diagnostics_summary(
                channel_index,
                temp_c=adc_temperatures.get(channel_index, np.nan),
                dropout_v=dropout_values.get(channel_index, np.nan),
                full_range_enabled=full_range_by_channel.get(channel_index, False),
                full_range_supported=full_range_supported.get(channel_index, False),
            )
            for channel_index in _PSU_CHANNEL_IDS
        ]
        flags = str(getattr(controller, "device_state_summary", "") or "").strip() or "n/a"
        tooltip_lines = [f"Device flags: {flags}"]
        if _coerce_bool(getattr(controller, "current_limit_active", False), False):
            tooltip_lines.append("Current limit/compliance is currently active.")
        for channel_index, summary in zip(_PSU_CHANNEL_IDS, summaries):
            tooltip_lines.append(summary)
            rail_summary = str(rail_summaries.get(channel_index, "") or "").strip() or "n/a"
            tooltip_lines.append(f"CH{channel_index} rails: {rail_summary}")
        return {
            "text": " | ".join(summaries) if summaries else "n/a",
            "tooltip": "\n".join(tooltip_lines),
        }

    def _schedule_delayed_refresh(self, delay_s: float) -> None:
        def _do_refresh() -> None:
            self._update_status_widgets()
            self._sync_manual_panel_from_controller()

        try:
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(int(delay_s * 1000), _do_refresh)
        except ImportError:
            _invoke_gui_callback(_do_refresh)

    def _update_manual_panel(self) -> None:
        controls = getattr(self, "manualPanelControls", None)
        if not isinstance(controls, dict):
            return
        controller = getattr(self, "controller", None)
        ready, reason = self._manual_controls_ready()
        full_range_supported = (
            getattr(controller, "full_range_supported_by_channel", {}) or {}
        )
        save_slot_widget = getattr(self, "manualPanelSaveSlotSpin", None)
        save_slot_index = (
            int(save_slot_widget.value())
            if save_slot_widget is not None and hasattr(save_slot_widget, "value")
            else -1
        )
        save_slot_exists = (
            save_slot_index >= 0 and self._manual_save_slot_exists(save_slot_index)
        )
        save_ready = ready and not save_slot_exists
        for channel_index, widgets in controls.items():
            output_widget = widgets.get("output_enabled")
            if output_widget is not None and hasattr(output_widget, "setEnabled"):
                output_widget.setEnabled(ready)
            range_widget = widgets.get("full_range")
            if range_widget is not None and hasattr(range_widget, "setEnabled"):
                range_widget.setEnabled(
                    ready
                    and _coerce_bool(full_range_supported.get(channel_index, False), False)
                )
            for widget_name in ("voltage", "current_limit"):
                widget = widgets.get(widget_name)
                if widget is not None and hasattr(widget, "setEnabled"):
                    widget.setEnabled(ready)
        for widget_name in (
            "manualPanelSaveSlotSpin",
            "manualPanelSaveNameEdit",
            "manualPanelSaveActiveBox",
            "manualPanelSaveValidBox",
        ):
            widget = getattr(self, widget_name, None)
            if widget is not None and hasattr(widget, "setEnabled"):
                widget.setEnabled(ready)
            elif widget is not None:
                widget.enabled = ready
        save_button = getattr(self, "manualPanelSaveButton", None)
        if save_button is not None and hasattr(save_button, "setEnabled"):
            save_button.setEnabled(save_ready)
        elif save_button is not None:
            save_button.enabled = save_ready

        frame = getattr(self, "manualPanelFrame", None)
        tooltip = (
            "Manual edits can be applied directly from the CH0/CH1 cards while the PSU communication is initialized."
        )
        if not ready and reason:
            tooltip = f"{tooltip}\nCurrently unavailable: {reason}."
        if frame is not None and hasattr(frame, "setToolTip"):
            frame.setToolTip(tooltip)
        if save_button is not None and hasattr(save_button, "setToolTip"):
            save_reason = reason if not ready else ""
            if save_slot_exists:
                save_reason = f"config {save_slot_index} already exists"
            save_button.setToolTip(
                "Save the current PSU output, range, voltage, and current-limit values into an empty config slot."
                + (
                    f"\nCurrently unavailable: {save_reason}."
                    if save_reason
                    else ""
                )
            )

    def _set_channel_panel_checkbox(self, checkbox: Any, *, checked: bool, enabled: bool) -> None:
        if checkbox is None:
            return
        block_signals = getattr(checkbox, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            set_checked = getattr(checkbox, "setChecked", None)
            if callable(set_checked):
                set_checked(bool(checked))
            else:
                checkbox.checked = bool(checked)
            set_enabled = getattr(checkbox, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(bool(enabled))
            else:
                checkbox.enabled = bool(enabled)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _set_channel_panel_numeric(self, widget: Any, *, value: float, enabled: bool) -> None:
        if widget is None:
            return
        block_signals = getattr(widget, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            set_value = getattr(widget, "setValue", None)
            if callable(set_value):
                set_value(float(value))
            else:
                widget.value = float(value)
            set_enabled = getattr(widget, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(bool(enabled))
            else:
                widget.enabled = bool(enabled)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _channel_panel_display_toggled(self, channel_index: int, checked: bool) -> None:
        channel = self._channel_by_number(channel_index)
        if channel is None:
            return

        getter = getattr(channel, "getParameterByName", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(getattr(channel, "DISPLAY", "Display"))
                if parameter is not None:
                    if hasattr(parameter, "setValueWithoutEvents"):
                        parameter.setValueWithoutEvents(bool(checked))
                    else:
                        parameter.value = bool(checked)
        channel.display = bool(checked)
        display_changed = getattr(channel, "displayChanged", None)
        if callable(display_changed):
            display_changed()
        self._update_channel_panel()

    def _toggle_advanced_panel(self, checked: bool) -> None:
        section = getattr(self, "channelPanelAdvancedSection", None)
        _set_widget_visible(section, bool(checked))
        button = getattr(self, "savePanelToggleButton", None)
        if button is not None and hasattr(button, "setText"):
            button.setText("Hide Save" if checked else "Save...")

    def _update_channel_panel(self) -> None:
        cards = getattr(self, "channelPanelCards", None)
        if not isinstance(cards, dict):
            return

        for channel_index, widgets in cards.items():
            snapshot = self._channel_panel_snapshot(channel_index)
            for key in (
                "title",
                "voltage_monitor",
                "current_monitor",
                "temperature_monitor",
                "dropout_monitor",
                "range_monitor",
                "rails_monitor",
            ):
                widget = widgets.get(key)
                if widget is not None and hasattr(widget, "setText"):
                    widget.setText(str(snapshot[key]))
            card = widgets.get("card")
            if card is not None and hasattr(card, "setStyleSheet"):
                card.setStyleSheet(snapshot["card_style"])
            for style_key in (
                "voltage_monitor_style",
                "current_monitor_style",
            ):
                widget_key = style_key.replace("_style", "")
                widget = widgets.get(widget_key)
                if widget is not None and hasattr(widget, "setStyleSheet"):
                    widget.setStyleSheet(snapshot[style_key])
            tooltip = self._channel_panel_card_tooltip(channel_index)
            if card is not None and hasattr(card, "setToolTip"):
                card.setToolTip(tooltip)
            for widget_name in (
                "title",
                "output_enabled",
                "full_range",
                "voltage",
                "current_limit",
            ):
                widget = widgets.get(widget_name)
                if widget is not None and hasattr(widget, "setToolTip"):
                    widget.setToolTip(tooltip)
            self._set_channel_panel_checkbox(
                widgets.get("display_box"),
                checked=bool(snapshot["display_checked"]),
                enabled=bool(snapshot["display_enabled"]),
            )

        global_diag = getattr(self, "channelPanelGlobalDiagnostics", None)
        if isinstance(global_diag, dict):
            controller = getattr(self, "controller", None)
            for ch_idx in cards:
                ch_snapshot = self._channel_panel_snapshot(ch_idx)
                for key, text in (
                    ("temperature_monitor", ch_snapshot.get("temperature_monitor", "n/a")),
                    ("dropout_monitor", ch_snapshot.get("dropout_monitor", "n/a")),
                    ("range_monitor", ch_snapshot.get("range_monitor", "n/a")),
                    ("rails_monitor", ch_snapshot.get("rails_monitor", "n/a")),
                ):
                    widget = global_diag.get(f"{key}_ch{ch_idx}")
                    if widget is not None and hasattr(widget, "setText"):
                        widget.setText(str(text))
                for style_key, fallback in (
                    ("temperature_monitor_style", _PSU_PANEL_METRIC_VALUE_STYLE),
                    ("dropout_monitor_style", _PSU_PANEL_METRIC_VALUE_STYLE),
                    ("range_monitor_style", _PSU_PANEL_METRIC_VALUE_STYLE),
                    ("rails_monitor_style", _PSU_PANEL_METRIC_VALUE_STYLE),
                ):
                    widget_key = f"{style_key.replace('_style', '')}_ch{ch_idx}"
                    widget = global_diag.get(widget_key)
                    if widget is not None and hasattr(widget, "setStyleSheet"):
                        widget.setStyleSheet(ch_snapshot.get(style_key, fallback))
            flags_widget = global_diag.get("flags")
            if flags_widget is not None and hasattr(flags_widget, "setText"):
                flags_widget.setText(
                    str(getattr(controller, "device_state_summary", "n/a") or "n/a")
                )
            ilim_widget = global_diag.get("ilim_active")
            if ilim_widget is not None and hasattr(ilim_widget, "setText"):
                active = getattr(controller, "current_limit_active", False)
                ilim_widget.setText("Yes" if active else "No")
                if active:
                    ilim_widget.setStyleSheet(_psu_feedback_style("warn"))
                else:
                    ilim_widget.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
            ilock_widget = global_diag.get("interlock")
            if ilock_widget is not None and hasattr(ilock_widget, "setText"):
                ilock_active = getattr(controller, "interlock_active", None)
                ilock_out_dis = getattr(controller, "interlock_out_disabled", False)
                ilock_bnc_dis = getattr(controller, "interlock_bnc_disabled", False)
                if ilock_active is None:
                    ilock_widget.setText("n/a")
                    ilock_widget.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
                elif ilock_active:
                    parts = []
                    if ilock_out_dis:
                        parts.append("OUT disabled")
                    if ilock_bnc_dis:
                        parts.append("BNC disabled")
                    ilock_widget.setText("OK" + (f" ({', '.join(parts)})" if parts else ""))
                    ilock_widget.setStyleSheet(_psu_feedback_style("ok"))
                else:
                    ilock_widget.setText("ERROR — check cable")
                    ilock_widget.setStyleSheet(_psu_feedback_style("error"))
            psu_enb_widget = global_diag.get("psu_enabled")
            if psu_enb_widget is not None and hasattr(psu_enb_widget, "setText"):
                psu_enb = getattr(controller, "psu_enabled_actual", None)
                if psu_enb is None:
                    psu_enb_widget.setText("n/a")
                    psu_enb_widget.setStyleSheet(_PSU_PANEL_METRIC_VALUE_STYLE)
                elif psu_enb:
                    psu_enb_widget.setText("Yes")
                    psu_enb_widget.setStyleSheet(_psu_feedback_style("ok"))
                else:
                    psu_enb_widget.setText("No")
                    psu_enb_widget.setStyleSheet(_psu_feedback_style("error"))

        self._update_manual_panel()

    def _channel_panel_card_tooltip(self, channel_index: int) -> str:
        controller = getattr(self, "controller", None)
        output_enabled = getattr(controller, "output_enabled_by_channel", {}) or {}
        full_range_by_channel = getattr(controller, "full_range_by_channel", {}) or {}
        full_range_supported = getattr(controller, "full_range_supported_by_channel", {}) or {}
        voltage_monitors = getattr(controller, "values", {}) or {}
        current_monitors = getattr(controller, "current_values", {}) or {}
        voltage_set_values = getattr(controller, "voltage_setpoint_values", {}) or {}
        current_limit_values = getattr(controller, "current_limit_values", {}) or {}
        adc_temperatures = getattr(controller, "adc_temperatures", {}) or {}
        dropout_values = getattr(controller, "dropout_values", {}) or {}
        rail_summaries = getattr(controller, "rail_summaries", {}) or {}
        diagnostics = _format_channel_diagnostics_summary(
            channel_index,
            temp_c=adc_temperatures.get(channel_index, np.nan),
            dropout_v=dropout_values.get(channel_index, np.nan),
            full_range_enabled=full_range_by_channel.get(channel_index, False),
            full_range_supported=full_range_supported.get(channel_index, False),
        )
        return "\n".join(
            (
                f"CH{channel_index}",
                f"Output: {'ON' if output_enabled.get(channel_index, False) else 'OFF'}",
                f"Vset: {_format_voltage_text(voltage_set_values.get(channel_index, np.nan))}",
                f"Vget: {_format_voltage_text(voltage_monitors.get(channel_index, np.nan))}",
                f"Ilim: {_format_current_text(current_limit_values.get(channel_index, np.nan))}",
                f"Iget: {_format_current_text(current_monitors.get(channel_index, np.nan))}",
                diagnostics,
                f"Rails: {str(rail_summaries.get(channel_index, '') or 'n/a')}",
            )
        )

    def _config_list_text(self) -> str:
        return str(getattr(self, "available_configs_text", "") or "").strip() or "n/a"

    def _config_list_tooltip_text(self) -> str:
        return "\n".join(
            (
                "Available PSU configs:",
                self._config_list_text(),
            )
        )

    def _ensure_config_selectors(self) -> None:
        if (
            getattr(self, "titleBar", None) is None
            or getattr(self, "titleBarLabel", None) is None
            or hasattr(self, "operatingConfigCombo")
        ):
            return

        label_type = type(self.titleBarLabel)
        insert_before = getattr(
            self,
            "stretchAction",
            None,
        )
        self.operatingConfigLabel = label_type("Config:")
        self.operatingConfigCombo = self._create_config_selector_widget()
        self.loadOperatingConfigButton = self._create_config_button_widget("Load now")
        self.savePanelToggleButton = self._create_config_button_widget("Save...")
        if hasattr(self.savePanelToggleButton, "setCheckable"):
            self.savePanelToggleButton.setCheckable(True)
        self._connect_config_selector(self.operatingConfigCombo, "operating_config")
        self._connect_config_button(self.loadOperatingConfigButton, self.loadOperatingConfigNow)
        self._connect_config_button(self.savePanelToggleButton, self._save_panel_toggle_clicked)
        if insert_before is not None and hasattr(self.titleBar, "insertWidget"):
            for attr_name, widget in (
                ("operatingConfigAction", self.operatingConfigLabel),
                ("operatingConfigComboAction", self.operatingConfigCombo),
                ("loadOperatingConfigAction", self.loadOperatingConfigButton),
                ("savePanelToggleAction", self.savePanelToggleButton),
            ):
                setattr(
                    self,
                    attr_name,
                    self.titleBar.insertWidget(insert_before, widget),
                )
        elif hasattr(self.titleBar, "addWidget"):
            for attr_name, widget in (
                ("operatingConfigAction", self.operatingConfigLabel),
                ("operatingConfigComboAction", self.operatingConfigCombo),
                ("loadOperatingConfigAction", self.loadOperatingConfigButton),
                ("savePanelToggleAction", self.savePanelToggleButton),
            ):
                setattr(self, attr_name, self.titleBar.addWidget(widget))
        else:
            self.operatingConfigAction = None
            self.operatingConfigComboAction = None
            self.loadOperatingConfigAction = None
            self.savePanelToggleAction = None

        self._update_config_controls()

    def _update_config_selectors(self) -> None:
        for attr_name, combo_attr, label_attr in (
            ("operating_config", "operatingConfigCombo", "operatingConfigLabel"),
        ):
            combo = getattr(self, combo_attr, None)
            combo_label = getattr(self, label_attr, None)
            if combo is None:
                continue
            block_signals = getattr(combo, "blockSignals", None)
            if callable(block_signals):
                block_signals(True)
            try:
                self._combo_clear(combo)
                for value, entry_label in self._config_selector_entries(attr_name):
                    self._combo_add_item(combo, entry_label, value)
                selected_index = self._combo_find_data(
                    combo,
                    self._config_setting_value(attr_name),
                )
                if selected_index < 0:
                    selected_index = 0
                set_current_index = getattr(combo, "setCurrentIndex", None)
                if callable(set_current_index):
                    set_current_index(selected_index)
                tooltip = self._config_selector_tooltip_text(attr_name)
                if hasattr(combo, "setToolTip"):
                    combo.setToolTip(tooltip)
                if combo_label is not None and hasattr(combo_label, "setToolTip"):
                    combo_label.setToolTip(tooltip)
            finally:
                if callable(block_signals):
                    block_signals(False)

    def _update_config_controls(self) -> None:
        self._update_config_selectors()
        self._update_manual_panel()

        button = getattr(self, "loadOperatingConfigButton", None)
        ready, reason = self._load_operating_now_ready()
        self._set_action_enabled(button, ready)
        if button is not None and hasattr(button, "setToolTip"):
            tooltip = (
                "Load the selected PSU config immediately. "
                "This action is only available while the PSU is ON."
            )
            if not ready and reason:
                tooltip = f"{tooltip}\nCurrently unavailable: {reason}."
            button.setToolTip(tooltip)
        save_button = getattr(self, "savePanelToggleButton", None)
        save_ready, save_reason = self._manual_controls_ready()
        self._set_action_enabled(save_button, save_ready)
        if save_button is not None and hasattr(save_button, "setToolTip"):
            tooltip = "Show or hide PSU config save tools."
            if not save_ready and save_reason:
                tooltip = f"{tooltip}\nCurrently unavailable: {save_reason}."
            save_button.setToolTip(tooltip)

    def _save_panel_toggle_clicked(self, checked: bool = False) -> None:
        self._toggle_advanced_panel(bool(checked))

    def _config_selector_changed(self, attr_name: str) -> None:
        combo_attr = {
            "standby_config": "standbyConfigCombo",
            "operating_config": "operatingConfigCombo",
        }.get(attr_name)
        combo = getattr(self, combo_attr, None) if combo_attr is not None else None
        if combo is None:
            return
        if self._set_config_setting_value(attr_name, self._combo_current_value(combo)):
            self._update_config_controls()

    def loadOperatingConfigNow(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        load_now = getattr(controller, "loadOperatingConfigNowFromThread", None)
        if callable(load_now):
            load_now(parallel=True)
            self._schedule_delayed_refresh(1.5)
            self._schedule_delayed_refresh(3.5)
            return
        controller.loadOperatingConfigNow()

    def _status_badge_style(self) -> str:
        """Return a compact badge style that reflects the PSU main state."""
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
        """Return the compact PSU runtime summary displayed in the toolbar."""
        controller = getattr(self, "controller", None)
        measured_voltages = getattr(controller, "values", {}) or {}
        measured_currents = getattr(controller, "current_values", {}) or {}
        output_enabled = getattr(controller, "output_enabled_by_channel", {}) or {}
        channel_summaries = [
            _format_channel_runtime_summary(
                channel_index,
                enabled=output_enabled.get(channel_index, False),
                voltage_v=measured_voltages.get(channel_index, np.nan),
                current_a=measured_currents.get(channel_index, np.nan),
            )
            for channel_index in _PSU_CHANNEL_IDS
        ]
        return " | ".join(channel_summaries)

    def _diagnostics_summary_text(self) -> str:
        controller = getattr(self, "controller", None)
        adc_temperatures = getattr(controller, "adc_temperatures", {}) or {}
        parts = [
            _format_channel_temperature_summary(
                channel_index,
                adc_temperatures.get(channel_index, np.nan),
            )
            for channel_index in _PSU_CHANNEL_IDS
        ]
        text = "Temp: " + " | ".join(parts)
        state = _normalize_runtime_state(getattr(self, "main_state", "Disconnected"))
        if state == "ST_ERR_ILOCK":
            text += "  |  Interlock error — connect cable or disable monitoring"
        elif state == "ST_ERR_PSU_DIS":
            text += "  |  PSU disabled — apply values or load a config"
        elif state.startswith("ST_ERR"):
            text += f"  |  Error: {state}"
        return text

    def _status_tooltip_text(self) -> str:
        """Return the full PSU status tooltip for the toolbar widgets."""
        controller = getattr(self, "controller", None)
        display_state = self._display_main_state()
        hardware_state = str(
            getattr(
                self,
                "hardware_main_state",
                getattr(self, "main_state", "Disconnected"),
            )
            or "Disconnected"
        )
        lines = [f"State: {display_state}"]
        if display_state != hardware_state:
            lines.append(f"Hardware state: {hardware_state}")
        lines.extend(
            (
                f"HV outputs: {getattr(self, 'output_summary', '') or 'n/a'}",
                f"Device flags: {getattr(controller, 'device_state_summary', '') or 'n/a'}",
                f"Loaded state: {getattr(self, 'loaded_state_text', '') or 'n/a'}",
                f"Readbacks: {self._status_summary_text()}",
                f"Diagnostics: {self._diagnostics_summary_text()}",
                f"Available configs: {getattr(self, 'available_configs_text', '') or 'n/a'}",
            )
        )
        return "\n".join(lines)

    def _update_status_widgets(self) -> None:
        """Refresh the global PSU status labels in the toolbar."""
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        diagnostics = getattr(self, "diagnosticsSummaryLabel", None)
        self._sync_acquisition_controls()
        if badge is None or summary is None:
            return

        badge_text = self._display_main_state()
        summary_text = self._status_summary_text()
        diagnostics_text = self._diagnostics_summary_text()
        diagnostics_tooltip = self._channel_panel_diagnostics_snapshot().get("tooltip", "")
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
        if diagnostics is not None and hasattr(diagnostics, "setText"):
            diagnostics.setText(diagnostics_text)
        if diagnostics is not None and hasattr(diagnostics, "setToolTip"):
            diagnostics.setToolTip(str(diagnostics_tooltip))
        self._update_channel_panel()

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
        """Hide framework-only PSU columns and keep key readbacks resizable."""
        if self.tree is None or not self.channels:
            return

        parameter_names = list(self.channels[0].getSortedDefaultChannel())
        for hidden_name in (
            getattr(Channel, "COLLAPSE", "Collapse"),
            getattr(Channel, "REAL", "Real"),
            getattr(Channel, "ACTIVE", "Active"),
            getattr(Channel, "ENABLED", "Enabled"),
            getattr(Channel, "VALUE", "Value"),
            getattr(Channel, "EQUATION", "Equation"),
            getattr(Channel, "MIN", "Min"),
            getattr(Channel, "MAX", "Max"),
        ):
            if hidden_name in parameter_names:
                self.tree.setColumnHidden(parameter_names.index(hidden_name), True)

        header = self.tree.header()
        if header is None:
            return

        for parameter_name, default_width in (
            (getattr(Channel, "MONITOR", "Monitor"), 88),
            (self.channelType.ID, 44),
            (self.channelType.OUTPUT_STATE, 58),
            (self.channelType.VOLTAGE_SET, 90),
            (self.channelType.CURRENT_SET, 90),
            (self.channelType.CURRENT_MONITOR, 92),
        ):
            if parameter_name not in parameter_names:
                continue
            column_index = parameter_names.index(parameter_name)
            header.setSectionResizeMode(
                column_index,
                type(header).ResizeMode.Interactive,
            )
            header.resizeSection(column_index, default_width)

    def _apply_channel_items(
        self,
        items: list[dict[str, Any]],
        *,
        persist: bool = True,
    ) -> None:
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
                    header.setSectionResizeMode(type(header).ResizeMode.ResizeToContents)
                for channel in self.getChannels():
                    collapse_changed = getattr(channel, "collapseChanged", None)
                    if callable(collapse_changed):
                        collapse_changed(toggle=False)
                self.tree.scheduleDelayedItemsLayout()
            toggle_advanced = getattr(self, "toggleAdvanced", None)
            if callable(toggle_advanced) and hasattr(self, "advancedAction"):
                toggle_advanced(advanced=self.advancedAction.state)
            self._update_channel_column_visibility()
            self._update_channel_panel()
            estimate_storage = getattr(self, "estimateStorage", None)
            if callable(estimate_storage):
                estimate_storage()
        finally:
            if self.tree is not None:
                self.tree.setUpdatesEnabled(True)
                self.tree.scheduleDelayedItemsLayout()
                viewport = getattr(self.tree, "viewport", lambda: None)()
                if viewport is not None and hasattr(viewport, "update"):
                    viewport.update()
            process_events = getattr(self, "processEvents", None)
            if callable(process_events):
                process_events()
            self.loading = False
        if persist and callable(export_config):
            export_config(useDefaultFile=True)

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

    def _normalize_channel_scaling(self, persist: bool = False) -> bool:
        """Migrate legacy oversized PSU row scaling back to the harmonized table size."""
        channels = list(self.getChannels()) if getattr(self, "channels", None) else []
        if not channels:
            return False

        normalized_channels: list[str] = []
        for channel in channels:
            scaling = str(
                getattr(channel, "scaling", _PSU_TABLE_SCALING) or _PSU_TABLE_SCALING
            ).strip().lower()
            if scaling not in _PSU_LEGACY_OVERSIZED_SCALINGS:
                continue

            set_without_events = getattr(channel, "_set_parameter_value_without_events", None)
            if callable(set_without_events):
                set_without_events(channel.SCALING, _PSU_TABLE_SCALING)
            else:
                getter = getattr(channel, "getParameterByName", None)
                parameter = getter(channel.SCALING) if callable(getter) else None
                if parameter is not None:
                    setter = getattr(parameter, "setValueWithoutEvents", None)
                    if callable(setter):
                        setter(_PSU_TABLE_SCALING)
                    else:
                        parameter.value = _PSU_TABLE_SCALING

            channel.scaling = _PSU_TABLE_SCALING
            scaling_changed = getattr(channel, "scalingChanged", None)
            previous_loading = getattr(channel, "loading", False)
            try:
                channel.loading = True
                if callable(scaling_changed):
                    scaling_changed()
            finally:
                channel.loading = previous_loading
            normalized_channels.append(str(getattr(channel, "name", "Unknown")))

        if not normalized_channels:
            return False

        if self.tree is not None:
            self.tree.scheduleDelayedItemsLayout()
            viewport = getattr(self.tree, "viewport", lambda: None)()
            if viewport is not None and hasattr(viewport, "update"):
                viewport.update()

        self.print(
            "Normalized legacy PSU table scaling to 'normal' for "
            + ", ".join(normalized_channels)
            + "."
        )
        export_config = getattr(self, "exportConfiguration", None)
        if persist and callable(export_config):
            export_config(useDefaultFile=True)
        return True

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=1,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the PSU controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Baud rate passed to cgc.psu.PSU.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=1.0,
            maximum=60.0,
            toolTip="Timeout in seconds used to connect and validate the PSU transport.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.STARTUP_TIMEOUT}"] = parameterDict(
            value=10.0,
            minimum=1.0,
            maximum=120.0,
            toolTip="Timeout in seconds used for PSU startup and shutdown sequences.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="startup_timeout_s",
        )
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=0.5,
            maximum=30.0,
            toolTip="Timeout in seconds used to poll PSU housekeeping.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
        )
        settings[f"{self.name}/{self.OPERATING_CONFIG}"] = parameterDict(
            value=-1,
            minimum=_PSU_FLOAT_SENTINEL,
            maximum=255,
            toolTip=(
                "PSU config exposed in the plugin toolbar. Use -1 to connect "
                "without enabling outputs until manual values are applied."
            ),
            parameterType=PARAMETERTYPE.INT,
            attr="operating_config",
            event=self._update_config_selectors,
        )
        settings[f"{self.name}/{self.SHUTDOWN_CONFIG}"] = parameterDict(
            value=-1,
            minimum=_PSU_FLOAT_SENTINEL,
            maximum=255,
            toolTip="Optional shutdown config index. Use -1 to disable config-based shutdown.",
            parameterType=PARAMETERTYPE.INT,
            attr="shutdown_config",
            advanced=True,
            event=self._update_config_selectors,
        )
        settings[f"{self.name}/{self.STATE}"] = parameterDict(
            value="Disconnected",
            toolTip="Latest PSU controller state reported by the driver.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="main_state",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.OUTPUTS}"] = parameterDict(
            value="CH0=OFF, CH1=OFF",
            toolTip="Latest PSU output enable summary.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="output_summary",
            indicator=True,
            internal=True,
            advanced=True,
            restore=False,
        )
        settings[f"{self.name}/{self.AVAILABLE_CONFIGS}"] = parameterDict(
            value="n/a",
            toolTip=(
                "PSU configuration slots reported by the controller after connect. "
                "Use these indices for standby, operating, and shutdown config settings."
            ),
            parameterType=PARAMETERTYPE.LABEL,
            attr="available_configs_text",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.INTERLOCK_MONITORING}"] = parameterDict(
            value=True,
            toolTip=(
                "Enable interlock monitoring. Disable for testing without "
                "the physical interlock cable connected."
            ),
            parameterType=PARAMETERTYPE.BOOL,
            attr="interlock_monitoring",
            advanced=True,
            event=self._interlock_monitoring_changed,
        )
        if f"{self.name}/Interval" in settings:
            settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        if f"{self.name}/{self.MAXDATAPOINTS}" in settings:
            settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

    def _set_on_ui_state(self, on: bool) -> None:
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
        if main_state != "ST_ON":
            return False, f"state is {main_state}"
        return True, ""

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
        """Disable manual acquisition controls until the PSU is actually ready."""
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
        """Only allow data recording when the PSU is initialized and in ST_ON."""
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

    def closeCommunication(self) -> None:
        controller = getattr(self, "controller", None)
        if self.useOnOffLogic and not hasattr(self, "onAction"):
            if controller:
                controller.closeCommunication()
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
        self._update_status_widgets()

    def shutdownCommunication(self) -> None:
        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False
            self._sync_local_on_action()
        controller = getattr(self, "controller", None)
        if controller:
            controller.shutdownCommunication()
        self._update_status_widgets()

    def setOn(self, on: "bool | None" = None) -> None:
        controller = getattr(self, "controller", None)
        current_state = self.isOn() if hasattr(self, "onAction") else False
        transition_target = getattr(controller, "transition_target_on", None)
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
        self._update_config_controls()
        self._update_status_widgets()
        if getattr(self, "loading", False):
            return

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
            self.print(
                f"PSU config file {file} not found. "
                "Bootstrapping transient CH0/CH1 channels until hardware initialization."
            )
            self._apply_channel_items(self._bootstrap_channel_items(), persist=False)
            plugin_manager = getattr(self, "pluginManager", None)
            device_manager = getattr(plugin_manager, "DeviceManager", None)
            global_update = getattr(device_manager, "globalUpdate", None)
            if callable(global_update):
                global_update(inout=self.inout)
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)
        self._normalize_channel_scaling(persist=useDefaultFile)
        self._update_channel_column_visibility()

    def toggleAdvanced(self, advanced: "bool | None" = False) -> None:
        super().toggleAdvanced(advanced=advanced)
        self._update_channel_column_visibility()


class PSUChannel(Channel):
    """PSU output channel definition."""

    ID = "CH"
    OUTPUT_STATE = "Output"
    VOLTAGE_SET = "Voltage set"
    CURRENT_SET = "Current set"
    CURRENT_MONITOR = "Current monitor"
    channelParent: PSUDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.id: int
        self.output_state: str
        self.voltage_set: str
        self.current_set: str
        self.current_monitor: str

        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Reference"
        channel[self.VALUE][_PARAMETER_ADVANCED_KEY] = True
        channel[self.VALUE][_PARAMETER_TOOLTIP_KEY] = (
            "Unused by the PSU plugin. Operator controls live in the fixed PSU panel; "
            "this hidden table field is kept only for framework compatibility."
        )
        channel[self.ENABLED][_PARAMETER_ADVANCED_KEY] = True
        channel[self.ACTIVE][_PARAMETER_ADVANCED_KEY] = True
        channel[self.DISPLAY][Parameter.HEADER] = "Display"
        channel[self.DISPLAY][_PARAMETER_EVENT_KEY] = self.displayChanged
        channel[self.SCALING][Parameter.VALUE] = "normal"
        monitor_name = getattr(self, "MONITOR", "Monitor")
        if monitor_name in channel:
            channel[monitor_name][Parameter.HEADER] = "Vget"
            channel[monitor_name][_PARAMETER_TOOLTIP_KEY] = (
                "Measured PSU output voltage read back from the controller."
            )
        channel[self.ID] = parameterDict(
            value="0",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="CH ",
            attr="id",
        )
        channel[self.OUTPUT_STATE] = parameterDict(
            value="OFF",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="On",
            attr="output_state",
            toolTip="Latest PSU output enable readback for this channel.",
        )
        channel[self.VOLTAGE_SET] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Vset",
            attr="voltage_set",
            toolTip="Configured PSU voltage setpoint read back from the controller.",
        )
        channel[self.CURRENT_SET] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Ilim",
            attr="current_set",
            toolTip=(
                "Configured PSU current setting read back from the controller. "
                "In normal constant-voltage operation this acts as the current limit/compliance."
            ),
        )
        channel[self.CURRENT_MONITOR] = parameterDict(
            value="n/a",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Iget",
            attr="current_monitor",
            toolTip="Measured PSU output current read back from the controller.",
        )
        return channel

    def setDisplayedParameters(self) -> None:
        # Keep framework-only parameters instantiated for bootstrap safety, but
        # order the visible PSU readbacks explicitly instead of inheriting the
        # generic IN-channel layout.
        displayed: list[str] = [
            getattr(self, "COLLAPSE", "Collapse"),
            getattr(self, "SELECT", "Select"),
            self.NAME,
            self.OUTPUT_STATE,
            self.VOLTAGE_SET,
            getattr(self, "MONITOR", "Monitor"),
            self.CURRENT_SET,
            self.CURRENT_MONITOR,
            self.ID,
            self.ENABLED,
            self.VALUE,
            getattr(self, "EQUATION", "Equation"),
            self.ACTIVE,
            self.REAL,
            getattr(self, "SMOOTH", "Smooth"),
            getattr(self, "LINEWIDTH", "Linewidth"),
            getattr(self, "LINESTYLE", "Linestyle"),
            getattr(self, "DISPLAYGROUP", "Group"),
            self.SCALING,
            getattr(self, "COLOR", "Color"),
            getattr(self, "MIN", "Min"),
            getattr(self, "MAX", "Max"),
            self.DISPLAY,
        ]
        self.displayedParameters = list(dict.fromkeys(displayed))

    def initGUI(self, item: dict) -> None:
        # Legacy PSU channel configs may not have initialized framework flags yet.
        # Seed the attributes used by core.Channel.updateColor() before the base init runs.
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
            self.max = _coerce_float(item.get(getattr(self, "MAX", "Max")), 0.0)
        super().initGUI(item)
        self._sync_output_state_widget()
        self.monitorChanged()
        self.scalingChanged()

    def scalingChanged(self) -> None:
        scaling_changed = getattr(super(), "scalingChanged", None)
        if callable(scaling_changed):
            scaling_changed()
        row_height = getattr(self, "rowHeight", 0)
        if row_height >= _PSU_MIN_ROW_HEIGHT:
            return
        if row_height <= 0:
            return
        self.rowHeight = _PSU_MIN_ROW_HEIGHT
        for parameter in getattr(self, "parameters", []):
            if hasattr(parameter, "setHeight"):
                parameter.setHeight(self.rowHeight)
        if not self.loading and self.tree:
            self.tree.scheduleDelayedItemsLayout()

    def channel_number(self) -> int:
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

    def _set_parameter_widget_style(self, parameter_name: str, style: str) -> None:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(parameter_name)
        if parameter is None:
            return
        widget = getattr(parameter, "getWidget", lambda: None)()
        if widget is None:
            return
        container = getattr(widget, "container", None)
        if container is not None and hasattr(container, "setStyleSheet"):
            container.setStyleSheet(style)
        if hasattr(widget, "setStyleSheet"):
            widget.setStyleSheet(style)

    def _sync_output_state_widget(self) -> None:
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(self.OUTPUT_STATE)
        if parameter is None:
            return
        state = getattr(self, "output_state", getattr(parameter, "value", "n/a"))
        self._set_parameter_widget_style(
            self.OUTPUT_STATE,
            _psu_output_state_badge_style(state),
        )

    def displayChanged(self) -> None:
        update_display = getattr(super(), "updateDisplay", None)
        if callable(update_display):
            update_display()

    def monitorChanged(self) -> None:
        self.warningState = False
        self._set_parameter_widget_style(
            getattr(self, "MONITOR", "Monitor"),
            _PSU_NEUTRAL_WIDGET_STYLE,
        )

    def realChanged(self) -> None:
        getter = getattr(self, "getParameterByName", None)
        if callable(getter):
            for parameter_name in (
                self.ID,
                getattr(self, "MONITOR", "Monitor"),
                self.OUTPUT_STATE,
                self.VOLTAGE_SET,
                self.CURRENT_SET,
                self.CURRENT_MONITOR,
            ):
                parameter = getter(parameter_name)
                if parameter is not None and hasattr(parameter, "setVisible"):
                    parameter.setVisible(self.real)
            enabled_parameter = getter(getattr(self, "ENABLED", "Enabled"))
            if enabled_parameter is None:
                return
        real_changed = getattr(super(), "realChanged", None)
        if callable(real_changed):
            real_changed()

    def setCurrentMonitorText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.CURRENT_MONITOR, text)

    def setOutputStateText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.OUTPUT_STATE, text)
        self._sync_output_state_widget()

    def setVoltageSetText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.VOLTAGE_SET, text)

    def setCurrentSetText(self, text: str) -> None:
        self._set_parameter_value_without_events(self.CURRENT_SET, text)

    def updateColor(self):
        """Keep PSU indicators visually aligned with DMMR/AMPR channel tables."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QBrush
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QSizePolicy

        color = super().updateColor()
        if color is None:
            return color

        neutral = QBrush()
        if hasattr(self, "setBackground"):
            for i in range(len(getattr(self, "parameters", [])) + 1):
                self.setBackground(i, neutral)

        for parameter in getattr(self, "parameters", []):
            widget = getattr(parameter, "getWidget", lambda: None)()
            if not widget:
                continue
            if hasattr(widget, "container"):
                widget.container.setStyleSheet("")
            if not isinstance(widget, QComboBox) and hasattr(widget, "setStyleSheet"):
                widget.setStyleSheet("")

        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return color

        display_param = getter(self.DISPLAY)
        if display_param:
            display_widget = display_param.getWidget()
            if isinstance(display_widget, QCheckBox):
                display_widget.setSizePolicy(
                    QSizePolicy.Policy.Maximum,
                    display_widget.sizePolicy().verticalPolicy(),
                )
                if (
                    hasattr(display_widget, "container")
                    and display_widget.container.layout()
                ):
                    display_widget.container.layout().setAlignment(
                        display_widget, Qt.AlignmentFlag.AlignCenter
                    )

        for parameter_name in (
            getattr(self, "MONITOR", "Monitor"),
            self.DISPLAY,
            self.ID,
            self.VOLTAGE_SET,
            self.CURRENT_SET,
            self.CURRENT_MONITOR,
        ):
            self._set_parameter_widget_style(parameter_name, _PSU_NEUTRAL_WIDGET_STYLE)
        self._sync_output_state_widget()
        return color


class PSUController(DeviceController):
    """PSU hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: PSUDevice

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.main_state = "Disconnected"
        self.hardware_main_state = "Disconnected"
        self.output_state_summary = "CH0=OFF, CH1=OFF"
        self.device_state_summary = "n/a"
        self.available_configs: list[dict[str, Any]] = []
        self.available_configs_text = "n/a"
        self.loaded_state_text = "n/a"
        self.initialized = False
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._manual_apply_state_lock = Lock()
        self._manual_apply_pending_state: dict[str, Any] | None = None
        self._manual_apply_worker_running = False
        self.values: dict[int, float] = {}
        self.current_values: dict[int, float] = {}
        self.output_enabled_by_channel: dict[int, bool] = {}
        self.voltage_setpoints: dict[int, str] = {}
        self.current_setpoints: dict[int, str] = {}
        self.voltage_setpoint_values: dict[int, float] = {}
        self.current_limit_values: dict[int, float] = {}
        self.full_range_by_channel: dict[int, bool] = {}
        self.full_range_supported_by_channel: dict[int, bool] = {}
        self.current_limit_active = False
        self.adc_temperatures: dict[int, float] = {}
        self.dropout_values: dict[int, float] = {}
        self.rail_summaries: dict[int, str] = {}
        self._last_live_readback_refresh_monotonic = 0.0
        self._last_housekeeping_refresh_monotonic = 0.0

    def initializeValues(self, reset: bool = False) -> None:
        if getattr(self, "values", None) is None or reset:
            self.values = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.current_values = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.output_enabled_by_channel = {
                channel.channel_number(): False
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.voltage_setpoints = {
                channel.channel_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.current_setpoints = {
                channel.channel_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.voltage_setpoint_values = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.current_limit_values = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.full_range_by_channel = {
                channel.channel_number(): False
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.full_range_supported_by_channel = {
                channel.channel_number(): False
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.current_limit_active = False
            self.adc_temperatures = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.dropout_values = {
                channel.channel_number(): np.nan
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self.rail_summaries = {
                channel.channel_number(): "n/a"
                for channel in self.controllerParent.getChannels()
                if channel.real
            }
            self._last_live_readback_refresh_monotonic = 0.0
            self._last_housekeeping_refresh_monotonic = 0.0

    def _format_loaded_config_text(self, config_index: int) -> str:
        entry = self._config_entry_by_index(config_index)
        if entry is None:
            return f"Config {config_index}"
        config_name = str(entry.get("name", "") or "").strip()
        if not config_name:
            return f"Config {config_index}"
        return f"Config {config_index}: {config_name}"

    def _set_loaded_config_text(self, text: str) -> None:
        self.loaded_state_text = str(text or "").strip() or "n/a"

    def _real_channel_numbers(self) -> list[int]:
        return [
            channel.channel_number()
            for channel in self.controllerParent.getChannels()
            if getattr(channel, "real", True)
        ]

    def _housekeeping_refresh_due(self, now_monotonic: float) -> bool:
        last_refresh = float(
            getattr(self, "_last_housekeeping_refresh_monotonic", 0.0) or 0.0
        )
        if last_refresh <= 0:
            return True
        return (
            now_monotonic - last_refresh
        ) >= _PSU_HOUSEKEEPING_REFRESH_PERIOD_S

    def _live_readback_refresh_due(self, now_monotonic: float) -> bool:
        last_refresh = float(
            getattr(self, "_last_live_readback_refresh_monotonic", 0.0) or 0.0
        )
        if last_refresh <= 0:
            return True
        return (
            now_monotonic - last_refresh
        ) >= _PSU_LIVE_READBACK_REFRESH_PERIOD_S

    def _read_live_readbacks(self, *, timeout_s: float) -> dict[str, Any] | None:
        device = self.device
        if device is None:
            return None

        get_device_enabled = getattr(device, "get_device_enabled", None)
        get_output_enabled = getattr(device, "get_output_enabled", None)
        get_channel_voltage = getattr(device, "get_channel_voltage", None)
        get_channel_current = getattr(device, "get_channel_current", None)
        if not (
            callable(get_device_enabled)
            and callable(get_output_enabled)
            and callable(get_channel_voltage)
            and callable(get_channel_current)
        ):
            return None

        device_enabled = bool(get_device_enabled(timeout_s=timeout_s))
        output_enabled = tuple(
            bool(value) for value in get_output_enabled(timeout_s=timeout_s)
        )
        measured_voltages: dict[int, float] = {}
        measured_currents: dict[int, float] = {}
        for channel_no in self._real_channel_numbers():
            measured_voltages[channel_no] = _coerce_float(
                get_channel_voltage(channel_no, timeout_s=timeout_s),
                np.nan,
            )
            measured_currents[channel_no] = _coerce_float(
                get_channel_current(channel_no, timeout_s=timeout_s),
                np.nan,
            )
        return {
            "device_enabled": device_enabled,
            "output_enabled": output_enabled,
            "values": measured_voltages,
            "current_values": measured_currents,
        }

    def _interlock_monitoring_changed(self) -> None:
        enabled = _coerce_bool(getattr(self.controllerParent, "interlock_monitoring", True), default=True)
        device = getattr(self, "device", None)
        if device is None or not getattr(self, "initialized", False):
            return
        timeout_s = float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
        try:
            device.set_interlock_enabled(enabled, enabled, timeout_s=timeout_s)
            state = "enabled" if enabled else "disabled"
            self.print(f"Interlock monitoring {state}.", flag=PRINT.INFO)
        except Exception as exc:
            self.print(f"Could not change interlock monitoring: {exc}", flag=PRINT.WARNING)

    def runInitialization(self) -> None:
        self.initialized = False
        self._dispose_device()
        try:
            driver_class = _get_psu_driver_class()
            self.device = driver_class(
                device_id=f"{self.controllerParent.name.lower()}_com{int(self.controllerParent.com)}",
                com=int(self.controllerParent.com),
                baudrate=int(self.controllerParent.baudrate),
                logger=logging.getLogger(f"esibd.plugins.{self.controllerParent.name.lower()}"),
                allow_process_backend=False,
            )
            backend_reason = str(
                getattr(self.device, "_process_backend_disabled_reason", "")
            ).strip()
            if backend_reason:
                self.print(backend_reason, flag=PRINT.WARNING)
            self.device.connect(timeout_s=float(self.controllerParent.connect_timeout_s))
            if not _coerce_bool(getattr(self.controllerParent, "interlock_monitoring", True), default=True):
                self.device.set_interlock_enabled(False, False, timeout_s=float(self.controllerParent.connect_timeout_s))
                self.print("Interlock monitoring disabled.", flag=PRINT.WARNING)
            self._refresh_available_configs()
            self._update_state()
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:  # noqa: BLE001
            self._restore_off_ui_state()
            self.print(
                f"PSU initialization failed on COM{int(self.controllerParent.com)}: "
                f"{self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
            self._dispose_device()
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        if self.device is not None:
            self.controllerParent._sync_channels()
        self.initializeValues(reset=True)
        self.initialized = True
        self.super_init_complete_called = True
        self._set_loaded_config_text("Connected")
        self._sync_status_to_gui()

    def _startup_kwargs(self) -> dict[str, Any]:
        standby_config = _coerce_int(
            getattr(self.controllerParent, "standby_config", -1),
            -1,
        )
        operating_config = _coerce_int(
            getattr(self.controllerParent, "operating_config", -1),
            -1,
        )
        kwargs: dict[str, Any] = {}
        if standby_config >= 0:
            kwargs["standby_config"] = standby_config
        if operating_config >= 0:
            kwargs["operating_config"] = operating_config
        return kwargs

    def _selected_operating_config_index(self) -> int:
        return _coerce_int(
            getattr(self.controllerParent, "operating_config", -1),
            -1,
        )

    def _config_entry_by_index(self, config_index: int) -> dict[str, Any] | None:
        for entry in self.available_configs:
            if _coerce_int(entry.get("index"), -1) == config_index:
                return entry
        return None

    def _config_slot_exists(self, config_index: int) -> bool:
        return self._config_entry_by_index(config_index) is not None

    def _operating_config_ready(self) -> tuple[bool, str, int]:
        config_index = self._selected_operating_config_index()
        if config_index < 0:
            return False, "select a PSU config first", config_index

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

    def _shutdown_config_index(self) -> int:
        return _coerce_int(
            getattr(self.controllerParent, "shutdown_config", -1),
            -1,
        )

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
            configs = list_configs(
                timeout_s=float(getattr(self.controllerParent, "connect_timeout_s", 5.0))
            )
        except Exception as exc:  # noqa: BLE001
            self.available_configs = []
            self.available_configs_text = "Unavailable"
            self.print(
                f"Could not read PSU config list: {self._format_exception(exc)}",
                flag=PRINT.WARNING,
            )
            return

        self.available_configs = list(configs or [])
        self.available_configs_text = _format_available_configs(configs)

    def _start_manual_mode(self, *, timeout_s: float) -> None:
        device = self.device
        if device is None:
            return
        device.set_output_enabled(False, False, timeout_s=timeout_s)
        device.set_device_enabled(True, timeout_s=timeout_s)
        self._set_loaded_config_text("Manual outputs OFF")

    def _verify_manual_state_unlocked(
        self,
        *,
        device: Any,
        voltage_values: dict[int, Any],
        current_limit_values: dict[int, Any],
        full_range_enabled: tuple[bool, bool],
        timeout_s: float,
    ) -> list[str]:
        warnings: list[str] = []
        get_voltage_limits = getattr(device, "get_channel_voltage_limits", None)
        if callable(get_voltage_limits):
            for channel_index in _PSU_CHANNEL_IDS:
                expected_voltage = _coerce_float(voltage_values.get(channel_index), 0.0)
                actual_voltage, _voltage_limit = get_voltage_limits(
                    channel_index,
                    timeout_s=timeout_s,
                )
                if not _setpoint_matches(
                    actual_voltage,
                    expected_voltage,
                    abs_tolerance=_PSU_SETPOINT_VERIFY_ABS_TOLERANCE_V,
                ):
                    warnings.append(
                        f"CH{channel_index} voltage setpoint readback "
                        f"{_format_voltage_text(actual_voltage)} does not match requested "
                        f"{_format_voltage_text(expected_voltage)}."
                    )

        get_current_limits = getattr(device, "get_channel_current_limits", None)
        if callable(get_current_limits):
            for channel_index in _PSU_CHANNEL_IDS:
                expected_current = _coerce_float(current_limit_values.get(channel_index), 0.0)
                actual_current, _current_limit = get_current_limits(
                    channel_index,
                    timeout_s=timeout_s,
                )
                if not _setpoint_matches(
                    actual_current,
                    expected_current,
                    abs_tolerance=_PSU_SETPOINT_VERIFY_ABS_TOLERANCE_A,
                ):
                    warnings.append(
                        f"CH{channel_index} current limit readback "
                        f"{_format_current_text(actual_current)} does not match requested "
                        f"{_format_current_text(expected_current)}."
                    )

        get_output_full_range = getattr(device, "get_output_full_range", None)
        if callable(get_output_full_range):
            actual_range = tuple(
                bool(value) for value in get_output_full_range(timeout_s=timeout_s)
            )
            if actual_range != tuple(bool(value) for value in full_range_enabled):
                warnings.append(
                    "PSU full-range readback does not match the requested state: "
                    f"requested={full_range_enabled}, actual={actual_range}."
                )
        return warnings

    def _verify_output_enable_state_unlocked(
        self,
        *,
        device: Any,
        any_output_enabled: bool,
        output_enabled: tuple[bool, bool],
        timeout_s: float,
    ) -> None:
        get_device_enabled = getattr(device, "get_device_enabled", None)
        if callable(get_device_enabled):
            actual_device_enabled = bool(get_device_enabled(timeout_s=timeout_s))
            if actual_device_enabled != bool(any_output_enabled):
                expected_text = "enabled" if any_output_enabled else "disabled"
                actual_text = "enabled" if actual_device_enabled else "disabled"
                raise RuntimeError(
                    f"PSU device enable readback is {actual_text} after requesting {expected_text}."
                )

        get_output_enabled = getattr(device, "get_output_enabled", None)
        if callable(get_output_enabled):
            actual_output_enabled = tuple(
                bool(value) for value in get_output_enabled(timeout_s=timeout_s)
            )
            if actual_output_enabled != tuple(bool(value) for value in output_enabled):
                raise RuntimeError(
                    "PSU output-enable readback does not match the requested state: "
                    f"requested={output_enabled}, actual={actual_output_enabled}."
                )

    def _safe_disable_outputs_after_failure(self, *, timeout_s: float) -> None:
        try:
            with self._controller_lock_section(
                "Could not acquire lock to recover PSU outputs after failure."
            ):
                device = self.device
                if device is None:
                    return
                with contextlib.suppress(Exception):
                    device.set_output_enabled(False, False, timeout_s=timeout_s)
                with contextlib.suppress(Exception):
                    device.set_device_enabled(False, timeout_s=timeout_s)
        except Exception:
            return

    def _perform_shutdown_sequence_unlocked(self, *, timeout_s: float) -> list[str]:
        device = self.device
        if device is None:
            return ["PSU device is not available."]

        errors: list[str] = []
        shutdown_config = self._shutdown_config_index()
        if shutdown_config >= 0:
            try:
                device.load_config(shutdown_config, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"load_config({shutdown_config}) failed: {self._format_exception(exc)}"
                )

        for channel_index in _PSU_CHANNEL_IDS:
            try:
                device.set_channel_current(channel_index, 0.0, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    "set_channel_current("
                    f"{channel_index}, 0.0) failed: {self._format_exception(exc)}"
                )
            try:
                device.set_channel_voltage(channel_index, 0.0, timeout_s=timeout_s)
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    "set_channel_voltage("
                    f"{channel_index}, 0.0) failed: {self._format_exception(exc)}"
                )

        try:
            device.set_output_enabled(False, False, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"set_output_enabled(False, False) failed: {self._format_exception(exc)}")
        try:
            device.set_device_enabled(False, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"set_device_enabled(False) failed: {self._format_exception(exc)}")
        return errors

    def _confirm_shutdown_unlocked(self, *, timeout_s: float) -> tuple[bool, str]:
        device = self.device
        if device is None:
            return False, "device disconnected before shutdown confirmation"

        snapshot = device.collect_housekeeping(timeout_s=timeout_s)
        output_enabled = tuple(
            bool(value) for value in snapshot.get("output_enabled", (False, False))
        )
        if any(output_enabled):
            summary = ", ".join(
                f"CH{index}={'ON' if enabled else 'OFF'}"
                for index, enabled in enumerate(output_enabled)
            )
            return False, f"outputs still enabled ({summary})"
        if _coerce_bool(snapshot.get("device_enabled"), False):
            return False, "device still enabled"
        return True, ""

    def readNumbers(self) -> None:
        if self.device is None or not getattr(self, "initialized", False):
            self.initializeValues(reset=True)
            return

        timeout_s = float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
        now_monotonic = time.monotonic()
        snapshot: dict[str, Any] | None = None
        live_readbacks: dict[str, Any] | None = None
        try:
            with self._controller_lock_section(
                "Could not acquire lock to read PSU housekeeping.",
            ):
                device = self.device
                if device is None:
                    return
                if self._housekeeping_refresh_due(now_monotonic):
                    snapshot = device.collect_housekeeping(timeout_s=timeout_s)
                    try:
                        live_readbacks = self._read_live_readbacks(timeout_s=timeout_s)
                    except Exception as exc:  # noqa: BLE001
                        self.print(
                            "Failed to refresh PSU live readbacks after housekeeping: "
                            f"{self._format_exception(exc)}",
                            flag=PRINT.WARNING,
                        )
                elif self._live_readback_refresh_due(now_monotonic):
                    live_readbacks = self._read_live_readbacks(timeout_s=timeout_s)
                    if live_readbacks is None:
                        snapshot = device.collect_housekeeping(timeout_s=timeout_s)
                else:
                    return
        except TimeoutError:
            self.errorCount += 1
            self.print("Timed out while polling PSU housekeeping.", flag=PRINT.ERROR)
            self.initializeValues(reset=True)
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to read PSU housekeeping: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
            self.initializeValues(reset=True)
            return

        try:
            if snapshot is not None:
                self._apply_snapshot(snapshot, refreshed_at=now_monotonic)
            if live_readbacks is not None:
                self._apply_live_readbacks(
                    live_readbacks,
                    refreshed_at=now_monotonic,
                )
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to apply PSU housekeeping snapshot: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
            self.initializeValues(reset=True)
            return

    def _apply_live_readbacks(
        self,
        readbacks: dict[str, Any],
        *,
        refreshed_at: float | None = None,
    ) -> None:
        output_enabled = tuple(
            bool(value) for value in readbacks.get("output_enabled", (False, False))
        )
        output_enabled_map = {
            channel_no: bool(output_enabled[channel_no])
            for channel_no in self._real_channel_numbers()
            if 0 <= channel_no < len(output_enabled)
        }
        measured_voltages = dict(getattr(self, "values", {}) or {})
        measured_voltages.update(
            {
                channel_no: _coerce_float(value, np.nan)
                for channel_no, value in (readbacks.get("values", {}) or {}).items()
            }
        )
        measured_currents = dict(getattr(self, "current_values", {}) or {})
        measured_currents.update(
            {
                channel_no: _coerce_float(value, np.nan)
                for channel_no, value in (
                    readbacks.get("current_values", {}) or {}
                ).items()
            }
        )

        self.main_state = _harmonize_psu_main_state(
            getattr(self, "hardware_main_state", "Unknown"),
            device_enabled=readbacks.get("device_enabled"),
            output_enabled=output_enabled,
        )
        self.output_state_summary = ", ".join(
            f"CH{index}={'ON' if bool(enabled) else 'OFF'}"
            for index, enabled in enumerate(output_enabled)
        )
        self.values = measured_voltages
        self.current_values = measured_currents
        if output_enabled_map:
            self.output_enabled_by_channel = output_enabled_map
        self._last_live_readback_refresh_monotonic = (
            time.monotonic() if refreshed_at is None else float(refreshed_at)
        )
        self._sync_status_to_gui(sync_manual_panel=bool(output_enabled_map))

    def _apply_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        refreshed_at: float | None = None,
    ) -> None:
        self.hardware_main_state = str(
            snapshot.get("main_state", {}).get("name", "Unknown")
        )
        output_enabled = tuple(
            bool(value) for value in snapshot.get("output_enabled", (False, False))
        )
        self.main_state = _harmonize_psu_main_state(
            self.hardware_main_state,
            device_enabled=snapshot.get("device_enabled"),
            output_enabled=output_enabled,
        )
        flags = snapshot.get("device_state", {}).get("flags", [])
        self.device_state_summary = ", ".join(str(flag) for flag in flags) if flags else "OK"
        self.output_state_summary = ", ".join(
            f"CH{index}={'ON' if bool(enabled) else 'OFF'}"
            for index, enabled in enumerate(output_enabled)
        )

        measured_voltages: dict[int, float] = {}
        measured_currents: dict[int, float] = {}
        output_enabled_map: dict[int, bool] = {}
        voltage_setpoints: dict[int, str] = {}
        current_setpoints: dict[int, str] = {}
        voltage_setpoint_values: dict[int, float] = {}
        current_limit_values: dict[int, float] = {}
        full_range_by_channel: dict[int, bool] = {}
        full_range_supported_by_channel: dict[int, bool] = {}
        adc_temperatures: dict[int, float] = {}
        dropout_values: dict[int, float] = {}
        rail_summaries: dict[int, str] = {}
        for channel_snapshot in snapshot.get("channels", []):
            channel_no = _coerce_int(channel_snapshot.get("channel"), -1)
            if channel_no < 0:
                continue
            output_enabled_map[channel_no] = bool(channel_snapshot.get("enabled", False))
            measured_voltages[channel_no] = _coerce_float(
                channel_snapshot.get("voltage", {}).get("measured_v"),
                np.nan,
            )
            measured_currents[channel_no] = _coerce_float(
                channel_snapshot.get("current", {}).get("measured_a"),
                np.nan,
            )
            voltage_setpoint_values[channel_no] = _coerce_float(
                channel_snapshot.get("voltage", {}).get("set_v"),
                np.nan,
            )
            voltage_setpoints[channel_no] = _format_voltage_text(
                voltage_setpoint_values[channel_no]
            )
            current_limit_values[channel_no] = _coerce_float(
                channel_snapshot.get("current", {}).get("set_a"),
                np.nan,
            )
            current_setpoints[channel_no] = _format_current_text(
                current_limit_values[channel_no]
            )
            full_range_by_channel[channel_no] = _coerce_bool(
                channel_snapshot.get("full_range", {}).get("enabled"),
                False,
            )
            full_range_supported_by_channel[channel_no] = _coerce_bool(
                channel_snapshot.get("full_range", {}).get("supported"),
                False,
            )
            adc_temperatures[channel_no] = _coerce_float(
                channel_snapshot.get("adc", {}).get("temp_adc_c"),
                np.nan,
            )
            dropout_values[channel_no] = _coerce_float(
                channel_snapshot.get("dropout_v"),
                np.nan,
            )
            rail_summaries[channel_no] = _format_rail_summary(
                channel_snapshot.get("rails", {})
            )

        self.values = measured_voltages
        self.current_values = measured_currents
        self.output_enabled_by_channel = output_enabled_map
        self.voltage_setpoints = voltage_setpoints
        self.current_setpoints = current_setpoints
        self.voltage_setpoint_values = voltage_setpoint_values
        self.current_limit_values = current_limit_values
        self.full_range_by_channel = full_range_by_channel
        self.full_range_supported_by_channel = full_range_supported_by_channel
        self.current_limit_active = _coerce_bool(
            snapshot.get("psu_state", {}).get("current_limit_active"),
            False,
        )
        psu_state_data = snapshot.get("psu_state", {})
        self.interlock_active = _coerce_bool(psu_state_data.get("interlock_active"), False)
        self.psu_enabled_actual = _coerce_bool(psu_state_data.get("psu_enabled_actual"), False)
        self.interlock_out_disabled = _coerce_bool(psu_state_data.get("interlock_out_disabled"), False)
        self.interlock_bnc_disabled = _coerce_bool(psu_state_data.get("interlock_bnc_disabled"), False)
        self.adc_temperatures = adc_temperatures
        self.dropout_values = dropout_values
        self.rail_summaries = rail_summaries
        refresh_time = time.monotonic() if refreshed_at is None else float(refreshed_at)
        self._last_housekeeping_refresh_monotonic = refresh_time
        self._last_live_readback_refresh_monotonic = refresh_time
        self._sync_status_to_gui(sync_manual_panel=True)

    def loadOperatingConfigNowFromThread(self, parallel: bool = True) -> None:
        if parallel:
            Thread(
                target=self.loadOperatingConfigNow,
                name=f"{self.controllerParent.name} loadOperatingConfigThread",
                daemon=True,
            ).start()
            return
        self.loadOperatingConfigNow()

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
                f"Cannot load {self.controllerParent.name} config while the PSU is OFF.",
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

        self._discard_pending_manual_state_apply()
        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        try:
            with self._controller_lock_section(
                "Could not acquire lock to load the PSU config."
            ):
                device = self.device
                if device is None:
                    return
                device.load_config(config_index, timeout_s=timeout_s)
            time.sleep(0.3)
            self._update_state()
            self._set_loaded_config_text(self._format_loaded_config_text(config_index))
            sync_manual = getattr(
                self.controllerParent,
                "_sync_manual_panel_from_controller",
                None,
            )
            if callable(sync_manual):
                sync_manual()
            self.print(f"Loaded PSU config {config_index}.")
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to load PSU config {config_index}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._sync_status_to_gui(sync_manual_panel=True)
        if self.values is None:
            return

        self._sync_status_to_gui(sync_manual_panel=True)
        for channel in self.controllerParent.getChannels():
            channel_no = channel.channel_number()
            if channel.real:
                channel.monitor = self.values.get(channel_no, np.nan)
                channel.setCurrentMonitorText(
                    _format_current_text(self.current_values.get(channel_no, np.nan))
                )
                channel.setOutputStateText(
                    "ON" if self.output_enabled_by_channel.get(channel_no, False) else "OFF"
                )
                channel.setVoltageSetText(self.voltage_setpoints.get(channel_no, "n/a"))
                channel.setCurrentSetText(self.current_setpoints.get(channel_no, "n/a"))
                channel._set_parameter_value_without_events(
                    channel.ENABLED,
                    self.output_enabled_by_channel.get(channel_no, False),
                )
                style_setter = getattr(channel, "_set_parameter_widget_style", None)
                if callable(style_setter):
                    output_enabled = self.output_enabled_by_channel.get(channel_no, False)
                    style_setter(
                        getattr(channel, "MONITOR", "Monitor"),
                        _psu_feedback_style(
                            _voltage_feedback_state(
                                enabled=output_enabled,
                                measured_v=self.values.get(channel_no, np.nan),
                                set_v=self.voltage_setpoint_values.get(
                                    channel_no,
                                    np.nan,
                                ),
                            )
                        ),
                    )
                    style_setter(
                        channel.CURRENT_MONITOR,
                        _psu_feedback_style(
                            _current_limit_feedback_state(
                                enabled=output_enabled,
                                measured_a=self.current_values.get(channel_no, np.nan),
                                limit_a=self.current_limit_values.get(
                                    channel_no,
                                    np.nan,
                                ),
                                current_limit_active=self.current_limit_active,
                            )
                        ),
                    )
                continue
            channel.monitor = np.nan
            channel.setCurrentMonitorText("n/a")
            channel.setOutputStateText("n/a")
            channel.setVoltageSetText("n/a")
            channel.setCurrentSetText("n/a")
            channel._set_parameter_value_without_events(channel.ENABLED, False)

    def applyManualStateFromThread(self, manual_state: dict[str, Any], parallel: bool = True) -> None:
        if parallel:
            self._queue_manual_state_apply(manual_state)
            return
        self.applyManualState(manual_state)

    def _copy_manual_state(self, manual_state: dict[str, Any]) -> dict[str, Any]:
        copied: dict[str, Any] = {}
        for key, value in manual_state.items():
            copied[key] = dict(value) if isinstance(value, dict) else value
        return copied

    def _queue_manual_state_apply(self, manual_state: dict[str, Any]) -> None:
        with self._manual_apply_state_lock:
            self._manual_apply_pending_state = self._copy_manual_state(manual_state)
            if self._manual_apply_worker_running:
                return
            self._manual_apply_worker_running = True
            Thread(
                target=self._manual_state_apply_worker,
                name=f"{self.controllerParent.name} applyManualStateThread",
                daemon=True,
            ).start()

    def _discard_pending_manual_state_apply(self) -> None:
        with self._manual_apply_state_lock:
            self._manual_apply_pending_state = None

    def _manual_state_apply_worker(self) -> None:
        while True:
            with self._manual_apply_state_lock:
                manual_state = self._manual_apply_pending_state
                self._manual_apply_pending_state = None
                if manual_state is None:
                    self._manual_apply_worker_running = False
                    return
            self.applyManualState(manual_state)

    def applyManualState(self, manual_state: dict[str, Any]) -> None:
        device = self.device
        if device is None or not getattr(self, "initialized", False):
            self.print(
                f"Cannot apply {self.controllerParent.name} manual values: communication not initialized.",
                flag=PRINT.WARNING,
            )
            return

        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        output_enabled = tuple(
            bool((manual_state.get("output_enabled", {}) or {}).get(channel_index, False))
            for channel_index in _PSU_CHANNEL_IDS
        )
        full_range_enabled = tuple(
            bool((manual_state.get("full_range_enabled", {}) or {}).get(channel_index, False))
            for channel_index in _PSU_CHANNEL_IDS
        )
        voltage_values = manual_state.get("voltage_values", {}) or {}
        current_limit_values = manual_state.get("current_limit_values", {}) or {}
        try:
            with self._controller_lock_section(
                "Could not acquire lock to apply PSU manual values."
            ):
                device = self.device
                if device is None:
                    return
                device.set_output_enabled(False, False, timeout_s=timeout_s)
                set_full_range = getattr(device, "set_output_full_range", None)
                if callable(set_full_range):
                    set_full_range(
                        full_range_enabled[0],
                        full_range_enabled[1],
                        timeout_s=timeout_s,
                    )
                for channel_index in _PSU_CHANNEL_IDS:
                    device.set_channel_voltage(
                        channel_index,
                        _coerce_float(voltage_values.get(channel_index), 0.0),
                        timeout_s=timeout_s,
                    )
                    device.set_channel_current(
                        channel_index,
                        _coerce_float(current_limit_values.get(channel_index), 0.0),
                        timeout_s=timeout_s,
                    )
                readback_warnings = self._verify_manual_state_unlocked(
                    device=device,
                    voltage_values=voltage_values,
                    current_limit_values=current_limit_values,
                    full_range_enabled=full_range_enabled,
                    timeout_s=timeout_s,
                )
                any_output_enabled = any(output_enabled)
                device.set_device_enabled(any_output_enabled, timeout_s=timeout_s)
                if any_output_enabled:
                    device.set_output_enabled(
                        output_enabled[0],
                        output_enabled[1],
                        timeout_s=timeout_s,
                    )
                self._verify_output_enable_state_unlocked(
                    device=device,
                    any_output_enabled=any_output_enabled,
                    output_enabled=output_enabled,
                    timeout_s=timeout_s,
                )
            self._update_state()
            self._set_loaded_config_text("Manual (unsaved)")
            sync_manual = getattr(
                self.controllerParent,
                "_sync_manual_panel_from_controller",
                None,
            )
            if callable(sync_manual):
                sync_manual()
            if readback_warnings:
                self.print(
                    "Applied PSU manual values with setpoint readback warning(s): "
                    + " ".join(readback_warnings),
                    flag=PRINT.WARNING,
                )
            self.print("Applied PSU manual values.")
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self._safe_disable_outputs_after_failure(timeout_s=timeout_s)
            with contextlib.suppress(Exception):
                self._update_state()
            self.print(
                f"Failed to apply PSU manual values: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._sync_status_to_gui()

    def saveCurrentConfigFromThread(
        self,
        config_index: int,
        *,
        config_name: str | None = None,
        active: bool = True,
        valid: bool = True,
        parallel: bool = True,
    ) -> None:
        if parallel:
            Thread(
                target=self.saveCurrentConfig,
                kwargs={
                    "config_index": config_index,
                    "config_name": config_name,
                    "active": active,
                    "valid": valid,
                },
                name=f"{self.controllerParent.name} saveConfigThread",
                daemon=True,
            ).start()
            return
        self.saveCurrentConfig(
            config_index=config_index,
            config_name=config_name,
            active=active,
            valid=valid,
        )

    def saveCurrentConfig(
        self,
        *,
        config_index: int,
        config_name: str | None = None,
        active: bool = True,
        valid: bool = True,
    ) -> None:
        device = self.device
        if device is None or not getattr(self, "initialized", False):
            self.print(
                f"Cannot save {self.controllerParent.name} config: communication not initialized.",
                flag=PRINT.WARNING,
            )
            return

        save_config = getattr(device, "save_config", None)
        if not callable(save_config):
            self.print(
                f"Cannot save {self.controllerParent.name} config: runtime does not expose save_config().",
                flag=PRINT.WARNING,
            )
            return

        if not callable(getattr(device, "list_configs", None)):
            self.print(
                f"Cannot save {self.controllerParent.name} config: runtime does not expose list_configs(), "
                "so overwrite-safe saving is unavailable.",
                flag=PRINT.WARNING,
            )
            return
        self._refresh_available_configs()
        if str(getattr(self, "available_configs_text", "") or "") == "Unavailable":
            self.print(
                f"Cannot save {self.controllerParent.name} config: existing config list is unavailable.",
                flag=PRINT.WARNING,
            )
            return
        if self._config_slot_exists(config_index):
            self.print(
                f"Cannot save {self.controllerParent.name} config {config_index}: "
                "this slot already exists. Choose an empty slot.",
                flag=PRINT.WARNING,
            )
            self._sync_status_to_gui()
            return

        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        try:
            with self._controller_lock_section(
                "Could not acquire lock to save the PSU config."
            ):
                device = self.device
                if device is None:
                    return
                save_config(
                    config_index,
                    name=config_name,
                    active=active,
                    valid=valid,
                    timeout_s=timeout_s,
                )
            self._refresh_available_configs()
            self._set_loaded_config_text(self._format_loaded_config_text(config_index))
            self._sync_status_to_gui()
            self.print(f"Saved PSU config {config_index}.")
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to save PSU config {config_index}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._sync_status_to_gui()

    def toggleOn(self) -> None:
        target_on = bool(getattr(self.controllerParent, "isOn", lambda: False)())
        device = self.device
        if device is None:
            self._end_transition()
            return

        self._discard_pending_manual_state_apply()
        stop_acquisition = getattr(self, "stopAcquisition", None)
        if callable(stop_acquisition):
            stop_acquisition()
            self.acquiring = False

        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))

        try:
            if target_on:
                with self._controller_lock_section(
                    "Could not acquire lock to start the PSU."
                ):
                    device = self.device
                    if device is None:
                        self._restore_off_ui_state()
                        return
                    startup_kwargs = self._startup_kwargs()
                    if startup_kwargs:
                        device.initialize(timeout_s=timeout_s, **startup_kwargs)
                        loaded_config = _coerce_int(
                            startup_kwargs.get("operating_config"),
                            _coerce_int(startup_kwargs.get("standby_config"), -1),
                        )
                        if loaded_config >= 0:
                            self._set_loaded_config_text(
                                self._format_loaded_config_text(loaded_config)
                            )
                        message = "PSU startup sequence completed from controller configs."
                    else:
                        self._start_manual_mode(timeout_s=timeout_s)
                        message = (
                            "PSU communication initialized without a startup config. "
                            "Outputs remain OFF until manual values are applied."
                        )
                self._update_state()
                sync_manual = getattr(
                    self.controllerParent,
                    "_sync_manual_panel_from_controller",
                    None,
                )
                if callable(sync_manual):
                    sync_manual()
                start_acquisition = getattr(self, "startAcquisition", None)
                if callable(start_acquisition):
                    start_acquisition()
                self.print(message)
            else:
                shutdown_confirmed = self.shutdownCommunication()
                if not shutdown_confirmed:
                    self._restore_on_ui_state()
                return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            if target_on:
                self._restore_off_ui_state()
            self.print(
                f"Failed to toggle PSU: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._end_transition()
            self._sync_status_to_gui()

    def shutdownCommunication(self) -> bool:
        device = self.device
        if device is None:
            self.closeCommunication()
            return True

        self._discard_pending_manual_state_apply()
        stop_acquisition = getattr(self, "stopAcquisition", None)
        if callable(stop_acquisition):
            stop_acquisition()
            self.acquiring = False
        self.print("Starting PSU shutdown sequence.")
        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        shutdown_errors: list[str] = []
        shutdown_confirmed = False
        confirmation_reason = "shutdown confirmation was not completed"
        try:
            with self._controller_lock_section(
                "Could not acquire lock to shut down the PSU."
            ):
                shutdown_errors = self._perform_shutdown_sequence_unlocked(
                    timeout_s=timeout_s
                )
                shutdown_confirmed, confirmation_reason = self._confirm_shutdown_unlocked(
                    timeout_s=float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
                )
        except TimeoutError:
            confirmation_reason = "controller lock timeout during shutdown"
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            confirmation_reason = self._format_exception(exc)
            self.print(
                f"PSU shutdown failed: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        if shutdown_errors:
            self.errorCount += 1
            self.print(
                "PSU shutdown sequence reported errors: " + "; ".join(shutdown_errors),
                flag=PRINT.ERROR,
            )
        if shutdown_confirmed:
            self.print("PSU shutdown sequence completed.")
        else:
            self.errorCount += 1
            self.print(
                "PSU shutdown could not be confirmed before disconnect: "
                f"{confirmation_reason}.",
                flag=PRINT.ERROR,
            )
        self.closeCommunication(
            final_state=(
                "Disconnected"
                if shutdown_confirmed
                else _PSU_SHUTDOWN_UNCONFIRMED_STATE
            )
        )
        return shutdown_confirmed

    def closeCommunication(self, *, final_state: str | None = None) -> None:
        base_close = getattr(super(), "closeCommunication", None)
        if callable(base_close):
            base_close()
        resolved_final_state = str(final_state or "Disconnected")
        is_disconnected = resolved_final_state == "Disconnected"
        self.main_state = resolved_final_state
        self.hardware_main_state = (
            "Disconnected" if is_disconnected else resolved_final_state
        )
        self.output_state_summary = (
            "CH0=OFF, CH1=OFF" if is_disconnected else "Unknown"
        )
        self.device_state_summary = "n/a" if is_disconnected else "Unknown"
        self.available_configs = []
        self.available_configs_text = "n/a"
        self._set_loaded_config_text("n/a")
        self.initializeValues(reset=True)
        self._sync_status_to_gui()
        self._dispose_device()
        self.initialized = False

    def _update_state(self) -> None:
        device = self.device
        if device is None:
            self.main_state = "Disconnected"
            self.hardware_main_state = "Disconnected"
            self.output_state_summary = "CH0=OFF, CH1=OFF"
            self.device_state_summary = "n/a"
            return

        try:
            timeout_s = float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
            snapshot = device.collect_housekeeping(
                timeout_s=timeout_s
            )
        except Exception:
            try:
                self.hardware_main_state = str(
                    device.get_status().get("connected", False)
                )
            except Exception:
                self.hardware_main_state = "Unknown"
            self.main_state = _normalize_runtime_state(self.hardware_main_state)
            self.device_state_summary = "Unknown"
            self.output_state_summary = "Unknown"
            return

        self._apply_snapshot(snapshot)
        live_readbacks = None
        with contextlib.suppress(Exception):
            live_readbacks = self._read_live_readbacks(timeout_s=timeout_s)
        if live_readbacks is not None:
            self._apply_live_readbacks(live_readbacks)

    def _sync_status_to_gui(self, *, sync_manual_panel: bool = False) -> None:
        self.controllerParent.main_state = self.main_state
        self.controllerParent.hardware_main_state = self.hardware_main_state
        self.controllerParent.output_summary = self.output_state_summary
        self.controllerParent.available_configs = list(self.available_configs)
        self.controllerParent.available_configs_text = self.available_configs_text
        self.controllerParent.loaded_state_text = self.loaded_state_text
        def _refresh_gui() -> None:
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
            else:
                update_config_selectors = getattr(
                    self.controllerParent,
                    "_update_config_selectors",
                    None,
                )
                if callable(update_config_selectors):
                    update_config_selectors()
            update_status_widgets = getattr(self.controllerParent, "_update_status_widgets", None)
            if callable(update_status_widgets):
                update_status_widgets()
            if sync_manual_panel:
                sync_manual = getattr(
                    self.controllerParent,
                    "_sync_manual_panel_from_controller",
                    None,
                )
                if callable(sync_manual):
                    sync_manual()

        _invoke_gui_callback(_refresh_gui)

    def _transition_guard(self) -> Lock:
        lock = getattr(self, "_transition_lock", None)
        if lock is None:
            lock = Lock()
            self._transition_lock = lock
        return lock

    def _dispose_device(self) -> None:
        device = self.device
        self.device = None
        self.initialized = False
        if device is None:
            return
        try:
            device.disconnect()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                device.close()

    def _restore_off_ui_state(self) -> None:
        sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_on_state):
            sync_on_state(False)
            return
        if hasattr(self.controllerParent, "onAction"):
            self.controllerParent.onAction.state = False

    def _restore_on_ui_state(self) -> None:
        """Restore toolbar ON/OFF widgets back to ON after a failed shutdown."""
        sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_on_state):
            sync_on_state(True)
            return
        if hasattr(self.controllerParent, "onAction"):
            self.controllerParent.onAction.state = True

    @contextlib.contextmanager
    def _controller_lock_section(
        self,
        timeout_message: str,
        *,
        already_acquired: bool = False,
    ):
        acquire = getattr(self.lock, "acquire", None)
        release = getattr(self.lock, "release", None)
        if callable(acquire) and callable(release):
            if already_acquired:
                yield
                return
            if not acquire(timeout=1):
                self.print(timeout_message, flag=PRINT.ERROR)
                raise TimeoutError(timeout_message)
            try:
                yield
            finally:
                release()
            return

        acquire_timeout = getattr(self.lock, "acquire_timeout", None)
        if callable(acquire_timeout):
            with acquire_timeout(
                1,
                timeoutMessage=timeout_message,
                already_acquired=already_acquired,
            ) as lock_acquired:
                if not lock_acquired:
                    raise TimeoutError(timeout_message)
                yield
            return

        raise TypeError(
            "PSU controller lock must provide either acquire_timeout() or acquire()/release()."
        )

    def _begin_transition(self, target_on: bool) -> bool:
        with self._transition_guard():
            if self.transitioning:
                return False
            self.transitioning = True
            self.transition_target_on = bool(target_on)
            return True

    def _end_transition(self) -> None:
        with self._transition_guard():
            self.transitioning = False
            self.transition_target_on = None

    def _format_exception(self, exc: Exception) -> str:
        message = str(exc).strip()
        if message:
            return f"{type(exc).__name__}: {message}"
        return repr(exc)
