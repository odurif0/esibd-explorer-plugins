"""DMMR plot/panel checks with real Qt, pyqtgraph, and Explorer plot methods.

Run in subprocesses: the rest of the suite installs lightweight ESIBD/Qt stubs.
No driver, serial port, or user settings are opened.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("case", ["constant", "varying", "manual", "empty", "log", "panel"])
def test_dmmr_plot_and_panel(case, tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", case, str(tmp_path)],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode == 77:
        pytest.skip("Real Qt/pyqtgraph or installed Explorer sources unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(case, output):
    import configparser
    import math

    import numpy as np

    try:
        import pyqtgraph as pg
        from PyQt6.QtCore import Qt, QTimer, pyqtSignal
        from PyQt6.QtGui import QFont
        from PyQt6.QtWidgets import QApplication, QColorDialog, QHBoxLayout, QWidget
        from PyQt6.QtGui import QColor
    except ImportError:
        return 77

    spec = importlib.util.find_spec("esibd")
    if spec is None or not spec.origin:
        return 77
    host = Path(spec.origin).parent
    if not (host / "core.py").is_file():
        return 77

    app = QApplication([])
    ns = dict(np=np, pg=pg, Qt=Qt, QTimer=QTimer, pyqtSignal=pyqtSignal, QFont=QFont, Path=Path,
              colors=types.SimpleNamespace(fg="#202020", bg="#ffffff"), cast=lambda _, obj: obj)

    def extract(path, name, parent=None):
        tree = ast.parse(path.read_text())
        nodes = tree.body if parent is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parent).body
        node = next(n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(module), str(path), "exec"), ns)
        return ns[name]

    for name in ("ViewBox", "LabelItem", "SciAxisItem", "PlotItem", "PlotWidget"):
        extract(host / "core.py", name)
    ns["PlotDataItem"] = pg.PlotDataItem
    host_live = type("HostLiveDisplay", (), {
        name: extract(host / "plugins.py", name, "LiveDisplay")
        for name in ("plotGroup", "plotChannel")
    })

    from test_dmmr_plugin_behavior import _install_esibd_stubs
    _install_esibd_stubs()
    sys.modules["esibd.plugins"].LiveDisplay = host_live
    ns["Device"] = sys.modules["esibd.plugins"].Device
    spec = importlib.util.spec_from_file_location("dmmr_plot_probe", ROOT / "dmmr/dmmr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Use the original host implementation before the fix to demonstrate the
    # actual +/-0.5 A fallback, rather than merely a missing-method failure.
    live_class = getattr(module, "_DMMRLiveDisplay", host_live)
    live = live_class.__new__(live_class)
    live.updateLegend = False
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.loading = False
    device.main_state = "ST_ON"
    device.isOn = lambda: True
    device.subtractBackgroundActive = lambda: False
    device.controller = types.SimpleNamespace(values={0: 1.5e-12, 1: -2e-12})
    device.getChannels = lambda: device.channels
    device.channels = []

    append = extract(host / "core.py", "appendValue", "Channel")

    class Buffer:
        def __init__(self, data=()): self.data = list(data)
        @property
        def size(self): return len(self.data)
        def get(self): return np.array(self.data)
        def add(self, x, lenT=None): self.data.append(x)

    class Channel:
        real = enabled = display = useMonitors = True
        useBackgrounds = False
        value = 0.0
        unit = "A"
        smooth = 0
        linewidth = 3
        logY = False
        plotCurve = None
        appendValue = append
        def __init__(self, number):
            self.number = number
            self.name = f"DMMR_M{number:02}"
            self._color = "#3182ce" if number == 0 else "#d97706"
            self.values = Buffer()
            self.time = Buffer(range(40))
        @property
        def color(self): return self._color
        @color.setter
        def color(self, value):
            self._color = value
            # Channel.color is a Parameter wrapper whose normal updateColor
            # event invalidates the old curve (Explorer Channel.updateDisplay).
            self.clearPlotCurve()
        def clearPlotCurve(self):
            if self.plotCurve is not None:
                plot.removeItem(self.plotCurve)
                self.plotCurve = None
        def getDevice(self): return device
        def getQtLineStyle(self): return Qt.PenStyle.SolidLine
        def getValues(self, **kwargs): return self.values.get()
        def convertDataDisplay(self, data): return data
        def module_address(self): return self.number
        def asDict(self, **kwargs): return {"Name": self.name, "Color": self.color}

    channel = Channel(0)
    device.channels = [channel, Channel(1)] if case == "panel" else [channel]
    plot = ns["PlotWidget"](parentPlugin=None, groupLabel="DMMR")
    plot.init()
    plot.finalizeInit()
    plot.resize(720, 400)
    plot.setXRange(0, 39, padding=0)
    live.livePlotWidgets = [plot]

    def draw(currents):
        channel.values = Buffer()
        for current in currents:
            channel.monitor = current
            channel.appendValue(channel.values.size)
        raw = channel.values.get().copy()
        live.plotGroup(plot, {device.name: (0, None, 1, channel.time.get())}, [channel], True)
        plot.show()
        app.processEvents()
        np.testing.assert_array_equal(channel.values.get(), raw)
        if np.all(np.isnan(raw)):
            assert channel.plotCurve is None
        else:
            np.testing.assert_array_equal(channel.plotCurve.getData()[1], raw)
        assert module.DMMRDevice.unit == "A"
        return plot.viewRange()[1]

    if case == "constant":
        for current in (1.5e-12, -3e-12, 30e-15, 2e-9, 0.0):
            low, high = draw([current] * 40)
            assert low < current < high
            assert high - low < max(abs(current) * 4, 4e-12), (current, low, high)
            assert plot.getViewBox().autoRangeEnabled()[1]
            if current == 1.5e-12:
                plot.grab().save(str(output / "dmmr-constant-picoamp-plot.png"))
        # Stacked plots hand the same plotting method a ViewBox directly.
        stacked = ns["ViewBox"]()
        plot.scene().addItem(stacked)
        channel.clearPlotCurve()
        channel.values = Buffer([1.5e-12] * 40)
        live.plotGroup(stacked, {device.name: (0, None, 1, channel.time.get())}, [channel], True)
        app.processEvents()
        low, high = stacked.viewRange()[1]
        assert low < 1.5e-12 < high and high - low < 6e-12
    elif case == "varying":
        for center in (1.5e-12, -3e-12, 0.0, 30e-15):
            currents = np.array([center + 3e-15 * math.sin(i) for i in range(40)])
            currents[12:15] = np.nan
            low, high = draw(currents)
            assert low <= np.nanmin(currents) and high >= np.nanmax(currents)
            assert high - low < 1e-12
        # Different constant channels in one group must ALL remain in view.
        other = Channel(1)
        other.values = Buffer([8e-12] * 40)
        channel.values = Buffer([1.5e-12] * 40)
        live.plotGroup(plot, {device.name: (0, None, 1, channel.time.get())}, [channel, other], True)
        app.processEvents()
        low, high = plot.viewRange()[1]
        assert low <= 1.5e-12 and high >= 8e-12
    elif case == "manual":
        plot.setYRange(-10e-12, 10e-12, padding=0)
        assert draw([1.5e-12] * 40) == [-10e-12, 10e-12]
        assert not plot.getViewBox().autoRangeEnabled()[1]
        plot.enableAutoRange(y=True)
        low, high = draw([1.5e-12] * 40)
        assert low < 1.5e-12 < high and high - low < 6e-12
    elif case == "empty":
        draw([np.nan] * 40)
        draw([1.5e-12] * 40)
        draw([np.nan] * 40)
        live.plotGroup(plot, {}, [], True)
    elif case == "log":
        plot.setLogMode(y=True)
        channel.values = Buffer([1.5e-12] * 40)
        live.plotGroup(plot, {device.name: (0, None, 1, channel.time.get())}, [channel], True)
        app.processEvents()
        low, high = plot.viewRange()[1]
        assert low < np.log10(1.5e-12) < high and high - low < 2
    elif case == "panel":
        window = QWidget()
        layout = QHBoxLayout(window)
        device.addContentWidget = layout.addWidget
        device._ensure_channel_panel()
        window.show()
        app.processEvents()
        cards = device.channelPanelCards
        assert all("color_button" in card for card in cards.values())
        for card in cards.values():
            button, checkbox = card["color_button"], card["display_box"]
            assert button.x() >= checkbox.x() + checkbox.width()
            assert button.isVisible() and button.isEnabled()
            assert button.width() <= 26
        # Exercise Explorer's actual INI writer, in a temporary directory only.
        exports = []
        ns.update(configparser=configparser, FILE_INI=".ini", INFO="Info", infoDict=lambda name: {"Name": name})
        export = extract(host / "plugins.py", "exportConfiguration", "ChannelManager")
        device.confINI, device.UTF8, device.CHANNEL = "DMMR.ini", "utf-8", "Channel"
        device.customConfigFile = lambda _: output / "DMMR.ini"
        device.pluginManager = types.SimpleNamespace(loading=True)
        def save(**kwargs):
            exports.append(kwargs)
            export(device, **kwargs)
        device.exportConfiguration = save
        draw([1.5e-12 + 3e-14 * math.sin(i) for i in range(40)])
        original_dialog = QColorDialog.getColor
        def choose():
            dialog = app.activeModalWidget()
            assert isinstance(dialog, QColorDialog)
            assert dialog.currentColor().name() == channel.color
            dialog.setCurrentColor(QColor("#bb4477"))
            device._update_channel_panel()  # Acquisition may refresh while choosing.
            dialog.accept()
        QTimer.singleShot(0, choose)
        cards[0]["color_button"].click()
        assert channel.color == "#bb4477"
        assert device.channels[1].color == "#d97706"
        assert exports == [{"useDefaultFile": True}]
        config = configparser.ConfigParser()
        config.read(output / "DMMR.ini")
        assert config["Channel_000"]["color"] == "#bb4477"
        draw([1.5e-12 + 3e-14 * math.sin(i) for i in range(40)])
        assert channel.plotCurve.opts["pen"].color().name() == "#bb4477"
        QTimer.singleShot(0, lambda: app.activeModalWidget().reject())
        cards[0]["color_button"].click()
        assert channel.color == "#bb4477" and len(exports) == 1
        QColorDialog.getColor = lambda *args: QColor("#bb4477")  # Unchanged choice
        cards[0]["color_button"].click()
        assert len(exports) == 1
        # A hardware rescan while the modal dialog is open must not edit an
        # orphaned channel object or fail when the module is no longer there.
        def rescan(*args):
            device.channels = device.channels[1:]
            return QColor("#00aa99")
        QColorDialog.getColor = rescan
        cards[0]["color_button"].click()
        assert channel.color == "#bb4477" and len(exports) == 1
        device.channels.insert(0, channel)
        QColorDialog.getColor = original_dialog
        device._rebuild_channel_panel_cards()
        device._update_channel_panel()
        assert "#bb4477" in device.channelPanelCards[0]["color_button"].styleSheet()
        app.processEvents()
        window.grab().save(str(output / "dmmr-color-panel.png"))
        plot.grab().save(str(output / "dmmr-picoamp-plot.png"))
        window.close()
    plot.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[2], Path(sys.argv[3])))
