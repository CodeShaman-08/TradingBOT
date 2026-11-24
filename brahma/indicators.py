"""Indicators used by the strategy.

Efficiency notes
----------------
* WMA is computed with ``np.convolve`` (O(n)) instead of pandas
  ``rolling().apply`` (O(n*window) with a Python callback).
* The Kalman layer is exposed both as an exact batch function and as an
  incremental O(1)-per-bar stateful filter. The live engine recomputes the
  strategy only when a *new* candle arrives, so cost is bounded.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd


def _as_array(values) -> np.ndarray:
    if isinstance(values, pd.Series):
        return values.to_numpy(dtype=float)
    return np.asarray(values, dtype=float)


def wma(values, length: int) -> np.ndarray:
    """Linearly weighted moving average, NaN-padded at the front."""
    arr = _as_array(values)
    out = np.full(arr.shape, np.nan, dtype=float)
    if length <= 0 or arr.size < length:
        return out
    if length == 1:
        return arr.copy()
    weights = np.arange(1, length + 1, dtype=float)
    weights /= weights.sum()
    # 'valid' convolution: out[k] uses arr[k .. k+length-1]
    conv = np.convolve(arr, weights[::-1], mode="valid")
    out[length - 1:] = conv
    return out


def hma(values, length: int) -> np.ndarray:
    """Hull Moving Average."""
    half = max(int(length / 2), 1)
    root = max(int(round(math.sqrt(length))), 1)
    return wma(2.0 * wma(values, half) - wma(values, length), root)


def hma3(values, length: int) -> np.ndarray:
    """The 'HMA3' variant used by the original script (length is halved first)."""
    p = length / 2.0
    p3 = max(int(p / 3), 1)
    p2 = max(int(p / 2), 1)
    p_int = max(int(p), 1)
    raw = wma(values, p3) * 3.0 - wma(values, p2) - wma(values, p_int)
    return wma(raw, p_int)


def ema(values, length: int) -> np.ndarray:
    arr = _as_array(values)
    if length <= 1:
        return arr.copy()
    return pd.Series(arr).ewm(span=length, adjust=False).mean().to_numpy(dtype=float)


class KalmanFilter:
    """Exact incremental form of the original 'kahlman' filter.

    State is (previous estimate, velocity). One new bar costs O(1), so the
    live loop never re-walks the whole history.
    """

    __slots__ = ("gain", "root", "previous", "velocity")

    def __init__(self, gain: float) -> None:
        if not 0 < gain < 1:
            raise ValueError("gain must be between 0 and 1")
        self.gain = float(gain)
        self.root = math.sqrt(2.0 * gain)
        self.previous = math.nan
        self.velocity = 0.0

    def reset(self) -> None:
        self.previous = math.nan
        self.velocity = 0.0

    def update(self, value: float) -> float:
        if value is None or not math.isfinite(value):
            return math.nan
        if not math.isfinite(self.previous):
            self.previous = float(value)
            return self.previous
        delta = float(value) - self.previous
        self.velocity += self.gain * delta
        self.previous = self.previous + delta * self.root + self.velocity
        return self.previous


def kalman(values, gain: float) -> np.ndarray:
    """Batch version. Exactly matches a sequential KalmanFilter over the series."""
    arr = _as_array(values)
    out = np.empty(arr.shape, dtype=float)
    g = float(gain)
    root = math.sqrt(2.0 * g)
    previous = math.nan
    velocity = 0.0

    for i in range(arr.size):
        value = arr[i]
        if not math.isfinite(value):
            out[i] = math.nan
            continue
        if not math.isfinite(previous):
            previous = value
            out[i] = value
            continue
        delta = value - previous
        velocity += g * delta
        previous = previous + delta * root + velocity
        out[i] = previous

    return out


def crossover(fast, slow) -> tuple[bool, bool]:
    """Return (cross_up, cross_down) for the last two points of two arrays."""
    if len(fast) < 2 or len(slow) < 2:
        return False, False
    f1, f0 = fast[-1], fast[-2]
    s1, s0 = slow[-1], slow[-2]
    if not all(math.isfinite(float(x)) for x in (f1, f0, s1, s0)):
        return False, False
    return (f1 > s1 and f0 <= s0), (s1 > f1 and s0 <= f0)


def slope(values, bars: int = 1) -> float:
    arr = _as_array(values)
    if bars <= 0 or arr.size <= bars:
        return math.nan
    a, b = arr[-1], arr[-1 - bars]
    if not (math.isfinite(a) and math.isfinite(b)):
        return math.nan
    return float(a - b)
