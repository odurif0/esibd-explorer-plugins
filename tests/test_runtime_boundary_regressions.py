"""Malformed buffers and thread setup errors must fail before unsafe native I/O."""

import ast
import logging
from pathlib import Path
import queue
import threading
import time
import types

import pytest

from test_amx_hd_plugin_behavior import _load_hd_controller_classes

ROOT = Path(__file__).resolve().parents[1]
COMMON = sorted(ROOT.glob("*/vendor/runtime/_driver_common.py"))


@pytest.fixture(params=COMMON, ids=lambda p: p.parents[2].name)
def transport(request):
    tree = ast.parse(request.param.read_text())
    mixin = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TimeoutSafeDllMixin")
    namespace = {"queue": queue, "threading": threading, "time": time}
    exec(compile(ast.Module(body=[mixin], type_ignores=[]), str(request.param), "exec"), namespace)
    module = types.SimpleNamespace(**namespace)
    driver = module.TimeoutSafeDllMixin()
    driver.thread_lock = threading.Lock()
    driver._transport_poisoned = False
    driver._transport_error = None
    driver.connected = True
    driver.logger = logging.getLogger("boundary-test")
    return module, driver


@pytest.mark.parametrize("failure", ["queue", "constructor", "start"])
def test_setup_failure_releases_lock_without_poisoning(transport, monkeypatch, failure):
    module, driver = transport
    called = []

    def fail(*args, **kwargs):
        raise RuntimeError("can't start new thread")

    with monkeypatch.context() as patch:
        if failure == "queue":
            patch.setattr(module.queue, "Queue", fail)
        elif failure == "constructor":
            patch.setattr(module.threading, "Thread", fail)
        else:
            patch.setattr(module.threading.Thread, "start", fail)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            driver._call_locked_with_timeout(lambda: called.append(True), 0.1, "read")
    assert called == []
    assert not driver._transport_poisoned
    assert not driver.thread_lock.locked()
    assert driver._call_locked_with_timeout(lambda: 42, 0.1, "next read") == 42


def test_failure_after_worker_start_keeps_native_transport_locked(transport, monkeypatch):
    module, driver = transport
    entered, release, done = threading.Event(), threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(3)
        done.set()
        return 0

    def broken_join(self, timeout=None):
        assert entered.wait(3)
        raise RuntimeError("join interrupted")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.threading.Thread, "join", broken_join)
            with pytest.raises(RuntimeError, match="join interrupted"):
                driver._call_locked_with_timeout(blocked, 0.1, "read")
        assert driver._transport_poisoned
        assert driver.thread_lock.locked()
    finally:
        release.set()
        assert done.wait(3)


def test_worker_without_result_does_not_wait_forever(transport, monkeypatch):
    module, driver = transport
    thread = types.SimpleNamespace(start=lambda: None, join=lambda timeout: None, is_alive=lambda: False)
    monkeypatch.setattr(module.threading, "Thread", lambda **kwargs: thread)
    with pytest.raises(RuntimeError, match="exited without a result"):
        driver._call_locked_with_timeout(lambda: 0, 0.1, "read")
    assert not driver.thread_lock.locked()
    assert not driver._transport_poisoned


def test_poison_hook_error_must_not_release_a_blocked_dll(transport):
    _module, driver = transport
    release, done = threading.Event(), threading.Event()

    def blocked():
        release.wait(3)
        done.set()

    def broken_hook():
        raise RuntimeError("hook failed")

    driver._on_transport_poisoned = broken_hook
    try:
        with pytest.raises(RuntimeError, match="hook failed"):
            driver._call_locked_with_timeout(blocked, 0.02, "read")
        assert driver._transport_poisoned
        assert driver.thread_lock.locked()
    finally:
        release.set()
        assert done.wait(3)


@pytest.fixture
def hd_base():
    _controller, cls = _load_hd_controller_classes()
    device = cls.__new__(cls)
    device.stream = 1
    calls = []

    def write(*args):
        calls.append(bytes(args[-1]))
        return 0

    device.amx_hd_dll = types.SimpleNamespace(
        COM_HVAMX4EDH_SetCurrentConfig=write,
        COM_HVAMX4EDH_SetConfigData=write,
        COM_HVAMX4EDH_SetDefaults=write,
    )
    return device, calls


def buffer_method(device, kind):
    if kind == "current":
        return device.set_current_config, device.CONFIG_DATA_SIZE
    if kind == "nvm":
        return lambda data: device.set_config_data(7, data), device.CONFIG_DATA_SIZE
    return device.set_defaults, device.DEFAULT_DATA_SIZE


@pytest.mark.parametrize("kind", ["current", "nvm", "defaults"])
@pytest.mark.parametrize("invalid", ["short", "long", "negative", "overflow", "float", "text", "integer"])
def test_invalid_hd_binary_configuration_never_reaches_dll(hd_base, kind, invalid):
    device, calls = hd_base
    setter, size = buffer_method(device, kind)
    data = [0] * size
    if invalid == "short":
        data.pop()
    elif invalid == "long":
        data.append(0)
    elif invalid == "integer":
        data = size
    else:
        data[0] = {"negative": -1, "overflow": 300, "float": 1.0, "text": "1"}[invalid]
    with pytest.raises((ValueError, TypeError)):
        setter(data)
    assert calls == []


@pytest.mark.parametrize("kind", ["current", "nvm", "defaults"])
@pytest.mark.parametrize("container", [bytes, bytearray, list, tuple])
def test_valid_hd_binary_configuration_is_transmitted_exactly(hd_base, kind, container):
    device, calls = hd_base
    setter, size = buffer_method(device, kind)
    data = bytes(i % 256 for i in range(size))
    assert setter(container(data)) == 0
    assert calls == [data]
