# Implementation Plan: Orchestrate the Exam Pipeline with Apache Airflow 3

> **Superseded in part (amendment A3, 2026-11-18).** The orchestrator is Dagster, not
> Airflow: assets partitioned by exam, one uv environment (the `orchestration` extra), no
> sensors or schedules. Read [research.md](research.md) D1–D5 for the decision record and
> [contracts/assets.md](contracts/assets.md) and [contracts/stack.md](contracts/stack.md)
> for the current contracts. Mapping: DAG → asset group; `select_exams` → static
> partitions; `pipeline_writer` pool → Dagster pool; `pending_summary` → `pending_review`
> check; Clear failed → Re-execute from failure. The text below is kept as the original
> record, and where it names Airflow concepts the mapping above applies.

**Branch**: `002-airflow-orchestration` (branched from
`feat/data-aggregation-pipeline`, because spec 001's acquisition PR is still open) | **Date**: 2026-11-09 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/002-airflow-orchestration/spec.md`

## Summary

Three Airflow 3.1 DAGs run the existing `pipeline` stage commands. They hold no stage
logic:

- **`exam_acquisition`**: manual trigger with an optional exam filter. It runs `catalog`
  once, then a mapped task group per exam (`discover → fetch`, extended stage by stage as
  spec 001 delivers), then `snapshot` only if every branch succeeded, plus a summary task
  that always runs and warns about pending candidates.
- **`exam_export`**: starts on the snapshot asset event and exports every exam the
  snapshot does not mark unverified.
- **`exam_verify`**: manual; starts a hand-check draw.

Every task shells out to `uv run pipeline <stage>` and maps its exit code to an Airflow
outcome. A one-slot pool mirrors the pipeline's write lock. Airflow runs from the
official Python 3.13 image with LocalExecutor, in a compose stack whose one Postgres
server holds Airflow's database and the pipeline's. The repository is mounted, so the
Mac's `pipeline` commands and the tasks share one store.

Planning found four small gaps in the pipeline's command line, which the DAGs and
their checks need (research A5–A7, A11). They ship first, as pipeline changes with their
own tests:

- **P1** `pipeline exams [--snapshot V]`: read-only exam list. The `--snapshot` form
  ships with spec 001's export stage.
- **P2** `pipeline candidates [--exam X]`: read-only list of pending candidates.
- **P3** Per-host request spacing persisted across invocations. Without it the existing
  contract's "the interval holds across invocations" is false, even by hand.
- **P4** `python -m pipeline.corpus PATH…`: the corpus guard also scans paths given on
  the command line, tracked or not, so it can check Airflow's task logs (SC-009).

## Technical Context

**Language/Version**: Python 3.13 for the project and for Airflow (official image
`apache/airflow:3.1.6-python3.13`).

**Primary Dependencies**:
- Apache Airflow 3.1.6, image only. It is never in `pyproject.toml` or `uv.lock`.
- uv 0.12.1, copied into the image.
- `postgres:17.6`.
- The project's existing `pipeline` extra, installed into a separate venv in the image.
- No new Python dependency in the project.

**Storage**: One Postgres 17 server with two databases, `airflow` (Airflow's metadata)
and `pipeline` (the spec 001 store, plus the new `host_requests` table from P3). Files:
the mounted repository (ledger, catalog, `./.pipeline-data` blob root, `exports/`).
Airflow task logs go in a gitignored `orchestration/logs/`.

**Testing**:
- **`tests/airflow/`**: pytest inside the image's `test` target. It uses `dag.test()`
  with a `FakeRunner` and a temporary SQLite metadata database, with no scheduler and no
  network.
- **`tests/pipeline/`**: P1–P4, plus the per-exam equivalence test. It uses the existing
  Postgres and MockTransport fixtures.
- **Core suite**: unchanged, with `--ignore=tests/airflow` added.

**Target Platform**: Docker Desktop on the M4 Max Mac (linux/arm64 containers); CI on
`ubuntu-latest` (linux/amd64). The image builds on both.

**Project Type**: Orchestration layer (DAG files and a compose stack) on top of a
CLI-driven batch pipeline.

**Performance Goals**: None. Runs are bound by the per-host 5 s interval, and writing
stages run one at a time by design.

**Constraints**:
- Workflow files contain no stage logic and use the Task SDK only (FR-001, FR-023).
- Secrets come from environment variables only.
- Logs and XComs carry counts and identifiers only.
- The core install, the `pipeline` install and `uv.lock` contain no Airflow.
- Tests are offline and need no scheduler.
- Files stay under 800 lines.

**Scale/Scope**: 6 exams, so at most 6 branches per run, 3 DAGs, and about 5 services.

No item is left as NEEDS CLARIFICATION: the five spec clarifications plus
[research.md](research.md) settle every choice.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Checked against constitution v1.1.0 and team directives
([team-context.md](team-context.md)).

| Gate | Pre-design | Post-design | How the design meets it |
|------|-----------|-------------|-------------------------|
| I. Typed code, checked by ty | PASS (with A2) | PASS | `orchestration/dags/` and `tests/airflow/` are fully annotated. ty cannot resolve `airflow` in the project venv, so amendment A2 moves their ty check into the Airflow CI job and the image's `test` target, where Airflow is importable. The project-wide hook excludes those two paths. P1–P4 live in `pipeline/` and use the existing ty gate. |
| II. ruff only | PASS | PASS | The new paths fall under the existing ruff config; ruff needs no imports resolved. |
| III. Tests, offline, deterministic | PASS | PASS | No scheduler, network or GPU is used. `dag.test()` runs in one process on SQLite. `FakeRunner` scripts exit codes, and P3's tests inject a wall clock and a monotonic clock. Each behaviour change gets a failing-first test. SC-001 is checked against known answers (equal event sets and row counts), not just "runs". |
| IV. KISS / YAGNI / DRY | PASS | PASS | **KISS**: one helper (`run_stage`) and no custom operators; the pool and trigger rules are built-in Airflow features. **YAGNI**: no triggerer, Celery, notification code or schedule; the DAG lists only the stages that exist. **DRY**: retry numbers live in one constant; the Airflow version only in the Dockerfile `FROM`; the exam list and pending state come from the pipeline (P1/P2), never re-derived in a DAG. |
| V. Structure / SOLID / LoD / CoC | PASS | PASS | One module per DAG, plus `stage.py` for invoking commands. DAGs depend on the CLI contract (argv, exit codes, summary lines), not on pipeline internals. A new exam is a catalog edit; a new stage is one entry in the branch. `harness/` and `pipeline/` never import Airflow (a guard test checks it). |
| Quality gates | PASS (with A2) | PASS | A new `airflow` CI job builds the `test` image and runs `tests/airflow` plus ty on the new paths. It uses pinned actions, `contents: read` and no secrets. The core and pipeline jobs are unchanged except for the ignore flag. |
| Project constraints: dependencies | **BLOCKED until A2 merges** | PASS (sequenced) | FR-021: amendment A2 lands first, with the CI skeleton and the ty exclusion it introduces (research A12). Airflow is not added to `pyproject.toml` or `uv.lock`. |
| Project constraints: separation, integrity, secrets | PASS | PASS | Only the pipeline is orchestrated, never the harness (a TODO item). Passwords live in the gitignored `orchestration/.env` and reach containers only as environment variables. Airflow Variables and Connections are not used. The README results stay TODO until a real run (SC-012). |
| Team: Airflow DAG patterns rule | PASS (adapted) | PASS | Adopted: `dags/` package, shared `default_args` with bounded retries, task groups, no schedule, `catchup=False`, an import test. Overridden by spec and constitution, as recorded in team-context.md: Airflow 2 APIs (`schedule_interval`, `days_ago`, `globals()` registration), `Variable.get`, KubernetesPodOperator, and alert callbacks. |
| Team: CDR-2026-027 pytest, CDR-2026-022 file size, CDR-2026-011 Actions | PASS | PASS | Class-grouped tests with fixtures in `tests/airflow/conftest.py`, parametrized exit-code tables, files under 800 lines, and a separate CI job with pinned actions. |

## Delivery order

Each step is a PR-sized commit series. Nothing needs a running Airflow until step 4.

1. **Amendment A2** (FR-021), with the `airflow` CI job skeleton, the ty exclusion and
   the CLAUDE.md line.
2. **Pipeline P1–P4**, with tests in `tests/pipeline/`, the contract update in
   [contracts/pipeline-cli-additions.md](contracts/pipeline-cli-additions.md) (mirrored
   into spec 001's `contracts/cli.md`), and the per-exam equivalence test (SC-001,
   pipeline half).
3. **Stack**: Dockerfile (`runtime` and `test` targets), compose file, Postgres init
   script, `.env.example`, `.dockerignore`, and `orchestration/logs/` in `.gitignore`.
4. **`stage.py` and `exam_acquisition`** (US1–US3), with `tests/airflow/`.
5. **`exam_verify`**: once spec 001's verify stage exists.
6. **`exam_export`** and `announce_snapshot` (US4): once spec 001's snapshot and export
   stages exist.
7. **README section and the real run** (SC-012): the counts are recorded only after the
   run; until then the README carries a TODO.

Steps 5–6 wait on spec 001 and may land after the rest (FR-004).

## Project Structure

### Documentation (this feature)

```text
specs/002-airflow-orchestration/
├── plan.md              # This file
├── research.md          # Phase 0: decisions A1–A12
├── data-model.md        # Phase 1: workflows, tasks, params, XComs, pool, asset, tables
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── dags.md                    # DAG ids, params, task graph, outcomes, XComs
│   ├── stack.md                   # compose services, env vars, mounts, ports, image
│   └── pipeline-cli-additions.md  # P1–P4 and the summary lines the DAGs parse
├── team-context.md      # team directives (before_specify / before_plan hooks)
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
orchestration/
├── Dockerfile               # FROM apache/airflow:3.1.6-python3.13; targets runtime, test
├── docker-compose.yaml      # postgres, airflow-init, apiserver, scheduler, dag-processor
├── postgres-init/
│   └── 10-pipeline-db.sh    # creates role + database "pipeline" from env
├── .env.example             # placeholders only; real .env is gitignored
└── dags/
    ├── __init__.py
    ├── stage.py             # run_stage(): argv → uv run pipeline … → outcome; summary parsing
    ├── acquisition.py       # DAG exam_acquisition
    ├── export.py            # DAG exam_export
    └── verify.py            # DAG exam_verify

pipeline/
├── __main__.py              # + `exams`, `candidates` commands (P1, P2)
├── acquire.py               # + pending_candidates() (P2)
├── web.py                   # + persisted per-host spacing (P3)
├── store.py                 # + host_requests read/upsert (P3)
├── corpus.py                # + optional PATH… arguments (P4)
└── schema.sql               # + host_requests table (P3)

tests/
├── airflow/                 # new; runs only in the airflow CI job / test image
│   ├── conftest.py          # AIRFLOW_HOME tmp + SQLite, FakeRunner, pool fixture
│   ├── test_dag_integrity.py
│   ├── test_stage.py
│   ├── test_acquisition.py
│   ├── test_verify.py
│   └── test_export.py
└── pipeline/
    ├── test_cli_listing.py  # P1, P2
    ├── test_guard.py        # + scanning given paths (P4)
    ├── test_web.py          # + cross-invocation spacing (P3)
    └── test_equivalence.py  # per-exam stage runs == one all-exam run (SC-001)

.github/workflows/ci.yml     # + airflow job; core job --ignore=tests/airflow
.dockerignore                # keeps .venv, .pipeline-data, exports, results, legacy out
```

**Structure Decision**: The directory is named `orchestration/`, not `airflow/`. A
top-level `airflow/` directory would sit on `sys.path` next to the real package whenever
Python runs from the repository root. `dags/` follows the team rule's layout.

## Complexity Tracking

| Item | Why Needed | Simpler Alternative Rejected Because |
|------|------------|-------------------------------------|
| Pipeline changes P1–P4 inside an orchestration feature | FR-005/007/009/012 and SC-009 need data or checks that only the pipeline owns (exam list, pending state, exportable exams, per-host timing, corpus fingerprints) | Re-deriving them in DAG code duplicates pipeline knowledge (DRY) and puts stage logic in workflows (FR-001). |
| A second ty run outside the project venv (amendment A2) | `airflow` is not importable in the project venv by design (FR-020) | Adding Airflow to the dev extra would put it in `uv.lock`, which FR-020 forbids. |
| SC-001/SC-011 checked in two halves | The pipeline cannot run inside Airflow's environment (FR-019) | A single end-to-end test would need both in one interpreter. |
