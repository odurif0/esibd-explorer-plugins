"""Production ESI uses a native worker, never the retained Python references."""

import ast
import importlib
import importlib.util
from pathlib import Path
import sys

import pytest

from conftest import PLUGIN_SPECS


ROOT = Path(__file__).resolve().parents[1]
ESI = ROOT / "esi"


def test_esi_has_no_bundled_python_interpreter():
    assert not (ESI / "vendor/python").exists()
    spec = next(spec for spec in PLUGIN_SPECS if spec.folder == "esi")
    assert not any(name.startswith("vendor/python/") for name in spec.bundled_files)
    assert {"native/manifest.json", "native/esibd-esi-worker.exe", "native/source.zip",
            "native/THIRD_PARTY_NOTICES.txt", "vendor/runtime/_native_worker.py"} <= set(spec.bundled_files)


def test_legacy_python_sources_remain_reference_only():
    assert (ESI / "vendor/runtime/esi/_process.py").is_file()
    assert (ROOT / "tools/vendor_esi_python.py").is_file()
    module = ast.parse((ESI / "esi_plugin.py").read_text(encoding="utf-8"))
    controller = next(node for node in module.body if isinstance(node, ast.ClassDef) and node.name == "ESIController")
    initialize = next(node for node in controller.body
                      if isinstance(node, ast.FunctionDef) and node.name == "runInitialization")
    constructors = [node for node in ast.walk(initialize) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name) and node.func.id == "driver"]
    assert len(constructors) == 1
    options = {keyword.arg: keyword.value for keyword in constructors[0].keywords}
    assert isinstance(options["native_backend"], ast.Constant) and options["native_backend"].value is True
    assert not {"process_backend", "worker_launcher"}.intersection(options)


@pytest.fixture
def runtime(monkeypatch):
    name = "_esi_native_packaging_contract"
    directory = ESI / "vendor/runtime"
    spec = importlib.util.spec_from_file_location(name, directory / "__init__.py",
                                                 submodule_search_locations=[str(directory)])
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    try:
        yield tuple(importlib.import_module(f"{name}.{suffix}") for suffix in
                    ("esi.esi", "_native_worker", "_driver_common", "esi._process"))
    finally:
        for key in tuple(sys.modules):
            if key.startswith(f"{name}."):
                del sys.modules[key]


def _forbid_python_backends(monkeypatch, runtime):
    driver, transport, common, legacy = runtime

    def forbidden(*args, **kwargs):
        pytest.fail("NO INLINE / NO EXTERNAL / NO PRIVATE PYTHON production fallback")

    monkeypatch.setattr(driver._ESIController, "__init__", forbidden)
    monkeypatch.setattr(legacy, "ESIProcessProxy", forbidden)
    monkeypatch.setattr(legacy, "_worker_python", forbidden)
    monkeypatch.setattr(common, "ControllerProcessProxy", forbidden)
    monkeypatch.setattr(transport.subprocess, "Popen", forbidden)


@pytest.mark.parametrize("frozen", (False, True), ids=("source-host", "frozen-host"))
def test_production_constructor_selects_only_native_worker(tmp_path, monkeypatch, runtime, frozen):
    driver, transport, _, _ = runtime
    launches = []

    class NativeProxy:
        def __init__(self, plugin_root, family, config, **options):
            self.session = "packaging-native-session"
            self.identity = {"family": family, "protocol": transport.PROTOCOL}
            self.closed = False
            launches.append((self, plugin_root, family, config, options))

        def close(self):
            self.closed = True
            return True

    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "Explorer.exe"))
    monkeypatch.setenv("ESIBD_ESI_WORKER_PYTHON", "must-not-launch-external-python")
    _forbid_python_backends(monkeypatch, runtime)
    monkeypatch.setattr(transport, "NativeWorkerProxy", NativeProxy)
    log_dir = tmp_path / "data/logs/esi"
    device = driver.ESI("packaging", com=16, native_backend=True, log_dir=log_dir)
    try:
        assert len(launches) == 1
        backend, root, family, config, options = launches[0]
        assert device._backend is backend and device._backend_mode == "process"
        assert backend.session == "packaging-native-session"
        assert backend.identity == {"family": "esi", "protocol": 1}
        assert root == ESI and family == "esi"
        assert config["device_id"] == "packaging" and config["com"] == 16
        assert config["dll_path"] == str(ESI / "vendor/runtime/esi/vendor/x64/COM-ESI-CTRL.dll")
        assert config["log_dir"] == options["log_dir"] == log_dir
        assert options["startup_timeout_s"] == 30. and options["stop_grace_s"] == 1.
        assert device._process_backend_disabled_reason == ""
    finally:
        device.close()
    assert backend.closed


def test_native_startup_failure_is_fatal_without_python_fallback(tmp_path, monkeypatch, runtime):
    driver, transport, _, _ = runtime
    _forbid_python_backends(monkeypatch, runtime)

    def failed_native_start(*args, **kwargs):
        raise RuntimeError("native packaging startup failed")

    monkeypatch.setattr(transport, "NativeWorkerProxy", failed_native_start)
    with pytest.raises(RuntimeError, match="native packaging startup failed"):
        driver.ESI("packaging-failure", com=16, native_backend=True, log_dir=tmp_path / "data/logs/esi")
