# Monitoring and Jenkins staging checks

Prometheus scrapes the two engine containers and HAProxy every **15 seconds**, and evaluates recording/alerting rules at the same interval. Grafana refreshes every **5 seconds**; a dashboard refresh does not make the scrape interval shorter. The provisioned dashboard has UID `flexpay-canary`, with a fixed datasource UID `prometheus`.

- Grafana: `http://localhost:3000/d/flexpay-canary` (user `admin`; local generated password in `.secrets/grafana_admin_password`).
- Prometheus targets: `http://localhost:9090/targets`; expect `stable:8080`, `canary:8080`, and `proxy:8404` to be UP.
- Alert state: `http://localhost:9090/alerts`. These rules expose pending/firing states; this demo does not configure Alertmanager or send pages.

All application measurements below include completed `/authorize` requests only. `/health` and `/metrics` calls do not dilute payment errors or request rates. The `version` label comes from the service, while the scrape `slot` label identifies stable/canary placement. A validation `4xx` is not a platform `5xx`, and a legitimate acquirer decline is a business outcome rather than an HTTP error.

| Signal | Dashboard / direct PromQL |
| --- | --- |
| Request rate | `sum by (version) (rate(transaction_requests_total[1m]))` |
| Observed traffic share (%) | `100 * flexpay:requests:rate1m / scalar(sum(flexpay:requests:rate1m))` |
| HTTP 5xx (%) | `100 * flexpay:error_ratio:5m` |
| P50 / P95 / P99 | `flexpay:latency_p50:5m`, `flexpay:latency_p95:5m`, `flexpay:latency_p99:5m` |
| Histogram calculation | `histogram_quantile(0.95, sum by (version, le) (rate(transaction_request_duration_seconds_bucket[5m])))` |
| Completed requests / 5m | `sum by (version) (increase(transaction_requests_total[5m]))` |
| In-flight requests | `transaction_inflight_requests` |
| Scrape transport availability | `up{job=~"transaction-engine\|haproxy"}` |
| HAProxy backend readiness | `haproxy_server_status{state="UP"}` |
| Proxy client-facing errors | `sum(rate(haproxy_frontend_http_responses_total{code="5xx"}[1m]))` |
| Mock approval ratio (%) | `100 * sum by (version) (rate(transaction_approvals_total{outcome="approved"}[5m])) / sum by (version) (rate(transaction_approvals_total[5m]))` |

The dashboard shows actual traffic percentages as well as configured server weights. Weighted routing controls probability over requests; small samples will not exactly match the configured percentage. Quantiles are interpolated histogram estimates, not exact per-request percentiles. `increase()` extrapolates to window boundaries, so the displayed request count may be fractional.

## Health and rollback policy

Promotion requires both backends to be ready, the expected telemetry to be present, and at least **100 canary requests in the last 5 minutes**. The initial 5% phase needs enough overall traffic to establish that sample. At 30 RPS, a four-minute mock load generates approximately 360 canary requests at 5%; statistical variation still applies. After 100% rollout, stable remains ready at weight zero and stable traffic samples naturally decay.

| Condition | Rule duration | Operator response |
| --- | --- | --- |
| v2.0 5xx > 1% absolute, or > v1.9 + 2 percentage points | 3 continuous minutes; at least 100 canary requests / 5m | Roll back |
| v2.0 P95 > 400ms | 3 continuous minutes; at least 100 canary requests / 5m | Roll back |
| Canary scrape unavailable or HAProxy canary backend unhealthy | 15s | Freeze promotion, roll back to healthy stable |
| Stable scrape/backend unavailable | 15s | Freeze promotion and restore the rollback target |
| Successful scrape but required request/latency/in-flight metrics missing | 1 minute | Freeze promotion; repair instrumentation |
| Canary assigned traffic but fewer than 100 recent requests | 5 minutes | Collect more data; do not infer health |

Thresholds operate on a **rolling five-minute window**, followed by the stated sustained alert duration. Total detection time therefore depends on traffic volume, prior healthy traffic in the window, and scrape/evaluation timing. An empty latency series is unknown, not zero. HTTP 5xx percentage excludes failures that happen before the request reaches the app; proxy 5xx and connection error panels cover those separately. A successful metrics scrape does not guarantee `/health` readiness, which is why HAProxy backend health is monitored too.

