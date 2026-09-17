"""Literal labels must survive configuration saves, restarts and module discovery."""
from __future__ import annotations

import ast
import configparser
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from test_dmmr_plugin_behavior import _load_module


@pytest.fixture
def rig(tmp_path, monkeypatch):
    module = _load_module()
    const = ModuleType("esibd.const")
    const.infoDict = lambda name: {"Name": name}
    monkeypatch.setitem(sys.modules, "esibd.const", const)
    types = module.PARAMETERTYPE
    kinds = {"Name": types.TEXT, "Module": types.INT, "Label": types.LABEL,
             "Color": types.TEXT, "Display": types.BOOL, "Enabled": types.BOOL, "Value": types.FLOAT}

    class Channel:
        real = True
        def __init__(self, values):
            self.data = {"Name": "DMMR_M00", "Module": 0, "Label": "", "Color": "#bb4477",
                         "Display": True, "Enabled": True, "Value": 1.5e-12}
            self.data.update({key: values[key] for key in kinds if key in values})
            self.name = self.data["Name"]
        def asDict(self, **kwargs): return dict(self.data)
        def getParameterByName(self, name): return SimpleNamespace(value=self.data[name], parameterType=kinds[name])
        def module_address(self): return int(self.data["Module"])
        def getParameterNames(self): return kinds

    device = module.DMMRDevice.__new__(module.DMMRDevice)
    device.confINI, device.UTF8, device.CHANNEL = "DMMR.ini", "utf-8", "Channel"
    device.channels = [Channel({"Name": f"DMMR_M{i:02}", "Module": i}) for i in range(2)]
    device.getChannels = lambda: device.channels
    device.customConfigFile = lambda name: tmp_path / name
    device.pluginManager = SimpleNamespace(loading=True)
    device.initialized = False
    device.hdfUpdateVersion = lambda file: None
    device.requireGroup = lambda file, name: file.require_group(name)
    device._default_channel_template = lambda: dict.fromkeys(kinds)
    device._hide_channel_table = device._hide_channel_table_actions = device._ensure_channel_panel = lambda: None
    device.print = lambda *args, **kwargs: None
    applications = []
    def apply(items, **kwargs):
        applications.append(kwargs)
        device.channels = [Channel(item) for item in items]
    device._apply_channel_items = apply
    return SimpleNamespace(module=module, device=device, const=const, Channel=Channel, applications=applications, tmp=tmp_path)


LABELS = ["", "Collecteur", "Électrode µA — 50%", "%(not_interpolation)s", "入口 α / collecteur 🧪"]


@pytest.mark.parametrize("label", LABELS)
@pytest.mark.parametrize("extension", [".ini", ".h5"])
def test_labels_survive_configuration_reload(rig, label, extension):
    if extension == ".h5":
        h5py = pytest.importorskip("h5py")
    rig.device.channels[0].data["Label"] = label
    rig.device.channels[1].data["Label"] = "Autre module"
    before = [dict(c.data) for c in rig.device.channels]
    file = rig.tmp / ("labels" + extension)
    rig.device.exportConfiguration(file=file)
    if extension == ".ini":
        # Explorer's existing ConfigParser can still read the file.
        config = configparser.ConfigParser()
        config.read(file, encoding="utf-8")
        assert config["Channel_000"]["Label"] == label
    else:
        with h5py.File(file, "r") as data:
            group = data["DMMR"]
            assert list(group["Label"].asstr()[:]) == [label, "Autre module"]
            assert group["Module"].dtype == np.dtype("int32")
            assert group["Value"].dtype == np.dtype("float32")
            assert group["Display"].dtype == np.dtype("bool")
    rig.device.loadConfiguration(file=file, append=True)
    assert rig.applications == [{"file": file, "append": True}]
    for old, new in zip(before, rig.device.channels):
        assert new.data["Label"] == old["Label"]
        assert new.name == old["Name"]
        assert new.module_address() == old["Module"]
        assert new.data["Color"] == old["Color"]
        assert float(new.data["Value"]) == pytest.approx(old["Value"], rel=1e-6, abs=0)


@pytest.mark.parametrize("extension", [".ini", ".h5"])
def test_configuration_without_labels_remains_loadable(rig, extension):
    file = rig.tmp / ("legacy" + extension)
    rig.device.exportConfiguration(file=file)
    if extension == ".ini":
        config = configparser.ConfigParser()
        config.read(file, encoding="utf-8")
        for section in ("Channel_000", "Channel_001"):
            del config[section]["Label"]
        with file.open("w", encoding="utf-8") as output:
            config.write(output)
    else:
        h5py = pytest.importorskip("h5py")
        with h5py.File(file, "a") as data:
            del data["DMMR/Label"]
    rig.device.loadConfiguration(file=file)
    assert [c.data["Label"] for c in rig.device.channels] == ["", ""]
    assert [c.name for c in rig.device.channels] == ["DMMR_M00", "DMMR_M01"]


