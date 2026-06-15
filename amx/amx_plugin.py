"""Drive AMX timing from ESIBD Explorer and monitor pulser readbacks."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
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
_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_amx_runtime"
_AMX_DRIVER_CLASS: type[Any] | None = None
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
_AMX_MAX_CONSECUTIVE_POLL_ERRORS = 10
_AMX_FREQUENCY_SPINBOX_STYLE = (
    "QDoubleSpinBox {"
    " background-color: #0f172a;"
    " color: #f8fafc;"
    " border: 1px solid #64748b;"
    " border-radius: 4px;"
    " padding: 2px 20px 2px 6px;"
    " selection-background-color: #2563eb;"
    "}"
    "QDoubleSpinBox:disabled {"
    " background-color: #1f2937;"
    " color: #94a3b8;"
    " border-color: #475569;"
    "}"
    "QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {"
    " background-color: #475569;"
    " border: 1px solid #94a3b8;"
    " width: 18px;"
    "}"
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


def _safe_device_attr(device: Any, name: str, default: Any) -> Any:
    """Read a device attribute with fallback, handling process-proxy RuntimeError."""
    try:
        return getattr(device, name, default)
    except RuntimeError:
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
            f"Could not create an import spec for bundled AMX runtime at {package_dir}."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise


def _get_amx_driver_class() -> type[Any]:
    global _AMX_DRIVER_CLASS

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


def providePlugins() -> "list[type[Plugin]]":
    return [AMXDevice]


class AMXDevice(Device):
    """Drive AMX oscillator and pulser timing from ESIBD Explorer."""

    documentation = (
        "Drives AMX frequency and pulser timing while monitoring pulser readbacks."
    )

    name = "AMX"
    version = "0.1.0"
    supportedVersion = "1.0.1"
    pluginType = PLUGINTYPE.INPUTDEVICE
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
        if hasattr(self, "advancedAction"):
            self.advancedAction.toolTipFalse = (
                f"Show expert columns and channel layout actions for {self.name}."
            )
            self.advancedAction.toolTipTrue = (
                f"Hide expert columns and channel layout actions for {self.name}."
            )
            self.advancedAction.setToolTip(self.advancedAction.toolTipFalse)
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_config_controls()
        self._update_channel_column_visibility()
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
    main_state: str
    device_enabled_state: str
    available_configs: list[dict[str, Any]]
    available_configs_text: str
    loaded_config_text: str

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
        lines.append("Choose the saved AMX signal/routing shape to load.")
        lines.append("Available AMX configs:")
        available = str(getattr(self, "available_configs_text", "") or "n/a")
        for entry in available.split(";"):
            entry = entry.strip()
            if entry:
                lines.append(f"- {entry}")
        return "\n".join(lines)

    def _loaded_config_tooltip_text(self) -> str:
        lines = [
            "AMX config currently reported in controller memory.",
            f"Loaded: {getattr(self, 'loaded_config_text', '') or 'n/a'}",
        ]
        available = str(getattr(self, "available_configs_text", "") or "").strip()
        if available:
            lines.append(f"Available: {available}")
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
        widget.setRange(0.001, 10000.0)
        widget.setDecimals(3)
        widget.setSingleStep(0.1)
        widget.setSuffix(" kHz")
        widget.setMinimumWidth(112)
        widget.setAccelerated(True)
        widget.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.PlusMinus)
        widget.setStyleSheet(_AMX_FREQUENCY_SPINBOX_STYLE)
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

    def _ensure_config_controls(self) -> None:
        if (
            getattr(self, "titleBar", None) is None
            or getattr(self, "titleBarLabel", None) is None
            or hasattr(self, "operatingConfigCombo")
        ):
            return

        label_type = type(self.titleBarLabel)
        insert_before = getattr(self, "stretchAction", None)

        self.loadedConfigLabel = label_type("Loaded:")
        self.loadedConfigValueLabel = label_type("")
        self.operatingConfigLabel = label_type("Signal:")
        self.operatingConfigCombo = self._create_config_selector_widget()
        self.loadOperatingConfigButton = self._create_config_button_widget("Load now")
        self.frequencyLabel = label_type("Freq:")
        self.frequencyWidget = self._create_frequency_widget()

        self._connect_config_selector(self.operatingConfigCombo, "operating_config")
        self._connect_config_button(self.loadOperatingConfigButton, self.loadOperatingConfigNow)
        self._connect_frequency_widget(self.frequencyWidget)

        widgets = (
            self.loadedConfigLabel,
            self.loadedConfigValueLabel,
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

    def _update_config_selector(self, combo: Any | None, attr_name: str) -> None:
        if combo is None:
            return
        block_signals = getattr(combo, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            self._combo_clear(combo)
            for value, label in self._config_selector_entries(attr_name):
                self._combo_add_item(combo, label, value)
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

    def _update_config_controls(self) -> None:
        self._update_config_selector(getattr(self, "standbyConfigCombo", None), "standby_config")
        self._update_config_selector(getattr(self, "operatingConfigCombo", None), "operating_config")
        signal_label = getattr(self, "operatingConfigLabel", None)
        signal_tooltip = self._config_selector_tooltip_text("operating_config")
        if signal_label is not None and hasattr(signal_label, "setToolTip"):
            signal_label.setToolTip(signal_tooltip)

        loaded_text = str(getattr(self, "loaded_config_text", "") or "n/a")
        loaded_tooltip = self._loaded_config_tooltip_text()
        loaded_value = getattr(self, "loadedConfigValueLabel", None)
        if loaded_value is not None and hasattr(loaded_value, "setText"):
            loaded_value.setText(loaded_text)
        if loaded_value is not None and hasattr(loaded_value, "setToolTip"):
            loaded_value.setToolTip(loaded_tooltip)
        loaded_label = getattr(self, "loadedConfigLabel", None)
        if loaded_label is not None and hasattr(loaded_label, "setToolTip"):
            loaded_label.setToolTip(loaded_tooltip)

        button = getattr(self, "loadOperatingConfigButton", None)
        ready, reason = self._load_operating_now_ready()
        self._set_action_enabled(button, ready)
        if button is not None and hasattr(button, "setToolTip"):
            tooltip = (
                "Load the selected AMX config immediately and reapply runtime timing. "
                "This action is only available while the AMX is ON."
            )
            if not ready and reason:
                tooltip = f"{tooltip}\nCurrently unavailable: {reason}."
            button.setToolTip(tooltip)

        frequency_widget = getattr(self, "frequencyWidget", None)
        if frequency_widget is not None:
            block_signals = getattr(frequency_widget, "blockSignals", None)
            if callable(block_signals):
                block_signals(True)
            try:
                set_value = getattr(frequency_widget, "setValue", None)
                if callable(set_value):
                    set_value(float(getattr(self, "frequency_khz", 2.0)))
            finally:
                if callable(block_signals):
                    block_signals(False)
            frequency_tooltip = (
                "Oscillator frequency applied to the selected AMX signal. "
                "Changes are applied immediately while the AMX is ON."
            )
            if hasattr(frequency_widget, "setToolTip"):
                frequency_widget.setToolTip(frequency_tooltip)
            frequency_label = getattr(self, "frequencyLabel", None)
            if frequency_label is not None and hasattr(frequency_label, "setToolTip"):
                frequency_label.setToolTip(frequency_tooltip)

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
        if self._set_config_setting_value(attr_name, self._combo_current_value(combo)):
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
        action = getattr(self, "deviceOnAction", None)
        if action is None:
            return
        action.blockSignals(True)
        try:
            action.state = self.isOn()
        finally:
            action.blockSignals(False)

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
        loaded = _compact_status_text(
            getattr(self, "loaded_config_text", None),
            default="n/a",
        )
        return f"Device: {device_enabled} | Faults: {faults} | Loaded: {loaded}"

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

    def _update_status_widgets(self) -> None:
        """Refresh the global AMX status labels in the toolbar."""
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        self._sync_acquisition_controls()
        if badge is None or summary is None:
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
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)

    def toggleAdvanced(self, advanced: "bool | None" = False) -> None:
        if self.channels:
            super().toggleAdvanced(advanced=advanced)
            for channel in self.getChannels():
                channel.setHidden(False)
            self._update_channel_column_visibility()
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

    def estimateStorage(self) -> None:
        if self.channels:
            super().estimateStorage()
            return

        self.maxDataPoints = 0
        widget = self.pluginManager.Settings.settings[
            f"{self.name}/{self.MAXDATAPOINTS}"
        ].getWidget()
        if widget:
            widget.setToolTip(
                "Storage estimate will be available after the first successful "
                "AMX hardware initialization."
            )

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
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
            toolTip="Oscillator frequency applied in kilohertz after startup.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="frequency_khz",
            event=self.frequencyChanged,
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

    def frequencyChanged(self, *, debounce: bool = False) -> None:
        self._update_width_bounds()
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
                channel.value = max_width_us
            channel._update_duty_label()

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
        if not _state_is_on(main_state):
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
        shutdown_confirmed = True
        controller = getattr(self, "controller", None)
        if controller:
            shutdown_confirmed = bool(controller.shutdownCommunication())
        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False if shutdown_confirmed else True
            self._sync_local_on_action()
        if not shutdown_confirmed:
            self.print(
                "AMX shutdown could not be confirmed; UI remains ON until "
                "the hardware state is verified.",
                flag=PRINT.WARNING,
            )
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


class AMXChannel(Channel):
    """AMX pulser channel definition."""

    ID = "Pulser"
    DELAY_US = "Delay us"
    WIDTH_TICKS = "Width ticks"
    BURST = "Burst"
    DUTY = "Duty"
    FREQ_KHZ = "Freq kHz"
    channelParent: AMXDevice

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
            self.DUTY, self.FREQ_KHZ, self.ID,
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
        self._disable_value_wheel()
        self.scalingChanged()

    def _disable_value_wheel(self) -> None:
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

    def valueChanged(self) -> None:
        base_handler = getattr(super(), "valueChanged", None)
        if callable(base_handler):
            base_handler()
        self._update_duty_label()
        self._sync_monitor_feedback()
        if not getattr(self.channelParent, "loading", False):
            self._schedule_value_apply()

    def enabledChanged(self) -> None:
        base_handler = getattr(super(), "enabledChanged", None)
        if callable(base_handler):
            base_handler()
        if not getattr(self, "enabled", False):
            self.monitor = np.nan
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        self._cancel_value_apply()
        self._apply_value_now()

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
        """Recalculate Duty (%) from current Width (us) and device frequency."""
        freq_khz = float(getattr(self.channelParent, "frequency_khz", 2.0))
        period_us = 1000.0 / freq_khz if freq_khz > 0 else 0.0
        width_us = _coerce_float(getattr(self, "value", 0.0), 0.0)
        if period_us > 0 and width_us > 0:
            self.setDutyText(f"{width_us / period_us * 100:.1f}")
        else:
            self.setDutyText("0.0")

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


class AMXController(DeviceController):
    """AMX hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: AMXDevice

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
        self.values: dict[int, float] = {}
        self.width_values: dict[int, str] = {}
        self.delay_values: dict[int, str] = {}
        self.burst_values: dict[int, str] = {}

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

    def runInitialization(self) -> None:
        self.initialized = False
        self._dispose_device()
        try:
            self._initialize_transport_session()
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:  # noqa: BLE001
            self._restore_off_ui_state()
            self.print(
                f"AMX initialization failed on COM{int(self.controllerParent.com)}: "
                f"{self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
            self._dispose_device()
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        self._finalize_transport_initialization()
        self._resume_pending_on_request_after_transport_ready()

    def _initialize_transport_session(self) -> None:
        """Create the AMX driver and open transport-only communication."""
        driver_class = _get_amx_driver_class()
        self.device = driver_class(
            device_id=f"{self.controllerParent.name.lower()}_com{int(self.controllerParent.com)}",
            com=int(self.controllerParent.com),
            baudrate=int(self.controllerParent.baudrate),
        )
        backend_reason = str(
            getattr(self.device, "_process_backend_disabled_reason", "")
        ).strip()
        if backend_reason:
            self.print(backend_reason, flag=PRINT.WARNING)
        self.device.connect(timeout_s=float(self.controllerParent.connect_timeout_s))
        self._refresh_available_configs()
        self._refresh_loaded_config_status()
        self._update_state()

    def _finalize_transport_initialization(self) -> None:
        """Sync transport state to the GUI after communication becomes ready."""
        if self.device is not None:
            self.controllerParent._sync_channels()
        self.initializeValues(reset=True)
        self.initialized = True
        self._consecutive_poll_errors = 0
        self.super_init_complete_called = True
        self._sync_status_to_gui()

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
        if self.transitioning or not self._begin_transition(True):
            return
        toggle_thread = getattr(self, "toggleOnFromThread", None)
        if callable(toggle_thread):
            toggle_thread(parallel=True)
            return
        self.toggleOn()

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
            return

        get_status = getattr(device, "get_status", None)
        if not callable(get_status):
            self.loaded_config_text = "Unavailable"
            return

        try:
            status = get_status()
        except Exception as exc:  # noqa: BLE001
            self.loaded_config_text = "Unavailable"
            self.print(
                f"Could not read loaded AMX config: {self._format_exception(exc)}",
                flag=PRINT.WARNING,
            )
            return

        self.loaded_config_text = _format_loaded_config_text(status)

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
            if self._config_entry_is_standby_like(entry):
                return False, f"config {config_index} is a standby slot", config_index
        elif self.available_configs:
            return (
                False,
                f"config {config_index} is not reported by the controller",
                config_index,
            )

        return True, "", config_index

    def _apply_runtime_settings(self, timeout_s: float) -> None:
        device = self.device
        if device is None:
            return
        device.set_frequency_khz(
            float(getattr(self.controllerParent, "frequency_khz", 2.0)),
            timeout_s=timeout_s,
        )
        for channel in self.controllerParent.getChannels():
            self._apply_channel_timing(channel, timeout_s)

    def _startup_snapshot_timeout_s(self) -> float:
        return float(getattr(self.controllerParent, "poll_timeout_s", 5.0))

    def _collect_startup_snapshot(self) -> dict[str, Any]:
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")
        return device.collect_housekeeping(timeout_s=self._startup_snapshot_timeout_s())

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
        deadline = time.monotonic() + max(float(settle_timeout_s), 0.1)
        latest_snapshot: dict[str, Any] | None = None
        while True:
            snapshot = self._collect_startup_snapshot()
            latest_snapshot = snapshot
            self._apply_snapshot(snapshot)
            if self._startup_snapshot_ready(snapshot):
                return snapshot
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    self._startup_failure_message(
                        config_index=config_index,
                        snapshot=latest_snapshot,
                    )
                )
            time.sleep(0.1)

    def _load_operating_config_and_enable_device(
        self,
        *,
        config_index: int,
        timeout_s: float,
        load_config_first: bool,
    ) -> dict[str, Any]:
        device = self.device
        if device is None:
            raise RuntimeError("AMX device disconnected.")

        if load_config_first:
            device.load_config(config_index, timeout_s=timeout_s)
            self._refresh_loaded_config_status()
        self._apply_runtime_settings(timeout_s)
        device.set_device_enabled(True, timeout_s=timeout_s)
        try:
            return self._wait_for_startup_ready_snapshot(
                config_index=config_index,
                settle_timeout_s=timeout_s,
            )
        except Exception:
            with contextlib.suppress(Exception):
                device.set_device_enabled(False, timeout_s=timeout_s)
            raise

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
        connect(timeout_s=timeout_s)
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
    ) -> None:
        """Load the selected operating config, apply runtime settings, and enable AMX."""
        with self._controller_lock_section(lock_message):
            self._ensure_transport_connected(timeout_s)
            self._load_operating_config_and_enable_device(
                config_index=config_index,
                timeout_s=timeout_s,
                load_config_first=True,
            )
        self._update_state()
        if restart_acquisition:
            start_acquisition = getattr(self, "startAcquisition", None)
            if callable(start_acquisition):
                start_acquisition()
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

        self._discard_pending_runtime_applies()
        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))
        try:
            self._start_operating_mode(
                config_index=config_index,
                timeout_s=timeout_s,
                lock_message="Could not acquire lock to load the AMX config.",
                success_message=f"Loaded AMX config {config_index}.",
            )
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to load AMX config {config_index}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._sync_status_to_gui()

    def _apply_channel_timing(self, channel: AMXChannel, timeout_s: float) -> None:
        device = self.device
        if device is None or not channel.real:
            return

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
            device.set_pulser_width_ticks(pulser, width_ticks, timeout_s=timeout_s)
        else:
            device.set_pulser_width_ticks(pulser, 0, timeout_s=timeout_s)

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
                if device is None:
                    return
                device.set_frequency_khz(
                    float(getattr(self.controllerParent, "frequency_khz", 2.0)),
                    timeout_s=timeout_s,
                )
                for channel in self.controllerParent.getChannels():
                    self._apply_channel_timing(channel, timeout_s)
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to apply AMX global settings: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )

    def applyGlobalSettingsFromThread(self, parallel: bool = True) -> None:
        if parallel:
            self._queue_global_settings_apply()
            return
        self.applyGlobalSettings()

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

    def applyValueFromThread(self, channel: AMXChannel, parallel: bool = True) -> None:
        if parallel:
            self._queue_channel_timing_apply(channel)
            return
        self.applyValue(channel)

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
        with self._global_apply_state_lock:
            self._global_apply_pending = False
        with self._channel_apply_state_lock:
            self._channel_apply_pending.clear()

    def runAcquisition(self) -> None:
        """Poll AMX housekeeping and push readbacks to the GUI from the acquisition thread.

        The framework runs this loop in ``acquisitionThread`` and expects
        ``readNumbers`` to perform the hardware read. The controller lock is not
        reentrant, so we acquire it once here and forward ``already_acquired=True``;
        without that, every poll silently aborted on the held lock and left the duty
        monitors and the status badge frozen at their startup snapshot.
        """
        while self.acquiring:
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
            time.sleep(self.controllerParent.interval / 1000)

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
                snapshot = device.collect_housekeeping(timeout_s=timeout_s)
                self._apply_snapshot(snapshot)
                self._consecutive_poll_errors = 0
        except TimeoutError as exc:
            if str(exc) == lock_timeout_message:
                return
            self._poll_error("Timed out while polling AMX housekeeping.", exc=None)
            return
        except Exception as exc:  # noqa: BLE001
            self._poll_error(
                f"Failed to read AMX housekeeping: {self._format_exception(exc)}",
                exc=exc,
            )
            return

    def _poll_error(self, message: str, *, exc: Exception | None) -> None:
        self.errorCount += 1
        self._consecutive_poll_errors += 1
        self.print(message, flag=PRINT.ERROR)
        self.initializeValues(reset=True)
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

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        device = self.device
        self.main_state = str(snapshot.get("main_state", {}).get("name", "Unknown"))
        self.device_enabled_state = (
            "ON" if bool(snapshot.get("device_enabled", False)) else "OFF"
        )
        flags = snapshot.get("device_state", {}).get("flags", [])
        self.device_state_summary = ", ".join(str(flag) for flag in flags) if flags else "OK"
        controller_flags = snapshot.get("controller_state", {}).get("flags", [])
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
        ticks_per_us = _safe_device_attr(device, "CLOCK", 100e6) / 1e6

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

        self.values = new_values
        self.width_values = new_width_ticks
        self.delay_values = new_delay_us
        self.burst_values = new_bursts
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
                if device is None:
                    return
                self._apply_channel_timing(channel, timeout_s)
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to apply AMX P{channel.pulser_number()}: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )

    def updateValues(self) -> None:
        if self.values is None:
            return

        self._sync_status_to_gui()
        device_is_on = getattr(self.controllerParent, "isOn", lambda: False)()
        freq_khz = float(getattr(self.controllerParent, "frequency_khz", 2.0))
        period_us = 1000.0 / freq_khz if freq_khz > 0 else 0.0

        for channel in self.controllerParent.getChannels():
            pulser = channel.pulser_number()
            if channel.real and device_is_on:
                channel.monitor = self.values.get(pulser, np.nan)
                channel.setWidthText(self.width_values.get(pulser, "n/a"))
                channel.setDelayText(self.delay_values.get(pulser, "n/a"))
                channel.setBurstText(self.burst_values.get(pulser, "n/a"))
                # Software-calculated duty from user width setting.
                width_us = _coerce_float(getattr(channel, "value", 0.0), 0.0)
                if period_us > 0 and width_us > 0:
                    channel.setDutyText(f"{width_us / period_us * 100:.1f}")
                else:
                    channel.setDutyText("0.0")
                channel.setFreqText(f"{freq_khz:.1f}")
            else:
                channel.monitor = np.nan
                channel.setWidthText("n/a")
                channel.setDelayText("n/a")
                channel.setBurstText("n/a")
                channel.setDutyText("n/a")
                channel.setFreqText("n/a")

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
        stop_acquisition = getattr(self, "stopAcquisition", None)
        if callable(stop_acquisition):
            stop_acquisition()
            self.acquiring = False

        timeout_s = float(getattr(self.controllerParent, "startup_timeout_s", 10.0))

        try:
            if target_on:
                ready, reason, config_index = self._operating_config_ready()
                if not ready:
                    self._restore_off_ui_state()
                    self.print(
                        f"Cannot start {self.controllerParent.name}: {reason}.",
                        flag=PRINT.WARNING,
                    )
                    return
                self._start_operating_mode(
                    config_index=config_index,
                    timeout_s=timeout_s,
                    lock_message="Could not acquire lock to start the AMX.",
                    success_message="AMX timing enabled.",
                    restart_acquisition=True,
                )
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
                f"Failed to toggle AMX: {self._format_exception(exc)}",
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

        self._discard_pending_runtime_applies()
        stop_acquisition = getattr(self, "stopAcquisition", None)
        if callable(stop_acquisition):
            stop_acquisition()
            self.acquiring = False
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
                "Could not acquire lock to shut down the AMX."
            ):
                device = self.device
                if device is None:
                    shutdown_confirmed = True
                else:
                    shutdown_result = device.shutdown(
                        timeout_s=float(getattr(self.controllerParent, "startup_timeout_s", 10.0)),
                        **shutdown_kwargs,
                    )
                    shutdown_confirmed = shutdown_result is not False
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            confirmation_reason = self._format_exception(exc)
            self.print(
                f"AMX shutdown failed: {self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
        else:
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
        base_close = getattr(super(), "closeCommunication", None)
        if callable(base_close):
            base_close()
        resolved_final_state = str(final_state or "Disconnected")
        is_disconnected = resolved_final_state == "Disconnected"
        self.main_state = resolved_final_state
        self.device_enabled_state = "OFF" if is_disconnected else "Unknown"
        self.device_state_summary = "n/a" if is_disconnected else "Unknown"
        self.controller_state_summary = "n/a" if is_disconnected else "Unknown"
        self.available_configs = []
        self.available_configs_text = "n/a"
        self.loaded_config_text = "n/a"
        self._discard_pending_runtime_applies()
        self._dispose_device()
        self.initialized = False
        self._sync_status_to_gui()

    def _update_state(self) -> None:
        device = self.device
        if device is None:
            self.main_state = "Disconnected"
            self.device_enabled_state = "OFF"
            self.device_state_summary = "n/a"
            self.controller_state_summary = "n/a"
            return

        try:
            with self._controller_lock_section(
                "Could not acquire lock to refresh the AMX state."
            ):
                device = self.device
                if device is None:
                    self.main_state = "Disconnected"
                    self.device_enabled_state = "OFF"
                    self.device_state_summary = "n/a"
                    self.controller_state_summary = "n/a"
                    return
                snapshot = device.collect_housekeeping(
                    timeout_s=float(getattr(self.controllerParent, "poll_timeout_s", 5.0))
                )
                self._apply_snapshot(snapshot)
        except Exception:
            try:
                connected = bool(device.get_status().get("connected", False))
                self.main_state = "Error" if connected else "Disconnected"
            except Exception:
                self.main_state = "Unknown"
            self.device_enabled_state = "Unknown"
            self.device_state_summary = "Unknown"
            self.controller_state_summary = "Unknown"


    def _sync_status_to_gui(self) -> None:
        self.controllerParent.main_state = self.main_state
        self.controllerParent.device_enabled_state = self.device_enabled_state
        self.controllerParent.available_configs = list(self.available_configs)
        self.controllerParent.available_configs_text = self.available_configs_text
        self.controllerParent.loaded_config_text = self.loaded_config_text
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
            if not acquire(timeout=float(timeout_s)):
                if log_timeout:
                    self.print(timeout_message, flag=PRINT.ERROR)
                raise TimeoutError(timeout_message)
            try:
                yield
            finally:
                release()
            return

        acquire_timeout = getattr(lock, "acquire_timeout", None)
        if callable(acquire_timeout):
            with acquire_timeout(
                float(timeout_s),
                timeoutMessage=timeout_message if log_timeout else "",
                already_acquired=already_acquired,
            ) as lock_acquired:
                if not lock_acquired:
                    if log_timeout:
                        self.print(timeout_message, flag=PRINT.ERROR)
                    raise TimeoutError(timeout_message)
                yield
            return

        raise TypeError(
            "AMX controller lock must provide either acquire_timeout() or acquire()/release()."
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
        return str(exc).strip()
