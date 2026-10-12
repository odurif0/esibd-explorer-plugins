"""A real Explorer toolbar action must use the text currently in the editor."""
from __future__ import annotations

from pathlib import Path
import sys

import pytest

from support.setpoint_ui import CASES
from support.qt import run_probe


@pytest.mark.parametrize("folder,surface", CASES)
def test_action_consumes_current_field_without_focus_out(folder, surface, tmp_path):
    run_probe(__file__, folder, surface, tmp_path, timeout=30)


LOCAL_CASES = [(folder, surface) for folder, surface in CASES if surface in ('target', 'width', 'voltage', 'current_limit')]


@pytest.mark.parametrize('folder,surface', LOCAL_CASES)
def test_output_button_consumes_current_field_but_off_does_not(folder, surface, tmp_path):
    run_probe(__file__, folder, surface, tmp_path, 'local', timeout=30)


if __name__ == "__main__":
    from support.setpoint_ui import probe
    raise SystemExit(probe(sys.argv[1], sys.argv[2], Path(sys.argv[3]),
                           actions='local' if len(sys.argv) > 4 else True))
