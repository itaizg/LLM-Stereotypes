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
| `exams` | `catalog` | reconcile with catalog.yaml |
| `exam_editions` | `catalog` | reconcile with catalog.yaml |
| `sources` | `catalog` (replays ledger) | reconcile with ledger |
| `artifacts` | `catalog` (replays ledger) | reconcile with ledger |
| `stimuli`, `questions` | `extract` | reconcile per edition; rows no longer produced are retired, not deleted |
| `answer_keys` | `key` | append-only (insert if new) |
| `translations` | `translate` | append-only (insert if new) |
| `extraction_failures` | the stage named in the row's `stage` column; each stage reconciles only its own rows | reconcile per (edition, stage) |
| `exam_checks` | `catalog` (replays ledger `handcheck` events; `snapshot` runs `catalog` first) | reconcile with ledger |
| `snapshots` | `snapshot` | append-only |

**Reconcile** means: compute the full set of rows this stage owns for one edition from its
inputs, then insert missing rows, update rows whose `content_hash` changed, and remove
rows that are no longer produced. Unchanged inputs produce zero writes (FR-019). The
`fetch` stage writes ledger events and blobs; `catalog` turns them into rows, so
`artifacts` and `sources` have one writer.

- **One transaction per edition.** Each stage reconciles one edition inside one database
  transaction. A crash leaves that edition fully old or fully new, never half-reconciled,
  and a snapshot (a `REPEATABLE READ` transaction) can see only committed editions.
- **Retire, don't delete, questions and stimuli.** Append-only `answer_keys` and
  `translations` reference them, so `extract` sets `retired = true` on a question or
  stimulus it no longer produces. If a later run produces it again, `retired` goes back to
  false. Retired rows keep their history, and the view excludes them. `extraction_failures`
  rows have no dependants and are deleted normally.
- **Changed content keeps its key rows, not its translations.** A key is tied to a printed
  item number on the official key sheet, so it stays attached to `question_id`. The `key`
  stage re-checks every current key against the question's current choice labels on each
  run (`key_label_mismatch`). Translations carry `source_content_hash`, so a translation of
  superseded content is never preferred.
- **One writer at a time.** Every command that writes (to the ledger, blobs, or Postgres)
  holds one pipeline-wide Postgres advisory lock for its whole run (research R16). That
  serializes ledger sequence numbers, fetch rate limiting, MT calls, `verify record`, and
  snapshot versions.

## Tables

### `exams` (the exam registry)

The `catalog` stage copies the `exams` section of `catalog.yaml` into this table, so the
spec's Exam entity is in every snapshot and in the data dictionary (FR-022). Parse
patterns and seed URLs stay in `catalog.yaml` only: they are pipeline configuration, not
dataset content.

| Column | Type | Rules |
|--------|------|-------|
| `exam_id` | text PK | one of `sat`, `psychometric`, `csat`, `gaokao`, `common_test`, `enem` (FR-001) |
| `name` | text, not null | official exam name |
| `body` | text, not null | official body (e.g. `INEP`, `KICE`, `NITE`) |
| `language` | text, not null | BCP 47 original language |
| `domains` | text[], not null | allowlisted official domains (FR-008) |
| `publishes_keys` | bool, not null | must be true (FR-006) |

### `exam_editions` (the exam catalog)

| Column | Type | Rules |
|--------|------|-------|
| `edition_id` | text PK | `{exam}-{year}-{sitting}`; matches `^[a-z][a-z_]*-20(1[5-9]\|2[0-5])-[a-z0-9-]+$` (the exam part allows `_` for `common_test`) |
| `exam_id` | text FK → `exams` | |
| `year` | int | 2015–2025 |
| `sitting` | text | sitting, form, or paper name (e.g. `national-i`, `main`, `practice-03`) |
| `kind` | text | `administered` \| `practice` (FR-005) |
| `status` | text | `ingested` \| `not_released` \| `blocked` (FR-002); never null |
| `status_reason` | text | required for `blocked` (`robots`, `non_allowlisted`, `login_required`) |
| `release_date` | date | required when `ingested` |
| `official_url` | text | required when `ingested` |
| `lineage` | text | format era, e.g. `sat-paper-2016`, `sat-digital`, `center-test`, `common-test` |
| `language` | text | BCP 47, e.g. `ko`, `zh-Hans`, `ja`, `he`, `pt-BR`, `en` |
| `english_item_map` | jsonb, nullable | `{"<section>:<item>": "<section>:<item>"}` from the original's numbering to the official English version's, only when they differ (research R10) |
| `evidence` | jsonb | list of `{type, source_id \| artifact_sha256, note}`; must not be empty. Types: `release_page`, `statement` (an official statement that the paper was not released), `index_absence` (absent from an official listing the catalog marks `complete_listing: true`) |

