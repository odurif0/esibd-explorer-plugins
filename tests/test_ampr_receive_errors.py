"""Replies the AMPR did not send in time are reported with how each setpoint ended.

2026-10-22/23 lab: the first command to module 0 after reconnecting, or after the PSU was enabled,
got no reply within the DLL's 1.5 s (-10), every later command answered, and the start failed.
"""
import re

import pytest

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
