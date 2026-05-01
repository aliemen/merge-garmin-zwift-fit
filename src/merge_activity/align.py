from datetime import datetime

import numpy as np


def _series_on_grid(records, field, t0, n_samples):
    """Build a 1 Hz array of `field` values, indexed from absolute time `t0`.

    Samples outside the records' actual time range are filled with the series
    mean so they don't bias the cross-correlation.
    """
    times, vals = [], []
    for r in records:
        v = r.get(field)
        if v is None:
            continue
        times.append((r["timestamp"] - t0).total_seconds())
        vals.append(float(v))
    if len(times) < 30:
        return None
    times = np.asarray(times, dtype=float)
    vals = np.asarray(vals, dtype=float)
    grid = np.arange(n_samples, dtype=float)
    inside = (grid >= times[0]) & (grid <= times[-1])
    series = np.full(n_samples, vals.mean())
    series[inside] = np.interp(grid[inside], times, vals)
    return series


def _correlate_with_lag_window(g, z, max_lag_s):
    """Return (best_lag_seconds, normalized_peak) within ±max_lag_s.

    A positive lag means Zwift's signal best matches Garmin's signal when
    Zwift timestamps are pushed FORWARD (later) by `lag` seconds.
    """
    g = g - g.mean()
    z = z - z.mean()
    norm = float(np.linalg.norm(g) * np.linalg.norm(z))
    if norm == 0:
        return 0, 0.0
    lags = np.arange(-max_lag_s, max_lag_s + 1)
    corrs = np.empty(len(lags), dtype=float)
    for i, lag in enumerate(lags):
        if lag >= 0:
            a, b = g[lag:], z[: len(z) - lag]
        else:
            a, b = g[: len(g) + lag], z[-lag:]
        corrs[i] = float(np.dot(a, b))
    peak = int(np.argmax(corrs))
    return int(lags[peak]), corrs[peak] / norm


def estimate_offset(garmin_records, zwift_records, max_lag_s=120, min_confidence=0.3):
    """Cross-correlate cadence (HR fallback) to estimate the Zwift→Garmin offset.

    Returns (offset_seconds, confidence, source) where `offset_seconds` should
    be ADDED to Zwift timestamps to align them with the Garmin clock. If both
    signals are too weak to correlate, returns (0.0, 0.0, None).
    """
    if not garmin_records or not zwift_records:
        return 0.0, 0.0, None
    t0 = min(garmin_records[0]["timestamp"], zwift_records[0]["timestamp"])
    t1 = max(garmin_records[-1]["timestamp"], zwift_records[-1]["timestamp"])
    n = int((t1 - t0).total_seconds()) + 1

    for field in ("cadence", "heart_rate"):
        g = _series_on_grid(garmin_records, field, t0, n)
        z = _series_on_grid(zwift_records, field, t0, n)
        if g is None or z is None:
            continue
        lag, conf = _correlate_with_lag_window(g, z, max_lag_s)
        if conf >= min_confidence:
            return float(lag), float(conf), field
    return 0.0, 0.0, None
