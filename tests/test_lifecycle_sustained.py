"""Sustained mixed-load mode of lifecycle_decomp_bench (schedule, metrics, summary)."""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

LOAD_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "load"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

import lifecycle_decomp_bench as ldb  # noqa: E402

PROFILE = {"band_low_hz": 3_550_000_000, "band_high_hz": 3_700_000_000,
           "channel_width_hz": 10_000_000, "heartbeat_interval_sec": 60}
EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeSas:
    """In-memory SAS with a fake clock; each request costs ``cost[op]`` seconds."""

    def __init__(self, cost=None, tx_sec=60, hb_rc=0, raise_on=None):
        self.t = 0.0
        self.cost = {"heartbeat": 0.01, **(cost or {})}
        self.tx_sec, self.hb_rc, self.raise_on = tx_sec, hb_rc, raise_on
        self.requests: list[tuple[str, dict]] = []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def utcnow(self):
        return EPOCH + timedelta(seconds=self.t)

    def post(self, op, item):
        self.requests.append((op, item))
        self.t += self.cost.get(op, 0.01)
        if self.raise_on == op:
            raise ConnectionError("boom")
        rc, body = 0, {}
        if op == "registration":
            body = {"cbsdId": f"fcc/{item['cbsdSerialNumber']}"}
        elif op == "grant":
            body = {"grantId": f"g-{item['cbsdId']}", "heartbeatInterval": 60}
        elif op == "heartbeat":
            rc = self.hb_rc
            tx = self.utcnow().replace(microsecond=0) + timedelta(seconds=self.tx_sec)
            body = {"transmitExpireTime": tx.strftime("%Y-%m-%dT%H:%M:%SZ")}
        return [{"response": {"responseCode": rc}, **body}], 5.0, {}, SimpleNamespace(status_code=200)


def run(sas, **kw):
    params = dict(fcc="fcc", user="u", seed=7, initial_devices=3, duration_sec=10.0,
                  heartbeat_interval_sec=2.0, arrival_interval_sec=4.0,
                  relinquish_interval_sec=5.0, late_threshold_ms=500.0)
    params.update(kw)
    return ldb._run_sustained(sas.post, PROFILE, clock=sas.clock, sleep=sas.sleep,
                              utcnow=sas.utcnow, **params)


class SequenceTests(unittest.TestCase):
    def test_initial_population_then_mixed_window(self):
        sas = FakeSas()
        rows, info = run(sas)
        self.assertEqual([r["operation"] for r in rows[:6]], ["registration", "grant"] * 3)
        self.assertTrue(all(r["event"] == "initial" for r in rows[:6]))
        ops = Counter(r["operation"] for r in rows)
        self.assertEqual(info["arrivals"], 2)          # t=4, t=8
        self.assertEqual(ops["registration"], 5)
        self.assertEqual(ops["grant"], 5)
        self.assertEqual(ops["relinquishment"], 1)     # t=5 (t=10 is outside the window)
        self.assertEqual((info["granted"], info["relinquished"], info["active_at_end"]), (5, 1, 4))
        self.assertEqual(info["heartbeat_interval_sec_used"], 2.0)
        self.assertTrue(all(r["success"] == 1 for r in rows))

    def test_heartbeat_state_period_and_stop_after_relinquish(self):
        sas = FakeSas()
        rows, _ = run(sas)
        hb_items = [i for op, i in sas.requests if op == "heartbeat"]
        by_dev: dict[str, list[str]] = {}
        for item in hb_items:
            by_dev.setdefault(item["cbsdId"], []).append(item["operationState"])
        for states in by_dev.values():
            self.assertEqual(states[0], "GRANTED")
            self.assertTrue(all(s == "AUTHORIZED" for s in states[1:]))

        hb = [r for r in rows if r["operation"] == "heartbeat"]
        for idx in {r["cbsd_index"] for r in hb}:
            sched = [r["scheduled_sec"] for r in hb if r["cbsd_index"] == idx]
            self.assertTrue(all(abs((b - a) - 2.0) < 1e-6 for a, b in zip(sched, sched[1:])))
        victim = next(r for r in rows if r["operation"] == "relinquishment")
        after = [r for r in hb if r["cbsd_index"] == victim["cbsd_index"]
                 and r["sent_sec"] > victim["sent_sec"]]
        self.assertEqual(after, [])
        self.assertEqual(sum(r["late"] for r in hb), 0)
        self.assertEqual(sum(r["missed"] for r in hb), 0)

    def test_reproducible_with_same_seed(self):
        seq = lambda: [(r["operation"], r["cbsd_index"]) for r in run(FakeSas())[0]]  # noqa: E731
        self.assertEqual(seq(), seq())

    def test_initial_heartbeats_are_staggered(self):
        rows, _ = run(FakeSas(), arrival_interval_sec=None, relinquish_interval_sec=None,
                      initial_devices=4, duration_sec=1.9)
        first = sorted(r["scheduled_sec"] for r in rows if r["operation"] == "heartbeat")
        base = first[0]
        self.assertEqual([round(x - base, 3) for x in first], [0.0, 0.5, 1.0, 1.5])


