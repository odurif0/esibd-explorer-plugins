"""Live PSU setpoint edits: what the plugin sends to its worker, and when.

The edit itself (ramp from the hardware setpoint, the other rail and the gates left alone, every
readback verified) runs in the PSU worker and is tested there: native/worker/tests/psu_controller.rs
(a_live_voltage_edit_steps_only_its_channel_without_gate_commands and the tests after it)."""
from copy import deepcopy
from threading import Event, Thread

import pytest



def test_a_refused_live_edit_switches_the_outputs_off_without_retrying(psu_live_rig):
    """The worker refuses an edit it cannot verify (native/worker/tests/psu_controller.rs:
    a_failed_live_write_is_not_retried_as_a_cold_start). The plugin then switches the outputs OFF
    under the same lock and never sends the state again as a cold start."""
    _, controller, device, state, messages = psu_live_rig
    state["voltage_values"][0] = 80.
    device.apply_failure = RuntimeError("CH0 voltage setpoint readback 100 V does not match requested 80 V.")
    controller.applyManualState(state)
    assert len(device.requests) == 1
    assert device.outputs == (False, False) and not device.enabled
    assert controller.errorCount == 1, messages
    assert any("Failed to apply PSU manual values" in msg and "does not match" in msg for msg in messages)
    assert "Applied PSU manual values." not in messages


def test_queued_field_edits_preserve_each_other_and_cancel_on_off(psu_live_rig):
    _, controller, device, _, messages = psu_live_rig
    # Hold the worker to reproduce several GUI commits before it dequeues.
    controller._manual_apply_worker_running = True
    for key, channel, target in (("voltage_values", 0, 80.),
                                  ("current_limit_values", 1, .015),
                                  ("voltage_values", 1, 230.),
                                  ("voltage_values", 0, 70.)):
        controller.applyManualStateFromThread({"setpoints_only": True, key: {channel: target}})
    assert device.calls == []
    controller._manual_state_apply_worker()
    # One operation for the worker, the last value of each field kept.
    assert device.requests == [{"setpoints_only": True, "voltage_values": {0: 70., 1: 230.},
                                "current_limit_values": {1: .015}}], device.requests
    assert device.calls == [("current", 1, .015), ("voltage", 0, 70.), ("voltage", 1, 230.)]
    assert device.voltages == {0: 70., 1: 230.}
    assert device.currents == {0: .01, 1: .015}
    assert device.outputs == (True, True) and device.enabled
    assert controller.errorCount == 0, messages
    device.calls.clear()
    controller._manual_apply_worker_running = True
    controller.applyManualStateFromThread({"setpoints_only": True, "current_limit_values": {0: .001}})
    controller._cancel_output_commands()
    controller._manual_state_apply_worker()
    assert device.calls == [] and controller._manual_apply_pending_state is None


def test_scan_writes_are_applied_without_a_log_line(psu_live_rig, monkeypatch):
    """2026-11-13 lab: MScan's rail writes, one per point, made 5 156 "Applied PSU manual values." lines.
    The scan's writes are applied as before, silently; an operator edit in the same batch is logged."""
    module, controller, device, _, messages = psu_live_rig
    controller.controllerParent.loading = False
    queued = []
    monkeypatch.setattr(controller, "_queue_native_gui", queued.append)
    sent = []
    unlocked = controller._apply_manual_state_unlocked
    monkeypatch.setattr(controller, "_apply_manual_state_unlocked",
                        lambda device, state, *a: sent.append(dict(state)) or unlocked(device, state, *a))

    class Channel:
        real = enabled = True

        def __init__(self, number, value):
            self.number, self.value = number, value

        def channel_number(self):
            return self.number

    # MScan marks the channel while it writes; the request is built on the next GUI turn.
    first, second = Channel(0, 80.), Channel(1, 230.)
    for channel in (first, second):
        channel.scan_driven = True
        controller.applyValueFromThread(channel)
        channel.scan_driven = False
    controller._manual_apply_worker_running = True
    for request in queued:
        request()
    controller._manual_state_apply_worker()
    assert device.voltages == {0: 80., 1: 230.}
    assert "Applied PSU manual values." not in messages
    assert sent and all("scan_driven" not in state for state in sent), "the worker accepts only its fields"
    queued.clear()
    controller.applyValueFromThread(Channel(0, 70.))  # the operator
    second.value, second.scan_driven = 220., True
    controller.applyValueFromThread(second)
    controller._manual_apply_worker_running = True
    for request in queued:
        request()
    controller._manual_state_apply_worker()
    assert device.voltages == {0: 70., 1: 220.}
    assert messages.count("Applied PSU manual values.") == 1, messages


def test_cancel_during_live_readback_does_not_write(psu_live_rig):
    _, controller, device, state, messages = psu_live_rig
    state["voltage_values"][0] = 80.
    getter = device.get_channel_voltage_limits

    def read(ch, **kw):
        controller._cancel_output_commands()
        return getter(ch, **kw)

    device.get_channel_voltage_limits = read
    controller.applyManualState(state)
    assert device.calls == []
    assert "Applied PSU manual values." not in messages


@pytest.mark.parametrize("target", [80., 480.])
@pytest.mark.parametrize("partial", [False, True])
def test_off_interrupts_live_ramp_and_cancels_queued_edits(psu_live_rig, target, partial):
    _, controller, device, state, messages = psu_live_rig
    state["voltage_values"][0] = target
    if partial:
        state = {"setpoints_only": True, "voltage_values": {0: target}}
    started, resume = Event(), Event()
    setter = device.set_channel_voltage

    def slow_write(ch, value, **kw):
        setter(ch, value, **kw)
        if value > 0:
            started.set()
            assert resume.wait(3)

    device.set_channel_voltage = slow_write
    shutdown_results = []
    stop = Thread(target=lambda: shutdown_results.append(controller.shutdownCommunication()))
    try:
        controller.applyManualStateFromThread(state)
        assert started.wait(3)
        queued = deepcopy(state)
        queued["voltage_values"][1] = 500.
        controller.applyManualStateFromThread(queued)
        stop.start()
        assert Event.wait(controller._output_cancel, 3)
    finally:
        resume.set()
        if stop.ident is not None:
            stop.join(3)
    assert shutdown_results == [True]
    assert device.outputs == (False, False) and not device.enabled and not device.connected
    positive_writes = [call for call in device.calls if call[0] == "voltage" and call[2] > 0]
    assert len(positive_writes) == 1 and positive_writes[0][1] == 0
    assert "Applied PSU manual values." not in messages
