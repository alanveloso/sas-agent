"""Experimento A — decomposição do lifecycle CBSD completo.

Operações: REG → SIQ → GRA → HBT → RLQ → DRG
(conexão mTLS persistente, ``SAS_TIMING=1``).

  client_ms ≈ application_total_ms + external_ms
  application_total_ms ≈ sum(stage_*)
  application_non_db_ms = application_total_ms − database_ms

Estágios exclusivos empilháveis: stage_{handler,database,serialization,other}_ms.
Campos nested_* são diagnósticos e podem sobrepor.

Saídas: lifecycle_decomp_{raw,summary,manifest}.*

Modo sustentado (``--sustained-sec D``): carga contínua mista num único
agente. Registra N CBSDs + Grant, depois, durante D segundos, envia
Heartbeats periódicos (fases escalonadas), chegadas periódicas de
novos CBSDs (Registration + Grant) e Relinquishments ocasionais (CBSD ativo
escolhido com semente). Um único cliente HTTPS persistente e sequencial
(1 requisição em voo); requisições de item único, portanto client_ms é a
latência HTTP de cada requisição. Saídas: lifecycle_sustained_*.

Agendamento de Heartbeat (``--heartbeat-schedule-mode``):
  * ``respect_transmit_expire`` (padrão): taxa nominal ``heartbeatInterval``,
    limitada para enviar antes do ``transmitExpireTime`` anterior com margem
    ``--heartbeat-safety-margin-sec`` (cobre a resolução de 1 s do SAS).
  * ``fixed_rate``: modo histórico — próximo envio em ``due + interval``,
    sem usar ``transmitExpireTime`` (pode marcar ``missed=1`` com
    ``responseCode=0`` quando o prazo truncado cai antes da agenda).
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import os
import random
import statistics
import sys
import time
from datetime import datetime, timezone
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
    "experimental_unit", "n_independent_deployments", "p95_is_point_estimate",
]
SUSTAINED_RAW_FIELDS = [
    "run_id", "timestamp_utc", "profile_id", "event", "operation", "cbsd_index",
    "heartbeat_seq", "scheduled_sec", "sent_sec", "lateness_ms", "late",
    "transmit_margin_ms", "missed", "client_ms", "http_status", "response_code",
    "success", "error",
]
SUSTAINED_SUMMARY_FIELDS = ["profile_id", "operation", "metric", "value"]


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


def _registration_item(
    profile: dict[str, Any], fcc: str, user: str, serial: str
) -> dict[str, Any]:
    return {
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
    }


def _grant_item(cbsd_id: str, low: int, high: int) -> dict[str, Any]:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": 20.0,
            "operationFrequencyRange": {"lowFrequency": low, "highFrequency": high},
        },
    }


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

    reg = one("registration", _registration_item(profile, fcc, user, serial))
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
    gr = one("grant", _grant_item(cbsd_id, low, high))
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
                # One container per profile; n counts within-container cycles.
                "experimental_unit": "http_request_within_container",
                "n_independent_deployments": 1,
                "p95_is_point_estimate": 1,
            })
    return out


def _parse_utc(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _next_heartbeat_due(
    *,
    due: float,
    hb_sec: float,
    clock_now: float,
    tx_expire: datetime | None,
    utc_now: datetime,
    schedule_mode: str,
    safety_margin_sec: float,
) -> float:
    """Next heartbeat due time on the monotonic ``clock`` timeline.

    ``fixed_rate`` keeps the historical cadence ``due + hb_sec``.
    ``respect_transmit_expire`` also caps the due time so the send happens
    before the previous ``transmitExpireTime`` (1 s SAS resolution) minus an
    explicit safety margin. Does not change protocol semantics — only when
    the client chooses to send.
    """
    ideal = due + hb_sec
    if schedule_mode != "respect_transmit_expire" or tx_expire is None:
        return ideal
    remaining = (tx_expire - utc_now).total_seconds() - safety_margin_sec
    return max(clock_now, min(ideal, clock_now + remaining))


def _run_sustained(
    post: Any,
    profile: dict[str, Any],
    *,
    fcc: str,
    user: str,
    seed: int,
    initial_devices: int,
    duration_sec: float,
    heartbeat_interval_sec: float | None,
    arrival_interval_sec: float | None,
    relinquish_interval_sec: float | None,
    late_threshold_ms: float,
    heartbeat_schedule_mode: str = "respect_transmit_expire",
    heartbeat_safety_margin_sec: float = 1.0,
    clock: Any = time.perf_counter,
    sleep: Any = time.sleep,
    utcnow: Any = lambda: datetime.now(timezone.utc),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Sequential mixed load; ``post(op, item)`` has the signature of ``_post``."""
    if heartbeat_schedule_mode not in ("respect_transmit_expire", "fixed_rate"):
        raise ValueError(
            "heartbeat_schedule_mode must be 'respect_transmit_expire' or 'fixed_rate'"
        )
    low, high = _channel(profile)
    rng = random.Random(seed)
    rows: list[dict[str, Any]] = []
    devices: dict[int, dict[str, Any]] = {}
    counts = {"registered": 0, "granted": 0, "relinquished": 0, "arrivals": 0}
    server_hbi: list[Any] = []
    t_origin = clock()

    def call(event: str, op: str, idx: int, item: dict[str, Any],
             scheduled: float | None = None, hb_seq: Any = "") -> tuple[dict[str, Any], list]:
        sent = clock()
        lateness = max(0.0, (sent - scheduled) * 1000.0) if scheduled is not None else None
        row: dict[str, Any] = {
            "event": event, "operation": op, "cbsd_index": idx, "heartbeat_seq": hb_seq,
            "scheduled_sec": round(scheduled - t_origin, 4) if scheduled is not None else "",
            "sent_sec": round(sent - t_origin, 4),
            "lateness_ms": round(lateness, 4) if lateness is not None else "",
            "late": int(lateness > late_threshold_ms) if lateness is not None else "",
            "transmit_margin_ms": "", "missed": "", "client_ms": "", "http_status": "",
            "response_code": "", "success": 0, "error": "",
        }
        items: list = []
        try:
            items, c_ms, _tmg, resp = post(op, item)
            row["client_ms"] = round(c_ms, 4)
            row["http_status"] = resp.status_code
            row["response_code"] = _rc(items)
            row["success"] = int(resp.status_code == 200 and row["response_code"] == 0)
        except Exception as exc:  # noqa: BLE001
            row["error"] = f"{type(exc).__name__}: {exc}"[:300]
        rows.append(row)
        return row, items

    def admit(idx: int, event: str, scheduled: float | None = None) -> bool:
        serial = f"sn_sus_{seed}_{idx:05d}"
        reg, items = call(event, "registration", idx,
                          _registration_item(profile, fcc, user, serial), scheduled)
        if not reg["success"]:
            return False
        counts["registered"] += 1
        cbsd_id = items[0]["cbsdId"]
        gr, gitems = call(event, "grant", idx, _grant_item(cbsd_id, low, high))
        if not gr["success"]:
            return False
        counts["granted"] += 1
        server_hbi.append(gitems[0].get("heartbeatInterval"))
        devices[idx] = {"cbsd_id": cbsd_id, "grant_id": gitems[0]["grantId"],
                        "state": "GRANTED", "tx_expire": None, "hb_seq": 0}
        return True

    for i in range(initial_devices):
        admit(i, "initial")
    hb_sec = float(
        heartbeat_interval_sec
        or next((v for v in server_hbi if v), None)
        or profile.get("heartbeat_interval_sec")
        or 60
    )

    t0 = clock()
    end = t0 + duration_sec
    heap: list[tuple[float, int, str, int]] = []
    order = iter(range(1 << 62))

    def push(due: float, kind: str, idx: int = -1) -> None:
        heapq.heappush(heap, (due, next(order), kind, idx))

    initial = sorted(devices)
    for n, idx in enumerate(initial):
        push(t0 + n * hb_sec / len(initial), "heartbeat", idx)
    if arrival_interval_sec:
        push(t0 + arrival_interval_sec, "arrival")
    if relinquish_interval_sec:
        push(t0 + relinquish_interval_sec, "relinquish")
    next_idx = initial_devices

    while heap:
        due, _, kind, idx = heapq.heappop(heap)
        if due >= end - 1e-9:
            break
        wait = due - clock()
        if wait > 0:
            sleep(wait)
        if kind == "heartbeat":
            dev = devices.get(idx)
            if dev is None:
                continue
            dev["hb_seq"] += 1
            margin = (
                (dev["tx_expire"] - utcnow()).total_seconds() * 1000.0
                if dev["tx_expire"] is not None else None
            )
            row, items = call(
                "periodic", "heartbeat", idx,
                {"cbsdId": dev["cbsd_id"], "grantId": dev["grant_id"],
                 "operationState": dev["state"]},
                due, dev["hb_seq"],
            )
            row["transmit_margin_ms"] = round(margin, 1) if margin is not None else ""
            # missed = sent after previous transmitExpireTime (CBSD RF window).
            # Independent of SAS responseCode: the agent may still renew (0).
            row["missed"] = int(margin is not None and margin < 0)
            if row["success"]:
                dev["state"] = "AUTHORIZED"
                dev["tx_expire"] = _parse_utc(items[0].get("transmitExpireTime"))
            push(
                _next_heartbeat_due(
                    due=due,
                    hb_sec=hb_sec,
                    clock_now=clock(),
                    tx_expire=dev["tx_expire"],
                    utc_now=utcnow(),
                    schedule_mode=heartbeat_schedule_mode,
                    safety_margin_sec=heartbeat_safety_margin_sec,
                ),
                "heartbeat",
                idx,
            )
        elif kind == "arrival":
            counts["arrivals"] += 1
            if admit(next_idx, "arrival", due):
                push(clock(), "heartbeat", next_idx)
            next_idx += 1
            push(due + arrival_interval_sec, "arrival")
        elif kind == "relinquish":
            if devices:
                victim = rng.choice(sorted(devices))
                dev = devices.pop(victim)
                row, _ = call("occasional", "relinquishment", victim,
                              {"cbsdId": dev["cbsd_id"], "grantId": dev["grant_id"]}, due)
                counts["relinquished"] += int(row["success"])
            push(due + relinquish_interval_sec, "relinquish")

    # Planned cadence counts (deterministic schedule), independent of success.
    planned_arrivals = 0
    if arrival_interval_sec and arrival_interval_sec > 0:
        t = t0 + arrival_interval_sec
        while t < end - 1e-9:
            planned_arrivals += 1
            t += arrival_interval_sec
    planned_relinquishments = 0
    if relinquish_interval_sec and relinquish_interval_sec > 0:
        t = t0 + relinquish_interval_sec
        while t < end - 1e-9:
            planned_relinquishments += 1
            t += relinquish_interval_sec
    info = {
        **counts,
        "initial_devices": initial_devices,
        "active_at_end": len(devices),
        "heartbeat_interval_sec_used": hb_sec,
        "server_heartbeat_interval_sec": next((v for v in server_hbi if v), None),
        "heartbeat_schedule_mode": heartbeat_schedule_mode,
        "heartbeat_safety_margin_sec": heartbeat_safety_margin_sec,
        "arrival_interval_sec": arrival_interval_sec,
        "relinquish_interval_sec": relinquish_interval_sec,
        "planned_arrivals": planned_arrivals,
        "planned_relinquishments": planned_relinquishments,
        "arrivals_executed": counts["arrivals"],
        "relinquishments_executed": counts["relinquished"],
        "arrival_admit_failures": max(0, counts["arrivals"] - (
            counts["registered"] - initial_devices
        )),
        "window_sec_configured": duration_sec,
        "window_sec_measured": round(clock() - t0, 4),
    }
    return rows, info


