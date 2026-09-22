"""Load real DMMR channels before Explorer creates its ON/OFF action.

Regression for the 2026-09-22 hardware log: ChannelManager.initGUI loads the
configuration, whereas onAction is only created later by finalizeInit.
"""
from __future__ import annotations

import ast
import configparser
from importlib.metadata import distribution
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scale", ["1", "1.5"])
@pytest.mark.parametrize("legacy", [True, False])
def test_existing_configuration_loads_before_on_action(legacy, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, __file__, str(int(legacy)), str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True, text=True, timeout=45,
    )
    if result.returncode == 77:
        pytest.skip("Qt or Explorer unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(legacy, output):
    try:
        from PyQt6.QtWidgets import QApplication, QTreeWidget, QVBoxLayout, QWidget
        from PyQt6.QtGui import QIcon
    except ImportError:
        return 77
    from test_psu_channel_integration_ui import real_framework
    from test_on_off_action_ui import explorer_state_action

    app = QApplication([])
    ns = real_framework()  # Actual Channel, Parameter and their Qt widgets.
    core = sys.modules["esibd.core"]
    core.LabviewDoubleSpinBox = ns["LabviewDoubleSpinBox"]
    core.DynamicNp = ns["DynamicNp"]
    core.getTestMode = lambda: False
    sys.modules["esibd.plugins"].LiveDisplay = type("LiveDisplay", (), {})
    host = Path(distribution("esibd-explorer").locate_file("esibd/plugins.py"))
    tree = ast.parse(host.read_text(encoding="utf-8"))
    manager = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "ChannelManager")
    # Crucially, do not replace isOn with a lambda returning a preselected state.
    # Use the same getter and configuration-to-Channel path as the installed host.
    for name in ("isOn", "addChannel", "updateChannelConfig"):
        method = next(n for n in manager.body if isinstance(n, ast.FunctionDef) and n.name == name)
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(host), "exec"), ns)
        setattr(ns["Device"], name, ns[name])
    ns["Device"].toggleAdvanced = lambda self, advanced=False: None
    ns["Device"].estimateStorage = lambda self: None

    spec = importlib.util.spec_from_file_location("dmmr_startup_probe", ROOT / "dmmr/dmmr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parent = module.DMMRDevice.__new__(module.DMMRDevice)
    parent.channels = []
    parent.channelType = module.DMMRChannel
    parent.loading = True
    parent.updating = False
    parent.interval = 1000
    parent.inout = ns["INOUT"].IN
    parent.useDisplays, parent.useBackgrounds, parent.logY = True, False, False
    parent.convertDataDisplay = parent.liveDisplay = None
    parent.controller = None
    parent.recording, parent.maxDataPoints = False, 100000
    parent.MAXDATAPOINTS = "Max data points"
    parent.main_state = "Disconnected"
    parent.confINI, parent.UTF8 = "DMMR.ini", "utf-8"
    parent.customConfigFile = lambda name: output / name
    parent.makeCoreIcon = lambda *args: QIcon()
    parent.processEvents = app.processEvents
    logs = []
    parent.print = lambda message, **kwargs: logs.append(message)
    parent.advancedAction = SimpleNamespace(state=False)
    parent.pluginManager = SimpleNamespace(
        Device=ns["Device"], ChannelManager=ns["Device"], loading=True, closing=False,
        Settings=SimpleNamespace(settings={"DMMR/Max data points": SimpleNamespace(getWidget=lambda: None)}),
        DeviceManager=SimpleNamespace(globalUpdate=lambda **kwargs: None),
        reconnectSource=lambda *args: None,
    )
    window = QWidget()
    layout = QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget

    class Tree(QTreeWidget):
        pass

    parent.tree = Tree()
    layout.addWidget(parent.tree)
    parent.tree.setColumnCount(len(parent._default_channel_template()))
    configuration = configparser.ConfigParser()
    for address in range(8):
        item = {"Name": f"DMMR_M{address:02}", "Module": str(address), "Label": f"Collecteur {address}"}
        if not legacy:
            item["Range mode"] = "Auto" if address == 0 else str(address % 5)
        configuration[f"Channel_{address:03}"] = item
    file = output / "DMMR.ini"
    with file.open("w", encoding="utf-8") as target:
        configuration.write(target)
    before = file.read_bytes()
    assert not hasattr(parent, "onAction")
    parent.loadConfiguration(useDefaultFile=True)
    assert len(parent.channels) == len(parent.channelPanelCards) == 8
    assert [ch.module_address() for ch in parent.channels] == list(range(8))
    expected = ["Auto" if legacy or i == 0 else str(i % 5) for i in range(8)]
    assert [ch.range_mode for ch in parent.channels] == expected
    assert [ch._requested_range_mode for ch in parent.channels] == expected
    assert all(not card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
    assert file.read_bytes() == before, "Loading must not overwrite the existing configuration"

    # Mirror finalizeInit creating the real host action, then refresh the cards.
    from PyQt6.QtWidgets import QToolBar
    window.name = "DeviceManager"
    window.titleBar = QToolBar(window)
    layout.addWidget(window.titleBar)
    StateAction = explorer_state_action()
    parent.onAction = StateAction(parentPlugin=window, iconFalse=QIcon(), iconTrue=QIcon(),
                                 toolTipFalse="ON", toolTipTrue="OFF", restore=False)
    parent.controller = SimpleNamespace(initializing=False, transitioning=False, values={})
    parent.loading = parent.pluginManager.loading = False
    parent._update_channel_panel()
    assert all(card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
    for flag in ("initializing", "transitioning"):
        setattr(parent.controller, flag, True)
        parent._update_channel_panel()
        assert all(not card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
        setattr(parent.controller, flag, False)
    parent.onAction.state = True
    parent._update_channel_panel()
    assert all(not card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
    parent.onAction.state = False
    parent.main_state = "Shutdown unconfirmed"
    parent._update_channel_panel()
    assert all(not card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
    parent.main_state = "Disconnected"
    parent._update_channel_panel()
    assert all(card["range_combo"].isEnabled() for card in parent.channelPanelCards.values())
    assert [card["range_combo"].currentData() for card in parent.channelPanelCards.values()] == expected
    window.resize(950, 520)
    window.show()
    app.processEvents()
    window.grab().save(str(output / "dmmr-startup.png"))
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(bool(int(sys.argv[1])), Path(sys.argv[2])))
