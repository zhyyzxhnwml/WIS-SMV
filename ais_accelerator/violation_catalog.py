"""
ais_accelerator/violation_catalog.py — 违规类型规范化目录（二级分类版）
========================================================================

【为什么存在？】
  检测链路上违规类型来自两处且都不规范：
    1. L1 加速器事件 → CandidateType（7 个规范码，如 ais_gap）
    2. LLM 自由输出 violation_type → 会编造出 AIS01 / AIS012 / AIS0104 /
       AIS Course Deviation / 0951 等杂乱代码，同一个意思几十种写法。
  而法规检索（search_by_violation）只认识 5 个代码，对不上的就查空 →
  DB 里 63% 违规记录 regulation_ref 为空，前端只能显示一串英文码。

【本模块做什么？】
  1. normalize_violation_code(raw)  → 把任意原始代码归一为规范码（幂等）
  2. describe(raw)                  → 返回 {code, label, meaning, penalty} 供展示
  3. describe_full(raw)             → 返回 {code, label, meaning, penalty,
                                       category, evidence_fields, base_risk} 供前端下钻
  4. build_violation_event(...)     → 生成"检测出xxx，与《法规》第X条相关度x%"
  5. FALLBACK_REG                   → 规范码→法规兜底文本（检索失败时保证有引用）
  6. list_catalog()                 → 返回二级目录（类目→类型列表，供前端下钻）
  7. list_categories()              → 一级类目列表（供饼图/筛选）
"""

from __future__ import annotations

from typing import Any, Optional

# ── 一级类目 ───────────────────────────────────────────────────────
# name: 类目标识 / label: 中文标签 / order: 显示排序
CATEGORIES: list[dict[str, Any]] = [
    {"name": "ais_device", "label": "AIS设备类", "order": 1},
    {"name": "navigation", "label": "航行行为类", "order": 2},
    {"name": "zone_control", "label": "区域管控类", "order": 3},
    {"name": "fishing", "label": "渔业作业类", "order": 4},
    {"name": "collision", "label": "避碰安全类", "order": 5},
    {"name": "data_quality", "label": "数据质量类", "order": 6},
    {"name": "other", "label": "其他", "order": 99},
]

CATEGORY_MAP: dict[str, str] = {c["name"]: c["label"] for c in CATEGORIES}

