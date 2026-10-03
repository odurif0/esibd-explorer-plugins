"""Independent real-Qt probes for readout, focused edits and card layout."""
from pathlib import Path
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("case", ["values", "editing", "layout"])
def test_pressure_panel(case, tmp_path):
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), case, str(tmp_path)],
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
                            capture_output=True, text=True, timeout=15)
    if result.returncode == 77:
        pytest.skip(result.stdout.strip())
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Traceback" not in result.stderr


def probe(case, output):
    import importlib.util
    import math
    from types import SimpleNamespace as NS
    try:
        from PyQt6.QtWidgets import QApplication, QScrollArea
    except ImportError:
        print("Real Qt required")
        return 77
    path = Path(__file__).resolve().parents[1] / "tpg366/_readout_panel.py"
    spec = importlib.util.spec_from_file_location("_pressure_panel_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = QApplication([])
    channels = [NS(name=f"P{i+1}", input=i+1, value=(i+1)*1e-6, enabled=True, display=True,
                   active=True, real=True, status="OK", gauge=f"Model {i+1}", color="#2c80e0") for i in range(6)]
    edits = []
    def edit(channel, field, value):
        edits.append((field, value))
        setattr(channel, field, value)
        panel.refresh(channels, live=True)
    panel = module.PressurePanel(edit)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(panel)
    scroll.resize(700, 500)
    panel.refresh(channels, live=True)
    scroll.show()
    app.processEvents()
    if case == "values":
        card = panel.cards[id(channels[0])]
        assert card[3].text() == "1.00e-06" and card[5].text() == "Model 1"
        channels[0].status = "Sensor error"
        panel.refresh(channels, live=True)
        assert card[3].text() == "—" and card[4].text() == "Sensor error"
        channels[0].real = False
        panel.refresh(channels, live=True)
        assert card[1].text() == "Derived" and card[3].text() == "1.00e-06" and card[4].text() == "Virtual"
        panel.refresh(channels, live=False)
        assert all(card[3].text() == "—" for card in panel.cards.values())
        assert not edits
    elif case == "editing":
        editor = panel.cards[id(channels[0])][2]
        editor.setFocus()
        app.processEvents()
        editor.setText("Typing")
        panel.refresh(channels, live=True)
        assert editor.text() == "Typing" and not edits
        editor.editingFinished.emit()
        assert edits == [("name", "Typing")]
        panel.cards[id(channels[0])][6].click()
        assert not channels[0].enabled and panel.cards[id(channels[0])][3].text() == "—"
        before = list(edits)
        editor.setFocus()
        app.processEvents()
        panel.refresh(list(reversed(channels)), live=True)
        app.processEvents()
        assert edits == before, "Removing a focused card must not commit a ghost edit"
    else:
        for width in (700, 245):
            scroll.resize(width, 500)
            app.processEvents()
            rectangles = [card[0].geometry() for card in panel.cards.values()]
            assert all(not a.intersects(b) for index, a in enumerate(rectangles) for b in rectangles[index+1:])
            if width == 245:
                assert len({rectangle.x() for rectangle in rectangles}) == 1
                assert scroll.verticalScrollBar().maximum() > 0
            assert scroll.grab().save(str(Path(output) / f"pressure-panel-{width}.png"))
    scroll.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], sys.argv[2]))
