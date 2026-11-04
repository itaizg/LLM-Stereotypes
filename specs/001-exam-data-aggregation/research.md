# Research: Cross-National Exam Question Dataset

Phase 0 output for [plan.md](plan.md). Each entry gives the decision, why it was chosen,
and the alternatives considered. R1–R3 were confirmed by the author during planning (spec
Clarifications, planning session).

## R1. Candidate URL discovery

- **Decision**: Discovery has two methods, and both produce *pending* candidates that need
  approval (FR-007):
  1. `index_crawl`: fetch the official past-paper listing pages and `sitemap.xml` that
     `catalog.yaml` lists for each exam (seed URLs, authored and committed by the
     maintainer), then extract links with `html.parser`. Links on a non-allowlisted domain
     are recorded as `rejected` candidates automatically (spec edge case). Seed pages are
     stored as blobs, so an index crawl that shows no paper for an edition can serve as
     `not_released` evidence.
  2. `manual`: `pipeline discover --add-url URL --edition E` records a URL the maintainer
     found by browsing or by any search engine of their choice.
  Discovery sits behind one function signature, so a search-API method can be added later
  without touching other stages.
- **Rationale**: Google's own overview page states that the Custom Search JSON API "is
  closed to new customers" and is discontinued on January 1, 2027. The allowlist holds
  about 10 known domains, so crawling their official index pages is more complete than
  search, deterministic, and free. It also avoids sending search traffic about copyrighted
  papers through a third party.
- **Alternatives considered**: Google CSE (closed to new customers, sunset 2027-01-01);
  Vertex AI Search (Google's suggested replacement for up to 50 domains, but needs a GCP
  project, indexing setup, and billing, too heavy for about 10 sites); Brave Search API
  with `site:` queries (works, but adds a paid key for little gain over crawling; can be
  added later as a third method).

## R2. Batch engine

- **Decision**: No Spark and no pandas. Extraction runs per artifact through
  `concurrent.futures.ProcessPoolExecutor`, with results sorted by artifact hash before
  writing. Snapshots read Postgres inside one `REPEATABLE READ` transaction and write
  Parquet with `pyarrow`.
- **Rationale**: At 10⁴–10⁵ questions (tens of MB of text), every table fits in memory many
  times over. The only CPU-heavy work is PDF parsing and OCR, which is independent per
  artifact. Deduplication is not a batch job: the canonical ID is a hash computed when a row
  is written (R7). Spark would add a JVM to CI and tests for no measured benefit, and could
  not be defended in an interview at this size. Keeping stage functions pure per artifact
  leaves a seam where Spark could later map them if the corpus grew by orders of magnitude.
- **Alternatives considered**: PySpark in local mode (the brief's choice; JVM cost, no
  benefit at this scale); pandas plus pyarrow (the brief's fallback; pandas adds nothing
  that pyarrow and SQL do not already cover).

## R3. Questions with five or more choices

- **Decision**: Export every eligible single-answer question whatever its choice count.
  Choices are relabelled `A)`, `B)`, `C)`, … in their original order, and the key labels
  are mapped the same way. The export prints the number of questions whose answer is
  beyond `D`, and the dataset card states the limitation. A high-priority TODO covers
  making harness scoring choice-count-aware.
- **Rationale**: This was the author's decision. FR-012 stays as written, and the harness
  stays unchanged (SC-007). Until the TODO lands, a gold answer `E` or later is scored as an
  extraction failure, which under-reports accuracy. That is why the export and the card say
  so explicitly instead of letting it pass silently.
- **Alternatives considered**: exporting only questions with four or fewer choices (loses
  most ENEM and CSAT items); changing the harness first (spec SC-007 forbids it within this
  feature).

## R4. Live store and test database

- **Decision**: Postgres 17 through psycopg 3, with plain SQL in `schema.sql` and no ORM.
  Tests get a fresh schema each through `SET search_path`, on a Postgres reached by
  `PIPELINE_TEST_DSN`. In CI that is a `postgres:17` service container. Locally it is any
  Postgres the maintainer runs, e.g. Homebrew `postgresql@17`. When the DSN is unset, DB
  tests skip locally. When `CI=true`, a missing DSN is a hard failure, so the required
  check can never pass by skipping.
