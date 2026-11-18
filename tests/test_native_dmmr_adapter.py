"""Native DMMR adapter checks without loading a DLL or real Explorer host."""

from threading import Event

import numpy as np
import pytest
from support.dmmr import NativeFacade


def methods(rig):
    return [name for name, _args, _kwargs in rig.backend.calls]


def assert_missing(rig):
    values, ranges, _token = rig.controller.measurementSnapshot()
    assert set(values) == set(ranges) == {0, 3}
    assert all(np.isnan(value) for value in (*values.values(), *ranges.values()))


def test_startup_uses_native_json_recovery_and_one_range_configuration(native_rig):
    rig = native_rig
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    assert methods(rig).count("begin_command_recovery") == 1
    assert methods(rig).count("configure_module_ranges") == 1
    assert rig.controller._verified_read_ranges == {0: (2, True), 3: (4, False)}
    payload = next(args for method, args, _ in rig.backend.calls
                   if method == "configure_module_ranges")
    assert payload == ({0: "Auto", 3: "4"},)
    events = [event for name, event in rig.backend.cancel_events
              if name in ("set_enable", "configure_module_ranges")]
    assert events and all(event is rig.controller._native_cancel_event for event in events)
    assert "continue_check" not in repr(rig.backend.calls)


def test_native_adapter_delegates_outer_batch_budget_to_local_facade_policy(native_rig, monkeypatch):
    requested = []
    def budget(cls, method, kwargs):
        requested.append((method, kwargs))
        return 90.0
    monkeypatch.setattr(NativeFacade, '_native_rpc_timeout_for', classmethod(budget), raising=False)
    native_rig.controller.acquiring = True
    native_rig.controller.readNumbers()
    assert requested == [('poll_currents', {'timeout_s': 0.1})]
    assert ('poll_currents', 90.0) in native_rig.backend.rpc_timeouts


def test_lost_enable_ack_is_verified_not_replayed(native_rig):
    rig = native_rig
    rig.backend.failures["set_enable"] = -12
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    assert methods(rig).count("set_enable") == 1
    recovery_args = next(args for name, args, _ in rig.backend.calls
                         if name == "recover_command")
    assert recovery_args == (-12, "set_enable(True)", {"enabled": True})


def test_lost_state_ack_uses_range_and_running_json_expectations(native_rig):
    rig = native_rig
    rig.backend.failures["get_state"] = -11
    rig.controller.toggleOn()
    assert rig.controller.acquiring
    recovery_args = next(args for name, args, _ in rig.backend.calls
                         if name == "recover_command")
    assert recovery_args[2] == {"running": True, "ranges": {0: (2, True), 3: (4, False)}}


def test_failed_native_range_setup_aborts_startup_and_verifies_off(native_rig):
    rig = native_rig
    rig.backend.failures["configure_module_ranges"] = RuntimeError("Range not confirmed")
    rig.controller.toggleOn()
    assert not rig.controller.acquiring
    assert not rig.parent.on
    assert not rig.backend.enabled and not rig.backend.automatic
    assert "disable_acquisition" in methods(rig)
    assert rig.controller.device is rig.hardware  # Keep communication for an explicit OFF.


