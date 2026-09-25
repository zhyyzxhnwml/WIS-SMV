"""
shipclaw/evidence.py — 违规证据链构建（技能与 Web API 共用）
================================================================

【这个文件做什么？】
  从 detection_results 行 + detailed_result 构建结构化证据数据，
  供两处使用：
    1. ViolationEvidenceSkill（悬浮窗智能体"违规证据链"技能）
    2. Web API /api/v1/violations/<id>/evidence（违规菜单证据可视化）

  证据数据与前端 EvidenceCard.vue 的渲染契约一致：
    { mmsi, ship_type, violated, verdict{}, track[], events[], stats{},
      regulation, final_assessment, recommended_action, annotations[] }
"""

from __future__ import annotations

import json
from typing import Any, Optional


def _sample(points: list, n: int = 300) -> list:
    """轨迹点过多时按步长采样，避免前端 JSON 过大。"""
    if len(points) <= n:
        return points
    step = len(points) / n
    return [points[int(i * step)] for i in range(n)]


def _fmt_time(t) -> str:
    """ISO 时间 → 'MM-DD HH:MM'。"""
    s = str(t or "")
    if len(s) >= 16:
        return s[5:10] + " " + s[11:16]
    return s


def _describe_code(raw: str) -> str:
    """违规/事件代码 → 中文标签。"""
    try:
        from ais_accelerator.violation_catalog import describe
        return describe(raw)["label"]
    except Exception:
        return raw or ""


def _build_gap_context(ev: dict, raw_track: list) -> Optional[dict]:
    """AIS 中断类事件补上下文：证据点通常只存"中断恢复后首点"。

    用该点时间戳在原始轨迹中定位，取前一个点作为"中断前最后信号点"，
    配合 features.gap_minutes 构成完整证据链（中断前 → 中断 N 分钟 → 恢复后）。
    非中断类事件返回 None。
    """
    evpts = ev.get("evidence_points") or []
    if not evpts:
        return None
    feats = ev.get("features") or {}
    gap_min = feats.get("gap_minutes")
    if gap_min is None:
        return None
    ts = evpts[0].get("timestamp")
    idx = None
    if ts:
        for i, p in enumerate(raw_track):
            if (p.get("timestamp") or "") == ts:
                idx = i
                break
    pre = raw_track[idx - 1] if (idx is not None and idx > 0) else None
    return {
        "gap_minutes": gap_min,
        "pre_point": pre,
        "post_point": evpts[0],
    }


def build_evidence_data(mmsi: str, row: dict, detail: dict) -> dict:
    """构建结构化证据数据（EvidenceCard 渲染契约）。

    Args:
        mmsi: 船舶 MMSI
        row: detection_results 行（violation_detected/violation_type/risk_level/
             confidence/quality_level/regulation_ref/final_assessment/
             recommended_action/annotations 等字段）
        detail: detailed_result 解析后的 dict（ship/track/events/feature_metadata）

    Returns:
        证据数据 dict（见模块 docstring）
    """
    ship = detail.get("ship") or {}
    raw_track = detail.get("track") or []
    track = _sample(raw_track)
    events = detail.get("events") or []
    fm = detail.get("feature_metadata") or {}
    vtype = row.get("violation_type") or ""
    label = _describe_code(vtype) or row.get("violation_label") or ""

    def _num(v, d=0.0):
        try:
            return float(v) if v is not None else d
        except (TypeError, ValueError):
            return d

    ev_list = []
    for ev in events[:10]:
        evpts = ev.get("evidence_points") or []
        feats = ev.get("features") or {}
        entry = {
            "type": ev.get("candidate_type") or "",
            "type_label": _describe_code(ev.get("candidate_type") or ""),
            "zone_name": ev.get("zone_name") or ev.get("zone_id") or "未知区域",
            "start_time": _fmt_time(ev.get("start_time")),
            "end_time": _fmt_time(ev.get("end_time")),
            "avg_speed": _num(feats.get("average_speed")),
            "max_speed": _num(feats.get("max_speed")),
            "duration_minutes": _num(feats.get("duration_minutes")),
            "evidence_points": [
                {
                    "lat": p.get("lat"), "lon": p.get("lon"),
                    "speed": p.get("speed"), "course": p.get("course"),
                    "timestamp": p.get("timestamp") or p.get("time") or "",
                } for p in evpts
            ],
        }
        gap_ctx = _build_gap_context(ev, raw_track)
        if gap_ctx:
            entry["gap_minutes"] = gap_ctx["gap_minutes"]
            entry["pre_point"] = gap_ctx["pre_point"]
            entry["post_point"] = gap_ctx["post_point"]
        ev_list.append(entry)

    risk = (row.get("risk_level") or "low").lower()
    if risk == "middle":
        risk = "mid"

    data = {
        "mmsi": mmsi,
        "ship_type": ship.get("ship_type") or "",
        "violated": bool(row.get("violation_detected")),
        "verdict": {
            "violation_type": vtype,
            "violation_label": label,
            "risk_level": risk,
            "confidence": _num(row.get("confidence")),
            # quality_report.level 是 @property 取不到，必须用表列
            "quality_level": row.get("quality_level") or "",
        },
        "track": [{
            "lat": p.get("lat"), "lon": p.get("lon"),
            "speed": p.get("speed"), "course": p.get("course"),
            "timestamp": p.get("timestamp") or p.get("time") or "",
        } for p in track],
        "events": ev_list,
        "stats": {
            "avg_speed": _num(fm.get("avg_speed")),
            "max_speed": _num(fm.get("max_speed")),
            "track_points": len(track),
        },
        "regulation": row.get("regulation_ref") or "",
        "final_assessment": (row.get("final_assessment") or "").strip(),
        "recommended_action": (row.get("recommended_action") or "").strip(),
    }
    try:
        anns = json.loads(row["annotations"]) if row.get("annotations") else []
        data["annotations"] = sorted({
            a.get("label") for a in anns
            if (a.get("label") or "") and a.get("type") in (
                "speed_violation", "low_speed_fishing", "restricted_area_entry",
                "ais_gap", "position_jump", "course_anomaly", "rule_hit",
            )
        })[:8]
    except Exception:
        data["annotations"] = []
    return data


