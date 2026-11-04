# Quickstart: Validating the Exam Dataset Pipeline

Runnable scenarios showing the feature works end to end. Scenarios 1–7 are offline and
synthetic, which is what CI runs. Scenario 8 is the maintainer's real-data run. It is never
part of CI, and its results are reported only after it has actually been executed.
Formats are in [contracts/](contracts/), and tables are in [data-model.md](data-model.md).

## Prerequisites

- Python 3.13 and `uv`.
- A local Postgres 17. On macOS:

  ```bash
  brew install postgresql@17
  ```

  ```bash
  brew services start postgresql@17
  ```

  ```bash
  createdb pipeline_test
  ```

- Install with the pipeline extra:

  ```bash
  uv sync --locked --extra dev --extra pipeline
  ```

- Point the tests at the local database:

  ```bash
  export PIPELINE_TEST_DSN=postgresql:///pipeline_test
  ```

No AWS credentials, API keys, Tesseract, or network are needed for scenarios 1–7.

## Scenario 1: the whole offline suite (SC-009)

```bash
uv run pytest tests/pipeline -q
```

Expected: all tests pass. Run the command a second time and expect identical results. With
networking disabled (for example, Wi-Fi off), the result is the same, since only the local
Postgres socket is used.

## Scenario 2: catalog completeness (US1, SC-001)

```bash
uv run pytest tests/pipeline/test_acquire.py -q -k catalog
```

Expected: a synthetic catalog that covers all 66 cells loads. Removing one cell, leaving an
edition without evidence, or citing an unapproved source each makes `catalog` exit with
code 3 and write nothing.

## Scenario 3: acquisition rules (US1 acceptance 3–4, FR-007–FR-009)

```bash
uv run pytest tests/pipeline/test_acquire.py -q -k "fetch or discover or review"
```

Expected, all against `httpx.MockTransport`:
- An unapproved candidate is never requested (the transport records zero calls to it).
- A non-allowlisted link is recorded as `rejected`. A robots-disallowed path is recorded as
  `refused_robots`, and a 401 as `login_required`.
- Two requests to one domain are at least `rate_limit_seconds` apart on the injected clock.
- A changed file at the same URL produces a second blob, and the first blob is unchanged.

## Scenario 4: extraction, keys, translation (US2, US3, SC-002, SC-004)

```bash
uv run pytest tests/pipeline/test_extract.py -q
```

Expected:
- Every question has an edition, full provenance, and either a key or a `no_official_key`
  failure.
- Figure, table, audio, and free-response items are stored with `eligible = false`.
- Two identical items in two editions share one `canonical_question_id`. A one-character
  difference does not.
- A key erratum becomes current, and the old key row remains.
- Half the synthetic items get `official` English and the rest get `mt`, with model,
  revision, and prompt hash recorded. A fake translator that returns empty text produces
  `mt_empty_output`.
- Running each stage a second time prints `inserted=0 updated=0 deleted=0`.

## Scenario 5: snapshot, export, harness smoke (US4, US5, SC-006, SC-007)

```bash
uv run pytest tests/pipeline/test_publish.py -q
```

Expected:
- The snapshot's manifest row counts equal the Parquet counts and the live counts for every
  table.
- Exporting the same exam from one snapshot twice gives byte-identical files that carry the
  snapshot version.
- After a key correction and a new snapshot, the new export shows only the new key.
- The exported bank loads through `harness.config.load_suite` (paired with the tracked
  `suites/stereotype_threat/conditions.yaml`, as CI already does). A `MockBackend` run
  writes the required artifacts, with no harness file modified.
- An exam with a failing hand-check event is refused on export with exit code 3.
- Rebuild equivalence: a rebuild into an empty schema from the same catalog, ledger, and
  blobs reproduces 100% of per-question content hashes (SC-005).

The same smoke test can also be run through the CLI on a synthetic export:

```bash
uv run python -m harness run <suite dir holding the exported questions.json> --backend mock --models smoke --n-samples 2 --results-dir /tmp/smoke
```

## Scenario 6: corpus guard (FR-025, FR-026, SC-008)

```bash
uv run pytest tests/pipeline/test_guard.py -q
```

```bash
uv run python -m pipeline.corpus
```

Expected: the tests show the guard failing (exit 3) in each of the three planted cases: a
corpus string outside the allowlist, a sample over the cap, and an entry with no licence.
On the real repository, the second command exits 0. This also runs as a pre-commit hook.

## Scenario 7: the dependency boundary (FR-030)

```bash
uv sync --locked
```

```bash
uv run pytest --ignore=tests/pipeline -q
```

Expected: the core install pulls no pipeline packages and every harness test passes. The
test `test_guard.py::test_harness_never_imports_pipeline` passes too.

## Scenario 8: real data (maintainer only, outside CI)

Needs `PIPELINE_DSN`, `PIPELINE_BLOB_ROOT` (S3 or local), Tesseract with language packs for
scanned papers, and `ANTHROPIC_API_KEY` for MT. `catalog` only succeeds once all 66 cells
in `catalog.yaml` have an edition with evidence (FR-003, FR-004). So discovery, review, and
fetch run for every exam first, and extraction for any exam starts only after that. Order,
shown for one exam:

```bash
uv run pipeline discover --exam enem
```

```bash
uv run pipeline review <candidate_id> --approve --edition enem-2019-day1-azul --role paper
```

```bash
uv run pipeline fetch --exam enem
```

```bash
uv run pipeline catalog
```

```bash
uv run pipeline extract --exam enem
```

```bash
uv run pipeline key --exam enem
```

```bash
uv run pipeline translate --exam enem
```

```bash
uv run pipeline verify draw --exam enem --seed 20261104
```

```bash
uv run pipeline verify record <filled csv>
```

```bash
uv run pipeline snapshot
```

```bash
uv run pipeline export --exam enem --eligible --snapshot snap-0001
```

Expected: the dataset card's coverage matrix has a status for every cell, and the export
prints its question count and its `answers_beyond_d` count. Commit `catalog.yaml`,
`ledger.jsonl`, and `fingerprints.txt`. Never commit the exported bank or anything under
the blob root. Results tables in the README come only from this run (prime directive).
