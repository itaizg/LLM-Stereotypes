# Quickstart: validating the crawler agent

These are the scenarios that show the feature works. Behaviour details are in
[contracts/](contracts/) and [data-model.md](data-model.md).

## Prerequisites

- **Spec 001 amendments merged** (FR-034): FR-006, FR-007/FR-008 and FR-012.
- **Constitution 1.5.0:** `crawler/` added to the Principle I paths, the `crawler` CI
  job, and the `crawler` extra.
- **Lexicon:** `crawler/lexicon.yaml` committed and reviewed for en, he, ko, zh, ja and
  pt.
- **Environment:** `uv sync --locked --extra dev --extra pipeline --extra crawler`.
- **Postgres:** a local server, with `PIPELINE_DSN` set (and `PIPELINE_TEST_DSN` for
  tests). The memory note `dev-environment-quirks` covers pgserver on this Mac.
- **Live runs only:** `TAVILY_API_KEY` and/or `BRAVE_API_KEY`, and `ANTHROPIC_API_KEY`.
  `XERJ_URL` and `XERJ_TOKEN` are optional.

## 1. Offline replay eval (CI, no network): Story 1 and the safety criteria

```bash
uv run pytest tests/crawler -q
```

**Expected:** all tests pass. `test_replay_eval.py` asserts:
- each synthetic fixture gets the expected status and reason. For example:
  - a numbered-list article → `failed` with `not_mcq_structure`;
  - a robots-disallowed path → `failed` with `blocked`, checks 2–4 `not_run`, and no
    GET for the page;
  - a private-address redirect → `non_public_address`;
  - a mismatched PDF → `type_mismatch`;
  - a script-only page → `script_only`;
- 0 forbidden requests in the transport log (SC-003);
- 100% of tickets validate (SC-004);
- a second replay run creates 0 new rows (SC-005);
- the query, cost and iteration limit fixtures stop with the right `stop_limit`
  (SC-006);
- no fixture stem appears in tickets, the database or spans (SC-009);
- the adversarial fake model cannot produce a `passed` proposal for an unprobed URL
  (research R4).

## 2. Coverage gate

```bash
uv run pytest tests/crawler --cov --cov-report=term
```

**Expected:** the `crawler` package is listed. CI's combined gate stays at 85% or above
(FR-036).

## 3. Host-slot limiter across processes (FR-010)

`tests/crawler/test_slots.py` starts two connections that reserve the same host in a
tight loop against loopback Postgres. **Expected:** the granted slot times are at least
5 s apart by the database clock, and no two are equal.

## 4. Review page: Story 2

```bash
uv run python -m crawler review <run_id>   # after a replay or live run
open results/crawler/<run_id>/review.html
```

**Expected:**
- rows appear only for `passed` content groups, with mirrors under one row and the
  official URL as primary;
- the restrictive-terms and `lexicon_missing` flags are visible;
- the injection-fixture title shows as literal text, and the console shows no CSP
  violation from page content;
- "Save decisions" downloads `decisions-<run_id>.json`, which matches
  [contracts/decisions-file.md](contracts/decisions-file.md).

## 5. Import: Story 3

```bash
uv run pipeline import-decisions results/crawler/<run_id>/decisions-<run_id>.json
uv run pipeline import-decisions results/crawler/<run_id>/decisions-<run_id>.json   # again
uv run pipeline candidates
```

**Expected:**
- the first import prints `approved` / `rejected` / `stale` per decision. The second
  prints no changes, and `git diff pipeline/ledger.jsonl` shows events from the first
  import only;
- an approval for a new exam adds one `exam_added` event, and `pipeline exams` lists
  that exam;
- a third-party approval adds `domain_allowlisted`;
- a `passed` but unapproved proposal is absent from the downloader's eligible set
  (SC-008).

`tests/pipeline/test_decisions.py` covers the same cases without a browser.

## 6. Live run and live eval (manual; spends money; needs approval)

```bash
uv run python -m crawler run            # prompts: Approve this budget? [y/N]
uv run python -m crawler status <run_id>
uv run python -m crawler.evals.live
```

**Expected:**
- the run ends as `completed` or `stopped_at_limit`, with its completeness, and no
  limit is exceeded;
- `results/crawler/evals/<run_id>.json` reports precision ≥ 0.90 and recall ≥ 0.80
  (SC-001, SC-002);
- a review of 50 groups takes under 15 minutes (SC-007, timed by the maintainer).

Results are reported as measured. If a threshold is missed, the summary says so; the
spec's assumptions allow the thresholds to be revised after the first live eval.

## 7. Memory across runs: Story 4

Run the replay twice with the in-memory `CrawlerMemory` fake shared between the runs.
**Expected:** the second run issues none of the first run's queries, probes no dead
host, and records each skip. Against real XERJ, `crawler status` shows
`memory_available: true`.
