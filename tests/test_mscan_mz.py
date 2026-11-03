"""m/z from the RF amplitude in MScan (user request 2026-11-03; Q2: 5 mm between opposite rods, r0 2.5 mm).

Ideal 50 % square wave: m/z = 4 e A / (q r0² (2π f)²), q the centre of the stable band on the
scan line a = 2 (U/A) q, or the low-mass cut-off q = 0.712 when RF only. Values checked against
the band table of docs/mscan_quadrupole.pdf.
"""
import json

import h5py
import numpy as np
import pytest

from support.mscan import add_offset


@pytest.mark.parametrize(('k', 'low', 'high'), [
    (.10, .246, .629), (.15, .375, .593), (.18, .455, .574), (.20, .510, .562), (.21, .538, .556),
])
def test_the_stable_band_matches_the_documented_table(mscan_module, k, low, high):
    band = mscan_module.square_wave_band(k)
    assert band == pytest.approx((low, high), abs=1.5e-3)


def test_nothing_is_transmitted_above_the_apex_and_rf_only_is_a_high_pass(mscan_module):
    assert mscan_module.square_wave_band(.2153) is not None
    assert mscan_module.square_wave_band(.22) is None
    assert mscan_module.square_wave_band(0.) == (0., .712)


def test_mz_follows_the_documented_formula(mscan_module):
    mz = mscan_module.mz_from_amplitude
    # 2.444 × A / q̄ × (4 mm / r0)² × (500 kHz / f)², q̄ = 0.536 at k = 0.2.
    assert mz([100.], frequency_hz=5e5, r0_mm=4., factor=.2)[0] == pytest.approx(2.444 * 100 / .5358, rel=2e-3)
    assert mz([100.], frequency_hz=5e5, r0_mm=4.)[0] == pytest.approx(2.444 * 100 / .712, rel=2e-3)  # RF only
    base = mz([100.], frequency_hz=5e5, r0_mm=5., factor=.2)[0]
    assert mz([100.], frequency_hz=2.5e5, r0_mm=5., factor=.2)[0] == pytest.approx(4 * base)  # ∝ 1/f²
    assert mz([100.], frequency_hz=5e5, r0_mm=2.5, factor=.2)[0] == pytest.approx(4 * base)  # ∝ 1/r0²
    assert mz([100., 200.], frequency_hz=5e5, r0_mm=5., factor=.2)[1] == pytest.approx(2 * base)  # ∝ A at fixed k
    # δ lowers U/A, so the band centre moves down and the same A means a heavier ion.
    assert mz([100.], frequency_hz=5e5, r0_mm=5., factor=.2, resolution=2.)[0] > base


def test_mz_is_unknown_with_one_pair_and_absent_above_the_apex(mscan_module):
    mz = mscan_module.mz_from_amplitude
    assert np.isnan(mz([100.], frequency_hz=5e5, r0_mm=5., factor=.2, pairs=1)).all()
    assert np.isnan(mz([100.], frequency_hz=5e5, r0_mm=5., factor=.22)).all()
    # The minimum U keeps U/A above the apex at small A: nothing transmitted there.
    values = mz([1., 100.], frequency_hz=5e5, r0_mm=5., factor=.2, minimum=.5)
    assert np.isnan(values[0]) and np.isfinite(values[1])


def with_frequency(r, text='500 kHz'):
    for row in r.amx.controller.output_rows:
        row['frequency'] = text


def test_the_scale_row_gives_the_mz_range_of_the_scan(mscan_rig):
    s = mscan_rig.scan
    s.mz_r0_mm = 5.
    with pytest.raises(mscan_rig.module.ScanError, match='AMX frequency unavailable'):
        s._mz_scale_text()
    with_frequency(mscan_rig)
    # No rod pair: RF only, the low-mass cut-off.
    assert s._mz_scale_text().splitlines() == [
        'A = 10 → 30 V ≈ m/z 22 → 66 (low-mass cut-off, RF only)',
        '500 kHz, r0 5 mm, RF attenuation factor 1']
    first, second = add_offset(mscan_rig, factor=.2, fixed=10.), add_offset(mscan_rig, factor=.2, fixed=10., pair=2)
    s.offset_minimum, s.offset_resolution = .5, 0.
    assert s._mz_scale_text().splitlines()[0] == 'A = 10 → 30 V ≈ m/z 29 → 88 (peak centre)'
    s.offset_channel_2 = s.NO_OFFSET
    assert s._mz_scale_text() == 'No m/z with one rod pair: the DC between the pairs is unknown.'
    assert first.writes == second.writes == []  # Display only.


