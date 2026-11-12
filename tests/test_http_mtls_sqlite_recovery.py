"""End-to-end SQLite recovery over real HTTPS/mTLS (Docker), not in-process process_*.

Complements ``test_sqlite_crash_recovery.py`` (which calls service functions directly).

Smoke only: one kill/restart cycle. Definitive repeats belong to the final campaign.

Clocks
------
* ``CLOCK_MONOTONIC`` (Linux boot-relative) for cross-process intervals on one host.
* UTC wall timestamps for human-readable provenance.
* Does **not** claim power-loss durability.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "benchmarks" / "load"
import sys

if str(LOAD) not in sys.path:
    sys.path.insert(0, str(LOAD))

from common import (  # noqa: E402
    build_mtls_context,
    cert_sha256_fingerprint,
    wait_http_200,
)

IMAGE = os.environ.get("SAS_RECOVERY_IMAGE", "sas-agent:article")
CERTS_CANDIDATES = [
    Path(os.environ["HARNESS_CERTS_DIR"]) if os.environ.get("HARNESS_CERTS_DIR") else None,
    ROOT / "evidence/tnsm-2026-12135-controlled-20261111T205011Z/meta/harness_certs",
    ROOT.parent / "src" / "harness" / "certs",
]


def _certs_dir() -> Path | None:
    for p in CERTS_CANDIDATES:
        if p and (p / "ca.cert").is_file() and (p / "domain_proxy.cert").is_file():
            return p.resolve()
    return None


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(
        ["docker", "image", "inspect", IMAGE],
        capture_output=True, text=True, timeout=30,
    )
    return r.returncode == 0


def _mono() -> float:
    return time.clock_gettime(time.CLOCK_MONOTONIC)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _run(cmd: list[str], timeout: float = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@unittest.skipUnless(_docker_ok() and _certs_dir() is not None, "docker image/certs unavailable")
class HttpMtlsSqliteRecoverySmoke(unittest.TestCase):
    """SIGKILL a live SAS container; restart with the same bind-mounted DB; HB over mTLS."""

    HOST_PORT = 19991
    NAME = "sas-http-sqlite-recovery-smoke"

    def setUp(self) -> None:
        self.certs = _certs_dir()
        assert self.certs is not None
        self.ctx = build_mtls_context(self.certs)
        self.tmp = tempfile.mkdtemp(prefix="sas_http_rec_")
        self.data_dir = Path(self.tmp) / "data"
        self.data_dir.mkdir()
        self.db_host = self.data_dir / "recovery.db"
        self.timeline: dict[str, object] = {"clock": "CLOCK_MONOTONIC+UTC", "events": []}
        _run(["docker", "rm", "-f", self.NAME])

    def tearDown(self) -> None:
        _run(["docker", "rm", "-f", self.NAME])
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mark(self, name: str, **extra: object) -> None:
        self.timeline["events"].append({
            "name": name,
            "mono_s": _mono(),
            "wall_utc": _utc(),
            **extra,
        })

    def _start(self) -> None:
        admin_fp = cert_sha256_fingerprint(self.certs / "domain_proxy.cert")
        cmd = [
            "docker", "run", "-d", "--name", self.NAME,
            "-p", f"{self.HOST_PORT}:9000",
            "-v", f"{self.certs}:/app/src/harness/certs:ro",
            "-v", f"{self.data_dir.resolve()}:/data",
            "-e", "SAS_DATABASE_PATH=/data/recovery.db",
            "-e", "BAND_PROFILE=cbrs_winnforum",
            "-e", f"SAS_ADMIN_CERT_SHA256={admin_fp}",
            IMAGE,
        ]
        proc = _run(cmd)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def _base(self) -> str:
        return f"https://127.0.0.1:{self.HOST_PORT}"

    def _wait_ready(self) -> None:
        ok, polls, err = wait_http_200(f"{self._base()}/ready", self.ctx, 90.0)
        self.assertTrue(ok, err)
        self._mark("http_ready", polls=polls)

    def test_sigkill_restart_same_db_then_https_heartbeat(self):
        # --- phase 1: populate ---
        self._mark("start_container_1")
        self._start()
        self._wait_ready()
        base = self._base()
        with httpx.Client(verify=self.ctx, timeout=30.0) as client:
            client.post(f"{base}/admin/injectdata/user_id", json={"userId": "rec_user"}).raise_for_status()
            client.post(
                f"{base}/admin/injectdata/fcc_id",
                json={"fccId": "rec_fcc", "fccMaxEirp": 47},
            ).raise_for_status()
            reg = client.post(
                f"{base}/v1.2/registration",
                json={"registrationRequest": [{
                    "userId": "rec_user", "fccId": "rec_fcc",
                    "cbsdSerialNumber": "rec_sn_0001", "cbsdCategory": "A",
                    "airInterface": {"radioTechnology": "E_UTRA"},
                    "installationParam": {
                        "latitude": 37.0, "longitude": -122.0, "height": 6.0,
                        "heightType": "AGL", "indoorDeployment": True, "antennaGain": 0,
                    },
                }]},
            )
            reg.raise_for_status()
            cbsd_id = reg.json()["registrationResponse"][0]["cbsdId"]
            self.assertEqual(reg.json()["registrationResponse"][0]["response"]["responseCode"], 0)
            gra = client.post(
                f"{base}/v1.2/grant",
                json={"grantRequest": [{
                    "cbsdId": cbsd_id,
                    "operationParam": {
                        "maxEirp": 20.0,
                        "operationFrequencyRange": {
                            "lowFrequency": 3_550_000_000,
                            "highFrequency": 3_560_000_000,
                        },
                    },
                }]},
            )
            gra.raise_for_status()
            g0 = gra.json()["grantResponse"][0]
            self.assertEqual(g0["response"]["responseCode"], 0)
            grant_id = g0["grantId"]
            hb1 = client.post(
                f"{base}/v1.2/heartbeat",
                json={"heartbeatRequest": [{
                    "cbsdId": cbsd_id, "grantId": grant_id, "operationState": "GRANTED",
                }]},
            )
            hb1.raise_for_status()
            self.assertEqual(hb1.json()["heartbeatResponse"][0]["response"]["responseCode"], 0)
            self._mark("pre_kill_heartbeat_ok", cbsd_id=cbsd_id, grant_id=grant_id)

        self.assertTrue(self.db_host.is_file(), "SQLite file missing on host bind mount")
        size_before = self.db_host.stat().st_size
        self.assertGreater(size_before, 0)

        # --- kill ---
        self._mark("sigkill")
        kill = _run(["docker", "kill", "--signal", "SIGKILL", self.NAME])
        self.assertEqual(kill.returncode, 0, kill.stderr)
        _run(["docker", "rm", "-f", self.NAME])
        self._mark("container_removed")

        # --- restart same DB ---
        self._mark("start_container_2")
        self._start()
        self._wait_ready()
        with httpx.Client(verify=self.ctx, timeout=30.0) as client:
            hb2 = client.post(
                f"{self._base()}/v1.2/heartbeat",
                json={"heartbeatRequest": [{
                    "cbsdId": cbsd_id, "grantId": grant_id, "operationState": "AUTHORIZED",
                }]},
            )
            self._mark(
                "first_https_heartbeat_after_restart",
                http_status=hb2.status_code,
            )
            self.assertEqual(hb2.status_code, 200, hb2.text)
            body = hb2.json()["heartbeatResponse"][0]
            self.assertEqual(body["response"]["responseCode"], 0, body)

        # --- timeline deltas (monotonic, same host) ---
        by_name = {e["name"]: e for e in self.timeline["events"]}
        t_kill = float(by_name["sigkill"]["mono_s"])
        ready_events = [e for e in self.timeline["events"] if e["name"] == "http_ready"]
        self.assertGreaterEqual(len(ready_events), 2)
        t_ready2 = float(ready_events[-1]["mono_s"])
        t_hb2 = float(by_name["first_https_heartbeat_after_restart"]["mono_s"])
        self.timeline["dt_sigkill_to_ready2_mono_s"] = t_ready2 - t_kill
        self.timeline["dt_sigkill_to_first_https_sas_mono_s"] = t_hb2 - t_kill
        self.timeline["dt_ready2_to_first_https_sas_mono_s"] = t_hb2 - t_ready2
        self.assertGreater(self.timeline["dt_sigkill_to_first_https_sas_mono_s"], 0.0)
        self.assertGreaterEqual(
            float(self.timeline["dt_sigkill_to_first_https_sas_mono_s"]),
            float(self.timeline["dt_sigkill_to_ready2_mono_s"]),
        )
        # Ready alone is not enough: we required responseCode 0 on HTTPS Heartbeat
        self.timeline["first_valid_sas_op"] = "https_heartbeat_responseCode_0"
        self.timeline["not_claimed"] = ["power_loss", "media_failure", "statistical_sla"]

        out = os.environ.get("SAS_HTTP_RECOVERY_TIMELINE_OUT")
        if out:
            Path(out).write_text(json.dumps(self.timeline, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
