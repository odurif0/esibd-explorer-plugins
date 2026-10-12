"""The fake Explorer used by unit tests must keep the names and values of Explorer 1.0.2.

2026-10-12: a plugin used Device.LOGGING, which eleven hand-written stand-ins lacked, and
13 unit tests failed although the real Explorer defines it.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
import re
import sys
import types

import pytest

from support import explorer
from support.qt import run_probe

ENUMS = ("PARAMETERTYPE", "PLUGINTYPE", "PRINT", "INOUT")
CONSTANT_CLASSES = {"Parameter": "esibd.core", "Channel": "esibd.core", "Device": "esibd.plugins",
                    "Scan": "esibd.plugins"}
MODULES = {"esibd.core": explorer.CORE, "esibd.plugins": explorer.PLUGINS}


def _constants(cls):
    return {name: value for name, value in vars(cls).items() if name.isupper() and not callable(value)}


def fake_api():
    return {
        "enums": {name: {m.name: m.value for m in getattr(explorer, name)} for name in ENUMS},
        "constants": {name: _constants(getattr(explorer, name)) for name in CONSTANT_CLASSES},
        "names": sorted(f"{module}.{name}" for module, names in MODULES.items() for name in names),
        "parameterDict": sorted(explorer._PARAMETER_KEYS),
        "INFO": explorer.INFO,
    }


def host_api():
    import importlib
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    core = importlib.import_module("esibd.core")
    modules = {"esibd.core": core, "esibd.plugins": importlib.import_module("esibd.plugins")}
    return {
        "enums": {name: {m.name: m.value for m in getattr(core, name)} for name in ENUMS},
        "constants": {name: _constants(getattr(modules[module], name)) for name, module in CONSTANT_CLASSES.items()},
        "names": sorted(f"{module}.{name}" for module, names in MODULES.items() for name in names
                        if hasattr(modules[module], name)),
        "parameterDict": sorted(set(explorer._PARAMETER_KEYS) & set(inspect.signature(core.parameterDict).parameters)),
        "INFO": core.INFO,
    }


def test_fake_explorer_matches_the_target_host():
    import explorer_host
    if explorer_host.interpreter_version(explorer_host.qt_python()) != explorer_host.TARGET_VERSION:
        pytest.skip(f"needs ESIBD Explorer {explorer_host.TARGET_VERSION}")
    result = run_probe(__file__, "--probe", timeout=120)
    host = json.loads(result.stdout.strip().splitlines()[-1])
    fake = json.loads(json.dumps(fake_api()))
    assert fake["enums"] == host["enums"]
    assert fake["names"] == host["names"]
    assert fake["parameterDict"] == host["parameterDict"]
    assert fake["constants"] == host["constants"]
    assert fake["INFO"] == host["INFO"]


def test_plugins_use_only_explorer_members_that_exist():
    # The fake matches the host exactly, so it can check code paths no unit test runs.
    # 2026-10-12: "Disable interlock" raised AttributeError on PRINT.INFO, absent in Explorer.
    known = {name: {member.name for member in getattr(explorer, name)} for name in ENUMS}
    known["Parameter"] = set(_constants(explorer.Parameter))
    pattern = re.compile(r"\b(" + "|".join(known) + r")\.([A-Z][A-Z_0-9]*)\b")
    unknown = set()
    folders = {path.parent for path in Path(explorer.ROOT).glob("*/*_plugin.py") if path.parent.name != "tests"}
    for path in (path for folder in folders for path in folder.rglob("*.py")):
        for owner, member in pattern.findall(path.read_text(encoding="utf-8")):
            if member not in known[owner]:
                unknown.add(f"{path.relative_to(explorer.ROOT)}: {owner}.{member}")
    assert not unknown, sorted(unknown)


def test_install_refuses_unknown_replacements():
    with pytest.raises(TypeError, match="Unknown Explorer names"):
        explorer.install(Chanel=object)


if __name__ == "__main__" and sys.argv[1:] == ["--probe"]:
    try:
        api = host_api()
    except ImportError as exc:
        print(f"Explorer unavailable: {exc}")
        raise SystemExit(77) from None
    print(json.dumps(api))
