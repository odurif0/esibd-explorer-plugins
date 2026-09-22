"""Range/current/time alignment through Explorer's real buffers and HDF exporter."""
from importlib.metadata import distribution
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import numpy as np
import pytest

from test_dmmr_plugin_behavior import _load_module
from test_plugin_history import _host_code


@pytest.fixture
def rig(tmp_path, monkeypatch):
    h5py = pytest.importorskip("h5py")
    host = Path(distribution("esibd-explorer").locate_file("esibd"))
    clock = SimpleNamespace(now=0.0)
    ns = dict(np=np, Path=Path, h5py=h5py, cast=lambda kind, value: value,
              time=SimpleNamespace(time=lambda: clock.now),
              INOUT=SimpleNamespace(IN=1, OUT=2), INPUTCHANNELS="Input Channels",
              OUTPUTCHANNELS="Output Channels", UNIT="Unit")

    def extract(file, name, parent=None):
        exec(_host_code(host / file, name, parent), ns)
        return ns[name]

    buffer = extract("core.py", "DynamicNp")
    host_append = extract("core.py", "appendValue", "Channel")
    host_clear = extract("core.py", "clearHistory", "Channel")
    append_data = extract("plugins.py", "appendData", "Device")
    exporter = extract("plugins.py", "appendOutputData", "Device")
    restore = extract("plugins.py", "restoreOutputData", "Device")
    estimate = extract("plugins.py", "estimateStorage", "Device")
    module = _load_module()
    ns["PRINT"] = module.PRINT
    monkeypatch.setattr(module.Channel, "appendValue", host_append, raising=False)
    monkeypatch.setattr(module.Channel, "clearHistory", host_clear, raising=False)
    monkeypatch.setattr(module.Device, "appendOutputData", exporter, raising=False)
    monkeypatch.setattr(module.Device, "restoreOutputData", restore, raising=False)
    monkeypatch.setattr(module.Device, "estimateStorage", estimate, raising=False)
    monkeypatch.setattr(sys.modules["esibd.core"], "DynamicNp", buffer, raising=False)
    const = ModuleType("esibd.const")
    const.INPUTCHANNELS, const.OUTPUTCHANNELS = ns["INPUTCHANNELS"], ns["OUTPUTCHANNELS"]
    monkeypatch.setitem(sys.modules, "esibd.const", const)
    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.channels = []
    device.time = buffer(dtype=np.float64, max_size=1000)
    device.maxDataPoints = 1000
    device.MAXDATAPOINTS = "Max data points"
    device.maxStorage = 50
    device.interval = 1000
    device.TIME = "Time"
    device.confh5 = "_DMMR.h5"
    device.useBackgrounds = False
    device.pluginManager = SimpleNamespace(Settings=SimpleNamespace(configPath=tmp_path, loading=False),
                                           Device=module.DMMRDevice, DeviceManager=True)
    device.pluginManager.Settings.settings = {
        "DMMR/Max data points": SimpleNamespace(getWidget=lambda: None),
    }
    device.getChannels = device.getDataChannels = lambda: device.channels
    device.getChannelByName = lambda name: next((ch for ch in device.channels if ch.name == name), None)
    device.liveDisplay = SimpleNamespace(livePlotWidgets=[])
    device.requireGroup = lambda owner, name: owner.require_group(name)
    device.plotableChannels = True
    device.updateValues = lambda: None
    device.liveDisplayActive = lambda: False
    device.measureInterval = lambda: None
    messages = []
    device.print = lambda msg, **kw: messages.append(msg)

    def add_channel():
        ch = module.DMMRChannel.__new__(module.DMMRChannel)
        ch.channelParent = device
        ch.pluginManager = device.pluginManager
        ch.module = len(device.channels)
        ch.name = f"DMMR_M{ch.module:02}"
        ch.real = ch.enabled = ch.useMonitors = True
        ch.useBackgrounds = False
        ch.inout = 1
        ch.value = 999.0  # Never record the unused reference when Read is muted.
        ch.monitor = np.nan
        ch.clearPlotCurve = lambda: None
        ch.values = buffer(max_size=device.maxDataPoints)
        device.channels.append(ch)
        return ch

    def record(count, *, missing=(), gap=False):
        for _ in range(count):
            clock.now += 1
            token = object()
            for i, ch in enumerate(device.channels):
                ch.monitor = np.nan if clock.now in missing else (clock.now + i * 1000) * 1e-12
                ch.measurement_range = int(clock.now + i) % 5
                ch._measurement_token = token
            append_data(device, nan=gap)

    def small_limit(limit):
        device.maxDataPoints = device.time.max_size = limit
        for ch in device.channels:
            ch.values.max_size = ch._range_buffer().max_size = limit

    def save(name="DMMR.h5", default=True):
        file = tmp_path / name
        with h5py.File(file, "w") as data:
            device.appendOutputData(data, useDefaultFile=default)
        return file

    return SimpleNamespace(module=module, device=device, clock=clock, buffer=buffer,
                           record=record, add=add_channel, small_limit=small_limit,
                           append=append_data, host_append=host_append, save=save,
                           h5py=h5py, tmp=tmp_path, messages=messages)


