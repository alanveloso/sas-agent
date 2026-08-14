#!/usr/bin/env python3
"""Benchmark campaign runner.

Writes under ``artifacts/raw/<campaign_id>/`` and a master manifest in
``artifacts/manifests/``. WInnForum stays out (separate harness).

Usage:
  export HARNESS_CERTS_DIR=/path/to/harness/certs
  .venv/bin/python benchmarks/run_campaign.py --smoke
  .venv/bin/python benchmarks/run_campaign.py
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "benchmarks"))
sys.path.insert(0, str(ROOT / "benchmarks" / "load"))

from benchkit.campaign_io import (  # noqa: E402
    campaign_manifest_path,
    new_campaign_id,
    write_json,
)
from benchkit import capture_manifest, git_snapshot  # noqa: E402

PY = sys.executable
BENCH = Path(__file__).resolve().parent

EXPERIMENT_DIRS = {
    "lifecycle": "lifecycle_decomposition",
    "mtls": "mtls_correctness_overhead",
    "persistence": "persistence_decomposition",
    "cold_start": "instrumented_cold_start",
    "boundary": "boundary_enforcement",
    "isolation": "agent_isolation",
    "factorial": "factorial_provisioning",
    "concurrent_process": "concurrent_slicing_v2",
    "concurrent_docker": "container_concurrency",
    "profile_robustness": "band_profile_robustness",
}


def _run(label: str, cmd: list[str], timeout: float) -> dict:
    print(f"\n######## {label} ########", flush=True)
    print(" ".join(cmd), flush=True)
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, timeout=timeout)
    return {
        "label": label,
        "cmd": cmd,
        "exit_code": proc.returncode,
        "wall_sec": round(time.perf_counter() - t0, 2),
    }


def _experiment_entry(campaign: str, label: str, exit_code: int) -> dict:
    rel = EXPERIMENT_DIRS.get(label)
    if not rel:
        return {"status": "success" if exit_code == 0 else "failed"}
    base = Path("artifacts") / "raw" / campaign / rel
    entry: dict = {
        "status": "success" if exit_code == 0 else "failed",
        "dir": str(base.as_posix()),
    }
    # Relative artifact names when present
    mapping = {
        "lifecycle": ("lifecycle_decomp_raw.csv", "lifecycle_decomp_summary.csv", "lifecycle_decomp_manifest.json"),
        "mtls": ("cert_matrix_results.csv", "tls_mtls_overhead_summary.csv", "cert_matrix_manifest.json"),
        "persistence": ("persistence_modes_runs.csv", "persistence_modes_summary.csv", "persistence_modes_manifest.json"),
        "cold_start": ("unified_coldstart_runs.csv", "unified_coldstart_summary.csv", "unified_coldstart_manifest.json"),
        "boundary": ("boundary_enforcement.csv", "boundary_enforcement.csv", "boundary_enforcement_manifest.json"),
        "isolation": ("agent_isolation_results.csv", "agent_isolation_results.csv", "agent_isolation_manifest.json"),
        "factorial": ("factorial_provisioning_runs.csv", "factorial_provisioning_summary.csv", "factorial_provisioning_manifest.json"),
        "concurrent_process": ("concurrent_slicing_v2_runs.csv", "concurrent_slicing_v2_summary.csv", "concurrent_slicing_v2_manifest.json"),
        "concurrent_docker": ("container_concurrency_runs.csv", "container_concurrency_summary.csv", "container_concurrency_manifest.json"),
        "profile_robustness": ("band_profile_robustness.csv", "band_profile_robustness.csv", "band_profile_robustness.csv"),
    }
    names = mapping.get(label)
    if names:
        raw, summary, manifest = names
        entry["raw"] = f"{rel}/{raw}"
        entry["summary"] = f"{rel}/{summary}"
        entry["manifest"] = f"{rel}/{manifest}"
        if label == "mtls":
            entry["requests"] = f"{rel}/tls_mtls_requests.csv"
    return entry


def _exp_dir(campaign: str, name: str) -> Path:
    path = ROOT / "artifacts" / "raw" / campaign / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--campaign-id", default=None)
    p.add_argument("--smoke", action="store_true")
    p.add_argument("--with-postgres", action="store_true")
    p.add_argument("--image", default="sas-agent:article")
    p.add_argument("--skip-lifecycle", action="store_true")
    p.add_argument("--skip-mtls", action="store_true")
    p.add_argument("--skip-persist", action="store_true")
    p.add_argument("--skip-cold", action="store_true")
    p.add_argument("--skip-boundary", action="store_true")
    p.add_argument("--skip-isolation", action="store_true")
    p.add_argument("--skip-factorial", action="store_true")
    p.add_argument("--skip-concurrent", action="store_true")
    p.add_argument("--skip-docker", action="store_true")
    p.add_argument("--skip-profile-robustness", action="store_true")
    p.add_argument("--skip-analyze", action="store_true")
    p.add_argument("--allow-dirty", action="store_true")
    args = p.parse_args()

    git = git_snapshot()
    if git.get("dirty") and not args.allow_dirty and not args.smoke:
        warnings.warn(
            "git working tree is DIRTY — official runs should use a clean "
            "commit (pass --allow-dirty to override).",
            stacklevel=1,
        )
        print(
            "ERROR: refuse official campaign on dirty tree "
            "(use --smoke or --allow-dirty).",
            file=sys.stderr,
        )
        return 2

    campaign = args.campaign_id or new_campaign_id(
        "eval-smoke" if args.smoke else "eval"
    )
    smoke = ["--smoke"] if args.smoke else []
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    steps: list[tuple[str, list[str], float]] = []

    if not args.skip_lifecycle:
        steps.append((
            "lifecycle",
            [PY, str(BENCH / "lifecycle" / "run.py"), "--campaign-id", campaign, *smoke],
            600 if args.smoke else 7200,
        ))
    if not args.skip_mtls:
        steps.append((
            "mtls",
            [PY, str(BENCH / "mtls" / "run.py"), "--campaign-id", campaign, *smoke],
            900 if args.smoke else 3600,
        ))
    if not args.skip_persist:
        pers = [
            PY, str(BENCH / "persistence" / "run.py"),
            "--campaign-id", campaign, *smoke,
        ]
        if args.with_postgres:
            pers.append("--with-postgres")
        steps.append(("persistence", pers, 600 if args.smoke else 3600))
    if not args.skip_cold:
        steps.append((
            "cold_start",
            [PY, str(BENCH / "cold_start" / "run.py"), "--campaign-id", campaign, *smoke],
            600 if args.smoke else 10800,
        ))

    if not args.skip_boundary:
        out = _exp_dir(campaign, "boundary_enforcement")
        steps.append((
            "boundary",
            [PY, str(BENCH / "load" / "boundary_enforcement_bench.py"), "--out-dir", str(out)],
            900,
        ))
    if not args.skip_isolation:
        out = _exp_dir(campaign, "agent_isolation")
        steps.append((
            "isolation",
            [PY, str(BENCH / "load" / "agent_isolation_bench.py"), "--out-dir", str(out)],
            300,
        ))
    if not args.skip_factorial:
        out = _exp_dir(campaign, "factorial_provisioning")
        fact = [
            PY, str(BENCH / "load" / "factorial_provisioning_bench.py"),
            "--out-dir", str(out),
        ]
        if args.smoke:
            fact += ["--repeats", "1", "--probe-n", "3", "--cooldown-sec", "0.2"]
        else:
            fact += ["--repeats", "5", "--probe-n", "10", "--cooldown-sec", "0.5"]
        steps.append(("factorial", fact, 600 if args.smoke else 3600))
    if not args.skip_concurrent:
        out = _exp_dir(campaign, "concurrent_slicing_v2")
        conc = [
            PY, str(BENCH / "metrics_sandbox" / "concurrent_slicing_bench.py"),
            "--v2-prefix", "--out-dir", str(out),
        ]
        if args.smoke:
            conc += ["--repeats", "1", "--k-values", "1", "3", "--devices", "60", "--cooldown-sec", "0.2"]
        else:
            conc += ["--repeats", "5", "--k-values", "1", "2", "3", "5", "10", "15",
                     "--devices", "1500", "--cooldown-sec", "2"]
        steps.append(("concurrent_process", conc, 300 if args.smoke else 7200))
    if not args.skip_docker:
        out = _exp_dir(campaign, "container_concurrency")
        certs = os.environ.get(
            "HARNESS_CERTS_DIR",
            str(ROOT.parent / "Spectrum-Access-System" / "src" / "harness" / "certs"),
        )
        dock = [
            PY, str(BENCH / "load" / "container_concurrency_bench.py"),
            "--image", args.image,
            "--certs-dir", certs,
            "--skip-thermal-gate",
            "--out-dir", str(out),
        ]
        if args.smoke:
            dock += ["--repeats", "1", "--k-values", "1", "2", "--devices", "60",
                     "--cooldown-sec", "1", "--scenarios", "A"]
        else:
            dock += ["--repeats", "5", "--k-values", "1", "2", "3", "5", "10", "15",
                     "--devices", "1500", "--cooldown-sec", "5", "--scenarios", "A", "B"]
        steps.append(("concurrent_docker", dock, 600 if args.smoke else 14400))
    if not args.skip_profile_robustness:
        steps.append((
            "profile_robustness",
            [PY, "-m", "unittest", "tests.test_band_profile_robustness", "-v"],
            120,
        ))

    if not args.skip_analyze:
        steps.append((
            "analyze",
            [PY, str(ROOT / "analysis" / "generate_tables.py"), "--campaign-id", campaign],
            120,
        ))

    results = []
    failures = 0
    fail_labels: list[str] = []
    for label, cmd, timeout in steps:
        try:
            info = _run(label, cmd, timeout)
        except subprocess.TimeoutExpired:
            info = {"label": label, "cmd": cmd, "exit_code": 124, "wall_sec": timeout}
        results.append(info)
        if label == "profile_robustness":
            src = ROOT / "benchmarks" / "data" / "band_profile_robustness.csv"
            dest = _exp_dir(campaign, "band_profile_robustness") / "band_profile_robustness.csv"
            if src.is_file():
                shutil.copy2(src, dest)
        if info["exit_code"] != 0:
            failures += 1
            fail_labels.append(label)

    completed = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    experiments = {
        info["label"]: _experiment_entry(campaign, info["label"], info["exit_code"])
        for info in results
        if info["label"] in EXPERIMENT_DIRS
    }

    campaign_type = "evaluation-smoke" if args.smoke else "evaluation"
    scope = list(EXPERIMENT_DIRS.values())

    master = {
        "schema_version": "1.0",
        "campaign_id": campaign,
        "campaign_type": campaign_type,
        "git_commit": git.get("commit"),
        "git_dirty": bool(git.get("dirty")),
        "git_dirty_file_count": git.get("dirty_file_count"),
        "started_at_utc": started,
        "completed_at_utc": completed,
        "experiments": experiments,
        "failures": fail_labels,
        "steps": results,
        "scope": scope,
        "host_capture": capture_manifest(
            seed=42,
            n=0,
            warmup=0,
            script_paths=[BENCH / "run_campaign.py"],
        ),
    }
    path = campaign_manifest_path(campaign)
    write_json(path, master)
    write_json(
        ROOT / "artifacts" / "raw" / campaign / "campaign_manifest.json",
        {
            "schema_version": "1.0",
            "campaign_id": campaign,
            "campaign_type": master["campaign_type"],
            "git_commit": master["git_commit"],
            "git_dirty": master["git_dirty"],
            "started_at_utc": started,
            "completed_at_utc": completed,
            "experiments": experiments,
            "failures": fail_labels,
            "manifest": f"artifacts/manifests/{campaign}.json",
        },
    )
    print(f"Wrote {path}")

    validation_block: dict = {"status": "skipped"}
    if not args.skip_analyze:
        info = _run(
            "validate",
            [PY, str(ROOT / "analysis" / "validate_campaign.py"), "--campaign-id", campaign],
            60,
        )
        results.append(info)
        master["steps"] = results
        report_path = ROOT / "artifacts" / "raw" / campaign / "validation_report.json"
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding="utf-8"))
            validation_block = {
                "status": report.get("status"),
                "checks_passed": report.get("checks_passed"),
                "checks_failed": report.get("checks_failed"),
                "report": "validation_report.json",
            }
        else:
            validation_block = {
                "status": "failed" if info["exit_code"] else "success",
                "checks_passed": None,
                "checks_failed": None,
                "report": None,
            }
        if info["exit_code"] != 0:
            failures += 1
            if "validate" not in fail_labels:
                fail_labels.append("validate")
        master["failures"] = fail_labels
        master["validation"] = validation_block
        write_json(path, master)
        write_json(
            ROOT / "artifacts" / "raw" / campaign / "campaign_manifest.json",
            {
                "schema_version": "1.0",
                "campaign_id": campaign,
                "campaign_type": master["campaign_type"],
                "git_commit": master["git_commit"],
                "git_dirty": master["git_dirty"],
                "started_at_utc": started,
                "completed_at_utc": completed,
                "experiments": experiments,
                "failures": fail_labels,
                "validation": validation_block,
                "manifest": f"artifacts/manifests/{campaign}.json",
            },
        )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
