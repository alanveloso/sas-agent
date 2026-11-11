"""Read-only uncertainty audit for article experiments (R1.7 / R2.5 / R2.6).

Writes JSON only. Never starts Docker or benchmarks. Does not invent CIs when
the experimental unit or sample size cannot sustain them.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from analysis.statistics import (  # noqa: E402
    EXPERIMENT_UNITS,
    mean_ci95,
    median,
    p95_report,
)

RAW = REPO / "artifacts" / "raw"


def _nums(rows: list[dict[str, str]], key: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        v = r.get(key, "")
        if v in ("", None):
            continue
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            continue
    return out


def _read(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _assess_series(values: list[float], *, independent: bool) -> dict[str, Any]:
    mean, lo, hi = mean_ci95(values) if independent else (None, None, None)
    p95 = p95_report(values)
    return {
        "n": len(values),
        "mean": mean,
        "mean_ci95": None if lo is None else {"low": lo, "high": hi},
        "mean_ci_refused": not independent,
        "mean_ci_refusal_reason": None if independent else (
            "observations are not independent experimental units"
        ),
        "p95": p95,
    }


def _lifecycle_raw_paths(campaign_dir: Path) -> list[Path]:
    flat = campaign_dir / "lifecycle_decomposition" / "lifecycle_decomp_raw.csv"
    if flat.is_file():
        return [flat]
    nested = sorted((campaign_dir / "lifecycle_decomposition").rglob("lifecycle_decomp_raw.csv"))
    if nested:
        return nested
    alt = campaign_dir / "lifecycle_decomp_raw.csv"
    return [alt] if alt.is_file() else []


def audit_lifecycle(campaign_dir: Path) -> dict[str, Any]:
    meta = dict(EXPERIMENT_UNITS["lifecycle_decomp"])
    paths = _lifecycle_raw_paths(campaign_dir)
    # Group rows by independent deployment directory when multi-rep layout is used.
    deployments: list[tuple[str, str, list[dict[str, str]]]] = []
    for path in paths:
        rows = [
            r for r in _read(path)
            if str(r.get("warmup")) in ("0", "False", "false", "")
        ]
        if not rows:
            continue
        profile = rows[0].get("profile_id") or path.parent.parent.name
        # flat layout → single deployment; nested .../<profile>/repk/ → rep id
        if path.parent.name.startswith("rep"):
            dep_id = path.parent.name
        elif path.parent.name == "lifecycle_decomposition":
            dep_id = "rep1"
        else:
            dep_id = path.parent.name
        deployments.append((str(profile), str(dep_id), rows))

    by_profile: dict[str, Any] = {}
    for profile, dep_id, rows in deployments:
        entry = by_profile.setdefault(
            profile,
            {
                "n_independent_deployments": 0,
                "deployments": {},
                "deployment_median_grant_client_ms": [],
                "deployment_median_e2e_client_ms": [],
            },
        )
        grant = _nums(
            [r for r in rows if r.get("operation") == "grant"],
            "client_ms",
        )
        # E2E proxy: sum of the six SAS operations' client_ms per cycle, then
        # median across cycles — used only descriptively inside a deployment.
        by_cycle: dict[str, float] = {}
        for r in rows:
            if r.get("operation") not in (
                "registration", "spectrumInquiry", "grant",
                "heartbeat", "relinquishment", "deregistration",
            ):
                continue
            try:
                by_cycle[r["cycle"]] = by_cycle.get(r["cycle"], 0.0) + float(r["client_ms"])
            except (KeyError, TypeError, ValueError):
                continue
        e2e = list(by_cycle.values())
        g_med = median(grant)
        e_med = median(e2e)
        entry["n_independent_deployments"] += 1
        entry["deployments"][dep_id] = {
            "n_cycles_observed": len(by_cycle),
            "grant_client_ms": _assess_series(grant, independent=False),
            "e2e_client_ms_within_deployment": _assess_series(e2e, independent=False),
            "grant_median_ms": g_med,
            "e2e_median_ms": e_med,
        }
        if g_med is not None:
            entry["deployment_median_grant_client_ms"].append(g_med)
        if e_med is not None:
            entry["deployment_median_e2e_client_ms"].append(e_med)

    for profile, entry in by_profile.items():
        dep_grants = entry.pop("deployment_median_grant_client_ms")
        dep_e2e = entry.pop("deployment_median_e2e_client_ms")
        entry["across_deployments"] = {
            "grant_median_ms": _assess_series(dep_grants, independent=True),
            "e2e_median_ms": _assess_series(dep_e2e, independent=True),
            "note": (
                "CI95 applies to the mean of deployment medians "
                f"(n={len(dep_grants)} independent containers), not to pooled cycles"
            ),
        }

    n_dep = sum(v["n_independent_deployments"] for v in by_profile.values())
    return {
        "experiment": "lifecycle_decomp",
        "catalog": meta,
        "n_raw_files": len(paths),
        "n_deployments_total": n_dep,
        "profiles": by_profile,
        "recommendation": (
            "Treat within-deployment cycle summaries as descriptive. Inferential "
            "profile claims must use the ≥5 independent deployment medians per "
            "profile — never pool 150 cycles as if they were independent replicas."
        ),
    }


def audit_coldstart(campaign_dir: Path) -> dict[str, Any]:
    meta = dict(EXPERIMENT_UNITS["unified_coldstart"])
    runs = _read(campaign_dir / "instrumented_cold_start" / "unified_coldstart_runs.csv")
    if not runs:
        runs = _read(campaign_dir / "unified_coldstart_runs.csv")
    ok = [r for r in runs if str(r.get("exit_code")) in ("0", "")]
    vals = _nums(ok, "cold_start_total_ms")
    # Flag pre-P0-04 decompositions if first_ready overwrite markers are absent
    # from the schema (legacy files lack rsa_last_failed_probe_error).
    legacy_decomp = bool(ok) and "rsa_last_failed_probe_error" not in ok[0]
    return {
        "experiment": "unified_coldstart",
        "catalog": meta,
        "cold_start_total_ms": _assess_series(vals, independent=True),
        "legacy_first_ready_risk": legacy_decomp,
        "legacy_note": (
            "CSV lacks post-P0-04 readiness columns; do not reuse historical "
            "internal decompositions without the P0-04 correction"
            if legacy_decomp else None
        ),
        "recommendation": (
            "Mean CI across independent containers is appropriate. Keep p95 as a "
            "point estimate unless n is substantially increased."
        ),
    }


def audit_concurrency(campaign_dir: Path) -> dict[str, Any]:
    meta = dict(EXPERIMENT_UNITS["container_concurrency"])
    runs = _read(campaign_dir / "container_concurrency" / "container_concurrency_runs.csv")
    if not runs:
        runs = _read(campaign_dir / "container_concurrency_runs.csv")
    groups: dict[str, list[dict[str, str]]] = {}
    for r in runs:
        if str(r.get("exit_code")) not in ("0", ""):
            continue
        key = f"K={r.get('k_containers')}|mode={r.get('population_mode', 'fixed_total')}"
        groups.setdefault(key, []).append(r)
    by_k: dict[str, Any] = {}
    for key, rows in sorted(groups.items()):
        load = _nums(rows, "load_wall_sec")
        batch_p95 = _nums(rows, "grant_batch_latency_p95_ms")
        by_k[key] = {
            "n_repeats": len(rows),
            "load_wall_sec": _assess_series(load, independent=True),
            "grant_batch_latency_p95_ms_across_repeats": _assess_series(
                batch_p95, independent=True
            ),
            "note": (
                "CI on grant_batch_latency_p95_ms is a CI for the mean of "
                "within-run batch p95s across repeats — not a CI for a per-Grant p95. "
                "Do not use legacy grant_latency_p95_ms."
            ),
        }
    return {
        "experiment": "container_concurrency",
        "catalog": meta,
        "groups": by_k,
        "k_selection_sensitivity": {
            "available": False,
            "reason": (
                "No calibration campaign with fixed population and measured "
                "memory/latency under the P1-09 limits is present in artifacts. "
                "Sensitivity of selected K cannot be assessed from current data."
            ),
            "minimal_collection": (
                "Run container_concurrency with --devices 1500, K∈{1,2,3,5,10,15}, "
                "5 repeats (calibration), then 5 validation repeats for selected K "
                "and reference K=15 with a disjoint seed (see P1-09)."
            ),
        },
        "recommendation": (
            "With n_repeats=5, mean CIs are wide; do not treat run-level p95 as a "
            "stable tail proof. Prefer median/IQR of load_wall_sec and "
            "grant_batch_latency_p95_ms across repeats."
        ),
    }


def build_report(campaign_id: str | None) -> dict[str, Any]:
    campaign_dir = RAW / campaign_id if campaign_id else None
    report: dict[str, Any] = {
        "campaign_id": campaign_id,
        "catalog": EXPERIMENT_UNITS,
        "figure_conventions": {
            "boxplots": (
                "IQR box, whiskers and fliers; not a confidence interval. "
                "The plotted sample is whatever unit the CSV row represents."
            ),
            "persistence_wall_median": (
                "Grouped bars of medians only — no error bars / no CI."
            ),
            "no_errorbar_ci_figures": True,
        },
        "prior_corrections": {
            "P0-02": "grant_latency_p95_ms is amortized, not per-Grant p95",
            "P0-04": "pre-fix cold-start decompositions are not reusable as-is",
            "P1-07": "fixed_total vs fixed_per_agent must not be pooled",
            "P1-09": "illustrative limits must not be tuned after seeing calibration",
        },
        "audits": {},
    }
    if campaign_dir and campaign_dir.is_dir():
        report["audits"]["lifecycle_decomp"] = audit_lifecycle(campaign_dir)
        report["audits"]["unified_coldstart"] = audit_coldstart(campaign_dir)
        report["audits"]["container_concurrency"] = audit_concurrency(campaign_dir)
    else:
        report["audits"] = {
            "note": "no campaign CSV loaded; catalog-only report",
        }
    return report


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", default=None)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)
    if args.out.exists():
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    report = build_report(args.campaign_id)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
