"""
shipclaw/core.py — ShipClaw 主类
==================================

【这个文件做什么？】
  ShipClaw 的核心入口：对话 + 技能路由 + 记忆读写 + 人格渲染。

【对话流程】
  1. 加载用户的人格和记忆（ClawMemory）
  2. 技能匹配（选匹配度最高的技能）
  3. 检查等级是否解锁
  4. 执行技能，获取原始结果
  5. 用人格风格渲染回复
  6. 更新记忆、经验、性格
  7. 返回回复 + 成长信息

【Ollama 集成】
  闲聊和人格渲染通过 Ollama Qwen2.5 实现。
  技能执行的结果作为上下文注入 Ollama prompt。
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .claw_memory import ClawMemory
from .personality import Personality, SKILL_EXP_REWARDS
from .skills import (
    ALL_SKILLS,
    match_best_skill,
    CasualChatSkill,
    load_skill_manual,
    load_user_custom_skills,
)


class ShipClaw:
    """ShipClaw — 可养成的海事智能体。

    用法:
        claw = ShipClaw()
        result = claw.chat("user_001", "禁渔区有什么规定？")
        print(result["reply"])       # 法规回答
        print(result["level"])       # 当前等级
        print(result["exp_gained"])  # 本次获得经验
    """

    def __init__(
        self,
        claw_memory: Optional[ClawMemory] = None,
        regulation_db: Optional[Any] = None,
        db_path: str = "data/ais.db",
        model_name: str = "qwen2.5:7b-instruct-q4_K_M",
        ollama_url: str = "http://localhost:11434",
    ):
        self.claw_memory = claw_memory or ClawMemory()
        self.regulation_db = regulation_db
        self.db_path = db_path
        self.model_name = model_name
        self.ollama_url = ollama_url

    def chat(self, user_id: str, message: str) -> dict[str, Any]:
        """核心对话方法。

        Args:
            user_id: 用户 ID
            message: 用户消息

        Returns:
            {
                "reply": 回复文本,
                "skill": 使用的技能名,
                "level": 当前等级,
                "exp_gained": 本次获得经验,
                "level_up": 是否升级,
                "personality": 人格状态,
                "growth_event": 成长事件（如有），
                "structured": 结构化证据数据（违规证据链技能；其余为 None）
            }
        """
        # 1. 加载人格和偏好
        personality = self.claw_memory.load_personality(user_id)
        preferences = self.claw_memory.load_preferences(user_id)
        chat_context = self.claw_memory.get_chat_context(user_id)

        # 2. 技能匹配（内置 + 该用户自定义技能）
        custom_skills = load_user_custom_skills(user_id)
        skill, match_score = match_best_skill(message, custom_skills)
        intended_skill = skill  # 记录用户真正想触发的工具（可能因等级锁定而降级）
        # 自定义技能用自身 min_level 判定解锁；内置技能走人格等级表
        if hasattr(skill, "min_level"):
            locked = personality.level < skill.min_level
        else:
            locked = not personality.can_use_skill(skill.name)

        # 3. 检查等级是否解锁
        if locked:
            if hasattr(skill, "min_level"):
                locked_reason = (
                    f"该功能需要 Lv{skill.min_level} 解锁（当前 Lv{personality.level}，"
                    f"还差 {personality.exp_to_next_level()} 经验）"
                )
            else:
                locked_reason = personality.skill_locked_reason(skill.name)
            # 降级到闲聊
            skill = CasualChatSkill()
            match_score = 0.1
            # 在回复中提示解锁条件
            unlock_hint = f"\n\n💡 {locked_reason}"

        # 执行技能（等级锁定走闲聊，解锁走真实技能）
        raw_result = self._execute_skill(
            skill, message, personality, preferences, chat_context, user_id
        )
        # 确定性技能（如违规证据链）可能返回 {"text","data"}，拆出结构化数据
        raw_result, structured_data = self._split_result(raw_result)
        reply = (raw_result + unlock_hint) if locked else raw_result

        # 5. 更新经验、性格
        exp_gained = personality.record_skill_use(skill.name)
        base_exp = 5  # 每次交互基础经验
        total_exp = base_exp + exp_gained
        leveled_up = personality.add_exp(total_exp, f"使用技能 {skill.name}")

        # 性格成长
        grown_trait = personality.grow_trait(skill.name)

        # 6. 保存记忆
        self.claw_memory.save_personality(user_id, personality)
        self.claw_memory.record_interaction(
            user_id=user_id,
            message=message,
            reply=reply[:200],
            skill=skill.name,
            personality=personality,
        )

        # 7. 构建返回
        growth_event = None
        if leveled_up:
            growth_event = f"升级到 Lv{personality.level}！"
        elif grown_trait:
            growth_event = f"{grown_trait} +{2}"

        # 8. 工具调用记录：告诉前端调用了哪个工具、做什么的、结果如何
        # 等级锁定时仍展示"用户想要的工具 + 锁定状态"，便于用户知道需要升级解锁
        shown = intended_skill if locked else skill
        tool_calls = [{
            "name": shown.name,
            "label": getattr(shown, "label", shown.name),
            "description": getattr(shown, "description", ""),
            "icon": getattr(shown, "icon", "🔧"),
            "params": getattr(shown, "params", []),
            "status": "locked" if locked else "ok",
            "summary": (raw_result or "")[:120],
        }]

        # 取出渲染型工具产生的 blocks（如有）
        blocks = getattr(ShipClaw, "_pending_blocks", None)
        ShipClaw._pending_blocks = None

        return {
            "reply": reply,
            "skill": skill.name,
            "tool_calls": tool_calls,
            "level": personality.level,
            "exp_gained": total_exp,
            "level_up": leveled_up,
            "personality": personality.to_dict(),
            "growth_event": growth_event,
            "structured": structured_data,   # 结构化证据数据（违规证据链技能），无则为 None
            "blocks": blocks or None,        # 渲染型工具产出的渲染块（chart/table/doc），无则为 None
        }

    @staticmethod
    def _split_result(raw_result):
        """确定性技能可能返回：
        - {"text","data"}（旧：违规证据链）
        - {"text","blocks"}（新：渲染型工具 chart/table/doc）
        - {"text","data","blocks"}（混合）
        拆成 (文本, 结构化数据)；blocks 存入 result["blocks"]。
        其余情况原样透传 (raw_result, None)。"""
        if isinstance(raw_result, dict):
            text = raw_result.get("text", "")
            data = raw_result.get("data")
            blocks = raw_result.get("blocks")
            if blocks:
                # 把 blocks 挂到临时字段，由 chat() 取出
                ShipClaw._pending_blocks = blocks
            return text, data
        return raw_result, None

    def _execute_skill(
        self,
        skill: Any,
        message: str,
        personality: Personality,
        preferences: dict,
        chat_context: str,
        user_id: str = "system",
    ) -> dict | str:
        """执行技能并用 Ollama 渲染回复。确定性技能可能返回 {"text","data"}。"""
        # 构建技能上下文（user_id 用于数据查询按用户隔离）
        context = {
            "regulation_db": self.regulation_db,
            "db_path": self.db_path,
            "preferences": preferences,
            "user_id": user_id,
        }

        # 加载技能操作手册（skill.md）：注入 LLM，让它严格按手册操作，避免答非所问
        # 自定义技能自带 manual（存于 DB）；内置技能从 shipclaw/skills/ 读取
        manual = getattr(skill, "manual", "") or load_skill_manual(skill.name)
        manual_label = getattr(skill, "label", skill.name)

        # 执行技能
        raw_result = skill.execute(message, context)

        # 如果技能返回 __OLLAMA__，直接用 Ollama 生成（闲聊也要注入手册防编造数据）
        if raw_result == "__OLLAMA__":
            return self._call_ollama_chat(
                message, personality, chat_context, "", manual=manual, manual_label=manual_label
            )

        # 确定性技能（如违规证据链）：查询结果即最终答复，跳过 LLM 人格渲染。
        # 防止 LLM 在"查无记录/无违规/证据链"等确定性结果上加戏编造。
        if getattr(skill, "deterministic", False):
            return raw_result

        # 否则，用 Ollama 基于技能结果渲染自然语言回复
        return self._render_with_personality(
            message, raw_result, personality, chat_context, manual=manual, manual_label=manual_label
        )

    def _render_with_personality(
        self,
        message: str,
        skill_result: str,
        personality: Personality,
        chat_context: str,
        manual: str = "",
        manual_label: str = "",
    ) -> str:
        """用 Ollama 基于技能结果和人格风格渲染回复。"""
        tone_instruction = personality.get_system_prompt_suffix()

        system_prompt = (
            f"你是港口船舶智能助手「小慧」，运行在智海巡航系统上。当前等级 Lv{personality.level}。\n"
            f"回答要自然、口语化。{tone_instruction}\n"
        )
        # 技能操作手册：LLM 必须按手册操作
        if manual:
            system_prompt += (
                f"\n【当前技能操作手册：{manual_label}】\n"
                f"请严格按以下手册操作本技能，不得超出其范围：\n"
                f"{manual}\n"
            )
        system_prompt += (
            f"\n以下是你内部系统的查询结果，请基于这些结果回答用户问题，不要暴露内部查询过程：\n"
            f"{skill_result}\n"
        )

        if chat_context:
            system_prompt += f"\n{chat_context}\n"

        return self._call_ollama_chat(message, personality, chat_context, system_prompt)

    def _call_ollama_chat(
        self,
        message: str,
        personality: Personality,
        chat_context: str,
        system_prompt: str,
        manual: str = "",
        manual_label: str = "",
    ) -> str:
        """调用 Ollama 生成回复。"""
        import llm_config

        # 开发者模式：模型/地址/参数全部调用时读取，改完即时生效；读不到回退实例属性
        cfg = llm_config.get_config()
        model_name = cfg.get("model") or self.model_name
        ollama_url = cfg.get("base_url") or self.ollama_url
        temperature = float(cfg.get("temperature", 0.7))
        options = llm_config.get_ollama_options()
        cfg_system = (cfg.get("system_prompt") or "").strip()

        if not system_prompt:
            tone_instruction = personality.get_system_prompt_suffix()
            system_prompt = (
                f"你是港口船舶智能助手「小慧」，运行在智海巡航系统上。当前等级 Lv{personality.level}。\n"
                f"回答要自然、口语化，像真人对话一样。{tone_instruction}\n"
            )
            if manual:
                system_prompt += (
                    f"\n【当前技能操作手册：{manual_label}】\n"
                    f"请严格按以下手册操作本技能，不得超出其范围：\n"
                    f"{manual}\n"
                )
            if chat_context:
                system_prompt += f"\n{chat_context}\n"
        if cfg_system:
            system_prompt = f"{cfg_system}\n\n{system_prompt}"

        try:
            import urllib.request
            full_prompt = f"{system_prompt}\n\n用户：{message}\n小慧："
            payload = json.dumps({
                "model": model_name,
                "prompt": full_prompt,
                "stream": False,
                "temperature": temperature,
                "options": options,
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{ollama_url}/api/generate",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            import urllib.request as _urllib
            with _urllib.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read().decode("utf-8"))
                return result.get("response", "").strip()
        except Exception as e:
            # Ollama 不可用时的降级回复
            return f"抱歉，我暂时无法连接到语言模型。请稍后再试。（错误：{str(e)[:50]}）"

    # ── 状态查询 ──

    def get_status(self, user_id: str) -> dict:
        """获取用户的养成状态。"""
        return self.claw_memory.get_status(user_id)

    def get_growth_log(self, user_id: str, limit: int = 20) -> list[dict]:
        """获取成长日志。"""
        return self.claw_memory.get_growth_log(user_id, limit)

    def update_personality(self, user_id: str, traits: dict[str, int]) -> dict:
        """手动调整性格维度。"""
        personality = self.claw_memory.load_personality(user_id)
        for trait, value in traits.items():
            if trait in personality.traits:
                personality.traits[trait] = max(0, min(100, value))
        self.claw_memory.save_personality(user_id, personality)
        return personality.to_dict()
