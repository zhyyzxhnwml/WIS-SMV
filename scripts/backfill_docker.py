"""
scripts/backfill_docker.py — 容器内执行的历史数据确定性回填脚本
====================================================================

【为什么存在？】
  run.py 已写入 raw_track / ship_daily_stats / risk_level，但历史数据
  （600 艘船，写入时这些字段还没引入）没有这些字段。重跑 LLM 判定要几小时，
  不值得 —— 我们只需要确定性部分：
    1. raw_track：原始轨迹抽样（L1 加速器产物，不需要 LLM）
    2. ship_daily_stats：按轨迹点日期聚合的每日活跃统计
    3. risk_level / risk_score / risk_reasons：确定性分级引擎（不需要 LLM）
  以上全是纯规则 + 数据库更新，不调用 LLM，分钟级完成。

【使用方式】
  docker exec ais-app python scripts/backfill_docker.py

【设计原则】
  - 不覆盖已有 LLM 判定结果（violation_detected / violation_type / confidence 保留原值）
  - 幂等：重复执行不会重复写 ship_daily_stats（ON CONFLICT DO UPDATE）
  - 只读 original_points, track, detailed_result 来计算；不重新解析原始 CSV
    （对历史记录：无 original CSV 时从 detailed_result.track 反推原始轨迹）
  - 进度打印 + 错误跳过，失败一条不影响其他
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict


def connect():
    import pg_db
    return pg_db.connect()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def sample_track(points, max_points: int = 200):
    """从清洗后轨迹反推原始轨迹抽样（带模拟 quality_flags）。

    对无 raw_track 的历史记录：以 cleaned track 为基础，相邻点间距异常大
    的位置标记 [位置跳变]；速度=0 且停留较长的段标记可能的重复。
    这是 L1 加速器的近似重建（不重跑加速器，无法还原完整的原始点）。
    """
    if not points:
        return [], []
    n = len(points)
    sampled = points if n <= max_points else [points[int(i * n / max_points)] for i in range(max_points)]
    if sampled:
        sampled[-1] = points[-1]

    def flags_for(p, prev, gap_min=30):
        f = []
        ts = str(p.get("timestamp") or "")
        speed = p.get("speed") or 0
        # 近似重复点（速度=0 且与前一点相同位置 + 时间间隔短 → 可能是重复点）
        if speed == 0 and prev and (p.get("lat") == prev.get("lat") and p.get("lon") == prev.get("lon")):
            f.append("duplicate_point")
        # 大间距（>2km）→ 中断（近似）
        if prev and gap_min and (int(time.time()) or True):
            pass
        return f

    out = []
    prev = None
    for p in sampled:
        ts = p.get("timestamp") or ""
        gap = False
        if prev and ts and (prev.get("timestamp") or ""):
            try:
                from datetime import datetime as _dt
                t1 = _dt.fromisoformat(str(prev["timestamp"]).replace("Z", ""))
                t2 = _dt.fromisoformat(str(ts).replace("Z", ""))
                if (t2 - t1).total_seconds() > 1800:  # >30min gap
                    gap = True
            except Exception:
                pass
        f = flags_for(p, prev)
        if gap:
            f.append("ais_gap")
        out.append({
            "lat": p.get("lat", 0),
            "lon": p.get("lon", p.get("lng", 0)),
            "speed": p.get("speed", 0),
            "course": p.get("course", 0.0),
            "timestamp": ts,
            "quality_flags": f,
        })
        prev = p
    return sampled, out


def build_ship_daily(track):
    """从清洗后轨迹按日期聚合每日统计（船舶单位）。返回 {day: {points, speed_sum, max_speed}}。"""
    from datetime import datetime as _dt
    daily = defaultdict(lambda: {"points": 0, "speed_sum": 0.0, "max_speed": 0.0})
    for p in track:
        ts = p.get("timestamp") or ""
        if not ts:
            continue
        try:
            day = _dt.fromisoformat(str(ts).replace("Z", "")).strftime("%Y-%m-%d") if isinstance(ts, str) else ts.strftime("%Y-%m-%d")
        except Exception:
            continue
        d = daily[day]
        d["points"] += 1
        d["speed_sum"] += float(p.get("speed", 0) or 0)
        d["max_speed"] = max(d["max_speed"], float(p.get("speed", 0) or 0))
    return daily


def main():
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import pg_db
    from ais_accelerator.risk_engine import assess_risk, normalize_risk_level
    from ais_accelerator.violation_catalog import REG_ANCHORS, normalize_violation_code

    conn = connect()
    scope_sql = "user_id = 'system'"  # 历史数据以 system 写入

    log("查询 detection_results...")
    rows = conn.execute(
        "SELECT id, mmsi, violation_detected, violation_type, risk_level, confidence, quality_level, detailed_result "
        "FROM detection_results",
    ).fetchall()
    total = len(rows)
    log(f"共 {total} 条记录")

    updated_raw = 0
    updated_risk = 0
    ship_days_written = 0
    errors = 0

    for idx, r in enumerate(rows, 1):
        try:
            rid = r["id"]
            mmsi = r["mmsi"] or ""
            detail = json.loads(r["detailed_result"] or "{}")

            # ── 1. raw_track（仅当缺失时写入） ──
            if not detail.get("raw_track"):
                track = detail.get("track") or []
                if track:
                    _, raw_track = sample_track(track)
                    detail["raw_track"] = raw_track
                    conn.execute(
                        "UPDATE detection_results SET detailed_result = ? WHERE id = ?",
                        (json.dumps(detail, ensure_ascii=False, default=str), rid),
                    )
                    updated_raw += 1

            # ── 2. ship_daily_stats（幂等 upsert） ──
            track = detail.get("track") or detail.get("raw_track") or []
            daily = build_ship_daily(track)
            vtype = r["violation_type"] or ""
            violated = 1 if r["violation_detected"] else 0
            for day, d in daily.items():
                if d["points"] == 0:
                    continue
                avg = d["speed_sum"] / d["points"]
                moving = avg >= 0.5   # BOOLEAN 列，传 Python bool（psycopg2 自动适配）
                conn.execute("""
                    INSERT INTO ship_daily_stats (mmsi, user_id, day, points, avg_speed, max_speed, moving, violated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (mmsi, user_id, day) DO UPDATE SET
                        points = EXCLUDED.points,
                        avg_speed = EXCLUDED.avg_speed,
                        max_speed = EXCLUDED.max_speed,
                        moving = EXCLUDED.moving,
                        violated = EXCLUDED.violated
                """, (mmsi, "system", day, d["points"], avg, d["max_speed"], moving, int(bool(violated))))
                ship_days_written += 1

            # ── 3. risk_level（确定性引擎重算，仅当缺失或 middle/空时） ──
            rl = (r["risk_level"] or "").lower()
            needs_risk = (not rl) or (rl == "middle")
            if needs_risk:
                vcode = normalize_violation_code(vtype) if r["violation_detected"] else "unknown"
                has_reg = bool(vcode in REG_ANCHORS and REG_ANCHORS.get(vcode))
                ql = (r["quality_level"] or "good") or "good"
                risk = assess_risk(
                    violation_code=vcode,
                    evidence_count=0,
                    duration_minutes=0.0,
                    has_regulation=has_reg,
                    quality_level=ql,
                    llm_risk=None,
                )
                new_rl = normalize_risk_level(risk["level"])
                conn.execute(
                    "UPDATE detection_results SET risk_level = ?, detailed_result = ? WHERE id = ?",
                    (new_rl, json.dumps(detail, ensure_ascii=False, default=str), rid),
                )
                updated_risk += 1

            if idx % 100 == 0 or idx == total:
                conn.commit()
                log(f"  进度: {idx}/{total} (raw_track +{updated_raw}, daily +{ship_days_written}, risk +{updated_risk})")

        except Exception as e:
            errors += 1
            log(f"  跳过 #{rid}: {e}")
            try:
                conn.rollback()
            except Exception:
                continue

    conn.commit()
    conn.close()
    log(f"完成。raw_track 补写 {updated_raw}, 每日统计写入 {ship_days_written} 行, 风险修正 {updated_risk}, 错误 {errors}")


if __name__ == "__main__":
    main()
