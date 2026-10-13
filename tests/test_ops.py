"""Safety boundaries for traffic changes and health analysis (stdlib unittest)."""

import copy
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from scripts.canary_ops import (
    CANARY, STABLE, FailureWindow, OperationError, Prometheus,
    Runtime, Server, analyze_observations, load, rollback, traffic,
)


class FakeRuntime:
    def __init__(self, stable=100, canary=0, stable_status="UP", canary_status="UP", fail_once=None):
        self.state = {
            "stable": Server("stable", stable_status, stable),
            "canary": Server("canary", canary_status, canary),
        }
        self.calls = []
        self.fail_once = fail_once
        self.locked = False
        self.lock_entries = 0

    @contextmanager
    def mutation_lock(self):
        if self.locked:
            raise AssertionError("Nested mutation lock would deadlock")
        self.locked = True
        self.lock_entries += 1
        try:
            yield
        finally:
            self.locked = False

    def servers(self):
        return dict(self.state)

    def weight(self, name, weight):
        if not self.locked:
            raise AssertionError("Weight mutation must hold exclusive lock")
        self.calls.append((name, weight))
        if self.fail_once == (name, weight):
            self.fail_once = None
            raise OperationError("injected command failure")
        current = self.state[name]
        self.state[name] = Server(name, current.status, weight)


def observations(canary_errors=0, p95=0.2, count=150, stable_errors=0):
    return {
        STABLE: {"requests_5m": 2850, "rps": 9.5, "error_rps": 9.5 * stable_errors, "p95_seconds": 0.35},
        CANARY: {"requests_5m": count, "rps": 0.5, "error_rps": 0.5 * canary_errors, "p95_seconds": p95},
    }


class HealthyPrometheus:
    def analyze(self, **_):
        return analyze_observations(observations())


class TrafficTests(unittest.TestCase):
    def test_initial_probe_is_ordered_and_needs_no_metric_history(self):
        runtime = FakeRuntime()
        state = traffic(runtime, 5)
        self.assertEqual(runtime.calls, [("canary", 5), ("stable", 95)])
        self.assertEqual(state["canary"].weight, 5)

    def test_beyond_ten_percent_requires_manual_approval(self):
        runtime = FakeRuntime(90, 10)
        with self.assertRaisesRegex(OperationError, "manual approval"):
            traffic(runtime, 25, prometheus=HealthyPrometheus())
        self.assertEqual(runtime.calls, [])

    def test_approved_promotion_requires_healthy_samples(self):
        runtime = FakeRuntime(90, 10)
        unhealthy = HealthyPrometheus()
        unhealthy.analyze = lambda: analyze_observations(observations(count=99))
        with self.assertRaisesRegex(OperationError, "Promotion blocked"):
            traffic(runtime, 25, approve=True, prometheus=unhealthy)
        self.assertEqual(runtime.calls, [])
        traffic(runtime, 25, approve=True, prometheus=HealthyPrometheus())
        self.assertEqual(runtime.calls, [("canary", 25), ("stable", 75)])

    def test_unavailable_canary_blocks_increase_without_mutation(self):
        runtime = FakeRuntime(canary_status="DOWN")
        with self.assertRaisesRegex(OperationError, "must be UP"):
            traffic(runtime, 5)
        self.assertEqual(runtime.calls, [])

    def test_reduction_does_not_require_new_approval(self):
        runtime = FakeRuntime(50, 50)
        traffic(runtime, 25)
        self.assertEqual(runtime.calls, [("stable", 75), ("canary", 25)])

    def test_rollback_restores_stable_before_disabling_canary(self):
        runtime = FakeRuntime(0, 100)
        rollback(runtime)
        self.assertEqual(runtime.calls, [("stable", 100), ("canary", 0)])

    def test_rollback_preserves_canary_when_stable_is_unavailable(self):
        runtime = FakeRuntime(0, 100, stable_status="DOWN")
        with self.assertRaisesRegex(OperationError, "canary remains enabled"):
            rollback(runtime)
        self.assertEqual(runtime.calls, [("stable", 100)])
        self.assertEqual(runtime.state["canary"].weight, 100)

    def test_partial_change_recovers_to_stable(self):
        runtime = FakeRuntime(95, 5, fail_once=("stable", 90))
        with self.assertRaisesRegex(OperationError, "Recovered to stable=100"):
            traffic(runtime, 10)
        self.assertEqual(runtime.calls, [("canary", 10), ("stable", 90), ("stable", 100), ("canary", 0)])
        self.assertEqual(runtime.state["stable"].weight, 100)
        self.assertEqual(runtime.state["canary"].weight, 0)
        self.assertEqual(runtime.lock_entries, 1)
        self.assertFalse(runtime.locked)

    def test_out_of_range_never_reaches_runtime(self):
        for value in (-1, 101):
            runtime = FakeRuntime()
            with self.assertRaisesRegex(OperationError, "between 0 and 100"):
                traffic(runtime, value)
            self.assertEqual(runtime.calls, [])


