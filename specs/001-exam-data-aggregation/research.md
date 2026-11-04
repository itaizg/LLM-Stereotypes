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
     stored as blobs. A paper missing from a page is "not found", **not** "not released":
     a gap in a listing may just be an incomplete page. It counts as `not_released`
     evidence (type `index_absence`) only when the maintainer has marked that seed
     `complete_listing: true` in the catalog, i.e. the page is the body's official,
     complete archive for that exam. Otherwise `not_released` needs an official
     `statement`. The dataset card reports the two evidence types separately, so a reader
     can tell "the body said so" from "the official archive has no such paper".
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
  no-op after a hash check. On S3 that is `If-None-Match: *`, and an S3 PUT is atomic.
  Locally, `put` writes to a temporary file in the same directory, fsyncs it, checks its
  hash, then publishes it with `os.link` to the final name, which fails if the name exists,
  and removes the temporary file. A crash therefore leaves at most a stray temporary file,
  never a partial file at a content-addressed path. An existing final file whose hash does
  not match is reported as corruption, not skipped. S3 tests use `botocore.stub.Stubber`,
  which is offline and adds no dependency.
- **Rationale**: Content addressing gives immutability and dedupe for free (FR-009). The
  protocol keeps tests off AWS (FR-027) and keeps S3 an adapter, not a dependency of stage
  logic (Principle V, dependency inversion).
- **Alternatives considered**: `moto` (another test dependency, when Stubber suffices for
  a thin adapter); `fsspec` (a general abstraction for a two-implementation need).

## R6. Fetching rules

- **Decision**: `httpx.AsyncClient` with one `asyncio.Lock` per domain and a minimum
  interval between requests. The default is 5 s, and an exam can override it in
  `catalog.yaml`. That lock covers one process. Two `fetch` processes cannot run at once,
  because every writing command holds the pipeline write lock (R16), so the per-domain
  interval holds across invocations too. The clock is injected so tests are deterministic. robots.txt is fetched
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
  - `canonical_text(text)`, exactly FR-010: Unicode NFKC, remove every character of
    Unicode category P\* (punctuation), collapse each run of whitespace (Z\* and control
    whitespace) to one space, and strip both ends. There is **no** case-folding, and
    whitespace is collapsed, not removed, so `Ice cream` and `ice cream`, or `ice cream`
    and `icecream`, stay distinct.
  - `canonical_question_id = "cq_" + sha256(canonical_text(stem) + "\x1f" + "\x1f".join(canonical_text(c) for c in choices))[:20]`,
    an exact match after FR-010 normalization.
  - `fingerprint_text(text)`, for the corpus guard only (R13): `canonical_text`, then
    `casefold`, then remove all whitespace. Detection should be more forgiving than
    identity: a copy that changes case or line breaks must still be caught, while two
    questions that differ only in case must not be linked. Both functions live in
    `corpus.py`, and a parametrized test pins the difference.
  - `content_hash = sha256(canonical JSON of {edition_id, section, item, print_order,
    stem, choices, modality flags, stimulus_id, artifact_sha256, page})`. It deliberately **excludes**
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
  `key_label_mismatch` failures (a label not among the question's current choices) and
  never infers a key. An erratum artifact must carry a `published` date. An ordinary key
  artifact may have none, since many key files are undated, and its rows store a null
  date. The view chooses the current key by `(is_erratum, published NULLS FIRST,
  artifact_sha256)`, so an erratum always wins over the ordinary key and the order depends
  only on artifact data, never on insertion order. When rows of equal
  `(is_erratum, published)` rank disagree on labels, `keys.py` writes `key_conflict`, and
  the question has no current key until a maintainer resolves the sources.
- **Rationale**: FR-012, FR-013, FR-016, and the corrected-key edge case. With an
  append-only design plus a derived current key, nothing is ever overwritten.

## R10. English text and machine translation

- **Decision**:
  - English-original questions (SAT, and NITE's English edition when it is catalogued as an
    edition in its own right) use their original text as English in the view. No
    translation row is written, because the text would be stored twice (DRY, FR-016).
    This is an interpretation of FR-014: "English text MUST be stored only as translation
    records" is read as applying to English that is not the original language, since
    FR-010 already puts an English-original edition's text in the question record. The
    view labels it `translation_source = original`, so queries treat all English
    uniformly. If the author prefers the literal reading, the alternative is one
    `translation_source = original` row per question that copies the text, with no other
    design change.
  - Official translations: a catalog edition with `translation_of: <edition_id>` and
    `language: en` is paired item by item. Matching item counts are not enough, because an
    English edition can reorder items or sections. A pair is written only when (a) both
    editions have exactly the same set of `(section, item)` keys, and (b) each pair agrees
    in structure: the same number of choices, the same modality flags, and the same
    stimulus grouping. Any item that fails (b) gets a `translation_pairing_mismatch`
    failure and no official row. If (a) fails, the whole edition gets one failure and
    nothing is paired. A catalog edition may give an explicit `item_map` when the official
    English numbering differs. The translation review sample (R14) then checks meaning,
    which structure cannot.
  - MT: a `Translator` protocol. The production implementation calls an OpenAI-compatible
    chat endpoint through the `openai` client at temperature 0. The default is
    Anthropic's OpenAI-compatible endpoint with the model set in `catalog.yaml`
    (`translation.model`, initially `claude-sonnet-5-5`). It records the requested model,
    the model ID the response reports (as `mt_model_revision`), and
    `mt_prompt_hash = sha256(prompt template)`. Each raw response is stored as a blob under
    a request key: the SHA-256 of the canonical JSON of everything that defines the
    request, namely `base_url`, model, temperature, prompt hash, and the **exact** source
    text (stem and choices, unnormalized). Text that differs only in case or punctuation, or
    a request sent to a different endpoint, therefore never reuses a cached response. The
    response blob is written with no-overwrite semantics (R5), so the first response stored
    for a request key is the only one ever used. A rebuild reads that blob instead of
    calling the API, so rebuilds are deterministic and offline.
  - The harness's `ApiBackend` is not reused. Its contract (letter answers, `max_tokens=50`,
    content only) does not expose the reported model ID, and changing it would be a
    harness change.
