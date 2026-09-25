"""
memory/pattern_memory.py — 情景记忆（Pattern Memory）
=====================================================

【这个文件做什么？】
  记录"某种场景下 LLM 是怎么判的"，下次出现高度相似的场景时
  直接复用，不用调 LLM。

【解决的问题】
  同样的场景（如"港口限速区C 超速"）已经判过几十次且结论一致，
  每次再调 LLM 就是浪费。情景记忆把稳定的判定模式存下来，
  下次直接复用。

【节省效果估算】
  运行 3-5 次后，稳定模式可达到 30-50% 的 LLM 调用节省。
  且节省的都是"已经判过几十次、结果一致的重复场景"。

【表结构 pattern_memory】
  - id: 自增主键
  - zone: 区域 ID
  - scenario: 行为类型（fishing/speeding/intrusion）
  - signature: 特征向量二进制
  - feature_hash: SHA256(signature)，快速索引
  - typical_judgment / typical_violation_type / typical_confidence
  - llm_conclusion_summary: LLM 对该场景的结论摘要
  - occurrence_count / first_occurrence / last_occurrence
  - judgment_consistency: 多次判定的结果一致率
  - related_regulations: 相关法规引用（JSON）
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import numpy as np


# 默认 SQLite 数据库路径
DEFAULT_DB_PATH = Path("data/memory/pattern_memory.db")

# 相似度阈值：超过此值认为是同一场景
SIMILARITY_THRESHOLD = 0.85

# 场景稳定的最小出现次数
STABLE_OCCURRENCE_THRESHOLD = 3

# 场景稳定所需的一致率
CONSISTENCY_THRESHOLD = 0.9


class PatternMemory:
    """情景记忆 —— 持久化存储场景模式。

    用法:
        pattern_mem = PatternMemory()
        match = pattern_mem.find_match(zone="...", scenario="...", signature=vector)
        if match and match["stable"]:
            # 直接复用，跳过 LLM
            result = match["typical_judgment"]
        else:
            # 正常调 LLM，然后记录结果
            pattern_mem.record_judgment(...)
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── 数据库初始化 ──

    def _init_db(self) -> None:
        """创建 pattern_memory 表（如不存在）。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pattern_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,

                    zone TEXT NOT NULL,
                    scenario TEXT NOT NULL,

                    signature BLOB,
                    feature_hash TEXT,

                    typical_judgment TEXT,
                    typical_violation_type TEXT,
                    typical_confidence REAL,
                    llm_conclusion_summary TEXT,

                    occurrence_count INTEGER DEFAULT 1,
                    first_occurrence TIMESTAMP,
                    last_occurrence TIMESTAMP,
                    judgment_consistency REAL DEFAULT 1.0,

                    related_regulations TEXT DEFAULT '[]',

                    UNIQUE(zone, scenario, feature_hash)
                )
                """
            )
            # 为快速查询创建索引
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pattern_zone_scenario "
                "ON pattern_memory(zone, scenario)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_pattern_hash "
                "ON pattern_memory(feature_hash)"
            )
            conn.commit()

    # ── 工具方法 ──

    @staticmethod
    def _compute_hash(signature: list[float]) -> str:
        """计算特征签名的 SHA256 哈希值。

        Args:
            signature: 浮点数特征向量

        Returns:
            SHA256 十六进制字符串
        """
        binary = struct.pack(f"{len(signature)}d", *signature)
        return hashlib.sha256(binary).hexdigest()

    @staticmethod
    def _serialize_vector(vector: list[float]) -> bytes:
        """将浮点数向量序列化为二进制 BLOB。"""
        return struct.pack(f"{len(vector)}d", *vector)

    @staticmethod
    def _deserialize_vector(blob: bytes) -> list[float]:
        """从二进制 BLOB 反序列化出浮点数向量。"""
        count = len(blob) // 8
        return list(struct.unpack(f"{count}d", blob))

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度。"""
        a_arr = np.array(a, dtype=np.float64)
        b_arr = np.array(b, dtype=np.float64)
        dot = np.dot(a_arr, b_arr)
        norm_a = np.linalg.norm(a_arr)
        norm_b = np.linalg.norm(b_arr)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(dot / (norm_a * norm_b))

    # ── 核心查询 ──

    def find_match(
        self,
        zone: str,
        scenario: str,
        signature: list[float],
    ) -> Optional[dict[str, Any]]:
        """查找与当前场景最匹配的历史模式。

        Args:
            zone: 区域 ID
            scenario: 行为类型
            signature: 当前场景的特征签名向量

        Returns:
            如果找到匹配的模式，返回带 stable 标记的完整记录；
            否则返回 None。
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT * FROM pattern_memory
                WHERE zone = ? AND scenario = ?
                ORDER BY occurrence_count DESC, last_occurrence DESC
                """,
                (zone, scenario),
            )
            rows = cur.fetchall()

        best_match = None
        best_similarity = 0.0

        for row in rows:
            stored_sig = self._deserialize_vector(row["signature"])
            similarity = self._cosine_similarity(signature, stored_sig)

            if similarity > best_similarity:
                best_similarity = similarity
                best_match = dict(row)

        if best_match is None or best_similarity < SIMILARITY_THRESHOLD:
            return None

        # 判断模式是否稳定可用
        result = dict(best_match)
        result["similarity"] = round(best_similarity, 4)
        result["stable"] = (
            best_match["occurrence_count"] >= STABLE_OCCURRENCE_THRESHOLD
            and (best_match["judgment_consistency"] or 0) >= CONSISTENCY_THRESHOLD
        )
        return result

    def find_match_by_hash(
        self,
        zone: str,
        scenario: str,
        signature: list[float],
    ) -> Optional[dict[str, Any]]:
        """通过精确哈希匹配查找（快速路径，比向量相似度快得多）。"""
        feature_hash = self._compute_hash(signature)
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT * FROM pattern_memory
                WHERE zone = ? AND scenario = ? AND feature_hash = ?
                """,
                (zone, scenario, feature_hash),
            )
            row = cur.fetchone()

        if row is None:
            return None

        result = dict(row)
        result["similarity"] = 1.0
        result["stable"] = (
            row["occurrence_count"] >= STABLE_OCCURRENCE_THRESHOLD
            and (row["judgment_consistency"] or 0) >= CONSISTENCY_THRESHOLD
        )
        return result

    def get_pattern_prompt_context(
        self,
        zone: str,
        scenario: str,
        signature: list[float],
    ) -> str:
        """生成一段可注入 LLM prompt 的情景记忆上下文。

        先尝试哈希匹配（快速），再尝试相似度匹配（慢）。

        Args:
            zone: 区域 ID
            scenario: 行为类型
            signature: 特征签名向量

        Returns:
            格式化的情景上下文文本，如无匹配则返回空字符串
        """
        # 先快速哈希匹配
        match = self.find_match_by_hash(zone, scenario, signature)
        if match is None:
            # 再慢速相似度匹配
            match = self.find_match(zone, scenario, signature)

        if match is None:
            return ""

        parts = ["【历史场景匹配】"]
        parts.append(f"  场景: {match['zone']} / {match['scenario']}")
        parts.append(f"  出现次数: {match['occurrence_count']} 次")
        parts.append(
            f"  历史结论: {match.get('typical_judgment', '未知')}"
        )
        if match.get("typical_violation_type"):
            parts.append(
                f"  违规类型: {match['typical_violation_type']}"
            )
        parts.append(
            f"  一致率: {match.get('judgment_consistency', 0):.0%}"
        )

        if match.get("llm_conclusion_summary"):
            parts.append(
                f"  结论摘要: {match['llm_conclusion_summary'][:100]}"
            )

        if match.get("stable"):
            parts.append(
                "  → 该模式已稳定（出现 3 次以上，一致率 ≥ 90%），"
                "可直接参考历史结论"
            )

        return "\n".join(parts)

    # ── 写入 ──

    def record_judgment(
        self,
        zone: str,
        scenario: str,
        signature: list[float],
        llm_result: dict[str, Any],
        related_regulations: Optional[list[str]] = None,
    ) -> None:
        """记录一次 LLM 判定结果到情景记忆。

        Args:
            zone: 区域 ID
            scenario: 行为类型
            signature: 特征签名向量（用于后续相似度匹配）
            llm_result: LLM 判定结果
            related_regulations: 关联的法规列表（可选）
        """
        feature_hash = self._compute_hash(signature)
        signature_blob = self._serialize_vector(signature)
        now = datetime.now().isoformat()

        violation = llm_result.get("violation_detected", False)
        judgment = "violation" if violation else "normal"
        vtype = llm_result.get("violation_type")
        confidence = llm_result.get("confidence", 0)
        assessment = llm_result.get("final_assessment", "")

        related = json.dumps(
            related_regulations or [], ensure_ascii=False
        )

        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row  # 使用命名列访问
            # 精确哈希查找
            cur = conn.execute(
                """
                SELECT * FROM pattern_memory
                WHERE zone = ? AND scenario = ? AND feature_hash = ?
                """,
                (zone, scenario, feature_hash),
            )
            existing = cur.fetchone()

            if existing:
                # 更新已有记录
                new_count = int(existing["occurrence_count"]) + 1
                old_consistency = float(existing["judgment_consistency"] or 1.0)

                # 更新一致率（滑动平均）
                is_consistent = 1.0 if existing["typical_judgment"] == judgment else 0.0
                new_consistency = (
                    old_consistency * (new_count - 1) + is_consistent
                ) / new_count

                conn.execute(
                    """
                    UPDATE pattern_memory SET
                        occurrence_count = ?,
                        last_occurrence = ?,
                        typical_confidence = ?,
                        llm_conclusion_summary = ?,
                        judgment_consistency = ?
                    WHERE zone = ? AND scenario = ? AND feature_hash = ?
                    """,
                    (
                        new_count,
                        now,
                        confidence,
                        assessment,
                        new_consistency,
                        zone,
                        scenario,
                        feature_hash,
                    ),
                )
            else:
                # 插入新记录
                # 检查是否有同一 zone+scenario 的相似记录
                cur = conn.execute(
                    """
                    SELECT * FROM pattern_memory
                    WHERE zone = ? AND scenario = ?
                    """,
                    (zone, scenario),
                )
                similar_rows = cur.fetchall()

                merged = False
                for srow in similar_rows:
                    stored_sig = self._deserialize_vector(srow["signature"])
                    similarity = self._cosine_similarity(signature, stored_sig)
                    if similarity >= SIMILARITY_THRESHOLD:
                        # 合并到已有记录
                        new_count = int(srow["occurrence_count"]) + 1
                        conn.execute(
                            """
                            UPDATE pattern_memory SET
                                occurrence_count = ?,
                                last_occurrence = ?,
                                typical_confidence = ?,
                                llm_conclusion_summary = ?,
                                typical_judgment = ?,
                                typical_violation_type = COALESCE(?, typical_violation_type),
                                related_regulations = ?
                            WHERE id = ?
                            """,
                            (
                                new_count,
                                now,
                                confidence,
                                assessment,
                                judgment,
                                vtype,
                                related,
                                srow["id"],
                            ),
                        )
                        merged = True
                        break

                if not merged:
                    conn.execute(
                        """
                        INSERT INTO pattern_memory (
                            zone, scenario, signature, feature_hash,
                            typical_judgment, typical_violation_type,
                            typical_confidence, llm_conclusion_summary,
                            occurrence_count, first_occurrence, last_occurrence,
                            judgment_consistency, related_regulations
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            zone,
                            scenario,
                            signature_blob,
                            feature_hash,
                            judgment,
                            vtype,
                            confidence,
                            assessment,
                            1,
                            now,
                            now,
                            1.0,
                            related,
                        ),
                    )
            conn.commit()

    # ── 统计 ──

    def get_stable_patterns(
        self, min_occurrences: int = 3
    ) -> list[dict[str, Any]]:
        """获取所有已稳定的模式。

        Args:
            min_occurrences: 最小出现次数阈值

        Returns:
            稳定模式列表
        """
        with sqlite3.connect(str(self.db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute(
                """
                SELECT zone, scenario, typical_judgment, occurrence_count,
                       judgment_consistency, typical_confidence,
                       typical_violation_type
                FROM pattern_memory
                WHERE occurrence_count >= ?
                  AND judgment_consistency >= ?
                ORDER BY occurrence_count DESC, judgment_consistency DESC
                """,
                (min_occurrences, CONSISTENCY_THRESHOLD),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_statistics(self) -> dict[str, Any]:
        """获取情景记忆库的总体统计信息。"""
        with sqlite3.connect(str(self.db_path)) as conn:
            cur = conn.execute("SELECT COUNT(*) as c FROM pattern_memory")
            total = cur.fetchone()[0]

            cur = conn.execute(
                """
                SELECT COUNT(*) as c FROM pattern_memory
                WHERE occurrence_count >= ? AND judgment_consistency >= ?
                """,
                (STABLE_OCCURRENCE_THRESHOLD, CONSISTENCY_THRESHOLD),
            )
            stable = cur.fetchone()[0]

            cur = conn.execute(
                """
                SELECT typical_judgment, COUNT(*) as c
                FROM pattern_memory
                GROUP BY typical_judgment
                """
            )
            by_judgment = {row[0]: row[1] for row in cur.fetchall()}

            cur = conn.execute(
                "SELECT COALESCE(SUM(occurrence_count), 0) as c FROM pattern_memory"
            )
            total_occurrences = cur.fetchone()[0]

            return {
                "total_patterns": total,
                "stable_patterns": stable,
                "total_occurrences": total_occurrences,
                "by_judgment": by_judgment,
            }

    def can_skip_llm(
        self,
        zone: str,
        scenario: str,
        signature: list[float],
    ) -> Optional[dict[str, Any]]:
        """判断是否可以跳过 LLM 调用，直接复用历史结论。

        Args:
            zone: 区域 ID
            scenario: 行为类型
            signature: 特征签名向量

        Returns:
            如果可以跳过，返回包含 typical_judgment 等信息的字典；
            否则返回 None
        """
        match = self.find_match_by_hash(zone, scenario, signature)
        if match is None:
            match = self.find_match(zone, scenario, signature)

        if match is not None and match.get("stable"):
            return {
                "violation_detected": match["typical_judgment"] == "violation",
                "violation_type": match.get("typical_violation_type"),
                "confidence": match.get("typical_confidence", 0.5),
                "source": "pattern_memory",
                "pattern_id": match["id"],
                "pattern_occurrences": match["occurrence_count"],
                "pattern_consistency": match["judgment_consistency"],
                "final_assessment": (
                    f"复用历史模式判定：该场景已出现 "
                    f"{match['occurrence_count']} 次，"
                    f"一致率 {match['judgment_consistency']:.0%}"
                ),
                "recommended_action": "auto_alert"
                if match["typical_judgment"] == "violation"
                else "monitor",
                "risk_level": "high"
                if match["typical_judgment"] == "violation"
                else "low",
            }
        return None