def test_the_frequency_comes_from_the_amx_source_period(mscan_module):
    rows = [dict(waveform=dict(clock_hz=1e8, source=dict(period=200)))] * 2
    assert mscan_module.MScan._frequency_hz(rows) == 5e5
    assert mscan_module.MScan._frequency_hz([dict(frequency='300 kHz')]) == 3e5
    with pytest.raises(mscan_module.ScanError):
        mscan_module.MScan._frequency_hz([dict(frequency='300 kHz'), dict(frequency='500 kHz')])


def test_the_plot_axis_and_the_file_use_the_scan_settings(mscan_rig, tmp_path):
    s = mscan_rig.scan
    with_frequency(mscan_rig)
    s.mz_r0_mm, s.mz_axis = 5., s.AXIS_MZ
    two = [add_offset(mscan_rig, factor=.2, fixed=10.), add_offset(mscan_rig, factor=.2, fixed=10., pair=2)]
    mscan_rig.on_pause = mscan_rig.offset.settle
    s._plan = s._preflight()
    s._plan['detectors'] = [mscan_rig.detector]
    k = len(s._plan['offsets'])
    s._validation.update(offset_v=np.full((3, k), np.nan), offset_target=np.full((3, k), np.nan))
    s.runScan(lambda: True)
    assert s._validation['status'] == 'completed', s._validation['error']
    s.offset_factor = .1  # Settings changed after the scan: the scan's own metadata decides.
    x, label = s._plot_axis(np.asarray([10., 20., 30.]))
    expected = mscan_rig.module.mz_from_amplitude([10., 20., 30.], frequency_hz=5e5, r0_mm=5., factor=.2, minimum=.5)
    assert label == 'm/z (Th)' and x == pytest.approx(expected)
    s.mz_axis = s.AXIS_AMPLITUDE
    assert s._plot_axis(np.asarray([10.]))[1] == s._axis_label()
    path = tmp_path / 'scan.h5'
    s.saveData(path)
    with h5py.File(path) as f:
        assert f['MScan/Validation/mz'][:] == pytest.approx(expected)
        definition = json.loads(f['MScan/Validation/mz'].attrs['Definition'])
        assert (definition['frequency_hz'], definition['r0_mm'], definition['factor']) == (5e5, 5., .2)
        assert 'centre of the transmitted band' in f['MScan/Validation/mz'].attrs['Unit']
    assert all(c.writes for c in two)


@pytest.mark.parametrize(('frequency', 'amplitudes'), [
    (500e3, [62.0, 92.0, 151.9, 212.0]), (450.45e3, [50.2, 74.6, 123.3, 170.6]), (400e3, [None, 58.7, 96.8, 135.0]),
])
def test_the_default_calibration_puts_the_tuning_mix_on_its_masses(mscan_module, frequency, amplitudes):
    # 2026-11-03, k = 0.21: the M03 peaks of the Agilent G2421A tuning mix at three RF frequencies.
    M = mscan_module.MScan
    for amplitude, expected in zip(amplitudes, (622.03, 922.01, 1521.97, 2121.93)):
        if amplitude is None:
            continue
        shown = mscan_module.mz_from_amplitude([amplitude], frequency_hz=frequency, r0_mm=M.DEFAULT_R0_MM, factor=.21,
                                               attenuation=M.DEFAULT_RF_ATTENUATION)[0]
        assert shown == pytest.approx(expected, rel=6e-3)


def test_the_rod_amplitude_sets_both_the_scale_and_the_filter_line(mscan_module):
    mz = mscan_module.mz_from_amplitude
    plain = mz([100.], frequency_hz=5e5, r0_mm=5., factor=.2)[0]
    attenuated = mz([100.], frequency_hz=5e5, r0_mm=5., factor=.2, attenuation=.978)[0]
    # The rods see 97.8 V: a lighter ion, and U/V = 0.2045 moves the band centre up.
    assert attenuated < .978 * plain
    assert np.isnan(mz([100.], frequency_hz=5e5, r0_mm=5., factor=.211, attenuation=.978)).all()
    assert np.isfinite(mz([100.], frequency_hz=5e5, r0_mm=5., factor=.211)).all()
