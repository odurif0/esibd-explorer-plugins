"""Actual AMPR name editor updates existing pyqtgraph curves, ON or OFF."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", ["ampr_a", "ampr_b"])
def test_ampr_name_editor_updates_plot_labels(folder, tmp_path):
    run_probe(__file__, folder, tmp_path, timeout=30)


def exercise(module, device, app, window, output):
    from importlib.metadata import distribution
    import time

    import numpy as np
    import pyqtgraph as pg
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    host = Path(distribution("esibd-explorer").locate_file("esibd"))
    namespace = module.Channel.updateDisplay.__globals__
    namespace["PlotDataItem"] = pg.PlotDataItem
    from explorer_host import compile_definition

    def extract(filename, name, parent=None):
        exec(compile_definition(host / filename, name, parent), namespace)
        return namespace[name]

    for name in ("ViewBox", "LabelItem", "SciAxisItem", "PlotItem", "PlotWidget"):
        extract("core.py", name)
    live_class = type("HostLiveDisplay", (), {
        name: extract("plugins.py", name, parent)
        for name, parent in (("plotGroup", "LiveDisplay"), ("plotChannel", "LiveDisplay"), ("getQtPen", "Plugin"))
    })
    live = live_class()
    plot = namespace["PlotWidget"](parentPlugin=None, groupLabel=device.name)
    plot.init()
    plot.finalizeInit()
    device.time = namespace["DynamicNp"](dtype=np.float64)
    device.getDisplayUnit = lambda: "V"
    device.subtractBackgroundActive = lambda: False
    device.unit = "V"
    device.pluginManager.connectAllSources = lambda: None
    channels = device.getChannels()
    first = channels[0]
    first.display = True
    for i in range(6):
        for channel in channels:
            channel.values.add(x=float(i + 1), lenT=device.time.size)
        device.time.add(time.time() - 6 + i)
    def draw(**kwargs):
        live.plotGroup(plot, {device.name: (0, None, 1, device.time.get())}, channels, True)
    live.plot = draw
    live.updateLegend = False
    device.liveDisplay = live
    draw()
    assert first.plotCurve.name() == "Inlet_Capillary (V)"
    times, values = device.time.get().copy(), first.values.get().copy()
    parameter = first.getParameterByName(first.NAME)
    for recording, text in ((False, "Sample_chamber"), (True, "Extractor"), (False, "Restored_label")):
        device.recording = recording
        parameter.line.setFocus()
        parameter.line.selectAll()
        QTest.keyClicks(parameter.line, text)
        QTest.keyClick(parameter.line, Qt.Key.Key_Return)
        app.processEvents()
        if recording:
            draw()  # Explorer refreshes active recordings on the next frame.
        assert first.name == text and first.plotCurve.name() == f"{text} (V)", (
            recording, first.name, first.plotCurve.name()
        )
        np.testing.assert_array_equal(first.values.get(), values)
        np.testing.assert_array_equal(device.time.get(), times)
    device.recording = False
    assert plot.grab().save(str(output / f"{device.name}-plot-labels.png"))


if __name__ == "__main__":
    from support.ampr_ui import probe
    raise SystemExit(probe(sys.argv[1], False, Path(sys.argv[2]), exercise=exercise))
