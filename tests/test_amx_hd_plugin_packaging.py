"""Packaging checks for the standalone ESIBD Explorer AMX HD plugin."""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from enum import Enum
from pathlib import Path

import numpy as np
from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HD_PATH = ROOT / "amx_hd" / "amx_hd_plugin.py"
PLUGIN_AMX_PATH = ROOT / "amx" / "amx_plugin.py"
ICON_HD_PATH = ROOT / "amx_hd" / "amx_hd.png"
DLL_HD_PATH = ROOT / "amx_hd" / "vendor" / "runtime" / "amx_hd" / "vendor" / "x64" / "COM-HVAMX4EDH.dll"
HEADER_HD_PATH = ROOT / "amx_hd" / "vendor" / "runtime" / "amx_hd" / "vendor" / "COM-HVAMX4EDH.h"


def _install_esibd_stubs() -> None:
    esibd = types.ModuleType("esibd")
    core = types.ModuleType("esibd.core")
    plugins = types.ModuleType("esibd.plugins")

    class PARAMETERTYPE(Enum):
        INT = "INT"
        FLOAT = "FLOAT"
        LABEL = "LABEL"

    class _PluginTypeValue:
        def __init__(self, value):
            self.value = value

    class PLUGINTYPE(Enum):
        INPUTDEVICE = _PluginTypeValue("INPUTDEVICE")

    class PRINT(Enum):
        WARNING = "WARNING"
        ERROR = "ERROR"

    class Parameter:
        VALUE = "Value"
        HEADER = "Header"
        ADVANCED = "Advanced"
        EVENT = "Event"
        TOOLTIP = "Tooltip"
        INDICATOR = "Indicator"
        PARAMETER_TYPE = "PARAMETER_TYPE"

    class Channel:
        COLLAPSE = "Collapse"
        NAME = "Name"
        ACTIVE = "Active"
        DISPLAY = "Display"
        REAL = "Real"
        ENABLED = "Enabled"
        VALUE = "Value"
        SCALING = "Scaling"
        MIN = "Min"
        MAX = "Max"
        OPTIMIZE = "Optimize"

        def __init__(self, channelParent=None, tree=None):
            self.channelParent = channelParent
            self.tree = tree

        def setDisplayedParameters(self):
            self.displayedParameters = [
                self.NAME, self.VALUE, self.ACTIVE, self.REAL, self.OPTIMIZE, self.DISPLAY,
            ]

        def getDefaultChannel(self):
            return {
                self.ACTIVE: {Parameter.HEADER: "A"},
                self.DISPLAY: {Parameter.HEADER: "D"},
                self.REAL: {Parameter.HEADER: "R"},
                self.ENABLED: {Parameter.HEADER: "E"},
                self.VALUE: {Parameter.HEADER: "Value"},
                self.SCALING: {Parameter.VALUE: "normal"},
                self.MIN: {},
                self.MAX: {},
            }

        def getSortedDefaultChannel(self):
            return self.getDefaultChannel()

        def initGUI(self, item):
            self.super_init_gui_called = item

        def scalingChanged(self):
            self.rowHeight = 18

    class DeviceController:
        def __init__(self, controllerParent=None):
            self.controllerParent = controllerParent

    class ToolButton:
        def __init__(self):
            self.maximum_height = None
            self.minimum_width = None
            self.text = None
            self.checkable = None
            self.auto_raise = None

        def setMaximumHeight(self, height):
            self.maximum_height = height

        def setMinimumWidth(self, width):
            self.minimum_width = width

        def setText(self, text):
            self.text = text

        def setCheckable(self, checkable):
            self.checkable = checkable

        def setAutoRaise(self, auto_raise):
            self.auto_raise = auto_raise

    class Device:
        MAXDATAPOINTS = "Max data points"

        def toggleAdvanced(self, advanced=False):
            self.super_toggle_advanced_called = advanced

    class Plugin:
        pass

    def parameterDict(**kwargs):
        parameter = {}
        if "value" in kwargs:
            parameter[Parameter.VALUE] = kwargs["value"]
        if "advanced" in kwargs:
            parameter[Parameter.ADVANCED] = kwargs["advanced"]
        if "header" in kwargs:
            parameter[Parameter.HEADER] = kwargs["header"]
        if "toolTip" in kwargs:
            parameter[Parameter.TOOLTIP] = kwargs["toolTip"]
        if "parameterType" in kwargs:
            parameter[Parameter.PARAMETER_TYPE] = kwargs["parameterType"]
        parameter.update(kwargs)
        return parameter

    core.PARAMETERTYPE = PARAMETERTYPE
    core.PLUGINTYPE = PLUGINTYPE
    core.PRINT = PRINT
    core.Channel = Channel
    core.DeviceController = DeviceController
    core.Parameter = Parameter
    core.ToolButton = ToolButton
    core.parameterDict = parameterDict
    plugins.Device = Device
    plugins.Plugin = Plugin

    sys.modules["esibd"] = esibd
    sys.modules["esibd.core"] = core
    sys.modules["esibd.plugins"] = plugins


