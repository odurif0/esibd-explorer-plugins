"""AMX/PSU amplitude scan, forked from Explorer's MassSpec (msScan).

Upstream: ioneater/ESIBD-Explorer, esibd/scans/ms/ms.py, commit 9945145.
Copyright (C) 2021-2026 Tim Esser. GPL-2.0-or-later; see LICENSE.
Modified 2026-09-22 for ESIBD Explorer Plugins: coupled rails, verified settling,
time-windowed acquisition and explicit missing data. No driver access here.
"""
from __future__ import annotations

import json
import math
from threading import Event
import time
from typing import Any, Callable

import h5py
import numpy as np
from PyQt6.QtCore import QObject, QThread, QTimer, Qt, pyqtSignal, pyqtSlot
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QHeaderView, QSizePolicy, QTreeWidgetItem

from esibd.core import INOUT, PARAMETERTYPE, PRINT, MetaChannel, Parameter, parameterDict, plotting
from esibd.plugins import Scan


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


class _Amplitude(MetaChannel):
    """An Explorer scan axis, not a second device or an independently writable rail."""
    def connectSource(self, giveFeedback=False):
        pass


class MScan(Scan):
    """Sweep symmetric PSU magnitudes while keeping the selected AMX waveform fixed.

    A is defined by Vpos=+A, Vneg=-A relative to the PSU reference. The x axis
    is volts, not m/z. The operator must establish the mass-filter waveform
    and calibration separately. Devices must already be enabled and recording.
    """
    name = 'MScan'
    version = '0.1.0'
    supportedVersion = '1.0'
    iconFile = 'mscan.png'
    useInvalidWhileWaiting = False
    AMX = 'AMX'
    OUTPUTS = 'AMX outputs'
    SUPPLIES = 'Driven PSU'
    FREQUENCY = 'Fixed frequency'
    NO_SIGNAL = 'Select channel'
    TIMEOUT = 'Settle timeout'
    TOLERANCE = 'Voltage tolerance'
    STATUS = 'Status'
    VALIDATION = 'Validation'
    PAIRS = {'CH0-CH1': (0, 1), 'CH2-CH3': (2, 3), 'CH0-CH3': (0, 1, 2, 3)}
    POLL_S = .05

    class Display(Scan.Display):
        def initGUI(self):
            super().initGUI()
            self.addAction(event=lambda: self.copyLineDataClipboard(line=self.ms),
                           toolTip='Data to Clipboard.', icon=self.dataClipboardIcon, before=self.copyAction)

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
        super().__init__(**kwargs)
        self.useDisplayChannel = True
        self._bridge = _GuiBridge(self)

    def getDefaultSettings(self):
        settings = super().getDefaultSettings()
        # Keep the historical storage key/attribute, but acquire only the selected
        # channel. The combo's other items are choices, never hidden acquisitions.
        settings[self.DISPLAY] = parameterDict(value=self.NO_SIGNAL, items=self.NO_SIGNAL,
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='displayDefault', event=self._signal_changed,
            toolTip='Signal measured after the quadrupole. Select an Explorer channel; only this channel is recorded. '
                    'Its device must be acquiring and recording. Units and device are shown in the choices\' tooltips.')
        settings[self.AMX] = parameterDict(value='None', items='None, AMX_A, AMX_B',
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='amx_name', event=self._setup_changed,
            toolTip='AMX with PSU associations already configured in its Settings. No automatic ON or config load.')
        settings[self.OUTPUTS] = parameterDict(value='CH0-CH1', items=', '.join(self.PAIRS),
            parameterType=PARAMETERTYPE.COMBO, fixedItems=True, attr='amx_outputs', event=self._setup_changed,
            toolTip='Outputs wired to this quadrupole. Both rails of each associated PSU are swept together. Other outputs sharing these supplies are also affected.')
        for key, value, attr in ((self.START, 50., 'start'), (self.STOP, 200., 'stop'), (self.STEP, 1., 'step')):
            settings[key] = parameterDict(value=value, minimum=.000001 if key == self.STEP else 0., maximum=1e6,
                parameterType=PARAMETERTYPE.FLOAT, attr=attr, event=self.estimateScanTime, instantUpdate=False,
                displayDecimals=3, unit='V', toolTip='Amplitude A: rail levels -A / +A, not peak-to-peak voltage.')
        settings[self.WAIT][Parameter.TOOLTIP] = 'Continuous time in tolerance after the PSU commands finish, before averaging (ms).'
        settings[self.WAITLONG][Parameter.TOOLTIP] = 'Continuous settling time for a large amplitude step (ms).'
        settings[self.AVERAGE][Parameter.MIN] = 1
        settings[self.TIMEOUT] = parameterDict(value=30., minimum=.1, maximum=3600., unit='s',
            parameterType=PARAMETERTYPE.FLOAT, attr='settle_timeout', advanced=True,
            toolTip='Abort if voltage settling or fresh detector data takes longer than this timeout.')
        settings[self.TOLERANCE] = parameterDict(value=1., minimum=.001, maximum=100., unit='V',
            parameterType=PARAMETERTYPE.FLOAT, attr='voltage_tolerance', advanced=True,
            toolTip='Allowed absolute difference between each measured PSU magnitude and its requested value.')
        settings[self.STATUS] = parameterDict(value='Idle', parameterType=PARAMETERTYPE.LABEL,
            attr='scan_status', indicator=True, restore=False,
            toolTip='Normal completion restores the original PSU setpoints. Stop/error holds the last setpoints; HV stays ON.')
        settings[self.SUPPLIES] = parameterDict(value='Select an AMX', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False,
            toolTip='Both listed PSU setpoints follow A. Physical rails are -A / +A relative to the PSU reference. '
                    'All AMX connectors sharing a listed PSU are affected. This display never commands a device.')
        settings[self.FREQUENCY] = parameterDict(value='Unavailable', parameterType=PARAMETERTYPE.LABEL,
            indicator=True, restore=False, toolTip='Read from AMX. Frequency, duty cycle and routing stay fixed; change them in AMX, not here.')
        settings[self.AVERAGE][Parameter.TOOLTIP] = 'Average fresh detector samples over this time window (ms), after voltage settling.'
        order = (self.AMX, self.OUTPUTS, self.SUPPLIES, self.FREQUENCY,
                 self.START, self.STOP, self.STEP, self.DISPLAY, self.WAIT, self.WAITLONG, self.LARGESTEP,
                 self.AVERAGE, self.SCANTIME, self.STATUS, self.NOTES, self.TIMEOUT, self.TOLERANCE)
        return {key: settings[key] for key in order}

    def initGUI(self):
        super().initGUI()
        self._format_settings()
        self._refresh_interface()
        # Discovery/readback only, on Qt: no hardware reads and no reinitializing
        # scan data when a device appears, disappears or changes its live value.
        self._interface_timer = QTimer(self._bridge)
        self._interface_timer.timeout.connect(self._refresh_interface)
        self._interface_timer.start(500)

    def _format_settings(self):
        labels = {self.DISPLAY: 'Measured signal', self.OUTPUTS: 'AMX connectors',
                  self.START: 'Amplitude from (V)', self.STOP: 'Amplitude to (V)', self.STEP: 'Amplitude step (V)',
                  self.WAIT: 'Settling (ms)', self.WAITLONG: 'Large-step settling (ms)', self.LARGESTEP: 'Large step (V)',
                  self.AVERAGE: 'Integration (ms)', self.SCANTIME: 'Estimated time',
                  self.TIMEOUT: 'Settle timeout (s)', self.TOLERANCE: 'Voltage tolerance (V)'}
        for key, label in labels.items():
            self.settingsMgr.settings[key].setText(0, label)  # labels, not INI/HDF5 keys
        self.settingsTree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for spin in self.settingsTree.findChildren(QAbstractSpinBox):
            spin.setKeyboardTracking(False)
        for key in (self.DISPLAY, self.AMX, self.OUTPUTS):
            combo = self.settingsMgr.settings[key].combo
            combo.setMaximumWidth(16777215)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
        for key in (self.SUPPLIES, self.FREQUENCY, self.STATUS):
            label = self.settingsMgr.settings[key].label
            label.setMaximumHeight(16777215)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def _signal_changed(self):
        if not self.loading and not self.settingsMgr.loading:
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

    def _refresh_interface(self):
        if (not self.finished or self.recording or self.loading or self.settingsMgr.loading
                or self.pluginManager.loading or self.pluginManager.closing):
            return
        channels = list(self.pluginManager.DeviceManager.channels())
        setting = self.settingsMgr.settings[self.DISPLAY]
        combo, selected = setting.combo, str(setting.value).strip()
        if not combo.view().isVisible():
            readable = [c for c in channels if callable(getattr(c, 'getValues', None)) and
                        (c.inout == INOUT.OUT or c.useMonitors or not c.real)]
            names = sorted({c.name for c in readable}, key=str.casefold)
            # Preserve a missing saved selection explicitly, never substitute a
            # different instrument. Drop obsolete *unselected* legacy items.
            choices = [self.NO_SIGNAL, *names]
            if selected and selected not in choices:
                choices.append(selected)
            previous = combo.blockSignals(True)
            if setting.items != choices:
                combo.clear()
                combo.addItems(choices)
            combo.setCurrentText(selected or self.NO_SIGNAL)
            for index, name in enumerate(choices):
                matches = [c for c in channels if c.name.strip().casefold() == name.strip().casefold()]
                if name == self.NO_SIGNAL:
                    text = 'Choose the detector channel to record.'
                elif len(matches) != 1:
                    text = 'Channel unavailable or name ambiguous. Select a unique existing channel.'
                else:
                    c = matches[0]
                    text = f'{c.getDevice().name}: {c.name} ({c.unit}). Only this signal will be recorded.'
                combo.setItemData(index, text, Qt.ItemDataRole.ToolTipRole)
            combo.blockSignals(previous)
        supplies, frequency = 'Select an AMX', 'Unavailable'
        try:
            amx = self._plugin(self.amx_name)
            selected_outputs = self.PAIRS[self.amx_outputs]
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
                shared = [f'CH{i}-CH{i + 1}' for i, source in links.items() if source == name and i not in selected_outputs]
                if shared:
                    lines.append('Also affects ' + ', '.join(shared))
            supplies = '\n'.join(lines)
            rows = json.loads(self._waveform(amx, selected_outputs))
            frequencies = list(dict.fromkeys(r.get('frequency', 'Unavailable') for r in rows))
            frequency = ', '.join(frequencies)
        except (ScanError, AttributeError, KeyError, TypeError, ValueError):
            pass  # unavailable is not a measurement; the preflight explains any blocker
        for key, value in ((self.SUPPLIES, supplies), (self.FREQUENCY, frequency)):
            setting = self.settingsMgr.settings[key]
            if setting.value != value:
                setting.value = value
        self.settingsTree.scheduleDelayedItemsLayout()

    def loadSettings(self, file=None, useDefaultFile=False):
        if not self.finished:
            self.print('Cannot load settings while a scan is running or being saved.', flag=PRINT.WARNING)
            return
        super().loadSettings(file=file, useDefaultFile=useDefaultFile)
        self._format_settings()
        self._refresh_interface()
        self.dummyInitialization()

    def initData(self):
        super().initData()
        if self.inputChannelGroupItem:
            self.inputChannelGroupItem.setText(0, 'Scan axis')
        if self.outputChannelGroupItem:
            self.outputChannelGroupItem.setText(0, 'Measured signal')
            self.outputChannelGroupItem.setToolTip(0, 'Live detector value. The plot and file contain averages over each integration window.')

    def getSteps(self, start, stop, step):
        try:
            return amplitude_steps(start, stop, step)
        except ScanError:
            return None

    def addInputChannels(self):
        steps = self.getSteps(self.start, self.stop, self.step)
        if steps is not None:
            self.addInputChannel('Amplitude', unit='V', recordingData=steps)

    def addInputChannel(self, name, start=None, stop=None, step=None, unit='V', recordingData=None):
        axis = _Amplitude(parentPlugin=self, name=name, unit=unit, recordingData=recordingData, inout=INOUT.IN)
        self.inputChannels.append(axis)
        self.channels.append(axis)
        if self.inputChannelGroupItem:
            row = QTreeWidgetItem(self.inputChannelGroupItem)
            columns = list(self.headerChannel.getSortedDefaultChannel())
            data = axis.getRecordingData()
            span = f'{data[0]:g} → {data[-1]:g}' if data is not None and len(data) else '—'
            for key, text in ((Parameter.NAME, name), (Parameter.VALUE, span), ('Unit', unit)):
                row.setText(columns.index(key), text)
            row.setToolTip(columns.index(Parameter.NAME), 'Planned amplitude A, not a separately writable PSU channel. Rail levels are -A / +A.')
        return axis

    def addOutputChannels(self):
        if not self.inputChannels:
            return
        data = np.full(len(self.inputChannels[0].recordingData), np.nan, dtype=np.float64)
        name = str(self.displayDefault).strip()
        if name and name != self.NO_SIGNAL:
            self.addOutputChannel(name=name, recordingData=data)
        if self.channelTree:
            self.channelTree.setHeaderLabels([item.get(Parameter.HEADER, '') or name.title()
                for name, item in self.headerChannel.getSortedDefaultChannel().items()])
            self.toggleAdvanced(advanced=False)

    def initScan(self):
        self._plan = None
        self._validation = {}
        if self._dummy_initialization:
            return super().initScan()
        try:
            name = str(self.displayDefault).strip()
            if not name or name == self.NO_SIGNAL:
                raise ScanError('Select a measured signal channel.')
            detector = self.pluginManager.DeviceManager.getChannelByName(name)
            if detector is None:
                raise ScanError(f'Measured signal {name} is unavailable. Select an existing channel.')
            self._registered(detector)
            self._plan = self._prepare()
            if not super().initScan():
                raise ScanError(f'Measured signal {name} must be acquiring and recording.')
            configured = [name]
            if [c.name for c in self.outputChannels] != configured:
                raise ScanError(f'Measured signal {name} must be available and recording.')
            self._plan['detectors'] = [c.sourceChannel for c in self.outputChannels]
            self._check_detectors()
            n, rails, outputs = len(self.inputChannels[0].recordingData), len(self._plan['rails']), len(configured)
            self._validation = dict(status='running', error='', point_status=['not acquired'] * n,
                rail_v=np.full((n, rails), np.nan), window_start=np.full(n, np.nan), window_end=np.full(n, np.nan),
                samples=np.zeros((n, outputs), dtype=np.int64), finite_samples=np.zeros((n, outputs), dtype=np.int64))
            self._cancel = Event()
            self.scan_status = 'Ready'
            return True
        except (ScanError, AttributeError, TypeError, ValueError) as exc:
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

    def _waveform(self, amx, selected):
        controller = amx.controller
        if (not controller.initialized or controller.device is None or not amx.isOn()
                or controller.initializing or controller.transitioning):
            raise ScanError('AMX is not ready and ON.')
        rows = controller.output_rows
        if not rows or len(rows) != 4:
            raise ScanError('AMX waveform readback is unavailable.')
        chosen = [rows[i] for i in selected]
        if any(r.get('state') != 'Periodic' or not r.get('timing') for r in chosen):
            raise ScanError('Use a known continuous periodic AMX waveform with resolved edge timing.')
        return json.dumps(chosen, sort_keys=True, allow_nan=False)

    def _prepare(self):
        steps = amplitude_steps(float(self.start), float(self.stop), float(self.step))
        if not all(math.isfinite(float(v)) and float(v) > 0
                   for v in (self.average, self.wait, self.waitLong, self.settle_timeout, self.voltage_tolerance)):
            raise ScanError('Averaging, settling, timeout and tolerance must be positive and finite.')
        amx = self._plugin(self.amx_name)
        selected = self.PAIRS[self.amx_outputs]
        waveform = self._waveform(amx, selected)
        links = [(attr, str(getattr(amx, attr))) for attr in ('psu_ch01', 'psu_ch23')
                 if (0 if attr == 'psu_ch01' else 2) in selected]
        rails = []
        for name in dict.fromkeys(name for _, name in links):
            psu = self._plugin(name)
            for number in (0, 1):
                candidates = [c for c in psu.getChannels() if c.real
                              and callable(getattr(c, 'channel_number', None)) and c.channel_number() == number]
                if len(candidates) != 1:
                    raise ScanError(f'{name} CH{number} is missing or ambiguous.')
                c = candidates[0]
                self._registered(c)
                if not hasattr(c, 'readback_status') or c.unit != 'V' or not c.useMonitors:
                    raise ScanError(f'{c.name}: current PSU Channel interface required.')
                if not c.enabled or not c.active or not c.initialized:
                    raise ScanError(f'{c.name}: enable the channel and use manual (not equation) control.')
                if not psu.isOn() or not math.isfinite(float(c.monitor)):
                    raise ScanError(f'{c.name}: PSU must already be ON with valid readbacks.')
                if c.min is None or c.max is None or min(steps) < c.min or max(steps) > c.max:
                    raise ScanError(f'{c.name}: scan exceeds channel limits.')
                token = psu.controller._output_cancel
                if token.is_set():
                    raise ScanError(f'{c.name}: output shutdown requested.')
                rails.append(dict(channel=c, name=c.name, device=psu, controller=psu.controller,
                    backend=psu.controller.device, token=token, initial=float(c.value), number=number))
        if not all(math.isfinite(r['initial']) and r['initial'] >= 0 for r in rails):
            raise ScanError('Initial PSU setpoints are invalid.')
        for plugin in self.pluginManager.plugins:
            if plugin is not self and isinstance(plugin, Scan) and not plugin.finished:
                raise ScanError(f'Finish {plugin.name} before starting this coupled scan.')
        return dict(amx=amx, amx_controller=amx.controller, amx_backend=amx.controller.device,
            selected=selected, links=links, waveform=waveform, rails=rails,
            expected=[r['initial'] for r in rails], timeout=float(self.settle_timeout),
            tolerance=float(self.voltage_tolerance), average=float(self.average) / 1000,
            wait=float(self.wait) / 1000, wait_long=float(self.waitLong) / 1000, large_step=float(self.largestep),
            metadata=dict(amx=amx.name, outputs=self.amx_outputs, waveform=json.loads(waveform), links=links,
                rails=[dict(channel=r['name'], psu=r['device'].name, rail='Vpos' if r['number'] == 0 else 'Vneg',
                            initial_vset=r['initial']) for r in rails],
                amplitude_definition='Vpos=+A, Vneg=-A relative to PSU reference; external offset not included',
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
        measured = []
        ready = True
        for rail, expected in zip(p['rails'], p['expected']):
            c, psu, ctrl = rail['channel'], rail['device'], rail['controller']
            self._registered(c)
            if (c.name != rail['name'] or self._plugin(psu.name) is not psu or psu.controller is not ctrl
                    or ctrl.device is not rail['backend'] or ctrl._output_cancel is not rail['token']
                    or rail['token'].is_set() or not psu.isOn() or not ctrl.initialized
                    or not c.enabled or not c.active or not c.real):
                raise ScanError(f'{rail["name"]}: source changed or was stopped.')
            if not math.isclose(float(c.value), expected, rel_tol=1e-10, abs_tol=1e-9):
                raise ScanError(f'{c.name}: setpoint changed outside the scan.')
            v = float(c.monitor)
            measured.append(v)
            busy = any(bool(getattr(ctrl, flag, False)) for flag in
                       ('initializing', 'transitioning', '_manual_apply_active', '_manual_apply_worker_running', '_hv_config_loading'))
            ready &= not busy and math.isfinite(v) and v >= 0 and abs(v - expected) <= p['tolerance']
        return ready, np.asarray(measured), time.time()

    def _command(self, targets):
        # Validate *all* sources before the first write. No OFF/ON, ranges or
        # current-limit writes: use the standard Channel.value path only.
        self._observation()
        for rail, target in zip(self._plan['rails'], targets):
            c = rail['channel']
            if not math.isfinite(target) or not c.min <= target <= c.max:
                raise ScanError(f'{c.name}: requested amplitude exceeds current limits.')
        self._plan['expected'] = list(targets)
        for rail, target in zip(self._plan['rails'], targets):
            rail['channel'].value = target

    def _check_detectors(self):
        for source in self._plan['detectors']:
            self._registered(source)
            device = source.getDevice()
            if not source.enabled or not source.initialized or not source.acquiring or not device.recording:
                raise ScanError(f'{source.name}: detector must remain enabled, acquiring and recording.')
            if not hasattr(device, 'time') or not hasattr(source, 'values'):
                raise ScanError(f'{source.name}: a timestamped Channel history is required.')

    def _history_start(self):
        self._check_detectors()
        baselines = []
        for c in self._plan['detectors']:
            t = c.getDevice().time.get(length=1)
            baselines.append((id(c.values), float(t[-1]) if len(t) else None))
        return time.time(), baselines

    def _read_window(self, start, end, baselines):
        self._check_detectors()
        results = []
        for c, (history, baseline) in zip(self._plan['detectors'], baselines):
            times = np.asarray(c.getDevice().time.get())
            values = np.asarray(c.getValues(subtractBackground=False))
            if history != id(c.values) or len(times) != len(values):
                raise ScanError(f'{c.name}: detector history reset or timestamps misaligned.')
            if baseline is not None and (not len(times) or times[0] > baseline):
                raise ScanError(f'{c.name}: history buffer truncated during averaging.')
            if not len(times) or times[-1] < end:
                return None  # wait for a sample closing the acquisition window
            if not np.isfinite(times).all() or np.any(np.diff(times) <= 0):
                raise ScanError(f'{c.name}: invalid or nonmonotonic timestamps.')
            samples = values[(times > start) & (times <= end)]
            valid = int(np.isfinite(samples).sum())
            mean = float(np.mean(samples)) if len(samples) and valid == len(samples) else np.nan
            results.append((mean, len(samples), valid))
        return results

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

    def _pause(self):
        if self._cancel.wait(self.POLL_S):
            raise ScanStopped('Scan stopped.')

    def _settle(self, seconds):
        deadline, since = time.monotonic() + self._plan['timeout'], None
        while True:
            ready, volts, _ = self._gui(self._observation)
            now = time.monotonic()
            since = (since if since is not None else now) if ready else None
            if since is not None and now - since >= seconds:
                return volts
            if now >= deadline:
                raise ScanError('PSU voltage settling timed out; no detector value assigned to this point.')
            self._pause()

    def _measure(self):
        start, baselines = self._gui(self._history_start)
        start_mono = time.monotonic()
        end = start + self._plan['average']
        deadline = start_mono + self._plan['average'] + self._plan['timeout']
        while True:
            ready, volts, wall = self._gui(self._observation)
            if not ready:
                raise ScanError('PSU readback became invalid or left tolerance during acquisition.')
            if abs((wall - start) - (time.monotonic() - start_mono)) > .5:
                raise ScanError('System clock changed during acquisition.')
            if time.monotonic() >= start_mono + self._plan['average']:
                values = self._gui(lambda: self._read_window(start, end, baselines))
                if values is not None:
                    return start, end, volts, values
            if time.monotonic() >= deadline:
                raise ScanError('Fresh detector data timed out.')
            self._pause()

    def runScan(self, recording):
        validation, index = self._validation, 0
        try:
            p = self._plan
            steps = self.inputChannels[0].recordingData
            for index, amplitude in enumerate(steps):
                if self._cancel.is_set():
                    raise ScanStopped('Scan stopped.')
                delta = max(abs(amplitude - v) for v in p['expected'])
                self._bridge.status.emit(f'{index + 1}/{len(steps)}: settling at {amplitude:g} V')
                self._gui(lambda a=float(amplitude): self._command([a] * len(p['rails'])))
                self._settle(p['wait_long'] if delta > p['large_step'] else p['wait'])
                self._bridge.status.emit(f'{index + 1}/{len(steps)}: acquiring at {amplitude:g} V')
                start, end, volts, values = self._measure()
                validation['window_start'][index], validation['window_end'][index] = start, end
                validation['rail_v'][index] = volts
                for j, (mean, count, finite) in enumerate(values):
                    self.outputChannels[j].recordingData[index] = mean
                    validation['samples'][index, j] = count
                    validation['finite_samples'][index, j] = finite
                validation['point_status'][index] = 'acquired' if all(math.isfinite(v[0]) for v in values) else 'invalid detector data'
                self.signalComm.scanUpdateSignal.emit(False)
            self._bridge.status.emit('Returning to initial PSU setpoints')
            self._gui(lambda: self._command([r['initial'] for r in p['rails']]))
            self._settle(p['wait'])
            validation['status'] = 'completed'
        except ScanStopped as exc:
            validation['status'], validation['error'] = 'stopped', str(exc)
        except Exception as exc:
            validation['status'], validation['error'] = 'error', str(exc)
            self.print(f'Scan aborted: {exc}', flag=PRINT.ERROR)
        finally:
            if validation['point_status'][index] == 'not acquired':
                validation['point_status'][index] = validation['status']
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
        Scan.finished.fset(self, value)
        for key, setting in self.settingsMgr.settings.items():
            if key not in (self.NOTES, self.STATUS, self.SCANTIME):
                setting.setEnabled(value)

    def toggleRecording(self):
        if not self.recording:
            self._cancel.set()
        elif self.finished and self.settingsTree:
            for spin in self.settingsTree.findChildren(QAbstractSpinBox):
                spin.interpretText()
        super().toggleRecording()

    def close(self):
        self._cancel.set()
        return super().close()

    def closeGUI(self):
        if hasattr(self, '_interface_timer'):
            self._interface_timer.stop()
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
        self.display.axes[0].set_xlabel('Amplitude A (V)')
        self.display.axes[0].relim()
        self.setLabelMargin(self.display.axes[0], .15)
        self.updateToolBar(update=update)
        self.defaultLabelPlot()

    def saveData(self, file):
        super().saveData(file)
        if not self._validation:
            return
        with h5py.File(file, 'a') as handle:
            group = handle[self.name].require_group(self.VALIDATION)
            group.attrs['status'] = self._validation['status']
            group.attrs['error'] = self._validation['error']
            group.attrs['setup'] = json.dumps(self._plan['metadata'], allow_nan=False)
            for key in ('rail_v', 'window_start', 'window_end', 'samples', 'finite_samples'):
                group.create_dataset(key, data=self._validation[key])
            group.create_dataset('point_status', data=self._validation['point_status'], dtype=h5py.string_dtype('utf-8'))
            group['rail_v'].attrs['Unit'] = 'V (unsigned PSU magnitudes)'
            for key in ('window_start', 'window_end'):
                group[key].attrs['Unit'] = 's since Unix epoch'

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
                group = handle[self.name].get(self.VALIDATION)
                if group is not None:
                    self._plan = {'metadata': json.loads(group.attrs['setup'])}
                    self._validation = {key: group[key][:] for key in
                        ('rail_v', 'window_start', 'window_end', 'samples', 'finite_samples')}
                    self._validation.update(status=group.attrs['status'], error=group.attrs['error'],
                        point_status=list(group['point_status'].asstr()[:]))
        return loaded

    def pythonPlotCode(self):
        return '''fig, ax = plt.subplots(constrained_layout=True)
ax.plot(inputChannels[0].recordingData, outputChannels[output_index].recordingData, marker='.', markersize=4)
ax.set_xlabel('Amplitude A (V)')
ax.set_ylabel(f'{outputChannels[output_index].name} ({outputChannels[output_index].unit})')
plt.show()
'''
