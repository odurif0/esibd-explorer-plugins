"""Explorer 1.0.2 API constraints that the stand-in classes of the other tests cannot reveal."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

import explorer_host

ROOT = Path(__file__).resolve().parents[1]
# Names the plugins use for Explorer Channel objects.
CHANNEL_NAMES = {"ch", "channel", "chan"}


def _plugin_sources():
    return sorted(path for path in ROOT.glob("*/*_plugin.py") if "tests" not in path.parts)


def test_no_plugin_writes_the_read_only_channel_loading():
    # 2026-10-07: "property 'loading' of 'ESIChannel' object has no setter" made every ESI panel action
    # fail, so HV could not be set to 0 from the panel. Use the device's loading counter instead.
    offenders = []
    for path in _plugin_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if (isinstance(target, ast.Attribute) and target.attr == "loading"
                        and isinstance(target.value, ast.Name) and target.value.id in CHANNEL_NAMES):
                    offenders.append(f"{path.relative_to(ROOT)}:{target.lineno}")
    assert len(_plugin_sources()) >= 15
    assert offenders == [], offenders


def test_explorer_channel_loading_is_read_only_on_the_target_host():
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    if explorer_host.interpreter_version(python) != explorer_host.TARGET_VERSION:
        pytest.skip(f"needs ESIBD Explorer {explorer_host.TARGET_VERSION}")
    probe = ("import sys, types; sys.modules['pyautogui'] = types.ModuleType('pyautogui');"
             "from esibd.core import Channel; from esibd.plugins import Plugin;"
             "print(Channel.loading.fset is None, Plugin.loading.fset is not None)")
    result = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=120,
                            env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.split()[-2:] == ["True", "True"], result.stdout  # Channel: read-only; device: counter.
