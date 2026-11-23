---

description: "Task list for the crawler agent (spec 003)"
---

# Tasks: Crawler Agent for Exam Source Discovery

**Input**: Design documents from `specs/003-crawler-agent/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: required. The constitution (Principle III) requires a failing-first test for
every behaviour change, and FR-035 requires the evals before any agent code. Every
"Tests" block below is written first and must fail before its implementation tasks start.

**Organization**: tasks are grouped by user story, so each story can be built and tested
as its own increment.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an unfinished task).
- **[Story]**: the user story the task belongs to (US1–US4).
- Every task names its file path.

## Path conventions

- **Crawler package:** `crawler/` at the repository root, with its own optional extra
  `crawler`. It imports `pipeline`; `pipeline/` and `harness/` never import it.
- **Pipeline changes:** `pipeline/` (store, schema, ledger fold, import step, fetch).
- **Tests:** `tests/crawler/` (no network, no keys, no GPU) and `tests/pipeline/`
  (loopback Postgres through `PIPELINE_TEST_DSN`).
- **Lint and type checks:** run ruff on named paths only (`crawler tests/crawler
  pipeline tests/pipeline`), never on `.`, which rewrites the untracked Spec Kit scripts.

---

## Phase 1: Setup (governance and project wiring)

**Purpose**: the prerequisites that block crawler code (FR-034, plan "Prerequisites"),
and the package, extra and CI job. Under Governance, the constitution amendment ships in
the same PR as the tooling that enforces it.

- [ ] T001 Amend the constitution 1.4.0 → 1.5.0 in `.specify/memory/constitution.md`: add `crawler/` to the Principle I typed paths, the `crawler` CI job to "Quality Gates and Automation", and the `crawler` optional extra to "Project Constraints → Dependencies"; bump the version, date and Sync Impact Report.
- [ ] T002 [P] Amend spec 001 in `specs/001-exam-data-aggregation/spec.md` (FR-034): FR-006 admits open scope with new exams added only by approval (spec 003 FR-028); FR-007 and FR-008 admit labelled third-party sources and the `pipeline import-decisions` step (FR-026); FR-012 admits labelled third-party answer keys (FR-029). Add a dated Clarifications entry that points to spec 003.
- [ ] T003 [P] Add the `crawler` optional extra to `pyproject.toml` (`langgraph`, `langchain-anthropic`, `pydantic` v2 pinned explicitly, `lingua-language-detector`, `opentelemetry-sdk`), add `crawler*` to the package include list, add `crawler` to `[tool.coverage.run] source`, then refresh `uv.lock` with `uv lock`.
- [ ] T004 [P] Add `--extra crawler` to the ty hook entry in `.pre-commit-config.yaml`, so ty checks `crawler/` with its dependencies resolved.
- [ ] T005 [P] Add a `crawler` job to `.github/workflows/ci.yml`, modelled on the `pipeline` job: a Postgres 17 service, `PIPELINE_TEST_DSN`, `uv sync --locked --extra dev --extra pipeline --extra crawler`, `pytest tests/crawler` under coverage, and upload of `.coverage.crawler` for the existing `coverage` combine job.
- [ ] T006 [P] Create the package skeleton: `crawler/__init__.py` (a docstring stating the one-way dependency on `pipeline`), `crawler/__main__.py` (argparse with `run`, `review` and `status` subcommands that exit 2 "not implemented"), `crawler/evals/__init__.py`, and `tests/crawler/__init__.py`.
- [ ] T007 Write `crawler/lexicon.yaml` (version 1): the four categories `exam`, `item`, `instruction` and `key` for en, he, ko, zh, ja and pt, in the shape given in data-model.md "Keyword lexicon". The maintainer reviews and commits it before any task in Phase 3 starts (FR-006; plan prerequisite).

**Checkpoint**: governance amended, the extra installs, CI has a `crawler` job, and the lexicon is reviewed.

---

## Phase 2: Foundational (blocking prerequisites)

**Purpose**: shared contracts, configuration, store schema, host-slot spacing, network
safety, tracing and test fakes. Every user story depends on these.

**⚠️ CRITICAL**: no user story work starts until this phase is complete.

### Tests for the foundation (write first, confirm they fail)

- [ ] T008 [P] Write the guard test `tests/crawler/test_guard.py`. It walks the ASTs of `pipeline/`, `harness/` and `orchestration/` and fails on any import of `crawler` (as `tests/pipeline/test_guard.py` does for its own rule). It also enforces FR-023 on `crawler/`: no call to `append_event` or any other ledger writer, no write to `catalog.yaml` or `ledger.jsonl`, no import of `pipeline.decisions` import logic (the model only), and no import of `pipeline.blobs`.
- [ ] T009 [P] Write the schema migration test in `tests/pipeline/test_store.py`: apply the spec 002 schema from a frozen copy at `tests/pipeline/fixtures/schema_v2.sql` (copied from `pipeline/schema.sql` before T017 changes it), run `store.apply_schema`, and assert that the tables, columns and constraints match a fresh `schema.sql` store at `schema_version = 3`. Also assert that a v3 store is left unchanged, and that the new `exams.exam_id` rule accepts `abc_1` and rejects `Bad-Key`.
- [ ] T010 [P] Write the host-slot tests in `tests/pipeline/test_store.py`: two connections reserve the same host in a tight loop; the returned slot times, read from the database clock, are at least 5 s apart and never equal. `PostgresHostLedger` asks for its reserved slot instead of read-then-write (research R6): the test injects its sleep function and asserts the requested waits, so it never sleeps for real or depends on elapsed time (Principle III).
- [ ] T011 [P] Write `tests/crawler/test_config.py`: the default `RunConfig` has the budget 500 / $10 / 100 iterations / 5 failures / 20 tool calls per iteration, `host_interval_s` 5, `concurrency` 8 and `classifier_max_cost_usd` 0.02; `run_id` matches `cr_<yyyymmddThhmmss>_<4 hex>`; every `ReasonCode` maps to exactly one class, transient or permanent, matching data-model.md.
- [ ] T012 [P] Write `tests/crawler/test_contracts.py`: each quantity field's name ends in its unit, and its JSON schema carries `unit`; `Proposal` validation rules (passed ⇔ checks 1–3 passed; reachable failed ⇒ the rest `not_run` and `content_hash` null; `key_located` never `failed`); titles are capped at 120 characters and excerpts at 300; `extra="forbid"` everywhere; a stored `HandoffTicket` cannot be `pending`; a ticket built from the synthetic MCQ fixture contains none of its stems or choices (FR-020).
- [ ] T013 [P] Write `tests/crawler/test_lexicon.py`: the committed `crawler/lexicon.yaml` loads, all four categories are non-empty for en, he, ko, zh, ja and pt, a malformed lexicon is a usage error, and keyword counting per category works on a synthetic sample.
- [ ] T014 [P] Write `tests/crawler/test_netsafe.py` (fake resolver and `httpx.MockTransport`): refuse private, loopback, link-local, multicast and metadata addresses (`169.254.169.254`, `fd00:ec2::254`) for IPv4, IPv6 and IPv4-mapped IPv6; re-check every redirect hop; stop after 5 hops with `too_many_redirects`; refuse non-http(s) schemes with `bad_scheme`; connect to the checked address (no second DNS lookup); record every refusal with no request sent.
- [ ] T015 [P] Write `tests/crawler/test_tracing.py`: every span carries `run_id`; `TAVILY_API_KEY`, `BRAVE_API_KEY`, `ANTHROPIC_API_KEY` and `XERJ_TOKEN` values are redacted from span attributes and error text; the local exporter writes `trace.jsonl`.
- [ ] T016 [P] Write `tests/pipeline/test_decisions.py::test_model_*`: the `DecisionsFile` model rejects unknown fields, a wrong `format`, files over 5 MB, an approve without `edition_id`, `role` or `exam`, a reject without `reason`, a bad `exam.key`, members with different `content_hash`, and a new exam without `new_exam`; it accepts the example in contracts/decisions-file.md.

### Implementation for the foundation

- [ ] T017 Add a `schema_version` table, the four `crawler_*` tables (`crawler_runs`, `crawler_proposals` with PK (`proposal_id`, `revision`), UNIQUE (`normalised_url`, `revision`), `observed_at` and `review_state`; `crawler_tickets`; `crawler_url_status`), the `exams.exam_id ~ '^[a-z][a-z0-9_]{1,40}$'` CHECK, `'crawler'` in the `sources.discovery_method` CHECK, and `needs_reprocess boolean DEFAULT false` on `questions` and `answer_keys` to `pipeline/schema.sql`, as data-model.md specifies.
- [ ] T018 Write `pipeline/migrations/003_crawler.sql`, which brings a version 2 store to version 3 in one transaction (the same changes as T017, with the old CHECK constraints dropped and replaced), and make `store.apply_schema` in `pipeline/store.py` versioned: an empty store gets `schema.sql` at v3, a store below v3 gets the migration, and a v3 store is left alone (makes T009 pass).
- [ ] T019 Add `store.reserve_host_slot(conn, host, interval_s) -> tuple[float, datetime]` to `pipeline/store.py`, using the single `INSERT … ON CONFLICT … GREATEST(clock_timestamp(), last_request_at + interval) RETURNING` statement from research R6; it returns the wait in seconds and the reserved slot time. Switch `PostgresHostLedger` to it, with its sleep function injectable (makes T010 pass).
- [ ] T020 [P] Implement `crawler/config.py`: `Budget`, `RunConfig` (models and prices, `refresh`, `exams_first`, `approved_by` and `approved_at`), the thresholds (5 items, 3–5 choices, 0.7 page-type confidence, 0.8 language confidence, 2 MB, 3 PDF pages, 64 KB probe read, 256 KB terms read, 7 days and 3 attempts for re-checks, 30-day dead hosts), the per-tool limits from contracts/tools.md, the `ReasonCode` → class mapping, and the `run_id` factory (makes T011 pass).
- [ ] T021 [P] Implement `crawler/contracts.py`: the enums, `SearchHit`, `ProbeResult`, `SampleResult`, `TermsResult`, `ToolError`, `Labels`, `Checks`, `Proposal` with its validators and `material_fingerprint()`, and `HandoffTicket` with `input`, `output` and `completeness`; units via `Field(json_schema_extra={"unit": …})` (makes T012 pass).
- [ ] T022 [P] Implement `crawler/lexicon.py`: load and validate `crawler/lexicon.yaml`, and `keyword_counts(text, language) -> dict[str, int]` plus `instruction_hits` (makes T013 pass).
- [ ] T023 [P] Implement `crawler/netsafe.py`: resolve, apply the address policy, connect to the pinned address with SNI and Host preserved, walk redirects with at most 5 hops each re-checked, and return typed refusals (makes T014 pass).
- [ ] T024 [P] Implement `crawler/tracing.py`: the OpenTelemetry tracer provider, a local JSONL exporter to `results/crawler/<run_id>/trace.jsonl`, an optional OTLP exporter when `OTEL_EXPORTER_OTLP_ENDPOINT` is set, a `run_id` span attribute on every span, and secret redaction modelled on `store.redact` (makes T015 pass).
- [ ] T025 [P] Implement `pipeline/decisions.py` with the `DecisionsFile`, `Decision`, `Member`, `ExamRef` and `NewExam` Pydantic models and the boundary checks only; no import logic yet (makes T016 pass).
- [ ] T026 Implement `crawler/slots.py`: an async `HostSlots` that calls `store.reserve_host_slot` in a worker thread, sleeps for the returned wait through an injected clock, and returns `observed_at`; plus an `asyncio.Semaphore(concurrency)` cap of 8 in flight. Write `tests/crawler/test_slots.py` first: a virtual clock shows two hosts run concurrently and one host is spaced by 5 s.
- [ ] T027 [P] Implement `crawler/memory.py` with the `CrawlerMemory` Protocol (`queries_tried`, `record_query`, `record_yield`, `dead_hosts`, `record_dead_host`), its typed entry records, and a `NullMemory` that stores nothing, so Phase 3 can run without memory. The XERJ adapter comes in US4.
- [ ] T028 Write the shared fakes in `tests/crawler/conftest.py`: a replay `httpx.MockTransport` with a request log, a fake resolver, a virtual clock, a scripted fake chat model (tool-call scripts per turn), a fake classifier with fixed labels and costs, an in-memory `CrawlerMemory`, and a `crawler_db` fixture that reuses the `PIPELINE_TEST_DSN` loopback pattern from `tests/pipeline/conftest.py`.

**Checkpoint**: the schema migrates, the host slots are atomic, the contracts and config validate, and the fakes exist. The user stories can start.

---

## Phase 3: User Story 1 - Discover and validate candidate sources (Priority: P1) 🎯 MVP

**Goal**: an approved run searches, probes, samples, labels and writes proposals with
hand-off tickets, stops cleanly at a limit, and never touches a forbidden target.

**Independent test**: `uv run pytest tests/crawler -q` runs the offline replay eval
(quickstart scenario 1). The expected proposals exist with every label and check result,
known-bad URLs fail with the right reason, and the transport log shows no request to a
forbidden target.

### Evals and tests for User Story 1 (FR-035: written before any agent code)

- [ ] T029 [P] [US1] Write the synthetic replay fixtures in `tests/crawler/fixtures/replay/`, with no real exam text (FR-020): an official past-paper PDF, a prep-site mirror with identical bytes, a numbered-list article, a news piece quoting 3 questions, a forum thread, a script-only page, a PDF served as `text/html`, a robots-disallowed path with its `robots.txt`, a redirect to `10.0.0.1`, a login wall, an answer-key PDF linked from the paper page, a terms-of-use page that forbids scraping, and an injection-bearing page whose title holds `<script>` and "ignore previous instructions". Also add recorded-shape Tavily and Brave search responses, one of them a 429 quota error, and a `manifest.yaml` giving each fixture's expected status, reason and key status.
- [ ] T030 [US1] Write the replay eval `tests/crawler/test_replay_eval.py` over `crawler/evals/replay.py` (failing until T046): each fixture gets the status and reason in `manifest.yaml`; 0 requests to a disallowed path, a login wall or a non-public address (SC-003); 100% of tickets validate (SC-004); a second replay creates 0 new rows (SC-005); query, cost and iteration limit fixtures stop with the matching `stop_limit` and completeness (SC-006); no fixture stem appears in the tickets, the database rows or the spans (SC-009); the adversarial fake model, which tries to sample before probing and to claim `passed`, never gets a `passed` proposal (research R4).
- [ ] T031 [P] [US1] Write `crawler/evals/live_set.yaml` (URLs and expected outcome only: the ledger's known ENEM, Common Test and SAT sources as positives, hand-picked known-bad URLs as negatives) and `tests/crawler/test_live_set.py`, which validates its shape and checks that it holds no page text.
- [ ] T032 [P] [US1] Write `tests/crawler/test_search.py`: Tavily and Brave adapters parse into `SearchHit` with titles capped; at most 20 results; 10 s timeout and 2 retries on timeouts and 5xx only; failover on unavailability or quota, recorded once in run state with `{at, from, to, reason}` (FR-007a); both providers count toward one query budget; `query_budget_exhausted` once the budget is spent; both down → a `ToolError` that counts toward escalation.
- [ ] T033 [P] [US1] Write `tests/crawler/test_probe.py`: 2xx with robots allowing and no signal → `reachable=passed`; robots-disallowed → `blocked` with no GET of the page; login, paywall and CAPTCHA signals; 4xx/5xx/timeout reason codes and their classes; at most 64 KB read; a permanent failure in `crawler_url_status` is refused without a request unless `refresh`; a transient failure is re-checked only after 7 days and fewer than 3 attempts (FR-016a); a URL that passed before is probed only, and `sample_content` runs again only when `size_bytes` or `ETag` changed (or `refresh` is set), otherwise the probe returns `unchanged` and `write_proposal` takes the no-op path; a passing probe is recorded in run state.
- [ ] T034 [P] [US1] Write `tests/crawler/test_sample.py`: refused with `not_run_unreachable` and no request when there is no passing probe this run (FR-012); reads at most 2 MB or 3 PDF pages; `%PDF-` with a mismatched content type → `type_mismatch`; a script-only page → `script_only`; MCQ structure (≥5 items, 3–5 choices, consistent) passes for the paper and fails for the numbered list and the quoting article; language below 0.8 confidence → `unknown` and `lexicon_missing`; the key status cases `linked_file`, `inline`, `likely` and `not_found` with evidence; a stable `content_hash` for identical bytes; the classifier is skipped over its cost cap with MCQ `not_run` and `not_classified`.
- [ ] T035 [P] [US1] Write `tests/crawler/test_classify.py`: the classifier prompt quotes page text as length-capped data in a fenced block (research R12); output parses into `SiteType`, `PageType` and `Topic` with confidences; malformed output is a typed error; cost is metered from token usage × configured price and reserved before the call.
- [ ] T036 [P] [US1] Write `tests/crawler/test_terms.py`: the terms or licence page is found from the domain root; the excerpt is at most 300 characters; `restrictive=true` for forbidding text adds the `restrictive_terms` flag and never fails the source; the result is cached per domain per run; at most 256 KB read.
- [ ] T037 [P] [US1] Write `tests/crawler/test_proposals.py` (Postgres): revision 1 and its ticket are written in one transaction; a retry returns the existing ticket (FR-021); the same fingerprint → no write; a material change (content, status, reason, key status or label) → revision n+1 and revision n `stale_superseded`; a confidence-only change → no revision (FR-021a); an older `observed_at` → no write and note `older_observation`; a new revision of an `approved` row that changes content or key status → flag `reopened_after_approval`; `crawler_url_status` is upserted on every call; a stated exam name or year missing from the page is replaced with null and the replacement recorded.
- [ ] T038 [P] [US1] Write `tests/crawler/test_graph.py`: the budget is reserved under one lock for parallel tool calls, so 20 concurrent `web_search` calls at 495 queries used admit exactly 5; at most 20 tool calls per iteration; `recursion_limit` is 2×100+1 and the explicit counter stops at 100 with `stopped_at_limit`/`iterations`; the $10 cap counts agent and classifier spend together; 5 consecutive failures of one tool → `escalated` and a success resets the counter (FR-003); the message history is compacted at task boundaries and above 50% of the window; tool output with instruction-like text is passed as quoted data.
- [ ] T039 [P] [US1] Write `tests/crawler/test_cli.py`: `run` without a TTY or with any answer but `y` exits 2 and writes nothing; approval records `approved_by` and `approved_at` in `crawler_runs` (FR-001); a missing key exits 2 and names the variable, never its value; exit codes 0, 1, 5 and 6; `summary.json` holds status, stop limit, counts, `cost_usd` and completeness; `status` lists runs and shows counts by status and reason.

### Implementation for User Story 1

- [ ] T040 [P] [US1] Implement `crawler/search.py`: the `web_search` tool with Tavily and Brave adapters over `httpx`, the query-budget reservation, failover recording in run state, and the limits from contracts/tools.md (makes T032 pass).
- [ ] T041 [P] [US1] Implement `crawler/probe.py`: the `probe_url` tool over `netsafe` and `slots`, robots.txt parsing for `pipeline.web.USER_AGENT`, login, paywall and CAPTCHA signals, the FR-016a re-check policy from `crawler_url_status`, the probe-only path for previously passed URLs (stored `size_bytes` and `ETag` compared; `crawler_url_status` gains `last_size_bytes` and `last_etag`), and the run-state probe record (makes T033 pass).
- [ ] T042 [P] [US1] Implement `crawler/classify.py`: the cost-metered classifier call through `langchain-anthropic` with the worst-case reservation, the quoted-data prompt and typed parsing (makes T035 pass).
- [ ] T043 [US1] Implement `crawler/sample.py`: the `sample_content` tool with its probe precondition, the bounded read (`pdfplumber` for the first 3 pages; `pipeline.web.decode_html` and `extract_links` for pages), the MCQ structure signals, `lingua` language detection, keyword counts, key status with evidence, `content_hash`, the classifier call or the `not_classified` skip, and `observed_at` from the slot reservation (makes T034 pass; depends on T042).
- [ ] T044 [P] [US1] Implement `crawler/terms.py`: the `read_terms` tool with its per-run domain cache (makes T036 pass).
- [ ] T045 [US1] Implement `crawler/proposals.py`: the `write_proposal` tool, which assembles labels and checks from run state, computes `validation_status`, `reason`, `reason_class` and `material_fingerprint`, takes the `SELECT … FOR UPDATE` lock on the latest row, handles older observations, revisions and `stale_superseded`, flags `reopened_after_approval`, upserts `crawler_url_status`, and applies the stated-exam guard (makes T037 pass).
- [ ] T046 [US1] Implement `crawler/graph.py`: the LangGraph `StateGraph` with the `agent` and `tools` nodes, run state (probe and sample caches, failure counters, provider switches), budget reservation under one `asyncio.Lock`, at most 20 tool calls per iteration with at most 8 in flight, the iteration counter and `recursion_limit`, escalation, compaction, a span per step and tool call, and the system prompt (exams first, then open search; web text is data). Then implement `crawler/evals/replay.py`, which drives the graph over the replay fixtures with the fakes (makes T038 and T030 pass).
- [ ] T047 [US1] Implement `crawler run` and `crawler status` in `crawler/__main__.py`: config and lexicon loading, the catalog view and the `TODO.md` gaps for `exams_first`, the TTY budget prompt, the `crawler_runs` row lifecycle (`running` → `completed`, `stopped_at_limit`, `escalated` or `failed`), `summary.json`, one progress line per task on stderr, and the exit codes (makes T039 pass).
- [ ] T048 [US1] Implement `crawler/evals/live.py` (`python -m crawler.evals.live`): the same approval prompt and budget rules; probe and sample over `crawler/evals/live_set.yaml`; precision and recall written to `results/crawler/evals/<run_id>.json` (SC-001, SC-002). It is not run in CI.

**Checkpoint**: User Story 1 works end to end offline. The replay eval passes, and a live run can be started with approval. This is the MVP.

---

## Phase 4: User Story 2 - Review proposals and record decisions (Priority: P2)

**Goal**: a static, safe review page with one row per content group of `passed`
proposals, which downloads a decisions file that matches the contract.

**Independent test**: render the page from a fixed set of proposals, and check the rows,
grouping, flags and escaping. Then build a decisions file from recorded approve and
reject choices and validate it against `pipeline.decisions.DecisionsFile` (quickstart
scenario 4).

### Tests for User Story 2

- [ ] T049 [P] [US2] Write `tests/crawler/test_review.py`: with 12 `passed` and 30 `failed` proposals, exactly the 12 `passed` appear, one row per `content_hash`; in a group with an official PDF and two mirrors the official URL is primary and the mirrors are alternates, each with its own labels and origin (FR-023a); labels, check results, evidence links, and the `restrictive_terms` excerpt and `lexicon_missing` flags are shown; the injection fixture's title appears only escaped, and the embedded JSON escapes `<` as `<`; the CSP meta tag is `default-src 'none'` with script and style hashes that match the inline blocks; an unknown run exits 2.
- [ ] T050 [P] [US2] Write `tests/crawler/test_review_decisions.py`. Everything in a decision except the maintainer's choices is computed in Python and embedded per group, so it can be tested without a browser: `review.decision_skeletons(run_id)` returns, per content group, the `decision_id` (`dec_<sha256(run_id|content_hash|revisions)[:16]>`), the `content_hash`, and each member's `proposal_id`, revision shown, `primary` and proposed `origin`. `review.merge_choices(skeleton, choices)` is the pure-Python reference for the page's merge step: given recorded choices (decision, exam, new exam, edition, role, published date, reason, confirmed origin per URL) it returns a decision that `DecisionsFile` accepts, and a reject without a reason or an approve without an edition is refused. A golden test renders the page and checks that the embedded skeletons equal `decision_skeletons(run_id)` and that the inline script contains no hashing code.

### Implementation for User Story 2

- [ ] T051 [US2] Implement `crawler/review.py`: query the run's `passed` proposals, group them by `content_hash`, choose the primary (official first, else first found), and render one self-contained HTML file with `string.Template` and `html.escape`; embed the data as escaped JSON; add filters by label and flag; add approve and reject controls (exam with a new-exam form, edition, role, published date, reason, origin per URL); embed `decision_skeletons(run_id)` so the inline script only copies a skeleton and adds the choices, mirroring `merge_choices` field for field and computing nothing itself; add a "Save decisions" button that builds a `Blob` and downloads `decisions-<run_id>.json`; and compute the CSP hashes from the inline script and style (makes T049 and T050 pass).
- [ ] T052 [US2] Wire `python -m crawler review RUN_ID [--out PATH]` in `crawler/__main__.py`, writing `results/crawler/<run_id>/review.html` by default; read-only on the store.

**Checkpoint**: Stories 1 and 2 both work. A run produces a page, and the page produces a valid decisions file.

---

## Phase 5: User Story 3 - Import decisions and hand approved sources on (Priority: P3)

**Goal**: `pipeline import-decisions` turns decisions into ledger events, the ledger
fold exposes only approved crawler sources to the downloader, new exams and allowlisted
domains appear in the catalog view, and reopened sources are suspended.

**Independent test**: import a decisions file over a fixed set of proposals, including a
decision on an outdated revision. Check which sources are eligible for the downloader,
that labels and evidence are kept, that the stale decision is rejected, and that a
second import changes nothing (quickstart scenario 5).

### Tests for User Story 3

- [ ] T053 [P] [US3] Write `tests/pipeline/test_ledger_crawler.py` for the fold in `pipeline/manifests.py`: `exam_added`, `domain_allowlisted` and `review_reopened` are accepted kinds; a crawler `candidate` carries `proposal_id`, `revision`, `origin`, `labels_digest` and `evidence`; a decision's `exam_added`, `domain_allowlisted` and `candidate` events apply only when that `decision_id` has its `review` event (a torn decision exposes nothing); `review_reopened` resets the source to `pending` and clears its approvals; the dedupe keys from data-model.md hold; the latest review wins; `SourceState.origin` is set from the candidate.
- [ ] T054 [P] [US3] Write the import tests in `tests/pipeline/test_decisions.py`: a decision on revision 2 when 3 is current → `stale` and nothing written for the whole decision (SC-008); an approval on a third-party domain → reviewed source with labels and evidence, plus `domain_allowlisted` for that exam; an approval for a new exam → one `exam_added`, and a later approval for the same exam adds no second one; an approval of a `failed` or `stale_superseded` member is refused; a new exam with no `linked_file` or `inline` key is refused; a reject → `candidate` and a rejected `review` with its reason; importing the same file twice → no new ledger lines; a changed decision on the same revision → one new `review` event; exit codes 0, 2, 3 and 4.
- [ ] T055 [P] [US3] Write the concurrency and atomicity tests in `tests/pipeline/test_decisions.py`: an import and a `write_proposal` on the same proposal serialise through `FOR UPDATE` (either the import commits first and the new revision is flagged `reopened_after_approval`, or the crawler commits first and the import reports `stale`); a crash between building and appending a decision's block leaves the ledger and store unchanged, and a re-run completes it.
- [ ] T056 [P] [US3] Write the reopen tests in `tests/pipeline/test_decisions.py` and `tests/pipeline/test_fetch.py`: the reopen sweep appends one `review_reopened` per flagged proposal, also with `--reopen-only`, and never twice; replaying it sets `needs_reprocess` on that source's questions and answer keys (FR-021b); `pipeline fetch` skips a crawler source with a revision newer than the approved one and reports `skipped_reopened`; a `passed` but unapproved proposal is never offered to fetch (FR-027).
- [ ] T057 [P] [US3] Write the catalog-view tests in `tests/pipeline/test_acquire.py`: the catalog view is `catalog.yaml` plus `exam_added` exams, and the allowlist is the catalog domains plus `domain_allowlisted` domains for that exam; a ledger-added exam becomes an `Exam` with `name`, `body` and `language` from the event, `domains` from the event's domain plus its `domain_allowlisted` domains, `publishes_keys = true` with the approving decision and proposal as `keys_evidence`, the default rate limit, and no seeds or parse config; `run_catalog` writes its `exams` row in the same transaction as the catalog exams; `validate_catalog` accepts editions of a ledger-added exam; `pipeline exams` lists the new exam; `pipeline extract` reports a ledger-added exam with no parse config as skipped, not as an error. A `catalog.yaml` entry with the same key takes precedence over the ledger values.

### Implementation for User Story 3

- [ ] T058 [US3] Extend `pipeline/manifests.py`: add `exam_added`, `domain_allowlisted` and `review_reopened` to `LEDGER_KINDS`; add the crawler `candidate` fields, `decision_id` on `review`, decision gating, the `review_reopened` → `pending` rule, the dedupe keys, and `origin` on `SourceState` and in `artifact_rows` (makes T053 pass).
- [ ] T059 [US3] Add `catalog_view(catalog, state) -> Catalog` to `pipeline/manifests.py`, which builds the `Exam` objects for `exam_added` exams and merges `domain_allowlisted` domains, as T057 specifies. Use it in `pipeline/acquire.py` wherever `load_catalog` feeds `run_catalog` (which then writes the new `exams` rows), discovery, review, fetch, extraction and `validate_catalog`, and in `pipeline exams` in `pipeline/__main__.py` (makes T057 pass).
- [ ] T060 [US3] Implement the import logic in `pipeline/decisions.py`: validation against the store (current revision `passed` and not superseded, the new-exam key rule), one transaction per decision with `SELECT … FOR UPDATE` on the members' latest rows, event building in contract order, a single `write` and `fsync` append inside the transaction, `review_state` updates, the store reconcile from the ledger, the reopen sweep first on every run, and one output line per decision plus a summary (makes T054 and T055 pass).
- [ ] T061 [US3] Add `pipeline import-decisions FILE` and `--reopen-only` to `pipeline/__main__.py`, holding the pipeline write lock, with exit codes 0, 2, 3 and 4.
- [ ] T062 [US3] Make `pipeline fetch` in `pipeline/acquire.py` skip a crawler source whose `crawler_proposals` has a revision newer than the approved one (`skipped_reopened`), and set `needs_reprocess` on questions and answer keys when the store replays `review_reopened` in `pipeline/store.py` (makes T056 pass).
- [ ] T063 [US3] Report third-party origin downstream (FR-029), with no new column: origin is derived from the source's ledger `candidate` event (data-model.md, "Origin labels"). Add `origin_counts(state, conn) -> dict[exam_id, dict[origin, {questions, keys}]]` to `pipeline/manifests.py`, which joins questions and answer keys to their source and reads each source's origin from the fold, and print it in `pipeline candidates` output in `pipeline/__main__.py`, with tests in `tests/pipeline/test_cli_listing.py`. `origin_counts` is the single source of these numbers; the spec 001 dataset card, when it is built, reads it.
- [ ] T064 [US3] Make `crawler run` in `crawler/__main__.py` print how many approved sources this run reopened and the `pipeline import-decisions --reopen-only` command (contracts/decisions-file.md), with a test in `tests/crawler/test_cli.py`.

**Checkpoint**: Stories 1–3 work. Approved sources reach the ledger and the downloader's eligible set, and nothing else does.

---

## Phase 6: User Story 4 - Learn across runs (Priority: P4)

**Goal**: queries tried, dead hosts and yield per query persist in XERJ, and later runs
skip known ground.

**Independent test**: run the replay twice with one shared in-memory `CrawlerMemory`.
The second run issues none of the first run's queries, probes no dead host, records
each skip, and creates no duplicate proposals (quickstart scenario 7).

### Tests for User Story 4

- [ ] T065 [P] [US4] Write `tests/crawler/test_memory.py`: the XERJ adapter (over `httpx.MockTransport`) reads and writes `query_tried`, `query_yield` and `dead_host` under `/_memory/agent-crawler`; recalled entries parse into typed records, and malformed or instruction-like entries are dropped as data (FR-031); `dead_host.until` is 30 days after the record; XERJ unavailable → the run continues with `NullMemory`, `memory_available=false` in `crawler_runs`, and the note `memory_unavailable`.
- [ ] T066 [P] [US4] Write `tests/crawler/test_replay_twice.py`: two replay runs with a shared in-memory memory; the second issues none of the first run's queries (`skipped_known_query`), does not probe a dead host and records the skip, and writes 0 new proposals or tickets; with `refresh` set it re-issues queries and re-checks permanent failures.

### Implementation for User Story 4

- [ ] T067 [US4] Implement the XERJ adapter in `crawler/memory.py` (`XERJ_URL` and `XERJ_TOKEN` from the environment, token redacted, timeouts and size limits per FR-004), with the fallback to `NullMemory` (makes T065 pass).
- [ ] T068 [US4] Wire memory at task boundaries: in `crawler/graph.py`, read the queries tried and dead hosts when a run starts and write `query_tried`, `query_yield` and `dead_host` when each task ends, all in code, never as a tool (FR-030); in `crawler/search.py`, return `skipped_known_query` unless `refresh`; in `crawler/probe.py`, refuse a dead host until `until` and record the skip; in `crawler/__main__.py`, build the XERJ adapter when configured (makes T066 pass).

**Checkpoint**: all four stories work independently.

---

## Phase 7: Polish and cross-cutting concerns

- [ ] T069 [P] Add question-text checks for crawler outputs: extend the guard scan in `pipeline/corpus.py`, or add a test in `tests/crawler/test_no_question_text.py`, so that `crawler/`, `tests/crawler/fixtures/` and the generated tickets and spans are scanned for fixture stems (SC-009, FR-020).
- [ ] T070 [P] Document the crawler in `README.md`: what it does, the human approval gate, the run → review → import flow, the budget prompt, and the fact that results are reported as measured. Add the crawler commands to `AGENTS.md` if it lists project commands.
- [ ] T071 Run `uv run ruff check crawler tests/crawler pipeline tests/pipeline`, `uv run ruff format --check` on the same paths, and the pre-commit ty hook; fix findings in the files they name.
- [ ] T072 Run the coverage gate with `PIPELINE_TEST_DSN` set (`uv run pytest --cov`) and confirm that the combined total is at least 85% with `crawler` listed (FR-036, quickstart scenario 2); add tests where `crawler/` falls short.
- [ ] T073 Walk quickstart scenarios 1–5 and 7 in `specs/003-crawler-agent/quickstart.md` offline, and fix any step that does not match the commands as built.
- [ ] T074 Maintainer task (manual, spends money): run `python -m crawler run` and `python -m crawler.evals.live` (quickstart scenario 6), time a 50-group review (SC-007), and record the measured precision, recall and review time in `results/crawler/evals/` and in the memory note for this feature. Report a missed threshold as missed.

---

## Dependencies and execution order

### Phase dependencies

- **Setup (Phase 1):** no dependencies. T007 (the reviewed lexicon) gates Phase 3.
- **Foundational (Phase 2):** depends on T003 and T006. It blocks every user story.
- **US1 (Phase 3):** depends on Phase 2 and T007.
- **US2 (Phase 4):** depends on Phase 2. It needs proposals in the store: its tests seed
  them directly, so it can be built in parallel with US1, but its end-to-end check uses
  a US1 run.
- **US3 (Phase 5):** depends on Phase 2 (T017–T019 and T025). Its tests seed proposals
  directly. T055 and T064 also need `crawler/proposals.py` (T045).
- **US4 (Phase 6):** depends on US1 (T040, T041, T046, T047).
- **Polish (Phase 7):** after the stories that are in scope.

### Within each story

- Tests first, and they must fail before the implementation starts.
- In US1: the replay fixtures and eval (T029, T030) come before any tool code (FR-035).
- Contracts and config before tools; tools before the graph; the graph before the CLI.
- `sample.py` (T043) needs `classify.py` (T042).
- `pipeline/decisions.py` import logic (T060) needs the fold (T058) and the catalog view
  (T059).

### Story completion order

```text
Setup ─▶ Foundational ─┬─▶ US1 (MVP) ─▶ US4
                       ├─▶ US2  (seeded tests; e2e after US1)
                       └─▶ US3  (seeded tests; T055/T064 after T045)
                                         └─▶ Polish
