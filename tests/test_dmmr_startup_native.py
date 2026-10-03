"""Controller + real runtime/ctypes boundary, with the DLL responses simulated."""
import json

import pytest

from test_dmmr_command_recovery import rig, startup  # noqa: F401
from test_dmmr_startup_diagnostics import NativeFunction, native  # noqa: F401


def wire_native(driver, hardware):
    """Keep the actual locks, timeout wrapper, diagnostics and ACK workaround."""
    driver.baudrate = hardware.baudrate
    driver.err_dict = {}
    driver.hk_running = False
    driver.device_id = 'DMMR-native-startup-test'
    driver._optional_command_support = {}
    driver._optional_command_warnings = set()
    last_error = 0

    def bind(export, function):
        def checked(*args):
            nonlocal last_error
            assert driver.thread_lock.locked(), export
            status = function(*args)
            if status < 0:
                last_error = status
            return status
        setattr(driver.dll, 'COM_DMMR_8_' + export, NativeFunction(checked))

    def value(arg):
        return getattr(arg, 'value', arg)

    def flag(getter, pointer):
        status, result = getter()
        pointer._obj.value = result
        return status

    def state(pointer):
        status, result, _ = hardware.get_state()
        pointer._obj.value = int(result, 16)
        return status

    def ranges(address, meas_range, auto):
        status, result, enabled = hardware.get_module_meas_range(value(address))
        meas_range._obj.value, auto._obj.value = result, enabled
        return status

    def baud(pointer):
        status, result = hardware.set_baud_rate(pointer._obj.value)
        pointer._obj.value = result
        return status

    def io(pointer):
        hardware.calls.append(('io',))
        pointer._obj.value = last_error
        return 0

    def comm(pointer):
        hardware.calls.append(('comm',))
        pointer._obj.value = 0
        return 0

    bind('SetEnable', lambda enabled: hardware.set_enable(value(enabled)))
    bind('GetEnable', lambda pointer: flag(hardware.get_enable, pointer))
    bind('SetAutomaticCurent', lambda enabled: hardware.set_automatic_current(value(enabled)))
    bind('GetAutomaticCurent', lambda pointer: flag(hardware.get_automatic_current, pointer))
    bind('SetModuleAutoRange', lambda address, enabled: hardware.set_module_auto_range(value(address), value(enabled)))
    bind('SetModuleMeasRange', lambda address, meas_range: hardware.set_module_meas_range(value(address), value(meas_range)))
    bind('GetModuleMeasRange', ranges)
    bind('GetState', state)
    bind('Purge', hardware.purge)
    bind('Close', lambda: 0 if hardware.disconnect() else -1)
    bind('SetBaudRate', baud)
    bind('GetIOState', io)
    bind('GetCommError', comm)
    driver._configure_dll_signatures()


@pytest.mark.parametrize('address', [2, 7])
def test_logged_g0_missing_reply_then_g2_or_g7_malformed_ack_through_real_runtime(startup, native, address):
    hw, driver = startup.hardware, native.driver
    wire_native(driver, hw)
    startup.controller.device = driver
    # g0Y reply absent is already verified by the runtime. The later g2Y/g7Y
    # malformed reply leaves subsequent exchanges desynchronized until purge.
    hw.faults = {('auto_range', 0): -10, ('auto_range', address): -12}
    hw.desync = True
    hw.desync_keys = {('auto_range', address)}
    startup.controller.toggleOn()
    assert startup.controller.acquiring and startup.parent.on
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
    rows = [json.loads(line) for line in native.directory.joinpath('dmmr_protocol_com13.jsonl').read_text().splitlines()]
    assert recovered in rows and all(event in rows for event in errors)
    assert driver._startup_log_path is None  # No continuous native capture.
    # Finish through the actual runtime OFF/close sequence, not fixture disposal.
    startup.parent.on = False
    startup.controller.toggleOn()
    assert startup.controller.main_state == 'Disconnected'
    assert not hw.enabled and not hw.automatic
