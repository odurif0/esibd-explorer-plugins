"""Crash resume in the complete ESIBD Explorer application (isolated HOME, test mode, no hardware).

Process 1 turns PSU_A ON and dies without closing (a crash). Process 2 must reconnect PSU_A by
itself and adopt it; its normal close must then remove the record, so a third start stays OFF.
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


def _run(tmp_path, phase):
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1",
               HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home" / ".config"))
    result = subprocess.run([python, str(Path(__file__)), str(tmp_path), phase], env=env, text=True,
                            capture_output=True, timeout=240)
    report_path = tmp_path / f"{phase}.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.is_file() else {}
    return result, report


def test_a_device_left_on_by_a_crash_is_resumed_and_a_normal_close_is_not(tmp_path):
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    if explorer_host.interpreter_version(python) != explorer_host.TARGET_VERSION:
        pytest.skip(f"needs ESIBD Explorer {explorer_host.TARGET_VERSION}")
    result, crash = _run(tmp_path, "crash")
    assert crash.get("on") is True and crash.get("record", {}).get("com"), (crash, result.stdout[-3000:], result.stderr[-3000:])
    result, resume = _run(tmp_path, "resume")
    assert resume.get("code") == 0, (resume, result.stdout[-3000:], result.stderr[-3000:])
    assert resume["on"] is True and resume["resumed_message"], resume
    assert resume["record_while_on"]["token"] != crash["record"]["token"]  # Rewritten by the new process.
    assert resume["record_after_close"] is None  # A normal close is never resumed.
    result, normal = _run(tmp_path, "normal")
    assert normal.get("code") == 0 and normal["on"] is False and not normal["resumed_message"], normal


def main(work: Path, phase: str) -> int:
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from esibd import const
    const.qSet.setValue(f"{const.GENERAL}/{const.TESTMODE}", True)
    plugins = Path(const.defaultPluginPath)
    if not (plugins / "psu_a").exists():
        shutil.copytree(ROOT / "psu_a", plugins / "psu_a", ignore=shutil.ignore_patterns("__pycache__", "logs"))
    Path(const.defaultConfigPath).mkdir(parents=True, exist_ok=True)
    ini = Path(const.defaultConfigPath) / "plugins.ini"
    if not ini.exists():
        ini.write_text("[PSU_A]\nenabled = True\n", encoding="utf-8")

    from PyQt6.QtCore import QTimer
    from esibd.core import Application, EsibdExplorer, SplashScreen

    app = Application(sys.argv[:1])
    app.splashScreen = SplashScreen(app=app)
    window = EsibdExplorer(app=app)
    app.mainWindow = window
    window.show()
    report = {"code": None}
    state = {"phase": "load", "deadline": time.monotonic() + 120, "loaded": None}
    messages = []

    def record_path(device):
        return Path(device.pluginManager.Settings.configPath) / f"{device.name}.session.json"

    def read(path):
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None

    def finish(code, close=True):
        report["code"] = code
        report["resumed_message"] = any("Resumed after an Explorer crash" in m or "Reconnecting to resume" in m
                                        for m in messages)
        (work / f"{phase}.json").write_text(json.dumps(report, default=str), encoding="utf-8")
        if not close:
            os._exit(0)  # A crash: no plugin closing, no record removal.
        try:
            window.closeApplication(confirm=False)
        except Exception:  # noqa: BLE001
            report["close_error"] = traceback.format_exc()
        report["record_after_close"] = read(record_path(state["device"])) if "device" in state else None
        (work / f"{phase}.json").write_text(json.dumps(report, default=str), encoding="utf-8")
        app.exit(code)

    def tick():
        try:
            manager = getattr(window, "pluginManager", None)
            if time.monotonic() > state["deadline"]:
                report["timeout"] = state["phase"]
                return finish(2)
            if state["phase"] == "load":
                if (manager is None or manager.loading or getattr(manager, "finalizing", True)
                        or not getattr(manager, "pluginNames", ())):
                    return
                device = getattr(manager, "PSU_A", None)
                if device is None:
                    report["plugins"] = [p.name for p in manager.plugins]
                    return finish(3)
                state.update(device=device, loaded=time.monotonic())
                original = device.print
                device.print = lambda message, *args, **kwargs: (messages.append(str(message)),
                                                                  original(message, *args, **kwargs))[-1]
                if phase == "crash":
                    device.setOn(True)
                state["phase"] = "observe"
            elif state["phase"] == "observe":
                device = state["device"]
                if time.monotonic() - state["loaded"] < 6:
                    return  # The resume starts 1.5 s after loading; test-mode initialization is immediate.
                report["on"] = bool(device.isOn())
                report["record"] = report["record_while_on"] = read(record_path(device))
                report["messages"] = messages[-10:]
                if phase == "crash":
                    finish(0, close=False)
                else:
                    finish(0)
        except Exception:  # noqa: BLE001
            report["exception"] = traceback.format_exc()
            finish(1)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), sys.argv[2]))
