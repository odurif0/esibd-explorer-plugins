"""Startup failures from 24/09: a malformed ACK must not blindly replay writes."""
from collections import Counter
import json
from types import SimpleNamespace

import pytest

from test_dmmr_ranges import RangeDevice, rig  # noqa: F401


class StartupDevice(RangeDevice):
    def __init__(self, recovery_class):
        super().__init__()
        self.recovery_class = recovery_class
        self.connected = True
        self._transport_poisoned = False
        self.baudrate = self.port_baud = 230400
        self.port_closes = 0
        self.modes = dict.fromkeys(range(8), False)
        self.ranges = dict.fromkeys(range(8), 0)
        self.faults, self.events, self.counts = {}, [], Counter()
        self.sticky = self.desync = self.persistent = False
        self.desync_keys = None
        self.poison_at = self.reject_write = None
        self.purge_status = 0
        self.purge_hook = lambda: None

    def new_command_recovery(self, **kw):
        return self.recovery_class(self, **kw)

    def record_protocol_event(self, event):
        self.events.append(json.loads(json.dumps(event)))

    def reply(self, key):
        self.counts[key] += 1
        if self.poison_at == key:
            self._transport_poisoned = True
        if key in self.faults and self.counts[key] == 1:
            if self.desync and (self.desync_keys is None or key in self.desync_keys):
                self.sticky = True
            return self.faults[key]
        return -13 if self.sticky else 0

    def set_module_auto_range(self, address, enabled, **kw):
        self.calls.append(('auto_range', address, enabled))
        if self.reject_write != ('auto_range', address):
            self.modes[address] = enabled
        return self.reply(('auto_range', address))

    def set_module_meas_range(self, address, value, **kw):
        self.calls.append(('fixed_range', address, value))
        if self.reject_write != ('fixed_range', address):
            self.ranges[address] = value
        return self.reply(('fixed_range', address))

    def get_module_meas_range(self, address, **kw):
        self.calls.append(('range_readback', address))
        return self.reply(('range_readback', address)), self.ranges[address], self.modes[address]

    def get_state(self, **kw):
        self.calls.append(('state',))
        return self.reply(('state',)), '0x0000', 'ST_ON'

    def set_enable(self, value, **kw):
        self.calls.append(('enable', value))
        self.enabled = value
        return self.reply(('enable', value))

    def set_automatic_current(self, value, **kw):
        self.calls.append(('automatic', value))
        self.automatic = value
        return self.reply(('automatic', value))

    def get_enable(self, **kw):
        self.calls.append(('read_enable',))
        return self.reply(('read_enable',)), self.enabled

    def get_automatic_current(self, **kw):
        self.calls.append(('read_automatic',))
        return self.reply(('read_automatic',)), self.automatic

    def _call_locked_with_timeout(self, method, timeout, name, *args):
        return method(*args)

    def purge(self):
        self.calls.append(('purge',))
        self.port_baud = 9600
        self.sticky = self.persistent
        self.purge_hook()
        return self.purge_status

    def set_baud_rate(self, baud):
        self.calls.append(('baud', baud))
        self.port_baud = baud
        return 0, baud

    def disconnect(self):
        if not self.connected:
            return True
        assert not self.enabled and not self.automatic
        self.calls.append(('disconnect',))
        self.connected = False
        self.port_closes += 1
        return True


@pytest.fixture
def startup(rig):
    recovery = rig.module._get_dmmr_driver_class().CommandRecovery
    rig.hardware = rig.controller.device = StartupDevice(recovery)
    rig.channels[:] = [SimpleNamespace(real=True, _requested_range_mode='Auto',
                                      module_address=lambda address=address: address)
                       for address in range(8)]
    rig.parent.getConfiguredModules = lambda: list(range(8))
    rig.controller.detected_module_ids = list(range(8))
    return rig


