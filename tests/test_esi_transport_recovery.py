"""Recover CGC receive errors without replaying an energizing command."""

import threading

import pytest

from support.esi_driver import make_controller


def test_receive_error_purges_before_next_read_without_replaying_hv(esi_driver_modules, monkeypatch):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    calls = []
    desynchronized = [False]

    def set_target(self, address, target):
        calls.append(("target", address, target))
        desynchronized[0] = True
        return -10

    def purge(self):
        calls.append(("purge",))
        desynchronized[0] = False
        return 0

    def read_enable(self):
        calls.append(("read",))
        return (-13, False) if desynchronized[0] else (0, True)

    monkeypatch.setattr(base_module.ESIBase, "set_hv_supply_target_output_voltage", set_target)
    monkeypatch.setattr(base_module.ESIBase, "get_enable", read_enable)
    monkeypatch.setattr(base_module.ESIBase, "purge", purge)
    with pytest.raises(RuntimeError, match="-10"):
        driver.set_hv_module_target(1, 700., timeout_s=.5)
    result = driver._call_locked_with_timeout(base_module.ESIBase.get_enable, .5, "read", driver)
    assert result == (0, True)
    assert calls == [("target", 1, 700.), ("purge",), ("read",)]
    assert driver._communication_error is None


def test_failed_purge_does_not_send_another_output_command(esi_driver_modules, monkeypatch):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    driver._communication_error = "get_heat_ctrl_power_limit: -10"
    calls = []
    monkeypatch.setattr(base_module.ESIBase, "purge", lambda self: calls.append("purge") or -4)
    monkeypatch.setattr(base_module.ESIBase, "set_hv_supply_target_output_voltage",
                        lambda *args: calls.append("target") or 0)
    with pytest.raises(RuntimeError, match="purge"):
        driver.set_hv_module_target(1, 700., timeout_s=.5)
    assert calls == ["purge"]
    assert driver._communication_error is not None


def test_purge_never_runs_beside_a_blocked_native_call(esi_driver_modules, monkeypatch):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    entered, resume = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(base_module.ESIBase, "purge", lambda self: calls.append("purge") or 0)

    def blocked():
        entered.set()
        resume.wait(3.)

    try:
        with pytest.raises(RuntimeError, match="DLL call timed out"):
            driver._call_locked_with_timeout(blocked, .05, "read")
        assert entered.is_set()
        driver._communication_error = "receive error"
        with pytest.raises(RuntimeError, match="transport is unusable"):
            driver._call_locked_with_timeout(lambda: calls.append("new"), .5, "read")
        assert calls == []
    finally:
        resume.set()
        driver._pending_dll_call["thread"].join(3.)


def test_force_close_does_not_purge_or_command_outputs(esi_driver_modules, monkeypatch):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    driver._set_port_claimed(True)
    driver._communication_error = "get_enable: -13"
    calls = []
    monkeypatch.setattr(base_module.ESIBase, "purge", lambda self: calls.append("purge") or -4)
    monkeypatch.setattr(base_module.ESIBase, "close_port", lambda self: calls.append("close") or 0)
    assert driver.force_close_transport(timeout_s=.5)
    assert calls == ["close"]
    assert not driver.connected and not driver._dll_port_claimed
    assert not type(driver)._active_connections


def test_crash_resume_connect_does_not_run_safe_off_or_enable_outputs(esi_driver_modules, monkeypatch):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    calls = []
    base = base_module.ESIBase
    monkeypatch.setattr(base, "open_port", lambda self, com: calls.append("open") or 0)
    monkeypatch.setattr(base, "set_comspeed", lambda self, baud: calls.append("baud") or (0, baud))
    monkeypatch.setattr(base, "get_dev_type", lambda self: calls.append("identity") or (0, self.DEVICE_TYPE))
    driver.discover_modules = lambda **kwargs: calls.append("inventory") or {0: {}, 1: {}, 2: {}}
    driver._prepare_safe_inventory = lambda timeout: pytest.fail("resume must not change output gates")
    driver.force_safe_off = lambda **kwargs: pytest.fail("resume must preserve the running outputs")
    assert driver.connect(timeout_s=.5, preserve_outputs=True)
    assert calls == ["open", "baud", "identity", "inventory"]


def test_receive_error_logs_vendor_latches_before_the_purge_clears_them(esi_driver_modules, monkeypatch, caplog):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    calls = []
    base = base_module.ESIBase
    monkeypatch.setattr(base, "set_hv_supply_target_output_voltage",
                        lambda self, address, target: calls.append("target") or -10)
    monkeypatch.setattr(base, "get_io_state", lambda self: calls.append("io_state") or (0, -12))
    monkeypatch.setattr(base, "get_comm_error", lambda self: calls.append("comm_error") or (0, 0x10))
    monkeypatch.setattr(base, "get_io_state_message", lambda self, value: f"io {value}")
    monkeypatch.setattr(base, "get_comm_error_message", lambda self, value: "framing error")
    monkeypatch.setattr(base, "purge", lambda self: calls.append("purge") or 0)
    monkeypatch.setattr(base, "get_enable", lambda self: calls.append("read") or (0, False))
    with caplog.at_level("WARNING", logger="test_esi_driver"):
        with pytest.raises(RuntimeError, match="-10"):
            driver.set_hv_module_target(1, 700., timeout_s=.5)
    warnings = [record.getMessage() for record in caplog.records if "vendor serial diagnostics" in record.getMessage()]
    assert len(warnings) == 1
    assert "get_io_state=-12 (io -12)" in warnings[0]
    assert "get_comm_error=0x00000010 (framing error)" in warnings[0]
    driver._call_locked_with_timeout(base.get_enable, .5, "read", driver)
    assert calls == ["target", "io_state", "comm_error", "purge", "read"]


def test_unreadable_vendor_latches_keep_the_original_error(esi_driver_modules, monkeypatch, caplog):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver.connected = True
    base = base_module.ESIBase

    def missing(self):
        raise AttributeError("no export")

    monkeypatch.setattr(base, "set_hv_supply_target_output_voltage", lambda self, address, target: -10)
    monkeypatch.setattr(base, "get_io_state", missing)
    monkeypatch.setattr(base, "get_comm_error", lambda self: (0, 0))
    monkeypatch.setattr(base, "get_comm_error_message", lambda self, value: missing(self))
    with caplog.at_level("WARNING", logger="test_esi_driver"):
        with pytest.raises(RuntimeError, match="-10"):
            driver.set_hv_module_target(1, 700., timeout_s=.5)
    message = next(record.getMessage() for record in caplog.records if "vendor serial diagnostics" in record.getMessage())
    assert "get_io_state=unavailable (AttributeError: no export)" in message
    assert "get_comm_error=0x00000000 (message unavailable (AttributeError))" in message


def test_poisoned_transport_skips_the_vendor_latches(esi_driver_modules, monkeypatch, caplog):
    _, module, base_module = esi_driver_modules
    driver = make_controller(module)
    driver._transport_poisoned = True
    monkeypatch.setattr(base_module.ESIBase, "get_io_state",
                        lambda self: pytest.fail("a poisoned transport must not be called"))
    with caplog.at_level("WARNING", logger="test_esi_driver"):
        driver._capture_serial_diagnostics("read", -10)
    assert "get_io_state: skipped (transport poisoned)" in caplog.text
