"""
triage_engine/router.py — 路由决策引擎
=========================================

【这个文件做什么？】
  根据 ML 模型输出的四分类概率，决定三层级联架构中的路由策略：

  1. normal（高置信度 ≥0.9）→ 直接通过，不走相似检索
  2. obvious_issue（高置信度 ≥0.9）→ 直接判定，不走 LLM
  3. has_risk（中置信度 0.5~0.7）→ pgvector 相似检索 + 可能走 LLM
  4. needs_review（低置信度 ≤0.5）→ 走完整流程（pgvector + Milvus + LLM）

【路由决策结果 RoutingDecision】
  包含：
  - 原始预测（category + confidence）
  - 是否启用 pgvector / Milvus / LLM
  - LLM prompt 类别
  - 相似检索命中结果
  - 延迟统计

【置信度阈值】
  高阈值：0.90 — 可不经 LLM 直接判定
  中阈值：0.70 — 需相似检索辅助
  低阈值：0.50 — 以下走完整审查流程
"""

from __future__ import annotations

import time  # 时间模块（用于延迟测量）
from dataclasses import dataclass, field  # 数据类装饰器
from typing import Any, Optional  # 类型注解

import numpy as np  # 数值计算库

from .model import TriageModel, LABELS  # 导入模型类和四分类标签


# 置信度阈值常量
HIGH_CONFIDENCE_THRESHOLD = 0.90  # 高置信度阈值（≥0.90 可不走 LLM）
MEDIUM_CONFIDENCE_THRESHOLD = 0.70  # 中置信度阈值（≥0.70 需相似检索）
LOW_CONFIDENCE_THRESHOLD = 0.50  # 低置信度阈值（<0.50 需全面审查）


@dataclass
class RoutingDecision:
    """路由决策的数据类，记录完整决策链信息。"""

    case_id: str = ""  # 案例唯一标识
    mmsi: str = ""  # 船舶 MMSI 号

    ml_category: str = ""  # ML 预测类别
    ml_confidence: float = 0.0  # ML 预测置信度

    routing: str = ""  # 路由决策：direct_pass / fast_confirm / careful_review / needs_review
    needs_pgvector: bool = False  # 是否需要 pgvector 相似检索
    needs_milvus: bool = False  # 是否需要 Milvus 轨迹序列检索
    needs_llm: bool = False  # 是否需要 LLM 判定

    pgvector_hits: list = field(default_factory=list)  # pgvector 相似检索命中结果列表
    milvus_hits: list = field(default_factory=list)  # Milvus 相似检索命中结果列表
    pgvector_latency: float = 0.0  # pgvector 检索耗时的秒数
    milvus_latency: float = 0.0  # Milvus 检索耗时的秒数
    total_latency: float = 0.0  # 总处理耗时的秒数

    all_probs: dict = field(default_factory=dict)  # 四类的完整概率分布

    @property
    def all_probabilities(self) -> dict:
        """兼容旧的属性名 all_probabilities -> all_probs"""
        return self.all_probs

    @property
    def pgvector_similar(self) -> list:
        """兼容旧代码可能引用的 pgvector_similar"""
        return self.pgvector_hits

    @property
    def triage_latency_ms(self) -> float:
        """兼容 run.py 中引用的 triage_latency_ms"""
        return self.total_latency * 1000

    @property
    def llm_prompt_template(self) -> str:
        """根据路由类别返回 LLM prompt 模板名称。"""
        templates = {  # 路由类别到 prompt 模板的映射字典
            "obvious_issue": "fast_confirm",  # 明显问题 → 快速确认模板
            "has_risk": "careful_review",  # 存在风险 → 仔细审查模板
            "needs_review": "needs_review",  # 无法判定 → 全面审查模板
            "normal": "quick_verify",  # 正常 → 快速验证模板
        }
        return templates.get(self.ml_category, "needs_review")  # 默认使用全面审查模板

    def to_dict(self) -> dict:
        """转为字典（便于序列化）。"""
        return {  # 返回所有字段的字典表示
            "case_id": self.case_id,  # 案例 ID
            "mmsi": self.mmsi,  # MMSI 号
            "ml_category": self.ml_category,  # ML 预测类别
            "ml_confidence": self.ml_confidence,  # ML 置信度
            "routing": self.routing,  # 路由决策
            "needs_pgvector": self.needs_pgvector,  # 是否需要 pgvector
            "needs_milvus": self.needs_milvus,  # 是否需要 Milvus
            "needs_llm": self.needs_llm,  # 是否需要 LLM
            "llm_prompt_template": self.llm_prompt_template,  # LLM prompt 模板
            "pgvector_hits_count": len(self.pgvector_hits),  # pgvector 命中数
            "milvus_hits_count": len(self.milvus_hits),  # Milvus 命中数
            "latency_seconds": round(self.total_latency, 3),  # 总耗时（保留 3 位小数）
        }


