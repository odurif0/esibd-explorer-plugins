"""Exercise each real runtime against an exclusive, delayed native port.

Only base DLL calls are simulated. The high-level connect/disconnect methods,
timeout workers, port registry and lock ownership are the production code.
"""
from __future__ import annotations

import gc
import importlib
import importlib.util
import logging
import sys
import threading
import weakref
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPECS = [
    ("ampr_a", "ampr", "AMPR"), ("ampr_b", "ampr", "AMPR"),
    ("amx_a", "amx", "AMX"), ("amx_b", "amx", "AMX"),
    ("amx_hd", "amx_hd", "AMXHD"),
    *((f"psu_{letter}", "psu", "PSU") for letter in "abcde"),
    ("esi", "esi", "ESI"),
]


class NativePort:
    def __init__(self):
        self.operations = []
        self.workers = set()
        self.open_entered = threading.Event()
        self.open_release = threading.Event()
        self.open_release.set()
        self.close_entered = threading.Event()
        self.close_release = threading.Event()
        self.close_release.set()
        self.baud_entered = threading.Event()
        self.baud_release = threading.Event()
        self.baud_release.set()
        self.open_status = 0
        self.close_status = 0
        self.allocate_handle = True
        self.handle_owner = None
        self.active = 0
        self.guard = threading.Lock()

    def invoke(self, name, device, event=None, release=None, result=0):
        with self.guard:
            assert self.active == 0, "native calls must never overlap"
            self.active += 1
            self.workers.add(threading.current_thread())
            self.operations.append((name, id(device)))
        try:
            if event is not None:
                event.set()
            if release is not None:
                assert release.wait(5), f"test did not release {name}"
            return result
        finally:
            with self.guard:
                self.active -= 1

    def open(self, device, *args):
        assert self.handle_owner is None, "Open must not reuse an owned native channel"
        if self.allocate_handle:
            self.handle_owner = id(device)
        return self.invoke("open", device, self.open_entered,
                           self.open_release, self.open_status)

    def close(self, device, *args):
        status = self.invoke("close", device, self.close_entered,
                             self.close_release, self.close_status)
        if status == 0:
            self.handle_owner = None
        return status

    def baud(self, device, baud):
        return self.invoke("baud", device, self.baud_entered,
                           self.baud_release, (0, baud))

    def count(self, name, device=None):
        return sum(op == name and (device is None or owner == id(device))
                   for op, owner in self.operations)

    def finish_open(self):
        self.open_release.set()
        self.join_workers()

    def join_workers(self):
        for worker in tuple(self.workers):
            if worker is not threading.current_thread():
                worker.join(2)
                assert not worker.is_alive()


