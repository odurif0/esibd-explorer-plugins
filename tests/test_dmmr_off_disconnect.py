"""OFF must close the DMMR port, not merely hide Explorer's exit warning."""
from __future__ import annotations

import ast
from importlib.metadata import PackageNotFoundError, distribution
import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_dmmr_toggle_failures import rig as toggle_rig, start
from test_shutdown_confirmation_regressions import runtime_for


@pytest.fixture(name="rig")
def off_rig():
    result = toggle_rig.__wrapped__()
    result.device.fail_start = None
    return result


def request_off(rig):
    rig.parent.on = False
    assert rig.controller._begin_transition(False)
    rig.controller.toggleOn()
    assert not rig.controller.transitioning


@pytest.fixture
def explorer_close():
    """Execute the installed host's actual initialized and exit checks."""
    try:
        host = Path(distribution("esibd-explorer").locate_file("esibd"))
    except PackageNotFoundError:
        pytest.skip("Explorer source unavailable")

    def extract(filename, parent, name):
        source = host / filename
        if not source.is_file():
            pytest.skip("Explorer source unavailable")
        tree = ast.parse(source.read_text())
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parent)
        node = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == name)
        ns = {"Device": object}
        exec(compile("from __future__ import annotations\n" + ast.unparse(node), str(source), "exec"), ns)
        return ns, ns[name]

    _, initialized = extract("plugins.py", "Device", "initialized")
    _, any_initialized = extract("plugins.py", "DeviceManager", "initialized")
    ns, close_application = extract("core.py", "EsibdExplorer", "closeApplication")
    host_device = type("HostDevice", (), {"initialized": initialized})
    dialogs = []
    ns["CloseDialog"] = lambda **kwargs: (
        dialogs.append(kwargs) or SimpleNamespace(exec=lambda: False)
    )

    def close(controller, *, other_connected=False):
        dialogs.clear()
        device = host_device()
        device.controller = controller
        plugins = [device, SimpleNamespace(initialized=other_connected)]
        manager = SimpleNamespace(pluginManager=SimpleNamespace(
            loading=False, getPluginsByClass=lambda cls: plugins,
        ))
        manager.initialized = lambda: any_initialized(manager)
        closed = []
        manager.closeCommunication = lambda **kwargs: closed.append("communication")
        window = SimpleNamespace(pluginManager=SimpleNamespace(
            DeviceManager=manager, closePlugins=lambda: closed.append("plugins"),
        ))
        result = close_application(window)
        return result, list(dialogs), closed

    return close


def test_on_off_closes_port_and_clears_explorer_exit_warning(rig, explorer_close):
    rig.parent.controller = rig.controller
    start(rig)
    assert rig.controller.acquiring and rig.device.connected
    result, dialogs, _ = explorer_close(rig.controller)
    assert not result and len(dialogs) == 1

    request_off(rig)

    assert not rig.device.enabled and not rig.device.automatic
    assert not rig.device.connected, "OFF left the serial port open"
    assert rig.device.port_closes == 1
    assert rig.controller.device is None
    assert not rig.controller.initialized and not rig.controller.acquiring
    assert rig.controller.main_state == rig.parent.main_state == "Disconnected"
    assert rig.parent.on is False
    last_readback = max(i for i, call in enumerate(rig.device.calls) if call[0] == "read")
    assert last_readback < rig.device.calls.index(("disconnect",))
    assert rig.device.calls[last_readback + 1:] == [("disconnect",), ("disconnect",)]
    assert explorer_close(rig.controller) == (True, [], ["plugins"])
    # A different connected device must still trigger Explorer's warning.
    result, dialogs, _ = explorer_close(rig.controller, other_connected=True)
    assert not result and len(dialogs) == 1


def test_on_after_off_reinitializes_instead_of_reusing_closed_device(rig):
    start(rig)
    request_off(rig)
    parent = rig.parent
    calls = []
    parent.controller = rig.controller
    parent.useOnOffLogic = True
    parent.loading = False
    parent.onAction = SimpleNamespace(state=False)
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state = lambda on: setattr(parent.onAction, "state", on)
    parent._update_status_widgets = lambda: None
    parent._sync_local_on_action = lambda: None
    parent.initializeCommunication = lambda: calls.append("initialize")
    rig.controller.toggleOnFromThread = lambda **kw: calls.append("toggle")

    rig.module.DMMRDevice.setOn(parent, on=True)

    assert calls == ["initialize"]
    assert rig.controller.device is None
    assert rig.device.port_closes == 1


