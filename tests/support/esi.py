"""ESI plugin loading and Explorer-like stand-ins shared by the ESI tests."""
from __future__ import annotations

import types

from support import explorer


class ESIChannel(explorer.Channel):
    """Base Channel behaviour the ESI channel relies on."""

    def __init__(self, channelParent=None, tree=None):
        super().__init__(channelParent, tree)
        self._parameters = {self.VALUE: types.SimpleNamespace(unit=""), self.MONITOR: types.SimpleNamespace(unit="")}

    def getDefaultChannel(self):
        return {self.VALUE: {}, self.ENABLED: {}, self.ACTIVE: {}, self.DISPLAY: {}, self.REAL: {}, self.MIN: {},
                self.MAX: {}}

    def setDisplayedParameters(self):
        self.displayedParameters = []

    def initGUI(self, item):
        self.module = item.get("Module", 2)

    def enabledChanged(self):
        self.base_enabled_changed = True

    def applyValue(self, apply=False):
        self.applied = apply


class ESIDeviceController(explorer.DeviceController):
    """Explorer's initComplete: start acquisition, then switch ON when the device is ON."""

    def initComplete(self):
        self.initialized = True
        self.startAcquisition()
        if self.controllerParent.isOn():
            self.toggleOnFromThread(parallel=True)


class ESIDeviceBase(explorer.Device):
    def getDefaultSettings(self):
        return {f"{self.name}/Interval": {explorer.Parameter.VALUE: 0},
                f"{self.name}/{self.MAXDATAPOINTS}": {explorer.Parameter.VALUE: 0}}


def load_plugin(path: str = "esi/esi_plugin.py", module_name: str = "esi_plugin_test"):
    """Import the ESI entry point against a fresh fake Explorer."""
    return explorer.load_plugin(path, module_name, runtimes=("esi",), Channel=ESIChannel,
                                DeviceController=ESIDeviceController, Device=ESIDeviceBase)


def install():
    """The fake Explorer with the ESI base behaviour, for probes that import the plugin themselves."""
    return explorer.install(Channel=ESIChannel, DeviceController=ESIDeviceController, Device=ESIDeviceBase)


class ExplorerLikeChannel:
    """Explorer 1.0.2: Channel.loading is a read-only view of the device's loading counter."""

    def __init__(self, device, module_number, enabled, value, heat=False):
        self.device, self.module, self.name = device, module_number, f"ESI_HV{module_number}"
        self._enabled, self._value, self.heat = enabled, value, heat
        self.events = []
        self.VALUE, self.ENABLED = "Value", "Enabled"

    @property
    def loading(self):
        return self.device.loading

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        self._value = value
        self.events.append(("value", value, self.device.loading))

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value
        self.events.append(("enabled", value, self.device.loading))

    def module_address(self):
        return self.module

    def is_heat_channel(self):
        return self.heat

    def getParameterByName(self, name):
        return None  # Explorer returns None for a parameter this stand-in does not model.


def counting_device(module, channels):
    """An ESIDevice whose loading flag is Explorer's counter."""
    class CountingESI(module.ESIDevice):
        _count = 0

        @property
        def loading(self):
            return self._count != 0

        @loading.setter
        def loading(self, value):  # Explorer's Plugin.loading counter
            self._count += 1 if value else -1

    device = object.__new__(CountingESI)
    device.getChannels = lambda: channels
    device.name = "ESI"
    device.printed = []
    device.print = lambda text, **kwargs: device.printed.append(text)
    return device
