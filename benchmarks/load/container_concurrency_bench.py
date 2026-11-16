"""Docker multi-container concurrency benchmark (spectrum slicing).

Partitions the CBRS band into K equal slices and runs K containers in
parallel, each owning one slice.

Population modes:
  fixed_total     — (default, historical) --devices N split across K agents
  fixed_per_agent — --devices-per-agent P per agent, total = K × P; outputs
                    use the container_concurrency_per_agent_* prefix

Each agent is driven by one sequential HTTPS client, so at most K requests
are in flight (one per agent); recorded as max_inflight_requests_*.

Scenarios:
  A — create K containers, wait all ready, then measure Reg+Grant load wall
  B — wall clock includes create + ready + load (teardown reported separately)

K ∈ {1,5,10,15}, 5 randomized repeats. Outputs:
  container_concurrency_{runs,summary,manifest}.*

Grant latency columns (Grants are sent as batched POSTs of up to BATCH_MAX):
  grant_batch_latency_*_ms     — HTTP round-trip of each Grant batch POST
  grant_amortized_ms_per_item  — sum(batch time) / sum(items in batches)
  grant_latency_*_ms           — legacy: per-batch time / batch size, repeated
                                 once per successful item; amortized, NOT the
                                 latency of an individual Grant.
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    BATCH_MAX,
    DEFAULT_SEED,
    DockerRunSpec,
    build_mtls_context,
    capture_manifest,
    chunked,
    docker_rm,
    docker_run,
    docker_stats_sample,
    docker_stats_sample_many,
    host_load_snapshot,
    image_digest,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_http_200,
    wait_thermal_gate,
    write_manifest,
)

BAND_LOW_HZ = 3_550_000_000
BAND_HIGH_HZ = 3_700_000_000
BAND_WIDTH_HZ = BAND_HIGH_HZ - BAND_LOW_HZ
CHANNEL_WIDTH_HZ = 10_000_000

DEFAULT_K = (1, 2, 3, 5, 10, 15)
DEFAULT_DEVICES = 1500
DEFAULT_REPEATS = 5

RUN_FIELDS = [
    "run_id", "timestamp_utc", "scenario", "repeat", "k_order_in_block",
    "k_containers", "slice_width_mhz", "total_devices", "device_seed",
    "create_ready_sec", "load_wall_sec", "scenario_wall_sec", "teardown_sec",
    "throughput_devices_per_sec", "grant_latency_mean_ms", "grant_latency_median_ms",
    "grant_latency_p95_ms", "grant_batch_count", "grant_batch_latency_mean_ms",
    "grant_batch_latency_median_ms", "grant_batch_latency_p95_ms",
    "grant_amortized_ms_per_item", "ram_peak_mb_sum", "cpu_pct_peak_sum",
    "temp_c_start", "devices_ok", "devices_failed", "exit_code", "error",
    "population_mode", "devices_per_agent_min", "devices_per_agent_max",
    "max_inflight_requests_total", "max_inflight_requests_per_agent",
    "stats_sweeps", "ram_after_load_mb_sum",
]
SUMMARY_FIELDS = [
    "scenario", "k_containers", "n_repeats", "metric",
    "median", "iqr", "min", "max", "mean", "ci95_low", "ci95_high",
    "population_mode", "total_devices",
    "experimental_unit", "ci95_of",
]
FIXED_TOTAL = "fixed_total"
FIXED_PER_AGENT = "fixed_per_agent"
INFLIGHT_PER_AGENT = 1
DEFAULT_STATS_INTERVAL_SEC = 0.5


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


def _partition_slices(k: int) -> list[tuple[int, int]]:
    width = BAND_WIDTH_HZ // k
    slices = [
        (BAND_LOW_HZ + i * width, BAND_LOW_HZ + (i + 1) * width) for i in range(k)
    ]
    slices[-1] = (slices[-1][0], BAND_HIGH_HZ)
    return slices


def _partition_devices(total: int, k: int) -> list[int]:
    base, rem = divmod(total, k)
    return [base + (1 if i < rem else 0) for i in range(k)]


def _agent_populations(k: int, total_devices: int, devices_per_agent: int | None) -> list[int]:
    if devices_per_agent is not None:
        return [devices_per_agent] * k
    return _partition_devices(total_devices, k)


def _channels(low: int, high: int) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    f = low
    while f + CHANNEL_WIDTH_HZ <= high:
        out.append((f, f + CHANNEL_WIDTH_HZ))
        f += CHANNEL_WIDTH_HZ
    if not out:
        out.append((low, min(high, low + CHANNEL_WIDTH_HZ)))
    return out


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


def _grant_item(cbsd_id: str, low: int, high: int) -> dict[str, Any]:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": 20.0,
            "operationFrequencyRange": {
                "lowFrequency": low,
                "highFrequency": high,
            },
        },
    }


def _rc(items: list[dict[str, Any]]) -> list[int]:
    return [it.get("response", {}).get("responseCode", -1) for it in items]


def _grant_latency_fields(
    batch_ms: list[float], batch_items: list[int], legacy_item_ms: list[float]
) -> dict[str, Any]:
    def r(v: float | None) -> Any:
        return round(float(v), 4) if v is not None else ""

    n_items = sum(batch_items)
    return {
        "grant_latency_mean_ms": r(statistics.fmean(legacy_item_ms)) if legacy_item_ms else "",
        "grant_latency_median_ms": r(statistics.median(legacy_item_ms)) if legacy_item_ms else "",
        "grant_latency_p95_ms": r(percentile(legacy_item_ms, 95)),
        "grant_batch_count": len(batch_ms),
        "grant_batch_latency_mean_ms": r(statistics.fmean(batch_ms)) if batch_ms else "",
        "grant_batch_latency_median_ms": r(statistics.median(batch_ms)) if batch_ms else "",
        "grant_batch_latency_p95_ms": r(percentile(batch_ms, 95)),
        "grant_amortized_ms_per_item": r(sum(batch_ms) / n_items) if n_items else "",
    }


def _ci95(values: list[float]) -> tuple[float | None, float | None]:
    """Student-t 95% CI for the mean of independent deployment repeats."""
    n = len(values)
    if n < 2:
        return None, None
    # Two-sided 95% t critical values (df=1..30); larger df ≈ 1.96.
    t_table = {
        1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571,
        6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131,
        16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060,
        26: 2.056, 27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
    }
    mean = statistics.fmean(values)
    half = t_table.get(n - 1, 1.96) * statistics.stdev(values) / math.sqrt(n)
    return mean - half, mean + half


def _iqr(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    q75 = percentile(values, 75)
    q25 = percentile(values, 25)
    if q75 is None or q25 is None:
        return None
    return q75 - q25


class _DockerStatsSampler:
    """Periodic docker-stats sampler during load.

    ``ram_peak_sum`` is the **maximum over complete sweeps** of the sum of
    per-container ``docker stats`` MemUsage (cgroup memory, RSS-like — **not**
    process PSS). It is a *sampled* maximum at ``interval`` seconds, **not** an
    absolute peak. Short loads previously often recorded ``sweeps=0`` because
    the background loop discarded incomplete sweeps; we now take a synchronous
    complete sample on enter and on exit so short loads still get ≥1 sample.
    """

    def __init__(self, names: list[str], interval: float = DEFAULT_STATS_INTERVAL_SEC):
        self.names = names
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.ram_peak_sum = 0.0
        self.cpu_peak_sum = 0.0
        self.sweeps = 0
        self.metric_kind = "docker_stats_memusage_cgroup_mb_sum"
        self.not_pss = True

    def __enter__(self) -> "_DockerStatsSampler":
        self._sample_complete()  # baseline before / at load start
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        self._sample_complete()  # final complete sample after load

    def _sample_complete(self) -> bool:
        """One full pass over all containers; discard if any read fails mid-sweep."""
        samples = docker_stats_sample_many(self.names)
        if len(samples) != len(self.names) or any(ram is None for _cpu, ram in samples):
            return False
        ram_sum = sum(ram for _cpu, ram in samples if ram is not None)
        cpu_sum = sum(cpu for cpu, _ram in samples if cpu is not None)
        self.ram_peak_sum = max(self.ram_peak_sum, ram_sum)
        self.cpu_peak_sum = max(self.cpu_peak_sum, cpu_sum)
        self.sweeps += 1
        return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            # Wait first so enter/exit samples are not immediately duplicated.
            if self._stop.wait(self.interval):
                break
            samples = docker_stats_sample_many(self.names)
            if self._stop.is_set():
                break  # load ended during the batched stats RPC
            if len(samples) != len(self.names) or any(ram is None for _c, ram in samples):
                continue  # incomplete / failed sweep — do not count
            ram_sum = sum(ram for _c, ram in samples if ram is not None)
            cpu_sum = sum(cpu for cpu, _r in samples if cpu is not None)
            self.ram_peak_sum = max(self.ram_peak_sum, ram_sum)
            self.cpu_peak_sum = max(self.cpu_peak_sum, cpu_sum)
            self.sweeps += 1


def _ram_sum_mb(names: list[str]) -> float | None:
    """Sum of current container memory (docker stats); None if any read fails."""
    samples = docker_stats_sample_many(names)
    if len(samples) != len(names) or any(ram is None for _c, ram in samples):
        return None
    return float(sum(ram for _c, ram in samples if ram is not None))


def _load_one_container(
    *,
    port: int,
    ctx: Any,
    worker_id: int,
    device_count: int,
    slice_low: int,
    slice_high: int,
    seed: int,
) -> dict[str, Any]:
    base = f"https://127.0.0.1:{port}"
    channels = _channels(slice_low, slice_high)
    fcc_id = f"dock_k_w{worker_id}"
    user_id = f"op_dock_w{worker_id}"
    grant_latencies: list[float] = []
    grant_batch_ms: list[float] = []
    grant_batch_items: list[int] = []
    ok = 0
    failed = 0
    error = ""

    with httpx.Client(verify=ctx, timeout=60.0) as client:
        client.post(f"{base}/admin/injectdata/user_id", json={"userId": user_id}).raise_for_status()
        client.post(
            f"{base}/admin/injectdata/fcc_id",
            json={"fccId": fcc_id, "fccMaxEirp": 47},
        ).raise_for_status()

        fixtures = [
            (fcc_id, f"sn_{seed}_{worker_id}_{i:05d}") for i in range(device_count)
        ]
        # Register in batches
        for chunk in chunked(
            [_reg_item(f, user_id, s) for f, s in fixtures], BATCH_MAX
        ):
            resp = client.post(
                f"{base}/v1.2/registration", json={"registrationRequest": chunk}
            )
            resp.raise_for_status()
            codes = _rc(resp.json()["registrationResponse"])
            for c in codes:
                if c != 0:
                    failed += 1
                    if not error:
                        error = f"registration rc={c}"

        cbsd_ids = [f"{f}/{s}" for f, s in fixtures]
        grant_items = [
            _grant_item(cid, *channels[i % len(channels)])
            for i, cid in enumerate(cbsd_ids)
        ]
        for chunk in chunked(grant_items, BATCH_MAX):
            t0 = time.perf_counter()
            resp = client.post(f"{base}/v1.2/grant", json={"grantRequest": chunk})
            batch_ms = (time.perf_counter() - t0) * 1000.0
            resp.raise_for_status()
            grant_batch_ms.append(batch_ms)
            grant_batch_items.append(len(chunk))
            codes = _rc(resp.json()["grantResponse"])
            per = batch_ms / max(1, len(chunk))
            for c in codes:
                if c == 0:
                    ok += 1
                    grant_latencies.append(per)
                else:
                    failed += 1
                    if not error:
                        error = f"grant rc={c}"

    return {
        "devices_ok": ok,
        "devices_failed": failed,
        "grant_latencies_ms": grant_latencies,
        "grant_batch_ms": grant_batch_ms,
        "grant_batch_items": grant_batch_items,
        "error": error,
    }


def _run_scenario(
    *,
    scenario: str,
    k: int,
    total_devices: int,
    seed: int,
    image: str,
    base_port: int,
    certs: Path,
    ctx: Any,
    ready_timeout: float,
    repeat: int,
    order_idx: int,
    devices_per_agent: int | None = None,
) -> dict[str, Any]:
    slices = _partition_slices(k)
    counts = _agent_populations(k, total_devices, devices_per_agent)
    total_devices = sum(counts)
    names = [f"sas-ccon-k{k}-w{i}-r{repeat}" for i in range(k)]
    ports = [base_port + i for i in range(k)]
    run_id = new_run_id()
    temp_start = host_load_snapshot().get("temperature_max_c")
    error = ""
    exit_code = 0
    create_ready = float("nan")
    load_wall = float("nan")
    scenario_wall = float("nan")
    teardown = float("nan")
    grant_all: list[float] = []
    grant_batch_ms: list[float] = []
    grant_batch_items: list[int] = []
    devices_ok = 0
    devices_failed = 0
    ram_peak: float | None = None
    cpu_peak: float | None = None
    stats_sweeps = 0
    ram_after_load: float | None = None

    t_scenario = time.perf_counter()
    try:
        t_create = time.perf_counter()
        for i in range(k):
            low, high = slices[i]
            docker_run(
                DockerRunSpec(
                    image=image,
                    name=names[i],
                    host_port=ports[i],
                    certs_dir=certs,
                    database_path=f"/data/sas_{run_id}_{i}.db",
                    band_low_hz=low,
                    band_high_hz=high,
                )
            )
        # Wait all ready
        for i, port in enumerate(ports):
            ok, _, err = wait_http_200(
                f"https://127.0.0.1:{port}/ready", ctx, ready_timeout
            )
            if not ok:
                raise RuntimeError(f"container {names[i]} not ready: {err}")
        create_ready = time.perf_counter() - t_create

        with _DockerStatsSampler(names) as sampler:
            t_load = time.perf_counter()
            with ThreadPoolExecutor(max_workers=k) as pool:
                futs = [
                    pool.submit(
                        _load_one_container,
                        port=ports[i],
                        ctx=ctx,
                        worker_id=i,
                        device_count=counts[i],
                        slice_low=slices[i][0],
                        slice_high=slices[i][1],
                        seed=seed,
                    )
                    for i in range(k)
                ]
                for fut in as_completed(futs):
                    res = fut.result()
                    devices_ok += res["devices_ok"]
                    devices_failed += res["devices_failed"]
                    grant_all.extend(res["grant_latencies_ms"])
                    grant_batch_ms.extend(res["grant_batch_ms"])
                    grant_batch_items.extend(res["grant_batch_items"])
                    if res["error"] and not error:
                        error = res["error"]
            load_wall = time.perf_counter() - t_load
        if sampler.sweeps:
            ram_peak, cpu_peak = sampler.ram_peak_sum, sampler.cpu_peak_sum
        stats_sweeps = sampler.sweeps

        if scenario == "A":
            scenario_wall = load_wall
        else:
            scenario_wall = time.perf_counter() - t_scenario
        ram_after_load = _ram_sum_mb(names)

        if devices_failed:
            exit_code = 1
    except Exception as exc:  # noqa: BLE001
        exit_code = 2
        error = str(exc)[:300]
        if scenario == "B":
            scenario_wall = time.perf_counter() - t_scenario
        elif load_wall != load_wall:  # nan
            scenario_wall = time.perf_counter() - t_scenario
    finally:
        t_td = time.perf_counter()
        for name in names:
            docker_rm(name)
        teardown = time.perf_counter() - t_td

    thr = (
        devices_ok / scenario_wall
        if scenario_wall and scenario_wall == scenario_wall and scenario_wall > 0
        else float("nan")
    )
    return {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "scenario": scenario,
        "repeat": repeat,
        "k_order_in_block": order_idx,
        "k_containers": k,
        "slice_width_mhz": round((BAND_WIDTH_HZ / k) / 1e6, 3),
        "total_devices": total_devices,
        "device_seed": seed,
        "create_ready_sec": round(create_ready, 4) if create_ready == create_ready else "",
        "load_wall_sec": round(load_wall, 4) if load_wall == load_wall else "",
        "scenario_wall_sec": round(scenario_wall, 4) if scenario_wall == scenario_wall else "",
        "teardown_sec": round(teardown, 4),
        "throughput_devices_per_sec": round(thr, 3) if thr == thr else "",
        **_grant_latency_fields(grant_batch_ms, grant_batch_items, grant_all),
        "ram_peak_mb_sum": round(ram_peak, 2) if ram_peak is not None else "",
        "cpu_pct_peak_sum": round(cpu_peak, 2) if cpu_peak is not None else "",
        "temp_c_start": round(float(temp_start), 2) if temp_start is not None else "",
        "devices_ok": devices_ok,
        "devices_failed": devices_failed,
        "exit_code": exit_code,
        "error": error,
        "population_mode": FIXED_TOTAL if devices_per_agent is None else FIXED_PER_AGENT,
        "devices_per_agent_min": min(counts),
        "devices_per_agent_max": max(counts),
        "max_inflight_requests_total": k * INFLIGHT_PER_AGENT,
        "max_inflight_requests_per_agent": INFLIGHT_PER_AGENT,
        "stats_sweeps": stats_sweeps,
        "ram_after_load_mb_sum": round(ram_after_load, 2) if ram_after_load is not None else "",
    }


def _aggregate(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = [
        "scenario_wall_sec",
        "load_wall_sec",
        "create_ready_sec",
        "throughput_devices_per_sec",
        "grant_latency_p95_ms",
        "grant_latency_mean_ms",
        "grant_batch_latency_median_ms",
        "grant_batch_latency_p95_ms",
        "grant_amortized_ms_per_item",
        "ram_peak_mb_sum",
        "ram_after_load_mb_sum",
    ]
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in runs:
        if r["exit_code"] != 0:
            continue
        key = (str(r["scenario"]), int(r["k_containers"]))
        groups.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for (scenario, k), rows in sorted(groups.items()):
        for metric in metrics:
            vals = []
            for r in rows:
                v = r.get(metric)
                if v == "" or v is None:
                    continue
                try:
                    vals.append(float(v))
                except (TypeError, ValueError):
                    continue
            if not vals:
                continue
            lo, hi = _ci95(vals)
            out.append({
                "scenario": scenario,
                "k_containers": k,
                "n_repeats": len(vals),
                "metric": metric,
                "median": round(float(statistics.median(vals)), 4),
                "iqr": round(_iqr(vals) or 0.0, 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "mean": round(statistics.fmean(vals), 4),
                "ci95_low": round(lo, 4) if lo is not None else "",
                "ci95_high": round(hi, 4) if hi is not None else "",
                "population_mode": rows[0].get("population_mode", FIXED_TOTAL),
                "total_devices": rows[0].get("total_devices", ""),
                "experimental_unit": "independent_deployment_repeat",
                "ci95_of": "mean_across_repeats",
            })
    return out


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--k-values", type=int, nargs="+", default=list(DEFAULT_K))
    pop = p.add_mutually_exclusive_group()
    pop.add_argument("--devices", type=int, default=DEFAULT_DEVICES,
                     help="fixed_total: devices split across K agents (default)")
    pop.add_argument("--devices-per-agent", type=int, default=None, metavar="P",
                     help="fixed_per_agent: P devices per agent, total = K*P")
    p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image", default="sas-agent:article")
    p.add_argument("--base-port", type=int, default=19200)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--ready-timeout-sec", type=float, default=90.0)
    p.add_argument("--cooldown-sec", type=float, default=5.0)
    p.add_argument("--max-temp-c", type=float, default=65.0)
    p.add_argument("--gate-timeout-sec", type=float, default=600.0)
    p.add_argument("--skip-thermal-gate", action="store_true")
    p.add_argument("--scenarios", nargs="+", default=["A", "B"], choices=["A", "B"])
    p.add_argument("--out-dir", type=Path, default=DATA)
    args = p.parse_args(argv)
    if args.devices_per_agent is not None and args.devices_per_agent < 1:
        p.error("--devices-per-agent must be >= 1")
    args.population_mode = FIXED_TOTAL if args.devices_per_agent is None else FIXED_PER_AGENT
    args.output_stem = (
        "container_concurrency" if args.devices_per_agent is None
        else "container_concurrency_per_agent"
    )
    return args


def main() -> int:
    args = _parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs)

    if not args.skip_thermal_gate:
        gate = wait_thermal_gate(
            max_tctl_c=args.max_temp_c,
            out_path=out_dir / f"thermal_gate_{args.output_stem}.json",
            label="container_concurrency",
            timeout_sec=args.gate_timeout_sec,
        )
        if not gate.get("passed"):
            print("FAIL thermal/load gate before container concurrency", flush=True)
            return 1

    runs_path = out_dir / f"{args.output_stem}_runs.csv"
    summary_path = out_dir / f"{args.output_stem}_summary.csv"
    manifest_path = out_dir / f"{args.output_stem}_manifest.json"

    all_runs: list[dict[str, Any]] = []
    failures = 0
    schedules: list[list[int]] = []

    with runs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUN_FIELDS)
        writer.writeheader()
        for scenario in args.scenarios:
            for repeat in range(1, args.repeats + 1):
                order = list(args.k_values)
                random.Random(args.seed + repeat * 17 + (0 if scenario == "A" else 99)).shuffle(order)
                schedules.append(order)
                device_seed = args.seed + 1000 * repeat
                print(
                    f"=== scenario={scenario} repeat={repeat}/{args.repeats} "
                    f"K-order={order} ===",
                    flush=True,
                )
                for idx, k in enumerate(order, start=1):
                    row = _run_scenario(
                        scenario=scenario,
                        k=k,
                        total_devices=args.devices,
                        seed=device_seed,
                        image=args.image,
                        base_port=args.base_port,
                        certs=certs,
                        ctx=ctx,
                        ready_timeout=args.ready_timeout_sec,
                        repeat=repeat,
                        order_idx=idx,
                        devices_per_agent=args.devices_per_agent,
                    )
                    writer.writerow(row)
                    fh.flush()
                    all_runs.append(row)
                    status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
                    print(
                        f"  K={k} devices={row['total_devices']} wall={row['scenario_wall_sec']}s "
                        f"grant_batch_p95={row['grant_batch_latency_p95_ms']}ms "
                        f"grant_amortized={row['grant_amortized_ms_per_item']}ms/item {status}",
                        flush=True,
                    )
                    if row["exit_code"] != 0:
                        failures += 1
                    if args.cooldown_sec > 0:
                        time.sleep(args.cooldown_sec)

    summary = _aggregate(all_runs)
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)

    write_manifest(
        manifest_path,
        capture_manifest(
            seed=args.seed,
            n=args.devices if args.devices_per_agent is None else args.devices_per_agent,
            warmup=0,
            script_paths=[LOAD_DIR / "container_concurrency_bench.py", LOAD_DIR / "common.py"],
            certs_dir=certs,
            extra={
                "experiment": "container_concurrency",
                "population": {
                    "mode": args.population_mode,
                    "n_semantics": "total devices split across K"
                    if args.devices_per_agent is None else "devices per agent",
                    "devices_per_agent": args.devices_per_agent,
                    "total_devices_by_k": {
                        str(k): sum(_agent_populations(k, args.devices, args.devices_per_agent))
                        for k in args.k_values
                    },
                },
                "request_concurrency": {
                    "model": "one sequential HTTPS client per agent; batches of up to "
                             f"{BATCH_MAX} items",
                    "max_inflight_requests_per_agent": INFLIGHT_PER_AGENT,
                    "max_inflight_requests_total": "k_containers * 1",
                },
                "scenarios": list(args.scenarios),
                "k_values": list(args.k_values),
                "k_schedules": schedules,
                "repeats": args.repeats,
                "grant_batch_max": BATCH_MAX,
                "grant_latency_semantics": {
                    "grant_batch_latency_*_ms": "HTTP round-trip per Grant batch POST",
                    "grant_amortized_ms_per_item": "sum(batch time) / sum(items in batches)",
                    "grant_latency_*_ms": "legacy amortized (batch time / batch size per successful item); not individual Grant latency",
                },
                "memory_semantics": {
                    "ram_peak_mb_sum": (
                        "SAMPLED maximum of sum(docker stats MemUsage) over complete "
                        f"sweeps at interval={DEFAULT_STATS_INTERVAL_SEC}s "
                        "(enter + periodic + exit). Cgroup/RSS-like container memory; "
                        "NOT process PSS; NOT an absolute peak between samples. "
                        "Blank only if every complete sample failed (stats_sweeps = 0)."
                    ),
                    "ram_after_load_mb_sum": (
                        "sum of docker-stats MemUsage per container once after load, "
                        "before teardown; distinct from the sampled maximum"
                    ),
                    "process_pss": (
                        "not measured here; see process concurrency "
                        "ram_peak_pss_mb for host-process PSS"
                    ),
                },
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "failures": failures,
                "outputs": {"runs": str(runs_path), "summary": str(summary_path)},
            },
        ),
    )
    print(f"Wrote {runs_path}\nWrote {summary_path}\nWrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
