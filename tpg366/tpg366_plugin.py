"""Pfeiffer TPG 366 pressure acquisition over the rear USB-B virtual COM port.

SPDX-License-Identifier: GPL-2.0-or-later
"""
from __future__ import annotations

import importlib.util
import hashlib
import logging
from pathlib import Path
import re
import sys
from threading import Event, Lock, Thread
import time

import numpy as np
import serial
from PyQt6.QtCore import pyqtSignal, pyqtSlot
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import QFrame, QLabel, QScrollArea

from esibd.core import Channel, DeviceController, PARAMETERTYPE, Parameter, PLUGINTYPE, PRINT, getTestMode, parameterDict
from esibd.plugins import Device


def _load_protocol():
    # Explorer imports every root *.py file without sys.modules registration.
    # Keep the protocol below that discovery level and load it privately here.
    path = Path(__file__).parent / "_runtime" / "_tpg366.py"
    if not path.is_file():
        raise ModuleNotFoundError(f"Missing bundled TPG366 protocol: {path}")
    name = "_esibd_bundled_tpg366"
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(f"Cannot load bundled TPG366 protocol: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module


_protocol = _load_protocol()


def _load_panel():
    path = Path(__file__).parent / "_readout_panel.py"
    if not path.is_file():
        raise ModuleNotFoundError(f"Missing bundled TPG366 panel: {path}")
    name = "_esibd_tpg366_readout_panel_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(f"Cannot load bundled TPG366 panel: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return module.PressurePanel


# A NAK, timeout or corrupt frame costs one sample, then a resynchronization.
# This many consecutive failed transactions (reads or resyncs) stop acquisition.
_READ_FAILURE_LIMIT = 3
_RESYNC_PAUSE_S = 0.2
_TRANSIENT_FRAME_ERRORS = ("Expected six status/pressure pairs", "Unknown gauge status", "Invalid pressure")


def _transient_read_error(exc):
    """Serial transaction or frame-format failure; unit errors stay fatal."""
    text = str(exc)
    return text.startswith(tuple(f"{command} [" for command in _protocol.READ_COMMANDS)) or text.startswith(
        _TRANSIENT_FRAME_ERRORS)


_GUI_DISPATCH_LOCK = Lock()


def _invoke_gui_callback(callback):
    """Run on the GUI thread: directly when already there, otherwise queued."""
    from PyQt6.QtCore import QObject, QThread, Qt
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None or QThread.currentThread() == app.thread():
        callback()
        return
    with _GUI_DISPATCH_LOCK:
        dispatcher = getattr(_invoke_gui_callback, "_dispatcher", None)
        if dispatcher is None:
            class _Dispatcher(QObject):
                requested = pyqtSignal(object)

                def __init__(self):
                    super().__init__()
                    self.requested.connect(self._run, Qt.ConnectionType.QueuedConnection)

                @pyqtSlot(object)
                def _run(self, queued):
                    try:
                        queued()
                    except Exception:
                        logging.getLogger(__name__).exception("TPG366 GUI callback failed.")

            dispatcher = _Dispatcher()
            dispatcher.moveToThread(app.thread())
            _invoke_gui_callback._dispatcher = dispatcher
    dispatcher.requested.emit(callback)


def _serial_port_name(value):
    """Accept Windows COM numbers like the CGC plugins, retaining explicit paths."""
    port = str(value).strip()
    match = re.fullmatch(r"(?:COM)?([0-9]+)", port, re.IGNORECASE)
    return f"COM{int(match.group(1))}" if match else port


def providePlugins():
    return [TPG366]


class TPG366(Device):
    """Read six pressures without changing gauge, relay or controller settings."""

    name = "TPG366"
    version = "0.1.3"
    supportedVersion = "1.0.2"
    pluginType = PLUGINTYPE.OUTPUTDEVICE
    unit = "mbar"
    logY = True
    useOnOffLogic = True
    iconFile = "tpg366.png"

    def exportConfigurationIfChanged(self) -> None:
        """Explorer 1.0.2 also runs this periodic save from a worker thread, but the
        export refreshes Explorer's file tree (Qt GUI work): run it on the GUI thread."""
        base = getattr(super(), "exportConfigurationIfChanged", None)
        if callable(base):
            _invoke_gui_callback(base)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.channelType = PressureChannel
        self.maxDataPoints = 100_000
        self.controller = PressureController(controllerParent=self)
        self._status_label = None
        self._pressure_panel = None

    def getDefaultSettings(self):
        settings = super().getDefaultSettings()
        settings[f"{self.name}/{self.INTERVAL}"][Parameter.VALUE] = 1000
        settings[f"{self.name}/COM"] = parameterDict(
            value="", parameterType=PARAMETERTYPE.TEXT, attr="com",
            toolTip="USB-B virtual COM port: number (e.g. 21), name (COM21), or device path. Do not share it with MAXIGAUGE.")
        settings[f"{self.name}/Baud rate"] = parameterDict(
            value=9600, parameterType=PARAMETERTYPE.INTCOMBO, attr="baudrate",
            items="9600,19200,38400,57600,115200", fixedItems=True,
            toolTip="Must match BAUD RATE USB on the TPG366 (factory setting: 9600).")
        settings[f"{self.name}/State"] = parameterDict(
            value="Disconnected", parameterType=PARAMETERTYPE.LABEL, attr="main_state",
            indicator=True, restore=False)
        settings[f"{self.name}/Identification"] = parameterDict(
            value="", parameterType=PARAMETERTYPE.LABEL, attr="identification",
            indicator=True, restore=False, advanced=True)
        return settings

    def initGUI(self):
        super().initGUI()
        self.initAction.setVisible(False)
        self.closeCommunicationAction.setVisible(False)
        self._status_label = QLabel("Disconnected")
        self.addContentWidget(self._status_label)
        self.tree.hide()
        for name in ("advancedAction", "duplicateChannelAction", "deleteChannelAction",
                     "moveChannelUpAction", "moveChannelDownAction", "copyAction"):
            action = getattr(self, name, None)
            if action is not None:
                action.setVisible(False)
        self._pressure_panel = _load_panel()(self._panel_edit)
        scroll = QScrollArea()
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._pressure_panel)
        self.addContentWidget(scroll)
        self._update_pressure_panel()
        self.deviceOnAction = self.addStateAction(
            event=lambda checked=False: self.setOn(checked),
            toolTipFalse="Connect TPG366 and start pressure acquisition.",
            iconFalse=self.makeIcon("switch-medium_on.png"),
            toolTipTrue="Stop TPG366 acquisition and disconnect; leave the gauges running.",
            iconTrue=self.makeIcon("switch-medium_off.png"),
            before=self.closeCommunicationAction, restore=False, defaultState=False)

    def finalizeInit(self):
        super().finalizeInit()
        self.onAction.toolTipFalse = "Connect TPG366 and start pressure acquisition."
        self.onAction.toolTipTrue = "Stop TPG366 acquisition and disconnect; leave the gauges running."
        self.onAction.setToolTip(self.onAction.toolTipFalse)
        self._set_state("Disconnected", False)

    def _panel_edit(self, channel, attribute, value):
        if attribute == "name":
            for char in getattr(channel, "invalid_chars", (".", "/")):
                value = value.replace(char.replace("\\", ""), "")
            value = value.strip() or channel.name
        if getattr(channel, attribute) != value:
            # Preserve Explorer's original Parameter callbacks and persistence.
            setattr(channel, attribute, value)
        self._update_pressure_panel()

    def _update_pressure_panel(self):
        panel = getattr(self, "_pressure_panel", None)
        if panel is not None:
            channels = [channel for channel in self.getChannels() if channel.real]
            for channel in channels:
                if self.controller.gauges:
                    channel.gauge = self.controller.gauges[int(channel.input) - 1]
            panel.refresh(channels, live=self.controller.acquiring and self.isOn())

    def _set_state(self, text, on):
        # Called only on the GUI thread: setting wrappers also touch Qt widgets.
        self.main_state = text
        if self._status_label is not None:
            self._status_label.setText(text)
        for name in ("onAction", "deviceOnAction"):
            action = getattr(self, name, None)
            if action is not None:
                action.state = on
        for channel in self.channels:
            channel.updateColor()
        self._update_pressure_panel()

    def loadConfiguration(self, file=None, useDefaultFile=False, append=False):
        # Prepopulate the physical six inputs, rather than Explorer's nine generic
        # channels. Existing user names and configuration are left untouched.
        if useDefaultFile and not self.customConfigFile(self.confINI).exists() and not self.channels:
            self.loading = True
            try:
                for i, color in enumerate(("#3182ce", "#d97706", "#2f855a", "#805ad5", "#c53030", "#319795"), 1):
                    self.addChannel({Channel.NAME: f"P{i}", PressureChannel.INPUT: i, Channel.COLOR: color})
            finally:
                self.loading = False
        super().loadConfiguration(file=file, useDefaultFile=useDefaultFile, append=append)
        self.estimateStorage()
        self._update_pressure_panel()

    def estimateStorage(self):
        if self.channels:
            super().estimateStorage()
        else:
            self.maxDataPoints = 100_000
        limit = max(1, int(self.maxDataPoints))
        if self.time.size and self.time.max_size:
            limit = self.time.max_size  # Never thin or erase an existing history on a settings edit.
        self.time.max_size = limit
        for channel in self.channels:
            for name in ("values", "backgrounds"):
                buffer = getattr(channel, name, None)
                if buffer is not None:
                    buffer.max_size = limit

    def intervalChanged(self):
        super().intervalChanged()
        if self.controller is not None:
            self.controller.interval_s = max(.1, self.interval / 1000)

    def setOn(self, on=None):
        requested = self.isOn() if on is None else bool(on)
        if requested:
            self.initializeCommunication()
        else:
            self.closeCommunication()

    def initializeCommunication(self):
        # Do not interpolate across a disconnected interval. The gap must be
        # timestamped before the worker can deliver (and record) its first packet.
        if self.controller.initializeCommunication(before_start=lambda: self.appendData(nan=True)):
            self.startRecording()

    def startRecording(self):
        # Record once per fresh six-gauge reply, not by repeatedly sampling the
        # last displayed values in a second, independent polling thread.
        if self.initialized and not self.controller._stop.is_set() and not self.recording:
            self.clearPlot()
            self.intervalChanged()
            self.recording = True
            self.lagging = 0
            self.hasRecorded = True

    def recordReading(self, reading):
        """Append one packet using its PC reception time, even if Qt was delayed."""
        self.updateValues()  # Keep Explorer's virtual-channel equations available.
        for channel in self.getChannels():
            channel.appendValue(lenT=self.time.size)
        self.time.add(reading.received_at)
        if self.liveDisplayActive():
            self.liveDisplay.plot(apply=False)  # Already on the GUI thread.
        else:
            self.measureInterval()

    def closeCommunication(self):
        self.recording = False
        self.controller.closeCommunication()

    @property
    def initialized(self):
        # Pending initialization / unconfirmed port closure must not disappear
        # from Explorer's "communication still active" closing check.
        return self.controller.initialized or self.controller.initializing


