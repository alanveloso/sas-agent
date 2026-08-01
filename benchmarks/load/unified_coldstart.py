"""Unified Docker cold-start protocol across all BandProfiles.

Definition:
  t0  = immediately before ``docker run -d``
  tend = first mTLS HTTP 200 on GET /ready

Campaign: 30 runs × each profile JSON in benchmarks/profiles/, randomized
order per block, container destroyed after each run, same image.

Intermediate wall segments (best-effort from docker logs):
  docker_create_ms, uvicorn_seen_ms, ready_ms (total = ready wall).

Outputs: unified_coldstart_{runs,summary,manifest}.csv/json
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
PROFILES = BENCH_DIR / "profiles"
DATA = BENCH_DIR / "data"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

from common import (  # noqa: E402
    DEFAULT_SEED,
    DockerRunSpec,
    build_mtls_context,
    capture_manifest,
    docker_rm,
    docker_run,
    image_digest,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_http_200,
    wait_thermal_gate,
    write_manifest,
)

RUN_FIELDS = [
    "run_id", "timestamp_utc", "repeat", "order_in_block", "profile_id",
    "image", "host_port",
    # External additive triad
    "cold_start_total_ms",
    "docker_run_to_container_started_ms",
    "internal_total_ms",
    "runtime_and_observation_gap_ms",
    "readiness_polling_ms",
    "ready_poll_count",
    "uvicorn_seen_ms",
    # Internal additive stack (do NOT include diag_bind_to_ready_ms)
    "python_bootstrap_ms",
    "profile_load_ms",
    "database_init_ms",
    "tls_init_ms",
    "internal_other_ms",
    # Nested diagnostic only
    "diag_bind_to_ready_ms",
    "startup_json",
    "temp_tctl_c_start", "exit_code", "error",
]
SUMMARY_FIELDS = [
    "profile_id", "n_ok", "n_fail", "metric", "median", "mean", "p95", "iqr", "min", "max",
]
# Stages that must be ≥0 and participate in internal accounting residual
INTERNAL_ADDITIVE_FIELDS = (
    "python_bootstrap_ms",
    "profile_load_ms",
    "database_init_ms",
    "tls_init_ms",
    "internal_other_ms",
)



def _resolve_certs(cli: Path | None) -> Path:
    if cli is not None:
        return cli.expanduser().resolve()
    env = os.environ.get("HARNESS_CERTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    candidate = ROOT.parent / "src" / "harness" / "certs"
    if candidate.is_dir():
        return candidate
    return (ROOT / "certs").resolve()


def _docker_logs_timestamps(name: str) -> str:
    proc = subprocess.run(
        ["docker", "logs", "--timestamps", name],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return (proc.stdout or "") + (proc.stderr or "")


def _uvicorn_seen_ms_from_logs(logs: str, t0_unix: float) -> float | None:
    """Parse first uvicorn/startup log line with docker --timestamps."""
    markers = (
        "Application startup complete",
        "Uvicorn running",
        "RSA mTLS listener starting",
    )
    for line in logs.splitlines():
        if not any(m in line for m in markers):
            continue
        # Format: 2026-07-23T22:00:00.123456789Z message
        ts = line.split(" ", 1)[0].strip()
        if not ts:
            continue
        try:
            from datetime import datetime, timezone

            if ts.endswith("Z"):
                ts = ts[:-1] + "+00:00"
            # Truncate ns to us for fromisoformat
            if "." in ts:
                head, rest = ts.split(".", 1)
                frac = "".join(c for c in rest if c.isdigit())[:6].ljust(6, "0")
                tz = rest[len("".join(c for c in rest if c.isdigit())) :]
                if not tz.startswith("+") and not tz.startswith("-"):
                    tz = "+00:00"
                ts = f"{head}.{frac}{tz}"
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt.timestamp() - t0_unix) * 1000.0)
        except ValueError:
            continue
    return None


def _iqr(vals: list[float]) -> float:
    return (percentile(vals, 75) or 0.0) - (percentile(vals, 25) or 0.0)


def _nonneg(v: Any) -> float | str:
    if v in ("", None):
        return ""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return ""
    return round(f, 4) if f >= 0 else ""


def _extract_stages(startup: dict[str, Any]) -> dict[str, Any]:
    """Map /ready.startup payload → non-negative stage columns."""
    stages = dict(startup.get("stages_ms") or {})
    intervals = dict(startup.get("intervals_ms") or {})
    marks = (
        startup.get("milestones_ms_from_python_entry")
        or startup.get("milestones_ms_from_process_start")
        or {}
    )
    bootstrap = _nonneg(
        stages.get("python_bootstrap_ms", marks.get("imports_done"))
    )
    profile = _nonneg(stages.get("profile_load_ms", intervals.get("profile_load")))
    database = _nonneg(stages.get("database_init_ms", intervals.get("database_init")))
    tls = _nonneg(stages.get("tls_init_ms", intervals.get("tls_init")))
    internal = _nonneg(stages.get("internal_total_ms", marks.get("first_ready")))
    other = stages.get("internal_other_ms")
    if other in ("", None) and internal != "" and all(
        x != "" for x in (bootstrap, profile, database, tls)
    ):
        other = round(
            max(0.0, float(internal) - float(bootstrap) - float(profile)
                - float(database) - float(tls)),
            4,
        )
    else:
        other = _nonneg(other)
    return {
        "python_bootstrap_ms": bootstrap,
        "profile_load_ms": profile,
        "database_init_ms": database,
        "tls_init_ms": tls,
        "internal_other_ms": other,
        "internal_total_ms": internal,
        "diag_bind_to_ready_ms": _nonneg(
            stages.get("diag_bind_to_ready_ms", stages.get("bind_to_ready_ms"))
        ),
    }


def _run_once(
    *,
    profile_path: Path,
    profile_id: str,
    image: str,
    host_port: int,
    certs_dir: Path,
    ctx: Any,
    timeout_sec: float,
    repeat: int,
    order: int,
    temp_start: float | None,
) -> dict[str, Any]:
    name = f"sas-ucold-{profile_id[:20]}-{repeat}-{order}"
    run_id = new_run_id()
    error = ""
    exit_code = 0
    docker_create_ms = float("nan")
    uvicorn_seen_ms = ""
    ready_ms = float("nan")
    polls = 0
    seg = {
        "python_bootstrap_ms": "",
        "profile_load_ms": "",
        "database_init_ms": "",
        "tls_init_ms": "",
        "internal_other_ms": "",
        "internal_total_ms": "",
        "diag_bind_to_ready_ms": "",
        "runtime_and_observation_gap_ms": "",
        "startup_json": "",
    }

    spec = DockerRunSpec(
        image=image,
        name=name,
        host_port=host_port,
        certs_dir=certs_dir,
        band_profile_file=profile_path,
        database_path=f"/data/sas_{run_id}.db",
        extra_env={"SAS_TIMING": "1"},
    )
    ready_url = f"https://127.0.0.1:{host_port}/ready"

    t0 = time.perf_counter()
    t0_unix = time.time()
    try:
        docker_run(spec)
        docker_create_ms = (time.perf_counter() - t0) * 1000.0
        ok, polls, poll_err = wait_http_200(ready_url, ctx, timeout_sec)
        ready_ms = (time.perf_counter() - t0) * 1000.0
        if not ok:
            exit_code = 1
            error = poll_err or "ready timeout"
        else:
            seen = _uvicorn_seen_ms_from_logs(_docker_logs_timestamps(name), t0_unix)
            if seen is not None:
                uvicorn_seen_ms = round(seen, 3)
            try:
                import httpx

                with httpx.Client(verify=ctx, timeout=5.0) as client:
                    body = client.get(ready_url).json()
                startup = body.get("startup") or {}
                seg["startup_json"] = json.dumps(startup, separators=(",", ":"))[:4000]
                seg.update(_extract_stages(startup))
                # Residual between host wall and attributed pieces.
                # Includes container runtime before Python, import before first
                # mark, transport, and poll granularity — never hidden.
                internal = seg.get("internal_total_ms")
                if (
                    internal != ""
                    and docker_create_ms == docker_create_ms
                    and ready_ms == ready_ms
                ):
                    gap = ready_ms - float(docker_create_ms) - float(internal)
                    seg["runtime_and_observation_gap_ms"] = round(max(0.0, gap), 3)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        ready_ms = (time.perf_counter() - t0) * 1000.0
        exit_code = 1
        error = str(exc)[:300]
    finally:
        docker_rm(name)

    # Fail the row if any additive internal stage is negative.
    for k in INTERNAL_ADDITIVE_FIELDS:
        v = seg.get(k)
        if v not in ("", None) and float(v) < 0:
            exit_code = 1
            error = f"negative stage {k}={v}"

    return {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "repeat": repeat,
        "order_in_block": order,
        "profile_id": profile_id,
        "image": image,
        "host_port": host_port,
        "cold_start_total_ms": round(ready_ms, 3),
        "docker_run_to_container_started_ms": (
            round(docker_create_ms, 3) if docker_create_ms == docker_create_ms else ""
        ),
        "readiness_polling_ms": round(
            max(0.0, ready_ms - (docker_create_ms if docker_create_ms == docker_create_ms else 0.0)),
            3,
        ),
        "ready_poll_count": polls,
        "uvicorn_seen_ms": uvicorn_seen_ms,
        **seg,
        "temp_tctl_c_start": round(temp_start, 2) if temp_start is not None else "",
        "exit_code": exit_code,
        "error": error,
    }


def _summarize(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_p: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        by_p.setdefault(r["profile_id"], []).append(r)
    out: list[dict[str, Any]] = []
    for pid, rows in sorted(by_p.items()):
        ok = [r for r in rows if r["exit_code"] == 0]
        vals = [float(r["cold_start_total_ms"]) for r in ok]
        n_fail = len(rows) - len(ok)
        if not vals:
            out.append({
                "profile_id": pid, "n_ok": 0, "n_fail": n_fail, "metric": "cold_start_total_ms",
                "median": "", "mean": "", "p95": "", "iqr": "", "min": "", "max": "",
            })
            continue
        out.append({
            "profile_id": pid,
            "n_ok": len(vals),
            "n_fail": n_fail,
            "metric": "cold_start_total_ms",
            "median": round(float(statistics.median(vals)), 3),
            "mean": round(statistics.fmean(vals), 3),
            "p95": round(percentile(vals, 95) or 0.0, 3),
            "iqr": round(_iqr(vals), 3),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--port", type=int, default=19100)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--profiles-dir", type=Path, default=PROFILES)
    p.add_argument("--timeout-sec", type=float, default=90.0)
    p.add_argument("--cooldown-sec", type=float, default=3.0)
    p.add_argument("--max-tctl-c", type=float, default=65.0)
    p.add_argument("--gate-timeout-sec", type=float, default=600.0)
    p.add_argument("--skip-thermal-gate", action="store_true")
    p.add_argument("--out-dir", type=Path, default=DATA)
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs)
    profiles = sorted(args.profiles_dir.glob("*.json"))
    if not profiles:
        raise SystemExit(f"no profiles in {args.profiles_dir}")

    if not args.skip_thermal_gate:
        wait_thermal_gate(
            max_tctl_c=args.max_tctl_c,
            out_path=out_dir / "thermal_gate_unified_coldstart.json",
            label="unified_coldstart",
            timeout_sec=args.gate_timeout_sec,
        )

    runs_path = out_dir / "unified_coldstart_runs.csv"
    summary_path = out_dir / "unified_coldstart_summary.csv"
    manifest_path = out_dir / "unified_coldstart_manifest.json"

    all_runs: list[dict[str, Any]] = []
    failures = 0
    with runs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUN_FIELDS)
        writer.writeheader()
        for repeat in range(1, args.n + 1):
            order = list(profiles)
            random.Random(args.seed + repeat).shuffle(order)
            print(f"=== coldstart block {repeat}/{args.n} ===", flush=True)
            from common import host_load_snapshot

            temp = host_load_snapshot().get("temperature_tctl_c")
            if temp is None:
                temp = host_load_snapshot().get("temperature_max_c")
            for idx, path in enumerate(order, start=1):
                pdata = json.loads(path.read_text(encoding="utf-8"))
                pid = pdata["profile_id"]
                row = _run_once(
                    profile_path=path,
                    profile_id=pid,
                    image=args.image,
                    host_port=args.port,
                    certs_dir=certs,
                    ctx=ctx,
                    timeout_sec=args.timeout_sec,
                    repeat=repeat,
                    order=idx,
                    temp_start=float(temp) if temp is not None else None,
                )
                writer.writerow(row)
                fh.flush()
                all_runs.append(row)
                status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
                print(f"  {pid}: {row['cold_start_total_ms']:.1f}ms {status}", flush=True)
                if row["exit_code"] != 0:
                    failures += 1
                if args.cooldown_sec > 0:
                    time.sleep(args.cooldown_sec)

    summary = _summarize(all_runs)
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)

    write_manifest(
        manifest_path,
        capture_manifest(
            seed=args.seed,
            n=args.n,
            warmup=0,
            script_paths=[LOAD_DIR / "unified_coldstart.py", LOAD_DIR / "common.py"],
            certs_dir=certs,
            extra={
                "experiment": "unified_coldstart",
                "definition": {
                    "external_additive": (
                        "cold_start_total = docker_run_to_container_started "
                        "+ internal_total + runtime_and_observation_gap"
                    ),
                    "internal_additive": (
                        "internal_total ≈ bootstrap + profile + database + tls "
                        "+ internal_other"
                    ),
                    "diag_bind_to_ready_ms": (
                        "nested server_bind→first_ready; NOT stacked with "
                        "internal additive stages"
                    ),
                    "dual_listener_aggregation": "max(RSA, ECDSA) per stage interval",
                    "no_negative_rule": "rows with negative additive stages → exit_code=1",
                },
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "profiles": [p.name for p in profiles],
                "failures": failures,
                "outputs": {"runs": str(runs_path.name), "summary": str(summary_path.name)},
            },
        ),
    )
    print(f"Wrote {runs_path}\nWrote {summary_path}\nWrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
