"""Read DMMR module currents and monitor live picoammeter measurements."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import sys
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

try:
    from esibd.core import LabviewDoubleSpinBox
except ImportError:  # pragma: no cover - exercised by lightweight plugin stubs
    class LabviewDoubleSpinBox:  # type: ignore[override]
        """Fallback used by unit-test stubs that do not expose the real widget."""

        def __init__(self, *args, **kwargs) -> None:
            self.NAN = "NaN"

_BUNDLED_RUNTIME_DIRNAME = "runtime"
_BUNDLED_RUNTIME_NAMESPACE_PREFIX = "_esibd_bundled_dmmr_runtime"
_DMMR_DRIVER_CLASS: type[Any] | None = None
_CHANNEL_NAME_KEY = getattr(Parameter, "NAME", getattr(Channel, "NAME", "Name"))
_CHANNEL_ENABLED_KEY = getattr(Channel, "ENABLED", "Enabled")
_CHANNEL_REAL_KEY = getattr(Channel, "REAL", "Real")
_PARAMETER_ADVANCED_KEY = getattr(Parameter, "ADVANCED", "Advanced")
_PARAMETER_TOOLTIP_KEY = getattr(Parameter, "TOOLTIP", "Tooltip")
_PARAMETER_EVENT_KEY = getattr(Parameter, "EVENT", "Event")
_DMMR_MODULE_KEY = "Module"
_DMMR_MIN_ROW_HEIGHT = 28
_DMMR_COMMUNICATION_LOST_STATE = "Communication lost"
_DMMR_SHUTDOWN_UNCONFIRMED_STATE = "Shutdown unconfirmed"
_DMMR_ATTENTION_STATE_TOKENS = ("error", "unconfirmed", "lost")
_DMMR_TRANSPORT_FAILURE_THRESHOLD = 3
_DMMR_POWER_ON_ICON = "switch-medium_on.png"
_DMMR_POWER_OFF_ICON = "switch-medium_off.png"
_DMMR_NEUTRAL_WIDGET_STYLE = (
    "background: transparent;"
    "background-color: transparent;"
    "border: none;"
    "QWidget { background: transparent; background-color: transparent; border: none; }"
    "QFrame { background: transparent; background-color: transparent; border: none; }"
    "QLabel { background: transparent; background-color: transparent; border: none; }"
    "QLineEdit { background: transparent; background-color: transparent; border: none; }"
    "QCheckBox { background: transparent; background-color: transparent; border: none; }"
)
_DMMR_TOGGLE_BUTTON_STYLE = (
    "QToolButton { margin: 0px; padding: 0px 6px; }"
    "QToolButton:checked { background-color: #1f2933; color: #ffffff; }"
)
_DMMR_PANEL_CARD_ACTIVE_STYLE = (
    "QFrame {"
    " background-color: #162433;"
    " border: 1px solid #3182ce;"
    " border-radius: 8px;"
    " color: #f7fafc;"
    "}"
)
_DMMR_PANEL_CARD_MUTED_STYLE = (
    "QFrame {"
    " background-color: #202938;"
    " border: 1px solid #64748b;"
    " border-radius: 8px;"
    " color: #f7fafc;"
    "}"
)
_DMMR_PANEL_CARD_DISCONNECTED_STYLE = (
    "QFrame {"
    " background-color: #151b26;"
    " border: 1px solid #475569;"
    " border-radius: 8px;"
    " color: #e2e8f0;"
    "}"
)
_DMMR_PANEL_TITLE_STYLE = "color: #f8fafc; font-weight: 700; font-size: 14px;"
_DMMR_PANEL_CURRENT_LABEL_STYLE = "color: #cbd5e1; font-weight: 600;"
_DMMR_PANEL_CURRENT_VALUE_STYLE = "color: #f8fafc; font-weight: 700; font-size: 18px;"
_DMMR_PANEL_BADGE_READ_STYLE = (
    "background-color: #1f2933; color: #ffffff; margin:0px; padding:0px 6px;"
)
_DMMR_PANEL_BADGE_MUTED_STYLE = (
    "background-color: #4a5568; color: #ffffff; margin:0px; padding:0px 6px;"
)
_DMMR_PANEL_BADGE_OFF_STYLE = (
    "background-color: #718096; color: #ffffff; margin:0px; padding:0px 6px;"
)
_DMMR_PANEL_READ_BUTTON_STYLE = (
    "QPushButton {"
    " background-color: #334155;"
    " color: #f8fafc;"
    " border: 1px solid #475569;"
    " border-radius: 6px;"
    " padding: 4px 10px;"
    "}"
    "QPushButton:checked {"
    " background-color: #0f172a;"
    " color: #ffffff;"
    " border: 1px solid #94a3b8;"
    "}"
    "QPushButton:disabled {"
    " background-color: #1f2937;"
    " color: #94a3b8;"
    " border: 1px solid #374151;"
    "}"
)
_DMMR_PANEL_EMPTY_STYLE = "color: #718096; font-style: italic; padding: 8px 0px;"
_DMMR_PANEL_CARD_MIN_WIDTH = 220
_DMMR_PANEL_CARD_MAX_WIDTH = 260
_DMMR_PANEL_GRID_COLUMNS = 3


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
    """Return True when a transport exception clearly indicates a dead backend."""
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


# A timed-out open_port poisons the COM port for the lifetime of the ESIBD
# Explorer process: the blocked vendor-DLL call keeps an exclusive OS handle, so
# no new instance can reopen it and every later retry fails with ERR_OPEN (-2)
# even once the device is powered on. The operator-facing guidance below makes
# that explicit instead of letting the user loop on a confusing
# 'Error opening port' message while the hardware is actually fine.
_DMMR_POISONED_PORT_RECOVERY = (
    "The COM port is now locked inside this ESIBD Explorer process: the timed-out "
    "attempt left a blocked vendor-DLL call holding an exclusive handle to the port. "
    "This instance can no longer reopen it, so every later retry will keep failing "
    "with 'Error opening port' (-2) even once the device is powered on. Power the "
    "device on, then RESTART ESIBD Explorer to release the port before trying again."
)
_DMMR_POISONED_PORT_RETRY = (
    "This retry is failing because an earlier timed-out connection attempt locked "
    "the COM port inside this ESIBD Explorer process. The device may well be powered "
    "on now, but the port cannot be reopened from this instance. RESTART ESIBD "
    "Explorer to release the port and retry."
)


def _dmmr_poisoned_port_guidance(
    exc: Exception,
    *,
    poisoned_com: int | None,
    current_com: int | None,
) -> str:
    """Return operator guidance for a failed DMMR init, or "" when none applies.

    A timed-out open_port poisons the COM port for the lifetime of the process
    (the blocked vendor-DLL thread keeps an exclusive OS handle), so no new
    instance can reopen it and every retry fails with ERR_OPEN (-2). Surface
    that instead of letting the operator loop on a confusing 'Error opening
    port' while the hardware is actually fine.
    """
    if _transport_failure_is_fatal(exc):
        return _DMMR_POISONED_PORT_RECOVERY
    if (
        poisoned_com is not None
        and current_com is not None
        and int(poisoned_com) == int(current_com)
    ):
        return _DMMR_POISONED_PORT_RETRY
    return ""


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


def _format_si_current(value_amps: Any) -> tuple[str, str]:
    """Format a current stored in amps using a readable SI prefix."""
    value = _coerce_float(value_amps, np.nan)
    if _is_nan(value):
        return ("NaN", "NaN")

    abs_value = abs(value)
    if abs_value == 0:
        scaled_value, unit = 0.0, "A"
    elif abs_value >= 1.0:
        scaled_value, unit = value, "A"
    elif abs_value >= 1e-3:
        scaled_value, unit = value * 1e3, "mA"
    elif abs_value >= 1e-6:
        scaled_value, unit = value * 1e6, "uA"
    elif abs_value >= 1e-9:
        scaled_value, unit = value * 1e9, "nA"
    elif abs_value >= 1e-12:
        scaled_value, unit = value * 1e12, "pA"
    else:
        scaled_value, unit = value * 1e15, "fA"

    if scaled_value == 0:
        number_text = "0"
    else:
        number_text = f"{scaled_value:.3f}".rstrip("0").rstrip(".")
    return (f"{number_text} {unit}", f"{value:.6e} A")


def _state_requires_operator_attention(state: Any) -> bool:
    """Return True when the raw state should stay visible even if the UI toggle is OFF."""
    normalized = str(state or "").strip().lower()
    return any(token in normalized for token in _DMMR_ATTENTION_STATE_TOKENS)


def _dmmr_panel_card_style(*, connected: bool, reading: bool) -> str:
    if not connected:
        return _DMMR_PANEL_CARD_DISCONNECTED_STYLE
    if reading:
        return _DMMR_PANEL_CARD_ACTIVE_STYLE
    return _DMMR_PANEL_CARD_MUTED_STYLE


def _dmmr_panel_badge_style(state: str) -> str:
    normalized = str(state or "").strip().lower()
    if normalized == "read":
        return _DMMR_PANEL_BADGE_READ_STYLE
    if normalized == "muted":
        return _DMMR_PANEL_BADGE_MUTED_STYLE
    return _DMMR_PANEL_BADGE_OFF_STYLE


class _DMMRCurrentMonitorSpinBox(LabviewDoubleSpinBox):
    """Numeric indicator widget that renders DMMR currents with SI prefixes."""

    def __init__(self, indicator: bool = False, displayDecimals: int = 2) -> None:
        super().__init__(indicator=indicator, displayDecimals=displayDecimals)
        set_decimals = getattr(self, "setDecimals", None)
        if callable(set_decimals):
            # Preserve picoamp-scale values internally while still rendering a compact SI string.
            set_decimals(1000)

    def textFromValue(self, value: float) -> str:  # noqa: N802
        numeric_value = _coerce_float(value, np.nan)
        if _is_nan(numeric_value) or np.isinf(numeric_value):
            return getattr(self, "NAN", "NaN")
        formatted_text, _raw_text = _format_si_current(numeric_value)
        return formatted_text


def _module_key_from_item(item: dict[str, Any]) -> int:
    """Return the physical DMMR module addressed by one channel item."""
    return _coerce_int(item.get(_DMMR_MODULE_KEY), 0)


def _generic_channel_name(device_name: str, module: int) -> str:
    """Generate a stable generic channel name from the physical mapping."""
    return f"{device_name}_M{module:02d}"


def _build_generic_channel_item(
    device_name: str,
    module: int,
    default_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a generic channel config for a newly detected DMMR module."""
    item = dict(default_item or {})
    item[_CHANNEL_NAME_KEY] = _generic_channel_name(device_name, module)
    item[_DMMR_MODULE_KEY] = str(module)
    item[_CHANNEL_REAL_KEY] = True
    item[_CHANNEL_ENABLED_KEY] = True
    return item


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
        return all(_module_key_from_item(item) == 0 for item in items)

    for item in items:
        for key, default_value in default_item.items():
            if key == _CHANNEL_NAME_KEY:
                continue
            item_value = item.get(key, default_value)
            if key == _DMMR_MODULE_KEY:
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
    """Remove stale DMMR1..N bootstrap channels from polluted configs."""
    if not items or default_item is None:
        return items, []

    default_key = _module_key_from_item(default_item)
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
    if any(_module_key_from_item(item) != default_key for item in residue_items):
        return items, []

    cleaned_items = [
        item for index, item in enumerate(items) if index not in set(residue_indices)
    ]
    if not cleaned_items:
        return items, []

    return cleaned_items, [
        (
            f"Removed legacy DMMR bootstrap channels: "
            f"{device_name}1..{device_name}{residue_count}",
            None,
        )
    ]


