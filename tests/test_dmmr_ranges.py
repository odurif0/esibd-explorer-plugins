"""Range commands/readbacks and paired current samples on a simulated DMMR."""

import numpy as np
import pytest



@pytest.mark.parametrize("fixed", range(5))
def test_on_applies_each_module_choice_then_verifies_it(dmmr_range_rig, fixed):
    dmmr_range_rig.channels[1]._requested_range_mode = str(fixed)
    dmmr_range_rig.controller.toggleOn()
    assert dmmr_range_rig.controller.acquiring
    expected = [
        ("enable", True), ("automatic", False),
        ("range_readback", 0), ("range_readback", 3),
        ("auto_range", 3, False), ("range_readback", 3),
    ]
    if fixed != 4:
        expected += [("fixed_range", 3, fixed), ("range_readback", 3)]
    assert dmmr_range_rig.hardware.calls[:len(expected)] == expected
    assert ("auto_range", 0, True) not in dmmr_range_rig.hardware.calls
    assert dmmr_range_rig.hardware.calls.count(("fixed_range", 3, fixed)) == int(fixed != 4)
    assert dmmr_range_rig.channels[1]._requested_range_mode == str(fixed)
    assert dmmr_range_rig.hardware.modes == {0: True, 3: False}
    # An automatic range can legitimately differ from the fixed module's range.
    assert dmmr_range_rig.hardware.ranges == {0: 2, 3: fixed}


@pytest.mark.parametrize("failure", ["auto_ack", "range_ack", "read_status", "read_exception",
                                    "wrong_auto", "wrong_range", "invalid_range"])


def test_unconfirmed_range_aborts_on_and_verifies_off(dmmr_range_rig, failure):
    dmmr_range_rig.hardware.failure = failure
    dmmr_range_rig.channels[1]._requested_range_mode = '0'  # A real change, not a redundant write.
    dmmr_range_rig.controller.toggleOn()
    assert not dmmr_range_rig.controller.acquiring
    assert not dmmr_range_rig.hardware.enabled
    assert not dmmr_range_rig.hardware.automatic
    assert not dmmr_range_rig.parent.on
    assert dmmr_range_rig.controller.errorCount == 1
    assert "DMMR acquisition enabled." not in dmmr_range_rig.messages
    assert any("Failed to toggle" in message for message in dmmr_range_rig.messages)


def test_invalid_saved_choice_is_not_silently_changed_to_auto(dmmr_range_rig):
    dmmr_range_rig.channels[0]._requested_range_mode = "5"
    dmmr_range_rig.controller.toggleOn()
    assert not dmmr_range_rig.controller.acquiring
    assert any("Invalid range mode" in message for message in dmmr_range_rig.messages)
    assert not any(call[0] == "auto_range" for call in dmmr_range_rig.hardware.calls)


def test_missing_range_api_is_not_treated_as_success(dmmr_range_rig):
    dmmr_range_rig.hardware.get_module_meas_range = None
    dmmr_range_rig.controller.toggleOn()
    assert not dmmr_range_rig.controller.acquiring
    assert not dmmr_range_rig.hardware.enabled


def test_poll_keeps_range_from_the_same_reply_not_the_requested_range(dmmr_range_rig):
    dmmr_range_rig.controller.acquiring = True
    dmmr_range_rig.controller.readNumbers()
    values, ranges, token = dmmr_range_rig.controller.measurementSnapshot()
    assert values == {0: 0.534e-12, 3: -2e-12}
    assert ranges == {0: 0, 3: 4}
    assert not any(call[0] == "range_readback" for call in dmmr_range_rig.hardware.calls)
    dmmr_range_rig.controller.updateValues()
    assert dmmr_range_rig.channels[0].monitor == values[0]
    assert dmmr_range_rig.channels[0].measurement_range == 0
    assert dmmr_range_rig.channels[0]._measurement_token is token
    # A range transition is kept, not silently subtracted or discarded.
    dmmr_range_rig.hardware.samples[0] = (0, 1e-12, 1)
    dmmr_range_rig.controller.readNumbers()
    assert dmmr_range_rig.controller.meas_ranges[0] == 1
    assert dmmr_range_rig.controller.values[0] == 1e-12
    assert dmmr_range_rig.controller.measurementSnapshot()[2] is not token


# Transport receive faults invalidate the whole poll and are covered by
# test_dmmr_transient_reads; malformed individual samples stay module-local.


@pytest.mark.parametrize("bad", [(0, np.nan, 0), (0, np.inf, 0),
                                 (0, 1e-12, 5), (0, 1e-12, -1), (0, 1e-12, True),
                                 TimeoutError("busy")])


def test_bad_sample_invalidates_both_current_and_range_without_losing_other_modules(dmmr_range_rig, bad):
    dmmr_range_rig.controller.acquiring = True
    dmmr_range_rig.controller.readNumbers()
    dmmr_range_rig.hardware.samples[0] = bad
    dmmr_range_rig.controller.readNumbers()
    assert np.isnan(dmmr_range_rig.controller.values[0])
    assert np.isnan(dmmr_range_rig.controller.meas_ranges[0])
    assert dmmr_range_rig.controller.values[3] == -2e-12
    assert dmmr_range_rig.controller.meas_ranges[3] == 4


def test_off_and_muting_clear_live_range_as_well_as_current(dmmr_range_rig):
    dmmr_range_rig.controller.acquiring = True
    dmmr_range_rig.controller.readNumbers()
    dmmr_range_rig.controller.updateValues()
    dmmr_range_rig.channels[0].enabled = False
    dmmr_range_rig.controller.updateValues()
    assert np.isnan(dmmr_range_rig.channels[0].monitor)
    assert np.isnan(dmmr_range_rig.channels[0].measurement_range)
    assert dmmr_range_rig.channels[1].measurement_range == 4
    dmmr_range_rig.parent.on = False
    dmmr_range_rig.controller.updateValues()
    assert all(np.isnan(channel.measurement_range) for channel in dmmr_range_rig.channels)


def test_reset_invalidates_both_parts_of_the_snapshot(dmmr_range_rig):
    dmmr_range_rig.controller.acquiring = True
    dmmr_range_rig.controller.readNumbers()
    dmmr_range_rig.controller.initializeValues(reset=True)
    values, ranges, _ = dmmr_range_rig.controller.measurementSnapshot()
    assert all(np.isnan(value) for value in values.values())
    assert all(np.isnan(value) for value in ranges.values())
