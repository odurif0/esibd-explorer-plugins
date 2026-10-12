"""Exercise each real runtime against an exclusive, delayed native port.

Only base DLL calls are simulated. The high-level connect/disconnect methods,
timeout workers, port registry and lock ownership are the production code.
"""
from __future__ import annotations

import gc
import threading
import weakref

import pytest
from support.cgc import SPECS


def fail_open(rig):
    device = rig.new()
    rig.hw.open_release.clear()
    with pytest.raises(RuntimeError, match="timed out"):
        device.connect(timeout_s=0.05)
    assert rig.hw.open_entered.is_set()
    return device


@pytest.mark.parametrize("late_status", [0, -2])
def test_explicit_new_connection_closes_only_a_finished_initial_open(cgc_rig, late_status):
    cgc_rig.hw.open_status = late_status
    old = fail_open(cgc_rig)
    assert cgc_rig.disconnect(old) is False
    before = list(cgc_rig.hw.operations)
    refused = cgc_rig.new()
    with pytest.raises(RuntimeError):
        refused.connect(timeout_s=0.05)
    assert cgc_rig.hw.operations == before
    assert cgc_rig.hw.count("close") == 0

    cgc_rig.hw.finish_open()
    cgc_rig.hw.open_status = 0
    fresh = cgc_rig.new()
    assert fresh.connect(timeout_s=0.2) is True
    assert cgc_rig.hw.count("close", old) == 1
    assert [op for op, owner in cgc_rig.hw.operations if owner == id(old)] == ["open", "close"]
    assert old._transport_poisoned is True
    assert old.thread_lock.locked()
    with pytest.raises(RuntimeError):
        old.connect(timeout_s=0.05)
    assert cgc_rig.hw.count("open", old) == 1
    assert cgc_rig.disconnect(fresh) is True


def test_abandoned_open_keeps_a_strong_owner_until_confirmed_close(cgc_rig):
    old = fail_open(cgc_rig)
    cgc_rig.hw.finish_open()
    reference = weakref.ref(old)
    del old
    gc.collect()
    assert reference() is not None, "the timed-out port owner must outlive the UI reference"
    fresh = cgc_rig.new()
    assert fresh.connect(timeout_s=0.2) is True
    assert cgc_rig.hw.count("close") == 1
    assert cgc_rig.disconnect(fresh) is True


@pytest.mark.parametrize("late_close_status", [0, -3])
def test_late_close_is_observed_not_replayed(cgc_rig, late_close_status):
    old = fail_open(cgc_rig)
    cgc_rig.hw.finish_open()
    cgc_rig.hw.close_release.clear()
    cgc_rig.hw.close_status = late_close_status
    refused = cgc_rig.new()
    with pytest.raises(RuntimeError):
        refused.connect(timeout_s=0.05)
    assert cgc_rig.hw.close_entered.is_set()
    assert cgc_rig.hw.count("close", old) == 1
    assert cgc_rig.hw.count("open") == 1
    cgc_rig.hw.close_release.set()
    cgc_rig.hw.join_workers()
    fresh = cgc_rig.new()
    if late_close_status == 0:
        assert fresh.connect(timeout_s=0.2) is True
        assert cgc_rig.hw.count("close", old) == 1
        assert cgc_rig.disconnect(fresh) is True
    else:
        with pytest.raises(RuntimeError):
            fresh.connect(timeout_s=0.05)
        assert cgc_rig.hw.count("close", old) == 1
        assert cgc_rig.hw.count("open") == 1
        assert old._dll_port_claimed is True


def test_close_minus_three_does_not_prove_failed_open_released(cgc_rig):
    cgc_rig.hw.allocate_handle = False
    cgc_rig.hw.open_status = -2
    cgc_rig.hw.close_status = -3
    old = cgc_rig.new()
    with pytest.raises(RuntimeError):
        old.connect(timeout_s=0.05)
    assert cgc_rig.disconnect(old) is False
    fresh = cgc_rig.new()
    with pytest.raises(RuntimeError):
        fresh.connect(timeout_s=0.05)
    assert cgc_rig.hw.count("open") == 1
    assert old._dll_port_claimed is True


def test_other_instance_cannot_open_or_close_a_connected_owner(cgc_rig):
    owner = cgc_rig.new()
    assert owner.connect(timeout_s=0.2) is True
    before = list(cgc_rig.hw.operations)
    other = cgc_rig.new()
    with pytest.raises(RuntimeError):
        other.connect(timeout_s=0.05)
    assert cgc_rig.disconnect(other) is True
    assert cgc_rig.hw.operations == before
    assert cgc_rig.disconnect(owner) is True


