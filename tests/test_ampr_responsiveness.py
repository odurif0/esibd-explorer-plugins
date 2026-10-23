"""AMPR connect, poll and write latency, and the state reports seen in the 2026-10-21 lab logs.

A 12-module poll read four setpoints per module under one lock for ~2.4 s, so
edits waited for whole cycles and OFF could not stop the poll in time.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from support.ampr import load_plugin


def _channel(module_id, number, value=0.0):
    return SimpleNamespace(real=True, enabled=True, value=value, name=f"M{module_id}CH{number}",
                           VALUE="Value", getParameterByName=lambda name: SimpleNamespace(displayDecimals=2),
                           module_address=lambda: module_id, channel_number=lambda: number,
                           lastAppliedValue=np.nan)


class _Device:
    NO_ERR = 0

    def __init__(self, read_s=0.0):
        self.calls = []
        self.targets = {}
        self.read_s = read_s

    def get_all_module_voltage_measured(self, module):
        time.sleep(self.read_s)
        self.calls.append(("measured", module))
        return 0, [float(module)] * 4

    def get_module_voltages(self, module):
        time.sleep(self.read_s)
        self.calls.append(("full", module))
        return {number: {"setpoint": self.targets.get((module, number), 0.0), "measured": float(module)}
                for number in range(1, 5)}

    def set_module_voltage(self, module, number, voltage):
        self.calls.append(("write", module, number, voltage))
        self.targets[(module, number)] = voltage
        return 0

    def get_module_voltage_setpoint(self, module, number):
        self.calls.append(("setpoint", module, number))
        return 0, self.targets.get((module, number), 0.0)


@pytest.fixture(params=["ampr_a", "ampr_b"])
def rig(request):
    module = load_plugin(f"{request.param}/ampr_plugin.py", "ampr_responsiveness_test")
    module._invoke_gui_callback = lambda fn: fn()
    modules = list(range(12))
    channels = [_channel(module_id, 1) for module_id in modules]
    logs = []
    parent = SimpleNamespace(name=request.param, isOn=lambda: True, getChannels=lambda: channels,
                             getConfiguredModules=lambda: modules, module_voltage_limit=lambda module_id: 1000.0)
    controller = module.AMPRController(parent)
    controller.lock = threading.Lock()
    controller.errorCount = 0
    controller.initialized = True
    controller.main_state = "ST_ON"
    controller.detected_module_ids = modules
    controller.print = lambda message, flag=None: logs.append((message, flag))
    controller._sync_status_to_gui = lambda: None
    controller._update_state = lambda **kwargs: None
    controller.acquiring = True
    controller.device = _Device()
    yield SimpleNamespace(module=module, controller=controller, channels=channels, logs=logs)
    controller._cancel_setpoints()
    worker = controller._setpoint_thread
    if worker is not None:
        worker.join(timeout=3)


def test_poll_reads_setpoints_of_one_module_in_turn_and_where_a_write_awaits_readback(rig):
    controller, device = rig.controller, rig.controller.device
    for _ in range(3):
        controller.readNumbers()
    assert [module_id for kind, module_id in device.calls if kind == "full"] == [0, 1, 2]
    assert len([call for call in device.calls if call[0] == "measured"]) == 3 * 11
    assert controller.values[(5, 1)] == 5.0

    request = rig.module._AMPRSetpoint(rig.channels[7], device, controller._setpoint_cancel)
    request.state = "sent"
    controller._latest_setpoints[request.key] = request
    device.calls.clear()
    controller.readNumbers()
    assert sorted(module_id for kind, module_id in device.calls if kind == "full") == [3, 7]


def test_a_write_takes_the_lock_between_module_reads_not_after_the_cycle(rig):
    controller = rig.controller
    controller.device = device = _Device(read_s=0.03)
    first_read = threading.Event()
    original = device.get_all_module_voltage_measured

    def measured(module_id):
        first_read.set()
        return original(module_id)
    device.get_all_module_voltage_measured = measured
    controller.signalComm = SimpleNamespace(
        updateValuesSignal=SimpleNamespace(emit=lambda: setattr(controller, "acquiring", False)))
    poll = threading.Thread(target=controller.runAcquisition)
    poll.start()
    assert first_read.wait(2)
    rig.channels[11].value = 42.0
    controller._queue_setpoint(rig.channels[11])
    poll.join(timeout=5)
    assert not poll.is_alive()
    controller._setpoint_thread and controller._setpoint_thread.join(timeout=3)
    reads = [index for index, call in enumerate(device.calls) if call[0] in ("measured", "full")]
    write = device.calls.index(("write", 11, 1, 42.0))
    assert write < reads[3], device.calls  # Not after the remaining ~10 modules.


def test_write_is_confirmed_by_its_own_readback_without_waiting_for_a_poll(rig):
    controller, channel = rig.controller, rig.channels[4]
    channel.value = 25.0
    controller._queue_setpoint(channel)
    controller._setpoint_thread.join(timeout=3)
    assert ("setpoint", 4, 1) in controller.device.calls
    assert controller._latest_setpoints[(4, 1)].state == "confirmed"
    assert channel.lastAppliedValue == 25.0


def test_a_readback_lagging_the_write_stays_sent_until_the_module_is_read(rig):
    controller, channel, device = rig.controller, rig.channels[4], rig.controller.device
    device.get_module_voltage_setpoint = lambda module_id, number: (0, 0.0)  # old value
    channel.value = 25.0
    controller._queue_setpoint(channel)
    controller._setpoint_thread.join(timeout=3)
    assert controller._latest_setpoints[(4, 1)].state == "sent"
    assert not [message for message, flag in rig.logs if flag == rig.module.PRINT.ERROR]
    controller.readNumbers()
    assert controller._latest_setpoints[(4, 1)].state == "confirmed"
    assert channel.lastAppliedValue == 25.0


def test_stopping_acquisition_ends_the_poll_at_the_next_module(rig):
    controller = rig.controller
    controller.device = device = _Device(read_s=0.02)
    token = controller._native_session_token
    original = device.get_all_module_voltage_measured

    def measured(module_id):
        if module_id == 2:
            controller.acquiring = False  # stopAcquisition during the cycle
        return original(module_id)
    device.get_all_module_voltage_measured = measured
    controller.readNumbers(poll_token=token)
    assert [call[1] for call in device.calls] == [0, 1, 2]


def test_connect_scans_once_and_resolves_capabilities_from_that_scan(rig):
    controller, parent = rig.controller, rig.controller.controllerParent
    scans, resolved = [], []
    info = {0: {"product_id": "AMPR module 4 channel 500 V", "product_no": 1, "hw_type": 2}}

    class Device:
        NO_ERR = 0
        def get_scanned_module_state(self):
            return 0, False, False
        def scan_modules(self):
            scans.append(True)
            return info
        def get_module_capabilities_from_scan(self, modules):
            resolved.append(modules)
            return {0: {"status": 0, "product_id": modules[0]["product_id"], "voltage_rating": 500, "channel_count": 4}}
        def get_module_capabilities(self):
            raise AssertionError("a second full module scan")
    parent._current_channel_items = lambda: []
    parent._default_channel_item = lambda: {}
    controller.device = Device()
    controller._refresh_module_scan()
    assert scans == [True] and resolved == [info]
    assert parent.module_voltage_limits == {0: 500.0}
    assert parent.module_channel_counts == {0: 4}


def test_runtime_resolves_scan_metadata_and_queries_only_incomplete_modules(rig):
    driver = rig.module._get_ampr_driver_class()
    queried = []
    facade = SimpleNamespace(NO_ERR=0, get_module_capabilities=lambda address: queried.append(address) or {"status": -10})
    result = driver.get_module_capabilities_from_scan(facade, {
        1: {"product_id": "x", "product_no": 132401, "hw_type": 222308, "fw_version": 3},
        4: {"product_id": "Dual 500V", "product_no": 9, "hw_type": 9},
        6: {"product_no": 9},  # identity read failed during the scan
    })
    assert result[1]["voltage_rating"] == 1000 and result[1]["channel_count"] == 4
    assert result[4]["voltage_rating"] == 500 and result[4]["channel_count"] == 2
    assert result[1]["status"] == result[4]["status"] == 0
    assert queried == [6] and result[6] == {"status": -10}


@pytest.mark.parametrize("plugin", ["ampr_a", "ampr_b"])
def test_leaving_st_on_while_monitored_is_reported_once_with_its_reason(plugin):
    module = load_plugin(f"{plugin}/ampr_plugin.py", "ampr_responsiveness_test")
    states = iter(["ST_ON", "ST_ERR_ILOCK", "ST_ERR_ILOCK", "ST_ON"])

    class Device:
        NO_ERR = 0
        def get_state(self):
            return 0, "0x0", next(states)
        def get_device_state(self):
            return 0, "0x800", ["DS_ILOCK_FAIL"]
        def get_voltage_state(self):
            return 0, "0x0", "VOLTAGE_OK"
        def get_interlock_state(self):
            return 0, "0x200", ["SI_ILOCK_REAR"]
    controller = module.AMPRController(SimpleNamespace(name=plugin, isOn=lambda: True))
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))
    controller.lock = threading.Lock()
    controller.device, controller.initialized, controller.acquiring = Device(), True, True
    controller.errorCount = 0
    controller.main_state = "ST_ON"
    for _ in range(4):
        controller._update_state()
    errors = [message for message, flag in logs if flag == module.PRINT.ERROR]
    assert len(errors) == 1
    assert "ST_ERR_ILOCK" in errors[0] and "DS_ILOCK_FAIL" in errors[0] and "SI_ILOCK_REAR" in errors[0]
    assert logs[-1] == ("AMPR is back in ST_ON.", None)


@pytest.mark.parametrize("plugin", ["ampr_a", "ampr_b"])
def test_refused_startup_reports_the_device_state_read_before_cleanup(plugin, monkeypatch):
    module = load_plugin(f"{plugin}/ampr_plugin.py", "ampr_responsiveness_test")
    monkeypatch.setattr(module.DeviceController, "toggleOn", lambda self: None, raising=False)
    order = []

    class Device:
        NO_ERR = 0
        def initialize(self, timeout_s):
            raise RuntimeError("AMPR PSU enable was not confirmed")
        def get_state(self):
            order.append("query")
            return 0, "0x8005", "ST_ERR_ILOCK"
        def get_device_state(self):
            return 0, "0x800", ["DS_ILOCK_FAIL"]
        def get_voltage_state(self):
            return 0, "0x0", "VOLTAGE_OK"
        def get_interlock_state(self):
            return 0, "0x200", ["SI_ILOCK_REAR"]
    parent = SimpleNamespace(name=plugin, isOn=lambda: True, connect_timeout_s=5.0, getChannels=lambda: [])
    controller = module.AMPRController(parent)
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))
    controller.lock = threading.Lock()
    controller.device, controller.errorCount = Device(), 0

    def cleanup(targets):
        order.append("cleanup")
        controller.device = None
        return True
    controller._safe_disable_after_toggle_failure = cleanup
    controller.toggleOn()
    assert order[0] == "query" and order[-1] == "cleanup"
    failure = next(message for message, flag in logs if message.startswith("Failed to toggle AMPR PSU"))
    assert "main state: ST_ERR_ILOCK" in failure and "interlock state: SI_ILOCK_REAR" in failure


@pytest.mark.parametrize("plugin", ["ampr_a", "ampr_b"])
def test_released_worker_message_names_the_transport_failure(plugin):
    module = load_plugin(f"{plugin}/ampr_plugin.py", "ampr_responsiveness_test")
    cause = "ampr native worker transport is unusable: COM_AMPR_12_GetState blocked past its native I/O deadline (5s)"
    backend = SimpleNamespace(session="s", closed=True, close=lambda grace_s=None: True,
                              lifecycle_attribute=lambda name: cause if name == "_transport_error" else None)
    controller = module.AMPRController(SimpleNamespace(name=plugin, isOn=lambda: True, getChannels=lambda: []))
    logs = []
    controller.print = lambda message, flag=None: logs.append((message, flag))
    controller._sync_status_to_gui = controller._restore_off_ui_state = lambda: None
    controller.device = SimpleNamespace(_backend=backend)
    assert controller._retire_native_worker()
    assert any(cause in message and "Hardware OFF was not confirmed" in message for message, _ in logs)


@pytest.mark.parametrize("plugin", ["ampr_a", "ampr_b"])
def test_an_ampr_without_channels_exports_nothing_and_logs_no_error(plugin):
    from support import explorer

    exported = []

    class RecordingDevice(explorer.Device):
        def exportConfiguration(self, file=None, useDefaultFile=False):
            exported.append((file, useDefaultFile))

    module = explorer.load_plugin(f"{plugin}/ampr_plugin.py", "ampr_responsiveness_test",
                                  runtimes=("ampr",), Device=RecordingDevice)
    device = object.__new__(module.AMPRDevice)
    device.channels = []
    device.exportConfiguration(useDefaultFile=True)
    assert exported == []
    device.channels = [object()]
    device.exportConfiguration(useDefaultFile=True)
    assert exported == [(None, True)]
