# AIS Accelarator — 三层级联消融实验与智能路由系统设计

> 文档生成日期：2026-07-16
> **现状对齐：2026-08-22**（按代码盘点更新实现状态；架构图与详细设计基本仍为有效目标设计）
> 基于 ais_accelerator v0.1.0 与 ship-violation-agent 现有架构

---

## 一、背景与问题

### 1.1 现有架构

当前系统由两个独立项目构成：

**ais_accelerator** — AIS 数据预处理流水线
```
原始CSV → quality(清洗) → segment(轨迹分段) → detect(候选事件检测)
       → merge(事件合并) → compress(RDP轨迹压缩) → rules(规则预筛)
       → adapter(拼装结构化输入) → 给 LLM
```

**ship-violation-agent** — LLM 违规判定系统
```
4 Agent 工作流: 规则引擎Agent → 轨迹分析Agent → AIS验证Agent → 主控Agent
或 单Agent验证: 加速器结果 + LLM 一次验证
```

### 1.2 核心问题

1. **预处理管线存在信息丢失风险** — compress 压缩可能丢掉关键轨迹点，detect 可能漏检真实违规，rules 预判可能带偏 LLM，且这些错误是无声的——LLM 无从知道预处理是否引入错误
2. **消融实验体系不完备** — 现有实验仅覆盖 precomputed_evidence 维度的消融，未覆盖全流水线模块级别的消融
3. **缺少自愈/验证机制** — 预处理结果正确与否缺乏自动验证

### 1.3 设计目标的演进

| 阶段 | 思路 | 问题 |
|---|---|---|
| 初始设计 | 预处理 → 压缩 → 给LLM精简数据 | 信息丢失，LLM无法自验证 |
| 改进思路 | 预处理打标记 → 保留完整数据给LLM | 数据完整了，但LLM处理所有case成本高 |
| **最终方案** | 三层级联：加速器打标记 → ML四分类路由 → LLM分级处理 + 反馈闭环 | 成本可控，安全可验证，持续进化 |

---

## 二、三层级联架构（最终方案）

### 2.1 架构总览

```
                        原始 AIS 数据
                              │
                              ▼
┌─────────────────────────────────────────────────┐
│  第一层：ais_accelerator (预处理 + 标记层)        │
│                                                  │
│  功能：不压缩、不筛选、不丢弃，只打标记            │
│                                                  │
│  ┌─────────┐ ┌────────┐ ┌───────┐ ┌──────────┐  │
│  │quality  │ │segment │ │detect │ │  rules   │  │
│  │清洗标记  │ │分段标记 │ │事件标记│ │规则命中标记│  │
│  └─────────┘ └────────┘ └───────┘ └──────────┘  │
│                          │                       │
│                   原始数据 + 全量标记              │
└──────────────────────────┬──────────────────────┘
                           │ 特征向量
                           ▼
┌─────────────────────────────────────────────────┐
│  第二层：ML Triage Engine (机器学习路由层)        │
│                                                  │
│  模型：LightGBM (四分类 + 置信度)                 │
│                                                  │
│  输入：加速器提取的特征向量                        │
│  输出：路由决策 + 相似案例引用                     │
│                                                  │
│  ┌──────────────┐                                │
│  │ 置信度 ≥ 0.9  ├───→ 明显问题 → LLM快速确认     │
│  │ 0.7 ≤ c < 0.9 ├───→ 查pgvector相似case        │
│  │              │      → 有风险 → LLM仔细审查     │
│  │ c < 0.7      ├───→ 查pgvector + Milvus       │
│  │              │      → 综合决定路由             │
│  │ 正常         ├───→ LLM快速验证                 │
│  └──────────────┘                                │
│                                                  │
│  存储：PostgreSQL + pgvector + Milvus             │
│  训练：初始训练 → 上线运行 → LLM反馈 → 增量更新   │
└──────────────────────────┬──────────────────────┘
                           │ 路由指令 + 相似案例
                           ▼
┌─────────────────────────────────────────────────┐
│  第三层：LLM (大模型判定层)                       │
│                                                  │
│  模型：Qwen2.5:7B (通过 Ollama)                  │
│                                                  │
│  三种处理深度：                                   │
│  ① 快速确认——收到"数据明显有问题"提示             │
│  ② 仔细审查——收到"需要认真看一下"提示 + 相似case  │
│  ③ 快速验证——收到"之前已验证过"提示               │
│                                                  │
│  输出：判定结果 + 置信度                          │
└──────────────────────┬───────────────────────────┘
                       │ 判定结果
                       ▼
┌─────────────────────────────────────────────────┐
│  反馈闭环 (持续学习)                              │
│                                                  │
│  LLM结果 vs ML预测 → 一致/不一致                  │
│  → 存入 PostgreSQL 训练集                         │
│  → 定期/触发式增量更新 LightGBM                   │
│  → ML 越跑越准，LLM 越用越少                      │
└─────────────────────────────────────────────────┘
```

