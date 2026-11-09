"""SQLite recovery after SIGKILL: committed Registration/Grant batches survive,
an in-flight (uncommitted) batch is rolled back, and the file stays consistent.

Each phase runs in a fresh interpreter against the same on-disk database file.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_COMMON = r"""
import json, os, sys, time
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
                  "size_committed": size_committed}), flush=True)

# Same service code path, but the transaction is never committed. A tiny page
# cache on this connection forces dirty pages into the file before the kill,
# so recovery must roll back a hot journal rather than discard a no-op.
db = database.SessionLocal()
db.execute(text("PRAGMA cache_size=8"))
db.commit = db.flush
batch(db, "pending", int(os.environ["N_PENDING"]))
print("PENDING", flush=True)
time.sleep(600)
"""

_VERIFY = _COMMON + r"""
from models.models import Cbsd, Grant
from services.heartbeat_service import process_heartbeat
out = {}
with database.engine.connect() as c:
    out["integrity_check"] = c.execute(text("PRAGMA integrity_check")).scalar()
    out["foreign_key_violations"] = len(c.execute(text("PRAGMA foreign_key_check")).fetchall())
    out["journal_mode"] = c.execute(text("PRAGMA journal_mode")).scalar()
    out["synchronous"] = c.execute(text("PRAGMA synchronous")).scalar()
db = database.SessionLocal()
out["grants"] = {g.cbsd_id: g.grant_id for g in db.query(Grant).all()}
out["pending_cbsds"] = db.query(Cbsd).filter(Cbsd.cbsd_serial_number.like("pending_%")).count()
out["orphan_grants"] = sum(
    1 for g in db.query(Grant).all() if db.get(Cbsd, g.cbsd_pk) is None
)
out["live_grants"] = db.query(Grant).filter(Grant.terminated.is_(False)).count()
hb = process_heartbeat(db, [
    {"cbsdId": c, "grantId": g, "operationState": "GRANTED"}
    for c, g in out["grants"].items()
], profile=profile)
out["heartbeat_codes"] = sorted({h["response"]["responseCode"] for h in hb})
db.close()
db = database.SessionLocal()
_, extra = batch(db, "after", 1)
db.close()
out["write_after_recovery"] = extra[0]["response"]["responseCode"]
print(json.dumps(out), flush=True)
"""


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
                line = proc.stdout.readline()
                if line.strip() != "PENDING":
                    self.fail(err_path.read_text()[-2000:])
                first = json.loads(first_line)
                size_before_kill = db_path.stat().st_size
                journal_before_kill = journal.is_file() and journal.stat().st_size > 0
                os.kill(proc.pid, signal.SIGKILL)
                proc.wait(timeout=30)
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

            done = subprocess.run(
                [sys.executable, "-c", _VERIFY], cwd=str(ROOT), env=env,
                capture_output=True, text=True, timeout=120,
            )
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


if __name__ == "__main__":
    unittest.main()
