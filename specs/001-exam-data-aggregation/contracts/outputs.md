# Contract: Outputs

## Snapshot layout

`<blob root>/snapshots/snap-NNNN/`:

| File | Content |
|------|---------|
| `manifest.json` | `{version, git_sha, catalog_sha256, ledger_sha256, row_counts: {table: n}, files: {name: sha256}}` |
| `exam_editions.parquet`, `sources.parquet`, `artifacts.parquet` | the catalog dataset |
| `questions.parquet`, `stimuli.parquet`, `answer_keys.parquet`, `translations.parquet`, `extraction_failures.parquet` | the questions dataset |
| `questions_flat.parquet` | the materialized view; the **only** input to `export` (FR-017, FR-023) |
| `exam_status.parquet` | `exam_id`, `status` (`unchecked`\|`verified`\|`unverified`), latest seed |
| `data_dictionary.md` | every table and column, generated from `schema.sql` comments (FR-022) |
| `dataset_card.md` | see below |

Snapshots are written once and never modified. The `snapshot` stage refuses to write over
an existing version.

### Dataset card sections

1. Coverage matrix: 6 exams × 11 years, one cell each, listing editions by status
   (`ingested`/`not_released`/`blocked`) and kind (`administered`/`practice`), with
   question counts.
2. Known gaps: `not_released` and `blocked` editions with reasons; Gaokao provincial
   papers (not yet catalogued, FR-002); failure counts by reason code.
3. Verification: per-exam hand-check status, seed, and result. Per-language translation
   review score distribution, reported as observed with no threshold (SC-004).
4. Translation provenance: MT model, reported revision, prompt hash, and the share of
   official vs MT English per exam.
5. Contamination notes: per exam from `card_notes`, plus the standing note that public exam
   papers are very likely in model training data. Within-condition comparisons survive
   this, but absolute scores do not mean much (CLAUDE.md Phase 3).
6. Harness limitations: the count of exported questions whose answer is beyond `D`, and
   the TODO reference (research R3). Multi-answer questions are stored but not exported.

## Suite question bank: `questions.json`

It keeps the existing suite format that `harness.config.load_suite` reads, so the harness
needs no change (FR-023). The harness reads `question_id`, `index`, `question`, `choices`,
and `answer`, and ignores the other keys.

```json
{
  "provenance": {
    "dataset": "exam-questions",
    "snapshot": "snap-0003",
    "exam": "enem",
    "eligible_only": true,
    "verification": "verified",
    "question_count": 412,
    "answers_beyond_d": 87,
    "edition_kinds": {"administered": 412, "practice": 0}
  },
  "questions": [
    {
      "question_id": "enem-2019-day1-azul:linguagens:12",
      "index": 1,
      "question": "<stimulus English>\n\n<question English stem>",
      "choices": ["A) …", "B) …", "C) …", "D) …", "E) …"],
      "answer": "C",
      "provenance": {
        "edition_id": "enem-2019-day1-azul",
        "edition_kind": "administered",
        "canonical_question_id": "cq_…",
        "content_hash": "…",
        "source_url": "https://…",
        "artifact_sha256": "…",
        "page": 7,
        "stimulus_id": "enem-2019-day1-azul:linguagens:stim-4",
        "original_labels": ["A", "B", "C", "D", "E"],
        "key_artifact_sha256": "…",
        "key_page": 2,
        "translation_source": "mt",
        "mt_model": "claude-sonnet-5-5",
        "mt_model_revision": "…",
        "mt_prompt_hash": "…"
      }
    }
  ]
}
```

Rules:
- Only rows with `eligible = true` from the snapshot's `questions_flat` are included, and
  the exam must not be `unverified` (FR-024). `unchecked` exams are exported with
  `"verification": "unchecked"`.
- `choices` are relabelled `A)`, `B)`, … in original order. `answer` is the relabelled
  letter of the single key label, and `original_labels` keeps the mapping.
- For a question with a text stimulus, `question` is the stimulus English, a blank line
  (`\n\n`), then the stem English, and `stimulus_id` is set (FR-023). Otherwise
  `stimulus_id` is null.
- Questions are ordered by `(edition_id, section, item)`, with `index` running 1..N. The
  JSON is written with `sort_keys`, `ensure_ascii=False`, `indent=2`, and a trailing
  newline. There are no timestamps. The same snapshot and exam always produce identical
  bytes.
- Exported banks hold corpus text, so they are written to a gitignored path and never
  committed.
