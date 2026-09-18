"""A real Explorer toolbar action must use the text currently in the editor."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_setpoint_entry_ui import CASES


@pytest.mark.parametrize("folder,surface", CASES)
def test_action_consumes_current_field_without_focus_out(folder, surface, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, surface, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt / Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


LOCAL_CASES = [(folder, surface) for folder, surface in CASES if surface in ('target', 'width', 'voltage', 'current_limit')]


@pytest.mark.parametrize('folder,surface', LOCAL_CASES)
def test_output_button_consumes_current_field_but_off_does_not(folder, surface, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), folder, surface, str(tmp_path), 'local'],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        text=True, capture_output=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip('Real Qt / Explorer sources unavailable')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr


def local_probe(device, window, spin, current_value, calls, folder, surface, output):
    from PyQt6.QtCore import QPoint, Qt
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    commands = []
    if folder.startswith('psu'):
        button_on = button_off = device.manualPanelControls[0]['output_enabled']
        key = 'voltage_values' if surface == 'voltage' else 'current_limit_values'
        device.controller.applyManualStateFromThread = lambda state, **_: commands.append((state['output_enabled'][0], state[key][0]))
    else:
        channel = device.channels[1 if folder == 'esi' else 0]
        channel.enabled = False
        channel.enabledChanged = lambda: commands.append((channel.enabled, channel.value))
        if folder == 'esi':
            button_on, button_off = (device.esiHVCards[1][name] for name in ('btn_on', 'btn_off'))
        else:
            button_on = button_off = device.amxPanelCards[0]['enable']
    for button, checked in ((button_on, False), (button_off, folder == 'esi')):
        blocked = button.blockSignals(True)
        button.setChecked(checked)
        button.blockSignals(blocked)
    spin.setValue(100.)
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, '250')
    app.processEvents()
    calls.clear()
    QTest.mouseClick(button_on, Qt.MouseButton.LeftButton, pos=QPoint(10, button_on.height() // 2))
    app.processEvents()
    assert commands == [(True, 250.)], (commands, spin.text())
    assert calls == [], ('An extra setpoint was sent before enabling', calls)
    assert current_value() == 250.
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, '350')
    app.processEvents()
    commands.clear()
    calls.clear()
    QTest.mouseClick(button_off, Qt.MouseButton.LeftButton, pos=QPoint(10, button_off.height() // 2))
    app.processEvents()
    assert commands == [(False, 250.)], (commands, spin.text())
    assert calls == [], ('An extra setpoint was sent before disabling', calls)
    assert current_value() == 250.
    window.grab().save(str(output / f'{folder}-{surface}-local.png'))
    spin.blockSignals(True)
    window.close()
    return 0


def action_probe(module, device, window, spin, current_value, calls, folder, surface, output, *, local=False):
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QIcon
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication
    from test_on_off_action_ui import explorer_state_action

    if local:
        return local_probe(device, window, spin, current_value, calls, folder, surface, output)
    app = QApplication.instance()
    window.name = device.name
    window.titleBar = device.titleBar
    controller = device.controller
    controller._begin_transition = lambda *_: True
    controller.controllerParent = device
    commands = []
    controller.toggleOnFromThread = lambda **_: commands.append((device.isOn(), current_value()))
    device._update_status_widgets = lambda: None
    device._sync_onoff_actions = lambda: None
    # The action owns no focus, exactly like Explorer's real toolbar buttons.
    StateAction = explorer_state_action()
    def action(checked):
        if folder.startswith("psu") and checked:
            device._apply_manual_panel_state()
        else:
            device.setOn(checked)
    button_action = StateAction(parentPlugin=window, restore=False, iconFalse=QIcon(), iconTrue=QIcon(),
                                toolTipFalse="Turn ON", toolTipTrue="Turn OFF", event=action)
    device.onAction = button_action
    device.isOn = lambda: button_action.state
    button = window.titleBar.widgetForAction(button_action)
    assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    app.processEvents()

    if folder.startswith("psu"):
        device.manualPanelControls[0]["output_enabled"].setChecked(True)
    spin.setValue(100.)
    calls.clear()
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "250")
    app.processEvents()
    assert spin.value() == 100.
    assert "250" in spin.text()
    calls.clear()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    app.processEvents()
    if folder.startswith("psu"):
        assert calls == [250.], (folder, surface, calls)
    else:
        assert commands == [(True, 250.)], (folder, surface, commands, spin.text())
        assert calls == [], "Reading the editor must not send an extra output command"
    assert current_value() == 250.
    # A second click is OFF. Do not submit a higher target before stopping.
    spin.setFocus()
    spin.selectAll()
    QTest.keyClicks(spin, "350")
    app.processEvents()
    commands.clear()
    calls.clear()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    app.processEvents()
    assert commands == [(False, 250.)], (folder, surface, commands)
    assert calls == [], ("OFF submitted a voltage increase", calls)
    assert spin.value() == 250.
    if not folder.startswith("psu"):
        # The next ON may need to reconnect. It still uses the text left in
        # the editor by OFF, without forcing a preliminary value command.
        controller.initialized = False
        device.initializeCommunication = lambda: commands.append(("initialize", current_value()))
        commands.clear()
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        app.processEvents()
        assert commands == [("initialize", 350.)], (folder, surface, commands)
        assert current_value() == 350.
        assert calls == []
    window.grab().save(str(output / f"{folder}-{surface}-action.png"))
    # Do not validate the unfinished field while closing this test fixture.
    spin.blockSignals(True)
    window.close()
    return 0


if __name__ == "__main__":
    from test_setpoint_entry_ui import probe
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3]),
                           actions='local' if len(sys.argv) > 4 else True))
