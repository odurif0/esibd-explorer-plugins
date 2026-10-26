"""Exercise the plugin OFF path with the actual discharge-checking runtime."""
import threading
import time
from types import SimpleNamespace

import pytest

from support.esi import load_plugin as _load_plugin
from support.esi_current import snapshot


@pytest.fixture
def control(esi_discharge_rig):
    module = _load_plugin()
    action = SimpleNamespace(state=False)  # the operator has just requested OFF
    parent = SimpleNamespace(onAction=action, isOn=lambda: action.state,
                             connect_timeout_s=.5, poll_timeout_s=.5,
                             getChannels=lambda: [], com=16)
    controller = module.ESIController(parent)
    controller.initialized = controller.acquiring = True
    controller.main_state = "ST_ON"
    controller.device = esi_discharge_rig.driver
    controller.errorCount = 0
    messages, statuses = [], []
    controller.print = lambda message, **kw: messages.append(message)
    controller._sync_status = lambda: statuses.append((controller.main_state, action.state))
    return SimpleNamespace(module=module, controller=controller, parent=parent,
                           statuses=statuses, messages=messages)


def test_complete_shutdown_with_runtime(control, esi_discharge_rig):
    c = control.controller
    assert c.shutdownCommunication()
    assert (control.module._ESI_STOPPING, True) in control.statuses
    assert control.statuses[-1] == ("Disconnected", False)
    assert c.device is None and not c.initialized and not c.acquiring
    assert esi_discharge_rig.calls[-1] == ("close",)
    assert esi_discharge_rig.calls.count(("disable",)) == 1
    assert not c.discharge_readings


def test_voltage_still_high_releases_communication_without_claiming_safe_off(control, esi_discharge_rig):
    esi_discharge_rig.values[2, True] = [-100.]
    c = control.controller
    assert not c.shutdownCommunication()
    assert c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert not c.initialized and c.device is None
    assert not control.parent.onAction.state
    assert not c.acquiring
    assert esi_discharge_rig.calls[-1] == ("close",)
    c._apply_snapshot(snapshot())
    c._on_discharge_progress({"modules": {}})
    assert c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert any("discharge" in message for message in control.messages)
    assert not c.shutdownCommunication(), "communication closure must not certify output shutdown"


def test_background_readback_cannot_erase_shutdown_check(control, esi_discharge_rig):
    c = control.controller
    c.main_state = control.module._ESI_STOPPING
    c.discharge_readings = {1: {"positive_v": 20., "negative_v": -15., "measured_a": 1e-9}}
    c._apply_snapshot(snapshot())
    c.readNumbers()
    assert c.main_state == control.module._ESI_STOPPING
    assert c.discharge_readings[1]["positive_v"] == 20.
    assert not esi_discharge_rig.calls


@pytest.mark.parametrize("state", ["stopping", "uncertain"])
def test_on_cannot_bypass_pending_shutdown(control, esi_discharge_rig, state):
    c = control.controller
    c.main_state = control.module._ESI_STOPPING if state == "stopping" else "Shutdown unconfirmed"
    control.parent.onAction.state = True
    c._output_cancel.set()
    c.toggleOn()
    assert c._output_cancel.is_set()
    assert not esi_discharge_rig.calls
    assert c.main_state != "Disconnected"


def test_initialization_failure_releases_backend_if_discharge_cannot_be_verified(control, esi_discharge_rig, monkeypatch, tmp_path):
    c = control.controller
    c.device = None
    c.main_state = "Disconnected"
    monkeypatch.setattr(control.module, "_get_esi_driver_class", lambda: lambda **kw: esi_discharge_rig.driver)
    esi_discharge_rig.driver.connect = lambda **kw: (_ for _ in ()).throw(RuntimeError("startup error"))
    control.parent.baudrate = 230400
    control.parent.pluginManager = SimpleNamespace(Settings=SimpleNamespace(dataPath=tmp_path))
    esi_discharge_rig.values[1, False] = [100.]
    c.runInitialization()
    assert c.device is None and not c.initialized
    assert c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert not control.parent.onAction.state
    assert esi_discharge_rig.calls[-1] == ("close",)
    assert not c.initializing


