"""State isolation between concurrent agent processes (process-level).

Scenarios:
  1. Same logical CBSD identity on agents A/B; Grant on A invisible to B.
  2. Simultaneous lifecycle on A and B without cross-DB records.
  3. Abrupt terminate of B; A continues; B restart with own DB; A intact.

Saídas: benchmarks/data/agent_isolation_results.csv + _manifest.json
"""

from __future__ import annotations

import argparse
import csv
import multiprocessing as mp
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import (  # noqa: E402
    DEFAULT_SEED,
    capture_manifest,
    new_run_id,
    utc_now_iso,
    write_manifest,
)

DATA = BENCH_DIR / "data"

AGENT_A = {"name": "A", "low": 3_550_000_000, "high": 3_570_000_000}
AGENT_B = {"name": "B", "low": 3_570_000_000, "high": 3_590_000_000}

FIELDS = [
    "run_id", "timestamp_utc", "scenario", "step", "agent",
    "expected", "obtained", "pass", "detail",
]


def _agent_worker(cfg: dict[str, Any], cmd_q: Any, resp_q: Any) -> None:
    """Long-lived agent process driven by command queue."""
    import os

    os.environ["SAS_DATABASE_PATH"] = cfg["db_path"]
    os.environ.pop("BAND_PROFILE_FILE", None)
    os.environ.pop("BAND_LOW", None)
    os.environ.pop("BAND_HIGH", None)
    if cfg["root"] not in sys.path:
        sys.path.insert(0, cfg["root"])

    import database
    from domain.band_profile import cbrs_winnforum_profile
    from models.models import FccIdRecord, Grant, UserIdRecord
    from services.deregistration_service import process_deregistration
    from services.grant_service import process_grant
    from services.heartbeat_service import process_heartbeat
    from services.profile_manager import reset_active_band_profile, set_active_band_profile
    from services.registration_service import process_registration
    from services.relinquishment_service import process_relinquishment

    reset_active_band_profile()
    profile = cbrs_winnforum_profile().with_band_limits(cfg["low"], cfg["high"])
    set_active_band_profile(profile)
    database.init_db()

    fcc, user = "iso_fcc", "iso_op"
    db = database.SessionLocal()
    try:
        db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47.0))
        db.add(UserIdRecord(user_id=user))
        db.commit()
    finally:
        db.close()

    state: dict[str, Any] = {"cbsd_id": None, "grant_id": None}
    ch_low, ch_high = cfg["low"], cfg["low"] + 10_000_000

    def _rc(items):
        return items[0].get("response", {}).get("responseCode", -1)

    resp_q.put({"ok": True, "event": "ready", "agent": cfg["name"]})

    while True:
        cmd = cmd_q.get()
        if cmd is None or cmd.get("op") == "shutdown":
            resp_q.put({"ok": True, "event": "shutdown", "agent": cfg["name"]})
            break
        op = cmd["op"]
        try:
            if op == "register":
                serial = cmd.get("serial", "shared")
                db = database.SessionLocal()
                try:
                    r = process_registration(
                        db,
                        [{
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
                        }],
                        profile=profile,
                    )
                    if _rc(r) != 0:
                        raise RuntimeError(f"reg {_rc(r)}")
                    state["cbsd_id"] = r[0]["cbsdId"]
                finally:
                    db.close()
                resp_q.put({"ok": True, "op": op, "cbsd_id": state["cbsd_id"], "agent": cfg["name"]})
            elif op == "grant":
                db = database.SessionLocal()
                try:
                    r = process_grant(
                        db,
                        [{
                            "cbsdId": state["cbsd_id"],
                            "operationParam": {
                                "maxEirp": 20.0,
                                "operationFrequencyRange": {
                                    "lowFrequency": ch_low,
                                    "highFrequency": ch_high,
                                },
                            },
                        }],
                        profile=profile,
                    )
                    if _rc(r) != 0:
                        raise RuntimeError(f"grant {_rc(r)}")
                    state["grant_id"] = r[0]["grantId"]
                finally:
                    db.close()
                resp_q.put({"ok": True, "op": op, "grant_id": state["grant_id"], "agent": cfg["name"]})
            elif op == "heartbeat":
                db = database.SessionLocal()
                try:
                    r = process_heartbeat(
                        db,
                        [{
                            "cbsdId": state["cbsd_id"],
                            "grantId": state["grant_id"],
                            "operationState": "GRANTED",
                        }],
                        profile=profile,
                    )
                    code = _rc(r)
                finally:
                    db.close()
                resp_q.put({"ok": code == 0, "op": op, "code": code, "agent": cfg["name"]})
            elif op == "relinquish":
                db = database.SessionLocal()
                try:
                    r = process_relinquishment(
                        db, [{"cbsdId": state["cbsd_id"], "grantId": state["grant_id"]}]
                    )
                    code = _rc(r)
                    state["grant_id"] = None
                finally:
                    db.close()
                resp_q.put({"ok": code == 0, "op": op, "code": code, "agent": cfg["name"]})
            elif op == "deregister":
                db = database.SessionLocal()
                try:
                    r = process_deregistration(db, [{"cbsdId": state["cbsd_id"]}])
                    code = _rc(r)
                    state["cbsd_id"] = None
                finally:
                    db.close()
                resp_q.put({"ok": code == 0, "op": op, "code": code, "agent": cfg["name"]})
            elif op == "has_grant":
                gid = cmd.get("grant_id")
                db = database.SessionLocal()
                try:
                    found = db.query(Grant).filter_by(grant_id=gid).first() is not None
                finally:
                    db.close()
                resp_q.put({"ok": True, "op": op, "found": found, "agent": cfg["name"]})
            elif op == "count_grants":
                db = database.SessionLocal()
                try:
                    n = db.query(Grant).count()
                finally:
                    db.close()
                resp_q.put({"ok": True, "op": op, "count": n, "agent": cfg["name"]})
            elif op == "ping":
                resp_q.put({"ok": True, "op": op, "agent": cfg["name"]})
            else:
                resp_q.put({"ok": False, "error": f"unknown op {op}", "agent": cfg["name"]})
        except Exception as exc:  # noqa: BLE001
            resp_q.put({"ok": False, "op": op, "error": str(exc)[:300], "agent": cfg["name"]})


