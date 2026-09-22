"""Live PSU setpoints must not restart the gates or rewrite the other rail."""
from copy import deepcopy
from threading import Event, Lock, Thread
from types import SimpleNamespace

import pytest

from psu_fakes import StatefulPSU
from test_psu_channels import load_psu
from test_psu_measurements import setup as dll_case


@pytest.fixture(params=[f"psu_{suffix}" for suffix in "abcde"])
def rig(request, monkeypatch):
    module = load_psu(request.param)
    parent = SimpleNamespace(
        name=request.param.upper(), getChannels=lambda: [], isOn=lambda: True,
        startup_timeout_s=1., poll_timeout_s=.1, interlock_monitoring=True,
    )
    controller = module.PSUController(parent)
    controller.lock = Lock()
    controller.initialized = True
    controller.errorCount = 0
    controller._update_state = lambda: None
    controller._sync_status_to_gui = lambda **kw: None
    messages = []
    controller.print = lambda msg, **kw: messages.append(msg)
    device = controller.device = StatefulPSU()
    device.enabled = True
    device.outputs = (True, True)
    device.interlocks = (True, True)
    device.voltages = {0: 100., 1: 240.}
    device.currents = {0: .01, 1: .02}
    # A stale housekeeping cache must not supply the start of a live ramp.
    controller.voltage_setpoint_values = {0: 0., 1: 500.}
    controller.full_range_by_channel = {0: True, 1: True}
    state = dict(output_enabled={0: True, 1: True}, full_range_enabled={0: False, 1: False},
                 voltage_values=dict(device.voltages), current_limit_values=dict(device.currents))
    monkeypatch.setattr(controller._output_cancel, "wait", lambda _: controller._output_cancel.is_set())
    return module, controller, device, state, messages


@pytest.mark.parametrize("channel,start,target", [
    (0, 100., 80.), (1, 100., 80.), (0, 100., 120.),
    (0, 380., 80.), (0, 80., 380.), (0, 100., 0.), (0, 0., 100.),
    (0, 100., 100.), (0, 100., 100.001),
])
@pytest.mark.parametrize("partial", [False, True])
def test_voltage_edit_only_steps_modified_channel_without_gate_commands(rig, channel, start, target, partial):
    module, controller, device, state, messages = rig
    device.voltages[channel] = start
    state["voltage_values"][channel] = target
    if partial:
        state = {"setpoints_only": True, "voltage_values": {channel: target}}
    other_voltage = device.voltages[1 - channel]
    controller.applyManualState(state)
    writes = device.calls
    assert all(call[0] == "voltage" and call[1] == channel for call in writes), writes
    if start == target:
        assert writes == []
    else:
        values = [start] + [call[2] for call in writes]
        assert values[-1] == target
        assert all(min(start, target) <= v <= max(start, target) for v in values)
        assert values == sorted(values, reverse=target < start)
        assert all(abs(b - a) <= module._PSU_VOLTAGE_RAMP_STEP_V for a, b in zip(values, values[1:]))
    assert device.voltages == {channel: target, 1 - channel: other_voltage}
    assert device.outputs == (True, True) and device.enabled
    assert controller.errorCount == 0, messages


def test_voltage_edit_through_real_driver_and_ctypes(dll_case):
    import ctypes

    controller, driver, calls, _status = dll_case
    controller.lock = Lock()
    controller.errorCount = 0
    controller.controllerParent.name = "PSU"
    controller.controllerParent.interlock_monitoring = True
    controller._update_state = lambda: None
    controller._sync_status_to_gui = lambda **kw: None
    messages = []
    controller.print = lambda msg, **kw: messages.append(msg)
    voltages, currents = {0: 100., 1: 240.}, {0: .01, 1: .02}
    ptr = ctypes.POINTER
    read_pair = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ptr(driver.WIN_BOOL), ptr(driver.WIN_BOOL))
    read_limits = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ctypes.c_uint,
                                  ptr(ctypes.c_double), ptr(ctypes.c_double))

    @read_pair
    def get_range(port, ch0, ch1):
        ch0[0] = ch1[0] = False
        return 0

    @read_pair
    def get_interlocks(port, output, bnc):
        output[0] = bnc[0] = True
        return 0

    @read_limits
    def get_voltage(port, channel, value, limit):
        value[0], limit[0] = voltages[channel], 1000.
        return 0

    @read_limits
    def get_current(port, channel, value, limit):
        value[0], limit[0] = currents[channel], 1.
        return 0

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint16, ctypes.c_uint, ctypes.c_double)
    def set_voltage(port, channel, value):
        calls.append(("SetPSUOutputVoltage", channel, value))
        voltages[channel] = value
        return 0

    driver.psu_dll.COM_HVPSU2D_GetPSUFullRange = get_range
    driver.psu_dll.COM_HVPSU2D_GetInterlockEnable = get_interlocks
    driver.psu_dll.COM_HVPSU2D_GetPSUSetOutputVoltage = get_voltage
    driver.psu_dll.COM_HVPSU2D_GetPSUSetOutputCurrent = get_current
    driver.psu_dll.COM_HVPSU2D_SetPSUOutputVoltage = set_voltage
    # No other DLL setters are provided: an enable/current/range command is an error.
    controller.applyManualState(dict(
        output_enabled={0: True, 1: True}, full_range_enabled={0: False, 1: False},
        voltage_values={0: 80., 1: 240.}, current_limit_values=currents,
    ))
    assert controller.errorCount == 0, messages
    assert [call for call in calls if isinstance(call, tuple)] == [("SetPSUOutputVoltage", 0, 80.)]
    assert voltages == {0: 80., 1: 240.}


