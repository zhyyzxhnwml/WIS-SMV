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
  一级 category  8 个业务大类（CATEGORIES）
  二级 code      16 个规范码（CANONICAL）—— "是哪类违规"
  三级 subtype   量级分档（SUBTYPE_RULES）—— "有多严重"，如 ais_gap#suspected_off

  16 个规范码里 `unknown` 是兜底码（无实测量级可依据，故不分档），
  其余 15 个实体码一律给 3 档细类。
"""

from __future__ import annotations

import re
from typing import Any, Optional

# ── 一级类目 ───────────────────────────────────────────────────────
# name: 类目标识 / label: 中文标签 / order: 显示排序
CATEGORIES: list[dict[str, Any]] = [
    {"name": "ais_device", "label": "AIS设备类", "order": 1},
    {"name": "navigation", "label": "航行行为类", "order": 2},
    {"name": "zone_control", "label": "区域管控类", "order": 3},
    {"name": "anchoring", "label": "锚泊管理", "order": 4},
    {"name": "fishing", "label": "渔业作业类", "order": 5},
    {"name": "collision", "label": "避碰安全类", "order": 6},
    {"name": "data_quality", "label": "数据质量类", "order": 7},
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
    "overtaking_violation": {
        "label": "违规追越",
        "meaning": "船舶追越他船时未按规定让路或未保持安全横距，妨碍被追越船正常航行，违反《国际海上避碰规则》关于追越的规定。",
        "penalty": "责令改正，处二千元以上二万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "navigation",
        "evidence_fields": ["overtaking", "relative_bearing", "min_distance_nm"],
        "base_risk": 65,
    },
    "wrong_way_navigation": {
        "label": "逆向航行",
        "meaning": "船舶在分道通航制或单向通航水域内逆向行驶，航向与该水域主流向相反，违反《国际海上避碰规则》关于分道通航制的规定。",
        "penalty": "责令改正，处二千元以上二万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "navigation",
        "evidence_fields": ["counterflow", "course_offset_degrees", "fairway_direction"],
        "base_risk": 70,
    },
    "illegal_towing": {
        "label": "违规拖带/并靠",
        "meaning": "两船长时间保持极小间距、同速同向航行，构成违规拖带或并靠作业，妨碍他船通航安全且危及本船操纵能力。",
        "penalty": "责令改正，处二千元以上二万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "navigation",
        "evidence_fields": ["towing", "separation_nm", "duration_minutes"],
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
    "anchoring_out_of_bounds": {
        "label": "越界锚泊",
        "meaning": "船舶在锚地范围以外水域锚泊，或在禁止锚泊的水域锚泊，妨碍其他船舶通航安全。",
        "penalty": "责令改正，处一千元以上一万元以下罚款；造成事故的，依法承担相应责任。",
        "category": "anchoring",
        "evidence_fields": ["anchorage", "anchoring_duration_minutes"],
        "base_risk": 50,
    },
    "fairway_anchoring": {
        "label": "航道内锚泊",
        "meaning": "船舶在通航航道（由船队实际航迹统计出的航行走廊）内锚泊停泊，占据通航水域，妨碍其他船舶正常通行。",
        "penalty": "责令改正，处一千元以上一万元以下罚款；造成事故的，依法承担相应责任。",
        "category": "anchoring",
        "evidence_fields": ["fairway_anchoring", "anchoring_duration_minutes", "in_corridor"],
        "base_risk": 55,
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
    "give_way_failure": {
        "label": "让路船未让路",
        "meaning": "两船构成交叉相遇局面（他船在本船右舷），负有让路义务的一方既未改向也未减速，直至实测最近会遇距离进入紧迫局面（0.2 海里）仍未采取避让行动，违反《国际海上避碰规则》关于让路船责任的规定。",
        "penalty": "责令改正，处二千元以上二万元以下罚款；情节严重的，暂扣或吊销船员适任证书。",
        "category": "collision",
        # cri 是接近过程起点上的"若双方都不动"预测值，判据量其实是实测最近距离
        # （distance_nm）——两个都列进证据字段，界面上要能一眼看到"实际靠了多近"。
        "evidence_fields": ["give_way", "relative_bearing", "encounter_type", "distance_nm", "cri"],
        "base_risk": 75,
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
#   CANONICAL 的规范码只回答"是哪类违规"，回答不了"有多严重"。实测 48 个
#   ais_gap 事件的 gap_minutes 跨 60 ~ 1160 分钟，全标成"AIS信号缺失"就把
#   "刚过检测线"和"消失近 20 小时"混成了一类。这里按 L1 事件 features 里的
#   实测量级把类型再切开。
#
# 【覆盖口径】15 个实体码**一律 3 档**（unknown 是兜底码，无实测量级，不分档）。
#   每个码都必须在这里有一条规则，且 breaks 恰好 2 个 —— 验证脚本会断言这一点。
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
    "overtaking_violation": {
        "field": "speed_diff_knots", "unit": "节", "breaks": [2, 5],
        "keys": ["slight", "obvious", "forced"],
        "labels": ["轻微追越", "明显追越", "强行追越"],
    },
    "wrong_way_navigation": {
        # ⚠ 断点必须**高于检测器的下限**，否则那一档永远空桶。本码的检测下限是
        # `counterflow_angle_degrees = 112.5`（偏离主流向超过正横后才算逆向，
        # 见 detect._detect_counterflow_events），原断点 [105, 135] 的第一档
        # （≤105°）因此**算术上不可达**——实测 6 条事件全落在后两档。
        # 现按检测线下沿起分：112.5–130 是"刚过线"，130–155 是常态逆向，
        # >155 才是近乎正对着主流向来。
        "field": "course_offset_degrees", "unit": "°", "breaks": [130, 155],
        "keys": ["deviating", "wrong_way", "severe"],
        "labels": ["轻度逆向", "逆向航行", "严重逆行"],
    },
    "low_speed_fishing": {
        "field": "duration_minutes", "unit": "分钟", "breaks": [30, 120],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短暂滞留", "持续作业", "长时间作业"],
    },
    "course_anomaly": {
        # ⚠ 断点必须**高于检测器的下限**（见 wrong_way_navigation 的同款说明）。
        # 本码的检测下限是 `course_change_degrees = 90`（相邻两点的航向差要**超过**
        # 90° 才算异常，见 detect._detect_course_anomaly_events），原断点 [30, 90]
        # 的前两档（≤30°、30–90°）**算术上不可达**——实测全库只 1 条事件、落最高档。
        # 现按检测线上沿起分：90–120 是"刚过线的突变"，120–160 是明显转向，
        # >160 是接近掉头。
        "field": "course_change_degrees", "unit": "°", "breaks": [120, 160],
        "keys": ["slight", "obvious", "sharp"],
        "labels": ["越线突变", "明显偏航", "剧烈转向"],
    },
    "collision_risk": {
        "field": "cri", "unit": "", "breaks": [0.5, 0.8],
        "keys": ["low", "mid", "high"],
        "labels": ["低风险接近", "中风险接近", "高风险接近"],
    },
    "restricted_area_entry": {
        "field": "point_count", "unit": "点", "breaks": [5, 20],
        "keys": ["brief", "sustained", "deep"],
        "labels": ["短暂越界", "持续进入", "深度闯入"],
    },
    "anchoring_out_of_bounds": {
        # 分档量级取**锚泊持续时长**而不是"越界距离"：
        # detect.py 实现的是"在禁止锚泊水域内保持锚泊态"（禁锚区读法，与
        # fishing_allowed 同构），此时船在区内、没有"越界多远"这个量可测。
        # 硬造一个距离字段会让分档全靠猜，时长则是实测出来的、且单调（越久越重）。
        # ⚠ 断点必须**高于检测器的下限**：`anchoring_min_minutes = 30`，原断点
        # [30, 180] 的第一档只在恰好 30.0 分钟时成立（零测集，实际等于空桶）。
        # 现取 45/180：30–45 是刚够锚泊态的短时违规，45–180 是持续锚泊。
        "field": "anchoring_duration_minutes", "unit": "分钟", "breaks": [45, 180],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短暂锚泊违规", "越界锚泊", "长时间违规锚泊"],
    },
    "ais_off": {
        # 量级取"导航要素不可用持续多久"：单条报文两项全废已是异常，持续越久
        # 越不像偶发设备故障。定档字段由 detect.py 的区间聚合产出。
        "field": "unavailable_minutes", "unit": "分钟", "breaks": [30, 120],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短时要素不可用", "持续要素不可用", "长时间要素不可用"],
    },
    "illegal_fishing": {
        # 与 low_speed_fishing 同量纲同断点：都是"作业持续了多久"。
        # 两者分档一致是刻意的——差异在**判据**（禁渔区/禁渔期 vs 开阔水域作业
        # 特征），不在严重度刻度上，刻度不同会让同一个时长在两个码里读起来不一样。
        "field": "fishing_duration_minutes", "unit": "分钟", "breaks": [30, 120],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短暂作业", "持续作业", "长时间作业"],
    },
    "position_jump": {
        # 分档量级取**推算速度**而非跳变距离：config.max_implied_speed_knots=80
        # 是检测线（超过即判跳变），分档取检测线之上的量级。
        # 字段名必须与 quality.py 写入的 details 键**逐字一致**（implied_speed_knots，
        # 不是 implied_speed）——_detect_quality_events 是把 details 原样搬进 features。
        "field": "implied_speed_knots", "unit": "节", "breaks": [150, 300],
        "keys": ["slight", "obvious", "severe"],
        "labels": ["轻微跳变", "明显跳变", "严重跳变"],
    },
    "give_way_failure": {
        # 与 collision_risk 同量纲同断点（都是 CRI）——同样刻意：让路失败总是伴随
        # 碰撞危险，两个码在同一个 CRI 值上应当读作同一档严重度。
        # 取的是**接近过程起点**上的 CRI（"若双方都不动"的预测），不是最近会遇点上
        # 那个：后者的分布被 f_tcpa 的符号跳变劈成两簇（实测 0.2 海里档 52/7/55），
        # 起点 CRI 才单调反映"不行动会多危险"（同档 5/33/76）。低危档偏薄是实情——
        # 都进了 370 米还没动作的局面本来就很少"低危"。
        "field": "cri", "unit": "", "breaks": [0.6, 0.8],
        "keys": ["low", "mid", "high"],
        "labels": ["低危未让路", "中危未让路", "高危未让路"],
    },
    "fairway_anchoring": {
        # 与越界锚泊同量纲（锚泊时长），但断点更严：航道内锚泊直接堵住通航水域，
        # 10 分钟就该报；越界锚泊要 30 分钟起。
        "field": "anchoring_duration_minutes", "unit": "分钟", "breaks": [30, 120],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短暂碍航停泊", "航道内锚泊", "长时间航道内锚泊"],
    },
    "illegal_towing": {
        # ⚠ 断点必须**高于检测器的下限**：`towing_min_minutes = 20`，原断点
        # [20, 60] 的第一档只在恰好 20.0 分钟时成立（零测集）——实测 14 条事件
        # 全在 sustained/prolonged 两档。现取 30/60：20–30 是刚够判定的短时并靠。
        "field": "duration_minutes", "unit": "分钟", "breaks": [30, 60],
        "keys": ["brief", "sustained", "prolonged"],
        "labels": ["短时并靠", "违规拖带", "长时间违规拖带"],
    },
}

# subtype code 的分隔符："{code}#{key}"，如 "ais_gap#suspected_off"
SUBTYPE_SEP = "#"

# ── 规范码严重度序（classify() 决定"这条记录该报哪一类"的依据） ──────
# 【为什么需要这一级】
#   classify() 原先按 events 的**时间顺序**取第一个能归一的码就 break。一艘船常同时
#   检出多类事件，按时间序取等于让"最早发生的那类"赢——早先的 ais_gap 会把稍后检出
#   的"违规追越"静默吃掉，新加的违规类型因此"加了却看不见"。改为按严重度取最重的一
#   类，其余类型仍留在事件列表里（证据弹窗可展开），类型分布才反映真实检出。
#
#   数值越大越严重。这是一张**业务口径表**，不是法规的量化分级——法规只规定"是什么
#   违规"，不规定"A 类比 B 类重多少"。要调整口径改这里的数字即可，不用动 classify()。
CODE_SEVERITY: dict[str, int] = {
    "restricted_area_entry": 100,   # 进入军事禁区等，后果最重
    "anchoring_out_of_bounds": 90,  # 碍航锚泊
    "fairway_anchoring": 88,        # 堵住通航航道，比越界锚泊略轻（前者在禁区，后者在航道）
    # 碰撞风险排在两个具体违反**之后**，不是数字凑出来的：
    # 违规追越/逆向航行/让路失败是**已成立的规则违反**，可归责于本船的具体义务
    # （COLREG 第13/10/15条）；「碰撞风险」是一个**风险状态**——可能双方都没违规，
    # 也可能是对方违规。违规列表的主类型按"违反了哪条规则"排，风险状态不该盖过
    # 具体的规则违反。（原值 85 是在碰撞检测从未运行过的前提下定的，与它实际会
    # 产出什么无关。）
    "overtaking_violation": 80,     # 违反追越让路义务（第13条）
    "wrong_way_navigation": 80,     # 违反分道通航制（第10条）
    "give_way_failure": 80,         # 违反交叉相遇让路义务（第15/16条）——与上两者同级
    "collision_risk": 78,           # 已构成碰撞危险（风险状态，非规则违反）
    "illegal_fishing": 75,
    "illegal_towing": 72,           # 违规拖带/并靠：碍航且危及操纵，但阈值标定弱于追越
    "speed_violation": 70,
    "ais_off": 65,                  # 涉嫌主动关闭，含主观逃避意图
    "ais_gap": 60,                  # 信号中断，可能是客观通信问题
    "low_speed_fishing": 55,
    "course_anomaly": 50,
    "position_jump": 45,
    "unknown": 0,
}

# 未在 CODE_SEVERITY 中列出的码（今后新增类型忘了登记时）取这个值：**高于 ais_gap(60)**，
# 这样"新类型漏登记"的失败模式是"它照样能显示出来"，而不是"被 ais_gap 静默吃掉"——
# 后者正是当初新类型加了却看不见的原因。验证脚本会断言每个 CANONICAL 码都已登记，
# 所以这个兜底值只在该断言被绕过时才起作用。
CODE_SEVERITY_DEFAULT = 65

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
    # 让路船未让路：锚 COLREG 第15条（交叉相遇局面："有他船在本船右舷的船舶应给
    # 他船让路"）。**不锚第16/17条**——不是它们不适用（第16条「让路船的行动」才是
    # 本码最贴的条文），而是 regulation_db 对 COLREG.pdf 的条款切分只切到 10 条
    # （2/3/9/14/15/19/25/31/36/37），16、17 没有独立条目，锚上去 get_article() 查空、
    # seed_regulations_milvus 的锚点校验还会报错。所幸第15条那个块的正文里**紧跟着**
    # 第十六条与第十七条的全文，检索回来读得到让路义务原文。
    # 本轮把检测器的让路义务收窄到**只有交叉相遇**（`config.give_way_duty_encounters`），
    # 锚点与判据的口径因此正好对齐：第15条就是交叉相遇局面的那一条。
    "give_way_failure": [("COLREG", "15")],
    # 航道内锚泊：主锚《海上交通安全法》第47条"船舶应当在符合安全条件的码头、
    # 泊位、装卸站、锚地、安全作业区停泊"——这句就是"停泊地点须为指定水域"的法定
    # 出处，航道不在其列；次锚《福州港港口章程》第23条"港内禁锚区"是该港禁止锚泊
    # 水域的本地出处（条文自身举的是桥梁上下游与取水口上下游，锚定它取的是"禁锚区
    # 这件事由该章程规定"）。两条都是复核过原文存在的，没有臆造。
    "fairway_anchoring": [
        ("中华人民共和国海上交通安全法2021", "47"),
        ("福州港港口章程", "23"),
    ],
    # 违规拖带/并靠：《海上交通安全法》第45条是**逐字讲拖带**的那一条——
    # "船舶载运或者拖带超长、超高、超宽、半潜的船舶、海上设施或者其他物体航行，
    # 应当采取拖拽部位加强、护航等特殊的安全保障措施…"。第47条补"并靠"这一侧
    # （停泊不得危及其他船舶、海上设施的安全）。
    "illegal_towing": [
        ("中华人民共和国海上交通安全法2021", "45"),
        ("中华人民共和国海上交通安全法2021", "47"),
    ],
    "illegal_fishing": [
        ("中华人民共和国渔港水域交通安全管理条例2019", "10"),
        ("中华人民共和国渔港水域交通安全管理条例2019", "21"),
    ],
    "low_speed_fishing": [("中华人民共和国渔港水域交通安全管理条例2019", "10")],
    "overtaking_violation": [("COLREG", "13")],
    "wrong_way_navigation": [("COLREG", "10")],
    # 越界锚泊：《福州港港口章程》第12条"锚地"逐一列明闽江口/兴化湾/福清湾/平潭等
    # 各锚地，是"锚地范围"的法定出处（实测数据海域 119~121.5°E / 24.5~27.7°N 正落在
    # 福州港辖区，这份章程是地理上对得上的那一部）。注意该条是**描述性**条款——它说
    # 明有哪些锚地，本身不含处罚表述；锚定它的意义是"越界的界从哪来"，罚则措辞见
    # FALLBACK_REG。不臆造更细的坐标条款（原文细节在附表1，未随正文入库）。
    # 第23条"港内禁锚区"是**禁止性**的那一条：全章程 44 条里只有它出现"严禁…锚泊"
    # （桥梁上下游规定范围、自来水厂/调水工程取水口上下游各 100 米）。检测器实现的
    # 正是"禁锚区"读法（区域声明 anchoring_allowed:false，见 detect._detect_anchoring_events
    # 的 docstring），与第23条的问法一致；一条描述性的"界"配一条禁止性的"禁"，
    # 罚则也就不必全压在 FALLBACK_REG 上。
    "anchoring_out_of_bounds": [
        ("福州港港口章程", "12"),
        ("福州港港口章程", "23"),
    ],
    "unknown": [],
}


# ── 法规兜底文本（检索失败时的保底引用） ─────────────────────────
# ── 法规文档元数据：效力层级（优先级）／文件性质／适用地域 ──
#
# key = PDF 文件名去扩展名，**和 REG_ANCHORS 用同一套 key**（改了这里就得改锚定表）。
#
# priority：数字**小 = 效力高**，检索排序的主键（见 regulation_db.search_by_violation）。
#   依据中国立法效力层级：法律 > 行政法规 > 国际规则 > 地方政府规章 > 规范性文件。
#   为什么不按相似度排：实测 nomic 在短中文同域文本上的余弦**分不开相关与不相关**
#   （相关对 0.583~0.845 与不相关对 0.727~0.772 完全重叠，详见 regulation_db.MIN_SIMILARITY
#   上方注释）。既然分数不可靠，就用"效力层级"这个**确定的事实**当主排序，相似度只在
#   同一层级内部决定先后。要调整口径，改这里的数字即可，不用动检索代码。
#
# doc_type 决定 rule/ 下的归放目录，region 供"地方法规优先本地"之类的展示/筛选用。
DOC_META: dict[str, dict[str, Any]] = {
    "中华人民共和国海上交通安全法2021": {
        "doc_type": "法律", "priority": 1, "region": "全国",
        "authority": "全国人民代表大会常务委员会",
    },
    "中华人民共和国渔港水域交通安全管理条例2019": {
        "doc_type": "行政法规", "priority": 2, "region": "全国",
        "authority": "国务院",
    },
    "COLREG": {
        "doc_type": "国际规则", "priority": 3, "region": "国际",
        "authority": "国际海事组织（IMO）",
    },
    "福州港港口章程": {
        "doc_type": "地方政府规章", "priority": 4, "region": "福州",
        "authority": "福州市人民政府",
    },
    "船舶防台风指南（试行）": {
        "doc_type": "规范性文件", "priority": 5, "region": "全国",
        "authority": "交通运输部海事局",
    },
    # regulations.json 里的 zones 解析出来的**示例**区域管制规定（军事禁区B/禁渔区A/
    # 港口限速区C）。名字里就写着"样例数据"：不是真实法条，是配着区域功能用的占位条文。
    # 排最后（与未登记同级），但**显式登记**——不登记的话每次导入都会告警，
    # 告警就失去意义了（真正的新来源会被这条噪音盖住）。
    "船舶违规检测规则库 - 样例数据": {
        "doc_type": "样例数据", "priority": 9, "region": "示例",
        "authority": "—",
    },
}

# 认不出的来源（新放进来还没登记元数据的 PDF）：排在所有已登记之后，
# 但仍然入库 —— 宁可排最后，也不要静默丢弃法规。
DOC_META_FALLBACK: dict[str, Any] = {
    "doc_type": "其他", "priority": 9, "region": "未知", "authority": "",
}


def doc_meta(source: str) -> dict[str, Any]:
    """来源名 → {doc_type, priority, region, authority}；未登记返回 fallback。"""
    return DOC_META.get((source or "").strip()) or dict(DOC_META_FALLBACK)


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
    "overtaking_violation": "《国际海上避碰规则》（COLREG）第十三条关于任何船舶在追越他船时均应给被追越船让路、直至驶过让清为止的规定",
    "wrong_way_navigation": "《国际海上避碰规则》（COLREG）第十条关于使用分道通航制区域的船舶应在相应通航分道内顺着该分道船舶总流向行驶的规定",
    "anchoring_out_of_bounds": "《福州港港口章程》关于船舶应在公布的锚地范围内锚泊、不得在航道及禁止锚泊水域锚泊的规定",
    "fairway_anchoring": "《中华人民共和国海上交通安全法》关于船舶应当在符合安全条件的码头、泊位、装卸站、锚地、安全作业区停泊，不得在通航航道内锚泊妨碍其他船舶通行的规定",
    "illegal_towing": "《中华人民共和国海上交通安全法》关于船舶载运或者拖带船舶、海上设施或者其他物体航行应采取加强拖拽部位等特殊安全保障措施、并按规定报告航行计划与显示信号的规定",
    "give_way_failure": "《国际海上避碰规则》（COLREG）第十五条关于交叉相遇局面下有他船在本船右舷的船舶应给他船让路的规定，以及第十六条关于须给他船让路的船舶应尽可能及早地采取大幅度行动、宽裕地让清他船的规定",
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
    # 违规追越（COLREG 第13条：追越船应让路，不得妨碍被追越船）
    "overtaking_violation": "overtaking_violation",
    "overtaking": "overtaking_violation",
    "overtaking_not_giving_way": "overtaking_violation",
    "违规追越": "overtaking_violation", "追越": "overtaking_violation",
    "追越未让路": "overtaking_violation", "违规超船": "overtaking_violation",
    # 逆向航行（COLREG 第10条：分道通航制，应按通航分道方向行驶）
    "wrong_way_navigation": "wrong_way_navigation",
    "wrong_way": "wrong_way_navigation", "counterflow": "wrong_way_navigation",
    "reverse_navigation": "wrong_way_navigation",
    "逆向航行": "wrong_way_navigation", "逆向": "wrong_way_navigation",
    "逆行": "wrong_way_navigation", "反向行驶": "wrong_way_navigation",
    # 越界锚泊（在锚地范围外或禁止锚泊水域锚泊）
    "anchoring_out_of_bounds": "anchoring_out_of_bounds",
    "illegal_anchoring": "anchoring_out_of_bounds",
    "anchoring_violation": "anchoring_out_of_bounds",
    "越界锚泊": "anchoring_out_of_bounds", "违规锚泊": "anchoring_out_of_bounds",
    "越界停泊": "anchoring_out_of_bounds", "非法锚泊": "anchoring_out_of_bounds",
    "锚地越界": "anchoring_out_of_bounds", "擅自锚泊": "anchoring_out_of_bounds",
    # 航道内锚泊（数据驱动航道走廊：在通航航道里抛锚堵路）
    "fairway_anchoring": "fairway_anchoring",
    "anchoring_in_fairway": "fairway_anchoring",
    "anchoring_in_channel": "fairway_anchoring",
    "航道内锚泊": "fairway_anchoring", "航道锚泊": "fairway_anchoring",
    "碍航锚泊": "fairway_anchoring", "航道内停泊": "fairway_anchoring",
    # 让路船未让路（COLREG 第15/16条：交叉相遇下的让路义务）
    "give_way_failure": "give_way_failure",
    "give_way_violation": "give_way_failure",
    "failure_to_give_way": "give_way_failure",
    "not_giving_way": "give_way_failure",
    "crossing_give_way": "give_way_failure",
    "让路船未让路": "give_way_failure", "未让路": "give_way_failure",
    "让路失败": "give_way_failure", "交叉相遇未让路": "give_way_failure",
    "未履行让路义务": "give_way_failure",
    # 违规拖带/并靠（两船长时间极小间距同速同行）
    "illegal_towing": "illegal_towing",
    "towing_violation": "illegal_towing",
    "improper_towing": "illegal_towing",
    "towing": "illegal_towing",
    "违规拖带": "illegal_towing", "违规拖曳": "illegal_towing",
    "非法拖带": "illegal_towing", "违规并靠": "illegal_towing",
    "拖带违规": "illegal_towing", "并靠违规": "illegal_towing",
    # 实测库里出现过：LLM 把 L1 的"标注类型"当违规类型输出了。
    # 时间戳非递增属数据质量问题，归 position_jump（同属 data_quality 类目）。
    "data_quality_non_increasing_time": "position_jump",
    "data_quality_position_jump": "position_jump",
    "non_increasing_time": "position_jump",
    # AIS 导航要素不可用（收到报文但内容整体不可用，与 ais_gap 的"没收到"互斥）
    "nav_data_unavailable": "ais_off",
    "ais_nav_unavailable": "ais_off",
    "ais导航要素不可用": "ais_off",
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


def _event_view(ev: Any) -> tuple[str, dict[str, Any]]:
    """事件 → (candidate_type 字符串, features 字典)。两种形态都要吃得下。

    写入端（orchestrator）手工构字典，回填/校验脚本往往直接把 CandidateEvent
    对象丢进来。原先 classify()/_subtype_of() 只认字典，混用会在遍历中途抛
    `'CandidateEvent' object has no attribute 'get'` —— 一个已经踩到过的坑。
    """
    if isinstance(ev, dict):
        raw_type = ev.get("candidate_type")
        features = ev.get("features") or {}
    else:  # 对象形态（CandidateEvent）
        raw_type = getattr(ev, "candidate_type", None)
        raw_type = getattr(raw_type, "value", raw_type)  # 枚举取 .value
        features = getattr(ev, "features", None) or {}
    return str(raw_type or ""), (features if isinstance(features, dict) else dict(features))


def _subtype_of(
    code: str, events: Optional[list[Any]]
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
        raw_type, features = _event_view(ev)
        # 只让同类事件给这个类型定档：别的类型的事件量纲不同，混进来会定错档
        if normalize_violation_code(raw_type) != code:
            continue
        try:
            val = float(features.get(field))
        except (TypeError, ValueError):
            continue
        idx = sum(1 for b in breaks if val > b)
        if idx > worst_idx:
            worst_idx, worst_val = idx, val

    if worst_idx < 0:
        return "", "", None
    return f"{code}{SUBTYPE_SEP}{rule['keys'][worst_idx]}", rule["labels"][worst_idx], worst_val


# ── L1 硬证据：足以在 LLM 判"无违规"时把记录**提升**为违规 ────────────
#
# 这个映射存在的原因是一个实测过的门槛：落库的 `violation_detected` **只由 LLM 置位**
# （run.py 写库那一行的 `1 if llm_result.get("violation_detected") else 0`），而整段
# "规范码 / 三级细类 / 法规引用"的物化又都挡在 `if llm_result.get("violation_detected"):`
# 之后。于是只要 LLM 说"这船没问题"，L1 实测到的追越、逆向、碰撞就永远到不了界面——
# 库里 617 行只有 42 行带违规码、且 100% 是 ais_gap，正是这个门槛造成的。
#
# 但不是所有 L1 类型都够格推翻 LLM（乱提升就是把"没违规"写成"有违规"）：
#   - 追越要"强行"（峰值速度差 ≥5 节）：第13条的让路义务在轻微追越里本就模糊，
#     而 ≥5 节的速度差配上 <0.1 海里的横距是硬碰硬的实测量。
#   - 碰撞要 CRI ≥0.6：CRI 是几何外推出来的连续量，低于此值更像"接近"而非"危险"。
#   - 超速**刻意不提升**：判据是数据驱动的工程阈值（同水域主流速 ×1.2），
#     不是法规限值。拿工程阈值去推翻 LLM 会把"比周围快一点"写成违规，
#     这一类留给人工复核更稳，也符合 `SUBTYPE_RULES` 里"工程分档不是法规阈值"的既定口径。
#   - 信号中断/位置跳变/航向异常不提升：既有的 ais_gap 路径已覆盖，
#     且这三类本身可能是客观通信问题，不是操船行为违规。
PROMOTABLE: dict[str, Optional[tuple[str, float]]] = {
    "overtaking_violation": ("max_speed_diff_knots", 5.0),  # 强行追越
    "restricted_area_entry": None,  # 闯入禁航区
    "anchoring_out_of_bounds": None,  # 越界锚泊
    # 违规捕捞：禁渔区 + 禁渔期 + 实测低速作业，判据来自区域配置与时间规则，
    # 成立即硬证据（与 restricted_area_entry 同一类：区域事实 + 实测行为）。
    "illegal_fishing": None,
    # 让路船未让路：两侧都是**实测**——实测最近会遇距离进了紧迫局面的门
    # （`give_way_close_quarters_nm`，0.2 海里）**且**让路船实测未改向、未减速。
    # 这条原先就写着可提升，但理由写错了：旧注释说"CRI 只用来选择让路角色，
    # 不用来制造违规"，而当时的门其实是**预测** DCPA≤0.5 海里 + CRI≥0.4——
    # 正是下面 collision_risk 被排除掉的那两个预测量（实测有九成被点名的事件
    # 从未进过 5 海里）。本轮把门换成实测距离之后，"预测量不用于制造违规"
    # 这条口径才真正成立：CRI 现在只做细类分档，不进判据。
    "give_way_failure": None,
    # 超速：门槛取 `max_speed >= 16.0`，就是检测器自己的绝对异常线
    # （`config.speed_abs_min_knots`，实测标定过：=16 命中 7 船，=25 命中 0 船）。
    # 为什么它算硬证据而碰撞风险不算：这一项是 **AIS 实测速度**（位置差分算出来的），
    # 与追越的实测横距同类；碰撞风险的 DCPA/TCPA 是预测器推出来的，故被排除。
    # 原先清单里没有它，不是因为评估过、而是因为速度检测器**从未触发过**
    # （判据依赖 regulations.json 的区域限速，而示例区全在 38°N，与实测海域零重叠），
    # 改成数据驱动判据后才第一次真的产出事件，这个问题才第一次被问到。
    "speed_violation": ("max_speed", 16.0),
    # ── 以下类型**刻意不提升** ──
    # 【2026-10-14 本轮剔除的三条】原先在清单里，实测在 HawaiiCoast_GT 的
    #   4186 船-日 / 48 起事故上 lift<1 —— 提升它们等于把干净的船写成违规：
    #   · fairway_anchoring：触发 77.1% 的船-日，lift 0.70。根因不是门槛错，
    #     是**走廊本身被锚地污染**：走廊取"船队点密度 ≥2 的格网"，而锚地里几十条船
    #     停在同一格，密度最高——于是"航道内锚泊"判据退化成"这条船锚着"。规则本身
    #     留着（事件照旧产出、照旧展示），只是不能再单独把 LLM 的"无违规"翻过来；
    #     要修就修走廊（用**运动中的点**建，或按锚地掩膜扣掉），那是 L1 的改动。
    #   · wrong_way_navigation：95 起触发，命中 0 起。判据依赖航向与流向场，
    #     在实测海域没有一次站得住，同样降为"事件照出、不提升"。
    #   · ais_off：30 起触发，命中 0 起。SOG/COG 取不可用编码确实是报文事实，
    #     但**报文不可用 ≠ 船舶违规**（也可能是岸站或转发环节），不该独立定性。
    # 剔除这三条后，提升清单从 79.9% 报警率 / lift 0.78 变成 10.0% 报警率 /
    #   lift 2.09（同一批 4186 船-日，见 `data/logs/diag_verdict.py`）。
    # position_jump / course_anomaly / ais_gap：数据质量与操纵异常，既有的 ais_gap
    #   路径已覆盖，且可能是客观设备/通信问题，不是操船行为违规。
    # low_speed_fishing：它本身就是"嫌疑"（语义如此），拿嫌疑去推翻 LLM 的"无违规"
    #   不成立，留给人工复核。
    # illegal_towing：0.10 海里的间距门限是**未标定**的工程阈值（比追越的 0.10 海里
    #   横距更硬，但拖带与"两船并排航行/同泊位系泊"在 AIS 上难分辨），
    #   拿它单独把一条干净的船变成违规证据不足。
    # 这里**故意没有 collision_risk**（原先有，门槛 cri>=0.6）。
    # 提升清单的用途是"LLM 说没问题时，硬证据仍能立住违规"。而碰撞风险的两项几何
    # 依据（DCPA/TCPA）是 TrajectoryPredictor **预测**出来的，不是实测：首次接线后
    # 实测 10450 对邻近船，被 CRI 判为有风险的船对里，预测最近会遇距离中位数 4.19nm、
    # p90 达 41nm、最大 177nm —— 而参与评估的船对本来就都在 10nm 之内，说明这个预测
    # 在真实数据上不可靠。拿它单独把一条干净的船变成违规，不成立。
    # 碰撞风险事件照旧产出、照旧进证据链，也照旧能在已经因别的理由成违规的船上当主类型，
    # 只是不能**单独**制造违规。
}

# 单船硬证据事件的合理上限。超过这个数说明检测跑飞了（阈值配错 / 数据异常），
# 此时宁可不提升，也不要往库里灌一船假违规。
PROMOTION_SANITY_LIMIT = 20


def deterministic_violation(events: Optional[list[Any]]) -> Optional[dict[str, Any]]:
    """从 L1 事件里挑出"硬证据"事件，供写入端在 LLM 判无违规时提升。

    Args:
        events: L1 事件列表；每项可以是候选事件对象（有 candidate_type / features），
            也可以是 {"candidate_type": ..., "features": {...}} 字典——写入端两种都用。

    Returns:
        {"code", "feature", "value", "count"}：命中的规范码、定档字段、实测值、
        硬证据事件数；无硬证据（或事件数超出 sanity 上限）时返回 None。
    """
    hard: list[tuple[str, str, float]] = []  # (规范码, 命中字段, 实测值)
    for ev in events or []:
        raw_type, features = _event_view(ev)  # 字典/对象两种形态都吃（见 _event_view）
        code = normalize_violation_code(raw_type)
        if code not in PROMOTABLE:  # 不在可提升清单里
            continue
        gate = PROMOTABLE[code]  # 该类型的门槛（None 表示无门槛）
        if gate is None:  # 无条件成立
            hard.append((code, "", 0.0))
            continue
        field_name, threshold = gate  # 门槛字段与阈值
        try:
            value = float(features.get(field_name))
        except (TypeError, ValueError):
            continue  # 缺实测值 → 不敢提升
        if value >= threshold:  # 过门槛
            hard.append((code, field_name, value))
    if not hard:  # 无硬证据
        return None
    if len(hard) > PROMOTION_SANITY_LIMIT:  # 明显跑飞
        return None
    # 取最严重的一类（与 classify 的取码口径一致），同级取实测值最大的
    code, feature_name, value = max(
        hard, key=lambda item: (CODE_SEVERITY.get(item[0], CODE_SEVERITY_DEFAULT), item[2])
    )
    return {"code": code, "feature": feature_name, "value": value, "count": len(hard)}


def classify(
    violation_type: Optional[str],    events: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """违规类型归类（细分）唯一入口 —— 写入端与回填脚本共用。

    【同一侧内部取哪一条：按 CODE_SEVERITY 取最严重的，不按时间取最早的】
      一艘船常同时检出多类事件（既有 ais_gap，又有违规追越）。这里取最严重的一类
      作为该记录的主类型，其余类型仍保留在 events 中供证据展示。若按时间顺序取最
      早的那种，ais_gap 会稳定地盖掉后检出的新增类型，界面上就看不到类型分化。

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

    # 【取哪一类：L1 事件里**最严重**的那个，而不是时间上最早的那个】
    # 曾经是"取第一个能归一的码就 break"，结果是同一艘船的早先事件（几乎总是
    # ais_gap）盖掉稍后检出的追越/逆向等新增类型，界面上就只剩一种违规。
    # 现在扫完全部事件按 CODE_SEVERITY 取最重的一类；其余类型仍在 events 里，
    # 证据弹窗照旧可以展开，只是"这条记录的主类型"归到最重的那个。
    code, code_source = "", "none"
    best_severity = -1
    for ev in events:
        c = normalize_violation_code(_event_view(ev)[0])
        if c == "unknown":
            continue
        severity = CODE_SEVERITY.get(c, CODE_SEVERITY_DEFAULT)
        if severity > best_severity:
            code, code_source, best_severity = c, "l1", severity
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


