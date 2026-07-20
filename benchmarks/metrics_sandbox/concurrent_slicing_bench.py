"""Stress test de orquestração fracionada (Spectrum Slicing) com concorrência real.

A banda CBRS completa (3550-3700 MHz, 150 MHz) é particionada em K fatias
iguais e a população fixa de CBSDs é dividida igualmente entre K instâncias
simultâneas do motor do sas-agent, uma por processo filho (``multiprocessing``,
start method ``spawn`` — bypass do GIL, estressando os núcleos físicos).

Cada processo filho:
  - constrói um ``BandProfile`` dinâmico em memória, derivado do CBRS via
    ``with_band_limits`` com os limites da sua fatia;
  - opera um banco SQLite isolado (``SAS_DATABASE_PATH`` próprio);
  - executa Registration -> Spectrum Inquiry -> Grant para cada CBSD da sua
    lista, medindo a latência de Spectrum Inquiry e Grant.

O processo pai cronometra o wall-clock (do disparo dos processos ao ``join``)
e amostra continuamente o RSS agregado dos K filhos via ``psutil`` (pico da
soma instantânea = overhead de memória da rodada).

Saídas em ``benchmarks/data/``:
  - concurrent_slicing_results.csv       (resumo por K, colunas fixas)
  - concurrent_slicing_results_raw.csv   (detalhe por worker)
  - concurrent_slicing_manifest.json     (snapshot de ambiente via common.py)

Uso:
  .venv/bin/python benchmarks/metrics_sandbox/concurrent_slicing_bench.py
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
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
    new_run_id,
    percentile,
    utc_now_iso,
    write_manifest,
)

DATA = BENCH_DIR / "data"

BAND_LOW_HZ = 3_550_000_000
BAND_HIGH_HZ = 3_700_000_000
BAND_WIDTH_HZ = BAND_HIGH_HZ - BAND_LOW_HZ  # 150 MHz
CHANNEL_WIDTH_HZ = 10_000_000

DEFAULT_K_VALUES = (1, 2, 3, 5, 10, 15)
DEFAULT_DEVICES = 1500
RSS_SAMPLE_INTERVAL_SEC = 0.1
CHILD_RSS_SAMPLE_EVERY = 25  # devices between self-RSS samples in the child

SUMMARY_FIELDS = [
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
    "exit_code",
    "error",
]


@dataclass(frozen=True)
class WorkerConfig:
    """Everything a child process needs; must be picklable (spawn)."""

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
    exit_code: int = 0
    error: str = ""

    def to_payload(self) -> dict[str, Any]:
        return self.__dict__


# --------------------------------------------------------------------------- #
# Child process: isolated sas-agent engine bound to one slice.
# --------------------------------------------------------------------------- #


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
    """10 MHz channels fully inside the slice; falls back to the whole slice."""
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


def _worker_main(cfg: WorkerConfig, out_queue: Any) -> None:
    """Entry point of each agent process (spawn start method)."""
    result = WorkerResult(worker_id=cfg.worker_id)
    try:
        import os

        # Environment must be set before the engine modules are imported:
        # database.py resolves SAS_DATABASE_PATH at import time.
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

        # Dynamic in-memory BandProfile restricted to this worker's slice.
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
        result.worker_rss_peak_mb = proc.memory_info().rss / (1024 * 1024)

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
            except Exception as exc:  # noqa: BLE001 — per-device isolation
                result.devices_failed += 1
                if not result.error:
                    result.error = str(exc)[:300]
            finally:
                db.close()

            if i % CHILD_RSS_SAMPLE_EVERY == 0:
                rss = proc.memory_info().rss / (1024 * 1024)
                result.worker_rss_peak_mb = max(result.worker_rss_peak_mb, rss)

        result.worker_wall_sec = time.perf_counter() - t_worker
        rss = proc.memory_info().rss / (1024 * 1024)
        result.worker_rss_peak_mb = max(result.worker_rss_peak_mb, rss)
        result.exit_code = 0 if result.devices_failed == 0 else 1
    except Exception as exc:  # noqa: BLE001 — report instead of dying silently
        result.exit_code = 2
        result.error = str(exc)[:300]
    out_queue.put(result.to_payload())


# --------------------------------------------------------------------------- #
# Parent process: orchestration, aggregate RSS sampling, aggregation.
# --------------------------------------------------------------------------- #


class AggregateRssSampler:
    """Background thread sampling the summed RSS of the child processes."""

    def __init__(self, pids: list[int], interval_sec: float = RSS_SAMPLE_INTERVAL_SEC):
        self._pids = pids
        self._interval = interval_sec
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak_mb: float = 0.0

    def __enter__(self) -> "AggregateRssSampler":
        self._thread = threading.Thread(
            target=self._loop, name="aggregate-rss-sampler", daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        import psutil

        procs: list[psutil.Process] = []
        for pid in self._pids:
            try:
                procs.append(psutil.Process(pid))
            except psutil.NoSuchProcess:
                continue
        while not self._stop.is_set():
            total = 0.0
            for proc in procs:
                try:
                    total += proc.memory_info().rss / (1024 * 1024)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.peak_mb = max(self.peak_mb, total)
            self._stop.wait(self._interval)


def _partition_slices(k: int) -> list[tuple[int, int]]:
    """K equal slices of the 150 MHz CBRS band."""
    width = BAND_WIDTH_HZ // k
    slices = [
        (BAND_LOW_HZ + i * width, BAND_LOW_HZ + (i + 1) * width) for i in range(k)
    ]
    # Last slice absorbs any integer-division remainder.
    slices[-1] = (slices[-1][0], BAND_HIGH_HZ)
    return slices


def _partition_devices(total: int, k: int) -> list[int]:
    """Split ``total`` devices into K near-equal counts (sum preserved)."""
    base, remainder = divmod(total, k)
    return [base + (1 if i < remainder else 0) for i in range(k)]


def _run_round(
    k: int,
    total_devices: int,
    seed: int,
    join_timeout_sec: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run one K round; returns (summary_row, raw_worker_rows)."""
    ctx = mp.get_context("spawn")
    out_queue: Any = ctx.Queue()
    slices = _partition_slices(k)
    device_counts = _partition_devices(total_devices, k)
    slice_width_mhz = (BAND_WIDTH_HZ / k) / 1e6

    with tempfile.TemporaryDirectory(prefix=f"slicing_k{k}_") as tmp_dir:
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
                name=f"sas-slice-k{k}-w{cfg.worker_id}",
                daemon=False,
            )
            for cfg in configs
        ]

        t0 = time.perf_counter()
        for proc in processes:
            proc.start()

        payloads: list[dict[str, Any]] = []
        with AggregateRssSampler([p.pid for p in processes if p.pid]) as sampler:
            # Drain the queue while children run: large payloads would block
            # child exit (queue feeder thread) if only read after join().
            deadline = time.monotonic() + join_timeout_sec
            while len(payloads) < k and time.monotonic() < deadline:
                try:
                    payloads.append(out_queue.get(timeout=1.0))
                except Exception:  # noqa: BLE001 — queue.Empty
                    continue
            for proc in processes:
                proc.join(timeout=max(0.0, deadline - time.monotonic()))
        wall_clock_sec = time.perf_counter() - t0

        # Cleanup: no child may survive the round (memory released before next K).
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

    summary = {
        "k_agents": k,
        "slice_width_mhz": round(slice_width_mhz, 3),
        "total_devices": total_devices,
        "wall_clock_time_sec": round(wall_clock_sec, 3),
        "grant_latency_mean_ms": (
            round(statistics.fmean(all_grant), 3) if all_grant else ""
        ),
        "ram_peak_mb": round(sampler.peak_mb, 1),
    }

    raw_rows: list[dict[str, Any]] = []
    for r in sorted(results, key=lambda x: x.worker_id):
        raw_rows.append(
            {
                "run_id": new_run_id(),
                "timestamp_utc": utc_now_iso(),
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
                    round(percentile(r.grant_latencies_ms, 95), 3)
                    if r.grant_latencies_ms
                    else ""
                ),
                "inquiry_latency_mean_ms": (
                    round(statistics.fmean(r.inquiry_latencies_ms), 3)
                    if r.inquiry_latencies_ms
                    else ""
                ),
                "inquiry_latency_p95_ms": (
                    round(percentile(r.inquiry_latencies_ms, 95), 3)
                    if r.inquiry_latencies_ms
                    else ""
                ),
                "registration_latency_mean_ms": (
                    round(statistics.fmean(r.registration_latencies_ms), 3)
                    if r.registration_latencies_ms
                    else ""
                ),
                "worker_rss_peak_mb": round(r.worker_rss_peak_mb, 1),
                "exit_code": r.exit_code,
                "error": r.error,
            }
        )

    failed_devices = sum(r.devices_failed for r in results)
    inquiry_mean = statistics.fmean(all_inquiry) if all_inquiry else float("nan")
    print(
        f"K={k:>2} slice={slice_width_mhz:6.1f} MHz devices={total_devices} "
        f"wall={wall_clock_sec:7.2f}s grant_mean={summary['grant_latency_mean_ms']}ms "
        f"inquiry_mean={inquiry_mean:.3f}ms ram_peak={summary['ram_peak_mb']}MB "
        f"failed={failed_devices} missing_workers={missing}",
        flush=True,
    )
    if missing:
        raise RuntimeError(
            f"K={k}: {missing} worker(s) did not report results before timeout"
        )
    return summary, raw_rows


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stress test de slicing concorrente (multiprocessing)",
    )
    parser.add_argument(
        "--k-values",
        type=int,
        nargs="+",
        default=list(DEFAULT_K_VALUES),
        help="números de agentes simultâneos por rodada",
    )
    parser.add_argument("--devices", type=int, default=DEFAULT_DEVICES)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--join-timeout-sec", type=float, default=600.0)
    parser.add_argument("--out-dir", type=Path, default=DATA)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    summary_path = out_dir / "concurrent_slicing_results.csv"
    raw_path = out_dir / "concurrent_slicing_results_raw.csv"
    manifest_path = out_dir / "concurrent_slicing_manifest.json"

    failures = 0
    with summary_path.open("w", newline="", encoding="utf-8") as summary_fh, raw_path.open(
        "w", newline="", encoding="utf-8"
    ) as raw_fh:
        summary_writer = csv.DictWriter(summary_fh, fieldnames=SUMMARY_FIELDS)
        raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
        summary_writer.writeheader()
        raw_writer.writeheader()

        for k in args.k_values:
            try:
                summary, raw_rows = _run_round(
                    k, args.devices, args.seed, args.join_timeout_sec
                )
            except Exception as exc:  # noqa: BLE001 — keep remaining rounds
                failures += 1
                print(f"FAIL [K={k}]: {exc}", flush=True)
                continue
            summary_writer.writerow(summary)
            summary_fh.flush()
            raw_writer.writerows(raw_rows)
            raw_fh.flush()
            failures += sum(1 for r in raw_rows if r["exit_code"] != 0)

    manifest = capture_manifest(
        seed=args.seed,
        n=args.devices,
        warmup=0,
        script_paths=[SCRIPT_DIR / "concurrent_slicing_bench.py", LOAD_DIR / "common.py"],
        extra={
            "experiment": "concurrent_slicing",
            "band_hz": [BAND_LOW_HZ, BAND_HIGH_HZ],
            "channel_width_hz": CHANNEL_WIDTH_HZ,
            "k_values": list(args.k_values),
            "total_devices": args.devices,
            "start_method": "spawn",
            "rss_sample_interval_sec": RSS_SAMPLE_INTERVAL_SEC,
            "failures": failures,
            "outputs": {
                "summary_csv": str(summary_path),
                "raw_csv": str(raw_path),
            },
        },
    )
    write_manifest(manifest_path, manifest)
    print(f"Wrote {summary_path}")
    print(f"Wrote {raw_path}")
    print(f"Wrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