Validation, enforced by the `catalog` stage before any write and fatal on failure:
- Every one of the 66 `(exam_id, year)` cells has ≥ 1 edition (FR-003).
- Every `evidence` item references an **approved** source or a stored artifact (FR-007).
- `ingested` editions have ≥ 1 artifact with role `paper` and ≥ 1 with role `key`, or
  else the edition is rejected with a message saying which role is missing.
- `exam_id` must be one of the six exams in `catalog.yaml`, and each exam must declare
  `publishes_keys: true` with evidence (FR-006).
- `not_released` editions need a `statement` or `index_absence` item. An `index_absence`
  item is valid only for a seed marked `complete_listing: true`. Absence from any other page
  is "not found", which is not evidence, and the cell stays unresolved until a maintainer
  records real evidence (research R1). The dataset card reports the two evidence types
  separately.

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
| `role` | text | `paper` \| `key` \| `key_erratum` \| `english` \| `audio` \| `statement` \| `index_page`; `english` is the official English version of this edition's paper (see below) |
| `media_type` | text | e.g. `application/pdf`, `audio/mpeg`, `text/html` |
| `published` | date, nullable | required for `key_erratum`; optional for `key`, since many ordinary key files carry no date |
| `bytes` | bigint | |

Changed content at the same URL gets a new `sha256` and a new row. Old rows and blobs are
never removed (FR-009 edge case).

