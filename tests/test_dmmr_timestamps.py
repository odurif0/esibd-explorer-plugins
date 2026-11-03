"""DMMR records keep increasing timestamps (2026-11-03: one equal stamp blocked every later MScan scan)."""
import numpy as np
import pytest


def record(rig, now):
    rig.clock.now = now
    token = object()
    for ch in rig.device.channels:
        ch.monitor, ch._measurement_token = 1e-12, token
    rig.module.DMMRDevice.appendData(rig.device, False, False)


def test_a_bunched_record_is_stamped_just_after_the_previous_one(dmmr_history_rig, monkeypatch):
    rig = dmmr_history_rig
    monkeypatch.setattr(rig.module.Device, "appendData", rig.append, raising=False)  # Explorer's own append
    rig.add()
    for now in (10.0, 10.0, 10.0, 9.95, 11.0):  # Same clock tick twice more, then 50 ms earlier.
        record(rig, now)
    times = rig.device.time.get()
    assert (np.diff(times) > 0).all()
    assert times == pytest.approx([10.0, 10.0 + 1e-6, 10.0 + 2e-6, 10.0 + 3e-6, 11.0], abs=1e-9)
    assert len(rig.device.channels[0].values.get()) == len(times)


def test_a_clock_adjustment_is_left_for_readers_to_skip(dmmr_history_rig, monkeypatch):
    rig = dmmr_history_rig
    monkeypatch.setattr(rig.module.Device, "appendData", rig.append, raising=False)
    rig.add()
    for now in (10.0, 11.0, 5.0):  # The clock went back 6 s: not a bunched record.
        record(rig, now)
    assert rig.device.time.get().tolist() == [10.0, 11.0, 5.0]
