"""Explorer controllers must retain and retire the real runtime's failed Open.

The eleven runtimes are covered through their family's controller; sibling
entrypoint equivalence is enforced separately by the family parity tests.
"""

import pytest



@pytest.mark.parametrize('late_status', [0, -2])
def test_plugin_retries_only_after_old_open_is_finished_and_closed(cgc_plugin, cgc_rig, late_status):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_status = late_status
    hw.open_release.clear()
    c.initializeCommunication()
    assert len(cgc_plugin.created) == 1
    old = cgc_plugin.created[0]
    assert c.device is old, 'an unconfirmed handle owner must not be dropped'
    assert not c.initialized and not c.initializing
    assert not cgc_plugin.parent.onAction.state
    assert c.main_state == 'Connection pending'
    assert [name for name, _ in hw.operations] == ['open']
    assert not cgc_plugin.successes

    # An explicit retry while Open is blocked must not replace the owner.
    c.initializeCommunication()
    assert cgc_plugin.created == [old]
    assert hw.count('open') == 1 and hw.count('close') == 0

    # A late native result alone never enables outputs or starts a new Open.
    hw.finish_open()
    assert c.device is old and not c.initialized
    assert hw.count('open') == 1 and hw.count('close') == 0
    hw.open_status = 0
    c.initializeCommunication()  # the next explicit connection request
    assert len(cgc_plugin.created) == 2
    assert c.device is cgc_plugin.created[1]
    assert cgc_plugin.successes == [True]
    assert [name for name, owner in hw.operations if owner == id(old)] == [
        'open', 'close', 'dispose']
    assert old._transport_poisoned and old.thread_lock.locked()
    assert cgc_rig.disconnect(c.device) is True


def test_plugin_cannot_discard_ambiguous_close_minus_three(cgc_plugin, cgc_rig):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    hw.open_status, hw.close_status = -2, -3
    hw.allocate_handle = False
    c.initializeCommunication()
    assert len(cgc_plugin.created) == 1
    old = cgc_plugin.created[0]
    assert c.device is old and old._dll_port_claimed
    assert c.main_state == 'Connection pending' and not c.initialized
    assert not cgc_plugin.parent.onAction.state
    assert set(name for name, _ in hw.operations) == {'open', 'close'}
    c.initializeCommunication()
    assert cgc_plugin.created == [old]
    assert hw.count('open') == 1
    assert hw.count('dispose') == 0
    assert not cgc_plugin.successes


def test_disposal_does_not_discard_a_runtime_poisoned_after_open(cgc_plugin, cgc_rig):
    c, hw = cgc_plugin.controller, cgc_rig.hw
    owner = cgc_rig.new()
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
        assert not cgc_plugin.created
        assert c.device is owner
        assert hw.count('open') == 1 and hw.count('close') == 0
    finally:
        release.set()
