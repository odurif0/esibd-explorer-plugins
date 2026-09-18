"""Exercise the plugin OFF path with the actual discharge-checking runtime."""
from types import SimpleNamespace

import pytest

import test_esi_discharge
from test_esi_plugin_behavior import _load_plugin
from test_esi_current_measurements import snapshot

rig = test_esi_discharge.rig  # Reuse the shared hardware-free pytest fixture.


@pytest.fixture
def control(rig):
    module = _load_plugin()
    action = SimpleNamespace(state=False)  # the operator has just requested OFF
    parent = SimpleNamespace(onAction=action, isOn=lambda: action.state,
                             connect_timeout_s=.5, poll_timeout_s=.5,
                             getChannels=lambda: [], com=16)
    controller = module.ESIController(parent)
    controller.initialized = controller.acquiring = True
    controller.main_state = "ST_ON"
    controller.device = rig.driver
    controller.errorCount = 0
    messages, statuses = [], []
    controller.print = lambda message, **kw: messages.append(message)
    controller._sync_status = lambda: statuses.append((controller.main_state, action.state))
    return SimpleNamespace(module=module, controller=controller, parent=parent,
                           statuses=statuses, messages=messages)


def test_complete_shutdown_with_runtime(control, rig):
    c = control.controller
    assert c.shutdownCommunication()
    assert (control.module._ESI_STOPPING, True) in control.statuses
    assert control.statuses[-1] == ("Disconnected", False)
    assert c.device is None and not c.initialized and not c.acquiring
    assert rig.calls[-1] == ("close",)
    assert rig.calls.count(("disable",)) == 1
    assert not c.discharge_readings


def test_voltage_still_high_keeps_off_retryable(control, rig):
    rig.values[2, True] = [-100.]
    c = control.controller
    assert not c.shutdownCommunication()
    assert c.main_state == "Shutdown unconfirmed"
    assert c.initialized and c.device is rig.driver
    assert control.parent.onAction.state
    assert not c.acquiring
    assert ("close",) not in rig.calls
    c._apply_snapshot(snapshot())
    c._on_discharge_progress({"modules": {}})
    assert c.main_state == "Shutdown unconfirmed"
    assert any("discharge" in message for message in control.messages)
    rig.values[2, True] = [-.3]
    assert c.shutdownCommunication()
    assert c.main_state == "Disconnected" and not control.parent.onAction.state


def test_background_readback_cannot_erase_shutdown_check(control, rig):
    c = control.controller
    c.main_state = control.module._ESI_STOPPING
    c.discharge_readings = {1: {"positive_v": 20., "negative_v": -15., "measured_a": 1e-9}}
    c._apply_snapshot(snapshot())
    c.readNumbers()
    assert c.main_state == control.module._ESI_STOPPING
    assert c.discharge_readings[1]["positive_v"] == 20.
    assert not rig.calls


@pytest.mark.parametrize("state", ["stopping", "uncertain"])
def test_on_cannot_bypass_pending_shutdown(control, rig, state):
    c = control.controller
    c.main_state = control.module._ESI_STOPPING if state == "stopping" else "Shutdown unconfirmed"
    control.parent.onAction.state = True
    c._output_cancel.set()
    c.toggleOn()
    assert c._output_cancel.is_set()
    assert not rig.calls
    assert c.main_state != "Disconnected"


def test_initialization_failure_keeps_backend_if_discharge_cannot_be_verified(control, rig, monkeypatch):
    c = control.controller
    c.device = None
    c.main_state = "Disconnected"
    monkeypatch.setattr(control.module, "_get_esi_driver_class", lambda: lambda **kw: rig.driver)
    rig.driver.connect = lambda **kw: (_ for _ in ()).throw(RuntimeError("startup error"))
    control.parent.baudrate = 230400
    rig.values[1, False] = [100.]
    c.runInitialization()
    assert c.device is rig.driver and c.initialized
    assert c.main_state == "Shutdown unconfirmed"
    assert control.parent.onAction.state
    assert ("close",) not in rig.calls
    assert not c.initializing


def test_reinitialize_must_not_discard_an_unconfirmed_backend(control, rig, monkeypatch):
    c = control.controller
    rig.values[1, False] = [100.]
    constructed = []
    monkeypatch.setattr(control.module, "_get_esi_driver_class", lambda: constructed.append(True))
    c.runInitialization()
    assert c.device is rig.driver and c.initialized
    assert c.main_state == "Shutdown unconfirmed"
    assert not constructed


def test_failed_on_rollback_requires_discharge_not_just_disable(control, rig):
    rig.values[1, False] = [50.]
    confirmed, _ = control.controller._force_safe_off_after_failure()
    assert not confirmed
    assert control.controller.device is rig.driver
    assert control.controller.main_state == "Shutdown unconfirmed"
    assert ("close",) not in rig.calls


def test_late_poll_after_confirmed_close_does_not_resurrect_on(control, rig):
    c = control.controller
    control.parent.onAction.state = True

    def poll(**kw):
        assert c.shutdownCommunication()
        return snapshot()

    rig.driver.collect_diagnostics = poll
    c.readNumbers()
    assert c.main_state == "Disconnected"
    assert c.device is None and not c.initialized
    assert control.parent.onAction.state is False
