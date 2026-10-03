"""Read-only return targets and live PSU currents, distinct from Ilim."""
import numpy as np
import pytest

from test_mscan import module, rig  # noqa: F401


def test_return_summary_displays_measured_current_not_ilim(rig):
    rails = rig.scan._plan['rails']
    expected = ['PSU_A_CH0: +40 V, Iget 2 mA', 'PSU_A_CH1: −50 V, Iget 2 mA']
    assert rig.scan._completion_text(rails).splitlines() == expected
    rails[0]['channel'].current_limit_readback = .032
    assert rig.scan._completion_text(rails).splitlines() == expected
    assert rig.psus[0].writes == []


def test_running_summary_keeps_return_voltage_and_updates_current(rig):
    rails = rig.scan._plan['rails']
    for r in rails:
        r['channel'].voltage_setpoint_readback = 70.
        r['channel'].current_readback = .0075
    assert rig.scan._completion_text(rails, running=True).splitlines() == [
        'PSU_A_CH0: +40 V, Iget 7.5 mA', 'PSU_A_CH1: −50 V, Iget 7.5 mA']
    assert rig.scan._completion_text(rails).splitlines() == [
        'PSU_A_CH0: +70 V, Iget 7.5 mA', 'PSU_A_CH1: −70 V, Iget 7.5 mA']
    assert rig.psus[0].writes == []


@pytest.mark.parametrize('value', [np.nan, np.inf, -np.inf, -.001, None, 'invalid'])
def test_missing_current_does_not_erase_return_voltage_or_other_channel(rig, value):
    rails = rig.scan._plan['rails']
    rails[0]['channel'].current_readback = value
    assert rig.scan._completion_text(rails).splitlines() == [
        'PSU_A_CH0: +40 V, Iget unavailable', 'PSU_A_CH1: −50 V, Iget 2 mA']


def test_zero_is_a_valid_measured_current(rig):
    rails = rig.scan._plan['rails']
    rails[0]['channel'].current_readback = 0.
    assert 'PSU_A_CH0: +40 V, Iget 0 mA' in rig.scan._completion_text(rails)


def test_return_voltage_is_not_invented_when_readback_is_missing(rig):
    rails = rig.scan._plan['rails']
    rails[0]['channel'].voltage_setpoint_readback = np.nan
    with pytest.raises(rig.module.ScanError, match='Vset readback unavailable'):
        rig.scan._completion_text(rails)
    # During the scan the initial target is already captured, even while the
    # driver invalidates readbacks for a voltage transition.
    assert 'PSU_A_CH0: +40 V, Iget 2 mA' in rig.scan._completion_text(rails, running=True)
