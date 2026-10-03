"""Temperature stability qualification only; no device I/O or temperature control."""
from collections import deque
import math
from threading import RLock


def _finite(value):
    try:
        return not isinstance(value, bool) and math.isfinite(value)
    except (TypeError, ValueError):
        return False


class TemperatureStability:
    """Require an uninterrupted sample window, small target error and small OLS drift.

    Timestamps describe returned observations, not ADC conversion times. Keep the
    sample just before the window boundary so the evidence spans at least the
    full window; no interpolation or extrapolation manufactures an observation.
    """
    def __init__(self, *, window_s=60.0, tolerance_c=0.2, drift_c_min=0.1, max_gap_s=2.0):
        values = (window_s, tolerance_c, drift_c_min, max_gap_s)
        if any(not _finite(x) or x <= 0 for x in values):
            raise ValueError('Stability parameters must be finite and positive')
        self.window_s, self.tolerance_c, self.drift_c_min, self.max_gap_s = map(float, values)
        self._lock = RLock()
        self.reset()

    def reset(self, state='Unavailable'):
        with self._lock:
            self._samples = deque()
            self._target = None
            self._state = state

    def update(self, timestamp, temperature, target, *, active=True, valid=True):
        with self._lock:
            if active is False:
                self.reset('OFF')
                return self.status(timestamp)
            if (active is not True or valid is not True
                    or any(not _finite(x) for x in (timestamp, temperature, target)) or target <= 0):
                self.reset()
                return self.status(timestamp)
            if self._samples and timestamp <= self._samples[-1][0]:
                self.reset()
                return self.status(timestamp)
            if (self._target != target or (self._samples and
                    timestamp - self._samples[-1][0] > self.max_gap_s)):
                self.reset('Stabilizing')
            self._target = float(target)
            self._state = 'Stabilizing'
            self._samples.append((float(timestamp), float(temperature)))
            while len(self._samples) > 2 and self._samples[1][0] <= timestamp - self.window_s:
                self._samples.popleft()
            return self.status(timestamp)

    def status(self, now):
        with self._lock:
            result = dict(state=self._state, stable=False, span_s=0.0, slope_c_min=None,
                          max_error_c=None, target_c=self._target, samples=len(self._samples), expires_at_s=None)
            if not self._samples:
                return result
            if (not _finite(now) or now < self._samples[-1][0]
                    or now - self._samples[-1][0] > self.max_gap_s):
                self.reset()
                return self.status(now)
            result['expires_at_s'] = self._samples[-1][0] + self.max_gap_s
            origin = self._samples[0][0]
            xs = [t-origin for t, _ in self._samples]
            ys = [v for _, v in self._samples]
            result['span_s'] = xs[-1]
            result['max_error_c'] = max(abs(v-self._target) for v in ys)
            if len(xs) >= 3:
                x_mean, y_mean = sum(xs)/len(xs), sum(ys)/len(ys)
                denominator = sum((x-x_mean)**2 for x in xs)
                if denominator > 0:
                    result['slope_c_min'] = 60*sum((x-x_mean)*(y-y_mean) for x, y in zip(xs, ys))/denominator
            result['stable'] = bool(result['span_s'] >= self.window_s
                and all(self._target-self.tolerance_c <= v <= self._target+self.tolerance_c for v in ys)
                and result['slope_c_min'] is not None
                and abs(result['slope_c_min']) < self.drift_c_min)
            if result['stable']:
                result['state'] = 'Stable'
            return result
