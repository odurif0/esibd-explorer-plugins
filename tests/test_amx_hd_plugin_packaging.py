"""Packaging checks for the standalone ESIBD Explorer AMX HD plugin."""

from __future__ import annotations

from pathlib import Path

from PIL import Image
from support.amx import load_plugin


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_HD_PATH = ROOT / "amx_hd" / "amx_hd_plugin.py"
PLUGIN_AMX_PATH = ROOT / "amx_a" / "amx_plugin.py"
ICON_HD_PATH = ROOT / "amx_hd" / "amx_hd.png"
DLL_HD_PATH = ROOT / "amx_hd" / "vendor" / "runtime" / "amx_hd" / "vendor" / "x64" / "COM-HVAMX4EDH.dll"
HEADER_HD_PATH = ROOT / "amx_hd" / "vendor" / "runtime" / "amx_hd" / "vendor" / "COM-HVAMX4EDH.h"


def test_amx_hd_plugin_exposes_expected_metadata():
    module = load_plugin("amx_hd")

    assert ICON_HD_PATH.exists()
    assert module.providePlugins() == [module.AMXHDDevice]
    assert module.AMXHDDevice.name == "AMX_HD"
    assert module.AMXHDDevice.supportedVersion == "1.0.2"
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
    module = load_plugin("amx_hd")

    assert module._BUNDLED_RUNTIME_NAMESPACE_PREFIX == "_esibd_bundled_amx_hd_runtime"
    # plugin_key is derived from the plugin directory name -> distinct from amx_a
    hd_module_name = module._bundled_runtime_module_name(PLUGIN_HD_PATH.parent)
    amx_module_name_stub = "_esibd_bundled_amx_runtime_amx_a"
    assert hd_module_name == "_esibd_bundled_amx_hd_runtime_amx_hd"
    assert hd_module_name != amx_module_name_stub


def test_amx_hd_driver_class_loads_from_private_runtime():
    module = load_plugin("amx_hd")

    driver_class = module._get_amx_driver_class()
    assert driver_class.__name__ == "AMXHD"
    # The HD controller must NOT be the normal AMX controller
    assert driver_class.__name__ != "AMX"


def test_amx_a_and_amx_hd_load_as_distinct_autonomous_plugins():
    amx_module = load_plugin("amx_a")
    amx_hd_module = load_plugin("amx_hd")

    # Distinct device identities
    assert amx_module.AMXDevice.name == "AMX_A"
    assert amx_hd_module.AMXHDDevice.name == "AMX_HD"
    assert amx_module.AMXDevice is not amx_hd_module.AMXHDDevice

    # Distinct bundled-runtime namespaces / module names (no cross-contamination)
    assert amx_module._BUNDLED_RUNTIME_NAMESPACE_PREFIX != amx_hd_module._BUNDLED_RUNTIME_NAMESPACE_PREFIX
    assert (
        amx_module._bundled_runtime_module_name(PLUGIN_AMX_PATH.parent)
        != amx_hd_module._bundled_runtime_module_name(PLUGIN_HD_PATH.parent)
    )

    # Distinct vendor DLLs
    amx_dll = (PLUGIN_AMX_PATH.parent / "vendor" / "runtime" / "amx" / "vendor" / "x64" / "COM-HVAMX4ED.dll")
    assert amx_dll.exists()
    assert amx_dll != DLL_HD_PATH
