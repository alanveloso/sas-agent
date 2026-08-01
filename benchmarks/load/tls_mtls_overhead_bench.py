"""Overhead isolado: TLS vs mTLS × nova conexão vs persistente.

Quatro condições, blocos randomizados:
  - tls_new / tls_persistent / mtls_new / mtls_persistent

Endpoint: GET /ready (custo de transporte, sem lógica CBSD).

Saídas:
  - tls_mtls_overhead_runs.csv      (agregado por bloco)
  - tls_mtls_requests.csv           (uma linha por requisição)
  - tls_mtls_overhead_summary.csv
  - tls_mtls_overhead_manifest.json

CPU: process times do container (user+system) via docker stats deltas,
normalizados por requisição — não % em janelas curtas.
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import ssl
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
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
    host_load_snapshot,
    image_digest,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_http_200,
    wait_thermal_gate,
    write_manifest,
)

CONDITIONS = (
    "tls_new",
    "tls_persistent",
    "mtls_new",
    "mtls_persistent",
)

RUN_FIELDS = [
    "run_id", "timestamp_utc", "block", "order_in_block", "condition",
    "auth_mode", "conn_mode", "n_requests", "warmup_requests", "wall_sec",
    "throughput_rps",
    "latency_median_ms", "latency_iqr_ms", "latency_p95_ms", "latency_mean_ms",
    "process_cpu_user_ms", "process_cpu_system_ms", "cpu_per_request_ms",
    "temp_tctl_c_start", "exit_code", "error",
]
REQ_FIELDS = [
    "run_id", "block", "condition", "request_index", "warmup",
    "connection_id", "connection_reused",
    "connection_setup_ms", "request_after_connection_ms", "total_ms",
    "http_status", "success",
]
SUMMARY_FIELDS = [
    "condition", "n_blocks", "metric", "median", "iqr", "min", "max", "mean", "p95",
]


def _resolve_certs(cli: Path | None) -> Path:
    if cli is not None:
        return cli.expanduser().resolve()
    env = os.environ.get("HARNESS_CERTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    candidate = ROOT.parent / "src" / "harness" / "certs"
    if candidate.is_dir():
        return candidate
    alt = Path("/home/alanveloso/Código/Spectrum-Access-System/src/harness/certs")
    if alt.is_dir():
        return alt
    return (ROOT / "certs").resolve()


def _tls_only_context(certs: Path) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(cafile=str(certs / "ca.cert"))
    return ctx


def _iqr(vals: list[float]) -> float:
    return (percentile(vals, 75) or 0.0) - (percentile(vals, 25) or 0.0)


def _container_cpu_ms(container: str) -> tuple[float, float]:
    """Return (user_ms, system_ms) from docker stats — best effort."""
    proc = subprocess.run(
        [
            "docker", "stats", "--no-stream", "--format",
            "{{.CPUPerc}}\t{{.Container}}",
            container,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    # Prefer /sys/fs/cgroup via docker exec for cumulative CPU
    proc2 = subprocess.run(
        [
            "docker", "exec", container, "cat", "/proc/1/stat",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc2.returncode != 0 or not proc2.stdout.strip():
        del proc  # silence unused
        return 0.0, 0.0
    parts = proc2.stdout.split()
    # utime (13), stime (14) in clock ticks
    try:
        utime = int(parts[13])
        stime = int(parts[14])
        hz = os.sysconf(os.sysconf_names.get("SC_CLK_TCK", "SC_CLK_TCK"))
        if not hz or hz <= 0:
            hz = 100
        return (utime * 1000.0 / hz), (stime * 1000.0 / hz)
    except (IndexError, ValueError, OSError):
        return 0.0, 0.0


def _run_block(
    *,
    condition: str,
    url: str,
    ctx: ssl.SSLContext,
    container: str,
    n_requests: int,
    warmup: int,
    block: int,
    order: int,
    req_writer: csv.DictWriter,
    req_fh: Any,
) -> dict[str, Any]:
    auth_mode, conn_mode = condition.split("_", 1)
    latencies: list[float] = []
    error = ""
    exit_code = 0
    snap = host_load_snapshot()
    temp = snap.get("temperature_tctl_c", snap.get("temperature_max_c"))
    run_id = new_run_id()

    limits_new = httpx.Limits(max_keepalive_connections=0, max_connections=1)
    limits_persist = httpx.Limits(max_keepalive_connections=8, max_connections=8)

    cpu0 = _container_cpu_ms(container)
    t_wall = time.perf_counter()
    try:
        if conn_mode == "new":
            conn_id = 0
            for i in range(warmup + n_requests):
                is_warm = i < warmup
                conn_id += 1
                t_setup0 = time.perf_counter()
                with httpx.Client(
                    verify=ctx, timeout=10.0, limits=limits_new
                ) as client:
                    setup_ms = (time.perf_counter() - t_setup0) * 1000.0
                    t_req0 = time.perf_counter()
                    resp = client.get(url)
                    after_ms = (time.perf_counter() - t_req0) * 1000.0
                    total_ms = setup_ms + after_ms
                    ok = resp.status_code == 200
                    if not is_warm:
                        if not ok:
                            resp.raise_for_status()
                        latencies.append(total_ms)
                    req_writer.writerow({
                        "run_id": run_id,
                        "block": block,
                        "condition": condition,
                        "request_index": i + 1,
                        "warmup": int(is_warm),
                        "connection_id": conn_id,
                        "connection_reused": 0,
                        "connection_setup_ms": round(setup_ms, 4),
                        "request_after_connection_ms": round(after_ms, 4),
                        "total_ms": round(total_ms, 4),
                        "http_status": resp.status_code,
                        "success": int(ok),
                    })
                    req_fh.flush()
        else:
            with httpx.Client(
                verify=ctx, timeout=10.0, limits=limits_persist
            ) as client:
                conn_id = 1
                for i in range(warmup + n_requests):
                    is_warm = i < warmup
                    reused = 1 if i > 0 else 0
                    t0 = time.perf_counter()
                    resp = client.get(url)
                    total_ms = (time.perf_counter() - t0) * 1000.0
                    ok = resp.status_code == 200
                    if not is_warm:
                        if not ok:
                            resp.raise_for_status()
                        latencies.append(total_ms)
                    req_writer.writerow({
                        "run_id": run_id,
                        "block": block,
                        "condition": condition,
                        "request_index": i + 1,
                        "warmup": int(is_warm),
                        "connection_id": conn_id,
                        "connection_reused": reused,
                        "connection_setup_ms": "" if reused else round(total_ms, 4),
                        "request_after_connection_ms": round(total_ms, 4) if reused else "",
                        "total_ms": round(total_ms, 4),
                        "http_status": resp.status_code,
                        "success": int(ok),
                    })
                    req_fh.flush()
    except Exception as exc:  # noqa: BLE001
        exit_code = 1
        error = str(exc)[:300]
    wall = time.perf_counter() - t_wall
    cpu1 = _container_cpu_ms(container)
    user_d = max(0.0, cpu1[0] - cpu0[0])
    sys_d = max(0.0, cpu1[1] - cpu0[1])
    n_ok = len(latencies)
    cpu_per = ((user_d + sys_d) / n_ok) if n_ok else ""

    thr = (n_ok / wall) if wall > 0 and n_ok else float("nan")
    return {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "block": block,
        "order_in_block": order,
        "condition": condition,
        "auth_mode": auth_mode,
        "conn_mode": conn_mode,
        "n_requests": n_ok,
        "warmup_requests": warmup,
        "wall_sec": round(wall, 4),
        "throughput_rps": round(thr, 3) if thr == thr else "",
        "latency_median_ms": round(float(statistics.median(latencies)), 4) if latencies else "",
        "latency_iqr_ms": round(_iqr(latencies), 4) if latencies else "",
        "latency_p95_ms": round(percentile(latencies, 95) or 0.0, 4) if latencies else "",
        "latency_mean_ms": round(statistics.fmean(latencies), 4) if latencies else "",
        "process_cpu_user_ms": round(user_d, 3),
        "process_cpu_system_ms": round(sys_d, 3),
        "cpu_per_request_ms": round(float(cpu_per), 4) if cpu_per != "" else "",
        "temp_tctl_c_start": round(float(temp), 2) if temp is not None else "",
        "exit_code": exit_code,
        "error": error,
    }


def _summarize(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = [
        "latency_median_ms",
        "latency_p95_ms",
        "latency_iqr_ms",
        "throughput_rps",
        "cpu_per_request_ms",
        "process_cpu_user_ms",
        "wall_sec",
    ]
    out: list[dict[str, Any]] = []
    for cond in CONDITIONS:
        rows = [r for r in runs if r["condition"] == cond and r["exit_code"] == 0]
        for metric in metrics:
            vals = []
            for r in rows:
                v = r.get(metric)
                if v == "" or v is None:
                    continue
                vals.append(float(v))
            if not vals:
                continue
            out.append({
                "condition": cond,
                "n_blocks": len(vals),
                "metric": metric,
                "median": round(float(statistics.median(vals)), 4),
                "iqr": round(_iqr(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "mean": round(statistics.fmean(vals), 4),
                "p95": round(percentile(vals, 95) or 0.0, 4),
            })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--blocks", type=int, default=30)
    p.add_argument("--requests-per-block", type=int, default=100)
    p.add_argument("--warmup", type=int, default=5)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--mtls-port", type=int, default=19400)
    p.add_argument("--tls-port", type=int, default=19401)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--cooldown-sec", type=float, default=0.5)
    p.add_argument("--skip-thermal-gate", action="store_true")
    p.add_argument("--out-dir", type=Path, default=DATA)
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    mtls_ctx = build_mtls_context(certs)
    tls_ctx = _tls_only_context(certs)

    if not args.skip_thermal_gate:
        wait_thermal_gate(
            out_path=out_dir / "thermal_gate_tls_mtls_overhead.json",
            label="tls_mtls_overhead",
        )

    mtls_name = "sas-overhead-mtls"
    tls_name = "sas-overhead-tls"
    docker_run(
        DockerRunSpec(
            image=args.image,
            name=mtls_name,
            host_port=args.mtls_port,
            certs_dir=certs,
            database_path=f"/data/overhead_mtls_{new_run_id()}.db",
            extra_env={"SAS_TLS_CLIENT_AUTH": "required"},
        )
    )
    docker_run(
        DockerRunSpec(
            image=args.image,
            name=tls_name,
            host_port=args.tls_port,
            certs_dir=certs,
            database_path=f"/data/overhead_tls_{new_run_id()}.db",
            extra_env={"SAS_TLS_CLIENT_AUTH": "none"},
        )
    )
    try:
        ok1, _, e1 = wait_http_200(
            f"https://127.0.0.1:{args.mtls_port}/ready", mtls_ctx, 90.0
        )
        ok2, _, e2 = wait_http_200(
            f"https://127.0.0.1:{args.tls_port}/ready", tls_ctx, 90.0
        )
        if not ok1 or not ok2:
            raise SystemExit(f"ready failed mtls={e1} tls={e2}")

        targets = {
            "mtls_new": (f"https://127.0.0.1:{args.mtls_port}/ready", mtls_ctx, mtls_name),
            "mtls_persistent": (f"https://127.0.0.1:{args.mtls_port}/ready", mtls_ctx, mtls_name),
            "tls_new": (f"https://127.0.0.1:{args.tls_port}/ready", tls_ctx, tls_name),
            "tls_persistent": (f"https://127.0.0.1:{args.tls_port}/ready", tls_ctx, tls_name),
        }

        runs_path = out_dir / "tls_mtls_overhead_runs.csv"
        req_path = out_dir / "tls_mtls_requests.csv"
        all_runs: list[dict[str, Any]] = []
        failures = 0
        with runs_path.open("w", newline="", encoding="utf-8") as fh, \
             req_path.open("w", newline="", encoding="utf-8") as req_fh:
            writer = csv.DictWriter(fh, fieldnames=RUN_FIELDS)
            writer.writeheader()
            req_writer = csv.DictWriter(req_fh, fieldnames=REQ_FIELDS)
            req_writer.writeheader()
            for block in range(1, args.blocks + 1):
                order = list(CONDITIONS)
                random.Random(args.seed + block).shuffle(order)
                print(f"=== block {block}/{args.blocks} order={order} ===", flush=True)
                for idx, cond in enumerate(order, start=1):
                    url, ctx, cname = targets[cond]
                    row = _run_block(
                        condition=cond,
                        url=url,
                        ctx=ctx,
                        container=cname,
                        n_requests=args.requests_per_block,
                        warmup=args.warmup,
                        block=block,
                        order=idx,
                        req_writer=req_writer,
                        req_fh=req_fh,
                    )
                    writer.writerow(row)
                    fh.flush()
                    all_runs.append(row)
                    status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
                    print(
                        f"  {cond}: med={row['latency_median_ms']}ms "
                        f"p95={row['latency_p95_ms']}ms rps={row['throughput_rps']} {status}",
                        flush=True,
                    )
                    if row["exit_code"] != 0:
                        failures += 1
                    if args.cooldown_sec > 0:
                        time.sleep(args.cooldown_sec)
    finally:
        docker_rm(mtls_name)
        docker_rm(tls_name)

    summary = _summarize(all_runs)
    summary_path = out_dir / "tls_mtls_overhead_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)

    write_manifest(
        out_dir / "tls_mtls_overhead_manifest.json",
        capture_manifest(
            seed=args.seed,
            n=args.blocks,
            warmup=args.warmup,
            script_paths=[LOAD_DIR / "tls_mtls_overhead_bench.py"],
            certs_dir=certs,
            extra={
                "experiment": "tls_mtls_overhead",
                "blocks": args.blocks,
                "requests_per_block": args.requests_per_block,
                "warmup_per_condition": args.warmup,
                "conditions": list(CONDITIONS),
                "per_request_file": "tls_mtls_requests.csv",
                "cpu_metric": "process_cpu_user/system_ms from /proc/1/stat deltas",
                "handshake_limitation": (
                    "httpx does not expose TLS handshake alone; "
                    "connection_setup_ms approximates client+handshake for new connections"
                ),
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "failures": failures,
            },
        ),
    )
    print(f"Wrote {runs_path}\nWrote {req_path}\nWrote {summary_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
