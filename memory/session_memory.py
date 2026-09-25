"""
memory/session_memory.py — 会话记忆（Session Memory）
=====================================================

【这个文件做什么？】
  单次运行（一次 python run.py）中，前几艘船的发现能被后面的船利用。
  进程内 Python 对象，运行结束即丢弃，不需要持久化。

【解决的问题】
  没有会话记忆时，处理 603 艘船，前 10 艘船发现的新模式，后面 590 艘不知道。
  有了会话记忆，每艘船判定完的结果可以立即被后续船舶参考。

【使用场景】
  - 同一区域出现多起同类违规，后面的船自动知道"该区域近期违规高发"
  - 本轮已确认的违规模式可以直接通报给后续船舶的 LLM prompt
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


class SessionMemory:
    """
    一次 run.py 执行过程中的短期记忆。
    跑完就丢，不需要持久化。
    """

    def __init__(self):
        # 本次运行中发现的新模式
        # { (zone, scenario): { "count": int, "violation_count": int,
        #                       "conclusion": Optional[str], "llm_summary": Optional[str] } }
        self.emerging_patterns: dict[tuple[str, str], dict] = {}

        # 每个区域当前的总体状态
        # { zone_id: { "ships_seen": int, "violations": int, "latest_summary": str } }
        self.zone_status: dict[str, dict] = {}

        # 最近 50 艘船的判定结果（用于趋势判断）
        self.recent_decisions: list[dict] = []

        # 本次运行已见过的 MMSI（避免同一批次中重复加载历史）
        self.seen_mmsi_in_session: set[str] = set()

    def record_llm_result(
        self,
        mmsi: str,
        zone: str,
        scenario: str,
        llm_result: dict,
    ) -> None:
        """记录一条 LLM 判定结果到会话记忆中。

        Args:
            mmsi: 船舶 MMSI 号
            zone: 区域 ID（如 "speed_limit_zone_C"）
            scenario: 行为类型（如 "speeding" / "fishing"）
            llm_result: LLM 判定结果字典
        """
        # ── 更新 emerging_patterns ──
        key = (zone, scenario)
        if key not in self.emerging_patterns:
            self.emerging_patterns[key] = {
                "count": 0,
                "violation_count": 0,
                "conclusion": None,
                "llm_summary": None,
            }
        self.emerging_patterns[key]["count"] += 1
        if llm_result.get("violation_detected"):
            self.emerging_patterns[key]["violation_count"] += 1
        self.emerging_patterns[key]["llm_summary"] = llm_result.get(
            "final_assessment", ""
        )

        # 如果该模式出现 >= 3 次且多数判定为违规 → 标记为"已确认模式"
        p = self.emerging_patterns[key]
        if p["count"] >= 3:
            if p["violation_count"] / p["count"] > 0.66:
                p["conclusion"] = "违规"
            else:
                p["conclusion"] = "正常"

        # ── 更新 zone_status ──
        if zone not in self.zone_status:
            self.zone_status[zone] = {"ships_seen": 0, "violations": 0}
        self.zone_status[zone]["ships_seen"] += 1
        if llm_result.get("violation_detected"):
            self.zone_status[zone]["violations"] += 1

        # ── 更新 recent_decisions ──
        self.recent_decisions.append(
            {
                "mmsi": mmsi,
                "violation": llm_result.get("violation_detected"),
                "risk": llm_result.get("risk_level"),
                "violation_type": llm_result.get("violation_type"),
                "time": datetime.now(),
            }
        )
        if len(self.recent_decisions) > 50:
            self.recent_decisions.pop(0)

        self.seen_mmsi_in_session.add(mmsi)

    def get_prompt_context(
        self,
        mmsi: str,
        zone: str,
        scenario: str,
    ) -> str:
        """生成一段文字，注入到下一艘船的 LLM prompt 中。

        Args:
            mmsi: 当前处理的船舶 MMSI
            zone: 当前船舶所在区域
            scenario: 当前船舶的行为类型

        Returns:
            一段可用于注入 prompt 的上下文文字，如无可用上下文则返回空字符串
        """
        parts = []

        # ── 本次运行已确认的模式 ──
        confirmed = [
            (k, v) for k, v in self.emerging_patterns.items() if v["conclusion"]
        ]
        if confirmed:
            parts.append("【本次运行已发现模式】")
            for (z, sc), v in confirmed[:3]:
                parts.append(
                    f"  {z} {sc}: {v['conclusion']}（已出现 {v['count']} 次）"
                )

        # ── 该区域当前状态 ──
        if zone in self.zone_status:
            zs = self.zone_status[zone]
            if zs["ships_seen"] > 1:
                parts.append(f"【{zone} 状态】")
                parts.append(
                    f"  已处理 {zs['ships_seen']} 艘船，违规 {zs['violations']} 艘"
                )

        # ── 该船是否在本轮出现过 ──
        if mmsi in self.seen_mmsi_in_session:
            parts.append(
                "【注意】该船 MMSI 在本轮已出现过一次，"
                "请关注其轨迹是否匹配上次的判定"
            )

        return "\n".join(parts) if parts else ""

    def get_summary(self) -> dict:
        """获取当前会话记忆的摘要（用于日志输出或调试）。"""
        return {
            "emerging_patterns_count": len(self.emerging_patterns),
            "confirmed_patterns": sum(
                1 for v in self.emerging_patterns.values() if v["conclusion"]
            ),
            "zones_tracked": list(self.zone_status.keys()),
            "ships_seen_count": len(self.seen_mmsi_in_session),
            "recent_decision_count": len(self.recent_decisions),
        }