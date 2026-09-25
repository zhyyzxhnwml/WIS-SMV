"""
llm_layer/handler.py — LLM 调用处理器
=======================================

【这个文件做什么？】
  封装 LLM 的调用流程：
  1. 根据路由结果选择 Prompt 模板
  2. 渲染数据（轨迹/标记/事件/相似案例）填充模板
  3. 调用 LLM（Ollama 本地模型）
  4. 解析 JSON 结果
  5. 提取 feedback_for_ml

【数据渲染】
  - 截断轨迹点防止超长上下文（默认 ≤200 个点）
  - 生成事件摘要和规则命中摘要
  - 附上 pgvector/Milvus 相似案例供参考
  - 各字段均有字符数上限，防止爆上下文窗口

【错误处理】
  - LLM 不可用 → 抛出 RuntimeError，由调用方决定如何处理
  - JSON 解析失败 → 返回带 error 标记的字典
  - 调用异常 → 返回安全默认值（不报违规）
"""

from __future__ import annotations

import json  # JSON编解码模块
import threading  # 配置重建锁（防止并发重建竞态）
import time  # 时间模块（用于性能计时）
import urllib.request  # 轻量 HTTP 探测（不依赖 requests 库）
from typing import Any, Optional  # 类型提示

import llm_config  # 全局模型/参数配置（开发者模式，调用时读取）
from .prompts import PROMPT_TEMPLATES  # 四级处理深度的Prompt模板字典

try:
    from langchain_ollama import ChatOllama  # Ollama LLM调用接口
    HAS_OLLAMA = True  # 是否成功导入Ollama库的标记
except ImportError:  # 导入失败
    HAS_OLLAMA = False  # Ollama库不可用标记

# [诊断用] 让初始化是否成功、失败在哪一步，直接打到标准输出（run.py 的 stdout 会透传到前端日志）
_LLM_DIAG = True


def _quality_level_of(qr: dict) -> str:
    """从质量报告的 issues 推导综合质量等级（good/fair/poor）。

    注意：QualityReport.level 是 @property，model_dump(mode='json') 不会序列化它
    （run.py 写 quality_level 列时直接访问 .level），此处必须从 issues 自己推导。
    """
    if not qr:
        return "good"
    sevs = {i.get("severity") for i in qr.get("issues", [])}
    if "critical" in sevs:
        return "poor"
    if "warning" in sevs:
        return "fair"
    return "good"


