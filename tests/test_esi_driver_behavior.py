"""Safety and validation checks for the bundled ESI driver."""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
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
    controller.allow_negative = False
    controller.connected = False
    controller._transport_poisoned = False
    controller._transport_error = None
    controller._module_inventory = {}
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

    monkeypatch.setattr(base_module.ESIBase, "open_port", lambda self, com: calls.append(("open", com)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_comspeed", lambda self, baud: calls.append(("baud", baud)) or (0, baud))
    monkeypatch.setattr(base_module.ESIBase, "get_dev_type", lambda self: calls.append(("device_type",)) or (0, self.DEVICE_TYPE))
    monkeypatch.setattr(base_module.ESIBase, "set_activation_state", lambda self, state: calls.append(("global", state)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_enable", lambda self, state: calls.append(("enable", state)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_hv_supply_target_output_voltage", lambda self, address, value: calls.append(("target", address, value)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "set_module_activation_state", lambda self, address, state: calls.append(("module", address, state)) or 0)
    monkeypatch.setattr(base_module.ESIBase, "update_module_presence", lambda self: 0)
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_presence",
        lambda self: (0, True, 3, [0, 0, 1, 1, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (0, self.MODULE_HVPS_TYPE),
    )

    assert controller.connect(timeout_s=0.5) is True
    assert calls[:3] == [("open", 14), ("baud", 230400), ("device_type",)]
    assert calls[3:11] == [
        ("global", False),
        ("enable", True),
        ("global", False),
        ("enable", True),
        ("target", 2, 0.0),
        ("module", 2, False),
        ("target", 3, 0.0),
        ("module", 3, False),
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
        lambda self: (0, True, 2, [0, 0, 1, 0, 1]),
    )
    monkeypatch.setattr(
        base_module.ESIBase,
        "get_module_dev_type",
        lambda self, address: (0, self.MODULE_HVPS_TYPE),
    )

    with pytest.raises(RuntimeError, match="module 3 was not detected"):
        controller.discover_modules(timeout_s=0.5)


def test_voltage_guard_enforces_3kv_and_explicit_negative_opt_in(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_voltage(3000) == 3000
    with pytest.raises(ValueError, match="3000 V hardware limit"):
        controller._validate_voltage(3000.1)
    with pytest.raises(ValueError, match="Negative ESI voltages are disabled"):
        controller._validate_voltage(-1)

    controller.allow_negative = True
    assert controller._validate_voltage(-3000) == -3000


def test_only_lab_hv_addresses_are_commandable(driver_modules):
    _runtime, driver_module, _base_module = driver_modules
    controller = _controller(driver_module)

    assert controller._validate_hv_address(2) == 2
    assert controller._validate_hv_address(3) == 3
    with pytest.raises(ValueError, match="must be one of"):
        controller._validate_hv_address(0)


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
