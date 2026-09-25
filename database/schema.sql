-- ============================================
-- AIS船舶违规检测系统 - PostgreSQL + pgvector
-- 数据库Schema完整建表语句
-- ============================================
-- 使用方式:
--   psql -h <host> -U <user> -d <db> -f schema.sql
-- 或在Python中:
--   from triage_engine.pgvector_client import PGVectorClient
--   client = PGVectorClient()
--   client.init_schema()
-- ============================================
-- 注意：修数据库连接请设置环境变量：
--   PG_HOST, PG_PORT, PG_DB, PG_USER, PG_PASSWORD
-- 或改 triage_engine/pgvector_client.py 第55-74行
-- ============================================

-- 启用 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;

-- ============================================
-- 1. 船舶主表
-- ============================================
CREATE TABLE IF NOT EXISTS ships (
    mmsi VARCHAR(20) PRIMARY KEY,
    ship_name VARCHAR(100),
    ship_type VARCHAR(50),
    length DECIMAL(10,2),
    width DECIMAL(10,2),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================
-- 2. 原始AIS数据表
-- ============================================
CREATE TABLE IF NOT EXISTS raw_ais (
    id BIGSERIAL PRIMARY KEY,
    mmsi VARCHAR(20) NOT NULL REFERENCES ships(mmsi),
    timestamp TIMESTAMP NOT NULL,
    lon DECIMAL(10,6) NOT NULL,
    lat DECIMAL(10,6) NOT NULL,
    speed DECIMAL(6,2) DEFAULT 0,
    course DECIMAL(6,2) DEFAULT 0,
    heading DECIMAL(6,2) DEFAULT 0,
    source_file VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_raw_ais_mmsi ON raw_ais(mmsi);
CREATE INDEX IF NOT EXISTS idx_raw_ais_time ON raw_ais(timestamp);

-- ============================================
-- 3. 案例主表（三级架构核心表）
-- ============================================
CREATE TABLE IF NOT EXISTS cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mmsi VARCHAR(20) NOT NULL REFERENCES ships(mmsi),
    source_file VARCHAR(255),
    segment_id VARCHAR(100),
    event_id VARCHAR(100),

    -- L1 加速器输出
    feature_vector VECTOR(64),
    feature_json JSONB,
    annotations JSONB,
    annotation_counts JSONB,

    -- L2 ML路由结果
    ml_prediction VARCHAR(50),
    ml_confidence DECIMAL(5,4),

    -- L3 LLM判定结果
    llm_result JSONB,
    llm_agreement BOOLEAN,
    llm_feedback JSONB,
    has_violation BOOLEAN,
    violation_type VARCHAR(50),

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cases_mmsi ON cases(mmsi);
CREATE INDEX IF NOT EXISTS idx_cases_ml_pred ON cases(ml_prediction);
CREATE INDEX IF NOT EXISTS idx_cases_violation ON cases(has_violation);
CREATE INDEX IF NOT EXISTS idx_cases_vector ON cases
    USING ivfflat (feature_vector vector_cosine_ops) WITH (lists = 100);

-- ============================================
-- 4. 轨迹序列表
-- ============================================
CREATE TABLE IF NOT EXISTS tracks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    mmsi VARCHAR(20) NOT NULL,
    track_json JSONB NOT NULL,
    segment_id VARCHAR(100),
    metadata JSONB,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tracks_case ON tracks(case_id);
CREATE INDEX IF NOT EXISTS idx_tracks_mmsi ON tracks(mmsi);

-- ============================================
-- 5. 训练集表（反馈回流）
-- ============================================
CREATE TABLE IF NOT EXISTS training_set (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID REFERENCES cases(id) ON DELETE SET NULL,
    feature_vector VECTOR(64),
    label VARCHAR(50) NOT NULL,
    label_source VARCHAR(20) DEFAULT 'llm',
    label_confidence DECIMAL(5,4) DEFAULT 1.0,
    sample_weight DECIMAL(6,2) DEFAULT 1.0,
    used_in_training BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_training_unused ON training_set(used_in_training)
    WHERE used_in_training = FALSE;

-- ============================================
-- 6. ML反馈记录表
-- ============================================
CREATE TABLE IF NOT EXISTS ml_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID REFERENCES cases(id) ON DELETE SET NULL,
    ml_prediction VARCHAR(50),
    ml_confidence DECIMAL(5,4),
    llm_result_text TEXT,
    llm_confidence DECIMAL(5,4),
    agreement BOOLEAN,
    error_type VARCHAR(20),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ml_feedback_case ON ml_feedback(case_id);
CREATE INDEX IF NOT EXISTS idx_ml_feedback_agree ON ml_feedback(agreement);

-- ============================================
-- 7. 违规记录表
-- ============================================
CREATE TABLE IF NOT EXISTS violations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID REFERENCES cases(id) ON DELETE SET NULL,
    mmsi VARCHAR(20) NOT NULL REFERENCES ships(mmsi),
    violation_type VARCHAR(50) NOT NULL,
    risk_level VARCHAR(10) DEFAULT 'low',
    violation_desc TEXT,
    evidence JSONB,
    ai_suggestion TEXT,
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_violations_mmsi ON violations(mmsi);
CREATE INDEX IF NOT EXISTS idx_violations_status ON violations(status);
CREATE INDEX IF NOT EXISTS idx_violations_type ON violations(violation_type);

-- ============================================
-- 8. 处分文书表
-- ============================================
CREATE TABLE IF NOT EXISTS disposal_docs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    violation_id UUID NOT NULL REFERENCES violations(id) ON DELETE CASCADE,
    doc_no VARCHAR(50) NOT NULL UNIQUE,
    doc_title VARCHAR(200),
    doc_content TEXT,
    operator_name VARCHAR(50),
    operator_id VARCHAR(50),
    status VARCHAR(20) DEFAULT '草稿',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_doc_violation ON disposal_docs(violation_id);
CREATE INDEX IF NOT EXISTS idx_doc_status ON disposal_docs(status);

-- ============================================
-- 9. 法规库
-- ============================================
CREATE TABLE IF NOT EXISTS regulations (
    id SERIAL PRIMARY KEY,
    violation_type VARCHAR(50) NOT NULL UNIQUE,
    law_name VARCHAR(200) NOT NULL,
    article VARCHAR(100),
    penalty TEXT,
    description TEXT,
    auto_alert_threshold DECIMAL(5,4) DEFAULT 0.85
);

-- ============================================
-- 10. 视图：待人工复核
-- ============================================
CREATE OR REPLACE VIEW v_pending_review AS
SELECT
    c.id AS case_id,
    c.mmsi,
    c.ml_prediction,
    c.ml_confidence,
    c.llm_result,
    c.has_violation,
    c.violation_type,
    f.error_type,
    f.created_at
FROM cases c
JOIN ml_feedback f ON f.case_id = c.id
WHERE f.agreement = FALSE
ORDER BY f.created_at DESC;