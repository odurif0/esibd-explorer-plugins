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

import contextlib
import html
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
from PyQt6.QtCore import QObject, Qt, QThread, QTimer, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QDesktopServices, QFont, QFontDatabase
from PyQt6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QSizePolicy, QSpinBox,
                             QTableWidget, QVBoxLayout, QWidget)

from esibd.core import INOUT, PARAMETERTYPE, PRINT, TreeWidget, getTestMode, parameterDict, plotting
from esibd.plugins import Plugin, Scan, SettingsManager

_RUNTIME_FILES = ("__init__.py", "_engine.py", "_simulator.py", "_beamline.py", "_log.py", "_native_engine.py", "_native_worker.py",
                  "_quadrupole.py", "_filter_rules.py")


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
        return tuple(importlib.import_module(f"{name}.{part}") for part in ("_engine", "_simulator", "_beamline", "_log", "_native_engine", "_native_worker",
                                                                             "_filter_rules"))
    except BaseException:
        for key in [key for key in sys.modules if key == name or key.startswith(name + ".")]:
            sys.modules.pop(key, None)
        raise


_engine, _simulator, _beamline, _log, _native_engine, _native_worker, _filter_rules = _load_runtime()

LEVEL_LABELS = (("fine", "Fine"), ("normal", "Normal"), ("wide", "Wide"))
SIMULATION_SETTINGS = dict(settle_s=0.5, average_s=1.0, seed=1)


def providePlugins():
    return [Transmission]


def _finite(value):
    try:
        return value is not None and not isinstance(value, bool) and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


# Plot axes (user, 2026-11-20): a current near 1e-11 A wrote its exponent on every tick ("2 × 10⁻¹¹") or above the
# axis ("1e−11"), taking the room of the plot. Currents are shown in the unit that suits them, ticks as plain numbers.
_CURRENT_UNITS = ((1.0, "A"), (1e-3, "mA"), (1e-6, "µA"), (1e-9, "nA"), (1e-12, "pA"), (1e-15, "fA"))


def _current_unit(values):
    """(factor, unit) bringing the largest current between 1 and 1000 in that unit."""
    peak = max((abs(float(v)) for v in values if _finite(v) and float(v) != 0.0), default=0.0)
    for size, unit in _CURRENT_UNITS:
        if peak >= size:
            return 1.0 / size, unit
    return 1.0 / _CURRENT_UNITS[-1][0], _CURRENT_UNITS[-1][1]


def _plain_log_ticks(axis):
    """Plain numbers on a log axis (0.5, 2, 10); matplotlib still thins the minor labels over several decades."""
    from matplotlib.ticker import LogFormatter

    class PlainLog(LogFormatter):
        def _num_to_string(self, x, vmin, vmax):
            return f"{x:g}"
    axis.set_major_formatter(PlainLog())
    axis.set_minor_formatter(PlainLog())


def _decade_limits(ax, values, decades=2):
    """Whole decades, at least ``decades``: only 10ⁿ labels, never "4 × 10⁻⁶" on every minor tick."""
    from matplotlib.ticker import NullFormatter
    positive = [float(v) for v in values if _finite(v) and float(v) > 0]
    if not positive:
        return
    low, high = math.floor(math.log10(min(positive))), math.ceil(math.log10(max(positive)))
    missing = decades - (high - low)
    if missing > 0:
        low, high = low - (missing + 1) // 2, high + missing // 2
    ax.set_ylim(10.0 ** low, 10.0 ** high)
    ax.yaxis.set_minor_formatter(NullFormatter())


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


def manual_control(channel) -> bool:
    """Set by its operator, never by Explorer: Explorer evaluates only inactive channels that have an
    equation. AMPR shows this flag as "Manual"; on 2026-10-26 every AMPR channel had it unticked, with
    no equation, and "Entry_IF1: enable it as an active real channel" blocked every start."""
    return bool(channel.active) or not str(getattr(channel, "equation", "") or "").strip()


def device_on(device):
    """ON for a device with an ON/OFF button; connected for any other device."""
    if getattr(device, "useOnOffLogic", False):
        return bool(device.isOn())
    initialized = getattr(device, "initialized", None)
    return bool(initialized) if isinstance(initialized, (bool, np.bool_)) else bool(device.isOn())


def output_off(channel):
    """A PSU-like output gate reported OFF (its readback is then NaN and could not show it)."""
    state = getattr(channel, "output_state", None)
    return isinstance(state, str) and state.strip().upper() == "OFF"


def device_busy(device):
    """What the device plugin is busy with (initialization, ON/OFF transition…), or ''."""
    controller = getattr(device, "controller", None)
    return next((what for flag, what in DEVICE_BUSY.items() if _flag(controller, flag)), "")


AMX_PAIR_LINKS = (("CH0-CH1", "psu_ch01"), ("CH2-CH3", "psu_ch23"))


def is_amx(device):
    """AMX and AMX HD plugins: each output pair is fed by the PSU linked in their Settings."""
    try:
        return all(hasattr(device, attr) for _, attr in AMX_PAIR_LINKS)
    except Exception:  # noqa: BLE001 - a plugin whose Settings are not loaded yet
        return False


def amx_pairs(plugins):
    """Every AMX output (channel pair CH0-CH1 or CH2-CH3) with its PSU rails, from Settings and channels only.

    The two lines of an AMX output drive the two rod pairs of one quadrupole, switching in opposite phase
    between the two outputs of the PSU linked to it (PSU CH0 = Vpos, CH1 = Vneg): one RF amplitude, whose
    two rails move together; moving one alone would make the RF asymmetric."""
    plugins = list(plugins)
    result = []
    for amx in plugins:
        if not is_amx(amx):
            continue
        for pair, attr in AMX_PAIR_LINKS:
            psu_name = str(getattr(amx, attr, "None") or "None")
            entry = dict(amx=amx.name, device=amx, pair=pair, psu=None, rails=None, problem="")
            matches = [p for p in plugins if getattr(p, "name", None) == psu_name]
            if psu_name in ("", "None"):
                entry["problem"] = f"no PSU linked to {amx.name} {pair} (AMX Settings)"
            elif len(matches) != 1:
                entry["problem"] = f"{psu_name}, linked to {amx.name} {pair}, is not loaded"
            else:
                entry["psu"] = psu_name
                rails = []
                for number in (0, 1):
                    found = [c for c in matches[0].getChannels() if getattr(c, "real", True)
                             and callable(getattr(c, "channel_number", None)) and c.channel_number() == number]
                    if len(found) != 1:
                        entry["problem"] = f"{psu_name} CH{number} is missing or ambiguous"
                        break
                    rails.append(found[0].name)
                else:
                    entry["rails"] = tuple(rails)
            result.append(entry)
    return result


def is_esi_channel(channel):
    """ESI source channels: HV with unconfirmed discharge, and heater temperatures that start heating."""
    try:
        device = channel.getDevice()
    except Exception:  # noqa: BLE001 - a channel without a device is not an ESI channel
        device = None
    return getattr(device, "name", "") == "ESI" or callable(getattr(channel, "is_heat_channel", None))


def current_source_problem(device_of, detector, spray):
    """The spray current is read on the ESI, never on the DMMR; the detector is never an ESI current.

    ``device_of`` maps channel names to their device; an unknown device is not judged.
    """
    spray_device = device_of.get(spray) if spray else None
    if spray_device not in (None, "ESI"):
        return (f"The spray current is read on the ESI, never on {spray_device}: choose an ESI current "
                f"(ESI_HV1_I or ESI_HV2_I) instead of {spray}.")
    if detector and device_of.get(detector) == "ESI":
        return (f"{detector} is the ESI spray current: the detector is the transmitted current "
                "(DMMR), never the ESI.")
    return ""