@pytest.mark.parametrize("limit", [20, 21, 100])
def test_alignment_through_thinning_missing_samples_and_late_channels(rig, limit):
    rig.add()
    rig.small_limit(limit)
    rig.record(17)
    late = rig.add()
    missing = tuple(range(20, 501, 7))
    for step in range(40):
        rig.record(10, missing=missing)
        times = rig.device.time.get()
        for i, ch in enumerate(rig.device.channels):
            valid = ~np.isin(times, missing)
            if ch is late:
                valid &= times > 17
            expected = np.where(valid, (times + i * 1000) * 1e-12, np.nan)
            np.testing.assert_allclose(ch.values.get(), expected, rtol=1e-6)
            np.testing.assert_array_equal(ch.range_history.get(), np.where(valid, (times + i) % 5, np.nan))
            assert ch.range_history.size == ch.values.size == rig.device.time.size <= limit


def test_recording_timer_does_not_repeat_a_poll_or_replace_muted_current_with_reference(rig):
    ch = rig.add()
    rig.record(1)
    # The host appends the same monitor on a second timer tick (no new polling).
    previous = ch.values
    ch.values = rig.buffer()
    rig.host_append(ch, lenT=0)
    rig.host_append(ch, lenT=1)
    np.testing.assert_array_equal(ch.values.get(), np.array([ch.monitor, ch.monitor], dtype=np.float32))
    ch.values = previous
    rig.clock.now += 1
    rig.append(rig.device)
    assert np.isnan(ch.values.get()[-1]) and np.isnan(ch.range_history.get()[-1])
    assert ch.monitor == 1e-12, "Do not erase the live monitor just because the recorder ran first"
    # Identical numeric values from a new polling cycle remain valid.
    ch._measurement_token = object()
    rig.clock.now += 1
    rig.append(rig.device)
    assert ch.values.get()[-1] == pytest.approx(1e-12)
    assert ch.range_history.get()[-1] == 1
    ch.enabled = False
    rig.record(1)
    assert np.isnan(ch.values.get()[-1]) and np.isnan(ch.range_history.get()[-1])
    assert ch.values.size == ch.range_history.size == rig.device.time.size


def test_no_recorded_current_before_the_controller_publishes_a_sample(rig):
    ch = rig.add()
    ch.monitor = 0.0  # A freshly constructed widget is not a hardware reading.
    rig.clock.now = 1
    rig.append(rig.device)
    assert np.isnan(ch.values.get()[0])
    assert np.isnan(ch.range_history.get()[0])


def test_pause_and_clear_history_keep_ranges_aligned(rig):
    ch = rig.add()
    rig.record(8)
    rig.record(1, gap=True)
    rig.record(2)
    assert np.isnan(ch.values.get()[8]) and np.isnan(ch.range_history.get()[8])
    ch.clearHistory()
    rig.device.time = rig.buffer(dtype=np.float64, max_size=rig.device.maxDataPoints)
    assert ch.values.size == ch.range_history.size == 0
    rig.record(3)
    np.testing.assert_array_equal(ch.range_history.get(), rig.device.time.get() % 5)


@pytest.mark.parametrize("window", [None, (11., 34.), (1., 20.)])
def test_hdf_pairs_have_the_exact_same_export_window_as_currents(rig, window):
    ch = rig.add()
    rig.record(50, missing=(15, 22))
    if window:
        rig.device.liveDisplay.livePlotWidgets = [SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=window))]
    file = rig.save(default=window is None)
    with rig.h5py.File(file) as data:
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
def test_restore_preserves_known_ranges_and_leaves_legacy_ranges_unknown(rig, legacy):
    ch = rig.add()
    rig.record(15)
    file = rig.save()
    expected_values = ch.values.get().copy()
    expected_ranges = ch.range_history.get().copy()
    if legacy:
        with rig.h5py.File(file, "a") as data:
            del data["DMMR/Measurement ranges"]
    ch.clearHistory()
    rig.device.restoreOutputData()
    np.testing.assert_array_equal(ch.values.get(), expected_values)
    np.testing.assert_array_equal(ch.range_history.get(), np.full(15, np.nan) if legacy else expected_ranges)
    rig.record(2)
    assert ch.values.size == ch.range_history.size == rig.device.time.size == 17


