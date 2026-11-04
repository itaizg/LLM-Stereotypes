# Implementation Plan: Cross-National Exam Question Dataset (Data Aggregation)

**Branch**: `docs/data-agg-plan` | **Date**: 2026-11-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/001-exam-data-aggregation/spec.md`

## Summary

Build a new, optional `pipeline/` package that builds two linked datasets: an exam catalog
of every edition of six admissions exams for 2015–2025 (66 exam-year cells), and a
questions dataset of every officially released question, with original-language text,
official keys, English text, and full provenance. Suite-ready `questions.json` banks are
exported from it, and the harness loads them unchanged.

The design rests on three ideas:

1. **Committed inputs, private outputs.** A human-authored `catalog.yaml` (exams, editions,
   evidence) and a machine-written, append-only `ledger.jsonl` (candidate URLs, review
   decisions, fetched-artifact hashes, hand-check results) hold every decision, with no
   question text. Raw files sit in a content-addressed blob store (local directory or S3).
   Postgres is the live store and is fully rebuildable from those three inputs (FR-020).
2. **Stages are reconcilers.** Each stage computes, for one edition, the rows it owns as a
   pure function of its inputs, then writes only the difference, in one transaction per
   edition. That gives idempotence (FR-019) and deterministic rebuilds without a workflow
   engine. One pipeline-wide write lock lets only one writing command run at a time
   (research R16).
3. **Derived "current", never stored.** Keys and translations are append-only. The current
   key and the preferred translation are chosen by the `questions_flat` view (FR-017), so
   no stage ever updates a flag that another stage reads.

Three planning decisions change the brief's stack (spec Clarifications, planning session).
Discovery crawls official index pages instead of the Google search API, which closes on
2027-01-01. Batch work uses pyarrow plus a process pool instead of PySpark. Questions with
five or more choices are exported, and a harness TODO covers scoring them.

## Technical Context

**Language/Version**: Python 3.13, managed with `uv`, locked in `uv.lock`.

**Primary Dependencies**: All of these go in a new optional `pipeline` extra; the core
install is unchanged (FR-030).
`psycopg[binary]` 3 (Postgres, parameterized queries only), `httpx` (async HTTP),
`boto3` (S3), `pyarrow` (Parquet snapshots), `pdfplumber` (PDF text with positions),
`pytesseract` (OCR wrapper; needs the system Tesseract binary, used only for scanned papers),
`openai` (thin client for machine translation through an OpenAI-compatible endpoint).
Standard library covers robots.txt (`urllib.robotparser`), HTML link extraction
(`html.parser`), and parallelism (`concurrent.futures`). Choices and rejected alternatives
are in [research.md](research.md).

**Storage**: Postgres 17 is the live store. Raw files and snapshots go to a
content-addressed blob store, either S3 or a local directory behind the same interface.
Snapshots are Parquet plus a JSON manifest. Committed manifests are
`pipeline/catalog.yaml` and `pipeline/ledger.jsonl`.

**Testing**: pytest under `tests/pipeline/`, fully offline. httpx `MockTransport` replaces
the network, the local blob store or a `botocore` Stubber replaces S3, a fake translator
and a fake OCR engine replace their services, and a local Postgres (a CI service container,
or a local server through `PIPELINE_TEST_DSN`) gives each test a fresh schema. All
fixtures are synthetic and built in code.

**Target Platform**: macOS (Apple Silicon) for the maintainer and `ubuntu-latest` for CI.
GPU-free.

**Project Type**: A CLI-driven batch data pipeline (`pipeline <stage>`), packaged next to
the existing harness library and CLI.

**Performance Goals**: None beyond finishing a full rebuild in one sitting. The expected
scale is a few hundred to a few thousand artifacts and 10⁴–10⁵ questions. Fetching is
deliberately slow: at most one request per domain every 5 s by default.

**Constraints**: Fetch only from allowlisted official domains, respecting robots.txt. No
question text in git outside the sample allowlist. Tests use no network, cloud, keys, or
GPU. Exports must be byte-identical for the same snapshot, and rebuilds must reproduce
identical per-question content hashes. Secrets come from environment variables only.

**Scale/Scope**: 6 exams, 11 years, about 66–200 editions (Gaokao national papers and SAT
practice tests add several per cell), and 10⁴–10⁵ questions. Exam text is in English,
Hebrew (with official English and other versions), Korean, Chinese, Japanese, and
Portuguese.

No item is left as NEEDS CLARIFICATION. The three open stack questions were settled with
the author during planning (spec Clarifications) and in [research.md](research.md).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

Checked against constitution v1.0.0 and team directives
([team-context.md](team-context.md)).

| Gate | Pre-design | Post-design | How the design meets it |
|------|-----------|-------------|-------------------------|
| I. Typed code, checked by ty | PASS | PASS | `pipeline/` is fully annotated. Parsed YAML, JSONL, and HTTP or DB rows are narrowed to frozen dataclasses at the boundary (`Any` only there). The ty hook gains `--extra pipeline` so pipeline imports resolve. Amendment A1 adds `pipeline/` to Principle I's named paths. |
| II. ruff only | PASS | PASS | No new linters. `pipeline/` and `tests/pipeline/` are inside the existing ruff config. |
| III. Tests are the contract, GPU-free, offline | PASS | PASS | All tests are offline (see Testing). Postgres runs locally, which FR-027 allows; the only connection is loopback to that local server, and amendment A1 states in Principle III that this is allowed, so the gate does not rest on interpretation. A1 also extends Principle III's test rule to `pipeline/`. Determinism comes from seeded samples, an injected clock for rate limiting, and sorted processing order. The rebuild-equivalence and export byte-identity tests check known answers, not just that the code runs. |
| IV. KISS / YAGNI / DRY | PASS | PASS | No ORM, workflow engine, or Spark. Plain functions plus dataclasses. One authority per fact: exam registry and editions in `catalog.yaml`; decisions in `ledger.jsonl`; schema and data dictionary in `schema.sql`, whose column comments generate the dictionary; failure reason codes in one `StrEnum`, with a test checking the SQL `CHECK` list matches; text normalization in `corpus.py`, where the corpus guard's looser `fingerprint_text` is defined as the FR-010 `canonical_text` plus case-folding and whitespace removal, so the rules are not duplicated; concurrency in one write lock; the sample cap is one constant. Non-eligible export modes and Gaokao provincial papers are left out until there is a real use (YAGNI). |
| V. Structure / SOLID / LoD / CoC | PASS | PASS | One module per concern, so each has one reason to change: acquisition from official sources (`acquire.py`: catalog, discover, review, fetch, which all change when acquisition rules change), extraction, keys, translation, verification, and delivery (`publish.py`: snapshot and export, plus the dictionary and card a snapshot ships with). `rebuild` only calls the stages in order, so it lives in `__main__.py` with the rest of the dispatch, not in a stage module. If `acquire.py` approaches the 800-line limit, `fetch` moves to its own module as a justified file-count overrun. Keys are written only by `keys.py` and translations only by `translate.py` (FR-016), and a static test checks this. `BlobStore`, `Translator`, and `OcrEngine` are small protocols with substitutable implementations. `harness/` never imports `pipeline/`; a test checks this (FR-030). The harness loads exported banks with no code change (Open/Closed). Conventions: blobs live at `<root>/raw/<sha256>`, snapshots at `<root>/snapshots/<version>/`, and the CLI follows the stage names. |
| Quality gates (pre-commit, CI) | PASS | PASS | A new CI job, `pipeline`, runs on a Postgres 17 service container with pinned actions and `contents: read`, with no secrets. The core `pytest` job ignores `tests/pipeline/`. The corpus guard runs as a pre-commit hook and therefore in CI. |
| Project constraints: dependencies | **BLOCKED until A1 merges** | PASS (sequenced) | FR-032: the "heavyweight frameworks MUST NOT be added" clause is removed by amendment A1, which must land **before or with** the first pipeline dependency. None of the chosen packages is a named heavyweight framework, but the plan still sequences A1 first, as the spec requires. The core install stays GPU-free and unchanged. |
| Project constraints: separation, integrity, secrets | PASS | PASS | `pipeline/` adds data only and runs no experiments. Secrets (`ANTHROPIC_API_KEY`, AWS credentials, `PIPELINE_DSN`) come only from environment variables and are never logged. The DSN is redacted in errors. |
| Team: CDR-2026-021 SQL injection | PASS | PASS | Every query uses psycopg placeholders. DDL is a static `schema.sql`. A regression test sends `' OR 1=1 --` style payloads through edition and question IDs. |
| Team: CDR-2026-027 pytest patterns, CDR-2026-022 file sizes | PASS | PASS | Class-grouped tests, shared fixtures in `tests/pipeline/conftest.py`, parametrized reason-code and normalization tables, files under 800 lines. |