### 2.2 三层职责对比

| 维度 | 第一层：加速器 | 第二层：ML | 第三层：LLM |
|---|---|---|---|
| **职责** | 数据清洗 + 打标记 + 特征提取 | 四分类路由 + 相似案例召回 | 最终判定 |
| **技术** | Python, Pydantic, 几何算法 | LightGBM, pgvector, Milvus | Qwen2.5:7B, LangChain |
| **数据量** | 全部原始数据 | 特征向量 | 路由后的数据子集 |
| **处理方式** | 确定性算法，0%丢数据 | 概率模型，持续进化 | 大模型推理 |
| **可解释性** | 完全可解释 | 特征重要性可解释 | 可解释性一般 |
| **性能** | 毫秒级/条 | 微秒级/条 | 秒级/条 |
| **成本** | 低 | 极低 | 高（Token消耗） |

---

## 三、详细设计

### 3.1 第一层：ais_accelerator 改造（打标记模式）

#### 3.1.1 改造原则

**不压缩、不筛选、不丢弃。** 所有原始轨迹点完整保留，加速器只在数据上打标记。

#### 3.1.2 标记体系

每个 AIS 点可携带以下标记：

```python
class AISAnnotation(BaseModel):
    """对单个 AIS 点或点区间的手工/自动标记"""
    type: str                                  # 标记类型
    # speed_violation | low_speed_fishing | restricted_area_entry
    # ais_gap | position_jump | course_anomaly | data_quality | rule_hit
    point_indices: list[int]                   # 对应点在轨迹数组中的索引
    start_time: datetime                       # 开始时间
    end_time: datetime                         # 结束时间
    label: str                                 # 中文描述
    detail: dict                               # 详细信息
    confidence: float                          # 加速器自评置信度
    source: str                                # 标记来源模块名
```

#### 3.1.3 每个模块的标记行为变更

| 模块 | 现行为（过滤） | 改行为（打标记） |
|---|---|---|
| **quality.py** | 过滤无效点、标注ais_gap/position_jump | 保留点，在 `tags` 字段标注问题 |
| **segment.py** | 切分轨迹为独立segment | 在切分点标记 `segment_boundary` |
| **detect.py** | 提取候选事件，事件外数据丢弃 | 在对应点上标记事件类型 |
| **merge.py** | 合并相似事件，保留合并后事件 | 标记关联事件ID，保留合并关系 |
| **compress.py** | RDP选择关键点，其余丢弃 | 标记 `key_point` 但保留所有点 |
| **rules.py** | 选择 top5 规则 | 标记所有命中规则 |
| **adapter.py** | 拼装精简输入 | 拼装完整数据 + annotations 数组 |

#### 3.1.4 加速器输出格式

