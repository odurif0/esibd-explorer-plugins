"""Replies the AMPR did not send in time are reported with how each setpoint ended.

2026-10-22/23 lab: the first command to module 0 after reconnecting, or after the PSU was enabled,
got no reply within the DLL's 1.5 s (-10), every later command answered, and the start failed.
"""
import re
from types import SimpleNamespace

import pytest
from support.ampr import load_plugin

TRACE = r"C:\Data\logs\ampr_a\ampr_startup_com13_20261023T131352Z.log"
SET = {"time": 0., "export": "SetModuleOutputVoltage", "inputs": [0, 0, 0.0], "status": -10,
       "elapsed_s": 1.5, "trace": TRACE}


def text(rig, event):
    return rig.module._receive_error_text(event)


def test_an_applied_setpoint_only_lost_its_reply(ampr_ramp_rig):
    message = text(ampr_ramp_rig, dict(SET, recovery={"applied": True, "readback_v": 0.0, "resend_status": None}))
    assert re.fullmatch(
        r"AMPR SetModuleOutputVoltage \(module 0 CH1, 0\.000 V\) got no reply within 1\.50 s at "
        r"\d\d:\d\d:\d\d \(status -10\)\. The read-back setpoint shows it was applied: only the reply "
        r"was lost\. DLL trace: " + re.escape(TRACE), message)


@pytest.mark.parametrize("readback,resent,expected", [
    (5.0, 0, "Not applied (read back 5.000 V); sent once more: accepted."),
    (None, -10, "Not applied (read back unreadable); sent once more: status -10."),
])
def test_an_unapplied_setpoint_tells_how_its_single_resend_ended(ampr_ramp_rig, readback, resent, expected):
    event = dict(SET, recovery={"applied": False, "readback_v": readback, "resend_status": resent})
    assert expected in text(ampr_ramp_rig, event)


def test_a_lost_read_names_its_module(ampr_ramp_rig):
    event = {"time": 0., "export": "GetModuleFwVersion", "inputs": [0], "status": -10, "elapsed_s": 1.545}
    message = text(ampr_ramp_rig, event)
    assert message.startswith("AMPR GetModuleFwVersion (module 0) got no reply within 1.54 s at ")
    assert "DLL trace" not in message and "applied" not in message


def test_report_drains_the_worker_record_once(ampr_ramp_rig):
    c = ampr_ramp_rig.controller
    pending = [dict(SET, recovery={"applied": True}), "not an event"]
    c.device.take_receive_errors = lambda timeout_s: [pending.pop(0) for _ in list(pending)]
    c._report_receive_errors()
    assert [message for message in ampr_ramp_rig.logs if "got no reply" in message] == [
        text(ampr_ramp_rig, dict(SET, recovery={"applied": True}))]
    c._report_receive_errors()
    assert sum("got no reply" in message for message in ampr_ramp_rig.logs) == 1


def test_report_never_breaks_on_an_older_worker_or_a_failed_call(ampr_ramp_rig):
    c = ampr_ramp_rig.controller
    c._report_receive_errors()  # No such method: nothing printed.

    def broken(timeout_s):
        raise RuntimeError("worker gone")

    c.device.take_receive_errors = broken
    c._report_receive_errors()
    assert not any("got no reply" in message for message in ampr_ramp_rig.logs)


def test_the_native_facade_routes_the_drain_to_the_worker():
    # 2026-10-28/30 lab: the worker logged "Unknown AMPR attribute: take_receive_errors" and the
    # recovered -10 never reached the operator: the facade asked for an attribute, not a method.
    module = load_plugin()
    facade_cls = module._get_ampr_driver_class()
    facade = facade_cls.__new__(facade_cls)
    calls = []

    class Backend:
        session = "native"

        def call_method(self, name, *args, **kwargs):
            calls.append(name)
            return [dict(SET)]

        def get_attribute(self, name, **kwargs):
            raise AssertionError(f"attribute RPC for {name}")

    object.__setattr__(facade, "_backend_mode", "process")
    object.__setattr__(facade, "_backend", Backend())
    assert facade.take_receive_errors(timeout_s=1.) == [dict(SET)]
    assert calls == ["take_receive_errors"]


@pytest.mark.parametrize("state", ["gui", "closing", "worker-thread"])
def test_general_close_uses_the_off_path_and_never_waits_on_the_gui_thread(monkeypatch, state):
    # 2026-10-30 lab: Close communication held the GUI thread for 1.8 s and skipped the ramp-down.
    module = load_plugin()
    calls = []
    monkeypatch.setattr(module, "_on_gui_thread", lambda: state != "worker-thread")
    monkeypatch.setattr(module, "getTestMode", lambda: False)
    monkeypatch.setattr(module, "_session_clear", lambda device: None)

    class Closing(module.AMPRDevice):
        def __init__(self):  # noqa: D107 - no Explorer plugin setup is needed here
            pass

        def setOn(self, on=None):
            calls.append(("setOn", on))

        def shutdownCommunication(self):
            calls.append("shutdown")

    device = Closing()
    device.useOnOffLogic, device.onAction = True, SimpleNamespace(state=True)
    device.controller = SimpleNamespace(initialized=True, _forced_close_state=None)
    device.pluginManager = SimpleNamespace(closing=state == "closing")
    device.closeCommunication()
    assert calls == ([("setOn", False)] if state == "gui" else ["shutdown"])


def test_explorer_exit_skips_the_descent_and_waits_for_the_verified_shutdown(ampr_ramp_rig, monkeypatch):
    c = ampr_ramp_rig.controller
    module = ampr_ramp_rig.module
    c.transitioning, c.transition_target_on = True, False
    c._abort_ramp_down = False
    waited = []

    def finished():
        waited.append(c._abort_ramp_down)
        c.transitioning = False

    monkeypatch.setattr(module, "_process_gui_events", finished)
    c.finish_off_before_exit()
    assert waited == [True], "a further OFF skips what is left of the ramp-down"
    assert not c.transitioning


def test_explorer_exit_reports_a_shutdown_that_does_not_end(ampr_ramp_rig, monkeypatch):
    c = ampr_ramp_rig.controller
    monkeypatch.setattr(ampr_ramp_rig.module, "_AMPR_EXIT_WAIT_S", .05)
    c.transitioning, c.transition_target_on = True, False
    c.finish_off_before_exit()
    assert any("still running" in message for message in ampr_ramp_rig.logs)
