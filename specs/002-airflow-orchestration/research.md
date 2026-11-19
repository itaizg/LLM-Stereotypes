# Research: Orchestrate the Exam Pipeline with Apache Airflow 3

> **A1–A12 below record the Airflow design, which is superseded.** The decisions that
> replace them are D1–D5 at the end of this file. A2 (the exit-code policy), A4 (one writer
> at a time) and the P1–P4 CLI additions carry over unchanged in behaviour.

Phase 0 decisions for [plan.md](plan.md). Each entry gives the decision, why it was
chosen, and what was rejected. Airflow APIs were checked against the Airflow 3.1.6 docs
(Task SDK, dynamic task mapping, assets, params, prerequisites) on 2026-11-09.

## A1. How a task runs a stage

**Decision**: Each stage task is a Python `@task` that calls one shared helper,
`run_stage(args)`. The helper runs
`uv run --project /opt/project --locked --offline --extra pipeline pipeline <args>` as a
subprocess with `cwd=/opt/project`, logs the command's stdout and stderr lines, and maps
the exit code to an outcome (A2). It returns the stage's summary line for XCom.

**Rationale**: FR-001 asks for the same command a maintainer types. A subprocess is the
only way to run it from Airflow's interpreter without installing the pipeline into
Airflow's environment (FR-019). A Python task is needed because the bash operator turns
every non-zero exit into a retryable failure; it cannot express "exit 2 and 3 never
retry". `--locked` asserts that the lockfile matches `pyproject.toml`, as
`uv sync --locked` does in CI. `--offline` guarantees that a task never downloads a
package; the image already holds every dependency.

**Rejected**:
- `@task.bash` / `BashOperator`: no per-exit-code retry control (only
  `skip_on_exit_code`).
- Importing `pipeline` inside the task: it would need the pipeline's dependencies
  (psycopg, httpx, pyarrow…) in Airflow's environment, which FR-019 and FR-020 forbid.
- `python -m pipeline`: it works without installing the project, but FR-001 names
  `uv run pipeline <stage>`, and the console script is the documented interface.

**Checked against the built image** (T006): `uv run --project /opt/project --locked
--offline --extra pipeline pipeline --help` works with `--network none`, but only because
the image build also fetches the project's build backend. Without that, the offline run
fails with "setuptools was not found in the cache": `uv run` installs the mounted
project on first use, and a lockfile sync never asks the index for the build
requirements.

## A2. Exit codes → task outcomes (FR-006)

**Decision**:

| Exit | Meaning (spec 001 CLI contract) | Task outcome |
|------|-------------------------------|--------------|
| 0 | success | success |
| 1 | unexpected error | raise `AirflowException` → retried |
| 2 | usage error | raise `AirflowFailException` → failed, no retry |
| 3 | integrity refusal | raise `AirflowFailException` → failed, no retry |
| 4 | write lock held | raise `AirflowException` → retried |
| other | not in the contract | raise `AirflowFailException` → failed, no retry |

Every DAG's `default_args` set `retries=2`, `retry_delay=timedelta(minutes=3)`, so
exits 1 and 4 get two retries three minutes apart. This is the spec's planning default
(Assumptions); the two numbers live in one module constant.

**Rationale**: `AirflowFailException` is Airflow's documented way to fail and ignore
the remaining retries. One retry policy covers both retryable codes. A lock wait of
three minutes is long enough for a hand-run `pipeline review` and short enough not to
stall a run. Unknown codes fail fast rather than loop. `uv`'s own failures (for example,
a stale image after a lockfile change) are non-zero codes too: the task log shows
`uv`'s message, and the fix is to rebuild the image (quickstart).

**Rejected**: separate retry delays per exit code. Airflow has one `retry_delay` per
task; emulating two would mean sleeping inside the task, which holds the writer slot.

## A3. Per-exam branches and the snapshot rule (FR-003, FR-005, FR-026)

**Decision**: The acquisition DAG is:

```text
select_exams → catalog → exam_branch.expand(exam=…) → snapshot → announce_snapshot
                                                    ↘ pending_summary (all_done)
```

- `select_exams` runs `pipeline exams` (P1 in the plan), reads the trigger's `exams`
  param (default `[]` = all), and fails with no retry if a name is not in the catalog.
  It runs before `catalog`, so an unknown name writes nothing at all.