class TriageRouter:
    """路由决策引擎。

    负责：
    - 接收 64 维特征向量
    - 调用 ML 模型推理
    - 根据置信度路由到不同审核链路
    - 触发向量相似检索（pgvector + Milvus）
    """

    def __init__(
        self,
        model: TriageModel,  # ML Triage 模型实例
        pgvector_client: Optional[Any] = None,  # pgvector 数据库客户端（可选）
        milvus_client: Optional[Any] = None,  # Milvus 向量数据库客户端（可选）
    ):
        self.model = model  # 存储 ML 模型引用
        self.pgvector_client = pgvector_client  # 存储 pgvector 客户端引用
        self.milvus_client = milvus_client  # 存储 Milvus 客户端引用

    def route(
        self,
        feature_vector: list[float],  # 64 维特征向量
        case_id: str = "",  # 案例 ID（可选）
        mmsi: str = "",  # MMSI 号（可选）
    ) -> RoutingDecision:
        """核心路由方法：输入 64 维特征，输出路由决策。

        决策逻辑（四步）：

        第 1 步：ML 推理
        └── 输出类别和置信度

        第 2 步：结合置信度确定路由
        ├── confidence ≥ 0.90:
        │   ├── obvious_issue → direct_pass（直接判定严重问题）
        │   └── normal → direct_pass（直接通过）
        ├── confidence ≥ 0.70:
        │   ├── has_risk → fast_confirm（相似检索 + LLM 快速确认）
        │   └── needs_review → careful_review（走完整流程）
        ├── confidence ≥ 0.50:
        │   ├── has_risk → careful_review（需要相似检索 + LLM 仔细审查）
        │   └── needs_review → needs_review（需要完整审查）
        └── confidence < 0.50:
            └── 任何类别 → needs_review（全部需要完整审查）

        第 3 步：按需执行向量检索
        ├── needs_pgvector → 查特征相似案例
        └── needs_milvus → 查轨迹形态相似案例

        第 4 步：组装 RoutingDecision 返回
        """
        decision = RoutingDecision(case_id=case_id, mmsi=mmsi)  # 创建决策数据类实例
        start = time.time()  # 记录开始时间

        # == 第 1 步：ML 推理 ==
        category, confidence, all_probs = self.model.predict_single(feature_vector)  # 调用模型推理单条数据
        decision.ml_category = category  # 赋值预测类别
        decision.ml_confidence = confidence  # 赋值预测置信度
        decision.all_probs = all_probs if all_probs else {}

        # == 第 2 步：路由决策逻辑 ==
        if confidence >= HIGH_CONFIDENCE_THRESHOLD:  # 高置信度（≥0.90）
            if category in ("obvious_issue",):  # 明显违规
                decision.routing = "direct_pass"  # 直接判定，不走 LLM
                decision.needs_pgvector = False  # 不需要 pgvector
                decision.needs_milvus = False  # 不需要 Milvus
                decision.needs_llm = False  # 不需要 LLM
            elif category == "normal":  # 正常
                decision.routing = "direct_pass"  # 直接通过（正常情况）
                decision.needs_pgvector = False
                decision.needs_milvus = False
                decision.needs_llm = False
            else:  # has_risk / needs_review（高置信度下的风险/待审）
                decision.routing = "fast_confirm"  # 快速确认
                decision.needs_pgvector = True  # 需要 pgvector 辅助
                decision.needs_milvus = False  # 不需要 Milvus
                decision.needs_llm = True  # 需要 LLM 确认

        elif confidence >= MEDIUM_CONFIDENCE_THRESHOLD:  # 中高置信度（0.70~0.90）
            if category == "has_risk":  # 存在风险
                decision.routing = "fast_confirm"  # 快速确认
                decision.needs_pgvector = True  # 需要 pgvector
                decision.needs_milvus = False  # 不需要 Milvus
                decision.needs_llm = True  # 需要 LLM
            elif category == "needs_review":  # 需要审查
                decision.routing = "careful_review"  # 仔细审查
                decision.needs_pgvector = True  # 需要 pgvector
                decision.needs_milvus = True  # 需要 Milvus
                decision.needs_llm = True  # 需要 LLM
            else:  # normal / obvious_issue（中等置信度下的正常/明显问题）
                decision.routing = "fast_confirm"  # 快速确认
                decision.needs_pgvector = True  # 需要 pgvector
                decision.needs_milvus = False  # 不需要 Milvus
                decision.needs_llm = True  # 需要 LLM

        elif confidence >= LOW_CONFIDENCE_THRESHOLD:  # 中低置信度（0.50~0.70）
            if category == "has_risk":  # 存在风险
                decision.routing = "careful_review"  # 仔细审查
                decision.needs_pgvector = True  # 需要 pgvector
                decision.needs_milvus = True  # 需要 Milvus
                decision.needs_llm = True  # 需要 LLM
            else:  # 其他类别低置信度
                decision.routing = "needs_review"  # 完整审查
                decision.needs_pgvector = True
                decision.needs_milvus = True
                decision.needs_llm = True

        else:  # 置信度低于低阈值（<0.50）
            decision.routing = "needs_review"  # 全部进入完整审查
            decision.needs_pgvector = True  # 需要全部检索
            decision.needs_milvus = True
            decision.needs_llm = True

        # == 第 3 步：按需执行向量检索 ==
        if decision.needs_pgvector and self.pgvector_client:  # 需要 pgvector 且客户端可用
            pg_start = time.time()  # 记录 pgvector 开始时间
            try:
                decision.pgvector_hits = self.pgvector_client.search_similar(  # 执行 pgvector 相似搜索
                    feature_vector, top_k=5,  # 查询向量和返回数量
                )
            except Exception as e:  # 检索异常
                print(f"[Router] pgvector 检索错误: {e}")
            decision.pgvector_latency = time.time() - pg_start  # 计算 pgvector 耗时

        if decision.needs_milvus and self.milvus_client:  # 需要 Milvus 且客户端可用
            ml_start = time.time()  # 记录 Milvus 开始时间
            try:
                # 将 64 维特征向量转为 128 维轨迹向量（简化：其他字段用默认值）
                from .milvus_client import track_to_vector  # 导入轨迹转向量函数
                dummy_track = [  # 构造虚拟轨迹点（简化处理）
                    {  # 单个轨迹点
                        "lat": feature_vector[0] % 90,  # 纬度（用特征值取模）
                        "lon": feature_vector[1] % 180,  # 经度（用特征值取模）
                        "speed": feature_vector[10] % 50,  # 速度（用平均速度特征取模）
                        "course": feature_vector[13] % 360,  # 航向（用航向变化特征取模）
                    }
                ]
                decision.milvus_hits = self.milvus_client.search_similar(  # 执行 Milvus 相似搜索
                    dummy_track, top_k=5,  # 虚拟轨迹和返回数量
                )
            except Exception as e:  # 检索异常
                print(f"[Router] Milvus 检索错误: {e}")
            decision.milvus_latency = time.time() - ml_start  # 计算 Milvus 耗时

        # == 第 4 步：组装结果 ==
        decision.total_latency = time.time() - start  # 计算总耗时
        return decision  # 返回完整路由决策

    def route_batch(
        self,
        feature_vectors: list[list[float]],  # 批量特征向量列表
        case_ids: Optional[list[str]] = None,  # 案例 ID 列表（可选）
        mmsi_list: Optional[list[str]] = None,  # MMSI 号列表（可选）
        batch_size: int = 32,  # 每批处理数量（默认 32）
    ) -> list[RoutingDecision]:
        """批量路由处理。"""
        n = len(feature_vectors)  # 总样本数
        decisions = []  # 初始化结果列表

        case_ids = case_ids or [""] * n  # 填充案例 ID
        mmsi_list = mmsi_list or [""] * n  # 填充 MMSI 号

        for i in range(0, n, batch_size):  # 按批次遍历
            batch = feature_vectors[i:i + batch_size]  # 当前批次的特征向量
            for j, vec in enumerate(batch):  # 遍历批次中的每条数据
                dec = self.route(  # 单条路由决策
                    vec,  # 特征向量
                    case_id=case_ids[i + j],  # 案例 ID
                    mmsi=mmsi_list[i + j],  # MMSI 号
                )
                decisions.append(dec)  # 添加到结果列表

        return decisions  # 返回所有决策结果

    def update_model(self, new_model: TriageModel) -> None:
        """热更新 ML 模型（不中断服务）。"""
        self.model = new_model  # 替换模型引用