class MetricTests(unittest.TestCase):
    def test_overload_marks_late_and_missed(self):
        sas = FakeSas(cost={"heartbeat": 1.5}, tx_sec=1)
        rows, _ = run(sas, arrival_interval_sec=None, relinquish_interval_sec=None)
        hb = [r for r in rows if r["operation"] == "heartbeat"]
        self.assertGreater(sum(r["late"] for r in hb), 0)
        self.assertGreater(sum(r["missed"] for r in hb), 0)
        self.assertTrue(all(r["transmit_margin_ms"] == "" for r in hb if r["heartbeat_seq"] == 1))

    def test_http_and_sas_outcomes_are_separate(self):
        rows, _ = run(FakeSas(hb_rc=500), arrival_interval_sec=None, relinquish_interval_sec=None)
        summary = {(s["operation"], s["metric"]): s["value"]
                   for s in ldb._summarize_sustained(rows, "p")}
        n = summary[("heartbeat", "requests")]
        self.assertGreater(n, 0)
        self.assertEqual(summary[("heartbeat", "http_200")], n)
        self.assertEqual(summary[("heartbeat", "sas_response_code_0")], 0)
        self.assertEqual(summary[("heartbeat", "response_code_500")], n)
        self.assertEqual(summary[("heartbeat", "success_rate")], 0.0)
        # failed heartbeats keep GRANTED state and no transmitExpireTime
        self.assertTrue(all(r["missed"] == 0 for r in rows if r["operation"] == "heartbeat"))

    def test_transport_error_is_recorded_and_run_continues(self):
        rows, _ = run(FakeSas(raise_on="relinquishment"))
        rlq = [r for r in rows if r["operation"] == "relinquishment"]
        self.assertEqual(len(rlq), 1)
        self.assertEqual((rlq[0]["http_status"], rlq[0]["success"]), ("", 0))
        self.assertIn("ConnectionError", rlq[0]["error"])
        self.assertGreater(sum(1 for r in rows if r["sent_sec"] > rlq[0]["sent_sec"]), 0)
        summary = {(s["operation"], s["metric"]): s["value"]
                   for s in ldb._summarize_sustained(rows, "p")}
        self.assertEqual(summary[("relinquishment", "transport_errors")], 1)

    def test_summary_latency_is_per_request(self):
        rows = [{"operation": "heartbeat", "client_ms": v, "http_status": 200, "response_code": 0,
                 "success": 1, "error": "", "lateness_ms": 0.0, "late": 0, "missed": 0}
                for v in [float(i) for i in range(1, 101)]]
        summary = {(s["operation"], s["metric"]): s["value"]
                   for s in ldb._summarize_sustained(rows, "p")}
        self.assertEqual(summary[("heartbeat", "client_ms_median")], 50.5)
        self.assertEqual(summary[("heartbeat", "client_ms_max")], 100.0)
        self.assertGreaterEqual(summary[("heartbeat", "client_ms_p95")], 95.0)
        self.assertTrue(set(rows[0]) <= set(ldb.SUSTAINED_RAW_FIELDS))


if __name__ == "__main__":
    unittest.main()
