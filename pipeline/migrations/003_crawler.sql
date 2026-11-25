-- Brings a store made by the spec 001/002 schema to schema version 3 (spec 003).
--
-- store.apply_schema runs this file in one transaction, right after schema.sql,
-- which has already created every missing table (schema_version, host_requests and
-- the crawler_* tables). What schema.sql cannot do to an existing table is done here:
-- new columns and replaced CHECK constraints. Every statement is idempotent.

ALTER TABLE exams DROP CONSTRAINT IF EXISTS exams_exam_id_check;
ALTER TABLE exams ADD CONSTRAINT exams_exam_id_check
    CHECK (exam_id ~ '^[a-z][a-z0-9_]{1,40}$');

ALTER TABLE sources DROP CONSTRAINT IF EXISTS sources_discovery_method_check;
ALTER TABLE sources ADD CONSTRAINT sources_discovery_method_check
    CHECK (discovery_method IN ('index_crawl', 'manual', 'crawler'));

ALTER TABLE questions
    ADD COLUMN IF NOT EXISTS needs_reprocess boolean NOT NULL DEFAULT false;
ALTER TABLE answer_keys
    ADD COLUMN IF NOT EXISTS needs_reprocess boolean NOT NULL DEFAULT false;

INSERT INTO schema_version (id, version) VALUES (true, 3)
    ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version;
