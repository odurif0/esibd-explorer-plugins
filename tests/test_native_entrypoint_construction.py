"""Exercise production constructor arguments through the real private facades."""
from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

from conftest import PLUGIN_SPECS


ROOT = Path(__file__).resolve().parents[1]
DLL_SPECS = tuple(spec for spec in PLUGIN_SPECS if spec.dll is not None)
CLASSES = {"ampr": "AMPR", "amx": "AMX", "amx_hd": "AMXHD",
           "dmmr": "DMMR", "esi": "ESI", "psu": "PSU"}


@pytest.mark.parametrize("spec", DLL_SPECS, ids=lambda spec: spec.folder)
def test_entrypoint_arguments_construct_real_native_facade(spec, tmp_path, monkeypatch):
    path = ROOT / spec.folder / spec.entrypoint
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and any(keyword.arg == "native_backend"
                     and isinstance(keyword.value, ast.Constant) and keyword.value.value is True
                     for keyword in node.keywords)]
    assert len(calls) == 1, "Each DLL entrypoint must have one explicit native constructor"
    package_name = f"_test_native_constructor_{spec.folder}"
    runtime_dir = ROOT / spec.folder / "vendor/runtime"
    module_spec = importlib.util.spec_from_file_location(
        package_name, runtime_dir / "__init__.py", submodule_search_locations=[str(runtime_dir)])
    assert module_spec is not None and module_spec.loader is not None
    runtime = importlib.util.module_from_spec(module_spec)
    monkeypatch.setitem(sys.modules, package_name, runtime)
    captured = []
    try:
        module_spec.loader.exec_module(runtime)
        transport = importlib.import_module(f"{package_name}._native_worker")
        driver_class = getattr(runtime, CLASSES[spec.runtime_family])

        class ConstructorProbe:
            def __init__(self, root, family, config, **options):
                # Only spawning is substituted. Argument admission, filtering,
                # DLL path selection and serialization use the real runtime.
                transport.encode(config)
                captured.append((root, family, config, options, self))

        def forbidden(*args, **kwargs):
            raise AssertionError("An inline controller must never be constructed")

        monkeypatch.setattr(transport, "NativeWorkerProxy", ConstructorProbe)
        monkeypatch.setattr(driver_class, "_PROCESS_CONTROLLER_CLASS", forbidden)
        parent = NS(name=spec.manager_name, com=16, baudrate=230400,
                    pluginManager=NS(Settings=NS(dataPath=tmp_path)))
        call = calls[0]
        environment = {"actual_driver": driver_class, "self": NS(controllerParent=parent),
                       "Path": Path, "logging": logging}
        # ESI names its log directory before constructing the facade.
        owner = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                     and call in tuple(ast.walk(node)))
        for keyword in call.keywords:
            if keyword.arg == "log_dir" and isinstance(keyword.value, ast.Name):
                name = keyword.value.id
                assignment = next(node for node in ast.walk(owner) if isinstance(node, ast.Assign)
                                  and any(isinstance(target, ast.Name) and target.id == name
                                          for target in node.targets))
                environment[name] = eval(compile(ast.Expression(assignment.value), str(path), "eval"),
                                         environment)
        constructor = ast.copy_location(ast.Call(func=ast.Name(id="actual_driver", ctx=ast.Load()),
                                                args=call.args, keywords=call.keywords), call)
        expression = ast.Expression(constructor)
        ast.fix_missing_locations(expression)
        device = eval(compile(expression, str(path), "eval"), environment)
        assert len(captured) == 1
        root, family, config, options, backend = captured[0]
        assert root == ROOT / spec.folder and family == spec.runtime_family
        assert config["device_id"] == f"{spec.manager_name.lower()}_com16"
        assert config["com"] == 16 and config["baudrate"] == 230400
        assert Path(config["dll_path"]).is_file()
        assert not {"native_backend", "logger", "thread_lock", "hk_thread"}.intersection(config)
        assert options["log_dir"] == tmp_path / "logs" / spec.manager_name.lower()
        assert object.__getattribute__(device, "_backend") is backend
        assert object.__getattribute__(device, "_backend_mode") == "process"
    finally:
        for name in tuple(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)