The absolute 1% emergency limit is deliberately wider than a real 99.9% authorization reliability SLO. The stable + 2 percentage point comparison provides context; with a healthy stable baseline, the stricter absolute 1% rule will normally trigger first. The operator CLI refuses promotion immediately on a failing current window; its watcher and Prometheus alerts use the sustained three-minute rule for automatic rollback/alerting. Missing data blocks promotion. An optional `guard` service runs the CLI watcher; enable it as documented in the main README.

Approval ratio is a **synthetic demonstration KPI**. Real rollout decisions require comparable provider, market, merchant, currency, and risk cohorts, and independent settlement/duplicate-charge checks. This mock supplies none of those guarantees.

## Jenkins prerequisites

`Jenkinsfile` runs on a dedicated **Linux** agent labeled `flexpay-staging-docker`, with a local Unix Docker socket and Docker Compose v2. Use one trusted executor, protected repository branches, Pipeline/Declarative, Credentials Binding, Input Step and Timestamper plugins. The default `DOCKER_CONTEXT` is local; remote Docker endpoints are rejected. The Docker/Trivy cache and the workspace must live on the agent's data disk; for a Windows-hosted VM, place its disk on `D:` as required by Peter's local policy.

Initialize the existing stable, proxy, and monitoring baseline on that agent under Compose project `flexpay-canary-staging` using the main README. The pipeline checks for an already-running healthy stable and proxy; it intentionally does not bootstrap or rebuild a stable rollback target. Keep the workspace path persistent, because file-secret and monitoring bind mounts refer to it. If another local demo uses port 8080, use a separate staging Docker host.

Create these **Secret file** credential IDs (values are never committed or printed):

- `flexpay-staging-database-url`
- `flexpay-staging-provider-api-key`
- `flexpay-staging-grafana-password`

Set `APPROVER` to Peter's actual Jenkins user ID or a controlled approver group. The first approval authorizes lint, unit/HTTP checks, image scan, and mock load checks; the second approval allows traffic beyond 10%. Jenkins administrators can also approve Input Step requests, so enforce controller access controls. CLI `--approve` records an operator decision and is not an authentication mechanism.

The pipeline lints both Dockerfiles, validates Prometheus configuration, builds a unique `build-<BUILD_NUMBER>-<commit>` image tag, runs tests without mounted credentials, and scans an exported image with Trivy. It fails on **fixable HIGH/CRITICAL vulnerabilities**; `--ignore-unfixed` is a documented demo trade-off. Trivy gets the image archive, not the Docker socket. Test summaries, scan output, the candidate image ID and retained stable image ID are archived as Jenkins artifacts.

Before deployment, the pipeline captures the actual stable image **ID**, restores stable traffic, and updates only canary with `--no-deps --no-build --wait`. It uses the candidate image ID for deployment, so later tag changes cannot select a different artifact. Readiness must also pass at HAProxy. Each traffic stage gets four minutes of 30-RPS mock load, followed by an explicit health analysis: **5% → 10% → approval → 25% → 50% → 100%**. Failure or approval timeout after staging begins attempts a traffic-only rollback. Stable and proxy containers, stable image, secrets and Docker volumes are retained; the pipeline never runs whole-stack `down` or image pruning.

After the manual promotion approval, another four-minute sample at 10% refreshes evidence that may have expired during the approval wait. The Prometheus rule fixtures verify the sustained threshold duration, low-volume suppression, high P95, missing telemetry, unreachable targets, and valid idle counters.

This is a staging pipeline definition. It needs an actual Jenkins controller/agent to execute; a local Python or Prometheus check does not prove Jenkins execution. Production would use a trusted image registry and digest deployment, signed artifacts, external secrets, isolated credentials, representative traffic, HA infrastructure, and an independent approval/audit service.

## References

- [Prometheus alert rules](https://prometheus.io/docs/prometheus/latest/configuration/alerting_rules/) describe the sustained `for` duration and pending/firing states.
- [Prometheus histogram query functions](https://prometheus.io/docs/prometheus/latest/querying/functions/) explain `histogram_quantile()` aggregation and interpolation.
- [HAProxy's native Prometheus metric reference](https://www.haproxy.com/documentation/haproxy-configuration-tutorials/alerts-and-monitoring/prometheus/) defines exporter metric names and labels.
- [Jenkins Pipeline syntax](https://www.jenkins.io/doc/book/pipeline/syntax/) and [Credentials Binding](https://www.jenkins.io/doc/pipeline/steps/credentials-binding/) document gates and file credentials.