class AnalysisTests(unittest.TestCase):
    def test_enough_healthy_data_allows_gate(self):
        result = analyze_observations(observations())
        self.assertTrue(result.healthy)

    def test_threshold_equality_is_healthy(self):
        result = analyze_observations(observations(canary_errors=0.01, p95=0.4, count=100))
        self.assertTrue(result.healthy)

    def test_error_latency_regressions_are_detected(self):
        result = analyze_observations(observations(canary_errors=0.2, p95=0.5))
        self.assertTrue(result.bad)
        self.assertFalse(result.healthy)
        self.assertEqual(len(result.reasons), 3)

    def test_missing_data_is_never_healthy(self):
        for version, metric in ((CANARY, "requests_5m"), (CANARY, "p95_seconds"), (STABLE, "rps")):
            data = observations()
            data[version][metric] = None
            result = analyze_observations(data)
            self.assertFalse(result.enough_data)
            self.assertFalse(result.healthy)

    def test_at_full_rollout_absolute_checks_work_without_baseline(self):
        data = observations()
        data[STABLE]["rps"] = 0
        self.assertFalse(analyze_observations(copy.deepcopy(data)).healthy)
        self.assertTrue(analyze_observations(copy.deepcopy(data), require_baseline=False).healthy)
        data[CANARY]["error_rps"] = 0.1
        self.assertTrue(analyze_observations(data, require_baseline=False).bad)

    def test_failure_window_requires_three_continuous_minutes(self):
        window = FailureWindow(180)
        bad = analyze_observations(observations(canary_errors=0.2))
        good = analyze_observations(observations())
        no_data = analyze_observations(observations(count=0))
        self.assertFalse(window.observe(bad, 10))
        self.assertFalse(window.observe(bad, 189))
        self.assertTrue(window.observe(bad, 190))
        self.assertFalse(window.observe(good, 200))
        self.assertFalse(window.observe(bad, 210))
        self.assertFalse(window.observe(no_data, 380))
        self.assertFalse(window.observe(bad, 390))


class RuntimeProtocolTests(unittest.TestCase):
    def test_stat_csv_filters_backend_and_parses_health_weights(self):
        runtime = Runtime()
        runtime.command = lambda _: (
            "# pxname,svname,status,weight,bck,\n"
            "transaction_engine,stable,UP,95,0,\n"
            "transaction_engine,canary,DOWN,5,0,\n"
            "transaction_engine,stable_fallback,UP,100,1,\n"
            "another_backend,canary,UP,100,0,\n"
        )
        state = runtime.servers()
        self.assertTrue(state["stable"].up)
        self.assertFalse(state["canary"].up)
        self.assertEqual(state["canary"].weight, 5)
        self.assertEqual(len(state), 3)

    def test_nonempty_mutation_response_is_rejected(self):
        runtime = Runtime()
        runtime.command = lambda _: "Permission denied"
        with self.assertRaisesRegex(OperationError, "rejected weight update"):
            runtime.weight("canary", 5)

    def test_mutation_is_verified_against_runtime_state(self):
        runtime = Runtime()
        runtime.command = lambda _: ""
        runtime.servers = lambda: {"canary": Server("canary", "UP", 0)}
        with self.assertRaisesRegex(OperationError, "verification failed"):
            runtime.weight("canary", 5)

    def test_missing_server_rows_fail_closed(self):
        runtime = Runtime()
        runtime.command = lambda _: "# pxname,svname,status,weight,\ntransaction_engine,stable,UP,100,\n"
        with self.assertRaisesRegex(OperationError, "missing stable or canary"):
            runtime.servers()

    def test_configured_weight_is_used_when_effective_weight_changes(self):
        runtime = Runtime()
        runtime.command = lambda _: (
            "# pxname,svname,status,weight,uweight,\n"
            "transaction_engine,stable,DOWN,0,95,\n"
            "transaction_engine,canary,UP,5,5,\n"
        )
        self.assertEqual(runtime.servers()["stable"].weight, 95)