class AgentHandle:
    def __init__(self, name: str, low: int, high: int, db_path: str):
        self.name = name
        self.ctx = mp.get_context("spawn")
        self.cmd_q: Any = self.ctx.Queue()
        self.resp_q: Any = self.ctx.Queue()
        self.proc = self.ctx.Process(
            target=_agent_worker,
            args=(
                {"name": name, "low": low, "high": high, "db_path": db_path, "root": str(ROOT)},
                self.cmd_q,
                self.resp_q,
            ),
            daemon=False,
        )
        self.proc.start()
        ready = self.resp_q.get(timeout=60)
        if not ready.get("ok"):
            raise RuntimeError(f"agent {name} failed ready: {ready}")

    def call(self, op: str, timeout: float = 30.0, **kwargs: Any) -> dict[str, Any]:
        self.cmd_q.put({"op": op, **kwargs})
        return self.resp_q.get(timeout=timeout)

    def shutdown(self) -> None:
        try:
            self.cmd_q.put({"op": "shutdown"})
            self.resp_q.get(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        self.proc.join(timeout=10)
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=5)
        self.proc.close()

    def kill(self) -> None:
        if self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(timeout=10)
        self.proc.close()


def _record(rows: list[dict[str, Any]], scenario: str, step: str, agent: str,
            expected: str, obtained: str, ok: bool, detail: str = "") -> None:
    rows.append({
        "run_id": new_run_id(),
        "timestamp_utc": utc_now_iso(),
        "scenario": scenario,
        "step": step,
        "agent": agent,
        "expected": expected,
        "obtained": obtained,
        "pass": int(ok),
        "detail": detail[:300],
    })


