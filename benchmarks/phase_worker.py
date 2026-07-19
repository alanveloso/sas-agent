#!/usr/bin/env python3
"""Subprocess worker for a single benchmark phase.

Invoked by the benchmark runners. Prints a single JSON object to stdout.

Environment:
  SAS_DATABASE_PATH
  BAND_PROFILE (used when --profile-file is absent)
  BAND_PROFILE_FILE (preferred; enables fair multi-profile comparison)

Args:
  --phase {resolve_profile,cold_start_monolithic,cold_start_core,
           grant_monolithic,grant_core,monolithic_with_cpas}
  --profile PROFILE_ID
  --profile-file PATH  (optional JSON BandProfile)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _rss_mb() -> float:
    import psutil

    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def _apply_profile_env(profile: str, profile_file: str | None) -> None:
    """Configure env so initialize_active_profile / on_startup see the same source."""
    if profile_file:
        path = str(Path(profile_file).expanduser().resolve())
        os.environ["BAND_PROFILE_FILE"] = path
        os.environ.pop("BAND_PROFILE", None)
    else:
        os.environ.pop("BAND_PROFILE_FILE", None)
        os.environ["BAND_PROFILE"] = profile
    os.environ.pop("BAND_LOW", None)
    os.environ.pop("BAND_HIGH", None)


def _boot_profile(profile: str, profile_file: str | None):
    from services.profile_manager import initialize_active_profile, reset_active_band_profile

    reset_active_band_profile()
    _apply_profile_env(profile, profile_file)
    return initialize_active_profile(
        None if profile_file else profile,
        force=True,
        profile_file=profile_file,
    )


def _grant_req(cbsd_id: str, low: int, high: int) -> dict:
    return {
        "cbsdId": cbsd_id,
        "operationParam": {
            "maxEirp": 20.0,
            "operationFrequencyRange": {
                "lowFrequency": low,
                "highFrequency": high,
            },
        },
    }


def _channel_pair(profile) -> tuple[int, int, int]:
    """Return (warm_low, meas_low, width) safely inside the active band."""
    width = int(profile.channel_width_hz)
    n = max(int(profile.channel_count), 1)
    if n == 1:
        warm_idx = meas_idx = 0
    elif n == 2:
        warm_idx, meas_idx = 0, 1
    else:
        warm_idx = max(0, min(n - 2, n // 3))
        meas_idx = warm_idx + 1
    warm_low = int(profile.band_low_hz + width * warm_idx)
    meas_low = int(profile.band_low_hz + width * meas_idx)
    if meas_low + width > profile.band_high_hz:
        meas_low = int(profile.band_high_hz - width)
        warm_low = max(int(profile.band_low_hz), meas_low - width)
    return warm_low, meas_low, width


def _seed_fixture(db, profile) -> tuple[dict, dict]:
    """Deterministic CBSD + two channels inside the active band (warmup + measured)."""
    import json as _json

    from models.models import Cbsd, FccIdRecord, UserIdRecord
    from services.authorization_service import ensure_gaa_authorization

    ensure_gaa_authorization(db, profile=profile)
    db.add(FccIdRecord(fcc_id="bench_fcc", fcc_max_eirp=47.0))
    db.add(UserIdRecord(user_id="bench_operator"))
    cbsd_id = "cbsd/bench_fcc/serial1"
    db.add(
        Cbsd(
            cbsd_id=cbsd_id,
            fcc_id="bench_fcc",
            user_id="bench_operator",
            cbsd_serial_number="serial1",
            cbsd_category="A",
            registration_json=_json.dumps(
                {
                    "cbsdCategory": "A",
                    "installationParam": {
                        "latitude": 37.0,
                        "longitude": -122.0,
                        "height": 6.0,
                        "heightType": "AGL",
                        "indoorDeployment": True,
                        "antennaGain": 0,
                    },
                }
            ),
        )
    )
    db.commit()

    warm_low, meas_low, width = _channel_pair(profile)
    return (
        _grant_req(cbsd_id, warm_low, warm_low + width),
        _grant_req(cbsd_id, meas_low, meas_low + width),
    )


def run(phase: str, profile: str, profile_file: str | None) -> dict:
    mem_initial = _rss_mb()
    t0 = time.perf_counter()

    if phase == "resolve_profile":
        from services.profile_manager import reset_active_band_profile, resolve_profile

        reset_active_band_profile()
        _apply_profile_env(profile, profile_file)
        resolve_profile(
            None if profile_file else profile,
            profile_file=profile_file,
        )
        latency_ms = (time.perf_counter() - t0) * 1000.0
        idle = _rss_mb()
        return {
            "latency_ms": latency_ms,
            "memory_initial_mb": mem_initial,
            "memory_idle_mb": idle,
            "memory_peak_mb": idle,
            "exit_code": 0,
            "error": "",
        }

    if phase == "cold_start_core":
        import database

        _boot_profile(profile, profile_file)
        database.init_db()
        session = database.SessionLocal()
        session.close()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        idle = _rss_mb()
        return {
            "latency_ms": latency_ms,
            "memory_initial_mb": mem_initial,
            "memory_idle_mb": idle,
            "memory_peak_mb": idle,
            "exit_code": 0,
            "error": "",
        }

    if phase == "cold_start_monolithic":
        _apply_profile_env(profile, profile_file)
        import main
        from services.profile_manager import reset_active_band_profile

        reset_active_band_profile()
        main.on_startup()
        latency_ms = (time.perf_counter() - t0) * 1000.0
        idle = _rss_mb()
        return {
            "latency_ms": latency_ms,
            "memory_initial_mb": mem_initial,
            "memory_idle_mb": idle,
            "memory_peak_mb": idle,
            "exit_code": 0,
            "error": "",
        }

    if phase == "grant_core":
        import database
        from services.grant_service import process_grant
        from services.profile_manager import get_active_band_profile

        profile_obj = _boot_profile(profile, profile_file)
        database.init_db()
        db = database.SessionLocal()
        try:
            warm_req, req = _seed_fixture(db, profile_obj)
            warm = process_grant(db, [warm_req], profile=profile_obj)
            if warm[0].get("response", {}).get("responseCode") != 0:
                return {
                    "latency_ms": 0.0,
                    "memory_initial_mb": mem_initial,
                    "memory_idle_mb": _rss_mb(),
                    "memory_peak_mb": _rss_mb(),
                    "exit_code": 2,
                    "error": f"warmup grant failed: {warm}",
                }
            idle = _rss_mb()
            t1 = time.perf_counter()
            responses = process_grant(db, [req], profile=get_active_band_profile())
            latency_ms = (time.perf_counter() - t1) * 1000.0
            peak = max(idle, _rss_mb())
            code = responses[0].get("response", {}).get("responseCode")
            if code != 0:
                return {
                    "latency_ms": latency_ms,
                    "memory_initial_mb": mem_initial,
                    "memory_idle_mb": idle,
                    "memory_peak_mb": peak,
                    "exit_code": 2,
                    "error": f"grant responseCode={code} body={responses}",
                }
            return {
                "latency_ms": latency_ms,
                "memory_initial_mb": mem_initial,
                "memory_idle_mb": idle,
                "memory_peak_mb": peak,
                "exit_code": 0,
                "error": "",
            }
        finally:
            db.close()

    if phase == "grant_monolithic":
        _apply_profile_env(profile, profile_file)
        import main
        from fastapi.testclient import TestClient
        from services.profile_manager import get_active_band_profile, reset_active_band_profile
        import database

        reset_active_band_profile()
        main.on_startup()
        profile_obj = get_active_band_profile()
        db = database.SessionLocal()
        try:
            warm_req, req = _seed_fixture(db, profile_obj)
            client = TestClient(main.app)
            warm = client.post("/v1.2/grant", json={"grantRequest": [warm_req]})
            if warm.status_code != 200 or warm.json()["grantResponse"][0]["response"][
                "responseCode"
            ] != 0:
                return {
                    "latency_ms": 0.0,
                    "memory_initial_mb": mem_initial,
                    "memory_idle_mb": _rss_mb(),
                    "memory_peak_mb": _rss_mb(),
                    "exit_code": 2,
                    "error": f"warmup HTTP grant failed: {warm.status_code} {warm.text[:500]}",
                }
            idle = _rss_mb()
            t1 = time.perf_counter()
            resp = client.post("/v1.2/grant", json={"grantRequest": [req]})
            latency_ms = (time.perf_counter() - t1) * 1000.0
            peak = max(idle, _rss_mb())
            if resp.status_code != 200:
                return {
                    "latency_ms": latency_ms,
                    "memory_initial_mb": mem_initial,
                    "memory_idle_mb": idle,
                    "memory_peak_mb": peak,
                    "exit_code": 2,
                    "error": f"HTTP {resp.status_code}: {resp.text[:500]}",
                }
            body = resp.json()
            code = body["grantResponse"][0]["response"]["responseCode"]
            if code != 0:
                return {
                    "latency_ms": latency_ms,
                    "memory_initial_mb": mem_initial,
                    "memory_idle_mb": idle,
                    "memory_peak_mb": peak,
                    "exit_code": 2,
                    "error": f"grant responseCode={code} body={body}",
                }
            return {
                "latency_ms": latency_ms,
                "memory_initial_mb": mem_initial,
                "memory_idle_mb": idle,
                "memory_peak_mb": peak,
                "exit_code": 0,
                "error": "",
            }
        finally:
            db.close()

    if phase == "monolithic_with_cpas":
        _apply_profile_env(profile, profile_file)
        import main
        import database
        from services.cpas_service import trigger_daily_activities
        from services.profile_manager import reset_active_band_profile

        reset_active_band_profile()
        main.on_startup()
        idle = _rss_mb()
        db = database.SessionLocal()
        try:
            t1 = time.perf_counter()
            trigger_daily_activities(db)
            time.sleep(0.25)
            latency_ms = (time.perf_counter() - t1) * 1000.0
            peak = max(idle, _rss_mb())
            return {
                "latency_ms": latency_ms,
                "memory_initial_mb": mem_initial,
                "memory_idle_mb": idle,
                "memory_peak_mb": peak,
                "exit_code": 0,
                "error": "",
            }
        finally:
            db.close()

    raise ValueError(f"Unknown phase: {phase}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-file", default=None)
    args = parser.parse_args()
    try:
        result = run(args.phase, args.profile, args.profile_file)
    except Exception as exc:  # noqa: BLE001
        result = {
            "latency_ms": 0.0,
            "memory_initial_mb": 0.0,
            "memory_idle_mb": 0.0,
            "memory_peak_mb": 0.0,
            "exit_code": 1,
            "error": f"{exc}\n{traceback.format_exc()}",
        }
    print(json.dumps(result))
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
