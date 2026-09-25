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


def _index_of_ts(track: list, ts: str) -> Optional[int]:
    """在轨迹里找某个时间戳的下标（找不返回 None）。"""
    if not ts:
        return None
    for i, p in enumerate(track):
        if (p.get("timestamp") or p.get("time") or "") == ts:
            return i
    return None


def _shift_iso(iso: str, minutes: float) -> str:
    """ISO 时间平移若干分钟；解析失败返回空串。"""
    dt = _parse_iso(iso)
    if dt is None:
        return ""
    from datetime import timedelta
    return (dt + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _speeds_in_window(track: list, start_iso: str, end_iso: str):
    """[start, end] **内部**（不含两端）轨迹点的 (平均, 最高) 航速。

    区间内取不到点 → (None, None)。AIS 中断类事件必然落进这一支：空档里本来
    就没有点 —— 而那正是"中断"这件事本身的证明。
    """
    if not start_iso or not end_iso:
        return None, None
    vals = []
    for p in track:
        ts = p.get("timestamp") or p.get("time") or ""
        if start_iso < ts < end_iso:
            try:
                vals.append(float(p.get("speed")))
            except (TypeError, ValueError):
                continue
    if not vals:
        return None, None
    return round(sum(vals) / len(vals), 1), round(max(vals), 1)


def _speeds_around(track: list, pre_iso: str, post_iso: str, n: int = 20):
    """空档两侧各取 n 个点的 (平均, 最高) 航速。

    features 里没有速度字段、区间内又取不到点时用它兜底：取"信号消失前最后
    n 个点 + 恢复后最初 n 个点"，回答的是"它消失时和在做什么"。返回值会带上
    speed_scope='context'，让前端如实标注口径，不冒充"区间均速"。
    """
    if not track:
        return None, None
    i_pre = _index_of_ts(track, pre_iso)
    i_post = _index_of_ts(track, post_iso)
    if i_pre is None and i_post is None:
        return None, None
    idxs: list[int] = []
    if i_pre is not None:
        idxs += range(max(0, i_pre - n + 1), i_pre + 1)
    if i_post is not None:
        idxs += range(i_post, min(len(track), i_post + n))
    vals = []
    for i in sorted(set(idxs)):
        try:
            vals.append(float(track[i].get("speed")))
        except (TypeError, ValueError):
            continue
    if not vals:
        return None, None
    return round(sum(vals) / len(vals), 1), round(max(vals), 1)


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

    def _opt_num(v):
        """同 _num，但字段缺失返回 None —— 0.0 与"没有这个量"在界面上必须分得开。"""
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    ev_list = []
    for ev in events[:10]:
        evpts = ev.get("evidence_points") or []
        feats = ev.get("features") or {}
        gap_ctx = _build_gap_context(ev, raw_track)
        gap_min = _opt_num((gap_ctx or {}).get("gap_minutes"))

        post_pt = (gap_ctx or {}).get("post_point") or {}
        pre_pt = (gap_ctx or {}).get("pre_point") or {}
        post_iso = post_pt.get("timestamp") or post_pt.get("time") or ""
        pre_iso = pre_pt.get("timestamp") or pre_pt.get("time") or ""

        start_iso = (ev.get("start_time") or "").strip()
        end_iso = (ev.get("end_time") or "").strip()
        if gap_ctx:
            # 中断类事件的 start/end 是**退化**的：两者都等于"信号恢复时刻"，
            # 界面上就成了 "20:17 ~ 20:17"。真实起点优先取中断前最后一个实测点
            # （pre_point.timestamp），取不到再按 "恢复时刻 − 中断时长" 反推。
            end_iso = post_iso or end_iso
            start_iso = pre_iso or _shift_iso(post_iso, -(gap_min or 0.0)) or start_iso

        # 持续时长：中断类事件的 features 里**只有 gap_minutes、没有 duration_minutes**，
        # 直接取会显示"持续 0 分"（真实 680 分）
        dur = _opt_num(feats.get("duration_minutes"))
        if (dur is None or dur <= 0) and gap_min:
            dur = gap_min

        # 航速：中断类事件的 features 里同样没有速度字段，取不到就显示 0.0 节（假数据）。
        # 依次尝试 features → 区间内实测 → 空档两侧 n 点，都取不到才留 None（前端显示 '-'）
        avg_sp = _opt_num(feats.get("average_speed"))
        max_sp = _opt_num(feats.get("max_speed"))
        speed_scope = ""
        if avg_sp is None and max_sp is None:
            avg_sp, max_sp = _speeds_in_window(raw_track, start_iso, end_iso)
            if avg_sp is not None or max_sp is not None:
                speed_scope = "window"
            else:
                avg_sp, max_sp = _speeds_around(raw_track, pre_iso, post_iso)
                speed_scope = "context" if avg_sp is not None else ""

        entry = {
            "type": ev.get("candidate_type") or "",
            "type_label": _describe_code(ev.get("candidate_type") or ""),
            "zone_name": ev.get("zone_name") or ev.get("zone_id") or "未知区域",
            "start_time": _fmt_time(start_iso),
            "end_time": _fmt_time(end_iso),
            # 完整 ISO：前端画时间轴要靠它算绝对时刻，_fmt_time 只到分钟、也不带年份
            "start_ts": start_iso,
            "end_ts": end_iso,
            "avg_speed": avg_sp,
            "max_speed": max_sp,
            "speed_scope": speed_scope,
            "duration_minutes": dur,
            "evidence_points": [
                {
                    "lat": p.get("lat"), "lon": p.get("lon"),
                    "speed": p.get("speed"), "course": p.get("course"),
                    "timestamp": p.get("timestamp") or p.get("time") or "",
                } for p in evpts
            ],
        }
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