@pytest.mark.parametrize("step", ["get_voltage", "set_enable"])
def test_timeout_after_open_never_qualifies_for_initial_open_recovery(cgc_rig, step):
    owner = cgc_rig.new()
    assert owner.connect(timeout_s=0.2) is True
    entered, release = threading.Event(), threading.Event()

    def command():
        return cgc_rig.hw.invoke(step, owner, entered, release)

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            owner._call_locked_with_timeout(command, 0.05, step)
        assert entered.is_set()
        release.set()
        cgc_rig.hw.join_workers()
        try:
            assert cgc_rig.disconnect(owner) is False
        except RuntimeError:
            assert owner._transport_poisoned
        other = cgc_rig.new()
        with pytest.raises(RuntimeError):
            other.connect(timeout_s=0.05)
        assert cgc_rig.hw.count("close") == 0
        assert cgc_rig.hw.count("open") == 1
        assert owner._dll_port_claimed is True
    finally:
        release.set()


def test_whole_connect_phase_is_protected_from_concurrent_disconnect(cgc_rig):
    cgc_rig.hw.baud_release.clear()
    device = cgc_rig.new()
    outcome = []

    def connect():
        try:
            outcome.append(device.connect(timeout_s=2))
        except BaseException as exc:
            outcome.append(exc)

    caller = threading.Thread(target=connect)
    caller.start()
    try:
        assert cgc_rig.hw.baud_entered.wait(1)
        before = list(cgc_rig.hw.operations)
        assert cgc_rig.disconnect(device) is False
        with pytest.raises(RuntimeError):
            device.connect(timeout_s=0.05)
        other = cgc_rig.new()
        with pytest.raises(RuntimeError):
            other.connect(timeout_s=0.05)
        assert cgc_rig.hw.operations == before
    finally:
        cgc_rig.hw.baud_release.set()
        caller.join(3)
    assert not caller.is_alive()
    assert outcome == [True]
    assert cgc_rig.disconnect(device) is True


def test_refused_concurrent_connect_cannot_reset_a_successful_connection(cgc_rig, monkeypatch):
    device = cgc_rig.new()
    at_reservation, release = threading.Event(), threading.Event()
    original = device._call_initial_open
    errors = []

    def delayed(*args, **kwargs):
        if threading.current_thread() is loser:
            at_reservation.set()
            assert release.wait(3)
        return original(*args, **kwargs)

    def connect():
        try:
            device.connect(timeout_s=.2)
        except RuntimeError as exc:
            errors.append(exc)

    monkeypatch.setattr(device, '_call_initial_open', delayed)
    loser = threading.Thread(target=connect)
    loser.start()
    try:
        assert at_reservation.wait(1)
        assert device.connect(timeout_s=.2) is True
        before = list(cgc_rig.hw.operations)
        release.set()
        loser.join(2)
        assert not loser.is_alive() and len(errors) == 1
        assert device.connected and device._dll_port_claimed
        assert cgc_rig.hw.operations == before, 'a refused reservation must not close the winner'
        assert cgc_rig.disconnect(device) is True
    finally:
        release.set()
        loser.join(2)


@pytest.mark.parametrize("cgc_rig", [SPECS[-1]], indirect=True, ids=["esi"])
@pytest.mark.parametrize("stage", ["prepare", "inventory"])
@pytest.mark.parametrize("discharge_ok", [False, True])
def test_esi_validated_connect_failure_never_bypasses_shutdown(cgc_rig, monkeypatch, stage, discharge_ok):
    device = cgc_rig.new()
    calls = []

    def fail(*args, **kwargs):
        raise RuntimeError("simulated inventory failure")

    def safe_off(*args, **kwargs):
        calls.append("off")
        return True

    def discharge(*args, **kwargs):
        calls.append("discharge")
        if not discharge_ok:
            raise RuntimeError("HV discharge unconfirmed")

    monkeypatch.setattr(device, "_prepare_safe_inventory" if stage == "prepare" else "discover_modules", fail)
    monkeypatch.setattr(device, "force_safe_off", safe_off)
    monkeypatch.setattr(device, "_verify_hv_discharge", discharge)
    with pytest.raises(RuntimeError, match="simulated inventory failure"):
        device.connect(timeout_s=.2)
    assert calls[-2:] == ["off", "discharge"]
    assert cgc_rig.hw.count("close") == int(discharge_ok)
    assert device.connected is not discharge_ok
    assert device._dll_port_claimed is not discharge_ok
    assert not getattr(device, "_open_failed", False)
    assert not getattr(device, "_opening_in_progress", False)


