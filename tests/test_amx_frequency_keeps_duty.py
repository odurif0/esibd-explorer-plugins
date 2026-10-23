"""A frequency edit keeps each pulser's duty cycle and delay as a share of the period.

2026-10-22 lab: config 79 (500 kHz, P0 high 100 of 200 ticks, all outputs symmetric)
moved by hand to 450 kHz kept P0 at 1.00 µs in a 2.22 µs period: 45 % / 55 %.
"""
from __future__ import annotations

import pytest
from support.amx import FOLDERS, make_case


def loaded_79(folder):
    module, parent, controller = make_case(folder)
    controller.loadOperatingConfigNow()  # Adopts 500 kHz, P0 1.00 µs, Manual.
    assert parent.frequency_khz == 500. and parent.channels[0].value == pytest.approx(1.)
    controller.device.calls.clear()
    return module, parent, controller


def change_frequency(parent, controller, khz):
    parent.frequency_khz = khz
    parent._follow_timing_basis()
    controller.applyGlobalSettings()
    controller._apply_snapshot(controller.device.collect_housekeeping())


@pytest.mark.parametrize("folder", FOLDERS)
def test_config79_stays_symmetric_at_450_khz(folder):
    _, parent, controller = loaded_79(folder)
    hw = controller.device
    change_frequency(parent, controller, 450.)
    assert hw.period + 2 == 222
    assert hw.widths[0] + 2 == 111  # half of 222 ticks
    assert parent.channels[0].value == pytest.approx(1.11)
    assert hw.delays[0] + 3 == round(10 * 222 / 200)  # P0's phase stays 5 % of the period
    # A longer period first, so no pulse ever overruns the old one.
    assert [call[0] for call in hw.calls if call[0] != "read"][:2] == ["frequency", "width"]
    assert hw.calls.index(("delay", 0, hw.delays[0])) > hw.calls.index(("width", 0, hw.widths[0]))


@pytest.mark.parametrize("folder", FOLDERS)
def test_a_shorter_period_is_written_after_the_shorter_pulses(folder):
    _, parent, controller = loaded_79(folder)
    hw = controller.device
    change_frequency(parent, controller, 600.)  # 167 ticks
    assert hw.widths[0] + 2 == round(100 * 167 / 200)
    writes = [call for call in hw.calls if call[0] in ("frequency", "width", "delay")]
    assert writes[0] == ("delay", 0, round(10 * 167 / 200) - 3)
    assert writes[-1] == ("frequency", 600.)


@pytest.mark.parametrize("folder", FOLDERS)
def test_going_back_restores_the_loaded_registers(folder):
    _, parent, controller = loaded_79(folder)
    hw = controller.device
    for khz in (450., 433., 600., 500.):
        change_frequency(parent, controller, khz)
    assert (hw.period, hw.widths[0], hw.delays[0]) == (198, 98, 7)


@pytest.mark.parametrize("folder", FOLDERS)
def test_keep_us_keeps_width_and_delay(folder):
    _, parent, controller = loaded_79(folder)
    parent.channels[0].keep = "width"
    change_frequency(parent, controller, 450.)
    hw = controller.device
    assert parent.channels[0].value == pytest.approx(1.)
    assert (hw.widths[0], hw.delays[0]) == (98, 7)


@pytest.mark.parametrize("folder", FOLDERS)
def test_equation_width_is_left_to_its_equation(folder):
    _, parent, controller = loaded_79(folder)
    parent.channels[0].active = False
    parent.frequency_khz = 450.
    parent._follow_timing_basis()
    assert parent.channels[0].value == pytest.approx(1.)


def external_trigger(controller, folder):
    if folder == "amx_hd":
        controller.hd_routing = {"timer_triggers": {0: 56}}  # Din0
    else:
        controller.pulser_triggers = {0: 3}  # DIN0


@pytest.mark.parametrize("folder", FOLDERS)
def test_pulser_not_triggered_by_the_oscillator_is_left_alone(folder):
    _, parent, controller = loaded_79(folder)
    external_trigger(controller, folder)
    parent.frequency_khz = 450.
    parent._follow_timing_basis()
    controller.applyGlobalSettings()
    hw = controller.device
    assert parent.channels[0].value == pytest.approx(1.)
    assert (hw.widths[0], hw.delays[0]) == (98, 7)


@pytest.mark.parametrize("folder", FOLDERS)
@pytest.mark.parametrize("basis", (None, "loading"))
def test_loaded_or_unknown_basis_is_not_rescaled(folder, basis):
    _, parent, _ = loaded_79(folder)
    if basis is None:
        del parent._timing_basis_khz
    else:
        parent.loading = True
    parent.frequency_khz = 450.
    parent._follow_timing_basis()
    assert parent.channels[0].value == pytest.approx(1.)
    assert parent._timing_basis_khz == 450.


@pytest.mark.parametrize("folder", FOLDERS)
def test_requested_duty_uses_the_clock_grid(folder):
    module, parent, _ = loaded_79(folder)
    parent.frequency_khz = 430.  # 233 ticks: 50 % is not representable
    assert module._requested_duty_percent(parent, 1.16) == pytest.approx(116 / 233 * 100)
