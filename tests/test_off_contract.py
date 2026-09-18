"""OFF needs hardware confirmation AND a closed port; failed OFF stays retryable."""
import importlib
from types import SimpleNamespace

import pytest

from test_shutdown_confirmation_regressions import FAMILIES, controller_for, runtime_for


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("failure", ["disable", "close"])
def test_shutdown_failure_keeps_backend_and_next_off_can_retry(family, failure):
    module, controller, _ = controller_for(family)
    parent = controller.controllerParent
    parent.onAction = SimpleNamespace(state=False)
    parent.isOn = lambda: parent.onAction.state
    controller.initialized = True
    controller.main_state = "ST_ON"
    calls = []
    device = SimpleNamespace(connected=True, recover=False)

    def shutdown(**kwargs):
        calls.append("shutdown")
        if not device.recover:
            return False
        device.connected = False
        return True

    def disconnect(**kwargs):
        calls.append("disconnect")
        if not device.recover:
            return False
        device.connected = False
        return True

    device.shutdown = shutdown
    device.disconnect = disconnect
    device.close = lambda: calls.append("dispose")
    controller.device = device
    if family == "psu":
        controller._perform_shutdown_sequence_unlocked = lambda **kw: []
        controller._confirm_shutdown_unlocked = lambda **kw: (
            device.recover or failure == "close", "simulated disable failure"
        )
    assert controller.shutdownCommunication() is False
    assert controller.device is device, "failure must not discard the only retry path"
    assert controller.initialized, "Explorer must still warn before closing"
    assert parent.onAction.state, "the next click must request OFF, not ON"
    assert controller.main_state == "Shutdown unconfirmed"
    assert "dispose" not in calls
    if family == "psu" and failure == "disable":
        assert "disconnect" not in calls, "do not close before confirming the outputs"
    device.recover = True
    parent.onAction.state = False
    assert controller.shutdownCommunication() is True
    assert controller.device is None
    assert not controller.initialized
    assert controller.main_state == "Disconnected"


@pytest.mark.parametrize("family", ["ampr", "amx", "amx_hd", "dmmr", "psu", "esi"])
def test_runtime_does_not_close_port_if_disable_is_unconfirmed(family):
    driver = runtime_for(family)
    closes = []
    if family == "ampr":
        driver.scan_modules = lambda **kw: {}
        driver.enable_psu = lambda *a, **kw: (driver.NO_ERR, True)
    elif family in {"amx", "amx_hd", "psu"}:
        driver.set_device_enabled = lambda *a, **kw: None
        driver.get_device_enabled = lambda **kw: True
        driver.set_output_enabled = lambda *a, **kw: None
        driver.get_output_enabled = lambda **kw: (True, True)
    elif family == "dmmr":
        driver.set_enable = driver.set_automatic_current = lambda *a, **kw: driver.NO_ERR
        driver.get_enable = driver.get_automatic_current = lambda **kw: (driver.NO_ERR, True)
    else:
        base = importlib.import_module(type(driver).__module__).ESIBase
        driver.force_safe_off = lambda **kw: (_ for _ in ()).throw(RuntimeError("OFF unconfirmed"))
        original = base.close_port
        base.close_port = lambda self: closes.append("close") or 0
        try:
            with pytest.raises(RuntimeError):
                driver.disconnect(timeout_s=0.1)
        finally:
            base.close_port = original
        assert driver.connected
        assert closes == []
        return
    driver.disconnect = lambda **kw: closes.append("close") or True
    with pytest.raises(RuntimeError):
        driver.shutdown()
    assert closes == []
    assert driver.connected


@pytest.mark.parametrize("family", FAMILIES)
def test_refresh_cannot_erase_unconfirmed_shutdown(family):
    _module, controller, _ = controller_for(family)
    controller.main_state = "Shutdown unconfirmed"
    controller.initialized = True
    controller.device = SimpleNamespace(
        NO_ERR=0, get_state=lambda **kw: (0, 1, "ST_ON"),
        collect_housekeeping=lambda **kw: {"main_state": {"name": "ST_ON"}},
    )
    refresh = getattr(controller, "_update_state", None)
    if callable(refresh):
        refresh()
    snapshot = getattr(controller, "_apply_snapshot", None)
    if callable(snapshot):
        snapshot({"main_state": {"name": "ST_ON"}})
    assert controller.main_state == "Shutdown unconfirmed"
    assert controller.initialized


