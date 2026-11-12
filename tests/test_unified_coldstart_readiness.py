"""Readiness observation semantics in unified_coldstart (polling window, first-200 snapshot)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx

LOAD_DIR = Path(__file__).resolve().parents[1] / "benchmarks" / "load"
if str(LOAD_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_DIR))

import common  # noqa: E402
import unified_coldstart as uc  # noqa: E402


def _mock_client_factory(handler):
    real_client = httpx.Client
    transport = httpx.MockTransport(handler)

    def factory(*_args, **kwargs):
        kwargs.pop("verify", None)
        return real_client(transport=transport, **kwargs)

    return factory


class WaitHttp200TraceTests(unittest.TestCase):
    def _handler_ready_on_third(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise httpx.ConnectError("refused", request=request)
            return httpx.Response(200, json={"status": "ok", "startup": {"k": 1}})

        return handler

    def test_trace_brackets_first_success_and_keeps_body(self):
        trace: dict = {}
        with mock.patch.object(common.httpx, "Client", _mock_client_factory(self._handler_ready_on_third())):
            ok, polls, err = common.wait_http_200(
                "https://x/ready", None, 5.0, poll_interval_sec=0.0, trace=trace
            )
        self.assertEqual((ok, polls, err), (True, 3, None))
        self.assertLessEqual(trace["last_failed_start"], trace["success_start"])
        self.assertLessEqual(trace["success_start"], trace["success_end"])
        self.assertEqual(trace["body"], {"status": "ok", "startup": {"k": 1}})
        self.assertEqual(trace["last_failed_error"], "refused")
        self.assertEqual(trace["poll_interval_sec"], 0.0)

    def test_without_trace_return_contract_unchanged(self):
        with mock.patch.object(common.httpx, "Client", _mock_client_factory(self._handler_ready_on_third())):
            self.assertEqual(
                common.wait_http_200("https://x/ready", None, 5.0, poll_interval_sec=0.0),
                (True, 3, None),
            )

    def test_first_attempt_success_has_no_failed_start(self):
        trace: dict = {}
        handler = lambda request: httpx.Response(200, json={"status": "ok"})  # noqa: E731
        with mock.patch.object(common.httpx, "Client", _mock_client_factory(handler)):
            common.wait_http_200("https://x/ready", None, 5.0, poll_interval_sec=0.0, trace=trace)
        self.assertIsNone(trace["last_failed_start"])
        self.assertEqual(uc._ready_window(trace, 0.0)["rsa_last_failed_probe_start_ms"], "")

    def test_record_attempts_lists_failed_and_success(self):
        trace: dict = {"attempts": []}
        with mock.patch.object(common.httpx, "Client", _mock_client_factory(self._handler_ready_on_third())):
            ok, polls, err = common.wait_http_200(
                "https://x/ready", None, 5.0, poll_interval_sec=0.01,
                trace=trace, record_attempts=True,
            )
        self.assertEqual((ok, polls, err), (True, 3, None))
        self.assertEqual(len(trace["attempts"]), 3)
        self.assertFalse(trace["attempts"][0]["ok"])
        self.assertFalse(trace["attempts"][1]["ok"])
        self.assertTrue(trace["attempts"][2]["ok"])
        self.assertEqual(trace["attempts"][0]["sleep_after_sec"], 0.01)
        self.assertEqual(trace["attempts"][2]["sleep_after_sec"], 0.0)
        # Effective gap between failed starts ≈ attempt duration + sleep
        gap = trace["attempts"][1]["start"] - trace["attempts"][0]["start"]
        self.assertGreaterEqual(gap, 0.01)


class ReadyWindowTests(unittest.TestCase):
    def test_window_relative_to_t0(self):
        trace = {
            "last_failed_start": 10.8, "last_failed_error": "EOF",
            "success_start": 10.85, "success_end": 10.89,
        }
        self.assertEqual(
            uc._ready_window(trace, 10.0),
            {
                "rsa_last_failed_probe_start_ms": 800.0,
                "rsa_last_failed_probe_error": "EOF",
                "rsa_ready_probe_ms": 40.0,
            },
        )


class RunOnceUsesFirstReadyResponseTests(unittest.TestCase):
    def _run(self, internal_total_ms: float) -> dict:
        startup = {
            "stages_ms": {
                "python_bootstrap_ms": 100.0,
                "profile_load_ms": 10.0,
                "database_init_ms": 20.0,
                "tls_init_ms": 5.0,
                "internal_other_ms": internal_total_ms - 135.0,
                "internal_total_ms": internal_total_ms,
            }
        }

        def fake_wait(url, ctx, timeout_sec, *, trace=None, **_kw):
            trace.update(
                last_failed_start=0.8, success_start=0.95, success_end=0.99,
                body={"status": "ok", "startup": startup},
            )
            return True, 7, None

        clock = iter([0.0, 0.1, 1.0])
        fake_time = SimpleNamespace(perf_counter=lambda: next(clock), time=lambda: 0.0)
        with mock.patch.object(uc, "docker_run"), mock.patch.object(uc, "docker_rm"), \
                mock.patch.object(uc, "_docker_logs_timestamps", return_value=""), \
                mock.patch.object(uc, "wait_http_200", fake_wait), \
                mock.patch.object(uc, "time", fake_time), \
                mock.patch("httpx.Client", side_effect=AssertionError("second /ready request")):
            return uc._run_once(
                profile_path=Path("p.json"), profile_id="p", image="img", host_port=1,
                certs_dir=Path("."), ctx=None, timeout_sec=5.0, repeat=1, order=1,
                temp_start=None,
            )

    def test_stages_come_from_first_200_and_window_is_recorded(self):
        row = self._run(500.0)
        self.assertEqual(row["exit_code"], 0, row["error"])
        self.assertEqual(row["cold_start_total_ms"], 1000.0)
        self.assertEqual(row["internal_total_ms"], 500.0)
        self.assertEqual(row["runtime_and_observation_gap_ms"], 400.0)
        self.assertEqual(row["external_residual_signed_ms"], 400.0)
        self.assertEqual(row["readiness_polling_ms"], 900.0)
        self.assertEqual(row["container_started_to_ready_observed_ms"], 900.0)
        self.assertEqual(row["rsa_last_failed_probe_start_ms"], 800.0)
        self.assertEqual(row["rsa_ready_probe_ms"], 40.0)
        self.assertEqual(row["ready_poll_count"], 7)
        self.assertEqual(row["ecdsa_ready_observed_ms"], "")
        self.assertEqual(set(row), set(uc.RUN_FIELDS))

    def test_signed_residual_exposes_overlap_hidden_by_clipped_gap(self):
        row = self._run(950.0)
        self.assertEqual(row["runtime_and_observation_gap_ms"], 0.0)
        self.assertEqual(row["external_residual_signed_ms"], -50.0)


class ProbeEcdsaTests(unittest.TestCase):
    def test_polls_port_9001_on_container_ip(self):
        seen = {}

        def fake_wait(url, ctx, timeout_sec, **_kw):
            seen["url"] = url
            return True, 2, None

        with mock.patch.object(uc, "_container_ip", return_value="172.17.0.2"), \
                mock.patch.object(uc, "wait_http_200", fake_wait):
            out = uc._probe_ecdsa("c", None, 1.0, uc.time.perf_counter())
        self.assertEqual(seen["url"], "https://172.17.0.2:9001/ready")
        self.assertEqual(out["ecdsa_ready_poll_count"], 2)
        self.assertNotEqual(out["ecdsa_ready_observed_ms"], "")
        self.assertEqual(out["ecdsa_ready_error"], "")

    def test_missing_ip_is_reported_not_raised(self):
        with mock.patch.object(uc, "_container_ip", return_value=""):
            out = uc._probe_ecdsa("c", None, 1.0, 0.0)
        self.assertEqual(out["ecdsa_ready_error"], "container ip unavailable")
        self.assertEqual(out["ecdsa_ready_observed_ms"], "")


if __name__ == "__main__":
    unittest.main()
