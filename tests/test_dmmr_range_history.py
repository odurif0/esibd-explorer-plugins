"""Range/current/time alignment through Explorer's real buffers and HDF exporter."""
from types import SimpleNamespace

import numpy as np
import pytest



@pytest.mark.parametrize("limit", [20, 21, 100])
def test_alignment_through_thinning_missing_samples_and_late_channels(dmmr_history_rig, limit):
    dmmr_history_rig.add()
    dmmr_history_rig.small_limit(limit)
    dmmr_history_rig.record(17)
    late = dmmr_history_rig.add()
    missing = tuple(range(20, 501, 7))
    for step in range(40):
        dmmr_history_rig.record(10, missing=missing)
        times = dmmr_history_rig.device.time.get()
        for i, ch in enumerate(dmmr_history_rig.device.channels):
            valid = ~np.isin(times, missing)
            if ch is late:
                valid &= times > 17
            expected = np.where(valid, (times + i * 1000) * 1e-12, np.nan)
            np.testing.assert_allclose(ch.values.get(), expected, rtol=1e-6)
            np.testing.assert_array_equal(ch.range_history.get(), np.where(valid, (times + i) % 5, np.nan))
            assert ch.range_history.size == ch.values.size == dmmr_history_rig.device.time.size <= limit


def test_recording_timer_does_not_repeat_a_poll_or_replace_muted_current_with_reference(dmmr_history_rig):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(1)
    # The host appends the same monitor on a second timer tick (no new polling).
    previous = ch.values
    ch.values = dmmr_history_rig.buffer()
    dmmr_history_rig.host_append(ch, lenT=0)
    dmmr_history_rig.host_append(ch, lenT=1)
    np.testing.assert_array_equal(ch.values.get(), np.array([ch.monitor, ch.monitor], dtype=np.float32))
    ch.values = previous
    dmmr_history_rig.clock.now += 1
    dmmr_history_rig.append(dmmr_history_rig.device)
    assert np.isnan(ch.values.get()[-1]) and np.isnan(ch.range_history.get()[-1])
    assert ch.monitor == 1e-12, "Do not erase the live monitor just because the recorder ran first"
    # Identical numeric values from a new polling cycle remain valid.
    ch._measurement_token = object()
    dmmr_history_rig.clock.now += 1
    dmmr_history_rig.append(dmmr_history_rig.device)
    assert ch.values.get()[-1] == pytest.approx(1e-12)
    assert ch.range_history.get()[-1] == 1
    ch.enabled = False
    dmmr_history_rig.record(1)
    assert np.isnan(ch.values.get()[-1]) and np.isnan(ch.range_history.get()[-1])
    assert ch.values.size == ch.range_history.size == dmmr_history_rig.device.time.size


def test_no_recorded_current_before_the_controller_publishes_a_sample(dmmr_history_rig):
    ch = dmmr_history_rig.add()
    ch.monitor = 0.0  # A freshly constructed widget is not a hardware reading.
    dmmr_history_rig.clock.now = 1
    dmmr_history_rig.append(dmmr_history_rig.device)
    assert np.isnan(ch.values.get()[0])
    assert np.isnan(ch.range_history.get()[0])


def test_pause_and_clear_history_keep_ranges_aligned(dmmr_history_rig):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(8)
    dmmr_history_rig.record(1, gap=True)
    dmmr_history_rig.record(2)
    assert np.isnan(ch.values.get()[8]) and np.isnan(ch.range_history.get()[8])
    ch.clearHistory()
    dmmr_history_rig.device.time = dmmr_history_rig.buffer(dtype=np.float64, max_size=dmmr_history_rig.device.maxDataPoints)
    assert ch.values.size == ch.range_history.size == 0
    dmmr_history_rig.record(3)
    np.testing.assert_array_equal(ch.range_history.get(), dmmr_history_rig.device.time.get() % 5)


@pytest.mark.parametrize("window", [None, (11., 34.), (1., 20.)])
def test_hdf_pairs_have_the_exact_same_export_window_as_currents(dmmr_history_rig, window):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(50, missing=(15, 22))
    if window:
        dmmr_history_rig.device.liveDisplay.livePlotWidgets = [SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=window))]
    file = dmmr_history_rig.save(default=window is None)
    with dmmr_history_rig.h5py.File(file) as data:
        values = data["DMMR/Output Channels/DMMR_M00"]
        ranges = data[values.attrs["Measurement range dataset"]]
        times = data["DMMR/Input Channels/Time"][:]
        assert values.shape == ranges.shape == times.shape
        np.testing.assert_allclose(values[:], np.where(np.isin(times, [15, 22]), np.nan, times * 1e-12), rtol=1e-6)
        np.testing.assert_array_equal(ranges[:], np.where(np.isin(times, [15, 22]), np.nan, times % 5))
        assert ranges.attrs["Current dataset"] == values.name
        assert ranges.dtype == np.dtype("float32")
        assert set(data["DMMR/Output Channels"]) == {ch.name}, "Ranges must not become fake current channels"


