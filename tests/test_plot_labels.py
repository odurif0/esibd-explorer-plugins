"""Renaming channels refreshes legends without resetting measurement history."""
from __future__ import annotations

import ast
import importlib.util
from importlib.metadata import distribution
from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_shutdown_confirmation_regressions import FAMILIES

PLUGINS = (
    ("ampr_a", "ampr", "AMPRChannel"), ("ampr_b", "ampr", "AMPRChannel"),
    ("amx_a", "amx", "AMXChannel"), ("amx_b", "amx", "AMXChannel"),
    ("amx_hd", "amx_hd", "AMXHDChannel"), ("dmmr", "dmmr", "DMMRChannel"),
    ("esi", "esi", "ESIChannel"),
    *((f"psu_{letter}", "psu", "PSUChannel") for letter in "abcde"),
)


@pytest.mark.parametrize("spec", PLUGINS, ids=lambda spec: spec[0])
@pytest.mark.parametrize("loading,recording", [(False, False), (False, True), (True, False)])
def test_channel_rename_invalidates_legend_not_history(spec, loading, recording, monkeypatch):
    folder, family, class_name = spec
    module = FAMILIES[family][0]()
    if Path(module.__file__).parent.name != folder:
        path = Path(module.__file__).parent.parent / folder / Path(module.__file__).name
        plugin_spec = importlib.util.spec_from_file_location(f"plot_labels_{folder}", path)
        module = importlib.util.module_from_spec(plugin_spec)
        monkeypatch.setitem(sys.modules, plugin_spec.name, module)
        plugin_spec.loader.exec_module(module)

    # Exercise the installed host's redraw policy, not a replacement for it.
    host = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    tree = ast.parse(host.read_text())
    channel_class = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Channel")
    namespace = {"INOUT": SimpleNamespace(IN=1, OUT=2)}
    for name in ("nameChanged", "updateDisplay"):
        method = next(node for node in channel_class.body if isinstance(node, ast.FunctionDef) and node.name == name)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(host), "exec"), namespace)
        monkeypatch.setattr(module.Channel, name, namespace[name], raising=False)

    events = []
    parent = SimpleNamespace(loading=loading, recording=recording,
                             liveDisplay=SimpleNamespace(plot=lambda **kwargs: events.append(("plot", kwargs))))
    manager = SimpleNamespace(ChannelManager=SimpleNamespace,
                              connectAllSources=lambda: events.append("sources"),
                              DeviceManager=SimpleNamespace(updateStaticPlot=lambda: events.append("static")))
    cls = getattr(module, class_name)
    channel = cls.__new__(cls)
    channel.channelParent, channel.pluginManager = parent, manager
    channel.inout, channel.useDisplays = 1, True
    channel.name = "Sample chamber"
    channel.values = values = np.array([1., 2., 3.])
    channel.clearPlotCurve = lambda: events.append("clear")
    if family == "ampr":
        channel._log_channel_event = lambda message: None
    channel.nameChanged()
    assert events.count("sources") == 1
    assert events.count("clear") == events.count("static") == int(not loading)
    plots = [event for event in events if isinstance(event, tuple)]
    assert plots == ([("plot", {"apply": True})] if not loading and not recording else [])
    assert channel.values is values
    np.testing.assert_array_equal(channel.values, [1., 2., 3.])


@pytest.mark.parametrize("label", ["", "   ", "Collecteur", "50% / collecteur", "Entr\u00e9e \u00b5A"])
def test_dmmr_legend_uses_label_without_renaming_recorded_channel(label):
    module = FAMILIES["dmmr"][0]()
    channel = module.DMMRChannel.__new__(module.DMMRChannel)
    channel.name, channel.label = "DMMR_M01", label
    assert channel.legendName == (label.strip() or "DMMR_M01")
    assert channel.name == "DMMR_M01"
    events = []
    channel.updateDisplay = lambda: events.append("display")
    channel.labelChanged()
    assert events == ["display"]
