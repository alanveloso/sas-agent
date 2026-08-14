"""Stress test de orquestração fracionada (Spectrum Slicing) com concorrência real.

A banda CBRS completa (3550-3700 MHz, 150 MHz) é particionada em K fatias
iguais e a população fixa de CBSDs é dividida igualmente entre K instâncias
simultâneas do motor do sas-agent, uma por processo filho (``multiprocessing``,
start method ``spawn`` — bypass do GIL, estressando os núcleos físicos).

Versão v2 (padrão com ``--repeats > 1``):
  - N repetições independentes por K;
  - ordem de K randomizada em cada bloco;
  - seed de dispositivos distinta por bloco;
  - P95/mediana **globais** sobre as latências individuais da rodada;
  - agregação entre rodadas (mediana, IQR, min/max, IC 95%);
  - cooldown térmico entre rodadas;
  - saídas ``concurrent_slicing_v2_*.csv/json`` (não sobrescreve a bateria v1).

Uso:
  .venv/bin/python benchmarks/metrics_sandbox/concurrent_slicing_bench.py --repeats 5
"""

from __future__ import annotations

import argparse
import csv
import math
import multiprocessing as mp
import random
import statistics
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
BENCH_DIR = SCRIPT_DIR.parent
ROOT = BENCH_DIR.parent
LOAD_DIR = BENCH_DIR / "load"
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (  # noqa: E402
    DEFAULT_SEED,
    capture_manifest,
    host_load_snapshot,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_thermal_gate,
    write_manifest,
)

DATA = BENCH_DIR / "data"

BAND_LOW_HZ = 3_550_000_000
BAND_HIGH_HZ = 3_700_000_000
BAND_WIDTH_HZ = BAND_HIGH_HZ - BAND_LOW_HZ  # 150 MHz
CHANNEL_WIDTH_HZ = 10_000_000

DEFAULT_K_VALUES = (1, 2, 3, 5, 10, 15)
DEFAULT_DEVICES = 1500
DEFAULT_REPEATS = 5
DEFAULT_COOLDOWN_SEC = 2.0
RSS_SAMPLE_INTERVAL_SEC = 0.1
CHILD_RSS_SAMPLE_EVERY = 25

RUN_FIELDS = [
    "run_id",
    "timestamp_utc",
    "repeat",
    "k_agents",
    "k_order_in_block",
    "slice_width_mhz",
    "total_devices",
    "device_seed",
    "wall_clock_time_sec",
    "throughput_devices_per_sec",
    "grant_latency_mean_ms",
    "grant_latency_median_ms",
    "grant_latency_p95_ms",
    "inquiry_latency_mean_ms",
    "ram_peak_mb",
    "ram_peak_pss_mb",
    "cpu_pct_mean",
    "cpu_pct_peak",
    "temp_c_start",
    "temp_c_max_during",
    "devices_ok",
    "devices_failed",
    "exit_code",
    "error",
]

SUMMARY_V2_FIELDS = [
    "k_agents",
    "slice_width_mhz",
    "n_repeats",
    "metric",
    "median",
    "iqr",
    "min",
    "max",
    "mean",
    "ci95_low",
    "ci95_high",
]

# Legacy v1 summary (single pass, ascending K).
SUMMARY_V1_FIELDS = [
    "k_agents",
    "slice_width_mhz",
    "total_devices",
    "wall_clock_time_sec",
    "grant_latency_mean_ms",
    "ram_peak_mb",
]

RAW_FIELDS = [
    "run_id",
    "timestamp_utc",
    "repeat",
    "k_agents",
    "worker_id",
    "slice_low_hz",
    "slice_high_hz",
    "devices",
    "devices_ok",
    "devices_failed",
    "worker_wall_sec",
    "grant_latency_mean_ms",
    "grant_latency_p95_ms",
    "inquiry_latency_mean_ms",
    "inquiry_latency_p95_ms",
    "registration_latency_mean_ms",
    "worker_rss_peak_mb",
    "worker_pss_peak_mb",
    "exit_code",
    "error",
]


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: int
    k_agents: int
    slice_low_hz: int
    slice_high_hz: int
    device_count: int
    db_path: str
    seed: int
    root_dir: str


