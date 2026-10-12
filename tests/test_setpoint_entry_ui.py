"""Setpoint typing and focus-out with real Qt and Explorer Parameter widgets.

No device connection is needed: command entry points capture the committed
requests, while panel construction, refresh and signal wiring are production code.
"""
from __future__ import annotations


import pytest
from support.qt import run_probe
from support.setpoint_ui import CASES


@pytest.mark.parametrize("folder,surface", CASES)
def test_setpoint_entry_waits_for_validation(folder, surface, tmp_path):
    run_probe(__file__, folder, surface, tmp_path, timeout=30)


