"""Native facade selection and legacy Python-worker fault references."""

import hashlib
import importlib.util
import json
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
    # Deliberate Python-process reference injection, never a production fallback.
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


@pytest.fixture
def legacy_bundle(tmp_path, monkeypatch):
    """Synthetic legacy bundle: no dependency on retired production Python files."""
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    directory = tmp_path / "esi/vendor/python"
    directory.mkdir(parents=True)
    files = {}
    for name in ("python.exe", "python314.dll", "python314.zip", "python314._pth"):
        data = f"test-only legacy bundle: {name}\n".encode("ascii")
        (directory / name).write_bytes(data)
        files[name] = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    manifest = {"version": "test-only", "files": files,
                "uncompressed_bytes": sum(file["bytes"] for file in files.values())}
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "__file__", str(tmp_path / "esi/vendor/runtime/esi/_process.py"))
    return module, directory


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


def test_frozen_legacy_reference_uses_only_mock_bundle_not_path_or_override(legacy_bundle, monkeypatch):
    module, directory = legacy_bundle
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "C:/Python/python.exe")
    monkeypatch.setattr(sys, "executable", "C:/Explorer/ESIBD Explorer.exe")
    assert module._worker_python() == [str(directory / "python.exe")]


def test_frozen_plugin_constructs_native_facade_without_inline_or_external_python(monkeypatch, tmp_path):
    import importlib
    import subprocess

    module = _load_plugin()
    driver = module._get_esi_driver_class()
    process_module = importlib.import_module(driver.__module__.rsplit(".", 1)[0] + "._process")
    native_module = importlib.import_module(driver.__module__.rsplit(".", 2)[0] + "._native_worker")
    calls, messages, constructed = [], [], []

    class FakeProxy:
        def __init__(self, plugin_root, family, config, **options):
            constructed.append((self, plugin_root, family, config, options))
            self.closed = False
            self.session = "native-esi-constructor-test"
            self.identity = {"family": family, "protocol": native_module.PROTOCOL}
            self._lifecycle = dict.fromkeys(native_module.LIFECYCLE_ATTRIBUTES, False)
            self._lifecycle.update(_transport_error="", _failed_open_cleanup_outcome=None)

        def lifecycle_attribute(self, name):
            return self._lifecycle[name]

        def get_attribute(self, name, **kwargs):
            pytest.fail(f"lifecycle {name} must use the native reply cache, not an RPC")

        def call_method(self, name, *args, **kwargs):
            assert controller.device._backend_mode == "process"
            assert name in ("connect", "disconnect")
            calls.append(name)
            if name == "connect":
                # A failed native Open may retain its partial handle until cleanup.
                self._lifecycle.update(_open_failed=True, _dll_port_claimed=True)
                raise RuntimeError("simulated port unavailable")
            self._lifecycle.update(connected=False, _dll_port_claimed=False,
                                   _failed_open_released=True)
            return True

        def wait_for_idle(self, timeout_s):
            return True

        def close(self):
            self.closed = True
            return True

    def unexpected_inline(*args, **kwargs):
        pytest.fail("standalone Explorer must not load the vendor DLL inline")

    def unexpected_python(*args, **kwargs):
        pytest.fail("native ESI must not construct a Python worker or search for external Python")

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:/Explorer/ESIBD Explorer.exe")
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "C:/WindowsApps/python.exe")
    monkeypatch.setattr(native_module, "NativeWorkerProxy", FakeProxy)
    monkeypatch.setattr(process_module, "ESIProcessProxy", unexpected_python)
    monkeypatch.setattr(process_module, "_worker_python", unexpected_python)
    monkeypatch.setattr(subprocess, "Popen", unexpected_python)
    monkeypatch.setattr(driver._PROCESS_CONTROLLER_CLASS, "__init__", unexpected_inline)
    parent = SimpleNamespace(com=16, baudrate=230400, connect_timeout_s=.2,
                             pluginManager=SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path)))
    controller = module.ESIController(parent)
    controller.print = lambda message, **kwargs: messages.append(message)
    controller.initializing = True

    controller.runInitialization()

    assert len(constructed) == 1
    proxy, plugin_root, family, config, options = constructed[0]
    assert plugin_root == Path(module.__file__).parent and family == "esi"
    assert proxy.session and proxy.identity == {"family": "esi", "protocol": native_module.PROTOCOL}
    assert config["com"] == 16 and config["baudrate"] == 230400
    assert config["device_id"] == "esi_com16"
    assert config["log_dir"] == options["log_dir"] == tmp_path / "logs/esi"
    assert options["startup_timeout_s"] == driver._PROCESS_STARTUP_TIMEOUT_S
    assert calls == ["connect", "disconnect"]
    assert proxy.closed and proxy.lifecycle_attribute("_failed_open_released")
    assert controller.device is None and not controller.initializing
    assert controller.main_state == "Disconnected"
    assert sum("plugin-local native Rust worker" in message for message in messages) == 1
    assert not any("private Python" in message for message in messages)
    assert any("initialization failed on COM16: simulated port unavailable" in message for message in messages)