@pytest.mark.parametrize('address', [2, 7])
@pytest.mark.parametrize('desync', [False, True])
def test_logged_autorange_fault_is_verified_without_replaying_the_write(startup, address, desync):
    hw = startup.hardware
    hw.faults[('auto_range', address)] = -12
    hw.desync = desync
    startup.controller.toggleOn()
    assert startup.controller.acquiring and startup.parent.on
    assert hw.enabled and not hw.automatic
    assert hw.calls.count(('auto_range', address, True)) == 1
    assert hw.calls.count(('enable', True)) == 1
    assert hw.calls.count(('purge',)) == int(desync)
    assert hw.port_baud == 230400
    assert set(startup.controller._verified_read_ranges) == set(range(8))
    event, = hw.events
    assert event['phase'] == 'startup' and event['status'] == -12
    assert event['verified'] and event['purged'] is desync
    assert any('Recovered DMMR startup' in msg for msg in startup.messages)
    assert not any(c[0] == 'disconnect' for c in hw.calls)


@pytest.mark.parametrize('fault', ['fixed_range', 'range_readback', 'automatic', 'read_automatic'])
@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_other_receive_faults_share_the_same_startup_budget(startup, fault, status):
    hw = startup.hardware
    startup.channels[3]._requested_range_mode = '4'
    key = (fault, 3) if fault in ('fixed_range', 'range_readback') else ((fault, False) if fault == 'automatic' else (fault,))
    hw.faults[key] = status
    hw.desync = True
    startup.controller.toggleOn()
    assert startup.controller.acquiring
    assert (hw.ranges[3], hw.modes[3]) == (4, False)
    assert hw.calls.count(('fixed_range', 3, 4)) == 1
    assert hw.calls.count(('purge',)) == 1
    assert hw.events[0]['status'] == status and hw.events[0]['verified']


@pytest.mark.parametrize('desync', [False, True])
def test_second_fault_in_same_startup_aborts_instead_of_retrying_forever(startup, desync):
    hw = startup.hardware
    hw.desync = desync
    hw.faults = {('auto_range', 2): -12, ('auto_range', 7): -12}
    startup.controller.toggleOn()
    assert not startup.controller.acquiring
    assert not hw.enabled and not hw.automatic
    assert not startup.parent.on
    starts = [event for event in hw.events if event['phase'] == 'startup']
    assert len(starts) == 2 and starts[0]['verified'] and not starts[1]['verified']
    assert 'budget' in starts[1]['error']
    assert hw.calls.count(('enable', True)) == 1
    assert hw.calls.count(('purge',)) <= 2  # Separate one-purge budgets: startup, then OFF.


@pytest.mark.parametrize('fault', ['auto_range', 'fixed_range'])
def test_wrong_readback_never_replays_a_configuration_write(startup, fault):
    hw = startup.hardware
    if fault == 'fixed_range':
        startup.channels[2]._requested_range_mode = '3'
    hw.reject_write = (fault, 2)
    hw.faults[hw.reject_write] = -12
    startup.controller.toggleOn()
    assert not startup.controller.acquiring and not startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert hw.counts[(fault, 2)] == 1
    assert ('purge',) not in hw.calls
    assert not hw.events[0]['verified']


def test_purge_must_not_silently_change_already_verified_modules(startup):
    hw = startup.hardware
    startup.channels[0]._requested_range_mode = '2'
    hw.faults[('auto_range', 5)] = -12
    hw.desync = True
    hw.purge_hook = lambda: hw.ranges.__setitem__(0, 0)
    startup.controller.toggleOn()
    assert not startup.controller.acquiring and not startup.parent.on
    assert hw.calls.count(('fixed_range', 0, 2)) == 1
    assert not hw.events[0]['verified']
    assert 'module 0' in hw.events[0]['error']


