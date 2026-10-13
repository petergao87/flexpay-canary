#!/usr/bin/env python3
"""Local, mock-only canary operations; no Docker daemon privileges required."""

import argparse
import concurrent.futures
import csv
import io
import json
import math
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass


BACKEND = "transaction_engine"
STABLE = "v1.9"
CANARY = "v2.0"


class OperationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Server:
    name: str
    status: str
    weight: int

    @property
    def up(self):
        return self.status == "UP"


class Runtime:
    def __init__(self, path=None, timeout=5):
        self.path = path or os.environ.get("HAPROXY_SOCKET", "/run/haproxy/admin.sock")
        self.timeout = timeout

    def command(self, command):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall((command + "\n").encode("utf-8"))
                connection.shutdown(socket.SHUT_WR)
                pieces = []
                while True:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    pieces.append(chunk)
                return b"".join(pieces).decode("utf-8", errors="replace").strip()
        except OSError as exc:
            raise OperationError(f"HAProxy runtime socket unavailable: {exc}") from exc

    def servers(self):
        output = self.command("show stat")
        if not output.startswith("# "):
            raise OperationError("HAProxy returned no valid statistics header")
        rows = csv.DictReader(io.StringIO(output[2:]))
        servers = {}
        try:
            for row in rows:
                if row.get("pxname") == BACKEND and row.get("svname") in {
                    "stable", "canary", "stable_fallback"
                }:
                    name = row["svname"]
                    # HAProxy 3.2 exposes uweight: the configured weight is
                    # distinct from effective weight during health changes.
                    weight = row.get("uweight") or row["weight"]
                    servers[name] = Server(name, row["status"], int(weight))
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationError("HAProxy returned malformed server statistics") from exc
        if "stable" not in servers or "canary" not in servers:
            raise OperationError("HAProxy statistics are missing stable or canary")
        return servers

    def weight(self, server, value):
        response = self.command(f"set server {BACKEND}/{server} weight {value}")
        if response:
            raise OperationError(f"HAProxy rejected weight update for {server}: {response}")
        actual = self.servers()[server].weight
        if actual != value:
            raise OperationError(f"Weight verification failed for {server}: wanted {value}, got {actual}")

    @contextmanager
    def mutation_lock(self):
        """Serialize manual operations and the guard across ops containers."""
        try:
            import fcntl
        except ImportError as exc:
            raise OperationError("Traffic mutations require the Linux ops container (Unix flock)") from exc
        path = os.environ.get("HAPROXY_LOCK", os.path.join(os.path.dirname(self.path), "operator.lock"))
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        except OSError as exc:
            raise OperationError(f"Cannot open operator lock: {exc}") from exc
        acquired = False
        deadline = time.monotonic() + 5
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise OperationError("Another traffic operation holds the lock; waited 5 seconds")
                    time.sleep(min(0.05, remaining))
                except OSError as exc:
                    raise OperationError(f"Cannot acquire operator lock: {exc}") from exc
            yield
        finally:
            try:
                if acquired:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class Prometheus:
    def __init__(self, url=None, timeout=5):
        self.url = (url or os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")).rstrip("/")
        self.timeout = timeout

    def query(self, expression):
        url = self.url + "/api/v1/query?" + urllib.parse.urlencode({"query": expression})
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = json.load(response)
            if payload.get("status") != "success":
                raise OperationError("Prometheus query failed")
            results = payload["data"]["result"]
            if not results:
                return None
            value = float(results[0]["value"][1])
            return value if math.isfinite(value) else None
        except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
            raise OperationError(f"Prometheus unavailable or returned invalid data: {exc}") from exc

    def analyze(self, require_baseline=True):
        selectors = {
            "canary": 'up{job="transaction-engine",slot="canary"}',
            "haproxy": 'up{job="haproxy"}',
        }
        if require_baseline:
            selectors["stable"] = 'up{job="transaction-engine",slot="stable"}'
        scrape_targets = {}
        scrape_reasons = []
        for name, selector in selectors.items():
            up = self.query(f"min({selector})")
            age = self.query(f"max(time() - timestamp({selector}))")
            scrape_targets[name] = {"up": up, "age_seconds": age}
            if up != 1:
                scrape_reasons.append(f"Current {name} Prometheus scrape is not UP")
            if age is None or age < 0 or age > 30:
                scrape_reasons.append(f"Current {name} Prometheus scrape is missing or older than 30 seconds")
        if scrape_reasons:
            # Five-minute rate windows can retain good-looking samples after
            # a target disappears. Those samples cannot authorize promotion.
            return Analysis(False, False, tuple(scrape_reasons), {"scrape_targets": scrape_targets})
        observations = {}
        for version in (STABLE, CANARY):
            label = f'version="{version}"'
            observations[version] = {
                "requests_5m": self.query(f"sum(increase(transaction_requests_total{{{label}}}[5m]))"),
                "rps": self.query(f"sum(rate(transaction_requests_total{{{label}}}[5m]))"),
                "error_rps": self.query(
                    f'sum(rate(transaction_requests_total{{{label},status=~"5.."}}[5m])) or vector(0)'
                ),
                "p95_seconds": self.query(
                    "histogram_quantile(0.95, sum by (le) (rate("
                    f"transaction_request_duration_seconds_bucket{{{label}}}[5m])))"
                ),
            }
        observations["scrape_targets"] = scrape_targets
        return analyze_observations(observations, require_baseline=require_baseline)


@dataclass(frozen=True)
class Analysis:
    enough_data: bool
    bad: bool
    reasons: tuple
    observations: dict

    @property
    def healthy(self):
        return self.enough_data and not self.bad

    def as_dict(self):
        return {
            "healthy": self.healthy,
            "enough_data": self.enough_data,
            "bad": self.bad,
            "reasons": list(self.reasons),
            "observations": self.observations,
        }


def analyze_observations(observations, require_baseline=True):
    reasons = []
    canary = observations[CANARY]
    stable = observations[STABLE]
    count = canary.get("requests_5m")
    enough = count is not None and count >= 100
    if not enough:
        reasons.append("Need at least 100 canary requests within the last 5 minutes")
    for version in (STABLE, CANARY):
        observed = observations[version]
        rate = observed.get("rps")
        error_rate = observed.get("error_rps")
        if rate is None or rate <= 0 or error_rate is None:
            if version == CANARY or require_baseline:
                enough = False
                reasons.append(f"Missing active request/error baseline for {version}")
            observed["error_fraction"] = None
        else:
            observed["error_fraction"] = error_rate / rate
    if canary.get("p95_seconds") is None:
        enough = False
        reasons.append("Missing canary latency samples")
    bad = False
    if enough:
        if canary["error_fraction"] > 0.01:
            bad = True
            reasons.append("Canary 5xx error rate exceeds 1%")
        if stable["error_fraction"] is not None and canary["error_fraction"] > stable["error_fraction"] + 0.02:
            bad = True
            reasons.append("Canary 5xx error rate exceeds stable by 2 percentage points")
        if canary["p95_seconds"] > 0.4:
            bad = True
            reasons.append("Canary P95 latency exceeds 400 ms")
    return Analysis(enough, bad, tuple(reasons), observations)


def _rollback_locked(runtime):
    # These are separate runtime commands: restoring a route precedes disabling one.
    runtime.weight("stable", 100)
    if not runtime.servers()["stable"].up:
        raise OperationError("Stable is not UP; its weight is restored, but canary remains enabled to preserve service")
    runtime.weight("canary", 0)
    return runtime.servers()


def rollback(runtime):
    with runtime.mutation_lock():
        return _rollback_locked(runtime)


def traffic(runtime, percentage, approve=False, prometheus=None):
    if not 0 <= percentage <= 100:
        raise OperationError("Canary percentage must be between 0 and 100")
    if percentage == 0:
        return rollback(runtime)
    with runtime.mutation_lock():
        return _traffic_locked(runtime, percentage, approve, prometheus)


def _traffic_locked(runtime, percentage, approve, prometheus):
    current = runtime.servers()
    total = current["stable"].weight + current["canary"].weight
    current_percentage = current["canary"].weight * 100 / total if total else 0
    increasing = percentage > current_percentage
    if increasing:
        if not current["stable"].up or not current["canary"].up:
            raise OperationError("Both stable and canary must be UP before increasing canary traffic")
        if percentage > 10:
            if not approve:
                raise OperationError("Traffic above 10% requires explicit manual approval: add --approve")
            result = (prometheus or Prometheus()).analyze()
            if not result.healthy:
                raise OperationError("Promotion blocked: " + "; ".join(result.reasons))
    # Weight changes are not atomic. Increase canary first; reduce canary only
    # after increasing stable. During each transition an active route remains.
    operations = [("canary", percentage), ("stable", 100 - percentage)]
    if not increasing:
        operations.reverse()
    try:
        for server, weight in operations:
            runtime.weight(server, weight)
        return runtime.servers()
    except OperationError as exc:
        try:
            _rollback_locked(runtime)
            recovery = "Recovered to stable=100, canary=0"
        except OperationError as recovery_error:
            recovery = f"Automatic recovery failed: {recovery_error}; inspect HAProxy immediately"
        raise OperationError(f"Traffic change failed: {exc}. {recovery}") from exc


def print_state(servers):
    total = servers["stable"].weight + servers["canary"].weight
    state = {name: {"status": item.status, "weight": item.weight} for name, item in servers.items()}
    state["canary_percentage"] = round(servers["canary"].weight * 100 / total, 2) if total else None
    state["route_available"] = any(item.up and item.weight > 0 for item in servers.values())
    print(json.dumps(state, sort_keys=True), flush=True)


class FailureWindow:
    def __init__(self, hold_seconds=180):
        self.hold_seconds = hold_seconds
        self.bad_since = None

    def observe(self, analysis, now):
        if not analysis.enough_data or not analysis.bad:
            self.bad_since = None
            return False
        if self.bad_since is None:
            self.bad_since = now
        return now - self.bad_since >= self.hold_seconds


def watch(runtime, prometheus, auto_rollback, interval=15, hold_seconds=180):
    window = FailureWindow(hold_seconds)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    while not stopped.is_set():
        try:
            servers = runtime.servers()
            if servers["canary"].weight == 0:
                window.bad_since = None
                print("Canary has no assigned traffic; watcher is waiting", flush=True)
            elif not servers["canary"].up:
                print("ALERT: canary is unavailable; HAProxy will route to healthy stable", flush=True)
                if auto_rollback:
                    print_state(rollback(runtime))
                    print("Automatic rollback completed", flush=True)
            else:
                result = prometheus.analyze(require_baseline=servers["stable"].weight > 0)
                print(json.dumps(result.as_dict(), sort_keys=True), flush=True)
                sustained_failure = window.observe(result, time.monotonic())
                if not result.enough_data:
                    print("ALERT: insufficient data; promotion is blocked", flush=True)
                if sustained_failure:
                    print(f"ALERT: canary exceeded health thresholds for {hold_seconds}s", flush=True)
                    if auto_rollback:
                        print_state(rollback(runtime))
                        window.bad_since = None
                        print("Automatic rollback completed", flush=True)
        except OperationError as exc:
            window.bad_since = None
            print(f"ALERT: analysis/operations unavailable; no promotion: {exc}", flush=True)
        stopped.wait(interval)


def load(url, duration, rps, workers):
    """Generate mock payment requests only; never contact a real provider."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "proxy", "stable", "canary"}:
        raise OperationError("Load target must be a local mock host: localhost, 127.0.0.1, proxy, stable, or canary")
    counter = Counter()
    lock = threading.Lock()
    start = time.monotonic()
    run_id = uuid.uuid4().hex
    completed = 0

    def request(index):
        key = f"mock-load-{run_id}-{index}"
        body = json.dumps({"amount": 100, "currency": "USD", "merchant_id": "demo-merchant", "request_id": key}).encode()
        req = urllib.request.Request(url.rstrip("/") + "/authorize", data=body,
                                     headers={"Content-Type": "application/json", "Idempotency-Key": key})
        status = "network_error"
        version = "unknown"
        try:
            try:
                response = urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as error:
                response = error
            with response:
                status = str(response.status)
                payload = json.load(response)
                version = payload.get("version", "unknown")
                if payload.get("simulated") is not True:
                    raise OperationError("Load target did not identify itself as a mock service")
        except (OSError, ValueError, TypeError, AttributeError, OperationError) as exc:
            status = "network_error"
            version = "unknown"
            with lock:
                counter["last_error"] = str(exc)
        finally:
            with lock:
                counter[(version, status)] += 1

    # A preflight check prevents accidental traffic against a production API.
    with urllib.request.urlopen(url.rstrip("/") + "/health", timeout=5) as response:
        health = json.load(response)
    if not isinstance(health, dict) or health.get("simulated") is not True:
        raise OperationError("Refusing load: /health must declare simulated=true")
    total = max(1, int(duration * rps))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        pending = set()
        for index in range(total):
            delay = start + index / rps - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # Bound pending work: report that requested RPS could not be met,
            # rather than queueing unbounded requests after the measurement.
            pending = {future for future in pending if not future.done()}
            if len(pending) >= workers * 2:
                counter[("generator", "capacity_drop")] += 1
                continue
            pending.add(pool.submit(request, index))
    elapsed = time.monotonic() - start
    rows = []
    last_error = counter.pop("last_error", None)
    for (version, status), count in sorted(counter.items()):
        rows.append({"version": version, "status": status, "requests": count, "rps": round(count / elapsed, 2)})
        if status != "capacity_drop":
            completed += count
    print(json.dumps({"mock_only": True, "scheduled": total, "completed": completed,
                      "elapsed_seconds": round(elapsed, 2), "requested_rps": rps,
                      "actual_rps": round(completed / elapsed, 2), "results": rows,
                      "last_error": last_error}, sort_keys=True), flush=True)
    return 1 if any(row["status"] in {"network_error", "capacity_drop"} for row in rows) else 0


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be finite and greater than zero")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    traffic_parser = commands.add_parser("traffic", help="set canary percentage; >10% increases require approval and healthy samples")
    traffic_parser.add_argument("percentage", type=int)
    traffic_parser.add_argument("--approve", action="store_true", help="record that a human approved promotion beyond 10%; this is not authentication")
    commands.add_parser("rollback", help="restore stable=100 before disabling canary")
    commands.add_parser("status", help="print current HAProxy server health and weights")
    commands.add_parser("analyze", help="evaluate current Prometheus data; exit 1 unless healthy")
    watch_parser = commands.add_parser("watch", help="poll health; alert by default, optionally roll back")
    watch_parser.add_argument("--auto-rollback", action="store_true")
    load_parser = commands.add_parser("load", help="generate requests against the mock service only")
    load_parser.add_argument("--url", default=os.environ.get("TRANSACTION_URL", "http://proxy:8080"))
    load_parser.add_argument("--duration", type=positive_float, default=60)
    load_parser.add_argument("--rps", type=positive_float, default=30)
    load_parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args(argv)
    try:
        runtime = Runtime()
        if args.command == "traffic":
            print("Runtime weight changes are sequential; a healthy route remains during transition", file=sys.stderr)
            print_state(traffic(runtime, args.percentage, args.approve))
        elif args.command == "rollback":
            print_state(rollback(runtime))
        elif args.command == "status":
            print_state(runtime.servers())
        elif args.command == "analyze":
            servers = runtime.servers()
            if not servers["canary"].up or servers["canary"].weight == 0:
                raise OperationError("Canary must be UP with assigned traffic to assess promotion health")
            result = Prometheus().analyze(require_baseline=servers["stable"].weight > 0)
            print(json.dumps(result.as_dict(), sort_keys=True), flush=True)
            return 0 if result.healthy else 1
        elif args.command == "watch":
            watch(runtime, Prometheus(), args.auto_rollback)
        elif args.command == "load":
            if not 1 <= args.workers <= 256:
                raise OperationError("Workers must be between 1 and 256")
            return load(args.url, args.duration, args.rps, args.workers)
        return 0
    except (OperationError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
