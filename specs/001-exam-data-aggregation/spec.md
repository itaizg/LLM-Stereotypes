# Feature Specification: Cross-National Exam Question Dataset (Data Aggregation)

**Feature Branch**: `docs/data-agg-spec`

**Created**: 2026-11-04

**Status**: Draft

**Input**: User description: "Specify the data aggregation feature as described in the brief, and follow the constitution" — brief: [`docs/briefs/exam-data-aggregation.md`](../../docs/briefs/exam-data-aggregation.md); governing rules: [`.specify/memory/constitution.md`](../../.specify/memory/constitution.md) v1.0.0.

## Overview

The evaluation harness currently has one hand-transcribed question bank (31 SAT questions).
That bank is too small for the experiments the harness is built to run, and it comes from a
single exam in a single language. This feature builds two linked, versioned datasets that
cover six national university-admissions exams over 2015–2025:

- an **exam catalog** recording every edition of every exam in scope and whether it was
  officially released, with evidence; and
- a **questions dataset** holding every officially released question from those editions,
  one record per question, with full provenance, the original-language text, an English
  rendering, and the official answer key.

From the questions dataset the maintainer exports suite-ready question banks that the
harness loads without any harness code change. The feature produces data only: it runs no
models and reports no results.

## User Scenarios & Testing *(mandatory)*

Actors:

- **Maintainer**: the repository author, who runs the pipeline, reviews candidate
  sources, and hand-checks samples.
- **Evaluation harness**: the downstream consumer of exported question banks.
- **Reviewer**: a reader (e.g. an interviewer) who audits a dataset row back to its source.

### User Story 1 - Know what exists before extracting anything (Priority: P1)

The maintainer builds a catalog of every edition (sitting, form, or province paper) of each
of the six exams for each year 2015–2025. Each edition is marked as ingested, not released,
or blocked, and carries the evidence for that status: the official release date and URL,
or the official statement or search record showing it was not released.

**Why this priority**: The brief requires the catalog first, and extraction runs only for
editions marked ingested. The catalog alone is a useful result: an honest coverage map that
separates "not released" from "not found", which is the main risk for Gaokao and the
Psychometric exam.

**Independent Test**: Build the catalog from recorded (synthetic) discovery results and
check that all 66 exam-year cells have at least one edition, every edition has one of the
three allowed statuses with evidence attached, and none is unknown.

**Acceptance Scenarios**:

1. **Given** an exam-year cell where the official body published the paper, **When** the
   catalog is built, **Then** each published edition appears as `ingested` with its
   release date, official URL, and the stored source file it points to.
2. **Given** an exam-year cell where the official body states the paper is not released,
   **When** the catalog is built, **Then** the edition appears as `not_released` with that
   statement recorded as evidence.
3. **Given** a paper that is released but cannot be fetched under the acquisition rules
   (robots.txt disallows it, it is only on a non-allowlisted domain, or it is behind a
   login), **When** the catalog is built, **Then** the edition appears as `blocked` with
   the reason recorded.
4. **Given** a candidate URL found by web search, **When** the maintainer has not approved
   it, **Then** nothing is fetched from it and it does not count as evidence.

---

### User Story 2 - Extract questions with official keys and full provenance (Priority: P2)

For every ingested edition, the maintainer extracts each question in its original language:
stem, choices, section, and modality flags (figure, table, audio, free-response). Each
question is linked to its official answer key and can be traced to the exact source file,
page, and pipeline version that produced it. Questions that cannot be extracted or have no
official key are recorded as failures with a reason code; they are never dropped silently.

**Why this priority**: The question records are the dataset. Without official keys and
provenance, the data cannot be defended or used for scoring.

**Independent Test**: Run extraction on a synthetic ingested edition (a fixture paper plus
a fixture key) and check that every question has a valid edition, complete provenance, and
either an official key or a failure record. Then run the stage again and check that nothing
new is added.

