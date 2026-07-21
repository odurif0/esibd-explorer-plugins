"""Control the CGC ESI source, two HV supplies, and heater module."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import importlib.util
import sys
import time
from pathlib import Path
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
from esibd.plugins import Device, Plugin


_RUNTIME_PREFIX = "_esibd_bundled_esi_runtime"
_ESI_DRIVER_CLASS: type[Any] | None = None
_ESI_MAX_VOLTAGE = 3000.0
_ESI_MAX_TEMPERATURE = 175.0
_ESI_HEAT_MODULE = 0
_ESI_HV_CHANNELS = ((1, 1), (2, 2))
_ESI_HV_MODULES = (1, 2)
_ESI_MODULES = (_ESI_HEAT_MODULE, *_ESI_HV_MODULES)
_ESI_COMMUNICATION_LOST = "Communication lost"
_PARAMETER_UNIT_KEY = getattr(Parameter, "UNIT", "Unit")
_ESI_POWER_ON_ICON = "switch-medium_on.png"
_ESI_POWER_OFF_ICON = "switch-medium_off.png"
_ESI_HV_CARD_WIDTH = 300
_ESI_CARD_SPACING = 12
_ESI_HEAT_CARD_WIDTH = 2 * _ESI_HV_CARD_WIDTH + _ESI_CARD_SPACING

_ESI_PANEL_CARD_ON = "QFrame { background-color: #162433; border: 1px solid #3182ce; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_CARD_OFF = "QFrame { background-color: #202938; border: 1px solid #64748b; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_CARD_DISC = "QFrame { background-color: #151b26; border: 1px solid #475569; border-radius: 8px; color: #e2e8f0; }"
_ESI_PANEL_CARD_HEAT = "QFrame { background-color: #2d1b0e; border: 1px solid #d97706; border-radius: 8px; color: #f7fafc; }"
_ESI_PANEL_TITLE = "color: #f8fafc; font-weight: 700; font-size: 14px;"
_ESI_PANEL_NAME = "color: #cbd5e1; font-weight: 600;"
_ESI_PANEL_VALUE = "color: #f8fafc; font-weight: 600;"
_ESI_PANEL_OFF = "color: #94a3b8; font-weight: 600;"
_ESI_PANEL_OK = "color: #4ade80; font-weight: 600;"
_ESI_PANEL_ERR = "color: #f87171; font-weight: 700;"
_ESI_BTN_HV_ACTIVE = "QPushButton { background-color: #3182ce; color: #f8fafc; font-weight: 700; border-radius: 4px; }"
_ESI_BTN_HV_OFF = "QPushButton { background-color: #374151; color: #bfdbfe; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"
_ESI_BTN_OFF_ACTIVE = "QPushButton { background-color: #4b5563; color: #e2e8f0; font-weight: 600; border-radius: 4px; }"
_ESI_BTN_OFF_INACTIVE = "QPushButton { background-color: #374151; color: #94a3b8; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"
_ESI_BTN_HEAT_ACTIVE = "QPushButton { background-color: #d97706; color: #1a1a2e; font-weight: 700; border-radius: 4px; }"
_ESI_BTN_HEAT_OFF = "QPushButton { background-color: #374151; color: #fbbf24; font-weight: 600; border-radius: 4px; } QPushButton:hover { background-color: #4b5563; }"


def _runtime_module_name(plugin_dir: Path) -> str:
    digest = hashlib.sha256(str(plugin_dir.resolve()).encode()).hexdigest()[:12]
    return f"{_RUNTIME_PREFIX}_{digest}"


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


def _fixed_channel_items(device_name: str) -> list[dict[str, Any]]:
    """Return the two module-level HV channels plus the heater channel."""
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
    return channels


def providePlugins() -> "list[type[Plugin]]":
    return [ESIDevice]


class ESIDevice(Device):
    """Electrospray HV and HEAT-CTRL-2410 controller."""

    documentation = (
        "Controls CGC ESI HVPS-3kB and HEAT-CTRL-2410 modules and monitors "
        "voltage, current, temperature, power, interlocks, and controller health."
    )
    name = "ESI"
    version = "0.1.0"
    supportedVersion = "1.0.1"
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
        self._ensure_local_on_action()
        self._ensure_status_widgets()
        self._ensure_operator_panel()
        self._update_channel_column_visibility()

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
            toolTipTrue=f"Turn {self.name} OFF.",
            iconTrue=self.makeIcon(_ESI_POWER_OFF_ICON),
            before=self.closeCommunicationAction,
            restore=False,
            defaultState=False,
        )
        self._sync_local_on_action()

    def _sync_local_on_action(self) -> None:
        action = getattr(self, "deviceOnAction", None)
        if action is None:
            return
        blocker = getattr(action, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        try:
            action.state = self.isOn()
        finally:
            if callable(blocker):
                blocker(False)

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

    def setOn(self, on: "bool | None" = None) -> None:
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
        if controller and getattr(controller, "initialized", False):
            controller.toggleOnFromThread(parallel=True)
        elif hasattr(self, "onAction") and self.isOn():
            self.initializeCommunication()

    def _ensure_operator_panel(self) -> None:
        """Replace the channel table with a compact operator control panel."""
        if hasattr(self, "esiPanel"):
            self._update_operator_panel()
            return
        try:
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
        cards_layout = QHBoxLayout(cards_row)
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(_ESI_CARD_SPACING)

        self.esiHVCards: dict[int, dict[str, Any]] = {}
        cards_layout.addStretch(1)
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
            current_label = QLabel("Current")
            current_label.setStyleSheet(_ESI_PANEL_NAME)
            current_value = QLabel("n/a")
            current_value.setStyleSheet(_ESI_PANEL_VALUE)

            grid = QGridLayout()
            grid.setContentsMargins(0, 0, 0, 0)
            grid.setHorizontalSpacing(10)
            grid.setVerticalSpacing(6)
            grid.addWidget(target_label, 0, 0)
            grid.addWidget(target_value, 0, 1)
            grid.addWidget(measured_label, 1, 0)
            grid.addWidget(measured_value, 1, 1)
            grid.addWidget(current_label, 2, 0)
            grid.addWidget(current_value, 2, 1)
            cl.addLayout(grid)

            cards_layout.addWidget(card)
            self.esiHVCards[address] = {
                "card": card,
                "sel_group": sel_group,
                "btn_on": btn_on,
                "btn_off": btn_off,
                "target": target_value,
                "measured": measured_value,
                "current": current_value,
            }
            sel_group.idClicked.connect(
                lambda gid, addr=address: self._panel_output_selected(addr, gid)
            )
        cards_layout.addStretch(1)
        layout.addWidget(cards_row)

        heat_card = QFrame()
        heat_card.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        heat_card.setFixedWidth(_ESI_HEAT_CARD_WIDTH)
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
        heat_header.addWidget(heat_title)
        heat_header.addStretch(1)
        heat_header.addWidget(heat_btn)
        heat_cl.addLayout(heat_header)

        heat_grid = QGridLayout()
        heat_grid.setContentsMargins(0, 0, 0, 0)
        heat_grid.setHorizontalSpacing(10)
        heat_grid.setVerticalSpacing(6)
        for row, (name, key) in enumerate((
            ("Set", "heat_target"),
            ("Measured", "heat_measured"),
            ("Power", "heat_power"),
            ("Sensor", "heat_sensor"),
            ("Interlock", "heat_interlock"),
        )):
            nl = QLabel(name)
            nl.setStyleSheet(_ESI_PANEL_NAME)
            vl = QLabel("n/a")
            vl.setStyleSheet(_ESI_PANEL_VALUE)
            heat_grid.addWidget(nl, row, 0)
            heat_grid.addWidget(vl, row, 1)
        heat_cl.addLayout(heat_grid)

        heat_row = QWidget()
        heat_layout = QHBoxLayout(heat_row)
        heat_layout.setContentsMargins(0, 0, 0, 0)
        heat_layout.addStretch(1)
        heat_layout.addWidget(heat_card)
        heat_layout.addStretch(1)
        layout.addWidget(heat_row)

        self.esiPanel = panel
        self.esiHeatButton = heat_btn
        self.esiHeatWidgets = {
            "heat_target": heat_grid.itemAtPosition(0, 1).widget(),
            "heat_measured": heat_grid.itemAtPosition(1, 1).widget(),
            "heat_power": heat_grid.itemAtPosition(2, 1).widget(),
            "heat_sensor": heat_grid.itemAtPosition(3, 1).widget(),
            "heat_interlock": heat_grid.itemAtPosition(4, 1).widget(),
        }
        heat_btn.toggled.connect(self._panel_heat_toggled)

        if self.tree is not None:
            self.tree.setVisible(False)
        self.addContentWidget(panel)
        self._update_operator_panel()

    def _panel_target_changed(self, address: int, value: float) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if channel.module_address() == address and not channel.is_heat_channel():
                channel.getParameterByName(channel.VALUE).value = float(value)
                break
        self._update_operator_panel()

    def _panel_output_selected(self, address: int, gid: int) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if channel.module_address() == address and not channel.is_heat_channel():
                want_enabled = gid == 1
                if channel.enabled != want_enabled:
                    channel.getParameterByName(channel.ENABLED).value = want_enabled
                break
        self._update_operator_panel()

    def _panel_heat_toggled(self, checked: bool) -> None:
        if getattr(self, "loading", False):
            return
        for channel in self.getChannels():
            if channel.is_heat_channel():
                channel.getParameterByName(channel.ENABLED).value = checked
        self._update_operator_panel()

    def _update_operator_panel(self) -> None:
        cards = getattr(self, "esiHVCards", None)
        if not isinstance(cards, dict):
            return
        controller = getattr(self, "controller", None)
        connected = controller is not None and getattr(controller, "initialized", False)
        values = getattr(controller, "values", {}) or {}
        currents = getattr(controller, "currents", {}) or {}

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
                ):
                    output_enabled = bool(channel.enabled)
                    target_value = abs(float(channel.value))
                    break

            if not connected:
                card.setStyleSheet(_ESI_PANEL_CARD_DISC)
                for btn in (btn_on, btn_off):
                    btn.setEnabled(False)
                for key in ("target", "measured", "current"):
                    w = widgets[key]
                    w.setEnabled(False)
                    if hasattr(w, "setText"):
                        w.setText("n/a")
                        w.setStyleSheet(_ESI_PANEL_OFF)
                continue

            for btn in (btn_on, btn_off):
                btn.setEnabled(True)
            widgets["target"].setEnabled(True)
            if output_enabled:
                card.setStyleSheet(_ESI_PANEL_CARD_ON)
                btn_on.setChecked(True)
                btn_on.setStyleSheet(_ESI_BTN_HV_ACTIVE)
                btn_off.setStyleSheet(_ESI_BTN_OFF_INACTIVE)
            else:
                card.setStyleSheet(_ESI_PANEL_CARD_OFF)
                btn_off.setChecked(True)
                btn_on.setStyleSheet(_ESI_BTN_HV_OFF)
                btn_off.setStyleSheet(_ESI_BTN_OFF_ACTIVE)
            # Update spinbox without triggering valueChanged
            spin = widgets["target"]
            spin.blockSignals(True)
            spin.setValue(target_value)
            spin.blockSignals(False)
            measured = values.get(address, np.nan)
            current = currents.get(address, np.nan)
            widgets["measured"].setText(
                f"{measured:.1f} V" if np.isfinite(measured) else "n/a"
            )
            widgets["current"].setText(
                f"{current * 1e9:.2f} nA" if np.isfinite(current) else "n/a"
            )

        heat_btn = getattr(self, "esiHeatButton", None)
        heat = getattr(self, "esiHeatWidgets", None)
        if isinstance(heat, dict) and connected:
            heat_valid = getattr(controller, "heat_readback_valid", False)
            heat_temp = values.get(_ESI_HEAT_MODULE, np.nan)
            heat_enabled = False
            for channel in self.getChannels():
                if channel.is_heat_channel():
                    heat_enabled = channel.enabled
                    break
            if heat_btn is not None:
                loading = getattr(self, "loading", False)
                if not loading:
                    heat_btn.blockSignals(True)
                    heat_btn.setChecked(heat_enabled)
                    heat_btn.blockSignals(False)
                heat_btn.setText("ON" if heat_enabled else "OFF")
                heat_btn.setStyleSheet(
                    _ESI_BTN_HEAT_ACTIVE if heat_enabled else _ESI_BTN_OFF_ACTIVE
                )
                heat_btn.setEnabled(heat_valid)
            heat["heat_target"].setText(
                f"{heat_temp:.1f} °C" if np.isfinite(heat_temp) else "n/a"
            )
            heat["heat_measured"].setText(
                f"{heat_temp:.1f} °C" if heat_valid else "INVALID"
            )
            heat["heat_sensor"].setStyleSheet(
                _ESI_PANEL_OK if heat_valid else _ESI_PANEL_ERR
            )
            heat["heat_sensor"].setText("OK" if heat_valid else "Disconnected")
            interlock = str(getattr(self, "interlock_state", "n/a") or "n/a")
            heat["heat_interlock"].setText(interlock)
            heat["heat_interlock"].setStyleSheet(
                _ESI_PANEL_OK if interlock == "OK" else _ESI_PANEL_ERR
            )
        elif isinstance(heat, dict):
            for widget in heat.values():
                widget.setText("n/a")
                widget.setStyleSheet(_ESI_PANEL_OFF)
            if heat_btn is not None:
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
                "Software ramp rate for normal target changes and ON/OFF "
                "transitions. Set to 0 for an immediate change."
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
                value=0.0,
                minimum=0.0,
                maximum=1000.0,
                toolTip=tooltip,
                parameterType=PARAMETERTYPE.FLOAT,
                attr=attr,
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
        settings[f"{self.name}/Interval"][Parameter.VALUE] = 1000
        settings[f"{self.name}/{self.MAXDATAPOINTS}"][Parameter.VALUE] = 100000
        return settings

    def ensureFixedChannels(self, *, persist: bool = False) -> None:
        """Replace the generic bootstrap layout with HV1, HV2, and HEAT."""
        channels = self.getChannels()
        existing_modules = [getattr(channel, "module", None) for channel in channels]
        expected_modules = [address for _number, address in _ESI_HV_CHANNELS]
        expected_modules.append(_ESI_HEAT_MODULE)
        expected_names = [item["Name"] for item in _fixed_channel_items(self.name)]
        existing_names = [str(getattr(channel, "name", "")) for channel in channels]
        if existing_modules == expected_modules and existing_names == expected_names:
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
        items = _fixed_channel_items(self.name)
        for item in items:
            matching = [
                channel
                for channel in channels
                if getattr(channel, "module", None) == item["Module"]
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
        """Create the fixed three-channel ESI layout instead of nine generic channels."""
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

    def closeCommunication(self) -> None:
        controller = getattr(self, "controller", None)
        if controller is not None:
            controller.shutdownCommunication()
        super().closeCommunication()


class ESIChannel(Channel):
    """One HVPS-3kB module pair or the HEAT-CTRL-2410 channel."""

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

    @property
    def unit(self) -> str:
        """Return the physical unit for this mixed-function ESI channel."""
        return "degC" if getattr(self, "module", 2) == _ESI_HEAT_MODULE else "V"

    def getDisplayUnit(self) -> str:
        return self.unit

    def initGUI(self, item: dict) -> None:
        super().initGUI(item)
        for parameter_name in (self.VALUE, self.MONITOR):
            self.getParameterByName(parameter_name).unit = self.unit

    def enabledChanged(self) -> None:
        super().enabledChanged()
        if not getattr(self.channelParent, "loading", False):
            self.applyValue(apply=True)


class ESIController(DeviceController):
    """ESIBD bridge for the timeout-safe ESI runtime."""

    controllerParent: ESIDevice

    def __init__(self, controllerParent) -> None:
        super().__init__(controllerParent=controllerParent)
        self.device: Any | None = None
        self.values: dict[int, float] | None = None
        self.currents: dict[int, float] = {}
        self.initialized = False
        self.main_state = "Disconnected"
        self.identity: dict[str, Any] = {}
        self.heat_readback_valid = False
        self.heat_max_temperature_c = _ESI_MAX_TEMPERATURE

    def runInitialization(self) -> None:
        self.initialized = False
        self._dispose_device()
        try:
            driver = _get_esi_driver_class()
            self.device = driver(
                device_id=f"esi_com{int(self.controllerParent.com)}",
                com=int(self.controllerParent.com),
                baudrate=int(self.controllerParent.baudrate),
                process_backend=False,
            )
            backend_reason = str(
                getattr(self.device, "_process_backend_disabled_reason", "")
            ).strip()
            if backend_reason:
                self.print(backend_reason, flag=PRINT.WARNING)
            self.device.connect(timeout_s=float(self.controllerParent.connect_timeout_s))
            self.device.set_global_active(
                True,
                timeout_s=float(self.controllerParent.connect_timeout_s),
            )
            heat_limits = {
                "voltage_v": float(self.controllerParent.heat_voltage_limit_v),
                "current_a": float(self.controllerParent.heat_current_limit_a),
                "power_w": float(self.controllerParent.heat_power_limit_w),
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
            snapshot = self.device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self.device.force_safe_off(
                timeout_s=float(self.controllerParent.connect_timeout_s)
            )
            self._apply_snapshot(snapshot)
            self.signalComm.initCompleteSignal.emit()
        except Exception as exc:
            self._restore_off_ui_state()
            self.print(
                f"ESI initialization failed on COM{int(self.controllerParent.com)}: {exc}\n"
                "Confirm the configured COM port, power the controller, and close "
                "the hardware probe and vendor control application before retrying.",
                flag=PRINT.ERROR,
            )
            self._dispose_device()
        finally:
            self.initializing = False

    def initComplete(self) -> None:
        self.controllerParent.ensureFixedChannels(persist=True)
        self.initializeValues(reset=True)
        self.initialized = self.device is not None
        with contextlib.suppress(AttributeError):
            super().initComplete()
        if self.initialized:
            self.print(
                "ESI initialized with HV and heater outputs forced OFF. "
                "Use the explicit output controls to energize HV1, HV2, or HEAT."
            )

    def initializeValues(self, reset: bool = False) -> None:
        if self.values is None or reset:
            self.values = {address: np.nan for address in _ESI_MODULES}
            self.currents = {address: np.nan for address in _ESI_MODULES}

    def readNumbers(self) -> None:
        if self.device is None or not self.initialized:
            self.initializeValues(reset=True)
            return
        if not self.controllerParent.isOn():
            self.initializeValues(reset=True)
            return
        try:
            snapshot = self.device.collect_diagnostics(
                timeout_s=float(self.controllerParent.poll_timeout_s)
            )
            self._apply_snapshot(snapshot)
        except Exception as exc:
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

    def applyValue(self, channel: ESIChannel) -> None:
        if self.device is None or not self.initialized or not self.controllerParent.isOn():
            return
        if not channel.enabled:
            try:
                self.device.set_output_active(
                    channel.module_address(),
                    False,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
            except Exception as exc:
                self.errorCount += 1
                self.print(
                    f"ESI failed to disable {channel.name}: {exc}",
                    flag=PRINT.ERROR,
                )
            return
        if channel.is_heat_channel():
            target = float(channel.value)
            try:
                self._require_valid_heat_readback()
                self.device.set_heater_temperature(
                    target,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
                self.device.set_output_active(
                    channel.module_address(),
                    True,
                    timeout_s=float(self.controllerParent.poll_timeout_s),
                )
            except Exception as exc:
                self.errorCount += 1
                rollback = self._disable_failed_channel(channel)
                self.print(
                    f"ESI rejected temperature {target:g} for HEAT: {exc}.{rollback}",
                    flag=PRINT.ERROR,
                )
            return
        # The C API exposes one unsigned target for the module's +/- output pair.
        target = abs(float(channel.value))
        try:
            self.device.set_hv_module_target(
                channel.module_address(),
                target,
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
        except Exception as exc:
            self.errorCount += 1
            rollback = self._disable_failed_channel(channel)
            self.print(
                f"ESI rejected target {target:g} V for module "
                f"{channel.module_address()}: {exc}.{rollback}",
                flag=PRINT.ERROR,
            )

    def updateValues(self) -> None:
        if self.values is None:
            return
        for channel in self.controllerParent.getChannels():
            if not (channel.enabled and channel.real):
                channel.monitor = np.nan
                continue
            if channel.is_heat_channel():
                channel.monitor = self.values.get(channel.module_address(), np.nan)
            else:
                channel.monitor = self.values.get(channel.module_address(), np.nan)

    def toggleOn(self) -> None:
        super().toggleOn()
        if self.device is None:
            return
        if getattr(self, "acquiring", False):
            self.stopAcquisition()
            self.acquiring = False
        timeout = float(self.controllerParent.connect_timeout_s)
        try:
            if self.controllerParent.isOn():
                # Safe OFF guarantees stored targets are zero before this gate opens.
                self.device.set_global_active(True, timeout_s=timeout)
                for address in _ESI_HV_MODULES:
                    self.device.set_hv_module_target(address, 0.0, timeout_s=timeout)
                for channel in self.controllerParent.getChannels():
                    if channel.is_heat_channel() or channel.enabled:
                        self.applyValue(channel)
                self.startAcquisition()
            else:
                self.device.force_safe_off(timeout_s=timeout)
                self.main_state = "Outputs OFF"
                self._sync_status()
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

    def _disable_failed_channel(self, channel: ESIChannel) -> str:
        """Best-effort safe fallback after a channel command fails."""
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
        try:
            device.force_safe_off(
                timeout_s=float(self.controllerParent.connect_timeout_s)
            )
        except Exception as rollback_exc:
            return False, (
                " Global safe OFF also failed; HV/heater state is unconfirmed. "
                f"Use the hardware interlock before approaching the source: {rollback_exc}"
            )
        return True, " All outputs were forced OFF"

    def _restore_off_ui_state(self) -> None:
        """Keep the UI from claiming ON after a failed transition."""
        sync_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_state):
            sync_state(False)
            return
        action = getattr(self.controllerParent, "onAction", None)
        if action is not None:
            action.state = False

    def _restore_on_ui_state(self) -> None:
        """Keep OFF reachable while the physical output state is uncertain."""
        sync_state = getattr(self.controllerParent, "_set_on_ui_state", None)
        if callable(sync_state):
            sync_state(True)
            return
        action = getattr(self.controllerParent, "onAction", None)
        if action is not None:
            action.state = True

    def _ramp_target(self, address: int, start: float, target: float) -> None:
        """Apply one normal setpoint transition in bounded 100 ms steps."""
        device = self.device
        if device is None:
            return
        rate = max(0.0, float(getattr(self.controllerParent, "ramp_rate_v_s", 0.0)))
        delta = float(target) - float(start)
        if rate == 0.0 or delta == 0.0:
            device.set_hv_module_target(
                address,
                float(target),
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            return

        step_interval_s = 0.1
        steps = max(1, int(np.ceil(abs(delta) / (rate * step_interval_s))))
        for step in range(1, steps + 1):
            value = float(start) + delta * step / steps
            device.set_hv_module_target(
                address,
                value,
                timeout_s=float(self.controllerParent.poll_timeout_s),
            )
            if step < steps:
                time.sleep(step_interval_s)

    def shutdownCommunication(self) -> bool:
        device = self.device
        if device is None:
            return True
        confirmed = False
        try:
            device.disconnect(timeout_s=float(self.controllerParent.connect_timeout_s))
            confirmed = True
        except Exception as exc:
            self.print(
                "ESI shutdown could not be confirmed. HV may remain energized; "
                f"use the hardware interlock/front panel: {exc}",
                flag=PRINT.ERROR,
            )
        finally:
            self._dispose_device()
            self.initialized = False
            self.main_state = "Disconnected" if confirmed else "Shutdown unconfirmed"
            self._sync_status()
        return confirmed

    def closeCommunication(self) -> None:
        with contextlib.suppress(AttributeError):
            super().closeCommunication()
        self.shutdownCommunication()

    def _apply_snapshot(self, snapshot: dict[str, Any]) -> None:
        self.main_state = str(snapshot["main_state"]["name"])
        self.values = {}
        self.currents = {}
        for address, module in snapshot["modules"].items():
            address = int(address)
            self.values[address] = (
                float(module["measured_v"]) if module["voltage_valid"] else np.nan
            )
            self.currents[address] = (
                float(module["measured_a"]) if module["current_valid"] else np.nan
            )
        heat = snapshot["heat"]
        heat_temperature = float(heat["monitor_temperature_c"])
        self.heat_max_temperature_c = float(
            heat["hardware_limits"]["max_temperature_c"]
        )
        self.heat_readback_valid = bool(
            heat["valid"]
            and np.isfinite(heat_temperature)
            and 0.0 <= heat_temperature <= self.heat_max_temperature_c
        )
        self.values[_ESI_HEAT_MODULE] = (
            heat_temperature if self.heat_readback_valid else np.nan
        )
        self.currents[_ESI_HEAT_MODULE] = (
            float(heat["monitor_current_a"]) if heat["valid"] else np.nan
        )
        self.controllerParent.main_state = self.main_state
        flags = snapshot["interlock_state"]["flags"]
        self.controllerParent.interlock_state = ", ".join(flags) if flags else "OK"
        module_identity = self.identity.get("modules", {})
        labels = []
        for address in _ESI_MODULES:
            info = module_identity.get(address, module_identity.get(str(address), {}))
            product_id = info.get("product_id") if isinstance(info, dict) else None
            fallback = "HEAT-CTRL-2410" if address == _ESI_HEAT_MODULE else "HVPS-3kB"
            label = product_id if isinstance(product_id, str) and product_id else fallback
            labels.append(f"{address}: {label}")
        self.controllerParent.detected_modules = ", ".join(labels)
        if self.heat_readback_valid:
            self.controllerParent.heat_status = (
                f"T={heat_temperature:.1f} degC, "
                f"P={float(heat['heater_power_w']):.2f} W, "
                f"Ilock=0x{int(heat['interlock_state']):02X}"
            )
        else:
            self.controllerParent.heat_status = (
                f"INVALID T={heat_temperature:.1f} degC; check temperature sensor"
            )
        self._sync_status()

    def _sync_status(self) -> None:
        self.controllerParent.main_state = self.main_state
        update = getattr(self.controllerParent, "_update_status_widgets", None)
        if callable(update):
            update()

    def _dispose_device(self) -> None:
        device = self.device
        self.device = None
        if device is not None:
            with contextlib.suppress(Exception):
                device.disconnect(
                    timeout_s=float(
                        getattr(self.controllerParent, "connect_timeout_s", 5.0)
                    )
                )
            with contextlib.suppress(Exception):
                device.close()
