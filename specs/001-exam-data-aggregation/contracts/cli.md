# Contract: `pipeline` CLI

Run it as `pipeline <command>`, the console script, or as `python -m pipeline <command>`.
Every stage can run on its own (FR-018) and is idempotent (FR-019).

## Environment

| Variable | Used by | Notes |
|----------|---------|-------|
| `PIPELINE_DSN` | all DB stages | Postgres connection string; never printed (redacted in errors) |
| `PIPELINE_BLOB_ROOT` | all | `s3://bucket/prefix` or a local path; default `./.pipeline-data` (gitignored) |
| `AWS_*` | S3 blob root | standard boto3 credential chain |
| `ANTHROPIC_API_KEY` | `translate` | only when MT calls are needed; a rebuild uses cached responses and needs no key |
| `PIPELINE_TEST_DSN` | tests | local Postgres for tests |

## Global flags

`--catalog PATH` (default `pipeline/catalog.yaml`) and `--ledger PATH` (default
`pipeline/ledger.jsonl`). `--exam X` and `--edition E` limit a stage to one exam or one
edition. Without them, the stage runs on everything in sorted `edition_id` order.

## Commands

| Command | Reads | Writes | Notes |
|---------|-------|--------|-------|
| `catalog` | catalog.yaml, ledger | `exam_editions`, `sources`, `artifacts`, `exam_checks` | Validates first (66 cells, evidence, keys published) and writes nothing if validation fails. Also applies `schema.sql` if the tables are missing. |
| `discover` | catalog seeds | ledger (`candidate` events), blobs (seed pages) | `--add-url URL --edition E` records a `manual` candidate. Off-allowlist links are recorded as `rejected`. |
| `review CANDIDATE_ID --approve --edition E --role ROLE [--published DATE]` / `--reject --reason TEXT` | ledger | ledger (`review` event) | The only way to approve. Shows the URL, domain, and found-on page before writing. |
| `fetch` | ledger (approved candidates) | blobs (`raw/<sha256>`), ledger (`fetch` events) | Enforces the allowlist, robots.txt, and the rate limit. Refusals are recorded, not raised. |
| `extract` | `artifacts`, blobs | `questions`, `stimuli`, `extraction_failures` (stage `extract`) | Only for `ingested` editions (FR-004). `--workers N` (default: CPU count). |
| `key` | key artifacts, `questions` | `answer_keys`, `extraction_failures` (stage `key`) | |
| `translate` | `questions`, `stimuli`, `english` artifacts, MT cache | `translations`, `extraction_failures` (stage `translate`), blobs (MT responses) | `--offline` fails instead of calling the API when a response is not cached. |
| `verify draw --exam X --seed S [--kind handcheck\|translation --language L]` | `questions_flat` | private CSV under `<blob root>/review/` | 50 per exam or 30 per language (FR-024). Refuses a seed already recorded for that exam. |
| `verify record CSV` | the filled CSV | ledger (`handcheck` or `translation_review` event) | Stores IDs, hashes, verdicts, and scores; never text. |
| `verify status [--exam X]` | `exam_checks` | stdout | `unchecked`, `verified`, or `unverified` per exam |
| `snapshot` | all tables | `snapshots/<version>/…`, `snapshots` row, `pipeline/fingerprints.txt` | Fails if the Parquet row counts differ from the transaction counts (FR-021). |
| `export --exam X --eligible --snapshot V [--out PATH]` | `snapshots/V/questions_flat.parquet` | `questions.json` (default `exports/<exam>/questions.json`, gitignored) | `--eligible` is required. A non-eligible export is not implemented (YAGNI). Prints the count of questions with answers beyond D (research R3). |
| `rebuild` | catalog, ledger, blobs | everything, in stage order, into the schema given by `--schema` | Runs `catalog → extract → key → translate --offline`, then prints per-question content-hash differences against `--compare-schema` if one is given (FR-020). |

`python -m pipeline.corpus` (the guard) is separate on purpose: it needs only the standard
library and PyYAML, so pre-commit and CI can run it without the `pipeline` extra.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | success (including "nothing to do") |
| 1 | unexpected error |
| 2 | usage error |
| 3 | integrity refusal: catalog validation failed, exam `unverified` on export, snapshot count mismatch, guard hit, or seed reuse |

## Output

Each stage ends with one summary line, for example
`extract: editions=12 inserted=840 updated=0 deleted=0 failures=+3/-0`. A re-run on
unchanged inputs prints all zeros, which the idempotence tests check. No question text is
written to stdout or logs.
