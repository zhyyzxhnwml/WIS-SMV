"""
ais_accelerator/pipeline.py — 核心加速管线
============================================

【这个文件做什么？】
  编排 AIS 加速器的完整处理管线：
  ingest → quality → segment → detect → merge → compress → annotate

【两种输出模式】
  1. iter_file(): 逐个输出 CandidatePackage（用于旧 Agent 接口，带压缩）
  2. annotate_track() / annotate_file(): 输出 AnnotatedTrack（标注模式，不压缩不丢点）

【管线编排模式】
  使用"管线模式"（Pipeline Pattern）：
  每个步骤的输出是下个步骤的输入，数据单向流动，没有回环。
  这种模式的好处：
  - 每个步骤可独立测试
  - 可随时在中间插入/移除处理节点
  - 处理逻辑清晰，易于 Debug
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import json  # JSON序列化模块
import time  # 时间模块
from typing import Iterator, Optional  # 类型注解

import llm_config  # 全局模型配置：缓存 key 用当前模型名，换模型自动失效旧缓存

from .adapter import build_agent_input  # 适配器：构建Agent输入
from .annotator import build_annotated_track  # 标注器：构建标注轨迹
from .cache import build_cache_key  # 缓存：构建缓存键
from .collision import detect_collision_risks, collision_risks_to_events  # 碰撞风险检测
from .compress import compress_for_event  # 压缩器：事件级压缩
from .config import AcceleratorConfig  # 加速器配置
from .detect import detect_candidates  # 检测器：候选事件检测
from .ingest import SQLiteAISStore  # 数据导入：SQLite存储
from .merge import merge_events  # 合并器：事件合并
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    AnnotatedTrack,  # 标注后的完整轨迹
    CandidatePackage,  # 候选事件包
    PipelineMetrics,  # 管线运行指标
    ShipProfile,  # 船舶概要
    TrajectorySegment,  # 轨迹段
)
from .predict import TrajectoryPredictor  # 轨迹预测器
from .quality import prepare_track  # 质量：轨迹预处理
from .rules import JsonRuleProvider, RuleProvider  # 规则：JSON规则提供者, 规则提供者协议
from .segment import segment_track  # 分段：轨迹分段
from .water import WaterChecker  # 陆地/水域判定器（规避陆地上的 AIS 数据）


class AcceleratorPipeline:  # 加速器管线（编排所有处理步骤）
    def __init__(  # 初始化管线
        self,
        config: Optional[AcceleratorConfig] = None,  # 加速器配置（可选）
        rule_provider: Optional[RuleProvider] = None,  # 规则提供者（可选）
        model_version: str = "qwen2.5:7b-instruct-q4_K_M",  # LLM模型版本
    ):
        self.config = config or AcceleratorConfig()  # 配置对象
        self.config.ensure_directories()  # 确保缓存和输出目录存在
        self.rules = rule_provider or JsonRuleProvider(self.config.regulations_path)  # 规则提供者
        self.model_version = model_version  # 模型版本号
        self.metrics = PipelineMetrics()  # 管线运行指标
        self.store = SQLiteAISStore(self.config)  # SQLite数据存储

    def _build_water_checker(  # 从当前 store 全部原始轨迹点构建航道走廊（陆地判定器）
        self,
    ) -> Optional[WaterChecker]:
        """从 store 中所有船的原始轨迹点构建 WaterChecker。

        走廊 = 船队实际航行过的密度网格（density ≥ 2 + 缓冲），
        保证内河/近岸船不被误判为"在陆地"。
        """
        try:
            lon_lats: list[tuple[float, float]] = []  # 所有轨迹点经纬度
            for _ship, raw_points in self.store.iter_ship_tracks():  # 逐船遍历
                for p in raw_points:  # 遍历每个点
                    lon_lats.append((p.lon, p.lat))  # 收集经纬度
            if not lon_lats:  # 无数据
                return None  # 返回空
            return WaterChecker(lon_lats=lon_lats)  # 构建判定器
        except Exception:  # 陆地判定失败不影响主流程
            return None

    def iter_file(  # 遍历文件中的所有候选事件包（生成器模式）
        self, source_path: str, max_ships: Optional[int] = None  # CSV源文件路径, 最大船舶数（可选）
    ) -> Iterator[CandidatePackage]:
        preprocessing_started = time.perf_counter()  # 记录预处理开始时间
        ingest_stats = self.store.ingest(source_path, max_ships=max_ships)  # 导入CSV数据到缓存
        self.metrics = PipelineMetrics(  # 重置管线指标
            source_path=ingest_stats.source_path,  # 源文件路径
            raw_rows=ingest_stats.raw_rows,  # 原始行数
            valid_rows=ingest_stats.valid_rows,  # 有效行数
            invalid_rows=ingest_stats.invalid_rows,  # 无效行数
            ship_count=ingest_stats.ship_count,  # 船舶总数
            ingest_seconds=ingest_stats.elapsed_seconds,  # 导入耗时
        )
        water = self._build_water_checker()  # 陆地判定器（从本批次全部船构建走廊）
        for ship, raw_points in self.store.iter_ship_tracks():  # 逐船遍历
            yield from self.packages_for_track(ship, raw_points, water=water)  # 生成该船的所有候选包
        self.metrics.preprocessing_seconds = time.perf_counter() - preprocessing_started  # 记录预处理总耗时

    def packages_for_track(  # 处理单条轨迹，生成候选事件包列表（压缩模式）
        self, ship: ShipProfile, raw_points: list[AISPoint],  # 船舶概要, 原始轨迹点
        water: Optional[WaterChecker] = None,  # 陆地判定器（可选）
    ) -> list[CandidatePackage]:
        self.metrics.original_track_points += len(raw_points)  # 累加原始轨迹点数
        self.metrics.estimated_original_characters += len(  # 累加原始估计字符数
            json.dumps(  # 序列化为JSON
                [point.to_agent_position() for point in raw_points],  # 每个点转为Agent位置格式
                ensure_ascii=False,  # 保留非ASCII字符
                default=str,  # 默认序列化函数
            )
        )
        points, quality_report, normalizer = prepare_track(  # 轨迹质量预处理
            ship.mmsi, raw_points, self.config,  # MMSI, 原始点, 配置
            water=water,  # 陆地判定器
        )
        if not points:  # 如果没有有效点
            return []  # 返回空列表
        segments = segment_track(  # 轨迹分段
            ship.mmsi, points, quality_report, self.rules, self.config  # MMSI, 点, 质量报告, 规则, 配置
        )
        self.metrics.segment_count += len(segments)  # 累加分段数
        raw_events = [  # 检测所有候选事件
            event  # 候选事件
            for segment in segments  # 遍历每个轨迹段
            for event in detect_candidates(segment, self.rules, self.config)  # 检测段内事件
        ]

        # ── 碰撞风险检测（基于预测轨迹） ──
        # 注意：碰撞风险需要邻近船数据，这里用空字典占位
        # 实际邻近船数据由 orchestrator 在运行时注入
        collision_events = self._detect_collisions(points, {})
        raw_events.extend(collision_events)
        self.metrics.raw_candidate_count += len(raw_events)  # 累加原始事件数
        events = merge_events(raw_events, self.config)  # 合并相邻同类事件
        self.metrics.merged_candidate_count += len(events)  # 累加合并后事件数

        full_track = TrajectorySegment(  # 构建完整轨迹段（用于压缩）
            segment_id=f"TRACK-{ship.mmsi}",  # 段ID（以TRACK-为前缀）
            mmsi=ship.mmsi,  # MMSI
            start_time=points[0].timestamp,  # 起始时间
            end_time=points[-1].timestamp,  # 结束时间
            points=points,  # 轨迹点
            zone_ids=sorted({zone for segment in segments for zone in segment.zone_ids}),  # 所有经过的区域ID（去重排序）
            quality_issues=quality_report.issues,  # 质量问题列表
        )
        packages: list[CandidatePackage] = []  # 候选包列表
        for event in events:  # 遍历每个合并后的事件
            matched_rules = self.rules.select(event, ship, self.config.max_rules)  # 匹配规则
            history, current, compression = compress_for_event(  # 事件级轨迹压缩
                event, full_track, self.config  # 事件, 完整轨迹, 配置
            )
            agent_input = build_agent_input(  # 构建Agent输入数据
                ship,  # 船舶概要
                event,  # 候选事件
                history,  # 压缩后的历史轨迹点
                current,  # 当前点
                matched_rules,  # 匹配的规则
                quality_report,  # 质量报告
            )
            model_key = llm_config.get("model") or self.model_version  # 开发者模式：换模型自动失效旧缓存
            cache_key = build_cache_key(  # 构建缓存键
                agent_input, self.rules.version, model_key  # Agent输入, 规则版本, 模型名
            )
            package = CandidatePackage(  # 创建候选事件包
                event=event,  # 候选事件
                matched_rules=matched_rules,  # 匹配的规则
                agent_input=agent_input,  # Agent输入数据
                compression=compression,  # 压缩信息
                cache_key=cache_key,  # 缓存键
            )
            packages.append(package)  # 添加到包列表
            self.metrics.agent_input_count += 1  # 累加Agent输入计数
            self.metrics.retained_track_points += compression.retained_total_points  # 累加保留的点数
            compressed_characters = len(  # 计算压缩后的字符数
                json.dumps(agent_input, ensure_ascii=False, default=str)  # 序列化Agent输入
            )
            self.metrics.estimated_compressed_characters += compressed_characters  # 累加压缩字符数
        return packages  # 返回候选包列表

    def _detect_collisions(
        self,
        own_points: list[AISPoint],
        nearby_tracks: dict[str, list[AISPoint]],
    ) -> list:
        """检测碰撞风险事件（基于预测轨迹）。

        Args:
            own_points: 本船轨迹点
            nearby_tracks: 邻近船轨迹 {MMSI: [AISPoint]}

        Returns:
            碰撞风险 CandidateEvent 列表
        """
        if not nearby_tracks:
            return []
        try:
            risks = detect_collision_risks(own_points, nearby_tracks, self.config)
            if risks:
                return collision_risks_to_events(risks, own_points[-1].mmsi if own_points else "")
        except Exception:
            pass  # 碰撞检测失败不影响主流程
        return []

    # ── Annotation mode (打标记模式，不压缩不丢弃) ──

    def annotate_track(  # 对单条轨迹运行完整管线（标注模式，保留所有原始数据）
        self, ship: ShipProfile, raw_points: list[AISPoint],  # 船舶概要, 原始轨迹点
        water: Optional[WaterChecker] = None,  # 陆地判定器（可选）
    ) -> AnnotatedTrack:
        """运行完整管线并输出 AnnotatedTrack（保留所有原始数据，只打标记）。"""
        self.metrics.original_track_points += len(raw_points)  # 累加原始轨迹点数

        # 1. 质量清洗（不丢点，只打标记）
        points, quality_report, normalizer = prepare_track(  # 轨迹质量预处理
            ship.mmsi, raw_points, self.config,  # MMSI, 原始点, 配置
            water=water,  # 陆地判定器
        )
        if not points:  # 如果没有有效点
            return AnnotatedTrack(  # 返回空的标注轨迹
                mmsi=ship.mmsi, ship=ship, track=[],  # 空轨迹
                quality_report=quality_report,  # 带质量报告
            )

        # 2. 轨迹分段
        segments = segment_track(  # 轨迹分段
            ship.mmsi, points, quality_report, self.rules, self.config  # MMSI, 点, 质量报告, 规则, 配置
        )
        self.metrics.segment_count += len(segments)  # 累加分段数

        # 3. 候选事件检测
        raw_events = [  # 检测所有候选事件
            event  # 候选事件
            for segment in segments  # 遍历每个轨迹段
            for event in detect_candidates(segment, self.rules, self.config)  # 检测段内事件
        ]
        self.metrics.raw_candidate_count += len(raw_events)  # 累加原始事件数

        # 3.5 碰撞风险检测（基于预测轨迹）
        collision_events = self._detect_collisions(points, {})
        raw_events.extend(collision_events)

        # 4. 事件合并
        events = merge_events(raw_events, self.config)  # 合并相邻同类事件
        self.metrics.merged_candidate_count += len(events)  # 累加合并后事件数

        # 5. 压缩关键点信息（但不丢点）
        full_track = TrajectorySegment(  # 构建完整轨迹段
            segment_id=f"TRACK-{ship.mmsi}",  # 段ID
            mmsi=ship.mmsi,  # MMSI
            start_time=points[0].timestamp,  # 起始时间
            end_time=points[-1].timestamp,  # 结束时间
            points=points,  # 轨迹点
            zone_ids=sorted({zone for segment in segments for zone in segment.zone_ids}),  # 所有区域ID（去重排序）
            quality_issues=quality_report.issues,  # 质量问题列表
        )

        # 6. 规则命中（收集所有事件的规则）
        matched_rules_map: dict[str, list[MatchedRule]] = {}  # event_id -> 匹配规则列表
        key_point_indices: list[int] = []  # 关键点在轨迹中的索引列表
        key_point_reasons: dict[str, str] = {}  # 索引 -> 选择原因的映射
        for event in events:  # 遍历每个事件
            matched_rules = self.rules.select(event, ship, self.config.max_rules)  # 匹配规则
            matched_rules_map[event.event_id] = matched_rules  # 存入映射

            # 收集关键点信息（来自 compress）
            try:
                history, current, compression = compress_for_event(  # 事件级压缩
                    event, full_track, self.config  # 事件, 完整轨迹, 配置
                )
                # 找到关键点在 track 中的索引
                for h_pt in history:  # 遍历压缩后的历史点
                    for idx, t_pt in enumerate(points):  # 遍历原始轨迹点
                        if (abs((t_pt.timestamp - h_pt.timestamp).total_seconds()) < 5  # 时间接近
                                and abs(t_pt.lat - h_pt.lat) < 0.001  # 纬度接近
                                and abs(t_pt.lon - h_pt.lon) < 0.001):  # 经度接近
                            if idx not in key_point_indices:  # 如果该索引尚未记录
                                key_point_indices.append(idx)  # 添加到关键点索引
                                # 取压缩选择的理由
                                reason_idx = len(key_point_indices) - 1  # 当前索引位置
                                if reason_idx < len(compression.selected_reasons):  # 如果有对应理由
                                    key_point_reasons[str(idx)] = (  # 记录选择理由
                                        compression.selected_reasons[reason_idx]
                                    )
                            break  # 找到匹配后跳出内层循环
                # current position（当前点）
                for idx, t_pt in enumerate(points):  # 遍历原始轨迹点
                    if abs((t_pt.timestamp - current.timestamp).total_seconds()) < 5:  # 时间接近
                        if idx not in key_point_indices:  # 如果该索引尚未记录
                            key_point_indices.append(idx)  # 添加到关键点索引
                            key_point_reasons[str(idx)] = "current_position"  # 标记为当前点
                        break  # 找到匹配后跳出循环
            except Exception:  # 捕获所有异常
                pass  # compress 失败不影响 annotation

        compress_info = {  # 压缩信息（关键点索引和原因）
            "key_point_indices": sorted(key_point_indices),  # 排序后的关键点索引
            "reasons": key_point_reasons,  # 索引->原因映射
        }

        # 7. 构建 AnnotatedTrack
        annotated = build_annotated_track(  # 构建标注轨迹
            ship=ship,  # 船舶概要
            raw_points=raw_points,  # 原始轨迹点
            prepared_points=points,  # 预处理后的轨迹点
            segments=segments,  # 轨迹段列表
            events=events,  # 候选事件列表
            matched_rules_map=matched_rules_map,  # 事件->规则映射
            compress_info=compress_info,  # 压缩信息
            quality_report=quality_report,  # 质量报告
            pipeline_metrics=self.metrics,  # 管线指标
            config=self.config,  # 配置
        )
        return annotated  # 返回标注轨迹

    def annotate_file(  # 对文件运行完整管线，输出标注轨迹列表
        self,
        source_path: str,  # CSV源文件路径
        max_ships: Optional[int] = None,  # 最大船舶数（可选）
    ) -> tuple[list[AnnotatedTrack], PipelineMetrics]:
        """对文件运行完整管线，输出 AnnotatedTrack 列表。"""
        preprocessing_started = time.perf_counter()  # 记录预处理开始时间
        ingest_stats = self.store.ingest(source_path, max_ships=max_ships)  # 导入CSV数据
        self.metrics = PipelineMetrics(  # 重置管线指标
            source_path=ingest_stats.source_path,  # 源文件路径
            raw_rows=ingest_stats.raw_rows,  # 原始行数
            valid_rows=ingest_stats.valid_rows,  # 有效行数
            invalid_rows=ingest_stats.invalid_rows,  # 无效行数
            ship_count=ingest_stats.ship_count,  # 船舶总数
            ingest_seconds=ingest_stats.elapsed_seconds,  # 导入耗时
        )
        results: list[AnnotatedTrack] = []  # 标注轨迹结果列表
        water = self._build_water_checker()  # 陆地判定器（从本批次全部船构建走廊）
        for ship, raw_points in self.store.iter_ship_tracks():  # 逐船遍历
            annotated = self.annotate_track(ship, raw_points, water=water)  # 对每条轨迹做标注
            results.append(annotated)  # 添加到结果列表
        self.metrics.preprocessing_seconds = time.perf_counter() - preprocessing_started  # 记录预处理总耗时
        return results, self.metrics  # 返回标注结果和管线指标