"""MScan must consume actual AMX decoding, including calibrated edge delays."""
import pytest

from support.amx import load_module
from support.amx import config79_snapshot


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


def test_known_periodic_waveform_with_edge_compensation_can_scan(mscan_rig, amx_module):
    decoded(mscan_rig, amx_module)
    assert all(r['frequency'] == '500 kHz' for r in mscan_rig.amx.controller.output_rows)
    assert all('timing' not in r for r in mscan_rig.amx.controller.output_rows)  # no invented exact phase
    mscan_rig.scan._plan = mscan_rig.scan._preflight()
    assert not mscan_rig.psus[0].writes  # preflight is read-only
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation['status'] == 'completed', mscan_rig.scan._validation
    rows = mscan_rig.scan._plan['metadata']['waveform']
    assert rows[0]['waveform']['trigger_delay'] == {'rise': 3, 'fall': 7}


@pytest.mark.parametrize('change', ('rise', 'fall', 'pulse_width', 'pulse_delay'))
def test_changed_edge_or_pulser_timing_aborts_before_any_voltage_write(mscan_rig, amx_module, change):
    snapshot = decoded(mscan_rig, amx_module)
    mscan_rig.scan._plan = mscan_rig.scan._preflight()
    if change in ('rise', 'fall'):
        snapshot['switches'][0]['trigger_delay'][change] += 1
    else:
        snapshot['pulsers'][0]['width_ticks' if change == 'pulse_width' else 'delay_ticks'] += 1
    mscan_rig.amx.controller.output_rows = amx_module._amx_output_rows(snapshot)
    mscan_rig.scan.runScan(lambda: True)
    assert mscan_rig.scan._validation['status'] == 'error'
    assert 'AMX waveform' in mscan_rig.scan._validation['error']
    assert not mscan_rig.psus[0].writes


@pytest.mark.parametrize(('case', 'message'), [
    ('external', 'External'), ('mapping', 'mapping'), ('off', 'turn the AMX ON'),
    ('static', 'Static'), ('missing_delay', 'edge-delay'), ('invalid_delay', 'edge-delay'),
])


def test_unavailable_or_non_periodic_waveforms_explain_which_connector_and_why(mscan_rig, amx_module, case, message):
    snapshot = config79_snapshot()
    if case == 'external': snapshot['switches'][0]['trigger_config'] = 3
    if case == 'mapping': snapshot['switch_mapping']['trigger_enabled'] = True
    if case == 'off': mscan_rig.amx.isOn = lambda: False
    if case == 'static': snapshot['pulsers'][0]['width_ticks'] = 0
    if case == 'missing_delay': snapshot['switches'][0]['trigger_delay']['rise'] = None
    if case == 'invalid_delay': snapshot['switches'][0]['trigger_delay']['fall'] = 16
    mscan_rig.amx.controller.output_rows = amx_module._amx_output_rows(snapshot)
    with pytest.raises(mscan_rig.module.ScanError, match=message) as error:
        mscan_rig.scan._prepare()
    if case != 'off':
        assert 'AMX_A CH0' in str(error.value)
    assert not mscan_rig.psus[0].writes


def test_old_zero_delay_summary_remains_usable_but_old_adjusted_summary_requires_update(mscan_rig, amx_module):
    snapshot = config79_snapshot()
    rows = amx_module._amx_output_rows(snapshot)
    for row in rows: row.pop('waveform', None)
    mscan_rig.amx.controller.output_rows = rows
    assert mscan_rig.scan._prepare()
    for row in rows:
        row.pop('timing', None)
        row['dwell'] = 'Delay adjusted'
    with pytest.raises(mscan_rig.module.ScanError, match='edge-delay'):
        mscan_rig.scan._prepare()


def test_four_connector_selection_is_no_longer_accepted(mscan_rig):
    assert 'CH0-CH3' not in mscan_rig.scan.PAIRS
    mscan_rig.scan.amx_outputs = 'CH0-CH3'
    with pytest.raises(mscan_rig.module.ScanError, match='Select one AMX pair'):
        mscan_rig.scan._prepare()
    assert not any(p.writes for p in mscan_rig.psus)