```json
{
  "ship": {"mmsi": "412345678", "ship_type": "渔船", ...},
  "annotations": [
    {
      "type": "speed_violation",
      "point_indices": [47, 48, 49, 50, 51, 52, 53],
      "start_time": "2024-03-15T08:30:00Z",
      "end_time": "2024-03-15T08:36:00Z",
      "label": "区域超速",
      "detail": {"max_speed": 15.2, "speed_limit": 12.0},
      "confidence": 0.92,
      "source": "detect"
    },
    {
      "type": "ais_gap",
      "point_indices": [88],
      "start_time": "2024-03-15T09:00:00Z",
      "end_time": "2024-03-15T09:45:00Z",
      "label": "AIS信号中断",
      "detail": {"gap_minutes": 45.0},
      "confidence": 0.95,
      "source": "quality"
    }
  ],
  "counts": {
    "total_points": 200,
    "critical_issues": 0,
    "warnings": 2,
    "info": 5
  },
  "track": [
    {"lat": 29.5, "lon": 106.5, "speed": 8.0, "course": 180,
     "timestamp": "...", "tags": ["segment_start"]},
    {"lat": 29.5, "lon": 106.5, "speed": 14.5, "course": 182,
     "timestamp": "...", "tags": ["speed_violation"]},
    ...
  ],
  "feature_vector": [0.0, 1.0, 0.5, ...],
  "metadata": {
    "pipeline_version": "ais-accelerator/v1",
    "quality_level": "fair",
    "processed_at": "2026-07-16T10:00:00Z"
  }
}
```

### 3.2 第二层：ML Triage Engine

#### 3.2.1 为什么选择 LightGBM

| 对比项 | LightGBM | 深度神经网络 | 逻辑回归 |
|---|---|---|---|
| 特征类型 | 数值+类别+标签，天然适配 | 需要embedding | 只适合线性 |
| 小样本表现 | 几百条可用 | 几千条起步 | 也还行 |
| 训练速度 | 秒级 | 分钟级 | 秒级 |
| 可解释性 | 特征重要性、SHAP值 | 黑盒 | 可解释 |
| 增量学习 | 支持 `init_model` | 需finetune | 支持 |
| 抗过拟合 | 有机制 | 依赖正则化 | 弱 |

#### 3.2.2 分类输出（✅ 已实现为四分类 + 置信度路由）

> 实现为 **LightGBM 四分类**：`normal` / `obvious_issue` / `has_risk` / `needs_review`
> （`triage_engine/router.py` `RoutingDecision` + `model.py`）。
> 置信度路由阈值：HIGH=0.90（`normal`/`obvious_issue` 可 direct_pass 不进 LLM）、MEDIUM=0.70、LOW=0.50。
> 路由类别：`direct_pass` / `fast_confirm` / `careful_review` / `needs_review`。

| 类别 | 标签 | 路由规则 | LLM提示 |
|---|---|---|---|
| `obvious_issue` | 明显问题 | 直接送LLM快速确认 | "这些数据明显有问题，请快速确认违规类型" |
| `has_risk` | 有风险 | 查pgvector相似case，附上历史结果送LLM | "这些数据需要你认真看一下，附上历史相似案例供参考" |
| `needs_review` | 低确定 | 查pgvector + Milvus，两个结果一起参考 | "这些数据无法自动判定，请仔细审查" |
| `normal` | 正常 | 直接送LLM快速验证 | "这些数据之前已验证过类似情况，快速确认即可" |

#### 3.2.3 特征提取（✅ 已实现）

> 实际实现：`ais_accelerator/annotator.py` 的 `FEATURE_NAMES` 共 **28 维**特征
> （含 `original_point_count / annotation_count / critical/warning/info_annotation_count /
> avg/max/min_annotation_confidence / total_duration_minutes / speed_variance / avg/max/min_speed /
> course_change_count / ais_gap_count / position_jump_count / rule_hit_count / event_count /
> overlap_annotation_count / segment_count / quality_issue_count / duplicate_point_count /
> collision_risk_count / max_cri / min_dcpa_nm / min_tcpa_minutes / avg_relative_speed / nearby_ship_count`）。
> 模型层（`triage_engine/model.py`）按 64 维训练（22 实际 + 42 预留，代码内 pad），
> `triage_engine/schema.sql` 的 `VECTOR(28)` 已同步，`database/schema.sql` 的 `VECTOR(64)` 为旧值。

