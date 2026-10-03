"""Prevent redundant range writes; recover a necessary write without replaying it."""
import pytest

from test_dmmr_command_recovery import rig, startup  # noqa: F401
from test_dmmr_zero_notebook import ns  # noqa: F401


@pytest.fixture(params=['notebook', 'plugin'])
def setup_case(request, startup, ns):
    hw = startup.hardware
    hw.enabled, hw.automatic = True, False
    for channel in startup.channels:
        channel._requested_range_mode = '0'

    def configure():
        if request.param == 'notebook':
            ns['configure_range'](hw, list(range(8)), 0, .1, [], set())
        else:
            recovery = startup.controller._new_command_recovery(hw, .1, 'startup')
            startup.controller._configure_module_ranges(hw, list(range(8)), .1, recovery=recovery)
    return hw, configure


def test_matching_fixed_ranges_are_read_but_never_written(setup_case):
    hw, configure = setup_case
    configure()
    assert {c[1] for c in hw.calls if c[0] == 'range_readback'} == set(range(8))
    assert not any(c[0] in ('auto_range', 'fixed_range') for c in hw.calls)


@pytest.mark.parametrize('status', [-10, -11, -12, -13])
@pytest.mark.parametrize('desync', [False, True])
def test_required_range_write_with_lost_ack_is_verified_not_replayed(setup_case, status, desync):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.faults[('fixed_range', 3)] = status
    hw.desync = desync
    configure()
    assert all(hw.ranges[a] == 0 and hw.modes[a] is False for a in range(8))
    assert hw.calls.count(('fixed_range', 3, 0)) == 1
    assert hw.calls.count(('purge',)) == int(desync)
    assert not any(c[0] in ('enable', 'disconnect') for c in hw.calls)
    incident, = hw.events
    assert incident['status'] == status and incident['verified']


def test_range_is_reread_after_switching_out_of_autorange(setup_case):
    hw, configure = setup_case
    hw.modes[3] = True
    original = hw.set_module_auto_range

    def setter(address, enabled, **kwargs):
        result = original(address, enabled, **kwargs)
        if address == 3:
            hw.ranges[address] = 2  # Auto range moved since the initial read.
        return result
    hw.set_module_auto_range = setter
    configure()
    assert (hw.ranges[3], hw.modes[3]) == (0, False)
    assert hw.calls.count(('fixed_range', 3, 0)) == 1
    start = hw.calls.index(('auto_range', 3, False))
    assert hw.calls[start + 1] == ('range_readback', 3)


def test_purge_must_recheck_previously_verified_modules(setup_case):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.faults[('fixed_range', 3)] = -12
    hw.desync = True
    hw.purge_hook = lambda: hw.ranges.__setitem__(0, 2)
    with pytest.raises(RuntimeError, match='[Mm]odule 0'):
        configure()
    assert not hw.events[-1]['verified']
    assert hw.calls.count(('fixed_range', 3, 0)) == 1


def test_second_fault_aborts_the_configuration(setup_case):
    hw, configure = setup_case
    hw.ranges.update({2: 4, 3: 4})
    hw.faults = {('fixed_range', 2): -12, ('fixed_range', 3): -12}
    with pytest.raises(RuntimeError, match='budget'):
        configure()
    assert len(hw.events) == 2
    assert hw.events[0]['verified'] and not hw.events[1]['verified']


def test_configuration_does_not_replay_a_rejected_write(setup_case):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.reject_write = ('fixed_range', 3)
    hw.faults[hw.reject_write] = -12
    with pytest.raises(RuntimeError, match='[Mm]odule 3'):
        configure()
    assert not hw.events[-1]['verified']
    assert hw.calls.count(('fixed_range', 3, 0)) == 1
    assert ('purge',) not in hw.calls


def test_poisoned_command_is_the_last_hardware_call(setup_case):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.poison_at = ('fixed_range', 3)
    with pytest.raises(RuntimeError, match='blocked'):
        configure()
    assert hw.calls[-1] == ('fixed_range', 3, 0)


def test_range_setup_read_error_is_recovered_without_writing(setup_case):
    hw, configure = setup_case
    hw.faults[('range_readback', 3)] = -12
    hw.desync = True
    configure()
    assert hw.calls.count(('purge',)) == 1
    assert not any(c[0] in ('auto_range', 'fixed_range', 'enable', 'disconnect') for c in hw.calls)
    assert hw.events[-1]['verified']


def test_persistent_receive_error_stops_after_one_purge(setup_case):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.faults[('fixed_range', 3)] = -12
    hw.desync = hw.persistent = True
    with pytest.raises(RuntimeError, match='-13'):
        configure()
    assert hw.calls.count(('purge',)) == 1
    assert hw.calls.count(('fixed_range', 3, 0)) == 1
    assert not hw.events[-1]['verified']
    assert not any(c[0] in ('enable', 'disconnect') for c in hw.calls)


def test_recovery_does_not_reenable_a_disabled_controller(setup_case):
    hw, configure = setup_case
    hw.ranges[3] = 4
    hw.faults[('fixed_range', 3)] = -12
    hw.enabled = False
    with pytest.raises(RuntimeError, match='Measurement enable not confirmed'):
        configure()
    assert not hw.events[-1]['verified']
    assert not any(c[0] in ('enable', 'purge', 'disconnect') for c in hw.calls)


def test_matching_autorange_is_not_rewritten(startup):
    hw = startup.hardware
    hw.enabled, hw.automatic = True, False
    hw.modes = dict.fromkeys(range(8), True)
    startup.controller._configure_module_ranges(hw, list(range(8)), .1,
        recovery=startup.controller._new_command_recovery(hw, .1, 'startup'))
    assert not any(c[0] in ('auto_range', 'fixed_range') for c in hw.calls)
