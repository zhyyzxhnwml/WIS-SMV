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
  4. classify(violation_type, events)→ 归类唯一入口：定规范码 + 按实测量级分档。
                                       **规范码取 L1 事件优先于 LLM 输出**（见函数注释）
  5. build_violation_event(...)     → 生成"检测出xxx，与《法规》第X条相关度x%"
  6. FALLBACK_REG                   → 规范码→法规兜底文本（检索失败时保证有引用）
  7. list_catalog()                 → 返回二级目录（类目→类型→细类，供前端下钻）
  8. list_categories()              → 一级类目列表（供饼图/筛选）
  9. list_subtypes(code)            → 某类型的静态细类定义（量级分档区间）

【三级分类口径】
  一级 category  7 个业务大类（CATEGORIES）
  二级 code      10 个规范码（CANONICAL）—— "是哪类违规"
  三级 subtype   量级分档（SUBTYPE_RULES）—— "有多严重"，如 ais_gap#suspected_off
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

# ── 量级分档规则（三级细类） ──────────────────────────────────────
# 【为什么需要这一级】
#   CANONICAL 的 10 个规范码只回答"是哪类违规"，回答不了"有多严重"。实测 48 个
#   ais_gap 事件的 gap_minutes 跨 60 ~ 1160 分钟，全标成"AIS信号缺失"就把
#   "刚过检测线"和"消失近 20 小时"混成了一类。这里按 L1 事件 features 里的
#   实测量级把类型再切开。
#
# field:  features 里取哪个字段
# unit:   单位（展示用）
# breaks: 升序断点，N 个断点切出 N+1 档
# keys:   各档的稳定英文键（拼进 subtype code，可读且不随标签改动而变）
# labels: 各档中文标签（与 breaks 对应，长度 = len(breaks)+1）
#
# 注意：breaks 的数值是**工程分档，不是法规阈值**——法规里没有"中断满 2 小时
# 算关闭"这种硬线。实测这批数据下三档都不空桶（8/10/24 条记录）；有业务口径
# 直接改 breaks 即可，keys 不变就不会影响已回填的 subtype。
SUBTYPE_RULES: dict[str, dict[str, Any]] = {
    "ais_gap": {
        "field": "gap_minutes", "unit": "分钟", "breaks": [120, 360],
        "keys": ["short", "long", "suspected_off"],
        "labels": ["短时中断", "长时中断", "疑似关闭"],
    },
    "speed_violation": {
        "field": "speed_over_ratio", "unit": "倍", "breaks": [1.2, 1.5],
        "keys": ["slight", "obvious", "severe"],
        "labels": ["轻微超速", "明显超速", "严重超速"],
    },
    "low_speed_fishing": {
        "field": "duration_minutes", "unit": "分钟", "breaks": [30, 120],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短暂滞留", "持续作业", "长时间作业"],
    },
    "course_anomaly": {
        "field": "course_change_degrees", "unit": "°", "breaks": [30, 90],
        "keys": ["slight", "obvious", "sharp"],
        "labels": ["小幅偏航", "明显偏航", "剧烈转向"],
    },
    "collision_risk": {
        "field": "cri", "unit": "", "breaks": [0.5, 0.8],
        "keys": ["low", "mid", "high"],
        "labels": ["低风险接近", "中风险接近", "高风险接近"],
    },
    "restricted_area_entry": {
        "field": "point_count", "unit": "点", "breaks": [5],
        "keys": ["brief", "sustained"],
        "labels": ["短暂越界", "持续进入"],
    },
    # ais_off / position_jump / illegal_fishing / unknown：
    # features 里没有稳定的量级字段，不分档（subtype 留空）
}

