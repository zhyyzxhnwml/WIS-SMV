"""
triage_engine — ML Triage 路由引擎（三级级联第二级）
=====================================================

【这个包做什么？】
  对加速器输出的特征向量做自动分类和路由。
  核心流程：LightGBM 初判 → 置信度分级 → 向量库检索 → 路由决策。

架构：
    ┌──────────────────┐
    │特征向量 (64维)   │  ← 来自 ais_accelerator
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ LightGBM 四分类   │  ← 输出 {normal, obvious_issue, has_risk, needs_review}
    │ (置信度校准)       │
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ 路由决策引擎       │  ← 根据置信度 + 历史相似案例决定下一级处理方式
    │ (pgvector + Milvus)│
    └────────┬─────────┘
             ▼
    ┌──────────────────┐
    │ LLM 分层处理       │  ← 选择对应深度的 Prompt 模板
    │ (fast_confirm /   │
    │  careful_review / │
    │  needs_review /   │
    │  quick_verify)    │
    └──────────────────┘
"""

from .model import TriageModel, LABELS  # 导入四分类模型类  # 导入四分类标签列表
from .router import TriageRouter, RoutingDecision  # 导入路由决策引擎类  # 导入路由决策结果数据类

__all__ = [  # 模块公开接口列表
    "TriageModel",  # 四分类预测模型类
    "TriageRouter",  # 路由决策引擎类
    "RoutingDecision",  # 路由决策结果数据类
    "LABELS",  # 四分类标签列表
]