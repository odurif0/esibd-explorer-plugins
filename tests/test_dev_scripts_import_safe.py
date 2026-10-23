"""Explorer imports every .py file of every folder in its plugin path.

2026-10-22: a lab install held the repository's tools/ folder next to the plugins.
Explorer 1.0.2 imported tools/smoke_native_worker.py, whose module-level argparse
rejected Explorer's command line and raised SystemExit: Explorer closed at startup.
Explorer only catches Exception, so no development script may exit, exec or parse
argv when imported.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEV_SCRIPTS = sorted(path for folder in ROOT.iterdir()
                     if folder.is_dir() and not folder.name.startswith(".") and folder.name != "tests"
                     and not any(folder.glob("*_plugin.py"))
                     for path in folder.glob("*.py"))


def _explorer_import(path):
    # Explorer 1.0.2 esibd.const.dynamicImport: not registered in sys.modules.
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


def test_dev_script_folders_are_found():
    assert {path.parent.name for path in DEV_SCRIPTS} >= {"tools", "native"}


@pytest.mark.parametrize("path", DEV_SCRIPTS, ids=lambda path: path.relative_to(ROOT).as_posix())
def test_importing_a_dev_script_never_stops_explorer(path, monkeypatch):
    execs = []

    def execv(program, arguments):  # Would replace the Explorer process.
        execs.append(program)
        raise OSError("exec refused by the test")

    monkeypatch.setattr(sys, "argv", ["ESIBD Explorer.exe"])
    monkeypatch.setenv("ESIBD_ZIG", sys.executable)
    monkeypatch.setattr(os, "execv", execv)
    try:
        _explorer_import(path)
    except Exception:  # Explorer reports these and keeps loading the other plugins.
        pass
    except BaseException as error:  # noqa: BLE001 - SystemExit or KeyboardInterrupt closes Explorer.
        pytest.fail(f"importing {path.name} raised {type(error).__name__}: {error}")
    assert execs == []