class LLMHandler:
    """LLM 分层处理器。

    职责：根据 ML Triage 的路由结果，选择合适的 Prompt 模板，
    渲染数据，调用 LLM，解析 JSON 结果，并提取 feedback。

    重要：本系统必须依赖 LLM 模型运行，不支持无模型工作。
    模型未启动时，analyze() 会抛出 RuntimeError。
    """

    def __init__(
        self,
        model_name: str = "qwen2.5:7b-instruct-q4_K_M",  # LLM模型名称（默认Qwen2.5量化版）
        base_url: str = "http://localhost:11434",  # Ollama服务地址（默认本地端口）
        temperature: float = 0.0,  # LLM采样温度（0=确定性输出）
    ):
        self.model_name = model_name  # LLM模型名称
        self.base_url = base_url  # Ollama服务地址
        self.temperature = temperature  # 采样温度
        self.llm = None  # LLM客户端实例（初始为None，延迟初始化）
        self._model_available = False  # 模型是否可用
        self._cfg_snapshot = None  # 配置快照（比对是否变化，变化则重建客户端）
        self._llm_lock = threading.Lock()  # 重建锁（防止并发重建竞态）
        self._init_llm()  # 尝试初始化 LLM
        self._cfg_snapshot = self._config_signature()  # 记录初始化时的配置快照

    def _ollama_up(self, timeout: float = 8.0) -> bool:
        """真实探测 Ollama 服务是否已能响应（HTTP 层，非 gRPC）。

        注意：Ollama 启动早期 HTTP 端点会返回 500/拒连（服务尚未就绪），
        这符合“已启动但此刻尚不稳定”的真实场景；能连上并读到模型列表才算可用。
        timeout 由 3s 提到 8s：某些机器上 Ollama 服务虽已启动、但模型首次载入时，
        /api/tags 响应会被并发地或启动期阻塞超过 3s，导致可用被误判为不可用。
        """
        if _LLM_DIAG:
            print(f"    [LLM-diag] _ollama_up({timeout}s) 探测 {self.base_url}/api/tags ...", flush=True)
        try:
            req = urllib.request.Request(
                f"{self.base_url}/api/tags",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                ok = resp.status == 200 and b"models" in body
                if _LLM_DIAG:
                    print(f"    [LLM-diag] _ollama_up → status={resp.status} 读到模型列表={ok} (len={len(body)})", flush=True)
                return ok
        except Exception as e:  # 联网/解析异常
            if _LLM_DIAG:
                print(f"    [LLM-diag] _ollama_up → FAILED {type(e).__name__}: {e}", flush=True)
            return False

    def _init_llm(self):
        """尝试初始化 LLM 客户端（可重复调用，用于自动恢复 / 配置变更重建）。

        修复：先做一次 HTTP 连通性探测，Ollama 能响应了才构造客户端；
        只有连通探测成功才标记模型可用，避免“构造成功=可用”的假阳性。
        注意：本次探测失败（Ollama 仍在启动早期）仅返回而不把状态钉死成 False，
        以便外层（analyze/_call_llm）下次调用时能再次尝试恢复。

        开发者模式：模型名/地址/参数从 llm_config 读取（调用时读取），
        配置变更后由 _ensure_llm() 触发重建，无需重启。
        """
        if _LLM_DIAG:
            print(f"    [LLM-diag] _init_llm() HAS_OLLAMA={HAS_OLLAMA}", flush=True)
        if not HAS_OLLAMA:
            return
        # 从全局配置读取（读不到回退实例属性），并缓存 num_predict/top_p/top_k
        try:
            cfg = llm_config.get_config()
            self.model_name = cfg.get("model", self.model_name)
            self.base_url = cfg.get("base_url", self.base_url)
            self.temperature = float(cfg.get("temperature", self.temperature))
            self._num_predict = cfg.get("num_predict")
            self._top_p = cfg.get("top_p")
            self._top_k = cfg.get("top_k")
        except Exception:
            self._num_predict = None
            self._top_p = None
            self._top_k = None
        try:
            # 先探测服务是否真的能响应；Ollama 启动早期会 500/拒连 → 不判可用
            if not self._ollama_up():
                self.llm = None
                # 不再强制置 False —— 为下次调用保留复活机会？不能：探测失败就保持“不可用”。
                # 见 _call_llm：不可用时会 `sleep` 后再次 _init_llm，若这里不置 False，
                # 下次循环的 `not self._model_available` 判 False 会误走 invoke。
                # 因此这里必须把状态置为不可用，但重试由 _call_llm 驱动。
                self._model_available = False
                return
            llm_kwargs = dict(  # 初始化Ollama聊天客户端
                model=self.model_name,  # 模型名称
                base_url=self.base_url,  # 服务地址
                temperature=self.temperature,  # 采样温度
            )
            if self._num_predict:  # 非空才传，避免覆盖 LangChain 默认
                llm_kwargs["num_predict"] = self._num_predict
            if self._top_p:  # 非空才传
                llm_kwargs["top_p"] = self._top_p
            if self._top_k:  # 非空才传
                llm_kwargs["top_k"] = self._top_k
            self.llm = ChatOllama(**llm_kwargs)
            self._model_available = True
            if _LLM_DIAG:
                print(f"    [LLM-diag] _init_llm → OK, model={self.model_name}, temp={self.temperature}, model_available=True", flush=True)
        except Exception as e:  # 初始化异常
            self.llm = None
            self._model_available = False
            if _LLM_DIAG:
                print(f"    [LLM-diag] _init_llm → EXC {type(e).__name__}: {e}", flush=True)

    def _config_signature(self) -> tuple:
        """当前 llm_config 中影响客户端的字段签名（用于变更检测）。"""
        cfg = llm_config.get_config()
        return (
            cfg.get("base_url"),
            cfg.get("model"),
            cfg.get("temperature"),
            cfg.get("num_predict"),
            cfg.get("top_p"),
            cfg.get("top_k"),
        )

    def _ensure_llm(self):
        """配置变更检测：签名与上次初始化不一致则加锁重建客户端（即时生效）。"""
        sig = self._config_signature()
        if self._cfg_snapshot == sig:
            return
        with self._llm_lock:
            # 双重检查：锁内再比对一次，避免拿到锁后发现已被其他线程重建
            if self._cfg_snapshot == sig:
                return
            self._cfg_snapshot = sig
            self._init_llm()

    @property
    def model_available(self) -> bool:
        """LLM 模型是否可用。"""
        return self._model_available and self.llm is not None

    def analyze(
        self,
        annotated_track: dict,  # 加速器输出的带标记轨迹字典
        routing: dict,  # 路由决策字典（RoutingDecision.to_dict()）
        max_track_points: int = 200,  # 轨迹点截断数（防止超长上下文）
    ) -> dict:
        """对一条带标记的轨迹做 LLM 判定。

        Args:
            annotated_track: 加速器输出的 AnnotatedTrack.to_agent_input()
            routing: 路由决策字典（RoutingDecision.to_dict()）
            max_track_points: 轨迹点截断数（防止超长上下文）

        Returns:
            dict: LLM 判定结果 + feedback
        """
        prompt_template = routing.get("llm_prompt_template", "quick_verify")  # 从路由中获取Prompt模板名称
        prompt_text = PROMPT_TEMPLATES.get(prompt_template, PROMPT_TEMPLATES["quick_verify"])  # 获取对应的Prompt模板文本

        # ── 渲染数据 ──
        rendered = self._render_prompt(  # 将数据渲染到Prompt模板中
            prompt_text,  # Prompt模板文本
            annotated_track,  # 带标记的轨迹数据
            routing,  # 路由决策信息
            max_track_points,  # 轨迹点截断数
        )

        # ── 调用 LLM ──
        started = time.perf_counter()  # 记录调用开始时间（高精度计时）
        result = self._call_llm(rendered)  # 调用LLM并获取结果字典
        latency = time.perf_counter() - started  # 计算LLM调用耗时（秒）

        # ── 标准化输出 ──
        result["routing"] = routing  # 将路由信息附加到结果中
        result["latency_seconds"] = round(latency, 3)  # 记录调用延迟（保留3位小数）
        result["model"] = self.model_name  # 记录使用的模型名称
        result["prompt_template"] = prompt_template  # 记录使用的Prompt模板名称

        # ── 数据质量标注（只加标注，不硬改判定；供前端徽章/审计）──
        qr = (annotated_track or {}).get("quality_report") or {}  # 质量报告（可能为空）
        dql = _quality_level_of(qr)  # 推导质量等级（good/fair/poor）
        cleaned = int(qr.get("invalid_points", 0) or 0)  # 清洗剔除点数
        result["data_quality_level"] = dql  # 数据质量等级
        result["data_quality_suspect"] = bool(dql != "good" or cleaned > 0)  # 有任何质量问题或剔除过点 → 数据存疑

        # ── 确保 feedback 存在 ──
        if "feedback_for_ml" not in result:  # 反馈字段不存在
            result["feedback_for_ml"] = {}  # 确保反馈字段存在（防止后续处理出错）

        return result  # 返回LLM判定结果

    def _summarize_quality(self, qr: dict) -> str:
        """生成数据质量摘要（注入 LLM prompt，让判定考虑数据可信度）。"""
        if not qr:
            return "无质量评估数据"
        level = _quality_level_of(qr)
        parts = [f"数据质量等级: {level}"]
        bits = []
        if qr.get("duplicate_points"):
            bits.append(f"重复点{qr['duplicate_points']}个")
        if qr.get("invalid_points"):
            bits.append(f"剔除异常点{qr['invalid_points']}个")
        if qr.get("retained_points") is not None:
            bits.append(f"保留{qr['retained_points']}点")
        if bits:
            parts.append("，".join(bits))
        codes = sorted({i.get("code") for i in qr.get("issues", [])})
        if codes:
            parts.append("问题: " + ",".join(codes))
        return "；".join(parts)

    def _render_prompt(
        self,
        template: str,  # Prompt模板文本
        track: dict,  # 轨迹数据字典
        routing: dict,  # 路由决策字典
        max_points: int,  # 最大轨迹点数（截断限制）
    ) -> str:
        """用数据填充 Prompt 模板。"""
        track_points = track.get("track", [])  # 从轨迹字典中提取轨迹点列表
        annotations = track.get("annotations", [])  # 从轨迹字典中提取标记列表
        events = track.get("events", [])  # 从轨迹字典中提取事件列表
        quality_report = track.get("quality_report", {})  # 从轨迹字典中提取数据质量报告
        feature_meta = track.get("feature_metadata", {})  # 从轨迹字典中提取特征元数据
        quality_summary = self._summarize_quality(quality_report)  # 数据质量摘要（注入模板，供判定参考数据可信度）

        # 事件摘要
        event_summary = self._summarize_events(events)  # 生成事件摘要字符串
        rule_hits = self._summarize_rules(annotations)  # 生成规则命中摘要字符串

        # 轨迹数据（截断防止超长）
        if len(track_points) > max_points:  # 轨迹点超过限制
            step = len(track_points) // max_points  # 计算抽样步长（等间隔采样）
            track_points = track_points[::step][:max_points]  # 等间隔采样并截断到最大点数
        track_data = json.dumps(track_points, ensure_ascii=False, default=str)  # 将轨迹点序列化为JSON字符串

        # 标记明细
        annotations_text = json.dumps(annotations, ensure_ascii=False,  # 将标记数据序列化为JSON字符串
                                       indent=2, default=str)

        # 相似案例
        similar = routing.get("pgvector_similar", []) + routing.get("milvus_similar", [])  # 合并PGVector和Milvus的相似案例列表
        similar_count = len(similar)  # 相似案例数量
        similar_text = json.dumps(similar, ensure_ascii=False, indent=2, default=str)[:2000]  # 序列化相似案例（截断前2000字符）

        # 平均置信度
        confidences = [a.get("confidence", 0) for a in annotations]  # 提取所有标记的置信度列表
        avg_conf = sum(confidences) / len(confidences) if confidences else 0  # 计算平均置信度

        # 时间范围
        start_time = feature_meta.get("total_duration_minutes", "?")  # 轨迹起始时间（或总时长）
        point_count = len(track.get("track", []))  # 轨迹点总数

        has_annotations = "是" if annotations else "否"  # 是否有标记的文本表示

        # 法规上下文（从 track 中提取，由 orchestrator 注入）
        regulation_context = track.get("_regulation_context", "")  # 法规上下文文本

        return template.format(
            event_summary=event_summary,  # 事件摘要内容
            annotation_confidence=f"{avg_conf:.2%}",  # 平均置信度（百分比格式）
            rule_hits=rule_hits,  # 规则命中摘要
            regulation_context=regulation_context,  # 法规上下文
            track_data=track_data[:8000],  # 轨迹JSON数据（截断到8000字符）
            annotations=annotations_text[:3000],  # 标记明细（截断到3000字符）
            similar_cases=similar_text,  # 相似案例文本
            similar_count=similar_count,  # 相似案例数量
            start_time=start_time,  # 开始时间
            end_time=start_time,  # 结束时间（实际应取最后一个点的时间）
            point_count=point_count,  # 轨迹点总数
            has_annotations=has_annotations,  # 是否有标记
            quality_summary=quality_summary,  # 数据质量摘要
        )

    def _summarize_events(self, events: list[dict]) -> str:
        """生成事件摘要字符串"""
        if not events:  # 无事件
            return "未检测到候选事件"  # 返回默认信息
        summaries = []  # 事件摘要列表（初始化为空列表）
        for ev in events[:3]:  # 遍历前3个事件
            etype = ev.get("candidate_type", ev.get("type", "unknown"))  # 事件类型
            zone = ev.get("zone_name") or ev.get("zone_id", "未知区域")  # 事件所在区域
            priority = ev.get("priority", "middle")  # 事件优先级
            summaries.append(f"[{priority}] {etype} @ {zone}")  # 添加单条摘要
        if len(events) > 3:  # 事件数超过3个
            summaries.append(f"... 还有 {len(events) - 3} 个事件")  # 超出3个的事件数量汇总
        return "; ".join(summaries)  # 用分号连接所有摘要

    def _summarize_rules(self, annotations: list[dict]) -> str:
        """生成规则命中摘要字符串"""
        rules = [a for a in annotations if a.get("type") == "rule_hit"]  # 提取所有规则命中类型的标记
        if not rules:  # 无命中规则
            return "无命中规则"  # 返回默认信息
        return "; ".join(  # 用分号连接前5条规则命中的标签
            f"{r.get('label', r.get('violation_code', '?'))}"  # 获取规则标签或违规代码
            for r in rules[:5]  # 遍历前5条规则
        )

    def _call_llm(self, prompt: str) -> dict:
        """调用 LLM 并解析 JSON 结果。

        LLM 不可用时自动尝试重连，仍失败则返回安全默认值（不抛异常）。
        """
        self._ensure_llm()  # 配置变更检测：不一致则重建客户端（开发者模式即时生效）
        # 尝试自动恢复：Ollama 可能仍在启动早期（HTTP 500/拒连），
        # 用带重试的初始化来覆盖启动窗口，避免启动期一次失败被永久钉死为不可用。
        max_retry = 5  # 最多重试次数
        for attempt in range(max_retry):
            if self.llm is None or not self._model_available:
                self._init_llm()  # 尝试重新连接 Ollama（内含 HTTP 探测）
            if self.llm is not None and self._model_available:
                break  # 已就绪，跳出
            if attempt < max_retry - 1:  # 非最后一次才等待（Ollama 秒级启动）
                time.sleep(2)  # 起步稳定期稍短，后续增长
        if self.llm is None or not self._model_available:
            return {  # LLM 不可用，返回安全默认值（mid+unverified，避免伪装成"低风险正常"）
                "violation_detected": False,
                "violation_type": None,
                "violation_label": "LLM不可用",
                "confidence": 0.0,
                "risk_level": "mid",
                "risk_unverified": True,
                "agrees_with_triage": None,
                "disagreement_reason": "LLM模型不可用，无法完成分析",
                "final_assessment": "LLM模型不可用，跳过判定",
                "recommended_action": "manual_review",
                "feedback_for_ml": {},
                "error": "llm_unavailable",
            }

        try:
            from langchain_core.messages import HumanMessage  # 人类消息类
            response = self.llm.invoke([  # 调用LLM获取响应
                HumanMessage(content=prompt)  # 以人类消息形式传入Prompt
            ])
            text = response.content  # 提取LLM返回的文本内容
            return self._parse_json(text)  # 解析并返回JSON结果
        except Exception as e:  # 异常对象
            return {  # 返回安全默认值（mid+unverified，避免伪装成"低风险正常"）
                "violation_detected": False,  # 未检测到违规
                "violation_type": None,  # 无违规类型
                "violation_label": "分析异常",  # 标记为分析异常
                "confidence": 0.0,  # 置信度为0
                "risk_level": "mid",  # 风险等级为中（故障兜底，不可信）
                "risk_unverified": True,
                "agrees_with_triage": None,  # 无法判断一致性
                "disagreement_reason": f"LLM调用异常: {str(e)}",  # 异常原因描述
                "final_assessment": "因模型调用异常，无法完成分析",  # 最终评估说明
                "recommended_action": "manual_review",  # 建议人工复核
                "feedback_for_ml": {},  # 空反馈字典
                "error": str(e),  # 原始异常信息
            }

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从 LLM 回复中提取 JSON。"""
        if not text:  # 空文本
            return {}  # 返回空字典
        text = text.strip()  # 去除首尾空白
        # 去掉 markdown 包裹
        if text.startswith("```"):  # Markdown代码块标记
            start = text.find("{")  # JSON对象起始位置
            end = text.rfind("}")  # JSON对象结束位置
            if start != -1 and end != -1:  # 找到JSON花括号
                text = text[start:end+1]  # 提取纯JSON部分
        try:
            return json.loads(text)  # 解析JSON字符串为字典
        except json.JSONDecodeError:  # JSON解析失败
            return {  # 返回错误信息字典
                "violation_detected": False,  # 未检测到违规
                "final_assessment": "LLM 回复解析失败",  # 最终评估说明
                "error": f"JSON解析失败: {text[:200]}",  # 错误信息（含前200字符原文）
            }

