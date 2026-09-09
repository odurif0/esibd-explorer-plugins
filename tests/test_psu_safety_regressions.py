"""Fault-injection tests for PSU output sequencing, not just successful writes."""

import importlib
import logging
import threading
import types

import pytest

from test_psu_plugin_behavior import _load_module


from psu_fakes import StatefulPSU


@pytest.fixture
def setup():
    module = _load_module()
    parent = types.SimpleNamespace(
        name="PSU_A", isOn=lambda: True, getChannels=lambda: [],
        startup_timeout_s=1.0, poll_timeout_s=0.1, connect_timeout_s=0.1,
        interlock_monitoring=True, com=1, baudrate=230400,
    )
    controller = module.PSUController(parent)
    controller.device = StatefulPSU()
    controller.initialized = True
    controller._update_state = lambda: None
    controller._refresh_available_configs = lambda: None
    messages = []
    controller.print = lambda msg, **kwargs: messages.append(msg)
    return module, controller, controller.device, messages


def manual(voltage=20.0):
    return {
        "output_enabled": {0: True, 1: False},
        "voltage_values": {0: voltage, 1: 0.0},
        "current_limit_values": {0: 0.01, 1: 0.0},
        "full_range_enabled": {0: False, 1: False},
    }


def test_off_interrupts_ramp_before_any_further_positive_step(setup):
    _module, controller, device, messages = setup
    started, resume = threading.Event(), threading.Event()
    original = device.set_channel_voltage

    def slow_write(channel, value, **kwargs):
        original(channel, value, **kwargs)
        if value > 0:
            started.set()
            assert resume.wait(3)

    device.set_channel_voltage = slow_write
    apply = threading.Thread(target=controller.applyManualState, args=(manual(300),))
    results = []
    shutdown = threading.Thread(target=lambda: results.append(controller.shutdownCommunication()))
    try:
        apply.start()
        assert started.wait(3)
        shutdown.start()
        assert controller._output_cancel.wait(3)
    finally:
        resume.set()
        apply.join(3)
        if shutdown.ident is not None:
            shutdown.join(3)
    assert not apply.is_alive() and not shutdown.is_alive()
    assert results == [True]
    assert not device.enabled and device.outputs == (False, False)
    assert not device.connected
    assert len([c for c in device.calls if c[0] == "voltage" and c[2] > 0]) == 1
    assert "Applied PSU manual values." not in messages


def test_old_command_token_stays_cancelled_after_new_on_session(setup):
    _module, controller, device, _messages = setup
    old = controller._output_cancel
    controller._cancel_output_commands()
    controller.toggleOn()
    assert controller._output_cancel is not old
    assert old.is_set()
    device.calls.clear()
    controller.applyManualState(manual(), cancel=old)
    assert device.calls == []


@pytest.mark.parametrize("fault", ["voltage", "current", "current_nan", "range", "missing"])
def test_bad_readback_prevents_activation(setup, fault):
    _module, controller, device, messages = setup
    if fault == "voltage":
        device.get_channel_voltage_limits = lambda *a, **k: (999.0, 10000.0)
    elif fault == "current":
        device.get_channel_current_limits = lambda *a, **k: (0.02, 1.0)
    elif fault == "current_nan":
        device.get_channel_current_limits = lambda *a, **k: (float("nan"), 1.0)
    elif fault == "range":
        device.get_output_full_range = lambda **k: (True, True)
    else:
        device.get_channel_voltage_limits = None
    controller.applyManualState(manual())
    assert ("global", True) not in device.calls
    assert ("outputs", True, False) not in device.calls
    assert device.outputs == (False, False)
    assert any("Failed to apply" in msg for msg in messages)


def test_cancel_after_readback_prevents_activation(setup):
    _module, controller, device, _messages = setup

    def read_range(**kwargs):
        controller._cancel_output_commands()
        return device.ranges

    device.get_output_full_range = read_range
    controller.applyManualState(manual())
    assert ("global", True) not in device.calls
    assert device.outputs == (False, False)


@pytest.mark.parametrize("enabled", [True, False])
def test_initialization_applies_both_interlock_states(setup, monkeypatch, enabled):
    module, controller, device, _messages = setup
    controller.device = None
    controller.controllerParent.interlock_monitoring = enabled
    device.interlocks = (not enabled, not enabled)
    monkeypatch.setattr(module, "_get_psu_driver_class", lambda: lambda **kw: device)
    controller.runInitialization()
    assert device.interlocks == (enabled, enabled)
    assert ("interlocks", enabled, enabled) in device.calls
    assert hasattr(controller.signalComm.initCompleteSignal, "last_emit")


@pytest.mark.parametrize("actual", [(False, False), (True, False), (None, True)])
def test_unconfirmed_interlock_prevents_activation(setup, actual):
    _module, controller, device, messages = setup
    device.get_interlock_enabled = lambda **kw: actual
    controller.applyManualState(manual())
    assert ("global", True) not in device.calls
    assert not device.enabled
    assert any("interlock" in msg and "not confirmed" in msg for msg in messages)