@pytest.mark.parametrize("missing", ["python.exe", "python314.dll", "python314.zip", "python314._pth"])
def test_incomplete_legacy_bundle_fails_without_external_or_inline_fallback(legacy_bundle, monkeypatch, missing):
    import shutil

    module, directory = legacy_bundle
    (directory / missing).unlink()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", str(sys.executable))
    monkeypatch.setattr(shutil, "which", lambda *args: pytest.fail("must not search PATH"))
    with pytest.raises(RuntimeError, match="Bundled ESI Python is incomplete") as error:
        module._worker_python()
    assert str(directory / missing) in str(error.value)


def test_duplicate_transport_release_does_not_close_same_owner_twice():
    module = _load_plugin()
    entered, finish = threading.Event(), threading.Event()
    calls, results = [], []
    action = SimpleNamespace(state=True)
    parent = SimpleNamespace(connect_timeout_s=.2, onAction=action, recording=True)
    controller = module.ESIController(parent)
    controller.initialized = controller.acquiring = True
    controller.print = lambda *args, **kwargs: None

    def close_transport(**kwargs):
        calls.append("close")
        entered.set()
        assert finish.wait(3.)
        return True

    owner = SimpleNamespace(force_close_transport=close_transport, close=lambda: None)
    controller.device = owner
    thread = threading.Thread(target=lambda: results.append(controller._release_failed_transport(owner)))
    try:
        thread.start()
        assert entered.wait(3.)
        assert controller._release_failed_transport(owner) is False
        assert controller.device is owner
        assert calls == ["close"]
    finally:
        finish.set()
        thread.join(3.)
    assert not thread.is_alive() and results == [True]
    assert controller.device is None and not controller.initialized and not controller.acquiring
    assert not action.state and not parent.recording
    assert controller.shutdown_unconfirmed
    assert controller.main_state == module._ESI_DISCONNECTED_UNCONFIRMED
    assert controller._release_failed_transport(owner) is False
    assert calls == ["close"]


def test_interrupting_a_discharge_check_logs_uncertainty_not_a_native_crash():
    module = _load_plugin()
    controller = module.ESIController(SimpleNamespace(connect_timeout_s=.2, recording=True))
    controller.device = SimpleNamespace(force_close_transport=lambda **kwargs: True, close=lambda: None)
    controller.initialized = True
    controller.main_state = module._ESI_STOPPING
    controller._output_lock = SimpleNamespace(acquire=lambda **kwargs: False)
    messages = []
    controller.print = lambda message, **kwargs: messages.append(message)
    assert controller.shutdownCommunication() is False
    assert "discharge check is still running" in messages[0]
    assert "shutdown remains unconfirmed" in messages[0]
    assert controller.device is None and controller.shutdown_unconfirmed
    assert controller.main_state == module._ESI_DISCONNECTED_UNCONFIRMED