```

## Parallel examples

### Phase 2 tests (all different files)

```text
T008 tests/crawler/test_guard.py
T011 tests/crawler/test_config.py
T012 tests/crawler/test_contracts.py
T013 tests/crawler/test_lexicon.py
T014 tests/crawler/test_netsafe.py
T015 tests/crawler/test_tracing.py
T016 tests/pipeline/test_decisions.py
```

T009 and T010 share `tests/pipeline/test_store.py`, so write them one after the other.

### User Story 1 tests, after T029

```text
T032 test_search.py   T033 test_probe.py    T034 test_sample.py
T035 test_classify.py T036 test_terms.py    T037 test_proposals.py
T038 test_graph.py    T039 test_cli.py      T031 live_set.yaml
```

### User Story 1 tools

```text
T040 crawler/search.py   T041 crawler/probe.py
T042 crawler/classify.py T044 crawler/terms.py
```

Then T043 (`sample.py`), T045 (`proposals.py`), T046 (`graph.py`), in that order.

### User Story 3 tests

```text
T053 tests/pipeline/test_ledger_crawler.py
T054–T056 tests/pipeline/test_decisions.py (one file: write in sequence)
T057 tests/pipeline/test_acquire.py
```

## Implementation strategy

### MVP first (User Story 1)

1. Phase 1, with the constitution and spec 001 amendments and the reviewed lexicon.
2. Phase 2.
3. Phase 3: replay eval first, then the tools, graph and CLI.
4. **Stop and validate:** the replay eval passes in CI, and a live run with an approved
   budget writes proposals. The output is useful on its own, even before review exists.

### Incremental delivery

1. US1 → proposals and tickets in the store.
2. US2 → the maintainer can review and save decisions.
3. US3 → approved sources reach the ledger, the catalog view and the downloader.
4. US4 → later runs spend their budget on new ground.

Each story is a PR-sized series of small commits with imperative messages. The
constitution amendment, the CI job and the coverage source entry land in the same PR as
the package skeleton (T001, T003, T005, T006).

## Notes

- `[P]` tasks touch different files and do not depend on an unfinished task.
- Never commit real exam text, review pages or decisions files (`results/crawler/` is
  gitignored, FR-033).
- Secrets come from the environment only, and are never printed or traced.
- Run ruff on named paths only, never on `.`.
