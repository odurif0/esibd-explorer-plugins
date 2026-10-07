"""Transmission: optimize ion transmission through the apertures of the beamline.

Explorer scan plugin with a three-step panel: describe the beamline once, choose
the target (total ion current or a mass selected by a quadrupole), choose the
stages, then Optimize. The devices stay owned by their own plugins: this plugin
only requests channel values through Explorer, on the Qt thread, and averages
the timestamped channel histories. Engine, simulated beamline and beamline
description live in ``_runtime/`` and are loaded privately. Amplitudes are in
volts like MScan; the m/z calibration belongs to MScan. Every run, quick sweep
and revert writes a JSON Lines log in <Explorer data path>/logs/transmission/.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import math
import os
from pathlib import Path
import platform
import sys
from threading import Event, Lock, Thread
import time
import traceback

import h5py
import numpy as np
from PyQt6.QtCore import QObject, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
                             QTableWidget, QVBoxLayout, QWidget)

from esibd.core import INOUT, PARAMETERTYPE, PRINT, TreeWidget, getTestMode, parameterDict, plotting
from esibd.plugins import Plugin, Scan, SettingsManager

_RUNTIME_FILES = ("__init__.py", "_engine.py", "_simulator.py", "_beamline.py", "_log.py")


def _load_runtime():
    # Explorer imports every root *.py file without sys.modules registration.
    # Keep the runtime below that discovery level and load it privately here.
    root = Path(__file__).resolve().parent / "_runtime"
    missing = [name for name in _RUNTIME_FILES if not (root / name).is_file()]
    if missing:
        raise ModuleNotFoundError(f"Missing bundled Transmission runtime file(s) in {root}: {', '.join(missing)}")
    name = f"_esibd_bundled_transmission_{root.parent.name}"
    try:
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, root / "__init__.py", submodule_search_locations=[str(root)])
            if spec is None or spec.loader is None:
                raise ModuleNotFoundError(f"Cannot load the bundled Transmission runtime: {root}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
        return tuple(importlib.import_module(f"{name}.{part}") for part in ("_engine", "_simulator", "_beamline", "_log"))
    except BaseException:
        for key in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
            sys.modules.pop(key, None)
        raise


_engine, _simulator, _beamline, _log = _load_runtime()

LEVEL_LABELS = (("fine", "Fine"), ("normal", "Normal"), ("wide", "Wide"))
SIMULATION_SETTINGS = dict(settle_s=0.5, average_s=1.0, seed=1)


def providePlugins():
    return [Transmission]


def _finite(value):
    try:
        return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _jsonable(value):
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def format_current(value):
    """A current with an SI prefix, e.g. 820 pA."""
    if not _finite(value):
        return "—"
    magnitude = abs(value)
    for factor, prefix in ((1e-15, "f"), (1e-12, "p"), (1e-9, "n"), (1e-6, "µ"), (1e-3, "m")):
        if magnitude < factor * 1000:
            return f"{value / factor:.3g} {prefix}A"
    return f"{value:.3g} A"


def _flag(owner, name):
    """A device-plugin state flag: only a real boolean counts."""
    value = getattr(owner, name, False)
    return isinstance(value, (bool, np.bool_)) and bool(value)


# Device-plugin states during which no setpoint may be written (the CGC plugins ignore it)
# and that Transmission never causes: they stop the run without any further command.
DEVICE_BUSY = {"initializing": "initialization", "transitioning": "ON/OFF transition", "ramping": "ramp at ON/OFF",
               "_hv_config_loading": "HV configuration loading"}


def device_on(device):
    """ON for a device with an ON/OFF button; connected for any other device."""
    if getattr(device, "useOnOffLogic", False):
        return bool(device.isOn())
    initialized = getattr(device, "initialized", None)
    return bool(initialized) if isinstance(initialized, (bool, np.bool_)) else bool(device.isOn())


def device_busy(device):
    """What the device plugin is busy with (initialization, ON/OFF transition…), or ''."""
    controller = getattr(device, "controller", None)
    return next((what for flag, what in DEVICE_BUSY.items() if _flag(controller, flag)), "")


def is_esi_channel(channel):
    """ESI source channels: HV with unconfirmed discharge, and heater temperatures that start heating."""
    try:
        device = channel.getDevice()
    except Exception:  # noqa: BLE001 - a channel without a device is not an ESI channel
        device = None
    return getattr(device, "name", "") == "ESI" or callable(getattr(channel, "is_heat_channel", None))


def read_mscan(path):
    """Amplitude (V), current (A), detector name and rail channels of a saved MScan file."""
    with h5py.File(path, "r") as handle:
        if "MScan" not in handle:
            raise ValueError(f"{Path(path).name} is not an MScan file")
        group = handle["MScan"]
        amplitude = np.asarray(group["Input Channels"]["Amplitude"][:], float)
        outputs = group["Output Channels"]
        detector = next(iter(outputs))
        current = np.asarray(outputs[detector][:], float)
        rails = []
        validation = group.get("Validation")
        if validation is not None and "setup" in validation.attrs:
            setup = json.loads(validation.attrs["setup"])
            rails = [rail["channel"] for rail in setup.get("rails", [])]
    return amplitude, current, detector, rails


class _Call:
    # Plain container: Explorer's dynamicImport omits sys.modules registration,
    # which dataclasses need to resolve postponed annotations.
    def __init__(self, action):
        self.action = action
        self.done = Event()
        self.expired = Event()
        self.result = None
        self.error = None


class _GuiBridge(QObject):
    request = pyqtSignal(object)
    status = pyqtSignal(str)
    refresh = pyqtSignal()
    watch = pyqtSignal(object)
    unwatch = pyqtSignal()
    plot = pyqtSignal(bool)
    measuring = pyqtSignal(bool)

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        self.request.connect(self.execute)
        self.status.connect(self.set_status)
        self.refresh.connect(plugin.refresh_panel)
        self.watch.connect(plugin._watch)
        self.unwatch.connect(plugin._unwatch)
        self.plot.connect(plugin._plot_requested)
        self.measuring.connect(plugin.set_measuring)

    @pyqtSlot(object)
    def execute(self, call):
        try:
            if call.expired.is_set():
                raise _engine.InstrumentError("Explorer request expired before it ran")
            call.result = call.action()
        except Exception as exc:  # noqa: BLE001 - forwarded to the worker
            call.error = exc
        finally:
            call.done.set()

    @pyqtSlot(str)
    def set_status(self, text):
        self.plugin.show_progress(text)


class ExplorerInstrument(_engine.Instrument):
    """The optimizer's view of Explorer channels. Every channel access runs on the Qt thread.

    Driven channels are written through ``Channel.value``, like a user edit, so the
    owning plugin keeps its ramps, limits and OFF logic. Measured values come from
    the channel histories: for devices with monitors (DMMR, PSU) Explorer records
    the monitor, for the others the value, each with the device timestamp.
    """

    GUI_TIMEOUT_S = 10.0
    SLOW_GUI_S = 0.5  # Explorer calls waiting longer than this for the Qt thread are logged.
    # Explorer timestamps samples when its Qt thread records them: after a freeze, the queued
    # recordings run in a burst, much closer together than the device interval. Such a burst
    # is one observation, not several (DMMR, TPG366 and ESI already record each reading once).
    BURST_FRACTION = 0.25
    # Device-plugin states during which no setpoint may be written (the CGC plugins ignore it)
    # and that Transmission never causes: they stop the run without any further command.
    DEVICE_BUSY = DEVICE_BUSY
    # A PSU setpoint is applied by its own worker: wait for it before the next setpoint (as MScan).
    APPLYING = ("_manual_apply_active", "_manual_apply_worker_running")

    def __init__(self, plugin, config, cancel):
        self.plugin, self.config, self.cancel = plugin, config, cancel
        self.bridge = plugin._bridge
        manager = plugin.pluginManager.DeviceManager
        self.driven = {name: self._resolve(manager, name) for name in config.driven_channels}
        for name, channel in self.driven.items():
            if is_esi_channel(channel):
                raise _engine.ConfigError(f"{name}: ESI channels are not driven by Transmission (HV discharge not "
                                          "confirmed; a temperature setpoint starts the heater). They can be measured.")
        self.measured = {name: self._resolve(manager, name) for name in (*config.current_channels, *config.pressure_channels)}
        self.expected = {}
        self.log = None
        self._latency = []
        self._window_details = {}

    def _record(self, event, **data):
        if self.log is not None:
            try:
                self.log(event, data)
            except Exception:  # noqa: BLE001, S110 - logging never stops the optimizer
                pass

    @staticmethod
    def _resolve(manager, name):
        matches = [c for c in manager.channels() if c.name.strip().lower() == name.strip().lower()]
        if not matches:
            raise _engine.ConfigError(f"Channel {name!r} not found in Explorer")
        if len(matches) > 1:
            raise _engine.ConfigError(f"Channel name {name!r} is ambiguous in Explorer")
        return matches[0]

    def _gui(self, action):
        timing = {}

        def timed():
            timing["start"] = time.perf_counter()
            try:
                return action()
            finally:
                timing["end"] = time.perf_counter()
        call = _Call(timed)
        posted = time.perf_counter()
        if QThread.currentThread() == self.bridge.thread():
            self.bridge.execute(call)
        else:
            self.bridge.request.emit(call)
            if not call.done.wait(self.GUI_TIMEOUT_S):
                call.expired.set()  # A delayed Qt callback must not write later.
                self._record("gui_timeout", waited_s=self.GUI_TIMEOUT_S, action=getattr(action, "__qualname__", ""))
                raise _engine.InstrumentError(f"Explorer did not respond within {self.GUI_TIMEOUT_S:g} s")
        if "start" in timing:
            latency, duration = timing["start"] - posted, timing["end"] - timing["start"]
            self._latency.append((latency, duration))
            if latency > self.SLOW_GUI_S:
                self._record("gui_slow", latency_s=latency, duration_s=duration, action=getattr(action, "__qualname__", ""))
        if call.error is not None:
            raise call.error
        return call.result

    def measuring(self, active):
        if active:
            # Runs after anything already queued on the Qt thread, a redraw included: the window
            # opens on an idle GUI, and the plugin postpones its redraws until it closes.
            self._gui(lambda: self.plugin.set_measuring(True))
        else:
            self.bridge.measuring.emit(False)

    @staticmethod
    def _nominal_interval(device, times):
        interval = getattr(device, "interval", None)
        if _finite(interval) and float(interval) > 0:
            return float(interval) / 1000.0
        steps = np.diff(times[-200:])
        steps = steps[steps > 0]
        return float(np.median(steps)) if len(steps) else 0.0

    def diagnostics(self):
        """Sample timing of the last averaging window and Qt-thread latency since the previous call."""
        latency, self._latency = self._latency, []
        result = dict(window=self._window_details)
        if latency:
            waits = [wait for wait, _ in latency]
            result["gui"] = dict(calls=len(latency), max_latency_s=max(waits), mean_latency_s=sum(waits) / len(waits),
                                 max_duration_s=max(duration for _, duration in latency))
        return result

    def snapshot(self):
        """State of every channel the run uses, for the log header."""
        def describe(channel):
            device = channel.getDevice()
            info = dict(device=getattr(device, "name", ""), on=device.isOn(), recording=getattr(device, "recording", None),
                        interval_ms=getattr(device, "interval", None))
            for key in ("value", "monitor", "min", "max", "unit", "useMonitors", "enabled", "active", "real"):
                value = getattr(channel, key, None)
                info[key] = value if value is None or isinstance(value, (bool, int, float, str)) else repr(value)
            return info
        return self._gui(lambda: dict(driven={name: describe(c) for name, c in self.driven.items()},
                                      measured={name: describe(c) for name, c in self.measured.items()}))

    # ---- Instrument
    def now(self):
        return time.time()

    def wait(self, seconds, cancellable=True):
        seconds = max(0.0, seconds)
        if not cancellable:
            time.sleep(seconds)
        elif self.cancel.wait(seconds):
            raise _engine.Stopped("Stopped by the operator")

    def setpoints(self, channels):
        return self._gui(lambda: {c: float(self.driven[c].value) for c in channels})

    def limits(self, channel):
        def read():
            c = self.driven[channel]
            lo, hi = getattr(c, "min", None), getattr(c, "max", None)
            if not (_finite(lo) and _finite(hi)) or float(lo) >= float(hi):
                raise _engine.InstrumentError(f"{channel}: define Min and Max in {c.getDevice().name}")
            lo, hi = float(lo), float(hi)
            if hasattr(c, "hardware_voltage_limit"):
                # PSU: unsigned magnitudes, never above the hardware limit (as MScan).
                ceiling = getattr(c, "hardware_voltage_limit", None)
                if not (_finite(ceiling) and float(ceiling) > 0):
                    raise _engine.InstrumentError(f"{channel}: hardware voltage limit not read yet from {c.getDevice().name}")
                lo, hi = max(lo, 0.0), min(hi, float(ceiling))
                if lo >= hi:
                    raise _engine.InstrumentError(f"{channel}: no admissible voltage range in {c.getDevice().name}")
            return lo, hi
        return self._gui(read)

    def _applying(self, names):
        """Driven channels whose device plugin is still applying a previous setpoint."""
        busy = []
        for name in names:
            controller = getattr(self.driven[name].getDevice(), "controller", None)
            if any(_flag(controller, flag) for flag in self.APPLYING):
                busy.append(name)
        return busy

    def command(self, values):
        deadline = time.monotonic() + self.config.settle_timeout_s
        while True:
            busy = self._gui(lambda: self._applying(values))
            if not busy:
                break
            if time.monotonic() >= deadline:
                raise _engine.InstrumentError(f"{', '.join(busy)}: the device plugin is still applying the previous "
                                              f"setpoint after {self.config.settle_timeout_s:g} s")
            time.sleep(self.config.poll_s)  # Bounded and never cancelled: a restore must be able to finish.

        def write():
            self._check()
            for name, value in values.items():
                channel = self.driven[name]
                channel.value = float(value)
                # Keep what the channel stored (possibly rounded) to detect later outside edits.
                self.expected[name] = float(channel.value)
        self._gui(write)

    def readbacks(self, channels):
        def read():
            result = {}
            for name in channels:
                channel = self.driven[name]
                monitor = getattr(channel, "monitor", None) if getattr(channel, "useMonitors", False) else None
                result[name] = float(monitor) if _finite(monitor) else math.nan
            return result
        return self._gui(read)

    @staticmethod
    def _measured_value(channel):
        value = channel.monitor if getattr(channel, "useMonitors", False) else channel.value
        return float(value) if _finite(value) else math.nan

    def latest(self, channels):
        return self._gui(lambda: {c: self._measured_value(self.measured[c]) for c in channels})

    def window(self, channels, start, end):
        def read():
            result, details = {}, {}
            for name in channels:
                channel = self.measured[name]
                device = channel.getDevice()
                times = np.asarray(device.time.get(), dtype=float)
                values = np.asarray(channel.getValues(subtractBackground=False), dtype=float)
                n = min(len(times), len(values))
                times, values = times[:n], values[:n]
                if not n or times[-1] < end:
                    return None  # Wait for a sample closing the window.
                lo, hi = np.searchsorted(times, (start, end), side="right")
                inside, chunk = times[lo:hi], values[lo:hi]
                finite = np.isfinite(chunk)
                t_valid, v_valid = inside[finite], chunk[finite]
                nominal = self._nominal_interval(device, times)
                if len(t_valid) > 1 and nominal > 0:
                    # Keep the last (freshest) sample of each burst.
                    keep = np.append(np.diff(t_valid) >= self.BURST_FRACTION * nominal, True)
                else:
                    keep = np.ones(len(t_valid), dtype=bool)
                used = v_valid[keep]
                count = len(used)
                mean = float(np.mean(used)) if count else math.nan
                sem = float(np.std(used, ddof=1) / math.sqrt(count)) if count > 1 else math.nan
                result[name] = (mean, sem, count)
                # Largest interval without a sample, edges included: an acquisition or GUI stall shows here.
                details[name] = dict(samples=int(hi - lo), finite=int(len(v_valid)), used=count,
                                     collapsed=int(len(v_valid) - count),
                                     repeats=int(np.sum(used[1:] == used[:-1])) if count > 1 else 0,
                                     nominal_interval_s=nominal,
                                     max_gap_s=float(np.max(np.diff(np.concatenate(([start], inside, [end]))))),
                                     closed_by=float(times[hi]) if hi < n else None)
            self._window_details = details
            return result
        return self._gui(read)

    def _check(self):
        for name, channel in self.driven.items():
            device = channel.getDevice()
            if not device_on(device):
                raise _engine.InstrumentError(f"{device.name} is OFF (driven channel {name})")
            if not (channel.enabled and channel.active and channel.real):
                raise _engine.InstrumentError(f"{name}: enable it as an active real channel in {device.name}")
            controller = getattr(device, "controller", None)
            for flag, what in self.DEVICE_BUSY.items():
                if _flag(controller, flag):
                    raise _engine.InstrumentError(f"{device.name}: {what} in progress (driven channel {name})")
            current, ilim = getattr(channel, "current_readback", None), getattr(channel, "current_limit_readback", None)
            if _finite(current) and _finite(ilim) and float(ilim) > 0 and float(current) >= float(ilim):
                # A rail at its current limit: short, discharge or overload. As MScan, no HV OFF from here.
                raise _engine.InstrumentError(f"{name}: output current {float(current) * 1000:g} mA reached Ilim "
                                              f"{float(ilim) * 1000:g} mA in {device.name}")
            expected = self.expected.get(name)
            if expected is not None and not math.isclose(float(channel.value), expected, rel_tol=1e-9, abs_tol=1e-9):
                raise _engine.InstrumentError(f"{name}: setpoint changed outside the optimizer ({expected:g} -> {float(channel.value):g})")
        for name, channel in self.measured.items():
            device = channel.getDevice()
            if not device_on(device):
                raise _engine.InstrumentError(f"{device.name} is OFF (measured channel {name})")
            if not getattr(device, "recording", True):
                raise _engine.InstrumentError(f"{device.name}: start recording to measure {name}")
            if not channel.enabled:
                raise _engine.InstrumentError(f"{name}: enable the channel in {device.name}")
            if not hasattr(device, "time") or not hasattr(channel, "values"):
                raise _engine.InstrumentError(f"{name}: a timestamped channel history is required")

    def check(self):
        self._gui(self._check)


# ============================================================================ dialogs

class _ConfigEditor(QDialog):
    """Advanced mode: the full TOML configuration, checked against the rules and Explorer."""

    def __init__(self, plugin, path, text=None, use=False):
        super().__init__()
        self.plugin, self.path = plugin, Path(path)
        self.setWindowTitle("Transmission — advanced configuration")
        self.resize(820, 780)
        layout = QVBoxLayout(self)
        self.use = QCheckBox("Use this configuration instead of the simple settings")
        self.use.setChecked(use)
        layout.addWidget(self.use)
        layout.addWidget(QLabel(str(self.path)))
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setPlainText(text if text is not None else
                                 self.path.read_text(encoding="utf-8") if self.path.is_file() else plugin.template_text())
        layout.addWidget(self.editor)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        actions = (("Check", self.check), ("From the simple settings", lambda: self.editor.setPlainText(plugin.generated_text())),
                   ("Import…", self.import_file), ("Export…", self.export_file))
        for label, slot in actions:
            button = QPushButton(label)
            button.clicked.connect(slot)
            buttons.addButton(button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def check(self):
        try:
            config = _engine.parse_config(self.editor.toPlainText())
        except _engine.ConfigError as exc:
            self.message.setText(f"Not valid: {exc}")
            return None
        missing = self.plugin.missing_channels(config)
        knobs = sum(len(stage.knobs) for stage in config.stages)
        text = f"Valid: {len(config.stages)} stage(s), {knobs} setting(s)."
        if missing:
            text += " Not found in Explorer: " + ", ".join(missing) + "."
        self.message.setText(text)
        return config

    def import_file(self):
        name, _ = QFileDialog.getOpenFileName(self, "Import a Transmission configuration", str(self.path.parent),
                                              "TOML (*.toml);;All files (*)")
        if name:
            self.editor.setPlainText(Path(name).read_text(encoding="utf-8"))
            self.check()

    def export_file(self):
        name, _ = QFileDialog.getSaveFileName(self, "Export the Transmission configuration", str(self.path.parent), "TOML (*.toml)")
        if name:
            Path(name).write_text(self.editor.toPlainText(), encoding="utf-8")

    def accept(self):
        if self.check() is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.editor.toPlainText(), encoding="utf-8")
        super().accept()


class _BeamlineDialog(QDialog):
    """Map each element setting, aperture current and gauge to Explorer channels."""

    NONE = "—"

    def __init__(self, plugin, beamline):
        super().__init__()
        self.plugin = plugin
        self.setWindowTitle("Transmission — beamline")
        self.resize(900, 760)
        beamline = _beamline.normalize(beamline)
        driven, currents, pressures = plugin.channel_lists()
        self.device_of = dict(beamline["devices"])
        for channel in plugin.pluginManager.DeviceManager.channels():
            try:
                self.device_of[channel.name] = channel.getDevice().name
            except Exception:  # noqa: BLE001 - a channel without a device: nothing to remember
                pass
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Settings</b> — the channel(s) of each element. An RF amplitude driven by two rails "
                                "(MScan's Vpos and Vneg) uses both, with the same amplitude A."))
        self.knobs = QTableWidget(len(_beamline.KNOBS), 5)
        self.knobs.setHorizontalHeaderLabels(["Element", "Setting", "Channel", "Second channel", "Gain"])
        self.knob_widgets = {}
        for row, (key, element, label, _) in enumerate(_beamline.KNOBS):
            mapped = list(beamline["knobs"].get(key, {}).items())
            first = self._combo(driven, mapped[0][0] if mapped else None)
            second = self._combo(driven, mapped[1][0] if len(mapped) > 1 else None)
            gain = QComboBox()
            gain.addItems(["+1", "−1"])
            gain.setCurrentIndex(1 if len(mapped) > 1 and mapped[1][1] < 0 else 0)
            for column, text in enumerate((element, label)):
                self.knobs.setCellWidget(row, column, QLabel(text))
            self.knobs.setCellWidget(row, 2, first)
            self.knobs.setCellWidget(row, 3, second)
            self.knobs.setCellWidget(row, 4, gain)
            self.knob_widgets[key] = (first, second, gain)
        self.knobs.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.knobs, 3)
        layout.addWidget(QLabel("<b>Currents</b> — the picoammeter channel of each aperture. "
                                "Missing apertures are skipped; the last one measured ends the beamline."))
        self.collectors = QTableWidget(len(_beamline.COLLECTORS), 2)
        self.collectors.setHorizontalHeaderLabels(["Aperture", "Current channel"])
        self.collector_widgets = {}
        for row, (key, label) in enumerate(_beamline.COLLECTORS):
            self.collectors.setCellWidget(row, 0, QLabel(label))
            combo = self._combo(currents, beamline["collectors"].get(key))
            self.collectors.setCellWidget(row, 1, combo)
            self.collector_widgets[key] = combo
        self.collectors.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.collectors, 2)
        layout.addWidget(QLabel("<b>Pressure limits</b> (optional soft interlock: the optimization stops and returns to "
                                "the stage start; the protective interlock belongs in the hardware)."))
        names = list(dict.fromkeys([*pressures, *beamline["gauges"]]))
        self.gauges = QTableWidget(len(names), 4)
        self.gauges.setHorizontalHeaderLabels(["Watch", "Gauge channel", "Minimum (mbar)", "Maximum (mbar)"])
        self.gauge_widgets = {}
        for row, name in enumerate(names):
            watch = QCheckBox()
            limits = beamline["gauges"].get(name)
            watch.setChecked(limits is not None)
            low, high = QLineEdit(f"{limits[0]:g}" if limits else "0"), QLineEdit(f"{limits[1]:g}" if limits else "")
            self.gauges.setCellWidget(row, 0, watch)
            self.gauges.setCellWidget(row, 1, QLabel(name))
            self.gauges.setCellWidget(row, 2, low)
            self.gauges.setCellWidget(row, 3, high)
            self.gauge_widgets[name] = (watch, low, high)
        self.gauges.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.gauges, 1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Collected ions read as"))
        self.polarity = QComboBox()
        self.polarity.addItems(["positive currents", "negative currents"])
        self.polarity.setCurrentIndex(0 if beamline["polarity"] == 1 else 1)
        row.addWidget(self.polarity)
        row.addStretch()
        layout.addLayout(row)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.result_beamline = beamline

    def _combo(self, names, current):
        combo = QComboBox()
        items = [self.NONE, *names]
        if current and current not in names:
            items.append(current)  # Keep a mapped channel even if its device is not loaded now.
        combo.addItems(items)
        combo.setCurrentText(current if current else self.NONE)
        return combo

    def beamline(self):
        knobs = {}
        for key, (first, second, gain) in self.knob_widgets.items():
            channels = {}
            if first.currentText() != self.NONE:
                channels[first.currentText()] = 1.0
            if second.currentText() != self.NONE and second.currentText() not in channels:
                channels[second.currentText()] = -1.0 if gain.currentIndex() == 1 else 1.0
            if channels:
                knobs[key] = channels
        collectors = {key: combo.currentText() for key, combo in self.collector_widgets.items() if combo.currentText() != self.NONE}
        gauges = {}
        for name, (watch, low, high) in self.gauge_widgets.items():
            if watch.isChecked():
                try:
                    gauges[name] = [float(low.text()), float(high.text())]
                except ValueError:
                    raise ValueError(f"Gauge {name}: enter the minimum and maximum in mbar") from None
        return _beamline.normalize(dict(polarity=1 if self.polarity.currentIndex() == 0 else -1,
                                        knobs=knobs, collectors=collectors, gauges=gauges, devices=self.device_of))

    def accept(self):
        try:
            self.result_beamline = self.beamline()
        except ValueError as exc:
            self.message.setText(f"<b>{exc}</b>")
            return
        super().accept()


class _SpectrumSignals(QObject):
    point = pyqtSignal(float, float)
    done = pyqtSignal(object)


class _SpectrumDialog(QDialog):
    """Pick the peak to optimize on a spectrum: a saved MScan file or a quick sweep of the filter."""

    def __init__(self, plugin, filter_key, center=None):
        super().__init__()
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
        from matplotlib.figure import Figure
        self.plugin = plugin
        self.setWindowTitle("Transmission — pick the peak")
        self.resize(900, 640)
        self.amplitude, self.current = [], []
        self.selection = None
        self.source = ""
        self.signals = _SpectrumSignals()
        self.signals.point.connect(self._add_point)
        self.signals.done.connect(self._sweep_done)
        self.cancel = Event()
        self.worker = None
        self.outcome = None
        layout = QVBoxLayout(self)
        source = QHBoxLayout()
        open_button = QPushButton("Open an MScan file…")
        open_button.clicked.connect(self.open_mscan)
        source.addWidget(open_button)
        source.addWidget(QLabel("  or quick sweep of"))
        self.filter = QComboBox()
        for key, label in _beamline.filters(plugin.active_beamline()):
            self.filter.addItem(label, key)
        index = self.filter.findData(filter_key)
        self.filter.setCurrentIndex(max(index, 0))
        source.addWidget(self.filter)
        self.start, self.stop = QDoubleSpinBox(), QDoubleSpinBox()
        for box, value in ((self.start, (center or 300.0) * 0.5), (self.stop, (center or 300.0) * 1.5)):
            box.setRange(0.0, 100000.0)
            box.setDecimals(1)
            box.setSuffix(" V")
            box.setValue(value)
        self.points = QSpinBox()
        self.points.setRange(5, 1001)
        self.points.setValue(61)
        for label, widget in (("from", self.start), ("to", self.stop), ("points", self.points)):
            source.addWidget(QLabel(label))
            source.addWidget(widget)
        self.sweep_button = QPushButton("Sweep")
        self.sweep_button.clicked.connect(self.sweep)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.cancel.set)
        self.stop_button.setEnabled(False)
        source.addWidget(self.sweep_button)
        source.addWidget(self.stop_button)
        source.addStretch()
        layout.addLayout(source)
        self.figure = Figure(constrained_layout=True)
        self.axes = self.figure.add_subplot(111)
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.mpl_connect("button_press_event", self._clicked)
        layout.addWidget(self.canvas, 1)
        self.info = QLabel("Load or sweep a spectrum, then click on the peak to optimize.")
        self.info.setWordWrap(True)
        layout.addWidget(self.info)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setEnabled(False)
        layout.addWidget(buttons)
        self._draw()

    def filter_key(self):
        return self.filter.currentData()

    def set_spectrum(self, amplitude, current, title=""):
        self.amplitude, self.current = list(map(float, amplitude)), list(map(float, current))
        self.selection = None
        self.ok.setEnabled(False)
        self._draw(title)

    def open_mscan(self):
        folder = str(self.plugin.pluginManager.Settings.getFullSessionPath())
        name, _ = QFileDialog.getOpenFileName(self, "Open an MScan spectrum", folder, "MScan (*mscan*.h5);;HDF5 (*.h5)")
        if name:
            self.load_mscan(name)

    def load_mscan(self, path):
        try:
            amplitude, current, detector, rails = read_mscan(path)
        except (OSError, KeyError, ValueError, StopIteration) as exc:
            self.info.setText(f"Cannot read {Path(path).name}: {exc}")
            self.plugin.session("mscan_unreadable", path=str(path), error=str(exc))
            return
        beamline = self.plugin.active_beamline()
        matched = [key for key, _ in _beamline.filters(beamline)
                   if rails and set(beamline["knobs"].get(f"{key}_rf", {})) == set(rails)]
        if matched:
            self.filter.setCurrentIndex(self.filter.findData(matched[0]))
        self.set_spectrum(amplitude, beamline["polarity"] * current, f"{Path(path).name} — {detector}")
        self.source = str(path)
        self.plugin.session("mscan_spectrum", path=str(path), detector=detector, rails=rails, points=len(amplitude),
                            filter=self.filter_key(), recognised=bool(matched))
        note = f" Filter {self.filter.currentText()} recognised from the MScan rails." if matched else (
            " Check that the filter shown is the quadrupole MScan swept." if rails else "")
        self.info.setText("Click on the peak to optimize." + note)

    def sweep(self):
        if self.filter_key() is None:
            self.info.setText("Map the RF amplitude of a quadrupole and a current after it first (Configure…).")
            return
        if self.plugin._busy():
            self.info.setText("Wait until the optimization, revert, devices or previous sweep have finished.")
            return
        key = self.filter_key()
        if not self.plugin._simulated():
            beamline = self.plugin.active_beamline()
            needed = (list(beamline["knobs"].get(f"{key}_rf", {})),
                      [*_beamline.filter_measure(beamline, key), *beamline["gauges"]])
            report = self.plugin.device_report(*needed)
            if any(entry["state"] != "ready" for entry in report):
                self.plugin.start_devices(report, then=self.sweep, action="sweep", on_progress=self.info.setText,
                                          needed=needed)
                return
        self.plugin._sweeping = True  # Before the worker starts: no optimization can start meanwhile.
        self.plugin.refresh_panel()
        amplitudes = np.linspace(self.start.value(), self.stop.value(), self.points.value())
        self.set_spectrum([], [], f"Quick sweep of {self.filter.currentText()}")
        self.source = "quick sweep"
        self.cancel.clear()
        self.sweep_button.setEnabled(False)
        self.stop_button.setEnabled(True)

        def work():
            outcome = self.plugin.quick_sweep(key, amplitudes, self.signals.point.emit, self.cancel)
            self.signals.done.emit(outcome)
        self.worker = Thread(target=work, name="Transmission sweep", daemon=True)
        self.worker.start()

    @pyqtSlot(float, float)
    def _add_point(self, amplitude, current):
        self.amplitude.append(amplitude)
        self.current.append(current)
        self._draw(self.axes.get_title())

    @pyqtSlot(object)
    def _sweep_done(self, outcome):
        self.outcome = outcome
        self.sweep_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        if outcome["status"] == "completed":
            self.info.setText("Click on the peak to optimize. The filter is back at its previous amplitude.")
        else:
            self.info.setText(f"Sweep {outcome['status']}: {outcome['reason']}")

    def select_near(self, amplitude):
        try:
            center, width = _engine.pick_peak(self.amplitude, self.current, amplitude)
        except ValueError as exc:
            self.info.setText(str(exc))
            return
        self.selection = (center, width)
        self.ok.setEnabled(True)
        self.info.setText(f"Peak at {center:.1f} V, half-width {width:.1f} V. OK to optimize this mass.")
        self._draw(self.axes.get_title())

    def _clicked(self, event):
        if event.inaxes is self.axes and event.xdata is not None and len(self.amplitude) >= 3:
            self.select_near(event.xdata)

    def _draw(self, title=""):
        self.axes.clear()
        if self.amplitude:
            self.axes.plot(self.amplitude, self.current, marker=".", linewidth=1)
        if self.selection:
            center, width = self.selection
            self.axes.axvline(center, color="tab:red")
            self.axes.axvspan(center - width, center + width, color="tab:red", alpha=0.15)
        self.axes.set_xlabel("Filter amplitude A (V)")
        self.axes.set_ylabel("Current after the filter (A)")
        self.axes.set_title(title, fontsize=9)
        self.canvas.draw_idle()

    def reject(self):
        self.cancel.set()
        super().reject()


# ============================================================================ panel

class _Panel(QWidget):
    """The three steps: beamline, target, stages; then Optimize."""

    def __init__(self, plugin):
        super().__init__()
        self.plugin = plugin
        layout = QVBoxLayout(self)
        bold = QFont()
        bold.setBold(True)

        beam = QGroupBox("1   Beamline")
        beam_layout = QVBoxLayout(beam)
        row = QHBoxLayout()
        self.beamline_label = QLabel()
        self.beamline_label.setWordWrap(True)
        row.addWidget(self.beamline_label, 1)
        self.configure = QPushButton("Configure…")
        row.addWidget(self.configure)
        beam_layout.addLayout(row)
        row = QHBoxLayout()
        self.devices_label = QLabel()
        self.devices_label.setWordWrap(True)
        row.addWidget(self.devices_label, 1)
        self.start_devices = QPushButton("Turn ON…")
        self.start_devices.setToolTip("Turn ON the devices this optimization needs and start their recording, "
                                      "after confirmation, as with each device's own power button.")
        row.addWidget(self.start_devices)
        beam_layout.addLayout(row)
        layout.addWidget(beam)

        target = QGroupBox("2   Target")
        grid = QGridLayout(target)
        self.total = QRadioButton("Total ion current")
        self.mass = QRadioButton("Selected mass")
        self.modes = QButtonGroup(self)
        self.modes.addButton(self.total)
        self.modes.addButton(self.mass)
        grid.addWidget(self.total, 0, 0, 1, 3)
        grid.addWidget(self.mass, 1, 0, 1, 3)
        grid.addWidget(QLabel("Filter"), 2, 0)
        self.filter = QComboBox()
        grid.addWidget(self.filter, 2, 1)
        grid.addWidget(QLabel("Peak"), 2, 2)
        self.center = QDoubleSpinBox()
        self.width = QDoubleSpinBox()
        for box in (self.center, self.width):
            box.setRange(0.0, 100000.0)
            box.setDecimals(1)
            box.setSuffix(" V")
        grid.addWidget(self.center, 2, 3)
        grid.addWidget(QLabel("±"), 2, 4)
        grid.addWidget(self.width, 2, 5)
        self.pick = QPushButton("Pick on spectrum…")
        grid.addWidget(self.pick, 2, 6)
        grid.setColumnStretch(7, 1)  # Keep the fields compact in a wide dock.
        layout.addWidget(target)

        stages = QGroupBox("3   Stages")
        stage_layout = QVBoxLayout(stages)
        self.stage_row = QHBoxLayout()
        self.stage_row.addStretch()
        stage_layout.addLayout(self.stage_row)
        level = QHBoxLayout()
        level.addWidget(QLabel("Exploration"))
        self.levels = QButtonGroup(self)
        self.level_buttons = {}
        for key, label in LEVEL_LABELS:
            button = QRadioButton(label)
            self.levels.addButton(button)
            self.level_buttons[key] = button
            level.addWidget(button)
        level.addStretch()
        stage_layout.addLayout(level)
        layout.addWidget(stages)
        self.stage_boxes = {}

        actions = QHBoxLayout()
        self.optimize = QPushButton("▶  Optimize")
        self.optimize.setFont(bold)
        self.optimize.setMinimumHeight(32)
        self.stop = QPushButton("■  Stop")
        self.stop.setMinimumHeight(32)
        self.simulation = QCheckBox("Simulation")
        self.simulation.setToolTip("Run on the built-in simulated beamline. No device is touched.")
        self.advanced = QPushButton("Advanced…")
        self.logs = QPushButton("Logs…")
        self.logs.setToolTip("Open the log folder (<Explorer data path>/logs/transmission): one file per run, "
                             "quick sweep and revert, for troubleshooting.")
        for widget in (self.optimize, self.stop, self.simulation):
            actions.addWidget(widget)
        actions.addStretch()
        actions.addWidget(self.advanced)
        actions.addWidget(self.logs)
        layout.addLayout(actions)
        self.progress = QLabel("Ready.")
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.setFont(bold)
        layout.addWidget(self.result)
        decision = QHBoxLayout()
        self.keep = QPushButton("Keep")
        self.keep.setToolTip("Keep the optimized settings.")
        self.revert = QPushButton("Revert")
        self.revert.setToolTip("Return every channel changed by the last run to its value before it, step by step.")
        decision.addWidget(self.keep)
        decision.addWidget(self.revert)
        decision.addStretch()
        layout.addLayout(decision)
        layout.addStretch()

    def set_stages(self, stages, skip):
        for box in self.stage_boxes.values():
            self.stage_row.removeWidget(box)
            box.deleteLater()
        self.stage_boxes = {}
        for key, label, *_ in stages:
            box = QCheckBox(label)
            box.setChecked(key not in skip)
            box.toggled.connect(self.plugin.panel_changed)
            self.stage_row.insertWidget(self.stage_row.count() - 1, box)  # Before the closing stretch.
            self.stage_boxes[key] = box


# ============================================================================ plugin

class Transmission(Scan):
    """Optimize the ion transmission through the beamline, stage by stage.

    Each stage maximizes the current measured after its aperture (or, for a
    selected mass, the height of its peak behind the filter). A point counts only
    if the flux reaching the aperture is still present, so a low aperture current
    caused by a lost beam is never taken for transmission. Moves are split into
    small steps that wait for the readbacks; the best point is adopted only after
    paired A/B measurements show a significant gain.
    """

    documentation = """Optimizes the ion transmission through the beamline apertures, stage by stage.
    1. Configure the beamline once (which channel drives each element, which picoammeter measures each aperture).
    2. Choose the target: the total ion current, or a mass selected by a quadrupole (pick its peak on an MScan
       spectrum or a quick sweep; amplitudes in volts, m/z calibration in MScan).
    3. Choose the stages and the exploration, then Optimize. Keep or Revert at the end.
    Simulation runs the built-in simulated beamline without touching any device.
    The soft interlocks only stop the optimization; the protective interlock belongs in the hardware."""

    name = "Transmission"
    version = "0.2.0"
    supportedVersion = "1.0"
    iconFile = "transmission.png"
    useInvalidWhileWaiting = False
    MARKERS = {"initial": "s", "probe": "^", "explore": "o", "reference": "D", "verify": "*"}
    PLOT_INTERVAL_S = 3.0  # Live redraws at most this often, and never during an averaging window.
    STATE_DEFAULTS = dict(mode="total", filter="q2", center=0.0, width=0.0, level="normal", skip=[],
                          simulation=False, advanced=False)

    class Display(Scan.Display):
        def initFig(self):
            super().initFig()
            if self.fig:
                self.axes = [self.fig.add_subplot(2, 2, i) for i in range(1, 5)]
                self.scan.labelAxis = self.axes[0]

    def __init__(self, **kwargs):
        self._cancel = Event()
        self._records = []
        self._stages_meta = []
        self._spectra = {}
        self._result = None
        self._optimizer = None
        self._config_used = ""
        self._simulated_run = False
        self._worker = None
        self._reverting = False
        self._sweeping = False
        self._bringup = None
        self._bringup_timer = None
        self._device_timer = None
        self._sim = None
        self._run_log = None
        self._session_log = None
        self._log_lock = Lock()
        self._watched = None
        self._watchdog = None
        self._measuring = False
        self._plot_pending = False
        self._plot_force = False
        self._last_plot = 0.0
        self._eval_times = []
        self._decided = True
        self._hint = ""
        self._updating = False  # The panel is being filled from the state: ignore its change signals.
        self.panel = None
        self.state = dict(self.STATE_DEFAULTS, skip=[])
        self.beamline_map = _beamline.empty()
        self.displayDefault = ""
        self.notes = ""
        super().__init__(**kwargs)
        self._bridge = _GuiBridge(self)

    # ---------------------------------------------------------------- files
    def _path(self, suffix):
        return Path(self.pluginManager.Settings.configPath) / f"Transmission{suffix}"

    def default_config_path(self):
        return self._path(".toml")

    def _load_state(self):
        try:
            state = json.loads(self._path(".state.json").read_text(encoding="utf-8"))
            self.state.update({k: v for k, v in state.items() if k in self.STATE_DEFAULTS})
        except (OSError, ValueError):
            pass
        try:
            self.beamline_map = _beamline.loads(self._path(".beamline.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            if self._path(".beamline.json").exists():
                self.print(f"Beamline description not readable: {exc}", flag=PRINT.WARNING)
            self.beamline_map = _beamline.empty()

    def _save_state(self):
        try:
            path = self._path(".state.json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.state, indent=2), encoding="utf-8")
        except OSError as exc:
            self.print(f"Transmission settings not saved: {exc}", flag=PRINT.WARNING)

    def save_beamline(self, beamline):
        self.beamline_map = _beamline.normalize(beamline)
        path = self._path(".beamline.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_beamline.dumps(self.beamline_map), encoding="utf-8")
        self.session("beamline_saved", beamline=self.beamline_map)

    # ---------------------------------------------------------------- logs
    def log_directory(self):
        """<Explorer data path>/logs/transmission: collected with the data, never inside the plugin."""
        return Path(self.pluginManager.Settings.dataPath) / "logs" / "transmission"

    def session(self, event, **data):
        """GUI actions and run outcomes, across sessions (rotating file). Never raises."""
        try:
            folder = self.log_directory()
            with self._log_lock:
                if self._session_log is None or self._session_log.path.parent != folder:
                    if self._session_log is not None:
                        self._session_log.close()
                    self._session_log = _log.session_log(folder)
                self._session_log.write(event, data)
        except Exception:  # noqa: BLE001, S110 - logging never stops the plugin
            pass

    def _environment(self):
        root = Path(__file__).resolve().parent
        try:
            from importlib.metadata import version
            explorer = version("esibd-explorer")
        except Exception:  # noqa: BLE001 - informative only
            explorer = "unknown"
        return dict(plugin_version=self.version, explorer_version=explorer, python=sys.version.split()[0],
                    platform=platform.platform(), host=platform.node(), pid=os.getpid(),
                    code=_log.digests([root / "transmission_plugin.py", *(root / "_runtime" / n for n in _RUNTIME_FILES)]))

    def _open_run_log(self, kind, **header):
        """A new log file for one run, quick sweep or revert, starting with everything needed to replay it."""
        try:
            log = _log.run_log(self.log_directory(), kind)
        except Exception as exc:  # noqa: BLE001 - run without a log rather than not at all
            log = _log.NullLog(f"{type(exc).__name__}: {exc}")
        log.write("header", dict(kind=kind, environment=self._environment(), **header))
        if log.error:
            self.print(f"Transmission log unavailable: {log.error}", flag=PRINT.WARNING)
        return log

    # ---------------------------------------------------------------- live redraws (Qt thread)
    def _plot_requested(self, force):
        self._plot_pending = True
        self._plot_force = self._plot_force or force
        self._maybe_plot()

    def set_measuring(self, active):
        """Called when an averaging window opens or closes; redraws wait for it to close."""
        self._measuring = bool(active)
        if not active:
            self._maybe_plot()

    def _maybe_plot(self):
        if not self._plot_pending or self._measuring or self.finished:
            return
        if not self._plot_force and time.monotonic() - self._last_plot < self.PLOT_INTERVAL_S:
            return  # The next measurement or window end brings another chance.
        self._plot_pending = self._plot_force = False
        self._last_plot = time.monotonic()
        self.signalComm.scanUpdateSignal.emit(False)

    @staticmethod
    def _channel_snapshot(instrument):
        if not isinstance(instrument, ExplorerInstrument):
            return None
        try:
            return instrument.snapshot()
        except Exception as exc:  # noqa: BLE001 - informative only
            return dict(error=f"{type(exc).__name__}: {exc}")

    def _watch(self, log):
        """While a log is open, record every Qt-thread freeze longer than 0.5 s (plots, other plugins, Explorer)."""
        self._watched = log
        if self._watchdog is None:
            self._watchdog = QTimer()
            self._watchdog.setInterval(100)
            self._watchdog.timeout.connect(self._tick)
        self._last_tick = time.perf_counter()
        self._watchdog.start()

    def _tick(self):
        now = time.perf_counter()
        blocked = now - self._last_tick
        self._last_tick = now
        if blocked > 0.6 and self._watched is not None:
            self._watched.write("gui_stall", blocked_s=blocked, during_window=self._measuring)

    def _unwatch(self):
        if self._watchdog is not None:
            self._watchdog.stop()
        self._watched = None

    def open_logs(self):
        folder = self.log_directory()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    # ---------------------------------------------------------------- settings and GUI
    def getDefaultSettings(self):
        return {self.NOTES: parameterDict(value="", parameterType=PARAMETERTYPE.TEXT, attr="notes",
                                          toolTip="Notes saved with the next optimization file.")}

    def initGUI(self):
        self.loading = True
        Plugin.initGUI(self)
        self.settingsTree = TreeWidget()  # Notes only; Explorer saves it with each file.
        self.settingsTree.setHeaderLabels([self.name, "Value"])
        self.settingsTree.setRootIsDecorated(False)
        self.settingsTree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self.settingsTree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.settingsMgr = SettingsManager(parentPlugin=self, pluginManager=self.pluginManager, tree=self.settingsTree,
            name=f"{self.name} Settings", defaultFile=self.pluginManager.Settings.configPath / self.configINI,
            dependencyPath=self.pluginManager.Settings.dependencyPath, sourceCodePath=self.pluginManager.Settings.sourceCodePath)
        self.settingsMgr.addDefaultSettings(plugin=self)
        self.settingsMgr.init()
        self.panel = _Panel(self)
        self.addContentWidget(self.panel)
        self.addContentWidget(self.settingsTree)
        self.recordingAction = self.addStateAction(event=self.toggleRecording,
            toolTipFalse="Optimize.", iconFalse=self.makeCoreIcon("play.png"),
            toolTipTrue="Stop: return step by step to the settings at the start of the current stage.",
            iconTrue=self.makeCoreIcon("stop.png"))
        panel = self.panel
        panel.configure.clicked.connect(self.configure_beamline)
        panel.pick.clicked.connect(self.pick_peak)
        panel.advanced.clicked.connect(self.edit_advanced)
        panel.logs.clicked.connect(self.open_logs)
        panel.start_devices.clicked.connect(lambda: self.start_devices())
        panel.optimize.clicked.connect(self.start_optimization)
        panel.stop.clicked.connect(self.stop_optimization)
        panel.keep.clicked.connect(self.keep)
        panel.revert.clicked.connect(self.undo)
        self._load_state()
        self._apply_state()
        for signal in (panel.total.toggled, panel.simulation.toggled, panel.center.valueChanged, panel.width.valueChanged,
                       panel.filter.currentIndexChanged, panel.levels.buttonToggled):
            signal.connect(self.panel_changed)
        self.loading = False
        self.toggleAdvanced(advanced=False)
        self.dummyInitialization()
        self._device_timer = QTimer()
        self._device_timer.setInterval(1500)  # Device states change in their own plugins.
        self._device_timer.timeout.connect(self.refresh_devices)
        self._device_timer.start()
        self.refresh_panel()
        self.session("plugin_start", environment=self._environment(), state=self.state,
                     beamline=_beamline.summary(self.beamline_map), log_directory=str(self.log_directory()))

    def _apply_state(self):
        panel, state = self.panel, self.state
        updating, self._updating = self._updating, True
        try:
            panel.total.setChecked(state["mode"] != "mass")
            panel.mass.setChecked(state["mode"] == "mass")
            panel.simulation.setChecked(bool(state["simulation"]))
            panel.center.setValue(float(state["center"] or 0.0))
            panel.width.setValue(float(state["width"] or 0.0))
            panel.level_buttons.get(state["level"], panel.level_buttons["normal"]).setChecked(True)
        finally:
            self._updating = updating

    def panel_changed(self, *_):
        if self._updating or self.panel is None:
            return
        panel, state = self.panel, self.state
        state["mode"] = "mass" if panel.mass.isChecked() else "total"
        state["simulation"] = panel.simulation.isChecked()
        state["center"], state["width"] = panel.center.value(), panel.width.value()
        if panel.filter.currentData() is not None:
            state["filter"] = panel.filter.currentData()
        state["level"] = next((k for k, b in panel.level_buttons.items() if b.isChecked()), "normal")
        if panel.stage_boxes:
            unchecked = {k for k, box in panel.stage_boxes.items() if not box.isChecked()}
            state["skip"] = sorted({*state["skip"], *unchecked} - {k for k in panel.stage_boxes if k not in unchecked})
        self._save_state()
        self.refresh_panel()

    def active_beamline(self):
        return _beamline.normalize(_simulator.BEAMLINE) if self._simulated() else self.beamline_map

    def refresh_panel(self):
        panel = self.panel
        if panel is None:
            return
        updating, self._updating = self._updating, True
        try:
            beamline = self.active_beamline()
            if self._simulated():
                panel.beamline_label.setText(f"Simulation: built-in beamline ({_beamline.summary(beamline)}).")
            elif beamline["knobs"] and beamline["collectors"]:
                panel.beamline_label.setText(f"✓ {_beamline.summary(beamline)}")
            else:
                panel.beamline_label.setText("Not configured yet: choose which channels drive the elements and measure the apertures.")
            filters = _beamline.filters(beamline)
            panel.filter.clear()
            for key, label in filters:
                panel.filter.addItem(label, key)
            index = panel.filter.findData(self.state["filter"])
            panel.filter.setCurrentIndex(index if index >= 0 else 0)
            if filters:
                self.state["filter"] = panel.filter.currentData()
            running = self._busy()
            editable = not self.state["advanced"] and not running
            mass = self.state["mode"] == "mass"
            for widget in (panel.filter, panel.center, panel.width, panel.pick):
                widget.setEnabled(mass and bool(filters) and editable)
            stages = self._plan(beamline)
            if [key for key, *_ in stages] != list(panel.stage_boxes):
                panel.set_stages(stages, self.state["skip"])
            for widget in (panel.total, panel.mass, *panel.stage_boxes.values(), *panel.level_buttons.values()):
                widget.setEnabled(editable)
            ready, reason = self._ready()
            panel.optimize.setEnabled(not running and ready)
            panel.optimize.setToolTip(reason)
            hint = "Ready." if ready else reason
            if not running and panel.progress.text() in ("", "Ready.", self._hint):
                panel.progress.setText(hint)
            self._hint = hint
            panel.stop.setEnabled(running)
            for widget in (panel.configure, panel.advanced, panel.simulation):
                widget.setEnabled(not running)
            if getTestMode():  # Explorer's test mode always simulates: show it, without changing the saved choice.
                panel.simulation.setChecked(True)
                panel.simulation.setEnabled(False)
                panel.simulation.setToolTip("Explorer's test mode is active: Transmission always simulates.")
            else:
                panel.simulation.setChecked(bool(self.state["simulation"]))
                panel.simulation.setToolTip("Run on the built-in simulated beamline. No device is touched.")
            decision = self._optimizer is not None and not running and not self._decided
            panel.keep.setVisible(decision)
            panel.revert.setVisible(decision)
            panel.advanced.setText("Advanced… (in use)" if self.state["advanced"] else "Advanced…")
            self.refresh_devices()
        finally:
            self._updating = updating

    def _ready(self):
        if self.state["advanced"]:
            return (self.default_config_path().is_file(), "Uses the advanced configuration.")
        beamline = self.active_beamline()
        if not beamline["knobs"] or not beamline["collectors"]:
            return False, "Configure the beamline first."
        if self.state["mode"] == "mass" and not _beamline.filters(beamline):
            return False, "Map a quadrupole RF amplitude and a current after it (Configure…) to select a mass."
        if self.state["mode"] == "mass" and not (self.state["center"] > 0 and self.state["width"] > 0):
            return False, "Pick the peak of the selected mass first."
        if not self._selected_stages(beamline):
            return False, "Choose at least one stage." if self._plan(beamline) else "No stage can run: map a setting and a current after it."
        return True, "Optimize the selected stages."

    def _busy(self):
        return not self.finished or self.recording or self._reverting or self._sweeping or self._bringup is not None

    def _plan(self, beamline):
        state = self.state
        return _beamline.stages(beamline, mode=state["mode"], filter_key=state["filter"] if state["mode"] == "mass" else None)

    def _selected_stages(self, beamline):
        return [key for key, *_ in self._plan(beamline) if key not in self.state["skip"]]

    def show_progress(self, text):
        if self.panel is not None:
            self.panel.progress.setText(text)

    def channel_lists(self):
        driven, currents, pressures = [], [], []
        for channel in self.pluginManager.DeviceManager.channels():
            try:
                unit = str(channel.unit or "")
            except Exception:  # noqa: BLE001 - a channel without a device unit
                unit = ""
            if unit == "A":
                currents.append(channel.name)
            elif "bar" in unit.lower():
                pressures.append(channel.name)
            elif (getattr(channel, "inout", None) == INOUT.IN and getattr(channel, "real", True)
                  and not is_esi_channel(channel)):
                driven.append(channel.name)
        return driven, currents, pressures

    def template_text(self):
        return _engine.template(*self.channel_lists())

    def generated_text(self):
        try:
            return self._generated()
        except ValueError as exc:
            return f"# The simple settings cannot generate a configuration yet: {exc}\n" + self.template_text()

    def _generated(self):
        state, beamline = self.state, self.active_beamline()
        return _beamline.build(beamline, selected=self._selected_stages(beamline), level=state["level"], mode=state["mode"],
                               filter_key=state["filter"] if state["mode"] == "mass" else None,
                               center=state["center"], width=state["width"],
                               settings=SIMULATION_SETTINGS if self._simulated() else None)

    def missing_channels(self, config):
        names = {c.name.strip().lower() for c in self.pluginManager.DeviceManager.channels()}
        wanted = (*config.driven_channels, *config.current_channels, *config.pressure_channels)
        return [name for name in wanted if name.strip().lower() not in names]

    def configure_beamline(self):
        dialog = _BeamlineDialog(self, self.beamline_map)
        if dialog.exec():
            self.save_beamline(dialog.result_beamline)
            self.refresh_panel()

    def pick_peak(self):
        dialog = _SpectrumDialog(self, self.state["filter"], self.state["center"] or None)
        if dialog.exec() and dialog.selection:
            self.apply_peak(dialog.filter_key(), *dialog.selection, source=dialog.source)

    def apply_peak(self, filter_key, center, width, source=""):
        self.session("peak_selected", filter=filter_key, center=center, width=width, source=source)
        self.state.update(filter=filter_key, center=float(center), width=float(width), mode="mass")
        self._save_state()
        self._apply_state()
        self.refresh_panel()

    def edit_advanced(self):
        path = self.default_config_path()
        text = None if path.is_file() else self.generated_text()
        dialog = _ConfigEditor(self, path, text, use=bool(self.state["advanced"]))
        if dialog.exec():
            self.state["advanced"] = dialog.use.isChecked()
            self._save_state()
            self.session("advanced_configuration", use=self.state["advanced"], path=str(path),
                         configuration=dialog.editor.toPlainText())
            self.refresh_panel()

    def estimateScanTime(self):
        pass  # The number of measurements is adaptive.

    # ---------------------------------------------------------------- instruments
    def _simulated(self):
        return bool(self.state.get("simulation")) or getTestMode()

    def _instrument(self, config, cancel):
        if self._simulated():
            if self._sim is None:
                self._sim = _simulator.SimulatedBeamline(seed=1, t0=time.time())
            self._sim.cancel, self._sim.pace = cancel, 0.02
            return self._sim
        instrument = ExplorerInstrument(self, config, cancel)
        instrument.check()
        return instrument

    def quick_sweep(self, filter_key, amplitudes, on_point, cancel):
        """Worker thread: sweep the filter amplitude, then return to the previous value.

        The panel stays busy until the filter is back, even if the dialog is closed meanwhile.
        """
        self._sweeping = True  # Usually already set on the Qt thread by the dialog, before this worker started.
        self._bridge.refresh.emit()
        try:
            return self._quick_sweep(filter_key, amplitudes, on_point, cancel)
        finally:
            self._sweeping = False
            self._bridge.refresh.emit()

    def _quick_sweep(self, filter_key, amplitudes, on_point, cancel):
        try:
            beamline = self.active_beamline()
            measure = _beamline.filter_measure(beamline, filter_key)
            step = max(float(np.max(np.abs(np.diff(amplitudes)))) if len(amplitudes) > 1 else 1.0, 0.1)
            target = _engine.Target(beamline["knobs"][f"{filter_key}_rf"], float(amplitudes[0]), step, measure,
                                    max_step=step, spectrum_points=0)
            options = dict(SIMULATION_SETTINGS) if self._simulated() else {}
            config = _engine.Config([], {k: tuple(v) for k, v in beamline["gauges"].items()}, target,
                                    polarity=beamline["polarity"], **options)
            instrument = self._gui_call(lambda: self._instrument(config, cancel))
        except (_engine.ConfigError, _engine.InstrumentError, KeyError, ValueError) as exc:
            self.session("quick_sweep_refused", filter=filter_key, reason=str(exc), traceback=traceback.format_exc())
            return dict(status="error", reason=str(exc), amplitude=[], current=[], sem=[])
        log = self._open_run_log("sweep", filter=filter_key, amplitudes=amplitudes, beamline=beamline, measure=measure,
                                 simulation=self._simulated(), channels=self._channel_snapshot(instrument))
        if isinstance(instrument, ExplorerInstrument):
            instrument.log = log.write
        self._bridge.watch.emit(log)
        try:
            optimizer = _engine.Optimizer(config, instrument, log=log.write,
                                          on_event=lambda kind, data: on_point(data["amplitude"], data["current"])
                                          if kind == "sweep" else None)
            outcome = optimizer.spectrum(amplitudes, measure)
        except Exception as exc:  # noqa: BLE001 - reported to the dialog, kept in the log
            log.write("exception", traceback=traceback.format_exc())
            outcome = dict(status="error", reason=f"{type(exc).__name__}: {exc}", amplitude=[], current=[], sem=[])
        finally:
            self._bridge.unwatch.emit()
            log.close()
        self.session("quick_sweep", filter=filter_key, points=len(amplitudes), status=outcome["status"],
                     reason=outcome["reason"], log=str(log.path) if log.path else None)
        return outcome

    def _gui_call(self, action):
        call = _Call(action)
        if QThread.currentThread() == self._bridge.thread():
            self._bridge.execute(call)
        else:
            self._bridge.request.emit(call)
            if not call.done.wait(10):
                call.expired.set()
                raise _engine.InstrumentError("Explorer did not respond within 10 s")
        if call.error is not None:
            raise call.error
        return call.result

    # ---------------------------------------------------------------- devices
    BRINGUP_S = 300.0  # Initialization and the ramp at ON of a supply can take minutes.

    def _needed(self):
        """(driven, measured) channels of what Optimize would run; the whole beamline when it cannot be built yet."""
        if self._simulated():
            return [], []
        try:
            _, config = self._configuration()
            return list(config.driven_channels), [*config.current_channels, *config.pressure_channels]
        except (_engine.ConfigError, OSError):
            beamline = self.beamline_map
            driven = [c for channels in beamline["knobs"].values() for c in channels]
            return driven, [*beamline["collectors"].values(), *beamline["gauges"]]

    def device_report(self, driven, measured):
        """One entry per device (or missing channel) the measurement needs, with its state and the fix."""
        channels = {}
        for channel in self.pluginManager.DeviceManager.channels():
            channels.setdefault(channel.name.strip().lower(), channel)
        known = self.beamline_map["devices"]
        entries = {}
        for role, names in (("driven", driven), ("measured", measured)):
            for name in dict.fromkeys(names):
                channel = channels.get(name.strip().lower())
                if channel is None:
                    owner = known.get(name)
                    entry = entries.setdefault(f"missing:{owner or ''}", dict(
                        name=owner or "Channels", device=None, driven=[], measured=[], esi=False, state="missing",
                        fix=f"enable the {owner} plugin (Plugin Manager), then restart Explorer" if owner else ""))
                    entry[role].append(name)
                    if not owner:
                        entry["fix"] = "not found in Explorer: " + ", ".join([*entry["driven"], *entry["measured"]])
                    continue
                device = channel.getDevice()
                entry = entries.setdefault(device.name, dict(name=device.name, device=device, driven=[], measured=[],
                                                             esi=device.name == "ESI" or is_esi_channel(channel)))
                entry[role].append(name)
        for entry in entries.values():
            device = entry["device"]
            if device is None:
                continue
            busy = device_busy(device)
            if busy:
                entry.update(state="busy", fix=f"{busy} in progress")
            elif not device_on(device):
                entry.update(state="off", fix="turn it ON")
            elif entry["measured"] and not getattr(device, "recording", True):
                entry.update(state="not recording", fix="start its recording")
            else:
                entry.update(state="ready", fix="")
            if entry["esi"] and entry["state"] in ("off", "not recording"):
                entry["fix"] = "turn it ON in the ESI plugin (never from Transmission)"
        return list(entries.values())

    @staticmethod
    def _startable(entry):
        return entry["state"] in ("off", "not recording") and not entry["esi"]

    @staticmethod
    def _roles(entry):
        roles = []
        if entry["driven"]:
            roles.append("drives " + ", ".join(entry["driven"]))
        if entry["measured"]:
            roles.append("measures " + ", ".join(entry["measured"]))
        return "; ".join(roles)

    def _describe(self, entry):
        return f"{entry['name']} ({self._roles(entry)})"

    def refresh_devices(self):
        panel = self.panel
        if panel is None:
            return
        if self._simulated():
            panel.devices_label.setText("Devices: none needed (simulation).")
            panel.start_devices.setVisible(False)
            return
        try:
            report = self.device_report(*self._needed())
        except Exception as exc:  # noqa: BLE001 - informative only
            panel.devices_label.setText(f"Devices: state unavailable ({exc}).")
            panel.start_devices.setVisible(False)
            return
        if not report:
            panel.devices_label.setText("")
            panel.start_devices.setVisible(False)
            return
        parts = []
        for entry in report:
            if entry["state"] == "ready":
                parts.append(f"✓ {entry['name']}")
            else:
                parts.append(f"<b>✗ {entry['name']}: {entry['state'] if entry['state'] != 'missing' else 'not available'}</b>")
        panel.devices_label.setText("Devices: " + " · ".join(parts))
        panel.devices_label.setToolTip("\n".join(self._describe(e) + (f": {e['fix']}" if e["fix"] else "") for e in report))
        panel.start_devices.setVisible(any(self._startable(e) for e in report))
        panel.start_devices.setEnabled(not self._busy())

    def _confirm_devices(self, text, action):
        """Ask the operator; True to go ahead. Tests replace this method."""
        box = QMessageBox(QMessageBox.Icon.Question, "Transmission — devices", text,
                          parent=QApplication.activeWindow() or self.panel)
        go = box.addButton(action, QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        return box.clickedButton() is go

    def start_devices(self, report=None, *, then=None, action="", on_progress=None, needed=None):
        """Propose to turn ON the devices that are OFF or not recording, then call ``then`` when all are ready.

        ``needed``: (driven, measured) channels; by default what Optimize would run.
        """
        if report is None:
            report = self.device_report(*(needed or self._needed()))
        startable = [e for e in report if self._startable(e)]
        blocked = [e for e in report if e["state"] != "ready" and not self._startable(e)]
        show = on_progress or self.show_progress
        if blocked:
            # Missing plugins, the ESI or a transition: nothing Transmission may fix; no partial start.
            text = "Not ready: " + "; ".join(f"{e['name']}: {e['fix']}" for e in blocked) + "."
            show(text)
            self.session("devices_not_ready", devices={e["name"]: e["state"] for e in blocked})
            return False
        if not startable:
            return True
        items = "".join(f"<li><b>{e['name']}</b> — {'turn ON' if e['state'] == 'off' else 'start recording'} "
                        f"({self._roles(e)})</li>" for e in startable)
        text = (f"<p>These devices are needed{' to ' + action if action else ''} but are not ready:</p><ul>{items}</ul>"
                "<p>Turning a device ON works as its own power button: a supply then applies the setpoints set in its "
                "plugin. Check them before going on.</p>")
        label = {"optimize": "Turn ON and optimize", "sweep": "Turn ON and sweep"}.get(action, "Turn ON")
        if not self._confirm_devices(text, label):
            show("Devices not turned ON: nothing started.")
            self.session("devices_declined", devices=[e["name"] for e in startable], action=action)
            return False
        self.session("devices_turn_on", devices={e["name"]: e["state"] for e in startable}, action=action)
        for entry in startable:
            if entry["state"] == "off":
                entry["device"].setOn(True)  # The device plugin's own ON path (transitions, ramps, checks).
        self._bringup = dict(names=[e["name"] for e in startable], then=then, show=show, action=action, needed=needed,
                             started=time.monotonic(), recording_requested=set())
        if self._bringup_timer is None:
            self._bringup_timer = QTimer()
            self._bringup_timer.setInterval(500)
            self._bringup_timer.timeout.connect(self._bringup_tick)
        self._bringup_timer.start()
        show(f"Turning ON {', '.join(self._bringup['names'])}…")
        self.refresh_panel()
        return True

    def _bringup_tick(self):
        bringup = self._bringup
        if bringup is None:
            return
        elapsed = time.monotonic() - bringup["started"]
        report = {e["name"]: e for e in self.device_report(*(bringup["needed"] or self._needed()))}
        pending = []
        for name in bringup["names"]:
            entry = report.get(name)
            if entry is None:
                continue  # No longer needed.
            device = entry["device"]
            if entry["state"] == "not recording" and name not in bringup["recording_requested"]:
                controller = getattr(device, "controller", None)
                if device_on(device) and getattr(controller, "initialized", True):
                    device.toggleRecording(on=True, manual=False)  # Refused by the plugin until it is really ON.
                    if getattr(device, "recording", False):
                        bringup["recording_requested"].add(name)
            if entry["state"] != "ready":
                pending.append(f"{name} ({entry['state']})")
        if not pending:
            self.cancel_bringup("Devices ready.", log="devices_ready")
            if bringup["then"] is not None:
                bringup["then"]()
            return
        if elapsed > self.BRINGUP_S:
            self.cancel_bringup(f"Devices not ready after {self.BRINGUP_S:.0f} s: {', '.join(pending)}. Nothing started.",
                                log="devices_timeout")
            return
        bringup["show"](f"Turning ON {', '.join(pending)}… {elapsed:.0f} s")

    def cancel_bringup(self, message, log="devices_cancelled"):
        bringup, self._bringup = self._bringup, None
        if self._bringup_timer is not None:
            self._bringup_timer.stop()
        if bringup is not None:
            bringup["show"](message)
            self.session(log, devices=bringup["names"], seconds=time.monotonic() - bringup["started"],
                         action=bringup["action"])
        self.refresh_panel()

    # ---------------------------------------------------------------- run
    def start_optimization(self):
        if self.finished and not self.recording and self._bringup is None:
            self.recording = True
            self.toggleRecording()
        self.refresh_panel()

    def stop_optimization(self):
        self.session("stop_requested", recording=self.recording, reverting=self._reverting,
                     starting_devices=self._bringup is not None)
        if self._bringup is not None:
            self.cancel_bringup("Stopped while turning the devices ON; they stay as they are.")
            return
        if self.recording:
            self.recording = False
            self.toggleRecording()
        else:
            self._cancel.set()  # Also stops a revert in progress.

    def _configuration(self):
        if self.state["advanced"]:
            path = self.default_config_path()
            if not path.is_file():
                raise _engine.ConfigError(f"No advanced configuration at {path}; use Advanced… to create it.")
            text = path.read_text(encoding="utf-8")
        else:
            try:
                text = self._generated()
            except ValueError as exc:
                raise _engine.ConfigError(str(exc)) from None
        return text, _engine.parse_config(text)

    def initScan(self):
        if self._dummy_initialization:
            return False
        self._cancel = Event()
        try:
            text, config = self._configuration()
            instrument = self._instrument(config, self._cancel)
            optimizer = _engine.Optimizer(config, instrument, on_event=self._on_event)
        except (_engine.ConfigError, _engine.InstrumentError, OSError) as exc:
            self.show_progress(f"Cannot start: {exc}")
            self.print(f"Cannot start: {exc}", flag=PRINT.WARNING)
            self.session("cannot_start", reason=str(exc), state=self.state, traceback=traceback.format_exc())
            return False
        self._optimizer, self._config_used, self._simulated_run = optimizer, text, self._simulated()
        self._records, self._stages_meta, self._spectra, self._result, self._eval_times = [], [], {}, None, []
        self._decided = True
        self._measuring = self._plot_pending = self._plot_force = False
        self._last_plot = 0.0
        if self.panel is not None:
            self.panel.result.setText("")
        self.show_progress("Simulation running…" if self._simulated_run else "Running…")
        self.toggleDisplay(visible=True)
        self.updateFile()
        explorer = isinstance(instrument, ExplorerInstrument)
        self._run_log = self._open_run_log(
            "simulation" if self._simulated_run else "run", file=self.file, state=self.state, beamline=self.active_beamline(),
            configuration=text, notes=self.notes, channels=self._channel_snapshot(instrument))
        optimizer.log = self._run_log.write
        if explorer:
            instrument.log = self._run_log.write
        self._watch(self._run_log)
        self.session("run_start", file=self.file, log=str(self._run_log.path) if self._run_log.path else None,
                     simulation=self._simulated_run, mode=self.state["mode"], advanced=self.state["advanced"],
                     stages=[stage.name for stage in config.stages])
        return True

    def _on_event(self, kind, data):
        # Worker thread: plain data only; the GUI is updated through signals.
        if kind == "stage":
            stage = data["stage"]
            self._stages_meta.append(dict(name=stage.name, knobs=[k.name for k in stage.knobs],
                                          lower=list(map(float, data["lower"])), upper=list(map(float, data["upper"])),
                                          start=dict(data["start"]), aperture=stage.aperture or "", budget=stage.budget,
                                          downstream=list(stage.downstream), normalize_by=list(stage.normalize_by),
                                          peak=stage.peak))
            self._bridge.status.emit(f"Stage {len(self._stages_meta)}/{len(self._optimizer.stages)} — {stage.name}: starting")
            self._bridge.plot.emit(True)
        elif kind == "evaluation":
            self._records.append(data["record"])
            self._eval_times.append(time.monotonic())
            self._bridge.status.emit(self._progress_text(data["record"]))
            self._bridge.plot.emit(False)
        elif kind == "spectrum":
            self._spectra[data["phase"]] = data["data"]
            self._bridge.plot.emit(True)
        elif kind == "status":
            self._bridge.status.emit(data["text"])

    def _progress_text(self, record):
        meta = self._stages_meta[-1] if self._stages_meta else {}
        rows = [r for r in self._records if r["stage"] == record["stage"]]
        start = [r["objective"] for r in rows if r["kind"] == "initial" and _finite(r["objective"])]
        best = [r["objective"] for r in rows if r["valid"] and _finite(r["objective"])]
        gain = (max(best) / np.mean(start) - 1) * 100 if start and best and np.mean(start) > 0 else 0.0
        stage_index = len(self._stages_meta)
        total = len(self._optimizer.stages)
        remaining = max(meta.get("budget", 0) - len(rows), 0) + sum(s.budget for s in self._optimizer.stages[stage_index:])
        text = (f"Stage {stage_index}/{total} — {record['stage']}: measurement {len(rows)}/{meta.get('budget', '?')}"
                f" · best so far {gain:+.0f} %")
        if len(self._eval_times) > 3:
            per = float(np.median(np.diff(self._eval_times[-20:])))
            text += f" · at most {max(1, round(remaining * per / 60))} min left"
        if not record["valid"]:
            text += f" · last point: {record['reason']}"
        return text

    def runScan(self, recording):
        try:
            self._result = self._optimizer.run()
        except Exception as exc:  # noqa: BLE001 - never leave the scan running
            self._result = dict(status="error", reason=f"{type(exc).__name__}: {exc}", stages=list(self._optimizer.results),
                                initial=dict(self._optimizer.initial), final=dict(self._optimizer.commanded),
                                evaluations=len(self._optimizer.history), spectra={}, checks={})
            self._run_log.write("exception", traceback=traceback.format_exc())
            self.print(f"Optimization failed: {exc}", flag=PRINT.ERROR)
        finally:
            optimizer = self._optimizer
            self._decided = all(math.isclose(optimizer.commanded.get(c, v), v, rel_tol=1e-9, abs_tol=1e-9)
                                for c, v in optimizer.initial.items())
            result = self._result or {}
            summary = self.result_summary()
            self._run_log.write("summary", status=result.get("status"), reason=result.get("reason"), line=summary,
                                stages=result.get("stages"), checks=result.get("checks"), target=result.get("target"),
                                changed=not self._decided)
            self.session("run_end", status=result.get("status"), reason=result.get("reason"), summary=summary,
                         file=self.file, log=str(self._run_log.path) if self._run_log.path else None)
            self._bridge.status.emit(self._status_line())
            self.signalComm.updateRecordingSignal.emit(False)
            self.signalComm.scanUpdateSignal.emit(True)

    def _status_line(self):
        result = self._result or {}
        status = str(result.get("status", "idle")).capitalize()
        reason = result.get("reason") or ""
        return f"{status}: {reason}" if reason else f"{status}."

    def result_summary(self):
        """One line: the end-of-beamline current before and after, and the stages improved."""
        result = self._result or {}
        checks = result.get("checks") or {}
        stages = result.get("stages") or []
        parts = []
        if "before" in checks and "after" in checks:
            channel = self._end_channel()
            before, after = checks["before"]["currents"].get(channel), checks["after"]["currents"].get(channel)
            if _finite(before) and _finite(after):
                target = result.get("target")
                label = (f"Selected mass ({target['filter'] or 'filter'} at {target['final_center']:.1f} V) on {channel}"
                         if target else channel)
                ratio = f" (×{after / before:.2g})" if before > 0 else ""
                parts.append(f"{label}: {format_current(before)} → {format_current(after)}{ratio}")
        if stages:
            parts.append(f"{sum(s['adopted'] for s in stages)}/{len(stages)} stage(s) improved")
        return ". ".join(parts)

    def _end_channel(self):
        try:
            config = _engine.parse_config(self._config_used)
        except _engine.ConfigError:
            return ""
        return config.stages[-1].downstream[-1] if config.stages else ""

    def _result_lines(self):
        lines = []
        for stage in (self._result or {}).get("stages", []):
            if stage["adopted"]:
                lines.append(f"{stage['stage']}: +{100 * stage['relative_gain']:.0f} % ({stage['gain']:.3g} ± {stage['gain_sem']:.2g})")
            else:
                lines.append(f"{stage['stage']}: kept the start")
        return "\n".join(lines)

    def scanUpdate(self, done=False):
        if done:
            self._unwatch()
            if self._run_log is not None:
                self._run_log.close()
        if done and self._result:
            summary = self.result_summary()
            if self.panel is not None:
                self.panel.result.setText(summary)
                self.show_progress(self._status_line())
            lines = self._result_lines()
            self.print(f"{self.name}: {self._status_line()} {summary}" + (f"\n{lines}" if lines else ""))
        super().scanUpdate(done=done)
        self.refresh_panel()

    @property
    def recording(self):
        return Scan.recording.fget(self)

    @recording.setter
    def recording(self, value):
        # DeviceManager also stops scans through this property, not only the button.
        if not value:
            self._cancel.set()
        Scan.recording.fset(self, value)

    @property
    def finished(self):
        return self._finished

    @finished.setter
    def finished(self, value):
        Scan.finished.fset(self, value)
        if value and hasattr(self, "_bridge"):
            self._bridge.refresh.emit()

    def toggleRecording(self):
        if not self.recording:
            self._cancel.set()
        elif self._reverting or self._sweeping or self._bringup is not None:
            self.print("Wait until the revert, the quick sweep or the devices have finished.", flag=PRINT.WARNING)
            self.recording = False
            return
        elif self.finished and not self._simulated():
            # Panel or toolbar: devices that are OFF or not recording are proposed for turning ON first.
            report = self.device_report(*self._needed())
            if any(entry["state"] != "ready" for entry in report):
                self.recording = False
                self.start_devices(report, then=self.start_optimization, action="optimize")
                self.refresh_panel()
                return
        super().toggleRecording()
        self.refresh_panel()

    def keep(self):
        self.session("keep", log=str(self._run_log.path) if self._run_log is not None and self._run_log.path else None)
        self._decided = True
        self.show_progress("Optimized settings kept.")
        self.refresh_panel()

    def undo(self):
        if self._busy():
            self.print("Wait until the optimization has finished.", flag=PRINT.WARNING)
            return
        if self._optimizer is None or not self._optimizer.initial:
            self.show_progress("Nothing to revert.")
            return
        self._cancel = Event()
        self._optimizer.instrument.cancel = self._cancel
        self._decided = True
        self._reverting = True
        self.show_progress("Reverting to the settings before the last run…")
        log = self._open_run_log("revert", file=self.file, initial=self._optimizer.initial, commanded=self._optimizer.commanded,
                                 run_log=str(self._run_log.path) if self._run_log is not None and self._run_log.path else None)
        self._optimizer.log = log.write
        if isinstance(self._optimizer.instrument, ExplorerInstrument):
            self._optimizer.instrument.log = log.write
        self._watch(log)

        def work():
            try:
                outcome = self._optimizer.restore_initial()
            except Exception as exc:  # noqa: BLE001 - never leave the panel locked
                outcome = dict(status="error", reason=f"{type(exc).__name__}: {exc}")
                log.write("exception", traceback=traceback.format_exc())
            finally:
                self._reverting = False
                self._bridge.unwatch.emit()
                log.close()
            self.session("revert", status=outcome["status"], reason=outcome["reason"], file=self.file,
                         log=str(log.path) if log.path else None)
            self._bridge.status.emit(f"Revert {outcome['status']}" + (f": {outcome['reason']}" if outcome["reason"] else "."))
            self._bridge.refresh.emit()
        self._worker = Thread(target=work, name=f"{self.name} revert", daemon=True)
        self._worker.start()
        self.refresh_panel()

    def close(self):
        self._cancel.set()
        self._unwatch()
        self.session("plugin_close")
        if self._session_log is not None:
            self._session_log.close()
        return super().close()

    # ---------------------------------------------------------------- display
    @plotting
    def plot(self, update=False, done=False, **kwargs):  # noqa: ARG002
        if not self.displayActive() or not getattr(self.display, "axes", None) or len(self.display.axes) < 4:
            return
        started = time.perf_counter()
        records = list(self._records)
        stages = list(self._stages_meta)
        ax_objective, ax_second, ax_currents, ax_pressures = self.display.axes
        for ax in self.display.axes:
            ax.clear()
        polarity = self._polarity()
        current = None
        for s, meta in enumerate(stages):
            rows = [r for r in records if r["stage"] == meta["name"]]
            if not rows:
                continue
            color = f"C{s % 10}"
            index = np.array([r["index"] for r in rows])
            start = [r["objective"] for r in rows if r["kind"] == "initial" and _finite(r["objective"])]
            scale = float(np.mean(start)) if start and np.mean(start) != 0 else 1.0
            for kind, marker in self.MARKERS.items():
                chosen = [r for r in rows if r["kind"] == kind and r["valid"]]
                if chosen:
                    ax_objective.plot([r["index"] for r in chosen], [r["objective"] / scale for r in chosen], marker,
                                      color=color, markersize=6 if kind == "verify" else 4, linestyle="none")
            lost = [r for r in rows if not r["valid"]]
            if lost:
                ax_objective.plot([r["index"] for r in lost], [0.0] * len(lost), "x", color="grey", linestyle="none")
            best = np.maximum.accumulate([r["objective"] / scale if r["valid"] and _finite(r["objective"]) else -np.inf for r in rows])
            ax_objective.step(index, np.where(np.isfinite(best), best, np.nan), where="post", color=color, linewidth=1,
                              label=meta["name"])
            ax_objective.axvline(index[0] - 0.5, color="grey", linewidth=0.5)
            current = (meta, rows, index)
            down = np.array([sum(polarity * r["currents"][c][0] for c in meta["downstream"]) for r in rows])
            ax_currents.semilogy(index, np.where(down > 0, down, np.nan), color="tab:green", linewidth=1,
                                 label="after the aperture" if s == 0 else None)
            if meta["aperture"]:
                hit = np.array([polarity * r["currents"][meta["aperture"]][0] for r in rows])
                ax_currents.semilogy(index, np.where(hit > 0, hit, np.nan), color="tab:red", linewidth=1,
                                     label="on the aperture" if s == 0 else None)
                ax_currents.semilogy(index, np.where(hit + down > 0, hit + down, np.nan), color="grey", linestyle="--",
                                     linewidth=0.8, label="reaching the aperture" if s == 0 else None)
        if self._spectra:
            for phase, color in (("before", "tab:grey"), ("after", "tab:green")):
                data = self._spectra.get(phase)
                if data and data.get("amplitude"):
                    ax_second.plot(data["amplitude"], data["current"], marker=".", color=color, label=phase)
            ax_second.set_title("Selected peak", fontsize=9)
            ax_second.set_xlabel("Filter amplitude (V)")
            ax_second.set_ylabel("Current after the filter (A)")
        else:
            if current is not None:
                meta, rows, index = current
                span = np.array(meta["upper"]) - np.array(meta["lower"])
                deltas = np.array([r["delta"] for r in rows])
                for k, knob in enumerate(meta["knobs"]):
                    normalized = (deltas[:, k] - meta["lower"][k]) / span[k] if span[k] else deltas[:, k] * 0
                    ax_second.plot(index, normalized, marker=".", linewidth=0.8, color=f"C{k % 10}", label=knob)
                ax_second.set_title(meta["name"], fontsize=9)
            ax_second.set_ylabel("Setting position in window")
            ax_second.set_ylim(-0.05, 1.05)
            ax_second.set_xlabel("Measurement")
        pressure_names = sorted({p for r in records for p in r["pressures"]})
        for p, name in enumerate(pressure_names):
            ax_pressures.semilogy([r["index"] for r in records],
                                  [r["pressures"][name][0] if r["pressures"].get(name) and r["pressures"][name][0] > 0 else np.nan
                                   for r in records], marker=".", linewidth=0.8, color=f"C{p % 10}", label=name)
        ax_objective.set_ylabel("Criterion / stage start")
        ax_currents.set_ylabel("Current (A)")
        ax_pressures.set_ylabel("Pressure (mbar)")
        for ax in (ax_objective, ax_currents, ax_pressures):
            ax.set_xlabel("Measurement")
        for ax in self.display.axes:
            if ax.get_legend_handles_labels()[0]:
                ax.legend(fontsize=7, loc="best")
        if stages and self.file and self.file.name:
            self.setLabelMargin(ax_objective, 0.15)
            self.labelPlot(self.file.name)
        self.display.canvas.draw_idle()
        elapsed = time.perf_counter() - started
        if elapsed > 0.3 and self._watched is not None:
            self._watched.write("plot_slow", duration_s=elapsed, records=len(records))

    def _polarity(self):
        try:
            return _engine.parse_config(self._config_used).polarity if self._config_used else 1
        except _engine.ConfigError:
            return 1

    # ---------------------------------------------------------------- files
    def saveData(self, file):
        with h5py.File(file, "a", track_order=True) as h5file:
            group = self.requireGroup(h5file, self.name)
            group.attrs["configuration"] = self._config_used
            group.attrs["result"] = json.dumps(_jsonable(self._result or {}))
            group.attrs["beamline"] = json.dumps(_jsonable(self.active_beamline()))
            group.attrs["simulation"] = bool(self._simulated_run)
            group.attrs["version"] = self.version
            group.attrs["log_file"] = str(self._run_log.path) if self._run_log is not None and self._run_log.path else ""
            group.attrs["notes"] = self.notes or ""
            stages = group.require_group("Stages")
            text = h5py.string_dtype("utf-8")
            for i, meta in enumerate(self._stages_meta, 1):
                rows = [r for r in self._records if r["stage"] == meta["name"]]
                g = stages.create_group(f"{i:02d}")
                g.attrs["meta"] = json.dumps(_jsonable(meta))
                g.attrs["name"] = meta["name"]
                if not rows:
                    continue
                g.create_dataset("index", data=np.array([r["index"] for r in rows], dtype=np.int64))
                for key in ("time", "begin", "objective", "objective_sem", "incident", "transmitted", "model_objective"):
                    g.create_dataset(key, data=np.array([r[key] for r in rows], dtype=float))
                g.create_dataset("valid", data=np.array([r["valid"] for r in rows], dtype=bool))
                g.create_dataset("kind", data=[r["kind"] for r in rows], dtype=text)
                g.create_dataset("reason", data=[r["reason"] for r in rows], dtype=text)
                g.create_dataset("delta", data=np.array([r["delta"] for r in rows], dtype=float))
                g["delta"].attrs["knobs"] = json.dumps(meta["knobs"])
                for key in ("currents", "pressures"):
                    channels = sorted(rows[0][key])
                    g.create_dataset(key, data=np.array([[r[key][c] for c in channels] for r in rows], dtype=float).reshape(len(rows), len(channels), 3))
                    g[key].attrs["channels"] = json.dumps(channels)
                    g[key].attrs["columns"] = json.dumps(["mean", "standard error", "samples"])
                channels = sorted(rows[0]["setpoints"])
                g.create_dataset("setpoints", data=np.array([[r["setpoints"][c] for c in channels] for r in rows], dtype=float))
                g["setpoints"].attrs["channels"] = json.dumps(channels)
                if meta.get("peak"):
                    g.attrs["peaks"] = json.dumps(_jsonable([r.get("peak") for r in rows]))
                g["time"].attrs["Unit"] = "s since Unix epoch (end of the averaging window)"

    def saveScanParallel(self, file):
        try:
            super().saveScanParallel(file)
        except Exception as exc:  # noqa: BLE001 - never leave the plugin unable to start again
            self.print(f"Optimization data could not be saved: {exc}", flag=PRINT.ERROR)
            self._bridge.status.emit(f"Save failed: {exc}. The data remain in memory.")
            self.signalComm.saveScanCompleteSignal.emit()

    def loadDataInternal(self):
        self._records, self._stages_meta = [], []
        with h5py.File(self.file, "r") as h5file:
            if self.name not in h5file:
                return False
            group = h5file[self.name]
            self._config_used = str(group.attrs.get("configuration", ""))
            self._result = json.loads(group.attrs.get("result", "{}"))
            self._spectra = dict(self._result.get("spectra") or {})
            self.notes = str(group.attrs.get("notes", ""))
            for key in sorted(group.get("Stages", {})):
                g = group["Stages"][key]
                meta = json.loads(g.attrs["meta"])
                self._stages_meta.append(meta)
                if "index" not in g:
                    continue
                currents_channels = json.loads(g["currents"].attrs["channels"])
                pressure_channels = json.loads(g["pressures"].attrs["channels"])
                setpoint_channels = json.loads(g["setpoints"].attrs["channels"])
                for j in range(len(g["index"])):
                    self._records.append(dict(
                        stage=meta["name"], index=int(g["index"][j]), kind=g["kind"].asstr()[j], reason=g["reason"].asstr()[j],
                        valid=bool(g["valid"][j]), delta=g["delta"][j], time=float(g["time"][j]), begin=float(g["begin"][j]),
                        objective=float(g["objective"][j]), objective_sem=float(g["objective_sem"][j]),
                        incident=float(g["incident"][j]), model_objective=float(g["model_objective"][j]),
                        transmitted=float(g["transmitted"][j]) if "transmitted" in g else math.nan,
                        currents={c: tuple(g["currents"][j, k]) for k, c in enumerate(currents_channels)},
                        pressures={c: tuple(g["pressures"][j, k]) for k, c in enumerate(pressure_channels)},
                        setpoints={c: float(g["setpoints"][j, k]) for k, c in enumerate(setpoint_channels)}))
        if self.panel is not None:
            self.panel.result.setText(self.result_summary())
        return True

    def generatePythonPlotCode(self):
        return f'''import h5py, json
import matplotlib.pyplot as plt

with h5py.File({str(self.file)!r}, "r") as f:
    group = f[{self.name!r}]
    fig, ax = plt.subplots(constrained_layout=True)
    for key in sorted(group["Stages"]):
        stage = group["Stages"][key]
        if "index" not in stage:
            continue
        valid = stage["valid"][:]
        ax.plot(stage["index"][:][valid], stage["objective"][:][valid], ".", label=stage.attrs["name"])
    ax.set_xlabel("Measurement")
    ax.set_ylabel("Criterion")
    ax.legend()
    plt.show()
'''
