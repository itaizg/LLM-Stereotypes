# Data Model: Cross-National Exam Question Dataset

Phase 1 output for [plan.md](plan.md). `pipeline/schema.sql` is the single authority. This
document describes it, and the generated `data_dictionary.md` is built from the schema's
`COMMENT ON` text. Identifier and hash rules are defined in [research.md](research.md) R7.

## Where each fact lives

| Fact | Authority | Written by |
|------|-----------|-----------|
| Exams, domains, rate limits, parse patterns, index pages, editions, evidence | `pipeline/catalog.yaml` (committed, human-authored) | maintainer; loaded by the `catalog` stage |
| Candidate URLs, review decisions, fetch outcomes, artifact hashes, hand-checks, translation reviews | `pipeline/ledger.jsonl` (committed, append-only) | `discover`, `review`, `fetch`, `verify` |
| Raw files, MT responses, snapshots | blob store (S3 or local directory, private) | `fetch`, `translate`, `snapshot` |
| Everything below | Postgres (live store, rebuildable) | stages as listed |

## Write ownership (FR-016)

Each table has exactly one writing stage:

| Table | Writer | Write mode |
|-------|--------|-----------|
| `exam_editions` | `catalog` | reconcile with catalog.yaml |
| `sources` | `catalog` (replays ledger) | reconcile with ledger |
| `artifacts` | `catalog` (replays ledger) | reconcile with ledger |
| `stimuli`, `questions` | `extract` | reconcile per edition |
| `answer_keys` | `key` | append-only (insert if new) |
| `translations` | `translate` | append-only (insert if new) |
| `extraction_failures` | the stage named in the row's `stage` column; each stage reconciles only its own rows | reconcile per (edition, stage) |
| `exam_checks` | `catalog` (replays ledger `handcheck` events) | reconcile with ledger |
| `snapshots` | `snapshot` | append-only |

**Reconcile** means: compute the full set of rows this stage owns for one edition from its
inputs, then insert missing rows, update rows whose `content_hash` changed, and delete
rows that are no longer produced. Unchanged inputs produce zero writes (FR-019). The
`fetch` stage writes ledger events and blobs; `catalog` turns them into rows, so
`artifacts` and `sources` have one writer.

## Tables

### `exam_editions` (the exam catalog)

| Column | Type | Rules |
|--------|------|-------|
| `edition_id` | text PK | `{exam}-{year}-{sitting}`; matches `^[a-z]+-20(1[5-9]\|2[0-5])-[a-z0-9-]+$` |
| `exam_id` | text | one of `sat`, `psychometric`, `csat`, `gaokao`, `common_test`, `enem` (FR-001) |
| `year` | int | 2015–2025 |
| `sitting` | text | sitting, form, or paper name (e.g. `national-i`, `main`, `practice-03`) |
| `kind` | text | `administered` \| `practice` (FR-005) |
| `status` | text | `ingested` \| `not_released` \| `blocked` (FR-002); never null |
| `status_reason` | text | required for `blocked` (`robots`, `non_allowlisted`, `login_required`) |
| `release_date` | date | required when `ingested` |
| `official_url` | text | required when `ingested` |
| `lineage` | text | format era, e.g. `sat-paper-2016`, `sat-digital`, `center-test`, `common-test` |
| `language` | text | BCP 47, e.g. `ko`, `zh-Hans`, `ja`, `he`, `pt-BR`, `en` |
| `translation_of` | text FK → `exam_editions` | set on official English editions paired with an original |
| `evidence` | jsonb | list of `{type, source_id \| artifact_sha256, note}`; must not be empty |

Validation, enforced by the `catalog` stage before any write and fatal on failure:
- Every one of the 66 `(exam_id, year)` cells has ≥ 1 edition (FR-003).
- Every `evidence` item references an **approved** source or a stored artifact (FR-007).
- `ingested` editions have ≥ 1 artifact with role `paper` and ≥ 1 with role `key`, or
  else the edition is rejected with a message saying which role is missing.
- `exam_id` must be one of the six exams in `catalog.yaml`, and each exam must declare
  `publishes_keys: true` with evidence (FR-006).

### `sources`

