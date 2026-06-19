"""Drive AMPR high-voltage channels and monitor measured output voltages."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
import time
from pathlib import Path
from threading import Lock
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
_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_ampr_runtime"
_AMPR_DRIVER_CLASS: type[Any] | None = None
_CHANNEL_NAME_KEY = getattr(Parameter, "NAME", getattr(Channel, "NAME", "Name"))
_CHANNEL_ENABLED_KEY = getattr(Channel, "ENABLED", "Enabled")
_CHANNEL_REAL_KEY = getattr(Channel, "REAL", "Real")
_PARAMETER_MIN_KEY = getattr(Parameter, "MIN", "Min")
_PARAMETER_MAX_KEY = getattr(Parameter, "MAX", "Max")
_PARAMETER_ADVANCED_KEY = getattr(Parameter, "ADVANCED", "Advanced")
_PARAMETER_TOOLTIP_KEY = getattr(Parameter, "TOOLTIP", "Tooltip")
_PARAMETER_EVENT_KEY = getattr(Parameter, "EVENT", "Event")
_AMPR_MODULE_KEY = "Module"
_AMPR_CHANNEL_ID_KEY = "CH"
_CHANNELS_PER_MODULE = 4
_CHANNELS_PER_MODULE_OPTIONS = {2, 4}
_AMPR_ABS_VOLTAGE_LIMIT = 1000.0
_AMPR_MIN_ROW_HEIGHT = 28
_AMPR_RAMP_STEP_S = 0.1
_AMPR_COMMUNICATION_LOST_STATE = "Communication lost"
_AMPR_SHUTDOWN_UNCONFIRMED_STATE = "Shutdown unconfirmed"
_AMPR_TRANSPORT_FAILURE_THRESHOLD = 3
_AMPR_POWER_ON_ICON = "switch-medium_on.png"
_AMPR_POWER_OFF_ICON = "switch-medium_off.png"
_AMPR_CHANNEL_ON_LABEL = "HV ON"
_AMPR_CHANNEL_OFF_LABEL = "HV OFF"
_AMPR_CHANNEL_TOGGLE_MIN_WIDTH = 58
_AMPR_MONITOR_OK_STYLE = "background-color: #2f855a; color: #ffffff; margin:0px;"
_AMPR_MONITOR_WARN_STYLE = "background-color: #dd6b20; color: #ffffff; margin:0px;"
_AMPR_MONITOR_ERROR_STYLE = "background-color: #c53030; color: #ffffff; margin:0px;"
_AMPR_MONITOR_NEUTRAL_STYLE = ""
_AMPR_MONITOR_OK_RELATIVE_TOLERANCE = 0.01
_AMPR_MONITOR_WARN_RELATIVE_TOLERANCE = 0.10
_AMPR_MONITOR_RELATIVE_FLOOR_V = 1.0


def _is_nan(value: Any) -> bool:
    """Return True when a value is NaN-like."""
    try:
        return bool(np.isnan(value))
    except TypeError:
        return False


def _coerce_int(value: Any, default: int) -> int:
    """Return an integer value from config-like input."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    """Return a float value from config-like input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Return a boolean value from config-like input."""
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
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) <= 1:
        return text
    return f"{parts[0]} +{len(parts) - 1}"


def _transport_failure_is_fatal(exc: Exception) -> bool:
    """Return True when an exception clearly indicates a dead AMPR transport."""
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


def _action_label(action: Any) -> str:
    """Extract a stable label from QAction-like objects and test doubles."""
    for attr_name in ("toolTip", "text", "objectName"):
        attr = getattr(action, attr_name, None)
        value = attr() if callable(attr) else attr
        if isinstance(value, str) and value:
            return value
    return ""


def _channel_key_from_item(item: dict[str, Any]) -> tuple[int, int]:
    """Return the physical AMPR output addressed by one channel item."""
    return (
        _coerce_int(item.get(_AMPR_MODULE_KEY), 0),
        _coerce_int(item.get(_AMPR_CHANNEL_ID_KEY), 1),
    )


def _generic_channel_name(device_name: str, module: int, channel_id: int) -> str:
    """Generate a stable generic channel name from the physical mapping."""
    return f"{device_name}_M{module:02d}_CH{channel_id}"


def _build_generic_channel_item(
    device_name: str,
    module: int,
    channel_id: int,
    default_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a generic channel config for a newly detected physical output."""
    item = dict(default_item or {})
    item[_CHANNEL_NAME_KEY] = _generic_channel_name(device_name, module, channel_id)
    item[_AMPR_MODULE_KEY] = str(module)
    item[_AMPR_CHANNEL_ID_KEY] = str(channel_id)
    item[_CHANNEL_REAL_KEY] = True
    item[_CHANNEL_ENABLED_KEY] = False
    return item


def _module_channel_count(
    module: int,
    module_channel_counts: dict[int, int] | None = None,
) -> int:
    """Return the expected channel count for one module."""
    if not module_channel_counts:
        return _CHANNELS_PER_MODULE
    channel_count = _coerce_int(
        module_channel_counts.get(_coerce_int(module, -1), _CHANNELS_PER_MODULE),
        _CHANNELS_PER_MODULE,
    )
    if channel_count not in _CHANNELS_PER_MODULE_OPTIONS:
        return _CHANNELS_PER_MODULE
    return channel_count


def _detected_output_keys(
    detected_modules: list[int],
    module_channel_counts: dict[int, int] | None = None,
) -> list[tuple[int, int]]:
    """Expand detected modules into the full ordered list of physical outputs."""
    return [
        (module, channel_id)
        for module in sorted({_coerce_int(module, -1) for module in detected_modules})
        if module >= 0
        for channel_id in range(
            1, _module_channel_count(module, module_channel_counts) + 1
        )
    ]


def _looks_like_bootstrap_items(
    items: list[dict[str, Any]],
    device_name: str,
    default_item: dict[str, Any] | None = None,
) -> bool:
    """Detect the default auto-generated ESIBD channel bootstrap."""
    if not items:
        return False

    expected_names = [f"{device_name}{index}" for index in range(1, len(items) + 1)]
    item_names = [str(item.get(_CHANNEL_NAME_KEY, "")) for item in items]
    if item_names != expected_names:
        return False

    if default_item is None:
        return all(_channel_key_from_item(item) == (0, 1) for item in items)

    for item in items:
        for key, default_value in default_item.items():
            if key == _CHANNEL_NAME_KEY:
                continue
            item_value = item.get(key, default_value)
            if key in {_AMPR_MODULE_KEY, _AMPR_CHANNEL_ID_KEY}:
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
    """Remove stale AMPR1..N bootstrap channels from previously polluted configs."""
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
            f"Removed legacy AMPR bootstrap channels: "
            f"{device_name}1..{device_name}{residue_count}",
            None,
        )
    ]


def _plan_channel_sync(
    current_items: list[dict[str, Any]],
    detected_modules: list[int],
    device_name: str,
    default_item: dict[str, Any] | None = None,
    module_channel_counts: dict[int, int] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, PRINT | None]]]:
    """Return the target channel config and corresponding sync log entries."""
    detected_keys = _detected_output_keys(
        detected_modules,
        module_channel_counts=module_channel_counts,
    )
    if not detected_keys:
        return current_items, []

    if _looks_like_bootstrap_items(current_items, device_name, default_item=default_item):
        bootstrap_items = [
            _build_generic_channel_item(
                device_name,
                module,
                channel_id,
                default_item=default_item,
            )
            for module, channel_id in detected_keys
        ]
        return bootstrap_items, [
            (
                "AMPR bootstrap config replaced from hardware scan.",
                None,
            )
        ]

    current_items, cleanup_logs = _strip_legacy_bootstrap_residue(
        current_items,
        device_name=device_name,
        default_item=default_item,
    )

    detected_set = set(detected_keys)
    kept_keys: set[tuple[int, int]] = set()
    added_modules: set[int] = set()
    virtualized_modules: set[int] = set()
    reactivated_modules: set[int] = set()
    duplicate_entries: list[tuple[str, int, int]] = []
    synced_items: list[dict[str, Any]] = []

    for item in current_items:
        synced_item = dict(item)
        module, channel_id = _channel_key_from_item(synced_item)
        key = (module, channel_id)
        if key in kept_keys:
            duplicate_entries.append(
                (str(synced_item.get(_CHANNEL_NAME_KEY, "")), module, channel_id)
            )
            synced_item[_CHANNEL_REAL_KEY] = False
            synced_items.append(synced_item)
            continue

        kept_keys.add(key)
        if key in detected_set:
            if not _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                reactivated_modules.add(module)
            synced_item[_CHANNEL_REAL_KEY] = True
        else:
            if _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                virtualized_modules.add(module)
            synced_item[_CHANNEL_REAL_KEY] = False
        synced_items.append(synced_item)

    for module, channel_id in detected_keys:
        key = (module, channel_id)
        if key in kept_keys:
            continue
        synced_items.append(
            _build_generic_channel_item(
                device_name,
                module,
                channel_id,
                default_item=default_item,
            )
        )
        added_modules.add(module)

    log_entries: list[tuple[str, PRINT | None]] = list(cleanup_logs)
    if added_modules:
        log_entries.append(
            (
                "Added generic AMPR channels for detected modules: "
                + ", ".join(str(module) for module in sorted(added_modules)),
                None,
            )
        )
    if virtualized_modules:
        log_entries.append(
            (
                "Marked AMPR channels virtual because modules are absent: "
                + ", ".join(str(module) for module in sorted(virtualized_modules)),
                None,
            )
        )
    if reactivated_modules:
        log_entries.append(
            (
                "Reactivated AMPR channels for modules: "
                + ", ".join(str(module) for module in sorted(reactivated_modules)),
                None,
            )
        )
    for channel_name, module, channel_id in duplicate_entries:
        log_entries.append(
            (
                f"Duplicate AMPR mapping detected for module {module} CH{channel_id}: {channel_name}",
                PRINT.WARNING,
            )
        )
    return synced_items, log_entries


