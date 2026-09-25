"""
ais_accelerator/models.py — 数据模型定义（Pydantic 合约）
============================================================

【这个文件做什么？】
  定义 AIS 加速器全流程中所有数据结构的"形状"。
  使用 Pydantic v2 做数据校验和序列化。

【关键模型】
  - AISPoint: 单个 AIS 位置报告点（时间/经纬度/速度/航向）
  - ShipProfile: 船舶概要（MMSI/名称/类型/尺寸）
  - QualityReport: 质量评估报告（重复点/无效点/异常标记）
  - TrajectorySegment: 轨迹分段（按时间/区域切割后的连续段）
  - CandidateEvent: 候选事件（超速/捕捞/禁航等 6 种类型）
  - MatchedRule: 命中的规则（来自 regulations.json）
  - CandidatePackage: 加速器→Agent 的最终输出包
  - AnnotatedTrack: 打标记模式下的完整轨迹输出（含特征向量）
  - PipelineMetrics: 管线处理指标（各类计数/耗时/压缩率）
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from datetime import datetime, timezone  # 日期时间处理
from enum import Enum  # 枚举类型
from typing import Any, Dict, List, Optional  # 类型注解

from pydantic import BaseModel, Field  # Pydantic 数据校验框架


class TagSource(str, Enum):  # 标记来源模块枚举
    """标记来源模块"""
    QUALITY = "quality"  # 质量清洗模块
    SEGMENT = "segment"  # 轨迹分段模块
    DETECT = "detect"  # 事件检测模块
    MERGE = "merge"  # 事件合并模块
    COMPRESS = "compress"  # 轨迹压缩模块
    RULES = "rules"  # 规则匹配模块
    ADAPTER = "adapter"  # 数据适配器模块


class TagSeverity(str, Enum):  # 标记严重程度枚举
    """标记严重程度"""
    INFO = "info"  # 信息提示
    WARNING = "warning"  # 警告
    CRITICAL = "critical"  # 严重


class AnnotationTag(BaseModel):  # 对单个AIS点或点区间的标记
    """对单个AIS点或点区间的标记"""
    type: str = Field(description="标记类型: speed_violation|low_speed_fishing|restricted_area_entry|ais_gap|position_jump|course_anomaly|data_quality|rule_hit|segment_boundary|key_point")  # 标记类型
    label: str = Field(description="中文描述")  # 中文标签描述
    detail: dict = Field(default_factory=dict, description="详细信息")  # 详细信息字典
    confidence: float = Field(default=0.0, ge=0, le=1, description="加速器自评置信度")  # 置信度（0~1）
    source: TagSource = Field(description="标记来源模块")  # 标记来源模块
    severity: TagSeverity = Field(default=TagSeverity.INFO, description="严重程度")  # 严重程度
    segment_id: Optional[str] = Field(default=None, description="关联的segment ID")  # 关联的轨迹段ID
    event_id: Optional[str] = Field(default=None, description="关联的event ID")  # 关联的事件ID


class QualitySeverity(str, Enum):  # 质量问题的严重程度枚举
    INFO = "info"  # 信息
    WARNING = "warning"  # 警告
    CRITICAL = "critical"  # 严重


class CandidateType(str, Enum):  # 候选事件类型枚举
    RESTRICTED_AREA_ENTRY = "restricted_area_entry"  # 进入禁航区
    SPEED_VIOLATION = "speed_violation"  # 超速违规
    LOW_SPEED_FISHING = "low_speed_fishing"  # 低速捕捞嫌疑
    AIS_GAP = "ais_gap"  # AIS信号缺失
    COURSE_ANOMALY = "course_anomaly"  # 航向异常
    POSITION_JUMP = "position_jump"  # 位置跳变
    COLLISION_RISK = "collision_risk"  # 碰撞风险


class QualityIssue(BaseModel):  # 质量问题记录模型
    code: str  # 问题代码
    severity: QualitySeverity  # 问题严重程度
    message: str  # 问题描述信息
    timestamp: Optional[datetime] = None  # 问题发生时间戳
    details: Dict[str, Any] = Field(default_factory=dict)  # 问题详情扩展字段


class AISPoint(BaseModel):  # 单个AIS数据点模型
    mmsi: str  # 船舶MMSI标识号
    timestamp: datetime  # 数据点时间戳
    lat: float  # 纬度
    lon: float  # 经度
    speed: float  # 航速（节）
    course: float  # 航向（度）
    status: Optional[str] = None  # 船舶状态
    quality_flags: List[str] = Field(default_factory=list)  # 质量标记列表
    tags: List[str] = Field(default_factory=list, description="人类可读的标记列表，如['疑似超速(v1)','最高速度']")  # 人类可读的标记标签列表

    def to_agent_position(self) -> Dict[str, Any]:  # 转换为Agent可用的位置字典格式
        data: Dict[str, Any] = {  # 构建基础位置数据字典
            "lat": self.lat,  # 纬度
            "lon": self.lon,  # 经度
            "speed": self.speed,  # 航速
            "course": self.course,  # 航向
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),  # ISO格式时间戳
        }
        if self.status:  # 如果有船舶状态
            data["status"] = self.status  # 添加状态字段
        if self.tags:  # 如果有标签
            data["tags"] = self.tags  # 添加标签字段
        return data  # 返回字典


class ShipProfile(BaseModel):  # 船舶概要模型
    mmsi: str  # 船舶MMSI标识号
    name: Optional[str] = None  # 船舶名称
    ship_type: Optional[str] = None  # 船舶类型
    length: Optional[float] = None  # 船长（米）
    width: Optional[float] = None  # 船宽（米）


class QualityReport(BaseModel):  # 数据质量评估报告模型
    mmsi: str  # 船舶MMSI标识号
    original_points: int = 0  # 原始数据点数
    retained_points: int = 0  # 保留的有效点数
    duplicate_points: int = 0  # 重复点数
    invalid_points: int = 0  # 质量清洗剔除的异常点数（重复点 + 位置跳变点）
    issues: List[QualityIssue] = Field(default_factory=list)  # 质量问题列表

    @property
    def level(self) -> str:  # 综合质量等级
        severities = {issue.severity for issue in self.issues}  # 收集所有问题的严重级别
        if QualitySeverity.CRITICAL in severities:  # 如果有严重问题
            return "poor"  # 质量为"差"
        if QualitySeverity.WARNING in severities:  # 如果有警告
            return "fair"  # 质量为"一般"
        return "good"  # 质量为"好"


class TrajectorySegment(BaseModel):  # 轨迹分段段模型
    segment_id: str  # 分段ID
    mmsi: str  # 船舶MMSI标识号
    start_time: datetime  # 段开始时间
    end_time: datetime  # 段结束时间
    points: List[AISPoint]  # 段内包含的AIS数据点列表
    zone_ids: List[str] = Field(default_factory=list)  # 经过的区域ID列表
    quality_issues: List[QualityIssue] = Field(default_factory=list)  # 该段的质量问题列表


class CandidateEvent(BaseModel):  # 候选事件模型
    event_id: str  # 事件唯一ID
    mmsi: str  # 船舶MMSI标识号
    candidate_type: CandidateType  # 候选事件类型
    related_types: List[CandidateType] = Field(default_factory=list)  # 相关的事件类型列表
    start_time: datetime  # 事件开始时间
    end_time: datetime  # 事件结束时间
    zone_id: Optional[str] = None  # 关联区域ID
    zone_name: Optional[str] = None  # 关联区域名称
    priority: str = "middle"  # 事件优先级（high/middle/low）
    features: Dict[str, Any] = Field(default_factory=dict)  # 事件特征字典
    evidence_points: List[AISPoint] = Field(default_factory=list)  # 作为证据的AIS数据点列表
    quality_issues: List[QualityIssue] = Field(default_factory=list)  # 相关问题列表
    source_event_ids: List[str] = Field(default_factory=list)  # 合并前原始事件ID列表


class MatchedRule(BaseModel):  # 匹配到的规则模型
    rule_id: str  # 规则ID
    violation_code: str  # 违规代码
    label: str  # 规则中文标签
    description: str = ""  # 规则描述
    zone_id: Optional[str] = None  # 关联区域ID
    zone_name: Optional[str] = None  # 关联区域名称
    speed_limit: Optional[float] = None  # 速度限制（节）
    validity_status: str = "effective"  # 规则有效性状态
    source: str = "regulations.json"  # 规则来源文件
    metadata: Dict[str, Any] = Field(default_factory=dict)  # 规则元数据扩展


class CompressionMetadata(BaseModel):  # 轨迹压缩元数据模型
    original_points: int  # 原始轨迹点数
    retained_history_points: int  # 保留的历史点数
    retained_total_points: int  # 保留的总点数
    compression_ratio: float  # 压缩率
    selected_reasons: List[str] = Field(default_factory=list)  # 关键点选择原因列表
    estimated_input_characters: int = 0  # 估计的输入字符数


class CandidatePackage(BaseModel):  # 加速器输出给Agent的候选事件包
    """The stable boundary between the accelerator and the existing agent."""

    event: CandidateEvent  # 候选事件
    matched_rules: List[MatchedRule]  # 匹配到的规则列表
    agent_input: Dict[str, Any]  # Agent输入数据字典
    compression: CompressionMetadata  # 压缩元数据
    cache_key: str  # 缓存键


class AgentAnalysisRecord(BaseModel):  # Agent分析记录模型
    event: CandidateEvent  # 被分析的候选事件
    matched_rules: List[MatchedRule]  # 匹配的规则
    compression: CompressionMetadata  # 压缩元数据
    agent_result: Dict[str, Any]  # Agent分析结果
    cache_key: str  # 缓存键
    cache_hit: bool = False  # 是否命中缓存
    agent_latency_seconds: float = 0.0  # Agent推理延迟（秒）


class IngestStats(BaseModel):  # 数据导入统计模型
    source_path: str  # 源文件路径
    cache_path: str  # 缓存文件路径
    raw_rows: int = 0  # 原始行数
    valid_rows: int = 0  # 有效行数
    invalid_rows: int = 0  # 无效行数
    ship_count: int = 0  # 船舶数量
    reused_cache: bool = False  # 是否复用了缓存
    elapsed_seconds: float = 0.0  # 耗时（秒）


class PipelineMetrics(BaseModel):  # 管线处理指标模型
    source_path: str = ""  # 源文件路径
    raw_rows: int = 0  # 原始数据行数
    valid_rows: int = 0  # 有效数据行数
    invalid_rows: int = 0  # 无效数据行数
    ship_count: int = 0  # 船舶数量
    segment_count: int = 0  # 轨迹分段数
    raw_candidate_count: int = 0  # 原始候选事件数
    merged_candidate_count: int = 0  # 合并后的候选事件数
    agent_input_count: int = 0  # 发送给Agent的输入数量
    original_track_points: int = 0  # 原始轨迹点数
    retained_track_points: int = 0  # 保留的轨迹点数
    estimated_original_characters: int = 0  # 估计的原始字符数
    estimated_compressed_characters: int = 0  # 估计的压缩后字符数
    cache_hits: int = 0  # 缓存命中次数
    agent_calls: int = 0  # Agent调用次数
    ingest_seconds: float = 0.0  # 数据导入耗时（秒）
    preprocessing_seconds: float = 0.0  # 预处理耗时（秒）
    agent_seconds: float = 0.0  # Agent推理耗时（秒）

    def summary(self) -> Dict[str, Any]:  # 生成汇总统计字典
        call_reduction_base = self.ship_count or 1  # 计算调用缩减率的基数
        return {  # 返回包含衍生指标的完整统计字典
            **self.model_dump(),  # 包含所有基础字段
            "candidate_filter_rate": round(  # 候选事件过滤率
                1 - self.merged_candidate_count / call_reduction_base, 4
            ),
            "point_compression_rate": round(  # 轨迹点压缩率
                1 - self.retained_track_points / (self.original_track_points or 1),
                4,
            ),
        }


class AnnotatedTrack(BaseModel):  # 加速器打标记后的完整输出模型
    """加速器打标记后的完整输出——不压缩不筛选，原始数据 + 标记"""

    mmsi: str  # 船舶MMSI标识号
    ship: ShipProfile  # 船舶概要信息
    annotations: List[AnnotationTag] = Field(  # 所有标记的完整列表
        default_factory=list,
        description="所有标记的完整列表",
    )
    track: List[AISPoint] = Field(  # 完整轨迹点列表
        default_factory=list,
        description="完整轨迹（所有点均保留，不压缩不丢弃）",
    )
    segments: List[str] = Field(  # 各segment的边界点索引
        default_factory=list,
        description="各segment的边界点索引，如['0','47','128']",
    )
    events: List[CandidateEvent] = Field(  # 检测到的候选事件列表
        default_factory=list,
        description="检测到的候选事件（保留完整信息）",
    )
    feature_vector: List[float] = Field(  # 供ML层使用的特征向量
        default_factory=list,
        description="供ML层使用的特征向量",
    )
    feature_metadata: Dict[str, Any] = Field(  # 特征明细字典
        default_factory=dict,
        description="特征明细（特征名→值）",
    )
    quality_report: Optional[QualityReport] = None  # 质量评估报告
    raw_track: List[dict] = Field(  # 原始轨迹点（清洗前，≤200 点抽样，带 quality_flags）
        default_factory=list,
        description="原始轨迹点（quality 清洗前），用于前端原始数据可视化",
    )
    pipeline_metrics: Optional[PipelineMetrics] = None  # 管线处理指标
    processed_at: str = Field(  # 处理时间戳（ISO格式）
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def annotation_counts(self) -> Dict[str, int]:  # 按严重程度+类型统计标记数量
        counts: Dict[str, int] = {}  # 统计结果字典
        for ann in self.annotations:  # 遍历所有标记
            key = f"{ann.severity.value}_{ann.type}"  # 构造统计键（严重级别_类型）
            counts[key] = counts.get(key, 0) + 1  # 计数累加
        return counts  # 返回统计结果

    def to_agent_input(self) -> Dict[str, Any]:  # 转换为旧的agent_input格式（兼容旧接口）
        """转换为旧的agent_input格式（兼容旧接口）"""
        return {  # 返回兼容旧接口的字典
            "ship": self.ship.model_dump(mode="json"),  # 船舶概要（JSON格式）
            "track": [pt.to_agent_position() for pt in self.track],  # 轨迹点列表（Agent格式）
            "annotations": [ann.model_dump(mode="json") for ann in self.annotations],  # 标记列表（JSON格式）
            "annotation_counts": self.annotation_counts,  # 标记分类统计
            "segments": self.segments,  # 分段边界索引
            "events": [ev.model_dump(mode="json") for ev in self.events],  # 候选事件列表（JSON格式）
            "feature_vector": self.feature_vector,  # 特征向量
            "feature_metadata": self.feature_metadata,  # 特征元数据
            "quality_report": self.quality_report.model_dump(mode="json")  # 质量报告（JSON格式）
            if self.quality_report  # 如果质量报告存在
            else None,  # 否则返回None
            "processed_at": self.processed_at,  # 处理时间戳
        }