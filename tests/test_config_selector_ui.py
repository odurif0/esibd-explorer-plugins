"""Real Qt: an open config list stays where the operator scrolled it (2026-11-02 lab, AMX HD).

Each poll rebuilt the list. With 180 configs and a poll per second it kept jumping back to its top,
so the configs at the end could not be reached.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from support.qt import run_probe


@pytest.mark.parametrize("folder", ["amx_a", "amx_hd", "psu_a"])
def test_a_config_list_survives_polling(folder, tmp_path):
    run_probe(__file__, folder, tmp_path, timeout=35)


def probe(folder, _output):
    try:
        from PyQt6.QtWidgets import QApplication, QComboBox
    except ImportError:
        return 77
    app = QApplication([])
    if folder == "psu_a":
        from support.psu import load_plugin
        cls = load_plugin(folder, "selector_psu").PSUDevice
    elif folder == "amx_hd":
        from support.amx import load_plugin
        cls = load_plugin(folder).AMXHDDevice
    else:
        from support.amx import load_plugin
        cls = load_plugin(folder).AMXDevice
    device = cls.__new__(cls)
    combo = QComboBox()
    device.operatingConfigCombo, device.operatingConfigLabel = combo, None
    entries = [(-1, "None")] + [(i, f"Config {i}") for i in range(180)]
    device._config_selector_entries = lambda attr: list(entries)
    device._config_setting_value = lambda attr: 42
    device._config_selector_tooltip_text = lambda attr: "tip"
    update = ((lambda: device._update_config_selectors()) if folder == "psu_a"
              else (lambda: device._update_config_selector(combo, "operating_config")))
    update()
    assert combo.count() == 181 and combo.currentData() == 42
    rebuilt = []
    clear = combo.clear
    combo.clear = lambda: (rebuilt.append(True), clear())[1]
    update()
    assert not rebuilt, "an unchanged list is not rebuilt"
    # Open and scrolled to its end: polls leave it alone, a new list waits for it to close.
    combo.showPopup()
    app.processEvents()
    bar = combo.view().verticalScrollBar()
    bar.setValue(bar.maximum())
    position = bar.value()
    assert position > 0
    entries.append((300, "Config 300"))
    for _ in range(3):
        update()
        app.processEvents()
    assert bar.value() == position and combo.view().isVisible() and not rebuilt
    combo.hidePopup()
    app.processEvents()
    update()
    assert rebuilt == [True] and combo.count() == 182 and combo.currentData() == 42
    return 0


if __name__ == "__main__":
    raise SystemExit(probe(sys.argv[1], Path(sys.argv[2])))
