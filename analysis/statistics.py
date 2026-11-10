"""Small statistics helpers for analysis (no plotting).

Confidence intervals for the mean use the Student-t approximation and are only
valid when the listed observations are independent experimental units. p95 is
reported as a point estimate; a CI is refused when the sample cannot sustain one.
"""

from __future__ import annotations

import math
import statistics
from typing import Any, Sequence

# Two-sided 95% critical values for df = 1..30; for larger df use 1.96.
_T_CRIT_95 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
    6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
    11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
    16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
    26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}


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
    """Nearest-rank percentile; ``p`` in [0, 100]."""
    if not values:
        return None
    ordered = sorted(values)
    if p <= 0:
        return ordered[0]
    if p >= 100:
        return ordered[-1]
    rank = max(1, math.ceil(p / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def t_critical_95(df: int) -> float:
    if df < 1:
        raise ValueError("df must be >= 1")
    if df <= 30:
        return _T_CRIT_95[df]
    return 1.96


def mean_ci95(values: Sequence[float]) -> tuple[float | None, float | None, float | None]:
    """Student-t 95% CI for the mean: ``(mean, lo, hi)``. None if ``n < 2``."""
    n = len(values)
    if n < 2:
        return None, None, None
    mean = statistics.fmean(values)
    half = t_critical_95(n - 1) * statistics.stdev(values) / math.sqrt(n)
    return mean, mean - half, mean + half


def p95_report(values: Sequence[float]) -> dict[str, Any]:
    """Point p95 plus an explicit reliability note; no fabricated CI.

    With small ``n``, nearest-rank p95 collapses toward the sample maximum
    (for ``n=5`` the rank is 5). A CI is not returned: order-statistic /
    binomial intervals for the 0.95 quantile are uninformative below roughly
    several dozen independent observations.
    """
    n = len(values)
    if n == 0:
        return {
            "n": 0,
            "p95": None,
            "order_rank": None,
            "ci95_low": None,
            "ci95_high": None,
            "point_estimate_only": True,
            "note": "no samples",
        }
    rank = max(1, math.ceil(0.95 * n))
    note = (
        f"nearest-rank p95 is order statistic {rank} of {n}; "
        "reported as a point estimate only (no CI)"
    )
    if n < 20:
        note += "; with n<20 the estimate is dominated by the extreme order statistics"
    return {
        "n": n,
        "p95": pct(values, 95),
        "order_rank": rank,
        "ci95_low": None,
        "ci95_high": None,
        "point_estimate_only": True,
        "note": note,
    }


# Catalog of article experiments: independent unit vs what summaries currently count.
EXPERIMENT_UNITS: dict[str, dict[str, Any]] = {
    "lifecycle_decomp": {
        "summary_n_counts": "HTTP request / lifecycle cycle within one container",
        "independent_unit": "Docker container (one per band profile)",
        "default_n_within_unit": 30,
        "default_n_independent": 1,
        "pseudoreplication_risk": True,
        "mean_ci_appropriate_on_summary_n": False,
        "p95_on_summary_n": "point estimate of within-container cycles only",
        "figure_dispersion": "boxplot of within-container cycles (IQR/whiskers), not a CI",
    },
    "unified_coldstart": {
        "summary_n_counts": "independent Docker cold start",
        "independent_unit": "Docker container destroyed after each run",
        "default_n_within_unit": 1,
        "default_n_independent": 30,
        "pseudoreplication_risk": False,
        "mean_ci_appropriate_on_summary_n": True,
        "p95_on_summary_n": "point estimate over ~30 independent starts; no CI",
        "figure_dispersion": "boxplot over independent containers (IQR/whiskers)",
    },
    "container_concurrency": {
        "summary_n_counts": "independent K-container deployment + load",
        "independent_unit": "deployment repeat (scenario × K)",
        "default_n_within_unit": 1,
        "default_n_independent": 5,
        "pseudoreplication_risk": False,
        "mean_ci_appropriate_on_summary_n": True,
        "p95_on_summary_n": (
            "grant_batch_latency_p95_ms is a within-run batch p95, then "
            "summarized across repeats; legacy grant_latency_p95_ms is amortized, "
            "not per-Grant"
        ),
        "figure_dispersion": "summary CI95 is for the mean across repeats (n≈5)",
    },
    "tls_mtls_overhead": {
        "summary_n_counts": "randomized block (condition × block)",
        "independent_unit": "block",
        "default_n_within_unit": 100,
        "default_n_independent": 30,
        "pseudoreplication_risk": True,
        "mean_ci_appropriate_on_summary_n": True,
        "note": (
            "within a persistent block, requests share one TLS connection; "
            "block-level medians/p95 are the right inputs to cross-block summaries. "
            "Endpoint is GET /ready (transport), not a CBSD-SAS operation."
        ),
        "p95_on_summary_n": "summary metric latency_p95_ms is the p95 of per-block latency medians/p95s",
        "figure_dispersion": "boxplot; request-level panels are descriptive, not independent n",
    },
    "persistence_modes": {
        "summary_n_counts": "independent child-process population run",
        "independent_unit": "process repeat × mode × population",
        "default_n_independent": 10,
        "pseudoreplication_risk": False,
        "mean_ci_appropriate_on_summary_n": True,
        "p95_on_summary_n": "not primary; median/IQR used",
        "figure_dispersion": (
            "boxplot over repeats; persistence_wall_median bars show medians "
            "without error bars"
        ),
    },
    "factorial_provisioning": {
        "summary_n_counts": "independent child-process cell run",
        "independent_unit": "repeat within factorial cell",
        "default_n_independent": 20,
        "pseudoreplication_risk": False,
        "mean_ci_appropriate_on_summary_n": True,
        "p95_on_summary_n": "probe_grant_p95_ms is within-run probe p95 if present",
        "figure_dispersion": "tables use median/IQR over repeats",
    },
}
