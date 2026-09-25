"""
llm_layer/prompts.py — LLM 分层处理的 Prompt 模板集合
=======================================================

【这个文件做什么？】
  定义四级处理深度的 Prompt 模板，对应 ML Triage 的四个路由结果。

  路由 → Prompt 模板映射:
    obvious_issue → fast_confirm:   数据明显有问题，LLM 快速确认系统预判
    has_risk      → careful_review: 有风险，需仔细审查 + 参考相似案例
    needs_review  → needs_review:   无法自动判定，LLM 全面审查
    normal        → quick_verify:   看起来正常，快速验证确认

【模板设计原则】
  1. 每个模板明确告诉 LLM：
     - 它是什么角色（船舶违规检测专家）
     - 系统已经做了什么（预判/标记/规则）
     - 它需要做什么（确认/审查/验证）
     - 输出格式要求（严格 JSON）

  2. 自动花括号转义：
    Python 的 str.format() 用 {} 做占位符，JSON 也用 {}。
    _fmt() 函数自动处理这个冲突 —— 把 JSON 示例的 {} 转成 {}，
    只保留真正的变量占位符（如 {event_summary}）。

【反馈收集】
  每个模板都要求 LLM 输出 feedback_for_ml 字段：
  - markings_were_helpful: 加速器的标记是否有用
  - missed_issues: 漏检的问题
  - false_positives: 误报的标记
  这些反馈会回流到 ML 训练集中。
"""

# ── 1. 快速确认（明显问题） ───────────────────────────────────────

import re  # 正则表达式模块（用于花括号转义）

def _fmt(t: str) -> str:
    """把模板中 JSON 示例的 {} → {}，保留 {变量} 占位符"""
    vars = {}  # 存储占位符变量名及其原始字符串的字典
    def _save(m):  # 回调函数：保存匹配到的占位符并替换为临时标记
        k = f"\x00{len(vars)}\x00"  # 用空字符包围的索引作为临时标记（\x00保证不会和正常文本冲突）
        vars[k] = m.group(0)  # 保存占位符原始字符串（如"{event_summary}"）
        return k  # 返回临时标记
    t = re.sub(r'\{[a-zA-Z_][a-zA-Z0-9_]*\}', _save, t)  # 将所有 {变量名} 替换为临时标记
    t = t.replace("{", "{{").replace("}", "}}")  # 将剩余的 { } 转义为 { }
    for k, v in vars.items():  # 遍历所有保存的占位符
        t = t.replace(k, v)  # 将临时标记还原为原始占位符
    return t  # 返回转义后的模板文本


# ── 风险等级分级标准（注入所有模板，让 LLM 输出与确定性引擎对齐） ──
# 注意：最终落库的 risk_level 由 ais_accelerator/risk_engine.py 确定性引擎裁定，
# LLM 的 risk_level 仅作为参考意见参与评分（一致性加分项）。
RISK_GRADING_STANDARD = """

【风险等级分级标准（risk_level 必须严格按此标准判定，不要主观发挥）】
- high（高风险）：仅限以下情形——进入禁航/限制区域；构成碰撞危险（DCPA<0.5海里且TCPA<12分钟）；禁渔区内违规捕捞且证据点充分；区域超速且最高速超过限速20%以上；AIS长时间（>2小时）蓄意关闭伴随逃避监管特征。
- middle（中风险）：其余一切违规情形——AIS信号异常/缺失、位置跳变、航向异常、超速未超限速20%、低速捕捞嫌疑、证据不足的疑似违规行为。
- low（低风险）：未检测到违规（violation_detected=false 时固定输出 low）。
判定优先级：违规类型危害性 > 证据充分度 > 持续时长。证据不足时不得判 high，宁可判 middle。
"""


FAST_CONFIRM_PROMPT = _fmt("""你是船舶违规检测专家。系统预处理已经标记出明显的数据问题，请快速确认。

【系统预判】
加速器检测到 {event_summary}
标记置信度: {annotation_confidence}
命中规则: {rule_hits}

【相关法规】
{regulation_context}

【数据质量】
{quality_summary}

【完整轨迹数据】
以下是从 {start_time} 到 {end_time} 的完整轨迹（{point_count} 个AIS点）：
{track_data}

【标记明细】
{annotations}

【任务】
1. 快速确认系统预判是否准确
2. 如果准确 → 确认违规类型和风险等级，引用具体法规条款
3. 如果不准确 → 说明为什么预判有误

【风险等级判定标准】（供参考，系统会用确定性引擎校准）
- high(≥70分)：进入禁航/限制区域、碰撞风险、违规捕捞、超速且超限速20%以上、持续30分钟以上
- mid(40-69分)：超速未达20%、AIS信号异常/缺失、航向异常、位置跳变
- low(<40分)：低速捕捞嫌疑、证据不足、疑似误报

【输出要求 - 严格按 JSON 格式】
```json
{
  "violation_detected": true 或 false,
  "violation_type": "违规代码",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/middle/low",
  "agrees_with_triage": true/false,
  "disagreement_reason": "如果不一致，说明原因",
  "final_assessment": "简要判定说明（1-2句话）",
  "recommended_action": "auto_alert/manual_review/monitor",
  "feedback_for_ml": {
    "markings_were_helpful": true/false,
    "missed_issues": [],
    "false_positives": []
  }
}
```
""") + RISK_GRADING_STANDARD  # 快速确认Prompt模板（对应 obvious_issue 路由）

