# Quickstart: validate crawler orchestration and tracing

Part A runs offline and in CI. Parts B–D are manual maintainer steps: Part B starts containers, and Parts C–D spend money on real search and LLM calls (as with spec 003's T074). Contracts: [trigger-api](contracts/trigger-api.md), [cli](contracts/cli.md), [telemetry](contracts/telemetry.md), [n8n-workflow](contracts/n8n-workflow.md).

## A. Offline checks (no Docker, no keys, no network)

```bash
uv sync --locked --extra dev --extra pipeline --extra crawler
```

```bash
uv run pytest tests/crawler -q
```

Expected: all pass. This covers approval rules, trigger auth, the busy/refusal/deadline paths with a fake launcher, routing content to OTLP only, a bounded unreachable endpoint, one log line per span, and the workflow/compose file checks.

## B. Bring up Langfuse and n8n

1. Create `orchestration/langfuse/.env` from `.env.example` (gitignored), then:
   ```bash
   docker compose -f orchestration/langfuse/docker-compose.yaml up -d
   ```
   Open `http://localhost:3000`, create a project and an API key pair.
2. Export the tracing env in your shell. Use real keys; never commit them:
   - `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel`
   - `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <base64 of pk:sk>`
3. Start n8n:
   ```bash
   docker compose -f orchestration/n8n/docker-compose.yaml up -d
   ```
   Import `crawler-run.workflow.json` and create the three credentials named in the n8n-workflow contract. Leave the workflow **inactive**.
4. Set `CRAWLER_TRIGGER_TOKEN` (32 characters or more) and start the trigger:
   ```bash
   uv run python -m crawler trigger
   ```
5. Check R1. From inside the n8n container:
   ```bash
   docker compose -f orchestration/n8n/docker-compose.yaml exec n8n wget -qO- http://host.docker.internal:8765/healthz
   ```
   Expected: `{"ok": true}`.

## C. Approval paths (cheap: refusals spend nothing)

1. With no approval, trigger the workflow by hand in n8n. Expected: `403 not_approved`, one email, and no `crawler_runs` row.
2. Give an approval:
   ```bash
   uv run python -m crawler approve
   ```
   Answer `y`. Expected: an `approval_id` and an expiry 30 days out.
3. Change a price in a run config and trigger with that config. Expected: `not_approved`, with a reason that says the pricing changed, and one email.

## D. End-to-end run (spends money; SC-001 to SC-005)

1. Trigger the workflow by hand in n8n. Expected: `202` with a `run_id`; the console prints one line per step (watch the trigger's terminal).
2. Trigger it again while the first run is still going. Expected: `409`, the run is recorded as skipped, and **no** email.
3. When the run finishes:
   - **SC-001:** in Langfuse, Sessions → the run id. The trace opens within 1 minute.
   - **SC-002:** the number of lines in `results/crawler/<run_id>/trace.jsonl` = the number of spans in the Langfuse trace = the number of step lines on the console (`grep "run=<run_id>" | grep -vc "step=crawler.cli"`).
   - The `agent.step` generations show the model, tokens, cost and prompt text. `trace.jsonl` contains no `langfuse.observation.input`.
4. **SC-003 (forced failures):** each of these must show up as a failure in n8n history and send exactly one email:
   - Stop the trigger, then trigger the workflow. Expected: `trigger_unreachable`.
   - Unset a search key and restart the trigger. Expected: a failure naming the variable, never its value.
   - Stop Langfuse during a run. Expected: the run still completes, there's an error line `trace export failed: …`, and `summary.json` `trace_export.spans_failed > 0`.
   - Set `CRAWLER_MAX_RUNTIME_S=120`. Expected: `timed_out`.
5. **SC-004:** compare the wall time of a run with tracing off against a run with Langfuse stopped. Expected: at most 10% slower.
6. **SC-005:** scan the repo, `n8n export:workflow` output, the console log and a Langfuse trace export for the values of `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `CRAWLER_TRIGGER_TOKEN` and the Langfuse secret key. Expected: zero hits.
7. Repeat step 3 with `CRAWLER_TRACE_CONTENT=metadata`. Expected: the generations show no text, and `summary.json` `trace_export.mode = "metadata"`.
8. Once all of this passes, activate the workflow's schedule in n8n.

Record the results for D in the PR description. Don't fill in tables with expected values; report only what actually ran (CLAUDE.md prime directive).