**Amendment A1** (governance PR, first in the series; FR-032, FR-033):

- Constitution, Project Constraints → Dependencies: delete the sentence "Heavyweight
  frameworks (LangChain, MLflow, W&B) MUST NOT be added." Keep Python 3.13, `uv`,
  `uv.lock`, `uv sync --locked`, the optional `model` extra, and the GPU-free core. Add:
  "Dataset-pipeline packages stay in the optional `pipeline` extra." Principle I adds
  `pipeline/` to its path list. Principle III extends "Every behavior change in
  `harness/`" to `harness/` and `pipeline/`, and adds: "Loopback connections to a locally
  run database are permitted; anything beyond the machine is not." Update the Sync Impact
  Report. The bump level is decided in that PR: removing a constraint and expanding
  guidance suggests MINOR (1.1.0), since no principle or gate is removed or redefined.
- CLAUDE.md: delete the "Do not add heavyweight dependencies" bullet, and reword
  differentiator #2 to: "The harness core is small enough to own end-to-end: no framework
  dependencies, readable in one sitting. The dataset pipeline is a separate, optional
  package with its own dependencies, so it adds no weight to the harness."

## Project Structure

### Documentation (this feature)

```text
specs/001-exam-data-aggregation/
├── plan.md              # This file
├── research.md          # Phase 0: decisions and rejected alternatives
├── data-model.md        # Phase 1: tables, view, state rules, identifiers
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── cli.md           # pipeline CLI: stages, flags, exit codes
│   ├── manifests.md     # catalog.yaml, ledger.jsonl, samples allowlist formats
│   └── outputs.md       # snapshot layout + suite question bank (questions.json) format
├── team-context.md      # team directives (before_specify / before_plan hooks)
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
pipeline/                      # NEW optional package; harness never imports it
├── __init__.py
├── __main__.py                # CLI dispatch (argparse → stage functions) and `rebuild`,
│                              #   which only calls the stages in order
├── catalog.yaml               # human-authored: exams, domains, rate limits, parse
│                              #   patterns, index pages, editions + evidence
├── ledger.jsonl               # machine-written, append-only decisions/events
├── schema.sql                 # DDL + questions_flat and integrity_violations views
│                              #   + COMMENTs (data dictionary)
├── store.py                   # Postgres: connect, apply schema, parameterized reconcile
├── blobs.py                   # BlobStore protocol; LocalBlobStore, S3BlobStore
├── manifests.py               # catalog.yaml and ledger.jsonl as typed data: readers,
│                              #   ledger replay, catalog validation
├── acquire.py                 # stages: catalog, discover, review, fetch
├── extract.py                 # stage: extract (text/OCR → questions, stimuli, failures)
├── keys.py                    # stage: key (sole writer of answer_keys)
├── translate.py               # stage: translate (sole writer of translations)
├── verify.py                  # hand-check + translation-review sampling and recording
├── publish.py                 # stages: snapshot (+ dictionary, card), export
├── corpus.py                  # normalization, canonical keys, fingerprints, repo guard
├── samples/allowlist.yaml     # openly licensed committed samples (initially empty)
└── fingerprints.txt           # winnowed corpus fingerprints (hashes only)

tests/pipeline/                # NEW; run by the CI `pipeline` job only
├── conftest.py                # Postgres schema-per-test, blob store, synthetic builders
├── test_acquire.py            # catalog completeness, ledger, review rules
├── test_store.py              # blob stores, Postgres store, CLI exit codes
├── test_fetch.py              # discover and fetch rules (allowlist, robots, rate limit)
├── test_extract.py            # extract, keys, translate, canonical dedupe, failures
├── test_publish.py            # snapshot counts, export byte-identity, harness smoke,
│                              #   rebuild equivalence, hand-check gating
└── test_guard.py              # corpus guard (3 failure cases), harness↛pipeline imports

# Modified (not new): pyproject.toml (pipeline extra, script, packages, ty hook extra),
# .pre-commit-config.yaml (ty --extra pipeline, corpus guard hook), .github/workflows/ci.yml
# (pipeline job), .gitignore (local blob root, exported banks), CLAUDE.md, constitution,
# TODO.md, README (dataset section).
```

