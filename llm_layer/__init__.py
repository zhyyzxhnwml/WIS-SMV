"""
llm_layer — LLM 分层处理层（三级级联第三级）
==============================================

【这个包做什么？】
  根据 ML Triage 的路由结果选择不同深度的 LLM 处理。
  目标：在效果和成本之间取得最佳平衡。

路由决策 → Prompt 模板           → LLM 处理深度
──────────────────────────────────────────────
obvious_issue → fast_confirm      → 快速确认（1次调用，轻量摘要）
has_risk      → careful_review    → 仔细审查（1次调用，带相似案例参考）
needs_review  → needs_review      → 全面审查（1次调用，最详细的分析）
normal        → quick_verify      → 快速验证（1次调用，最轻量确认）

【设计原则】
  每种路由只调用 1 次 LLM，不做多次链式调用。
  加速器和 ML 层已经做了大量预过滤和预判断，
  LLM 只做最终的"确认/否决"和"深度解释"。
"""

from .handler import LLMHandler  # LLM调用处理器类
from .prompts import PROMPT_TEMPLATES  # Prompt模板字典（四级处理深度）

__all__ = [  # 模块对外暴露的公共接口列表
    "LLMHandler",  # LLM调用处理器类
    "PROMPT_TEMPLATES",  # Prompt模板字典（四级处理深度）
]
