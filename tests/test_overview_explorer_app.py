"""Overview in the complete ESIBD Explorer application (isolated HOME, offscreen, isolated process).

User request (2026-11-22): one page with the essential parameters and a general ON/OFF. The application starts as a
user would, with two stand-in device plugins named AMPR_A and TPG366 (Explorer's own example device, test mode:
fake readings): the cards, the device lights, All ON (confirmed) pressing each ON button in beam order, a card
showing the value read back, All OFF, the log, and closing.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback
import types

import pytest

import explorer_host
from support.qt import EXPLORER_QT_ENV, fatal_stack

ROOT = Path(__file__).resolve().parents[1]
STAND_INS = '''
from esibd.examples.custom_device.custom_device import CustomDevice
from esibd.plugins import Device


def providePlugins():
    return [AMPR, Gauges]


class AMPR(CustomDevice):
    name = "AMPR_A"

    def setOn(self, on=None):
        Device.setOn(self, on)  # Without the example's message box.


class Gauges(CustomDevice):
    name = "TPG366"

    def setOn(self, on=None):
        Device.setOn(self, on)
'''


def test_overview_turns_the_devices_on_and_off_in_the_complete_explorer_application(tmp_path):
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    version = explorer_host.interpreter_version(python)
    if not explorer_host.is_target(version):
        pytest.skip(f"the complete-application test needs ESIBD Explorer {explorer_host.TARGET_VERSION}; found {version}")
    env = dict(os.environ, **EXPLORER_QT_ENV, PYTHONUNBUFFERED="1", PYTHONFAULTHANDLER="1",
               HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home" / ".config"))
    result = subprocess.run([python, str(Path(__file__)), str(tmp_path)], env=env, text=True, capture_output=True,
                            timeout=300)
    output = result.stdout[-6000:] + result.stderr[-6000:] + fatal_stack(result.stderr)
    assert (tmp_path / "report.json").is_file(), output  # A crash leaves no report.
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert result.returncode == 0 and report["code"] == 0, (report, output)
    assert "Traceback" not in result.stdout + result.stderr, output
    assert {"Overview", "AMPR_A", "TPG366"} <= set(report["plugins"])
    assert report["titles"] == ["Source", "Funnel 1", "Funnel 2", "Q1", "Q2", "Q3", "Q4", "Detector"]
    assert report["lights_before"] == {"TPG366": ["off", "OFF"], "AMPR_A": ["off", "OFF"]}
    assert report["entry_before"] == "—"
    assert report["confirmed"] == ["TPG366", "AMPR_A"]
    on = report["on"]
    assert on["ok"] and on["summary"] == "All ON finished. turned ON: TPG366, AMPR_A.", on
    assert on["buttons"] == {"TPG366": True, "AMPR_A": True} and on["lights"] == {"TPG366": ["on", "ON"], "AMPR_A": ["on", "ON"]}
    assert on["entry"] not in ("", "—")  # Read back by the device once ON.
    off = report["off"]
    assert off["ok"] and off["summary"] == "All OFF finished. turned OFF: AMPR_A, TPG366.", off
    assert off["buttons"] == {"TPG366": False, "AMPR_A": False} and off["lights"] == {"TPG366": ["off", "OFF"], "AMPR_A": ["off", "OFF"]}
    log = report["log"]
    assert [line.split(" ", 2)[2] for line in log if "pressed" in line] == [
        "All ON: TPG366 ON pressed.", "All ON: AMPR_A ON pressed.", "All OFF: AMPR_A OFF pressed.", "All OFF: TPG366 OFF pressed."]
    assert any(line.endswith("AMPR_A: OFF → Connecting") or line.endswith("AMPR_A: OFF → ON") for line in log), log
    assert report["close_s"] < 10 and "close_error" not in report


def main(work):
    """Run the application; write report.json in ``work``."""
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from esibd import const
    const.qSet.setValue(f"{const.GENERAL}/{const.TESTMODE}", True)
    plugins = Path(const.defaultPluginPath)
    assert plugins.is_relative_to(home), plugins
    shutil.copytree(ROOT / "overview", plugins / "overview", ignore=shutil.ignore_patterns("__pycache__"))
    stand_ins = plugins / "stand_ins"
    stand_ins.mkdir(parents=True)
    (stand_ins / "stand_ins.py").write_text(STAND_INS, encoding="utf-8")
    import esibd.examples.custom_device as example
    shutil.copy(Path(example.__file__).parent / "cookie.png", stand_ins / "cookie.png")
    Path(const.defaultConfigPath).mkdir(parents=True, exist_ok=True)
    (Path(const.defaultConfigPath) / "plugins.ini").write_text(
        "[Overview]\nenabled = True\n[AMPR_A]\nenabled = True\n[TPG366]\nenabled = True\n", encoding="utf-8")

    from PyQt6.QtCore import QTimer

    from esibd.core import Application, EsibdExplorer, SplashScreen

    app = Application(sys.argv[:1])
    app.setStyle("Fusion")
    app.splashScreen = SplashScreen(app=app)
    window = EsibdExplorer(app=app)
    app.mainWindow = window
    window.resize(1400, 900)
    window.show()
    report = {"code": None}
    state = {"phase": "load", "deadline": time.monotonic() + 240}

    def finish(code):
        report["code"] = code
        started = time.monotonic()
        try:
            window.closeApplication(confirm=False)
        except Exception:  # noqa: BLE001 - reported
            report["close_error"] = traceback.format_exc()
        report["close_s"] = time.monotonic() - started
        (work / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        app.exit(code)

    def snapshot(plugin, devices):
        plugin.refresh()
        module = state["module"]
        return dict(buttons={d.name: bool(d.onAction.state) for d in devices},
                    lights={d.name: list(module.light(d)) for d in devices},
                    entry=plugin.cards.cards[1].rows[1][1].text(),
                    ok=plugin._sequence.ok, summary=plugin._sequence.summary)

    def tick():
        try:
            if time.monotonic() > state["deadline"]:
                report["timeout"] = state["phase"]
                return finish(2)
            manager = getattr(window, "pluginManager", None)
            if state["phase"] == "load":
                if (manager is None or manager.loading or getattr(manager, "finalizing", True)
                        or not getattr(manager, "pluginNames", ())):
                    return
                report["plugins"] = [p.name for p in manager.plugins]
                plugin = getattr(manager, "Overview", None)
                if plugin is None:
                    return finish(3)
                module = types.SimpleNamespace(**type(plugin).__init__.__globals__)._overview  # Not in sys.modules.
                ampr, gauges = manager.AMPR_A, manager.TPG366
                ampr.getChannels()[0].name = "Entry_IF1"  # The lab's name for the funnel entry.
                state.update(plugin=plugin, module=module, devices=[gauges, ampr])
                plugin.refresh()
                report["titles"] = [card.base_title for card in plugin.cards.cards]
                report["lights_before"] = {d.name: list(module.light(d)) for d in (gauges, ampr)}
                report["entry_before"] = plugin.cards.cards[1].rows[1][1].text()

                def confirm(devices):
                    report["confirmed"] = [d.name for d in devices]
                    return True
                plugin._confirm_on = confirm
                plugin.all_on_button.click()
                state["phase"] = "on"
            elif state["phase"] in ("on", "off"):
                plugin = state["plugin"]
                if plugin.running():
                    return
                devices = state["devices"]
                if state["phase"] == "on":
                    if plugin.cards.cards[1].rows[1][1].text() == "—" and time.monotonic() < state["deadline"] - 200:
                        plugin.refresh()
                        return  # The first fake readings.
                    report["on"] = snapshot(plugin, devices)
                    plugin.raiseDock(True)
                    app.processEvents()
                    window.grab().save(str(work / "explorer_on.png"))
                    plugin.all_off_button.click()
                    state["phase"] = "off"
                    return
                report["off"] = snapshot(plugin, devices)
                log = Path(manager.Settings.dataPath) / "logs" / "overview" / "overview.log"
                report["log"] = log.read_text(encoding="utf-8").splitlines()
                window.grab().save(str(work / "explorer_off.png"))
                state["phase"] = "done"
                QTimer.singleShot(300, lambda: finish(0))
        except Exception:  # noqa: BLE001 - reported
            report["exception"] = traceback.format_exc()
            finish(1)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