- `exam_branch` is a `@task_group` mapped with `expand(exam=…)`. Its tasks are the
  implemented per-exam stages in order, each running `pipeline <stage> --exam <exam>`.
  Today these are `discover → fetch`. `extract`, `key` and `translate` are added to the
  group in the spec 001 PR that implements each one (FR-004).
- `snapshot` uses the default trigger rule `all_success`, so one failed branch skips it
  (FR-005). It is added when spec 001's snapshot stage lands; until then the DAG ends
  at the branches and the summary.
- `pending_summary` uses `trigger_rule="all_done"`, so it runs whatever the branches did.

**Rationale**: Mapped task-group instances are independent: one exam's failure marks
only that instance's downstream tasks `upstream_failed`, and the other instances keep
running. That is FR-005 with no custom code. The exam list is read at run time, so a
catalog edit needs no DAG change (Open/Closed).

**Rejected**: one static TaskGroup per exam generated at parse time. The DAG processor
would have to read the catalog on every parse, and the exam list would freeze at parse
time. Branching operators: they choose a path; they don't isolate failures.

## A4. One writer at a time (FR-011)

**Decision**: An Airflow pool `pipeline_writer` with 1 slot. Every task that runs a
writing command (`catalog`, `discover`, `fetch`, `extract`, `key`, `translate`,
`snapshot`, `verify draw`) sets `pool="pipeline_writer"`. Read-only tasks (`exams`,
`candidates`, `export`, `verify status`) use the default pool. The `airflow-init`
service creates the pool with `airflow pools set pipeline_writer 1 ...`.

**Rationale**: The pipeline's Postgres advisory lock already guarantees one writer; the
pool makes Airflow queue writers instead of starting them only to hit the lock (exit 4).
So exit 4 happens only when the maintainer runs a writing command by hand during a run
(edge case), and the retry policy (A2) covers it. Fan-out therefore buys failure
isolation, not speed. That is honest: the per-host rate limit means fetch would not get
much faster anyway.

**Rejected**: `max_active_tis_per_dag=1` (it does not cover a second DAG, such as
verify) or `max_active_runs=1` (two DAGs can still overlap).

## A5. Request spacing across invocations (FR-012, SC-006)

**Finding**: The spec 001 CLI contract says the write lock keeps the per-host interval
"across invocations". That only holds while one process runs. `Fetcher` keeps each host's
last request time in memory, so when one command ends and the next starts, the first
request of the new process does not wait. This already happens by hand
(`pipeline discover` then `pipeline fetch` both hit the same exam's hosts, starting with
`robots.txt`). Per-exam tasks make back-to-back invocations the normal case.

**Decision** (pipeline change P3 in the plan): persist the last request time per host in
a small Postgres table, `host_requests (host text primary key, last_request_at
timestamptz not null)`.
- `Fetcher` reads it when it starts and waits out the remaining interval before a host's
  first request. It upserts the row after each request.
- It only runs inside commands that hold the write lock, so there are no concurrent
  writers.
- The stored time is wall-clock (`time.time()`). The in-process spacing keeps using the
  monotonic clock.
- Tests inject both clocks, so they stay deterministic (constitution III).
- The table holds hostnames and times only, no corpus text.

**Rationale**: It makes the existing contract true for every caller, by hand or from
Airflow. The DAG never has to know about hosts. It lives where the lock lives.

**Rejected**:
- A fixed cool-down at process start: it costs 5 s per host per command and still breaks
  if the interval grows.
- Running `fetch` once for all exams instead of per exam: that loses failure isolation
  (FR-005).
- Leaving it alone because no two exams share a host today: one exam's own consecutive
  stages already share hosts.

## A6. Pending candidates warning and summary (FR-007)

**Decision**:
- Add a read-only command, `pipeline candidates [--exam X]` (P2). It replays the ledger
  and prints one line per pending candidate, then a summary line:
  `candidate <source_id> exam=<exam_id> domain=<domain>` …
  `candidates: pending=<N>`. It takes no lock and opens no database connection.
- After `discover --exam X` exits 0, the discover task runs it and returns the pending
  IDs as XCom. If `pending>0`, it logs a WARNING with the count and the IDs.
- `pending_summary` (all_done) reads every branch's XCom and logs one WARNING line per
  exam with pending candidates, then a total. If a branch's discover failed, it logs
  that the exam's pending list is unknown. It never fails.