@pytest.mark.parametrize('poison_during_purge', [False, True])
def test_poisoned_transport_never_gets_another_call_including_cleanup(startup, poison_during_purge):
    hw = startup.hardware
    hw.faults[('auto_range', 2)] = -12
    if poison_during_purge:
        hw.desync = True
        hw.purge_hook = lambda: setattr(hw, '_transport_poisoned', True)
    else:
        hw.poison_at = ('auto_range', 2)
    startup.controller.toggleOn()
    assert not startup.controller.acquiring
    assert startup.parent.on and startup.controller.main_state == 'Shutdown unconfirmed'
    assert startup.controller.initialized and startup.controller.device is hw
    assert hw.calls[-1] == (('purge',) if poison_during_purge else ('auto_range', 2, True))
    assert not any(c[0] == 'disconnect' for c in hw.calls)


def test_lost_enable_ack_is_still_a_failed_on_with_verified_off(startup):
    hw = startup.hardware
    hw.faults[('enable', True)] = -12
    hw.desync = True
    startup.controller.toggleOn()
    assert not startup.controller.acquiring and not startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert hw.calls.count(('enable', True)) == 1
    assert not any(e['phase'] == 'startup' for e in hw.events)
    assert hw.events[-1]['phase'] == 'shutdown' and hw.events[-1]['verified']


def test_persistent_desynchronization_retains_backend_and_unconfirmed_stop(startup):
    hw = startup.hardware
    hw.faults[('auto_range', 2)] = -12
    hw.desync = hw.persistent = True
    startup.controller.toggleOn()
    assert not startup.controller.acquiring and startup.parent.on
    assert startup.controller.main_state == 'Shutdown unconfirmed'
    assert startup.controller.device is hw and startup.controller.initialized
    assert hw.calls.count(('purge',)) == 2
    assert all(not e['verified'] for e in hw.events)
    assert not any(c[0] == 'disconnect' for c in hw.calls)


def test_explicit_off_resynchronizes_once_then_closes_after_both_verified_gates(startup):
    hw = startup.hardware
    startup.controller.toggleOn()
    startup.parent.on = False
    hw.sticky = True
    startup.controller.toggleOn()
    assert startup.controller.main_state == 'Disconnected'
    assert not startup.controller.initialized and startup.controller.device is None
    assert hw.calls.count(('purge',)) == 1
    closed = hw.calls.index(('disconnect',))
    assert hw.calls[closed-2:closed] == [('read_automatic',), ('read_enable',)]
    assert not hw.enabled and not hw.automatic and not hw.connected


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
def test_final_startup_state_read_uses_same_checked_recovery(startup, status):
    hw = startup.hardware
    hw.faults[('state',)] = status
    hw.desync = True
    startup.controller.toggleOn()
    assert startup.controller.acquiring and startup.parent.on
    assert hw.calls.count(('purge',)) == 1
    assert hw.calls.count(('enable', True)) == 1
    assert hw.events[-1]['verified']
    assert len(startup.controller._verified_read_ranges) == 8


@pytest.mark.parametrize('code', [1, 2, 0x8001])
def test_valid_reply_with_non_running_hardware_state_cannot_start_acquisition(startup, code):
    hw = startup.hardware
    state_name = 'ST_ERR_MODULE' if code == 0x8001 else f'UNKNOWN_STATE_0x{code:04X}'
    hw.get_state = lambda **kw: (0, f'0x{code:04X}', state_name)
    startup.controller.toggleOn()
    assert not startup.controller.acquiring and not startup.parent.on
    assert not hw.enabled and not hw.automatic
    assert ('purge',) not in hw.calls
    assert any(state_name in msg for msg in startup.messages)


def test_lost_off_ack_with_both_false_readbacks_needs_no_purge(startup):
    hw = startup.hardware
    startup.controller.toggleOn()
    startup.parent.on = False
    hw.counts[('automatic', False)] = 0
    hw.faults[('automatic', False)] = -12
    hw.purge_status = -12  # An unnecessary purge must not turn verified OFF into failure.
    startup.controller.toggleOn()
    assert startup.controller.main_state == 'Disconnected'
    assert not hw.enabled and not hw.automatic and not hw.connected
    assert ('purge',) not in hw.calls
    assert hw.events[-1]['verified'] and not hw.events[-1]['purged']
