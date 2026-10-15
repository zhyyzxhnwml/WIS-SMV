"""
ais_accelerator/annotator.py — 标注构建器
===========================================

【这个文件做什么？】
  将管线各阶段的输出（质量/分段/事件/规则/压缩）聚合为结构化的
  AnnotatedTrack 对象。所有原始数据完整保留，附加各类标记（tag）。

【两种模式】
  1. 标注模式（当前实现）：不压缩不筛选，所有点保留，只打标记
  2. 压缩模式（pipelines.py）：筛选关键点，输出 CandidatePackage

【标注类型】
  - quality: 数据质量问题（重复点/坐标越界/AIS中断等）
  - segment: 轨迹段边界
  - detect: 候选事件（超速/捕捞/禁航等）
  - rules: 规则命中
  - key_point: 关键点（压缩阶段选出的重要点）

【特征向量提取（22 维）】
  为 ML Triage 层提取定长特征向量：
  - 点数、标记数、严重程度分布
  - 平均/最高/最低置信度
  - 时长、速度统计（平均/最大/最小/方差）
  - 航向变化次数、AIS中断次数、位置跳变次数
  - 规则命中数、事件数、重叠标记数、分段数
  - 质量问题和重复点计数
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import json  # JSON序列化模块
from typing import Any, Optional  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    AnnotatedTrack,  # 标注后的完整轨迹
    AnnotationTag,  # 标注标记
    CandidateEvent,  # 候选事件
    MatchedRule,  # 匹配的规则
    PipelineMetrics,  # 管线运行指标
    QualityReport,  # 数据质量报告
    ShipProfile,  # 船舶概要
    TagSeverity,  # 标记严重程度枚举
    TagSource,  # 标记来源枚举
    TrajectorySegment,  # 轨迹段
)


def _sample_raw_track(raw_points, max_points: int = 200) -> list[dict]:
    """从原始轨迹点（清洗前）抽样 ≤max_points 个点，带 quality_flags。

    用于前端"原始数据可视化"图层（灰色原始轨迹 + 噪声点高亮）。
    """
    if not raw_points:
        return []
    n = len(raw_points)
    if n <= max_points:
        sampled = raw_points
    else:
        # 等间隔抽样，保留首尾
        step = n / max_points
        indices = [int(i * step) for i in range(max_points)]
        indices[-1] = n - 1
        sampled = [raw_points[i] for i in indices]
    out = []
    for pt in sampled:
        try:
            ts = pt.timestamp.isoformat() if hasattr(pt.timestamp, "isoformat") else str(pt.timestamp)
        except Exception:
            ts = ""
        out.append({
            "lat": pt.lat, "lon": pt.lon, "speed": pt.speed,
            "course": pt.course or 0.0, "timestamp": ts,
            "quality_flags": list(pt.quality_flags) if pt.quality_flags else [],
        })
    return out


def build_annotated_track(  # 构建带标注的完整轨迹
    ship: ShipProfile,  # 船舶概要信息
    raw_points: list[AISPoint],  # 原始输入AIS点列表（quality之前）
    prepared_points: list[AISPoint],  # 经过quality处理后的AIS点列表
    segments: list[TrajectorySegment],  # 轨迹分段结果
    events: list[CandidateEvent],  # 候选事件列表（detect + merge后）
    matched_rules_map: dict[str, list[MatchedRule]],  # event_id -> 匹配规则列表的映射
    compress_info: Optional[dict] = None,  # 来自compress的关键点信息（可选）
    quality_report: Optional[QualityReport] = None,  # 数据质量报告（可选）
    pipeline_metrics: Optional[PipelineMetrics] = None,  # 管线运行指标（可选）
    config: Optional[AcceleratorConfig] = None,  # 加速器配置（可选）
) -> AnnotatedTrack:
    """将管线输出聚合为带标记的完整轨迹。

    所有原始点完整保留，不压缩、不筛选、不丢弃。
    各类标记（质量/分段/事件/规则）附加到 points 的 tags 字段 + annotations 列表。
    """
    tag_seq: list[AnnotationTag] = []  # 标注标记序列
    track = list(prepared_points)  # 构建可变的轨迹点列表
    event_tag_index: dict[str, int] = {}  # event_id -> tag_seq索引的映射（用于关联规则）

    # ── 1. 质量标记 ──
    if quality_report:  # 如果有质量报告
        _annotate_quality(quality_report, tag_seq, track)  # 添加质量问题标记

    # ── 2. 分段标记 ──
    _annotate_segments(segments, tag_seq, track)  # 添加轨迹段边界标记

    # ── 3. 候选事件标记 ──
    _annotate_events(events, tag_seq, track, event_tag_index)  # 添加候选事件标记

    # ── 4. 规则命中标记 ──
    _annotate_rules(matched_rules_map, event_tag_index, tag_seq)  # 添加规则命中标记

    # ── 5. 关键点标记（compress 信息） ──
    _annotate_key_points(compress_info, tag_seq, track)  # 添加关键点标记

    # ── 6. 提取特征向量 ──
    feature_vector, feature_metadata = _extract_features(  # 提取ML特征向量
        ship, prepared_points, segments, events, matched_rules_map,  # 输入数据
        quality_report, tag_seq  # 质量报告和标记序列
    )

    # ── 7. 构建 segment 边界索引 ──
    segment_boundaries = _build_segment_boundaries(segments, prepared_points)  # 计算段边界索引

    # ── 8. 原始轨迹抽样（≤200 点，带 quality_flags）──
    _raw_track = _sample_raw_track(raw_points)  # 抽样原始轨迹点

    return AnnotatedTrack(  # 返回完整的标注轨迹
        mmsi=ship.mmsi,  # 船舶MMSI
        ship=ship,  # 船舶概要
        annotations=tag_seq,  # 标注标记列表
        track=track,  # 轨迹点列表（带有tags标记）
        segments=segment_boundaries,  # 段边界索引
        events=events,  # 候选事件列表
        feature_vector=feature_vector,  # 特征向量（ML用）
        feature_metadata=feature_metadata,  # 特征元数据（特征名->值）
        quality_report=quality_report,  # 质量报告
        pipeline_metrics=pipeline_metrics,  # 管线运行指标
        raw_track=_raw_track,  # 原始轨迹抽样
    )


# ═══════════════════════════════════════════════════════════
# 内部标记函数
# ═══════════════════════════════════════════════════════════

def _severity_for_code(code: str) -> TagSeverity:  # 根据质量问题代码确定严重程度
    if code in {"invalid_mmsi", "coordinate_out_of_range", "parse_error"}:  # 严重问题
        return TagSeverity.CRITICAL  # 返回"严重"级别
    if code in {"ais_gap", "position_jump", "speed_out_of_range",  # 需要关注的问题
                 "course_out_of_range", "non_increasing_time"}:
        return TagSeverity.WARNING  # 返回"警告"级别
    return TagSeverity.INFO  # 其他问题返回"信息"级别


def _annotate_quality(  # 为轨迹添加质量问题标记
    quality_report: QualityReport,  # 质量报告
    tag_seq: list[AnnotationTag],  # 标记序列（将被追加）
    track: list[AISPoint],  # 轨迹点列表
) -> None:
    for issue in quality_report.issues:  # 遍历每个质量问题
        severity = _severity_for_code(issue.code)  # 确定该问题的严重程度
        tag = AnnotationTag(  # 创建标注标记
            type=f"data_quality_{issue.code}",  # 标记类型（以data_quality_前缀区分）
            label=issue.message,  # 标记标签（问题描述）
            detail=issue.details,  # 标记详情
            confidence=0.95 if severity == TagSeverity.CRITICAL else 0.85,  # 置信度（严重问题更高）
            source=TagSource.QUALITY,  # 标记来源：质量检查
            severity=severity,  # 严重程度
        )
        tag_seq.append(tag)  # 将标记添加到序列


def _annotate_segments(  # 为轨迹添加分段边界标记
    segments: list[TrajectorySegment],  # 轨迹段列表
    tag_seq: list[AnnotationTag],  # 标记序列（将被追加）
    track: list[AISPoint],  # 轨迹点列表
) -> None:
    for seg in segments:  # 遍历每个轨迹段
        tag_seq.append(AnnotationTag(  # 添加段边界标记
            type="segment_boundary",  # 标记类型
            label=f"轨迹段: {seg.segment_id}",  # 段标识
            detail={  # 详情
                "segment_id": seg.segment_id,  # 段ID
                "point_count": len(seg.points),  # 该段包含的点数
                "zone_ids": seg.zone_ids,  # 经过的区域ID列表
            },
            confidence=1.0,  # 分段是确定的，置信度为1.0
            source=TagSource.SEGMENT,  # 标记来源：轨迹分段
            severity=TagSeverity.INFO,  # 信息级别
            segment_id=seg.segment_id,  # 关联的段ID
        ))
        for issue in seg.quality_issues:  # 遍历该段内的质量问题
            tag_seq.append(AnnotationTag(  # 添加分段质量标记
                type=f"segment_quality_{issue.code}",  # 标记类型
                label=issue.message,  # 问题描述
                detail=issue.details,  # 问题详情
                confidence=0.85,  # 置信度
                source=TagSource.SEGMENT,  # 标记来源：轨迹分段
                severity=_severity_for_code(issue.code),  # 根据代码确定严重程度
                segment_id=seg.segment_id,  # 关联的段ID
            ))


def _index_of(  # 在轨迹点列表中查找与目标点最匹配的点的索引
    point: AISPoint,  # 目标点
    track: list[AISPoint],  # 轨迹点列表
    default: int = 0,  # 未找到时的默认索引
) -> int:
    """在 track 中查找点的时间位置（近似匹配）""" 
    for i, p in enumerate(track):  # 遍历轨迹点# 时间差<5秒# 纬度差<0.001度
        if abs((p.timestamp - point.timestamp).total_seconds()) < 5 and abs(p.lat - point.lat) < 0.001  and abs(p.lon - point.lon) < 0.001:  # 经度差<0.001度
            return i  # 返回匹配的索引
    return default  # 未找到，返回默认索引


def _annotate_events(  # 为轨迹添加候选事件标记
    events: list[CandidateEvent],  # 候选事件列表
    tag_seq: list[AnnotationTag],  # 标记序列（将被追加）
    track: list[AISPoint],  # 轨迹点列表
    event_tag_index: dict[str, int],  # event_id->tag索引的映射（字典将被填充）
) -> None:
    for event in events:  # 遍历每个候选事件
        # 在对应的轨迹点上打 tags
        tags_for_this_event: set[str] = set()  # 该事件需要添加的tag文本集合
        for ev_pt in event.evidence_points:  # 遍历事件的每个证据点
            idx = _index_of(ev_pt, track)  # 在轨迹中查找该点
            if idx < len(track):  # 如果找到了有效索引
                tag_text = f"[{event.candidate_type.value}]"  # 构建tag文本，如[速度异常]
                track[idx].tags.append(tag_text) if tag_text not in track[idx].tags else None  # 去重添加
                tags_for_this_event.add(tag_text)  # 记录已添加的tag文本

        # 构建 AnnotationTag
        candidate_label = {  # 事件类型 -> 中文标签的映射
            "speed_violation": "超速行驶",  # 超速（含区域限速与数据驱动两路判据）
            "low_speed_fishing": "疑似低速捕捞",  # 低速捕捞（开阔水域作业嫌疑）
            "illegal_fishing": "违规捕捞",  # 违规捕捞（禁渔区/禁渔期内的法定违规）
            "restricted_area_entry": "进入限制区域",  # 进入禁航区
            "anchoring_out_of_bounds": "越界锚泊",  # 越界锚泊
            "fairway_anchoring": "航道内锚泊",  # 航道内锚泊（数据驱动走廊）
            "overtaking_violation": "违规追越",  # 违规追越（COLREG 第13条）
            "give_way_failure": "让路船未让路",  # 让路船未让路（COLREG 第15/16条）
            "illegal_towing": "违规拖带/并靠",  # 违规拖带/并靠（长时间极小间距同速同行）
            "wrong_way_navigation": "逆向航行",  # 逆向航行（COLREG 第10条）
            "collision_risk": "碰撞风险",  # 碰撞风险
            "ais_gap": "AIS信号中断",  # AIS信号中断（时间上没收到报文）
            "ais_off": "AIS导航要素不可用",  # 报文收到了但内容整体不可用
            "position_jump": "位置跳变",  # 位置跳变
            "course_anomaly": "航向突变",  # 航向突变
        }.get(event.candidate_type.value, event.candidate_type.value)  # 获取中文标签，默认用英文

        tag = AnnotationTag(  # 创建事件标记
            type=event.candidate_type.value,  # 标记类型（事件类型字符串）
            label=candidate_label,  # 中文标签
            detail={  # 详情
                "event_id": event.event_id,  # 事件ID
                "priority": event.priority,  # 优先级
                "features": event.features,  # 事件特征
                "zone_id": event.zone_id,  # 区域ID
                "zone_name": event.zone_name,  # 区域名称
                "related_types": [t.value for t in event.related_types],  # 相关类型列表
                "evidence_point_indices": [  # 证据点在轨迹中的索引列表
                    _index_of(pt, track) for pt in event.evidence_points  # 查找每个证据点
                ],
            },
            confidence={  # 基于优先级的置信度
                "high": 0.90,  # 高优先级
                "middle": 0.75,  # 中优先级
                "low": 0.60,  # 低优先级
            }.get(event.priority, 0.75),  # 默认0.75
            source=TagSource.DETECT,  # 标记来源：事件检测
            severity=TagSeverity.WARNING if event.priority == "high" else TagSeverity.INFO,  # 高优先级为警告，其余为信息
            segment_id=event.event_id,  # 关联的段ID（复用事件ID）
            event_id=event.event_id,  # 关联的事件ID
        )
        event_tag_index[event.event_id] = len(tag_seq)  # 记录事件标记在序列中的位置
        tag_seq.append(tag)  # 将标记添加到序列


def _annotate_rules(  # 为事件添加规则命中标记
    matched_rules_map: dict[str, list[MatchedRule]],  # event_id -> 匹配规则列表的映射
    event_tag_index: dict[str, int],  # event_id -> tag索引的映射
    tag_seq: list[AnnotationTag],  # 标记序列（将被追加）
) -> None:
    for event_id, rules in matched_rules_map.items():  # 遍历每个事件ID及其匹配规则
        parent_idx = event_tag_index.get(event_id)  # 获取父事件标记的索引（暂未使用）
        for rule in rules:  # 遍历每条规则
            tag = AnnotationTag(  # 创建规则命中标记
                type="rule_hit",  # 标记类型
                label=rule.label,  # 规则标签
                detail={  # 详情
                    "rule_id": rule.rule_id,  # 规则ID
                    "violation_code": rule.violation_code,  # 违规代码
                    "zone_id": rule.zone_id,  # 区域ID
                    "zone_name": rule.zone_name,  # 区域名称
                    "speed_limit": rule.speed_limit,  # 限速值
                    "description": rule.description,  # 规则描述
                    "event_id": event_id,  # 关联的事件ID
                },
                confidence=0.90 if rule.validity_status == "effective" else 0.50,  # 生效规则置信度高
                source=TagSource.RULES,  # 标记来源：规则引擎
                severity=TagSeverity.WARNING if rule.validity_status == "effective" else TagSeverity.INFO,  # 生效规则为警告，其余为信息
                event_id=event_id,  # 关联的事件ID
            )
            tag_seq.append(tag)  # 将标记添加到序列


def _annotate_key_points(  # 为轨迹点添加关键点标记（来自压缩阶段）
    compress_info: Optional[dict],  # 压缩信息字典（可选）
    tag_seq: list[AnnotationTag],  # 标记序列（将被追加）
    track: list[AISPoint],  # 轨迹点列表
) -> None:
    if not compress_info:  # 如果没有压缩信息
        return  # 直接返回
    for idx in compress_info.get("key_point_indices", []):  # 遍历关键点索引列表
        if 0 <= idx < len(track):  # 如果索引在有效范围内
            label = compress_info.get("reasons", {}).get(str(idx), "关键点")  # 获取该点的标记原因，默认"关键点"
            track[idx].tags.append(f"[{label}]")  # 在轨迹点上添加tag


def _build_segment_boundaries(  # 构建段边界索引列表
    segments: list[TrajectorySegment],  # 轨迹段列表
    track: list[AISPoint],  # 轨迹点列表
) -> list[str]:
    """返回每个 segment 在 track 中的起始点索引"""
    boundaries: list[str] = []  # 边界索引列表
    for seg in segments:  # 遍历每个轨迹段
        if seg.points:  # 如果段内有轨迹点
            idx = _index_of(seg.points[0], track)  # 查找该段第一个点在轨迹中的索引
            boundaries.append(str(idx))  # 将索引转为字符串并添加
    return boundaries  # 返回边界索引列表


# ═══════════════════════════════════════════════════════════
# 特征提取（给 ML 层用）
# ═══════════════════════════════════════════════════════════

FEATURE_NAMES = [  # 特征向量名称列表（28维）
    # 数值特征（16维）
    "original_point_count",  # 原始轨迹点数
    "annotation_count",  # 标注标记总数
    "critical_annotation_count",  # 严重标记数
    "warning_annotation_count",  # 警告标记数
    "info_annotation_count",  # 信息标记数
    "avg_annotation_confidence",  # 平均置信度
    "max_annotation_confidence",  # 最高置信度
    "min_annotation_confidence",  # 最低置信度
    "total_duration_minutes",  # 总时长（分钟）
    "speed_variance",  # 速度方差
    "avg_speed",  # 平均速度
    "max_speed",  # 最高速度
    "min_speed",  # 最低速度
    "course_change_count",  # 航向变化次数
    "ais_gap_count",  # AIS中断次数
    "position_jump_count",  # 位置跳变次数
    # 扩展特征
    "rule_hit_count",  # 规则命中数
    "event_count",  # 事件数
    "overlap_annotation_count",  # 重叠标记数
    "segment_count",  # 分段数
    "quality_issue_count",  # 质量问题数
    "duplicate_point_count",  # 重复点数
    # 碰撞风险特征（6维）
    "collision_risk_count",  # 碰撞风险事件数
    "max_cri",  # 最高碰撞风险指数
    "min_dcpa_nm",  # 最小DCPA（海里）
    "min_tcpa_minutes",  # 最小TCPA（分钟，正值表示未来）
    "avg_relative_speed",  # 平均相对速度（节）
    "nearby_ship_count",  # 邻近船数量
]


def _extract_features(  # 从管线输出提取ML特征向量
    ship: ShipProfile,  # 船舶概要
    prepared_points: list[AISPoint],  # 预处理后的轨迹点
    segments: list[TrajectorySegment],  # 轨迹段列表
    events: list[CandidateEvent],  # 候选事件列表
    matched_rules_map: dict[str, list[MatchedRule]],  # event_id -> 匹配规则列表
    quality_report: Optional[QualityReport],  # 质量报告（可选）
    annotations: list[AnnotationTag],  # 标注标记列表
) -> tuple[list[float], dict[str, Any]]:
    """从管线输出提取 ML 特征向量。

    Returns:
        (vector, metadata): vector 是定长浮点数组，metadata 是特征名→值映射
    """
    speeds = [p.speed for p in prepared_points if p.speed is not None]  # 提取所有非空速度值
    duration_minutes = 0  # 轨迹总时长（分钟）
    if len(prepared_points) >= 2:  # 如果有至少两个点
        duration_minutes = (  # 计算时间差（分钟）
            (prepared_points[-1].timestamp - prepared_points[0].timestamp)  # 最后一个点时间 - 第一个点时间
            .total_seconds() / 60  # 转为分钟
        )

    n_critical = sum(1 for a in annotations if a.severity == TagSeverity.CRITICAL)  # 严重标记数
    n_warning = sum(1 for a in annotations if a.severity == TagSeverity.WARNING)  # 警告标记数
    n_info = sum(1 for a in annotations if a.severity == TagSeverity.INFO)  # 信息标记数
    confs = [a.confidence for a in annotations]  # 所有标记的置信度列表
    avg_conf = sum(confs) / len(confs) if confs else 0.0  # 平均置信度

    # 航向变化次数
    course_changes = 0  # 航向变化计数器
    for prev, curr in zip(prepared_points, prepared_points[1:]):  # 相邻点配对遍历
        if prev.course is not None and curr.course is not None:  # 两个点都有航向值
            diff = abs(curr.course - prev.course)  # 航向差绝对值
            if diff > 180:  # 如果航向差>180度（跨越正北方向）
                diff = 360 - diff  # 用补角
            if diff > 30:  # 如果航向变化>30度
                course_changes += 1  # 计数加1

    # 事件类型去重计数
    event_types = set(e.candidate_type.value for e in events)  # 事件类型的去重集合
    total_rules = sum(len(rules) for rules in matched_rules_map.values())  # 规则命中总数

    # 重叠标记数：同一个点有多个事件标记
    tagged_points = [p for p in prepared_points if p.tags]  # 有标记的点列表
    overlap_points = sum(1 for p in tagged_points if len(p.tags) > 1)  # 有多个标记的点数

    quality_issue_count = len(quality_report.issues) if quality_report else 0  # 质量报告中的问题数
    dup_count = quality_report.duplicate_points if quality_report else 0  # 重复点数

    # 碰撞风险特征
    collision_events = [e for e in events if e.candidate_type.value == "collision_risk"]
    collision_risk_count = len(collision_events)
    max_cri = max(
        (e.features.get("cri", 0) for e in collision_events),
        default=0.0,
    )
    min_dcpa = min(
        (e.features.get("dcpa_nm", float("inf")) for e in collision_events),
        default=0.0,
    )
    min_tcpa = min(
        (e.features.get("tcpa_minutes", float("inf")) for e in collision_events if e.features.get("tcpa_minutes", 0) > 0),
        default=0.0,
    )
    avg_relative_speed = (
        sum(e.features.get("relative_speed", 0) for e in collision_events) / collision_risk_count
        if collision_risk_count > 0
        else 0.0
    )
    nearby_ship_count = len({e.features.get("target_mmsi") for e in collision_events if e.features.get("target_mmsi")})

    metadata: dict[str, Any] = {  # 特征元数据
        "original_point_count": len(prepared_points),  # 原始轨迹点数
        "annotation_count": len(annotations),  # 标注标记总数
        "critical_annotation_count": n_critical,  # 严重标记数
        "warning_annotation_count": n_warning,  # 警告标记数
        "info_annotation_count": n_info,  # 信息标记数
        "avg_annotation_confidence": round(avg_conf, 4),  # 平均置信度（4位小数）
        "max_annotation_confidence": round(max(confs), 4) if confs else 0,  # 最高置信度
        "min_annotation_confidence": round(min(confs), 4) if confs else 0,  # 最低置信度
        "total_duration_minutes": round(duration_minutes, 2),  # 总时长（2位小数）
        "speed_variance": round(_variance(speeds), 4) if len(speeds) > 1 else 0,  # 速度方差
        "avg_speed": round(sum(speeds) / len(speeds), 4) if speeds else 0,  # 平均速度
        "max_speed": round(max(speeds), 2) if speeds else 0,  # 最高速度
        "min_speed": round(min(speeds), 2) if speeds else 0,  # 最低速度
        "course_change_count": course_changes,  # 航向变化次数
        "ais_gap_count": _annotation_count_by_type(annotations, "ais_gap"),  # AIS中断标记数
        "position_jump_count": _annotation_count_by_type(annotations, "position_jump"),  # 位置跳变标记数
        "rule_hit_count": total_rules,  # 规则命中总数
        "event_count": len(events),  # 事件总数
        "overlap_annotation_count": overlap_points,  # 重叠标记点数
        "segment_count": len(segments),  # 分段数
        "quality_issue_count": quality_issue_count,  # 质量问题数
        "duplicate_point_count": dup_count,  # 重复点数
        "collision_risk_count": collision_risk_count,  # 碰撞风险事件数
        "max_cri": round(max_cri, 4),  # 最高CRI
        "min_dcpa_nm": round(min_dcpa, 4),  # 最小DCPA
        "min_tcpa_minutes": round(min_tcpa, 2),  # 最小TCPA
        "avg_relative_speed": round(avg_relative_speed, 4),  # 平均相对速度
        "nearby_ship_count": nearby_ship_count,  # 邻近船数量
        # 各检测器产出的行为违规计数（与碰撞风险并列，供证据面板展开）。
        # 前三个是单船/区域检测器，后两个是多船检测器（colreg/towing）。
        # 注意：这几个**不在 FEATURE_NAMES 里**，所以只进 metadata、不进特征向量
        # ——加它们不会改变向量维度（改了维度，新旧记录的 feature_vector 就不可比了）。
        "overtaking_count": _annotation_count_by_type(annotations, "overtaking_violation"),
        "wrong_way_count": _annotation_count_by_type(annotations, "wrong_way_navigation"),
        "anchoring_out_of_bounds_count": _annotation_count_by_type(annotations, "anchoring_out_of_bounds"),
        "give_way_count": _annotation_count_by_type(annotations, "give_way_failure"),
        "illegal_towing_count": _annotation_count_by_type(annotations, "illegal_towing"),
    }

    vector = [float(metadata.get(name, 0)) for name in FEATURE_NAMES]  # 按特征名顺序构建浮点向量
    return vector, metadata  # 返回特征向量和元数据


def _annotation_count_by_type(annotations: list[AnnotationTag], type_name: str) -> int:  # 按类型统计标记数量
    return sum(1 for a in annotations if a.type == type_name)  # 过滤并计数


def _variance(values: list[float]) -> float:  # 计算样本方差
    if len(values) < 2:  # 如果数据不足2个
        return 0.0  # 返回0
    mean = sum(values) / len(values)  # 计算均值
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)  # 样本方差公式（除n-1）