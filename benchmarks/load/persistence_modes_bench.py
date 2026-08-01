"""Experimento C — isolamento de persistência no provisionamento.

Modos (mesma população, hardware e randomização; granularidade documentada):

  sqlite_commit   — um SessionLocal (≈ commit) por dispositivo (baseline atual)
  sqlite_batch    — um único Session/transação para todos os dispositivos
  sqlite_memory   — SQLite ``:memory:`` com commits por dispositivo
  postgres        — PostgreSQL multiwriter (requer FACTORIAL_POSTGRES_URL)

Decomposição analítica:
  population_wall_ms ≈ tempo de parede do provisionamento
  decision_ms_sum = soma de application_total_ms instrumentado (quase o ciclo;
    NÃO é uma métrica independente somável com database_ms_sum)
  database_ms_sum = parcela SQLAlchemy cursor time

Saídas: persistence_modes_{runs,summary,manifest}.*
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import os
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
DATA = BENCH_DIR / "data"
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (  # noqa: E402
    DEFAULT_SEED,
    capture_manifest,
    new_run_id,
    percentile,
    utc_now_iso,
    wait_thermal_gate,
    write_manifest,
)

CHANNEL_WIDTH_HZ = 10_000_000
POPULATIONS = (133, 1000)
MODES = ("sqlite_commit", "sqlite_batch", "sqlite_memory", "postgres")

RUN_FIELDS = [
    "run_id", "timestamp_utc", "repeat", "order_in_block", "persist_mode",
    "devices", "population_wall_sec", "population_wall_ms",
    "database_ms_sum", "decision_ms_sum",
    "transactions_count", "commits_count", "rollbacks_count",
    "journal_mode", "synchronous_mode", "cache_size",
    "transaction_batch_size", "database_path_type", "database_file_size_bytes",
    "probe_grant_mean_ms", "exit_code", "error",
]
SUMMARY_FIELDS = [
    "persist_mode", "devices", "n_repeats", "metric",
    "median", "iqr", "min", "max", "mean",
]


def _reg(fcc: str, user: str, serial: str) -> dict[str, Any]:
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
        },
    }


def _rc(items: list[dict[str, Any]]) -> int:
    return items[0].get("response", {}).get("responseCode", -1)


def _channels(low: int, high: int) -> list[tuple[int, int]]:
    out = []
    f = low
    while f + CHANNEL_WIDTH_HZ <= high:
        out.append((f, f + CHANNEL_WIDTH_HZ))
        f += CHANNEL_WIDTH_HZ
    return out or [(low, high)]


def _pg_create_db(admin_url: str, name: str) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    eng.dispose()


def _pg_drop_db(admin_url: str, name: str) -> None:
    from sqlalchemy import create_engine, text

    eng = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :n AND pid <> pg_backend_pid()"
            ),
            {"n": name},
        )
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
    eng.dispose()


def _worker(payload: dict[str, Any], out_q: Any) -> None:
    import os
    import sys as _sys

    mode = payload["mode"]
    devices = payload["devices"]
    seed = payload["seed"]
    root = payload["root"]
    run_id = payload["run_id"]
    pg_url = payload.get("postgres_admin_url", "")
    result = {
        "run_id": run_id,
        "timestamp_utc": utc_now_iso(),
        "persist_mode": mode,
        "devices": devices,
        "population_wall_sec": "",
        "population_wall_ms": "",
        "database_ms_sum": "",
        "decision_ms_sum": "",
        "transactions_count": "",
        "commits_count": "",
        "rollbacks_count": 0,
        "journal_mode": "",
        "synchronous_mode": "",
        "cache_size": "",
        "transaction_batch_size": "",
        "database_path_type": "",
        "database_file_size_bytes": "",
        "probe_grant_mean_ms": "",
        "exit_code": 0,
        "error": "",
    }
    db_name = f"pers_{mode}_{run_id}"
    tmp_ctx = None
    try:
        os.environ["SAS_TIMING"] = "1"
        if root not in _sys.path:
            _sys.path.insert(0, root)

        if mode == "postgres":
            _pg_create_db(pg_url, db_name)
            os.environ.pop("SAS_DATABASE_PATH", None)
            os.environ["DATABASE_URL"] = pg_url.rsplit("/", 1)[0] + f"/{db_name}"
        elif mode == "sqlite_memory":
            os.environ.pop("SAS_DATABASE_PATH", None)
            os.environ["DATABASE_URL"] = "sqlite:///:memory:?cache=shared"
        else:
            import tempfile as _tf

            tmp_ctx = _tf.TemporaryDirectory(prefix=f"pers_{mode}_")
            tmp = tmp_ctx.name
            os.environ.pop("DATABASE_URL", None)
            os.environ["SAS_DATABASE_PATH"] = str(Path(tmp) / "sas.db")

        # Fresh process (spawn): import database only after env is set.
        import services.request_timing as rt

        rt.enable_timing(True)
        import database
        from domain.band_profile import cbrs_winnforum_profile
        from models.models import FccIdRecord, UserIdRecord
        from services.grant_service import process_grant
        from services.profile_manager import reset_active_band_profile, set_active_band_profile
        from services.registration_service import process_registration
        from services.relinquishment_service import process_relinquishment
        from services.request_timing import (
            clear_timing,
            install_sqlalchemy_timing,
            start_timing,
        )

        reset_active_band_profile()
        profile = cbrs_winnforum_profile()
        set_active_band_profile(profile)
        database.init_db()
        install_sqlalchemy_timing(database.engine)

        # Record SQLite pragmas / path type
        if mode == "sqlite_memory":
            result["database_path_type"] = "memory"
            result["transaction_batch_size"] = 1
        elif mode == "sqlite_batch":
            result["database_path_type"] = "file"
            result["transaction_batch_size"] = devices
        elif mode == "sqlite_commit":
            result["database_path_type"] = "file"
            result["transaction_batch_size"] = 1
        else:
            result["database_path_type"] = "postgres"
            result["transaction_batch_size"] = 1

        if mode.startswith("sqlite"):
            from sqlalchemy import text

            with database.engine.connect() as conn:
                result["journal_mode"] = str(conn.execute(text("PRAGMA journal_mode")).scalar())
                result["synchronous_mode"] = str(conn.execute(text("PRAGMA synchronous")).scalar())
                result["cache_size"] = str(conn.execute(text("PRAGMA cache_size")).scalar())

        fcc = f"p{abs(hash(mode)) % 1000:03d}{seed % 100000:05d}"[:15]
        user = f"u{seed % 100000}"
        db = database.SessionLocal()
        try:
            db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47.0))
            db.add(UserIdRecord(user_id=user))
            db.commit()
        finally:
            db.close()

        channels = _channels(3_550_000_000, 3_700_000_000)
        db_ms = 0.0
        decision_ms = 0.0
        commits = 0
        transactions = 0
        t_pop = time.perf_counter()

        if mode == "sqlite_batch":
            bag = start_timing("population_batch")
            db = database.SessionLocal()
            transactions = 1
            try:
                for i in range(devices):
                    serial = f"sn_{seed}_{i:05d}"
                    low, high = channels[i % len(channels)]
                    reg = process_registration(db, [_reg(fcc, user, serial)], profile=profile)
                    if _rc(reg) != 0:
                        raise RuntimeError(f"reg rc={_rc(reg)}")
                    gr = process_grant(
                        db,
                        [{
                            "cbsdId": reg[0]["cbsdId"],
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": low,
                                    "highFrequency": high,
                                },
                            },
                        }],
                        profile=profile,
                    )
                    if _rc(gr) != 0:
                        raise RuntimeError(f"grant rc={_rc(gr)}")
                db.commit()
                commits = 1
            finally:
                db.close()
                if bag:
                    d = bag.to_dict()
                    db_ms = float(d["database_ms"])
                    decision_ms = float(d["application_total_ms"])
                clear_timing()
        else:
            for i in range(devices):
                bag = start_timing("population_one")
                serial = f"sn_{seed}_{i:05d}"
                low, high = channels[i % len(channels)]
                db = database.SessionLocal()
                transactions += 1
                try:
                    reg = process_registration(db, [_reg(fcc, user, serial)], profile=profile)
                    if _rc(reg) != 0:
                        raise RuntimeError(f"reg rc={_rc(reg)}")
                    gr = process_grant(
                        db,
                        [{
                            "cbsdId": reg[0]["cbsdId"],
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": low,
                                    "highFrequency": high,
                                },
                            },
                        }],
                        profile=profile,
                    )
                    if _rc(gr) != 0:
                        raise RuntimeError(f"grant rc={_rc(gr)}")
                    # SessionLocal context commits on close for sqlite_commit path
                    # via explicit commit in get_db pattern — force commit here
                    db.commit()
                    commits += 1
                finally:
                    db.close()
                    if bag:
                        d = bag.to_dict()
                        db_ms += float(d["database_ms"])
                        decision_ms += float(d["application_total_ms"])
                    clear_timing()

        wall = time.perf_counter() - t_pop
        result["population_wall_sec"] = round(wall, 4)
        result["population_wall_ms"] = round(wall * 1000.0, 3)
        result["database_ms_sum"] = round(db_ms, 3)
        # decision_ms_sum ≈ instrumented application_total across population
        # (includes DB nested in handlers). Not independently additive with database_ms_sum.
        result["decision_ms_sum"] = round(decision_ms, 3)
        result["transactions_count"] = transactions
        result["commits_count"] = commits

        db_path = os.environ.get("SAS_DATABASE_PATH")
        if db_path and Path(db_path).is_file():
            result["database_file_size_bytes"] = Path(db_path).stat().st_size
        elif mode == "sqlite_memory":
            result["database_file_size_bytes"] = 0

        # Probe grants (10) with per-device sessions.
        probe_low, probe_high = channels[0]
        db = database.SessionLocal()
        try:
            reg = process_registration(db, [_reg(fcc, user, f"probe_{seed}")], profile=profile)
            probe_id = reg[0]["cbsdId"]
        finally:
            db.close()
        lats = []
        for _ in range(10):
            db = database.SessionLocal()
            try:
                t0 = time.perf_counter()
                gr = process_grant(
                    db,
                    [{
                        "cbsdId": probe_id,
                        "operationParam": {
                            "maxEirp": 20.0,
                            "operationFrequencyRange": {
                                "lowFrequency": probe_low,
                                "highFrequency": probe_high,
                            },
                        },
                    }],
                    profile=profile,
                )
                lats.append((time.perf_counter() - t0) * 1000.0)
                gid = gr[0].get("grantId")
                if gid:
                    process_relinquishment(db, [{"cbsdId": probe_id, "grantId": gid}])
            finally:
                db.close()
        result["probe_grant_mean_ms"] = round(statistics.fmean(lats), 3)
    except Exception as exc:  # noqa: BLE001
        result["exit_code"] = 1
        result["error"] = str(exc)[:300]
    finally:
        if mode == "postgres":
            try:
                import database as dbmod

                dbmod.engine.dispose()
            except Exception:  # noqa: BLE001
                pass
            try:
                _pg_drop_db(pg_url, db_name)
            except Exception:  # noqa: BLE001
                pass
        if tmp_ctx is not None:
            tmp_ctx.cleanup()
    out_q.put(result)


def _iqr(vals: list[float]) -> float:
    return (percentile(vals, 75) or 0.0) - (percentile(vals, 25) or 0.0)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--repeats", type=int, default=10)
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--modes", nargs="+", default=["sqlite_commit", "sqlite_batch", "sqlite_memory"])
    p.add_argument("--populations", type=int, nargs="+", default=list(POPULATIONS))
    p.add_argument("--timeout-sec", type=float, default=600.0)
    p.add_argument(
        "--postgres-url",
        default=os.environ.get(
            "FACTORIAL_POSTGRES_URL",
            "postgresql+psycopg://sas:sas@127.0.0.1:55432/postgres",
        ),
    )
    p.add_argument("--out-dir", type=Path, default=DATA)
    p.add_argument("--skip-thermal-gate", action="store_true")
    args = p.parse_args()

    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_thermal_gate:
        wait_thermal_gate(
            out_path=out_dir / "thermal_gate_persistence_modes.json",
            label="persistence_modes",
        )
    runs_path = out_dir / "persistence_modes_runs.csv"
    summary_path = out_dir / "persistence_modes_summary.csv"

    cells = [
        (mode, n)
        for mode in args.modes
        for n in args.populations
    ]
    all_runs: list[dict[str, Any]] = []
    failures = 0

    with runs_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=RUN_FIELDS)
        writer.writeheader()
        for repeat in range(1, args.repeats + 1):
            order = list(cells)
            random.Random(args.seed + repeat).shuffle(order)
            print(f"=== persist repeat={repeat} cells={len(order)} ===", flush=True)
            for idx, (mode, devices) in enumerate(order, start=1):
                ctx = mp.get_context("spawn")
                q: Any = ctx.Queue()
                payload = {
                    "mode": mode,
                    "devices": devices,
                    "seed": args.seed + 1000 * repeat + devices + hash(mode) % 997,
                    "root": str(ROOT),
                    "run_id": new_run_id(),
                    "postgres_admin_url": args.postgres_url if mode == "postgres" else "",
                }
                proc = ctx.Process(target=_worker, args=(payload, q), daemon=False)
                proc.start()
                try:
                    row = q.get(timeout=args.timeout_sec)
                except Exception as exc:  # noqa: BLE001
                    row = {
                        "run_id": payload["run_id"],
                        "timestamp_utc": utc_now_iso(),
                        "persist_mode": mode,
                        "devices": devices,
                        "population_wall_sec": "",
                        "database_ms_sum": "",
                        "decision_ms_sum": "",
                        "probe_grant_mean_ms": "",
                        "exit_code": 2,
                        "error": str(exc)[:300],
                    }
                proc.join(timeout=30)
                if proc.is_alive():
                    proc.terminate()
                row["repeat"] = repeat
                row["order_in_block"] = idx
                writer.writerow(row)
                fh.flush()
                all_runs.append(row)
                status = "ok" if row["exit_code"] == 0 else f"FAIL:{row['error']}"
                print(
                    f"  {mode} N={devices}: wall={row['population_wall_sec']}s "
                    f"db_ms={row['database_ms_sum']} {status}",
                    flush=True,
                )
                if row["exit_code"] != 0:
                    failures += 1

    # summary
    summary: list[dict[str, Any]] = []
    metrics = ("population_wall_sec", "database_ms_sum", "decision_ms_sum", "probe_grant_mean_ms")
    groups: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for r in all_runs:
        if r["exit_code"] != 0:
            continue
        groups.setdefault((r["persist_mode"], int(r["devices"])), []).append(r)
    for (mode, n), rows in sorted(groups.items()):
        for metric in metrics:
            vals = [float(r[metric]) for r in rows if r.get(metric) not in ("", None)]
            if not vals:
                continue
            summary.append({
                "persist_mode": mode,
                "devices": n,
                "n_repeats": len(vals),
                "metric": metric,
                "median": round(float(statistics.median(vals)), 4),
                "iqr": round(_iqr(vals), 4),
                "min": round(min(vals), 4),
                "max": round(max(vals), 4),
                "mean": round(statistics.fmean(vals), 4),
            })
    with summary_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        w.writerows(summary)

    write_manifest(
        out_dir / "persistence_modes_manifest.json",
        capture_manifest(
            seed=args.seed,
            n=args.repeats,
            warmup=0,
            script_paths=[LOAD_DIR / "persistence_modes_bench.py"],
            extra={
                "experiment": "persistence_modes",
                "modes": list(args.modes),
                "populations": list(args.populations),
                "decision_ms_sum_semantics": (
                    "Sum of TimingBag.application_total_ms over population "
                    "(includes nested DB). Not independently additive with "
                    "database_ms_sum. Prefer population_wall_ms as primary DV."
                ),
                "failures": failures,
            },
        ),
    )
    print(f"Wrote {runs_path}\nWrote {summary_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
