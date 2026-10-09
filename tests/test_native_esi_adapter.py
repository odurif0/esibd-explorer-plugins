"""ESI GUI ownership and safety state, including actual native-process faults."""
from __future__ import annotations

import copy
import importlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_esi_plugin_behavior import _load_plugin
from test_native_worker import binary  # Local offline executable test-backend fixture.


def snapshot(*, active=True):
    return {
        "main_state": {"name": "STATE_ON", "hex": "0x0"},
        "enabled": True,
        "device_state": {"hex": "0x00000000", "flags": []},
        "interlock_state": {"hex": "0x00000000", "flags": []},
        "modules": {
            address: {
                "target_v": 100. if active else 0.,
                "module_active": active, "control_active": active,
                "measured_v": 99. if active else 0., "voltage_valid": True,
                "measured_a": address * 1e-9, "current_valid": True,
                "led": {"red": False, "green": active, "blue": False},
                "pwm": {"voltage_set_v": 100. if active else 0.,
                        "voltage_measured_v": 99. if active else 0.},
                "measurement": {"voltage_polarity": "positive", "voltage_fresh": True,
                                "current_polarity": "positive", "current_fresh": True},
            } for address in (1, 2)
        },
        "heat": {
            "valid": True, "monitor_temperature_c": 25., "monitor_current_a": .1,
            "target_temperature_c": 25., "heater_power_w": 1., "power_limit_w": 2.,
            "voltage_limit_v": 10., "current_limit_a": 1., "interlock_state": 0,
            "active": active, "module_active": active, "module_gate_active": active,
            "device_gate_active": active, "control_active": active,
            "hardware_limits": {"max_temperature_c": 175., "max_power_w": 10.,
                                "max_voltage_v": 24., "max_current_a": 2.},
        },
    }


class Driver:
    _process_backend_disabled_reason = ""
    _transport_poisoned = False
    _open_failed = _opening_in_progress = _failed_open_released = False

    def __init__(self):
        self.calls = []
        self.hook = lambda name: None
        self.off_result = False
        self.closed = False
        self.data = snapshot(active=False)

    def reply(self, name, value, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        self.hook(name)
        return copy.deepcopy(value)

    def connect(self, **kwargs):
        return self.reply("connect", True, **kwargs)

    def set_global_active(self, active, **kwargs):
        return self.reply("global", active, active, **kwargs)

    def configure_heat_limits(self, **kwargs):
        result = {key: value for key, value in kwargs.items() if key in ("voltage_v", "current_a", "power_w")}
        if "power_w" in result:
            self.data["heat"]["power_limit_w"] = result["power_w"]
        return self.reply("heat_limits", result, **kwargs)

    def get_heat_configuration(self, **kwargs):
        return self.reply("heat_config", self.data["heat"], **kwargs)

    def collect_identity(self, **kwargs):
        return self.reply("identity", {"modules": {}}, **kwargs)

    def force_safe_off(self, **kwargs):
        return self.reply("safe_off", False, **kwargs)

    def configure_hv_max_voltage_steps(self, value, **kwargs):
        return self.reply("steps", {1: value, 2: value}, value, **kwargs)

    def select_hv_voltage_adc(self, address, **kwargs):
        return self.reply(f"adc{address}", kwargs["negative"], address, **kwargs)

    def collect_diagnostics(self, **kwargs):
        return self.reply("diagnostics", self.data, **kwargs)

    def list_configs(self, **kwargs):
        return self.reply("configs", [{"index": 1, "name": "Spray"}], **kwargs)

    def set_hv_module_target(self, address, target, **kwargs):
        return self.reply("target", target, address, target, **kwargs)

    def set_heater_temperature(self, target, **kwargs):
        return self.reply("temperature", target, target, **kwargs)

    def set_output_active(self, address, active, **kwargs):
        return self.reply("output", True if active else self.off_result, address, active, **kwargs)

    def disconnect(self, **kwargs):
        return self.reply("disconnect", True, **kwargs)

    def force_close_transport(self, **kwargs):
        self.closed = True
        return self.reply("force_close", True, **kwargs)

    def close(self):
        self.closed = True
        self.calls.append(("close", (), {}))


@pytest.fixture
def control(tmp_path):
    module = _load_plugin()
    channels = []
    for address, current in ((1, False), (2, False), (0, False), (1, True), (2, True)):
        channel = module.ESIChannel.__new__(module.ESIChannel)
        channel.module = address
        channel.function = "HV current" if current else "HEAT-CTRL-2410" if address == 0 else "HVPS-3kB (+/- pair)"
        channel.enabled = channel.real = True
        channel.value = np.nan if current else 25. if address == 0 else 100.
        channel.monitor = np.nan
        channel.name = f"ESI_{address}" + ("_I" if current else "")
        channels.append(channel)
    action = SimpleNamespace(state=True)
    completions, messages = [], []
    parent = SimpleNamespace(
        com=16, baudrate=230400, connect_timeout_s=.2, poll_timeout_s=.2, interval=1000.,
        heat_voltage_limit_v=10., heat_current_limit_a=1., heat_power_limit_w=0.,
        ramp_rate_v_s=0., recording=True, onAction=action, isOn=lambda: action.state,
        getChannels=lambda: channels,
        ensureFixedChannels=lambda **kwargs: completions.append(kwargs),
        pluginManager=SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path)),
    )
    c = module.ESIController(parent)
    c.errorCount = 0
    c.print = lambda message, **kwargs: messages.append(message)
    c.initializeValues(reset=True)
    c.initialized = c.acquiring = True
    c.main_state = "STATE_ON"
    c.device = Driver()
    c.toggleOnFromThread = lambda **kwargs: completions.append("toggle")
    emitted = []
    c.signalComm = SimpleNamespace(
        initializationReady=SimpleNamespace(emit=emitted.append),
        initCompleteSignal=SimpleNamespace(emit=lambda: pytest.fail('ESI needs an immutable completion payload')),
    )
    return SimpleNamespace(module=module, c=c, parent=parent, channels=channels,
                           messages=messages, completions=completions, emitted=emitted)


