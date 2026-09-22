"""PSU Channel publication: magnitudes, physical IDs, validity and expiry.

The headless fixtures use plain Channels. The Qt integration test additionally
executes Explorer's actual Channel, Parameter, DeviceManager and UCM machinery.
"""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import numpy as np
import pytest

from test_psu_plugin_behavior import _install_esibd_stubs
from test_psu_measurements import setup as dll_case

ROOT = Path(__file__).resolve().parents[1]


def load_psu(folder):
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location(f"channel_{folder}", ROOT / folder / "psu_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def snapshot(positive=123., negative=234., enabled=(True, True)):
    return {
        "main_state": {"name": "ST_ON"}, "device_enabled": True,
        "output_enabled": enabled,
        "channels": [dict(channel=ch, enabled=enabled[ch],
                          voltage={"measured_v": voltage, "set_v": 999.})
                     for ch, voltage in enumerate((positive, negative))],
    }


def make_psu(folder="psu_a"):
    module = load_psu(folder)
    parent = module.PSUDevice.__new__(module.PSUDevice)
    parent.interval = 1000
    parent.loading = False
    parent.print = lambda *a, **kw: None
    parent.isOn = lambda: True
    parent.channels = [SimpleNamespace(
        id=ch, name=f"{parent.name}_CH{ch}", channelParent=parent,
        real=True, enabled=True, active=True, useMonitors=True, initialized=True,
        value=0., monitor=np.nan, unit="V", readback_status="PSU disconnected.",
        channel_number=lambda ch=ch: ch, getDevice=lambda: parent,
    ) for ch in (0, 1)]
    parent.getChannels = lambda: parent.channels
    controller = parent.controller = module.PSUController(parent)
    controller.device = SimpleNamespace(connected=True, _transport_poisoned=False)
    controller.initialized = True
    # Omit only unrelated toolbars/settings, not production Channel publication.
    controller._sync_status_to_gui = lambda sync_manual_panel=False: module._invoke_gui_callback(
        lambda: controller._update_channel_values(sync_setpoints=sync_manual_panel))
    return module, parent, controller


@pytest.fixture(params=[f"psu_{suffix}" for suffix in "abcde"])
def rig(request, monkeypatch):
    module, parent, controller = make_psu(request.param)
    clock = SimpleNamespace(now=100.)
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock.now))
    controller._apply_snapshot(snapshot(), refreshed_at=clock.now)
    return module, parent, controller, clock


def monitors(parent):
    return {ch.id: ch.monitor for ch in parent.getChannels()}


def test_standard_monitors_are_vget_not_indices_or_vset(rig):
    _, parent, controller, _ = rig
    controller._read_live_readbacks = lambda **kw: pytest.fail("Unexpected hardware read")
    controller._update_state = lambda **kw: pytest.fail("Unexpected hardware refresh")
    controller.updateValues()
    assert monitors(parent) == {0: 123., 1: 234.}
    assert [ch.value for ch in parent.channels] == [999., 999.]
    assert not hasattr(parent, "get_hv_readback")
    assert not hasattr(controller, "get_hv_readback")


def test_mapping_survives_reorder_removal_rename_and_virtual_channels(rig):
    _, parent, controller, _ = rig
    parent.channels.reverse()
    parent.channels[0].name = "User supplied negative rail"
    controller.updateValues()
    assert monitors(parent) == {1: 234., 0: 123.}
    parent.channels.pop()
    controller.updateValues()
    assert monitors(parent) == {1: 234.}
    parent.channels[0].real = False
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor)


@pytest.mark.parametrize("value", (None, float("nan"), float("inf"), -1.))
def test_invalid_measurement_never_becomes_zero_or_absolute_value(rig, value):
    _, parent, controller, clock = rig
    controller._apply_snapshot(snapshot(negative=value), refreshed_at=clock.now)
    assert parent.channels[0].monitor == 123.
    assert np.isnan(parent.channels[1].monitor)
    assert "invalid" in parent.channels[1].readback_status


def test_disabled_and_missing_measurements_do_not_reuse_old_values(rig):
    _, parent, controller, clock = rig
    controller._apply_snapshot(snapshot(enabled=(False, True)), refreshed_at=clock.now)
    assert np.isnan(parent.channels[0].monitor)
    assert parent.channels[1].monitor == 234.
    assert all(ch.enabled for ch in parent.channels), "HV OFF must not disable the Explorer Channel"
    controller._apply_live_readbacks({"device_enabled": True, "output_enabled": (True, True),
                                     "values": {0: 12.}}, refreshed_at=clock.now)
    assert parent.channels[0].monitor == 12.
    assert np.isnan(parent.channels[1].monitor)
    controller._apply_snapshot(snapshot(0., 0.), refreshed_at=clock.now)
    assert monitors(parent) == {0: 0., 1: 0.}, "A measured zero is valid"
    parent.channels[0].enabled = False
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor)


def test_early_qt_timer_reschedules_remaining_expiry(rig, monkeypatch):
    import sys
    _, parent, controller, clock = rig
    callbacks = []
    monkeypatch.setitem(sys.modules, "PyQt6.QtCore", SimpleNamespace(
        QTimer=SimpleNamespace(singleShot=lambda ms, cb: callbacks.append((ms, cb)))))
    monkeypatch.setitem(sys.modules, "PyQt6.QtWidgets", SimpleNamespace(
        QApplication=SimpleNamespace(instance=lambda: True)))
    controller._schedule_readback_expiry(controller._hv_readback)
    assert callbacks[0][0] == 2001
    clock.now = 101.91
    callbacks.pop(0)[1]()  # Qt coarse timers may fire up to 5% early.
    assert parent.channels[0].monitor == 123.
    assert len(callbacks) == 1 and 80 < callbacks[0][0] < 100
    clock.now = 102.01
    callbacks.pop(0)[1]()
    assert not callbacks
    assert np.isnan(parent.channels[0].monitor)


