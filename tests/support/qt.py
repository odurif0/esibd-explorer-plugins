"""Run a real-Qt probe script in a fresh interpreter (offscreen, Explorer host Python)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import explorer_host

SKIP = 77  # A probe exits 77 when real PyQt6 or the Explorer sources are unavailable.


def run_probe(script, *args, timeout: float, scale: str | None = None, env: dict | None = None,
              allow_traceback: bool = False) -> subprocess.CompletedProcess:
    """Run ``script`` (usually the calling test file) and fail with its output unless it exits 0."""
    environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", **(env or {})}
    if scale is not None:
        environment["QT_SCALE_FACTOR"] = scale
    result = subprocess.run([explorer_host.qt_python(), str(Path(script).resolve()), *map(str, args)], env=environment,
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode == SKIP:
        pytest.skip((result.stdout + result.stderr).strip() or "Real Qt/Explorer unavailable")
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]
    if not allow_traceback:
        assert "Traceback" not in result.stderr, result.stderr[-6000:]
    return result
