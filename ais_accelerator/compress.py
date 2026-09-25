"""
ais_accelerator/compress.py — 轨迹压缩（事件感知）
=====================================================

【这个文件做什么？】
  对每条候选事件，从整条轨迹中挑选最有信息量的几个历史点。
  目标：用极少量（默认 5 个）轨迹点就能让 LLM 做判断。

【选择策略 —— 优先级排序】
  1. (最高) pre_event: 事件开始前的最后一个点
  2. event_start / event_end: 事件的起始点和结束点
  3. maximum_speed: 超速事件中的最高速度点
  4. minimum_speed: 捕捞事件中的最低速度点
  5. maximum_course_change: 航向变化最大的点（转弯点）
  6. (最低) rdp_shape: 用 RDP 算法选取保持轨迹形状的点

【RDP 算法】
  Ramer-Douglas-Peucker 折线简化算法：
  递归地保留"离直线最远的点"，直到所有点到简化线的距离小于阈值。
  此处用二分搜索找最佳 epsilon，使选点数精确达到目标值。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import json  # JSON序列化模块

from .config import AcceleratorConfig  # 加速器配置
from .geometry import circular_angle_diff, rdp_indices  # 几何计算函数
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    CandidateEvent,  # 候选事件
    CandidateType,  # 候选事件类型
    CompressionMetadata,  # 压缩元数据
    TrajectorySegment,  # 轨迹分段
)


def _point_key(point: AISPoint) -> tuple:  # 生成点的唯一键（用于去重）
    return (point.timestamp.isoformat(), round(point.lat, 7), round(point.lon, 7))  # 时间戳+经纬度


def _latest_at_or_before(  # 获取事件结束时间之前的最后一个点
    points: list[AISPoint], event: CandidateEvent  # AIS点列表, 候选事件
) -> AISPoint:
    candidates = [point for point in points if point.timestamp <= event.end_time]  # 筛选出事件结束前的点
    if candidates:  # 如果有符合条件的点
        return candidates[-1]  # 返回最后一个（时间最晚的）
    return points[-1]  # 否则返回轨迹的最后一个点


def _rdp_fill_indices(points: list[AISPoint], target: int) -> list[int]:  # 用RDP算法选取指定数量的索引
    if target <= 0 or not points:  # 目标数<=0或点列表为空
        return []  # 返回空列表
    if len(points) <= target:  # 如果点数不超过目标数
        return list(range(len(points)))  # 返回所有点的索引
    coordinates = [(point.lon, point.lat) for point in points]  # 提取经纬度坐标
    low, high = 0.0, 2.0  # 二分搜索的边界（epsilon最小值、最大值）
    best = [0, len(points) - 1]  # 最佳索引列表（初始为首尾两点）
    for _ in range(24):  # 二分搜索迭代24次以确定最佳epsilon
        epsilon = (low + high) / 2  # 当前epsilon值
        indices = rdp_indices(coordinates, epsilon)  # 用RDP算法获取保留点的索引
        if len(indices) > target:  # 如果保留点数多于目标
            low = epsilon  # 增加epsilon（减少保留点）
        else:  # 保留点数不超过目标
            best = indices  # 更新最佳结果
            high = epsilon  # 减小epsilon（增加保留点）
    if len(best) < target:  # 如果最佳结果仍少于目标数
        available = [index for index in range(len(points)) if index not in best]  # 未被选中的索引
        step = len(available) / max(1, target - len(best))  # 均匀采样步长
        best.extend(  # 从可用索引中均匀选取补齐
            available[min(int(offset * step), len(available) - 1)]  # 计算要添加的索引
            for offset in range(target - len(best))  # 需要补充的数量
            if available  # 如果还有可用索引
        )
    return sorted(set(best))[:target]  # 去重排序后返回，不超过目标数


def _bounded_rdp_source(  # 对超长轨迹进行均匀降采样（限制RDP输入规模）
    points: list[AISPoint], limit: int = 512  # AIS点列表, 最大输入点数量
) -> list[AISPoint]:
    """Bound RDP cost while retaining the full pre-event time span."""
    if len(points) <= limit:  # 如果点数不超过限制
        return points  # 直接返回
    step = (len(points) - 1) / (limit - 1)  # 计算采样步长
    indices = sorted({int(round(index * step)) for index in range(limit)})  # 均匀采样的索引
    return [points[index] for index in indices]  # 返回采样后的点列表


def compress_for_event(  # 为单个候选事件压缩轨迹
    event: CandidateEvent,  # 候选事件
    track: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
) -> tuple[list[AISPoint], AISPoint, CompressionMetadata]:
    all_points = track.points  # 轨迹中的所有点
    if not all_points:  # 如果轨迹为空
        raise ValueError("cannot compress an empty trajectory")  # 抛出值错误
    current = _latest_at_or_before(all_points, event)  # 事件结束前的最后一个点（当前点）
    # The existing trajectory agent interprets ``history`` as observations
    # preceding ``current_position``.  Never leak future points into an event.
    points = [point for point in all_points if point.timestamp <= current.timestamp]  # 仅保留当前点之前的点（防止未来信息泄漏）
    current_key = _point_key(current)  # 当前点的唯一键

    selected: dict[tuple, tuple[AISPoint, str, int]] = {}  # 已选中的点字典（键→（点, 原因, 优先级））

    def add(point: AISPoint, reason: str, priority: int) -> None:  # 添加一个点到选中列表
        key = _point_key(point)  # 点的唯一键
        if key == current_key:  # 如果是当前点（已单独保留）
            return  # 跳过
        existing = selected.get(key)  # 查找是否已选中
        if existing is None or priority > existing[2]:  # 未选中或优先级更高
            selected[key] = (point, reason, priority)  # 添加或替换

    before = [point for point in points if point.timestamp < event.start_time]  # 事件开始前的点列表
    if before:  # 如果有事件前的点
        add(before[-1], "pre_event", 100)  # 添加事件开始前的最后一个点（最高优先100）
    if event.evidence_points:  # 如果有证据点
        add(event.evidence_points[0], "event_start", 95)  # 事件的起始点（优先95）
        add(event.evidence_points[-1], "event_end", 90)  # 事件的结束点（优先90）
        if event.candidate_type == CandidateType.SPEED_VIOLATION:  # 如果是超速事件
            add(max(event.evidence_points, key=lambda point: point.speed), "maximum_speed", 98)  # 添加最高速度点（优先98）
        elif event.candidate_type == CandidateType.LOW_SPEED_FISHING:  # 如果是低速捕捞事件
            add(min(event.evidence_points, key=lambda point: point.speed), "minimum_speed", 98)  # 添加最低速度点（优先98）

    if len(points) >= 2:  # 如果至少有两个点
        turn_point = max(  # 找航向变化最大的点
            zip(points, points[1:]),  # 相邻点对
            key=lambda pair: circular_angle_diff(pair[0].course, pair[1].course),  # 按航向差排序
        )[1]  # 取第二个点（变化后的点）
        add(turn_point, "maximum_course_change", 80)  # 添加航向变化最大点（优先80）

    target = config.max_history_points  # 目标历史点数
    if len(selected) < target:  # 如果已选点数不足目标
        rdp_points = _bounded_rdp_source(points)  # 对超长轨迹进行降采样
        for index in _rdp_fill_indices(rdp_points, target + 1):  # 用RDP算法选取补充点
            add(rdp_points[index], "rdp_shape", 10)  # 添加形状保持点（最低优先10）

    chosen = sorted(  # 按优先级降序、时间升序排列
        selected.values(), key=lambda value: (-value[2], value[0].timestamp)  # 先排优先级，后排时间
    )[:target]  # 取前target个
    chosen.sort(key=lambda value: value[0].timestamp)  # 按时间升序排列最终结果
    history = [value[0] for value in chosen]  # 提取历史点列表
    reasons = [value[1] for value in chosen]  # 提取选中原因列表
    serialized = {  # 构建序列化输入
        "current_position": current.to_agent_position(),  # 当前点位置
        "history": [point.to_agent_position() for point in history],  # 历史点位置列表
    }
    metadata = CompressionMetadata(  # 创建压缩元数据
        original_points=len(all_points),  # 原始点数
        retained_history_points=len(history),  # 保留的历史点数
        retained_total_points=len(history) + 1,  # 保留的总点数（含当前点）
        compression_ratio=round(  # 压缩率
            1 - (len(history) + 1) / max(1, len(all_points)), 6  # 1 - (压缩后/原始)
        ),
        selected_reasons=reasons,  # 选中原因列表
        estimated_input_characters=len(  # 估算的输入字符数
            json.dumps(serialized, ensure_ascii=False, default=str)  # JSON序列化
        ),
    )
    return history, current, metadata  # 返回历史点列表、当前点、压缩元数据