**Acceptance Scenarios**:

1. **Given** an ingested edition with an official key file, **When** extraction runs,
   **Then** each question gets a stable identifier, its original-language content, modality
   flags, and provenance (source URL, source file content hash, page, pipeline version),
   and its key record cites the official key file's content hash and page.
2. **Given** a question whose key is missing from the official key file, **When**
   extraction runs, **Then** the question is recorded as a failure with a "no official
   key" reason code. No key is inferred, generated, or taken from a non-official source.
3. **Given** a question with a figure, table, audio, or free-response format, **When**
   extraction runs, **Then** the question is stored with the matching modality flag and is
   marked ineligible for suite export, not discarded.
4. **Given** the same question appearing in two editions, **When** extraction runs,
   **Then** both records are kept and both point to one shared canonical question. Two
   questions are the same only if their normalized original-language stem and choices are
   identical; near-matches are not linked (FR-010).
5. **Given** a stage that has already completed, **When** it is run again on the same
   inputs, **Then** no records are added or changed.

---

### User Story 3 - English for every question, with official text preferred (Priority: P3)

For every non-English question, an English rendering exists, or a failure record explains
why it does not. Official English editions are always used where they exist. Machine
translation fills in only where no official English exists, and each machine-translated
record says which model, revision, and prompt produced it.

**Why this priority**: The harness's existing suite is English-language. Cross-exam use
depends on English, but correctness of the original data (P1, P2) comes first.

**Independent Test**: With a synthetic non-English edition that has an official English
version for half its questions, run the translation stage and check that official English
is chosen for that half and that the rest is flagged machine translation with model,
revision, and prompt hash recorded.

**Acceptance Scenarios**:

1. **Given** a question with an official English version, **When** the preferred English
   text is resolved, **Then** the official version is chosen over any machine translation.
2. **Given** a question with several translations of the same kind, **When** the
   preferred English text is resolved, **Then** the latest one is chosen.
3. **Given** a question with no official English, **When** the translation stage runs,
   **Then** a machine-translation record is created and flagged as such, with model,
   revision, and prompt hash recorded. If translation fails, a failure record with a
   reason code is created instead.

---

### User Story 4 - Export a suite-ready question bank (Priority: P4)

The maintainer exports the eligible questions of one exam as a question bank in the suite
format the harness already reads. The harness loads it and runs a mock-model smoke test
without any change to harness code.

**Why this priority**: This is how the dataset reaches the harness, but it is only worth
doing once P1–P3 data exists.

**Independent Test**: Export from a synthetic dataset and load the result through the
harness's existing suite loader, then run the offline smoke test against the deterministic
mock model.

**Acceptance Scenarios**:

1. **Given** an exam with eligible questions, **When** the maintainer exports it with the
   eligible-only option, **Then** the bank contains only text-only, single-answer multiple-choice,
   officially keyed questions with English text, each carrying its provenance. A question
   that uses a text shared passage carries that passage's English text before its stem.
2. **Given** an exported bank, **When** the harness loads it, **Then** it loads and the
   mock-model smoke test passes, with no harness code changed.
3. **Given** a question whose current key or preferred translation changes, **When** the
   bank is exported again from a new snapshot, **Then** the export reflects only the new
   current values. The export never shows two conflicting values for the same question.
4. **Given** a snapshot, **When** the same exam is exported from it twice, **Then** the two
   files are byte-identical and both record that snapshot's version.

---

### User Story 5 - Reproducible, documented, versioned snapshots (Priority: P5)

The maintainer produces versioned snapshots of both datasets, with a data dictionary and a
dataset card (coverage per exam-year cell, known gaps, contamination notes). The whole
dataset can be rebuilt from the stored raw files with identical per-question content.

**Why this priority**: Reproducibility and documentation make the dataset defensible, but
they wrap the data produced in P1–P4.

**Independent Test**: Rebuild a synthetic dataset from its stored raw files into an empty
store, and compare per-question content hashes and snapshot row counts with the original.

