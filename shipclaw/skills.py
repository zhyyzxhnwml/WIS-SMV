"""
shipclaw/skills.py — ShipClaw 技能系统
========================================

【这个文件做什么？】
  定义 ShipClaw 的 9 个技能，每个技能有匹配度、最低等级、执行逻辑，
  以及对应的技能操作手册（shipclaw/skills/<name>.md，注入 LLM 防答非所问）。

【技能列表】
  1. CasualChatSkill        — 日常闲聊（Lv1，匹配度最低 → 兜底）
  2. ShipQuerySkill         — 船舶信息查询（Lv2，检测 MMSI 号/船名）
  3. RegulationQASkill      — 海事法规查询（Lv3，检测法规关键词）
  4. UserOverviewSkill      — 我的数据概况（Lv3，汇总辖区概况）
  5. ViolationListSkill     — 我的违规记录（Lv4，列出违规记录）
  6. ViolationEvidenceSkill — 违规证据链（Lv4，查单船违规判定依据，输出轨迹/规则/法规证据链）
  7. TrendAnalysisSkill     — 违规趋势分析（Lv5，检测趋势关键词）
  8. CollisionAlertSkill    — 碰撞风险评估（Lv7，检测碰撞/避碰关键词）
  9. ShipListSkill          — 我的船舶列表（Lv2，列出辖区船舶）

【匹配逻辑】
  每个技能的 match() 返回 0~1 的匹配度。
  ShipClaw 选匹配度最高的技能执行。
  如果最高匹配度 < 0.3，走 CasualChatSkill 兜底。

【设计原则】
  - 技能之间互斥（选最高匹配度的）
  - 每个技能独立，可单独测试
  - 技能执行结果是一段文本，由 ShipClaw 用人格风格渲染后返回
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Protocol


# ── 技能操作手册（skill.md） ──

SKILLS_DIR = Path(__file__).parent / "skills"


def load_skill_manual(skill_name: str) -> str:
    """加载技能操作手册（shipclaw/skills/<name>.md）。

    手册正文会被注入 LLM 系统提示，让智能体严格按手册操作，避免答非所问。
    返回时剥掉 YAML frontmatter，只留正文操作指引；文件不存在返回空串。
    """
    path = SKILLS_DIR / f"{skill_name}.md"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, IOError):
        return ""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            text = parts[2]
    return text.strip()


# ── 技能协议 ──

class Skill(Protocol):
    """技能协议。"""
    name: str
    min_level: int
    exp_reward: int

    def match(self, message: str) -> float:
        """匹配度 0~1。"""
        ...

    def execute(self, message: str, context: dict) -> str:
        """执行技能，返回结果文本。"""
        ...


# ── 1. 闲聊技能 ──

class CasualChatSkill:
    """闲聊技能——兜底，匹配度始终为 0.1。"""
    name = "casual_chat"
    label = "日常闲聊"
    description = "不查询数据，自然对话、寒暄、介绍自己"
    icon = "💬"
    params = []
    min_level = 1
    exp_reward = 5

    def match(self, message: str) -> float:
        msg = message.lower()
        # 打招呼
        if any(kw in msg for kw in ["你好", "嗨", "hello", "hi", "早上好", "下午好", "晚上好"]):
            return 0.9
        # 闲聊关键词
        if any(kw in msg for kw in ["你是谁", "你叫什么", "介绍一下自己", "天气", "今天"]):
            return 0.8
        # 其他无法匹配的消息
        return 0.1

    def execute(self, message: str, context: dict) -> str:
        # 闲聊直接返回提示，由 ShipClaw 用 Ollama 生成
        return "__OLLAMA__"


# ── 2. 船舶查询技能 ──

class ShipQuerySkill:
    """船舶查询技能——检测 MMSI 号或船舶相关关键词。"""
    name = "ship_query"
    label = "船舶信息查询"
    description = "按 MMSI 查询该船舶的检测结果（状态、航速、路由、违规类型）"
    icon = "🚢"
    params = [{"name": "mmsi", "label": "MMSI 号", "description": "6~9 位数字", "required": True}]
    min_level = 2
    exp_reward = 8

    # MMSI 正则（6~9 位数字）。不能用 \b(\d{6,9})\b：\w 含中文，数字后紧跟中文时边界失效。
    MMSI_PATTERN = re.compile(r'(?<!\d)(\d{6,9})(?!\d)')

    def match(self, message: str) -> float:
        msg = message.lower()
        # 包含 MMSI 号
        if self.MMSI_PATTERN.search(message):
            return 0.85
        # 船舶查询关键词（"查询/查一下 + 船名" 也应路由到这里）
        if any(kw in msg for kw in ["船舶", "查船", "找船", "这艘船", "那艘船", "船在哪", "查询", "查一下"]):
            return 0.7
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        mmsi_match = self.MMSI_PATTERN.search(message)
        mmsi = mmsi_match.group(1) if mmsi_match else None

        if not mmsi:
            # 尝试从偏好中获取常查船舶
            preferences = context.get("preferences", {})
            freq_ships = preferences.get("frequent_ships", [])
            if freq_ships:
                mmsi = freq_ships[0]

        if not mmsi:
            return "请提供船舶的 MMSI 号（6~9 位数字），我可以帮你查询。"

        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            row = conn.execute(
                f"SELECT * FROM detection_results WHERE mmsi=?{(' AND ' + scope_sql) if scope_sql else ''} ORDER BY analysis_time DESC LIMIT 1",
                (mmsi,) + scope_params,
            ).fetchone()
            conn.close()

            if not row:
                return f"未找到 MMSI {mmsi} 的记录。该船可能尚未被系统扫描过。"

            parts = [f"MMSI {mmsi} 的信息："]
            if row["violation_detected"]:
                parts.append(f"  状态：违规（{row.get('violation_type', '未知类型')}）")
                parts.append(f"  风险等级：{row.get('risk_level', '未知')}")
            else:
                parts.append("  状态：正常航行")
            if "avg_speed" in row.keys():
                parts.append(f"  平均航速：{row['avg_speed']:.1f} 节")
            if "max_speed" in row.keys():
                parts.append(f"  最高航速：{row['max_speed']:.1f} 节")
            if "routing" in row.keys():
                parts.append(f"  路由分类：{row['routing']}")
            if "analysis_time" in row.keys():
                parts.append(f"  最近分析：{row['analysis_time'][:16]}")

            return "\n".join(parts)
        except Exception as e:
            return f"查询船舶信息时出错：{e}"


# ── 3. 法规问答技能 ──

class RegulationQASkill:
    """法规问答技能——检测法规相关关键词，调用 RegulationDB。"""
    name = "regulation_qa"
    label = "海事法规查询"
    description = "检索海事法规条款（限速、禁航、锚泊、碰撞避让等），返回条文与罚则"
    icon = "📜"
    params = [{"name": "query", "label": "法规关键词", "description": "如：限速、锚泊、禁渔", "required": True}]
    min_level = 3
    exp_reward = 10

    def match(self, message: str) -> float:
        msg = message.lower()
        # 法规关键词
        reg_kws = [
            "法规", "法律", "条例", "规定", "罚", "处罚", "罚款",
            "禁渔", "禁航", "限速", "超速", "避碰", "碰撞",
            "安全法", "管理条例", "港口", "航道", "台风",
            "第几条", "哪条", "条款", "违反", "违规",
            "ais", "信号", "关闭",
        ]
        if any(kw in msg for kw in reg_kws):
            return 0.85
        # 问句模式
        if any(kw in msg for kw in ["能不能", "可不可以", "是否允许", "有什么规定"]):
            return 0.7
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        reg_db = context.get("regulation_db")
        if not reg_db:
            return "法规数据库暂不可用，请稍后再试。"

        try:
            # 语义检索
            results = reg_db.search(message, top_k=3)
            if not results:
                return "未找到与您问题直接相关的法规条目。您可以换个方式描述，或询问具体的法规名称。"

            parts = []
            for i, r in enumerate(results, 1):
                source = r.get("source", "")
                article_num = r.get("article_number", "")
                title = r.get("title", "")
                content = r.get("content", "")
                penalty = r.get("penalty", "")
                similarity = r.get("similarity", 0)

                # 截断过长内容
                if len(content) > 300:
                    content = content[:300] + "..."

                parts.append(f"{i}. {source} 第{article_num}条")
                if title:
                    parts.append(f"   {title}")
                parts.append(f"   {content}")
                if penalty:
                    parts.append(f"   罚则：{penalty}")

            return "\n".join(parts)
        except Exception as e:
            return f"查询法规时出错：{e}"


# ── 4. 趋势分析技能 ──

class TrendAnalysisSkill:
    """趋势分析技能——检测趋势/统计关键词。"""
    name = "trend_analysis"
    label = "违规趋势分析"
    description = "统计当前辖区违规分布、风险等级、多次违规船舶，分析规律与趋势"
    icon = "📊"
    params = []
    min_level = 5
    exp_reward = 12

    def match(self, message: str) -> float:
        msg = message.lower()
        # 0.9 > 法规的 0.85：当"违规趋势分析"同时命中法规("违规")与趋势("趋势")时，
        # 让趋势分析胜出，避免"问统计却答法规"的答非所问
        if any(kw in msg for kw in ["趋势", "统计", "分析", "规律", "模式", "分布", "变化"]):
            return 0.9
        if any(kw in msg for kw in ["多少", "几起", "比例", "占比", "排名"]):
            return 0.7
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            and_scope = f" AND {scope_sql}" if scope_sql else ""
            from_scope = f" WHERE {scope_sql}" if scope_sql else ""

            # 总体统计
            total = conn.execute(f"SELECT COUNT(*) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]
            violations = conn.execute(f"SELECT COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope}", scope_params).fetchone()["c"]
            ships = conn.execute(f"SELECT COUNT(DISTINCT mmsi) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]

            parts = [f"系统共扫描 {ships} 艘船，{total} 条记录，发现 {violations} 起违规。"]

            # 违规类型分布
            by_type = conn.execute(
                f"SELECT violation_type, COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope} GROUP BY violation_type ORDER BY c DESC",
                scope_params,
            ).fetchall()
            if by_type:
                type_str = "、".join(f"{r['violation_type']}({r['c']}起)" for r in by_type)
                parts.append(f"违规类型分布：{type_str}")

            # 风险等级分布
            by_risk = conn.execute(
                f"SELECT risk_level, COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope} GROUP BY risk_level",
                scope_params,
            ).fetchall()
            if by_risk:
                risk_str = "、".join(f"{r['risk_level']}({r['c']}起)" for r in by_risk)
                parts.append(f"风险等级：{risk_str}")

            # 惯犯
            recid = conn.execute(
                f"SELECT mmsi, COUNT(*) as c FROM detection_results WHERE violation_detected=1{and_scope} GROUP BY mmsi HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 3",
                scope_params,
            ).fetchall()
            if recid:
                recid_str = "、".join(f"{r['mmsi']}({r['c']}次)" for r in recid)
                parts.append(f"多次违规船：{recid_str}")

            conn.close()
            return "\n".join(parts)
        except Exception as e:
            return f"分析趋势时出错：{e}"


# ── 5. 碰撞预警技能 ──

class CollisionAlertSkill:
    """碰撞预警技能——检测碰撞/避碰/安全距离关键词。"""
    name = "collision_alert"
    label = "碰撞风险评估"
    description = "结合碰撞法规与近期碰撞风险记录，评估船舶会遇风险（DCPA/TCPA/CRI）"
    icon = "⚠️"
    params = []
    min_level = 7
    exp_reward = 15

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["碰撞", "避碰", "会遇", "dcpa", "tcpa", "cri", "碰撞风险"]):
            return 0.9
        if any(kw in msg for kw in ["安全距离", "最近会遇", "让路", "直航", "追越", "对遇"]):
            return 0.8
        if any(kw in msg for kw in ["预警", "危险", "接近", "靠近"]):
            return 0.6
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        reg_db = context.get("regulation_db")
        user_id = context.get("user_id", "system")

        parts = []

        # 查询碰撞相关法规
        if reg_db:
            try:
                results = reg_db.search_by_violation("collision_risk", top_k=3)
                if results:
                    parts.append("【碰撞相关法规】")
                    for r in results:
                        source = r.get("source", "")
                        article_num = r.get("article_number", "")
                        content = r.get("content", "")[:200]
                        parts.append(f"  {source} 第{article_num}条：{content}")
            except Exception:
                pass

        # 查询最近的碰撞风险记录（按用户隔离）
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            rows = conn.execute(
                f"""SELECT mmsi, violation_type, risk_level, analysis_time
                   FROM detection_results
                   WHERE (violation_type LIKE %s OR violation_type LIKE %s){(' AND ' + scope_sql) if scope_sql else ''}
                   ORDER BY analysis_time DESC LIMIT 5""",
                ("%collision%", "%碰撞%") + scope_params,
            ).fetchall()
            conn.close()

            if rows:
                parts.append("【近期碰撞风险记录】")
                for r in rows:
                    parts.append(f"  MMSI {r['mmsi']}：{r['violation_type']}（{r['risk_level']}）{r['analysis_time'][:16]}")
            elif not parts:
                parts.append("当前没有检测到碰撞风险记录。系统会在扫描数据时自动检测碰撞风险（基于预测轨迹的 DCPA/TCPA/CRI 计算）。")
        except Exception:
            if not parts:
                parts.append("碰撞预警系统就绪。当检测到船舶间碰撞风险时，会自动发出预警。")

        # 碰撞风险知识
        if not any("DCPA" in p for p in parts):
            parts.append("【碰撞风险指标说明】")
            parts.append("  DCPA（最近会遇距离）：两船预测轨迹上的最近距离，越小越危险")
            parts.append("  TCPA（最近会遇时间）：到达最近会遇点的时间，越短越紧急")
            parts.append("  CRI（碰撞风险指数）：综合指标 0~1，≥0.7 为高风险")

        return "\n".join(parts)


# ── 6. 我的船舶列表 ──

class ShipListSkill:
    """我的船舶列表——列出当前用户辖区内的所有船舶。"""
    name = "ship_list"
    label = "我的船舶列表"
    description = "列出该用户辖区内的所有船舶（MMSI、状态、最近分析时间）"
    icon = "🗒️"
    params = []
    min_level = 2
    exp_reward = 8

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["我的船", "船舶列表", "有哪些船", "所有船", "我的船舶", "查看船舶", "全部船", "船列表", "看看船"]):
            return 0.78
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            from_scope = f" WHERE {scope_sql}" if scope_sql else ""
            rows = conn.execute(
                f"SELECT mmsi, MAX(analysis_time) as analysis_time, "
                f"MAX(CASE WHEN violation_detected = 1 THEN 1 ELSE 0 END) as has_violation "
                f"FROM detection_results{from_scope} "
                f"GROUP BY mmsi ORDER BY analysis_time DESC LIMIT 20",
                scope_params,
            ).fetchall()
            conn.close()
            if not rows:
                return "当前辖区暂无船舶检测记录。"
            parts = [f"共 {len(rows)} 艘船："]
            for r in rows:
                status = "违规" if r["has_violation"] else "正常"
                parts.append(f"  {r['mmsi']} [{status}] {r['analysis_time'][:16] if r['analysis_time'] else ''}")
            return "\n".join(parts)
        except Exception as e:
            return f"查询船舶列表时出错：{e}"


# ── 7. 我的违规记录 ──

class ViolationListSkill:
    """违规记录——列出当前用户辖区最近的违规记录。"""
    name = "violation_records"
    label = "我的违规记录"
    description = "列出该用户辖区内最近的违规记录（MMSI、违规类型、风险等级、时间）"
    icon = "🚨"
    params = [{"name": "limit", "label": "条数", "description": "最多返回的违规记录数（默认 10）", "required": False}]
    min_level = 4
    exp_reward = 10

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["我的违规", "违规记录", "最近违规", "违规列表", "违规情况", "违规详情", "处罚记录", "违规事件"]):
            return 0.92
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            and_scope = f" AND {scope_sql}" if scope_sql else ""
            # 违规类型 → 中文标签
            try:
                from ais_accelerator.violation_catalog import describe as _d
                def _vl(t):
                    return _d(t or "")["label"]
            except Exception:
                def _vl(t):
                    return t or ""
            rows = conn.execute(
                f"SELECT mmsi, violation_type, risk_level, analysis_time "
                f"FROM detection_results WHERE violation_detected = 1{and_scope} "
                f"ORDER BY analysis_time DESC LIMIT 10",
                scope_params,
            ).fetchall()
            conn.close()
            if not rows:
                return "当前辖区暂无违规记录。"
            parts = [f"最近 {len(rows)} 起违规："]
            for r in rows:
                parts.append(f"  {r['mmsi']} {_vl(r['violation_type'])}（{r['risk_level']}）{r['analysis_time'][:16] if r['analysis_time'] else ''}")
            return "\n".join(parts)
        except Exception as e:
            return f"查询违规记录时出错：{e}"


# ── 8. 违规证据链（完整技能：查库组装证据链，非纯 LLM） ──

class ViolationEvidenceSkill:
    """违规证据链——按 MMSI 查询某艘船违规判定的完整依据。

    与 ShipQuerySkill（只给汇总行）不同，本技能从 detection_results 的
    detailed_result / violation_event / regulation_ref / final_assessment
    组装出四段证据链：① 轨迹分析 ② 规则匹配 ③ 法规引用 ④ LLM 复核，
    回答"这艘船怎么违规的、依据是什么、对应法规第几条"。
    """
    name = "violation_evidence"
    label = "违规证据链"
    description = "按 MMSI 查询某艘船违规判定的完整依据：轨迹分析、规则匹配、法规引用三段证据链"
    icon = "🔍"
    params = [{"name": "mmsi", "label": "MMSI 号", "description": "6~9 位数字", "required": True}]
    min_level = 4
    exp_reward = 12
    # 确定性技能：execute 返回的查询结果即最终答复，跳过 LLM 人格渲染。
    # 防止 LLM 在"查无记录/无违规"等结果上加戏编造法规科普。
    deterministic = True

    # MMSI 正则（6~9 位数字）。注意不能用 \b(\d{6,9})\b：Python3 的 \w 含中文，
    # "413570530的违规"里"的"是 \w，数字与"的"之间不构成词边界 → 匹配失败。
    # 改用 lookaround：前后都不能是数字，避免把 10+ 位串截出子串。
    MMSI_PATTERN = re.compile(r'(?<!\d)(\d{6,9})(?!\d)')

    def match(self, message: str) -> float:
        msg = message.lower()
        has_mmsi = bool(self.MMSI_PATTERN.search(message))
        # 明确的"证据链 / 违规原因 / 怎么违规"意图（无论有没有 MMSI，都要抢在 ShipQuery/RegulationQA 前面）
        if any(k in msg for k in ["证据链", "违规原因", "违规依据", "判定依据", "为什么违规", "怎么违规", "为什么判"]):
            return 0.97
        # MMSI + 违规判定相关词
        if has_mmsi and any(k in msg for k in ["违规", "证据", "依据", "为什么", "怎么", "原因", "判定", "详情", "怎么回事"]):
            return 0.95
        # 有 MMSI 且问详情/情况
        if has_mmsi and any(k in msg for k in ["详情", "怎么回事", "什么情况"]):
            return 0.9
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        m = self.MMSI_PATTERN.search(message)
        mmsi = m.group(1) if m else None
        if not mmsi:
            # 尝试从偏好中获取常查船舶
            preferences = context.get("preferences", {})
            freq_ships = preferences.get("frequent_ships", [])
            if freq_ships:
                mmsi = freq_ships[0]
        if not mmsi:
            return "请提供船舶的 MMSI 号（6~9 位数字），我可以帮你查询这艘船的违规判定依据与证据链。"

        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            row = conn.execute(
                f"SELECT * FROM detection_results WHERE mmsi=?{(' AND ' + scope_sql) if scope_sql else ''} ORDER BY analysis_time DESC LIMIT 1",
                (mmsi,) + scope_params,
            ).fetchone()
            conn.close()

            if not row:
                return f"未找到 MMSI {mmsi} 的检测记录。该船可能尚未被系统扫描过。"
            detail = json.loads(row["detailed_result"]) if row.get("detailed_result") else {}
            # 返回 {"text", "data"}：text 为可读证据链纯文本（前端兜底），
            # data 为结构化 JSON（前端渲染判定卡片/事件表格/轨迹图）。
            text = self._build_evidence(mmsi, row, detail)
            data = self._build_evidence_data(mmsi, row, detail)
            return {"text": text, "data": data}
        except Exception as e:
            return f"查询违规证据链时出错：{e}"

    # ── 证据链组装 ──

    @staticmethod
    def _describe_code(code: str) -> str:
        """违规代码 → 中文标签（violation_catalog 的规范码与 CandidateType 同义）。"""
        try:
            from ais_accelerator.violation_catalog import describe
            info = describe(code or "")
            return (info or {}).get("label") or code or ""
        except Exception:
            return code or ""

    @staticmethod
    def _fmt_latlon(lat, lon) -> str:
        """坐标 → 可读文本，如 121.6200°E 38.1500°N。"""
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            return ""
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        return f"{abs(lon):.4f}°{ew} {abs(lat):.4f}°{ns}"

    @staticmethod
    def _fmt_time(ts) -> str:
        """ISO 时间 → YYYY-MM-DD HH:MM。"""
        return (ts or "")[:16].replace("T", " ")

    def _build_evidence(self, mmsi: str, row: dict, detail: dict | None = None) -> str:
        if detail is None:
            detail = json.loads(row["detailed_result"]) if row.get("detailed_result") else {}
        ship = detail.get("ship") or {}
        events = detail.get("events") or []
        fm = detail.get("feature_metadata") or {}
        qr = detail.get("quality_report") or {}
        try:
            anns = json.loads(row["annotations"]) if row.get("annotations") else []
        except Exception:
            anns = []

        ship_type = ship.get("ship_type") or ""
        head = f"MMSI {mmsi}" + (f"（{ship_type}）" if ship_type else "")
        parts = [head]

        def _num(v, default=0.0):
            try:
                return float(v) if v is not None else default
            except (TypeError, ValueError):
                return default

        # ── 正常船：解释"为什么判定正常" ──
        if not row.get("violation_detected"):
            parts.append("判定：正常航行，未检出违规")
            routing = row.get("routing") or "—"
            parts.append(f"· ML 路由：{routing}（{row.get('ml_category') or '—'}，置信度 {_num(row.get('ml_confidence')):.0%}）")
            if fm.get("avg_speed") is not None:
                parts.append(f"· 全程平均航速 {_num(fm['avg_speed']):.1f} 节")
            if qr.get("level"):
                parts.append(f"· AIS 数据质量：{qr['level']}")
            return "\n".join(parts)

        # ── 违规船：四段证据链 ──
        vtype = row.get("violation_type") or ""
        label = self._describe_code(vtype) or row.get("violation_label") or ""
        parts.append(f"违规判定：{label or vtype or '疑似违规'} ｜ 风险等级 {row.get('risk_level') or 'low'} ｜ 置信度 {_num(row.get('confidence')):.0%}")

        # ① 轨迹分析
        parts.append("")
        parts.append("① 轨迹分析")
        raw_track = detail.get("track") or []
        if events:
            for ev in events[:4]:
                et_label = self._describe_code(ev.get("candidate_type") or "")
                zname = ev.get("zone_name") or ev.get("zone_id") or "未知区域"
                st = self._fmt_time(ev.get("start_time"))
                en = self._fmt_time(ev.get("end_time"))
                evpts = ev.get("evidence_points") or []
                feats = ev.get("features") or {}
                parts.append(f"  · 事件：{et_label} ｜ 区域：{zname}")
                gap_ctx = self._build_gap_context(ev, raw_track)
                gap_min = feats.get("gap_minutes")
                if gap_ctx and gap_min:
                    # AIS 中断类：单点无法说明违规，补中断前/中断时长/恢复后
                    parts.append(f"    AIS 信号中断 {_num(gap_min):.0f} 分钟（约 {_num(gap_min) / 60:.1f} 小时），期间无任何信号回传")
                    pre, post = gap_ctx["pre_point"], gap_ctx["post_point"]
                    if pre:
                        pre_loc = self._fmt_latlon(pre.get("lat"), pre.get("lon"))
                        if pre_loc:
                            parts.append(f"    中断前：{self._fmt_time(pre.get('timestamp'))}  最后正常信号 {pre_loc}")
                    if post:
                        post_loc = self._fmt_latlon(post.get("lat"), post.get("lon"))
                        if post_loc:
                            parts.append(f"    恢复后：{self._fmt_time(post.get('timestamp'))}  {post_loc}")
                else:
                    if st and en:
                        parts.append(f"    时间：{st} ~ {en}")
                    if evpts:
                        parts.append(f"    证据点：{len(evpts)} 个")
                        p0, pn = evpts[0], evpts[-1]
                        loc0 = self._fmt_latlon(p0.get("lat"), p0.get("lon"))
                        locn = self._fmt_latlon(pn.get("lat"), pn.get("lon"))
                        if loc0 and locn:
                            # 单证据点或首尾同点：显示"位置"而非误导性的"区间 A→A"
                            if len(evpts) <= 1 or loc0 == locn:
                                parts.append(f"    位置：{loc0}")
                            else:
                                parts.append(f"    区间：{loc0} → {locn}")
                if feats.get("average_speed") is not None:
                    parts.append(f"    平均航速 {_num(feats['average_speed']):.1f} 节")
                if feats.get("max_speed") is not None:
                    parts.append(f"    最高航速 {_num(feats['max_speed']):.1f} 节")
                if feats.get("duration_minutes") is not None:
                    parts.append(f"    持续 {_num(feats['duration_minutes']):.0f} 分钟")
        if fm.get("avg_speed") is not None:
            parts.append(f"  · 全程平均航速 {_num(fm['avg_speed']):.1f} 节，最高 {_num(fm.get('max_speed')):.1f} 节")
        if qr.get("level"):
            parts.append(f"  · AIS 数据质量：{qr['level']}")
        if anns:
            v_labels = sorted({
                a.get("label") for a in anns
                if (a.get("label") or "") and a.get("type") in (
                    "speed_violation", "low_speed_fishing", "restricted_area_entry",
                    "ais_gap", "position_jump", "course_anomaly", "rule_hit",
                )
            })
            if v_labels:
                parts.append(f"  · L1 打标：{'、'.join(v_labels[:8])}")

        # ② 规则匹配（regulations.json）
        parts.append("")
        parts.append("② 规则匹配（regulations.json）")
        ve = row.get("violation_event") or ""
        if ve:
            parts.append(f"  · {ve[:300]}")
        else:
            parts.append(f"  · 命中违规类型：{label or vtype or '未知'}，依据见下方法规引用")

        # ③ 法规引用
        parts.append("")
        parts.append("③ 法规引用")
        parts.append(f"  · {row.get('regulation_ref') or '（该违规暂无具体法规引用，仅有违规类型判定）'}")

        # ④ LLM 复核
        parts.append("")
        parts.append("④ LLM 复核")
        fa = (row.get("final_assessment") or "").strip()
        if fa:
            parts.append(f"  · {fa[:300]}")
        ra = (row.get("recommended_action") or "").strip()
        if ra:
            parts.append(f"  · 建议处置：{ra}")

        return "\n".join(parts)

    # ── 结构化数据（供前端渲染判定卡片 / 事件表格 / 轨迹图）──

    @staticmethod
    def _sample(points: list, n: int = 300) -> list:
        """轨迹点过多时按步长采样，避免前端 JSON 过大。"""
        if len(points) <= n:
            return points
        step = len(points) / n
        return [points[int(i * step)] for i in range(n)]

    @staticmethod
    def _build_gap_context(ev: dict, raw_track: list) -> dict | None:
        """AIS 中断类事件补上下文：证据点通常只存"中断恢复后首点"，
        单点无法说明违规。用该点时间戳在原始轨迹中定位，取前一个点作为
        "中断前最后信号点"，配合 features.gap_minutes 构成完整证据链
        （中断前 → 中断 N 分钟 → 恢复后）。非中断类事件返回 None。"""
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

    def _build_evidence_data(self, mmsi: str, row: dict, detail: dict) -> dict:
        # 委托公共模块（shipclaw/evidence.py），与 Web API /violations/<id>/evidence 共用同一实现
        from .evidence import build_evidence_data
        return build_evidence_data(mmsi, row, detail)


# ── 9. 我的数据概况 ──

class UserOverviewSkill:
    """数据概况——汇总当前用户辖区总体情况。"""
    name = "user_overview"
    label = "我的数据概况"
    description = "汇总该用户辖区总体情况：船舶数、AIS 数据量、违规数、风险分布"
    icon = "📋"
    params = []
    min_level = 3
    exp_reward = 9

    def match(self, message: str) -> float:
        msg = message.lower()
        if any(kw in msg for kw in ["我的数据", "数据概况", "辖区概况", "我的辖区", "总体情况", "总览", "我的辖区数据", "概况"]):
            return 0.88
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        user_id = context.get("user_id", "system")
        try:
            import pg_db
            conn = pg_db.connect()
            scope_sql, scope_params = pg_db.user_scope(conn, user_id)
            from_scope = f" WHERE {scope_sql}" if scope_sql else ""
            and_scope = f" AND {scope_sql}" if scope_sql else ""
            ships = conn.execute(f"SELECT COUNT(DISTINCT mmsi) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]
            total = conn.execute(f"SELECT COUNT(*) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]
            vcount = conn.execute(f"SELECT COUNT(*) as c FROM detection_results WHERE violation_detected = 1{and_scope}", scope_params).fetchone()["c"]
            points = conn.execute(f"SELECT COALESCE(SUM(track_points), 0) as c FROM detection_results{from_scope}", scope_params).fetchone()["c"]
            conn.close()
            return f"当前辖区概况：船舶 {ships} 艘，检测记录 {total} 条，AIS 数据 {points:,} 条，违规 {vcount} 起。"
        except Exception as e:
            return f"查询数据概况时出错：{e}"


# ── 9. 用户自定义技能（开发者提交 skill.md） ──

CUSTOM_SKILL_TEMPLATE = """---
name: my_skill
label: 我的技能
icon: 🧩
description: 用一句话说明这个技能能做什么（会显示给用户）
min_level: 1
trigger_keywords: 关键词1, 关键词2, 关键词3
---

