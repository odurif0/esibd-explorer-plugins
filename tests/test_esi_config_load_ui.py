"""Export/reload ESI current channels through the real Explorer INI/Qt path."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('keys', ['ini', 'lower-dict', 'mixed-ini'])
def test_esi_configuration_roundtrip_in_qt(tmp_path, keys):
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONUNBUFFERED='1')
    result = subprocess.run([env.get('ESIBD_QT_PYTHON', sys.executable), str(Path(__file__)),
                             str(tmp_path), keys], env=env, text=True, capture_output=True, timeout=40)
    if result.returncode == 77:
        pytest.skip(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr


def run(target, keys):
    import configparser
    from types import ModuleType, SimpleNamespace as NS
    host = os.environ.get('ESIBD_EXPLORER_SOURCE')
    if host:
        sys.path.insert(0, host)
    sys.modules['pyautogui'] = ModuleType('pyautogui')
    try:
        from PyQt6.QtCore import QObject
        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QApplication, QHeaderView
        from esibd import core, plugins
        import numpy as np
    except ImportError as exc:
        print(f'Full Explorer/Qt dependencies unavailable: {exc}')
        return 77
    app = QApplication([])
    module = core.dynamicImport('esi_config_load_test', ROOT / 'esi/esi_plugin.py')
    assert module is not None
    class Manager(NS):
        def __getattr__(self, name):
            return getattr(plugins, name, None)
    logs = []
    manager = Manager(loading=True, plugins=[], reconnectSource=lambda *a: None,
        DeviceManager=NS(globalUpdate=lambda **kw: pytest.fail('Hardware/global update during configuration loading')),
        Settings=NS(loading=True), logger=NS(print=lambda **kw: logs.append(kw)))
    device = module.ESIDevice.__new__(module.ESIDevice)
    QObject.__init__(device)
    device._loading, device.channels = 0, []
    device.loading = True
    device.pluginManager = manager
    device.dependencyPath, device.sourceCodePath = ROOT / 'esi', ROOT / 'esi/esi_plugin.py'
    device.interval, device.maxDataPoints = 1000, 100000
    device.inout, device.unit = core.INOUT.IN, 'V'
    device.useDisplays = device.useMonitors = True
    device.useBackgrounds = device.logY = False
    device.convertDataDisplay = device.liveDisplay = None
    device.makeCoreIcon = lambda *a, **kw: QIcon()
    device.print = lambda message, **kw: logs.append(dict(message=message, **kw))
    device.time = core.DynamicNp(max_size=100000)
    device.tree = core.TreeWidget()
    device.tree.resize(1150, 300)
    device.controller = module.ESIController(device)
    class NoHardware:
        def __getattr__(self, name):
            pytest.fail(f'Configuration loading called the hardware: {name}')
    device.controller.device = NoHardware()
    manager.plugins = [device]

    def add(item):
        channel = module.ESIChannel(device, device.tree)
        device.tree.setColumnCount(len(channel.parameters))
        device.tree.addTopLevelItem(channel)
        device.channels.append(channel)
        channel.initGUI(item)
        return channel

    items = module._fixed_channel_items('ESI')
    custom_names = ['Source_voltage', 'Extractor_voltage', 'Capillary_temperature', 'Source_current', 'Extractor_current']
    for index, item in enumerate(items):
        item['Name'] = custom_names[index]
        item['Enabled'] = index != 1
        if index < 3:
            item['Value'] = [123., 234., 67.][index]
        add(item)
    before = [ch.asDict(includeTempParameters=True, formatValue=True) for ch in device.channels]
    ini = target / 'ESI.ini'
    # Native exporter lowercases the INI keys even when the in-memory defaults
    # use Name/Module/Function. Current channels must survive the next startup.
    device.exportConfiguration(file=ini)
    saved_bytes = ini.read_bytes()
    parser = configparser.ConfigParser()
    parser.read(ini, encoding='utf-8')
    assert 'name' in dict(parser['Channel_003']) and 'Name' not in dict(parser['Channel_003'])
    device.channels = []
    device.tree.clear()
    for section in parser.sections():
        if section == core.INFO:
            continue
        item = parser[section]
        if keys == 'lower-dict':
            item = dict(item)
        elif keys == 'mixed-ini':
            mixed = configparser.ConfigParser()
            mixed.read_dict({'Channel': {key.upper(): value for key, value in item.items()}})
            item = mixed['Channel']
        add(item)
    after = [ch.asDict(includeTempParameters=True, formatValue=True) for ch in device.channels]
    assert after == before, (before, after)
    assert ini.read_bytes() == saved_bytes
    assert [ch.name for ch in device.channels] == custom_names
    assert [ch.module for ch in device.channels] == [1, 2, 0, 1, 2]
    assert [ch.value for ch in device.channels[:3]] == [123., 234., 67.]
    assert [ch.enabled for ch in device.channels] == [True, False, True, True, True]
    for channel in device.channels[3:]:
        assert channel.is_current_channel() and channel.unit == 'A' and np.isnan(channel.value)
        for name in (channel.VALUE, channel.MONITOR):
            param = channel.getParameterByName(name)
            assert param.indicator and param.spin.isReadOnly()
    for channel in device.channels[:3]:
        assert not channel.is_current_channel()
        assert not channel.getParameterByName(channel.VALUE).indicator
    device.tree.setHeaderLabels([p.name for p in device.channels[0].parameters])
    for index, parameter in enumerate(device.channels[0].parameters):
        device.tree.setColumnHidden(index, parameter.name not in ('Name', 'Value', 'Monitor', 'Module', 'Function', 'Enabled'))
    device.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    device.tree.show()
    app.processEvents()
    device.tree.grab().save(str(target / f'esi-reloaded-{keys}.png'))
    assert not any(entry.get('flag') == core.PRINT.ERROR for entry in logs), logs
    print('ESI_CONFIG_ROUNDTRIP_OK', keys)
    return 0


if __name__ == '__main__':
    raise SystemExit(run(Path(sys.argv[1]), sys.argv[2]))
