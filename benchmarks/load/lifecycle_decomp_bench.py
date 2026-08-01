"""Experimento A — decomposição do lifecycle CBSD completo.

Operações: REG → SIQ → GRA → HBT → RLQ → DRG
(conexão mTLS persistente, ``SAS_TIMING=1``).

  client_ms ≈ application_total_ms + external_ms
  application_total_ms ≈ sum(stage_*)
  application_non_db_ms = application_total_ms − database_ms

Estágios exclusivos empilháveis: stage_{handler,database,serialization,other}_ms.
Campos nested_* são diagnósticos e podem sobrepor.

Saídas: lifecycle_decomp_{raw,summary,manifest}.*
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx

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

OPS = (
    "registration",
    "spectrumInquiry",
    "grant",
    "heartbeat",
    "relinquishment",
    "deregistration",
)
STAGE_FIELDS = (
    "stage_handler_ms",
    "stage_database_ms",
    "stage_serialization_ms",
    "stage_other_ms",
)

RAW_FIELDS = [
    "run_id", "timestamp_utc", "profile_id", "cycle", "warmup", "operation",
    "client_ms", "application_total_ms", "application_non_db_ms", "database_ms",
    "external_ms",
    *STAGE_FIELDS,
    "nested_decision_ms", "nested_database_ms", "nested_serialization_ms",
    "lifecycle_total_ms", "contrib_pct",
    "http_status", "response_code", "request_bytes", "response_bytes",
    "connection_reused", "success",
    "exit_code", "error",
]
SUMMARY_FIELDS = [
    "profile_id", "operation", "n", "metric", "median", "iqr", "p95", "mean",
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


def _channel(profile: dict[str, Any]) -> tuple[int, int]:
    low = int(profile["band_low_hz"])
    width = int(profile.get("channel_width_hz") or 10_000_000)
    high = low + width
    band_high = int(profile["band_high_hz"])
    if high > band_high:
        high = band_high
        low = max(int(profile["band_low_hz"]), high - width)
    return low, high


def _parse_timing(resp: httpx.Response) -> dict[str, Any]:
    raw = resp.headers.get("X-SAS-Timing") or resp.headers.get("x-sas-timing")
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _post(
    client: httpx.Client, base: str, op: str, item: dict[str, Any]
) -> tuple[list[dict[str, Any]], float, dict[str, Any], httpx.Response]:
    key = {
        "registration": "registrationRequest",
        "spectrumInquiry": "spectrumInquiryRequest",
        "grant": "grantRequest",
        "heartbeat": "heartbeatRequest",
        "relinquishment": "relinquishmentRequest",
        "deregistration": "deregistrationRequest",
    }[op]
    resp_key = {
        "registration": "registrationResponse",
        "spectrumInquiry": "spectrumInquiryResponse",
        "grant": "grantResponse",
        "heartbeat": "heartbeatResponse",
        "relinquishment": "relinquishmentResponse",
        "deregistration": "deregistrationResponse",
    }[op]
    t0 = time.perf_counter()
    resp = client.post(f"{base}/v1.2/{op}", json={key: [item]})
    client_ms = (time.perf_counter() - t0) * 1000.0
    timing = _parse_timing(resp)
    body = resp.json() if resp.content else {}
    return body.get(resp_key) or [], client_ms, timing, resp


def _rc(items: list[dict[str, Any]]) -> int:
    if not items:
        return -1
    return items[0].get("response", {}).get("responseCode", -1)


def _iqr(vals: list[float]) -> float:
    return (percentile(vals, 75) or 0.0) - (percentile(vals, 25) or 0.0)


def _op_row(
    op: str,
    client_ms: float,
    tmg: dict[str, Any],
    resp: httpx.Response,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    app = float(tmg.get("application_total_ms") or 0.0)
    db = float(tmg.get("database_ms") or tmg.get("stage_database_ms") or 0.0)
    non_db = float(
        tmg.get("application_non_db_ms")
        or tmg.get("application_cpu_ms")
        or max(0.0, app - db)
    )
    req_bytes = tmg.get("request_bytes", "")
    resp_bytes = tmg.get("response_bytes", "")
    if resp_bytes == "" and resp.content is not None:
        resp_bytes = len(resp.content)
    rc = _rc(items)
    ok = resp.status_code == 200 and rc == 0
    row = {
        "operation": op,
        "client_ms": round(client_ms, 4),
        "application_total_ms": round(app, 4),
        "application_non_db_ms": round(non_db, 4),
        "database_ms": round(db, 4),
        "external_ms": round(max(0.0, client_ms - app), 4),
        "nested_decision_ms": round(float(tmg.get("nested_decision_ms") or 0.0), 4),
        "nested_database_ms": round(float(tmg.get("nested_database_ms") or db), 4),
        "nested_serialization_ms": round(
            float(tmg.get("nested_serialization_ms") or tmg.get("serialization_ms") or 0.0),
            4,
        ),
        "http_status": resp.status_code,
        "response_code": rc,
        "request_bytes": req_bytes if req_bytes != "" else "",
        "response_bytes": resp_bytes,
        "connection_reused": 1,  # persistent Client across cycle
        "success": int(ok),
    }
    for f in STAGE_FIELDS:
        row[f] = round(float(tmg.get(f) or 0.0), 4)
    # Fallback if older image without stages: derive exclusive approx
    if not any(float(row[f]) for f in STAGE_FIELDS) and app > 0:
        ser = float(tmg.get("serialization_ms") or 0.0)
        pol = float(tmg.get("policy_ms") or tmg.get("decision_ms") or 0.0)
        row["stage_database_ms"] = round(db, 4)
        row["stage_serialization_ms"] = round(ser, 4)
        row["stage_handler_ms"] = round(max(0.0, pol - db), 4)
        row["stage_other_ms"] = round(
            max(0.0, app - row["stage_handler_ms"] - db - ser), 4
        )
    return row


def _run_cycle(
    client: httpx.Client,
    base: str,
    profile: dict[str, Any],
    fcc: str,
    user: str,
    serial: str,
) -> list[dict[str, Any]]:
    low, high = _channel(profile)
    rows: list[dict[str, Any]] = []
    t_life = time.perf_counter()

    def one(op: str, item: dict[str, Any]) -> list[dict[str, Any]]:
        items, c_ms, tmg, resp = _post(client, base, op, item)
        if resp.status_code != 200:
            raise RuntimeError(f"{op} http={resp.status_code}")
        if _rc(items) != 0:
            raise RuntimeError(f"{op} rc={_rc(items)}")
        rows.append(_op_row(op, c_ms, tmg, resp, items))
        return items

    reg = one(
        "registration",
        {
            "userId": user,
            "fccId": fcc,
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
                "eirpCapability": float(profile.get("category_a_max_eirp_dbm") or 30),
            },
        },
    )
    cbsd_id = reg[0]["cbsdId"]
    one(
        "spectrumInquiry",
        {
            "cbsdId": cbsd_id,
            "inquiredSpectrum": [{
                "lowFrequency": int(profile["band_low_hz"]),
                "highFrequency": int(profile["band_high_hz"]),
            }],
        },
    )
    gr = one(
        "grant",
        {
            "cbsdId": cbsd_id,
            "operationParam": {
                "maxEirp": 20.0,
                "operationFrequencyRange": {
                    "lowFrequency": low,
                    "highFrequency": high,
                },
            },
        },
    )
    grant_id = gr[0]["grantId"]
    one(
        "heartbeat",
        {"cbsdId": cbsd_id, "grantId": grant_id, "operationState": "GRANTED"},
    )
    one(
        "relinquishment",
        {"cbsdId": cbsd_id, "grantId": grant_id},
    )
    one(
        "deregistration",
        {"cbsdId": cbsd_id},
    )

    life_ms = (time.perf_counter() - t_life) * 1000.0
    for r in rows:
        r["lifecycle_total_ms"] = round(life_ms, 4)
        r["contrib_pct"] = (
            round(100.0 * float(r["client_ms"]) / life_ms, 3) if life_ms else 0.0
        )
    return rows


def _summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metrics = (
        "client_ms",
        "application_total_ms",
        "application_non_db_ms",
        "database_ms",
        "external_ms",
        *STAGE_FIELDS,
        "contrib_pct",
    )
    out: list[dict[str, Any]] = []
    keys = sorted({
        (r["profile_id"], r["operation"])
        for r in rows
        if str(r.get("warmup")) in ("0", "False")
    })
    for pid, op in keys:
        subset = [
            r for r in rows
            if r["profile_id"] == pid
            and r["operation"] == op
            and str(r["warmup"]) in ("0", "False")
            and r["exit_code"] in (0, "0")
        ]
        for metric in metrics:
            vals = [float(r[metric]) for r in subset if r.get(metric) not in ("", None)]
            if not vals:
                continue
            out.append({
                "profile_id": pid,
                "operation": op,
                "n": len(vals),
                "metric": metric,
                "median": round(float(statistics.median(vals)), 4),
                "iqr": round(_iqr(vals), 4),
                "p95": round(percentile(vals, 95) or 0.0, 4),
                "mean": round(statistics.fmean(vals), 4),
            })
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--n", type=int, default=30)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--image", default="sas-agent:monolithic")
    p.add_argument("--port", type=int, default=19500)
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--profiles-dir", type=Path, default=PROFILES)
    p.add_argument("--profile", default=None, help="Run a single profile_id (optional)")
    p.add_argument("--out-dir", type=Path, default=DATA)
    p.add_argument("--skip-thermal-gate", action="store_true")
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs)
    profiles = sorted(args.profiles_dir.glob("*.json"))
    if args.profile:
        profiles = [
            pth for pth in profiles
            if json.loads(pth.read_text())["profile_id"] == args.profile
        ]
    if not profiles:
        raise SystemExit("no profiles")
    rng = random.Random(args.seed)
    profiles = list(profiles)
    rng.shuffle(profiles)

    if not args.skip_thermal_gate:
        wait_thermal_gate(
            out_path=out_dir / "thermal_gate_lifecycle_decomp.json",
            label="lifecycle_decomp",
        )

    raw_path = out_dir / "lifecycle_decomp_raw.csv"
    summary_path = out_dir / "lifecycle_decomp_summary.csv"
    all_rows: list[dict[str, Any]] = []
    failures = 0
    empty_fail = {f: "" for f in RAW_FIELDS if f not in (
        "run_id", "timestamp_utc", "profile_id", "cycle", "warmup",
        "operation", "exit_code", "error",
    )}

    with raw_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RAW_FIELDS)
        writer.writeheader()
        for path in profiles:
            pdata = json.loads(path.read_text(encoding="utf-8"))
            pid = pdata["profile_id"]
            name = f"sas-ldecomp-{pid[:20]}"
            print(f"=== lifecycle decomp {pid} ===", flush=True)
            docker_run(
                DockerRunSpec(
                    image=args.image,
                    name=name,
                    host_port=args.port,
                    certs_dir=certs,
                    band_profile_file=path,
                    database_path=f"/data/ldecomp_{new_run_id()}.db",
                    extra_env={"SAS_TIMING": "1"},
                )
            )
            base = f"https://127.0.0.1:{args.port}"
            try:
                ok, _, err = wait_http_200(f"{base}/ready", ctx, 90.0)
                if not ok:
                    raise RuntimeError(err)
                with httpx.Client(verify=ctx, timeout=30.0) as client:
                    client.post(
                        f"{base}/admin/injectdata/user_id",
                        json={"userId": f"op_{pid}"},
                    ).raise_for_status()
                    client.post(
                        f"{base}/admin/injectdata/fcc_id",
                        json={"fccId": f"fcc_{pid[:10]}", "fccMaxEirp": 47},
                    ).raise_for_status()
                    total = args.warmup + args.n
                    for cycle in range(1, total + 1):
                        warm = cycle <= args.warmup
                        try:
                            op_rows = _run_cycle(
                                client,
                                base,
                                pdata,
                                f"fcc_{pid[:10]}",
                                f"op_{pid}",
                                f"sn_{args.seed}_{cycle:05d}",
                            )
                            for op_row in op_rows:
                                row = {
                                    "run_id": new_run_id(),
                                    "timestamp_utc": utc_now_iso(),
                                    "profile_id": pid,
                                    "cycle": cycle,
                                    "warmup": int(warm),
                                    "exit_code": 0,
                                    "error": "",
                                    **op_row,
                                }
                                writer.writerow(row)
                                fh.flush()
                                if not warm:
                                    all_rows.append(row)
                        except Exception as exc:  # noqa: BLE001
                            failures += 1
                            writer.writerow({
                                **empty_fail,
                                "run_id": new_run_id(),
                                "timestamp_utc": utc_now_iso(),
                                "profile_id": pid,
                                "cycle": cycle,
                                "warmup": int(warm),
                                "operation": "cycle",
                                "exit_code": 1,
                                "error": str(exc)[:300],
                            })
                            fh.flush()
            finally:
                docker_rm(name)

    summary = _summarize(all_rows)
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)

    write_manifest(
        out_dir / "lifecycle_decomp_manifest.json",
        capture_manifest(
            seed=args.seed,
            n=args.n,
            warmup=args.warmup,
            script_paths=[LOAD_DIR / "lifecycle_decomp_bench.py"],
            certs_dir=certs,
            extra={
                "experiment": "lifecycle_decomp",
                "operations": list(OPS),
                "model": (
                    "client ≈ application_total + external; "
                    "application_total ≈ sum(stage_*); "
                    "application_non_db = application_total − database"
                ),
                "stages_exclusive": list(STAGE_FIELDS),
                "stage_semantics": (
                    "exclusive stack: handler+database+serialization+other; "
                    "auth/validation omitted (FastAPI/Pydantic before _run_op)"
                ),
                "nested_diagnostics": [
                    "nested_decision_ms",
                    "nested_database_ms",
                    "nested_serialization_ms",
                ],
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "profiles": [json.loads(p.read_text())["profile_id"] for p in profiles],
                "failures": failures,
            },
        ),
    )
    print(f"Wrote {raw_path}\nWrote {summary_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
