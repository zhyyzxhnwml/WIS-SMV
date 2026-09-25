"""
ais_accelerator/segment.py — 轨迹分段
=======================================

【这个文件做什么？】
  将一条船连续的位置序列切分成多个"轨迹段"（TrajectorySegment）。
  分段依据：
  1. AIS 信号间隔 > 30 分钟 → 说明船可能关机/靠港/出了信号盲区
  2. 轨迹总时长 > 120 分钟 → 避免一整段太长
  3. 进入/离开管制区域 → 不同区域适用不同规则

【为什么需要分段？】
  一条船可能在外面跑几天，中间可能多次进出不同管制区域。
  不分段的话，事件检测和规则匹配会混在一起，无法准确判定。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import hashlib  # 哈希计算模块
from typing import Iterable  # 可迭代对象类型

from .config import AcceleratorConfig  # 加速器配置
from .models import AISPoint, QualityReport, TrajectorySegment  # 数据模型
from .rules import RuleProvider  # 规则提供器


def _segment_id(mmsi: str, first: AISPoint, last: AISPoint) -> str:  # 生成轨迹段唯一ID
    raw = f"{mmsi}|{first.timestamp.isoformat()}|{last.timestamp.isoformat()}"  # 原始字符串：MMSI|起始时间|结束时间
    return f"SEG-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"  # 返回以SEG-为前缀的12字符哈希ID


def _zone_ids(point: AISPoint, rules: RuleProvider) -> tuple[str, ...]:  # 获取某AIS点所在的所有区域ID
    return tuple(sorted(str(zone.get("id", zone.get("name"))) for zone in rules.zones_at(point.lat, point.lon)))  # 返回按字母排序的区域ID元组


def segment_track(  # 将船舶轨迹点序列切分为多个轨迹段
    mmsi: str,  # 船舶MMSI标识号
    points: Iterable[AISPoint],  # AIS数据点可迭代序列
    quality_report: QualityReport,  # 质量评估报告
    rules: RuleProvider,  # 规则提供器
    config: AcceleratorConfig,  # 加速器配置
) -> list[TrajectorySegment]:
    ordered = sorted(points, key=lambda point: point.timestamp)  # 按时间戳排序的轨迹点列表
    if not ordered:  # 如果无有效点
        return []  # 返回空列表

    groups: list[list[AISPoint]] = []  # 轨迹段分组列表
    current: list[AISPoint] = [ordered[0]]  # 当前正在构建的轨迹段点列表
    current_zones = _zone_ids(ordered[0], rules)  # 当前段所在区域ID

    for point in ordered[1:]:  # 从第二个点开始遍历
        previous = current[-1]  # 当前段最后一个点
        gap_minutes = (point.timestamp - previous.timestamp).total_seconds() / 60  # 当前点与上一个点的时间间隔（分钟）
        duration_minutes = (point.timestamp - current[0].timestamp).total_seconds() / 60  # 当前段已持续时长（分钟）
        point_zones = _zone_ids(point, rules)  # 当前点所在区域ID
        should_split = (  # 是否应该切分新段
            gap_minutes > config.ais_gap_minutes  # 信号间隔超过阈值
            or duration_minutes > config.max_segment_minutes  # 段时长超过最大限制
            or point_zones != current_zones  # 区域发生变化
        )
        if should_split:  # 如果需要切分
            groups.append(current)  # 将当前段加入分组列表
            current = [point]  # 开始新段
            current_zones = point_zones  # 更新当前段区域
        else:  # 不需要切分
            current.append(point)  # 将点加入当前段
    groups.append(current)  # 将最后一个段加入分组列表

    segments: list[TrajectorySegment] = []  # 结果轨迹段列表
    for group in groups:  # 遍历每个分组
        start, end = group[0].timestamp, group[-1].timestamp  # 段起始时间和结束时间
        issues = [  # 筛选属于该段时间范围内的质量问题
            issue  # 质量问题
            for issue in quality_report.issues  # 遍历所有质量问题
            if issue.timestamp is None or start <= issue.timestamp <= end  # 时间在段范围内或无时间戳
        ]
        zones = sorted(  # 收集该段经过的所有区域ID，去重后排序
            {
                str(zone.get("id", zone.get("name")))  # 区域ID或区域名称
                for point in group  # 遍历段内所有点
                for zone in rules.zones_at(point.lat, point.lon)  # 获取点所在区域
            }
        )
        segments.append(  # 添加轨迹段到结果列表
            TrajectorySegment(
                segment_id=_segment_id(mmsi, group[0], group[-1]),  # 段ID
                mmsi=mmsi,  # 船舶MMSI
                start_time=start,  # 段起始时间
                end_time=end,  # 段结束时间
                points=group,  # 段内数据点
                zone_ids=zones,  # 经过的区域ID
                quality_issues=issues,  # 相关问题
            )
        )
    return segments  # 返回轨迹段列表