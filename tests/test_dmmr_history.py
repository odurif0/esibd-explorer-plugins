"""History retention/alignment using Explorer's actual buffers and append path.

Only the UI shell is stubbed. No installed files, user settings, or hardware
are touched. The host algorithms are extracted rather than reimplemented.
"""
from __future__ import annotations

import ast
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_dmmr_plugin_behavior import _load_module


@pytest.fixture
def rig(monkeypatch):
    try:
        host = Path(distribution("esibd-explorer").locate_file("esibd"))
    except PackageNotFoundError:
        pytest.skip("Installed Explorer sources unavailable")
    clock = SimpleNamespace(now=0.0)
    ns = {"np": np, "time": SimpleNamespace(time=lambda: clock.now), "INOUT": SimpleNamespace(IN=1, OUT=2)}

    def extract(filename, name, parent=None):
        path = host / filename
        tree = ast.parse(path.read_text())
        nodes = tree.body if parent is None else next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == parent).body
        node = next(n for n in nodes if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name)
        unit = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
        exec(compile(ast.fix_missing_locations(unit), str(path), "exec"), ns)
        return ns[name]

    buffer = extract("core.py", "DynamicNp")
    append_value = extract("core.py", "appendValue", "Channel")
    append_data = extract("plugins.py", "appendData", "Device")
    estimate = extract("plugins.py", "estimateStorage", "Device")
    module = _load_module()
    monkeypatch.setattr(module.Device, "estimateStorage", estimate, raising=False)
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.channels = []
    device.time = buffer(dtype=np.float64)  # Same allocation as Device.__init__.
    device.maxDataPoints = 100000
    device.maxStorage = 50
    device.interval = 1000
    device.useBackgrounds = False
    device.MAXDATAPOINTS = "Max data points"
    device.pluginManager = SimpleNamespace(Settings=SimpleNamespace(settings={
        "DMMR/Max data points": SimpleNamespace(getWidget=lambda: None),
    }))
    device.plotableChannels = True
    device.getChannels = lambda: device.channels
    device.updateValues = lambda: None
    device.liveDisplayActive = lambda: False
    device.measureInterval = lambda: None

    class Channel:
        useMonitors = enabled = real = True
        useBackgrounds = False
        inout = 1
        monitor = 0.0
        appendValue = append_value

    def add_channel():
        channel = Channel()
        # Channel.__init__ and Channel.clearHistory both capture this setting.
        channel.values = buffer(max_size=device.maxDataPoints)
        device.channels.append(channel)
        return channel

    def record(count, *, missing=(), gap=False):
        for _ in range(count):
            clock.now += 1
            for i, channel in enumerate(device.channels):
                channel.monitor = np.nan if int(clock.now) in missing else (clock.now + i * 1000) * 1e-12
            append_data(device, nan=gap)

    def small_limit(limit):
        monkeypatch.setattr(module.Device, "estimateStorage", lambda self: setattr(self, "maxDataPoints", limit))
        device.estimateStorage()

    return SimpleNamespace(module=module, device=device, buffer=buffer,
                           add_channel=add_channel, record=record, small_limit=small_limit)


@pytest.mark.parametrize("initial", [0, 100000])
def test_pre_discovery_capacity_is_never_zero(rig, initial):
    rig.device.maxDataPoints = initial
    rig.device.estimateStorage()
    channel = rig.add_channel()
    assert channel.values.max_size > 0
    assert rig.device.time.max_size == channel.values.max_size


@pytest.mark.parametrize("count", [1, 8])
def test_discovery_preserves_all_samples_and_their_timestamps(rig, count):
    rig.device.estimateStorage()  # No hardware detected yet.
    for _ in range(count):
        rig.add_channel()
    rig.device.estimateStorage()  # Recompute after actual discovery.
    rig.record(1000)
    np.testing.assert_array_equal(rig.device.time.get(), np.arange(1, 1001))
    for i, channel in enumerate(rig.device.channels):
        assert channel.values.size == 1000
        assert channel.values.max_size == rig.device.time.max_size == rig.device.maxDataPoints
        np.testing.assert_allclose(channel.values.get(), (np.arange(1, 1001) + i * 1000) * 1e-12, rtol=1e-6)


