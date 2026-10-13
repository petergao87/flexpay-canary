"""A standard-library-only authorization mock with operational behavior.

This service does not contact a provider, write transaction state, or guarantee
idempotency. It exists to demonstrate deployment, routing, and monitoring.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import signal
import socket
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping


LOGGER = logging.getLogger("transaction-engine")
BUCKETS = (0.05, 0.1, 0.15, 0.18, 0.2, 0.25, 0.3, 0.4, 0.5, 1.0, 2.0, 5.0)
ID_NAMESPACE = uuid.UUID("030e0e2b-73e4-42ba-a6f0-6b45140fbf40")


def emit(event: str, **fields: object) -> None:
    """Only callers' explicitly selected operational fields reach stdout."""
    LOGGER.info(json.dumps({"event": event, **fields}, separators=(",", ":")))


@dataclass(frozen=True)
class Config:
    version: str = "v1.9"
    host: str = "0.0.0.0"
    port: int = 8080
    delay_min_ms: float = 250.0
    delay_max_ms: float = 400.0
    error_rate: float = 0.0
    decline_rate: float = 0.02
    database_url_file: str = "/run/secrets/database_url"
    provider_api_key_file: str = "/run/secrets/provider_api_key"
    max_request_bytes: int = 16384
    request_timeout_seconds: float = 3.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        values = os.environ if env is None else env

        def number(name: str, default: str) -> float:
            try:
                result = float(values.get(name, default))
            except ValueError as exc:
                raise ValueError(f"{name} must be a finite number") from exc
            if not math.isfinite(result):
                raise ValueError(f"{name} must be a finite number")
            return result

        def integer(name: str, default: str) -> int:
            try:
                return int(values.get(name, default))
            except ValueError as exc:
                raise ValueError(f"{name} must be an integer") from exc

        version = values.get("VERSION", "v1.9")
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", version):
            raise ValueError("VERSION must contain 1 to 32 letters, digits, dots, underscores or hyphens")
        minimum = number("DELAY_MIN_MS", "250")
        maximum = number("DELAY_MAX_MS", "400")
        if not 0 <= minimum <= maximum <= 10000:
            raise ValueError("DELAY_MIN_MS and DELAY_MAX_MS must satisfy 0 <= min <= max <= 10000")
        error_rate = number("ERROR_RATE", "0")
        decline_rate = number("DECLINE_RATE", "0.02")
        if not 0 <= error_rate <= 1 or not 0 <= decline_rate <= 1:
            raise ValueError("ERROR_RATE and DECLINE_RATE must be between 0 and 1")
        port = integer("PORT", "8080")
        limit = integer("MAX_REQUEST_BYTES", "16384")
        timeout = number("REQUEST_TIMEOUT_SECONDS", "3")
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if not 1 <= limit <= 1048576:
            raise ValueError("MAX_REQUEST_BYTES must be between 1 and 1048576")
        if not 0.1 <= timeout <= 30:
            raise ValueError("REQUEST_TIMEOUT_SECONDS must be between 0.1 and 30")
        level = values.get("LOG_LEVEL", "INFO").upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return cls(
            version=version,
            host=values.get("HOST", "0.0.0.0"),
            port=port,
            delay_min_ms=minimum,
            delay_max_ms=maximum,
            error_rate=error_rate,
            decline_rate=decline_rate,
            database_url_file=values.get("DATABASE_URL_FILE", "/run/secrets/database_url"),
            provider_api_key_file=values.get("PROVIDER_API_KEY_FILE", "/run/secrets/provider_api_key"),
            max_request_bytes=limit,
            request_timeout_seconds=timeout,
            log_level=level,
        )


class SecretFiles:
    """Read mounted files anew, allowing an in-place update without restart.

    These checks establish only that the mock has readable configuration. They
    do not establish connectivity to a real database or acquirer.
    """

    def __init__(self, config: Config) -> None:
        self.paths = (config.database_url_file, config.provider_api_key_file)

    def ready(self) -> bool:
        for filename in self.paths:
            try:
                # Bound even misconfigured files; never log paths or contents.
                with Path(filename).open("rb") as secret_file:
                    content = secret_file.read(65537)
                if not content.strip() or len(content) > 65536:
                    return False
                content.decode("utf-8")
            except (OSError, UnicodeError, ValueError):
                return False
        return True