- **Rationale**: The brief fixes Postgres. The `questions_flat` view and the reconcile
  statements are logic under test, so tests must run on the same engine as production
  (FR-027 allows a local database). Plain parameterized SQL satisfies CDR-2026-021
  directly and keeps every query readable.
- **Alternatives considered**: SQLAlchemy (an extra layer with nothing to buy at about 10
  tables); SQLite or DuckDB in tests (dialect drift from production); testcontainers (needs
  Docker on every developer machine; a CI service container is simpler); `pytest-postgresql`
  (needs `pg_ctl` binaries on PATH; no simpler than a DSN).

## R5. Raw file and snapshot storage

- **Decision**: A `BlobStore` protocol with `put(data) -> sha256`, `get(sha256)`,
  `exists(sha256)`, and `put_named(path, data)` for snapshots. `LocalBlobStore` uses a
  directory and `S3BlobStore` uses boto3. The layout is the same for both:
  `raw/<sha256>` and `snapshots/<version>/…`. `put` never overwrites: an existing key is a
  no-op after a hash check. On S3 that is `If-None-Match: *`; locally it is an exclusive
  create. S3 tests use `botocore.stub.Stubber`, which is offline and adds no dependency.
- **Rationale**: Content addressing gives immutability and dedupe for free (FR-009). The
  protocol keeps tests off AWS (FR-027) and keeps S3 an adapter, not a dependency of stage
  logic (Principle V, dependency inversion).
- **Alternatives considered**: `moto` (another test dependency, when Stubber suffices for
  a thin adapter); `fsspec` (a general abstraction for a two-implementation need).

## R6. Fetching rules

- **Decision**: `httpx.AsyncClient` with one `asyncio.Lock` per domain and a minimum
  interval between requests. The default is 5 s, and an exam can override it in
  `catalog.yaml`. The clock is injected so tests are deterministic. robots.txt is fetched
  once per domain, through the same limiter, and parsed with `urllib.robotparser` using the
  project's user agent (`LLM-Stereotypes-dataset/1.0 (+repository URL)`, with no personal
  contact details). Refusal outcomes are recorded in the ledger as `fetch` events:
  `refused_domain`, `refused_robots`, `login_required` (401, 403, or a redirect to a host
  outside the allowlist), `not_approved`, and `http_error`. Responses that redirect
  off-allowlist are not followed.
- **Rationale**: This meets FR-008 literally, makes every refusal auditable, and keeps the
  network out of tests through `httpx.MockTransport`.
- **Alternatives considered**: `aiohttp` (equivalent, but httpx's MockTransport makes
  offline tests simpler); `curl` (the brief allows it only for recorded debugging).

## R7. Identifiers, hashes, normalization

- **Decision**:
  - `edition_id = "{exam}-{year}-{sitting}"`, for example `csat-2019-main`,
    `gaokao-2021-national-i`, `sat-2016-practice-03`. These are authored in the catalog.
  - `question_id = "{edition_id}:{section}:{item}"`, where `item` is the printed item
    number. For the Common Test it is the answer-box number (解答番号), so each box is one
    question record.
  - `normalize(text)`: Unicode NFKC, then `casefold`, then remove every character of
    Unicode category P\* (punctuation) and Z\* or whitespace. It lives in one function
    in `corpus.py`.
  - `canonical_question_id = "cq_" + sha256(normalize(stem) + "\x1f" + "\x1f".join(normalize(c) for c in choices))[:20]`,
    an exact match after normalization (FR-010).
  - `content_hash = sha256(canonical JSON of {edition_id, section, item, stem, choices,
    modality flags, stimulus_id, artifact_sha256, page})`. It deliberately **excludes**
    `pipeline_version` (the git SHA), so an unrelated commit does not change the hashes and
    rebuilds reproduce them (FR-020). The git SHA is still stored as provenance.
- **Rationale**: Every ID can be derived from catalog data and extracted content, so rebuilds
  reproduce them. Canonical JSON (`sort_keys`, `ensure_ascii=False`, compact separators)
  removes serializer ambiguity.
- **Alternatives considered**: UUIDs (not reproducible); fuzzy dedupe with MinHash
  (rejected in the spec: near-matches must not link).

## R8. Text extraction and OCR

