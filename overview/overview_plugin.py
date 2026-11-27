"""Overview: one page that shows the beamline and turns every device ON or OFF.

User request (2026-11-22): a unified, simple page with the essential parameters, one card per chamber in beam
order with its pressure, and a general ON/OFF. Version 1 only reads: it never writes a channel. All ON and All OFF
press each device's own ON button, as the operator would in Explorer's device bar. All ON: one device after the
other in beam order, after a confirmation, stopping at the first failure. All OFF: no confirmation; scans and maps
stop first, then every supply's OFF is pressed at once (ESI, AMX, PSU, AMPR), the readers (DMMR, TPG366) once the
supplies are OFF; a device that fails or hangs never holds the others. A device's
lights and states are those its plugin reports. Logic without Qt in ``_runtime/_overview.py``; events in
<Explorer data path>/logs/overview/overview.log.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

from collections import deque
import html
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time

from PyQt6.QtCore import QPointF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPalette, QPen, QPixmap, QPolygonF
from PyQt6.QtWidgets import (QFrame, QGridLayout, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea,
                             QSizePolicy, QStyle, QToolButton, QVBoxLayout, QWidget)

from esibd.core import PLUGINTYPE, PRINT
from esibd.plugins import Plugin, Scan

_RUNTIME_FILES = ("__init__.py", "_overview.py")


def _load_runtime():
    # Explorer imports every root *.py file without sys.modules registration: the runtime is loaded privately.
    root = Path(__file__).resolve().parent / "_runtime"
    missing = [name for name in _RUNTIME_FILES if not (root / name).is_file()]
    if missing:
        raise ModuleNotFoundError(f"Missing bundled Overview runtime file(s) in {root}: {', '.join(missing)}")
    name = f"_esibd_bundled_overview_{root.parent.name}"
    try:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError(f"Cannot load the bundled Overview runtime: {root}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return importlib.import_module(f"{name}._overview")
    except BaseException:
        for key in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
            sys.modules.pop(key, None)
        raise


_overview = _load_runtime()


def providePlugins():
    return [Overview]


# Status colours only (state of a device, of Sync, a value outside its window); everything else follows the palette.
LIGHTS = {"on": "#2f855a", "ok": "#2f855a", "busy": "#dd6b20", "fault": "#c53030", "off": "#718096",
          "unknown": "#a0aec0"}
WARN = "#dd6b20"
CARD_STYLE = "QFrame#overviewCard { border: 1px solid palette(mid); border-radius: 8px; }"


def _dot(color, size=12):
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def _dim(label):
    label.setForegroundRole(QPalette.ColorRole.PlaceholderText)
    return label


class _Trend(QWidget):
    """The last 30 minutes of one value, sampled by the Overview itself (never from Explorer's history)."""

    WINDOW_S = 1800.0

    def __init__(self):
        super().__init__()
        self.points = deque(maxlen=4000)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip("Last 30 min, one point per second.")

    def add(self, when, value):
        self.points.append((when, value))
        while self.points and self.points[0][0] < when - self.WINDOW_S:
            self.points.popleft()
        self.update()

    def paintEvent(self, event):  # noqa: ARG002, N802 - Qt
        values = [v for _, v in self.points if v is not None]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        painter.setPen(palette.color(QPalette.ColorRole.PlaceholderText))
        painter.drawText(self.rect().adjusted(0, 0, -2, 0), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
                         "30 min")
        if len(values) < 2:
            painter.end()
            return
        low, high = min(values), max(values)
        span = (high - low) or abs(high) or 1.0
        end = self.points[-1][0]
        width, height = self.width() - 46, self.height() - 4
        pen = QPen(palette.color(QPalette.ColorRole.Highlight))
        pen.setWidthF(1.5)
        painter.setPen(pen)
        line = QPolygonF()
        for when, value in self.points:
            if value is None:
                if line.size() > 1:
                    painter.drawPolyline(line)
                line = QPolygonF()
                continue
            x = width * (1 - (end - when) / self.WINDOW_S)
            y = 2 + height * (1 - (value - low) / span)
            line.append(QPointF(x, y))
        if line.size() > 1:
            painter.drawPolyline(line)
        painter.end()


class _Card(QFrame):
    """One chamber: its title and pressure, then its rows; the main value of the card in large digits."""

    def __init__(self, card, fixed, big):
        super().__init__()
        self.setObjectName("overviewCard")
        self.setStyleSheet(CARD_STYLE)
        self.base_title = card["title"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        head = QHBoxLayout()
        self.title = QLabel(card["title"])
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.pressure = _dim(QLabel(""))
        self.pressure.setFont(fixed)
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.pressure)
        layout.addLayout(head)
        self.rows = []
        for row in card["rows"]:
            line = QHBoxLayout()
            name = QLabel(row["label"])
            value = QLabel("—")
            value.setFont(big if row.get("main") else fixed)
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            line.addWidget(name)
            line.addStretch(1)
            line.addWidget(value)
            layout.addLayout(line)
            small = _dim(QLabel(""))
            small.setAlignment(Qt.AlignmentFlag.AlignRight)
            small.setVisible(False)
            layout.addWidget(small)
            trend = None
            if row.get("trend"):
                trend = _Trend()
                layout.addWidget(trend)
            self.rows.append((name, value, small, trend))
        layout.addStretch(1)
        self.setMinimumWidth(_Cards.CARD_W)
        self.setMaximumWidth(_Cards.CARD_MAX_W)  # Compact cards; the label stays near its value.

    def show_view(self, view, when):
        title = self.base_title + (f" · {view.owner}" if view.owner else "")
        if self.title.text() != title:
            self.title.setText(title)
            self.title.setToolTip(f"{view.owner} drives this quadrupole." if view.owner else "")
        if self.pressure.text() != view.pressure:
            self.pressure.setText(view.pressure)
        self.pressure.setToolTip(view.pressure_tip)
        for (name, value, small, trend), row in zip(self.rows, view.rows):
            if value.text() != row.text:
                value.setText(row.text)
            style = f"color: {WARN};" if row.warn else ""
            if value.styleSheet() != style:
                value.setStyleSheet(style)
            value.setToolTip(row.tooltip)
            name.setToolTip(row.tooltip)
            if small.text() != row.small:
                small.setText(row.small)
                small.setVisible(bool(row.small))
            if trend is not None:
                trend.add(when, row.trend)

    def restyle(self):
        self.setStyleSheet("")
        self.setStyleSheet(CARD_STYLE)


class _Cards(QWidget):
    """The cards in beam order, left to right then top to bottom; fewer columns when the dock is narrow."""

    CARD_W = 210
    CARD_MAX_W = 340
    MAX_COLUMNS = 4  # Eight cards: two rows of four on a wide dock.

    def __init__(self, cards):
        super().__init__()
        self.cards = cards
        self.grid = QGridLayout(self)
        # Room for the vertical scroll bar: in a short dock the last column slid under it (probe, 2026-11-22).
        self.grid.setContentsMargins(0, 0, self.style().pixelMetric(QStyle.PixelMetric.PM_ScrollBarExtent) + 2, 0)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.columns = 0
        self._place(self.MAX_COLUMNS)

    def _place(self, columns):
        if columns == self.columns:
            return
        self.columns = columns
        for card in self.cards:
            self.grid.removeWidget(card)
        for index, card in enumerate(self.cards):
            self.grid.addWidget(card, index // columns, index % columns)

    def resizeEvent(self, event):  # noqa: N802 - Qt
        spacing = self.grid.spacing()
        self._place(max(1, min(self.MAX_COLUMNS, (event.size().width() + spacing) // (self.CARD_W + spacing))))
        super().resizeEvent(event)


class Overview(Plugin):
    """The beamline on one page, one card per chamber in beam order, and All ON / All OFF for every device.

    Read-only: the Overview never writes a channel. All ON and All OFF press each device's own ON button, one after
    the other; a device's state is the one its plugin reports.
    """

    name = "Overview"
    pluginType = PLUGINTYPE.CONTROL
    version = "0.6.3"
    supportedVersion = "1.0"
    iconFile = "overview.png"
    CONFIG_FILE = "Overview.json"
    REFRESH_MS = 1000
    SEQUENCE_MS = 250

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.clock = time.monotonic  # Tests may give their own.
        self._board = None
        self._config = {}
        self._config_broken = False
        self._sequence = None
        self._timer = None
        self._sequence_timer = None
        self._buttons = {}
        self._states = {}
        self._refresh_error = ""
        self.cards = None

    # ---------------------------------------------------------------- configuration
    def _config_path(self):
        return Path(self.pluginManager.Settings.configPath) / self.CONFIG_FILE

    def _load_config(self):
        """Layout (default: the lab beamline) and the output addresses already seen for its channel names."""
        path = self._config_path()
        layout = _overview.DEFAULT_LAYOUT
        if not path.is_file():
            return layout, {}
        try:
            self._config = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(self._config, dict):
                raise ValueError("not a JSON object")
            if "cards" in self._config:
                layout = _overview.check_layout(self._config["cards"])
            addresses = self._config.get("addresses", {})
            return layout, addresses if isinstance(addresses, dict) else {}
        except (OSError, ValueError) as exc:
            self._config_broken = True  # Never overwritten: the operator's file is kept as it is.
            self.print(f"{path.name} not used ({exc}); the default layout is shown.", flag=PRINT.WARNING)
            return _overview.DEFAULT_LAYOUT, {}

    def _save_addresses(self):
        resolver = self._board.resolver
        resolver.changed = False
        if self._config_broken:
            return
        self._config["addresses"] = dict(sorted(resolver.bindings.items()))
        path = self._config_path()
        try:
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(self._config, indent=1, ensure_ascii=False), encoding="utf-8")
            os.replace(temporary, path)
        except OSError as exc:
            self._log(f"Cannot save {path.name}: {exc}")

    # ---------------------------------------------------------------- GUI
    def initGUI(self):
        super().initGUI()
        layout, addresses = self._load_config()
        self._board = _overview.Board(layout, addresses)
        fixed = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        big = QFont(fixed)
        big.setPointSizeF(max(fixed.pointSizeF(), 9.0) * 1.5)
        big.setBold(True)
        top = QHBoxLayout()
        self.all_off_button = QPushButton("■ All OFF")
        self.all_off_button.setToolTip("Turn every device OFF at once: scans and maps stop first, the readers last.")
        self.all_on_button = QPushButton("▶ All ON")
        self.all_on_button.setToolTip("Turn every device ON in beam order, after confirmation.")
        for button in (self.all_off_button, self.all_on_button):
            font = button.font()
            font.setBold(True)
            button.setFont(font)
            button.setMinimumHeight(30)
        self.all_off_button.clicked.connect(self.all_off)
        self.all_on_button.clicked.connect(self.all_on)
        self.sequence_label = QLabel("")
        self.sequence_label.setWordWrap(True)
        top.addWidget(self.all_off_button)
        top.addWidget(self.all_on_button)
        top.addWidget(self.sequence_label, 1)
        self.device_row = QHBoxLayout()
        self.device_row.setSpacing(2)
        self.device_row.addStretch(1)
        self.cards = _Cards([_Card(card, fixed, big) for card in self._board.layout])
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)  # Fewer columns instead.
        scroll.setWidget(self.cards)
        self.activity_label = _dim(QLabel(""))
        self.activity_label.setWordWrap(True)
        self.activity_label.setTextFormat(Qt.TextFormat.RichText)
        self.log_label = _dim(QLabel(""))
        self.log_label.setWordWrap(True)
        main = QVBoxLayout()
        main.addLayout(top)
        main.addLayout(self.device_row)
        main.addWidget(scroll, 1)
        main.addWidget(self.activity_label)
        main.addWidget(self.log_label)
        self.addContentLayout(main)

    def finalizeInit(self):
        super().finalizeInit()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(self.REFRESH_MS)
        self._sequence_timer = QTimer(self)
        self._sequence_timer.timeout.connect(self._tick_sequence)

    def updateTheme(self):
        super().updateTheme()
        for card in getattr(self.cards, "cards", ()):
            card.restyle()
        self._states = {}  # Lights redrawn at the next refresh.

    def _devices(self):
        return list(self.pluginManager.DeviceManager.getDevices())

    def _plugin(self, name):
        plugin = getattr(self.pluginManager, name, None)
        return plugin if getattr(plugin, "name", None) == name else None

    def refresh(self):
        """Once a second on the Qt thread: the latest values only, never a history, never a call to a device."""
        if self._board is None or self.cards is None:
            return
        try:
            devices = self._devices()
            self._board.update(devices, self.pluginManager.DeviceManager.channels())
            if self._board.resolver.changed:
                self._save_addresses()
            self._show_lights(devices)
            when = time.time()
            for card, view in zip(self.cards.cards, self._board.cards(self._quadrupole_owner())):
                card.show_view(view, when)
            self._show_activity()
            self._refresh_error = ""
        except Exception as exc:  # noqa: BLE001 - said once, never raised into Qt's timer
            text = f"Overview not refreshed: {exc}"
            if text != self._refresh_error:
                self._refresh_error = text
                self._log(text)
                self.print(text, flag=PRINT.WARNING)

    def _show_lights(self, devices):
        shown = _overview.managed(devices, True)
        names = [device.name for device in shown]
        if names != list(self._buttons):
            for button in self._buttons.values():
                self.device_row.removeWidget(button)
                button.deleteLater()
            self._buttons, self._states = {}, {}
            for index, name in enumerate(names):
                button = QToolButton()
                button.setText(name)
                button.setAutoRaise(True)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                button.clicked.connect(lambda _checked=False, n=name: self._open(n))
                self.device_row.insertWidget(index, button)
                self._buttons[name] = button
        for device in shown:
            state = _overview.light(device)
            if self._states.get(device.name) == state:
                continue
            previous = self._states.get(device.name)
            self._states[device.name] = state
            button = self._buttons[device.name]
            button.setIcon(_dot(LIGHTS[state[0]]))
            detail = _overview.main_state(device)
            button.setToolTip(f"{device.name}: {state[1]}" + (f" (state: {detail})" if detail and detail != state[1] else "")
                              + ". Click to open its tab.")
            if previous is not None:
                self._log(f"{device.name}: {previous[1]} → {state[1]}")

    def _open(self, name):
        device = next((d for d in self._devices() if d.name == name), None)
        if device is not None:
            device.raiseDock(True)

    # ---------------------------------------------------------------- activity (MScan, Transmission, Sync)
    def _map_running(self, mscan):
        owns = getattr(mscan, "_map_owns_scans", None)
        try:
            return bool(owns()) if callable(owns) else False
        except Exception:  # noqa: BLE001
            return False

    def _quadrupole_owner(self):
        mscan = self._plugin("MScan")
        if mscan is None:
            return None
        if self._map_running(mscan):
            return str(getattr(mscan, "quadrupole", "") or ""), "MScan map"
        if getattr(mscan, "recording", False):
            return str(getattr(mscan, "quadrupole", "") or ""), "MScan scan"
        return None

    def _transmission_busy(self, transmission):
        busy = getattr(transmission, "_busy", None)
        try:
            return bool(busy()) if callable(busy) else bool(getattr(transmission, "recording", False))
        except Exception:  # noqa: BLE001
            return False

    def _show_activity(self):
        parts = []
        mscan = self._plugin("MScan")
        if mscan is not None:
            if self._map_running(mscan):
                parts.append(html.escape(f"MScan map: {getattr(mscan, 'map_status', '') or 'running'}"))
            elif getattr(mscan, "recording", False):
                parts.append("MScan scan running")
        transmission = self._plugin("Transmission")
        if transmission is not None and self._transmission_busy(transmission):
            parts.append("Transmission running")
        for scan in self.pluginManager.getPluginsByClass(Scan):
            if scan.name not in ("MScan", "Transmission") and getattr(scan, "recording", False):
                parts.append(html.escape(f"{scan.name} running"))
        sync = self._plugin("Sync")
        if sync is not None and callable(getattr(sync, "light_state", None)):
            state, words = sync.light_state()
            parts.append(f"<span style='color:{LIGHTS.get(state, LIGHTS['unknown'])}'>●</span> "
                         + html.escape(f"Sync: {words}"))
        text = " · ".join(parts) or "No scan running."
        if self.activity_label.text() != text:
            self.activity_label.setText(text)

    def _activity_running(self):
        """What still runs and must stop before All OFF turns the devices OFF ('' when nothing)."""
        running = []
        mscan = self._plugin("MScan")
        if mscan is not None and self._map_running(mscan):
            running.append("MScan map")
        transmission = self._plugin("Transmission")
        if transmission is not None and self._transmission_busy(transmission):
            running.append("Transmission")
        for scan in self.pluginManager.getPluginsByClass(Scan):
            if getattr(scan, "recording", False) or not getattr(scan, "finished", True):
                running.append(scan.name)
        return ", ".join(dict.fromkeys(running))

    def _stop_activity(self):
        """All OFF, first: the map (as its Stop map button), Transmission, then every scan (Explorer's stopScans)."""
        mscan = self._plugin("MScan")
        if mscan is not None and self._map_running(mscan):
            handler = getattr(mscan, "_map_action", None)  # The Map… button: stops a running or starting map.
            if callable(handler):
                handler()
        transmission = self._plugin("Transmission")
        if transmission is not None:
            if getattr(transmission, "_bringup", None) is not None and callable(getattr(transmission, "cancel_bringup", None)):
                transmission.cancel_bringup("All OFF pressed.")
            if self._transmission_busy(transmission) and callable(getattr(transmission, "stop_optimization", None)):
                transmission.stop_optimization()
        self.pluginManager.DeviceManager.stopScans()

    # ---------------------------------------------------------------- All ON / All OFF
    def running(self):
        return self._sequence is not None and not self._sequence.finished

    def all_off(self):
        """No confirmation, always available; cancels a running All ON."""
        if self.running():
            if not self._sequence.on:
                self._log("All OFF already running.")
                return
            self._sequence.cancel("All OFF pressed")
        self._start(False)

    def all_on(self):
        if self.running():
            self._log("A sequence is running: wait for it, or press All OFF.")
            return
        devices = _overview.managed(self._devices(), True)
        if not devices:
            self._log("All ON: no device to turn ON.")
            self._show_sequence()
            return
        if not self._confirm_on(devices):
            self._log("All ON declined.")
            return
        self._start(True)

    def _confirm_on(self, devices):
        """Ask the operator; True to go ahead. Tests replace this method."""
        box = QMessageBox(QMessageBox.Icon.Question, "All ON",
                          f"Turn ON, one after the other: {', '.join(d.name for d in devices)}?\n\n"
                          "Each device starts exactly as with its own ON button: a supply applies the setpoints of "
                          "its plugin, the AMPR ramps to them. The ESI starts with its HV and heater deselected.",
                          parent=self.all_on_button.window())
        go = box.addButton("Turn all ON", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is go

    def _start(self, on):
        self._sequence = _overview.Sequence(on, self._devices(), self.clock(), stop_activity=self._stop_activity,
                                            activity=self._activity_running, log=self._log)
        self.print(f"{self._sequence.word}: " + (", ".join(s.name for s in self._sequence.steps) or "no device") + ".")
        self._tick_sequence()
        if self.running():
            self._sequence_timer.start(self.SEQUENCE_MS)

    def _tick_sequence(self):
        sequence = self._sequence
        if sequence is None:
            return
        try:
            sequence.tick(self.clock())
        except Exception as exc:  # noqa: BLE001 - reported; no further press
            sequence.cancel(f"error: {exc}")
        if sequence.finished:
            if self._sequence_timer is not None:
                self._sequence_timer.stop()
            self.print(sequence.summary, flag=PRINT.MESSAGE if sequence.ok else PRINT.WARNING)
        self._show_sequence()

    def _show_sequence(self):
        sequence = self._sequence
        self.all_on_button.setEnabled(not self.running())
        if sequence is None:
            return
        if sequence.finished:
            text, style = sequence.summary, "" if sequence.ok else f"color: {LIGHTS['fault']};"
        elif sequence.phase == "activity":
            text, style = "All OFF: stopping scans and maps…", ""
        else:
            pending = sequence.pending()
            waited = self.clock() - min(step.started for step in pending) if pending else 0.0
            text = (f"{sequence.word}: " + ", ".join(f"{step.name} ({_overview.light(step.device)[1]})" for step in pending)
                    + f"… {waited:.0f} s") if pending else f"{sequence.word}…"
            style = ""
        if self.sequence_label.text() != text:
            self.sequence_label.setText(text)
        if self.sequence_label.styleSheet() != style:
            self.sequence_label.setStyleSheet(style)

    # ---------------------------------------------------------------- log
    def _log(self, text):
        line = f"{time.strftime('%H:%M:%S')} {text}"
        if getattr(self, "log_label", None) is not None:
            self.log_label.setText(line)
        try:
            folder = Path(self.pluginManager.Settings.dataPath) / "logs" / "overview"
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "overview.log").open("a", encoding="utf-8") as log:
                log.write(f"{time.strftime('%Y-%m-%d')} {line}\n")
        except OSError:
            pass  # A log that cannot be written never stops anything.

    def close(self):
        if self._timer is not None:
            self._timer.stop()
        if self._sequence_timer is not None:
            self._sequence_timer.stop()
        if self.running():
            self._sequence.cancel("Explorer closing")
        return super().close()