def _plan_channel_sync(
    current_items: list[dict[str, Any]],
    detected_modules: list[int],
    device_name: str,
    default_item: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[tuple[str, PRINT | None]]]:
    """Return the target channel config and corresponding sync log entries."""
    detected_modules = sorted({_coerce_int(module, -1) for module in detected_modules if _coerce_int(module, -1) >= 0})
    if not detected_modules:
        return current_items, []

    if _looks_like_bootstrap_items(current_items, device_name, default_item=default_item):
        bootstrap_items = [
            _build_generic_channel_item(
                device_name,
                module,
                default_item=default_item,
            )
            for module in detected_modules
        ]
        return bootstrap_items, [
            ("DMMR bootstrap config replaced from hardware scan.", None)
        ]

    current_items, cleanup_logs = _strip_legacy_bootstrap_residue(
        current_items,
        device_name=device_name,
        default_item=default_item,
    )

    detected_set = set(detected_modules)
    kept_keys: set[int] = set()
    added_modules: set[int] = set()
    virtualized_modules: set[int] = set()
    reactivated_modules: set[int] = set()
    duplicate_entries: list[tuple[str, int]] = []
    synced_items: list[dict[str, Any]] = []

    for item in current_items:
        synced_item = dict(item)
        module = _module_key_from_item(synced_item)
        if module in kept_keys:
            duplicate_entries.append((str(synced_item.get(_CHANNEL_NAME_KEY, "")), module))
            synced_item[_CHANNEL_REAL_KEY] = False
            synced_items.append(synced_item)
            continue

        kept_keys.add(module)
        if module in detected_set:
            if not _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                reactivated_modules.add(module)
            synced_item[_CHANNEL_REAL_KEY] = True
        else:
            if _coerce_bool(synced_item.get(_CHANNEL_REAL_KEY), default=True):
                virtualized_modules.add(module)
            synced_item[_CHANNEL_REAL_KEY] = False
        synced_items.append(synced_item)

    for module in detected_modules:
        if module in kept_keys:
            continue
        synced_items.append(
            _build_generic_channel_item(
                device_name,
                module,
                default_item=default_item,
            )
        )
        added_modules.add(module)

    log_entries: list[tuple[str, PRINT | None]] = list(cleanup_logs)
    if added_modules:
        log_entries.append(
            (
                "Added generic DMMR channels for detected modules: "
                + ", ".join(str(module) for module in sorted(added_modules)),
                None,
            )
        )
    if virtualized_modules:
        log_entries.append(
            (
                "Marked DMMR channels virtual because modules are absent: "
                + ", ".join(str(module) for module in sorted(virtualized_modules)),
                None,
            )
        )
    if reactivated_modules:
        log_entries.append(
            (
                "Reactivated DMMR channels for modules: "
                + ", ".join(str(module) for module in sorted(reactivated_modules)),
                None,
            )
        )
    for channel_name, module in duplicate_entries:
        log_entries.append(
            (
                f"Duplicate DMMR mapping detected for module {module}: {channel_name}",
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
            f"Could not create an import spec for bundled DMMR runtime at {package_dir}."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise


def _get_dmmr_driver_class() -> type[Any]:
    """Load the DMMR driver lazily from the bundled runtime only."""
    global _DMMR_DRIVER_CLASS

    if _DMMR_DRIVER_CLASS is not None:
        return _DMMR_DRIVER_CLASS

    plugin_dir = Path(__file__).resolve().parent
    bundled_runtime_dir = plugin_dir / "vendor" / _BUNDLED_RUNTIME_DIRNAME
    bundled_runtime_init = bundled_runtime_dir / "__init__.py"
    if not bundled_runtime_init.exists():
        raise ModuleNotFoundError(
            "Bundled DMMR runtime not found in vendor/runtime; "
            "plugin installation is incomplete."
        )

    runtime_module_name = _bundled_runtime_module_name(plugin_dir)
    _load_private_runtime_package(runtime_module_name, bundled_runtime_dir)
    module = importlib.import_module(f"{runtime_module_name}.dmmr")
    _DMMR_DRIVER_CLASS = cast(type[Any], module.DMMR)
    return _DMMR_DRIVER_CLASS


def providePlugins() -> "list[type[Plugin]]":
    """Return the plugins provided by this module."""
    return [DMMRDevice]


class DMMRDevice(Device):
    """Read DMMR module currents and expose live current monitors."""

    documentation = (
        "Reads DMMR module currents and exposes live picoammeter measurements."
    )

    name = "DMMR"
    version = "0.1.0"
    supportedVersion = "1.0.1"
    pluginType = PLUGINTYPE.INPUTDEVICE
    unit = "A"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "dmmr.png"
    channels: "list[DMMRChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    POLL_TIMEOUT = "Poll timeout (s)"
    STATE = "State"
    DETECTED_MODULES = "Detected modules"

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = DMMRChannel

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
        self.controller = DMMRController(controllerParent=self)
        self._hide_channel_table()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()

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
        self._hide_channel_table()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()
        self._update_channel_column_visibility()
        self._sync_acquisition_controls()

    def getChannels(self) -> "list[DMMRChannel]":
        return cast("list[DMMRChannel]", super().getChannels())

    com: int
    baudrate: int
    connect_timeout_s: float
    poll_timeout_s: float
    main_state: str
    detected_modules: str
    device_state_summary: str
    voltage_state_summary: str
    temperature_state_summary: str

    def getConfiguredModules(self) -> list[int]:
        """Return sorted module addresses referenced by real channels."""
        return sorted(
            {channel.module_address() for channel in self.getChannels() if channel.real}
        )

    def _current_channel_items(self) -> list[dict[str, Any]]:
        """Snapshot current channels into config dictionaries."""
        return [channel.asDict() for channel in self.getChannels()]

    def _default_channel_template(self) -> dict[str, dict[str, Any]]:
        """Return the default DMMR channel parameter definitions."""
        return self.channelType(channelParent=self, tree=None).getSortedDefaultChannel()

    def _default_channel_item(self) -> dict[str, Any]:
        """Return the persisted default DMMR channel configuration."""
        return self.channelType(channelParent=self, tree=None).asDict()

    def _ensure_local_on_action(self) -> None:
        """Expose the global DMMR ON/OFF control directly in the plugin toolbar."""
        if (
            not self.useOnOffLogic
            or hasattr(self, "deviceOnAction")
            or not hasattr(self, "closeCommunicationAction")
        ):
            return

        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(on=checked),
            toolTipFalse=f"Turn {self.name} ON.",
            iconFalse=self.makeIcon(_DMMR_POWER_ON_ICON),
            toolTipTrue=f"Turn {self.name} OFF and disconnect.",
            iconTrue=self.makeIcon(_DMMR_POWER_OFF_ICON),
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
        """Hide generic channel-table actions superseded by the DMMR panel."""
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

    def _panel_channels(self) -> "list[DMMRChannel]":
        return sorted(
            [channel for channel in self.getChannels() if channel.real],
            key=lambda channel: channel.module_address(),
        )

    def _channel_by_module(self, module: int) -> "DMMRChannel | None":
        for channel in self.getChannels():
            if channel.real and channel.module_address() == module:
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

    def _channel_enabled_checked(self, channel: Any | None) -> bool:
        if channel is None:
            return False
        getter = getattr(channel, "getParameterByName", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(getattr(channel, "ENABLED", "Enabled"))
                if parameter is not None:
                    return _coerce_bool(
                        getattr(parameter, "value", getattr(channel, "enabled", False)),
                        _coerce_bool(getattr(channel, "enabled", False), False),
                    )
        return _coerce_bool(getattr(channel, "enabled", False), False)

    def _clear_channel_panel_layout(self, layout: Any) -> None:
        if layout is None:
            return
        count = getattr(layout, "count", None)
        take_at = getattr(layout, "takeAt", None)
        if not callable(count) or not callable(take_at):
            return
        while count():
            item = take_at(0)
            if item is None:
                continue
            child_layout = getattr(item, "layout", lambda: None)()
            if child_layout is not None:
                self._clear_channel_panel_layout(child_layout)
            widget = getattr(item, "widget", lambda: None)()
            if widget is not None:
                if hasattr(widget, "setParent"):
                    widget.setParent(None)
                delete_later = getattr(widget, "deleteLater", None)
                if callable(delete_later):
                    delete_later()

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

    def _set_channel_panel_button(self, button: Any, *, checked: bool, enabled: bool) -> None:
        if button is None:
            return
        block_signals = getattr(button, "blockSignals", None)
        if callable(block_signals):
            block_signals(True)
        try:
            set_checked = getattr(button, "setChecked", None)
            if callable(set_checked):
                set_checked(bool(checked))
            else:
                button.checked = bool(checked)
            set_enabled = getattr(button, "setEnabled", None)
            if callable(set_enabled):
                set_enabled(bool(enabled))
            else:
                button.enabled = bool(enabled)
        finally:
            if callable(block_signals):
                block_signals(False)

    def _channel_panel_display_toggled(self, module: int, checked: bool) -> None:
        channel = self._channel_by_module(module)
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

    def _channel_panel_read_toggled(self, module: int, checked: bool) -> None:
        channel = self._channel_by_module(module)
        if channel is None:
            return

        getter = getattr(channel, "getParameterByName", None)
        if callable(getter):
            with contextlib.suppress(Exception):
                parameter = getter(getattr(channel, "ENABLED", "Enabled"))
                if parameter is not None:
                    if hasattr(parameter, "setValueWithoutEvents"):
                        parameter.setValueWithoutEvents(bool(checked))
                    else:
                        parameter.value = bool(checked)
        channel.enabled = bool(checked)
        enabled_changed = getattr(channel, "enabledChanged", None)
        if callable(enabled_changed):
            enabled_changed()
        self._update_channel_panel()

    def _channel_panel_snapshot(self, module: int) -> dict[str, Any]:
        controller = getattr(self, "controller", None)
        channel = self._channel_by_module(module)
        raw_state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        connected = raw_state not in {"Disconnected", _DMMR_COMMUNICATION_LOST_STATE}
        is_on = getattr(self, "isOn", None)
        device_on = connected and callable(is_on) and bool(is_on())
        read_checked = self._channel_enabled_checked(channel)
        current_value = (
            (getattr(controller, "values", {}) or {}).get(module, np.nan)
            if device_on and read_checked
            else np.nan
        )
        current_text, raw_text = _format_si_current(current_value)
        if not device_on:
            state_text = "OFF" if connected else "Disconnected"
            current_text = "n/a"
            raw_text = "n/a"
        elif not read_checked:
            state_text = "Muted"
            current_text = "Muted"
            raw_text = "Read disabled"
        else:
            state_text = "Read"

        return {
            "title": f"Module {module}",
            "state_text": state_text,
            "state_style": _dmmr_panel_badge_style(state_text),
            "card_style": _dmmr_panel_card_style(
                connected=connected,
                reading=bool(device_on and read_checked),
            ),
            "current_text": current_text,
            "current_tooltip": raw_text,
            "read_checked": read_checked,
            "read_enabled": channel is not None,
            "display_checked": self._channel_display_checked(channel),
            "display_enabled": channel is not None,
        }

    def _rebuild_channel_panel_cards(self) -> None:
        grid = getattr(self, "channelPanelGrid", None)
        if grid is None:
            return

        from PyQt6.QtWidgets import (
            QCheckBox,
            QFrame,
            QHBoxLayout,
            QLabel,
            QPushButton,
            QSizePolicy,
            QVBoxLayout,
        )

        self._clear_channel_panel_layout(grid)
        self.channelPanelCards = {}
        channels = self._panel_channels()
        if not channels:
            empty_label = QLabel("No detected modules yet.")
            empty_label.setStyleSheet(_DMMR_PANEL_EMPTY_STYLE)
            grid.addWidget(empty_label, 0, 0)
            self.channelPanelSignature = ()
            return

        for index, channel in enumerate(channels):
            module = channel.module_address()
            card = QFrame()
            card.setSizePolicy(
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Fixed,
            )
            card.setMinimumWidth(_DMMR_PANEL_CARD_MIN_WIDTH)
            card.setMaximumWidth(_DMMR_PANEL_CARD_MAX_WIDTH)

            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(12, 12, 12, 12)
            card_layout.setSpacing(8)

            header_layout = QHBoxLayout()
            header_layout.setContentsMargins(0, 0, 0, 0)
            header_layout.setSpacing(8)

            title_label = QLabel(f"Module {module}")
            title_label.setStyleSheet(_DMMR_PANEL_TITLE_STYLE)
            state_badge = QLabel("n/a")
            header_layout.addWidget(title_label)
            header_layout.addStretch(1)
            header_layout.addWidget(state_badge)
            card_layout.addLayout(header_layout)

            current_name = QLabel("Current")
            current_name.setStyleSheet(_DMMR_PANEL_CURRENT_LABEL_STYLE)
            current_value = QLabel("NaN")
            current_value.setStyleSheet(_DMMR_PANEL_CURRENT_VALUE_STYLE)
            card_layout.addWidget(current_name)
            card_layout.addWidget(current_value)

            controls_layout = QHBoxLayout()
            controls_layout.setContentsMargins(0, 0, 0, 0)
            controls_layout.setSpacing(10)

            read_button = QPushButton("Read")
            read_button.setCheckable(True)
            read_button.setStyleSheet(_DMMR_PANEL_READ_BUTTON_STYLE)
            read_button.clicked.connect(
                lambda checked, module=module: self._channel_panel_read_toggled(
                    module,
                    checked,
                )
            )
            display_box = QCheckBox("Display")
            display_box.toggled.connect(
                lambda checked, module=module: self._channel_panel_display_toggled(
                    module,
                    checked,
                )
            )
            controls_layout.addWidget(read_button)
            controls_layout.addStretch(1)
            controls_layout.addWidget(display_box)
            card_layout.addLayout(controls_layout)

            row = index // _DMMR_PANEL_GRID_COLUMNS
            column = index % _DMMR_PANEL_GRID_COLUMNS
            grid.addWidget(card, row, column)
            self.channelPanelCards[module] = {
                "card": card,
                "title": title_label,
                "state_badge": state_badge,
                "current_value": current_value,
                "read_button": read_button,
                "display_box": display_box,
            }

        self.channelPanelSignature = tuple(
            channel.module_address() for channel in channels
        )

    def _ensure_channel_panel(self) -> None:
        """Replace the generic channel table with compact DMMR module cards."""
        self._hide_channel_table()
        self._hide_channel_table_actions()
        if not hasattr(self, "channelPanel"):
            from PyQt6.QtWidgets import (
                QGridLayout,
                QHBoxLayout,
                QVBoxLayout,
                QWidget,
            )

            panel = QWidget()
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(12, 12, 12, 12)
            panel_layout.setSpacing(12)

            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(0)
            row_layout.addStretch(1)

            host = QWidget()
            grid = QGridLayout(host)
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(12)
            grid.setVerticalSpacing(12)
            row_layout.addWidget(host)
            row_layout.addStretch(1)
            panel_layout.addWidget(row)

            self.channelPanel = panel
            self.channelPanelHost = host
            self.channelPanelGrid = grid
            self.channelPanelCards = {}
            self.channelPanelSignature = None
            self.addContentWidget(panel)

        signature = tuple(channel.module_address() for channel in self._panel_channels())
        if getattr(self, "channelPanelSignature", None) != signature:
            self._rebuild_channel_panel_cards()
        self._update_channel_panel()

    def _display_main_state(self) -> str:
        """Return the operator-facing state shown in the toolbar badge."""
        raw_state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        is_on = getattr(self, "isOn", None)
        if (
            raw_state != "Disconnected"
            and not _state_requires_operator_attention(raw_state)
            and callable(is_on)
            and not bool(is_on())
        ):
            return "OFF"
        return raw_state

    def _ensure_status_widgets(self) -> None:
        """Add compact global DMMR status labels to the plugin toolbar."""
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
        """Return a compact badge style that reflects the DMMR main state."""
        state = self._display_main_state()
        if state == "ST_ON":
            background = "#2f855a"
        elif state == "OFF":
            background = "#4a5568"
        elif state == "ST_STBY":
            background = "#b7791f"
        elif state == "Disconnected":
            background = "#718096"
        elif (
            state == "ST_OVERLOAD"
            or state.startswith("ST_ERR")
            or _state_requires_operator_attention(state)
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
        """Return the compact DMMR runtime summary displayed in the toolbar."""
        modules = str(getattr(self, "detected_modules", "") or "None")
        faults = _compact_status_text(
            getattr(self, "device_state_summary", None),
            default="n/a",
        )
        voltage = _compact_status_text(
            getattr(self, "voltage_state_summary", None),
            default="n/a",
        )
        return f"Modules: {modules} | Faults: {faults} | Rails: {voltage}"

    def _status_tooltip_text(self) -> str:
        """Return the full DMMR status tooltip for the toolbar widgets."""
        display_state = self._display_main_state()
        hardware_state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        lines = [f"State: {display_state}"]
        if display_state != hardware_state:
            lines.append(f"Hardware state: {hardware_state}")
        lines.extend(
            (
                f"Modules: {getattr(self, 'detected_modules', '') or 'None'}",
                f"Faults: {getattr(self, 'device_state_summary', '') or 'n/a'}",
                f"Voltage rails: {getattr(self, 'voltage_state_summary', '') or 'n/a'}",
                f"Temperature: {getattr(self, 'temperature_state_summary', '') or 'n/a'}",
            )
        )
        return "\n".join(lines)

    def _update_status_widgets(self) -> None:
        """Refresh the global DMMR status labels in the toolbar."""
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        self._sync_acquisition_controls()
        if badge is None or summary is None:
            self._update_channel_panel()
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
        self._update_channel_panel()

    def _update_channel_panel(self) -> None:
        cards = getattr(self, "channelPanelCards", None)
        if not isinstance(cards, dict):
            return

        signature = tuple(channel.module_address() for channel in self._panel_channels())
        if getattr(self, "channelPanelSignature", None) != signature:
            self._rebuild_channel_panel_cards()
            cards = getattr(self, "channelPanelCards", None)
            if not isinstance(cards, dict):
                return

        for module, widgets in cards.items():
            snapshot = self._channel_panel_snapshot(module)
            for key in ("title", "current_value"):
                widget = widgets.get(key)
                if widget is not None and hasattr(widget, "setText"):
                    widget.setText(str(snapshot["current_text" if key == "current_value" else key]))
            current_widget = widgets.get("current_value")
            if current_widget is not None and hasattr(current_widget, "setToolTip"):
                current_widget.setToolTip(str(snapshot["current_tooltip"]))
            state_badge = widgets.get("state_badge")
            if state_badge is not None and hasattr(state_badge, "setText"):
                state_badge.setText(str(snapshot["state_text"]))
            if state_badge is not None and hasattr(state_badge, "setStyleSheet"):
                state_badge.setStyleSheet(snapshot["state_style"])
            card = widgets.get("card")
            if card is not None and hasattr(card, "setStyleSheet"):
                card.setStyleSheet(snapshot["card_style"])
            self._set_channel_panel_button(
                widgets.get("read_button"),
                checked=bool(snapshot["read_checked"]),
                enabled=bool(snapshot["read_enabled"]),
            )
            self._set_channel_panel_checkbox(
                widgets.get("display_box"),
                checked=bool(snapshot["display_checked"]),
                enabled=bool(snapshot["display_enabled"]),
            )

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
        """Hide framework columns and configure the Current column as resizable."""
        if self.tree is None or not self.channels:
            return

        parameter_names = list(self.channels[0].getSortedDefaultChannel())
        for hidden_name in (Channel.COLLAPSE, Channel.REAL, Channel.ACTIVE):
            if hidden_name in parameter_names:
                self.tree.setColumnHidden(parameter_names.index(hidden_name), True)

        # Make the Current column user-resizable with a generous default width.
        monitor_name = getattr(Channel, "MONITOR", "Monitor")
        if monitor_name in parameter_names:
            header = self.tree.header()
            if header is not None:
                monitor_index = parameter_names.index(monitor_name)
                header.setSectionResizeMode(
                    monitor_index, type(header).ResizeMode.Interactive
                )
                # Compute a default width from the monitor widget font.
                default_width = 200
                for channel in self.channels[:1]:
                    param = channel.getParameterByName(monitor_name)
                    widget = (
                        getattr(param, "getWidget", lambda: None)()
                        if param is not None
                        else None
                    )
                    if widget is not None:
                        fm = getattr(widget, "fontMetrics", None)
                        metrics = fm() if callable(fm) else None
                        advance = getattr(metrics, "horizontalAdvance", None)
                        if callable(advance):
                            default_width = advance("-123.456 fA   ") + 20
                header.resizeSection(monitor_index, default_width)

    def _sync_channels_from_detected_modules(self, detected_modules: list[int]) -> bool:
        """Synchronize channels from the latest detected DMMR module scan."""
        current_items = self._current_channel_items()
        target_items, log_entries = _plan_channel_sync(
            current_items=current_items,
            detected_modules=detected_modules,
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
                    header.setSectionResizeMode(type(header).ResizeMode.ResizeToContents)
                for channel in self.getChannels():
                    channel.collapseChanged(toggle=False)
                self.tree.scheduleDelayedItemsLayout()
            if hasattr(self, "advancedAction"):
                self.toggleAdvanced(advanced=self.advancedAction.state)
            self._hide_channel_table()
            self._hide_channel_table_actions()
            self._update_channel_column_visibility()
            self._ensure_channel_panel()
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
        """Skip the generic bootstrap until DMMR hardware is initialized."""
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
                    f"DMMR config file {file} not found. "
                    "Channels will be created after successful hardware initialization."
                )
                self._set_channel_headers_from_template()
                if hasattr(self, "advancedAction"):
                    self.toggleAdvanced(advanced=self.advancedAction.state)
                self._hide_channel_table()
                self._hide_channel_table_actions()
                self._ensure_channel_panel()
                if self.tree is not None:
                    self.tree.scheduleDelayedItemsLayout()
                self.pluginManager.DeviceManager.globalUpdate(inout=self.inout)
            finally:
                if self.tree is not None:
                    self.tree.setUpdatesEnabled(True)
                self.loading = False
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)
        self._hide_channel_table()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()

    def toggleAdvanced(self, advanced: "bool | None" = False) -> None:
        """Handle advanced columns without hiding DMMR channels."""
        if self.channels:
            super().toggleAdvanced(advanced=advanced)
            for channel in self.getChannels():
                channel.setHidden(False)
            self._hide_channel_table()
            self._hide_channel_table_actions()
            self._update_channel_column_visibility()
            self._ensure_channel_panel()
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
        self._hide_channel_table()
        self._hide_channel_table_actions()
        self._ensure_channel_panel()

    def estimateStorage(self) -> None:
        """Avoid division by zero before the first DMMR channel discovery."""
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
                "DMMR hardware initialization."
            )

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=1,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the DMMR controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Baud rate passed to cgc.dmmr.DMMR.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=10.0,
            minimum=1.0,
            maximum=60.0,
            toolTip="Timeout in seconds used to initialize and shutdown the controller.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=0.5,
            maximum=30.0,
            toolTip="Timeout in seconds used for polling DMMR state and module currents.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
        )
        settings[f"{self.name}/{self.STATE}"] = parameterDict(
            value="Disconnected",
            toolTip="Latest DMMR controller state reported by the driver.",
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
        """Disable manual acquisition controls until the DMMR is actually ready."""
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
        """Only allow data recording when the DMMR is initialized and in ST_ON."""
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
        controller = self.controller if hasattr(self, "controller") else None
        controller_initialized = bool(getattr(controller, "initialized", False))
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

        if controller and controller_initialized and not forced_close_state:
            self.shutdownCommunication()
            return

        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False
            self._sync_local_on_action()
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
        """Run the full DMMR hardware shutdown sequence from the toolbar action."""
        self.stopAcquisition()
        shutdown_confirmed = True
        if self.controller:
            shutdown_confirmed = bool(self.controller.shutdownCommunication())
        if self.useOnOffLogic and hasattr(self, "onAction"):
            self.onAction.state = False if shutdown_confirmed else True
            self._sync_local_on_action()
            self._sync_toolbar_communication_controls()
            self._update_status_widgets()
        if not shutdown_confirmed:
            self.print(
                "DMMR shutdown could not be confirmed; UI remains ON until the hardware state is verified.",
                flag=PRINT.WARNING,
            )
        self.recording = False
        self._sync_acquisition_controls()

    def _set_on_ui_state(self, on: bool) -> None:
        """Synchronize the ESIBD and local DMMR ON/OFF actions."""
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
        """Toggle the DMMR without relying on a channel apply path."""
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
        self._update_status_widgets()
        if getattr(self, "loading", False):
            return

        if controller and getattr(controller, "initialized", False):
            begin_transition = getattr(self.controller, "_begin_transition", None) if self.controller else None
            if self.controller and (not callable(begin_transition) or begin_transition(self.isOn())):
                self.controller.toggleOnFromThread(parallel=True)
        elif hasattr(self, "onAction") and self.isOn():
            self.initializeCommunication()


class DMMRChannel(Channel):
    """DMMR module channel definition."""

    MODULE = "Module"
    channelParent: DMMRDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.module: int

        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Reference (A)"
        channel[self.VALUE][_PARAMETER_ADVANCED_KEY] = True
        channel[self.VALUE][_PARAMETER_TOOLTIP_KEY] = (
            "Reference field only. The DMMR plugin is read-only; live current is "
            "shown through channel monitors."
        )
        channel[self.ENABLED][_PARAMETER_ADVANCED_KEY] = False
        channel[self.ENABLED][Parameter.HEADER] = "Read"
        channel[self.ENABLED][_PARAMETER_TOOLTIP_KEY] = (
            "Enable or mute this DMMR module in the UI."
        )
        channel[self.DISPLAY][Parameter.HEADER] = "Display"
        channel[self.DISPLAY][_PARAMETER_EVENT_KEY] = self.displayChanged
        channel[self.ACTIVE][_PARAMETER_ADVANCED_KEY] = True
        monitor_name = getattr(self, "MONITOR", "Monitor")
        if monitor_name in channel:
            channel[monitor_name][Parameter.HEADER] = "Current"
            channel[monitor_name][_PARAMETER_TOOLTIP_KEY] = (
                "Measured DMMR module current. Values are stored in amps and "
                "displayed with automatic SI prefixes."
            )
        channel[self.MODULE] = parameterDict(
            value="0",
            parameterType=PARAMETERTYPE.LABEL,
            advanced=False,
            indicator=True,
            header="Mod",
            attr="module",
        )
        return channel

    def setDisplayedParameters(self) -> None:
        super().setDisplayedParameters()
        if self.OPTIMIZE in self.displayedParameters:
            self.displayedParameters.remove(self.OPTIMIZE)
        if self.DISPLAY in self.displayedParameters:
            self.displayedParameters.remove(self.DISPLAY)
        self.displayedParameters.append(self.MODULE)
        self.displayedParameters.append(self.DISPLAY)

    def initGUI(self, item: dict) -> None:
        super().initGUI(item)
        if callable(getattr(self, "getParameterByName", None)):
            self._upgrade_monitor_widget()
        self._upgrade_toggle_widget(self.ENABLED, "Read", 52)
        self._sync_enabled_toggle_widget()
        self.scalingChanged()
        self._sync_neutral_parameter_styles()

    def scalingChanged(self) -> None:
        super().scalingChanged()
        monitor_parameter_getter = getattr(self, "getParameterByName", None)
        if callable(monitor_parameter_getter):
            monitor_parameter = monitor_parameter_getter(getattr(self, "MONITOR", "Monitor"))
            monitor_widget = (
                getattr(monitor_parameter, "getWidget", lambda: None)()
                if monitor_parameter is not None
                else None
            )
            if monitor_widget is not None:
                self._set_monitor_widget_minimum_width(monitor_widget)
        if self.rowHeight >= _DMMR_MIN_ROW_HEIGHT:
            return
        self.rowHeight = _DMMR_MIN_ROW_HEIGHT
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
            parameter.check.setMaximumHeight(max(parameter.rowHeight, _DMMR_MIN_ROW_HEIGHT))
            parameter.check.setMinimumWidth(minimum_width)
            parameter.check.setText(label)
            parameter.check.setCheckable(True)
            if hasattr(parameter.check, "setStyleSheet"):
                parameter.check.setStyleSheet(_DMMR_TOGGLE_BUTTON_STYLE)
            if hasattr(parameter.check, "setAutoRaise"):
                parameter.check.setAutoRaise(False)
        parameter.value = initial_value

    def _sync_enabled_toggle_widget(self) -> None:
        """Keep the Read button state visually synchronized with channel.enabled."""
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(getattr(self, "ENABLED", "Enabled"))
        widget = getattr(parameter, "check", None) if parameter is not None else None
        if widget is None:
            return
        enabled_value = getattr(self, "enabled", None)
        if enabled_value is None and parameter is not None:
            enabled_value = getattr(parameter, "value", False)
        if hasattr(widget, "setChecked"):
            widget.setChecked(bool(enabled_value))
        if hasattr(widget, "setStyleSheet"):
            widget.setStyleSheet(_DMMR_TOGGLE_BUTTON_STYLE)

    def _set_parameter_widget_style(self, parameter_name: str, style: str) -> None:
        """Apply the same background style to a parameter widget and its container."""
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        parameter = getter(parameter_name)
        if parameter is None:
            return
        widget = getattr(parameter, "getWidget", lambda: None)()
        if widget is None:
            return

        seen: set[int] = set()
        targets = [getattr(widget, "container", None), widget]
        for owner in list(targets):
            if owner is None:
                continue
            line_edit = getattr(owner, "lineEdit", None)
            if callable(line_edit):
                targets.append(line_edit())
            find_children = getattr(owner, "findChildren", None)
            if callable(find_children):
                with contextlib.suppress(Exception):
                    targets.extend(find_children(object))

        for target in targets:
            if target is None:
                continue
            target_id = id(target)
            if target_id in seen:
                continue
            seen.add(target_id)
            if hasattr(target, "setStyleSheet"):
                target.setStyleSheet(style)

    def _sync_neutral_parameter_styles(self) -> None:
        """Keep Display and Mod visually neutral like the other DMMR cells."""
        for parameter_name in (self.DISPLAY, self.MODULE):
            self._set_parameter_widget_style(parameter_name, _DMMR_NEUTRAL_WIDGET_STYLE)

    def _upgrade_monitor_widget(self) -> None:
        """Replace the default numeric monitor widget with the SI-aware DMMR monitor."""
        parameter = self.getParameterByName(getattr(self, "MONITOR", "Monitor"))
        if parameter is None:
            return

        widget_getter = getattr(parameter, "getWidget", None)
        current_widget = widget_getter() if callable(widget_getter) else getattr(parameter, "widget", None)
        if isinstance(current_widget, _DMMRCurrentMonitorSpinBox):
            return

        current_value = getattr(self, "monitor", np.nan)
        parameter.widget = _DMMRCurrentMonitorSpinBox(indicator=True, displayDecimals=3)
        parameter.applyWidget()
        self._set_monitor_widget_minimum_width(parameter.widget)
        parameter.value = current_value
        self._sync_monitor_widget()

    def _set_monitor_widget_minimum_width(
        self,
        widget: Any,
    ) -> None:
        """Set a minimum width so the current value stays readable when the column is narrow."""
        for metrics_owner in (widget, getattr(widget, "lineEdit", lambda: None)()):
            if metrics_owner is None:
                continue
            font_metrics = getattr(metrics_owner, "fontMetrics", None)
            metrics = font_metrics() if callable(font_metrics) else None
            advance = getattr(metrics, "horizontalAdvance", None)
            if callable(advance):
                minimum = advance("-123.456 fA") + 10
                if hasattr(widget, "setMinimumWidth"):
                    widget.setMinimumWidth(minimum)
                return

    def realChanged(self) -> None:
        self.getParameterByName(self.MODULE).setVisible(self.real)
        super().realChanged()

    def enabledChanged(self) -> None:
        super().enabledChanged()
        if not self.enabled:
            self.monitor = np.nan
        self._sync_enabled_toggle_widget()

    def monitorChanged(self) -> None:
        super().monitorChanged()
        self._sync_monitor_widget()

    def displayChanged(self) -> None:
        super().updateDisplay()

    def updateColor(self):
        """Keep all cells neutral; center the Display checkbox."""
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QBrush
        from PyQt6.QtWidgets import QCheckBox, QComboBox, QSizePolicy

        color = super().updateColor()
        if color is None:
            return color

        neutral = QBrush()
        for i in range(len(self.parameters) + 1):
            self.setBackground(i, neutral)

        for parameter in self.parameters:
            widget = parameter.getWidget()
            if not widget:
                continue
            if hasattr(widget, "container"):
                widget.container.setStyleSheet("")
            if not isinstance(widget, QComboBox):
                widget.setStyleSheet("")

        # Center the Display checkbox in its cell
        display_param = self.getParameterByName(self.DISPLAY)
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

        self._sync_neutral_parameter_styles()
        self._sync_enabled_toggle_widget()

        return color

    def module_address(self) -> int:
        """Return the configured DMMR module address as an integer."""
        return _coerce_int(self.module, 0)

    def _sync_monitor_widget(self) -> None:
        """Render the live current with automatic SI prefixes in the monitor field."""
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return

        monitor_name = getattr(self, "MONITOR", "Monitor")
        parameter = getter(monitor_name)
        if parameter is None:
            return

        widget = getattr(parameter, "getWidget", lambda: None)()
        if widget is None:
            return

        formatted_text, raw_text = _format_si_current(getattr(self, "monitor", np.nan))
        line_edit = getattr(widget, "lineEdit", lambda: None)()
        is_numeric_widget = callable(getattr(widget, "setValue", None)) and callable(
            getattr(widget, "textFromValue", None)
        )
        if not is_numeric_widget and line_edit is not None and hasattr(line_edit, "setText"):
            line_edit.setText(formatted_text)
        elif not is_numeric_widget and hasattr(widget, "setText"):
            widget.setText(formatted_text)
        elif hasattr(widget, "update"):
            widget.update()

        tooltip_base = str(getattr(parameter, "toolTip", "") or "").strip()
        tooltip = f"{formatted_text} ({raw_text})"
        if tooltip_base:
            tooltip = f"{tooltip_base}\n{tooltip}"
        if hasattr(widget, "setToolTip"):
            widget.setToolTip(tooltip)
        if line_edit is not None and hasattr(line_edit, "setToolTip"):
            line_edit.setToolTip(tooltip)


class DMMRController(DeviceController):
    """DMMR hardware controller used by the ESIBD Explorer plugin."""

    controllerParent: DMMRDevice

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.detected_module_ids: list[int] = []
        self.detected_modules_text = ""
        self.main_state = "Disconnected"
        self.device_state_summary = "n/a"
        self.voltage_state_summary = "n/a"
        self.temperature_state_summary = "n/a"
        self.initialized = False
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._close_lock = Lock()
        self._forced_close_state: str | None = None
        self._consecutive_transport_failures = 0
        # COM port (if any) whose transport was poisoned by a timed-out DLL call
        # earlier in this session and is therefore still locked in-process.
        self._poisoned_com: int | None = None

    def initializeValues(self, reset: bool = False) -> None:
        if getattr(self, "values", None) is None or reset:
            get_channels = getattr(self.controllerParent, "getChannels", None)
            if not callable(get_channels):
                self.values = {}
                return
            self.values = {
                channel.module_address(): np.nan
                for channel in get_channels()
                if channel.real
            }

    def _measurement_modules(self) -> list[int]:
        configured_modules_getter = getattr(self.controllerParent, "getConfiguredModules", None)
        configured_modules = (
            {
                _coerce_int(module, -1)
                for module in configured_modules_getter()
                if _coerce_int(module, -1) >= 0
            }
            if callable(configured_modules_getter)
            else set()
        )
        detected_modules = {
            _coerce_int(module, -1)
            for module in getattr(self, "detected_module_ids", [])
            if _coerce_int(module, -1) >= 0
        }
        if detected_modules:
            return sorted(configured_modules & detected_modules) if configured_modules else sorted(
                detected_modules
            )
        return sorted(configured_modules)

    def _wrong_command_status(self, status: Any, device: Any | None = None) -> bool:
        device = self.device if device is None else device
        if device is None:
            return False
        wrong_command = getattr(device, "ERR_COMMAND_WRONG", None)
        return wrong_command is not None and status == wrong_command

    def _disable_automatic_current_for_module_polling(
        self,
        *,
        timeout_s: float,
        log_warning: bool = False,
        device: Any | None = None,
    ) -> bool:
        """Force the controller back to manual module polling mode.

        The plugin reads one module at a time via ``get_module_current()``.
        That command stream is incompatible with the firmware automatic-current
        frame mode used by ``get_current()``.
        """
        device = self.device if device is None else device
        if device is None:
            return False

        disable_automatic = getattr(device, "set_automatic_current", None)
        if not callable(disable_automatic):
            return False

        status = disable_automatic(False, timeout_s=timeout_s)
        if status != getattr(device, "NO_ERR", status):
            raise RuntimeError(
                "set_automatic_current(False) failed: "
                f"{self._format_status(status, device=device)}"
            )

        if log_warning:
            self.print(
                "DMMR automatic current mode was active; switched back to "
                "manual module polling.",
                flag=PRINT.WARNING,
            )
        return True

    def runInitialization(self) -> None:
        self.initialized = False
        self._forced_close_state = None
        self._end_transition()
        self._dispose_device()
        try:
            dmmr_driver_class = _get_dmmr_driver_class()
            self.device = dmmr_driver_class(
                device_id=f"{self.controllerParent.name.lower()}_com{int(self.controllerParent.com)}",
                com=int(self.controllerParent.com),
                baudrate=int(self.controllerParent.baudrate),
            )
            backend_reason = str(
                getattr(self.device, "_process_backend_disabled_reason", "")
            ).strip()
            if backend_reason:
                self.print(backend_reason, flag=PRINT.WARNING)
            module_info = self.device.initialize(
                timeout_s=float(self.controllerParent.connect_timeout_s)
            )
            self.detected_module_ids = sorted(module_info)
            self.detected_modules_text = (
                ", ".join(str(module) for module in self.detected_module_ids)
                if self.detected_module_ids
                else "None"
            )
            self._update_state()
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:  # noqa: BLE001
            self._restore_off_ui_state()
            guidance = self._init_failure_guidance(exc)
            message = (
                f"DMMR initialization failed on COM{int(self.controllerParent.com)}: "
                f"{self._format_exception(exc)}"
            )
            if guidance:
                message = f"{message}\n{guidance}"
            self.print(message, flag=PRINT.ERROR)
            self._dispose_device()
        finally:
            self.initializing = False

    def _init_failure_guidance(self, exc: Exception) -> str:
        """Operator guidance appended to an init-failure message, or "" if none.

        Tracks whether a transport was poisoned by a timed-out DLL call earlier
        in this session so that later retries explain why they still fail (the
        COM port is locked in-process) instead of looping on a bare
        'Error opening port' (-2) while the hardware is actually responsive.
        """
        current_com = _coerce_int(getattr(self.controllerParent, "com", None), -1)
        guidance = _dmmr_poisoned_port_guidance(
            exc,
            poisoned_com=getattr(self, "_poisoned_com", None),
            current_com=current_com,
        )
        if _transport_failure_is_fatal(exc) and current_com >= 0:
            self._poisoned_com = current_com
        return guidance

    def initComplete(self) -> None:
        if self.device is not None and self.detected_module_ids:
            self.controllerParent._sync_channels_from_detected_modules(
                self.detected_module_ids
            )
        self.initializeValues()
        self.initialized = True
        # A fresh transport reached this far, so any earlier in-process port
        # poisoning is no longer relevant for this COM port.
        self._poisoned_com = None
        self.super_init_complete_called = True
        self._sync_status_to_gui()
        if self.device is None:
            self.print(
                "DMMR initialization simulated because ESIBD Test mode is active. "
                "No hardware communication was attempted.",
                flag=PRINT.WARNING,
            )
            return

        modules_text = self.detected_modules_text or "None"
        self.print(
            f"DMMR initialized on COM{int(self.controllerParent.com)}. "
            f"State: {self.main_state}. Detected modules: {modules_text}."
        )
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
        # If _update_state detected an unusable device it will have set
        # acquiring=False and emitted closeCommunicationSignal.  Bail out
        # immediately to avoid a cascade of redundant module-read errors.
        if not self.acquiring:
            return

        if not getattr(self.controllerParent, "isOn", lambda: False)():
            return

        # Capture last-good readings before resetting so a single slow/failing
        # module does not wipe every other channel to NaN for this poll cycle.
        previous_values = dict(getattr(self, "values", {}) or {})
        self.initializeValues(reset=True)
        new_values = {
            channel.module_address(): previous_values.get(channel.module_address(), np.nan)
            for channel in self.controllerParent.getChannels()
            if channel.real
        }
        poll_modules = self._measurement_modules()

        for module in poll_modules:
            status = None
            measured_current = np.nan
            try:
                with self._controller_lock_section(
                    f"Could not acquire lock to read DMMR module {module}.",
                ):
                    device = self.device
                    if device is None:
                        return
                    status, measured_current, _meas_range = device.get_module_current(
                        module,
                        timeout_s=float(self.controllerParent.poll_timeout_s),
                    )
            except TimeoutError:
                # Transient controller-lock contention (another operation holds
                # the lock); skip this module this cycle, last-good value kept.
                continue
            except Exception as exc:  # noqa: BLE001
                self.errorCount += 1
                self.print(
                    f"Failed to read DMMR module {module}: {exc}",
                    flag=PRINT.ERROR,
                )
                if _transport_failure_is_fatal(exc):
                    self._handle_transport_loss()
                    return
                continue

            if status == getattr(device, "NO_ERR", status):
                new_values[module] = float(measured_current)
                continue

            if self._wrong_command_status(status, device=device):
                try:
                    with self._controller_lock_section(
                        "Could not acquire lock to recover DMMR polling mode.",
                    ):
                        device = self.device
                        if device is None:
                            return
                        recovered = self._disable_automatic_current_for_module_polling(
                            timeout_s=float(self.controllerParent.connect_timeout_s),
                            log_warning=True,
                            device=device,
                        )
                        if recovered:
                            status, measured_current, _meas_range = device.get_module_current(
                                module,
                                timeout_s=float(self.controllerParent.poll_timeout_s),
                            )
                    if status == getattr(device, "NO_ERR", status):
                        new_values[module] = float(measured_current)
                        continue
                except Exception as exc:  # noqa: BLE001
                    self.errorCount += 1
                    self.print(
                        "Failed to recover DMMR manual polling mode: "
                        f"{self._format_exception(exc)}",
                        flag=PRINT.ERROR,
                    )
                    continue

            self.errorCount += 1
            self.print(
                f"DMMR rejected current read for module {module}: "
                f"{self._format_status(status)}",
                flag=PRINT.ERROR,
            )

        self.values = new_values

    def fakeNumbers(self) -> None:
        self.initializeValues(reset=True)

    def applyValue(self, channel: DMMRChannel) -> None:
        """DMMR is a read-only measurement device at channel level."""
        return

    def updateValues(self) -> None:
        if self.values is None:
            return

        self._sync_status_to_gui()
        device_is_on = self.controllerParent.isOn()
        for channel in self.controllerParent.getChannels():
            if channel.enabled and channel.real and device_is_on:
                channel.monitor = self.values.get(channel.module_address(), np.nan)
            else:
                channel.monitor = np.nan
            sync_monitor = getattr(channel, "_sync_monitor_widget", None)
            if callable(sync_monitor):
                sync_monitor()

    def toggleOn(self) -> None:
        base_toggle_on = getattr(super(), "toggleOn", None)
        if callable(base_toggle_on):
            base_toggle_on()

        target_on = bool(self.controllerParent.isOn())
        device = self.device
        if device is None:
            self._end_transition()
            return

        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False

        try:
            if target_on:
                measurement_modules = self._measurement_modules()
                with self._controller_lock_section(
                    "Could not acquire lock to enable DMMR acquisition."
                ):
                    device = self.device
                    if device is None:
                        self._restore_off_ui_state()
                        return
                    enable_status = device.set_enable(
                        True,
                        timeout_s=float(self.controllerParent.connect_timeout_s),
                    )
                    if enable_status != device.NO_ERR:
                        raise RuntimeError(
                            f"set_enable(True) failed: {self._format_status(enable_status, device=device)}"
                        )
                    set_module_auto_range = getattr(device, "set_module_auto_range", None)
                    if callable(set_module_auto_range):
                        for module in measurement_modules:
                            auto_range_status = set_module_auto_range(
                                module,
                                True,
                                timeout_s=float(self.controllerParent.connect_timeout_s),
                            )
                            if auto_range_status != device.NO_ERR:
                                raise RuntimeError(
                                    f"set_module_auto_range({module}, True) failed: "
                                    f"{self._format_status(auto_range_status, device=device)}"
                                )
                    self._disable_automatic_current_for_module_polling(
                        timeout_s=float(self.controllerParent.connect_timeout_s),
                        device=device,
                    )
                self._update_state()
                start_acquisition = getattr(self, "startAcquisition", None)
                if callable(start_acquisition):
                    start_acquisition()
                self.print("DMMR acquisition enabled.")
            else:
                with self._controller_lock_section(
                    "Could not acquire lock to disable DMMR acquisition."
                ):
                    device = self.device
                    if device is None:
                        return
                    automatic_status = device.set_automatic_current(
                        False,
                        timeout_s=float(self.controllerParent.connect_timeout_s),
                    )
                    if automatic_status != device.NO_ERR:
                        raise RuntimeError(
                            "set_automatic_current(False) failed: "
                            f"{self._format_status(automatic_status, device=device)}"
                        )
                    enable_status = device.set_enable(
                        False,
                        timeout_s=float(self.controllerParent.connect_timeout_s),
                    )
                    if enable_status != device.NO_ERR:
                        raise RuntimeError(
                            f"set_enable(False) failed: {self._format_status(enable_status, device=device)}"
                        )
                self._update_state()
                self.print("DMMR acquisition disabled.")
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            if target_on:
                self._restore_off_ui_state()
            else:
                self._restore_on_ui_state()
            self._update_state()
            self.print(
                f"Failed to toggle DMMR acquisition: {self._format_exception(exc)}"
                f"{self._runtime_diagnostics(device=device)}",
                flag=PRINT.ERROR,
            )
        finally:
            self._end_transition()
            self._sync_status_to_gui()

    def closeCommunication(self, *, final_state: str | None = None) -> None:
        close_lock = self._close_guard()
        if not close_lock.acquire(blocking=False):
            return
        try:
            self._end_transition()
            # Signal the acquisition loop to stop early so it releases the
            # controller lock without waiting for a contested acquire_timeout.
            self.acquiring = False

            # Best-effort attempt to disable the DMMR hardware so the
            # physical LED reflects the disconnected state.  Failures are
            # expected and silently ignored when the link is already broken.
            self._attempt_device_disable()

            base_close = getattr(super(), "closeCommunication", None)
            if callable(base_close):
                base_close()
            if final_state is None:
                final_state = self._forced_close_state
                if final_state is None:
                    is_on = getattr(self.controllerParent, "isOn", None)
                    final_state = (
                        _DMMR_COMMUNICATION_LOST_STATE
                        if callable(is_on) and bool(is_on())
                        else "Disconnected"
                    )
            self.main_state = final_state
            self.detected_module_ids = []
            self.detected_modules_text = ""
            summary_value = "n/a" if final_state == "Disconnected" else "Unknown"
            self.device_state_summary = summary_value
            self.voltage_state_summary = summary_value
            self.temperature_state_summary = summary_value
            self._sync_status_to_gui()
            self._dispose_device()
            self.initialized = False
            self._clear_transport_failures()
            self._forced_close_state = None
        finally:
            close_lock.release()

    def shutdownCommunication(self) -> bool:
        """Run the DMMR shutdown sequence before releasing communication resources."""
        device = self.device
        if device is None:
            self.closeCommunication()
            return True

        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False
        self.print("Starting DMMR shutdown sequence.")
        shutdown_confirmed = False
        try:
            with self._controller_lock_section(
                "Could not acquire lock to shut down the DMMR."
            ):
                device = self.device
                if device is None:
                    shutdown_confirmed = True
                else:
                    device.shutdown(timeout_s=float(self.controllerParent.connect_timeout_s))
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self._update_state()
            self.print(
                f"DMMR shutdown failed: {self._format_exception(exc)}"
                f"{self._runtime_diagnostics(device=device)}",
                flag=PRINT.ERROR,
            )
        else:
            shutdown_confirmed = True
            self.print("DMMR shutdown sequence completed.")
        finally:
            self.closeCommunication(
                final_state=(
                    "Disconnected"
                    if shutdown_confirmed
                    else _DMMR_SHUTDOWN_UNCONFIRMED_STATE
                )
            )
        return shutdown_confirmed

    def _update_state(self) -> None:
        if self.device is None:
            self.main_state = "Disconnected"
            self.device_state_summary = "n/a"
            self.voltage_state_summary = "n/a"
            self.temperature_state_summary = "n/a"
            self._clear_transport_failures()
            return

        timeout_s = float(self.controllerParent.poll_timeout_s)
        try:
            with self._controller_lock_section(
                "Could not acquire lock to refresh the DMMR state."
            ):
                device = self.device
                if device is None:
                    self.main_state = "Disconnected"
                    self.device_state_summary = "n/a"
                    self.voltage_state_summary = "n/a"
                    self.temperature_state_summary = "n/a"
                    self._clear_transport_failures()
                    return
                status, _state_hex, state_name = device.get_state(timeout_s=timeout_s)
        except TimeoutError:
            # Transient controller-lock contention; skip this refresh and keep
            # the last state. A real device fault is handled by except-Exception.
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            failure_count = self._note_transport_failure()
            transport_unusable = _transport_failure_is_fatal(exc)
            transport_lost = transport_unusable or (
                failure_count >= _DMMR_TRANSPORT_FAILURE_THRESHOLD
            )
            self.main_state = (
                _DMMR_COMMUNICATION_LOST_STATE if transport_lost else "State error"
            )
            self.print(f"Failed to read DMMR state: {exc}", flag=PRINT.ERROR)
            self.device_state_summary = self._safe_query_state("get_device_state") or "Unknown"
            self.voltage_state_summary = self._safe_query_state("get_voltage_state") or "Unknown"
            self.temperature_state_summary = self._safe_query_state("get_temperature_state") or "Unknown"
            # When the DLL marks the instance unusable (e.g. after a timeout),
            # every subsequent call will fail immediately.  Stop the acquisition
            # loop and trigger closeCommunication via the framework's
            # thread-safe signal so the GUI updates on the main thread and the
            # COM port is released for potential re-initialization.
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
                f"Failed to read DMMR state: {self._format_status(status, device=device)}",
                flag=PRINT.ERROR,
            )

        self.device_state_summary = self._safe_query_state("get_device_state") or "Unknown"
        self.voltage_state_summary = self._safe_query_state("get_voltage_state") or "Unknown"
        self.temperature_state_summary = (
            self._safe_query_state("get_temperature_state") or "Unknown"
        )

    def _handle_transport_loss(self) -> None:
        """Force immediate backend teardown after a fatal transport timeout."""
        if (
            self._forced_close_state == _DMMR_COMMUNICATION_LOST_STATE
            and self.device is None
        ):
            return

        self.print(
            "Communication with the DMMR picoammeter was lost. Live current "
            "readings are no longer reliable, and the monitored channels may "
            "still be energized by their high-voltage sources. Do not assume "
            "any channel is safe based on the last displayed value; verify the "
            "state of the controlling HV devices before approaching the setup.",
            flag=PRINT.ERROR,
        )

        self.main_state = _DMMR_COMMUNICATION_LOST_STATE
        self.device_state_summary = "Unknown"
        self.voltage_state_summary = "Unknown"
        self.temperature_state_summary = "Unknown"
        self._forced_close_state = _DMMR_COMMUNICATION_LOST_STATE
        self.acquiring = False
        self.initialized = False
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
        self.controllerParent.voltage_state_summary = self.voltage_state_summary
        self.controllerParent.temperature_state_summary = self.temperature_state_summary
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

    def _close_guard(self) -> Lock:
        lock = getattr(self, "_close_lock", None)
        if lock is None:
            lock = Lock()
            self._close_lock = lock
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
            with contextlib.suppress(Exception):
                device.close()
            with contextlib.suppress(Exception):
                device._set_port_claimed(False)
        # Force the Python GC to release the DMMR DLL instance so that the
        # underlying serial port handle is freed.  Without this the DLL may
        # keep the COM port locked and prevent re-initialization on the same
        # port (Windows error -2 "port already in use").
        del device
        gc.collect()

    def _attempt_device_disable(self) -> None:
        """Best-effort attempt to disable the DMMR hardware before closing.

        Sends ``set_enable(False)`` to turn off modules so the firmware
        changes the physical LED away from the green ST_ON indicator.
        Failures are silently ignored because the serial link is typically
        already degraded when this method is called.
        """
        device = self.device
        if device is None:
            return
        try:
            device.set_enable(False, timeout_s=1.0)
        except Exception:  # noqa: BLE001
            pass

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
            ("temperature state", "get_temperature_state"),
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

        raise TypeError(
            "DMMR controller lock must provide either acquire_timeout() or "
            "acquire()/release()."
        )

    def _begin_transition(self, target_on: bool) -> bool:
        """Mark a global DMMR ON/OFF transition as active."""
        with self._transition_guard():
            if self.transitioning:
                return False
            self.transitioning = True
            self.transition_target_on = bool(target_on)
            return True

    def _end_transition(self) -> None:
        """Clear transition bookkeeping after a global DMMR ON/OFF sequence."""
        with self._transition_guard():
            self.transitioning = False
            self.transition_target_on = None

    def _format_exception(self, exc: Exception) -> str:
        message = str(exc).strip()
        lower_message = message.lower()
        com_number = _coerce_int(getattr(self.controllerParent, "com", None), 0)

        if "timed out during 'open_port'" in lower_message:
            hint = (
                f" Selected COM{com_number} did not respond. Check that the DMMR is "
                "powered, that the configured COM port is correct, and that no other "
                "application is holding the port."
            )
            message = f"{message}{hint}"
        elif "open_port failed:" in lower_message and "error opening port" in lower_message:
            hint = (
                f" Windows could not open COM{com_number}. The port is likely wrong, "
                "already in use, or stale after a previous connection failure. Close "
                "other serial tools and replug or power-cycle the DMMR before retrying."
            )
            message = f"{message}{hint}"

        if message:
            return f"{type(exc).__name__}: {message}"
        return repr(exc)
