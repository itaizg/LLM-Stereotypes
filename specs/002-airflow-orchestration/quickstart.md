# Quickstart: validate the Dagster orchestration

Runnable checks that show the feature works. Interfaces are defined in
[contracts/](contracts/), and fields in [data-model.md](data-model.md). Airflow was replaced
by Dagster before it was merged (research D1–D5).

## Prerequisites

- `uv sync --extra pipeline --extra orchestration --extra dev` (one environment).
- For the compose route: Docker Desktop, the repository folder kept downloaded in iCloud
  (**Keep Downloaded**; an evicted file reads as missing inside a container), and
  `orchestration/.env` filled in from `orchestration/.env.example`.
- A Postgres for `PIPELINE_DSN`: the compose `postgres` service alone is enough.
- Ports 5432 and 3000 free on the Mac for the compose route.

## Q1. Offline tests (no scheduler, no network)

```bash
uv run pytest -q --ignore=tests/pipeline --ignore=tests/dagster   # core suite: no Dagster needed
uv run pytest tests/dagster -q                                     # stage policy, assets, partitions
uv run pytest tests/pipeline -q                                    # P1–P4 + equivalence (needs PIPELINE_TEST_DSN)
uv run ty check
uv run dagster definitions validate -m exam_dagster.definitions -d orchestration
```

Expected:
- All pass.
- `tests/dagster` shows the definitions load (SC-003), every exit code mapped (SC-004), and
  the assets issue exactly the commands in `tests/fixtures/orchestration_acquisition_argv.json` (SC-001).
- `tests/pipeline` shows that per-exam runs equal one all-exam run (SC-001), and that
  requests to one host stay at least 5 s apart across invocations (SC-006).

## Q2. Isolation checks (SC-008)

```bash
uv sync                                       # core install only
uv run python -c "import dagster"             # expect ModuleNotFoundError
uv run python -c "import harness, pipeline"   # imports without Dagster
uv run pytest tests/pipeline/test_guard.py -q # harness/pipeline never import Dagster
```

## Q3. Start it (US3, SC-010)

**On the Mac:**

```bash
# Postgres: only its compose service (fill orchestration/.env first; set
# PIPELINE_PG_PORT there if 5432 is already used by another container).
docker compose -f orchestration/docker-compose.yaml up -d postgres
export PIPELINE_DSN="postgresql://pipeline:<PIPELINE_DB_PASSWORD>@localhost:5432/pipeline"
orchestration/dev.sh
```

Use your `PIPELINE_PG_PORT` in the DSN if you set one. Check the connection first with
`uv run python -m pipeline catalog` (exit 1 there means the database refused you).

`dev.sh` is this, so use it instead of a bare `dagster dev` (which has no code to load,
and without `DAGSTER_HOME` has no writer-pool limit):

```bash
DAGSTER_HOME=$PWD/orchestration/dagster_home \
  uv run dagster dev -m exam_dagster.definitions -d orchestration
```

**With compose:**

```bash
docker compose -f orchestration/docker-compose.yaml up -d --build
```

Expected: the UI at <http://127.0.0.1:3000> shows the `exam_acquisition` assets, with one
partition per exam in `uv run pipeline exams`. If the code location fails to load, the error
shows the `pipeline exams` message (for example an invalid catalog, exit 3).

## Q4. A first acquisition run (US1)

In the UI, open the `exam_acquisition` job (or select `discovered` and `fetched`), then
**Launch backfill** over all partitions. Expected:
- One partition per exam runs `discovered → fetched`, even while the catalog is still
  incomplete.
- `catalog` does not run. Run the `validate_catalog` job when you think the catalog is
  complete; exit 3 lists what is still missing.
- `discovered` shows a WARN check `pending_review` with the pending ids when candidates
  wait for a review (FR-007); the backfill still succeeds.
- Writing steps never overlap: check the run timeline.

Launch a second backfill while the first is running. Expected: no two writing steps
overlap across the two, and no step log shows `locked:` (SC-005).

Then approve one candidate from the Mac:

```bash
uv run pipeline candidates --exam csat
uv run pipeline review <source_id> --approve --edition <edition_id> --role paper
```

Launch a backfill over the `csat` partition only. Expected:
- Only `csat` runs (SC-014).
- Its `fetched` log shows the approved source fetched (US3 scenario 4).
- A third identical launch adds no ledger line: check with `git diff --stat
  pipeline/ledger.jsonl` (SC-002).

## Q5. Unknown exam (FR-026)

Open the partitions of `discovered`. Expected: an exam not in the catalog is not listed, so
it cannot be launched. Add an exam to the catalog, then **Reload definitions**: it appears.

## Q6. Failure and resume (US2, FR-025, SC-013)

1. Stop Postgres for a moment during a run:
   `docker compose -f orchestration/docker-compose.yaml stop postgres`, then start it again.
2. Expected: the running step exits 1 and retries; if every retry fails, that exam's
   partition fails and the other exams finish.
3. Open the failed run and choose **Re-execute from failure** (or launch a backfill over the
   failed partitions).
4. Expected: only that exam's remaining steps run. Its `fetched` log shows `unchanged` for
   files already stored, which means conditional requests and no re-download.

## Q7. Lock held by hand (edge case)

Run `uv run pipeline fetch` on the Mac while a run's writing step is queued. Expected: the
step meets exit 4, logs `locked: …`, and retries 3 minutes later.

## Q8. No secrets or corpus text in run storage (SC-009)

```bash
uv run python -m pipeline.corpus orchestration/dagster_home   # expect no hits (P4)
```

For compose, copy a run's logs out of the volume or read them in the UI. The corpus check is
only meaningful once a snapshot has published fingerprints. Before that,
`pipeline/fingerprints.txt` is empty and the check passes trivially.

## Q9. Export and verify (US4) — once spec 001 snapshot, export and verify exist

- An `exported` asset partitioned by exam is materialised by hand (or by
  `AutomationCondition.eager()` if that is chosen later). It exports only exams listed by
  `pipeline exams --snapshot snap-NNNN`.
- `exam_verify` never runs unless launched by hand.

## Q10. The real run (SC-012)

Run Q4 against the real catalog and websites, then record the counts from the `discovered`
and `fetched` metadata in the README. Until that run happens, the README shows
`TODO: real acquisition run not yet executed`.
