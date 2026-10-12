"""Real Qt OFF/Disconnect controls and Explorer's Device.close after a failed Open."""
from __future__ import annotations

from pathlib import Path

import pytest
from support.qt import run_probe


@pytest.mark.parametrize('scale', ['1', '1.5'])
@pytest.mark.parametrize('action', ['disconnect', 'local_off', 'global_off', 'host_close'])
def test_failed_open_close_controls(action, scale, tmp_path):
    script = Path(__file__).with_name('test_dmmr_connection_retry_ui.py')
    run_probe(script, '-5', tmp_path, action, timeout=30, scale=scale)


