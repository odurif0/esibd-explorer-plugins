"""Transmission plugin with real Explorer Scan/Settings/Display classes under Qt (isolated process)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ("simulation-total", "simulation-mass", "stop", "not-ready", "advanced", "spectrum",
             "explorer-beamline", "explorer-run", "explorer-mass", "explorer-external-change", "explorer-device-off",
             "explorer-interlock", "explorer-not-recording", "explorer-psu", "explorer-devices")

# Explorer channels backed by the simulator physics: one stage, short timings (advanced mode).
EXPLORER_CONFIG = """
settle_s = 0.1
average_s = 0.3
poll_s = 0.05
settle_timeout_s = 5.0
verify_pairs = 2
reference_every = 4
strategy = "coordinate"
[pressure]
P_funnel = [2.0, 6.0]
[[stage]]
name = "Funnel"
aperture = "A1"
downstream = ["A2", "A3", "A4", "Collector"]
budget = 18
[[stage.knob]]
name = "Inlet"
channels = { Inlet = 1.0 }
window = [-60.0, 60.0]
max_step = 10.0
[[stage.knob]]
name = "Funnel exit"
channels = { Funnel_exit = 1.0 }
window = [-10.0, 10.0]
max_step = 1.5
"""

# Selected mass behind Q2 (amplitude in volts), one short peak stage on the Q1 offset.
EXPLORER_MASS_CONFIG = """
settle_s = 0.1
average_s = 0.3
poll_s = 0.05
settle_timeout_s = 5.0
verify_pairs = 2
reference_every = 4
strategy = "coordinate"
[target]
filter = "Q2"
channels = { Q2_RF = 1.0 }
center = 520.0
width = 30.0
measure = ["A3", "A4", "Collector"]
points = 3
spectrum_points = 7
[[stage]]
name = "Q1"
aperture = "A2"
downstream = ["A3", "A4", "Collector"]
peak = true
budget = 12
[[stage.knob]]
name = "Q1 offset"
channels = { Q1_offset = 1.0 }
window = [-4.0, 4.0]
max_step = 1.0
"""


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_transmission_in_real_explorer_and_qt(tmp_path, scenario):
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", PYTHONUNBUFFERED="1", PYTHONFAULTHANDLER="1")
    result = subprocess.run([env.get("ESIBD_QT_PYTHON", sys.executable), str(Path(__file__)), scenario, str(tmp_path)],
                            env=env, text=True, capture_output=True, timeout=240)
    if result.returncode == 77:
        pytest.skip(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]
    assert "Traceback" not in result.stderr, result.stderr[-6000:]


def run(scenario, target, app_holder):
    sys.modules["pyautogui"] = ModuleType("pyautogui")
    try:
        from PyQt6.QtWidgets import QApplication, QWidget, QVBoxLayout, QHBoxLayout
        from PyQt6.QtTest import QTest
        from esibd import core, plugins
        import h5py
        import numpy as np
    except ImportError as exc:
        print(f"Full Explorer/Qt dependencies unavailable: {exc}")
        return 77
    import json
    import time

    app = QApplication([])
    app_holder.append(app)
    errors = []

    def log(**kw):
        print("LOG", kw)
        if kw.get("flag") == core.PRINT.ERROR:
            errors.append(kw.get("message"))

    class Manager(NS):
        def __getattr__(self, name):
            return getattr(plugins, name, None)

    manager = Manager(plugins=[], loading=True, closing=False, testing=False, logger=NS(print=log),
                      connectAllSources=lambda: None, reconnectSource=lambda *a: None)
    manager.getPluginsByClass = lambda cls: [p for p in manager.plugins if isinstance(p, cls)]
    manager.getPluginsByType = lambda kind: [p for p in manager.plugins if getattr(p, "pluginType", None) == kind]
    dm = manager.DeviceManager = plugins.DeviceManager.__new__(plugins.DeviceManager)
    from PyQt6.QtCore import QObject
    QObject.__init__(dm)
    dm.pluginManager = manager
    devices = []
    dm.getDevices = lambda inout=core.INOUT.BOTH: devices
    exports = []
    dm.exportConfiguration = lambda file: exports.append(file)
    measurement = [0]

    def next_file(suffix):
        measurement[0] += 1
        return target / f"M{measurement[0]:03d}{suffix}"
    manager.Settings = NS(loading=True, configPath=target / "config", dataPath=target / "data", dependencyPath=ROOT / "transmission",
                          sourceCodePath=ROOT / "transmission/transmission_plugin.py", sessionPath=target,
                          measurementNumber=1, getFullSessionPath=lambda: target, saveSettings=lambda **kw: None,
                          incrementMeasurementNumber=lambda: None, getMeasurementFileName=next_file)
    manager.Settings.configPath.mkdir(parents=True, exist_ok=True)
    manager.Explorer = NS(root=None, populateTree=lambda: None, activeFileFullPath=None)
    manager.Text = NS(setText=lambda *a, **kw: None)

    name = "transmission_plugin"
    module = core.dynamicImport(name, ROOT / "transmission" / "transmission_plugin.py")
    assert module is not None and name not in sys.modules  # Explorer's discovery: no registration
    assert module.providePlugins() == [module.Transmission]
    runtime = [key for key in sys.modules if key.startswith("_esibd_bundled_transmission_")]
    assert sorted(runtime) == ["_esibd_bundled_transmission_transmission",
                               "_esibd_bundled_transmission_transmission._beamline",
                               "_esibd_bundled_transmission_transmission._engine",
                               "_esibd_bundled_transmission_transmission._log",
                               "_esibd_bundled_transmission_transmission._simulator"], runtime

    window = QWidget()
    layout = QHBoxLayout(window)
    scan_widget, plot_widget = QWidget(), QWidget()
    scan_layout, plot_layout = QVBoxLayout(scan_widget), QVBoxLayout(plot_widget)
    layout.addWidget(scan_widget, 1)
    layout.addWidget(plot_widget, 2)
    confirmations, answer = [], [False]  # The device dialog is answered by the scenario, never shown.
    module.Transmission._confirm_devices = lambda self, text, action: (confirmations.append((text, action)), answer[0])[1]
    scan = module.Transmission(pluginManager=manager, dependencyPath=ROOT / "transmission",
                               sourceCodePath=ROOT / "transmission/transmission_plugin.py")
    manager.plugins.append(scan)
    scan.addContentWidget = scan_layout.addWidget
    scan.initGUI()
    scan_layout.insertWidget(0, scan.titleBar)
    scan.statusAction = NS(setVisible=lambda *a: None)
    manager.loading = False
    manager.Settings.loading = False
    assert set(scan.settingsMgr.settings) == {"Notes"}
    assert scan.default_config_path() == target / "config" / "Transmission.toml"
    scan.display = scan.Display(scan=scan, pluginManager=manager)
    scan.display.addContentWidget = plot_layout.addWidget
    scan.display.initGUI()
    scan.display.raiseDock = lambda *a, **kw: None
    scan.toggleDisplay = lambda visible: None if visible else scan.display.closeGUI()
    scan.displayActive = lambda: True
    panel = scan.panel
    window.resize(1500, 800)
    window.show()
    app.processEvents()

    def progress():
        return panel.progress.text()

    def wait_finished(seconds=200):
        deadline = time.monotonic() + seconds
        while (not scan.finished or scan.recording) and time.monotonic() < deadline:
            QTest.qWait(20)
        QTest.qWait(50)
        assert scan.finished and not scan.recording, progress()

    def wait_worker(seconds=60):
        deadline = time.monotonic() + seconds
        while (scan._reverting or scan._worker.is_alive()) and time.monotonic() < deadline:
            QTest.qWait(20)
        QTest.qWait(50)
        assert not scan._reverting and not scan._worker.is_alive()

    def wait_saved(seconds=20):
        deadline = time.monotonic() + seconds
        while not exports and time.monotonic() < deadline:
            QTest.qWait(20)
        assert exports == [scan.file] and scan.file.exists(), (exports, scan.file)

    logs = target / "data" / "logs" / "transmission"

    def session_events():
        return module._log.read(logs / "transmission_session.jsonl")

    def run_logs(kind):
        return sorted((logs / "runs").glob(f"transmission_*_{kind}.jsonl"))

    def stage_choice(*keys):
        for key, box in panel.stage_boxes.items():
            box.setChecked(key in keys)
        app.processEvents()

    def cleanup():
        import matplotlib.pyplot as plt
        scan.display.closeGUI()
        scan.closeGUI()
        window.close()
        plt.close("all")
        app.processEvents()

    try:
        return scenario_body(locals())
    finally:
        cleanup()


def write_mscan(path, amplitude, current, rails):
    import json
    import h5py
    with h5py.File(path, "w") as handle:
        group = handle.create_group("MScan")
        group.create_group("Input Channels").create_dataset("Amplitude", data=amplitude)
        group.create_group("Output Channels").create_dataset("DMMR Collector", data=current)
        setup = dict(rails=[dict(channel=name, psu="PSU_SIM", rail=rail) for name, rail in zip(rails, ("Vpos", "Vneg"))])
        group.create_group("Validation").attrs["setup"] = json.dumps(setup)


def scenario_body(env):
    globals().update(env)
    import json
    import time
    simulator, beamline_module = module._simulator, module._beamline

    if scenario == "not-ready":
        # Nothing configured: the panel says what to do first and Optimize stays disabled.
        assert not panel.optimize.isEnabled() and progress() == "Configure the beamline first.", progress()
        assert "Not configured" in panel.beamline_label.text() and not panel.stage_boxes
        assert not panel.keep.isVisibleTo(window) and not panel.revert.isVisibleTo(window)
        panel.simulation.setChecked(True)
        assert panel.optimize.isEnabled() and progress() == "Ready." and "Simulation" in panel.beamline_label.text()
        assert [panel.filter.itemData(i) for i in range(panel.filter.count())] == ["q1", "q2", "q3", "q4"]
        panel.mass.setChecked(True)
        assert scan.state["mode"] == "mass" and not panel.optimize.isEnabled()
        assert progress() == "Pick the peak of the selected mass first." and panel.pick.isEnabled()
        panel.total.setChecked(True)
        stage_choice()
        assert not panel.optimize.isEnabled() and progress() == "Choose at least one stage."
        stage_choice("q1")
        assert sorted(scan.state["skip"]) == ["final", "funnel", "q2", "q3", "q4"] and panel.optimize.isEnabled()
        # The choices persist across Explorer sessions.
        saved = json.loads((target / "config" / "Transmission.state.json").read_text(encoding="utf-8"))
        assert saved["simulation"] and saved["mode"] == "total" and "q1" not in saved["skip"]
        start = session_events()[0]
        assert start["event"] == "plugin_start" and start["log_directory"] == str(logs)
        assert set(start["environment"]["code"]) == {"transmission_plugin.py", "__init__.py", "_engine.py", "_simulator.py",
                                                     "_beamline.py", "_log.py"}
        opened = []
        module.QDesktopServices.openUrl = lambda url: opened.append(url.toLocalFile())
        panel.logs.click()
        assert opened == [str(logs)] and logs.is_dir()
        assert not exports and not errors, errors
        return 0

    if scenario == "advanced":
        path = scan.default_config_path()
        editor = module._ConfigEditor(scan, path, scan.generated_text())
        text = editor.editor.toPlainText()
        assert "cannot generate" in text and "[[stage]]" in text  # No beamline yet: template from Explorer channels.
        editor.editor.setPlainText("[[stage]]\nname='a'\n")
        editor.accept()
        assert "Not valid" in editor.message.text() and not path.exists()
        editor.editor.setPlainText(simulator.CONFIG)
        assert editor.check() is not None and "Not found in Explorer" in editor.message.text()
        editor.use.setChecked(True)
        editor.accept()
        assert path.read_text(encoding="utf-8") == simulator.CONFIG
        # Through the panel: the dialog result switches the advanced configuration on.
        module._ConfigEditor.exec = lambda self: (self.use.setChecked(True), True)[1]
        scan.edit_advanced()
        assert scan.state["advanced"] and panel.advanced.text() == "Advanced… (in use)"
        assert panel.optimize.isEnabled() and not panel.total.isEnabled()
        scan.start_optimization()
        app.processEvents()
        assert not scan.recording and scan.finished and "not found in Explorer" in progress(), progress()
        path.write_text("polarity = 3\n[[stage]]\nname='a'\ndownstream=['B']\n[[stage.knob]]\n"
                        "channels={X=1.0}\nwindow=[-1.0, 1.0]\nmax_step=1.0\n", encoding="utf-8")
        scan.start_optimization()
        app.processEvents()
        assert progress().startswith("Cannot start") and "polarity" in progress(), progress()
        path.unlink()
        scan.refresh_panel()
        assert not panel.optimize.isEnabled()
        scan.start_optimization()
        app.processEvents()
        assert "No advanced configuration" in progress(), progress()
        events = session_events()
        missing = next(e for e in events if e["event"] == "devices_not_ready")  # Checked before any start.
        assert missing["devices"] == {"Channels": "missing"} and not confirmations
        refused = [e for e in events if e["event"] == "cannot_start"]
        assert len(refused) == 2 and "polarity" in refused[0]["reason"] and refused[0]["traceback"]
        assert [e["use"] for e in session_events() if e["event"] == "advanced_configuration"] == [True]
        # "From the simple settings" writes what the panel would run.
        scan.state.update(advanced=False, simulation=True)
        editor = module._ConfigEditor(scan, path, scan.generated_text())
        assert module._engine.parse_config(editor.editor.toPlainText()).stages[0].name == "Inlet + funnel"
        assert not exports and not errors, errors
        return 0

    if scenario == "spectrum":
        panel.simulation.setChecked(True)
        amplitude = np.linspace(150.0, 900.0, 76)
        volts = {name: spec[2] for name, spec in simulator.KNOBS.items()}
        current = []
        for value in amplitude:
            volts["Q2_RF"] = value
            physics = simulator.transport(volts)
            current.append(sum(physics[c] for c in ("A3", "A4", "Collector")))
        write_mscan(target / "S001_mscan.h5", amplitude, current, ["Q2_RF"])
        (target / "other.h5").write_bytes(b"not hdf5")
        dialog = module._SpectrumDialog(scan, "q1")
        dialog.load_mscan(target / "other.h5")
        assert "Cannot read" in dialog.info.text() and not dialog.ok.isEnabled()
        dialog.load_mscan(target / "S001_mscan.h5")
        assert dialog.filter_key() == "q2" and "recognised" in dialog.info.text(), dialog.info.text()
        dialog.select_near(500.0)
        center, width = dialog.selection
        assert abs(center - 520.0) < 5.0 and 20.0 < width < 50.0 and dialog.ok.isEnabled(), dialog.selection
        # A quick sweep of the filter on the (simulated) beamline, then back to its amplitude.
        dialog.start.setValue(300.0)
        dialog.stop.setValue(700.0)
        dialog.points.setValue(21)
        dialog.sweep()
        app.processEvents()
        assert scan._sweeping and not panel.optimize.isEnabled()  # Locked until the filter is back.
        deadline = time.monotonic() + 60
        while dialog.outcome is None and time.monotonic() < deadline:
            QTest.qWait(20)
        QTest.qWait(50)
        assert not scan._sweeping and panel.optimize.isEnabled()
        assert dialog.outcome["status"] == "completed", dialog.outcome
        assert len(dialog.amplitude) == 21 and dialog.sweep_button.isEnabled()
        assert scan._sim.setpoint["Q2_RF"] == simulator.KNOBS["Q2_RF"][2]
        dialog.select_near(540.0)
        assert abs(dialog.selection[0] - 520.0) < 15.0, dialog.selection
        (sweep,) = run_logs("sweep")
        events = [e for e in module._log.read(sweep) if e["event"] not in ("plot_slow", "gui_stall", "gui_slow")]
        kinds = [e["event"] for e in events]
        assert kinds[0] == "header" and kinds[1] == "sweep_start" and kinds[-1] == "sweep_end", kinds
        assert events[0]["filter"] == "q2" and len(events[0]["amplitudes"]) == 21 and events[0]["simulation"]
        assert sum(k == "measure" for k in kinds) == 21 and events[-1]["status"] == "completed"
        quick = [e for e in session_events() if e["event"] == "quick_sweep"]
        assert quick[0]["status"] == "completed" and quick[0]["log"] == str(sweep)
        assert [e["event"] for e in session_events() if e["event"].startswith("mscan")] == ["mscan_unreadable", "mscan_spectrum"]
        window.grab().save(str(target / "spectrum-panel.png"))
        dialog.grab().save(str(target / "spectrum-dialog.png"))
        # Pick on spectrum… → OK fills the target.
        def accept(self):
            self.load_mscan(target / "S001_mscan.h5")
            self.select_near(520.0)
            return True
        module._SpectrumDialog.exec = accept
        scan.pick_peak()
        assert scan.state["mode"] == "mass" and scan.state["filter"] == "q2" and panel.mass.isChecked()
        picked = [e for e in session_events() if e["event"] == "peak_selected"][-1]
        assert picked["source"] == str(target / "S001_mscan.h5") and picked["filter"] == "q2"
        assert abs(panel.center.value() - 520.0) < 5.0 and panel.width.value() > 20.0 and panel.optimize.isEnabled()
        assert [key for key in panel.stage_boxes] == ["funnel", "q1", "q2", "q3", "q4", "final"]
        assert "Q2 rf amplitude" not in scan.generated_text() and 'filter = "Q2"' in scan.generated_text()
        assert not exports and not errors, errors
        return 0

    if scenario.startswith("explorer-"):
        return explorer_scenario(env)

    panel.simulation.setChecked(True)
    scan.notes = "Funnel tuning (trial 1)"
    if scenario == "simulation-mass":
        scan.apply_peak("q2", 520.0, 30.0)
        stage_choice("q1")
    elif scenario == "simulation-total":
        stage_choice("funnel", "final")
    assert panel.optimize.isEnabled(), progress()
    panel.optimize.click()
    app.processEvents()
    assert not panel.optimize.isEnabled() and panel.stop.isEnabled() and not panel.configure.isEnabled()
    if scenario == "stop":
        deadline = time.monotonic() + 60
        while len(scan._records) < 8 and time.monotonic() < deadline:
            QTest.qWait(20)
        panel.stop.click()
    wait_finished()
    result = scan._result
    print("RESULT", json.dumps(module._jsonable(result))[:2000])
    print("PANEL", progress(), "|", panel.result.text())
    if scenario == "stop":
        assert result["status"] == "stopped", result
        # Stop returns to the start of the stage in progress; completed stages keep their verdict.
        current = scan._stages_meta[-1]
        assert all(abs(result["final"][c] - v) < 1e-9 for c, v in current["start"].items()), result
        for done in result["stages"]:
            assert all(abs(result["final"][c] - v) < 1e-9 for c, v in done["final"].items() if c not in current["start"])
        assert progress().startswith("Stopped"), progress()
    else:
        assert result["status"] == "completed", result
        assert progress() == "Completed.", progress()
        stages = result["stages"]
        if scenario == "simulation-total":
            assert [s["stage"] for s in stages] == ["Inlet + funnel", "Final refinement"] and stages[0]["adopted"], stages
            assert panel.result.text().startswith("Collector: ") and "stage(s) improved" in panel.result.text()
        else:
            assert [s["stage"] for s in stages] == ["Q1"] and abs(result["target"]["final_center"] - 520.0) < 10.0
            assert panel.result.text().startswith("Selected mass (Q2 at 5"), panel.result.text()
            assert set(scan._spectra) == {"before", "after"}
            assert len(scan.display.axes[1].get_lines()) == 2  # Peak before/after.
    assert not errors, errors
    wait_saved()
    with h5py.File(scan.file, "r") as handle:
        group = handle["Transmission"]
        assert group.attrs["simulation"] and group.attrs["notes"] == "Funnel tuning (trial 1)"
        assert json.loads(group.attrs["result"])["status"] == result["status"]
        assert json.loads(group.attrs["beamline"]) == beamline_module.normalize(simulator.BEAMLINE)
        stages = group["Stages"]
        assert len(stages) == len(scan._stages_meta)
        first = stages[sorted(stages)[0]]
        n = len(first["index"])
        assert first["currents"].shape[1:] == (len(json.loads(first["currents"].attrs["channels"])), 3)
        assert first["delta"].shape[0] == n and set(first["kind"].asstr()[:]) >= {"initial", "probe"}
        assert "configuration" in group.attrs
        if scenario == "simulation-mass":
            assert len(json.loads(first.attrs["peaks"])) == n
        (run_log,) = run_logs("simulation")
        assert group.attrs["log_file"] == str(run_log)
    events = module._log.read(run_log)
    kinds = [e["event"] for e in events if e["event"] not in ("plot_slow", "gui_stall", "gui_slow")]  # GUI timing: any time
    assert kinds[:2] == ["header", "run_start"] and kinds[-2:] == ["run_end", "summary"], kinds[-5:]
    header = events[0]
    assert header["file"] == str(scan.file) and header["configuration"] == scan._config_used and header["notes"]
    assert header["environment"]["plugin_version"] == module.Transmission.version and header["state"]["simulation"]
    assert sum(k == "evaluation" for k in kinds) == len(scan._records)
    summary = next(e for e in events if e["event"] == "summary")
    assert summary["status"] == result["status"] and summary["line"] == panel.result.text()
    session = [e["event"] for e in session_events()]
    assert session[0] == "plugin_start" and session[-1] == "run_end" and "run_start" in session, session
    window.grab().save(str(target / f"{scenario}.png"))
    records, spectra = len(scan._records), dict(scan._spectra)
    if scenario != "stop":
        assert panel.keep.isVisible() and panel.revert.isVisible()
    if scenario == "simulation-total":
        panel.keep.click()
        assert not panel.keep.isVisible() and progress() == "Optimized settings kept."
        assert session_events()[-1]["event"] == "keep" and session_events()[-1]["log"] == str(run_log)
    elif scenario == "simulation-mass":
        panel.revert.click()
        wait_worker()
        assert progress() == "Revert restored.", progress()
        for channel in ("Q1_RF", "Q1_offset", "Q2_RF"):
            assert scan._sim.setpoint[channel] == simulator.KNOBS[channel][2], channel
        assert not panel.revert.isVisible() and panel.optimize.isEnabled()
        (revert,) = run_logs("revert")
        events = [e for e in module._log.read(revert) if e["event"] not in ("plot_slow", "gui_stall", "gui_slow")]
        assert events[0]["run_log"] == str(run_log) and events[1]["event"] == "revert_start"
        assert events[-1]["event"] == "revert_end" and events[-1]["status"] == "restored"
        assert session_events()[-1]["event"] == "revert" and session_events()[-1]["log"] == str(revert)
    if scenario == "stop":
        assert "stop_requested" in [e["event"] for e in session_events()]
        assert next(e for e in events if e["event"] == "run_end")["status"] == "stopped"
        assert any(e["event"] == "restore" for e in events)
    scan.loadData(scan.file)
    assert len(scan._records) == records and scan._stages_meta, (len(scan._records), records)
    assert set(scan._spectra) == set(spectra)
    app.processEvents()
    assert not errors, errors
    return 0


def explorer_scenario(env):
    import json
    import math
    import threading
    import time
    import numpy as np
    from PyQt6.QtCore import QTimer
    simulator, beamline_module = module._simulator, module._beamline
    sets = []

    class FakeDevice:
        def __init__(self, name, unit, monitors):
            self.name, self.unit, self.useMonitors = name, unit, monitors
            self.on, self.recording = True, True
            self.time = core.DynamicNp(dtype=np.float64)
            self.channels = []
            self.on_requests, self.recording_requests = [], []

        def isOn(self):
            return self.on

        def setOn(self, on=None):
            # Like a CGC plugin: ON completes after its initialization.
            self.on_requests.append(on)
            if on:
                QTimer.singleShot(400, lambda: setattr(self, "on", True))
            else:
                self.on = False

        def toggleRecording(self, on=None, manual=True):
            self.recording_requests.append(on)
            if on and self.on:  # Refused until the device is really ON.
                self.recording = True

        def getChannels(self):
            return self.channels

    class FakeChannel:
        def __init__(self, device, name, value=0.0, limits=(None, None)):
            self.device, self.name = device, name
            self._value, self.min, self.max = float(value), *limits
            self.monitor = float(value) if device.useMonitors else None
            self.useMonitors, self.unit = device.useMonitors, device.unit
            self.enabled = self.active = self.real = True
            self.inout = core.INOUT.IN if device.unit == "V" else core.INOUT.OUT
            self.values = core.DynamicNp()
            device.channels.append(self)

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, value):
            sets.append((self.name, float(value), time.monotonic()))
            self._value = float(value)

        def getDevice(self):
            return self.device

        def getValues(self, subtractBackground=False):
            return self.values.get()

    psu = FakeDevice("PSU_SIM", "V", True)
    psu.controller = NS(initializing=False, transitioning=False, _manual_apply_active=False,
                        _manual_apply_worker_running=False, _hv_config_loading=False)
    dmmr = FakeDevice("DMMR_SIM", "A", True)
    tpg = FakeDevice("TPG_SIM", "mbar", False)
    knobs = {name: FakeChannel(psu, name, spec[2], spec[:2]) for name, spec in simulator.KNOBS.items()}
    currents = {name: FakeChannel(dmmr, name) for name in simulator.CURRENTS}
    pressures = {name: FakeChannel(tpg, name, value) for name, value in simulator.PRESSURES.items()}
    devices.extend([psu, dmmr, tpg])
    rng = np.random.default_rng(0)
    state = dict(pressure_factor=1.0)

    def physics():
        # Readbacks follow the setpoints at 200 V/s; DMMR and TPG append timestamped samples.
        for channel in knobs.values():
            step = 200.0 * 0.05
            delta = channel.value - channel.monitor
            channel.monitor = channel.value if abs(delta) <= step else channel.monitor + math.copysign(step, delta)
        true = simulator.transport({name: c.monitor for name, c in knobs.items()})
        now = time.time()
        for name, channel in currents.items():
            if dmmr.recording:
                channel.monitor = true[name] * (1 + 0.01 * rng.normal()) + 2e-14 * rng.normal()
                channel.values.add(channel.monitor)
        for name, channel in pressures.items():
            if tpg.recording:
                channel._value = simulator.PRESSURES[name] * (state["pressure_factor"] if name == "P_funnel" else 1.0)
                channel.values.add(channel._value)
        if dmmr.recording:
            dmmr.time.add(now)
        if tpg.recording:
            tpg.time.add(now)
        for channel in knobs.values():
            channel.values.add(channel.monitor)
        psu.time.add(now)

    timer = QTimer()
    timer.timeout.connect(physics)
    timer.start(50)
    QTest.qWait(300)
    start = {name: c.value for name, c in knobs.items()}
    plots = []
    original_plot = scan.plot

    def recorded_plot(*args, **kwargs):
        plots.append((time.monotonic(), scan._measuring, kwargs.get("done", False)))
        return original_plot(*args, **kwargs)
    scan.plot = recorded_plot

    def check_steps(limits, external=None):
        # Every request went through Channel.value in steps no larger than max_step.
        previous = dict(start)
        for name, value, _ in sets:
            if external is not None and value == previous[name] + external:
                previous[name] = value
                continue
            limit = limits.get(name)
            assert limit is not None and abs(value - previous[name]) <= limit + 1e-9, (name, previous[name], value)
            previous[name] = value

    if scenario == "explorer-beamline":
        esi = FakeDevice("ESI", "V", True)
        esi_hv = FakeChannel(esi, "ESI HV1", 0.0, (0.0, 3000.0))
        devices.append(esi)
        driven, measured, gauges = scan.channel_lists()
        assert driven == list(simulator.KNOBS) and measured == list(simulator.CURRENTS)  # No ESI channel offered.
        assert gauges == list(simulator.PRESSURES)
        config = module._engine.parse_config('[[stage]]\nname="s"\ndownstream=["A1"]\n[[stage.knob]]\n'
                                             'channels={"ESI HV1"=1.0}\nwindow=[-1.0, 1.0]\nmax_step=1.0\n')
        with pytest.raises(module._engine.ConfigError, match="ESI channels are not driven"):
            module.ExplorerInstrument(scan, config, threading.Event())
        devices.remove(esi)
        assert esi_hv.value == 0.0 and not sets
        dialog = module._BeamlineDialog(scan, scan.beamline_map)
        for key, channels in simulator.BEAMLINE["knobs"].items():
            dialog.knob_widgets[key][0].setCurrentText(next(iter(channels)))
        dialog.knob_widgets["q2_rf"][1].setCurrentText("Q3_RF")  # A second rail, then removed again.
        for key, channel in simulator.BEAMLINE["collectors"].items():
            dialog.collector_widgets[key].setCurrentText(channel)
        watch, low, high = dialog.gauge_widgets["P_funnel"]
        watch.setChecked(True)
        low.setText("2")
        high.setText("6")
        dialog.collector_widgets["A2"].setCurrentText("A1")
        dialog.accept()
        assert "two apertures" in dialog.message.text() and dialog.result() == 0
        dialog.collector_widgets["A2"].setCurrentText("A2")
        dialog.knob_widgets["q2_rf"][1].setCurrentText(dialog.NONE)
        dialog.accept()
        owners = {c.name: d.name for d in devices for c in d.channels}
        expected = dict(simulator.BEAMLINE, gauges={"P_funnel": [2.0, 6.0]}, devices=owners)
        assert dialog.result_beamline == beamline_module.normalize(expected), dialog.result_beamline
        assert dialog.result_beamline["devices"]["Inlet"] == "PSU_SIM" and "P_Q4" not in dialog.result_beamline["devices"]
        scan.save_beamline(dialog.result_beamline)
        module._BeamlineDialog.exec = lambda self: (self.accept(), True)[1]
        scan.configure_beamline()  # Reopened unchanged, the dialog gives back the saved mapping.
        saved = beamline_module.loads((target / "config" / "Transmission.beamline.json").read_text(encoding="utf-8"))
        assert saved == beamline_module.normalize(expected) == scan.beamline_map
        assert panel.beamline_label.text() == "✓ 12 settings · 5 currents · 1 gauge", panel.beamline_label.text()
        assert panel.optimize.isEnabled() and len(panel.stage_boxes) == 6
        config = module._engine.parse_config(scan.generated_text())
        assert scan.missing_channels(config) == [] and config.pressure == {"P_funnel": (2.0, 6.0)}
        assert (config.settle_s, config.average_s) == (1.0, 2.0)  # Real devices: the default timings.
        # A freeze of Explorer's Qt thread records queued samples in a burst: one observation, not several.
        burst_device = FakeDevice("BURST_SIM", "A", True)
        burst_device.interval = 100  # ms
        duplicated, dmmr_like = FakeChannel(burst_device, "Burst"), FakeChannel(burst_device, "BurstNaN")
        t0 = 1000.0
        offsets = (0.1, 0.2, 0.3, 1.300, 1.301, 1.302, 1.303, 1.4, 1.5, 1.6)
        for offset, a, b in zip(offsets, (1, 2, 3, 9, 9, 9, 9, 5, 6, 0), (1, 2, 3, 9, math.nan, math.nan, math.nan, 5, 6, 0)):
            burst_device.time.add(t0 + offset)
            duplicated.values.add(a)
            dmmr_like.values.add(b)
        devices.append(burst_device)
        config = module._engine.parse_config('[[stage]]\nname="s"\ndownstream=["Burst", "BurstNaN"]\n[[stage.knob]]\n'
                                             'channels={Inlet=1.0}\nwindow=[-1.0, 1.0]\nmax_step=1.0\n')
        instrument = module.ExplorerInstrument(scan, config, threading.Event())
        data = instrument.window(["Burst", "BurstNaN"], t0, t0 + 1.55)
        used = np.array([1, 2, 3, 9, 5, 6], float)
        for name in ("Burst", "BurstNaN"):
            mean, sem, count = data[name]
            assert count == 6 and mean == pytest.approx(used.mean()) and sem == pytest.approx(used.std(ddof=1) / math.sqrt(6))
        details = instrument.diagnostics()["window"]
        assert (details["Burst"]["samples"], details["Burst"]["finite"], details["Burst"]["collapsed"]) == (9, 9, 3)
        assert (details["BurstNaN"]["finite"], details["BurstNaN"]["collapsed"]) == (6, 0)
        assert details["Burst"]["max_gap_s"] == pytest.approx(1.0) and details["Burst"]["nominal_interval_s"] == 0.1
        assert instrument.window(["Burst"], t0, t0 + 1.7) is None  # Not closed yet.
        timer.stop()
        assert not sets and not errors, errors
        return 0

    if scenario == "explorer-psu":
        return psu_scenario(env, locals())
    if scenario == "explorer-devices":
        return devices_scenario(env, locals())
    scan.save_beamline(simulator.BEAMLINE)  # The fake channels carry the simulator's names.
    path = scan.default_config_path()
    path.write_text(EXPLORER_MASS_CONFIG if scenario == "explorer-mass" else EXPLORER_CONFIG, encoding="utf-8")
    scan.state.update(advanced=True, simulation=False)
    scan.refresh_panel()
    assert "✓" in panel.beamline_label.text() and panel.optimize.isEnabled(), progress()
    if scenario == "explorer-not-recording":
        dmmr.recording = False
        scan.refresh_devices()
        assert "✗ DMMR_SIM: not recording" in panel.devices_label.text() and panel.start_devices.isVisible()
        scan.start_optimization()
        app.processEvents()
        (text, action), = confirmations
        assert action == "Turn ON and optimize" and "DMMR_SIM</b> — start recording (measures A1" in text
        assert not scan.recording and progress() == "Devices not turned ON: nothing started.", progress()
        assert not sets and dmmr.recording_requests == []
        timer.stop()
        return 0
    if scenario == "explorer-mass":
        # Quick sweep through Explorer channels, then the filter goes back to its amplitude.
        points, outcome = [], {}
        worker = threading.Thread(target=lambda: outcome.update(scan.quick_sweep(
            "q2", np.linspace(250.0, 330.0, 5), lambda a, c: points.append((a, c)), threading.Event())))
        worker.start()
        while worker.is_alive():
            QTest.qWait(20)
        assert outcome["status"] == "completed" and len(points) == 5, outcome
        (sweep,) = run_logs("sweep")
        assert module._log.read(sweep)[0]["channels"]["driven"]["Q2_RF"]["value"] == start["Q2_RF"]
        assert knobs["Q2_RF"].value == start["Q2_RF"]
        check_steps({"Q2_RF": 20.0})
        sets.clear()
    scan.start_optimization()
    if scenario not in ("explorer-run", "explorer-mass"):
        deadline = time.monotonic() + 60
        while len(scan._records) < 5 and time.monotonic() < deadline:
            QTest.qWait(20)
        count = len(sets)
        if scenario == "explorer-external-change":
            knobs["Inlet"].value = knobs["Inlet"].value + 3.0  # an operator edit during the run
            count += 1
        elif scenario == "explorer-device-off":
            psu.on = False
        elif scenario == "explorer-interlock":
            state["pressure_factor"] = 2.0  # 8 mbar in the funnel
    wait_finished(150)
    result = scan._result
    print("RESULT", json.dumps(module._jsonable(result))[:1500])
    limits = {"Q2_RF": 15.0, "Q1_offset": 1.0} if scenario == "explorer-mass" else {"Inlet": 10.0, "Funnel_exit": 1.5}
    check_steps(limits, 3.0 if scenario == "explorer-external-change" else None)
    (run_log,) = run_logs("run")
    events = module._log.read(run_log)
    channels = events[0]["channels"]
    driven, measured = next(iter(channels["driven"])), next(iter(channels["measured"]))
    assert channels["driven"][driven]["min"] == simulator.KNOBS[driven][0] and channels["driven"][driven]["device"] == "PSU_SIM"
    assert channels["measured"][measured]["unit"] == "A" and channels["measured"][measured]["on"]
    measures = [e for e in events if e["event"] == "measure"]
    assert measures and all(m["instrument"]["window"] for m in measures)
    assert all(m["instrument"]["window"][c]["max_gap_s"] >= 0 for m in measures for c in m["instrument"]["window"])
    assert any("gui" in m["instrument"] for m in measures)
    # Live redraws: never inside an averaging window, and spaced (a few are forced: stage start, spectra).
    live = [t for t, measuring, done in plots if not done]
    assert not any(measuring for _, measuring, _ in plots), "redraw during an averaging window"
    assert sum(b - a < 2.9 for a, b in zip(live, live[1:])) <= 3, live
    if scenario == "explorer-run":
        assert result["status"] == "completed", result
        stage = result["stages"][0]
        assert stage["adopted"] and stage["relative_gain"] > 0.05, stage
        assert all(abs(knobs[c].value - v) < 1e-9 for c, v in stage["final"].items())
        assert panel.revert.isVisible()
        panel.revert.click()
        wait_worker()
        assert all(knobs[c].value == start[c] for c in ("Inlet", "Funnel_exit")), "Revert restored the start"
        check_steps(limits)
    elif scenario == "explorer-mass":
        assert result["status"] == "completed", result
        assert abs(result["target"]["final_center"] - 520.0) <= 60.0 and knobs["Q2_RF"].value == result["target"]["final_center"]
        assert len(result["spectra"]["before"]["amplitude"]) == 7 and "after" in result["spectra"]
        assert panel.result.text().startswith("Selected mass (Q2 at "), panel.result.text()
    elif scenario in ("explorer-external-change", "explorer-device-off"):
        assert result["status"] == "instrument error", result
        assert ("changed outside" if scenario == "explorer-external-change" else "is OFF") in result["reason"], result
        assert len(sets) == count, "no command after the instrument error"
        end = next(e for e in events if e["event"] == "run_end")
        assert end["status"] == "instrument error" and "InstrumentError" in end["traceback"]
        assert progress().startswith("Instrument error:"), progress()
    elif scenario == "explorer-interlock":
        assert result["status"] == "interlock" and "P_funnel" in result["reason"], result
        assert all(abs(knobs[c].value - start[c]) < 1e-9 for c in ("Inlet", "Funnel_exit")), "stage start restored"
    timer.stop()
    wait_saved()
    assert not errors, errors
    return 0


def devices_scenario(env, ctx):
    """Devices that are OFF or not recording are proposed for turning ON, never turned ON without consent."""
    globals().update(env)
    import threading
    import time
    psu, dmmr, tpg, timer, sets = ctx["psu"], ctx["dmmr"], ctx["tpg"], ctx["timer"], ctx["sets"]
    FakeDevice, FakeChannel, wait_finished = ctx["FakeDevice"], ctx["FakeChannel"], env["wait_finished"]
    scan.default_config_path().write_text(EXPLORER_CONFIG, encoding="utf-8")
    scan.save_beamline(module._simulator.BEAMLINE)
    scan.state.update(advanced=True, simulation=False)
    psu.on = dmmr.on = dmmr.recording = False
    scan.refresh_panel()
    label = panel.devices_label.text()
    assert "✗ PSU_SIM: off" in label and "✗ DMMR_SIM: off" in label and "✓ TPG_SIM" in label, label
    assert panel.start_devices.isVisible() and "PSU_SIM (drives Inlet, Funnel_exit)" in panel.devices_label.toolTip()
    window.grab().save(str(target / "devices.png"))
    # 1. Declined from the toolbar: nothing is turned ON, nothing is sent.
    scan.recordingAction.trigger()
    app.processEvents()
    assert confirmations[-1][1] == "Turn ON and optimize" and not scan.recording and scan.finished
    assert psu.on_requests == [] and dmmr.on_requests == [] and not sets
    # 2. Accepted: ON through each device's own path, then recording, then the optimization starts by itself.
    answer[0] = True
    panel.optimize.click()
    text = confirmations[-1][0]
    assert "PSU_SIM</b> — turn ON (drives Inlet, Funnel_exit)" in text and "DMMR_SIM</b> — turn ON (measures" in text
    assert "TPG_SIM" not in text and "applies the setpoints" in text
    assert psu.on_requests == [True] and dmmr.on_requests == [True] and tpg.on_requests == []
    assert not panel.optimize.isEnabled() and progress().startswith("Turning ON"), progress()
    deadline = time.monotonic() + 30
    while not scan.recording and scan.finished and time.monotonic() < deadline:
        QTest.qWait(50)
    assert dmmr.recording and dmmr.recording_requests[-1] is True and scan.recording, progress()
    wait_finished(120)
    assert scan._result["status"] == "completed", scan._result
    session = [e["event"] for e in session_events()]
    assert session.index("devices_declined") < session.index("devices_turn_on") < session.index("devices_ready") \
        < session.index("run_start"), session
    # 3. Not offered: a missing plugin (named from the beamline), the ESI, a device in transition.
    ghost = dict(module._simulator.BEAMLINE, knobs=dict(module._simulator.BEAMLINE["knobs"], q4_rf={"Ghost": 1.0}),
                 devices={"Ghost": "PSU_B"})
    scan.save_beamline(ghost)
    esi = FakeDevice("ESI", "V", True)
    esi.on = False
    FakeChannel(esi, "ESI current")
    devices.append(esi)
    psu.controller = NS(transitioning=True)
    report = {e["name"]: e for e in scan.device_report(["Ghost", "Inlet"], ["ESI current", "A1"])}
    assert report["PSU_B"]["state"] == "missing" and "enable the PSU_B plugin" in report["PSU_B"]["fix"]
    assert report["ESI"]["state"] == "off" and "ESI plugin" in report["ESI"]["fix"]
    assert report["PSU_SIM"]["state"] == "busy" and report["DMMR_SIM"]["state"] == "ready"
    count = len(confirmations)
    assert scan.start_devices(list(report.values())) is False and len(confirmations) == count  # No question asked.
    message = progress()
    assert message.startswith("Not ready: ") and "PSU_SIM: ON/OFF transition in progress" in message, message
    assert "PSU_B: enable the PSU_B plugin (Plugin Manager), then restart Explorer" in message
    assert "ESI: turn it ON in the ESI plugin (never from Transmission)" in message
    assert esi.on_requests == []
    # 4. A PSU output gate OFF: shown, never switched from here, and refused by the run check.
    devices.remove(esi)
    psu.controller = NS(transitioning=False)
    ctx["knobs"]["Inlet"].output_state = "OFF"
    report = {e["name"]: e for e in scan.device_report(["Inlet", "Funnel_exit"], ["A1"])}
    assert report["PSU_SIM"]["state"] == "output off" and "(Inlet)" in report["PSU_SIM"]["fix"]
    assert scan.start_devices(list(report.values())) is False and len(confirmations) == count
    config = module._engine.parse_config(EXPLORER_CONFIG)
    instrument = module.ExplorerInstrument(scan, config, threading.Event())
    with pytest.raises(module._engine.InstrumentError, match="Inlet: output OFF in PSU_SIM"):
        instrument.check()
    ctx["knobs"]["Inlet"].output_state = "ON"
    instrument.check()
    timer.stop()
    return 0


def psu_scenario(env, ctx):
    """PSU protections taken from MScan: device states, applying worker, Ilim, hardware voltage limit."""
    globals().update(env)
    import threading
    import time
    knobs, sets, psu, timer, start = ctx["knobs"], ctx["sets"], ctx["psu"], ctx["timer"], ctx["start"]
    wait_finished, run_logs = env["wait_finished"], env["run_logs"]
    for channel in knobs.values():
        channel.hardware_voltage_limit, channel.current_readback, channel.current_limit_readback = 1000.0, 1e-4, 1e-3
    knobs["Inlet"].hardware_voltage_limit = 130.0  # Caps the window above the device Max (400 V).
    scan.default_config_path().write_text(EXPLORER_CONFIG, encoding="utf-8")
    scan.save_beamline(module._simulator.BEAMLINE)
    scan.state.update(advanced=True, simulation=False)
    # 1. A device transition refuses the start without any command.
    psu.controller.transitioning = True
    scan.start_optimization()
    app.processEvents()
    assert not scan.recording and "ON/OFF transition in progress" in progress(), progress()
    psu.controller.transitioning = False
    # 2. Each write starts the PSU's apply worker for 0.3 s: the next setpoint waits for it.
    applying, overlaps, last = [], [], [0.0]
    original = type(knobs["Inlet"]).value.fset

    def busy_write(channel, value):
        now = time.monotonic()
        # The channels of one command are written together; a new command must wait for the worker.
        if psu.controller._manual_apply_worker_running and now - last[0] > 0.05:
            overlaps.append(now)
        original(channel, value)
        last[0] = now
        psu.controller._manual_apply_worker_running = True
        applying.append(now)
        threading.Timer(0.3, lambda: setattr(psu.controller, "_manual_apply_worker_running", False)).start()
    type(knobs["Inlet"]).value = property(type(knobs["Inlet"]).value.fget, busy_write)
    scan.start_optimization()
    deadline = time.monotonic() + 60
    while len(scan._records) < 6 and time.monotonic() < deadline:
        QTest.qWait(20)
    assert len(applying) > 6 and not overlaps, "a setpoint was written while the previous one was being applied"
    assert all(value <= 130.0 for name, value, _ in sets if name == "Inlet"), "hardware voltage limit exceeded"
    # 3. A rail at its current limit stops the run without any further command.
    count = len(sets)
    knobs["Funnel_exit"].current_readback = 1e-3
    wait_finished(60)
    QTest.qWait(500)
    result = scan._result
    assert result["status"] == "instrument error" and "reached Ilim" in result["reason"], result
    assert len(sets) <= count + 1, "commands after the current limit"  # At most the write already in flight.
    (run_log,) = run_logs("run")
    end = next(e for e in module._log.read(run_log) if e["event"] == "run_end")
    assert "Ilim" in end["reason"] and end["traceback"]
    timer.stop()
    return 0


if __name__ == "__main__":
    applications = []
    try:
        code = run(sys.argv[1], Path(sys.argv[2]), applications)
        print("QT_SCENARIO_COMPLETE", code)
    finally:
        if applications:
            from PyQt6.QtCore import QCoreApplication, QEvent
            for widget in applications[0].topLevelWidgets():
                widget.hide()
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            applications[0].processEvents()
    raise SystemExit(code)
