"""Per-second time-series helpers."""
from __future__ import annotations

import numpy as np
from scipy.ndimage import gaussian_filter1d, median_filter


def robust_z(x: np.ndarray, mask: np.ndarray | None = None, lo: float = -2.0, hi: float = 8.0) -> np.ndarray:
    """Median/MAD z-score (robust to the sparse spikes we care about), clipped."""
    x = np.asarray(x, dtype=np.float64)
    ref = x[mask] if mask is not None and mask.any() else x
    if ref.size == 0:
        return np.zeros_like(x)
    med = np.median(ref)
    mad = np.median(np.abs(ref - med)) * 1.4826
    scale = mad if mad > 1e-9 else (ref.std() if ref.std() > 1e-9 else 1.0)
    z = np.clip((x - med) / scale, lo, hi)
    if mask is not None:
        z = np.where(mask, z, 0.0)
    return z


def smooth(x: np.ndarray, sigma: float) -> np.ndarray:
    return gaussian_filter1d(np.asarray(x, dtype=np.float64), sigma) if sigma > 0 and len(x) > 1 else x


def rolling_baseline(x: np.ndarray, window: int) -> np.ndarray:
    window = max(3, min(int(window), max(3, len(x) // 2 * 2 - 1)))
    if window % 2 == 0:
        window += 1
    return median_filter(np.asarray(x, dtype=np.float64), size=window, mode="nearest")


def bin_events(times: list[float] | np.ndarray, n: int, weights=None) -> np.ndarray:
    t = np.asarray(times, dtype=np.float64)
    out = np.zeros(n)
    if t.size == 0:
        return out
    idx = np.floor(t).astype(int)
    ok = (idx >= 0) & (idx < n)
    w = np.ones_like(t) if weights is None else np.asarray(weights, dtype=np.float64)
    np.add.at(out, idx[ok], w[ok])
    return out
