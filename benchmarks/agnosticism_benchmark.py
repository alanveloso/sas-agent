#!/usr/bin/env python3
"""Architecture + multi-profile agnosticism benchmark.

Experiment A — Architectural isolation (fixed profile via JSON):
  monolithic vs. core (cold_start + grant_hot)

Experiment B — Profile agnosticism (8 profiles, all via BAND_PROFILE_FILE):
  cold_start + grant_hot on the core

Emits raw/aggregated/summary CSVs and a JSON manifest (no prose report).
"""

from __future__ import annotations

import csv
import json
import math
import os
import platform
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
PROFILES_DIR = SANDBOX / "profiles"
DATA = SANDBOX / "data"
LOGS = SANDBOX / "logs" / "agnosticism"
WORKER = SANDBOX / "phase_worker.py"
PYTHON = sys.executable

SEED = 42
N = 30
WARMUP = 3
TIMEOUT_SEC = 120

# All profiles loaded via JSON (same I/O path) for fair comparison.
PROFILE_FILES: dict[str, Path] = {
    "cbrs_winnforum": PROFILES_DIR / "cbrs_winnforum.json",
    "br_anatel_privativa": PROFILES_DIR / "br_anatel_privativa.json",
    "lab_custom_3p8": PROFILES_DIR / "lab_custom_3p8.json",
    "synth_narrow_5mhz": PROFILES_DIR / "synth_narrow_5mhz.json",
    "synth_wide_20mhz": PROFILES_DIR / "synth_wide_20mhz.json",
    "synth_protections_all_on": PROFILES_DIR / "synth_protections_all_on.json",
    "synth_protections_all_off": PROFILES_DIR / "synth_protections_all_off.json",
    "synth_short_timers": PROFILES_DIR / "synth_short_timers.json",
}

# Fixed profile for the architecture experiment (JSON path).
ARCH_PROFILE_ID = "cbrs_winnforum"

RAW_CSV = DATA / "agnosticism_runs_raw.csv"
AGG_CSV = DATA / "agnosticism_efficiency.csv"
SUMMARY_CSV = DATA / "agnosticism_summary.csv"
MANIFEST = DATA / "agnosticism_manifest.json"

RAW_FIELDS = [
    "run_id",
    "order",
    "timestamp_utc",
    "experiment",
    "comparison_scenario",
    "profile",
    "profile_file",
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
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1))
    return ordered[k]


def _cv(values: list[float]) -> float:
    """Coefficient of variation (%)."""
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return (statistics.pstdev(values) / mean) * 100.0


def _build_scenarios() -> list[dict[str, str]]:
    scenarios: list[dict[str, str]] = []
    arch_file = str(PROFILE_FILES[ARCH_PROFILE_ID])

    # Experiment A — architectural isolation
    for instance, phase, worker in (
        ("monolithic", "cold_start", "cold_start_monolithic"),
        ("core", "cold_start", "cold_start_core"),
        ("monolithic", "grant_hot", "grant_monolithic"),
        ("core", "grant_hot", "grant_core"),
    ):
        scenarios.append(
            {
                "experiment": "A_architecture",
                "comparison_scenario": "architectural_isolation",
                "profile": ARCH_PROFILE_ID,
                "profile_file": arch_file,
                "instance_type": instance,
                "phase": phase,
                "worker_phase": worker,
            }
        )

    # Experiment B — agnosticism (core × 8 profiles)
    for profile_id, path in PROFILE_FILES.items():
        for phase, worker in (
            ("cold_start", "cold_start_core"),
            ("grant_hot", "grant_core"),
        ):
            scenarios.append(
                {
                    "experiment": "B_agnosticism",
                    "comparison_scenario": "profile_agnosticism",
                    "profile": profile_id,
                    "profile_file": str(path),
                    "instance_type": "core",
                    "phase": phase,
                    "worker_phase": worker,
                }
            )
    return scenarios


def _run_once(scenario: dict[str, str], order: int, warmup: bool) -> dict:
    run_id = uuid.uuid4().hex[:12]
    fd, db_path = tempfile.mkstemp(
        prefix=f"agn_{run_id}_", suffix=".db", dir=str(SANDBOX)
    )
    os.close(fd)
    Path(db_path).unlink(missing_ok=True)

    env = os.environ.copy()
    env["SAS_DATABASE_PATH"] = db_path
    env["BAND_PROFILE_FILE"] = scenario["profile_file"]
    env.pop("BAND_PROFILE", None)
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
        "--profile-file",
        scenario["profile_file"],
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
            f"ENV BAND_PROFILE_FILE={scenario['profile_file']} "
            f"SAS_DATABASE_PATH={db_path}\n"
            f"RC={proc.returncode}\n--- STDOUT ---\n{proc.stdout}\n"
            f"--- STDERR ---\n{proc.stderr}\n",
            encoding="utf-8",
        )
        payload: dict = {}
        for line in reversed((proc.stdout or "").splitlines()):
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
                "error": f"no JSON; stderr={((proc.stderr or '')[:800])}",
            }
        exit_code = int(payload.get("exit_code", proc.returncode))
        error = str(payload.get("error", ""))
    except subprocess.TimeoutExpired as exc:
        log_path.write_text(f"TIMEOUT {TIMEOUT_SEC}s\n{exc}", encoding="utf-8")
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
        "experiment": scenario["experiment"],
        "comparison_scenario": scenario["comparison_scenario"],
        "profile": scenario["profile"],
        "profile_file": scenario["profile_file"],
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
        if row["warmup"] == "true" or int(row["exit_code"]) != 0:
            continue
        key = (
            row["experiment"],
            row["comparison_scenario"],
            row["profile"],
            row["instance_type"],
            row["phase"],
        )
        groups.setdefault(key, []).append(row)

    out: list[dict] = []
    for (exp, scenario, profile, instance, phase), rows in sorted(groups.items()):
        lats = [float(r["latency_ms"]) for r in rows]
        idles = [float(r["memory_idle_mb"]) for r in rows]
        peaks = [float(r["memory_peak_mb"]) for r in rows]
        out.append(
            {
                "experiment": exp,
                "comparison_scenario": scenario,
                "profile": profile,
                "instance_type": instance,
                "phase": phase,
                "latency_mean_ms": round(statistics.fmean(lats), 4),
                "latency_p95_ms": round(_p95(lats), 4),
                "latency_stdev_ms": round(statistics.pstdev(lats), 4)
                if len(lats) > 1
                else 0.0,
                "memory_idle_mb": round(statistics.fmean(idles), 4),
                "memory_peak_mb": round(statistics.fmean(peaks), 4),
                "n_samples": len(rows),
            }
        )
    return out