def read_mscan(path, detector=None):
    """Amplitude (V), current (A), detector name and rail channels of a saved MScan file.

    MScan records every enabled DMMR module (2026-11-03): take the column of ``detector``, the
    beamline's detector channel, when the file has it; else the reference module of an older file;
    else the first column.
    """
    with h5py.File(path, "r") as handle:
        if "MScan" not in handle:
            raise ValueError(f"{Path(path).name} is not an MScan file")
        group = handle["MScan"]
        amplitude = np.asarray(group["Input Channels"]["Amplitude"][:], float)
        outputs = group["Output Channels"]
        validation = group.get("Validation")
        reference = str(validation.attrs.get("reference_channel", "")) if validation is not None else ""
        names = list(outputs)
        chosen = next((name for name in (detector, reference) if name and name in names), names[0])
        current = np.asarray(outputs[chosen][:], float)
        rails = []
        if validation is not None and "setup" in validation.attrs:
            setup = json.loads(validation.attrs["setup"])
            rails = [rail["channel"] for rail in setup.get("rails", [])]
    return amplitude, current, chosen, rails


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
            if self.plugin._closing:
                raise _engine.InstrumentError("Transmission is closing; Explorer action refused")
            if call.expired.is_set():
                raise _engine.InstrumentError("Explorer request expired before it ran")
            call.result = call.action()
        except Exception as exc:  # noqa: BLE001 - forwarded to the worker
            call.error = exc
        finally:
            call.done.set()

    @pyqtSlot(str)
    def set_status(self, text):
        if not self.plugin._closing:
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
    QUANTIZATION_V = 0.05  # Beyond it, a PSU setpoint readback is not the rounding of the request.
    # A PSU shows no hardware voltage limit from a write until its next housekeeping read (every 2 s): on 2026-11-20
    # (lab) a run stopped right after moving PSU_E, "hardware voltage limit not read yet". The last limit read in the
    # run stands meanwhile; one never read is waited for this long.
    LIMIT_WAIT_S = 5.0

    def __init__(self, plugin, config, cancel):
        self.plugin, self.config, self.cancel = plugin, config, cancel
        self.bridge = plugin._bridge
        manager = plugin.pluginManager.DeviceManager
        self.driven = {name: self._resolve(manager, name) for name in config.driven_channels}
        for name, channel in self.driven.items():
            if is_esi_channel(channel):
                raise _engine.ConfigError(f"{name}: ESI channels are not driven by Transmission (HV discharge not "
                                          "confirmed; a temperature setpoint starts the heater). They can be measured.")
        self.measured = {name: self._resolve(manager, name) for name in config.measured_channels}
        self.switches = self._amx_switches(plugin, config)
        self.expected = {}
        self.revisions = {}
        self.log = None
        self._latency = []
        self._window_details = {}
        self._ceilings = {}  # PSU channel → last hardware voltage limit read in this run.

    def _amx_switches(self, plugin, config):
        """The AMX of every driven output. An AMX output is one RF drive: a setting moves both of its rails,
        with the same gain, and nothing else; an AMX pulser channel is never driven."""
        for name, channel in self.driven.items():
            if is_amx(channel.getDevice()):
                raise _engine.ConfigError(f"{name}: AMX pulser channels are not driven; an RF amplitude is the PSU rails "
                                          "of an AMX output")
        settings = [(knob.name, knob.channels, False) for stage in config.stages for knob in stage.knobs]
        if config.target is not None:
            # A selected m/z (2026-11-24) moves its filters' rails and rods together: each AMX output in it still
            # moves both of its rails with the same gain (a symmetric RF), next to the other electrodes.
            settings.append((f"{config.target.filter_name or 'Mass filter'} RF amplitude", config.target.channels, True))
        switches = {}
        for entry in amx_pairs(getattr(plugin.pluginManager, "plugins", None) or []):
            if not entry["rails"]:
                continue
            rails = [rail.strip().lower() for rail in entry["rails"]]
            for name, channels, target in settings:
                gains = {channel.strip().lower(): gain for channel, gain in channels.items()}
                if not set(rails) & set(gains):
                    continue
                others_allowed = target and set(rails) <= set(gains)
                if (set(gains) != set(rails) and not others_allowed) or gains[rails[0]] != gains[rails[1]]:
                    raise _engine.ConfigError(
                        f"{name}: {entry['amx']} {entry['pair']} is one RF drive; move both rails "
                        f"{' and '.join(entry['rails'])} together, with the same gain, and nothing else")
                switches[entry["amx"]] = entry["device"]
        return switches

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
        try:
            result = self.plugin._dispatch_gui_call(call, self.GUI_TIMEOUT_S)
        except _engine.InstrumentError:
            if call.expired.is_set() and not self.plugin._closing:
                self._record("gui_timeout", waited_s=self.GUI_TIMEOUT_S, action=getattr(action, "__qualname__", ""))
            raise
        if "start" in timing:
            latency, duration = timing["start"] - posted, timing["end"] - timing["start"]
            self._latency.append((latency, duration))
            if latency > self.SLOW_GUI_S:
                self._record("gui_slow", latency_s=latency, duration_s=duration, action=getattr(action, "__qualname__", ""))
        return result

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
                if _finite(ceiling) and float(ceiling) > 0:
                    self._ceilings[channel] = float(ceiling)
                if channel not in self._ceilings:
                    return c.getDevice().name  # Not read yet: waited for.
                lo, hi = max(lo, 0.0), min(hi, self._ceilings[channel])
                if lo >= hi:
                    raise _engine.InstrumentError(f"{channel}: no admissible voltage range in {c.getDevice().name}")
            return lo, hi
        deadline = time.monotonic() + self.LIMIT_WAIT_S
        while True:
            result = self._gui(read)
            if not isinstance(result, str):
                return result
            if time.monotonic() >= deadline:
                raise _engine.InstrumentError(f"{channel}: hardware voltage limit not read from {result} "
                                              f"after {self.LIMIT_WAIT_S:g} s; is it ON?")
            self.wait(0.25)

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
                self.revisions[name] = getattr(channel, "voltage_request_revision", None)
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
            if not (channel.enabled and channel.real):
                raise _engine.InstrumentError(f"{name}: enable this real channel in {device.name}")
            if not manual_control(channel):
                raise _engine.InstrumentError(f"{name}: driven by its equation in {device.name}; "
                                              "tick Manual there or clear the equation")
            if output_off(channel):
                raise _engine.InstrumentError(f"{name}: output OFF in {device.name}; turn it ON in the {device.name} panel")
            controller = getattr(device, "controller", None)
            for flag, what in self.DEVICE_BUSY.items():
                if _flag(controller, flag):
                    raise _engine.InstrumentError(f"{device.name}: {what} in progress (driven channel {name})")
            current, ilim = getattr(channel, "current_readback", None), getattr(channel, "current_limit_readback", None)
            if _finite(current) and _finite(ilim) and float(ilim) > 0 and float(current) >= float(ilim):
                # A rail at its current limit: short, discharge or overload. As MScan, no HV OFF from here.
                raise _engine.InstrumentError(f"{name}: output current {float(current) * 1000:g} mA reached Ilim "
                                              f"{float(ilim) * 1000:g} mA in {device.name}")
            expected, revision = self.expected.get(name), self.revisions.get(name)
            if revision is None:
                changed = expected is not None and not math.isclose(float(channel.value), expected, rel_tol=1e-9, abs_tol=1e-9)
            else:
                # A PSU publishes its quantized setpoint on the channel (2026-10-30 lab: 89.25315 V reads
                # back 89.25 V and stopped the run). As in MScan, only a new request is an outside edit.
                changed = (channel.voltage_request_revision != revision
                           or abs(float(channel.value) - expected) > self.QUANTIZATION_V)
            if changed:
                raise _engine.InstrumentError(f"{name}: setpoint changed outside the optimizer ({expected:g} -> {float(channel.value):g})")
        for name, device in self.switches.items():
            # Without its AMX switching, the rails reach no electrode: the RF amplitude would not exist.
            if not device_on(device):
                raise _engine.InstrumentError(f"{name} is OFF (it switches the RF rails); turn it ON in the {name} panel")
            busy = device_busy(device)
            if busy:
                raise _engine.InstrumentError(f"{name}: {busy} in progress (it switches the RF rails)")
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
    """Map each element setting, the detector, the spray current and the gauges to Explorer channels."""

    NONE = "—"

    def __init__(self, plugin, beamline):
        super().__init__()
        self.plugin = plugin
        self.setWindowTitle("Transmission — beamline")
        self.resize(900, 760)
        self.pairs = plugin.amx_pairs()
        known = {(entry["amx"], entry["pair"]): entry["rails"] for entry in self.pairs if entry["rails"]}
        beamline = _beamline.migrate(beamline, known)  # An RF saved as the two rails of a pair shows that pair.
        driven, currents, pressures = plugin.channel_lists()
        self.device_of = dict(beamline["devices"])
        for channel in plugin.pluginManager.DeviceManager.channels():
            try:
                self.device_of[channel.name] = channel.getDevice().name
            except Exception:  # noqa: BLE001 - a channel without a device: nothing to remember
                pass
        layout = QVBoxLayout(self)
        intro = QLabel("<b>Settings</b> — the channel(s) of each element. Each RF amplitude is an AMX output "
                       "(CH0-CH1 or CH2-CH3): its two lines drive the rod pairs of the quadrupole, switching in "
                       "opposite phase between the two outputs of the PSU linked to it in the AMX Settings, which "
                       "move together (Vpos = Vneg = A, as in MScan). Choose the AMX output only.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.knobs = QTableWidget(len(_beamline.KNOBS), 5)
        self.knobs.setHorizontalHeaderLabels(["Element", "Setting", "Channel or AMX output", "Second channel", "Gain"])
        self.knob_widgets = {}
        self.pair_widgets = {}
        for row, (key, element, label, kind) in enumerate(_beamline.KNOBS):
            if kind == "rf":
                for column, text in enumerate((element, label)):
                    self.knobs.setCellWidget(row, column, QLabel(text))
                combo, rails = self._pair_combo(beamline["rf"].get(key)), QLabel()
                combo.currentIndexChanged.connect(lambda _index, combo=combo, rails=rails: self._show_rails(combo, rails))
                self._show_rails(combo, rails)
                self.knobs.setCellWidget(row, 2, combo)
                self.knobs.setSpan(row, 3, 1, 2)
                self.knobs.setCellWidget(row, 3, rails)
                self.pair_widgets[key] = combo
                continue
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
        detector_intro = QLabel("<b>Detector</b> — the one current measured and maximized. Standard: after Q4. "
                                "Placed right behind an aperture, it optimizes what passes through it: only the "
                                "elements before it, that aperture included, are then optimized.")
        detector_intro.setWordWrap(True)
        layout.addWidget(detector_intro)
        row = QHBoxLayout()
        detector = beamline["detector"]
        row.addWidget(QLabel("Current channel"))
        # Physically the spray current is read on the ESI and the detector never is (lab, 2026-10-22).
        spray_currents = [name for name in currents if self.device_of.get(name) == "ESI"]
        detector_currents = [name for name in currents if name not in spray_currents]
        self.detector_channel = self._combo(detector_currents, detector.get("channel"))
        row.addWidget(self.detector_channel, 1)
        row.addWidget(QLabel("Position"))
        self.detector_position = QComboBox()
        for key, label in _beamline.DETECTOR_POSITIONS:
            self.detector_position.addItem(label, key)
        self.detector_position.setCurrentIndex(self.detector_position.findData(detector.get("position", "after_q4")))
        row.addWidget(self.detector_position, 1)
        layout.addLayout(row)
        spray_intro = QLabel("<b>Spray current</b> — the electrospray current must be stable while optimizing "
                             "(30–60 nA by default). Every averaging window is measured again until the spray lies in "
                             "this range and fluctuates by at most 20 %; after 2 min without a stable spray the "
                             "optimization stops and returns to the stage start.")
        spray_intro.setWordWrap(True)
        layout.addWidget(spray_intro)
        row = QHBoxLayout()
        spray = beamline["spray"]
        low, high = spray.get("range_na", _beamline.SPRAY_RANGE_NA)
        row.addWidget(QLabel("Current channel"))
        self.spray_channel = self._combo(spray_currents, spray.get("channel"))
        self.spray_channel.setToolTip("The ESI HV current of the emitter (ESI plugin), read at 1 Hz or faster. "
                                      "It is always read on the ESI, never on the DMMR.")
        row.addWidget(self.spray_channel, 1)
        row.addWidget(QLabel("Minimum (nA)"))
        self.spray_low = QLineEdit(f"{low:g}")
        row.addWidget(self.spray_low)
        row.addWidget(QLabel("Maximum (nA)"))
        self.spray_high = QLineEdit(f"{high:g}")
        row.addWidget(self.spray_high)
        layout.addLayout(row)
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

    @staticmethod
    def pair_key(amx, pair):
        """Item data of a pair choice (Qt finds strings, not Python tuples)."""
        return f"{amx}\t{pair}"

    def _pair_combo(self, current):
        combo = QComboBox()
        combo.addItem(self.NONE, "")
        for entry in self.pairs:  # AMX outputs only: the PSU of each follows from the AMX Settings.
            combo.addItem(_beamline.pair_label(entry["amx"], entry["pair"]), self.pair_key(entry["amx"], entry["pair"]))
        if current:
            wanted = self.pair_key(current["amx"], current["pair"])
            index = combo.findData(wanted)
            if index < 0:  # Keep a mapped pair even if its AMX is not loaded now.
                combo.addItem(f"{_beamline.pair_label(current['amx'], current['pair'])} · not loaded", wanted)
                index = combo.count() - 1
            combo.setCurrentIndex(index)
        return combo

    def _show_rails(self, combo, label):
        data = combo.currentData() or ""
        entry = next((e for e in self.pairs if self.pair_key(e["amx"], e["pair"]) == data), None)
        if not data:
            label.setText("")
        elif entry is None:
            label.setText(f"⚠ {data.split(chr(9))[0]} is not loaded")
        elif entry["rails"] is None:
            label.setText(f"⚠ {entry['problem']}")
        else:
            label.setText(f"{entry['psu']} (from the {entry['amx']} Settings): Vpos {entry['rails'][0]}, "
                          f"Vneg {entry['rails'][1]}, both = A")
        label.setToolTip(label.text())

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
        rf = {}
        for key, combo in self.pair_widgets.items():
            if combo.currentData():
                amx, pair = combo.currentData().split("\t")
                rf[key] = {"amx": amx, "pair": pair}
        detector = ({"channel": self.detector_channel.currentText(), "position": self.detector_position.currentData()}
                    if self.detector_channel.currentText() != self.NONE else {})
        spray = {}
        problem = current_source_problem(self.device_of, detector.get("channel"), self.spray_channel.currentText())
        if problem:
            raise ValueError(problem)
        if self.spray_channel.currentText() != self.NONE:
            try:
                spray = {"channel": self.spray_channel.currentText(),
                         "range_na": [float(self.spray_low.text()), float(self.spray_high.text())]}
            except ValueError:
                raise ValueError("Spray current: enter the minimum and maximum in nA") from None
        gauges = {}
        for name, (watch, low, high) in self.gauge_widgets.items():
            if watch.isChecked():
                try:
                    gauges[name] = [float(low.text()), float(high.text())]
                except ValueError:
                    raise ValueError(f"Gauge {name}: enter the minimum and maximum in mbar") from None
        return _beamline.normalize(dict(polarity=1 if self.polarity.currentIndex() == 0 else -1,
                                        knobs=knobs, rf=rf, detector=detector, spray=spray, gauges=gauges,
                                        devices=self.device_of))

    def accept(self):
        try:
            beamline = self.beamline()
            rails = {c for entry in self.pairs if entry["rails"] for c in entry["rails"]}
            for key, channels in beamline["knobs"].items():
                used = sorted(set(channels) & rails)
                if used:
                    raise ValueError(f"{_beamline.LABEL[key]}: {', '.join(used)} is a rail of an AMX output; "
                                     "it moves with that output and cannot be a separate setting")
        except ValueError as exc:
            self.message.setText(f"<b>{exc}</b>")
            return
        self.result_beamline = beamline
        super().accept()


class _SpectrumSignals(QObject):
    point = pyqtSignal(float, float)
    done = pyqtSignal(object)
    text = pyqtSignal(str)


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
        beamline = self.plugin.active_beamline()
        try:
            amplitude, current, detector, rails = read_mscan(path, (beamline.get("detector") or {}).get("channel"))
        except (OSError, KeyError, ValueError, IndexError) as exc:
            self.info.setText(f"Cannot read {Path(path).name}: {exc}")
            self.plugin.session("mscan_unreadable", path=str(path), error=str(exc))
            return
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
            if not self.plugin._closing:
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
        factor, unit = _current_unit(self.current)
        if self.amplitude:
            self.axes.plot(self.amplitude, np.asarray(self.current, dtype=float) * factor, marker=".", linewidth=1)
        if self.selection:
            center, width = self.selection
            self.axes.axvline(center, color="tab:red")
            self.axes.axvspan(center - width, center + width, color="tab:red", alpha=0.15)
        self.axes.set_xlabel("Filter amplitude A (V)")
        self.axes.set_ylabel(f"Current after the filter ({unit})")
        self.axes.ticklabel_format(axis="y", useOffset=False)
        self.axes.set_title(title, fontsize=9)
        self.canvas.draw_idle()

    def reject(self):
        self.cancel.set()
        super().reject()


# ============================================================================ panel

class _MassFilterDialog(QDialog):
    """Filter… (m/z selection): the m/z to select, or a peak picked in volts, the filter quadrupoles, and the settings the
    rules give (user, 2026-11-24: the finest filter of one m/z on Q2 and/or Q4, the frequency optimized too)."""

    FILTERS = (("Q2", ("q2",)), ("Q4", ("q4",)), ("Q2 + Q4", ("q2", "q4")))

    def __init__(self, plugin):
        super().__init__(plugin.panel)
        self.plugin, self.source = plugin, ""
        self.setWindowTitle("m/z selection")
        state = plugin.state
        layout = QVBoxLayout(self)
        form = QGridLayout()
        self.by_mz, self.by_volts = QRadioButton("m/z"), QRadioButton("peak in volts")
        group = QButtonGroup(self)
        group.addButton(self.by_mz)
        group.addButton(self.by_volts)
        self.mz = QDoubleSpinBox()
        self.mz.setRange(10.0, 100000.0)
        self.mz.setDecimals(2)
        self.mz.setValue(float(state["mz"]))
        row = QHBoxLayout()
        for widget in (self.by_mz, self.mz):
            row.addWidget(widget)
        row.addSpacing(16)
        row.addWidget(self.by_volts)
        row.addStretch()
        form.addWidget(QLabel("Select by"), 0, 0)
        form.addLayout(row, 0, 1)
        self.quads, quads = {}, QButtonGroup(self)
        row = QHBoxLayout()
        for label, keys in self.FILTERS:
            button = QRadioButton(label)
            quads.addButton(button)
            row.addWidget(button)
            self.quads[keys] = button
        row.addStretch()
        self.quads.get(tuple(state["filters"]), self.quads[("q2",)]).setChecked(True)
        form.addWidget(QLabel("Filter on"), 1, 0)
        form.addLayout(row, 1, 1)
        self.frequency = QCheckBox("optimize (one AMX frequency for Q2 and Q4)")
        self.frequency.setChecked(bool(state["optimize_frequency"]))
        form.addWidget(QLabel("Frequency"), 2, 0)
        form.addWidget(self.frequency, 2, 1)
        self.filter = QComboBox()
        for key, label in _beamline.filters(plugin.active_beamline()):
            self.filter.addItem(label, key)
        self.filter.setCurrentIndex(max(self.filter.findData(state["filter"]), 0))
        self.center, self.width = QDoubleSpinBox(), QDoubleSpinBox()
        for box, value in ((self.center, state["center"]), (self.width, state["width"])):
            box.setRange(0.0, 100000.0)
            box.setDecimals(1)
            box.setSuffix(" V")
            box.setValue(float(value or 0.0))
        self.pick = QPushButton("Pick on spectrum…")
        row = QHBoxLayout()
        for widget in (self.filter, QLabel("peak"), self.center, QLabel("±"), self.width, self.pick):
            row.addWidget(widget)
        row.addStretch()
        form.addWidget(QLabel("Peak in volts"), 3, 0)
        form.addLayout(row, 3, 1)
        layout.addLayout(form)
        rules = QGroupBox("Rules")
        self.rules = QLabel()
        self.rules.setTextFormat(Qt.TextFormat.RichText)
        self.rules.setWordWrap(True)
        self.rules.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        QVBoxLayout(rules).addWidget(self.rules)
        layout.addWidget(rules)
        self.progress = QLabel()
        self.progress.setWordWrap(True)
        layout.addWidget(self.progress)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.set_button = buttons.addButton("Set filter", QDialogButtonBox.ButtonRole.ActionRole)
        self.set_button.setToolTip("Set the filter now: its RF rails and rods, in small verified steps.")
        self.finest_button = buttons.addButton("Finest filter", QDialogButtonBox.ButtonRole.ActionRole)
        self.finest_button.setToolTip("Raise k step by step while R gains, measuring the peak at each step; "
                                      "then compare the frequencies at that R. Keeps the best.")
        self.stop_button = buttons.addButton("Stop", QDialogButtonBox.ButtonRole.ActionRole)
        self.stop_button.setEnabled(False)
        self.cancel_event = Event()
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self.set_button.clicked.connect(self.set_filter)
        self.finest_button.clicked.connect(self.finest)
        self.stop_button.clicked.connect(self.cancel_event.set)
        layout.addWidget(buttons)
        (self.by_mz if state["mass_by"] == "mz" else self.by_volts).setChecked(True)
        for signal in (self.by_mz.toggled, self.mz.valueChanged, quads.buttonToggled, self.frequency.toggled,
                       self.filter.currentIndexChanged, self.center.valueChanged, self.width.valueChanged):
            signal.connect(self.refresh)
        self.pick.clicked.connect(self.pick_peak)
        self.plan = None
        self.refresh()

    def values(self):
        return dict(mass_by="mz" if self.by_mz.isChecked() else "volts", mz=self.mz.value(),
                    filters=list(next(keys for keys, button in self.quads.items() if button.isChecked())),
                    optimize_frequency=self.frequency.isChecked(),
                    filter=self.filter.currentData() or self.plugin.state["filter"],
                    center=self.center.value(), width=self.width.value())

    def refresh(self, *_):
        mz = self.by_mz.isChecked()
        for widget in (self.mz, *self.quads.values(), self.frequency):
            widget.setEnabled(mz)
        for widget in (self.filter, self.center, self.width, self.pick):
            widget.setEnabled(not mz)
        if mz:
            self.plan, text = self.plugin.mass_plan_text(self.values())
            self.rules.setText(text)
        else:
            self.plan = None
            self.rules.setText("The filter amplitude is the peak picked in volts; its DC is left as it is.")
        ready = mz and self.plan is not None and not self.plan.refused and not self.plugin._busy()
        self.set_button.setEnabled(ready)
        self.finest_button.setEnabled(ready)

    def pick_peak(self):
        dialog = _SpectrumDialog(self.plugin, self.filter.currentData(), self.center.value() or None)
        if dialog.exec() and dialog.selection:
            self.filter.setCurrentIndex(max(self.filter.findData(dialog.filter_key()), 0))
            self.center.setValue(float(dialog.selection[0]))
            self.width.setValue(float(dialog.selection[1]))
            self.source = dialog.source

    def set_filter(self):
        self.set_button.setEnabled(False)
        self.progress.setText("Setting the filter…")
        self.plugin.set_filter(self.values(), self._done)

    def finest(self):
        self.set_button.setEnabled(False)
        self.finest_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.cancel_event.clear()
        self.progress.setText("Finest filter: setting the starting k…")
        self.plugin.finest_filter(self.values(), self.progress.setText, self._done, self.cancel_event)

    def _done(self, text):
        self.stop_button.setEnabled(False)
        self.progress.setText(text)
        self.refresh()


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
        self.mass = QRadioButton("m/z selection")
        self.modes = QButtonGroup(self)
        self.modes.addButton(self.total)
        self.modes.addButton(self.mass)
        grid.addWidget(self.total, 0, 0, 1, 3)
        grid.addWidget(self.mass, 1, 0)
        # The choices of the filter live in Filter… (user, 2026-11-24): an m/z or a peak in volts, Q2 / Q4.
        self.mass_summary = QLabel()
        self.mass_summary.setWordWrap(True)
        grid.addWidget(self.mass_summary, 1, 1)
        self.mass_filter = QPushButton("Filter…")
        self.mass_filter.setToolTip("Choose the m/z, or a peak in volts, and the filter quadrupoles.")
        grid.addWidget(self.mass_filter, 1, 2)
        grid.setColumnStretch(1, 1)
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

    Each stage maximizes the current of the single detector (or, for a selected
    mass, the height of its peak). The detector normally sits after Q4; behind an
    aperture, only the elements before it are optimized. Moves are split into
    small steps that wait for the readbacks; the best point is adopted only after
    paired A/B measurements show a significant gain.
    """

    FOLDER = "Transmission"  # <Data path>/Transmission/<YYYY-MM-DD>/, never Explorer's session fields, as MScan.

    documentation = """Optimizes the ion transmission through the beamline apertures, stage by stage.
    1. Configure the beamline once (which channel drives each element, the detector channel and its position).
    2. Choose the target: the total ion current, or a mass selected by a quadrupole (pick its peak on an MScan
       spectrum or a quick sweep; amplitudes in volts, m/z calibration in MScan).
    3. Choose the stages and the exploration, then Optimize. Keep or Revert at the end.
    Simulation runs the built-in simulated beamline without touching any device.
    The soft interlocks only stop the optimization; the protective interlock belongs in the hardware."""

    name = "Transmission"
    version = "0.6.3"
    supportedVersion = "1.0"
    iconFile = "transmission.png"
    useInvalidWhileWaiting = False
    MARKERS = {"initial": "s", "probe": "^", "explore": "o", "reference": "D", "verify": "*"}
    PLOT_INTERVAL_S = 3.0  # Live redraws at most this often, and never during an averaging window.
    STATE_DEFAULTS = dict(mode="total", filter="q2", center=0.0, width=0.0, level="normal", skip=[],
                          mass_by="volts", mz=922.0, filters=["q2"], optimize_frequency=True, mz_k=0.0,
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
        self._native_lock = Lock()
        self._native_workers = {}  # Includes children being configured, not only running optimizers.
        self._gui_calls = set()
        self._closing = False
        self._sweep_cancel = None
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

    def _new_native_optimizer(self, config, instrument, **kwargs):
        with self._native_lock:
            if self._closing:
                raise _engine.InstrumentError("Transmission is closing")
        worker = _native_worker.NativeWorkerProxy(Path(__file__).resolve().parent, "transmission", {},
                                                  log_dir=self.log_directory())
        try:
            with self._native_lock:
                if self._closing:
                    raise _engine.InstrumentError("Transmission closed during native startup")
                self._native_workers[worker] = None
            optimizer = _native_engine.Optimizer(config, instrument, worker=worker, **kwargs)
            with self._native_lock:
                if self._closing:
                    raise _engine.InstrumentError("Transmission closed during native configuration")
                self._native_workers[worker] = optimizer
            return optimizer
        except BaseException:
            try:
                worker.close(grace_s=0)
            finally:
                with self._native_lock:
                    self._native_workers.pop(worker, None)
            raise

    def _request_native_stop(self):
        self._cancel.set()
        with self._native_lock:
            sweep_cancel = self._sweep_cancel
            optimizers = [o for o in self._native_workers.values() if o is not None]
        if sweep_cancel is not None:
            sweep_cancel.set()
        for optimizer in optimizers:
            optimizer.request_stop()  # Also interrupts an in-flight native GP fit.

    def _native_active(self):
        with self._native_lock:
            return any(o is None or o._active for o in self._native_workers.values())

    @staticmethod
    def _native_available(optimizer):
        return (optimizer is not None and not optimizer._failed and not optimizer._closed
                and not optimizer.worker.closed)

    def _retire_native_optimizer(self, optimizer):
        if optimizer is None:
            return True
        try:
            if not optimizer.close(close_worker=True):
                return False
        except Exception as exc:  # noqa: BLE001 - cleanup must not leave the scan panel locked
            self.print(f"Native worker cleanup failed: {exc}; hardware state is not confirmed.", flag=PRINT.WARNING)
            return False
        finally:
            if optimizer.worker.closed:
                with self._native_lock:
                    self._native_workers.pop(optimizer.worker, None)
        return optimizer.worker.closed

    def session(self, event, **data):
        """GUI actions and run outcomes, across sessions (rotating file). Never raises."""
        if self._closing and event not in ("plugin_close", "native_force_close"):
            return
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
        if self._closing:
            return
        self._plot_pending = True
        self._plot_force = self._plot_force or force
        self._maybe_plot()

    def set_measuring(self, active):
        """Called when an averaging window opens or closes; redraws wait for it to close."""
        if self._closing:
            return
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
        if self._closing:
            return
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
        panel.mass_filter.clicked.connect(self.open_mass_filter)
        panel.advanced.clicked.connect(self.edit_advanced)
        panel.logs.clicked.connect(self.open_logs)
        panel.start_devices.clicked.connect(lambda: self.start_devices())
        panel.optimize.clicked.connect(self.start_optimization)
        panel.stop.clicked.connect(self.stop_optimization)
        panel.keep.clicked.connect(self.keep)
        panel.revert.clicked.connect(self.undo)
        self._load_state()
        self._apply_state()
        for signal in (panel.total.toggled, panel.simulation.toggled, panel.levels.buttonToggled):
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
            panel.level_buttons.get(state["level"], panel.level_buttons["normal"]).setChecked(True)
        finally:
            self._updating = updating

    def panel_changed(self, *_):
        if self._updating or self.panel is None:
            return
        panel, state = self.panel, self.state
        state["mode"] = "mass" if panel.mass.isChecked() else "total"
        state["simulation"] = panel.simulation.isChecked()
        state["level"] = next((k for k, b in panel.level_buttons.items() if b.isChecked()), "normal")
        if panel.stage_boxes:
            unchecked = {k for k, box in panel.stage_boxes.items() if not box.isChecked()}
            state["skip"] = sorted({*state["skip"], *unchecked} - {k for k in panel.stage_boxes if k not in unchecked})
        self._save_state()
        self.refresh_panel()

    def amx_pairs(self):
        return amx_pairs(getattr(self.pluginManager, "plugins", None) or [])

    @staticmethod
    def _rail_lookup(pairs):
        index = {(entry["amx"], entry["pair"]): entry for entry in pairs}

        def rails(amx, pair):
            entry = index.get((amx, pair))
            if entry is None:
                raise ValueError(f"{amx} is not loaded (enable it in the Plugin Manager, then restart Explorer)")
            if entry["rails"] is None:
                raise ValueError(entry["problem"])
            return entry["rails"]
        return rails

    def resolved_beamline(self):
        """(beamline with each RF pair turned into its two rails, problems); see _beamline.resolve."""
        pairs = self.amx_pairs()
        known = {(entry["amx"], entry["pair"]): entry["rails"] for entry in pairs if entry["rails"]}
        rails = {channel for entry in pairs if entry["rails"] for channel in entry["rails"]}
        return _beamline.resolve(_beamline.migrate(self.beamline_map, known), self._rail_lookup(pairs), rails)

    def active_beamline(self):
        return _beamline.normalize(_simulator.BEAMLINE) if self._simulated() else self.resolved_beamline()[0]

    def refresh_panel(self):
        if self._closing:
            return
        panel = self.panel
        if panel is None:
            return
        updating, self._updating = self._updating, True
        try:
            beamline = self.active_beamline()
            problems = [] if self._simulated() else self.resolved_beamline()[1]
            if self._simulated():
                panel.beamline_label.setText(f"Simulation: built-in beamline ({_beamline.summary(beamline)}).")
            elif beamline["knobs"] and beamline["detector"]:
                panel.beamline_label.setText(f"✓ {_beamline.summary(beamline)}"
                                             + "".join(f"\n⚠ {problem}" for problem in problems))
            else:
                panel.beamline_label.setText("Not configured yet: choose which channels drive the elements, and the detector."
                                             + "".join(f"\n⚠ {problem}" for problem in problems))
            filters = _beamline.filters(beamline)
            if filters and self.state["filter"] not in dict(filters):
                self.state["filter"] = filters[0][0]
            running = self._busy()
            editable = not self.state["advanced"] and not running
            mass = self.state["mode"] == "mass"
            panel.mass_filter.setEnabled(mass and bool(filters) and editable)
            panel.mass_summary.setText(self.mass_summary() if mass else "")
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
        if not beamline["knobs"] or not beamline["detector"]:
            return False, "Configure the beamline first."
        if self.state["mode"] == "mass" and not _beamline.filters(beamline):
            return False, "Map a quadrupole RF amplitude and a current after it (Configure…) to select a mass."
        if self.state["mode"] == "mass" and self.state["mass_by"] == "mz":
            problem = self.mass_problem()
            if problem:
                return False, problem
        elif self.state["mode"] == "mass" and not (self.state["center"] > 0 and self.state["width"] > 0):
            return False, "Pick the peak of the selected mass first."
        if not self._selected_stages(beamline):
            return False, "Choose at least one stage." if self._plan(beamline) else "No stage can run: map a setting and a current after it."
        return True, "Optimize the selected stages."

    def _busy(self):
        return (self._closing or not self.finished or self.recording or self._reverting or self._sweeping
                or self._bringup is not None or self._native_active())

    def _plan(self, beamline):
        state = self.state
        if state["mode"] == "mass" and state["mass_by"] == "mz":
            keys = [k for k in ("q2", "q4") if k in state["filters"]] or ["q2"]
            return _beamline.stages(beamline, mode="mass", filter_key=keys[-1],
                                    exclude=tuple(f"{key}_{part}" for key in keys for part in ("rf", "offset")))
        return _beamline.stages(beamline, mode=state["mode"], filter_key=state["filter"] if state["mode"] == "mass" else None)

    def _selected_stages(self, beamline):
        return [key for key, *_ in self._plan(beamline) if key not in self.state["skip"]]

    def show_progress(self, text):
        if self.panel is not None:
            self.panel.progress.setText(text)

    def channel_lists(self):
        """(driven, currents, pressures) channel names. AMX pulser channels and the rails of AMX outputs are
        not offered as settings: an RF amplitude is chosen as an AMX output."""
        driven, currents, pressures = [], [], []
        rails = {c for entry in self.amx_pairs() if entry["rails"] for c in entry["rails"]}
        for channel in self.pluginManager.DeviceManager.channels():
            try:
                if channel.name in rails or is_amx(channel.getDevice()):
                    continue
            except Exception:  # noqa: BLE001, S110 - a channel without a device is judged by its unit below
                pass
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
        lines = [_engine.template(*self.channel_lists()).rstrip("\n"), "",
                 "# An RF amplitude is an AMX output: one knob moves both rails (PSU outputs) of that AMX output, each with gain 1.0",
                 "# (Vpos = Vneg = A, as in MScan), and nothing else."]
        for entry in self.amx_pairs():
            if entry["rails"]:
                rails = ", ".join(f'"{rail}" = 1.0' for rail in entry["rails"])
                lines.append(f"#   {_beamline.pair_label(entry['amx'], entry['pair'])}: channels = {{ {rails} }}")
            else:
                lines.append(f"#   {_beamline.pair_label(entry['amx'], entry['pair'])}: {entry['problem']}")
        return "\n".join(lines) + "\n"

    def generated_text(self):
        try:
            return self._generated()
        except ValueError as exc:
            return f"# The simple settings cannot generate a configuration yet: {exc}\n" + self.template_text()

    def _generated(self):
        state, beamline = self.state, self.active_beamline()
        if state["mode"] == "mass" and state["mass_by"] == "mz":
            # The target is replaced by the m/z target of the rules (_configuration); the RF and the rods of each
            # filter are left out of the stages, since the target owns them.
            keys = [k for k in ("q2", "q4") if k in state["filters"]]
            exclude = tuple(f"{key}_{part}" for key in keys for part in ("rf", "offset"))
            return _beamline.build(beamline, selected=self._selected_stages(beamline), level=state["level"], mode="mass",
                                   filter_key=keys[-1], center=float(state["mz"]), width=max(float(state["mz"]) / 100, 0.1),
                                   exclude=exclude, settings=SIMULATION_SETTINGS if self._simulated() else None)
        return _beamline.build(beamline, selected=self._selected_stages(beamline), level=state["level"], mode=state["mode"],
                               filter_key=state["filter"] if state["mode"] == "mass" else None,
                               center=state["center"], width=state["width"],
                               settings=SIMULATION_SETTINGS if self._simulated() else None)

    def missing_channels(self, config):
        names = {c.name.strip().lower() for c in self.pluginManager.DeviceManager.channels()}
        wanted = (*config.driven_channels, *config.measured_channels)
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
        self.state.update(filter=filter_key, center=float(center), width=float(width), mode="mass", mass_by="volts")
        self._save_state()
        self._apply_state()
        self.refresh_panel()

    # ---------------------------------------------------------------- selected m/z (2026-11-24)
    MSCAN_QUADRUPOLES = "MScan_quadrupoles.json"  # MScan's profiles: AMX output, rod pairs, r0, RF attenuation.

    def _mscan_profiles(self):
        path = Path(self.pluginManager.Settings.configPath) / self.MSCAN_QUADRUPOLES
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _rod_channel(selection, channels):
        """The AMPR channel an MScan rod pair names: '<address> — <name>' or a name; by address when renamed."""
        if not isinstance(selection, str) or selection in ("", "None"):
            return None

        def address(channel):
            try:
                return f"{channel.getDevice().name} M{channel.module_address()} CH{channel.channel_number()}"
            except Exception:  # noqa: BLE001 - not an AMPR output
                return None
        for channel in channels:
            where = address(channel)
            if where and selection in (f"{where} — {channel.name}", channel.name):
                return channel
        if " — " in selection:
            wanted = selection.split(" — ", 1)[0]
            return next((c for c in channels if address(c) == wanted), None)
        return None

    def mass_filter_context(self):
        """(quadrupoles, wiring, q1, problems) for the rules, read on the Qt thread.

        Each quadrupole comes from MScan's profile of that name: its AMX output (its PSU rails), its rod pairs
        (pair 1 = x + U), r0 and the RF attenuation; its limits from the PSU (hardware voltage limit, Ilim: read,
        never written). Transmission's own mapping of the RF must name the same AMX output. q1: (channel, value)
        of Transmission's Q1 offset, which the rods follow.
        """
        profiles, beamline = self._mscan_profiles(), self.active_beamline()
        channels = [c for c in self.pluginManager.DeviceManager.channels() if getattr(c, "real", True)]
        by_name = {c.name: c for c in channels}
        pairs = self.amx_pairs()
        quadrupoles, wiring, problems = {}, {}, []
        for name in ("Q2", "Q4"):
            profile = profiles.get(name) or {}
            entry = next((e for e in pairs if e["amx"] == profile.get("AMX") and e["pair"] == profile.get("AMX connectors")), None)
            if entry is None or not entry["rails"]:
                why = f" ({entry['problem']})" if entry is not None and entry["problem"] else ""
                problems.append(f"{name}: set its AMX output in MScan's {name} profile{why}")
                continue
            mapped = beamline["knobs"].get(f"{name.lower()}_rf")
            if mapped and {c.lower() for c in mapped} != {c.lower() for c in entry["rails"]}:
                problems.append(f"{name}: Transmission drives the RF of {', '.join(mapped)} but MScan's {name} profile "
                                f"{entry['amx']} {entry['pair']} ({', '.join(entry['rails'])}): map the same AMX output")
                continue
            rods = [self._rod_channel(profile.get(key), channels) for key in ("Quadrupole offset", "Quadrupole offset pair 2")]
            if None in rods or rods[0] is rods[1]:
                problems.append(f"{name}: set its two rod pairs in MScan's {name} profile")
                continue
            rails = [by_name.get(rail) for rail in entry["rails"]]
            limits = [float(c.hardware_voltage_limit) for c in rails if c is not None
                      and _finite(getattr(c, "hardware_voltage_limit", None)) and float(c.hardware_voltage_limit) > 0]
            ilims = [1e3 * float(c.current_limit_readback) for c in rails if c is not None
                     and _finite(getattr(c, "current_limit_readback", None)) and float(c.current_limit_readback) > 0]
            values = [float(rod.value) for rod in rods if _finite(getattr(rod, "value", None))]
            amplitudes = [float(c.value) for c in rails if c is not None and _finite(getattr(c, "value", None))]
            r0, attenuation = profile.get("Field radius r0"), profile.get("RF attenuation factor")
            quadrupoles[name] = _filter_rules.Quadrupole(
                name, float(r0) if _finite(r0) else None, float(attenuation) if _finite(attenuation) else None,
                min(limits) if limits else None, min(ilims) if ilims else None,
                sum(values) / 2 if len(values) == 2 else None, (values[0] - values[1]) / 2 if len(values) == 2 else None,
                sum(amplitudes) / len(amplitudes) if amplitudes else None)
            wiring[name] = dict(rails=tuple(entry["rails"]), rods=(rods[0].name, rods[1].name), amx=entry["amx"],
                                device=entry["device"], pair=entry["pair"])
        q1 = None
        q1_channels = beamline["knobs"].get("q1_offset") or {}
        if q1_channels:
            channel = by_name.get(next(iter(q1_channels)))
            if channel is not None and _finite(getattr(channel, "value", None)):
                q1 = (channel.name, float(channel.value))
        if q1 is None:
            problems.append("Map the Q1 offset (Configure…): the axis potential of the filter follows Q1_DC")
        return quadrupoles, wiring, q1, problems

    def mass_plan(self, values=None, k=None, frequency_hz=None):
        """(plan, wiring, q1, problem) of the m/z in ``values`` (default: the saved choice): at the AMX's frequency
        and the k kept by the last finest-filter ladder (else the rules' starting k), unless given."""
        values = dict(self.state, **(values or {}))
        quadrupoles, wiring, q1, problems = self.mass_filter_context()
        names = [key.upper() for key in ("q2", "q4") if key in values["filters"]]
        missing = [name for name in names if name not in quadrupoles]
        if not names or missing or q1 is None:
            return None, wiring, q1, "; ".join(problems) or "Choose Q2, Q4 or both"
        if len({wiring[name]["amx"] for name in names}) > 1:
            return None, wiring, q1, "Q2 and Q4 on different AMX: one frequency is needed for both"
        frequency = getattr(wiring[names[0]]["device"], "frequency_khz", None)
        if frequency_hz is None and (not _finite(frequency) or float(frequency) <= 0):
            return None, wiring, q1, f"{wiring[names[0]]['amx']}: frequency not known"
        if k is None and _finite(values.get("mz_k")) and float(values["mz_k"]) > 0:
            k = float(values["mz_k"])
        try:
            plan = _filter_rules.Rules().plan(float(values["mz"]), quadrupoles, names, q1[1],
                                              frequency_hz=frequency_hz or float(frequency) * 1e3, k=k)
        except ValueError as exc:
            return None, wiring, q1, str(exc)
        return plan, wiring, q1, plan.refused

    def mass_problem(self):
        """Why the selected m/z cannot run ('' when it can)."""
        if self._simulated():
            return "An m/z needs the instrument (MScan's quadrupoles): in simulation, pick a peak in volts."
        return self.mass_plan()[3]

    def mass_summary(self):
        state = self.state
        if state["mass_by"] != "mz":
            if not state["center"]:
                return "No peak picked yet."
            label = dict(_beamline.FILTERS).get(state["filter"], state["filter"])
            return f"Peak {state['center']:.1f} V ± {state['width']:.1f} V on {label}."
        names = " + ".join(key.upper() for key in ("q2", "q4") if key in state["filters"])
        try:
            plan, _, _, problem = self.mass_plan()
        except Exception as exc:  # noqa: BLE001 - shown, never raised into the panel
            return f"m/z {state['mz']:g} on {names}: {exc}"
        if plan is None or problem:
            return f"m/z {state['mz']:g} on {names}: {problem}"
        return (f"m/z {plan.mz:g} on {names} · k {plan.k:.4f} · {plan.frequency_hz / 1e3:.1f} kHz · R ≈ {plan.r_predicted:.0f}"
                + (" (extrapolated)" if plan.flags else ""))

    def mass_plan_text(self, values):
        """(plan or None, rich text) for the Mass filter dialog."""
        try:
            plan, wiring, q1, problem = self.mass_plan(values)
        except Exception as exc:  # noqa: BLE001
            return None, f"<span style='color:#c53030'>{html.escape(str(exc))}</span>"
        if plan is None:
            return None, f"<span style='color:#c53030'>{html.escape(problem)}</span>"
        lines = [f"R cap {plan.r_cap:.0f} (n {plan.cycles:.0f} RF periods) · starting k {plan.k:.5f}: R ≈ {plan.r_predicted:.0f}",
                 f"{plan.frequency_hz / 1e3:.3f} kHz · E {plan.energy_ev:g} eV · Q1_DC {q1[1]:.2f} V"]
        for setting in plan.settings:
            if setting.role == "filter":
                lines.append(f"{setting.name}: A {setting.amplitude_v:.2f} V · U {setting.u_v:.2f} V · x {setting.x_v:.2f} V "
                             f"· rods {setting.rods[0]:.2f} / {setting.rods[1]:.2f} V · RF ~{setting.load_ma:.0f} mA per rail")
            else:
                u_now = "" if setting.u_present_v is None else f"U {setting.u_present_v:.2f} V → 0 · "
                lines.append(f"{setting.name}: transmits all m/z above {setting.cutoff_mz:.0f} (RF only) · {u_now}"
                             f"offset to ground {setting.x_v:.2f} V · A {setting.amplitude_v:.1f} V (both optimized in its stage)")
        text = "<br>".join(html.escape(line) for line in lines)
        if plan.flags:
            text += "<br><span style='color:#dd6b20'>" + "<br>".join(html.escape(f) for f in plan.flags) + "</span>"
        if plan.refused:
            text += f"<br><span style='color:#c53030'>{html.escape(plan.refused)}</span>"
        return plan, text

    def _mz_target(self, parsed):
        """The engine's target for the selected m/z: the coordinate is the m/z; rails and rods follow the rules'
        law, the rods also follow Q1_DC."""
        plan, wiring, q1, problem = self.mass_plan()
        if plan is None or problem:
            raise _engine.ConfigError(problem or "No plan for the selected m/z")
        self.session("mz_plan", summary=plan.summary(), flags=plan.flags, k=plan.k, frequency_hz=plan.frequency_hz,
                     r_predicted=plan.r_predicted, r_cap=plan.r_cap)
        return self._target_of(plan, wiring, q1, parsed.measure, points=parsed.points,
                               spectrum_points=parsed.spectrum_points, spectrum_span=parsed.spectrum_span)

    @staticmethod
    def _target_of(plan, wiring, q1, measure, **options):
        gains, offsets, follow, origin, amplitude = _filter_rules.target_parts(plan, wiring, q1[0], q1[1])
        width = plan.mz / (2 * plan.r_predicted)  # Half-width of the band, in m/z.
        names = " + ".join(s.name for s in plan.settings if s.role == "filter")
        return _engine.Target(gains, plan.mz, width, list(measure), filter_name=f"m/z {plan.mz:g} on {names}",
                              offsets=offsets, follow=follow, follow_origin=origin, amplitude_channel=amplitude,
                              **options)

    def open_mass_filter(self):
        dialog = _MassFilterDialog(self)
        if dialog.exec():
            self.apply_mass_filter(dialog.values(), source=getattr(dialog, "source", ""))

    def apply_mass_filter(self, values, source=""):
        if (values.get("mz", self.state["mz"]) != self.state["mz"]
                or list(values.get("filters", self.state["filters"])) != list(self.state["filters"])):
            values = dict(values, mz_k=0.0)  # Another m/z or filter: the rules' starting k again.
        self.state.update({k: v for k, v in values.items() if k in self.STATE_DEFAULTS}, mode="mass")
        self.session("mass_filter", **{k: self.state[k] for k in ("mass_by", "mz", "filters", "optimize_frequency",
                                                                   "filter", "center", "width")}, source=source)
        self._save_state()
        if self.panel is not None:
            self._apply_state()
            self.refresh_panel()

    def _u_zero_jobs(self, plan, wiring, cancel):
        """Qt thread: (name, config, instrument) for each quadrupole that does not filter, sits before the detector
        and still has a resolving DC (user, 2026-11-24): its rod pairs go to their mean, U = 0 between them, their DC
        offset to ground kept; it then transmits all m/z above its RF cut-off. Behind the detector: left alone."""
        beamline, jobs = self.active_beamline(), []
        detector = beamline["detector"]["channel"] if beamline["detector"] else None
        for setting in plan.settings:
            if (setting.role != "rf only" or setting.u_present_v is None or abs(setting.u_present_v) < 0.005
                    or detector is None or not _beamline.filter_measure(beamline, setting.name.lower())):
                continue
            gains, offsets, amplitude = _filter_rules.u_zero_parts(wiring[setting.name]["rods"], setting.x_v)
            target = _engine.Target(gains, 0.0, 2.0, [detector], spectrum_points=0, offsets=offsets,
                                    amplitude_channel=amplitude, filter_name=f"{setting.name} U = 0")
            spray = beamline["spray"]
            spray = _engine.Spray(spray["channel"], *(v / 1e9 for v in spray["range_na"])) if spray else None
            config = _engine.Config([], {k: tuple(v) for k, v in beamline["gauges"].items()}, target, spray,
                                    polarity=beamline["polarity"])
            jobs.append((setting.name, config, self._instrument(config, cancel)))
        return jobs

    def _run_u_zero(self, jobs, log):
        """Worker thread: run the jobs of ``_u_zero_jobs`` (1 V steps with readbacks); an error stops what follows."""
        for name, config, instrument in jobs:
            optimizer = None
            try:
                optimizer = self._new_native_optimizer(config, instrument, log=log)
                outcome = optimizer.goto()
            finally:
                self._retire_native_optimizer(optimizer)
            if outcome["status"] != "completed":
                raise _engine.InstrumentError(f"{name}: U = 0 not set ({outcome['status']}: {outcome['reason']})")
            self.session("u_zero", quadrupole=name, rods=list(config.target.channels), status=outcome["status"])

    def set_filter(self, values, on_done):
        """Set filter (Filter…): the rails and rods of the m/z target to their values, by the native engine
        in bounded steps with readbacks, then left there. ``on_done(text)`` runs on the Qt thread."""
        if self._busy():
            on_done("Wait until the optimization has finished.")
            return
        self.apply_mass_filter(values)
        try:
            _, parsed = self._configuration()
            target = parsed.target
            beamline = self.active_beamline()
            spray = beamline["spray"]
            spray = _engine.Spray(spray["channel"], *(v / 1e9 for v in spray["range_na"])) if spray else None
            config = _engine.Config([], {k: tuple(v) for k, v in beamline["gauges"].items()}, target, spray,
                                    polarity=beamline["polarity"])
            cancel = Event()
            instrument = self._instrument(config, cancel)
            plan, wiring, _, _ = self.mass_plan()
            u_zero = self._u_zero_jobs(plan, wiring, cancel) if plan is not None else []
        except (_engine.ConfigError, _engine.InstrumentError, KeyError, ValueError) as exc:
            on_done(f"Filter not set: {exc}")
            return
        self._sweeping = True
        self.refresh_panel()
        log = self._open_run_log("set_filter", target=dict(channels=target.channels, offsets=target.offsets,
                                                          center=target.center, follow=target.follow),
                                 channels=self._channel_snapshot(instrument))
        if isinstance(instrument, ExplorerInstrument):
            instrument.log = log.write
        signals = _SpectrumSignals()
        signals.text.connect(on_done)
        self._filter_signals = signals  # Kept alive until the worker has reported.

        def work():
            optimizer = None
            try:
                self._run_u_zero(u_zero, log.write)
                optimizer = self._new_native_optimizer(config, instrument, log=log.write)
                outcome = optimizer.goto()
            except Exception as exc:  # noqa: BLE001 - reported to the dialog, kept in the log
                log.write("exception", traceback=traceback.format_exc())
                outcome = dict(status="error", reason=f"{type(exc).__name__}: {exc}")
            finally:
                self._retire_native_optimizer(optimizer)
                self._sweeping = False
                log.close()
            self.session("set_filter", status=outcome["status"], reason=outcome.get("reason", ""),
                         log=str(log.path) if log.path else None)
            if not self._closing:
                signals.text.emit("Filter set." if outcome["status"] == "completed"
                                  else f"Filter not set ({outcome['status']}): {outcome.get('reason', '')}")
                self._bridge.refresh.emit()
        Thread(target=work, name="Transmission set filter", daemon=True).start()

    FINEST_POINTS = 21  # Spectrum points across ± 3 predicted half-widths, at each k of the ladder.
    AMX_APPLY_S = 20.0  # An AMX frequency is applied by the AMX plugin in its own thread: waited for this long.

    def _set_amx_frequency(self, device, khz):
        """Write the AMX frequency as its operator would (the AMX keeps its duty), then wait until it has applied it;
        the frequency is common to both outputs (Q2 and Q4). Worker thread."""
        def write():
            if not device_on(device):
                raise _engine.InstrumentError(f"{device.name} is OFF: its frequency cannot be applied")
            device.frequency_khz = float(khz)
            return float(device.frequency_khz)
        applied = self._gui_call(write)
        began = time.monotonic()
        while time.monotonic() - began < self.AMX_APPLY_S:
            time.sleep(0.5)
            if time.monotonic() - began >= 2.0 and not self._gui_call(lambda: device_busy(device)):
                return applied
        raise _engine.InstrumentError(f"{device.name} still busy {self.AMX_APPLY_S:g} s after a frequency change")

    def finest_filter(self, values, on_progress, on_done, cancel):
        """Finest filter (Filter…, user 2026-11-24): the finest useful filter of the m/z, measured.

        From the present k, k climbs in the rules' steps; at each k the filter is set and a spectrum across the band
        measures R and the height. The ladder stops when R no longer gains 5 % per step, when the peak is no longer
        measurable (10 × the noise of its flanks), or at the closing; the best R measured wins. With the frequency
        optimized, the candidates are then compared at that R (the highest signal at equal R). The filter is left at
        the best setting and that k is kept for the next optimizations. Stopped or failed, the filter and the
        frequency return to where they started. ``on_progress`` and ``on_done`` run on the Qt thread.
        """
        if self._busy():
            on_done("Wait until the optimization has finished.")
            return
        self.apply_mass_filter(values)
        try:
            if self._simulated():
                raise _engine.ConfigError(self.mass_problem())
            start, wiring, q1, problem = self.mass_plan()
            if start is None or problem:
                raise _engine.ConfigError(problem or "No plan for the selected m/z")
            beamline = self.active_beamline()
            keys = [k for k in ("q2", "q4") if k in self.state["filters"]]
            measure = _beamline.filter_measure(beamline, keys[-1])
            if not measure:
                raise _engine.ConfigError("The detector must sit behind the filter")
            spray = beamline["spray"]
            spray = _engine.Spray(spray["channel"], *(v / 1e9 for v in spray["range_na"])) if spray else None
            config = _engine.Config([], {k: tuple(v) for k, v in beamline["gauges"].items()},
                                    self._target_of(start, wiring, q1, measure, spectrum_points=0), spray,
                                    polarity=beamline["polarity"])
            instrument = self._instrument(config, cancel)
            u_zero = self._u_zero_jobs(start, wiring, cancel)
            amx = wiring[keys[0].upper()]["device"]
            quad = next(s.name for s in start.settings if s.role == "filter")
        except (_engine.ConfigError, _engine.InstrumentError, KeyError, ValueError) as exc:
            on_done(f"Finest filter not started: {exc}")
            return
        self._sweeping = True
        self.refresh_panel()
        log = self._open_run_log("finest_filter", mz=start.mz, start=start.summary(), flags=start.flags,
                                 channels=self._channel_snapshot(instrument))
        if isinstance(instrument, ExplorerInstrument):
            instrument.log = log.write
        signals = _SpectrumSignals()
        signals.text.connect(on_progress)
        signals.done.connect(on_done)
        self._filter_signals = signals
        rules = _filter_rules.Rules()

        def plan_at(k, frequency_hz):
            plan = self._gui_call(lambda: self.mass_plan(k=k, frequency_hz=frequency_hz))[0]
            if plan is None or plan.refused:
                raise _engine.ConfigError(plan.refused if plan is not None else "No plan")
            return plan

        def work():
            optimizer, text, kept = None, "", None
            try:
                self._run_u_zero(u_zero, log.write)
                optimizer = self._new_native_optimizer(config, instrument, log=log.write)

                def measure_at(plan):
                    """Set the filter at ``plan``, then measure its band until it can be trusted (rules.measured)."""
                    if cancel.is_set():
                        raise _engine.Stopped("Stopped by the operator")
                    optimizer.retarget(self._target_of(plan, wiring, q1, measure, spectrum_points=0))
                    moved = optimizer.goto()
                    if moved["status"] != "completed":
                        raise _engine.InstrumentError(f"filter at k {plan.k:.5f}: {moved['status']} {moved['reason']}")

                    def sweep(center, half_span):
                        coordinates = list(np.linspace(max(center - half_span, 1.0), center + half_span,
                                                       self.FINEST_POINTS))
                        result = optimizer.spectrum(coordinates, measure)  # Back at the centre afterwards.
                        if result["status"] != "completed":
                            raise _engine.InstrumentError(f"spectrum at k {plan.k:.5f}: {result['status']} {result['reason']}")
                        band = _filter_rules.measure_band(result["amplitude"], result["current"])
                        log.write("finest_sweep", k=plan.k, frequency_hz=plan.frequency_hz, band=band,
                                  mz=result["amplitude"], current=result["current"])
                        return band
                    band = _filter_rules.measured(rules, plan, sweep)
                    log.write("finest_step", k=plan.k, frequency_hz=plan.frequency_hz, r_predicted=plan.r_predicted, band=band)
                    return band

                best, _, why = _filter_rules.climb(
                    rules, start.k, lambda k: plan_at(k, start.frequency_hz), measure_at,
                    report=lambda plan, band: signals.text.emit(
                        f"k {plan.k:.5f}: R {band['r']:.0f}, peak {1e12 * band['height']:.2f} pA"))
                if best is None:
                    raise _engine.InstrumentError("no measurable peak at the starting k")
                plan, band = best
                if self.state["optimize_frequency"]:
                    candidates = sorted({rules.snap_frequency(f * 1e3) for f in rules.c["frequencies_khz"]}
                                        | {start.frequency_hz})
                    r_equal = plan.r_predicted  # The same predicted R at every frequency; the measure checks it.
                    found = {start.frequency_hz: (plan, band)}
                    for frequency in candidates:
                        if frequency == start.frequency_hz:
                            continue
                        self._set_amx_frequency(amx, frequency / 1e3)
                        k_f = rules.k_for_resolution(r_equal, start.mz, frequency, quad=None)[0]
                        candidate = plan_at(k_f, frequency)
                        found[frequency] = (candidate, measure_at(candidate))
                        signals.text.emit(f"{frequency / 1e3:.3f} kHz: k {k_f:.5f}, R {found[frequency][1]['r']:.0f}, "
                                          f"peak {1e12 * found[frequency][1]['height']:.2f} pA")
                    _, (plan, band) = _filter_rules.choose_frequency(found, band["r"])
                    self._set_amx_frequency(amx, plan.frequency_hz / 1e3)
                ladder = band
                band = measure_at(plan)  # The kept setting is measured once more, never chosen on one measurement.
                signals.text.emit(f"Kept k {plan.k:.5f}: R {band['r']:.0f} again (ladder {ladder['r']:.0f})")
                kept = plan
                text = (f"Finest filter: k {plan.k:.5f} at {plan.frequency_hz / 1e3:.3f} kHz, R {band['r']:.0f} "
                        f"(ladder {ladder['r']:.0f}, model {plan.r_predicted:.0f}), peak {1e12 * band['height']:.2f} pA "
                        f"({why}). Kept for the next optimizations.")
            except Exception as exc:  # noqa: BLE001 - stopped or failed: back to the start, said and logged
                stopped = isinstance(exc, _engine.Stopped)
                log.write("exception", traceback=traceback.format_exc())
                text = "Stopped" if stopped else f"Finest filter failed: {exc}"
                try:
                    cancel.clear()
                    if optimizer is not None:
                        if amx is not None and float(getattr(amx, "frequency_khz", 0.0)) * 1e3 != start.frequency_hz:
                            self._set_amx_frequency(amx, start.frequency_hz / 1e3)
                        optimizer.retarget(self._target_of(start, wiring, q1, measure, spectrum_points=0))
                        back = optimizer.goto()
                        text += "; the filter is back at its start" if back["status"] == "completed" else \
                            f"; returning to the start failed: {back['reason']}"
                except Exception as again:  # noqa: BLE001
                    log.write("return_failed", traceback=traceback.format_exc())
                    text += f"; returning to the start failed: {again}"
                text += "."
            finally:
                self._retire_native_optimizer(optimizer)
                self._sweeping = False
                log.close()
            if kept is not None:
                self._gui_call(lambda: (self.state.update(mz_k=kept.k), self._save_state()))
            self.session("finest_filter", text=text, k=kept.k if kept else None,
                         frequency_hz=kept.frequency_hz if kept else None, log=str(log.path) if log.path else None)
            if not self._closing:
                signals.done.emit(text)
                self._bridge.refresh.emit()
        Thread(target=work, name="Transmission finest filter", daemon=True).start()

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
        with self._native_lock:
            if self._closing:
                return dict(status="error", reason="Transmission is closing", amplitude=[], current=[], sem=[])
            self._sweep_cancel = cancel
        self._sweeping = True  # Usually already set on the Qt thread by the dialog, before this worker started.
        self._bridge.refresh.emit()
        try:
            return self._quick_sweep(filter_key, amplitudes, on_point, cancel)
        finally:
            self._sweeping = False
            with self._native_lock:
                self._sweep_cancel = None
            if not self._closing:
                self._bridge.refresh.emit()

    def _quick_sweep(self, filter_key, amplitudes, on_point, cancel):
        try:
            beamline = self.active_beamline()
            measure = _beamline.filter_measure(beamline, filter_key)
            step = max(float(np.max(np.abs(np.diff(amplitudes)))) if len(amplitudes) > 1 else 1.0, 0.1)
            target = _engine.Target(beamline["knobs"][f"{filter_key}_rf"], float(amplitudes[0]), step, measure,
                                    max_step=step, spectrum_points=0)
            options = dict(SIMULATION_SETTINGS) if self._simulated() else {}
            spray = beamline["spray"]
            spray = (_engine.Spray(spray["channel"], *(v / 1e9 for v in spray["range_na"])) if spray else None)
            config = _engine.Config([], {k: tuple(v) for k, v in beamline["gauges"].items()}, target, spray,
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
        optimizer = None
        try:
            optimizer = self._new_native_optimizer(config, instrument, log=log.write,
                                          on_event=lambda kind, data: on_point(data["amplitude"], data["current"])
                                          if kind == "sweep" and not self._closing else None)
            outcome = optimizer.spectrum(amplitudes, measure)
        except Exception as exc:  # noqa: BLE001 - reported to the dialog, kept in the log
            log.write("exception", traceback=traceback.format_exc())
            outcome = dict(status="error", reason=f"{type(exc).__name__}: {exc}", amplitude=[], current=[], sem=[])
        finally:
            self._retire_native_optimizer(optimizer)
            if not self._closing:
                self._bridge.unwatch.emit()
            log.close()
        self.session("quick_sweep", filter=filter_key, points=len(amplitudes), status=outcome["status"],
                     reason=outcome["reason"], log=str(log.path) if log.path else None)
        return outcome

    def _gui_call(self, action):
        return self._dispatch_gui_call(_Call(action), 10.0)

    def _dispatch_gui_call(self, call, timeout_s):
        with self._native_lock:
            if self._closing:
                raise _engine.InstrumentError("Transmission is closing; Explorer action refused")
            self._gui_calls.add(call)
        try:
            if QThread.currentThread() == self._bridge.thread():
                self._bridge.execute(call)
            else:
                self._bridge.request.emit(call)
                if not call.done.wait(timeout_s):
                    call.expired.set()  # A delayed Qt callback must not write later.
                    raise _engine.InstrumentError(f"Explorer did not respond within {timeout_s:g} s")
        finally:
            with self._native_lock:
                self._gui_calls.discard(call)
        if call.error is not None:
            raise call.error
        return call.result

    # ---------------------------------------------------------------- devices
    BRINGUP_S = 300.0  # Initialization and the ramp at ON of a supply can take minutes.
    # The detector is best read at 5-10 Hz while optimizing: 10-20 samples per 2 s averaging window
    # (the DMMR default of 1 s gives two). Only a hint: its interval belongs to the DMMR plugin.
    DETECTOR_INTERVAL_MS = 200.0
    # The spray current (ESI) needs a sample in every averaging window and two to judge its spread.
    SPRAY_INTERVAL_MS = 1000.0

    def _needed(self):
        """(driven, measured) channels of what Optimize would run; the whole beamline when it cannot be built yet."""
        if self._simulated():
            return [], []
        try:
            _, config = self._configuration()
            return list(config.driven_channels), list(config.measured_channels)
        except (_engine.ConfigError, OSError):
            beamline = self.active_beamline()
            driven = [c for channels in beamline["knobs"].values() for c in channels]
            detector = [beamline["detector"]["channel"]] if beamline["detector"] else []
            spray = [beamline["spray"]["channel"]] if beamline["spray"] else []
            return driven, [*detector, *spray, *beamline["gauges"]]

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
        wanted = {name.strip().lower() for name in driven}
        for pair in self.amx_pairs():
            switched = [rail for rail in pair["rails"] or () if rail.strip().lower() in wanted]
            if switched:
                entry = entries.setdefault(pair["amx"], dict(name=pair["amx"], device=pair["device"], driven=[], measured=[],
                                                              esi=False))
                entry.setdefault("switches", []).append(pair["pair"])
        for entry in entries.values():
            device = entry["device"]
            if device is None:
                continue
            busy = device_busy(device)
            gates = [name for name in entry["driven"] if output_off(channels[name.strip().lower()])]
            if busy:
                entry.update(state="busy", fix=f"{busy} in progress")
            elif not device_on(device):
                entry.update(state="off", fix="turn it ON")
            elif gates:  # Never switched from here: an HV gate is the operator's decision.
                entry.update(state="output off", fix=f"turn the output ON in the {device.name} panel ({', '.join(gates)})")
            elif entry["measured"] and not getattr(device, "recording", True):
                entry.update(state="not recording", fix="start its recording")
            else:
                entry.update(state="ready", fix="")
            if entry["esi"] and entry["state"] == "off":
                entry["fix"] = "turn it ON in the ESI plugin (never from Transmission)"
            interval = getattr(device, "interval", None)
            currents = [name for name in entry["measured"] if getattr(channels[name.strip().lower()], "unit", "") == "A"]
            slowest = self.SPRAY_INTERVAL_MS if entry["esi"] else self.DETECTOR_INTERVAL_MS  # ESI: the spray current.
            if currents and _finite(interval) and float(interval) > slowest:
                need = ("at least 1 Hz (interval 1000 ms) is needed to judge the spray" if entry["esi"]
                        else "5–10 Hz (interval 100–200 ms) is recommended while optimizing")
                entry["hint"] = f"reads {', '.join(currents)} at {1000.0 / float(interval):.3g} Hz; {need}"
        return list(entries.values())

    @staticmethod
    def _startable(entry):
        # An ESI that is ON may start recording from here (it only reads it); turning the ESI ON
        # is the operator's, in its plugin. 2026-10-22: "turn it ON" was shown for an ESI ON.
        return entry["state"] == "not recording" or (entry["state"] == "off" and not entry["esi"])

    @staticmethod
    def _roles(entry):
        roles = []
        if entry["driven"]:
            roles.append("drives " + ", ".join(entry["driven"]))
        if entry["measured"]:
            roles.append("measures " + ", ".join(entry["measured"]))
        if entry.get("switches"):
            roles.append("switches the RF of " + ", ".join(entry["switches"]))
        return "; ".join(roles)

    def _describe(self, entry):
        return f"{entry['name']} ({self._roles(entry)})"

    def refresh_devices(self):
        if self._closing:
            return
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
                slow = " (⚠ slow: at least 1 Hz needed)" if entry["esi"] else " (⚠ slow: 5–10 Hz recommended)"
                parts.append(f"✓ {entry['name']}" + (slow if entry.get("hint") else ""))
            else:
                parts.append(f"<b>✗ {entry['name']}: {entry['state'] if entry['state'] != 'missing' else 'not available'}</b>")
        panel.devices_label.setText("Devices: " + " · ".join(parts))
        panel.devices_label.setToolTip("\n".join(self._describe(e) + (f": {e['fix']}" if e["fix"] else "")
                                                 + (f" ({e['hint']})" if e.get("hint") else "") for e in report))
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
        blocked = [e for e in report.values() if e["state"] not in ("ready", "off", "not recording")]
        if blocked:  # E.g. turned ON with an output gate still OFF: the operator's decision, not ours.
            self.cancel_bringup("Not ready: " + "; ".join(f"{e['name']}: {e['fix']}" for e in blocked) + ".",
                                log="devices_not_ready")
            return
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
        self._request_native_stop()
        self.session("stop_requested", recording=self.recording, reverting=self._reverting,
                     starting_devices=self._bringup is not None)
        if self._bringup is not None:
            self.cancel_bringup("Stopped while turning the devices ON; they stay as they are.")
            return
        if self.recording:
            self.recording = False
            self.toggleRecording()

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
        config = _engine.parse_config(text)
        if not self.state["advanced"] and self.state["mode"] == "mass" and self.state["mass_by"] == "mz":
            config.target = self._mz_target(config.target)
            config = _engine.Config(config.stages, config.pressure, config.target, config.spray,
                                    **{key: getattr(config, key) for key in _engine.Config.DEFAULTS})
        if not self._simulated():
            device_of = {}
            for channel in self.pluginManager.DeviceManager.channels():
                with contextlib.suppress(Exception):  # A channel without a device is not judged.
                    device_of[channel.name] = channel.getDevice().name
            spray = config.spray.channel if config.spray is not None else None
            for detector in config.current_channels or (None,):
                problem = current_source_problem(device_of, detector, spray)
                if problem:
                    raise _engine.ConfigError(problem)
        return text, config

    def _scan_file(self, now=None):
        """<Data path>/Transmission/<date>/<date_time>[_simulation]_transmission.h5 (user choice 2026-10-30)."""
        now = time.localtime() if now is None else now
        folder = Path(self.pluginManager.Settings.dataPath) / self.FOLDER / time.strftime("%Y-%m-%d", now)
        stem = time.strftime("%Y-%m-%d_%H-%M-%S", now) + ("_simulation" if self._simulated_run else "")
        candidate, n = folder / f"{stem}_transmission.h5", 2
        while candidate.exists():
            candidate, n = folder / f"{stem}_{n}_transmission.h5", n + 1
        return candidate

    def updateFile(self):
        if self._dummy_initialization:
            self.file = Path()
        else:
            self.file = self._scan_file()
            self.file.parent.mkdir(parents=True, exist_ok=True)
        if self.displayActive() and self.display:
            self.display.file = self.file

    def initScan(self):
        if self._dummy_initialization:
            return False
        if self._closing or self._native_active() or self._reverting or self._sweeping:
            self.show_progress("Wait until the current Transmission operation has finished.")
            return False
        self._cancel = Event()
        try:
            text, config = self._configuration()
            instrument = self._instrument(config, self._cancel)
            self._u_zero = []
            if not self.state["advanced"] and self.state["mode"] == "mass" and self.state["mass_by"] == "mz":
                plan, wiring, _, _ = self.mass_plan()
                self._u_zero = self._u_zero_jobs(plan, wiring, self._cancel)
            previous = getattr(self, "_optimizer", None)
            optimizer = self._new_native_optimizer(config, instrument, on_event=self._on_event)
        except Exception as exc:  # noqa: BLE001 - startup/protocol failures are not retried
            self.show_progress(f"Cannot start: {exc}")
            self.print(f"Cannot start: {exc}", flag=PRINT.WARNING)
            self.session("cannot_start", reason=str(exc), state=self.state, traceback=traceback.format_exc())
            return False
        if not self._retire_native_optimizer(previous):
            self._retire_native_optimizer(optimizer)
            self.show_progress("Cannot start: the previous native child could not be retired.")
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
        if self._closing:
            return
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
        optimizer = self._optimizer
        try:
            self._run_u_zero(getattr(self, "_u_zero", []), self._run_log.write)  # The other quadrupole: U = 0.
            self._result = optimizer.run()
        except Exception as exc:  # noqa: BLE001 - never leave the scan running
            self._result = dict(status="error", reason=f"{type(exc).__name__}: {exc}", stages=list(optimizer.results),
                                initial=dict(optimizer.initial), final=dict(optimizer.commanded),
                                evaluations=len(optimizer.history), spectra={}, checks={})
            self._run_log.write("exception", traceback=traceback.format_exc())
            self.print(f"Optimization failed: {exc}", flag=PRINT.ERROR)
        finally:
            unchanged = all(math.isclose(optimizer.commanded.get(c, v), v, rel_tol=1e-9, abs_tol=1e-9)
                            for c, v in optimizer.initial.items())
            result = self._result or {}
            self._decided = (unchanged or self._closing or not self._native_available(optimizer)
                             or result.get("status") in ("error", "configuration error"))
            if self._decided:
                self._retire_native_optimizer(optimizer)
            if result.get("status") == "instrument error" and not unchanged and not self._closing:
                # An instrument error sends no further command: say what stayed moved
                # (2026-10-30 lab: PSU_E was left at 89.25 V instead of 85 V without a word).
                moved = ", ".join(f"{c} {optimizer.commanded[c]:g} (start {v:g})" for c, v in optimizer.initial.items()
                                  if not math.isclose(optimizer.commanded.get(c, v), v, rel_tol=1e-9, abs_tol=1e-9))
                self.print(f"Stopped with settings moved: {moved}. "
                           + ("Set them back by hand." if self._decided else "Keep or Revert them in the panel."),
                           flag=PRINT.WARNING)
            summary = self.result_summary()
            self._run_log.write("summary", status=result.get("status"), reason=result.get("reason"), line=summary,
                                stages=result.get("stages"), checks=result.get("checks"), target=result.get("target"),
                                changed=not unchanged)
            self.session("run_end", status=result.get("status"), reason=result.get("reason"), summary=summary,
                         file=self.file, log=str(self._run_log.path) if self._run_log.path else None)
            if not self._closing:
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
                label = (f"m/z selection ({target['filter'] or 'filter'} at {target['final_center']:.1f}) on {channel}"
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
        if self._closing:
            return
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
            self._request_native_stop()
        Scan.recording.fset(self, value)

    @property
    def finished(self):
        return self._finished

    @finished.setter
    def finished(self, value):
        Scan.finished.fset(self, value)
        if value and hasattr(self, "_bridge") and not self._closing:
            self._bridge.refresh.emit()

    def toggleRecording(self):
        if not self.recording:
            self._request_native_stop()
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
        if self._busy():
            self.print("Wait until the optimization has finished.", flag=PRINT.WARNING)
            return
        self.session("keep", log=str(self._run_log.path) if self._run_log is not None and self._run_log.path else None)
        if not self._retire_native_optimizer(self._optimizer):
            self.show_progress("Native cleanup failed; no channel was changed.")
            return
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
        optimizer = self._optimizer
        if not self._native_available(optimizer):
            self.show_progress("Native session is closed or failed; revert will not be retried.")
            return
        self._cancel = Event()
        optimizer.instrument.cancel = self._cancel
        self._decided = True
        self._reverting = True
        self.show_progress("Reverting to the settings before the last run…")
        log = self._open_run_log("revert", file=self.file, initial=optimizer.initial, commanded=optimizer.commanded,
                                 run_log=str(self._run_log.path) if self._run_log is not None and self._run_log.path else None)
        optimizer.log = log.write
        if isinstance(optimizer.instrument, ExplorerInstrument):
            optimizer.instrument.log = log.write
        self._watch(log)

        def work():
            try:
                outcome = optimizer.restore_initial()
            except Exception as exc:  # noqa: BLE001 - never leave the panel locked
                outcome = dict(status="error", reason=f"{type(exc).__name__}: {exc}")
                log.write("exception", traceback=traceback.format_exc())
            finally:
                self._retire_native_optimizer(optimizer)
                self._reverting = False
                if not self._closing:
                    self._bridge.unwatch.emit()
                log.close()
            self.session("revert", status=outcome["status"], reason=outcome["reason"], file=self.file,
                         log=str(log.path) if log.path else None)
            if not self._closing:
                self._bridge.status.emit(f"Revert {outcome['status']}" + (f": {outcome['reason']}" if outcome["reason"] else "."))
                self._bridge.refresh.emit()
        self._worker = Thread(target=work, name=f"{self.name} revert", daemon=True)
        self._worker.start()
        self.refresh_panel()

    def close(self):
        with self._native_lock:
            if self._closing:
                return
            self._closing = True
            workers = dict(self._native_workers)
            for optimizer in workers.values():
                if optimizer is not None:
                    optimizer._closed = True  # Forbid even non-cancellable recovery actions before reaping.
            calls = list(self._gui_calls)
            self._gui_calls.clear()
        for call in calls:
            call.expired.set()
            call.error = _engine.InstrumentError("Transmission closed before Explorer completed its action")
            call.done.set()
        self._request_native_stop()
        # Qt can no longer drain recovery actions while its plugin is closing.
        # Reap, rather than block that thread or claim OFF/restoration succeeded.
        active = any(o is None or o._active for o in workers.values())
        if active:
            self.session("native_force_close", outputs_confirmed_off=False, restoration_confirmed=False,
                         reason="Explorer is closing; pending channel actions are refused")
        for worker in workers:
            try:
                worker.close(grace_s=0)
            except Exception as exc:  # noqa: BLE001 - still retire the other independently supervised children
                self.print(f"Native worker close failed: {exc}; hardware state is not confirmed.", flag=PRINT.WARNING)
            finally:
                with self._native_lock:
                    self._native_workers.pop(worker, None)
        self._bringup = None
        for timer in (self._bringup_timer, self._device_timer):
            if timer is not None:
                timer.stop()
        self._unwatch()
        self.session("plugin_close")
        if self._run_log is not None:
            self._run_log.close()
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
        ratios, lines = [], []  # Plotted criterion ratios; current lines, drawn once their unit is known.
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
                    values = [r["objective"] / scale for r in chosen]
                    ratios += values
                    ax_objective.plot([r["index"] for r in chosen], values, marker,
                                      color=color, markersize=6 if kind == "verify" else 4, linestyle="none")
            lost = [r for r in rows if not r["valid"]]
            if lost:  # At the bottom of the axis, whatever its scale.
                ax_objective.plot([r["index"] for r in lost], [0.03] * len(lost), "x", color="grey", linestyle="none",
                                  transform=ax_objective.get_xaxis_transform())
            best = np.maximum.accumulate([r["objective"] / scale if r["valid"] and _finite(r["objective"]) else -np.inf for r in rows])
            ax_objective.step(index, np.where(np.isfinite(best), best, np.nan), where="post", color=color, linewidth=1,
                              label=meta["name"])
            ax_objective.axvline(index[0] - 0.5, color="grey", linewidth=0.5)
            current = (meta, rows, index)
            down = np.array([sum(polarity * r["currents"][c][0] for c in meta["downstream"]) for r in rows])
            lines.append((index, down, dict(color="tab:green", linewidth=1,
                                            label=("after the aperture" if meta["aperture"] else "detector") if s == 0 else None)))
            if meta["aperture"]:
                hit = np.array([polarity * r["currents"][meta["aperture"]][0] for r in rows])
                lines.append((index, hit, dict(color="tab:red", linewidth=1, label="on the aperture" if s == 0 else None)))
                lines.append((index, hit + down, dict(color="grey", linestyle="--", linewidth=0.8,
                                                      label="reaching the aperture" if s == 0 else None)))
        factor, unit = _current_unit([v for _, values, _ in lines for v in values if v > 0])
        for index, values, style in lines:
            ax_currents.semilogy(index, np.where(values > 0, values * factor, np.nan), **style)
        if lines:
            _plain_log_ticks(ax_currents.yaxis)
        finite = [v for v in ratios if _finite(v)]
        if finite and min(finite) > 0 and max(finite) / min(finite) > 30:
            ax_objective.set_yscale("log")  # A stage that started near zero: its ratio spans decades.
            _plain_log_ticks(ax_objective.yaxis)
        else:
            ax_objective.ticklabel_format(axis="y", useOffset=False)
        if self._spectra:
            spectra = {phase: self._spectra.get(phase) for phase in ("before", "after")}
            spectrum_factor, spectrum_unit = _current_unit(
                [v for data in spectra.values() if data for v in data.get("current") or []])
            for phase, color in (("before", "tab:grey"), ("after", "tab:green")):
                data = spectra[phase]
                if data and data.get("amplitude"):
                    ax_second.plot(data["amplitude"], np.asarray(data["current"], dtype=float) * spectrum_factor,
                                   marker=".", color=color, label=phase)
            ax_second.set_title("Selected peak", fontsize=9)
            ax_second.set_xlabel("Filter amplitude (V)")
            ax_second.set_ylabel(f"Current ({spectrum_unit})")  # The title says where.
            ax_second.ticklabel_format(axis="y", useOffset=False)
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
        pressures = []
        for p, name in enumerate(pressure_names):
            values = [r["pressures"][name][0] if r["pressures"].get(name) and r["pressures"][name][0] > 0 else np.nan
                      for r in records]
            pressures += values
            ax_pressures.semilogy([r["index"] for r in records], values, marker=".", linewidth=0.8,
                                  color=f"C{p % 10}", label=name)
        _decade_limits(ax_pressures, pressures)
        ax_objective.set_ylabel("Criterion / stage start")
        ax_currents.set_ylabel(f"Current ({unit})")
        ax_pressures.set_ylabel("Pressure (mbar)")
        for ax in (ax_objective, ax_currents, ax_pressures):
            ax.set_xlabel("Measurement")
        for ax in self.display.axes:  # Four plots share the panel: room for the curves, not the labels.
            ax.tick_params(labelsize=8)
            ax.xaxis.label.set_size(9)
            ax.yaxis.label.set_size(9)
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