# 我的技能 操作手册

## 触发条件
当用户提到哪些内容时使用本技能（与上方 trigger_keywords 保持一致）。

## 操作步骤
1. 第一步做什么
2. 第二步做什么

## 输出规范
- 回答应包含哪些内容、用什么格式呈现

## 禁止事项
- 禁止编造数据，一切以实际信息为准
- 超出本技能范围的问题，如实说明或引导使用其它功能
"""


def _split_frontmatter(text: str) -> tuple[str, str, str]:
    """拆 skill.md：返回 (frontmatter 文本, 正文, 元信息 dict)。格式不符抛 ValueError。"""
    if not text.strip().startswith("---"):
        raise ValueError("必须以 --- 开头编写 YAML 元信息（name / label / icon / trigger_keywords 等）")
    parts = text.split("---", 2)
    if len(parts) < 3 or not parts[1].strip():
        raise ValueError("缺少或空的 YAML 元信息段，请用 --- 包裹")
    fm, body = parts[1], parts[2]
    meta = {}
    for line in fm.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        meta[k.strip().lower()] = v.strip()
    return fm, body.strip(), meta


def parse_skill_keywords(raw: str) -> list[str]:
    """解析 trigger_keywords：支持逗号/中文顿号/换行分隔，以及 \"- item\" 列表。"""
    keywords = []
    for chunk in re.split(r"[,，、\n]+", raw or ""):
        chunk = chunk.strip()
        if chunk.startswith("- "):
            chunk = chunk[2:].strip()
        if chunk:
            keywords.append(chunk)
    return keywords


