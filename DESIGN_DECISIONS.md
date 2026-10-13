# Design decisions and operational boundaries

## Scope and success criterion

This two-hour exercise implements and demonstrates controlled **deployment** of a mock transaction engine. During normal weight changes, both instances stay alive, no proxy reload is needed, and existing requests complete on their original instance. This is not a claim of uninterrupted service during a host failure, an abrupt crash, a network partition or a full-stack restart.

The payment risk changes the design: a proxy timeout does not tell us whether an acquirer already authorized a charge. Returning an error is safer than transparently replaying an authorization whose result is unknown. Real zero-loss operation needs durable transaction state, cross-version idempotency, reconciliation and provider-specific recovery. The mock explicitly does not claim these guarantees.

## Tool choices

| Choice | Why it fits this delivery | Cost or limit |
|---|---|---|
| Docker Compose | Evaluator can run the entire topology locally; clear service/version configuration | Single host, no production scheduling or automatic replica replacement |
| HAProxy | Active HTTP readiness checks, weighted round-robin, a runtime API, warm backup and built-in proxy metrics | Privileged control socket must be isolated; two weight changes are sequential |
| Python standard library | Small auditable mock; no runtime dependency installation or external payment calls | Custom metrics exporter and thread-based HTTP server are demonstration code |
| Prometheus + Grafana | Version comparison, distributions, target health and reproducible dashboard provisioning | Local storage and dashboard access; no configured external alert delivery |
| Jenkins | Familiar DevOps workflow, reviewed image build and explicit human promotion input | Requires a dedicated configured agent; not an installed Jenkins service |

Nginx upstream weights would also satisfy the core requirement, but changing configuration would require reload management and active health behavior is less convenient in the open-source baseline. Kubernetes with a service mesh/Flagger would add deployment capacity and analysis features, but cluster setup and policies would compete with verifying the core exercise. A cloud ALB with weighted target groups is appropriate for an AWS production architecture; it would need cloud credentials, billable infrastructure and more evidence than this self-contained exercise.

## Traffic control and restart behavior

The backend contains stable and canary with weights `100-p` and `p`. Weighted round-robin operates on HTTP request assignments, without a sticky session or a version cookie. There are no long-lived business sessions in the mock. At 100% the normal stable entry has zero weight, but a separate backup entry points at the same stable instance and becomes eligible if no primary server is available. It is a rollback path, not an extra stable replica.

The initial configuration always boots stable100/canary0. `ops traffic 5` starts the first canary stage after backend checks. This deliberate distinction gives predictable recovery: restarting a proxy after a rollback cannot restore an old risky percentage. Runtime weights are not persisted; a proxy restart needs a fresh, authorized canary rollout. This sacrifices preserving promotion across restart in favor of a conservative known state.

The operator updates canary first when increasing exposure and restores stable first during rollback. Commands are verified against live stats; a shared lock serializes local operators and the optional guard. If an intermediate update fails, the operator attempts to restore stable exposure and surfaces the error. The update is not a multi-server transaction, so a brief intermediate share can differ from the requested percentage. There is always a healthy eligible route when a normal change starts with healthy backends.

Health checks run every second, with two failures to exclude a server and two successes to return it. Docker DNS resolution allows a replaced canary container to get a new IP without restarting the stable container or proxy. Requests in the detection window can fail; clients must reconcile unknown payment results, rather than automatically retry them.

