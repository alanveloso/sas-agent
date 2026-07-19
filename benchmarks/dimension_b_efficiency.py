#!/usr/bin/env python3
"""Dimension B: A/B micro-benchmarks with raw + aggregated data."""

from __future__ import annotations

import csv
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

SANDBOX = Path(__file__).resolve().parent
ROOT = SANDBOX.parent
DATA = SANDBOX / "data"
LOGS = SANDBOX / "logs"
WORKER = SANDBOX / "phase_worker.py"
PYTHON = sys.executable

SEED = 42
N = 30
WARMUP = 3
TIMEOUT_SEC = 120

# (comparison_scenario, profile, instance_type, phase -> worker --phase)
SCENARIOS: list[dict[str, str]] = [
    # Comparison 1 — architectural isolation
    {
        "comparison_scenario": "architectural_isolation",
        "profile": "cbrs_winnforum",
        "instance_type": "monolithic",
        "phase": "cold_start",
        "worker_phase": "cold_start_monolithic",
    },
    {
        "comparison_scenario": "architectural_isolation",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "cold_start",
        "worker_phase": "cold_start_core",
    },
    {
        "comparison_scenario": "architectural_isolation",
        "profile": "cbrs_winnforum",
        "instance_type": "monolithic",
        "phase": "grant_hot",
        "worker_phase": "grant_monolithic",
    },
    {
        "comparison_scenario": "architectural_isolation",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "grant_hot",
        "worker_phase": "grant_core",
    },
    {
        "comparison_scenario": "architectural_isolation",
        "profile": "cbrs_winnforum",
        "instance_type": "monolithic_with_cpas",
        "phase": "cpas_on_demand",
        "worker_phase": "monolithic_with_cpas",
    },
    # Comparison 2 — parameterization weight
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "resolve_profile",
        "worker_phase": "resolve_profile",
    },
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "br_anatel_privativa",
        "instance_type": "core",
        "phase": "resolve_profile",
        "worker_phase": "resolve_profile",
    },
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "cold_start",
        "worker_phase": "cold_start_core",
    },
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "br_anatel_privativa",
        "instance_type": "core",
        "phase": "cold_start",
        "worker_phase": "cold_start_core",
    },
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "grant_hot",
        "worker_phase": "grant_core",
    },
    {
        "comparison_scenario": "parameterization_weight",
        "profile": "br_anatel_privativa",
        "instance_type": "core",
        "phase": "grant_hot",
        "worker_phase": "grant_core",
    },
]

RAW_FIELDS = [
    "run_id",
    "order",
    "timestamp_utc",
    "comparison_scenario",
    "profile",
    "instance_type",
    "phase",
    "latency_ms",
    "memory_initial_mb",
    "memory_idle_mb",
    "memory_peak_mb",
    "exit_code",
    "error",
    "python_version",
    "temp_db",
    "warmup",
]


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[k]


