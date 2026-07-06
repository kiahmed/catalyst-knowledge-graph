# `tools/infra` — GCP infrastructure as Terraform

Provisions everything needed for an end-to-end Robotics-module deployment:

- Data bucket (DuckDB file + exports)
- Artifact Registry repo for containers
- Service accounts (least-privilege per tool)
- Secret Manager entries
- Cloud Function (robotics-ingest)
- Cloud Run service (robotics-render)
- Cloud Scheduler cron jobs
- Pub/Sub topic for ingest→render fanout

(robotics-social Cloud Run Job retired 2026-07-05 — social posting moved
to soljet-postiz.)

## You don't run Terraform by hand

`make deploy` (from the repo root) drives all of this — see `DEPLOYMENT.md`.
It reads `.env.prod`, generates `prod.auto.tfvars`, pushes secrets, builds
artifacts, and runs `terraform apply`. `dev-utils/deploy.sh` is the engine.

`prod.auto.tfvars` is **generated** — never hand-edited. `terraform.tfvars`
+ `terraform.tfvars.example` are legacy; `.env.prod` supersedes them.

Run raw `terraform plan` / `apply` in this directory only for inspection or
debugging. For normal use:

```bash
make deploy            # build + push + apply (runs deploy-preflight first)
```

Terraform fully owns these resources — config, IAM, **and** the deployed
code. The function source object path and the image tag are set per-run
from the git SHA, so `terraform apply` redeploys code on every change.
There are no `lifecycle.ignore_changes` escape hatches and no separate
per-tool deploy scripts.

## Secrets

Terraform creates the Secret Manager *entries*, not the *values*. Set values out-of-band:

```bash
gcloud secrets versions add GEMINI_API_KEY --data-file=<(echo -n "$GEMINI_API_KEY")
```

Terraform state never sees the raw values.

## File map

| File | What |
|---|---|
| `main.tf` | Provider, backend, service enablement |
| `variables.tf` | Input vars (project_id, region, cron schedules, etc.) |
| `outputs.tf` | URLs, bucket names, SA emails after apply |
| `bucket.tf` | `robotics-data` bucket + lifecycle rules |
| `artifact_registry.tf` | `robotics` Docker repo + cleanup policy |
| `service_accounts.tf` | Per-tool SAs + role bindings |
| `secrets.tf` | Secret Manager entries (no values) |
| `cloud_functions.tf` | robotics-ingest Gen 2 function |
| `cloud_run.tf` | robotics-render + robotics-og services |
| `cloud_scheduler.tf` | Daily crons |
| `pubsub.tf` | Fan-out topic + subscription |

## First-deploy ordering — handled automatically

A Cloud Run resource can't be created before its image exists in Artifact
Registry, and the function source / secret values must exist before the
function deploys. `dev-utils/deploy.sh` handles this in one run:

1. bootstrap `terraform apply -target=...` — data bucket, AR repo, secret containers
2. push secret values to Secret Manager
3. upload the ingest function source zip; build + push the render image
4. full `terraform apply` — everything now resolves

No manual `-target` juggling needed; `make deploy` is idempotent on re-runs.

## Cost estimate

Idle (no traffic):
- Cloud Run / Functions: $0 (scale to zero)
- Cloud Storage: ~$0.02/GB/month × <1GB ≈ $0.02
- Secret Manager: ~$0.06/secret × 1 secret ≈ $0.06
- Artifact Registry: ~$0.10/GB × <2GB ≈ $0.20
- Firebase Hosting: Free tier (10GB storage, 360MB/day)

Expected total idle: **< $1/month**.

Active:
- Daily ingest: ~30s function-time × 1 run ≈ $0.00001/day
- Daily render: ~5 cards × 20s × Cloud Run = ~$0.01/day
- Egress from Firebase: included in free tier until 10GB/month out

Expected active total: **< $5/month** through Phase 1.