@pytest.mark.parametrize("result", [False, None, "exception"])
def test_failed_port_close_keeps_a_retry_and_the_exit_warning(rig, explorer_close, result):
    start(rig)
    if result == "exception":
        rig.device.disconnect_error = RuntimeError("port close failed")
    else:
        rig.device.disconnect_result = result
    request_off(rig)
    assert not rig.device.enabled
    assert rig.device.connected
    assert rig.controller.device is rig.device
    assert rig.controller.initialized
    assert rig.controller.main_state == "Shutdown unconfirmed"
    assert rig.parent.on is True  # Next click retries OFF, not ON.
    assert rig.device.port_closes == 0
    accepted, dialogs, _ = explorer_close(rig.controller)
    assert not accepted and len(dialogs) == 1
    enables = rig.device.calls.count(("enable", True))

    rig.device.disconnect_error = None
    rig.device.disconnect_result = True
    request_off(rig)
    assert rig.controller.device is None
    assert not rig.controller.initialized
    assert not rig.device.connected and rig.device.port_closes == 1
    assert rig.device.calls.count(("enable", True)) == enables
    assert explorer_close(rig.controller) == (True, [], ["plugins"])


@pytest.mark.parametrize("gate", ["enable", "automatic"])
def test_unconfirmed_gate_does_not_discard_connection_needed_for_retry(rig, gate):
    start(rig)
    rig.device.readback[gate] = (0, True)
    request_off(rig)
    assert ("disconnect",) not in rig.device.calls
    assert rig.device.connected and rig.controller.initialized
    assert rig.controller.device is rig.device
    assert rig.parent.on and rig.controller.main_state == "Shutdown unconfirmed"
    rig.device.readback.clear()
    request_off(rig)
    assert rig.device.port_closes == 1
    assert rig.controller.device is None


@pytest.mark.parametrize("close_status", [0, -2])
def test_off_uses_real_runtime_disconnect_and_honors_native_close_result(rig, monkeypatch, close_status):
    driver = runtime_for("dmmr")
    driver._DEFAULT_IO_TIMEOUT_S = 0.2
    base = importlib.import_module(type(driver).__module__).DMMRBase
    native_closes = []
    monkeypatch.setattr(base, "close_port", lambda self: native_closes.append("close") or close_status)
    for name in ("set_enable", "set_automatic_current", "get_enable", "get_automatic_current"):
        setattr(driver, name, getattr(rig.device, name))
    rig.controller.device = driver
    request_off(rig)
    assert native_closes == ["close"]
    if close_status == 0:
        assert not driver.connected and not driver._dll_port_claimed
        assert rig.controller.device is None and not rig.controller.initialized
    else:
        assert driver.connected and driver._dll_port_claimed
        assert rig.controller.device is driver and rig.controller.initialized
        assert rig.parent.on and rig.controller.main_state == "Shutdown unconfirmed"


def test_blocked_native_close_keeps_port_claim_without_another_native_close(rig, monkeypatch):
    import threading

    driver = runtime_for("dmmr")
    driver._DEFAULT_IO_TIMEOUT_S = 0.02
    base = importlib.import_module(type(driver).__module__).DMMRBase
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    native_closes = []

    def blocked_close(self):
        native_closes.append("close")
        entered.set()
        release.wait(3)
        finished.set()
        return 0

    monkeypatch.setattr(base, "close_port", blocked_close)
    for name in ("set_enable", "set_automatic_current", "get_enable", "get_automatic_current"):
        setattr(driver, name, getattr(rig.device, name))
    rig.controller.device = driver
    try:
        request_off(rig)
        assert entered.is_set() and not finished.is_set()
        assert driver._transport_poisoned and driver._dll_port_claimed
        assert rig.controller.device is driver and rig.controller.initialized
        assert rig.parent.on and rig.controller.main_state == "Shutdown unconfirmed"
        request_off(rig)
        assert native_closes == ["close"]
        assert driver._dll_port_claimed
        assert rig.controller.device is driver and rig.controller.initialized
        assert rig.parent.on
    finally:
        release.set()
        assert finished.wait(1)
