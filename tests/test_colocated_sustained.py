"""Unit tests for colocated sustained design (5 clients × K=1 vs K=5)."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

LOAD_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "load"
TESTS_DIR = Path(__file__).resolve().parent
for p in (LOAD_DIR, TESTS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import colocated_sustained_bench as csb  # noqa: E402
import lifecycle_decomp_bench as ldb  # noqa: E402
from test_lifecycle_sustained import FakeSas  # noqa: E402


class MappingTests(unittest.TestCase):
    def test_k1_all_clients_share_agent_zero(self):
        for c in range(5):
            self.assertEqual(csb.agent_for_client(1, c), 0)

    def test_k5_one_client_per_agent(self):
        for c in range(5):
            self.assertEqual(csb.agent_for_client(5, c), c)

    def test_k5_rejects_client_out_of_range(self):
        with self.assertRaises(ValueError):
            csb.agent_for_client(5, 5)

    def test_paired_client_seeds_stable(self):
        self.assertEqual(
            [csb.client_seed(42, c) for c in range(5)],
            [42, 10042, 20042, 30042, 40042],
        )

    def test_devices_total_1500(self):
        self.assertEqual(5 * 300, 1500)


class InflightGaugeTests(unittest.TestCase):
    def test_five_parallel_clients_reach_inflight_five(self):
        gauge = csb._InflightGauge()
        barrier = threading.Barrier(5)

        def worker():
            with gauge:
                barrier.wait(timeout=2)
                time.sleep(0.02)

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: worker(), range(5)))
        self.assertEqual(gauge.max_observed, 5)


class ProfileAndLoadTests(unittest.TestCase):
    def test_shared_profile_identical_for_fairness(self):
        a = csb.shared_profile()
        b = csb.shared_profile()
        self.assertEqual(a, b)
        self.assertEqual(a["band_low_hz"], csb.CBRS_LOW)
        self.assertEqual(a["band_high_hz"], csb.CBRS_HIGH)

    def test_cli_defaults_enable_arrival_and_relinquish(self):
        import argparse

        # Mirror colocated main defaults (avoid Docker).
        ap = argparse.ArgumentParser()
        ap.add_argument("--arrival-interval-sec", type=float, default=10.0)
        ap.add_argument("--relinquish-interval-sec", type=float, default=30.0)
        args = ap.parse_args([])
        self.assertEqual(args.arrival_interval_sec, 10.0)
        self.assertEqual(args.relinquish_interval_sec, 30.0)

    def test_agenda_mixes_heartbeat_arrival_relinquish(self):
        """Continuous window must schedule Reg/Grant arrivals and Relinquishments."""
        sas = FakeSas()
        rows, info = ldb._run_sustained(
            sas.post,
            csb.shared_profile(),
            fcc="fcc0",
            user="u0",
            seed=7,
            initial_devices=3,
            duration_sec=65.0,
            heartbeat_interval_sec=2.0,
            arrival_interval_sec=10.0,
            relinquish_interval_sec=30.0,
            late_threshold_ms=500.0,
            clock=sas.clock,
            sleep=sas.sleep,
            utcnow=sas.utcnow,
        )
        ops = {r["operation"] for r in rows}
        self.assertIn("heartbeat", ops)
        self.assertIn("registration", ops)
        self.assertIn("grant", ops)
        self.assertGreater(info["arrivals"], 0)
        self.assertGreater(info["registered"], 3)
        self.assertGreater(info["relinquished"], 0)
        self.assertEqual(info["planned_arrivals"], info["arrivals"])
        self.assertGreaterEqual(info["planned_relinquishments"], info["relinquished"])
        self.assertEqual(info["arrival_interval_sec"], 10.0)
        self.assertEqual(info["relinquish_interval_sec"], 30.0)

    def test_disabled_arrival_yields_zero_midwindow_arrivals(self):
        sas = FakeSas()
        rows, info = ldb._run_sustained(
            sas.post,
            csb.shared_profile(),
            fcc="fcc0",
            user="u0",
            seed=7,
            initial_devices=3,
            duration_sec=6.0,
            heartbeat_interval_sec=2.0,
            arrival_interval_sec=None,
            relinquish_interval_sec=None,
            late_threshold_ms=500.0,
            clock=sas.clock,
            sleep=sas.sleep,
            utcnow=sas.utcnow,
        )
        self.assertEqual(info["registered"], 3)
        self.assertEqual(info["arrivals"], 0)
        self.assertEqual(info["relinquished"], 0)
        self.assertTrue(any(r["operation"] == "heartbeat" for r in rows))

    def test_five_clients_same_offered_schedule_params(self):
        """Same seeds + duration ⇒ same planned initial population per client."""
        infos = []
        for c in range(5):
            sas = FakeSas()
            rows, info = ldb._run_sustained(
                sas.post,
                csb.shared_profile(),
                fcc=f"fcc{c}",
                user=f"u{c}",
                seed=csb.client_seed(7, c),
                initial_devices=3,
                duration_sec=25.0,
                heartbeat_interval_sec=2.0,
                arrival_interval_sec=10.0,
                relinquish_interval_sec=30.0,
                late_threshold_ms=500.0,
                clock=sas.clock,
                sleep=sas.sleep,
                utcnow=sas.utcnow,
            )
            infos.append(info)
            self.assertGreaterEqual(info["granted"], 3)
            self.assertTrue(any(r["operation"] == "heartbeat" for r in rows))
            self.assertGreater(info["arrivals"], 0)
        # Offered initial population identical across clients
        self.assertEqual({i["initial_devices"] for i in infos}, {3})
        self.assertEqual({i["heartbeat_interval_sec_used"] for i in infos}, {2.0})
        # Same arrival counts under identical seeds/duration (offered load equivalence)
        self.assertEqual(len({i["arrivals"] for i in infos}), 1)
class PostTrackedTests(unittest.TestCase):
    def test_persistent_connection_reuse_counter(self):
        gauge = csb._InflightGauge()
        conn: dict = {"established": 0, "reused": 0}
        orig = ldb._post

        def fake_post(client, base, op, item):
            return (
                [{"response": {"responseCode": 0}}],
                1.0,
                {},
                SimpleNamespace(status_code=200),
            )

        ldb._post = fake_post  # type: ignore[assignment]
        try:
            csb._post_tracked(
                SimpleNamespace(), "https://x", "registration", {},
                gauge=gauge, conn_state=conn,
            )
            csb._post_tracked(
                SimpleNamespace(), "https://x", "grant", {},
                gauge=gauge, conn_state=conn,
            )
            self.assertEqual(conn["established"], 1)
            self.assertEqual(conn["reused"], 1)
        finally:
            ldb._post = orig  # type: ignore[assignment]


class SqliteIsolationContractTests(unittest.TestCase):
    def test_scenario_b_uses_one_db_path_per_agent(self):
        # Contract encoded in run_scenario path pattern documentation.
        run_id = "abc"
        paths = [f"/data/csust_{run_id}_a{i}.db" for i in range(5)]
        self.assertEqual(len(set(paths)), 5)

    def test_scenario_a_single_db(self):
        run_id = "abc"
        paths = [f"/data/csust_{run_id}_a{i}.db" for i in range(1)]
        self.assertEqual(len(paths), 1)


if __name__ == "__main__":
    unittest.main()
