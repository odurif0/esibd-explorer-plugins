"""A saved AMX config owns its timing until the operator explicitly edits it."""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("amx_a", "amx_b", "amx_hd")
# 79 is the classic manufacturer's example; 12 is deliberately custom and
# unrelated to its name. Never infer timing from a slot number or label.
PRESETS = {
    79: (198, (98, 0, 0, 0)),
    12: (798, (298, 508, 0, 0)),
}


class RegisterDevice:
    CLOCK = DEF_CLOCK = 100e6
    OSC_OFFSET = PULSER_WIDTH_OFFSET = 2
    PULSER_DELAY_OFFSET = 3

    def __init__(self, count=4):
        self.connected = True
        self.enabled = False
        self.count = count
        self.calls = []
        self.fail_read = False
        self.period = 49998
        self.widths = [9998] * count
        self.config = -1

    def set_device_enabled(self, enabled, timeout_s=None):
        self.enabled = enabled
        self.calls.append(("enable", enabled))

    def load_config(self, index, timeout_s=None):
        self.config = index
        self.period, widths = PRESETS[index]
        self.widths = list(widths[:self.count])
        self.calls.append(("load", index))

    def get_status(self):
        return dict(memory_config=self.config, memory_config_name="Stored signal", memory_config_source="memory")

    def set_frequency_khz(self, frequency, timeout_s=None):
        self.period = round(self.CLOCK / (frequency * 1000) - self.OSC_OFFSET)
        self.calls.append(("frequency", frequency))

    def set_pulser_width_ticks(self, pulser, width, timeout_s=None):
        self.widths[pulser] = width
        self.calls.append(("width", pulser, width))

    def collect_state_snapshot(self, timeout_s=None):
        return dict(device_enabled=self.enabled, main_state={"name": "STATE_ON"},
                    device_state={"flags": []}, controller_state={"flags": []})

    def collect_housekeeping(self, timeout_s=None):
        self.calls.append(("read",))
        if self.fail_read:
            raise RuntimeError("timing read failed")
        return dict(self.collect_state_snapshot(), oscillator={"period": self.period},
                    pulsers=[dict(pulser=p, width_ticks=width, delay_ticks=7 if p == 0 else 0, burst=0)
                             for p, width in enumerate(self.widths)])

    @property
    def timing_writes(self):
        return [call for call in self.calls if call[0] in ("frequency", "width")]


def load_module(folder):
    family = "amx_hd" if folder == "amx_hd" else "amx"
    stub = "test_amx_hd_plugin_packaging" if family == "amx_hd" else "test_amx_plugin_behavior"
    importlib.import_module(stub)._install_esibd_stubs()
    spec = importlib.util.spec_from_file_location(f"config_load_{folder}", ROOT / folder / f"{family}_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_case(folder):
    module = load_module(folder)
    hd = folder == "amx_hd"
    cls = module.AMXHDDevice if hd else module.AMXDevice
    parent = cls.__new__(cls)
    parent.loading = False
    parent.frequency_khz = 2.
    parent.operating_config = 79
    parent.connect_timeout_s = parent.poll_timeout_s = parent.startup_timeout_s = 1.
    parent.isOn = lambda: True
    parent.print = lambda *args, **kwargs: None
    parent._sync_acquisition_controls = lambda: None
    parent.channels = [SimpleNamespace(
        real=True, enabled=True, active=False, equation="123", value=100., id=p,
        pulser_number=lambda p=p: p, getParameterByName=lambda _: None,
        setWidthText=lambda _: None, setDelayText=lambda _: None, setBurstText=lambda _: None,
        setDutyText=lambda _: None, setFreqText=lambda _: None,
    ) for p in range(2 if hd else 4)]
    parent.getChannels = lambda: parent.channels
    cls = module.AMXHDController if hd else module.AMXController
    controller = parent.controller = cls(parent)
    controller.lock = Lock()
    controller.errorCount = 0
    controller.print = parent.print
    controller.device = RegisterDevice(count=len(parent.channels))
    controller.initialized = True
    controller.available_configs = [dict(index=index, name="Stored signal", active=True, valid=True) for index in PRESETS]
    controller._restart_acquisition_after_transition = lambda: None
    return module, parent, controller


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("action", ("toggleOn", "loadOperatingConfigNow"))
@pytest.mark.parametrize("index", PRESETS)
def test_load_adopts_saved_timing_without_any_timing_write(folder, action, index):
    _, parent, controller = make_case(folder)
    parent.operating_config = index
    getattr(controller, action)()
    hw = controller.device
    period, widths = PRESETS[index]
    assert hw.timing_writes == [], hw.calls
    assert hw.period == period
    assert hw.widths == list(widths[:hw.count])
    assert parent.frequency_khz == 100e3 / (period + 2)
    for channel, width in zip(parent.channels, widths):
        assert channel.value == pytest.approx((width + 2) / 100 if width else 0.)
        assert channel.enabled is (width > 0)
        assert channel.active is True  # an old equation must not undo the load
        assert channel.equation == "123"  # retained for later explicit reuse
    # Explicit subsequent edits still work; reloading restores the preset.
    parent.channels[0].value = .5
    controller.applyValue(parent.channels[0])
    assert hw.widths[0] == 48
    parent.frequency_khz = 250.
    controller.applyGlobalSettings()
    assert hw.period == 398
    hw.calls.clear()
    controller.loadOperatingConfigNow()
    assert hw.timing_writes == []
    assert hw.period == period
    assert parent.frequency_khz == 100e3 / (period + 2)


@pytest.mark.parametrize("folder", FOLDERS)
def test_pending_gui_readback_blocks_old_setpoints_and_is_cancelled_on_disconnect(folder):
    module, parent, controller = make_case(folder)
    callbacks = []
    module._invoke_gui_callback = callbacks.append
    controller.loadOperatingConfigNow()
    hw = controller.device
    assert hw.timing_writes == []
    assert parent.frequency_khz == 2.
    # Transition has ended on the worker, but Qt has not delivered the new values.
    controller.applyGlobalSettings()
    controller.applyValue(parent.channels[0])
    assert hw.timing_writes == []
    controller._discard_pending_runtime_applies()
    controller.device = None
    for callback in callbacks:
        callback()
    assert parent.frequency_khz == 2., "A stale callback from the disconnected device changed settings"


@pytest.mark.parametrize("folder", FOLDERS)
def test_failed_readback_does_not_apply_old_gui_settings(folder):
    _, parent, controller = make_case(folder)
    controller.device.fail_read = True
    with pytest.raises(RuntimeError, match="timing read failed"):
        controller._load_operating_config_and_enable_device(
            config_index=79, timeout_s=1., load_config_first=True)
    assert controller.device.timing_writes == []
    assert parent.frequency_khz == 2.
    assert parent.channels[0].value == 100.