class _MissedReading:
    """A lost packet: only its PC timestamp is recorded, with NaN pressures."""

    def __init__(self, received_at):
        self.received_at = received_at


class PressureChannel(Channel):
    INPUT = "Input"
    STATUS = "Status"
    GAUGE = "Gauge"

    def getDefaultChannel(self):
        channel = super().getDefaultChannel()
        channel[self.VALUE][Parameter.HEADER] = "P (mbar)"
        channel[self.INPUT] = parameterDict(
            value=1, parameterType=PARAMETERTYPE.INTCOMBO, items="1,2,3,4,5,6", fixedItems=True,
            attr="input", event=self.resetReading, toolTip="Physical TPG366 input (1–6).")
        channel[self.STATUS] = parameterDict(value="Disconnected", parameterType=PARAMETERTYPE.LABEL,
                                            indicator=True, restore=False, attr="status")
        channel[self.GAUGE] = parameterDict(value="", parameterType=PARAMETERTYPE.LABEL,
                                           indicator=True, restore=False, advanced=True, attr="gauge")
        channel[self.ENABLED][Parameter.ADVANCED] = False
        channel[self.ENABLED][Parameter.TOOLTIP] = "Include this input in acquisition. Does not switch the physical gauge."
        return channel

    def setDisplayedParameters(self):
        super().setDisplayedParameters()
        self.insertDisplayedParameter(self.INPUT, before=self.NAME)
        self.insertDisplayedParameter(self.STATUS, before=self.DISPLAY)
        self.displayedParameters.append(self.GAUGE)

    def tempParameters(self):
        return [*super().tempParameters(), self.STATUS, self.GAUGE]

    def resetReading(self):
        if not self.loading:
            self.value = np.nan
            self.status = "Waiting for data" if self.enabled else "Excluded"
            device = self.getDevice()
            gauges = device.controller.gauges
            self.gauge = gauges[int(self.input) - 1] if gauges else ""
            device._update_pressure_panel()

    def enabledChanged(self):
        super().enabledChanged()
        self.resetReading()
        if not self.loading:
            self.updateColor()

    def updateColor(self):
        """Use native OFF cells without changing the saved curve colours."""
        device = self.getDevice()
        controller = getattr(device, "controller", None)
        if (self.enabled and getattr(controller, "acquiring", False)
                and getattr(device, "onAction", None) is not None and device.isOn()):
            return super().updateColor()
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
        return QColor()


