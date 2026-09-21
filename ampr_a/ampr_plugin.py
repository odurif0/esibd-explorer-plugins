"""Drive AMPR high-voltage channels and monitor measured output voltages."""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import logging
import sys
import time
from pathlib import Path
from threading import Event, Lock, RLock, Thread
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
_AMPR_MODULE_KEY = "Module"
_AMPR_CHANNEL_ID_KEY = "CH"
_CHANNELS_PER_MODULE = 4
_CHANNELS_PER_MODULE_OPTIONS = {2, 4}
_AMPR_ABS_VOLTAGE_LIMIT = 1000.0
_AMPR_MIN_ROW_HEIGHT = 28
_AMPR_RAMP_STEP_S = 0.1
_AMPR_MONITOR_INTERVAL_S = 1.0
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


# A timed-out open_port poisons the COM port for the lifetime of the ESIBD
# Explorer process: the blocked vendor-DLL call keeps an exclusive OS handle, so
# no new instance can reopen it and every later retry fails with ERR_OPEN (-2)
# even once the device is powered on. The operator-facing guidance below makes
# that explicit instead of letting the user loop on a confusing
# 'Error opening port' message while the hardware is actually fine.
_AMPR_POISONED_PORT_RECOVERY = (
    "The COM port is now locked inside this ESIBD Explorer process: the timed-out "
    "attempt left a blocked vendor-DLL call holding an exclusive handle to the port. "
    "This instance can no longer reopen it, so every later retry will keep failing "
    "with 'Error opening port' (-2) even once the device is powered on. Power the "
    "device on, then RESTART ESIBD Explorer to release the port before trying again."
)
_AMPR_POISONED_PORT_RETRY = (
    "This retry is failing because an earlier timed-out connection attempt locked "
    "the COM port inside this ESIBD Explorer process. The device may well be powered "
    "on now, but the port cannot be reopened from this instance. RESTART ESIBD "
    "Explorer to release the port and retry."
)


def _ampr_poisoned_port_guidance(
    exc: Exception,
    *,
    poisoned_com: int | None,
    current_com: int | None,
) -> str:
    """Return operator guidance for a failed AMPR init, or "" when none applies.

    A timed-out open_port poisons the COM port for the lifetime of the process
    (the blocked vendor-DLL thread keeps an exclusive OS handle), so no new
    instance can reopen it and every retry fails with ERR_OPEN (-2). Surface
    that instead of letting the operator loop on a confusing 'Error opening
    port' while the hardware is actually fine.
    """
    if _transport_failure_is_fatal(exc):
        return _AMPR_POISONED_PORT_RECOVERY
    if (
        poisoned_com is not None
        and current_com is not None
        and int(poisoned_com) == int(current_com)
    ):
        return _AMPR_POISONED_PORT_RETRY
    return ""


def _configure_channel_entry(tree: Any) -> None:
    """Commit background clicks, but never let a mouse OFF steal editor focus."""
    if not callable(getattr(tree, "setFocusPolicy", None)) or hasattr(tree, "_ampr_entry_filter"):
        return
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QAbstractSpinBox, QApplication

    class EntryFilter(QObject):
        def eventFilter(self, watched: Any, event: Any) -> bool:
            if event.type() == QEvent.Type.MouseButtonPress:
                focused = QApplication.focusWidget()
                if isinstance(focused, QAbstractSpinBox) and tree.isAncestorOf(focused):
                    focused.clearFocus()
            return False

    # A NoFocus/TabFocus checkbox otherwise redirects mouse focus to the tree
    # before its clicked signal, committing the voltage before OFF is known.
    tree.setFocusPolicy(Qt.FocusPolicy.TabFocus)
    tree._ampr_entry_filter = EntryFilter(tree)
    tree.viewport().installEventFilter(tree._ampr_entry_filter)
    tree.header().viewport().installEventFilter(tree._ampr_entry_filter)


def _finish_channel_edit(channel: Any) -> None:
    """Finish editors for an explicit user action, never for a global refresh."""
    getter = getattr(channel, "getParameterByName", None)
    if not callable(getter):
        return
    for name in (getattr(channel, "VALUE", "Value"), getattr(channel, "RAMP_RATE", "Ramp rate")):
        parameter = getter(name)
        widget = getattr(parameter, "spin", None)
        if widget is not None and getattr(widget, "hasFocus", lambda: False)():
            blocked = widget.blockSignals(True)
            try:
                widget.interpretText()
            finally:
                widget.blockSignals(blocked)


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
        if QThread.currentThread() == app.thread():
            callback()
            return

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
    with _RUNTIME_LOAD_LOCK:
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
    POLL_TIMEOUT = "Poll timeout (s)"
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
            {
                module
                for module in (
                    channel.module_address() for channel in self.getChannels() if channel.real
                )
                if module >= 0
            }
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
        # StateAction.toggled updates the icon/tooltip; only triggered sends commands.
        action.state = self.isOn()

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
                    "AMPR hardware initialization."
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
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=0.5,
            maximum=60.0,
            toolTip="Timeout in seconds used to poll readbacks during monitoring.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
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
                "Default for channel configurations without a saved ramp speed. "
                "Each channel's Ramp (V/s) column controls its actual ramp."
            ),
            parameterType=PARAMETERTYPE.FLOAT,
            advanced=True,
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
        settings[f"{self.name}/Interval"][_PARAMETER_TOOLTIP_KEY] = (
            "Recording interval in ms. Monitor and Status read back voltages once per second, including during ramps."
        )
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

    def setOn(self, on: "bool | None" = None) -> None:
        """Toggle the AMPR without the generic immediate apply=True jump."""
        controller = self.controller if hasattr(self, "controller") else None
        current_state = self.isOn() if hasattr(self, "onAction") else False
        requested_on = current_state if on is None else bool(on)
        transition_target = getattr(controller, "transition_target_on", None)
        if controller and (
            getattr(controller, "initializing", False)
            or getattr(controller, "transitioning", False)
        ):
            if not requested_on:
                needs_worker = controller._request_off()
                if hasattr(self, "onAction"):
                    self.onAction.state = False
                self._sync_local_on_action()
                if (needs_worker and not getattr(controller, "initializing", False)
                        and controller._begin_transition(False)):
                    controller.toggleOnFromThread(parallel=True)
                return
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

        if self.isOn():
            for channel in self.getChannels():
                _finish_channel_edit(channel)
        if getattr(self, "initialized", False):
            begin_transition = getattr(self.controller, "_begin_transition", None) if self.controller else None
            if self.controller and (not callable(begin_transition) or begin_transition(self.isOn())):
                self.controller.toggleOnFromThread(parallel=True)
            else:
                for channel in self.channels:
                    if channel.controller:
                        channel.controller.toggleOnFromThread(parallel=True)
        elif hasattr(self, "onAction") and self.isOn():
            if controller is not None:
                controller._cancel_ramp = False
            self.initializeCommunication()