def prepare_initialization(control, monkeypatch):
    c, driver = control.c, control.c.device
    c.device = None
    c.initialized = c.acquiring = False
    c.initializing = True
    c.main_state = "Disconnected"
    options = []
    def construct(**kwargs):
        options.append(kwargs)
        return driver
    monkeypatch.setattr(control.module, "_get_esi_driver_class", lambda: construct)
    return driver, options


def test_native_initialization_keeps_verified_safe_sequence(control, monkeypatch):
    driver, options = prepare_initialization(control, monkeypatch)
    control.c.runInitialization()
    assert options[0]["native_backend"] is True
    assert "process_backend" not in options[0] and "worker_launcher" not in options[0]
    assert options[0]["log_dir"] == control.parent.pluginManager.Settings.dataPath / "logs/esi"
    assert [call[0] for call in driver.calls] == [
        "connect", "global", "heat_limits", "identity", "safe_off", "steps",
        "adc1", "adc2", "diagnostics", "configs",
    ]
    assert control.c.targets == {1: 0., 2: 0.}
    assert control.c.module_active == {1: False, 2: False}
    assert control.emitted == [(control.c._connection_generation, driver)] and not control.c.initializing
    control.c.initComplete(control.emitted[0])
    count = len(control.completions)
    control.c.initComplete(control.emitted[0])
    assert len(control.completions) == count, "duplicate completion must not restart acquisition"


@pytest.mark.parametrize("boundary", ["connect", "global", "heat_limits", "identity", "safe_off",
                                     "steps", "adc1", "adc2", "diagnostics", "configs"])