def test_expiry_updates_channels_not_a_separate_consumer_cache(rig):
    _, parent, controller, clock = rig
    clock.now = 101.9
    controller.updateValues()
    assert parent.channels[0].monitor == 123.
    clock.now = 102.
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor)
    assert "expired" in parent.channels[0].readback_status
    parent.interval = 5000
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor), "Interval change cannot revive an old sample"
    controller._apply_snapshot(snapshot(), refreshed_at=clock.now)
    clock.now = 110.
    controller.updateValues()
    assert parent.channels[0].monitor == 123.
    clock.now = 112.
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor)


@pytest.mark.parametrize("state", ("Disconnected", "Shutdown unconfirmed", "ST_ERR_ILOCK", "ST_STBY"))
def test_bad_state_invalidates_standard_monitors(rig, state):
    _, parent, controller, _ = rig
    controller.main_state = state
    controller.updateValues()
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


@pytest.mark.parametrize("flag", ("initializing", "transitioning", "_manual_apply_active", "_manual_apply_worker_running", "_hv_config_loading"))
def test_pending_changes_are_not_current_readbacks(rig, flag):
    _, parent, controller, _ = rig
    setattr(controller, flag, True)
    controller.updateValues()
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


def test_off_reset_and_reconnect_do_not_revive_readings(rig):
    _, parent, controller, clock = rig
    clock.now += .5
    controller._cancel_output_commands()
    assert np.isnan(parent.channels[0].monitor)
    controller._output_cancel = Event()
    controller._apply_snapshot(snapshot(), refreshed_at=100.)
    assert np.isnan(parent.channels[0].monitor), "Late pre-OFF sample"
    controller._apply_snapshot(snapshot(), refreshed_at=clock.now)
    assert parent.channels[0].monitor == 123.
    controller.initializeValues(reset=True)
    assert np.isnan(parent.channels[0].monitor)
    controller._apply_snapshot(snapshot(), refreshed_at=clock.now)
    controller.device = SimpleNamespace(connected=True)
    controller.updateValues()
    assert np.isnan(parent.channels[0].monitor)


@pytest.mark.parametrize("case", ("missing_controller", "missing_device", "disconnected", "uninitialized", "poisoned"))
def test_unavailable_connection_never_exposes_cached_voltages(rig, case):
    _, parent, controller, _ = rig
    if case == "missing_controller": parent.controller = None
    elif case == "missing_device": controller.device = None
    elif case == "disconnected": controller.device.connected = False
    elif case == "uninitialized": controller.initialized = False
    elif case == "poisoned": controller.device._transport_poisoned = True
    controller.updateValues()
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


@pytest.mark.parametrize("method", ("readNumbers", "_update_state"))
def test_failed_live_read_after_housekeeping_invalidates_channels(rig, method):
    _, parent, controller, _ = rig
    controller._last_housekeeping_refresh_monotonic = 0.
    controller.device.collect_housekeeping = lambda **kw: snapshot()
    controller._read_live_readbacks = lambda **kw: (_ for _ in ()).throw(RuntimeError("read failed"))
    getattr(controller, method)()
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


def test_lock_timeout_invalidates_channels(rig):
    _, parent, controller, _ = rig
    @contextmanager
    def busy(*args, **kwargs):
        raise TimeoutError("busy")
        yield
    controller._controller_lock_section = busy
    controller.readNumbers()
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


def test_off_cancels_commands_before_notifying_channel_consumers(rig):
    _, _, controller, _ = rig
    observed = []
    controller._invalidate_hv_readback = lambda: observed.append(controller._output_cancel.is_set())
    controller._cancel_output_commands()
    assert observed == [True]
    assert controller._manual_apply_pending_state is None


def test_aborted_manual_write_does_not_restore_previous_sample(rig):
    _, parent, controller, _ = rig
    controller._apply_manual_state_unlocked = lambda *args: False
    controller.applyManualState({})
    assert all(np.isnan(ch.monitor) for ch in parent.channels)


def test_late_read_and_unconfirmed_shutdown_stay_invalid(rig):
    _, parent, controller, clock = rig
    def late_read(**kwargs):
        clock.now += .5
        controller._invalidate_hv_readback()
        return snapshot()
    controller.device.collect_housekeeping = late_read
    controller._read_live_readbacks = lambda **kw: None
    controller._update_state()
    assert np.isnan(parent.channels[0].monitor)
    controller.main_state = "Shutdown unconfirmed"
    controller._apply_live_readbacks({"device_enabled": True, "output_enabled": (True, True),
                                     "values": {0: 12., 1: 13.}}, refreshed_at=clock.now)
    controller.updateValues()
    assert controller.main_state == "Shutdown unconfirmed"
    assert np.isnan(parent.channels[0].monitor)


def test_real_ctypes_readback_is_published_without_extra_dll_calls(dll_case):
    controller, _driver, calls, _status = dll_case
    controller.hardware_main_state = "ST_ON"
    controller._apply_live_readbacks(controller._read_live_readbacks(timeout_s=.5))
    before = list(calls)
    controller.updateValues()
    assert [ch.monitor for ch in controller.controllerParent.getChannels()] == [1., 2.]
    assert calls == before