@pytest.fixture(params=SPECS, ids=[spec[0] for spec in SPECS])
def rig(request, monkeypatch):
    slug, family, name = request.param
    namespace = f"_connection_recovery_{slug}"
    runtime = ROOT / slug / "vendor" / "runtime"
    spec = importlib.util.spec_from_file_location(
        namespace, runtime / "__init__.py", submodule_search_locations=[str(runtime)]
    )
    package = importlib.util.module_from_spec(spec)
    sys.modules[namespace] = package
    spec.loader.exec_module(package)
    module = importlib.import_module(f"{namespace}.{family}.{family}")
    cls = getattr(module, f"_{name}Controller")
    base = getattr(module, f"{name}Base")
    native = NativePort()
    logger = logging.getLogger(f"test.initial_connection.{slug}")
    monkeypatch.setattr(base, "__init__", lambda self, *a, **kw: setattr(self, "err_dict", {}))
    # Use descriptors, not a bound NativePort method: production passes either
    # a bound super() method or an unbound base method with the device argument.
    monkeypatch.setattr(base, "open_port", lambda self, *a: native.open(self, *a))
    monkeypatch.setattr(base, "close_port", lambda self, *a: native.close(self, *a))
    for method in ("set_baud_rate", "set_comspeed"):
        if hasattr(base, method):
            monkeypatch.setattr(base, method, lambda self, baud: native.baud(self, baud))
    for method in ("get_device_type", "get_dev_type"):
        if hasattr(base, method):
            monkeypatch.setattr(base, method, lambda self: (0, self.DEVICE_TYPE))
    if hasattr(cls, "_warn_if_unexpected_product_id"):
        monkeypatch.setattr(cls, "_warn_if_unexpected_product_id", lambda *a, **kw: None)
    if family == "esi":
        monkeypatch.setattr(cls, "_prepare_safe_inventory", lambda self, timeout: None)
        monkeypatch.setattr(cls, "force_safe_off", lambda self, **kw: True)
        monkeypatch.setattr(cls, "discover_modules", lambda self, **kw: {})
        monkeypatch.setattr(cls, "set_global_active", lambda self, *a, **kw: None)
        monkeypatch.setattr(cls, "_verify_hv_discharge", lambda self, *a, **kw: None)
    instances = weakref.WeakSet()

    def new():
        device = cls(f"retry_{slug}_{len(instances)}", com=15, logger=logger)
        device._DEFAULT_IO_TIMEOUT_S = 0.05
        instances.add(device)
        return device

    def disconnect(device):
        if family == "ampr":
            return device.disconnect()
        return device.disconnect(timeout_s=0.05)

    yield SimpleNamespace(new=new, disconnect=disconnect, hw=native, cls=cls,
                          base=base, family=family)
    native.open_release.set()
    native.close_release.set()
    native.baud_release.set()
    native.join_workers()
    # Each case imports its own runtime. Retire only its simulated registry;
    # no native library, real port or other test's owner is touched.
    if hasattr(cls, "_active_connections"):
        cls._active_connections.clear()
    if hasattr(cls, "_connected_instance"):
        cls._connected_instance = None
    for key in list(sys.modules):
        if key == namespace or key.startswith(namespace + "."):
            sys.modules.pop(key, None)


def fail_open(rig):
    device = rig.new()
    rig.hw.open_release.clear()
    with pytest.raises(RuntimeError, match="timed out"):
        device.connect(timeout_s=0.05)
    assert rig.hw.open_entered.is_set()
    return device


@pytest.mark.parametrize("late_status", [0, -2])
def test_explicit_new_connection_closes_only_a_finished_initial_open(rig, late_status):
    rig.hw.open_status = late_status
    old = fail_open(rig)
    assert rig.disconnect(old) is False
    before = list(rig.hw.operations)
    refused = rig.new()
    with pytest.raises(RuntimeError):
        refused.connect(timeout_s=0.05)
    assert rig.hw.operations == before
    assert rig.hw.count("close") == 0

    rig.hw.finish_open()
    rig.hw.open_status = 0
    fresh = rig.new()
    assert fresh.connect(timeout_s=0.2) is True
    assert rig.hw.count("close", old) == 1
    assert [op for op, owner in rig.hw.operations if owner == id(old)] == ["open", "close"]
    assert old._transport_poisoned is True
    assert old.thread_lock.locked()
    with pytest.raises(RuntimeError):
        old.connect(timeout_s=0.05)
    assert rig.hw.count("open", old) == 1
    assert rig.disconnect(fresh) is True


def test_abandoned_open_keeps_a_strong_owner_until_confirmed_close(rig):
    old = fail_open(rig)
    rig.hw.finish_open()
    reference = weakref.ref(old)
    del old
    gc.collect()
    assert reference() is not None, "the timed-out port owner must outlive the UI reference"
    fresh = rig.new()
    assert fresh.connect(timeout_s=0.2) is True
    assert rig.hw.count("close") == 1
    assert rig.disconnect(fresh) is True


@pytest.mark.parametrize("late_close_status", [0, -3])
def test_late_close_is_observed_not_replayed(rig, late_close_status):
    old = fail_open(rig)
    rig.hw.finish_open()
    rig.hw.close_release.clear()
    rig.hw.close_status = late_close_status
    refused = rig.new()
    with pytest.raises(RuntimeError):
        refused.connect(timeout_s=0.05)
    assert rig.hw.close_entered.is_set()
    assert rig.hw.count("close", old) == 1
    assert rig.hw.count("open") == 1
    rig.hw.close_release.set()
    rig.hw.join_workers()
    fresh = rig.new()
    if late_close_status == 0:
        assert fresh.connect(timeout_s=0.2) is True
        assert rig.hw.count("close", old) == 1
        assert rig.disconnect(fresh) is True
    else:
        with pytest.raises(RuntimeError):
            fresh.connect(timeout_s=0.05)
        assert rig.hw.count("close", old) == 1
        assert rig.hw.count("open") == 1
        assert old._dll_port_claimed is True