class PressureController(DeviceController):
    """One serial worker owns open/query/close; GUI state changes use queued Qt slots."""

    update = pyqtSignal(object)

    def __init__(self, controllerParent):
        super().__init__(controllerParent)
        self.update.connect(self._receive)
        self._stop = Event()
        self._stop.set()
        self._generation = 0
        self._worker = None
        self._retained_port = None
        self._awake = None
        self.gauges = ()
        self.interval_s = 1.0
        self.initialized = self.initializing = self.acquiring = False

    def initializeCommunication(self, before_start=None):
        if self.initialized or self.initializing or (self._worker is not None and self._worker.is_alive()):
            return False  # A finished worker may still have a close result queued for Qt.
        if self._retained_port is not None:
            self.controllerParent._set_state("Disconnect unconfirmed — retry OFF", True)
            return False
        parent = self.controllerParent
        port = _serial_port_name(parent.com)
        self._simulation = bool(getTestMode())
        if not port and not self._simulation:
            parent._set_state("Select the USB COM port in Settings", False)
            return False
        if any(int(channel.input) not in range(1, 7) for channel in parent.getChannels()):
            parent._set_state("Invalid input number — choose 1 to 6", False)
            return False
        self._generation += 1
        self._stop = Event()
        self.initializing = True
        self.interval_s = max(.1, parent.interval / 1000)
        self._invalidate("Connecting")
        parent._set_state("Connecting", True)
        baudrate = int(parent.baudrate)
        self.print("ON requested: simulation (no hardware)." if self._simulation else
                   f"ON requested: {port}, {baudrate} baud, 8N1, no flow control; Interval={self.interval_s * 1000:g} ms.")
        if not self._simulation:
            # Held until the worker confirms the port closed; a retained port keeps it.
            self._awake = _protocol.SystemAwakeRequest(f"ESIBD Explorer: TPG366 on {port} is connected")
            if not self._awake.hold(True) and sys.platform == "win32":
                self.print("Windows refused the keep-awake request: disable sleep in the power plan "
                           "while the gauges are read.", flag=PRINT.WARNING)
        self._worker = Thread(target=self._run, args=(self._generation, self._stop, port, baudrate, self._simulation),
                              name="TPG366 USB", daemon=True)
        self.initThread = self.acquisitionThread = self._worker
        if before_start is not None:
            before_start()
        self._worker.start()
        return True

    def _run(self, generation, stop, port_name, baudrate, simulation):
        port, error, close_error = None, "", ""
        phase = "opening USB port"
        try:
            if stop.is_set():
                raise _protocol.Cancelled
            if simulation:
                self.update.emit((generation, "ready", ("SIMULATION — no hardware", ("SIMULATION",) * 6)))
                while not stop.is_set():
                    reading = _protocol.Reading(tuple(10.0 ** -n for n in range(3, 9)), (0,) * 6, "mbar", time.time())
                    self.update.emit((generation, "sample", reading))
                    stop.wait(self.interval_s)
                return
            port = serial.Serial(port=port_name, baudrate=baudrate, bytesize=serial.EIGHTBITS,
                                 parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                 timeout=.05, write_timeout=1.0, xonxoff=False, rtscts=False, dsrdtr=False)
            link = _protocol.TPG366Link(port, stop)
            reported_naks = 0

            def report_naks():
                # Retransmissions after NAK follow the TPG 366 protocol; keep them visible.
                nonlocal reported_naks
                if link.nak_count > reported_naks:
                    self.update.emit((generation, "retransmitted", (link.nak_count - reported_naks, link.nak_count, phase)))
                    reported_naks = link.nak_count

            phase = "initialization"
            try:
                link.initialize()
            finally:
                report_naks()
            identification, unit = link.identification, link.unit
            self.update.emit((generation, "ready", (link.identification, link.gauges)))
            failures, resync = 0, False
            while not stop.is_set():
                started = time.monotonic()
                try:
                    if resync:
                        phase = "resynchronization"
                        if stop.wait(_RESYNC_PAUSE_S):
                            break
                        link.initialize()  # ETX, AYT synchronization and TID; read-only.
                        if link.identification != identification:
                            raise RuntimeError(f"Controller identification changed: {link.identification!r}.")
                        if link.unit != unit:  # never adopt a unit change silently
                            raise _protocol.ProtocolError("Pressure unit changed during resynchronization. Reconnect to continue.")
                        self.update.emit((generation, "resynchronized", link.gauges))
                        resync = False
                    phase = "pressure acquisition"
                    reading = link.read_pressures()
                except _protocol.ProtocolError as exc:
                    report_naks()
                    failures += 1
                    if failures >= _READ_FAILURE_LIMIT or not _transient_read_error(exc):
                        raise
                    resync = True
                    self.update.emit((generation, "missed", (
                        f"{port_name}, {baudrate} baud, 8N1; {phase}: {type(exc).__name__}: {exc}", time.time())))
                else:
                    report_naks()
                    if failures:
                        self.update.emit((generation, "recovered", failures))
                    failures = 0
                    self.update.emit((generation, "sample", reading))
                stop.wait(max(0, self.interval_s - (time.monotonic() - started)))
        except _protocol.Cancelled:
            pass
        except Exception as exc:
            context = "simulation" if simulation else f"{port_name}, {baudrate} baud, 8N1; {phase}"
            error = f"{context}: {type(exc).__name__}: {exc}"
            self.update.emit((generation, "error", error))
        finally:
            if port is not None:
                try:
                    port.close()
                    if port.is_open:
                        raise OSError("Serial port still reports open after close().")
                except Exception as exc:
                    close_error = f"{type(exc).__name__}: {exc}"
                else:
                    port = None
            self.update.emit((generation, "finished", (port, error, close_error)))

    def startAcquisition(self):
        if not self.initialized and not self.initializing:
            self.initializeCommunication()

    def stopAcquisition(self):
        self.closeCommunication()
        return True

    def closeCommunication(self):
        requested = not self._stop.is_set()
        self._stop.set()  # Cancel before waiting for any I/O. Only the worker closes.
        if requested:
            self.print("OFF requested: stop acquisition and close USB; gauges unchanged.")
        self.acquiring = False
        self.controllerParent.recording = False
        self._invalidate("Disconnected")
        if self._worker is not None and self._worker.is_alive():
            self.controllerParent._set_state("Disconnecting", True)
        elif self._retained_port is not None:
            self.controllerParent._set_state("Disconnecting", True)
            self._worker = Thread(target=self._retry_close, args=(self._generation, self._retained_port),
                                  name="TPG366 USB close", daemon=True)
            self._worker.start()
        elif self.initialized or self.initializing:
            # Do not claim closure or reconnect until the worker's queued
            # result transfers any unclosed port back to the GUI.
            self.controllerParent._set_state("Disconnecting", True)
        else:
            self.initialized = self.initializing = False
            self.controllerParent._set_state("Disconnected", False)

    def _retry_close(self, generation, port):
        error = ""
        try:
            port.close()
            if port.is_open:
                raise OSError("Serial port still reports open after close().")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        else:
            port = None
        self.update.emit((generation, "finished", (port, "", error)))

    def _invalidate(self, status):
        channels = self.controllerParent.getChannels()
        self.values = np.full(len(channels), np.nan)
        for channel in channels:
            if channel.real and channel.active:
                channel.value = np.nan
                channel.status = status if channel.enabled else "Excluded"

    @pyqtSlot(object)
    def _receive(self, event):
        generation, kind, payload = event
        if generation != self._generation:
            return
        parent = self.controllerParent
        if kind in {"ready", "sample", "error", "missed", "resynchronized", "recovered", "retransmitted"} and self._stop.is_set():
            return  # No queued sample/initialization can revive an OFF request.
        if kind == "ready":
            identification, gauges = payload
            self.gauges = gauges
            self.initializing = False
            self.initialized = self.acquiring = True
            parent.identification = identification
            for channel in parent.getChannels():
                channel.gauge = gauges[int(channel.input) - 1]
            parent._set_state("Simulation — no hardware" if self._simulation else "Acquiring", True)
            self.print(identification)
        elif kind == "sample":
            values = []
            for channel in parent.getChannels():
                index = int(channel.input) - 1
                value = payload.pressures[index] if channel.enabled and channel.real and channel.active else np.nan
                values.append(value)
                if channel.real and channel.active:
                    channel.value = value
                    channel.status = _protocol.STATUS[payload.statuses[index]] if channel.enabled else "Excluded"
                    if self.gauges:
                        channel.gauge = self.gauges[index]
            self.values = np.array(values)
            update_panel = getattr(parent, "_update_pressure_panel", None)
            if callable(update_panel):
                update_panel()
            if parent.recording:
                parent.recordReading(payload)
        elif kind == "missed":
            message, missed_at = payload
            self._invalidate("Read error — resynchronizing")
            if parent.recording:
                # Record the lost packet as a gap, never as the previous pressure.
                parent.recordReading(_MissedReading(missed_at))
            parent._set_state("Read error — resynchronizing", True)  # also refreshes the cards
            self.print(f"Pressure sample missed, resynchronizing: {message}", flag=PRINT.WARNING)
        elif kind == "resynchronized":
            self.gauges = payload
        elif kind == "retransmitted":
            new, total, phase = payload
            self.print(f"TPG366 answered NAK {new}x during {phase}; the command was retransmitted as the "
                       f"TPG 366 protocol specifies ({total} NAK(s) since ON).", flag=PRINT.WARNING)
        elif kind == "recovered":
            parent._set_state("Simulation — no hardware" if self._simulation else "Acquiring", True)
            self.print(f"TPG366 communication recovered after {payload} failed transaction(s).")
        elif kind == "error":
            self._invalidate("Communication error")
            parent.recording = False
            parent._set_state("Communication error — disconnecting", True)
            self.print(payload, flag=PRINT.ERROR)
        elif kind == "finished":
            self._stop.set()
            self._retained_port, error, close_error = payload
            if self._retained_port is None and self._awake is not None:
                self._awake.hold(False)
            self.initializing = self.acquiring = False
            self.initialized = self._retained_port is not None
            parent.recording = False
            self._invalidate("Communication error" if error else "Disconnected")
            if close_error:
                parent._set_state("Disconnect unconfirmed — retry OFF", True)
                self.print(f"Could not close the USB port: {close_error}", flag=PRINT.ERROR)
            else:
                parent._set_state("Communication error — disconnected" if error else "Disconnected", False)
