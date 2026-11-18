# Contract: Dagster assets

*(Replaces `contracts/dags.md`; see [research.md](../research.md) D1–D5.)*

What the maintainer and the tests can rely on. The code location is `orchestration/exam_dagster/`
(`stage.py`, `assets.py`, `definitions.py`). Entities and fields are defined in
[data-model.md](../data-model.md).

## Common behaviour

- **One environment.** Dagster is the optional `orchestration` extra of the project
  (amendment A3). It never enters `harness/` or `pipeline/`; the pipeline runs as a
  subprocess because the CLI is the contract: the write lock, the exit codes, and "a step
  runs exactly what a maintainer types".
- **One stage command per step**, through `stage.run_stage(argv, exam=None, logger=None)`,
  which runs `<python> -m pipeline <argv>` with `cwd` = `PIPELINE_PROJECT` (else this
  checkout) and the process environment, unchanged.
- **Outcomes**:

  | Exit | Raises | Effect |
  |------|--------|--------|
  | 0 | — | success; returns `StageResult` |
  | 1, 4 | `RetryableStageError` | retried by the asset's `RetryPolicy`: 2 retries, 3 minutes apart |
  | 2, 3, other | `dagster.Failure(allow_retries=False)` | failed, no retry |

- **Logging** (to the step's log):
  - one INFO line `pipeline <argv>` before the run;
  - every stdout line at INFO and every stderr line at WARNING;
  - one INFO line `exit=<code>`.
  - The environment, the DSN and the command's working files are never logged.
- **Results are metadata, never data.** Assets return `MaterializeResult(metadata=…)` with
  counts, ids and the summary line. There is no IO manager and nothing is pickled: the data
  stays in the pipeline's store (FR-014).
- **No schedules, no sensors, no notifications** (FR-027). Runs are manual.
- **Only the orchestration module reads `PIPELINE_PROJECT`** from the environment. The DSN
  and tokens reach the pipeline through the inherited environment (FR-015).

## `exam_acquisition`

Group of three assets, partitioned by exam, plus the job `exam_acquisition` that selects the
group. Trigger: manual, from the UI (Materialize, or Launch backfill over the chosen
partitions) or `dagster job backfill`.

```text
catalog ──► discovered[exam] ──► fetched[exam]
```

| Asset | Partitioned | Command(s) | Pool | Metadata | Outcome rules |
|-------|-------------|------------|------|----------|---------------|
| `catalog` | no | `catalog` | `pipeline_writer` | `summary` | Exit 3 (invalid catalog) fails it, and nothing downstream runs (US1 scenario 3). |
| `discovered` | exam | `discover --exam X`, then `candidates --exam X` | `pipeline_writer` | `summary`, `pending_count`, `pending_ids` | Also yields the check `pending_review`. Logs a WARNING `pending candidates for X: N (id, id, …)` when N > 0 (FR-007). |
| `fetched` | exam | `fetch --exam X` | `pipeline_writer` | `summary` | Fetch records refusals and HTTP errors in the ledger and still exits 0, so the step succeeds (edge case). |

**Partitions** are the exams `pipeline exams` lists when the definitions load. An unknown
exam is not a partition, so it cannot be launched (FR-026). A broken catalog makes
`pipeline exams` exit 3 and the code location fails to load with that message. After editing
`catalog.yaml`, use **Reload definitions** in the UI (or restart `dagster dev`).

**`pending_review` check** (on `discovered`, severity WARN): `passed` is true when no
candidate is pending. Pending candidates show as a warning, never a failure.

**Writer serialisation**: pool `pipeline_writer`, limit 1 (`concurrency.pools.default_limit`
in each `dagster.yaml`; granularity `op`, the default). Only one writing step runs at a
time across every run and backfill (SC-005).

**Order across exams**: partitions run in no fixed order (the pool serialises them but does
not order them). Only the order within one exam is part of the contract: `discover`,
`candidates`, `fetch`.

**Backfills.** A failed partition does not stop the others. Each partition's status is shown
in the backfill view, replacing the old `pending_summary` task together with its `all_done`
trigger rule and its final failing step.

**Resume (FR-025)**: open the failed run and choose **Re-execute from failure**, or launch a
backfill over the failed partitions only. Steps that succeeded are not repeated; `fetch`
reports `unchanged` for files already stored, which means conditional requests and no
re-download.

## `exam_export` *(lands with spec 001 snapshot and export; not built yet)*

An `exported` asset partitioned by exam, downstream of a `snapshot` asset, started manually
(or with `AutomationCondition.eager()` if a later decision wants it). Only exams listed by
`pipeline exams --snapshot V` are partitions that can be exported, so unverified exams never
reach it (FR-009). Command: `export --exam X --eligible --snapshot V`.

## `exam_verify` *(lands with spec 001 verify; not built yet)*

A small op job, manual only, with run config
`{"exam": "csat", "seed": 7, "kind": "handcheck"}` or
`{"exam": "enem", "seed": 3, "kind": "translation", "language": "pt"}`.

| Op | Command | Pool |
|----|---------|------|
| `draw` | `verify draw --exam X --seed S [--kind K --language L]` | writer |
| `status` | `verify status --exam X` | none |

`verify record CSV` is a command-line step done by the maintainer and is not part of this job.
