"""MScan plugin rig shared by the MScan tests."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from threading import Event
import time
from types import ModuleType
from types import SimpleNamespace as NS

import h5py
import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def mscan_module(monkeypatch):
    pytest.importorskip('PyQt6.QtCore')
    core, plugins = ModuleType('esibd.core'), ModuleType('esibd.plugins')
    class Scan:
        Display = type('Display', (), {})
        recording = property(lambda s: s._recording, lambda s, value: setattr(s, '_recording', value))
        def saveData(self, file):
            with h5py.File(file, 'w') as f:
                group = f.create_group(self.name)
                group.create_group('Input Channels').create_dataset('Amplitude', data=self.inputChannels[0].recordingData)
                for c in self.outputChannels:
                    group.require_group('Output Channels').create_dataset(c.name, data=c.recordingData)
        def loadDataInternal(self):
            return True
    plugins.Scan = Scan
    plugins.Plugin = type('Plugin', (), {})
    plugins.SettingsManager = type('SettingsManager', (), {})
    core.TreeWidget = type('TreeWidget', (), {})
    core.INFO, core.infoDict = 'Info', lambda name: {'Plugin': name}
    core.INOUT = NS(IN='IN', OUT='OUT')
    core.PRINT = NS(WARNING='warning', ERROR='error')
    core.MetaChannel = type('MetaChannel', (), {})
    core.Parameter, core.PARAMETERTYPE = NS(), NS()
    core.parameterDict = lambda **kw: kw
    core.plotting = lambda fn: fn
    monkeypatch.setitem(sys.modules, 'esibd.core', core)
    monkeypatch.setitem(sys.modules, 'esibd.plugins', plugins)
    spec = importlib.util.spec_from_file_location('mscan_protocol_test', ROOT / 'mscan/mscan_plugin.py')
    result = importlib.util.module_from_spec(spec)
    # Explorer's dynamicImport executes without registering the entrypoint.
    monkeypatch.delitem(sys.modules, spec.name, raising=False)
    spec.loader.exec_module(result)
    assert spec.name not in sys.modules
    return result


class History:
    def __init__(self):
        self.data = []
    def get(self, length=None):
        return np.asarray(self.data[-length:] if length else self.data, dtype=float)


class Rail:
    def __init__(self, parent, index):
        self.parent, self.id, self.number = parent, 100 + index, index
        self.name = f'{parent.name}_CH{index}'
        self._value, self.monitor = 40. + 10 * index, 40. + 10 * index
        self.real = self.enabled = self.active = self.initialized = self.useMonitors = True
        self.unit, self.readback_status = 'V', 'Valid'
        self.min, self.max = 0., 250.
        self.hardware_voltage_limit, self.hardware_current_limit = 200., .5
        self.voltage_setpoint_readback = self._value
        self.voltage_request_revision = 0
        self.current_limit_readback, self.current_readback = .01, .002
    def channel_number(self):
        return self.number
    @property
    def value(self):
        return self._value
    @value.setter
    def value(self, v):
        self.parent.writes.append((self.number, v))
        self.voltage_request_revision += 1
        self._value = v
        self.parent.controller._manual_apply_worker_running = True
        self.monitor = np.nan


@pytest.fixture
def mscan_rig(mscan_module):
    scan = mscan_module.MScan.__new__(mscan_module.MScan)
    scan._cancel, scan._recording = Event(), True
    scan.notes = ''
    scan.amx_name, scan.amx_outputs = 'AMX_A', 'CH0-CH1'
    scan.start, scan.stop, scan.step = 10., 30., 10.
    scan.settling_s = .003
    scan.integration_s, scan.settle_timeout, scan.voltage_tolerance = .012, .15, 1.
    rows = [dict(state='Periodic', timing={'period': 200, 'high': 100, 'phase': i % 2 * 100}) for i in range(4)]
    amx = NS(name='AMX_A', psu_ch01='PSU_A', psu_ch23='PSU_B', isOn=lambda: True,
        controller=NS(initialized=True, device=object(), initializing=False, transitioning=False, output_rows=rows))
    psus = []
    for name in ('PSU_A', 'PSU_B'):
        p = NS(name=name, writes=[], controller=NS(initialized=True, device=object(), _output_cancel=Event(),
               _manual_apply_worker_running=False), isOn=lambda: True)
        p.channels = [Rail(p, i) for i in (0, 1)]
        p.getChannels = lambda p=p: p.channels
        psus.append(p)
    detector_device = NS(name='DMMR', initialized=True, acquiring=True, recording=True, time=History(), interval=2)
    detector = NS(name='Detector', real=True, unit='A', module_address=lambda: 3, enabled=True, initialized=True, acquiring=True,
                  values=History(), getDevice=lambda: detector_device)
    scan.detector_module = 'Module 3 — Detector'
    detector.getValues = lambda **kw: detector.values.get()
    all_channels = [c for p in psus for c in p.channels] + [detector]
    manager = NS(plugins=[amx, *psus, scan])
    manager.DeviceManager = NS(channels=lambda: all_channels,
        getChannelByName=lambda name: next((c for c in all_channels if c.name.lower() == name.lower()), None))
    scan.pluginManager = manager
    scan._gui = lambda action, **kw: action()
    # This non-Qt rig executes post-reap notifications synchronously as well.
    scan._queue_completion = lambda action: scan._gui(action, cancel=False)
    scan._bridge = NS(status=NS(emit=lambda text: setattr(scan, 'scan_status', text)))
    scan.signalComm = NS(scanUpdateSignal=NS(emit=lambda done: None), updateRecordingSignal=NS(emit=lambda value: None))
    scan.print = lambda *a, **kw: None
    scan.POLL_S = .002
    scan._plan = scan._prepare()
    scan._plan['detectors'] = [detector]
    scan.inputChannels = [NS(recordingData=np.asarray([10., 20., 30.]))]
    scan.outputChannels = [NS(name='Detector', recordingData=np.full(3, np.nan))]
    scan._validation = dict(status='running', error='', point_status=['not acquired'] * 3,
        rail_v=np.full((3, len(scan._plan['rails'])), np.nan), rail_i=np.full((3, len(scan._plan['rails'])), np.nan),
        window_start=np.full(3, np.nan), window_end=np.full(3, np.nan),
        samples=np.zeros((3, 1), dtype=int), finite_samples=np.zeros((3, 1), dtype=int))
    r = NS(module=mscan_module, scan=scan, psus=psus, amx=amx, detector=detector,
           detector_device=detector_device, all_channels=all_channels,
           current=lambda: psus[0].channels[0].value, on_pause=lambda: None)
    def pause():
        r.on_pause()
        if scan._cancel.wait(scan.POLL_S):
            raise mscan_module.ScanStopped('Scan stopped.')
        for p in psus:
            p.controller._manual_apply_worker_running = False
            for c in p.channels:
                c.monitor = c.value
                c.voltage_setpoint_readback = c.value
        detector_device.time.data.append(time.time())
        detector.values.data.append(r.current())
    scan._pause = pause
    # This fixture exercises the retained Python reference only. Native worker
    # and production entrypoint coverage live in native/worker/tests/mscan_*.rs.
    scan.runScan = scan._run_scan_python_reference
    detector_device.time.data.append(time.time() - .01)
    detector.values.data.append(40.)
    return r


@pytest.fixture
def mscan_continuous(mscan_rig, monkeypatch):
    s = mscan_rig.scan
    clock = NS(now=0., epoch=mscan_rig.detector_device.time.data[-1], jump=0.)
    mscan_rig.detector_device.interval = 2
    mscan_rig.detector_device.time.data = [clock.epoch - .004, clock.epoch - .002, clock.epoch]
    mscan_rig.detector.values.data = [40., 40., 40.]
    clock.wall = lambda: clock.epoch + clock.now + clock.jump
    monkeypatch.setattr(mscan_rig.module, 'time', NS(time=clock.wall, monotonic=lambda: clock.now))
    s.scan_mode, s.time_step_s, s.sweep_rate = s.CONTINUOUS, .04, 250.
    s._plan = s._preflight()
    r = NS(rig=mscan_rig, scan=s, clock=clock, busy=lambda: False, record=lambda: True,
           sample=lambda: mscan_rig.current(), tick=lambda: None)
    def pause(seconds=None):
        r.tick()
        if s._cancel.is_set():
            raise mscan_rig.module.ScanStopped('Scan stopped.')
        clock.now += s.POLL_S if seconds is None else max(seconds, 1e-8)
        if not r.busy():
            for p in mscan_rig.psus:
                p.controller._manual_apply_worker_running = False
                for c in p.channels:
                    c.monitor = c.value
                    c.voltage_setpoint_readback = c.value
        if r.record():
            mscan_rig.detector_device.time.data.append(clock.wall())
            mscan_rig.detector.values.data.append(r.sample())
    s._pause = pause
    return r


def run(r):
    r.scan.runScan(lambda: True)
    return r.scan._validation