def _bundled_runtime_module_name(plugin_dir: Path | None = None) -> str:
    """Return the private Python module namespace used for the bundled runtime."""
    resolved_plugin_dir = Path(__file__).resolve().parent if plugin_dir is None else plugin_dir
    plugin_key = resolved_plugin_dir.name.replace("-", "_")
    return f"{_BUNDLED_RUNTIME_NAMESPACE_PREFIX}_{plugin_key}"


def _load_private_runtime_package(module_name: str, package_dir: Path) -> None:
    """Load a bundled runtime package from disk under a private module name."""
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
            f"Could not create an import spec for bundled AMPR runtime at {package_dir}."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise


def _get_ampr_driver_class() -> type[Any]:
    """Load the AMPR driver lazily from the bundled runtime only."""
    global _AMPR_DRIVER_CLASS

    if _AMPR_DRIVER_CLASS is not None:
        return _AMPR_DRIVER_CLASS

    plugin_dir = Path(__file__).resolve().parent
    bundled_runtime_dir = plugin_dir / "vendor" / _BUNDLED_RUNTIME_DIRNAME
    bundled_runtime_init = bundled_runtime_dir / "__init__.py"
    if not bundled_runtime_init.exists():
        raise ModuleNotFoundError(
            "Bundled AMPR runtime not found in vendor/runtime; "
            "plugin installation is incomplete."
        )

    runtime_module_name = _bundled_runtime_module_name(plugin_dir)
    _load_private_runtime_package(runtime_module_name, bundled_runtime_dir)
    module = importlib.import_module(f"{runtime_module_name}.ampr")
    _AMPR_DRIVER_CLASS = cast(type[Any], module.AMPR)
    return _AMPR_DRIVER_CLASS


def providePlugins() -> "list[type[Plugin]]":
    """Return the plugins provided by this module."""
    return [AMPRDevice]


