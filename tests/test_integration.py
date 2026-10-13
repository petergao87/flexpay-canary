"""Opt-in HTTP integration checks against the running local mock/proxy.

Run with FLEXPAY_INTEGRATION=1 and FLEXPAY_BASE_URL=http://127.0.0.1:8080.
These tests never alter HAProxy traffic weights or call real payment providers.
"""

import concurrent.futures
import json
import os
import unittest
import urllib.error
import urllib.parse
import urllib.request
import uuid


@unittest.skipUnless(os.environ.get("FLEXPAY_INTEGRATION") == "1", "opt in with FLEXPAY_INTEGRATION=1")
class HttpIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_url = os.environ.get("FLEXPAY_BASE_URL", "http://127.0.0.1:8080").rstrip("/")
        parsed = urllib.parse.urlparse(cls.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "proxy"}:
            raise RuntimeError("Integration checks only support local mock hosts")
        with urllib.request.urlopen(cls.base_url + "/health", timeout=5) as response:
            health = json.load(response)
        if health.get("simulated") is not True:
            raise RuntimeError("Refusing integration checks: target must declare simulated=true")

    def authorize(self, body):
        key = "integration-" + uuid.uuid4().hex
        request = urllib.request.Request(
            self.base_url + "/authorize", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": key},
        )
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_health_identifies_version_and_readiness(self):
        with urllib.request.urlopen(self.base_url + "/health", timeout=5) as response:
            health = json.load(response)
        self.assertTrue(health["ready"])
        self.assertIn(health["version"], {"v1.9", "v2.0"})

    def test_concurrent_authorizations_finish_and_report_version(self):
        requests = [{"amount": 100, "currency": "USD", "merchant_id": "integration",
                     "request_id": str(uuid.uuid4())} for _ in range(20)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(self.authorize, requests))
        ids = set()
        for status, body in results:
            self.assertEqual(status, 200)
            self.assertTrue(body["simulated"])
            self.assertIn(body["version"], {"v1.9", "v2.0"})
            self.assertIn(body["status"], {"authorized", "declined"})
            ids.add(body["transaction_id"])
        self.assertEqual(len(ids), len(results))

    def test_invalid_payment_is_4xx(self):
        status, _ = self.authorize({"amount": -1, "currency": "USD"})
        self.assertGreaterEqual(status, 400)
        self.assertLess(status, 500)


if __name__ == "__main__":
    unittest.main()
