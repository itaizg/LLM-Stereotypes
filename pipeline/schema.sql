-- Exam-dataset schema: the single authority for tables, constraints, views, and
-- the data dictionary (every COMMENT below is published in data_dictionary.md).
-- Design: specs/001-exam-data-aggregation/data-model.md.
--
-- Everything is idempotent (IF NOT EXISTS / OR REPLACE) so applying it twice
-- changes nothing. There are no migrations: the store is rebuildable from the
-- catalog, the ledger, and the blobs (FR-020), so a schema change is a rebuild.

-- ---------------------------------------------------------------------------
-- exams: the registry of the six exams in scope (FR-001)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exams (
    exam_id        text PRIMARY KEY
                   CHECK (exam_id IN ('sat', 'psychometric', 'csat', 'gaokao',
                                      'common_test', 'enem')),
    name           text NOT NULL,
    body           text NOT NULL,
    language       text NOT NULL,
    domains        text[] NOT NULL,
    publishes_keys boolean NOT NULL CHECK (publishes_keys)
);

-- ---------------------------------------------------------------------------
-- exam_editions: the exam catalog (FR-002, FR-003, FR-005)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exam_editions (
    edition_id       text PRIMARY KEY
                     CHECK (edition_id ~ '^[a-z][a-z_]*-20(1[5-9]|2[0-5])-[a-z0-9-]+$'),
    exam_id          text NOT NULL REFERENCES exams (exam_id),
    year             integer NOT NULL CHECK (year BETWEEN 2015 AND 2025),
    sitting          text NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('administered', 'practice')),
    status           text NOT NULL
                     CHECK (status IN ('ingested', 'not_released', 'blocked')),
    status_reason    text
                     CHECK (status_reason IN ('robots', 'non_allowlisted',
                                              'login_required')),
    release_date     date,
    official_url     text,
    lineage          text NOT NULL,
    language         text NOT NULL,
    english_item_map jsonb,
    evidence         jsonb NOT NULL
                     CHECK (jsonb_typeof(evidence) = 'array'
                            AND jsonb_array_length(evidence) > 0),
    CHECK (edition_id = exam_id || '-' || year || '-' || sitting),
    CHECK (status <> 'ingested'
           OR (release_date IS NOT NULL AND official_url IS NOT NULL)),
    CHECK (status <> 'blocked' OR status_reason IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- sources: candidate URLs and their review (FR-007, FR-008)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sources (
    source_id        text PRIMARY KEY,
    url              text NOT NULL UNIQUE,
    domain           text NOT NULL,
    discovery_method text NOT NULL CHECK (discovery_method IN ('index_crawl', 'manual')),
    found_on         text,
    edition_id       text REFERENCES exam_editions (edition_id),
    decision         text NOT NULL DEFAULT 'pending'
                     CHECK (decision IN ('pending', 'approved', 'rejected')),
    decision_reason  text,
    fetch_outcome    text NOT NULL DEFAULT 'not_attempted'
                     CHECK (fetch_outcome IN ('not_attempted', 'fetched',
                                              'refused_domain', 'refused_robots',
                                              'login_required', 'not_approved',
                                              'http_error')),
    CHECK (decision <> 'rejected' OR decision_reason IS NOT NULL),
    CHECK (decision <> 'approved' OR edition_id IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- artifacts: immutable raw files, by content hash (FR-009)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS artifacts (
    sha256     text NOT NULL,
    edition_id text NOT NULL REFERENCES exam_editions (edition_id),
    source_id  text NOT NULL REFERENCES sources (source_id),
    role       text NOT NULL
               CHECK (role IN ('paper', 'key', 'key_erratum', 'english', 'audio',
                               'statement', 'index_page')),
    media_type text NOT NULL,
    published  date,
    bytes      bigint NOT NULL CHECK (bytes >= 0),
    PRIMARY KEY (sha256, edition_id),
    CHECK (role <> 'key_erratum' OR published IS NOT NULL)
);

-- ---------------------------------------------------------------------------
-- stimuli: shared passages, figures, audio (stored once, FR-010)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stimuli (
    stimulus_id      text PRIMARY KEY,
    edition_id       text NOT NULL REFERENCES exam_editions (edition_id),
    modality         text NOT NULL CHECK (modality IN ('text', 'figure', 'table', 'audio')),
    original_text    text,
    artifact_sha256  text NOT NULL,
    page             integer NOT NULL,
    pipeline_version text NOT NULL,
    content_hash     text NOT NULL,
    retired          boolean NOT NULL DEFAULT false
);

-- ---------------------------------------------------------------------------
-- questions: original-language content only (FR-010, FR-011)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS questions (
    question_id           text PRIMARY KEY,
    edition_id            text NOT NULL REFERENCES exam_editions (edition_id),
    canonical_question_id text NOT NULL,
    section               text NOT NULL,
    item                  text NOT NULL,
    print_order           integer NOT NULL CHECK (print_order >= 1),
    stem                  text NOT NULL,
    choices               jsonb NOT NULL CHECK (jsonb_typeof(choices) = 'array'),
    has_figure            boolean NOT NULL,
    has_table             boolean NOT NULL,
    has_audio             boolean NOT NULL,
    is_free_response      boolean NOT NULL,
    stimulus_id           text REFERENCES stimuli (stimulus_id),
    source_url            text NOT NULL,
    artifact_sha256       text NOT NULL,
    page                  integer NOT NULL,
    pipeline_version      text NOT NULL,
    content_hash          text NOT NULL,
    retired               boolean NOT NULL DEFAULT false
);
CREATE INDEX IF NOT EXISTS questions_edition_idx ON questions (edition_id, print_order);
CREATE INDEX IF NOT EXISTS questions_canonical_idx ON questions (canonical_question_id);

-- ---------------------------------------------------------------------------
-- answer_keys: official keys only, append-only (FR-012, FR-016)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS answer_keys (
    key_id           bigserial PRIMARY KEY,
    question_id      text NOT NULL REFERENCES questions (question_id),
    labels           text[] NOT NULL CHECK (cardinality(labels) >= 1),
    artifact_sha256  text NOT NULL,
    page             integer NOT NULL,
    is_erratum       boolean NOT NULL,
    published        date,
    pipeline_version text NOT NULL,
    UNIQUE (question_id, labels, artifact_sha256)
);

-- ---------------------------------------------------------------------------
-- translations: English text that is not the original language (FR-014, FR-015)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS translations (
    translation_id     bigserial PRIMARY KEY,
    subject_kind       text NOT NULL CHECK (subject_kind IN ('question', 'stimulus')),
    subject_id         text NOT NULL,
    source_content_hash text NOT NULL,
    translation_source text NOT NULL CHECK (translation_source IN ('official', 'mt')),
    stem               text,
    choices            jsonb,
    artifact_sha256    text,
    page               integer,
    mt_model           text,
    mt_model_revision  text,
    mt_prompt_hash     text,
    mt_response_sha256 text,
    pipeline_version   text NOT NULL,
    CHECK (translation_source <> 'official'
           OR (artifact_sha256 IS NOT NULL AND page IS NOT NULL)),
    CHECK (translation_source <> 'mt'
           OR (mt_model IS NOT NULL AND mt_model_revision IS NOT NULL
               AND mt_prompt_hash IS NOT NULL AND mt_response_sha256 IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS translations_unique_idx ON translations (
    subject_kind, subject_id, source_content_hash, translation_source,
    (coalesce(artifact_sha256, mt_response_sha256))
);

-- ---------------------------------------------------------------------------
-- extraction_failures: nothing is dropped silently (FR-013)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS extraction_failures (
    edition_id       text NOT NULL REFERENCES exam_editions (edition_id),
    subject_id       text NOT NULL,
    stage            text NOT NULL CHECK (stage IN ('extract', 'key', 'translate')),
    reason_code      text NOT NULL
                     CHECK (reason_code IN (
                         'artifact_unreadable', 'unsupported_format',
                         'ocr_low_confidence', 'item_split_failed',
                         'choices_not_found', 'stimulus_unresolved',
                         'no_official_key', 'key_label_mismatch', 'key_conflict',
                         'translation_pairing_mismatch', 'mt_failed',
                         'mt_empty_output')),
    detail           text,
    artifact_sha256  text,
    page             integer,
    pipeline_version text NOT NULL,
    PRIMARY KEY (stage, subject_id, reason_code)
);

-- ---------------------------------------------------------------------------
-- exam_checks: hand-check events replayed from the ledger (FR-024)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS exam_checks (
    exam_id   text NOT NULL REFERENCES exams (exam_id),
    event_seq integer NOT NULL,
    seed      bigint NOT NULL,
    passed    boolean NOT NULL,
    n_items   integer NOT NULL,
    n_failed  integer NOT NULL,
    items     jsonb NOT NULL,
    PRIMARY KEY (exam_id, event_seq)
);

-- ---------------------------------------------------------------------------
-- snapshots: registry of published snapshots (FR-021)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS snapshots (
    version        text PRIMARY KEY CHECK (version ~ '^snap-[0-9]{4,}$'),
    git_sha        text NOT NULL,
    catalog_sha256 text NOT NULL,
    ledger_sha256  text NOT NULL,
    content_sha256 text NOT NULL,
    row_counts     jsonb NOT NULL,
    uri            text NOT NULL
);

-- ---------------------------------------------------------------------------
-- questions_flat: one row per live question with its current key and preferred
-- English (FR-017). Every export reads this view, never the tables.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW questions_flat AS
WITH ranked_keys AS (
    -- Rank by (is_erratum, published) so an erratum outranks an ordinary key and a
    -- dated key outranks an undated one; rank 1 may hold several equal-rank rows.
    SELECT k.*, rank() OVER (
        PARTITION BY k.question_id
        ORDER BY k.is_erratum DESC, k.published DESC NULLS LAST
    ) AS rnk
    FROM answer_keys k
),
conflicting_keys AS (
    -- Equal-rank official keys that disagree: no current key (never a silent pick).
    SELECT question_id FROM ranked_keys
    WHERE rnk = 1
    GROUP BY question_id
    HAVING count(DISTINCT labels) > 1
),
current_keys AS (
    SELECT DISTINCT ON (r.question_id)
           r.question_id, r.labels, r.artifact_sha256, r.page
    FROM ranked_keys r
    WHERE r.rnk = 1
      AND NOT EXISTS (SELECT 1 FROM conflicting_keys c WHERE c.question_id = r.question_id)
    ORDER BY r.question_id, r.artifact_sha256 DESC
),
preferred_translations AS (
    -- Official over MT, then the latest; never one made from superseded content.
    SELECT DISTINCT ON (t.subject_kind, t.subject_id) t.*
    FROM translations t
    LEFT JOIN questions q
           ON t.subject_kind = 'question' AND q.question_id = t.subject_id
    LEFT JOIN stimuli s
           ON t.subject_kind = 'stimulus' AND s.stimulus_id = t.subject_id
    WHERE t.source_content_hash = coalesce(q.content_hash, s.content_hash)
    ORDER BY t.subject_kind, t.subject_id,
             (t.translation_source = 'official') DESC, t.translation_id DESC
),
joined AS (
    SELECT
        q.question_id, q.edition_id, q.canonical_question_id, q.section, q.item,
        q.print_order, q.stem, q.choices, q.has_figure, q.has_table, q.has_audio,
        q.is_free_response, q.stimulus_id, q.source_url, q.artifact_sha256, q.page,
        q.pipeline_version, q.content_hash, q.retired,
        e.exam_id, e.kind AS edition_kind, e.lineage, e.language,
        ck.question_id AS has_current_key,
        ck.labels AS key_labels,
        ck.artifact_sha256 AS key_artifact_sha256,
        ck.page AS key_page,
        CASE WHEN e.language = 'en' THEN q.stem ELSE pt.stem END AS english_stem,
        CASE WHEN e.language = 'en' THEN q.choices ELSE pt.choices END AS english_choices,
        CASE WHEN e.language = 'en' THEN 'original' ELSE pt.translation_source
        END AS translation_source,
        pt.artifact_sha256 AS translation_artifact_sha256,
        pt.mt_model, pt.mt_model_revision, pt.mt_prompt_hash,
        st.modality AS stimulus_modality,
        CASE WHEN st.stimulus_id IS NULL THEN NULL
             WHEN e.language = 'en' THEN st.original_text
             ELSE spt.stem END AS stimulus_english,
        spt.translation_source AS stimulus_translation_source,
        spt.artifact_sha256 AS stimulus_translation_artifact_sha256,
        spt.mt_model AS stimulus_mt_model,
        spt.mt_model_revision AS stimulus_mt_model_revision,
        spt.mt_prompt_hash AS stimulus_mt_prompt_hash
    FROM questions q
    JOIN exam_editions e ON e.edition_id = q.edition_id
    LEFT JOIN current_keys ck ON ck.question_id = q.question_id
    LEFT JOIN preferred_translations pt
           ON pt.subject_kind = 'question' AND pt.subject_id = q.question_id
    LEFT JOIN stimuli st ON st.stimulus_id = q.stimulus_id
    LEFT JOIN preferred_translations spt
           ON spt.subject_kind = 'stimulus' AND spt.subject_id = st.stimulus_id
    WHERE NOT q.retired
)
SELECT
    j.question_id, j.edition_id, j.canonical_question_id, j.section, j.item,
    j.print_order, j.stem, j.choices, j.has_figure, j.has_table, j.has_audio,
    j.is_free_response, j.stimulus_id, j.source_url, j.artifact_sha256, j.page,
    j.pipeline_version, j.content_hash, j.retired,
    j.exam_id, j.edition_kind, j.lineage, j.language,
    j.key_labels, j.key_artifact_sha256, j.key_page,
    j.english_stem, j.english_choices, j.translation_source,
    j.translation_artifact_sha256, j.mt_model, j.mt_model_revision, j.mt_prompt_hash,
    j.stimulus_modality, j.stimulus_english, j.stimulus_translation_source,
    j.stimulus_translation_artifact_sha256, j.stimulus_mt_model,
    j.stimulus_mt_model_revision, j.stimulus_mt_prompt_hash,
    (j.ineligible_reason IS NULL) AS eligible,
    j.ineligible_reason
FROM (
    -- The first failing rule, in the order the spec lists them.
    SELECT joined.*,
        CASE
            WHEN joined.has_figure THEN 'figure'
            WHEN joined.has_table THEN 'table'
            WHEN joined.has_audio THEN 'audio'
            WHEN joined.is_free_response THEN 'free_response'
            WHEN jsonb_array_length(joined.choices) < 2 THEN 'fewer_than_two_choices'
            WHEN joined.has_current_key IS NULL THEN 'no_current_key'
            WHEN cardinality(joined.key_labels) <> 1 THEN 'multi_answer'
            WHEN NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(joined.choices) c
                WHERE c ->> 'label' = joined.key_labels[1]
            ) THEN 'key_label_mismatch'
            WHEN joined.english_stem IS NULL THEN 'no_english'
            WHEN joined.stimulus_id IS NOT NULL AND joined.stimulus_modality <> 'text'
                THEN 'non_text_stimulus'
            WHEN joined.stimulus_id IS NOT NULL AND joined.stimulus_english IS NULL
                THEN 'stimulus_no_english'
        END AS ineligible_reason
    FROM joined
) j;

-- ---------------------------------------------------------------------------
-- integrity_violations: invariants the constraints cannot express (SC-002,
-- SC-004). `snapshot` refuses to publish while any row exists.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW integrity_violations AS
SELECT 'edition_without_output'::text AS check_name, e.edition_id AS subject_id
FROM exam_editions e
WHERE e.status = 'ingested'
  AND NOT EXISTS (SELECT 1 FROM questions q
                  WHERE q.edition_id = e.edition_id AND NOT q.retired)
  AND NOT EXISTS (SELECT 1 FROM extraction_failures f
                  WHERE f.edition_id = e.edition_id AND f.stage = 'extract')
UNION ALL
SELECT 'key_not_official', k.question_id
FROM answer_keys k
JOIN questions q ON q.question_id = k.question_id
WHERE NOT EXISTS (SELECT 1 FROM artifacts a
                  WHERE a.sha256 = k.artifact_sha256
                    AND a.edition_id = q.edition_id
                    AND a.role IN ('key', 'key_erratum'))
UNION ALL
SELECT 'english_missing', q.question_id
FROM questions q
JOIN exam_editions e ON e.edition_id = q.edition_id
WHERE NOT q.retired AND e.language <> 'en'
  AND NOT EXISTS (SELECT 1 FROM translations t
                  WHERE t.subject_kind = 'question' AND t.subject_id = q.question_id
                    AND t.source_content_hash = q.content_hash)
  AND NOT EXISTS (SELECT 1 FROM extraction_failures f
                  WHERE f.stage = 'translate' AND f.subject_id = q.question_id)
UNION ALL
SELECT 'english_missing', s.stimulus_id
FROM stimuli s
JOIN exam_editions e ON e.edition_id = s.edition_id
WHERE NOT s.retired AND s.modality = 'text' AND e.language <> 'en'
  AND NOT EXISTS (SELECT 1 FROM translations t
                  WHERE t.subject_kind = 'stimulus' AND t.subject_id = s.stimulus_id
                    AND t.source_content_hash = s.content_hash)
  AND NOT EXISTS (SELECT 1 FROM extraction_failures f
                  WHERE f.stage = 'translate' AND f.subject_id = s.stimulus_id)
UNION ALL
SELECT 'question_without_artifact', q.question_id
FROM questions q
WHERE NOT q.retired
  AND NOT EXISTS (SELECT 1 FROM artifacts a
                  WHERE a.sha256 = q.artifact_sha256 AND a.edition_id = q.edition_id);

-- ---------------------------------------------------------------------------
-- Data dictionary (FR-022): one COMMENT per table, view, and column.
-- ---------------------------------------------------------------------------
COMMENT ON TABLE exams IS 'The six admissions exams in scope (FR-001), copied from catalog.yaml.';
COMMENT ON COLUMN exams.exam_id IS 'Stable exam identifier: sat, psychometric, csat, gaokao, common_test, enem.';
COMMENT ON COLUMN exams.name IS 'Official exam name.';
COMMENT ON COLUMN exams.body IS 'Official body that sets and publishes the exam.';
COMMENT ON COLUMN exams.language IS 'Original language of the exam, BCP 47.';
COMMENT ON COLUMN exams.domains IS 'Allowlisted official domains files may be fetched from (FR-008).';
COMMENT ON COLUMN exams.publishes_keys IS 'True when the official body publishes answer keys; required for scope (FR-006).';

COMMENT ON TABLE exam_editions IS 'The exam catalog: one row per edition (exam x year x sitting, form, or paper) with its status and evidence.';
COMMENT ON COLUMN exam_editions.edition_id IS 'Stable identifier {exam}-{year}-{sitting}.';
COMMENT ON COLUMN exam_editions.exam_id IS 'The exam this edition belongs to.';
COMMENT ON COLUMN exam_editions.year IS 'Exam-year cell, 2015-2025 (SAT practice tests use their release year).';
COMMENT ON COLUMN exam_editions.sitting IS 'Sitting, form, or paper name, e.g. national-i or practice-03.';
COMMENT ON COLUMN exam_editions.kind IS 'administered (a real sitting) or practice (an official practice test), FR-005.';
COMMENT ON COLUMN exam_editions.status IS 'ingested, not_released, or blocked; never unknown (FR-002).';
COMMENT ON COLUMN exam_editions.status_reason IS 'Why a blocked edition cannot be fetched: robots, non_allowlisted, or login_required.';
COMMENT ON COLUMN exam_editions.release_date IS 'Official release date; required when ingested.';
COMMENT ON COLUMN exam_editions.official_url IS 'Official URL of the release; required when ingested.';
COMMENT ON COLUMN exam_editions.lineage IS 'Format era, e.g. sat-paper-2016, sat-digital, center-test, common-test.';
COMMENT ON COLUMN exam_editions.language IS 'Original language of the paper, BCP 47.';
COMMENT ON COLUMN exam_editions.english_item_map IS 'Optional map from this edition''s (section:item) to the official English version''s numbering, when they differ.';
COMMENT ON COLUMN exam_editions.evidence IS 'Non-empty list of {type, source_id or artifact_sha256, note}: release_page, statement, or index_absence.';

COMMENT ON TABLE sources IS 'Candidate URLs found by index crawl or added by hand, with the maintainer''s review decision (FR-007).';
COMMENT ON COLUMN sources.source_id IS 'src_ plus the first 16 hex digits of the SHA-256 of the URL.';
COMMENT ON COLUMN sources.url IS 'The candidate URL.';
COMMENT ON COLUMN sources.domain IS 'Lower-cased host of the URL.';
COMMENT ON COLUMN sources.discovery_method IS 'index_crawl (found on an official index page) or manual (added by the maintainer).';
COMMENT ON COLUMN sources.found_on IS 'The seed page the link was found on, for index_crawl.';
COMMENT ON COLUMN sources.edition_id IS 'Edition of the latest approving review; required when approved.';
COMMENT ON COLUMN sources.decision IS 'pending, approved, or rejected; the latest review event decides.';
COMMENT ON COLUMN sources.decision_reason IS 'Why a candidate was rejected; required when rejected.';
COMMENT ON COLUMN sources.fetch_outcome IS 'Result of the latest fetch attempt, including refusals (FR-008).';

COMMENT ON TABLE artifacts IS 'Immutable raw files stored once under their content hash and linked to an edition (FR-009).';
COMMENT ON COLUMN artifacts.sha256 IS 'SHA-256 of the file bytes; the blob key raw/<sha256>.';
COMMENT ON COLUMN artifacts.edition_id IS 'Edition the file serves; one file may serve several editions.';
COMMENT ON COLUMN artifacts.source_id IS 'The approved source it was fetched from.';
COMMENT ON COLUMN artifacts.role IS 'paper, key, key_erratum, english (official English version of the paper), audio, statement, or index_page.';
COMMENT ON COLUMN artifacts.media_type IS 'Media type, e.g. application/pdf.';
COMMENT ON COLUMN artifacts.published IS 'Publication date; required for key_erratum, optional otherwise.';
COMMENT ON COLUMN artifacts.bytes IS 'File size in bytes.';

COMMENT ON TABLE stimuli IS 'Shared passages, figures, tables, or audio items several questions refer to; stored once.';
COMMENT ON COLUMN stimuli.stimulus_id IS 'Identifier {edition_id}:{section}:stim-{n}.';
COMMENT ON COLUMN stimuli.edition_id IS 'Edition the stimulus belongs to.';
COMMENT ON COLUMN stimuli.modality IS 'text, figure, table, or audio.';
COMMENT ON COLUMN stimuli.original_text IS 'Original-language text; null for figure and audio.';
COMMENT ON COLUMN stimuli.artifact_sha256 IS 'Raw file the stimulus was extracted from (provenance).';
COMMENT ON COLUMN stimuli.page IS 'Page in the raw file (provenance).';
COMMENT ON COLUMN stimuli.pipeline_version IS 'Git SHA of the pipeline that produced the row, with -dirty if uncommitted.';
COMMENT ON COLUMN stimuli.content_hash IS 'Hash of the stimulus content, excluding pipeline_version.';
COMMENT ON COLUMN stimuli.retired IS 'True when the latest extraction no longer produces this stimulus.';

COMMENT ON TABLE questions IS 'One row per extracted question: original-language content only, with full provenance (FR-010).';
COMMENT ON COLUMN questions.question_id IS 'Identifier {edition_id}:{section}:{item}.';
COMMENT ON COLUMN questions.edition_id IS 'Edition the question belongs to.';
COMMENT ON COLUMN questions.canonical_question_id IS 'Exact-match duplicate key (cq_ plus a hash of the normalized stem and choices); duplicates share it.';
COMMENT ON COLUMN questions.section IS 'Section of the paper.';
COMMENT ON COLUMN questions.item IS 'Printed item number (for the Common Test, the answer-box number).';
COMMENT ON COLUMN questions.print_order IS 'Position in the edition in page and reading order, from 1; exports sort by it.';
COMMENT ON COLUMN questions.stem IS 'Question stem in the original language.';
COMMENT ON COLUMN questions.choices IS 'Ordered list of {label, text} in the original labels; empty for free response.';
COMMENT ON COLUMN questions.has_figure IS 'True when the question uses a figure.';
COMMENT ON COLUMN questions.has_table IS 'True when the question uses a table.';
COMMENT ON COLUMN questions.has_audio IS 'True when the question uses audio.';
COMMENT ON COLUMN questions.is_free_response IS 'True when the question has no choices.';
COMMENT ON COLUMN questions.stimulus_id IS 'Shared stimulus the question refers to, if any.';
COMMENT ON COLUMN questions.source_url IS 'Official URL of the raw file (provenance).';
COMMENT ON COLUMN questions.artifact_sha256 IS 'Raw file the question was extracted from (provenance).';
COMMENT ON COLUMN questions.page IS 'Page in the raw file (provenance).';
COMMENT ON COLUMN questions.pipeline_version IS 'Git SHA of the pipeline that produced the row, with -dirty if uncommitted.';
COMMENT ON COLUMN questions.content_hash IS 'Hash of the content-defining fields, excluding pipeline_version; stable across rebuilds (FR-020).';
COMMENT ON COLUMN questions.retired IS 'True when the latest extraction no longer produces this question; kept for its history.';

COMMENT ON TABLE answer_keys IS 'Official answer keys, append-only; the current key is chosen by the questions_flat view (FR-012).';
COMMENT ON COLUMN answer_keys.key_id IS 'Surrogate key; never used for ordering.';
COMMENT ON COLUMN answer_keys.question_id IS 'The question the key answers.';
COMMENT ON COLUMN answer_keys.labels IS 'Sorted set of correct original choice labels; one or more.';
COMMENT ON COLUMN answer_keys.artifact_sha256 IS 'Official key file the key was read from (role key or key_erratum).';
COMMENT ON COLUMN answer_keys.page IS 'Page in the key file.';
COMMENT ON COLUMN answer_keys.is_erratum IS 'True when the key file is an official erratum; an erratum outranks an ordinary key.';
COMMENT ON COLUMN answer_keys.published IS 'Publication date of the key file; null when undated.';
COMMENT ON COLUMN answer_keys.pipeline_version IS 'Git SHA of the pipeline that produced the row, with -dirty if uncommitted.';

COMMENT ON TABLE translations IS 'English text that is not the original language, official or machine-made, append-only (FR-014).';
COMMENT ON COLUMN translations.translation_id IS 'Surrogate key; the greater id is the later translation.';
COMMENT ON COLUMN translations.subject_kind IS 'question or stimulus.';
COMMENT ON COLUMN translations.subject_id IS 'The question_id or stimulus_id translated.';
COMMENT ON COLUMN translations.source_content_hash IS 'content_hash of the subject this was made from; a translation of superseded content is never preferred.';
COMMENT ON COLUMN translations.translation_source IS 'official (published by the exam body) or mt (machine translation).';
COMMENT ON COLUMN translations.stem IS 'English stem (for a stimulus, its text).';
COMMENT ON COLUMN translations.choices IS 'English choices in the same order and labels as the original.';
COMMENT ON COLUMN translations.artifact_sha256 IS 'Official English file; required for official.';
COMMENT ON COLUMN translations.page IS 'Page in the official English file; required for official.';
COMMENT ON COLUMN translations.mt_model IS 'Model requested for the machine translation.';
COMMENT ON COLUMN translations.mt_model_revision IS 'Model ID the response reported.';
COMMENT ON COLUMN translations.mt_prompt_hash IS 'SHA-256 of the prompt template.';
COMMENT ON COLUMN translations.mt_response_sha256 IS 'Blob hash of the raw response, which makes rebuilds offline.';
COMMENT ON COLUMN translations.pipeline_version IS 'Git SHA of the pipeline that produced the row, with -dirty if uncommitted.';

COMMENT ON TABLE extraction_failures IS 'Questions, stimuli, or editions that could not be extracted, keyed, or translated, with a reason code (FR-013).';
COMMENT ON COLUMN extraction_failures.edition_id IS 'Edition the failure belongs to.';
COMMENT ON COLUMN extraction_failures.subject_id IS 'question_id, stimulus_id, or edition:<edition_id> for a whole-edition failure.';
COMMENT ON COLUMN extraction_failures.stage IS 'Stage that recorded the failure: extract, key, or translate.';
COMMENT ON COLUMN extraction_failures.reason_code IS 'Reason from the fixed list; mirrored by pipeline.extract.ReasonCode.';
COMMENT ON COLUMN extraction_failures.detail IS 'Positions and counts only; never question text.';
COMMENT ON COLUMN extraction_failures.artifact_sha256 IS 'Raw file involved, if any.';
COMMENT ON COLUMN extraction_failures.page IS 'Page involved, if any.';
COMMENT ON COLUMN extraction_failures.pipeline_version IS 'Git SHA of the pipeline that recorded the failure.';

COMMENT ON TABLE exam_checks IS 'Hand-check events replayed from the ledger; exam verification status is derived from them (FR-024).';
COMMENT ON COLUMN exam_checks.exam_id IS 'Exam that was checked.';
COMMENT ON COLUMN exam_checks.event_seq IS 'Ledger sequence number of the event.';
COMMENT ON COLUMN exam_checks.seed IS 'Seed that drew the sample.';
COMMENT ON COLUMN exam_checks.passed IS 'True when every sampled question matched the source.';
COMMENT ON COLUMN exam_checks.n_items IS 'Number of questions checked.';
COMMENT ON COLUMN exam_checks.n_failed IS 'Number of questions that failed.';
COMMENT ON COLUMN exam_checks.items IS 'Per sampled question: question_id, content_hash, key_labels, verdicts, forced.';

COMMENT ON TABLE snapshots IS 'Registry of published snapshots; the one table a snapshot does not copy.';
COMMENT ON COLUMN snapshots.version IS 'Snapshot version, snap-NNNN.';
COMMENT ON COLUMN snapshots.git_sha IS 'Git SHA of the pipeline that produced the snapshot.';
COMMENT ON COLUMN snapshots.catalog_sha256 IS 'SHA-256 of catalog.yaml at snapshot time.';
COMMENT ON COLUMN snapshots.ledger_sha256 IS 'SHA-256 of ledger.jsonl at snapshot time.';
COMMENT ON COLUMN snapshots.content_sha256 IS 'Hash of the snapshot''s content; an unchanged store publishes no new version (FR-019).';
COMMENT ON COLUMN snapshots.row_counts IS 'Row count per table, taken in the snapshot transaction (FR-021).';
COMMENT ON COLUMN snapshots.uri IS 'Where the snapshot files live in the blob store.';

COMMENT ON VIEW questions_flat IS 'Read-only flattened view: each live question with its one current key and preferred English (FR-017). All exports read it.';
COMMENT ON COLUMN questions_flat.question_id IS 'See questions.question_id.';
COMMENT ON COLUMN questions_flat.edition_id IS 'See questions.edition_id.';
COMMENT ON COLUMN questions_flat.canonical_question_id IS 'See questions.canonical_question_id.';
COMMENT ON COLUMN questions_flat.section IS 'See questions.section.';
COMMENT ON COLUMN questions_flat.item IS 'See questions.item.';
COMMENT ON COLUMN questions_flat.print_order IS 'See questions.print_order.';
COMMENT ON COLUMN questions_flat.stem IS 'Original-language stem (questions.stem).';
COMMENT ON COLUMN questions_flat.choices IS 'Original-language choices (questions.choices).';
COMMENT ON COLUMN questions_flat.has_figure IS 'See questions.has_figure.';
COMMENT ON COLUMN questions_flat.has_table IS 'See questions.has_table.';
COMMENT ON COLUMN questions_flat.has_audio IS 'See questions.has_audio.';
COMMENT ON COLUMN questions_flat.is_free_response IS 'See questions.is_free_response.';
COMMENT ON COLUMN questions_flat.stimulus_id IS 'See questions.stimulus_id.';
COMMENT ON COLUMN questions_flat.source_url IS 'See questions.source_url.';
COMMENT ON COLUMN questions_flat.artifact_sha256 IS 'See questions.artifact_sha256.';
COMMENT ON COLUMN questions_flat.page IS 'See questions.page.';
COMMENT ON COLUMN questions_flat.pipeline_version IS 'See questions.pipeline_version.';
COMMENT ON COLUMN questions_flat.content_hash IS 'See questions.content_hash.';
COMMENT ON COLUMN questions_flat.retired IS 'Always false in this view; retired questions are excluded.';
COMMENT ON COLUMN questions_flat.exam_id IS 'Exam of the question''s edition.';
COMMENT ON COLUMN questions_flat.edition_kind IS 'administered or practice (exam_editions.kind).';
COMMENT ON COLUMN questions_flat.lineage IS 'Format era of the edition.';
COMMENT ON COLUMN questions_flat.language IS 'Original language of the edition.';
COMMENT ON COLUMN questions_flat.key_labels IS 'Correct labels of the current key; null when none or when equal-rank official keys disagree.';
COMMENT ON COLUMN questions_flat.key_artifact_sha256 IS 'Official key file of the current key.';
COMMENT ON COLUMN questions_flat.key_page IS 'Page of the current key in that file.';
COMMENT ON COLUMN questions_flat.english_stem IS 'Preferred English stem; the original text for English-original editions.';
COMMENT ON COLUMN questions_flat.english_choices IS 'Preferred English choices, same order and labels as the original.';
COMMENT ON COLUMN questions_flat.translation_source IS 'original, official, or mt: where the English stem came from.';
COMMENT ON COLUMN questions_flat.translation_artifact_sha256 IS 'Official English file of the preferred translation, if official.';
COMMENT ON COLUMN questions_flat.mt_model IS 'Model of the preferred translation, if machine-made.';
COMMENT ON COLUMN questions_flat.mt_model_revision IS 'Reported model ID of the preferred translation, if machine-made.';
COMMENT ON COLUMN questions_flat.mt_prompt_hash IS 'Prompt hash of the preferred translation, if machine-made.';
COMMENT ON COLUMN questions_flat.stimulus_modality IS 'Modality of the shared stimulus, if any.';
COMMENT ON COLUMN questions_flat.stimulus_english IS 'Preferred English text of a text stimulus.';
COMMENT ON COLUMN questions_flat.stimulus_translation_source IS 'Source of the stimulus English: official or mt; the stem and its stimulus can differ.';
COMMENT ON COLUMN questions_flat.stimulus_translation_artifact_sha256 IS 'Official English file of the stimulus translation, if official.';
COMMENT ON COLUMN questions_flat.stimulus_mt_model IS 'Model of the stimulus translation, if machine-made.';
COMMENT ON COLUMN questions_flat.stimulus_mt_model_revision IS 'Reported model ID of the stimulus translation, if machine-made.';
COMMENT ON COLUMN questions_flat.stimulus_mt_prompt_hash IS 'Prompt hash of the stimulus translation, if machine-made.';
COMMENT ON COLUMN questions_flat.eligible IS 'True when the question may be exported to a suite bank (spec Assumptions).';
COMMENT ON COLUMN questions_flat.ineligible_reason IS 'First failing eligibility rule; null when eligible.';

COMMENT ON VIEW integrity_violations IS 'Invariants constraints cannot express; snapshot refuses to publish while this view has rows (SC-002, SC-004).';
COMMENT ON COLUMN integrity_violations.check_name IS 'edition_without_output, key_not_official, english_missing, or question_without_artifact.';
COMMENT ON COLUMN integrity_violations.subject_id IS 'The edition, question, or stimulus that breaks the invariant.';