def _run_once(scenario: dict[str, str], order: int, warmup: bool) -> dict:
    run_id = uuid.uuid4().hex[:12]
    fd, db_path = tempfile.mkstemp(prefix=f"bench_{run_id}_", suffix=".db", dir=str(SANDBOX))
    os.close(fd)
    Path(db_path).unlink(missing_ok=True)  # let SQLAlchemy create fresh

    env = os.environ.copy()
    env["SAS_DATABASE_PATH"] = db_path
    env["BAND_PROFILE"] = scenario["profile"]
    env.pop("BAND_PROFILE_FILE", None)
    env.pop("BAND_LOW", None)
    env.pop("BAND_HIGH", None)

    log_path = LOGS / f"{run_id}.log"
    cmd = [
        PYTHON,
        str(WORKER),
        "--phase",
        scenario["worker_phase"],
        "--profile",
        scenario["profile"],
    ]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SEC,
            check=False,
        )
        log_path.write_text(
            f"CMD: {' '.join(cmd)}\n"
            f"ENV BAND_PROFILE={env.get('BAND_PROFILE')} SAS_DATABASE_PATH={db_path}\n"
            f"RC={proc.returncode}\n--- STDOUT ---\n{proc.stdout}\n--- STDERR ---\n{proc.stderr}\n",
            encoding="utf-8",
        )
        payload = {}
        stdout = (proc.stdout or "").strip()
        if stdout:
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    payload = json.loads(line)
                    break
        if not payload:
            payload = {
                "latency_ms": 0.0,
                "memory_initial_mb": 0.0,
                "memory_idle_mb": 0.0,
                "memory_peak_mb": 0.0,
                "exit_code": proc.returncode or 1,
                "error": f"no JSON from worker; stderr={proc.stderr[:800]}",
            }
        exit_code = int(payload.get("exit_code", proc.returncode))
        error = str(payload.get("error", ""))
        if proc.returncode != 0 and not error:
            error = f"worker rc={proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"TIMEOUT after {TIMEOUT_SEC}s\n{exc}", encoding="utf-8")
        payload = {
            "latency_ms": TIMEOUT_SEC * 1000.0,
            "memory_initial_mb": 0.0,
            "memory_idle_mb": 0.0,
            "memory_peak_mb": 0.0,
            "exit_code": 124,
            "error": "timeout",
        }
        exit_code = 124
        error = "timeout"
    except Exception as exc:  # noqa: BLE001
        log_path.write_text(f"PARENT ERROR: {exc}", encoding="utf-8")
        payload = {
            "latency_ms": 0.0,
            "memory_initial_mb": 0.0,
            "memory_idle_mb": 0.0,
            "memory_peak_mb": 0.0,
            "exit_code": 1,
            "error": str(exc),
        }
        exit_code = 1
        error = str(exc)

    if exit_code == 0:
        Path(db_path).unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)

    return {
        "run_id": run_id,
        "order": order,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "comparison_scenario": scenario["comparison_scenario"],
        "profile": scenario["profile"],
        "instance_type": scenario["instance_type"],
        "phase": scenario["phase"],
        "latency_ms": float(payload.get("latency_ms", 0.0)),
        "memory_initial_mb": float(payload.get("memory_initial_mb", 0.0)),
        "memory_idle_mb": float(payload.get("memory_idle_mb", 0.0)),
        "memory_peak_mb": float(payload.get("memory_peak_mb", 0.0)),
        "exit_code": exit_code,
        "error": error.replace("\n", " | ")[:2000],
        "python_version": sys.version.split()[0],
        "temp_db": db_path,
        "warmup": str(warmup).lower(),
    }


def _aggregate(raw_rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in raw_rows:
        if row["warmup"] == "true":
            continue
        if int(row["exit_code"]) != 0:
            continue
        key = (
            row["comparison_scenario"],
            row["profile"],
            row["instance_type"],
            row["phase"],
        )
        groups.setdefault(key, []).append(row)

    out: list[dict] = []
    for (scenario, profile, instance, phase), rows in sorted(groups.items()):
        lats = [float(r["latency_ms"]) for r in rows]
        idles = [float(r["memory_idle_mb"]) for r in rows]
        peaks = [float(r["memory_peak_mb"]) for r in rows]
        out.append(
            {
                "comparison_scenario": scenario,
                "profile": profile,
                "instance_type": instance,
                "phase": phase,
                "latency_mean_ms": round(statistics.fmean(lats), 4) if lats else 0.0,
                "latency_p95_ms": round(_p95(lats), 4) if lats else 0.0,
                "memory_idle_mb": round(statistics.fmean(idles), 4) if idles else 0.0,
                "memory_peak_mb": round(statistics.fmean(peaks), 4) if peaks else 0.0,
                "n_samples": len(rows),
            }
        )
    return out


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    schedule: list[tuple[dict[str, str], bool]] = []
    for scenario in SCENARIOS:
        for _ in range(WARMUP):
            schedule.append((scenario, True))
        for _ in range(N):
            schedule.append((scenario, False))

    rng.shuffle(schedule)

    raw_path = DATA / "dimension_b_runs_raw.csv"
    agg_path = DATA / "dimension_b_efficiency.csv"

    raw_rows: list[dict] = []
    with raw_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_FIELDS)
        writer.writeheader()
        for order, (scenario, warmup) in enumerate(schedule, start=1):
            print(
                f"[{order}/{len(schedule)}] {scenario['comparison_scenario']} "
                f"{scenario['instance_type']} {scenario['phase']} "
                f"{scenario['profile']} warmup={warmup}",
                flush=True,
            )
            row = _run_once(scenario, order=order, warmup=warmup)
            writer.writerow(row)
            fh.flush()
            raw_rows.append(row)

    agg_rows = _aggregate(raw_rows)
    with agg_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "comparison_scenario",
                "profile",
                "instance_type",
                "phase",
                "latency_mean_ms",
                "latency_p95_ms",
                "memory_idle_mb",
                "memory_peak_mb",
                "n_samples",
            ],
        )
        writer.writeheader()
        writer.writerows(agg_rows)

    failures = sum(1 for r in raw_rows if r["warmup"] == "false" and int(r["exit_code"]) != 0)
    print(f"Wrote {raw_path} ({len(raw_rows)} rows)")
    print(f"Wrote {agg_path} ({len(agg_rows)} groups)")
    print(f"Non-warmup failures: {failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
