"""
orchestrator.py — 三级级联系统顶层编排器
===========================================

【这个文件做什么？】
  作为三级级联系统的"总指挥"，协调：
  L1. ais_accelerator — 质量清洗/事件检测/特征提取
  L2. TriageRouter  — ML 分类/置信度路由/向量检索
  L3. LLMHandler    — 分级 Prompt/LLM 判定/反馈提取

【三种运行模式】
  1. run_pipeline_only(): 仅加速器标注（不调 ML / LLM）
     → 适用：快速数据预览、加速器效果调试
  2. run_with_ml(): 加速器 + ML 路由（不调 LLM）
     → 适用：大规模初筛，仅看 ML 分类结果
  3. run_full(): 全链路（加速器 → ML → LLM → 反馈）
     → 适用：正式分析，最完整的处理链路

【设计模式 —— 策略模式】
  每一层都是可选的（Optional），调用方可以根据需要
  组合不同的处理深度，而不需要修改编排器的代码。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from ais_accelerator import AISAccelerationFacade
from ais_accelerator.models import AnnotatedTrack
from memory import SessionMemory, ShipMemory


class EndToEndOrchestrator:
    """三层级联系统主编排器。

    不需要数据库/ML 模型/LLM 时，每一层均可独立使用。
    例如：
        orchestrator.run_pipeline_only("data.csv")  # 只跑加速器
        orchestrator.run_with_ml("data.csv")         # 加速器 + ML
        orchestrator.run_full("data.csv")            # 全链路

    新增记忆系统（2026-08）：
        - SessionMemory:  单次运行中的短期记忆，跨船传播新发现
        - ShipMemory:     船舶履历持久化，支持惯犯识别
        - PatternMemory:  场景模式持久化，稳定模式可跳过 LLM
    """

    def __init__(
        self,
        accelerator: Optional[AISAccelerationFacade] = None,  # L1加速器外观，None时自动创建默认实例
        triage_router: Optional["TriageRouter"] = None,  # L2 ML路由模块，可选
        llm_handler: Optional["LLMHandler"] = None,  # L3 LLM处理模块，可选
        feedback_loop: Optional["FeedbackLoop"] = None,  # 反馈闭环模块，可选
        pgvector_client: Optional["PGVectorClient"] = None,  # PGVector向量数据库客户端，可选
        milvus_client: Optional["MilvusClient"] = None,  # Milvus向量数据库客户端，可选
        session_memory: Optional[SessionMemory] = None,  # 会话记忆（可选）
        ship_memory: Optional[ShipMemory] = None,  # 船舶记忆（可选）
        regulation_db: Optional[Any] = None,  # 法规向量数据库（可选）
    ):
        self.accelerator = accelerator or AISAccelerationFacade()  # L1加速器实例
        self.triage_router = triage_router  # L2 ML路由实例
        self.llm_handler = llm_handler  # L3 LLM处理实例
        self.feedback_loop = feedback_loop  # 反馈闭环实例
        self.pgvector = pgvector_client  # PGVector数据库客户端实例
        self.milvus = milvus_client  # Milvus数据库客户端实例
        self.session_memory = session_memory  # 会话记忆实例
        self.ship_memory = ship_memory  # 船舶记忆实例
        self.regulation_db = regulation_db  # 法规向量数据库实例

    # ── 可选组件注册（延迟初始化） ──

    def set_triage_router(self, router) -> None:  # router: TriageRouter实例
        self.triage_router = router  # 设置L2 ML路由模块

    def set_llm_handler(self, handler) -> None:  # handler: LLMHandler实例
        self.llm_handler = handler  # 设置L3 LLM处理模块

    def set_feedback_loop(self, loop) -> None:  # loop: FeedbackLoop实例
        self.feedback_loop = loop  # 设置反馈闭环模块

    # ═══════════════════════════════════════════════════════════
    # 不同处理深度的入口
    # ═══════════════════════════════════════════════════════════

    def run_pipeline_only(
        self, source_path: str, max_ships: Optional[int] = None  # source_path: 输入CSV路径; max_ships: 最大处理船舶数（可选）
    ) -> tuple[list[AnnotatedTrack], dict]:
        """仅跑加速器标注模式（不调 ML / LLM）。"""
        results, metrics = self.accelerator.annotate_file(source_path, max_ships)  # results: 标注结果列表; metrics: 处理指标
        return results, metrics.summary()  # 返回标注结果和汇总指标

    def run_with_ml(
        self,
        source_path: str,  # 输入CSV文件路径
        max_ships: Optional[int] = None,  # 最大处理的船舶数量，None表示全部
    ) -> list[dict]:
        """加速器 + ML 路由（不调 LLM，只看 ML 路由结果）。"""
        tracks, metrics = self.accelerator.annotate_file(source_path, max_ships)  # tracks: 标注后的船舶轨迹列表; metrics: 处理指标
        outputs = []  # 输出结果列表
        for track in tracks:  # 遍历每一条标注轨迹
            entry = track.to_agent_input()  # 将轨迹转换为Agent可用的输入格式
            if self.triage_router:  # 如果有ML路由模块
                decision = self.triage_router.route(  # ML路由分类决策
                    feature_vector=track.feature_vector,  # 轨迹特征向量
                    track_points=[p.model_dump(mode="json") for p in track.track],  # 轨迹点JSON格式列表
                    mmsi=track.mmsi,  # 船舶MMSI号
                )
                entry["routing"] = decision.to_dict()  # 路由决策转为字典存入结果
            entry["pipeline_metrics"] = metrics  # 流水线指标
            outputs.append(entry)  # 加入输出列表
        return outputs  # 返回所有处理结果

    def run_full(
        self,
        source_path: str,  # 输入CSV文件路径
        max_ships: Optional[int] = None,  # 最大处理船舶数，None表示全部
        save_results: Optional[str] = None,  # 结果保存路径，None表示不保存
    ) -> list[dict]:
        """全链路：加速器 → ML 路由 → LLM 判定 → 反馈。"""
        total_start = time.perf_counter()  # 全链路起始时间
        tracks, metrics = self.accelerator.annotate_file(source_path, max_ships)  # tracks: 标注轨迹列表; metrics: 处理指标
        outputs = []  # 输出结果列表

        for track in tracks:  # 遍历每条标注轨迹
            entry = track.to_agent_input()  # 转换为Agent输入格式字典
            entry["mmsi"] = track.mmsi  # 船舶MMSI号

            # Step 1: ML 路由
            routing_decision = None  # ML路由决策，初始化为None
            if self.triage_router:  # 如果有ML路由模块
                decision = self.triage_router.route(  # 执行ML路由分类
                    feature_vector=track.feature_vector,  # 轨迹特征向量
                    track_points=[p.model_dump(mode="json") for p in track.track],  # 轨迹点JSON序列化
                    mmsi=track.mmsi,  # 船舶MMSI号
                )
                entry["routing"] = decision.to_dict()  # 路由决策转为字典
                routing_decision = decision  # 保存路由决策供后续使用

                # 存入 pgvector
                if self.pgvector:  # 如果有PGVector客户端
                    case_id = self.pgvector.insert_case(  # 插入案件记录，返回案件ID
                        mmsi=track.mmsi,  # 船舶MMSI号
                        feature_vector=track.feature_vector,  # 特征向量
                        feature_json=track.feature_metadata,  # 特征元数据JSON
                        annotations=[a.model_dump(mode="json")
                                     for a in track.annotations],  # 标注数据JSON列表
                        annotation_counts=track.annotation_counts,  # 各类标注计数
                        source_file=source_path,  # 源文件路径
                    )
                    entry["case_id"] = case_id  # 案件ID
                    self.pgvector.update_ml_result(  # 更新ML路由结果
                        case_id, decision.ml_category, decision.ml_confidence  # 案件ID、ML分类、ML置信度
                    )

                    # 存入 Milvus
                    if self.milvus and track.track:  # 如果有Milvus客户端且轨迹数据存在
                        try:
                            track_points = [  # 轨迹点JSON格式列表
                                p.model_dump(mode="json") for p in track.track
                            ]
                            llm_info = {}  # LLM结果占位（此时LLM还未判定）
                            self.milvus.insert_track(  # 插入轨迹到Milvus
                                id=case_id,  # 案件ID作为轨迹ID
                                mmsi=track.mmsi,  # 船舶MMSI号
                                track_points=track_points,  # 轨迹点数据
                                segment_id=track.track[0].timestamp.isoformat()
                                    if track.track else None,  # 航段ID（使用首条记录的时间戳）
                                metadata={  # 元数据：特征信息和路由决策
                                    "feature_metadata": track.feature_metadata,  # 特征元数据
                                    "routing": decision.to_dict(),  # 路由决策
                                },
                            )
                        except Exception as e:  # Milvus插入失败
                            entry["milvus_error"] = str(e)  # 记录错误信息

            # Step 2: LLM 判定（含记忆注入）
            if self.llm_handler and routing_decision:  # 如果有LLM处理模块且已有路由决策
                llm_result = self._llm_with_memory(  # 调用带记忆的LLM分析
                    track=track,
                    entry=entry,
                    routing_decision=routing_decision,
                )
                entry["llm_result"] = llm_result  # 存入LLM分析结果

                # Step 3: 反馈回流
                if self.feedback_loop and entry.get("case_id"):  # 有反馈闭环且有案件ID
                    feedback = self.feedback_loop.process_result(  # 处理反馈回流
                        case_id=entry["case_id"],  # 案件ID
                        ml_prediction=routing_decision.ml_category,  # ML预测分类
                        ml_confidence=routing_decision.ml_confidence,  # ML预测置信度
                        llm_result=llm_result,  # LLM判定结果
                    )
                    entry["feedback"] = feedback  # 存入反馈信息

                    # 更新 pgvector 中 LLM 结果
                    if self.pgvector:  # 如果有PGVector客户端
                        try:
                            self.pgvector.update_llm_result(  # 更新LLM判定结果到数据库
                                case_id=entry["case_id"],  # 案件ID
                                llm_result=llm_result,  # LLM分析结果
                                has_violation=llm_result.get("violation_detected"),  # 是否检测到违规
                                violation_type=llm_result.get("violation_type"),  # 违规类型
                            )
                        except Exception:  # 数据库更新失败时静默处理
                            pass

            entry["pipeline_metrics"] = metrics.summary()  # 流水线汇总指标
            outputs.append(entry)  # 加入输出列表

        # 总耗时
        total_elapsed = time.perf_counter() - total_start  # 全链路处理总耗时（秒）

        # 反馈闭环：全部处理完后检查增量训练
        if self.feedback_loop and self.feedback_loop.auto_retrain:  # 如果有反馈闭环且启用了自动重训练
            try:
                self.feedback_loop.maybe_retrain()  # 检查是否需要触发增量训练
            except Exception as e:  # 重训练失败时打印警告
                print(f"  [反馈] 自动训练检查失败: {e}")

        # 汇总
        summary = {  # 处理汇总信息
            "total_ships": len(tracks),  # 处理船舶总数
            "total_time_seconds": round(total_elapsed, 2),  # 总耗时（秒，保留2位小数）
            "avg_time_per_ship": round(total_elapsed / max(len(tracks), 1), 3),  # 平均每艘船处理时间
            "pipeline_metrics": metrics.summary(),  # 流水线指标摘要
            "routing_counts": self._count_routings(outputs),  # 各路由分类的计数
            "violation_counts": self._count_violations(outputs),  # 违规检测计数
        }
        output = {  # 最终输出结构
            "summary": summary,  # 汇总信息
            "results": outputs,  # 详细结果列表
        }

        # 保存
        if save_results:  # 如果指定了保存路径
            path = Path(save_results)  # 结果文件路径
            path.parent.mkdir(parents=True, exist_ok=True)  # 创建上级目录
            path.write_text(  # 写入JSON文件
                json.dumps(output, ensure_ascii=False, indent=2, default=str),  # JSON序列化（支持非ASCII字符和日期类型）
                encoding="utf-8",
            )
            print(f"[Save] 结果已保存: {path}")

        return outputs  # 返回处理结果列表

    # ═══════════════════════════════════════════════════════════
    # 智能体记忆系统
    # ═══════════════════════════════════════════════════════════

    def _extract_scenario(self, track: AnnotatedTrack) -> tuple[str, str]:
        """从加速器标注中提取区域和行为场景。"""
        zone = "unknown_zone"
        scenario = "unknown"

        # 从事件中提取区域和场景
        for ev in track.events:
            if ev.zone_id:
                zone = ev.zone_id
            if ev.zone_name:
                zone = ev.zone_name
            candidate_type = ev.candidate_type.value if hasattr(
                ev.candidate_type, "value"
            ) else str(ev.candidate_type)
            scenario = candidate_type
            break

        # 从标注中补充提取
        if not track.events:
            for ann in track.annotations:
                if ann.type in ("speed_violation", "low_speed_fishing",
                                "restricted_area_entry", "ais_gap",
                                "course_anomaly", "position_jump"):
                    scenario = ann.type
                if ann.detail.get("zone_name"):
                    zone = ann.detail["zone_name"]
                elif ann.detail.get("zone_id"):
                    zone = ann.detail["zone_id"]
                break

        return zone, scenario

    def _llm_with_memory(
        self,
        track: AnnotatedTrack,
        entry: dict,
        routing_decision: Any,
    ) -> dict:
        """带记忆支持的 LLM 分析。

        流程：
        1. 查船舶记忆 → 注入历史记录到 prompt
        2. 查会话记忆 → 注入本轮发现到 prompt
        3. 调 LLM
        4. 写入船舶记忆和会话记忆

        注意：PatternMemory（情景记忆）不再用于检测管道。
        记忆系统只用于小慧智能体（个性化对话），不干预检测管道的 LLM 判定。
        """
        mmsi = track.mmsi
        zone, scenario = self._extract_scenario(track)
        routing_dict = routing_decision.to_dict()
        memory_sources = []

        # ── 第 1 步: 查船舶记忆（ShipMemory）→ 注入历史 ──
        ship_history_text = ""
        if self.ship_memory:
            ship_history_text = self.ship_memory.get_history_text(mmsi)
            if ship_history_text:
                memory_sources.append("ship")

        # ── 第 2 步: 查会话记忆（SessionMemory）→ 注入本轮上下文 ──
        session_context = ""
        if self.session_memory:
            session_context = self.session_memory.get_prompt_context(
                mmsi=mmsi, zone=zone, scenario=scenario,
            )
            if session_context:
                memory_sources.append("session")

        # ── 第 3 步: 调 LLM（注入记忆上下文） ──
        # 在原始 entry 中注入记忆上下文
        augmented_entry = dict(entry)
        if ship_history_text:
            augmented_entry["_ship_history"] = ship_history_text
        if session_context:
            augmented_entry["_session_context"] = session_context

        # 注入法规上下文（从 RegulationDB 查询）
        if self.regulation_db:
            try:
                zone, scenario = self._extract_scenario(track)
                # 从事件中提取违规类型
                violation_type = ""
                for ev in track.events:
                    violation_type = ev.candidate_type.value
                    break
                if violation_type:
                    reg_context = self.regulation_db.format_regulation_context(
                        violation_type, zone
                    )
                    if reg_context:
                        augmented_entry["_regulation_context"] = reg_context
            except Exception:
                pass  # 法规查询失败不影响主流程

        llm_result = self.llm_handler.analyze(
            annotated_track=augmented_entry,
            routing=routing_dict,
        )

        # ── 关联具体法规条款（用于违规监控展示） ──
        # regulation_db 已注入法规上下文到 prompt；这里再为违规类型生成
        # 规范码/中文标签/法规引用/可读违规事件，供前端展示。
        if self.regulation_db and llm_result.get("violation_detected"):
            try:
                from ais_accelerator.violation_catalog import (
                    normalize_violation_code, describe, FALLBACK_REG, build_violation_event,
                )
                # LLM 自由输出的 violation_type 很乱（AIS01/AIS012/...），
                # 检索前先归一化为规范码；为空时退回 L1 事件类型（scenario）。
                _vtype = (llm_result.get("violation_type", "") or scenario or "").strip()
                _code = normalize_violation_code(_vtype)
                _info = describe(_code)
                llm_result["violation_code"] = _code
                llm_result["violation_label"] = _info["label"]

                _arts = self.regulation_db.search_by_violation(_vtype, zone, top_k=3)
                _best = _arts[0] if _arts else None
                if _arts:
                    _refs = []
                    for _a in _arts:
                        _name = _a.get("source", "")
                        _num = _a.get("article_number", "")
                        _title = (_a.get("title", "") or "").strip()
                        _sim = _a.get("similarity")
                        _ref = f"{_name}"
                        if _num and _num != "0":
                            _ref += f" 第{_num}条"
                        if _title:
                            _ref += f"《{_title}》"
                        if _sim is not None:
                            _ref += f"（相关度{_sim:.0%}）"
                        _refs.append(_ref)
                    llm_result["regulation_ref"] = "；".join(_refs[:3])
                else:
                    # 无检索结果 → 目录兜底引用（保证每条违规都有法规）
                    llm_result["regulation_ref"] = FALLBACK_REG.get(_code, FALLBACK_REG.get("unknown", ""))
                llm_result["violation_event"] = build_violation_event(
                    _vtype,
                    llm_result.get("final_assessment", "") or "",
                    llm_result.get("regulation_ref", "") or "",
                    _best,
                )
            except Exception as _re:
                # 检索失败不影响主流程
                llm_result["regulation_ref"] = llm_result.get("regulation_ref", "")

        # 记录使用了哪些记忆
        if memory_sources:
            llm_result["memory_sources"] = memory_sources

        # ── 第 4 步: 写入船舶记忆和会话记忆 ──
        # 写入船舶记忆
        if self.ship_memory:
            try:
                self.ship_memory.update_after_judgment(
                    mmsi=mmsi,
                    llm_result=llm_result,
                    zone=zone,
                    scenario=scenario,
                )
            except Exception as e:
                print(f"  [Memory] ship_memory 写入失败: {e}")

        # 写入会话记忆
        if self.session_memory:
            try:
                self.session_memory.record_llm_result(
                    mmsi=mmsi,
                    zone=zone,
                    scenario=scenario,
                    llm_result=llm_result,
                )
            except Exception as e:
                print(f"  [Memory] session_memory 写入失败: {e}")

        return llm_result

    # ── 统计 ──

    @staticmethod
    def _count_routings(outputs: list[dict]) -> dict:  # outputs: 处理结果列表
        counts: dict[str, int] = {}  # 各路由类型计数（如：high/medium/low/unknown）
        for out in outputs:  # 遍历每条处理结果
            routing = out.get("routing", {})  # 当前结果的routing字段
            r = routing.get("routing", "unknown")  # 获取实际路由结果，默认为unknown
            counts[r] = counts.get(r, 0) + 1  # 对应路由计数+1
        return counts  # 返回路由分类计数结果

    @staticmethod
    def _count_violations(outputs: list[dict]) -> dict:  # outputs: 处理结果列表
        violations = 0  # 违规次数计数
        total = 0  # LLM分析的总次数
        for out in outputs:  # 遍历每条处理结果
            llm = out.get("llm_result")  # 获取LLM分析结果
            if llm:  # 如果有LLM结果
                total += 1  # 已分析计数+1
                if llm.get("violation_detected"):  # 如果检测到违规
                    violations += 1  # 违规计数+1
        return {  # 返回违规统计信息
            "total_llm_analyzed": total,  # LLM分析总次数
            "violations_found": violations,  # 发现的违规次数
            "violation_rate": round(violations / max(total, 1), 4),  # 违规率（避免除以0）
        }