@pytest.mark.parametrize("limit", [20, 21, 100])
def test_time_and_values_are_thinned_together_at_capacity(rig, limit):
    rig.device.estimateStorage()
    for _ in range(3):
        rig.add_channel()
    rig.small_limit(limit)
    for _ in range(50):
        rig.record(10)
        timestamps = rig.device.time.get()
        assert 0 < len(timestamps) <= limit
        assert timestamps[-1] == (_ + 1) * 10
        assert np.all(np.diff(timestamps) > 0)
        for i, channel in enumerate(rig.device.channels):
            assert channel.values.size == len(timestamps)
            np.testing.assert_allclose(channel.values.get(), (timestamps + i * 1000) * 1e-12, rtol=1e-6)


def test_nan_samples_and_pause_gaps_keep_their_time_slots(rig):
    rig.device.estimateStorage()
    channel = rig.add_channel()
    rig.device.estimateStorage()
    rig.record(100, missing=(20, 21, 70))
    rig.record(1, gap=True)
    rig.record(10)
    expected = np.arange(1, 112, dtype=float) * 1e-12
    expected[[19, 20, 69, 100]] = np.nan
    np.testing.assert_allclose(channel.values.get(), expected, rtol=1e-6)
    assert channel.values.size == rig.device.time.size == 111


def test_missing_samples_remain_at_their_own_timestamps_after_thinning(rig):
    rig.device.estimateStorage()
    channel = rig.add_channel()
    rig.small_limit(21)
    missing = tuple(range(1, 501, 7))
    for _ in range(50):
        rig.record(10, missing=missing)
        times = rig.device.time.get()
        expected = np.where(np.isin(times, missing), np.nan, times * 1e-12)
        np.testing.assert_allclose(channel.values.get(), expected, rtol=1e-6)
        assert channel.values.size == times.size <= 21


def test_storage_edit_does_not_reset_or_rethin_an_active_history(rig):
    rig.device.estimateStorage()
    channel = rig.add_channel()
    rig.small_limit(100)
    rig.record(60)
    values, times = channel.values, rig.device.time
    old = values.get().copy()
    rig.small_limit(20)  # Explorer advertises storage changes on next restart.
    assert channel.values is values and rig.device.time is times
    np.testing.assert_array_equal(values.get(), old)
    assert values.max_size == times.max_size == 100
    rig.record(20)
    assert values.size == times.size == 80
    np.testing.assert_allclose(values.get(), times.get() * 1e-12, rtol=1e-6)


def test_existing_valid_history_is_not_cleared_when_limits_are_repaired(rig):
    channel = rig.add_channel()
    # Simulates buffers with unequal limits but still-valid existing samples.
    rig.record(40)
    values, times = channel.values, rig.device.time
    before = values.get().copy()
    rig.device.estimateStorage()
    assert channel.values is values and rig.device.time is times
    np.testing.assert_array_equal(values.get(), before)
    assert values.max_size == times.max_size > 0
    rig.record(20)
    np.testing.assert_allclose(values.get(), times.get() * 1e-12, rtol=1e-6)


def test_new_channel_aligns_with_existing_history_instead_of_inventing_samples(rig):
    rig.device.estimateStorage()
    first = rig.add_channel()
    rig.small_limit(100)
    rig.record(40)
    second = rig.add_channel()
    rig.device.estimateStorage()
    rig.record(20)
    assert np.all(np.isnan(second.values.get()[:40]))
    np.testing.assert_allclose(second.values.get()[40:], (np.arange(41, 61) + 1000) * 1e-12, rtol=1e-6)
    assert first.values.size == second.values.size == rig.device.time.size == 60
    assert first.values.max_size == second.values.max_size == rig.device.time.max_size
