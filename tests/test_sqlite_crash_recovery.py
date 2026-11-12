"""SQLite recovery after SIGKILL: committed Registration/Grant batches survive,
an in-flight (uncommitted) batch is rolled back, and the file stays consistent.

Each phase runs in a fresh interpreter against the same on-disk database file.

Temporal contract
-----------------
* Cross-process chronology uses wall-clock UTC (``time.time()`` / ISO).
* Intra-process intervals use ``time.perf_counter()`` (monotonic).
* Recovery here is **process restart against the same DB file**, not power-loss
  or media failure, and not Docker HTTP readiness.
* "First valid SAS operation" = Heartbeat batch with ``responseCode == 0`` for
  every surviving Grant (not merely process start or an HTTP 200).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_COMMON = r"""
import json, os, sys, time
from datetime import datetime, timezone
sys.path.insert(0, os.environ["SAS_ROOT"])
import database
from sqlalchemy import text
from domain.band_profile import cbrs_winnforum_profile
from services.profile_manager import set_active_band_profile
from services.registration_service import process_registration
from services.grant_service import process_grant

profile = cbrs_winnforum_profile()
set_active_band_profile(profile)
database.init_db()
FCC, USER = "crashfcc", "crashuser"

def batch(db, prefix, n):
    regs = process_registration(db, [{
        "userId": USER, "fccId": FCC, "cbsdSerialNumber": f"{prefix}_{i:04d}",
        "cbsdCategory": "A", "airInterface": {"radioTechnology": "E_UTRA"},
        "installationParam": {"latitude": 37.0, "longitude": -122.0, "height": 6.0,
                              "heightType": "AGL", "indoorDeployment": True,
                              "antennaGain": 0},
    } for i in range(n)], profile=profile)
    assert all(r["response"]["responseCode"] == 0 for r in regs), regs[:1]
    lows = [3_550_000_000 + 10_000_000 * (i % 15) for i in range(n)]
    grants = process_grant(db, [{
        "cbsdId": r["cbsdId"],
        "operationParam": {"maxEirp": 20.0, "operationFrequencyRange": {
            "lowFrequency": lows[i], "highFrequency": lows[i] + 10_000_000}},
    } for i, r in enumerate(regs)], profile=profile)
    assert all(g["response"]["responseCode"] == 0 for g in grants), grants[:1]
    return regs, grants

def wall_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
"""

_WRITE = _COMMON + r"""
from models.models import FccIdRecord, UserIdRecord
db = database.SessionLocal()
db.add(FccIdRecord(fcc_id=FCC, fcc_max_eirp=47.0))
db.add(UserIdRecord(user_id=USER))
db.commit()
db.close()

db = database.SessionLocal()
regs, grants = batch(db, "ok", int(os.environ["N_COMMITTED"]))
db.close()
size_committed = os.path.getsize(database.DB_PATH)
print(json.dumps({"committed": {g["cbsdId"]: g["grantId"] for g in grants},
                  "size_committed": size_committed,
                  "event": "committed_ok",
                  "wall_utc": wall_iso()}), flush=True)

# Same service code path, but the transaction is never committed. A tiny page
# cache on this connection forces dirty pages into the file before the kill,
# so recovery must roll back a hot journal rather than discard a no-op.
db = database.SessionLocal()
db.execute(text("PRAGMA cache_size=8"))
db.commit = db.flush
batch(db, "pending", int(os.environ["N_PENDING"]))
print(json.dumps({"event": "PENDING", "wall_utc": wall_iso()}), flush=True)
time.sleep(600)
"""

_VERIFY = _COMMON + r"""
from models.models import Cbsd, Grant
from services.heartbeat_service import process_heartbeat

mono0 = time.perf_counter()
wall0 = time.time()
out = {
    "event": "verify",
    "clock_notes": {
        "intra_process_intervals": "perf_counter_ms from verify process start",
        "cross_process_timeline": "wall_utc / wall_unix; not comparable to parent mono",
        "not_measured": ["docker_http_ready", "power_loss", "media_failure"],
    },
    "t_verify_process_start_wall_utc": wall_iso(),
    "t_verify_process_start_wall_unix": wall0,
}
# DB available = integrity_check ok (file openable + consistent after rollback).
with database.engine.connect() as c:
    out["integrity_check"] = c.execute(text("PRAGMA integrity_check")).scalar()
    out["foreign_key_violations"] = len(c.execute(text("PRAGMA foreign_key_check")).fetchall())
    out["journal_mode"] = c.execute(text("PRAGMA journal_mode")).scalar()
    out["synchronous"] = c.execute(text("PRAGMA synchronous")).scalar()
out["t_db_available_perf_ms"] = (time.perf_counter() - mono0) * 1000.0
out["t_db_available_wall_utc"] = wall_iso()
out["t_db_available_wall_unix"] = time.time()

