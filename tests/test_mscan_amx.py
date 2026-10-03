"""MScan must consume actual AMX decoding, including calibrated edge delays."""
import pytest

from test_amx_config_load import load_module
from test_amx_outputs import config79_snapshot
from test_mscan import module, rig  # noqa: F401


@pytest.fixture(params=('amx_a', 'amx_b'))
def amx_module(request):
    return load_module(request.param)


def decoded(rig, amx_module, **changes):
    snapshot = config79_snapshot()
    # Legitimate per-edge compensation registers, not a frequency change.
    for switch in snapshot['switches']:
        switch['trigger_delay'] = {'rise': 3, 'fall': 7}
    for key, value in changes.items():
        snapshot[key] = value
    rig.amx.controller.output_rows = amx_module._amx_output_rows(snapshot)
    return snapshot


def test_known_periodic_waveform_with_edge_compensation_can_scan(rig, amx_module):
    decoded(rig, amx_module)
    assert all(r['frequency'] == '500 kHz' for r in rig.amx.controller.output_rows)
    assert all('timing' not in r for r in rig.amx.controller.output_rows)  # no invented exact phase
    rig.scan._plan = rig.scan._preflight()
    assert not rig.psus[0].writes  # preflight is read-only
    rig.scan.runScan(lambda: True)
    assert rig.scan._validation['status'] == 'completed', rig.scan._validation
    rows = rig.scan._plan['metadata']['waveform']
    assert rows[0]['waveform']['trigger_delay'] == {'rise': 3, 'fall': 7}


@pytest.mark.parametrize('change', ('rise', 'fall', 'pulse_width', 'pulse_delay'))
def test_changed_edge_or_pulser_timing_aborts_before_any_voltage_write(rig, amx_module, change):
    snapshot = decoded(rig, amx_module)
    rig.scan._plan = rig.scan._preflight()
    if change in ('rise', 'fall'):
        snapshot['switches'][0]['trigger_delay'][change] += 1
    else:
        snapshot['pulsers'][0]['width_ticks' if change == 'pulse_width' else 'delay_ticks'] += 1
    rig.amx.controller.output_rows = amx_module._amx_output_rows(snapshot)
    rig.scan.runScan(lambda: True)
    assert rig.scan._validation['status'] == 'error'
    assert 'AMX waveform' in rig.scan._validation['error']
    assert not rig.psus[0].writes


@pytest.mark.parametrize(('case', 'message'), [
    ('external', 'External'), ('mapping', 'mapping'), ('off', 'turn the AMX ON'),
    ('static', 'Static'), ('missing_delay', 'edge-delay'), ('invalid_delay', 'edge-delay'),
])
def test_unavailable_or_non_periodic_waveforms_explain_which_connector_and_why(rig, amx_module, case, message):
    snapshot = config79_snapshot()
    if case == 'external': snapshot['switches'][0]['trigger_config'] = 3
    if case == 'mapping': snapshot['switch_mapping']['trigger_enabled'] = True
    if case == 'off': rig.amx.isOn = lambda: False
    if case == 'static': snapshot['pulsers'][0]['width_ticks'] = 0
    if case == 'missing_delay': snapshot['switches'][0]['trigger_delay']['rise'] = None
    if case == 'invalid_delay': snapshot['switches'][0]['trigger_delay']['fall'] = 16
    rig.amx.controller.output_rows = amx_module._amx_output_rows(snapshot)
    with pytest.raises(rig.module.ScanError, match=message) as error:
        rig.scan._prepare()
    if case != 'off':
        assert 'AMX_A CH0' in str(error.value)
    assert not rig.psus[0].writes


def test_old_zero_delay_summary_remains_usable_but_old_adjusted_summary_requires_update(rig, amx_module):
    snapshot = config79_snapshot()
    rows = amx_module._amx_output_rows(snapshot)
    for row in rows: row.pop('waveform', None)
    rig.amx.controller.output_rows = rows
    assert rig.scan._prepare()
    for row in rows:
        row.pop('timing', None)
        row['dwell'] = 'Delay adjusted'
    with pytest.raises(rig.module.ScanError, match='edge-delay'):
        rig.scan._prepare()


def test_four_connector_selection_is_no_longer_accepted(rig):
    assert 'CH0-CH3' not in rig.scan.PAIRS
    rig.scan.amx_outputs = 'CH0-CH3'
    with pytest.raises(rig.module.ScanError, match='Select one AMX pair'):
        rig.scan._prepare()
    assert not any(p.writes for p in rig.psus)