# subtype code 的分隔符："{code}#{key}"，如 "ais_gap#suspected_off"
SUBTYPE_SEP = "#"

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
    # 下面几个是实测在 DB 的 violation_type 列里真的出现过的 LLM 编造码，
    # 语义都是"AIS 信号间隔异常"，此前全部归一到 unknown（显示成"其他违规"）。
    # 注意 ais002 早在上面 AIS关闭 组里（历史上被当成"主动关闭"），这里只补缺的。
    "ais001": "ais_gap", "ais003": "ais_gap",
    "ais1001": "ais_gap", "ais1101": "ais_gap", "ais1234": "ais_gap",
    "ais信号间隔异常": "ais_gap", "ais_interval_anomaly": "ais_gap",
    "ais信号异常间隔": "ais_gap",
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
    # 实测库里出现过：LLM 把 L1 的"标注类型"当违规类型输出了。
    # 时间戳非递增属数据质量问题，归 position_jump（同属 data_quality 类目）。
    "data_quality_non_increasing_time": "position_jump",
    "data_quality_position_jump": "position_jump",
    "non_increasing_time": "position_jump",
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
    """把任意原始代码归一为规范码（幂等）。无法识别时返回 'unknown'。

    传入三级细类（如 "ais_gap#suspected_off"）时按二级规范码处理 —— 细类不是
    独立类型，调用方（看板聚合、?type= 筛选）拿到它时想要的都是"它属于哪一类"。
    """
    if not raw or not str(raw).strip():
        return "unknown"
    key = _norm_key(str(raw).split(SUBTYPE_SEP)[0])
    if key in _CODE_MAP:
        return _CODE_MAP[key]
    if key in CANONICAL:
        return key
    return "unknown"


def raw_aliases(code: str) -> list[str]:
    """返回所有会被归一化到 code 的原始写法（含 code 自身）。

    用途：把违规类型筛选**下推到 SQL**。接口层此前是在 Python 里逐行 continue，
    没有 LIMIT/OFFSET，全表取回再切片；下推后要用 IN (...) 列出该码的全部
    等价写法（LLM 历史上输出过 AIS002 / ais_off / AIS_GAP 等几十种变体）。
    """
    code = normalize_violation_code(code)
    if code == "unknown":
        return []
    return sorted({code} | {k for k, v in _CODE_MAP.items() if v == code})


def codes_of_category(category: str) -> list[str]:
    """返回某业务大类（一级类目 name）下的全部规范码。

    用途：?category= 筛选下推；同时供前端做「类目 → 类型」联动时的校验。
    """
    cat = (category or "").strip()
    return [code for code, meta in CANONICAL.items() if meta.get("category") == cat]


def code_of_label(label: str) -> str:
    """中文标签 → 规范码的反查。

    用途：?type= 筛选下推时，前端下拉可能传的是中文标签（catalog 里展示的就是
    标签）。反查不到返回 'unknown'，由调用方决定是否忽略该筛选条件。
    """
    want = (label or "").strip()
    if not want:
        return "unknown"
    for code, meta in CANONICAL.items():
        if meta.get("label") == want:
            return code
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


def _subtype_of(
    code: str, events: Optional[list[dict[str, Any]]]
) -> tuple[str, str, Optional[float]]:
    """按 SUBTYPE_RULES 给同类事件按实测量级分档，取最严重的一档。

    一条记录可能有多个同类事件（实测 id=49 有两个 ais_gap：60.2 分钟和 781.48
    分钟）。记录级只留一个细类，取最严重的那档，完整事件列表交给证据弹窗展示。

    Returns:
        (subtype_code, subtype_label, metric)；该类型不分档或无可用量级时
        返回 ("", "", None)。
    """
    rule = SUBTYPE_RULES.get(code)
    if not rule or not events:
        return "", "", None

    field, breaks = rule["field"], rule["breaks"]
    worst_idx, worst_val = -1, None
    for ev in events:
        # 只让同类事件给这个类型定档：别的类型的事件量纲不同，混进来会定错档
        if normalize_violation_code(ev.get("candidate_type") or "") != code:
            continue
        try:
            val = float((ev.get("features") or {}).get(field))
        except (TypeError, ValueError):
            continue
        idx = sum(1 for b in breaks if val > b)
        if idx > worst_idx:
            worst_idx, worst_val = idx, val

    if worst_idx < 0:
        return "", "", None
    return f"{code}{SUBTYPE_SEP}{rule['keys'][worst_idx]}", rule["labels"][worst_idx], worst_val