def test_close_minus_three_does_not_prove_failed_open_released(rig):
    rig.hw.allocate_handle = False
    rig.hw.open_status = -2
    rig.hw.close_status = -3
    old = rig.new()
    with pytest.raises(RuntimeError):
        old.connect(timeout_s=0.05)
    assert rig.disconnect(old) is False
    fresh = rig.new()
    with pytest.raises(RuntimeError):
        fresh.connect(timeout_s=0.05)
    assert rig.hw.count("open") == 1
    assert old._dll_port_claimed is True


def test_other_instance_cannot_open_or_close_a_connected_owner(rig):
    owner = rig.new()
    assert owner.connect(timeout_s=0.2) is True
    before = list(rig.hw.operations)
    other = rig.new()
    with pytest.raises(RuntimeError):
        other.connect(timeout_s=0.05)
    assert rig.disconnect(other) is True
    assert rig.hw.operations == before
    assert rig.disconnect(owner) is True


@pytest.mark.parametrize("step", ["get_voltage", "set_enable"])
def test_timeout_after_open_never_qualifies_for_initial_open_recovery(rig, step):
    owner = rig.new()
    assert owner.connect(timeout_s=0.2) is True
    entered, release = threading.Event(), threading.Event()

    def command():
        return rig.hw.invoke(step, owner, entered, release)

    try:
        with pytest.raises(RuntimeError, match="timed out"):
            owner._call_locked_with_timeout(command, 0.05, step)
        assert entered.is_set()
        release.set()
        rig.hw.join_workers()
        try:
            assert rig.disconnect(owner) is False
        except RuntimeError:
            assert owner._transport_poisoned
        other = rig.new()
        with pytest.raises(RuntimeError):
            other.connect(timeout_s=0.05)
        assert rig.hw.count("close") == 0
        assert rig.hw.count("open") == 1
        assert owner._dll_port_claimed is True
    finally:
        release.set()


def test_whole_connect_phase_is_protected_from_concurrent_disconnect(rig):
    rig.hw.baud_release.clear()
    device = rig.new()
    outcome = []

    def connect():
        try:
            outcome.append(device.connect(timeout_s=2))
        except BaseException as exc:
            outcome.append(exc)

    caller = threading.Thread(target=connect)
    caller.start()
    try:
        assert rig.hw.baud_entered.wait(1)
        before = list(rig.hw.operations)
        assert rig.disconnect(device) is False
        with pytest.raises(RuntimeError):
            device.connect(timeout_s=0.05)
        other = rig.new()
        with pytest.raises(RuntimeError):
            other.connect(timeout_s=0.05)
        assert rig.hw.operations == before
    finally:
        rig.hw.baud_release.set()
        caller.join(3)
    assert not caller.is_alive()
    assert outcome == [True]
    assert rig.disconnect(device) is True


def test_refused_concurrent_connect_cannot_reset_a_successful_connection(rig, monkeypatch):
    device = rig.new()
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
        before = list(rig.hw.operations)
        release.set()
        loser.join(2)
        assert not loser.is_alive() and len(errors) == 1
        assert device.connected and device._dll_port_claimed
        assert rig.hw.operations == before, 'a refused reservation must not close the winner'
        assert rig.disconnect(device) is True
    finally:
        release.set()
        loser.join(2)


@pytest.mark.parametrize("rig", [SPECS[-1]], indirect=True, ids=["esi"])
@pytest.mark.parametrize("stage", ["prepare", "inventory"])
@pytest.mark.parametrize("discharge_ok", [False, True])
def test_esi_validated_connect_failure_never_bypasses_shutdown(rig, monkeypatch, stage, discharge_ok):
    device = rig.new()
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
    assert rig.hw.count("close") == int(discharge_ok)
    assert device.connected is not discharge_ok
    assert device._dll_port_claimed is not discharge_ok
    assert not getattr(device, "_open_failed", False)
    assert not getattr(device, "_opening_in_progress", False)


