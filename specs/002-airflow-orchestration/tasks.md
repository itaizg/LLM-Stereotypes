---

description: "Task list for orchestrating the exam pipeline with Apache Airflow 3"
---

# Tasks: Orchestrate the Exam Pipeline with Apache Airflow 3

> **Superseded in part (amendment A3, 2026-11-18).** The orchestrator is Dagster, not
> Airflow: assets partitioned by exam, one uv environment (the `orchestration` extra), no
> sensors or schedules. Read [research.md](research.md) D1–D5 for the decision record and
> [contracts/assets.md](contracts/assets.md) and [contracts/stack.md](contracts/stack.md)
> for the current contracts. Mapping: DAG → asset group; `select_exams` → static
> partitions; `pipeline_writer` pool → Dagster pool; `pending_summary` → `pending_review`
> check; Clear failed → Re-execute from failure. The text below is kept as the original
> record, and where it names Airflow concepts the mapping above applies.

**Input**: Design documents from `specs/002-airflow-orchestration/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Required. Constitution Principle III says every behaviour change ships with a
test that fails without it. Write each test task first and confirm it fails before the
implementation task that follows it.

**Organization**: Tasks are grouped by user story, so each story can be built and tested
on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task)
- **[Story]**: the user story the task belongs to (US1–US4)
- Paths are relative to the repository root. The layout is in plan.md → Project Structure.

## Conventions every task follows

- **Gates**: `uvx ruff@0.16.4 check . && uvx ruff@0.16.4 format --check .`, then ty, then
  pytest.
  - `pipeline/` and `tests/pipeline/` use the project ty:
    `uv run --locked --extra dev --extra pipeline ty check`.
  - `orchestration/` and `tests/airflow/` use ty inside the test image:
    `docker run --rm --network none -v "$PWD":/opt/project -w /opt/project exam-airflow-test ty check orchestration tests/airflow`.
- **Size**: every file stays under 800 lines (CDR-2026-022).
- **Docstrings** explain why, not what (CLAUDE.md).
- **Running the CLI locally**: use `.venv/bin/python -m pipeline …` from the repository
  root. iCloud hides `.pth` files, so the `pipeline` console script fails on the Mac.
  Inside containers, use `uv run … pipeline`.
- **Database tests** need `PIPELINE_TEST_DSN`. Locally, start pgserver in the session
  scratchpad (see memory: dev-environment-quirks).
- **Commits**: small, imperative messages ending with
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Commit or push only when the
  maintainer asks.

---

## Phase 1: Setup

**Purpose**: Put the feature on its own branch.

- [X] T001 Create branch `002-airflow-orchestration` from `main`, after spec 001's acquisition PR ([PR #5](https://github.com/itaizg/LLM-Stereotypes/pull/5)) merges, or else from `feat/data-aggregation-pipeline`. Move the untracked `specs/002-airflow-orchestration/` and the `TODO.md` "Later: Airflow orchestration" section onto it. Update the **Feature Branch** line in `specs/002-airflow-orchestration/spec.md` and `specs/002-airflow-orchestration/plan.md`.

---

## Phase 2: Foundational (blocks every user story)

**Purpose**:
- the constitution amendment (FR-021);
- the image every Airflow test runs in;
- the one helper that every DAG task uses to run a stage.

**⚠️ The first workflow file, `acquisition.py` (T021), must not be written before PR 1
(T001–T010) merges (FR-021).** `stage.py` (T010) is a helper module, not a workflow. It
lands with the amendment, because the new CI job needs it to pass (governance: a gate
lands with its tooling).

### Amendment A2 (research A12)

- [X] T002 Amend `.specify/memory/constitution.md` 1.1.0 → 1.2.0, using the text in research.md A12:
  - Dependencies: Airflow only in its own image environment, never in `pyproject.toml` or `uv.lock`, never imported by `harness/` or `pipeline/`.
  - Principle I: add `orchestration/`, checked in the Airflow job.
  - Principle III: workflow tests run without a scheduler, in their own CI job.
  - Quality Gates: the `airflow` CI job; and pre-commit runs ty on `orchestration/` and `tests/airflow/` inside the test image when those paths change, so the local gate equals the CI gate.

  Update the Sync Impact Report, and set **Last Amended** to the merge date.
- [X] T003 [P] Add one line to `CLAUDE.md` under "Environment constraints": Airflow is optional and lives in `orchestration/`; like vLLM, it is a separate environment and never a harness dependency.
- [X] T004 [P] In `pyproject.toml`, add `"orchestration/"` and `"tests/airflow/"` to `[tool.ty.src] exclude`, with a comment pointing at amendment A2, and `extra-paths = ["orchestration/dags"]` to `[tool.ty.environment]` so `import stage` resolves. This keeps the project-wide ty hook in `.pre-commit-config.yaml` green without Airflow installed. Also add a `local` hook `ty-airflow` to `.pre-commit-config.yaml`:
  - `entry: bash -c '<the docker run command above>'`, because pre-commit runs `entry` without a shell and `$PWD` would not expand
  - `language: system`
  - `files: ^(orchestration|tests/airflow)/`
  - `pass_filenames: false`

  The hook runs only when those paths change, so harness and pipeline contributors need no Docker. It runs the same ty in the same image as the CI `airflow` job, so the local gate equals the CI gate.
- [X] T005 In `.github/workflows/ci.yml`:
  - change the core `pytest` job to `pytest -q --ignore=tests/pipeline --ignore=tests/airflow`;
  - set `SKIP: ty-airflow` as an `env` entry on the `lint` job's pre-commit step, with a comment: that job never builds `exam-airflow-test`, and the `airflow` job below runs the same ty in the same image, so the gates still match;
  - add an `airflow` job (`contents: read`, pinned `actions/checkout`, no secrets) that runs:
    1. `docker build -f orchestration/Dockerfile --target test -t exam-airflow-test .`;
    2. `docker run --rm --network none -e CI=true -v "$PWD":/opt/project -w /opt/project exam-airflow-test pytest tests/airflow -q`;
    3. the same image, also with `--network none`, running `ty check orchestration tests/airflow`.

  Only the build step has network access (for `pip` and `uv`).

  The job fails until T006–T010 exist; the amendment PR lands them together, per governance.

### Image and test harness

- [X] T006 Create `orchestration/Dockerfile` (contracts/stack.md → Image):
  - **Target `runtime`**:
    - `FROM apache/airflow:3.1.6-python3.13`;
    - `COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /bin/uv`;
    - `ENV UV_PROJECT_ENVIRONMENT=/opt/pipeline-venv UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy`;
    - create `/opt/pipeline-venv` owned by `airflow`;
    - copy `pyproject.toml` and `uv.lock` to a build folder;
    - `uv sync --locked --extra pipeline --no-install-project`.
  - **Target `test`**: `FROM runtime`, then `pip install` pinned `pytest==9.1.1` and the ty version used by the pre-commit hook into Airflow's environment, together with `apache-airflow==${AIRFLOW_VERSION}` so pip cannot move Airflow. Default command: `pytest tests/airflow -q`.
- [X] T007 [P] Create `.dockerignore` at the repository root. Exclude `.git`, `.venv`, `.pipeline-data`, `exports`, `results`, `legacy`, `data/pdfs`, `orchestration/logs`, `**/__pycache__`, `.ruff_cache`, `.pytest_cache`, and `*.egg-info`. The build needs only `pyproject.toml`, `uv.lock` and `orchestration/`.
- [X] T008 [P] Create `tests/airflow/conftest.py` and `tests/airflow/fakes.py`. There is no `tests/airflow/__init__.py`: with pytest's default import mode it would turn this directory into a package named `airflow` that shadows the real one. `FakeRunner` lives in `fakes.py`, because `from conftest import …` is ambiguous with `tests/conftest.py`:
  - **Collection guard**: skip the whole directory when the real `airflow` package is not importable (test `airflow.exceptions`, since `tests/` on `sys.path` makes a bare `airflow` look present); fail instead when `CI=true`, mirroring `tests/pipeline/conftest.py`.
  - **Session fixture**: `AIRFLOW_HOME` in a temporary directory, with SQLite `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN` and `AIRFLOW__CORE__LOAD_EXAMPLES=false`. It runs the metadata migration once and creates the pool `pipeline_writer` with 1 slot.
  - **`FakeRunner` dataclass**: records each argv and returns scripted `(exit_code, stdout, stderr)` per argv prefix.
  - **`fake_runner` fixture**: monkeypatches `stage.RUNNER`.
  - **`dagbag` fixture**: loads `orchestration/dags`.

### Stage invocation (research A1/A2, contracts/dags.md → Common behaviour)

- [X] T009 [P] Write `tests/airflow/test_stage.py`, parametrized over the exit codes:
  - `run_stage` returns a `StageResult` on 0;
  - raises `AirflowException` (retryable) on 1 and 4;
  - raises `AirflowFailException` on 2, 3 and 99 (SC-004);
  - logs `pipeline <argv>`, each stdout line at INFO, each stderr line at WARNING, and `exit=<code>`;
  - never logs any environment value: set a sentinel `PIPELINE_DSN` and assert it appears in no captured log record (SC-009);
  - builds the command `uv run --project /opt/project --locked --offline --extra pipeline pipeline <argv>` with `cwd=/opt/project`.
- [X] T010 Create `orchestration/dags/__init__.py` (empty) and `orchestration/dags/stage.py`:
  - `RETRIES = 2` and `RETRY_DELAY = timedelta(minutes=3)`, defined once;
  - `DEFAULT_ARGS`, the `PIPELINE_WRITER = "pipeline_writer"` pool name, and `PROJECT = "/opt/project"`;
  - `Runner` protocol, with the real `RUNNER` built on `subprocess.run(…, capture_output=True, text=True)`;
  - `StageResult`/`DiscoverResult` `TypedDict`s (data-model.md);
  - `run_stage(argv, *, exam=None) -> StageResult`, mapping exit codes per research A2;
  - `summary_line(stdout, prefix)`, which picks the stage's summary line.

  Imports come only from `airflow.sdk`, except `AirflowException`, `AirflowFailException`
  and `AirflowSkipException`, which Airflow 3.1.6 exposes only from `airflow.exceptions`
  (contracts/dags.md → Common behaviour). T009 must pass.

**Checkpoint**: the amendment is merged, the test image builds, and `run_stage` is
tested, so the user stories can start.

---

## Phase 3: User Story 1 — Run acquisition for all exams in one trigger (Priority: P1) 🎯 MVP

**Goal**: One trigger runs `catalog` once, then `discover → fetch` per selected exam, and
an always-run summary of pending candidates. The run stays green when candidates are
pending.

**Independent Test**:
- `tests/airflow/test_acquisition.py` runs the DAG with `dag.test()` and a `FakeRunner`,
  and asserts the exact argv sequence.
- `tests/pipeline/test_equivalence.py` shows that this argv sequence equals one all-exam
  hand run: the same ledger events (ignoring order and `at`) and equal row counts.

### Pipeline additions P1 and P2 (contracts/pipeline-cli-additions.md)

- [X] T011 [P] [US1] Write `tests/pipeline/test_cli_listing.py`, class `TestExams`:
  - `main(["exams"])` prints sorted exam ids, then `exams: count=N`, opens no database connection, and takes no lock;
  - `--exam` or `--edition` exits 2;
  - an unloadable catalog exits 3.

  Use the `dataset` fixture's catalog.
- [X] T012 [P] [US1] In `tests/pipeline/test_cli_listing.py`, class `TestCandidates`:
  - `main(["candidates"])` prints `candidate <id> exam=<exam> domain=<domain>` for every pending source, sorted by `(exam, source_id)`, then `candidates: pending=N`;
  - reviewed sources are excluded;
  - a candidate whose domain is listed by two exams appears under both;
  - `--exam X` filters to X;
  - an unknown exam exits 2;
  - no URL path appears in the output.
- [X] T013 [US1] Implement `pending_candidates(catalog, ledger_path, exam=None) -> list[tuple[str, str, str]]` in `pipeline/acquire.py`. It replays the ledger with `replay_ledger`, keeps sources whose `decision == "pending"`, and attributes each to every exam whose `domains` list its domain. It has no I/O beyond reading the ledger.
- [X] T014 [US1] Add the read-only commands `exams` (P1, plain form only) and `candidates` (P2) to `pipeline/__main__.py`. Neither takes `writing()` or calls `connect()`. Unknown exams raise `UsageError`. T011–T012 must pass.
- [X] T015 [P] [US1] Mirror P1 (plain form) and P2 into `specs/001-exam-data-aggregation/contracts/cli.md`, in the Commands table and the Output section, so that contract stays the single authority.
- [X] T016 [P] [US1] Create `tests/fixtures/airflow_acquisition_argv.json`: the argv lists the acquisition DAG issues after `exams` for the two exams of the `dataset` fixture (`catalog`, then per exam `discover --exam X`, `candidates --exam X`, `fetch --exam X`). It is the single contract between the two halves of SC-001/SC-011. Then write `tests/pipeline/test_equivalence.py` (SC-001, SC-011), asserting first that the file's exam ids equal the dataset's.

  Set up the shared environment:
  - monkeypatch `pipeline.web.new_client` to return a client on the `web` fixture's transport;
  - monkeypatch `pipeline.web.SystemClock` to return the `clock` fixture;
  - set `PIPELINE_DSN` to `schema_dsn`;
  - pass `--catalog` and `--ledger` from the dataset.

  Compare two runs on fresh ledgers and schemas:
  - (a) run each argv in the file through `pipeline.__main__.main`;
  - (b) run `main(["catalog"])`, `main(["discover"])` and `main(["fetch"])` once, with no `--exam`.

  Assert:
  - every call exits 0;
  - equal multisets of ledger events, with `at` and `seq` removed;
  - equal row counts in every table.

  Then repeat (a) twice more and assert 0 new ledger lines each time, and 0 row changes on the third run (SC-002). The tables trail the ledger by one run, because `catalog` runs before `discover` and the second `catalog` records what the first `discover` found.

### Acquisition DAG (contracts/dags.md → exam_acquisition)

- [X] T017 [P] [US1] Write `tests/airflow/test_dag_integrity.py`:
  - the DagBag has 0 import errors (SC-003), and `exam_acquisition` exists;
  - `schedule is None`, `catchup is False`, and `default_args` retries and delay equal `stage.RETRIES` and `stage.RETRY_DELAY`;
  - every task whose argv starts with a writing command uses pool `pipeline_writer`, and `select_exams`/`pending_summary` do not;
  - `pending_summary.trigger_rule == "all_done"`;
  - a static scan of `orchestration/dags/*.py` finds only `airflow.sdk` imports and `from airflow.exceptions import` of the three outcome exceptions (FR-023);
  - the same scan rejects any reference to `Variable` or `Connection` in `orchestration/dags/*.py`, whether imported or used by name (FR-015);
  - and rejects notification settings (`email`, `*_callback`, `sla`; FR-027), the string `git` (FR-017), and any process-starting import outside `stage.py`.
- [X] T018 [US1] Write `tests/airflow/test_acquisition.py`, class `TestHappyPath` (depends on T016's fixture file). With `dag.test()`, a `FakeRunner` whose `exams` output lists the two exam ids from `tests/fixtures/airflow_acquisition_argv.json`, and exit 0 everywhere, the argv sequence is:
  1. `exams`;
  2. `catalog`;
  3. per exam: `discover --exam X`, `candidates --exam X`, `fetch --exam X`.

  The recorded commands must match `tests/fixtures/airflow_acquisition_argv.json`: `exams` then `catalog` first, the same multiset of commands, and the same order *within each exam*. Which exam runs first is not fixed, because the pool serialises exams without ordering them. The run state is success. Conf naming only the second exam runs only that exam's branch (SC-014).
- [X] T019 [P] [US1] In `tests/airflow/test_acquisition.py`, class `TestGuards`:
  - conf `{"exams": ["nope"]}` fails `select_exams` with a message naming `nope`, and the runner never sees `catalog` (FR-026, SC-014);
  - a scripted `catalog` exit 3 means no `discover` argv is recorded and the run fails (US1 scenario 3).
- [X] T020 [P] [US1] In `tests/airflow/test_acquisition.py`, class `TestPendingCandidates`. When `candidates --exam a` prints two candidate lines:
  - the discover task logs a WARNING with count 2 and both IDs;
  - its XCom `pending` holds both IDs;
  - `pending_summary` logs `pending candidates for a: 2 (…)` and a total line;
  - the run state is still success (FR-007, SC-007).
- [X] T021 [US1] Create `orchestration/dags/acquisition.py`:
  - **DAG**: `@dag(dag_id="exam_acquisition", schedule=None, catchup=False, start_date=2026-11-01 UTC, default_args=stage.DEFAULT_ARGS, tags=["exam-pipeline"])`, with `params={"exams": Param([], type="array", items={"type": "string"})}`.
  - **`select_exams`**: runs `exams`, validates the param against the output, and returns the sorted selection.
  - **`catalog`**: in the writer pool.
  - **`@task_group exam_branch(exam)`**:
    - `discover`: `discover --exam X`, then `candidates --exam X`; returns `DiscoverResult` and warns when pending > 0;
    - `fetch`: `fetch --exam X`;
    - both in the writer pool;
    - expanded with `exam_branch.expand(exam=selected)`.
  - **`pending_summary`**: `trigger_rule="all_done"`; pulls the XComs of the other tasks and logs the summary. Pending candidates never fail it, but it fails last when a task left no result, because a leaf on `all_done` would otherwise make a failed run green.
  - **Stage list**: one tuple constant, so a stage from spec 001 is one line to add (FR-004).

  T017–T020 must pass.
- [X] T022 [US1] Run `tests/airflow/` in the test image and `tests/pipeline/test_cli_listing.py` plus `tests/pipeline/test_equivalence.py` locally. All must pass. Fix any failure before continuing.

**Checkpoint**: US1 works end to end in tests. It is the MVP and can be demonstrated once
US3's stack exists, or with `dag.test()`.

---

## Phase 4: User Story 2 — Failures and concurrency are handled safely (Priority: P2)

**Goal**:
- one exam's failure doesn't stop the others and skips the snapshot gate;
- writers never overlap;
- requests to one host stay ≥ 5 s apart across commands;
- a failed run can be resumed.

**Independent Test**:
- `tests/airflow/test_acquisition.py::TestFailures` covers isolation and retries.
- `tests/pipeline/test_web.py::TestSpacingAcrossInvocations` uses two `Fetcher` lifetimes sharing a schema and fake clocks.

### Pipeline addition P3 (research A5, data-model.md → host_requests)

- [X] T023 [P] [US2] Write `tests/pipeline/test_web.py`, class `TestSpacingAcrossInvocations`, against the `db` fixture. Two sequential `Fetcher` instances share the same schema, a `FakeClock` and an injected fake wall clock:
  - the second instance's first request to a host the first instance just used waits out the rest of the interval (≥ 5 s since the last request, SC-006);
  - a host last used more than one interval ago is not delayed;
  - a `last_request_at` in the future is treated as now;
  - `robots.txt` requests and retried requests also update the row.
- [X] T024 [US2] Add `host_requests (host text PRIMARY KEY, last_request_at timestamptz NOT NULL)` to `pipeline/schema.sql`, with a column comment saying it is operational state: not rebuilt by `pipeline rebuild` and not in snapshots.
- [X] T025 [US2] In `pipeline/store.py`, add:
  - `load_host_requests(conn) -> dict[str, float]` (epoch seconds);
  - `record_host_request(conn, host, at_epoch) -> None` (upsert, with a parameterized query).
- [X] T026 [US2] Change `Fetcher` in `pipeline/web.py`:
  - accept an optional `HostLedger` protocol (`last(host) -> float | None`, `record(host, at) -> None`) and a `wall: Callable[[], float]` (default `time.time`);
  - before the first request to a host in a process, wait `max(0, interval − (wall() − last))`;
  - record after every request, including robots and retries.

  The in-process spacing stays on the monotonic `Clock`.
- [X] T027 [US2] Wire a Postgres-backed `HostLedger` into `discover` and `fetch` in `pipeline/__main__.py`, using the connection already held by `writing()`. T023 must pass, and all existing `tests/pipeline/test_web.py` and `tests/pipeline/test_fetch.py` tests must stay green.
- [X] T028 [P] [US2] Update the fetch row and the intro paragraph in `specs/001-exam-data-aggregation/contracts/cli.md` (the interval now holds across invocations, via `host_requests`). Add the table to `specs/001-exam-data-aggregation/data-model.md`.

### Orchestration behaviour

- [X] T029 [P] [US2] Write `tests/airflow/test_acquisition.py`, class `TestFailures`:
  - with `fetch --exam a` scripted to exit 3, exam `b`'s branch still completes, `a`'s fetch is failed with exactly 1 try, and `pending_summary` still runs (US2 scenario 1, FR-005);
  - with `fetch --exam a` scripted to exit 1 three times, the task ends failed after 3 tries;
  - with exit 4 on the first try and 0 on the second, the task ends success (research A2). Use `retry_delay=0` through a test override of `stage.RETRY_DELAY`.
- [X] T030 [P] [US2] In `tests/airflow/test_dag_integrity.py`, assert that the pool `pipeline_writer` is the only pool used by writing tasks across all loaded DAGs, and that its slot count is 1 in the test metadata database (SC-005, structural).
- [X] T031 [US2] Write `tests/airflow/test_acquisition.py`, class `TestResume` (SC-013):
  1. run once with exam `a`'s fetch failing;
  2. clear only the failed task instances with downstream;
  3. re-run the same run with the runner now returning 0.

  Assert that the runner sees only `fetch --exam a` (and later stages) the second time. If Airflow 3.1.6's `dag.test()` cannot resume an existing run, mark the test `xfail(strict=True)` with the reason, and record in `specs/002-airflow-orchestration/research.md` A11 that resume is checked by quickstart Q6.
- [X] T032 [US2] Run `tests/airflow/` in the test image and `tests/pipeline/` locally. All must pass.

**Checkpoint**: US1 and US2 pass together. Writers are serialised, and the request gap
holds across commands.

---

## Phase 5: User Story 3 — Start the orchestrator locally with one command (Priority: P3)

**Goal**:
- `docker compose -f orchestration/docker-compose.yaml up -d --build` starts Airflow with
  every workflow loaded;
- one Postgres server holds two separate databases, and only loopback ports are open;
- secrets come from `orchestration/.env`;
- the Mac and the tasks share the store.

**Independent Test**: quickstart Q2, Q3 and Q8, and
`tests/pipeline/test_guard.py::TestAirflowIsolation`.

- [X] T033 [P] [US3] Write `tests/pipeline/test_guard.py`, class `TestAirflowIsolation` (SC-008, FR-002, FR-020):
  - no `*.py` under `harness/` or `pipeline/` imports `airflow`;
  - `uv.lock` contains no package named `apache-airflow*`;
  - `pyproject.toml` lists no Airflow package in any extra.
- [X] T034 [P] [US3] In `tests/pipeline/test_guard.py`, class `TestGuardPaths` (P4). `python -m pipeline.corpus <dir>`:
  - flags an untracked file under `<dir>` that contains a fingerprinted string;
  - ignores binary-suffix and allowlist rules for untracked files;
  - exits 3 on a hit;
  - behaves as before with no arguments.
- [X] T035 [US3] Implement P4 in `pipeline/corpus.py`: `main(argv)` accepts optional paths, and `scan(repo_root, extra_paths=())` also walks regular files under each path for `corpus_text` hits only. T034 must pass.
- [X] T036 [P] [US3] Mirror P4 into `specs/001-exam-data-aggregation/contracts/cli.md`, in the paragraph about `python -m pipeline.corpus`.
- [X] T037 [P] [US3] Create `orchestration/postgres-init/10-pipeline-db.sh` (executable, `set -euo pipefail`). It creates the role `pipeline` with `$PIPELINE_DB_PASSWORD` and the database `pipeline` owned by it, and revokes `CONNECT` on `pipeline` and `airflow` from `PUBLIC`. Pass the password as a `psql` variable, never by string-building SQL (CDR-2026-021).
- [X] T038 [P] [US3] Create `orchestration/.env.example` with `AIRFLOW_UID=`, `AIRFLOW_DB_PASSWORD=change-me`, `PIPELINE_DB_PASSWORD=change-me` and a commented-out `# ANTHROPIC_API_KEY=`. Confirm that the existing `.env` rule in `.gitignore` covers `orchestration/.env`, and add `orchestration/logs/` to `.gitignore`.
- [X] T039 [US3] Create `orchestration/docker-compose.yaml` per contracts/stack.md:
  - **Services**:
    - `postgres` (`postgres:17.6`, `127.0.0.1:5432:5432`, volume `pgdata`, init dir mounted);
    - `airflow-init` (`airflow db migrate` and `airflow pools set pipeline_writer 1 "one pipeline writer at a time"`);
    - `airflow-apiserver` (`127.0.0.1:8080`);
    - `airflow-scheduler`;
    - `airflow-dag-processor`.
  - **Build**: `context: ..`, `dockerfile: orchestration/Dockerfile`, `target: runtime`.
  - **Shared Airflow env**:
    - `LocalExecutor`;
    - the `airflow` DB URL from `${AIRFLOW_DB_PASSWORD}`;
    - `AIRFLOW__CORE__DAGS_FOLDER=/opt/project/orchestration/dags`;
    - `LOAD_EXAMPLES=false`;
    - `DAGS_ARE_PAUSED_AT_CREATION=false`.
  - **Mounts**: the repository at `/opt/project` on init, scheduler and dag-processor; logs at `./logs`.
  - **Scheduler**: `PIPELINE_DSN` built from `${PIPELINE_DB_PASSWORD}`, plus an optional `ANTHROPIC_API_KEY`.
  - **Not included**: no Redis, Celery, flower or triggerer.
  - **Health and order**: healthchecks, and `depends_on: service_completed_successfully` for init.
- [X] T040 [US3] Add a CI step to the `airflow` job in `.github/workflows/ci.yml`: `docker compose -f orchestration/docker-compose.yaml config -q`, with dummy env values from `orchestration/.env.example`, so a broken compose file fails CI without starting the stack.
- [ ] T041 [US3] Manual check, done by the maintainer and recorded in the PR description: run quickstart Q2, Q3 and Q8 on the Mac. These cover:
  - one command starts the stack;
  - `airflow dags list-import-errors` prints none (SC-010);
  - `psql` as `airflow` cannot connect to `pipeline` (US3 scenario 2);
  - the sentinel password and the corpus guard find nothing in `orchestration/logs/` (SC-009);
  - `pipeline review` on the Mac followed by a `{"exams": [...]}` run fetches the approved source (US3 scenario 4);
  - the two-run Gantt check in quickstart Q4 shows no overlapping writing tasks and no `locked:` log (SC-005).

  Do not claim this passed until it has run.

**Checkpoint**: US1–US3 are usable on the Mac. The README section can be written.

---

## Phase 6: User Story 4 — Export after each snapshot, verify on demand (Priority: P4)

**Goal**:
- a published snapshot emits the `pipeline-snapshot` asset event;
- the event starts `exam_export` over the exportable exams;
- `exam_verify` runs only when triggered by hand.

**Independent Test**: `tests/airflow/test_export.py` and `tests/airflow/test_verify.py`
with a `FakeRunner`, and `tests/pipeline/test_cli_listing.py::TestExamsSnapshot`.

**⚠️ Blocked**:
- T042–T047 need spec 001's `snapshot` and `export` stages (spec 001 US4);
- T048–T049 need spec 001's `verify` stage (spec 001 US5).

Until then, those DAG parts stay absent (FR-004).

- [ ] T042 [P] [US4] Write `tests/pipeline/test_cli_listing.py`, class `TestExamsSnapshot`:
  - `main(["exams", "--snapshot", V])` lists the exams of `snapshots/V/exam_status.parquet` that are not `unverified`, sorted, then `exams: count=N snapshot=V`;
  - an unknown V exits 2.
- [ ] T043 [US4] Implement `exams --snapshot V` (P1, full form) in `pipeline/__main__.py`, reading `exam_status.parquet` through the publish module spec 001 provides. Confirm in the same PR that spec 001's `snapshot` prints exactly `snapshot: published snap-NNNN …` or `snapshot: unchanged (snap-NNNN)` (contracts/pipeline-cli-additions.md). Mirror both into `specs/001-exam-data-aggregation/contracts/cli.md`. T042 must pass.
- [ ] T044 [P] [US4] Write `tests/airflow/test_acquisition.py`, class `TestSnapshot`:
  - after branch success, `snapshot` runs once in the writer pool;
  - a `published snap-0007` summary makes `announce_snapshot` succeed with outlet `extra == {"snapshot": "snap-0007"}`;
  - an `unchanged (snap-0006)` summary makes `announce_snapshot` skipped, with no asset event;
  - a failed branch skips `snapshot` (FR-005, US4 scenarios 1–2).
- [ ] T045 [P] [US4] Write `tests/airflow/test_export.py`:
  - `exam_export` has `schedule=[Asset("pipeline-snapshot")]`;
  - with a triggering event carrying `{"snapshot": "snap-0007"}`, the argv sequence is `exams --snapshot snap-0007`, then `export --exam X --eligible --snapshot snap-0007` for each listed exam only (FR-009);
  - a manual run with no `snapshot` param, or a malformed one, fails `resolve_snapshot` with no retry;
  - an empty exam list gives 0 export tasks and a successful run.
- [ ] T046 [US4] Add the `snapshot` task (writer pool, default trigger rule) and `announce_snapshot` (`outlets=[SNAPSHOT]`; it uses `outlet_events` to set `extra`, and raises `AirflowSkipException` when the summary is unchanged) to `orchestration/dags/acquisition.py`. Define `SNAPSHOT = Asset("pipeline-snapshot")` once in `orchestration/dags/stage.py`. T044 must pass.
- [ ] T047 [US4] Create `orchestration/dags/export.py`:
  - **DAG**: `exam_export`, `schedule=[SNAPSHOT]`, `catchup=False`, `params={"snapshot": Param("", type="string")}`.
  - **`resolve_snapshot`**: reads the newest `triggering_asset_events` `extra`, or else the param, and checks `snap-\d{4}`.
  - **`exportable_exams`**: runs `exams --snapshot V`.
  - **`export.expand(exam=…)`**: default pool.

  T045 must pass.
- [ ] T048 [P] [US4] Write `tests/airflow/test_verify.py`:
  - `exam_verify` has `schedule is None`;
  - a manual run with `{"exam": "csat", "seed": 7}` runs `verify draw --exam csat --seed 7` in the writer pool, then `verify status --exam csat` in the default pool;
  - a translation run passes `--kind translation --language L`;
  - a missing `exam` or `seed` fails param validation (FR-008, US4 scenario 3).
- [ ] T049 [US4] Create `orchestration/dags/verify.py` per contracts/dags.md → exam_verify. T048 must pass.
- [ ] T050 [US4] Run the full `tests/airflow/` in the test image. All must pass, and `test_dag_integrity.py` must now cover all three DAGs.

**Checkpoint**: All four stories are complete in tests. Quickstart Q9 can run on the Mac.

---

## Phase 7: Stages added as spec 001 delivers them (US1, FR-004)

**⚠️ Each task lands in the same PR as the spec 001 stage it names.**

- [ ] T051 [US1] When spec 001 delivers `extract` and `key` (spec 001 US2):
  - add them to the stage tuple in `orchestration/dags/acquisition.py`, after `fetch`, in the writer pool;
  - extend `TestHappyPath` in `tests/airflow/test_acquisition.py` with `extract --exam X` and `key --exam X`;
  - add their stages to the equivalence test in `tests/pipeline/test_equivalence.py`.
- [ ] T052 [US1] When spec 001 delivers `translate` (spec 001 US3):
  - add it to the stage tuple after `key`, in the writer pool;
  - pass `ANTHROPIC_API_KEY` to the scheduler in `orchestration/docker-compose.yaml`;
  - extend the same two tests.
- [ ] T053 [US1] When spec 001's OCR path lands, add `tesseract-ocr` to the `runtime` target in `orchestration/Dockerfile` (as `root`, then back to `airflow`), so `extract` works inside the container.

---

## Phase 8: Polish & cross-cutting concerns

- [ ] T054 [P] Add an "Orchestration (optional)" section to `README.md`. It covers:
  - what the three DAGs do;
  - the one start command;
  - how to trigger with an exam filter;
  - how to resume a failed run;
  - why Airflow lives outside the lockfile (constitution A2).

  It also carries the line `TODO: real acquisition run not yet executed` in place of any counts (SC-012). Frame it as evaluation and data infrastructure, not as helper code (CLAUDE.md).
- [ ] T055 [P] Move the matching `TODO.md` items: leave "Orchestrate harness runs" and "Deploy beyond one machine" as they are, and note under "Date-partitioned backfills" that catchup stays off (FR-010).
- [ ] T056 Run all gates on the whole repository:
  - ruff check and format;
  - project ty;
  - ty in the test image;
  - `pytest -q --ignore=tests/pipeline --ignore=tests/airflow`;
  - `pytest tests/pipeline -q` with `PIPELINE_TEST_DSN`;
  - `pytest tests/airflow -q` in the image;
  - `wc -l` on every new or changed file (< 800).

  Report actual results, failures included.
- [ ] T057 Run quickstart Q4–Q7 on the Mac against the fixture-sized real catalog: a first run, an approve and filtered re-run, an unknown exam, failure and resume, and a lock held by hand. Record what actually happened in the PR description.
- [ ] T058 Real run (SC-012), done by the maintainer: run quickstart Q10 against the real catalog. Replace the README TODO with the recorded counts from `pending_summary` and the fetch summaries only after the run, and leave the TODO in place if it has not run.

---

## Dependencies & Execution Order

### Phase dependencies

- **Phase 1 (Setup)**: none.
- **Phase 2 (Foundational)**:
  - T002–T005 form the amendment PR and land together with T006–T010 (governance: a new gate lands with its tooling);
  - this phase blocks every story.
- **Phase 3 (US1)**: depends on Phase 2. T013 → T014; T021 depends on T010, T014 and T017–T020.
- **Phase 4 (US2)**:
  - depends on Phase 2;
  - T029–T031 depend on T021, since they test the acquisition DAG;
  - T023–T028 (P3) depend only on Phase 2 and can run in parallel with US1.
- **Phase 5 (US3)**:
  - depends on Phase 2;
  - T033–T040 are independent of US1 and US2;
  - T041 needs T021, so there is a DAG to see.
- **Phase 6 (US4)**: depends on US1 (T021) and on spec 001 US4/US5. It is blocked externally.
- **Phase 7**: each task depends on its spec 001 stage and on T021.
- **Phase 8**: after the stories in scope. T058 can happen any time after T041.

### User story dependencies

- **US1 (P1)**: needs only Foundational. MVP.
- **US2 (P2)**:
  - its pipeline half (P3) is independent of US1;
  - its DAG half extends US1's tests.
- **US3 (P3)**: independent, except the manual check T041, which needs a DAG.
- **US4 (P4)**: needs US1's DAG and spec 001's later stages.

### Within each story

Tests come first and must fail, then the implementation, then the story's run-all task
(T022, T032, T050).

---

## Parallel Examples

### Foundational

```text
T003 CLAUDE.md line          T004 ty exclusion          T007 .dockerignore
T008 tests/airflow/conftest  T009 tests/airflow/test_stage.py
```

### User Story 1

```text
T011 + T012 tests/pipeline/test_cli_listing.py   (same file, write together)
T016 tests/pipeline/test_equivalence.py
T017 tests/airflow/test_dag_integrity.py
T018–T020 tests/airflow/test_acquisition.py      (same file, write together)
T015 spec 001 contract mirror
```

### User Story 2 alongside User Story 1 (different files)

```text
T023 tests/pipeline/test_web.py → T024 schema.sql → T025 store.py → T026 web.py → T027 __main__.py
```

### User Story 3

```text
T033 + T034 tests/pipeline/test_guard.py   T037 postgres init   T038 .env.example + .gitignore
```

---

## Implementation Strategy

### MVP first (US1)

1. Phase 1, then Phase 2: the amendment, the image, and `run_stage`.
2. Phase 3 (US1): P1, P2, the equivalence test, and the acquisition DAG.
3. **Stop and validate**: T022 is green; `dag.test()` shows the right command sequence;
   the equivalence test passes.

### Incremental delivery

1. US1: acquisition in tests.
2. US2: failure isolation, a request gap that holds across commands (P3), and resume.
3. US3: the local stack. The first real use on the Mac follows (T041, T057).
4. US4: when spec 001 delivers snapshot, export and verify.
5. Phase 7 tasks, one per spec 001 stage, as each lands.

### Suggested PR series (plan.md → Delivery order)

| PR | Tasks |
|----|-------|
| 1. Amendment A2 + image + `run_stage` | T001–T010 |
| 2. Pipeline listing commands + equivalence | T011–T016 |
| 3. Acquisition DAG | T017–T022 |
| 4. Request spacing across invocations | T023–T028 |
| 5. Failure, retry and resume tests | T029–T032 |
| 6. Local stack + isolation + guard paths | T033–T041 |
| 7. README | T054–T056 |
| 8. Export and verify DAGs, once spec 001 delivers them | T042–T050 |

---

## Notes

- [P] means a different file and no dependency on an unfinished task. Tasks on the same
  file are listed in order.
- Never put stage logic in a DAG file. If a DAG seems to need pipeline knowledge, add a
  read-only pipeline command instead (the P1/P2 pattern).
- Never write results or counts that have not come from an executed run (T041, T057,
  T058).
