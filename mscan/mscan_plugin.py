"""Quadrupole amplitude scan for AMX/PSU with DMMR current acquisition.

Upstream: ioneater/ESIBD-Explorer, esibd/scans/ms/ms.py, commit 9945145.
Copyright (C) 2021-2026 Tim Esser. GPL-2.0-or-later; see LICENSE.
Modified 2026-09-22 for ESIBD Explorer Plugins: coupled rails, verified settling,
time-windowed acquisition and explicit missing data. No driver access here.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import deque
import configparser
import contextlib
import importlib.util
import json
import math
from pathlib import Path
from threading import Event
import sys
import time
from typing import Any, Callable
import warnings

import h5py
import numpy as np
from PyQt6.QtCore import QEvent, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import (QAbstractSpinBox, QApplication, QComboBox, QFileDialog, QHeaderView, QSizePolicy,
                             QTreeWidgetItem)

from esibd.core import INFO, INOUT, PARAMETERTYPE, PRINT, MetaChannel, Parameter, TreeWidget, infoDict, parameterDict, plotting
from esibd.plugins import Plugin, Scan, SettingsManager


def _load_native_runtime(part):
    path = Path(__file__).resolve().parent / "_runtime" / f"{part}.py"
    if not path.is_file():
        raise ModuleNotFoundError(f"Missing bundled MScan native adapter: {path}")
    name = f"_esibd_bundled_mscan{part}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ModuleNotFoundError(f"Cannot load bundled MScan native adapter: {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def providePlugins():
    return [MScan]


class ScanError(RuntimeError):
    """Cannot acquire a scientifically valid point under the selected conditions."""


class ScanStopped(ScanError):
    pass


def manual_control(channel) -> bool:
    """Set by its operator, never by Explorer: Explorer only evaluates inactive channels that have
    an equation. AMPR shows this flag as "Manual"; on 2026-10-26 every AMPR channel had it unticked,
    with no equation, and an offset or a sweep on them was refused although nothing else drove them."""
    return bool(channel.active) or not str(getattr(channel, 'equation', '') or '').strip()


def amplitude_steps(start: float, stop: float, step: float) -> np.ndarray:
    """Bounded, inclusive when aligned; never overshoot the requested voltage."""
    if not all(math.isfinite(v) for v in (start, stop, step)) or min(start, stop) < 0 or step <= 0:
        raise ScanError('Use finite, nonnegative voltages and a positive step.')
    distance = abs(stop - start)
    if distance == 0 or distance / step > 100_000:
        raise ScanError('Choose different limits and at most 100,001 points.')
    count = int(math.floor(distance / step + 1e-10)) + 1
    if count < 2:
        raise ScanError('The step is larger than the scan span.')
    result = start + math.copysign(step, stop - start) * np.arange(count, dtype=np.float64)
    # Floating point rounding must not add an out-of-range voltage.
    result[-1] = min(result[-1], stop) if stop > start else max(result[-1], stop)
    return result


# m/z from the amplitude (user request 2026-11-03). Ideal 50 % square wave, conventions of Steel and Henchman
# (U and V = A per rod, from the axis): a = 8eU/(m r0² Ω²), q = 4eA/(m r0² Ω²), so m/z = 4eA / (q r0² Ω²).
# Derivation and figures: docs/mscan_quadrupole.pdf.
_E_OVER_U = 1.602176634e-19 / 1.66053906660e-27  # Elementary charge over atomic mass unit (C/kg).
_RF_ONLY_Q = .7120  # Largest stable q at a = 0 (RF only): the low-mass cut-off.


def _square_wave_trace(a, q):
    """Half-trace of the one-period transfer matrix, x direction; stable when |trace| < 2."""
    w1 = np.sqrt((a + 2 * q).astype(complex)) + 1e-16
    w2 = np.sqrt((a - 2 * q).astype(complex)) + 1e-16
    h = np.pi / 2
    return (2 * np.cos(w1 * h) * np.cos(w2 * h) - (w1 / w2 + w2 / w1) * np.sin(w1 * h) * np.sin(w2 * h)).real


_BANDS = {}


def square_wave_band(k):
    """Stable q interval on the scan line a = 2kq (k = U/A); None above the apex (k ≈ 0.2153).

    k = 0 gives (0, 0.712): RF only, a high-pass whose upper q is the low-mass cut-off.
    """
    k = round(abs(float(k)), 5)
    if k not in _BANDS:
        if k == 0:
            _BANDS[k] = (0., _RF_ONLY_Q)
        else:
            q = np.linspace(1e-4, .8, 16001)
            a = 2 * k * q
            stable = (np.abs(_square_wave_trace(a, q)) < 2) & (np.abs(_square_wave_trace(-a, -q)) < 2)
            _BANDS[k] = (float(q[stable][0]), float(q[stable][-1])) if stable.any() else None
    return _BANDS[k]


def mz_from_amplitude(amplitudes, *, frequency_hz, r0_mm, factor=0., resolution=0., minimum=.5, pairs=2):
    """m/z (Th) at the centre of the transmitted band for each amplitude A; RF only: the low-mass cut-off.

    U = max(minimum, |factor| × A − resolution), as MScan drives the rod pairs. NaN where nothing is
    transmitted (above the apex) or where the DC between the pairs is unknown (one driven pair).
    """
    amplitudes = np.asarray(amplitudes, dtype=float)
    scale = 4 * _E_OVER_U / ((r0_mm * 1e-3) ** 2 * (2 * np.pi * frequency_hz) ** 2)
    result = np.full(amplitudes.shape, np.nan)
    if factor and pairs != 2:
        return result  # Pair 2 keeps its own potential: the filtering DC is not known.
    for i, amplitude in enumerate(amplitudes):
        if not math.isfinite(amplitude) or amplitude <= 0:
            continue
        k = max(minimum, abs(factor) * amplitude - resolution) / amplitude if factor else 0.
        band = square_wave_band(k)
        if band is not None:
            q = band[1] if not factor else (band[0] + band[1]) / 2
            result[i] = scale * amplitude / q
    return result


class _Call:
    # Explorer's dynamicImport omits sys.modules registration, which dataclasses
    # need when resolving postponed annotations. Keep this a plain container.
    def __init__(self, action: Callable, cancel: Event | None):
        self.action = action
        self.cancel = cancel
        self.done = Event()
        self.expired = Event()
        self.result: Any = None
        self.error: Exception | None = None


class _GuiBridge(QObject):
    request = pyqtSignal(object)
    status = pyqtSignal(str)

    def __init__(self, scan):
        super().__init__()
        self.scan = scan
        self.request.connect(self.execute)
        self.status.connect(self.set_status)

    @pyqtSlot(object)
    def execute(self, call):
        try:
            if call.expired.is_set() or (call.cancel is not None and call.cancel.is_set()):
                raise ScanStopped('Scan stopped before the queued action.')
            call.result = call.action()
        except Exception as exc:
            call.error = exc
        finally:
            call.done.set()

    @pyqtSlot(str)
    def set_status(self, text):
        self.scan.scan_status = text
        # QLabel updates do not notify Explorer's tree about wrapped row height.
        # Idle polling is paused during a scan and while its file is being saved.
        if self.scan.settingsTree:
            self.scan._layout_readonly_fields()


class _Amplitude(MetaChannel):
    """An Explorer scan axis, not a second device or an independently writable rail."""
    def connectSource(self, giveFeedback=False):
        pass


class _Signal(MetaChannel):
    """Data only: no live Input/Output widgets; retain units of archived data."""
    def connectSource(self, giveFeedback=False):
        unit, inout = self.unit, self.inout
        # DMMR is an Explorer input-device with a current monitor. The saved
        # data are scan outputs, independently of the source device category.
        self.inout = None
        try:
            super().connectSource(giveFeedback=giveFeedback)
        finally:
            self.inout = inout
        if unit:
            self.unit = unit


class _Settings(SettingsManager):
    """UTF-8 settings and one-way migration; imported files are never rewritten."""
    @staticmethod
    def _read_ini(file):
        config = configparser.ConfigParser(interpolation=None)
        # Explorer writes UTF-8 but its 1.0.1 reader uses the Windows locale.
        # Accept an optional BOM from Windows editors, without lossy decoding.
        if file.exists():
            config.read(file, encoding='utf-8-sig')
        return config

    def _settings_path(self, file, useDefaultFile, *, save=False):
        if useDefaultFile:
            return self.defaultFile
        if file is None:
            dialog = QFileDialog.getSaveFileName if save else QFileDialog.getOpenFileName
            file = Path(dialog(parent=self.pluginManager.mainWindow, caption='msScan settings',
                directory=self.pluginManager.Settings.configPath.as_posix(), filter=self.FILTER_INI_H5)[0])
        return None if Path(file) == Path() else Path(file)

    def loadSettings(self, file=None, useDefaultFile=False):
        file = self._settings_path(file, useDefaultFile)
        if file is None:
            return
        if file.suffix.lower() != '.ini':
            return super().loadSettings(file=file)
        config = self._read_ini(file)
        self.loading = True
        try:
            items = []
            for name, default in self.defaultSettings.items():
                saved = config[name] if name in config else {}
                item = dict(default)
                item[Parameter.NAME] = name
                item[Parameter.TREE] = self.tree if default[Parameter.WIDGET] is None else None
                for field in (Parameter.VALUE, Parameter.DEFAULT, Parameter.ITEMS):
                    item[field] = saved.get(field, default[field])
                items.append(item)
            self.updateSettings(items, file)
            if not file.exists() or any(name not in config for name in self.defaultSettings):
                self.saveSettings(file=file)
            self.tree.collapseAll()
        finally:
            self.loading = False

    def updateSettings(self, items, file):
        saved = {}
        if file.exists():
            if file.suffix.lower() == '.ini':
                config = self._read_ini(file)
                saved = {key: dict(config[key]) for key in config.sections()}
            else:
                with h5py.File(file, 'r') as handle:
                    group = handle.get(f'{self.parentPlugin.name}/Settings')
                    if group is not None:
                        saved = {key: dict(value.attrs) for key, value in group.items()}
        scan = self.parentPlugin
        aliases = {scan.OUTPUTS: 'AMX outputs',
                   scan.START: 'From', scan.STOP: 'To', scan.STEP: 'Step'}
        def old_value(key, field):
            values = saved.get(key, {})
            return values.get(field, values.get(field.lower()))
        for item in items:
            key = item[Parameter.NAME]
            if key not in saved:
                for field in (Parameter.VALUE, Parameter.DEFAULT):
                    value = old_value(aliases.get(key, ''), field)
                    if key == scan.SETTLING:
                        times = [old_value(k, field) for k in ('Wait', 'Wait long')]
                        times = [float(t) / 1000 for t in times if t is not None]
                        value = max(times) if times else None
                    elif key == scan.INTEGRATION:
                        value = old_value('Average', field)
                        value = float(value) / 1000 if value is not None else None
                    elif key == scan.RATE and old_value(scan.MODE, Parameter.VALUE) == scan.CONTINUOUS:
                        step = old_value(scan.STEP, field)
                        step = old_value('Step', field) if step is None else step
                        duration = old_value(scan.TIME_STEP, field)
                        if step is not None:
                            duration = 1. if duration is None else float(duration)
                            if not math.isfinite(duration) or duration <= 0:
                                raise ScanError('Cannot convert the old sweep: invalid Time step.')
                            value = float(step) / duration
                            if not math.isfinite(value) or value <= 0:
                                raise ScanError('Cannot convert the old sweep: invalid Amplitude step.')
                    if value is not None:
                        item[field] = value
            if key == scan.MODE:
                # Missing modes in old files always mean the old stepped scan.
                # An unknown saved mode must remain visible and block Start.
                item[Parameter.ITEMS] = ','.join(dict.fromkeys(
                    [scan.STEPPED, scan.CONTINUOUS, str(item[Parameter.VALUE])]))
            if key == scan.OUTPUTS:
                # A former four-connector scan must not silently become a
                # different physical pair when the settings are loaded.
                for field in (Parameter.VALUE, Parameter.DEFAULT):
                    if field in item and item[field] not in scan.PAIRS:
                        item[field] = scan.NO_PAIR
                item[Parameter.ITEMS] = ','.join([scan.NO_PAIR, *scan.PAIRS])
            if key in (scan.AMX, scan.OFFSET, scan.OFFSET_2):
                # Keep missing selections explicit, without phantom old choices.
                if key in (scan.OFFSET, scan.OFFSET_2):
                    choices = [scan.NO_OFFSET, *[scan._offset_label(c) for c in scan.pluginManager.DeviceManager.channels() if scan._is_ampr(c)]]
                else:
                    choices = ['None', *[p.name for p in scan.pluginManager.plugins if p.name in scan.AMX_PLUGINS]]
                choices.append(str(item[Parameter.VALUE]))
                item[Parameter.ITEMS] = ','.join(dict.fromkeys(choices))
        scan.notes = str(old_value('Notes', Parameter.VALUE) or '')
        super().updateSettings(items, file)

    def requireParentItem(self, name, parentItem):
        # Display-only groups: setting keys (INI sections, HDF5 'MScan/Settings/<key>') stay flat,
        # so files written before and after the grouping (2026-11-03) read the same.
        group = self.parentPlugin.GROUP_OF.get(name)
        if group is None:
            return super().requireParentItem(name, parentItem)
        for i in range(parentItem.childCount()):
            child = parentItem.child(i)
            if not isinstance(child, Parameter) and child.text(0) == group:
                return child
        item = QTreeWidgetItem(parentItem, [group])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        item.setFirstColumnSpanned(True)
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

    def saveSettings(self, file=None, useDefaultFile=False):
        file = self._settings_path(file, useDefaultFile, save=True)
        if file is None:
            return
        # Do not append defaults to an existing imported INI or HDF5.
        if self.loading and file != self.defaultFile and file.exists():
            return
        if file.suffix.lower() != '.ini':
            return super().saveSettings(file=file, useDefaultFile=useDefaultFile)
        config = self._read_ini(file)
        config[INFO] = infoDict(self.name)
        for name, setting in self.settings.items():
            if setting.internal:
                continue
            if name not in config:
                config[name] = {}
            config[name][Parameter.VALUE] = setting.formatValue()
            config[name][Parameter.DEFAULT] = setting.formatValue(setting.default)
            if setting.parameterType in (PARAMETERTYPE.COMBO, PARAMETERTYPE.INTCOMBO, PARAMETERTYPE.FLOATCOMBO):
                config[name][Parameter.ITEMS] = ','.join(setting.items)
        with file.open('w', encoding='utf-8') as handle:
            config.write(handle)


class MScan(Scan):
    """Sweep symmetric PSU magnitudes while keeping the selected AMX waveform fixed.

    A is defined by Vpos=+A, Vneg=-A relative to the PSU reference. The x axis
    is volts, not m/z. The operator must establish the mass-filter waveform
    and calibration separately. Devices must already be enabled and recording.
    """
    name = 'MScan'
    version = '0.2.0'
    TITLE = 'msScan — AMX/PSU'
    supportedVersion = '1.0'
    iconFile = 'mscan.png'
    useInvalidWhileWaiting = False
    AMX = 'AMX'
    AMX_PLUGINS = ('AMX_A', 'AMX_B', 'AMX_HD')  # Each publishes decoded CH0–CH3 output_rows.
    OUTPUTS = 'AMX connectors'
    START = 'Amplitude from (V)'
    STOP = 'Amplitude to (V)'
    STEP = 'Amplitude step (V)'
    PASSES = 'Passes'
    MODE = 'Scan mode'
    STEPPED = 'Step by step'
    CONTINUOUS = 'Continuous'
    TIME_STEP = 'Time step (s)'
    RATE = 'Sweep rate'
    # Every enabled DMMR module is recorded (user request 2026-11-03): no module is chosen. The
    # former 'DMMR module' selection is ignored in old settings files.
    MODULES = 'DMMR modules'
    DMMR_INTERVAL = 'DMMR interval (ms)'
    SETTLING = 'Settling time (s)'
    INTEGRATION = 'Measurement time (s)'
    CADENCE = 'DMMR data interval'
    SAMPLES = 'Samples at last point'
    SCANTIME = 'Minimum duration'
    SUPPLIES = 'Driven PSU'
    SUPPLY_READBACK = 'PSU readback'
    FILE_LABEL = 'File name'
    FOLDER = 'MScan'  # <Data path>/MScan/<YYYY-MM-DD>/, never Explorer's session fields (user choice 2026-10-26)
    FREQUENCY = 'Fixed frequency'
    AMPLITUDE_LIMITS = 'Allowed amplitude (V)'
    FINAL_VOLTAGES = 'After completion'
    CURRENT_LIMITS = 'PSU Ilim'
    OFFSET = 'Quadrupole offset'  # Rod pair 1; this key predates pair 2 and saved settings keep it.
    OFFSET_2 = 'Quadrupole offset pair 2'
    OFFSET_FACTOR = 'Offset coefficient'
    OFFSET_RESOLUTION = 'Resolution offset'
    OFFSET_MINIMUM = 'Minimum U'
    OFFSET_FIXED = 'Fixed offset'
    OFFSET_READBACK = 'Offset readback'
    FILTER_PREVIEW = 'Filter preview'
    MZ_R0 = 'Field radius r0'
    MZ_AXIS = 'Plot axis'
    MZ_SCALE = 'Mass scale'  # Never '/' in a key: Explorer reads it as a group (tree and HDF5).
    AXIS_AMPLITUDE, AXIS_MZ = 'Amplitude (V)', 'm/z'
    NO_OFFSET = 'None'
    NO_PAIR = 'Select AMX pair'
    TIMEOUT = 'Settle timeout'
    TOLERANCE = 'Voltage tolerance'
    STATUS = 'Status'
    VALIDATION = 'Validation'
    PAIRS = {'CH0-CH1': (0, 1), 'CH2-CH3': (2, 3)}
    POLL_S = .05
    PICOAMPERE = 1e12  # Plot unit; files keep amperes.
    # Panel layout (user request 2026-11-03: the flat list was not intuitive). Status first, next to
    # Start; then what to choose, in the order a scan is prepared; live readings last. None: top level.
    LAYOUT = (
        (None, (STATUS,)),
        ('Setup', (AMX, OUTPUTS, SUPPLIES, FREQUENCY, MODULES, DMMR_INTERVAL, CADENCE, FILE_LABEL)),
        ('Amplitude scan', (AMPLITUDE_LIMITS, CURRENT_LIMITS, MODE, START, STOP, STEP, RATE, TIME_STEP, PASSES,
                            SETTLING, INTEGRATION, SCANTIME, TIMEOUT, TOLERANCE)),
        ('Mass filter', (OFFSET, OFFSET_2, OFFSET_FACTOR, OFFSET_RESOLUTION, OFFSET_MINIMUM, OFFSET_FIXED,
                         FILTER_PREVIEW)),
        ('m/z', (MZ_R0, MZ_AXIS, MZ_SCALE)),
        ('Live readback', (SUPPLY_READBACK, OFFSET_READBACK, FINAL_VOLTAGES, SAMPLES)),
    )
    GROUP_OF = {key: group for group, keys in LAYOUT if group for key in keys}
    # Read-only rows whose text wraps over several lines.
    WRAPPED = (STATUS, SUPPLIES, FREQUENCY, MODULES, CADENCE, SAMPLES, SCANTIME, AMPLITUDE_LIMITS, FINAL_VOLTAGES,
               CURRENT_LIMITS, OFFSET_READBACK, SUPPLY_READBACK, FILTER_PREVIEW, MZ_SCALE)
    SQUARE_WAVE_APEX = .2153  # Largest U/V transmitted by an ideal 50 % square wave (docs/mscan_quadrupole.pdf).
    # PSU regulator headroom (dropout = internal supply − output, read by the PSU plugin each poll). Lab 2026-11-03,
    # PSU_A in Half range: the internal supply fell from 256 V at 0 mA to 248.5 V at 150 mA; the output was exact
    # with 2.7 V of headroom, 0.3 V low with 0.9 V and 2 V low with 0.05 V, and the scan waited 30 s for 250 V.
    HEADROOM_START_V = 2.  # Start refused when Amplitude to exceeds output + dropout − this margin.
    HEADROOM_LOST_V = 1.  # Below this, with the output out of tolerance, the PSU cannot reach its target.
    HEADROOM_LOW_V = 2.  # Points measured below this are flagged (low_headroom).
    # The load still to come lowers the ceiling: 4.3 V for a 206 V rise that day (Half range, 500 kHz), about 2 %.
    # Above ceiling − (2 V + 3 % of the rise), Start is allowed with a warning that names the remedy.
    HEADROOM_SAG = .03
    # Displayed names where they differ from the setting keys, which files keep unchanged.
    # The mass filter is named after its physics (U, k, δ, x), not after the AMPR "offset" (2026-11-03).
    LABELS = {INTEGRATION: 'Measurement per point (s)', RATE: 'Sweep rate (V/s)',
              TIMEOUT: 'Settle timeout (s)', TOLERANCE: 'Voltage tolerance (V)', FREQUENCY: 'AMX frequency',
              OFFSET: 'Rod pair 1 (AMPR)', OFFSET_2: 'Rod pair 2 (AMPR)', OFFSET_FACTOR: 'U/V ratio k',
              OFFSET_RESOLUTION: 'Resolution δ (V)', OFFSET_MINIMUM: 'Minimum U (V)',
              OFFSET_FIXED: 'Axis potential x (V)', FILTER_PREVIEW: 'Preview',
              SUPPLY_READBACK: 'PSU rails', OFFSET_READBACK: 'Rod pairs (AMPR)', MZ_R0: 'r0 (mm)',
              MZ_SCALE: 'm/z range'}

    class Display(Scan.Display):
        def closeGUI(self):
            if not self.scan.finished:
                # Explorer refuses to close during a scan. Change nothing: on 2026-10-26 this
                # method first dropped the file-label axis, so every later plot update raised
                # IndexError, the last one before saving too, and scan 012 was lost.
                super().closeGUI()
                return
            if self.titleBar is not None:
                # Detach every action while both owners are alive: navigation
                # belongs to navToolBar, Data to Clipboard belongs to Display.
                # Leaving either attached can crash Qt during their destruction.
                self.titleBar.clear()
            if self.axes and self.scan.labelAxis is self.axes[0]:
                # initFig lent this display's axis to the scan's file label; a redraw of a
                # display still open must not label this closed figure (no renderer left).
                live = getattr(self.scan, 'display', None)
                self.scan.labelAxis = live.axes[0] if live not in (None, self) and getattr(live, 'axes', None) else None
            super().closeGUI()

        def initGUI(self):
            super().initGUI()
            self.titleBarLabel.setText(self.scan.TITLE)
            self.addAction(event=self.copy_data, toolTip='Data to Clipboard (amplitude and each current in pA).',
                           icon=self.dataClipboardIcon, before=self.copyAction)
            self.autoscaleAction = self.addStateAction(event=self.autoscale_changed,
                toolTipFalse='Manual scale: zoom and pan are kept. Click to follow the data again.',
                iconFalse=self.makeCoreIcon('scaleX_manual.png'),
                toolTipTrue='Automatic scale: the plot follows the data. Zooming or panning switches to manual.',
                iconTrue=self.makeCoreIcon('scaleX_auto.png'), before=self.copyAction, restore=False)
            self.autoscaleAction.state = True

        def autoscale_changed(self):
            if self.autoscaleAction.state:
                self.scan.plot(update=True)

        def copyClipboard(self):
            # In dark mode Explorer rebuilds the figure for the copy and then restores its limits:
            # that is not an operator zoom and must not switch the scale to manual (2026-11-02 lab:
            # after a copy, the next scan stayed on the previous view).
            state = getattr(getattr(self, 'autoscaleAction', None), 'state', True)
            self.scan._rescaling = True
            try:
                super().copyClipboard()
            finally:
                self.scan._rescaling = False
                if getattr(self, 'autoscaleAction', None) is not None:
                    self.autoscaleAction.state = state

        def limits_changed(self, _ax=None):
            # A zoom or pan by the operator, not a redraw by plot(), switches to manual scale.
            if not self.scan._rescaling and getattr(self, 'autoscaleAction', None) is not None:
                self.autoscaleAction.state = False

        def copy_data(self):
            lines = [self.ms, *self.extra_lines]
            x = np.asarray(self.ms.get_xdata(), dtype=float)
            header = '\t'.join([self.axes[0].get_xlabel(), *[f'{line.get_label()} (pA)' for line in lines]])
            rows = ['\t'.join(f'{value:.12g}' for value in values)
                    for values in zip(x, *[np.asarray(line.get_ydata(), dtype=float) for line in lines])]
            QApplication.clipboard().setText('\n'.join([header, *rows]))

        def provideDock(self):
            created = super().provideDock()
            if self.dock:
                self.dock.title = self.scan.TITLE
                self.dock.setWindowTitle(self.scan.TITLE)
                self.titleBarLabel.setText(self.scan.TITLE)
            return created

        def initFig(self):
            super().initFig()
            self.extra_lines, self.pass_lines = [], []
            if self.fig and self.canvas:
                self.axes.append(self.fig.add_subplot(111))
                self.ms = self.axes[0].plot([], [], marker='.', markersize=4)[0]
                self.scan.labelAxis = self.axes[0]
                self.axes[0].callbacks.connect('xlim_changed', self.limits_changed)
                self.axes[0].callbacks.connect('ylim_changed', self.limits_changed)
                # No MZCalculator: these abscissae are volts, not calibrated m/z.

    def __init__(self, **kwargs):
        self._plot_fault_reported = False
        self._rescaling = False
        self._cancel = Event()
        self._plan = None
        self._validation = {}
        self._last_scan_status = ''
        self._ready = False
        self.displayDefault = ''  # Plot selection only; never an acquisition setting.
        self.notes = ''
        self._collapsed_groups = set()  # Groups the operator folded, kept when settings are reloaded.
        self._arranging = False
        super().__init__(**kwargs)
        self.useDisplayChannel = False  # Every recorded DMMR module is plotted, with a legend.
        self._bridge = _GuiBridge(self)

    def getDefaultSettings(self):
        settings = {}
        settings[self.MODULES] = parameterDict(value='No DMMR module enabled', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Every enabled current module of the DMMR is recorded, each in its own column and plot line: '
                    'enable or disable modules in the DMMR plugin. Point windows follow the DMMR time axis, shared '
                    'by all modules. A module without a reading in a window gives NaN for that point only and '
                    'never stops the scan. DMMR acquisition and recording must already be active.')
        settings[self.AMX] = parameterDict(value='None', items=', '.join(['None', *self.AMX_PLUGINS]),
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='amx_name', event=self._setup_changed,
            toolTip='AMX with PSU associations already configured in its Settings. No automatic ON or config load.')
        settings[self.OUTPUTS] = parameterDict(value='CH0-CH1', items=', '.join([self.NO_PAIR, *self.PAIRS]),
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='amx_outputs', event=self._setup_changed,
            toolTip='Outputs wired to this quadrupole. Both rails of each associated PSU are swept together. Other outputs sharing these supplies are also affected.')
        settings[self.MODE] = parameterDict(value=self.STEPPED, items=f'{self.STEPPED},{self.CONTINUOUS}',
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='scan_mode', event=self._mode_changed,
            toolTip='Step by step: settle, then measure at each amplitude. Continuous: PC-timed voltage increments '
                    'with acquisition during transitions; not a hardware-synchronized analogue ramp. '
                    'The horizontal axis is commanded amplitude, not a simultaneous voltage/current measurement.')
        settings[self.TIME_STEP] = parameterDict(value=1., minimum=.001, maximum=3600., unit='s',
            parameterType=PARAMETERTYPE.FLOAT, attr='time_step_s', event=self.estimateScanTime,
            instantUpdate=False, displayDecimals=3,
            toolTip='Continuous only: requested interval between voltage commands. DMMR samples are averaged '
                    'over each actual command interval, including transitions. Does not change DMMR sampling. '
                    'Must cover the selected module\'s valid data interval (see DMMR data interval); '
                    'unknown cadence or a shorter step blocks Start. No new valid sample blocks the next command. '
                    'A final interval is measured at the last amplitude. PC jitter may lengthen an interval, '
                    'never shorten the next to catch up. A whole missed interval or an unfinished PSU move '
                    'aborts the scan without skipping commands. Actual timestamps are saved.')
        settings[self.RATE] = parameterDict(value=1., minimum=.000001, maximum=1e6,
            parameterType=PARAMETERTYPE.FLOAT, attr='sweep_rate', event=self.estimateScanTime,
            instantUpdate=False, displayDecimals=15, unit='V/s',
            toolTip='Continuous only: positive requested sweep speed in V/s. Direction follows Amplitude from/to. '
                    'Command increment = Sweep rate × Time step; the stepped-mode Amplitude step is ignored. '
                    'PC-timed, sequential PSU writes, not a verified analogue slew rate or synchronized ramp.')
        settings[self.PASSES] = parameterDict(value=1, minimum=1, maximum=1000,
            parameterType=PARAMETERTYPE.INT, attr='passes', event=self.estimateScanTime, instantUpdate=False,
            toolTip='The same sweep is repeated this many times in one file. Each pass is kept; the plot shows '
                    'every pass faintly and their mean. Step by step: each pass returns to the start like any '
                    'other step. Continuous: each pass ends like a single sweep, then returns to the start and '
                    'settles (Initial/final settling) before sweeping again.')
        settings[self.DMMR_INTERVAL] = parameterDict(value=1000, minimum=100, maximum=10000,
            parameterType=PARAMETERTYPE.INT, event=self._dmmr_interval_changed,
            instantUpdate=False, restore=False, unit='ms',
            toolTip='Live DMMR software interval, shared by ALL its modules and recording. '
                    'Edits use the existing DMMR setting and its allowed range; no ON/OFF or ADC setting is changed. '
                    'The delay is applied after module reads, so actual sample intervals can be longer. '
                    'Explorer normally permits 100–10000 ms (1000 ms default), not a hardware sampling-rate guarantee. '
                    'Loading a scan file never applies its saved interval to DMMR. Cannot edit during a scan.')
        for key, value, attr in ((self.START, 50., 'start'), (self.STOP, 200., 'stop'), (self.STEP, 1., 'step')):
            settings[key] = parameterDict(value=value, minimum=.000001 if key == self.STEP else 0., maximum=1e6,
                parameterType=PARAMETERTYPE.FLOAT, attr=attr, event=self.estimateScanTime, instantUpdate=False,
                displayDecimals=3, unit='V', toolTip='Amplitude A: rail levels -A / +A, not peak-to-peak voltage.')
        for key, value, attr, tooltip in (
            (self.SETTLING, .5, 'settling_s', 'Continuous time within voltage tolerance after the PSU commands finish. '
                'Step by step: at every point and on return. Continuous: only at the start and on return.'),
            (self.INTEGRATION, 1., 'integration_s', 'Time window for averaging new DMMR samples, after settling. '
                'This does not change DMMR sampling. Empty or invalid windows are saved as NaN.'),
        ):
            settings[key] = parameterDict(value=value, minimum=.001, maximum=3600., unit='s',
                parameterType=PARAMETERTYPE.FLOAT, attr=attr, event=self.estimateScanTime,
                instantUpdate=False, displayDecimals=3, toolTip=tooltip)
        for key, value, attr, tooltip in (
            (self.CADENCE, 'Select a DMMR module', None, 'Intervals between the last 21 valid recorded samples of the selected module, '
                'not empty recorder ticks. The minimum continuous Time step is the larger of the configured DMMR polling '
                'interval and the longest observed interval, rounded up to milliseconds. At least two fresh valid samples '
                'are required. This observed software bound does not certify an ADC conversion rate or future timing.'),
            (self.SAMPLES, 'Not acquired', None, 'Number of finite/total DMMR samples used at the last acquired point.'),
            (self.SCANTIME, 'Unavailable', 'scantime', 'Step by step: all settling and measurement windows plus final settling. '
                'Continuous: one time step per amplitude, including the last, plus one settling per pass '
                'and the final settling. '
                'Approach/return, communication and waiting for DMMR data add time. Neither mode is inherently faster.'),
        ):
            settings[key] = parameterDict(value=value, parameterType=PARAMETERTYPE.LABEL,
                indicator=True, restore=False, attr=attr, toolTip=tooltip)
        settings[self.TIMEOUT] = parameterDict(value=30., minimum=.1, maximum=3600., unit='s',
            parameterType=PARAMETERTYPE.FLOAT, attr='settle_timeout', advanced=True,
            toolTip='Abort if voltage settling or fresh detector data takes longer than this timeout.')
        settings[self.TOLERANCE] = parameterDict(value=1., minimum=.001, maximum=100., unit='V',
            parameterType=PARAMETERTYPE.FLOAT, attr='voltage_tolerance', advanced=True,
            toolTip='Allowed absolute difference between each measured PSU magnitude and its requested value.')
        settings[self.STATUS] = parameterDict(value='Idle', parameterType=PARAMETERTYPE.LABEL,
            attr='scan_status', indicator=True, restore=False,
            toolTip='Readiness is checked from existing device/channel states, without any hardware command. '
                    'Normal completion restores the original PSU setpoints. Stop/error holds the last setpoints; HV stays ON.')
        settings[self.SUPPLIES] = parameterDict(value='Select an AMX', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Both listed PSU setpoints follow A. Physical rails are -A / +A relative to the PSU reference. '
                    'All AMX connectors sharing a listed PSU are affected. This display never commands a device.')
        settings[self.SUPPLY_READBACK] = parameterDict(value='Select an AMX', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Live readback of each driven PSU rail: measured voltage (Monitor) and the setpoint confirmed by the PSU. '
                    'Display only; it never commands a device.')
        settings[self.FILE_LABEL] = parameterDict(value='', parameterType=PARAMETERTYPE.TEXT, attr='file_label',
            toolTip='Optional name added to each scan file: <Data path>/MScan/<date>/<date_time>_<name>_PSU-<X>_<from>-<to>V.h5. '
                    'Letters, digits, - and _ are kept; other characters become -.')
        settings[self.FREQUENCY] = parameterDict(value='Unavailable', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False, toolTip='Read from AMX. Frequency, duty cycle and routing stay fixed; change them in AMX, not here.')
        for key, tooltip in (
            (self.AMPLITUDE_LIMITS, 'Intersection of all selected PSU hardware voltage limits and Explorer channel limits. '
                'Read-only: choose the sweep with Amplitude from/to. Change channel Min/Max in PSU; the hardware ceiling cannot be overridden. '
                'Read for the current range; unavailable limits block Start. This is a setpoint range, not a guarantee of regulation under load.'),
            (self.FINAL_VOLTAGES, 'Normal completion restores these initial PSU voltage setpoints (V), even outside the scan interval. '
                'Signed levels relative to the PSU reference. Iget (mA) is the PSU current measured now, '
                'not a prediction of current after the return. It updates during the scan; the return voltage stays fixed. '
                'Stop/error does not restore or switch HV OFF.'),
            (self.CURRENT_LIMITS, 'Hardware-read Ilim and maximum programmable current, in CH0/CH1 order. MScan never changes Ilim. '
                'A measured current at or above Ilim blocks/aborts acquisition. Foldback can limit below Ilim; voltage must also stay in tolerance. '
                'This is not a fast overcurrent protection or a guarantee against transients.'),
        ):
            settings[key] = parameterDict(value='Unavailable', parameterType=PARAMETERTYPE.LABEL,
                indicator=True, restore=False, toolTip=tooltip)
        settings[self.OFFSET] = parameterDict(value=self.NO_OFFSET, items=self.NO_OFFSET,
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='offset_channel', event=self._offset_changed,
            toolTip='AMPR channel (module, channel) of quadrupole rod pair 1. During the scan it is set to '
                    'x + U, with U = max(Minimum U, k × A − δ), at every amplitude A, '
                    'then verified: the AMPR hardware '
                    'setpoint must confirm the scan target and its Monitor must be within Voltage tolerance before '
                    'acquisition. Normal completion restores its initial value; Stop/error holds the last value. '
                    'None: not driven, only the RF amplitude is swept.')
        settings[self.OFFSET_2] = parameterDict(value=self.NO_OFFSET, items=self.NO_OFFSET,
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='offset_channel_2', event=self._offset_changed,
            toolTip='AMPR channel of quadrupole rod pair 2, set to x − U: the same axis potential, the DC of '
                    'opposite sign. Verified, recorded and restored like pair 1. None: only pair 1 is driven.')
        settings[self.OFFSET_FACTOR] = parameterDict(value=.2, minimum=-100., maximum=100.,
            parameterType=PARAMETERTYPE.FLOAT, attr='offset_factor', event=self._offset_changed,
            instantUpdate=False, displayDecimals=4,
            toolTip='k = U/V: ratio of the DC U to the RF amplitude V (here the scanned amplitude A). It sets the '
                    'resolution: for an ideal 50 % square wave nothing is transmitted above about 0.215, and '
                    'm/Δm ≈ 0.157 / (0.2153 − k), about 10 at 0.20 and 30 at 0.21. '
                    '0: no DC between the pairs, RF only. Negative: the pairs are swapped. '
                    'Not the α of textbooks, which is the Mathieu parameter a (k = a / 2q). '
                    'Each commanded value is rounded to the AMPR channel display precision.')
        settings[self.OFFSET_RESOLUTION] = parameterDict(value=0., minimum=0., maximum=1000.,
            parameterType=PARAMETERTYPE.FLOAT, attr='offset_resolution', event=self._offset_changed,
            instantUpdate=False, displayDecimals=3,
            toolTip='δ, in V, subtracted from k × A, so it acts on U and on the filter, unlike the axis potential x. '
                    'With k at its limit, a peak keeps about the same width in A over the scan, about '
                    '6.4 × δ for an ideal square wave. 0: straight line through zero.')
        settings[self.OFFSET_MINIMUM] = parameterDict(value=.5, minimum=.001, maximum=1000.,
            parameterType=PARAMETERTYPE.FLOAT, attr='offset_minimum', event=self._offset_changed,
            instantUpdate=False, displayDecimals=3,
            toolTip='Smallest U, in V. At zero DC the filter would pass every heavy '
                    'ion, so the DC never reaches zero during a scan, as on commercial instruments. Peaks whose tip lies '
                    'below (Minimum U + δ) / k are not filtered reliably. Not used when k is 0.')
        settings[self.OFFSET_FIXED] = parameterDict(value=0., minimum=-1000., maximum=1000.,
            parameterType=PARAMETERTYPE.FLOAT, attr='offset_fixed', event=self._offset_changed,
            instantUpdate=False, displayDecimals=3,
            toolTip='x, in V: DC common to both rod pairs, constant during the scan. It is the potential of the '
                    'quadrupole axis, so it sets the ion energy, not the mass filter: keep the value the rods hold '
                    'outside a scan. Pair 1 is set to x + U, pair 2 to x − U.')
        settings[self.FILTER_PREVIEW] = parameterDict(value='No rod pair selected', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='What the scan will apply, computed from these settings over Amplitude from/to: U at both ends '
                    'and the potential of each rod pair. The resolution assumes an ideal 50 % square wave with an '
                    'amplitude A on the rods, which is not verified on this set-up. Display only, never a command.')
        settings[self.OFFSET_READBACK] = parameterDict(value='Not driven', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Rod pair channels: AMPR Monitor (measured) and setpoint. During a scan: scan target, '
                    'Monitor and whether the AMPR confirmed the target. Display only, never a command.')
        settings[self.MZ_R0] = parameterDict(value=5., minimum=.1, maximum=100., unit='mm',
            parameterType=PARAMETERTYPE.FLOAT, attr='mz_r0_mm', event=self._mz_changed, instantUpdate=False,
            displayDecimals=3,
            toolTip='Field radius of the quadrupole: distance from its axis to the inner surface of a rod. '
                    'm/z varies as A / r0², so 1 % on r0 is 2 % on m/z. Q2: 5 mm, provisional (2026-11-03). '
                    'r0 also absorbs the unverified ratio between the rod amplitude and A: to calibrate on a '
                    'known peak, set r0 to r0 × √(m/z shown / m/z known).')
        settings[self.MZ_AXIS] = parameterDict(value=self.AXIS_AMPLITUDE, items=f'{self.AXIS_AMPLITUDE},{self.AXIS_MZ}',
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='mz_axis', event=self._mz_changed,
            toolTip='Abscissa of the plot. m/z is computed from A, the AMX frequency, r0 and the mass filter '
                    'settings of the scan. The file always keeps A, and adds the m/z in Validation/mz.')
        settings[self.MZ_SCALE] = parameterDict(value='Unavailable', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='m/z at Amplitude from/to: the centre of the transmitted band for an ideal 50 % square wave, '
                    'or the low-mass cut-off when RF only. m/z = 4 e A / (q r0² (2π f)²), with q the centre of the '
                    'stable band on the scan line a = 2 (U/A) q (docs/mscan_quadrupole.pdf). Theory: exact only once '
                    'r0 is calibrated on a known peak.')
        order = [key for _, keys in self.LAYOUT for key in keys]
        return {key: settings[key] for key in order}

    def initGUI(self):
        # Use Explorer's common plugin/services, not the historical scan GUI.
        self.loading = True
        Plugin.initGUI(self)
        self.titleBarLabel.setText(self.TITLE)
        self.settingsTree = TreeWidget()
        self.settingsTree.setMinimumWidth(200)
        self.settingsTree.setHeaderLabels(['msScan', 'Value'])
        self.settingsTree.setRootIsDecorated(True)  # Fold arrows of the setting groups.
        self.settingsTree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.settingsTree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.settingsTree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.settingsTree.itemCollapsed.connect(lambda item: self._group_folded(item, True))
        self.settingsTree.itemExpanded.connect(lambda item: self._group_folded(item, False))
        self.addContentWidget(self.settingsTree)
        self._layout_timer = QTimer(self._bridge)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._layout_readonly_fields)
        self.settingsMgr = _Settings(parentPlugin=self, pluginManager=self.pluginManager, tree=self.settingsTree,
            name=f'{self.name} Settings', defaultFile=self.pluginManager.Settings.configPath / self.configINI,
            dependencyPath=self.pluginManager.Settings.dependencyPath, sourceCodePath=self.pluginManager.Settings.sourceCodePath)
        self.settingsMgr.addDefaultSettings(plugin=self)
        self.settingsMgr.init()
        self.addAction(event=lambda: self.loadSettings(file=None), toolTip='Load msScan settings.', icon=self.makeCoreIcon('blue-folder-import.png'))
        self.addAction(event=lambda: self.saveSettings(file=None), toolTip='Export msScan settings.', icon=self.makeCoreIcon('blue-folder-export.png'))
        self.recordingAction = self.addStateAction(event=self.toggleRecording,
            toolTipFalse='Start the AMX/PSU scan, recording every enabled DMMR module.', iconFalse=self.makeCoreIcon('play.png'),
            toolTipTrue='Stop the scan; hold the last PSU setpoints. HV stays ON.', iconTrue=self.makeCoreIcon('stop.png'))
        self.loading = False
        self.estimateScanTime()
        self.dummyInitialization()
        self.recordingAction.toggled.connect(self._update_scan_action)
        button = self.titleBar.widgetForAction(self.recordingAction)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._update_scan_action()
        self._format_settings()
        self.toggleAdvanced(advanced=False)
        self._refresh_interface()
        # Discovery/readback only, on Qt: no hardware reads and no reinitializing
        # scan data when a device appears, disappears or changes its live value.
        self._interface_timer = QTimer(self._bridge)
        self._interface_timer.timeout.connect(self._refresh_interface)
        self._interface_timer.start(500)

    def toggleAdvanced(self, advanced=None):
        if advanced is not None:
            self.advancedAction.state = advanced
        if getattr(self, 'settingsMgr', None) is not None:
            for setting in self.settingsMgr.settings.values():
                if setting.advanced:
                    setting.setHidden(not self.advancedAction.state)
            self._mode_fields()

    def _mode_fields(self):
        if not all(key in self.settingsMgr.settings for key in (self.MODE, self.INTEGRATION, self.TIME_STEP, self.RATE, self.SETTLING)):
            return
        continuous = getattr(self, 'scan_mode', self.STEPPED) == self.CONTINUOUS
        for key in (self.INTEGRATION, self.STEP):
            self.settingsMgr.settings[key].setHidden(continuous)
        for key in (self.TIME_STEP, self.RATE):
            self.settingsMgr.settings[key].setHidden(not continuous)
        self.settingsMgr.settings[self.SETTLING].setText(0,
            'Initial/final settling (s)' if continuous else self.SETTLING)

    def _mode_changed(self):
        if not self.loading and not self.settingsMgr.loading:
            self._mode_fields()
            self._refresh_interface()

    def _update_scan_action(self):
        action = getattr(self, 'recordingAction', None)
        if action is not None:
            action.setText('Stop scan' if self.recording else 'Start scan')
            # Stop remains available throughout the run, even if a source fails.
            # A new scan must wait for the previous worker and file save to finish.
            enabled = self.recording or (self.finished and self._ready)
            tip = action.toolTipTrue if self.recording else action.toolTipFalse
            if not enabled:
                # A greyed button says why, without scrolling to the Status row.
                tip = f'{tip}\n\n{getattr(self, "scan_status", "")}'.strip()
            action.setToolTip(tip)
            action.setEnabled(enabled)

    def _format_settings(self):
        # Preserve old step/time ratios, including when Qt interprets the text
        # on Start or focus loss. Trim zeros, not significant digits: rounded
        # text can silently drop an aligned endpoint from the generated scan.
        spin = self.settingsMgr.settings[self.RATE].spin
        spin.textFromValue = lambda value: f'{value:.15f}'.rstrip('0').rstrip('.')
        spin.setValue(spin.value())
        for key, label in self.LABELS.items():
            self.settingsMgr.settings[key].setText(0, label)  # labels, not INI/HDF5 keys
        self.settingsTree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for spin in self.settingsTree.findChildren(QAbstractSpinBox):
            spin.setKeyboardTracking(False)
        for key in (self.AMX, self.OUTPUTS, self.MODE, self.OFFSET, self.OFFSET_2):
            combo = self.settingsMgr.settings[key].combo
            combo.setMaximumWidth(16777215)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        for key in self.WRAPPED:
            label = self.settingsMgr.settings[key].label
            label.setMaximumHeight(16777215)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setProperty('mscanWrappedLabel', True)
            label.installEventFilter(self)
        self._unfold_groups()
        self._layout_readonly_fields()

    def _groups(self):
        root = self.settingsTree.invisibleRootItem()
        return [item for item in (root.child(i) for i in range(root.childCount())) if not isinstance(item, Parameter)]

    def _unfold_groups(self):
        # Settings (re)loads rebuild the tree folded: open every group the operator did not fold.
        self._arranging = True
        try:
            for item in self._groups():
                item.setExpanded(item.text(0) not in self._collapsed_groups)
        finally:
            self._arranging = False

    def _group_folded(self, item, folded):
        manager = getattr(self, 'settingsMgr', None)
        if self._arranging or self.loading or manager is None or manager.loading or isinstance(item, Parameter):
            return
        (self._collapsed_groups.add if folded else self._collapsed_groups.discard)(item.text(0))

    def eventFilter(self, obj, event):
        # Defer height changes until QTreeWidget has finished updating editors.
        # Scheduling a new item layout inside their Resize events can recurse
        # through visualRect/updateEditorGeometries until Qt exhausts its stack.
        if (event.type() == QEvent.Type.Resize and obj.property('mscanWrappedLabel')
                and event.size().width() != event.oldSize().width()):
            self._layout_timer.start(0)
        return super().eventFilter(obj, event)

    @staticmethod
    def _fit_readonly_label(label):
        # Recompute from the actual column width, not QLabel's preferred
        # wrapping width (sizeHint can otherwise double the row height).
        # Release both old bounds so shorter text AND narrower columns reflow.
        label.setMinimumHeight(0)
        label.setMaximumHeight(16777215)
        label.setFixedHeight(max(0, label.heightForWidth(label.width())))

    def _layout_readonly_fields(self):
        for key in self.WRAPPED:
            label = self.settingsMgr.settings[key].label
            self._fit_readonly_label(label)
        self.settingsTree.scheduleDelayedItemsLayout()

    def provideDock(self):
        created = super().provideDock()
        if self.dock:
            self.dock.title = self.TITLE
            self.dock.setWindowTitle(self.TITLE)
            self.titleBarLabel.setText(self.TITLE)
        return created

    @staticmethod
    def _is_dmmr(channel):
        try:
            return (channel.getDevice().name == 'DMMR' and channel.real and channel.unit == 'A'
                    and callable(getattr(channel, 'module_address', None)) and channel.module_address() >= 0)
        except (AttributeError, TypeError, ValueError):
            return False

    def _dmmr_device(self):
        devices = list({id(c.getDevice()): c.getDevice() for c in self.pluginManager.DeviceManager.channels()
                        if self._is_dmmr(c)}.values())
        if len(devices) != 1:
            raise ScanError('No DMMR module discovered. Check the DMMR plugin.' if not devices
                            else 'Several DMMR devices found; MScan records one.')
        return devices[0]

    def _modules(self):
        """Every enabled DMMR current module, by module number: all are recorded, none is chosen.

        User request 2026-11-03: a chosen "reference" module was a trap (that day it was M04, flat,
        while the peaks were on M03). The DMMR time axis, shared by all modules, now times the points.
        """
        device = self._dmmr_device()
        modules = sorted((c for c in self.pluginManager.DeviceManager.channels()
                          if self._is_dmmr(c) and c.enabled and c.getDevice() is device),
                         key=lambda c: (c.module_address(), c.name))
        if not modules:
            raise ScanError('Enable at least one DMMR module in the DMMR plugin.')
        names = [c.name for c in modules]
        shared = sorted({name for name in names if names.count(name) > 1})
        if shared:
            raise ScanError(f'DMMR modules share the name {", ".join(shared)}: rename them in the DMMR plugin.')
        return modules

    def _dmmr_clock(self, modules):
        """The DMMR time axis and, per tick, whether at least one recorded module has a new reading."""
        device = modules[0].getDevice()
        times = np.asarray(device.time.get(), dtype=float)
        valid = np.zeros(len(times), dtype=bool)
        for c in modules:
            values = np.asarray(c.getValues(subtractBackground=False), dtype=float)
            if len(values) != len(times):
                raise ScanError(f'{c.name}: DMMR timestamps and values are misaligned.')
            valid |= np.isfinite(values)
        return device, times, valid

    def _module_values(self, start, end):
        """Average every recorded module over (start, end] of the shared DMMR time axis.

        A module without a usable history gives NaN for that point only: it never stops the scan.
        """
        results = []
        for c in (self._plan or {}).get('detectors', []):
            try:
                times = np.asarray(c.getDevice().time.get(), dtype=float)
                values = np.asarray(c.getValues(subtractBackground=False), dtype=float)
                n = min(len(times), len(values))
                lo, hi = np.searchsorted(times[:n], (start, end), side='right')
                results.append(self._average_samples(values[lo:hi]))
            except (AttributeError, TypeError, ValueError):
                results.append((np.nan, 0, 0))
        return results

    @staticmethod
    def _is_ampr(channel):
        try:
            return (channel.getDevice().name.startswith('AMPR') and channel.real and channel.unit == 'V'
                    and callable(getattr(channel, 'module_address', None))
                    and callable(getattr(channel, 'channel_number', None)))
        except (AttributeError, TypeError, ValueError):
            return False

    @staticmethod
    def _offset_label(channel):
        return f'{channel.getDevice().name} M{channel.module_address()} CH{channel.channel_number()} — {channel.name}'

    def _offset_changed(self):
        if not self.loading and not self.settingsMgr.loading:
            self._refresh_interface()

    def _offset_sources(self):
        """The AMPR channels of rod pairs 1 and 2 with their sign (+1, -1), pair 1 first; [] when not driven."""
        sources = []
        for attr, sign in (('offset_channel', 1.), ('offset_channel_2', -1.)):
            selection = str(getattr(self, attr, self.NO_OFFSET)).strip()
            if not selection or selection == self.NO_OFFSET:
                continue
            matches = [c for c in self.pluginManager.DeviceManager.channels() if self._is_ampr(c)
                       and selection in (self._offset_label(c), c.name)]
            if len(matches) != 1:
                raise ScanError(f'Rod pair channel {selection} is unavailable or ambiguous. Check the AMPR channels.')
            sources.append((matches[0], sign))
        if sources and sources[0][1] < 0:
            raise ScanError('Select the AMPR channel of rod pair 1; pair 2 alone is not driven.')
        if len(sources) == 2 and sources[0][0] is sources[1][0]:
            raise ScanError('Rod pairs 1 and 2 must be two different AMPR channels.')
        return sources

    def _offset_fixed(self):
        try:
            fixed = float(getattr(self, 'offset_fixed', 0.))
        except (TypeError, ValueError):
            fixed = math.nan
        if not math.isfinite(fixed):
            raise ScanError('Axis potential x must be finite.')
        return fixed

    def _offset_resolution(self):
        try:
            resolution = float(getattr(self, 'offset_resolution', 0.))
        except (TypeError, ValueError):
            resolution = math.nan
        if not math.isfinite(resolution) or resolution < 0:
            raise ScanError('Resolution δ must be finite and not negative.')
        return resolution

    def _offset_minimum(self):
        try:
            minimum = float(getattr(self, 'offset_minimum', .5))
        except (TypeError, ValueError):
            minimum = math.nan
        if not math.isfinite(minimum) or minimum <= 0:
            raise ScanError('Minimum U must be finite and above 0 V: at zero DC every heavy ion passes.')
        return minimum

    @staticmethod
    def _offset_value(fixed, sign, factor, resolution, minimum, amplitude):
        """fixed ± max(U_min, U/V × A − δ). U/V = 0 is RF-only; a negative U/V swaps the pairs."""
        if factor == 0:
            return fixed
        return fixed + sign * math.copysign(max(minimum, abs(factor) * float(amplitude) - resolution), factor)

    @staticmethod
    def _offset_formula(fixed, sign, factor, resolution, minimum):
        if factor == 0:
            return f'{fixed:g}'
        sign = math.copysign(sign, sign * factor)
        line = f'{abs(factor):g} × A − {resolution:g}' if resolution else f'{abs(factor):g} × A'
        variable = f'max({minimum:g}, {line})'
        if fixed == 0:
            return variable if sign > 0 else f'−{variable}'
        return f'{fixed:g} {"+" if sign > 0 else "−"} {variable}'

    @staticmethod
    def _offset_bounds(c, ctrl):
        minimum = MScan._number(c, 'min', 'channel minimum', nonnegative=False)
        maximum = MScan._number(c, 'max', 'channel maximum', nonnegative=False)
        limit = getattr(ctrl, '_module_voltage_limit', None)
        rating = float(limit(c.module_address())) if callable(limit) else math.inf
        low, high = max(minimum, -rating), min(maximum, rating)
        if low > high:
            raise ScanError(f'{c.name}: no admissible offset range.')
        return low, high

    def _offset_plan(self, low, high):
        """Read-only validation of the offset channels, pair 1 first; [] when the offset is not driven."""
        sources = self._offset_sources()
        if not sources:
            return []
        try:
            factor = float(self.offset_factor)
        except (AttributeError, TypeError, ValueError):
            factor = math.nan
        if not math.isfinite(factor):
            raise ScanError('U/V ratio k must be finite.')
        fixed, resolution, floor = self._offset_fixed(), self._offset_resolution(), self._offset_minimum()
        return [self._offset_pair_plan(c, sign, factor, fixed, resolution, floor, low, high) for c, sign in sources]

    def _offset_pair_plan(self, c, sign, factor, fixed, resolution, floor, low, high):
        self._registered(c)
        device = c.getDevice()
        ctrl = device.controller
        if not all(hasattr(ctrl, field) for field in ('_latest_setpoints', '_setpoint_lock', '_setpoint_cancel')):
            raise ScanError(f'{c.name}: update MScan and AMPR plugins together; setpoint confirmation required.')
        if not c.enabled:
            raise ScanError(f'{c.name}: switch the AMPR offset channel ON.')
        if not manual_control(c):
            raise ScanError(f'{c.name}: driven by its equation; tick Manual in {device.name} or clear the equation.')
        if not device.isOn() or not ctrl.initialized or ctrl.device is None:
            raise ScanError(f'{c.name}: turn the AMPR ON.')
        if any(bool(getattr(ctrl, flag, False)) for flag in ('initializing', 'transitioning', 'ramping')):
            raise ScanError(f'{c.name}: wait for the AMPR transition to finish.')
        if ctrl._setpoint_cancel.is_set():
            raise ScanError(f'{c.name}: AMPR output shutdown requested.')
        minimum, maximum = self._offset_bounds(c, ctrl)
        # Monotonic in A, so the two ends of the span bound the whole scan.
        first, last = sorted(self._offset_value(fixed, sign, factor, resolution, floor, a) for a in (low, high))
        if first < minimum or last > maximum:
            raise ScanError(f'{c.name}: rod pair potential {first:g}–{last:g} V '
                            f'({self._offset_formula(fixed, sign, factor, resolution, floor)}) exceeds '
                            f'allowed {minimum:g}–{maximum:g} V.')
        initial = self._number(c, 'value', 'offset setpoint', nonnegative=False)
        if not minimum <= initial <= maximum:
            raise ScanError(f'{c.name}: final offset restoration to {initial:g} V is outside allowed {minimum:g}–{maximum:g} V.')
        o = dict(channel=c, name=c.name, device=device, controller=ctrl, backend=ctrl.device,
                 token=ctrl._setpoint_cancel, key=(c.module_address(), c.channel_number()), factor=factor,
                 fixed=fixed, resolution=resolution, floor=floor, sign=sign, pair=1 if sign > 0 else 2,
                 initial=initial, expected=initial, requested=initial, written=False, observed=math.nan, ready=False)
        state, detail = self._offset_request_state(o)
        if state != 'confirmed':
            raise ScanError(f'{c.name}: offset setpoint {initial:g} V not confirmed by the AMPR ({state}). {detail}'.strip())
        if not math.isfinite(float(c.monitor)):
            raise ScanError(f'{c.name}: no valid AMPR Monitor readback for the offset.')
        return o

    @staticmethod
    def _offset_request_state(o):
        """State of the AMPR's latest request for the offset channel, for the scan's own target."""
        ctrl, c = o['controller'], o['channel']
        with ctrl._setpoint_lock:
            request = ctrl._latest_setpoints.get(o['key'])
            fields = None if request is None else (request.channel, request.target, request.state, request.detail)
        if fields is None:
            if o['written']:
                return 'pending', 'Waiting for the AMPR to take the scan target.'
            # Untouched since its connection: the AMPR confirmed it at ON (AMPR's own default).
            return str(getattr(c, '_ampr_setpoint_state', 'confirmed')), ''
        owner, target, state, detail = fields
        if owner is not c or not math.isclose(float(target), o['expected'], rel_tol=1e-10, abs_tol=1e-9):
            return 'pending', 'Waiting for the AMPR to take the scan target.'
        return str(state), str(detail)

    def _offset_observation(self):
        """GUI-only: readiness of every offset channel. Aborts on any change made outside the scan."""
        ready = True
        for o in self._plan.get('offsets') or []:
            ready = self._offset_pair_observation(o) and ready
        return ready

    def _offset_pair_observation(self, o):
        c, device, ctrl = o['channel'], o['device'], o['controller']
        self._registered(c)
        if (c.name != o['name'] or self._plugin(device.name) is not device or device.controller is not ctrl
                or ctrl.device is not o['backend'] or ctrl._setpoint_cancel is not o['token'] or o['token'].is_set()
                or not device.isOn() or not ctrl.initialized or not c.enabled or not manual_control(c) or not c.real):
            raise ScanError(f'{o["name"]}: offset source changed or was stopped.')
        if not math.isclose(float(c.value), o['expected'], rel_tol=1e-10, abs_tol=1e-9):
            raise ScanError(f'{c.name}: offset changed outside the scan '
                            f'(scan target {o["expected"]:g} V, new request {float(c.value):g} V).')
        state, detail = self._offset_request_state(o)
        if state in ('error', 'mismatch', 'stored'):
            raise ScanError(f'{c.name}: offset setpoint {o["expected"]:g} V {state}. {detail}'.strip())
        monitor = float(c.monitor)
        busy = any(bool(getattr(ctrl, flag, False)) for flag in ('initializing', 'transitioning', 'ramping'))
        o['observed'] = monitor
        o['ready'] = (not busy and state == 'confirmed' and math.isfinite(monitor)
                      and abs(monitor - o['expected']) <= self._plan['tolerance'])
        return o['ready']

    def _offset_target(self, amplitude):
        """Each pair's DC target at amplitude A: fixed ± max(U_min, U/V × A − δ) (pair 1 +, pair 2 −)."""
        return [self._offset_value(o['fixed'], o['sign'], o['factor'], o['resolution'], o['floor'], amplitude)
                for o in self._plan.get('offsets') or []]

    def _offset_snapshot(self):
        return [(o['observed'], o['expected']) for o in self._plan.get('offsets') or []]

    def _not_ready(self, message):
        """Name the offset when it, rather than the PSU, is what is not confirmed."""
        for o in (self._plan or {}).get('offsets') or []:
            if not o['ready']:
                return ScanError(f'Rod pair {o["pair"]} {o["name"]}: target {o["expected"]:g} V not confirmed by the AMPR '
                                 f'or Monitor {o["observed"]:g} V outside tolerance. {message}')
        return ScanError(message)

    def _offset_readback_text(self, *, running=False):
        if running:
            offsets = (self._plan or {}).get('offsets') or []
            if not offsets:
                return 'Not driven'
            return '\n'.join(f'{o["name"]}: target {o["expected"]:+g} V, Monitor {o["observed"]:+.3f} V '
                             f'({"confirmed" if o["ready"] else "waiting for confirmation"})' for o in offsets)
        sources = self._offset_sources()
        if not sources:
            return 'Not driven'
        return '\n'.join(f'{c.name} (pair {1 if sign > 0 else 2}): Monitor {float(c.monitor):+.3f} V, '
                         f'set {float(c.value):+g} V' for c, sign in sources)

    def _filter_preview_text(self):
        """What the scan will apply to the rod pairs, from the settings alone: no device is read."""
        sources = self._offset_sources()
        if not sources:
            return 'No rod pair selected: only the RF amplitude is swept, no DC follows A.'
        try:
            factor = float(self.offset_factor)
        except (AttributeError, TypeError, ValueError):
            factor = math.nan
        if not math.isfinite(factor):
            raise ScanError('U/V ratio k must be finite.')
        fixed, resolution, floor = self._offset_fixed(), self._offset_resolution(), self._offset_minimum()
        low, high = float(self.start), float(self.stop)
        if not (math.isfinite(low) and math.isfinite(high)):
            raise ScanError('Amplitude from/to must be finite.')
        if factor == 0:
            if len(sources) == 2:
                return f'RF only (k = 0): both pairs held at x = {fixed:g} V, no DC between the pairs.'
            return (f'k = 0: pair 1 held at x = {fixed:g} V. Pair 2 not driven: '
                    'the DC between the pairs depends on its potential.')
        u = [max(floor, abs(factor) * a - resolution) for a in (low, high)]
        line = f'{abs(factor):g} × A − {resolution:g}' if resolution else f'{abs(factor):g} × A'
        lines = [f'U = max({floor:g}, {line}) = {u[0]:g} → {u[1]:g} V for A = {low:g} → {high:g} V']
        for c, sign in sources:
            first, last = (self._offset_value(fixed, sign, factor, resolution, floor, a) for a in (low, high))
            lines.append(f'Pair {1 if sign > 0 else 2} = x {"+" if sign * factor > 0 else "−"} U = '
                         f'{first:+g} → {last:+g} V')
        if len(sources) == 1:
            lines.append('Pair 2 not driven: the DC between the pairs also depends on its potential.')
        if factor < 0:
            lines.append('k < 0: pairs swapped, pair 1 below x.')
        if abs(factor) >= self.SQUARE_WAVE_APEX:
            lines.append(f'|k| ≥ {self.SQUARE_WAVE_APEX:g}: above the apex of an ideal 50 % square wave, '
                         'no ion is expected through the filter.')
        elif resolution == 0 and abs(factor) >= .15:
            lines.append(f'm/Δm ≈ {.157 / (self.SQUARE_WAVE_APEX - abs(factor)):.2g} '
                         '(ideal square wave, rods at amplitude A)')
        return '\n'.join(lines)

    def _refresh_filter_preview(self):
        try:
            text = self._filter_preview_text()
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            text = f'Unavailable: {exc}'
        setting = self.settingsMgr.settings[self.FILTER_PREVIEW]
        if setting.value != text:
            setting.value = text

    def _mz_changed(self):
        if not self.loading and not self.settingsMgr.loading:
            self._refresh_interface()
            self.plot(update=False)

    @staticmethod
    def _frequency_hz(rows):
        """AMX output frequency from its decoded rows (source period, else the displayed text)."""
        values, units = [], {'Hz': 1., 'kHz': 1e3, 'MHz': 1e6}
        for row in rows:
            waveform = row.get('waveform') or {}
            source = waveform.get('source') or {}
            if waveform.get('clock_hz') and source.get('period'):
                values.append(float(waveform['clock_hz']) / float(source['period']))
                continue
            text = str(row.get('frequency', '')).split()
            if len(text) == 2 and text[1] in units:
                try:
                    values.append(float(text[0]) * units[text[1]])
                except ValueError:
                    pass
        if (len(values) != len(rows) or not values or any(not math.isfinite(v) or v <= 0 for v in values)
                or max(values) - min(values) > 1e-6 * max(values)):
            raise ScanError('AMX frequency unavailable: m/z needs it.')
        return values[0]

    def _mz_inputs(self, metadata=None):
        """Frequency, r0 and mass filter of a scan (its metadata) or of the present settings."""
        if metadata:
            offset = metadata.get('offset')
            rows = metadata.get('waveform') or []
            mass_filter = (dict(factor=float(offset['coefficient']), resolution=float(offset.get('resolution_offset_v', 0.)),
                                minimum=float(offset.get('minimum_u_v', .5)), pairs=len(offset.get('pairs') or []))
                           if offset else dict(factor=0.))
        else:
            sources = self._offset_sources()
            mass_filter = (dict(factor=float(self.offset_factor), resolution=self._offset_resolution(),
                                minimum=self._offset_minimum(), pairs=len(sources)) if sources else dict(factor=0.))
            rows = self._amx_rows(self._plugin(self.amx_name), self._selected_outputs())
        return dict(frequency_hz=self._frequency_hz(rows), r0_mm=float(self.mz_r0_mm), **mass_filter)

    def _scan_metadata(self):
        return (self._plan or {}).get('metadata') or None

    def _mz_scale_text(self):
        inputs = self._mz_inputs()
        low, high = float(self.start), float(self.stop)
        mz = mz_from_amplitude([low, high], **inputs)
        if inputs['factor'] and inputs.get('pairs') != 2:
            return 'No m/z with one rod pair: the DC between the pairs is unknown.'
        if not np.isfinite(mz).any():
            return 'No m/z: U/A above the apex over the whole scan, no ion transmitted.'
        quantity = 'peak centre' if inputs['factor'] else 'low-mass cut-off, RF only'
        shown = ' → '.join(f'{value:.0f}' if math.isfinite(value) else 'none' for value in mz)
        return (f'A = {low:g} → {high:g} V ≈ m/z {shown} ({quantity})\n'
                f'{inputs["frequency_hz"] / 1e3:g} kHz, r0 {inputs["r0_mm"]:g} mm; theory until r0 is calibrated')

    def _refresh_mz_scale(self):
        try:
            text = self._mz_scale_text()
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            text = f'Unavailable: {exc}'
        setting = self.settingsMgr.settings[self.MZ_SCALE]
        if setting.value != text:
            setting.value = text

    def _plot_axis(self, amplitudes):
        """Abscissa values and label: A, or m/z when chosen and computable for this scan."""
        if getattr(self, 'mz_axis', self.AXIS_AMPLITUDE) == self.AXIS_MZ and len(amplitudes):
            try:
                inputs = self._mz_inputs(self._scan_metadata())
                values = mz_from_amplitude(amplitudes, **inputs)
                if np.isfinite(values).any():
                    return values, 'm/z (Th)' if inputs['factor'] else 'Low-mass cut-off m/z (Th), RF only'
            except (ScanError, AttributeError, KeyError, TypeError, ValueError):
                pass  # The amplitude axis stays: the m/z scale row says why.
        return amplitudes, self._axis_label()

    def estimateScanTime(self):
        if not all(hasattr(self, attr) for attr in ('start', 'stop', 'step', 'settling_s', 'integration_s')):
            return
        try:
            steps = self._scan_steps()
            mode, duration = self._timing()
            if mode == self.CONTINUOUS:
                self.scantime = f'{len(steps) * duration + (self._passes() + 1) * self.settling_s:g} s + control/data delays'
            else:
                self.scantime = f'{len(steps) * (self.settling_s + duration) + self.settling_s:g} s + ramping/data delays'
        except (ScanError, AttributeError):
            self.scantime = 'Invalid amplitude range or timing'

    def _timing(self):
        mode = getattr(self, 'scan_mode', self.STEPPED)
        if mode not in (self.STEPPED, self.CONTINUOUS):
            raise ScanError('Select Step by step or Continuous scan mode.')
        duration = float(self.time_step_s if mode == self.CONTINUOUS else self.integration_s)
        if not math.isfinite(duration) or duration <= 0:
            raise ScanError('Time step must be positive and finite.' if mode == self.CONTINUOUS
                            else 'Measurement time must be positive and finite.')
        return mode, duration

    def _dmmr_interval_setting(self):
        device = self._dmmr_device()
        settings = getattr(self.pluginManager.Settings, 'settings', {})
        setting = settings.get(f'{device.name}/{device.INTERVAL}')
        if setting is None or setting.spin is None:
            raise ScanError('DMMR Interval setting unavailable. Initialize the DMMR.')
        return device, setting

    def _dmmr_interval_editable(self, *, before_start=False):
        return (self.finished and (before_start or not self.recording)
                and not any(isinstance(p, Scan) and not p.finished for p in self.pluginManager.plugins if p is not self))

    def _sync_dmmr_interval(self):
        setting = self.settingsMgr.settings[self.DMMR_INTERVAL]
        spin = setting.spin
        # Disabling a focused field emits editingFinished too. A display sync
        # must not re-enter the command callback while locking for acquisition.
        previous = spin.blockSignals(True)
        try:
            device, source = self._dmmr_interval_setting()
            self._cadence_epoch(device)
            spin.setEnabled(self._dmmr_interval_editable())
            if spin.hasFocus() or spin.lineEdit().hasFocus():
                return  # never overwrite unsubmitted keyboard input
            spin.setSpecialValueText('')
            spin.setRange(source.spin.minimum(), source.spin.maximum())
            spin.setValue(int(source.value))
            spin.setToolTip(setting.toolTip)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            spin.setEnabled(False)
            spin.setRange(0, 0)
            spin.setSpecialValueText('Unavailable')
            spin.setToolTip(str(exc))
        finally:
            spin.blockSignals(previous)

    def _dmmr_interval_changed(self, *, before_start=False):
        if self.loading or self.settingsMgr.loading or self.pluginManager.loading or self.pluginManager.closing:
            return False
        applied = True
        try:
            if not self.finished or (self.recording and not before_start):
                raise ScanError('Cannot change the DMMR interval during a scan.')
            if not self._dmmr_interval_editable(before_start=before_start):
                raise ScanError('Finish the other scan before changing the shared DMMR interval.')
            device, source = self._dmmr_interval_setting()
            requested = int(self.settingsMgr.settings[self.DMMR_INTERVAL].value)
            if not source.spin.minimum() <= requested <= source.spin.maximum():
                raise ScanError('DMMR interval outside its software setting limits.')
            if source.value != requested:
                self._cadence_epoch(device)
                source.value = requested  # same live Setting, on Qt; no direct driver command
                if not source.instantUpdate:
                    source.changedEvent()  # commit its native callback and persistence, just like Enter
                self._cadence_epoch(device)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            applied = False
            self.print(str(exc), flag=PRINT.WARNING)
        self._sync_dmmr_interval()
        self._refresh_interface()
        return applied

    def _cadence_epoch(self, device):
        configured = float(getattr(device, 'interval', np.nan)) / 1000
        if not math.isfinite(configured) or configured <= 0:
            raise ScanError('DMMR polling interval unavailable.')
        previous = getattr(self, '_dmmr_cadence_epoch', None)
        if previous is None:
            self._dmmr_cadence_epoch = (device, configured, -np.inf)
        elif previous[0] is not device or previous[1] != configured:
            # Do not mix old/fast or old/slow samples with the newly configured
            # interval. Keep the actual history intact for all users of DMMR.
            self._dmmr_cadence_epoch = (device, configured, time.time())
        return configured, self._dmmr_cadence_epoch[2]

    def _detector_cadence(self, modules):
        """Bound the scan by the DMMR's usable history: a tick counts when a recorded module has a reading."""
        device = modules[0].getDevice()
        if not device.recording or not any(c.acquiring for c in modules):
            raise ScanError('DMMR acquisition/recording stopped.')
        configured, since = self._cadence_epoch(device)
        _, times, valid = self._dmmr_clock(modules)
        self._require_monotonic(device, times, 'DMMR: invalid timestamps.')
        valid_times = self._last_finite_times(times, np.where(valid, 0., np.nan), since, 21)
        if len(valid_times) < 2:
            reason = 'after DMMR interval change' if math.isfinite(since) else 'to determine Time step'
            raise ScanError(f'DMMR: waiting for two valid recorded samples {reason}.')
        intervals = np.diff(valid_times)
        largest = float(intervals.max())
        if not 0 <= time.time() - valid_times[-1] <= max(2., 3 * configured, 3 * largest):
            raise ScanError('DMMR: waiting for fresh valid samples.')
        # Round Unix timestamp subtraction to microseconds before the GUI's
        # millisecond ceiling; binary roundoff must not add a spurious ms.
        micros = round(max(configured, largest) * 1e6)
        return dict(configured_interval_s=configured, typical_interval_s=float(np.median(intervals)),
            largest_interval_s=largest, minimum_time_step_s=((micros + 999) // 1000) / 1000,
            last_sample_time=float(valid_times[-1]), sample_count=len(valid_times))

    def _require_monotonic(self, device, times, message):
        """Require finite, strictly increasing timestamps over the whole history.

        Called on Qt every poll: rescan only samples appended since the last
        validated prefix. Thinning or clearing changes the prefix endpoints and
        forces a full rescan, so the result equals a full validation.
        """
        cache = getattr(self, '_validated_times', None)
        if cache is None:
            cache = self._validated_times = {}
        key, first = id(device.time), 0
        known = cache.get(key)
        if known is not None:
            start, count, end = known
            if 0 < count <= len(times) and times[0] == start and times[count - 1] == end:
                first = count - 1
        tail = times[first:]
        if not np.isfinite(tail).all() or np.any(np.diff(tail) <= 0):
            cache.pop(key, None)
            raise ScanError(message)
        if len(times):
            cache[key] = (float(times[0]), len(times), float(times[-1]))

    @staticmethod
    def _last_finite_times(times, values, since, count):
        """Last ``count`` timestamps after ``since`` with a finite value (sorted times)."""
        lower = int(np.searchsorted(times, since, side='right'))
        found, end, chunk = [], len(times), 256
        while end > lower and sum(map(len, found)) < count:
            begin = max(lower, end - chunk)
            found.insert(0, times[begin:end][np.isfinite(values[begin:end])])
            end, chunk = begin, chunk * 4
        return np.concatenate(found)[-count:] if found else times[:0]

    def _require_time_step(self, modules, duration, window_start=None):
        cadence = self._detector_cadence(modules)
        minimum = cadence['minimum_time_step_s']
        if duration < minimum:
            raise ScanError(f'DMMR: Time step must be at least {minimum:g} s for the DMMR cadence '
                            f'(requested {duration:g} s). Increase Time step or change DMMR acquisition settings.')
        if window_start is not None and cadence['last_sample_time'] <= window_start:
            raise ScanError('DMMR: no new valid sample within Time step. '
                            'Scan stopped without advancing; increase Time step or check DMMR acquisition.')
        return cadence

    def _acquisition_info(self):
        try:
            running = self._plan is not None and self._plan.get('detectors') and not self.finished
            cadence = self._detector_cadence(self._plan['detectors'] if running else self._modules())
            text = (f'≈ {cadence["typical_interval_s"]:.3g} s between valid samples; '
                    f'Time step ≥ {cadence["minimum_time_step_s"]:g} s')
        except (ScanError, AttributeError, ValueError, TypeError) as exc:
            text = str(exc)
        self.settingsMgr.settings[self.CADENCE].value = text
        counts, finite = self._validation.get('samples'), self._validation.get('finite_samples')
        windows = self._validation.get('window_end')
        acquired = np.flatnonzero(np.isfinite(windows)) if windows is not None else []
        text = 'Not acquired'
        if len(acquired) and counts is not None:
            row = acquired[-1]
            names = [c.name for c in self.outputChannels][:counts.shape[1]] if np.ndim(counts) == 2 else []
            text = ', '.join(f'{name} {int(finite[row, j])}/{int(counts[row, j])}' for j, name in enumerate(names))
            text = f'{text} valid/total' if text else 'Not acquired'
        self.settingsMgr.settings[self.SAMPLES].value = text

    def scanUpdate(self, done=False):
        self._acquisition_info()
        super().scanUpdate(done=done)

    def _setup_changed(self):
        if not self.loading and not self.settingsMgr.loading:
            self._refresh_interface()
            self.dummyInitialization()

    def updateDisplayDefault(self):
        # Called by native plot/file loading as well as populateDisplayChannel.
        # Changing the available choices must never clear acquired/loaded data.
        if self.display and self.displayActive() and self.useDisplayChannel:
            combo = self.display.displayComboBox
            previous = combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findText(self.displayDefault)))
            combo.blockSignals(previous)
            self.updateDisplayChannel()

    def _completion_text(self, rails, *, running=False):
        lines = []
        for r in rails:
            voltage = r['initial'] if running else self._number(r['channel'], 'voltage_setpoint_readback', 'Vset readback')
            try:
                current = f'Iget {self._number(r["channel"], "current_readback", "current measurement") * 1000:g} mA'
            except ScanError:
                current = 'Iget unavailable'
            lines.append(f'{r["name"]}: {"+" if r["number"] == 0 else "−"}{voltage:g} V, {current}')
        if lines:
            try:
                if running:
                    offsets = [(o['name'], o['initial'], o['pair']) for o in (self._plan or {}).get('offsets') or []]
                else:
                    offsets = [(c.name, float(c.value), 1 if sign > 0 else 2) for c, sign in self._offset_sources()]
                for name, value, pair in offsets:
                    lines.append(f'{name}: {value:+g} V (rod pair {pair})')
            except ScanError:
                lines.append('Rod pairs: unavailable')
        return '\n'.join(lines) or 'Unavailable'

    def _refresh_completion(self, rails, *, running=False):
        try:
            text = self._completion_text(rails, running=running)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError):
            text = 'Unavailable'
        setting = self.settingsMgr.settings[self.FINAL_VOLTAGES]
        if setting.value != text:
            setting.value = text
        setting.label.setToolTip(f'{text}\n\n{setting.toolTip}')

    def _readback_text(self, rails):
        lines = []
        for r in rails:
            c, sign = r['channel'], '+' if r['number'] == 0 else '−'
            try:
                monitor = self._number(c, 'monitor', 'voltage readback')
                measured = f'{sign}{monitor:.2f} V'
            except ScanError:
                measured = 'no readback'
            try:
                setpoint = f'{sign}{self._number(c, "voltage_setpoint_readback", "Vset readback"):g} V'
            except ScanError:
                setpoint = 'unconfirmed'
            dropout, full = self._rail_headroom(r)
            headroom = f', headroom {dropout:.1f} V' if math.isfinite(dropout) else ''
            lines.append(f'{r["name"]}: {measured} (set {setpoint}){headroom}, {self._range_text(full)}')
        return '\n'.join(lines) or 'Select an AMX'

    def _refresh_readback(self, rails):
        try:
            text = self._readback_text(rails)
        except (AttributeError, KeyError, TypeError, ValueError):
            text = 'Unavailable'
        setting = self.settingsMgr.settings[self.SUPPLY_READBACK]
        if setting.value != text:
            setting.value = text

    def _refresh_offset_readback(self, *, running=False):
        try:
            text = self._offset_readback_text(running=running)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            text = f'Unavailable: {exc}'
        setting = self.settingsMgr.settings[self.OFFSET_READBACK]
        if setting.value != text:
            setting.value = text

    def _refresh_interface(self):
        if (self.loading or self.settingsMgr.loading or self.pluginManager.loading or self.pluginManager.closing):
            return
        self._sync_dmmr_interval()
        if not self.finished or self.recording:
            # Keep setup/readiness frozen during acquisition. Only observe Iget;
            # the return voltage must stay the initial target captured by the plan.
            # A plan restored from a saved file holds only its metadata (loadDataInternal).
            self._refresh_completion((self._plan or {}).get('rails', []), running=True)
            self._refresh_readback((self._plan or {}).get('rails', []))
            self._refresh_offset_readback(running=True)
            self._layout_readonly_fields()
            return
        channels = list(self.pluginManager.DeviceManager.channels())
        try:
            modules = self._modules()
            device = modules[0].getDevice()
            recorded = ', '.join(f'M{c.module_address()} {c.name}' for c in modules)
            live = device.recording and any(c.acquiring for c in modules)
            modules_text = f'{recorded}\n{"Recording" if live else "Start DMMR acquisition and recording"}'
        except (ScanError, AttributeError, TypeError, ValueError) as exc:
            modules_text = str(exc)
        if self.settingsMgr.settings[self.MODULES].value != modules_text:
            self.settingsMgr.settings[self.MODULES].value = modules_text
        amx_setting = self.settingsMgr.settings[self.AMX]
        if not amx_setting.combo.view().isVisible():
            choices = ['None', *sorted({p.name for p in self.pluginManager.plugins if p.name in self.AMX_PLUGINS})]
            selected_amx = str(amx_setting.value)
            if selected_amx not in choices:
                choices.append(selected_amx)
            previous = amx_setting.combo.blockSignals(True)
            if amx_setting.items != choices:
                amx_setting.combo.clear()
                amx_setting.combo.addItems(choices)
            amx_setting.combo.setCurrentText(selected_amx)
            amx_setting.combo.blockSignals(previous)
        ampr = sorted((c for c in channels if self._is_ampr(c)),
                      key=lambda c: (c.getDevice().name, c.module_address(), c.channel_number(), c.name))
        for key in (self.OFFSET, self.OFFSET_2):
            offset_setting = self.settingsMgr.settings[key]
            if offset_setting.combo.view().isVisible():
                continue
            choices = [self.NO_OFFSET, *[self._offset_label(c) for c in ampr]]
            selected_offset = str(offset_setting.value)
            if selected_offset not in choices:
                choices.append(selected_offset)  # never silently replace a lost selection
            previous = offset_setting.combo.blockSignals(True)
            if offset_setting.items != choices:
                offset_setting.combo.clear()
                offset_setting.combo.addItems(choices)
            offset_setting.combo.setCurrentText(selected_offset)
            offset_setting.combo.blockSignals(previous)
        self._acquisition_info()
        self.estimateScanTime()
        supplies, frequency = 'Select an AMX', 'Unavailable'
        try:
            amx = self._plugin(self.amx_name)
            selected_outputs = self._selected_outputs()
            links = {0: str(amx.psu_ch01), 2: str(amx.psu_ch23)}
            names = list(dict.fromkeys(links[i] for i in (0, 2) if i in selected_outputs))
            lines = []
            for name in names:
                if name in ('', 'None'):
                    lines.append('PSU not associated — configure AMX Settings')
                    continue
                try:
                    self._plugin(name)
                    lines.append(f'{name}: CH0 = CH1 = A')
                except ScanError:
                    lines.append(f'{name}: unavailable')
                shared = []
                for other in self.pluginManager.plugins:
                    if other.name not in self.AMX_PLUGINS:
                        continue
                    for i, attr in ((0, 'psu_ch01'), (2, 'psu_ch23')):
                        if str(getattr(other, attr, 'None')) == name and (other is not amx or i not in selected_outputs):
                            shared.append(f'{other.name} CH{i}-CH{i + 1}')
                if shared:
                    lines.append('Also affects ' + ', '.join(shared))
            supplies = '\n'.join(lines)
            # A known period is not an exact phase/dwell measurement. Keep it
            # visible even when a different readback requirement blocks Start.
            rows = self._amx_rows(amx, selected_outputs)
            frequencies = [r.get('frequency', 'Unavailable')
                if r.get('state') == 'Periodic' else 'Unavailable' for r in rows]
            frequency = frequencies[0] if len(set(frequencies)) == 1 else '\n'.join(
                f'CH{i}: {value}' for i, value in zip(selected_outputs, frequencies))
        except (ScanError, AttributeError, KeyError, TypeError, ValueError):
            pass  # unavailable is not a measurement; the preflight explains any blocker
        limits, currents = 'Unavailable', 'Unavailable'
        rails = []
        limit_help = ('Read-only admissible range. Choose the sweep with Amplitude from/to. '
                      'Channel Min/Max can restrict it; the PSU hardware ceiling cannot be overridden. '
                      '"Reachable now" is output + dropout: the PSU cannot regulate above it, and less at a '
                      'larger load. Start is refused above it minus 2 V; Full range raises it.')
        try:
            rails = self._associated_rails(self._plugin(self.amx_name), self._selected_outputs())
            low, high = self._amplitude_range(rails)
            limits = f'{low:g} – {high:g}'
            reachable = self._headroom_ceiling(rails)
            if reachable is not None and reachable[0] < high:
                ceiling, rail, full = reachable
                limits += f'\nReachable now ≤ {ceiling:.0f} V ({rail["name"]}, {self._range_text(full)})'
            limit_help += '\n' + '\n'.join(
                f'{r["device"].name} CH{r["number"]}: channel {r["channel"].min:g}–{r["channel"].max:g} V; '
                f'PSU-reported maximum {r["channel"].hardware_voltage_limit:g} V (current range).'
                for r in rails)
            currents = '\n'.join(f'{r["device"].name} CH{r["number"]}: '
                f'{self._number(r["channel"], "current_limit_readback", "Ilim readback") * 1000:g} mA '
                f'(max {self._number(r["channel"], "hardware_current_limit", "current capacity") * 1000:g} mA)'
                for r in rails)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError):
            pass
        for key, value in ((self.SUPPLIES, supplies), (self.FREQUENCY, frequency),
                           (self.AMPLITUDE_LIMITS, limits), (self.CURRENT_LIMITS, currents)):
            setting = self.settingsMgr.settings[key]
            if setting.value != value:
                setting.value = value
        self.settingsMgr.settings[self.AMPLITUDE_LIMITS].label.setToolTip(limit_help)
        self.settingsMgr.settings[self.AMPLITUDE_LIMITS].setToolTip(0, limit_help)
        self._refresh_completion(rails)
        self._refresh_readback(rails)
        self._refresh_offset_readback()
        self._refresh_filter_preview()  # Frozen during a scan with the settings it shows.
        self._refresh_mz_scale()
        # Same preflight as Start, but do not initialize data, bind a plan, log
        # warnings or touch a device. Keep the last outcome (including save errors).
        try:
            plan = self._preflight()
            self._ready = True
            status = 'Ready to scan'
            if plan.get('headroom_warning'):
                status += f'\nWarning: {plan["headroom_warning"]}'
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            self._ready = False
            status = f'Not ready: {exc}'
        if self._last_scan_status:
            status = f'Last scan: {self._last_scan_status}\n{status}'
        if self.scan_status != status:
            self.scan_status = status
        self._update_scan_action()
        self._layout_readonly_fields()

    def loadSettings(self, file=None, useDefaultFile=False):
        if not self.finished:
            self.print('Cannot load settings while a scan is running or being saved.', flag=PRINT.WARNING)
            return
        self._arranging = True  # Scan.loadSettings unfolds every group; keep the operator's choice.
        try:
            super().loadSettings(file=file, useDefaultFile=useDefaultFile)
        finally:
            self._arranging = False
        self._format_settings()
        self.toggleAdvanced()
        self._refresh_interface()
        self.dummyInitialization()

    def initData(self):
        for channel in self.channels:
            channel.onDelete()
        self.channels, self.inputChannels, self.outputChannels = [], [], []

    def connectAllSources(self):
        for channel in self.channels:
            channel.connectSource()

    def getSteps(self, start, stop, step):
        try:
            return amplitude_steps(start, stop, step)
        except ScanError:
            return None

    def _command_step(self):
        if getattr(self, 'scan_mode', self.STEPPED) != self.CONTINUOUS:
            return float(self.step)
        rate = float(self.sweep_rate)
        _, duration = self._timing()
        if not math.isfinite(rate) or rate <= 0:
            raise ScanError('Sweep rate must be positive and finite.')
        increment = rate * duration
        if not math.isfinite(increment) or increment <= 0:
            raise ScanError('Sweep rate × Time step must give a finite positive voltage increment.')
        return increment

    def _scan_steps(self):
        start, stop, step = float(self.start), float(self.stop), self._command_step()
        span = abs(stop - start)
        # Match amplitude_steps' floating-point margin for an aligned endpoint.
        if (getattr(self, 'scan_mode', self.STEPPED) == self.CONTINUOUS
                and 0 < span and span / step + 1e-10 < 1):
            raise ScanError(f'Sweep rate × Time step gives {step:g} V per command, exceeding the {span:g} V scan span. '
                            'Reduce Sweep rate or Time step.')
        return np.tile(amplitude_steps(start, stop, step), self._passes())

    def _passes(self):
        """Repeated sweeps, in both modes."""
        passes = getattr(self, 'passes', 1)
        passes = 1 if passes is None else int(passes)
        if not 1 <= passes <= 1000:
            raise ScanError('Passes must be between 1 and 1000.')
        return passes

    def addInputChannels(self):
        try:
            steps = self._scan_steps()
        except (ScanError, AttributeError, TypeError, ValueError):
            return
        self.addInputChannel('Amplitude', unit='V', recordingData=steps)

    def addInputChannel(self, name, start=None, stop=None, step=None, unit='V', recordingData=None):
        axis = _Amplitude(parentPlugin=self, name=name, unit=unit, recordingData=recordingData, inout=INOUT.IN)
        self.inputChannels.append(axis)
        self.channels.append(axis)

        return axis

    def addOutputChannels(self):
        if not self.inputChannels:
            return
        try:
            planned = (self._plan or {}).get('detectors')
            modules = planned if planned else self._modules()
        except ScanError:
            return  # No synthetic or unavailable channel in the data model.
        self.displayDefault = modules[0].name
        # Every enabled DMMR module, by module number.
        for channel in modules:
            self.addOutputChannel(name=channel.name, unit='A',
                                  recordingData=np.full(len(self.inputChannels[0].recordingData), np.nan, dtype=np.float64))

    def addOutputChannel(self, name, unit='', recordingData=None, recordingBackground=None):
        channel = _Signal(parentPlugin=self, name=name, unit=unit, inout=INOUT.OUT,
            recordingData=recordingData, recordingBackground=recordingBackground)
        self.outputChannels.append(channel)
        self.channels.append(channel)
        return channel

    def _preflight(self):
        """Read-only validation shared by the idle status and actual Start."""
        modules = self._modules()
        for c in modules:
            self._registered(c)
        plan = self._prepare()
        self._check_detectors(modules)
        device = modules[0].getDevice()
        plan['detectors'] = modules
        plan['detector_identity'] = (device, [(c.module_address(), c.name) for c in modules])
        self._cadence_epoch(device)
        plan['detector_interval_ms'] = float(device.interval)
        plan['metadata']['detector'] = dict(
            device=device.name, interval_ms=plan['detector_interval_ms'],
            modules=[dict(module=c.module_address(), channel=c.name) for c in modules],
            windows='DMMR time axis, shared by every recorded module; a tick is valid when at least one module '
                    'has a new reading. Each module is averaged over the same windows.')
        if plan['mode'] == self.CONTINUOUS:
            plan['metadata']['detector_cadence'] = self._require_time_step(modules, plan['average'])
        return plan

    def initScan(self):
        self._plan = None
        self._validation = {}
        if self._dummy_initialization:
            self.addInputChannels()
            self.addOutputChannels()
            self.updateFile()
            self.populateDisplayChannel()
            return bool(self.inputChannels and self.outputChannels)
        self._last_scan_status = ''
        try:
            self._plan = self._preflight()
            if self._plan.get('headroom_warning'):
                self.print(f'Warning: {self._plan["headroom_warning"]}', flag=PRINT.WARNING)
            configured = [c.name for c in self._plan['detectors']]
            self.addInputChannels()
            self.addOutputChannels()
            self.toggleDisplay(visible=True)
            self.updateFile()
            self.populateDisplayChannel()
            if [c.name for c in self.outputChannels] != configured:
                raise ScanError(f'DMMR modules {", ".join(configured)} must be available and recording.')
            self._plan['detectors'] = [c.sourceChannel for c in self.outputChannels]
            self._check_detectors()
            n, rails, outputs = len(self.inputChannels[0].recordingData), len(self._plan['rails']), len(configured)
            self._validation = dict(status='running', error='', point_status=['not acquired'] * n,
                rail_v=np.full((n, rails), np.nan), rail_i=np.full((n, rails), np.nan),
                window_start=np.full(n, np.nan), window_end=np.full(n, np.nan),
                samples=np.zeros((n, outputs), dtype=np.int64), finite_samples=np.zeros((n, outputs), dtype=np.int64),
                rail_dropout=np.full((n, rails), np.nan), low_headroom=np.zeros(n, dtype=bool))
            if self._plan.get('offsets'):
                k = len(self._plan['offsets'])  # One column per rod pair.
                self._validation.update(offset_v=np.full((n, k), np.nan), offset_target=np.full((n, k), np.nan))
            self._cancel = Event()
            self._plot_fault_reported = False  # A plot fault is reported once per scan, not once per session.
            autoscale = getattr(self.display, 'autoscaleAction', None) if self.display else None
            if autoscale is not None:
                autoscale.state = True  # A new scan follows its own data, whatever view the last one had.
            self.scan_status = 'Ready'
            return True
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            self.scan_status = str(exc)
            self.print(f'Cannot start: {exc}', flag=PRINT.WARNING)
            self._plan = None
            return False

    def _file_label(self):
        text = '-'.join(str(getattr(self, 'file_label', '') or '').split())
        text = ''.join(char if char.isalnum() or char in '-_' else '-' for char in text).strip('-_')
        return text[:60]

    def _scan_file(self, now=None):
        """<Data path>/MScan/<date>/<date_time>[_<name>]_PSU-<X>_<from>-<to>V.h5 (user choice 2026-10-26)."""
        now = time.localtime() if now is None else now
        folder = Path(self.pluginManager.Settings.dataPath) / self.FOLDER / time.strftime('%Y-%m-%d', now)
        span = f'{float(self.start):g}-{float(self.stop):g}V'
        parts = [time.strftime('%Y-%m-%d_%H-%M-%S', now), self._file_label(), self._psu_tag(separator='-').replace(' ', '-'), span]
        stem = '_'.join(part for part in parts if part)
        candidate, n = folder / f'{stem}.h5', 2
        while candidate.exists():
            candidate, n = folder / f'{stem}_{n}.h5', n + 1
        return candidate

    def updateFile(self):
        if self._dummy_initialization:
            self.file = Path()
        else:
            self.file = self._scan_file()
            self.file.parent.mkdir(parents=True, exist_ok=True)
        if self.displayActive() and self.display:
            self.display.file = self.file

    def _plugin(self, name):
        matches = [p for p in self.pluginManager.plugins if p.name == name]
        if len(matches) != 1:
            raise ScanError(f'Plugin {name} is missing or ambiguous.')
        return matches[0]

    def _registered(self, channel):
        manager = self.pluginManager.DeviceManager
        matches = [c for c in manager.channels() if c.name.strip().lower() == channel.name.strip().lower()]
        if len(matches) != 1 or matches[0] is not channel or manager.getChannelByName(channel.name) is not channel:
            raise ScanError(f'Channel {channel.name} is missing, renamed or ambiguous.')

    def _selected_outputs(self):
        if self.amx_outputs not in self.PAIRS:
            raise ScanError('Select one AMX pair: CH0-CH1 or CH2-CH3.')
        return self.PAIRS[self.amx_outputs]

    def _amx_rows(self, amx, selected):
        controller = amx.controller
        if controller.initializing or controller.transitioning:
            raise ScanError(f'{amx.name}: wait for the AMX transition to finish.')
        if not controller.initialized or controller.device is None or not amx.isOn():
            raise ScanError(f'{amx.name}: turn the AMX ON.')
        rows = controller.output_rows
        if not rows or len(rows) != 4:
            state = str(getattr(controller, 'main_state', '') or 'unknown')
            hint = (f'turn {amx.name} recording ON: its outputs are decoded from its readbacks'
                    if getattr(amx, 'recording', True) is False else
                    'wait for its next readback, a few seconds after a config load or a timing edit')
            raise ScanError(f'{amx.name}: no decoded AMX waveform yet (state {state}); {hint}.')
        return [rows[i] for i in selected]

    def _waveform(self, amx, selected):
        chosen = self._amx_rows(amx, selected)
        for number, row in zip(selected, chosen):
            if row.get('state') != 'Periodic':
                raise ScanError(f'{amx.name} CH{number}: {row.get("state", "Unknown")}. '
                    f'{row.get("detail", "Check the AMX output routing and readbacks.")}')
            # Current AMX versions publish source timing AND per-edge raw
            # delay registers. Earlier zero-delay summaries remain usable;
            # never silently ignore nonzero or missing delay registers.
            if not (row.get('waveform') or row.get('timing')):
                raise ScanError(f'{amx.name} CH{number}: AMX edge-delay readback unavailable. '
                    'Update AMX and MScan together; check the AMX readback. Do not zero compensation delays.')
        return json.dumps(chosen, sort_keys=True, allow_nan=False)

    @staticmethod
    def _number(channel, attr, label, *, nonnegative=True):
        try:
            value = float(getattr(channel, attr))
        except (AttributeError, TypeError, ValueError):
            value = np.nan
        if not math.isfinite(value) or (nonnegative and value < 0):
            raise ScanError(f'{channel.name}: {label} unavailable or invalid; wait for a fresh PSU readback.')
        return value

    def _voltage_bounds(self, c):
        minimum = self._number(c, 'min', 'channel minimum', nonnegative=False)
        maximum = self._number(c, 'max', 'channel maximum', nonnegative=False)
        ceiling = self._number(c, 'hardware_voltage_limit', 'hardware voltage limit')
        low, high = max(0., minimum), min(maximum, ceiling)
        if low > high:
            raise ScanError(f'{c.name}: no admissible voltage range.')
        return low, high

    def _amplitude_range(self, rails):
        bounds = [self._voltage_bounds(r['channel']) for r in rails]
        low, high = max(b[0] for b in bounds), min(b[1] for b in bounds)
        if low > high:
            raise ScanError('The selected PSUs have no common amplitude range.')
        return low, high

    def _current_limits(self, c):
        ilim = self._number(c, 'current_limit_readback', 'Ilim readback')
        capacity = self._number(c, 'hardware_current_limit', 'hardware current limit')
        if not 0 < ilim <= capacity:
            raise ScanError(f'{c.name}: Ilim must be positive and within the hardware current limit.')
        return ilim, capacity

    def _check_current(self, c, ilim):
        current = self._number(c, 'current_readback', 'current measurement')
        if current >= ilim:
            raise ScanError(f'{c.name}: measured current {current * 1000:g} mA reached/exceeded '
                            f'Ilim {ilim * 1000:g} mA; point not acquired. MScan does not switch HV OFF.')
        return current

    @staticmethod
    def _rail_headroom(rail):
        """Present dropout of a PSU rail (V, NaN when not reported) and its range (True Full, False Half, None)."""
        ctrl, number = rail['controller'], rail['number']
        try:
            dropout = float((getattr(ctrl, 'dropout_values', None) or {}).get(number, math.nan))
        except (TypeError, ValueError):
            dropout = math.nan
        full = (getattr(ctrl, 'full_range_by_channel', None) or {}).get(number)
        return dropout, None if full is None else bool(full)

    @staticmethod
    def _range_text(full):
        return 'Full range' if full else 'Half range' if full is False else 'range unknown'

    def _headroom_ceiling(self, rails):
        """Highest output each rail can reach at its present load: output + dropout. The load only grows
        with A, and the internal supply only falls with the load, so no larger amplitude is reachable."""
        ceilings = []
        for r in rails:
            dropout, full = self._rail_headroom(r)
            monitor = float(getattr(r['channel'], 'monitor', math.nan))
            if math.isfinite(dropout) and dropout >= 0 and math.isfinite(monitor):
                ceilings.append((abs(monitor) + dropout, r, full))
        return min(ceilings, key=lambda item: item[0]) if ceilings else None

    def _headroom_advice(self, rail, full):
        return (f'Switch {rail["device"].name} to Full range or lower Amplitude to.' if full is False
                else 'Lower Amplitude to, or the RF load (frequency).')

    def _headroom_guard(self):
        """Stop at once when a rail is out of tolerance with no regulator headroom left: it cannot reach its target.

        Two different PSU readings in a row are required, so a single reading never stops the scan; a rail still
        rising keeps its headroom. Without this the scan waited for the whole settle timeout (2026-11-03).
        """
        p = self._plan
        if not p or not p.get('rails'):
            return
        state = p.setdefault('headroom_lost', {})
        samples = p.setdefault('headroom_samples', deque(maxlen=4096))
        now = time.time()
        dropouts = []
        for rail, expected in zip(p['rails'], p['expected']):
            c = rail['channel']
            dropout, full = self._rail_headroom(rail)
            dropouts.append(dropout)
            try:
                v = float(c.monitor)
            except (TypeError, ValueError):
                v = math.nan
            lost = (math.isfinite(dropout) and math.isfinite(v) and dropout < self.HEADROOM_LOST_V
                    and abs(v - expected) > p['tolerance'])
            if not lost:
                state.pop(rail['name'], None)
                continue
            seen = state.setdefault(rail['name'], set())
            seen.add((v, dropout))
            if len(seen) >= 2:
                try:
                    current = f', {float(c.current_readback) * 1000:.0f} mA'
                except (TypeError, ValueError):
                    current = ''
                raise ScanError(f'{c.name} cannot reach {expected:g} V: {rail["device"].name} has no headroom left '
                                f'(dropout {dropout:.2f} V at {v:.2f} V{current}, {self._range_text(full)}). '
                                + self._headroom_advice(rail, full))
        samples.append((now, dropouts))

    def _store_headroom(self, index, start, end):
        """Lowest dropout of each rail over the point window, and whether the point was measured below HEADROOM_LOW_V."""
        validation, p = self._validation, self._plan or {}
        if 'rail_dropout' not in validation:
            return
        samples = [d for t, d in p.get('headroom_samples', ()) if start <= t <= end]
        if not samples and p.get('headroom_samples'):
            samples = [p['headroom_samples'][-1][1]]  # PSU polls are slower than short windows.
        if not samples:
            return
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)  # A rail that never reported its dropout.
            lowest = np.nanmin(np.asarray(samples, dtype=float), axis=0)
        validation['rail_dropout'][index] = lowest
        validation['low_headroom'][index] = bool(np.any(lowest < self.HEADROOM_LOW_V))

    def _associated_rails(self, amx, selected):
        """Resolve the actual shared Channels without driver calls or commands."""
        names = [str(getattr(amx, attr)) for attr in ('psu_ch01', 'psu_ch23')
                 if (0 if attr == 'psu_ch01' else 2) in selected]
        rails = []
        for name in dict.fromkeys(names):
            if name in ('', 'None'):
                raise ScanError(f'{amx.name}: associate a PSU in AMX Settings for the selected connectors.')
            psu = self._plugin(name)
            for number in (0, 1):
                candidates = [c for c in psu.getChannels() if c.real
                              and callable(getattr(c, 'channel_number', None)) and c.channel_number() == number]
                if len(candidates) != 1:
                    raise ScanError(f'{name} CH{number} is missing or ambiguous.')
                c = candidates[0]
                self._registered(c)
                rails.append(dict(channel=c, name=c.name, device=psu, controller=psu.controller,
                    backend=psu.controller.device, token=psu.controller._output_cancel, number=number))
        return rails

    def _prepare(self):
        self._scan_steps()
        # Check the *requested endpoints*, even if a non-aligned step would not
        # acquire the last endpoint. Never silently accept an incompatible span.
        low, high = sorted((float(self.start), float(self.stop)))
        mode, duration = self._timing()
        if not all(math.isfinite(float(v)) and float(v) > 0
                   for v in (self.settling_s, self.settle_timeout, self.voltage_tolerance)):
            raise ScanError('Settling, timeout and tolerance must be positive and finite.')
        if float(self.settling_s) >= float(self.settle_timeout):
            raise ScanError('Settle timeout must exceed the settling duration.')
        if self.amx_name in ('', 'None'):
            raise ScanError('Select an AMX.')
        amx = self._plugin(self.amx_name)
        selected = self._selected_outputs()
        waveform = self._waveform(amx, selected)
        links = [(attr, str(getattr(amx, attr))) for attr in ('psu_ch01', 'psu_ch23')
                 if (0 if attr == 'psu_ch01' else 2) in selected]
        rails = self._associated_rails(amx, selected)
        headroom_warning = None
        for r in rails:
            c, psu, ctrl = r['channel'], r['device'], r['controller']
            if not hasattr(c, 'readback_status') or c.unit != 'V' or not c.useMonitors:
                raise ScanError(f'{c.name}: current PSU Channel interface required.')
            if not all(hasattr(c, field) for field in ('hardware_voltage_limit', 'hardware_current_limit',
                       'voltage_setpoint_readback', 'current_limit_readback', 'current_readback', 'voltage_request_revision')):
                raise ScanError(f'{c.name}: update MScan and PSU plugins together; numeric limits/current and command tracking required.')
            if not c.enabled:
                raise ScanError(f'{c.name}: enable the PSU channel.')
            if not manual_control(c):
                raise ScanError(f'{c.name}: driven by its equation; tick Manual in {psu.name} or clear the equation.')
            if not c.initialized or not psu.isOn():
                raise ScanError(f'{c.name}: turn the PSU ON.')
            if any(bool(getattr(ctrl, flag, False)) for flag in
                   ('initializing', 'transitioning', '_manual_apply_active', '_manual_apply_worker_running', '_hv_config_loading')):
                raise ScanError(f'{c.name}: wait for the PSU transition to finish.')
            if not math.isfinite(float(c.monitor)):
                raise ScanError(f'{c.name}: no valid PSU voltage readback.')
            minimum, maximum = self._voltage_bounds(c)
            if low < minimum or high > maximum:
                raise ScanError(f'{c.name}: requested scan {low:g}–{high:g} V exceeds allowed {minimum:g}–{maximum:g} V.')
            initial = self._number(c, 'voltage_setpoint_readback', 'Vset readback')
            if not math.isclose(float(c.value), initial, rel_tol=1e-10, abs_tol=1e-9):
                raise ScanError(f'{c.name}: Vset is not yet confirmed by the PSU.')
            if not minimum <= initial <= maximum:
                raise ScanError(f'{c.name}: final restoration to {initial:g} V is outside allowed {minimum:g}–{maximum:g} V.')
            ilim, imax = self._current_limits(c)
            self._check_current(c, ilim)
            if r['token'].is_set():
                raise ScanError(f'{c.name}: output shutdown requested.')
            dropout, full = self._rail_headroom(r)
            if math.isfinite(dropout) and dropout >= 0:
                level = abs(float(c.monitor))
                ceiling = level + dropout
                if high > ceiling - self.HEADROOM_START_V:
                    raise ScanError(f'{c.name}: at most about {ceiling:.0f} V at its present load '
                                    f'({self._range_text(full)}, dropout {dropout:.1f} V), not Amplitude to {high:g} V '
                                    f'with {self.HEADROOM_START_V:g} V of headroom. ' + self._headroom_advice(r, full))
                margin = self.HEADROOM_START_V + self.HEADROOM_SAG * max(0., high - level)
                if headroom_warning is None and high > ceiling - margin:
                    headroom_warning = (f'{c.name}: about {ceiling:.0f} V reachable now ({self._range_text(full)}), '
                                        'less under load: ' + ('Full range recommended.' if full is False
                                                               else 'Amplitude to is close to the PSU ceiling.'))
            r.update(initial=initial, ilim=ilim, current_capacity=imax,
                     voltage_capacity=float(c.hardware_voltage_limit),
                     request_revision=c.voltage_request_revision, confirmed_vset=initial)
        for plugin in self.pluginManager.plugins:
            if plugin is not self and isinstance(plugin, Scan) and not plugin.finished:
                raise ScanError(f'Finish {plugin.name} before starting this coupled scan.')
        offsets = self._offset_plan(low, high)
        return dict(amx=amx, offsets=offsets, amx_controller=amx.controller, amx_backend=amx.controller.device,
            selected=selected, links=links, waveform=waveform, rails=rails,
            expected=[r['initial'] for r in rails], requested_range=(low, high), timeout=float(self.settle_timeout),
            headroom_warning=headroom_warning,
            tolerance=float(self.voltage_tolerance), average=duration, wait=float(self.settling_s), mode=mode,
            metadata=dict(amx=amx.name, outputs=self.amx_outputs, waveform=json.loads(waveform), links=links,
                mode=mode, passes=self._passes(), points_per_pass=len(self._scan_steps()) // self._passes(),
                settling_s=float(self.settling_s), measurement_s=duration,
                time_step_s=duration if mode == self.CONTINUOUS else None,
                command_step_v=self._command_step(),
                requested_rate_v_s=math.copysign(float(self.sweep_rate), self.stop - self.start)
                    if mode == self.CONTINUOUS else None,
                acquisition=('PC-timed increments; late commands never shorten the next interval to catch up. '
                    'All DMMR samples within actual command intervals, including transitions. '
                    'X is requested amplitude, not simultaneous measured voltage. Rail commands are sequential; '
                    'timestamps are PC/Explorer times, not hardware-synchronized ADC timestamps.'
                    if mode == self.CONTINUOUS else 'Settle each amplitude, then average new DMMR samples.'),
                rails=[dict(channel=r['name'], psu=r['device'].name, rail='Vpos' if r['number'] == 0 else 'Vneg',
                            initial_vset=r['initial'], final_vset=r['initial'],
                            voltage_limit_v=r['voltage_capacity'], ilim_a=r['ilim'],
                            current_limit_a=r['current_capacity'],
                            full_range=self._rail_headroom(r)[1],
                            dropout_at_start_v=(lambda d: d if math.isfinite(d) else None)(self._rail_headroom(r)[0]))
                       for r in rails],
                amplitude_definition='Vpos=+A, Vneg=-A relative to PSU reference; external offset not included',
                offset=None if not offsets else dict(
                    coefficient=offsets[0]['factor'], fixed_v=offsets[0]['fixed'],
                    resolution_offset_v=offsets[0]['resolution'], minimum_u_v=offsets[0]['floor'],
                    pairs=[dict(pair=o['pair'], channel=o['name'], ampr=o['device'].name, module=o['key'][0],
                                ampr_channel=o['key'][1], sign=int(o['sign']), initial_v=o['initial'], final_v=o['initial'])
                           for o in offsets],
                    definition='U = max(minimum_u_v, coefficient × RF amplitude A − resolution_offset_v); pair 1 = fixed_v + U, '
                               'pair 2 = fixed_v − U (coefficient = k = U/V; fixed_v = axis potential x). '
                               'Commanded on each AMPR channel at each amplitude (rounded to its display precision); '
                               'verified by its setpoint readback and Monitor. offset_v and offset_target have one '
                               'column per pair.'),
                calibration='None; amplitude in V, not m/z', background_subtracted=False))

    def _observation(self):
        """GUI-only: immutable readback copy; never performs a hardware read."""
        p = self._plan
        amx = p['amx']
        if (self._plugin(amx.name) is not amx or amx.controller is not p['amx_controller']
                or amx.controller.device is not p['amx_backend']
                or any(str(getattr(amx, attr)) != name for attr, name in p['links'])
                or self._waveform(amx, p['selected']) != p['waveform']):
            raise ScanError('AMX waveform, source or PSU association changed during the scan.')
        measured, currents = [], []
        ready = True
        for rail, expected in zip(p['rails'], p['expected']):
            c, psu, ctrl = rail['channel'], rail['device'], rail['controller']
            self._registered(c)
            if (c.name != rail['name'] or self._plugin(psu.name) is not psu or psu.controller is not ctrl
                    or ctrl.device is not rail['backend'] or ctrl._output_cancel is not rail['token']
                    or rail['token'].is_set() or not psu.isOn() or not ctrl.initialized
                    or not c.enabled or not manual_control(c) or not c.real):
                raise ScanError(f'{rail["name"]}: source changed or was stopped.')
            if c.voltage_request_revision != rail['request_revision']:
                raise ScanError(f'{c.name}: setpoint changed outside the scan '
                                f'(scan target {expected:g} V, new request {float(c.value):g} V).')
            v = float(c.monitor)
            measured.append(v)
            busy = any(bool(getattr(ctrl, flag, False)) for flag in
                       ('initializing', 'transitioning', '_manual_apply_active', '_manual_apply_worker_running', '_hv_config_loading'))
            current = float(c.current_readback)
            currents.append(current)
            # During a write the producer invalidates the numerical fields.
            # Settling can wait for fresh data, acquisition cannot accept a gap.
            fields = ('hardware_voltage_limit', 'hardware_current_limit', 'current_limit_readback', 'voltage_setpoint_readback')
            fresh = not busy and all(math.isfinite(float(getattr(c, f, np.nan))) for f in fields)
            if fresh:
                # The PSU verifies each write before publishing its quantized
                # setpoint. Accept that first echo, not as a new request. Later
                # hardware changes without a scan command must still abort.
                readback = float(c.voltage_setpoint_readback)
                confirmed = rail['confirmed_vset']
                if confirmed is not None and not math.isclose(readback, confirmed, rel_tol=1e-10, abs_tol=1e-9):
                    raise ScanError(f'{c.name}: PSU setpoint changed outside the scan '
                                    f'(confirmed {confirmed:g} V, read back {readback:g} V).')
                rail['confirmed_vset'] = readback
                minimum, maximum = self._voltage_bounds(c)
                if (p['requested_range'][0] < minimum or p['requested_range'][1] > maximum
                        or not minimum <= rail['initial'] <= maximum):
                    raise ScanError(f'{c.name}: voltage limits changed; scan or final restoration is no longer allowed.')
                ilim, _ = self._current_limits(c)
                if not math.isclose(ilim, rail['ilim'], rel_tol=1e-10, abs_tol=1e-12):
                    raise ScanError(f'{c.name}: Ilim changed during the scan.')
                if math.isfinite(current):
                    self._check_current(c, ilim)
            ready &= (fresh and math.isfinite(current) and current >= 0 and math.isfinite(v)
                      and v >= 0 and abs(v - expected) <= p['tolerance'])
        ready = self._offset_observation() and ready
        self._headroom_guard()
        return ready, np.asarray(measured), np.asarray(currents), time.time()

    def _command(self, targets, latest=None, *, offsets=()):
        # Validate *all* sources before the first write. No OFF/ON, ranges or
        # current-limit writes: use the standard Channel.value path only.
        planned, offsets = self._plan.get('offsets') or [], list(offsets or ())
        if len(planned) != len(offsets):
            raise ScanError('Rod pair targets missing or unexpected; no scan command sent.')
        if 'detector_interval_ms' in self._plan:
            self._check_detectors()
        self._observation()
        for o, offset in zip(planned, offsets):
            if any(bool(getattr(o['controller'], flag, False)) for flag in ('initializing', 'transitioning', 'ramping')):
                raise ScanError(f'{o["name"]}: AMPR transition in progress; no scan command sent.')
            minimum, maximum = self._offset_bounds(o['channel'], o['controller'])
            if not math.isfinite(offset) or not minimum <= offset <= maximum:
                raise ScanError(f'{o["name"]}: offset {offset:g} V exceeds allowed {minimum:g}–{maximum:g} V.')
        for rail, target in zip(self._plan['rails'], targets):
            c = rail['channel']
            if any(bool(getattr(rail['controller'], flag, False)) for flag in (
                    'initializing', 'transitioning', '_manual_apply_active', '_manual_apply_worker_running', '_hv_config_loading')):
                raise ScanError(f'{c.name}: PSU transition in progress; no scan command sent.')
            minimum, maximum = self._voltage_bounds(c)
            if not math.isfinite(target) or not minimum <= target <= maximum:
                raise ScanError(f'{c.name}: requested amplitude exceeds allowed {minimum:g}–{maximum:g} V.')
            ilim, _ = self._current_limits(c)
            self._check_current(c, ilim)
        if latest is not None and time.monotonic() >= latest:
            raise ScanError('Continuous time step missed. Increase Time step; no catch-up command sent.')
        started = time.time()
        self._plan['expected'] = list(targets)
        for rail, target in zip(self._plan['rails'], targets):
            rail['channel'].value = target
            revision = rail['channel'].voltage_request_revision
            if revision != rail['request_revision']:
                rail['confirmed_vset'] = None
            rail['request_revision'] = revision
        for o, offset in zip(planned, offsets):
            c = o['channel']
            before = float(c.value)
            c.value = offset
            # The channel rounds to its display precision: verify what was really requested.
            commanded = float(c.value)
            o['requested'], o['expected'], o['ready'] = offset, commanded, False
            o['written'] |= not math.isclose(commanded, before, rel_tol=1e-10, abs_tol=1e-9)
        return started, time.time()

    def _check_detectors(self, sources=None):
        """At Start every recorded module must be ready; during the scan only the DMMR itself must keep
        recording: a module that stops reading gives NaN in its own column and never stops the scan."""
        modules = self._plan['detectors'] if sources is None else sources
        if not modules:
            raise ScanError('Enable at least one DMMR module in the DMMR plugin.')
        device = modules[0].getDevice()
        for source in modules:
            self._registered(source)
            if not self._is_dmmr(source) or source.getDevice() is not device:
                raise ScanError(f'{source.name}: not a current module of the recorded DMMR.')
            if not hasattr(source, 'values'):
                raise ScanError(f'{source.name}: a timestamped Channel history is required.')
            if sources is not None:
                if not source.initialized:
                    raise ScanError(f'{source.name}: initialize the DMMR.')
                if not source.acquiring:
                    raise ScanError(f'{source.name}: start DMMR acquisition.')
        identity = self._plan.get('detector_identity') if sources is None else None
        if identity and (device is not identity[0]
                         or [(c.module_address(), c.name) for c in modules] != identity[1]):
            raise ScanError('DMMR module identity changed during the scan.')
        if sources is None and 'detector_interval_ms' in self._plan and float(device.interval) != self._plan['detector_interval_ms']:
            raise ScanError('DMMR interval changed during the scan. Restart with the new cadence.')
        if not device.recording:
            raise ScanError('DMMR: start recording.')
        if not hasattr(device, 'time'):
            raise ScanError('DMMR: a timestamped history is required.')

    def _history_start(self):
        self._check_detectors()
        t = self._plan['detectors'][0].getDevice().time.get(length=1)  # One DMMR time axis for all modules.
        baseline = float(t[-1]) if len(t) else None
        return time.time(), [(id(c.values), baseline) for c in self._plan['detectors']]

    def _window_samples(self, start, end, baselines, *, closed=True):
        self._check_detectors()
        results = []
        for c, (history, baseline) in zip(self._plan['detectors'], baselines):
            times = np.asarray(c.getDevice().time.get())
            values = np.asarray(c.getValues(subtractBackground=False))
            if history != id(c.values) or len(times) != len(values):
                raise ScanError(f'{c.name}: detector history reset or timestamps misaligned.')
            if baseline is not None and (not len(times) or times[0] > baseline):
                raise ScanError(f'{c.name}: history buffer truncated during averaging.')
            self._require_monotonic(c.getDevice(), times, f'{c.name}: invalid or nonmonotonic timestamps.')
            if closed and (not len(times) or times[-1] < end):
                return None  # wait for a sample closing the acquisition window
            lo, hi = np.searchsorted(times, (start, end), side='right')
            results.append((times[lo:hi], values[lo:hi]))
        return results

    @staticmethod
    def _average_samples(samples):
        # DMMR records NaN at each recorder tick without a new reading: average the readings
        # present (2026-10-26: requiring all finite left every point NaN). Infinity is a wrong reading.
        samples = np.asarray(samples, dtype=float)
        finite = np.isfinite(samples)
        valid = int(finite.sum())
        mean = float(np.mean(samples[finite])) if valid and not np.isinf(samples).any() else np.nan
        return mean, len(samples), valid

    def _read_window(self, start, end, baselines):
        data = self._window_samples(start, end, baselines)
        return None if data is None else [self._average_samples(values) for _, values in data]

    def _gui(self, action, cancel=True):
        call = _Call(action, self._cancel if cancel else None)
        if QThread.currentThread() == self._bridge.thread():
            self._bridge.execute(call)
        else:
            self._bridge.request.emit(call)
        deadline = time.monotonic() + 5
        while not call.done.wait(.02):
            if (cancel and self._cancel.is_set()) or time.monotonic() > deadline:
                call.expired.set()  # a delayed Qt callback must not write later
                raise ScanStopped('Scan stopped.') if self._cancel.is_set() else ScanError('Qt response timed out.')
        if call.error:
            raise call.error
        return call.result

    def _queue_completion(self, action):
        # Only post-reap GUI completion uses this queue, never hardware commands.
        call = _Call(action, None)
        if QThread.currentThread() == self._bridge.thread():
            self._bridge.execute(call)
        else:
            self._bridge.request.emit(call)

    def _pause(self, seconds=None):
        if self._cancel.wait(self.POLL_S if seconds is None else max(0., seconds)):
            raise ScanStopped('Scan stopped.')

    def _settle(self, seconds):
        deadline, since = time.monotonic() + self._plan['timeout'], None
        while True:
            ready, volts, _, _ = self._gui(self._observation)
            now = time.monotonic()
            since = (since if since is not None else now) if ready else None
            if since is not None and now - since >= seconds:
                return volts
            if now >= deadline:
                raise self._not_ready('PSU voltage settling timed out; no detector value assigned to this point.')
            self._pause()

    def _measure(self):
        start, baselines = self._gui(self._history_start)
        start_mono = time.monotonic()
        end = start + self._plan['average']
        deadline = start_mono + self._plan['average'] + self._plan['timeout']
        while True:
            ready, volts, currents, wall = self._gui(self._observation)
            if not ready:
                raise self._not_ready('PSU readback became invalid or left tolerance during acquisition.')
            if abs((wall - start) - (time.monotonic() - start_mono)) > .5:
                raise ScanError('System clock changed during acquisition.')
            if time.monotonic() >= start_mono + self._plan['average']:
                values = self._gui(lambda: self._read_window(start, end, baselines))
                if values is not None:
                    return start, end, volts, currents, values
            if time.monotonic() >= deadline:
                raise ScanError('Fresh detector data timed out.')
            self._pause()

    def _store_point(self, index, start, end, volts, currents, values, *, continuous=False, offsets=()):
        validation = self._validation
        validation['window_start'][index], validation['window_end'][index] = start, end
        validation['rail_v'][index], validation['rail_i'][index] = volts, currents
        if offsets:
            validation['offset_v'][index] = [monitor for monitor, _ in offsets]
            validation['offset_target'][index] = [target for _, target in offsets]
        for j, (mean, count, finite) in enumerate(values):
            self.outputChannels[j].recordingData[index] = mean
            validation['samples'][index, j], validation['finite_samples'][index, j] = count, finite
        self._store_headroom(index, start, end)
        # Acquired when at least one module read something in the window, like the native engine's DMMR clock.
        validation['point_status'][index] = (('acquired during sweep' if continuous else 'acquired')
            if any(math.isfinite(mean) for mean, _, _ in values) else 'invalid detector data')
        self.signalComm.scanUpdateSignal.emit(False)

    def _report_low_headroom(self):
        flags = np.asarray(self._validation.get('low_headroom', []), dtype=bool)
        if flags.any() and self.inputChannels:
            amplitudes = np.asarray(self.inputChannels[0].recordingData, dtype=float)[flags]
            shown = ', '.join(f'{a:g}' for a in amplitudes[:6]) + (', …' if len(amplitudes) > 6 else '')
            self.print(f'{len(amplitudes)} point(s) measured with less than {self.HEADROOM_LOW_V:g} V of PSU headroom '
                       f'(A = {shown} V): the RF amplitude may not have been regulated there.', flag=PRINT.WARNING)

    def _pass_label(self, index, count):
        passes = int(((self._plan or {}).get('metadata') or {}).get('passes') or 1)
        return f'pass {index // (count // passes) + 1}/{passes}, ' if passes > 1 else ''

    def _run_stepped(self, steps):
        p = self._plan
        for index, amplitude in enumerate(steps):
            if self._cancel.is_set():
                raise ScanStopped('Scan stopped.')
            self._bridge.status.emit(f'{self._pass_label(index, len(steps))}{index + 1}/{len(steps)}: settling at {amplitude:g} V')
            self._gui(lambda a=float(amplitude): self._command([a] * len(p['rails']), offsets=self._offset_target(a)))
            self._settle(p['wait'])
            self._bridge.status.emit(f'{self._pass_label(index, len(steps))}{index + 1}/{len(steps)}: acquiring at {amplitude:g} V')
            self._store_point(index, *self._measure(), offsets=self._offset_snapshot())

    def _continuous_command(self, amplitude, latest=None, *, window_start=None):
        if latest is not None and time.monotonic() >= latest:
            raise ScanError('Continuous time step missed. Increase Time step; no catch-up command sent.')
        # Validate the live DMMR cadence on Qt as well as in preflight.
        # No new voltage command may outrun the DMMR data.
        self._require_time_step(self._plan['detectors'], self._plan['average'], window_start)
        # The deadline is checked on Qt, immediately before any setpoint write;
        # a late queued callback must not issue a catch-up command.
        return self._command([float(amplitude)] * len(self._plan['rails']), latest=latest,
                             offsets=self._offset_target(amplitude))

    def _capture_continuous(self, raw, origin, cutoff, baselines, *, cancel=True):
        start = raw['detector_time'][-1] if raw['detector_time'] else origin
        # A rolling Explorer history may discard samples already copied here.
        # The last copied timestamp must still be retained: otherwise unseen
        # samples may have been lost, and _window_samples must fail closed.
        cursor = [(identity, start) for identity, _ in baselines] if raw['detector_time'] else baselines
        def read():
            samples = self._window_samples(start, cutoff, cursor, closed=False)
            latest = self._plan['detectors'][0].getDevice().time.get(length=1)
            return samples, float(latest[-1]) if len(latest) else -math.inf
        samples, watermark = self._gui(read, cancel=cancel)
        raw['detector_time'].extend(samples[0][0].tolist())
        # One row per DMMR tick, one column per recorded module.
        raw['detector_current'].extend(np.column_stack([values for _, values in samples]).tolist())
        return watermark

    def _continuous_windows(self, pending, raw, watermark):
        while pending and watermark >= pending[0][2]:
            index, start, end, volts, currents, offsets = pending.popleft()
            left, right = (bisect_right(raw['detector_time'], t) for t in (start, end))
            block = np.asarray(raw['detector_current'][left:right], dtype=float).reshape(-1, len(self._plan['detectors']))
            values = [self._average_samples(block[:, j]) for j in range(block.shape[1])]
            self._store_point(index, start, end, volts, currents, values, continuous=True, offsets=offsets)

    def _run_continuous(self, steps):
        p = self._plan
        raw = self._validation['continuous'] = dict(
            command_start=np.full(len(steps), np.nan), command_end=np.full(len(steps), np.nan),
            detector_time=[], detector_current=[], psu_time=[], psu_v=[], psu_i=[], psu_target=[], psu_ready=[],
            **(dict(offset_v=[], offset_target=[]) if p.get('offsets') else {}))
        pending = deque()
        clock = dict(origin=None, origin_mono=None, baselines=None, cutoff=None)
        passes = int((p.get('metadata') or {}).get('passes') or 1)
        per_pass = len(steps) // passes
        try:
            # Each pass is a complete sweep: initial command and settling, timed commands, drained windows.
            for first in range(0, len(steps), per_pass):
                self._continuous_pass(steps, first, first + per_pass, raw, pending, clock)
        finally:
            # Keep available raw samples from an interrupted window, but never
            # present that incomplete window as an acquired spectrum point.
            origin, origin_mono = clock['origin'], clock['origin_mono']
            if origin is not None and abs((time.time() - origin) - (time.monotonic() - origin_mono)) <= .5:
                try:
                    watermark = self._capture_continuous(raw, origin, clock['cutoff'] if clock['cutoff'] is not None
                                                        else time.time(), clock['baselines'], cancel=False)
                    self._continuous_windows(pending, raw, watermark)
                except Exception:
                    pass  # retain the original failure; no fabricated replacement samples

    def _continuous_pass(self, steps, first, last, raw, pending, clock):
        p = self._plan
        label = self._pass_label(first, len(steps))
        self._bridge.status.emit(f'{label}Initial settling at {steps[first]:g} V')
        raw['command_start'][first], raw['command_end'][first] = self._gui(lambda: self._continuous_command(steps[first]))
        self._settle(p['wait'])
        # Define the wall/monotonic origin together, on the same Qt callback
        # that establishes the detector-history identity and baseline.
        (origin, baselines), origin_mono = self._gui(lambda: (self._history_start(), time.monotonic()))
        clock.update(origin=origin, origin_mono=origin_mono, baselines=baselines, cutoff=None)
        index, begin, ready_seen = first, origin, True
        last_command = origin_mono
        duration = p['average']
        next_deadline = origin_mono + duration
        while index < last or pending:
            ready, volts, currents, wall = self._gui(self._observation)
            now = time.monotonic()
            if abs((wall - origin) - (now - origin_mono)) > .5:
                raise ScanError('System clock changed during continuous acquisition.')
            raw['psu_time'].append(wall)
            raw['psu_v'].append(volts.copy())
            raw['psu_i'].append(currents.copy())
            raw['psu_target'].append(list(p['expected']))
            raw['psu_ready'].append(bool(ready))
            offset_now = self._offset_snapshot()
            if offset_now:
                raw['offset_v'].append([monitor for monitor, _ in offset_now])
                raw['offset_target'].append([target for _, target in offset_now])
            if ready_seen and not ready:
                raise self._not_ready('PSU readback became invalid or left tolerance after reaching the continuous target.')
            # A first ready observation arriving after the deadline cannot
            # prove that the PSU kept up, even if it has caught up by now.
            ready_seen |= ready and now <= next_deadline
            if not ready and now - last_command >= p['timeout']:
                raise self._not_ready('PSU voltage settling timed out during continuous acquisition.')
            # A delayed closing timestamp can finalize an earlier window,
            # but a new command requires a valid sample in the current one.
            cutoff = clock['cutoff']
            watermark = self._capture_continuous(raw, origin, cutoff if cutoff is not None else wall, baselines)
            self._continuous_windows(pending, raw, watermark)
            if pending and wall - pending[0][2] > p['timeout']:
                raise ScanError('Fresh detector data timed out during continuous acquisition.')
            deadline = next_deadline
            now = time.monotonic()
            if index < last and now >= deadline:
                if not ready or not ready_seen:
                    raise self._not_ready('PSU target was not confirmed within the continuous time step. '
                                          'Increase Time step; point not acquired and no next command sent.')
                if now >= deadline + duration:
                    raise ScanError('Continuous time step missed. Increase Time step; no catch-up command sent.')
                if index + 1 < last:
                    command = self._gui(lambda: self._continuous_command(
                        steps[index + 1], deadline + duration, window_start=begin))
                    raw['command_start'][index + 1], raw['command_end'][index + 1] = command
                    end = command[0]
                    last_command, ready_seen = time.monotonic(), False
                    # Never shorten the next interval to make up for PC
                    # jitter. Actual command intervals are saved as measured.
                    next_deadline = last_command + duration
                else:
                    self._gui(lambda: self._require_time_step(p['detectors'], duration, begin))
                    end = clock['cutoff'] = time.time()
                if end <= begin:
                    raise ScanError('System clock moved backwards during continuous acquisition.')
                pending.append((index, begin, end, volts.copy(), currents.copy(), offset_now))
                index += 1
                begin = end
                if index < last:
                    self._bridge.status.emit(f'{label}{index + 1}/{len(steps)}: continuous at {steps[index]:g} V')
            if index < last or pending:
                self._pause(min(self.POLL_S, max(0., next_deadline - time.monotonic()))
                    if index < last else self.POLL_S)

    def _run_scan_python_reference(self, recording):
        """Retained reference for cross-language tests, never a production fallback."""
        validation = self._validation
        try:
            p = self._plan
            steps = self.inputChannels[0].recordingData
            if p['mode'] == self.CONTINUOUS:
                self._run_continuous(steps)
            else:
                self._run_stepped(steps)
            self._bridge.status.emit('Returning to initial PSU setpoints' +
                                     (' and offset' if p.get('offsets') else ''))
            self._gui(lambda: self._command([r['initial'] for r in p['rails']],
                                           offsets=[o['initial'] for o in p.get('offsets') or []]))
            self._settle(p['wait'])
            validation['status'] = 'completed'
        except ScanStopped as exc:
            validation['status'], validation['error'] = 'stopped', str(exc)
        except Exception as exc:
            validation['status'], validation['error'] = 'error', str(exc)
            self.print(f'Scan aborted: {exc}', flag=PRINT.ERROR)
        finally:
            for index, status in enumerate(validation['point_status']):
                if status == 'not acquired':
                    validation['point_status'][index] = validation['status']
                    break
            self._report_low_headroom()
            self._bridge.status.emit(validation['status'].capitalize() +
                                     (': ' + validation['error'] if validation['error'] else ''))
            self.signalComm.updateRecordingSignal.emit(False)
            self.signalComm.scanUpdateSignal.emit(True)

    def runScan(self, recording):
        origin_plan, origin_cancel = self._plan, self._cancel
        worker = None
        failure = None
        try:
            worker = _load_native_runtime("_native_worker").NativeWorkerProxy(
                Path(__file__).resolve().parent, "mscan", {},
                log_dir=Path(self.pluginManager.Settings.dataPath) / "logs" / "mscan")
            def claim():
                if self._plan is not origin_plan or self._cancel is not origin_cancel:
                    raise ScanStopped('MScan session was replaced during native worker startup.')
                if getattr(self, '_native_worker', None) is not None:
                    raise ScanStopped('Native MScan worker was replaced during startup.')
                self._native_worker = worker
                if origin_cancel.is_set():
                    raise ScanStopped('Scan stopped before native start.')
                return _load_native_runtime("_native_scan").NativeScanAdapter(
                    self, worker.call_method, emit_completion=False)
            adapter = self._gui(claim, cancel=False)
            adapter.run()
        except ScanStopped as exc:
            failure = ('stopped', str(exc))
        except Exception as exc:
            failure = ('error', str(exc))
        finally:
            if worker is not None:
                worker.close(grace_s=0)
            def finish():
                owns_worker = getattr(self, '_native_worker', None) is worker
                if owns_worker:
                    self._native_worker = None
                if not owns_worker or self._plan is not origin_plan or self._cancel is not origin_cancel:
                    return
                validation = self._validation
                if failure is not None:
                    validation['status'], validation['error'] = failure
                if validation['status'] == 'error':
                    self.print(f'Scan aborted: {validation["error"]}', flag=PRINT.ERROR)
                if np.any(validation.get('low_headroom', ())):
                    self._report_low_headroom()
                # The engine already marks the point it stopped on: mark one only when it could not
                # (2026-11-02 lab files: two points read "error" after one failed).
                if validation['status'] not in validation['point_status']:
                    for index, status in enumerate(validation['point_status']):
                        if status == 'not acquired':
                            validation['point_status'][index] = validation['status']
                            break
                self._bridge.status.emit(validation['status'].capitalize() +
                                         (': ' + validation['error'] if validation['error'] else ''))
                # Stop/error never restores, re-enables or queues another voltage.
                self.signalComm.updateRecordingSignal.emit(False)
                self.signalComm.scanUpdateSignal.emit(True)
            self._queue_completion(finish)

    @property
    def recording(self):
        return Scan.recording.fget(self)

    @recording.setter
    def recording(self, value):
        # DeviceManager also stops scans through this property (not the button).
        if not value:
            self._cancel.set()
            worker = getattr(self, "_native_worker", None)
            if worker is not None:
                worker.cancel()
        Scan.recording.fset(self, value)

    @property
    def finished(self):
        return self._finished

    @finished.setter
    def finished(self, value):
        if value and not self.finished:
            self._last_scan_status = self.scan_status
        Scan.finished.fset(self, value)
        for key, setting in self.settingsMgr.settings.items():
            if not setting.indicator:
                setting.setEnabled(value)
        self._update_scan_action()

    def toggleRecording(self):
        if not self.recording:
            self._cancel.set()
        elif self.finished and self.settingsTree:
            for spin in self.settingsTree.findChildren(QAbstractSpinBox):
                spin.interpretText()
            # Toolbar/shortcut activation need not leave the spinbox. Commit
            # the DMMR proxy before preflight, never after a voltage command.
            proxy = self.settingsMgr.settings[self.DMMR_INTERVAL].spin
            if proxy.isEnabled() and not self._dmmr_interval_changed(before_start=True):
                self.recording = False
                return
        super().toggleRecording()

    def close(self):
        self._cancel.set()
        worker = getattr(self, "_native_worker", None)
        if worker is not None:
            worker.close(grace_s=0)
        return super().close()

    def closeGUI(self):
        if hasattr(self, '_interface_timer'):
            self._interface_timer.stop()
        if hasattr(self, '_layout_timer'):
            self._layout_timer.stop()
        if self.finished and self.titleBar is not None:
            self.titleBar.clear()  # actions and toolbar can have different owners
        super().closeGUI()

    @plotting
    def plot(self, update=False, done=False, **kwargs):
        try:
            self._plot(update=update)
        except Exception as exc:  # noqa: BLE001
            # A display fault must never stop the scan or the save that follows its last update.
            if not self._plot_fault_reported:
                self._plot_fault_reported = True
                self.print(f'Plot not updated: {exc}', flag=PRINT.WARNING)

    def _plot(self, update=False):
        display = self.display
        if display is None or not getattr(display, 'axes', None) or getattr(display, 'ms', None) is None:
            if self.inputChannels and not self.finished:
                raise RuntimeError('the MScan plot is not open')  # Reported once per scan by plot().
            return
        ax = display.axes[0]
        outputs = self.outputChannels if self.inputChannels else []
        x = np.asarray(self.inputChannels[0].getRecordingData() if outputs else [], dtype=float)
        x, x_label = self._plot_axis(x)
        passes = int(((self._plan or {}).get('metadata') or {}).get('passes') or 1)
        if passes < 1 or len(x) % passes:
            passes = 1  # Older files: one sweep.
        n = len(x) // passes
        suffix = f' (mean of {passes})' if passes > 1 else ''
        names = [f'{c.name}{suffix}' for c in outputs]
        if [line.get_label() for line in display.extra_lines] != names[1:]:
            for line in display.extra_lines:
                line.remove()
            display.extra_lines = [ax.plot([], [], marker='.', markersize=4, label=name)[0] for name in names[1:]]
        display.ms.set_label(names[0] if names else '_primary')
        means = [display.ms, *display.extra_lines]
        if len(display.pass_lines) != (passes * len(outputs) if passes > 1 else 0):
            for line in display.pass_lines:
                line.remove()
            display.pass_lines = [ax.plot([], [], linewidth=.8, alpha=.35, color=means[j].get_color(),
                                          label='_pass')[0]
                                  for j in range(len(outputs)) for _ in range(passes)] if passes > 1 else []
        for j, (line, channel) in enumerate(zip(means, outputs)):
            data = np.asarray(channel.getRecordingData(), dtype=float).reshape(passes, n) * self.PICOAMPERE
            with np.errstate(all='ignore'), warnings.catch_warnings():
                warnings.simplefilter('ignore', RuntimeWarning)  # A point not yet measured in any pass.
                line.set_data(x[:n], np.nanmean(data, axis=0) if passes > 1 else data[0])
            for k, pass_line in enumerate(display.pass_lines[j * passes:(j + 1) * passes]):
                pass_line.set_data(x[:n], data[k])
        if not outputs:
            display.ms.set_data([], [])
        legend = ax.get_legend()
        if len(outputs) > 1 or passes > 1:
            # Below the file label, which uses the top margin reserved by setLabelMargin.
            ax.legend(loc='upper left', bbox_to_anchor=(0, .87), fontsize='small')
        elif legend is not None:
            legend.remove()
        ax.set_ylabel('Current (pA)')
        ax.set_xlabel(x_label)
        autoscale = getattr(display, 'autoscaleAction', None)
        if autoscale is None or autoscale.state:
            self._rescaling = True
            try:
                ax.relim()
                self.setLabelMargin(ax, .15)
                ax.get_xlim()  # Resolve any deferred autoscale now, not as a later "user" change.
            finally:
                self._rescaling = False
        self.updateToolBar(update=update)
        self.defaultLabelPlot()
        canvas = getattr(getattr(display, 'fig', None), 'canvas', None)
        if hasattr(canvas, 'draw_idle'):
            canvas.draw_idle()  # Redraw even when the file label could not be placed.

    def defaultLabelPlot(self):
        if len(self.outputChannels) > 1:
            self.labelPlot(self.file.name if self.file else '')  # The legend names each module.
        else:
            super().defaultLabelPlot()

    def labelPlot(self, label):
        # Label only the live display: a closed figure has no renderer left (2026-10-26:
        # 'FigureCanvasBase' object has no attribute 'get_renderer'), and an empty axis list
        # raised IndexError at every update.
        display = self.display if self.displayActive() else None
        axes = getattr(display, 'axes', None)
        canvas = getattr(getattr(display, 'fig', None), 'canvas', None)
        if not axes or not hasattr(canvas, 'get_renderer'):
            return
        self.labelAxis = axes[0]
        self._rescaling = True
        try:
            super().labelPlot(label)
        finally:
            self._rescaling = False

    def _psu_names(self):
        rails = ((self._plan or {}).get('metadata') or {}).get('rails') or []
        names = [str(r.get('psu', '')) for r in rails]
        if not any(names):
            try:
                amx = self._plugin(self.amx_name)
                selected = self._selected_outputs()
                names = [str(getattr(amx, attr)) for attr, first in (('psu_ch01', 0), ('psu_ch23', 2)) if first in selected]
            except (ScanError, AttributeError, KeyError, TypeError, ValueError):
                names = []
        return [name for name in dict.fromkeys(names) if name and name != 'None']

    def _psu_tag(self, separator=', '):
        letters = [name[4:] if name.upper().startswith('PSU_') else name for name in self._psu_names()]
        return ('PSU ' + separator.join(letters)) if letters else ''

    def _axis_label(self):
        metadata = (self._plan or {}).get('metadata', {})
        axis = self._psu_tag() or 'Amplitude A'
        return f'{axis}, commanded (V)' if metadata.get('mode') == self.CONTINUOUS else f'{axis} (V)'

    def saveData(self, file):
        super().saveData(file)
        if not self._validation:
            return
        with h5py.File(file, 'a') as handle:
            if self.notes:
                handle[self.name].attrs['notes'] = self.notes
            group = handle[self.name].require_group(self.VALIDATION)
            group.attrs['status'] = self._validation['status']
            group.attrs['error'] = self._validation['error']
            group.attrs['setup'] = json.dumps(self._plan['metadata'], allow_nan=False)
            group.attrs['modules'] = json.dumps([c.name for c in self.outputChannels])
            group.attrs['channels'] = ('Every enabled DMMR module is recorded, in the order of modules; no module is a '
                'reference. Each is averaged over the same windows (start, end] of the DMMR time axis. Samples and '
                'finite_samples have one column per module. NaN samples are recorder ticks without a new reading and '
                'are skipped; an infinite sample invalidates that module\'s point. A point is acquired when at least '
                'one module has a reading in its window.')
            for key in ('rail_v', 'rail_i', 'window_start', 'window_end', 'samples', 'finite_samples'):
                group.create_dataset(key, data=self._validation[key])
            group.create_dataset('point_status', data=self._validation['point_status'], dtype=h5py.string_dtype('utf-8'))
            group['rail_v'].attrs['Unit'] = 'V (unsigned PSU magnitudes)'
            group['rail_i'].attrs['Unit'] = 'A (PSU readback at end of integration, not detector current)'
            try:
                inputs = self._mz_inputs(self._scan_metadata())
                group.create_dataset('mz', data=mz_from_amplitude(
                    np.asarray(self.inputChannels[0].recordingData, dtype=float), **inputs))
                group['mz'].attrs['Unit'] = ('Th: m/z at the centre of the transmitted band (low-mass cut-off when '
                                             'RF only); NaN where not transmitted or unknown')
                group['mz'].attrs['Definition'] = json.dumps(dict(
                    formula='m/z = 4 e A / (q r0^2 (2 pi f)^2) / u; q = centre of the stable band on a = 2 (U/A) q, '
                            'U = max(minimum, factor A - resolution); ideal 50 % square wave, rod amplitude = A',
                    calibration='theory with this r0; r0 absorbs the rod amplitude ratio once fitted on a known peak',
                    **inputs))
            except (ScanError, AttributeError, KeyError, TypeError, ValueError):
                pass  # No AMX frequency in this scan's metadata: A only.
            if 'rail_dropout' in self._validation:
                group.create_dataset('rail_dropout', data=self._validation['rail_dropout'])
                group['rail_dropout'].attrs['Unit'] = ('V (lowest PSU regulator headroom, dropout, over the point window, '
                                                       'one column per rail; NaN when not reported)')
                group.create_dataset('low_headroom', data=np.asarray(self._validation['low_headroom'], dtype=bool))
                group['low_headroom'].attrs['Description'] = (f'True where a rail had less than {self.HEADROOM_LOW_V:g} V '
                                                              'of headroom: the RF amplitude may not have been regulated.')
            if 'offset_v' in self._validation:
                for key in ('offset_v', 'offset_target'):
                    group.create_dataset(key, data=self._validation[key])
                group['offset_v'].attrs['Unit'] = 'V (AMPR Monitor of each rod pair channel, one column per pair)'
                group['offset_target'].attrs['Unit'] = ('V (DC offset commanded on each AMPR channel, one column per pair: '
                                                        'pair 1 fixed + max(U_min, U/V × A − δ), pair 2 fixed − the same term)')
            for key in ('window_start', 'window_end'):
                group[key].attrs['Unit'] = 's since Unix epoch'
            if 'continuous' in self._validation:
                raw = group.create_group('Continuous')
                for key, values in self._validation['continuous'].items():
                    data = np.asarray(values, dtype=bool if key == 'psu_ready' else float)
                    if key in ('psu_v', 'psu_i', 'psu_target'):
                        data = data.reshape(-1, self._validation['rail_v'].shape[1])
                    if key == 'detector_current':
                        data = data.reshape(-1, max(1, len(self.outputChannels)))
                    if key in ('offset_v', 'offset_target') and np.ndim(self._validation.get('offset_v')) == 2:
                        data = data.reshape(-1, self._validation['offset_v'].shape[1])
                    raw.create_dataset(key, data=data)
                raw.attrs['timestamps'] = 'PC/Explorer times; not hardware-synchronized ADC timestamps.'
                raw.attrs['commands'] = 'GUI dispatch of sequential rail requests; not hardware write/settling times.'
                raw.attrs['readbacks'] = 'Snapshots of shared PSU channels, NaN while unavailable. No interpolation.'
                raw['detector_current'].attrs['Unit'] = ('A (raw DMMR samples at detector_time, one column per module '
                                                         'as in Validation modules; no background subtraction)')
                raw['psu_v'].attrs['Unit'] = 'V (unsigned PSU magnitudes)'
                raw['psu_i'].attrs['Unit'] = 'A (PSU current, not detector current)'
                raw['psu_target'].attrs['Unit'] = 'V (requested PSU magnitudes)'
                if 'offset_v' in raw:
                    raw['offset_v'].attrs['Unit'] = 'V (AMPR Monitor of each rod pair channel, one column per pair)'
                    raw['offset_target'].attrs['Unit'] = 'V (offset commanded on each AMPR channel, one column per pair)'
                for key in ('command_start', 'command_end', 'detector_time', 'psu_time'):
                    raw[key].attrs['Unit'] = 's since Unix epoch'
                group['rail_v'].attrs['Description'] = 'PSU observation at end of command interval; not synchronized to detector samples.'
                group['rail_i'].attrs['Unit'] = 'A (PSU observation at end of command interval, not detector current)'

    def saveScanParallel(self, file):
        try:
            super().saveScanParallel(file)
        except Exception as exc:
            self.print(f'Scan data could not be saved: {exc}', flag=PRINT.ERROR)
            self._bridge.status.emit(f'Save failed: {exc}. Data remains in memory.')
            self.signalComm.saveScanCompleteSignal.emit()

    def loadDataInternal(self):
        self._plan, self._validation = None, {}
        loaded = super().loadDataInternal()
        if loaded:
            with h5py.File(self.file, 'r') as handle:
                saved = handle[self.name]
                old_notes = saved.get('Settings/Notes')
                self.notes = str(saved.attrs.get('notes', old_notes.attrs.get('Value', '') if old_notes is not None else ''))
                group = saved.get(self.VALIDATION)
                if group is not None:
                    self._plan = {'metadata': json.loads(group.attrs['setup'])}
                    self._validation = {key: group[key][:] for key in
                        ('rail_v', 'window_start', 'window_end', 'samples', 'finite_samples')}
                    # Files predating current monitoring remain readable; no
                    # current measurement is invented for their old points.
                    self._validation['rail_i'] = (group['rail_i'][:] if 'rail_i' in group
                        else np.full_like(self._validation['rail_v'], np.nan))
                    for key in ('offset_v', 'offset_target', 'rail_dropout', 'low_headroom'):
                        if key in group:
                            self._validation[key] = group[key][:]
                    if 'Continuous' in group:
                        self._validation['continuous'] = {key: data[:] for key, data in group['Continuous'].items()}
                    self._validation.update(status=group.attrs['status'], error=group.attrs['error'],
                        point_status=list(group['point_status'].asstr()[:]))
        return loaded

    def pythonPlotCode(self):
        passes = int(((self._plan or {}).get('metadata') or {}).get('passes') or 1)
        return '''fig, ax = plt.subplots(constrained_layout=True)
passes = PASSES  # the same sweep repeated in this file
x = np.asarray(inputChannels[0].recordingData)
n = len(x) // passes
for output in outputChannels:  # Every recorded DMMR module, in A in the file.
    y = np.asarray(output.recordingData).reshape(passes, n) * 1e12
    line, = ax.plot(x[:n], np.nanmean(y, axis=0), marker='.', markersize=4, label=output.name)
    for single in y if passes > 1 else ():
        ax.plot(x[:n], single, linewidth=.8, alpha=.35, color=line.get_color())
ax.set_xlabel(AXIS_LABEL)
ax.set_ylabel('Current (pA)')
if len(outputChannels) > 1 or passes > 1:
    ax.legend()
plt.show()
'''.replace('AXIS_LABEL', repr(self._axis_label())).replace('PASSES', str(passes))
