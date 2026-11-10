"""Statistical uncertainty helpers and experiment-unit annotations (P1-11)."""

from __future__ import annotations

import json
import math
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "benchmarks" / "load"
for p in (str(ROOT), str(LOAD)):
    if p not in sys.path:
        sys.path.insert(0, p)

from analysis import statistics as st  # noqa: E402
from analysis import uncertainty_report as ur  # noqa: E402
import container_concurrency_bench as ccb  # noqa: E402
import lifecycle_decomp_bench as ldb  # noqa: E402
import unified_coldstart as ucs  # noqa: E402


class MeanCiTests(unittest.TestCase):
    def test_refuses_n_less_than_2(self):
        self.assertEqual(st.mean_ci95([]), (None, None, None))
        self.assertEqual(st.mean_ci95([1.0]), (None, None, None))

    def test_known_t_critical_and_width(self):
        # n=5 → df=4 → t=2.776; sd of [1,2,3,4,5] = √2.5
        vals = [1.0, 2.0, 3.0, 4.0, 5.0]
        mean, lo, hi = st.mean_ci95(vals)
        self.assertAlmostEqual(mean, 3.0)
        half = 2.776 * math.sqrt(2.5) / math.sqrt(5)
        self.assertAlmostEqual(lo, 3.0 - half, places=3)
        self.assertAlmostEqual(hi, 3.0 + half, places=3)
        self.assertEqual(st.t_critical_95(4), 2.776)
        self.assertEqual(st.t_critical_95(100), 1.96)

    def test_concurrency_ci_matches_analysis_helper(self):
        vals = [10.0, 12.0, 11.0, 13.0, 9.0]
        lo_b, hi_b = ccb._ci95(vals)
        mean, lo, hi = st.mean_ci95(vals)
        self.assertAlmostEqual(lo_b, lo, places=6)
        self.assertAlmostEqual(hi_b, hi, places=6)
        self.assertAlmostEqual(mean, sum(vals) / 5)


class P95ReportTests(unittest.TestCase):
    def test_small_n_is_point_estimate_without_ci(self):
        # n=5 → rank ceil(0.95*5)=5 → sample max; no CI fabricated
        rep = st.p95_report([1, 2, 3, 4, 100])
        self.assertEqual(rep["p95"], 100)
        self.assertEqual(rep["order_rank"], 5)
        self.assertIsNone(rep["ci95_low"])
        self.assertTrue(rep["point_estimate_only"])
        self.assertIn("n<20", rep["note"])

    def test_n30_rank_is_29th_order_statistic(self):
        vals = list(range(1, 31))
        rep = st.p95_report(vals)
        self.assertEqual(rep["order_rank"], 29)
        self.assertEqual(rep["p95"], 29)
        self.assertIsNone(rep["ci95_high"])


