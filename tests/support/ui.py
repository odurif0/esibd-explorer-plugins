"""Real-Explorer pieces shared by the Qt probe tests."""
from __future__ import annotations
import __future__

import ast
from enum import Enum, auto
from importlib.metadata import distribution
from pathlib import Path
import re
import sys
import time
from types import SimpleNamespace
from typing import cast

import numpy as np


def explorer_state_action():
    """Load the installed Action/StateAction code without starting Explorer."""
    from importlib.metadata import distribution

    from PyQt6.QtCore import QObject, pyqtSignal
    from PyQt6.QtGui import QAction, QIcon

    path = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    tree = ast.parse(path.read_text(encoding="utf-8"))
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in ("Action", "StateAction")]
    assert len(classes) == 2
    ns = dict(QObject=QObject, QAction=QAction, pyqtSignal=pyqtSignal, Icon=QIcon)
    source = "from __future__ import annotations\n" + "\n".join(ast.unparse(node) for node in classes)
    exec(compile(source, str(path), "exec"), ns)
    return ns["StateAction"]


def real_framework():
    from asteval import Interpreter
    from PyQt6 import QtCore, QtGui, QtWidgets
    import pyqtgraph as pg
    root = Path(distribution("esibd-explorer").locate_file("esibd"))
    trees = {name: ast.parse((root / name).read_text()) for name in ("core.py", "const.py", "plugins.py")}
    ns = dict(np=np, pg=pg, re=re, time=time, Path=Path, cast=cast, Enum=Enum, auto=auto,
              pyqtSignal=QtCore.pyqtSignal, pyqtSlot=QtCore.pyqtSlot, Qt=QtCore.Qt,
              Setting=type("Setting", (), {}), getDarkMode=lambda: True,
              colors=SimpleNamespace(fg="#eeeeee", bg="#20232a", highlight="#308cc6"),
              aeval=Interpreter(), CompactComboBox=QtWidgets.QComboBox,
              LedIndicator=QtWidgets.QLabel)
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})

    def extract(file, name, parent=None):
        nodes = trees[file].body if parent is None else parent.body
        node = next(n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(root / file), "exec",
                     flags=__future__.annotations.compiler_flag), ns)
        return ns[name]

    for name in ("INOUT", "PARAMETERTYPE", "PRINT", "PLUGINTYPE", "makeWrapper"):
        extract("const.py", name)
    from explorer_host import add_const_names
    add_const_names(root, ns)  # e.g. Explorer 1.0.1's valid_chars / validateText
    for name in ("DynamicNp", "ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox",
                 "Label", "ColorButton", "CheckBox", "ToolButton", "LineEdit", "Parameter", "parameterDict", "RelayChannel", "Channel"):
        extract("core.py", name)
    ns["ScanChannel"] = type("ScanChannel", (ns["Channel"],), {})
    from support import explorer
    explorer.install()
    core = sys.modules["esibd.core"]
    for name in ("Channel", "Parameter", "PARAMETERTYPE", "PRINT", "PLUGINTYPE", "parameterDict", "ToolButton"):
        setattr(core, name, ns[name])
    Device = sys.modules["esibd.plugins"].Device
    ns["Device"] = ns["ChannelManager"] = Device
    Device.getChannels = lambda self: self.channels
    Device.initialized = property(lambda self: bool(self.controller and self.controller.initialized))
    Device.subtractBackgroundActive = lambda self: False
    Device.getIcon = lambda self, *args, **kwargs: QtGui.QIcon()  # 1.0.1 passes desaturate=
    device_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "Device")
    for name in ("updateValues", "applyValues"):
        setattr(Device, name, extract("plugins.py", name, device_node))
    dm_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "DeviceManager")
    manager = type("DeviceManager", (), {})
    for name in ("channels", "getChannelByName", "getDevices", "getInputDevices", "getOutputDevices", "getRelays", "globalUpdate"):
        setattr(manager, name, extract("plugins.py", name, dm_node))
    ucm_node = next(n for n in trees["plugins.py"].body if isinstance(n, ast.ClassDef) and n.name == "UCM")
    UCMChannel = extract("plugins.py", "UCMChannel", ucm_node)
    ns["UCMChannel"], ns["DeviceManager"] = UCMChannel, manager
    return ns
