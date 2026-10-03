"""MScan validates DMMR history incrementally, with the same verdicts as a full scan."""
from __future__ import annotations

from types import SimpleNamespace as NS

import numpy as np
import pytest

from test_mscan import module  # noqa: F401  (pytest fixture)


def _owner():
    return NS()


def _check(module, owner, device, times):
    module.MScan._require_monotonic(owner, device, np.asarray(times, dtype=float), 'bad timestamps')


def test_last_finite_times_matches_a_full_mask(module):
    rng = np.random.default_rng(3)
    times = np.cumsum(rng.uniform(.01, .2, 5000))
    values = rng.normal(size=5000)
    values[rng.random(5000) < .4] = np.nan
    for since in (-np.inf, times[0], times[2500], times[-30], times[-1]):
        for count in (1, 21, 400):
            expected = times[np.isfinite(values) & (times > since)][-count:]
            actual = module.MScan._last_finite_times(times, values, since, count)
            np.testing.assert_array_equal(actual, expected)


def test_only_new_samples_are_rescanned(module, monkeypatch):
    owner, device = _owner(), NS(time=object())
    history = list(np.arange(200_000, dtype=float))
    _check(module, owner, device, history)
    scanned = []
    real_diff = np.diff
    monkeypatch.setattr(module.np, 'diff', lambda a, *args, **kw: scanned.append(len(a)) or real_diff(a, *args, **kw))
    history += [200_000., 200_001.]
    _check(module, owner, device, history)
    assert scanned == [3], 'the validated prefix must not be rescanned on every poll'


def test_new_nonmonotonic_or_nonfinite_samples_are_rejected(module):
    for bad in (5., np.nan, np.inf):
        owner, device = _owner(), NS(time=object())
        history = [1., 2., 6.]
        _check(module, owner, device, history)
        with pytest.raises(module.ScanError, match='bad timestamps'):
            _check(module, owner, device, history + [bad])


def test_rewritten_history_is_fully_revalidated(module):
    owner, device = _owner(), NS(time=object())
    _check(module, owner, device, [1., 2., 3., 4., 5.])
    # Thinning/clearing rewrites the array: its first sample changes, so a bad
    # value anywhere in the rewritten prefix is still found.
    with pytest.raises(module.ScanError):
        _check(module, owner, device, [2., 4., 3., 5., 6.])
    # A cleared history starts a new prefix.
    _check(module, owner, device, [10., 11.])
    with pytest.raises(module.ScanError):
        _check(module, owner, device, [10., 11., 11.])
