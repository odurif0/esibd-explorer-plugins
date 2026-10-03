"""Reproduce host defects against the real Explorer source, without instruments."""
from __future__ import annotations

import ast
import builtins
import configparser
import copy
from datetime import datetime
import __future__
import os
from pathlib import Path
from types import SimpleNamespace as NS
from typing import cast

import pytest


@pytest.fixture
def host_source():
    root = Path(os.environ.get("ESIBD_EXPLORER_SOURCE", str(Path.home() / "Git/ESIBD-Explorer"))) / "esibd"
    if not (root / "core.py").is_file():
        pytest.skip("Real Explorer source is required")
    return root


def definition(path, name, class_name=None):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    if class_name:
        tree = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    node = copy.deepcopy(next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name))
    node.decorator_list = []
    return node


def compile_function(node, namespace):
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, "real-explorer-function", "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace[node.name]


@pytest.mark.parametrize("value, expected", [(0., "0.00"), (123.456, "123.46"), (.02, "0.02"),
                                             (1e-12, "1.00e-12"), (-2.5e-9, "-2.50e-09")])
def test_linear_cursor_retains_small_nonzero_measurements(host_source, value, expected):
    namespace = dict(cast=cast, datetime=datetime, QMouseEvent=type("MouseEvent", (), {}))
    tree = ast.parse((host_source / "core.py").read_text(encoding="utf-8"))
    if any(isinstance(node, ast.FunctionDef) and node.name == "_format_linear_plot_value" for node in tree.body):
        compile_function(definition(host_source / "core.py", "_format_linear_plot_value"), namespace)
    move = compile_function(definition(host_source / "core.py", "mouseMoveEvent", "PlotItem"), namespace)
    texts = []
    geometry = NS(contains=lambda point: True, width=lambda: 800)
    view = NS(geometry=lambda: geometry, mapSceneToView=lambda point: point)
    label = NS(setText=texts.append, setPos=lambda *args: None, boundingRect=lambda: NS(width=lambda: 200))
    plot = NS(showXY=True, getViewBox=lambda: view, xyLabel=label,
              ctrl=NS(logYCheck=NS(isChecked=lambda: False)))
    move(plot, NS(x=lambda: 1700000000., y=lambda: value))
    assert texts[-1].endswith(f"y = {expected}")


def test_missing_name_uses_defaults_without_crashing_warning(host_source):
    original = definition(host_source / "core.py", "initGUI", "Channel")
    loop = next(node for node in original.body if isinstance(node, ast.For)
                and isinstance(node.iter, ast.Call) and isinstance(node.iter.func, ast.Attribute)
                and node.iter.func.attr == "items")
    wrapper = ast.parse("def restore(self, item):\n    pass\n").body[0]
    wrapper.body = [loop]
    manager_type = type("ChannelManager", (), {})
    manager = manager_type()
    manager.channelsChanged = False
    fields = {"Name": NS(value=None), "Foo": NS(value=None)}
    warnings = []
    channel = NS(NAME="Name", VALUE="Value", parameters=[], channelParent=manager,
                 pluginManager=NS(ChannelManager=manager_type), tempParameters=lambda: [],
                 getParameterByName=fields.__getitem__, print=warnings.append,
                 getSortedDefaultChannel=lambda: {"Name": {"Restore": True, "Value": "Default"},
                                                    "Foo": {"Restore": True, "Value": 50}})
    restore = compile_function(wrapper, dict(Parameter=NS(RESTORE="Restore")))
    restore(channel, {"Foo": 3, "Other": True})
    assert fields["Name"].value == "Default"
    assert fields["Foo"].value == 3
    assert manager.channelsChanged and "<unnamed>" in warnings[0]


def test_settings_read_matches_utf8_writer_in_non_utf8_locale(host_source, tmp_path, monkeypatch):
    node = definition(host_source / "plugins.py", "loadSettings", "SettingsManager")
    names = {child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
             and isinstance(child.value, ast.Name) and child.value.id == "Parameter"}
    parameter = NS(**{name: name.lower() for name in names})
    defaults = {name.lower(): 0 for name in names}
    defaults.update(value="default", default="default", internal=False, items="", toolTip="",
                    widget=None, parameter_type="text", min=0, max=1)
    captured = []
    tree = NS(collapseAll=lambda: None, topLevelItem=lambda index: index, expandItem=lambda item: None)
    settings = NS(loading=False, UTF8="utf-8", defaultSettings={"Detector/Label": defaults},
                  parentPlugin=NS(name="DMMR"), SETTINGS="Settings", tree=tree,
                  updateSettings=lambda items, file: captured.extend(items), print=lambda *args, **kwargs: None,
                  saveSettings=lambda **kwargs: pytest.fail("Existing settings must not be overwritten"))
    file = tmp_path / "settings.ini"
    file.write_text("[Detector/Label]\nvalue = µA – capillary °C\ndefault = default\n", encoding="utf-8")
    real_open = builtins.open
    def locale_open(path, mode="r", **kwargs):
        if "b" not in mode:
            if kwargs.get("encoding") in (None, "locale"):
                kwargs["encoding"] = "cp1252"
        return real_open(path, mode, **kwargs)
    monkeypatch.setattr(builtins, "open", locale_open)
    load = compile_function(node, dict(configparser=configparser, Parameter=parameter,
                                      Path=Path, FILE_INI=".ini", cast=cast,
                                      parameterDict=lambda **kwargs: kwargs))
    load(settings, file=file)
    assert captured[0]["value"] == "µA – capillary °C"
    assert not settings.loading


def test_all_host_ini_readers_have_an_explicit_encoding(host_source):
    readers = []
    for path in (host_source / "core.py", host_source / "plugins.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "read"
                    and ((isinstance(node.func.value, ast.Name) and node.func.value.id == "confParser")
                         or (isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "confParser"))):
                readers.append((path.name, node.lineno))
                assert any(keyword.arg == "encoding" for keyword in node.keywords), (path, node.lineno)
    assert len(readers) == 7
