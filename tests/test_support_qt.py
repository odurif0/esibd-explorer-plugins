"""Real-Qt probe helpers: a crash keeps its stack, and the teardown itself does not crash."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from support.qt import fatal_stack, run_probe

CRASH = """
import ctypes
import numpy  # Its extension modules follow the stack in faulthandler's report.

def crash():
    ctypes.string_at(0)

crash()
"""


def test_a_crashed_probe_reports_its_stack_not_only_the_extension_modules():
    result = subprocess.run([sys.executable, "-c", CRASH], env={**os.environ, "PYTHONFAULTHANDLER": "1"},
                            capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    stack = fatal_stack(result.stderr)
    assert "Fatal Python error" in stack and "in crash" in stack, result.stderr
    assert "Extension modules" not in stack
    assert fatal_stack("Traceback (most recent call last):\n") == ""


TOOLBAR = """
import sys
sys.path.insert(0, sys.argv[1])
try:
    from PyQt6.QtWidgets import QApplication, QToolBar, QVBoxLayout, QWidget
except ImportError:
    sys.exit(77)
from support.qt import dispose_windows

app = QApplication([])
plugin = QWidget()  # Owns the actions, like an Explorer plugin whose title bar sits elsewhere.
window = QWidget()
bar = QToolBar()
QVBoxLayout(window).addWidget(bar)
for index in range(40):
    bar.addAction(plugin.addAction(f"Action {index}"))
window.resize(200, 80)
window.show()
app.processEvents()
assert any(w.parentWidget() is not None for w in app.topLevelWidgets())  # The extension menu.
dispose_windows(app)
"""


def test_disposing_probe_windows_spares_a_toolbar_extension_menu(tmp_path):
    # Deleting the overflow menu on its own crashed the action removal that followed.
    script = tmp_path / "toolbar_probe.py"
    script.write_text(TOOLBAR, encoding="utf-8")
    run_probe(script, Path(__file__).resolve().parent, timeout=60, env={"MALLOC_PERTURB_": "165"})