class AMPRDevice(Device):
    """Drive AMPR channels and read back their measured voltages."""

    documentation = (
        "Drives AMPR high-voltage channels and monitors measured output voltages."
    )

    name = "AMPR_A"
    version = "0.1.0"
    supportedVersion = "1.0.1"
    pluginType = PLUGINTYPE.INPUTDEVICE
    unit = "V"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "ampr.png"
    channels: "list[AMPRChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    STARTUP_TIMEOUT = "Startup timeout (s)"
    RAMP_RATE = "Ramp rate (V/s)"
    STATE = "State"
    DETECTED_MODULES = "Detected modules"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = AMPRChannel
        self.module_channel_counts: dict[int, int] = {}
        self.module_voltage_limits: dict[int, float] = {}

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
        self.controller = AMPRController(controllerParent=self)

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
        self._update_channel_column_visibility()
        self._sync_acquisition_controls()

    def getChannels(self) -> "list[AMPRChannel]":
        return cast("list[AMPRChannel]", super().getChannels())

    com: int
    baudrate: int
    connect_timeout_s: float
    startup_timeout_s: float
    ramp_rate_v_s: float
    module_channel_counts: dict[int, int]
    module_voltage_limits: dict[int, float]
    main_state: str
    detected_modules: str
    device_state_summary: str
    interlock_state_summary: str
    voltage_state_summary: str

    def getConfiguredModules(self) -> list[int]:
        """Return sorted module addresses referenced by real channels."""
        return sorted(
            {channel.module_address() for channel in self.getChannels() if channel.real}
        )

    def module_voltage_limit(self, module: int) -> float:
        """Return the detected voltage limit for one module, defaulting to ±1000 V."""
        limit = _coerce_float(
            self.module_voltage_limits.get(_coerce_int(module, 0)),
            _AMPR_ABS_VOLTAGE_LIMIT,
        )
        if not np.isfinite(limit) or limit <= 0:
            return _AMPR_ABS_VOLTAGE_LIMIT
        return min(abs(limit), _AMPR_ABS_VOLTAGE_LIMIT)

    def module_channel_count(self, module: int) -> int:
        """Return the detected channel count for one module, defaulting to 4."""
        return _module_channel_count(module, self.module_channel_counts)

    def _apply_module_voltage_limits(self) -> bool:
        """Apply per-module voltage ratings to current channels."""
        changed = False
        for channel in self.getChannels():
            changed = channel.applyModuleVoltageLimit(
                self.module_voltage_limit(channel.module_address())
            ) or changed
        return changed

    def _current_channel_items(self) -> list[dict[str, Any]]:
        """Snapshot current channels into config dictionaries."""
        return [
            channel.asDict()
            for channel in self.getChannels()
        ]

    def _default_channel_template(self) -> dict[str, dict[str, Any]]:
        """Return the default AMPR channel parameter definitions."""
        return self.channelType(channelParent=self, tree=None).getSortedDefaultChannel()

    def _default_channel_item(self) -> dict[str, Any]:
        """Return the persisted default AMPR channel configuration."""
        return self.channelType(channelParent=self, tree=None).asDict()

    def _ensure_local_on_action(self) -> None:
        """Expose the global AMPR ON/OFF control directly in the plugin toolbar."""
        if (
            not self.useOnOffLogic
            or hasattr(self, "deviceOnAction")
            or not hasattr(self, "closeCommunicationAction")
        ):
            return

        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(on=checked),
            toolTipFalse=f"Turn {self.name} ON.",
            iconFalse=self.makeIcon(_AMPR_POWER_ON_ICON),
            toolTipTrue=f"Turn {self.name} OFF and disconnect.",
            iconTrue=self.makeIcon(_AMPR_POWER_OFF_ICON),
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

    def _ensure_status_widgets(self) -> None:
        """Add compact global AMPR status labels to the plugin toolbar."""
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
        """Return a compact badge style that reflects the AMPR main state."""
        state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        if state == "ST_ON":
            background = "#2f855a"
        elif state == "ST_STBY":
            background = "#b7791f"
        elif state == "Disconnected":
            background = "#718096"
        elif (
            state == "ST_OVERLOAD"
            or state.startswith("ST_ERR")
            or "error" in state.lower()
            or "lost" in state.lower()
        ):
            background = "#c53030"
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
        """Return the compact AMPR runtime summary displayed in the toolbar."""
        modules = str(getattr(self, "detected_modules", "") or "None")
        interlock = _compact_status_text(
            getattr(self, "interlock_state_summary", None),
            default="n/a",
        )
        faults = _compact_status_text(
            getattr(self, "device_state_summary", None),
            default="n/a",
        )
        return f"Modules: {modules} | Interlock: {interlock} | Faults: {faults}"

    def _status_tooltip_text(self) -> str:
        """Return the full AMPR status tooltip for the toolbar widgets."""
        return "\n".join(
            (
                f"State: {getattr(self, 'main_state', 'Disconnected') or 'Disconnected'}",
                f"Modules: {getattr(self, 'detected_modules', '') or 'None'}",
                f"Interlock: {getattr(self, 'interlock_state_summary', '') or 'n/a'}",
                f"Faults: {getattr(self, 'device_state_summary', '') or 'n/a'}",
                f"Voltage rails: {getattr(self, 'voltage_state_summary', '') or 'n/a'}",
            )
        )

    def _update_status_widgets(self) -> None:
        """Refresh the global AMPR status labels in the toolbar."""
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        self._sync_acquisition_controls()
        if badge is None or summary is None:
            return

        badge_text = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
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

    def _set_channel_headers_from_template(self) -> None:
        """Apply channel headers even when no concrete channel exists yet."""
        if self.tree is None:
            return
        self.tree.setHeaderLabels(
            [
                parameter_dict.get(Parameter.HEADER, "") or name.title()
                for name, parameter_dict in self._default_channel_template().items()
            ]
        )

    def _update_channel_column_visibility(self) -> None:
        """Hide framework columns that are not useful for the AMPR UI."""
        if self.tree is None or not self.channels:
            return

        parameter_names = list(self.channels[0].getSortedDefaultChannel())
        for hidden_name in (Channel.COLLAPSE, Channel.REAL):
            if hidden_name in parameter_names:
                self.tree.setColumnHidden(parameter_names.index(hidden_name), True)

    def _sync_channels_from_detected_modules(self, detected_modules: list[int]) -> bool:
        """Synchronize channels from the latest detected AMPR module scan."""
        current_items = self._current_channel_items()
        target_items, log_entries = _plan_channel_sync(
            current_items=current_items,
            detected_modules=detected_modules,
            device_name=self.name,
            default_item=self._default_channel_item(),
            module_channel_counts=self.module_channel_counts,
        )
        if target_items == current_items:
            return False

        self._apply_channel_items(target_items)
        for message, flag in log_entries:
            if flag is None:
                self.print(message)
            else:
                self.print(message, flag=flag)
        self.exportConfiguration(useDefaultFile=True)
        return True

    def _apply_channel_items(self, items: list[dict[str, Any]]) -> None:
        """Apply a rebuilt channel configuration using the standard ESIBD flow."""
        config_file = self.customConfigFile(self.confINI)
        self.loading = True
        if self.tree is not None:
            self.tree.setUpdatesEnabled(False)
        try:
            self.updateChannelConfig(items, config_file)
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
                    channel.collapseChanged(toggle=False)
                self.tree.scheduleDelayedItemsLayout()
            if hasattr(self, "advancedAction"):
                self.toggleAdvanced(advanced=self.advancedAction.state)
            self._update_channel_column_visibility()
            self.estimateStorage()
            self.pluginManager.DeviceManager.globalUpdate(inout=self.inout)
        finally:
            if self.tree is not None:
                self.tree.setUpdatesEnabled(True)
                self.tree.scheduleDelayedItemsLayout()
                self.tree.viewport().update()
            self.processEvents()
            self.loading = False

    def loadConfiguration(
        self,
        file: "Path | None" = None,
        useDefaultFile: bool = False,
        append: bool = False,
    ) -> None:
        """Skip the generic 9-channel bootstrap until AMPR hardware is initialized."""
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
                    f"AMPR config file {file} not found. "
                    "Channels will be created after successful hardware initialization."
                )
                self._set_channel_headers_from_template()
                if hasattr(self, "advancedAction"):
                    self.toggleAdvanced(advanced=self.advancedAction.state)
                if self.tree is not None:
                    self.tree.scheduleDelayedItemsLayout()
                self.pluginManager.DeviceManager.globalUpdate(inout=self.inout)
            finally:
                if self.tree is not None:
                    self.tree.setUpdatesEnabled(True)
                self.loading = False
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)

    def toggleAdvanced(self, advanced: "bool | None" = False) -> None:
        """Handle advanced columns without hiding AMPR channels."""
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
        """Avoid division by zero before the first AMPR channel discovery."""
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
                "AMPR hardware initialization."
            )

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=1,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the AMPR controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Baud rate passed to cgc.ampr.AMPR.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=1.0,
            maximum=30.0,
            toolTip="Timeout in seconds used to connect and validate the controller.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.STARTUP_TIMEOUT}"] = parameterDict(
            value=20.0,
            minimum=1.0,
            maximum=120.0,
            toolTip="Timeout in seconds used to wait for the AMPR to reach ST_ON after pressing ON.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="startup_timeout_s",
        )
        settings[f"{self.name}/{self.RAMP_RATE}"] = parameterDict(
            value=10.0,
            minimum=0.0,
            maximum=_AMPR_ABS_VOLTAGE_LIMIT,
            toolTip=(
                "Software ramp rate used for AMPR global ON/OFF transitions. "
                "Set to 0 to disable ramping."
            ),
            parameterType=PARAMETERTYPE.FLOAT,
            attr="ramp_rate_v_s",
        )
        settings[f"{self.name}/{self.STATE}"] = parameterDict(
            value="Disconnected",
            toolTip="Latest AMPR controller state reported by the driver.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="main_state",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.DETECTED_MODULES}"] = parameterDict(
            value="",
            toolTip="Module addresses detected during initialization.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="detected_modules",
            indicator=True,
            internal=True,
            advanced=True,
            restore=False,
        )
        settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

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
        """Disable manual acquisition controls until the AMPR is actually ready."""
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
        """Only allow data recording when the AMPR is initialized and in ST_ON."""
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
        """Close communication safely even if plugin finalization failed early."""
        controller = getattr(self, "controller", None)
        forced_close_state = getattr(controller, "_forced_close_state", None)
        if self.useOnOffLogic and not hasattr(self, "onAction"):
            self.stopAcquisition()
            if controller:
                close_kwargs = (
                    {"final_state": forced_close_state}
                    if forced_close_state
                    else {}
                )
                controller.closeCommunication(**close_kwargs)
            self.recording = False
            self._sync_acquisition_controls()
            return

        if controller and getattr(controller, "initialized", False) and not forced_close_state:
            self.shutdownCommunication()
            return

        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False
            self._sync_local_on_action()
            self._sync_toolbar_communication_controls()
        self.stopAcquisition()
        if controller:
            close_kwargs = (
                {"final_state": forced_close_state}
                if forced_close_state
                else {}
            )
            controller.closeCommunication(**close_kwargs)
        self.recording = False
        self._sync_acquisition_controls()

    def shutdownCommunication(self) -> None:
        """Run the full AMPR hardware shutdown sequence from the toolbar action."""
        shutdown_confirmed = True
        controller = getattr(self, "controller", None)
        self.stopAcquisition()
        if controller:
            shutdown_confirmed = bool(controller.shutdownCommunication())
        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False if shutdown_confirmed else True
            self._sync_local_on_action()
            self._sync_toolbar_communication_controls()
        if not shutdown_confirmed:
            self.print(
                "AMPR shutdown could not be confirmed; UI remains ON until "
                "the hardware state is verified.",
                flag=PRINT.WARNING,
            )
        self.recording = False
        self._sync_acquisition_controls()

    def _set_on_ui_state(self, on: bool) -> None:
        """Synchronize the ESIBD and local AMPR ON/OFF actions."""
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

    def setOn(self, on: "bool | None" = None) -> None:
        """Toggle the AMPR without the generic immediate apply=True jump."""
        controller = self.controller if hasattr(self, "controller") else None
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
        if getattr(self, "loading", False):
            return

        if getattr(self, "initialized", False):
            begin_transition = getattr(self.controller, "_begin_transition", None) if self.controller else None
            if self.controller and (not callable(begin_transition) or begin_transition(self.isOn())):
                self.controller.toggleOnFromThread(parallel=True)
            else:
                for channel in self.channels:
                    if channel.controller:
                        channel.controller.toggleOnFromThread(parallel=True)
        elif hasattr(self, "onAction") and self.isOn():
            self.initializeCommunication()