@pytest.mark.parametrize("failure", ["lost_ack", "startup_timeout"])
def test_ampr_runtime_keeps_port_after_failed_initialize(failure):
    driver = runtime_for("ampr")
    closed = []
    driver.disconnect = lambda: closed.append(True)
    driver.format_status = str

    def call(_fn, _timeout, step, *args):
        if step == "get_state_before_initialize":
            return 0, 0, "ST_STBY"
        if step == "get_scanned_module_state":
            return 0, False, False
        if step == "enable_psu":
            if failure == "lost_ack":
                raise RuntimeError("lost acknowledgement")
            return 0, True
        if step == "disable_psu_after_initialize_failure":
            return 0, True  # Even native cleanup may leave the PSU enabled.
        raise AssertionError(step)

    driver._call_locked_with_timeout = call
    with pytest.raises(RuntimeError):
        driver.initialize(timeout_s=0)
    assert driver.connected and driver._dll_port_claimed
    assert not closed, "the plugin must still be able to verify/retry OFF"


def test_esi_off_disconnects_and_next_on_uses_reinitialization():
    module, controller, _ = controller_for("esi")
    parent = controller.controllerParent
    parent.onAction = SimpleNamespace(state=False)
    parent.isOn = lambda: parent.onAction.state
    controller.initialized = True
    calls = []
    controller.device = SimpleNamespace(
        disconnect=lambda **kw: calls.append("safe_off_and_disconnect") or True,
        close=lambda: None,
    )
    controller.toggleOn()
    assert controller.main_state == "Disconnected"
    assert not controller.initialized
    assert controller.device is None
    device = object.__new__(module.ESIDevice)
    device.controller, device.onAction, device.loading = controller, parent.onAction, False
    device._sync_local_on_action = lambda: None
    device.isOn = parent.isOn
    device.getChannels = lambda: []
    device.initializeCommunication = lambda: calls.append("reinitialize")
    module.ESIDevice.setOn(device, True)
    assert calls[-1] == "reinitialize"


@pytest.mark.parametrize("failure", ["initialize", "ramp", "unexpected_state"])
@pytest.mark.parametrize("disable_result", [True, None, False, "exception"])
def test_ampr_startup_failure_never_claims_off_without_confirmation(failure, disable_result):
    module, controller, messages = controller_for("ampr")
    parent = controller.controllerParent
    parent.onAction = SimpleNamespace(state=True)
    parent.isOn = lambda: parent.onAction.state
    parent.updateValues = lambda **kw: None
    parent.ramp_rate_v_s = 0.0
    controller.initialized = True
    controller._refresh_module_scan = lambda: None
    controller._channel_target_voltages = lambda **kw: {(0, 1): 10.0}
    controller._apply_target_voltages = lambda *a, **kw: None
    controller._apply_target_voltages_locked = lambda *a, **kw: None
    controller._sync_status_to_gui = lambda: None
    module.DeviceController.toggleOn = lambda self: None

    def initialize(**kwargs):
        if failure == "initialize":
            raise RuntimeError("lost startup ACK; gate may be enabled")

    def ramp(**kwargs):
        if failure == "ramp":
            raise RuntimeError("setpoint write failed")

    def disable(enabled):
        assert enabled is False
        if disable_result == "exception":
            raise RuntimeError("OFF failed")
        return 0, disable_result

    controller._ramp_target_voltages = ramp
    controller._update_state = lambda: setattr(
        controller, "main_state", "ST_STBY" if failure == "unexpected_state" else "ST_ON"
    )
    controller.device = SimpleNamespace(
        NO_ERR=0, connected=True, initialize=initialize, enable_psu=disable,
        disconnect=lambda: True, close=lambda: None,
    )
    backend = controller.device
    controller.toggleOn()
    if disable_result is False:
        assert not parent.onAction.state
        assert controller.main_state == "Disconnected"
        assert not controller.initialized
    else:
        assert parent.onAction.state
        assert controller.main_state == "Shutdown unconfirmed"
        assert controller.initialized and controller.device is backend
        assert not any("cleanup disabled the PSU" in message for message in messages)
