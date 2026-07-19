#!/usr/bin/env python3
"""Performance-focused benchmark: cold start + hot grant only.

Comparison 1: monolithic vs. core (CBRS profile).
Comparison 2: cbrs_winnforum vs. br_anatel_privativa (core) — profile-agnostic check.

Emits raw/aggregated CSVs and a JSON manifest (no prose report).
"""

from __future__ import annotations

import csv
import json
import platform
import random
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SANDBOX = Path(__file__).resolve().parent
ROOT = SANDBOX.parent
if str(SANDBOX) not in sys.path:
    sys.path.insert(0, str(SANDBOX))

from dimension_b_efficiency import (  # noqa: E402
    DATA,
    LOGS,
    N,
    RAW_FIELDS,
    SEED,
    WARMUP,
    _aggregate,
    _run_once,
)

RAW_CSV = DATA / "performance_focus_runs_raw.csv"
AGG_CSV = DATA / "performance_focus_efficiency.csv"
MANIFEST = DATA / "performance_focus_manifest.json"

SCENARIOS: list[dict[str, str]] = [
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
        # Agnosticism check: same core, two band packages. The goal is not to
        # measure a profile's "weight", but to confirm the core's performance
        # does not depend on the active regulatory profile.
        "comparison_scenario": "profile_agnosticism",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "cold_start",
        "worker_phase": "cold_start_core",
    },
    {
        "comparison_scenario": "profile_agnosticism",
        "profile": "br_anatel_privativa",
        "instance_type": "core",
        "phase": "cold_start",
        "worker_phase": "cold_start_core",
    },
    {
        "comparison_scenario": "profile_agnosticism",
        "profile": "cbrs_winnforum",
        "instance_type": "core",
        "phase": "grant_hot",
        "worker_phase": "grant_core",
    },
    {
        "comparison_scenario": "profile_agnosticism",
        "profile": "br_anatel_privativa",
        "instance_type": "core",
        "phase": "grant_hot",
        "worker_phase": "grant_core",
    },
]


def _env_snapshot() -> dict:
    info: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "seed": SEED,
        "n": N,
        "warmup": WARMUP,
        "scenarios": len(SCENARIOS),
        "focus": "cold_start + grant_hot; no CBRS terms, Radon, CPAS or coverage",
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
            (ln for ln in mem.splitlines() if ln.startswith("MemTotal:")),
            "",
        )
    except Exception:  # noqa: BLE001
        pass
    return info


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

    raw_rows: list[dict] = []
    with RAW_CSV.open("w", newline="", encoding="utf-8") as fh:
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
    with AGG_CSV.open("w", newline="", encoding="utf-8") as fh:
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

    env = _env_snapshot()
    failures = sum(
        1 for r in raw_rows if r["warmup"] == "false" and int(r["exit_code"]) != 0
    )
    env["n_runs"] = len(raw_rows)
    env["n_groups"] = len(agg_rows)
    env["non_warmup_failures"] = failures
    MANIFEST.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote {RAW_CSV} ({len(raw_rows)} rows)")
    print(f"Wrote {AGG_CSV} ({len(agg_rows)} groups)")
    print(f"Wrote {MANIFEST}")
    print(f"Non-warmup failures: {failures}")
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