@pytest.mark.skipif(sys.platform.startswith("win"), reason="verify private bootstrap before the Windows DLL load")
def test_real_private_runtime_bootstraps_in_worker_without_host_modules(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(RuntimeError, match="CGC ESI is supported only on Windows"):
        module.ESIProcessProxy({"device_id": "bootstrap_test", "com": 16,
                               "log_dir": str(tmp_path / "data/logs/esi")}, startup_timeout_s=5.)


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


def test_native_startup_failure_does_not_fall_back_to_inline_or_python(monkeypatch):
    import importlib
    import subprocess

    module = _load_plugin()
    driver = module._get_esi_driver_class()
    process_module = importlib.import_module(driver.__module__.rsplit(".", 1)[0] + "._process")
    native_module = importlib.import_module(driver.__module__.rsplit(".", 2)[0] + "._native_worker")

    def fail(*args, **kwargs):
        raise RuntimeError("no native worker executable")

    def unexpected_python(*args, **kwargs):
        pytest.fail("native startup failure must not launch a Python worker")

    monkeypatch.setattr(native_module, "NativeWorkerProxy", fail)
    monkeypatch.setattr(process_module, "ESIProcessProxy", unexpected_python)
    monkeypatch.setattr(process_module, "_worker_python", unexpected_python)
    monkeypatch.setattr(subprocess, "Popen", unexpected_python)
    monkeypatch.setattr(driver._PROCESS_CONTROLLER_CLASS, "__init__",
                        lambda *args, **kwargs: pytest.fail("DLL must not load in Explorer"))
    with pytest.raises(RuntimeError, match="no native worker executable"):
        driver(device_id="esi_test", com=16, native_backend=True)


@pytest.mark.parametrize("override", [False, True])
def test_worker_uses_console_python_when_explorer_uses_pythonw(tmp_path, monkeypatch, override):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    windowed = tmp_path / "pythonw.exe"
    console = tmp_path / "python.exe"
    console.touch()
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(sys, "executable", str(windowed))
    if override:
        monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", str(windowed))
    else:
        monkeypatch.delenv("ESIBD_ESI_WORKER_PYTHON", raising=False)
    assert module._worker_python() == [str(console)]
    console.unlink()
    with pytest.raises(RuntimeError, match="requires python.exe, not pythonw.exe"):
        module._worker_python()


def test_startup_crash_reports_interpreter_exit_code_and_stderr(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = tmp_path / "crashing_controller.py"
    fake.write_text("import os, sys\nprint('bootstrap failure marker', file=sys.stderr, flush=True)\nos._exit(23)\n")
    with pytest.raises(RuntimeError) as error:
        module.ESIProcessProxy({}, controller_file=str(fake), startup_timeout_s=5.)
    message = str(error.value)
    assert "ESI worker startup failed" in message
    assert sys.executable in message
    assert "23 (0x00000017)" in message
    assert "bootstrap failure marker" in message


def test_startup_exception_keeps_the_python_traceback(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = tmp_path / "invalid_controller.py"
    fake.write_text("raise RuntimeError('controller import failure')\n")
    with pytest.raises(RuntimeError) as error:
        module.ESIProcessProxy({}, controller_file=str(fake), startup_timeout_s=5.)
    message = str(error.value)
    assert "controller import failure" in message
    assert "Traceback (most recent call last)" in message
    assert str(fake) in message


def test_runtime_crash_keeps_its_exit_status(workers):
    proxy = workers()
    with pytest.raises(RuntimeError) as error:
        proxy.call_method("crash", rpc_timeout_s=2.)
    assert "7 (0x00000007)" in str(error.value)
    assert "interpreter=" in str(error.value)
    assert proxy.closed and not proxy._stderr_reader.is_alive()


def test_stderr_is_drained_without_deadlock_or_unbounded_storage(tmp_path):
    spec = importlib.util.spec_from_file_location("esi_process_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fake = tmp_path / "noisy_controller.py"
    fake.write_text('''
import os
class Controller:
    def flood(self):
        os.write(2, b"x" * (1024 * 1024) + b"tail marker")
        os._exit(29)
''')
    proxy = module.ESIProcessProxy({}, controller_file=str(fake), startup_timeout_s=5.)
    try:
        with pytest.raises(RuntimeError) as error:
            proxy.call_method("flood", rpc_timeout_s=5.)
        assert "tail marker" in str(error.value)
        assert "29 (0x0000001D)" in str(error.value)
        assert len(proxy._stderr_tail) <= 8192
    finally:
        proxy.close()
