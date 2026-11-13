"""Setpoint typing and focus-out with real Qt and Explorer Parameter widgets.

No device connection is needed: command entry points capture the committed
requests, while panel construction, refresh and signal wiring are production code.
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest
from support.qt import run_probe
from support.setpoint_ui import CASES


@pytest.mark.parametrize("folder,surface", CASES)
def test_setpoint_entry_waits_for_validation(folder, surface, tmp_path):
    run_probe(__file__, folder, surface, tmp_path, timeout=30)


if __name__ == "__main__":  # run_probe executes this file; without it the probe never ran (42418c9)
    from support.setpoint_ui import probe
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3])))