**Acceptance Scenarios**:

1. **Given** the stored raw files, **When** the dataset is rebuilt from scratch, **Then**
   every per-question content hash is identical to the previous build.
2. **Given** a published snapshot version, **When** its row counts are compared with the
   live store at that version, **Then** they match for every table.
3. **Given** a snapshot, **Then** a data dictionary describing every field and a dataset
   card with per-cell coverage, known gaps, and contamination notes are delivered with it.

---

### Edge Cases

- **Key published separately from the paper** (different file, date, or URL): the key is
  linked to the same edition, and its own source file hash and page are recorded.
- **Corrected answer key** (an official erratum or rescored item): a new key record is
  added and becomes current. The earlier record is kept for audit and is no longer current.
- **File changes at the same URL**: the new content gets a new content hash and is stored
  as a new raw file. The earlier file is never overwritten.
- **Official URL disappears later**: the stored raw file remains the source of truth, and
  the rebuild does not need the live URL.
- **Search result on a non-allowlisted domain** (e.g. a mirror or prep site): it is
  recorded as a rejected candidate and never fetched, even if it holds the paper.
- **Shared stimulus** (one passage, chart, or listening clip used by several questions):
  the stimulus is stored once and each question records which one it uses. On export, a
  text stimulus's English text is placed before each question's stem, so the question keeps
  its context; a figure or audio stimulus makes the question ineligible (FR-023).
- **Format breaks within an exam** (e.g. SAT paper → digital, the 2016 SAT redesign, Japan's
  National Center Test → Common Test in 2021): the edition records its format/lineage, and
  the cell is not reported as missing.
- **Scanned papers with poor OCR** (older Korean, Chinese, and Japanese scans): low-confidence
  extraction becomes a failure record with a reason code, never a silently wrong record.
- **An exam offered in several official languages** (e.g. the Psychometric exam): official
  non-Hebrew versions are treated as official translations, or as separate editions if
  their questions differ.
- **A cell with no edition at all** (e.g. a sitting cancelled): the cell still gets an
  edition row with evidence, so cell coverage stays complete.

## Requirements *(mandatory)*

### Functional Requirements

**Scope and catalog**

- **FR-001**: The system MUST cover exactly six exams (SAT; Psychometric Entrance Test by
  NITE; CSAT by KICE; Gaokao; Japan's Common Test and its predecessor, the National Center
  Test; ENEM) for the years 2015–2025, i.e. 66 exam-year cells.
- **FR-002**: The catalog MUST hold one record per edition (exam × year × sitting/form/
  province). Each record MUST have a stable edition identifier, a status of exactly one of
  `ingested`, `not_released`, or `blocked`, the official release date and URL when they
  exist, a format/lineage field, an edition kind (`administered` or `practice`), and the
  evidence for its status. For Gaokao, this feature catalogs only the nationally set papers
  (National I/II/III, New Curriculum I/II). Papers set independently by provinces are a
  planned later step. The catalog MUST be able to add them as new editions without any
  schema change, and until then the dataset card MUST list them as a known gap.
- **FR-003**: Every exam-year cell MUST have at least one catalog record, including cells
  that yield no questions. No record may have an unknown or missing status.
- **FR-004**: The catalog MUST be complete before question extraction runs, and extraction
  MUST run only for editions marked `ingested`.
- **FR-005**: Official practice tests published by an exam body (e.g. College Board SAT
  practice tests) MUST count as editions. They are marked edition kind `practice` and
  assigned to the year of their official release. Released administered forms are marked
  `administered`. Exports and coverage reports MUST be able to tell the two kinds apart.
- **FR-006**: An exam MUST NOT be added to the scope unless its official body publishes
  answer keys.

**Source acquisition**

- **FR-007**: Web search MUST be used only to discover candidate URLs. Every candidate MUST
  be recorded and approved by the maintainer before it is fetched or used as evidence.
