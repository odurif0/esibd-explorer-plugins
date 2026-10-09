"""Native PSU hardware operations stay serialized in Rust, not the GUI loop."""

from __future__ import annotations

from copy import deepcopy
import math
from threading import Event
from types import SimpleNamespace

import pytest

from psu_fakes import StatefulPSU
from test_psu_plugin_behavior import _load_module


class NativeProxy:
    session = "native-psu-test-session"

    def __init__(self):
        self.calls = []
        self.readbacks = {0: (100.0, 1000.0), 1: (240.0, 1000.0)}
        self.on_call = None
        self.result = None
        self.failure = None

    def call_method(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.on_call is not None:
            self.on_call(method)
        if method == "get_channel_voltage_limits":
            return self.readbacks[args[0]]
        if self.failure is not None:
            raise self.failure
        if method not in {"apply_manual_state", "ramp_channel_voltage"}:
            raise AssertionError(f"unexpected native operation: {method}")
        return self.result


@pytest.fixture
def native_case():
    module = _load_module()
    parent = SimpleNamespace(
        name="PSU_A", isOn=lambda: True, getChannels=lambda: [],
        startup_timeout_s=1.0, poll_timeout_s=0.1, interlock_monitoring=True,
        ramp_step_v=100.0, ramp_step_interval_s=0.05,
    )
    controller = module.PSUController(parent)
    controller.initialized = True
    device = controller.device = StatefulPSU()
    proxy = device._backend = NativeProxy()
    device._backend_mode = "process"
    updates, messages = [], []
    controller._update_state = lambda: updates.append("readback")
    controller._sync_status_to_gui = lambda **kwargs: updates.append(kwargs)
    controller.print = lambda message, **kwargs: messages.append(message)
    return module, controller, device, proxy, updates, messages


def manual():
    return dict(
        output_enabled={0: True, 1: False},
        full_range_enabled={0: False, 1: True},
        voltage_values={0: 300.0, 1: 777.12345},
        current_limit_values={0: 0.01, 1: 0.012345},
    )


def test_native_manual_has_one_atomic_operation_and_preserves_gui_status(native_case):
    _module, controller, device, proxy, updates, messages = native_case
    state = manual()
    before = deepcopy(state)

    def check_lock(method):
        assert controller.lock.locked()
        assert controller._manual_apply_active

    proxy.on_call = check_lock
    controller.applyManualState(state)
    assert [method for method, _, _ in proxy.calls] == ["get_channel_voltage_limits", "apply_manual_state"]
    method, args, kwargs = proxy.calls[-1]
    assert method == "apply_manual_state"
    assert args == (state,) and args[0] is state and state == before
    assert kwargs["interlock_monitoring"] is True
    assert kwargs["ramp_step_v"] == 100.0
    assert kwargs["ramp_step_interval_s"] == 0.05
    assert kwargs["timeout_s"] == 1.0
    assert kwargs["_cancel_event"] is controller._output_cancel
    assert 30.0 <= kwargs["rpc_timeout_s"] <= 3600.0
    assert device.calls == [], "there must be no Python-side hardware ramp or gate writes"
    assert "readback" in updates
    assert updates[-1] == {"sync_manual_panel": True}
    assert "Applied PSU manual values." in messages
    assert controller.errorCount == 0 and not controller._manual_apply_active


@pytest.mark.parametrize("partial", [True, False])
def test_manual_masks_partial_values_and_interlock_setting_are_not_rewritten(native_case, partial):
    _module, controller, device, proxy, _updates, _messages = native_case
    controller.controllerParent.interlock_monitoring = False
    state = {"setpoints_only": True, "voltage_values": {1: 80.12345}} if partial else manual()
    before = deepcopy(state)
    assert controller._apply_manual_state_unlocked(device, state, 1.0, controller._output_cancel)
    assert proxy.calls[-1][1] == (state,)
    assert state == before
    assert proxy.calls[-1][2]["interlock_monitoring"] is False
    assert device.calls == []


def test_native_current_only_edit_needs_no_gui_voltage_reads(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    state = {"setpoints_only": True, "current_limit_values": {0: 0.015}}
    assert controller._apply_manual_state_unlocked(device, state, 1.0, controller._output_cancel)
    assert [method for method, _, _ in proxy.calls] == ["apply_manual_state"]
    assert proxy.calls[0][1] == (state,)


def test_native_ramp_uses_worker_readback_not_gui_start_and_never_python_steps(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    proxy.readbacks[0] = (900.0, 1000.0)
    controller.controllerParent.ramp_step_v = 25.0
    controller.controllerParent.ramp_step_interval_s = 0.2
    assert controller._ramp_channel_voltage(
        device, 0, 80.0, timeout_s=1.0, cancel=controller._output_cancel, start_v=0.0,
    )
    assert [method for method, _, _ in proxy.calls] == ["get_channel_voltage_limits", "ramp_channel_voltage"]
    method, args, kwargs = proxy.calls[-1]
    assert method == "ramp_channel_voltage" and args == (0, 80.0)
    assert "start_v" not in kwargs
    assert kwargs["ramp_step_v"] == 25.0 and kwargs["ramp_step_interval_s"] == 0.2
    steps = math.ceil((900.0 - 80.0) / 25.0)
    assert kwargs["rpc_timeout_s"] >= steps * (1.0 + 0.2) + 32.0
    assert device.calls == []


def test_budget_includes_front_panel_changes_within_reported_range(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    proxy.readbacks[0] = (0.0, 1000.0)
    assert controller._ramp_channel_voltage(device, 0, 100.0, timeout_s=1.0, cancel=controller._output_cancel)
    kwargs = proxy.calls[-1][2]
    assert kwargs["rpc_timeout_s"] >= 9 * 1.05 + 32.0 + 5.0


@pytest.mark.parametrize("operation", ["manual", "ramp"])
def test_unsupported_ramp_deadline_is_rejected_before_writes(native_case, operation):
    _module, controller, device, proxy, _updates, _messages = native_case
    controller.controllerParent.ramp_step_v = 0.01
    controller.controllerParent.ramp_step_interval_s = 0.05
    with pytest.raises(ValueError, match="one-hour deadline"):
        if operation == "manual":
            controller._apply_manual_state_unlocked(device, manual(), 1.0, controller._output_cancel)
        else:
            controller._ramp_channel_voltage(device, 0, 100.0, timeout_s=1.0, cancel=controller._output_cancel)
    assert all(method == "get_channel_voltage_limits" for method, _, _ in proxy.calls)
    assert device.calls == []


def test_output_off_does_not_budget_irrelevant_slow_ramps(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    controller.controllerParent.ramp_step_v = 0.000001
    controller.controllerParent.ramp_step_interval_s = 1000.0
    state = manual()
    state["output_enabled"] = {0: False, 1: False}
    assert controller._apply_manual_state_unlocked(device, state, 1.0, controller._output_cancel)
    assert [method for method, _, _ in proxy.calls] == ["apply_manual_state"]


@pytest.mark.parametrize("operation", ["manual", "ramp"])
def test_pre_cancelled_operations_never_dispatch_and_old_tokens_stay_cancelled(native_case, operation):
    _module, controller, device, proxy, _updates, _messages = native_case
    old = Event()
    old.set()
    controller._output_cancel = Event()
    if operation == "manual":
        assert not controller._apply_manual_state_unlocked(device, manual(), 1.0, old)
    else:
        assert not controller._ramp_channel_voltage(device, 0, 100.0, timeout_s=1.0, cancel=old)
    assert proxy.calls == [] and device.calls == []


def test_cancel_after_budget_read_prevents_native_apply(native_case):
    _module, controller, device, proxy, _updates, messages = native_case
    proxy.on_call = lambda method: controller._output_cancel.set()
    controller.applyManualState(manual())
    assert [method for method, _, _ in proxy.calls] == ["get_channel_voltage_limits"]
    assert "Applied PSU manual values." not in messages
    assert device.calls == []


def test_cancel_arriving_with_reply_does_not_publish_success(native_case):
    _module, controller, device, proxy, updates, messages = native_case
    proxy.on_call = lambda method: controller._output_cancel.set() if method == "apply_manual_state" else None
    controller.applyManualState(manual())
    assert "Applied PSU manual values." not in messages
    assert "readback" not in updates
    assert not controller._manual_apply_active


@pytest.mark.parametrize("failure", [RuntimeError("native verification failed"), TimeoutError("native worker terminated; output safety unknown")])
def test_native_failure_keeps_existing_locked_recovery_and_no_success(native_case, failure):
    _module, controller, device, proxy, _updates, messages = native_case
    proxy.failure = failure
    recovery = []

    def recover(**kwargs):
        assert controller.lock.locked()
        recovery.append(kwargs)
        return False

    controller._safe_disable_outputs_after_failure = recover
    controller.applyManualState(manual())
    assert recovery == [{"timeout_s": 1.0, "already_acquired": True}]
    assert "Applied PSU manual values." not in messages
    assert not controller._manual_apply_active


def test_native_unexpected_success_payload_is_not_a_successful_placeholder(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    proxy.result = True
    with pytest.raises(RuntimeError, match="Unexpected result"):
        controller._apply_manual_state_unlocked(device, manual(), 1.0, controller._output_cancel)


def test_process_reference_without_native_session_keeps_python_ramp(native_case, monkeypatch):
    _module, controller, device, proxy, _updates, _messages = native_case
    proxy.session = None
    monkeypatch.setattr(controller._output_cancel, "wait", lambda _: False)
    assert controller._ramp_channel_voltage(device, 0, 200.0, timeout_s=1.0, cancel=controller._output_cancel)
    assert device.calls == [("voltage", 0, 100.0), ("voltage", 0, 200.0)]
    assert proxy.calls == []


def test_inline_reference_manual_branch_is_preserved(native_case, monkeypatch):
    _module, controller, device, proxy, _updates, _messages = native_case
    device._backend_mode = "inline"
    device.interlocks = (True, True)
    monkeypatch.setattr(controller._output_cancel, "wait", lambda _: False)
    assert controller._apply_manual_state_unlocked(device, manual(), 1.0, controller._output_cancel)
    assert proxy.calls == []
    assert device.outputs == (True, False) and device.enabled
    assert device.voltages == {0: 300.0, 1: 777.12345}


def test_invalid_timeout_is_rejected_before_dispatch(native_case):
    _module, controller, device, proxy, _updates, _messages = native_case
    for timeout in [0.0, -1.0, float("nan"), float("inf"), 120.0]:
        with pytest.raises(ValueError):
            controller._ramp_channel_voltage(device, 0, 100.0, timeout_s=timeout, cancel=controller._output_cancel)
    assert proxy.calls == []


def test_transport_consumes_cancel_locally_and_preserves_integer_channel_maps(native_case, monkeypatch):
    from test_native_worker import TRANSPORT

    _module, controller, device, _proxy, _updates, _messages = native_case
    proxy = TRANSPORT.NativeWorkerProxy.__new__(TRANSPORT.NativeWorkerProxy)
    requests = []

    def request(op, timeout_s, *, _cancel_event=None, **fields):
        assert op == "call"
        assert _cancel_event is controller._output_cancel
        assert "cancel" not in fields["kwargs"] and "_cancel_event" not in fields["kwargs"]
        assert fields["kwargs"]["timeout_s"] == 1.0
        requests.append((timeout_s, fields))
        if fields["method"] == "get_channel_voltage_limits":
            return 100.0, 1000.0
        assert fields["method"] == "apply_manual_state"
        return None

    proxy.session = "native-adapter-codec-test"
    monkeypatch.setattr(proxy, "_request", request)
    device._backend = proxy
    state = manual()
    assert controller._apply_manual_state_unlocked(device, state, 1.0, controller._output_cancel)
    assert requests[-1][1]["args"][0]["output_enabled"] == {"$map": [[0, True], [1, False]]}
    assert requests[-1][1]["args"][0]["full_range_enabled"] == {"$map": [[0, False], [1, True]]}


def test_native_worker_loss_never_becomes_gui_off_confirmation(native_case, monkeypatch):
    module, controller, device, proxy, _updates, messages = native_case
    device.outputs = (True, False)
    device.enabled = True
    proxy.failure = RuntimeError("native worker terminated; output safety unknown")

    def unavailable(*args, **kwargs):
        raise RuntimeError("native worker terminated; output safety unknown")

    for name in ("set_output_enabled", "set_device_enabled", "get_device_enabled", "get_output_enabled"):
        monkeypatch.setattr(device, name, unavailable)
    controller.applyManualState(manual())
    assert controller.main_state == module._PSU_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.output_state_summary == "Unknown"
    assert controller.device is device
    assert device.connected and device.outputs == (True, False)
    assert any("outputs may still be live" in message for message in messages)
    assert "Applied PSU manual values." not in messages
    assert not any(call[0] == "disconnect" for call in device.calls)


def test_unconfirmed_native_shutdown_keeps_backend_available_for_explicit_recovery(native_case, monkeypatch):
    module, controller, device, _proxy, _updates, messages = native_case
    device.outputs = (True, False)
    device.enabled = True

    def unavailable(*args, **kwargs):
        raise RuntimeError("native worker terminated; output safety unknown")

    for name in ("set_channel_current", "set_channel_voltage", "set_output_enabled", "set_device_enabled", "collect_housekeeping"):
        monkeypatch.setattr(device, name, unavailable)
    assert controller.shutdownCommunication() is False
    assert controller.device is device
    assert controller.main_state == module._PSU_SHUTDOWN_UNCONFIRMED_STATE
    assert device.connected and device.outputs == (True, False)
    assert not any(call[0] == "disconnect" for call in device.calls)
    assert any("could not be confirmed" in message for message in messages)
