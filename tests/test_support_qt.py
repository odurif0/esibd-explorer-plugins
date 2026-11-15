"""Real-Qt probe helpers: a crash keeps its stack, and the teardown itself does not crash."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from support import qt
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


LATE = """
import os, sys
from pathlib import Path
work, runs = Path(sys.argv[1]), Path(sys.argv[2]) / "runs"
assert list(work.iterdir()) == [], "the working directory is not as the first run found it"
(work / "result.txt").write_text("written")
count = int(runs.read_text()) + 1 if runs.exists() else 1
runs.write_text(str(count))
mode = sys.argv[3]
if mode == "crash":
    os.abort()
if mode == "code":
    sys.exit(3)
assert mode == "late-once" and count > 1, "too late"
"""


def _late_probe(tmp_path, mode):
    work, kept = tmp_path / "work", tmp_path / "kept"
    work.mkdir(), kept.mkdir()
    script = kept / "late_probe.py"
    script.write_text(LATE, encoding="utf-8")
    return script, work, kept


def test_a_probe_that_is_late_once_passes_its_second_run_and_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(qt, "SECOND_RUN", [])
    script, work, kept = _late_probe(tmp_path, "late-once")
    run_probe(script, work, kept, "late-once", timeout=60)
    assert (kept / "runs").read_text() == "2"
    assert [path.name for path in work.iterdir()] == ["result.txt"]  # The second run started from an empty directory,
    assert (tmp_path / "work-first-run" / "result.txt").is_file()  # and the first run's files are kept.
    [(test, why)] = qt.SECOND_RUN
    assert "test_a_probe_that_is_late_once" in test and why == "late_probe.py:14: AssertionError: too late"


def test_a_probe_that_fails_twice_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(qt, "SECOND_RUN", [])
    script, work, kept = _late_probe(tmp_path, "always")
    with pytest.raises(AssertionError, match="AssertionError"):
        run_probe(script, work, kept, "always", timeout=60)
    assert (kept / "runs").read_text() == "2" and qt.SECOND_RUN == []


@pytest.mark.parametrize("mode", ["crash", "code"])
def test_a_crashed_probe_is_not_run_again(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(qt, "SECOND_RUN", [])
    script, work, kept = _late_probe(tmp_path, mode)
    with pytest.raises(AssertionError):
        run_probe(script, work, kept, mode, timeout=60)
    assert (kept / "runs").read_text() == "1" and qt.SECOND_RUN == []


def test_the_second_run_can_be_turned_off(tmp_path, monkeypatch):
    monkeypatch.setattr(qt, "SECOND_RUN", [])
    monkeypatch.setenv("ESIBD_PROBE_SECOND_RUN", "0")
    script, work, kept = _late_probe(tmp_path, "late-once")
    with pytest.raises(AssertionError, match="too late"):
        run_probe(script, work, kept, "late-once", timeout=60)
    assert (kept / "runs").read_text() == "1"
