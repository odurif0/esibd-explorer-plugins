"""Instruments keep Windows from idle sleep while a port is claimed; simulated power API, no Windows needed."""
from __future__ import annotations

import ctypes
import importlib.util
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_esi_driver_behavior import RUNTIME_NAME, _controller, _load_runtime

ROOT = Path(__file__).resolve().parents[1]


class PowerApi:
    def __init__(self):
        self.calls, self.refuse, self.handles = [], False, 0

    def create(self, reason):
        self.handles += 1
        self.calls.append(("create", reason))
        return self.handles

    def set(self, handle, kind):
        self.calls.append(("set", handle, kind))
        return 0 if self.refuse else 1

    def clear(self, handle, kind):
        self.calls.append(("clear", handle, kind))
        return 1

    def close(self, handle):
        self.calls.append(("close", handle))
        return 1


@pytest.fixture
def runtime(monkeypatch):
    _load_runtime()
    common = sys.modules[f"{RUNTIME_NAME}._driver_common"]
    module = sys.modules[f"{RUNTIME_NAME}.esi.esi"]
    api = PowerApi()
    monkeypatch.setattr(common, "_power_api", lambda: api)
    yield SimpleNamespace(common=common, module=module, api=api)
    module._ESIController._active_connections.clear()


def test_request_is_set_once_and_cleared_with_its_handle(runtime):
    request = runtime.common.SystemAwakeRequest("ESIBD Explorer: test")
    assert request.hold(True) and request.hold(True) and request.held
    assert not request.hold(False) and not request.hold(False)
    assert runtime.api.calls == [("create", "ESIBD Explorer: test"), ("set", 1, 1), ("clear", 1, 1), ("close", 1)]


def test_refused_or_failing_request_never_raises(runtime, monkeypatch):
    runtime.api.refuse = True
    request = runtime.common.SystemAwakeRequest("refused")
    assert request.hold(True) is False and runtime.api.calls[-1] == ("close", 1)
    def broken():
        raise OSError("no power API")
    monkeypatch.setattr(runtime.common, "_power_api", broken)
    assert request.hold(True) is False and request.hold(False) is False


def test_without_windows_the_request_is_inert():
    _load_runtime()
    common = sys.modules[f"{RUNTIME_NAME}._driver_common"]
    if sys.platform != "win32":
        assert common.SystemAwakeRequest("inert").hold(True) is False


def test_reason_context_has_the_windows_x64_layout(runtime):
    context = runtime.common._reason_context_type()
    assert ctypes.sizeof(context) == 8 + 3 * ctypes.sizeof(ctypes.c_void_p)
    assert context.SimpleReasonString.offset == 8
    assert context(0, 1, "reason").SimpleReasonString == "reason"


def test_controller_holds_exactly_while_its_port_is_claimed(runtime):
    driver = _controller(runtime.module)
    driver._set_port_claimed(True)
    assert driver._system_awake.held
    assert runtime.api.calls[0] == ("create", "ESIBD Explorer: ESI test_esi on COM14 is connected")
    driver._set_port_claimed(True)  # Idempotent.
    driver._set_port_claimed(False)
    assert not driver._system_awake.held
    assert [call[0] for call in runtime.api.calls] == ["create", "set", "clear", "close"]


def test_reserving_a_port_holds_and_a_poisoned_claim_keeps_holding(runtime):
    driver = _controller(runtime.module)
    driver._reserve_open_port(1.)
    assert driver._dll_port_claimed and driver._system_awake.held
    driver._transport_poisoned = True
    driver._set_port_claimed(False)  # Uncertain owner: outputs may still be energized.
    assert driver._dll_port_claimed and driver._system_awake.held


def test_two_instruments_hold_independent_requests(runtime):
    first, second = _controller(runtime.module), _controller(runtime.module)
    second.device_id, second.com, second.port_num = "second", 15, 1
    first._set_port_claimed(True)
    second._set_port_claimed(True)
    first._set_port_claimed(False)
    assert not first._system_awake.held and second._system_awake.held
    second._set_port_claimed(False)
    assert runtime.api.calls.count(("close", 1)) == runtime.api.calls.count(("close", 2)) == 1


def test_refusal_is_logged_on_windows_only(runtime, monkeypatch, caplog):
    runtime.api.refuse = True
    driver = _controller(runtime.module)
    with caplog.at_level(logging.WARNING, logger="test_esi_driver"):
        driver._set_port_claimed(True)
        assert "keep-awake" not in caplog.text or sys.platform == "win32"
        driver._set_port_claimed(False)
        monkeypatch.setattr(sys, "platform", "win32")
        driver._set_port_claimed(True)
    assert "Windows refused the keep-awake request" in caplog.text


def test_tpg366_runtime_carries_the_same_request_code(monkeypatch):
    def block(path):
        source = path.read_text(encoding="utf-8")
        start = source.index("_POWER_REQUEST_SYSTEM_REQUIRED = 1")
        end = source.index("class DllPortClaimRegistryMixin:") if "DllPortClaimRegistryMixin" in source else len(source)
        return source[start:end].strip()
    assert block(ROOT / "tpg366/_runtime/_tpg366.py") == block(ROOT / "amx_a/vendor/runtime/_driver_common.py")
    spec = importlib.util.spec_from_file_location("_tpg366_awake_test", ROOT / "tpg366/_runtime/_tpg366.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)  # Dataclasses resolve annotations through it.
    spec.loader.exec_module(module)
    api = PowerApi()
    module._power_api = lambda: api
    request = module.SystemAwakeRequest("tpg")
    assert request.hold(True) and not request.hold(False) and len(api.calls) == 4
