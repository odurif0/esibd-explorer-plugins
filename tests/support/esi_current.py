"""ESI current-measurement rig shared by the ESI current tests."""
from types import SimpleNamespace

import numpy as np

from support.esi import load_plugin as _load_plugin


def channel_for(module, address, *, current=False, enabled=True):
    channel = module.ESIChannel.__new__(module.ESIChannel)
    channel.module = address
    channel.function = "HV current" if current else "HVPS-3kB (+/- pair)"
    channel.enabled = enabled
    channel.real = True
    channel.value = 100.0 if not current else np.nan
    channel.monitor = np.nan
    channel.name = f"ESI_HV{address}" + ("_I" if current else "")
    return channel


def rig():
    module = _load_plugin()
    channels = [channel_for(module, 1), channel_for(module, 2),
                channel_for(module, 0), channel_for(module, 1, current=True),
                channel_for(module, 2, current=True)]
    parent = SimpleNamespace(getChannels=lambda: channels, isOn=lambda: True,
                             poll_timeout_s=1., connect_timeout_s=2.)
    controller = module.ESIController(parent)
    controller.print = lambda *args, **kwargs: None
    controller._sync_status = lambda: None
    controller.initialized = True
    controller.errorCount = 0
    return module, channels, controller


def snapshot():
    return {
        "main_state": {"name": "ST_ON"}, "enabled": True,
        "modules": {
            1: {"target_v": 100., "measured_v": 98., "voltage_valid": True,
                "measured_a": 2.5e-9, "current_valid": True,
                "measurement": {"voltage_polarity": "positive", "voltage_fresh": True}},
            2: {"target_v": 200., "measured_v": 198., "voltage_valid": True,
                "measured_a": -4.5e-9, "current_valid": True,
                "measurement": {"voltage_polarity": "positive", "voltage_fresh": True}},
        },
        "heat": {"monitor_temperature_c": 25., "monitor_current_a": .1,
                 "hardware_limits": {"max_temperature_c": 175.}, "valid": True,
                 "heater_power_w": 1., "interlock_state": 0},
        "interlock_state": {"flags": []},
    }