def _summarize_sustained(rows: list[dict[str, Any]], profile_id: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def add(op: str, metric: str, value: Any) -> None:
        out.append({"profile_id": profile_id, "operation": op, "metric": metric, "value": value})

    for op in sorted({r["operation"] for r in rows}):
        sub = [r for r in rows if r["operation"] == op]
        sas_ok = sum(int(r["success"]) for r in sub)
        add(op, "requests", len(sub))
        add(op, "http_200", sum(1 for r in sub if r["http_status"] == 200))
        add(op, "transport_errors", sum(1 for r in sub if r["error"]))
        add(op, "sas_response_code_0", sas_ok)
        add(op, "success_rate", round(sas_ok / len(sub), 6))
        for rc in sorted({r["response_code"] for r in sub if r["response_code"] not in ("", 0)}):
            add(op, f"response_code_{rc}", sum(1 for r in sub if r["response_code"] == rc))
        lat = [float(r["client_ms"]) for r in sub if r["client_ms"] != ""]
        if lat:
            add(op, "client_ms_median", round(statistics.median(lat), 4))
            add(op, "client_ms_p95", round(percentile(lat, 95) or 0.0, 4))
            add(op, "client_ms_max", round(max(lat), 4))
        if op == "heartbeat":
            late = [float(r["lateness_ms"]) for r in sub if r["lateness_ms"] != ""]
            add(op, "late", sum(int(r["late"] or 0) for r in sub))
            add(op, "missed_transmit_expire", sum(int(r["missed"] or 0) for r in sub))
            if late:
                add(op, "lateness_ms_p95", round(percentile(late, 95) or 0.0, 4))
                add(op, "lateness_ms_max", round(max(late), 4))
    return out


def _main_sustained(args: argparse.Namespace, certs: Path, ctx: Any, out_dir: Path) -> int:
    path = next(
        (p for p in sorted(args.profiles_dir.glob("*.json"))
         if json.loads(p.read_text())["profile_id"] == (args.profile or "cbrs_winnforum")),
        None,
    )
    if path is None:
        raise SystemExit("profile not found")
    pdata = json.loads(path.read_text(encoding="utf-8"))
    pid = pdata["profile_id"]
    fcc, user = f"fcc_{pid[:10]}", f"op_{pid}"
    name = f"sas-lsustained-{pid[:20]}"
    if not args.skip_thermal_gate:
        wait_thermal_gate(out_path=out_dir / "thermal_gate_lifecycle_sustained.json",
                          label="lifecycle_sustained")
    docker_run(DockerRunSpec(
        image=args.image, name=name, host_port=args.port, certs_dir=certs,
        band_profile_file=path, database_path=f"/data/lsustained_{new_run_id()}.db",
    ))
    base = f"https://127.0.0.1:{args.port}"
    try:
        ok, _, err = wait_http_200(f"{base}/ready", ctx, 90.0)
        if not ok:
            raise RuntimeError(err)
        with httpx.Client(verify=ctx, timeout=30.0) as client:
            client.post(f"{base}/admin/injectdata/user_id", json={"userId": user}).raise_for_status()
            client.post(f"{base}/admin/injectdata/fcc_id",
                        json={"fccId": fcc, "fccMaxEirp": 47}).raise_for_status()
            rows, info = _run_sustained(
                lambda op, item: _post(client, base, op, item), pdata,
                fcc=fcc, user=user, seed=args.seed,
                initial_devices=args.sustained_devices,
                duration_sec=args.sustained_sec,
                heartbeat_interval_sec=args.heartbeat_interval_sec,
                arrival_interval_sec=args.arrival_interval_sec,
                relinquish_interval_sec=args.relinquish_interval_sec,
                late_threshold_ms=args.late_threshold_ms,
                heartbeat_schedule_mode=args.heartbeat_schedule_mode,
                heartbeat_safety_margin_sec=args.heartbeat_safety_margin_sec,
            )
    finally:
        docker_rm(name)

    raw_path = out_dir / "lifecycle_sustained_raw.csv"
    summary_path = out_dir / "lifecycle_sustained_summary.csv"
    with raw_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUSTAINED_RAW_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({"run_id": new_run_id(), "timestamp_utc": utc_now_iso(),
                        "profile_id": pid, **r})
    summary = _summarize_sustained(rows, pid)
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUSTAINED_SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)
    failures = sum(1 for r in rows if not r["success"])
    write_manifest(
        out_dir / "lifecycle_sustained_manifest.json",
        capture_manifest(
            seed=args.seed, n=args.sustained_devices, warmup=0,
            script_paths=[LOAD_DIR / "lifecycle_decomp_bench.py"], certs_dir=certs,
            extra={
                "experiment": "lifecycle_sustained",
                "profile_id": pid,
                "parameters": {
                    "initial_devices": args.sustained_devices,
                    "duration_sec": args.sustained_sec,
                    "heartbeat_interval_sec_cli": args.heartbeat_interval_sec,
                    "heartbeat_schedule_mode": args.heartbeat_schedule_mode,
                    "heartbeat_safety_margin_sec": args.heartbeat_safety_margin_sec,
                    "heartbeat_schedule": (
                        "respect_transmit_expire: nominal heartbeatInterval per CBSD, "
                        "capped to send before previous transmitExpireTime minus "
                        f"{args.heartbeat_safety_margin_sec}s safety margin "
                        "(covers SAS 1 s timestamp resolution); initial phases "
                        "staggered evenly over one interval; arrivals start at once"
                        if args.heartbeat_schedule_mode == "respect_transmit_expire"
                        else "fixed_rate (historical): due + heartbeatInterval only; "
                             "initial phases staggered; does not use transmitExpireTime"
                    ),
                    "arrival_interval_sec": args.arrival_interval_sec,
                    "arrival_pattern": "periodic (deterministic); each = Registration + Grant",
                    "relinquish_interval_sec": args.relinquish_interval_sec,
                    "relinquish_selection": "seeded uniform choice among active CBSDs; "
                                            "relinquished CBSD stops heartbeating",
                    "late_threshold_ms": args.late_threshold_ms,
                },
                "outcome": info,
                "request_concurrency": {"max_inflight_requests": 1,
                                        "model": "single sequential client, one item per request"},
                "connection_policy": "one persistent httpx.Client (mTLS keep-alive) for the "
                                     "whole run; reconnects only if the server closes",
                "metric_semantics": {
                    "client_ms": "HTTP round-trip of one single-item request (not amortized)",
                    "success": "HTTP 200 and SAS responseCode 0 (reported separately)",
                    "lateness_ms": "send time minus scheduled time (agenda delay)",
                    "late": "lateness_ms above late_threshold_ms",
                    "missed": "heartbeat sent after the previous transmitExpireTime "
                              "(1 s SAS resolution); CBSD RF-window breach, independent "
                              "of responseCode — SAS may still renew with code 0",
                    "transmit_margin_ms": "previous transmitExpireTime minus send-time UTC; "
                                         "negative means missed",
                },
                "image": {"tag": args.image, "digest": image_digest(args.image)},
                "failures": failures,
            },
        ),
    )
    print(f"Wrote {raw_path}\nWrote {summary_path}")
    for s in summary:
        print(f"  {s['operation']:15s} {s['metric']:24s} {s['value']}")
    return 1 if failures else 0


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
    s = p.add_argument_group("sustained mixed load (lifecycle_sustained_* outputs)")
    s.add_argument("--sustained-sec", type=float, default=None,
                   help="enable sustained mode with this load window")
    s.add_argument("--sustained-devices", type=int, default=50)
    s.add_argument("--heartbeat-interval-sec", type=float, default=None,
                   help="default: heartbeatInterval returned by Grant")
    s.add_argument(
        "--heartbeat-schedule-mode",
        choices=("respect_transmit_expire", "fixed_rate"),
        default="respect_transmit_expire",
        help="respect_transmit_expire (default) caps the nominal interval using the "
             "previous transmitExpireTime; fixed_rate is the historical due+interval "
             "cadence",
    )
    s.add_argument(
        "--heartbeat-safety-margin-sec",
        type=float,
        default=1.0,
        help="seconds before transmitExpireTime to target the next send "
             "(respect_transmit_expire only); default 1.0 covers SAS 1 s resolution",
    )
    s.add_argument("--arrival-interval-sec", type=float, default=10.0)
    s.add_argument("--relinquish-interval-sec", type=float, default=30.0)
    s.add_argument("--late-threshold-ms", type=float, default=1000.0)
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    certs = _resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs)
    if args.sustained_sec is not None:
        return _main_sustained(args, certs, ctx, out_dir)
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
