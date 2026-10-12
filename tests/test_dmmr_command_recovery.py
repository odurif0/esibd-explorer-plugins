"""Startup failures from 24/09: a malformed ACK must not blindly replay writes."""

import pytest



@pytest.mark.parametrize('address', [2, 7])
@pytest.mark.parametrize('desync', [False, True])
def test_logged_autorange_fault_is_verified_without_replaying_the_write(dmmr_startup, address, desync):
    hw = dmmr_startup.hardware
    hw.faults[('auto_range', address)] = -12
    hw.desync = desync
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.acquiring and dmmr_startup.parent.on
    assert hw.enabled and not hw.automatic
    assert hw.calls.count(('auto_range', address, True)) == 1
    assert hw.calls.count(('enable', True)) == 1
    assert hw.calls.count(('purge',)) == int(desync)
    assert hw.port_baud == 230400
    assert set(dmmr_startup.controller._verified_read_ranges) == set(range(8))
    event, = hw.events
    assert event['phase'] == 'startup' and event['status'] == -12
    assert event['verified'] and event['purged'] is desync
    assert any('Recovered DMMR startup' in msg for msg in dmmr_startup.messages)
    assert not any(c[0] == 'disconnect' for c in hw.calls)


@pytest.mark.parametrize('fault', ['fixed_range', 'range_readback', 'automatic', 'read_automatic'])
@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_other_receive_faults_share_the_same_startup_budget(dmmr_startup, fault, status):
    hw = dmmr_startup.hardware
    dmmr_startup.channels[3]._requested_range_mode = '4'
    key = (fault, 3) if fault in ('fixed_range', 'range_readback') else ((fault, False) if fault == 'automatic' else (fault,))
    hw.faults[key] = status
    hw.desync = True
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.acquiring
    assert (hw.ranges[3], hw.modes[3]) == (4, False)
    assert hw.calls.count(('fixed_range', 3, 4)) == 1
    assert hw.calls.count(('purge',)) == 1
    assert hw.events[0]['status'] == status and hw.events[0]['verified']


@pytest.mark.parametrize('desync', [False, True])
def test_second_fault_in_same_startup_aborts_instead_of_retrying_forever(dmmr_startup, desync):
    hw = dmmr_startup.hardware
    hw.desync = desync
    hw.faults = {('auto_range', 2): -12, ('auto_range', 7): -12}
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring
    assert not hw.enabled and not hw.automatic
    assert not dmmr_startup.parent.on
    starts = [event for event in hw.events if event['phase'] == 'startup']
    assert len(starts) == 2 and starts[0]['verified'] and not starts[1]['verified']
    assert 'budget' in starts[1]['error']
    assert hw.calls.count(('enable', True)) == 1
    assert hw.calls.count(('purge',)) <= 2  # Separate one-purge budgets: startup, then OFF.


@pytest.mark.parametrize('fault', ['auto_range', 'fixed_range'])
def test_wrong_readback_never_replays_a_configuration_write(dmmr_startup, fault):
    hw = dmmr_startup.hardware
    if fault == 'fixed_range':
        dmmr_startup.channels[2]._requested_range_mode = '3'
    hw.reject_write = (fault, 2)
    hw.faults[hw.reject_write] = -12
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring and not dmmr_startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert hw.counts[(fault, 2)] == 1
    assert ('purge',) not in hw.calls
    assert not hw.events[0]['verified']


def test_purge_must_not_silently_change_already_verified_modules(dmmr_startup):
    hw = dmmr_startup.hardware
    dmmr_startup.channels[0]._requested_range_mode = '2'
    hw.faults[('auto_range', 5)] = -12
    hw.desync = True
    hw.purge_hook = lambda: hw.ranges.__setitem__(0, 0)
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring and not dmmr_startup.parent.on
    assert hw.calls.count(('fixed_range', 0, 2)) == 1
    assert not hw.events[0]['verified']
    assert 'module 0' in hw.events[0]['error']


