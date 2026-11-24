# Data Model: Orchestrate the Exam Pipeline with Apache Airflow 3

> **Superseded in part (amendment A3, 2026-11-18).** The orchestrator is Dagster, not
> Airflow: assets partitioned by exam, one uv environment (the `orchestration` extra), no
> sensors or schedules. Read [research.md](research.md) D1–D5 for the decision record and
> [contracts/assets.md](contracts/assets.md) and [contracts/stack.md](contracts/stack.md)
> for the current contracts. Mapping: DAG → asset group; `select_exams` → static
> partitions; `pipeline_writer` pool → Dagster pool; `pending_summary` → `pending_review`
> check; Clear failed → Re-execute from failure. The text below is kept as the original
> record, and where it names Airflow concepts the mapping above applies.

The spec's key entities, as Airflow objects. The pipeline's own data model is unchanged
except for one operational table, `host_requests`.

## Workflow (DAG)

| DAG id | Trigger | Params | Tasks (in order) |
|--------|---------|--------|------------------|
| `exam_acquisition` | manual only (`schedule=None`) | `exams`: array of strings, default `[]` (= all) | `select_exams` → `catalog` → `exam_branch[exam]` → `snapshot`¹ → `announce_snapshot`¹; `pending_summary` after the branches |
| `exam_export`¹ | asset `pipeline-snapshot`, or manual | `snapshot`: string, default `""` (manual runs must set it) | `resolve_snapshot` → `exportable_exams` → `export[exam]` |
| `exam_verify`² | manual only | `exam` (string, required), `seed` (integer, required), `kind` (`handcheck` \| `translation`), `language` (string, needed for `translation`) | `draw` → `status` |

¹ Added when spec 001's snapshot and export stages exist (FR-004).
² Added when spec 001's verify stage exists.

All DAGs share these settings:
- `catchup=False` and a fixed `start_date` (2026-11-01, UTC);
- `default_args = {"retries": 2, "retry_delay": 3 min}`;
- tags `["exam-pipeline"]`;
- no email, no callbacks, no SLA (FR-027).

## Run (DAG run)

One execution, with outcome success or failed as Airflow defines it. Pending candidates
never change the outcome (FR-007). Resuming is not a new run: the maintainer clears the
failed task instances "only failed, with downstream" in the same run (FR-025).

## Stage task

| Field | Source | Notes |
|-------|--------|-------|
| `task_id` | the stage name (`catalog`, `discover`, `fetch`, …) | inside `exam_branch` it is `exam_branch.<stage>`, with a map index per exam |
| argv | `["<stage>", …flags]`, plus `--exam <exam>` inside a branch | always run as `uv run … pipeline <argv>` (research A1) |
| `pool` | `pipeline_writer` for writing commands; `default_pool` otherwise | see Writer slot |
| exit code → state | research A2 | 0 success; 1 and 4 retry; 2, 3 and other codes fail with no retry |
| log | the command's stdout and stderr lines, unchanged | the pipeline prints counts and IDs only; `run_stage` adds only the argv and the exit code, never the environment |
| XCom (return value) | `StageResult` (below) | small, with no text from the corpus |

`StageResult` (a `TypedDict`, JSON-serialisable):

| Key | Type | Example |
|-----|------|---------|
| `stage` | str | `"fetch"` |
| `exam` | str \| None | `"csat"` |
| `summary` | str | `"fetch: fetched=3 unchanged=12 refused=0 errors=0 skipped=1"` |

The discover task returns `DiscoverResult`, which is `StageResult` plus:

| Key | Type | Example |
|-----|------|---------|
| `pending` | list[str] | `["src_4f2a…", "src_91c0…"]` |

## Exam branch

One mapped instance of the `exam_branch` task group per selected exam. The map index
order is the sorted exam list from `select_exams`, so a given exam always gets the same
index in a run.

| Stage (in order) | Command | Writer | Present |
|------------------|---------|--------|---------|
| discover | `discover --exam X`, then `candidates --exam X` | yes | now |
| fetch | `fetch --exam X` | yes | now |
| extract | `extract --exam X` | yes | when spec 001 US2 lands |
| key | `key --exam X` | yes | when spec 001 US2 lands |
| translate | `translate --exam X` | yes | when spec 001 US3 lands |

State rules:
- If a task fails, the later tasks in its instance become `upstream_failed`, and other
  instances keep going (FR-005).
- `snapshot` runs only if every instance's last task succeeded (default trigger rule).
- `pending_summary` runs once every instance has ended, whatever the outcome
  (`all_done`).

## Writer slot

The Airflow pool `pipeline_writer`, with `slots=1`. The `airflow-init` service creates
it. Every task running `catalog`, `discover`, `fetch`, `extract`, `key`, `translate`,
`snapshot` or `verify draw` uses it, in any DAG.

Read-only commands never use it. These are `exams`, `candidates`, `export` and
`verify status` (spec 001 CLI contract: `export` and `verify status` take no lock).

## Snapshot event

| Field | Value |
|-------|-------|
| asset | `Asset("pipeline-snapshot")` (a name, no URI scheme) |
| producer | `exam_acquisition.announce_snapshot`. It succeeds only when the snapshot summary line is `published`; when the line is `unchanged`, it is skipped, so no event is sent. |
| `extra` | `{"snapshot": "snap-NNNN"}` |
| consumer | `exam_export` (`schedule=[SNAPSHOT]`), which reads `extra["snapshot"]` from `triggering_asset_events` |

## Pipeline table added by P3

```sql
-- Operational state, not dataset content. It is not rebuilt by `pipeline rebuild` and
-- not part of any snapshot; losing it costs at most one interval wait per host.
CREATE TABLE IF NOT EXISTS host_requests (
    host             text PRIMARY KEY,   -- lowercase hostname, e.g. www.kice.re.kr
    last_request_at  timestamptz NOT NULL -- wall-clock time of the last request sent
);
```

Rules:
- It is written only by commands that hold the write lock (`discover`, `fetch`), so
  there is never more than one writer.
- It is read once when `Fetcher` starts. Before the first request to a host, the
  fetcher waits `max(0, interval(host) − (now − last_request_at))`. It upserts the row
  after every request, including `robots.txt` and retried requests.
- A `last_request_at` in the future (clock skew) is treated as now.

## Secrets and configuration (environment only)

| Variable | Where it is set | Read by |
|----------|-----------------|---------|
| `PIPELINE_DSN` | compose, from `orchestration/.env` (container form); the maintainer's shell (Mac form, `localhost`) | the pipeline only. `run_stage` passes the environment through and never logs it. |
| `PIPELINE_DB_PASSWORD`, `AIRFLOW_DB_PASSWORD` | `orchestration/.env` | the Postgres init script and compose interpolation |
| `ANTHROPIC_API_KEY` | `orchestration/.env`, when translate lands | the pipeline's translate stage |
| `AIRFLOW_UID` | `orchestration/.env` | compose: makes the container user write files owned by the Mac user |

There are no Airflow Variables or Connections (FR-015).