**Structure Decision**: A single repository with a second top-level package, `pipeline/`,
next to `harness/`. The direction of dependence is one way: `pipeline/` may import
`harness.config` in tests to prove exports load, while `harness/` never imports `pipeline/`.
Stages are modules and are not split into a package per exam. Per-exam differences (item
markers, choice markers, key-table layout, column crops) are data in `catalog.yaml`, read
by one generic parser (Convention over Configuration). A per-exam parser module is added
only when an exam's layout cannot be expressed as config. That would be the second real
instance, and would be a justified overrun (YAGNI).

**File count (FR-034)**: 17 new files in `pipeline/` (counting `catalog.yaml`,
`ledger.jsonl`, `samples/allowlist.yaml`, and `fingerprints.txt`) plus 7 in
`tests/pipeline/` is **24, over the target of 20 by 4**. Implementation added three
files to the earlier estimate of 21, each forced by the 800-line limit (CDR-2026-022)
and single responsibility: `manifests.py` (typed manifests, split from the acquisition
stages when `acquire.py` reached 878 lines), `test_store.py`, and `test_fetch.py` (the
storage and network tests, split from `test_acquire.py`).
Justification for the PR: (a) `tests/pipeline/` needs its own `conftest.py` so the core CI
job can ignore the directory without importing pipeline-only dependencies; (b)
`fingerprints.txt` and `samples/allowlist.yaml` are data files required by FR-025 and
FR-026. Merges already made to get here: candidates, reviews, artifacts, and hand-checks
share one ledger; exams and editions share one catalog; the guard, normalization, and
fingerprinting share `corpus.py`; snapshot and export share `publish.py`. Merging further
would put two writers or two reasons to change in one module (FR-016, Principle V).

