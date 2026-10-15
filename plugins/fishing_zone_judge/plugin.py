---
name: fishing_zone_judge
label: 禁渔区捕捞判定
icon: 🐟
description: 依据区域限制与区内低速滞留特征，判定禁渔区/禁航区内的捕捞嫌疑
hooks: adjudicate
trust: sandbox
spec: v1
timeout_ms: 1200
max_findings: 5
can_raise_risk: false
min_level: 1
trigger_keywords: 禁渔区, 捕捞, 违法捕捞, 违规作业, 禁航区停留
---
"""禁渔区捕捞判定 —— facts-only、纯函数、零副作用。

【它为什么长这样】
  L1（detect.py）已经能报出 restricted_area_entry / illegal_fishing，但它按
  单个区域、单条规则工作，判完之后没有"这条船在这个区里待了多久、慢了多少"的
  整体刻画。这个插件补的就是这句话：把 L1 的离散事件收敛成一条可读的结论。

【它绝不做什么】
  不写 violation_code / risk_level / violation_detected —— 那三列是 L1 实测证据
  与 risk_engine 的确定性结论，插件只有只读的 facts.base。想升风险必须走
  raise_risk_to，而本插件 manifest 里 can_raise_risk 是 false。
"""
import datetime as _dt

PLUGIN = {"name": "fishing_zone_judge", "entry_tool": "adjudicate"}
DEFAULTS = {
    "low_speed_knots": 3.0,        # 低于此速视为作业中
    "min_dwell_minutes": 20.0,     # 区内总停留时长门槛
    "min_points": 6,               # 区内/低速点数门槛，防两三个点就下结论
    "confidence_base": 0.55,
    "min_confidence": 0.6,         # 低于此不产出，宁缺勿滥
}
_TIME_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")


def apply(ctx):
    """dsh 风格入口：只做注册，注册即登记撤销方式（由 PluginContext 收集）。"""
    cfg = dict(DEFAULTS)
    cfg.update(ctx.config or {})
    ctx.tools.register(
        name="adjudicate",
        description="判定禁渔区/禁航区内的捕捞嫌疑",
        input_schema={"type": "object", "required": ["facts_version"]},
        handler=lambda facts: _judge(facts, cfg),
    )
    ctx.log.info("fishing_zone_judge 已注册，低速门槛 %.1f 节，停留门槛 %.0f 分钟"
                 % (cfg["low_speed_knots"], cfg["min_dwell_minutes"]))


def _judge(facts, cfg):
    if str(facts.get("facts_version") or "") != "v1":
        return []
    zones = facts.get("zones") or []
    points = (facts.get("track") or {}).get("points") or []
    events = facts.get("l1_events") or []
    if not zones or not points:
        return []

    out = []
    for zone in zones:
        restrictions = zone.get("restrictions") or {}
        # 只对「明确限制了捕捞或进入」的区域下结论；限制未知的区域不下结论
        if restrictions.get("fishing_allowed") is not False and \
                restrictions.get("entry_allowed") is not False:
            continue

        inside = [p for p in points if _in_bbox(p, zone.get("bbox"))]
        if len(inside) < cfg["min_points"]:
            continue
        dwell = _dwell_minutes(inside)
        if dwell < cfg["min_dwell_minutes"]:
            continue
        slow = [p for p in inside if _f(p.get("sog")) <= cfg["low_speed_knots"]]
        if len(slow) < cfg["min_points"]:
            continue

        conf = cfg["confidence_base"] + 0.02 * len(slow) + min(0.25, dwell / 240.0)
        hit_types = [str(e.get("candidate_type") or "") for e in events
                     if str(e.get("zone_id") or "") in
                     (str(zone.get("id") or ""), str(zone.get("name") or ""))]
        # L1 已经报过事件则上调可信度。认的是**区内**那两个码：
        # illegal_fishing 是禁渔区/禁渔期内的法定违规（detect 的禁渔区分支现在产它），
        # low_speed_fishing 现在只用于**开阔水域**的作业嫌疑、不带 zone_id，
        # 所以它在这份"按 zone_id 过滤"的 hit_types 里其实筛不出来——留着它不误伤，
        # 但真正会命中的是 illegal_fishing 与 restricted_area_entry。
        if any(t in ("restricted_area_entry", "illegal_fishing", "low_speed_fishing")
               for t in hit_types):
            conf = conf + 0.10          # L1 已经报过事件，可信度上调
        conf = min(0.95, conf)
        if conf < cfg["min_confidence"]:
            continue

        zone_name = zone.get("name") or zone.get("id") or "受限区域"
        out.append({
            "code": "fishing_in_restricted_zone",
            "label": "%s内捕捞嫌疑" % zone_name,
            "confidence": round(conf, 2),
            "severity": "high" if conf >= 0.8 else "mid",
            "evidence": "%s内停留 %.0f 分钟，区内轨迹点 %d 个，其中低速(<%.0f节) %d 个%s"
                        % (zone_name, dwell, len(inside), cfg["low_speed_knots"], len(slow),
                           ("，L1 已报 " + "/".join(sorted(set(hit_types)))) if hit_types else ""),
            "metric": round(dwell, 1),
            "metric_unit": "分钟",
            "span": [str(inside[0].get("ts") or ""), str(inside[-1].get("ts") or "")],
            "regulation_ref": restrictions.get("regulation_ref") or "《渔业法》相关条款",
        })
        if len(out) >= 5:
            break
    return out


def _f(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _in_bbox(point, bbox):
    if not bbox or len(bbox) < 4:
        return False
    lon, lat = _f(point.get("lon")), _f(point.get("lat"))
    return bbox[0] <= lon <= bbox[2] and bbox[1] <= lat <= bbox[3]


def _dwell_minutes(points):
    stamps = sorted(s for s in (_parse_ts(p.get("ts")) for p in points) if s)
    if len(stamps) < 2:
        return 0.0
    return max(0.0, (stamps[-1] - stamps[0]).total_seconds() / 60.0)


def _parse_ts(raw):
    text = str(raw or "")[:19]
    for fmt in _TIME_FORMATS:
        try:
            return _dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
