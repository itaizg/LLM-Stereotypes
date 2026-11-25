# Implementation Plan: Crawler Orchestration and Tracing

**Branch**: `004-crawler-orchestration` | **Date**: 2026-11-25 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/004-crawler-orchestration/spec.md`. Mission: [mission.md](mission.md).

## Summary

n8n, running in Docker, starts spec 003's crawler on a schedule or on demand. It does this through a small local **run trigger** (`python -m crawler trigger`), a stdlib HTTP server bound to localhost with a token check. Its start/status contract is written so a later Kubernetes Job launcher can replace it ([todo.md](todo.md)).

Unattended runs are covered by a **standing budget approval** stored in Postgres. It expires after 30 days, and a change of model or price cancels it.

The crawler's existing OpenTelemetry spans are also exported over OTLP to a self-hosted **Langfuse**:
- LLM calls get `gen_ai.*` attributes.
- Prompt and response text goes only on the export path, and can be switched off with a setting.
- A log processor driven by the same spans prints **one structured console line per step**, so the trace and the console can't drift apart.
- Every outcome other than *completed* sends exactly one email notification from n8n.

The crawler's decision logic (graph, tools, budget limits) is unchanged.

## Technical Context

**Language/Version**: Python 3.13 (uv, `uv.lock`); n8n and Langfuse are containers, not Python dependencies

**Primary Dependencies**:
- Existing `crawler` extra (LangGraph, langchain-anthropic, OpenTelemetry SDK), plus `opentelemetry-exporter-otlp-proto-http`.
- Run trigger: stdlib `http.server`, `subprocess`, `hmac`.
- Logging: stdlib `logging`.
- n8n (pinned tag) and Langfuse v3 (pinned tag, official compose), both via Docker Compose.

**Storage**:
- Postgres (pipeline database): new `crawler_budget_approvals` table; nullable `crawler_runs.approval_id`. Refused runs write no row (see [data-model.md](data-model.md)).
- Run artifacts: `results/crawler/<run_id>/` (`summary.json`, `trace.jsonl`, new `trigger.json`), with the root configurable through `CRAWLER_RESULTS_DIR`.
- n8n and Langfuse data: Docker named volumes.

**Testing**:
- pytest, GPU-free, no network: the trigger with an injected fake launcher, approval rules against loopback Postgres, the log processor and exporters with the in-memory span exporter.
- OTLP-unreachable behaviour is tested against a closed loopback port.
- Static checks on the workflow JSON (no credentials, only calls the trigger) and on the compose files (images pinned).

**Target Platform**: macOS (Apple Silicon) with Docker Desktop; the crawler runs on the host

**Project Type**: CLI and agent package (`crawler/`) plus infrastructure config (`orchestration/n8n`, `orchestration/langfuse`)

**Performance Goals**: with Langfuse unreachable, a run takes at most 10% longer than with tracing off (SC-004); a trace can be found by run id within 1 minute (SC-001)

**Constraints**:
- Self-hosted only.
- Secrets only from env vars or n8n's credential store.
- The trigger binds to 127.0.0.1 only.
- `harness/` and `pipeline/` import none of this.
- Prompt text never reaches `trace.jsonl`, the console or the repo (FR-010b).
- Maximum run time defaults to 3 h (R5).

**Scale/Scope**: one maintainer, at most one run at a time, about one run a night; each run has up to 100 agent iterations and 500 queries (spec 003 limits)

All Technical Context unknowns are resolved in [research.md](research.md) (R1–R12).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Rule | Status | How the design meets it |
|---|---|---|
| I. Typed code, ty | Pass | New modules (`trigger.py`, `approval.py`, `logs.py`) are fully typed. Request and response bodies are Pydantic models at the trust boundary. |
| II. ruff only | Pass | No new tools. The `.specify/` exclude from the CI fix (`99fde24`) is allowed by the constitution 1.5.1 PATCH amendment, which permits excluding vendored tool code. |
| III. Tests, GPU-free, no network | Pass | All tests use fakes, the in-memory exporter or loopback Postgres. Live n8n and Langfuse checks are manual quickstart steps. |
| IV. KISS/YAGNI/DRY | Pass | Stdlib server, not a framework. The trigger contract is a documented HTTP shape, not an abstract launcher class (the Kubernetes launcher is the 2nd instance, deferred). Logs come from spans (one source). |
| V. Structure, low coupling | Pass | One concern per module: the trigger starts and reports, approval decides, logs format. n8n only maps exit outcomes (constitution §2 and team §2). |
| Quality gates | Pass | Tests go in `tests/crawler`, so the existing `crawler` CI job and the 85% coverage gate cover them. |
| Agentic: HITL before spending | Pass (justified) | A person gives the standing approval in advance through a TTY prompt. It expires after 30 days and is cancelled by a price or model change (Complexity Tracking). |
| Agentic: traced steps, run_id matches | Pass | No span sites change. `run_id` is stamped on every span and is the Langfuse session id. |
| Agentic: tool count ≤ 5 | Pass | No new agent tools. The trigger isn't an agent tool. |
| Dependencies: optional extras | Pass | The OTLP exporter joins the `crawler` extra. n8n and Langfuse are containers. `harness/` and `pipeline/` don't import `crawler/` (existing guard test). |
| Secrets: env only, never printed | Pass | `CRAWLER_TRIGGER_TOKEN` and `OTEL_EXPORTER_OTLP_HEADERS` join `SECRET_ENV` for redaction. The workflow export has credential references, not values. |

**Post-design re-check (after Phase 1):** still passing. The contracts add one CLI subcommand (`approve`), one server subcommand (`trigger`), two `run` options and two exit codes, with no new agent behaviour. The only open item is the principle II note, which is unrelated to this feature's code.

## Project Structure

### Documentation (this feature)

```text
specs/004-crawler-orchestration/
├── mission.md           # Mission brief
├── spec.md              # Feature spec (clarified 2026-11-25)
├── plan.md              # This file
├── research.md          # Phase 0: R1–R12
├── data-model.md        # Phase 1: entities, tables, state transitions
├── quickstart.md        # Phase 1: validation guide (offline + manual live)
├── contracts/
│   ├── trigger-api.md   # Run trigger HTTP contract (start/status/health)
│   ├── cli.md           # Changes to spec 003's crawler CLI
│   ├── telemetry.md     # Span attributes, content modes, console line format
│   └── n8n-workflow.md  # Workflow nodes, outcome mapping, notification
├── todo.md              # Deferred: containerized crawler / Kubernetes (option D)
├── team-context.md      # team-discover output
└── checklists/requirements.md
```

### Source Code (repository root)

```text
crawler/
├── cli.py            # + `approve`, `trigger` subcommands; `run --approval standing --run-id`; exits 3/4; SIGTERM handling; CRAWLER_RESULTS_DIR
├── approval.py       # NEW: standing approval create/revoke/check, pricing fingerprint
├── trigger.py        # NEW: stdlib HTTP run trigger, launcher, deadline watcher, trigger.json
├── logs.py           # NEW: LogSpanProcessor, text/JSON formatters
├── tracing.py        # + OTLP via BatchSpanProcessor, export-failure counter, CONTENT_KEYS filtering, gen_ai/langfuse attrs helpers
├── graph.py          # + gen_ai/content attributes on agent.step (attribute calls only; no control-flow change)
└── classify.py       # + gen_ai/content attributes on the classifier call

