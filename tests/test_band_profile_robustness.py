"""BandProfile robustness: invalid configs must fail at load time.

Exports ``benchmarks/data/band_profile_robustness.csv``.
"""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "benchmarks" / "data"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from domain.band_profile import BandProfile, profile_from_mapping  # noqa: E402
from services.profile_manager import load_profile_from_file  # noqa: E402


def _base() -> dict[str, Any]:
    return {
        "profile_id": "robust_test",
        "band_low_hz": 3_550_000_000,
        "band_high_hz": 3_700_000_000,
        "channel_width_hz": 10_000_000,
        "regulatory_rule_id": "ROBUST",
    }


CASES: list[tuple[str, Callable[[], None], type[BaseException]]] = []


def _case(name: str, exc: type[BaseException]):
    def deco(fn: Callable[[], None]):
        CASES.append((name, fn, exc))
        return fn
    return deco


@_case("json_malformed", ValueError)
def _malformed():
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        fh.write("{not-json")
        path = fh.name
    try:
        load_profile_from_file(path)
    finally:
        Path(path).unlink(missing_ok=True)


@_case("file_missing", FileNotFoundError)
def _missing():
    load_profile_from_file("/tmp/does_not_exist_band_profile_xyz.json")


@_case("low_gt_high", ValueError)
def _low_gt_high():
    d = _base()
    d["band_low_hz"] = 3_700_000_000
    d["band_high_hz"] = 3_550_000_000
    profile_from_mapping(d)


@_case("channel_width_zero", ValueError)
def _channel_zero():
    d = _base()
    d["channel_width_hz"] = 0
    profile_from_mapping(d)


@_case("channel_width_negative", ValueError)
def _channel_neg():
    d = _base()
    d["channel_width_hz"] = -10_000_000
    profile_from_mapping(d)


@_case("missing_profile_id", (ValueError, KeyError, TypeError))
def _missing_id():
    d = _base()
    del d["profile_id"]
    profile_from_mapping(d)


@_case("missing_band_low", (ValueError, KeyError, TypeError))
def _missing_low():
    d = _base()
    del d["band_low_hz"]
    profile_from_mapping(d)


@_case("empty_profile_id", ValueError)
def _empty_id():
    d = _base()
    d["profile_id"] = "  "
    profile_from_mapping(d)


@_case("empty_regulatory_rule", ValueError)
def _empty_rule():
    d = _base()
    d["regulatory_rule_id"] = ""
    profile_from_mapping(d)


@_case("eirp_ref_bw_zero", ValueError)
def _eirp_ref_zero():
    d = _base()
    d["eirp_reference_bandwidth_hz"] = 0
    profile_from_mapping(d)


class BandProfileRobustnessTests(unittest.TestCase):
    results: list[dict[str, Any]] = []

    @classmethod
    def tearDownClass(cls):
        DATA.mkdir(parents=True, exist_ok=True)
        path = DATA / "band_profile_robustness.csv"
        fields = ["case", "expected_exception", "obtained_exception", "pass", "detail"]
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            w.writerows(cls.results)
        print(f"Wrote {path} ({len(cls.results)} rows)")

    def test_invalid_profiles_rejected(self):
        for name, fn, expected in CASES:
            with self.subTest(case=name):
                obtained = ""
                ok = False
                detail = ""
                try:
                    fn()
                    detail = "no exception raised"
                except Exception as exc:  # noqa: BLE001
                    obtained = type(exc).__name__
                    if isinstance(expected, tuple):
                        ok = isinstance(exc, expected)
                    else:
                        ok = isinstance(exc, expected)
                    detail = str(exc)[:200]
                    if not ok:
                        detail = f"unexpected {obtained}: {detail}"
                BandProfileRobustnessTests.results.append({
                    "case": name,
                    "expected_exception": (
                        "|".join(e.__name__ for e in expected)
                        if isinstance(expected, tuple)
                        else expected.__name__
                    ),
                    "obtained_exception": obtained,
                    "pass": int(ok),
                    "detail": detail,
                })
                self.assertTrue(ok, f"{name}: {detail}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
