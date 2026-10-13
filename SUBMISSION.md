# Submission fields

Repository: **https://github.com/petergao87/flexpay-canary**. It must remain public for the evaluator. A login-required/private repository and localhost links cannot be reviewed.

| Form field | Value |
|---|---|
| Deliverable URL | https://github.com/petergao87/flexpay-canary |
| GitHub Repository URL (optional) | https://github.com/petergao87/flexpay-canary |
| Notes (optional) | Use the text below |

## Suggested notes

Implemented a self-contained Docker Compose canary for a mock payment transaction engine, with v1.9/v2.0 running concurrently, HAProxy runtime weighted routing, one-command rollback, Prometheus/Grafana version comparison, optional automated rollback, mounted secrets, and a Jenkins approval-gated staging pipeline. README contains setup, phased rollout and failure drills; DESIGN_DECISIONS.md explains payment retry risks, thresholds, rollback capacity and production trade-offs. See evidence/VALIDATION.md for checks actually performed. This is a local deployment demonstration; real acquirer calls, durable payment state, multi-host HA and production deployment are deliberately outside scope.

## Published repository

The completed solution is already published at [petergao87/flexpay-canary](https://github.com/petergao87/flexpay-canary). Use that address for both URL fields in the submission form.

To obtain a local copy:

```bash
git clone https://github.com/petergao87/flexpay-canary.git
cd flexpay-canary
```

Do not publish `.secrets`, `.env`, local cache/data directories or any unrelated existing project. There is no need to expose the payment mock or Grafana on the public Internet for this repository-based submission.