**Rationale**: The pipeline is the only authority on what "pending" means (ledger
replay), so the DAG must not replay the ledger itself (DRY). The command is also useful
by hand: the maintainer needs these IDs to run `pipeline review`. Output carries IDs,
exam IDs and domains only (FR-014).

A candidate is attributed to every exam whose catalog `domains` list its domain. The
ledger's `candidate` event does not record the edition named by `--add-url`, so the
domain is the one attribution rule that works for crawled and manual candidates alike.

**Rejected**:
- Parsing `discover`'s `candidates=+N`: it counts only new candidates, not every pending
  one.
- A distinct run state for pending candidates: rejected in clarification (Q3).

## A7. Snapshot event and export (FR-009, FR-010)

**Decision**:
- A task `announce_snapshot` follows `snapshot` and declares
  `outlets=[Asset("pipeline-snapshot")]`. It parses the snapshot summary line.
  - On `snapshot: published snap-NNNN` it sets
    `outlet_events[SNAPSHOT].extra = {"snapshot": "snap-NNNN"}` and succeeds, so an
    asset event is emitted.
  - On `snapshot: unchanged (snap-NNNN)` it raises `AirflowSkipException`; a skipped task
    emits no event, so export does not start.
- The export DAG uses `schedule=[SNAPSHOT]`. Its tasks:
  1. `resolve_snapshot` reads the version from `triggering_asset_events`, or from the
     `snapshot` param when triggered by hand. It fails if neither is present.
  2. `exportable_exams` runs `pipeline exams --snapshot V` (P1), which lists the exams
     not marked unverified in that snapshot.
  3. `export.expand(exam=…)` runs `pipeline export --exam X --eligible --snapshot V`.
- Export takes no lock, so it uses the default pool.

**Rationale**: An event fires only for a new version, as FR-009 requires. The exam filter
comes from the snapshot's own `exam_status`, the same data export checks, so the DAG
never exports an exam the pipeline would refuse. An exit 3 from export is then a genuine
integrity failure and fails the task. Passing the version in the event's `extra` ties the
export to the exact snapshot that triggered it.

**Rejected**:
- Treating export's exit 3 as "skip". It would hide other integrity refusals, such as an
  exam missing from `exam_status`.
- Reading `verify status` from the live database: it can change after the snapshot was
  taken.

## A8. Verify DAG (FR-008)

**Decision**: `exam_verify`, `schedule=None`, with params `exam` (required, from the
catalog list), `seed` (required integer) and `kind` (`handcheck` default, or
`translation` with `language`). The tasks are:
- `draw`: runs `pipeline verify draw …` in the writer pool.
- `status`: runs `pipeline verify status --exam X`.

`verify record` stays a command-line step, because it needs the hand-filled CSV.

**Rationale**: The hand-check is a human step (constitution: never automate approval or
hand-checks). The DAG only starts it and shows the status.

## A9. Image and project environment (FR-018, FR-019, FR-020)

**Decision**:
- **Base image**: `orchestration/Dockerfile`, `FROM apache/airflow:3.1.6-python3.13`.
  Airflow 3.1 supports Python 3.10–3.13 and PostgreSQL 13–17.
- **uv**: copied from the pinned `ghcr.io/astral-sh/uv:0.12.1` image, the version used
  on the dev Mac.
- **Project environment**: `UV_PROJECT_ENVIRONMENT=/opt/pipeline-venv`,
  `UV_PYTHON_DOWNLOADS=never`.
- **Dependencies at build time**: copy `pyproject.toml` and `uv.lock`, then
  `uv sync --locked --extra pipeline --no-install-project`.
- **Project at run time**: `uv run` installs only the mounted project into the venv, on
  first use, offline. uv locks the environment while it syncs.
- **Test image**: a second build target, `test`, adds pinned `pytest` and `ty` to
  Airflow's own environment. It is used by CI and locally for `tests/airflow`.
- **Single version pin**: the version lives only in the Dockerfile's `FROM` line. CI and
  local runs use the same image, so there is no separate constraints file to keep in sync.

**Rationale**: Airflow's packages never touch the project's lockfile, and the project's
packages never touch Airflow's environment. The host's `.venv` (macOS, under iCloud) is
mounted but ignored, because `UV_PROJECT_ENVIRONMENT` points elsewhere.

**Rejected**:
- Installing the pipeline with `pip` into Airflow's environment: it breaks FR-019 and
  risks dependency clashes with Airflow's constraints.