- **FR-008**: Files MUST be fetched only from allowlisted official domains, respecting
  robots.txt and a per-domain rate limit. Fetch attempts that violate these rules MUST be
  refused and recorded.
- **FR-009**: Every fetched raw file MUST be stored once under its content hash and MUST
  never be changed afterwards. Each discovered URL and each raw file MUST be linked to an
  edition.

**Questions, keys, translations**

- **FR-010**: Each question record MUST hold original-language content only: a stable
  question identifier, the edition identifier, a canonical-question link for duplicates, a
  section, the stem and choices, modality flags (figure, table, audio, free-response), a
  shared-stimulus reference where applicable, and provenance (source URL, raw file content
  hash, page, pipeline version). Two questions MUST share a canonical question exactly when
  their original-language stem and choices are identical after Unicode NFKC normalization,
  whitespace collapsing, and punctuation removal. Near-matches MUST NOT be linked, so the
  link is deterministic across rebuilds (FR-020).
- **FR-011**: All question types MUST be ingested. Figure, table, audio, and free-response
  questions MUST be flagged ineligible for suite export and MUST NOT be dropped.
- **FR-012**: Answer keys MUST come only from official artifacts. Each key record MUST link
  to a question and cite the official raw file's content hash and page. A key is a set of one
  or more choice labels, valid for any number of choices. Multi-answer questions are stored
  in full but are ineligible for suite export; single-answer questions are eligible whatever
  their choice count. Keys MUST NOT be
  generated, inferred, or repaired by a model or by hand.
- **FR-013**: A question without an official key, or one that cannot be extracted
  reliably, MUST produce a failure record with a reason code from a fixed, documented
  list.
- **FR-014**: English text MUST be stored only as translation records linked to a
  question. Each record MUST state its source (official or machine translation) and its
  own provenance. Machine-translation records MUST also state model, model revision, and
  prompt hash.
- **FR-015**: Machine translation MUST be used only when no official English version of
  that question exists.
- **FR-016**: Official English text and answer keys MUST each have exactly one
  authoritative store, each written by exactly one pipeline stage. No stage may write these
  values anywhere else.
- **FR-017**: A single flattened view MUST combine each question with its one current key
  and its preferred translation (official over machine, then latest over earlier). All
  exports MUST read from this view.

**Pipeline behavior**

- **FR-018**: The pipeline MUST be split into separate stages (at minimum: catalog,
  discover, fetch, extract, key, translate, export, snapshot). Each stage MUST be runnable
  on its own.
- **FR-019**: Every stage MUST be idempotent. Re-running it on unchanged inputs MUST add
  and change nothing.
- **FR-020**: Rebuilding from stored raw files MUST reproduce identical per-question
  content hashes.

**Delivery**

- **FR-021**: The system MUST produce versioned snapshots of both datasets whose row counts
  match the live store for that version.
- **FR-022**: A data dictionary (every table and field) and a dataset card (coverage per
  exam-year cell, known gaps, training-data contamination notes) MUST be delivered with
  each snapshot.
- **FR-023**: The maintainer MUST be able to export the eligible questions of one exam as
  a question bank in the existing suite format, which the harness loads unchanged
  (constitution Principle V: new suites through config only). For a question that uses a
  text shared stimulus, the export MUST put the stimulus's preferred English text, then a
  blank line, before the question stem in the question field, and MUST record the stimulus
  id in the question's provenance. Exports MUST be built only from a named snapshot, never
  from the live store, and each bank MUST record that snapshot's version. Exporting the same
  exam from the same snapshot MUST produce a byte-identical file.
- **FR-024**: The system MUST support a hand-check of a random, seeded sample of 50
  questions per exam against the source, and a human-reviewed, scored sample of 30
  translated questions per language, with results recorded. If any sampled question fails
  to match the source in stem, choices, or key, the exam MUST be marked `unverified`: it is
  excluded from suite export and labelled `unverified` in snapshots until its extraction is
  fixed, its questions are re-extracted, and a new 50-question sample, drawn with a new
  recorded seed, passes in full.

