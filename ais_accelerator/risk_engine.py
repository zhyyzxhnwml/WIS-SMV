"""
ais_accelerator/risk_engine.py — 风险等级确定性分级引擎
==========================================================

【为什么存在？】
  旧链路里 risk_level 由 LLM 自由输出（无标准、拍脑袋），且
  LLM 不可用时一律返回 'low'（把"系统坏了"伪装成"低风险正常"）。
  这里用一条确定性加权评分引擎替代 LLM 的主观判定：
    - LLM 的 risk_level 降级为"参考意见"，不一致时以规则引擎为准
    - 评分逻辑、阈值全部写在数据（violation_catalog.CANONICAL）里，可解释

【评分模型】
  总分 0~100 = 违规类型基础分(catalog.base_risk, 0~90)
             + 证据充分度归一化加分（<=15）
             + 命中法规锚定条款（+15，有 REG_ANCHORS）
             - AIS质量差扣分（poor: -10）
             + LLM 与规则结论一致加分（+5）
  阈值：>=70 high / 40-69 mid / <40 low

【用法】
  from ais_accelerator.risk_engine import assess_risk
  result = assess_risk(violation_code="speed_violation",
                       evidence_count=8, duration_minutes=12.5,
                       has_regulation=True, quality_level="good",
                       llm_risk="mid")
  # result = {"score": 72, "level": "high", "reasons": [...]}
"""

from __future__ import annotations

from typing import Any, Optional

from .violation_catalog import CANONICAL, REG_ANCHORS

# ── 阈值 ─────────────────────────────────────────────────────────
THRESHOLDS = {"high": 70, "mid": 40}

# ── 证据充分度归一化参数 ─────────────────────────────────────────
EVIDENCE_MAX_POINTS = 10
EVIDENCE_MAX_MINUTES = 30.0
EVIDENCE_MAX_SCORE = 15

# 法规命中加分
REGULATION_HIT_SCORE = 15

# AIS 质量扣分
QUALITY_PENALTY = {"poor": 10, "fair": 4, "good": 0, "": 0}

# LLM 一致性加分
LLM_AGREE_SCORE = 5


def _normalize_level(level: Optional[str]) -> str:
    s = (level or "").strip().lower()
    if s in ("high", "h"):
        return "high"
    if s in ("mid", "middle", "medium", "m"):
        return "mid"
    if s in ("low", "l"):
        return "low"
    return "low"


def _score_to_level(score: int) -> str:
    if score >= THRESHOLDS["high"]:
        return "high"
    if score >= THRESHOLDS["mid"]:
        return "mid"
    return "low"


def assess_risk(
    violation_code: str,
    evidence_count: int = 0,
    duration_minutes: float = 0.0,
    has_regulation: bool = False,
    quality_level: str = "good",
    llm_risk: Optional[str] = None,
    extra_penalty: int = 0,
    extra_bonus: int = 0,
) -> dict[str, Any]:
    meta = CANONICAL.get(violation_code, CANONICAL.get("unknown", {}))
    reasons: list[str] = []
    score = 0

    base = int(meta.get("base_risk", 30))
    score += base
    label = meta.get("label", violation_code)
    reasons.append(f"{label}(基础+{base})")

    ev_by_points = min(evidence_count / EVIDENCE_MAX_POINTS, 1.0)
    ev_by_duration = min(duration_minutes / EVIDENCE_MAX_MINUTES, 1.0)
    ev_score = int(max(ev_by_points, ev_by_duration) * EVIDENCE_MAX_SCORE)
    if ev_score > 0:
        score += ev_score
        reasons.append(f"证据充分(+{ev_score})")

    if has_regulation or (violation_code in REG_ANCHORS and REG_ANCHORS[violation_code]):
        score += REGULATION_HIT_SCORE
        reasons.append(f"命中法规(+{REGULATION_HIT_SCORE})")

    q_penalty = QUALITY_PENALTY.get((quality_level or "").strip().lower(), 0)
    if q_penalty:
        score -= q_penalty
        reasons.append(f"AIS质量{quality_level}(-{q_penalty})")

    if extra_penalty:
        score -= extra_penalty
    if extra_bonus:
        score += extra_bonus
        reasons.append(f"额外加分(+{extra_bonus})")

    score = max(0, min(100, score))
    level = _score_to_level(score)

    llm_agreed = None
    if llm_risk:
        llm_norm = _normalize_level(llm_risk)
        llm_agreed = (llm_norm == level)
        if llm_agreed:
            reasons.append("与LLM判定一致")
        else:
            reasons.append(f"LLM判{llm_norm}，引擎修正为{level}")

    return {"score": score, "level": level, "reasons": reasons, "llm_agreed": llm_agreed}


def risk_label(level: Optional[str]) -> str:
    n = _normalize_level(level)
    return {"high": "高风险", "mid": "中风险", "low": "低风险"}.get(n, "低风险")


def normalize_risk_level(level: Optional[str]) -> str:
    return _normalize_level(level)
