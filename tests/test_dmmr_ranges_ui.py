"""Real Qt selectors, settings widgets, worker delivery and compact layout."""
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scale", ["1", "1.5"])
def test_ranges_with_real_qt(scale, tmp_path):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen", "QT_SCALE_FACTOR": scale},
                            capture_output=True, text=True, timeout=45)
    if result.returncode == 77:
        pytest.skip("PyQt6 unavailable")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr, result.stderr


def probe(output):
    try:
        from PyQt6.QtCore import QThread, Qt, QPoint, QPointF
        from PyQt6.QtGui import QWheelEvent
        from PyQt6.QtTest import QTest
        from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget
    except ImportError:
        return 77
    import threading
    import numpy as np
    from test_dmmr_plugin_behavior import _install_esibd_stubs
    from test_dmmr_cards import use_real_label_parameters
    from test_dmmr_ranges import RangeDevice

    app = QApplication([])
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location("dmmr_range_ui", ROOT / "dmmr/dmmr_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parent = module.DMMRDevice.__new__(module.DMMRDevice)
    parent.loading = False
    parent.onAction = SimpleNamespace(state=False)
    parent.main_state = "Disconnected"
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state = lambda value: setattr(parent.onAction, "state", value)
    parent.connect_timeout_s = parent.poll_timeout_s = 0.1
    parent.channels = [SimpleNamespace(name=f"DMMR_M{i:02}", label="Collecteur" if i == 0 else "",
                                      enabled=True, real=True, display=True, color="#3182ce",
                                      module_address=lambda i=i: i) for i in range(8)]
    parent.getChannels = lambda: parent.channels
    logs, exports = [], []
    parent.print = lambda message, **kw: logs.append(message)
    parent.exportConfiguration = lambda **kw: exports.append([ch.range_mode for ch in parent.channels])
    use_real_label_parameters(module, parent)
    channel_cls = module.DMMRChannel
    channel_cls.range_mode = property(lambda ch: ch.range_parameter.value,
                                     lambda ch, value: setattr(ch.range_parameter, "value", value))
    # Mirror a widget-backed monitor and assert every setter runs on the GUI thread.
    def set_monitor(ch, value):
        assert QThread.currentThread() == app.thread()
        ch._monitor = value
        ch.current_label.setText(str(value))
    channel_cls.monitor = property(lambda ch: ch._monitor, set_monitor)
    for ch in parent.channels:
        ch.current_label = QLabel()
        ch.monitor = np.nan
        declaration = ch.getDefaultChannel()[ch.RANGE_MODE]
        p = module.Parameter
        ch.range_parameter = p(ch.RANGE_MODE, ch, parameterType=declaration[p.PARAMETER_TYPE],
                               items=declaration[p.ITEMS], fixedItems=True, event=ch.rangeChanged)
        ch.range_mode = "Auto"
        ch.rangeChanged()
        ch._sync_monitor_widget = lambda: None
    controller = module.DMMRController(parent)
    parent.controller = controller
    controller.print = parent.print
    controller.device = hardware = RangeDevice()
    controller.detected_module_ids = [0, 3]
    controller.initialized = True
    # Only the two simulated addresses are polled/configured in this probe.
    parent.getConfiguredModules = lambda: [0, 3]
    window = QWidget()
    layout = QVBoxLayout(window)
    parent.addContentWidget = layout.addWidget
    parent._ensure_channel_panel()
    window.resize(850, 500)
    window.show()

    def flush():
        for _ in range(20):
            app.processEvents()

    flush()
    combo = parent.channelPanelCards[3]["range_combo"]
    assert combo.isEnabled() and combo.currentData() == "Auto"
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key.Key_End)
    flush()
    assert combo.currentData() == "4"
    assert parent.channels[3].range_mode == "4"
    assert parent.channels[3]._requested_range_mode == "4"
    assert exports[-1][3] == "4"
    assert all(ch.range_mode == "Auto" for i, ch in enumerate(parent.channels) if i != 3)
    count = len(exports)
    # Wheel scrolling must not silently change a saved range, even when focused.
    wheel = QWheelEvent(QPointF(5, 5), QPointF(combo.mapToGlobal(QPoint(5, 5))), QPoint(), QPoint(0, 120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(combo, wheel)
    flush()
    assert combo.currentData() == "4" and len(exports) == count
    # Configuration-save errors restore both the visible choice and worker cache.
    def denied(**kw):
        raise PermissionError("read-only config")
    parent.exportConfiguration = denied
    QTest.keyClick(combo, Qt.Key.Key_Home)
    flush()
    assert combo.currentData() == "4" and parent.channels[3]._requested_range_mode == "4"
    assert any("Could not save Module 3 range" in text for text in logs)
    parent.onAction.state = True
    controller.toggleOn()
    flush()
    assert controller.acquiring, logs
    assert hardware.modes[3] is False and hardware.ranges[3] == 4
    parent._update_channel_panel()
    assert not combo.isEnabled()
    parent._channel_panel_range_changed(3, "Auto")
    assert parent.channels[3].range_mode == "4", "A disabled selector must not reconfigure acquisition"

    def work():
        controller.readNumbers()
        controller.updateValues()
    errors = []
    def checked_work():
        try:
            work()
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=checked_work)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive()
    flush()
    assert not errors, errors
    assert parent.channels[0].monitor == 0.534e-12
    assert parent.channels[0].measurement_range == 0
    assert parent.channelPanelCards[0]["range_value"].text() == "Used: 0"
    assert parent.channelPanelCards[3]["range_value"].text() == "Used: 4"
    for width in (370, 850, 1650):
        window.resize(width, 500)
        flush()
        for widgets in parent.channelPanelCards.values():
            selector, used = widgets["range_combo"], widgets["range_value"]
            assert not selector.geometry().intersects(used.geometry())
            assert used.width() >= used.fontMetrics().horizontalAdvance(used.text())
            assert widgets["card"].width() <= 210
        window.grab().save(str(output / f"dmmr-ranges-{width}.png"))
    parent.onAction.state = False
    controller.updateValues()
    parent._update_channel_panel()
    flush()
    assert combo.isEnabled() and combo.currentData() == "4"
    assert parent.channelPanelCards[3]["range_value"].text() == "Used: —"
    assert np.isnan(parent.channels[3].measurement_range)
    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(Path(sys.argv[1])))
