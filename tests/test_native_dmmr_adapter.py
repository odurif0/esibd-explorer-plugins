"""Native DMMR adapter checks without loading a DLL or real Explorer host."""

import json
from collections import deque
from threading import Event

import numpy as np
import pytest

from test_dmmr_ranges import rig as legacy_rig


class NativeBackend:
    family = "dmmr"
    session = "native-test-session"

    def __init__(self):
        self.closed = False
        self.connected = True
        self.enabled = False
        self.automatic = False
        self.calls = []
        self.cancel_events = []
        self.rpc_timeouts = []
        self.cancel_count = 0
        self.token = 0
        self.poll_replies = deque()
        self.failures = {}
        self.after_poll = None

    def call_method(self, method, *args, rpc_timeout_s=30.0,
                    _cancel_event=None, **kwargs):
        # Events belong to the proxy locally. Everything sent to Rust is JSON.
        json.dumps({"args": args, "kwargs": kwargs}, allow_nan=False)
        assert rpc_timeout_s > 0
        self.rpc_timeouts.append((method, rpc_timeout_s))
        assert _cancel_event is None or isinstance(_cancel_event, Event)
        self.cancel_events.append((method, _cancel_event))
        self.calls.append((method, args, kwargs))
        failure = self.failures.get(method)
        if isinstance(failure, Exception):
            raise failure
        if method == "begin_command_recovery":
            return {"phase": args[0], "used": False}
        if method == "set_enable":
            self.enabled = args[0]
            return 0 if failure is None else failure
        if method == "get_enable":
            return 0, self.enabled
        if method == "set_automatic_current":
            self.automatic = args[0]
            return 0 if failure is None else failure
        if method == "get_automatic_current":
            return 0, self.automatic
        if method == "get_state":
            return (0 if failure is None else failure), 0, "ST_ON"
        if method in ("get_device_state", "get_voltage_state", "get_temperature_state"):
            return 0, 0, []
        if method == "verify_running":
            assert self.enabled and not self.automatic
            return 0, 0, "ST_ON"
        if method == "recover_command":
            expected = args[2]
            if "enabled" in expected:
                assert self.enabled is expected["enabled"]
            result = {"verified": True, "expected": expected}
            if expected.get("running"):
                assert self.enabled and not self.automatic
                result["result"] = (0, 0, "ST_ON")
            return result
        if method == "configure_module_ranges":
            return {address: (2 if mode == "Auto" else int(mode), mode == "Auto")
                    for address, mode in args[0].items()}
        if method == "configure_read_recovery":
            return {"configured": True}
        if method == "recover_read":
            return {"verified": True, "awaiting_samples": [0, 3]}
        if method == "poll_currents":
            self.token += 1
            reply = (self.poll_replies.popleft() if self.poll_replies else {
                "token": self.token, "values": {0: 0.534e-12, 3: -2e-12},
                "ranges": {0: 0, 3: 4}, "valid": {0: True, 3: True},
                "recovery": None,
            })
            if self.after_poll:
                self.after_poll()
            if isinstance(reply, Exception):
                raise reply
            return reply
        if method == "disable_acquisition":
            if failure is not None:
                return failure
            self.enabled = self.automatic = False
            return True
        if method == "shutdown":
            if failure is not None:
                return failure
            self.enabled = self.automatic = False
            self.connected = False
            return True
        if method == "disconnect":
            self.connected = False
            return True
        if method in ("begin_startup_diagnostics", "end_startup_diagnostics"):
            return ""
        if method == "format_status":
            return str(args[0])
        raise AssertionError(f"Unexpected native method: {method}")

    def cancel(self):
        self.cancel_count += 1

    def close(self, *, grace_s=0):
        self.calls.append(("process_close", (), {}))
        self.closed, self.connected = True, False
        return True


class NativeFacade:
    NO_ERR = 0
    NO_DATA = 1
    _transport_poisoned = False
    _open_failed = False
    _failed_open_released = False
    _opening_in_progress = False
    _startup_log_path = None

    def __init__(self, backend):
        self._backend = backend

    @property
    def connected(self):
        return self._backend.connected

    def new_command_recovery(self, **kwargs):
        raise AssertionError("Python callback recovery must not reach a native worker")

    def new_read_recovery(self, **kwargs):
        raise AssertionError("Python object recovery must not reach a native worker")

    def close(self):
        self._backend.calls.append(("process_close", (), {}))
        self._backend.closed = True

    def __getattr__(self, name):
        allowed = {
            "set_enable", "set_automatic_current", "get_automatic_current", "get_state",
            "get_device_state", "get_voltage_state", "get_temperature_state",
            "begin_startup_diagnostics", "end_startup_diagnostics", "disconnect", "format_status",
        }
        if name not in allowed:
            raise AttributeError(name)
        return lambda *args, **kwargs: self._backend.call_method(name, *args, **kwargs)


@pytest.fixture
def native_rig(legacy_rig):
    backend = NativeBackend()
    legacy_rig.backend = backend
    legacy_rig.hardware = NativeFacade(backend)
    legacy_rig.controller.device = legacy_rig.hardware
    return legacy_rig


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