class AMPRChannel(Channel):
    """AMPR output channel definition."""

    MODULE = "Module"
    ID = "CH"
    channelParent: AMPRDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.module: int
        self.id: int

        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Voltage (V)"
        channel[self.VALUE][_PARAMETER_MIN_KEY] = -_AMPR_ABS_VOLTAGE_LIMIT
        channel[self.VALUE][_PARAMETER_MAX_KEY] = _AMPR_ABS_VOLTAGE_LIMIT
        channel[self.ENABLED][_PARAMETER_ADVANCED_KEY] = False
        channel[self.ENABLED][Parameter.HEADER] = "On"
        channel[self.ENABLED][_PARAMETER_TOOLTIP_KEY] = (
            "Enable this AMPR output channel. Disabled channels are held at 0 V."
        )
        channel[self.ACTIVE][Parameter.HEADER] = "Manual"
        channel[self.ACTIVE][_PARAMETER_TOOLTIP_KEY] = (
            "If enabled, this channel uses its manual voltage setpoint. "
            "If disabled, ESIBD will drive it from the channel equation."
        )
        channel[self.DISPLAY][Parameter.HEADER] = "Display"
        channel[self.DISPLAY][_PARAMETER_EVENT_KEY] = self.displayChanged
        channel[self.SCALING][Parameter.VALUE] = "large"
        channel[self.MIN][Parameter.VALUE] = -_AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MIN][_PARAMETER_ADVANCED_KEY] = False
        channel[self.MIN][_PARAMETER_MIN_KEY] = -_AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MIN][_PARAMETER_MAX_KEY] = _AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MIN][_PARAMETER_EVENT_KEY] = self.minChanged
        channel[self.MAX][Parameter.VALUE] = _AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MAX][_PARAMETER_ADVANCED_KEY] = False
        channel[self.MAX][_PARAMETER_MIN_KEY] = -_AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MAX][_PARAMETER_MAX_KEY] = _AMPR_ABS_VOLTAGE_LIMIT
        channel[self.MAX][_PARAMETER_EVENT_KEY] = self.maxChanged
        channel[self.MODULE] = parameterDict(
            value="0",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Mod",
            attr="module",
        )
        channel[self.ID] = parameterDict(
            value="1",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="CH ",
            attr="id",
        )
        return channel

    def setDisplayedParameters(self) -> None:
        super().setDisplayedParameters()
        if self.OPTIMIZE in self.displayedParameters:
            self.displayedParameters.remove(self.OPTIMIZE)
        if self.DISPLAY in self.displayedParameters:
            self.displayedParameters.remove(self.DISPLAY)
        self.displayedParameters.append(self.MODULE)
        self.displayedParameters.append(self.ID)
        self.displayedParameters.append(self.DISPLAY)

    def initGUI(self, item: dict) -> None:
        super().initGUI(item)
        self._upgrade_toggle_widget(self.ENABLED, _AMPR_CHANNEL_ON_LABEL, _AMPR_CHANNEL_TOGGLE_MIN_WIDTH)
        self._upgrade_toggle_widget(self.ACTIVE, "Manual", 72)
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        self.scalingChanged()

    def scalingChanged(self) -> None:
        super().scalingChanged()
        if self.rowHeight >= _AMPR_MIN_ROW_HEIGHT:
            return
        self.rowHeight = _AMPR_MIN_ROW_HEIGHT
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
        parameter = self.getParameterByName(parameter_name)
        if parameter is None:
            return

        initial_value = bool(parameter.value)
        parameter.widget = ToolButton()
        parameter.applyWidget()
        if parameter.check:
            parameter.check.setMaximumHeight(max(parameter.rowHeight, _AMPR_MIN_ROW_HEIGHT))
            parameter.check.setMinimumWidth(minimum_width)
            parameter.check.setText(label)
            parameter.check.setCheckable(True)
            if hasattr(parameter.check, "setAutoRaise"):
                parameter.check.setAutoRaise(False)
        parameter.value = initial_value

    def monitorChanged(self) -> None:
        self._sync_monitor_feedback()

    def _channel_log_prefix(self) -> str:
        return (
            f"AMPR channel {getattr(self, 'name', 'Unknown')} "
            f"(module {self.module_address()} CH{self.channel_number()})"
        )

    def _log_channel_event(self, message: str) -> None:
        if getattr(self.channelParent, "loading", False):
            return
        self.channelParent.print(f"{self._channel_log_prefix()}: {message}")

    def nameChanged(self) -> None:
        super().nameChanged()
        self._log_channel_event(f"Name changed to {self.name!r}.")

    def valueChanged(self) -> None:
        super().valueChanged()
        self._sync_monitor_feedback()
        self._log_channel_event(f"Voltage setpoint changed to {float(self.value):.3f} V.")

    def equationChanged(self) -> None:
        super().equationChanged()
        if str(getattr(self, "equation", "")).strip():
            self._log_channel_event(f"Equation changed to {self.equation!r}.")
            return
        self._log_channel_event("Equation cleared.")

    def activeChanged(self) -> None:
        super().activeChanged()
        mode = "manual" if self.active else "equation"
        self._log_channel_event(f"Control mode changed to {mode}.")

    def realChanged(self) -> None:
        self.getParameterByName(self.MODULE).setVisible(self.real)
        self.getParameterByName(self.ID).setVisible(self.real)
        super().realChanged()

    def enabledChanged(self) -> None:
        super().enabledChanged()
        if not self.enabled:
            self.monitor = np.nan
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        state = "ON" if self.enabled else "OFF"
        self._log_channel_event(f"Output switched {state}.")
        if not getattr(self.channelParent, "loading", False):
            apply_value = getattr(self, "applyValue", None)
            if callable(apply_value):
                apply_value(apply=True)

    def displayChanged(self) -> None:
        super().updateDisplay()
        state = "ON" if self.display else "OFF"
        self._log_channel_event(f"Display switched {state}.")

    def updateColor(self):
        """Keep the Display checkbox centered in its column."""
        color = super().updateColor()
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtWidgets import QCheckBox, QSizePolicy
        except Exception:
            return color

        display_param = self.getParameterByName(self.DISPLAY)
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

    def _enabled_toggle_label(self) -> str:
        """Return the explicit ON/OFF label used by the channel toggle."""
        enabled_value = getattr(self, "enabled", None)
        if enabled_value is None:
            getter = getattr(self, "getParameterByName", None)
            if callable(getter):
                try:
                    enabled_parameter = getter(getattr(self, "ENABLED", "Enabled"))
                except Exception:  # noqa: BLE001
                    enabled_parameter = None
                enabled_value = getattr(enabled_parameter, "value", False)
        return _AMPR_CHANNEL_ON_LABEL if bool(enabled_value) else _AMPR_CHANNEL_OFF_LABEL

    def _sync_enabled_toggle_widget(self) -> None:
        """Keep the per-channel toggle text synchronized with the enabled state."""
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        try:
            parameter = getter(getattr(self, "ENABLED", "Enabled"))
        except Exception:  # noqa: BLE001
            return
        widget = getattr(parameter, "check", None)
        if widget is None or not hasattr(widget, "setText"):
            return
        widget.setText(self._enabled_toggle_label())

    def _monitor_feedback_state(self) -> str:
        """Classify monitor accuracy relative to the current setpoint."""
        if not getattr(self, "enabled", False) or not getattr(self, "real", True):
            return "default"
        if getattr(self, "waitToStabilize", False):
            return "default"

        channel_parent = getattr(self, "channelParent", None)
        controller = getattr(channel_parent, "controller", None)
        if controller is None or not getattr(controller, "acquiring", False):
            return "default"
        if not callable(getattr(channel_parent, "isOn", None)) or not channel_parent.isOn():
            return "default"

        monitor_value = _coerce_float(getattr(self, "monitor", np.nan), np.nan)
        target_value = _coerce_float(getattr(self, "value", np.nan), np.nan)
        if _is_nan(monitor_value) or _is_nan(target_value):
            return "default"

        reference = max(abs(target_value), _AMPR_MONITOR_RELATIVE_FLOOR_V)
        relative_error = abs(monitor_value - target_value) / reference
        if relative_error <= _AMPR_MONITOR_OK_RELATIVE_TOLERANCE:
            return "ok"
        if relative_error <= _AMPR_MONITOR_WARN_RELATIVE_TOLERANCE:
            return "warn"
        return "error"

    def _sync_monitor_feedback(self) -> None:
        """Apply green/orange/red monitor background based on setpoint tracking accuracy."""
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
            style = _AMPR_MONITOR_OK_STYLE
        elif state == "warn":
            style = _AMPR_MONITOR_WARN_STYLE
        elif state == "error":
            style = _AMPR_MONITOR_ERROR_STYLE
        else:
            style = _AMPR_MONITOR_NEUTRAL_STYLE

        widget.setStyleSheet(style)
        self.warningState = state in {"warn", "error"}

    def minChanged(self) -> None:
        super().updateMin()
        self._log_channel_event(f"Minimum changed to {float(self.min):.3f} V.")

    def maxChanged(self) -> None:
        super().updateMax()
        self._log_channel_event(f"Maximum changed to {float(self.max):.3f} V.")

    def module_address(self) -> int:
        """Return the configured AMPR module address as an integer."""
        return _coerce_int(self.module, 0)

    def channel_number(self) -> int:
        """Return the configured AMPR channel number as an integer."""
        return _coerce_int(self.id, 1)

    def _set_parameter_value_without_events(self, parameter_name: str, value: Any) -> bool:
        """Set one parameter value silently and report whether it changed."""
        parameter = self.getParameterByName(parameter_name)
        if parameter is None:
            return False
        equals = getattr(parameter, "equals", None)
        if callable(equals):
            try:
                if equals(value):
                    return False
            except Exception:  # noqa: BLE001
                pass
        else:
            current_value = getattr(parameter, "value", None)
            if current_value == value:
                return False
        setter = getattr(parameter, "setValueWithoutEvents", None)
        if callable(setter):
            setter(value)
        else:
            parameter.value = value
        return True

    def applyModuleVoltageLimit(self, limit: float) -> bool:
        """Apply one detected per-module voltage limit to the channel UI/state."""
        limit = max(0.0, min(float(limit), _AMPR_ABS_VOLTAGE_LIMIT))
        lower = -limit
        upper = limit
        changed = False

        for parameter_name in (self.VALUE, self.MIN, self.MAX):
            parameter = self.getParameterByName(parameter_name)
            if parameter is None:
                continue
            if getattr(parameter, "min", None) != lower:
                parameter.min = lower
                changed = True
            if getattr(parameter, "max", None) != upper:
                parameter.max = upper
                changed = True

        clamped_min = min(max(_coerce_float(getattr(self, "min", lower), lower), lower), upper)
        clamped_max = min(max(_coerce_float(getattr(self, "max", upper), upper), lower), upper)
        if clamped_min > clamped_max:
            clamped_min, clamped_max = lower, upper

        changed = self._set_parameter_value_without_events(self.MIN, clamped_min) or changed
        changed = self._set_parameter_value_without_events(self.MAX, clamped_max) or changed

        current_value = getattr(self, "value", 0.0)
        if _is_nan(current_value):
            clamped_value = current_value
        else:
            clamped_value = min(
                max(_coerce_float(current_value, 0.0), clamped_min),
                clamped_max,
            )
        changed = self._set_parameter_value_without_events(self.VALUE, clamped_value) or changed

        self.updateMin()
        self.updateMax()
        return changed