def test_native_poll_publishes_paired_actual_ranges_with_fresh_tokens(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    values, ranges, first = rig.controller.measurementSnapshot()
    assert values == {0: 0.534e-12, 3: -2e-12}
    assert ranges == {0: 0, 3: 4}  # Not the requested Auto/range labels.
    rig.controller.readNumbers()
    assert rig.controller.measurementSnapshot()[2] is not first
    assert "get_module_current" not in methods(rig)
    rig.controller.updateValues()
    assert rig.channels[0].monitor == 0.534e-12
    assert rig.channels[0].measurement_range == 0


@pytest.mark.parametrize("current,meas_range,valid", [
    (np.nan, 0, True), (np.inf, 0, True), (1e-12, 5, True),
    (1e-12, True, True), (1e-12, 0, False), (1e-12, 0, 1),
])


def test_invalid_native_sample_invalidates_current_and_range_together(
        native_rig, current, meas_range, valid):
    rig = native_rig
    rig.controller.acquiring = True
    rig.backend.poll_replies.append({
        "token": 1, "values": {0: current, 3: -2e-12},
        "ranges": {0: meas_range, 3: 4}, "valid": {0: valid, 3: True}, "recovery": None,
    })
    rig.controller.readNumbers()
    values, ranges, _token = rig.controller.measurementSnapshot()
    assert np.isnan(values[0]) and np.isnan(ranges[0])
    assert values[3] == -2e-12 and ranges[3] == 4
    assert rig.controller.acquiring


def test_recovered_native_cycle_is_missing_then_requires_new_cycle(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.backend.poll_replies.append({
        "token": 1, "values": {0: 1e-12, 3: 2e-12}, "ranges": {0: 0, 3: 4},
        "valid": {0: True, 3: True}, "recovery": {"verified": True},
    })
    rig.controller.readNumbers()
    assert_missing(rig)
    rig.controller.readNumbers()
    assert rig.controller.values == {0: 0.534e-12, 3: -2e-12}


def test_retired_sample_token_stops_acquisition_instead_of_republishing(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.controller.readNumbers()
    rig.backend.token = 0
    rig.controller.readNumbers()
    assert_missing(rig)
    assert not rig.controller.acquiring
    assert "disable_acquisition" in methods(rig)
    assert not rig.backend.enabled and not rig.backend.automatic


@pytest.mark.parametrize("late_action", ["off", "retire"])
def test_late_poll_does_not_publish_after_off_or_device_retirement(native_rig, late_action):
    rig = native_rig
    rig.controller.acquiring = True
    if late_action == "off":
        rig.backend.after_poll = lambda: setattr(rig.parent, "on", False)
    else:
        rig.backend.after_poll = lambda: setattr(rig.controller, "device", None)
    rig.controller.readNumbers()
    assert_missing(rig)


def test_state_receive_recovery_stays_native_and_uses_json_baseline(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.controller._verified_read_ranges = {0: (2, True), 3: (4, False)}
    incident = rig.controller._recover_read(-13, "get_state")
    assert incident["verified"]
    assert methods(rig) == ["configure_read_recovery", "recover_read"]
    assert rig.backend.calls[0][1] == ({0: (2, True), 3: (4, False)},)
    assert rig.backend.calls[1][1] == (-13, "get_state")


def test_confirmed_shutdown_cancels_locally_and_releases_process_last(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.backend.enabled = True
    event = rig.controller._native_cancel_event = Event()
    assert rig.controller.shutdownCommunication()
    assert event.is_set() and rig.backend.cancel_count >= 1
    assert not rig.backend.enabled and not rig.backend.automatic
    assert not rig.backend.connected and rig.backend.closed
    assert rig.controller.device is None and rig.controller.main_state == "Disconnected"
    assert methods(rig).index("shutdown") < methods(rig).index("process_close")
    shutdown_event = next(event for name, event in rig.backend.cancel_events if name == "shutdown")
    assert shutdown_event is None  # A fresh OFF request must not inherit startup cancellation.


@pytest.mark.parametrize("failure", [False, RuntimeError("OFF not confirmed")])
def test_failed_shutdown_retains_backend_and_off_retry_ui(native_rig, failure):
    rig = native_rig
    rig.backend.enabled = True
    rig.backend.failures["shutdown"] = failure
    assert not rig.controller.shutdownCommunication()
    assert rig.controller.device is rig.hardware
    assert rig.backend.connected and not rig.backend.closed
    assert rig.backend.enabled
    assert rig.parent.on and rig.controller.initialized
    assert rig.controller.main_state == rig.module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert "disconnect" not in methods(rig) and "process_close" not in methods(rig)


def test_explicit_off_checks_both_gates_before_disposal(native_rig):
    rig = native_rig
    rig.backend.enabled = rig.backend.automatic = True
    rig.parent.on = False
    rig.controller.toggleOn()
    assert rig.controller.device is None
    assert not rig.backend.enabled and not rig.backend.automatic
    assert methods(rig).index("disable_acquisition") < methods(rig).index("disconnect")
    assert methods(rig)[-1] == "process_close"


def test_closed_native_transport_is_retired_without_false_off_confirmation(native_rig):
    rig = native_rig
    rig.controller.acquiring = True
    rig.backend.after_poll = lambda: setattr(rig.backend, "closed", True)
    rig.backend.poll_replies.append(TimeoutError("Native worker request timed out"))
    rig.controller.readNumbers()
    assert_missing(rig)
    assert not rig.controller.acquiring
    assert rig.controller.device is None and rig.backend.closed
    assert not rig.parent.on and not rig.controller.initialized
    assert rig.controller.main_state == "Disconnected: shutdown unconfirmed"
    assert "shutdown" not in methods(rig) and "disconnect" not in methods(rig)
