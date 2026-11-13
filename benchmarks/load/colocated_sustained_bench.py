"""Co-located sustained load: SQLite + persistent mTLS + multi-client comparison.

Comparative design (client count and offered load held constant)
----------------------------------------------------------------
* **Scenario A (single agent):** K=1 SAS Agent; N_clients persistent mTLS
  clients; each client owns ``devices_per_client`` CBSDs; all clients target
  the same agent. Max in-flight ≈ N_clients.
* **Scenario B (co-location):** K=N_clients SAS Agents on one host; same
  N_clients; client *i* targets agent *i* with an independent SQLite file.
  Max in-flight ≈ N_clients.

Default article comparison: N_clients=5, devices_per_client=300 (1500 total),
identical BandProfile on every agent, identical schedule parameters and
paired seeds. Co-location ≠ multi-host deployment.

Reuses ``lifecycle_decomp_bench._run_sustained`` and Docker helpers from
``common`` / ``container_concurrency_bench`` (stats sampler). Historical
runners are untouched.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
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
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

import container_concurrency_bench as ccb  # noqa: E402
import lifecycle_decomp_bench as ldb  # noqa: E402
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
    utc_now_iso,
    wait_http_200,
    wait_thermal_gate,
    write_manifest,
)

CBRS_LOW = 3_550_000_000
CBRS_HIGH = 3_700_000_000
PROFILE_ID = "cbrs_winnforum"
STATS_INTERVAL_SEC = 0.5

RAW_FIELDS = [
    "run_id", "timestamp_utc", "scenario", "repeat", "pair_id", "k_agents",
    "n_clients", "client_id", "agent_id", "profile_id", "event", "operation",
    "cbsd_index", "heartbeat_seq", "scheduled_sec", "sent_sec", "lateness_ms",
    "late", "transmit_margin_ms", "missed", "client_ms", "http_status",
    "response_code", "success", "error", "connection_reused", "inflight_observed",
]
RUN_FIELDS = [
    "run_id", "timestamp_utc", "scenario", "repeat", "pair_id", "k_agents",
    "n_clients", "devices_per_client", "total_devices", "seed",
    "create_ready_sec", "sustained_wall_sec", "teardown_sec",
    "max_inflight_configured", "max_inflight_observed",
    "connections_established_sum", "connection_reuse_events_sum",
    "requests_completed", "http_200_sum", "sas_ok_sum",
    "heartbeat_late_sum", "missed_transmit_expire_sum",
    "registered_sum", "granted_sum", "arrivals_sum", "relinquished_sum",
    "ram_peak_mb_sum", "ram_after_load_mb_sum", "cpu_pct_peak_sum",
    "stats_sweeps", "stats_interval_sec", "temp_c_start", "temp_c_end",
    "load1_start", "exit_code", "error", "failure_class",
]
SUMMARY_FIELDS = [
    "scenario", "repeat", "pair_id", "k_agents", "operation", "metric", "value",
]
CLIENT_FIELDS = [
    "run_id", "scenario", "repeat", "pair_id", "client_id", "agent_id",
    "devices", "seed", "requests", "sas_ok", "http_200", "registered",
    "granted", "arrivals", "relinquished", "hb_late", "missed",
    "connections_established", "connection_reuse_events",
    "client_ms_median", "client_ms_p95",
]


class _InflightGauge:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._n = 0
        self.max_observed = 0

    def __enter__(self) -> "_InflightGauge":
        with self._lock:
            self._n += 1
            self.max_observed = max(self.max_observed, self._n)
        return self

    def __exit__(self, *exc: object) -> None:
        with self._lock:
            self._n -= 1


def client_seed(base_seed: int, client_id: int) -> int:
    """Paired per-client seed (identical across scenarios A and B)."""
    return int(base_seed) + int(client_id) * 10_000


def agent_for_client(k_agents: int, client_id: int) -> int:
    """Map client → agent. K=1 → all on agent 0; K=N → client i on agent i."""
    if k_agents < 1:
        raise ValueError("k_agents must be >= 1")
    if k_agents == 1:
        return 0
    if client_id < 0 or client_id >= k_agents:
        raise ValueError(
            f"with k_agents={k_agents}, client_id must be in [0, {k_agents})"
        )
    return client_id


def shared_profile() -> dict[str, Any]:
    """Identical BandProfile bounds for every agent/client (fair load)."""
    return {
        "profile_id": PROFILE_ID,
        "band_low_hz": CBRS_LOW,
        "band_high_hz": CBRS_HIGH,
        "channel_width_hz": 10_000_000,
        "heartbeat_interval_sec": 60,
    }


def _post_tracked(
    client: httpx.Client,
    base: str,
    op: str,
    item: dict[str, Any],
    *,
    gauge: _InflightGauge,
    conn_state: dict[str, Any],
) -> tuple[list[dict[str, Any]], float, dict[str, Any], httpx.Response]:
    with gauge:
        items, client_ms, tmg, resp = ldb._post(client, base, op, item)
    established = int(conn_state.get("established", 0))
    if established == 0:
        conn_state["established"] = 1
        reused = False
    else:
        reused = True
        conn_state["reused"] = int(conn_state.get("reused", 0)) + 1
    conn_state["last_reused"] = reused
    return items, client_ms, tmg, resp


def _run_one_client(
    *,
    port: int,
    ctx: Any,
    client_id: int,
    agent_id: int,
    devices_per_client: int,
    seed: int,
    duration_sec: float,
    heartbeat_interval_sec: float | None,
    arrival_interval_sec: float | None,
    relinquish_interval_sec: float | None,
    late_threshold_ms: float,
    heartbeat_schedule_mode: str,
    heartbeat_safety_margin_sec: float,
    gauge: _InflightGauge,
    inject_lock: threading.Lock | None,
) -> dict[str, Any]:
    """One persistent mTLS client driving ``devices_per_client`` CBSDs."""
    base = f"https://127.0.0.1:{port}"
    profile = shared_profile()
    fcc, user = f"csust_fcc_c{client_id}", f"csust_op_c{client_id}"
    conn_state: dict[str, Any] = {"established": 0, "reused": 0}
    error = ""
    try:
        with httpx.Client(verify=ctx, timeout=60.0) as client:
            # Serialize admin inject when many clients share one agent (A).
            lock = inject_lock or threading.Lock()
            with lock:
                client.post(
                    f"{base}/admin/injectdata/user_id", json={"userId": user}
                ).raise_for_status()
                client.post(
                    f"{base}/admin/injectdata/fcc_id",
                    json={"fccId": fcc, "fccMaxEirp": 47},
                ).raise_for_status()

            def post(op: str, item: dict[str, Any]):
                return _post_tracked(
                    client, base, op, item, gauge=gauge, conn_state=conn_state
                )

            rows, info = ldb._run_sustained(
                post,
                profile,
                fcc=fcc,
                user=user,
                seed=seed,
                initial_devices=devices_per_client,
                duration_sec=duration_sec,
                heartbeat_interval_sec=heartbeat_interval_sec,
                arrival_interval_sec=arrival_interval_sec,
                relinquish_interval_sec=relinquish_interval_sec,
                late_threshold_ms=late_threshold_ms,
                heartbeat_schedule_mode=heartbeat_schedule_mode,
                heartbeat_safety_margin_sec=heartbeat_safety_margin_sec,
            )
    except Exception as exc:  # noqa: BLE001
        rows, info = [], {}
        error = f"{type(exc).__name__}: {exc}"[:400]

    for i, r in enumerate(rows):
        r["client_id"] = client_id
        r["agent_id"] = agent_id
        r["connection_reused"] = int(i > 0)
        r["inflight_observed"] = ""
    return {
        "client_id": client_id,
        "agent_id": agent_id,
        "seed": seed,
        "devices": devices_per_client,
        "rows": rows,
        "info": info,
        "connections_established": int(conn_state.get("established", 0)),
        "connection_reuse_events": int(conn_state.get("reused", 0)),
        "error": error,
    }


def run_scenario(
    *,
    scenario: str,
    k_agents: int,
    n_clients: int,
    devices_per_client: int,
    seed: int,
    image: str,
    base_port: int,
    certs: Path,
    ctx: Any,
    ready_timeout: float,
    duration_sec: float,
    heartbeat_interval_sec: float | None,
    arrival_interval_sec: float | None,
    relinquish_interval_sec: float | None,
    late_threshold_ms: float,
    heartbeat_schedule_mode: str,
    heartbeat_safety_margin_sec: float,
    repeat: int,
    pair_id: int,
    skip_thermal_gate: bool,
    thermal_timeout_sec: float,
    out_dir: Path,
) -> dict[str, Any]:
    """Execute one scenario (A or B). Raises SystemExit on thermal-gate failure."""
    if scenario not in ("A", "B"):
        raise ValueError("scenario must be A or B")
    if scenario == "A" and k_agents != 1:
        raise ValueError("scenario A requires k_agents=1")
    if scenario == "B" and k_agents != n_clients:
        raise ValueError("scenario B requires k_agents == n_clients")

    out_dir.mkdir(parents=True, exist_ok=True)
    if not skip_thermal_gate:
        gate = wait_thermal_gate(
            out_path=out_dir / f"thermal_gate_{scenario}_p{pair_id}_r{repeat}.json",
            label=f"coloc_sust_{scenario}_p{pair_id}_r{repeat}",
            timeout_sec=thermal_timeout_sec,
        )
        if not gate.get("passed"):
            raise SystemExit(
                f"THERMAL_GATE_FAILED scenario={scenario} pair={pair_id} "
                f"repeat={repeat} final={gate.get('final')}"
            )

    total_devices = n_clients * devices_per_client
    names = [f"sas-csust-{scenario.lower()}-a{i}-p{pair_id}-r{repeat}" for i in range(k_agents)]
    ports = [base_port + i for i in range(k_agents)]
    run_id = new_run_id()
    snap0 = host_load_snapshot()
    temp_start = snap0.get("temperature_tctl_c") or snap0.get("temperature_max_c")
    load1_start = snap0.get("loadavg_1m")
    gauge = _InflightGauge()
    inject_lock = threading.Lock()  # used when K=1 (shared agent)
    error = ""
    exit_code = 0
    failure_class = ""
    create_ready = float("nan")
    sustained_wall = float("nan")
    teardown = float("nan")
    client_results: list[dict[str, Any]] = []
    ram_peak = None
    cpu_peak = None
    stats_sweeps = 0
    ram_after = None

    try:
        t_create = time.perf_counter()
        for i in range(k_agents):
            docker_run(
                DockerRunSpec(
                    image=image,
                    name=names[i],
                    host_port=ports[i],
                    certs_dir=certs,
                    database_path=f"/data/csust_{run_id}_a{i}.db",
                    band_profile=PROFILE_ID,
                    # Same band for every agent (fair offered load / profile).
                    band_low_hz=CBRS_LOW,
                    band_high_hz=CBRS_HIGH,
                )
            )
        for i, port in enumerate(ports):
            ok, _, err = wait_http_200(
                f"https://127.0.0.1:{port}/ready", ctx, ready_timeout
            )
            if not ok:
                raise RuntimeError(f"infra: {names[i]} not ready: {err}")
        create_ready = time.perf_counter() - t_create

        with ccb._DockerStatsSampler(names, interval=STATS_INTERVAL_SEC) as sampler:
            t_load = time.perf_counter()
            with ThreadPoolExecutor(max_workers=n_clients) as pool:
                futs = []
                for c in range(n_clients):
                    agent_id = agent_for_client(k_agents, c)
                    futs.append(
                        pool.submit(
                            _run_one_client,
                            port=ports[agent_id],
                            ctx=ctx,
                            client_id=c,
                            agent_id=agent_id,
                            devices_per_client=devices_per_client,
                            seed=client_seed(seed, c),
                            duration_sec=duration_sec,
                            heartbeat_interval_sec=heartbeat_interval_sec,
                            arrival_interval_sec=arrival_interval_sec,
                            relinquish_interval_sec=relinquish_interval_sec,
                            late_threshold_ms=late_threshold_ms,
                            heartbeat_schedule_mode=heartbeat_schedule_mode,
                            heartbeat_safety_margin_sec=heartbeat_safety_margin_sec,
                            gauge=gauge,
                            inject_lock=inject_lock if k_agents == 1 else None,
                        )
                    )
                for fut in as_completed(futs):
                    client_results.append(fut.result())
            sustained_wall = time.perf_counter() - t_load
        if sampler.sweeps:
            ram_peak, cpu_peak = sampler.ram_peak_sum, sampler.cpu_peak_sum
        stats_sweeps = sampler.sweeps
        ram_after = ccb._ram_sum_mb(names)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        exit_code = 2
        error = msg[:400]
        failure_class = "infrastructure" if msg.startswith("infra:") else "harness"
    finally:
        t_td = time.perf_counter()
        for name in names:
            docker_rm(name)
        teardown = time.perf_counter() - t_td

    client_results.sort(key=lambda r: r["client_id"])
    all_rows: list[dict[str, Any]] = []
    for cr in client_results:
        if cr.get("error") and not error:
            error = cr["error"]
            exit_code = max(exit_code, 1)
            failure_class = failure_class or "agent_or_client"
        for r in cr["rows"]:
            r["inflight_observed"] = gauge.max_observed
            all_rows.append(r)

    # SAS/application failures are measured outcomes, not infra aborts.
    app_fail = sum(1 for r in all_rows if not r.get("success"))
    if app_fail and exit_code == 0:
        exit_code = 1
        failure_class = "measured_sas_or_transport"

    snap1 = host_load_snapshot()
    temp_end = snap1.get("temperature_tctl_c") or snap1.get("temperature_max_c")

    def _sum_info(key: str) -> int:
        return sum(int(cr.get("info", {}).get(key) or 0) for cr in client_results)

    requests_completed = len(all_rows)
    http_200 = sum(1 for r in all_rows if r.get("http_status") == 200)
    sas_ok = sum(1 for r in all_rows if r.get("success"))
    hb_late = sum(
        int(r.get("late") or 0) for r in all_rows if r.get("operation") == "heartbeat"
    )
    missed = sum(
        int(r.get("missed") or 0) for r in all_rows if r.get("operation") == "heartbeat"
    )
    conn_est = sum(int(cr.get("connections_established") or 0) for cr in client_results)
    conn_reuse = sum(int(cr.get("connection_reuse_events") or 0) for cr in client_results)

    run_row = {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "scenario": scenario,
        "repeat": repeat,
        "pair_id": pair_id,
        "k_agents": k_agents,
        "n_clients": n_clients,
        "devices_per_client": devices_per_client,
        "total_devices": total_devices,
        "seed": seed,
        "create_ready_sec": round(create_ready, 4) if create_ready == create_ready else "",
        "sustained_wall_sec": (
            round(sustained_wall, 4) if sustained_wall == sustained_wall else ""
        ),
        "teardown_sec": round(teardown, 4),
        "max_inflight_configured": n_clients,
        "max_inflight_observed": gauge.max_observed,
        "connections_established_sum": conn_est,
        "connection_reuse_events_sum": conn_reuse,
        "requests_completed": requests_completed,
        "http_200_sum": http_200,
        "sas_ok_sum": sas_ok,
        "heartbeat_late_sum": hb_late,
        "missed_transmit_expire_sum": missed,
        "registered_sum": _sum_info("registered"),
        "granted_sum": _sum_info("granted"),
        "arrivals_sum": _sum_info("arrivals"),
        "relinquished_sum": _sum_info("relinquished"),
        "ram_peak_mb_sum": round(ram_peak, 2) if ram_peak is not None else "",
        "ram_after_load_mb_sum": round(ram_after, 2) if ram_after is not None else "",
        "cpu_pct_peak_sum": round(cpu_peak, 2) if cpu_peak is not None else "",
        "stats_sweeps": stats_sweeps,
        "stats_interval_sec": STATS_INTERVAL_SEC,
        "temp_c_start": round(float(temp_start), 2) if temp_start is not None else "",
        "temp_c_end": round(float(temp_end), 2) if temp_end is not None else "",
        "load1_start": round(float(load1_start), 3) if load1_start is not None else "",
        "exit_code": exit_code,
        "error": error,
        "failure_class": failure_class,
    }

    # --- persist ---
    def _append(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
        write_header = not path.is_file()
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for row in rows:
                w.writerow(row)

    _append(
        out_dir / "colocated_sustained_raw.csv",
        RAW_FIELDS,
        [
            {
                "run_id": run_id,
                "timestamp_utc": utc_now_iso(),
                "scenario": scenario,
                "repeat": repeat,
                "pair_id": pair_id,
                "k_agents": k_agents,
                "n_clients": n_clients,
                "profile_id": PROFILE_ID,
                **r,
            }
            for r in all_rows
        ],
    )
    _append(out_dir / "colocated_sustained_runs.csv", RUN_FIELDS, [run_row])

    client_rows = []
    for cr in client_results:
        rows = cr["rows"]
        lats = [float(r["client_ms"]) for r in rows if r.get("client_ms") not in ("", None)]
        med = round(sorted(lats)[len(lats) // 2], 4) if lats else ""
        p95 = (
            round(sorted(lats)[max(0, int(0.95 * (len(lats) - 1)))], 4) if lats else ""
        )
        info = cr.get("info") or {}
        client_rows.append({
            "run_id": run_id,
            "scenario": scenario,
            "repeat": repeat,
            "pair_id": pair_id,
            "client_id": cr["client_id"],
            "agent_id": cr["agent_id"],
            "devices": cr["devices"],
            "seed": cr["seed"],
            "requests": len(rows),
            "sas_ok": sum(1 for r in rows if r.get("success")),
            "http_200": sum(1 for r in rows if r.get("http_status") == 200),
            "registered": info.get("registered", ""),
            "granted": info.get("granted", ""),
            "arrivals": info.get("arrivals", ""),
            "relinquished": info.get("relinquished", ""),
            "hb_late": sum(
                int(r.get("late") or 0) for r in rows if r.get("operation") == "heartbeat"
            ),
            "missed": sum(
                int(r.get("missed") or 0)
                for r in rows
                if r.get("operation") == "heartbeat"
            ),
            "connections_established": cr["connections_established"],
            "connection_reuse_events": cr["connection_reuse_events"],
            "client_ms_median": med,
            "client_ms_p95": p95,
        })
    _append(out_dir / "colocated_sustained_clients.csv", CLIENT_FIELDS, client_rows)

    summary = ldb._summarize_sustained(
        [{**r, "profile_id": "aggregate"} for r in all_rows], "aggregate"
    )
    _append(
        out_dir / "colocated_sustained_summary.csv",
        SUMMARY_FIELDS,
        [
            {
                "scenario": scenario,
                "repeat": repeat,
                "pair_id": pair_id,
                "k_agents": k_agents,
                "operation": s["operation"],
                "metric": s["metric"],
                "value": s["value"],
            }
            for s in summary
        ],
    )

    write_manifest(
        out_dir / f"manifest_{scenario}_p{pair_id}_r{repeat}.json",
        capture_manifest(
            seed=seed,
            n=total_devices,
            warmup=0,
            script_paths=[LOAD_DIR / "colocated_sustained_bench.py"],
            certs_dir=certs,
            extra={
                "experiment": "colocated_sustained",
                "design": {
                    "scenario": scenario,
                    "k_agents": k_agents,
                    "n_clients": n_clients,
                    "devices_per_client": devices_per_client,
                    "total_devices": total_devices,
                    "client_to_agent": {
                        str(c): agent_for_client(k_agents, c) for c in range(n_clients)
                    },
                    "band_profile": PROFILE_ID,
                    "band_hz": [CBRS_LOW, CBRS_HIGH],
                    "offered_load_constant": True,
                    "deployment_model": "single_host",
                    "not": "multi_host_distributed",
                },
                "parameters": {
                    "duration_sec": duration_sec,
                    "heartbeat_schedule_mode": heartbeat_schedule_mode,
                    "heartbeat_safety_margin_sec": heartbeat_safety_margin_sec,
                    "heartbeat_interval_sec": heartbeat_interval_sec,
                    "arrival_interval_sec": arrival_interval_sec,
                    "relinquish_interval_sec": relinquish_interval_sec,
                    "late_threshold_ms": late_threshold_ms,
                    "client_seeds": [client_seed(seed, c) for c in range(n_clients)],
                },
                "request_concurrency": {
                    "max_inflight_configured": n_clients,
                    "max_inflight_observed": gauge.max_observed,
                    "model": "one sequential client thread each; clients concurrent",
                },
                "connection_policy": "one persistent httpx.Client (mTLS) per client",
                "sqlite": {
                    "k_databases": k_agents,
                    "path_pattern": f"/data/csust_{run_id}_a<i>.db",
                    "isolation": "one file per agent container",
                },
                "memory_sampling": {
                    "method": "docker stats MemUsage (cgroup) sum across agent containers",
                    "interval_sec": STATS_INTERVAL_SEC,
                    "sampled_max_definition": (
                        "max over complete sweeps (enter + periodic + exit); "
                        "NOT absolute peak; NOT process PSS"
                    ),
                    "after_load": "single docker stats sample after load window",
                    "column_ram_peak_mb_sum": "legacy name for sampled maximum",
                },
                "failure_taxonomy": {
                    "infrastructure": "container/ready/docker failures",
                    "harness": "bench exceptions before measured SAS outcomes",
                    "measured_sas_or_transport": "HTTP/SAS failures under load (kept)",
                },
                "client_outcomes": [
                    {
                        "client_id": cr["client_id"],
                        "agent_id": cr["agent_id"],
                        "seed": cr["seed"],
                        **(cr.get("info") or {}),
                        "error": cr.get("error"),
                    }
                    for cr in client_results
                ],
                "run": run_row,
                "image": {"tag": image, "digest": image_digest(image)},
            },
        ),
    )
    return run_row


def run_paired_campaign(
    *,
    pairs: int,
    n_clients: int,
    devices_per_client: int,
    duration_sec: float,
    seed: int,
    image: str,
    certs: Path,
    ctx: Any,
    base_port: int,
    ready_timeout: float,
    heartbeat_interval_sec: float | None,
    arrival_interval_sec: float | None,
    relinquish_interval_sec: float | None,
    late_threshold_ms: float,
    heartbeat_schedule_mode: str,
    heartbeat_safety_margin_sec: float,
    skip_thermal_gate: bool,
    thermal_timeout_sec: float,
    out_dir: Path,
) -> int:
    """Five (or N) paired A/B repeats with counterbalanced order and paired seeds."""
    out_dir.mkdir(parents=True, exist_ok=True)
    schedule = []
    rng = random.Random(seed)
    for pair in range(1, pairs + 1):
        order = ["A", "B"]
        rng.shuffle(order)
        pair_seed = seed + pair * 1000
        schedule.append({"pair_id": pair, "seed": pair_seed, "order": order})
    (out_dir / "campaign_schedule.json").write_text(
        json.dumps(
            {
                "created_at_utc": utc_now_iso(),
                "pairs": pairs,
                "n_clients": n_clients,
                "devices_per_client": devices_per_client,
                "duration_sec": duration_sec,
                "schedule": schedule,
                "note": "Paired seeds; A=K1/B=K=n_clients; client count fixed",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    worst = 0
    for item in schedule:
        pair_id = item["pair_id"]
        pair_seed = item["seed"]
        for scenario in item["order"]:
            k = 1 if scenario == "A" else n_clients
            print(
                f"=== colocated_sustained pair={pair_id} scenario={scenario} K={k} "
                f"clients={n_clients} seed={pair_seed} ===",
                flush=True,
            )
            row = run_scenario(
                scenario=scenario,
                k_agents=k,
                n_clients=n_clients,
                devices_per_client=devices_per_client,
                seed=pair_seed,
                image=image,
                base_port=base_port,
                certs=certs,
                ctx=ctx,
                ready_timeout=ready_timeout,
                duration_sec=duration_sec,
                heartbeat_interval_sec=heartbeat_interval_sec,
                arrival_interval_sec=arrival_interval_sec,
                relinquish_interval_sec=relinquish_interval_sec,
                late_threshold_ms=late_threshold_ms,
                heartbeat_schedule_mode=heartbeat_schedule_mode,
                heartbeat_safety_margin_sec=heartbeat_safety_margin_sec,
                repeat=pair_id,
                pair_id=pair_id,
                skip_thermal_gate=skip_thermal_gate,
                thermal_timeout_sec=thermal_timeout_sec,
                out_dir=out_dir,
            )
            print(
                f"  exit={row['exit_code']} class={row['failure_class']!r} "
                f"inflight={row['max_inflight_observed']}/{row['max_inflight_configured']} "
                f"req={row['requests_completed']} sas_ok={row['sas_ok_sum']} "
                f"missed={row['missed_transmit_expire_sum']} "
                f"wall={row['sustained_wall_sec']}",
                flush=True,
            )
            worst = max(worst, int(row["exit_code"]))
    return worst


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("paired", "single"), default="paired")
    p.add_argument("--scenario", choices=("A", "B"), default=None,
                   help="single mode only")
    p.add_argument("--pairs", type=int, default=5)
    p.add_argument("--n-clients", type=int, default=5)
    p.add_argument("--devices-per-client", type=int, default=300)
    p.add_argument("--sustained-sec", type=float, required=True)
    p.add_argument("--heartbeat-interval-sec", type=float, default=None)
    # Defaults match lifecycle_decomp sustained: continuous Registration+Grant /
    # Relinquish mix during the window (Reviewer 1 continuous workload).
    # Pass 0 or a negative value via explicit override only if disabling; use
    # None only when the CLI flag is omitted historically — now defaulted on.
    p.add_argument(
        "--arrival-interval-sec",
        type=float,
        default=10.0,
        help="Periodic Registration+Grant during sustained window (0 disables)",
    )
    p.add_argument(
        "--relinquish-interval-sec",
        type=float,
        default=30.0,
        help="Periodic Relinquish during sustained window (0 disables)",
    )
    p.add_argument("--late-threshold-ms", type=float, default=500.0)
    p.add_argument(
        "--heartbeat-schedule-mode",
        choices=("respect_transmit_expire", "fixed_rate"),
        default="respect_transmit_expire",
    )
    p.add_argument("--heartbeat-safety-margin-sec", type=float, default=1.0)
    p.add_argument("--image", default="sas-agent:article")
    p.add_argument("--certs-dir", type=Path, default=None)
    p.add_argument("--base-port", type=int, default=19600)
    p.add_argument("--ready-timeout", type=float, default=90.0)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--skip-thermal-gate", action="store_true")
    p.add_argument("--thermal-timeout-sec", type=float, default=600.0)
    args = p.parse_args(argv)

    certs = ldb._resolve_certs(args.certs_dir)
    ctx = build_mtls_context(certs)
    out_dir = args.out_dir.expanduser().resolve()

    common = dict(
        n_clients=args.n_clients,
        devices_per_client=args.devices_per_client,
        duration_sec=args.sustained_sec,
        seed=args.seed,
        image=args.image,
        certs=certs,
        ctx=ctx,
        base_port=args.base_port,
        ready_timeout=args.ready_timeout,
        heartbeat_interval_sec=args.heartbeat_interval_sec,
        arrival_interval_sec=args.arrival_interval_sec,
        relinquish_interval_sec=args.relinquish_interval_sec,
        late_threshold_ms=args.late_threshold_ms,
        heartbeat_schedule_mode=args.heartbeat_schedule_mode,
        heartbeat_safety_margin_sec=args.heartbeat_safety_margin_sec,
        skip_thermal_gate=args.skip_thermal_gate,
        thermal_timeout_sec=args.thermal_timeout_sec,
        out_dir=out_dir,
    )

    if args.mode == "paired":
        return run_paired_campaign(pairs=args.pairs, **common)

    if args.scenario is None:
        raise SystemExit("--scenario required in single mode")
    k = 1 if args.scenario == "A" else args.n_clients
    row = run_scenario(
        scenario=args.scenario,
        k_agents=k,
        repeat=1,
        pair_id=0,
        **common,
    )
    print(
        f"exit={row['exit_code']} inflight={row['max_inflight_observed']} "
        f"req={row['requests_completed']} sas_ok={row['sas_ok_sum']}"
    )
    return int(row["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
