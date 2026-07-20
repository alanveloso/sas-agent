"""Fase 2 — cross-profile lifecycle benchmark against the Docker agent (mTLS).

For each BandProfile JSON in ``benchmarks/profiles/`` a fresh ``sas-agent``
container is started with the profile injected via ``BAND_PROFILE_FILE`` and a
clean SQLite database. The script measures:

- container cold start (docker run -> first mTLS 200 on /ready);
- per-step latency of the full CBSD lifecycle over HTTPS/mTLS
  (registration -> spectrumInquiry -> grant -> heartbeat -> relinquishment ->
  deregistration), N measured cycles after warmup;
- container RSS via ``docker stats`` sampled once per measured cycle;
- cross-profile coefficient of variation (CV) of the per-profile means,
  the empirical evidence of regulatory agnosticism.

Outputs: profile_lifecycle_raw.csv, profile_lifecycle.csv (summary) and
profile_lifecycle_manifest.json in ``benchmarks/data/``.
"""

from __future__ import annotations

import argparse
import csv
import json
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
    DEFAULT_SEED,
    DockerRunSpec,
    build_mtls_context,
    capture_manifest,
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
PROFILES_DIR = BENCH_DIR / "profiles"

PHASES = (
    "registration",
    "spectrum_inquiry",
    "grant",
    "heartbeat",
    "relinquishment",
    "deregistration",
)

ENDPOINTS = {
    "registration": ("/v1.2/registration", "registrationRequest", "registrationResponse"),
    "spectrum_inquiry": ("/v1.2/spectrumInquiry", "spectrumInquiryRequest", "spectrumInquiryResponse"),
    "grant": ("/v1.2/grant", "grantRequest", "grantResponse"),
    "heartbeat": ("/v1.2/heartbeat", "heartbeatRequest", "heartbeatResponse"),
    "relinquishment": ("/v1.2/relinquishment", "relinquishmentRequest", "relinquishmentResponse"),
    "deregistration": ("/v1.2/deregistration", "deregistrationRequest", "deregistrationResponse"),
}

RAW_FIELDS = [
    "run_id",
    "timestamp_utc",
    "profile_id",
    "cycle",
    "warmup",
    *(f"lat_{p}_ms" for p in PHASES),
    "lat_cycle_total_ms",
    "rss_mb",
    "exit_code",
    "error",
]

SUMMARY_FIELDS = ["profile_id", "metric", "value"]


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Cross-profile lifecycle benchmark (Docker + mTLS)",
    )
    p.add_argument("--n", type=int, default=30, help="measured cycles per profile")
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--port", type=int, default=19010)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--profiles-dir", type=Path, default=PROFILES_DIR)
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


def _channel(profile: dict[str, Any], idx: int) -> tuple[int, int]:
    """Channel safely inside the band, mirroring benchmarks/lifecycle_worker.py."""
    low_hz = int(profile["band_low_hz"])
    high_hz = int(profile["band_high_hz"])
    width = int(profile["channel_width_hz"])
    n = max((high_hz - low_hz) // width, 1)
    ch = max(0, min(n - 1, idx))
    low = low_hz + width * ch
    if low + width > high_hz:
        low = high_hz - width
    return low, low + width


def _reg_req(fcc_id: str, user_id: str, serial: str) -> dict[str, Any]:
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
            # No eirpCapability: profiles with wide EIRP reference bandwidth
            # (e.g. br_anatel_privativa, 100 MHz -> 20 dB offset) would cap the
            # allowed EIRP density below the 20 dBm/MHz used in grants.
        },
    }


def _rc(items: list[dict[str, Any]]) -> int:
    return items[0].get("response", {}).get("responseCode", -1)


def _post(
    client: httpx.Client,
    base_url: str,
    phase: str,
    req: dict[str, Any],
) -> tuple[list[dict[str, Any]], float]:
    """POST a single-item batch; return (responses, latency_ms)."""
    path, in_key, out_key = ENDPOINTS[phase]
    t0 = time.perf_counter()
    resp = client.post(f"{base_url}{path}", json={in_key: [req]})
    latency = (time.perf_counter() - t0) * 1000.0
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code} in {phase}: {resp.text[:300]}")
    return resp.json()[out_key], latency


