# Research: Crawler Orchestration and Tracing

Spec: [spec.md](spec.md). Every unknown in the plan's Technical Context is resolved here.

## R1. How n8n reaches the crawler

- **Decision:** n8n runs in Docker Desktop and calls the run trigger at `http://host.docker.internal:8765`. The trigger is a stdlib `http.server` process on the Mac, bound to `127.0.0.1`.
- **Rationale:** this is clarification Q1 (option B). On Docker Desktop for Mac, `host.docker.internal` forwards to the host's loopback, so the trigger never listens on a LAN interface (FR-001b). A stdlib server adds no dependency (constitution IV).
- **Alternatives:** n8n's Execute Command node with native n8n (rejected in Q1); FastAPI/uvicorn (a dependency for 2 endpoints); binding to `0.0.0.0` (exposes a money-spending endpoint to the LAN).
- **Verify in the quickstart:** from the n8n container, `curl http://host.docker.internal:8765/healthz` reaches a server bound to 127.0.0.1. If a Docker Desktop version doesn't forward to loopback, the fallback is binding to the Docker Desktop host address. The token check (FR-001b) stays mandatory either way.

## R2. The run id the trigger returns at start

- **Decision:** the trigger creates the run id (with the crawler's existing `new_run_id` format) and passes it as a new `crawler run --run-id` option. The crawler validates the format and refuses duplicates.
- **Rationale:** *start* must return the run id right away (US1 scenario 2). Parsing it from the child's stderr is fragile.
- **Alternatives:** parsing stderr; returning a trigger-side job id mapped to the run id later (a second id to keep track of).

## R3. One run at a time (FR-003)

- **Decision:** `crawler run` takes a session-level Postgres advisory lock, `pg_try_advisory_lock(hashtext('crawler.run'))`, before it spends anything. If the lock is taken, it exits with the new code **4 (busy)**. The trigger maps exit 4 to `409 run_in_progress`, and n8n records it as *skipped* with no notification.
- **Rationale:** the lock covers runs started by hand as well as by the trigger. Postgres releases it if the process dies, so a crash can't leave a stale lock (constitution 8, stateless services). The trigger keeps no state of its own about it.
- **Alternatives:** checking `crawler_runs.status = 'running'` (a crashed run leaves that row stuck); a lock file (stale after a crash, and doesn't carry over to Kubernetes).

## R4. Standing budget approval (FR-004)

- **Decision:**
  - New table `crawler_budget_approvals`.
  - `python -m crawler approve [--config FILE]` shows the same budget description as `run` and asks `Approve this standing budget for 30 days? [y/N]` on a TTY. On `y`, it inserts the approval and revokes any earlier active one.
  - `crawler run --approval standing` skips the prompt. It proceeds only if an active approval exists that hasn't expired, whose **pricing fingerprint** equals the run's, and whose limits are at least the run's limits.
  - The pricing fingerprint is the SHA-256 of the canonical JSON of `agent_model` and `classifier_model` (names and prices).
  - On success, the run records `approved_by`/`approved_at` from the approval, plus `approval_id`.
  - On failure, it exits with the new code **3 (not approved)** before connecting to any paid API.
- **Rationale:** this is clarifications Q1 and Q4. The limit comparison is "within the cap", so a smaller run is allowed. A change to models or prices doesn't match the fingerprint, so it cancels the approval. Storing approvals in the database keeps them working when runs later become Kubernetes Jobs ([todo.md](todo.md)).
- **Alternatives:** an env-var cap (no record of who approved it); an approval file (stays on the Mac and doesn't carry over to a container).
- **Exit code 3:** interactive refusals also move from 2 to 3, so "not approved" has a single code. This changes spec 003's CLI contract, recorded in [contracts/cli.md](contracts/cli.md).

## R5. Maximum run time (FR-006)

- **Decision:**
  - The trigger enforces a wall-clock deadline: `CRAWLER_MAX_RUNTIME_S`, default **10800 s (3 h)**.
  - At the deadline, it sends SIGTERM to the child's process group, waits 30 s, then sends SIGKILL.
  - The crawler handles SIGTERM like an unexpected error: the run is marked `failed`, with `summary.json` `"terminated": true`.
  - The trigger writes `outcome: "timed_out"` to `trigger.json` (R7).
  - Separately, n8n stops polling at deadline + 15 min. If the trigger stays silent that long, n8n records `timed_out` itself.
- **Rationale:** the crawler's own limits (queries, cost, iterations) don't cover a hung HTTP call or a slow host. A deadline enforced from outside the process is what Kubernetes `activeDeadlineSeconds` does later. 3 h is a placeholder, sized from 100 iterations at a 5 s host interval with sampling. It gets tuned after the first live run (T074 in spec 003).
- **Alternatives:** a fourth crawler stop limit (`time`), which changes crawler budget logic (a non-goal) and the `crawler_runs` CHECK constraint.

## R6. Langfuse ingestion

- **Decision:**
  - Self-hosted Langfuse v3, run from its official Docker Compose file pinned to a release tag.
  - The crawler's existing OpenTelemetry spans are exported over OTLP/HTTP:
    - `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel`
    - `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64(pk:sk)>`
  - Add `opentelemetry-exporter-otlp-proto-http` to the `crawler` extra.
  - Use a **BatchSpanProcessor** with `export_timeout_millis=5000` and a bounded queue (2048), not the current SimpleSpanProcessor.
- **Rationale:** this reuses `crawler/tracing.py` instead of adding the Langfuse SDK, so there's a single tracing path. The batch processor exports off the hot path, so an unreachable Langfuse costs at most one bounded flush at shutdown (SC-004). Today the OTLP path is silently skipped when the package is missing, which violates FR-012. The exporter becomes a real dependency, and a missing endpoint is reported.
- **Alternatives:** the Langfuse Python SDK (a second tracing path, and it would duplicate spans); Langfuse v2 (end of life).
- **Verify during implementation:** check the attribute mapping in R8 against Langfuse's OpenTelemetry docs for the pinned version.

## R7. Run status without trigger state

- **Decision:** the trigger writes `trigger.json` into the run directory (pid, started_at, deadline_at, exit_code, outcome). `GET /runs/{id}` combines `trigger.json`, `summary.json` and the `crawler_runs` row. The trigger holds nothing in memory that it needs to answer a status request.
- **Rationale:** if the trigger restarts, status still works (constitution 8). In Kubernetes, the Job status replaces `trigger.json` behind the same response shape.

## R8. Trace content (FR-008 to FR-010b)

- **Decision:**
  - The `agent.step` span and the classifier call get `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` and `gen_ai.usage.cost` (USD). They also get `langfuse.observation.type=generation`, so Langfuse renders them as LLM calls.
  - The root `crawler.run` span gets `langfuse.trace.name=crawler.run` and `langfuse.session.id=<run_id>`, so the trace can be found by run id (SC-001).
  - In `full` content mode (the default, `CRAWLER_TRACE_CONTENT=full|metadata`), the `langfuse.observation.input`/`output` attributes are set with the prompt and response text. That text is redacted at the source and cut to 64 KB per attribute.
  - The JSONL exporter and the console log processor drop those two keys (`CONTENT_KEYS`), so the text only goes to OTLP (FR-010b).
  - In `metadata` mode, the content attributes are never set. Prompt and response hashes are set in both modes.
- **Rationale:** this is clarification Q2. Redacting at the source covers every exporter at once. The 64 KB cap keeps large page samples from filling Langfuse.

## R9. Structured console logs (FR-013, FR-014)

- **Decision:**
  - A `LogSpanProcessor` in `crawler/logs.py` writes one stdlib `logging` record per finished span to stderr: timestamp, level, run_id, span name (step), tool, outcome and duration in ms.
  - The level is ERROR when the span status is ERROR or an `error` attribute is present.
  - The output is text by default; `CRAWLER_LOG_FORMAT=json` gives one JSON object per line.
  - The existing one-line-per-task progress output goes through the same logger.
- **Rationale:** deriving log lines from spans makes "the same steps appear in the trace and on the console" (SC-002) true by construction, with no second set of call sites.
- **Alternatives:** structlog (a dependency); separate log calls next to each span (they would drift).

## R10. Notification channel (FR-015a)

- **Decision:** email, through n8n's built-in Send Email node and an SMTP credential kept in n8n's credential store. The workflow has a single Notify node, so switching to Slack or Telegram means replacing one node.
- **Rationale:** email needs no new account, and SMTP credentials stay in n8n's encrypted store, out of the exported workflow (FR-016/017).

## R11. Repo layout of the non-Python pieces

- **Decision:**
  - `orchestration/n8n/docker-compose.yaml`: n8n, pinned tag, SQLite in a named volume, `N8N_ENCRYPTION_KEY` from env.
  - `orchestration/n8n/crawler-run.workflow.json`: exported without credentials.
  - `orchestration/langfuse/docker-compose.yaml`: Langfuse's official compose, pinned, with secrets from a gitignored `.env`.
  - Named volumes only, never bind mounts into the iCloud repo.
- **Rationale:** `orchestration/` already holds the Dagster compose, so this keeps the "how runs are scheduled" knowledge in one place. Named volumes avoid the iCloud bind-mount problem.

## R12. Schedule default

- **Decision:** cron `0 2 * * *` (02:00 local time). The workflow is exported **inactive**, and the maintainer activates it after the first manual run passes the quickstart.
- **Rationale:** a workflow that can spend money shouldn't start a schedule just because it was imported.
