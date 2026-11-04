---

description: "Task list for the Cross-National Exam Question Dataset (data aggregation) feature"
---

# Tasks: Cross-National Exam Question Dataset (Data Aggregation)

**Input**: Design documents from `specs/001-exam-data-aggregation/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md),
[team-context.md](team-context.md)

**Tests**: Required. FR-029 and constitution Principle III say every behavior ships with a
pytest test that fails without it. Within each story, write the tests first and confirm
they fail before you implement.

**Organization**: Tasks are grouped by user story, so each story can be built and tested as
its own increment. The story order follows the spec's priorities (P1 → P5).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependency on unfinished tasks)
- **[Story]**: Which user story the task belongs to (US1–US5)
- Every task names the exact file(s) it touches

## Ground rules for every task

These apply to every task below. They are listed once here instead of in each task.

- **Synthetic data only in tests.** Every fixture is built in code (`tests/pipeline/conftest.py`)
  from invented text such as `"Synthetic stem 7"`. Never copy real exam text into a tracked
  file (FR-025).
- **No network, cloud, keys, or GPU in tests** (FR-027). Use `httpx.MockTransport`, the
  local blob store or `botocore.stub.Stubber`, `FakeTranslator`, and `FakeOcrEngine`.
- **Parameterized SQL only** (FR-031, CDR-2026-021). Use psycopg placeholders, and use
  `psycopg.sql.Identifier` only for table or column names taken from a fixed in-code list.
- **Types**: annotate every function and module-level variable. Narrow parsed YAML, JSONL,
  HTTP, and DB rows to frozen dataclasses at the boundary (`Any` only there).
- **No question text in stdout, logs, ledger, catalog, or failure `detail`.** Use IDs,
  positions, and counts only.
- **Secrets** come from environment variables only. Never print or log them, and redact the
  DSN in error messages (FR-028).
- **Determinism**: process editions in sorted `edition_id` order, use seeded randomness, and
  inject the clock. Never read the wall clock in logic that feeds a hash or an ID.
- **File limits**: keep every file under 800 lines (CDR-2026-022). Do not create files the
  plan does not list (FR-034; the plan already justifies 21).
- Run `uv run --locked --extra dev --extra pipeline ruff check`, `ruff format --check`,
  and `ty check` before you mark a task done.

## Path Conventions

Single repository with a second top-level package: `pipeline/` next to `harness/`, and its
tests in `tests/pipeline/`. `harness/` never imports `pipeline/` (FR-030).

---

## Phase 1: Setup (Governance and Project Wiring)

**Purpose**: Land amendment A1 (FR-032, FR-033) **before** any pipeline dependency, then
wire up the optional package, CI, and hooks.

**⚠️ GATE**: T001–T002 must be committed (their own PR, or the first commits of the series)
before T003 adds any dependency. Until A1 merges, the old dependency rule still governs
(FR-032).

- [X] T001 Amend the constitution (A1) in `.specify/memory/constitution.md`: (a) in Project Constraints → Dependencies, delete the sentence "Heavyweight frameworks (LangChain, MLflow, W&B) MUST NOT be added." and add "Dataset-pipeline packages stay in the optional `pipeline` extra."; keep Python 3.13, `uv`, `uv.lock`, `uv sync --locked`, the `model` extra, and the GPU-free core wording unchanged; (b) in Principle I, change the path list "`harness/`, `data/`, and `tests/`" to "`harness/`, `pipeline/`, `data/`, and `tests/`"; (c) in Principle III, change "Every behavior change in `harness/`" to "Every behavior change in `harness/` or `pipeline/`", and add after the no-network bullet: "Loopback connections to a locally run database are permitted; anything beyond the machine is not."; (d) rewrite the Sync Impact Report header for the version change, proposing 1.1.0 (MINOR: a constraint removed and guidance expanded; no principle or gate removed or redefined), listing the modified sections and the CLAUDE.md follow-up; the bump level is confirmed in the amendment PR and recorded in its description (spec Dependencies); (e) set the footer to `**Version**: <confirmed version> | **Ratified**: 2026-11-03 | **Last Amended**: <merge date>`
- [X] T002 [P] Update `CLAUDE.md` (A1, FR-032, FR-033): delete the bullet "Do not add heavyweight dependencies (LangChain, MLflow, W&B)…"; replace differentiator #2 with the plan's text ("The harness core is small enough to own end-to-end: no framework dependencies, readable in one sitting. The dataset pipeline is a separate, optional package with its own dependencies, so it adds no weight to the harness."), keeping its portfolio/production sentence; in the vLLM bullet, replace "This preserves the no-heavyweight-deps rule" with "This keeps inference engines out of the harness core", since the old rule no longer exists
- [X] T003 Add the `pipeline` optional extra to `pyproject.toml`: `psycopg[binary]>=3.2`, `httpx`, `boto3`, `pyarrow`, `pdfplumber`, `pytesseract`, `openai` (lower bounds only; `uv.lock` pins); add `[project.scripts] pipeline = "pipeline.__main__:main"`; change `[tool.setuptools.packages.find] include` to `["harness*", "pipeline*", "legacy*"]`; then run `uv lock` and commit `uv.lock`
- [X] T004 Verify the core install is unchanged (FR-030, quickstart Scenario 7): run `uv sync --locked` and confirm with `uv pip list` that none of psycopg, httpx, boto3, pyarrow, pdfplumber, pytesseract is installed; then run `uv sync --locked --extra dev --extra pipeline` and confirm they are (depends on T003)
- [X] T005 [P] Add `.pipeline-data/` (default local blob root) and `exports/` (exported banks hold corpus text) to `.gitignore`, under a comment that explains both
- [X] T006 [P] Create the package skeleton: `pipeline/__init__.py` (docstring only, no imports, so `python -m pipeline.corpus` needs only stdlib and PyYAML; state that the harness never imports this package), `pipeline/ledger.jsonl` (empty file), `pipeline/fingerprints.txt` (empty file), and `pipeline/samples/allowlist.yaml` containing `samples: []` with a header comment pointing at contracts/manifests.md
- [X] T007 [P] Update `.pre-commit-config.yaml`: change the ty hook entry to `uv run --locked --extra dev --extra pipeline ty check`; add a local hook `corpus-guard` with `entry: uv run --locked python -m pipeline.corpus`, `language: system`, `pass_filenames: false`, `always_run: true`, with a comment that it needs only the core install (research R13)
- [X] T008 [P] Update `.github/workflows/ci.yml`: change the `test` job's step to `pytest -q --ignore=tests/pipeline`; add a `pipeline` job on `ubuntu-latest` with a `postgres:17` service (env `POSTGRES_PASSWORD: postgres`, `pg_isready` health check, port 5432), the same pinned `actions/checkout@v5` (with `fetch-depth: 0`, so the ledger test can reach `origin/main`; research R15) and `astral-sh/setup-uv@v10.0.1`, `uv sync --locked --extra dev --extra pipeline`, env `PIPELINE_TEST_DSN: postgresql://postgres:postgres@localhost:5432/postgres`, and `pytest tests/pipeline -q`; no secrets; keep the workflow-level `permissions: contents: read`; reword the header comment so it says the core jobs use the core set only and the `pipeline` job adds the optional extra plus a local Postgres

**Checkpoint**: Governance landed; `uv sync --locked` is unchanged; CI has a `pipeline` job (failing until tests exist is fine on a feature branch).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The schema, store, blob store, normalization, catalog and ledger readers, CLI
shell, test fixtures, and the corpus guard. Every story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests for the foundation (write first, confirm they fail)