- `PythonVirtualenvOperator`: it builds a venv per task at run time and needs network
  access.

## A10. Stack layout and the shared store (FR-016, FR-018, Clarification Q1)

**Decision**: `orchestration/docker-compose.yaml` runs these services:

| Service | Role |
|---------|------|
| `postgres` | `postgres:17.6`. Owns the `airflow` database (role `airflow`) and the `pipeline` database (role `pipeline`), created by an init script. Port `127.0.0.1:5432:5432`, so only the Mac can reach it. |
| `airflow-init` | `airflow db migrate`, creates the pool, then exits |
| `airflow-apiserver` | UI and API on `127.0.0.1:8080`, SimpleAuthManager |
| `airflow-scheduler` | LocalExecutor; tasks run here |
| `airflow-dag-processor` | parses `orchestration/dags/` |

No triggerer, no Redis, no Celery, no flower.

- **Mounts**: the repository is bind-mounted at `/opt/project` into the scheduler and the
  DAG processor, and `AIRFLOW__CORE__DAGS_FOLDER=/opt/project/orchestration/dags`. Tasks
  run with `cwd=/opt/project`, so the default `PIPELINE_BLOB_ROOT` (`./.pipeline-data`)
  and the ledger are the same files the Mac sees.
- **DSN**: tasks use `PIPELINE_DSN=postgresql://pipeline:…@postgres:5432/pipeline`. The
  Mac uses the same DSN with `localhost`.
- **Role separation**: each role owns only its database, and `CONNECT` is revoked from
  `PUBLIC`. So Airflow cannot write pipeline tables even by mistake (FR-016).
- **Secrets**: passwords live in `orchestration/.env` (gitignored by the existing `.env`
  rule). `orchestration/.env.example` is committed with placeholders. Compose passes them
  to the containers as environment variables. Nothing goes into Airflow Variables or
  Connections (FR-015).
- **Start command**: `docker compose -f orchestration/docker-compose.yaml up -d`
  (FR-018).

**Rationale**: This is the official compose file cut down to LocalExecutor, with one
Postgres server holding both databases (Q1). The 5432 port is bound to loopback only.
There is no local Postgres on the Mac to clash with.