class EngineState:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.secrets = SecretFiles(config)
        self.lock = threading.Lock()
        self.draining = False
        self.inflight = 0
        self.requests = {status: 0 for status in (200, 400, 408, 411, 413, 415, 500, 503)}
        self.approvals = {outcome: 0 for outcome in ("approved", "declined", "failed")}
        self.buckets = {boundary: 0 for boundary in BUCKETS}
        self.duration_sum = 0.0
        self.duration_count = 0
        self.random = random.Random()

    def ready(self) -> bool:
        with self.lock:
            draining = self.draining
        return not draining and self.secrets.ready()

    def admit(self) -> bool:
        if not self.secrets.ready():
            return False
        with self.lock:
            if self.draining:
                return False
            self.inflight += 1
            return True

    def begin_draining(self) -> bool:
        with self.lock:
            if self.draining:
                return False
            self.draining = True
            return True

    def observe(self, status: int, duration: float, outcome: str | None, admitted: bool) -> None:
        with self.lock:
            self.requests[status] = self.requests.get(status, 0) + 1
            self.duration_count += 1
            self.duration_sum += duration
            for boundary in BUCKETS:
                if duration <= boundary:
                    self.buckets[boundary] += 1
            if outcome is not None:
                self.approvals[outcome] += 1
            if admitted:
                self.inflight -= 1

    def metrics(self) -> str:
        # Read secret state outside the metrics lock to avoid blocking request
        # completions on a filesystem operation.
        secret_ready = self.secrets.ready()
        version = self.config.version
        with self.lock:
            lines = [
                "# HELP transaction_requests_total Authorization HTTP responses by version and status.",
                "# TYPE transaction_requests_total counter",
            ]
            for status, count in sorted(self.requests.items()):
                lines.append(f'transaction_requests_total{{version="{version}",status="{status}"}} {count}')
            lines.extend([
                "# HELP transaction_request_duration_seconds Authorization request duration including body reading and simulated processing.",
                "# TYPE transaction_request_duration_seconds histogram",
            ])
            for boundary in BUCKETS:
                lines.append(
                    f'transaction_request_duration_seconds_bucket{{version="{version}",le="{boundary:g}"}} {self.buckets[boundary]}'
                )
            lines.extend([
                f'transaction_request_duration_seconds_bucket{{version="{version}",le="+Inf"}} {self.duration_count}',
                f'transaction_request_duration_seconds_sum{{version="{version}"}} {self.duration_sum:.9g}',
                f'transaction_request_duration_seconds_count{{version="{version}"}} {self.duration_count}',
                "# HELP transaction_inflight_requests Admitted authorization requests still processing.",
                "# TYPE transaction_inflight_requests gauge",
                f'transaction_inflight_requests{{version="{version}"}} {self.inflight}',
                "# HELP transaction_approvals_total Simulated authorization outcomes; invalid client requests are excluded.",
                "# TYPE transaction_approvals_total counter",
            ])
            for outcome, count in self.approvals.items():
                lines.append(f'transaction_approvals_total{{version="{version}",outcome="{outcome}"}} {count}')
            lines.extend([
                "# HELP transaction_ready Whether mounted secret files are readable and admissions are open; no real dependency probe.",
                "# TYPE transaction_ready gauge",
                f'transaction_ready{{version="{version}"}} {int(secret_ready and not self.draining)}',
                "# HELP transaction_draining Whether shutdown has stopped new authorization admissions.",
                "# TYPE transaction_draining gauge",
                f'transaction_draining{{version="{version}"}} {int(self.draining)}',
            ])
        return "\n".join(lines) + "\n"


class RequestError(Exception):
    def __init__(self, status: int, code: str) -> None:
        self.status = status
        self.code = code


