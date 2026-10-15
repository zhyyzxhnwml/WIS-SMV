# -*- coding: utf-8 -*-
"""只读输入契约 facts v1 —— 插件能看到的全部事实。

【为什么不把内部结构直接给插件】
  run.py 里现成有 entry = annotated.to_agent_input()（models.py:296），字段一应俱全，
  直接丢给插件最省事。但那样 AnnotatedTrack 一改字段名，所有插件会**静默失效** ——
  插件多半写的是 facts.get("xxx") or {}，取不到就是空，不报错、不生效，最难查。
  所以这里做一层归一：内部结构怎么变都行，facts v1 的键是承诺过的。

【字段名与内部不一致是故意的】
  内部用 speed/course/timestamp，契约用 sog/cog/ts（航海惯例缩写）。
  换名是为了让「插件写的是契约字段」这件事一眼可辨 —— 插件里出现 speed
  就说明它拿错了来源。

【facts 里没有的东西，插件也拿不到】
  没有 API key、没有 DB、没有文件路径、没有网络。base 段是只读的基线结论，
  插件能看到它，但写不回去（见 registry.run_adjudicators：只往 plugin_findings 写）。
"""
from typing import Any

FACTS_VERSION = "v1"
MAX_POINTS = 200          # 与 llm_layer._render_prompt 的 max_track_points 同口径

# 具名特征清单的来源（annotator.py:332 FEATURE_NAMES，28 维，每项带中文注释）。
# 惰性导入：facts.py 只在父进程被用到，但导入链上带着 pydantic 模型，
# 不该让 sandbox_runner（子进程）沾到任何它。
def _feature_names() -> tuple[str, ...]:
    try:
        from ais_accelerator.annotator import FEATURE_NAMES
        return tuple(FEATURE_NAMES)
    except Exception:
        return ()


# 给 AI 起草器用的特征说明（提示词里要原样嵌进去，否则它会凭空引用不存在的字段）
FEATURE_DOC = """\
original_point_count 原始轨迹点数 | annotation_count 标注标记总数 |
critical_annotation_count 严重标记数 | warning_annotation_count 警告标记数 |
info_annotation_count 信息标记数 | avg_annotation_confidence 平均置信度 |
max_annotation_confidence 最高置信度 | min_annotation_confidence 最低置信度 |
total_duration_minutes 总时长(分钟) | speed_variance 速度方差 |
avg_speed 平均速度(节) | max_speed 最高速度(节) | min_speed 最低速度(节) |
course_change_count 航向变化次数 | ais_gap_count AIS中断次数 |
position_jump_count 位置跳变次数 | rule_hit_count 规则命中数 | event_count 事件数 |
overlap_annotation_count 重叠标记数 | segment_count 分段数 |
quality_issue_count 质量问题数 | duplicate_point_count 重复点数 |
collision_risk_count 碰撞风险事件数 | max_cri 最高碰撞风险指数 |
min_dcpa_nm 最小DCPA(海里) | min_tcpa_minutes 最小TCPA(分钟,正值为未来) |
avg_relative_speed 平均相对速度(节) | nearby_ship_count 邻近船数量"""