@pytest.mark.parametrize("partial", [False, True])
def test_current_limit_edit_also_keeps_voltage_and_gates(rig, partial):
    _, controller, device, state, messages = rig
    state["current_limit_values"][0] = .015
    if partial:
        state = {"setpoints_only": True, "current_limit_values": {0: .015}}
    controller.applyManualState(state)
    assert device.calls == [("current", 0, .015)]
    assert device.voltages == {0: 100., 1: 240.}
    assert device.outputs == (True, True) and device.enabled
    assert controller.errorCount == 0, messages


def test_disabled_channel_stays_disabled_during_edit(rig):
    _, controller, device, state, messages = rig
    device.outputs = (False, True)
    state["output_enabled"][0] = False
    state["voltage_values"][0] = 80.
    controller.applyManualState(state)
    assert device.calls == [("voltage", 0, 80.)]
    assert device.outputs == (False, True) and device.enabled
    assert controller.errorCount == 0, messages


@pytest.mark.parametrize("fault", ["voltage_nan", "voltage_negative", "voltage_missing", "current_nan",
                                   "limit_missing", "limit_low", "current_limit_missing", "interlocks"])
def test_unverified_live_state_cannot_start_a_voltage_ramp(rig, fault):
    _, controller, device, state, messages = rig
    state["voltage_values"][0] = 380.
    if fault == "voltage_nan":
        device.voltages[0] = float("nan")
    elif fault == "voltage_negative":
        device.voltages[0] = -100.
    elif fault == "voltage_missing":
        device.get_channel_voltage_limits = None
    elif fault == "current_nan":
        device.currents[0] = float("nan")
    elif fault == "current_limit_missing":
        device.get_channel_current_limits = lambda ch, **kw: (device.currents[ch], None)
    elif fault == "interlocks":
        device.interlocks = (False, True)
    else:
        device.get_channel_voltage_limits = lambda ch, **kw: (device.voltages[ch],
                                                        None if fault == "limit_missing" else 200.)
    controller.applyManualState(state)
    assert not any(call[0] == "voltage" for call in device.calls)
    assert device.outputs == (False, False) and not device.enabled
    assert controller.errorCount == 1
    assert any("Failed to apply" in msg for msg in messages)


@pytest.mark.parametrize("fault", ["ignored_voltage", "lost_ack", "gate_off", "range_changed", "current_ignored"])
def test_failed_live_write_disables_without_retrying_a_cold_start(rig, fault):
    _, controller, device, state, messages = rig
    state["voltage_values"][0] = 80.
    setter = device.set_channel_voltage

    def write(ch, value, **kw):
        if fault != "ignored_voltage":
            setter(ch, value, **kw)
        if fault == "lost_ack":
            raise RuntimeError("ACK lost after applied write")
        if fault == "gate_off":
            device.outputs = (False, True)
        if fault == "range_changed":
            device.ranges = (True, False)

    device.set_channel_voltage = write
    if fault == "current_ignored":
        state["current_limit_values"][0] = .001
        device.set_channel_current = lambda *a, **kw: None
    controller.applyManualState(state)
    assert device.outputs == (False, False) and not device.enabled
    assert not any(call in [("outputs", True, True), ("global", True)] for call in device.calls)
    assert not any(call[0] == "voltage" and call[2] == 0 for call in device.calls)
    if fault == "current_ignored":
        assert not any(call[0] == "voltage" for call in device.calls)
    assert controller.errorCount == 1, messages
    assert "Applied PSU manual values." not in messages


