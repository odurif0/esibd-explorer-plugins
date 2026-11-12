"""Native facade selection and legacy Python-worker fault references."""

import contextlib
import hashlib
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from support.esi import load_plugin as _load_plugin
from support.esi_process import FAKE_CONTROLLER, WORKER


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
    from support.esi_current import rig, snapshot

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


class _VanishingController:
    """An ESI whose OFF reads its outputs back OFF, reports the discharge, then vanishes (-100)."""

    def __init__(self, error, *, report=True):
        self.error, self.report, self.closed = error, report, []
        self.connected, self._transport_poisoned = True, False

    def disconnect(self, timeout_s, on_discharge):
        if self.report:  # The runtime reports only after HV, heater and global enable read back OFF.
            on_discharge({"consecutive": 0, "limit_v": 10.0, "modules": {
                1: {"positive_v": 23.475, "negative_v": -19.897, "measured_a": -6.47e-10},
                2: {"positive_v": -0.046, "negative_v": -0.048, "measured_a": 0.0}}})
        raise RuntimeError(self.error)

    def force_close_transport(self, timeout_s=None):
        self.closed.append("force")
        return True

    def close(self):
        self.closed.append("close")


def _off_controller(module, device):
    parent = SimpleNamespace(connect_timeout_s=.2, recording=True)
    c = module.ESIController(parent)
    c.device, c.initialized = device, True
    messages = []
    c.print = lambda message, **kwargs: messages.append((message, kwargs.get("flag")))
    c._restore_off_ui_state = c._restore_on_ui_state = lambda **kwargs: None
    return c, messages


def test_controller_switched_off_after_outputs_off_is_not_an_unconfirmed_shutdown():
    """2026-11-11 lab: switched off 12 s into the discharge check, the ESI said "HV may remain energized"."""
    module = _load_plugin()
    device = _VanishingController("ESI COM_ESI_CTRL_GetModuleDataReadyFlags failed: -100 (Device not connected)")
    c, messages = _off_controller(module, device)
    assert c._shutdown_communication_unlocked() is False
    text = " ".join(message for message, _ in messages)
    assert "HV may remain energized" not in text and "switched off or its cable unplugged" in text
    assert "HV1 POS +23.5 V / NEG -19.9 V" in text
    # Released at once (a retry OFF hung 7 s on Purge), no "shutdown unconfirmed".
    assert device.closed[0] == "force" and c.device is None
    assert not c.shutdown_unconfirmed and "controller lost at" in c.discharge_unverified
    assert c.main_state == module._ESI_DISCONNECTED_DISCHARGE
    assert "ON reconnects" in text and not any(flag == module.PRINT.ERROR for _, flag in messages)


@pytest.mark.parametrize("error,report", [
    ("ESI COM_ESI_CTRL_GetModuleDataReadyFlags failed: -100 (Device not connected)", False),  # outputs not yet OFF
    ("ESI COM_ESI_CTRL_GetModuleDataReadyFlags failed: -10 (Timeout)", True),  # controller still there
])
def test_other_off_failures_stay_unconfirmed(error, report):
    module = _load_plugin()
    device = _VanishingController(error, report=report)
    c, messages = _off_controller(module, device)
    c._shutdown_communication_unlocked()
    assert any("HV may remain energized" in message for message, _ in messages)
    assert c.shutdown_unconfirmed and not c.discharge_unverified
    assert c.main_state in ("Shutdown unconfirmed", module._ESI_DISCONNECTED_UNCONFIRMED)


def test_on_with_the_controller_switched_off_runs_no_off_sequence(monkeypatch):
    """2026-11-11 lab: ON while the box was off; Open blocked, then an OFF sequence reported an unconfirmed shutdown."""
    module = _load_plugin()

    class Unreachable:
        connected, _transport_poisoned, _open_failed, _opening_in_progress, _failed_open_released = (
            False, True, False, False, False)

        def __init__(self, **kwargs):
            self.closed = []

        def connect(self, **kwargs):
            raise RuntimeError("esi native worker transport is unusable: COM_ESI_CTRL_Open blocked past its "
                               "native I/O deadline (5s); worker terminated")

        def force_close_transport(self, timeout_s=None):
            self.closed.append("force")
            return True

        def close(self):
            self.closed.append("close")

    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: Unreachable)
    parent = SimpleNamespace(connect_timeout_s=.2, com=16, baudrate=230400,
                             pluginManager=SimpleNamespace(Settings=SimpleNamespace(dataPath="/tmp")))
    c = module.ESIController(parent)
    c.discharge_unverified = "controller lost at 13:38:54"
    messages = []
    c.print = lambda message, **kwargs: messages.append(message)
    c._restore_off_ui_state = c._restore_on_ui_state = lambda **kwargs: None
    c.shutdownCommunication = lambda: pytest.fail("no OFF sequence on a connection that never opened")
    c.runInitialization()
    assert c.device is None and not c.initializing
    assert c.main_state == module._ESI_DISCONNECTED_DISCHARGE  # What was known before the attempt.
    assert not c.shutdown_unconfirmed
    assert not any("OFF requested" in m or "could not be confirmed" in m for m in messages)
    assert any("Is the controller switched on?" in m for m in messages), messages


