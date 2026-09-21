"""Startup setpoints must be confirmed by readback, just like later edits."""
import math

import pytest

import test_ampr_parallel_ramps as ramps

rig = ramps.rig


def test_each_startup_target_is_confirmed_at_its_own_ramp_completion(rig):
    c = rig.controller
    history = []
    publish = c._publish_setpoint

    def record(request, state, detail):
        publish(request, state, detail)
        history.append((rig.clock.now, request.key, request.state))

    c._publish_setpoint = record
    assert c._begin_transition(True)
    assert set(c._latest_setpoints) == {(2, 1), (2, 2), (2, 3)}
    assert all(request.state == "pending" for request in c._latest_setpoints.values())
    assert not rig.writes, "Tracking a startup target must not send it ahead of its ramp"
    ramps.ramp(rig)
    for key, duration in [((2, 1), 10.), ((2, 2), 20.), ((2, 3), 10.)]:
        confirmations = [time for time, recorded_key, state in history
                         if recorded_key == key and state == "confirmed"]
        assert confirmations and min(confirmations) == pytest.approx(duration)
    assert not any(state == "mismatch" for _, _, state in history), "A partial ramp is not a rejected setpoint"
    assert not c._pending_setpoints
    writes = list(rig.writes)
    c._end_transition()
    assert rig.writes == writes
    for channel in rig.channels:
        assert channel.lastAppliedValue == channel.value
        c._queue_setpoint(channel)  # A global update must not replay the startup voltages.
    assert not c._pending_setpoints and c._setpoint_thread is None
    assert rig.writes == writes


@pytest.mark.parametrize("readback,state", [
    (None, "sent"), (float("nan"), "sent"), (0., "mismatch"),
])
def test_startup_ack_does_not_replace_hardware_confirmation(rig, readback, state):
    c = rig.controller
    read = c.device.get_module_voltages

    def faulty_read(module):
        result = read(module)
        for data in result.values():
            data["setpoint"] = readback
        return result

    c.device.get_module_voltages = faulty_read
    assert c._begin_transition(True)
    ramps.ramp(rig)
    c._end_transition()
    assert not c.ramping and not c.transitioning
    assert all(request.state == state for request in c._latest_setpoints.values())
    assert all(math.isnan(ch.lastAppliedValue) for ch in rig.channels)
    assert not c._pending_setpoints and c._setpoint_thread is None

    # The next valid readback confirms the requests; no extra voltage command.
    writes = list(rig.writes)
    c.device.get_module_voltages = read
    c.readNumbers()
    assert all(request.state == "confirmed" for request in c._latest_setpoints.values())
    assert all(ch.lastAppliedValue == ch.value for ch in rig.channels)
    assert rig.writes == writes


def test_startup_tracking_does_not_change_ramp_writes(rig):
    # Compare the same ramp with and without the new startup tracking, including
    # its cadence and final values. The tracking must add no voltage commands.
    ramps.ramp(rig)
    reference = list(rig.writes)
    rig.controller.device.set_module_voltages(2, {1: 0., 2: 0., 3: 0.})
    rig.writes.clear()
    rig.clock.now = 0.
    assert rig.controller._begin_transition(True)
    ramps.ramp(rig)
    rig.controller._end_transition()
    assert rig.writes == reference


def test_off_cancels_startup_confirmation_before_the_next_on(rig):
    c = rig.controller
    assert c._begin_transition(True)
    ramps.ramp(rig)
    c._end_transition()
    previous = list(c._latest_setpoints.values())
    assert previous and all(request.state == "confirmed" for request in previous)

    assert c._begin_transition(False)
    assert all(request.cancel.is_set() and request.state == "stored" for request in previous)
    writes = list(rig.writes)
    c.readNumbers()  # A late reply must not revive cancelled startup confirmations.
    assert all(request.state == "stored" for request in previous)
    c._end_transition()
    assert rig.writes == writes

    assert c._begin_transition(True)
    current = list(c._latest_setpoints.values())
    assert all(request.state == "pending" and not request.cancel.is_set() for request in current)
    assert all(new.cancel is not old.cancel for new, old in zip(current, previous, strict=True))
    c.readNumbers()  # Even a matching old setpoint must not confirm an unsent ramp.
    assert all(request.state == "pending" for request in current)
    ramps.ramp(rig)
    c._end_transition()
    assert all(request.state == "confirmed" for request in current)
    assert all(request.state == "stored" for request in previous)