@pytest.mark.parametrize("cgc_rig", [SPECS[-1]], indirect=True, ids=["esi"])
def test_esi_identity_failure_may_close_port_but_never_send_output_commands(cgc_rig, monkeypatch):
    device = cgc_rig.new()
    calls = []
    monkeypatch.setattr(cgc_rig.base, "get_dev_type", lambda self: (0, 123))
    monkeypatch.setattr(device, "force_safe_off", lambda **kw: calls.append("off"))
    with pytest.raises(RuntimeError, match="device type mismatch"):
        device.connect(timeout_s=.2)
    assert not calls
    assert cgc_rig.hw.count("close") == 1
    assert not device.connected and not device._dll_port_claimed


def test_connect_checks_readiness_under_the_reservation_lock(cgc_rig, monkeypatch):
    device = cgc_rig.new()
    assert device.connect(timeout_s=.2) is True
    before = list(cgc_rig.hw.operations)
    original_getattribute = cgc_rig.cls.__getattribute__
    reads = []

    def checked_getattribute(self, name):
        if self is device and name in ("connected", "_opening_in_progress"):
            assert cgc_rig.cls._active_connections_lock.locked(), (
                "connect must check opening and ready state atomically", name)
            reads.append(name)
        return original_getattribute(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(cgc_rig.cls, "__getattribute__", checked_getattribute)
        assert device.connect(timeout_s=.2) is True
    assert reads == ["_opening_in_progress", "connected"]
    assert cgc_rig.hw.operations == before
    assert cgc_rig.disconnect(device) is True


def test_reentrant_connect_keeps_the_outer_setup_guard(cgc_rig, monkeypatch):
    device = cgc_rig.new()
    checked = []

    def verify(*args, **kwargs):
        before = list(cgc_rig.hw.operations)
        with pytest.raises(RuntimeError, match="in progress"):
            device.connect(timeout_s=.2)
        assert device._opening_in_progress
        assert device._opening_caller is threading.current_thread()
        assert cgc_rig.disconnect(device) is False
        assert cgc_rig.hw.operations == before
        checked.append(True)
        return device.DEVICE_TYPE if cgc_rig.family == "ampr" else None

    hook = ("_verify_device_type" if cgc_rig.family == "ampr" else
            "_prepare_safe_inventory" if cgc_rig.family == "esi" else
            "_warn_if_unexpected_product_id")
    monkeypatch.setattr(device, hook, verify)
    assert device.connect(timeout_s=.2) is True
    assert checked == [True]
    assert device.connected and device._dll_port_claimed
    assert not device._opening_in_progress
    assert cgc_rig.hw.count("open") == 1
    assert cgc_rig.hw.count("close") == 0
    assert cgc_rig.disconnect(device) is True


def test_ready_shortcut_cannot_reclaim_a_port_closed_since_the_check(cgc_rig, monkeypatch):
    device = cgc_rig.new()
    assert device.connect(timeout_s=.2) is True
    original_ready = device._connection_is_ready
    replacements = []

    def checked_then_replaced():
        # Deterministic interleaving: after the readiness lock is released,
        # another caller closes the old connection and opens a new owner.
        # The original connect may linearize before that close, but must not
        # restore its old claim or interfere with the replacement's port.
        ready = original_ready()
        assert ready
        assert not cgc_rig.cls._active_connections_lock.locked()
        assert cgc_rig.disconnect(device) is True
        replacement = cgc_rig.new()
        assert replacement.connect(timeout_s=.2) is True
        replacements.append(replacement)
        return ready

    monkeypatch.setattr(device, "_connection_is_ready", checked_then_replaced)
    assert device.connect(timeout_s=.2) is True
    assert len(replacements) == 1
    replacement = replacements[0]
    assert not device.connected and not device._dll_port_claimed
    assert replacement.connected and replacement._dll_port_claimed
    assert cgc_rig.hw.handle_owner == id(replacement)
    assert set(cgc_rig.cls._active_connections) == {id(replacement)}
    assert cgc_rig.hw.count("open") == 2
    assert cgc_rig.hw.count("close") == 1
    before = list(cgc_rig.hw.operations)
    assert cgc_rig.disconnect(device) is True
    assert cgc_rig.hw.operations == before, "retired instance must not close the new owner"
    assert cgc_rig.disconnect(replacement) is True


def test_baud_timeout_is_not_recoverable_as_initial_open(cgc_rig):
    cgc_rig.hw.baud_release.clear()
    old = cgc_rig.new()
    with pytest.raises(RuntimeError, match="timed out"):
        old.connect(timeout_s=0.05)
    assert cgc_rig.hw.baud_entered.is_set()
    cgc_rig.hw.baud_release.set()
    cgc_rig.hw.join_workers()
    other = cgc_rig.new()
    with pytest.raises(RuntimeError):
        other.connect(timeout_s=0.05)
    assert cgc_rig.hw.count("open") == 1
    assert cgc_rig.hw.count("close") == 0
    assert old._dll_port_claimed is True
