"""An absent backend or a successful close must never stand in for a safe stop."""

import importlib
import logging
import threading
import types

import pytest

from psu_fakes import StatefulPSU
from test_ampr_plugin_channel_sync import _load_module as load_ampr
from test_amx_plugin_behavior import _load_module as load_amx
from test_amx_hd_plugin_behavior import _load_hd_plugin_module as load_hd
from test_dmmr_plugin_behavior import _load_module as load_dmmr
from test_esi_plugin_behavior import _load_plugin as load_esi
from test_psu_plugin_behavior import _load_module as load_psu

FAMILIES = {
    "ampr": (load_ampr, "AMPRController", "_get_ampr_driver_class"),
    "amx": (load_amx, "AMXController", "_get_amx_driver_class"),
    "amx_hd": (load_hd, "AMXHDController", "_get_amx_driver_class"),
    "dmmr": (load_dmmr, "DMMRController", "_get_dmmr_driver_class"),
    "esi": (load_esi, "ESIController", "_get_esi_driver_class"),
    "psu": (load_psu, "PSUController", "_get_psu_driver_class"),
}


def controller_for(family):
    loader, name, _getter = FAMILIES[family]
    module = loader()
    parent = types.SimpleNamespace(
        name=family, getChannels=lambda: [], isOn=lambda: False,
        connect_timeout_s=0.1, poll_timeout_s=0.1, startup_timeout_s=0.1,
    )
    controller = getattr(module, name)(parent)
    controller.lock = threading.Lock()
    controller.errorCount = 0
    messages = []
    controller.print = lambda msg, **kwargs: messages.append(msg)
    return module, controller, messages


def runtime_for(family):
    loader, _name, getter = FAMILIES[family]
    module = loader()
    cls = getattr(module, getter)()._PROCESS_CONTROLLER_CLASS
    driver = cls.__new__(cls)
    driver.connected = True
    driver._transport_poisoned = False
    driver._transport_error = None
    driver._dll_port_claimed = True
    driver.thread_lock = threading.Lock()
    driver.device_id = "shutdown-regression"
    driver.logger = logging.getLogger("shutdown-regression")
    driver.stop_housekeeping = lambda: None
    driver._set_port_claimed = lambda value: setattr(driver, "_dll_port_claimed", value)
    return driver


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("state", ["Shutdown unconfirmed", "Communication lost"])
def test_attention_state_survives_repeated_close_and_refresh(family, state):
    _module, controller, _messages = controller_for(family)
    controller.main_state = state
    controller.device = None
    for _ in range(3):
        assert controller.shutdownCommunication() is False
        controller.closeCommunication()
        refresh = getattr(controller, "_update_state", None)
        if callable(refresh):
            refresh()
        assert controller.main_state != "Disconnected"
        assert controller.main_state in {"Shutdown unconfirmed", "Communication lost"}


@pytest.mark.parametrize("family", ["ampr", "amx", "amx_hd", "dmmr"])
@pytest.mark.parametrize("result", [None, False])
def test_missing_positive_driver_confirmation_is_not_success(family, result):
    _module, controller, messages = controller_for(family)
    calls = []
    controller.device = types.SimpleNamespace(
        shutdown=lambda **kwargs: calls.append("shutdown") or result,
        disconnect=lambda: True,
        close=lambda: None,
    )
    assert controller.shutdownCommunication() is False
    assert controller.main_state == "Shutdown unconfirmed"
    assert calls == ["shutdown"]
    assert not any("sequence completed" in msg for msg in messages)


@pytest.mark.parametrize("family", FAMILIES)
def test_already_confirmed_disconnection_remains_idempotent(family):
    _module, controller, _messages = controller_for(family)
    assert controller.device is None
    assert controller.shutdownCommunication() is True
    assert controller.shutdownCommunication() is True
    assert controller.main_state == "Disconnected"


@pytest.mark.parametrize("family", ["amx", "amx_hd"])
@pytest.mark.parametrize("disable_works", [True, False])
def test_standby_must_not_leave_amx_enabled(family, disable_works):
    driver = runtime_for(family)
    enabled = True
    calls = []
    driver.load_config = lambda number, **kw: calls.append(("load", number))
    driver.get_device_enabled = lambda **kw: enabled

    def disable(value, **kwargs):
        nonlocal enabled
        calls.append(("enable", value))
        if disable_works:
            enabled = value

    driver.set_device_enabled = disable
    driver.disconnect = lambda **kw: calls.append(("disconnect",)) or True
    if disable_works:
        assert driver.shutdown(standby_config=3, disable_device=False) is True
        assert enabled is False
    else:
        with pytest.raises(RuntimeError, match="disable verification"):
            driver.shutdown(standby_config=3, disable_device=False)
    assert ("enable", False) in calls
    if disable_works:
        assert calls[-1] == ("disconnect",)
    else:
        assert ("disconnect",) not in calls


