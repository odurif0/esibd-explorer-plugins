"""Optional PSU associations must not become imports, commands or invented volts."""
import ast
from enum import Enum
from importlib.metadata import distribution
from pathlib import Path
import re
from types import SimpleNamespace

import numpy as np
import pytest

from test_amx_config_load import make_case
from test_amx_outputs import config79_snapshot


class Item:
    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tooltip = value


@pytest.fixture(params=("amx_a", "amx_b"))
def rig(request):
    module, parent, controller = make_case(request.param)
    items = [[Item() for _ in range(7)] for _ in range(4)]
    parent.amxOutputTable = SimpleNamespace(item=lambda row, col: items[row][col])
    controller._apply_snapshot(config79_snapshot())
    parent.psu_ch01, parent.psu_ch23 = "PSU_A", "None"
    register_sources(parent)
    return module, parent, controller, items


def register_sources(parent, *plugins):
    parent.pluginManager = SimpleNamespace(plugins=list(plugins))
    def channels():
        return [c for p in parent.pluginManager.plugins
                for c in getattr(p, "getChannels", lambda: [])() if hasattr(c, "name")]
    def lookup(name):
        return next((c for c in channels() if c.name.lower() == name.lower()), None)
    parent.pluginManager.DeviceManager = SimpleNamespace(channels=channels, getChannelByName=lookup)


def provider(name="PSU_A", positive=101., negative=102.):
    channels = [SimpleNamespace(
        id=ch, name=f"{name}_CH{ch}", monitor=value, value=9999., initialized=True,
        unit="V", useMonitors=True, real=True, enabled=True,
        readback_status="Measured PSU voltage.",
    ) for ch, value in enumerate((positive, negative))]
    return SimpleNamespace(name=name, getChannels=lambda: channels)


@pytest.mark.parametrize("case", ("none", "absent", "old", "duplicate", "error", "malformed"))
def test_unavailable_provider_keeps_symbolic_rails(rig, case):
    _, parent, _, items = rig
    psu = provider()
    if case == "none": parent.psu_ch01 = "None"
    elif case == "old": parent.pluginManager.plugins = [SimpleNamespace(name="PSU_A")]
    elif case == "duplicate": parent.pluginManager.plugins = [psu, psu]
    elif case == "error":
        def fail():
            raise RuntimeError("Unreadable PSU")
        psu.getChannels = fail
        parent.pluginManager.plugins = [psu]
    elif case == "malformed":
        psu.getChannels = lambda: None
        parent.pluginManager.plugins = [psu]
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ Vpos"
    assert "0 V" not in items[0][3].text
    assert items[0][6].text == ("—" if case == "none" else "PSU_A")


@pytest.mark.parametrize("invalid", (None, float("nan"), float("inf"), -3.))
def test_bad_or_wrong_sign_value_does_not_gain_a_voltage(rig, invalid):
    _, parent, _, items = rig
    parent.pluginManager.plugins = [provider(positive=invalid)]
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"
    parent.pluginManager.plugins = [provider(negative=invalid)]
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ +101 V"


@pytest.mark.parametrize("psu_folder", [f"psu_{suffix}" for suffix in "abcde"])
def test_each_real_psu_publishes_channels_for_either_pair(rig, psu_folder):
    from test_psu_channels import make_psu, snapshot
    _, parent, controller, items = rig
    _, psu, psu_controller = make_psu(psu_folder)
    psu_controller._apply_snapshot(snapshot(101., 102.))
    parent.psu_ch01 = parent.psu_ch23 = psu.name
    parent.pluginManager.plugins = [parent, psu]
    before = [dict(row) for row in controller.output_rows]
    parent._update_output_table()
    assert all(row[3].text == "-102 V ↔ +101 V" for row in items)
    assert all(row[6].text == psu.name for row in items)
    assert controller.output_rows == before
    assert controller.device.timing_writes == []


def test_static_hiz_and_unknown_routing_are_not_replaced_by_both_rails(rig):
    _, parent, controller, items = rig
    parent.pluginManager.plugins = [provider()]
    parent.psu_ch23 = "PSU_A"
    state = config79_snapshot()
    state["switches"][0]["trigger_config"] = 32  # constant 1 -> Vpos
    state["switches"][1]["trigger_config"] = 0   # constant 0 -> Vneg
    state["switches"][2]["enable_config"] = 0    # Hi-Z
    state["switches"][3]["trigger_config"] = 3   # external
    controller._apply_snapshot(state)
    parent._update_output_table()
    assert [row[3].text for row in items] == ["+101 V", "-102 V", "Hi-Z", "Unknown"]
    parent.main_state = "Shutdown unconfirmed"
    parent._update_output_table()
    assert all(row[3].text == "Unknown" for row in items)