def classify(
    violation_type: Optional[str],
    events: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """违规类型归类（细分）唯一入口 —— 写入端与回填脚本共用。

    【规范码优先级：L1 事件的 candidate_type > LLM 输出的 violation_type】
      这条优先级是"违规类型细分"能成立的前提。实测 42 条违规记录的 48 个 L1
      事件 100% 是 ais_gap（L1 确实检测到了信号中断），而 violation_type 列是
      LLM 自由编的 ais_off / AIS002 / AIS1234 / AIS信号间隔异常…。取 LLM 那侧
      有两个后果：35 条明明检测到"信号中断"的记录显示成"信号异常/关闭"，以及
      7 条编得太离谱的码归一到 unknown 显示成"其他违规"。改取 L1 后 42 条全部
      归到"AIS信号缺失"，细分才谈得上。

      "故意关闭"这类主观判性不丢：它由 LLM 的 final_assessment 承载，并且
      gap_minutes > 360 分钟的记录会在三级细类里落到"疑似关闭"。

    Args:
        violation_type: LLM 输出的原始违规类型（DB 的 violation_type 列）
        events: L1 事件列表（每项含 candidate_type / features）

    Returns:
        describe_full() 的全部字段，外加：
          subtype       三级细类 code（如 "ais_gap#suspected_off"），不分档为 ""
          subtype_label 细类中文标签（如 "疑似关闭"）
          metric        定档依据的实测值（如 680.32）
          metric_unit   实测值单位（如 "分钟"）
          code_source   "l1" | "llm" | "none"，规范码是哪一侧定的（可追溯）
    """
    events = events or []

    code, code_source = "", "none"
    for ev in events:
        c = normalize_violation_code(ev.get("candidate_type") or "")
        if c != "unknown":
            code, code_source = c, "l1"
            break
    if not code:
        c = normalize_violation_code(violation_type)
        if c != "unknown":
            code, code_source = c, "llm"
    if not code:
        code, code_source = "unknown", "none"

    info = describe_full(code)
    subtype, subtype_label, metric = _subtype_of(code, events)
    info.update({
        "subtype": subtype,
        "subtype_label": subtype_label,
        "metric": metric,
        "metric_unit": (SUBTYPE_RULES.get(code) or {}).get("unit", "") if subtype else "",
        "code_source": code_source,
    })
    return info


def list_subtypes(code: str) -> list[dict[str, Any]]:
    """返回某规范码下的静态细类定义（不含数量，数量由接口层聚合）。

    Returns:
        [{code, key, label, unit, min, max}]；该类型不分档时返回 []
    """
    rule = SUBTYPE_RULES.get(code)
    if not rule:
        return []
    out = []
    for i, key in enumerate(rule["keys"]):
        out.append({
            "code": f"{code}{SUBTYPE_SEP}{key}",
            "key": key,
            "label": rule["labels"][i],
            "unit": rule["unit"],
            # min/max 左开右闭（min=None 表示下界取到 -inf），供前端展示区间
            "min": rule["breaks"][i - 1] if i > 0 else None,
            "max": rule["breaks"][i] if i < len(rule["breaks"]) else None,
        })
    return out


def describe_subtype(subtype: str) -> dict[str, Any]:
    """三级细类码 → 完整信息，供接口层反查标签/量纲/所属类型。

    Args:
        subtype: 细类码（如 "ais_gap#suspected_off"）或裸键（如 "suspected_off"，
            裸键只在给定 parent 时才能解析，这里不支持）

    Returns:
        {code, key, label, unit, min, max, parent_code, parent_label}；
        认不出时 label 为空串、parent_code 为空串（调用方据此决定是否展示）
    """
    raw = (subtype or "").strip()
    empty = {"code": "", "key": "", "label": "", "unit": "",
             "min": None, "max": None, "parent_code": "", "parent_label": ""}
    if not raw or SUBTYPE_SEP not in raw:
        return empty
    parent = raw.split(SUBTYPE_SEP)[0]
    for s in list_subtypes(parent):
        if s["code"] == raw:
            return {**s, "parent_code": parent,
                    "parent_label": (CANONICAL.get(parent) or {}).get("label", "")}
    return empty


def resolve_subtype(raw: str) -> str:
    """细类码或裸键 → 完整细类码（幂等）。

    "suspected_off" → "ais_gap#suspected_off"；已是完整码则原样返回；
    认不出返回空串（调用方据此返回 0 条，而不是退化成不过滤）。

    存在的意义：`?subtype=` 是外部可传的查询参数，不该要求调用方先知道
    父类型 —— 裸键里已经隐含了唯一归属，扫一遍 SUBTYPE_RULES 就能补全。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if SUBTYPE_SEP in s:
        return s if describe_subtype(s)["label"] else ""
    for parent in SUBTYPE_RULES:
        if any(x["code"] == f"{parent}{SUBTYPE_SEP}{s}" for x in list_subtypes(parent)):
            return f"{parent}{SUBTYPE_SEP}{s}"
    return ""


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
            # 三级细类的静态定义（标签/量级区间）；数量由接口层按库聚合后填
            "subtypes": list_subtypes(code),
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