class PrometheusResponseTests(unittest.TestCase):
    def test_nan_and_empty_results_are_missing_data(self):
        for result in ([], [{"value": [100, "NaN"]}]):
            response = io.BytesIO(json.dumps({"status": "success", "data": {"result": result}}).encode())
            with patch("urllib.request.urlopen", return_value=response):
                self.assertIsNone(Prometheus().query("test_metric"))

    def test_load_preflight_refuses_a_non_mock(self):
        response = io.BytesIO(b'{"ready":true}')
        with patch("urllib.request.urlopen", return_value=response) as request:
            with self.assertRaisesRegex(OperationError, "Refusing load"):
                load("http://localhost", 1, 1, 1)
            self.assertEqual(request.call_count, 1)

    def test_load_rejects_external_hosts_before_network_request(self):
        with patch("urllib.request.urlopen") as request:
            with self.assertRaisesRegex(OperationError, "local mock host"):
                load("https://api.example.com", 1, 1, 1)
            request.assert_not_called()

    def test_current_scrape_loss_blocks_even_with_historical_good_metrics(self):
        prometheus = Prometheus()
        expressions = []
        def query(expression):
            expressions.append(expression)
            if "timestamp" in expression:
                return 5
            if 'slot="canary"' in expression:
                return 0
            return 1
        prometheus.query = query
        result = prometheus.analyze()
        self.assertFalse(result.healthy)
        self.assertTrue(any("canary" in reason and "not UP" in reason for reason in result.reasons))
        self.assertFalse(any("transaction_requests_total" in expression for expression in expressions))

    def test_stale_scrapes_block_promotion(self):
        prometheus = Prometheus()
        prometheus.query = lambda expression: 31 if "timestamp" in expression else 1
        result = prometheus.analyze()
        self.assertFalse(result.enough_data)
        self.assertTrue(any("older than 30" in reason for reason in result.reasons))

    def test_haproxy_and_required_stable_scrape_loss_block_promotion(self):
        for selector in ('job="haproxy"', 'slot="stable"'):
            prometheus = Prometheus()
            prometheus.query = lambda expression: 5 if "timestamp" in expression else (0 if selector in expression else 1)
            self.assertFalse(prometheus.analyze().healthy)

    def test_full_rollout_needs_fresh_canary_and_proxy_without_stable(self):
        prometheus = Prometheus()
        expressions = []
        def query(expression):
            expressions.append(expression)
            if "timestamp" in expression:
                return 30
            if expression.startswith("min(up"):
                return 1
            if "increase" in expression:
                return 150
            if 'status=~"5.."' in expression:
                return 0
            if "histogram_quantile" in expression:
                return 0.2
            return 1
        prometheus.query = query
        self.assertTrue(prometheus.analyze(require_baseline=False).healthy)
        self.assertFalse(any('slot="stable"' in expression for expression in expressions))


class MutationLockTests(unittest.TestCase):
    def test_flock_release_even_when_operation_fails(self):
        calls = []
        fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4,
                                     flock=lambda descriptor, mode: calls.append(mode))
        with patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                patch("os.open", return_value=123), patch("os.close") as close:
            with self.assertRaisesRegex(OperationError, "failed operation"):
                with Runtime().mutation_lock():
                    raise OperationError("failed operation")
        self.assertEqual(calls, [3, 4])
        close.assert_called_once_with(123)


    def test_lock_contention_times_out_without_mutation(self):
        def blocked(*_):
            raise BlockingIOError("another process owns the lock")
        fake_fcntl = SimpleNamespace(LOCK_EX=1, LOCK_NB=2, LOCK_UN=4, flock=blocked)
        with patch.dict("sys.modules", {"fcntl": fake_fcntl}), \
                patch("os.open", return_value=123), patch("os.close") as close, \
                patch("time.monotonic", side_effect=[0, 5]):
            with self.assertRaisesRegex(OperationError, "waited 5 seconds"):
                with Runtime().mutation_lock():
                    self.fail("contended lock must never enter mutation body")
        close.assert_called_once_with(123)


class CliStartupTests(unittest.TestCase):
    def test_direct_script_help_starts_from_an_unrelated_working_directory(self):
        root = Path(__file__).resolve().parents[1]
        local_directory = root / ".local"
        local_directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ops-cli-test-", dir=local_directory) as directory:
            environment = dict(os.environ)
            environment.update({"PYTHONDONTWRITEBYTECODE": "1", "TEMP": directory,
                                "TMP": directory, "TMPDIR": directory})
            for command in ([], ["traffic"]):
                with self.subTest(command=command):
                    result = subprocess.run(
                        [sys.executable, "-B", str(root / "scripts/canary_ops.py"), *command, "--help"],
                        cwd=directory, env=environment, capture_output=True, text=True, timeout=15,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("traffic", result.stdout)
                    self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
