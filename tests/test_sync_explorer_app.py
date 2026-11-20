"""Sync in the complete ESIBD Explorer application (isolated HOME, offscreen, isolated process).

User request (2026-11-20): copy the conf and data folders of the lab PC to \\\\hive\\esi, one way, every hour and
after each new scan file, with an account of the user's choice. The application starts as a user would: plugin
discovery, the panel, the account dialog (on Linux the account is refused: Windows only), Copy now with a
stand-in for robocopy, the files on the "share", the state file without any password, the log and closing.
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
PASSWORD = "s3cret-Pa55"


def test_sync_copies_conf_and_data_in_the_complete_explorer_application(tmp_path):
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
    assert "Sync" in report["plugins"]
    assert report["defaults"] == {"destination": "\\\\hive\\esi", "automatic": True, "account": "Windows account"}
    assert report["account"].startswith("Not connected: network accounts are handled on Windows only")
    computer = report["computer"]
    copied = set(report["copied"])
    assert {f"{computer}/conf/old.ini", f"{computer}/data/MScan/2026-11-20/scan.h5",
            f"{computer}/data/logs/test/now.jsonl"} <= copied, report  # A log is copied even while written.
    # live.h5 and the files Explorer has just written at its start (Settings.ini…) are left for the next copy.
    assert f"{computer}/data/MScan/2026-11-20/live.h5" not in copied
    assert not {name for name in copied if name.endswith((".ini", ".h5", ".json"))
                and name not in {f"{computer}/conf/old.ini", f"{computer}/data/MScan/2026-11-20/scan.h5"}}, copied
    assert "being written left for the next copy" in report["summary"], report["summary"]
    state = report["state"]
    assert state["destination"] == str(tmp_path / "share") and state["last"] and state["automatic"] is True
    assert report["log"][-1].split()[2] == "OK" and len(report["log"]) == 1, report["log"]
    assert not report["password_found"], report["password_found"]  # Never in a file of Explorer.
    assert report["close_s"] < 10 and "close_error" not in report


def main(work):
    """Run the application; write report.json in ``work``."""
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from esibd import const
    const.qSet.setValue(f"{const.GENERAL}/{const.TESTMODE}", True)
    assert Path(const.defaultPluginPath).is_relative_to(home), const.defaultPluginPath
    shutil.copytree(ROOT / "sync", Path(const.defaultPluginPath) / "sync", ignore=shutil.ignore_patterns("__pycache__"))
    Path(const.defaultConfigPath).mkdir(parents=True, exist_ok=True)
    (Path(const.defaultConfigPath) / "plugins.ini").write_text("[Sync]\nenabled = True\n", encoding="utf-8")

    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QDialog

    from esibd.core import Application, EsibdExplorer, SplashScreen
    from support.sync import fake_robocopy, write

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
                plugin = getattr(manager, "Sync", None)
                if plugin is None:
                    return finish(3)
                module = types.SimpleNamespace(**type(plugin).__init__.__globals__)  # Not in sys.modules (Explorer).
                report["computer"] = module._sync.computer_name()
                report["defaults"] = dict(destination=plugin.destination.text(), automatic=plugin.automatic.isChecked(),
                                          account=plugin.account.text())
                conf, data = Path(manager.Settings.configPath), Path(manager.Settings.dataPath)
                write(conf / "old.ini")
                write(data / "MScan" / "2026-11-20" / "scan.h5")
                write(data / "MScan" / "2026-11-20" / "live.h5", age_s=5)  # Being written: left for later.
                write(data / "logs" / "test" / "now.jsonl", age_s=1)  # A log: copied even while written.
                plugin.runner = fake_robocopy
                plugin.destination.setText(str(work / "share"))
                plugin.destination.editingFinished.emit()

                def fill(dialog):
                    dialog.account.setText("LAB\\me")
                    dialog.password.setText(PASSWORD)
                    return QDialog.DialogCode.Accepted
                module._AccountDialog.exec = fill
                plugin.connect_account()  # Refused on Linux: no copy starts from it.
                report["account"] = plugin.state()["summary"]
                plugin.copy_button.click()
                state.update(phase="copying", plugin=plugin, started=time.monotonic())
            elif state["phase"] == "copying":
                plugin = state["plugin"]
                summary = plugin.state()["summary"]
                if plugin._copying or "copied" not in summary:
                    return
                share = work / "share"
                report["summary"] = summary
                report["copied"] = sorted(p.relative_to(share).as_posix() for p in share.rglob("*") if p.is_file())
                report["state"] = json.loads((Path(manager.Settings.configPath) / "Sync.json").read_text(encoding="utf-8"))
                log = Path(manager.Settings.dataPath) / "logs" / "sync" / "sync.log"
                report["log"] = log.read_text(encoding="utf-8").splitlines()
                report["password_found"] = [str(p) for p in home.rglob("*") if p.is_file() and p.stat().st_size < 5e7
                                            and PASSWORD.encode() in p.read_bytes()]
                plugin.raiseDock(True)
                app.processEvents()
                window.grab().save(str(work / "explorer.png"))
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