def _agnosticism_summary(agg: list[dict]) -> list[dict]:
    """Cross-profile CV for core cold_start and grant_hot."""
    out: list[dict] = []
    for phase in ("cold_start", "grant_hot"):
        rows = [
            r
            for r in agg
            if r["experiment"] == "B_agnosticism"
            and r["phase"] == phase
            and r["instance_type"] == "core"
        ]
        if not rows:
            continue
        lats = [float(r["latency_mean_ms"]) for r in rows]
        peaks = [float(r["memory_peak_mb"]) for r in rows]
        out.append(
            {
                "phase": phase,
                "n_profiles": len(rows),
                "latency_mean_across_profiles_ms": round(statistics.fmean(lats), 4),
                "latency_min_ms": round(min(lats), 4),
                "latency_max_ms": round(max(lats), 4),
                "latency_cv_pct": round(_cv(lats), 4),
                "memory_peak_mean_mb": round(statistics.fmean(peaks), 4),
                "memory_peak_cv_pct": round(_cv(peaks), 4),
                "profiles": ",".join(sorted(r["profile"] for r in rows)),
            }
        )
    return out


def _env_snapshot(scenarios: int, n_exec: int, failures: int) -> dict:
    info: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "seed": SEED,
        "n": N,
        "warmup": WARMUP,
        "n_scenarios": scenarios,
        "n_runs": n_exec,
        "non_warmup_failures": failures,
        "profile_load_path": "BAND_PROFILE_FILE (JSON) for all profiles",
        "profiles": {k: str(v) for k, v in PROFILE_FILES.items()},
        "confounders_controlled": [
            "same load path for all profiles (JSON file)",
            "fresh SQLite per repetition",
            "subprocess isolation (no profile cache)",
            "grant warmup before hot measurement",
            "channel indices relative to active band",
            "shuffled schedule with fixed seed",
        ],
    }
    try:
        cpu = subprocess.run(
            ["lscpu"], capture_output=True, text=True, check=False, timeout=10
        )
        for line in cpu.stdout.splitlines():
            if "Model name" in line or "Nome do modelo" in line:
                info["cpu_model"] = line.split(":", 1)[-1].strip()
                break
    except Exception:  # noqa: BLE001
        pass
    try:
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")
        info["meminfo_total"] = next(
            (ln for ln in mem.splitlines() if ln.startswith("MemTotal:")), ""
        )
    except Exception:  # noqa: BLE001
        pass
    return info


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    missing = [k for k, p in PROFILE_FILES.items() if not p.is_file()]
    if missing:
        raise SystemExit(f"Missing profile files: {missing}")

    scenarios = _build_scenarios()
    rng = random.Random(SEED)
    schedule: list[tuple[dict[str, str], bool]] = []
    for scenario in scenarios:
        for _ in range(WARMUP):
            schedule.append((scenario, True))
        for _ in range(N):
            schedule.append((scenario, False))
    rng.shuffle(schedule)

    raw_rows: list[dict] = []
    with RAW_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_FIELDS)
        writer.writeheader()
        for order, (scenario, warmup) in enumerate(schedule, start=1):
            print(
                f"[{order}/{len(schedule)}] {scenario['experiment']} "
                f"{scenario['instance_type']} {scenario['phase']} "
                f"{scenario['profile']} warmup={warmup}",
                flush=True,
            )
            row = _run_once(scenario, order=order, warmup=warmup)
            writer.writerow(row)
            fh.flush()
            raw_rows.append(row)

    agg = _aggregate(raw_rows)
    with AGG_CSV.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "experiment",
                "comparison_scenario",
                "profile",
                "instance_type",
                "phase",
                "latency_mean_ms",
                "latency_p95_ms",
                "latency_stdev_ms",
                "memory_idle_mb",
                "memory_peak_mb",
                "n_samples",
            ],
        )
        writer.writeheader()
        writer.writerows(agg)

    summary = _agnosticism_summary(agg)
    with SUMMARY_CSV.open("w", newline="", encoding="utf-8") as fh:
        if summary:
            writer = csv.DictWriter(fh, fieldnames=list(summary[0].keys()))
            writer.writeheader()
            writer.writerows(summary)

    failures = sum(
        1 for r in raw_rows if r["warmup"] == "false" and int(r["exit_code"]) != 0
    )
    env = _env_snapshot(len(scenarios), len(raw_rows), failures)
    MANIFEST.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {RAW_CSV} ({len(raw_rows)} rows)")
    print(f"Wrote {AGG_CSV} ({len(agg)} groups)")
    print(f"Wrote {SUMMARY_CSV}")
    print(f"Wrote {MANIFEST}")
    print(f"Non-warmup failures: {failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