@pytest.mark.parametrize("legacy", [False, True])
def test_restore_preserves_known_ranges_and_leaves_legacy_ranges_unknown(dmmr_history_rig, legacy):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(15)
    file = dmmr_history_rig.save()
    expected_values = ch.values.get().copy()
    expected_ranges = ch.range_history.get().copy()
    if legacy:
        with dmmr_history_rig.h5py.File(file, "a") as data:
            del data["DMMR/Measurement ranges"]
    ch.clearHistory()
    dmmr_history_rig.device.restoreOutputData()
    np.testing.assert_array_equal(ch.values.get(), expected_values)
    np.testing.assert_array_equal(ch.range_history.get(), np.full(15, np.nan) if legacy else expected_ranges)
    dmmr_history_rig.record(2)
    assert ch.values.size == ch.range_history.size == dmmr_history_rig.device.time.size == 17


@pytest.mark.parametrize("corruption", ["length", "invalid"])
def test_invalid_range_history_is_not_attached_to_restored_current(dmmr_history_rig, corruption):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(10)
    file = dmmr_history_rig.save()
    with dmmr_history_rig.h5py.File(file, "a") as data:
        path = "DMMR/Measurement ranges/DMMR_M00"
        if corruption == "length":
            del data[path]
            data.create_dataset(path, data=np.arange(3.))
        else:
            data[path][0] = 5
    dmmr_history_rig.device.restoreOutputData()
    assert ch.values.size == ch.range_history.size == 10
    assert np.isnan(ch.range_history.get()).all()
    assert any("Ignoring invalid range history" in message for message in dmmr_history_rig.messages)


def test_duplicate_export_does_not_relabel_existing_measurements(dmmr_history_rig):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.record(10)
    file = dmmr_history_rig.save()
    original = ch.range_history.get().copy()
    dmmr_history_rig.record(10)
    with dmmr_history_rig.h5py.File(file, "a") as data:
        dmmr_history_rig.device.appendOutputData(data, useDefaultFile=True)
        np.testing.assert_array_equal(data["DMMR/Measurement ranges/DMMR_M00"][:], original)
        assert data["DMMR/Output Channels/DMMR_M00"].shape == (10,)


@pytest.mark.parametrize("count", [1, 8])
@pytest.mark.parametrize("backgrounds", [False, True])
def test_storage_estimate_budgets_the_range_series_and_synchronizes_all_buffers(dmmr_history_rig, count, backgrounds):
    dmmr_history_rig.device.useBackgrounds = backgrounds
    for _ in range(count):
        ch = dmmr_history_rig.add()
        ch._range_buffer()
    dmmr_history_rig.device.estimateStorage()
    # Payload: one float64 timestamp, plus current/range and optional BG float32.
    bytes_per_point = 8 + count * (12 if backgrounds else 8)
    payload = dmmr_history_rig.device.maxDataPoints * bytes_per_point
    assert 0 < 50 * 1024**2 - payload <= 2 * bytes_per_point
    for ch in dmmr_history_rig.device.channels:
        assert ch.values.max_size == ch.range_history.max_size == dmmr_history_rig.device.time.max_size == dmmr_history_rig.device.maxDataPoints


def test_storage_changes_do_not_rethin_or_reset_current_range_pairs(dmmr_history_rig):
    ch = dmmr_history_rig.add()
    dmmr_history_rig.small_limit(100)
    dmmr_history_rig.record(60)
    old = ch.range_history.get().copy()
    identity = ch.range_history
    dmmr_history_rig.device.maxStorage = 10
    dmmr_history_rig.device.estimateStorage()
    assert ch.range_history is identity
    np.testing.assert_array_equal(ch.range_history.get(), old)
    assert ch.values.max_size == ch.range_history.max_size == dmmr_history_rig.device.time.max_size == 100
    dmmr_history_rig.record(20)
    assert ch.values.size == ch.range_history.size == dmmr_history_rig.device.time.size == 80
    np.testing.assert_array_equal(ch.range_history.get(), dmmr_history_rig.device.time.get() % 5)


@pytest.mark.parametrize("remap", [False, True])
def test_channel_rebuild_preserves_ranges_only_for_the_same_channel_and_module(dmmr_history_rig, remap):
    old = dmmr_history_rig.add()
    dmmr_history_rig.record(20)
    values = old.values.get().copy()
    history = old.range_history
    device = dmmr_history_rig.device
    device.tree = None
    device.inout = 1
    device.pluginManager.DeviceManager = SimpleNamespace(globalUpdate=lambda **kw: None)
    for method in ("_hide_channel_table", "_hide_channel_table_actions", "_update_channel_column_visibility",
                   "_ensure_channel_panel", "processEvents"):
        setattr(device, method, lambda: None)

    def rebuild(items, file, append=False):
        # Explorer rebuilds Channel objects but retains histories by name.
        device.channels = []
        ch = dmmr_history_rig.add()
        ch.values = dmmr_history_rig.buffer(initialData=values, max_size=1000)
        ch.module = 3 if remap else 0
    device.updateChannelConfig = rebuild
    device._apply_channel_items([], file=dmmr_history_rig.tmp / "channels.ini")
    new = device.channels[0]
    assert new is not old
    if remap:
        assert np.isnan(new._range_buffer().get()).all()
    else:
        assert new.range_history is history
        np.testing.assert_array_equal(new.range_history.get(), device.time.get() % 5)