def law_display_name(source: str) -> str:
    """法规 source → 展示用名称。

    source 就是 PDF 文件名去扩展名（REG_ANCHORS 拿它当主键，**不能改动库里的值**），
    所以尾上带着年份：「中华人民共和国海上交通安全法2021」。没有哪部法叫这个名字，
    展示时去掉。

    只在**末尾**且**前面不是「年」或括号**时才去 ——「1972年国际海上避碰规则」的
    年份是名字本身的一部分（且在开头），不受影响。
    """
    s = (source or "").strip()
    return re.sub(r"(?<![年（(])(?:19|20)\d{2}$", "", s) or s


def format_article_ref(art: dict[str, Any], with_similarity: bool = True) -> str:
    """法规条目 → 展示用引用文本。

        《中华人民共和国海上交通安全法》第36条（船舶在航行中应当按照有关规定开启…）相关度68%

    【为什么书名号只包法规名】
      article["title"] 是解析时取的法条**正文首句**（见 RegulationParser.parse_pdf_text：
      `re.split(r"[。；\\n]", content, maxsplit=1)[0]`），不是标题 —— 中文法条多数没有
      条标题。把它塞进《》会读成"有这么一部法"，实测界面里就出现过
      「《船舶在航行中应当按照有关规定开启船舶的自动识别、航行数据》」这种把半句正文
      当法名的引用。所以标题放（）里。
    """
    src = law_display_name(art.get("source", ""))
    num = str(art.get("article_number", "") or "")
    title = (art.get("title") or "").strip()
    sim = art.get("similarity")

    ref = f"《{src}》" if src else ""
    if num and num != "0":
        # 小数编号（"4.1.1"，指南/标准体例）省掉"第/条" ——「第4.1.1条」不是中文
        # 法规的引用写法；「第X条」体例才加。
        ref += f"{num}" if "." in num else f"第{num}条"
    if title:
        ref += f"（{title[:40]}）"
    if with_similarity and sim is not None:
        ref += f"相关度{sim:.0%}"
    return ref


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
        src = law_display_name(best_article.get("source", ""))
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