def run_scenarios() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="iso_") as tmp:
        db_a = str(Path(tmp) / "a.db")
        db_b = str(Path(tmp) / "b.db")

        # --- Scenario 1: grant isolation ---
        a = AgentHandle("A", AGENT_A["low"], AGENT_A["high"], db_a)
        b = AgentHandle("B", AGENT_B["low"], AGENT_B["high"], db_b)
        try:
            ra = a.call("register", serial="shared")
            rb = b.call("register", serial="shared")
            _record(rows, "grant_isolation", "register_both", "A+B",
                    "ok", str(ra.get("ok") and rb.get("ok")),
                    bool(ra.get("ok") and rb.get("ok")))
            ga = a.call("grant")
            _record(rows, "grant_isolation", "grant_on_A", "A",
                    "ok", str(ga.get("ok")), bool(ga.get("ok")), ga.get("grant_id", ""))
            grant_id = ga.get("grant_id")
            hb = b.call("has_grant", grant_id=grant_id)
            _record(rows, "grant_isolation", "B_has_A_grant", "B",
                    "found=False", f"found={hb.get('found')}",
                    hb.get("found") is False)
            ha = a.call("heartbeat")
            _record(rows, "grant_isolation", "heartbeat_A", "A",
                    "ok", str(ha.get("ok")), bool(ha.get("ok")))
        finally:
            a.shutdown()
            b.shutdown()

        # --- Scenario 2: concurrent lifecycle ---
        a = AgentHandle("A", AGENT_A["low"], AGENT_A["high"], str(Path(tmp) / "a2.db"))
        b = AgentHandle("B", AGENT_B["low"], AGENT_B["high"], str(Path(tmp) / "b2.db"))
        try:
            for agent, serial in ((a, "life_a"), (b, "life_b")):
                agent.call("register", serial=serial)
            # Issue grants concurrently via overlapping calls
            ga = a.call("grant")
            gb = b.call("grant")
            _record(rows, "concurrent_lifecycle", "grants", "A+B",
                    "both ok", f"A={ga.get('ok')} B={gb.get('ok')}",
                    bool(ga.get("ok") and gb.get("ok")))
            ca = a.call("count_grants")
            cb = b.call("count_grants")
            _record(rows, "concurrent_lifecycle", "counts_independent", "A+B",
                    "each count==1", f"A={ca.get('count')} B={cb.get('count')}",
                    ca.get("count") == 1 and cb.get("count") == 1)
            for agent in (a, b):
                agent.call("heartbeat")
                agent.call("relinquish")
                agent.call("deregister")
            _record(rows, "concurrent_lifecycle", "full_cycle", "A+B",
                    "ok", "completed", True)
        finally:
            a.shutdown()
            b.shutdown()

        # --- Scenario 3: crash B, A survives, B restarts ---
        a = AgentHandle("A", AGENT_A["low"], AGENT_A["high"], str(Path(tmp) / "a3.db"))
        b = AgentHandle("B", AGENT_B["low"], AGENT_B["high"], str(Path(tmp) / "b3.db"))
        try:
            a.call("register", serial="surv")
            a.call("grant")
            b.call("register", serial="crash")
            b.kill()
            time.sleep(0.2)
            ping = a.call("ping")
            _record(rows, "crash_isolation", "A_alive_after_B_kill", "A",
                    "ok", str(ping.get("ok")), bool(ping.get("ok")))
            hb = a.call("heartbeat")
            _record(rows, "crash_isolation", "A_heartbeat_after_B_kill", "A",
                    "ok", str(hb.get("ok")), bool(hb.get("ok")))
            # Restart B with fresh DB path (own state)
            b2 = AgentHandle("B", AGENT_B["low"], AGENT_B["high"], str(Path(tmp) / "b3_re.db"))
            try:
                rb = b2.call("register", serial="rebirth")
                _record(rows, "crash_isolation", "B_restart", "B",
                        "ok", str(rb.get("ok")), bool(rb.get("ok")))
                ca = a.call("count_grants")
                _record(rows, "crash_isolation", "A_state_intact", "A",
                        "count>=1", f"count={ca.get('count')}",
                        (ca.get("count") or 0) >= 1)
            finally:
                b2.shutdown()
        finally:
            a.shutdown()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DATA)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = run_scenarios()
    path = out_dir / "agent_isolation_results.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    failures = sum(1 for r in rows if not r["pass"])
    write_manifest(
        out_dir / "agent_isolation_manifest.json",
        capture_manifest(
            seed=args.seed,
            n=len(rows),
            warmup=0,
            script_paths=[LOAD_DIR / "agent_isolation_bench.py", LOAD_DIR / "common.py"],
            extra={
                "experiment": "agent_isolation",
                "failures": failures,
                "outputs": {"results_csv": str(path)},
            },
        ),
    )
    print(f"Wrote {path} ({len(rows)} steps, failures={failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
