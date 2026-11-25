# Deferred: containerize the crawler (option D)

Decided 2026-11-25 in `/speckit-clarify` (spec 004, Q1). Spec 004 ships option B: n8n runs in Docker and calls a local run trigger, and the crawler runs on the host. This file records the follow-up toward a Kubernetes deployment, where XERJ also runs as a containerized service.

## Goal

Run the crawler as its own container image. Docker Compose runs it locally next to n8n, Langfuse, XERJ and Postgres. Later it runs as a Kubernetes Job.

## Already in place from spec 004 (keep it this way)

- The run trigger contract (FR-001a) is independent of where the crawler runs: *start* returns a run id or a refusal reason, and *status* returns the outcome.
- The standing budget approval lives in the pipeline database, not on local disk (FR-004).
- The run artifact location is configurable (FR-005a).
- Secrets come from env vars only (FR-016), which map onto Kubernetes Secrets.

## To do

- [ ] Write a crawler image (Python 3.13, `crawler` + `pipeline` extras, `uv --locked`), with a non-root user and no secrets baked in.
- [ ] Add a Compose service for the crawler, plus XERJ, Postgres and Langfuse services. Set `XERJ_URL`/`PIPELINE_DSN` to service names.
- [ ] Choose an artifact store for containers (a named volume locally, object storage or a PVC on Kubernetes).
- [ ] Write a Kubernetes launcher behind the same trigger contract. *start* creates a Job from the crawler image, and *status* reads the Job status.
  - FR-006 maximum run time → `activeDeadlineSeconds`
  - FR-003 one run at a time → refuse if an active Job exists (or `concurrencyPolicy: Forbid` if the schedule moves to a CronJob)
  - Exit codes → Job outcome, using the same mapping as FR-005
- [ ] Write Kubernetes manifests or a Helm chart for XERJ, Langfuse and n8n, with secrets through External Secrets or Kubernetes Secrets.
- [ ] Repeat the spec 004 SC-001..SC-005 checks in the containerized setup.

## Known risks

- The repo sits in iCloud Drive, and iCloud folders and Docker bind mounts are unreliable together. Use named volumes rather than bind-mounting the repo.
- The local Postgres is pgserver on the host. In Compose it becomes a real Postgres service, so data migration is needed.
- Hosted deployment is a non-goal of spec 004, so this needs its own spec (`/speckit-specify`) before any work starts.
