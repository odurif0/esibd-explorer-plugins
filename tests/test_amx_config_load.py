"""A saved AMX config owns its timing until the operator explicitly edits it."""
from __future__ import annotations

from pathlib import Path

import pytest
from support.amx import FOLDERS, PRESETS, make_case

ROOT = Path(__file__).resolve().parents[1]
# 79 is the classic manufacturer's example; 12 is deliberately custom and
# unrelated to its name. Never infer timing from a slot number or label.


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