HAProxy runtime changes are documented in its [official runtime API](https://www.haproxy.com/documentation/haproxy-runtime-api/reference/set-server/). Retry control follows the [official configuration manual](https://www.haproxy.com/documentation/haproxy-configuration-manual/latest/): `retries 0` and `retry-on none` prohibit proxy request replay.

## Rollback and draining

One `ops rollback` command returns future requests to stable, with no application rebuild, deployment replacement or proxy reload. A local healthy socket normally applies the command within seconds; container startup for the operator and a busy host can add delay. It is not a contractual rollback-time guarantee. Requests already executing on canary finish there, so rollback does not erase transactions or revoke already-issued authorizations.

The operator first verifies stable is available. If stable is down it preserves the available canary route and fails visibly; zeroing canary would create an outage. The warm stable container must retain enough CPU/memory/provider/database capacity to absorb 100% of traffic. Retiring stable immediately at full promotion would invalidate this promise.

SIGTERM closes admission and waits for worker threads. The server returns readiness503 while draining, and Compose has a 15-second stop budget for sub-second mock requests. A safe replacement reduces the selected version's weight to zero before stopping it and waits for its inflight gauge to reach zero. Abrupt SIGKILL or host loss cannot be made graceful by a longer timeout.

## Monitoring philosophy and thresholds

Compare the same endpoint and measurement definition across versions. Authorization counters include successful responses and validation/server failures, but exclude `/health` and `/metrics`; their frequent polling must not dilute business traffic/error measurements. P50 measures the typical experience, P95 catches tail degradation before merchant timeouts, and P99 makes a long tail visible. Rate and observed share validate that the requested canary is actually receiving traffic.

Application metrics cannot see a connection that never reached the service. The dashboard therefore also shows HAProxy backend health and response/transport errors. A network partition can leave Prometheus-to-canary scraping healthy while HAProxy-to-canary readiness fails; the proxy metric is the important distinction. Target health, missing instrumentation and low sample size are separate failure signals, never a green zero-error measurement.

The proposed rollout limits are a canary 5xx fraction of at most 1%, no more than 2 percentage points above stable, P95 at most 400 ms, at least 100 canary requests in five minutes, and a live stable comparison before promotion. Metric failures sustained for three minutes trigger rollback; backend unavailability bypasses that sample window. At 100%, stable stops receiving new traffic, so the guard uses absolute canary limits rather than insisting on fresh stable metrics. At 0%, the guard does not demand a canary traffic sample.

The absolute 1% limit makes the +2 percentage-point relative check mostly redundant when stable is healthy. This keeps the canary gate strict; 1% does not meet the company's 99.9% SLO. A production rollout would derive limits from the existing error budget, transaction value and acquirer behavior, and tune them from historical variance. Five-minute windows and a three-minute hold reduce noise but delay business-error rollback. A severe live-payment incident warrants immediate human rollback even before the automation timer expires.

With 12 million transactions/month, average traffic is about 4.6 requests/second; a 5% canary sees about 0.23 requests/second, or 69 requests/5min at that average. Thus the 100-request gate can legitimately block promotion at 5% during quiet periods. Simply holding longer does not increase samples in a fixed five-minute window at a steady rate. Use an approved 10% stage, wait for representative higher-volume traffic, or design a statistically justified longer analysis window; never manufacture confidence from sparse traffic. The supplied 30 RPS generator accelerates the local mock exercise and is not meant for production traffic.

Authorization approval rate is a business KPI, separate from service success. A bank decline may be an expected successful orchestration response. The mock exposes outcomes, but does not provide statistically meaningful real approval behavior. Production gates should compare acquirer/market/payment-method cohorts, exclude ordinary declines from infrastructure error rates, watch duplicate/pending transactions and reconcile settlement state. This implementation deliberately avoids labels containing merchant IDs, request IDs, PANs or provider credentials.

## Failure scenarios

| Failure | Immediate behavior | Operator/automation response | Remaining risk |
|---|---|---|---|
| v2.0 fails on startup | Canary readiness fails; stable proxy startup is still allowed; default canary weight0 | `traffic 5` refuses an unavailable canary; investigate logs, keep stable serving | `up --wait` reports unhealthy even though stable API remains accessible |
| v2.0 returns20% 5xx | Readiness can stay healthy, so5xx pass to callers without retry; error counters show divergence | Stop promotion; `ops analyze` fails; Prometheus alerts and the optional guard rolls back after enough samples and3min hold | Exposed canary transactions can fail before detection; human rollback can be immediate |
| Proxy-to-v2.0 network partition | Failed checks remove canary after two checks; warm stable handles new requests, including at100% | Proxy health signal triggers availability handling; operator/guard sets canary weight0 | Inflight requests can timeout with unknown result; requires real reconciliation |
| Canary becomes unreachable at100% | Stable backup becomes eligible after primary is excluded | Roll back and retain stable | Detection delay; backup is the same stable instance, not high availability |
| Stable fails during canary | Healthy canary may receive all eligible traffic regardless of requested share | Freeze promotions; restore stable; rollback refuses to blackhole service | Small requested share does not cap traffic after stable failure; paging is essential |
| Prometheus is unavailable | Data comparison unavailable; HAProxy still routes | Freeze promotion; emit warning; handle backend health independently | No automatic metric-based rollback during telemetry outage |
| Control socket/proxy fails | Runtime changes cannot complete | Operator exits nonzero; restore proxy; restart default is stable-only | One local proxy/host remains a single point of failure |
| Secret is unreadable/empty | Mock readiness and authorization return503 | Restore secret mount/readability, then readiness recovers | Both versions share credential files; a bad common rotation can affect both |

## Secret handling and limitations

Secret values are generated locally, excluded from Git and Docker build context, and mounted only into authorized services. The app reads `*_FILE` paths rather than logging environment values. Compose file secrets are bind mounts, **not encrypted managed secrets**; source-file ownership and directory access matter. See [Docker's Compose secret guidance](https://docs.docker.com/compose/how-tos/use-secrets/).

The app rereads the mounted file on each request, demonstrating credential update without application redeployment. A source file must be overwritten in place; atomically replacing a single-file bind source can keep the old inode mounted. A temporarily empty file fails readiness safely. The real solution would use an overlapping credential rotation, independently validate provider authentication, and avoid coupling readiness to every third-party outage.

## What changes with two weeks and real production traffic

1. Deploy multiple stable/canary replicas across failure domains behind redundant managed ingress; reserve rollback capacity and rate-limit/provider-limit each cohort.
2. Use immutable image digests with signatures/SBOMs, scanned patched bases, workload identities, network policy, TLS and authenticated control operations. Current supporting images use explicit release/LTS tags, which remain mutable until digests are pinned.
3. Put payment state and idempotency in shared durable storage with uniqueness/transaction guarantees. Verify concurrent cross-version processing, provider timeout reconciliation, exactly-once settlement semantics and expand/contract schema compatibility. Never deploy irreversible schema changes with a service canary.
4. Gate on representative market/acquirer cohorts, approval-rate changes, pending-state age, duplicates and error-budget burn, with minimum sample sizes justified by statistics rather than a single arbitrary count.
5. Integrate authenticated analysis/promotion with Jenkins change approval, audited deployment identity, Alertmanager/on-call delivery and a maintained progressive-deployment controller. Test it against network partitions, dependency failure and long-tail requests.
6. Run load/soak and disaster-recovery drills, restore drills, real SIGTERM tests, vulnerability remediation and capacity tests. Keep prior binary/API/schema compatibility long enough for emergency rollback.

Corners cut for this delivery: one host/proxy and one instance per version; synthetic payment work and secret readiness; no real database or business correctness guarantee; no cloud provisioning, external notification routing, Jenkins installation or public service hosting. These limits are explicit so the evaluator can distinguish the verified deployment mechanics from the work required to safely process money.
