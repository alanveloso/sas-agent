"""Fase 3 — spectrum slicing benchmark against the Docker agent (mTLS).

Two experiments prove the on-demand slice orchestration model:

1. Boundary enforcement: an agent instance is started as the exclusive
   manager of a 20 MHz slice of the CBRS band (3550-3570 MHz) via
   ``BAND_LOW``/``BAND_HIGH``. A grant at 3560 MHz (inside the slice) must be
   approved (responseCode 0) and a grant at 3600 MHz (inside CBRS but outside
   the slice) must be rejected instantly with UNSUPPORTED_SPECTRUM (300),
   without consulting external registries. Denial latency is recorded.

2. Local efficiency: a "national" agent (full 3550-3700 MHz band) is
   pre-populated with N devices+grants while a "sliced" agent
   (3550-3570 MHz) holds only the proportional share of devices in its
   niche (N * 20/150). The grant issuance latency (lookup + interference
   cross-check) of a probe CBSD is then measured on both, quantifying the
   latency reduction from managing a restricted local database.

Outputs: slicing_results_raw.csv, slicing_results.csv and
slicing_manifest.json in ``benchmarks/data/``.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
import time
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
    DockerRunSpec,
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

CBRS_LOW = 3_550_000_000
CBRS_HIGH = 3_700_000_000
SLICE_LOW = 3_550_000_000
SLICE_HIGH = 3_570_000_000
CHANNEL_WIDTH = 10_000_000

IN_SLICE_GRANT = (3_560_000_000, 3_570_000_000)  # "3560 MHz" request
OUT_OF_SLICE_GRANT = (3_600_000_000, 3_610_000_000)  # "3600 MHz" request

UNSUPPORTED_SPECTRUM = 300

RAW_FIELDS = [
    "run_id",
    "timestamp_utc",
    "scenario",
    "case",
    "iteration",
    "warmup",
    "low_hz",
    "high_hz",
    "response_code",
    "expected_code",
    "latency_ms",
    "rss_mb",
    "exit_code",
    "error",
]

SUMMARY_FIELDS = ["scenario", "metric", "value"]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Spectrum slicing benchmark (Docker + mTLS)")
    p.add_argument("--n", type=int, default=30, help="measured probe grants per scenario")
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument(
        "--population",
        type=int,
        default=1000,
        help="devices pre-populated in the national agent",
    )
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--port", type=int, default=19020)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--ready-timeout-sec", type=float, default=90.0)
    p.add_argument("--out-dir", type=Path, default=DATA)
    return p.parse_args()


def _resolve_certs(cli: Path | None) -> Path:
    import os

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


def _channels(low_hz: int, high_hz: int) -> list[tuple[int, int]]:
    out = []
    f = low_hz
    while f + CHANNEL_WIDTH <= high_hz:
        out.append((f, f + CHANNEL_WIDTH))
        f += CHANNEL_WIDTH
    return out


def _rc(items: list[dict[str, Any]]) -> list[int]:
    return [it.get("response", {}).get("responseCode", -1) for it in items]


def _inject_fixtures(
    client: httpx.Client, base_url: str, fcc_ids: list[str], user_id: str
) -> None:
    resp = client.post(f"{base_url}/admin/injectdata/user_id", json={"userId": user_id})
    resp.raise_for_status()
    for fcc in fcc_ids:
        resp = client.post(
            f"{base_url}/admin/injectdata/fcc_id",
            json={"fccId": fcc, "fccMaxEirp": 47},
        )
        resp.raise_for_status()


def _populate(
    client: httpx.Client,
    base_url: str,
    *,
    scenario: str,
    count: int,
    channels: list[tuple[int, int]],
    seed: int,
) -> float:
    """Register ``count`` devices and issue one grant each. Returns wall seconds."""
    t0 = time.perf_counter()
    user_id = f"pop_op_{scenario}"
    fixtures = [
        (f"pop_{scenario}_{seed}_{i}", f"sn_pop_{seed}_{i:05d}")
        for i in range(count)
    ]
    _inject_fixtures(client, base_url, [f for f, _ in fixtures], user_id)

    reg_items = [_reg_item(f, user_id, s) for f, s in fixtures]
    for chunk in chunked(reg_items, BATCH_MAX):
        resp = client.post(f"{base_url}/v1.2/registration", json={"registrationRequest": chunk})
        resp.raise_for_status()
        codes = _rc(resp.json()["registrationResponse"])
        bad = [c for c in codes if c != 0]
        if bad:
            raise RuntimeError(f"population registration failed: responseCode={sorted(set(bad))}")

    cbsd_ids = [f"{f}/{s}" for f, s in fixtures]
    grant_items = [
        _grant_item(cid, *channels[i % len(channels)])
        for i, cid in enumerate(cbsd_ids)
    ]
    for chunk in chunked(grant_items, BATCH_MAX):
        resp = client.post(f"{base_url}/v1.2/grant", json={"grantRequest": chunk})
        resp.raise_for_status()
        codes = _rc(resp.json()["grantResponse"])
        bad = [c for c in codes if c != 0]
        if bad:
            raise RuntimeError(f"population grant failed: responseCode={sorted(set(bad))}")
    return time.perf_counter() - t0


def _grant_probe(
    client: httpx.Client,
    base_url: str,
    cbsd_id: str,
    low: int,
    high: int,
) -> tuple[int, str | None, float]:
    """Single grant request; returns (responseCode, grantId, latency_ms)."""
    t0 = time.perf_counter()
    resp = client.post(
        f"{base_url}/v1.2/grant",
        json={"grantRequest": [_grant_item(cbsd_id, low, high)]},
    )
    latency = (time.perf_counter() - t0) * 1000.0
    resp.raise_for_status()
    body = resp.json()["grantResponse"][0]
    return (
        body.get("response", {}).get("responseCode", -1),
        body.get("grantId"),
        latency,
    )


def _relinquish(client: httpx.Client, base_url: str, cbsd_id: str, grant_id: str) -> None:
    resp = client.post(
        f"{base_url}/v1.2/relinquishment",
        json={"relinquishmentRequest": [{"cbsdId": cbsd_id, "grantId": grant_id}]},
    )
    resp.raise_for_status()


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs_dir = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs_dir)
    base_url = f"https://127.0.0.1:{args.port}"

    slice_share = (SLICE_HIGH - SLICE_LOW) / (CBRS_HIGH - CBRS_LOW)
    sliced_population = max(1, round(args.population * slice_share))

    scenarios = {
        "national": {
            "band_low": CBRS_LOW,
            "band_high": CBRS_HIGH,
            "population": args.population,
            "channels": _channels(CBRS_LOW, CBRS_HIGH),
        },
        "sliced": {
            "band_low": SLICE_LOW,
            "band_high": SLICE_HIGH,
            "population": sliced_population,
            "channels": _channels(SLICE_LOW, SLICE_HIGH),
        },
    }

    raw_path = out_dir / "slicing_results_raw.csv"
    summary_path = out_dir / "slicing_results.csv"
    manifest_path = out_dir / "slicing_manifest.json"

    failures = 0
    summary_rows: list[dict[str, Any]] = []
    grant_means: dict[str, float] = {}

    with raw_path.open("w", newline="", encoding="utf-8") as raw_fh:
        raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
        raw_writer.writeheader()

        def emit_raw(**kwargs: Any) -> None:
            row = {k: "" for k in RAW_FIELDS}
            row.update(
                {"run_id": new_run_id(), "timestamp_utc": utc_now_iso()}
            )
            row.update(kwargs)
            raw_writer.writerow(row)
            raw_fh.flush()

        for scenario, cfg in scenarios.items():
            container = f"sas-slicing-{scenario}"
            print(
                f"=== scenario {scenario}: band "
                f"{cfg['band_low']/1e6:.0f}-{cfg['band_high']/1e6:.0f} MHz, "
                f"population {cfg['population']} ===",
                flush=True,
            )
            spec = DockerRunSpec(
                image=args.image,
                name=container,
                host_port=args.port,
                certs_dir=certs_dir,
                band_profile="cbrs_winnforum",
                band_low_hz=cfg["band_low"],
                band_high_hz=cfg["band_high"],
            )
            try:
                docker_run(spec)
                ok, _, err = wait_http_200(f"{base_url}/ready", ctx, args.ready_timeout_sec)
                if not ok:
                    raise RuntimeError(f"readiness failed: {err}")

                with httpx.Client(verify=ctx, timeout=60.0) as client:
                    pop_secs = _populate(
                        client,
                        base_url,
                        scenario=scenario,
                        count=cfg["population"],
                        channels=cfg["channels"],
                        seed=args.seed,
                    )
                    print(f"  populated {cfg['population']} devices in {pop_secs:.1f}s", flush=True)
                    summary_rows.append(
                        {"scenario": scenario, "metric": "population_devices", "value": cfg["population"]}
                    )
                    summary_rows.append(
                        {"scenario": scenario, "metric": "population_wall_sec", "value": pop_secs}
                    )

                    # Probe CBSD used for boundary checks and latency probes.
                    probe_fcc = f"probe_{scenario}"
                    probe_user = f"probe_op_{scenario}"
                    probe_serial = "sn_probe_00001"
                    _inject_fixtures(client, base_url, [probe_fcc], probe_user)
                    resp = client.post(
                        f"{base_url}/v1.2/registration",
                        json={"registrationRequest": [_reg_item(probe_fcc, probe_user, probe_serial)]},
                    )
                    resp.raise_for_status()
                    reg_body = resp.json()["registrationResponse"][0]
                    if reg_body["response"]["responseCode"] != 0:
                        raise RuntimeError(f"probe registration failed: {reg_body}")
                    probe_id = reg_body["cbsdId"]

                    # --- Boundary enforcement (sliced scenario only) ---
                    if scenario == "sliced":
                        for case, (low, high), expected in (
                            ("grant_in_slice_3560mhz", IN_SLICE_GRANT, 0),
                            ("grant_out_of_slice_3600mhz", OUT_OF_SLICE_GRANT, UNSUPPORTED_SPECTRUM),
                        ):
                            code, grant_id, latency = _grant_probe(
                                client, base_url, probe_id, low, high
                            )
                            ok_case = code == expected
                            if not ok_case:
                                failures += 1
                            emit_raw(
                                scenario=scenario,
                                case=case,
                                iteration=1,
                                warmup=0,
                                low_hz=low,
                                high_hz=high,
                                response_code=code,
                                expected_code=expected,
                                latency_ms=latency,
                                exit_code=0 if ok_case else 1,
                                error="" if ok_case else f"expected {expected}, got {code}",
                            )
                            summary_rows.append(
                                {
                                    "scenario": scenario,
                                    "metric": f"{case}_response_code",
                                    "value": code,
                                }
                            )
                            summary_rows.append(
                                {
                                    "scenario": scenario,
                                    "metric": f"{case}_latency_ms",
                                    "value": latency,
                                }
                            )
                            print(
                                f"  {case}: rc={code} (expected {expected}) {latency:.1f}ms",
                                flush=True,
                            )
                            if grant_id:
                                _relinquish(client, base_url, probe_id, grant_id)

                    # --- Local efficiency: probe grant latency ---
                    probe_low, probe_high = SLICE_LOW, SLICE_LOW + CHANNEL_WIDTH
                    latencies: list[float] = []
                    total = args.warmup + args.n
                    for i in range(1, total + 1):
                        is_warmup = i <= args.warmup
                        code, grant_id, latency = _grant_probe(
                            client, base_url, probe_id, probe_low, probe_high
                        )
                        err_msg = ""
                        if code != 0:
                            err_msg = f"probe grant rc={code}"
                            if not is_warmup:
                                failures += 1
                        elif not is_warmup:
                            latencies.append(latency)
                        _, rss = docker_stats_sample(container)
                        emit_raw(
                            scenario=scenario,
                            case="probe_grant",
                            iteration=i,
                            warmup=int(is_warmup),
                            low_hz=probe_low,
                            high_hz=probe_high,
                            response_code=code,
                            expected_code=0,
                            latency_ms=latency,
                            rss_mb=rss if rss is not None else "",
                            exit_code=0 if code == 0 else 1,
                            error=err_msg,
                        )
                        if grant_id:
                            _relinquish(client, base_url, probe_id, grant_id)

                    if latencies:
                        grant_means[scenario] = statistics.fmean(latencies)
                        summary_rows += [
                            {"scenario": scenario, "metric": "probe_grant_mean_ms", "value": statistics.fmean(latencies)},
                            {"scenario": scenario, "metric": "probe_grant_p50_ms", "value": percentile(latencies, 50)},
                            {"scenario": scenario, "metric": "probe_grant_p95_ms", "value": percentile(latencies, 95)},
                            {"scenario": scenario, "metric": "probe_grant_samples", "value": len(latencies)},
                        ]
                        print(
                            f"  probe grant mean={statistics.fmean(latencies):.2f}ms "
                            f"p95={percentile(latencies, 95):.2f}ms",
                            flush=True,
                        )
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL [{scenario}]: {exc}", flush=True)
                summary_rows.append(
                    {"scenario": scenario, "metric": "scenario_error", "value": str(exc)[:200]}
                )
            finally:
                docker_rm(container)

    if "national" in grant_means and "sliced" in grant_means:
        national = grant_means["national"]
        sliced = grant_means["sliced"]
        reduction_pct = (national - sliced) / national * 100.0 if national else 0.0
        summary_rows.append(
            {"scenario": "ALL", "metric": "grant_latency_reduction_pct", "value": reduction_pct}
        )
        print(f"Grant latency reduction (sliced vs national): {reduction_pct:.2f}%")

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary_rows)

    manifest = capture_manifest(
        seed=args.seed,
        n=args.n,
        warmup=args.warmup,
        script_paths=[LOAD_DIR / "slicing_benchmark.py", LOAD_DIR / "common.py"],
        certs_dir=certs_dir,
        extra={
            "experiment": "spectrum_slicing",
            "image": {"tag": args.image, "digest": image_digest(args.image)},
            "port": args.port,
            "slice_hz": [SLICE_LOW, SLICE_HIGH],
            "national_band_hz": [CBRS_LOW, CBRS_HIGH],
            "population_national": args.population,
            "population_sliced": sliced_population,
            "failures": failures,
            "outputs": {
                "raw_csv": str(raw_path),
                "summary_csv": str(summary_path),
            },
        },
    )
    write_manifest(manifest_path, manifest)
    print(f"Wrote {raw_path}")
    print(f"Wrote {summary_path}")
    print(f"Wrote {manifest_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
