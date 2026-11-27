"""Quadrupole physics shared by MScan and Transmission: ideal first-zone 50 % square wave.

Canonical file: mscan/_runtime/_quadrupole.py. tools/sync_family_siblings.py copies it word for word to
transmission/_runtime/_quadrupole.py (``SHARED``); each plugin loads its own copy. Edit only this file.

Per-rod parameters for an ion of mass m and charge e (m/z in Th): a = 8 e U / (m r0² Ω²), q = 4 e V / (m r0² Ω²),
V the RF amplitude on the rods (attenuation × A, A the commanded PSU magnitude). On the scan line a = 2 (U/V) q a
band of q is stable; MScan drives U = max(minimum, |k| A − δ) on the rod pairs. Derivation: docs/mscan_quadrupole.pdf.
SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import math

import numpy as np

E_OVER_U = 1.602176634e-19 / 1.66053906660e-27  # Elementary charge over atomic mass unit (C/kg).
RF_ONLY_Q = .7120  # Largest stable q at a = 0 (RF only): the low-mass cut-off.
APEX_U_OVER_V = 0.215339  # U/V at the apex of the first zone (a, q) = (0.238110, 0.552873).


def square_wave_trace(a, q):
    """Half-trace of the one-period transfer matrix, x direction; stable when |trace| < 2."""
    w1 = np.sqrt((a + 2 * q).astype(complex)) + 1e-16
    w2 = np.sqrt((a - 2 * q).astype(complex)) + 1e-16
    h = np.pi / 2
    return (2 * np.cos(w1 * h) * np.cos(w2 * h) - (w1 / w2 + w2 / w1) * np.sin(w1 * h) * np.sin(w2 * h)).real


_BANDS = {}


def square_wave_band(k):
    """Stable q interval on the scan line a = 2kq (k = U/A); None above the apex (k ≈ 0.2153).

    k = 0 gives (0, 0.712): RF only, a high-pass whose upper q is the low-mass cut-off.
    """
    k = round(abs(float(k)), 5)
    if k not in _BANDS:
        if k == 0:
            _BANDS[k] = (0., RF_ONLY_Q)
        else:
            q = np.linspace(1e-4, .8, 16001)
            a = 2 * k * q
            stable = (np.abs(square_wave_trace(a, q)) < 2) & (np.abs(square_wave_trace(-a, -q)) < 2)
            _BANDS[k] = (float(q[stable][0]), float(q[stable][-1])) if stable.any() else None
    return _BANDS[k]


def mz_from_amplitude(amplitudes, *, frequency_hz, r0_mm, factor=0., resolution=0., minimum=.5, pairs=2,
                      attenuation=1.):
    """m/z (Th) at the centre of the transmitted band for each amplitude A; RF only: the low-mass cut-off.

    U = max(minimum, |factor| × A − resolution), as MScan drives the rod pairs, and the rods see an RF
    amplitude attenuation × A. NaN where nothing is transmitted (above the apex) or where the DC between
    the pairs is unknown (one driven pair).
    """
    amplitudes = np.asarray(amplitudes, dtype=float)
    scale = 4 * E_OVER_U / ((r0_mm * 1e-3) ** 2 * (2 * np.pi * frequency_hz) ** 2)
    result = np.full(amplitudes.shape, np.nan)
    if factor and pairs != 2:
        return result  # Pair 2 keeps its own potential: the filtering DC is not known.
    for i, amplitude in enumerate(amplitudes):
        if not math.isfinite(amplitude) or amplitude <= 0:
            continue
        rods = attenuation * amplitude  # RF amplitude actually on the rods
        k = max(minimum, abs(factor) * amplitude - resolution) / rods if factor else 0.
        band = square_wave_band(k)
        if band is not None:
            q = band[1] if not factor else (band[0] + band[1]) / 2
            result[i] = scale * rods / q
    return result


# ---------------------------------------------------------------------------------------------------- precise band
def _stable(k, q):
    a = np.array([2 * k * q])
    q = np.array([q])
    return bool(abs(square_wave_trace(a, q)[0]) < 2 and abs(square_wave_trace(-a, -q)[0]) < 2)


_PRECISE = {}


def stable_band(k):
    """Stable q interval on a = 2kq with edges refined by bisection (1e-10 in q); None above the apex.

    ``square_wave_band`` rounds k to 1e-5 and reads the edges on a 5e-5 grid, enough for m/z labels; near
    the closing the width of the band is a few grid steps, so resolution and centre need this one.
    """
    k = abs(float(k))
    key = round(k, 10)
    if key not in _PRECISE:
        if k == 0:
            _PRECISE[key] = (0., RF_ONLY_Q)
            return _PRECISE[key]
        q = np.linspace(1e-4, .8, 16001)
        a = 2 * k * q
        stable = (np.abs(square_wave_trace(a, q)) < 2) & (np.abs(square_wave_trace(-a, -q)) < 2)
        if not stable.any():
            _PRECISE[key] = None
            return None
        step = q[1] - q[0]
        low, high = float(q[stable][0]), float(q[stable][-1])

        def edge(inside, outside):
            for _ in range(60):
                middle = (inside + outside) / 2
                if _stable(k, middle):
                    inside = middle
                else:
                    outside = middle
                if abs(inside - outside) < 1e-10:
                    break
            return inside
        _PRECISE[key] = (edge(low, max(low - step, 0.)), edge(high, high + step))
    return _PRECISE[key]


def ideal_resolution(k, attenuation=1.):
    """Ideal R = centre / width of the stable band in amplitude, for U/A = k on rods seeing attenuation × A.

    Infinite transit: the band edges are those of the infinite periodic system. NaN above the apex.
    """
    band = stable_band(abs(k) / attenuation)
    if band is None or band[1] <= band[0]:
        return math.nan
    return (band[0] + band[1]) / 2 / (band[1] - band[0])


def closing_factor(attenuation=1.):
    """The k = U/A at which the band closes (the apex): attenuation × U/V of the apex."""
    return attenuation * APEX_U_OVER_V


def amplitude_from_mz(mz, *, frequency_hz, r0_mm, factor=0., resolution=0., minimum=.5, attenuation=1.):
    """Commanded A whose band centre is ``mz`` (the inverse of ``mz_from_amplitude`` with a filtering DC).

    U = max(minimum, |factor| × A − resolution) depends on A, so the centre is found by fixed-point iteration.
    NaN when no band exists (above the apex) or for RF only (no centre: use ``amplitude_at_q``).
    """
    if not factor or not math.isfinite(mz) or mz <= 0:
        return math.nan
    scale = (r0_mm * 1e-3) ** 2 * (2 * np.pi * frequency_hz) ** 2 / (4 * E_OVER_U * attenuation)
    amplitude = mz * .55 * scale
    for _ in range(40):
        band = stable_band(max(minimum, abs(factor) * amplitude - resolution) / (attenuation * amplitude))
        if band is None:
            return math.nan
        new = mz * (band[0] + band[1]) / 2 * scale
        if abs(new - amplitude) < 1e-9 * amplitude:
            return new
        amplitude = new
    return amplitude


def amplitude_at_q(mz, q, *, frequency_hz, r0_mm, attenuation=1.):
    """Commanded A that puts an ion of ``mz`` at Mathieu q (RF only: q below 0.712 is transmitted)."""
    return mz * q * (r0_mm * 1e-3) ** 2 * (2 * np.pi * frequency_hz) ** 2 / (4 * E_OVER_U * attenuation)


def rf_cycles(mz, *, frequency_hz, energy_ev, length_m):
    """RF periods an ion of ``mz`` (Th) with axial energy ``energy_ev`` (eV per charge) spends over ``length_m``."""
    if energy_ev <= 0:
        return math.nan
    speed = math.sqrt(2 * energy_ev * E_OVER_U / mz)
    return frequency_hz * length_m / speed