- **Decision**: `pdfplumber` for PDFs with a text layer. It gives character positions, so
  config-defined column crops handle two-column layouts. Pages with no text layer go to an
  `OcrEngine` protocol, implemented with `pytesseract` using language packs `kor`,
  `chi_sim`, `jpn`, `heb`, `por`, and `eng`. A page whose mean word confidence is below
  one threshold constant becomes an `ocr_low_confidence` failure. Item and choice
  splitting uses per-exam regex patterns from `catalog.yaml`: item markers such as
  `^\d+\.`, `QUESTÃO \d+`, or `問\d+`, and choice markers such as `①–⑤`, `A–E`, or
  `(1)–(4)`. Pages that hold figures (image objects in the item's area) or tables (ruling
  lines) set the modality flags. Audio artifacts set `has_audio` on the questions the
  catalog maps to them. HWP-only artifacts become `unsupported_format` failures.
- **Rationale**: pdfplumber is MIT-licensed (on pdfminer.six) and layout-aware. Tesseract is
  the standard offline OCR with all six scripts. Config-driven patterns avoid six
  near-duplicate parser modules (DRY, CoC).
- **Alternatives considered**: PyMuPDF (fast, but AGPL); `pypdf` (weak layout
  information); cloud OCR (breaks offline rebuilds and adds a service); a parser module
  per exam from the start (YAGNI until config demonstrably cannot express a layout).

## R9. Answer keys

- **Decision**: `keys.py` parses only artifacts whose catalog role is `key` or
  `key_erratum`, using the per-exam key-table patterns. It stores each key as a sorted set
  of the question's *original* choice labels. It writes `no_official_key` and
  `key_label_mismatch` failures (a label not among the question's choices) and never
  infers a key. An erratum artifact carries its own `published` date in the catalog. The
  current key is the one from the latest-published key artifact, with ties broken by
  insertion order, chosen in the view.
- **Rationale**: FR-012, FR-013, FR-016, and the corrected-key edge case. With an
  append-only design plus a derived current key, nothing is ever overwritten.

## R10. English text and machine translation

- **Decision**:
  - English-original questions (SAT, and NITE's English edition when it is catalogued as an
    edition in its own right) use their original text as English in the view. No
    translation row is written, because the text would be stored twice (DRY).
  - Official translations: a catalog edition with `translation_of: <edition_id>` and
    `language: en` is paired item by item, using the same `(section, item)`. If the two
    editions have different item counts, a `translation_pairing_mismatch` failure is
    written and nothing is paired.
  - MT: a `Translator` protocol. The production implementation calls an OpenAI-compatible
    chat endpoint through the `openai` client at temperature 0. The default is
    Anthropic's OpenAI-compatible endpoint with the model set in `catalog.yaml`
    (`translation.model`, initially `claude-sonnet-5-5`). It records the requested model,
    the model ID the response reports (as `mt_model_revision`), and
    `mt_prompt_hash = sha256(prompt template)`. Each raw response is stored as a blob under
    a request key `sha256(model|prompt_hash|normalized source)`. A rebuild reads that blob
    instead of calling the API, so rebuilds are deterministic and offline.
  - The harness's `ApiBackend` is not reused. Its contract (letter answers, `max_tokens=50`,
    content only) does not expose the reported model ID, and changing it would be a
    harness change.
- **Rationale**: FR-014 and FR-015. MT outputs become stored inputs, so FR-020 holds even
  though the API is not deterministic.
- **Contamination note for the dataset card**: if a model from the same family as the MT
  model is later evaluated on MT text, the translations may favour it. The card records
  the MT model for this reason.

## R11. Snapshots, versions, row counts

- **Decision**: `pipeline snapshot` opens one `REPEATABLE READ, READ ONLY` transaction,
  writes every table plus the materialized `questions_flat` and the derived exam
  verification status to `snapshots/snap-NNNN/*.parquet`, and records `manifest.json`. The
  manifest holds the version, git SHA, ledger SHA-256, catalog SHA-256, and per-table row
  counts taken inside the same transaction. It then reads the Parquet files back and fails
  if any count differs (FR-021, SC-006). It generates `data_dictionary.md` from the
  `COMMENT ON` text in `schema.sql`, and `dataset_card.md` from coverage queries plus the
  per-exam notes in `catalog.yaml`. Finally it refreshes `pipeline/fingerprints.txt`. The
  version is sequential (`snap-0001`, …) and recorded in a `snapshots` table.
