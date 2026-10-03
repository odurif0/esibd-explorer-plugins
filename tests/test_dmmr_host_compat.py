"""DMMR export against the actual 1.0.1 host, not only local Explorer 0.8.x."""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_dmmr_range_history import rig  # noqa: F401
from test_plugin_history import _host_code

HOST = Path(__file__).parent / 'fixtures/esibd_1_0_1_export.txt'


@pytest.fixture(params=['installed', '1.0.1'])
def host_export(rig, request, monkeypatch):
    if request.param == '1.0.1':
        ns = dict(np=np, Path=Path, h5py=rig.h5py, PRINT=rig.module.PRINT,
                  cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
                  OUTPUTCHANNELS='Output Channels', UNIT='Unit')
        exec(_host_code(HOST, 'appendOutputData', 'Device'), ns)
        monkeypatch.setattr(rig.module.Device, 'appendOutputData', ns['appendOutputData'])
    for _ in range(2):
        ch = rig.add()
        ch.getRecordedParameters = lambda: []
    rig.record(50, missing=(15, 22))
    rig.device.liveDisplay.livePlotWidgets = [
        SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=(11., 34.)))]
    return rig


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


def test_explorer_1_0_1_automatic_save_and_restore(rig, monkeypatch):
    ns = dict(np=np, Path=Path, h5py=rig.h5py, PRINT=rig.module.PRINT,
              cast=lambda kind, value: value, INPUTCHANNELS='Input Channels',
              OUTPUTCHANNELS='Output Channels', UNIT='Unit')
    for name in ('appendOutputData', 'exportOutputData'):
        exec(_host_code(HOST, name, 'Device'), ns)
    monkeypatch.setattr(rig.module.Device, 'appendOutputData', ns['appendOutputData'])
    ch = rig.add()
    ch.getRecordedParameters = lambda: []
    rig.record(20, missing=(7,))
    rig.device.liveDisplay.livePlotWidgets = [SimpleNamespace(getAxis=lambda _: SimpleNamespace(range=(5., 10.)))]
    rig.device.hdfUpdateVersion = lambda file: None
    rig.device.exportConfiguration = lambda **kw: None
    # Actual host call made at configuration changes and shutdown.
    ns['exportOutputData'](rig.device, useDefaultFile=True, useAllHistory=True)
    expected = ch.values.get().copy(), ch.range_history.get().copy()
    ch.clearHistory()
    rig.device.restoreOutputData()
    np.testing.assert_array_equal(ch.values.get(), expected[0])
    np.testing.assert_array_equal(ch.range_history.get(), expected[1])
    assert ch.values.size == ch.range_history.size == rig.device.time.size == 20
