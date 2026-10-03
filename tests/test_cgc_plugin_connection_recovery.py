"""Explorer controllers must retain and retire the real runtime's failed Open.

The eleven runtimes are covered through their family's controller; sibling
entrypoint equivalence is enforced separately by the family parity tests.
"""
from types import SimpleNamespace

import pytest

from test_cgc_initial_connection_recovery import rig  # noqa: F401
from test_shutdown_confirmation_regressions import FAMILIES, controller_for


@pytest.fixture
def plugin(rig, monkeypatch):
    return build_plugin(rig, monkeypatch)


def build_plugin(rig, monkeypatch):
    module, controller, messages = controller_for(rig.family)
    parent = controller.controllerParent
    parent.com, parent.baudrate = 15, 9600
    parent.connect_timeout_s = parent.poll_timeout_s = .05
    parent.heat_voltage_limit_v = parent.heat_current_limit_a = parent.heat_power_limit_w = 0
    parent.onAction = SimpleNamespace(state=True)
    parent.isOn = lambda: parent.onAction.state
    parent._set_on_ui_state = lambda value: setattr(parent.onAction, 'state', bool(value))
    controller.initialized = False
    controller.initializing = False

    def launch(self):
        if self.initializing:
            return
        self.initializing = True
        self.runInitialization()

    # Keep the real controller's explicit-request hook; only replace Explorer's
    # thread launcher. A worker call alone is not a fresh user ON request.
    monkeypatch.setattr(module.DeviceController, 'initializeCommunication', launch, raising=False)
    for name in ('_refresh_module_scan', '_refresh_available_configs',
                 '_refresh_loaded_config_status', '_update_state', '_sync_status_to_gui',
                 '_sync_status', '_apply_interlock_setting_unlocked', '_apply_snapshot'):
        if hasattr(controller, name):
            monkeypatch.setattr(controller, name, lambda *a, **kw: None)
    created, successes = [], []

    def factory(**kwargs):
        device = rig.new()
        device.close = lambda: rig.hw.invoke('dispose', device)
        if rig.family == 'esi':
            device.collect_identity = lambda **kw: {}
            device.collect_diagnostics = lambda **kw: {}
            device.configure_hv_max_voltage_steps = lambda *a, **kw: None
        created.append(device)
        return device

    # Diagnostics must not interrogate a failed opening, even one that returned
    # an error without poisoning the worker (and may own a partial handle).
    for name in ('get_state', 'get_device_state', 'get_voltage_state',
                 'get_interlock_state', 'get_controller_state'):
        if hasattr(rig.base, name):
            monkeypatch.setattr(rig.base, name, lambda self, *a, **kw:
                                rig.hw.invoke('diagnostic_read', self, result=(0, 0, 'ST_OFF')))
    monkeypatch.setattr(module, FAMILIES[rig.family][2], lambda: factory)
    controller.signalComm = SimpleNamespace(
        initCompleteSignal=SimpleNamespace(emit=lambda: successes.append(True)))
    return SimpleNamespace(module=module, controller=controller, parent=parent, created=created,
                           successes=successes, messages=messages)


@pytest.mark.parametrize('late_status', [0, -2])
def test_plugin_retries_only_after_old_open_is_finished_and_closed(plugin, rig, late_status):
    c, hw = plugin.controller, rig.hw
    hw.open_status = late_status
    hw.open_release.clear()
    c.initializeCommunication()
    assert len(plugin.created) == 1
    old = plugin.created[0]
    assert c.device is old, 'an unconfirmed handle owner must not be dropped'
    assert not c.initialized and not c.initializing
    assert not plugin.parent.onAction.state
    assert c.main_state == 'Connection pending'
    assert [name for name, _ in hw.operations] == ['open']
    assert not plugin.successes

    # An explicit retry while Open is blocked must not replace the owner.
    c.initializeCommunication()
    assert plugin.created == [old]
    assert hw.count('open') == 1 and hw.count('close') == 0

    # A late native result alone never enables outputs or starts a new Open.
    hw.finish_open()
    assert c.device is old and not c.initialized
    assert hw.count('open') == 1 and hw.count('close') == 0
    hw.open_status = 0
    c.initializeCommunication()  # the next explicit connection request
    assert len(plugin.created) == 2
    assert c.device is plugin.created[1]
    assert plugin.successes == [True]
    assert [name for name, owner in hw.operations if owner == id(old)] == [
        'open', 'close', 'dispose']
    assert old._transport_poisoned and old.thread_lock.locked()
    assert rig.disconnect(c.device) is True


def test_plugin_cannot_discard_ambiguous_close_minus_three(plugin, rig):
    c, hw = plugin.controller, rig.hw
    hw.open_status, hw.close_status = -2, -3
    hw.allocate_handle = False
    c.initializeCommunication()
    assert len(plugin.created) == 1
    old = plugin.created[0]
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == 'Connection pending' and not c.initialized
    assert not plugin.parent.onAction.state
    assert set(name for name, _ in hw.operations) == {'open', 'close'}
    c.initializeCommunication()
    assert plugin.created == [old]
    assert hw.count('open') == 1
    assert hw.count('dispose') == 0
    assert not plugin.successes


def test_disposal_does_not_discard_a_runtime_poisoned_after_open(plugin, rig):
    c, hw = plugin.controller, rig.hw
    owner = rig.new()
    assert owner.connect(timeout_s=.2)
    owner.close = lambda: hw.invoke('dispose', owner)
    c.device, c.initialized, c.main_state = owner, True, 'ST_ON'
    import threading
    entered, release = threading.Event(), threading.Event()
    try:
        with pytest.raises(RuntimeError, match='timed out'):
            owner._call_locked_with_timeout(
                lambda: hw.invoke('set_enable', owner, entered, release), .05, 'set_enable')
        assert entered.is_set()
        assert c._dispose_device() is False
        assert c.device is owner and c.initialized
        assert c.main_state == 'Shutdown unconfirmed'
        assert hw.count('close') == hw.count('dispose') == 0
        release.set()
        hw.join_workers()
        c.initializeCommunication()
        assert not plugin.created
        assert c.device is owner
        assert hw.count('open') == 1 and hw.count('close') == 0
    finally:
        release.set()