- [X] T009 Create `tests/pipeline/conftest.py`: (a) a session fixture that reads `PIPELINE_TEST_DSN`, skips DB tests when it is unset, and calls `pytest.fail` when it is unset and `CI=true` (research R4); (b) a `db` fixture that creates a fresh schema `t_<uuid hex>`, runs `SET search_path` to it, applies `pipeline/schema.sql`, yields a psycopg connection, and drops the schema afterwards; (c) a `blob_store` fixture returning `LocalBlobStore(tmp_path / "blobs")`; (d) a `FakeClock` with `now()` and `sleep()` that advances virtual time; (e) builders, all from invented text, exposed as fixtures because test modules cannot import from conftest: `make_pdf(pages, *, image_pages=(), ruled_pages=()) -> bytes`, a hand-written PDF 1.4 writer with Helvetica text, optional image XObjects, and optional ruling lines (no new dependency), and `seed_edition(conn, …) -> str` for inserting an exam and edition directly; the `build_catalog(tmp_path, *, editions=…)` and `write_ledger(tmp_path, events)` builders are added with T023, where the catalog and ledger formats are first read; (f) `FakeOcrEngine(text_by_page, confidence)` and `FakeTranslator(responses | raises | empty)` are added with T038 and T046, when their protocols exist; (g) no `git_state` fixture: stages take `pipeline_version` as a parameter (the CLI passes `pipeline.store.pipeline_version()`), so tests pass a fixed clean or `-dirty` string and never depend on the developer's working tree; keep it under 800 lines
- [X] T010 [P] Write normalization tests in `tests/pipeline/test_guard.py` (class `TestNormalization`): a parametrized table for `canonical_text` (NFKC full-width → ASCII, punctuation of every P* category removed, whitespace runs collapsed to one space, ends stripped, **case kept**, `ice cream` ≠ `icecream`); `fingerprint_text` additionally case-folds and removes all whitespace; a pinned-difference test showing two strings that differ only in case get different `canonical_question_id` but the same fingerprint; `content_hash` is unchanged when only `pipeline_version` changes and changes when `print_order` changes; `canonical_json` uses sorted keys, `ensure_ascii=False`, and compact separators
- [X] T011 [P] Write blob-store tests in `tests/pipeline/test_acquire.py` (class `TestBlobStore`): `put` returns the SHA-256 and stores under `raw/<sha256>`; a second `put` of the same bytes is a no-op; an existing final file whose bytes do not match its name raises `BlobCorruptionError`; a simulated crash between temp write and `os.link` leaves only a temp file and no final file; `put_named` refuses to overwrite an existing name; `S3BlobStore.put` sends `IfNoneMatch="*"` and treats a 412 as "exists after hash check", using `botocore.stub.Stubber`
- [X] T012 [P] Write store tests in `tests/pipeline/test_acquire.py` (class `TestStore`): `apply_schema` is idempotent; `write_lock` on a second connection raises `LockHeld` while the first holds it, and the CLI maps that to exit code 4 with nothing written; `reconcile` reports inserted/updated/deleted counts and makes zero writes on an identical second call; a crash mid-edition (exception inside the transaction) leaves that edition's rows unchanged; SQL-injection regression: edition and question IDs such as `x' OR 1=1 --` and `x'); DROP TABLE questions; --` are stored and read back verbatim and no table is affected (CDR-2026-021); a connection error message never contains the DSN password
- [X] T013 [P] Write guard tests in `tests/pipeline/test_guard.py` (class `TestCorpusGuard`), each against a temporary git repo and a synthetic fingerprint file: the guard fails (exit 3) for (a) a synthetic corpus stem planted in a tracked `.md` file outside the allowlist, (b) the same text as `\uXXXX`-escaped CJK inside a `.json` value, (c) the text inside an `.ipynb` cell output, (d) HTML-entity-encoded text in an `.html` file, (e) synthetic MT English text, (f) a tracked `.pdf`, `.hwp`, `.parquet`, or `.mp3` outside the allowlist, (g) an allowlisted sample that pushes one exam over `SAMPLE_CAP_PER_EXAM`, (h) an allowlist entry with an empty `licence` or `licence_evidence` (SC-008); it passes for a clean repo and skips allowlisted samples and `pipeline/fingerprints.txt`; a winnowing test shows that any shared normalized run of 63 characters is detected and that a 23-character overlap is not required to be; plus `test_harness_never_imports_pipeline`, which parses every `harness/**/*.py` with `ast` and fails on any `import pipeline` or `from pipeline` (FR-030); plus `test_single_writers` (FR-016, research R15), which scans every `pipeline/*.py` and fails if SQL writing `answer_keys` (`INSERT INTO answer_keys`, `UPDATE answer_keys`, `DELETE FROM answer_keys`, or the table name passed to `insert_if_new`/`reconcile`) appears outside `pipeline/keys.py`, or SQL writing `translations` appears outside `pipeline/translate.py`

### Implementation for the foundation

