-- ============================================
-- 0. 如果你还没创建数据库，先手动创建
-- ============================================
-- CREATE DATABASE legal_ai;
-- \c legal_ai;

-- ============================================
-- 1. 扩展
-- ============================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================
-- 2. 删除旧表（按依赖顺序逆序删除）
-- ============================================
DROP TABLE IF EXISTS search_logs CASCADE;
DROP TABLE IF EXISTS expert_rules CASCADE;
DROP TABLE IF EXISTS event_law_links CASCADE;
DROP TABLE IF EXISTS law_articles CASCADE;
DROP TABLE IF EXISTS laws CASCADE;
DROP TABLE IF EXISTS event_keywords CASCADE;
DROP TABLE IF EXISTS legal_events CASCADE;
DROP TABLE IF EXISTS raw_documents CASCADE;
DROP TABLE IF EXISTS sync_jobs CASCADE;
DROP TABLE IF EXISTS countries CASCADE;
DROP TABLE IF EXISTS legal_sources CASCADE;

-- ============================================
-- 3. 数据源表
-- ============================================
CREATE TABLE legal_sources (
    code VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    base_url TEXT,
    description TEXT,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO legal_sources (code, name, base_url, description, enabled)
VALUES
('ofac', 'OFAC', 'https://ofac.treasury.gov', '美国OFAC公开制裁数据源', TRUE),
('canlii', 'CanLII', 'https://www.canlii.org', '加拿大公开法律案例数据源', TRUE)
ON CONFLICT (code) DO NOTHING;

-- ============================================
-- 4. 国家表
-- ============================================
CREATE TABLE countries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    iso_code VARCHAR(10) UNIQUE NOT NULL,
    name VARCHAR(100) UNIQUE NOT NULL,
    legal_system VARCHAR(100),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO countries (iso_code, name, legal_system)
VALUES
('US', 'United States', 'common law'),
('CA', 'Canada', 'common law')
ON CONFLICT (iso_code) DO NOTHING;

-- ============================================
-- 5. 同步任务表
-- ============================================
CREATE TABLE sync_jobs (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) REFERENCES legal_sources(code) ON DELETE SET NULL,
    job_type VARCHAR(50) NOT NULL,              -- full_sync / incremental_sync / keyword_sync
    status VARCHAR(30) NOT NULL DEFAULT 'running',  -- running / success / failed / skipped
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP,
    message TEXT,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX idx_sync_jobs_source ON sync_jobs(source_code);
CREATE INDEX idx_sync_jobs_status ON sync_jobs(status);
CREATE INDEX idx_sync_jobs_started_at ON sync_jobs(started_at DESC);

-- ============================================
-- 6. 原始文档表（抓取层）
-- ============================================
CREATE TABLE raw_documents (
    id BIGSERIAL PRIMARY KEY,
    source_code VARCHAR(50) NOT NULL REFERENCES legal_sources(code) ON DELETE CASCADE,
    source_uid VARCHAR(255) NOT NULL,
    document_type VARCHAR(50) NOT NULL,         -- case_meta / case_detail / sanction / rss
    keyword VARCHAR(255),
    url TEXT,
    title TEXT,
    published_at TIMESTAMP,
    modified_at TIMESTAMP,
    content_hash VARCHAR(64),
    raw_html TEXT,
    raw_text TEXT,
    raw_json JSONB,
    status VARCHAR(30) NOT NULL DEFAULT 'new',  -- new / updated / unchanged / failed
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source_code, source_uid, document_type)
);

CREATE INDEX idx_raw_documents_source ON raw_documents(source_code);
CREATE INDEX idx_raw_documents_uid ON raw_documents(source_uid);
CREATE INDEX idx_raw_documents_keyword ON raw_documents(keyword);
CREATE INDEX idx_raw_documents_published_at ON raw_documents(published_at DESC);

-- ============================================
-- 7. 规范化事件表（核心主表）
-- ============================================
CREATE TABLE legal_events (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    source_code VARCHAR(50) NOT NULL REFERENCES legal_sources(code) ON DELETE RESTRICT,
    source_uid VARCHAR(255) NOT NULL,
    country_id UUID REFERENCES countries(id) ON DELETE SET NULL,
    raw_document_id BIGINT REFERENCES raw_documents(id) ON DELETE SET NULL,

    external_ref VARCHAR(255),      -- citation / case number / sanction uid
    title TEXT NOT NULL,
    event_type VARCHAR(50) NOT NULL DEFAULT 'case',  -- case / sanction / article / commentary
    jurisdiction VARCHAR(120),
    court_name VARCHAR(255),
    case_number VARCHAR(255),

    decision_date DATE,
    published_at TIMESTAMP,
    modified_at TIMESTAMP,

    legal_field VARCHAR(100),
    facts TEXT,
    issues TEXT,
    reasoning TEXT,
    judgment TEXT,
    outcome VARCHAR(100),
    summary TEXT,
    url TEXT,

    keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    embedding VECTOR(1536),         -- 如果你后面换 embedding 模型维度，这里同步改
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (source_code, source_uid)
);

