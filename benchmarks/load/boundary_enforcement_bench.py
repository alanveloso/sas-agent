"""Systematic boundary enforcement bench (spawn-isolated per profile).

Writes ``boundary_enforcement.csv`` with expected/observed codes, family, profile.
Matrix: 8 BandProfiles × 18 cases = 144 rows.
"""

from __future__ import annotations

import argparse
import csv
import json
import multiprocessing as mp
import sys
import tempfile
from pathlib import Path
from typing import Any

LOAD_DIR = Path(__file__).resolve().parent
BENCH_DIR = LOAD_DIR.parent
ROOT = BENCH_DIR.parent
PROFILES = BENCH_DIR / "profiles"
DATA = BENCH_DIR / "data"
for p in (str(ROOT), str(LOAD_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from common import capture_manifest, write_manifest  # noqa: E402

SUCCESS = 0
INVALID_PARAM = 103
UNSUPPORTED_SPECTRUM = 300

FIELDS = [
    "profile_id", "family", "case", "low_hz", "high_hz", "max_eirp",
    "expected_code", "obtained_code", "pass", "error",
]


def _cases(p: dict[str, Any]) -> list[dict[str, Any]]:
    from domain.band_profile import profile_from_mapping

    low = int(p["band_low_hz"])
    high = int(p["band_high_hz"])
    width = int(p["channel_width_hz"])
    mid_low = low + width if low + 2 * width <= high else low
    mid_high = mid_low + width
    bp = profile_from_mapping(p)
    allowed_a = bp.max_allowed_eirp_mhz("A", fcc_max_eirp=47.0)
    eirp = min(20.0, allowed_a)
    eps = 0.1
    mis_high = mid_high + width // 2
    mis_ok = mis_high <= high
    return [
        {"case": "freq_below_band", "family": "frequency", "low": low - 2 * width, "high": low - width, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "freq_start_at_fmin", "family": "frequency", "low": low, "high": low + width, "eirp": eirp, "expected": SUCCESS},
        {"case": "freq_inside", "family": "frequency", "low": mid_low, "high": mid_high, "eirp": eirp, "expected": SUCCESS},
        {"case": "freq_end_at_fmax", "family": "frequency", "low": high - width, "high": high, "eirp": eirp, "expected": SUCCESS},
        {"case": "freq_start_at_fmax", "family": "frequency", "low": high, "high": high + width, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "freq_above_band", "family": "frequency", "low": high + width, "high": high + 2 * width, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "freq_partial_below", "family": "frequency", "low": low - width // 2, "high": low + width // 2, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "freq_partial_above", "family": "frequency", "low": high - width // 2, "high": high + width // 2, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "eirp_below_max", "family": "power", "low": mid_low, "high": mid_high, "eirp": allowed_a - eps, "expected": SUCCESS},
        {"case": "eirp_exact_max", "family": "power", "low": mid_low, "high": mid_high, "eirp": allowed_a, "expected": SUCCESS},
        {"case": "eirp_above_max", "family": "power", "low": mid_low, "high": mid_high, "eirp": allowed_a + eps, "expected": INVALID_PARAM},
        {"case": "eirp_negative", "family": "power", "low": mid_low, "high": mid_high, "eirp": -1.0, "expected": SUCCESS},
        {"case": "width_equal_profile", "family": "channel", "low": mid_low, "high": mid_high, "eirp": eirp, "expected": SUCCESS},
        {"case": "width_half", "family": "channel", "low": mid_low, "high": mid_low + max(width // 2, 1), "eirp": eirp, "expected": SUCCESS},
        {"case": "width_double_in_band", "family": "channel", "low": low, "high": low + 2 * width if low + 2 * width <= high else high, "eirp": eirp, "expected": SUCCESS},
        {"case": "width_overflow_band", "family": "channel", "low": low, "high": high + width, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
        {"case": "misaligned_inside", "family": "channel", "low": mid_low + width // 2, "high": mis_high if mis_ok else high, "eirp": eirp, "expected": SUCCESS if mis_ok else UNSUPPORTED_SPECTRUM},
        {"case": "starts_inside_ends_outside", "family": "channel", "low": high - width // 2, "high": high + width // 2, "eirp": eirp, "expected": UNSUPPORTED_SPECTRUM},
    ]


def _profile_worker(profile_map: dict[str, Any], root: str, out_q: Any) -> None:
    import os

    rows: list[dict[str, Any]] = []
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["SAS_DATABASE_PATH"] = str(Path(tmp) / "t.db")
            os.environ.pop("BAND_PROFILE_FILE", None)
            if root not in sys.path:
                sys.path.insert(0, root)

            import database
            from domain.band_profile import profile_from_mapping
            from models.models import FccIdRecord, UserIdRecord
            from services.grant_service import process_grant
            from services.profile_manager import reset_active_band_profile, set_active_band_profile
            from services.registration_service import process_registration

            reset_active_band_profile()
            profile = profile_from_mapping(profile_map)
            set_active_band_profile(profile)
            database.init_db()
            fcc, user = f"bnd_{profile.profile_id[:10]}", "bnd_op"
            db = database.SessionLocal()
            try:
                db.add(FccIdRecord(fcc_id=fcc, fcc_max_eirp=47.0))
                db.add(UserIdRecord(user_id=user))
                db.commit()
            finally:
                db.close()

            for i, case in enumerate(_cases(profile_map)):
                err = ""
                got = -1
                try:
                    db = database.SessionLocal()
                    try:
                        reg = process_registration(
                            db,
                            [{
                                "userId": user,
                                "fccId": fcc,
                                "cbsdSerialNumber": f"s{i:04d}",
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
                        if reg[0]["response"]["responseCode"] != 0:
                            raise RuntimeError(f"reg {reg[0]['response']}")
                        cbsd_id = reg[0]["cbsdId"]
                        gr = process_grant(
                            db,
                            [{
                                "cbsdId": cbsd_id,
                                "operationParam": {
                                    "maxEirp": case["eirp"],
                                    "operationFrequencyRange": {
                                        "lowFrequency": case["low"],
                                        "highFrequency": case["high"],
                                    },
                                },
                            }],
                            profile=profile,
                        )
                        got = gr[0]["response"]["responseCode"]
                    finally:
                        db.close()
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)[:200]
                rows.append({
                    "profile_id": profile_map["profile_id"],
                    "family": case["family"],
                    "case": case["case"],
                    "low_hz": case["low"],
                    "high_hz": case["high"],
                    "max_eirp": case["eirp"],
                    "expected_code": case["expected"],
                    "obtained_code": got,
                    "pass": int(got == case["expected"]),
                    "error": err,
                })
    except Exception as exc:  # noqa: BLE001
        rows.append({
            "profile_id": profile_map.get("profile_id", "?"),
            "family": "setup",
            "case": "profile_boot",
            "low_hz": "",
            "high_hz": "",
            "max_eirp": "",
            "expected_code": SUCCESS,
            "obtained_code": -1,
            "pass": 0,
            "error": str(exc)[:300],
        })
    out_q.put(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DATA)
    parser.add_argument("--profiles-dir", type=Path, default=PROFILES)
    args = parser.parse_args()
    out_dir = args.out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    profiles = [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(args.profiles_dir.glob("*.json"))
    ]
    all_rows: list[dict[str, Any]] = []
    ctx = mp.get_context("spawn")
    for pmap in profiles:
        print(f"=== boundary {pmap['profile_id']} ===", flush=True)
        q: Any = ctx.Queue()
        proc = ctx.Process(target=_profile_worker, args=(pmap, str(ROOT), q))
        proc.start()
        rows = q.get(timeout=180)
        proc.join(timeout=30)
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=5)
        proc.close()
        all_rows.extend(rows)
        fails = sum(1 for r in rows if not r["pass"])
        print(f"  {len(rows)} cases, fails={fails}", flush=True)

    path = out_dir / "boundary_enforcement.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(all_rows)

    failures = sum(1 for r in all_rows if not r["pass"])
    write_manifest(
        out_dir / "boundary_enforcement_manifest.json",
        capture_manifest(
            seed=42,
            n=len(all_rows),
            warmup=0,
            script_paths=[LOAD_DIR / "boundary_enforcement_bench.py"],
            extra={
                "experiment": "boundary_enforcement",
                "profiles": [p["profile_id"] for p in profiles],
                "n_profiles": len(profiles),
                "n_cases_per_profile": 18,
                "failures": failures,
                "outputs": {"csv": str(path)},
            },
        ),
    )
    print(f"Wrote {path} ({len(all_rows)} rows, failures={failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