- **Rationale**: Taking counts inside the snapshot transaction makes "matches the live store
  for that version" exact without a versioned store. Generating the dictionary from schema
  comments keeps one source of truth.

## R12. Export determinism

- **Decision**: Export reads only `snapshots/<version>/questions_flat.parquet`. It sorts by
  `(edition_id, section, item)` and assigns `index` 1..N in that order. It writes JSON with
  `sort_keys=True`, `ensure_ascii=False`, `indent=2`, and a trailing newline. It includes
  the snapshot version and no timestamps. An exam whose status is `unverified` is refused
  with exit code 3. `unchecked` exams are exported, with `"verification": "unchecked"` in
  the bank's provenance (spec FR-024 excludes only `unverified`).
- **Rationale**: FR-023 byte identity. A test exports twice and compares bytes.

## R13. Corpus guard (FR-025, FR-026)

- **Decision**: CI has no access to the private corpus, so the guard compares tracked files
  against **fingerprints**, not text. It uses k-gram winnowing (Schleimer, Wilkerson &
  Aiken 2003) over `normalize()`d text with k = 24 characters and window w = 40, so any
  shared normalized run of at least 63 characters is guaranteed to be caught. Short CJK
  stems are covered because k counts characters, not words. Fingerprints are the first 16
  hex digits of the SHA-256 of each selected k-gram, stored sorted and unique in
  `pipeline/fingerprints.txt`. The corpus covers original stems, choices, stimuli, and
  official translations. The guard (`python -m pipeline.corpus`, standard library and
  PyYAML only) scans `git ls-files` text files, skipping allowlisted samples and the
  fingerprint file itself. It fails when it finds a fingerprint hit, a sample over the cap
  (`SAMPLE_CAP_PER_EXAM = 50`, one constant), or an allowlist entry without
  `licence` and `licence_evidence`. It runs as a pre-commit hook, and therefore in CI.
- **Rationale**: Hashes are committable (brief rule 5) and cannot practically be read back
  as text. Winnowing gives a guaranteed detection length instead of a heuristic one.
  SC-008's three failure cases are planted in tests against a synthetic corpus.
- **Alternatives considered**: exact whole-stem hashes (miss partial copies); a Bloom
  filter (smaller, but adds code and false positives); running the guard only on the
  maintainer's machine (FR-026 needs an automated check in CI).

## R14. Hand-check and translation review (FR-024, SC-003, SC-004)

- **Decision**: `pipeline verify draw --exam X --seed S` samples 50 current questions with
  `random.Random(S)` over the sorted `question_id`s. It writes a private CSV outside git
  (the default is `<blob root>/review/`) with source page references for the maintainer.
  `pipeline verify record <csv>` appends a `handcheck` ledger event holding the exam, seed,
  sampled IDs with their content hashes, and per-item pass/fail for stem, choices, and key.
  Exam status is derived from the ledger:
  - `unchecked` when there is no event;
  - `unverified` when the latest event has any failure;
  - `verified` when the latest event passed in full.
  A passing event after a failure counts only if its seed has not been used before and its
  sampled content hashes come from a re-extraction, i.e. at least one hash differs from the
  failing event's. Translation review samples 30 per language in the same way and records
  a 0–3 adequacy score per item: 0 wrong meaning, 1 major error, 2 minor error,
  3 accurate. The mean and distribution are published as observed, with no threshold
  (SC-004).
- **Rationale**: The ledger holds IDs, hashes, and verdicts but no text, so results survive
  a rebuild and can be audited.

## R15. Dependency placement and CI wiring

- **Decision**: In pyproject, add the `pipeline` extra listed in plan.md and
  `[project.scripts] pipeline = "pipeline.__main__:main"`, and add `"pipeline*"` to package
  discovery. The ty hook becomes
  `uv run --locked --extra dev --extra pipeline ty check`. CI changes: the existing `test`
  job runs `pytest --ignore=tests/pipeline`. A new `pipeline` job runs on a
  `postgres:17` service with `uv sync --locked --extra dev --extra pipeline` and
  `pytest tests/pipeline`, with `PIPELINE_TEST_DSN` pointing at the service. A test checks
  that no module under `harness/` imports `pipeline` (FR-030).
- **Rationale**: The core install and the harness CI jobs stay byte-for-byte the same in
  what they install (FR-030). Tesseract is not installed in CI because OCR is faked in tests.