def _run_cycle(
    client: httpx.Client,
    base_url: str,
    profile: dict[str, Any],
    fcc_id: str,
    user_id: str,
    serial: str,
    low: int,
    high: int,
) -> dict[str, float]:
    timings: dict[str, float] = {}
    t_total = time.perf_counter()

    reg, timings["registration"] = _post(
        client, base_url, "registration", _reg_req(fcc_id, user_id, serial)
    )
    if _rc(reg) != 0:
        raise RuntimeError(f"registration rc={_rc(reg)} body={reg}")
    cbsd_id = reg[0]["cbsdId"]

    si, timings["spectrum_inquiry"] = _post(
        client,
        base_url,
        "spectrum_inquiry",
        {"cbsdId": cbsd_id, "inquiredSpectrum": [{"lowFrequency": low, "highFrequency": high}]},
    )
    if _rc(si) != 0:
        raise RuntimeError(f"spectrum_inquiry rc={_rc(si)} body={si}")

    gr, timings["grant"] = _post(
        client,
        base_url,
        "grant",
        {
            "cbsdId": cbsd_id,
            "operationParam": {
                "maxEirp": 20.0,
                "operationFrequencyRange": {"lowFrequency": low, "highFrequency": high},
            },
        },
    )
    if _rc(gr) != 0:
        raise RuntimeError(f"grant rc={_rc(gr)} body={gr}")
    grant_id = gr[0]["grantId"]

    hb, timings["heartbeat"] = _post(
        client,
        base_url,
        "heartbeat",
        {"cbsdId": cbsd_id, "grantId": grant_id, "operationState": "GRANTED"},
    )
    if _rc(hb) != 0:
        raise RuntimeError(f"heartbeat rc={_rc(hb)} body={hb}")

    rel, timings["relinquishment"] = _post(
        client, base_url, "relinquishment", {"cbsdId": cbsd_id, "grantId": grant_id}
    )
    if _rc(rel) != 0:
        raise RuntimeError(f"relinquishment rc={_rc(rel)} body={rel}")

    dereg, timings["deregistration"] = _post(
        client, base_url, "deregistration", {"cbsdId": cbsd_id}
    )
    if _rc(dereg) != 0:
        raise RuntimeError(f"deregistration rc={_rc(dereg)} body={dereg}")

    timings["cycle_total"] = (time.perf_counter() - t_total) * 1000.0
    return timings


def _inject_fixtures(
    client: httpx.Client, base_url: str, fcc_id: str, user_id: str
) -> None:
    resp = client.post(f"{base_url}/admin/injectdata/user_id", json={"userId": user_id})
    resp.raise_for_status()
    resp = client.post(
        f"{base_url}/admin/injectdata/fcc_id",
        json={"fccId": fcc_id, "fccMaxEirp": 47},
    )
    resp.raise_for_status()


def _cv_pct(values: list[float]) -> float | None:
    clean = [v for v in values if v is not None]
    if len(clean) < 2:
        return None
    mean = statistics.fmean(clean)
    if mean == 0:
        return None
    return statistics.stdev(clean) / mean * 100.0


