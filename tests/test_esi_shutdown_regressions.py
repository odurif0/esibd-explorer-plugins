"""Output-sequence regressions: use events, not timing-dependent races."""

import logging
import runpy
import sys
import threading
import types
from pathlib import Path

import pytest

TESTS = Path(__file__).parent


def load_plugin():
    return runpy.run_path(str(TESTS / "test_esi_plugin_behavior.py"))["_load_plugin"]()


@pytest.mark.parametrize("command", ["ramp", "hv", "heat"])
@pytest.mark.parametrize("shutdown", [False, True])
def test_off_cancels_inflight_output_command(monkeypatch, command, shutdown):
    module = load_plugin()
    on = True
    paused = threading.Event()
    resume = threading.Event()
    calls = []
    errors = []
    parent = types.SimpleNamespace(
        name="ESI", isOn=lambda: on, getChannels=lambda: [],
        poll_timeout_s=1.0, connect_timeout_s=1.0, ramp_rate_v_s=1000.0,
    )
    controller = module.ESIController(parent)
    controller.initialized = True
    controller.heat_readback_valid = True
    controller.targets = {1: 100.0}
    controller.module_active = {1: command == "ramp"}
    controller.global_enabled = True
    controller.print = lambda *args, **kwargs: None

    def pause():
        paused.set()
        assert resume.wait(3), "test did not release the in-flight command"

    class Device:
        enabled = True
        target = 100.0

        def set_hv_module_target(self, address, target, **kwargs):
            self.target = target
            calls.append(("target", target))
            if command == "hv":
                pause()
            return target

        def set_heater_temperature(self, target, **kwargs):
            self.target = target
            calls.append(("heat", target))
            pause()

        def set_output_active(self, address, active, **kwargs):
            self.enabled = active
            calls.append(("enable", active))
            return active

        def set_global_active(self, active, **kwargs):
            self.enabled = active
            calls.append(("global", active))
            return active

        def force_safe_off(self, **kwargs):
            self.enabled = False
            self.target = 0.0
            calls.append(("off",))
            return False

        def disconnect(self, **kwargs):
            self.force_safe_off()
            return True

        def close(self):
            pass

    device = Device()
    controller.device = device
    channel = types.SimpleNamespace(
        enabled=True, value=300.0, module_address=lambda: 0 if command == "heat" else 1,
        is_heat_channel=lambda: command == "heat", name="test",
    )
    monkeypatch.setattr(module, "time", types.SimpleNamespace(sleep=lambda _: pause()))

    def guarded(fn):
        try:
            fn()
        except BaseException as exc:
            errors.append(exc)

    apply = threading.Thread(target=guarded, args=(lambda: controller.applyValue(channel),))
    stop = None
    cancel = controller._output_cancel
    try:
        apply.start()
        assert paused.wait(3)
        on = False
        stop = threading.Thread(
            target=guarded,
            args=(controller.shutdownCommunication if shutdown else controller.toggleOn,),
        )
        stop.start()
        # OFF must cancel before waiting for the output-sequence lock.
        assert cancel.wait(3)
    finally:
        resume.set()
        apply.join(3)
        if stop is not None:
            stop.join(3)
    assert not apply.is_alive()
    assert stop is not None and not stop.is_alive()
    assert not errors
    assert not device.enabled
    assert device.target == 0.0
    assert not any(call == ("enable", True) for call in calls)
    assert len([call for call in calls if call[0] in {"target", "heat"}]) == 1
    assert controller.main_state == "Disconnected"
    assert controller.device is None
    assert not controller.initialized
    # A queued command after OFF must not write anything either.
    before = list(calls)
    controller.applyValue(channel)
    assert calls == before
    if not shutdown:
        # The next ON reconnects before activating. Old requests stay cancelled.
        controller.device = device
        controller.initialized = True
        on = True
        controller.toggleOn()
        assert controller._output_cancel is not cancel
        assert not controller._output_cancel.is_set()
        controller.applyValue(channel)
        assert device.enabled


def test_poisoned_esi_disconnect_and_plugin_shutdown_remain_unconfirmed():
    helpers = runpy.run_path(str(TESTS / "test_esi_driver_behavior.py"))
    helpers["_load_runtime"]()
    driver_module = sys.modules[helpers["RUNTIME_NAME"] + ".esi.esi"]
    device = helpers["_controller"](driver_module)
    device.logger = logging.getLogger("esi_shutdown_regression")
    device.connected = True
    device._poison_transport("blocked read")
    assert not device.connected
    with pytest.raises(RuntimeError, match="transport is unusable"):
        device.disconnect()

    module = load_plugin()
    controller = module.ESIController(types.SimpleNamespace(connect_timeout_s=1.0))
    controller.device = device
    controller.initialized = True
    messages = []
    controller.print = lambda message, **kwargs: messages.append(message)
    assert controller.shutdownCommunication() is False
    assert controller.main_state == "Shutdown unconfirmed"
    assert any("HV may remain energized" in message for message in messages)
    # Retain the poisoned backend and Explorer's close warning, not a false OFF.
    assert controller.device is device
    assert controller.initialized
    assert controller.shutdownCommunication() is False
    assert controller.main_state == "Shutdown unconfirmed"


def test_command_queued_before_off_cannot_revive_after_new_on():
    module = load_plugin()
    on = True
    parent = types.SimpleNamespace(
        isOn=lambda: on, getChannels=lambda: [],
        poll_timeout_s=1.0, connect_timeout_s=1.0,
    )
    controller = module.ESIController(parent)
    controller.initialized = True
    controller.print = lambda *args, **kwargs: None
    calls = []
    controller.device = types.SimpleNamespace(
        force_safe_off=lambda **kwargs: calls.append(("off",)),
        set_output_active=lambda address, active, **kwargs: calls.append(("active", address, active)) or active,
        set_global_active=lambda active, **kwargs: calls.append(("global", active)) or active,
        set_hv_module_target=lambda address, target, **kwargs: calls.append(("target", address, target)) or target,
    )
    channel = types.SimpleNamespace(
        enabled=True, value=100.0, is_heat_channel=lambda: False, module_address=lambda: 1,
    )
    waiting = threading.Event()
    original_lock = controller._output_lock
    main_thread = threading.current_thread()

    class ObservedLock:
        def __enter__(self):
            if threading.current_thread() is not main_thread:
                waiting.set()
            original_lock.acquire()
        def __exit__(self, *args):
            original_lock.release()

    controller._output_lock = ObservedLock()
    errors = []
    def queued_apply():
        try:
            controller.applyValue(channel)
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=queued_apply)
    try:
        with original_lock:
            thread.start()
            assert waiting.wait(3)
            on = False
            controller.toggleOn()
            on = True
            controller.toggleOn()
            after_new_on = list(calls)
    finally:
        thread.join(3)
    assert not thread.is_alive()
    assert not errors
    assert calls == after_new_on, "an old queued command used the new ON generation"


def test_esi_shutdown_rejects_false_driver_confirmation():
    module = load_plugin()
    controller = module.ESIController(types.SimpleNamespace(connect_timeout_s=1.0))
    controller.device = types.SimpleNamespace(disconnect=lambda **kwargs: False, close=lambda: None)
    controller.print = lambda *args, **kwargs: None
    assert controller.shutdownCommunication() is False
    assert controller.main_state == "Shutdown unconfirmed"