**Risks noted**:
- The repository lives in iCloud Drive. A bind mount of an evicted file ("Optimize Mac
  Storage") reads as missing, so the quickstart asks to keep the folder downloaded.
- Containers write the ledger while iCloud syncs; a conflict copy
  (`ledger 2.jsonl`) is possible if the file is edited on two devices. The existing
  ledger test catches a ledger that is not a clean append.

## A11. Testing without a scheduler (FR-022, SC-001, SC-011)

**Decision**: The Airflow tests live in `tests/airflow/` and run only in the `airflow` CI
job, inside the `test` image. The core job adds `--ignore=tests/airflow`. Without Airflow
installed they skip locally, and under `CI=true` they fail, mirroring `tests/pipeline`.
They use an in-process `FakeRunner` in place of the subprocess, which records argv and
returns scripted exit codes and stdout. The test container runs with `--network none`, so
a test that reaches the network fails instead of passing by luck.

1. **DAG integrity**: the DagBag loads all three DAGs with 0 import errors (SC-003). It
   also checks:
   - no schedule, except export's asset schedule, and `catchup=False`;
   - every writing task is in `pipeline_writer`, and every read-only task is not;
   - trigger rules (`snapshot` all_success, `pending_summary` all_done);
   - only Task SDK imports (`airflow.sdk`), plus the three outcome exceptions from
     `airflow.exceptions`, checked statically (FR-023). Checked against the
     `apache-airflow-task-sdk` 1.1.6 wheel that Airflow 3.1.6 pins: its `airflow.sdk`
     namespace has no `AirflowFailException`, `AirflowSkipException` or
     `AirflowException`. Newer SDK releases add them, so when Airflow is upgraded the
     import can move to `airflow.sdk.exceptions` in `stage.py` and the scan.
2. **Exit codes**: `run_stage` with each exit code gives the outcome in A2 (SC-004).
3. **Acquisition run**: `dag.test()` with a FakeRunner on a temporary SQLite metadata
   database checks four cases:
   - the argv sequence is `exams`, `catalog`, then per exam `discover --exam X`,
     `candidates --exam X`, `fetch --exam X`;
   - one exam's failure leaves the others complete and the snapshot skipped;
   - an unknown exam fails before `catalog`;
   - pending candidates produce the summary warning (SC-007, SC-014).
4. **Secrets**: a sentinel `PIPELINE_DSN` never appears in the captured logs or XComs
   (SC-009).

Both halves read one fixture file of argv lists,
`tests/fixtures/airflow_acquisition_argv.json`. The Airflow half asserts that the DAG
issues exactly that sequence. The pipeline half (`tests/pipeline`, in-process,
MockTransport, Postgres) runs exactly that sequence through `pipeline.__main__.main`. It
then checks that the result has the same ledger events and table rows as each stage run
once for all exams. Event order and the `at` audit timestamps are not compared. Together,
the two halves give SC-001 and SC-011 without installing the pipeline into Airflow's
environment.

**Spec correction (applied to spec.md)**: SC-001 asked for byte-identical ledgers. That
cannot hold between two executions:
- every ledger line carries a wall-clock `at`;
- the pool does not fix the order in which exams run.

SC-001 and SC-011 now say what the two halves actually check.

**Resume (SC-013)**: resuming means Airflow's "Clear → only failed, with downstream".
Airflow re-runs those task instances and leaves the succeeded ones alone. The tests
check what makes this safe:
- each exam is its own mapped instance;
- `snapshot` depends on all branches;
- every stage is idempotent, and fetch sends conditional requests (already tested in
  `tests/pipeline/test_fetch.py`).

An in-process clear-and-resume test was attempted with `dag.test()` and **does not
work in Airflow 3.1.6**: `DAG.test` clears every task instance of its logical date before
it runs, so a second call re-runs the whole run, `exams` and `catalog` included. That
test is kept as `xfail(strict=True, raises=AssertionError)` in `TestResume`, so it will
flag itself when a later Airflow can resume. What is tested instead:
- after a failed run, exactly the failed `fetch` instance and `pending_summary` are in
  the failed state;
- clearing those with `clear_task_instances` leaves `select_exams`, `catalog`, every
  `discover`, and the other exam's `fetch` in `success`.

Re-running the cleared tasks is checked by hand in quickstart Q6, and the README says so.

**Rejected**:
- A live scheduler in CI, which FR-022 forbids.
- Installing the pipeline into the Airflow test image to run it in-process: that is the
  same clash as A9.

## A12. Constitution amendment A2 (FR-021)

**Decision**: An amendment PR, landing before any DAG file, bumps the constitution
1.1.0 → 1.2.0 (MINOR: materially expanded guidance; no principle or gate is removed).

- **Project Constraints → Dependencies** adds:
  > "Workflow orchestration (Apache Airflow) runs only in its own environment, the
  > official Airflow Docker image extended in `orchestration/`. It is never added to
  > `pyproject.toml` or `uv.lock`, and `harness/` and `pipeline/` never import it."
- **Principle I** adds `orchestration/` to its path list. ty checks it, and
  `tests/airflow/`, in the Airflow CI job, where Airflow is importable. The project-wide
  ty hook excludes both paths.
- **Principle III** adds:
  > "Workflow tests run without a scheduler, in their own CI job."
- **Quality Gates** adds:
  > "CI MUST run the `airflow` job: build the test image, run `tests/airflow`, and ty on
  > `orchestration/`. pre-commit runs ty on `orchestration/` and `tests/airflow/` inside
  > the test image whenever those paths change, so the local gate equals the CI gate."

  CI's `lint` job runs `pre-commit run --all-files` but never builds the test image. It
  therefore sets `SKIP=ty-airflow`, and the `airflow` job runs the same ty in the same
  image. One check, one tool version, run once in CI.
- **Sync Impact Report**: updated in the same PR.
- **CLAUDE.md**: gains one line under Environment constraints:
  > "Airflow is optional and lives in `orchestration/`. Like vLLM, it is a separate
  > environment and never a harness dependency."

**Rationale**: The governance rule says that a new gate lands with the tooling that
enforces it. So the amendment PR also adds the CI job skeleton and the ty exclusion.

---

# Amendment A3: Dagster replaces Airflow (2026-11-18)

Airflow 3.1.6 was never merged. Before it was, the orchestrator was changed to Dagster. The
decisions below replace A1, A3 and A5–A12 where they conflict; A2 and A4 stand.

## D1. Dagster, replacing Airflow

**Decision**: Dagster 1.13.x (pinned in `pyproject.toml`), run with `dagster dev` on the Mac
and as a compose stack (Postgres, init, web server, daemon).

**Rationale**: The work is "run these stages for these exams in order, one writer at a
time". Dagster expresses that with partitions, a pool and retry policies, with no second
image, no constraint file, and no scheduler/dag-processor pair. Airflow's strengths (time
schedules, a large operator catalogue) are unused: runs are manual.

