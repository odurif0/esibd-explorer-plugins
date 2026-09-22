"""Range commands/readbacks and paired current samples on a simulated DMMR."""
from types import SimpleNamespace

import numpy as np
import pytest

from test_dmmr_acquisition_regressions import Device as PollingDevice
from test_dmmr_plugin_behavior import _load_module


class RangeDevice(PollingDevice):
    def __init__(self):
        super().__init__()
        self.modes = {0: True, 3: True}
        self.ranges = {0: 2, 3: 4}
        self.failure = None
        self.samples = {0: (0, 0.534e-12, 0), 3: (0, -2e-12, 4)}

    def set_module_auto_range(self, address, enabled, **kwargs):
        status = super().set_module_auto_range(address, enabled, **kwargs)
        self.modes[address] = enabled
        return -12 if self.failure == "auto_ack" else status

    def set_module_meas_range(self, address, value, **kwargs):
        self.calls.append(("fixed_range", address, value))
        self.ranges[address] = value
        return -12 if self.failure == "range_ack" else 0

    def get_module_meas_range(self, address, **kwargs):
        self.calls.append(("range_readback", address))
        if self.failure == "read_exception":
            raise RuntimeError("range read failed")
        return (
            -12 if self.failure == "read_status" else 0,
            5 if self.failure == "invalid_range" else 2 if self.failure == "wrong_range" else self.ranges[address],
            not self.modes[address] if self.failure == "wrong_auto" else self.modes[address],
        )

    def get_module_current(self, address, **kwargs):
        self.calls.append(("current", address))
        sample = self.samples[address]
        if isinstance(sample, Exception):
            raise sample
        return sample


@pytest.fixture
def rig():
    module = _load_module()
    channels = [SimpleNamespace(real=True, enabled=True, monitor=np.nan,
                                _requested_range_mode=mode, module_address=lambda a=a: a)
                for a, mode in ((0, "Auto"), (3, "4"))]
    parent = SimpleNamespace(connect_timeout_s=0.1, poll_timeout_s=0.1,
                             on=True, getChannels=lambda: channels,
                             getConfiguredModules=lambda: [0, 3])
    parent.isOn = lambda: parent.on
    parent._set_on_ui_state = lambda value: setattr(parent, "on", value)
    controller = module.DMMRController(parent)
    controller.device = hardware = RangeDevice()
    controller.initialized = True
    controller.detected_module_ids = [0, 3]
    messages = []
    controller.print = lambda message, **kw: messages.append(message)
    return SimpleNamespace(module=module, controller=controller, hardware=hardware,
                           channels=channels, parent=parent, messages=messages)


@pytest.mark.parametrize("fixed", range(5))
def test_on_applies_each_module_choice_then_verifies_it(rig, fixed):
    rig.channels[1]._requested_range_mode = str(fixed)
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    assert rig.hardware.calls[:7] == [
        ("enable", True), ("automatic", False),
        ("auto_range", 0, True), ("range_readback", 0),
        ("auto_range", 3, False), ("fixed_range", 3, fixed), ("range_readback", 3),
    ]
    assert rig.channels[1]._requested_range_mode == str(fixed)
    assert rig.hardware.modes == {0: True, 3: False}
    # An automatic range can legitimately differ from the fixed module's range.
    assert rig.hardware.ranges == {0: 2, 3: fixed}


@pytest.mark.parametrize("failure", ["auto_ack", "range_ack", "read_status", "read_exception",
                                    "wrong_auto", "wrong_range", "invalid_range"])
def test_unconfirmed_range_aborts_on_and_verifies_off(rig, failure):
    rig.hardware.failure = failure
    rig.controller.toggleOn()
    assert not rig.controller.acquiring
    assert not rig.hardware.enabled
    assert not rig.hardware.automatic
    assert not rig.parent.on
    assert rig.controller.errorCount == 1
    assert "DMMR acquisition enabled." not in rig.messages
    assert any("Failed to toggle" in message for message in rig.messages)


def test_invalid_saved_choice_is_not_silently_changed_to_auto(rig):
    rig.channels[0]._requested_range_mode = "5"
    rig.controller.toggleOn()
    assert not rig.controller.acquiring
    assert any("Invalid range mode" in message for message in rig.messages)
    assert not any(call[0] == "auto_range" for call in rig.hardware.calls)


def test_missing_range_api_is_not_treated_as_success(rig):
    rig.hardware.get_module_meas_range = None
    rig.controller.toggleOn()
    assert not rig.controller.acquiring
    assert not rig.hardware.enabled


def test_poll_keeps_range_from_the_same_reply_not_the_requested_range(rig):
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    values, ranges, token = rig.controller.measurementSnapshot()
    assert values == {0: 0.534e-12, 3: -2e-12}
    assert ranges == {0: 0, 3: 4}
    assert not any(call[0] == "range_readback" for call in rig.hardware.calls)
    rig.controller.updateValues()
    assert rig.channels[0].monitor == values[0]
    assert rig.channels[0].measurement_range == 0
    assert rig.channels[0]._measurement_token is token
    # A range transition is kept, not silently subtracted or discarded.
    rig.hardware.samples[0] = (0, 1e-12, 1)
    rig.controller.readNumbers()
    assert rig.controller.meas_ranges[0] == 1
    assert rig.controller.values[0] == 1e-12
    assert rig.controller.measurementSnapshot()[2] is not token


@pytest.mark.parametrize("bad", [(-12, 1e-12, 2), (0, np.nan, 0), (0, np.inf, 0),
                                 (0, 1e-12, 5), (0, 1e-12, -1), (0, 1e-12, True),
                                 TimeoutError("busy")])
def test_bad_sample_invalidates_both_current_and_range_without_losing_other_modules(rig, bad):
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    rig.hardware.samples[0] = bad
    rig.controller.readNumbers()
    assert np.isnan(rig.controller.values[0])
    assert np.isnan(rig.controller.meas_ranges[0])
    assert rig.controller.values[3] == -2e-12
    assert rig.controller.meas_ranges[3] == 4


def test_off_and_muting_clear_live_range_as_well_as_current(rig):
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    rig.controller.updateValues()
    rig.channels[0].enabled = False
    rig.controller.updateValues()
    assert np.isnan(rig.channels[0].monitor)
    assert np.isnan(rig.channels[0].measurement_range)
    assert rig.channels[1].measurement_range == 4
    rig.parent.on = False
    rig.controller.updateValues()
    assert all(np.isnan(channel.measurement_range) for channel in rig.channels)


def test_reset_invalidates_both_parts_of_the_snapshot(rig):
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    rig.controller.initializeValues(reset=True)
    values, ranges, _ = rig.controller.measurementSnapshot()
    assert all(np.isnan(value) for value in values.values())
    assert all(np.isnan(value) for value in ranges.values())