- **Rationale**: FR-014 and FR-015. MT outputs become stored inputs, so FR-020 holds even
  though the API is not deterministic.
- **Contamination note for the dataset card**: if a model from the same family as the MT
  model is later evaluated on MT text, the translations may favour it. The card records
  the MT model for this reason.

## R11. Snapshots, versions, row counts

- **Decision**: `pipeline snapshot` runs under the pipeline write lock (R16), so two
  snapshots, or a snapshot and a writing stage, never overlap. It works in this order:
  1. It runs the `catalog` stage first, so `sources`, `artifacts`, and `exam_checks`
     reflect the ledger file it is about to hash. A `verify record` made just before a
     snapshot is therefore never left out.
  2. It picks the next version, `snap-NNNN`, one above the highest version with a
     published manifest.
  3. It opens one `REPEATABLE READ, READ ONLY` transaction and writes every table except
     the `snapshots` registry (including `exam_checks`), plus the materialized
     `questions_flat` and the derived exam verification status, to
     `snapshots/snap-NNNN/*.parquet`. Per-table row counts are taken inside the same
     transaction.
  4. It reads the Parquet files back and fails if any count differs (FR-021, SC-006).
  5. It generates `data_dictionary.md` from the `COMMENT ON` text in `schema.sql`, and
     `dataset_card.md` from coverage queries plus the per-exam notes in `catalog.yaml`.
  6. It writes `manifest.json` **last**, with no-overwrite semantics. A version exists only
     once its manifest exists. The manifest holds the version, git SHA, ledger SHA-256,
     catalog SHA-256, row counts, and the hash of every file.
  7. It then inserts the `snapshots` row and refreshes `pipeline/fingerprints.txt`.
  If a run fails before step 6, the version has no manifest, so a retry reuses the same
  version number and overwrites its partial files. Published versions are never touched.
- **Rationale**: Taking counts inside the snapshot transaction makes "matches the live store
  for that version" exact without a versioned store. Generating the dictionary from schema
  comments keeps one source of truth.

## R12. Export determinism

- **Decision**: Export reads only two files of one snapshot:
  `snapshots/<version>/questions_flat.parquet` for questions and `exam_status.parquet` for
  the exam's verification status. Both come from the same snapshot transaction. It sorts by
  `(edition_id, print_order)`, which is numeric printed order, and assigns `index` 1..N in
  that order. It writes JSON with
  `sort_keys=True`, `ensure_ascii=False`, `indent=2`, and a trailing newline. It includes
  the snapshot version and no timestamps. An exam whose status is `unverified` is refused
  with exit code 3. `unchecked` exams are exported, with `"verification": "unchecked"` in
  the bank's provenance (spec FR-024 excludes only `unverified`).
- **Rationale**: FR-023 byte identity. A test exports twice and compares bytes.