**Integrity and repository rules (brief rules 5–6, constitution)**

- **FR-025**: The bulk corpus MUST NOT enter version control. What is committed is
  fetchers, parsers, manifests, hashes, and small question samples. A sample MAY contain
  only openly licensed questions: public domain, or released under a licence that permits
  redistribution. Each sample file MUST be listed in a sample allowlist that gives its
  source, provenance, question count, licence, and evidence for that licence (the
  official licence statement and its URL). Each sample MUST stay under the per-exam size
  cap. Test fixtures stay synthetic.
- **FR-026**: An automated check MUST fail if any tracked file outside the sample
  allowlist contains corpus question text, if an allowlisted sample goes over its cap, or
  if an allowlist entry has no open licence and licence evidence.
- **FR-027**: All tests MUST run offline and deterministically: no AWS or other cloud
  access, no internet, no API keys, no GPU (constitution Principle III). Any database or
  batch engine a test needs MUST run locally.
- **FR-028**: Secrets MUST come only from environment variables and MUST never be
  committed, printed, or logged.
- **FR-029**: All new code MUST meet the constitution's gates: fully type-annotated and
  clean under ty, clean under ruff check and format, and covered by pytest tests that fail
  without the behavior they test. Every behavior change ships with a test.
- **FR-030**: The harness MUST NOT import from or depend on the pipeline. Dependencies
  needed only by the pipeline MUST be installable separately, so the core install stays
  unchanged and GPU-free (constitution Project Constraints, Principle V).
- **FR-031**: Every query against the store MUST use parameters, never string-built SQL
  (team directive CDR-2026-021).
- **FR-032**: The "no heavyweight dependencies" rule is obsolete (author decision,
  2026-11-04). This feature MUST remove it from both CLAUDE.md and the constitution (Project
  Constraints → Dependencies), so the two stay consistent. Until that amendment merges, the
  current rule still governs, and no pipeline dependency that it prohibits may be added.
  The constitution change MUST follow its own amendment process: a pull request
  that edits the constitution, updates the Sync Impact Report, and bumps the version. It
  MUST land before or together with the first pipeline dependency. The rest of the
  Dependencies constraint is unchanged: Python 3.13, `uv`, the lockfile, and the GPU-free
  core install.
- **FR-033**: CLAUDE.md differentiator #2 ("Small enough to own end-to-end") MUST be
  reworded to stay true once the dataset pipeline exists (brief rule 7).
- **FR-034**: The feature SHOULD add no more than 20 new files (brief rule 11). Any
  overrun MUST be justified in the pull request.

### Key Entities *(include if feature involves data)*

- **Exam**: one of the six admissions exams in scope. It has an official body, an
  allowlisted set of official domains, and an original language.
- **Exam Edition**: one released or unreleased paper of an exam in a year (by sitting,
  form, or province). It has a status, evidence, release date, official URL,
  format/lineage, and kind (`administered` or `practice`). Each edition belongs to exactly one exam-year cell.
- **Source**: a discovered candidate URL with its discovery method, review decision, and
  linked edition.
- **Artifact**: an immutable raw file, identified by its content hash, fetched from an
  approved source and linked to an edition.
- **Question**: one question's original-language content, modality flags, shared-stimulus
  reference, canonical-question link, and provenance. It belongs to one edition.
- **Shared Stimulus**: a passage, figure, or audio item that several questions refer to.
  It is stored once, with its own modality flag and English translation.
- **Translation**: an English rendering of one question. It is official or machine-made,
  with provenance, and for machine translations also model, revision, and prompt hash.
- **Answer Key**: the official correct answer(s) for one question, as a set of one or more
  choice labels, citing the official artifact hash and page. A question may have several over time, and exactly one is current.