class AMPRController(DeviceController):
    """AMPR hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: AMPRDevice

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.detected_module_ids: list[int] = []
        self.detected_modules_text = ""
        self.main_state = "Disconnected"
        self.device_state_summary = "n/a"
        self.interlock_state_summary = "n/a"
        self.voltage_state_summary = "n/a"
        self.initialized = False
        self.ramping = False
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._forced_close_state: str | None = None
        self._consecutive_transport_failures = 0

    def initializeValues(self, reset: bool = False) -> None:
        if getattr(self, "values", None) is None or reset:
            get_channels = getattr(self.controllerParent, "getChannels", None)
            if not callable(get_channels):
                self.values = {}
                return
            self.values = {
                (channel.module_address(), channel.channel_number()): np.nan
                for channel in get_channels()
                if channel.real
            }

    def _module_voltage_limit(self, module: int) -> float:
        """Return one module voltage limit with a safe ±1000 V fallback."""
        limit_getter = getattr(self.controllerParent, "module_voltage_limit", None)
        if callable(limit_getter):
            return float(limit_getter(module))
        return _AMPR_ABS_VOLTAGE_LIMIT

    def runInitialization(self) -> None:
        self.initialized = False
        self._dispose_device()
        try:
            ampr_driver_class = _get_ampr_driver_class()
            self.device = ampr_driver_class(
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
            self._refresh_module_scan()
            self._update_state()
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:  # noqa: BLE001
            self._restore_off_ui_state()
            self.print(
                f"AMPR initialization failed on COM{int(self.controllerParent.com)}: "
                f"{self._format_exception(exc)}",
                flag=PRINT.ERROR,
            )
            self._dispose_device()
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        if self.device is not None and self.detected_module_ids:
            self.controllerParent._sync_channels_from_detected_modules(
                self.detected_module_ids
            )
            apply_limits = getattr(self.controllerParent, "_apply_module_voltage_limits", None)
            export_config = getattr(self.controllerParent, "exportConfiguration", None)
            if callable(apply_limits) and apply_limits() and callable(export_config):
                self.controllerParent.exportConfiguration(useDefaultFile=True)
        self.initializeValues()
        self.initialized = True
        self.super_init_complete_called = True
        self._sync_status_to_gui()
        if self.device is None:
            self.print(
                "AMPR initialization simulated because ESIBD Test mode is active. "
                "No hardware communication was attempted.",
                flag=PRINT.WARNING,
            )
            return

        modules_text = self.detected_modules_text or "None"
        self.print(
            f"AMPR initialized on COM{int(self.controllerParent.com)}. "
            f"State: {self.main_state}. Detected modules: {modules_text}."
        )
        if self.main_state == "ST_ON":
            start_acquisition = getattr(self, "startAcquisition", None)
            if callable(start_acquisition):
                start_acquisition()
        if getattr(self.controllerParent, "isOn", lambda: False)():
            with contextlib.suppress(Exception):
                self.controllerParent.updateValues(apply=False)
            if self._begin_transition(True):
                self.toggleOnFromThread(parallel=True)

    def readNumbers(self) -> None:
        if self.device is None or not getattr(self, "initialized", False):
            self.initializeValues(reset=True)
            return

        self._update_state()
        if self.main_state == _AMPR_COMMUNICATION_LOST_STATE or self.device is None:
            self.initializeValues(reset=True)
            return
        if self.main_state != "ST_ON":
            self.initializeValues(reset=True)
            return

        new_values = {
            (channel.module_address(), channel.channel_number()): np.nan
            for channel in self.controllerParent.getChannels()
            if channel.real
        }

        configured_modules = set(self.controllerParent.getConfiguredModules())
        detected_modules = set(self.detected_module_ids)
        if detected_modules:
            poll_modules = sorted(configured_modules & detected_modules)
        else:
            poll_modules = sorted(configured_modules)

        for module in poll_modules:
            try:
                with self._controller_lock_section(
                    f"Could not acquire lock to read AMPR module {module}."
                ):
                    device = self.device
                    if device is None:
                        return
                    voltages = device.get_module_voltages(module)
            except TimeoutError:
                # Transient controller-lock contention (another operation holds
                # the lock); skip this module this cycle. A real read fault is
                # raised by the device and handled by except-Exception below.
                continue
            except Exception as exc:  # noqa: BLE001
                self.errorCount += 1
                self.print(f"Failed to read module {module}: {exc}", flag=PRINT.ERROR)
                if _transport_failure_is_fatal(exc):
                    self._handle_transport_loss()
                    return
                continue

            for channel_id, voltage_data in voltages.items():
                measured = voltage_data.get("measured")
                new_values[(module, channel_id)] = (
                    np.nan if measured is None else float(measured)
                )

        self.values = new_values

    def fakeNumbers(self) -> None:
        self.initializeValues(reset=True)
        # Do not fabricate AMPR output readbacks in ESIBD test mode.
        # Showing random "measured" voltages is misleading because no hardware
        # communication happened at all in that mode.

    def applyValue(self, channel: AMPRChannel) -> None:
        device = self.device
        if (
            device is None
            or not getattr(self, "initialized", False)
            or getattr(self, "ramping", False)
            or getattr(self, "transitioning", False)
            or not self.controllerParent.isOn()
            or self.main_state != "ST_ON"
        ):
            return

        target_voltage = float(channel.value if channel.enabled else 0.0)
        module = channel.module_address()
        channel_id = channel.channel_number()
        voltage_limit = self._module_voltage_limit(module)
        if abs(target_voltage) > voltage_limit:
            self.errorCount += 1
            self.print(
                f"Refusing {target_voltage:.3f} V for module {module} CH{channel_id}: "
                f"detected module rating is ±{voltage_limit:.0f} V.",
                flag=PRINT.ERROR,
            )
            return
        try:
            with self._controller_lock_section(
                f"Could not acquire lock to apply module {module} CH{channel_id}."
            ):
                device = self.device
                if device is None:
                    return
                status = device.set_module_voltage(module, channel_id, target_voltage)
        except TimeoutError:
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(
                f"Failed to apply {target_voltage:.3f} V to module "
                f"{module} CH{channel_id}: {exc}",
                flag=PRINT.ERROR,
            )
            return

        if status != device.NO_ERR:
            self.errorCount += 1
            self.print(
                f"AMPR rejected {target_voltage:.3f} V for module "
                f"{module} CH{channel_id}: {self._format_status(status, device=device)}",
                flag=PRINT.ERROR,
            )

    def updateValues(self) -> None:
        if self.values is None:
            return

        self._sync_status_to_gui()
        device_is_on = self.controllerParent.isOn()
        for channel in self.controllerParent.getChannels():
            if channel.enabled and channel.real and device_is_on:
                channel.monitor = np.nan if channel.waitToStabilize else self.values.get(
                    (channel.module_address(), channel.channel_number()),
                    np.nan,
                )
                continue
            channel.monitor = np.nan

    def toggleOn(self) -> None:
        super().toggleOn()
        device = self.device
        if device is None:
            self._restore_off_ui_state()
            self._end_transition()
            return
        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False

        startup_timeout_s = float(
            getattr(
                self.controllerParent,
                "startup_timeout_s",
                self.controllerParent.connect_timeout_s,
            )
        )
        ramp_rate_v_s = max(
            0.0,
            float(getattr(self.controllerParent, "ramp_rate_v_s", 0.0)),
        )
        state_updated = False
        startup_completed = False
        startup_targets: dict[tuple[int, int], float] = {}

        try:
            if self.controllerParent.isOn():
                with contextlib.suppress(Exception):
                    self.controllerParent.updateValues(apply=False)
                try:
                    with self._controller_lock_section(
                        "Could not acquire lock to toggle the AMPR PSU."
                    ):
                        device = self.device
                        if device is None:
                            self._restore_off_ui_state()
                            return
                        self.print(
                            f"Starting AMPR PSU. Waiting up to {startup_timeout_s:.1f} s for ST_ON."
                        )
                        device.initialize(timeout_s=startup_timeout_s)
                        status = device.NO_ERR
                        startup_completed = True
                except TimeoutError:
                    self._restore_off_ui_state()
                    return
                if status == device.NO_ERR:
                    self._refresh_module_scan()
                    self._update_state()
                    state_updated = True
                    apply_limits = getattr(self.controllerParent, "_apply_module_voltage_limits", None)
                    export_config = getattr(self.controllerParent, "exportConfiguration", None)
                    if callable(apply_limits) and apply_limits() and callable(export_config):
                        self.controllerParent.exportConfiguration(useDefaultFile=True)
                    startup_targets = self._channel_target_voltages(
                        respect_device_state=True
                    )
                    if startup_targets:
                        zero_targets = {key: 0.0 for key in startup_targets}
                        self._apply_target_voltages(
                            zero_targets,
                            timeout_message=(
                                "Could not acquire lock to zero AMPR outputs after startup."
                            ),
                        )
                        self._ramp_target_voltages(
                            start_targets=zero_targets,
                            end_targets=startup_targets,
                            rate_v_s=ramp_rate_v_s,
                            label="up",
                        )
            else:
                shutdown_after_ramp_error = False
                start_targets = self._channel_target_voltages(respect_device_state=False)
                try:
                    self._ramp_target_voltages(
                        start_targets=start_targets,
                        end_targets={key: 0.0 for key in start_targets},
                        rate_v_s=ramp_rate_v_s,
                        label="down",
                    )
                except Exception as exc:  # noqa: BLE001
                    shutdown_after_ramp_error = True
                    self.errorCount += 1
                    self.print(
                        f"AMPR ramp-down before shutdown failed: {self._format_exception(exc)}",
                        flag=PRINT.ERROR,
                    )
                shutdown_confirmed = self.shutdownCommunication()
                if not shutdown_confirmed:
                    self._restore_on_ui_state()
                if shutdown_after_ramp_error or not shutdown_confirmed:
                    return
                return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            if startup_completed:
                self._safe_disable_after_toggle_failure(startup_targets)
            self._restore_off_ui_state()
            self._update_state()
            self.print(
                f"Failed to toggle AMPR PSU: {self._format_exception(exc)}"
                f"{self._runtime_diagnostics(device=device)}",
                flag=PRINT.ERROR,
            )
            return
        finally:
            self._end_transition()

        if status != device.NO_ERR:
            self.errorCount += 1
            self.print(
                f"Failed to toggle AMPR PSU: {self._format_status(status, device=device)}",
                flag=PRINT.ERROR,
            )

        if not state_updated:
            self._update_state()
        if self.controllerParent.isOn():
            if self.main_state != "ST_ON":
                self.errorCount += 1
                self._restore_off_ui_state()
                self.print(
                    "AMPR PSU ON sequence ended in an unexpected state: "
                    f"{self.main_state}.{self._runtime_diagnostics(device=device)}",
                    flag=PRINT.ERROR,
                )
                return
            start_acquisition = getattr(self, "startAcquisition", None)
            if callable(start_acquisition):
                start_acquisition()
            self.print("AMPR PSU turned ON. State: ST_ON.")

    def closeCommunication(self, *, final_state: str | None = None) -> None:
        base_close = getattr(super(), "closeCommunication", None)
        if callable(base_close):
            base_close()
        if final_state is None:
            final_state = getattr(self, "_forced_close_state", None) or "Disconnected"
        self.main_state = final_state
        self.detected_module_ids = []
        self.detected_modules_text = ""
        if hasattr(self, "controllerParent"):
            self.controllerParent.module_channel_counts = {}
            self.controllerParent.module_voltage_limits = {}
        summary_value = "n/a" if final_state == "Disconnected" else "Unknown"
        self.device_state_summary = summary_value
        self.interlock_state_summary = summary_value
        self.voltage_state_summary = summary_value
        self._sync_status_to_gui()
        self._dispose_device()
        self.initialized = False
        self._clear_transport_failures()
        self._forced_close_state = None

    def shutdownCommunication(self) -> bool:
        """Run the AMPR shutdown sequence before releasing communication resources."""
        device = self.device
        if device is None:
            self.closeCommunication()
            return True

        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False
        self.print("Starting AMPR shutdown sequence.")
        shutdown_confirmed = False
        confirmation_reason = "shutdown confirmation was not completed"
        try:
            with self._controller_lock_section(
                "Could not acquire lock to shut down the AMPR."
            ):
                device = self.device
                if device is None:
                    shutdown_confirmed = True
                else:
                    device.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            confirmation_reason = self._format_exception(exc)
            with contextlib.suppress(Exception):
                self._update_state()
            self.print(
                f"AMPR shutdown failed: {self._format_exception(exc)}"
                f"{self._runtime_diagnostics(device=device)}",
                flag=PRINT.ERROR,
            )
        else:
            shutdown_confirmed = True
            self.print("AMPR shutdown sequence completed.")
        finally:
            if not shutdown_confirmed:
                self.print(
                    "AMPR shutdown could not be confirmed before disconnect: "
                    f"{confirmation_reason}.",
                    flag=PRINT.ERROR,
                )
            self.closeCommunication(
                final_state=(
                    "Disconnected"
                    if shutdown_confirmed
                    else _AMPR_SHUTDOWN_UNCONFIRMED_STATE
                )
            )
        return shutdown_confirmed

    def _refresh_module_scan(self) -> None:
        if self.device is None:
            return

        try:
            status, mismatch, rating_failure = self.device.get_scanned_module_state()
        except Exception as exc:  # noqa: BLE001
            self.print(f"Could not query scanned AMPR module state: {exc}", flag=PRINT.WARNING)
            status = None
            mismatch = False
            rating_failure = False

        if (
            self.device is not None
            and status == self.device.NO_ERR
            and (mismatch or rating_failure)
        ):
            rescan_status = self.device.rescan_modules()
            if rescan_status != self.device.NO_ERR:
                raise RuntimeError(
                    f"AMPR rescan failed: {self._format_status(rescan_status)}"
                )
            persist_status = self.device.set_scanned_module_state()
            if persist_status != self.device.NO_ERR:
                raise RuntimeError(
                    "AMPR scanned module state could not be stored: "
                    f"{self._format_status(persist_status)}"
                )

        module_info = self.device.scan_modules()
        self.detected_module_ids = sorted(module_info)
        self.detected_modules_text = (
            ", ".join(str(module) for module in self.detected_module_ids)
            if self.detected_module_ids
            else "None"
        )
        self._refresh_module_capabilities()

        configured_modules = set(self.controllerParent.getConfiguredModules())
        current_items = self.controllerParent._current_channel_items()
        default_item = self.controllerParent._default_channel_item()
        if _looks_like_bootstrap_items(
            current_items,
            device_name=self.controllerParent.name,
            default_item=default_item,
        ):
            configured_modules.discard(0)
        missing_modules = sorted(configured_modules - set(self.detected_module_ids))
        if missing_modules:
            self.print(
                "Configured modules not detected during AMPR scan: "
                + ", ".join(str(module) for module in missing_modules),
                flag=PRINT.WARNING,
            )

    def _refresh_module_capabilities(self) -> None:
        """Update per-module voltage limits and channel counts from AMPR metadata."""
        if self.device is None:
            self.controllerParent.module_channel_counts = {}
            self.controllerParent.module_voltage_limits = {}
            return
        if not hasattr(self.device, "get_module_capabilities"):
            return

        try:
            module_capabilities = self.device.get_module_capabilities()
        except Exception as exc:  # noqa: BLE001
            self.print(
                f"Could not query AMPR module capabilities: {exc}",
                flag=PRINT.WARNING,
            )
            return

        channel_counts: dict[int, int] = {}
        limits: dict[int, float] = {}
        unresolved_limits: list[str] = []
        unresolved_channel_counts: list[str] = []
        for module, payload in cast("dict[Any, dict[str, Any]]", module_capabilities).items():
            module_id = _coerce_int(module, -1)
            if module_id < 0:
                continue
            status = payload.get("status")
            if status != getattr(self.device, "NO_ERR", status):
                continue
            channel_count = _coerce_int(payload.get("channel_count"), 0)
            if channel_count in _CHANNELS_PER_MODULE_OPTIONS:
                channel_counts[module_id] = channel_count
            else:
                unresolved_channel_counts.append(
                    f"{module_id} ({payload.get('product_id', 'unknown product')})"
                )
            rating = payload.get("voltage_rating")
            if rating is None:
                unresolved_limits.append(
                    f"{module_id} ({payload.get('product_id', 'unknown product')})"
                )
            else:
                limits[module_id] = min(
                    abs(_coerce_float(rating, _AMPR_ABS_VOLTAGE_LIMIT)),
                    _AMPR_ABS_VOLTAGE_LIMIT,
                )
        self.controllerParent.module_channel_counts = channel_counts
        self.controllerParent.module_voltage_limits = limits
        if unresolved_channel_counts:
            self.print(
                "Could not determine AMPR module channel counts for: "
                + ", ".join(unresolved_channel_counts)
                + ". Falling back to 4 channels.",
                flag=PRINT.WARNING,
            )
        if unresolved_limits:
            self.print(
                "Could not determine AMPR module voltage ratings for: "
                + ", ".join(unresolved_limits)
                + ". Falling back to ±1000 V.",
                flag=PRINT.WARNING,
            )

    def _update_state(self) -> None:
        if self.device is None:
            self.main_state = "Disconnected"
            self.device_state_summary = "n/a"
            self.interlock_state_summary = "n/a"
            self.voltage_state_summary = "n/a"
            self._clear_transport_failures()
            return

        try:
            with self._controller_lock_section(
                "Could not acquire lock to refresh the AMPR state."
            ):
                device = self.device
                if device is None:
                    self.main_state = "Disconnected"
                    self.device_state_summary = "n/a"
                    self.interlock_state_summary = "n/a"
                    self.voltage_state_summary = "n/a"
                    self._clear_transport_failures()
                    return
                status, _state_hex, state_name = device.get_state()
        except TimeoutError:
            # Transient controller-lock contention (e.g. a voltage ramp holding
            # the lock); skip this refresh and keep the last state. Not counted
            # as an error: a real device fault is handled by except-Exception.
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            failure_count = self._note_transport_failure()
            transport_unusable = _transport_failure_is_fatal(exc)
            transport_lost = transport_unusable or (
                failure_count >= _AMPR_TRANSPORT_FAILURE_THRESHOLD
            )
            self.main_state = (
                _AMPR_COMMUNICATION_LOST_STATE if transport_lost else "State error"
            )
            self.print(f"Failed to read AMPR state: {exc}", flag=PRINT.ERROR)
            self.device_state_summary = (
                self._safe_query_state("get_device_state") or "Unknown"
            )
            self.interlock_state_summary = (
                self._safe_query_state("get_interlock_state") or "Unknown"
            )
            self.voltage_state_summary = (
                self._safe_query_state("get_voltage_state") or "Unknown"
            )
            if transport_lost:
                self._handle_transport_loss()
            return

        self._clear_transport_failures()
        if status == device.NO_ERR:
            self.main_state = state_name
        else:
            self.main_state = "State error"
            self.errorCount += 1
            self.print(
                f"Failed to read AMPR state: {self._format_status(status, device=device)}",
                flag=PRINT.ERROR,
            )
        self.device_state_summary = self._safe_query_state("get_device_state") or "Unknown"
        self.interlock_state_summary = (
            self._safe_query_state("get_interlock_state") or "Unknown"
        )
        self.voltage_state_summary = self._safe_query_state("get_voltage_state") or "Unknown"

    def _handle_transport_loss(self) -> None:
        """Force immediate AMPR teardown after repeated transport failures."""
        if (
            getattr(self, "_forced_close_state", None) == _AMPR_COMMUNICATION_LOST_STATE
            and self.device is None
        ):
            return

        self.print(
            "Communication with the AMPR high-voltage amplifier was lost. "
            "OUTPUTS MAY REMAIN ENERGIZED AT THEIR LAST SETPOINT because the "
            "device can no longer be commanded to disable them. Manually verify "
            "all outputs are OFF via the front panel / hardware interlock before "
            "approaching the device.",
            flag=PRINT.ERROR,
        )

        # Cancel any in-flight voltage ramp so the background toggle thread stops
        # commanding an amplifier whose transport is gone.
        self._cancel_ramp = True
        self.main_state = _AMPR_COMMUNICATION_LOST_STATE
        self.device_state_summary = "Unknown"
        self.interlock_state_summary = "Unknown"
        self.voltage_state_summary = "Unknown"
        self._forced_close_state = _AMPR_COMMUNICATION_LOST_STATE
        self.acquiring = False
        self.initialized = False
        self.ramping = False
        self._clear_transport_failures()
        self._end_transition()
        self._restore_off_ui_state()
        self._dispose_device()
        self._sync_status_to_gui()
        close_signal = getattr(getattr(self, "signalComm", None), "closeCommunicationSignal", None)
        emit = getattr(close_signal, "emit", None)
        if callable(emit):
            emit()

    def _sync_status_to_gui(self) -> None:
        self.controllerParent.main_state = self.main_state
        self.controllerParent.detected_modules = self.detected_modules_text
        self.controllerParent.device_state_summary = self.device_state_summary
        self.controllerParent.interlock_state_summary = self.interlock_state_summary
        self.controllerParent.voltage_state_summary = self.voltage_state_summary
        def _refresh_gui() -> None:
            sync_acquisition_controls = getattr(
                self.controllerParent,
                "_sync_acquisition_controls",
                None,
            )
            if callable(sync_acquisition_controls):
                sync_acquisition_controls()
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

    def _note_transport_failure(self) -> int:
        failures = int(getattr(self, "_consecutive_transport_failures", 0)) + 1
        self._consecutive_transport_failures = failures
        return failures

    def _clear_transport_failures(self) -> None:
        self._consecutive_transport_failures = 0

    def _dispose_device(self) -> None:
        import gc

        device = self.device
        self.device = None
        self.initialized = False
        if device is None:
            return

        try:
            device.disconnect()
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                device.close()
            except Exception:  # noqa: BLE001
                pass
            with contextlib.suppress(Exception):
                device._set_port_claimed(False)
        del device
        gc.collect()

    def _format_status(self, status: int, device: Any | None = None) -> str:
        device = self.device if device is None else device
        if device is None:
            return str(status)
        try:
            return str(device.format_status(status))
        except Exception:  # noqa: BLE001
            return str(status)

    def _safe_query_state(self, getter_name: str, device: Any | None = None) -> str | None:
        device = self.device if device is None else device
        if device is None:
            return None
        getter = getattr(device, getter_name, None)
        if getter is None:
            return None
        try:
            status, _state_hex, state = getter(timeout_s=float(self.controllerParent.poll_timeout_s))
        except Exception:  # noqa: BLE001
            return None
        if status != getattr(device, "NO_ERR", status):
            return None
        if isinstance(state, list):
            return ", ".join(str(entry) for entry in state) if state else "OK"
        return str(state)

    def _runtime_diagnostics(self, device: Any | None = None) -> str:
        diagnostics: list[str] = []
        for label, getter_name in (
            ("main state", "get_state"),
            ("device state", "get_device_state"),
            ("voltage state", "get_voltage_state"),
            ("interlock state", "get_interlock_state"),
        ):
            state = self._safe_query_state(getter_name, device=device)
            if state:
                diagnostics.append(f"{label}: {state}")
        if not diagnostics:
            return ""
        return " (" + "; ".join(diagnostics) + ")"

    def _restore_off_ui_state(self) -> None:
        """Reset toolbar ON/OFF widgets back to OFF after a failed startup."""
        sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_on_state):
            sync_on_state(False)
            return
        if hasattr(self.controllerParent, "onAction"):
            self.controllerParent.onAction.state = False
        sync_local = getattr(self.controllerParent, "_sync_local_on_action", None)
        if callable(sync_local):
            sync_local()

    def _restore_on_ui_state(self) -> None:
        """Restore toolbar ON/OFF widgets back to ON after a failed shutdown."""
        sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_on_state):
            sync_on_state(True)
            return
        if hasattr(self.controllerParent, "onAction"):
            self.controllerParent.onAction.state = True
        sync_local = getattr(self.controllerParent, "_sync_local_on_action", None)
        if callable(sync_local):
            sync_local()

    @contextlib.contextmanager
    def _controller_lock_section(
        self,
        timeout_message: str,
        *,
        already_acquired: bool = False,
        timeout_s: float = 1.0,
        log_timeout: bool = True,
    ):
        """Acquire the controller lock without swallowing hardware exceptions."""
        lock = getattr(self, "lock", None)
        if lock is None:
            lock = Lock()
            self.lock = lock

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

        with lock.acquire_timeout(
            float(timeout_s),
            timeoutMessage=timeout_message if log_timeout else "",
        ) as lock_acquired:
            if not lock_acquired:
                raise TimeoutError(timeout_message)
            yield

    def _begin_transition(self, target_on: bool) -> bool:
        """Mark a global AMPR ON/OFF transition as active."""
        with self._transition_guard():
            if self.transitioning:
                return False
            self.transitioning = True
            self.transition_target_on = bool(target_on)
            return True

    def _end_transition(self) -> None:
        """Clear transition bookkeeping after a global AMPR ON/OFF sequence."""
        with self._transition_guard():
            self.transitioning = False
            self.transition_target_on = None

    def _channel_target_voltages(
        self,
        *,
        respect_device_state: bool,
    ) -> dict[tuple[int, int], float]:
        """Return target voltages keyed by (module, channel)."""
        targets: dict[tuple[int, int], float] = {}
        device_is_on = getattr(self.controllerParent, "isOn", lambda: False)()
        get_channels = getattr(self.controllerParent, "getChannels", None)
        if not callable(get_channels):
            return targets
        for channel in get_channels():
            if not channel.real:
                continue
            target_voltage = 0.0
            if channel.enabled and (device_is_on or not respect_device_state):
                target_voltage = float(channel.value)
            targets[(channel.module_address(), channel.channel_number())] = target_voltage
        return targets

    @staticmethod
    def _group_target_voltages(
        targets: dict[tuple[int, int], float],
    ) -> dict[int, dict[int, float]]:
        """Group per-channel targets by module."""
        grouped_targets: dict[int, dict[int, float]] = {}
        for (module, channel_id), voltage in targets.items():
            grouped_targets.setdefault(module, {})[channel_id] = float(voltage)
        return grouped_targets

    def _apply_target_voltages_locked(
        self,
        targets: dict[tuple[int, int], float],
        *,
        device: Any,
    ) -> None:
        """Apply a full AMPR target map while the controller lock is held."""
        for module, module_targets in sorted(self._group_target_voltages(targets).items()):
            voltage_limit = self._module_voltage_limit(module)
            for channel_id, voltage in sorted(module_targets.items()):
                if abs(float(voltage)) > voltage_limit:
                    raise RuntimeError(
                        f"Refusing {float(voltage):.3f} V for module {module} CH{channel_id}: "
                        f"detected module rating is ±{voltage_limit:.0f} V."
                    )
            if hasattr(device, "set_module_voltages"):
                statuses = device.set_module_voltages(module, module_targets)
                for channel_id, status in statuses.items():
                    if status != device.NO_ERR:
                        raise RuntimeError(
                            "AMPR rejected "
                            f"{float(module_targets[channel_id]):.3f} V for module "
                            f"{module} CH{channel_id}: {self._format_status(status, device=device)}"
                        )
                continue

            for channel_id, voltage in sorted(module_targets.items()):
                status = device.set_module_voltage(module, channel_id, voltage)
                if status != device.NO_ERR:
                    raise RuntimeError(
                        "AMPR rejected "
                        f"{float(voltage):.3f} V for module "
                        f"{module} CH{channel_id}: {self._format_status(status, device=device)}"
                    )

    def _apply_target_voltages(
        self,
        targets: dict[tuple[int, int], float],
        *,
        timeout_message: str,
    ) -> None:
        """Apply a full AMPR target map under the controller lock."""
        if not targets:
            return

        with self._controller_lock_section(timeout_message):
            device = self.device
            if device is None:
                raise RuntimeError("AMPR device is not available.")
            self._apply_target_voltages_locked(targets, device=device)

    def _ramp_target_voltages(
        self,
        *,
        start_targets: dict[tuple[int, int], float],
        end_targets: dict[tuple[int, int], float],
        rate_v_s: float,
        label: str,
    ) -> None:
        """Ramp all AMPR output targets simultaneously."""
        output_keys = sorted(set(start_targets) | set(end_targets))
        if not output_keys:
            return

        normalized_start = {
            key: float(start_targets.get(key, 0.0))
            for key in output_keys
        }
        normalized_end = {
            key: float(end_targets.get(key, 0.0))
            for key in output_keys
        }
        max_delta = max(
            abs(normalized_end[key] - normalized_start[key]) for key in output_keys
        )
        if max_delta <= 0.0:
            return

        if rate_v_s <= 0.0:
            self._apply_target_voltages(
                normalized_end,
                timeout_message="Could not acquire lock to apply AMPR voltages.",
            )
            return

        estimated_duration_s = max_delta / rate_v_s
        steps = max(1, int(np.ceil(estimated_duration_s / _AMPR_RAMP_STEP_S)))
        self.print(
            f"Starting AMPR ramp-{label} at {rate_v_s:.1f} V/s "
            f"(estimated {estimated_duration_s:.1f} s)."
        )
        self._cancel_ramp = False
        self.ramping = True
        try:
            for step in range(1, steps + 1):
                if getattr(self, "_cancel_ramp", False):
                    self.print(
                        f"AMPR ramp-{label} cancelled before completion.",
                        flag=PRINT.WARNING,
                    )
                    return
                fraction = step / steps
                step_targets = {
                    key: normalized_start[key]
                    + (normalized_end[key] - normalized_start[key]) * fraction
                    for key in output_keys
                }
                self._apply_target_voltages(
                    step_targets,
                    timeout_message="Could not acquire lock to apply AMPR ramp step.",
                )
                if step < steps:
                    time.sleep(_AMPR_RAMP_STEP_S)
        finally:
            self.ramping = False
        if getattr(self.controllerParent, "isOn", lambda: False)():
            updated_targets = self._channel_target_voltages(respect_device_state=True)
            if updated_targets != normalized_end:
                self.print("Applying updated AMPR targets queued during ramp.")
                self._apply_target_voltages(
                    updated_targets,
                    timeout_message="Could not acquire lock to apply queued AMPR targets.",
                )
        self.print(f"AMPR ramp-{label} completed.")

    def _safe_disable_after_toggle_failure(
        self,
        targets: dict[tuple[int, int], float],
    ) -> None:
        """Best-effort cleanup after a failed AMPR startup/ramp sequence."""
        device = self.device
        if device is None:
            return

        cleanup_errors: list[str] = []
        zero_targets = {key: 0.0 for key in targets}
        try:
            with self._controller_lock_section(
                "Could not acquire lock for AMPR failure cleanup."
            ):
                device = self.device
                if device is None:
                    cleanup_errors.append("device disappeared")
                else:
                    if zero_targets:
                        try:
                            self._apply_target_voltages_locked(zero_targets, device=device)
                        except Exception as cleanup_exc:  # noqa: BLE001
                            cleanup_errors.append(f"zeroing failed: {cleanup_exc}")
                    try:
                        status, _enabled = device.enable_psu(False)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_errors.append(f"disable_psu failed: {cleanup_exc}")
                    else:
                        if status != device.NO_ERR:
                            cleanup_errors.append(
                                f"disable_psu failed: {self._format_status(status, device=device)}"
                            )
        except TimeoutError:
            cleanup_errors.append("lock timeout")

        if cleanup_errors:
            self.print(
                "AMPR startup cleanup encountered issues: " + "; ".join(cleanup_errors),
                flag=PRINT.WARNING,
            )
            return
        self.print("AMPR startup cleanup disabled the PSU after failure.", flag=PRINT.WARNING)

    def _format_exception(self, exc: Exception) -> str:
        message = str(exc).strip()
        lower_message = message.lower()
        controller_parent = getattr(self, "controllerParent", None)
        com_number = _coerce_int(getattr(controller_parent, "com", None), 0)

        if "timed out during 'open_port'" in lower_message:
            hint = (
                f" Selected COM{com_number} did not respond. Check that the AMPR is "
                "powered, that the configured COM port is correct, and that no other "
                "application is holding the port."
            )
            message = f"{message}{hint}"
        elif "open_port failed:" in lower_message and "error opening port" in lower_message:
            hint = (
                f" Windows could not open COM{com_number}. The port is likely wrong, "
                "already in use, or stale after a previous connection failure. Close "
                "other serial tools and replug or power-cycle the AMPR before retrying."
            )
            message = f"{message}{hint}"

        if message:
            return f"{type(exc).__name__}: {message}"
        return repr(exc)