- [X] T014 Write the tables in `pipeline/schema.sql` exactly as in data-model.md: `exams`, `exam_editions` (with `exam_id` FK → `exams` and the nullable `english_item_map`), `sources`, `artifacts`, `stimuli`, `questions`, `answer_keys`, `translations`, `extraction_failures`, `exam_checks`, `snapshots` (with `content_sha256`), with PKs, FKs, unique constraints (`answer_keys (question_id, labels, artifact_sha256)`; the `translations` unique index on `coalesce(artifact_sha256, mt_response_sha256)`), `retired boolean NOT NULL DEFAULT false` on `questions` and `stimuli`, and `CHECK` constraints for every enum (`exam_id`, `kind`, `status`, `status_reason`, `discovery_method`, `decision`, `fetch_outcome`, `role`, `modality`, `subject_kind`, `translation_source`, `stage`, `reason_code` with the 12 codes in data-model.md), the `edition_id` regex, `year BETWEEN 2015 AND 2025`, `cardinality(labels) >= 1`, and the per-status required columns (`release_date` and `official_url` when `ingested`, `status_reason` when `blocked`, `mt_*` when `mt`, `artifact_sha256` and `page` when `official`); use `CREATE … IF NOT EXISTS` so it applies idempotently; add a `COMMENT ON TABLE` and `COMMENT ON COLUMN` for **every** table and column, since they are the data dictionary (FR-022)
- [X] T015 Add the `questions_flat` view to `pipeline/schema.sql` (FR-017, data-model "View"): one row per non-retired question with every `questions` column; `exam_id`, `edition_kind`, `lineage`, `language` from `exam_editions`; the current key chosen by greatest `(is_erratum, published NULLS FIRST, artifact_sha256)`, with **no** key when the question has a `key`-stage `key_conflict` failure; the preferred translation among rows whose `source_content_hash` equals the current `content_hash` (official over mt, then greatest `translation_id`), or `translation_source = 'original'` with the original text when the edition language is `en`; the stimulus modality, its preferred English, and its translation provenance columns; `eligible` and `ineligible_reason`, checking the rules in the listed order (figure, table, audio, free response, fewer than 2 choices, no current key, more than one key label, key label not in current choices, no English, non-text stimulus, stimulus without English); then add the `integrity_violations(check_name, subject_id)` view with the four checks in data-model.md (`edition_without_output`, `key_not_official`, `english_missing`, `question_without_artifact`); add `COMMENT ON VIEW` and column comments for both views (depends on T014)
- [X] T016 [P] Implement `pipeline/corpus.py` normalization (stdlib only): `canonical_text`, `fingerprint_text` (defined as `canonical_text` + `casefold` + remove all whitespace, so the rules are not duplicated), `canonical_json`, `canonical_question_id` (`"cq_" + sha256(stem + "\x1f" + "\x1f".join(choices))[:20]` over canonical texts), `content_hash(fields)` over the R7 field set excluding `pipeline_version`, and the constant `SAMPLE_CAP_PER_EXAM = 50`; docstrings explain why identity is exact and detection is looser (makes T010 pass)
- [X] T017 Implement the corpus guard in `pipeline/corpus.py` (stdlib + PyYAML only): `winnow(text, k=24, w=40) -> set[str]` returning 16-hex-digit SHA-256 prefixes of selected k-grams; `build_fingerprints(texts) -> list[str]` (sorted, unique); `load_allowlist(path)` narrowing to a frozen dataclass; `scan(repo_root) -> list[GuardHit]` over `git ls-files -z`, skipping allowlisted paths and `pipeline/fingerprints.txt`, parsing `.json` and `.ipynb` and scanning every string value, decoding `\uXXXX` escapes and HTML entities in other text files, and refusing tracked `.pdf`, `.hwp`, `.parquet`, and audio files not in the allowlist; per-exam cap and licence checks; `main()` printing one line per hit (path and kind, never the matched text) and returning 3 on any hit; an `if __name__ == "__main__"` block (depends on T016; makes T013 pass)
- [X] T018 [P] Implement `pipeline/blobs.py`: a `BlobStore` Protocol (`put(data) -> str`, `get(sha256) -> bytes`, `exists(sha256) -> bool`, `put_named(path, data) -> None`, `get_named(path) -> bytes`, `exists_named(path) -> bool`); `LocalBlobStore` with temp file in the same directory, fsync, hash check, `os.link` publish, temp cleanup, and `BlobCorruptionError`; `S3BlobStore` with boto3 `put_object(IfNoneMatch="*")`; `open_blob_store(root: str | None)` reading `PIPELINE_BLOB_ROOT`, default `./.pipeline-data`, `s3://bucket/prefix` → S3 (makes T011 pass)
- [X] T019 Implement `pipeline/store.py`: `connect(dsn)` that wraps connection errors with the DSN redacted; `apply_schema(conn)` that runs `schema.sql` when the tables are missing; `write_lock(conn)` context manager using `pg_try_advisory_lock` on one fixed key, raising `LockHeld`; `reconcile(conn, table, scope, rows, *, retire=False) -> Counts` that inserts missing rows, updates rows whose `content_hash` changed, and deletes (or, with `retire=True`, sets `retired = true` on) rows no longer produced, all inside one transaction per scope and with table and column names from a fixed whitelist; `insert_if_new(conn, table, rows) -> int` using `ON CONFLICT DO NOTHING`; a `StageSummary` dataclass whose `line()` prints e.g. `extract: editions=12 inserted=840 updated=0 deleted=0 failures=+3/-0`; `pipeline_version(repo_root) -> str` returning `git rev-parse HEAD`, with a `-dirty` suffix when `git status --porcelain -- 'pipeline/*.py' pipeline/schema.sql` is non-empty (research R7), called once by the CLI and passed to every stage as a parameter, so tests supply a fixed string (depends on T014; makes T012 pass)
- [X] T020 Implement catalog and ledger readers in `pipeline/manifests.py` (the 800-line limit split typed manifests from the stages in `pipeline/acquire.py`): frozen dataclasses `Exam`, `Seed`, `ParseConfig`, `Edition`, `EvidenceItem`, `TranslationConfig`, `Catalog`; `load_catalog(path) -> Catalog` that narrows YAML and raises `CatalogError` on shape errors; ledger event dataclasses for `candidate`, `review`, `fetch`, `handcheck`, `translation_review`; `read_ledger(path)` that raises `LedgerError` on a duplicate or decreasing `seq`; `append_event(path, kind, fields, *, clock)` that must be called under the write lock, allocates `seq = last + 1`, writes canonical JSON plus a newline, and fsyncs; `ledger_is_prefix_extension(old: bytes, new: bytes) -> bool`; `source_id_for(url) = "src_" + sha256(url)[:16]` (depends on T016)
- [X] T021 Implement the CLI shell in `pipeline/__main__.py` (dispatch only): argparse with global `--catalog`, `--ledger`, `--exam`, `--edition`; one subparser per command in contracts/cli.md, each initially returning a "not implemented" usage error; a `main(argv) -> int` that maps `CatalogError`/integrity errors → 3, `LockHeld` → 4 with "another pipeline command is running", argparse errors → 2, anything else → 1, and redacts the DSN in any message; it reads `PIPELINE_DSN` and `PIPELINE_BLOB_ROOT` from the environment only
- [X] T022 Run `uv run python -m pipeline.corpus` on the real repository and confirm exit 0 with the empty `pipeline/fingerprints.txt` (quickstart Scenario 6, second command); run `uvx --with pre-commit-uv pre-commit run --all-files` and confirm the `corpus-guard` and ty hooks pass

**Checkpoint**: `pytest tests/pipeline -q -k "Normalization or BlobStore or Store or CorpusGuard"` passes against a local Postgres; the guard runs in pre-commit. User stories can now start.

---

## Phase 3: User Story 1 — Know what exists before extracting anything (Priority: P1) 🎯 MVP

**Goal**: A validated catalog of every edition of all six exams for 2015–2025, each marked
`ingested`, `not_released`, or `blocked` with evidence, plus the discover → review → fetch
acquisition path that produces that evidence under the allowlist, robots, and rate-limit
rules.

**Independent Test**: quickstart Scenarios 2 and 3. Build the catalog from a synthetic
66-cell `catalog.yaml` and synthetic ledger; every cell has ≥ 1 edition with one of three
statuses and evidence. Removing a cell, dropping evidence, or citing an unapproved source
exits 3 and writes nothing.

### Tests for User Story 1 (write first, confirm they fail)

