# Validation evidence

Performed on 13 October 2026, with Peter's explicit approval for local static checks, unit tests, Docker builds and integration tests. Linux containers ran in a dedicated WSL2 Docker Engine 29.6.1 with Compose 5.6.0. Image, build, volume and tool caches are on the D drive.

## Completed checks

| Check | Observed result | Evidence |
|---|---|---|
| Python service/operator unit tests | 40 passed; 3 HTTP opt-in cases skipped in this run | [unit-tests.txt](unit-tests.txt) |
| HTTP tests through the real container proxy | All 3 opt-in cases passed: concurrent authorizations, health/version, invalid payload | [http-integration.txt](http-integration.txt) |
| Compose configuration and image builds | Valid; both version containers and proxy built and started | `docker compose config --quiet`, `up --build --wait` |
| HAProxy configuration and control permissions | Valid; non-root proxy UID 99 and shared control GID 10002; operator mutation succeeded without capabilities | Runtime traffic/state artifacts below |
| Prometheus configuration and alerts | Valid; 18 recording/alert rules, 6 semantic fixture cases passed | `promtool check config`, `check rules`, `test rules` |
| Dockerfile lint | Both app and proxy passed Hadolint 2.15.1 | Actual images built after lint |
| Image vulnerability gate | No HIGH/CRITICAL findings with known fixes in the app image | [trivy-report.txt](trivy-report.txt) |
| Grafana provisioning | Dashboard `flexpay-canary`, 17 panels, provisioned=true; API health and database OK | Verified authenticated local API |
| Version coexistence | Separate stable/canary containers returned v1.9/v2.0 respectively; both healthy | `docker compose ps` and each `/version` endpoint |
| Manual approval boundary | Increase from 5% to 25% without `--approve` rejected | [approval-gate.txt](approval-gate.txt) |
| Full promotion analysis | 100% canary healthy without requiring a new stable traffic baseline | [full-rollout-analysis.json](full-rollout-analysis.json) |

Trivy 0.75.0 scanned OS/Python packages using `--severity HIGH,CRITICAL --ignore-unfixed --exit-code 1`. This is a point-in-time gate under those filters, not proof that every severity or every future vulnerability is absent. The Jenkins pipeline definition was reviewed and its individual local validation steps exercised; Jenkins itself was not installed or run end to end.

## Actual traffic distribution

Requests used distinct mock IDs and were sent only to the local mock. No production endpoints or acquirers were contacted.

| Configured canary | v1.9 responses | v2.0 responses | Total | Failures | Artifact |
|---|---:|---:|---:|---:|---|
| 5% | 4,275 | 225 | 4,500 | 0 | [traffic-05-confirmed.json](traffic-05-confirmed.json) |
| 10% | 540 | 60 | 600 | 0 | [traffic-10.json](traffic-10.json) |
| 25% | 450 | 150 | 600 | 0 | [traffic-25.json](traffic-25.json) |
| 50% | 300 | 300 | 600 | 0 | [traffic-50.json](traffic-50.json) |
| 100% | 0 | 600 | 600 | 0 | [traffic-100.json](traffic-100.json) |

These prove routing mechanics and the absence of observed failures during these requests. The 5% run lasted 150 seconds; the later per-step runs lasted 20 seconds. They are **not five-minute soak acceptance runs at every stage**. The deployment playbook and Jenkins pipeline prescribe longer observation and representative traffic for a real rollout. Histogram-derived canary P95 was approximately 240 ms during the healthy verification; values are approximate bucket estimates.

## Failure verification

At 100% canary, the canary container was paused to make it unreachable. After active checks excluded it, the retained stable backup handled 100/100 requests successfully while the primary stable weight remained zero. The container was unpaused and a manual rollback restored stable100/canary0.

- [Unreachable canary runtime state](canary-unreachable-state.json)
- [Backup traffic responses](backup-fallback-traffic.json)
- [Manual rollback state](rollback-state.json)

The pause test simulates an unresponsive backend; it is not a physical network-partition test. A selective proxy-to-canary partition is covered in the design analysis, and can still cause in-flight timeout failures before exclusion.

The startup drill supplied an invalid error-rate configuration, causing the new canary to exit on startup. `traffic 5` rejected its unavailable backend, while stable handled all 100 mock requests successfully. The Windows-to-WSL adapter was corrected to forward the non-secret Compose override variables before this drill was accepted as evidence.

- [Startup promotion refusal](startup-failure-gate.txt)
- [Stable traffic while canary fails startup](startup-failure-traffic.json)

The error drill set canary `ERROR_RATE=0.2` at 10% traffic and ran 9,000 requests over 300 seconds. Canary handled 718 requests, of which 146 returned the expected simulated500 response (20.33%). The guard observed a continuous threshold breach for 180 seconds and automatically restored stable100/canary0. There were no generator network failures. Guard was then stopped and the healthy canary configuration restored.

- [Error-drill traffic](auto-rollback-traffic.json)
- [Guard analysis and automatic rollback log](auto-rollback-log.txt)
- [Final automated rollback state](auto-rollback-state.json)
- [Actual Grafana dashboard during the fault drill](grafana-dashboard.png)

The real-container shutdown drill first observed an in-flight authorization, then sent SIGTERM with `docker stop`. The request returned200, the application logged `draining` followed by `drained` with inflight0, and the container exited0. A 1,500ms mock delay made signal delivery deterministic; normal120–220ms canary delays were restored afterward.

- [Authorization completed after SIGTERM](drain-response.json)
- [Graceful shutdown events](drain-log.txt)
- [Container exit code](drain-exitcode.txt)

The application does not provide durable transaction state, real provider authentication, cross-version idempotency or multi-host high availability; these remain explicit production limitations.

## Delivered local state

After the drills, normal canary credentials/delays/error rate were restored, the guard was stopped, and traffic returned to stable95/canary5. A fresh 330-second run completed 9,900 requests: stable9,406 and canary494, all200. The final five-minute analysis reported zero5xx for both versions, approximately239ms canary P95, adequate samples and all required scrape targetsUP. The application containers remain running so Peter can inspect the local deployment.

- [Fresh healthy traffic](final-healthy-traffic.json)
- [Fresh healthy metrics analysis](final-healthy-analysis.json)
- [Final live routing state](final-state.json)
- [Version/target runtime snapshot](runtime-snapshot.json)
- [Actual healthy Grafana dashboard](grafana-healthy.png)