- **Extraction Failure**: a record that a question (or edition) could not be extracted,
  keyed, or translated, with a reason code from a fixed list.
- **Flattened Question View**: a read-only combination of a question with its current key
  and its preferred translation, as captured in a snapshot. The snapshot's copy of this view
  is the only input to exports.
- **Snapshot**: a versioned, immutable copy of both datasets, delivered with its data
  dictionary and dataset card.
- **Suite Question Bank**: an exported file in the harness's existing suite format,
  holding eligible questions of one exam and recording the snapshot version it came from.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All 66 exam-year cells have at least one edition. 100% of editions have one of
  the three allowed statuses with evidence attached, and 0 have an unknown status.
- **SC-002**: 100% of ingested editions have question records or failure records. 0 question
  records lack a valid edition, 0 have missing provenance, and 0 keys lack an official
  source. These counts are checked automatically on every change.
- **SC-003**: In a hand-checked, seeded random sample of 50 questions per exam (300 total),
  100% match the source in stem, choices, and key. No exam that failed its latest check
  appears in a suite export.
- **SC-004**: 100% of non-English questions have an English record or a recorded failure
  reason. A human-reviewed sample of 30 translations per language is scored, and the scores
  are published in the dataset card. No pass threshold is set in advance, and scores are
  reported as observed.
- **SC-005**: A from-scratch rebuild from stored raw files reproduces 100% of per-question
  content hashes, and re-running any stage adds 0 records.
- **SC-006**: Each snapshot's row counts match the live store exactly for every table, and
  each snapshot is delivered with its data dictionary and dataset card.
- **SC-007**: For every exam with eligible questions, an exported bank loads into the
  harness and passes the offline mock-model smoke test, with 0 harness code changes.
- **SC-008**: The automated repository check finds 0 corpus question strings in tracked
  files outside the sample allowlist, 0 samples over their cap, and 0 samples without
  open-licence evidence. It is shown to fail in each of three cases: a known corpus string
  planted outside the allowlist, an allowlisted sample pushed over its cap, and an
  allowlist entry with its licence removed.
- **SC-009**: The full test suite runs with networking disabled and no cloud credentials,
  and produces the same results on repeated runs.

## Assumptions

- **Delivery constraints from the brief** (decided by the stakeholder; for planning, not
  part of the requirements above): Python 3.13+, asyncio with REST clients, Google
  Programmable Search JSON API for discovery, PySpark for batch jobs, AWS S3 for raw files
  and Parquet snapshots, Postgres as the live store. The brief's open risk applies: at about
  10⁴–10⁵ questions, the batch engine needs a defensible reason, and the plan should
  evaluate a pandas fallback. That choice is now a pure engineering trade-off, since FR-032
  removes the dependency-weight rule.
- "Blocked" means released but not legally or technically fetchable under FR-008 (robots,
  non-allowlisted host, login or paywall). It is distinct from `not_released`.
- Japan's National Center Test (2015–2020) is treated as the predecessor lineage of the
  Common Test (2021–), so all 11 Japanese cells are in scope. The format/lineage field
  records the change. The same field records the SAT 2016 redesign and the paper → digital
  change.
- Official translations published by an exam body (e.g. the Psychometric exam's English
  version) count as `official` translations.
- Eligibility for suite export means: text-only, multiple-choice with exactly one correct
  choice, has a current official key, has a preferred English translation, and either uses
  no shared stimulus or uses a text-only one that itself has a preferred English translation.
  The question's exam must also not be `unverified` (FR-024).
- The 50-per-exam and 30-per-language samples are drawn with a recorded seed, so the same
  sample can be regenerated for audit.
- "Small sample" (FR-025) means at most 50 questions per exam across all committed sample
  files (confirmed). The cap is defined in one place, and changing it is a deliberate,
  reviewed commit.
- Whether an exam's questions are openly licensed is decided per source, from the official
  licence statement, and is never assumed. An exam with no openly licensed source simply
  has no committed sample. It is still fully covered in the private dataset.
