"""Calibration-driven K selection (analysis/provisioning_selection.py)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from analysis import provisioning_selection as ps  # noqa: E402

LIMITS = {"startup_sec": 10.0, "provisioning_sec": 5.0,
          "grant_batch_p95_ms": 100.0, "memory_mb": 1000.0}
FIELDS = ["scenario", "k_containers", "total_devices", "device_seed", "exit_code",
          "devices_failed", "create_ready_sec", "load_wall_sec",
          "grant_batch_latency_p95_ms", "grant_latency_p95_ms", "ram_peak_mb_sum",
          "ram_after_load_mb_sum", "config_id"]


def row(k, *, load, mem, startup=2.0, p95=50.0, seed=1, exit_code=0, failed=0,
        peak="", cid="", total=1500):
    return {"scenario": "A", "k_containers": k, "total_devices": total, "device_seed": seed,
            "exit_code": exit_code, "devices_failed": failed, "create_ready_sec": startup,
            "load_wall_sec": load, "grant_batch_latency_p95_ms": p95,
            "grant_latency_p95_ms": 1.0, "ram_peak_mb_sum": peak,
            "ram_after_load_mb_sum": mem, "config_id": cid}


def calib():
    # K=1 too slow, K=3 meets everything, K=5 also meets → choose 3
    return [row(1, load=8.0, mem=100, seed=s) for s in (1, 2)] + \
           [row(3, load=4.0, mem=300, seed=s) for s in (1, 2)] + \
           [row(5, load=3.0, mem=500, seed=s) for s in (1, 2)]


class SelectionTests(unittest.TestCase):
    def test_smallest_feasible_k_is_selected(self):
        cands = ps.evaluate(calib(), LIMITS)
        self.assertEqual(ps.select(cands)["k_containers"], 3)
        k1 = next(c for c in cands if c["k_containers"] == 1)
        self.assertFalse(k1["feasible"])
        self.assertEqual(k1["violations"], ["provisioning_sec:8>5"])

    def test_every_repeat_must_pass(self):
        rows = calib() + [row(3, load=6.0, mem=300, seed=3)]   # one slow repeat
        self.assertEqual(ps.select(ps.evaluate(rows, LIMITS))["k_containers"], 5)

    def test_tie_on_k_broken_by_memory_then_id(self):
        rows = [row(2, load=4.0, mem=400, cid="b"), row(2, load=4.0, mem=300, cid="c"),
                row(2, load=4.0, mem=300, cid="a")]
        self.assertEqual(ps.select(ps.evaluate(rows, LIMITS))["config_id"], "a")

    def test_infeasible_when_limits_conflict(self):
        tight = {**LIMITS, "memory_mb": 250.0}   # time needs K>=3, memory allows K<=1
        cands = ps.evaluate(calib(), tight)
        self.assertIsNone(ps.select(cands))
        self.assertTrue(all(c["violations"] for c in cands))

    def test_failed_run_missing_memory_and_non_durable_are_infeasible(self):
        cases = {
            "failed": [row(1, load=1.0, mem=100, failed=3)],
            "exit": [row(1, load=1.0, mem=100, exit_code=2)],
            "missing": [row(1, load=1.0, mem="", peak="")],
            "zero": [row(1, load=1.0, mem=0.0, peak=0.0)],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                self.assertIsNone(ps.select(ps.evaluate(rows, LIMITS)))
        cands = ps.evaluate([row(1, load=1.0, mem=100)], LIMITS, persistence="sqlite_memory")
        self.assertEqual(cands[0]["violations"], ["non_durable_persistence:sqlite_memory"])

    def test_memory_uses_worse_of_after_load_and_peak(self):
        cands = ps.evaluate([row(1, load=1.0, mem=100, peak=1500)], LIMITS)
        self.assertEqual(cands[0]["worst"]["memory_mb"], 1500)
        self.assertFalse(cands[0]["feasible"])


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="prov_sel_"))

    def _csv(self, name, rows, fields=FIELDS):
        path = self.tmp / name
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        return path

    def test_legacy_csv_without_batch_p95_is_rejected(self):
        legacy = [f for f in FIELDS if f != "grant_batch_latency_p95_ms"]
        path = self._csv("legacy.csv", calib(), legacy)
        with self.assertRaisesRegex(ValueError, "legacy grant_latency_p95_ms"):
            ps.load_rows([path])

    def test_mixed_populations_are_rejected(self):
        path = self._csv("mixed.csv", [row(1, load=1, mem=1), row(2, load=1, mem=1, total=3000)])
        with self.assertRaisesRegex(ValueError, "no extrapolation"):
            ps.run([path], [], LIMITS, reference_k=2)

    def test_validation_is_independent_and_reports_selected_and_reference(self):
        cal = self._csv("cal.csv", calib())
        val_rows = [row(k, load=load, mem=100 * k, seed=11) for k, load in
                    ((1, 8.0), (3, 6.0), (5, 3.0))]
        val = self._csv("val.csv", val_rows)
        res = ps.run([cal], [val], LIMITS, reference_k=5)
        self.assertEqual(res["selected"]["k_containers"], 3)
        got = {c["k_containers"]: c["feasible"] for c in res["validation"]}
        self.assertEqual(got, {3: False, 5: True})   # K=1 not reported; K=3 fails validation

        reuse = self._csv("reuse.csv", [row(3, load=4.0, mem=300, seed=1)])
        with self.assertRaisesRegex(ValueError, "device seeds"):
            ps.run([cal], [reuse], LIMITS, reference_k=5)

    def test_cli_writes_result_and_never_overwrites(self):
        cal = self._csv("cal.csv", calib())
        out = self.tmp / "sel.json"
        args = ["--calibration", str(cal), "--out", str(out), "--limit-provisioning-sec", "5",
                "--limit-memory-mb", "250"]
        self.assertEqual(ps.main(args), 0)
        res = json.loads(out.read_text())
        self.assertTrue(res["infeasible"])
        self.assertIsNone(res["selected"])
        self.assertTrue(res["limits_are_illustrative"])
        self.assertEqual(ps.main(args), 2)

    def test_runs_as_script(self):
        cal = self._csv("cal.csv", calib())
        out = self.tmp / "script.json"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "analysis" / "provisioning_selection.py"),
             "--calibration", str(cal), "--out", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(out.read_text())["selected"]["k_containers"], 1)


if __name__ == "__main__":
    unittest.main()