@pytest.mark.parametrize('poison_during_purge', [False, True])
def test_poisoned_transport_never_gets_another_call_including_cleanup(dmmr_startup, poison_during_purge):
    hw = dmmr_startup.hardware
    hw.faults[('auto_range', 2)] = -12
    if poison_during_purge:
        hw.desync = True
        hw.purge_hook = lambda: setattr(hw, '_transport_poisoned', True)
    else:
        hw.poison_at = ('auto_range', 2)
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring
    assert dmmr_startup.parent.on and dmmr_startup.controller.main_state == 'Shutdown unconfirmed'
    assert dmmr_startup.controller.initialized and dmmr_startup.controller.device is hw
    assert hw.calls[-1] == (('purge',) if poison_during_purge else ('auto_range', 2, True))
    assert not any(c[0] == 'disconnect' for c in hw.calls)


def test_lost_enable_ack_is_still_a_failed_on_with_verified_off(dmmr_startup):
    hw = dmmr_startup.hardware
    hw.faults[('enable', True)] = -12
    hw.desync = True
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring and not dmmr_startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert hw.calls.count(('enable', True)) == 1
    assert not any(e['phase'] == 'startup' for e in hw.events)
    assert hw.events[-1]['phase'] == 'shutdown' and hw.events[-1]['verified']


def test_persistent_desynchronization_retains_backend_and_unconfirmed_stop(dmmr_startup):
    hw = dmmr_startup.hardware
    hw.faults[('auto_range', 2)] = -12
    hw.desync = hw.persistent = True
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring and dmmr_startup.parent.on
    assert dmmr_startup.controller.main_state == 'Shutdown unconfirmed'
    assert dmmr_startup.controller.device is hw and dmmr_startup.controller.initialized
    assert hw.calls.count(('purge',)) == 2
    assert all(not e['verified'] for e in hw.events)
    assert not any(c[0] == 'disconnect' for c in hw.calls)


def test_explicit_off_resynchronizes_once_then_closes_after_both_verified_gates(dmmr_startup):
    hw = dmmr_startup.hardware
    dmmr_startup.controller.toggleOn()
    dmmr_startup.parent.on = False
    hw.sticky = True
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.main_state == 'Disconnected'
    assert not dmmr_startup.controller.initialized and dmmr_startup.controller.device is None
    assert hw.calls.count(('purge',)) == 1
    closed = hw.calls.index(('disconnect',))
    assert hw.calls[closed-2:closed] == [('read_automatic',), ('read_enable',)]
    assert not hw.enabled and not hw.automatic and not hw.connected


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_final_startup_state_read_uses_same_checked_recovery(dmmr_startup, status):
    hw = dmmr_startup.hardware
    hw.faults[('state',)] = status
    hw.desync = True
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.acquiring and dmmr_startup.parent.on
    assert hw.calls.count(('purge',)) == 1
    assert hw.calls.count(('enable', True)) == 1
    assert hw.events[-1]['verified']
    assert len(dmmr_startup.controller._verified_read_ranges) == 8


@pytest.mark.parametrize('code', [1, 2, 0x8001])
def test_valid_reply_with_non_running_hardware_state_cannot_start_acquisition(dmmr_startup, code):
    hw = dmmr_startup.hardware
    state_name = 'ST_ERR_MODULE' if code == 0x8001 else f'UNKNOWN_STATE_0x{code:04X}'
    hw.get_state = lambda **kw: (0, f'0x{code:04X}', state_name)
    dmmr_startup.controller.toggleOn()
    assert not dmmr_startup.controller.acquiring and not dmmr_startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert ('purge',) not in hw.calls
    assert any(state_name in msg for msg in dmmr_startup.messages)


def test_lost_off_ack_with_both_false_readbacks_needs_no_purge(dmmr_startup):
    hw = dmmr_startup.hardware
    dmmr_startup.controller.toggleOn()
    dmmr_startup.parent.on = False
    hw.counts[('automatic', False)] = 0
    hw.faults[('automatic', False)] = -12
    hw.purge_status = -12  # An unnecessary purge must not turn verified OFF into failure.
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.main_state == 'Disconnected'
    assert not hw.enabled and not hw.automatic and not hw.connected
    assert ('purge',) not in hw.calls
    assert hw.events[-1]['verified'] and not hw.events[-1]['purged']
