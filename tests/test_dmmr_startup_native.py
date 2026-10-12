"""Controller + real runtime/ctypes boundary, with the DLL responses simulated."""
import json

import pytest

from support.dmmr import wire_native


@pytest.mark.parametrize('address', [2, 7])
def test_logged_g0_missing_reply_then_g2_or_g7_malformed_ack_through_real_runtime(dmmr_startup, dmmr_native, address):
    hw, driver = dmmr_startup.hardware, dmmr_native.driver
    wire_native(driver, hw)
    dmmr_startup.controller.device = driver
    # g0Y reply absent is already verified by the runtime. The later g2Y/g7Y
    # malformed reply leaves subsequent exchanges desynchronized until purge.
    hw.faults = {('auto_range', 0): -10, ('auto_range', address): -12}
    hw.desync = True
    hw.desync_keys = {('auto_range', address)}
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.acquiring and dmmr_startup.parent.on
    assert hw.calls.count(('auto_range', 0, True)) == 1
    assert hw.calls.count(('auto_range', address, True)) == 1
    assert hw.calls.count(('enable', True)) == 1
    assert hw.calls.count(('purge',)) == 1
    assert hw.port_baud == 230400
    assert not driver._transport_poisoned
    failed = hw.calls.index(('auto_range', address, True))
    assert hw.calls[failed+1:failed+3] == [('io',), ('comm',)]
    assert hw.calls.index(('comm',), failed) < hw.calls.index(('purge',))
    events = driver.protocol_diagnostics()['events']
    errors = [event for event in events if event['kind'] == 'dll_error']
    assert [event['status'] for event in errors][:2] == [-10, -12]
    assert all('get_io_state' in event and 'get_comm_error' in event for event in errors)
    recovered, = [event for event in events if event['kind'] == 'command_recovery']
    assert recovered['phase'] == 'startup' and recovered['verified'] and recovered['purged']
    rows = [json.loads(line) for line in dmmr_native.directory.joinpath('dmmr_protocol_com13.jsonl').read_text().splitlines()]
    assert recovered in rows and all(event in rows for event in errors)
    assert driver._startup_log_path is None  # No continuous native capture.
    # Finish through the actual runtime OFF/close sequence, not fixture disposal.
    dmmr_startup.parent.on = False
    dmmr_startup.controller.toggleOn()
    assert dmmr_startup.controller.main_state == 'Disconnected'
    assert not hw.enabled and not hw.automatic
