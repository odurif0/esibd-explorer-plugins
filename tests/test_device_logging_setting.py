"""Explorer's per-device Logging switch is read by none of these plugins, so they do not offer it."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

import explorer_host

ROOT = Path(__file__).resolve().parents[1]
POP = 'settings.pop(f"{self.name}/Logging", None)'


def _device_classes():
    for path in sorted(ROOT.glob("*/*_plugin.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and base.id == "Device" for base in node.bases
            ):
                yield path, node


def test_every_device_plugin_drops_the_logging_setting():
    devices = list(_device_classes())
    assert len(devices) == 13  # 15 folders minus MScan and Transmission.
    for path, node in devices:
        where = f"{path.relative_to(ROOT)}:{node.name}"
        defaults = {
            stmt.targets[0].id: stmt.value.value
            for stmt in node.body
            if isinstance(stmt, ast.Assign) and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
        }
        assert defaults.get("log") is False, where  # Code reading device.log still gets a value.
        method = next(stmt for stmt in node.body
                      if isinstance(stmt, ast.FunctionDef) and stmt.name == "getDefaultSettings")
        assert POP in ast.unparse(method).replace("'", '"'), where


def test_explorer_still_names_the_device_setting_logging():
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    if explorer_host.interpreter_version(python) != explorer_host.TARGET_VERSION:
        pytest.skip(f"needs ESIBD Explorer {explorer_host.TARGET_VERSION}")
    probe = ("import inspect, sys, types; sys.modules['pyautogui'] = types.ModuleType('pyautogui');"
             "from esibd.plugins import Device;"
             "print(Device.LOGGING, \"f'{self.name}/{self.LOGGING}'\" in inspect.getsource(Device.getDefaultSettings))")
    result = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.split()[-2:] == ["Logging", "True"], result.stdout