**Alternatives**: keep Airflow (rejected by the maintainer, who chose to replace it); run
both side by side (rejected: two orchestrators to keep correct).

## D2. Assets partitioned by exam, not a DAG with a mapped branch

**Decision**: `catalog` (unpartitioned), `discovered[exam]` and `fetched[exam]`
(`StaticPartitionsDefinition`). The partitions are read from `pipeline exams` when the
definitions load.

**Rationale**: "Per exam" is a partition, and a failed partition does not stop the others.
`select_exams` disappears: an unknown exam is not a partition, so FR-026 holds by
construction. `pending_summary` and its `all_done` trigger rule disappear too: a backfill
reports a status per partition, and pending candidates are the WARN check `pending_review`.

**No sensors and no schedules.** Runs are manual, so nothing needs to watch the catalog. After
editing `catalog.yaml` the maintainer uses **Reload definitions**. A broken catalog fails the
code location load with the `pipeline exams` message, which is where it should be seen.

## D3. One uv environment

**Decision**: Dagster is the optional `orchestration` extra of the root project, in the one
`uv.lock`. A core, `model` or `pipeline` install pulls none of it, and `tests/pipeline/test_guard.py`
checks that `harness/` and `pipeline/` never import it.

**Rationale**: The second environment existed only because Airflow ships its own image and
constraint files and conflicts with the pipeline's dependencies. Dagster is a normal pip
package. This also removes the image-only test and type-check gate (amendment A2): pytest and
ty run on the host.

## D4. Keep the subprocess boundary

**Decision**: `stage.py` runs `<python> -m pipeline <argv>`; it does not import `pipeline`.

**Rationale**: The CLI is the contract. The write lock, the exit codes and "a step runs
exactly what a maintainer types" all live there, and the equivalence test
(`tests/pipeline/test_equivalence.py`) compares the assets' commands with a hand run. An import
would bypass all three. Same interpreter, so no second environment is needed.

## D5. Pool, retries and failure semantics

- **Writer**: pool `pipeline_writer`, `concurrency.pools.default_limit: 1` in both
  `dagster.yaml` files, enforced per step (the default granularity). Replaces A4's Airflow pool.
- **Retries**: `RetryPolicy(max_retries=2, delay=180)`. Exit 1 and 4 raise
  `RetryableStageError`; exits 2, 3 and others raise `Failure(allow_retries=False)`.
- **Resume (FR-025)**: Re-execute from failure, or a backfill over the failed partitions.
- **Check as part of the asset step**: `pending_review` is declared with `check_specs` and
  yielded by `discovered`, rather than as a separate `@asset_check`, so it reads the pending
  ids already parsed in that step instead of running `pipeline candidates` a second time.

## D6. Constitution amendment A3 (replaces A12)

1.1.0 → 1.3.0 (A2's 1.2.0 was drafted for Airflow and never ratified). Principle I lists
`orchestration/` and ty checks it in the project environment; Principle III runs
orchestration tests without a scheduler or daemon in their own CI job; Quality Gates runs the
`dagster` CI job; Dependencies names the `orchestration` extra; CLAUDE.md's Environment
constraints line is reworded.

## D7. `catalog` is not upstream of the exam assets

First real run (2026-11-19): `catalog` exits 3 on a new catalog (key evidence must cite an
approved source; editions missing), and as the upstream of `discovered` it blocked
discovery. Reading the CLI shows only the `catalog` stage calls `validate_catalog`;
`discover` and `fetch` use `load_catalog` and work from the ledger and blob store, so they
need a catalog that parses, not a complete one. A complete catalog is reached *through*
discover, review and fetch, so the dependency was a deadlock. Decision: drop it. `catalog`
stays an asset with its own job `validate_catalog`, and `exam_acquisition` covers
`discovered` and `fetched`. The supersession of the Airflow rule "an invalid catalog
stops everything" applies only to the validation step itself.
