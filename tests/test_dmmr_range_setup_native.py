"""Range setup crosses real runtime/ctypes/logging, never raw mock setters only."""
import json

import pytest

from support.dmmr import wire_native


@pytest.mark.parametrize('consumer', ['plugin', 'notebook'])
@pytest.mark.parametrize('changed', [False, True])
def test_g30_reply_mismatch_diagnostics_and_single_write(dmmr_startup, dmmr_native, dmmr_notebook_ns, consumer, changed):
    hw, driver = dmmr_startup.hardware, dmmr_native.driver
    hw.enabled, hw.automatic = True, False
    hw.ranges[3] = 4 if changed else 0
    hw.faults = {('fixed_range', 3): -12}
    hw.desync = True
    wire_native(driver, hw)
    if consumer == 'notebook':
        recoveries = []
        dmmr_notebook_ns['configure_range'](driver, list(range(8)), 0, .1, [], set(), recoveries)
    else:
        dmmr_startup.controller.device = driver
        for channel in dmmr_startup.channels:
            channel._requested_range_mode = '0'
        dmmr_startup.controller._configure_module_ranges(driver, list(range(8)), .1,
            recovery=dmmr_startup.controller._new_command_recovery(driver, .1, 'startup'))
    assert all(hw.ranges[a] == 0 and hw.modes[a] is False for a in range(8))
    assert hw.calls.count(('fixed_range', 3, 0)) == int(changed)
    assert hw.calls.count(('purge',)) == int(changed)
    assert not any(c[0] in ('auto_range', 'enable', 'disconnect') for c in hw.calls)
    if changed:
        index = hw.calls.index(('fixed_range', 3, 0))
        assert hw.calls[index + 1:index + 3] == [('io',), ('comm',)]
        events = driver.protocol_diagnostics()['events']
        recovered, = [e for e in events if e['kind'] == 'command_recovery']
        assert recovered['verified'] and recovered['purged'] and recovered['status'] == -12
        assert hw.port_baud == 230400
        assert not driver._transport_poisoned
        rows = [json.loads(line) for line in (dmmr_native.directory / 'dmmr_protocol_com13.jsonl').read_text().splitlines()]
        assert recovered in rows
        if consumer == 'notebook':
            incident, = recoveries
            serialized = json.loads(json.dumps(incident))  # Protocol log stores argument tuples as lists.
            assert all(recovered[key] == value for key, value in serialized.items())