| Column | Type | Rules |
|--------|------|-------|
| `source_id` | text PK | `src_` + sha256(url)[:16] |
| `url` | text unique | |
| `domain` | text | lower-cased host |
| `discovery_method` | text | `index_crawl` \| `manual` |
| `found_on` | text | seed URL, for `index_crawl` |
| `edition_id` | text FK, nullable | set at review; required for `approved` |
| `decision` | text | `pending` \| `approved` \| `rejected` (latest `review` event wins) |
| `decision_reason` | text | required for `rejected` |
| `fetch_outcome` | text | `not_attempted` \| `fetched` \| `refused_domain` \| `refused_robots` \| `login_required` \| `not_approved` \| `http_error` |

Candidates on a non-allowlisted domain are created as `rejected`, with reason
`non_allowlisted`, and are never fetched.

### `artifacts`

| Column | Type | Rules |
|--------|------|-------|
| `sha256` | text | blob key; PK together with `edition_id` |
| `edition_id` | text FK | one artifact may serve several editions (e.g. one key PDF) |
| `source_id` | text FK | the approved source it was fetched from |
| `role` | text | `paper` \| `key` \| `key_erratum` \| `english` \| `audio` \| `statement` \| `index_page` |
| `media_type` | text | e.g. `application/pdf`, `audio/mpeg`, `text/html` |
| `published` | date, nullable | required for `key_erratum` (orders key corrections) |
| `bytes` | bigint | |

Changed content at the same URL gets a new `sha256` and a new row. Old rows and blobs are
never removed (FR-009 edge case).

### `stimuli`

| Column | Type | Rules |
|--------|------|-------|
| `stimulus_id` | text PK | `{edition_id}:{section}:stim-{n}` |
| `edition_id` | text FK | |
| `modality` | text | `text` \| `figure` \| `table` \| `audio` |
| `original_text` | text, nullable | null for figure and audio |
| `artifact_sha256`, `page`, `pipeline_version` | | provenance |
| `content_hash` | text | as for questions, over the stimulus fields |

### `questions`

| Column | Type | Rules |
|--------|------|-------|
| `question_id` | text PK | `{edition_id}:{section}:{item}` |
| `edition_id` | text FK, not null | |
| `canonical_question_id` | text, not null | exact-match dedupe key (FR-010); duplicates share it |
| `section` | text | |
| `item` | text | printed item number (for the Common Test, the answer-box number) |
| `stem` | text | original language |
| `choices` | jsonb | ordered list of `{label, text}` in original labels (`①`, `A`, `1`…); empty for free response |
| `has_figure`, `has_table`, `has_audio`, `is_free_response` | bool, not null | FR-011 |
| `stimulus_id` | text FK, nullable | |
| `source_url`, `artifact_sha256`, `page`, `pipeline_version` | not null | provenance (SC-002); `pipeline_version` is the git SHA |
| `content_hash` | text, not null | excludes `pipeline_version` (R7) |

### `answer_keys`

| Column | Type | Rules |
|--------|------|-------|
| `key_id` | bigserial PK | insertion order breaks ties |
| `question_id` | text FK, not null | |
| `labels` | text[], not null | sorted set of original choice labels, ≥ 1 element (FR-012); for free response, the official answer string(s) |
| `artifact_sha256` | text, not null | must be an artifact with role `key` or `key_erratum` for the same edition |
| `page` | int, not null | |
| `published` | date, not null | the artifact's publication date |
| `pipeline_version` | text, not null | |
| unique | `(question_id, labels, artifact_sha256)` | re-running inserts nothing |

**Current key**: the row with the greatest `(published, key_id)` for the question.

### `translations`

| Column | Type | Rules |
|--------|------|-------|
| `translation_id` | bigserial PK | |
| `subject_kind` | text | `question` \| `stimulus` |
| `subject_id` | text, not null | `question_id` or `stimulus_id` |
| `source_content_hash` | text, not null | the subject's `content_hash` this translation was made from |
| `translation_source` | text | `official` \| `mt` (FR-014) |
| `stem` | text | English (for a stimulus: its text) |
| `choices` | jsonb | English, same order and labels as the original |
| `artifact_sha256`, `page` | | required for `official` |
| `mt_model`, `mt_model_revision`, `mt_prompt_hash`, `mt_response_sha256` | | required for `mt` |
| `pipeline_version` | text, not null | |
| unique | `(subject_kind, subject_id, source_content_hash, translation_source, coalesce(artifact_sha256, mt_response_sha256))` | |

