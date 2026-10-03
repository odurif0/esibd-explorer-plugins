"""ESI export through the exact Explorer 1.0.1 export methods, not only local 0.8.x."""
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from test_esi_plugin_behavior import _load_plugin
from test_plugin_history import _host_code

HOST = Path(__file__).parent / "fixtures/esibd_1_0_1_export.txt"


@pytest.fixture
def esi(tmp_path, monkeypatch):
    h5py = pytest.importorskip("h5py")
    module = _load_plugin()
    const = ModuleType("esibd.const")
    const.OUTPUTCHANNELS, const.UNIT = "Output Channels", "Unit"
    monkeypatch.setitem(sys.modules, "esibd.const", const)
    received = []
    # The host's own writer: record which history selection reached it.
    monkeypatch.setattr(module.Device, "appendOutputData",
                        lambda self, h5file, useAllHistory=False: received.append(useAllHistory), raising=False)
    device = module.ESIDevice.__new__(module.ESIDevice)
    device.liveDisplay = SimpleNamespace(previewFileTypes=[".h5"])
    device.time = SimpleNamespace(get=lambda: np.arange(3.0))
    device.confh5 = "_ESI.h5"
    device.pluginManager = SimpleNamespace(Settings=SimpleNamespace(configPath=tmp_path))
    device.hdfUpdateVersion = lambda h5file: None
    device.exportConfiguration = lambda **kwargs: None
    device.print = lambda *args, **kwargs: None
    device.getDataChannels = lambda: []
    device.useBackgrounds = False
    return SimpleNamespace(module=module, device=device, received=received, h5py=h5py, tmp=tmp_path)


@pytest.mark.parametrize("history", [True, False])
def test_explorer_1_0_1_export_reaches_the_esi_override(esi, history):
    ns = dict(np=np, Path=Path, h5py=esi.h5py, PRINT=esi.module.PRINT, cast=lambda kind, value: value)
    exec(_host_code(HOST, "exportOutputData", "Device"), ns)
    # Exactly the call Explorer 1.0.1 makes at shutdown and on Ctrl+S.
    ns["exportOutputData"](esi.device, useDefaultFile=True, useAllHistory=history)
    assert esi.received == [history]


@pytest.mark.parametrize("kwargs,expected", [
    ({}, False), ({"useAllHistory": True}, True), ({"useDefaultFile": True}, True),
    ({"useDefaultFile": True, "useAllHistory": False}, False),
])
def test_both_host_call_conventions_select_the_same_history(esi, kwargs, expected):
    with esi.h5py.File(esi.tmp / "data.h5", "w") as h5file:
        esi.device.appendOutputData(h5file, **kwargs)
    assert esi.received == [expected]