db = database.SessionLocal()
out["grants"] = {g.cbsd_id: g.grant_id for g in db.query(Grant).all()}
out["pending_cbsds"] = db.query(Cbsd).filter(Cbsd.cbsd_serial_number.like("pending_%")).count()
out["orphan_grants"] = sum(
    1 for g in db.query(Grant).all() if db.get(Cbsd, g.cbsd_pk) is None
)
out["live_grants"] = db.query(Grant).filter(Grant.terminated.is_(False)).count()

t_hb = time.perf_counter()
hb = process_heartbeat(db, [
    {"cbsdId": c, "grantId": g, "operationState": "GRANTED"}
    for c, g in out["grants"].items()
], profile=profile)
out["t_first_valid_heartbeat_batch_perf_ms"] = (time.perf_counter() - t_hb) * 1000.0
out["t_first_valid_sas_op_perf_ms"] = (time.perf_counter() - mono0) * 1000.0
out["t_first_valid_sas_op_wall_utc"] = wall_iso()
out["t_first_valid_sas_op_wall_unix"] = time.time()
out["heartbeat_codes"] = sorted({h["response"]["responseCode"] for h in hb})
out["first_valid_sas_op"] = "heartbeat_batch_responseCode_0"
db.close()

db = database.SessionLocal()
_, extra = batch(db, "after", 1)
db.close()
out["write_after_recovery"] = extra[0]["response"]["responseCode"]
out["t_verify_total_perf_ms"] = (time.perf_counter() - mono0) * 1000.0
out["t_verify_done_wall_utc"] = wall_iso()
out["t_verify_done_wall_unix"] = time.time()
# Legacy aliases used by earlier notes / callers
out["t_integrity_ms"] = out["t_db_available_perf_ms"]
out["t_first_valid_heartbeat_batch_ms"] = out["t_first_valid_heartbeat_batch_perf_ms"]
out["t_kill_to_first_heartbeat_ms"] = out["t_first_valid_sas_op_perf_ms"]
out["t_verify_total_ms"] = out["t_verify_total_perf_ms"]
print(json.dumps(out), flush=True)
"""


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class SqliteCrashRecoveryTests(unittest.TestCase):
    N_COMMITTED = 20
    N_PENDING = 300

    def _env(self, db_path: Path) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("DATABASE_URL", None)
        env.update(
            SAS_ROOT=str(ROOT),
            SAS_DATABASE_PATH=str(db_path),
            N_COMMITTED=str(self.N_COMMITTED),
            N_PENDING=str(self.N_PENDING),
        )
        return env

    def test_sigkill_mid_transaction_keeps_committed_and_rolls_back_pending(self):
        with tempfile.TemporaryDirectory(prefix="sas_crash_") as tmp:
            db_path = Path(tmp) / "sas.db"
            journal = Path(f"{db_path}-journal")
            env = self._env(db_path)
            timeline: dict[str, object] = {"phases": []}

            err_path = Path(tmp) / "write.stderr"
            with err_path.open("w") as err:
                proc = subprocess.Popen(
                    [sys.executable, "-c", _WRITE], cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=err, text=True,
                )
            watchdog = threading.Timer(120, proc.kill)
            watchdog.start()
            try:
                first_line = proc.stdout.readline()
                second_line = proc.stdout.readline()
                first = json.loads(first_line)
                second = json.loads(second_line)
                if second.get("event") != "PENDING":
                    self.fail(err_path.read_text()[-2000:])
                size_before_kill = db_path.stat().st_size
                journal_before_kill = journal.is_file() and journal.stat().st_size > 0

                t_kill_mono = time.perf_counter()
                t_kill_wall = time.time()
                timeline["t_sigkill_wall_utc"] = _utc_iso()
                timeline["t_sigkill_wall_unix"] = t_kill_wall
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=30)
                t_exit_wall = time.time()
                timeline["t_writer_process_exit_wall_unix"] = t_exit_wall
                timeline["t_writer_process_exit_wall_utc"] = _utc_iso()
                timeline["t_sigkill_to_process_exit_mono_ms"] = (
                    time.perf_counter() - t_kill_mono
                ) * 1000.0
            finally:
                watchdog.cancel()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=30)
                proc.stdout.close()

            self.assertEqual(proc.returncode, -signal.SIGKILL)
            committed = first["committed"]
            self.assertEqual(len(committed), self.N_COMMITTED)
            self.assertTrue(journal_before_kill, "no rollback journal at kill time")
            self.assertGreater(
                size_before_kill, first["size_committed"],
                "uncommitted pages never reached the file; recovery not exercised",
            )

            timeline["t_verify_spawn_wall_unix"] = time.time()
            timeline["t_verify_spawn_wall_utc"] = _utc_iso()
            done = subprocess.run(
                [sys.executable, "-c", _VERIFY], cwd=str(ROOT), env=env,
                capture_output=True, text=True, timeout=120,
            )
            t_recovered_wall = time.time()
            timeline["t_verify_done_wall_unix"] = t_recovered_wall
            timeline["t_verify_done_wall_utc"] = _utc_iso()
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            out = json.loads(done.stdout.strip().splitlines()[-1])

            self.assertEqual(out["integrity_check"], "ok")
            self.assertEqual(out["foreign_key_violations"], 0)
            self.assertEqual((out["journal_mode"], out["synchronous"]), ("delete", 2))
            self.assertEqual(out["grants"], committed)
            self.assertEqual(out["pending_cbsds"], 0)
            self.assertEqual(out["orphan_grants"], 0)
            self.assertEqual(out["live_grants"], self.N_COMMITTED)
            self.assertEqual(out["heartbeat_codes"], [0])
            self.assertEqual(out["write_after_recovery"], 0)
            self.assertFalse(journal.exists(), "hot journal left behind after recovery")

            # Intra-verify monotonic intervals
            self.assertGreaterEqual(float(out["t_db_available_perf_ms"]), 0.0)
            self.assertGreaterEqual(
                float(out["t_first_valid_sas_op_perf_ms"]),
                float(out["t_db_available_perf_ms"]),
            )
            self.assertEqual(out["first_valid_sas_op"], "heartbeat_batch_responseCode_0")

            # Cross-process wall deltas (seconds) — parent-authored
            timeline["dt_sigkill_to_process_exit_wall_s"] = (
                float(timeline["t_writer_process_exit_wall_unix"])
                - float(timeline["t_sigkill_wall_unix"])
            )
            timeline["dt_sigkill_to_verify_spawn_wall_s"] = (
                float(timeline["t_verify_spawn_wall_unix"])
                - float(timeline["t_sigkill_wall_unix"])
            )
            timeline["dt_sigkill_to_verify_done_wall_s"] = (
                t_recovered_wall - float(timeline["t_sigkill_wall_unix"])
            )
            timeline["dt_sigkill_to_db_available_wall_s"] = (
                float(out["t_db_available_wall_unix"])
                - float(timeline["t_sigkill_wall_unix"])
            )
            timeline["dt_sigkill_to_first_valid_sas_wall_s"] = (
                float(out["t_first_valid_sas_op_wall_unix"])
                - float(timeline["t_sigkill_wall_unix"])
            )
            timeline["child"] = {
                k: out[k]
                for k in (
                    "t_verify_process_start_wall_utc",
                    "t_db_available_wall_utc",
                    "t_first_valid_sas_op_wall_utc",
                    "t_db_available_perf_ms",
                    "t_first_valid_heartbeat_batch_perf_ms",
                    "t_first_valid_sas_op_perf_ms",
                    "t_verify_total_perf_ms",
                    "journal_mode",
                    "synchronous",
                    "clock_notes",
                )
            }
            # Persist timeline next to the temp DB for smoke inspection when requested
            if os.environ.get("SAS_CRASH_TIMELINE_OUT"):
                Path(os.environ["SAS_CRASH_TIMELINE_OUT"]).write_text(
                    json.dumps(timeline, indent=2) + "\n", encoding="utf-8"
                )

            self.assertGreater(timeline["dt_sigkill_to_first_valid_sas_wall_s"], 0.0)
            # Scope reminder encoded in the artifact
            self.assertIn("power_loss", out["clock_notes"]["not_measured"])


class SqlitePragmaContractTests(unittest.TestCase):
    """Production defaults: no explicit PRAGMA → SQLite DELETE + FULL (2)."""

    def test_default_journal_and_synchronous(self):
        with tempfile.TemporaryDirectory(prefix="sas_pragma_") as tmp:
            env = dict(os.environ)
            env.pop("DATABASE_URL", None)
            env["SAS_DATABASE_PATH"] = str(Path(tmp) / "p.db")
            env["SAS_ROOT"] = str(ROOT)
            code = r"""
import os, sys, json
sys.path.insert(0, os.environ["SAS_ROOT"])
import database
from sqlalchemy import text
database.init_db()
with database.engine.connect() as c:
    print(json.dumps({
        "journal_mode": c.execute(text("PRAGMA journal_mode")).scalar(),
        "synchronous": c.execute(text("PRAGMA synchronous")).scalar(),
    }))
"""
            done = subprocess.run(
                [sys.executable, "-c", code], cwd=str(ROOT), env=env,
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(done.returncode, 0, done.stderr[-1000:])
            out = json.loads(done.stdout.strip().splitlines()[-1])
            self.assertEqual(out["journal_mode"], "delete")
            self.assertEqual(out["synchronous"], 2)


if __name__ == "__main__":
    unittest.main()
