"""ESI driver runtime helpers shared by the ESI driver tests."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
import sys
import threading

import pytest

RUNTIME_DIR = Path(__file__).resolve().parents[2] / "esi" / "vendor" / "runtime"
RUNTIME_NAME = "_esi_driver_test_runtime"


def load_runtime():
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
def esi_driver_modules():
    runtime = load_runtime()
    driver_module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    base_module = sys.modules[f"{RUNTIME_NAME}.esi.esi_base"]
    driver_module._ESIController._active_connections.clear()
    yield runtime, driver_module, base_module
    driver_module._ESIController._active_connections.clear()


def make_controller(driver_module):
    controller = object.__new__(driver_module._ESIController)
    controller.device_id = "test_esi"
    controller.com = 14
    controller.baudrate = 230400
    controller.port_num = 0
    controller._dll_port_claimed = False
    controller.connected = False
    controller._transport_poisoned = False
    controller._transport_error = None
    controller._module_inventory = {}
    controller._hv_measurement_requests = {}
    controller.thread_lock = threading.Lock()
    controller.logger = logging.getLogger("test_esi_driver")
    controller.err_dict = {"-15": "Wrong argument"}
    return controller
