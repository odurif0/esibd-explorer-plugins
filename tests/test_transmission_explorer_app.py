"""Transmission in the complete ESIBD Explorer application (isolated HOME, offscreen, isolated process).

The other Transmission UI tests build the plugin with Explorer's real Scan classes in
a test harness. This one starts the whole application as a user would: plugin
discovery from the plugin path, docks, Settings with a real Data path, a simulated run
from the panel, the saved HDF5 file and logs, Keep, and closing the application.
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

ROOT = Path(__file__).resolve().parents[1]


def test_transmission_runs_in_the_complete_explorer_application(tmp_path):
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    version = explorer_host.interpreter_version(python)
    if version != explorer_host.TARGET_VERSION:
        pytest.skip(f"the complete-application test needs ESIBD Explorer {explorer_host.TARGET_VERSION}; found {version}")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1", PYTHONFAULTHANDLER="1",
               HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home" / ".config"))
    result = subprocess.run([python, str(Path(__file__)), str(tmp_path)], env=env, text=True, capture_output=True,
                            timeout=300)
    output = result.stdout[-6000:] + result.stderr[-6000:]
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert result.returncode == 0 and report["code"] == 0, (report, output)
    assert "Traceback" not in result.stdout + result.stderr, output
    assert "Transmission" in report["plugins"]
    assert report["ready"][0] and report["ready"][2].startswith("Simulation: built-in beamline")
    run = report["result"]
    assert run["status"] == "completed" and run["file_exists"] and run["keep_visible"] and run["display"], run
    assert run["line"].startswith("Collector: ") and run["records"] > 10
    data = Path(report["data_path"])
    assert data.is_relative_to(tmp_path) and Path(run["file"]).is_relative_to(data)
    assert len([p for p in report["logs"] if p.startswith("runs/") and p.endswith("_simulation.jsonl")]) == 1
    assert report["session_events"] == ["plugin_start", "run_start", "run_end", "keep"]
    assert report["h5"]["log_file"].startswith(str(data / "logs" / "transmission" / "runs"))
    assert report["h5"]["notes"] == "complete-application test" and "close_error" not in report


def main(work):
    """Run the application; write report.json in ``work``."""
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from esibd import const
    const.qSet.setValue(f"{const.GENERAL}/{const.TESTMODE}", True)
    assert Path(const.defaultPluginPath).is_relative_to(home), const.defaultPluginPath
    shutil.copytree(ROOT / "transmission", Path(const.defaultPluginPath) / "transmission",
                    ignore=shutil.ignore_patterns("__pycache__"))
    Path(const.defaultConfigPath).mkdir(parents=True, exist_ok=True)
    (Path(const.defaultConfigPath) / "plugins.ini").write_text("[Transmission]\nenabled = True\n", encoding="utf-8")

    from PyQt6.QtCore import QTimer
    from esibd.core import Application, EsibdExplorer, SplashScreen

    app = Application(sys.argv[:1])
    app.setStyle("Fusion")
    app.splashScreen = SplashScreen(app=app)
    window = EsibdExplorer(app=app)
    app.mainWindow = window
    window.resize(1600, 1000)
    window.show()
    report = {"code": None}
    state = {"phase": "load", "deadline": time.monotonic() + 240}

    def finish(code):
        report["code"] = code
        try:
            window.closeApplication(confirm=False)
        except Exception:  # noqa: BLE001 - reported
            report["close_error"] = traceback.format_exc()
        (work / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        app.exit(code)

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
                plugin = getattr(manager, "Transmission", None)
                if plugin is None:
                    return finish(3)
                report["data_path"] = str(manager.Settings.dataPath)
                panel = plugin.panel
                panel.simulation.setChecked(True)
                for key, box in panel.stage_boxes.items():
                    box.setChecked(key == "funnel")
                plugin.notes = "complete-application test"
                report["ready"] = [panel.optimize.isEnabled(), panel.progress.text(), panel.beamline_label.text()]
                panel.optimize.click()
                state.update(phase="running", plugin=plugin, started=time.monotonic())
            elif state["phase"] == "running":
                plugin = state["plugin"]
                if not plugin.finished or plugin.recording or time.monotonic() - state["started"] < 1:
                    return
                panel = plugin.panel
                report["result"] = dict(status=plugin._result.get("status"), reason=plugin._result.get("reason"),
                                        line=panel.result.text(), keep_visible=panel.keep.isVisible(),
                                        file=str(plugin.file), file_exists=plugin.file.exists(),
                                        records=len(plugin._records), display=bool(plugin.displayActive()))
                plugin.raiseDock(True)
                app.processEvents()
                window.grab().save(str(work / "explorer.png"))
                panel.keep.click()
                logs = Path(manager.Settings.dataPath) / "logs" / "transmission"
                report["logs"] = sorted(p.relative_to(logs).as_posix() for p in logs.rglob("*") if p.is_file())
                with (logs / "transmission_session.jsonl").open(encoding="utf-8") as handle:
                    report["session_events"] = [json.loads(line)["event"] for line in handle]
                import h5py
                with h5py.File(plugin.file, "r") as handle:
                    group = handle["Transmission"]
                    report["h5"] = dict(log_file=group.attrs.get("log_file"), notes=group.attrs.get("notes"))
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
