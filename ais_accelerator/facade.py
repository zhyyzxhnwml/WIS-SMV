"""
ais_accelerator/facade.py — 外观模式统一接口
==============================================

【这个文件做什么？】
  提供 AIS 加速器的"外观"（Facade）模式统一接口。
  上层调用者（CLI/HTTP API/编排器）只需和这个类打交道。

【提供的接口】
  - iter_agent_inputs(): 遍历所有候选事件包（生成器，节省内存）
  - prepare_file(): 一次性处理整个文件
  - annotate_file(): 标注模式处理文件（不压缩，只打标记）
  - annotate_track(): 对单条内存中的轨迹做标注
  - prepare_track(): 处理单条内存轨迹，返回候选包
  - analyze_file(): 配合外部 detector 做完整的 Agent 分析（含缓存）

【设计模式——外观（Facade）】
  隐藏内部复杂的管线、缓存、配置等细节，对外提供简洁的 API。
  调用方不需要知道内部的 pipeline/cache/metrics 如何协作。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import time  # 时间模块
from typing import Any, Iterator, Optional, Protocol  # 类型注解

from .cache import AnalysisResultCache  # 分析结果缓存
from .config import AcceleratorConfig  # 加速器配置
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    AgentAnalysisRecord,  # Agent分析记录
    AnnotatedTrack,  # 标注后的完整轨迹
    CandidatePackage,  # 候选事件包（含agent_input）
    PipelineMetrics,  # 管线运行指标
    ShipProfile,  # 船舶概要
)
from .pipeline import AcceleratorPipeline  # 加速器管线
from .quality import parse_raw_point  # 原始数据点解析函数
from .rules import RuleProvider  # 规则提供者协议
from .water import WaterChecker  # 陆地/水域判定器（规避陆地上的 AIS 数据）


class ExistingDetector(Protocol):  # 外部Agent检测器的协议接口
    def analyze(self, ship_data: dict) -> dict:  # 分析船舶数据，返回结果字典
        ...


class AISAccelerationFacade:  # AIS加速器外观类（统一接口）
    """Stable facade used by CLI, HTTP API, and the existing agent program."""

    def __init__(  # 初始化外观
        self,
        config: Optional[AcceleratorConfig] = None,  # 加速器配置（可选，默认创建）
        rule_provider: Optional[RuleProvider] = None,  # 规则提供者（可选）
        model_version: str = "qwen2.5:7b-instruct-q4_K_M",  # LLM模型版本
    ):
        self.config = config or AcceleratorConfig()  # 配置对象
        self.pipeline = AcceleratorPipeline(  # 创建加速器管线
            config=self.config,  # 传递配置
            rule_provider=rule_provider,  # 传递规则提供者
            model_version=model_version,  # 传递模型版本
        )
        self.cache = AnalysisResultCache(  # 创建分析结果缓存
            self.config.cache_dir / "agent_results.sqlite"  # 缓存数据库路径
        )

    @property
    def metrics(self) -> PipelineMetrics:  # 获取管线运行指标
        return self.pipeline.metrics  # 委托给管线对象

    def iter_agent_inputs(  # 遍历所有候选事件包（生成器模式，节省内存）
        self, source_path: str, max_ships: Optional[int] = None  # CSV源文件路径, 最大船舶数（可选）
    ) -> Iterator[CandidatePackage]:
        """Yield packages whose ``agent_input`` can be passed to analyze()."""
        yield from self.pipeline.iter_file(source_path, max_ships=max_ships)  # 委托给管线

    def prepare_file(  # 一次性处理整个文件，返回所有候选包和指标
        self, source_path: str, max_ships: Optional[int] = None  # CSV源文件路径, 最大船舶数（可选）
    ) -> tuple[list[CandidatePackage], PipelineMetrics]:
        packages = list(self.iter_agent_inputs(source_path, max_ships=max_ships))  # 收集所有候选包
        return packages, self.metrics  # 返回候选包列表和指标

    def annotate_file(  # 对文件运行标注模式管线 —— 不压缩不筛选，只打标记
        self, source_path: str, max_ships: Optional[int] = None  # CSV源文件路径, 最大船舶数（可选）
    ) -> tuple[list[AnnotatedTrack], PipelineMetrics]:
        """对文件运行标注模式管线 —— 不压缩不筛选，只打标记。"""
        return self.pipeline.annotate_file(source_path, max_ships=max_ships)  # 委托给管线

    def annotate_track(  # 对单条内存中的轨迹运行标注模式
        self,
        ship: dict[str, Any],  # 船舶信息字典
        points: list[dict[str, Any] | AISPoint],  # 轨迹点列表（原始字典或AISPoint对象）
        water: Optional[WaterChecker] = None,  # 陆地判定器（可选）
    ) -> AnnotatedTrack:
        """对单条轨迹运行标注模式 —— 返回带标记的完整轨迹。"""
        profile = ShipProfile.model_validate(ship)  # 验证并构建船舶概要
        normalized: list[AISPoint] = []  # 归一化后的AIS点列表
        for value in points:  # 遍历每个点
            if isinstance(value, AISPoint):  # 如果已是AISPoint对象
                normalized.append(value)  # 直接添加
                continue  # 继续下一个
            row = {**value, "mmsi": profile.mmsi}  # 补充MMSI字段
            point, _issues = parse_raw_point(row)  # 解析原始数据行
            if point is not None:  # 如果解析成功
                normalized.append(point)  # 添加归一化后的点
        return self.pipeline.annotate_track(profile, normalized, water=water)  # 委托给管线进行标注

    def prepare_track(  # 处理单条内存轨迹，返回候选包列表（服务间直接调用）
        self,
        ship: dict[str, Any],  # 船舶信息字典
        points: list[dict[str, Any] | AISPoint],  # 轨迹点列表
        water: Optional[WaterChecker] = None,  # 陆地判定器（可选）
    ) -> list[CandidatePackage]:
        """Prepare an in-memory track for direct service-to-service use."""
        profile = ShipProfile.model_validate(ship)  # 验证并构建船舶概要
        normalized: list[AISPoint] = []  # 归一化后的AIS点列表
        for value in points:  # 遍历每个点
            if isinstance(value, AISPoint):  # 如果已是AISPoint对象
                normalized.append(value)  # 直接添加
                continue  # 继续下一个
            row = {**value, "mmsi": profile.mmsi}  # 补充MMSI字段
            point, _issues = parse_raw_point(row)  # 解析原始数据行
            if point is not None:  # 如果解析成功
                normalized.append(point)  # 添加归一化后的点
        self.pipeline.metrics = PipelineMetrics(  # 设置管线指标
            source_path="in_memory",  # 来源标记为内存
            raw_rows=len(points),  # 原始行数
            valid_rows=len(normalized),  # 有效行数
            invalid_rows=len(points) - len(normalized),  # 无效行数
            ship_count=1,  # 只处理一条船
        )
        return self.pipeline.packages_for_track(profile, normalized, water=water)  # 委托给管线生成候选包

    def analyze_file(  # 配合外部 detector 做完整的 Agent 分析（支持缓存）
        self,
        detector: ExistingDetector,  # 外部Agent检测器
        source_path: str,  # CSV源文件路径
        max_ships: Optional[int] = None,  # 最大船舶数（可选）
        use_cache: bool = True,  # 是否使用缓存（默认开启）
    ) -> tuple[list[AgentAnalysisRecord], PipelineMetrics]:
        records: list[AgentAnalysisRecord] = []  # Agent分析记录列表
        for package in self.iter_agent_inputs(source_path, max_ships=max_ships):  # 遍历每个候选包
            cached = self.cache.get(package.cache_key) if use_cache else None  # 尝试从缓存获取
            started = time.perf_counter()  # 记录开始时间
            if cached is None:  # 如果缓存未命中
                result = detector.analyze(package.agent_input)  # 调用外部检测器分析
                latency = time.perf_counter() - started  # 计算推理耗时
                self.pipeline.metrics.agent_calls += 1  # 记录Agent调用次数
                self.pipeline.metrics.agent_seconds += latency  # 累计Agent耗时
                if use_cache:  # 如果启用缓存
                    self.cache.put(package.cache_key, result)  # 将结果存入缓存
                cache_hit = False  # 标记为缓存未命中
            else:  # 如果缓存命中
                result = cached  # 直接使用缓存结果
                latency = time.perf_counter() - started  # 计算读取耗时
                self.pipeline.metrics.cache_hits += 1  # 记录缓存命中次数
                cache_hit = True  # 标记为缓存命中
            records.append(  # 添加分析记录
                AgentAnalysisRecord(
                    event=package.event,  # 候选事件
                    matched_rules=package.matched_rules,  # 匹配的规则
                    compression=package.compression,  # 压缩信息
                    agent_result=result,  # Agent分析结果
                    cache_key=package.cache_key,  # 缓存键
                    cache_hit=cache_hit,  # 是否缓存命中
                    agent_latency_seconds=latency,  # 推理耗时
                )
            )
        return records, self.metrics  # 返回所有分析记录和管线指标