class _AMPRRampCancelled(Exception):
    """An explicit OFF interrupted startup/ramp-up; the same worker must stop."""


class _AMPRSetpoint:
    """One immutable input snapshot; only its delivery state changes under a lock."""

    def __init__(self, channel, device, cancel: Event) -> None:
        self.channel = channel
        self.key = (channel.module_address(), channel.channel_number())
        self.value = float(channel.value)
        self.target = self.value if channel.enabled else 0.0
        self.device = device
        self.cancel = cancel
        parameter = channel.getParameterByName(channel.VALUE)
        self.decimals = int(getattr(parameter, "displayDecimals", 2))
        self.state = "pending"
        self.detail = ""


class AMPRChannel(Channel):
    """AMPR output channel definition."""

    MODULE = "Module"
    ID = "CH"
    RAMP_RATE = "Ramp rate"
    channelParent: AMPRDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.module: int
        self.id: int

        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Voltage (V)"
        channel[self.VALUE][_PARAMETER_MIN_KEY] = -_AMPR_ABS_VOLTAGE_LIMIT
        channel[self.VALUE][_PARAMETER_MAX_KEY] = _AMPR_ABS_VOLTAGE_LIMIT
        channel[self.ENABLED][_PARAMETER_ADVANCED_KEY] = False
        channel[self.ENABLED][Parameter.HEADER] = "Status"
        channel[self.ENABLED][_PARAMETER_TOOLTIP_KEY] = (
            "Enable this AMPR output channel. Disabled channels are held at 0 V. "
            "Colour compares Monitor with the target (zero during global ramp-down): "
            "green within 1%, orange within 10%, red beyond; reference floor 1 V. "
            "This is voltage tracking feedback, not confirmation of safe discharge."
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
        channel[self.RAMP_RATE] = parameterDict(
            value=_coerce_float(getattr(self.channelParent, "ramp_rate_v_s", 10.0), 10.0),
            minimum=0.0,
            maximum=_AMPR_ABS_VOLTAGE_LIMIT,
            parameterType=PARAMETERTYPE.FLOAT,
            header="Ramp (V/s)",
            advanced=False,
            attr="ramp_rate_v_s",
            toolTip="This channel's ON/OFF ramp speed. All channels ramp in parallel. 0 disables this channel's ramp.",
        )
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
        self.displayedParameters.insert(self.displayedParameters.index(self.MONITOR) + 1, self.RAMP_RATE)
        self.displayedParameters.append(self.MODULE)
        self.displayedParameters.append(self.ID)
        self.displayedParameters.append(self.DISPLAY)

    def initGUI(self, item: dict) -> None:
        super().initGUI(item)
        self._upgrade_toggle_widget(self.ENABLED, _AMPR_CHANNEL_ON_LABEL, _AMPR_CHANNEL_TOGGLE_MIN_WIDTH)
        self._upgrade_toggle_widget(self.ACTIVE, "Manual", 72)
        _configure_channel_entry(getattr(self, "tree", None))
        self._sync_enabled_toggle_widget()
        self._sync_monitor_feedback()
        self.scalingChanged()
        for name in (self.VALUE, self.RAMP_RATE):
            parameter = self.getParameterByName(name)
            if parameter is not None and getattr(parameter, "spin", None) is not None:
                parameter.spin.setKeyboardTracking(False)
                if name == self.VALUE:
                    parameter.spin.editingFinished.connect(self._retry_failed_setpoint)

    def applyValue(self, apply: bool = False) -> None:
        if not self.real:
            return
        controller = getattr(self, "controller", None) or getattr(self.channelParent, "controller", None)
        if controller is not None:
            # Explorer marks lastAppliedValue before dispatch; AMPR must wait
            # for the hardware setpoint readback instead.
            controller.applyValueFromThread(self, force=apply)

    def _retry_failed_setpoint(self) -> None:
        if getattr(self, "_ampr_setpoint_state", "") in {"error", "mismatch"}:
            self.applyValue(apply=True)

    def _setpoint_feedback(self, state: str, target: float, detail: str) -> None:
        """Update the existing voltage cell, never replace the user's input."""
        self._ampr_setpoint_state = state
        parameter = self.getParameterByName(self.VALUE)
        widget = getattr(parameter, "spin", None)
        if widget is not None:
            tooltip = f"Requested output: {target:.3f} V. {detail}"
            if widget.toolTip() != tooltip:
                widget.setToolTip(tooltip)
        self._sync_monitor_feedback()

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
        if self.enabled and not getattr(self.channelParent, "loading", False):
            _finish_channel_edit(self)
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
        """Use the native palette for OFF cells without changing the saved plot colour."""
        try:
            from PyQt6.QtCore import Qt
            from PyQt6.QtGui import QBrush, QColor
            from PyQt6.QtWidgets import QCheckBox, QSizePolicy
        except ImportError:
            return None
        self._ampr_colors_active = self._output_enabled()
        if self._ampr_colors_active:
            color = super().updateColor()
        else:
            color = QColor()
            for index in range(len(self.parameters) + 1):
                self.setBackground(index, QBrush())
            for parameter in self.parameters:
                widget = parameter.getWidget()
                if widget is not None:
                    widget.setStyleSheet("")
                    container = getattr(widget, "container", None)
                    if container is not None:
                        container.setStyleSheet("")
            self.defaultStyleSheet = ""
        self._sync_monitor_feedback()

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
        if hasattr(widget, "setFocusPolicy"):
            from PyQt6.QtCore import Qt
            # Mouse OFF must not commit a new voltage before requesting zero.
            # ON explicitly finishes the editor in enabledChanged instead.
            widget.setFocusPolicy(Qt.FocusPolicy.TabFocus)

    def _output_enabled(self) -> bool:
        """Gate cell colours, not hardware state or confirmation of shutdown."""
        if not getattr(self, "enabled", False) or not getattr(self, "real", False):
            return False
        parent = getattr(self, "channelParent", None)
        controller = getattr(parent, "controller", None)
        # Explorer restores channels before creating the controller (initGUI)
        # and onAction (finalizeInit). Its isOn() requires that action to exist.
        if not getattr(controller, "initialized", False) or getattr(parent, "onAction", None) is None:
            return False
        state = getattr(controller, "main_state", None)
        # Missing state is not ON. An explicit Shutdown unconfirmed, however,
        # must retain feedback while the controller/action are kept for retry.
        transitioning = getattr(controller, "transitioning", False) or getattr(controller, "ramping", False)
        return bool(state and state not in {"Disconnected", "ST_STBY"} and (parent.isOn() or transitioning))

    def _monitor_feedback_state(self) -> str:
        """Classify monitor accuracy relative to the current setpoint."""
        if not self._output_enabled():
            return "default"
        controller = self.channelParent.controller
        if controller.main_state == _AMPR_SHUTDOWN_UNCONFIRMED_STATE:
            return "error"
        transitioning = getattr(controller, "transitioning", False) or getattr(controller, "ramping", False)
        if not transitioning:
            if getattr(self, "_ampr_setpoint_state", "confirmed") != "confirmed":
                return "default"
            if not getattr(controller, "acquiring", False):
                return "default"

        monitor_value = _coerce_float(getattr(self, "monitor", np.nan), np.nan)
        target_value = (0.0 if transitioning and getattr(controller, "transition_target_on", None) is False
                        else _coerce_float(getattr(self, "value", np.nan), np.nan))
        if not np.isfinite(monitor_value) or not np.isfinite(target_value):
            return "default"

        reference = max(abs(target_value), _AMPR_MONITOR_RELATIVE_FLOOR_V)
        relative_error = abs(monitor_value - target_value) / reference
        if relative_error <= _AMPR_MONITOR_OK_RELATIVE_TOLERANCE:
            return "ok"
        if relative_error <= _AMPR_MONITOR_WARN_RELATIVE_TOLERANCE:
            return "warn"
        return "error"

    def _sync_monitor_feedback(self) -> None:
        """Refresh feedback, but leave all OFF cells neutral (errors remain in tooltips/logs)."""
        active = self._output_enabled()
        if getattr(self, "_ampr_colors_active", None) != active and callable(getattr(self, "setBackground", None)):
            self.updateColor()
        getter = getattr(self, "getParameterByName", None)
        if not callable(getter):
            return
        value_parameter = getter(getattr(self, "VALUE", "Value"))
        value_widget = getattr(value_parameter, "spin", None)
        if value_widget is not None:
            state = getattr(self, "_ampr_setpoint_state", "confirmed") if active else "stored"
            style = (_AMPR_MONITOR_ERROR_STYLE if state in {"error", "mismatch"}
                     else _AMPR_MONITOR_WARN_STYLE if state in {"pending", "sent"} else "")
            value_widget.setStyleSheet(style)
        # Keep the measured value readable with the native palette. Its tracking
        # feedback belongs to the clickable Status control, not the numeric cell.
        for name in (getattr(self, "MONITOR", "Monitor"), getattr(self, "ENABLED", "Enabled")):
            parameter = getter(name)
            widget_getter = getattr(parameter, "getWidget", None)
            widget = widget_getter() if callable(widget_getter) else getattr(parameter, "widget", None)
            if widget is not None and hasattr(widget, "setStyleSheet"):
                widget.setStyleSheet("")
        if widget is None or not hasattr(widget, "setStyleSheet"):
            return

        state = self._monitor_feedback_state() if active else "default"
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

    _MODULE_ADDRESS_MIN = 0
    _MODULE_ADDRESS_MAX = 11
    _CHANNEL_NUMBER_MIN = 1
    _CHANNEL_NUMBER_MAX = 4

    def _module_address_raw(self) -> int | None:
        """Return the configured module address, or None when unparseable."""
        try:
            return int(self.module)
        except (TypeError, ValueError):
            return None

    def module_address(self) -> int:
        """Return the configured AMPR module address as an integer.

        An invalid or out-of-range address must never silently fall back to
        module 0: return -1 so callers skip the channel instead of driving
        the wrong hardware.
        """
        address = self._module_address_raw()
        if address is None or not (
            self._MODULE_ADDRESS_MIN <= address <= self._MODULE_ADDRESS_MAX
        ):
            return -1
        return address

    def channel_number(self) -> int:
        """Return the configured AMPR channel number as an integer.

        An invalid or out-of-range channel must never silently fall back to
        CH1: return 0 so callers skip the channel instead of driving the
        wrong output.
        """
        try:
            number = int(self.id)
        except (TypeError, ValueError):
            return 0
        if not (self._CHANNEL_NUMBER_MIN <= number <= self._CHANNEL_NUMBER_MAX):
            return 0
        return number

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
        self._cancel_ramp = False
        self._last_output_targets: dict[tuple[int, int], float] = {}
        self.transitioning = False
        self.transition_target_on: bool | None = None
        self._transition_lock = Lock()
        self._setpoint_lock = Lock()
        self._pending_setpoints: dict[tuple[int, int], _AMPRSetpoint] = {}
        self._latest_setpoints: dict[tuple[int, int], _AMPRSetpoint] = {}
        self._setpoint_cancel = Event()
        self._setpoint_thread: Thread | None = None
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
            guidance = self._init_failure_guidance(exc)
            message = (
                f"AMPR initialization failed on COM{int(self.controllerParent.com)}: "
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
        guidance = _ampr_poisoned_port_guidance(
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
            apply_limits = getattr(self.controllerParent, "_apply_module_voltage_limits", None)
            export_config = getattr(self.controllerParent, "exportConfiguration", None)
            if callable(apply_limits) and apply_limits() and callable(export_config):
                self.controllerParent.exportConfiguration(useDefaultFile=True)
        self.initializeValues()
        self.initialized = True
        # A fresh transport reached this far, so any earlier in-process port
        # poisoning is no longer relevant for this COM port.
        self._poisoned_com = None
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
        if getattr(self, "_cancel_ramp", False):
            if self._begin_transition(False):
                self.toggleOnFromThread(parallel=True)
            return
        if self.main_state == "ST_ON":
            start_acquisition = getattr(self, "startAcquisition", None)
            if callable(start_acquisition):
                start_acquisition()
        if getattr(self.controllerParent, "isOn", lambda: False)():
            with contextlib.suppress(Exception):
                self.controllerParent.updateValues(apply=False)
            if self._begin_transition(True):
                self.toggleOnFromThread(parallel=True)

    def runAcquisition(self) -> None:
        """Poll AMPR readbacks while reusing the acquisition-loop lock."""
        while self.acquiring:
            started = time.monotonic()
            try:
                with self._controller_lock_section(
                    "Could not acquire lock to acquire AMPR data.",
                    timeout_s=1.0,
                    log_timeout=False,
                ):
                    if not self.acquiring:
                        break  # A ramp/OFF may have taken over while this thread waited.
                    self.readNumbers(already_acquired=True)
                    self.signalComm.updateValuesSignal.emit()
            except TimeoutError:
                continue
            finally:
                time.sleep(max(0.0, _AMPR_MONITOR_INTERVAL_S - (time.monotonic() - started)))

    def readNumbers(self, *, already_acquired: bool = False) -> None:
        if self.device is None or not getattr(self, "initialized", False):
            self.initializeValues(reset=True)
            return

        self._update_state(already_acquired=already_acquired)
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

        poll_device = self.device
        for module in poll_modules:
            try:
                with self._controller_lock_section(
                    f"Could not acquire lock to read AMPR module {module}.",
                    already_acquired=already_acquired,
                ):
                    device = self.device
                    if device is None:
                        return
                    voltages = device.get_module_voltages(module)
                    # Polling already reads both requested and measured voltage.
                    # Check the request while holding the same hardware lock so
                    # an old frame can never confirm a newer write.
                    self._confirm_setpoints(module, voltages, device)
                    if not hasattr(self, "_last_output_targets"):
                        self._last_output_targets = {}
                    for channel_id, data in voltages.items():
                        target = data.get("setpoint")
                        if target is not None and np.isfinite(target):
                            self._last_output_targets[(module, channel_id)] = float(target)
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

        if self.device is poll_device:
            self.values = new_values

    def fakeNumbers(self) -> None:
        self.initializeValues(reset=True)
        # Do not fabricate AMPR output readbacks in ESIBD test mode.
        # Showing random "measured" voltages is misleading because no hardware
        # communication happened at all in that mode.

    def applyValue(self, channel: AMPRChannel) -> None:
        self.applyValueFromThread(channel)

    def applyValueFromThread(self, channel: AMPRChannel, *, force: bool = False) -> None:
        """Snapshot on the GUI thread; one worker serializes all pending writes."""
        _invoke_gui_callback(lambda: self._queue_setpoint(channel, force=force))

    def _queue_setpoint(self, channel: AMPRChannel, *, force: bool = False) -> None:
        if self.device is None or not self.initialized or not self.controllerParent.isOn():
            channel.lastAppliedValue = np.nan
            feedback = getattr(channel, "_setpoint_feedback", None)
            if callable(feedback):
                feedback("stored", float(channel.value), "Stored only; no active ON command/communication.")
            return
        if force and channel.enabled:
            _finish_channel_edit(channel)
        request = _AMPRSetpoint(channel, self.device, self._setpoint_cancel)
        with self._setpoint_lock:
            previous = self._latest_setpoints.get(request.key)
            if (previous is not None and previous.channel is channel
                    and previous.target == request.target and previous.value == request.value
                    and previous.device is request.device and previous.cancel is request.cancel
                    and (not force or previous.state in {"pending", "sent"})):
                return
            self._latest_setpoints[request.key] = request
            # Invalid edits must also supersede an older, still queued target.
            self._pending_setpoints.pop(request.key, None)
        module, number = request.key
        limit = self._module_voltage_limit(module)
        if not 0 <= module < 12 or not 1 <= number <= _CHANNELS_PER_MODULE or not np.isfinite(request.target) or abs(request.target) > limit:
            self._publish_setpoint(request, "error", f"Refusing invalid target/address (module rating ±{limit:.0f} V).")
            return
        if request.cancel.is_set():
            self._publish_setpoint(request, "stored", "Not sent; output is stopping.")
            return
        # Publish pending BEFORE exposing the request to an already running
        # worker; a very fast ACK must not be overwritten by this initial state.
        self._publish_setpoint(request, "pending", "Waiting for communication; not yet applied.")
        with self._setpoint_lock:
            if self._latest_setpoints.get(request.key) is not request or request.cancel.is_set():
                return
            self._pending_setpoints[request.key] = request
        self._start_setpoint_worker()

    def _start_setpoint_worker(self) -> None:
        if not hasattr(self, "_setpoint_lock"):
            return
        failed = []
        with self._setpoint_lock:
            if (not self._pending_setpoints or self._setpoint_thread is not None
                    or self.transitioning or self.ramping):
                return
            try:
                self._setpoint_thread = Thread(target=self._write_pending_setpoints,
                                              name=f"{self.controllerParent.name} setpoints", daemon=True)
                self._setpoint_thread.start()
            except Exception as exc:  # noqa: BLE001
                self._setpoint_thread = None
                failed = list(self._pending_setpoints.values())
                self._pending_setpoints.clear()
                failure = str(exc)
        for request in failed:
            self._publish_setpoint(request, "error", f"Cannot start setpoint worker: {failure}")

    def _write_pending_setpoints(self) -> None:
        while True:
            with self._setpoint_lock:
                if not self._pending_setpoints or self.transitioning or self.ramping:
                    # Release ownership atomically: an edit arriving now must
                    # either belong to this worker or start the next one.
                    self._setpoint_thread = None
                    return
            try:
                with self._controller_lock_section("Waiting to send AMPR setpoints.", log_timeout=False):
                    with self._setpoint_lock:
                        if not self._pending_setpoints or self.transitioning or self.ramping:
                            continue
                        key = next(iter(self._pending_setpoints))
                        request = self._pending_setpoints.pop(key)
                    if request.cancel.is_set() or request.device is not self.device or not self.initialized:
                        self._publish_setpoint(request, "stored", "Not sent; communication was stopped.")
                        continue
                    if self.main_state != "ST_ON":
                        self._publish_setpoint(request, "error", "Not sent; AMPR is not in ST_ON.")
                        continue
                    # A native timeout is an I/O failure, NOT lock contention:
                    # do not retry it or mistake it for an unsent command.
                    try:
                        status = request.device.set_module_voltage(*key, request.target)
                        if status != request.device.NO_ERR:
                            raise RuntimeError(f"AMPR rejected the command: {self._format_status(status, request.device)}")
                    except Exception as exc:  # noqa: BLE001
                        self._publish_setpoint(request, "error", str(exc))
                        if _transport_failure_is_fatal(exc):
                            self._handle_transport_loss()
                    else:
                        self._last_output_targets[key] = request.target
                        self._publish_setpoint(request, "sent", "Sent; awaiting the hardware setpoint readback.")
            except TimeoutError:
                # Retain every channel's latest request until the poll/ramp
                # releases the lock. Never access widgets from this worker.
                continue
            except Exception as exc:  # noqa: BLE001
                with self._setpoint_lock:
                    failed = [item for item in self._latest_setpoints.values()
                              if item.state in {"pending", "sent"}]
                    self._pending_setpoints.clear()
                    self._setpoint_thread = None
                for item in failed:
                    self._publish_setpoint(item, "error", f"Setpoint worker failed: {exc}")
                return

    def _publish_setpoint(self, request: _AMPRSetpoint, state: str, detail: str) -> None:
        with self._setpoint_lock:
            if (self._latest_setpoints.get(request.key) is not request
                    or (request.cancel.is_set() and state not in {"stored", "error"})):
                return
            changed = request.state != state
            if not changed and request.detail == detail:
                return
            request.state, request.detail = state, detail
        if changed and state in {"error", "mismatch"}:
            self.print(f"AMPR module {request.key[0]} CH{request.key[1]}, requested {request.target:.3f} V: {detail}", flag=PRINT.ERROR)

        def update_gui() -> None:
            with self._setpoint_lock:
                if (self._latest_setpoints.get(request.key) is not request
                        or request.state != state or request.detail != detail
                        or (request.cancel.is_set() and state not in {"stored", "error"})):
                    return
            if changed and state == "error":
                self.errorCount += 1
            channel = request.channel
            channel.lastAppliedValue = request.value if state == "confirmed" else np.nan
            feedback = getattr(channel, "_setpoint_feedback", None)
            if callable(feedback):
                feedback(state, request.target, detail)

        _invoke_gui_callback(update_gui)

    def _confirm_setpoints(self, module: int, voltages: dict, device: Any) -> None:
        if not hasattr(self, "_setpoint_lock"):
            return
        with self._setpoint_lock:
            requests = [request for key, request in self._latest_setpoints.items()
                        if key[0] == module and request.device is device
                        and not request.cancel.is_set()
                        and request.state in {"sent", "confirmed", "mismatch"}]
        for request in requests:
            actual = _coerce_float(voltages.get(request.key[1], {}).get("setpoint"), np.nan)
            if not np.isfinite(actual):
                self._publish_setpoint(request, "sent", "Hardware setpoint readback unavailable; not confirmed.")
            elif round(actual, request.decimals) == round(request.target, request.decimals):
                # This is display precision, not an electrical output tolerance.
                self._publish_setpoint(request, "confirmed", f"Hardware setpoint: {actual:.3f} V. Measured voltage is shown in Monitor.")
            else:
                self._publish_setpoint(request, "mismatch", f"Hardware setpoint is {actual:.3f} V, not the requested value.")

    def _cancel_setpoints(self) -> None:
        if not hasattr(self, "_setpoint_lock"):
            return
        with self._setpoint_lock:
            self._setpoint_cancel.set()
            self._pending_setpoints.clear()
            requests = list(self._latest_setpoints.values())
        for request in requests:
            if request.state not in {"error", "mismatch"}:
                self._publish_setpoint(request, "stored", "Request cancelled by OFF/disconnection; not confirmed.")

    def updateValues(self) -> None:
        if self.values is None:
            return

        self._sync_status_to_gui()
        device_is_on = self.controllerParent.isOn()
        transitioning = getattr(self, "transitioning", False) or getattr(self, "ramping", False)
        monitoring = (getattr(self, "initialized", False) and self.main_state == "ST_ON"
                      and (device_is_on or transitioning))
        for channel in self.controllerParent.getChannels():
            if channel.enabled and channel.real and monitoring:
                # A settling output is still a valid measurement. Do not hide
                # it (or the last queued ramp sample) behind a stabilization timer.
                channel.monitor = self.values.get(
                    (channel.module_address(), channel.channel_number()),
                    np.nan,
                )
            else:
                channel.monitor = np.nan
            sync_feedback = getattr(channel, "_sync_monitor_feedback", None)
            if callable(sync_feedback):
                sync_feedback()

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
        rates = getattr(self, "_transition_rates", None)
        if rates is None:
            rates = self._channel_ramp_rates()
        targets = getattr(self, "_transition_targets", None)
        if targets is None:
            targets = self._channel_target_voltages(respect_device_state=False)
        state_updated = False
        startup_targets: dict[tuple[int, int], float] = {}

        try:
            if self.controllerParent.isOn():
                self._check_ramp_cancelled()
                try:
                    with self._controller_lock_section(
                        "Could not acquire lock to toggle the AMPR PSU."
                    ):
                        device = self.device
                        if device is None:
                            self._restore_off_ui_state()
                            return
                        self._check_ramp_cancelled()
                        self.print(
                            f"Starting AMPR PSU. Waiting up to {startup_timeout_s:.1f} s for ST_ON."
                        )
                        device.initialize(timeout_s=startup_timeout_s)
                        self._check_ramp_cancelled()
                        status = device.NO_ERR
                except TimeoutError:
                    # Even a timed-out startup may already have enabled the PSU.
                    raise
                if status == device.NO_ERR:
                    self._refresh_module_scan()
                    self._update_state()
                    state_updated = True
                    apply_limits = getattr(self.controllerParent, "_apply_module_voltage_limits", None)
                    export_config = getattr(self.controllerParent, "exportConfiguration", None)
                    if callable(apply_limits) and apply_limits() and callable(export_config):
                        self.controllerParent.exportConfiguration(useDefaultFile=True)
                    startup_targets = dict(targets)
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
                            rates_v_s=rates,
                            label="up",
                        )
            else:
                self._ramp_down_and_shutdown(rates)
                return
        except _AMPRRampCancelled:
            self.print("AMPR ramp-up interrupted by OFF; stopping outputs.")
            self._ramp_down_and_shutdown(rates)
            return
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self._safe_disable_after_toggle_failure(startup_targets)
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
                self.print(
                    "AMPR PSU ON sequence ended in an unexpected state: "
                    f"{self.main_state}.{self._runtime_diagnostics(device=device)}",
                    flag=PRINT.ERROR,
                )
                self._safe_disable_after_toggle_failure(startup_targets)
                return
            start_acquisition = getattr(self, "startAcquisition", None)
            if callable(start_acquisition):
                start_acquisition()
            self.print("AMPR PSU turned ON. State: ST_ON.")

    def closeCommunication(self, *, final_state: str | None = None) -> None:
        self._cancel_setpoints()
        if (final_state or self.main_state) == _AMPR_SHUTDOWN_UNCONFIRMED_STATE and self.device is not None:
            # Keep both the backend and Explorer's closing warning until OFF succeeds.
            self.acquiring = False
            self.initialized = True
            self.main_state = _AMPR_SHUTDOWN_UNCONFIRMED_STATE
            self.device_state_summary = self.interlock_state_summary = self.voltage_state_summary = "Unknown"
            self._restore_on_ui_state()
            self._sync_status_to_gui()
            return
        base_close = getattr(super(), "closeCommunication", None)
        if callable(base_close):
            base_close()
        if final_state is None:
            final_state = getattr(self, "_forced_close_state", None)
        if final_state is None:
            # Absence of a communication object is not proof of a safe stop.
            final_state = self.main_state
            if self.device is not None or final_state not in (
                "Disconnected", _AMPR_SHUTDOWN_UNCONFIRMED_STATE, _AMPR_COMMUNICATION_LOST_STATE
            ):
                final_state = _AMPR_SHUTDOWN_UNCONFIRMED_STATE
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
        self._cancel_setpoints()
        device = self.device
        if device is None:
            self.closeCommunication()
            return self.main_state == "Disconnected"

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
                    shutdown_confirmed = self.main_state == "Disconnected"
                else:
                    shutdown_confirmed = device.shutdown() is True
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
            if shutdown_confirmed:
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

    def _update_state(self, *, already_acquired: bool = False) -> None:
        if self.main_state == _AMPR_SHUTDOWN_UNCONFIRMED_STATE:
            return  # Only a new explicit, confirmed OFF may clear this diagnosis.
        if self.device is None:
            return  # Preserve the last shutdown/transport-loss diagnosis.

        try:
            with self._controller_lock_section(
                "Could not acquire lock to refresh the AMPR state.",
                already_acquired=already_acquired,
            ):
                device = self.device
                if device is None:
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
        if (self.device is not device or not getattr(self, "initialized", False)
                or self.main_state == _AMPR_SHUTDOWN_UNCONFIRMED_STATE):
            return
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
        # ESIBD setting attributes are widget-backed properties, not plain data.
        def _refresh_gui() -> None:
            self.controllerParent.main_state = self.main_state
            self.controllerParent.detected_modules = self.detected_modules_text
            self.controllerParent.device_state_summary = self.device_state_summary
            self.controllerParent.interlock_state_summary = self.interlock_state_summary
            self.controllerParent.voltage_state_summary = self.voltage_state_summary
            sync_acquisition_controls = getattr(
                self.controllerParent,
                "_sync_acquisition_controls",
                None,
            )
            if callable(sync_acquisition_controls):
                sync_acquisition_controls()
            get_channels = getattr(self.controllerParent, "getChannels", lambda: [])
            for channel in get_channels():
                sync_feedback = getattr(channel, "_sync_monitor_feedback", None)
                if callable(sync_feedback):
                    sync_feedback()
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

        self._cancel_setpoints()
        device = self.device
        self.device = None
        self.initialized = False
        self._last_output_targets = {}
        if device is None:
            return

        try:
            if getattr(device, "connected", True):
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
            status, _state_hex, state = getter(
                timeout_s=float(
                    getattr(self.controllerParent, "poll_timeout_s", 5.0)
                )
            )
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
        def _update_gui() -> None:
            sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_on_state):
                sync_on_state(False)
                return
            if hasattr(self.controllerParent, "onAction"):
                self.controllerParent.onAction.state = False
            sync_local = getattr(self.controllerParent, "_sync_local_on_action", None)
            if callable(sync_local):
                sync_local()

        _invoke_gui_callback(_update_gui)

    def _restore_on_ui_state(self) -> None:
        """Restore toolbar ON/OFF widgets back to ON after a failed shutdown."""
        def _update_gui() -> None:
            sync_on_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_on_state):
                sync_on_state(True)
                return
            if hasattr(self.controllerParent, "onAction"):
                self.controllerParent.onAction.state = True
            sync_local = getattr(self.controllerParent, "_sync_local_on_action", None)
            if callable(sync_local):
                sync_local()

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
            already_acquired=already_acquired,
        ) as lock_acquired:
            if not lock_acquired:
                raise TimeoutError(timeout_message)
            yield

    def _request_off(self) -> bool:
        """Cancel before waiting for serial I/O; the existing worker performs OFF."""
        self._cancel_ramp = True
        self._cancel_setpoints()
        with self._transition_guard():
            if self.transitioning:
                self.transition_target_on = False
                return False
            # The previous worker may have just relinquished ownership.
            return True

    def _check_ramp_cancelled(self) -> None:
        if getattr(self, "_cancel_ramp", False):
            raise _AMPRRampCancelled()

    def _channel_ramp_rates(self) -> dict[tuple[int, int], float]:
        """Capture committed per-channel rates on the GUI thread."""
        default = float(getattr(self.controllerParent, "ramp_rate_v_s", 10.0))
        return {(ch.module_address(), ch.channel_number()): float(getattr(ch, "ramp_rate_v_s", default))
                for ch in getattr(self.controllerParent, "getChannels", lambda: [])() if ch.real}

    def _begin_transition(self, target_on: bool) -> bool:
        """Capture controls on the GUI thread before starting the one ON/OFF worker."""
        with self._transition_guard():
            if self.transitioning:
                return False
            self._transition_targets = self._channel_target_voltages(respect_device_state=False)
            self._transition_rates = self._channel_ramp_rates()
            self.transitioning = True
            self.transition_target_on = bool(target_on)
            if target_on:
                self._cancel_ramp = False
            if not target_on:
                self._cancel_setpoints()
            elif hasattr(self, "_setpoint_lock"):
                with self._setpoint_lock:
                    if self._setpoint_cancel.is_set():
                        self._setpoint_cancel = Event()
                    # Startup targets need the same ACK/readback tracking as
                    # later edits. Otherwise their GUI state can stay "stored"
                    # after the ramp and suppress the normal Status feedback.
                    requests = [
                        _AMPRSetpoint(channel, self.device, self._setpoint_cancel)
                        for channel in self.controllerParent.getChannels()
                        if channel.real and (channel.module_address(), channel.channel_number())
                        in self._transition_targets
                    ]
                    for request in requests:
                        self._latest_setpoints[request.key] = request
                        self._pending_setpoints[request.key] = request
                for request in requests:
                    self._publish_setpoint(request, "pending", "Startup target awaiting ramp and hardware setpoint readback.")
                # No separate write worker: the ramp consumes each request at
                # its target, and its existing readbacks confirm it.
            return True

    def _end_transition(self) -> None:
        """Do not lose an OFF arriving between the last ramp step and worker exit."""
        stop_attempted = False
        while True:
            with self._transition_guard():
                must_stop = (not stop_attempted and getattr(self, "_cancel_ramp", False)
                             and getattr(self, "transition_target_on", None) is False
                             and self.device is not None
                             and self.main_state not in {_AMPR_SHUTDOWN_UNCONFIRMED_STATE,
                                                         _AMPR_COMMUNICATION_LOST_STATE, "Disconnected"})
                if not must_stop:
                    self.transitioning = False
                    self.transition_target_on = None
                    self._transition_targets = self._transition_rates = None
                    break
            stop_attempted = True
            self._ramp_down_and_shutdown(self._transition_rates or {})
        self._start_setpoint_worker()

    def _ramp_down_and_shutdown(self, rates: dict[tuple[int, int], float]) -> None:
        """Descend from accepted/read-back targets, never from unfinished GUI text."""
        try:
            with self._controller_lock_section("Could not acquire lock before AMPR ramp-down."):
                reached = {key: value for key, value in getattr(self, "_last_output_targets", {}).items()
                           if key in rates}
            self._ramp_target_voltages(start_targets=reached, end_targets=dict.fromkeys(reached, 0.0),
                                      rates_v_s=rates, label="down")
        except Exception as exc:  # noqa: BLE001
            self.errorCount += 1
            self.print(f"AMPR ramp-down before shutdown failed: {self._format_exception(exc)}", flag=PRINT.ERROR)
        if not self.shutdownCommunication():
            self._restore_on_ui_state()

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
            module = channel.module_address()
            channel_id = channel.channel_number()
            if module < 0 or channel_id <= 0:
                self.print(
                    f"Skipping channel with invalid module/channel configuration "
                    f"(module {channel.module!r} CH{channel.id!r}); fix the channel "
                    "configuration.",
                    flag=PRINT.ERROR,
                )
                continue
            target_voltage = 0.0
            if channel.enabled and (device_is_on or not respect_device_state):
                target_voltage = float(channel.value)
            targets[(module, channel_id)] = target_voltage
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
        cancellable: bool = False,
    ) -> None:
        """Apply targets serially, checking OFF between channels during ramp-up."""
        if not hasattr(self, "_last_output_targets"):
            self._last_output_targets = {}
        if cancellable:
            for key, voltage in sorted(targets.items()):
                self._check_ramp_cancelled()
                self._apply_target_voltages_locked({key: voltage}, device=device)
            self._check_ramp_cancelled()
            return
        for module, module_targets in sorted(self._group_target_voltages(targets).items()):
            voltage_limit = self._module_voltage_limit(module)
            for channel_id, voltage in sorted(module_targets.items()):
                if not np.isfinite(voltage) or abs(float(voltage)) > voltage_limit:
                    raise RuntimeError(
                        f"Refusing {float(voltage):.3f} V for module {module} CH{channel_id}: "
                        f"detected module rating is ±{voltage_limit:.0f} V."
                    )
            if hasattr(device, "set_module_voltages"):
                statuses = device.set_module_voltages(module, module_targets)
                if set(statuses) != set(module_targets):
                    raise RuntimeError("AMPR did not acknowledge every channel in the ramp step.")
                for channel_id, status in statuses.items():
                    if status != device.NO_ERR:
                        raise RuntimeError(
                            "AMPR rejected "
                            f"{float(module_targets[channel_id]):.3f} V for module "
                            f"{module} CH{channel_id}: {self._format_status(status, device=device)}"
                        )
                    self._last_output_targets[(module, channel_id)] = module_targets[channel_id]
                continue

            for channel_id, voltage in sorted(module_targets.items()):
                status = device.set_module_voltage(module, channel_id, voltage)
                if status != device.NO_ERR:
                    raise RuntimeError(
                        "AMPR rejected "
                        f"{float(voltage):.3f} V for module "
                        f"{module} CH{channel_id}: {self._format_status(status, device=device)}"
                    )
                self._last_output_targets[(module, channel_id)] = voltage

    def _apply_target_voltages(
        self,
        targets: dict[tuple[int, int], float],
        *,
        timeout_message: str,
        cancellable: bool = False,
    ) -> None:
        """Apply a full AMPR target map under the controller lock."""
        if not targets:
            return

        with self._controller_lock_section(timeout_message):
            device = self.device
            if device is None:
                raise RuntimeError("AMPR device is not available.")
            self._apply_target_voltages_locked(targets, device=device, cancellable=cancellable)

    def _ramp_target_voltages(
        self,
        *,
        start_targets: dict[tuple[int, int], float],
        end_targets: dict[tuple[int, int], float],
        rates_v_s: dict[tuple[int, int], float],
        label: str,
    ) -> None:
        """Advance every channel on each tick, using its own speed and elapsed time."""
        keys = sorted(set(start_targets) | set(end_targets))
        current = {key: float(start_targets.get(key, 0.0)) for key in keys}
        targets = {key: float(end_targets.get(key, 0.0)) for key in keys}
        for key in keys:
            rate = rates_v_s.get(key, np.nan)
            if not np.isfinite(rate) or rate < 0.0:
                raise ValueError(f"Invalid AMPR ramp rate for {key}: {rate}")
            if not np.isfinite(current[key]) or not np.isfinite(targets[key]):
                raise ValueError(f"Invalid AMPR ramp voltage for {key}")
        rising = label == "up"
        if rising:
            self._check_ramp_cancelled()
        if not keys:
            return
        self.print(f"Starting AMPR ramp-{label}: all channels in parallel at their configured speeds.")
        self.ramping = True
        last_tick = time.monotonic()
        next_monitor = last_tick
        try:
            while True:
                if rising:
                    self._check_ramp_cancelled()
                    # Only explicitly committed edits may retarget a running ramp.
                    # The worker never reads a focused Qt editor.
                    with self._setpoint_lock:
                        requests = {key: request for key, request in self._pending_setpoints.items()
                                    if key in targets and request.device is self.device
                                    and not request.cancel.is_set()}
                    for key, request in requests.items():
                        targets[key] = request.target
                else:
                    requests = {}
                immediate = {key: targets[key] for key in keys
                             if rates_v_s[key] == 0.0 and targets[key] != current[key]}
                if immediate:
                    self._apply_target_voltages(immediate,
                        timeout_message="Could not acquire lock to apply AMPR voltages.", cancellable=rising)
                    current.update(immediate)
                # A reached, acknowledged target must not be sent again by the
                # ordinary setpoint worker when the transition ends.
                for key, request in requests.items():
                    if current[key] != request.target:
                        continue
                    with self._setpoint_lock:
                        if self._pending_setpoints.get(key) is not request:
                            continue
                        self._pending_setpoints.pop(key)
                    self._publish_setpoint(request, "sent", "Ramp target sent; awaiting hardware setpoint readback.")
                reached = all(current[key] == targets[key] for key in keys)
                now = time.monotonic()
                if reached or now + 1e-9 >= next_monitor:
                    # The normal poller is stopped during transitions. Read in
                    # this same worker, under the same lock as voltage writes:
                    # no concurrent DLL calls and no target masquerading as a measurement.
                    with self._controller_lock_section("Could not acquire lock to read AMPR ramp voltages."):
                        self.readNumbers(already_acquired=True)
                        self.signalComm.updateValuesSignal.emit()
                    next_monitor = now + _AMPR_MONITOR_INTERVAL_S
                    if rising:
                        self._check_ramp_cancelled()
                if reached:
                    break
                time.sleep(max(0.0, _AMPR_RAMP_STEP_S - (time.monotonic() - last_tick)))
                now = time.monotonic()
                elapsed, last_tick = now - last_tick, now
                step_targets = {}
                for key in keys:
                    distance = targets[key] - current[key]
                    if distance == 0.0:
                        continue
                    step = rates_v_s[key] * elapsed
                    step_targets[key] = (targets[key] if abs(distance) <= step + 1e-9
                                         else current[key] + float(np.sign(distance)) * step)
                self._apply_target_voltages(step_targets,
                    timeout_message="Could not acquire lock to apply AMPR ramp step.", cancellable=rising)
                current.update(step_targets)
        finally:
            self.ramping = False
        self.print(f"AMPR ramp-{label} completed.")

    def _safe_disable_after_toggle_failure(
        self,
        targets: dict[tuple[int, int], float],
    ) -> bool:
        """Confirm disable and port closure, or preserve an explicit OFF retry."""
        self._cancel_setpoints()
        device = self.device
        if device is None:
            return False

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
                        status, enabled = device.enable_psu(False)
                    except Exception as cleanup_exc:  # noqa: BLE001
                        cleanup_errors.append(f"disable_psu failed: {cleanup_exc}")
                    else:
                        if status != device.NO_ERR:
                            cleanup_errors.append(
                                f"disable_psu failed: {self._format_status(status, device=device)}"
                            )
                        elif enabled is not False:
                            cleanup_errors.append("PSU disable was not confirmed")
                    if not cleanup_errors:
                        try:
                            if device.disconnect() is not True:
                                cleanup_errors.append("port closure was not confirmed")
                        except Exception as cleanup_exc:
                            cleanup_errors.append(f"port closure failed: {cleanup_exc}")
        except TimeoutError:
            cleanup_errors.append("lock timeout")

        if cleanup_errors:
            self.print(
                "AMPR startup cleanup encountered issues: " + "; ".join(cleanup_errors),
                flag=PRINT.WARNING,
            )
            self.closeCommunication(final_state=_AMPR_SHUTDOWN_UNCONFIRMED_STATE)
            return False
        self.closeCommunication(final_state="Disconnected")
        self._restore_off_ui_state()
        self.print("AMPR startup cleanup disabled the PSU and disconnected after failure.", flag=PRINT.WARNING)
        return True

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
