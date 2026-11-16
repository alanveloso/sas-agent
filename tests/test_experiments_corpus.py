"""Ensure the published experiments collection is discoverable by analysis tools."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class ExperimentsCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = REPO / "evidence" / "experiments"
        if not cls.root.is_dir():
            raise unittest.SkipTest("evidence/experiments not built")

    def test_manifest_and_checksums_exist(self) -> None:
        self.assertTrue((self.root / "manifest.json").is_file())
        self.assertTrue((self.root / "checksums.sha256").is_file())
        self.assertTrue((self.root / "README.md").is_file())

    def test_required_categories(self) -> None:
        for name in (
            "functional",
            "lifecycle",
            "coldstart",
            "security",
            "persistence",
            "factorial",
            "concurrency",
            "provisioning",
            "sustained",
            "recovery",
            "analysis",
            "meta",
        ):
            self.assertTrue((self.root / name).is_dir(), name)

    def test_colocated_is_mixed_campaign(self) -> None:
        import csv

        runs = self.root / "sustained" / "colocated" / "colocated_sustained_runs.csv"
        self.assertTrue(runs.is_file())
        rows = list(csv.DictReader(runs.open(encoding="utf-8")))
        self.assertEqual(len(rows), 10)
        for r in rows:
            self.assertGreater(int(r["arrivals_sum"]), 0)
            self.assertGreater(int(r["relinquished_sum"]), 0)

    def test_winnforum_65_pass(self) -> None:
        import csv
        from collections import Counter

        p = self.root / "functional" / "winnforum" / "lifecycle_validation.csv"
        rows = list(csv.DictReader(p.open(encoding="utf-8")))
        self.assertEqual(len(rows), 65)
        self.assertEqual(Counter(r["result"] for r in rows)["pass"], 65)

    def test_k3_validation_disjoint_from_calibration_seeds(self) -> None:
        import csv

        calib = list(
            csv.DictReader(
                (
                    self.root
                    / "provisioning"
                    / "calibration"
                    / "container_concurrency_runs.csv"
                ).open(encoding="utf-8")
            )
        )
        val = list(
            csv.DictReader(
                (
                    self.root
                    / "provisioning"
                    / "validation"
                    / "k3_independent"
                    / "container_concurrency_runs.csv"
                ).open(encoding="utf-8")
            )
        )
        c_seeds = {int(r["device_seed"]) for r in calib}
        v_seeds = {int(r["device_seed"]) for r in val}
        self.assertTrue(v_seeds.isdisjoint(c_seeds))
        self.assertEqual(sorted(v_seeds), [95242, 96242, 97242, 98242, 99242])

    def test_stage_and_validate_hooks(self) -> None:
        from analysis.stage_experiments import stage_experiments
        from analysis.load_data import latest_campaign, load_coldstart_summary

        stage_experiments()
        self.assertEqual(latest_campaign(), "experiments")
        cold = load_coldstart_summary("experiments")
        self.assertTrue(cold)


if __name__ == "__main__":
    unittest.main()
