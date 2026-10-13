"""Exercise real local HTTP behavior, metrics, secret reload, and draining."""

from __future__ import annotations

import dataclasses
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from app.main import Config, EngineServer, LOGGER


class ConfigTests(unittest.TestCase):
    def test_environment_configuration_and_rejects_invalid_values(self) -> None:
        config = Config.from_env({
            "VERSION": "v2.0", "ERROR_RATE": "0.2", "DELAY_MIN_MS": "120",
            "DELAY_MAX_MS": "220", "PROVIDER_API_KEY_FILE": "/run/secrets/rotatable-key",
        })
        self.assertEqual(config.version, "v2.0")
        self.assertEqual(config.error_rate, 0.2)
        self.assertEqual(config.delay_min_ms, 120)
        self.assertEqual(config.provider_api_key_file, "/run/secrets/rotatable-key")
        for env in (
            {"ERROR_RATE": "NaN"}, {"ERROR_RATE": "2"}, {"DECLINE_RATE": "-1"},
            {"DELAY_MIN_MS": "500", "DELAY_MAX_MS": "100"}, {"PORT": "0"},
            {"MAX_REQUEST_BYTES": "0"}, {"REQUEST_TIMEOUT_SECONDS": "0"},
            {"VERSION": 'v2.0"}\nmalicious'}, {"LOG_LEVEL": "SECRET"},
        ):
            with self.subTest(env=env), self.assertRaises(ValueError):
                Config.from_env(env)


class HTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep transient test files on the project's D: drive, never on C:.
        self.temp = tempfile.TemporaryDirectory(prefix=".app-test-", dir=Path(__file__).parent)
        self.database = Path(self.temp.name) / "database_url"
        self.provider = Path(self.temp.name) / "provider_api_key"
        self.database.write_text("mock://database-only-for-local-validation\n", encoding="utf-8")
        self.provider.write_text("fake-local-secret-do-not-log-xyq748\n", encoding="utf-8")
        self.config = Config(
            version="v2.0", host="127.0.0.1", port=0,
            delay_min_ms=0, delay_max_ms=0, decline_rate=0,
            database_url_file=str(self.database), provider_api_key_file=str(self.provider),
        )
        self.server = EngineServer(("127.0.0.1", 0), self.config)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        self.thread.start()
        self.closed = False

    def tearDown(self) -> None:
        if not self.closed:
            self.server.state.begin_draining()
            self.server.shutdown()
            self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method: str, path: str, body: object = None, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None and not isinstance(body, bytes) else body
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def await_count(self, count: int) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with self.server.state.lock:
                current = self.server.state.duration_count
            if current >= count:
                return
            time.sleep(0.002)
        self.fail(f"authorization observations did not reach {count}")

    def test_readiness_version_and_initial_metrics(self) -> None:
        status, _, body = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"ready": True, "version": "v2.0", "simulated": True})
        status, headers, body = self.request("GET", "/version")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Transaction-Version"], "v2.0")
        self.assertEqual(json.loads(body)["version"], "v2.0")
        status, headers, body = self.request("GET", "/metrics")
        self.assertEqual(status, 200)
        self.assertIn("version=0.0.4", headers["Content-Type"])
        metrics = body.decode()
        self.assertIn('transaction_request_duration_seconds_count{version="v2.0"} 0', metrics)
        self.assertIn('transaction_ready{version="v2.0"} 1', metrics)
        self.assertIn('transaction_inflight_requests{version="v2.0"} 0', metrics)

    def test_authorization_and_prometheus_outcome_histogram(self) -> None:
        status, headers, body = self.request("POST", "/authorize", {"amount": 19.95, "currency": "USD"})
        response = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Transaction-Version"], "v2.0")
        self.assertEqual(response["status"], "authorized")
        self.assertTrue(response["simulated"])
        self.assertFalse(response["durable_state"])
        self.assertFalse(response["idempotency_guaranteed"])
        self.assertEqual(response["amount"], 19.95)
        self.await_count(1)
        metrics = self.request("GET", "/metrics")[2].decode()
        self.assertIn('transaction_requests_total{version="v2.0",status="200"} 1', metrics)
        self.assertIn('transaction_approvals_total{version="v2.0",outcome="approved"} 1', metrics)
        self.assertIn('transaction_request_duration_seconds_bucket{version="v2.0",le="+Inf"} 1', metrics)
        self.assertIn('transaction_request_duration_seconds_count{version="v2.0"} 1', metrics)
        self.assertIn('transaction_inflight_requests{version="v2.0"} 0', metrics)
        self.assertGreater(self.server.state.duration_sum, 0)
        previous = 0
        for count in self.server.state.buckets.values():
            self.assertGreaterEqual(count, previous)
            self.assertLessEqual(count, 1)
            previous = count

    def test_client_validation_is_bounded_and_does_not_count_business_failures(self) -> None:
        invalid = (
            (b"not-json", 400, {}),
            ([], 400, {}),
            ({"amount": True}, 400, {}),
            ({"amount": 0}, 400, {}),
            ({"amount": -5}, 400, {}),
            ({"amount": 10 ** 500}, 400, {}),
            (b'{"amount": NaN}', 400, {}),
            (b'{"amount": Infinity}', 400, {}),
            ({"amount": 1, "currency": "usd"}, 400, {}),
            ({"amount": 1, "merchant_id": ""}, 400, {}),
            ({"amount": 1}, 400, {"Idempotency-Key": "x" * 129}),
            ({"amount": 1}, 415, {"Content-Type": "text/plain"}),
            (b"x" * (self.config.max_request_bytes + 1), 413, {}),
        )
        for body, expected, headers in invalid:
            with self.subTest(body_type=type(body).__name__, expected=expected, headers=headers):
                status, _, response = self.request("POST", "/authorize", body, headers)
                self.assertEqual(status, expected)
                self.assertTrue(json.loads(response)["simulated"])
        self.await_count(len(invalid))
        self.assertEqual(self.server.state.approvals, {"approved": 0, "declined": 0, "failed": 0})
        self.assertEqual(self.server.state.inflight, 0)

    def test_missing_or_conflicting_lengths_are_rejected(self) -> None:
        examples = (
            (b"", 411),
            (b"Content-Length: 1\r\nContent-Length: 1\r\n", 400),
            (b"Content-Length: -1\r\n", 400),
            (b"Transfer-Encoding: chunked\r\n", 400),
            (b"Content-Length: " + b"9" * 5000 + b"\r\n", 413),
        )
        for length_headers, expected in examples:
            with self.subTest(expected=expected, header_length=len(length_headers)):
                with socket.create_connection(("127.0.0.1", self.port), timeout=3) as client:
                    client.sendall(
                        b"POST /authorize HTTP/1.1\r\nHost: localhost\r\n"
                        b"Content-Type: application/json\r\n" + length_headers + b"Connection: close\r\n\r\n"
                    )
                    response = http.client.HTTPResponse(client)
                    response.begin()
                    self.assertEqual(response.status, expected)
                    response.read()
        self.await_count(len(examples))

    def test_fault_injection_and_declines_have_distinct_metrics(self) -> None:
        self.server.state.config = dataclasses.replace(self.config, error_rate=1)
        status, _, body = self.request("POST", "/authorize", {"amount": 1})
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body)["error"], "simulated_provider_failure")
        self.server.state.config = dataclasses.replace(self.config, decline_rate=1)
        status, _, body = self.request("POST", "/authorize", {"amount": 1})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "declined")
        self.await_count(2)
        self.assertEqual(self.server.state.requests[500], 1)
        self.assertEqual(self.server.state.requests[200], 1)
        self.assertEqual(self.server.state.approvals, {"approved": 0, "declined": 1, "failed": 1})

    def test_secret_files_reload_and_readiness_recovers_without_restart(self) -> None:
        self.provider.write_text("\n", encoding="utf-8")
        self.assertEqual(self.request("GET", "/health")[0], 503)
        status, _, body = self.request("POST", "/authorize", {"amount": 1})
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"], "not_ready")
        self.provider.write_text("rotated-fake-local-secret\n", encoding="utf-8")
        self.assertEqual(self.request("GET", "/health")[0], 200)
        self.assertEqual(self.request("POST", "/authorize", {"amount": 1})[0], 200)
        self.await_count(2)
        self.database.unlink()
        self.assertEqual(self.request("GET", "/health")[0], 503)
        metrics = self.request("GET", "/metrics")[2].decode()
        self.assertIn('transaction_ready{version="v2.0"} 0', metrics)
        self.assertNotIn("rotated-fake-local-secret", metrics)

    def test_keys_produce_consistent_demo_ids_without_claiming_idempotency(self) -> None:
        headers = {"Idempotency-Key": "demo-key"}
        first = json.loads(self.request("POST", "/authorize", {"amount": 1}, headers)[2])
        second = json.loads(self.request("POST", "/authorize", {"amount": 2}, headers)[2])
        self.assertEqual(first["transaction_id"], second["transaction_id"])
        self.assertFalse(first["idempotency_guaranteed"])
        # The second request executes again; there is deliberately no dedup store.
        self.assertEqual(second["amount"], 2)
        self.await_count(2)
        self.assertEqual(self.server.state.requests[200], 2)

    def test_structured_logs_do_not_include_secrets_card_data_or_merchant_input(self) -> None:
        marker = "sensitive-body-value-never-log-71234"
        with self.assertLogs(LOGGER, level="INFO") as captured:
            self.request("POST", "/authorize", {"amount": 1, "merchant_id": marker, "card": marker}, {"Idempotency-Key": marker})
            deadline = time.monotonic() + 2
            while not captured.records and time.monotonic() < deadline:
                time.sleep(0.002)
        self.assertTrue(captured.records)
        records = [json.loads(record.getMessage()) for record in captured.records]
        self.assertEqual(records[0]["event"], "authorization")
        self.assertEqual(set(records[0]), {"event", "version", "status", "duration_ms"})
        logged = "\n".join(captured.output)
        self.assertNotIn(marker, logged)
        self.assertNotIn(self.provider.read_text(encoding="utf-8").strip(), logged)
        self.assertNotIn(self.database.read_text(encoding="utf-8").strip(), logged)

    def test_draining_rejects_new_work_but_finishes_admitted_request(self) -> None:
        self.server.state.config = dataclasses.replace(self.config, delay_min_ms=350, delay_max_ms=350)
        result: list[tuple[int, dict[str, str], bytes]] = []
        errors: list[Exception] = []

        def authorize() -> None:
            try:
                result.append(self.request("POST", "/authorize", {"amount": 1}))
            except Exception as exc:
                errors.append(exc)

        worker = threading.Thread(target=authorize)
        worker.start()
        deadline = time.monotonic() + 2
        while self.server.state.inflight == 0 and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertEqual(self.server.state.inflight, 1)
        metrics = self.request("GET", "/metrics")[2].decode()
        self.assertIn('transaction_inflight_requests{version="v2.0"} 1', metrics)
        self.assertTrue(self.server.state.begin_draining())
        self.assertFalse(self.server.state.begin_draining())
        self.assertEqual(self.request("GET", "/health")[0], 503)
        self.assertEqual(self.request("POST", "/authorize", {"amount": 2})[0], 503)
        self.server.shutdown()
        self.server.server_close()
        self.closed = True
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result[0][0], 200)
        self.assertEqual(json.loads(result[0][2])["status"], "authorized")
        self.assertEqual(self.server.state.inflight, 0)
        self.assertEqual(self.server.state.requests[200], 1)
        self.assertEqual(self.server.state.requests[503], 1)


if __name__ == "__main__":
    unittest.main()