`translate` writes an `mt` row only when no `official` row exists for the same subject and
`source_content_hash` (FR-015). English-original questions get no row; the view uses their
original text (research R10).

**Preferred translation**: among rows whose `source_content_hash` equals the subject's
current `content_hash`, prefer `official` over `mt`, then the greatest `translation_id`.
A translation made from superseded content is never preferred.

### `extraction_failures`

| Column | Type | Rules |
|--------|------|-------|
| `edition_id` | text FK, not null | |
| `subject_id` | text | `question_id`, `stimulus_id`, or `edition:<edition_id>` for whole-edition failures |
| `stage` | text | `extract` \| `key` \| `translate` |
| `reason_code` | text | `CHECK` against the fixed list below |
| `detail` | text | free text; never holds question text, only positions and counts |
| `artifact_sha256`, `page`, `pipeline_version` | | |
| PK | `(stage, subject_id, reason_code)` | |

**Reason codes** (`pipeline.extract.ReasonCode`, a `StrEnum`; a test asserts the SQL
`CHECK` list is identical): `artifact_unreadable`, `unsupported_format`,
`ocr_low_confidence`, `item_split_failed`, `choices_not_found`, `stimulus_unresolved`,
`no_official_key`, `key_label_mismatch`, `translation_pairing_mismatch`, `mt_failed`,
`mt_empty_output`.

### `exam_checks`

Derived from the ledger's `handcheck` events: `exam_id`, `seed`, `event_seq`, `passed`,
`n_items`, `n_failed`, `sample_hashes` (text[]). Exam verification status (`unchecked` \|
`verified` \| `unverified`) is computed from these rows by the function `verify.status()`
(research R14) and written into every snapshot.

### `snapshots`

`version` (PK, `snap-NNNN`), `git_sha`, `catalog_sha256`, `ledger_sha256`, `row_counts`
(jsonb), `uri`.

## View: `questions_flat` (FR-017)

One row per question, combining:
- every `questions` column;
- `edition_kind`, `lineage`, `language`, and `exam_id` from `exam_editions`;
- the **current key**: `key_labels`, `key_artifact_sha256`, `key_page`;
- the **preferred English**: `english_stem`, `english_choices`, `translation_source`
  (`original` for English-original editions, otherwise `official` or `mt`), and the
  translation's provenance;
- the stimulus: `stimulus_modality` and `stimulus_english` (the preferred translation of a
  text stimulus);
- `eligible` (bool) and `ineligible_reason` (text, null when eligible).

`eligible` is true exactly when all of these hold (spec Assumptions): no figure, table,
audio, or free-response flag; `jsonb_array_length(choices) >= 2`; exactly one key label;
a current key exists; English exists; and the stimulus, if any, is `text` with English.
`ineligible_reason` names the first rule that fails, in the order listed. Exam-level
`unverified` exclusion is applied by the export, not the view, because verification status
lives in the ledger.

## State rules

**Edition status** comes from the catalog and is never inferred by code:

```text
(catalog entry) ── status: ingested ──► extraction allowed (FR-004)
                ── status: not_released ──► no fetch, no extraction
                ── status: blocked ──► no fetch, no extraction; reason required
```

A maintainer can change status in catalog.yaml (e.g. `blocked` → `ingested` once an
allowlisted official copy is found). The next `catalog` run reconciles the change.

**Source decision**: `pending` → `approved` | `rejected`, through ledger `review` events
(the latest wins). Only `approved` sources are fetched or cited as evidence.

**Key currency**: a new `key_erratum` artifact adds a row whose `published` date is later,
so it becomes current. The older row stays for audit (spec edge case).

**Exam verification**: `unchecked` → `verified` | `unverified`; `unverified` → `verified`
only under the re-extraction and new-seed rule (research R14).

## Volume estimate

| Table | Rows (order of magnitude) |
|-------|---------------------------|
| `exam_editions` | 10² |
| `sources` | 10³ |
| `artifacts` | 10³ |
| `questions` | 10⁴–10⁵ |
| `answer_keys`, `translations` | ≈ `questions` |
| `extraction_failures` | 10²–10⁴ |
