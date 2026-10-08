"""PSU: the last Vset/Ilim the PSU held are proposed again in the panel at the next connection.

User decision (2026-10-08): prefill only (nothing is sent before the operator validates a field or
turns the output ON), and the remembered values are the PSU's last setpoints.
"""
from __future__ import annotations

import json
import types

import numpy as np
import pytest

from test_psu_channels import make_psu


@pytest.fixture(params=[f"psu_{suffix}" for suffix in "abcde"])
def psu(request, tmp_path):
    module, parent, controller = make_psu(request.param)
    parent.pluginManager = types.SimpleNamespace(Settings=types.SimpleNamespace(configPath=tmp_path))
    parent.com, parent.operating_config = 5, -1
    parent.printed = []
    parent.print = lambda text, **kwargs: parent.printed.append(text)
    controller.output_enabled_by_channel = {0: False, 1: False}
    return module, parent, controller, tmp_path / f"{parent.name}_last_setpoints.json"


def _connect(parent, controller, vset, ilim, resume=False):
    """A new connection, then one housekeeping read of the PSU setpoints."""
    parent._pending_setpoints, parent._setpoint_prefill_decided = {}, resume
    controller.voltage_setpoint_values = dict(vset)
    controller.current_limit_values = dict(ilim)
    parent._setpoints_observed(controller, busy=False)


def _memory(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_psu_setpoints_are_remembered_then_proposed_after_a_power_cycle(psu):
    module, parent, controller, path = psu
    _connect(parent, controller, {0: 300.0, 1: 120.0}, {0: 0.001, 1: 0.002})
    assert parent._pending_setpoints == {} and parent.printed == []
    assert _memory(path)["com"] == "5"
    assert {ch: (r["vset"], r["ilim"]) for ch, r in _memory(path)["channels"].items()} == {
        "0": (300.0, 0.001), "1": (120.0, 0.002)}
    # PSU powered off meanwhile: it comes back at 0 V and its default limit.
    _connect(parent, controller, {0: 0.0, 1: 0.0}, {0: 0.0005, 1: 0.002})
    assert parent._pending_setpoints == {0: {"voltage": 300.0, "current_limit": 0.001}, 1: {"voltage": 120.0}}
    assert "CH0 Vset 300 V, Ilim 0.001 A" in parent.printed[0] and "validate a field" in parent.printed[0]
    assert _memory(path)["channels"]["0"]["vset"] == 300.0  # Proposed values are not overwritten by the 0 V.
    # The operator validates CH0's Vset only: its Ilim stays proposed.
    parent._drop_pending_setpoints({0: ["voltage"]})
    assert parent._pending_setpoints == {0: {"current_limit": 0.001}, 1: {"voltage": 120.0}}
    controller.voltage_setpoint_values[0] = 250.0
    parent._setpoints_observed(controller, busy=False)
    assert _memory(path)["channels"]["0"] == pytest.approx(dict(vset=250.0, ilim=0.001, time=_memory(path)["channels"]["0"]["time"]))


def test_validating_in_the_panel_drops_only_the_applied_fields(psu):
    module, parent, controller, path = psu
    parent._pending_setpoints = {0: {"voltage": 300.0, "current_limit": 0.001}, 1: {"voltage": 120.0}}
    parent.getChannels = lambda: []
    parent.controller = types.SimpleNamespace(applyManualStateFromThread=lambda state, parallel=True: None)
    parent._schedule_delayed_refresh = lambda delay: None
    parent._submit_manual_panel_state({"setpoints_only": True, "current_limit_values": {0: 0.001}})
    assert parent._pending_setpoints == {0: {"voltage": 300.0}, 1: {"voltage": 120.0}}
    parent._submit_manual_panel_state({"output_enabled": {0: True, 1: False}, "voltage_values": {0: 300.0, 1: 120.0},
                                       "current_limit_values": {0: 0.001, 1: 0.002}})
    assert parent._pending_setpoints == {}  # Turning an output ON applies the whole panel.


@pytest.mark.parametrize("case", ["output on", "stored config", "other port", "crash resume", "busy"])
def test_nothing_is_proposed_when_the_psu_or_a_config_decides(psu, case):
    module, parent, controller, path = psu
    path.write_text(json.dumps({"com": "5", "channels": {"0": {"vset": 300.0, "ilim": 0.001}}}), encoding="utf-8")
    if case == "output on":
        controller.output_enabled_by_channel = {0: True, 1: False}
    elif case == "stored config":
        parent.operating_config = 3
    elif case == "other port":
        parent.com = 6
    if case == "busy":
        parent._pending_setpoints, parent._setpoint_prefill_decided = {}, False
        controller.voltage_setpoint_values, controller.current_limit_values = {0: 0.0}, {0: 0.0005}
        parent._setpoints_observed(controller, busy=True)
        assert parent._setpoint_prefill_decided is False and _memory(path)["channels"]["0"]["vset"] == 300.0
        return
    _connect(parent, controller, {0: 0.0, 1: 0.0}, {0: 0.0005, 1: 0.0005}, resume=case == "crash resume")
    assert parent._pending_setpoints == {}
    if case == "other port":
        assert _memory(path)["com"] == "6"  # This PSU's own values from now on.
    else:
        assert _memory(path)["channels"]["0"]["vset"] == 0.0  # The PSU's actual setpoints are the last values.


def test_out_of_range_memories_are_not_proposed(psu):
    module, parent, controller, path = psu
    parent.channels[0].min, parent.channels[0].max = 0.0, 200.0
    parent.channels[0].hardware_current_limit = 0.0005
    path.write_text(json.dumps({"com": "5", "channels": {"0": {"vset": 300.0, "ilim": 0.001}}}), encoding="utf-8")
    _connect(parent, controller, {0: 0.0, 1: 0.0}, {0: 0.0002, 1: 0.0002})
    assert parent._pending_setpoints == {}


def test_without_explorer_settings_nothing_is_written(psu):
    module, parent, controller, path = psu
    del parent.pluginManager
    _connect(parent, controller, {0: 300.0, 1: 120.0}, {0: 0.001, 1: 0.002})
    assert not path.exists() and np.isnan(np.nan)