从加速器的标记中提取以下特征向量（每个 segment 一条）：

```python
# 数值特征
FEATURE_NUMERIC = [
    "original_point_count",          # 原始轨迹点数
    "annotation_count",              # 标记总数
    "critical_annotation_count",     # 严重标记数
    "warning_annotation_count",      # 警告标记数
    "avg_annotation_confidence",     # 标记平均置信度
    "max_annotation_confidence",     # 标记最高置信度
    "min_annotation_confidence",     # 标记最低置信度
    "total_duration_minutes",        # 轨迹总时长
    "speed_variance",                # 速度方差
    "avg_speed",                     # 平均速度
    "max_speed",                     # 最高速度
    "course_change_count",           # 航向变化次数
    "ais_gap_count",                 # AIS中断次数
    "position_jump_count",           # 位置跳变次数
    "rule_hit_count",                # 规则命中数
    "overlap_annotation_count",      # 重叠标记数（同时命中多种类型）
]

# 分类特征（one-hot 编码）
FEATURE_CATEGORICAL = [
    "quality_level",                 # good / fair / poor
    "ship_type",                     # 渔船 / 货轮 / 油轮 / 客轮 ...
    "primary_candidate_type",        # 主要候选事件类型
    "zone_type",                     # 区域类型
    "season_restriction_active",     # 是否处于禁渔期/限航期
]

# 布尔特征（0/1）
FEATURE_BOOLEAN = [
    "has_rule_violation",            # 是否命中明确违规规则
    "has_critical_quality_issue",    # 是否存在严重数据质量问题
    "has_ais_gap",                   # 是否含AIS信号中断
    "has_speed_violation",           # 是否含超速
    "has_fishing_suspicion",         # 是否含疑似捕捞
    "has_restricted_entry",          # 是否进入限制区域
    "has_course_anomaly",            # 是否航向异常
    "multiple_event_types",          # 是否包含多种事件类型
]
```

#### 3.2.4 路由决策流程（✅ 已实现于 `triage_engine/router.py`）

```python
def triage(self, feature_vector, track_points, annotations):
    # Step 1: LightGBM 预判（四分类）
    pred = self.model.predict(feature_vector)  # [p_normal, p_obvious, p_risk, p_review]
    category = pred.argmax()
    confidence = pred.max()

    # Step 2: 基于置信度路由（实际阈值：HIGH=0.90 / MEDIUM=0.70 / LOW=0.50）
    if category in ("normal", "obvious_issue") and confidence >= 0.90:
        return Route("direct_pass", confidence, [])       # 不进 LLM

    if category == "has_risk" and confidence >= 0.70:
        similar = self.pgvector.search(feature_vector, top_k=3)
        return Route("fast_confirm", confidence, similar)

    if confidence >= 0.50:
        similar = self.pgvector.search(feature_vector, top_k=3)
        return Route("careful_review", confidence, similar)

    # confidence < 0.50 或 needs_review → 全面查
    similar = self.pgvector.search(feature_vector, top_k=5)
    traj_similar = self.milvus.search(
        self._track_to_vector(track_points), top_k=3
    )
    return Route("needs_review", confidence, similar + traj_similar)
```

### 3.3 第三层：LLM 分级处理

#### 3.3.1 三种处理深度的 Prompt

**快速确认（明显问题）：**
```text
系统分析标记：这批数据存在明显的{type}问题，{detail}
置信度：{confidence}
请快速确认违规类型，不需要逐点验证。
```

**仔细审查（有风险）：**
```text
系统分析标记：这批数据存在{type}风险，{detail}
置信度：{confidence}（未达到自动判定阈值）
历史相似案例：{similar_cases}
请仔细审查以下完整轨迹数据，验证标记是否准确，给出最终判定。
```