def test_range_change_still_disables_before_relay_even_with_stale_cache(rig):
    _, controller, device, state, messages = rig
    state["full_range_enabled"][0] = True
    controller.full_range_by_channel = {0: True, 1: False}  # misleading cache
    switch = device.set_output_full_range
    observations = []

    def switch_range(*enabled, **kw):
        observations.append((device.outputs, device.get_channel_measured_voltage(0),
                             device.get_channel_measured_voltage(1)))
        switch(*enabled, **kw)

    device.set_output_full_range = switch_range
    controller.applyManualState(state)
    assert device.calls[0] == ("outputs", False, False)
    assert observations == [((False, False), 0., 0.)]
    assert device.ranges == (True, False)
    assert device.outputs == (True, True)
    assert controller.errorCount == 0, messages


def test_range_change_refused_if_disable_is_not_confirmed(rig):
    _, controller, device, state, messages = rig
    state["full_range_enabled"][0] = True
    device.set_output_enabled = lambda *a, **kw: None
    controller.applyManualState(state)
    assert not any(call[0] in ("voltage", "current", "range") for call in device.calls)
    assert any("not confirmed disabled before reconfiguration" in msg for msg in messages)


def test_queued_field_edits_preserve_each_other_and_cancel_on_off(rig):
    _, controller, device, _, messages = rig
    # Hold the worker to reproduce several GUI commits before it dequeues.
    controller._manual_apply_worker_running = True
    for key, channel, target in (("voltage_values", 0, 80.),
                                  ("current_limit_values", 1, .015),
                                  ("voltage_values", 1, 230.),
                                  ("voltage_values", 0, 70.)):
        controller.applyManualStateFromThread({"setpoints_only": True, key: {channel: target}})
    assert device.calls == []
    controller._manual_state_apply_worker()
    assert device.calls == [("current", 1, .015), ("voltage", 0, 70.), ("voltage", 1, 230.)]
    assert device.voltages == {0: 70., 1: 230.}
    assert device.currents == {0: .01, 1: .015}
    assert device.outputs == (True, True) and device.enabled
    assert controller.errorCount == 0, messages
    device.calls.clear()
    controller._manual_apply_worker_running = True
    controller.applyManualStateFromThread({"setpoints_only": True, "current_limit_values": {0: .001}})
    controller._cancel_output_commands()
    controller._manual_state_apply_worker()
    assert device.calls == [] and controller._manual_apply_pending_state is None


def test_cancel_during_live_readback_does_not_write(rig):
    _, controller, device, state, messages = rig
    state["voltage_values"][0] = 80.
    getter = device.get_channel_voltage_limits

    def read(ch, **kw):
        controller._cancel_output_commands()
        return getter(ch, **kw)

    device.get_channel_voltage_limits = read
    controller.applyManualState(state)
    assert device.calls == []
    assert "Applied PSU manual values." not in messages


@pytest.mark.parametrize("target", [80., 480.])
@pytest.mark.parametrize("partial", [False, True])
def test_off_interrupts_live_ramp_and_cancels_queued_edits(rig, target, partial):
    _, controller, device, state, messages = rig
    state["voltage_values"][0] = target
    if partial:
        state = {"setpoints_only": True, "voltage_values": {0: target}}
    started, resume = Event(), Event()
    setter = device.set_channel_voltage

    def slow_write(ch, value, **kw):
        setter(ch, value, **kw)
        if value > 0:
            started.set()
            assert resume.wait(3)

    device.set_channel_voltage = slow_write
    shutdown_results = []
    stop = Thread(target=lambda: shutdown_results.append(controller.shutdownCommunication()))
    try:
        controller.applyManualStateFromThread(state)
        assert started.wait(3)
        queued = deepcopy(state)
        queued["voltage_values"][1] = 500.
        controller.applyManualStateFromThread(queued)
        stop.start()
        assert Event.wait(controller._output_cancel, 3)
    finally:
        resume.set()
        if stop.ident is not None:
            stop.join(3)
    assert shutdown_results == [True]
    assert device.outputs == (False, False) and not device.enabled and not device.connected
    positive_writes = [call for call in device.calls if call[0] == "voltage" and call[2] > 0]
    assert len(positive_writes) == 1 and positive_writes[0][1] == 0
    assert "Applied PSU manual values." not in messages
