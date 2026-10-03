"""Explorer 1.0.2 runs its periodic configuration save from a worker thread.

The export refreshes Explorer's file tree, so every plugin forwards that call to
the Qt GUI thread instead of touching widgets from the worker.
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace

import pytest

import test_ampr_plugin_channel_sync
import test_amx_hd_plugin_behavior
import test_amx_plugin_behavior
import test_dmmr_plugin_behavior
import test_esi_plugin_behavior
import test_psu_plugin_behavior

ROOT = Path(__file__).resolve().parents[1]


def _load_tpg366(monkeypatch):
    core, plugins = ModuleType("esibd.core"), ModuleType("esibd.plugins")
    for name in ("Channel", "DeviceController"):
        setattr(core, name, type(name, (), {}))
    core.PARAMETERTYPE = core.PRINT = core.Parameter = SimpleNamespace()
    core.PLUGINTYPE = SimpleNamespace(OUTPUTDEVICE="Output device")
    core.getTestMode, core.parameterDict = (lambda: False), (lambda **kwargs: kwargs)
    plugins.Device = type("Device", (), {})
    monkeypatch.setitem(sys.modules, "esibd.core", core)
    monkeypatch.setitem(sys.modules, "esibd.plugins", plugins)
    monkeypatch.setitem(sys.modules, "serial", sys.modules.get("serial") or ModuleType("serial"))
    spec = importlib.util.spec_from_file_location("tpg366_periodic_save_test", ROOT / "tpg366/tpg366_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PLUGINS = {
    "ampr": (lambda mp: test_ampr_plugin_channel_sync._load_module(), "AMPRDevice"),
    "amx": (lambda mp: test_amx_plugin_behavior._load_module(), "AMXDevice"),
    "amx_hd": (lambda mp: test_amx_hd_plugin_behavior._load_hd_plugin_module(), "AMXHDDevice"),
    "dmmr": (lambda mp: test_dmmr_plugin_behavior._load_module(), "DMMRDevice"),
    "esi": (lambda mp: test_esi_plugin_behavior._load_plugin(), "ESIDevice"),
    "psu": (lambda mp: test_psu_plugin_behavior._load_module(), "PSUDevice"),
    "tpg366": (_load_tpg366, "TPG366"),
}


@pytest.fixture(scope="module")
def app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("plugin", sorted(PLUGINS))
def test_periodic_configuration_save_runs_on_the_gui_thread(app, plugin, monkeypatch):
    load, class_name = PLUGINS[plugin]
    module = load(monkeypatch)
    calls = []
    monkeypatch.setattr(module.Device, "exportConfigurationIfChanged",
                        lambda self: calls.append(threading.current_thread()), raising=False)
    cls = getattr(module, class_name)
    device = cls.__new__(cls)

    device.exportConfigurationIfChanged()  # Explorer's direct call on the GUI thread
    assert calls == [threading.main_thread()]

    worker = threading.Thread(target=device.exportConfigurationIfChanged)  # Explorer 1.0.2's thread
    worker.start()
    worker.join()
    assert len(calls) == 1, "the worker must not run the export itself"
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert calls == [threading.main_thread()] * 2


def test_hosts_without_periodic_save_are_unaffected(app, monkeypatch):
    module = test_dmmr_plugin_behavior._load_module()
    monkeypatch.delattr(module.Device, "exportConfigurationIfChanged", raising=False)
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.exportConfigurationIfChanged()  # Explorer 1.0.1 has no base method: nothing to do