@pytest.mark.parametrize("raises", [False, True])
def test_retired_initialization_never_commands_or_closes_replacement(control, monkeypatch, boundary, raises):
    driver, _ = prepare_initialization(control, monkeypatch)
    replacement = Driver()
    def retire(name):
        if name == boundary:
            control.c._connection_generation += 1
            control.c._initialization_generation = control.c._connection_generation
            control.c.device = replacement
            control.c.initializing = True
            control.c.main_state = "Connecting"
            if raises:
                raise RuntimeError("old native reply failed")
    driver.hook = retire
    control.c.runInitialization()
    assert driver.calls[-1][0] == boundary
    assert not replacement.calls
    assert control.c.device is replacement and control.c.initializing
    assert control.c.main_state == "Connecting" and not control.emitted
    assert not control.c.available_configs


def test_queued_completion_cannot_initialize_replacement_still_opening(control):
    c = control.c
    c._initialization_generation = c._connection_generation
    c._initialization_ready = (c._connection_generation, c.device)
    ticket = c._initialization_ready
    c._connection_generation += 1
    c._initialization_generation = c._connection_generation
    c.device = Driver()
    c.initialized = c.acquiring = False
    c.main_state = "Connecting"
    c.initComplete(ticket)
    assert not c.initialized and not c.acquiring
    assert not control.completions and not c.device.calls


@pytest.mark.parametrize('same_backend', [False, True])
def test_old_queued_completion_cannot_consume_newer_ready_payload(control, same_backend):
    c = control.c
    c._initialization_generation = c._connection_generation
    c._emit_initialization_complete(c._connection_generation, c.device)
    old_ticket = control.emitted[-1]
    c._connection_generation += 1
    c._initialization_generation = c._connection_generation
    if not same_backend:
        c.device = Driver()
    c.initialized = c.acquiring = False
    c._emit_initialization_complete(c._connection_generation, c.device)
    new_ticket = control.emitted[-1]
    c.initComplete(old_ticket)
    c.initComplete()  # Explorer's payload-free signal is not proof of this initialization.
    assert c._initialization_ready == new_ticket
    assert not c.initialized and not c.acquiring and not control.completions
    c.initComplete(new_ticket)
    assert c.initialized and c.acquiring
    count = len(control.completions)
    c.initComplete(old_ticket)
    c.initComplete(new_ticket)
    assert len(control.completions) == count


