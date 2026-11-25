-- Brings a schema version 3 store to version 4 (spec 004, crawler orchestration).
--
-- store.apply_schema runs this file in one transaction, after schema.sql (which has
-- already created crawler_budget_approvals) and, for older stores, after
-- 003_crawler.sql. It adds what schema.sql cannot add to an existing table. Every
-- statement is idempotent.

ALTER TABLE crawler_runs
    ADD COLUMN IF NOT EXISTS approval_id text
    REFERENCES crawler_budget_approvals (approval_id);

INSERT INTO schema_version (id, version) VALUES (true, 4)
    ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version;
