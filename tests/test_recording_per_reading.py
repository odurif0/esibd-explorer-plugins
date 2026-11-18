"""ESI and TPG366 readings are recorded once, at the time they were read; no lag guard.

Explorer 1.0.2 records a device from a timer of its Qt thread, with the time of the call, and pauses or
closes it when that thread is late (2026-11-17: ESI 11 s behind, DMMR closed at 13:40).
"""
from __future__ import annotations

import threading
from collections import deque
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from explorer_host import compile_definition
from support.esi import load_plugin as load_esi


@pytest.fixture
def host():
    try:
        root = Path(distribution("esibd-explorer").locate_file("esibd"))
    except PackageNotFoundError:
        pytest.skip("Installed Explorer sources unavailable")
    ns = {"np": np, "INOUT": SimpleNamespace(IN=1, OUT=2)}
    exec(compile_definition(root / "core.py", "DynamicNp"), ns)
    exec(compile_definition(root / "core.py", "appendValue", "Channel"), ns)
    return SimpleNamespace(DynamicNp=ns["DynamicNp"], appendValue=ns["appendValue"])


def _esi(module, host, recording=True):
    class Channel:
        appendValue = host.appendValue

        def __init__(self, name, address, current):
            self.name, self.address, self.function = name, address, "HV current" if current else "HV"
            self.enabled = self.real = self.useMonitors = True
            self.useBackgrounds, self.inout, self.value, self.monitor = False, 1, np.nan, np.nan
            self.values = host.DynamicNp(max_size=1000)

        def module_address(self):
            return self.address

        def getRecordedParameters(self):
            return []

    channels = [Channel("ESI_HV1", 1, False), Channel("ESI_HV1_I", 1, True)]
    device = object.__new__(module.ESIDevice)
    device.time = host.DynamicNp(max_size=1000, dtype=np.float64)
    device.getChannels = lambda: channels
    device.plotableChannels = True
    device.updateValues = lambda: None
    device.liveDisplayActive = lambda: False
    device.recording = recording
    controller = object.__new__(module.ESIController)
    controller.controllerParent = device
    controller.values, controller.currents = {}, {}
    controller._pending_samples = deque(maxlen=module._ESI_PENDING_SAMPLES)
    controller._samples_lock = threading.Lock()
    controller._samples_dropped = 0
    controller.print = lambda *a, **k: None
    device.controller = controller
    return device, controller, channels


def _read(controller, stamp, volts, amps):
    controller.values, controller.currents = {1: volts}, {1: amps}
    controller._publish_sample(stamp)


def test_esi_readings_become_one_row_each_at_their_own_time(host):
    module = load_esi()
    device, controller, (hv, current) = _esi(module, host)
    _read(controller, 100.0, 1000., 4e-8)
    _read(controller, 101.0, 1001., 5e-8)  # two readings before Explorer's timer comes
    module.ESIDevice.appendData(device, False, False)
    module.ESIDevice.appendData(device, False, False)  # no new reading: no row
    _read(controller, 102.0, 1002., np.nan)  # a current that was not valid stays NaN, alone
    module.ESIDevice.appendData(device, False, False)
    assert device.time.get().tolist() == [100.0, 101.0, 102.0]
    assert hv.values.get().tolist() == [1000., 1001., 1002.]
    amps = current.values.get()
    assert amps[:2].tolist() == pytest.approx([4e-8, 5e-8]) and np.isnan(amps[2])
    assert hv.monitor == 1002., "the latest reading is on screen"


def test_esi_is_not_queued_while_not_recording_and_never_paused_for_a_late_display(host):
    module = load_esi()
    device, controller, _ = _esi(module, host, recording=False)
    _read(controller, 100.0, 1000., 4e-8)
    assert controller.takeSamples() == []
    device.lastIntervalTime, device.lagging = 0., 300
    device.closeCommunication = lambda: pytest.fail("a late display closed the ESI")
    assert module.ESIDevice.bufferLagging(device) is False
    module.ESIDevice.measureInterval(device, reset=False)
    assert device.lagging == 0 and device.lagging_seconds == 0


def test_tpg366_is_never_paused_or_closed_for_a_late_display():
    from support.explorer import load_plugin
    module = load_plugin("tpg366/tpg366_plugin.py", "tpg366_lag_test", runtimes=("tpg366",))
    device = object.__new__(module.TPG366)
    device.lastIntervalTime, device.lagging = 0., 300
    device.closeCommunication = lambda: pytest.fail("a late display closed the TPG366")
    assert device.bufferLagging() is False
    device.measureInterval(reset=False)
    assert device.lagging == 0 and device.lagging_seconds == 0
