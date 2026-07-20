"""Comparative Docker cold-start benchmark: monolithic vs core.

Measures wall time from ``docker run`` until the first mTLS HTTP 200 on
``GET /ready``. Runs paired alternating rounds and emits long-format CSV plus
a reproducibility manifest.
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import time
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

from common import (  # noqa: E402
    DEFAULT_N,
    DEFAULT_SEED,
    DEFAULT_WARMUP,
    LONG_FIELDS,
    DockerRunSpec,
    append_long_row,
    build_mtls_context,
    capture_manifest,
    docker_rm,
    docker_run,
    image_digest,
    new_run_id,
    utc_now_iso,
    wait_http_200,
    write_manifest,
)

DATA = BENCH_DIR / "data"

RAW_FIELDS = [
    "run_id",
    "pair_id",
    "order",
    "timestamp_utc",
    "architecture",
    "image",
    "host_port",
    "latency_ms",
    "ready_polls",
    "warmup",
    "exit_code",
    "error",
]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Paired container cold-start: monolithic vs core",
    )
    p.add_argument("--n", type=int, default=DEFAULT_N)
    p.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image-monolithic", default="sas-agent:monolithic")
    p.add_argument("--image-core", default="sas-agent:core")
    p.add_argument(
        "--certs-dir",
        type=Path,
        default=None,
        help="Harness certs directory (default: HARNESS_CERTS_DIR or ../src/harness/certs)",
    )
    p.add_argument("--timeout-sec", type=float, default=90.0)
    p.add_argument("--port-monolithic", type=int, default=19000)
    p.add_argument("--port-core", type=int, default=19001)
    p.add_argument("--out-dir", type=Path, default=DATA)
    return p.parse_args()


def _resolve_certs(cli: Path | None) -> Path:
    import os

    if cli is not None:
        return cli.expanduser().resolve()
    env = os.environ.get("HARNESS_CERTS_DIR")
    if env:
        return Path(env).expanduser().resolve()
    # Monorepo layout: <root>/src/harness/certs next to sas-agent/
    candidate = ROOT.parent / "src" / "harness" / "certs"
    if candidate.is_dir():
        return candidate
    local = ROOT / "certs"
    return local.resolve()


def _schedule(
    n: int,
    warmup: int,
    seed: int,
) -> list[tuple[int, str, bool]]:
    """Return (pair_id, architecture, is_warmup) with alternating arch order.

    Within each pair the order of architectures is shuffled with a
    deterministic RNG so temporal confounding is controlled.
    """
    rng = random.Random(seed)
    schedule: list[tuple[int, str, bool]] = []
    total = n + warmup
    for pair_id in range(1, total + 1):
        is_warmup = pair_id <= warmup
        arches = ["monolithic", "core"]
        rng.shuffle(arches)
        for arch in arches:
            schedule.append((pair_id, arch, is_warmup))
    return schedule


def _run_once(
    *,
    architecture: str,
    image: str,
    host_port: int,
    certs_dir: Path,
    pair_id: int,
    order: int,
    warmup: bool,
    timeout_sec: float,
    ctx: Any,
) -> dict[str, Any]:
    name = f"sas-cold-{architecture}-{pair_id}-{order}"
    run_id = new_run_id()
    error: str | None = None
    exit_code = 0
    latency_ms = float("nan")
    polls = 0

    spec = DockerRunSpec(
        image=image,
        name=name,
        host_port=host_port,
        certs_dir=certs_dir,
    )
    ready_url = f"https://127.0.0.1:{host_port}/ready"

    t0 = time.perf_counter()
    try:
        docker_run(spec)
        ok, polls, poll_err = wait_http_200(ready_url, ctx, timeout_sec)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        if not ok:
            exit_code = 1
            error = poll_err or "ready timeout"
    except Exception as exc:  # noqa: BLE001
        latency_ms = (time.perf_counter() - t0) * 1000.0
        exit_code = 1
        error = str(exc)
    finally:
        docker_rm(name)

    return {
        "run_id": run_id,
        "pair_id": pair_id,
        "order": order,
        "timestamp_utc": utc_now_iso(),
        "architecture": architecture,
        "image": image,
        "host_port": host_port,
        "latency_ms": latency_ms,
        "ready_polls": polls,
        "warmup": int(warmup),
        "exit_code": exit_code,
        "error": error or "",
    }


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    certs_dir = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs_dir)

    images = {
        "monolithic": args.image_monolithic,
        "core": args.image_core,
    }
    ports = {
        "monolithic": args.port_monolithic,
        "core": args.port_core,
    }

    schedule = _schedule(args.n, args.warmup, args.seed)
    raw_path = out_dir / "container_coldstart_raw.csv"
    long_path = out_dir / "container_coldstart.csv"
    manifest_path = out_dir / "container_coldstart_manifest.json"

    long_rows: list[dict[str, Any]] = []
    raw_rows: list[dict[str, Any]] = []

    with raw_path.open("w", newline="", encoding="utf-8") as raw_fh, long_path.open(
        "w", newline="", encoding="utf-8"
    ) as long_fh:
        raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
        long_writer = csv.DictWriter(long_fh, fieldnames=list(LONG_FIELDS))
        raw_writer.writeheader()
        long_writer.writeheader()

        for order, (pair_id, architecture, is_warmup) in enumerate(schedule, start=1):
            row = _run_once(
                architecture=architecture,
                image=images[architecture],
                host_port=ports[architecture],
                certs_dir=certs_dir,
                pair_id=pair_id,
                order=order,
                warmup=is_warmup,
                timeout_sec=args.timeout_sec,
                ctx=ctx,
            )
            raw_writer.writerow(row)
            raw_fh.flush()
            raw_rows.append(row)

            # Long format keeps non-warmup samples for hypothesis tests / plots.
            if not is_warmup:
                long_rows.append(
                    append_long_row(
                        long_writer,
                        long_fh,
                        architecture,
                        1,
                        "cold_start_ms",
                        row["latency_ms"],
                    )
                )
                long_rows.append(
                    append_long_row(
                        long_writer,
                        long_fh,
                        architecture,
                        1,
                        "ready_polls",
                        row["ready_polls"],
                    )
                )
            status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
            print(
                f"[{order}/{len(schedule)}] pair={pair_id} "
                f"{architecture} {row['latency_ms']:.1f}ms polls={row['ready_polls']} "
                f"warmup={is_warmup} {status}",
                flush=True,
            )

    failures = sum(
        1
        for r in raw_rows
        if not r["warmup"] and r["exit_code"] != 0
    )
    manifest = capture_manifest(
        seed=args.seed,
        n=args.n,
        warmup=args.warmup,
        script_paths=[LOAD_DIR / "container_coldstart.py", LOAD_DIR / "common.py"],
        certs_dir=certs_dir,
        extra={
            "experiment": "container_coldstart",
            "health_url_template": "https://127.0.0.1:{port}/ready",
            "ports": ports,
            "images": {
                arch: {
                    "tag": tag,
                    "digest": image_digest(tag),
                }
                for arch, tag in images.items()
            },
            "n_runs": len(raw_rows),
            "non_warmup_failures": failures,
            "timeout_sec": args.timeout_sec,
            "outputs": {
                "raw_csv": str(raw_path),
                "long_csv": str(long_path),
            },
        },
    )
    write_manifest(manifest_path, manifest)
    print(f"Wrote {raw_path}")
    print(f"Wrote {long_path}")
    print(f"Wrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
