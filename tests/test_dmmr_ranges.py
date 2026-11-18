"""The live current and the range it was measured in come from the same poll of the worker.

Range setting at ON and its readbacks run in the DMMR's native worker (native/worker/tests/dmmr_facade.rs,
tests/test_native_dmmr_adapter.py, tests/test_dmmr_toggle_failures.py).
"""
import numpy as np


def test_poll_keeps_range_from_the_same_reply_not_the_requested_range(native_rig):
    native_rig.controller.acquiring = True
    native_rig.controller.readNumbers()
    values, ranges, token = native_rig.controller.measurementSnapshot()
    assert values == {0: 0.534e-12, 3: -2e-12}
    assert ranges == {0: 0, 3: 4}
    assert [name for name, _, _ in native_rig.backend.calls] == ["get_state", "get_device_state",
                                                                "get_voltage_state", "get_temperature_state",
                                                                "poll_currents"]
    native_rig.controller.updateValues()
    assert native_rig.channels[0].monitor == values[0]
    assert native_rig.channels[0].measurement_range == 0
    assert native_rig.channels[0]._measurement_token is token
    # A range transition is kept, not silently subtracted or discarded.
    native_rig.backend.poll_replies.append({"token": 99, "values": {0: 1e-12, 3: -2e-12},
                                            "ranges": {0: 1, 3: 4}, "valid": {0: True, 3: True},
                                            "recovery": None})
    native_rig.controller.readNumbers()
    assert native_rig.controller.meas_ranges[0] == 1
    assert native_rig.controller.values[0] == 1e-12
    assert native_rig.controller.measurementSnapshot()[2] is not token


def test_off_and_muting_clear_live_range_as_well_as_current(native_rig):
    native_rig.controller.acquiring = True
    native_rig.controller.readNumbers()
    native_rig.controller.updateValues()
    native_rig.channels[0].enabled = False
    native_rig.controller.updateValues()
    assert np.isnan(native_rig.channels[0].monitor)
    assert np.isnan(native_rig.channels[0].measurement_range)
    assert native_rig.channels[1].measurement_range == 4
    native_rig.parent.on = False
    native_rig.controller.updateValues()
    assert all(np.isnan(channel.measurement_range) for channel in native_rig.channels)


def test_reset_invalidates_both_parts_of_the_snapshot(native_rig):
    native_rig.controller.acquiring = True
    native_rig.controller.readNumbers()
    native_rig.controller.initializeValues(reset=True)
    values, ranges, _ = native_rig.controller.measurementSnapshot()
    assert all(np.isnan(value) for value in values.values())
    assert all(np.isnan(value) for value in ranges.values())