def test_explorer_qt_queued_initialization_carries_originating_payload():
    script = '''
import importlib.util
import sys
import threading
from types import SimpleNamespace
from PyQt6.QtWidgets import QApplication

spec = importlib.util.spec_from_file_location('esi_init_payload_probe', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
app = QApplication([])
completed, started = [], []
parent = SimpleNamespace(name='ESI', maxErrorCount=10, print=lambda *a, **k: None,
                         getChannels=lambda: [], isOn=lambda: False,
                         ensureFixedChannels=lambda **k: completed.append(k))
parent.pluginManager = SimpleNamespace(Device=SimpleNamespace, Settings=SimpleNamespace(errorResetTime=1))
c = module.ESIController(parent)
c.startAcquisition = lambda: started.append(c.device)
c._initialization_generation = c._connection_generation
c.device = object()
old = (c._connection_generation, c.device)
worker = threading.Thread(target=lambda: c._emit_initialization_complete(*old))
worker.start()
worker.join(3.)
assert not worker.is_alive()
c._connection_generation += 1
c._initialization_generation = c._connection_generation
c.device = object()
new = (c._connection_generation, c.device)
c._initialization_ready = new
c.signalComm.initCompleteSignal.emit()
app.processEvents()
assert not completed and not started and not c.initialized
assert c._initialization_ready == new
worker = threading.Thread(target=lambda: c._emit_initialization_complete(*new))
worker.start()
worker.join(3.)
assert not worker.is_alive()
app.processEvents()
assert completed == [{'persist': True}] and started == [new[1]] and c.initialized
c.signalComm.initializationReady.emit(old)
c.signalComm.initializationReady.emit(new)
assert len(completed) == 1 and len(started) == 1
'''
    result = subprocess.run(
        [os.environ.get('ESIBD_QT_PYTHON', sys.executable), '-c', script,
         str(Path(__file__).resolve().parents[1] / 'esi/esi_plugin.py')],
        env={**os.environ, 'QT_QPA_PLATFORM': 'offscreen'},
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Traceback' not in result.stderr, result.stderr


@pytest.mark.parametrize("boundary", ["identity", "diagnostics", "configs"])
def test_fatal_crash_resume_read_releases_transport_without_output_commands(control, monkeypatch, boundary):
    driver, _ = prepare_initialization(control, monkeypatch)
    control.c.resume_session = True
    def fail(name):
        if name == boundary:
            driver._transport_poisoned = True
            raise RuntimeError("native transport is unusable")
    driver.hook = fail
    control.c.runInitialization()
    assert control.c.device is None and driver.closed and not control.emitted
    assert control.c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert control.c.shutdown_unconfirmed and not control.parent.onAction.state
    assert all(call[0] in ("connect", "identity", "diagnostics", "configs", "force_close", "close")
               for call in driver.calls)
    assert driver.calls[0][2]["preserve_outputs"] is True


@pytest.mark.parametrize("kind,boundary", [("temperature", "temperature"), ("power", "heat_config"),
                                          ("power", "heat_limits")])
@pytest.mark.parametrize("raises", [False, True])
def test_retired_heater_edit_never_rolls_back_or_publishes_to_new_connection(control, kind, boundary, raises):
    c, old = control.c, control.c.device
    c._apply_snapshot(snapshot())
    replacement = Driver()
    stability = []
    def retire(name):
        if name == boundary:
            c._connection_generation += 1
            c.device = replacement
            c.heat_power_error = c.heat_temperature_error = "New connection"
            c.heat_power_limit_w = 7.
            stability.append(c._heat_stability_generation)
            if raises:
                raise RuntimeError("late old reply failed")
    old.hook = retire
    getattr(c, "_apply_heat_" + kind)(3. if kind == "power" else 30., c._output_cancel)
    assert old.calls[-1][0] == boundary and not replacement.calls
    assert c.device is replacement and c.errorCount == 0
    assert c.heat_power_limit_w == 7.
    assert c.heat_power_error == c.heat_temperature_error == "New connection"
    assert c._heat_stability_generation == stability[0]


@pytest.mark.parametrize("kind", ["hv", "temperature", "power", "global"])
def test_synchronous_error_handler_reconnection_cannot_receive_old_rollback(control, monkeypatch, kind):
    c, old = control.c, control.c.device
    c._apply_snapshot(snapshot())
    replacement = Driver()
    def fail(name):
        raise RuntimeError("old operation failed")
    old.hook = fail
    counts = [0]
    def error_count(self, value):
        counts[0] = value
        if value:
            self._connection_generation += 1
            self.device = replacement
            self.main_state = "Connecting"
    monkeypatch.setattr(control.module.ESIController, "errorCount",
                        property(lambda self: counts[0], error_count), raising=False)
    if kind == "hv":
        c.applyValue(control.channels[0])
    elif kind == "global":
        c.toggleOn()
    else:
        getattr(c, "_apply_heat_" + kind)(3. if kind == "power" else 30., c._output_cancel)
    assert counts[0] == 1
    assert c.device is replacement and c.main_state == "Connecting"
    assert not replacement.calls, "a failed old request must not force the replacement OFF"


def test_late_config_load_does_not_switch_adc_on_replacement(control):
    c, old = control.c, control.c.device
    control.parent.name = "ESI"
    control.parent.operating_config = 1
    replacement = Driver()
    def load(index, **kwargs):
        c._connection_generation += 1
        c.device = replacement
        return 0
    old.load_config = load
    c.loadOperatingConfigNow()
    assert c.loaded_config_text == "n/a" and c.device is replacement
    assert not replacement.calls


def test_old_connection_generation_poll_cannot_restore_output_state(control):
    c = control.c
    def read(**kwargs):
        c._connection_generation += 1
        c.initializeValues(reset=True)
        c.main_state = "Connecting"
        return snapshot()
    c.device.collect_diagnostics = read
    c.readNumbers()
    assert c.main_state == "Connecting"
    assert c.module_active == {1: None, 2: None}
    assert np.isnan(c.values[1]) and np.isnan(control.channels[3].value)


def test_poll_started_before_output_off_cannot_restore_old_on(control):
    c, channel = control.c, control.channels[0]
    channel.enabled = False
    def read(**kwargs):
        c.applyValue(channel)
        return snapshot()
    c.device.collect_diagnostics = read
    c.readNumbers()
    assert c.module_active[1] is False and c.targets[1] == 0.
    assert np.isnan(c.values[1]) and np.isnan(control.channels[3].value)


def test_recoverable_poll_failure_clears_plots_without_replaying_outputs(control):
    c, driver = control.c, control.c.device
    c._apply_snapshot(snapshot())
    c.updateValues()
    assert np.isfinite(control.channels[3].value)
    def fail(name):
        raise RuntimeError("temporary communication error")
    driver.hook = fail
    c.readNumbers()
    assert c.device is driver and c.initialized
    assert c.main_state == control.module._ESI_COMMUNICATION_LOST and c._output_cancel.is_set()
    assert all(np.isnan(channel.monitor) for channel in control.channels)
    assert all(np.isnan(channel.value) for channel in control.channels[3:])
    driver.hook = lambda name: None
    driver.data = snapshot()
    c.readNumbers()
    c.updateValues()
    assert c.main_state == "STATE_ON" and not c._output_cancel.is_set()
    assert c.errorCount == 0 and control.channels[3].value == 1e-9
    assert [call[0] for call in driver.calls] == ["diagnostics", "diagnostics"]


def test_cancelled_on_generation_still_allows_explicit_local_off(control):
    c, driver, channel = control.c, control.c.device, control.channels[0]
    c._output_cancel.set()
    c.main_state = control.module._ESI_COMMUNICATION_LOST
    c.applyValue(channel)
    assert not driver.calls, "the cancelled ON generation must not be revived"
    channel.enabled = False
    c.applyValue(channel)
    assert [(name, args) for name, args, _ in driver.calls] == [("output", (1, False))]
    assert c.module_active[1] is False and c.targets[1] == 0.
    assert c._output_cancel.is_set(), "OFF must not re-arm a queued ON"


def test_native_idle_failure_is_contained_and_off_stays_unconfirmed(control):
    c, driver = control.c, control.c.device
    def fail(**kwargs):
        raise RuntimeError("native worker transport is unusable")
    driver.wait_for_idle = fail
    assert c.shutdownCommunication() is False
    assert c.device is None and driver.closed
    assert c.shutdown_unconfirmed and c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert not control.parent.onAction.state and not control.parent.recording
    assert not any(call[0] == "disconnect" for call in driver.calls)


def test_retired_discharge_progress_and_success_cannot_certify_new_connection(control):
    c, old = control.c, control.c.device
    replacement = Driver()
    report = {"modules": {1: {"positive_v": 0., "negative_v": 0., "measured_a": 0.}}}
    def disconnect(*, on_discharge, **kwargs):
        assert c._release_failed_transport(old)
        c._connection_generation += 1
        c.device = replacement
        c.initialized = True
        c.main_state = control.module._ESI_STOPPING
        c.initializeValues(reset=True)
        on_discharge(report)
        return True
    old.disconnect = disconnect
    assert c.shutdownCommunication() is False
    assert c.device is replacement and c.initialized
    assert c.main_state == control.module._ESI_STOPPING and not c.discharge_readings
    assert c.module_active == {1: None, 2: None} and not replacement.calls


def test_queued_plot_stop_cannot_disable_restarted_acquisition(control, monkeypatch):
    queued = []
    monkeypatch.setattr(control.module, "_invoke_gui_callback", queued.append)
    control.c._stop_local_acquisition()
    control.c._connection_generation += 1
    control.c.startAcquisition()
    control.parent.recording = True
    for callback in queued:
        callback()
    assert control.c.acquiring and control.parent.recording


class Widget:
    def __init__(self):
        self.enabled = True
        self.checked = False
        self.style = self.text = self.tooltip = ""
        self.value = None
        self.blocked = False

    def blockSignals(self, value):
        previous, self.blocked = self.blocked, value
        return previous

    def setStyleSheet(self, value):
        self.style = value

    def setEnabled(self, value):
        self.enabled = value

    def setChecked(self, value):
        self.checked = value

    def setText(self, value):
        self.text = value

    def setToolTip(self, value):
        self.tooltip = value

    def setValue(self, value):
        self.value = value

    def hasFocus(self):
        return False

    def setMinimumWidth(self, value):
        self.width = value

    def fontMetrics(self):
        return SimpleNamespace(horizontalAdvance=lambda text: len(text) * 8)


def panel(control):
    device = control.module.ESIDevice.__new__(control.module.ESIDevice)
    device.controller = control.c
    device.loading = False
    device.getChannels = control.parent.getChannels
    device._update_heat_controls = lambda: None
    device._update_heat_stability_display = lambda: None
    keys = ("card", "btn_on", "btn_off", "target", "hardware_target", "module_gate", "gate",
            "control", "pwm_set", "pwm_measured", "led", "measured", "current")
    device.esiHVCards = {1: {key: Widget() for key in keys}}
    device.esiHVCards[1]["adc_buttons"] = [Widget(), Widget()]
    device.esiHeatWidgets = {key: Widget() for key in (
        "heat_target", "heat_measured", "heat_power_limit", "heat_power", "heat_sensor", "heat_interlock")}
    device.esiHeatButton = Widget()
    device.esiHeatCard = Widget()
    return device


@pytest.mark.parametrize("off_result", [False, True, None, 0])
def test_hv_off_button_uses_confirmation_not_saved_selection_and_remains_retryable(control, off_result):
    c, channel = control.c, control.channels[0]
    c._apply_snapshot(snapshot())
    channel.enabled = False
    c.device.off_result = off_result
    c.applyValue(channel)
    device = panel(control)
    device._update_operator_panel()
    card = device.esiHVCards[1]
    if off_result is False:
        assert c.module_active[1] is False and c.targets[1] == 0.
        assert card["card"].style == control.module._ESI_PANEL_CARD_OFF
    else:
        assert c.module_active[1] is None and np.isnan(c.targets[1])
        assert card["card"].style == control.module._ESI_PANEL_CARD_ERR
        assert card["btn_off"].style != control.module._ESI_BTN_OFF_ACTIVE
        assert any("did not confirm" in message for message in control.messages)
    assert not channel.enabled and card["btn_off"].enabled
    attempts = []
    channel.applyValue = lambda **kwargs: attempts.append(kwargs)
    device._panel_output_selected(1, 0)
    assert attempts == [{"apply": True}]


def test_heat_off_without_readback_stays_red_and_off_reachable(control):
    control.channels[2].enabled = False
    control.c.heat_activation = {}
    control.c.heat_readback_valid = False
    device = panel(control)
    device._update_operator_panel()
    assert device.esiHeatCard.style == control.module._ESI_PANEL_CARD_ERR
    assert device.esiHeatButton.text == "Unconfirmed"
    assert device.esiHeatButton.checked and device.esiHeatButton.enabled
    attempts = []
    control.channels[2].applyValue = lambda **kwargs: attempts.append(kwargs)
    device._panel_heat_toggled(False)
    assert attempts == [{"apply": True}]


@pytest.fixture
def native_facade(control, binary, monkeypatch, tmp_path):
    driver_class = control.module._get_esi_driver_class()
    transport = importlib.import_module(driver_class.__module__.rsplit(".", 2)[0] + "._native_worker")
    proxy_class = transport.NativeWorkerProxy
    created = []
    def factory(root, family, config, **kwargs):
        assert family == "esi"
        assert config["device_id"] == "native_esi_gui_test" and config["com"] == 16
        proxy = proxy_class(root, "test", {"connected": True, "_dll_port_claimed": True,
                                          "_transport_poisoned": False, "_transport_error": None},
                            command=[str(binary), "--family", "test"],
                            log_dir=tmp_path, stop_grace_s=.05)
        created.append(proxy)
        return proxy
    monkeypatch.setattr(transport, "NativeWorkerProxy", factory)
    driver = driver_class(device_id="native_esi_gui_test", com=16, native_backend=True, log_dir=tmp_path)
    proxy = created[0]
    original = proxy.call_method
    routes = {}
    calls = []
    def route(method, *args, **kwargs):
        calls.append(method)
        target, values = routes[method]
        return original(target, *values, **kwargs)
    monkeypatch.setattr(proxy, "call_method", route)
    control.c.device = driver
    yield SimpleNamespace(driver=driver, proxy=proxy, routes=routes, calls=calls,
                          transport=transport, proxy_class=proxy_class, binary=binary)
    for proxy in created:
        proxy.close(grace_s=0)


def wait_for_call(proxy):
    until = time.monotonic() + 2.
    while proxy._active is None and time.monotonic() < until:
        time.sleep(.005)
    assert proxy._active is not None


def assert_released_unknown(control, proxy):
    assert proxy.closed and proxy._process.poll() is not None
    assert proxy._pending == {}
    assert control.c.device is None and not control.c.initialized and not control.c.acquiring
    assert control.c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert control.c.shutdown_unconfirmed and not control.parent.onAction.state
    assert not control.parent.recording
    assert all(np.isnan(channel.monitor) for channel in control.channels)
    assert all(np.isnan(channel.value) for channel in control.channels[3:])
    assert any("hardware interlock" in message for message in control.messages)


def test_native_facade_complete_snapshot_reaches_gui_and_plots(control, native_facade):
    native_facade.routes["collect_diagnostics"] = ("echo", [snapshot()])
    control.c.readNumbers()
    control.c.updateValues()
    assert control.c.main_state == "STATE_ON" and control.c.module_active == {1: True, 2: True}
    assert [channel.monitor for channel in control.channels] == [99., 99., 25., 1e-9, 2e-9]
    device = panel(control)
    device._update_operator_panel()
    assert device.esiHVCards[1]["card"].style == control.module._ESI_PANEL_CARD_ON
    assert device.esiHeatButton.text == "ON"


def test_native_crash_releases_gui_without_affecting_other_worker(control, native_facade):
    native_facade.routes["collect_diagnostics"] = ("crash", [])
    other = native_facade.proxy_class(
        Path(__file__).resolve().parents[1], "test", {},
        command=[str(native_facade.binary), "--family", "test"])
    try:
        control.c._apply_snapshot(snapshot())
        control.c.updateValues()
        started = time.monotonic()
        control.c.readNumbers()
        assert time.monotonic() - started < 3.
        assert_released_unknown(control, native_facade.proxy)
        assert native_facade.proxy._process.returncode == 73
        assert other.call_method("echo", "alive") == "alive"
    finally:
        other.close(grace_s=0)


@pytest.mark.parametrize('response', ['crash', 'unconfirmed'])
def test_toolbar_off_never_restores_on_after_native_worker_was_reaped(control, native_facade, response):
    c = control.c
    device = control.module.ESIDevice.__new__(control.module.ESIDevice)
    for key, value in vars(control.parent).items():
        setattr(device, key, value)
    device.controller = c
    device.loading = False
    device.deviceOnAction = SimpleNamespace(state=True)
    device._update_status_widgets = lambda: None
    control.parent = c.controllerParent = device
    c.toggleOnFromThread = lambda **kwargs: c.toggleOn()
    native_facade.routes['disconnect'] = ('crash', []) if response == 'crash' else ('echo', [False])
    device.setOn(False)
    if response == 'crash':
        assert_released_unknown(control, native_facade.proxy)
        assert not device.deviceOnAction.state
        c._restore_on_ui_state()
        assert not device.onAction.state and not device.deviceOnAction.state
    else:
        assert c.device is native_facade.driver and not native_facade.proxy.closed
        assert c.shutdown_unconfirmed and c.main_state == 'Shutdown unconfirmed'
        assert device.onAction.state and device.deviceOnAction.state


@pytest.mark.parametrize("operation", ["poll", "output"])
def test_native_blocked_call_off_is_bounded_reaps_worker_and_rejects_late_reply(control, native_facade, operation):
    c = control.c
    if operation == "poll":
        native_facade.routes["collect_diagnostics"] = ("hang", [])
        target = c.readNumbers
    else:
        native_facade.routes["set_hv_module_target"] = ("hang", [])
        target = lambda: c.applyValue(control.channels[0])
    errors = []
    def run():
        try:
            target()
        except Exception as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    try:
        thread.start()
        wait_for_call(native_facade.proxy)
        started = time.monotonic()
        assert c.shutdownCommunication() is False
        assert time.monotonic() - started < 3.
    finally:
        native_facade.proxy.close(grace_s=0)
        thread.join(3.)
    assert not thread.is_alive() and not errors
    assert_released_unknown(control, native_facade.proxy)


def test_native_disconnect_progress_is_caller_owned_and_confirmed_shutdown_clears_plot(control, native_facade):
    c = control.c
    report = {"modules": {1: {"positive_v": 0., "negative_v": 0., "measured_a": 0.},
                          2: {"positive_v": 0., "negative_v": 0., "measured_a": 0.}},
              "consecutive": 3, "limit_v": 1.}
    native_facade.routes["disconnect"] = ("progress", [report])
    reports = []
    original = c._on_discharge_progress
    def progress(value):
        reports.append((threading.get_ident(), value))
        original(value)
    c._on_discharge_progress = progress
    caller = threading.get_ident()
    assert c.shutdownCommunication() is True
    assert reports == [(caller, report)]
    assert c.main_state == "Disconnected" and not c.shutdown_unconfirmed
    assert c.device is None and native_facade.proxy.closed
    assert not c.discharge_readings and not control.parent.recording
    assert not control.parent.onAction.state
    assert all(np.isnan(channel.monitor) for channel in control.channels)


@pytest.mark.parametrize('response', ['negative', 'error'])
def test_healthy_native_unconfirmed_shutdown_keeps_on_and_allows_verified_off_retry(control, native_facade, response):
    c = control.c
    native_facade.routes['disconnect'] = ('echo', [False]) if response == 'negative' else ('fail', [])
    assert c.shutdownCommunication() is False
    assert c.device is native_facade.driver and c.initialized
    assert c.main_state == 'Shutdown unconfirmed' and c.shutdown_unconfirmed
    assert control.parent.onAction.state and not control.parent.recording
    assert c._output_cancel.is_set() and not c.acquiring
    assert not native_facade.proxy.closed and native_facade.proxy._process.poll() is None
    assert native_facade.driver.connected is True
    assert all(np.isnan(channel.monitor) for channel in control.channels)
    assert all(np.isnan(channel.value) for channel in control.channels[3:])
    report = {'modules': {1: {'positive_v': 0., 'negative_v': 0., 'measured_a': 0.},
                          2: {'positive_v': 0., 'negative_v': 0., 'measured_a': 0.}},
              'consecutive': 3, 'limit_v': 1.}
    native_facade.routes['disconnect'] = ('progress', [report])
    control.parent.onAction.state = False
    assert c.shutdownCommunication() is True
    assert c.device is None and not c.initialized and not c.shutdown_unconfirmed
    assert c.main_state == 'Disconnected' and not control.parent.onAction.state
    assert native_facade.proxy.closed and native_facade.proxy._process.poll() is not None


def test_native_facade_event_never_serialized_and_cancelled_command_never_enables_output(control, native_facade):
    cancel = threading.Event()
    cancel.set()
    native_facade.routes["set_output_active"] = ("echo", [True])
    with pytest.raises(RuntimeError, match="cancelled"):
        native_facade.driver.set_output_active(1, True, cancel_event=cancel, timeout_s=.2)
    assert not native_facade.proxy.closed and native_facade.proxy._pending == {}
    assert native_facade.proxy._active is None
