"""The AMX output panel reads timing constants on the GUI thread every 500 ms.

On the native facade a getattr was a worker request: behind a blocked Open it froze
the GUI for up to 5 s and raised "amx operation queue timed out" (lab log 2026-10-20).
"""
from __future__ import annotations

import pytest

from support.amx import load_plugin


@pytest.mark.parametrize("folder", ["amx_a", "amx_b", "amx_hd"])
def test_timing_constants_never_wait_for_the_worker(folder):
    module = load_plugin(folder)
    driver = module._get_amx_driver_class()
    requests = []

    class BusyFacade(driver):
        def __init__(self):  # No worker: any request would be a round trip.
            pass

        def __getattr__(self, name):
            requests.append(name)
            raise TimeoutError("amx operation queue timed out")

    device = BusyFacade()
    for name, default in (("CLOCK", 100e6), ("OSC_OFFSET", 2), ("PULSER_WIDTH_OFFSET", 2),
                          ("PULSER_DELAY_OFFSET", 3)):
        assert module._safe_device_attr(device, name, default) == default
    assert requests == []


@pytest.mark.parametrize("folder", ["amx_a", "amx_hd"])
def test_a_busy_plain_device_falls_back_instead_of_raising(folder):
    module = load_plugin(folder)

    class Busy:
        def __getattr__(self, name):
            raise TimeoutError("amx operation queue timed out")

    assert module._safe_device_attr(Busy(), "CLOCK", 100e6) == 100e6
