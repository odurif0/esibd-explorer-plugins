"""DMMR records keep increasing timestamps (2026-11-03: one equal stamp blocked every later MScan scan)."""
from types import SimpleNamespace

import numpy as np
import pytest


def record(rig, stamps):
    """One reading per stamp, read by the worker, then taken by Explorer's timer."""
    queue = [(stamp, {0: 1e-12}, {0: 0}, object()) for stamp in stamps]
    rig.device.controller = SimpleNamespace(takeSamples=lambda: [queue.pop(0) for _ in range(len(queue))])
    rig.device.isOn = lambda: True
    rig.module.DMMRDevice.appendData(rig.device, False, False)


def test_a_bunched_reading_is_stamped_just_after_the_previous_one(dmmr_history_rig):
    rig = dmmr_history_rig
    rig.add()
    record(rig, (10.0, 10.0, 10.0, 9.95, 11.0))  # The same stamp twice more, then 50 ms earlier.
    times = rig.device.time.get()
    assert (np.diff(times) > 0).all()
    assert times == pytest.approx([10.0, 10.0 + 1e-6, 10.0 + 2e-6, 10.0 + 3e-6, 11.0], abs=1e-9)
    assert len(rig.device.channels[0].values.get()) == len(times)


def test_a_clock_adjustment_is_left_for_readers_to_skip(dmmr_history_rig):
    rig = dmmr_history_rig
    rig.add()
    record(rig, (10.0, 11.0, 5.0))  # The clock went back 6 s: not a bunched reading.
    assert rig.device.time.get().tolist() == [10.0, 11.0, 5.0]
