---
description: "Task list for spec 004, crawler orchestration and tracing"
---

# Tasks: Crawler Orchestration and Tracing

**Input**: Design documents from `specs/004-crawler-orchestration/`: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md), [data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: included. Constitution III and the agentic constraints require a failing test with every behaviour change and ≥85% coverage. All tests are GPU-free and network-free (loopback Postgres is allowed), and live under `tests/crawler/` so the existing `crawler` CI job runs them.

**Organization**: grouped by user story (US1 P1 unattended start, US2 P2 Langfuse trace, US3 P3 console lines).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1/US2/US3 from spec.md
- Paths are relative to the repo root

---

## Phase 1: Setup

**Purpose**: dependencies and gitignore entries that the later phases need.

- [X] T001 Add `opentelemetry-exporter-otlp-proto-http>=1.30,<2` to the `crawler` extra in `pyproject.toml`, run `uv lock`, and commit `uv.lock`. Remove the `# ty: ignore[unresolved-import]` on the OTLP import in `crawler/tracing.py`, since the package is now installed
- [X] T002 [P] Add `orchestration/langfuse/.env` and `orchestration/n8n/.env` to `.gitignore`

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: the shared CLI and artifact changes that the trigger, tracing and logging all build on.

**⚠️ CRITICAL**: no user story work starts until this phase is done.

- [X] T003 [P] Write failing tests in `tests/crawler/test_crawler_cli.py`:
  - an interactive refusal exits 3 (not 2) and writes `summary.json` with `status: "not_approved"` and no `crawler_runs` row
  - a usage error still exits 2
  - `CRAWLER_RESULTS_DIR` moves the run directory
- [X] T004 [P] Write failing tests in `tests/crawler/test_tracing.py`: `CRAWLER_TRIGGER_TOKEN` and `OTEL_EXPORTER_OTLP_HEADERS` values are redacted from span attributes and status messages
- [X] T005 In `crawler/cli.py`:
  - add `EXIT_NOT_APPROVED = 3` and `EXIT_BUSY = 4`, and move the TTY refusal from `EXIT_USAGE` to `EXIT_NOT_APPROVED`
  - replace the `RESULTS` constant with a `results_root(env)` that reads `CRAWLER_RESULTS_DIR` (default `results/crawler`)
  - write a zero-count `summary.json` (`status` `not_approved` or `busy`) on refusal paths before returning (data-model.md, Run Artifacts)
- [X] T006 [P] Add `CRAWLER_TRIGGER_TOKEN` and `OTEL_EXPORTER_OTLP_HEADERS` to `SECRET_ENV` in `crawler/tracing.py`. Lower the minimum secret length for the headers value only if the tests in T004 need it
- [X] T007 Update the spec 003 CLI contract `specs/003-crawler-agent/contracts/cli.md` (analysis I2):
  - move exit 2 "budget not approved" to 3, and add exit 4 busy, linking [contracts/cli.md](contracts/cli.md)
  - change step 2's "exits 2 and writes nothing" to "exits 3, writes no database rows, and writes only `summary.json` with `status: "not_approved"`"
  - grep `tests/crawler/` for assertions that a refused run leaves no run directory, and update them in the same commit as T005

**Checkpoint**: `uv run pytest tests/crawler -q` passes; exits 2, 3 and 4 have distinct meanings.

---

## Phase 3: User Story 1 — Start a crawler run without sitting at a terminal (Priority: P1) 🎯 MVP

**Goal**: n8n (schedule or webhook) → run trigger → `crawler run --approval standing`, with one run at a time, a deadline, the outcome recorded, and one email for each run that doesn't complete.

**Independent Test**: [quickstart.md](quickstart.md) parts A, B and C, plus D.1, D.2 and D.4 (trigger unreachable, timeout). The workflow shows the run id and its outcome, and the run directory exists.

### Tests for User Story 1 ⚠️ (write first; they must fail)

- [X] T008 [P] [US1] Write `tests/crawler/test_approval.py` (loopback Postgres, `conftest.py` fixtures). Cases:
  - approve inserts an approval with a 30-day expiry
  - a second approve revokes the first, and the unique index allows only one active approval
  - `--revoke`
  - the check passes when limits are ≤ the cap with an equal pricing fingerprint
  - the check fails, with a distinct reason, for: none active, expired (with an injected `now`), a changed model price, a changed model name, and any limit above the cap
- [X] T009 [P] [US1] Write `tests/crawler/test_cli_run_standing.py`:
  - `--approval standing` with no approval exits 3 before any `Factory` paid call (the fake factory asserts it's never invoked)
  - with a valid approval, the `crawler_runs` row has `approval_id`, `approved_by` and `approved_at` from the approval
  - while another connection holds `pg_advisory_lock(hashtext('crawler.run'))`, the run exits 4 and writes no row
  - `--run-id` with a bad format, or one that already exists, exits 2
  - calling the installed SIGTERM handler while a fake model call is blocked marks the run `failed` and writes `terminated: true`
- [X] T010 [P] [US1] Write `tests/crawler/test_trigger.py` against an in-process server on an ephemeral loopback port, with an injected fake launcher (no real subprocess). Cases:
  - `/healthz` needs no auth
  - a missing or wrong token gets 401
  - the server refuses to start when the token is shorter than 32 characters
  - `POST /runs` gives 202, 403, 409 or 422 for fake exit codes 3, 4 and 2, and for "still running"
  - unknown body fields get 422
  - `GET /runs/{id}` combines `trigger.json` and `summary.json` after a trigger "restart" (new server instance)
  - when the deadline passes, the fake process gets SIGTERM, then SIGKILL, and `outcome: "timed_out"`
  - unknown run ids get 404
  - the exit code → outcome mapping matches data-model.md
- [X] T011 [P] [US1] Write `tests/crawler/test_orchestration_files.py`:
  - `orchestration/n8n/crawler-run.workflow.json` parses, has `active: false`, and has no credential values or `Bearer <value>` strings
  - every HTTP Request node URL starts with `http://host.docker.internal:8765/`
  - every outcome except `completed` and `skipped` reaches the Notify node
  - both compose files pin image tags (no `latest`, no untagged images) and use named volumes only

### Implementation for User Story 1

- [X] T012 [US1] Write migration `pipeline/migrations/004_crawler_orchestration.sql` and mirror it in `pipeline/schema.sql`:
  - table `crawler_budget_approvals` (fields per data-model.md)
  - the unique partial index `((true)) WHERE revoked_at IS NULL`
  - `ALTER TABLE crawler_runs ADD COLUMN IF NOT EXISTS approval_id text REFERENCES crawler_budget_approvals`
  - make sure `pipeline.store.apply_schema` picks the migration up
- [X] T013 [US1] Write `crawler/approval.py`:
  - `pricing_fingerprint(config) -> str` (SHA-256 of canonical JSON of `agent_model` + `classifier_model`)
  - `create(conn, config, approved_by, now) -> Approval`, which revokes earlier approvals in the same transaction
  - `revoke(conn, now) -> bool`
  - `check(conn, config, now) -> Approval | Refusal`, where `Refusal.reason` is an enum: `none_active | expired | pricing_changed | over_cap`
  - typed Pydantic models; USD units in field metadata
- [X] T014 [US1] Add `crawler approve [--config FILE] [--revoke]` to `crawler/cli.py`: the TTY prompt `Approve this standing budget for 30 days? [y/N]`; no TTY or any other answer exits 3; prints `approval_id` and `expires_at` (contracts/cli.md)
- [X] T015 [US1] Extend `crawler run` in `crawler/cli.py`:
  - `--approval tty|standing` (standing calls `approval.check` before `connect` to any paid service)
  - `--run-id` with format and uniqueness checks
  - `pg_try_advisory_lock(hashtext('crawler.run'))` on the main connection before the `crawler_runs` insert, with exit 4 when busy
  - record `approval_id`
  - a SIGTERM handler that raises inside the run so the existing failure path writes `failed` and `terminated: true`
- [X] T016 [US1] Write `crawler/trigger.py`:
  - a stdlib `ThreadingHTTPServer` bound to `127.0.0.1`
  - bearer auth with `hmac.compare_digest`; refuse to start without a token of 32+ characters
  - Pydantic request and response models
  - `Launcher` as a typed Protocol (`start(run_id, config) -> Process`), with a `SubprocessLauncher` that runs `sys.executable -m crawler run --approval standing --run-id …` in a new process group
  - a 10 s early-exit wait
  - a deadline watcher thread (`CRAWLER_MAX_RUNTIME_S`, default 10800; SIGTERM, then SIGKILL after 30 s)
  - `trigger.json` written at launch and at exit
  - the single `EXIT_TO_OUTCOME` mapping
  - `GET /runs/{id}` read from files plus the `crawler_runs` row
- [X] T017 [US1] Wire `python -m crawler trigger [--port 8765]` into `crawler/cli.py` (`crawler/__main__.py` only calls `cli.main`)
- [X] T018 [P] [US1] Write `orchestration/n8n/docker-compose.yaml`: n8n with a pinned tag, the `n8n_data` named volume, `N8N_ENCRYPTION_KEY` and timezone from `orchestration/n8n/.env`, port 5678 bound to `127.0.0.1`, and an `extra_hosts` entry `host.docker.internal:host-gateway`
- [X] T019 [US1] Build and export `orchestration/n8n/crawler-run.workflow.json` per [contracts/n8n-workflow.md](contracts/n8n-workflow.md):
  - Schedule `0 2 * * *` and the Webhook trigger
  - Start run, route the start response, Respond to Webhook, the poll loop with deadline + 15 min, route the outcome, and Notify (Send Email)
  - credentials referenced only by name; exported inactive
- [X] T020 [US1] Add an "Orchestration" section to `README.md` or `orchestration/n8n/README.md`, linking the quickstart. It covers starting the trigger, importing the workflow, naming the credentials and activating the schedule

**Checkpoint**: T008–T011 pass. Quickstart B and C work by hand. US1 is a usable MVP even without Langfuse.

---

## Phase 4: User Story 2 — See the whole run as one trace (Priority: P2)

**Goal**: one Langfuse trace per run, found by run id. It shows LLM generations (model, tokens, cost, latency, text in `full` mode) and tool spans with errors marked. Export failures never fail the run.

**Independent Test**: quickstart D.3 (SC-001, SC-002 trace part), D.4 (Langfuse stopped), D.5 (SC-004) and D.7 (metadata mode).

### Tests for User Story 2 ⚠️

- [X] T021 [P] [US2] Write `tests/crawler/test_tracing_export.py` using the in-memory span exporter in place of OTLP. Cases:
  - in `full` mode, the `agent.step` and classifier spans carry `gen_ai.*`, `langfuse.observation.type=generation`, prompt/response hashes and content keys
  - content longer than 64 KB is cut and marked `…[truncated]`
  - secrets inside the content are redacted
  - `trace.jsonl` never holds `CONTENT_KEYS` in either mode
  - in `metadata` mode, no span holds `CONTENT_KEYS`
  - the root span has `langfuse.session.id == run_id`
  - (analysis C1) a `tool.<name>` span whose result is a `ToolError` counting as a failure (not in spec 003's `NOT_FAILURES`) ends with status ERROR and `langfuse.observation.level=ERROR`, with the error code as the status message. A `NOT_FAILURES` outcome stays OK. The same holds for an `agent.step` whose model call fails or returns a refusal
- [X] T022 [P] [US2] Add these to `tests/crawler/test_tracing_export.py`:
  - with `OTEL_EXPORTER_OTLP_ENDPOINT` pointing at a closed loopback port, a fake run of N spans finishes within a bounded extra time (shutdown flush ≤ 5 s), and `summary.json` `trace_export` reports `spans_failed > 0` and an error class
  - an endpoint set but the exporter unimportable (monkeypatched) gives `trace_export.error` and does not raise
  - the run's exit code is unchanged in both cases

### Implementation for User Story 2

- [X] T023 [US2] In `crawler/tracing.py`:
  - replace the silent OTLP skip with a `CountingExporter` wrapping `OTLPSpanExporter`, inside a `BatchSpanProcessor(export_timeout_millis=5000, max_queue_size=2048)`
  - add `Tracing.export_status() -> TraceExport`
  - add `CONTENT_KEYS`, and make `JsonlExporter` drop those keys
  - add `Tracing.content_mode`, read from `CRAWLER_TRACE_CONTENT` (`full` default, or `metadata`)
  - add helpers `generation(span, model, input_tokens, output_tokens, cost_usd, prompt, response)` (redact, hash, cut to 64 KB; content only in `full` mode) and `mark_root(span, run_id)`
  - (analysis C1) add `mark_failed(span, code)`, which sets status ERROR with the redacted code as the message, and `langfuse.observation.level=ERROR`
- [X] T024 [US2] In `crawler/graph.py`:
  - call `tracing.mark_root` on the `crawler.run` span, and `tracing.generation(...)` inside the existing `agent.step` span (serialize `graph["messages"]` and `reply` as text)
  - (analysis C1) call `tracing.mark_failed(span, code)` in `_call` when the result is a `ToolError` not in `NOT_FAILURES`, and in `agent_node` on a model exception or a refusal
  - attribute and status calls only; no control-flow change
- [X] T025 [US2] In `crawler/classify.py`, wrap the classifier model call in a span (`llm.classify`, if one doesn't exist yet) and call `tracing.generation(...)` with the classifier model, usage and text
- [X] T026 [US2] In `crawler/cli.py`, add the `trace_export` block (mode, endpoint_set, counts, error) to `summary.json`, and print one ERROR line `trace export failed: <n> spans, <error>` when `spans_failed > 0` or `error` is set (FR-012)
- [X] T027 [P] [US2] Write `orchestration/langfuse/docker-compose.yaml` (Langfuse v3 official compose pinned to one release tag, named volumes only, web on `127.0.0.1:3000`) and `orchestration/langfuse/.env.example` (variable names only)
- [X] T028 [US2] Check the attribute names in T023 against Langfuse's OpenTelemetry docs for the pinned version (research R6). Fix [contracts/telemetry.md](contracts/telemetry.md) and the code if they differ

**Checkpoint**: T021–T022 pass. Quickstart D.3 shows a complete trace by session id.

---

## Phase 5: User Story 3 — Read the run live on the console (Priority: P3)

**Goal**: one structured stderr line per finished span, text by default and JSON by setting, with the same steps as the trace and no content or secrets.

**Independent Test**: quickstart D.3. The number of console step lines equals the number of `trace.jsonl` lines for the run.

### Tests for User Story 3 ⚠️

- [ ] T029 [P] [US3] Write `tests/crawler/test_logs.py`:
  - one line per finished span, carrying `run_id`, `step`, `tool`, `outcome` and `duration_ms`
  - an ERROR level for an ERROR status, an `error` attribute, or a failing `outcome`
  - INFO for spec 003 `NOT_FAILURES` outcomes
  - `CRAWLER_LOG_FORMAT=json` gives one valid JSON object per line
  - lines never contain `CONTENT_KEYS` text or secret values
  - for a fake run, the number of lines equals the number of `trace.jsonl` records

### Implementation for User Story 3

- [ ] T030 [US3] Write `crawler/logs.py`:
  - `LogSpanProcessor(SpanProcessor)` whose `on_end` emits one `logging` record from the span (name, attributes, status, duration)
  - `TextFormatter` and `JsonFormatter`
  - `configure_logging(env) -> logging.Logger`, which reads `CRAWLER_LOG_FORMAT`
- [ ] T031 [US3] Register `LogSpanProcessor` in `Tracing.__init__` in `crawler/tracing.py`. Route the run-level stderr prints in `crawler/cli.py` (budget description, refusals, summary, reopened notice) through the same logger with `step=crawler.cli`, keeping the exact message texts that `test_crawler_cli.py` asserts

**Checkpoint**: T029 passes. Console lines and `trace.jsonl` match for a replayed run.

---

## Phase 6: Polish & Cross-Cutting

- [ ] T032 [P] Update `specs/003-crawler-agent/quickstart.md` and the spec 003 `research.md` R10 note: the OTLP exporter is now in the `crawler` extra, and its failures are reported
- [x] T033 [P] Constitution follow-up for the `.specify/` ruff exclude: done 2026-11-25 as the PATCH amendment 1.5.1 to principle II in `.specify/memory/constitution.md` (analysis D1)
### Analysis follow-ups (from `/speckit-analyze`, 2026-11-25; do these before T034/T035)

- [ ] T036 [P] (U1, SC-003) Add a "failing tool" forced-failure step to `quickstart.md` D.4: an invalid search-provider key makes the search tool escalate, giving exit 5, outcome `escalated`, one email and an ERROR span in Langfuse. Add an offline case to `tests/crawler/test_trigger.py` that maps a fake exit 5 to `escalated`
- [ ] T037 [P] (I3) Reword FR-001b in `spec.md` to "…and do nothing except start the crawler command, report its status, and stop it at the maximum run time (FR-006)"
- [ ] T038 [P] (C2, FR-008) Add a case to `tests/crawler/test_tracing_export.py`: every exported span's parent id matches the `parent_id` of the same span in `trace.jsonl` (same nesting)
- [ ] T039 [P] (A1) Replace the "Default maximum run time is the crawler's own budget limit plus a margin" assumption in `spec.md` with "Default maximum run time is 3 h (`CRAWLER_MAX_RUNTIME_S`), a placeholder tuned after the first live run (research R5, T035)"
- [ ] T040 (D2) Draft a PATCH amendment in `.specify/memory/constitution.md` (1.5.1 → 1.5.2, with the Sync Impact Report): under Agent loops → HITL, state that a standing approval, given in advance by a person, time-limited, and bound to the exact models and prices, counts as human approval before spending. Leave it for the maintainer to review in the PR
- [ ] T041 [P] (T1) Add `trigger_unreachable` to the Workflow Execution outcome list in `spec.md` Key Entities, to match data-model.md and FR-015a
- [ ] T042 [P] (U2) Change T017 to name one file: the `trigger` subcommand is registered in `crawler/cli.py` (`crawler/__main__.py` only calls `cli.main`; check before editing)

Not a task: analysis C4 (the SC-005 scan of the console and Langfuse export is manual) stays in T035, because T021 and T029 already check for secret values offline.

- [ ] T034 Run `uv run pytest tests/crawler -q`, then the coverage gate. Run `pre-commit run --files` on the changed files (never `ruff` on `.`; see the repo note on `.specify/`). Confirm ≥85% coverage for `crawler`, and that `tests/crawler/test_crawler_guard.py` still passes with the new modules (analysis C3: `harness/` and `pipeline/` don't import `crawler/`)
- [ ] T035 Maintainer only, spends money: run quickstart parts B–D. Record the measured SC-001 to SC-005 results in the PR description (only executed results), and set `CRAWLER_MAX_RUNTIME_S` from the observed run time. Activate the schedule only after D passes

---

## Dependencies & Execution Order

### Phase Dependencies

- Setup (T001–T002) → Foundational (T003–T007) → all user stories.
- US1, US2 and US3 depend only on Foundational. They touch some of the same files (`crawler/cli.py`, `crawler/tracing.py`), so do them **in order P1 → P2 → P3** when one person works alone.
- Polish runs after the stories it covers; T035 comes last.

### Within each story

- Tests first, and they must fail. Then schema → domain module → CLI wiring → infrastructure files.
- US1: T012 → T013 → T014/T015 → T016 → T017. T018 is independent. T019 needs T016's contract fixed.
- US2: T023 → T024/T025 → T026. T027 is independent. T028 comes after T023.
- US3: T030 → T031 (T031 edits `tracing.py` after T023).

### Parallel Opportunities

- T002 alongside T001. T003, T004 and T006 together (different files).
- US1 tests T008–T011 together, and T018 at any point in US1.
- US2 tests T021–T022 together, and T027 at any point in US2.
- T032 and T033 together.

## Parallel Example: User Story 1

```text
Together: T008 test_approval.py | T009 test_cli_run_standing.py | T010 test_trigger.py | T011 test_orchestration_files.py | T018 n8n docker-compose.yaml
Then in order: T012 migration → T013 approval.py → T014 + T015 cli.py → T016 trigger.py → T017 → T019 workflow export → T020 docs
```

## Implementation Strategy

### MVP first (US1 only)

1. Phases 1–2, then Phase 3.
2. Stop and validate with quickstart A–C plus D.1, D.2 and D.4. Scheduled, approved, single-run orchestration with failure emails works without Langfuse.

### Incremental delivery

1. US1 → demo the unattended start.
2. US2 → Langfuse traces (the mission's headline).
3. US3 → console parity.
4. Polish, then the live run (T035) for the measured success criteria.
