"""HV current channels must be measurements, never additional ESI outputs."""

from types import SimpleNamespace
from threading import Event

import numpy as np
import pytest

from test_esi_plugin_behavior import _load_plugin


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
                "measured_a": 2.5e-9, "current_valid": True},
            2: {"target_v": 200., "measured_v": 198., "voltage_valid": True,
                "measured_a": -4.5e-9, "current_valid": True},
        },
        "heat": {"monitor_temperature_c": 25., "monitor_current_a": .1,
                 "hardware_limits": {"max_temperature_c": 175.}, "valid": True,
                 "heater_power_w": 1., "interlock_state": 0},
        "interlock_state": {"flags": []},
    }


def test_default_layout_contains_two_read_only_current_measurements():
    module = _load_plugin()
    items = module._fixed_channel_items("ESI")
    assert [item["Name"] for item in items] == ["ESI_HV1", "ESI_HV2", "ESI_HEAT", "ESI_HV1_I", "ESI_HV2_I"]
    for item in items[3:]:
        assert item["Function"] == "HV current"
        assert item["Enabled"] and item["Display"]
        assert np.isnan(item["Value"])


@pytest.mark.parametrize("order", [(0, 1, 2), (2, 0, 1)])
def test_upgrade_adds_currents_without_recreating_existing_channels(order):
    module, channels, _ = rig()
    outputs = [channels[index] for index in order]
    outputs[0].name = "Custom_HV1"
    outputs[0].values = np.array([91., 92.])
    device = module.ESIDevice.__new__(module.ESIDevice)
    device.getChannels = lambda: outputs
    added, saved = [], []
    device.addChannel = lambda item: added.append(item)
    device.exportConfiguration = lambda **kw: saved.append(kw)
    device.ensureFixedChannels(persist=True)
    assert [item["Name"] for item in added] == ["ESI_HV1_I", "ESI_HV2_I"]
    assert outputs[0].name == "Custom_HV1"
    assert outputs[0].value == 100. and outputs[0].enabled
    assert outputs[0].values.tolist() == [91., 92.]
    assert saved == [{"useDefaultFile": True}]


def test_missing_current_channel_only_is_added():
    module, channels, _ = rig()
    channels.pop()
    device = module.ESIDevice.__new__(module.ESIDevice)
    device.getChannels = lambda: channels
    added = []
    device.addChannel = added.append
    device.ensureFixedChannels()
    assert [item["Name"] for item in added] == ["ESI_HV2_I"]


def test_no_upgrade_or_writes_for_complete_layout():
    module, channels, _ = rig()
    channels[2].name = "ESI_HEAT"
    device = module.ESIDevice.__new__(module.ESIDevice)
    device.getChannels = lambda: channels
    device.addChannel = lambda item: pytest.fail("Current channels were duplicated")
    device.exportConfiguration = lambda **kw: pytest.fail("Unchanged configuration rewritten")
    device.ensureFixedChannels(persist=True)


def test_voltage_and_current_reach_distinct_monitor_channels_in_si_units():
    _, channels, controller = rig()
    controller._apply_snapshot(snapshot())
    controller.updateValues()
    assert [ch.monitor for ch in channels] == [98., 198., 25., 2.5e-9, -4.5e-9]
    assert [ch.value for ch in channels[:3]] == [100., 100., 100.]
    assert [ch.value for ch in channels[3:]] == [2.5e-9, -4.5e-9]
    assert [ch.unit for ch in channels] == ["V", "V", "degC", "A", "A"]


def test_invalid_current_does_not_reuse_previous_sample_or_invalidate_voltage():
    _, channels, controller = rig()
    data = snapshot()
    controller._apply_snapshot(data)
    controller.updateValues()
    data["modules"][1]["current_valid"] = False
    controller._apply_snapshot(data)
    controller.updateValues()
    assert np.isnan(channels[3].monitor) and np.isnan(channels[3].value)
    assert channels[0].monitor == 98.
    assert channels[4].monitor == -4.5e-9


def test_missing_module_invalidates_its_current():
    _, channels, controller = rig()
    data = snapshot()
    controller._apply_snapshot(data)
    controller.updateValues()
    del data["modules"][1]
    controller._apply_snapshot(data)
    controller.updateValues()
    assert np.isnan(channels[3].monitor) and np.isnan(channels[3].value)
    assert channels[4].monitor == -4.5e-9


def test_disabled_current_channel_cannot_record_last_good_value():
    _, channels, controller = rig()
    controller._apply_snapshot(snapshot())
    controller.updateValues()
    channels[3].enabled = False
    controller.updateValues()
    assert np.isnan(channels[3].monitor) and np.isnan(channels[3].value)
    assert channels[4].monitor == -4.5e-9


def test_current_is_measured_even_when_its_voltage_output_channel_is_off():
    _, channels, controller = rig()
    channels[0].enabled = False
    controller._apply_snapshot(snapshot())
    controller.updateValues()
    assert np.isnan(channels[0].monitor)
    assert channels[3].monitor == 2.5e-9


@pytest.mark.parametrize("enabled", [True, False])
def test_current_channel_can_never_issue_an_output_command(enabled):
    _, channels, controller = rig()

    class Device:
        def __getattr__(self, name):
            pytest.fail(f"Measurement issued hardware call: {name}")

    controller.device = Device()
    current = channels[3]
    current.enabled = enabled
    current.value = 1e9  # Even programmatic edits must not become voltage commands.
    controller.applyValue(current)
    controller._apply_value_unlocked(current, Event())


def test_current_channel_edits_do_not_trigger_explorer_global_update():
    module, channels, _ = rig()
    current = channels[3]
    current.valueChanged()
    current.applyValue(apply=True)
    current.enabledChanged()
    assert not getattr(current, "applied", False)
    assert module.ESIChannel.is_current_channel(current)


def test_poll_uses_existing_snapshot_once_and_failure_invalidates_currents():
    _, channels, controller = rig()
    calls = []
    controller.device = SimpleNamespace(collect_diagnostics=lambda **kw: calls.append(kw) or snapshot())
    controller.readNumbers()
    controller.updateValues()
    assert len(calls) == 1
    assert channels[3].monitor == 2.5e-9

    def failed(**kw):
        raise RuntimeError("communication lost")

    controller.device.collect_diagnostics = failed
    controller.readNumbers()
    controller.updateValues()
    assert all(np.isnan(ch.monitor) for ch in channels)
    assert all(np.isnan(ch.value) for ch in channels[3:])


def test_shutdown_clears_current_measurements():
    _, channels, controller = rig()
    controller._apply_snapshot(snapshot())
    controller.updateValues()
    controller.main_state = "Shutdown unconfirmed"
    controller.readNumbers()
    controller.updateValues()
    assert all(np.isnan(ch.monitor) for ch in channels)
    assert all(np.isnan(ch.value) for ch in channels[3:])