# ── 规范违规类型目录（二级） ────────────────────────────────────────
# code: 规范码（与 CandidateType 及法规库 violation_codes 对齐）
# label: 中文标签（前端主展示）
# meaning: 含义解释（"这是什么违规"）
# penalty: 常见罚则（展示用，具体以法规原文为准）
# category: 所属一级类目
# evidence_fields: 该类型需要的证据指标（供前端定制证据图）
# base_risk: 风险等级基础分（0~100，供 risk_engine 使用）
CANONICAL: dict[str, dict[str, Any]] = {
    "ais_off": {
        "label": "AIS信号异常/关闭",
        "meaning": "船舶未按规定持续开启并正确发送AIS信号，出现信号间隔过长或主动关闭，涉嫌逃避监管。",
        "penalty": "责令改正，处二千元以上二万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "ais_device",
        "evidence_fields": ["ais_gap", "signal_interval"],
        "base_risk": 55,
    },
    "ais_gap": {
        "label": "AIS信号缺失",
        "meaning": "船舶AIS信号出现缺失或中断，未保持自动识别系统连续正常发射。",
        "penalty": "责令改正，处二千元以上二万元以下罚款。",
        "category": "ais_device",
        "evidence_fields": ["ais_gap", "signal_interval"],
        "base_risk": 50,
    },
    "course_anomaly": {
        "label": "航向异常/偏离航线",
        "meaning": "船舶航向发生突变或大幅偏离计划航线，可能存在违规转向或航行异常。",
        "penalty": "责令改正，情节严重的暂扣或吊销船员适任证书。",
        "category": "navigation",
        "evidence_fields": ["course_anomaly", "track"],
        "base_risk": 40,
    },
    "speed_violation": {
        "label": "超速/限速违规",
        "meaning": "船舶在限速区域（如港口航道、特殊水域）超过规定航速行驶。",
        "penalty": "责令改正，处一千元以上一万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "navigation",
        "evidence_fields": ["speed_violation", "speed_time_curve"],
        "base_risk": 60,
    },
    "restricted_area_entry": {
        "label": "进入禁航/限制区域",
        "meaning": "船舶进入军事禁区、禁止进入区域或其他限制水域。",
        "penalty": "责令改正，处一万元以上十万元以下罚款；情节严重构成犯罪的，依法追究刑事责任。",
        "category": "zone_control",
        "evidence_fields": ["restricted_area_entry", "zone_track"],
        "base_risk": 90,
    },
    "illegal_fishing": {
        "label": "违规捕捞",
        "meaning": "在禁渔区、禁渔期进行捕捞作业，或从事非法渔业活动。",
        "penalty": "没收渔获物和违法所得，处五万元以下罚款。",
        "category": "fishing",
        "evidence_fields": ["low_speed_fishing", "zone_track"],
        "base_risk": 70,
    },
    "low_speed_fishing": {
        "label": "低速捕捞嫌疑",
        "meaning": "船舶以异常低速航行，速度特征符合捕捞作业，存在违规捕捞嫌疑。",
        "penalty": "责令改正，没收渔获物和违法所得。",
        "category": "fishing",
        "evidence_fields": ["low_speed_fishing", "speed_time_curve"],
        "base_risk": 35,
    },
    "position_jump": {
        "label": "位置跳变异常",
        "meaning": "AIS位置点发生明显跳变，与船舶实际运动不符，可能存在信号造假或设备异常。",
        "penalty": "责令改正，处二千元以上二万元以下罚款。",
        "category": "data_quality",
        "evidence_fields": ["position_jump", "track"],
        "base_risk": 45,
    },
    "collision_risk": {
        "label": "碰撞风险",
        "meaning": "与他船构成碰撞危险，未遵守《国际海上避碰规则》，存在碰撞风险。",
        "penalty": "责令改正，处一万元以上十万元以下罚款。",
        "category": "collision",
        "evidence_fields": ["collision_risk", "track"],
        "base_risk": 80,
    },
    "unknown": {
        "label": "其他违规",
        "meaning": "未归类到已知违规类型，具体行为以违规描述和法规引用为准。",
        "penalty": "",
        "category": "other",
        "evidence_fields": [],
        "base_risk": 30,
    },
}

# ── 锚定法规条款（人工核对过，存在于法规库 collection 中的"正确条款"） ──
# 纯向量检索排序不可靠（会召回无关条款），锚定表保证"这个违规 → 这条法规"
# 是准的。每个规范码给 1~2 条：(source, article_number)。
REG_ANCHORS: dict[str, list[tuple[str, str]]] = {
    "ais_off": [("中华人民共和国海上交通安全法2021", "36")],
    "ais_gap": [("中华人民共和国海上交通安全法2021", "36")],
    "position_jump": [("中华人民共和国海上交通安全法2021", "36")],
    "speed_violation": [("中华人民共和国海上交通安全法2021", "43")],
    "course_anomaly": [("中华人民共和国海上交通安全法2021", "43")],
    "restricted_area_entry": [("中华人民共和国海上交通安全法2021", "44")],
    "collision_risk": [("COLREG", "14"), ("COLREG", "15")],
    "illegal_fishing": [
        ("中华人民共和国渔港水域交通安全管理条例2019", "10"),
        ("中华人民共和国渔港水域交通安全管理条例2019", "21"),
    ],
    "low_speed_fishing": [("中华人民共和国渔港水域交通安全管理条例2019", "10")],
    "unknown": [],
}


# ── 法规兜底文本（检索失败时的保底引用） ─────────────────────────
FALLBACK_REG = {
    "ais_off": "《中华人民共和国海上交通安全法》关于船舶配备并正常使用自动识别系统（AIS）设备、保持航行信号连续的规定",
    "ais_gap": "《中华人民共和国海上交通安全法》关于船舶保持自动识别系统（AIS）连续正常发射的规定",
    "course_anomaly": "《国际海上避碰规则》（COLREG）关于船舶安全航行与转向操纵的规定",
    "speed_violation": "《中华人民共和国海上交通安全法》关于船舶在限制水域按限速规定航行的规定",
    "restricted_area_entry": "《中华人民共和国海上交通安全法》关于未经批准不得进入禁航区、军事管制区等限制水域的规定",
    "illegal_fishing": "《中华人民共和国渔业法》关于不得在禁渔区、禁渔期捕捞作业的规定",
    "low_speed_fishing": "《中华人民共和国渔业法》关于不得在禁渔区、禁渔期捕捞作业的规定",
    "position_jump": "《中华人民共和国海上交通安全法》关于船舶AIS设备正常使用、不得虚报瞒报动态信息的规定",
    "collision_risk": "《国际海上避碰规则》（COLREG）关于船舶避碰责任与让路行动的规定",
    "unknown": "《中华人民共和国海上交通安全法》",
}

