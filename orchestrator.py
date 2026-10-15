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

            # Step 2: 判定（含记忆注入）
            # 门控：`RoutingDecision.needs_llm` 由 L2 的风险分与 RISK_GATE_THRESHOLD
            # 决定（router.py 第 2b 步），但**全仓原先没有任何调用方读它** ——
            # 于是每船必调 LLM、报警率 93%、最终判定 lift 0.88（比乱抓还差）。
            # 这里接上：不放行的船判定交给 L1 实测证据，不花 LLM 预算。
            # getattr 默认 True：拿到的不是 router 产物时保持"照旧调 LLM"，不擅自省。
            if self.llm_handler and routing_decision:  # 如果有LLM处理模块且已有路由决策
                if getattr(routing_decision, "needs_llm", True):  # 门控放行 → 调 LLM
                    llm_result = self._llm_with_memory(  # 调用带记忆的LLM分析
                        track=track,
                        entry=entry,
                        routing_decision=routing_decision,
                    )
                else:  # 门控不放行 → 判定只由 L1 硬证据产生
                    llm_result = self._verdict_without_llm(
                        track=track,
                        entry=entry,
                        routing_decision=routing_decision,
                    )
                entry["llm_result"] = llm_result  # 存入判定结果

                # Step 3: 反馈回流
                # 只回流真正被 LLM 判过的：feedback 收的是**弱标签**，
                # 把 L1 证据当 LLM 结论喂进去等于给训练集贴错来源的标签。
                if (self.feedback_loop and entry.get("case_id")
                        and not llm_result.get("llm_skipped")):
                    feedback = self.feedback_loop.process_result(  # 处理反馈回流
                        case_id=entry["case_id"],  # 案件ID
                        ml_prediction=routing_decision.ml_category,  # ML预测分类
                        ml_confidence=routing_decision.ml_confidence,  # ML预测置信度
                        llm_result=llm_result,  # LLM判定结果
                        # 文件模式（无 pgvector）下 feedback.py 是
                        # `if ... and feature_vector:` 才入库 —— 不传这个参数，
                        # 每条记录都被**静默丢弃**，回路看起来在工作其实一条没进。
                        feature_vector=track.feature_vector,  # 轨迹特征向量
                    )
                    entry["feedback"] = feedback  # 存入反馈信息

                # 更新 pgvector 中的判定结果
                # 刻意放在 feedback 那道门**之外**：门控跳过的船判定由 L1 立，
                # 也该落库，否则案例库里这些船没有结论（原先它嵌在 feedback 分支里，
                # 只有 feedback_loop 存在时才写）。
                if self.pgvector and entry.get("case_id"):
                    try:
                        self.pgvector.update_llm_result(  # 更新判定结果到数据库
                            case_id=entry["case_id"],  # 案件ID
                            llm_result=llm_result,  # 判定结果
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
            # 白名单取自 violation_catalog.CANONICAL，而不是在这里手写类型元组：
            # 手写的那版（speed_violation/low_speed_fishing/…）不会崩，但每加一个
            # 新违规码就得记得回来补一笔，忘了就静默漏掉。CANONICAL 的键就是
            # 全部事件码，加码时它自动跟上。
            from ais_accelerator.violation_catalog import CANONICAL  # 全部规范违规码

            event_types = set(CANONICAL)  # 事件类标注的 type 取值集合
            for ann in track.annotations:
                if ann.type in event_types:
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

        # ── 判定收尾：L1 确定性提升 + 规范码/法规物化 ──
        # 抽成 _finalize_verdict 是为了让"门控不放行、不调 LLM"的那条路
        # （_verdict_without_llm）走**同一套**判定语义，否则两条路会各自演化。
        self._finalize_verdict(track, llm_result, zone, scenario, routing_decision)

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

    # ── 判定收尾（两条路共用） ──

    def _finalize_verdict(
        self,
        track: AnnotatedTrack,
        llm_result: dict,
        zone: str,
        scenario: str,
        routing_decision: Any = None,
    ) -> dict:
        """在原始判定之上施加三条来源的提升，并把规范码/法规引用物化。

        原地修改并返回 llm_result。两条路都走这里：
          · `_llm_with_memory`      —— LLM 已给出判定
          · `_verdict_without_llm`  —— 门控不放行，未调 LLM
        口径必须完全一致，否则同一艘船"调不调 LLM"会得到两套判定语义。

        提升顺序即优先级（前者已定性则后者不再改写结语）：
          1. L1 硬证据（实测事件的规范码，证据最具体）
          2. L2 风险门控（模型风险分越阈值，可复核的嫌疑）
        """
        self._apply_l1_promotion(track, llm_result)
        self._apply_l2_gate_promotion(track, llm_result, routing_decision)
        self._materialize_violation(track, llm_result, zone, scenario)
        return llm_result

    @staticmethod
    def _apply_l2_gate_promotion(
        track: AnnotatedTrack,
        llm_result: dict,
        routing_decision: Any,
    ) -> None:
        """L2 风险门控提升：风险分越过门控阈值的船，判定即为**疑似违规**。

        【为什么要有这一条】
          实测：现役最终判定（LLM 全送 + 提升清单）在宽区间真值上 lift 0.88 ——
          比乱抓还差。把同一批船改成"只按 L2 风险分卡阈值"，召回反而更高。
          在 l3_hawaii 的 136 船-日上配对比较：门控放行的 17 条里，LLM **否决**了
          1 条真阳（宽区间口径）——也就是说 LLM 的"无违规"会把 L2 高风险船撤回来，
          而 L2 风险分本身才是更准的那条判据（全量留一天：@10% 报警率
          lift 3.22 / 精确区间 2.72，见 data/logs/diag_final_rule.py）。

          所以这里把"风险分越阈值"直接立成判定来源之一：**LLM 说无违规也不能撤回**。
          LLM 依然会跑（门控放行的船），但它在这一层的作用是提供分类、说明与法条，
          不再是"能不能报"的开关。

        【为什么不是无条件相信 LLM】
          LLM 只由 `violation_detected` 一个布尔量决定报不报，而它在实测里
          （a）自己捞不到真阳（discipline 臂 0/30）、（b）还会撤掉真阳。
          让它拥有否决权 = 让最不可靠的一层决定最终输出。
        """
        if routing_decision is None:
            return
        try:
            from triage_engine.router import RISK_GATE_THRESHOLD
            risk = float(getattr(routing_decision, "risk_score", 0.0) or 0.0)
            # 风险分无论立不立判定都记下来：事后要能回答"这条为什么报/为什么没报"
            llm_result.setdefault("gate_risk_score", risk)
            llm_result.setdefault("gate_threshold", float(RISK_GATE_THRESHOLD))
            if llm_result.get("violation_detected") or risk < RISK_GATE_THRESHOLD:
                return
            llm_result["violation_detected"] = True
            llm_result["detection_source"] = "l2_risk_gate"  # 可追溯：这条是 L2 立的
            # 与 L1 提升同一口径：硬证据/模型都指向违规时不该出现"违规但低风险"的行。
            # 注意 risk_engine 随后会用 violation_code 的基础分覆盖 risk_level，
            # 所以 run.py 那边对这一来源补了 extra_bonus（见那里的注释）。
            if (llm_result.get("risk_level") or "").lower() not in {"mid", "medium", "high"}:
                llm_result["risk_level"] = "mid"
            _note = (f"[风险门控] L2 风险分 {risk:.4f} ≥ 门控阈值 {RISK_GATE_THRESHOLD:.6f}"
                     f"（约 10% 的船会越线），判定为疑似违规；"
                     + ("未经 LLM 复核。" if llm_result.get("llm_skipped")
                        else "LLM 复核未撤回该判定。"))
            _prev = (llm_result.get("final_assessment") or "").strip()
            llm_result["final_assessment"] = (f"{_note} {_prev}" if _prev else _note)[:500]
            # violation_type 同样留空：它代表"LLM 的原始判性"，
            # 规范码由 _materialize_violation 从 L1 事件直接定。
        except Exception:
            pass  # 提升失败不影响主流程

    @staticmethod
    def _apply_l1_promotion(track: AnnotatedTrack, llm_result: dict) -> None:
        """L1 确定性提升：判定层说"没问题"时，硬证据仍能立住违规。

        这一步必须在 `_materialize_violation` 的 `if violation_detected:` **之前**，
        因为那道门后面挂着规范码/三级细类/法规引用的全部物化，而 violation_detected
        本身只由 LLM 置位。不提升的话，L1 实测到的追越/逆向/碰撞永远到不了界面：
        实测 617 行只有 42 行带违规码且 100% 是 ais_gap，就是这个门槛造成的。
        """
        if llm_result.get("violation_detected"):
            return
        try:
            from ais_accelerator.violation_catalog import deterministic_violation
            _hard = deterministic_violation(track.events if track is not None else None)
            if _hard:
                llm_result["violation_detected"] = True
                llm_result["detection_source"] = "l1_deterministic"  # 可追溯：这条是 L1 立的
                # 风险等级：没给或给得过低时抬到 mid——硬证据成立却没风险等级，
                # 界面上会出现"有违规但风险低"的自相矛盾行。
                # 必须是 "mid" 不是 "middle"：risk_level 这一列全链路用的是
                # llm_layer/handler.py 与 risk_engine.py 的 {high, mid, low} 词表，
                # 而 "middle" 是**事件优先级**（models.py EventPriority）的词表，
                # 两者只是恰好同名。写错会让仪表盘风险饼图多出一段假分段——
                # 实测 41 条违规里 middle=7 / mid=2 同时出现，就是这么来的。
                if (llm_result.get("risk_level") or "").lower() not in {"mid", "medium", "high"}:
                    llm_result["risk_level"] = "mid"
                _note = (  # 在结语里说明这是确定性检测立的，不是 LLM 判断
                    f"[确定性检测] L1 实测命中「{_hard['code']}」"
                    f"（{_hard['count']} 项硬证据"
                    + (f"，{_hard['feature']}={_hard['value']:.2f}" if _hard["feature"] else "")
                    + "），未经 LLM 复核。"
                )
                _prev = (llm_result.get("final_assessment") or "").strip()
                llm_result["final_assessment"] = (f"{_note} {_prev}" if _prev else _note)[:500]
                # violation_type 留空：它代表"LLM 的原始判性"，硬证据提升不改变这一点，
                # 规范码由下面的 classify() 从 L1 事件直接定，不经过这一列。
        except Exception:
            pass  # 提升失败不影响主流程（退回原有判定口径）

    def _materialize_violation(
        self,
        track: AnnotatedTrack,
        llm_result: dict,
        zone: str,
        scenario: str,
    ) -> None:
        """关联具体法规条款（用于违规监控展示）：规范码/中文标签/三级细类/法条引用。

        规范码/中文标签/三级细类的物化**不依赖 regulation_db**（那是纯目录运算），
        只有"检索具体法条"才需要。原先整段都挡在 `self.regulation_db` 后面，
        于是 Milvus 不可用时违规记录会带着空的 violation_code 落库、界面退回
        按 LLM 脏码显示——新类型就这么"加了却看不见"。现在只有检索受它影响。
        """
        if not llm_result.get("violation_detected"):
            return
        try:
            from ais_accelerator.violation_catalog import (
                classify, FALLBACK_REG, build_violation_event, format_article_ref,
            )
            # L1 事件是**实测证据**，classify() 以它的 candidate_type 为准
            # （优先于 LLM 自由输出的 violation_type —— 后者是 AIS01/AIS012/
            #  data_quality_non_increasing_time 这类杂乱值，实测同一批数据里
            #  42 条违规的 L1 事件 100% 是 ais_gap，而 LLM 输出了十余种写法）。
            # 三级细类由 features 里的实测量级定档（如 ais_gap#suspected_off）。
            _events = []
            for _ev in (track.events or []):
                _ct = (_ev.candidate_type.value
                       if hasattr(_ev.candidate_type, "value")
                       else str(_ev.candidate_type))
                _events.append({
                    "candidate_type": _ct,
                    "features": dict(getattr(_ev, "features", None) or {}),
                })
            # 无 L1 事件时退回 scenario（加速器标注出的类型），仍优于纯 LLM 输出
            _vtype = (llm_result.get("violation_type", "") or scenario or "").strip()
            _info = classify(_vtype, _events)
            _code = _info["code"]
            llm_result["violation_code"] = _code
            llm_result["violation_label"] = _info["label"]
            llm_result["violation_subtype"] = _info["subtype"]
            llm_result["violation_metric"] = _info["metric"] or 0

            # 用**规范码**检索（不是 LLM 的脏码）：检索词取该码的中文含义
            _arts = []
            if self.regulation_db:  # 只有检索这一步需要法规库
                try:
                    _arts = self.regulation_db.search_by_violation(_code, zone, top_k=3)
                except Exception:
                    _arts = []  # 检索失败退回下面的目录兜底引用
            _best = _arts[0] if _arts else None
            if _arts:
                # 引用文案统一由 violation_catalog.format_article_ref 生成
                # （backfill_violation_taxonomy 回填存量时用的是同一个函数，
                #  否则新写入与存量回填的文案会不一致）
                _refs = [format_article_ref(_a) for _a in _arts]
                llm_result["regulation_ref"] = "；".join(_refs[:3])
            else:
                # 检索为空（含"相似度低于门槛被丢弃"）→ 目录兜底引用
                # 注：兜底文本是"该违规适用什么规则"的通用陈述，不是伪造的法条号；
                # 回填脚本的 --fix-regulation 更保守（宁可不写），两者口径不同是有意的。
                llm_result["regulation_ref"] = FALLBACK_REG.get(_code, FALLBACK_REG.get("unknown", ""))
            llm_result["violation_event"] = build_violation_event(
                _code,
                llm_result.get("final_assessment", "") or "",
                llm_result.get("regulation_ref", "") or "",
                _best,
            )
        except Exception:
            # 分类失败不影响主流程
            llm_result["regulation_ref"] = llm_result.get("regulation_ref", "")

    def _verdict_without_llm(
        self,
        track: AnnotatedTrack,
        entry: dict | None = None,
        routing_decision: Any = None,
    ) -> dict:
        """门控不放行时的判定：**不调 LLM**，判定完全由 L1 实测证据产生。

        为什么要有这条路：`triage_engine.router` 早就把 `decision.needs_llm` 算好了
        （RISK_GATE_THRESHOLD），但全仓没有任何调用方读它——于是每船必调 LLM，
        实测报警率 93%、最终判定 lift 0.88（比乱抓还差），而门控本来就是为了把
        LLM 预算集中在 L2 风险分最高的那一小撮船上。

        与 `_llm_with_memory` 的差异只有来源标注（detection_source / llm_skipped），
        返回结构保持一致，因为 run.py 下游只按字段名取值、不区分来源。

        注意：这里**不**写船舶/会话记忆。记忆写的是"LLM 判定过的结论"，而这条路上
        压根没有 LLM 判定；把 L1 证据当 LLM 结论写进记忆会污染小慧的历史上下文。
        """
        import time as _time
        _t0 = _time.perf_counter()
        zone, scenario = self._extract_scenario(track)

        # 起点 = "判定层未发现违规"。与 handler.analyze 的 normal 口径对齐：
        # risk_level 交给随后的 classify/risk_engine 覆盖，此处给最低档即可。
        llm_result: dict = {
            "violation_detected": False,
            "violation_type": "",
            "risk_level": "low",
            "confidence": 0.0,
            "recommended_action": "monitor",
            "final_assessment": "",
            "agrees_with_triage": None,
            "llm_skipped": True,          # 可追溯：本条的判定未经 LLM
            "detection_source": "l1_only",
            "latency_seconds": 0.0,
            "prompt_template": "gated_skip",  # 落库后能一眼看出不是 LLM 判的
            "feedback_for_ml": {},
        }
        if routing_decision is not None:
            try:
                llm_result["routing"] = routing_decision.to_dict()
            except Exception:
                pass
        # 数据质量标注沿用 handler 的口径（同一个 _quality_level_of），
        # 否则"调没调 LLM"会让同一份数据的质量徽章变样。
        try:
            from llm_layer.handler import _quality_level_of
            _qr = ((entry or {}).get("quality_report")
                   or (getattr(track, "quality_report", None) or {}))
            if hasattr(_qr, "model_dump"):
                _qr = _qr.model_dump(mode="json")
            _ql = _quality_level_of(_qr if isinstance(_qr, dict) else {})
            llm_result["data_quality_level"] = _ql
            llm_result["data_quality_suspect"] = bool(
                _ql != "good" or int((_qr or {}).get("invalid_points", 0) or 0) > 0)
        except Exception:
            pass

        self._finalize_verdict(track, llm_result, zone, scenario, routing_decision)
        llm_result["latency_seconds"] = round(_time.perf_counter() - _t0, 3)
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
