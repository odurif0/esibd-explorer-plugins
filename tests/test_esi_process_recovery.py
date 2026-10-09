"""Real ESI worker processes, with a fake instrument instead of a Windows DLL."""

import importlib.util
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_esi_plugin_behavior import _load_plugin


WORKER = Path(__file__).resolve().parents[1] / "esi/vendor/runtime/esi/_process.py"
FAKE_CONTROLLER = '''
import os
import time

class Controller:
    def __init__(self, **kwargs):
        self.counter = 0
        self._transport_poisoned = False
        self.connected = True

    def ping(self):
        print("vendor output must not corrupt RPC", flush=True)
        os.write(1, b"native vendor output\\n")
        return (b"config bytes", {1: (True, 700.)})

    def add(self, amount):
        value = self.counter
        time.sleep(.01)
        self.counter = value + amount
        return self.counter

    def block(self, *, on_discharge=None, cancel_event=None):
        if on_discharge:
            on_discharge({"entered": True})
        if cancel_event is not None:
            return cancel_event.wait(5.)
        time.sleep(60.)

    def disconnect(self, **kwargs):
        raise RuntimeError("get_enable failed: -13 (Wrong command received)")

    def collect_diagnostics(self, **kwargs):
        raise RuntimeError("get_enable failed: -13 (Wrong command received)")

    def crash(self):
        os._exit(7)

    def poison(self):
        self._transport_poisoned = True
        raise RuntimeError("ESI DLL call timed out")
'''


@pytest.fixture
def workers(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)
    created = []

    def create():
        proxy = module.ESIProcessProxy({}, controller_file=str(fake), startup_timeout_s=5.)
        created.append(proxy)
        return proxy

    yield create
    for proxy in created:
        proxy.close()


def test_private_worker_roundtrip_bytes_and_native_stdout(workers):
    proxy = workers()
    assert proxy.call_method("ping", rpc_timeout_s=2.) == (b"config bytes", {1: (True, 700.)})


def test_concurrent_requests_cannot_mix_replies(workers):
    proxy = workers()
    results, errors = [], []

    def add():
        try:
            results.append(proxy.call_method("add", 1, rpc_timeout_s=3.))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5.)
        assert not thread.is_alive()
    assert not errors
    assert sorted(results) == list(range(1, 9))


def test_progress_and_stop_event_cross_process_boundary(workers):
    proxy = workers()
    cancel = threading.Event()
    reports = []

    def progress(report):
        reports.append(report)
        cancel.set()

    assert proxy.call_method("block", rpc_timeout_s=3., cancel_event=cancel, on_discharge=progress)
    assert reports == [{"entered": True}]


@pytest.mark.parametrize("operation", ["crash", "poison", "block"])
def test_crash_or_block_releases_worker_and_allows_fresh_instance(workers, operation):
    proxy = workers()
    with pytest.raises(RuntimeError):
        proxy.call_method(operation, rpc_timeout_s=.2)
    assert proxy.closed and proxy._process.poll() is not None
    replacement = workers()
    assert replacement.call_method("ping", rpc_timeout_s=2.)[0] == b"config bytes"


def test_explicit_close_interrupts_blocked_request_without_stopping_other_worker(workers):
    proxy, other = workers(), workers()
    entered = threading.Event()
    errors = []

    def call():
        try:
            proxy.call_method("block", rpc_timeout_s=60., on_discharge=lambda report: entered.set())
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    assert entered.wait(3.)
    assert proxy.close()
    thread.join(3.)
    assert not thread.is_alive() and errors
    assert proxy._process.poll() is not None
    assert other.call_method("add", 1, rpc_timeout_s=2.) == 1


def _control(module, proxy):
    driver_type = module._get_esi_driver_class()
    driver = object.__new__(driver_type)
    object.__setattr__(driver, "_backend", proxy)
    object.__setattr__(driver, "_backend_mode", "process")
    action = SimpleNamespace(state=True)
    parent = SimpleNamespace(onAction=action, isOn=lambda: action.state, getChannels=lambda: [],
                             connect_timeout_s=.2, poll_timeout_s=.2, recording=True)
    controller = module.ESIController(parent)
    controller.device = driver
    controller.initialized = controller.acquiring = True
    controller.main_state = "STATE_ON"
    controller.print = lambda message, **kwargs: messages.append(message)
    messages = []
    return controller, parent, messages


def test_failed_shutdown_detaches_and_reconnects_only_esi(workers):
    module = _load_plugin()
    old, other = workers(), workers()
    c, parent, messages = _control(module, old)
    assert c.shutdownCommunication() is False
    assert c.device is None and not c.initialized and not c.acquiring
    assert c.main_state == module._ESI_DISCONNECTED_UNCONFIRMED
    assert not parent.onAction.state and not parent.recording
    assert old._process.poll() is not None
    assert any("HV/heater shutdown is unconfirmed" in message for message in messages)
    assert c._dispose_device() is True
    replacement = workers()
    c.device = _control(module, replacement)[0].device
    assert c.device.connected is True
    assert other.call_method("add", 1, rpc_timeout_s=2.) == 1


@pytest.mark.parametrize("output_command", [True, False])
def test_plugin_close_interrupts_inflight_command_or_poll_and_keeps_uncertainty(workers, output_command):
    import contextlib
    module = _load_plugin()
    proxy = workers()
    c, parent, _ = _control(module, proxy)
    entered = threading.Event()
    errors = []

    def command():
        with c._output_lock if output_command else contextlib.nullcontext():
            try:
                proxy.call_method("block", rpc_timeout_s=60., on_discharge=lambda report: entered.set())
            except Exception as exc:
                errors.append(exc)

    thread = threading.Thread(target=command)
    thread.start()
    assert entered.wait(3.)
    c.closeCommunication()
    thread.join(3.)
    assert not thread.is_alive() and errors
    assert c.device is None and not c.initialized
    assert not parent.onAction.state and not parent.recording
    assert c.main_state == module._ESI_DISCONNECTED_UNCONFIRMED
    assert not c.closing