class EngineServer(ThreadingHTTPServer):
    # Joining non-daemon request threads during server_close is the drain.
    daemon_threads = False
    block_on_close = True
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], config: Config) -> None:
        self.state = EngineState(config)
        super().__init__(address, Handler)

    def handle_error(self, request: socket.socket, client_address: object) -> None:
        # Exceptions and request addresses can contain user-controlled values.
        emit("connection_error", version=self.state.config.version)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FlexPayMock"
    sys_version = ""

    @property
    def state(self) -> EngineState:
        return self.server.state  # type: ignore[attr-defined]

    def setup(self) -> None:
        self.request.settimeout(self.server.state.config.request_timeout_seconds)  # type: ignore[attr-defined]
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        # Default access logs include request paths and arbitrary query data.
        pass

    def _send(self, status: int, data: bytes, content_type: str) -> None:
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Transaction-Version", self.state.config.version)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, body: dict[str, object]) -> None:
        self._send(status, json.dumps(body, separators=(",", ":")).encode(), "application/json")

    def _error(self, status: int, code: str) -> None:
        self._json(status, {"error": code, "version": self.state.config.version, "simulated": True})

    def do_GET(self) -> None:
        if self.path == "/health":
            ready = self.state.ready()
            self._json(200 if ready else 503, {
                "ready": ready,
                "version": self.state.config.version,
                "simulated": True,
            })
        elif self.path == "/version":
            self._json(200, {"version": self.state.config.version, "simulated": True})
        elif self.path == "/metrics":
            self._send(200, self.state.metrics().encode(), "text/plain; version=0.0.4; charset=utf-8")
        elif self.path == "/authorize":
            self._error(405, "method_not_allowed")
        else:
            self._error(404, "not_found")

    def _payload(self) -> dict[str, object]:
        if self.headers.get("Transfer-Encoding"):
            raise RequestError(400, "transfer_encoding_not_supported")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            raise RequestError(411, "content_length_required")
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise RequestError(400, "invalid_content_length")
        if len(lengths[0]) > 10:
            raise RequestError(413, "payload_too_large")
        length = int(lengths[0])
        if length > self.state.config.max_request_bytes:
            raise RequestError(413, "payload_too_large")
        if self.headers.get_content_type() != "application/json":
            raise RequestError(415, "application_json_required")
        if self.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise RequestError(415, "content_encoding_not_supported")
        try:
            data = self.rfile.read(length)
        except (socket.timeout, TimeoutError) as exc:
            raise RequestError(408, "body_read_timeout") from exc
        if len(data) != length:
            raise RequestError(400, "incomplete_body")
        try:
            def reject_constant(value: str) -> None:
                raise ValueError("nonfinite_json_number")

            body = json.loads(data.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise RequestError(400, "invalid_json") from exc
        if not isinstance(body, dict):
            raise RequestError(400, "json_object_required")
        amount = body.get("amount")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise RequestError(400, "invalid_amount")
        # Compare before float conversion so an enormous JSON integer is a
        # client error rather than an OverflowError in the request thread.
        if not 0 < amount <= 1_000_000_000 or not math.isfinite(amount):
            raise RequestError(400, "invalid_amount")
        currency = body.get("currency", "USD")
        merchant = body.get("merchant_id", "demo-merchant")
        request_id = body.get("request_id")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise RequestError(400, "invalid_currency")
        if not isinstance(merchant, str) or not 1 <= len(merchant) <= 128:
            raise RequestError(400, "invalid_merchant_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 128):
            raise RequestError(400, "invalid_request_id")
        keys = self.headers.get_all("Idempotency-Key", [])
        if len(keys) > 1:
            raise RequestError(400, "invalid_idempotency_key")
        if keys and (not 1 <= len(keys[0]) <= 128 or any(ord(ch) < 33 or ord(ch) > 126 for ch in keys[0])):
            raise RequestError(400, "invalid_idempotency_key")
        return body

    def do_POST(self) -> None:
        if self.path != "/authorize":
            self._error(404, "not_found")
            return
        # perf_counter stays monotonic while retaining sub-millisecond
        # resolution on Windows Python 3.12 (monotonic uses GetTickCount64).
        start = time.perf_counter()
        status = 500
        outcome = None
        admitted = False
        try:
            admitted = self.state.admit()
            if not admitted:
                raise RequestError(503, "not_ready")
            payload = self._payload()
            delay = self.state.random.uniform(self.state.config.delay_min_ms, self.state.config.delay_max_ms)
            time.sleep(delay / 1000.0)
            if self.state.random.random() < self.state.config.error_rate:
                outcome = "failed"
                raise RequestError(500, "simulated_provider_failure")
            declined = self.state.random.random() < self.state.config.decline_rate
            outcome = "declined" if declined else "approved"
            key = self.headers.get("Idempotency-Key")
            merchant = str(payload.get("merchant_id", "demo-merchant"))
            identifier = uuid.uuid5(ID_NAMESPACE, json.dumps([merchant, key])) if key else uuid.uuid4()
            status = 200
            self._json(status, {
                "transaction_id": str(identifier),
                "status": "declined" if declined else "authorized",
                "version": self.state.config.version,
                "amount": payload["amount"],
                "currency": payload.get("currency", "USD"),
                "simulated": True,
                "durable_state": False,
                "idempotency_guaranteed": False,
            })
        except RequestError as exc:
            status = exc.status
            if status >= 500:
                outcome = "failed"
            try:
                self._error(status, exc.code)
            except OSError:
                pass
        except OSError:
            # A disconnected client must not leak its headers/body into logs.
            pass
        finally:
            duration = time.perf_counter() - start
            self.state.observe(status, duration, outcome, admitted)
            emit("authorization", version=self.state.config.version, status=status, duration_ms=round(duration * 1000, 3))


def main() -> int:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    try:
        config = Config.from_env()
    except ValueError as exc:
        emit("configuration_error", reason=str(exc))
        return 2
    LOGGER.setLevel(config.log_level)
    try:
        server = EngineServer((config.host, config.port), config)
    except OSError:
        emit("startup_error", reason="cannot_bind_listener", version=config.version)
        return 2

    def terminate(signum: int, frame: object) -> None:
        if server.state.begin_draining():
            emit("draining", version=config.version, signal=signum)
            # shutdown must run outside the serve_forever thread.
            threading.Thread(target=server.shutdown, name="shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    emit("started", version=config.version, port=config.port, ready=server.state.ready(), simulated=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.state.begin_draining()
        server.server_close()
        emit("drained", version=config.version, inflight=server.state.inflight)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
