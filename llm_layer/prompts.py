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
- mid（中风险）：其余一切违规情形——AIS信号异常/缺失、位置跳变、航向异常、超速未超限速20%、低速捕捞嫌疑、证据不足的疑似违规行为。
- low（低风险）：未检测到违规（violation_detected=false 时固定输出 low）。
判定优先级：违规类型危害性 > 证据充分度 > 持续时长。证据不足时不得判 high，宁可判 mid。
注意：risk_level 的词表是 {{high, mid, low}}。不要写 "middle"/"medium"——
"middle" 是**事件优先级**（EventPriority）的词表，两者只是恰好同名；写混会让
仪表盘的风险饼图把同一档拆成两个扇区（实测出现过 middle=7 与 mid=2 并存）。
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
  "violation_type": "必须精确取自下方词表的 code",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/mid/low",
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
  "violation_type": "必须精确取自下方词表的 code",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/mid/low",
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
  "violation_type": "必须精确取自下方词表的 code",
  "violation_label": "中文描述",
  "confidence": 0.0-1.0,
  "risk_level": "high/mid/low",
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


# ── 违规类型词表注入 ────────────────────────────────────────────────
# violation_type 原先在模板里写作 "违规代码"——一个**没有可选值**的占位符，
# 于是 LLM 只能自创。实测同一批数据里出现过 AIS01 / AIS012 / AIS1001 /
# data_quality_non_increasing_time / ais异常关闭 等十余种写法；这些脏码一路写进
# CSV，又作为弱标签回流去训 L2 的 LightGBM（llm_layer/feedback.py），
# 等于让模型去学一个并不存在的类别体系。
#
# 词表在 handler 侧以 {violation_vocab} 注入，不在此处 import 违规目录：
# ais_accelerator 的 __init__ 会把整个包拉起来（实测 2.8s），且会反转 L3→L1 的分层。
#
# 只注入到真正需要 LLM 输出违规码的三个模板：quick_verify 走 normal 路由、
# violation_type 固定为 null，给它加词表纯属给最便宜的路径加 token——
# 而成本-质量帕累托正是本文的核心指标之一。
_VOCAB_BLOCK = """
【违规类型词表（violation_type 必须精确取其中一个 code，禁止自创）】
{violation_vocab}
"""

_JSON_MARKER = "【输出要求 - 严格按 JSON 格式】"


def _inject_vocab(t: str) -> str:
    """把词表块插到 JSON 输出要求之前。

    {violation_vocab} 保持字面量交给 handler 的 .format() 填充——此处已在
    _fmt() 之后，插进来的花括号不会被转义，正是所需。
    """
    return t.replace(_JSON_MARKER, _VOCAB_BLOCK.strip() + "\n\n" + _JSON_MARKER, 1)


# ── 判性依据注入 ────────────────────────────────────────────────────
# 【为什么需要】原先四个模板给 LLM 的只有：事件**名字**（"碰撞风险 @ 某区域"）、
# 规则命中的**标签**（"违规追越"）、和一段被截到 8000 字符的轨迹 JSON ——
# 而 L1 实际测到的量值（DCPA、实测最小间距、速差、超速比例）、23 维窗口特征
# （含 min_sep_nm）、航速统计、以及 L2 的风险分，**一个都没进 prompt**。
# 于是 LLM 被要求判违规，却拿不到判据；再拿它的表现当"LLM 不行"的证据，
# 不成立。这一段把判据和判定纪律一起补回去。
#
# 放在【相关法规】之前：法规是"该怎么做"，实测是"实际做了什么"，
# 先看事实再对照规则，顺序与人的判案顺序一致。
_EVIDENCE_BLOCK = """【判性依据：上游实测（先读这一段，结论必须落到数字上）】
{triage_context}

{measured_evidence}

判定纪律（违反此条的结论视为无效）：
1. 判 violation_detected=true，必须在 final_assessment 里落到**可复核的依据**上：
   要么是某个量值越过了某条门槛（DCPA<0.5 海里且 TCPA<12 分钟、实测最小间距过小、
   追越速差≥5 节、最高速超限速 20% 以上、信号缺失>2 小时），要么是第 3 条所说的、
   能从轨迹点名到时间与位置的行为事实。两者都给不出，就不许判 true。
2. 上面列出的量值**不足以支撑**任何一条门槛时，判 violation_detected=false，
   并在 disagreement_reason 里说明缺哪项证据 —— 不要为了配合系统预判而编造量值，
   也不要因为"系统标记了"就默认违规成立。
3. **但"上游没命中"不等于"没有违规"**：L1 的硬证据清单是**有限的**（区域进入、
   追越、越界锚泊、违规捕捞、让路未让、超速这几类），它没编码的行为正是要你来看的 ——
   例如 COLREG 会遇局面与该让不让、违反分道通航制/航路规则、异常机动与徘徊、
   与他船长时间伴随或并靠、以及上面量值正常但行为整体反常的情形。
   在这些情形下，判 true 的依据是**轨迹本身可复核的事实**（时间、位置、航向/航速序列），
   在 final_assessment 里把事实写清楚。
4. 量值以本节为准；轨迹 JSON 是抽样且被截断的，看不出量值时不要靠它反推。

"""


def _inject_evidence(t: str) -> str:
    """把判性依据块插到【相关法规】之前。`{triage_context}`/`{measured_evidence}`
    同样保持字面量交给 handler 的 .format() 填充。"""
    return t.replace("\n【相关法规】", "\n" + _EVIDENCE_BLOCK + "【相关法规】", 1)


PROMPT_TEMPLATES = {  # 四级Prompt模板字典（路由名称→模板文本）
    "fast_confirm": _inject_vocab(_inject_evidence(FAST_CONFIRM_PROMPT)),  # 快速确认（明显问题）
    "careful_review": _inject_vocab(_inject_evidence(CAREFUL_REVIEW_PROMPT)),  # 仔细审查（有风险）
    "needs_review": _inject_vocab(_inject_evidence(NEEDS_REVIEW_PROMPT)),  # 全面审查（无法自动判定）
    # 快速验证（正常）：同样注入实测依据——"确认无异常"也是一个判定，
    # 同样需要数字支撑；不注入词表（它不输出违规码）。
    "quick_verify": _inject_evidence(QUICK_VERIFY_PROMPT),
}