# ── LLM 自由代码 → 规范码映射 ────────────────────────────────────
# 键：小写、空格/连字符/点 → 下划线 之后的形态（由 _norm_key 统一处理）
# 注意：ais_gap 与 low_speed_fishing 现在是独立类型，不再并入 ais_off / illegal_fishing
_CODE_MAP = {
    # AIS 信号异常/关闭（主动关闭 AIS，有主观逃避监管意图）
    "ais_off": "ais_off",
    "ais01": "ais_off", "ais010": "ais_off", "ais012": "ais_off",
    "ais013": "ais_off", "ais015": "ais_off", "ais0104": "ais_off",
    "ais0102": "ais_off", "ais0103": "ais_off", "ais0105": "ais_off",
    "ais02": "ais_off", "ais03": "ais_off", "ais04": "ais_off",
    "ais05": "ais_off", "ais06": "ais_off", "ais07": "ais_off",
    "ais08": "ais_off", "ais09": "ais_off", "ais10": "ais_off",
    "ais11": "ais_off", "ais12": "ais_off", "ais13": "ais_off",
    "ais14": "ais_off", "ais15": "ais_off", "ais16": "ais_off",
    "ais17": "ais_off", "ais18": "ais_off", "ais19": "ais_off",
    "ais20": "ais_off", "ais0201": "ais_off", "ais002": "ais_off",
    "ais021": "ais_off", "ais023": "ais_off", "ais_01": "ais_off",
    "ais_02": "ais_off", "ais_04": "ais_off", "ais_010": "ais_off",
    "ais_012": "ais_off", "ais_ais_01": "ais_off", "ais12345": "ais_off",
    "ais_close": "ais_off", "ais_absence": "ais_off",
    "ais_anomaly": "ais_off", "ais_anomaly_close": "ais_off",
    "ais_anomaly_off": "ais_off", "ais_signal_off": "ais_off",
    "ais_off_signal": "ais_off",
    "ais异常关闭": "ais_off", "ais信号异常": "ais_off", "ais关闭": "ais_off",
    # AIS 信号缺失（信号中断/丢失，客观通信问题，区别于主动关闭）
    "ais_gap": "ais_gap",
    "ais_gap_long": "ais_gap",
    "ais_signal_lost": "ais_gap", "ais信号丢失": "ais_gap", "ais信号缺失": "ais_gap",
    "ais中断": "ais_gap",
    # 航向异常
    "course_anomaly": "course_anomaly",
    "ais_course_deviation": "course_anomaly",
    "ais_course_anomaly": "course_anomaly",
    "course_deviation": "course_anomaly",
    "track_deviation": "course_anomaly",
    "航线偏离": "course_anomaly", "航向异常": "course_anomaly",
    # 超速
    "speed_violation": "speed_violation",
    "over_speed": "speed_violation", "超速": "speed_violation",
    # 区域管控
    "restricted_area_entry": "restricted_area_entry",
    "进入禁航区": "restricted_area_entry", "进入限制区域": "restricted_area_entry",
    # 违规捕捞
    "illegal_fishing": "illegal_fishing", "违规捕捞": "illegal_fishing",
    # 低速捕捞嫌疑（独立类型）
    "low_speed_fishing": "low_speed_fishing",
    "低速捕捞": "low_speed_fishing",
    # 其它规范码
    "position_jump": "position_jump",
    "collision_risk": "collision_risk",
}


def _norm_key(raw: str) -> str:
    """把原始代码统一成匹配键：小写、空白/连字符/点 → 下划线。"""
    s = (raw or "").strip().lower()
    for ch in (" ", "-", ".", "／", "/"):
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s


