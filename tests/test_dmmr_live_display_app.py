"""DMMR live display in the complete ESIBD Explorer (isolated HOME, test mode, no hardware).

User report (2026-10-08): the y axis must read in pA with its unit, and changing a curve colour
while acquisition is stopped made the DMMR curves disappear. Explorer redraws over a window ending
*now*; once the acquisition stopped longer ago than the display time, the redraw found no data.
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


def test_dmmr_axis_reads_pa_and_a_colour_change_keeps_stopped_curves(tmp_path):
    python = os.environ.get("ESIBD_QT_PYTHON", sys.executable)
    if explorer_host.interpreter_version(python) != explorer_host.TARGET_VERSION:
        pytest.skip(f"needs ESIBD Explorer {explorer_host.TARGET_VERSION}")
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1",
               HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "home" / ".config"))
    result = subprocess.run([python, str(Path(__file__)), str(tmp_path)], env=env, text=True,
                            capture_output=True, timeout=240)
    assert (tmp_path / "report.json").is_file(), (result.returncode, result.stdout[-3000:], result.stderr[-3000:])
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert report.get("code") == 0, (report, result.stdout[-3000:], result.stderr[-3000:])
    assert report["before"] == report["after"] == {"DMMR_M01": True, "DMMR_M02": True}
    assert report["color"] == "#ff0000" and report["recording"] is False
    # The window ends at the last sample of the stopped acquisition (19 min ago), not now.
    assert -1300 < report["xrange"][0] < report["xrange"][1] < -1100, report["xrange"]
    assert (report["label"], report["units"]) == ("Current", "pA")
    assert report["linear"] == {"left": ["10", "20"], "right": ["10", "20"]}
    assert report["log"] == {"left": ["1·10¹", "1·10²"], "right": ["1·10¹", "1·10²"]}
    assert report["legend"] == ["DMMR_M01 (pA)", "DMMR_M02 (pA)"]
    # Only the display changes: data, Explorer's display conversion and scans stay in amperes.
    assert 8e-12 < report["data"] < 1.2e-11 and report["converted"] == 1e-11
    assert report["recording_window"] is None  # While recording, Explorer's own window applies.
    assert report["label_legend"] == ["Collecteur (pA)", "DMMR_M02 (pA)"]
    assert report["label_legend_text"] == report["label_legend"]
    assert report["failed_label_legend"] == report["label_legend"]
    assert report["recording_label_legend"] == ["Entr\u00e9e \u00b5A 50% (pA)", "DMMR_M02 (pA)"]
    assert report["empty_label_legend"] == ["DMMR_M01 (pA)", "DMMR_M02 (pA)"]
    assert report["label_channel_names"] == ["DMMR_M01", "DMMR_M02"]


def main(work: Path) -> int:
    home = Path(os.environ["HOME"])
    home.mkdir(parents=True, exist_ok=True)
    sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
    from esibd import const
    const.qSet.setValue(f"{const.GENERAL}/{const.TESTMODE}", True)
    plugins = Path(const.defaultPluginPath)
    shutil.copytree(ROOT / "dmmr", plugins / "dmmr", ignore=shutil.ignore_patterns("__pycache__", "logs"))
    Path(const.defaultConfigPath).mkdir(parents=True, exist_ok=True)
    (Path(const.defaultConfigPath) / "plugins.ini").write_text("[DMMR]\nenabled = True\n", encoding="utf-8")

    import numpy as np
    from PyQt6.QtCore import QTimer
    from esibd.core import Application, EsibdExplorer, SplashScreen

    app = Application(sys.argv[:1])
    app.splashScreen = SplashScreen(app=app)
    window = EsibdExplorer(app=app)
    app.mainWindow = window
    window.resize(1400, 900)
    window.show()
    report = {"code": None}
    state = {"phase": "load", "t": time.monotonic(), "deadline": time.monotonic() + 120}

    def curves(device):
        return {channel.name: channel.plotCurve is not None for channel in device.getChannels()}

    def finish(code):
        report["code"] = code
        (work / "report.json").write_text(json.dumps(report, default=str), encoding="utf-8")
        try:
            window.closeApplication(confirm=False)
        except Exception:  # noqa: BLE001
            report["close_error"] = traceback.format_exc()
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
                device = state["device"] = manager.DMMR
                device._sync_channels_from_detected_modules([1, 2])
                now = time.time()
                for i in range(60):  # An acquisition stopped 19 minutes ago.
                    for k, channel in enumerate(device.getChannels()):
                        channel.values.add(x=(k + 1) * 1e-11 * (1 + 0.1 * np.sin(i / 5)), lenT=device.time.size)
                    device.time.add(now - 1200 + i)
                for channel in device.getChannels():
                    channel.display = True
                device.toggleLiveDisplay()
                state.update(phase="plot", t=time.monotonic())
            elif state["phase"] == "plot":
                if time.monotonic() - state["t"] < 1:
                    return
                device, display = state["device"], state["device"].liveDisplay
                if not state.get("raised"):
                    display.raiseDock(True)
                    state.update(raised=True, t=time.monotonic())
                    return
                if manager.resizing:
                    return  # Explorer suspends drawing during dock layout changes.
                display_time = display.getDisplayTime
                display.getDisplayTime = lambda: -1  # Drawn while acquiring, when the data were recent.
                display.plot(apply=True)
                display.getDisplayTime = display_time
                report["recording"] = device.recording
                report["before"] = curves(device)
                channel = device.getChannels()[0]
                channel.getParameterByName(channel.COLOR).value = "#ff0000"  # As the colour dialog does.
                state.update(phase="after", t=time.monotonic())
            elif state["phase"] == "after":
                if time.monotonic() - state["t"] < 1:
                    return
                device, display = state["device"], state["device"].liveDisplay
                report["after"] = curves(device)
                report["color"] = device.getChannels()[0].color
                widget = display.livePlotWidgets[0]
                report["xrange"] = [float(v) - time.time() for v in widget.getViewBox().viewRange()[0]]
                left, right = widget.getAxis("left"), widget.getAxis("right")
                report["label"], report["units"] = left.labelText, left.labelUnits
                for mode, values, spacing in (("linear", [1e-11, 2e-11], 1e-11), ("log", [-11.0, -10.0], 1.0)):
                    report[mode] = {}
                    for side, axis in (("left", left), ("right", right)):
                        saved = axis.logMode
                        axis.logMode = mode == "log"
                        report[mode][side] = axis.tickStrings(values, 1.0, spacing)
                        axis.logMode = saved
                report["legend"] = [c.plotCurve.name() for c in device.getChannels() if c.plotCurve]
                report["data"] = float(device.getChannels()[0].getValues()[-1])
                report["converted"] = float(device.getChannels()[0].convertDataDisplay(np.array([1e-11]))[0])
                device._recording = True  # Explorer's Device.recording state, without starting anything.
                report["recording_window"] = display._frozen_end()
                device._recording = False
                channels = device.getChannels()
                times = device.time.get().copy()
                samples = [ch.values.get().copy() for ch in channels]
                editor = device.channelPanelCards[1]["label_edit"]
                editor.setText("Collecteur")
                editor.editingFinished.emit()
                report["label_legend"] = [ch.plotCurve.name() for ch in channels]
                report["label_legend_text"] = [label.text for _, label in widget.legend.items]
                assert window.grab().save(str(work / "dmmr-plot-labels.png"))
                export = device.exportConfiguration
                def denied(**kwargs):
                    raise PermissionError("read-only configuration")
                device.exportConfiguration = denied
                editor.setText("Unsaved label")
                editor.editingFinished.emit()
                report["failed_label_legend"] = [ch.plotCurve.name() for ch in channels]
                assert channels[0].label == "Collecteur" and editor.isModified()
                device.exportConfiguration = export
                device._recording = True
                display.getDisplayTime = lambda: -1
                editor.setText("Entr\u00e9e \u00b5A 50%")
                editor.editingFinished.emit()
                display.plot(apply=True)  # The next recording frame, without hardware I/O.
                report["recording_label_legend"] = [ch.plotCurve.name() for ch in channels]
                device._recording = False
                editor.setText("")
                editor.editingFinished.emit()
                report["empty_label_legend"] = [ch.plotCurve.name() for ch in channels]
                report["label_channel_names"] = [ch.name for ch in channels]
                np.testing.assert_array_equal(device.time.get(), times)
                for ch, values in zip(channels, samples, strict=True):
                    np.testing.assert_array_equal(ch.values.get(), values)
                finish(0)
        except Exception:  # noqa: BLE001
            report["exception"] = traceback.format_exc()
            finish(1)

    timer = QTimer()
    timer.timeout.connect(tick)
    timer.start(200)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
