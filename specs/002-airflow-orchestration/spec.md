# Feature Specification: Orchestrate the Exam Pipeline with Apache Airflow 3

> **Superseded in part (amendment A3, 2026-11-18).** The orchestrator is Dagster, not
> Airflow: assets partitioned by exam, one uv environment (the `orchestration` extra), no
> sensors or schedules. Read [research.md](research.md) D1–D5 for the decision record and
> [contracts/assets.md](contracts/assets.md) and [contracts/stack.md](contracts/stack.md)
> for the current contracts. Mapping: DAG → asset group; `select_exams` → static
> partitions; `pipeline_writer` pool → Dagster pool; `pending_summary` → `pending_review`
> check; Clear failed → Re-execute from failure. The text below is kept as the original
> record, and where it names Airflow concepts the mapping above applies.

**Feature Branch**: `002-airflow-orchestration` (branched from
`feat/data-aggregation-pipeline`, because spec 001's acquisition PR is still open)

**Created**: 2026-11-09

**Status**: Draft

**Input**: User description: "according to the mission brief". Source:
[Mission Brief: Exam Pipeline on Apache Airflow 3](https://claude.ai/artifact/W8g3fyQ3T92shJWif1io3e),
as of 2026-11-09, including the maintainer's answers to its open questions.

## Overview

The exam-dataset pipeline (spec 001) runs as separate, idempotent stage commands. The
maintainer runs them by hand, one after another, for six exams. This feature adds
workflows in Apache Airflow 3.1 that run those same commands in order, per exam, with
retries, a single writer at a time, and a record of every run. The workflows call the
existing commands; they hold no stage logic of their own, and the command line keeps
working without Airflow.

The orchestrator choice (Airflow 3.1, the official Docker image, LocalExecutor) was made
by the stakeholder in the mission brief. It is a scope decision, not an open design
choice for planning.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Run acquisition for all exams in one trigger (Priority: P1)

The maintainer triggers one acquisition run. It runs the catalog stage once for all
exams, then each exam's stages in order, then one snapshot. Each step runs the same
command the maintainer would type, so the results equal a hand-run sequence.

**Why this priority**: This is the core of the feature. Without it, nothing else here
has anything to orchestrate.

**Independent Test**: On the fixture catalog and the fake website used by the pipeline
tests, run the acquisition workflow in test mode and run the same stages by hand through
the command line. Compare the two ledgers and the pipeline table row counts.

**Acceptance Scenarios**:

1. **Given** the fixture catalog and fake website, **When** the maintainer triggers an
   acquisition run, **Then** catalog runs once, each exam runs its implemented stages in
   order, and the ledger and row counts equal those of the hand-run sequence.
2. **Given** a completed run, **When** the maintainer triggers it again with unchanged
   inputs, **Then** no ledger line is added and no row changes.
3. **Given** the catalog stage refuses an invalid catalog, **When** the run starts,
   **Then** no exam stage runs and the run ends failed.
4. **Given** a stage from spec 001 that is not implemented yet, **When** the workflow is
   loaded, **Then** that stage is absent from it, and the run covers the stages that
   exist.
5. **Given** discover finds new candidates, **When** the run continues, **Then** it does
   not pause, fetch requests only approved sources, each affected exam's discover task
   logs a warning, and the run's final summary task lists the pending candidates per
   exam. The run's outcome is unchanged by pending candidates.
6. **Given** the maintainer names one exam when triggering, **When** the run completes,
   **Then** catalog runs once, only that exam's branch runs, and the snapshot runs if that
   branch succeeded. With no exam named, every exam in the catalog runs.

---

### User Story 2 - Failures and concurrency are handled safely (Priority: P2)

A failure in one exam does not stop the others. Each stage's exit code decides whether
it is retried or stops. Only one stage writes at a time, and parallel work never sends
requests to a host faster than the command line would.

**Why this priority**: Unattended runs must not corrupt the store, break site etiquette,
or retry errors that cannot fix themselves.

**Independent Test**: Use stage stand-ins that return each exit code, two concurrent
runs, and the fake website with a virtual clock. Check task outcomes, writer overlap, and
request spacing per host.

**Acceptance Scenarios**:

1. **Given** one exam's fetch fails, **When** the run continues, **Then** the other
   exams' stages still run to completion.
2. **Given** a stage exits with each of the five exit codes, **When** its task finishes,
   **Then** the outcome matches FR-006.
3. **Given** two runs are active, **When** both reach writing stages, **Then** no two
   writing stages run at the same time.
4. **Given** the fake website, **When** a run fetches for several exams, **Then**
   requests to any one host are at least 5 seconds apart.
5. **Given** one exam's branch failed and the snapshot was skipped, **When** the
   maintainer fixes the cause and resumes the run, **Then** only the failed tasks and the
   tasks after them run, the snapshot runs once, and no file already stored is downloaded
   again.

---

### User Story 3 - Start the orchestrator locally with one command (Priority: P3)

From a clean checkout, the maintainer starts Airflow on the development Mac with one
command and sees every workflow ready to trigger. Secrets come from the environment, and
the harness and pipeline installs are unchanged.

**Why this priority**: The workflows are only useful once the maintainer can run them;
this also proves the isolation rules hold.

**Independent Test**: Start the stack from a clean checkout; check that every workflow
loads without errors, that the orchestrator's records sit in their own database, and that
the core and `pipeline` installs contain no Airflow package.

**Acceptance Scenarios**:

1. **Given** a clean checkout with Docker, **When** the maintainer runs the documented
   start command, **Then** Airflow starts and every workflow appears with no load errors.
2. **Given** the stack is running, **When** a run completes, **Then** Airflow's records
   are in a database separate from the pipeline store, and no pipeline table was written
   by Airflow itself.
3. **Given** a sentinel value for the database connection secret, **When** a run
   completes, **Then** the value appears in no task log and no data passed between tasks.
4. **Given** the stack is running, **When** the maintainer approves a candidate with
   `pipeline review` on the Mac, **Then** the next acquisition run fetches that source,
   because the Mac and the containers use the same pipeline database and stored files.

---

### User Story 4 - Export after each snapshot, verify on demand (Priority: P4)

Publishing a new snapshot starts the export workflow, which exports every exam not marked
unverified. The hand-check workflow runs only when the maintainer starts it.

**Why this priority**: These stages come last in spec 001 (US4, US5). The workflows can
only be finished once those stages exist.

**Independent Test**: With the snapshot and export stages available, publish a snapshot
in test mode and check that export runs for the eligible exams only; start the verify
workflow by hand and check that it never starts on its own.

**Acceptance Scenarios**:

1. **Given** a run publishes a new snapshot version, **When** it completes, **Then** the
   export workflow starts and exports each exam not marked unverified from that snapshot.
2. **Given** the snapshot stage finds nothing changed, **When** it completes, **Then** no
   new version is published and export does not start.
3. **Given** the verify workflow, **When** no one triggers it, **Then** it never runs.

---

### Edge Cases

- The maintainer runs a writing command by hand while a run is active: the stage task
  meets the pipeline lock (exit 4) and retries after a delay.
- An exam has no seeds (SAT today): discover finds nothing, and the exam's branch
  succeeds with zero counts.
- A host answers with a `Retry-After` longer than the pipeline's cap: the pipeline records
  an HTTP error for that source; the task itself succeeds, as the command does.
- The container restarts mid-stage: rerunning is safe because every stage is idempotent.
  A ledger left unreadable is refused by the pipeline, and the task fails for the
  maintainer to inspect.
- An exam is marked unverified: export skips it rather than failing on the pipeline's
  refusal.
- The trigger names an exam that is not in the catalog: the run fails before any exam
  stage runs, and no ledger line is added.
- An exam's branch fails: the snapshot does not run for that run (FR-005). After the
  fix, the maintainer resumes the same run, and only the failed branch's remaining tasks
  and the snapshot run (FR-025).
- The stack is stopped while the maintainer runs a `pipeline` command on the Mac: the
  pipeline database is down, so the command cannot reach the store and fails. It works
  again once the stack is started.

## Requirements *(mandatory)*

### Functional Requirements

**Workflow scope**

- **FR-001**: Each stage task MUST run the stage's command-line command
  (`uv run pipeline <stage>` with the same flags a maintainer would pass). Workflow files
  MUST contain no stage logic.
- **FR-002**: The command line MUST keep working with Airflow absent. `pipeline` and
  `harness` MUST NOT import Airflow.
- **FR-003**: The acquisition workflow MUST run `catalog` once for all exams; then, per
  exam, `discover` → `fetch` → `extract` → `key` → `translate`; then one `snapshot` after
  every exam's branch has finished.
- **FR-004**: The workflow MUST include only stages that are implemented. Each spec 001
  stage is added when it is delivered.
- **FR-005**: Per-exam stages MUST run as one branch per exam in the catalog. A failure
  in one branch MUST NOT stop the other branches. The snapshot MUST run only when every
  branch in the run succeeded.
- **FR-026**: An acquisition trigger MAY name a subset of the catalog's exams; when it
  names none, every exam in the catalog runs. `catalog` still runs once for all exams.
  Only the named exams get a branch, and the snapshot rule in FR-005 applies to those
  branches. A name not in the catalog MUST fail the run before any exam stage runs.
- **FR-006**: Exit codes MUST map to task outcomes: 0 succeeds; 1 retries a bounded
  number of times; 4 (lock held) retries after a delay; 2 (usage) and 3 (integrity
  refusal) fail with no retry.
- **FR-007**: Source approval MUST stay with the maintainer, through `pipeline review`.
  No task approves or rejects a candidate. The run MUST NOT pause for approval: fetch
  continues with the sources already approved, and new candidates wait for a later run.
  When candidates are awaiting review after discover, that exam's discover task MUST log
  a warning with the number of pending candidates and their identifiers. A final summary
  task MUST run after every branch has ended, whatever their outcomes, and list the
  pending candidates per exam. Pending candidates MUST NOT change the run's outcome: no
  task fails because of them, and no notification is sent.
- **FR-008**: The verify workflow MUST run only when the maintainer triggers it.
- **FR-009**: The export workflow MUST start when a snapshot publishes a new version, and
  MAY also be triggered by hand. It MUST export only exams not marked unverified.
- **FR-010**: Workflows MUST run only when triggered or when FR-009's event fires. There
  is no time schedule and no catch-up of past dates.

**Concurrency and etiquette**

- **FR-011**: At most one writing stage MUST run at any time, across all active runs,
  matching the pipeline's single write lock.
- **FR-012**: Requests from a fetch run MAY be concurrent across hosts, but each host
  MUST keep the pipeline's limits: allowlist, robots.txt, at least 5 seconds between
  requests, and backoff on 429 and 503.

**Observability and data handling**

- **FR-013**: Each task MUST log its stage's summary counts (for example fetched,
  unchanged, refused).
- **FR-027**: Failed runs and tasks MUST be reported in the Airflow UI only. No email,
  chat or desktop notification is sent, so the stack needs no notification secrets.
- **FR-014**: Task logs and data passed between tasks MUST carry counts and identifiers
  only, never question text.
- **FR-015**: Secrets MUST reach tasks as environment variables only. They MUST NOT be
  stored as Airflow Variables or Connections, and MUST never be logged.
- **FR-016**: Airflow's records MUST live in a database separate from the pipeline store.
  Both databases MUST run on one Postgres server inside the Docker stack. The pipeline
  database's port MUST be published to the host, so `pipeline` commands run on the Mac
  (with `PIPELINE_DSN` pointing at `localhost`) use the same store as the tasks. Airflow
  MUST NOT write pipeline tables.
- **FR-017**: Tasks append to the committed ledger in the repository checkout. Airflow
  MUST NOT commit or push.

**Environment**

- **FR-018**: One documented command MUST start Airflow locally from the official Docker
  image for Python 3.13, with LocalExecutor instead of the image's default executor.
- **FR-019**: Inside the container, the project MUST run from its own environment, built
  from the project's lockfile, separate from Airflow's packages and from the host's
  environment. The repository checkout and the stored-files folder MUST be mounted into
  the containers, so tasks and commands on the Mac read and write the same ledger and
  files.
- **FR-020**: The core and `pipeline` installs MUST contain no Airflow package. Airflow's
  packages MUST NOT enter the project's lockfile.
- **FR-021**: A constitution amendment allowing the separate Airflow environment MUST
  land before the first workflow file, following the amendment process (Sync Impact
  Report, version bump).

**Quality**

- **FR-022**: All tests MUST run offline and deterministically, with no running
  scheduler. Airflow-dependent tests MUST run in their own CI job, so the core suite never
  installs Airflow.
- **FR-023**: Workflow files MUST use Airflow's Task SDK only, never its internal models.
- **FR-024**: New code MUST pass ruff, ty and pytest, and every file MUST stay under 800
  lines (CDR-2026-022).

**Reruns**

- **FR-025**: After a branch fails, the maintainer MUST be able to resume the same run:
  only the failed tasks and the tasks after them (including the snapshot) run again, and
  tasks that already succeeded in that run MUST NOT be repeated. Stages that do run again
  rely on the pipeline's own stored state to avoid repeat downloads: fetch sends
  conditional requests for sources it already stored, so unchanged files are not
  downloaded again.

### Key Entities

- **Workflow**: a named, versioned definition of stages and their order: acquisition,
  export, verify.
- **Run**: one execution of a workflow, started by a trigger or an event; it holds the
  outcome of each task.
- **Stage task**: one invocation of one stage command, either for all exams or for one
  exam; it ends with an exit code and summary counts.
- **Exam branch**: the per-exam sequence of stage tasks within an acquisition run.
- **Writer slot**: the single permission to run a writing stage, shared by all runs.
- **Snapshot event**: the signal that a new snapshot version was published; it starts the
  export workflow.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: On the fixture catalog, running each stage once per exam, as the
  acquisition workflow does, and running it once for all exams by hand produce the same
  set of ledger events and equal row counts in every pipeline table. The comparison
  leaves out event order and each event's `at` audit time: these differ between any two
  executions, by hand or not.
- **SC-002**: A second run on unchanged inputs adds 0 ledger lines and changes 0 rows.
- **SC-003**: 100% of workflow definitions load with 0 errors.
- **SC-004**: All 5 exit codes produce their specified outcome, each shown by a test.
- **SC-005**: Every writing task, in every workflow, uses the one writer pool, and that
  pool has exactly 1 slot. In the quickstart's two-run check, 0 writing stages overlap and
  0 tasks meet the lock.
- **SC-006**: In the fake-website run, requests to any one host are at least 5 seconds
  apart across all tasks and runs.
- **SC-007**: 0 sources are fetched without an approval event in the ledger; 100% of
  runs that leave candidates pending end with a summary task listing them per exam; and
  0 runs change outcome because candidates are pending.
- **SC-008**: The core and `pipeline` installs contain 0 Airflow packages, and `harness`
  and `pipeline` import with Airflow absent.
- **SC-009**: A sentinel secret appears 0 times in task logs or data passed between
  tasks, and the corpus guard finds 0 corpus strings in Airflow's logs.
- **SC-010**: From a clean checkout, one command starts Airflow, and every workflow
  appears with 0 load errors.
- **SC-011**: With networking disabled, the acquisition workflow runs end to end in
  test mode with a stand-in for the command line, and issues exactly the expected command
  sequence. The pipeline runs that same sequence against a local database and the fake
  website (SC-001). Both halves are needed because the pipeline cannot run inside
  Airflow's environment (FR-019).
- **SC-012**: One real acquisition run completes on the development Mac, and its counts
  are recorded. Until it has run, the README carries a TODO, not a number.
- **SC-013**: Resuming a run in which one branch failed re-runs 0 tasks that had
  succeeded, and the fake website receives 0 unconditional requests for files already
  stored.
- **SC-014**: A run triggered with one exam named runs 0 stage tasks for any other exam;
  a run with no exam named runs a branch for 100% of catalog exams; a run naming an
  unknown exam adds 0 ledger lines.

## Assumptions

- The mission brief (as of 2026-11-09, all review edits accepted) is the source. This spec
  adds decisions the brief does not state; each is recorded under Clarifications.
- Bounded retries: exit 1 retries twice; exit 4 retries with a delay of a few minutes.
  These are planning defaults the plan may tune.
- SC-001 and SC-011 were reworded during planning (research A11). Byte-identical ledgers
  cannot hold, because ledger lines carry wall-clock times and the order of exams is not
  fixed. A single in-process end-to-end test would need the pipeline inside Airflow's
  environment.
- SC-005 is checked structurally in tests (pool membership and size) and by observation
  in the quickstart. The in-process test runner cannot hold two runs active at once.
- Resuming re-runs whole stage commands, not single requests. Inside a re-run fetch,
  an already-stored file still costs one conditional request (answered 304), but no
  download. Skipping that request too would change the pipeline, which is out of scope.
- Everything runs on one machine, the M4 Max development Mac, with Docker available.
  The pipeline database lives in the Docker stack (FR-016), so the stack must be up for
  any `pipeline` command that touches the store, including ones run on the Mac.
- A "writing stage" is any command that takes the pipeline's write lock. The writer slot
  covers exactly those commands, so a read-only command such as `verify status` never
  waits for it.
- Out of scope (brief non-goals): replacing the command line or implementing spec 001's
  missing stages; automating approval or hand-checks; orchestrating harness runs; cloud,
  Kubernetes or remote executors; raising any host's request rate; date-partitioned
  backfills. The deferred ones are tracked in `TODO.md`.

## Clarifications

### Session 2026-11-09

- Q: Should an acquisition run pause after discover for the maintainer's approval?
  → A: No pause. Fetch continues with approved sources; pending candidates raise a
  warning on the run and are logged per exam (FR-007).
- Q: Where should the pipeline's database and stored files live, so that Airflow's tasks
  and the `pipeline` commands run on the Mac use the same data? → A: In the Docker stack.
  One Postgres server holds Airflow's database and the pipeline's; the pipeline
  database's port is published to the Mac, and the repository and stored-files folder are
  mounted into the containers (FR-016, FR-019, US3 scenario 4).
- Q: When one exam's branch fails, should the run still publish a snapshot? → A: Only if
  every branch succeeded. Resuming the failed run repeats no task that succeeded, and
  re-run stages avoid repeat downloads (FR-005, FR-025, US2 scenario 5, SC-013).
- Q: When discover finds candidates not yet reviewed, how should the run show it? → A:
  The run stays successful. Each exam's discover task logs a warning, and a final summary
  task lists pending candidates per exam (FR-007, US1 scenario 5, SC-007).
- Q: Should a run be able to target only some exams? → A: Yes, an optional exam filter
  on the trigger; the default is all exams (FR-026, US1 scenario 6, SC-014).
- Q: When an acquisition run fails, how should the maintainer find out? → A: In the
  Airflow UI only; no notifications (FR-027).

## Dependencies

- Spec 001 stages: catalog, discover, review and fetch exist
  ([PR #5](https://github.com/itaizg/LLM-Stereotypes/pull/5)); extract, key, translate,
  verify, snapshot and export arrive with spec 001 US2–US5.
- The pipeline's write lock, exit codes and fetch etiquette, as defined in spec 001's CLI
  contract.
- The constitution amendment in FR-021.
- Docker on the development Mac.