**快速验证（正常）：**
```text
系统分析标记：这批数据无明显异常（置信度{confidence}）
历史已有{count}例相似数据被判定为正常。
请快速过目确认。
```

#### 3.3.2 LLM 输出格式（统一）

```json
{
  "violation_detected": true/false,
  "violation_type": "违规代码",
  "violation_label": "违规描述",
  "confidence": 0.0-1.0,
  "agrees_with_ml_triage": true/false,
  "triage_disagreement_reason": "如推翻ML判定，说明原因",
  "final_assessment": "综合判定说明",
  "recommended_action": "auto_alert/manual_review/monitor",
  "feedback_for_ml": {
    "llm_judgment_confidence": 0.0-1.0,
    "markings_were_helpful": true/false,
    "missed_issues": [],
    "false_positives": []
  }
}
```

### 3.4 数据库设计

#### 3.4.1 PostgreSQL + pgvector

存储业务数据、特征向量、训练集。

```sql
-- 1. 案例库（带特征向量）
CREATE TABLE cases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mmsi TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- 数据标识
    source_file TEXT,
    segment_id TEXT,
    event_id TEXT,
    
    -- 特征向量（加速器输出；实际维度 28，见 3.2.3，此处保留旧 64 写法）
    feature_vector VECTOR(64),           -- pgvector 索引
    feature_json JSONB,                  -- 原始特征明细
    
    -- 标记
    annotations JSONB,                   -- 加速器标记完整数据
    annotation_counts JSONB,             -- 标记统计
    
    -- ML 结果
    ml_prediction TEXT,                  -- 路由结果
    ml_confidence FLOAT,
    
    -- LLM 结果
    llm_result JSONB,                    -- LLM 完整输出
    llm_agreement BOOLEAN,              -- LLM是否同意ML路由
    llm_feedback JSONB,                 -- LLM对数据标记的反馈
    
    -- 业务字段
    has_violation BOOLEAN,
    violation_type TEXT,
    ground_truth BOOLEAN,               -- 真实标注（如果已复核）
    
    -- 索引
    created_at_idx,
    mmsi_idx,
    has_violation_idx
);

-- 2. 训练集（带标注的记录）
CREATE TABLE training_set (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID REFERENCES cases(id),
    
    feature_vector VECTOR(64),             -- 实际维度 28（同 cases）
    label TEXT NOT NULL,                   -- 目标变量（四分类）
    label_source TEXT,                     -- LLM / 人工复核
    label_confidence FLOAT,
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    used_in_training BOOLEAN DEFAULT FALSE,
    weight FLOAT DEFAULT 1.0              -- 样本权重（重要case更重）
);

-- 3. LLM 反馈表（用于分析ML偏差）
CREATE TABLE ml_feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id UUID REFERENCES cases(id),
    
    ml_prediction TEXT,
    ml_confidence FLOAT,
    llm_result TEXT,
    llm_confidence FLOAT,
    
    agreement BOOLEAN,                    -- LLM vs ML 一致？
    error_type TEXT,                      -- FP / FN / correct
    
    created_at TIMESTAMPTZ DEFAULT NOW(),
    reviewed BOOLEAN DEFAULT FALSE,        -- 已被复核？
    human_verdict TEXT                     -- 人工复核结果
);

-- pgvector 索引
CREATE INDEX idx_cases_feature ON cases 
    USING ivfflat (feature_vector vector_cosine_ops)
    WITH (lists = 100);
```

#### 3.4.2 Milvus

存储轨迹序列向量，用于序列级相似度搜索。

