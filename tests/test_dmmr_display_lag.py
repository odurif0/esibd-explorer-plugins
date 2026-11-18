"""Every DMMR reading is recorded once, at the time it was read; a late Qt thread delays the display only.

Explorer 1.0.2 recorded the latest value from a timer of its Qt thread, stamped with the time of the call.
Lab 2026-11-17: the DMMR answered 4.9 times a second for 30 h while the share of NaN on M03 grew from 5 to
38 %; Explorer's lag guard then paused recording 25 times and closed the DMMR at 13:40 (as on 2026-10-21).
"""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from support import explorer


def _controller(module, recording=True):
    controller = module.DMMRController.__new__(module.DMMRController)
    controller.controllerParent = SimpleNamespace(recording=recording)
    from collections import deque
    from threading import RLock
    controller._sample_lock = RLock()
    controller.values, controller.meas_ranges = {}, {}
    controller._sample_token = object()
    controller._pending_samples = deque(maxlen=module._DMMR_PENDING_SAMPLES)
    controller._samples_dropped = 0
    controller.messages = []
    controller.print = lambda text, flag=None: controller.messages.append(text)
    return controller


def test_the_dmmr_never_pauses_counts_or_closes_for_a_late_display():
    module = explorer.load_plugin("dmmr/dmmr_plugin.py", "dmmr_display_lag_test", runtimes=("dmmr",))
    device = object.__new__(module.DMMRDevice)
    device.controller = SimpleNamespace(maxErrorCount=25, errorCount=0)
    device.lastIntervalTime = 0.  # The Qt thread has not come back for 56 years.
    device.lagging = 41
    device.closeCommunication = lambda: pytest.fail("a late display closed the DMMR")
    assert device.bufferLagging() is False and device.bufferLagging(5) is False
    device.measureInterval(reset=False)
    assert device.lagging == 0 and device.lagging_seconds == 0
    assert device.controller.errorCount == 0


def test_readings_are_queued_with_their_time_only_while_recording():
    module = explorer.load_plugin("dmmr/dmmr_plugin.py", "dmmr_samples_test", runtimes=("dmmr",))
    controller = _controller(module)
    controller._publish_sample({0: 1e-12}, {0: 2}, 10.5)
    controller._publish_sample({0: 2e-12}, {0: 2}, 10.7)
    samples = controller.takeSamples()
    assert [(s[0], s[1], s[2]) for s in samples] == [(10.5, {0: 1e-12}, {0: 2}), (10.7, {0: 2e-12}, {0: 2})]
    assert samples[0][3] is not samples[1][3], "one token per reading"
    assert controller.measurementSnapshot()[0] == {0: 2e-12}, "the live value is the latest reading"
    assert controller.takeSamples() == []
    idle = _controller(module, recording=False)
    idle._publish_sample({0: 1e-12}, {0: 2}, 10.5)
    assert idle.takeSamples() == [] and idle.measurementSnapshot()[0] == {0: 1e-12}


def test_a_qt_thread_away_for_too_long_is_told_how_many_readings_were_not_recorded():
    module = explorer.load_plugin("dmmr/dmmr_plugin.py", "dmmr_overflow_test", runtimes=("dmmr",))
    controller = _controller(module)
    for i in range(module._DMMR_PENDING_SAMPLES + 3):
        controller._publish_sample({0: float(i)}, {0: 2}, float(i))
    samples = controller.takeSamples()
    assert len(samples) == module._DMMR_PENDING_SAMPLES and samples[0][0] == 3.
    assert controller.messages and controller.messages[0].startswith("3 DMMR readings were not recorded")


def test_each_reading_becomes_one_history_row_at_its_own_time(dmmr_history_rig):
    """Explorer's own buffers and channel append: several readings per timer call, or none."""
    rig = dmmr_history_rig
    rig.add()
    rig.add()
    device, module = rig.device, rig.module
    queue = []
    device.controller = SimpleNamespace(takeSamples=lambda: [queue.pop(0) for _ in range(len(queue))])
    device.isOn = lambda: True
    readings = [(100.0, {0: 1e-12, 1: 5e-12}, {0: 0, 1: 1}), (100.2, {0: 2e-12, 1: np.nan}, {0: 0, 1: np.nan}),
                (100.4, {0: 3e-12, 1: 6e-12}, {0: 0, 1: 1})]
    queue.extend((t, v, r, object()) for t, v, r in readings[:2])  # two readings before the first call
    module.DMMRDevice.appendData(device, False, False)
    module.DMMRDevice.appendData(device, False, False)  # no new reading: no row
    queue.append((*readings[2], object()))
    module.DMMRDevice.appendData(device, False, False)
    assert device.time.get().tolist() == [100.0, 100.2, 100.4]
    m0, m1 = (ch.values.get() for ch in device.channels)
    assert m0.tolist() == pytest.approx([1e-12, 2e-12, 3e-12])
    assert m1[0] == pytest.approx(5e-12) and np.isnan(m1[1]) and m1[2] == pytest.approx(6e-12)
    assert device.channels[1]._range_buffer().get().tolist()[::2] == [1, 1]
    assert device._reading_interval_ms == pytest.approx(200)  # what "Interval (measured)" shows


def test_a_reading_stamped_before_the_last_row_still_follows_it(dmmr_history_rig):
    rig = dmmr_history_rig
    rig.add()
    device, module = rig.device, rig.module
    device.controller = SimpleNamespace(takeSamples=lambda: [(50.0, {0: 1e-12}, {0: 0}, object()),
                                                             (50.0, {0: 2e-12}, {0: 0}, object())])
    device.isOn = lambda: True
    module.DMMRDevice.appendData(device, False, False)
    times = device.time.get()
    assert (np.diff(times) > 0).all() and times[0] == 50.0