@dataclass
class WorkerResult:
    worker_id: int
    devices_ok: int = 0
    devices_failed: int = 0
    worker_wall_sec: float = 0.0
    grant_latencies_ms: list[float] = field(default_factory=list)
    inquiry_latencies_ms: list[float] = field(default_factory=list)
    registration_latencies_ms: list[float] = field(default_factory=list)
    worker_rss_peak_mb: float = 0.0
    worker_pss_peak_mb: float = 0.0
    exit_code: int = 0
    error: str = ""

    def to_payload(self) -> dict[str, Any]:
        return self.__dict__


def _registration_request(fcc_id: str, user_id: str, serial: str) -> dict[str, Any]:
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
        },
    }


def _slice_channels(low_hz: int, high_hz: int) -> list[tuple[int, int]]:
    channels: list[tuple[int, int]] = []
    f = low_hz
    while f + CHANNEL_WIDTH_HZ <= high_hz:
        channels.append((f, f + CHANNEL_WIDTH_HZ))
        f += CHANNEL_WIDTH_HZ
    if not channels:
        channels.append((low_hz, high_hz))
    return channels


def _response_code(responses: list[dict[str, Any]]) -> int:
    return responses[0].get("response", {}).get("responseCode", -1)


def _rss_pss_mb(proc: Any) -> tuple[float, float | None]:
    rss = proc.memory_info().rss / (1024 * 1024)
    try:
        return rss, proc.memory_full_info().pss / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return rss, None


