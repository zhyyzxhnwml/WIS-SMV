"""
shipclaw — 可养成的海事智能体 ShipClaw
========================================

把"小慧"从无状态 Q&A 机器人升级为可养成的个性化智能体。

核心特性：
  1. 等级系统：Lv1~10，解锁不同技能
  2. 性格系统：四维性格（专业度/亲切度/严谨度/主动性），随交互成长
  3. 技能系统：法规问答/船舶查询/趋势分析/碰撞预警/闲聊
  4. 养成记忆：持久化用户偏好、交互历史、成长事件

快速开始:
    from shipclaw import ShipClaw
    claw = ShipClaw()
    result = claw.chat("user_001", "禁渔区有什么规定？")
    print(result["reply"])
"""

from .core import ShipClaw
from .personality import Personality
from .claw_memory import ClawMemory
from .skills import ALL_SKILLS, match_best_skill

__all__ = [
    "ShipClaw",
    "Personality",
    "ClawMemory",
    "ALL_SKILLS",
    "match_best_skill",
]