```python
# Milvus Collection 设计
collection_schema = {
    "collection_name": "trajectory_sequences",
    "description": "AIS轨迹序列向量，用于序列模式匹配",
    "fields": [
        {"name": "id", "type": DataType.VARCHAR, "max_length": 64, "is_primary": True},
        {"name": "mmsi", "type": DataType.VARCHAR, "max_length": 16},
        {"name": "segment_id", "type": DataType.VARCHAR, "max_length": 64},
        {"name": "sequence_vector", "type": DataType.FLOAT_VECTOR, "dim": 128},
        {"name": "has_violation", "type": DataType.BOOL},
        {"name": "violation_type", "type": DataType.VARCHAR, "max_length": 32},
        {"name": "point_count", "type": DataType.INT32},
        {"name": "duration_minutes", "type": DataType.FLOAT},
        {"name": "metadata_json", "type": DataType.VARCHAR, "max_length": 2048},
        {"name": "created_at", "type": DataType.INT64},
    ],
    "indexes": [
        {
            "index_name": "sequence_vector_idx",
            "field_name": "sequence_vector",
            "index_type": "IVF_FLAT",
            "metric_type": "COSINE",
            "params": {"nlist": 128}
        }
    ]
}
```

#### 3.4.3 向量化策略

| 数据类型 | 存储位置 | 向量化方法 | 维度 |
|---|---|---|---|
| 特征向量 | pgvector | 加速器提取的数值+分类特征（annotator.py `FEATURE_NAMES`） | 28（模型层 pad 到 64 训练） |
| 轨迹序列 | Milvus | 轨迹点序列 → 固定长度向量（padding + 插值） | 128 |

轨迹序列向量化方案：
```python
def track_to_vector(track_points: list[dict]) -> list[float]:
    """
    将轨迹序列转为固定长度向量。
    方法：对时间轴均匀采样 N 个点，每个点取 [lat, lon, speed, course] 4 维，
    组成 4N 维向量。不足 padding，超出截断。
    N = 32 → 128 维
    """
    N = 32
    # 时间轴均匀插值采样
    sampled = interpolate_to_n(track_points, N)
    # 展平为 [lat1, lon1, speed1, course1, lat2, ...]
    vector = []
    for pt in sampled:
        vector.extend([pt["lat"], pt["lon"], pt["speed"], pt["course"]])
    return vector  # 128 维
```

### 3.5 反馈闭环与持续学习

#### 3.5.1 闭环流程

```
LLM 判定完成
    │
    ▼
┌─────────────────────────────┐
│ 比较 LLM 结果 vs ML 预测     │
│                             │
│  LLM.violation_detected     │
│  VS                         │
│  ML.prediction (四分类)      │
└──────────┬──────────────────┘
           │
     ┌─────┴─────┐
     │           │
   一致         不一致
     │           │
     ▼           ▼
┌────────┐ ┌───────────────────┐
│ ML置信 │ │ 重点分析            │
│ 度+1   │ │                    │
│        │ │ ① 插入反馈表        │
│        │ │ ② 触发增量训练      │
│        │ │ ③ 如多次不一致 →    │
│        │ │   本轮 batch 结束   │
│        │ │   后自动调参         │
└────────┘ └───────────────────┘
```

#### 3.5.2 增量学习

```python
def incremental_update():
    """增量更新 LightGBM 模型"""
    # 取最近 N 条有 LLM 反馈的数据
    new_data = fetch_unused_training_data(limit=1000)
    
    if len(new_data) < 100:
        return  # 样本不足，跳过
    
    X = extract_features(new_data)
    y = extract_labels(new_data)  # 以 LLM 判定为 label
    
    # 增量训练（保留旧模型）
    self.model = lgb.train(
        params,
        lgb.Dataset(X, y),
        num_boost_round=50,
        init_model=self.model,       # 从当前模型继续训练
        valid_sets=valid_set,
        callbacks=[early_stopping(10)],
    )
    
    # 保存新模型
    self.model.save_model("triage_model.txt")
```

#### 3.5.3 样本权重策略

- **不一致样本**：权重 3.0（重点学习）
- **高置信度正确样本**：权重 1.0
- **低置信度正确样本**：权重 0.5
- **人工复核样本**：权重 5.0（最高优先级）

### 3.6 消融实验框架（扩展）

在三层级联架构下，消融实验覆盖三个层面：