class SummaryAnnotationTests(unittest.TestCase):
    def test_lifecycle_summary_marks_within_container_unit(self):
        rows = [
            {"profile_id": "p", "operation": "grant", "warmup": "0", "exit_code": 0,
             "client_ms": 1.0, "application_total_ms": 1.0, "application_non_db_ms": 0.5,
             "database_ms": 0.5, "external_ms": 0.0, "stage_handler_ms": 0.1,
             "stage_database_ms": 0.5, "stage_serialization_ms": 0.1, "stage_other_ms": 0.0,
             "contrib_pct": 10.0}
            for _ in range(5)
        ]
        summary = ldb._summarize(rows)
        client = next(s for s in summary if s["metric"] == "client_ms")
        self.assertEqual(client["experimental_unit"], "http_request_within_container")
        self.assertEqual(client["n_independent_deployments"], 1)
        self.assertEqual(client["p95_is_point_estimate"], 1)
        self.assertEqual(client["n"], 5)
        self.assertLessEqual(set(client), set(ldb.SUMMARY_FIELDS))

    def test_coldstart_summary_has_mean_ci_and_independent_unit(self):
        runs = [
            {"profile_id": "cbrs", "exit_code": 0, "cold_start_total_ms": float(100 + i)}
            for i in range(5)
        ]
        summary = ucs._summarize(runs)
        row = summary[0]
        self.assertEqual(row["experimental_unit"], "independent_docker_container")
        self.assertEqual(row["p95_is_point_estimate"], 1)
        self.assertNotEqual(row["ci95_low"], "")
        self.assertLess(float(row["ci95_low"]), float(row["mean"]))
        self.assertGreater(float(row["ci95_high"]), float(row["mean"]))
        lo, hi = ucs._ci95([float(100 + i) for i in range(5)])
        self.assertAlmostEqual(float(row["ci95_low"]), lo, places=3)

    def test_concurrency_aggregate_documents_ci_of_mean(self):
        rows = [
            {"scenario": "A", "k_containers": 1, "exit_code": 0, "load_wall_sec": 1.0 + i * 0.1,
             "create_ready_sec": 1.0, "scenario_wall_sec": 1.0, "throughput_devices_per_sec": 100,
             "grant_latency_p95_ms": 1.0, "grant_latency_mean_ms": 1.0,
             "grant_batch_latency_median_ms": 1.0, "grant_batch_latency_p95_ms": 2.0,
             "grant_amortized_ms_per_item": 0.1, "ram_peak_mb_sum": 10,
             "ram_after_load_mb_sum": 10, "population_mode": "fixed_total",
             "total_devices": 1500}
            for i in range(5)
        ]
        agg = ccb._aggregate(rows)
        load = next(r for r in agg if r["metric"] == "load_wall_sec")
        self.assertEqual(load["experimental_unit"], "independent_deployment_repeat")
        self.assertEqual(load["ci95_of"], "mean_across_repeats")
        self.assertEqual(load["n_repeats"], 5)
        self.assertNotEqual(load["ci95_low"], "")


class CatalogAndReportTests(unittest.TestCase):
    def test_catalog_flags_lifecycle_pseudoreplication(self):
        life = st.EXPERIMENT_UNITS["lifecycle_decomp"]
        cold = st.EXPERIMENT_UNITS["unified_coldstart"]
        self.assertTrue(life["pseudoreplication_risk"])
        self.assertFalse(life["mean_ci_appropriate_on_summary_n"])
        self.assertFalse(cold["pseudoreplication_risk"])
        self.assertTrue(cold["mean_ci_appropriate_on_summary_n"])

    def test_report_refuses_mean_ci_on_correlated_cycles(self):
        assessed = ur._assess_series([1.0, 2.0, 3.0], independent=False)
        self.assertTrue(assessed["mean_ci_refused"])
        self.assertIsNone(assessed["mean_ci95"])
        self.assertTrue(assessed["p95"]["point_estimate_only"])

    def test_report_cli_writes_json_without_overwrite(self):
        tmp = Path(tempfile.mkdtemp(prefix="uncert_"))
        out = tmp / "uncertainty.json"
        self.assertEqual(ur.main(["--out", str(out)]), 0)
        report = json.loads(out.read_text())
        self.assertIn("lifecycle_decomp", report["catalog"])
        self.assertTrue(report["figure_conventions"]["no_errorbar_ci_figures"])
        self.assertIn("P0-02", report["prior_corrections"])
        self.assertEqual(ur.main(["--out", str(out)]), 2)

    def test_runs_as_script(self):
        out = Path(tempfile.mkdtemp(prefix="uncert_script_")) / "u.json"
        proc = subprocess.run(
            [sys.executable, str(ROOT / "analysis" / "uncertainty_report.py"),
             "--out", str(out)],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("unified_coldstart", json.loads(out.read_text())["catalog"])

    def test_k_sensitivity_absent_without_calibration_data(self):
        report = ur.build_report(None)
        # catalog-only path
        self.assertEqual(report["audits"]["note"], "no campaign CSV loaded; catalog-only report")
        # empty campaign dir still reports the need for P1-09 collection
        tmp = Path(tempfile.mkdtemp(prefix="camp_"))
        (tmp / "dummy").write_text("x", encoding="utf-8")
        # build_report expects artifacts/raw/<id>; call audit directly
        audit = ur.audit_concurrency(tmp)
        self.assertFalse(audit["k_selection_sensitivity"]["available"])
        self.assertIn("P1-09", audit["k_selection_sensitivity"]["minimal_collection"])


if __name__ == "__main__":
    unittest.main()