def test_reinitialize_releases_old_port_and_waits_for_an_explicit_new_on(control, esi_discharge_rig, monkeypatch):
    c = control.controller
    esi_discharge_rig.values[1, False] = [100.]
    constructed = []
    monkeypatch.setattr(control.module, "_get_esi_driver_class", lambda: constructed.append(True))
    c.runInitialization()
    assert c.device is None and not c.initialized
    assert c.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert not constructed
    assert c._dispose_device() is True, "the released port must permit the next ON"


def test_failed_on_rollback_requires_discharge_not_just_disable(control, esi_discharge_rig):
    esi_discharge_rig.values[1, False] = [50.]
    confirmed, _ = control.controller._force_safe_off_after_failure()
    assert not confirmed
    assert control.controller.device is None
    assert control.controller.main_state == control.module._ESI_DISCONNECTED_UNCONFIRMED
    assert esi_discharge_rig.calls[-1] == ("close",)


def test_late_poll_after_confirmed_close_does_not_resurrect_on(control, esi_discharge_rig):
    c = control.controller
    control.parent.onAction.state = True

    def poll(**kw):
        assert c.shutdownCommunication()
        return snapshot()

    esi_discharge_rig.driver.collect_diagnostics = poll
    c.readNumbers()
    assert c.main_state == "Disconnected"
    assert c.device is None and not c.initialized
    assert control.parent.onAction.state is False


def test_discharge_readings_are_reported_every_ten_seconds(control, esi_discharge_rig, monkeypatch):
    # 2026-10-22: the check ran silently for up to a minute and looked stuck.
    c = control.controller
    clock = [100.]
    monkeypatch.setattr(control.module.time, "monotonic", lambda: clock[0])
    c.initializeValues(reset=True)  # As the shutdown does before the check.
    c.main_state = control.module._ESI_STOPPING
    c._discharge_outputs_off = True  # The first report has already been received.
    c._discharge_started_at = c._discharge_reported_at = 100.
    report = {"consecutive": 1, "limit_v": 10.0,
              "modules": {1: {"positive_v": 4.9, "negative_v": -3.8, "measured_a": 1e-9},
                          2: {"positive_v": float("nan"), "negative_v": -0.04, "measured_a": 0.}}}
    before = len(control.messages)
    clock[0] = 105.
    c._on_discharge_progress(report)
    assert len(control.messages) == before
    clock[0] = 110.5
    c._on_discharge_progress(report)
    assert control.messages[-1] == ("ESI discharge check, 10 s: HV1 POS +4.9 V / NEG -3.8 V, "
                                    "HV2 POS n/a V / NEG -0.0 V; 1 consecutive rounds <= 10 V so far.")
    c._on_discharge_progress(report)
    assert len(control.messages) == before + 1


def test_unconfirmed_shutdown_reports_the_last_readings(control, esi_discharge_rig, monkeypatch):
    # 2026-10-23: the check timed out with HV1 still at about 3 V, and the error gave no value.
    report = {"consecutive": 0, "limit_v": 10.0,
              "modules": {1: {"positive_v": 2.849, "negative_v": -2.712, "measured_a": 0.},
                          2: {"positive_v": -0.045, "negative_v": -0.049, "measured_a": 0.}}}

    def disconnect(timeout_s, on_discharge):
        on_discharge(report)
        raise RuntimeError("ESI discharge unconfirmed: fresh ADC voltages on both polarities "
                           "did not remain below the discharge limit for consecutive rounds")

    monkeypatch.setattr(esi_discharge_rig.driver, "disconnect", disconnect)
    assert not control.controller.shutdownCommunication()
    assert any(message.endswith("for consecutive rounds. Last readings: HV1 POS +2.8 V / NEG -2.7 V, "
                                "HV2 POS -0.0 V / NEG -0.0 V.") for message in control.messages)


