"""ESI's optional host launcher preserves private RPC and crash isolation."""

import importlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest

from test_esi_plugin_behavior import _load_plugin
from test_esi_process_recovery import FAKE_CONTROLLER, WORKER


def load_worker():
    spec = importlib.util.spec_from_file_location("esi_host_worker_test", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def explorer_runtime():
    source = os.environ.get("ESIBD_EXPLORER_SOURCE")
    if not source:
        pytest.skip("Set ESIBD_EXPLORER_SOURCE to exercise the real host worker API")
    path = Path(source) / "esibd/worker_runtime.py"
    if not path.is_file():
        pytest.skip("This Explorer source does not supply the worker API")
    spec = importlib.util.spec_from_file_location("esi_real_explorer_worker_runtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("operation", ["roundtrip", "crash", "block"])
def test_host_launcher_owns_interpreter_but_esi_owns_rpc_and_shutdown(tmp_path, monkeypatch, operation):
    module = load_worker()
    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)
    interpreter = sys.executable
    calls = []

    def host_launcher(worker_file):
        calls.append(worker_file)
        return subprocess.Popen([interpreter, "-I", "-B", "-u", str(worker_file)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def unexpected_fallback():
        pytest.fail("A host-owned worker must not consult the plugin interpreter")

    monkeypatch.setattr(module, "_worker_python", unexpected_fallback)
    proxy = module.ESIProcessProxy({}, controller_file=str(fake), worker_launcher=host_launcher,
                                  startup_timeout_s=5.)
    try:
        assert calls == [WORKER.resolve()]
        assert proxy._command[:4] == [interpreter, "-I", "-B", "-u"]
        if operation == "roundtrip":
            assert proxy.call_method("ping", rpc_timeout_s=2.)[0] == b"config bytes"
        else:
            with pytest.raises(RuntimeError):
                proxy.call_method(operation, rpc_timeout_s=.2)
            assert proxy.closed and proxy._process.poll() is not None
    finally:
        assert proxy.close()


def test_failed_host_launcher_never_tries_another_interpreter(monkeypatch):
    module = load_worker()

    def fail(worker_file):
        raise RuntimeError("Explorer worker Python is incomplete")

    monkeypatch.setattr(module, "_worker_python", lambda: pytest.fail("unexpected local fallback"))
    with pytest.raises(RuntimeError, match="Explorer worker Python is incomplete"):
        module.ESIProcessProxy({}, worker_launcher=fail)


def test_isolated_driver_passes_launcher_only_to_parent_proxy(monkeypatch):
    module = _load_plugin()
    driver = module._get_esi_driver_class()
    process = importlib.import_module(driver.__module__.rsplit(".", 1)[0] + "._process")
    launched = []
    launcher = lambda script: None

    def create(kwargs, **options):
        assert "worker_launcher" not in kwargs, "a host callback must never be pickled into the child"
        launched.append((kwargs, options))
        return SimpleNamespace(closed=False)

    monkeypatch.setattr(process, "ESIProcessProxy", create)
    device = driver(device_id="host_launcher_test", com=16, process_backend=True, worker_launcher=launcher)
    assert device._backend_mode == "process"
    assert launched[0][1] == {"worker_launcher": launcher}


def test_plugin_selects_native_worker_without_host_python_launcher(tmp_path, monkeypatch):
    module = _load_plugin()
    options, messages = [], []
    launcher = lambda script: None

    def fail_driver(**kwargs):
        options.append(kwargs)
        raise RuntimeError("simulated host runtime failure")

    monkeypatch.setattr(module, "_get_esi_driver_class", lambda: fail_driver)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    parent = SimpleNamespace(com=16, baudrate=230400, connect_timeout_s=.2,
                             pluginManager=SimpleNamespace(launchWorkerProcess=launcher,
                                                           Settings=SimpleNamespace(dataPath=tmp_path)))
    controller = module.ESIController(parent)
    controller.print = lambda message, **kwargs: messages.append(message)
    controller.initializing = True
    controller.runInitialization()
    assert len(options) == 1
    assert options[0]["native_backend"] is True
    assert "worker_launcher" not in options[0]
    assert "process_backend" not in options[0]
    assert options[0]["log_dir"] == tmp_path / "logs/esi"
    assert controller.device is None and not controller.initializing
    assert any("plugin-local native Rust worker" in message for message in messages)
    assert any("simulated host runtime failure" in message for message in messages)
    assert not any("plugin's private Python" in message for message in messages)


def test_real_explorer_launcher_keeps_other_device_alive_and_allows_restart(explorer_runtime, tmp_path, monkeypatch):
    module = load_worker()
    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    monkeypatch.setattr(module, "_worker_python", lambda: pytest.fail("unexpected local interpreter"))
    created = []

    def create():
        proxy = module.ESIProcessProxy({}, controller_file=str(fake), startup_timeout_s=5.,
                                      worker_launcher=explorer_runtime.launch_worker_process)
        created.append(proxy)
        return proxy

    try:
        blocked, other = create(), create()
        with pytest.raises(RuntimeError):
            blocked.call_method("block", rpc_timeout_s=.2)
        assert blocked.closed and blocked._process.poll() is not None
        assert other.call_method("ping", rpc_timeout_s=2.)[0] == b"config bytes"
        replacement = create()
        assert replacement.call_method("add", 1, rpc_timeout_s=2.) == 1
        assert other.call_method("add", 1, rpc_timeout_s=2.) == 1
    finally:
        for proxy in created:
            assert proxy.close()


@pytest.mark.slow
@pytest.mark.skipif(not sys.platform.startswith("win") and not os.environ.get("ESIBD_ESI_TEST_WINE"),
                    reason="requires Windows or explicit ESIBD_ESI_TEST_WINE=1")
@pytest.mark.parametrize("operation", ["roundtrip", "crash", "block", "vendor_bootstrap"])
def test_shared_official_python_worker_without_hardware(explorer_runtime, tmp_path, monkeypatch, operation):
    module = load_worker()
    directory = tmp_path / "Explorer installation"
    shutil.copytree(WORKER.parents[2] / "python", directory / "worker-python")
    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)

    def windows_path(path):
        path = Path(path).resolve()
        return str(path) if sys.platform.startswith("win") else str(PureWindowsPath("Z:/", *path.parts[1:]))

    if not sys.platform.startswith("win"):
        wine = shutil.which("wine")
        if not wine:
            pytest.skip("Wine is not installed")
        original_popen = subprocess.Popen

        def launch(command, **kwargs):
            return original_popen([wine, windows_path(command[0]), *command[1:-1], windows_path(command[-1])], **kwargs)

        monkeypatch.setattr(explorer_runtime.subprocess, "Popen", launch)
        # Simulate only host discovery; real Win32 loader reset requires a Windows host.
        monkeypatch.setattr(explorer_runtime, "get_worker_python",
                            lambda: explorer_runtime.validate_worker_python(directory / "worker-python"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(directory / "ESIBD Explorer.exe"))
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "invalid external interpreter must be ignored")
    monkeypatch.setattr(module, "_worker_python", lambda: pytest.fail("unexpected local interpreter"))
    log_dir = tmp_path / "data/logs/esi"
    kwargs = {"device_id": "shared_runtime_bootstrap", "com": 16,
              "log_dir": windows_path(log_dir)} if operation == "vendor_bootstrap" else {}
    proxy = module.ESIProcessProxy(kwargs, controller_file=None if kwargs else windows_path(fake),
                                  worker_launcher=explorer_runtime.launch_worker_process)
    try:
        assert explorer_runtime.get_worker_python() == directory / "worker-python/python.exe"
        if operation == "vendor_bootstrap":
            assert proxy.get_attribute("connected", timeout_s=3.) is False
            assert (log_dir / "esi_shared_runtime_bootstrap.log").is_file()
        elif operation == "roundtrip":
            assert proxy.call_method("ping", rpc_timeout_s=3.)[0] == b"config bytes"
        else:
            with pytest.raises(RuntimeError):
                proxy.call_method(operation, rpc_timeout_s=.3)
            assert proxy.closed and proxy._process.poll() is not None
    finally:
        assert proxy.close()


@pytest.mark.slow
@pytest.mark.skipif(sys.platform.startswith("win") or not os.environ.get("ESIBD_ESI_TEST_WINE"),
                    reason="explicit Wine probe of the real Win32 host launcher")
def test_windows_launcher_resets_and_restores_real_dll_directory_under_wine(explorer_runtime, tmp_path):
    wine = shutil.which("wine")
    if not wine:
        pytest.skip("Wine is not installed")
    directory = tmp_path / "Explorer installation"
    shutil.copytree(WORKER.parents[2] / "python", directory / "worker-python")
    fake = tmp_path / "controller.py"
    fake.write_text(FAKE_CONTROLLER)
    internal = directory / "_internal"
    internal.mkdir()
    probe = tmp_path / "windows_host_probe.py"
    probe.write_text('''
import importlib.util
import sys
from pathlib import Path

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

runtime = load("real_windows_host", sys.argv[1])
worker = load("real_windows_esi", sys.argv[2])
directory = Path(sys.argv[3])
sys.frozen = True
sys.executable = str(directory / "ESIBD Explorer.exe")
sys._MEIPASS = str(directory / "_internal")
kernel = runtime._windows_kernel()
assert kernel.SetDllDirectoryW(sys._MEIPASS)

def current_dll_directory():
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    assert kernel.GetDllDirectoryW(len(buffer), buffer)
    return buffer.value

assert current_dll_directory() == sys._MEIPASS
created = []
def create():
    proxy = worker.ESIProcessProxy({}, controller_file=sys.argv[4],
                                  worker_launcher=runtime.launch_worker_process)
    created.append(proxy)
    assert proxy._command[0] == str(directory / "worker-python/python.exe")
    assert current_dll_directory() == sys._MEIPASS
    return proxy

try:
    first, other = create(), create()
    try:
        first.call_method("crash", rpc_timeout_s=3)
        raise AssertionError("worker should crash")
    except RuntimeError:
        assert first.closed
    assert other.call_method("ping", rpc_timeout_s=3)[0] == b"config bytes"
    replacement = create()
    assert replacement.call_method("add", 1, rpc_timeout_s=3) == 1
    print("windows-host-launcher-ok", flush=True)
finally:
    for proxy in created:
        assert proxy.close()
    assert current_dll_directory() == sys._MEIPASS
''')

    def windows_path(path):
        return str(PureWindowsPath("Z:/", *Path(path).resolve().parts[1:]))

    result = subprocess.run(
        [wine, windows_path(directory / "worker-python/python.exe"), "-I", "-B", "-u",
         windows_path(probe), windows_path(explorer_runtime.__file__), windows_path(WORKER),
         windows_path(directory), windows_path(fake)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "windows-host-launcher-ok" in result.stdout