## Delivery sequence (one PR-sized commit series each)

1. **Governance (A1)**: constitution amendment, CLAUDE.md edits, TODO entries (done in
   planning). No code.
2. **Skeleton and catalog (P1)**: pyproject extra, `schema.sql`, `store.py`, `blobs.py`,
   `catalog.yaml` with all 66 cells, the `catalog`, `discover`, `review`, and `fetch`
   stages, the CI `pipeline` job, the corpus guard with its pre-commit hook, and
   `test_acquire.py` plus `test_guard.py`.
3. **Extraction and keys (P2)**: `extract.py`, `keys.py`, `test_extract.py` (extraction
   parts).
4. **Translation (P3)**: `translate.py` and its tests.
5. **Delivery (P4, P5)**: `publish.py`, `verify.py`, `test_publish.py`, and the README
   dataset section.
6. **Data runs (maintainer, outside CI)**: fill in the catalog, approve sources, fetch,
   extract, hand-check, snapshot. Results tables come only from these runs (prime
   directive).

## Risks carried into tasks

- **Legacy notebooks versus the corpus guard.** `legacy/*.ipynb` may hold SAT question text
  in cell outputs. If SAT practice tests are ingested, FR-026 would flag them, and
  `legacy/` must stay untouched. Task: run the guard against `legacy/` as soon as SAT
  fingerprints exist. If it finds hits, bring the conflict to the author; do not exempt
  `legacy/` silently.
- **Gaokao and NITE coverage.** These depend on what the official bodies actually publish.
  `not_released` with evidence is a valid result, and a thin catalog for these exams is
  expected, not a failure.
- **Japan's Common Test item structure.** Answers are per answer box (解答番号), and one
  printed question can hold several boxes. The plan treats each answer box as a question
  record (see data-model). If config cannot express this layout, it is the most likely
  first per-exam parser module.
- **OCR quality.** Scanned CJK papers run through Tesseract with a per-page mean confidence
  threshold. Pages below it become `ocr_low_confidence` failures, never silently wrong
  text. The threshold is one constant, set from the first hand-check.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| 24 new files, against the target of 20 (FR-034 SHOULD) | Separate test directory with its own conftest; two data files required by FR-025 and FR-026 | Putting pipeline tests in `tests/` would make the core CI job import pipeline-only dependencies or skip modules. Merging more modules would break FR-016's single writer or Principle V's single responsibility. |
| Postgres needed in tests | FR-027 requires the real store engine to run locally. The `questions_flat` view and reconcile SQL are the logic under test. | SQLite in tests would test a different SQL dialect from production, so the view's behavior would go untested. |