def test_off_returns_to_off_once_outputs_are_verified_and_on_waits(control, esi_discharge_rig, monkeypatch):
    # 2026-10-23: the operator waited for the whole discharge check with the button still ON.
    c = control.controller
    seen = []
    original = c._on_discharge_progress

    def progress(report):
        original(report)
        if not seen:
            seen.append((c.main_state, control.parent.onAction.state))
            control.parent.onAction.state = True  # The operator presses ON during the check.
            c.toggleOn()
            seen.append(control.parent.onAction.state)

    monkeypatch.setattr(c, "_on_discharge_progress", progress)
    assert c.shutdownCommunication()
    assert seen == [(control.module._ESI_STOPPING, False), False]
    assert control.parent._session_closed, "a refused ON is not recorded for a crash resume"
    assert "ESI outputs OFF; the HV discharge check continues in the background." in control.messages
    assert any("ON is possible once it ends" in message for message in control.messages)
    assert control.messages[-1].startswith("ESI HV discharge verified after ")
    assert control.statuses[-1] == ("Disconnected", False)
    assert esi_discharge_rig.calls.count(("close",)) == 1
    assert not c._discharge_outputs_off


@pytest.mark.parametrize("caller", ["explorer-closing", "worker-thread", "gui-click"])
def test_only_explorer_closing_waits_on_the_gui_thread_for_the_check(control, esi_discharge_rig, monkeypatch, caller):
    # 2026-10-26 lab: Close communication, clicked during the check, waited on the GUI thread and
    # then ran a second OFF there: Explorer froze for 33 s.
    c = control.controller
    monkeypatch.setattr(control.module, "_on_gui_thread", lambda: caller != "worker-thread")
    control.parent.pluginManager = SimpleNamespace(closing=caller == "explorer-closing")
    driver = esi_discharge_rig.driver
    disconnect = driver.disconnect
    checking, finish = threading.Event(), threading.Event()

    def slow_disconnect(**kwargs):
        on_discharge = kwargs["on_discharge"]

        def progress(report):
            on_discharge(report)
            checking.set()
            assert finish.wait(5.)
        return disconnect(**{**kwargs, "on_discharge": progress})

    monkeypatch.setattr(driver, "disconnect", slow_disconnect)
    off = threading.Thread(target=c.shutdownCommunication)
    off.start()
    assert checking.wait(5.)
    threading.Timer(.2, finish.set).start()
    started = time.monotonic()
    if caller == "gui-click":
        assert not c.shutdownCommunication()
        assert time.monotonic() - started < .1, "the GUI thread must not wait for the check"
        assert "communication closes when it ends" in control.messages[-1]
    else:
        assert c.shutdownCommunication(), "closing must report the verdict of the running check"
    off.join(5.)
    assert c.main_state == "Disconnected" and c.device is None
    assert not any("attempting to release communication" in message for message in control.messages)
    assert esi_discharge_rig.calls.count(("close",)) == 1


@pytest.mark.parametrize("outputs_off,expected", [(False, "Stopping: checking HV"), (True, "OFF: HV discharging")])
def test_badge_tells_when_only_the_discharge_check_remains(control, outputs_off, expected):
    class Label:
        def setText(self, text):
            self.text = text

    device = SimpleNamespace(
        main_state=control.module._ESI_STOPPING, statusBadgeLabel=Label(), statusSummaryLabel=Label(),
        controller=SimpleNamespace(_discharge_outputs_off=outputs_off),
        _status_summary_text=lambda: "", _status_badge_style=lambda: "",
        _sync_load_config_action=lambda: None, _update_operator_panel=lambda: None)
    control.module.ESIDevice._update_status_widgets(device)
    assert device.statusBadgeLabel.text == expected


@pytest.mark.parametrize("closing", [True, False])
def test_explorer_closing_closes_communication_before_the_gui(control, monkeypatch, closing):
    calls = []
    monkeypatch.setattr(control.module.Device, "closeGUI", lambda self: calls.append("gui"), raising=False)

    class Closing(control.module.ESIDevice):
        def __init__(self):  # noqa: D107 - no Explorer plugin setup is needed here
            pass

    device = Closing()
    device.controller = SimpleNamespace(closeCommunication=lambda: calls.append("communication"))
    device.pluginManager = SimpleNamespace(closing=closing)
    device.closeGUI()
    assert calls == (["communication", "gui"] if closing else ["gui"])
