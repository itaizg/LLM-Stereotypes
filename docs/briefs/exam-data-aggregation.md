# Mission Brief — Cross-National Exam Question Dataset

## Goal

Deliver two linked, versioned datasets for six yearly national admissions exams (2015–2025):
- an **exam catalog** that records every edition and whether it was released;
- a **questions dataset** of every officially released question from those editions, with one row per question, a fixed schema, full provenance, and the original text plus English.

Both live in Postgres with Parquet snapshots in S3, and suite-ready `questions.json` banks are exported from the questions dataset.

## Constraints

**Stack:** Python 3.13+, asyncio + REST (`curl` only for recorded debugging), Google Programmable Search JSON API, PySpark, AWS S3, Postgres.

**Scope:**
- Exams: SAT, Psychometric (NITE), CSAT (KICE), Gaokao, Common Test, ENEM.
- Years: 2015–2025, which is 66 (exam, year) cells in total. Each cell expands into one or more editions (sitting, form, or province).
- All question types are ingested. Figure, table, audio, and free-response items are flagged ineligible for suite export, never dropped.
- Pipeline steps are well separated.

**Dataset shape:**
- **Exam catalog (`exam_editions`):** one row per exam × year × sitting/form/province, with `edition_id`, status (`ingested` / `not_released` / `blocked`), official release date, official URL, and evidence. It is complete even for editions that yielded no questions.
- **Questions dataset:** tables `questions`, `translations`, `answer_keys`, and `extraction_failures`. Every row references an `edition_id`.
- **Supporting tables:** `sources` (discovered URLs) and `artifacts` (raw files), each linked to an edition.
- `questions` holds original-language content only: a stable `question_id`, an `edition_id`, a `canonical_question_id` (dedupe link), section, original-language text and choices, modality flags, and provenance (URL, artifact SHA-256, page, pipeline git SHA).
- `translations` and `answer_keys` are the only stores for English text and official keys. Each row links to a `question_id` and records its own provenance. A translation row carries `translation_source` (official or MT) and, for MT, model, revision, and prompt hash. A key row carries the official artifact SHA-256 and page. Each is written by one pipeline stage only, and no stage writes those fields anywhere else.
- `questions_flat` is a view that joins `questions` to its one current `answer_keys` row and its preferred `translations` row. The preferred translation is official over MT, then latest over earlier. The `questions.json` export reads this view, so nothing is stored twice and consumers cannot see conflicting values.
- Both datasets are delivered with a data dictionary and a dataset card: coverage per cell, known gaps, and contamination notes.

**Rules:**
1. Search is used only to discover candidate URLs, and every URL is reviewed before use. Fetch only from allowlisted official domains, respecting robots.txt and rate limits.
2. Raw files go into S3 under their content hash and are never modified. Spark jobs are idempotent.
3. Answer keys come only from official artifacts, never from an LLM. Questions without a key go to `extraction_failures` with a reason code.
4. MT is used only where no official English edition exists. MT rows are flagged and record model, revision, and prompt hash.
5. No question text goes into git. Only fetchers, parsers, manifests, and hashes are committed, and fixtures are synthetic.
6. Tests and CI run offline: local Postgres, local Spark, no AWS. Secrets come from env vars only.
7. CLAUDE.md is amended to drop the no-heavyweight-deps rule and reword differentiator #2.
8. The catalog is built first. Question extraction runs only for editions marked `ingested`.
9. Do not add any exam without published answer keys. 
10. Make sure code follows DRY, CoC, LOD, KISS, YAGNI, couple/decouple, SOLID
11. Try to make the new file count <=20 if possible.

## Success Criteria

1. **Catalog complete.** All 66 cells (6 exams × 11 years) have at least one edition in `exam_editions`. Every edition is `ingested`, `not_released`, or `blocked`, with evidence. None is `unknown`.
2. **Questions extracted.** Every `ingested` edition has questions or `extraction_failures` rows, and no question lacks a valid `edition_id`. Zero question rows have null provenance or a key without an official source (enforced by a CI test).
3. A hand-checked sample of 50 questions per exam matches the source in all cases.
4. Every non-English question has an English row or a recorded failure reason. A human-reviewed sample of 30 per language is scored.
5. Rebuilding from S3 reproduces identical per-question content hashes, and re-running a stage adds nothing.
6. Versioned Parquet snapshots of both datasets, the data dictionary, and the dataset card exist, and their row counts match Postgres.
7. `pipeline export --exam <x> --eligible` writes a `questions.json` that loads in `harness.config` and passes a `MockBackend` CI smoke test.
8. A CI check fails if tracked files contain corpus question text.

## Non-Goals

- Running models or producing results.
- Publishing the dataset or question text publicly.
- Generating, paraphrasing, or repairing questions or keys.
- Pre-2015 papers (TODO: "everything released").
- Cross-exam difficulty equating, streaming ingestion, or exams beyond the six.

## Open Risks

- Gaokao and Psychometric official release coverage is patchy, so the registry must tell "not released" apart from "not found".
- The SAT paper → digital format break needs to be a column.
- OCR quality on older Korean, Chinese, and Japanese scans.
- At roughly 10⁴–10⁵ questions, Spark/S3 needs a defensible reason. Suggest a fallback to pandas if necessary