## R13. Corpus guard (FR-025, FR-026)

- **Decision**: CI has no access to the private corpus, so the guard compares tracked files
  against **fingerprints**, not text. It uses k-gram winnowing (Schleimer, Wilkerson &
  Aiken 2003) over `fingerprint_text()` output (R7) with k = 24 characters and window
  w = 40, so any
  shared normalized run of at least 63 characters is guaranteed to be caught. Short CJK
  stems are covered because k counts characters, not words. Fingerprints are the first 16
  hex digits of the SHA-256 of each selected k-gram, stored sorted and unique in
  `pipeline/fingerprints.txt`. The corpus covers original stems, choices, stimuli, and
  **all** English translations, official and MT, since MT English is derived from
  copyrighted text too. The guard (`python -m pipeline.corpus`, standard library and
  PyYAML only) scans `git ls-files`, skipping allowlisted samples and the fingerprint file
  itself:
  - **Text files** are fingerprinted after decoding. `.json` and `.ipynb` files are parsed,
    and every string value is scanned, so CJK text stored as `\uXXXX` escapes (the
    default of `json.dumps`) is caught. Other text files also have `\uXXXX` escapes and
    HTML entities decoded before scanning. A test plants escaped CJK text.
  - **Binary files** cannot be fingerprinted reliably, so the guard refuses the ones that
    can carry papers: any tracked `.pdf`, `.hwp`, `.parquet`, or audio file is a failure
    unless it is in the sample allowlist. Other binaries, such as the committed result
    plots, are allowed.
  It fails when it finds a fingerprint hit, a refused binary, a sample over the cap
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
  sampled IDs with their content hashes and current key labels, and per-item pass/fail for
  stem, choices, and key. `verify record` runs under the pipeline write lock (R16) and
  refuses (exit 3) a CSV whose seed is already recorded for that exam, checking under the
  lock, so two concurrent records cannot both claim one seed. It also refuses a CSV whose
  hashes no longer match the current questions, i.e. one drawn before a re-extraction.
  Exam status is derived from the ledger and the current store:
  - `unchecked` when there is no event, or when the latest event passed but any sampled
    question's current content hash or key labels differ from the recorded ones (the
    check is stale, and a new sample is needed before export can rely on it);
  - `unverified` when the latest event has any failure;
  - `verified` when the latest event passed in full and is not stale.
  **Recovery from `unverified`** follows FR-024 and closes the gap where a fresh random
  sample simply misses the bad items. A passing event clears the failure only if (a) its
  seed is new for that exam; (b) every question that failed in the last failing event now
  has a different content hash or key (or is retired), which is evidence that it was
  re-extracted; and (c) the event includes those previously failed questions, re-checked,
  in addition to the 50-question random sample. `verify draw` adds them automatically
  after a failure. Translation review samples 30 per language in the same way and records
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

## R16. Concurrency and atomicity

- **Decision**: One maintainer runs the pipeline, but nothing stops two terminals, so the
  rule is explicit. Every command that writes anything (the ledger, blobs, or Postgres)
  first takes one session-level Postgres advisory lock with `pg_try_advisory_lock` on a
  fixed key. It holds the lock for its whole run. If the lock is taken, the command exits
  with code 4 and the message "another pipeline command is running", without waiting.
  Read-only commands (`verify status`, `export`) take no lock. Under the lock:
  - ledger `seq` numbers are allocated, and each event line is appended and fsynced, so
    `seq` is strictly increasing with no ties;
  - only one `fetch` runs, so per-domain rate limits hold across invocations (R6);
  - only one `translate` runs, and the no-overwrite response blob (R10) fixes one MT
    result per request key;
  - `verify record`'s seed check and its append are one step (R14);
  - snapshot version choice and publication, including the fingerprint refresh, form one
    serialized operation (R11).
  On top of that, each stage reconciles each edition in one transaction (data-model.md),
  so a crash never leaves an edition half-written.
- **Rationale**: One global lock is the simplest rule that removes every race listed
  above, and contention is not a concern for a single-maintainer batch tool. The lock lives
  in Postgres because every writing command already connects to it, and Postgres releases
  it automatically if the process dies. A lock file would not survive a crash as cleanly,
  and would not cover a second machine using the same database.
- **Alternatives considered**: per-resource locks (ledger, domain, snapshot), which allow
  more parallelism that nothing needs, and add more ways to deadlock; optimistic retries on
  unique-constraint conflicts (they cover the database but not the ledger file or blobs).