def test_labels_stay_attached_to_module_addresses_when_discovery_reorders_them(rig):
    rig.device.channels[0].data["Label"] = "Collecteur"
    rig.device.channels[1].data["Label"] = "Entrée"
    current = rig.device._current_channel_items()
    template = rig.Channel({}).asDict()
    planned, _ = rig.module._plan_channel_sync(current, [1, 0, 3], "DMMR", default_item=template)
    labels = {int(item["Module"]): item["Label"] for item in planned}
    assert labels == {0: "Collecteur", 1: "Entrée", 3: ""}
    assert {item["Name"] for item in planned} == {"DMMR_M00", "DMMR_M01", "DMMR_M03"}


def test_change_detection_reads_literal_labels_as_utf8(rig, monkeypatch):
    label = "Électrode µA — 50%"
    rig.device.channels[0].data["Label"] = label
    rig.device.exportConfiguration(useDefaultFile=True)
    read = configparser.ConfigParser.read
    def checked_read(self, filenames, encoding=None):
        assert encoding == "utf-8"  # Must not use Windows' locale encoding.
        return read(self, filenames, encoding=encoding)
    monkeypatch.setattr(configparser.ConfigParser, "read", checked_read)
    def compare(items, *, ignoreIndicators):
        assert ignoreIndicators
        return [], any(item["Label"] != channel.data["Label"] for item, channel in zip(items, rig.device.channels))
    rig.device.compareItemsConfig = compare
    assert not rig.device.channelConfigChanged()
    rig.device.channels[0].data["Label"] = "Changed label"
    assert rig.device.channelConfigChanged()
    assert not rig.device.channelConfigChanged(file=rig.tmp / "missing.ini", useDefaultFile=False)


def test_failed_ini_save_leaves_existing_config_intact(rig, monkeypatch):
    rig.device.exportConfiguration(useDefaultFile=True)
    file = rig.tmp / "DMMR.ini"
    before = file.read_bytes()
    rig.device.channels[0].data["Label"] = "New label"
    def denied(self, target):
        raise PermissionError("read-only destination")
    monkeypatch.setattr(Path, "replace", denied)
    with pytest.raises(PermissionError):
        rig.device.exportConfiguration(useDefaultFile=True)
    assert file.read_bytes() == before
    assert set(rig.tmp.iterdir()) == {file}


def test_hdf_export_preserves_existing_measurement_data_and_metadata(rig):
    h5py = pytest.importorskip("h5py")
    file = rig.tmp / "acquisition.h5"
    with h5py.File(file, "w") as data:
        data.create_dataset("DMMR/Time", data=np.arange(100.))
        data.create_dataset("DMMR/Output Channels/DMMR_M00", data=np.arange(100.) * 1e-12)
    rig.device.channels[0].data["Label"] = "Collecteur"
    rig.device.exportConfiguration(file=file)
    rig.device.channels[0].data["Label"] = "Changed since recording"
    rig.device.exportConfiguration(file=file)
    with h5py.File(file, "r") as data:
        np.testing.assert_array_equal(data["DMMR/Time"][:], np.arange(100.))
        np.testing.assert_array_equal(data["DMMR/Output Channels/DMMR_M00"][:], np.arange(100.) * 1e-12)
        assert data["DMMR/Label"].asstr()[0] == "Collecteur"


@pytest.mark.parametrize("extension", [".ini", ".h5"])
def test_ascii_configuration_matches_explorer_schema(rig, extension):
    """Compare against the actual host exporter, not a reimplementation of it."""
    h5py = pytest.importorskip("h5py")
    try:
        host = Path(distribution("esibd-explorer").locate_file("esibd/plugins.py"))
    except PackageNotFoundError:
        pytest.skip("Installed Explorer unavailable")
    tree = ast.parse(host.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ChannelManager")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "exportConfiguration")
    ns = dict(Path=Path, configparser=configparser, np=np, h5py=h5py, infoDict=rig.const.infoDict,
              INFO="Info", FILE_INI=".ini", PRINT=rig.module.PRINT,
              PARAMETERTYPE=SimpleNamespace(**{k: getattr(rig.module.PARAMETERTYPE, k) for k in ("INT", "FLOAT", "BOOL")}, COLOR="COLOR"))
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(host), "exec"), ns)
    rig.device.channels[0].data["Label"] = "Collecteur"
    rig.device.channels[1].data["Label"] = "Entrance"
    reference, result = (rig.tmp / (name + extension) for name in ("host", "plugin"))
    ns["exportConfiguration"](rig.device, file=reference)
    rig.device.exportConfiguration(file=result)
    if extension == ".ini":
        assert result.read_bytes() == reference.read_bytes()
    else:
        with h5py.File(reference) as expected, h5py.File(result) as actual:
            assert set(expected["DMMR"]) == set(actual["DMMR"])
            for key in expected["DMMR"]:
                np.testing.assert_array_equal(expected[f"DMMR/{key}"][:], actual[f"DMMR/{key}"][:])
                assert expected[f"DMMR/{key}"].dtype == actual[f"DMMR/{key}"].dtype
