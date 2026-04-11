-- ============================================
-- 1️⃣ 创建数据库
-- ============================================

CREATE DATABASE legal_ai
    WITH
    OWNER = postgres
    ENCODING = 'UTF8'
    LC_COLLATE = 'en_US.utf8'
    LC_CTYPE = 'en_US.utf8'
    TEMPLATE = template0;

\c legal_ai;

-- ============================================
-- 2️⃣ 启用扩展
-- ============================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- 3️⃣ 国家表
-- ============================================

CREATE TABLE countries (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name VARCHAR(100) NOT NULL UNIQUE,
    legal_system VARCHAR(100),  -- common law / civil law
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 4️⃣ 法律主表
-- ============================================

CREATE TABLE laws (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_id UUID REFERENCES countries(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    enactment_date DATE,
    status VARCHAR(50),  -- active / repealed
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_laws_country ON laws(country_id);

-- ============================================
-- 5️⃣ 法条表
-- ============================================

CREATE TABLE law_articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    law_id UUID REFERENCES laws(id) ON DELETE CASCADE,
    article_number VARCHAR(50),
    chapter VARCHAR(100),
    content TEXT NOT NULL,
    keywords TEXT[],
    embedding VECTOR(1536),   -- 预留向量字段
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_law_articles_law ON law_articles(law_id);
CREATE INDEX idx_law_articles_keywords ON law_articles USING GIN(keywords);
CREATE INDEX idx_law_articles_vector ON law_articles USING ivfflat (embedding vector_cosine_ops);

-- ============================================
-- 6️⃣ 案例主表
-- ============================================

CREATE TABLE cases (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_id UUID REFERENCES countries(id) ON DELETE CASCADE,
    court_name VARCHAR(255),
    case_number VARCHAR(100),
    case_year INT,
    legal_field VARCHAR(100),  -- inheritance / contract / tort
    facts TEXT,
    issues TEXT,
    reasoning TEXT,
    judgment TEXT,
    outcome VARCHAR(100),  -- plaintiff_win / defendant_win
    embedding VECTOR(1536),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_cases_country ON cases(country_id);
CREATE INDEX idx_cases_year ON cases(case_year);
CREATE INDEX idx_cases_field ON cases(legal_field);
CREATE INDEX idx_cases_vector ON cases USING ivfflat (embedding vector_cosine_ops);

-- ============================================
-- 7️⃣ 案例-法条关联表
-- ============================================

CREATE TABLE case_articles (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    case_id UUID REFERENCES cases(id) ON DELETE CASCADE,
    article_id UUID REFERENCES law_articles(id) ON DELETE CASCADE,
    relevance_score NUMERIC(4,3)  -- 关联强度
);

CREATE INDEX idx_case_articles_case ON case_articles(case_id);
CREATE INDEX idx_case_articles_article ON case_articles(article_id);

-- ============================================
-- 8️⃣ 专家规则表
-- ============================================

CREATE TABLE expert_rules (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    country_id UUID REFERENCES countries(id),
    legal_field VARCHAR(100),
    rule_name VARCHAR(255),
    rule_condition TEXT,
    risk_level VARCHAR(50),
    suggestion TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 9️⃣ 用户查询日志
-- ============================================

CREATE TABLE search_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    input_text TEXT,
    extracted_keywords TEXT[],
    predicted_outcome VARCHAR(100),
    confidence NUMERIC(5,2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);