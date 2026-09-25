"""
memory/ship_memory.py — 船舶记忆（Ship Memory）
================================================

【这个文件做什么？】
  记住每一艘船的历史行为，支持"惯犯识别"和"趋势跟踪"。
  使用 SQLite 持久化存储，每次处理船舶时读取/写入。

【解决的问题】
  同一艘船在多个 CSV 文件中反复出现时，LLM 不用每次都从零判断。
  支持识别"惯犯"（累计违规 3 次以上）和行为趋势分析。

【表结构 ship_memory】
  - mmsi: 船舶 ID（主键）
  - first_seen / last_seen: 首次/最近出现时间
  - total_appearances: 累计出现次数
  - total_violations: 累计违规次数
  - violation_types: JSON 格式的违规类型计数
  - last_judgment / last_assessment: 最近一次 LLM 判定结果
  - last_confidence / last_risk_level / last_recommended_action: 最近判定详情
  - risk_trend: 风险趋势（escalating / stable / improving）
  - recent_events: 最近 5 次事件的简短记录（JSON）
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# 默认 SQLite 数据库路径
DEFAULT_DB_PATH = Path("data/memory/ship_memory.db")


class ShipMemory:
    """船舶记忆 —— 持久化存储每艘船的历史行为。

    用法:
        ship_mem = ShipMemory()
        history = ship_mem.get_history("412345678")
        ship_mem.update_after_judgment(mmsi="412345678", llm_result={...})
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 数据库初始化 ──

    def _init_db(self) -> None:
        """创建 ship_memory 表（如不存在）。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ship_memory (
                    mmsi TEXT PRIMARY KEY,
                    first_seen TIMESTAMP,
                    last_seen TIMESTAMP,
                    total_appearances INTEGER DEFAULT 1,

                    total_violations INTEGER DEFAULT 0,
                    violation_types TEXT DEFAULT '{}',

                    last_judgment TEXT,
                    last_assessment TEXT,
                    last_confidence REAL,
                    last_risk_level TEXT DEFAULT 'low',
                    last_recommended_action TEXT DEFAULT 'monitor',

                    risk_trend TEXT DEFAULT 'stable',

                    recent_events TEXT DEFAULT '[]',

                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    # ── 读取 ──

    def get_history(self, mmsi: str) -> Optional[dict[str, Any]]:
        """查询某艘船的历史记录。

        Args:
            mmsi: 船舶 MMSI 号

        Returns:
            历史记录字典，如果不存在则返回 None
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM ship_memory WHERE mmsi = ?", (mmsi,)
            )
            row = cur.fetchone()
        if row is None:
            return None
        return dict(row)

    def get_history_text(self, mmsi: str) -> str:
        """生成一段可注入 LLM prompt 的船舶历史文本。

        Args:
            mmsi: 船舶 MMSI 号

        Returns:
            格式化的历史记录文本，如无历史则返回空字符串
        """
        history = self.get_history(mmsi)
        if history is None:
            return ""

        parts = ["【该船历史记录】"]
        parts.append(f"  首次出现: {history.get('first_seen', '未知')}")
        parts.append(f"  最近出现: {history.get('last_seen', '未知')}")
        parts.append(f"  累计出现: {history['total_appearances']} 次")

        violations = history.get("total_violations", 0)
        vtypes = json.loads(history.get("violation_types", "{}"))
        if violations > 0:
            vtype_str = "、".join(
                f"{k} {v}次" for k, v in sorted(vtypes.items(), key=lambda x: -x[1])
            )
            parts.append(f"  历史违规: {violations} 次（{vtype_str}）")
        else:
            parts.append(f"  历史违规: 无")

        assessment = history.get("last_assessment")
        if assessment:
            parts.append(f"  最近结论: {assessment[:100]}")

        trend = history.get("risk_trend", "stable")
        trend_map = {
            "escalating": "⚠ 风险上升（违规频率增加）",
            "improving": "✓ 趋于改善（近期正常）",
            "stable": "→ 状态稳定",
        }
        parts.append(f"  风险趋势: {trend_map.get(trend, trend)}")

        events = json.loads(history.get("recent_events", "[]"))
        if events:
            parts.append("  最近事件:")
            for ev in events[-5:]:
                parts.append(f"    · {ev.get('time', '')} {ev.get('summary', '')}")

        parts.append("")
        parts.append("请综合历史记录和当前数据做出判断")
        return "\n".join(parts)

    # ── 写入 ──

    def update_after_judgment(
        self,
        mmsi: str,
        llm_result: dict[str, Any],
        zone: str = "",
        scenario: str = "",
    ) -> None:
        """LLM 判定完成后，更新船舶记忆。

        Args:
            mmsi: 船舶 MMSI 号
            llm_result: LLM 判定结果字典
            zone: 当前区域（可选，用于事件记录）
            scenario: 当前行为类型（可选）
        """
        now = datetime.now().isoformat()
        violation = llm_result.get("violation_detected", False)
        vtype = llm_result.get("violation_type") or "unknown"
        assessment = llm_result.get("final_assessment", "")
        confidence = llm_result.get("confidence", 0)
        risk = llm_result.get("risk_level", "low")
        action = llm_result.get("recommended_action", "monitor")

        with sqlite3.connect(str(self.db_path)) as conn:
            # 先查已有记录
            cur = conn.execute("SELECT * FROM ship_memory WHERE mmsi = ?", (mmsi,))
            existing = cur.fetchone()

            if existing:
                # 更新已有记录
                total_appearances = existing[3] + 1
                total_violations = existing[4] + (1 if violation else 0)

                # 更新违规类型统计
                vtypes = json.loads(existing[5] or "{}")
                if violation:
                    vtypes[vtype] = vtypes.get(vtype, 0) + 1

                # 更新最近事件
                events = json.loads(existing[12] or "[]")
                events.append(
                    {
                        "time": now[:10],
                        "summary": f"{'违规' if violation else '正常'}"
                        f"{' - ' + vtype if violation else ''}"
                        f"{' @ ' + zone if zone else ''}",
                    }
                )
                if len(events) > 5:
                    events = events[-5:]

                # 计算风险趋势
                recent = json.loads(existing[12] or "[]")
                recent.append(
                    {
                        "time": now[:10],
                        "violation": violation,
                    }
                )
                recent = recent[-10:]
                violation_count = sum(1 for e in recent if e.get("violation"))
                if violation_count >= 4:
                    risk_trend = "escalating"
                elif violation_count <= 1 and total_appearances >= 3:
                    risk_trend = "improving"
                else:
                    risk_trend = "stable"

                conn.execute(
                    """
                    UPDATE ship_memory SET
                        last_seen = ?, total_appearances = ?,
                        total_violations = ?, violation_types = ?,
                        last_judgment = ?, last_assessment = ?,
                        last_confidence = ?, last_risk_level = ?,
                        last_recommended_action = ?,
                        risk_trend = ?, recent_events = ?,
                        updated_at = ?
                    WHERE mmsi = ?
                    """,
                    (
                        now,
                        total_appearances,
                        total_violations,
                        json.dumps(vtypes, ensure_ascii=False),
                        json.dumps(llm_result, ensure_ascii=False),
                        assessment,
                        confidence,
                        risk,
                        action,
                        risk_trend,
                        json.dumps(events, ensure_ascii=False),
                        now,
                        mmsi,
                    ),
                )
            else:
                # 新船舶记录
                first_seen = now
                vtypes = {vtype: 1} if violation else {}
                events = [
                    {
                        "time": now[:10],
                        "summary": f"{'违规' if violation else '正常'}"
                        f"{' - ' + vtype if violation else ''}"
                        f"{' @ ' + zone if zone else ''}",
                    }
                ]
                risk_trend = "escalating" if violation else "stable"

                conn.execute(
                    """
                    INSERT INTO ship_memory (
                        mmsi, first_seen, last_seen, total_appearances,
                        total_violations, violation_types,
                        last_judgment, last_assessment,
                        last_confidence, last_risk_level,
                        last_recommended_action, risk_trend,
                        recent_events, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        mmsi,
                        first_seen,
                        now,
                        1,
                        1 if violation else 0,
                        json.dumps(vtypes, ensure_ascii=False),
                        json.dumps(llm_result, ensure_ascii=False),
                        assessment,
                        confidence,
                        risk,
                        action,
                        risk_trend,
                        json.dumps(events, ensure_ascii=False),
                        now,
                    ),
                )
            conn.commit()

    # ── 统计 ──

    def get_recidivist_ships(self, min_violations: int = 3) -> list[dict[str, Any]]:
        """查询累计违规超过指定次数的船舶（惯犯）。

        Args:
            min_violations: 最少违规次数阈值（默认 3 次）

        Returns:
            惯犯船舶列表，按违规次数降序排列
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT mmsi, total_appearances, total_violations,
                       violation_types, last_assessment, risk_trend
                FROM ship_memory
                WHERE total_violations >= ?
                ORDER BY total_violations DESC
                """,
                (min_violations,),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_statistics(self) -> dict[str, Any]:
        """获取船舶记忆库的总体统计信息。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row

            total = conn.execute("SELECT COUNT(*) as c FROM ship_memory").fetchone()[0]
            ever_violated = conn.execute(
                "SELECT COUNT(*) as c FROM ship_memory WHERE total_violations > 0"
            ).fetchone()[0]
            recidivists = conn.execute(
                "SELECT COUNT(*) as c FROM ship_memory WHERE total_violations >= 3"
            ).fetchone()[0]

            trends = conn.execute(
                """
                SELECT risk_trend, COUNT(*) as c
                FROM ship_memory
                GROUP BY risk_trend
                """
            ).fetchall()

            return {
                "total_ships": total,
                "ever_violated": ever_violated,
                "recidivists": recidivists,
                "trends": {row[0]: row[1] for row in trends},
            }