def load_evidence_for_record(vid: int, scope_sql: str = "", scope_params: tuple = ()) -> Optional[dict]:
    """按 detection_results.id 加载证据数据（违规菜单证据可视化用）。

    Args:
        vid: detection_results 主键
        scope_sql / scope_params: 用户数据隔离条件（app._user_scope() 的输出）

    Returns:
        证据数据 dict；记录不存在或非违规记录返回 None
    """
    import pg_db

    conn = pg_db.connect()
    try:
        r = conn.execute(
            f"SELECT * FROM detection_results WHERE id = ? AND violation_detected = 1"
            f"{(' AND ' + scope_sql) if scope_sql else ''}",
            (vid,) + tuple(scope_params),
        ).fetchone()
    finally:
        conn.close()
    if not r:
        return None
    row = dict(r)
    try:
        detail = json.loads(row.get("detailed_result") or "{}")
    except (json.JSONDecodeError, TypeError):
        detail = {}
    return build_evidence_data(row.get("mmsi", ""), row, detail)


def evidence_completeness(data: dict) -> dict:
    """证据充分度评估（违规菜单"证据充分度"指标条用）。

    Returns:
        { hasTrack, trackPoints, eventCount, evidencePoints, coverageHours,
          hasRegulation, hasAssessment, confidence }
    """
    track = data.get("track") or []
    events = data.get("events") or []
    ev_points = sum(len(e.get("evidence_points") or []) for e in events)

    # 轨迹覆盖时长（首尾时间差）
    coverage_hours = 0.0
    if len(track) >= 2:
        try:
            from datetime import datetime
            t0 = _parse_iso(track[0].get("timestamp"))
            t1 = _parse_iso(track[-1].get("timestamp"))
            if t0 and t1:
                coverage_hours = round((t1 - t0).total_seconds() / 3600, 1)
        except Exception:
            pass

    return {
        "hasTrack": len(track) >= 2,
        "trackPoints": len(track),
        "eventCount": len(events),
        "evidencePoints": ev_points,
        "coverageHours": coverage_hours,
        "hasRegulation": bool((data.get("regulation") or "").strip()),
        "hasAssessment": bool((data.get("final_assessment") or "").strip()),
        "confidence": (data.get("verdict") or {}).get("confidence", 0),
    }


def _parse_iso(s: str):
    """'2021-07-28T09:28:29Z' / '2021-07-28 09:28:29' → datetime（失败 None）。"""
    if not s:
        return None
    s = str(s).replace("Z", "").replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            from datetime import datetime
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None
