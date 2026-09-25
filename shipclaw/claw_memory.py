"""
shipclaw/claw_memory.py — ShipClaw 养成记忆
==============================================

【这个文件做什么？】
  持久化存储 ShipClaw 的养成状态：人格、偏好、交互历史、成长事件。
  每个用户独立养成，不同用户的小慧性格可能不同。

【表结构 claw_memory】
  - user_id: 用户 ID（主键）
  - personality_json: 人格状态（性格维度 + 等级 + 经验）
  - preferences_json: 用户偏好（语言风格 / 关注区域 / 常查船舶）
  - interaction_count: 交互次数
  - last_interaction: 最近交互时间
  - growth_events_json: 成长事件列表
  - chat_history_json: 最近 20 条对话摘要
  - created_at: 创建时间
  - updated_at: 更新时间

【设计原则】
  - 每次交互后自动更新人格和记忆
  - 偏好从交互历史中自动学习：关注区域（focus_zones）/ 常查船舶（frequent_ships）/ 语言风格（language_style）
  - 对话历史保留最近 20 条，用于上下文注入
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .personality import Personality


# 默认数据库路径
DEFAULT_DB_PATH = Path("data/memory/claw_memory.db")

# 已知港区/区域关键词（用于自动学习 focus_zones）
KNOWN_ZONES = [
    "福州港", "厦门港", "泉州港", "湄洲湾", "宁德港", "三都澳",
    "江阴港", "罗源湾", "松下港", "平潭港", "可门港", "闽江口",
    "台江", "马尾", "海沧", "东渡", "招银", "围头湾", "东山湾",
    "泉州湾", "湄洲湾", "沙埕港", "龙海", "古雷", "兴化湾",
    "主航道", "锚地", "锚泊区", "泊位", "港区", "港池",
]
ZONE_HINT = [
    "关注", "留意", "查看", "查询", "监控", "查到", "有没有", "统计",
    "分析", "预警", "报警", "违规", "隐患", "检查", "巡逻", "靠泊",
    "锚泊", "进出港", "航行", "辖区",
]


def _learn_language_style(message: str) -> str:
    """从用户消息推断语言风格：auto / professional / casual。

    规则：
      - 含海事/法规/查询数据术语 → professional
      - 句尾语气词、口语短语或短句 → casual
      - 其余 → auto
    """
    text = message.strip()
    if not text:
        return "auto"
    professional_terms = re.search(
        r"(MMSI|IMO|AIS|航速|航向|经纬度|坐标|航道|锚地|泊位|潮汐|吃水|载重|"
        r"法规|罚款|处罚|条文|违航|超速|漂移|碰撞|搁浅|海图|VTS|引航|船籍港|"
        r"吨位|排水量|航行|轨迹|查询|定位|实时|\bNm\b|\bkn\b)",
        text, re.IGNORECASE,
    )
    if professional_terms:
        return "professional"

    # 句尾语气词结束，或口语短语，或简短寒暄 → casual
    casual_signal = (
        re.search(r"[了吧嘛啊呢哦啦呀]$", text)
        or re.search(r"(哈哈|嘻嘻|嗯嗯|谢谢|好的|没关系|随便|你好|聊聊)", text)
        or (len(text) <= 10 and not re.search(r"\d{4,}", text))
    )
    if casual_signal:
        return "casual"
    return "auto"


def _learn_focus_zones(message: str, current: list[str]) -> tuple[list[str], str | None]:
    """从用户消息学习关注区域。

    要求同时满足：
      1. 消息里出现 KNOWN_ZONES 中的区域词
      2. 消息带关注意图（ZONE_HINT 中的动作词）

    返回 (新列表, 新增区域名或 None)。
    """
    zones = []
    for z in KNOWN_ZONES:
        if z in message and z not in zones:
            zones.append(z)
    if not zones:
        return current, None
    if not any(h in message for h in ZONE_HINT):
        return current, None

    learned = []
    added = None
    for z in zones:
        if z not in current:
            current.append(z)
            if added is None:
                added = z
    if len(current) > 12:
        current = current[-12:]
    learned = current
    return learned, added


def _learn_frequent_ships(message: str, current: list[str]) -> tuple[list[str], str | None]:
    """从用户消息学习常查船舶（MMSI 或 IMO 号）。"""
    mmsi_match = re.search(r"(?:MMSI[:\s#]*)?(\d{6,9})", message, re.IGNORECASE)
    if not mmsi_match:
        return current, None
    ship_id = mmsi_match.group(1)
    if ship_id not in current:
        current.append(ship_id)
        if len(current) > 10:
            current = current[-10:]
        return current, ship_id
    return current, None


class ClawMemory:
    """ShipClaw 的养成记忆——持久化到 SQLite。

    用法:
        mem = ClawMemory()
        personality = mem.load_personality("user_001")
        mem.record_interaction("user_001", "什么是禁渔区？", "禁渔区是指...", "regulation_qa")
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS claw_memory (
                    user_id TEXT PRIMARY KEY,
                    personality_json TEXT DEFAULT '{}',
                    preferences_json TEXT DEFAULT '{}',
                    interaction_count INTEGER DEFAULT 0,
                    last_interaction TEXT,
                    growth_events_json TEXT DEFAULT '[]',
                    chat_history_json TEXT DEFAULT '[]',
                    created_at TEXT,
                    updated_at TEXT
                )
            """)
            conn.commit()

    # ── 加载 ──

    def load_personality(self, user_id: str) -> Personality:
        """加载用户的人格状态。如果用户不存在，返回默认人格。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT personality_json FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        if row and row["personality_json"]:
            return Personality.from_json(row["personality_json"])
        return Personality()

    def load_preferences(self, user_id: str) -> dict[str, Any]:
        """加载用户偏好。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT preferences_json FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        if row and row["preferences_json"]:
            return json.loads(row["preferences_json"])
        return {
            "focus_zones": [],      # 关注区域
            "frequent_ships": [],   # 常查船舶
            "language_style": "auto",  # auto / professional / casual
        }

    def get_chat_context(self, user_id: str, max_turns: int = 5) -> str:
        """获取最近对话上下文，用于注入 LLM prompt。

        Args:
            user_id: 用户 ID
            max_turns: 最多返回几轮对话

        Returns:
            格式化的对话上下文文本
        """
        history = self._load_chat_history(user_id)
        if not history:
            return ""

        recent = history[-max_turns:]
        parts = ["【最近对话】"]
        for turn in recent:
            role = "用户" if turn.get("role") == "user" else "小慧"
            msg = turn.get("content", "")[:100]
            parts.append(f"  {role}: {msg}")

        return "\n".join(parts)

    def get_growth_log(self, user_id: str, limit: int = 20) -> list[dict]:
        """获取成长事件日志。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT growth_events_json FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        if row and row["growth_events_json"]:
            events = json.loads(row["growth_events_json"])
            return events[-limit:]
        return []

    def get_status(self, user_id: str) -> dict[str, Any]:
        """获取用户的完整养成状态。"""
        personality = self.load_personality(user_id)
        preferences = self.load_preferences(user_id)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT interaction_count, last_interaction, created_at FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        return {
            "user_id": user_id,
            "personality": personality.to_dict(),
            "preferences": preferences,
            "interaction_count": row["interaction_count"] if row else 0,
            "last_interaction": row["last_interaction"] if row else None,
            "created_at": row["created_at"] if row else None,
        }

    # ── 写入 ──

    def save_personality(self, user_id: str, personality: Personality) -> None:
        """保存人格状态。"""
        now = datetime.now().isoformat()
        personality_json = personality.to_json()

        with sqlite3.connect(str(self.db_path)) as conn:
            # 检查用户是否存在
            cur = conn.execute(
                "SELECT user_id FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            exists = cur.fetchone()

            if exists:
                conn.execute(
                    "UPDATE claw_memory SET personality_json = ?, updated_at = ? WHERE user_id = ?",
                    (personality_json, now, user_id),
                )
            else:
                conn.execute(
                    """INSERT INTO claw_memory (user_id, personality_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?)""",
                    (user_id, personality_json, now, now),
                )
            conn.commit()

    def record_interaction(
        self,
        user_id: str,
        message: str,
        reply: str,
        skill: str,
        personality: Optional[Personality] = None,
    ) -> None:
        """记录一次交互，更新记忆和偏好。

        Args:
            user_id: 用户 ID
            message: 用户消息
            reply: ShipClaw 回复
            skill: 使用的技能名
            personality: 更新后的人格（如果为 None 则不更新人格）
        """
        now = datetime.now().isoformat()

        # 加载现有数据
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT * FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        if row:
            interaction_count = row["interaction_count"] + 1
            preferences = json.loads(row["preferences_json"] or "{}")
            chat_history = json.loads(row["chat_history_json"] or "[]")
            growth_events = json.loads(row["growth_events_json"] or "[]")
        else:
            interaction_count = 1
            preferences = {"focus_zones": [], "frequent_ships": [], "language_style": "auto"}
            chat_history = []
            growth_events = []

        # 更新对话历史（保留最近 20 条）
        chat_history.append({
            "role": "user",
            "content": message[:200],
            "time": now,
        })
        chat_history.append({
            "role": "assistant",
            "content": reply[:200],
            "skill": skill,
            "time": now,
        })
        chat_history = chat_history[-20:]

        # 学习偏好：提取关注区域 / 常查船舶 / 语言风格
        # 每次交互最多记 1 条偏好成长事件，避免刷屏
        learned_desc = None  # 本次学到的新偏好描述

        current_zones = preferences.get("focus_zones", [])
        zones, added_zone = _learn_focus_zones(message, list(current_zones))
        if zones != current_zones:
            preferences["focus_zones"] = zones
            learned_desc = f"关注区域{added_zone}" if added_zone else "更新关注区域"

        current_ships = preferences.get("frequent_ships", [])
        ships, added_ship = _learn_frequent_ships(message, list(current_ships))
        if ships != current_ships:
            preferences["frequent_ships"] = ships
            if added_ship and learned_desc is None:
                learned_desc = f"常查船舶 {added_ship}"

        learned_style = _learn_language_style(message)
        if preferences.get("language_style", "auto") != learned_style and learned_style != "auto":
            # 风格一旦确认过就不再回退成 auto
            preferences["language_style"] = learned_style
            if learned_desc is None:
                learned_desc = f"语言风格调整为{learned_style}"

        if learned_desc:
            growth_events.append({
                "time": now,
                "event": f"记住你的偏好：{learned_desc}",
                "scope": "preference",
                "level": None,
                "type": "learning",
                "tag": "learned",
            })
            growth_events = growth_events[-50:]

        # 更新人格
        personality_json = "{}"
        if personality:
            personality_json = personality.to_json()
            # 合并成长事件
            new_events = personality.growth_log
            for event in new_events:
                if event not in growth_events:
                    growth_events.append(event)
            growth_events = growth_events[-50:]

        # 写入数据库
        with sqlite3.connect(str(self.db_path)) as conn:
            if row:
                conn.execute("""
                    UPDATE claw_memory SET
                        personality_json = ?,
                        preferences_json = ?,
                        interaction_count = ?,
                        last_interaction = ?,
                        growth_events_json = ?,
                        chat_history_json = ?,
                        updated_at = ?
                    WHERE user_id = ?
                """, (
                    personality_json,
                    json.dumps(preferences, ensure_ascii=False),
                    interaction_count,
                    now,
                    json.dumps(growth_events, ensure_ascii=False),
                    json.dumps(chat_history, ensure_ascii=False),
                    now,
                    user_id,
                ))
            else:
                conn.execute("""
                    INSERT INTO claw_memory (
                        user_id, personality_json, preferences_json,
                        interaction_count, last_interaction,
                        growth_events_json, chat_history_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    user_id,
                    personality_json,
                    json.dumps(preferences, ensure_ascii=False),
                    interaction_count,
                    now,
                    json.dumps(growth_events, ensure_ascii=False),
                    json.dumps(chat_history, ensure_ascii=False),
                    now,
                    now,
                ))
            conn.commit()

    def update_preferences(self, user_id: str, preferences: dict[str, Any]) -> None:
        """直接更新用户偏好。"""
        now = datetime.now().isoformat()
        prefs_json = json.dumps(preferences, ensure_ascii=False)

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                "UPDATE claw_memory SET preferences_json = ?, updated_at = ? WHERE user_id = ?",
                (prefs_json, now, user_id),
            )
            conn.commit()

    # ── 内部方法 ──

    def _load_chat_history(self, user_id: str) -> list[dict]:
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                "SELECT chat_history_json FROM claw_memory WHERE user_id = ?",
                (user_id,),
            )
            row = cur.fetchone()

        if row and row["chat_history_json"]:
            return json.loads(row["chat_history_json"])
        return []
