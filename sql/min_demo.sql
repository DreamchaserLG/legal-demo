CREATE DATABASE legal_demo
    WITH
    OWNER = postgres
    ENCODING = 'UTF8'
    LC_COLLATE = 'en_US.utf8'
    LC_CTYPE = 'en_US.utf8'
    TEMPLATE = template0;

\c legal_demo;
-- ============================================
-- 2️⃣ 启用扩展
-- ============================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;

DROP TABLE IF EXISTS item_keywords CASCADE;
DROP TABLE IF EXISTS source_items CASCADE;
DROP TABLE IF EXISTS sync_logs CASCADE;

CREATE TABLE source_items (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) NOT NULL,         -- canlii / ofac
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

CREATE INDEX idx_source_items_source ON source_items(source_code);
CREATE INDEX idx_source_items_published ON source_items(published_at DESC);
CREATE INDEX idx_source_items_title_trgm ON source_items USING GIN (title gin_trgm_ops);
CREATE INDEX idx_source_items_summary_trgm ON source_items USING GIN (summary gin_trgm_ops);
CREATE INDEX idx_source_items_raw_text_trgm ON source_items USING GIN (raw_text gin_trgm_ops);
CREATE INDEX idx_source_items_title_lower_trgm ON source_items USING GIN (LOWER(title) gin_trgm_ops);
CREATE INDEX idx_source_items_summary_lower_trgm ON source_items USING GIN (LOWER(summary) gin_trgm_ops);
CREATE INDEX idx_source_items_raw_text_lower_trgm ON source_items USING GIN (LOWER(raw_text) gin_trgm_ops);

CREATE TABLE item_keywords (
    id BIGSERIAL PRIMARY KEY,
    item_id BIGINT NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    keyword VARCHAR(255) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (item_id, keyword)
);

CREATE INDEX idx_item_keywords_keyword ON item_keywords(keyword);
CREATE INDEX idx_item_keywords_keyword_lower ON item_keywords(LOWER(keyword));

CREATE TABLE sync_logs (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) NOT NULL,
    status VARCHAR(30) NOT NULL,
    message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