**Official English versions are artifacts, not editions.** An official English version
of a paper (e.g. the Psychometric exam's English version) is attached to the original
edition as an artifact with role `english`. `extract` never reads it, so its text never
enters `questions`. `translate` parses it in memory and writes the paired text only as
`official` rows in `translations`. That keeps official English in one store (FR-016,
constitution Principle IV) and keeps the English version out of exports and cell coverage
as a second copy of the same questions. An English paper whose questions differ from the
original is not a translation: it is catalogued as its own edition with `language: en`,
and its questions are English-original (spec Edge Cases). Official versions in other
languages are not catalogued in this feature and are listed as a known gap in the dataset
card.

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
| `print_order` | int, not null | position of the question in the edition, in page and reading order, from 1; the export sorts by it, so item `10` never sorts before `2` |
| `stem` | text | original language |
| `choices` | jsonb | ordered list of `{label, text}` in original labels (`①`, `A`, `1`…); empty for free response |
| `has_figure`, `has_table`, `has_audio`, `is_free_response` | bool, not null | FR-011 |
| `stimulus_id` | text FK, nullable | |
| `source_url`, `artifact_sha256`, `page`, `pipeline_version` | not null | provenance (SC-002); `pipeline_version` is the git SHA, with a `-dirty` suffix when pipeline code (`pipeline/**/*.py`, `pipeline/schema.sql`) has uncommitted changes |
| `content_hash` | text, not null | excludes `pipeline_version` (R7) |
| `retired` | bool, not null | true when the latest `extract` run no longer produces this question |

`stimuli` has the same `retired` column.

### `answer_keys`

| Column | Type | Rules |
|--------|------|-------|
| `key_id` | bigserial PK | surrogate only; never used for ordering |
| `question_id` | text FK, not null | |
| `labels` | text[], not null | sorted set of original choice labels, ≥ 1 element (FR-012); for free response, the official answer string(s) |
| `artifact_sha256` | text, not null | must be an artifact with role `key` or `key_erratum` for the same edition |
| `page` | int, not null | |
| `is_erratum` | bool, not null | the artifact's role is `key_erratum` |
| `published` | date, nullable | the artifact's `published` date; null for an undated ordinary key |
| `pipeline_version` | text, not null | |
| unique | `(question_id, labels, artifact_sha256)` | re-running inserts nothing |

**Current key**: the row with the greatest `(is_erratum, published NULLS FIRST,
artifact_sha256)` for the question, an order that uses only stable artifact data, so a
rebuild picks the same key. An erratum always outranks an ordinary key. If two rows of the
top `(is_erratum, published)` rank have different `labels`, the official sources disagree,
so the `key` stage writes a `key_conflict` failure and the view gives the question no
current key. The tie-break is never used to pick a winner silently.

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
original text. FR-014 covers only English that is not the question's original language
(spec Clarifications, analysis session): for an English-original edition, FR-010 already
requires that text in the question record, and a copy in `translations` would be a second
store of the same text (FR-016). See research R10.

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

**No silent empty editions** (SC-002). When `extract` produces zero questions for an
`ingested` edition and has not already written a whole-edition failure (such as
`artifact_unreadable`), it writes `item_split_failed` with `subject_id =
edition:<edition_id>` and a `detail` giving the page count. So every ingested edition has
question rows or failure rows.

**Reason codes** (`pipeline.extract.ReasonCode`, a `StrEnum`; a test asserts the SQL
`CHECK` list is identical): `artifact_unreadable`, `unsupported_format`,
`ocr_low_confidence`, `item_split_failed`, `choices_not_found`, `stimulus_unresolved`,
`no_official_key`, `key_label_mismatch`, `key_conflict`, `translation_pairing_mismatch`,
`mt_failed`, `mt_empty_output`.

### `exam_checks`

Derived from the ledger's `handcheck` events: `exam_id`, `seed`, `event_seq`, `passed`,
`n_items`, `n_failed`, and `items` (jsonb: per sampled question its `question_id`,
`content_hash`, `key_labels`, and verdicts). Exam verification status (`unchecked` \|
`verified` \| `unverified`) is computed by `verify.status()` from these rows *and* the
current questions and keys (research R14), so a verified exam whose sampled questions have
since changed drops back to `unchecked`. The status is written into every snapshot, and the
rows themselves are snapshotted too.

### `snapshots`

`version` (PK, `snap-NNNN`), `git_sha`, `catalog_sha256`, `ledger_sha256`,
`content_sha256`, `row_counts` (jsonb), `uri`. `content_sha256` identifies what the
snapshot holds (research R11), so an unchanged store produces no new version (FR-019).
This is the registry of published snapshots. It is the one table a
snapshot does not copy, because its own row is written only after the snapshot is
published (research R11).

## View: `questions_flat` (FR-017)

One row per non-retired question, combining:
- every `questions` column;
- `edition_kind`, `lineage`, `language`, and `exam_id` from `exam_editions`;
- the **current key**: `key_labels`, `key_artifact_sha256`, `key_page`;
- the **preferred English**: `english_stem`, `english_choices`, `translation_source`
  (`original` for English-original editions, otherwise `official` or `mt`), and the
  translation's provenance;
- the stimulus: `stimulus_modality`, `stimulus_english` (the preferred translation of a
  text stimulus), and that translation's own provenance: `stimulus_translation_source`,
  `stimulus_translation_artifact_sha256`, `stimulus_mt_model`, `stimulus_mt_model_revision`,
  and `stimulus_mt_prompt_hash`. A stem and its stimulus can come from different sources;
- `eligible` (bool) and `ineligible_reason` (text, null when eligible).

`eligible` is true exactly when all of these hold (spec Assumptions): no figure, table,
audio, or free-response flag; `jsonb_array_length(choices) >= 2`; a current key exists
(none exists under `key_conflict`); it has exactly one label, and that label is among the
question's current choice labels; English exists; and the stimulus, if any, is `text`
with English. `ineligible_reason` names the first rule that fails, in the order listed.
The view does not apply the exam-level `unverified` exclusion. The export applies it,
reading `exam_status.parquet` from the same snapshot (contracts/outputs.md).

## View: `integrity_violations` (SC-002, SC-004)

One row per `(check_name, subject_id)` that breaks an invariant the schema's constraints cannot
express on their own. `snapshot` counts these rows inside its transaction and refuses to
publish (exit 3) if any exist, printing counts per check, never text. The test suite
checks the same view on synthetic data. The checks:

| `check_name` | A row means |
|--------------|-------------|
| `edition_without_output` | an `ingested` edition has no non-retired question and no failure row |
| `key_not_official` | an `answer_keys` row's `artifact_sha256` is not an artifact with role `key` or `key_erratum` for the question's edition |
| `english_missing` | a non-retired question (or a text stimulus) in a non-English edition has no preferred translation and no `translate`-stage failure |
| `question_without_artifact` | a question's `artifact_sha256` is not an artifact of its edition |

Missing editions and missing provenance need no check: foreign keys and `NOT NULL` already
make them impossible.

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

**Key currency**: a new `key_erratum` artifact adds a row that outranks the ordinary key
(and any earlier erratum), so it becomes current. The older row stays for audit (spec edge
case).

**Exam verification**: `unchecked` → `verified` | `unverified`. `verified` → `unchecked`
when any sampled question's current content hash or key differs from the recorded one.
`unverified` → `verified` only under the recovery rule: a new seed, every previously failed
question changed, and those questions re-checked (research R14).

## Volume estimate

| Table | Rows (order of magnitude) |
|-------|---------------------------|
| `exams` | 6 |
| `exam_editions` | 10² |
| `sources` | 10³ |
| `artifacts` | 10³ |
| `questions` | 10⁴–10⁵ |
| `answer_keys`, `translations` | ≈ `questions` |
| `extraction_failures` | 10²–10⁴ |