def _sample_points(points: list[dict], max_points: int = MAX_POINTS) -> list[dict]:
    """等间隔抽样并归一字段名：{ts, lon, lat, sog, cog}。"""
    if not points:
        return []
    step = max(1, len(points) // max_points)
    picked = points[::step][:max_points]
    out = []
    for p in picked:
        out.append({
            "ts": str(p.get("timestamp") or ""),
            "lon": _f(p.get("lon")),
            "lat": _f(p.get("lat")),
            "sog": _f(p.get("speed")),
            "cog": _f(p.get("course")),
        })
    return out


def _f(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _zones_from_events(events: list[dict], rules: Any) -> list[dict]:
    """把 L1 事件引用的区域还原成 {id, name, restrictions, bbox, time_rules}。

    不遍历全部区域、也不做几何查询 —— 只取这条船**真的碰过**的区域（事件里带 zone_id）。
    几何由宿主算好 bbox 给插件，插件不必自己判断点在不在多边形里，
    也就省掉了对 shapely 的依赖。
    """
    if not events:
        return []
    provider = getattr(rules, "zone_provider", rules)      # RagRuleProvider 包了一层
    zone_by_id = getattr(provider, "zone_by_id", None)
    if not callable(zone_by_id):
        return []

    seen: dict[str, dict] = {}
    for ev in events:
        zid = str(ev.get("zone_id") or ev.get("zone_name") or "")
        if not zid or zid in seen:
            continue
        zone = zone_by_id(zid) or {}
        boundary = zone.get("boundary") or []
        bbox = None
        if boundary:
            try:
                lons = [float(p[0]) for p in boundary]
                lats = [float(p[1]) for p in boundary]
                bbox = [min(lons), min(lats), max(lons), max(lats)]
            except (TypeError, ValueError, IndexError):
                bbox = None
        seen[zid] = {
            "id": zone.get("id", zid),
            "name": zone.get("name", zid),
            "restrictions": zone.get("restrictions") or {},
            "time_rules": zone.get("time_rules") or [],
            "bbox": bbox,
        }
    return list(seen.values())


def _l1_events(events: list[dict]) -> list[dict]:
    out = []
    for ev in events:
        ct = ev.get("candidate_type")
        out.append({
            "candidate_type": ct.value if hasattr(ct, "value") else str(ct or ""),
            "zone_id": ev.get("zone_id") or "",
            "zone_name": ev.get("zone_name") or "",
            "priority": ev.get("priority") or "",
            "start_ts": str(ev.get("start_time") or ""),
            "end_ts": str(ev.get("end_time") or ""),
            "features": ev.get("features") or {},
        })
    return out


def build_facts(entry: dict, llm_result: dict | None, rules: Any = None,
                max_points: int = MAX_POINTS) -> dict:
    """组装 facts v1。entry 是 run.py 里 annotated.to_agent_input() 的返回。"""
    entry = entry or {}
    llm_result = llm_result or {}
    # 拷贝一份再补齐键。**不能原地 setdefault**：entry 是 run.py 里那份要
    # json.dumps 进 detection_results.detailed_result 的对象，原地改会让
    # 「启用插件」这件事本身改变 detailed_result 的内容 —— 那正是本层
    # 承诺不做的（对基线零影响）。
    fm = dict(entry.get("feature_metadata") or {})
    qr = entry.get("quality_report") or {}
    events = entry.get("events") or []
    routing = entry.get("routing") or {}
    points = entry.get("track") or []

    # features：按 FEATURE_NAMES 逐个取，取不到的键给 None（而不是省略）——
    # 省略会让插件无法区分「这个特征恒为 0」和「这个特征不存在」。
    for name in _feature_names():
        fm.setdefault(name, None)

    return {
        "facts_version": FACTS_VERSION,
        "mmsi": str(entry.get("mmsi") or (entry.get("ship") or {}).get("mmsi") or ""),
        "analysis_time": str(entry.get("processed_at") or ""),
        "quality": {
            "level": (qr.get("level") or "good"),
            "invalid_points": int(qr.get("invalid_points") or 0),
            "duplicate_points": int(qr.get("duplicate_points") or 0),
            "retained_points": qr.get("retained_points"),
        },
        "track": {
            "n_points": len(points),
            "duration_min": _f(fm.get("total_duration_minutes")),
            "avg_speed": _f(fm.get("avg_speed")),
            "max_speed": _f(fm.get("max_speed")),
            "points": _sample_points(points, max_points),
        },
        "features": {k: fm.get(k) for k in (_feature_names() or fm.keys())},
        "l1_events": _l1_events(events),
        "l2": {
            "ml_category": routing.get("ml_category") or "",
            "ml_confidence": _f(routing.get("ml_confidence")),
            "routing": routing.get("routing") or "",
            "probs": routing.get("all_probabilities") or {},
        },
        "zones": _zones_from_events(events, rules),
        "base": {
            "violation_detected": int(llm_result.get("violation_detected") or 0),
            "violation_code": str(llm_result.get("violation_code") or ""),
            "risk_level": str(llm_result.get("risk_level") or ""),
            "confidence": _f(llm_result.get("confidence")),
            "assessment": str(llm_result.get("final_assessment") or "")[:1000],
        },
    }


# ── 动态试跑用的样本：一份该命中、一份不该命中、一份空数据 ──
# 「不该命中」那份是关键：LLM 生成插件最常见的缺陷是无脑 return [一条 finding]，
# 用一份明确不该命中的样本一跑就露馅（见 forge.trial 的恒真检测）。
#
# `_name` / `_expect` 是给试跑器看的元数据，不是 facts 的一部分 ——
# forge.trial() 会剥掉所有下划线开头的键再交给插件。
def sample_facts() -> list[dict]:
    base_zone = {
        "id": "Z_TEST_FISH",
        "name": "测试禁渔区",
        "restrictions": {"fishing_allowed": False, "entry_allowed": True},
        "time_rules": [],
        "bbox": [120.0, 30.0, 120.2, 30.2],
    }
    # 区内 30 个点、间隔 2 分钟（10:00 → 10:58）→ 滞留 58 分钟，应当命中
    # 注意是分钟位而不是秒位：写成 %60 秒会得到 0.97 分钟，插件会正确地拒绝它，
    # 样本就失去了「该命中」的意义（这个坑踩过一次，写下来）。
    inside = [{"ts": f"2026-01-01T10:{i * 2:02d}:00", "lon": 120.1, "lat": 30.1,
               "sog": 2.0, "cog": 90.0} for i in range(30)]
    # 区外高速通过 → 不该命中
    outside = [{"ts": f"2026-01-01T12:{i * 2:02d}:00", "lon": 121.5, "lat": 31.5,
                "sog": 12.0, "cog": 45.0} for i in range(30)]

    def _f(pts, feats):
        return {
            "facts_version": FACTS_VERSION, "mmsi": "413000000",
            "analysis_time": "2026-01-01T11:00:00", "quality": {"level": "good"},
            "track": {"n_points": len(pts), "duration_min": 58.0, "avg_speed": 2.0,
                      "max_speed": 2.0, "points": pts},
            "features": feats, "zones": [base_zone],
            "l1_events": [], "l2": {}, "base": {},
        }

    return [
        dict(_f(inside, {"avg_speed": 2.0, "total_duration_minutes": 58.0,
                         "ais_gap_count": 0}),
             _name="禁渔区内低速滞留（该命中）", _expect="hit"),
        dict(_f(outside, {"avg_speed": 12.0, "total_duration_minutes": 58.0,
                          "ais_gap_count": 0}),
             _name="区外高速通过（不该命中）", _expect="none"),
        dict(_f([], {}), _name="空轨迹（不该命中）", _expect="none"),
    ]


# 试跑默认样本。模块级算一次即可：纯 Python 构造 60 个点，微秒级，
# 且没有任何外部依赖（facts.py 被 sandbox_runner 排除在导入链之外）。
SAMPLE_FACTS = sample_facts()