def test_parent_pipe_loss_stops_even_a_permanently_blocked_dll(workers):
    proxy = workers()
    entered = threading.Event()
    errors = []

    def call():
        try:
            proxy.call_method("block", rpc_timeout_s=60., on_discharge=lambda report: entered.set())
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=call)
    thread.start()
    assert entered.wait(3.)
    proxy._process.stdin.close()  # Explorer exited, leaving no request pipe owner.
    proxy._process.wait(timeout=3.)
    thread.join(3.)
    assert not thread.is_alive() and errors


def test_error_threshold_close_inside_poll_does_not_unlock_mutex_or_resurrect_fault(workers, monkeypatch):
    module = _load_plugin()
    proxy = workers()
    c, parent, messages = _control(module, proxy)

    class ThresholdController(module.ESIController):
        @property
        def errorCount(self):
            return self._errors

        @errorCount.setter
        def errorCount(self, value):
            self._errors = value
            if value == 25 and self.initialized:
                self.closeCommunication()  # Explorer invokes this synchronously from its setter.

    c.__class__ = ThresholdController
    c._errors = 24
    c.lock = threading.Lock()
    base_closes = []
    monkeypatch.setattr(module.DeviceController, "closeCommunication",
                        lambda self: base_closes.append(True) or self.lock.release(), raising=False)
    with c.lock:
        c.readNumbers()
        assert c.lock.locked(), "the poll must release its own mutex once, on return"
    assert not base_closes
    assert c.device is None and not c.initialized and not c.acquiring
    assert not parent.onAction.state and not parent.recording
    assert c.main_state == module._ESI_DISCONNECTED_UNCONFIRMED
    assert c.shutdown_unconfirmed


def test_readback_recovery_resets_consecutive_errors_without_replaying_outputs():
    from test_esi_current_measurements import rig, snapshot

    module, channels, c = rig()
    calls = []
    current_cancel = c._output_cancel

    def read(**kwargs):
        calls.append("read")
        if len(calls) == 1:
            raise RuntimeError("-10 (Error receiving command)")
        return snapshot()

    c.device = SimpleNamespace(collect_diagnostics=read)
    c.errorCount = 0
    c.readNumbers()
    assert c.errorCount == 1 and current_cancel.is_set()
    c.readNumbers()
    assert c.errorCount == 0
    assert c.main_state == snapshot()["main_state"]["name"]
    assert c._output_cancel is not current_cancel and not c._output_cancel.is_set()
    assert current_cancel.is_set(), "old queued commands must remain cancelled"
    assert calls == ["read", "read"]


def test_frozen_explorer_uses_explicit_python_instead_of_relaunching_explorer(monkeypatch):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "C:/Python/python.exe")
    assert module._worker_python() == ["C:/Python/python.exe"]


@pytest.mark.skipif(sys.platform.startswith("win"), reason="verify private bootstrap before the Windows DLL load")
def test_real_private_runtime_bootstraps_in_worker_without_host_modules():
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="CGC ESI is supported only on Windows"):
        module.ESIProcessProxy({"device_id": "bootstrap_test", "com": 16}, startup_timeout_s=5.)


def test_retired_owner_cannot_restore_on_via_a_queued_gui_callback(monkeypatch):
    module = _load_plugin()
    action = SimpleNamespace(state=False)
    c = module.ESIController(SimpleNamespace(onAction=action))
    c.device = object()
    queued = []
    monkeypatch.setattr(module, "_invoke_gui_callback", queued.append)
    c._restore_on_ui_state()
    c.device = None
    queued[0]()
    assert action.state is False


def test_off_during_initialization_cancels_before_scheduling_close(monkeypatch):
    module = _load_plugin()
    action = SimpleNamespace(state=True)
    cancel = threading.Event()
    closed = []
    c = SimpleNamespace(initializing=True, _output_cancel=cancel, closeCommunication=lambda: closed.append(True))
    device = object.__new__(module.ESIDevice)
    device.controller = c
    device.onAction = action
    device.isOn = lambda: action.state
    device._sync_local_on_action = lambda: None
    device.loading = False
    queued = []
    monkeypatch.setattr(module, "Thread", lambda **kwargs: SimpleNamespace(start=lambda: queued.append(kwargs["target"])))
    device.setOn(False)
    assert c._initial_open_close_requested and cancel.is_set()
    assert not closed and len(queued) == 1
    queued[0]()
    assert closed == [True]


def test_cancelled_initialization_does_not_leave_connecting_status():
    module = _load_plugin()
    c = module.ESIController(SimpleNamespace())
    c.main_state = "Connecting"
    c.initializing = True
    c._initial_open_close_requested = True
    c.runInitialization()
    assert c.main_state == "Disconnected" and not c.initializing


def test_isolation_startup_failure_does_not_fall_back_to_inline(monkeypatch):
    import importlib

    module = _load_plugin()
    driver = module._get_esi_driver_class()
    process_module = importlib.import_module(driver.__module__.rsplit(".", 1)[0] + "._process")
    def fail(*args, **kwargs):
        raise RuntimeError("no worker interpreter")
    monkeypatch.setattr(process_module, "ESIProcessProxy", fail)
    monkeypatch.setattr(driver._PROCESS_CONTROLLER_CLASS, "__init__",
                        lambda *args, **kwargs: pytest.fail("DLL must not load in Explorer"))
    with pytest.raises(RuntimeError, match="no worker interpreter"):
        driver(device_id="esi_test", com=16, process_backend=True)
