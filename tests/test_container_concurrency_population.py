"""Population modes in container_concurrency_bench (fixed total vs fixed per agent)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

LOAD_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "load"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

import container_concurrency_bench as ccb  # noqa: E402


class AgentPopulationTests(unittest.TestCase):
    def test_historical_split_is_unchanged(self):
        for k in ccb.DEFAULT_K:
            counts = ccb._agent_populations(k, ccb.DEFAULT_DEVICES, None)
            self.assertEqual(counts, ccb._partition_devices(ccb.DEFAULT_DEVICES, k))
            self.assertEqual(sum(counts), 1500)
            self.assertEqual(set(counts), {1500 // k})

    def test_uneven_split_differs_by_at_most_one(self):
        counts = ccb._agent_populations(7, 1500, None)
        self.assertEqual(sum(counts), 1500)
        self.assertLessEqual(max(counts) - min(counts), 1)

    def test_fixed_per_agent_scales_total(self):
        for k in (1, 5, 15):
            counts = ccb._agent_populations(k, ccb.DEFAULT_DEVICES, 100)
            self.assertEqual(counts, [100] * k)
            self.assertEqual(sum(counts), 100 * k)


class CliTests(unittest.TestCase):
    def test_default_is_historical(self):
        args = ccb._parse_args([])
        self.assertEqual(args.devices, 1500)
        self.assertIsNone(args.devices_per_agent)
        self.assertEqual(args.population_mode, ccb.FIXED_TOTAL)
        self.assertEqual(args.output_stem, "container_concurrency")

    def test_per_agent_uses_separate_outputs(self):
        args = ccb._parse_args(["--devices-per-agent", "100"])
        self.assertEqual(args.population_mode, ccb.FIXED_PER_AGENT)
        self.assertEqual(args.output_stem, "container_concurrency_per_agent")

    def test_modes_are_mutually_exclusive_and_validated(self):
        for argv in (["--devices", "1500", "--devices-per-agent", "100"],
                     ["--devices-per-agent", "0"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr"):
                with self.assertRaises(SystemExit):
                    ccb._parse_args(argv)


class StatsSamplerTests(unittest.TestCase):
    def test_partial_sweep_is_not_counted(self):
        sampler = ccb._DockerStatsSampler(["a", "b"], interval=0.0)

        def many(names):
            sampler._stop.set()  # load ends during batched stats RPC
            return [(1.0, 100.0), (1.0, 100.0)]

        with mock.patch.object(ccb, "docker_stats_sample_many", side_effect=many):
            sampler._loop()
        self.assertEqual((sampler.sweeps, sampler.ram_peak_sum), (0, 0.0))

    def test_complete_sweep_sums_containers(self):
        sampler = ccb._DockerStatsSampler(["a", "b"], interval=0.0)
        calls = iter([[(1.0, 100.0), (2.0, 50.0)]])

        def many(_names):
            try:
                return next(calls)
            except StopIteration:
                sampler._stop.set()
                return [(None, None), (None, None)]

        with mock.patch.object(ccb, "docker_stats_sample_many", side_effect=many):
            sampler._loop()
        self.assertEqual((sampler.sweeps, sampler.ram_peak_sum, sampler.cpu_peak_sum),
                         (1, 150.0, 3.0))

    def test_enter_exit_sample_covers_short_loads(self):
        """Short loads must not leave sweeps=0 when docker stats succeed."""
        sampler = ccb._DockerStatsSampler(["a", "b"], interval=10.0)
        with mock.patch.object(
            ccb, "docker_stats_sample_many", return_value=[(1.0, 40.0), (1.0, 40.0)]
        ):
            with sampler:
                pass  # load shorter than interval
        self.assertGreaterEqual(sampler.sweeps, 2)  # enter + exit
        self.assertEqual(sampler.ram_peak_sum, 80.0)
        self.assertTrue(sampler.not_pss)

    def test_failed_read_does_not_count_incomplete_enter_sample(self):
        sampler = ccb._DockerStatsSampler(["a", "b"], interval=10.0)
        with mock.patch.object(
            ccb, "docker_stats_sample_many", return_value=[(1.0, 10.0), (None, None)]
        ):
            self.assertFalse(sampler._sample_complete())
        self.assertEqual((sampler.sweeps, sampler.ram_peak_sum), (0, 0.0))

class RunScenarioTests(unittest.TestCase):
    def _run(self, k, devices_per_agent):
        loads = []

        def fake_load(**kw):
            loads.append(kw["device_count"])
            n = kw["device_count"]
            return {"devices_ok": n, "devices_failed": 0, "grant_latencies_ms": [1.0] * n,
                    "grant_batch_ms": [100.0], "grant_batch_items": [n], "error": ""}

        sampler = mock.MagicMock()
        sampler.return_value.__enter__.return_value = mock.Mock(
            ram_peak_sum=0.0, cpu_peak_sum=0.0, sweeps=0
        )
        with mock.patch.multiple(
            ccb,
            docker_run=mock.Mock(), docker_rm=mock.Mock(),
            docker_stats_sample=mock.Mock(return_value=(1.0, 100.0)),
            docker_stats_sample_many=mock.Mock(
                side_effect=lambda names: [(1.0, 100.0)] * len(names)
            ),
            wait_http_200=mock.Mock(return_value=(True, 200, "")),
            host_load_snapshot=mock.Mock(return_value={}),
            _DockerStatsSampler=sampler, _load_one_container=fake_load,
        ):
            row = ccb._run_scenario(
                scenario="A", k=k, total_devices=ccb.DEFAULT_DEVICES, seed=1, image="img",
                base_port=1, certs=Path("."), ctx=None, ready_timeout=1.0, repeat=1,
                order_idx=1, devices_per_agent=devices_per_agent,
            )
        return row, sorted(loads)

    def test_historical_row(self):
        row, loads = self._run(5, None)
        self.assertEqual(loads, [300] * 5)
        self.assertEqual(set(row), set(ccb.RUN_FIELDS))
        self.assertEqual(row["population_mode"], ccb.FIXED_TOTAL)
        self.assertEqual(row["total_devices"], 1500)
        self.assertEqual((row["devices_per_agent_min"], row["devices_per_agent_max"]), (300, 300))
        self.assertEqual(row["max_inflight_requests_total"], 5)
        self.assertEqual(row["max_inflight_requests_per_agent"], 1)
        self.assertEqual(row["grant_amortized_ms_per_item"], round(500.0 / 1500, 4))
        # no complete docker-stats sweep → blank peak, not 0.0
        self.assertEqual((row["stats_sweeps"], row["ram_peak_mb_sum"]), (0, ""))
        self.assertEqual(row["ram_after_load_mb_sum"], 500.0)

    def test_fixed_per_agent_row(self):
        row, loads = self._run(3, 40)
        self.assertEqual(loads, [40] * 3)
        self.assertEqual(row["population_mode"], ccb.FIXED_PER_AGENT)
        self.assertEqual(row["total_devices"], 120)
        self.assertEqual(row["devices_ok"], 120)
        self.assertEqual((row["devices_per_agent_min"], row["devices_per_agent_max"]), (40, 40))
        self.assertEqual(row["max_inflight_requests_total"], 3)

        summary = ccb._aggregate([row])
        self.assertTrue(summary)
        for s in summary:
            self.assertLessEqual(set(s), set(ccb.SUMMARY_FIELDS))
            self.assertEqual((s["population_mode"], s["total_devices"]), (ccb.FIXED_PER_AGENT, 120))
        self.assertIn("grant_batch_latency_p95_ms", {s["metric"] for s in summary})

    def test_aggregate_accepts_rows_without_new_columns(self):
        row, _ = self._run(1, None)
        legacy = {k: v for k, v in row.items() if k not in ("population_mode",)}
        for s in ccb._aggregate([legacy]):
            self.assertEqual(s["population_mode"], ccb.FIXED_TOTAL)


if __name__ == "__main__":
    unittest.main()
