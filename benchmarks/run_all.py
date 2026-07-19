#!/usr/bin/env python3
"""Orchestrate Dimensions A/B/C and write the execution manifest (data only)."""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SANDBOX = Path(__file__).resolve().parent
ROOT = SANDBOX.parent
DATA = SANDBOX / "data"
PYTHON = sys.executable

SCRIPTS = [
    SANDBOX / "dimension_a_quality.py",
    SANDBOX / "dimension_b_efficiency.py",
    SANDBOX / "dimension_c_coverage.py",
    SANDBOX / "phase_worker.py",
    SANDBOX / "run_all.py",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _capture_env() -> dict:
    info: dict = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor() or "",
        "machine": platform.machine(),
        "node": platform.node(),
    }
    try:
        cpu = subprocess.run(
            ["lscpu"], capture_output=True, text=True, check=False, timeout=10
        )
        info["lscpu"] = cpu.stdout
        for line in cpu.stdout.splitlines():
            if "Model name" in line or "Nome do modelo" in line:
                info["cpu_model"] = line.split(":", 1)[-1].strip()
            if line.strip().startswith("CPU(s):") or "CPU(s)" == line.split(":")[0].strip():
                if "cpu_count" not in info:
                    info["cpu_count"] = line.split(":", 1)[-1].strip()
    except Exception as exc:  # noqa: BLE001
        info["lscpu_error"] = str(exc)

    try:
        mem = Path("/proc/meminfo").read_text(encoding="utf-8")
        info["meminfo_total"] = next(
            (ln for ln in mem.splitlines() if ln.startswith("MemTotal:")),
            "",
        )
    except Exception as exc:  # noqa: BLE001
        info["meminfo_error"] = str(exc)

    try:
        import importlib.metadata as md

        pkgs = {}
        for name in (
            "radon",
            "memory-profiler",
            "pytest",
            "pytest-cov",
            "psutil",
            "coverage",
            "fastapi",
            "sqlalchemy",
        ):
            try:
                pkgs[name] = md.version(name)
            except md.PackageNotFoundError:
                pkgs[name] = None
        info["packages"] = pkgs
    except Exception as exc:  # noqa: BLE001
        info["packages_error"] = str(exc)

    info["script_hashes"] = {p.name: _sha256(p) for p in SCRIPTS if p.is_file()}
    info["seed"] = 42
    info["n"] = 30
    info["warmup"] = 3
    info["cpas_policy"] = (
        "Monolithic cold start without CPAS (does not start on boot). "
        "Extra scenario monolithic_with_cpas triggers trigger_daily_activities on demand."
    )
    return info


def _run_script(path: Path) -> int:
    print(f"\n=== Running {path.name} ===", flush=True)
    proc = subprocess.run([PYTHON, str(path)], cwd=str(ROOT), check=False)
    return proc.returncode


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    env = _capture_env()
    commands = []
    rc_all = 0

    for script in (
        SANDBOX / "dimension_a_quality.py",
        SANDBOX / "dimension_b_efficiency.py",
        SANDBOX / "dimension_c_coverage.py",
    ):
        rc = _run_script(script)
        commands.append({"script": script.name, "returncode": rc})
        if rc != 0:
            rc_all = rc

    env["commands"] = commands
    manifest = DATA / "execution_manifest.json"
    manifest.write_text(json.dumps(env, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {manifest}")
    return rc_all


if __name__ == "__main__":
    raise SystemExit(main())