# ── 2. 仔细审查（有风险） ─────────────────────────────────────────

CAREFUL_REVIEW_PROMPT = _fmt("""你是船舶违规检测专家。系统标记这批数据存在一定风险，但置信度不足需要你仔细审查。

【系统预判】
加速器检测到: {event_summary}
标记置信度: {annotation_confidence}

【相关法规】
{regulation_context}

【数据质量】
{quality_summary}

【历史相似案例】
以下 {similar_count} 个历史案例的特征与本案例相似，供参考：
{similar_cases}

【完整轨迹数据】
以下是从 {start_time} 到 {end_time} 的完整轨迹（{point_count} 个AIS点）：
{track_data}

【标记明细】
{annotations}

【审查要点】
1. 逐条分析系统标记是否准确
2. 特别关注低置信度的标记（标记明细中各条目的 confidence）
3. 在完整数据中是否发现有标记遗漏的问题
4. 历史相似案例的结论是否适用

【风险等级判定标准】（供参考，系统会用确定性引擎校准）
- high(≥70分)：进入禁航/限制区域、碰撞风险、违规捕捞、超速且超限速20%以上
- mid(40-69分)：超速未达20%、AIS信号异常/缺失、航向异常、位置跳变
- low(<40分)：低速捕捞嫌疑、证据不足、疑似误报

【输出要求 - 严格按 JSON 格式】
```json
{
  "violation_detected": true 或 false,
  "violation_type": "违规代码",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/middle/low",
  "agrees_with_triage": true/false,
  "disagreement_reason": "如果不一致，详细说明推翻预判的原因",
  "final_assessment": "综合判定说明（2-3句话，描述审查过程）",
  "recommended_action": "auto_alert/manual_review/monitor",
  "feedback_for_ml": {
    "markings_were_helpful": true/false,
    "marking_accuracy_rating": 0.0-1.0,
    "missed_issues": ["漏检的问题1", "漏检的问题2"],
    "false_positives": ["误报的标记1"]
  }
}
```
""") + RISK_GRADING_STANDARD  # 仔细审查Prompt模板（对应 has_risk 路由）

# ── 3. 全面审查（无法自动判定） ─────────────────────────────────

NEEDS_REVIEW_PROMPT = _fmt("""你是船舶违规检测专家。系统无法自动判定这批数据，需要你做全面审查。

【系统预判】
标记置信度普遍偏低（{annotation_confidence}），系统没有足够把握

【相关法规】
{regulation_context}

【数据质量】
{quality_summary}

【历史相似案例 - 特征相似】
{similar_cases}

【完整轨迹数据】
以下是从 {start_time} 到 {end_time} 的完整轨迹（{point_count} 个AIS点）：
{track_data}

【标记明细】
{annotations}

【审查要求】
1. 系统置信度低的原因可能是数据质量差或特征不明显，请参考【数据质量】
2. 请仔细检查完整轨迹，自行判断是否存在违规
3. 不要被系统标记带偏——它们可能完全不准确
4. 参考历史相似案例但不能依赖它们

【输出要求 - 严格按 JSON 格式】
```json
{
  "violation_detected": true 或 false,
  "violation_type": "违规代码",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/middle/low",
  "agrees_with_triage": true/false,
  "disagreement_reason": "说明推翻/确认预判的原因",
  "final_assessment": "综合判定说明（3-5句话，包含对不确定因素的分析）",
  "recommended_action": "auto_alert/manual_review/monitor",
  "feedback_for_ml": {
    "markings_were_helpful": true/false,
    "marking_accuracy_rating": 0.0-1.0,
    "data_quality_issues": ["影响判断的数据问题"],
    "would_benefit_from": ["能帮助更好判断的额外信息"]
  }
}
```
""") + RISK_GRADING_STANDARD  # 全面审查Prompt模板（对应 needs_review 路由）

# ── 4. 快速验证（正常） ─────────────────────────────────────────

QUICK_VERIFY_PROMPT = _fmt("""你是船舶违规检测专家。系统判定这批数据正常，请快速验证确认。

【系统预判】
未检测到明显异常（置信度: {annotation_confidence}）

【相关法规】
{regulation_context}

【数据质量】
{quality_summary}

【轨迹概览】
时间范围: {start_time} → {end_time}
轨迹点数: {point_count}
是否有标记: {has_annotations}

【任务】
快速扫描轨迹数据，确认是否确实无异常。

【输出要求 - 严格按 JSON 格式】
```json
{
  "violation_detected": false,
  "violation_type": null,
  "violation_label": null,
  "confidence": 0.0-1.0,
  "risk_level": "low",
  "agrees_with_triage": true,
  "disagreement_reason": null,
  "final_assessment": "简要确认说明",
  "recommended_action": "monitor",
  "feedback_for_ml": {
    "verification_confidence": 0.0-1.0
  }
}
```
""") + RISK_GRADING_STANDARD  # 快速验证Prompt模板（对应 normal 路由）


PROMPT_TEMPLATES = {  # 四级Prompt模板字典（路由名称→模板文本）
    "fast_confirm": FAST_CONFIRM_PROMPT,  # 快速确认（明显问题）
    "careful_review": CAREFUL_REVIEW_PROMPT,  # 仔细审查（有风险）
    "needs_review": NEEDS_REVIEW_PROMPT,  # 全面审查（无法自动判定）
    "quick_verify": QUICK_VERIFY_PROMPT,  # 快速验证（正常）
}
