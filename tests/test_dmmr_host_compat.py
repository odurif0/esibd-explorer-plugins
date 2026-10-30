"""DMMR export against the actual 1.0.1 host, not only local Explorer 0.8.x."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from explorer_host import compile_definition

HOST = Path(__file__).parent / 'fixtures/esibd_1_0_1_export.txt'


@pytest.fixture(params=['installed', '1.0.1'])
def host_export(dmmr_history_rig, request, monkeypatch):
    if request.param == '1.0.1':
        ns = dict(np=np, Path=Path, h5py=dmmr_history_rig.h5py, PRINT=dmmr_history_rig.module.PRINT,
                  cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
                  OUTPUTCHANNELS='Output Channels', UNIT='Unit')
        exec(compile_definition(HOST, 'appendOutputData', 'Device'), ns)
        monkeypatch.setattr(dmmr_history_rig.module.Device, 'appendOutputData', ns['appendOutputData'])
    for _ in range(2):
        ch = dmmr_history_rig.add()
        ch.getRecordedParameters = lambda: []
    dmmr_history_rig.record(50, missing=(15, 22))
    dmmr_history_rig.device.liveDisplay.livePlotWidgets = [
        SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=(11., 34.)))]
    return dmmr_history_rig


@pytest.mark.parametrize('options,complete', [
    ({}, False), ({'useAllHistory': False}, False), ({'useAllHistory': True}, True),
    ({'useDefaultFile': True}, True), ({'useDefaultFile': False}, False),
    ({'useDefaultFile': True, 'useAllHistory': False}, False),
    ({'useDefaultFile': False, 'useAllHistory': True}, True),
])


def test_export_selection_keeps_time_current_range_aligned(host_export, options, complete):
    rig = host_export
    with rig.h5py.File(rig.tmp / 'selected.h5', 'w') as saved:
        rig.device.appendOutputData(saved, **options)
        times = saved['DMMR/Input Channels/Time'][:]
        expected = rig.device.time.get()[slice(None) if complete else slice(10, 33)]
        np.testing.assert_array_equal(times, expected)
        for i, ch in enumerate(rig.device.channels):
            values = saved[f'DMMR/Output Channels/{ch.name}']
            ranges = saved[values.attrs['Measurement range dataset']]
            valid = ~np.isin(times, [15, 22])
            np.testing.assert_allclose(values[:], np.where(valid, (times + i*1000)*1e-12, np.nan), rtol=1e-6)
            np.testing.assert_array_equal(ranges[:], np.where(valid, (times+i) % 5, np.nan))
            assert values.shape == ranges.shape == times.shape


def test_explorer_1_0_1_automatic_save_and_restore(dmmr_history_rig, monkeypatch):
    ns = dict(np=np, Path=Path, h5py=dmmr_history_rig.h5py, PRINT=dmmr_history_rig.module.PRINT,
              cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
              OUTPUTCHANNELS='Output Channels', UNIT='Unit')
    for name in ('appendOutputData', 'exportOutputData'):
        exec(compile_definition(HOST, name, 'Device'), ns)
    monkeypatch.setattr(dmmr_history_rig.module.Device, 'appendOutputData', ns['appendOutputData'])
    ch = dmmr_history_rig.add()
    ch.getRecordedParameters = lambda: []
    dmmr_history_rig.record(20, missing=(7,))
    dmmr_history_rig.device.liveDisplay.livePlotWidgets = [SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=(5., 10.)))]
    dmmr_history_rig.device.hdfUpdateVersion = lambda file: None
    dmmr_history_rig.device.exportConfiguration = lambda **kw: None
    # Actual host call made at configuration changes and shutdown.
    ns['exportOutputData'](dmmr_history_rig.device, useDefaultFile=True, useAllHistory=True)
    expected = ch.values.get().copy(), ch.range_history.get().copy()
    ch.clearHistory()
    dmmr_history_rig.device.restoreOutputData()
    np.testing.assert_array_equal(ch.values.get(), expected[0])
    np.testing.assert_array_equal(ch.range_history.get(), expected[1])
    assert ch.values.size == ch.range_history.size == dmmr_history_rig.device.time.size == 20


def test_hourly_store_survives_samples_arriving_during_the_export(dmmr_history_rig, monkeypatch, tmp_path):
    # 2026-10-30 lab: the hourly store runs on its own thread while the GUI thread appends, and
    # each history is read after the previous one. "DMMR range history is not aligned" aborted the
    # stores of 10:37 (DMMR_M00) and 11:37 (DMMR_M01).
    ns = dict(np=np, Path=Path, h5py=dmmr_history_rig.h5py, PRINT=dmmr_history_rig.module.PRINT,
              cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
              OUTPUTCHANNELS='Output Channels', UNIT='Unit')
    exec(compile_definition(HOST, 'appendOutputData', 'Device'), ns)
    host_append = ns['appendOutputData']
    first, second = dmmr_history_rig.add(), dmmr_history_rig.add()
    for channel in (first, second):
        channel.getRecordedParameters = lambda: []
    dmmr_history_rig.record(20)
    device = dmmr_history_rig.device
    device.liveDisplay.livePlotWidgets = []

    def appended_meanwhile(self, h5file, useAllHistory=False):
        host_append(self, h5file, useAllHistory)
        dmmr_history_rig.record(2)  # Arrived after the histories were read by the host.

    monkeypatch.setattr(dmmr_history_rig.module.Device, 'appendOutputData', appended_meanwhile)
    with dmmr_history_rig.h5py.File(tmp_path / 'store.h5', 'w') as h5file:
        device.appendOutputData(h5file, True)
    with dmmr_history_rig.h5py.File(tmp_path / 'store.h5', 'r') as h5file:
        group = h5file[device.name]
        for channel in (first, second):
            ranges = group[dmmr_history_rig.module._DMMR_RANGE_GROUP][channel.name]
            assert ranges.shape == group['Output Channels'][channel.name].shape == (20,)
            np.testing.assert_array_equal(ranges[:], channel.range_history.get()[:20])


def test_a_real_range_misalignment_is_still_refused(dmmr_history_rig, monkeypatch, tmp_path):
    ns = dict(np=np, Path=Path, h5py=dmmr_history_rig.h5py, PRINT=dmmr_history_rig.module.PRINT,
              cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
              OUTPUTCHANNELS='Output Channels', UNIT='Unit')
    exec(compile_definition(HOST, 'appendOutputData', 'Device'), ns)
    monkeypatch.setattr(dmmr_history_rig.module.Device, 'appendOutputData', ns['appendOutputData'])
    channel = dmmr_history_rig.add()
    channel.getRecordedParameters = lambda: []
    dmmr_history_rig.record(20)
    device = dmmr_history_rig.device
    device.liveDisplay.livePlotWidgets = []
    channel.range_history = type(channel.values)(initialData=np.zeros(5, dtype=np.float32),
                                                 max_size=channel.values.max_size)
    with pytest.raises(ValueError, match="not aligned"), dmmr_history_rig.h5py.File(tmp_path / 'bad.h5', 'w') as h5file:
        device.appendOutputData(h5file, True)
