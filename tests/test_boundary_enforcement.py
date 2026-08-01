"""Unittest wrapper around boundary_enforcement_bench (deterministic matrix)."""

from __future__ import annotations

import csv
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "benchmarks" / "load" / "boundary_enforcement_bench.py"
CSV = ROOT / "benchmarks" / "data" / "boundary_enforcement.csv"


class BoundaryEnforcementIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        proc = subprocess.run(
            [sys.executable, str(BENCH)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode not in (0, 1):
            raise RuntimeError(proc.stderr[-2000:] or proc.stdout[-2000:])
        cls.stdout = proc.stdout

    def test_csv_written(self):
        self.assertTrue(CSV.is_file(), "boundary_enforcement.csv missing")

    def test_all_cases_pass(self):
        rows = list(csv.DictReader(CSV.open(encoding="utf-8")))
        self.assertGreater(len(rows), 50)
        fails = [r for r in rows if r["pass"] != "1"]
        self.assertEqual(
            fails,
            [],
            f"{len(fails)} boundary failures: "
            + ", ".join(f"{r['profile_id']}/{r['case']} got={r['obtained_code']}" for r in fails[:10]),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
