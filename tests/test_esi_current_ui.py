"""ESI current widgets, plots and HDF records using real Explorer methods.

Subprocess isolation avoids the rest of the suite's Qt/ESIBD stubs. No hardware,
user settings or full Explorer application is opened.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CASES = ("widgets", "panel", "groups", "horizontal", "vertical", "stacked", "constant", "manual", "log", "recording", "off", "stopping", "unconfirmed")


@pytest.mark.parametrize("case", CASES)
def test_current_in_explorer(case, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), case, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=35,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt/pyqtgraph or Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(case, output):
    import re
    import threading
    import time
    import types
    from enum import Enum
    from importlib.metadata import PackageNotFoundError, distribution
    from types import SimpleNamespace

    import numpy as np

    try:
        import h5py
        import pyqtgraph as pg
        from PyQt6 import QtCore, QtGui, QtWidgets
        from PyQt6.QtTest import QTest
        host = Path(distribution("esibd-explorer").locate_file("esibd"))
    except (ImportError, PackageNotFoundError):
        return 77
    if not (host / "core.py").is_file():
        return 77

    app = QtWidgets.QApplication([])
    ns = dict(np=np, re=re, pg=pg, h5py=h5py, Path=Path, time=time,
              Thread=threading.Thread, cast=lambda _, value: value,
              getTestMode=lambda: False, pyqtSignal=QtCore.pyqtSignal,
              INOUT=Enum("INOUT", "IN OUT NONE"),
              PARAMETERTYPE=Enum("PARAMETERTYPE", "COMBO INTCOMBO FLOATCOMBO TEXT INT FLOAT EXP BOOL COLOR LABEL PATH"),
              PRINT=SimpleNamespace(DEBUG=0, ERROR=1, WARNING=2, VERBOSE=3),
              colors=SimpleNamespace(fg="#202020", bg="#ffffff"))
    for qt in (QtCore, QtGui, QtWidgets):
        ns.update({name: getattr(qt, name) for name in dir(qt) if name.startswith("Q")})
    ns["Qt"] = QtCore.Qt
    trees = {name: ast.parse((host / name).read_text()) for name in ("core.py", "plugins.py", "const.py")}

    def extract(file, name, parent=None):
        nodes = trees[file].body
        if parent:
            nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == parent).body
        node = next(n for n in nodes if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name)
        code = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(code), str(host / file), "exec"), ns)
        return ns[name]

    from test_esi_plugin_behavior import _install_esibd_stubs
    _install_esibd_stubs()
    core, plugins = sys.modules["esibd.core"], sys.modules["esibd.plugins"]
    ns["Channel"], ns["Device"] = core.Channel, plugins.Device
    ns["Setting"] = type("Setting", (), {})
    core.PARAMETERTYPE = ns["PARAMETERTYPE"]
    # plot() also accepts output-only devices; this loader only models inputs.
    ns["PLUGINTYPE"] = SimpleNamespace(INPUTDEVICE=core.PLUGINTYPE.INPUTDEVICE, OUTPUTDEVICE=object())
    for name in ("ParameterWidget", "Label", "LabviewSpinBox", "LabviewDoubleSpinBox", "LabviewSciSpinBox", "Parameter", "DynamicNp"):
        extract("core.py", name)
    core.Parameter = ns["Parameter"]
    for name in ("ViewBox", "LabelItem", "SciAxisItem", "PlotItem", "PlotWidget"):
        extract("core.py", name)
    ns["PlotDataItem"] = pg.PlotDataItem
    plugins.LiveDisplay = type("HostLiveDisplay", (), {
        name: extract("plugins.py", name, "LiveDisplay")
        for name in ("getGroups", "initFig", "updateStackedViews", "updateMouseEnabled", "plot", "plotGroup", "plotChannel")
    })
    constants = types.ModuleType("esibd.const")
    for node in trees["const.py"].body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("UNIT", "INPUTCHANNELS", "OUTPUTCHANNELS"):
                    ns[target.id] = ast.literal_eval(node.value)
                    setattr(constants, target.id, ns[target.id])
    sys.modules["esibd.const"] = constants
    for name in ("appendOutputData", "appendData"):
        setattr(plugins.Device, name, extract("plugins.py", name, "Device"))
    core.Channel.appendValue = extract("core.py", "appendValue", "Channel")
    spec = importlib.util.spec_from_file_location("esi_current_ui_probe", ROOT / "esi/esi_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    from test_esi_current_measurements import channel_for, snapshot
    device = module.ESIDevice.__new__(module.ESIDevice)
    device.loading = True
    device.plotting = False
    device.channels = [channel_for(module, 1), channel_for(module, 2), channel_for(module, 0),
                       channel_for(module, 1, current=True), channel_for(module, 2, current=True)]
    device.channels[2].name = "ESI_HEAT"
    device.getChannels = lambda: device.channels
    device.getDataChannels = device.getChannels
    device.getActiveChannels = device.getChannels
    device.isOn = lambda: True
    device.subtractBackgroundActive = lambda: False
    device.print = lambda *args, **kw: None
    device.useBackgrounds = False
    device.requireGroup = lambda target, name: target.require_group(name)
    device.TIME = "Time"
    device.time = ns["DynamicNp"](dtype=np.float64, max_size=100_000)
    controller = module.ESIController(device)
    device.controller = controller
    controller.print = device.print
    controller._sync_status = lambda: None
    controller.initialized = controller.acquiring = True
    controller.errorCount = 0
    for index, channel in enumerate(device.channels):
        channel.channelParent = device
        channel.display = channel.active = True
        channel.inout = ns["INOUT"].IN
        channel.useMonitors, channel.useBackgrounds = True, False
        channel.waitToStabilize = False
        channel.values = ns["DynamicNp"](max_size=100_000)
        channel.time = device.time
        channel.displayGroup = "ESI"
        channel.getDevice = lambda: device
        channel.convertDataDisplay = lambda data: data
        channel.getValues = lambda *args, ch=channel, **kw: ch.values.get()
        channel.getQtLineStyle = lambda: QtCore.Qt.PenStyle.SolidLine
        channel.smooth, channel.linewidth, channel.logY = 0, 2, False
        channel.plotCurve = None
        channel.color = ("#3182ce", "#dd6b20", "#319795", "#3182ce", "#dd6b20")[index]

        def clear(ch=channel):
            if ch.plotCurve is not None:
                ch.plotCurve.curveParent.removeItem(ch.plotCurve)
                ch.plotCurve = None
        channel.clearPlotCurve = clear

    if case == "widgets":
        # Execute the real Channel.initGUI and Parameter widgets, not an imagined
        # read-only widget contract. The rest of the hidden channel table is inert.
        core.Channel.initGUI = extract("core.py", "initGUI", "Channel")
        core.Channel.updateMin = extract("core.py", "updateMin", "Channel")
        core.Channel.updateMax = extract("core.py", "updateMax", "Channel")
        core.Channel.valueChanged = extract("core.py", "valueChanged", "Channel")
        core.Channel.EQUATION, core.Channel.SELECT, core.Channel.COLLAPSE = "Equation", "Select", "Collapse"
        global_updates = []
        window = QtWidgets.QTreeWidget()
        names = ["Name", "Value", "Monitor", "Min", "Max", "Module", "Function"]
        window.setColumnCount(len(names))
        window.setHeaderLabels(names)
        window.resize(1100, 235)
        module.ESIChannel.value = property(lambda ch: ch.params["Value"].value,
                                          lambda ch, value: setattr(ch.params["Value"], "value", value))
        module.ESIChannel.monitor = property(lambda ch: ch.params["Monitor"].value,
                                            lambda ch, value: setattr(ch.params["Monitor"], "value", value))
        module.ESIChannel.min = property(lambda ch: ch.params["Min"].value)
        module.ESIChannel.max = property(lambda ch: ch.params["Max"].value)
        for channel, item in zip(device.channels, module._fixed_channel_items("ESI"), strict=True):
            channel.loading = True
            channel.rowHeight = 27
            channel.print = device.print
            channel.pluginManager = SimpleNamespace(ChannelManager=plugins.Device, Device=plugins.Device,
                                                   DeviceManager=SimpleNamespace(globalUpdate=lambda **kw: global_updates.append(kw)))
            channel.displayedParameters = names
            channel.updateColor = channel.realChanged = channel.scalingChanged = lambda: None
            row = QtWidgets.QTreeWidgetItem(window)
            channel.params = {}
            channel.getParameterByName = channel.params.get
            defaults = {}
            for index, name in enumerate(names):
                kind = ns["PARAMETERTYPE"].LABEL if name in ("Name", "Function") else ns["PARAMETERTYPE"].FLOAT
                parameter = ns["Parameter"](name, channel, parameterType=kind, tree=window, itemWidget=row, column=index,
                                             indicator=name not in ("Value", "Min", "Max"),
                                             event=channel.valueChanged if name == "Value" else None)
                channel.params[name] = parameter
                defaults[name] = {ns["Parameter"].VALUE: item.get(name, np.nan), ns["Parameter"].RESTORE: True}
            channel.parameters = list(channel.params.values())
            channel.getSortedDefaultChannel = lambda defaults=defaults: defaults
            channel.tempParameters = lambda: ["Monitor"]
            channel.initGUI(item)
            channel.loading = False
        data = snapshot()
        currents = (2.345678912e-9, -4.567891234e-9)
        for address, value in enumerate(currents, 1):
            data["modules"][address]["measured_a"] = value
        controller._apply_snapshot(data)
        controller.updateValues()
        device.loading = False
        window.show()
        app.processEvents()
        for channel, expected in zip(device.channels[3:], currents, strict=True):
            for name in ("Value", "Monitor"):
                parameter = channel.params[name]
                assert isinstance(parameter.spin, ns["LabviewSciSpinBox"])
                assert parameter.indicator and parameter.spin.isReadOnly()
                assert parameter.unit == "A"
                assert parameter.value == pytest.approx(expected, abs=1e-18), (channel.name, name, parameter.value, parameter.spin.text(), parameter.spin.minimum(), parameter.spin.maximum())
                parameter.spin.setFocus()
                QTest.keyClick(parameter.spin, QtCore.Qt.Key.Key_Up)
                QTest.keyClicks(parameter.spin, "500")
                assert parameter.value == pytest.approx(expected, abs=1e-18)
            assert "e" in channel.params["Monitor"].spin.text().lower()
        assert not global_updates
        assert not device.channels[0].params["Value"].spin.isReadOnly()
        window.grab().save(str(output / "esi-current-widgets.png"))
        return 0

    if case in ("panel", "off", "stopping", "unconfirmed"):
        window = QtWidgets.QWidget()
        window.resize(720, 710)
        layout = QtWidgets.QVBoxLayout(window)
        device.tree = None
        device.addContentWidget = layout.addWidget
        device.loading = False

        class BoundParameter:
            def __init__(self, channel, attr):
                self.channel, self.attr = channel, attr

            @property
            def value(self):
                return getattr(self.channel, self.attr)

            @value.setter
            def value(self, value):
                setattr(self.channel, self.attr, value)

        for channel in device.channels:
            channel._parameters = {"Value": BoundParameter(channel, "value"), "Enabled": BoundParameter(channel, "enabled")}
        controller._apply_snapshot(snapshot())
        controller.updateValues()
        device._ensure_operator_panel()
        window.show()
        app.processEvents()
        assert device.esiHVCards[1]["current"].text() == "2.50 nA"
        assert device.esiHVCards[2]["current"].text() == "-4.50 nA"
        assert "98.0" in device.esiHVCards[1]["measured"].text()
        window.grab().save(str(output / "esi-current-panel.png"))
        if case != "panel":
            calls = []
            saved_enabled = [channel.enabled for channel in device.channels]
            device.esiHeatButton.toggled.connect(lambda checked: calls.append(checked))
            for widgets in device.esiHVCards.values():
                widgets["sel_group"].idClicked.connect(lambda index: calls.append(index))
            if case == "off":
                controller.initialized = controller.acquiring = False
                controller.main_state = "Disconnected"
                controller.initializeValues(reset=True)
            else:
                controller.main_state = module._ESI_STOPPING
                controller._on_discharge_progress({
                    "modules": {a: {"positive_v": 12.5, "negative_v": -11., "measured_a": 2.5e-9}
                                for a in (1, 2)}, "consecutive": 0, "limit_v": 1.})
                if case == "unconfirmed":
                    controller.main_state = "Shutdown unconfirmed"
            device._update_operator_panel()
            QTest.qWait(30)  # let nested Qt LayoutRequest events settle
            for widgets in device.esiHVCards.values():
                current_widget = widgets["current"]
                assert widgets["card"].rect().contains(QtCore.QRect(
                    current_widget.mapTo(widgets["card"], QtCore.QPoint()), current_widget.size()
                )), "Current readback clipped after the voltage label expands"
                assert widgets["btn_on"].styleSheet() == module._ESI_BTN_NEUTRAL
                assert not widgets["btn_on"].isChecked()
                assert not widgets["btn_on"].isEnabled()
                assert not widgets["target"].isEnabled()
                if case == "stopping":
                    assert "POS 12.50" in widgets["measured"].text()
                    assert "NEG -11.00" in widgets["measured"].text()
                    assert widgets["current"].text() == "2.50 nA"
                expected_style = (module._ESI_PANEL_CARD_DISC if case == "off" else
                                  module._ESI_PANEL_CARD_STOPPING if case == "stopping" else module._ESI_PANEL_CARD_ERR)
                assert widgets["card"].styleSheet() == expected_style
            assert device.esiHeatButton.styleSheet() == module._ESI_BTN_NEUTRAL
            assert not device.esiHeatButton.isChecked()
            assert not calls, "Rendering OFF must not dispatch output commands"
            assert [channel.enabled for channel in device.channels] == saved_enabled
            window.grab().save(str(output / f"esi-{case}.png"))
            # Reconnection must restore controls and status colors from readback,
            # not change the remembered per-module enable settings.
            controller.initialized = True
            controller.main_state = "STATE_ON"
            controller._apply_snapshot(snapshot())
            device._update_operator_panel()
            for widgets in device.esiHVCards.values():
                assert widgets["target"].isEnabled()
                assert widgets["current"].isEnabled()
                assert widgets["btn_on"].styleSheet() == module._ESI_BTN_HV_ACTIVE
            return 0
        # Reordering measurements must not redirect panel controls to currents.
        device.channels[:] = device.channels[3:] + device.channels[:3]
        requested = [channel.value for channel in device.channels[:2]]
        device._panel_target_changed(1, 123.)
        device._panel_output_selected(1, 0)
        assert [channel.value for channel in device.channels[:2]] == requested
        assert all(channel.enabled for channel in device.channels[:2])
        assert device.channels[2].value == 123. and not device.channels[2].enabled
        return 0

    # A complete acquisition cycle: monitor updates -> Explorer.appendData ->
    # DynamicNp buffers. No fresh measurement is fabricated after a failed read.
    device.plotableChannels = device.channels
    device.updateValues = controller.updateValues
    device.liveDisplayActive = lambda: False
    device.measureInterval = lambda **kw: None
    t0 = 1_700_000_000.
    clock = SimpleNamespace(now=t0)
    ns["time"] = SimpleNamespace(time=lambda: clock.now)
    expected = []
    for index in range(1000):
        data = snapshot()
        data["modules"][1]["measured_a"] = 2.5e-9 + np.sin(index / 60.) * 1e-10
        data["modules"][2]["measured_a"] = -4.5e-9 + np.cos(index / 40.) * 1e-10
        if index in (100, 101):
            data["modules"][1]["current_valid"] = False
        controller._apply_snapshot(data)
        device.appendData()
        expected.append([ch.monitor for ch in device.channels])
        clock.now += 1.
    expected = np.array(expected)
    for index, channel in enumerate(device.channels):
        np.testing.assert_allclose(channel.values.get(), expected[:, index], atol=1e-18)
        assert channel.values.size == device.time.size == 1000

    live = module._ESILiveDisplay.__new__(module._ESILiveDisplay)
    device.liveDisplay = live
    live.parentPlugin = device
    live.pluginManager = device.pluginManager = SimpleNamespace(loading=False, resizing=False, Settings=SimpleNamespace(loading=False))
    live.initializedDock = True
    device.recording = True
    device.minTime = lambda: t0
    live.getDisplayTime = lambda: -1
    live.GroupActionState = Enum("GroupActionState", "ALL DEVICE UNIT GROUP")
    live.StackActionState = Enum("StackActionState", "HORIZONTAL VERTICAL STACKED")
    live.groupAction = SimpleNamespace(state=live.GroupActionState.ALL)
    live.stackAction = SimpleNamespace(state=live.StackActionState.VERTICAL)
    live.autoScaleAction = SimpleNamespace(state=True)
    live.updateLegend = False
    live.print = device.print
    live.waitForCondition = lambda **kw: True
    live.livePlotWidgets = []
    live.clearPlot = lambda: live.livePlotWidgets.clear()
    window = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(window)
    live.addContentWidget = layout.addWidget
    window.resize(900, 720)

    if case == "groups":
        for state in live.GroupActionState:
            live.groupAction.state = state
            groups = live.getGroups()
            assert live.channelGroups is groups
            assert len(groups) == 3
            for channels in groups.values():
                assert len({ch.unit for ch in channels}) == 1
        device.channels[3].display = False
        assert all(device.channels[3] not in group for group in live.getGroups().values())
        return 0

    if case == "recording":
        path = output / "esi-current.h5"
        with h5py.File(path, "w") as h5file:
            device.appendOutputData(h5file, useDefaultFile=True)
        with h5py.File(path, "r") as h5file:
            np.testing.assert_array_equal(h5file["ESI/Input Channels/Time"], np.arange(1000) + t0)
            for index, channel in enumerate(device.channels):
                data = h5file[f"ESI/Output Channels/{channel.name}"]
                assert data.attrs["Unit"] == channel.unit
                np.testing.assert_allclose(data[:], expected[:, index], atol=1e-16)
            assert np.isnan(h5file["ESI/Output Channels/ESI_HV1_I"][100])
        # Manual export keeps Explorer's selected time window and backgrounds.
        device.useBackgrounds = True
        for channel in device.channels:
            channel.backgrounds = ns["DynamicNp"](initialData=np.zeros(1000))
        live.livePlotWidgets = [SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=(t0 + 50, t0 + 150)))]
        with h5py.File(output / "esi-current-selection.h5", "w") as h5file:
            device.appendOutputData(h5file, useDefaultFile=False)
            np.testing.assert_array_equal(h5file["ESI/Input Channels/Time"], np.arange(50, 150) + t0)
            for index, channel in enumerate(device.channels):
                group = h5file["ESI/Output Channels"]
                assert group[channel.name].attrs["Unit"] == channel.unit
                assert group[channel.name + "_BG"].attrs["Unit"] == channel.unit
                np.testing.assert_allclose(group[channel.name][:], expected[50:150, index], atol=1e-16)
            # A repeat append must not overwrite existing data or its metadata.
            group["ESI_HV1_I"].attrs["Unit"] = "original"
            device.appendOutputData(h5file, useDefaultFile=False)
            assert group["ESI_HV1_I"].attrs["Unit"] == "original"
        return 0

    if case in ("horizontal", "vertical", "stacked"):
        live.stackAction.state = getattr(live.StackActionState, case.upper())
    else:
        # One current trace lets us test degenerate auto-range without another
        # signal at a different current level hiding the regression.
        for channel in device.channels:
            channel.display = channel is device.channels[3]
        current = device.channels[3]
        if case in ("constant", "manual", "log"):
            current.values = ns["DynamicNp"](initialData=np.full(1000, 2.5e-9))
        if case == "log":
            current.logY = True
    live.initFig()
    axes = {device.name: (0, None, 1, device.time.get())}
    live.getTimeAxes = lambda: axes
    if case == "manual":
        live.livePlotWidgets[0].setYRange(-1e-8, 1e-8, padding=0)
    before = live.livePlotWidgets[0].viewRange()[1]
    live.plot(apply=True)
    window.show()
    app.processEvents()
    for plot in live.livePlotWidgets:
        item = plot.getPlotItem() if hasattr(plot, "getPlotItem") else plot
        if getattr(item, "groupLabel", None) is not None and item.legend is not None:
            assert item.groupLabel.sceneBoundingRect().bottom() <= item.legend.sceneBoundingRect().top()
    for channel in device.channels:
        if channel.display:
            assert channel.plotCurve is not None
            x, y = channel.plotCurve.getData()
            np.testing.assert_array_equal(x, device.time.get())
            np.testing.assert_allclose(y, np.log10(channel.values.get()) if case == "log" else channel.values.get(), atol=1e-18)
            assert f"({channel.unit})" in channel.plotCurve.name()
    if case == "constant":
        low, high = live.livePlotWidgets[0].viewRange()[1]
        assert low < 2.5e-9 < high and high - low < 1e-8, (low, high)
        assert live.livePlotWidgets[0].getViewBox().autoRangeEnabled()[1]
    elif case == "manual":
        assert live.livePlotWidgets[0].viewRange()[1] == before
    elif case == "log":
        assert device.channels[3].plotCurve.opts["logMode"][1]
    window.grab().save(str(output / f"esi-current-{case}.png"))
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
