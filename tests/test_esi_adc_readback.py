"""ESI voltage ADC on the electrospray connector: a readback only, shown and recorded when fresh.

User request (2026-10-08): the ADC stays on the electrospray connector (POS/NEG), switchable on
the card during an experiment; the displayed value must be the measured voltage, reliably.
"""
from types import SimpleNamespace

import numpy as np
import pytest

from test_esi_current_measurements import rig, snapshot


class FakeDevice:
    def __init__(self, calls):
        self.calls = calls

    def select_hv_voltage_adc(self, address, *, negative, timeout_s):
        self.calls.append((address, negative))


def adc_rig(monkeypatch, connector="POS"):
    module, channels, controller = rig()
    controller.controllerParent.hv1_adc_connector = connector
    controller.controllerParent.interval = 1000
    controller.main_state = "STATE_ON"
    calls = []
    controller.device = FakeDevice(calls)
    # Run the operator's switch synchronously.
    monkeypatch.setattr(module, "Thread", lambda target, daemon: SimpleNamespace(start=target))
    return module, controller, calls


def poll(controller, at, *, fresh=True, volts=1000., polarity="positive", valid=True):
    data = snapshot()
    data["modules"][1].update(measured_v=volts, voltage_valid=valid,
                              measurement={"voltage_polarity": polarity, "voltage_fresh": fresh})
    controller._apply_snapshot(data, observed_at=at)
    return controller.values[1], controller.adc_readback[1]["state"]


def test_after_a_switch_conversions_are_discarded_until_settled(monkeypatch):
    _module, controller, calls = adc_rig(monkeypatch)
    controller._select_voltage_adc(1)
    start = controller._adc_selected_at[1]

    assert calls == [(1, False)]
    assert poll(controller, start + .2)[1] == "settling"
    # Two conversions discarded, but still within the settling time.
    assert poll(controller, start + .5)[1] == "settling"
    value, state = poll(controller, start + 1.2, volts=1001.)
    assert (value, state) == (1001., "ok")


def test_a_value_without_new_conversion_ages_then_is_neither_shown_nor_recorded(monkeypatch):
    _module, controller, _calls = adc_rig(monkeypatch)
    assert poll(controller, 100.) == (1000., "ok")  # No switch by this controller: settled.
    # Same conversion still current within the stale limit (3 s at a 1 s interval).
    assert poll(controller, 102., fresh=False, volts=-5.) == (1000., "ok")
    value, state = poll(controller, 103.5, fresh=False, volts=-5.)
    assert np.isnan(value) and state == "stale"
    assert controller.adc_readback[1]["value"] == 1000.  # Shown as stale, never as current.


@pytest.mark.parametrize("polarity,valid,state", [
    ("negative", True, "mismatch"), (None, True, "mismatch"), ("positive", False, "invalid")])
def test_wrong_connector_or_invalid_reading_is_never_shown_as_the_electrospray_voltage(
        monkeypatch, polarity, valid, state):
    _module, controller, _calls = adc_rig(monkeypatch)
    assert poll(controller, 100.)[1] == "ok"
    value, observed = poll(controller, 101., polarity=polarity, valid=valid)
    assert np.isnan(value) and observed == state
    # The earlier value is forgotten: a later read without a new conversion cannot revive it.
    value, observed = poll(controller, 101.5, fresh=False)
    assert np.isnan(value) and observed == "waiting"


def test_negative_connector_reads_negative_volts(monkeypatch):
    _module, controller, _calls = adc_rig(monkeypatch, connector="NEG")
    assert poll(controller, 100., polarity="negative", volts=-998.)[0] == -998.
    assert poll(controller, 101., polarity="positive")[1] == "mismatch"


def test_operator_switch_is_a_measurement_command_never_during_the_off_check(monkeypatch):
    _module, controller, calls = adc_rig(monkeypatch, connector="NEG")
    for state in ("Stopping: checking HV", "Shutdown unconfirmed"):
        controller.main_state = state
        controller.selectVoltageAdcFromThread(1)
    assert calls == []
    controller.main_state = "STATE_ON"
    controller.selectVoltageAdcFromThread(1)
    assert calls == [(1, True)]
    controller._output_cancel.set()  # OFF requested: a queued switch is dropped.
    controller.selectVoltageAdcFromThread(1)
    assert calls == [(1, True)]


def test_failed_switch_reports_and_leaves_the_readback_unconfirmed(monkeypatch):
    _module, controller, _calls = adc_rig(monkeypatch, connector="NEG")
    printed = []
    controller.print = lambda message, **_kwargs: printed.append(message)

    def fail(*_args, **_kwargs):
        raise RuntimeError("verification failed")

    controller.device.select_hv_voltage_adc = fail
    controller._select_voltage_adc(1)
    assert controller.errorCount == 1 and "HV1 voltage ADC not switched to NEG" in printed[0]
    assert poll(controller, controller._adc_selected_at[1] + 5.)[1] == "mismatch"


def test_config_load_points_the_adc_back_at_the_electrospray_connectors(monkeypatch):
    _module, controller, calls = adc_rig(monkeypatch)
    controller.controllerParent.hv2_adc_connector = "NEG"
    controller.controllerParent.operating_config = 3
    controller.controllerParent.name = "ESI"
    loaded = []
    controller.device.load_config = lambda index, timeout_s: loaded.append(index)
    controller.loadOperatingConfigNow()
    assert loaded == [3] and calls == [(1, False), (2, True)]


@pytest.mark.parametrize("readback,expected", [
    (None, "n/a"),
    ({"state": "ok", "requested": "POS", "value": 1000.04}, "POS 1000.0 V"),
    ({"state": "ok", "requested": "NEG", "value": -998.}, "NEG -998.0 V"),
    ({"state": "mismatch", "requested": "POS", "selected": "NEG"}, "ADC on NEG, POS requested"),
    ({"state": "mismatch", "requested": "POS", "selected": None}, "ADC on unknown, POS requested"),
    ({"state": "invalid", "requested": "POS"}, "POS invalid"),
    ({"state": "stale", "requested": "POS", "value": 1000.}, "POS stale (1000.0 V)"),
    ({"state": "settling", "requested": "NEG", "value": np.nan}, "NEG settling…"),
    ({"state": "waiting", "requested": "POS", "value": np.nan}, "POS waiting for a reading"),
])
def test_panel_text_names_the_connector_and_never_hides_a_problem(readback, expected):
    module = rig()[0]
    assert module._adc_readback_text(readback)[0] == expected