def test_renaming_reordering_and_ambiguous_registry(rig):
    _, parent, _, items = rig
    psu = provider()
    register_sources(parent, psu)
    sources = psu.getChannels()
    sources[0].name, sources[1].name = "My positive supply", "My negative supply"
    sources.reverse()
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ +101 V"
    duplicate = SimpleNamespace(getChannels=lambda: [sources[0]])
    parent.pluginManager.plugins.append(duplicate)
    parent._update_output_table()
    assert items[0][3].text == "Vneg ↔ +101 V"


@pytest.mark.parametrize("attribute,value", (("real", False), ("enabled", False), ("initialized", False),
                                           ("useMonitors", False), ("unit", "A")))
def test_ineligible_source_is_not_used(rig, attribute, value):
    _, parent, _, items = rig
    psu = provider()
    setattr(psu.getChannels()[0], attribute, value)
    register_sources(parent, psu)
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"


def test_old_psu_monitor_without_validity_contract_is_rejected(rig):
    _, parent, _, items = rig
    psu = provider()
    del psu.getChannels()[0].readback_status
    register_sources(parent, psu)
    parent._update_output_table()
    assert items[0][3].text == "-102 V ↔ Vpos"
    assert "update the PSU" in items[0][3].tooltip


def test_settings_are_persistent_independent_and_do_not_use_slashes(rig, monkeypatch):
    module, parent, _, _ = rig
    parent.MAXDATAPOINTS = "Max data points"
    monkeypatch.setattr(module.Device, "getDefaultSettings", lambda self: {
        f"{self.name}/Interval": {module.Parameter.VALUE: 1000},
        f"{self.name}/Max data points": {module.Parameter.VALUE: 100000},
    }, raising=False)
    definitions = parent.getDefaultSettings()
    for key, attr in ((parent.PSU_CH01, "psu_ch01"), (parent.PSU_CH23, "psu_ch23")):
        assert "/" not in key  # Explorer splits at the last slash.
        definition = definitions[f"{parent.name}/{key}"]
        assert definition["value"] == "None"
        assert definition["attr"] == attr
        assert definition["items"].split(",") == ["None", "PSU_A", "PSU_B", "PSU_C", "PSU_D", "PSU_E"]
        assert definition["fixedItems"] is True
        assert definition.get("restore", True) is True
        assert definition["event"] == parent._update_output_table


def install_real_settings(module, parent, layout):
    """Exercise Explorer Parameter and makeSettingWrapper on real Qt combos."""
    from PyQt6 import QtCore, QtGui, QtWidgets
    import sys
    core = sys.modules["esibd.core"]
    root = Path(distribution("esibd-explorer").locate_file("esibd"))
    ns = dict(np=np, re=re, Path=Path, cast=lambda _, value: value,
              Channel=core.Channel, Setting=type("Setting", (), {}),
              Qt=QtCore.Qt, pyqtSignal=QtCore.pyqtSignal,
              CompactComboBox=QtWidgets.QComboBox, LedIndicator=QtWidgets.QLabel,
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    source = ast.parse((root / "core.py").read_text())
    for name in ("ParameterWidget", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "CheckBox", "Parameter"):
        node = next(node for node in source.body if isinstance(node, ast.ClassDef) and node.name == name)
        exec(compile("from __future__ import annotations\n" + ast.unparse(node), str(root / "core.py"), "exec"), ns)
    source = ast.parse((root / "const.py").read_text())
    wrapper = next(node for node in source.body if isinstance(node, ast.FunctionDef) and node.name == "makeSettingWrapper")
    exec(compile("from __future__ import annotations\n" + ast.unparse(wrapper), str(root / "const.py"), "exec"), ns)
    module.Device.getDefaultSettings = lambda self: {
        f"{self.name}/Interval": {module.Parameter.VALUE: 1000},
        f"{self.name}/Max data points": {module.Parameter.VALUE: 100000},
    }
    parent.MAXDATAPOINTS = "Max data points"
    definitions = parent.getDefaultSettings()
    owner = SimpleNamespace(loading=True, print=parent.print, settings={})
    for key, attr in ((parent.PSU_CH01, "psu_ch01"), (parent.PSU_CH23, "psu_ch23")):
        full_name = f"{parent.name}/{key}"
        definition = definitions[full_name]
        parameter = ns["Parameter"](full_name, owner,
            parameterType=ns["PARAMETERTYPE"].COMBO,
            items=definition["items"], fixedItems=definition["fixedItems"],
            event=definition["event"], toolTip=definition["toolTip"])
        owner.settings[full_name] = parameter
        setattr(type(parent), attr, ns["makeSettingWrapper"](full_name, owner))
        parameter.value = definition["value"]
        layout.addWidget(QtWidgets.QLabel(key))
        layout.addWidget(parameter.combo)
    owner.loading = False
    return owner