@pytest.mark.parametrize("rig", [SPECS[-1]], indirect=True, ids=["esi"])
def test_esi_identity_failure_may_close_port_but_never_send_output_commands(rig, monkeypatch):
    device = rig.new()
    calls = []
    monkeypatch.setattr(rig.base, "get_dev_type", lambda self: (0, 123))
    monkeypatch.setattr(device, "force_safe_off", lambda **kw: calls.append("off"))
    with pytest.raises(RuntimeError, match="device type mismatch"):
        device.connect(timeout_s=.2)
    assert not calls
    assert rig.hw.count("close") == 1
    assert not device.connected and not device._dll_port_claimed


def test_connect_checks_readiness_under_the_reservation_lock(rig, monkeypatch):
    device = rig.new()
    assert device.connect(timeout_s=.2) is True
    before = list(rig.hw.operations)
    original_getattribute = rig.cls.__getattribute__
    reads = []

    def checked_getattribute(self, name):
        if self is device and name in ("connected", "_opening_in_progress"):
            assert rig.cls._active_connections_lock.locked(), (
                "connect must check opening and ready state atomically", name)
            reads.append(name)
        return original_getattribute(self, name)

    with monkeypatch.context() as patch:
        patch.setattr(rig.cls, "__getattribute__", checked_getattribute)
        assert device.connect(timeout_s=.2) is True
    assert reads == ["_opening_in_progress", "connected"]
    assert rig.hw.operations == before
    assert rig.disconnect(device) is True


def test_reentrant_connect_keeps_the_outer_setup_guard(rig, monkeypatch):
    device = rig.new()
    checked = []

    def verify(*args, **kwargs):
        before = list(rig.hw.operations)
        with pytest.raises(RuntimeError, match="in progress"):
            device.connect(timeout_s=.2)
        assert device._opening_in_progress
        assert device._opening_caller is threading.current_thread()
        assert rig.disconnect(device) is False
        assert rig.hw.operations == before
        checked.append(True)
        return device.DEVICE_TYPE if rig.family == "ampr" else None

    hook = ("_verify_device_type" if rig.family == "ampr" else
            "_prepare_safe_inventory" if rig.family == "esi" else
            "_warn_if_unexpected_product_id")
    monkeypatch.setattr(device, hook, verify)
    assert device.connect(timeout_s=.2) is True
    assert checked == [True]
    assert device.connected and device._dll_port_claimed
    assert not device._opening_in_progress
    assert rig.hw.count("open") == 1
    assert rig.hw.count("close") == 0
    assert rig.disconnect(device) is True


def test_ready_shortcut_cannot_reclaim_a_port_closed_since_the_check(rig, monkeypatch):
    device = rig.new()
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
        assert not rig.cls._active_connections_lock.locked()
        assert rig.disconnect(device) is True
        replacement = rig.new()
        assert replacement.connect(timeout_s=.2) is True
        replacements.append(replacement)
        return ready

    monkeypatch.setattr(device, "_connection_is_ready", checked_then_replaced)
    assert device.connect(timeout_s=.2) is True
    assert len(replacements) == 1
    replacement = replacements[0]
    assert not device.connected and not device._dll_port_claimed
    assert replacement.connected and replacement._dll_port_claimed
    assert rig.hw.handle_owner == id(replacement)
    assert set(rig.cls._active_connections) == {id(replacement)}
    assert rig.hw.count("open") == 2
    assert rig.hw.count("close") == 1
    before = list(rig.hw.operations)
    assert rig.disconnect(device) is True
    assert rig.hw.operations == before, "retired instance must not close the new owner"
    assert rig.disconnect(replacement) is True


def test_baud_timeout_is_not_recoverable_as_initial_open(rig):
    rig.hw.baud_release.clear()
    old = rig.new()
    with pytest.raises(RuntimeError, match="timed out"):
        old.connect(timeout_s=0.05)
    assert rig.hw.baud_entered.is_set()
    rig.hw.baud_release.set()
    rig.hw.join_workers()
    other = rig.new()
    with pytest.raises(RuntimeError):
        other.connect(timeout_s=0.05)
    assert rig.hw.count("open") == 1
    assert rig.hw.count("close") == 0
    assert old._dll_port_claimed is True