def normalize_violation_code(raw: Optional[str]) -> str:
    """把任意原始代码归一为规范码（幂等）。无法识别时返回 'unknown'。"""
    if not raw or not str(raw).strip():
        return "unknown"
    key = _norm_key(raw)
    if key in _CODE_MAP:
        return _CODE_MAP[key]
    if key in CANONICAL:
        return key
    return "unknown"


def describe(raw: Optional[str]) -> dict[str, str]:
    """返回违规类型的中文展示信息（兼容旧接口）。

    Args:
        raw: 原始违规类型代码（LLM 输出 / DB 存储值）

    Returns:
        {code, label, meaning, penalty}
    """
    code = normalize_violation_code(raw)
    meta = CANONICAL.get(code, CANONICAL["unknown"])
    return {
        "code": code,
        "label": meta["label"],
        "meaning": meta["meaning"],
        "penalty": meta["penalty"],
    }


def describe_full(raw: Optional[str]) -> dict[str, Any]:
    """返回违规类型的完整信息（含类目、证据字段、风险基础分）。

    Args:
        raw: 原始违规类型代码

    Returns:
        {code, label, meaning, penalty, category, category_label,
         evidence_fields, base_risk}
    """
    code = normalize_violation_code(raw)
    meta = CANONICAL.get(code, CANONICAL["unknown"])
    cat = meta.get("category", "other")
    return {
        "code": code,
        "label": meta["label"],
        "meaning": meta["meaning"],
        "penalty": meta["penalty"],
        "category": cat,
        "category_label": CATEGORY_MAP.get(cat, "其他"),
        "evidence_fields": meta.get("evidence_fields", []),
        "base_risk": meta.get("base_risk", 30),
    }


def list_categories() -> list[dict[str, Any]]:
    """返回一级类目列表。"""
    return [dict(c) for c in sorted(CATEGORIES, key=lambda x: x["order"])]


def list_catalog() -> dict[str, list[dict[str, Any]]]:
    """返回二级目录：类目 → 类型列表。

    Returns:
        {
            "categories": [{"name", "label", "order"}],
            "types": [{"code", "label", "meaning", "penalty", "category",
                       "category_label", "evidence_fields", "base_risk"}]
        }
    """
    types = []
    for code, meta in CANONICAL.items():
        cat = meta.get("category", "other")
        types.append({
            "code": code,
            "label": meta["label"],
            "meaning": meta["meaning"],
            "penalty": meta["penalty"],
            "category": cat,
            "category_label": CATEGORY_MAP.get(cat, "其他"),
            "evidence_fields": meta.get("evidence_fields", []),
            "base_risk": meta.get("base_risk", 30),
        })
    return {
        "categories": list_categories(),
        "types": types,
    }


def build_violation_event(
    raw_type: Optional[str],
    final_assessment: str = "",
    regulation_ref: str = "",
    best_article: Optional[dict[str, Any]] = None,
) -> str:
    """生成可读的违规事件描述，形如：

        "检测出AIS信号异常/关闭，与《中华人民共和国海上交通安全法2021》第35条（…）相关度约87%"

    Args:
        raw_type: 原始违规类型代码
        final_assessment: LLM 判定说明（作为行为补充）
        regulation_ref: 已生成的法规引用文本（优先用它的表述）
        best_article: 最相关法规条目（含 source/article_number/title/similarity）

    Returns:
        拼装好的中文描述
    """
    info = describe(raw_type)
    label = info["label"]

    parts = [f"检测出{label}"]
    if best_article and best_article.get("source"):
        src = best_article.get("source", "")
        num = best_article.get("article_number", "")
        title = (best_article.get("title") or "").strip()
        sim = best_article.get("similarity")
        seg = f"与《{src}》"
        if num and num != "0":
            seg += f"第{num}条"
        if title:
            seg += f"（{title[:40]}）"
        if sim is not None:
            seg += f"相关度约{sim:.0%}"
        parts.append(seg)
    elif regulation_ref:
        parts.append(f"涉及{regulation_ref}")

    body = "，".join(parts)
    if final_assessment:
        body = body.rstrip("。") + "。" + final_assessment.rstrip("。")
    return body + "。"
