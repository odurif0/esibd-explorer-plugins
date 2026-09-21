"""Real Explorer table/Qt signals: readbacks persist from ramp-up through OFF."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
@pytest.mark.parametrize("scale", ["1", "1.5"])
def test_live_status_and_monitor_during_on_and_off(folder, scale, tmp_path):
    result = subprocess.run(
        [sys.executable, __file__, folder, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
        capture_output=True, text=True, timeout=35,
    )
    if result.returncode == 77:
        pytest.skip("Installed Explorer and Qt are required")
    assert result.returncode == 0, result.stdout + result.stderr


def exercise(module, parent, app, window, output):
    import ast
    from importlib.metadata import distribution
    import json
    import threading
    import time
    from types import MethodType
    from PyQt6 import QtCore
    from PyQt6.QtTest import QTest
    import numpy as np

    c = parent.controller
    parent.loading = True
    for ch, target in zip(parent.channels, (10., 20.)):
        ch.enabled = True
        ch.value = target
        ch.ramp_rate_v_s = 10.
    # Real startup begins with saved, unapplied targets, not an artificial
    # "confirmed" feedback state. No device is connected at this point.
    for ch in parent.channels:
        c._queue_setpoint(ch)
        assert ch._ampr_setpoint_state == "stored"
    parent.channels[1].name = "Second_HV"
    parent.loading = False
    for ch in parent.channels:
        ch._sync_enabled_toggle_widget()
    parent.toggleAdvanced(False)
    parent.connect_timeout_s = parent.startup_timeout_s = 2.
    module.DeviceController.toggleOn = lambda self: None
    c.lock = threading.Lock()
    c._refresh_module_scan = lambda: None
    # Use Explorer's actual poller startup, not a flag-only stub: the normal
    # AMPR runAcquisition thread must take over when the transition finishes.
    core_path = Path(distribution("esibd-explorer").locate_file("esibd/core.py"))
    base = next(node for node in ast.parse(core_path.read_text()).body
                if isinstance(node, ast.ClassDef) and node.name == "DeviceController")
    start = next(node for node in base.body if isinstance(node, ast.FunctionDef)
                 and node.name == "startAcquisition")
    namespace = {"Thread": threading.Thread, "PRINT": module.PRINT}
    exec(compile(ast.Module(body=[start], type_ignores=[]), str(core_path), "exec"), namespace)
    c.startAcquisition = MethodType(namespace["startAcquisition"], c)
    c.acquisitionThread = None

    def stop_acquisition():
        c.acquiring = False
        if c.acquisitionThread is not None:
            c.acquisitionThread.join(3)
            assert not c.acquisitionThread.is_alive(), "Poller did not stop before ramp-down"

    c.stopAcquisition = stop_acquisition
    c.print = lambda *args, **kwargs: None
    samples, writes, events, failures = [], [], [], []
    phase = "up"
    channels = parent.channels

    class Hardware:
        NO_ERR = 0
        connected = True
        state = "ST_ON"

        def __init__(self):
            self.accepted = {ch.channel_number(): 0. for ch in channels}
            self.offsets = {ch.channel_number(): .05 for ch in channels}

        def initialize(self, **kwargs):
            self.state = "ST_ON"

        def get_state(self):
            assert c.lock.locked()
            return (0, 0, self.state)

        def set_module_voltages(self, address, values):
            assert c.lock.locked()
            assert threading.current_thread() is not threading.main_thread()
            self.accepted.update(values)
            writes.append((time.monotonic(), dict(values)))
            return dict.fromkeys(values, 0)

        def get_module_voltages(self, address):
            assert c.lock.locked()
            return {number: {"setpoint": target, "measured": target + self.offsets[number]}
                    for number, target in self.accepted.items()}

        def shutdown(self):
            assert c.lock.locked()
            assert all(value == 0. for value in self.accepted.values())
            assert samples[-1]["phase"] == "down"  # Already rendered during the descent.
            events.append("shutdown")
            self.connected = False
            self.state = "ST_STBY"
            return True

        def close(self):
            assert not self.connected, "Do not release an unconfirmed output"
            events.append("close")

    class Bridge(QtCore.QObject):
        updateValuesSignal = QtCore.pyqtSignal()

        def __init__(self):
            super().__init__()
            self.updateValuesSignal.connect(self.publish)

        @QtCore.pyqtSlot()
        def publish(self):
            try:
                assert QtCore.QThread.currentThread() == app.thread()
                c.updateValues()
                styles = [ch.getParameterByName(ch.ENABLED).getWidget().styleSheet() for ch in channels]
                values = [ch.monitor for ch in channels]
                if not c.initialized:
                    return  # A final queued sample must not repaint a disconnected device.
                assert np.isfinite(values).all(), (phase, values)
                for ch in channels:
                    assert ch.getParameterByName(ch.MONITOR).getWidget().styleSheet() == ""
                    assert ch.getParameterByName(ch.ENABLED).getWidget().isCheckable()
                samples.append({"phase": phase, "time": time.monotonic(), "values": values, "styles": styles})
                # Mid-ramp captures show two channels advancing together, with
                # one finishing before the other (rather than two sequential ramps).
                if sum(sample["phase"] == phase for sample in samples) == 2:
                    window.grab().save(str(output / f"{parent.name}-{phase}-live.png"))
            except BaseException as exc:
                failures.append(exc)

    bridge = Bridge()
    c.signalComm = bridge
    c.device = Hardware()
    c.initialized = True
    c.detected_module_ids = [0]
    c.main_state = "ST_ON"
    parent.onAction.state = True
    c.acquiring = False
    c.initializeValues(reset=True)

    for phase in ("up", "down", "up", "down"):
        if phase == "up" and c.device is None:
            assert all(ch._ampr_setpoint_state == "stored" for ch in channels)
            c.device = Hardware()  # Reconnect without reloading or editing the saved targets.
            c.initialized = True
            c.main_state = "ST_ON"
            c.initializeValues(reset=True)
        sample_start = len(samples)
        parent.onAction.state = phase == "up"
        assert c._begin_transition(phase == "up")
        # Confirm that the stabilization window does not suppress live ramps.
        for ch in channels:
            ch.waitToStabilize = True

        def worker():
            try:
                c.toggleOn()
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        deadline = time.monotonic() + 10.
        while thread.is_alive() and time.monotonic() < deadline:
            QTest.qWait(20)
        thread.join(.1)
        assert not thread.is_alive(), "Transition did not terminate"
        app.processEvents()
        assert not failures, failures
        frames = [sample for sample in samples[sample_start:] if sample["phase"] == phase]
        assert len(frames) >= 2, frames
        assert .75 <= frames[1]["time"] - frames[0]["time"] <= 1.35, frames
        first, mid = frames[0], frames[1]
        if phase == "up":
            assert mid["values"][0] > first["values"][0]
            assert mid["values"][1] > first["values"][1]
            assert mid["values"][0] == pytest.approx(10.05, abs=.15)
            assert mid["values"][1] == pytest.approx(10.05, abs=.3)
            assert "#2f855a" in mid["styles"][0]
            assert "#c53030" in mid["styles"][1]
            # The normal poller must take over after the last ramp finishes;
            # keep checking real samples, not just the last in-ramp frame.
            assert not c.transitioning and not c.ramping and c.acquiring
            phase = "stable"
            count_writes = len(writes)
            for offsets, colours, name in [
                ((.05, .05), ("#2f855a", "#2f855a"), "stable"),
                ((.5, 5.), ("#dd6b20", "#c53030"), "stable-deviation"),
                ((.05, .05), ("#2f855a", "#2f855a"), "stable-recovered"),
            ]:
                with c.lock:
                    c.device.offsets = dict(zip((1, 2), offsets, strict=True))
                # Wait for two new signals so an older queued frame cannot
                # masquerade as a fresh normal-poller readback.
                count = len(samples)
                deadline = time.monotonic() + 4.
                while len(samples) < count + 2 and time.monotonic() < deadline:
                    QTest.qWait(20)
                assert len(samples) >= count + 2, "Normal polling stopped at the end of the ramp"
                assert not failures, failures
                assert all(ch._ampr_setpoint_state == "confirmed" for ch in channels), [
                    ch._ampr_setpoint_state for ch in channels]
                for ch, colour in zip(channels, colours, strict=True):
                    assert colour in ch.getParameterByName(ch.ENABLED).getWidget().styleSheet(), (
                        "Status lost its tracking colour after ramp-up", ch._ampr_setpoint_state)
                assert [ch.monitor for ch in channels] == pytest.approx([10. + offsets[0], 20. + offsets[1]])
                assert len(writes) == count_writes, "Confirmation must not replay startup voltages"
                window.grab().save(str(output / f"{parent.name}-{name}.png"))
            stable_frames = [sample for sample in samples[sample_start:] if sample["phase"] == "stable"]
            assert all(.75 <= b["time"] - a["time"] <= 1.35
                       for a, b in zip(stable_frames, stable_frames[1:]))
        else:
            assert mid["values"][0] < first["values"][0]
            assert mid["values"][1] < first["values"][1]
            assert mid["values"][0] == pytest.approx(.05, abs=.15)
            assert mid["values"][1] == pytest.approx(10.05, abs=.3)
            assert "#dd6b20" in mid["styles"][0]  # .05 V against zero: 5% of the 1 V floor.
            assert "#c53030" in mid["styles"][1]

    assert events == ["shutdown", "close"] * 2
    assert c.main_state == "Disconnected" and not c.initialized
    for ch in channels:
        assert ch.background(0).style() == QtCore.Qt.BrushStyle.NoBrush
        assert ch.getParameterByName(ch.ENABLED).getWidget().styleSheet() == ""
        assert ch.getParameterByName(ch.MONITOR).getWidget().styleSheet() == ""
        assert ch.enabled, "The configured channel selections must be kept"
    window.grab().save(str(output / f"{parent.name}-off.png"))
    (output / f"{parent.name}-samples.json").write_text(json.dumps(samples, indent=2))


if __name__ == "__main__":
    from test_ampr_startup_ui import probe
    raise SystemExit(probe(sys.argv[1], False, Path(sys.argv[2]), exercise=exercise))
