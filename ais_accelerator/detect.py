"""
ais_accelerator/detect.py — 候选事件检测器
=============================================

【这个文件做什么？】
  对轨迹段进行 6 种类型的"候选违规事件"检测：
  1. RESTRICTED_AREA_ENTRY — 进入禁航/限制区域
  2. SPEED_VIOLATION — 区域超速（超过该区域限速）
  3. LOW_SPEED_FISHING — 疑似低速捕捞（极低速 + 在禁渔区）
  4. AIS_GAP — AIS 信号中断（质量事件）
  5. POSITION_JUMP — 位置跳变（质量事件）
  6. COURSE_ANOMALY — 航向突变（突然大角度转向）

【设计原则】
  - Recall-first：阈值偏低宁可误报，不漏掉真违规
  - 纯规则驱动：不使用任何 ML 模型，所有规则可解释
  - 逐段独立：每个轨迹段单独检测，段间不影响

【关键算法】
  - _contiguous_runs(): 找到连续满足条件的点区间
  - season_is_active(): 判断季节性规则（如禁渔期）是否生效
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import hashlib  # 哈希计算模块
from statistics import median  # 中位数计算
from typing import Callable, Iterable, Optional  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .geometry import circular_angle_diff  # 航向角度差计算
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    CandidateEvent,  # 候选事件
    CandidateType,  # 候选事件类型
    QualityIssue,  # 质量问题
    TrajectorySegment,  # 轨迹分段
)
from .rules import RuleProvider, season_is_active  # 规则提供器和季节性规则判断


def _event_id(  # 生成事件的唯一ID
    mmsi: str,  # 船舶MMSI
    candidate_type: CandidateType,  # 候选事件类型
    first: AISPoint,  # 起始点
    last: AISPoint,  # 结束点
    zone_id: Optional[str] = None,  # 区域ID（可选）
) -> str:
    raw = "|".join(  # 拼接原始字符串
        [
            mmsi,  # MMSI
            candidate_type.value,  # 事件类型
            first.timestamp.isoformat(),  # 起始时间
            last.timestamp.isoformat(),  # 结束时间
            zone_id or "",  # 区域ID（空字符串代替None）
        ]
    )
    return f"EVT-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:14]}"  # SHA1哈希前14位作为ID


def _contiguous_runs(  # 查找连续满足条件的点区间
    points: list[AISPoint], predicate: Callable[[AISPoint], bool]  # AIS点列表, 条件判断函数
) -> list[list[AISPoint]]:
    runs: list[list[AISPoint]] = []  # 连续区间列表
    current: list[AISPoint] = []  # 当前正在累积的区间
    for point in points:  # 遍历每个点
        if predicate(point):  # 如果满足条件
            current.append(point)  # 加入当前区间
        elif current:  # 如果不满足且当前区间非空
            runs.append(current)  # 保存当前区间
            current = []  # 开始新的区间
    if current:  # 循环结束后，如果还有未保存的区间
        runs.append(current)  # 保存最后一个区间
    return runs  # 返回所有连续区间


def _issues_for_points(  # 获取与指定点列表时间范围相关的质量问题
    issues: Iterable[QualityIssue], points: list[AISPoint]  # 质量问题列表, AIS点列表
) -> list[QualityIssue]:
    if not points:  # 如果点列表为空
        return []  # 返回空列表
    start, end = points[0].timestamp, points[-1].timestamp  # 起点和终点的时间戳
    return [  # 返回时间范围内的质量问题
        issue  # 质量问题
        for issue in issues  # 遍历所有问题
        if issue.timestamp is None or start <= issue.timestamp <= end  # 无时间戳或在范围内
    ]


def detect_candidates(  # 检测轨迹段中的所有候选事件
    segment: TrajectorySegment,  # 轨迹分段
    rules: RuleProvider,  # 规则提供器
    config: AcceleratorConfig,  # 加速器配置
) -> list[CandidateEvent]:
    if not segment.points:  # 如果分段没有点
        return []  # 返回空列表
    events: list[CandidateEvent] = []  # 候选事件列表
    events.extend(_detect_zone_events(segment, rules, config))  # 检测区域相关事件
    events.extend(_detect_quality_events(segment))  # 检测质量相关事件
    events.extend(_detect_course_events(segment, config))  # 检测航向异常事件
    return events  # 返回所有检测到的事件


def _detect_zone_events(  # 检测区域相关事件（禁航、超速、捕捞）
    segment: TrajectorySegment,  # 轨迹分段
    rules: RuleProvider,  # 规则提供器
    config: AcceleratorConfig,  # 加速器配置
) -> list[CandidateEvent]:
    events: list[CandidateEvent] = []  # 候选事件列表
    zones = {  # 收集轨迹涉及的所有区域（按ID去重）
        str(zone.get("id", zone.get("name"))): zone  # 区域ID → 区域信息
        for point in segment.points  # 遍历每个轨迹点
        for zone in rules.zones_at(point.lat, point.lon)  # 查询点所在的区域
    }
    for zone_id, zone in zones.items():  # 遍历每个区域
        zone_name = zone.get("name", zone_id)  # 区域名称
        restrictions = zone.get("restrictions", {})  # 区域限制条件
        in_zone = lambda point: any(  # 判断点是否在该区域内
            str(item.get("id", item.get("name"))) == zone_id  # 区域ID匹配
            for item in rules.zones_at(point.lat, point.lon)  # 查询点所在的区域
        )
        zone_points = [point for point in segment.points if in_zone(point)]  # 区域内所有点
        if not zone_points:  # 如果没有点在区域内
            continue  # 跳过该区域

        if restrictions.get("entry_allowed") is False:  # 如果该区域禁止进入
            first, last = zone_points[0], zone_points[-1]  # 第一个和最后一个进入的点
            event_id = _event_id(  # 生成事件ID
                segment.mmsi,  # MMSI
                CandidateType.RESTRICTED_AREA_ENTRY,  # 事件类型
                first,  # 起始点
                last,  # 结束点
                zone_id,  # 区域ID
            )
            events.append(  # 添加禁航区进入事件
                CandidateEvent(
                    event_id=event_id,  # 事件ID
                    source_event_ids=[event_id],  # 来源事件ID
                    mmsi=segment.mmsi,  # MMSI
                    candidate_type=CandidateType.RESTRICTED_AREA_ENTRY,  # 候选事件类型
                    start_time=first.timestamp,  # 开始时间
                    end_time=last.timestamp,  # 结束时间
                    zone_id=zone_id,  # 区域ID
                    zone_name=zone_name,  # 区域名称
                    priority="high",  # 优先级（高）
                    features={"entry_allowed": False, "point_count": len(zone_points)},  # 特征
                    evidence_points=[first, last] if first != last else [first],  # 证据点
                    quality_issues=_issues_for_points(segment.quality_issues, zone_points),  # 质量问题
                )
            )

        speed_limit = restrictions.get("max_speed_knots")  # 区域限速
        if speed_limit is not None:  # 如果有限速
            threshold = float(speed_limit) * config.speed_candidate_margin  # 候选阈值（略低于限速）
            for run in _contiguous_runs(  # 查找超速连续区间
                segment.points,  # 轨迹点
                lambda point: in_zone(point) and point.speed >= threshold,  # 在区域内且速度超过阈值
            ):
                first, last = run[0], run[-1]  # 区间起始点和结束点
                event_id = _event_id(  # 生成事件ID
                    segment.mmsi,  # MMSI
                    CandidateType.SPEED_VIOLATION,  # 事件类型
                    first,  # 起始点
                    last,  # 结束点
                    zone_id,  # 区域ID
                )
                max_speed = max(point.speed for point in run)  # 区间内最高速度
                events.append(  # 添加超速事件
                    CandidateEvent(
                        event_id=event_id,  # 事件ID
                        source_event_ids=[event_id],  # 来源事件ID
                        mmsi=segment.mmsi,  # MMSI
                        candidate_type=CandidateType.SPEED_VIOLATION,  # 候选事件类型
                        start_time=first.timestamp,  # 开始时间
                        end_time=last.timestamp,  # 结束时间
                        zone_id=zone_id,  # 区域ID
                        zone_name=zone_name,  # 区域名称
                        priority="high" if max_speed > float(speed_limit) else "middle",  # 超过限速为高，否则为中
                        features={  # 特征
                            "speed_limit": float(speed_limit),  # 区域限速
                            "candidate_threshold": round(threshold, 3),  # 候选阈值
                            "max_speed": max_speed,  # 最高速度
                            "average_speed": sum(point.speed for point in run) / len(run),  # 平均速度
                            "point_count": len(run),  # 点数
                        },
                        evidence_points=run,  # 证据点
                        quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
                    )
                )

        if restrictions.get("fishing_allowed") is False:  # 如果该区域禁止捕鱼
            time_rules = zone.get("time_rules", [])  # 区域的时间规则（如禁渔期）
            for run in _contiguous_runs(  # 查找低速连续区间
                segment.points,  # 轨迹点
                lambda point: (  # 条件：在区域内、低速、且不在季节性禁渔期外
                    in_zone(point)  # 在区域内
                    and point.speed <= config.low_speed_enter_knots  # 速度低于进入阈值
                    and (  # 时间规则检查
                        not time_rules  # 无时间规则
                        or any(season_is_active(rule, point.timestamp) for rule in time_rules)  # 或当前在有效期内
                    )
                ),
            ):
                duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60  # 持续时长（分钟）
                if len(run) < 2 or duration < config.low_speed_min_minutes:  # 点数太少或时间太短
                    continue  # 跳过
                earlier = [point.speed for point in segment.points if point.timestamp < run[0].timestamp]  # 事件前的速度列表
                prior_speed = median(earlier) if earlier else None  # 事件前的速度中位数
                event_id = _event_id(  # 生成事件ID
                    segment.mmsi,  # MMSI
                    CandidateType.LOW_SPEED_FISHING,  # 事件类型
                    run[0],  # 起始点
                    run[-1],  # 结束点
                    zone_id,  # 区域ID
                )
                events.append(  # 添加低速捕捞事件
                    CandidateEvent(
                        event_id=event_id,  # 事件ID
                        source_event_ids=[event_id],  # 来源事件ID
                        mmsi=segment.mmsi,  # MMSI
                        candidate_type=CandidateType.LOW_SPEED_FISHING,  # 候选事件类型
                        start_time=run[0].timestamp,  # 开始时间
                        end_time=run[-1].timestamp,  # 结束时间
                        zone_id=zone_id,  # 区域ID
                        zone_name=zone_name,  # 区域名称
                        priority="high"  # 优先级
                        if prior_speed is not None  # 如果事件前速度已知
                        and prior_speed >= config.prior_normal_speed_knots  # 且之前正常速度行驶
                        else "middle",  # 否则为中优先级
                        features={  # 特征
                            "duration_minutes": round(duration, 2),  # 持续分钟数
                            "minimum_speed": min(point.speed for point in run),  # 最低速度
                            "average_speed": sum(point.speed for point in run) / len(run),  # 平均速度
                            "prior_median_speed": prior_speed,  # 事件前速度中位数
                            "point_count": len(run),  # 点数
                        },
                        evidence_points=run,  # 证据点
                        quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
                    )
                )
    return events  # 返回所有区域相关事件


def _detect_quality_events(segment: TrajectorySegment) -> list[CandidateEvent]:  # 检测质量相关事件（AIS中断、位置跳变）
    events: list[CandidateEvent] = []  # 候选事件列表
    for index, point in enumerate(segment.points):  # 遍历每个轨迹点
        for flag, candidate_type in (  # 质量标记与事件类型的对应关系
            ("ais_gap", CandidateType.AIS_GAP),  # AIS信号中断
            ("position_jump", CandidateType.POSITION_JUMP),  # 位置跳变
        ):
            if flag not in point.quality_flags:  # 如果点没有该质量标记
                continue  # 跳过
            evidence = segment.points[max(0, index - 1) : index + 1]  # 当前点及其前一点作为证据
            first, last = evidence[0], evidence[-1]  # 起始点和结束点
            event_id = _event_id(segment.mmsi, candidate_type, first, last)  # 生成事件ID
            matching_issues = [  # 匹配时间戳的质量问题
                issue  # 质量问题
                for issue in segment.quality_issues  # 遍历质量问题
                if issue.code == flag and issue.timestamp == point.timestamp  # 代码和时间匹配
            ]
            events.append(  # 添加质量事件
                CandidateEvent(
                    event_id=event_id,  # 事件ID
                    source_event_ids=[event_id],  # 来源事件ID
                    mmsi=segment.mmsi,  # MMSI
                    candidate_type=candidate_type,  # 候选事件类型
                    start_time=first.timestamp,  # 开始时间
                    end_time=last.timestamp,  # 结束时间
                    priority="middle",  # 中优先级
                    features=(matching_issues[0].details if matching_issues else {}),  # 详细特征
                    evidence_points=evidence,  # 证据点
                    quality_issues=matching_issues,  # 质量问题
                )
            )
    return events  # 返回所有质量事件


def _detect_course_events(  # 检测航向异常事件
    segment: TrajectorySegment, config: AcceleratorConfig  # 轨迹分段, 加速器配置
) -> list[CandidateEvent]:
    events: list[CandidateEvent] = []  # 候选事件列表
    for previous, current in zip(segment.points, segment.points[1:]):  # 遍历相邻点对
        elapsed_minutes = (current.timestamp - previous.timestamp).total_seconds() / 60  # 时间间隔（分钟）
        if elapsed_minutes <= 0:  # 如果时间未递增
            continue  # 跳过
        difference = circular_angle_diff(previous.course, current.course)  # 航向差（度）
        rate = difference / elapsed_minutes  # 航向变化速率（度/分钟）
        if (  # 如果航向变化小于阈值或变化速率小于阈值
            difference <= config.course_change_degrees  # 航向变化未超过阈值
            or rate <= config.course_rate_degrees_per_minute  # 变化速率未超过阈值
        ):
            continue  # 跳过
        event_id = _event_id(  # 生成事件ID
            segment.mmsi, CandidateType.COURSE_ANOMALY, previous, current  # MMSI, 事件类型, 前后点
        )
        events.append(  # 添加航向异常事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.COURSE_ANOMALY,  # 候选事件类型
                start_time=previous.timestamp,  # 开始时间
                end_time=current.timestamp,  # 结束时间
                priority="middle",  # 中优先级
                features={  # 特征
                    "course_change_degrees": round(difference, 2),  # 航向变化（度）
                    "course_rate_degrees_per_minute": round(rate, 2),  # 变化速率（度/分钟）
                    "elapsed_minutes": round(elapsed_minutes, 3),  # 时间间隔（分钟）
                },
                evidence_points=[previous, current],  # 证据点
                quality_issues=_issues_for_points(  # 质量问题
                    segment.quality_issues, [previous, current]  # 轨迹质量问题列表, 当前点对
                ),
            )
        )
    return events  # 返回所有航向异常事件