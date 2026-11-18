"""Behavior checks for the standalone ESIBD Explorer DMMR plugin."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import numpy as np
import pytest
from support.dmmr import load_plugin


PLUGIN_PATH = (
    Path(__file__).resolve().parents[1]
    / "dmmr"
    / "dmmr_plugin.py"
)


def test_bootstrap_config_is_replaced_from_detected_modules():
    module = load_plugin()
    default_item = {
        "Module": "0",
        "Real": True,
        "Enabled": True,
    }

    bootstrap_items = [
        {"Name": f"DMMR{index}", "Module": 0, "Real": True, "Enabled": True}
        for index in range(1, 7)
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=bootstrap_items,
        detected_modules=[2, 5],
        device_name="DMMR",
        default_item=default_item,
    )

    assert synced_items == [
        {"Name": "DMMR_M02", "Module": "2", "Real": True, "Enabled": True},
        {"Name": "DMMR_M05", "Module": "5", "Real": True, "Enabled": True},
    ]
    assert log_entries == [("DMMR bootstrap config replaced from hardware scan.", None)]


def test_existing_config_is_merged_and_duplicates_are_neutralized():
    module = load_plugin()

    current_items = [
        {"Name": "Keep", "Module": 1, "Real": True, "Enabled": True},
        {"Name": "MissingLater", "Module": 3, "Real": True, "Enabled": False},
        {"Name": "Duplicate", "Module": "1", "Real": True, "Enabled": True},
    ]

    synced_items, log_entries = module._plan_channel_sync(
        current_items=current_items,
        detected_modules=[1, 2],
        device_name="DMMR",
    )

    assert synced_items[0]["Real"] is True
    assert synced_items[1]["Real"] is False
    assert synced_items[2]["Real"] is False
    assert synced_items[3] == {
        "Name": "DMMR_M02",
        "Module": "2",
        "Real": True,
        "Enabled": True,
    }
    assert ("Added generic DMMR channels for detected modules: 2", None) in log_entries
    assert ("Marked DMMR channels virtual because modules are absent: 3", None) in log_entries
    assert (
        "Duplicate DMMR mapping detected for module 1: Duplicate",
        module.PRINT.WARNING,
    ) in log_entries


def test_controller_toggle_off_failure_restores_on_ui_state():
    module = load_plugin()
    ui_states = []
    log_messages = []

    class FakeDevice:
        NO_ERR = 0

        def set_automatic_current(self, enabled, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", "ST_ON"

        def get_device_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["DEVICE_OK"]

        def get_voltage_state(self, timeout_s=None):
            return self.NO_ERR, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"]

        def get_temperature_state(self, timeout_s=None):
            return self.NO_ERR, "0x0000", ["TEMPERATURE_OK"]

        def format_status(self, status):
            return str(status)

    parent = types.SimpleNamespace(
        connect_timeout_s=7.0,
        poll_timeout_s=2.0,
        isOn=lambda: False,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.print = lambda message, flag=None: log_messages.append((message, flag))

    controller.toggleOn()

    assert ui_states == [True]
    assert any(
        "Failed to toggle DMMR acquisition:" in message
        for message, _flag in log_messages
    )


def test_controller_retains_backend_and_unconfirmed_shutdown_when_transport_is_unusable():
    module = load_plugin()
    ui_states = []

    class FakeDevice:
        def get_state(self, timeout_s=None):
            raise RuntimeError(
                "DMMR DLL call timed out during 'get_state'. "
                "The DMMR instance is now marked unusable."
            )

        def get_device_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_voltage_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

        def get_temperature_state(self, timeout_s=None):
            raise RuntimeError("transport unusable")

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.transitioning = True
    controller.transition_target_on = True

    controller._update_state()

    assert controller.main_state == module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert controller._forced_close_state == module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.acquiring is False
    assert controller.initialized is True
    assert isinstance(controller.device, FakeDevice)
    assert controller.transitioning is False
    assert controller.transition_target_on is None
    assert ui_states == [True]
    assert not hasattr(controller.signalComm.closeCommunicationSignal, 'last_emit')


def test_controller_retains_shutdown_uncertainty_after_repeated_state_failures():
    module = load_plugin()
    ui_states = []

    class FakeDevice:
        def get_state(self, timeout_s=None):
            raise RuntimeError("serial timeout while reading state")

        def get_device_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def get_voltage_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def get_temperature_state(self, timeout_s=None):
            raise RuntimeError("serial timeout")

        def disconnect(self):
            return True

        def close(self):
            return None

    parent = types.SimpleNamespace(
        poll_timeout_s=2.0,
        _set_on_ui_state=lambda on: ui_states.append(on),
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller.device = FakeDevice()
    controller.initialized = True
    controller.acquiring = True
    controller.transitioning = True
    controller.transition_target_on = True

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()
    assert controller.main_state == "State error"
    assert controller.device is not None

    controller._update_state()

    assert controller.main_state == module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert isinstance(controller.device, FakeDevice)
    assert controller.initialized is True
    assert controller.acquiring is False
    assert not hasattr(controller.signalComm.closeCommunicationSignal, 'last_emit')
    assert ui_states == [True]


def test_close_communication_preserves_forced_communication_lost_state():
    module = load_plugin()

    parent = types.SimpleNamespace(
        isOn=lambda: False,
        _update_status_widgets=lambda: None,
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
    )

    controller = module.DMMRController(parent)
    controller._forced_close_state = module._DMMR_COMMUNICATION_LOST_STATE

    controller.closeCommunication()

    assert controller.main_state == module._DMMR_COMMUNICATION_LOST_STATE
    assert controller.device_state_summary == "Unknown"
    assert controller.voltage_state_summary == "Unknown"
    assert controller.temperature_state_summary == "Unknown"


def test_controller_lock_section_rejects_unsupported_lock_type():
    module = load_plugin()

    controller = module.DMMRController(types.SimpleNamespace())
    controller.lock = object()

    with pytest.raises(TypeError, match="must provide either acquire_timeout"):
        with controller._controller_lock_section("boom"):
            pass


def test_controller_formats_open_port_timeout_with_operator_hint():
    module = load_plugin()

    parent = types.SimpleNamespace(com=3)
    controller = module.DMMRController(parent)

    message = controller._format_exception(
        RuntimeError(
            "DMMR DLL call timed out during 'open_port'. "
            "The device may be powered off or unresponsive. "
            "The DMMR instance is now marked unusable."
        )
    )

    assert "RuntimeError:" in message
    assert "Selected COM3 did not respond." in message
    assert "configured COM port is correct" in message


def test_controller_formats_open_port_error_with_operator_hint():
    module = load_plugin()

    parent = types.SimpleNamespace(com=3)
    controller = module.DMMRController(parent)

    message = controller._format_exception(
        RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    )

    assert "RuntimeError:" in message
    assert "Windows could not open COM3." in message
    assert "already in use" in message


def test_controller_update_values_clears_monitor_when_channel_is_disabled():
    module = load_plugin()

    class FakeChannel:
        def __init__(self, module, enabled):
            self._module = module
            self.real = True
            self.enabled = enabled
            self.monitor = 99.0

        def module_address(self):
            return self._module

    channel_enabled = FakeChannel(1, True)
    channel_disabled = FakeChannel(2, False)
    parent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [channel_enabled, channel_disabled],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.values = {1: 1.5e-12, 2: 2.5e-12}

    controller.updateValues()

    assert channel_enabled.monitor == 1.5e-12
    assert np.isnan(channel_disabled.monitor)


def test_controller_update_values_resyncs_monitor_widgets():
    module = load_plugin()

    class FakeChannel:
        def __init__(self, module, enabled):
            self._module = module
            self.real = True
            self.enabled = enabled
            self.monitor = 99.0
            self.sync_calls = 0

        def module_address(self):
            return self._module

        def _sync_monitor_widget(self):
            self.sync_calls += 1

    channel_enabled = FakeChannel(1, True)
    channel_disabled = FakeChannel(2, False)
    parent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: [channel_enabled, channel_disabled],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)
    controller.values = {1: 1.5e-12, 2: 2.5e-12}

    controller.updateValues()

    assert channel_enabled.monitor == 1.5e-12
    assert np.isnan(channel_disabled.monitor)
    assert channel_enabled.sync_calls == 1
    assert channel_disabled.sync_calls == 1


def test_dmmr_monitor_widget_formats_currents_with_si_prefixes():
    module = load_plugin()

    widget = module._DMMRCurrentMonitorSpinBox(indicator=True, displayDecimals=3)

    assert widget.decimals() == 1000
    assert widget.textFromValue(1.5e-12) == "1.5 pA"
    assert widget.textFromValue(-2.0e-9) == "-2 nA"
    assert widget.textFromValue(np.nan) == "NaN"


def test_upgrade_monitor_widget_replaces_default_numeric_widget():
    module = load_plugin()

    class FakeParameter:
        def __init__(self):
            self.widget = object()
            self.value = 1.5e-12
            self.apply_calls = 0

        def applyWidget(self):
            self.apply_calls += 1

        def getWidget(self):
            return self.widget

    parameter = FakeParameter()
    sync_calls = []
    fake_channel = types.SimpleNamespace(
        MONITOR="Monitor",
        monitor=1.5e-12,
        getParameterByName=lambda name: parameter if name == "Monitor" else None,
        _set_monitor_widget_minimum_width=lambda *args, **kwargs: None,
        _sync_monitor_widget=lambda: sync_calls.append(True),
    )

    module.DMMRChannel._upgrade_monitor_widget(fake_channel)

    assert isinstance(parameter.widget, module._DMMRCurrentMonitorSpinBox)
    assert parameter.apply_calls == 1
    assert parameter.value == 1.5e-12
    assert sync_calls == [True]


def test_sync_monitor_widget_does_not_overwrite_numeric_spinbox_text():
    module = load_plugin()

    class FakeLineEdit:
        def __init__(self):
            self.text_calls = []
            self.tooltip = None

        def setText(self, text):
            self.text_calls.append(text)

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    class FakeSpinWidget:
        def __init__(self):
            self.line_edit = FakeLineEdit()
            self.tooltip = None
            self.update_calls = 0

        def lineEdit(self):
            return self.line_edit

        def setValue(self, _value):
            return None

        def textFromValue(self, _value):
            return "unused"

        def update(self):
            self.update_calls += 1

        def setToolTip(self, tooltip):
            self.tooltip = tooltip

    parameter = types.SimpleNamespace(
        toolTip="Measured DMMR module current.",
        getWidget=lambda: FakeSpinWidget(),
    )
    widget = parameter.getWidget()
    parameter.getWidget = lambda: widget
    fake_channel = types.SimpleNamespace(
        MONITOR="Monitor",
        monitor=1.5e-12,
        getParameterByName=lambda name: parameter if name == "Monitor" else None,
        _set_monitor_widget_minimum_width=lambda *args, **kwargs: None,
    )

    module.DMMRChannel._sync_monitor_widget(fake_channel)

    assert widget.line_edit.text_calls == []
    assert widget.update_calls == 1
    assert "1.5 pA" in widget.tooltip
    assert "1.5 pA" in widget.line_edit.tooltip


def test_set_monitor_widget_minimum_width_keeps_fixed_width():
    module = load_plugin()

    class FakeMetrics:
        def horizontalAdvance(self, text):
            return len(text) * 7

    class FakeLineEdit:
        def fontMetrics(self):
            return FakeMetrics()

    class FakeWidget:
        def __init__(self):
            self._minimum_width = 50
            self.line_edit = FakeLineEdit()

        def lineEdit(self):
            return self.line_edit

        def fontMetrics(self):
            return FakeMetrics()

        def minimumWidth(self):
            return self._minimum_width

        def setMinimumWidth(self, width):
            self._minimum_width = width

    widget = FakeWidget()
    fake_channel = types.SimpleNamespace()

    module.DMMRChannel._set_monitor_widget_minimum_width(
        fake_channel,
        widget,
    )

    assert widget.minimumWidth() > 60


def test_init_failure_guidance_explains_poisoned_port_recovery():
    """A timed-out open_port (inside initialize()) locks the COM port for the
    process lifetime; the init-failure guidance must tell the operator to
    restart ESIBD Explorer instead of letting retries loop on a confusing
    '-2 (Error opening port)'."""
    module = load_plugin()
    controller = module.DMMRController(types.SimpleNamespace(com=10))

    fatal_exc = RuntimeError(
        "DMMR DLL call timed out during 'open_port'. The device may be powered "
        "off or unresponsive. The DMMR instance is now marked unusable."
    )
    guidance1 = controller._init_failure_guidance(fatal_exc)
    assert "RESTART ESIBD Explorer" in guidance1
    assert controller._poisoned_com == 10

    retry_exc = RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    guidance2 = controller._init_failure_guidance(retry_exc)
    assert "RESTART ESIBD Explorer" in guidance2
    assert "without confirmed closure" in guidance2
    assert "every later retry" not in guidance1 + guidance2
    assert controller._poisoned_com == 10


def test_init_failure_guidance_after_a_stopped_worker_does_not_ask_for_a_restart():
    """Found 2026-11-14: the worker that held the port had been stopped and reaped, which frees the
    port, and the guidance still asked to restart Explorer (which stops every other device)."""
    module = load_plugin()
    controller = module.DMMRController(types.SimpleNamespace(com=10))
    controller._native_worker_retired_unconfirmed = True  # Set by _retire_native_worker; device is None.
    guidance = controller._init_failure_guidance(RuntimeError(
        "dmmr native worker transport is unusable: worker exited unexpectedly; exit=1; stderr=(empty)."))
    assert "does not need a restart" in guidance and "RESTART" not in guidance
    assert controller._poisoned_com is None


def test_init_failure_guidance_silent_without_prior_poisoning():
    module = load_plugin()
    controller = module.DMMRController(types.SimpleNamespace(com=10))

    assert controller._init_failure_guidance(
        RuntimeError("DMMR open_port failed: -2 (Error opening port)")
    ) == ""
    assert controller._poisoned_com is None


def test_run_initialization_does_not_emit_success_after_fatal_state_update():
    """A device destroyed by _update_state() must not look like 'Test mode'."""
    module = load_plugin()
    emitted = []

    class FakeDevice:
        def __init__(self):
            self.NO_ERR = 0

        def initialize(self, timeout_s=None):
            return {1: {}}

        def disconnect(self, timeout_s=None):
            return True

        def close(self):
            return None

    parent = types.SimpleNamespace(
        name="DMMR",
        com=10,
        baudrate=230400,
        connect_timeout_s=5.0,
        getChannels=lambda: [],
        main_state="",
        detected_modules="",
        device_state_summary="",
        voltage_state_summary="",
        temperature_state_summary="",
        _update_status_widgets=lambda: None,
    )

    controller = module.DMMRController(parent)

    class _Signal:
        def emit(self):
            emitted.append(1)

    class _SignalComm:
        initCompleteSignal = _Signal()

    controller.signalComm = _SignalComm()
    controller.print = lambda message, flag=None: None
    controller._dispose_device = lambda: None
    controller._restore_off_ui_state = lambda: None

    class _FakeDriverClass:
        def __new__(cls, **kwargs):
            return FakeDevice()

    original_getter = module._get_dmmr_driver_class
    module._get_dmmr_driver_class = lambda: _FakeDriverClass

    def fatal_update_state(*args, **kwargs):
        controller.device = None  # simulate fatal transport loss during update

    controller._update_state = fatal_update_state

    try:
        controller.runInitialization()
    finally:
        module._get_dmmr_driver_class = original_getter

    assert emitted == []
    assert controller.initializing is False


def test_open_port_rejects_out_of_range_com():
    module = load_plugin()
    runtime_name = module._bundled_runtime_module_name(
        Path(__file__).resolve().parents[1] / "dmmr"
    )
    module._get_dmmr_driver_class()
    base = sys.modules[f"{runtime_name}.dmmr.dmmr_base"].DMMRBase

    instance = object.__new__(base)
    with pytest.raises(ValueError, match="1..255"):
        instance.open_port(256)
    with pytest.raises(ValueError, match="1..255"):
        instance.open_port(0)


def test_update_values_marshals_monitor_widget_updates(monkeypatch):
    """updateValues() runs on the acquisition thread; monitor widget syncs
    must go through the GUI dispatcher, never run directly."""
    module = load_plugin()

    dispatched = []

    def fake_invoke(callback):
        dispatched.append(callback)

    monkeypatch.setattr(module, "_invoke_gui_callback", fake_invoke)

    class FakeChannel:
        def __init__(self):
            self.enabled = True
            self.real = True
            self.monitor = None
            self.address = 1

        def module_address(self):
            return self.address

        def _sync_monitor_widget(self):
            raise AssertionError("must not run directly from updateValues")

    channels = [FakeChannel()]
    controller = module.DMMRController(controllerParent=None)
    controller.values = {1: 1.5e-12}
    controller.controllerParent = types.SimpleNamespace(
        isOn=lambda: True,
        getChannels=lambda: channels,
    )
    controller._sync_status_to_gui = lambda: None

    controller.updateValues()

    assert len(dispatched) == 1
    # Even the monitor's Parameter setter must wait for the GUI dispatcher.
    assert channels[0].monitor is None
    channels[0]._sync_monitor_widget = lambda: None
    dispatched[0]()
    assert channels[0].monitor == 1.5e-12


# ---------------------------------------------------------------- through the DMMR's (scripted) native worker
# What the worker does behind each call (module polls, OFF gate commands, read recovery) is tested in
# native/worker/tests/dmmr_facade.rs.

def _worker_controller(module, *, on=True, channels=(1, 2)):
    from support.dmmr import FaultingDMMR

    class FakeChannel:
        def __init__(self, address):
            self._address, self.real, self.enabled = address, True, True

        def module_address(self):
            return self._address

    sync_calls = []
    parent = types.SimpleNamespace(
        connect_timeout_s=7.0, poll_timeout_s=2.5, isOn=lambda: on,
        getChannels=lambda: [FakeChannel(a) for a in channels], getConfiguredModules=lambda: list(channels),
        main_state="", detected_modules="", device_state_summary="", voltage_state_summary="",
        temperature_state_summary="", _update_status_widgets=lambda: sync_calls.append("sync"),
    )
    controller = module.DMMRController(parent)
    device = controller.device = FaultingDMMR()
    device.fail_start = None
    controller.initialized = True
    controller.detected_module_ids = list(channels)
    device.get_device_state = lambda **kw: (0, "0x0000", ["DEVICE_OK"])
    device.get_voltage_state = lambda **kw: (0, "0x0007", ["VS_3V3_OK", "VS_5V0_OK", "VS_12V_OK"])
    return controller, device, parent, sync_calls


def test_a_read_publishes_the_worker_poll_and_the_controller_state():
    module = load_plugin()
    controller, device, _, _ = _worker_controller(module)
    device.poll_currents = lambda modules, **kw: {"token": 7, "values": {1: 1e-12, 2: 2e-12},
                                                  "ranges": {1: 1, 2: 2}, "valid": {1: True, 2: True},
                                                  "recovery": None}
    controller.acquiring = True
    controller.readNumbers()
    assert controller.main_state == "ST_ON"
    assert controller.values == {1: 1e-12, 2: 2e-12} and controller.meas_ranges == {1: 1, 2: 2}
    assert controller.device_state_summary == "DEVICE_OK"
    assert controller.voltage_state_summary == "VS_3V3_OK, VS_5V0_OK, VS_12V_OK"


def test_a_read_takes_the_controller_lock_for_the_state_then_the_poll():
    module = load_plugin()
    controller, device, _, _ = _worker_controller(module, channels=(3,))
    sections = []
    original = controller._controller_lock_section

    def section(message, **kwargs):
        sections.append(message)
        return original(message, **kwargs)
    controller._controller_lock_section = section
    controller.acquiring = True
    controller.readNumbers()
    assert sections == ["Could not acquire lock to refresh the DMMR state.",
                        "Could not acquire lock to read DMMR modules."]
    assert controller.values == {3: 1e-12}


def test_off_verifies_both_gates_closes_the_port_and_syncs_the_status():
    module = load_plugin()
    controller, device, parent, sync_calls = _worker_controller(module, on=False)
    device.enabled = True
    controller.detected_modules_text = "3"
    controller.toggleOn()
    assert device.calls[:4] == [("automatic", False), ("enable", False), ("read", "automatic"), ("read", "enable")]
    assert ("disconnect",) in device.calls
    assert parent.main_state == "Disconnected"
    assert controller.device is None and controller.initialized is False
    assert sync_calls


def test_a_confirmed_shutdown_marks_the_dmmr_disconnected():
    module = load_plugin()
    controller, device, _, _ = _worker_controller(module)
    assert controller.shutdownCommunication() is True
    assert controller.main_state == "Disconnected"
    assert controller.device is None and controller.initialized is False


@pytest.mark.parametrize("failure", ["raises", "unconfirmed"])
def test_a_failed_shutdown_keeps_the_connection_for_another_off(failure):
    module = load_plugin()
    controller, device, _, _ = _worker_controller(module, on=False)
    if failure == "raises":
        def shutdown(**kwargs):
            raise RuntimeError("boom")
        device.shutdown = shutdown
    else:
        device.readback["enable"] = (0, True)
    assert controller.shutdownCommunication() is False
    assert controller.main_state == module._DMMR_SHUTDOWN_UNCONFIRMED_STATE
    assert controller.device is device and controller.initialized is True