- [X] T023 [US1] Write catalog validation tests in `tests/pipeline/test_acquire.py` (class `TestCatalogValidation`, parametrized): a full synthetic 66-cell catalog passes; each of these exits 3 with **zero** rows written and a message naming the problem: a missing `(exam, year)` cell, an edition with empty `evidence`, evidence citing a `pending` or `rejected` source, an `ingested` edition missing a `paper` or a `key` artifact (the message names the role), an exam with `publishes_keys: false` or no `keys_evidence` (FR-006), a `not_released` edition whose only evidence is `index_absence` from a seed without `complete_listing: true`, a `blocked` edition without `status_reason`, an `edition_id` that breaks the regex, a seventh exam ID (FR-001)
- [X] T024 [US1] Write catalog reconcile tests in `tests/pipeline/test_acquire.py` (class `TestCatalogStage`): `catalog` writes `exams` (six rows from the catalog's `exams` section), `exam_editions`, `sources` (latest `review` decides `decision`, latest `fetch` decides `fetch_outcome`), and `artifacts` (role and `published` from the review, `sha256`, `media_type`, and `bytes` from the fetch); a second run prints `inserted=0 updated=0 deleted=0`; changing one edition from `blocked` to `ingested` updates only that row; adding a Gaokao provincial edition (e.g. `gaokao-2020-beijing`) needs no schema change (FR-002); one key artifact serving two editions gives two `artifacts` rows with one blob; an official English version approved with `--role english` on the original edition adds an `artifacts` row and **no** `exam_editions` row, and does not satisfy the `paper` role requirement
- [X] T025 [US1] Write acquisition tests in `tests/pipeline/test_acquire.py` (classes `TestDiscover`, `TestReview`, `TestFetch`) with `httpx.MockTransport` and `FakeClock`: discover extracts links from a seed HTML page and a `sitemap.xml`, stores the seed page as a blob, records allowlisted links as pending `index_crawl` candidates and off-allowlist links as `rejected` with reason `non_allowlisted`; `--add-url` records a `manual` candidate; a second discover appends no event for a known `source_id`; review approve requires `--edition` and `--role`, `--role key_erratum` requires `--published`, and the latest review wins; repeating a review identical to the latest one appends nothing (FR-019); fetch never requests an unapproved or rejected candidate (the transport records zero calls); a robots-disallowed path → `refused_robots`; 401 or 403 → `login_required`; a redirect to a non-allowlisted host → `login_required`, not followed; a non-allowlisted approved URL → `refused_domain`; 5xx → `http_error`; two requests to one domain are ≥ `rate_limit_seconds` apart on the fake clock, including the robots.txt request; changed bytes at the same URL give a second blob and leave the first unchanged; refetching unchanged bytes appends no event (FR-019)
- [X] T026 [US1] Write ledger integrity tests in `tests/pipeline/test_acquire.py` (class `TestLedger`; the reader, append, prefix-extension, and source-id cases were written with T020, so this task adds the stage-level and git-based cases): `catalog` exits 3 on a duplicate or decreasing `seq`; `ledger_is_prefix_extension` accepts an appended file and rejects an edited or truncated one; a test that compares the working `pipeline/ledger.jsonl` with `git show $(git merge-base HEAD origin/main):pipeline/ledger.jsonl` using `ledger_is_prefix_extension` (an empty file when the ledger does not exist at that commit); it skips locally when `origin/main` is missing and fails when `CI=true` (research R15); no ledger line written by any stage contains a synthetic fixture stem

### Implementation for User Story 1

- [X] T027 [US1] Implement `validate_catalog(catalog, ledger_state) -> list[str]` in `pipeline/acquire.py`, enforcing every rule in data-model.md `exam_editions` → Validation and contracts/manifests.md, and returning all problems at once (makes T023 pass)
- [X] T028 [US1] Implement the `catalog` stage in `pipeline/acquire.py`: `run_catalog(conn, catalog_path, ledger_path) -> StageSummary` that applies the schema if needed, takes the write lock, loads and validates (writes nothing on failure), replays the ledger in `seq` order into in-memory `sources` and `artifacts` rows, then reconciles `exams` (from the catalog's `exams` section), and `exam_editions`, `sources`, and `artifacts` per edition in one transaction each (makes T024 pass)
- [X] T029 [US1] Implement the `discover` stage in `pipeline/acquire.py`: async `discover(catalog, ledger_path, blobs, client, clock, *, exam=None)` that fetches each seed URL through the per-domain limiter (T031), stores the page as a blob, extracts `href`s with `html.parser` and `<loc>`s with `xml.etree.ElementTree`, resolves relative URLs, lower-cases the host, and appends `candidate` events only for new `source_id`s; `add_manual_url(url, edition_id)`; one entry point so a search-API method can be added later (research R1)
- [X] T030 [US1] Implement the `review` stage in `pipeline/acquire.py`: `review(ledger_path, source_id, *, approve, edition_id, role, published, reason)` that prints URL, domain, and found-on page, validates the arguments (approve needs edition and role; `key_erratum` needs `published`; reject needs reason), and appends one `review` event, or nothing and prints `review: unchanged` when the source's latest review has the same decision, edition, role, date, and reason (FR-019)
- [X] T031 [US1] Implement the `fetch` stage in `pipeline/acquire.py`: a `DomainLimiter` (one `asyncio.Lock` per domain, minimum interval from the exam's `rate_limit_seconds` or the default 5 s, injected clock); robots.txt fetched once per domain through the limiter and parsed with `urllib.robotparser` using the user agent `LLM-Stereotypes-dataset/1.0 (+<repository URL>)`; `httpx.AsyncClient(follow_redirects=False)` with manual redirect following only within the exam's allowlist; outcomes per research R6; a `fetch` event appended only when the outcome or `sha256` differs from the source's latest fetch event; blobs written through `BlobStore.put` (makes T025 pass)
- [ ] T032 [US1] Wire `catalog`, `discover` (with `--add-url URL --edition E`), `review CANDIDATE_ID --approve|--reject …`, and `fetch` into `pipeline/__main__.py`; each writing command takes the write lock for its whole run and ends with one summary line (makes T026 and the CLI-level cases of T023–T025 pass)
- [ ] T033 [US1] Author the exam registry in `pipeline/catalog.yaml` (contracts/manifests.md): `version: 1`, `translation: {model: claude-sonnet-5-5, base_url: https://api.anthropic.com/v1/}`, `defaults: {rate_limit_seconds: 5}`, and one `exams` entry for each of `sat`, `psychometric`, `csat`, `gaokao`, `common_test`, `enem` with `name`, `body`, `language`, `publishes_keys: true`, `domains`, `seeds`, a `parse` block, and, only for an exam with official English versions (the Psychometric exam), an optional `parse_english` block; the maintainer must confirm every domain and seed URL from the official body's own site (do not guess), and `keys_evidence` source IDs are filled in during the data run (T073); leave `editions: []` with a comment that `catalog` exits 3 until all 66 cells are authored, which is the intended FR-003 gate

**Checkpoint**: US1 is complete when `pytest tests/pipeline/test_acquire.py -q` passes. The catalog alone is a deliverable (an honest coverage map), even before any extraction.

---

## Phase 4: User Story 2 — Extract questions with official keys and full provenance (Priority: P2)

**Goal**: For every `ingested` edition, original-language questions and stimuli with
modality flags, canonical-question links, and full provenance; official keys as label sets
with current-key ranking; and a reason-coded failure for everything that cannot be
extracted or keyed.

**Independent Test**: quickstart Scenario 4 (extraction and key parts). Run `extract` and
`key` on a synthetic ingested edition built from `minimal_pdf` plus a synthetic key PDF;
every question has a valid edition, complete provenance, and either a current key or a
`no_official_key` failure; a second run changes nothing.

### Tests for User Story 2 (write first, confirm they fail)

- [ ] T034 [US2] Write parser tests in `tests/pipeline/test_extract.py` (class `TestParsePages`) on synthetic `Page` objects (no PDF): items split by the configured `item_marker`, choices by `choice_markers`, sections tracked, `print_order` from 1 in page and reading order, so item `10` comes after item `9`; a two-column `columns: 2` crop reads the left column first; a shared text passage becomes one stimulus referenced by every question that uses it; image objects in an item's area set `has_figure`; ruling lines set `has_table`; an item with no choices sets `is_free_response`; the catalog's audio mapping sets `has_audio`; failures with reason codes `item_split_failed`, `choices_not_found`, and `stimulus_unresolved`; failure `detail` never contains item text
- [ ] T035 [US2] Write extract-stage tests in `tests/pipeline/test_extract.py` (class `TestExtractStage`) against `db` and `blob_store`: a `minimal_pdf` paper produces questions whose `source_url`, `artifact_sha256`, `page`, `pipeline_version`, and `content_hash` are all set (SC-002); an image-only page goes through `FakeOcrEngine`, and below `OCR_MIN_CONFIDENCE` gives `ocr_low_confidence`; an `application/x-hwp` artifact gives `unsupported_format`; corrupt bytes give `artifact_unreadable`; `not_released` and `blocked` editions are never extracted (FR-004); an `english` artifact is never read by `extract` and adds no `questions` rows; an ingested edition whose paper yields zero questions gets one `item_split_failed` failure with `subject_id = edition:<edition_id>`, and `integrity_violations` reports `edition_without_output` when that failure is removed by hand (SC-002); identical items in two editions share one `canonical_question_id`, while a one-character or a case-only difference does not (FR-010); changing only `pipeline_version` writes nothing; a question removed on re-extraction is `retired = true` and its key rows remain, and it is un-retired if it reappears; a second run prints `inserted=0 updated=0 deleted=0`; results are identical with `--workers 1` and `--workers 4`
- [ ] T036 [US2] Write key-stage tests in `tests/pipeline/test_extract.py` (class `TestKeyStage`): keys parsed by `key_table` from a `key`-role artifact are stored as sorted label sets citing that artifact's `sha256` and `page`, including when the key is published as a separate file with its own date and URL; a multi-answer key is stored in full with `eligible = false`; a question missing from the key file gets `no_official_key` and no key row (FR-012); a key label not among the question's choices gives `key_label_mismatch`; a `key_erratum` with a `published` date becomes current and the old row remains; an undated ordinary key is stored with `published` null and is current; two equal-rank keys with different labels give `key_conflict` and the view has no current key for that question; a key-like table inside a `paper` artifact is ignored; a key row inserted by hand citing a `paper` artifact appears in `integrity_violations` as `key_not_official`; a second run inserts nothing
- [ ] T037 [US2] Write schema-consistency tests in `tests/pipeline/test_extract.py` (class `TestSchemaContract`): the `ReasonCode` members equal the `reason_code` `CHECK` list parsed from `pipeline/schema.sql`; a parametrized table over `questions_flat` checks each `ineligible_reason` value and that the first failing rule in the listed order is the one reported

### Implementation for User Story 2

- [ ] T038 [US2] In `pipeline/extract.py`, define `ReasonCode(StrEnum)` with the 12 codes in data-model.md; frozen dataclasses `Word`, `Page` (text words with positions, image boxes, ruling lines, `ocr_confidence`), `ExtractedQuestion`, `ExtractedStimulus`, `Failure`; an `OcrEngine` Protocol and `TesseractOcrEngine` (pytesseract with `kor`, `chi_sim`, `jpn`, `heb`, `por`, `eng`, returning words and mean confidence); the constant `OCR_MIN_CONFIDENCE = 60` (Tesseract's 0–100 scale) with a docstring marking it provisional until the first hand-check sets it in a reviewed commit (research R8, plan risk)
- [ ] T039 [US2] Implement `read_pages(data, media_type, ocr) -> list[Page] | Failure` in `pipeline/extract.py`: pdfplumber for PDFs with a text layer (words with positions, `page.images`, `page.lines`/`rects` for ruling lines), the `OcrEngine` for pages without one, `unsupported_format` for HWP and other non-PDF papers, and `artifact_unreadable` for parse errors
- [ ] T040 [US2] Implement `parse_pages(pages, parse, edition) -> ExtractedEdition` in `pipeline/extract.py` as a pure function: column crops, section detection, item and choice splitting by the catalog regexes, stimulus grouping (`{edition_id}:{section}:stim-{n}`), modality flags, `question_id = "{edition_id}:{section}:{item}"`, `print_order`, `canonical_question_id` and `content_hash` from `pipeline/corpus.py`, and reason-coded failures; per-exam layout differences stay data in `catalog.yaml` (plan Structure Decision) (makes T034 pass)
- [ ] T041 [US2] Implement the `extract` stage in `pipeline/extract.py`: `run_extract(conn, catalog, blobs, ocr, *, workers, exam=None, edition=None) -> StageSummary` that selects only `ingested` editions in sorted order, maps only `paper` artifacts (never `english`) through `concurrent.futures.ProcessPoolExecutor` (pure per-artifact work), sorts results by artifact hash, writes an edition-level `item_split_failed` when an edition yields zero questions and has no whole-edition failure yet, and reconciles `stimuli` and `questions` with `retire=True` and `extraction_failures` (stage `extract`) per edition in one transaction, setting `pipeline_version` from `pipeline.store.pipeline_version` (makes T035 pass)
- [ ] T042 [US2] Implement `pipeline/keys.py`, the only writer of `answer_keys` (FR-016): `parse_key_artifact(pages, parse) -> dict[(section, item), labels]` for artifacts with role `key` or `key_erratum` only; `run_key(conn, catalog, blobs, …) -> StageSummary` that inserts key rows with `insert_if_new` (citing `artifact_sha256`, `page`, `is_erratum`, `published`, `pipeline_version`), re-checks every current key against current choice labels (`key_label_mismatch`), detects equal-rank disagreement (`key_conflict`), records `no_official_key`, and reconciles its own failures (stage `key`) per edition; it never infers or repairs a key (makes T036 pass)
- [ ] T043 [US2] Wire `extract` (with `--workers N`, default CPU count) and `key` into `pipeline/__main__.py`, each under the write lock with a summary line (makes T037 and the CLI-level cases of T035–T036 pass)

**Checkpoint**: US1 and US2 work together: `pytest tests/pipeline/test_acquire.py tests/pipeline/test_extract.py -q -k "not Translate"` passes.

---

## Phase 5: User Story 3 — English for every question, with official text preferred (Priority: P3)

**Goal**: Every non-English question and text stimulus gets an English translation row or a
failure. Official English editions are paired first; machine translation fills the rest,
with model, reported revision, prompt hash, and a cached response blob so rebuilds run
offline.

**Independent Test**: quickstart Scenario 4 (translation part). A synthetic non-English exam
with two editions, one with an official English version attached as an `english` artifact
and one without: the first edition's questions get `official` English and the other's get
`mt` with model, revision, and prompt hash recorded. The English version adds no edition
and no `questions` rows.

### Tests for User Story 3 (write first, confirm they fail)

- [ ] T044 [US3] Write official-pairing tests in `tests/pipeline/test_extract.py` (class `TestOfficialPairing`): the official English version is a `minimal_pdf` `english` artifact of the original edition, parsed in memory with `parse_english` (or `parse` when absent); after `translate`, `questions` and `exam_editions` row counts are unchanged and its text exists only in `official` `translations` rows (FR-016); items pair by identical `(section, item)` keys; the edition's `english_item_map` overrides numbering; a different key set fails the whole edition with one `translation_pairing_mismatch` failure and no official rows; a per-item mismatch in choice count, modality flags, or stimulus grouping fails only that item; stimuli pair too; official rows cite the English artifact's `sha256` and `page`
- [ ] T045 [US3] Write translate-stage tests in `tests/pipeline/test_extract.py` (class `TestTranslateStage`) with `FakeTranslator`: no `mt` row is written for a subject with an `official` row for the same `source_content_hash` (FR-015); `mt` rows record `mt_model`, `mt_model_revision` (the model ID the fake response reports), `mt_prompt_hash`, and `mt_response_sha256`; the response blob is stored under the request key (canonical JSON of `base_url`, model, temperature, prompt hash, and the exact unnormalized text), so text differing only in case or a different `base_url` makes a new request; a second run makes zero translator calls and inserts nothing; `--offline` with a cache miss makes no call and exits 1 naming the subject ID; empty output → `mt_empty_output`; a raised error → `mt_failed`; English-original editions get no translation rows and the view reports `translation_source = 'original'`; the preferred translation is official over mt, then the latest; after a question's content changes, its old translation is not preferred and a new one is made

### Implementation for User Story 3

- [ ] T046 [US3] In `pipeline/translate.py`, define the `Translator` Protocol (`translate(request) -> TranslationResponse` with English stem, choices, and reported model ID), the constant `PROMPT_TEMPLATE`, `prompt_hash()`, `request_key(request) -> str`, and `OpenAICompatibleTranslator` using the `openai` client with `base_url` and model from `catalog.yaml`, temperature 0, and the API key from `ANTHROPIC_API_KEY` (never logged); add a docstring on why the harness `ApiBackend` is not reused (research R10)
- [ ] T047 [US3] Implement official pairing in `pipeline/translate.py`: `load_english(edition, artifacts, blobs, catalog, ocr) -> ExtractedEdition | Failure`, which reads each `english` artifact with `pipeline.extract.read_pages` and `parse_pages` (using `parse_english` merged over `parse`) in memory and stores nothing in `questions` or `stimuli`; `pair_official(original_rows, english, english_item_map) -> (rows, failures)` with the set-equality check (a) and the per-item structural check (b) from research R10 (makes T044 pass)
- [ ] T048 [US3] Implement the `translate` stage in `pipeline/translate.py`, the only writer of `translations` (FR-016): `run_translate(conn, catalog, blobs, translator, *, offline, …) -> StageSummary` that writes official rows first, then for each remaining non-English question and text stimulus whose current `content_hash` has no row, reads the cached response at `mt/<request_key>` or calls the translator and stores the raw response with no-overwrite `put_named`, writes the `mt` row with `insert_if_new`, and reconciles its own failures (stage `translate`) per edition (makes T045 pass)
- [ ] T049 [US3] Wire `translate` (with `--offline`) into `pipeline/__main__.py` under the write lock with a summary line; it must not require `ANTHROPIC_API_KEY` when every response is cached

**Checkpoint**: US1–US3 work together: `pytest tests/pipeline/test_extract.py -q` passes.

---

## Phase 6: User Story 4 — Export a suite-ready question bank (Priority: P4)

**Goal**: A named snapshot (Parquet plus manifest), exam verification status from
hand-checks, and an `export` that writes a byte-identical, harness-loadable
`questions.json` from that snapshot only.

**Independent Test**: quickstart Scenario 5 (export parts). Snapshot a synthetic dataset,
export one exam twice (byte-identical, records the snapshot version), load the result with
`harness.config.load_suite` beside the tracked `conditions.yaml`, and run `MockBackend`
with no harness file modified.

### Tests for User Story 4 (write first, confirm they fail)

- [ ] T050 [US4] Write snapshot-core tests in `tests/pipeline/test_publish.py` (class `TestSnapshotCore`): the first snapshot is `snap-0001` and the next `snap-0002`; it writes every table except `snapshots`, plus `questions_flat.parquet` and `exam_status.parquet`, and writes `manifest.json` last with version, git SHA, catalog and ledger SHA-256, row counts, and file hashes; a failure injected before the manifest leaves no manifest, and a retry reuses the same version; a published version is never modified; `snapshot` runs `catalog` first, so a `handcheck` event appended just before is included; a second `snapshot` with nothing changed prints `snapshot: unchanged (snap-0001)`, writes no file and no registry row, and a change to one row makes `snap-0002` (FR-019); a `pipeline_version` ending in `-dirty` exits 3 with nothing written; any `integrity_violations` row exits 3 with nothing written and prints counts per check, never text
- [ ] T051 [US4] Write verification tests in `tests/pipeline/test_publish.py` (class `TestVerify`): `verify draw` with a fixed seed picks the same 50 `question_id`s every time, and all of them for an exam with fewer than 50 (FR-024), and writes a CSV under `<blob root>/review/` with IDs, content hashes, key labels, and page references, but no question text; `verify record` appends a `handcheck` event; a reused seed exits 3; a CSV whose hashes no longer match current questions exits 3; status is `unchecked` with no event, `verified` after a full pass, `unverified` after any failure, and back to `unchecked` when a sampled question's content hash or key later changes; recovery from `unverified` requires a new seed, every previously failed question changed, and those questions included (`forced: true`), which `verify draw` adds automatically; translation review draws `min(30, N)` per language and records 0–3 scores; `exam_checks` rows are rebuilt by `catalog` from the ledger
- [ ] T052 [US4] Write export tests in `tests/pipeline/test_publish.py` (class `TestExport`): only `eligible` rows are written; choices are relabelled `A)`, `B)`, … in original order with `answer` mapped and `original_labels` kept; a text-stimulus question's `question` is stimulus English + `"\n\n"` + stem English, with `stimulus_id` and a separate `stimulus_translation` provenance block; questions are ordered by `(edition_id, print_order)` with `index` 1..N; two exports from one snapshot are byte-identical and record its version; `answers_beyond_d` is printed and stored in provenance; after a key erratum and a new snapshot, the new export shows only the new key (US4 scenario 3); an `unverified` exam exits 3; an exam missing from `exam_status.parquet` fails; an `unchecked` exam exports with `"verification": "unchecked"`; `edition_kinds` counts `administered` and `practice` separately (FR-005); export reads no Postgres (it runs with `PIPELINE_DSN` unset)
- [ ] T053 [US4] Write the harness smoke test in `tests/pipeline/test_publish.py` (class `TestHarnessSmoke`, SC-007): export a synthetic exam to `tmp_path/suite/questions.json`, copy the tracked `suites/stereotype_threat/conditions.yaml` beside it, call `harness.config.load_suite`, then `harness.runner.run_suite` with `MockBackend` and `n_samples=2`, and assert the run writes `manifest.json`, `responses.jsonl`, `scores.csv`, and `stats.json`; record the SHA-256 of every file under `harness/` before the test and assert they are identical afterwards (so unrelated uncommitted harness edits do not make it fail)

### Implementation for User Story 4

- [ ] T054 [US4] Extend the `catalog` stage in `pipeline/acquire.py` to replay `handcheck` ledger events into `exam_checks` (`exam_id`, `seed`, `event_seq`, `passed`, `n_items`, `n_failed`, `items`) with `reconcile` (data-model write ownership)
- [ ] T055 [US4] Implement `pipeline/verify.py`: `status(conn, exam_id) -> Literal["unchecked", "verified", "unverified"]` from `exam_checks` plus current questions and keys, including the stale and recovery rules (research R14); `draw(conn, exam_id, seed, blobs, *, kind, language=None)` using `random.Random(seed)` over sorted IDs, drawing `min(50, N)`, adding previously failed questions after a failure, and writing the CSV with `put_named`; `record(conn, csv_path, ledger_path)` under the write lock with the seed-reuse and stale-hash refusals; translation-review draw and record for `min(30, N)` per language with 0–3 scores (makes T051 pass)
- [ ] T056 [US4] Implement the snapshot core in `pipeline/publish.py`: `snapshot(conn, catalog_path, ledger_path, blobs) -> str` that follows research R11 in order: refuses a dirty `pipeline_version` (exit 3), runs `catalog`, opens one `REPEATABLE READ, READ ONLY` transaction, refuses (exit 3) if `integrity_violations` has rows, computes `content_sha256` (catalog and ledger SHA-256s, `schema.sql` SHA-256, and a per-table digest over rows in primary-key order) and returns the latest version unchanged when it matches that manifest's, then picks the next `snap-NNNN` above the highest version with a manifest, writes each table plus `questions_flat` and `exam_status` (from `verify.status`) with `pyarrow` to `snapshots/<version>/*.parquet`, takes row counts in the same transaction, writes `manifest.json` last with no-overwrite, and inserts the `snapshots` registry row (makes T050 pass)
- [ ] T057 [US4] Implement `export(blobs, exam_id, snapshot_version, out_path) -> ExportSummary` in `pipeline/publish.py` exactly per contracts/outputs.md: read only `questions_flat.parquet` and `exam_status.parquet`, refuse `unverified` (exit 3) or a missing exam, filter `eligible`, sort, relabel, build the stimulus prefix and the provenance blocks, and write JSON with `sort_keys=True`, `ensure_ascii=False`, `indent=2`, and a trailing newline, with no timestamps; return the question count and `answers_beyond_d` (makes T052 and T053 pass)
- [ ] T058 [US4] Wire `verify draw|record|status`, `snapshot`, and `export --exam X --eligible --snapshot V [--out PATH]` into `pipeline/__main__.py`; `verify status` and `export` take no lock; `export` without `--eligible` is a usage error (exit 2); the default output is `exports/<exam>/questions.json`; export prints its question count and `answers_beyond_d`

**Checkpoint**: US1–US4 work: an exported synthetic bank loads into the harness and passes the mock smoke test with zero harness changes.

---

## Phase 7: User Story 5 — Reproducible, documented, versioned snapshots (Priority: P5)

**Goal**: Snapshots whose row counts are proven to match the live store, delivered with a
generated data dictionary and dataset card; refreshed corpus fingerprints; and a `rebuild`
that reproduces every per-question content hash from stored inputs.

**Independent Test**: quickstart Scenario 5 (rebuild and documentation parts). Rebuild a
synthetic dataset into an empty schema from the same catalog, ledger, and blobs; 100% of
content hashes and every table's row count match; the snapshot carries a dictionary and a
card.

### Tests for User Story 5 (write first, confirm they fail)

- [ ] T059 [US5] Write delivery tests in `tests/pipeline/test_publish.py` (class `TestSnapshotDelivery`): manifest counts equal the Parquet counts and the live counts for every table (SC-006); a Parquet file tampered with before readback makes `snapshot` exit 3 with no manifest written; `data_dictionary.md` lists every table and column in `schema.sql`, including `exams`, and a test fails if any column lacks a `COMMENT ON`; `dataset_card.md` has the six sections of contracts/outputs.md, a 66-cell coverage matrix split by status and kind, `statement` and `index_absence` evidence reported separately, Gaokao provincial papers and other-language official versions listed as known gaps, failure counts by reason code, translation-review scores as observed with no threshold, MT provenance and official/MT share per exam, and the `answers_beyond_d` limitation
- [ ] T060 [US5] Write fingerprint-refresh tests in `tests/pipeline/test_publish.py` (class `TestFingerprintRefresh`): after `snapshot`, `pipeline/fingerprints.txt` (pointed at a temp path in the test) is sorted, unique, lower-case 16-hex lines covering original stems, choices, stimuli, and all English translations, official and MT; the guard (`pipeline.corpus.scan`) then flags a tracked file holding one synthetic exported question
- [ ] T061 [US5] Write the rebuild-equivalence test in `tests/pipeline/test_publish.py` (class `TestRebuild`, SC-005, FR-020): run every stage into schema A, then `rebuild --schema B --compare-schema A` from the same catalog, ledger, and blobs with translation `--offline`; assert 100% of `(question_id, content_hash)` pairs and `canonical_question_id`s match, every table's row count matches, the rebuild made zero translator calls and zero network calls, and a deliberate change to one blob makes the comparison report exactly that question

### Implementation for User Story 5

- [ ] T062 [US5] Add readback verification to `snapshot` in `pipeline/publish.py`: after writing the Parquet files, read each back with `pyarrow`, compare its row count with the transaction count, and raise an integrity error (exit 3) before the manifest is written if any differs (makes the count cases of T059 pass)
- [ ] T063 [US5] Implement `data_dictionary(conn) -> str` in `pipeline/publish.py`, built from `pg_description` for every table, view, and column, and write `data_dictionary.md` into the snapshot directory before the manifest
- [ ] T064 [US5] Implement `dataset_card(conn, catalog, ledger, export_stats) -> str` in `pipeline/publish.py` with the six sections in contracts/outputs.md, built only from store queries, ledger events, and `card_notes`; it states the standing contamination note and that within-condition comparisons survive contamination while absolute scores do not mean much; it never contains invented numbers, and empty sections say "no data yet"; its known-gaps section lists Gaokao provincial papers and official versions in languages other than the original and English (research R10); write `dataset_card.md` into the snapshot directory before the manifest (makes T059 pass)
- [ ] T065 [US5] Add the fingerprint refresh to `snapshot` in `pipeline/publish.py`: after the registry row, compute `pipeline.corpus.build_fingerprints` over all original stems, choices, stimulus texts, and translation texts in the snapshot, and rewrite `pipeline/fingerprints.txt` (path injectable for tests) (makes T060 pass)
- [ ] T066 [US5] Implement `rebuild(dsn, schema, catalog_path, ledger_path, blobs, *, compare_schema=None) -> RebuildReport` in `pipeline/__main__.py`, beside the dispatch, since it only calls the stage functions in order (plan Constitution Check V): it runs `catalog → extract → key → translate --offline` into the given schema and, with `compare_schema`, prints per-question content-hash differences and per-table count differences; wire it as `rebuild --schema S [--compare-schema C]` (makes T061 pass)

**Checkpoint**: All five stories work: `pytest tests/pipeline -q` passes, twice in a row with identical results (SC-009).

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, cross-story checks, and validation of the whole series.

- [ ] T067 [P] Add a cross-stage logging test in `tests/pipeline/test_publish.py` (class `TestNoTextLeaks`): run every stage on the synthetic dataset with `capsys` and `caplog`, and assert no synthetic stem, choice, or stimulus string appears in stdout, stderr, or logs, and no secret value set in the environment (`ANTHROPIC_API_KEY`, DSN password) appears either (FR-028)
- [ ] T068 [P] Add a "Dataset pipeline" section to `README.md` framed as evaluation infrastructure (CLAUDE.md): what the two datasets are, the stage diagram (catalog → discover → review → fetch → extract → key → translate → snapshot → export), committed inputs vs private outputs, the corpus guard, how to install the `pipeline` extra and run the quickstart, and the limitations (contamination, five-choice scoring TODO, multi-answer not exported, Gaokao provincial gap); any coverage or results table is a `TODO: filled only from an executed data run` marker (prime directive)
- [ ] T069 [P] Confirm `TODO.md` still holds both high-priority entries the plan added (five-plus-choice scoring and multi-answer export) and that their wording matches the final export behavior (`answers_beyond_d`, single-answer filter); edit `TODO.md` only if the wording drifted
- [ ] T070 Run quickstart Scenarios 1–7 from `specs/001-exam-data-aggregation/quickstart.md` in order on a local Postgres 17, including Scenario 1 twice and once with networking off, and Scenario 7 on a core-only install; fix any failure before continuing
- [ ] T071 Run `uvx --with pre-commit-uv pre-commit run --all-files` and `uv run pytest -q --ignore=tests/pipeline`, and confirm that every core harness test still passes and that `wc -l pipeline/*.py tests/pipeline/*.py` shows no file at or over 800 lines
- [ ] T072 Count new files with `git diff --name-status --diff-filter=A main -- pipeline tests/pipeline` and put the FR-034 justification from plan.md (24 files, 4 over the target, and why: the 800-line limit forced `manifests.py`, `test_store.py`, and `test_fetch.py`) in the pull request description; if `fetch` was split out of `acquire.py` (plan Constitution Check V), add that file and its reason too

---

## Phase 9: Maintainer Data Runs (outside CI; never simulated)

**Purpose**: plan delivery step 6 and quickstart Scenario 8. The maintainer runs these on
real sources. Every number reported anywhere comes only from these runs (prime directive).
An agent may prepare commands and review diffs, but must not invent catalog evidence,
source decisions, hand-check verdicts, or translation scores.

- [ ] T073 Run `pipeline discover` for each exam and the needed `pipeline discover --add-url …`; review every candidate with `pipeline review`, approving only official-body URLs; approve an official English version with `--role english` on its original edition, never as a new edition; fill `keys_evidence` for each exam in `pipeline/catalog.yaml` (FR-006, FR-007)
- [ ] T074 Author all editions for the 66 cells in `pipeline/catalog.yaml` with real evidence: `ingested` with release date and official URL, `not_released` with an official `statement` or `index_absence` from a `complete_listing: true` seed, or `blocked` with its reason; record lineage changes (SAT 2016 redesign and digital, Center Test → Common Test 2021) and SAT practice tests as `kind: practice`; run `pipeline fetch` and then `pipeline catalog` until it exits 0 (FR-003, SC-001)
- [ ] T075 Fill each exam's `parse` block in `pipeline/catalog.yaml` from its real layout and run `pipeline extract`, `pipeline key`, and `pipeline translate` per exam; if an exam's layout (most likely the Common Test answer boxes) cannot be expressed as config, stop and raise a per-exam parser module with the author as a justified file-count overrun (plan risk)
- [ ] T076 As soon as the first `pipeline snapshot` has refreshed `pipeline/fingerprints.txt` with SAT text, run `uv run python -m pipeline.corpus` and check `legacy/*.ipynb`; if it reports hits, bring the conflict to the author, and do not exempt or edit `legacy/` silently (plan risk)
- [ ] T077 Run `pipeline verify draw` (`min(50, N)` per exam, recorded seed) and `pipeline verify record`, and the 30-per-language translation review; set `OCR_MIN_CONFIDENCE` in `pipeline/extract.py` from the first hand-check results; follow the recovery rule for any `unverified` exam (FR-024, SC-003, SC-004)
- [ ] T078 Run `pipeline snapshot` and `pipeline export --exam X --eligible --snapshot V` for each exam with eligible questions, then run the harness mock smoke test on each export (SC-007); commit only `pipeline/catalog.yaml`, `pipeline/ledger.jsonl`, and `pipeline/fingerprints.txt`, never `exports/` or the blob root; replace the README TODO markers with numbers from these runs only

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: T001–T002 first (governance gate), then T003 → T004; T005–T008 can run alongside T003.
- **Foundational (Phase 2)**: depends on Setup; blocks every user story.
- **US1 (Phase 3)**: depends on Foundational.
- **US2 (Phase 4)**: depends on Foundational. Its tests build `artifacts` rows through the `catalog` stage, so it also depends on T028 (catalog stage) from US1.
- **US3 (Phase 5)**: depends on US2 (it translates extracted questions and stimuli).
- **US4 (Phase 6)**: depends on US2 for questions and keys; its export tests use English text, so it also needs US3, or fixtures inserted directly into `translations`.
- **US5 (Phase 7)**: depends on US4 (it extends `snapshot`); `rebuild` uses every stage from US1–US3.
- **Polish (Phase 8)**: depends on US1–US5.
- **Data runs (Phase 9)**: depend on everything above being merged; maintainer-only.

### User Story Dependencies

The stories form a chain because each consumes the previous stage's tables:
US1 (catalog) → US2 (questions, keys) → US3 (English) → US4 (snapshot, export) → US5 (proof and documents).
Each story is still independently **testable**: its tests build their inputs through
synthetic fixtures, and its checkpoint can be validated before the next story starts.

### Within Each User Story

- Tests first; confirm they fail for the right reason.
- Dataclasses and protocols → pure functions → stage function → CLI wiring.
- Tasks in the same file run in order (no [P]).

### Parallel Opportunities

- Setup: T002 alongside T001; T005, T006, T007, T008 together.
- Foundational tests: T010, T011, T012, T013 together after T009 (T011 and T012 share `test_acquire.py` but are separate classes; if one person writes both, do them in sequence).
- Foundational implementation: T016 and T018 together; T014 → T015 → T019 in sequence (same schema file, then the store that applies it); T017 after T016 (same file).
- Across stories, very little runs in parallel: most US2–US5 tasks share `test_extract.py`, `test_publish.py`, `publish.py`, or `__main__.py`. Polish tasks T067, T068, T069 can run together.

---

## Parallel Example: Phase 2 (Foundational)

```bash
# After T009 (conftest.py) exists, write these test classes together:
Task: "T010 Normalization tests in tests/pipeline/test_guard.py"
Task: "T011 Blob-store tests in tests/pipeline/test_acquire.py (TestBlobStore)"
Task: "T013 Corpus-guard tests in tests/pipeline/test_guard.py (TestCorpusGuard)"

# Then implement independent modules together:
Task: "T016 Normalization in pipeline/corpus.py"
Task: "T018 BlobStore protocol and implementations in pipeline/blobs.py"
```

## Parallel Example: Setup

```bash
Task: "T005 .gitignore entries"
Task: "T006 pipeline/ skeleton and data files"
Task: "T007 .pre-commit-config.yaml hooks"
Task: "T008 .github/workflows/ci.yml pipeline job"
```

---

## Implementation Strategy

### MVP First (User Story 1 only)

1. Phase 1: Setup, with governance A1 landed first.
2. Phase 2: Foundational.
3. Phase 3: US1. The catalog and acquisition path.
4. **Stop and validate**: quickstart Scenarios 2 and 3 pass, and the guard runs in pre-commit.
   The coverage map with `not_released` vs `blocked` evidence is useful on its own.

### Incremental Delivery (matches plan "Delivery sequence")

1. Governance PR (T001–T002).
2. Skeleton and catalog PR (T003–T033): plan step 2.
3. Extraction and keys PR (T034–T043): plan step 3.
4. Translation PR (T044–T049): plan step 4.
5. Delivery PR (T050–T072): plan step 5.
6. Data runs (T073–T078): plan step 6, maintainer only.

Each PR is a series of small, imperative commits (CLAUDE.md conventions), e.g. "Add the
exam catalog schema", "Enforce the fetch allowlist and robots rules".

---

## Notes

- [P] tasks touch different files and depend on no unfinished task.
- [USn] maps a task to its user story for traceability.
- Commit after each task or logical group; stop at any checkpoint to validate.
- Never fill a result table, coverage number, or verdict from anything but an executed run.
