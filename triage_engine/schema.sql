-- ==========================================================================
-- AIS Accelarator — 三层级联系统数据库 Schema
-- 数据库：PostgreSQL 16+ (with pgvector 0.7+)
-- ==========================================================================

-- 启用 pgvector 扩展
CREATE EXTENSION IF NOT EXISTS vector;  -- 启用 pgvector 向量数据类型扩展
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- 启用 UUID 生成扩展

-- ── 1. 案例库（核心表） ──────────────────────────────────────────────
-- 每条记录对应一次加速器 + ML + LLM 的完整处理
CREATE TABLE cases (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 案例唯一标识（UUID 主键）
    mmsi            TEXT NOT NULL,  -- 船舶 MMSI 号（水上移动通信业务标识码）
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 记录创建时间（带时区时间戳）

    -- 数据来源
    source_file     TEXT,  -- 来源文件名
    segment_id      TEXT,  -- 轨迹分段标识
    event_id        TEXT,  -- 事件标识

    -- 加速器输出（特征向量 + 结构化标记）
    feature_vector  VECTOR(28),  -- 28 维特征向量（与加速器 FEATURE_NAMES 一致，pgvector 余弦检索）
    feature_json    JSONB,  -- 特征明细 JSON（特征名→值）
    annotations     JSONB,  -- 加速器标记完整列表（JSON 格式）
    annotation_counts JSONB,  -- 标记统计 JSON（{critical: N, warning: N, ...}）

    -- ML 预测结果
    ml_prediction   TEXT,  -- ML 路由预测结果：obvious_issue / has_risk / needs_review / normal
    ml_confidence   DOUBLE PRECISION,  -- ML 模型预测置信度（0~1 之间的浮点数）

    -- LLM 判定结果
    llm_result      JSONB,  -- LLM 完整输出结果（JSON 格式）
    llm_agreement   BOOLEAN,  -- LLM 是否同意 ML 路由决策
    llm_feedback    JSONB,  -- LLM 对数据标记的反馈（JSON 格式）

    -- 业务标注
    has_violation   BOOLEAN,  -- 最终是否判定为违规
    violation_type  TEXT,  -- 违规类型（如超速、AIS 关闭等）
    ground_truth    BOOLEAN,  -- 人工复核后的真实标注（ground truth）

    -- 索引
    routing_decision TEXT GENERATED ALWAYS AS (  -- 路由决策字段（由 ml_prediction 自动生成）
        CASE
            WHEN ml_prediction IS NOT NULL THEN ml_prediction  -- 有预测结果则使用预测值
            ELSE 'not_routed'  -- 否则标记为"未路由"
        END
    ) STORED
);

-- 索引定义
CREATE INDEX idx_cases_mmsi ON cases (mmsi);  -- 按 MMSI 号快速查找案例
CREATE INDEX idx_cases_created_at ON cases (created_at DESC);  -- 按创建时间降序排序
CREATE INDEX idx_cases_has_violation ON cases (has_violation) WHERE has_violation IS NOT NULL;  -- 筛选有违规标记的案例
CREATE INDEX idx_cases_routing ON cases (routing_decision);  -- 按路由决策类型快速查找
CREATE INDEX idx_cases_ground_truth ON cases (ground_truth) WHERE ground_truth IS TRUE;  -- 筛选已确认真实标注的案例

-- pgvector 索引（IVFFlat，默认 100 个列表）
CREATE INDEX idx_cases_feature ON cases  -- 特征向量的 pgvector 余弦相似度索引
    USING ivfflat (feature_vector vector_cosine_ops)  -- 使用 IVF-Flat 算法 + 余弦距离
    WITH (lists = 100);  -- IVF 聚类列表数量设为 100


-- ── 2. 训练集 ────────────────────────────────────────────────────────
-- 从 cases 中筛选出有可靠标注的记录用于模型训练
CREATE TABLE training_set (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 训练记录唯一标识（UUID 主键）
    case_id         UUID REFERENCES cases(id) ON DELETE CASCADE,  -- 关联案例 ID（级联删除）

    feature_vector  VECTOR(28) NOT NULL,  -- 28 维特征向量（非空）
    label           TEXT NOT NULL,  -- 目标变量标签：obvious_issue / has_risk / needs_review / normal 之一
    label_source    TEXT NOT NULL DEFAULT 'llm',  -- 标签来源：LLM（大模型）/ human_review（人工复核）/ heuristic（启发式规则）
    label_confidence DOUBLE PRECISION DEFAULT 1.0,  -- 标签置信度（默认 1.0 即完全可信）

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 记录创建时间
    used_in_training BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否已用于模型训练
    sample_weight   DOUBLE PRECISION NOT NULL DEFAULT 1.0,  -- 样本权重（默认 1.0，不一致样本权重更高）

    -- 自动权重：不一致样本权重更高
    CONSTRAINT valid_label CHECK (label IN ('obvious_issue', 'has_risk', 'needs_review', 'normal'))  -- 标签取值的合法性约束
);

CREATE INDEX idx_training_set_label ON training_set (label);  -- 按标签类型快速查找
CREATE INDEX idx_training_set_used ON training_set (used_in_training) WHERE used_in_training = FALSE;  -- 筛选未用于训练的数据
CREATE INDEX idx_training_set_case ON training_set (case_id);  -- 按关联案例 ID 快速查找