def main() -> int:
    args = _parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs_dir = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs_dir)

    profile_files = sorted(args.profiles_dir.glob("*.json"))
    if not profile_files:
        raise SystemExit(f"No profile JSON files in {args.profiles_dir}")

    raw_path = out_dir / "profile_lifecycle_raw.csv"
    summary_path = out_dir / "profile_lifecycle.csv"
    manifest_path = out_dir / "profile_lifecycle_manifest.json"

    base_url = f"https://127.0.0.1:{args.port}"
    failures = 0
    # profile_id -> metric -> per-profile aggregate (used for the ALL/CV rows)
    profile_means: dict[str, dict[str, float]] = {}
    summary_rows: list[dict[str, Any]] = []

    with raw_path.open("w", newline="", encoding="utf-8") as raw_fh:
        raw_writer = csv.DictWriter(raw_fh, fieldnames=RAW_FIELDS)
        raw_writer.writeheader()

        for profile_path in profile_files:
            profile = json.loads(profile_path.read_text(encoding="utf-8"))
            profile_id = profile["profile_id"]
            container = f"sas-lifecycle-{profile_id}"
            print(f"=== profile {profile_id} ({profile_path.name}) ===", flush=True)

            spec = DockerRunSpec(
                image=args.image,
                name=container,
                host_port=args.port,
                certs_dir=certs_dir,
                band_profile_file=profile_path,
            )

            t0 = time.perf_counter()
            cold_start_ms: float | None = None
            profile_error = ""
            cycle_rows: list[dict[str, Any]] = []
            rss_samples: list[float] = []
            try:
                docker_run(spec)
                ok, _, err = wait_http_200(
                    f"{base_url}/ready", ctx, args.ready_timeout_sec
                )
                cold_start_ms = (time.perf_counter() - t0) * 1000.0
                if not ok:
                    raise RuntimeError(f"readiness failed: {err}")

                with httpx.Client(verify=ctx, timeout=30.0) as client:
                    fcc_id = f"bench_{profile_id[:12]}"
                    user_id = "bench_operator"
                    _inject_fixtures(client, base_url, fcc_id, user_id)
                    low, high = _channel(profile, 1)

                    total = args.warmup + args.n
                    for cycle in range(1, total + 1):
                        is_warmup = cycle <= args.warmup
                        serial = f"sn_{args.seed}_{cycle:05d}"
                        row: dict[str, Any] = {
                            "run_id": new_run_id(),
                            "timestamp_utc": utc_now_iso(),
                            "profile_id": profile_id,
                            "cycle": cycle,
                            "warmup": int(is_warmup),
                            "exit_code": 0,
                            "error": "",
                        }
                        try:
                            timings = _run_cycle(
                                client, base_url, profile,
                                fcc_id, user_id, serial, low, high,
                            )
                            for phase in PHASES:
                                row[f"lat_{phase}_ms"] = timings[phase]
                            row["lat_cycle_total_ms"] = timings["cycle_total"]
                        except Exception as exc:  # noqa: BLE001
                            row["exit_code"] = 1
                            row["error"] = str(exc)[:400]
                            if not is_warmup:
                                failures += 1
                        _, rss = docker_stats_sample(container)
                        if rss is not None:
                            row["rss_mb"] = rss
                            if not is_warmup:
                                rss_samples.append(rss)
                        raw_writer.writerow(row)
                        raw_fh.flush()
                        if not is_warmup:
                            cycle_rows.append(row)
                        if cycle % 10 == 0 or cycle == total:
                            print(f"  cycle {cycle}/{total}", flush=True)
            except Exception as exc:  # noqa: BLE001
                profile_error = str(exc)
                failures += 1
                print(f"FAIL [{profile_id}]: {exc}", flush=True)
            finally:
                docker_rm(container)

            means: dict[str, float] = {}
            ok_rows = [r for r in cycle_rows if r["exit_code"] == 0]
            for phase in (*PHASES, "cycle_total"):
                vals = [r[f"lat_{phase}_ms"] for r in ok_rows if f"lat_{phase}_ms" in r]
                if vals:
                    means[f"lat_{phase}_mean_ms"] = statistics.fmean(vals)
                    summary_rows += [
                        {"profile_id": profile_id, "metric": f"lat_{phase}_mean_ms", "value": statistics.fmean(vals)},
                        {"profile_id": profile_id, "metric": f"lat_{phase}_p50_ms", "value": percentile(vals, 50)},
                        {"profile_id": profile_id, "metric": f"lat_{phase}_p95_ms", "value": percentile(vals, 95)},
                    ]
            if cold_start_ms is not None:
                means["cold_start_ms"] = cold_start_ms
                summary_rows.append(
                    {"profile_id": profile_id, "metric": "cold_start_ms", "value": cold_start_ms}
                )
            if rss_samples:
                means["rss_mb_peak"] = max(rss_samples)
                summary_rows += [
                    {"profile_id": profile_id, "metric": "rss_mb_mean", "value": statistics.fmean(rss_samples)},
                    {"profile_id": profile_id, "metric": "rss_mb_peak", "value": max(rss_samples)},
                ]
            summary_rows += [
                {"profile_id": profile_id, "metric": "cycles_ok", "value": len(ok_rows)},
                {"profile_id": profile_id, "metric": "cycles_failed", "value": len(cycle_rows) - len(ok_rows)},
            ]
            if profile_error:
                summary_rows.append(
                    {"profile_id": profile_id, "metric": "profile_error", "value": profile_error[:200]}
                )
            profile_means[profile_id] = means

    # Cross-profile CV of per-profile means (agnosticism evidence).
    cv_metrics = [
        *(f"lat_{p}_mean_ms" for p in (*PHASES, "cycle_total")),
        "cold_start_ms",
        "rss_mb_peak",
    ]
    for metric in cv_metrics:
        values = [m[metric] for m in profile_means.values() if metric in m]
        cv = _cv_pct(values)
        if cv is not None:
            summary_rows.append(
                {"profile_id": "ALL", "metric": f"cv_{metric}_pct", "value": cv}
            )

    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(summary_rows)

    manifest = capture_manifest(
        seed=args.seed,
        n=args.n,
        warmup=args.warmup,
        script_paths=[LOAD_DIR / "profile_lifecycle.py", LOAD_DIR / "common.py"],
        certs_dir=certs_dir,
        extra={
            "experiment": "profile_lifecycle",
            "image": {"tag": args.image, "digest": image_digest(args.image)},
            "port": args.port,
            "profiles": [p.name for p in profile_files],
            "non_warmup_failures": failures,
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
    for metric in cv_metrics:
        row = next(
            (r for r in summary_rows if r["profile_id"] == "ALL" and r["metric"] == f"cv_{metric}_pct"),
            None,
        )
        if row:
            print(f"CV {metric}: {row['value']:.2f}%")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
