"""
memory — 三级级联系统智能体记忆模块
====================================

提供三类记忆能力：
1. SessionMemory  — 会话记忆（进程内，跑完丢弃）
2. ShipMemory     — 船舶记忆（SQLite 持久化）
3. PatternMemory  — 情景记忆（SQLite 持久化 + 相似度匹配）

快速开始:
    from memory import SessionMemory, ShipMemory, PatternMemory

    session = SessionMemory()
    ship_mem = ShipMemory()
    pattern_mem = PatternMemory()
"""

from .session_memory import SessionMemory
from .ship_memory import ShipMemory
from .pattern_memory import PatternMemory

__all__ = [
    "SessionMemory",
    "ShipMemory",
    "PatternMemory",
]