CREATE INDEX idx_legal_events_source ON legal_events(source_code);
CREATE INDEX idx_legal_events_country ON legal_events(country_id);
CREATE INDEX idx_legal_events_event_type ON legal_events(event_type);
CREATE INDEX idx_legal_events_decision_date ON legal_events(decision_date DESC);
CREATE INDEX idx_legal_events_published_at ON legal_events(published_at DESC);
CREATE INDEX idx_legal_events_legal_field ON legal_events(legal_field);
CREATE INDEX idx_legal_events_keywords_gin ON legal_events USING GIN(keywords);
CREATE INDEX idx_legal_events_metadata_gin ON legal_events USING GIN(metadata);
CREATE INDEX idx_legal_events_title_trgm ON legal_events USING GIN (title gin_trgm_ops);

-- 向量索引：数据量足够大后再建，初期可以先保留字段不建索引
CREATE INDEX idx_legal_events_vector
ON legal_events
USING ivfflat (embedding vector_cosine_ops);

-- ============================================
-- 8. 关键词关联表
-- ============================================
CREATE TABLE event_keywords (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES legal_events(id) ON DELETE CASCADE,
    keyword VARCHAR(255) NOT NULL,
    source VARCHAR(50) NOT NULL DEFAULT 'user_input',   -- user_input / extracted / manual
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, keyword, source)
);

CREATE INDEX idx_event_keywords_event ON event_keywords(event_id);
CREATE INDEX idx_event_keywords_keyword ON event_keywords(keyword);

-- ============================================
-- 9. 法律主表
-- ============================================
CREATE TABLE laws (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_id UUID REFERENCES countries(id) ON DELETE CASCADE,
    source_code VARCHAR(50),
    source_uid VARCHAR(255),
    name VARCHAR(255) NOT NULL,
    enactment_date DATE,
    status VARCHAR(50),   -- active / repealed / amended
    summary TEXT,
    url TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_laws_country ON laws(country_id);
CREATE INDEX idx_laws_name_trgm ON laws USING GIN (name gin_trgm_ops);

-- ============================================
-- 10. 法条表
-- ============================================
CREATE TABLE law_articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    law_id UUID NOT NULL REFERENCES laws(id) ON DELETE CASCADE,
    article_number VARCHAR(50),
    chapter VARCHAR(100),
    title VARCHAR(255),
    content TEXT NOT NULL,
    keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    embedding VECTOR(1536),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_law_articles_law ON law_articles(law_id);
CREATE INDEX idx_law_articles_keywords ON law_articles USING GIN(keywords);
CREATE INDEX idx_law_articles_content_trgm ON law_articles USING GIN (content gin_trgm_ops);
CREATE INDEX idx_law_articles_vector
ON law_articles
USING ivfflat (embedding vector_cosine_ops);

-- ============================================
-- 11. 事件-法条关联表
-- ============================================
CREATE TABLE event_law_links (
    id BIGSERIAL PRIMARY KEY,
    event_id UUID NOT NULL REFERENCES legal_events(id) ON DELETE CASCADE,
    article_id UUID NOT NULL REFERENCES law_articles(id) ON DELETE CASCADE,
    relevance_score NUMERIC(6,4) DEFAULT 0,
    link_reason TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (event_id, article_id)
);

CREATE INDEX idx_event_law_links_event ON event_law_links(event_id);
CREATE INDEX idx_event_law_links_article ON event_law_links(article_id);

-- ============================================
-- 12. 专家规则表
-- ============================================
CREATE TABLE expert_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_id UUID REFERENCES countries(id) ON DELETE SET NULL,
    legal_field VARCHAR(100),
    rule_name VARCHAR(255) NOT NULL,
    rule_condition TEXT,
    risk_level VARCHAR(50),
    suggestion TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_expert_rules_country ON expert_rules(country_id);
CREATE INDEX idx_expert_rules_field ON expert_rules(legal_field);

-- ============================================
-- 13. 用户查询日志
-- ============================================
CREATE TABLE search_logs (
    id BIGSERIAL PRIMARY KEY,
    input_text TEXT NOT NULL,
    extracted_keywords TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    result_count INTEGER NOT NULL DEFAULT 0,
    predicted_outcome VARCHAR(100),
    confidence NUMERIC(5,2),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_search_logs_created_at ON search_logs(created_at DESC);