def test_off_is_accepted_during_an_on_transition(setup):
    module, controller, _device, _messages = setup
    ui = module.PSUDevice.__new__(module.PSUDevice)
    ui.controller = controller
    ui.onAction = types.SimpleNamespace(state=True)
    ui.isOn = lambda: ui.onAction.state
    ui._set_on_ui_state = lambda state: setattr(ui.onAction, "state", state)
    controller.transitioning = True
    controller.transition_target_on = True
    ui.setOn(False)
    assert controller._output_cancel.is_set()
    assert not ui.onAction.state


def test_off_before_initialization_worker_prevents_open_and_late_init_complete(setup, monkeypatch):
    module, controller, _device, _messages = setup
    controller._cancel_output_commands()
    monkeypatch.setattr(module, "_get_psu_driver_class", lambda: pytest.fail("must not open"))
    controller.runInitialization()
    controller.initComplete()
    assert not controller.initialized
    assert not hasattr(controller.signalComm.initCompleteSignal, "last_emit")


def test_startup_cancelled_after_standby_does_not_load_operating_config():
    module = _load_module()
    cls = module._get_psu_driver_class()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.connected = True
    driver.device_id = "test"
    driver.logger = logging.getLogger("psu-cancel-startup")
    cancel = threading.Event()
    calls = []
    driver.connect = lambda **kw: None
    driver.load_config = lambda index, **kw: (calls.append(index), cancel.set())
    driver._cleanup_initialize_failure = lambda timeout: calls.append("cleanup")
    with pytest.raises(RuntimeError, match="cancelled by OFF"):
        driver.initialize(standby_config=1, operating_config=2, cancel=cancel)
    assert calls == [1, "cleanup"]


def test_config_load_reapplies_interlock_setting(setup):
    _module, controller, device, _messages = setup
    controller.controllerParent.operating_config = 7
    device.load_config = lambda *a, **kw: setattr(device, "interlocks", (False, False))
    controller.loadOperatingConfigNow()
    assert device.interlocks == (True, True)


def test_manual_apply_requires_disable_before_changing_setpoints(setup):
    _module, controller, device, messages = setup
    device.enabled = True
    device.outputs = (True, False)
    device.set_output_enabled = lambda *a, **kw: None  # ACK, but command ignored.
    controller.applyManualState(manual())
    assert not any(call[0] in {"voltage", "current", "range"} for call in device.calls)
    assert ("global", True) not in device.calls
    assert any("not confirmed disabled before changing setpoints" in msg for msg in messages)


def test_manual_mode_requires_channel_disable_before_global_enable(setup):
    _module, controller, device, _messages = setup
    device.outputs = (True, False)
    device.set_output_enabled = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="not confirmed disabled before manual mode"):
        controller._start_manual_mode(timeout_s=0.1, cancel=controller._output_cancel)
    assert ("global", True) not in device.calls


def test_ramp_target_is_checked_against_limit_before_enable(setup):
    _module, controller, device, messages = setup
    device.get_channel_voltage_limits = lambda ch, **kw: (device.voltages[ch], 200.0)
    controller.applyManualState(manual(300))
    assert not device.enabled
    assert ("global", True) not in device.calls
    assert any("verified hardware limit" in msg for msg in messages)


def test_off_interrupts_discharge_wait_without_switching_relay(setup):
    _module, controller, device, _messages = setup

    def measured(channel, **kwargs):
        controller._cancel_output_commands()
        return 1000.0

    device.get_channel_measured_voltage = measured
    state = manual()
    state["full_range_enabled"][0] = True
    controller.applyManualState(state)
    assert not any(call[0] == "range" for call in device.calls)
    assert ("global", True) not in device.calls


def test_missing_discharge_readback_refuses_range_switch(setup):
    _module, controller, device, _messages = setup
    device.get_channel_measured_voltage = None
    state = manual()
    state["full_range_enabled"][0] = True
    controller.applyManualState(state)
    assert not any(call[0] == "range" for call in device.calls)
    assert not device.enabled


@pytest.mark.parametrize("quantity", ["voltage", "current"])
@pytest.mark.parametrize("limit", [None, float("nan"), float("inf"), -1.0])
def test_positive_commands_require_valid_hardware_limits(quantity, limit):
    module = _load_module()
    cls = module._get_psu_driver_class()._PROCESS_CONTROLLER_CLASS
    base = importlib.import_module(cls.__module__).PSUBase
    driver = cls.__new__(cls)
    driver.connected = True
    driver._transport_poisoned = False
    driver.thread_lock = threading.Lock()
    driver.logger = logging.getLogger("psu-limit-regression")
    driver._read_device_limit = lambda *a: limit
    writes = []
    driver._call_locked_with_timeout = lambda method, timeout, step, *args: writes.append(args) or 0
    setter = getattr(driver, f"set_channel_{quantity}")
    with pytest.raises(RuntimeError, match="Cannot verify"):
        setter(0, 100, timeout_s=0.1)
    assert writes == []
    setter(0, 0.0, timeout_s=0.1)
    assert len(writes) == 1 and writes[0][-1] == 0.0
    assert hasattr(base, f"set_psu_output_{quantity}")