#### 3.6.1 加速器层消融

| 实验 | 配置 | 预期影响 |
|---|---|---|
| 无标记 | 加速器关闭 | LLM 面对裸数据，准确率↓，Token↑ |
| 无质量标记 | 关闭 quality 标记 | 脏数据未被标记，LLM 可能被误导 |
| 无事件检测 | 关闭 detect | LLM 自己发现异常点，可能漏检 |
| 无规则标记 | 关闭 rules | LLM 自己判断法规，可能判错 |
| 无轨迹分段 | 关闭 segment | 整条轨迹不分段，上下文变长 |

#### 3.6.2 ML 层消融

| 实验 | 配置 | 预期影响 |
|---|---|---|
| 无 ML | 跳过 Triage，全部送 LLM | Token↑，成本↑ |
| 无向量检索 | ML 只做四分类，不查相似案例 | 低置信度 case 准确率↓ |
| 无 Milvus | 只查 pgvector | 轨迹模式匹配能力降低 |

#### 3.6.3 LLM 层消融

| 实验 | 配置 | 预期影响 |
|---|---|---|
| 无分级 | 所有 case 用同一 prompt | 效率降低 |
| 无 feedback | 不收集 LLM 反馈 | ML 无法持续学习 |

---

## 四、实施路线图

> **状态说明（2026-08-22）：** 带 ✅ 的已实现；带 ⚠️ 的仅部分/未完整落地；空项为待办。

### Phase 1：加速器改造（✅ 已完成）

- [x] quality.py 打标记模式（`QualityReport`：`good/fair/poor`，`QualityIssue.code` 10 种）✅
- [x] segment.py 轨迹分段标记（`segments` 边界索引）✅
- [x] detect.py 候选事件检测（`CandidateType` 7 类：restricted_area_entry / speed_violation / low_speed_fishing / ais_gap / course_anomaly / position_jump / collision_risk）✅
- [x] merge.py 事件合并 ✅
- [x] compress.py RDP 事件感知压缩（500点→~10点，`compress_for_event`）✅
- [x] rules.py 规则预筛（`JsonRuleProvider` 射线法 + `RagRuleProvider` + `season_is_active`）✅
- [x] annotator.py 组装完整数据 + annotations + **28 维特征** ✅
- [x] water.py WaterChecker 陆地规避（LandMask 栅格 + 数据驱动航道走廊）✅
- [x] collision.py 碰撞风险评估（CIR / DCPA / TCPA）✅

### Phase 2：数据库搭建（✅ 已完成）

- [x] PostgreSQL + pgvector 建表（`triage_engine/schema.sql` `VECTOR(28)`；`database/schema.sql` 含完整 E-R）✅
- [x] Milvus 建 collection（`regulation_articles` 法规向量，1024 维；**仅 Docker Server 模式**）✅
- [x] 向量化工具（`pgvector_client.py` + `milvus_client.py`）✅
- [x] 写入/查询接口 + 连接自愈（closed-channel 重建重试）✅

### Phase 3：ML Triage Engine（✅ 已完成，分类数已从三分类升级为四分类）

- [x] 初始训练集构建（`train.py` `build_synthetic_dataset` 64 维特征模板）✅
- [x] LightGBM **四分类**模型训练（`normal / obvious_issue / has_risk / needs_review`）✅
- [x] 路由决策引擎（`router.py` `TriageRouter.route()` + `RoutingDecision`）✅
- [x] pgvector 相似 case 召回 ✅
- [x] Milvus 轨迹序列召回 ✅
- [x] 置信度阈值（HIGH=0.90 / MEDIUM=0.70 / LOW=0.50）✅

### Phase 4：LLM 分级处理（✅ 已完成）

- [x] 四级 Prompt 模板（`fast_confirm / careful_review / needs_review / quick_verify`）✅
- [x] LLM 统一输出格式 + feedback 字段（`feedback.py` 分歧样本权重 3.0/1.0）✅
- [ ] 多模型对比（Qwen2.5:7B 单一模型，未做多模型对比评估）

