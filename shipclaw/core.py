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
    load_enabled_plugins,
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
        # 0. 造插件意图 —— 必须在技能匹配**之前**拦截。
        #
        # 不把它做成一个 Skill 的理由：造插件是**元操作**，它的产物会成为技能池里的
        # 新成员。放进同一个池子里，「做个禁渔区插件」和「禁渔区怎么判」词面高度重叠
        # （都含"禁渔区"），谁赢全靠权重调参，很脆。这里用确定性关键词显式拦截。
        from plugin_runtime import detect_forge_intent, forge_plugin
        if detect_forge_intent(message):
            return self._forge(user_id, message)

        # 1. 加载人格和偏好
        personality = self.claw_memory.load_personality(user_id)
        preferences = self.claw_memory.load_preferences(user_id)
        chat_context = self.claw_memory.get_chat_context(user_id)

        # 2. 技能匹配（内置 + 已启用的可执行插件）
        #    插件是**全局**的（不按用户），与「全局共享，超管启用」的作用域一致。
        extra_skills = load_enabled_plugins()
        skill, match_score = match_best_skill(message, extra_skills)
        intended_skill = skill  # 记录用户真正想触发的工具（可能因等级锁定而降级）
        # 插件自带 min_level 判定解锁；内置技能走人格等级表
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

    def _forge(self, user_id: str, message: str) -> dict[str, Any]:
        """造插件意图的独立返回路径：不走技能匹配、不加经验、不写对话记忆。

        为什么不加经验：造插件是一次性的管理动作，不是「使用技能」。
        给它记经验会让养成曲线被刷（连说十遍"做个插件"就升级了）。
        为什么不写记忆：草稿内容会污染后续对话上下文。
        """
        from plugin_runtime import forge_plugin

        try:
            import pg_db
            row = pg_db.connect().execute(
                "SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            username = (row["username"] if row else "") or ""
        except Exception:
            username = ""

        result = forge_plugin(user_id, message, username=username)

        if result.get("ok"):
            reply = (
                f"🧩 插件草稿已生成：**{result['label']}**（`{result['name']}`）\n\n"
                f"· 生成轮次：{result['attempts']} 次"
                + (f"（前 {result['attempts'] - 1} 次没过校验，已按报错重写）"
                   if result["attempts"] > 1 else "") + "\n"
                f"· 静态校验：AST {result['validation'].get('ast_nodes', '?')} 节点，"
                f"注册工具 {result['validation'].get('registered_tools', [])}\n"
                f"· 动态试跑：{len(result.get('trial') or [])} 份样本全部通过"
                f"（含「不该命中」的样本）\n\n"
                "草稿**还没有生效**——造出来的插件不会自动上线，"
                "得由超级管理员过目后手动点「启用」。\n"
                "点下面的「查看草稿」可以直接跳到它的行上。\n"
                "启用后**对所有用户生效**：所有人的判定批次都会跑它。"
            )
        else:
            # 按 kind 分别解释。四种失败的**成因完全不同**，用同一段「换个说法」
            # 兜住全部，等于对系统故障说「是你需求写得不好」——用户会照做着改半天
            # 而不可能改好。（2026-10-10：一个模板里的 KeyError 被这样包装了。）
            kind = result.get("kind") or "internal"
            if kind == "validation":
                why = ("模型写的代码没过校验（导入白名单、禁用名字、双下划线属性），"
                       "或者判定写成了「恒真」——任何输入都返回违规，"
                       "被内置的对照样本挡下了。\n"
                       "可以换个说法再试一次，把判定条件讲具体些"
                       "（比如「区内停留超过 20 分钟且平均速度低于 3 节」）。")
            elif kind == "duplicate":
                why = "系统里已经有同名插件了。换个名字，或直接编辑已有的那一个。"
            elif kind == "model":
                # forge 已经把具体成因写进 error 了（超时 / 连不上 / 截断 / 格式），
                # 这里只需点明「不是你的写法问题」并给出改的地方。
                why = ("这条不是你的需求写法问题 —— 具体成因见上面那行。\n"
                       "「起草」用途用的模型可以在「开发者 → 提供商」里换。")
            else:
                why = ("这是系统内部故障，**不是你的描述问题**——换个说法也一样。\n"
                       "服务端日志里有完整堆栈，麻烦把它发给开发者。")
            reply = ("插件没能生成出来。\n\n"
                     f"原因：{result.get('error') or '未知'}\n\n" + why)
        return {
            "reply": reply,
            "skill": "plugin_forge",
            "tool_calls": [{
                "name": "plugin_forge",
                "label": "AI 造插件",
                "description": "根据自然语言需求生成可执行判定插件草稿",
                "icon": "🧩",
                "params": [],
                "status": "ok" if result.get("ok") else "fail",
                "summary": (result.get("name") or result.get("error") or "")[:120],
            }],
            "level": None,
            "exp_gained": 0,
            "level_up": False,
            "personality": None,
            "growth_event": None,
            "structured": None,
            "blocks": None,
            # 前端据此渲染「查看草稿」按钮，跳到 /dev?tab=plugins
            "forge": result if result.get("ok") else None,
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
        """调用模型生成回复。

        传输统一走 llm_providers.complete()（「对话与智能体」绑定的提供商）——
        本地 Ollama 与 OpenAI 兼容网关同一个出口，所以把 chat 绑到云端时这条链路也是通的。
        prompt 组装保持原样，保证默认本地 Ollama 下发出的请求与改造前逐字相同。
        """
        import llm_config
        import llm_providers

        # 开发者模式：模型/地址/参数全部调用时读取，改完即时生效；读不到回退实例属性
        cfg = llm_config.get_config()
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
            full_prompt = f"{system_prompt}\n\n用户：{message}\n小慧："
            text, err = llm_providers.complete(full_prompt, purpose="chat", timeout=60)
            if err:
                return f"抱歉，我暂时无法连接到语言模型。请稍后再试。（错误：{err[:50]}）"
            return text
        except Exception as e:
            # 模型不可用时的降级回复
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
