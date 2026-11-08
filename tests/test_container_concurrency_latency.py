"""Grant latency semantics in container_concurrency_bench (batch vs amortized)."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

LOAD_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "load"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

import container_concurrency_bench as ccb  # noqa: E402


class GrantLatencyFieldsTests(unittest.TestCase):
    def test_batch_and_amortized_are_separate(self):
        fields = ccb._grant_latency_fields(
            [200.0, 300.0, 100.0], [100, 100, 50], [2.0] * 100 + [3.0] * 100 + [2.0] * 50
        )
        self.assertEqual(fields["grant_batch_count"], 3)
        self.assertEqual(fields["grant_batch_latency_mean_ms"], 200.0)
        self.assertEqual(fields["grant_batch_latency_median_ms"], 200.0)
        self.assertEqual(fields["grant_batch_latency_p95_ms"], 300.0)
        self.assertEqual(fields["grant_amortized_ms_per_item"], 2.4)
        self.assertEqual(fields["grant_latency_p95_ms"], 3.0)

    def test_empty_inputs_yield_blank_cells(self):
        fields = ccb._grant_latency_fields([], [], [])
        self.assertEqual(fields["grant_batch_count"], 0)
        for key, value in fields.items():
            if key != "grant_batch_count":
                self.assertEqual(value, "", key)

    def test_run_fields_keep_legacy_columns(self):
        for col in ("grant_latency_mean_ms", "grant_latency_median_ms", "grant_latency_p95_ms"):
            self.assertIn(col, ccb.RUN_FIELDS)
        for col in ccb._grant_latency_fields([], [], []):
            self.assertIn(col, ccb.RUN_FIELDS)


class LoadOneContainerTimingTests(unittest.TestCase):
    def _handler(self, request: httpx.Request) -> httpx.Response:
        body = request.read()
        payload = json.loads(body) if body else {}
        if request.url.path.endswith("/registration"):
            items = payload["registrationRequest"]
            return httpx.Response(200, json={"registrationResponse": [{"response": {"responseCode": 0}}] * len(items)})
        if request.url.path.endswith("/grant"):
            items = payload["grantRequest"]
            codes = [0] * len(items)
            if len(items) == 50:
                codes[-1] = 400
            return httpx.Response(
                200, json={"grantResponse": [{"response": {"responseCode": c}} for c in codes]}
            )
        return httpx.Response(200, json={})

    def test_records_one_sample_per_batch_post(self):
        transport = httpx.MockTransport(self._handler)
        real_client = httpx.Client

        def fake_client(*_args, **kwargs):
            kwargs.pop("verify", None)
            return real_client(transport=transport, **kwargs)

        clock = iter([0.0, 0.2, 1.0, 1.3, 2.0, 2.1])
        fake_time = SimpleNamespace(perf_counter=lambda: next(clock))
        with mock.patch.object(ccb.httpx, "Client", fake_client), mock.patch.object(
            ccb, "time", fake_time
        ):
            res = ccb._load_one_container(
                port=1, ctx=None, worker_id=0, device_count=250,
                slice_low=ccb.BAND_LOW_HZ, slice_high=ccb.BAND_HIGH_HZ, seed=1,
            )

        self.assertEqual(res["grant_batch_items"], [100, 100, 50])
        for got, want in zip(res["grant_batch_ms"], [200.0, 300.0, 100.0]):
            self.assertAlmostEqual(got, want, places=6)
        self.assertEqual(res["devices_ok"], 249)
        self.assertEqual(res["devices_failed"], 1)
        self.assertEqual(len(res["grant_latencies_ms"]), 249)

        fields = ccb._grant_latency_fields(
            res["grant_batch_ms"], res["grant_batch_items"], res["grant_latencies_ms"]
        )
        self.assertEqual(fields["grant_batch_latency_p95_ms"], 300.0)
        self.assertEqual(fields["grant_amortized_ms_per_item"], 2.4)
        self.assertEqual(fields["grant_latency_p95_ms"], 3.0)


if __name__ == "__main__":
    unittest.main()
