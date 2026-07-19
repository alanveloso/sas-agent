#!/usr/bin/env python3
"""Subprocess worker that runs the full CBSD-SAS lifecycle, phase by phase.

Lifecycle order (WInnForum): registration -> spectrumInquiry -> grant ->
heartbeat -> relinquishment -> deregistration.

Each invocation runs two lifecycles in the same process/DB:
  1) a warmup lifecycle (discarded) to warm imports/caches/SQLite;
  2) a measured lifecycle, timing each endpoint plus the end-to-end total.

Modes (--mode):
  core        -> calls services.*_service directly (isolated domain core)
  monolithic  -> exercises POST /v1.2/* through fastapi.testclient.TestClient

Prints a single JSON object to stdout with per-phase latencies.

Env:
  SAS_DATABASE_PATH
  BAND_PROFILE_FILE (preferred) or BAND_PROFILE
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

PHASES = [
    "registration",
    "spectrum_inquiry",
    "grant",
    "heartbeat",
    "relinquishment",
    "deregistration",
]


def _rss_mb() -> float:
    import psutil

    return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)


def _apply_profile_env(profile: str, profile_file: str | None) -> None:
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


def _channel(profile, idx: int) -> tuple[int, int]:
    """GAA channel safely inside the active band, first third (avoids edges)."""
    width = int(profile.channel_width_hz)
    n = max(int(profile.channel_count), 1)
    ch = max(0, min(n - 1, idx))
    low = int(profile.band_low_hz + width * ch)
    if low + width > profile.band_high_hz:
        low = int(profile.band_high_hz - width)
    return low, low + width


def _reg_req(fcc_id: str, user_id: str, serial: str) -> dict:
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
        },
    }


def _inject_records(fcc_id: str, user_id: str) -> None:
    import database
    from models.models import FccIdRecord, UserIdRecord

    db = database.SessionLocal()
    try:
        db.add(FccIdRecord(fcc_id=fcc_id, fcc_max_eirp=47.0))
        db.add(UserIdRecord(user_id=user_id))
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Lifecycle runners (core vs monolithic). Each _call returns the inner list of
# responses (without the wrapper), standardizing both paths.
# --------------------------------------------------------------------------- #


class _CoreRunner:
    def __init__(self, profile):
        import database

        self.profile = profile
        self._db_factory = database.SessionLocal

    def _db(self):
        return self._db_factory()

    def registration(self, req):
        from services.registration_service import process_registration

        db = self._db()
        try:
            return process_registration(db, [req], profile=self.profile)
        finally:
            db.close()

    def spectrum_inquiry(self, req):
        from services.spectrum_inquiry_service import process_spectrum_inquiry

        db = self._db()
        try:
            return process_spectrum_inquiry(db, [req], profile=self.profile)
        finally:
            db.close()

    def grant(self, req):
        from services.grant_service import process_grant

        db = self._db()
        try:
            return process_grant(db, [req], profile=self.profile)
        finally:
            db.close()

    def heartbeat(self, req):
        from services.heartbeat_service import process_heartbeat

        db = self._db()
        try:
            return process_heartbeat(db, [req], profile=self.profile)
        finally:
            db.close()

    def relinquishment(self, req):
        from services.relinquishment_service import process_relinquishment

        db = self._db()
        try:
            return process_relinquishment(db, [req])
        finally:
            db.close()

    def deregistration(self, req):
        from services.deregistration_service import process_deregistration

        db = self._db()
        try:
            return process_deregistration(db, [req])
        finally:
            db.close()


class _MonolithicRunner:
    _PATHS = {
        "registration": ("/v1.2/registration", "registrationRequest", "registrationResponse"),
        "spectrum_inquiry": ("/v1.2/spectrumInquiry", "spectrumInquiryRequest", "spectrumInquiryResponse"),
        "grant": ("/v1.2/grant", "grantRequest", "grantResponse"),
        "heartbeat": ("/v1.2/heartbeat", "heartbeatRequest", "heartbeatResponse"),
        "relinquishment": ("/v1.2/relinquishment", "relinquishmentRequest", "relinquishmentResponse"),
        "deregistration": ("/v1.2/deregistration", "deregistrationRequest", "deregistrationResponse"),
    }

    def __init__(self, app):
        from fastapi.testclient import TestClient

        self.client = TestClient(app)

    def _post(self, phase: str, req: dict):
        path, in_key, out_key = self._PATHS[phase]
        resp = self.client.post(path, json={in_key: [req]})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} in {phase}: {resp.text[:400]}")
        return resp.json()[out_key]

    def registration(self, req):
        return self._post("registration", req)

    def spectrum_inquiry(self, req):
        return self._post("spectrum_inquiry", req)

    def grant(self, req):
        return self._post("grant", req)

    def heartbeat(self, req):
        return self._post("heartbeat", req)

    def relinquishment(self, req):
        return self._post("relinquishment", req)

    def deregistration(self, req):
        return self._post("deregistration", req)


def _rc(resp_list) -> int:
    return resp_list[0].get("response", {}).get("responseCode")


def _run_cycle(runner, profile, fcc_id, user_id, serial, low, high, *, measure):
    """Run one full lifecycle. When measure, return (timings, peak_rss).

    Raises RuntimeError naming the phase if any responseCode != 0.
    """
    timings: dict[str, float] = {}
    peak = _rss_mb()
    t_total = time.perf_counter()

    # Registration
    t = time.perf_counter()
    reg = runner.registration(_reg_req(fcc_id, user_id, serial))
    timings["registration"] = (time.perf_counter() - t) * 1000.0
    if _rc(reg) != 0:
        raise RuntimeError(f"registration rc={_rc(reg)} body={reg}")
    cbsd_id = reg[0]["cbsdId"]
    peak = max(peak, _rss_mb())

    # Spectrum Inquiry
    t = time.perf_counter()
    si = runner.spectrum_inquiry(
        {"cbsdId": cbsd_id, "inquiredSpectrum": [{"lowFrequency": low, "highFrequency": high}]}
    )
    timings["spectrum_inquiry"] = (time.perf_counter() - t) * 1000.0
    if _rc(si) != 0:
        raise RuntimeError(f"spectrum_inquiry rc={_rc(si)} body={si}")
    peak = max(peak, _rss_mb())

    # Grant
    t = time.perf_counter()
    gr = runner.grant(
        {
            "cbsdId": cbsd_id,
            "operationParam": {
                "maxEirp": 20.0,
                "operationFrequencyRange": {"lowFrequency": low, "highFrequency": high},
            },
        }
    )
    timings["grant"] = (time.perf_counter() - t) * 1000.0
    if _rc(gr) != 0:
        raise RuntimeError(f"grant rc={_rc(gr)} body={gr}")
    grant_id = gr[0]["grantId"]
    peak = max(peak, _rss_mb())

    # Heartbeat (first is always GRANTED)
    t = time.perf_counter()
    hb = runner.heartbeat({"cbsdId": cbsd_id, "grantId": grant_id, "operationState": "GRANTED"})
    timings["heartbeat"] = (time.perf_counter() - t) * 1000.0
    if _rc(hb) != 0:
        raise RuntimeError(f"heartbeat rc={_rc(hb)} body={hb}")
    peak = max(peak, _rss_mb())

    # Relinquishment
    t = time.perf_counter()
    rel = runner.relinquishment({"cbsdId": cbsd_id, "grantId": grant_id})
    timings["relinquishment"] = (time.perf_counter() - t) * 1000.0
    if _rc(rel) != 0:
        raise RuntimeError(f"relinquishment rc={_rc(rel)} body={rel}")
    peak = max(peak, _rss_mb())

    # Deregistration
    t = time.perf_counter()
    dereg = runner.deregistration({"cbsdId": cbsd_id})
    timings["deregistration"] = (time.perf_counter() - t) * 1000.0
    if _rc(dereg) != 0:
        raise RuntimeError(f"deregistration rc={_rc(dereg)} body={dereg}")
    peak = max(peak, _rss_mb())

    timings["cycle_total"] = (time.perf_counter() - t_total) * 1000.0
    if not measure:
        return None
    return timings, peak


def run(mode: str, profile: str, profile_file: str | None) -> dict:
    mem_initial = _rss_mb()
    fcc_id, user_id = "bench_fcc", "bench_operator"

    import database

    if mode == "core":
        profile_obj = _boot_profile(profile, profile_file)
        database.init_db()
        _inject_records(fcc_id, user_id)
        runner = _CoreRunner(profile_obj)
    elif mode == "monolithic":
        _apply_profile_env(profile, profile_file)
        import main
        from services.profile_manager import get_active_band_profile, reset_active_band_profile

        reset_active_band_profile()
        main.on_startup()
        profile_obj = get_active_band_profile()
        _inject_records(fcc_id, user_id)
        runner = _MonolithicRunner(main.app)
    else:
        raise ValueError(f"invalid mode: {mode}")

    warm_low, warm_high = _channel(profile_obj, max(0, int(profile_obj.channel_count) // 3))
    meas_low, meas_high = _channel(profile_obj, max(0, int(profile_obj.channel_count) // 3) + 1)

    # Warmup lifecycle (discarded).
    _run_cycle(runner, profile_obj, fcc_id, user_id, "warm", warm_low, warm_high, measure=False)

    idle = _rss_mb()
    timings, peak = _run_cycle(
        runner, profile_obj, fcc_id, user_id, "meas", meas_low, meas_high, measure=True
    )

    result = {
        "exit_code": 0,
        "error": "",
        "memory_initial_mb": mem_initial,
        "memory_idle_mb": idle,
        "memory_peak_mb": max(peak, idle),
    }
    for phase, ms in timings.items():
        result[f"lat_{phase}_ms"] = ms
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", required=True, choices=["core", "monolithic"])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-file", default=None)
    args = parser.parse_args()
    try:
        result = run(args.mode, args.profile, args.profile_file)
    except Exception as exc:  # noqa: BLE001
        result = {
            "exit_code": 2,
            "error": f"{exc}\n{traceback.format_exc()}",
            "memory_initial_mb": 0.0,
            "memory_idle_mb": 0.0,
            "memory_peak_mb": 0.0,
        }
    print(json.dumps(result))
    return int(result.get("exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
