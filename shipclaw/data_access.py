"""
shipclaw/data_access.py — 技能公共数据访问层
==============================================

【这个文件做什么？】
  把现有技能里重复的 pg_db 查询收敛成统一函数，供三个渲染型技能
  （chart_render / table_render / doc_render）以及其它技能共用。
  所有查询都带 user_id 数据隔离（pg_db.user_scope）。

【函数列表】
  _scope(user_id)                   -> (scope_sql, params)
  q_overview(user_id)               -> {ships, total, violations, ...}
  q_violation_types(user_id)        -> [{code, label, count}]
  q_risk_distribution(user_id)      -> [{level, label, count}]
  q_daily_trend(user_id, days=14)   -> [{day, count}]
  q_violation_list(user_id, limit)  -> [{mmsi, type, risk, time}]
  q_ship_list(user_id, limit)       -> [{mmsi, status, time}]
  q_top_violators(user_id, limit)   -> [{mmsi, count}]
"""

from __future__ import annotations

from typing import Any, Optional


def _scope(user_id: str):
    """获取用户数据隔离条件。"""
    import pg_db
    conn = pg_db.connect()
    scope_sql, scope_params = pg_db.user_scope(conn, user_id)
    conn.close()
    return scope_sql, scope_params


def _norm_risk(rl: Optional[str]) -> str:
    """归一化风险等级。"""
    s = (rl or "").strip().lower()
    if s in ("high",):
        return "high"
    if s in ("mid", "middle", "medium"):
        return "mid"
    return "low"


def q_overview(user_id: str) -> dict[str, Any]:
    """辖区总览统计。"""
    import pg_db
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    from_scope = f" WHERE {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]
        violations = conn.execute(
            f"SELECT COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope}", scope_params
        ).fetchone()["c"]
        ships = conn.execute(
            f"SELECT COUNT(DISTINCT mmsi) as c FROM detection_results{from_scope}", scope_params
        ).fetchone()["c"]
        vships = conn.execute(
            f"SELECT COUNT(DISTINCT mmsi) as c FROM detection_results WHERE violation_detected=1{and_scope}", scope_params
        ).fetchone()["c"]
    finally:
        conn.close()
    return {
        "ships": ships or 0,
        "violation_ships": vships or 0,
        "total_records": total or 0,
        "violations": violations or 0,
    }


def q_violation_types(user_id: str) -> list[dict[str, Any]]:
    """按规范码聚合的违规类型分布（中文标签 + 数量）。"""
    import pg_db
    from ais_accelerator.violation_catalog import describe, normalize_violation_code
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT COALESCE(NULLIF(violation_type,''),'unknown') as raw_type, COUNT(*) as c "
            f"FROM detection_results WHERE violation_detected=1{and_scope} GROUP BY raw_type",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    agg: dict[str, int] = {}
    for r in rows:
        code = normalize_violation_code(r["raw_type"])
        agg[code] = agg.get(code, 0) + r["c"]
    out = []
    for code, count in sorted(agg.items(), key=lambda kv: -kv[1]):
        info = describe(code)
        out.append({"code": code, "label": info["label"], "count": count})
    return out


def q_risk_distribution(user_id: str) -> list[dict[str, Any]]:
    """风险等级分布。"""
    import pg_db
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT risk_level, COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope} GROUP BY risk_level",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    level_order = {"high": 0, "mid": 1, "middle": 1, "low": 2}
    level_label = {"high": "高风险", "mid": "中风险", "middle": "中风险", "low": "低风险"}
    out = []
    for r in rows:
        rl = _norm_risk(r["risk_level"])
        out.append({"level": rl, "label": level_label.get(rl, "低风险"), "count": r["c"]})
    out.sort(key=lambda x: level_order.get(x["level"], 9))
    return out


def q_daily_trend(user_id: str, days: int = 14) -> list[dict[str, Any]]:
    """违规日趋势。"""
    import pg_db
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT SUBSTR(analysis_time,1,10) as day, COUNT(*) as c "
            f"FROM detection_results WHERE violation_detected=1{and_scope} "
            f"GROUP BY day ORDER BY day LIMIT {int(days)}",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    return [{"day": r["day"], "count": r["c"]} for r in rows]


def q_violation_list(user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """最近违规记录列表。"""
    import pg_db
    from ais_accelerator.violation_catalog import describe
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT mmsi, violation_type, risk_level, analysis_time "
            f"FROM detection_results WHERE violation_detected=1{and_scope} "
            f"ORDER BY analysis_time DESC LIMIT {int(limit)}",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        info = describe(r["violation_type"] or "")
        out.append({
            "mmsi": r["mmsi"],
            "violation_type": info["label"],
            "risk_level": _norm_risk(r["risk_level"]),
            "analysis_time": (r["analysis_time"] or "")[:16],
        })
    return out


def q_ship_list(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """船舶列表（最近分析）。"""
    import pg_db
    scope_sql, scope_params = _scope(user_id)
    from_scope = f" WHERE {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT mmsi, MAX(analysis_time) as analysis_time, "
            f"MAX(CASE WHEN violation_detected=1 THEN 1 ELSE 0 END) as has_violation "
            f"FROM detection_results{from_scope} "
            f"GROUP BY mmsi ORDER BY analysis_time DESC LIMIT {int(limit)}",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        out.append({
            "mmsi": r["mmsi"],
            "status": "违规" if r["has_violation"] else "正常",
            "analysis_time": (r["analysis_time"] or "")[:16],
        })
    return out


def q_top_violators(user_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """多次违规船舶 Top N。"""
    import pg_db
    scope_sql, scope_params = _scope(user_id)
    and_scope = f" AND {scope_sql}" if scope_sql else ""
    conn = pg_db.connect()
    try:
        rows = conn.execute(
            f"SELECT mmsi, COUNT(*) as c FROM detection_results "
            f"WHERE violation_detected=1{and_scope} GROUP BY mmsi HAVING COUNT(*) > 1 "
            f"ORDER BY c DESC LIMIT {int(limit)}",
            scope_params,
        ).fetchall()
    finally:
        conn.close()
    return [{"mmsi": r["mmsi"], "count": r["c"]} for r in rows]