@pytest.mark.parametrize("family", ["amx", "amx_hd"])
def test_explicit_amx_disable_is_also_verified(family):
    driver = runtime_for(family)
    driver.set_device_enabled = lambda *a, **k: None
    driver.get_device_enabled = lambda **k: True
    driver.disconnect = lambda **k: True
    with pytest.raises(RuntimeError, match="remained enabled"):
        driver.shutdown()


@pytest.mark.parametrize("enabled", [True, None, False])
def test_ampr_checks_returned_enable_flag(enabled):
    driver = runtime_for("ampr")
    driver.scan_modules = lambda **kw: {}
    driver.enable_psu = lambda value, **kw: (0, enabled)
    driver.disconnect = lambda: True
    if enabled is False:
        assert driver.shutdown() is True
    else:
        with pytest.raises(RuntimeError, match="disable was not confirmed"):
            driver.shutdown()


@pytest.mark.parametrize("family", ["ampr", "amx", "amx_hd", "psu", "dmmr"])
def test_disconnected_driver_cannot_confirm_physical_shutdown(family):
    driver = runtime_for(family)
    driver.connected = False
    assert driver.shutdown() is False


@pytest.mark.parametrize("enabled", [False, True, None])
def test_dmmr_verifies_disable_readback(enabled):
    driver = runtime_for("dmmr")
    driver.set_automatic_current = lambda *a, **k: 0
    driver.set_enable = lambda *a, **k: 0
    driver.get_automatic_current = lambda **k: (0, False)
    driver.get_enable = lambda **k: (0, enabled)
    driver.disconnect = lambda: True
    if enabled is False:
        assert driver.shutdown() is True
    else:
        with pytest.raises(RuntimeError, match="disable was not confirmed"):
            driver.shutdown()


def test_dmmr_never_closes_in_parallel_with_a_poisoned_dll_call(monkeypatch):
    driver = runtime_for("dmmr")
    base = importlib.import_module(type(driver).__module__).DMMRBase
    calls = []
    monkeypatch.setattr(base, "close_port", lambda self: calls.append("close") or 0)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def blocked():
        started.set()
        release.wait(3)
        finished.set()
        return 0

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            driver._call_locked_with_timeout(blocked, 0.02, "blocked read")
        assert started.is_set()
        assert driver.disconnect() is False
        assert driver.disconnect() is False
        with pytest.raises(RuntimeError, match="transport is unusable"):
            driver.shutdown()
        assert calls == []
        assert driver._dll_port_claimed
    finally:
        release.set()
        assert finished.wait(3)


def test_dmmr_not_connected_response_is_not_a_confirmed_stop():
    driver = runtime_for("dmmr")
    calls = []
    driver.set_automatic_current = lambda *a, **k: calls.append("automatic") or driver.ERR_NOT_CONNECTED
    driver.set_enable = lambda *a, **k: calls.append("enable") or driver.ERR_NOT_CONNECTED
    driver.get_automatic_current = lambda **k: (driver.ERR_NOT_CONNECTED, False)
    driver.get_enable = lambda **k: (driver.ERR_NOT_CONNECTED, False)
    driver.disconnect = lambda: calls.append("disconnect") or True
    with pytest.raises(RuntimeError, match="shutdown incomplete"):
        driver.shutdown()
    assert calls == ["automatic", "enable"]
    assert driver.connected


@pytest.mark.parametrize("snapshot", [{}, {"device_enabled": False}, {"output_enabled": (False, False)}])
def test_psu_missing_readbacks_cannot_confirm_shutdown(snapshot):
    _module, controller, _messages = controller_for("psu")
    controller.device = StatefulPSU()
    controller.device.collect_housekeeping = lambda **kw: snapshot
    assert controller.shutdownCommunication() is False
    assert controller.main_state == "Shutdown unconfirmed"


def test_psu_standby_driver_checks_both_gates():
    driver = runtime_for("psu")
    fake = StatefulPSU()
    fake.enabled = True
    fake.outputs = (True, True)
    for name in ("get_output_enabled", "set_output_enabled", "get_device_enabled", "set_device_enabled"):
        setattr(driver, name, getattr(fake, name))
    driver.load_config = lambda *a, **k: None
    driver.disconnect = lambda **k: True
    assert driver.shutdown(standby_config=7, disable_outputs=False, disable_device=False) is True
    assert fake.outputs == (False, False) and fake.enabled is False