@pytest.mark.parametrize("corruption", ["length", "invalid"])
def test_invalid_range_history_is_not_attached_to_restored_current(rig, corruption):
    ch = rig.add()
    rig.record(10)
    file = rig.save()
    with rig.h5py.File(file, "a") as data:
        path = "DMMR/Measurement ranges/DMMR_M00"
        if corruption == "length":
            del data[path]
            data.create_dataset(path, data=np.arange(3.))
        else:
            data[path][0] = 5
    rig.device.restoreOutputData()
    assert ch.values.size == ch.range_history.size == 10
    assert np.isnan(ch.range_history.get()).all()
    assert any("Ignoring invalid range history" in message for message in rig.messages)


def test_duplicate_export_does_not_relabel_existing_measurements(rig):
    ch = rig.add()
    rig.record(10)
    file = rig.save()
    original = ch.range_history.get().copy()
    rig.record(10)
    with rig.h5py.File(file, "a") as data:
        rig.device.appendOutputData(data, useDefaultFile=True)
        np.testing.assert_array_equal(data["DMMR/Measurement ranges/DMMR_M00"][:], original)
        assert data["DMMR/Output Channels/DMMR_M00"].shape == (10,)


@pytest.mark.parametrize("count", [1, 8])
@pytest.mark.parametrize("backgrounds", [False, True])
def test_storage_estimate_budgets_the_range_series_and_synchronizes_all_buffers(rig, count, backgrounds):
    rig.device.useBackgrounds = backgrounds
    for _ in range(count):
        ch = rig.add()
        ch._range_buffer()
    rig.device.estimateStorage()
    # Payload: one float64 timestamp, plus current/range and optional BG float32.
    bytes_per_point = 8 + count * (12 if backgrounds else 8)
    payload = rig.device.maxDataPoints * bytes_per_point
    assert 0 < 50 * 1024**2 - payload <= 2 * bytes_per_point
    for ch in rig.device.channels:
        assert ch.values.max_size == ch.range_history.max_size == rig.device.time.max_size == rig.device.maxDataPoints


def test_storage_changes_do_not_rethin_or_reset_current_range_pairs(rig):
    ch = rig.add()
    rig.small_limit(100)
    rig.record(60)
    old = ch.range_history.get().copy()
    identity = ch.range_history
    rig.device.maxStorage = 10
    rig.device.estimateStorage()
    assert ch.range_history is identity
    np.testing.assert_array_equal(ch.range_history.get(), old)
    assert ch.values.max_size == ch.range_history.max_size == rig.device.time.max_size == 100
    rig.record(20)
    assert ch.values.size == ch.range_history.size == rig.device.time.size == 80
    np.testing.assert_array_equal(ch.range_history.get(), rig.device.time.get() % 5)


@pytest.mark.parametrize("remap", [False, True])
def test_channel_rebuild_preserves_ranges_only_for_the_same_channel_and_module(rig, remap):
    old = rig.add()
    rig.record(20)
    values = old.values.get().copy()
    history = old.range_history
    device = rig.device
    device.tree = None
    device.inout = 1
    device.pluginManager.DeviceManager = SimpleNamespace(globalUpdate=lambda **kw: None)
    for method in ("_hide_channel_table", "_hide_channel_table_actions", "_update_channel_column_visibility",
                   "_ensure_channel_panel", "processEvents"):
        setattr(device, method, lambda: None)

    def rebuild(items, file, append=False):
        # Explorer rebuilds Channel objects but retains histories by name.
        device.channels = []
        ch = rig.add()
        ch.values = rig.buffer(initialData=values, max_size=1000)
        ch.module = 3 if remap else 0
    device.updateChannelConfig = rebuild
    device._apply_channel_items([], file=rig.tmp / "channels.ini")
    new = device.channels[0]
    assert new is not old
    if remap:
        assert np.isnan(new._range_buffer().get()).all()
    else:
        assert new.range_history is history
        np.testing.assert_array_equal(new.range_history.get(), device.time.get() % 5)
