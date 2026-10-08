"""Control the CGC ESI source, two HV supplies, and heater module."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import logging
import sys
import time
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Any, cast

import numpy as np

from esibd.core import (
    PARAMETERTYPE,
    PLUGINTYPE,
    PRINT,
    Channel,
    DeviceController,
    Parameter,
    parameterDict,
)
from esibd.plugins import Device, LiveDisplay, Plugin


_RUNTIME_PREFIX = "_esibd_bundled_esi_runtime"
_ESI_DRIVER_CLASS: type[Any] | None = None
# Serializes the private runtime load and driver-class publish across threads.
_RUNTIME_LOAD_LOCK = RLock()
_GUI_DISPATCH_LOCK = RLock()
_ESI_MAX_VOLTAGE = 3000.0
_ESI_HV_MAX_VOLTAGE_STEP = 10.008
_ESI_MAX_TEMPERATURE = 175.0
_ESI_HEAT_MODULE = 0
_ESI_HV_CHANNELS = ((1, 1), (2, 2))
_ESI_HV_MODULES = (1, 2)
_ESI_MODULES = (_ESI_HEAT_MODULE, *_ESI_HV_MODULES)
_ESI_COMMUNICATION_LOST = "Communication lost"
_ESI_STOPPING = "Stopping: checking HV"
_PARAMETER_UNIT_KEY = getattr(Parameter, "UNIT", "Unit")
_ESI_POWER_ON_ICON = "switch-medium_on.png"
_ESI_POWER_OFF_ICON = "switch-medium_off.png"
_ESI_HV_CARD_WIDTH = 300
_ESI_CARD_SPACING = 12
_ESI_HEAT_CARD_WIDTH = 2 * _ESI_HV_CARD_WIDTH + _ESI_CARD_SPACING

_ESI_PANEL_CARD_ON = "QFrame { background-color: #162433; border: 1px solid #3182ce; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_CARD_OFF = "QFrame { background-color: #202938; border: 1px solid #64748b; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_CARD_DISC = "QFrame { background-color: #242424; border: 1px solid #555555; border-radius: 8px; color: #dddddd; }"
_ESI_PANEL_CARD_STOPPING = "QFrame { background-color: #292720; border: 1px solid #d69e2e; border-radius: 8px; color: #f7fafc; }"
_ESI_BTN_NEUTRAL = "QPushButton { background-color: #393939; color: #aaaaaa; font-weight: 600; border-radius: 4px; }"
_ESI_PANEL_NEUTRAL = "color: #aaaaaa; font-weight: 600;"
_ESI_PANEL_CARD_ERR = "QFrame { background-color: #2d1719; border: 1px solid #ef4444; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_CARD_HEAT = "QFrame { background-color: #2d1b0e; border: 1px solid #d97706; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_TITLE = "color: #f8fafc; font-weight: 700; font-size: 14px;"
_ESI_PANEL_NAME = "color: #cbd5e1; font-weight: 600;"
_ESI_PANEL_VALUE = "color: #f8fafc; font-weight: 600;"
_ESI_PANEL_OFF = "color: #94a3b8; font-weight: 600;"
_ESI_PANEL_OK = "color: #4ade80; font-weight: 600;"
_ESI_PANEL_STANDBY = "color: #d69e2e; font-weight: 600;"
_ESI_PANEL_ERR = "color: #f87171; font-weight: 700;"
_ESI_HEAT_INPUT = ("QDoubleSpinBox { background-color: #202938; color: #f8fafc; "
                   "border: 1px solid #64748b; border-radius: 3px; padding: 2px; } "
                   "QDoubleSpinBox:disabled { color: #94a3b8; }")
_ESI_BTN_HV_ACTIVE = "QPushButton { background-color: #3182ce; color: #f8fafc; font-weight: 700; border-radius: 4px; }"
_ESI_BTN_HV_OFF = "QPushButton { background-color: #374151; color: #bfdbfe; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"
_ESI_BTN_OFF_ACTIVE = "QPushButton { background-color: #4b5563; color: #e2e8f0; font-weight: 600; border-radius: 4px; }"
_ESI_BTN_OFF_INACTIVE = "QPushButton { background-color: #374151; color: #94a3b8; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"
_ESI_BTN_HEAT_ACTIVE = "QPushButton { background-color: #d97706; color: #1a1a2e; font-weight: 700; border-radius: 4px; }"
_ESI_BTN_HEAT_OFF = "QPushButton { background-color: #374151; color: #fbbf24; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int conversion returning *default* on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """Best-effort bool conversion returning *default* on failure."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    try:
        return bool(int(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _runtime_module_name(plugin_dir: Path) -> str:
    digest = hashlib.sha256(str(plugin_dir.resolve()).encode()).hexdigest()[:12]
    return f"{_RUNTIME_PREFIX}_{digest}"


def _get_temperature_stability_class():
    """Load the shared, hardware-free qualifier in a private namespace."""
    path = Path(__file__).resolve().with_name("_heater_stability.py")
    if not path.is_file():
        raise ModuleNotFoundError(f"Missing bundled heater stability helper: {path}")
    source = path.read_bytes()
    digest = hashlib.sha256(str(path).encode() + source).hexdigest()[:16]
    name = f"_esibd_bundled_esi_stability_{digest}"
    with _RUNTIME_LOAD_LOCK:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError(str(path))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            try:
                exec(compile(source, str(path), "exec"), module.__dict__)
            except BaseException:
                sys.modules.pop(name, None)
                raise
        return sys.modules[name].TemperatureStability


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


def _load_runtime_package(name: str, runtime_dir: Path) -> None:
    if name in sys.modules:
        return
    spec = importlib.util.spec_from_file_location(
        name,
        runtime_dir / "__init__.py",
        submodule_search_locations=[str(runtime_dir)],
    )
    if spec is None or spec.loader is None:
        raise ModuleNotFoundError("Could not create the bundled ESI runtime package.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise


def _get_esi_driver_class() -> type[Any]:
    """Load the ESI driver lazily from this plugin's private runtime."""
    global _ESI_DRIVER_CLASS
    if _ESI_DRIVER_CLASS is not None:
        return _ESI_DRIVER_CLASS
    with _RUNTIME_LOAD_LOCK:
        if _ESI_DRIVER_CLASS is not None:
            return _ESI_DRIVER_CLASS
        plugin_dir = Path(__file__).resolve().parent
        runtime_dir = plugin_dir / "vendor" / "runtime"
        if not (runtime_dir / "__init__.py").is_file():
            raise ModuleNotFoundError(
                "Bundled ESI runtime not found in vendor/runtime; installation is incomplete."
            )
        runtime_name = _runtime_module_name(plugin_dir)
        _load_runtime_package(runtime_name, runtime_dir)
        module = importlib.import_module(f"{runtime_name}.esi")
        _ESI_DRIVER_CLASS = cast(type[Any], module.ESI)
        return _ESI_DRIVER_CLASS


_ESI_CURRENT_FUNCTION = "HV current"


def _is_current_channel(channel: Any) -> bool:
    return (getattr(channel, "_current_measurement", False)
            or getattr(channel, "function", "") == _ESI_CURRENT_FUNCTION)


def _fixed_channel_items(device_name: str) -> list[dict[str, Any]]:
    """Return three output controls and two read-only HV current channels."""
    channels = [
        {
            "Name": f"{device_name}_HV{number}",
            "Module": address,
            "Function": "HVPS-3kB (+/- pair)",
            "Enabled": False,
            "Active": True,
            "Real": True,
            "Value": 0.0,
            "Min": 0.0,
            "Max": _ESI_MAX_VOLTAGE,
            "Display": True,
        }
        for number, address in _ESI_HV_CHANNELS
    ]
    channels.append(
        {
            "Name": f"{device_name}_HEAT",
            "Module": _ESI_HEAT_MODULE,
            "Function": "HEAT-CTRL-2410",
            "Enabled": False,
            "Active": True,
            "Real": True,
            "Value": 20.0,
            "Min": 0.0,
            "Max": _ESI_MAX_TEMPERATURE,
            "Display": True,
        }
    )
    channels.extend(
        {
            "Name": f"{device_name}_HV{number}_I",
            "Module": address,
            "Function": _ESI_CURRENT_FUNCTION,
            "Enabled": True,
            "Active": True,
            "Real": True,
            "Value": np.nan,
            "Display": True,
            "Color": color,
        }
        for (number, address), color in zip(_ESI_HV_CHANNELS, ("#3182ce", "#dd6b20"))
    )
    return channels


class _ESILiveDisplay(LiveDisplay):
    """Keep current, voltage and temperature on separate physical axes."""

    def initFig(self) -> None:
        super().initFig()
        for plot in self.livePlotWidgets:
            item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
            if getattr(item, "groupLabel", None) is not None and item.legend is not None:
                item.legend.setOffset((8, 24))  # Keep the unit heading above the traces' legend.

    def getGroups(self):
        groups = {}
        for name, channels in super().getGroups().items():
            units = dict.fromkeys(channel.unit for channel in channels)
            if len(units) <= 1:
                groups[name] = channels
            else:
                for unit in units:
                    groups[f"{name} ({unit})"] = [ch for ch in channels if ch.unit == unit]
        self.channelGroups = groups  # Explorer's plot loop consumes this cache.
        return groups

    def plotGroup(self, livePlotWidget, timeAxes, channels, apply) -> None:
        from pyqtgraph import ViewBox

        super().plotGroup(livePlotWidget, timeAxes, channels, apply)
        if not channels or any(channel.unit != "A" for channel in channels):
            return
        view = livePlotWidget if isinstance(livePlotWidget, ViewBox) else livePlotWidget.getViewBox()
        if view is None or not view.autoRangeEnabled()[1]:
            return  # Do not replace a manual Y zoom.
        if any(ch.plotCurve is not None and ch.plotCurve.opts["logMode"][1] for ch in channels):
            return
        bounds = view.childrenBounds()[1]
        if bounds is None or not np.all(np.isfinite(bounds)) or bounds[0] != bounds[1]:
            return
        # A constant nanoamp current must not inherit pyqtgraph's default 1 A span.
        current = float(bounds[0])
        half_span = 10.0 ** np.floor(np.log10(abs(current))) if current else 1e-12
        view.setRange(yRange=(current - half_span, current + half_span),
                      padding=0, disableAutoRange=False)


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


def _set_parameter_quietly(channel: Any, constant: str, attr: str, value: Any) -> None:
    """Set a channel parameter without its event (the caller holds the device's loading counter)."""
    getter = getattr(channel, "getParameterByName", None)
    parameter = getter(getattr(channel, constant, constant.title())) if callable(getter) else None
    setter = getattr(parameter, "setValueWithoutEvents", None)
    if callable(setter):
        setter(value)
    else:
        setattr(channel, attr, value)


def providePlugins() -> "list[type[Plugin]]":
    return [ESIDevice]


def _create_card_grid(parent: Any, max_columns: int, spacing: int = 12) -> Any:
    """Keep the usual card grid, wrapping to fewer columns in a narrow dock."""
    from PyQt6.QtCore import QRect, QSize
    from PyQt6.QtWidgets import QLayout

    class CardGrid(QLayout):
        def __init__(self) -> None:
            super().__init__(parent)
            self._items = []
            self.setContentsMargins(0, 0, 0, 0)
            self.setSpacing(spacing)

        def addItem(self, item) -> None:
            self._items.append(item)
            self.invalidate()

        def count(self) -> int:
            return len(self._items)

        def itemAt(self, index):
            return self._items[index] if 0 <= index < self.count() else None

        def takeAt(self, index):
            if 0 <= index < self.count():
                item = self._items.pop(index)
                self.invalidate()
                return item
            return None

        def minimumSize(self):
            size = QSize(0, 0)
            for item in self._items:
                size = size.expandedTo(item.minimumSize())
            return size

        def sizeHint(self):
            columns = min(max_columns, self.count())
            if not columns:
                return QSize(0, 0)
            width = max(item.sizeHint().width() for item in self._items)
            width = columns * width + (columns - 1) * self.spacing()
            return QSize(width, self.heightForWidth(width))

        def hasHeightForWidth(self) -> bool:
            return True

        def heightForWidth(self, width: int) -> int:
            return self._arrange(QRect(0, 0, width, 0), apply=False)

        def setGeometry(self, rect) -> None:
            super().setGeometry(rect)
            self._arrange(rect, apply=True)

        def _arrange(self, rect, *, apply: bool) -> int:
            if not self._items:
                return 0
            gap = self.spacing()
            minimum = max(1, self.minimumSize().width())
            columns = min(max_columns, self.count(), max(1, (rect.width() + gap) // (minimum + gap)))
            width = max(minimum, (rect.width() - (columns - 1) * gap) // columns)
            width = min(width, max(item.maximumSize().width() for item in self._items))
            left = rect.x() + max(0, (rect.width() - columns * width - (columns - 1) * gap) // 2)
            y = rect.y()
            for start in range(0, self.count(), columns):
                row = self._items[start:start + columns]
                heights = [
                    max(item.minimumSize().height(),
                        item.heightForWidth(width) if item.hasHeightForWidth() else item.sizeHint().height())
                    for item in row
                ]
                if apply:
                    for column, (item, height) in enumerate(zip(row, heights)):
                        item.setGeometry(QRect(left + column * (width + gap), y, width, height))
                y += max(heights) + gap
            return y - rect.y() - gap

    return CardGrid()


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


class ESIDevice(Device):
    """Electrospray HV and HEAT-CTRL-2410 controller."""

    documentation = (
        "Controls CGC ESI HVPS-3kB and HEAT-CTRL-2410 modules and monitors "
        "voltage, current, temperature, power, interlocks, and controller health."
    )
    name = "ESI"
    version = "0.1.0"
    supportedVersion = "1.0.2"
    pluginType = PLUGINTYPE.INPUTDEVICE
    unit = "V"
    useMonitors = True
    useOnOffLogic = True
    iconFile = "esi.png"
    channels: "list[ESIChannel]"

    COM = "COM"
    BAUDRATE = "Baud rate"
    CONNECT_TIMEOUT = "Connect timeout (s)"
    POLL_TIMEOUT = "Poll timeout (s)"
    RAMP_RATE = "Ramp rate (V/s)"
    HEAT_VOLTAGE_LIMIT = "Heat voltage limit (V)"
    HEAT_CURRENT_LIMIT = "Heat current limit (A)"
    HEAT_POWER_LIMIT = "Heat power limit (W)"
    STATE = "State"
    INTERLOCK = "Interlock"
    MODULES = "Modules"
    HEAT_STATUS = "Heat status"
    OPERATING_CONFIG = "Operating config"
    AVAILABLE_CONFIGS = "Available configs"
    LOADED_CONFIG = "Loaded config"

    LiveDisplay = _ESILiveDisplay

    def appendOutputData(self, h5file, useAllHistory: bool | None = None, *, useDefaultFile: bool = False) -> None:
        """Keep Explorer's file schema, but record each channel's physical unit."""
        from esibd.const import OUTPUTCHANNELS, UNIT

        # Explorer 1.0.1 calls this with useAllHistory=; 0.8.x used useDefaultFile=.
        # The host's second positional parameter has the same meaning in both.
        full_history = useDefaultFile if useAllHistory is None else useAllHistory
        path = f"{self.name}/{OUTPUTCHANNELS}"
        previous = set(h5file[path]) if path in h5file else set()
        super().appendOutputData(h5file, full_history)
        group = h5file.get(path)
        if group is None:
            return
        for channel in self.getDataChannels():
            names = (channel.name, channel.name + "_BG") if self.useBackgrounds else (channel.name,)
            for name in names:
                if name in group and name not in previous:
                    group[name].attrs[UNIT] = channel.unit

    def exportConfigurationIfChanged(self) -> None:
        """Explorer 1.0.2 also runs this periodic save from a worker thread, but the
        export refreshes Explorer's file tree (Qt GUI work): run it on the GUI thread."""
        base = getattr(super(), "exportConfigurationIfChanged", None)
        if callable(base):
            _invoke_gui_callback(base)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.channelType = ESIChannel

    def initGUI(self) -> None:
        super().initGUI()
        if hasattr(self, "initAction"):
            self.initAction.setVisible(False)
        self.controller = ESIController(controllerParent=self)

    def finalizeInit(self) -> None:
        super().finalizeInit()
        _session_start(self)  # Resumes a device left ON by an Explorer crash.
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_load_config_action()
        self._ensure_operator_panel()
        self._update_channel_column_visibility()

    def _ensure_load_config_action(self) -> None:
        """Expose a toolbar button that loads the selected operating config."""
        if getattr(self, "loadConfigButton", None) is not None:
            return
        title_bar = getattr(self, "titleBar", None)
        if title_bar is None:
            return
        try:
            from PyQt6.QtWidgets import QPushButton
        except ImportError:
            return
        button = QPushButton("Load config")
        button.setToolTip(
            "Load the NVM configuration selected in 'Operating config' onto the "
            "controller. The ESI is forced safe OFF around the load."
        )
        insert_before = getattr(self, "stretchAction", None)
        if insert_before is not None and hasattr(title_bar, "insertWidget"):
            action = title_bar.insertWidget(insert_before, button)
        elif hasattr(title_bar, "addWidget"):
            action = title_bar.addWidget(button)
        else:
            return
        if hasattr(button, "clicked") and hasattr(button.clicked, "connect"):
            button.clicked.connect(self._load_config_clicked)
        self.loadConfigButton = button
        self.loadConfigAction = action
        self._sync_load_config_action()

    def _sync_load_config_action(self) -> None:
        button = getattr(self, "loadConfigButton", None)
        if button is None:
            return
        controller = getattr(self, "controller", None)
        button.setEnabled(
            controller is not None
            and getattr(controller, "initialized", False)
            and getattr(controller, "device", None) is not None
            and getattr(controller, "main_state", "") not in (_ESI_STOPPING, "Shutdown unconfirmed")
        )

    def _load_config_clicked(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is None:
            return
        load_now = getattr(controller, "loadOperatingConfigNowFromThread", None)
        if callable(load_now):
            load_now(parallel=True)
            return
        controller.loadOperatingConfigNow()

    def _ensure_local_on_action(self) -> None:
        if (
            not self.useOnOffLogic
            or hasattr(self, "deviceOnAction")
            or not hasattr(self, "closeCommunicationAction")
        ):
            return
        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(on=checked),
            toolTipFalse=f"Turn {self.name} ON.",
            iconFalse=self.makeIcon(_ESI_POWER_ON_ICON),
            toolTipTrue=f"Turn {self.name} OFF and disconnect.",
            iconTrue=self.makeIcon(_ESI_POWER_OFF_ICON),
            before=self.closeCommunicationAction,
            restore=False,
            defaultState=False,
        )
        self._sync_local_on_action()

    def _sync_local_on_action(self) -> None:
        _session_sync(self)  # Crash resume record follows the ON state.
        action = getattr(self, "deviceOnAction", None)
        if action is None:
            return
        # StateAction.toggled updates the icon/tooltip; only triggered sends commands.
        action.state = self.isOn()

    def _ensure_status_widgets(self) -> None:
        """Add compact ESI status labels to the plugin toolbar."""
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
        state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        if state in ("STATE_ON", "ST_ON"):
            background = "#2f855a"
        elif state in ("Disconnected",):
            background = "#718096"
        elif state == _ESI_STOPPING:
            background = "#975a16"
        elif "lost" in state.lower() or "unconfirmed" in state.lower():
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
        com = getattr(self, "com", "?")
        interlock = str(getattr(self, "interlock_state", "n/a") or "n/a")
        heat = str(getattr(self, "heat_status", "") or "")
        parts = [f"COM{com}", f"Interlock: {interlock}"]
        if heat:
            parts.append(heat)
        return " | ".join(parts)

    def _update_heat_stability_display(self) -> None:
        """Refresh only the thermal label; expiry never polls or commands hardware."""
        heat = getattr(self, "esiHeatWidgets", None)
        controller = getattr(self, "controller", None)
        if not heat or controller is None:
            return
        stability = controller.heat_stability_status()
        state = stability["state"]
        inactive = not controller.initialized or controller.main_state in (
            "Disconnected", "Connection pending", "Communication lost", "Shutdown unconfirmed", _ESI_STOPPING)
        if inactive:
            state = "n/a"
        elif state in ("Stable", "Stabilizing") and self.esiHeatButton.text() != "ON":
            state = "Unavailable"
        label = heat["heat_stability"]
        label.setText(state)
        label.setStyleSheet(_ESI_PANEL_OK if state == "Stable" else
                           _ESI_PANEL_STANDBY if state == "Stabilizing" else _ESI_PANEL_NEUTRAL)
        slope = stability["slope_c_min"]
        detail = (f"Observed span: {min(stability['span_s'], 60.):.0f}/60 s. "
                  + (f"Drift: {slope:+.3f} °C/min." if slope is not None else "Drift not yet available."))
        label.setToolTip(
            "Measured temperature within ±0.2 °C of the applied target for a full 60 s window, "
            "with absolute fitted drift <0.1 °C/min. " + detail +
            " Missing/invalid data, OFF and new commands reset qualification. "
            "This is not an independent accuracy, pressure-equilibrium or electrical-safety check."
        )
        timer = self._heat_stability_timer
        expiry = stability["expires_at_s"]
        if inactive or expiry is None:
            timer.stop()
        else:
            timer.start(max(1, int(np.ceil((expiry - time.monotonic()) * 1000)) + 1))

    def _update_status_widgets(self) -> None:
        badge = getattr(self, "statusBadgeLabel", None)
        summary = getattr(self, "statusSummaryLabel", None)
        if badge is None or summary is None:
            return
        state = str(getattr(self, "main_state", "Disconnected") or "Disconnected")
        summary_text = self._status_summary_text()
        tooltip = "\n".join((
            f"State: {state}",
            f"COM: {getattr(self, 'com', '?')}",
            f"Interlock: {getattr(self, 'interlock_state', 'n/a')}",
            f"Modules: {getattr(self, 'detected_modules', 'n/a')}",
            f"Heat: {getattr(self, 'heat_status', 'n/a')}",
        ))
        if hasattr(badge, "setText"):
            badge.setText(state)
        if hasattr(badge, "setToolTip"):
            badge.setToolTip(tooltip)
        if hasattr(badge, "setStyleSheet"):
            badge.setStyleSheet(self._status_badge_style())
        if hasattr(summary, "setText"):
            summary.setText(summary_text)
        if hasattr(summary, "setToolTip"):
            summary.setToolTip(tooltip)
        self._sync_load_config_action()
        self._update_operator_panel()

    def _update_channel_column_visibility(self) -> None:
        """Hide framework columns not useful for the ESI UI."""
        if self.tree is None or not self.channels:
            return
        parameter_names = list(self.channels[0].getSortedDefaultChannel())
        for hidden_name in (Channel.COLLAPSE, Channel.REAL):
            if hidden_name in parameter_names:
                self.tree.setColumnHidden(parameter_names.index(hidden_name), True)

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

        _invoke_gui_callback(_update_gui)

    def _finish_setpoint_edits(self, channel: Any = None) -> None:
        for ch in [channel] if channel is not None else self.getChannels():
            if getattr(ch, "is_current_channel", lambda: False)():
                continue
            target = (getattr(self, "esiHeatTarget", None) if ch.is_heat_channel() else
                      (getattr(self, "esiHVCards", {}).get(ch.module_address(), {}) or {}).get("target"))
            if ch.is_heat_channel():
                power = getattr(self, "esiHeatPowerLimit", None)
                if power is not None and power.isEnabled() and _finish_spinbox_edit(power):
                    self.heat_power_limit_w = float(power.value())
            if _finish_spinbox_edit(target):
                # Suppress the value event while committing the draft; the caller applies it.
                # Explorer 1.0.2: Channel.loading is read-only and reflects this device counter.
                self.loading = True
                try:
                    ch.value = float(target.value())
                finally:
                    self.loading = False
            else:
                getter = getattr(ch, "getParameterByName", None)
                parameter = getter(getattr(ch, "VALUE", "Value")) if callable(getter) else None
                _finish_spinbox_edit(getattr(parameter, "spin", None))

    def setOn(self, on: "bool | None" = None) -> None:
        _session_request(self, bool(on) if on is not None
                         else bool(self.isOn()) if hasattr(self, "onAction") else False)  # Crash resume record.
        if on is not None and hasattr(self, "onAction") and self.onAction.state is not on:
            self.onAction.state = on
        self._sync_local_on_action()
        if getattr(self, "loading", False):
            return
        controller = getattr(self, "controller", None)
        if controller and (
            getattr(controller, "initializing", False)
            or getattr(controller, "transitioning", False)
        ):
            self.print(
                f"{self.name} ON/OFF transition already in progress.",
                flag=PRINT.WARNING,
            )
            return
        if self.isOn():
            self._finish_setpoint_edits()
            # ON never energizes: every output starts deselected, its target or temperature kept.
            # A crash resume instead adopts the outputs as they run (ESIController._adopt_hardware_state).
            if not getattr(controller, "resume_session", False):
                self._deselect_outputs("ON")
        if controller and getattr(controller, "initialized", False):
            controller.toggleOnFromThread(parallel=True)
        elif hasattr(self, "onAction") and self.isOn():
            self.initializeCommunication()

    def toggleRecording(self, on: "bool | None" = None, manual: bool = True) -> None:
        super().toggleRecording(on=on, manual=manual)
        _session_sync(self)

    def _deselect_outputs(self, when: str) -> list[str]:
        """Set every HV/HEAT output selection OFF without any command; targets stay as they were."""
        deselected = []
        self.loading = True  # No enabled event: nothing is sent; the hardware is OFF or being forced OFF.
        try:
            for channel in self.getChannels():
                if _is_current_channel(channel) or not getattr(channel, "enabled", False):
                    continue
                getter = getattr(channel, "getParameterByName", None)
                try:
                    parameter = getter(getattr(channel, "ENABLED", "Enabled")) if callable(getter) else None
                except KeyError:
                    parameter = None
                setter = getattr(parameter, "setValueWithoutEvents", None)
                if callable(setter):
                    setter(False)
                else:
                    channel.enabled = False
                unit = "°C" if channel.is_heat_channel() else "V"
                deselected.append(f"{channel.name} (target {float(channel.value):g} {unit} kept)")
        finally:
            self.loading = False
        if deselected:
            self.print(f"Outputs start OFF at {when}: " + ", ".join(deselected)
                       + ". Select an output to energize it.")
        update = getattr(self, "_update_operator_panel", None)
        if callable(update) and hasattr(self, "esiHVCards"):
            update()
        return deselected

    def _ensure_operator_panel(self) -> None:
        """Replace the channel table with a compact operator control panel."""
        if hasattr(self, "esiPanel"):
            self._update_operator_panel()
            return
        try:
            from PyQt6.QtCore import Qt, QTimer
            from PyQt6.QtWidgets import (
                QButtonGroup,
                QDoubleSpinBox,
                QFrame,
                QGridLayout,
                QHBoxLayout,
                QLabel,
                QPushButton,
                QSizePolicy,
                QVBoxLayout,
                QWidget,
            )
        except ImportError:
            return

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        cards_row = QWidget()
        cards_layout = _create_card_grid(cards_row, 2, _ESI_CARD_SPACING)

        self.esiHVCards: dict[int, dict[str, Any]] = {}
        for module_number, address in ((1, 1), (2, 2)):
            card = QFrame()
            card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            card.setFixedWidth(_ESI_HV_CARD_WIDTH)
            cl = QVBoxLayout(card)
            cl.setContentsMargins(12, 12, 12, 12)
            cl.setSpacing(8)

            title = QLabel(f"HVPS-3kB · HV{module_number} (+/- pair)")
            title.setStyleSheet(_ESI_PANEL_TITLE)
            cl.addWidget(title)

            sel_row = QHBoxLayout()
            sel_row.setContentsMargins(0, 0, 0, 0)
            sel_row.setSpacing(6)
            btn_on = QPushButton("+/- ON")
            btn_off = QPushButton("OFF")
            # A mouse click must not commit a pending voltage before OFF.
            btn_on.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            btn_off.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            for btn in (btn_on, btn_off):
                btn.setCheckable(True)
                btn.setFixedHeight(32)
                btn.setMinimumWidth(90)
            btn_on.setStyleSheet(_ESI_BTN_HV_OFF)
            btn_off.setStyleSheet(_ESI_BTN_OFF_ACTIVE)
            sel_group = QButtonGroup(card)
            sel_group.setExclusive(True)
            sel_group.addButton(btn_on, 1)
            sel_group.addButton(btn_off, 0)
            sel_row.addStretch(1)
            sel_row.addWidget(btn_on)
            sel_row.addWidget(btn_off)
            sel_row.addStretch(1)
            cl.addLayout(sel_row)

            target_label = QLabel("Set")
            target_label.setStyleSheet(_ESI_PANEL_NAME)
            target_value = QDoubleSpinBox()
            target_value.setKeyboardTracking(False)
            target_value.setRange(0.0, _ESI_MAX_VOLTAGE)
            target_value.setDecimals(1)
            target_value.setSingleStep(10.0)
            target_value.setSuffix(" V")
            target_value.setValue(0.0)
            target_value.setStyleSheet(_ESI_PANEL_VALUE)
            target_value.setFixedHeight(28)
            target_value.valueChanged.connect(
                lambda val, addr=address: self._panel_target_changed(addr, val)
            )
            measured_label = QLabel("ADC readback")
            measured_label.setStyleSheet(_ESI_PANEL_NAME)
            measured_value = QLabel("n/a")
            measured_value.setStyleSheet(_ESI_PANEL_VALUE)
            hardware_target_label = QLabel("HW target")
            hardware_target_label.setStyleSheet(_ESI_PANEL_NAME)
            hardware_target_value = QLabel("n/a")
            hardware_target_value.setStyleSheet(_ESI_PANEL_VALUE)
            module_gate_label = QLabel("Module")
            module_gate_label.setStyleSheet(_ESI_PANEL_NAME)
            module_gate_value = QLabel("n/a")
            module_gate_value.setStyleSheet(_ESI_PANEL_VALUE)
            gate_label = QLabel("Global gate")
            gate_label.setStyleSheet(_ESI_PANEL_NAME)
            gate_value = QLabel("n/a")
            gate_value.setStyleSheet(_ESI_PANEL_VALUE)
            current_label = QLabel("Current")
            current_label.setStyleSheet(_ESI_PANEL_NAME)
            current_value = QLabel("n/a")
            current_value.setStyleSheet(_ESI_PANEL_VALUE)
            control_label = QLabel("HV control")
            control_label.setStyleSheet(_ESI_PANEL_NAME)
            control_value = QLabel("n/a")
            control_value.setStyleSheet(_ESI_PANEL_VALUE)
            pwm_label = QLabel("PWM set / measured")
            pwm_label.setStyleSheet(_ESI_PANEL_NAME)
            pwm_value = QLabel("n/a")
            pwm_value.setStyleSheet(_ESI_PANEL_VALUE)
            led_label = QLabel("Module LED")
            led_label.setStyleSheet(_ESI_PANEL_NAME)
            led_value = QLabel("n/a")
            led_value.setStyleSheet(_ESI_PANEL_VALUE)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(6)
            grid.addWidget(target_label, 0, 0)
            grid.addWidget(target_value, 0, 1)
            grid.addWidget(hardware_target_label, 1, 0)
            grid.addWidget(hardware_target_value, 1, 1)
            grid.addWidget(module_gate_label, 2, 0)
            grid.addWidget(module_gate_value, 2, 1)
            grid.addWidget(gate_label, 3, 0)
            grid.addWidget(gate_value, 3, 1)
            grid.addWidget(control_label, 4, 0)
            grid.addWidget(control_value, 4, 1)
            grid.addWidget(pwm_label, 5, 0)
            grid.addWidget(pwm_value, 5, 1)
            grid.addWidget(led_label, 6, 0)
            grid.addWidget(led_value, 6, 1)
            grid.addWidget(measured_label, 7, 0)
            grid.addWidget(measured_value, 7, 1)
            grid.addWidget(current_label, 8, 0)
            grid.addWidget(current_value, 8, 1)
            cl.addLayout(grid)

            cards_layout.addWidget(card)
            self.esiHVCards[address] = {
                "card": card,
                "sel_group": sel_group,
                "btn_on": btn_on,
                "btn_off": btn_off,
                "target": target_value,
                "hardware_target": hardware_target_value,
                "module_gate": module_gate_value,
                "gate": gate_value,
                "control": control_value,
                "pwm": pwm_value,
                "led": led_value,
                "measured": measured_value,
                "current": current_value,
            }
            sel_group.idClicked.connect(
                lambda gid, addr=address: self._panel_output_selected(addr, gid)
            )
        layout.addWidget(cards_row)

        heat_card = QFrame()
        heat_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        heat_card.setMaximumWidth(_ESI_HEAT_CARD_WIDTH)
        heat_card.setStyleSheet(_ESI_PANEL_CARD_OFF)
        heat_cl = QVBoxLayout(heat_card)
        heat_cl.setContentsMargins(12, 12, 12, 12)
        heat_cl.setSpacing(8)

        heat_header = QHBoxLayout()
        heat_header.setContentsMargins(0, 0, 0, 0)
        heat_header.setSpacing(8)
        heat_title = QLabel("HEAT-CTRL-2410")
        heat_title.setStyleSheet(_ESI_PANEL_TITLE)
        heat_btn = QPushButton("OFF")
        heat_btn.setFixedHeight(32)
        heat_btn.setMinimumWidth(80)
        heat_btn.setStyleSheet(_ESI_BTN_OFF_ACTIVE)
        heat_btn.setCheckable(True)
        heat_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        heat_header.addWidget(heat_title)
        heat_header.addStretch(1)
        heat_header.addWidget(heat_btn)
        heat_cl.addLayout(heat_header)

        controls = QGridLayout()
        self.esiHeatTarget = QDoubleSpinBox()
        self.esiHeatPowerLimit = QDoubleSpinBox()
        for row, (title, spin, suffix) in enumerate((
            ("Temperature", self.esiHeatTarget, " °C"),
            ("Power limit", self.esiHeatPowerLimit, " W"),
        )):
            spin.setDecimals(3)
            spin.setRange(0., 1000.)  # Disabled until device maxima are available.
            spin.setSuffix(suffix)
            spin.setKeyboardTracking(False)
            spin.setEnabled(False)
            controls.addWidget(QLabel(title), row, 0)
            controls.addWidget(spin, row, 1)
        self.esiHeatPowerLimit.setSpecialValueText("Keep device limit")
        self.esiHeatTarget.valueChanged.connect(self._panel_heat_target_changed)
        self.esiHeatPowerLimit.valueChanged.connect(self._panel_heat_power_changed)
        self._heat_limits_timer = QTimer(heat_card)
        self._heat_limits_timer.setSingleShot(True)
        self._heat_limits_timer.timeout.connect(self._update_heat_controls)
        heat_cl.addLayout(controls)

        heat_grid = QGridLayout()
        heat_grid.setContentsMargins(0, 0, 0, 0)
        heat_grid.setHorizontalSpacing(10)
        heat_grid.setVerticalSpacing(6)
        heat_widgets = {}
        for row, (name, key) in enumerate((
            ("Applied target", "heat_target"),
            ("Measured", "heat_measured"),
            ("Applied power limit", "heat_power_limit"),
            ("PID power target", "heat_power"),
            ("Sensor", "heat_sensor"),
            ("Interlock", "heat_interlock"),
            ("Stability", "heat_stability"),
        )):
            nl = QLabel(name)
            nl.setStyleSheet(_ESI_PANEL_NAME)
            vl = QLabel("n/a")
            vl.setStyleSheet(_ESI_PANEL_VALUE)
            heat_grid.addWidget(nl, row, 0)
            heat_grid.addWidget(vl, row, 1)
            heat_widgets[key] = vl
        heat_cl.addLayout(heat_grid)

        heat_row = QWidget()
        heat_layout = QHBoxLayout(heat_row)
        heat_layout.setContentsMargins(0, 0, 0, 0)
        heat_layout.addWidget(heat_card)
        layout.addWidget(heat_row)

        self.esiPanel = panel
        self.esiHeatCard = heat_card
        self.esiHeatButton = heat_btn
        self.esiHeatWidgets = heat_widgets
        heat_btn.toggled.connect(self._panel_heat_toggled)
        self._heat_stability_timer = QTimer(heat_card)
        self._heat_stability_timer.setSingleShot(True)
        self._heat_stability_timer.timeout.connect(self._update_heat_stability_display)

        if self.tree is not None:
            self.tree.setVisible(False)
        layout.addStretch(1)
        self.addContentWidget(_scrollable_panel(panel))
        self._update_operator_panel()

    def _panel_target_changed(self, address: int, value: float) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if (channel.module_address() == address and not channel.is_heat_channel()
                    and not _is_current_channel(channel)):
                channel.getParameterByName(channel.VALUE).value = float(value)
                break
        self._update_operator_panel()

    def _panel_output_selected(self, address: int, gid: int) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if (channel.module_address() == address and not channel.is_heat_channel()
                    and not _is_current_channel(channel)):
                want_enabled = gid == 1
                if channel.enabled != want_enabled:
                    if want_enabled:
                        self._finish_setpoint_edits(channel)
                    channel.getParameterByName(channel.ENABLED).value = want_enabled
                break
        self._update_operator_panel()

    def _heater_channel(self):
        return next((ch for ch in self.getChannels()
                     if ch.is_heat_channel() and not _is_current_channel(ch)), None)

    def _panel_heat_target_changed(self, value: float) -> None:
        if not getattr(self, "loading", False):
            channel = self._heater_channel()
            if channel is not None:
                channel.getParameterByName(channel.VALUE).value = float(value)

    def _panel_heat_power_changed(self, value: float) -> None:
        if not getattr(self, "loading", False):
            self.heat_power_limit_w = float(value)  # Existing persistent setting; its event applies it.

    def _heat_power_limit_changed(self) -> None:
        controller = getattr(self, "controller", None)
        if not getattr(self, "loading", False) and controller is not None and controller.initialized:
            controller.applyHeatPowerFromThread(float(self.heat_power_limit_w))

    def _update_heat_controls(self) -> None:
        if not hasattr(self, "esiHeatTarget"):
            return
        controller = self.controller
        if controller is None:
            for spin in (self.esiHeatTarget, self.esiHeatPowerLimit):
                spin.setEnabled(False)
                spin.setStyleSheet(_ESI_HEAT_INPUT)
                spin.setToolTip("Waiting for fresh, valid device limits.")
            self._heat_limits_timer.stop()
            return
        now = time.monotonic()
        stamp = getattr(controller, "heat_limits_observed_at", float('-inf'))
        expiry = stamp + float(getattr(self, "interval", 1000.)) / 1000. + float(getattr(self, "poll_timeout_s", 3.))
        available = (controller.initialized and controller.main_state == "STATE_ON"
                     and not getattr(controller, "transitioning", False) and now <= expiry)
        channel = self._heater_channel()
        for spin, maximum, requested in (
            (self.esiHeatTarget, controller.heat_max_temperature_c, float(channel.value) if channel else 0.),
            (self.esiHeatPowerLimit, controller.heat_max_power_w, float(getattr(self, "heat_power_limit_w", 50.))),
        ):
            valid = available and np.isfinite(maximum) and maximum > 0
            blocked = spin.blockSignals(True)
            if valid:
                # Round the editor's upper bound down, never above the device maximum.
                spin.setMaximum(float(np.floor(maximum * 1000.) / 1000.))
            if np.isfinite(requested) and not spin.hasFocus():
                spin.setValue(requested)
            spin.blockSignals(blocked)
            spin.setEnabled(bool(valid))
            spin.setToolTip(f"Device maximum: {maximum:g}. Requested: {requested:g}."
                            if valid else "Waiting for fresh, valid device limits.")
        self.esiHeatTarget.setToolTip(self.esiHeatTarget.toolTip() + " " + controller.heat_temperature_error)
        self.esiHeatTarget.setStyleSheet(_ESI_HEAT_INPUT + ("QDoubleSpinBox { color: #f87171; }" if controller.heat_temperature_error else ""))
        self.esiHeatPowerLimit.setToolTip(self.esiHeatPowerLimit.toolTip() +
            f" Applied limit: {controller.heat_power_limit_w:g} W. "
            "Ceiling for the device PID, not imposed or measured power. 0 keeps the device setting. "
            + controller.heat_power_error)
        self.esiHeatPowerLimit.setStyleSheet(_ESI_HEAT_INPUT + ("QDoubleSpinBox { color: #f87171; }" if controller.heat_power_error else ""))
        if available:
            self._heat_limits_timer.start(max(1, int(np.ceil((expiry - now) * 1000)) + 1))
        else:
            self._heat_limits_timer.stop()

    def _panel_heat_toggled(self, checked: bool) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if channel.is_heat_channel():
                if checked:
                    self._finish_setpoint_edits(channel)
                if channel.enabled != checked:
                    channel.getParameterByName(channel.ENABLED).value = checked
                elif not checked:
                    # An unconfirmed OFF must remain retryable even though the
                    # saved selection is already OFF (Parameter may suppress it).
                    channel.applyValue(apply=True)
        self._update_operator_panel()

    def _update_operator_panel(self) -> None:
        cards = getattr(self, "esiHVCards", None)
        if not isinstance(cards, dict):
            return
        controller = getattr(self, "controller", None)
        connected = controller is not None and getattr(controller, "initialized", False)
        values = getattr(controller, "values", {}) or {}
        currents = getattr(controller, "currents", {}) or {}
        targets = getattr(controller, "targets", {}) or {}
        module_active = getattr(controller, "module_active", {}) or {}
        module_control_active = getattr(controller, "module_control_active", {}) or {}
        module_led_rgb = getattr(controller, "module_led_rgb", {}) or {}
        pwm_voltage_set = getattr(controller, "pwm_voltage_set", {}) or {}
        pwm_voltage_measured = getattr(controller, "pwm_voltage_measured", {}) or {}
        measurement_polarity = getattr(controller, "measurement_polarity", {}) or {}
        global_enabled = getattr(controller, "global_enabled", None)
        state = getattr(controller, "main_state", "Disconnected")
        stopping = state == _ESI_STOPPING
        uncertain = state == "Shutdown unconfirmed"
        inactive = not connected or stopping or uncertain
        inactive_style = (_ESI_PANEL_CARD_ERR if uncertain else _ESI_PANEL_CARD_STOPPING
                          if stopping else _ESI_PANEL_CARD_DISC)

        for address, widgets in cards.items():
            card = widgets["card"]
            btn_on = widgets["btn_on"]
            btn_off = widgets["btn_off"]

            output_enabled = False
            target_value = 0.0
            for channel in self.getChannels():
                if (
                    channel.module_address() == address
                    and not channel.is_heat_channel()
                    and not _is_current_channel(channel)
                ):
                    output_enabled = bool(channel.enabled)
                    target_value = abs(float(channel.value))
                    break

            if inactive:
                card.setStyleSheet(inactive_style)
                for btn in (btn_on, btn_off):
                    btn.setEnabled(False)
                    btn.setStyleSheet(_ESI_BTN_NEUTRAL)
                btn_off.setChecked(True)
                for key in (
                    "target",
                    "hardware_target",
                    "module_gate",
                    "gate",
                    "control",
                    "pwm",
                    "led",
                    "measured",
                    "current",
                ):
                    w = widgets[key]
                    w.setEnabled(False)
                    if hasattr(w, "setText"):
                        w.setText("n/a")
                        w.setStyleSheet(_ESI_PANEL_NEUTRAL)
                if stopping:
                    readings = getattr(controller, "discharge_readings", {}).get(address, {})
                    positive = readings.get("positive_v", np.nan)
                    negative = readings.get("negative_v", np.nan)
                    measured = [f"{label} {value:.2f} V" if np.isfinite(value) else f"{label} n/a"
                                for label, value in (("POS", positive), ("NEG", negative))]
                    widgets["measured"].setText("\n".join(measured))
                    current = readings.get("measured_a", np.nan)
                    widgets["current"].setText(f"{current * 1e9:.2f} nA" if np.isfinite(current) else "n/a")
                    widgets["module_gate"].setText("Stopping")
                elif uncertain:
                    widgets["module_gate"].setText("Unconfirmed")
                continue

            for key, widget in widgets.items():
                if key not in ("card", "sel_group", "btn_on", "btn_off"):
                    widget.setEnabled(True)
                    widget.setStyleSheet(_ESI_PANEL_VALUE)
            for btn in (btn_on, btn_off):
                btn.setEnabled(True)
            widgets["target"].setEnabled(True)
            hardware_target = targets.get(address, np.nan)
            expected_hardware_target = target_value if output_enabled else 0.0
            target_confirmed = (
                np.isfinite(hardware_target)
                and np.isclose(
                    hardware_target,
                    expected_hardware_target,
                    rtol=1e-9,
                    atol=1e-6,
                )
            )
            module_enabled = module_active.get(address)
            control_active = module_control_active.get(address)
            control_required = output_enabled and target_value != 0.0
            control_confirmed = not control_required or control_active is True
            output_confirmed = (
                output_enabled
                and module_enabled is True
                and global_enabled is True
                and target_confirmed
                and control_confirmed
            )
            if output_confirmed:
                card.setStyleSheet(_ESI_PANEL_CARD_ON)
                btn_on.setChecked(True)
                btn_on.setStyleSheet(_ESI_BTN_HV_ACTIVE)
                btn_off.setStyleSheet(_ESI_BTN_OFF_INACTIVE)
            elif output_enabled:
                card.setStyleSheet(_ESI_PANEL_CARD_ERR)
                btn_on.setChecked(True)
                btn_on.setStyleSheet(_ESI_BTN_HV_ACTIVE)
                btn_off.setStyleSheet(_ESI_BTN_OFF_INACTIVE)
            else:
                card.setStyleSheet(_ESI_PANEL_CARD_OFF)
                btn_off.setChecked(True)
                btn_on.setStyleSheet(_ESI_BTN_HV_OFF)
                btn_off.setStyleSheet(_ESI_BTN_OFF_ACTIVE)
            # Polling must not replace an uncommitted edit (including its cursor).
            spin = widgets["target"]
            if not spin.hasFocus():
                spin.blockSignals(True)
                spin.setValue(target_value)
                spin.blockSignals(False)
            widgets["hardware_target"].setText(
                f"{hardware_target:.1f} V" if np.isfinite(hardware_target) else "n/a"
            )
            widgets["hardware_target"].setStyleSheet(
                _ESI_PANEL_OK if target_confirmed else _ESI_PANEL_ERR
            )
            widgets["module_gate"].setText(
                "ACTIVE"
                if module_enabled is True
                else "STANDBY"
                if module_enabled is False
                else "n/a"
            )
            widgets["module_gate"].setStyleSheet(
                _ESI_PANEL_OK
                if module_enabled is True and output_enabled
                else _ESI_PANEL_STANDBY
                if module_enabled is False and not output_enabled
                else _ESI_PANEL_ERR
            )
            widgets["gate"].setText(
                "ON" if global_enabled is True else "OFF" if global_enabled is False else "n/a"
            )
            widgets["gate"].setStyleSheet(
                _ESI_PANEL_OK if global_enabled is True else _ESI_PANEL_ERR
            )
            widgets["control"].setText(
                "ACTIVE"
                if control_active is True
                else "IDLE"
                if control_active is False
                else "n/a"
            )
            widgets["control"].setStyleSheet(
                _ESI_PANEL_OK if control_confirmed else _ESI_PANEL_ERR
            )
            pwm_set = pwm_voltage_set.get(address, np.nan)
            pwm_measured = pwm_voltage_measured.get(address, np.nan)
            polarity = measurement_polarity.get(address)
            polarity_code = (
                "NEG"
                if polarity == "negative"
                else "POS"
                if polarity == "positive"
                else "?"
            )
            widgets["pwm"].setText(
                f"{pwm_set:.1f} / {pwm_measured:.1f} V ({polarity_code} ADC)"
                if np.isfinite(pwm_set) and np.isfinite(pwm_measured)
                else "n/a"
            )
            led_rgb = module_led_rgb.get(address)
            led_colors = {
                (False, False, False): "OFF",
                (True, False, False): "RED",
                (False, True, False): "GREEN",
                (False, False, True): "BLUE",
                (True, True, False): "YELLOW",
                (False, True, True): "CYAN",
                (True, False, True): "MAGENTA",
                (True, True, True): "WHITE",
            }
            led_text = led_colors.get(tuple(led_rgb), "n/a") if led_rgb is not None else "n/a"
            widgets["led"].setText(led_text)
            widgets["led"].setStyleSheet(_ESI_PANEL_VALUE)
            measured = values.get(address, np.nan)
            current = currents.get(address, np.nan)
            widgets["measured"].setText(
                f"{polarity_code} {measured:.1f} V"
                if np.isfinite(measured)
                else "n/a"
            )
            widgets["current"].setText(
                f"{current * 1e9:.2f} nA" if np.isfinite(current) else "n/a"
            )

        self._update_heat_controls()
        heat_btn = getattr(self, "esiHeatButton", None)
        heat = getattr(self, "esiHeatWidgets", None)
        heat_card = getattr(self, "esiHeatCard", None)
        if heat_card is not None:
            heat_card.setStyleSheet(inactive_style if inactive else _ESI_PANEL_CARD_OFF)
        if isinstance(heat, dict) and not inactive:
            heat_valid = getattr(controller, "heat_readback_valid", False)
            heat_temp = values.get(_ESI_HEAT_MODULE, np.nan)
            heat_enabled = False
            for channel in self.getChannels():
                if channel.is_heat_channel():
                    heat_enabled = channel.enabled
                    break
            activation = getattr(controller, "heat_activation", {}) or {}
            heat_target = getattr(controller, "heat_target_temperature_c", np.nan)
            heat_confirmed = bool(
                heat_enabled and heat_valid and global_enabled is True
                and all(activation.get(key) is True for key in (
                    "active", "module_active", "module_gate_active",
                    "device_gate_active", "control_active",
                ))
            )
            heat_off_confirmed = (
                activation.get("module_active") is False
                and activation.get("module_gate_active") is False
            )
            if heat_card is not None:
                heat_card.setStyleSheet(_ESI_PANEL_CARD_ON if heat_confirmed else
                                       _ESI_PANEL_CARD_ERR if heat_enabled else _ESI_PANEL_CARD_OFF)
            for widget in heat.values():
                widget.setStyleSheet(_ESI_PANEL_VALUE if heat_enabled else _ESI_PANEL_NEUTRAL)
            if heat_btn is not None:
                loading = getattr(self, "loading", False)
                if not loading:
                    heat_btn.blockSignals(True)
                    heat_btn.setChecked(heat_enabled or not heat_off_confirmed)
                    heat_btn.blockSignals(False)
                heat_btn.setText("ON" if heat_confirmed else
                                 "OFF" if not heat_enabled and heat_off_confirmed else "Unconfirmed")
                heat_btn.setToolTip(
                    "Module and temperature control confirmed; click to turn OFF."
                    if heat_confirmed else "Module disabled; click to turn ON."
                    if not heat_enabled and heat_off_confirmed else
                    "Requested and measured heater states do not agree, or a readback is missing. "
                    "Click to request OFF."
                )
                heat_btn.setStyleSheet(_ESI_BTN_HEAT_ACTIVE if heat_confirmed else _ESI_BTN_NEUTRAL)
                heat_btn.setMinimumWidth(max(80, heat_btn.fontMetrics().horizontalAdvance("Unconfirmed") + 24))
                # Sensor failure must block ON, never the operator's OFF action.
                heat_btn.setEnabled(heat_valid or heat_enabled or not heat_off_confirmed)
            heat["heat_target"].setText(
                f"{heat_target:.1f} °C"
                if np.isfinite(heat_target)
                else "n/a"
            )
            heat["heat_measured"].setText(
                f"{heat_temp:.1f} °C" if heat_valid else "INVALID"
            )
            applied_power = getattr(controller, "heat_power_limit_w", np.nan)
            heat['heat_power_limit'].setText(f'{applied_power:.3f} W' if np.isfinite(applied_power) else 'n/a')
            heat['heat_power_limit'].setStyleSheet(_ESI_PANEL_ERR if controller.heat_power_error else
                                                 _ESI_PANEL_VALUE if heat_enabled else _ESI_PANEL_NEUTRAL)
            heat['heat_power_limit'].setToolTip(controller.heat_power_error)
            heat_power = getattr(controller, "heat_power_w", np.nan)
            heat["heat_power"].setText(
                f"{heat_power:.1f} W" if np.isfinite(heat_power) else "n/a"
            )
            heat["heat_sensor"].setStyleSheet(
                (_ESI_PANEL_OK if heat_valid else _ESI_PANEL_ERR)
                if heat_enabled else _ESI_PANEL_NEUTRAL
            )
            heat["heat_sensor"].setText("OK" if heat_valid else "Invalid")
            interlock = str(getattr(self, "interlock_state", "n/a") or "n/a")
            self._update_heat_stability_display()
            heat["heat_interlock"].setText(interlock)
            heat["heat_interlock"].setStyleSheet(
                (_ESI_PANEL_OK if interlock == "OK" else _ESI_PANEL_ERR)
                if heat_enabled else _ESI_PANEL_NEUTRAL
            )
        elif isinstance(heat, dict):
            for widget in heat.values():
                widget.setText("n/a")
                widget.setStyleSheet(_ESI_PANEL_NEUTRAL)
            if heat_btn is not None:
                blocked = heat_btn.blockSignals(True)
                heat_btn.setChecked(False)
                heat_btn.blockSignals(blocked)
                heat_btn.setText("Stopping" if stopping else "Unknown" if uncertain else "OFF")
                heat_btn.setStyleSheet(_ESI_BTN_NEUTRAL)
                heat_btn.setEnabled(False)

    def getChannels(self) -> "list[ESIChannel]":
        return cast("list[ESIChannel]", super().getChannels())

    def getDefaultSettings(self) -> dict[str, dict]:
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.COM}"] = parameterDict(
            value=1,
            minimum=1,
            maximum=255,
            toolTip="Windows COM port number used by the ESI controller.",
            parameterType=PARAMETERTYPE.INT,
            attr="com",
        )
        settings[f"{self.name}/{self.BAUDRATE}"] = parameterDict(
            value=230400,
            minimum=1,
            maximum=1_000_000,
            toolTip="Vendor controller baud rate.",
            parameterType=PARAMETERTYPE.INT,
            attr="baudrate",
        )
        settings[f"{self.name}/{self.CONNECT_TIMEOUT}"] = parameterDict(
            value=5.0,
            minimum=1.0,
            maximum=30.0,
            toolTip="Timeout for connection, identity validation, and safe OFF.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="connect_timeout_s",
        )
        settings[f"{self.name}/{self.POLL_TIMEOUT}"] = parameterDict(
            value=3.0,
            minimum=0.5,
            maximum=30.0,
            toolTip="Timeout for one diagnostic and readback snapshot.",
            parameterType=PARAMETERTYPE.FLOAT,
            attr="poll_timeout_s",
        )
        settings[f"{self.name}/{self.RAMP_RATE}"] = parameterDict(
            value=500.0,
            minimum=0.0,
            maximum=_ESI_MAX_VOLTAGE,
            toolTip=(
                "Software ramp rate for changes to an active HV target. Initial "
                "activation uses the module's own voltage steps; disabling requests "
                "zero immediately. Set to 0 for an immediate change."
            ),
            parameterType=PARAMETERTYPE.FLOAT,
            attr="ramp_rate_v_s",
        )
        for label, attr, tooltip in (
            (
                self.HEAT_VOLTAGE_LIMIT,
                "heat_voltage_limit_v",
                "Optional HEAT-CTRL-2410 voltage limit. 0 keeps the hardware setting.",
            ),
            (
                self.HEAT_CURRENT_LIMIT,
                "heat_current_limit_a",
                "Optional HEAT-CTRL-2410 current limit. 0 keeps the hardware setting.",
            ),
            (
                self.HEAT_POWER_LIMIT,
                "heat_power_limit_w",
                "Optional HEAT-CTRL-2410 power limit. 0 keeps the hardware setting.",
            ),
        ):
            settings[f"{self.name}/{label}"] = parameterDict(
                value=50.0 if attr == "heat_power_limit_w" else 0.0,
                minimum=0.0,
                maximum=1000.0,
                toolTip=tooltip,
                parameterType=PARAMETERTYPE.FLOAT,
                attr=attr,
                event=self._heat_power_limit_changed if attr == "heat_power_limit_w" else None,
                advanced=True,
            )
        for label, attr, tooltip in (
            (self.STATE, "main_state", "Latest ESI controller state."),
            (self.INTERLOCK, "interlock_state", "Latest ESI interlock flags."),
            (self.MODULES, "detected_modules", "Detected module addresses and types."),
            (
                self.HEAT_STATUS,
                "heat_status",
                "Latest HEAT-CTRL-2410 temperature, power, and interlock state.",
            ),
        ):
            settings[f"{self.name}/{label}"] = parameterDict(
                value="Disconnected" if label == self.STATE else "n/a",
                toolTip=tooltip,
                parameterType=PARAMETERTYPE.LABEL,
                attr=attr,
                indicator=True,
                internal=True,
                restore=False,
            )
        settings[f"{self.name}/{self.OPERATING_CONFIG}"] = parameterDict(
            value=-1,
            minimum=-1,
            maximum=255,
            toolTip=(
                "ESI config slot exposed in the plugin toolbar. Use -1 to "
                "connect without loading a saved configuration."
            ),
            parameterType=PARAMETERTYPE.INT,
            attr="operating_config",
        )
        settings[f"{self.name}/{self.AVAILABLE_CONFIGS}"] = parameterDict(
            value="n/a",
            toolTip=(
                "ESI configuration slots reported by the controller after "
                "connect. Use these indices for the operating config setting."
            ),
            parameterType=PARAMETERTYPE.LABEL,
            attr="available_configs_text",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/{self.LOADED_CONFIG}"] = parameterDict(
            value="n/a",
            toolTip="ESI configuration currently loaded in volatile memory.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="loaded_config_text",
            indicator=True,
            internal=True,
            restore=False,
        )
        settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

    def ensureFixedChannels(self, *, persist: bool = False) -> None:
        """Preserve existing output controls and add missing current measurements."""
        channels = self.getChannels()
        items = _fixed_channel_items(self.name)
        outputs = [ch for ch in channels if not _is_current_channel(ch)]
        currents = [ch for ch in channels if _is_current_channel(ch)]
        if len(outputs) == 3 and {getattr(ch, "module", None) for ch in outputs} == {1, 2, _ESI_HEAT_MODULE}:
            existing = {getattr(ch, "module", None) for ch in currents}
            missing = [item for item in items[3:] if item["Module"] not in existing]
            if not missing:
                return
            add = getattr(self, "addChannel", None)
            if callable(add):
                # No rebuild: retain names, targets, enabled states, colors and history.
                for item in missing:
                    add(item)
                if persist:
                    self.exportConfiguration(useDefaultFile=True)
                return
        if channels and not all(
            str(getattr(channel, "name", "")).startswith(self.name)
            for channel in channels
        ):
            self.print(
                "Keeping the existing ESI channel configuration; expected module "
                "mapping is HEAT=0, HV1=1, HV2=2.",
                flag=PRINT.WARNING,
            )
            return
        update = getattr(self, "updateChannelConfig", None)
        custom_file = getattr(self, "customConfigFile", None)
        if not callable(update) or not callable(custom_file):
            return
        for item in items:
            if item["Function"] == _ESI_CURRENT_FUNCTION:
                continue  # A measurement never inherits a voltage target.
            matching = [
                channel
                for channel in channels
                if getattr(channel, "module", None) == item["Module"]
                and not _is_current_channel(channel)
            ]
            if not matching:
                continue
            source = next(
                (channel for channel in matching if getattr(channel, "enabled", False)),
                matching[0],
            )
            try:
                value = float(getattr(source, "value"))
            except (AttributeError, TypeError, ValueError):
                continue
            if np.isfinite(value):
                item["Value"] = min(max(abs(value), item["Min"]), item["Max"])
        update(items, custom_file(self.confINI))
        if persist:
            export = getattr(self, "exportConfiguration", None)
            if callable(export):
                export(useDefaultFile=True)

    def loadConfiguration(
        self,
        file: "Path | None" = None,
        useDefaultFile: bool = False,
        append: bool = False,
    ) -> None:
        """Create output controls and current monitors instead of generic channels."""
        if useDefaultFile:
            file = self.customConfigFile(self.confINI)

        if (
            useDefaultFile
            and file not in {None, Path()}
            and cast(Path, file).suffix.lower() == ".ini"
            and not cast(Path, file).exists()
            and not self.channels
        ):
            self.print(f"Creating fixed ESI channel config {file}")
            self.ensureFixedChannels(persist=True)
            return

        super().loadConfiguration(file=file, useDefaultFile=False, append=append)
        if useDefaultFile:
            self.ensureFixedChannels(persist=True)
        # A saved output selection is history, not a command: the plugin starts switched OFF.
        self._deselect_outputs("start")

    def closeCommunication(self) -> None:
        _session_clear(self)  # A disconnection or Explorer closing is never resumed.
        controller = getattr(self, "controller", None)
        if controller is not None:
            controller.shutdownCommunication()
        super().closeCommunication()


class ESIChannel(Channel):
    """An output control or a read-only HV current measurement."""

    MODULE = "Module"
    FUNCTION = "Function"
    channelParent: ESIDevice

    def getDefaultChannel(self) -> dict[str, dict]:
        self.module: int
        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "Target"
        channel[self.VALUE][Parameter.MIN] = 0.0
        channel[self.VALUE][Parameter.MAX] = _ESI_MAX_VOLTAGE
        channel[self.VALUE][_PARAMETER_UNIT_KEY] = "V"
        channel[self.ENABLED][Parameter.HEADER] = "Output On"
        channel[self.MODULE] = parameterDict(
            value=2,
            minimum=0,
            maximum=3,
            toolTip="Fixed CGC ESI module address (HEAT=0, HV1=1, HV2=2).",
            parameterType=PARAMETERTYPE.INT,
            attr="module",
            header="Module",
            indicator=True,
            advanced=False,
        )
        channel[self.FUNCTION] = parameterDict(
            value="HVPS-3kB",
            toolTip="Shared target for the coupled positive and negative outputs.",
            parameterType=PARAMETERTYPE.LABEL,
            attr="function",
            header="Function",
            indicator=True,
            advanced=False,
        )
        return channel

    def setDisplayedParameters(self) -> None:
        super().setDisplayedParameters()
        self.displayedParameters.append(self.MODULE)
        self.displayedParameters.append(self.FUNCTION)

    def module_address(self) -> int:
        return int(self.module)

    def is_heat_channel(self) -> bool:
        return self.module_address() == _ESI_HEAT_MODULE

    def is_current_channel(self) -> bool:
        return _is_current_channel(self)

    @property
    def unit(self) -> str:
        """Return the physical unit for this mixed-function ESI channel."""
        if self.is_current_channel():
            return "A"
        return "degC" if getattr(self, "module", 2) == _ESI_HEAT_MODULE else "V"

    def getDisplayUnit(self) -> str:
        return self.unit

    def initGUI(self, item: dict) -> None:
        # INI sections are case-insensitive; copying them directly would turn
        # Name/Module/Function into lowercase keys and break Explorer on reload.
        names = {name.casefold(): name for name in self.getDefaultChannel()}
        item = {names.get(key.casefold(), key): value for key, value in item.items()}
        self._current_measurement = item.get(self.FUNCTION) == _ESI_CURRENT_FUNCTION
        if self.is_current_channel():
            # Set before Explorer constructs the editors. Current is not a target.
            for name in (self.VALUE, self.MONITOR):
                parameter = self.getParameterByName(name)
                parameter.parameterType = PARAMETERTYPE.EXP
                parameter.indicator = True
                parameter.min = parameter.max = None
            item = {**item, self.VALUE: np.nan}
        super().initGUI(item)
        for parameter_name in (self.VALUE, self.MONITOR):
            self.getParameterByName(parameter_name).unit = self.unit
        spin = getattr(self.getParameterByName(self.VALUE), "spin", None)
        if spin is not None:
            spin.setKeyboardTracking(False)

    def updateMin(self) -> None:
        if not self.is_current_channel():
            super().updateMin()

    def updateMax(self) -> None:
        if not self.is_current_channel():
            super().updateMax()

    def valueChanged(self) -> None:
        if not self.is_current_channel():
            super().valueChanged()

    def applyValue(self, apply: bool = False) -> None:
        if not apply and not self.is_current_channel() and self.is_heat_channel():
            # Editing a temperature is not an activation command.
            if self.real and self.value != self.lastAppliedValue:
                self.lastAppliedValue = self.value
                controller = getattr(self.channelParent, 'controller', None)
                if self.enabled and controller is not None:
                    controller.applyHeatTemperatureFromThread(float(self.value))
            return
        if not self.is_current_channel():
            if self.enabled:
                finish = getattr(self.channelParent, "_finish_setpoint_edits", None)
                if callable(finish):
                    finish(self)
            super().applyValue(apply=apply)

    def enabledChanged(self) -> None:
        if self.is_current_channel():
            return
        super().enabledChanged()
        if not getattr(self.channelParent, "loading", False):
            self.applyValue(apply=True)


class _SupersededHeatEdit(Exception):
    """A field edit has been replaced by a newer operator request."""


class ESIController(DeviceController):
    """ESIBD bridge for the timeout-safe ESI runtime."""

    controllerParent: ESIDevice

    # Set by a crash-resume record: connect without any command and adopt the hardware state.
    resume_session = False

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        # Serialize output sequences, not just individual DLL calls. OFF cancels
        # the current generation before waiting for an in-flight command.
        self._output_lock = RLock()
        self._output_cancel = Event()
        self._heat_stability_lock = RLock()
        self._heat_stability_generation = 0
        self._heat_stability = _get_temperature_stability_class()()
        self.values: dict[int, float] | None = None
        self.currents: dict[int, float] = {}
        self.targets: dict[int, float] = {}
        self.module_active: dict[int, bool | None] = {}
        self.module_control_active: dict[int, bool | None] = {}
        self.module_led_rgb: dict[int, tuple[bool, bool, bool] | None] = {}
        self.pwm_voltage_set: dict[int, float] = {}
        self.pwm_voltage_measured: dict[int, float] = {}
        self.measurement_polarity: dict[int, str | None] = {}
        self.global_enabled: bool | None = None
        self.initialized = False
        self.main_state = "Disconnected"
        self.discharge_readings: dict[int, dict[str, float]] = {}
        self.interlock_state = "n/a"
        self.detected_modules = "n/a"
        self.heat_status = "n/a"
        self.heat_activation: dict[str, bool | None] = {}
        self.identity: dict[str, Any] = {}
        self.heat_readback_valid = False
        self.heat_max_temperature_c = np.nan
        self.heat_max_power_w = np.nan
        self.heat_power_limit_w = np.nan
        self.heat_limits_observed_at = float('-inf')
        self.heat_power_error = ""
        self.heat_temperature_error = ""
        self._heat_power_command = None
        self._heat_power_request = None
        self._heat_temperature_request = None
        self.available_configs: list[dict[str, Any]] = []
        self.available_configs_text = "n/a"
        self.loaded_config_text = "n/a"

    def initializeCommunication(self) -> None:
        if self.initializing:
            return
        acquisition_thread = getattr(self, 'acquisitionThread', None)
        if acquisition_thread is not None and acquisition_thread.is_alive():
            self.closeCommunication()
        # Arm the request before scheduling its worker, never inside it: a
        # close issued before that worker starts must still cancel the request.
        self._initial_open_close_requested = False
        super().initializeCommunication()

    def runInitialization(self) -> None:
        if getattr(self, '_initial_open_close_requested', False):
            if not self.initialized:
                self._restore_off_ui_state()
            self._sync_status()
            self.initializing = False
            return
        if not self._dispose_device():
            self.initializing = False
            return  # Never replace an unconfirmed backend/port reservation.
        self.initialized = False
        try:
            driver = _get_esi_driver_class()
            device = driver(
                device_id=f"esi_com{int(self.controllerParent.com)}",
                com=int(self.controllerParent.com),
                baudrate=int(self.controllerParent.baudrate),
                process_backend=False,
            )
            if getattr(self, '_initial_open_close_requested', False):
                # This freshly constructed backend has never been opened.
                with contextlib.suppress(Exception):
                    device.close()
                self._restore_off_ui_state()
                self._sync_status()
                return
            self.device = device
            backend_reason = str(
                getattr(self.device, "_process_backend_disabled_reason", "")
            ).strip()
            if backend_reason:
                self.print(backend_reason, flag=PRINT.WARNING)
            self.device.connect(timeout_s=float(self.controllerParent.connect_timeout_s))
            if getattr(self, '_initial_open_close_requested', False):
                # Open succeeded after a close request. Verify shutdown rather
                # than activating or publishing this cancelled connection.
                self.shutdownCommunication()
                return
            if self.resume_session:
                # Crash resume: no enable, limit, safe-off or step command; identity and state reads only.
                try:
                    self.identity = self.device.collect_identity(
                        timeout_s=float(self.controllerParent.poll_timeout_s))
                    self._resume_snapshot = self.device.collect_diagnostics(
                        timeout_s=float(self.controllerParent.poll_timeout_s))
                except Exception as exc:  # noqa: BLE001 - never shut down a resumed source
                    self._resume_snapshot = None
                    self.print(f"Resume after an Explorer crash: ESI state unreadable ({exc}); communication "
                               "stays open and nothing was commanded.", flag=PRINT.ERROR)
                self.signalComm.initCompleteSignal.emit()
                return
            self.device.set_global_active(
                True,
                timeout_s=float(self.controllerParent.connect_timeout_s),
            )
            heat_limits = {
                "voltage_v": float(self.controllerParent.heat_voltage_limit_v),
                "current_a": float(self.controllerParent.heat_current_limit_a),
                # Power is applied/read back only by an explicit heater command or edit.
            }
            requested_limits = {
                name: value for name, value in heat_limits.items() if value > 0
            }
            if requested_limits:
                self.device.configure_heat_limits(
                    **requested_limits,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
            self.identity = self.device.collect_identity(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self.device.force_safe_off(
                timeout_s=float(self.controllerParent.connect_timeout_s)
            )
            self.device.configure_hv_max_voltage_steps(
                _ESI_HV_MAX_VOLTAGE_STEP,
                timeout_s=float(self.controllerParent.connect_timeout_s),
            )
            snapshot = self.device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self._apply_snapshot(snapshot)
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:
            if self.device is None:
                self._restore_off_ui_state()
            elif self._initial_open_incomplete():
                self._dispose_device()  # No HV/heater command after a failed Open.
                self._restore_off_ui_state()
            else:
                self.shutdownCommunication()
            self.print(
                f"ESI initialization failed on COM{int(self.controllerParent.com)}: {exc}\n"
                "Confirm the configured COM port, power the controller, and close "
                "the hardware probe and vendor control application before retrying.",
                flag=PRINT.ERROR,
            )
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        if getattr(self, '_initial_open_close_requested', False):
            return  # Ignore a success signal queued before an explicit close.
        self.controllerParent.ensureFixedChannels(persist=True)
        self.initializeValues(reset=True)
        self.initialized = self.device is not None
        if self.initialized:
            self._refresh_available_configs()
        # Explorer's DeviceController.initComplete would call updateValues(apply=True) when the device
        # is ON, i.e. re-apply every saved target and output selection: done here without it.
        if self.initialized:
            if self.resume_session:
                self._adopt_hardware_state()
            self.startAcquisition()
            if self.controllerParent.isOn() and not self.resume_session:
                self.toggleOnFromThread(parallel=True)
            self.resume_session = False
        if self.initialized:
            self.print(
                "ESI initialized with HV and heater outputs forced OFF. "
                "Use the explicit output controls to energize HV1, HV2, or HEAT."
            )

    def _adopt_hardware_state(self) -> None:
        """Crash resume: show the ESI as it runs (targets and active outputs); nothing is sent."""
        snapshot = getattr(self, "_resume_snapshot", None)
        self._resume_snapshot = None
        if snapshot is None:
            return
        self._apply_snapshot(snapshot)
        parent = self.controllerParent
        adopted = []
        parent.loading = True  # No value or enable event: the hardware already holds these values.
        try:
            for channel in parent.getChannels():
                if _is_current_channel(channel):
                    continue
                if channel.is_heat_channel():
                    active = (self.heat_activation or {}).get("active") is True
                    target, unit = self.heat_target_temperature_c, "°C"
                else:
                    address = channel.module_address()
                    active = self.module_active.get(address) is True and self.global_enabled is True
                    target, unit = self.targets.get(address, np.nan), "V"
                if np.isfinite(target):
                    _set_parameter_quietly(channel, "VALUE", "value", float(target))
                _set_parameter_quietly(channel, "ENABLED", "enabled", bool(active))
                channel.lastAppliedValue = channel.value
                adopted.append(f"{channel.name} {'ON' if active else 'OFF'} {float(channel.value):g} {unit}")
        finally:
            parent.loading = False
        self.print("Resumed after an Explorer crash: ESI adopted as it runs (" + ", ".join(adopted)
                   + "); nothing was switched, forced OFF or re-applied.")

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
                timeout_s=float(self.controllerParent.connect_timeout_s)
            )
        except Exception as exc:
            self.available_configs = []
            self.available_configs_text = "Unavailable"
            self.print(
                f"Could not read ESI config list: {exc}",
                flag=PRINT.WARNING,
            )
            return
        self.available_configs = list(configs or [])
        if self.available_configs:
            parts = [
                f"{_coerce_int(c.get('index'), -1)}: "
                f"{str(c.get('name', '') or '').strip() or '<unnamed>'}"
                for c in self.available_configs
            ]
            self.available_configs_text = ", ".join(parts)
        else:
            self.available_configs_text = "No saved configs"

    def _config_entry_by_index(self, config_index: int) -> dict[str, Any] | None:
        for entry in self.available_configs:
            if _coerce_int(entry.get("index"), -1) == config_index:
                return entry
        return None

    def _operating_config_ready(self) -> tuple[bool, str, int]:
        config_index = _coerce_int(
            getattr(self.controllerParent, "operating_config", -1), -1
        )
        if config_index < 0:
            return False, "select an ESI config first", config_index
        entry = self._config_entry_by_index(config_index)
        if entry is not None:
            if not _coerce_bool(entry.get("valid"), True):
                return (
                    False,
                    f"config {config_index} is marked invalid on the controller",
                    config_index,
                )
        return True, "", config_index

    def loadOperatingConfigNowFromThread(self, parallel: bool = True) -> None:
        if parallel:
            Thread(
                target=self.loadOperatingConfigNow,
                name=f"{self.controllerParent.name} loadConfigThread",
                daemon=True,
            ).start()
            return
        self.loadOperatingConfigNow()

    def loadOperatingConfigNow(self) -> None:
        cancel = self._output_cancel
        with self._output_lock:
            if not cancel.is_set():
                self._load_operating_config_unlocked()

    def _load_operating_config_unlocked(self) -> None:
        device = self.device
        if device is None or not getattr(self, "initialized", False):
            self.print(
                f"Cannot load {self.controllerParent.name} config: "
                "communication not initialized.",
                flag=PRINT.WARNING,
            )
            return
        is_on = getattr(self.controllerParent, "isOn", None)
        if not callable(is_on) or not bool(is_on()):
            self.print(
                f"Cannot load {self.controllerParent.name} config while the ESI is OFF.",
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
        timeout_s = float(self.controllerParent.connect_timeout_s)
        self._reset_heat_stability()
        try:
            device.load_config(config_index, timeout_s=timeout_s)
            self.loaded_config_text = f"Config {config_index}"
            self.print(f"Loaded ESI config {config_index}.")
        except Exception as exc:
            self.errorCount += 1
            self.print(
                f"Failed to load ESI config {config_index}: {exc}",
                flag=PRINT.ERROR,
            )

    def _reset_heat_stability(self) -> None:
        with self._heat_stability_lock:
            self._heat_stability_generation += 1
            self._heat_stability.reset()
        refresh = getattr(self.controllerParent, "_update_heat_stability_display", None)
        if callable(refresh):
            _invoke_gui_callback(refresh)

    def heat_stability_status(self) -> dict:
        return self._heat_stability.status(time.monotonic())

    def initializeValues(self, reset: bool = False) -> None:
        if self.values is None or reset:
            self._reset_heat_stability()
            self.values = {address: np.nan for address in _ESI_MODULES}
            self.currents = {address: np.nan for address in _ESI_MODULES}
            self.targets = {address: np.nan for address in _ESI_HV_MODULES}
            self.module_active = {address: None for address in _ESI_HV_MODULES}
            self.module_control_active = {
                address: None for address in _ESI_HV_MODULES
            }
            self.module_led_rgb = {address: None for address in _ESI_HV_MODULES}
            self.pwm_voltage_set = {address: np.nan for address in _ESI_HV_MODULES}
            self.pwm_voltage_measured = {
                address: np.nan for address in _ESI_HV_MODULES
            }
            self.measurement_polarity = {
                address: None for address in _ESI_HV_MODULES
            }
            self.global_enabled = None
            self.heat_activation = {}
            self.heat_readback_valid = False
            self.heat_target_temperature_c = np.nan
            self.heat_power_w = np.nan
            self.heat_power_limit_w = np.nan
            self.heat_max_power_w = np.nan
            self.heat_max_temperature_c = np.nan
            self.heat_limits_observed_at = float('-inf')
            self._heat_power_command = None
            refresh = getattr(self.controllerParent, '_update_operator_panel', None)
            if callable(refresh):
                _invoke_gui_callback(refresh)

    def readNumbers(self) -> None:
        if self.main_state == _ESI_STOPPING:
            return  # The shutdown worker owns the ADC mux and publishes readbacks.
        if self.main_state == "Shutdown unconfirmed":
            self.initializeValues(reset=True)
            return
        if self.device is None or not self.initialized:
            self.initializeValues(reset=True)
            return
        if not self.controllerParent.isOn():
            self.initializeValues(reset=True)
            return
        device = self.device
        generation = self._heat_stability_generation
        try:
            snapshot = device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            observed_at = time.monotonic()
            if device is self.device:
                self._apply_snapshot(snapshot, observed_at=observed_at, stability_generation=generation)
        except Exception as exc:
            if device is not self.device or self.main_state in (_ESI_STOPPING, "Shutdown unconfirmed"):
                return
            self.errorCount += 1
            self.main_state = _ESI_COMMUNICATION_LOST
            self._sync_status()
            self.initializeValues(reset=True)
            self.print(
                "ESI readback failed; the transport state is unknown and HV may "
                f"remain energized: {exc}",
                flag=PRINT.ERROR,
            )

    def fakeNumbers(self) -> None:
        self.initializeValues(reset=True)

    def applyHeatTemperatureFromThread(self, value: float) -> None:
        cancel = self._output_cancel
        request = object()
        self._heat_temperature_request = request
        Thread(target=self._apply_heat_temperature, args=(value, cancel, request), daemon=True).start()

    def _apply_heat_temperature(self, value: float, cancel: Event, request=None) -> None:
        with self._output_lock:
            if request is not None and request is not self._heat_temperature_request:
                return
            if (cancel.is_set() or not self.initialized or self.device is None
                    or not self.controllerParent.isOn() or self.main_state != "STATE_ON"
                    or getattr(self, 'transitioning', False)):
                return
            self._reset_heat_stability()
            try:
                self._require_valid_heat_readback()
                self.device.set_heater_temperature(value,
                    timeout_s=float(self.controllerParent.poll_timeout_s), cancel_event=cancel)
                if cancel.is_set() or (request is not None and request is not self._heat_temperature_request):
                    return
                self.heat_temperature_error = ""
            except Exception as exc:
                error = f"Temperature unconfirmed: {exc}"
                if request is None or request is self._heat_temperature_request:
                    self.heat_temperature_error = error
                self.errorCount += 1
                for ch in self.controllerParent.getChannels():
                    if ch.is_heat_channel() and not _is_current_channel(ch):
                        error += self._disable_failed_channel(ch)
                        break
                if request is None or request is self._heat_temperature_request:
                    self.heat_temperature_error = error
                self.print(error, flag=PRINT.ERROR)
            finally:
                self._reset_heat_stability()
                self._sync_status()

    def applyHeatPowerFromThread(self, value: float) -> None:
        cancel = self._output_cancel  # Capture before worker dispatch, never revive queued work.
        request = object()
        self._heat_power_request = request
        Thread(target=self._apply_heat_power, args=(value, cancel, request), daemon=True).start()

    def _apply_heat_power(self, value: float, cancel: Event, request=None) -> None:
        with self._output_lock:
            if request is not None and request is not self._heat_power_request:
                return
            if (cancel.is_set() or not self.initialized or self.device is None
                    or not self.controllerParent.isOn() or self.main_state != "STATE_ON"
                    or getattr(self, 'transitioning', False)):
                return
            self._reset_heat_stability()
            self.heat_power_error = "Power limit pending verification."
            try:
                if value > 0:
                    self._require_valid_heat_readback()
                self._configure_heat_power_unlocked(value, cancel, request=request)
            except _SupersededHeatEdit:
                pass  # Supersession is not a hardware fault; do not replay or enable.
            except Exception as exc:
                error = f"Power limit unconfirmed: {exc}"
                if request is None or request is self._heat_power_request:
                    self.heat_power_error = error
                self.errorCount += 1
                for ch in self.controllerParent.getChannels():
                    if ch.is_heat_channel() and not _is_current_channel(ch):
                        error += self._disable_failed_channel(ch)
                        break
                if request is None or request is self._heat_power_request:
                    self.heat_power_error = error
                self.print(error, flag=PRINT.ERROR)
            finally:
                self._reset_heat_stability()
                self._sync_status()

    def _configure_heat_power_unlocked(self, value: float, cancel: Event, *, target=None, request=None) -> None:
        def permission():
            if cancel.is_set():
                raise InterruptedError("ESI heater operation cancelled")
            if request is not None and request is not self._heat_power_request:
                raise _SupersededHeatEdit()
        permission()
        timeout = float(self.controllerParent.poll_timeout_s)
        before = self.device.get_heat_configuration(timeout_s=timeout)
        permission()
        if target is not None:
            max_t = float(before['hardware_limits']['max_temperature_c'])
            if not (np.isfinite(target) and np.isfinite(max_t) and max_t > 0 and 0 <= target <= max_t):
                raise ValueError(f"Temperature must be within the device maximum {max_t:g} °C.")
        maximum = float(before['hardware_limits']['max_power_w'])
        current = float(before['power_limit_w'])
        if not (np.isfinite(value) and np.isfinite(maximum) and maximum > 0 and 0 <= value <= maximum):
            raise ValueError(f"Power limit must be within the device maximum {maximum:g} W (0 keeps the device limit).")
        if value == 0:
            actual = current  # Preserve existing saved legacy configurations, never reinterpret zero as OFF.
        elif self._heat_power_command == (value, current):
            actual = current
        else:
            permission()
            self._require_valid_heat_readback()
            applied = self.device.configure_heat_limits(power_w=value, timeout_s=timeout, cancel_event=cancel)
            permission()
            actual = float(applied['power_w'])
            if not (np.isfinite(actual) and 0 < actual <= min(value, maximum)):
                raise ValueError(f"Applied power {actual:g} W does not respect the requested ceiling {value:g} W.")
            after = self.device.get_heat_configuration(timeout_s=timeout)
            permission()
            actual_max = float(after['hardware_limits']['max_power_w'])
            if (not np.isfinite(actual_max) or not 0 < actual <= actual_max
                    or float(after['power_limit_w']) != actual
                    or any(after[key] != before[key] for key in ('voltage_limit_v', 'current_limit_a'))):
                raise ValueError("Independent power-limit readback did not confirm the applied limit with unchanged V/I.")
            self._heat_power_command = (value, actual)
        if not (np.isfinite(actual) and 0 < actual <= maximum):
            raise ValueError("The device power limit is unavailable or zero; choose a positive limit.")
        with self._heat_stability_lock:
            # Fence snapshots acquired before or during this verified command.
            self._reset_heat_stability()
            self.heat_power_limit_w = actual
            self.heat_power_error = ""

    def applyValue(self, channel: ESIChannel) -> None:
        if _is_current_channel(channel):
            return
        cancel = self._output_cancel
        with self._output_lock:
            if not cancel.is_set():
                self._apply_value_unlocked(channel, cancel)

    def _apply_value_unlocked(self, channel: ESIChannel, cancel: Event) -> None:
        if _is_current_channel(channel):
            return
        if self.device is None or not self.initialized or not self.controllerParent.isOn():
            return
        if channel.is_heat_channel():
            # Do not keep displaying a previous ON after a failed/new command.
            # Acquisition, not the requested target, restores measured status.
            self.heat_activation = {}
            self._reset_heat_stability()
        if not channel.enabled:
            try:
                self.device.set_output_active(
                    channel.module_address(),
                    False,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
                if not channel.is_heat_channel():
                    self.targets[channel.module_address()] = 0.0
                    self.module_active[channel.module_address()] = False
            except Exception as exc:
                self.errorCount += 1
                self.print(
                    f"ESI failed to disable {channel.name}: {exc}",
                    flag=PRINT.ERROR,
                )
            finally:
                if channel.is_heat_channel():
                    self._reset_heat_stability()
            return
        if channel.is_heat_channel():
            target = float(channel.value)
            try:
                self._require_valid_heat_readback()
                requested_power = float(getattr(self.controllerParent, 'heat_power_limit_w', 0.))
                if requested_power != 0:
                    try:
                        self._configure_heat_power_unlocked(requested_power, cancel, target=target)
                    except Exception as exc:
                        self.heat_power_error = f"Power limit unconfirmed: {exc}"
                        raise
                if cancel.is_set():
                    return
                self.device.set_heater_temperature(
                    target,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                    cancel_event=cancel,
                )
                if cancel.is_set():
                    return
                enabled = self.device.set_output_active(
                    channel.module_address(),
                    True,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                    cancel_event=cancel,
                )
                self.global_enabled = bool(enabled)
                self.heat_temperature_error = ""
            except Exception as exc:
                self.heat_temperature_error = f"Temperature unconfirmed: {exc}"
                self.errorCount += 1
                rollback = self._disable_failed_channel(channel)
                self.print(
                    f"ESI rejected temperature {target:g} for HEAT: {exc}.{rollback}",
                    flag=PRINT.ERROR,
                )
            finally:
                self._reset_heat_stability()
            return
        # The C API exposes one unsigned target for the module's +/- output pair.
        target = abs(float(channel.value))
        address = channel.module_address()
        try:
            previous = self.targets.get(address, np.nan)
            if (
                self.module_active.get(address) is True
                and self.global_enabled is True
                and np.isfinite(previous)
            ):
                if not self._ramp_target(address, float(previous), target, cancel=cancel):
                    return
                applied = target
            else:
                applied = self.device.set_hv_module_target(
                    address,
                    target,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
            if cancel.is_set():
                return
            enabled = self.device.set_output_active(
                address,
                True,
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            self.targets[address] = float(applied)
            self.module_active[address] = bool(enabled)
            self.global_enabled = bool(enabled)
        except Exception as exc:
            self.errorCount += 1
            rollback = self._disable_failed_channel(channel)
            self.print(
                f"ESI rejected target {target:g} V for module "
                f"{address}: {exc}.{rollback}",
                flag=PRINT.ERROR,
            )

    def updateValues(self) -> None:
        for channel in self.controllerParent.getChannels():
            current = _is_current_channel(channel)
            readings = self.currents if current else self.values
            value = (readings.get(channel.module_address(), np.nan)
                     if readings is not None and channel.enabled and channel.real else np.nan)
            channel.monitor = value
            if current:
                # Explorer may record .value for disabled/non-real IN channels.
                # It must never fall back to a stale current or a dummy target.
                channel.value = value

    def toggleOn(self) -> None:
        target_on = bool(self.controllerParent.isOn())
        if target_on and self.main_state in (_ESI_STOPPING, "Shutdown unconfirmed"):
            self._restore_on_ui_state()
            self.print("ESI cannot restart until shutdown is confirmed; retry OFF.", flag=PRINT.WARNING)
            return
        if not target_on:
            self._output_cancel.set()
            self._reset_heat_stability()
        with self._output_lock:
            # A newer OFF request takes precedence over a queued ON request.
            if target_on and (not self.controllerParent.isOn()
                              or self.main_state in (_ESI_STOPPING, "Shutdown unconfirmed")):
                return
            if target_on:
                self._output_cancel = Event()
            self._toggle_on_unlocked(target_on)

    def _toggle_on_unlocked(self, target_on: bool) -> None:
        cancel = self._output_cancel
        super().toggleOn()
        if self.device is None:
            return
        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False
        timeout = float(self.controllerParent.connect_timeout_s)
        try:
            if target_on:
                # The shared gate must not restart a stored heater selection.
                self.heat_activation = {}
                self._reset_heat_stability()
                self.device.set_output_active(_ESI_HEAT_MODULE, False, timeout_s=timeout)
                # Clear every stored HV target before opening the shared gate.
                for address in _ESI_HV_MODULES:
                    self.device.set_output_active(address, False, timeout_s=timeout)
                    self.targets[address] = 0.0
                    self.module_active[address] = False
                if cancel.is_set():
                    return
                self.global_enabled = bool(
                    self.device.set_global_active(True, timeout_s=timeout)
                )
                # No output is energized here: HV1, HV2 and HEAT stay OFF until the operator
                # selects them (the 2026-10-07 incident restarted a saved HV1 at 1000 V).
                if not cancel.is_set():
                    self.startAcquisition()
            else:
                self._shutdown_communication_unlocked()
        except Exception as exc:
            self.errorCount += 1
            rollback_confirmed, rollback = self._force_safe_off_after_failure()
            if rollback_confirmed:
                self._restore_off_ui_state()
            else:
                self._restore_on_ui_state()
            self.print(
                "ESI ON/OFF transition failed: "
                f"{exc}.{rollback}",
                flag=PRINT.ERROR,
            )

    def _invalidate_heat_confirmation(self) -> None:
        with self._heat_stability_lock:
            self._reset_heat_stability()
            self.heat_activation = {}
            self.heat_readback_valid = False
            self.heat_limits_observed_at = float('-inf')
            self.heat_power_limit_w = np.nan
            self.heat_target_temperature_c = np.nan
            self.heat_power_w = np.nan
            self._heat_power_command = None

    def _disable_failed_channel(self, channel: ESIChannel) -> str:
        """Best-effort safe fallback after a channel command fails."""
        heat = channel.is_heat_channel()
        if heat:
            self._invalidate_heat_confirmation()
        device = self.device
        if device is None:
            return " Device is unavailable; output state is unconfirmed"
        address = channel.module_address()
        timeout = float(self.controllerParent.poll_timeout_s)
        failures = []
        if not channel.is_heat_channel():
            try:
                device.set_hv_module_target(address, 0.0, timeout_s=timeout)
            except Exception as exc:
                failures.append(f"zero target failed: {exc}")
        try:
            device.set_output_active(address, False, timeout_s=timeout)
        except Exception as exc:
            failures.append(f"deactivation failed: {exc}")
        finally:
            if heat:
                self._invalidate_heat_confirmation()
        if failures:
            return (
                " Safe disable also failed; output state is unconfirmed and the "
                f"hardware interlock must be used: {'; '.join(failures)}"
            )
        return " The affected output was forced OFF"

    def _require_valid_heat_readback(self) -> None:
        if not self.heat_readback_valid:
            raise RuntimeError(
                "ESI heater readback is invalid or outside the hardware range; "
                "connect and verify the temperature sensor before enabling HEAT"
            )

    def _force_safe_off_after_failure(self) -> tuple[bool, str]:
        """Best-effort global rollback after a failed ON/OFF transition."""
        device = self.device
        if device is None:
            return False, " Device is unavailable; all output states are unconfirmed"
        # A logical disable alone is not an OFF confirmation after failed ON.
        self._output_cancel.set()
        if not self._shutdown_communication_unlocked():
            return False, " Shutdown remains unconfirmed; use the hardware interlock/front panel"
        return True, " Outputs disabled, HV discharge verified, and port closed"

    def _restore_off_ui_state(self) -> None:
        """Keep the UI from claiming ON after a failed transition."""
        self.resume_session = False
        def _update_gui() -> None:
            sync_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_state):
                sync_state(False)
                return
            action = getattr(self.controllerParent, "onAction", None)
            if action is not None:
                action.state = False

        _invoke_gui_callback(_update_gui)

    def _restore_on_ui_state(self) -> None:
        """Keep OFF reachable while the physical output state is uncertain."""
        def _update_gui() -> None:
            sync_state = getattr(self.controllerParent, "_set_on_ui_state", None)
            if callable(sync_state):
                sync_state(True)
                return
            action = getattr(self.controllerParent, "onAction", None)
            if action is not None:
                action.state = True

        _invoke_gui_callback(_update_gui)

    def _ramp_target(
        self, address: int, start: float, target: float, *, cancel: Event | None = None
    ) -> bool:
        """Apply bounded steps; yield to OFF within one step plus DLL latency."""
        cancel = self._output_cancel if cancel is None else cancel
        device = self.device
        if device is None or cancel.is_set():
            return False
        rate = max(0.0, float(getattr(self.controllerParent, "ramp_rate_v_s", 0.0)))
        delta = float(target) - float(start)
        if rate == 0.0 or delta == 0.0:
            device.set_hv_module_target(
                address,
                float(target),
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            return not cancel.is_set()

        step_interval_s = 0.1
        steps = max(1, int(np.ceil(abs(delta) / (rate * step_interval_s))))
        for step in range(1, steps + 1):
            if cancel.is_set():
                return False
            value = float(start) + delta * step / steps
            device.set_hv_module_target(
                address,
                value,
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            if step < steps and cancel.wait(step_interval_s):
                return False
        return not cancel.is_set()

    def shutdownCommunication(self) -> bool:
        self._initial_open_close_requested = True
        self._output_cancel.set()
        self._reset_heat_stability()
        with self._output_lock:
            return self._shutdown_communication_unlocked()

    def _on_discharge_progress(self, report: dict) -> None:
        # Runs under the runtime's DLL lock, never make another DLL call here.
        if self.main_state != _ESI_STOPPING:
            return  # A late return after timeout cannot turn uncertainty into OFF.
        self.discharge_readings = report["modules"]
        self.global_enabled = False
        for address, data in self.discharge_readings.items():
            self.targets[address] = 0.0
            self.module_active[address] = False
            self.values[address] = data.get("negative_v", np.nan)
            self.currents[address] = data.get("measured_a", np.nan)
        self._sync_status()

    def _shutdown_communication_unlocked(self) -> bool:
        device = self.device
        if device is None:
            return self.main_state == "Disconnected"
        if self._initial_open_incomplete():
            # An unfinished initial connection is not an output shutdown.
            # The runtime retires a failed Open only after its worker returns.
            self.initializeValues(reset=True)
            self.discharge_readings = {}
            return self._dispose_device()
        confirmed = False
        self.acquiring = False
        self.main_state = _ESI_STOPPING
        self.initializeValues(reset=True)
        self.discharge_readings = {}
        self._restore_on_ui_state()
        self._sync_status()
        try:
            result = device.disconnect(
                timeout_s=float(self.controllerParent.connect_timeout_s),
                on_discharge=self._on_discharge_progress,
            )
            if result is not True:
                raise RuntimeError("ESI driver did not confirm safe shutdown")
            confirmed = True
        except Exception as exc:
            self.print(
                "ESI shutdown could not be confirmed. HV may remain energized; "
                f"use the hardware interlock/front panel: {exc}",
                flag=PRINT.ERROR,
            )
        finally:
            self.acquiring = False
            self.initializeValues(reset=True)
            if confirmed:
                self._dispose_device(shutdown_confirmed=True)
                self.initialized = False
                self.discharge_readings = {}
                self._restore_off_ui_state()
            else:
                # Keep the backend/port reservation and Explorer's closing warning.
                self.initialized = True
                self._restore_on_ui_state()
            self.main_state = "Disconnected" if confirmed else "Shutdown unconfirmed"
            self._sync_status()
        return confirmed

    def closeCommunication(self) -> None:
        with contextlib.suppress(AttributeError):
            super().closeCommunication()
        self.shutdownCommunication()

    def _apply_snapshot(self, snapshot: dict[str, Any], *, observed_at=None, stability_generation=None) -> None:
        if self.main_state in (_ESI_STOPPING, "Shutdown unconfirmed"):
            return  # A late status poll must not erase the shutdown check or uncertainty.
        self.main_state = str(snapshot["main_state"]["name"])
        self.values = {}
        self.currents = {}
        self.targets = {}
        self.module_active = {}
        self.module_control_active = {}
        self.module_led_rgb = {}
        self.pwm_voltage_set = {}
        self.pwm_voltage_measured = {}
        self.measurement_polarity = {}
        self.global_enabled = bool(snapshot.get("enabled", False))
        for address, module in snapshot["modules"].items():
            address = int(address)
            self.targets[address] = float(module.get("target_v", np.nan))
            self.module_active[address] = module.get("module_active")
            self.module_control_active[address] = module.get("control_active")
            led = module.get("led", {})
            self.module_led_rgb[address] = (
                bool(led.get("red")),
                bool(led.get("green")),
                bool(led.get("blue")),
            ) if isinstance(led, dict) else None
            pwm = module.get("pwm", {})
            self.pwm_voltage_set[address] = float(
                pwm.get("voltage_set_v", np.nan)
            )
            self.pwm_voltage_measured[address] = float(
                pwm.get("voltage_measured_v", np.nan)
            )
            measurement = module.get("measurement", {})
            polarity = measurement.get("voltage_polarity")
            self.measurement_polarity[address] = (
                polarity if polarity in ("positive", "negative") else None
            )
            self.values[address] = (
                float(module["measured_v"]) if module["voltage_valid"] else np.nan
            )
            self.currents[address] = (
                float(module["measured_a"]) if module["current_valid"] else np.nan
            )
        heat = snapshot["heat"]
        heat_temperature = float(heat["monitor_temperature_c"])
        with self._heat_stability_lock:
            heat_snapshot_current = stability_generation is None or stability_generation == self._heat_stability_generation
            if heat_snapshot_current:
                self.heat_activation = {key: heat.get(key) for key in (
                    "active", "module_active", "module_gate_active",
                    "device_gate_active", "control_active",
                )}
                self.heat_target_temperature_c = float(heat.get("target_temperature_c", np.nan))
                self.heat_power_w = float(heat.get("heater_power_w", np.nan))
                self.heat_max_power_w = float(heat["hardware_limits"].get("max_power_w", np.nan))
                self.heat_power_limit_w = float(heat.get("power_limit_w", np.nan))
                self.heat_limits_observed_at = time.monotonic() if observed_at is None else observed_at
                self.heat_max_temperature_c = float(heat["hardware_limits"]["max_temperature_c"])
                self.heat_readback_valid = bool(
                    heat["valid"] and np.isfinite(self.heat_max_temperature_c)
                    and self.heat_max_temperature_c > 0.0 and np.isfinite(heat_temperature)
                    and 0.0 <= heat_temperature <= self.heat_max_temperature_c)
                self.values[_ESI_HEAT_MODULE] = heat_temperature if self.heat_readback_valid else np.nan
                self.currents[_ESI_HEAT_MODULE] = float(heat["monitor_current_a"]) if heat["valid"] else np.nan
                # The expected interval plus the existing read budget bounds missing observations.
                max_gap_s = (
                    max(0., float(getattr(self.controllerParent, "interval", 1000.))) / 1000.
                    + max(0.1, float(getattr(self.controllerParent, "poll_timeout_s", 3.)))
                )
                if max_gap_s != self._heat_stability.max_gap_s:
                    self._heat_stability.reset()
                    self._heat_stability.max_gap_s = max_gap_s
                try:
                    no_fault = int(snapshot["device_state"]["hex"], 0) == 0
                except (KeyError, TypeError, ValueError):
                    no_fault = False
                active = snapshot.get("enabled") is True and all(self.heat_activation.get(key) is True for key in (
                    "active", "module_active", "module_gate_active", "device_gate_active", "control_active"))
                known_off = snapshot.get("enabled") is False or self.heat_activation.get("module_active") is False
                self._heat_stability.update(
                    time.monotonic() if observed_at is None else observed_at,
                    heat_temperature, self.heat_target_temperature_c,
                    active=not known_off,
                    valid=self.heat_readback_valid and active and no_fault and self.main_state == "STATE_ON",
                )
            else:
                # No repeated last measurement with a new acquisition timestamp.
                self.values[_ESI_HEAT_MODULE] = np.nan
                self.currents[_ESI_HEAT_MODULE] = np.nan
        flags = snapshot["interlock_state"]["flags"]
        self.interlock_state = ", ".join(flags) if flags else "OK"
        module_identity = self.identity.get("modules", {})
        labels = []
        for address in _ESI_MODULES:
            info = module_identity.get(address, module_identity.get(str(address), {}))
            product_id = info.get("product_id") if isinstance(info, dict) else None
            fallback = "HEAT-CTRL-2410" if address == _ESI_HEAT_MODULE else "HVPS-3kB"
            label = product_id if isinstance(product_id, str) and product_id else fallback
            labels.append(f"{address}: {label}")
        self.detected_modules = ", ".join(labels)
        if heat_snapshot_current and self.heat_readback_valid:
            self.heat_status = (
                f"T={heat_temperature:.1f} degC, "
                f"Pset={float(heat['heater_power_w']):.2f} W, "
                f"Ilock=0x{int(heat['interlock_state']):02X}"
            )
        elif heat_snapshot_current:
            self.heat_status = (
                f"INVALID T={heat_temperature:.1f} degC; check temperature sensor"
            )
        self._sync_status()

    def _sync_status(self) -> None:
        # ESIBD setting attributes call widget setters, including LABEL fields.
        def update() -> None:
            self.controllerParent.main_state = self.main_state
            self.controllerParent.interlock_state = self.interlock_state
            self.controllerParent.detected_modules = self.detected_modules
            self.controllerParent.heat_status = self.heat_status
            refresh = getattr(self.controllerParent, "_update_status_widgets", None)
            if callable(refresh):
                refresh()

        _invoke_gui_callback(update)

    def _initial_open_incomplete(self) -> bool:
        return self.device is not None and any(bool(getattr(self.device, name, False)) for name in (
            "_open_failed", "_opening_in_progress", "_failed_open_released",
        ))

    def _dispose_device(self, *, shutdown_confirmed: bool = False) -> bool:
        self._output_cancel.set()
        with self._output_lock:
            if self.device is None:
                return self.main_state != "Shutdown unconfirmed"
            if self._initial_open_incomplete():
                released = False
                try:
                    released = self.device.disconnect(
                        timeout_s=float(self.controllerParent.connect_timeout_s)
                    ) is True
                except Exception as exc:
                    self.print(f"ESI connection cleanup remains unconfirmed: {exc}", flag=PRINT.ERROR)
                self.initialized = False
                self.acquiring = False
                self.main_state = "Disconnected" if released else "Connection pending"
                self._restore_off_ui_state()
                self._sync_status()
                if not released:
                    return False
            elif not shutdown_confirmed:
                return self._shutdown_communication_unlocked()
            device, self.device = self.device, None
            with contextlib.suppress(Exception):
                device.close()
            return True
