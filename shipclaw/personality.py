"""
shipclaw/personality.py — ShipClaw 人格系统
=============================================

【这个文件做什么？】
  管理 ShipClaw 的可养成人格：等级 + 性格双轨制。

【等级系统】
  Lv1: 基础对话 + 闲聊
  Lv2: 船舶查询
  Lv3: 法规问答
  Lv5: 趋势分析
  Lv7: 碰撞预警建议
  Lv10: 全技能 + 自定义人格

  升级阈值递增：Lv1→2 需 50 exp, Lv2→3 需 100, Lv3→4 需 200...

【性格系统】
  四维性格（0~100，初始 50）：
  - 专业度：问法规问题 → 提升 → 用术语 + 引法条
  - 亲切度：闲聊 → 提升 → 口语化 + 关心语气
  - 严谨度：问趋势/数据 → 提升 → 给出处 + 不确定时说"需要确认"
  - 主动性：问预警/风险 → 提升 → 主动提醒相关风险

【设计原则】
  - 性格变化是渐进的（每次 +2），不会突变
  - 等级提供明确目标，性格提供细腻变化
  - 人格状态可序列化到 SQLite
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ── 等级配置 ──

LEVEL_THRESHOLDS = {
    1: 0,   # 初始等级
    2: 50,
    3: 150,   # 50 + 100
    4: 350,   # 150 + 200
    5: 750,   # 350 + 400
    6: 1350,  # 750 + 600
    7: 2350,  # 1350 + 1000
    8: 4350,  # 2350 + 2000
    9: 8350,  # 4350 + 4000
    10: 16350, # 8350 + 8000
}

MAX_LEVEL = 10

# 技能解锁等级
SKILL_LEVEL_REQUIREMENTS = {
    "casual_chat": 1,
    "ship_query": 2,
    "ship_list": 2,
    "regulation_qa": 3,
    "user_overview": 3,
    "violation_records": 4,
    "trend_analysis": 5,
    "collision_alert": 7,
}

# 技能经验奖励
SKILL_EXP_REWARDS = {
    "casual_chat": 5,
    "ship_query": 8,
    "ship_list": 8,
    "regulation_qa": 10,
    "user_overview": 9,
    "violation_records": 10,
    "trend_analysis": 12,
    "collision_alert": 15,
}

# 首次使用技能的里程碑奖励
FIRST_SKILL_BONUS = 20

# 性格维度名称
TRAIT_NAMES = ["专业度", "亲切度", "严谨度", "主动性"]

# 性格维度对应的技能
TRAIT_SKILL_MAP = {
    "专业度": "regulation_qa",
    "亲切度": "casual_chat",
    "严谨度": "trend_analysis",
    "主动性": "collision_alert",
}

# 每次交互的性格增长值
TRAIT_INCREMENT = 2

# 初始性格值
DEFAULT_TRAIT_VALUE = 50


@dataclass
class Personality:
    """ShipClaw 的人格——等级解锁技能，性格影响表达。"""

    level: int = 1
    exp: int = 0
    traits: dict[str, int] = field(default_factory=lambda: {
        name: DEFAULT_TRAIT_VALUE for name in TRAIT_NAMES
    })
    growth_log: list[dict[str, Any]] = field(default_factory=list)
    skills_used: set[str] = field(default_factory=set)

    # ── 经验与等级 ──

    def add_exp(self, amount: int, reason: str = "") -> bool:
        """增加经验值，返回是否升级。

        Args:
            amount: 经验值增量
            reason: 增长原因

        Returns:
            是否升级（True = 刚好升了一级）
        """
        old_level = self.level
        self.exp += amount

        # 检查升级
        while self.level < MAX_LEVEL:
            next_threshold = LEVEL_THRESHOLDS.get(self.level + 1, float("inf"))
            if self.exp >= next_threshold:
                self.level += 1
            else:
                break

        leveled_up = self.level > old_level
        if leveled_up:
            self.growth_log.append({
                "time": datetime.now().isoformat(),
                "event": f"升级到 Lv{self.level}",
                "exp": amount,
                "level": self.level,
            })

        return leveled_up

    def exp_to_next_level(self) -> int:
        """距离下一级还差多少经验。"""
        if self.level >= MAX_LEVEL:
            return 0
        next_threshold = LEVEL_THRESHOLDS.get(self.level + 1, float("inf"))
        return max(0, next_threshold - self.exp)

    def exp_progress(self) -> float:
        """当前等级的进度（0~1）。"""
        if self.level >= MAX_LEVEL:
            return 1.0
        current_threshold = LEVEL_THRESHOLDS.get(self.level, 0)
        next_threshold = LEVEL_THRESHOLDS.get(self.level + 1, current_threshold + 1)
        if next_threshold <= current_threshold:
            return 1.0
        return min(1.0, (self.exp - current_threshold) / (next_threshold - current_threshold))

    # ── 技能解锁 ──

    def can_use_skill(self, skill_name: str) -> bool:
        """检查当前等级是否解锁了指定技能。"""
        required = SKILL_LEVEL_REQUIREMENTS.get(skill_name, 1)
        return self.level >= required

    def skill_locked_reason(self, skill_name: str) -> Optional[str]:
        """如果技能被锁定，返回原因；否则返回 None。"""
        required = SKILL_LEVEL_REQUIREMENTS.get(skill_name, 1)
        if self.level < required:
            return f"该功能需要 Lv{required} 解锁（当前 Lv{self.level}，还差 {self.exp_to_next_level()} 经验）"
        return None

    def record_skill_use(self, skill_name: str) -> int:
        """记录技能使用，返回获得的经验值。

        首次使用技能有里程碑奖励。
        """
        is_first = skill_name not in self.skills_used
        self.skills_used.add(skill_name)

        base_exp = SKILL_EXP_REWARDS.get(skill_name, 5)
        total_exp = base_exp + (FIRST_SKILL_BONUS if is_first else 0)

        if is_first:
            self.growth_log.append({
                "time": datetime.now().isoformat(),
                "event": f"首次使用技能「{skill_name}」",
                "exp": total_exp,
                "milestone": True,
            })

        return total_exp

    # ── 性格成长 ──

    def grow_trait(self, skill_name: str) -> Optional[str]:
        """根据使用的技能增长对应性格维度。

        Returns:
            增长的维度名称，如果没有增长则返回 None
        """
        for trait, mapped_skill in TRAIT_SKILL_MAP.items():
            if mapped_skill == skill_name:
                old_value = self.traits.get(trait, DEFAULT_TRAIT_VALUE)
                new_value = min(100, old_value + TRAIT_INCREMENT)
                self.traits[trait] = new_value
                return trait
        return None

    # ── 表达风格 ──

    def get_tone(self) -> str:
        """根据性格维度返回当前表达风格描述。"""
        dominant = max(self.traits, key=self.traits.get)  # type: ignore
        dominant_value = self.traits[dominant]

        if dominant_value >= 70:
            tone_map = {
                "专业度": "专业严谨，善用术语和法条引用",
                "亲切度": "亲切自然，像朋友聊天",
                "严谨度": "严谨细致，注重出处和不确定性",
                "主动性": "主动积极，会提醒潜在风险",
            }
            return tone_map.get(dominant, "平衡风格")
        return "平衡风格"

    def get_system_prompt_suffix(self) -> str:
        """根据性格生成注入 LLM system prompt 的风格指令。"""
        parts = []

        if self.traits.get("专业度", 50) > 70:
            parts.append("使用专业术语，引用具体法规条款编号。")
        elif self.traits.get("专业度", 50) > 60:
            parts.append("适当使用专业术语，必要时引用法规。")

        if self.traits.get("亲切度", 50) > 70:
            parts.append("用口语化、亲切的语气回答，像朋友聊天。")
        elif self.traits.get("亲切度", 50) > 60:
            parts.append("语气友好自然。")

        if self.traits.get("严谨度", 50) > 70:
            parts.append("给出信息来源，不确定时明确说'需要进一步确认'。")
        elif self.traits.get("严谨度", 50) > 60:
            parts.append("尽量给出依据，避免绝对化表述。")

        if self.traits.get("主动性", 50) > 70:
            parts.append("主动提醒用户可能相关的风险和注意事项。")
        elif self.traits.get("主动性", 50) > 60:
            parts.append("适当提醒相关风险。")

        return " ".join(parts) if parts else ""

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "exp": self.exp,
            "traits": dict(self.traits),
            "skills_used": list(self.skills_used),
            "growth_log": self.growth_log[-20:],  # 只保留最近 20 条
            "tone": self.get_tone(),
            "exp_to_next": self.exp_to_next_level(),
            "exp_progress": round(self.exp_progress(), 3),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Personality":
        p = cls()
        p.level = data.get("level", 1)
        p.exp = data.get("exp", 0)
        p.traits = data.get("traits", {name: DEFAULT_TRAIT_VALUE for name in TRAIT_NAMES})
        p.skills_used = set(data.get("skills_used", []))
        p.growth_log = data.get("growth_log", [])
        return p

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, text: str) -> "Personality":
        return cls.from_dict(json.loads(text))
