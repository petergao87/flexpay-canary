# Submission fields

Repository: **https://github.com/petergao87/flexpay-canary**. It must remain public for the evaluator. A login-required/private repository and localhost links cannot be reviewed.

| Form field | Value |
|---|---|
| Deliverable URL | https://github.com/petergao87/flexpay-canary |
| GitHub Repository URL (optional) | https://github.com/petergao87/flexpay-canary |
| Notes (optional) | Use the text below |

## Suggested notes

Implemented a self-contained Docker Compose canary for a mock payment transaction engine, with v1.9/v2.0 running concurrently, HAProxy runtime weighted routing, one-command rollback, Prometheus/Grafana version comparison, optional automated rollback, mounted secrets, and a Jenkins approval-gated staging pipeline. README contains setup, phased rollout and failure drills; DESIGN_DECISIONS.md explains payment retry risks, thresholds, rollback capacity and production trade-offs. See evidence/VALIDATION.md for checks actually performed. This is a local deployment demonstration; real acquirer calls, durable payment state, multi-host HA and production deployment are deliberately outside scope.

## Publishing from an authenticated GitHub CLI

The project is ready for a dedicated repository. These commands publish only this project, not the surrounding workspace. Replace the account if an organization is intended, and obtain the owner's approval before public release.

```bash
gh auth login
gh repo create YOUR-ACCOUNT/flexpay-canary --public --source . --remote origin --push
```

Or create an empty **public** repository in GitHub, copy its verified URL and then:

```bash
git remote add origin https://github.com/YOUR-ACCOUNT/flexpay-canary.git
git push -u origin main
```

Do not publish `.secrets`, `.env`, local cache/data directories or any unrelated existing project. There is no need to expose the payment mock or Grafana on the public Internet for this repository-based submission.
