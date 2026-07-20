"""Asynchronous mTLS stress test: monolithic vs core under concurrent CBSDs.

Injects Registration and Grant batch storms via ``httpx.AsyncClient`` with the
WInnForum client certificates. Collects RPS, error rate, tail latencies and
continuous CPU/RSS samples (docker stats every 100 ms).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

from common import (  # noqa: E402
    BATCH_MAX,
    DEFAULT_SEED,
    LONG_FIELDS,
    DockerRunSpec,
    append_long_row,
    build_mtls_context,
    capture_manifest,
    chunked,
    docker_rm,
    docker_run,
    docker_stats_sample,
    image_digest,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_http_200,
    write_manifest,
)

DATA = BENCH_DIR / "data"
LOAD_LEVELS = (10, 100, 500, 1000)
DEFAULT_REPEATS = 5
STATS_INTERVAL_SEC = 0.1

RAW_FIELDS = [
    "run_id",
    "repeat",
    "timestamp_utc",
    "architecture",
    "load_level",
    "phase",
    "wall_ms",
    "requests",
    "successes",
    "errors",
    "rps",
    "error_rate",
    "latency_p50_ms",
    "latency_p95_ms",
    "latency_p99_ms",
    "latency_p99_9_ms",
    "cpu_pct_mean",
    "cpu_pct_peak",
    "rss_mb_mean",
    "rss_mb_peak",
    "exit_code",
    "error",
]


@dataclass
class ResourceSample:
    cpu_pct: float
    rss_mb: float


@dataclass
class ResourceSampler:
    """Background sampler for docker stats or psutil."""

    interval_sec: float = STATS_INTERVAL_SEC
    container: str | None = None
    pid: int | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    samples: list[ResourceSample] = field(default_factory=list)

    def start(self) -> None:
        self._stop.clear()
        self.samples.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, float | None]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        return self.summary()

    def summary(self) -> dict[str, float | None]:
        if not self.samples:
            return {
                "cpu_pct_mean": None,
                "cpu_pct_peak": None,
                "rss_mb_mean": None,
                "rss_mb_peak": None,
            }
        cpus = [s.cpu_pct for s in self.samples]
        rss = [s.rss_mb for s in self.samples]
        return {
            "cpu_pct_mean": float(statistics.fmean(cpus)),
            "cpu_pct_peak": float(max(cpus)),
            "rss_mb_mean": float(statistics.fmean(rss)),
            "rss_mb_peak": float(max(rss)),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            sample = self._one()
            if sample is not None:
                self.samples.append(sample)
            self._stop.wait(self.interval_sec)

    def _one(self) -> ResourceSample | None:
        if self.container:
            cpu, rss = docker_stats_sample(self.container)
            if cpu is None or rss is None:
                return None
            return ResourceSample(cpu_pct=cpu, rss_mb=rss)
        if self.pid is not None:
            try:
                import psutil

                proc = psutil.Process(self.pid)
                # Non-blocking cpu percent needs a prior call; use interval=None
                # after priming, or a short blocking sample.
                cpu = proc.cpu_percent(interval=None)
                rss = proc.memory_info().rss / (1024 * 1024)
                if cpu == 0.0 and not self.samples:
                    # Prime psutil measurement.
                    proc.cpu_percent(interval=0.0)
                    return None
                return ResourceSample(cpu_pct=float(cpu), rss_mb=float(rss))
            except Exception:  # noqa: BLE001
                return None
        return None


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="mTLS stress: monolithic vs core")
    p.add_argument(
        "--target",
        choices=("monolithic", "core", "both"),
        default="both",
    )
    p.add_argument("--base-url-monolithic", default=None)
    p.add_argument("--base-url-core", default=None)
    p.add_argument("--image-monolithic", default="sas-agent:monolithic")
    p.add_argument("--image-core", default="sas-agent:core")
    p.add_argument("--port-monolithic", type=int, default=19000)
    p.add_argument("--port-core", type=int, default=19001)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    p.add_argument(
        "--load-levels",
        type=int,
        nargs="+",
        default=list(LOAD_LEVELS),
    )
    p.add_argument("--max-in-flight", type=int, default=100)
    p.add_argument("--timeout-sec", type=float, default=120.0)
    p.add_argument("--ready-timeout-sec", type=float, default=90.0)
    p.add_argument(
        "--manage-containers",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Start/stop Docker targets when base URLs are not provided",
    )
    p.add_argument("--container-monolithic", default=None)
    p.add_argument("--container-core", default=None)
    p.add_argument("--pid", type=int, default=None, help="psutil fallback PID")
    p.add_argument("--out-dir", type=Path, default=DATA)
    return p.parse_args()


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


def _reg_item(fcc_id: str, user_id: str, serial: str) -> dict[str, Any]:
    return {
        "userId": user_id,
        "fccId": fcc_id,
        "cbsdSerialNumber": serial,
        "cbsdCategory": "A",
        "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": {
            "latitude": 37.0,
            "longitude": -122.0,
            "height": 6.0,
            "heightType": "AGL",
            "indoorDeployment": True,
            "antennaGain": 0,
            "eirpCapability": 30.0,
        },
    }


def _grant_item(cbsd_id: str, low_hz: int, high_hz: int) -> dict[str, Any]:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": 20.0,
            "operationFrequencyRange": {
                "lowFrequency": low_hz,
                "highFrequency": high_hz,
            },
        },
    }


def _cbsd_ids(load_level: int, seed: int, architecture: str) -> list[tuple[str, str, str]]:
    """Return list of (fcc_id, user_id, serial) unique per architecture/load."""
    out: list[tuple[str, str, str]] = []
    prefix = architecture[:3]
    for i in range(load_level):
        fcc = f"dev_{prefix}_{seed}_{i}"
        user = f"op_{prefix}_{seed}"
        serial = f"sn_{prefix}_{seed}_{i:05d}"
        out.append((fcc, user, serial))
    return out


async def _inject_fixtures(
    client: httpx.AsyncClient,
    base_url: str,
    fixtures: list[tuple[str, str, str]],
) -> None:
    """Inject unique FCC/user ids required for successful registration."""
    users = sorted({u for _, u, _ in fixtures})
    fccs = sorted({f for f, _, _ in fixtures})
    for user in users:
        resp = await client.post(
            f"{base_url}/admin/injectdata/user_id",
            json={"userId": user},
        )
        resp.raise_for_status()
    for fcc in fccs:
        resp = await client.post(
            f"{base_url}/admin/injectdata/fcc_id",
            json={"fccId": fcc, "fccMaxEirp": 47},
        )
        resp.raise_for_status()


def _response_codes(body: dict[str, Any], wrapper_key: str) -> list[int]:
    items = body.get(wrapper_key) or []
    codes: list[int] = []
    for item in items:
        resp = item.get("response") or {}
        code = resp.get("responseCode")
        codes.append(int(code) if code is not None else -1)
    return codes


@dataclass
class PhaseResult:
    latencies_ms: list[float]
    successes: int
    errors: int
    wall_ms: float
    error_detail: str = ""

    @property
    def requests(self) -> int:
        return self.successes + self.errors

    @property
    def rps(self) -> float:
        if self.wall_ms <= 0:
            return 0.0
        return self.successes / (self.wall_ms / 1000.0)

    @property
    def error_rate(self) -> float:
        if self.requests == 0:
            return 1.0
        return self.errors / self.requests


async def _post_batch(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    wrapper_key: str,
    sem: asyncio.Semaphore,
) -> tuple[float, int, int, str]:
    """Return (latency_ms, successes, errors, error_detail)."""
    async with sem:
        t0 = time.perf_counter()
        try:
            resp = await client.post(url, json=payload)
            latency = (time.perf_counter() - t0) * 1000.0
            if resp.status_code != 200:
                n = len(next(iter(payload.values()), []))
                return latency, 0, n, f"HTTP {resp.status_code}"
            body = resp.json()
            codes = _response_codes(body, wrapper_key)
            ok = sum(1 for c in codes if c == 0)
            err = len(codes) - ok
            detail = ""
            if err:
                bad = sorted({c for c in codes if c != 0})
                detail = f"responseCode={bad}"
            return latency, ok, err, detail
        except Exception as exc:  # noqa: BLE001
            latency = (time.perf_counter() - t0) * 1000.0
            n = len(next(iter(payload.values()), []))
            return latency, 0, n, str(exc)


async def _run_phase(
    client: httpx.AsyncClient,
    url: str,
    batches: list[dict[str, Any]],
    wrapper_key: str,
    max_in_flight: int,
) -> PhaseResult:
    sem = asyncio.Semaphore(max_in_flight)
    t0 = time.perf_counter()
    results = await asyncio.gather(
        *[
            _post_batch(client, url, payload, wrapper_key, sem)
            for payload in batches
        ]
    )
    wall_ms = (time.perf_counter() - t0) * 1000.0
    latencies = [r[0] for r in results]
    successes = sum(r[1] for r in results)
    errors = sum(r[2] for r in results)
    details = [r[3] for r in results if r[3]]
    return PhaseResult(
        latencies_ms=latencies,
        successes=successes,
        errors=errors,
        wall_ms=wall_ms,
        error_detail="; ".join(details[:3]),
    )


def _cbrs_channel(idx: int) -> tuple[int, int]:
    """10 MHz GAA channel inside CBRS 3550–3700 MHz."""
    width = 10_000_000
    band_low = 3_550_000_000
    band_high = 3_700_000_000
    n = max(1, (band_high - band_low) // width)
    ch = idx % n
    low = band_low + width * ch
    return low, low + width


async def _stress_once(
    *,
    client: httpx.AsyncClient,
    base_url: str,
    architecture: str,
    load_level: int,
    seed: int,
    max_in_flight: int,
    sampler: ResourceSampler,
) -> dict[str, Any]:
    fixtures = _cbsd_ids(load_level, seed, architecture)
    await _inject_fixtures(client, base_url, fixtures)

    reg_items = [_reg_item(f, u, s) for f, u, s in fixtures]
    reg_batches = [
        {"registrationRequest": chunk}
        for chunk in chunked(reg_items, BATCH_MAX)
    ]

    sampler.start()
    try:
        reg = await _run_phase(
            client,
            f"{base_url}/v1.2/registration",
            reg_batches,
            "registrationResponse",
            max_in_flight,
        )

        cbsd_ids = [f"{f}/{s}" for f, _, s in fixtures]
        grant_items = [
            _grant_item(cid, *_cbrs_channel(i))
            for i, cid in enumerate(cbsd_ids)
        ]
        grant_batches = [
            {"grantRequest": chunk}
            for chunk in chunked(grant_items, BATCH_MAX)
        ]
        grant = await _run_phase(
            client,
            f"{base_url}/v1.2/grant",
            grant_batches,
            "grantResponse",
            max_in_flight,
        )
    finally:
        resources = sampler.stop()

    # Aggregate both phases for headline metrics; keep per-phase in raw rows.
    all_lat = reg.latencies_ms + grant.latencies_ms
    total_ok = reg.successes + grant.successes
    total_err = reg.errors + grant.errors
    wall_ms = reg.wall_ms + grant.wall_ms
    rps = total_ok / (wall_ms / 1000.0) if wall_ms > 0 else 0.0
    error_rate = (
        total_err / (total_ok + total_err) if (total_ok + total_err) else 1.0
    )
    exit_code = 0 if total_err == 0 else 1
    error = "; ".join(
        x for x in (reg.error_detail, grant.error_detail) if x
    )

    return {
        "architecture": architecture,
        "load_level": load_level,
        "reg": reg,
        "grant": grant,
        "wall_ms": wall_ms,
        "requests": total_ok + total_err,
        "successes": total_ok,
        "errors": total_err,
        "rps": rps,
        "error_rate": error_rate,
        "latency_p50_ms": percentile(all_lat, 50),
        "latency_p95_ms": percentile(all_lat, 95),
        "latency_p99_ms": percentile(all_lat, 99),
        "latency_p99_9_ms": percentile(all_lat, 99.9),
        **resources,
        "exit_code": exit_code,
        "error": error,
    }


def _emit_metrics(
    long_writer: csv.DictWriter,
    long_fh: Any,
    architecture: str,
    load_level: int,
    result: dict[str, Any],
) -> None:
    metrics = (
        ("rps", result["rps"]),
        ("error_rate", result["error_rate"]),
        ("latency_p50_ms", result["latency_p50_ms"]),
        ("latency_p95_ms", result["latency_p95_ms"]),
        ("latency_p99_ms", result["latency_p99_ms"]),
        ("latency_p99_9_ms", result["latency_p99_9_ms"]),
        ("cpu_pct_mean", result["cpu_pct_mean"]),
        ("cpu_pct_peak", result["cpu_pct_peak"]),
        ("rss_mb_mean", result["rss_mb_mean"]),
        ("rss_mb_peak", result["rss_mb_peak"]),
        ("wall_ms", result["wall_ms"]),
    )
    for name, value in metrics:
        if value is None:
            continue
        append_long_row(long_writer, long_fh, architecture, load_level, name, value)


def _phase_raw_row(
    *,
    run_id: str,
    repeat: int,
    architecture: str,
    load_level: int,
    phase: str,
    phase_result: PhaseResult,
    resources: dict[str, float | None],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "repeat": repeat,
        "timestamp_utc": utc_now_iso(),
        "architecture": architecture,
        "load_level": load_level,
        "phase": phase,
        "wall_ms": phase_result.wall_ms,
        "requests": phase_result.requests,
        "successes": phase_result.successes,
        "errors": phase_result.errors,
        "rps": phase_result.rps,
        "error_rate": phase_result.error_rate,
        "latency_p50_ms": percentile(phase_result.latencies_ms, 50),
        "latency_p95_ms": percentile(phase_result.latencies_ms, 95),
        "latency_p99_ms": percentile(phase_result.latencies_ms, 99),
        "latency_p99_9_ms": percentile(phase_result.latencies_ms, 99.9),
        "cpu_pct_mean": resources.get("cpu_pct_mean"),
        "cpu_pct_peak": resources.get("cpu_pct_peak"),
        "rss_mb_mean": resources.get("rss_mb_mean"),
        "rss_mb_peak": resources.get("rss_mb_peak"),
        "exit_code": 0 if phase_result.errors == 0 else 1,
        "error": phase_result.error_detail,
    }


async def _ensure_target(
    *,
    architecture: str,
    base_url: str | None,
    image: str,
    host_port: int,
    certs_dir: Path,
    manage: bool,
    container_name: str | None,
    ready_timeout: float,
    ctx: Any,
) -> tuple[str, str | None, bool]:
    """Return (base_url, container_name_or_None, started_by_us)."""
    if base_url:
        return base_url.rstrip("/"), container_name, False
    if not manage:
        raise SystemExit(
            f"--base-url-{architecture} is required when --no-manage-containers"
        )
    name = container_name or f"sas-stress-{architecture}"
    docker_run(
        DockerRunSpec(
            image=image,
            name=name,
            host_port=host_port,
            certs_dir=certs_dir,
        )
    )
    url = f"https://127.0.0.1:{host_port}"
    ok, _, err = wait_http_200(f"{url}/ready", ctx, ready_timeout)
    if not ok:
        docker_rm(name)
        raise RuntimeError(f"{architecture} failed readiness: {err}")
    return url, name, True


async def _async_main(args: argparse.Namespace) -> int:
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs_dir = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs_dir)

    targets: list[str]
    if args.target == "both":
        targets = ["monolithic", "core"]
    else:
        targets = [args.target]

    images = {
        "monolithic": args.image_monolithic,
        "core": args.image_core,
    }
    ports = {
        "monolithic": args.port_monolithic,
        "core": args.port_core,
    }
    base_urls_cli = {
        "monolithic": args.base_url_monolithic,
        "core": args.base_url_core,
    }
    container_cli = {
        "monolithic": args.container_monolithic,
        "core": args.container_core,
    }

    started: list[str] = []
    resolved: dict[str, tuple[str, str | None]] = {}

    try:
        for arch in targets:
            url, cname, we_started = await _ensure_target(
                architecture=arch,
                base_url=base_urls_cli[arch],
                image=images[arch],
                host_port=ports[arch],
                certs_dir=certs_dir,
                manage=args.manage_containers,
                container_name=container_cli[arch],
                ready_timeout=args.ready_timeout_sec,
                ctx=ctx,
            )
            resolved[arch] = (url, cname)
            if we_started and cname:
                started.append(cname)

        raw_path = out_dir / "mtls_stress_results_raw.csv"
        long_path = out_dir / "mtls_stress_results.csv"
        manifest_path = out_dir / "mtls_stress_manifest.json"

        failures = 0
        with raw_path.open("w", newline="", encoding="utf-8") as raw_fh, long_path.open(
            "w", newline="", encoding="utf-8"
        ) as long_fh:
            raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
            long_writer = csv.DictWriter(long_fh, fieldnames=list(LONG_FIELDS))
            raw_writer.writeheader()
            long_writer.writeheader()

            limits = httpx.Limits(
                max_connections=args.max_in_flight * 2,
                max_keepalive_connections=args.max_in_flight,
            )
            timeout = httpx.Timeout(args.timeout_sec)

            async with httpx.AsyncClient(
                verify=ctx,
                timeout=timeout,
                limits=limits,
            ) as client:
                for arch in targets:
                    base_url, container = resolved[arch]
                    for load_level in args.load_levels:
                        for repeat in range(1, args.repeats + 1):
                            run_id = new_run_id()
                            sampler = ResourceSampler(
                                container=container,
                                pid=args.pid if container is None else None,
                            )
                            print(
                                f"=== {arch} load={load_level} repeat={repeat}/{args.repeats} ===",
                                flush=True,
                            )
                            try:
                                result = await _stress_once(
                                    client=client,
                                    base_url=base_url,
                                    architecture=arch,
                                    load_level=load_level,
                                    seed=args.seed + repeat,
                                    max_in_flight=args.max_in_flight,
                                    sampler=sampler,
                                )
                            except Exception as exc:  # noqa: BLE001
                                failures += 1
                                print(f"FAIL: {exc}", flush=True)
                                continue

                            resources = {
                                "cpu_pct_mean": result["cpu_pct_mean"],
                                "cpu_pct_peak": result["cpu_pct_peak"],
                                "rss_mb_mean": result["rss_mb_mean"],
                                "rss_mb_peak": result["rss_mb_peak"],
                            }
                            for phase_name, phase_obj in (
                                ("registration", result["reg"]),
                                ("grant", result["grant"]),
                                (
                                    "combined",
                                    PhaseResult(
                                        latencies_ms=(
                                            result["reg"].latencies_ms
                                            + result["grant"].latencies_ms
                                        ),
                                        successes=result["successes"],
                                        errors=result["errors"],
                                        wall_ms=result["wall_ms"],
                                        error_detail=result["error"],
                                    ),
                                ),
                            ):
                                raw_writer.writerow(
                                    _phase_raw_row(
                                        run_id=run_id,
                                        repeat=repeat,
                                        architecture=arch,
                                        load_level=load_level,
                                        phase=phase_name,
                                        phase_result=phase_obj,
                                        resources=resources,
                                    )
                                )
                            raw_fh.flush()
                            _emit_metrics(
                                long_writer, long_fh, arch, load_level, result
                            )
                            if result["exit_code"] != 0:
                                failures += 1
                            print(
                                f"  rps={result['rps']:.1f} "
                                f"err={result['error_rate']:.3f} "
                                f"p99={result['latency_p99_ms']} "
                                f"cpu_peak={result['cpu_pct_peak']} "
                                f"rss_peak={result['rss_mb_peak']}",
                                flush=True,
                            )

        manifest = capture_manifest(
            seed=args.seed,
            n=args.repeats,
            warmup=0,
            script_paths=[
                LOAD_DIR / "mtls_stress_test.py",
                LOAD_DIR / "common.py",
            ],
            certs_dir=certs_dir,
            extra={
                "experiment": "mtls_stress",
                "target": args.target,
                "load_levels": list(args.load_levels),
                "repeats": args.repeats,
                "max_in_flight": args.max_in_flight,
                "batch_max": BATCH_MAX,
                "stats_interval_sec": STATS_INTERVAL_SEC,
                "base_urls": {a: resolved[a][0] for a in targets},
                "containers": {a: resolved[a][1] for a in targets},
                "images": {
                    a: {"tag": images[a], "digest": image_digest(images[a])}
                    for a in targets
                },
                "non_warmup_failures": failures,
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
    finally:
        for name in started:
            docker_rm(name)


def main() -> int:
    args = _parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