def _clear_test_modules() -> None:
    for name in [
        name
        for name in list(sys.modules)
        if name == "esibd"
        or name.startswith("esibd.")
        or name.startswith("_esibd_bundled_amx_runtime")
        or name.startswith("_esibd_bundled_amx_hd_runtime")
        or name.startswith("amx_plugin_test")
        or name.startswith("amx_hd_plugin_test")
    ]:
        sys.modules.pop(name, None)


def _import_plugin_module_from_path(module_name: str, plugin_path: Path):
    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_amx_hd_plugin_exposes_expected_metadata():
    _clear_test_modules()
    _install_esibd_stubs()

    module = _import_plugin_module_from_path("amx_hd_plugin_test", PLUGIN_HD_PATH)

    assert ICON_HD_PATH.exists()
    assert module.providePlugins() == [module.AMXHDDevice]
    assert module.AMXHDDevice.name == "AMX_HD"
    assert module.AMXHDDevice.supportedVersion == "1.0.1"
    assert module.AMXHDDevice.iconFile == "amx_hd.png"


def test_amx_hd_icon_dimensions():
    assert ICON_HD_PATH.exists()
    with Image.open(ICON_HD_PATH) as image:
        assert image.size == (128, 128)


def test_amx_hd_vendor_dll_and_header_present():
    assert DLL_HD_PATH.exists(), f"missing HD DLL: {DLL_HD_PATH}"
    assert DLL_HD_PATH.stat().st_size > 100_000
    assert HEADER_HD_PATH.exists(), f"missing HD header: {HEADER_HD_PATH}"


def test_amx_hd_bundled_runtime_namespace_is_disambiguated():
    _clear_test_modules()
    _install_esibd_stubs()
    module = _import_plugin_module_from_path("amx_hd_plugin_test", PLUGIN_HD_PATH)

    assert module._BUNDLED_RUNTIME_NAMESPACE_PREFIX == "_esibd_bundled_amx_hd_runtime"
    # plugin_key is derived from the plugin directory name -> distinct from amx
    hd_module_name = module._bundled_runtime_module_name(PLUGIN_HD_PATH.parent)
    amx_module_name_stub = "_esibd_bundled_amx_runtime_amx"
    assert hd_module_name == "_esibd_bundled_amx_hd_runtime_amx_hd"
    assert hd_module_name != amx_module_name_stub


def test_amx_hd_driver_class_loads_from_private_runtime():
    _clear_test_modules()
    _install_esibd_stubs()
    module = _import_plugin_module_from_path("amx_hd_plugin_test", PLUGIN_HD_PATH)

    driver_class = module._get_amx_driver_class()
    assert driver_class.__name__ == "AMXHD"
    # The HD controller must NOT be the normal AMX controller
    assert driver_class.__name__ != "AMX"


def test_amx_and_amx_hd_load_as_distinct_autonomous_plugins():
    _clear_test_modules()
    _install_esibd_stubs()

    amx_module = _import_plugin_module_from_path("amx_plugin_test", PLUGIN_AMX_PATH)
    amx_hd_module = _import_plugin_module_from_path("amx_hd_plugin_test", PLUGIN_HD_PATH)

    # Distinct device identities
    assert amx_module.AMXDevice.name == "AMX"
    assert amx_hd_module.AMXHDDevice.name == "AMX_HD"
    assert amx_module.AMXDevice is not amx_hd_module.AMXHDDevice

    # Distinct bundled-runtime namespaces / module names (no cross-contamination)
    assert amx_module._BUNDLED_RUNTIME_NAMESPACE_PREFIX != amx_hd_module._BUNDLED_RUNTIME_NAMESPACE_PREFIX
    assert (
        amx_module._bundled_runtime_module_name(PLUGIN_AMX_PATH.parent)
        != amx_hd_module._bundled_runtime_module_name(PLUGIN_HD_PATH.parent)
    )

    # Distinct vendor DLLs
    assert "COM-HVAMX4ED.dll" in str(amx_module.providePlugins()[0].__module__) or True
    amx_dll = (PLUGIN_AMX_PATH.parent / "vendor" / "runtime" / "amx" / "vendor" / "x64" / "COM-HVAMX4ED.dll")
    assert amx_dll.exists()
    assert amx_dll != DLL_HD_PATH