-- ── 3. ML vs LLM 反馈表（用于偏差分析） ────────────────────────────
CREATE TABLE ml_feedback (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 反馈记录唯一标识（UUID 主键）
    case_id         UUID REFERENCES cases(id) ON DELETE CASCADE,  -- 关联案例 ID（级联删除）

    ml_prediction   TEXT NOT NULL,  -- ML 路由预测结果
    ml_confidence   DOUBLE PRECISION NOT NULL,  -- ML 预测置信度
    llm_result_text TEXT,  -- LLM 判定结果的简要摘要文本
    llm_confidence  DOUBLE PRECISION,  -- LLM 判定的置信度

    agreement       BOOLEAN NOT NULL,  -- LLM 与 ML 的判定是否一致
    error_type      TEXT,  -- 错误类型：FP（误报）/ FN（漏报）/ correct（正确）/ uncertain（不确定）
    -- FP: ML 判为明显问题，LLM 说没问题
    -- FN: ML 判为正常，LLM 说有问题
    -- correct: 一致
    -- uncertain: 无法判断

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 记录创建时间
    reviewed        BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否已被人工复核
    human_verdict   TEXT,  -- 人工复核后的最终判断结果
    human_notes     TEXT  -- 人工复核备注说明
);

CREATE INDEX idx_ml_feedback_agreement ON ml_feedback (agreement);  -- 按一致/不一致筛选
CREATE INDEX idx_ml_feedback_error_type ON ml_feedback (error_type) WHERE error_type IN ('FP', 'FN');  -- 筛选误报和漏报记录
CREATE INDEX idx_ml_feedback_reviewed ON ml_feedback (reviewed) WHERE reviewed = FALSE;  -- 筛选待人工复核的记录


-- ── 4. 配置与实验跟踪 ────────────────────────────────────────────────
CREATE TABLE experiments (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 实验记录唯一标识（UUID 主键）
    name            TEXT NOT NULL,  -- 实验名称
    description     TEXT,  -- 实验描述说明
    config_json     JSONB NOT NULL,  -- 实验配置 JSON（哪些模块开关、参数设置）
    ml_model_version TEXT,  -- ML 模型的版本号
    llm_model        TEXT,  -- 使用的 LLM 模型名称
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),  -- 实验创建时间
    completed_at    TIMESTAMPTZ,  -- 实验完成时间
    status          TEXT NOT NULL DEFAULT 'running'  -- 实验状态：running（运行中）/ completed（已完成）/ failed（失败）
                    CHECK (status IN ('running', 'completed', 'failed')),  -- status 字段的合法性约束
    summary_json    JSONB  -- 实验结果摘要 JSON
);

CREATE TABLE experiment_cases (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),  -- 实验案例关联记录唯一标识
    experiment_id   UUID REFERENCES experiments(id) ON DELETE CASCADE,  -- 关联实验 ID（级联删除）
    case_id         UUID REFERENCES cases(id) ON DELETE CASCADE,  -- 关联案例 ID（级联删除）

    ml_prediction   TEXT,  -- ML 模型预测结果
    ml_confidence   DOUBLE PRECISION,  -- ML 模型预测置信度
    llm_result      JSONB,  -- LLM 的判定结果 JSON
    latency_ms      INTEGER,  -- 处理延迟（毫秒）

    UNIQUE (experiment_id, case_id)  -- 保证同一实验中不会重复记录同一案例
);


-- ══════════════════════════════════════════════════════════════════════
-- 视图
-- ══════════════════════════════════════════════════════════════════════

-- ML 性能监控视图
CREATE VIEW v_ml_performance AS  -- 创建 ML 性能监控视图：统计各预测类别的准确率
SELECT
    ml_prediction,  -- ML 预测类别
    COUNT(*) AS total,  -- 该类别总记录数
    SUM(CASE WHEN agreement THEN 1 ELSE 0 END) AS correct,  -- 预测正确的记录数
    ROUND(
        SUM(CASE WHEN agreement THEN 1 ELSE 0 END)::numeric /  -- 准确率计算（正确数/总数）
        NULLIF(COUNT(*), 0), 4  -- 避免除零错误
    ) AS accuracy,  -- 准确率（保留 4 位小数）
    AVG(ml_confidence) AS avg_confidence  -- 平均置信度
FROM ml_feedback
WHERE reviewed OR agreement  -- 只统计已人工复核或与 LLM 一致的记录
GROUP BY ml_prediction
ORDER BY ml_prediction;  -- 按预测类别排序

-- 偏差分析视图
CREATE VIEW v_ml_bias AS  -- 创建 ML 偏差分析视图：统计误报和漏报的分布
SELECT
    error_type,  -- 错误类型：FP（误报）/ FN（漏报）
    ml_prediction,  -- ML 预测类别
    COUNT(*) AS count,  -- 该错误类型的发生次数
    ROUND(AVG(ml_confidence)::numeric, 4) AS avg_ml_confidence  -- 该错误类型的平均 ML 置信度
FROM ml_feedback
WHERE error_type IS NOT NULL  -- 只统计有明显错误类型的记录
GROUP BY error_type, ml_prediction
ORDER BY count DESC;  -- 按发生次数降序排列

-- 待复核视图
CREATE VIEW v_pending_review AS  -- 创建待复核视图：列出需要人工复核的 FP/FN 案例
SELECT
    f.id AS feedback_id,  -- 反馈记录 ID
    c.id AS case_id,  -- 案例 ID
    c.mmsi,  -- 船舶 MMSI 号
    f.ml_prediction,  -- ML 预测结果
    f.llm_result_text,  -- LLM 判定结果摘要
    f.error_type,  -- 错误类型
    f.created_at  -- 反馈记录创建时间
FROM ml_feedback f
JOIN cases c ON c.id = f.case_id  -- 关联案例表获取更多信息
WHERE f.reviewed = FALSE  -- 仅筛选未被复核的记录
  AND f.error_type IN ('FP', 'FN')  -- 仅筛选误报或漏报的记录
ORDER BY f.created_at ASC;  -- 按创建时间升序排列（最旧的优先）