-- Demo schema for a legal retrieval and prediction assistant.
-- Run inside the target database, for example:
--   psql -U postgres -d legal_demo -f sql/legal_agent_demo.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS source_items (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) NOT NULL,
    source_uid VARCHAR(255) NOT NULL,
    title TEXT NOT NULL,
    item_url TEXT,
    published_at TIMESTAMP NULL,
    summary TEXT,
    raw_text TEXT,
    raw_json JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_code, source_uid)
);

CREATE INDEX IF NOT EXISTS idx_source_items_source ON source_items(source_code);
CREATE INDEX IF NOT EXISTS idx_source_items_published ON source_items(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_items_updated ON source_items(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_items_source_updated ON source_items(source_code, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_source_items_title_lower_trgm
    ON source_items USING GIN (LOWER(title) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_source_items_summary_lower_trgm
    ON source_items USING GIN (LOWER(summary) gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_source_items_raw_text_lower_trgm
    ON source_items USING GIN (LOWER(raw_text) gin_trgm_ops);

CREATE TABLE IF NOT EXISTS item_keywords (
    id BIGSERIAL PRIMARY KEY,
    item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    keyword VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (item_id, keyword)
);

CREATE INDEX IF NOT EXISTS idx_item_keywords_keyword ON item_keywords(keyword);
CREATE INDEX IF NOT EXISTS idx_item_keywords_keyword_lower ON item_keywords(LOWER(keyword));

CREATE TABLE IF NOT EXISTS sync_logs (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) NOT NULL,
    status VARCHAR(30) NOT NULL,
    message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_sync_logs_source_created ON sync_logs(source_code, created_at DESC);

CREATE TABLE IF NOT EXISTS ingestion_tasks (
    id BIGSERIAL PRIMARY KEY,
    fingerprint VARCHAR(128) NOT NULL,
    query_text TEXT NOT NULL,
    normalized_keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    source_filter VARCHAR(50) NOT NULL DEFAULT 'all',
    origin_page VARCHAR(30) NOT NULL DEFAULT 'search',
    desired_count INTEGER NOT NULL DEFAULT 0,
    current_local_count INTEGER NOT NULL DEFAULT 0,
    result_total_after INTEGER NOT NULL DEFAULT 0,
    status VARCHAR(30) NOT NULL DEFAULT 'queued',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    items_processed INTEGER NOT NULL DEFAULT 0,
    claimed_by VARCHAR(120),
    sources_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    message TEXT,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP NULL,
    finished_at TIMESTAMP NULL,
    last_heartbeat TIMESTAMP NULL
);

CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_status_created
    ON ingestion_tasks(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_fingerprint
    ON ingestion_tasks(fingerprint);
CREATE INDEX IF NOT EXISTS idx_ingestion_tasks_claimed_by
    ON ingestion_tasks(claimed_by);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ingestion_tasks_active_fingerprint
    ON ingestion_tasks(fingerprint)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS agent_runs (
    id BIGSERIAL PRIMARY KEY,
    input_text TEXT NOT NULL,
    module_code VARCHAR(30) NOT NULL DEFAULT 'canada',
    source_filter VARCHAR(50) NOT NULL DEFAULT 'all',
    sort_mode VARCHAR(30) NOT NULL DEFAULT 'relevance',
    extracted_keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    structured_analysis JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_created_at ON agent_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_keywords ON agent_runs USING GIN(extracted_keywords);
CREATE INDEX IF NOT EXISTS idx_agent_runs_module_created_at ON agent_runs(module_code, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_predictions (
    id BIGSERIAL PRIMARY KEY,
    agent_run_id BIGINT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    module_code VARCHAR(30) NOT NULL DEFAULT 'canada',
    model_provider VARCHAR(50) NOT NULL DEFAULT 'openai',
    model_name VARCHAR(100) NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'preview',
    predicted_outcome TEXT NOT NULL,
    likely_prevailing_party VARCHAR(100),
    confidence NUMERIC(5,2) NOT NULL DEFAULT 0,
    reasoning TEXT NOT NULL,
    key_factors JSONB NOT NULL DEFAULT '[]'::jsonb,
    supporting_items JSONB NOT NULL DEFAULT '[]'::jsonb,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_predictions_run_id ON agent_predictions(agent_run_id);
CREATE INDEX IF NOT EXISTS idx_agent_predictions_status ON agent_predictions(status);
CREATE INDEX IF NOT EXISTS idx_agent_predictions_module_created_at ON agent_predictions(module_code, created_at DESC);

CREATE TABLE IF NOT EXISTS agent_chat_logs (
    id BIGSERIAL PRIMARY KEY,
    module_code VARCHAR(30) NOT NULL DEFAULT 'canada',
    input_text TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    raw_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_chat_logs_module_created_at
    ON agent_chat_logs(module_code, created_at DESC);