def validate_custom_skill_md(text: str) -> dict:
    """校验用户提交的 skill.md，返回规范化元信息与手册正文；不合法抛 ValueError。"""
    fm, body, meta = _split_frontmatter(text)

    name = (meta.get("name") or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", name):
        raise ValueError("name 需为小写字母开头的英文标识（字母/数字/下划线，≤31 字符），如 my_skill")
    if name in {s.name for s in ALL_SKILLS}:
        raise ValueError(f"技能名 {name} 与内置技能冲突，请换一个（内置：{', '.join(s.name for s in ALL_SKILLS)}）")

    label = (meta.get("label") or "").strip()
    if not label or len(label) > 30:
        raise ValueError("label（显示名）必填，且不超过 30 字")

    icon = (meta.get("icon") or "🧩").strip()[:8]
    if not icon:
        icon = "🧩"

    description = (meta.get("description") or "").strip()
    if not description:
        raise ValueError("description（技能说明）必填，用户会看到它")

    try:
        min_level = int(meta.get("min_level") or 1)
    except (TypeError, ValueError):
        raise ValueError("min_level 需为 1~10 的数字")
    if not 1 <= min_level <= 10:
        raise ValueError("min_level 需为 1~10")

    keywords = parse_skill_keywords(meta.get("trigger_keywords") or "")
    if not keywords:
        raise ValueError("trigger_keywords（触发词）至少需要一个，用逗号分隔")

    if not body:
        raise ValueError("操作手册正文不能为空（触发条件/操作步骤/输出规范/禁止事项）")

    return {
        "name": name,
        "label": label,
        "icon": icon,
        "description": description,
        "min_level": min_level,
        "trigger_keywords": keywords,
        "manual_md": text.strip(),
        "manual_body": body,
    }


class CustomSkill:
    """用户自定义技能——LLM 驱动：命中触发词即路由到本技能，操作手册注入 LLM。

    不执行任意代码（安全）：execute 返回 __OLLAMA__，由 ShipClaw 把
    self.manual 注入系统提示，让 LLM 严格按手册作答。
    """
    name = ""
    label = ""
    description = ""
    icon = "🧩"
    params = []
    min_level = 1
    exp_reward = 6
    MATCH_SCORE = 0.8  # 高于通用内置词匹配(0.7)，低于高特定内置匹配(0.85+)

    def __init__(self, *, name, label, description, icon="🧩", min_level=1,
                 trigger_keywords, manual_md, skill_id=None, status="active"):
        self.name = name
        self.label = label
        self.description = description
        self.icon = icon or "🧩"
        self.min_level = int(min_level or 1)
        self.trigger_keywords = [k.strip() for k in trigger_keywords if k and k.strip()]
        # 存"正文"作手册（剥掉 frontmatter），供 _execute_skill 直接注入
        self.manual = _strip_manual_body(manual_md)
        self.skill_id = skill_id
        self.status = status

    def match(self, message: str) -> float:
        msg = message.lower()
        for kw in self.trigger_keywords:
            if kw and kw.lower() in msg:
                return self.MATCH_SCORE
        return 0.0

    def execute(self, message: str, context: dict) -> str:
        # 纯 LLM 驱动：不查询数据，手册在 _execute_skill 注入
        return "__OLLAMA__"

    @classmethod
    def from_row(cls, row: dict) -> "CustomSkill":
        return cls(
            skill_id=row.get("id"),
            name=row["name"],
            label=row["label"],
            description=row.get("description", ""),
            icon=row.get("icon", "🧩"),
            min_level=row.get("min_level", 1),
            trigger_keywords=(row.get("trigger_keywords") or "").split(","),
            manual_md=row.get("manual_md", ""),
            status=row.get("status", "active"),
        )


def _strip_manual_body(md_text: str) -> str:
    """去掉 skill.md 的 YAML frontmatter，返回手册正文。"""
    try:
        _fm, body, _meta = _split_frontmatter(md_text)
        return body
    except ValueError:
        return md_text.strip()


def load_user_custom_skills(user_id: str) -> list[CustomSkill]:
    """加载用户的自定义技能（仅 active）；system/default/空 → 无。"""
    if not user_id or user_id in ("system", "default"):
        return []
    try:
        import pg_db
        conn = pg_db.connect()
        rows = conn.execute(
            "SELECT id, name, label, icon, description, min_level, trigger_keywords, manual_md, status "
            "FROM custom_skills WHERE user_id = ? AND status = 'active' ORDER BY id",
            (user_id,),
        ).fetchall()
        conn.close()
        return [CustomSkill.from_row(r) for r in rows]
    except Exception:
        return []


# ── 渲染型工具（MCP 工具：画图/写表格/写文档，内联渲染不落文件） ──
from .render_skills import ChartToolSkill, TableToolSkill, DocToolSkill

# ── 技能注册表 ──

ALL_SKILLS = [
    CasualChatSkill(),
    ShipQuerySkill(),
    RegulationQASkill(),
    TrendAnalysisSkill(),
    CollisionAlertSkill(),
    ShipListSkill(),
    ViolationListSkill(),
    ViolationEvidenceSkill(),
    UserOverviewSkill(),
    # 渲染型工具（MCP 工具）
    ChartToolSkill(),
    TableToolSkill(),
    DocToolSkill(),
]


def get_tool_catalog() -> list[dict]:
    """返回全部工具的目录元数据（前端展示工具列表/说明用）。"""
    return [
        {
            "name": s.name,
            "label": s.label,
            "description": s.description,
            "icon": s.icon,
            "params": s.params,
            "min_level": s.min_level,
        }
        for s in ALL_SKILLS
    ]


def match_best_skill(message: str, extra_skills: Optional[list[Any]] = None) -> tuple[Any, float]:
    """匹配最佳技能。

    Args:
        message: 用户消息
        extra_skills: 额外技能（如用户自定义技能 CustomSkill），与内置技能一起参与匹配

    Returns:
        (skill, match_score) 匹配度最高的技能及其分数

    平局规则：分数相同且一方是自定义技能时，**自定义技能优先**——
    用户自己做的技能应盖过同分的内置/闲聊（如自定义"天气"0.8 vs 闲聊"今天"0.8）。
    """
    pool = list(ALL_SKILLS)
    extra_names = set()
    if extra_skills:
        pool.extend(extra_skills)
        extra_names = {s.name for s in extra_skills}

    best_skill = pool[0]  # 默认闲聊（casual_chat 未匹配返回 0.1 兜底）
    best_score = 0.0
    best_is_extra = best_skill.name in extra_names

    for skill in pool:
        score = skill.match(message)
        is_extra = skill.name in extra_names
        if score > best_score or (score == best_score and is_extra and not best_is_extra):
            best_score = score
            best_skill = skill
            best_is_extra = is_extra

    return best_skill, best_score