- The existing SAT banks (`suites/stereotype_threat/questions.json` and the legacy root
  `questions.json`, College Board copyright) are not openly licensed. They were removed from
  tracking on 2026-11-04 and are kept local only. Tests and CI use a synthetic stand-in.
  They remain in git history: purging history is optional, as with the PDFs.
- Snapshots and raw files are stored privately. Publishing the dataset or its text is a
  non-goal, consistent with the brief and with the project's College Board copyright stance.
- Constitution Project Constraints / CLAUDE.md "Do not expand the science before Phases 0–2
  exist": the harness, statistics, and MockBackend already exist on `main`, so Phase 0–2
  is treated as met. This feature adds data and does not change any experiment.
- Pre-2015 papers, cross-exam equating, streaming ingestion, model runs, and exams beyond
  the six are out of scope (brief Non-Goals).

## Dependencies

- Existing harness suite loader and `MockBackend` (consumers of FR-023 / SC-007).
- Maintainer availability for source approval (FR-007) and the hand-check samples (FR-024).
- Official publication by each exam body. Coverage depends on what each body released,
  which is why `not_released` is a valid, evidenced outcome.
- Governance: the constitution amendment in FR-032 removes a project constraint. The
  version bump level (MINOR vs MAJOR under the constitution's semantic-versioning rule) is
  decided in that amendment PR.

## Clarifications

### Session 2026-11-04

- Q: For Gaokao, which papers count as editions? → A: National papers now; provincial
  papers as a later step, addable without schema change and listed as a known gap until
  then (FR-002).
- Q: Do official SAT practice tests count as editions? → A: Yes. They are marked edition
  kind `practice` and assigned to their release year, kept distinct from `administered`
  forms (FR-005).
- Q: How should brief rule 7's dependency change be reconciled with the constitution?
  → A: The "no heavyweight dependencies" rule is obsolete. It is to be removed from both
  CLAUDE.md and the constitution through the amendment process (FR-032). The amendment is
  not yet applied, so the rule stays in force until it merges.
- Correction (spec review): small question samples may be committed to git, which relaxes
  brief rule 5. They are bounded by an allowlist and a per-exam cap, the bulk corpus stays
  out, and test fixtures stay synthetic (FR-025, FR-026, SC-008).
- Follow-up: the cap of 50 questions per exam is confirmed. Committed samples may contain
  only openly licensed (public domain or redistributable) questions, with licence evidence
  in the allowlist (FR-025, FR-026).
- Q: When a question has more than one correct answer, or a choice count other than four,
  how is its key stored and exported? → A: The key is a set of one or more choice labels,
  for any number of choices. Multi-answer questions are stored but not exported, and
  single-answer questions are exported whatever their choice count (FR-012). Exporting
  multi-answer questions, which needs harness set-match scoring, is a high-priority item
  in TODO.md.
- Q: When several questions share one passage, chart or listening clip, how does the
  exported bank include it? → A: The dataset stores the stimulus once. On export, a text
  stimulus's English text is placed before each question's stem and its id is kept in
  provenance, with no harness change. A figure or audio stimulus makes the question
  ineligible (FR-023).
- Q: How does the system decide that two question records are the same question? → A: Exact
  match only: identical original-language stem and choices after normalizing Unicode,
  whitespace and punctuation. Near-matches are not linked (FR-010).
- Q: If an exam's hand-check finds a sampled question that does not match the source, what
  happens to that exam's data? → A: The exam is marked `unverified`, excluded from suite
  export and labelled in snapshots, until its extraction is fixed, it is re-extracted, and a
  new seeded 50-question sample passes (FR-024, SC-003).
- Q: Is a suite question bank built from a fixed snapshot or from the live store? → A: Only
  from a named snapshot. The bank records the snapshot version, and re-exporting from the
  same snapshot gives a byte-identical file (FR-023).
