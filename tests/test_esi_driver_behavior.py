"""Safety and validation checks for the bundled ESI driver."""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import sys
import threading
import types
from pathlib import Path

import pytest


RUNTIME_DIR = Path(__file__).resolve().parents[1] / "esi" / "vendor" / "runtime"
RUNTIME_NAME = "_esi_driver_test_runtime"


def _load_runtime():
    for name in tuple(sys.modules):
        if name == RUNTIME_NAME or name.startswith(f"{RUNTIME_NAME}."):
            sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        RUNTIME_NAME,
        RUNTIME_DIR / "__init__.py",
        submodule_search_locations=[str(RUNTIME_DIR)],
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[RUNTIME_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def driver_modules():
    runtime = _load_runtime()
    driver_module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base_module = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"]
    driver_module._ESIController._connected_instance = None
    yield runtime, driver_module, base_module
    driver_module._ESIController._connected_instance = None


def _controller(driver_module):
    controller = object.__new__(driver_module._ESIController)
    controller.device_id = "test_esi"
    controller.com = 14
    controller.baudrate = 230400
    controller.connected = False
    controller._transport_poisoned = False
    controller._transport_error = None
    controller._module_inventory = {}
    controller._hv_measurement_requests = {}
    controller._hv_activation_ack_warned = set()
    controller.thread_lock = threading.Lock()
    controller.logger = logging.getLogger("test_esi_driver")
    controller.err_dict = {"-15": "Wrong argument"}
    return controller


def test_connect_validates_identity_and_forces_known_off_state(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    calls = []
    module_active = {1: True, 2: True}

    monkeypatch.setattr(base_module.ESIBase, "open_port", lambda self, com: calls.append(("open", com)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_comspeed", lambda self, baud: calls.append(("baud", baud)) or (0, baud))
    monkeypatch.setattr(base_module.ESIBase, "get_dev_type", lambda self: calls.append(("device_type",)) or (0, self.DEVICE_TYPE))
    monkeypatch.setattr(base_module.ESIBase, "set_enable", lambda self, state: calls.append(("enable", state)) or 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, value: calls.append(("heat_target", value)) or (0, value),
    )
    monkeypatch.setattr(base_module.ESIBase, "set_hv_supply_target_output_voltage", lambda self, address, value: calls.append(("target", address, value)) or 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda self, address, state: (
            calls.append(("module", address, state)),
            module_active.__setitem__(address, state),
            self.ERR_COMMAND_RECEIVE,
        )[-1],
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda self, address: (
            calls.append(("pwm", address)) or 0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            module_active[address],
            0,
        ),
    )
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 3, [1, 1, 1, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (
            0,
            self.MODULE_HTCTRL_TYPE if address == 0 else self.MODULE_HVPS_TYPE,
        ),
    )

    assert controller.connect(timeout_s=0.5) is True
    assert calls[:3] == [("open", 14), ("baud", 230400), ("device_type",)]
    assert calls[3:] == [
        ("enable", False),
        ("enable", True),
        ("heat_target", 0.0),
        ("target", 1, 0.0),
        ("module", 1, False),
        ("pwm", 1),
        ("target", 2, 0.0),
        ("module", 2, False),
        ("pwm", 2),
        ("enable", False),
    ]


def test_connect_rejects_wrong_controller_type(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    closed = []
    monkeypatch.setattr(base_module.ESIBase, "open_port", lambda self, com: 0)
    monkeypatch.setattr(base_module.ESIBase, "set_comspeed", lambda self, baud: (0, baud))
    monkeypatch.setattr(base_module.ESIBase, "get_dev_type", lambda self: (0, 0xFFFF))
    monkeypatch.setattr(base_module.ESIBase, "close_port", lambda self: closed.append(True) or 0)

    with pytest.raises(RuntimeError, match="device type mismatch"):
        controller.connect(timeout_s=0.5)

    assert closed == [True]
    assert controller.connected is False
    assert driver_module._ESIController._connected_instance is None


def test_discovery_requires_both_expected_hv_modules(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 2, [1, 1, 0, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (
            0,
            self.MODULE_HTCTRL_TYPE if address == 0 else self.MODULE_HVPS_TYPE,
        ),
    )

    with pytest.raises(RuntimeError, match="module 2 was not detected"):
        controller.discover_modules(timeout_s=0.5)


def test_voltage_guard_enforces_unsigned_3kv_vendor_range(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_voltage(3000) == 3000
    with pytest.raises(ValueError, match="between 0 and 3000 V"):
        controller._validate_voltage(3000.1)
    with pytest.raises(ValueError, match="finite"):
        controller._validate_voltage(float("nan"))
    with pytest.raises(ValueError, match="finite"):
        controller._validate_voltage(float("inf"))
    with pytest.raises(ValueError, match="between 0 and 3000 V"):
        controller._validate_voltage(-1)


def test_hv_ctypes_signatures_match_vendor_header(driver_modules):
    _runtime, _driver_module, base_module = driver_modules

    class FakeFunction:
        argtypes = None
        restype = None

    names = (
        "COM_ESI_CTRL_SetEnable",
        "COM_ESI_CTRL_GetEnable",
        "COM_ESI_CTRL_SetModuleActivationState",
        "COM_ESI_CTRL_GetModuleActivationState",
        "COM_ESI_CTRL_GetModuleLEDData",
        "COM_ESI_CTRL_SetHVsupplyMeasRanges",
        "COM_ESI_CTRL_GetHVsupplyOutputVoltage",
        "COM_ESI_CTRL_GetHVsupplyOutputCurrent",
        "COM_ESI_CTRL_GetHVsupplyPhase",
        "COM_ESI_CTRL_GetHVsupplyTargetOutputVoltage",
        "COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage",
        "COM_ESI_CTRL_GetHVsupplyParamsPWM",
    )
    base = object.__new__(base_module.ESIBase)
    base.esi_dll = types.SimpleNamespace(
        **{name: FakeFunction() for name in names}
    )

    base._configure_hv_dll_signatures()

    assert base.esi_dll.COM_ESI_CTRL_SetHVsupplyTargetOutputVoltage.argtypes == [
        ctypes.c_uint,
        ctypes.c_double,
    ]
    assert base.esi_dll.COM_ESI_CTRL_SetHVsupplyMeasRanges.argtypes == [
        ctypes.c_uint,
        ctypes.c_bool,
        ctypes.c_bool,
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetHVsupplyOutputVoltage.argtypes == [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_double),
    ]
    assert base.esi_dll.COM_ESI_CTRL_GetModuleLEDData.argtypes == [
        ctypes.c_uint,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_bool),
    ]
    assert all(getattr(base.esi_dll, name).restype is ctypes.c_int for name in names)


def test_module_target_api_is_unsigned_and_keeps_compatibility_alias(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    targets = {}

    def set_target(_self, address, value):
        calls.append(("set", address, value))
        targets[address] = value
        return 0

    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        set_target,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, address: (
            calls.append(("get", address)) or 0,
            targets[address],
        ),
    )

    assert controller.set_hv_module_target(1, 125.0, timeout_s=0.5) == 125.0
    assert controller.set_target_voltage(2, 250.0, timeout_s=0.5) == 250.0

    assert calls == [
        ("set", 1, 125.0),
        ("get", 1),
        ("set", 2, 250.0),
        ("get", 2),
    ]


def test_module_target_rejects_unconfirmed_value(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        lambda _self, _address, _value: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, _address: (0, 0.0),
    )

    with pytest.raises(RuntimeError, match="target verification failed"):
        controller.set_hv_module_target(1, 10.0, timeout_s=0.5)


def test_global_enable_is_written_then_verified(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    state = {"enabled": False}

    def set_enable(_self, enabled):
        calls.append(("set", enabled))
        state["enabled"] = enabled
        return 0

    monkeypatch.setattr(base_module.ESIBase, "set_enable", set_enable)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get",)) or 0, state["enabled"]),
    )

    assert controller.set_global_active(True, timeout_s=0.5) is True
    assert calls == [("set", True), ("get",)]


def test_global_enable_rejects_unconfirmed_state(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, _enabled: 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (0, False),
    )

    with pytest.raises(RuntimeError, match="enable verification failed"):
        controller.set_global_active(True, timeout_s=0.5)


def test_hv_measurement_selector_uses_physical_polarity_and_current_range(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda self, address, negative, high_current: calls.append(
            (address, negative, high_current)
        ) or 0,
    )

    selected = controller.select_hv_measurement(
        2,
        negative=True,
        high_current=False,
        timeout_s=0.5,
    )
    selected_again = controller.select_hv_measurement(
        2,
        negative=True,
        high_current=False,
        timeout_s=0.5,
    )

    assert selected is True
    assert selected_again is True
    assert calls == [(2, True, False)]


def test_hv_measurement_selector_caches_firmware_command_receive_failure(
    driver_modules,
    monkeypatch,
    caplog,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda self, address, negative, high_current: calls.append(
            (address, negative, high_current)
        ) or self.ERR_COMMAND_RECEIVE,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_output_voltage",
        lambda self, address: (0, True, 0.0),
    )

    with caplog.at_level(logging.WARNING, logger="test_esi_driver"):
        assert controller.select_hv_measurement(
            1, negative=False, timeout_s=0.5
        ) is False
        assert controller.select_hv_measurement(
            1, negative=False, timeout_s=0.5
        ) is False
        assert controller.select_hv_measurement(
            2, negative=True, timeout_s=0.5
        ) is False
        assert controller.select_hv_measurement(
            1, negative=True, timeout_s=0.5
        ) is False

    assert calls == [
        (1, False, False),
        (2, True, False),
        (1, True, False),
    ]
    assert "did not acknowledge measurement-channel selection" in caplog.text


def test_hv_measurement_selector_does_not_mask_transport_failure(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda self, address, negative, high_current: self.ERR_COMMAND_RECEIVE,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_output_voltage",
        lambda self, address: (self.ERR_COMMAND_RECEIVE, False, 0.0),
    )

    with pytest.raises(
        RuntimeError,
        match="verify_hv_readback_after_selector_rejection",
    ):
        controller.select_hv_measurement(1, negative=False, timeout_s=0.5)

    assert controller._hv_measurement_requests == {}


def test_hv_measurement_selector_keeps_other_failures_blocking(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_meas_ranges",
        lambda self, address, negative, high_current: self.ERR_COMMAND_WRONG,
    )

    with pytest.raises(RuntimeError, match=r"select_hv_measurement\(1\) failed"):
        controller.select_hv_measurement(1, negative=False, timeout_s=0.5)

    assert controller._hv_measurement_requests == {}


def test_only_lab_hv_addresses_are_commandable(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_hv_address(1) == 1
    assert controller._validate_hv_address(2) == 2
    with pytest.raises(ValueError, match="must be one of"):
        controller._validate_hv_address(3)

    assert controller._validate_controlled_address(0) == 0


def test_heat_output_disable_uses_zero_target_not_module_activation(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, state: calls.append(("enable", state)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get_enable",)) or 0, True),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, value: calls.append(("temperature", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda self, address, active: (_ for _ in ()).throw(
            AssertionError("Heat must not use module activation")
        ),
    )

    assert controller.set_output_active(0, True, timeout_s=0.5) is True
    assert controller.set_output_active(0, False, timeout_s=0.5) is False
    assert calls == [
        ("enable", True),
        ("get_enable",),
        ("temperature", 0.0),
    ]


def test_hv_output_state_uses_module_toggle_and_global_gate(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    module_state = {1: False}
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_enable",
        lambda _self, state: calls.append(("enable", state)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_enable",
        lambda _self: (calls.append(("get_enable",)) or 0, True),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_hv_supply_target_output_voltage",
        lambda _self, address, value: calls.append(("target", address, value)) or 0,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_target_output_voltage",
        lambda _self, address: (calls.append(("get_target", address)) or 0, 0.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, address, active: (
            calls.append(("module", address, active)),
            module_state.__setitem__(address, active),
            0,
        )[-1],
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, address: (
            calls.append(("pwm", address)) or 0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            module_state[address],
            0,
        ),
    )

    assert controller.set_output_active(1, True, timeout_s=0.5) is True
    assert controller.set_output_active(1, False, timeout_s=0.5) is False
    assert calls == [
        ("module", 1, True),
        ("pwm", 1),
        ("enable", True),
        ("get_enable",),
        ("target", 1, 0.0),
        ("get_target", 1),
        ("module", 1, False),
        ("pwm", 1),
    ]


def test_module_activation_accepts_missing_ack_only_when_pwm_confirms(
    driver_modules,
    monkeypatch,
    caplog,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    calls = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, address, active: calls.append(
            ("module", address, active)
        ) or controller.ERR_COMMAND_RECEIVE,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, address: (
            calls.append(("pwm", address)) or 0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            True,
            0,
        ),
    )

    with caplog.at_level(logging.WARNING, logger="test_esi_driver"):
        assert controller.set_hv_module_active(1, True, timeout_s=0.5) is True
        assert controller.set_hv_module_active(1, True, timeout_s=0.5) is True

    assert calls == [
        ("module", 1, True),
        ("pwm", 1),
        ("module", 1, True),
        ("pwm", 1),
    ]
    assert caplog.text.count("PWM status independently confirmed") == 1


def test_module_activation_rejects_unconfirmed_missing_ack(
    driver_modules,
    monkeypatch,
):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_module_activation_state",
        lambda _self, _address, _active: controller.ERR_COMMAND_RECEIVE,
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_hv_supply_params_pwm",
        lambda _self, _address: (
            0,
            1.0,
            0.5,
            0.0,
            0.0,
            0.0,
            0.0,
            False,
            0,
        ),
    )

    with pytest.raises(RuntimeError, match="activation verification failed"):
        controller.set_hv_module_active(1, True, timeout_s=0.5)


def test_discovery_rejects_wrong_heat_controller_type(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 3, [1, 1, 1, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (0, self.MODULE_HVPS_TYPE),
    )

    with pytest.raises(RuntimeError, match="module 0 type mismatch"):
        controller.discover_modules(timeout_s=0.5)


def test_heater_temperature_is_limited_by_hardware(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    applied = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_hw_limits",
        lambda self: (0, 24.0, 10.0, 200.0, 180.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_heater_temperature",
        lambda self, target: applied.append(target) or (0, target),
    )

    assert controller.set_heater_temperature(125.0, timeout_s=0.5) == 125.0
    assert applied == [125.0]
    with pytest.raises(ValueError, match="hardware maximum 180"):
        controller.set_heater_temperature(181.0, timeout_s=0.5)


def test_heat_limit_overrides_validate_reported_maxima(driver_modules, monkeypatch):
    _runtime, driver_module, base_module = driver_modules
    controller = _controller(driver_module)
    controller.connected = True
    applied = []
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_heat_ctrl_hw_limits",
        lambda self: (0, 24.0, 8.0, 150.0, 180.0),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_voltage_limit",
        lambda self, value: applied.append(("voltage", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_current_limit",
        lambda self, value: applied.append(("current", value)) or (0, value),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "set_heat_ctrl_power_limit",
        lambda self, value: applied.append(("power", value)) or (0, value),
    )

    assert controller.configure_heat_limits(
        voltage_v=20.0,
        power_w=120.0,
        timeout_s=0.5,
    ) == {"voltage_v": 20.0, "power_w": 120.0}
    assert applied == [("voltage", 20.0), ("power", 120.0)]
    with pytest.raises(ValueError, match="hardware maximum 24"):
        controller.configure_heat_limits(voltage_v=25.0, timeout_s=0.5)
    applied.clear()
    with pytest.raises(ValueError, match="hardware maximum 8"):
        controller.configure_heat_limits(
            voltage_v=20.0,
            current_a=9.0,
            timeout_s=0.5,
        )
    assert applied == []


def test_second_inline_controller_is_rejected(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    first = _controller(driver_module)
    second = _controller(driver_module)
    first.device_id = "first"
    second.device_id = "second"
    first._claim_single_instance()

    with pytest.raises(RuntimeError, match="single-instance"):
        second._claim_single_instance()


def test_process_rpc_budgets_cover_batched_dll_operations(driver_modules):
    runtime, _driver_module, _base_module = driver_modules
    driver = runtime.ESI

    assert driver._rpc_timeout_for("connect", {"timeout_s": 5.0}) == 160.0
    assert driver._rpc_timeout_for("collect_identity", {"timeout_s": 3.0}) == 90.0
    assert driver._rpc_timeout_for("collect_diagnostics", {"timeout_s": 3.0}) == 60.0
    assert driver._rpc_timeout_for("force_safe_off", {"timeout_s": 5.0}) == 50.0
    assert driver._rpc_timeout_for("disconnect", {"timeout_s": 5.0}) == 60.0
    assert driver._rpc_timeout_for("get_heat_configuration", {"timeout_s": 3.0}) == 45.0