def test_reconnection_supersedes_a_cut_discharge_check():
    module = _load_plugin()
    device = object.__new__(module.ESIDevice)
    device.com, device.interlock_state, device.heat_status = 16, "OK", ""
    device.main_state = module._ESI_DISCONNECTED_DISCHARGE
    device.shutdown_unconfirmed, device.discharge_unverified = False, "controller lost at 13:38:54"
    assert "#975a16" in device._status_badge_style()  # amber, not the red of an unconfirmed shutdown
    assert "discharge not measured to the end (controller lost at 13:38:54)" in device._status_summary_text()
    c = module.ESIController(SimpleNamespace())
    c.discharge_unverified = "controller lost at 13:38:54"
    c.device, c.resume_session, c._initialization_generation = object(), False, None
    messages = []
    c.print = lambda message, **kwargs: messages.append(message)
    c.controllerParent.ensureFixedChannels = lambda **kwargs: None
    c.controllerParent.isOn = lambda: False
    c.initializeValues = c.startAcquisition = lambda **kwargs: None
    c._sync_status = lambda: None
    c.initComplete()
    assert not c.discharge_unverified and any("superseded" in m for m in messages)


def _explorer_start_acquisition(self):
    """Explorer 1.0.2 DeviceController.startAcquisition, with the gap that let two callers both start."""
    if self.acquisitionThread and self.acquisitionThread.is_alive():
        self.acquiring = False
        self.acquisitionThread.join(timeout=5)
    time.sleep(.05)
    self.acquisitionThread = threading.Thread(target=self.runAcquisition, daemon=True)
    self.acquiring = True
    self.acquisitionThread.start()


class _TimeoutLock:
    def __init__(self):
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def acquire_timeout(self, timeout, timeoutMessage=""):
        acquired = self._lock.acquire(timeout=timeout)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()


def _polling_controller(module):
    c = module.ESIController(SimpleNamespace())
    c.initialized, c.acquisitionThread, c.lock = True, None, _TimeoutLock()
    c.getDevice = lambda: SimpleNamespace(interval=10)
    polls = []
    c.readNumbers = lambda: polls.append((threading.current_thread(), time.monotonic()))
    return c, polls


def test_concurrent_acquisition_starts_poll_the_esi_once(monkeypatch):
    """2026-11-11 lab: after an OFF/ON, the ON thread and Explorer's recording start each started a loop:
    two polls per second, lock timeouts at every lost reply and 50 MB of worker log per hour."""
    module = _load_plugin()
    base = next(c for c in module.ESIController.__mro__[1:] if "startAcquisition" in vars(c))
    monkeypatch.setattr(base, "startAcquisition", _explorer_start_acquisition)
    c, polls = _polling_controller(module)
    c.startAcquisition()  # the loop of the previous connection
    time.sleep(.05)
    barrier = threading.Barrier(2)
    starters = [threading.Thread(target=lambda: (barrier.wait(), c.startAcquisition())) for _ in range(2)]
    for thread in starters:
        thread.start()
    for thread in starters:
        thread.join(5)
    time.sleep(.2)
    since = time.monotonic() - .1
    pollers = {thread for thread, at in polls if at > since}
    c.acquiring = False
    assert pollers == {c.acquisitionThread}, pollers


def test_a_superseded_acquisition_loop_stops():
    module = _load_plugin()
    c, polls = _polling_controller(module)
    c.acquiring = True
    c.acquisitionThread = old = threading.Thread(target=c.runAcquisition, daemon=True)
    old.start()
    time.sleep(.05)
    c.acquisitionThread = threading.current_thread()  # a newer loop took over
    old.join(1)
    assert not old.is_alive() and c.acquiring
