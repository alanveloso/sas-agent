"""Small statistics helpers for analysis (no plotting)."""

from __future__ import annotations

import statistics
from typing import Sequence


def fmean(values: Sequence[float]) -> float | None:
    return statistics.fmean(values) if values else None


def median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def iqr(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    qs = statistics.quantiles(values, n=4, method="inclusive")
    return qs[2] - qs[0]


def pct(values: Sequence[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    k = max(1, int(round(p / 100.0 * len(ordered)))) - 1
    return ordered[min(k, len(ordered) - 1)]
