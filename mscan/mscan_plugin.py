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
import json
import math
from pathlib import Path
from threading import Event
import time
from typing import Any, Callable

import h5py
import numpy as np
from PyQt6.QtCore import QEvent, QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QFileDialog, QHeaderView, QSizePolicy

from esibd.core import INFO, INOUT, PARAMETERTYPE, PRINT, MetaChannel, Parameter, TreeWidget, infoDict, parameterDict, plotting
from esibd.plugins import Plugin, Scan, SettingsManager


def providePlugins():
    return [MScan]


class ScanError(RuntimeError):
    """Cannot acquire a scientifically valid point under the selected conditions."""


class ScanStopped(ScanError):
    pass


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
        aliases = {scan.DETECTOR: 'Display', scan.OUTPUTS: 'AMX outputs',
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
            if key in (scan.DETECTOR, scan.AMX, scan.OFFSET):
                # Keep missing selections explicit, without phantom old choices.
                if key == scan.DETECTOR:
                    choices = [scan.NO_SIGNAL, *[scan._module_label(c) for c in scan.pluginManager.DeviceManager.channels() if scan._is_dmmr(c)]]
                elif key == scan.OFFSET:
                    choices = [scan.NO_OFFSET, *[scan._offset_label(c) for c in scan.pluginManager.DeviceManager.channels() if scan._is_ampr(c)]]
                else:
                    choices = ['None', *[p.name for p in scan.pluginManager.plugins if p.name in ('AMX_A', 'AMX_B')]]
                choices.append(str(item[Parameter.VALUE]))
                item[Parameter.ITEMS] = ','.join(dict.fromkeys(choices))
        scan.notes = str(old_value('Notes', Parameter.VALUE) or '')
        super().updateSettings(items, file)

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
    OUTPUTS = 'AMX connectors'
    START = 'Amplitude from (V)'
    STOP = 'Amplitude to (V)'
    STEP = 'Amplitude step (V)'
    MODE = 'Scan mode'
    STEPPED = 'Step by step'
    CONTINUOUS = 'Continuous'
    TIME_STEP = 'Time step (s)'
    RATE = 'Sweep rate'
    DETECTOR = 'DMMR module'
    DMMR_INTERVAL = 'DMMR interval (ms)'
    SETTLING = 'Settling time (s)'
    INTEGRATION = 'Measurement time (s)'
    CADENCE = 'DMMR data interval'
    SAMPLES = 'Samples at last point'
    SCANTIME = 'Minimum duration'
    SUPPLIES = 'Driven PSU'
    FREQUENCY = 'Fixed frequency'
    AMPLITUDE_LIMITS = 'Allowed amplitude (V)'
    FINAL_VOLTAGES = 'After completion'
    CURRENT_LIMITS = 'PSU Ilim'
    OFFSET = 'Quadrupole offset'
    OFFSET_FACTOR = 'Offset coefficient'
    OFFSET_READBACK = 'Offset readback'
    NO_OFFSET = 'None'
    NO_SIGNAL = 'Select DMMR module'
    NO_PAIR = 'Select AMX pair'
    TIMEOUT = 'Settle timeout'
    TOLERANCE = 'Voltage tolerance'
    STATUS = 'Status'
    VALIDATION = 'Validation'
    PAIRS = {'CH0-CH1': (0, 1), 'CH2-CH3': (2, 3)}
    POLL_S = .05

    class Display(Scan.Display):
        def closeGUI(self):
            if self.scan.finished and self.titleBar is not None:
                # Detach every action while both owners are alive: navigation
                # belongs to navToolBar, Data to Clipboard belongs to Display.
                # Leaving either attached can crash Qt during their destruction.
                self.titleBar.clear()
            super().closeGUI()

        def initGUI(self):
            super().initGUI()
            self.titleBarLabel.setText(self.scan.TITLE)
            self.addAction(event=lambda: self.copyLineDataClipboard(line=self.ms),
                           toolTip='Data to Clipboard.', icon=self.dataClipboardIcon, before=self.copyAction)

        def provideDock(self):
            created = super().provideDock()
            if self.dock:
                self.dock.title = self.scan.TITLE
                self.dock.setWindowTitle(self.scan.TITLE)
                self.titleBarLabel.setText(self.scan.TITLE)
            return created

        def initFig(self):
            super().initFig()
            if self.fig and self.canvas:
                self.axes.append(self.fig.add_subplot(111))
                self.ms = self.axes[0].plot([], [], marker='.', markersize=4)[0]
                self.scan.labelAxis = self.axes[0]
                # No MZCalculator: these abscissae are volts, not calibrated m/z.

    def __init__(self, **kwargs):
        self._cancel = Event()
        self._plan = None
        self._validation = {}
        self._last_scan_status = ''
        self._ready = False
        self.displayDefault = ''  # Plot selection only; never an acquisition setting.
        self.notes = ''
        super().__init__(**kwargs)
        self.useDisplayChannel = True
        self._bridge = _GuiBridge(self)

    def getDefaultSettings(self):
        settings = {}
        settings[self.DETECTOR] = parameterDict(value=self.NO_SIGNAL, items=self.NO_SIGNAL,
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='detector_module', event=self._signal_changed,
            toolTip='Physical DMMR module wired to the ion collector after the quadrupole. '
                    'Only this current channel is acquired. DMMR acquisition and recording must already be active.')
        settings[self.AMX] = parameterDict(value='None', items='None, AMX_A, AMX_B',
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
                'Continuous: one time step per amplitude, including the last, plus initial/final settling. '
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
            toolTip='AMPR channel (module, channel) applying the quadrupole offset. During the scan it is set to '
                    'Offset coefficient × A at every amplitude, then verified: the AMPR hardware setpoint must confirm '
                    'the scan target and its Monitor must be within Voltage tolerance before acquisition. Normal '
                    'completion restores its initial value; Stop/error holds the last value. None: not driven.')
        settings[self.OFFSET_FACTOR] = parameterDict(value=.2, minimum=-100., maximum=100.,
            parameterType=PARAMETERTYPE.FLOAT, attr='offset_factor', event=self._offset_changed,
            instantUpdate=False, displayDecimals=4,
            toolTip='Offset = coefficient × A (V per V of amplitude A), applied to the selected AMPR channel. '
                    'The commanded value is rounded to the AMPR channel display precision.')
        settings[self.OFFSET_READBACK] = parameterDict(value='Not driven', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Offset channel readback: AMPR Monitor (measured) and its setpoint. During a scan: scan target, '
                    'Monitor and whether the AMPR confirmed the target. Display only, never a command.')
        order = (self.AMX, self.OUTPUTS, self.SUPPLIES, self.FREQUENCY, self.DETECTOR, self.DMMR_INTERVAL,
                 self.AMPLITUDE_LIMITS, self.CURRENT_LIMITS, self.MODE, self.START, self.STOP, self.STEP, self.RATE,
                 self.OFFSET, self.OFFSET_FACTOR, self.OFFSET_READBACK, self.FINAL_VOLTAGES,
                 self.SETTLING, self.INTEGRATION, self.TIME_STEP, self.CADENCE, self.SAMPLES,
                 self.SCANTIME, self.STATUS, self.TIMEOUT, self.TOLERANCE)
        return {key: settings[key] for key in order}

    def initGUI(self):
        # Use Explorer's common plugin/services, not the historical scan GUI.
        self.loading = True
        Plugin.initGUI(self)
        self.titleBarLabel.setText(self.TITLE)
        self.settingsTree = TreeWidget()
        self.settingsTree.setMinimumWidth(200)
        self.settingsTree.setHeaderLabels(['msScan', 'Value'])
        self.settingsTree.setRootIsDecorated(False)
        self.settingsTree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.settingsTree.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.settingsTree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
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
            toolTipFalse='Start the AMX/PSU scan with the selected DMMR module.', iconFalse=self.makeCoreIcon('play.png'),
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
            action.setToolTip(action.toolTipTrue if self.recording else action.toolTipFalse)
            # Stop remains available throughout the run, even if a source fails.
            # A new scan must wait for the previous worker and file save to finish.
            action.setEnabled(self.recording or (self.finished and self._ready))

    def _format_settings(self):
        # Preserve old step/time ratios, including when Qt interprets the text
        # on Start or focus loss. Trim zeros, not significant digits: rounded
        # text can silently drop an aligned endpoint from the generated scan.
        spin = self.settingsMgr.settings[self.RATE].spin
        spin.textFromValue = lambda value: f'{value:.15f}'.rstrip('0').rstrip('.')
        spin.setValue(spin.value())
        labels = {self.INTEGRATION: 'Measurement per point (s)', self.RATE: 'Sweep rate (V/s)',
                  self.TIMEOUT: 'Settle timeout (s)', self.TOLERANCE: 'Voltage tolerance (V)',
                  self.OFFSET: 'Quadrupole offset (AMPR)', self.OFFSET_FACTOR: 'Offset coefficient (V/V)'}
        for key, label in labels.items():
            self.settingsMgr.settings[key].setText(0, label)  # labels, not INI/HDF5 keys
        self.settingsTree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for spin in self.settingsTree.findChildren(QAbstractSpinBox):
            spin.setKeyboardTracking(False)
        for key in (self.DETECTOR, self.AMX, self.OUTPUTS, self.MODE, self.OFFSET):
            combo = self.settingsMgr.settings[key].combo
            combo.setMaximumWidth(16777215)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        for key in (self.SUPPLIES, self.FREQUENCY, self.STATUS, self.CADENCE, self.SAMPLES,
                    self.SCANTIME, self.AMPLITUDE_LIMITS, self.FINAL_VOLTAGES, self.CURRENT_LIMITS, self.OFFSET_READBACK):
            label = self.settingsMgr.settings[key].label
            label.setMaximumHeight(16777215)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            label.setProperty('mscanWrappedLabel', True)
            label.installEventFilter(self)
        self._layout_readonly_fields()

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
        for key in (self.SUPPLIES, self.FREQUENCY, self.STATUS, self.CADENCE, self.SAMPLES,
                    self.SCANTIME, self.AMPLITUDE_LIMITS, self.FINAL_VOLTAGES, self.CURRENT_LIMITS, self.OFFSET_READBACK):
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

    @staticmethod
    def _module_label(channel):
        return f'Module {channel.module_address()} — {channel.name}'

    def _detector(self):
        selection = str(self.detector_module).strip()
        if not selection or selection == self.NO_SIGNAL:
            raise ScanError('Select a DMMR module connected to the ion collector.')
        matches = [c for c in self.pluginManager.DeviceManager.channels() if self._is_dmmr(c)
                   and selection in (self._module_label(c), c.name)]
        if len(matches) != 1:
            raise ScanError(f'DMMR module {selection} is unavailable or ambiguous. Check DMMR discovery and selection.')
        return matches[0]

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

    def _offset_source(self):
        """The AMPR channel applying the quadrupole offset, or None when not driven."""
        selection = str(getattr(self, 'offset_channel', self.NO_OFFSET)).strip()
        if not selection or selection == self.NO_OFFSET:
            return None
        matches = [c for c in self.pluginManager.DeviceManager.channels() if self._is_ampr(c)
                   and selection in (self._offset_label(c), c.name)]
        if len(matches) != 1:
            raise ScanError(f'Quadrupole offset channel {selection} is unavailable or ambiguous. Check the AMPR channels.')
        return matches[0]

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
        """Read-only validation of the offset channel; None when the offset is not driven."""
        c = self._offset_source()
        if c is None:
            return None
        self._registered(c)
        device = c.getDevice()
        ctrl = device.controller
        try:
            factor = float(self.offset_factor)
        except (AttributeError, TypeError, ValueError):
            factor = math.nan
        if not math.isfinite(factor):
            raise ScanError('Offset coefficient must be finite.')
        if not all(hasattr(ctrl, field) for field in ('_latest_setpoints', '_setpoint_lock', '_setpoint_cancel')):
            raise ScanError(f'{c.name}: update MScan and AMPR plugins together; setpoint confirmation required.')
        if not c.enabled:
            raise ScanError(f'{c.name}: switch the AMPR offset channel ON.')
        if not c.active:
            raise ScanError(f'{c.name}: use manual (not equation) control for the offset.')
        if not device.isOn() or not ctrl.initialized or ctrl.device is None:
            raise ScanError(f'{c.name}: turn the AMPR ON.')
        if any(bool(getattr(ctrl, flag, False)) for flag in ('initializing', 'transitioning', 'ramping')):
            raise ScanError(f'{c.name}: wait for the AMPR transition to finish.')
        if ctrl._setpoint_cancel.is_set():
            raise ScanError(f'{c.name}: AMPR output shutdown requested.')
        minimum, maximum = self._offset_bounds(c, ctrl)
        first, last = sorted((factor * low, factor * high))
        if first < minimum or last > maximum:
            raise ScanError(f'{c.name}: offset {first:g}–{last:g} V (coefficient {factor:g} × A) exceeds '
                            f'allowed {minimum:g}–{maximum:g} V.')
        initial = self._number(c, 'value', 'offset setpoint', nonnegative=False)
        if not minimum <= initial <= maximum:
            raise ScanError(f'{c.name}: final offset restoration to {initial:g} V is outside allowed {minimum:g}–{maximum:g} V.')
        o = dict(channel=c, name=c.name, device=device, controller=ctrl, backend=ctrl.device,
                 token=ctrl._setpoint_cancel, key=(c.module_address(), c.channel_number()), factor=factor,
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
        """GUI-only: offset readiness. Aborts on any change made outside the scan."""
        o = self._plan.get('offset')
        if not o:
            return True
        c, device, ctrl = o['channel'], o['device'], o['controller']
        self._registered(c)
        if (c.name != o['name'] or self._plugin(device.name) is not device or device.controller is not ctrl
                or ctrl.device is not o['backend'] or ctrl._setpoint_cancel is not o['token'] or o['token'].is_set()
                or not device.isOn() or not ctrl.initialized or not c.enabled or not c.active or not c.real):
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
        o = self._plan.get('offset')
        return None if not o else o['factor'] * float(amplitude)

    def _offset_snapshot(self):
        o = self._plan.get('offset')
        return None if not o else (o['observed'], o['expected'])

    def _not_ready(self, message):
        """Name the offset when it, rather than the PSU, is what is not confirmed."""
        o = (self._plan or {}).get('offset')
        if o and not o['ready']:
            return ScanError(f'Quadrupole offset {o["name"]}: target {o["expected"]:g} V not confirmed by the AMPR '
                             f'or Monitor {o["observed"]:g} V outside tolerance. {message}')
        return ScanError(message)

    def _offset_readback_text(self, *, running=False):
        o = (self._plan or {}).get('offset') if running else None
        if running:
            if not o:
                return 'Not driven'
            state = 'confirmed' if o['ready'] else 'waiting for confirmation'
            return f'{o["name"]}: target {o["expected"]:+g} V, Monitor {o["observed"]:+.3f} V ({state})'
        c = self._offset_source()
        if c is None:
            return 'Not driven'
        factor = float(self.offset_factor)
        lines = [f'{c.name}: Monitor {float(c.monitor):+.3f} V, set {float(c.value):+g} V']
        if all(math.isfinite(float(v)) for v in (self.start, self.stop)):
            lines.append(f'Scan: {factor:g} × A = {factor * float(self.start):+g} … {factor * float(self.stop):+g} V')
        return '\n'.join(lines)

    def estimateScanTime(self):
        if not all(hasattr(self, attr) for attr in ('start', 'stop', 'step', 'settling_s', 'integration_s')):
            return
        try:
            steps = self._scan_steps()
            mode, duration = self._timing()
            if mode == self.CONTINUOUS:
                self.scantime = f'{len(steps) * duration + 2 * self.settling_s:g} s + control/data delays'
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
        device = self._detector().getDevice()
        settings = getattr(self.pluginManager.Settings, 'settings', {})
        setting = settings.get(f'{device.name}/{device.INTERVAL}')
        if setting is None or setting.spin is None:
            raise ScanError('DMMR Interval setting unavailable. Select an initialized DMMR module.')
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

    def _detector_cadence(self, c):
        """Bound the scan by this module's usable history, not recorder ticks."""
        device = c.getDevice()
        if not c.acquiring or not device.recording:
            raise ScanError(f'{c.name}: DMMR acquisition/recording stopped.')
        configured, since = self._cadence_epoch(device)
        times = np.asarray(device.time.get())
        values = np.asarray(c.getValues(subtractBackground=False))
        if len(times) != len(values):
            raise ScanError(f'{c.name}: DMMR timestamps and values are misaligned.')
        self._require_monotonic(device, times, f'{c.name}: invalid DMMR timestamps.')
        valid_times = self._last_finite_times(times, values, since, 21)
        if len(valid_times) < 2:
            reason = 'after DMMR interval change' if math.isfinite(since) else 'to determine Time step'
            raise ScanError(f'{c.name}: waiting for two valid recorded DMMR samples {reason}.')
        intervals = np.diff(valid_times)
        largest = float(intervals.max())
        if not 0 <= time.time() - valid_times[-1] <= max(2., 3 * configured, 3 * largest):
            raise ScanError(f'{c.name}: waiting for fresh valid DMMR samples.')
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

    def _require_time_step(self, c, duration, window_start=None):
        cadence = self._detector_cadence(c)
        minimum = cadence['minimum_time_step_s']
        if duration < minimum:
            raise ScanError(f'{c.name}: Time step must be at least {minimum:g} s for the selected DMMR module '
                            f'(requested {duration:g} s). Increase Time step or change DMMR acquisition settings.')
        if window_start is not None and cadence['last_sample_time'] <= window_start:
            raise ScanError(f'{c.name}: no new valid DMMR sample within Time step. '
                            'Scan stopped without advancing; increase Time step or check DMMR acquisition.')
        return cadence

    def _acquisition_info(self):
        try:
            cadence = self._detector_cadence(self._detector())
            text = (f'≈ {cadence["typical_interval_s"]:.3g} s between valid samples; '
                    f'Time step ≥ {cadence["minimum_time_step_s"]:g} s')
        except (ScanError, AttributeError, ValueError, TypeError) as exc:
            text = str(exc)
        self.settingsMgr.settings[self.CADENCE].value = text
        counts, finite = self._validation.get('samples'), self._validation.get('finite_samples')
        windows = self._validation.get('window_end')
        acquired = np.flatnonzero(np.isfinite(windows)) if windows is not None else []
        self.settingsMgr.settings[self.SAMPLES].value = (f'{int(finite[acquired[-1], 0])} valid / {int(counts[acquired[-1], 0])} total'
            if len(acquired) and counts is not None else 'Not acquired')

    def scanUpdate(self, done=False):
        self._acquisition_info()
        super().scanUpdate(done=done)

    def _signal_changed(self):
        if not self.loading and not self.settingsMgr.loading:
            self._refresh_interface()
            self.dummyInitialization()

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
                o = (self._plan or {}).get('offset') if running else None
                c = None if running else self._offset_source()
                if o or c is not None:
                    name, value = (o['name'], o['initial']) if o else (c.name, float(c.value))
                    lines.append(f'{name}: {value:+g} V (quadrupole offset)')
            except ScanError:
                lines.append('Quadrupole offset: unavailable')
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
            self._refresh_completion(self._plan['rails'] if self._plan else [], running=True)
            self._refresh_offset_readback(running=True)
            self._layout_readonly_fields()
            return
        channels = list(self.pluginManager.DeviceManager.channels())
        modules = sorted([c for c in channels if self._is_dmmr(c)], key=lambda c: (c.module_address(), c.name))
        setting = self.settingsMgr.settings[self.DETECTOR]
        combo, selected = setting.combo, str(setting.value).strip()
        if not combo.view().isVisible():
            legacy = [c for c in modules if c.name == selected]
            if len(legacy) == 1:
                selected = self._module_label(legacy[0])
            choices = list(dict.fromkeys([self.NO_SIGNAL, *[self._module_label(c) for c in modules]]))
            if selected and selected not in choices:
                choices.append(selected)  # never silently replace a lost selection
            previous = combo.blockSignals(True)
            if setting.items != choices:
                combo.clear()
                combo.addItems(choices)
            combo.setCurrentText(selected or self.NO_SIGNAL)
            for index, name in enumerate(choices):
                matches = [c for c in modules if self._module_label(c) == name]
                if len(matches) == 1:
                    c = matches[0]
                    text = f'{c.name}: ion current (A), DMMR module {c.module_address()}. '
                    text += 'Acquiring and recording.' if c.acquiring and c.getDevice().recording else 'Start DMMR acquisition and recording.'
                else:
                    text = 'Select a discovered DMMR module; missing/ambiguous modules cannot start a scan.'
                combo.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)
            combo.blockSignals(previous)
        amx_setting = self.settingsMgr.settings[self.AMX]
        if not amx_setting.combo.view().isVisible():
            choices = ['None', *sorted({p.name for p in self.pluginManager.plugins if p.name in ('AMX_A', 'AMX_B')})]
            selected_amx = str(amx_setting.value)
            if selected_amx not in choices:
                choices.append(selected_amx)
            previous = amx_setting.combo.blockSignals(True)
            if amx_setting.items != choices:
                amx_setting.combo.clear()
                amx_setting.combo.addItems(choices)
            amx_setting.combo.setCurrentText(selected_amx)
            amx_setting.combo.blockSignals(previous)
        offset_setting = self.settingsMgr.settings[self.OFFSET]
        if not offset_setting.combo.view().isVisible():
            ampr = sorted((c for c in channels if self._is_ampr(c)),
                          key=lambda c: (c.getDevice().name, c.module_address(), c.channel_number(), c.name))
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
                    if other.name not in ('AMX_A', 'AMX_B'):
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
                      'Channel Min/Max can restrict it; the PSU hardware ceiling cannot be overridden.')
        try:
            rails = self._associated_rails(self._plugin(self.amx_name), self._selected_outputs())
            low, high = self._amplitude_range(rails)
            limits = f'{low:g} – {high:g}'
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
        self._refresh_offset_readback()
        # Same preflight as Start, but do not initialize data, bind a plan, log
        # warnings or touch a device. Keep the last outcome (including save errors).
        try:
            self._preflight()
            self._ready = True
            status = 'Ready to scan'
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
        super().loadSettings(file=file, useDefaultFile=useDefaultFile)
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
        return amplitude_steps(start, stop, step)

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
        data = np.full(len(self.inputChannels[0].recordingData), np.nan, dtype=np.float64)
        try:
            source = self._detector()
        except ScanError:
            return  # No synthetic or unavailable channel in the data model.
        self.displayDefault = source.name
        self.addOutputChannel(name=source.name, unit='A', recordingData=data)

    def addOutputChannel(self, name, unit='', recordingData=None, recordingBackground=None):
        channel = _Signal(parentPlugin=self, name=name, unit=unit, inout=INOUT.OUT,
            recordingData=recordingData, recordingBackground=recordingBackground)
        self.outputChannels.append(channel)
        self.channels.append(channel)
        return channel

    def _preflight(self):
        """Read-only validation shared by the idle status and actual Start."""
        detector = self._detector()
        self._registered(detector)
        plan = self._prepare()
        self._check_detectors([detector])
        plan['detectors'] = [detector]
        plan['detector_identity'] = (detector.getDevice(), detector.module_address(), detector.name)
        self._cadence_epoch(detector.getDevice())
        plan['detector_interval_ms'] = float(detector.getDevice().interval)
        plan['metadata']['detector'] = dict(device=detector.getDevice().name, module=detector.module_address(), channel=detector.name,
                                          interval_ms=plan['detector_interval_ms'])
        if plan['mode'] == self.CONTINUOUS:
            plan['metadata']['detector_cadence'] = self._require_time_step(detector, plan['average'])
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
            name = self._plan['detectors'][0].name
            self.addInputChannels()
            self.addOutputChannels()
            self.toggleDisplay(visible=True)
            self.updateFile()
            self.populateDisplayChannel()
            configured = [name]
            if [c.name for c in self.outputChannels] != configured:
                raise ScanError(f'Measured signal {name} must be available and recording.')
            self._plan['detectors'] = [c.sourceChannel for c in self.outputChannels]
            self._check_detectors()
            n, rails, outputs = len(self.inputChannels[0].recordingData), len(self._plan['rails']), len(configured)
            self._validation = dict(status='running', error='', point_status=['not acquired'] * n,
                rail_v=np.full((n, rails), np.nan), rail_i=np.full((n, rails), np.nan),
                window_start=np.full(n, np.nan), window_end=np.full(n, np.nan),
                samples=np.zeros((n, outputs), dtype=np.int64), finite_samples=np.zeros((n, outputs), dtype=np.int64))
            if self._plan.get('offset'):
                self._validation.update(offset_v=np.full(n, np.nan), offset_target=np.full(n, np.nan))
            self._cancel = Event()
            self.scan_status = 'Ready'
            return True
        except (ScanError, AttributeError, KeyError, TypeError, ValueError) as exc:
            self.scan_status = str(exc)
            self.print(f'Cannot start: {exc}', flag=PRINT.WARNING)
            self._plan = None
            return False

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
            raise ScanError('AMX waveform readback is unavailable.')
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
        for r in rails:
            c, psu, ctrl = r['channel'], r['device'], r['controller']
            if not hasattr(c, 'readback_status') or c.unit != 'V' or not c.useMonitors:
                raise ScanError(f'{c.name}: current PSU Channel interface required.')
            if not all(hasattr(c, field) for field in ('hardware_voltage_limit', 'hardware_current_limit',
                       'voltage_setpoint_readback', 'current_limit_readback', 'current_readback', 'voltage_request_revision')):
                raise ScanError(f'{c.name}: update MScan and PSU plugins together; numeric limits/current and command tracking required.')
            if not c.enabled:
                raise ScanError(f'{c.name}: enable the PSU channel.')
            if not c.active:
                raise ScanError(f'{c.name}: use manual (not equation) control.')
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
            r.update(initial=initial, ilim=ilim, current_capacity=imax,
                     voltage_capacity=float(c.hardware_voltage_limit),
                     request_revision=c.voltage_request_revision, confirmed_vset=initial)
        for plugin in self.pluginManager.plugins:
            if plugin is not self and isinstance(plugin, Scan) and not plugin.finished:
                raise ScanError(f'Finish {plugin.name} before starting this coupled scan.')
        offset = self._offset_plan(low, high)
        return dict(amx=amx, offset=offset, amx_controller=amx.controller, amx_backend=amx.controller.device,
            selected=selected, links=links, waveform=waveform, rails=rails,
            expected=[r['initial'] for r in rails], requested_range=(low, high), timeout=float(self.settle_timeout),
            tolerance=float(self.voltage_tolerance), average=duration, wait=float(self.settling_s), mode=mode,
            metadata=dict(amx=amx.name, outputs=self.amx_outputs, waveform=json.loads(waveform), links=links,
                mode=mode, settling_s=float(self.settling_s), measurement_s=duration,
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
                            current_limit_a=r['current_capacity']) for r in rails],
                amplitude_definition='Vpos=+A, Vneg=-A relative to PSU reference; external offset not included',
                offset=None if offset is None else dict(
                    channel=offset['name'], ampr=offset['device'].name, module=offset['key'][0], ampr_channel=offset['key'][1],
                    coefficient=offset['factor'], initial_v=offset['initial'], final_v=offset['initial'],
                    definition='Offset = coefficient × A, commanded on the AMPR channel at each amplitude '
                               '(rounded to its display precision); verified by its setpoint readback and Monitor.'),
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
                    or not c.enabled or not c.active or not c.real):
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
        return ready, np.asarray(measured), np.asarray(currents), time.time()

    def _command(self, targets, latest=None, *, offset=None):
        # Validate *all* sources before the first write. No OFF/ON, ranges or
        # current-limit writes: use the standard Channel.value path only.
        o = self._plan.get('offset')
        if (o is None) != (offset is None):
            raise ScanError('Quadrupole offset target missing or unexpected; no scan command sent.')
        if 'detector_interval_ms' in self._plan:
            self._check_detectors()
        self._observation()
        if o is not None:
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
        if o is not None:
            c = o['channel']
            before = float(c.value)
            c.value = offset
            # The channel rounds to its display precision: verify what was really requested.
            commanded = float(c.value)
            o['requested'], o['expected'], o['ready'] = offset, commanded, False
            o['written'] |= not math.isclose(commanded, before, rel_tol=1e-10, abs_tol=1e-9)
        return started, time.time()

    def _check_detectors(self, sources=None):
        for source in self._plan['detectors'] if sources is None else sources:
            self._registered(source)
            device = source.getDevice()
            if not self._is_dmmr(source):
                raise ScanError(f'{source.name}: select a real DMMR current module.')
            identity = self._plan.get('detector_identity') if sources is None else None
            if identity and (device is not identity[0] or source.module_address() != identity[1] or source.name != identity[2]):
                raise ScanError('DMMR module identity changed during the scan.')
            if sources is None and 'detector_interval_ms' in self._plan and float(device.interval) != self._plan['detector_interval_ms']:
                raise ScanError('DMMR interval changed during the scan. Restart with the new cadence.')
            if not source.enabled:
                raise ScanError(f'{source.name}: enable the detector channel.')
            if not source.initialized:
                raise ScanError(f'{source.name}: initialize the detector.')
            if not source.acquiring:
                raise ScanError(f'{source.name}: start detector acquisition.')
            if not device.recording:
                raise ScanError(f'{source.name}: start detector recording.')
            if not hasattr(device, 'time') or not hasattr(source, 'values'):
                raise ScanError(f'{source.name}: a timestamped Channel history is required.')

    def _history_start(self):
        self._check_detectors()
        baselines = []
        for c in self._plan['detectors']:
            t = c.getDevice().time.get(length=1)
            baselines.append((id(c.values), float(t[-1]) if len(t) else None))
        return time.time(), baselines

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
        valid = int(np.isfinite(samples).sum())
        mean = float(np.mean(samples)) if len(samples) and valid == len(samples) else np.nan
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

    def _store_point(self, index, start, end, volts, currents, values, *, continuous=False, offset=None):
        validation = self._validation
        validation['window_start'][index], validation['window_end'][index] = start, end
        validation['rail_v'][index], validation['rail_i'][index] = volts, currents
        if offset is not None:
            validation['offset_v'][index], validation['offset_target'][index] = offset
        for j, (mean, count, finite) in enumerate(values):
            self.outputChannels[j].recordingData[index] = mean
            validation['samples'][index, j], validation['finite_samples'][index, j] = count, finite
        validation['point_status'][index] = (('acquired during sweep' if continuous else 'acquired')
            if all(math.isfinite(v[0]) for v in values) else 'invalid detector data')
        self.signalComm.scanUpdateSignal.emit(False)

    def _run_stepped(self, steps):
        p = self._plan
        for index, amplitude in enumerate(steps):
            if self._cancel.is_set():
                raise ScanStopped('Scan stopped.')
            self._bridge.status.emit(f'{index + 1}/{len(steps)}: settling at {amplitude:g} V')
            self._gui(lambda a=float(amplitude): self._command([a] * len(p['rails']), offset=self._offset_target(a)))
            self._settle(p['wait'])
            self._bridge.status.emit(f'{index + 1}/{len(steps)}: acquiring at {amplitude:g} V')
            self._store_point(index, *self._measure(), offset=self._offset_snapshot())

    def _continuous_command(self, amplitude, latest=None, *, window_start=None):
        if latest is not None and time.monotonic() >= latest:
            raise ScanError('Continuous time step missed. Increase Time step; no catch-up command sent.')
        # Validate the live detector cadence on Qt as well as in preflight.
        # No new voltage command may outrun the selected module's data.
        self._require_time_step(self._plan['detectors'][0], self._plan['average'], window_start)
        # The deadline is checked on Qt, immediately before any setpoint write;
        # a late queued callback must not issue a catch-up command.
        return self._command([float(amplitude)] * len(self._plan['rails']), latest=latest,
                             offset=self._offset_target(amplitude))

    def _capture_continuous(self, raw, origin, cutoff, baselines, *, cancel=True):
        start = raw['detector_time'][-1] if raw['detector_time'] else origin
        # A rolling Explorer history may discard samples already copied here.
        # The last copied timestamp must still be retained: otherwise unseen
        # samples may have been lost, and _window_samples must fail closed.
        cursor = [(identity, start) for identity, _ in baselines] if raw['detector_time'] else baselines
        def read():
            samples = self._window_samples(start, cutoff, cursor, closed=False)[0]
            latest = self._plan['detectors'][0].getDevice().time.get(length=1)
            return samples, float(latest[-1]) if len(latest) else -math.inf
        (times, currents), watermark = self._gui(read, cancel=cancel)
        raw['detector_time'].extend(times.tolist())
        raw['detector_current'].extend(currents.tolist())
        return watermark

    def _continuous_windows(self, pending, raw, watermark):
        while pending and watermark >= pending[0][2]:
            index, start, end, volts, currents, offset = pending.popleft()
            left, right = (bisect_right(raw['detector_time'], t) for t in (start, end))
            values = self._average_samples(np.asarray(raw['detector_current'][left:right]))
            self._store_point(index, start, end, volts, currents, [values], continuous=True, offset=offset)

    def _run_continuous(self, steps):
        p = self._plan
        raw = self._validation['continuous'] = dict(
            command_start=np.full(len(steps), np.nan), command_end=np.full(len(steps), np.nan),
            detector_time=[], detector_current=[], psu_time=[], psu_v=[], psu_i=[], psu_target=[], psu_ready=[],
            **(dict(offset_v=[], offset_target=[]) if p.get('offset') else {}))
        pending = deque()
        origin = baselines = cutoff = None
        try:
            self._bridge.status.emit(f'Initial settling at {steps[0]:g} V')
            raw['command_start'][0], raw['command_end'][0] = self._gui(lambda: self._continuous_command(steps[0]))
            self._settle(p['wait'])
            # Define the wall/monotonic origin together, on the same Qt callback
            # that establishes the detector-history identity and baseline.
            (origin, baselines), origin_mono = self._gui(lambda: (self._history_start(), time.monotonic()))
            index, begin, ready_seen = 0, origin, True
            last_command = origin_mono
            duration = p['average']
            next_deadline = origin_mono + duration
            while index < len(steps) or pending:
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
                if offset_now is not None:
                    raw['offset_v'].append(offset_now[0])
                    raw['offset_target'].append(offset_now[1])
                if ready_seen and not ready:
                    raise self._not_ready('PSU readback became invalid or left tolerance after reaching the continuous target.')
                # A first ready observation arriving after the deadline cannot
                # prove that the PSU kept up, even if it has caught up by now.
                ready_seen |= ready and now <= next_deadline
                if not ready and now - last_command >= p['timeout']:
                    raise self._not_ready('PSU voltage settling timed out during continuous acquisition.')
                # A delayed closing timestamp can finalize an earlier window,
                # but a new command requires a valid sample in the current one.
                watermark = self._capture_continuous(raw, origin, cutoff if cutoff is not None else wall, baselines)
                self._continuous_windows(pending, raw, watermark)
                if pending and wall - pending[0][2] > p['timeout']:
                    raise ScanError('Fresh detector data timed out during continuous acquisition.')
                deadline = next_deadline
                now = time.monotonic()
                if index < len(steps) and now >= deadline:
                    if not ready or not ready_seen:
                        raise self._not_ready('PSU target was not confirmed within the continuous time step. '
                                              'Increase Time step; point not acquired and no next command sent.')
                    if now >= deadline + duration:
                        raise ScanError('Continuous time step missed. Increase Time step; no catch-up command sent.')
                    if index + 1 < len(steps):
                        command = self._gui(lambda: self._continuous_command(
                            steps[index + 1], deadline + duration, window_start=begin))
                        raw['command_start'][index + 1], raw['command_end'][index + 1] = command
                        end = command[0]
                        last_command, ready_seen = time.monotonic(), False
                        # Never shorten the next interval to make up for PC
                        # jitter. Actual command intervals are saved as measured.
                        next_deadline = last_command + duration
                    else:
                        self._gui(lambda: self._require_time_step(p['detectors'][0], duration, begin))
                        end = cutoff = time.time()
                    if end <= begin:
                        raise ScanError('System clock moved backwards during continuous acquisition.')
                    pending.append((index, begin, end, volts.copy(), currents.copy(), offset_now))
                    index += 1
                    begin = end
                    if index < len(steps):
                        self._bridge.status.emit(f'{index + 1}/{len(steps)}: continuous at {steps[index]:g} V')
                if index < len(steps) or pending:
                    self._pause(min(self.POLL_S, max(0., next_deadline - time.monotonic()))
                        if index < len(steps) else self.POLL_S)
        finally:
            # Keep available raw samples from an interrupted window, but never
            # present that incomplete window as an acquired spectrum point.
            if origin is not None and abs((time.time() - origin) - (time.monotonic() - origin_mono)) <= .5:
                try:
                    watermark = self._capture_continuous(raw, origin, cutoff if cutoff is not None else time.time(),
                                                        baselines, cancel=False)
                    self._continuous_windows(pending, raw, watermark)
                except Exception:
                    pass  # retain the original failure; no fabricated replacement samples

    def runScan(self, recording):
        validation = self._validation
        try:
            p = self._plan
            steps = self.inputChannels[0].recordingData
            if p['mode'] == self.CONTINUOUS:
                self._run_continuous(steps)
            else:
                self._run_stepped(steps)
            self._bridge.status.emit('Returning to initial PSU setpoints' + (' and offset' if p.get('offset') else ''))
            self._gui(lambda: self._command([r['initial'] for r in p['rails']],
                                            offset=p['offset']['initial'] if p.get('offset') else None))
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
            self._bridge.status.emit(validation['status'].capitalize() + (': ' + validation['error'] if validation['error'] else ''))
            # Stop/error never restores, re-enables or queues another voltage.
            self.signalComm.updateRecordingSignal.emit(False)
            self.signalComm.scanUpdateSignal.emit(True)

    @property
    def recording(self):
        return Scan.recording.fget(self)

    @recording.setter
    def recording(self, value):
        # DeviceManager also stops scans through this property (not the button).
        if not value:
            self._cancel.set()
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
        if self.outputChannels and self.inputChannels:
            self.display.ms.set_data(self.inputChannels[0].getRecordingData(),
                                     self.outputChannels[self.getOutputIndex()].getRecordingData())
            if not update:
                output = self.outputChannels[self.getOutputIndex()]
                self.display.axes[0].set_ylabel(f'{output.name} ({output.unit})')
        else:
            self.display.ms.set_data([], [])
        self.display.axes[0].set_xlabel(self._axis_label())
        self.display.axes[0].relim()
        self.setLabelMargin(self.display.axes[0], .15)
        self.updateToolBar(update=update)
        self.defaultLabelPlot()

    def _axis_label(self):
        metadata = (self._plan or {}).get('metadata', {})
        return 'Commanded amplitude A (V)' if metadata.get('mode') == self.CONTINUOUS else 'Amplitude A (V)'

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
            for key in ('rail_v', 'rail_i', 'window_start', 'window_end', 'samples', 'finite_samples'):
                group.create_dataset(key, data=self._validation[key])
            group.create_dataset('point_status', data=self._validation['point_status'], dtype=h5py.string_dtype('utf-8'))
            group['rail_v'].attrs['Unit'] = 'V (unsigned PSU magnitudes)'
            group['rail_i'].attrs['Unit'] = 'A (PSU readback at end of integration, not detector current)'
            if 'offset_v' in self._validation:
                for key in ('offset_v', 'offset_target'):
                    group.create_dataset(key, data=self._validation[key])
                group['offset_v'].attrs['Unit'] = 'V (AMPR Monitor of the quadrupole offset channel)'
                group['offset_target'].attrs['Unit'] = 'V (offset commanded on the AMPR channel, coefficient × A)'
            for key in ('window_start', 'window_end'):
                group[key].attrs['Unit'] = 's since Unix epoch'
            if 'continuous' in self._validation:
                raw = group.create_group('Continuous')
                for key, values in self._validation['continuous'].items():
                    data = np.asarray(values, dtype=bool if key == 'psu_ready' else float)
                    if key in ('psu_v', 'psu_i', 'psu_target'):
                        data = data.reshape(-1, self._validation['rail_v'].shape[1])
                    raw.create_dataset(key, data=data)
                raw.attrs['timestamps'] = 'PC/Explorer times; not hardware-synchronized ADC timestamps.'
                raw.attrs['commands'] = 'GUI dispatch of sequential rail requests; not hardware write/settling times.'
                raw.attrs['readbacks'] = 'Snapshots of shared PSU channels, NaN while unavailable. No interpolation.'
                raw['detector_current'].attrs['Unit'] = 'A (raw DMMR samples, no background subtraction)'
                raw['psu_v'].attrs['Unit'] = 'V (unsigned PSU magnitudes)'
                raw['psu_i'].attrs['Unit'] = 'A (PSU current, not detector current)'
                raw['psu_target'].attrs['Unit'] = 'V (requested PSU magnitudes)'
                if 'offset_v' in raw:
                    raw['offset_v'].attrs['Unit'] = 'V (AMPR Monitor of the quadrupole offset channel)'
                    raw['offset_target'].attrs['Unit'] = 'V (offset commanded on the AMPR channel)'
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
                    for key in ('offset_v', 'offset_target'):
                        if key in group:
                            self._validation[key] = group[key][:]
                    if 'Continuous' in group:
                        self._validation['continuous'] = {key: data[:] for key, data in group['Continuous'].items()}
                    self._validation.update(status=group.attrs['status'], error=group.attrs['error'],
                        point_status=list(group['point_status'].asstr()[:]))
        return loaded

    def pythonPlotCode(self):
        return '''fig, ax = plt.subplots(constrained_layout=True)
ax.plot(inputChannels[0].recordingData, outputChannels[output_index].recordingData, marker='.', markersize=4)
ax.set_xlabel(AXIS_LABEL)
ax.set_ylabel(f'{outputChannels[output_index].name} ({outputChannels[output_index].unit})')
plt.show()
'''.replace('AXIS_LABEL', repr(self._axis_label()))