pipeline/
├── schema.sql                          # + crawler_budget_approvals, crawler_runs.approval_id, status 'not_approved'
└── migrations/004_crawler_orchestration.sql

orchestration/
├── n8n/
│   ├── docker-compose.yaml
│   └── crawler-run.workflow.json       # exported inactive, no credentials
└── langfuse/
    ├── docker-compose.yaml             # official v3 compose, pinned
    └── .env.example                    # names only; real .env gitignored

tests/crawler/
├── test_approval.py      # loopback Postgres: expiry, fingerprint, limits, revoke
├── test_trigger.py       # fake launcher: auth, busy→409, refusal codes, deadline→timed_out, status from files
├── test_logs.py          # one line per span, error level, JSON mode, no content keys
├── test_tracing_export.py# content only on OTLP path, metadata mode, unreachable endpoint bounded + reported
├── test_cli_run_standing.py # exit 3 without approval, exit 4 when lock held, --run-id validation
└── test_orchestration_files.py # workflow JSON: no secrets, only trigger URL; compose images pinned
```

**Structure Decision**: all Python changes stay inside the existing optional `crawler/` package and its test folder, so the `crawler` extra, CI job and coverage source already cover them. Container config goes under `orchestration/`, next to the Dagster compose, because that folder already holds "how runs get scheduled". `harness/` and `pipeline/` code are untouched, apart from the schema and migration files that already hold the crawler's tables.

## Complexity Tracking

| Item | Why needed | Simpler alternative rejected because |
|---|---|---|
| Standing approval in place of a per-run TTY prompt (constitution HITL) | Unattended runs can't answer a prompt (FR-004, clarification Q4). | A per-run approval step in n8n was an option for FR-004, and the maintainer chose the standing cap instead. The design keeps a human approval, given in advance, limited to 30 days and to an exact set of models and prices. |
| `.specify/` in ruff `extend-exclude` (principle II) | Vendored Spec Kit scripts broke CI (commit `99fde24`, not part of this feature). | Fixing upstream code locally drifts from the manifest hashes. **Resolved:** constitution 1.5.1 (a PATCH amendment) allows excluding vendored tool code. |
| New exit codes 3 and 4 | The trigger and n8n need to tell "not approved" and "busy" apart from a usage error (FR-005). | Reusing exit 2 for three meanings would force parsing stderr. |
