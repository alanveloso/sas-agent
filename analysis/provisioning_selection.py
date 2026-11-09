"""Calibration-driven choice of the number of SAS Agents K (illustrative example).

Reads ``container_concurrency_runs.csv`` files (read-only) and applies:

  Rule: among the K values present in the calibration data, select the smallest
  K for which EVERY calibration repeat satisfies EVERY limit and the persistence
  mode is durable. Ties on K (distinct ``config_id``) are broken by lower
  worst-case memory, then by ``config_id``. If no K qualifies, the result is
  "infeasible" with the violated limits per K; no configuration is forced.

No extrapolation: only calibrated K values and a single total population are
accepted. Validation files are evaluated with the same limits but are never
used for the choice; they must use device seeds disjoint from calibration.

Never starts Docker, the SAS agent, or benchmarks.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

# Illustrative limits, fixed before calibration/validation (not real SLAs).
ILLUSTRATIVE_LIMITS = {
    "startup_sec": 60.0,          # all K agents ready; one cbrs_winnforum heartbeat interval
    "provisioning_sec": 60.0,     # Reg+Grant of the whole population; same anchor
    "grant_batch_p95_ms": 1000.0,  # HTTP round-trip of a Grant batch (≤100 items)
    "memory_mb": 2048.0,          # sum over agents
}
CONSTRAINTS = (
    ("startup_sec", ("create_ready_sec",)),
    ("provisioning_sec", ("load_wall_sec",)),
    ("grant_batch_p95_ms", ("grant_batch_latency_p95_ms",)),
    ("memory_mb", ("ram_after_load_mb_sum", "ram_peak_mb_sum")),
)
DURABLE_PERSISTENCE = frozenset({"sqlite_file_full"})


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _num(value: Any) -> float | None:
    try:
        return float(value) if value not in ("", None) else None
    except (TypeError, ValueError):
        return None


def load_rows(paths: list[Path], scenario: str = "A") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if "grant_batch_latency_p95_ms" not in (reader.fieldnames or []):
                raise ValueError(
                    f"{path}: no grant_batch_latency_p95_ms; legacy grant_latency_p95_ms "
                    "is not a per-Grant p95 and is not accepted"
                )
            rows += [r for r in reader if r.get("scenario", scenario) == scenario]
    return rows


def _population(rows: list[dict[str, Any]]) -> int:
    pops = {int(float(r["total_devices"])) for r in rows}
    if len(pops) != 1:
        raise ValueError(f"mixed total_devices {sorted(pops)}; no extrapolation across loads")
    return pops.pop()


def evaluate(
    rows: list[dict[str, Any]],
    limits: dict[str, float],
    persistence: str = "sqlite_file_full",
) -> list[dict[str, Any]]:
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for r in rows:
        k = int(float(r["k_containers"]))
        groups.setdefault((k, r.get("config_id") or f"k{k}"), []).append(r)

    out = []
    for (k, cid), rs in sorted(groups.items()):
        violations: list[str] = []
        if persistence not in DURABLE_PERSISTENCE:
            violations.append(f"non_durable_persistence:{persistence}")
        failed = sum(
            1 for r in rs
            if str(r.get("exit_code")) not in ("0", "") or (_num(r.get("devices_failed")) or 0) > 0
        )
        if failed:
            violations.append(f"failed_runs:{failed}")
        worst: dict[str, float | None] = {}
        median: dict[str, float | None] = {}
        for name, cols in CONSTRAINTS:
            vals = []
            for r in rs:
                present = [v for v in (_num(r.get(c)) for c in cols) if v is not None and v > 0]
                vals.append(max(present) if present else None)
            if any(v is None for v in vals):
                worst[name] = median[name] = None
                violations.append(f"missing:{name}")
                continue
            worst[name] = max(vals)
            median[name] = _median(vals)
            if worst[name] > limits[name]:
                violations.append(f"{name}:{worst[name]:.4g}>{limits[name]:.4g}")
        out.append({
            "config_id": cid, "k_containers": k, "n_repeats": len(rs),
            "worst": worst, "median": median,
            "feasible": not violations, "violations": violations,
        })
    return out


def select(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    feasible = [c for c in candidates if c["feasible"]]
    if not feasible:
        return None
    return min(feasible, key=lambda c: (c["k_containers"], c["worst"]["memory_mb"], c["config_id"]))


def run(
    calibration: list[Path],
    validation: list[Path],
    limits: dict[str, float],
    reference_k: int,
    persistence: str = "sqlite_file_full",
    scenario: str = "A",
) -> dict[str, Any]:
    cal_rows = load_rows(calibration, scenario)
    population = _population(cal_rows)
    cands = evaluate(cal_rows, limits, persistence)
    chosen = select(cands)
    result: dict[str, Any] = {
        "rule": "smallest calibrated K whose every repeat meets every limit; "
                "ties: lower worst-case memory, then config_id; else infeasible",
        "limits": limits,
        "limits_are_illustrative": True,
        "persistence": persistence,
        "scenario": scenario,
        "total_devices": population,
        "calibration_files": [str(p) for p in calibration],
        "calibration": cands,
        "selected": None if chosen is None else {
            "config_id": chosen["config_id"], "k_containers": chosen["k_containers"],
        },
        "infeasible": chosen is None,
        "reference_k": reference_k,
    }
    if validation:
        val_rows = load_rows(validation, scenario)
        if _population(val_rows) != population:
            raise ValueError("validation total_devices differs from calibration")
        overlap = ({r.get("device_seed") for r in val_rows}
                   & {r.get("device_seed") for r in cal_rows})
        if overlap:
            raise ValueError(f"validation reuses calibration device seeds {sorted(overlap)}")
        wanted = {reference_k} | ({chosen["k_containers"]} if chosen else set())
        result["validation_files"] = [str(p) for p in validation]
        result["validation"] = [
            c for c in evaluate(val_rows, limits, persistence) if c["k_containers"] in wanted
        ]
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--calibration", type=Path, nargs="+", required=True)
    p.add_argument("--validation", type=Path, nargs="*", default=[])
    p.add_argument("--reference-k", type=int, default=15)
    p.add_argument("--persistence", default="sqlite_file_full")
    p.add_argument("--scenario", default="A")
    for key, val in ILLUSTRATIVE_LIMITS.items():
        p.add_argument(f"--limit-{key.replace('_', '-')}", type=float, default=val, dest=key)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args(argv)

    if args.out.exists():
        print(f"refusing to overwrite {args.out}", file=sys.stderr)
        return 2
    limits = {k: getattr(args, k) for k in ILLUSTRATIVE_LIMITS}
    result = run(args.calibration, args.validation, limits, args.reference_k,
                 args.persistence, args.scenario)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")

    for c in result["calibration"]:
        print(f"calib K={c['k_containers']:>3} {c['config_id']:>8} "
              f"{'OK ' if c['feasible'] else 'NO '} {'; '.join(c['violations'])}")
    sel = result["selected"]
    print("selected:", "INFEASIBLE" if sel is None else f"K={sel['k_containers']}")
    for c in result.get("validation", []):
        print(f"valid K={c['k_containers']:>3} {'PASS' if c['feasible'] else 'FAIL'} "
              f"{'; '.join(c['violations'])}")
    print(f"Wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