def _worker_main(cfg: WorkerConfig, out_queue: Any) -> None:
    result = WorkerResult(worker_id=cfg.worker_id)
    try:
        import os

        os.environ["SAS_DATABASE_PATH"] = cfg.db_path
        os.environ.pop("BAND_PROFILE_FILE", None)
        os.environ.pop("BAND_LOW", None)
        os.environ.pop("BAND_HIGH", None)

        if cfg.root_dir not in sys.path:
            sys.path.insert(0, cfg.root_dir)

        import psutil

        import database
        from domain.band_profile import cbrs_winnforum_profile
        from models.models import FccIdRecord, UserIdRecord
        from services.grant_service import process_grant
        from services.profile_manager import (
            reset_active_band_profile,
            set_active_band_profile,
        )
        from services.registration_service import process_registration
        from services.spectrum_inquiry_service import process_spectrum_inquiry

        reset_active_band_profile()
        profile = cbrs_winnforum_profile().with_band_limits(
            cfg.slice_low_hz, cfg.slice_high_hz
        )
        set_active_band_profile(profile)
        database.init_db()

        fcc_id = f"slice_k{cfg.k_agents}_w{cfg.worker_id}"
        user_id = f"op_k{cfg.k_agents}_w{cfg.worker_id}"
        session = database.SessionLocal()
        try:
            session.add(FccIdRecord(fcc_id=fcc_id, fcc_max_eirp=47.0))
            session.add(UserIdRecord(user_id=user_id))
            session.commit()
        finally:
            session.close()

        channels = _slice_channels(cfg.slice_low_hz, cfg.slice_high_hz)
        proc = psutil.Process(os.getpid())
        rss, pss = _rss_pss_mb(proc)
        result.worker_rss_peak_mb = rss
        if pss is not None:
            result.worker_pss_peak_mb = pss

        t_worker = time.perf_counter()
        for i in range(cfg.device_count):
            serial = f"sn_{cfg.seed}_{cfg.worker_id}_{i:05d}"
            low, high = channels[i % len(channels)]
            db = database.SessionLocal()
            try:
                t0 = time.perf_counter()
                reg = process_registration(
                    db,
                    [_registration_request(fcc_id, user_id, serial)],
                    profile=profile,
                )
                reg_ms = (time.perf_counter() - t0) * 1000.0
                if _response_code(reg) != 0:
                    raise RuntimeError(f"registration rc={_response_code(reg)}")
                cbsd_id = reg[0]["cbsdId"]

                t0 = time.perf_counter()
                inquiry = process_spectrum_inquiry(
                    db,
                    [
                        {
                            "cbsdId": cbsd_id,
                            "inquiredSpectrum": [
                                {
                                    "lowFrequency": cfg.slice_low_hz,
                                    "highFrequency": cfg.slice_high_hz,
                                }
                            ],
                        }
                    ],
                    profile=profile,
                )
                inquiry_ms = (time.perf_counter() - t0) * 1000.0
                if _response_code(inquiry) != 0:
                    raise RuntimeError(f"spectrumInquiry rc={_response_code(inquiry)}")

                t0 = time.perf_counter()
                grant = process_grant(
                    db,
                    [
                        {
                            "cbsdId": cbsd_id,
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": low,
                                    "highFrequency": high,
                                },
                            },
                        }
                    ],
                    profile=profile,
                )
                grant_ms = (time.perf_counter() - t0) * 1000.0
                if _response_code(grant) != 0:
                    raise RuntimeError(f"grant rc={_response_code(grant)}")

                result.registration_latencies_ms.append(reg_ms)
                result.inquiry_latencies_ms.append(inquiry_ms)
                result.grant_latencies_ms.append(grant_ms)
                result.devices_ok += 1
            except Exception as exc:  # noqa: BLE001
                result.devices_failed += 1
                if not result.error:
                    result.error = str(exc)[:300]
            finally:
                db.close()

            if i % CHILD_RSS_SAMPLE_EVERY == 0:
                rss, pss = _rss_pss_mb(proc)
                result.worker_rss_peak_mb = max(result.worker_rss_peak_mb, rss)
                if pss is not None:
                    result.worker_pss_peak_mb = max(result.worker_pss_peak_mb, pss)

        result.worker_wall_sec = time.perf_counter() - t_worker
        rss, pss = _rss_pss_mb(proc)
        result.worker_rss_peak_mb = max(result.worker_rss_peak_mb, rss)
        if pss is not None:
            result.worker_pss_peak_mb = max(result.worker_pss_peak_mb, pss)
        result.exit_code = 0 if result.devices_failed == 0 else 1
    except Exception as exc:  # noqa: BLE001
        result.exit_code = 2
        result.error = str(exc)[:300]
    out_queue.put(result.to_payload())


