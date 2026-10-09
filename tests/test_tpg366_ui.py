"""Full Explorer classes + real Qt, with only the serial instrument simulated.

Optional OS screenshot / Matplotlib widgets are not used by these probes.
Subprocess isolation avoids the older tests' global ESIBD/Qt module stubs.
"""
from pathlib import Path
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("acquire", "stop_during_read", "late_ready", "disconnect_failure", "read_failure", "nak_retransmitted", "transient_nak", "resync_unit_change",
         "simulation", "history", "settings", "reconnect", "render", "export", "pause", "queued_events", "statuses", "persistence", "pending_close", "pending_close_failure", "isolation", "negative", "global_action", "discovery", "port_names", "power_switch", "connection_diagnostics", "read_diagnostics", "open_diagnostics", "cards")


@pytest.mark.parametrize("case", CASES)
def test_real_explorer_pressure_plugin(case, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "XDG_CONFIG_HOME": str(tmp_path / "config")},
        capture_output=True, text=True, timeout=30)
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(case, output):
    import importlib.util
    import math
    import threading
    import time
    from types import ModuleType, SimpleNamespace

    import numpy as np
    try:
        from PyQt6 import QtCore, QtGui, QtWidgets
        import pyqtgraph as pg
    except ImportError:
        print("Real PyQt6 and pyqtgraph are required")
        return 77
    if importlib.util.find_spec("esibd") is None:
        print("Explorer is not installed")
        return 77
    # Some distro installations split out these optional dependencies. None is
    # involved in channel widgets, serial controllers, histories or pyqtgraph.
    if importlib.util.find_spec("matplotlib.backends.backend_qt") is None:
        backend = ModuleType("matplotlib.backends.backend_qt")
        backend.NavigationToolbar2QT = QtWidgets.QToolBar
        sys.modules[backend.__name__] = backend
        canvas = ModuleType("matplotlib.backends.backend_qtagg")
        canvas.FigureCanvasQTAgg = QtWidgets.QWidget
        sys.modules[canvas.__name__] = canvas
    sys.modules.setdefault("pyautogui", ModuleType("pyautogui"))
    from esibd import core, plugins
    from test_tpg366_protocol import FakeSerial

    app = QtWidgets.QApplication([])
    exceptions = []
    def report_exception(*args):
        import traceback
        exceptions.append(args)
        traceback.print_exception(*args)
    sys.excepthook = report_exception
    plugin_file = ROOT / "tpg366/tpg366_plugin.py"
    if case in {"isolation", "discovery"}:
        import shutil
        shutil.copytree(ROOT / "tpg366", Path(output) / "tpg366")
        plugin_file = Path(output) / "tpg366/tpg366_plugin.py"
    if case == "discovery":
        loaded, errors = [], []
        discovery = SimpleNamespace(
            loadPluginsFromModule=lambda **kw: loaded.append(kw["Module"]),
            logger=SimpleNamespace(print=lambda *a, **kw: errors.append((a, kw))),
            qm=SimpleNamespace(setText=lambda *a: None, setIcon=lambda *a: None,
                               open=lambda: None, raise_=lambda: None))
        core.PluginManager.loadPluginsFromPath(discovery, Path(output))
        assert not errors, errors
        assert len(loaded) == 1 and loaded[0].providePlugins()[0].name == "TPG366"
        return 0
    original_path = list(sys.path)
    spec = importlib.util.spec_from_file_location("tpg366_ui_probe", plugin_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert sys.path == original_path
    assert sys.modules["_esibd_bundled_tpg366"] is module._protocol
    if case == "isolation":
        protocol_file = plugin_file.parent / "_runtime" / "_tpg366.py"
        assert Path(module._protocol.__file__) == protocol_file
        assert module.providePlugins() == [module.TPG366]
        protocol_file.unlink()
        try:
            spec.loader.exec_module(importlib.util.module_from_spec(spec))
        except ModuleNotFoundError:
            return 0
        raise AssertionError("A cached module hid the missing bundled protocol")
    module.getTestMode = lambda: case == "simulation"
    # Windows power requests, simulated: held from ON until the port is confirmed closed.
    awake_calls = []
    module._protocol._power_api = lambda: SimpleNamespace(
        create=lambda reason: awake_calls.append(("create", reason)) or len(awake_calls),
        set=lambda handle, kind: awake_calls.append(("set", handle, kind)) or 1,
        clear=lambda handle, kind: awake_calls.append(("clear", handle, kind)) or 1,
        close=lambda handle: awake_calls.append(("close", handle)) or 1)
    logs = []
    settings = SimpleNamespace(configPath=Path(output), settings={}, loading=False, errorResetTime=10,
                               getFullSessionPath=lambda: Path(output))
    manager = SimpleNamespace(Device=plugins.Device, ChannelManager=plugins.ChannelManager, Scan=plugins.Scan,
                              SettingsManager=plugins.SettingsManager, Settings=settings,
                              DeviceManager=SimpleNamespace(globalUpdate=lambda **kw: None, stopScans=lambda: None,
                                                            channels=lambda **kw: device.channels,
                                                            updateStaticPlot=lambda: None,
                                                            getChannelByName=lambda *a, **kw: None),
                              logger=SimpleNamespace(print=lambda **kw: logs.append(kw)), loading=True, closing=False, testing=False,
                              Explorer=SimpleNamespace(populateTree=lambda: None),
                              Text=SimpleNamespace(setText=lambda *a, **kw: None),
                              getPluginsByClass=lambda *a: [], connectAllSources=lambda: None,
                              reconnectSource=lambda *a: None)
    import inspect
    kwargs = dict(pluginManager=manager, dependencyPath=ROOT / "tpg366")
    if "sourceCodePath" in inspect.signature(plugins.Plugin.__init__).parameters:
        kwargs["sourceCodePath"] = ROOT / "tpg366/tpg366_plugin.py"
    device = module.TPG366(**kwargs)
    for name, default in device.getDefaultSettings().items():
        if default.get(core.Parameter.ATTR):
            setattr(device, default[core.Parameter.ATTR], default[core.Parameter.VALUE])
        settings.settings[name] = SimpleNamespace(getWidget=lambda: None)
    device.com = "TEST-USB"
    device.interval = 100
    device.showLiveDisplay = False
    device.liveDisplayActive = lambda: False
    device.staticDisplayActive = lambda: False
    device.clearPlot = lambda: None
    device.measureInterval = lambda **kwargs: None
    device.initGUI()
    # The real device manager installs this global action during finalizeInit.
    # Use a real StateAction rather than a Boolean stub, without constructing the
    # unrelated global docks and all the host's other hardware plugins.
    device.onAction = device.addStateAction(event=lambda: device.setOn(on=None),
                                           toolTipFalse="TPG366 on", toolTipTrue="TPG366 off",
                                           iconFalse=device.getIcon(), iconTrue=device.getIcon(), restore=False)
    device._set_state("Disconnected", False)
    assert not device.loading
    manager.loading = False
    controller = device.controller
    ports, opened_names = [], []
    read_started, release = threading.Event(), threading.Event()

    def open_port(**kwargs):
        assert QtCore.QThread.currentThread() != app.thread(), "GUI opened the serial port"
        assert kwargs["baudrate"] == 9600 and kwargs["timeout"] == .05
        opened_names.append(kwargs["port"])
        if case == "open_diagnostics":
            raise OSError("USB port unavailable")
        port = FakeSerial(**kwargs)
        ports.append(port)
        if case == "connection_diagnostics":
            if len(ports) == 1:
                port.ack = b""  # Fail initialization before identification.
            else:
                port.before_ack = b"\n"  # Successful explicit reconnect despite a stale tail.
        if case == "read_diagnostics":
            def missing_ack(data):
                if data == b"PRX\r":
                    port.ack = b""
            port.on_write = missing_ack
        if case in {"disconnect_failure", "pending_close_failure"}:
            port.fail_close = True
        if case == "read_failure":
            port.responses["PRX"] = "garbage"
        if case in {"nak_retransmitted", "transient_nak", "resync_unit_change"}:
            # One NAK is retransmitted within the poll; three NAKs (every
            # transmission of the second poll) lose that sample.
            naks = {2} if case == "nak_retransmitted" else {2, 3, 4}
            def nak_prx(data):
                port.ack = b"\x06\r\n"
                if data == b"PRX\r" and port.writes.count(b"PRX\r") in naks:
                    port.ack = b"\x15\r\n"
                    if case == "resync_unit_change":
                        port.responses["UNI"] = "1"  # front panel changed during the outage
            port.on_write = nak_prx
        if case == "negative":
            from test_tpg366_protocol import frame
            port.responses["PRX"] = frame(values=["-1.0000E-03"] * 6)
        if case in {"stop_during_read", "late_ready"}:
            target = b"AYT\r" if case == "late_ready" else b"PRX\r"
            def hold(data):
                if data == target:
                    read_started.set()
                    assert release.wait(4)
            port.on_write = hold
        close = port.close
        def checked_close():
            assert QtCore.QThread.currentThread() != app.thread(), "GUI closed the serial port"
            assert not (read_started.is_set() and not release.is_set()), "Closed during a read"
            close()
        port.close = checked_close
        return port

    module.serial.Serial = open_port
    # Exercise widget-backed settings as well as real Channel.Parameter wrappers.
    label = QtWidgets.QLabel()
    def state_setter(self, value):
        assert QtCore.QThread.currentThread() == app.thread(), "Worker mutated GUI setting"
        label.setText(value)
    module.TPG366.main_state = property(lambda self: label.text(), state_setter)
    def wait(condition, seconds=4):
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.003)
        app.processEvents()
        assert not exceptions, repr(exceptions)
        assert condition(), (device.main_state, logs)

    saved_colors = [ch.color for ch in device.channels]
    def assert_channel_colors(acquiring):
        for channel, saved_color in zip(device.channels, saved_colors):
            # Appearance must never overwrite the persistent curve colour.
            assert channel.color == channel.asDict()[channel.COLOR] == saved_color
            if acquiring and channel.enabled:
                expected = QtGui.QColor(saved_color)
                if core.getDarkMode():
                    expected = expected.darker(150 if channel.active else 200)
                elif not channel.active:
                    expected = expected.darker(115)
                assert channel.background(1).color() == expected
                assert channel.background(1).style() == QtCore.Qt.BrushStyle.SolidPattern
            else:
                for index in range(len(channel.parameters) + 1):
                    assert channel.background(index).style() == QtCore.Qt.BrushStyle.NoBrush, channel.name
                assert channel.defaultStyleSheet == ""
                for parameter in channel.parameters:
                    widget = parameter.getWidget()
                    if widget is not None:
                        assert widget.styleSheet() == "", (channel.name, parameter.name, widget.styleSheet())
                        container = getattr(widget, "container", None)
                        if container is not None:
                            assert container.styleSheet() == "", (channel.name, parameter.name, container.styleSheet())

    try:
        assert len(device.channels) == 6
        assert_channel_colors(False)
        assert [int(ch.input) for ch in device.channels] == list(range(1, 7))
        assert all(ch.getParameterByName(ch.VALUE).indicator for ch in device.channels)
        assert all(ch.values.max_size == device.time.max_size > 0 for ch in device.channels)
        if case == "settings":
            default = device.getDefaultSettings()
            assert default["TPG366/Interval"][core.Parameter.VALUE] == 1000
            device.com = ""
            device.setOn(True)
            assert "COM port" in device.main_state and not ports and not device.initialized
            device.toggleRecording(on=True)
            assert not device.recording
            return 0
        if case == "port_names":
            # The Windows field log showed Serial trying the literal path '21'.
            # CGC plugins accept port numbers; accept those without breaking
            # existing full names, Windows device paths or Linux serial paths.
            for configured, expected in (("21", "COM21"), (21, "COM21"), (" 21 ", "COM21"),
                                         ("COM21", "COM21"), ("com21", "COM21"), ("COM003", "COM3"),
                                         (r"\\.\COM21", r"\\.\COM21"), ("/dev/ttyUSB0", "/dev/ttyUSB0"),
                                         ("TEST-USB", "TEST-USB")):
                device.com = configured
                device.deviceOnAction.trigger()
                wait(lambda: all(math.isfinite(ch.value) for ch in device.channels))
                assert opened_names[-1] == expected, (configured, opened_names[-1], expected)
                assert device.isOn() and device.recording
                device.deviceOnAction.trigger()
                wait(lambda: not device.initialized and not controller._worker.is_alive())
                assert ports[-1].close_count == 1 and not ports[-1].is_open
                assert not device.isOn() and not device.deviceOnAction.state
            return 0
        if case == "power_switch":
            from PyQt6.QtTest import QTest
            action = device.deviceOnAction
            for icon, filename in ((action.iconFalse, "switch-medium_on.png"),
                                   (action.iconTrue, "switch-medium_off.png")):
                assert Path(icon.fileName) == ROOT / "tpg366" / filename, icon.fileName
                assert (ROOT / "tpg366" / filename).read_bytes() == (ROOT / "dmmr" / filename).read_bytes()
                assert not icon.isNull()
            # The global action belongs in DeviceManager's toolbar, not here.
            device.titleBar.removeAction(device.onAction)
            window = QtWidgets.QWidget()
            window.setWindowTitle("TPG366")
            layout = QtWidgets.QVBoxLayout(window)
            layout.addWidget(device.titleBar)
            layout.addWidget(device)
            window.resize(760, 260)
            window.show()
            app.processEvents()
            button = device.titleBar.widgetForAction(action)
            assert isinstance(button, QtWidgets.QToolButton) and button.isVisible()

            def snapshot(filename, on):
                app.processEvents()
                assert button.isChecked() == action.state == device.isOn() == on
                assert button.toolTip() == (action.toolTipTrue if on else action.toolTipFalse)
                assert button.icon().pixmap(16, 16).toImage() == action.getIcon().pixmap(16, 16).toImage()
                assert_channel_colors(on)
                assert window.grab().save(str(Path(output) / filename))

            snapshot("switch-off.png", False)
            # Synchronizing QAction state updates appearance, not hardware.
            action.state = True
            action.state = False
            assert not ports
            QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
            wait(lambda: all(math.isfinite(ch.value) for ch in device.channels))
            assert device.recording and len(ports) == 1
            snapshot("switch-on.png", True)
            first = device.channels[0]
            first.enabled = False
            snapshot("switch-channel-off.png", True)
            first.enabled = True
            wait(lambda: math.isfinite(first.value))
            assert_channel_colors(True)
            QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
            assert_channel_colors(False)  # Immediately neutral, even before USB close completes.
            wait(lambda: not device.initialized and not controller._worker.is_alive())
            assert not device.recording and ports[0].close_count == 1
            assert all(math.isnan(ch.value) for ch in device.channels)
            snapshot("switch-disconnected.png", False)
            for channel in device.channels:
                channel.updateColor()  # A theme/appearance refresh while OFF stays neutral.
            assert_channel_colors(False)
            first.enabled = False
            first.enabled = True
            assert_channel_colors(False)
            QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
            wait(lambda: len(ports) == 2 and all(math.isfinite(ch.value) for ch in device.channels))
            assert device.isOn() and device.recording
            snapshot("switch-reconnected.png", True)
            QTest.mouseClick(button, QtCore.Qt.MouseButton.LeftButton)
            wait(lambda: not device.initialized and not controller._worker.is_alive())
            assert ports[1].close_count == 1 and not device.isOn()
            assert_channel_colors(False)
            window.hide()
            return 0
        (device.onAction if case == "global_action" else device.deviceOnAction).trigger()
        if case == "queued_events":
            # Let initialization and pressure signals queue while Qt is blocked.
            deadline = time.monotonic() + 3
            while (not ports or ports[0].writes.count(b"UNI\r") < 2) and time.monotonic() < deadline:
                time.sleep(.005)
            assert ports and ports[0].writes.count(b"UNI\r") >= 2
            before = device.time.size
            device.setOn(False)
            wait(lambda: not device.initialized)
            assert device.time.size == before
            assert not device.isOn() and all(math.isnan(ch.value) for ch in device.channels)
            old_generation = controller._generation
            wait(lambda: not controller._worker.is_alive())
            device.setOn(True)
            controller._receive((old_generation, "finished", (None, "old error", "")))
            assert device.initialized
            return 0
        if case in {"stop_during_read", "late_ready"}:
            wait(read_started.is_set)
            assert device.initialized  # Explorer must see initialization in progress.
            device.deviceOnAction.trigger()
            assert controller._stop.is_set()
            assert_channel_colors(False)
            assert not ports[0].close_count
            release.set()
            wait(lambda: not device.initialized)
            assert ports[0].close_count == 1
            assert all(math.isnan(ch.value) for ch in device.channels)
            assert not device.isOn()
            return 0
        if case in {"connection_diagnostics", "read_diagnostics", "open_diagnostics"}:
            # A persistent read fault stops after the bounded resynchronization attempts.
            wait(lambda: "error — disconnected" in device.main_state, 10)
            text = "\n".join(str(entry) for entry in logs)
            assert "ON requested" in text and "TEST-USB" in text and "9600 baud" in text and "8N1" in text, text
            assert "OFF requested" not in text, text  # A spontaneous error is not a user OFF.
            if case == "open_diagnostics":
                assert "opening USB port" in text and "USB port unavailable" in text, text
                assert not ports
            else:
                phase, command = ("initialization", "AYT") if case == "connection_diagnostics" else ("pressure acquisition", "PRX")
                assert phase in text and f"{command} [waiting for ACK]" in text, text
                assert "partial reply: b''" in text, text
                assert ports[0].close_count == 1 and not ports[0].is_open
            assert not device.initialized and not device.isOn() and not device.recording
            assert_channel_colors(False)
            if case == "connection_diagnostics":
                wait(lambda: not controller._worker.is_alive())
                device.deviceOnAction.trigger()
                wait(lambda: len(ports) == 2 and all(math.isfinite(ch.value) for ch in device.channels))
                device.deviceOnAction.trigger()
                wait(lambda: not device.initialized and not controller._worker.is_alive())
                controller.closeCommunication()  # Repeated close must not duplicate OFF logs.
                text = "\n".join(str(entry) for entry in logs)
                assert text.count("ON requested") == 2 and text.count("OFF requested") == 1, text
            Path(output, "diagnostic-log.txt").write_text(text, encoding="utf-8")
            return 0
        if case == "read_failure":
            wait(lambda: "error — disconnected" in device.main_state)
            text = "\n".join(str(entry) for entry in logs)
            assert text.count("Pressure sample missed") == module._READ_FAILURE_LIMIT - 1, text
            assert ports[0].writes.count(b"PRX\r") == module._READ_FAILURE_LIMIT
            assert all(math.isnan(ch.value) for ch in device.channels)
            assert not device.recording and not device.isOn() and not device.initialized
            assert_channel_colors(False)
            assert ports[0].close_count == 1
            return 0
        if case == "resync_unit_change":
            wait(lambda: "error — disconnected" in device.main_state, 8)
            text = "\n".join(str(entry) for entry in logs)
            assert "Pressure unit changed during resynchronization" in text, text
            assert ports[0].writes.count(b"PRX\r") == 4, "no frame may be read in the new unit"
            assert not device.initialized and ports[0].close_count == 1
            return 0
        if case == "nak_retransmitted":
            wait(lambda: bool(ports) and ports[0].writes.count(b"PRX\r") >= 4 and all(math.isfinite(ch.value) for ch in device.channels), 8)
            text = "\n".join(str(entry) for entry in logs)
            assert "answered NAK 1x during pressure acquisition" in text and "retransmitted" in text, text
            assert "Pressure sample missed" not in text, text
            assert ports[0].writes.count(b"AYT\r") == 1 and device.main_state == "Acquiring"
            return 0
        if case == "transient_nak":
            wait(lambda: bool(ports) and ports[0].writes.count(b"PRX\r") >= 5 and all(math.isfinite(ch.value) for ch in device.channels), 8)
            text = "\n".join(str(entry) for entry in logs)
            assert "Pressure sample missed" in text and "PRX [waiting for ACK]" in text and "3 transmissions" in text, text
            assert "recovered after 1 failed" in text, text
            assert device.main_state == "Acquiring" and device.initialized and device.recording
            assert len(ports) == 1 and ports[0].close_count == 0
            assert ports[0].writes.count(b"AYT\r") == 2  # one initial, one resynchronization
            times = device.time.get()
            assert np.all(np.diff(times) > 0), times.tolist()
            for channel in device.channels:
                history = channel.values.get()
                assert np.isnan(history).any(), "the lost packet must be recorded as a gap"
                assert np.isfinite(history[-1])
            return 0
        wait(lambda: all(math.isfinite(ch.value) for ch in device.channels))
        if case == "simulation":
            assert not ports and "Simulation" in device.main_state
            assert controller._awake is None and not awake_calls  # No hardware: no keep-awake.
        else:
            assert device.identification.startswith("TPG366,")
            assert len(ports) == 1
            assert controller._awake.held and awake_calls == [
                ("create", "ESIBD Explorer: TPG366 on TEST-USB is connected"), ("set", 1, 1)]
        if case in {"acquire", "reconnect", "render"}:
            first = device.channels[0]
            first.name = "Pressure chamber"
            first.input = 2
            assert first.gauge == "IKR" and math.isnan(first.value)
            first.input = 1
            assert first.gauge == "TPR/PCR"
            first.enabled = False
            assert math.isnan(first.value) and first.status == "Excluded"
            wait(lambda: device.time.size >= 4)
            assert math.isnan(first.values.get()[-1])
            first.enabled = True
            wait(lambda: math.isfinite(first.value))
        if case == "negative":
            assert [ch.value for ch in device.channels] == [-.001] * 6
            wait(lambda: all(ch.values.size > 1 for ch in device.channels))
            np.testing.assert_allclose([ch.values.get()[-1] for ch in device.channels], [-.001] * 6, rtol=1e-6)
        if case == "global_action":
            device.onAction.trigger()
            wait(lambda: not device.initialized)
            assert not device.deviceOnAction.state
            return 0
        if case.startswith("pending_close"):
            generation = controller._generation
            device.setOn(False)
            controller._worker.join(2)  # Finished signal has not yet reached Qt.
            assert not controller._worker.is_alive()
            device.setOn(False)
            assert device.initialized and device.main_state == "Disconnecting"
            device.setOn(True)
            assert controller._generation == generation and len(ports) == 1
            if case == "pending_close_failure":
                wait(lambda: "unconfirmed" in device.main_state)
                assert device.initialized and controller._retained_port is ports[0]
                ports[0].fail_close = False
                device.setOn(False)
            wait(lambda: not device.initialized)
            return 0
        if case == "statuses":
            from test_tpg366_protocol import frame
            ports[0].responses["PRX"] = frame(statuses=(1, 2, 3, 4, 5, 6))
            wait(lambda: device.channels[5].status == "Identification error")
            assert all(math.isnan(ch.value) for ch in device.channels)
            assert [ch.status for ch in device.channels] == list(module._protocol.STATUS[1:])
        if case == "pause":
            device.toggleRecording(on=False)
            count = device.time.size
            polls = ports[0].writes.count(b"PRX\r")
            wait(lambda: ports[0].writes.count(b"PRX\r") > polls + 1)
            assert device.time.size == count and device.initialized and ports[0].is_open
            device.toggleRecording(on=True)
            wait(lambda: device.time.size > count + 1)
        if case == "history":
            # Stop the worker and feed distinct real samples through the same Qt
            # slot; assert host DynamicNp histories stay aligned and unthinned.
            device.setOn(False)
            wait(lambda: not device.initialized)
            controller._stop = threading.Event()
            device.recording = True
            received_at = time.time()
            for i in range(1000):
                reading = module._protocol.Reading(tuple((i + j + 1) * 1e-6 for j in range(6)), (0,) * 6, "mbar", received_at + i)
                controller._receive((controller._generation, "sample", reading))
            n = device.time.size
            assert n >= 1000
            np.testing.assert_array_equal(device.time.get()[-1000:], received_at + np.arange(1000))
            for j, channel in enumerate(device.channels):
                assert channel.values.size == n
                np.testing.assert_allclose(channel.values.get()[-1000:], (np.arange(1000) + j + 1) * 1e-6, rtol=1e-6)
            old_time = device.time.get().copy()
            old_capacity = device.time.max_size
            device.maxStorage = 5
            device.estimateStorage()
            assert device.time.max_size == old_capacity
            np.testing.assert_array_equal(device.time.get(), old_time)
        if case == "cards":
            device.resize(720, 450)
            device.show()
            app.processEvents()
            panel = device._pressure_panel
            assert device.tree.isHidden() and len(panel.cards) == 6
            assert all(panel.cards[id(channel)][3].text() == f"{channel.value:.2e}" for channel in device.channels)
            write = ports[0].write
            def worker_only_write(data):
                assert QtCore.QThread.currentThread() != app.thread(), "Card issued serial I/O"
                return write(data)
            ports[0].write = worker_only_write
            first = device.channels[0]
            editor = panel.cards[id(first)][2]
            editor.setText("Sample chamber")
            editor.editingFinished.emit()
            assert first.name == "Sample chamber"
            panel.cards[id(device.channels[1])][6].click()
            panel.cards[id(device.channels[2])][7].click()
            assert not device.channels[1].enabled and not device.channels[2].display
            assert panel.cards[id(device.channels[1])][3].text() == "—"
            controller.gauges = tuple(f"Type {index+1}" for index in range(6))
            device.exportConfiguration(useDefaultFile=True)
            device.loadConfiguration(useDefaultFile=True)
            assert len(panel.cards) == 6 and device.channels[0].name == "Sample chamber"
            assert not device.channels[1].enabled and not device.channels[2].display
            assert [int(channel.input) for channel in device.channels] == list(range(1, 7))
            wait(lambda: math.isfinite(device.channels[0].value))
            assert [channel.gauge for channel in device.channels] == list(controller.gauges)
            app.processEvents()
            assert device.grab().save(str(Path(output) / "tpg366-cards.png"))
            wait(lambda: device.time.size >= 6)
            plot = core.PlotWidget(parentPlugin=None, groupLabel="TPG366")
            plot.init()
            plot.finalizeInit()
            live = device.liveDisplay
            live.livePlotWidgets = [plot]
            def draw(**kwargs):
                live.plotGroup(plot, {device.name: (0, None, 1, device.time.get())}, device.channels, True)
            live.plot = draw
            live.recordingAction = SimpleNamespace(state=device.recording)
            device.liveDisplayActive = lambda: True
            draw()
            first = device.channels[0]
            assert first.plotCurve.name() == "Sample chamber (mbar)"
            device.toggleRecording(on=False)
            timeline, values = device.time.get().copy(), first.values.get().copy()
            editor = panel.cards[id(first)][2]
            editor.setText("Pressure cell")
            editor.editingFinished.emit()
            assert first.plotCurve.name() == "Pressure cell (mbar)"
            np.testing.assert_array_equal(device.time.get(), timeline)
            np.testing.assert_array_equal(first.values.get(), values)
            device.toggleRecording(on=True)
            editor.setText("Beamline pressure")
            editor.editingFinished.emit()
            wait(lambda: first.plotCurve is not None)
            assert first.plotCurve.name() == "Beamline pressure (mbar)"
        if case in {"render", "export"}:
            wait(lambda: device.time.size >= 6 and all(np.isfinite(ch.values.get()[-2:]).all() for ch in device.channels))
            plot = core.PlotWidget(parentPlugin=None, groupLabel="TPG366")
            plot.init()
            plot.finalizeInit()
            plot.setLogMode(y=True)
            device.liveDisplay.livePlotWidgets = [plot]
            timeline = device.time.get().copy()
            device.liveDisplay.plotGroup(plot, {device.name: (0, None, 1, timeline)}, device.channels, True)
            for channel in device.channels:
                x, y = channel.plotCurve.getData()
                np.testing.assert_allclose(y, np.log10(channel.values.get()), rtol=1e-6)
                np.testing.assert_array_equal(x, timeline)
            if case == "export":
                import h5py
                with h5py.File(Path(output) / "pressures.h5", "w") as saved:
                    device.appendOutputData(saved, True)
                    np.testing.assert_array_equal(saved["TPG366/Input Channels/Time"][:], timeline)
                    for channel in device.channels:
                        dataset = saved[f"TPG366/Output Channels/{channel.name}"]
                        np.testing.assert_array_equal(dataset[:], channel.values.get())
                        assert dataset.attrs["Unit"] == "mbar"
            else:
                window = QtWidgets.QWidget()
                layout = QtWidgets.QVBoxLayout(window)
                layout.addWidget(device)
                layout.addWidget(plot)
                layout.setStretch(0, 2)
                layout.setStretch(1, 3)
                window.resize(760, 660)
                window.show()
                app.processEvents()
                assert window.grab().save(str(Path(output) / "tpg366.png"))
        device.setOn(False)
        if case == "disconnect_failure":
            wait(lambda: "unconfirmed" in device.main_state)
            assert device.initialized and device.isOn()
            assert controller._retained_port is ports[0]
            wait(lambda: not controller._worker.is_alive())
            assert controller._awake.held  # The port may still be open: keep the PC awake.
            ports[0].fail_close = False
            device.deviceOnAction.trigger()
        wait(lambda: not device.initialized and not device.isOn())
        if case != "simulation":
            assert not controller._awake.held and awake_calls[-2:] == [("clear", 1, 1), ("close", 1)]
        assert all(math.isnan(ch.value) for ch in device.channels)
        if case == "persistence":
            device.channels[0].name = "Sample chamber"
            device.channels[1].enabled = False
            device.channels[2].display = False
            device.exportConfiguration(useDefaultFile=True)
            device.loadConfiguration(useDefaultFile=True)
            assert len(device.channels) == 6
            assert device.channels[0].name == "Sample chamber"
            assert not device.channels[1].enabled and not device.channels[2].display
            assert all(math.isnan(ch.value) for ch in device.channels)
            assert [int(ch.input) for ch in device.channels] == list(range(1, 7))
        if case == "reconnect":
            wait(lambda: not controller._worker.is_alive())
            device.deviceOnAction.trigger()
            wait(lambda: len(ports) == 2 and math.isfinite(device.channels[0].value))
            device.setOn(False)
            wait(lambda: not device.initialized)
            assert [port.close_count for port in ports] == [1, 1]
        return 0
    finally:
        release.set()
        controller.closeCommunication()
        if controller._worker:
            controller._worker.join(2)
        app.processEvents()
        assert not exceptions, repr(exceptions)


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], sys.argv[2]))