### Phase 5：反馈闭环（✅ 已完成）

- [x] LLM 结果 → 训练集自动插入（`data/feedback_training_data.json` / PostgreSQL 模式）✅
- [x] LightGBM 增量更新（`incremental_update`，RETRAIN_THRESHOLD=50，COOLDOWN=3600）✅
- [ ] 偏差分析仪表盘（ML vs LLM 一致率监控）—— 未做（Roadmap）
- [ ] 人工复核管理—— 未做（Roadmap）

### Phase 6：消融实验（⚠️ 部分完成）

- [ ] 全流水线消融运行器（遍历所有模块开关组合）—— 未做
- [x] 对比报告生成（速度/实用性/容错，`experiments/run.py` → `results/run_<timestamp>/`）✅
- [x] 结果可视化（charts/ + experiment_summary.csv）✅
- [ ] 论文级消融（人工 GT + 6 基线 + 统计检验）—— 规划见 [论文/方案A_三级级联论文方案.md](../论文/方案A_三级级联论文方案.md)

---

## 五、关键技术决策记录

| 决策 | 选择 | 替代方案 | 理由 |
|---|---|---|---|
| ML模型 | LightGBM | DNN, XGBoost | 表格数据天然适配，增量学习支持好，可解释 |
| 向量检索策略 | pgvector + Milvus 双检索 | 只用其一 | 结构化向量（特征）用pgvector，序列向量用Milvus |
| 轨迹序列向量化 | 时间轴均匀采样 N=32 → 128维 | 直接拼接、RNN embedding | 简单可控，不依赖NN |
| 增量学习 | `init_model` 参数 | 全量重训练 | 高效，保留旧知识 |
| 训练集标注 | LLM 结果作为弱标注 | 纯人工标注 | 成本可控，配合人工复核校正 |
| 加速器输出 | 不压缩不丢弃只打标记 | 保留原来的压缩逻辑 | 保证安全可验证 |

---

## 六、相关文件索引（✅ 全部已实现）

| 文件 | 说明 |
|---|---|
| `ais_accelerator/` | 加速器核心模块 ✅ |
| `ais_accelerator/annotator.py` | 轨迹标注 + 28 维特征提取（`FEATURE_NAMES`） |
| `ais_accelerator/models.py` | Pydantic 模型（`AnnotatedTrack` / `CandidateType` 7 类） |
| `ais_accelerator/pipeline.py` | 管线主流程（ingest→quality→segment→detect→碰撞→merge→compress→build） |
| `ais_accelerator/facade.py` | 外观模式统一接口（`AISAccelerationFacade`） |
| `experiments/run.py` | 架构对比实验入口（速度/实用性/容错） |
| `experiments/feedback_experiment/` | 反馈闭环实验（阈值扫描 / A-B 对照） |
| `triage_engine/` | ML 路由层 ✅ |
| `triage_engine/model.py` | LightGBM 四分类模型封装 |
| `triage_engine/router.py` | 路由决策（`TriageRouter` / `RoutingDecision`） |
| `triage_engine/pgvector_client.py` | PostgreSQL + pgvector 客户端 |
| `triage_engine/milvus_client.py` | Milvus 客户端（轨迹检索 + 法规向量） |
| `triage_engine/train.py` | 初始训练 + 增量训练 |
| `llm_layer/` | LLM 层 ✅ |
| `llm_layer/prompts.py` | 四级 Prompt 模板 |
| `llm_layer/handler.py` | LLM 调用（HTTP 探测 + 5 次重试 + 安全默认） |
| `llm_layer/feedback.py` | 反馈闭环 + 增量训练 |
| `memory/` | 记忆系统（Session / Ship / Pattern）✅ |
| `run.py` | 全链路唯一入口（`_ensure_initialized` + `process_file`） |
| `orchestrator.py` | 主编排器（`EndToEndOrchestrator`，三模式） |
