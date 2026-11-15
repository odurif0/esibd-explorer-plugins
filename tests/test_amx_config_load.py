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


def _remembering_case(folder, tmp_path):
    """A case whose plugin has Explorer's configuration folder, where the last timing is kept."""
    from types import SimpleNamespace
    module, parent, controller = make_case(folder)
    parent.pluginManager = SimpleNamespace(Settings=SimpleNamespace(configPath=str(tmp_path)))
    parent.com = 11
    messages = []
    controller.print = parent.print = lambda text, **kwargs: messages.append(text)
    return module, parent, controller, messages


def _edit_frequency(parent, controller, frequency_khz):
    """The operator's frequency edit: pulsers keeping % follow it, then it is written."""
    previous, parent.frequency_khz = parent.frequency_khz, frequency_khz
    parent._keep_duty_cycles(previous, frequency_khz)
    controller.applyGlobalSettings()


@pytest.mark.parametrize("folder", FOLDERS)
def test_on_brings_back_the_timing_last_set_on_the_config(folder, tmp_path):
    """User, 2026-11-14: a frequency set by hand was lost at every start; the config's own came back."""
    _, parent, controller, _ = _remembering_case(folder, tmp_path)
    controller.toggleOn()  # Config 79: 500 kHz, P0 high 100 of 200 ticks, delay 7 + 3 ticks.
    _edit_frequency(parent, controller, 300.)  # 333 ticks: P0 166, delay round(10 * 333 / 200) = 17.
    hw = controller.device
    assert (hw.period, hw.widths[0], hw.delays[0]) == (331, 164, 14)
    file = tmp_path / f"{parent.name}_last_timing.json"
    assert file.is_file()

    # A new Explorer: another controller and device, the same configuration folder.
    _, parent, controller, messages = _remembering_case(folder, tmp_path)
    controller.toggleOn()
    hw = controller.device
    assert hw.calls[:2] == [("enable", True), ("load", 79)] or hw.calls[0] == ("load", 79), hw.calls
    assert (hw.period, hw.widths[0], hw.delays[0]) == (331, 164, 14), "as the operator left it"
    assert parent.frequency_khz == 300., "the value typed, not 300.300 computed from the period"
    assert parent.channels[0].value == pytest.approx(1.66) and parent.channels[0].enabled is True
    assert parent._timing_edited is True
    assert any("restored: oscillator 300 kHz (the config holds 500 kHz)" in text for text in messages), messages
    # The width keeps its origin: back at 500 kHz it is 100 of 200 ticks again, not round(166 * 200 / 333).
    _edit_frequency(parent, controller, 500.)
    assert (hw.period, hw.widths[0], hw.delays[0]) == (198, 98, 7)

    # Load now returns to the config as saved, and the next ON has nothing to bring back.
    _edit_frequency(parent, controller, 400.)
    controller.loadOperatingConfigNow()
    assert hw.period == 198 and parent.frequency_khz == 500. and parent._timing_edited is False
    _, parent, controller, messages = _remembering_case(folder, tmp_path)
    controller.toggleOn()
    assert controller.device.timing_writes == [] and not any("restored" in text for text in messages)


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("change", ("config", "port"))
def test_a_timing_set_on_another_config_or_device_is_not_brought_back(folder, tmp_path, change):
    _, parent, controller, _ = _remembering_case(folder, tmp_path)
    controller.toggleOn()
    _edit_frequency(parent, controller, 300.)
    _, parent, controller, messages = _remembering_case(folder, tmp_path)
    if change == "config":
        parent.operating_config = 12
    else:
        parent.com = 12
    controller.toggleOn()
    hw = controller.device
    assert hw.timing_writes == [] and hw.period == PRESETS[parent.operating_config][0]
    assert not any("restored" in text for text in messages)


@pytest.mark.parametrize("folder", FOLDERS)
def test_a_stopped_pulser_and_its_width_come_back(folder, tmp_path):
    _, parent, controller, _ = _remembering_case(folder, tmp_path)
    parent.operating_config = 12  # P0 3.00 µs and P1 5.10 µs in 8 µs.
    controller.toggleOn()
    parent.channels[1].enabled = False
    controller.applyValue(parent.channels[1])
    assert controller.device.widths[1] == 0
    _, parent, controller, _ = _remembering_case(folder, tmp_path)
    parent.operating_config = 12
    controller.toggleOn()
    assert controller.device.widths[:2] == [298, 0]
    assert parent.channels[1].enabled is False and parent.channels[1].value == pytest.approx(5.1)


@pytest.mark.parametrize("folder", FOLDERS)
def test_a_restore_that_fails_leaves_the_config_as_saved(folder, tmp_path):
    _, parent, controller, _ = _remembering_case(folder, tmp_path)
    controller.toggleOn()
    _edit_frequency(parent, controller, 300.)
    _, parent, controller, messages = _remembering_case(folder, tmp_path)
    hw = controller.device

    def refused(frequency, timeout_s=None):
        raise RuntimeError("SetOscillatorPeriod failed")
    hw.set_frequency_khz = refused
    controller.toggleOn()
    assert (hw.period, hw.widths[0], hw.delays[0]) == (198, 98, 7), "config 79 as saved"
    assert parent.frequency_khz == 500. and parent._timing_edited is False
    assert any("not restored on config 79" in text and "loaded again as saved" in text for text in messages)
    assert controller.main_state == "STATE_ON"
