"""ON/OFF synchronization with Explorer's real StateAction and Qt tool buttons."""
from __future__ import annotations

import ast
from pathlib import Path
import sys
from types import MethodType

import pytest

from conftest import PLUGIN_SPECS
from support.qt import run_probe
from support.ui import explorer_state_action

ROOT = Path(__file__).resolve().parents[1]
# A scan has recording controls, not a device ON/OFF action.
ENTRYPOINTS = [ROOT / spec.folder / spec.entrypoint for spec in PLUGIN_SPECS if spec.runtime_family]


@pytest.mark.parametrize("entrypoint", ENTRYPOINTS, ids=lambda path: path.parent.name)
def test_on_off_state_icon_tooltip_and_next_click(entrypoint, tmp_path):
    run_probe(__file__, entrypoint, tmp_path, timeout=30)


def probe(path, output):
    from importlib.metadata import PackageNotFoundError
    try:
        from PyQt6.QtCore import Qt
        from PyQt6.QtGui import QIcon
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QToolBar, QVBoxLayout, QWidget
        StateAction = explorer_state_action()
    except (ImportError, PackageNotFoundError):
        return 77

    app = QApplication([])
    tree = ast.parse(path.read_text(encoding="utf-8"))
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "_sync_local_on_action")
    synced = []  # The crash-resume record follows every ON-state sync; it only writes a file.
    ns = {"_session_sync": lambda device: synced.append(bool(device.isOn()))}
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), "exec"), ns)
    parent = QWidget()
    parent.name = path.parent.name.upper()
    layout = QVBoxLayout(parent)
    parent.titleBar = QToolBar(parent)
    layout.addWidget(parent.titleBar)
    feedback = QLabel()
    layout.addWidget(feedback)
    parent.calls = []
    icons = [QIcon(str(path.parent / f"switch-medium_{state}.png")) for state in ("on", "off")]
    assert all(not icon.isNull() for icon in icons)
    kwargs = dict(parentPlugin=parent, iconFalse=icons[0], iconTrue=icons[1], restore=False)
    parent.onAction = StateAction(**kwargs, toolTipFalse="Global ON", toolTipTrue="Global OFF")
    parent.deviceOnAction = StateAction(**kwargs, toolTipFalse=f"Turn {parent.name} ON.",
                                       toolTipTrue=f"Turn {parent.name} OFF and disconnect.",
                                       event=parent.calls.append)
    parent.isOn = lambda: parent.onAction.state
    sync = MethodType(ns["_sync_local_on_action"], parent)
    button = parent.titleBar.widgetForAction(parent.deviceOnAction)
    parent.resize(400, 110)
    parent.show()

    def check(state):
        app.processEvents()
        action = parent.deviceOnAction
        assert action.state == state
        assert button.isChecked() == state
        expected_icon = icons[int(state)]
        assert action.icon().cacheKey() == expected_icon.cacheKey()
        assert button.icon().cacheKey() == expected_icon.cacheKey()
        expected_tooltip = action.toolTipTrue if state else action.toolTipFalse
        assert action.toolTip() == button.toolTip() == expected_tooltip
        feedback.setText(expected_tooltip)

    # Exercise global ON, OFF rollback after failure, unchanged-state polls and
    # both directions of the next real mouse click, not just QAction.trigger().
    for state in (True, False, True, False):
        commands_before = len(parent.calls)
        parent.onAction.state = state
        for _ in range(3):
            sync()
        check(state)
        assert len(parent.calls) == commands_before, "Synchronization issued a command"
        assert synced[-3:] == [state] * 3, synced
        parent.grab().save(str(output / f"{path.parent.name}-{'on' if state else 'off'}.png"))
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert parent.calls[-1] == (not state), "Next click must match the displayed action"
        assert len(parent.calls) == commands_before + 1
        check(not state)
    parent.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1]), Path(sys.argv[2])))