class AggregateResourceSampler:
    """Background sampler for summed RSS and mean/peak CPU of child PIDs."""

    def __init__(self, pids: list[int], interval_sec: float = RSS_SAMPLE_INTERVAL_SEC):
        self._pids = pids
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_mb: float = 0.0
        self.peak_pss_mb: float = 0.0
        self.cpu_samples: list[float] = []
        self.temp_samples: list[float] = []

    def __enter__(self) -> "AggregateResourceSampler":
        self._thread = threading.Thread(
            target=self._loop, name="aggregate-resource-sampler", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        import psutil

        procs: list[Any] = []
        for pid in self._pids:
            try:
                p = psutil.Process(pid)
                p.cpu_percent(interval=None)  # prime
                procs.append(p)
            except psutil.NoSuchProcess:
                continue
        while not self._stop.is_set():
            total_rss = 0.0
            total_pss = 0.0
            saw_pss = False
            total_cpu = 0.0
            for proc in procs:
                try:
                    rss, pss = _rss_pss_mb(proc)
                    total_rss += rss
                    if pss is not None:
                        total_pss += pss
                        saw_pss = True
                    total_cpu += proc.cpu_percent(interval=None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.peak_mb = max(self.peak_mb, total_rss)
            if saw_pss:
                self.peak_pss_mb = max(self.peak_pss_mb, total_pss)
            self.cpu_samples.append(total_cpu)
            try:
                temps = psutil.sensors_temperatures() or {}
                readings = [
                    float(e.current)
                    for entries in temps.values()
                    for e in entries
                    if e.current is not None
                ]
                if readings:
                    self.temp_samples.append(max(readings))
            except Exception:  # noqa: BLE001
                pass
            self._stop.wait(self._interval)


def _partition_slices(k: int) -> list[tuple[int, int]]:
    width = BAND_WIDTH_HZ // k
    slices = [
        (BAND_LOW_HZ + i * width, BAND_LOW_HZ + (i + 1) * width) for i in range(k)
    ]
    slices[-1] = (slices[-1][0], BAND_HIGH_HZ)
    return slices


def _partition_devices(total: int, k: int) -> list[int]:
    base, remainder = divmod(total, k)
    return [base + (1 if i < remainder else 0) for i in range(k)]


def _temp_max_c() -> float | None:
    snap = host_load_snapshot()
    val = snap.get("temperature_max_c")
    return float(val) if val is not None else None


def _ci95(values: list[float]) -> tuple[float | None, float | None]:
    """95% CI via Student-t approximation (df=n-1). Requires n>=2."""
    n = len(values)
    if n < 2:
        return None, None
    mean = statistics.fmean(values)
    sd = statistics.stdev(values)
    # Critical values for df=4..9 (n=5..10) and asymptotic 1.96.
    t_table = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262}
    tcrit = t_table.get(n - 1, 1.96)
    half = tcrit * sd / math.sqrt(n)
    return mean - half, mean + half


def _iqr(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    q75 = percentile(values, 75)
    q25 = percentile(values, 25)
    if q75 is None or q25 is None:
        return None
    return q75 - q25


def _run_round(
    k: int,
    total_devices: int,
    seed: int,
    join_timeout_sec: float,
    *,
    repeat: int = 1,
    k_order_in_block: int = 1,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ctx = mp.get_context("spawn")
    out_queue: Any = ctx.Queue()
    slices = _partition_slices(k)
    device_counts = _partition_devices(total_devices, k)
    slice_width_mhz = (BAND_WIDTH_HZ / k) / 1e6
    temp_start = _temp_max_c()
    run_id = new_run_id()

    with tempfile.TemporaryDirectory(prefix=f"slicing_k{k}_r{repeat}_") as tmp_dir:
        configs = [
            WorkerConfig(
                worker_id=i,
                k_agents=k,
                slice_low_hz=slices[i][0],
                slice_high_hz=slices[i][1],
                device_count=device_counts[i],
                db_path=str(Path(tmp_dir) / f"agent_{i}.db"),
                seed=seed,
                root_dir=str(ROOT),
            )
            for i in range(k)
        ]
        processes = [
            ctx.Process(
                target=_worker_main,
                args=(cfg, out_queue),
                name=f"sas-slice-k{k}-r{repeat}-w{cfg.worker_id}",
                daemon=False,
            )
            for cfg in configs
        ]

        t0 = time.perf_counter()
        for proc in processes:
            proc.start()

        payloads: list[dict[str, Any]] = []
        with AggregateResourceSampler([p.pid for p in processes if p.pid]) as sampler:
            deadline = time.monotonic() + join_timeout_sec
            while len(payloads) < k and time.monotonic() < deadline:
                try:
                    payloads.append(out_queue.get(timeout=1.0))
                except Exception:  # noqa: BLE001
                    continue
            for proc in processes:
                proc.join(timeout=max(0.0, deadline - time.monotonic()))
        wall_clock_sec = time.perf_counter() - t0

        for proc in processes:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=10.0)
            proc.close()
        out_queue.close()
        out_queue.join_thread()

    results = [WorkerResult(**p) for p in payloads]
    missing = k - len(results)

    all_grant = [ms for r in results for ms in r.grant_latencies_ms]
    all_inquiry = [ms for r in results for ms in r.inquiry_latencies_ms]
    devices_ok = sum(r.devices_ok for r in results)
    devices_failed = sum(r.devices_failed for r in results)

    cpu_mean = statistics.fmean(sampler.cpu_samples) if sampler.cpu_samples else None
    cpu_peak = max(sampler.cpu_samples) if sampler.cpu_samples else None
    temp_max = max(sampler.temp_samples) if sampler.temp_samples else temp_start

    run_row: dict[str, Any] = {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "repeat": repeat,
        "k_agents": k,
        "k_order_in_block": k_order_in_block,
        "slice_width_mhz": round(slice_width_mhz, 3),
        "total_devices": total_devices,
        "device_seed": seed,
        "wall_clock_time_sec": round(wall_clock_sec, 3),
        "throughput_devices_per_sec": (
            round(devices_ok / wall_clock_sec, 3) if wall_clock_sec > 0 else ""
        ),
        "grant_latency_mean_ms": (
            round(statistics.fmean(all_grant), 3) if all_grant else ""
        ),
        "grant_latency_median_ms": (
            round(float(statistics.median(all_grant)), 3) if all_grant else ""
        ),
        "grant_latency_p95_ms": (
            round(percentile(all_grant, 95) or 0.0, 3) if all_grant else ""
        ),
        "inquiry_latency_mean_ms": (
            round(statistics.fmean(all_inquiry), 3) if all_inquiry else ""
        ),
        "ram_peak_mb": round(sampler.peak_mb, 1),
        "ram_peak_pss_mb": round(sampler.peak_pss_mb, 1) if sampler.peak_pss_mb else "",
        "cpu_pct_mean": round(cpu_mean, 2) if cpu_mean is not None else "",
        "cpu_pct_peak": round(cpu_peak, 2) if cpu_peak is not None else "",
        "temp_c_start": round(temp_start, 2) if temp_start is not None else "",
        "temp_c_max_during": round(temp_max, 2) if temp_max is not None else "",
        "devices_ok": devices_ok,
        "devices_failed": devices_failed,
        "exit_code": 0 if devices_failed == 0 and missing == 0 else 1,
        "error": "; ".join(r.error for r in results if r.error)[:400],
    }

    raw_rows: list[dict[str, Any]] = []
    for r in sorted(results, key=lambda x: x.worker_id):
        raw_rows.append(
            {
                "run_id": run_id,
                "timestamp_utc": utc_now_iso(),
                "repeat": repeat,
                "k_agents": k,
                "worker_id": r.worker_id,
                "slice_low_hz": slices[r.worker_id][0],
                "slice_high_hz": slices[r.worker_id][1],
                "devices": device_counts[r.worker_id],
                "devices_ok": r.devices_ok,
                "devices_failed": r.devices_failed,
                "worker_wall_sec": round(r.worker_wall_sec, 3),
                "grant_latency_mean_ms": (
                    round(statistics.fmean(r.grant_latencies_ms), 3)
                    if r.grant_latencies_ms
                    else ""
                ),
                "grant_latency_p95_ms": (
                    round(percentile(r.grant_latencies_ms, 95) or 0.0, 3)
                    if r.grant_latencies_ms
                    else ""
                ),
                "inquiry_latency_mean_ms": (
                    round(statistics.fmean(r.inquiry_latencies_ms), 3)
                    if r.inquiry_latencies_ms
                    else ""
                ),
                "inquiry_latency_p95_ms": (
                    round(percentile(r.inquiry_latencies_ms, 95) or 0.0, 3)
                    if r.inquiry_latencies_ms
                    else ""
                ),
                "registration_latency_mean_ms": (
                    round(statistics.fmean(r.registration_latencies_ms), 3)
                    if r.registration_latencies_ms
                    else ""
                ),
                "worker_rss_peak_mb": round(r.worker_rss_peak_mb, 1),
                "worker_pss_peak_mb": round(r.worker_pss_peak_mb, 1) if r.worker_pss_peak_mb else "",
                "exit_code": r.exit_code,
                "error": r.error,
            }
        )

    print(
        f"repeat={repeat} K={k:>2} order={k_order_in_block} "
        f"wall={wall_clock_sec:7.2f}s grant_mean={run_row['grant_latency_mean_ms']} "
        f"p95={run_row['grant_latency_p95_ms']} rss={run_row['ram_peak_mb']}MB "
        f"pss={run_row['ram_peak_pss_mb']}MB "
        f"failed={devices_failed} missing={missing}",
        flush=True,
    )
    if missing:
        raise RuntimeError(
            f"K={k} repeat={repeat}: {missing} worker(s) did not report results"
        )
    return run_row, raw_rows


def _aggregate_between_runs(run_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = (
        "wall_clock_time_sec",
        "throughput_devices_per_sec",
        "grant_latency_mean_ms",
        "grant_latency_median_ms",
        "grant_latency_p95_ms",
        "ram_peak_mb",
        "ram_peak_pss_mb",
        "cpu_pct_mean",
        "cpu_pct_peak",
    )
    by_k: dict[int, list[dict[str, Any]]] = {}
    for row in run_rows:
        by_k.setdefault(int(row["k_agents"]), []).append(row)

    out: list[dict[str, Any]] = []
    for k in sorted(by_k):
        rows = by_k[k]
        slice_w = rows[0]["slice_width_mhz"]
        for metric in metrics:
            vals = [float(r[metric]) for r in rows if r.get(metric) not in ("", None)]
            if not vals:
                continue
            ci_lo, ci_hi = _ci95(vals)
            out.append(
                {
                    "k_agents": k,
                    "slice_width_mhz": slice_w,
                    "n_repeats": len(vals),
                    "metric": metric,
                    "median": round(float(statistics.median(vals)), 4),
                    "iqr": round(_iqr(vals) or 0.0, 4),
                    "min": round(min(vals), 4),
                    "max": round(max(vals), 4),
                    "mean": round(statistics.fmean(vals), 4),
                    "ci95_low": round(ci_lo, 4) if ci_lo is not None else "",
                    "ci95_high": round(ci_hi, 4) if ci_hi is not None else "",
                }
            )
    return out


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stress test de slicing concorrente (multiprocessing)",
    )
    parser.add_argument("--k-values", type=int, nargs="+", default=list(DEFAULT_K_VALUES))
    parser.add_argument("--devices", type=int, default=DEFAULT_DEVICES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--cooldown-sec", type=float, default=DEFAULT_COOLDOWN_SEC)
    parser.add_argument("--join-timeout-sec", type=float, default=600.0)
    parser.add_argument(
        "--randomize-k",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Shuffle K order per repeat block (default: on when repeats>1)",
    )
    parser.add_argument("--out-dir", type=Path, default=DATA)
    parser.add_argument(
        "--v2-prefix",
        action="store_true",
        default=None,
        help="Force v2 output filenames even with repeats=1",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    use_v2 = args.repeats > 1 or args.v2_prefix is True
    randomize = (
        args.randomize_k if args.randomize_k is not None else args.repeats > 1
    )

    if use_v2:
        runs_path = out_dir / "concurrent_slicing_v2_runs.csv"
        summary_path = out_dir / "concurrent_slicing_v2_summary.csv"
        raw_path = out_dir / "concurrent_slicing_v2_raw.csv"
        manifest_path = out_dir / "concurrent_slicing_v2_manifest.json"
    else:
        runs_path = out_dir / "concurrent_slicing_results.csv"
        summary_path = runs_path  # v1: summary == per-K single row file
        raw_path = out_dir / "concurrent_slicing_results_raw.csv"
        manifest_path = out_dir / "concurrent_slicing_manifest.json"

    failures = 0
    all_runs: list[dict[str, Any]] = []
    schedules: list[list[int]] = []

    with runs_path.open("w", newline="", encoding="utf-8") as runs_fh, raw_path.open(
        "w", newline="", encoding="utf-8"
    ) as raw_fh:
        run_fields = RUN_FIELDS if use_v2 else SUMMARY_V1_FIELDS
        runs_writer = csv.DictWriter(runs_fh, fieldnames=run_fields, extrasaction="ignore")
        raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
        runs_writer.writeheader()
        raw_writer.writeheader()

        for repeat in range(1, args.repeats + 1):
            gate = wait_thermal_gate(
                max_tctl_c=65.0,
                max_load1=1.0,
                max_cpu_pct=10.0,
                timeout_sec=1200,
                out_path=out_dir / f"thermal_gate_concurrent_repeat_{repeat}.json",
                label=f"concurrent_slicing_repeat_{repeat}",
            )
            if not gate.get("passed"):
                print(
                    f"FAIL thermal/load gate before concurrent repeat={repeat}",
                    flush=True,
                )
                return 1
            order = list(args.k_values)
            if randomize:
                rng = random.Random(args.seed + repeat)
                rng.shuffle(order)
            schedules.append(list(order))
            device_seed = args.seed + 1000 * repeat
            print(
                f"=== block repeat={repeat}/{args.repeats} K-order={order} "
                f"device_seed={device_seed} ===",
                flush=True,
            )
            for order_idx, k in enumerate(order, start=1):
                try:
                    run_row, raw_rows = _run_round(
                        k,
                        args.devices,
                        device_seed,
                        args.join_timeout_sec,
                        repeat=repeat,
                        k_order_in_block=order_idx,
                    )
                except Exception as exc:  # noqa: BLE001
                    failures += 1
                    print(f"FAIL [repeat={repeat} K={k}]: {exc}", flush=True)
                    continue
                all_runs.append(run_row)
                if use_v2:
                    runs_writer.writerow(run_row)
                else:
                    runs_writer.writerow(
                        {
                            "k_agents": run_row["k_agents"],
                            "slice_width_mhz": run_row["slice_width_mhz"],
                            "total_devices": run_row["total_devices"],
                            "wall_clock_time_sec": run_row["wall_clock_time_sec"],
                            "grant_latency_mean_ms": run_row["grant_latency_mean_ms"],
                            "ram_peak_mb": run_row["ram_peak_mb"],
                        }
                    )
                runs_fh.flush()
                raw_writer.writerows(raw_rows)
                raw_fh.flush()
                if run_row["exit_code"] != 0:
                    failures += 1
                if args.cooldown_sec > 0:
                    time.sleep(args.cooldown_sec)

    if use_v2:
        summary_rows = _aggregate_between_runs(all_runs)
        with summary_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=SUMMARY_V2_FIELDS)
            writer.writeheader()
            writer.writerows(summary_rows)
        print(f"Wrote {summary_path}")

    manifest = capture_manifest(
        seed=args.seed,
        n=args.devices,
        warmup=0,
        script_paths=[SCRIPT_DIR / "concurrent_slicing_bench.py", LOAD_DIR / "common.py"],
        extra={
            "experiment": "concurrent_slicing_v2" if use_v2 else "concurrent_slicing",
            "band_hz": [BAND_LOW_HZ, BAND_HIGH_HZ],
            "channel_width_hz": CHANNEL_WIDTH_HZ,
            "k_values": list(args.k_values),
            "k_schedules": schedules,
            "total_devices": args.devices,
            "repeats": args.repeats,
            "randomize_k": randomize,
            "cooldown_sec": args.cooldown_sec,
            "start_method": "spawn",
            "concurrency_level": "process",
            "memory_primary_metric": "pss_sum_peak_mb",
            "memory_secondary_metric": "rss_sum_peak_mb",
            "rss_sample_interval_sec": RSS_SAMPLE_INTERVAL_SEC,
            "failures": failures,
            "outputs": {
                "runs_csv": str(runs_path),
                "summary_csv": str(summary_path),
                "raw_csv": str(raw_path),
            },
        },
    )
    write_manifest(manifest_path, manifest)
    print(f"Wrote {runs_path}")
    print(f"Wrote {raw_path}")
    print(f"Wrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
