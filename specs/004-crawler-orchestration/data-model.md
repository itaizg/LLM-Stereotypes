# Data Model: Crawler Orchestration and Tracing

## Standing Budget Approval (`crawler_budget_approvals`, Postgres)

| Field | Type | Rule |
|---|---|---|
| `approval_id` | text PK | `ap-<UTC timestamp>-<4 hex>` |
| `approved_by` | text, not null | `$USER` at the TTY prompt |
| `approved_at` | timestamptz, not null | |
| `expires_at` | timestamptz, not null | `approved_at + 30 days` (FR-004) |
| `revoked_at` | timestamptz, null | set when a newer approval is given, or by `crawler approve --revoke` |
| `limits` | jsonb, not null | `{max_queries, max_cost_usd (USD), max_iterations}` from the approved `Budget` |
| `pricing_fingerprint` | text, not null | SHA-256 of the canonical JSON of `agent_model` + `classifier_model` (model names, prices in USD/Mtok) |
| `pricing` | jsonb, not null | the readable form of what was fingerprinted, for audit |

**Valid for a run** if all of these hold:
- `revoked_at IS NULL`
- `now() < expires_at`
- `pricing_fingerprint` equals the run's fingerprint
- each of the run's limits is ≤ the matching value in `limits`

**State transitions:**
- `active` → `revoked`, when a new approval is given or on `--revoke`.
- `active` → `expired`, when time passes `expires_at`. This is derived, not stored.

There is at most one active approval, enforced by `CREATE UNIQUE INDEX … ON crawler_budget_approvals ((true)) WHERE revoked_at IS NULL`. Creating a new approval revokes the old one in the same transaction.

## Crawler Run (`crawler_runs`, existing; changes)

- New column `approval_id text NULL REFERENCES crawler_budget_approvals`: set for `--approval standing`, null for runs approved at the TTY.
- `approved_by` and `approved_at` come from the standing approval when one is used.
- The status set is unchanged in Postgres. A refused run (not approved, busy) writes **no** `crawler_runs` row, because it spends nothing and writes nothing (spec 003, FR-001). Those outcomes exist only in `summary.json`, `trigger.json` and n8n history.

## Run Artifacts (`$CRAWLER_RESULTS_DIR/<run_id>/`, default `results/crawler/<run_id>/`)

| File | Written by | Content |
|---|---|---|
| `summary.json` | crawler | Existing fields, plus `trace_export` `{mode: "full"\|"metadata"\|"off", endpoint_set: bool, spans_exported: int, spans_failed: int, error: str\|null}`, plus `approval_id`, plus `terminated: bool`. Now also written for refusals: `status: "not_approved"` or `"busy"`, with zero counts. |
| `trace.jsonl` | crawler | Unchanged format. Never contains `langfuse.observation.input`/`output` (FR-010b). |
| `trigger.json` | run trigger | `{run_id, pid, started_at, deadline_at, ended_at, exit_code, outcome}`. Written at launch, then rewritten when the run ends. |

## Workflow Execution (n8n history; not stored by this repo)

| Field | Values |
|---|---|
| trigger | `schedule` \| `webhook` |
| run_id | from the trigger's start response, or absent when the trigger was unreachable |
| outcome | `completed`, `failed`, `escalated`, `stopped_at_limit`, `not_approved`, `timed_out`, `skipped`, `trigger_unreachable` |
| notified | true for every outcome except `completed` and `skipped` (FR-015a) |

**Exit code → outcome** (one place: `crawler/trigger.py`, mirrored in [contracts/trigger-api.md](contracts/trigger-api.md)):

| Exit | Outcome |
|---|---|
| 0 | completed |
| 1 | failed |
| 2 | failed (usage error: bad config or missing env var) |
| 3 | not_approved |
| 4 | skipped (busy) |
| 5 | escalated |
| 6 | stopped_at_limit |
| killed at the deadline | timed_out (overrides the exit code) |

## Run Trace (Langfuse; external)

- One trace per run. The root span is `crawler.run`, with `langfuse.session.id = run_id`.
- Child spans: `agent.step` (generation), `tool.<name>`, and the classifier call (generation).
- The attribute list is in [contracts/telemetry.md](contracts/telemetry.md).

## Console Log Line

`{ts, level, run_id, step, tool, outcome, duration_ms}`: one line per finished span (see [contracts/telemetry.md](contracts/telemetry.md)).
