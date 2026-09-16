"""AMPR edits must survive slow I/O, coalesce, and never outlive OFF.

Real Qt/Explorer input-event coverage lives in test_ampr_setpoint_ui.py.
"""
from __future__ import annotations

import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from test_ampr_plugin_channel_sync import _clear_test_modules, _install_esibd_stubs

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=['ampr_a', 'ampr_b'])
def rig(request):
    _clear_test_modules()
    _install_esibd_stubs()
    spec = importlib.util.spec_from_file_location('ampr_setpoint_test', ROOT / request.param / 'ampr_plugin.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Worker feedback is tested against a real GUI thread in the UI tests.
    module._invoke_gui_callback = lambda fn: fn()
    # The lightweight host stubs otherwise omit the inherited dispatch chain.
    # UI tests execute the original Explorer methods rather than this shim.
    def host_apply(channel, apply=False):
        if channel.real and (channel.value != channel.lastAppliedValue or apply):
            channel.lastAppliedValue = channel.value
            channel.channelParent.controller.applyValueFromThread(channel)
    def host_dispatch(controller, channel):
        worker = threading.Thread(target=controller.applyValue, args=(channel,))
        controller._setpoint_thread = worker
        worker.start()
    module.Channel.applyValue = host_apply
    module.DeviceController.applyValueFromThread = host_dispatch
    logs = []
    channels = []
    parent = SimpleNamespace(name=request.param, isOn=lambda: True, getChannels=lambda: channels,
                             getConfiguredModules=lambda: [0], interval=100,
                             module_voltage_limit=lambda module: 1000.0)
    controller = module.AMPRController(parent)
    parent.controller = controller
    controller.lock = threading.Lock()
    controller.errorCount = 0
    controller.initialized = True
    controller.main_state = 'ST_ON'
    controller.print = lambda message, flag=None: logs.append(message)
    controller._sync_status_to_gui = lambda: None
    controller._update_state = lambda **kwargs: None
    controller.acquiring = True

    class Device:
        NO_ERR = 0
        def __init__(self):
            self.calls = []
            self.targets = {}
            self.status = 0
        def set_module_voltage(self, module, number, voltage):
            self.calls.append((module, number, voltage))
            if self.status == 0:
                self.targets[number] = voltage
            return self.status
        def get_module_voltages(self, module):
            return {number: {'setpoint': target, 'measured': target}
                    for number, target in self.targets.items()}
        def format_status(self, status): return f'error {status}'

    controller.device = device = Device()
    for number in (1, 2):
        channel = module.AMPRChannel.__new__(module.AMPRChannel)
        channel.value = 0.0
        channel.real = channel.enabled = True
        channel.module, channel.id = '0', str(number)
        channel.name = f'CH{number}'
        channel.channelParent = parent
        channel.controller = None
        channel.lastAppliedValue = np.nan
        channel.getParameterByName = lambda name: None
        channels.append(channel)
    yield SimpleNamespace(module=module, controller=controller, device=device,
                          channels=channels, logs=logs, parent=parent)
    cancel = getattr(controller, '_cancel_setpoints', None)
    if cancel:
        cancel()
    if controller.lock.locked():
        controller.lock.release()
    worker = getattr(controller, '_setpoint_thread', None)
    if worker is not None:
        worker.join(timeout=3)
        assert not worker.is_alive()


def wait_idle(controller):
    thread = getattr(controller, '_setpoint_thread', None)
    if thread is not None:
        thread.join(timeout=3)
        assert not thread.is_alive(), 'setpoint worker did not finish'


def submit(rig, index, value):
    channel = rig.channels[index]
    channel.value = value
    channel.applyValue()


def test_busy_poll_does_not_lose_the_last_edit_or_block_the_caller(rig):
    rig.controller.lock.acquire()
    start = time.monotonic()
    submit(rig, 0, 10.0)
    submit(rig, 0, 20.0)
    submit(rig, 1, -35.0)
    assert time.monotonic() - start < .25
    # Longer than the old one-second apply timeout.
    time.sleep(1.15)
    assert rig.device.calls == []
    assert np.isnan(rig.channels[0].lastAppliedValue)
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert sorted(rig.device.calls) == [(0, 1, 20.0), (0, 2, -35.0)]
    assert np.isnan(rig.channels[0].lastAppliedValue), 'an ACK is not a readback'
    rig.controller.readNumbers()
    assert rig.channels[0].lastAppliedValue == 20.0
    assert rig.channels[1].lastAppliedValue == -35.0


def test_edits_during_an_inflight_write_are_not_reordered(rig):
    entered, release = threading.Event(), threading.Event()
    original = rig.device.set_module_voltage
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original(*args)
    rig.device.set_module_voltage = blocked
    submit(rig, 0, 10.0)
    assert entered.wait(2)
    try:
        submit(rig, 0, 20.0)
        submit(rig, 0, 30.0)
        submit(rig, 1, -40.0)
    finally:
        release.set()
    wait_idle(rig.controller)
    assert rig.device.calls == [(0, 1, 10.0), (0, 1, 30.0), (0, 2, -40.0)]
    rig.controller.readNumbers()
    assert rig.channels[0].lastAppliedValue == 30.0


def test_off_cancels_waiting_commands_even_after_a_new_on(rig):
    rig.controller.lock.acquire()
    submit(rig, 0, 50.0)
    assert rig.controller._begin_transition(False)
    rig.controller._end_transition()
    assert rig.controller._begin_transition(True)
    rig.controller._end_transition()
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert rig.device.calls == []
    # Only a freshly requested value belongs to the new ON session.
    submit(rig, 0, 55.0)
    wait_idle(rig.controller)
    assert rig.device.calls == [(0, 1, 55.0)]


def test_edits_wait_for_startup_without_being_discarded(rig):
    assert rig.controller._begin_transition(True)
    submit(rig, 0, 60.0)
    submit(rig, 0, 65.0)
    assert rig.device.calls == []
    rig.controller._end_transition()
    wait_idle(rig.controller)
    assert rig.device.calls == [(0, 1, 65.0)]


def test_failure_is_visible_and_can_be_explicitly_retried(rig):
    rig.device.status = -12
    submit(rig, 0, 10.0)
    wait_idle(rig.controller)
    assert len(rig.device.calls) == 1
    assert np.isnan(rig.channels[0].lastAppliedValue)
    assert rig.channels[0]._ampr_setpoint_state == 'error'
    assert any('rejected' in message.lower() for message in rig.logs)
    # Global equation refresh must not flood the same failing command.
    for _ in range(10):
        rig.channels[0].applyValue()
    wait_idle(rig.controller)
    assert len(rig.device.calls) == 1
    rig.device.status = 0
    rig.channels[0].applyValue(apply=True)
    wait_idle(rig.controller)
    rig.controller.readNumbers()
    assert rig.channels[0].lastAppliedValue == 10.0
    assert rig.channels[0]._ampr_setpoint_state == 'confirmed'


def test_ack_does_not_hide_a_different_or_missing_setpoint_readback(rig):
    submit(rig, 0, 10.0)
    wait_idle(rig.controller)
    rig.device.targets[1] = 9.0
    rig.controller.readNumbers()
    assert rig.channels[0]._ampr_setpoint_state == 'mismatch'
    assert np.isnan(rig.channels[0].lastAppliedValue)
    rig.device.targets[1] = None
    rig.controller.readNumbers()
    assert rig.channels[0]._ampr_setpoint_state == 'sent'
    assert np.isnan(rig.channels[0].lastAppliedValue)
    # Only the displayed precision matters, not floating-point equality.
    rig.device.targets[1] = 10.000000001
    rig.controller.readNumbers()
    assert rig.channels[0]._ampr_setpoint_state == 'confirmed'
    assert rig.channels[0].lastAppliedValue == 10.0


@pytest.mark.parametrize('value', [float('nan'), float('inf'), 1001.0, -1001.0])
def test_invalid_targets_do_not_reach_the_device(rig, value):
    submit(rig, 0, value)
    wait_idle(rig.controller)
    assert rig.device.calls == []
    assert rig.logs


def test_channel_disable_replaces_a_waiting_positive_target_with_zero(rig):
    rig.controller.lock.acquire()
    submit(rig, 0, 45.0)
    rig.channels[0].enabled = False
    rig.channels[0].applyValue(apply=True)
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert rig.device.calls == [(0, 1, 0.0)]


def test_failed_worker_start_is_reported_and_does_not_leave_a_lock(rig, monkeypatch):
    class BadThread:
        def __init__(self, **kwargs): pass
        def start(self): raise RuntimeError('cannot start thread')
    monkeypatch.setattr(rig.module, 'Thread', BadThread, raising=False)
    submit(rig, 0, 5.0)
    assert rig.device.calls == []
    assert rig.channels[0]._ampr_setpoint_state == 'error'
    assert rig.controller.lock.acquire(timeout=.1)
    rig.controller.lock.release()


def test_many_edits_keep_one_worker_and_one_pending_value_per_channel(rig, monkeypatch):
    workers = []
    def create_worker(**kwargs):
        worker = threading.Thread(**kwargs)
        workers.append(worker)
        return worker
    monkeypatch.setattr(rig.module, 'Thread', create_worker)
    rig.controller.lock.acquire()
    for value in range(200):
        submit(rig, value % 2, float(value))
    assert len(workers) == 1
    assert len(rig.controller._pending_setpoints) == 2
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert sorted(rig.device.calls) == [(0, 1, 198.0), (0, 2, 199.0)]


def test_invalid_edit_supersedes_an_older_unsent_value(rig):
    rig.controller.lock.acquire()
    submit(rig, 0, 45.0)
    submit(rig, 0, 1001.0)
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert rig.device.calls == []
    assert rig.channels[0]._ampr_setpoint_state == 'error'


def test_native_timeout_is_not_retried_as_lock_contention(rig):
    calls = []
    def timeout(*args):
        calls.append(args)
        raise TimeoutError('vendor write timed out')
    rig.device.set_module_voltage = timeout
    submit(rig, 0, 45.0)
    wait_idle(rig.controller)
    assert calls == [(0, 1, 45.0)]
    assert rig.channels[0]._ampr_setpoint_state == 'error'
    assert np.isnan(rig.channels[0].lastAppliedValue)


def test_old_request_never_reaches_a_replacement_device(rig):
    rig.controller.lock.acquire()
    submit(rig, 0, 45.0)
    replacement = type(rig.device)()
    rig.controller.device = replacement
    rig.controller.lock.release()
    wait_idle(rig.controller)
    assert rig.device.calls == replacement.calls == []
    assert np.isnan(rig.channels[0].lastAppliedValue)


def test_an_old_readback_cannot_confirm_a_new_queued_value(rig):
    submit(rig, 0, 10.0)
    wait_idle(rig.controller)
    rig.controller.readNumbers()
    assert rig.channels[0].lastAppliedValue == 10.0
    rig.controller.lock.acquire()
    submit(rig, 0, 20.0)
    rig.controller._confirm_setpoints(0, {1: {'setpoint': 10.0}}, rig.device)
    assert rig.channels[0]._ampr_setpoint_state == 'pending'
    assert np.isnan(rig.channels[0].lastAppliedValue)
    rig.controller.lock.release()
    wait_idle(rig.controller)
    rig.controller.readNumbers()
    assert rig.channels[0].lastAppliedValue == 20.0


def test_fast_worker_cannot_have_its_ack_overwritten_by_initial_feedback(rig):
    entered, release = threading.Event(), threading.Event()
    original_write = rig.device.set_module_voltage
    def blocked(*args):
        entered.set()
        assert release.wait(3)
        return original_write(*args)
    rig.device.set_module_voltage = blocked
    submit(rig, 0, 10.0)
    assert entered.wait(2)
    worker = rig.controller._setpoint_thread
    original_publish = rig.controller._publish_setpoint
    def delayed_feedback(request, state, detail):
        if request.value == 30.0 and state == 'pending':
            release.set()
            worker.join(timeout=2)
            assert not worker.is_alive()
        original_publish(request, state, detail)
    rig.controller._publish_setpoint = delayed_feedback
    try:
        submit(rig, 0, 30.0)
    finally:
        release.set()
    wait_idle(rig.controller)
    rig.controller.readNumbers()
    assert rig.channels[0]._ampr_setpoint_state == 'confirmed'
    assert rig.channels[0].lastAppliedValue == 30.0


def test_unchanged_readbacks_do_not_repaint_the_editor_each_poll(rig):
    submit(rig, 0, 10.0)
    wait_idle(rig.controller)
    rig.controller.readNumbers()
    callbacks = []
    rig.module._invoke_gui_callback = callbacks.append
    for _ in range(10):
        rig.controller.readNumbers()
    assert callbacks == []


def test_unexpected_worker_failure_does_not_strand_the_queue(rig):
    def broken_lock(*args, **kwargs):
        raise RuntimeError('lock adapter failed')
    rig.controller._controller_lock_section = broken_lock
    submit(rig, 0, 10.0)
    wait_idle(rig.controller)
    assert rig.device.calls == []
    assert rig.channels[0]._ampr_setpoint_state == 'error'
    assert not rig.controller._pending_setpoints
