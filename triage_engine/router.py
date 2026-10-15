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

# ── LLM 门控：按"非正常三类的最大概率"而不是 ml_confidence ──────────────
#
# 【为什么不用 ml_confidence】
#   ml_confidence 是**最大类概率**，它回答的是"模型对自己这个判断有多确信"，
#   不是"这条船有多可能出事"。在 HawaiiCoast_GT（4186 船-日 / 48 起事故）上实测：
#       · -ml_confidence 与"这条船真出没出事故" **反相关**：AUC 0.404（低于随机）
#       · max(p_has_risk, p_obvious_issue, p_needs_review)：AUC 0.592，正相关
#   按前者挑船 = 专门挑错船。旧模型（合成数据训练的 triage_model.txt）把这个
#   毛病放大了：84% 的船给出 0.997 置信度、has_risk 一次都没输出过，
#   3777/4186 全塞进 obvious_issue，于是门控等于随机。
#
# 【为什么是"最大"而不是"求和"】
#   三类概率和 p_sum 在实测里高度饱和（p50=p95=0.999），排序靠并列、
#   结果随并列打破方式抖动；取最大值 pmax 有 2354 个不同取值，排序是确定的。
#
# 【阈值怎么定】
#   按 **LLM 预算不变** 标定：让本规则在全量数据上的调用率与改动前持平，
#   于是"同样花这些 LLM 调用，捞到多少真事故"才是可比的。
#   同预算下实测：宽区间召回 18.8%→31.2%（lift 1.13→1.88），
#   精确锚点 7.7%→38.5%（lift 0.46→2.32）。
NON_NORMAL_LABELS = ("has_risk", "obvious_issue", "needs_review")

# 非正常三类最大概率 ≥ 此值才送 LLM。
#
# 【2026-10 重标定：0.985 → 0.000880】
#   0.985 是给**旧模型**定的：那个模型（合成数据训练）在真数据上 100/107 条判
#   obvious_issue、风险分挤在 0.995~0.997（107 条里只有 27 个不同取值），
#   任何阈值都约等于"全放行"——接线实测只跳过 15.9%，而且跳过的恰好是它认为
#   最正常的那批。现役 triage_model.txt 已换成用真值重训 + 23 维窗口特征的模型，
#   分数分布完全变了：p50=0.0001 / p90=0.00088 / max=0.9943（135 个不同取值）。
#
#   新阈值的依据分两段，各取所需（见 data/logs/pick_gate.py）：
#     ① 调用率选 10% —— 用**留一天 OOF** 分数画曲线（`diag_gate_ci.py`），
#        因为模型在真值上训过，同一批船的分数是记忆，据此选阈值等于挑它记住了哪几天。
#        10% 那一档：宽区间 lift 3.34 [2.26, 4.45]、精确区间 lift 3.05 [1.33, 4.91]
#        （按天自助 2000 次）。两个真值口径都站得住，召回 33%/30%，
#        LLM 调用量只有现役（92.6% 报警率）的 1/9。
#        2% 档 lift 6.05 但自助区间 [2.72, 9.63]，精确区间口径下区间含 0 —— 小样本极值，不取。
#     ② 阈值数值 = 部署模型风险分分布上的 1-10% 分位 = 0.000880（实测调用率 10.0%）。
#        这一步只用部署模型：它要回答的只是"这条分数线上有多少船"，与泛化无关。
#
#   ⚠ 换模型必须重标定：阈值是**分数分布**的分位，不是绝对质量。重训后跑
#     `python data/logs/pick_gate.py`（它按目标调用率反算阈值）。
RISK_GATE_THRESHOLD = 0.000880


@dataclass
class RoutingDecision:
    """路由决策的数据类，记录完整决策链信息。"""

    case_id: str = ""  # 案例唯一标识
    mmsi: str = ""  # 船舶 MMSI 号

    ml_category: str = ""  # ML 预测类别
    ml_confidence: float = 0.0  # ML 预测置信度
    risk_score: float = 0.0  # 非正常三类的最大概率（LLM 门控用的就是这个）

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
        """转为字典（便于序列化）。

        `risk_score` 与 `all_probs` 必须在这里，不能只在 dataclass 上：LLM 那条路
        拿的是 **本方法的返回值**（orchestrator `routing_dict = routing_decision.to_dict()`
        → `LLMHandler.analyze(routing=...)`）。原先这两项不在返回值里，于是
        L3 从来不知道 L2 的风险分有多少、自己为什么被门控叫来 —— 只能从轨迹
        形状猜，而那是最弱的一条线索。落库/前端也多一份可核对的决策依据。
        """
        return {  # 返回所有字段的字典表示
            "case_id": self.case_id,  # 案例 ID
            "mmsi": self.mmsi,  # MMSI 号
            "ml_category": self.ml_category,  # ML 预测类别
            "ml_confidence": self.ml_confidence,  # ML 置信度
            "risk_score": self.risk_score,  # 非正常三类的最大概率（门控依据）
            "all_probs": dict(self.all_probs),  # 四类完整概率分布
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
        n_events: Optional[int] = None,  # L1 报出的候选事件条数（见下方门控说明）
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
        # 风险分：非正常三类的最大概率。见文件头 RISK_GATE_THRESHOLD 的实测依据。
        _probs = decision.all_probs
        decision.risk_score = max(
            (float(_probs.get(k, 0.0) or 0.0) for k in NON_NORMAL_LABELS), default=0.0)

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

        # == 第 2b 步：LLM 门控覆盖上面按置信度的阶梯判定 ==
        # 阶梯仍然决定 routing 标签与是否需要相似检索（下游语义不变），
        # 但"要不要花 LLM"这个决定由这里出。
        #
        # 【判据一（优先）：L1 的证据强度】调用方给了 n_events 就用它。
        #   实测（注入集 240 船，零额外 LLM 成本，见 experiments/paper/gate_design.py）：
        #                完整规则      摘掉让路规则   摘掉追越规则
        #   风险分判据   64.3%@240调   64.3%@240     64.3%@240
        #   证据强度判据 **100%@65调**  **67.4%@78**  **79.4%@106**
        #   三个场景**全部**同时赢成本与质量。理由：门控真正该问的是
        #   "规则层对这条船有没有把握"，而 risk_score 学的是"这条轨迹有多少个点"。
        #   规则一条候选事件都没报 → 那正是 L1 的能力边界（规则盲区），
        #   此时才值得让 LLM 看第二眼；报了事件说明规则已经看过并给出了判断。
        #
        # 【判据二（回退）：风险分】调用方没给 n_events 时保持原行为，兼容旧调用。
        if n_events is not None:
            decision.needs_llm = int(n_events) == 0
        else:
            decision.needs_llm = decision.risk_score >= RISK_GATE_THRESHOLD

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