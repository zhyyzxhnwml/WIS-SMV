"""
ais_accelerator/geometry.py — 空间几何工具函数
=================================================

【这个文件做什么？】
  提供所有空间计算工具函数，零外部依赖。

【函数清单】
  - haversine_nm(): Haversine 公式计算两点间球面距离（海里）
  - circular_angle_diff(): 计算两个角度的最小差值（处理 0°/360° 环绕）
  - point_in_polygon(): 射线法判断点是否在多边形内
  - perpendicular_distance(): 点到线段的最短垂直距离（RDP 算法核心）
  - rdp_indices(): Ramer-Douglas-Peucker 折线简化算法

【Haversine 公式】
  计算地球表面两点间的球面距离，精度约 0.5%，适合航海应用。
  地球半径取 3440.065 海里（1 海里 ≈ 1.852 公里）。

【射线法】
  从目标点向右画水平射线，统计与多边形边的交点个数。
  奇数 → 点在多边形内；偶数 → 点在多边形外。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import math  # 数学函数库
from typing import Sequence, Tuple  # 类型注解


EARTH_RADIUS_NM = 3440.065  # 地球半径（海里）


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:  # Haversine公式计算球面距离，返回海里数
    lat1_r, lon1_r, lat2_r, lon2_r = map(  # 将经纬度从角度转为弧度
        math.radians, (lat1, lon1, lat2, lon2)  # 四个角度值转为弧度
    )
    dlat = lat2_r - lat1_r  # 纬度差（弧度）
    dlon = lon2_r - lon1_r  # 经度差（弧度）
    a = (  # Haversine公式中间值
        math.sin(dlat / 2) ** 2  # sin(dlat/2)^2
        + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlon / 2) ** 2  # cos(lat1)*cos(lat2)*sin(dlon/2)^2
    )
    return 2 * EARTH_RADIUS_NM * math.asin(min(1.0, math.sqrt(a)))  # 返回两点间距离（海里）


def circular_angle_diff(first: float, second: float) -> float:  # 计算两个角度的最小差值（度）
    return abs((second - first + 180.0) % 360.0 - 180.0)  # 返回[0,180]范围内的角度差


def point_in_polygon(  # 射线法判断点是否在多边形内
    lon: float, lat: float, boundary: Sequence[Sequence[float]]  # 经度, 纬度, 多边形边界点列表
) -> bool:
    """Ray-casting point-in-polygon check; boundary uses [lon, lat]."""
    if len(boundary) < 3:  # 多边形边数少于3
        return False  # 不能构成多边形，返回False
    inside = False  # 点是否在多边形内部标记
    for index, first in enumerate(boundary):  # 遍历多边形每条边
        second = boundary[(index + 1) % len(boundary)]  # 当前边的终点（下一个顶点）
        x1, y1 = float(first[0]), float(first[1])  # 当前边起点经度、纬度
        x2, y2 = float(second[0]), float(second[1])  # 当前边终点经度、纬度
        if (y1 > lat) != (y2 > lat):  # 如果边的两端分别在目标点的上下两侧
            x_intersection = x1 + (lat - y1) * (x2 - x1) / (y2 - y1)  # 计算射线与边的交点经度
            if lon < x_intersection:  # 如果目标点在交点左侧
                inside = not inside  # 翻转内外标记（奇数次在内部）
    return inside  # 返回判断结果


def perpendicular_distance(  # 计算点到线段的最短垂直距离
    point: Tuple[float, float],  # 点坐标
    start: Tuple[float, float],  # 线段起点坐标
    end: Tuple[float, float],  # 线段终点坐标
) -> float:
    if start == end:  # 如果线段退化为点
        return math.hypot(point[0] - start[0], point[1] - start[1])  # 返回点到点的欧几里得距离
    dx = end[0] - start[0]  # 线段在x方向上的分量
    dy = end[1] - start[1]  # 线段在y方向上的分量
    numerator = abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0])  # 叉积的绝对值（两倍三角形面积）
    return numerator / math.hypot(dx, dy)  # 除以底边长得到垂直距离


def rdp_indices(points: Sequence[Tuple[float, float]], epsilon: float) -> list[int]:  # Ramer-Douglas-Peucker折线简化算法，返回保留点的索引
    """Return indices retained by Ramer-Douglas-Peucker."""
    if len(points) <= 2:  # 如果点数不超过2
        return list(range(len(points)))  # 返回所有点的索引
    max_distance = -1.0  # 最大垂直距离初始值
    split_index = 0  # 最大距离对应的分割点索引
    for index in range(1, len(points) - 1):  # 遍历除首尾外的中间点
        distance = perpendicular_distance(points[index], points[0], points[-1])  # 计算当前点到首尾连线的垂直距离
        if distance > max_distance:  # 如果距离大于当前最大值
            max_distance = distance  # 更新最大距离
            split_index = index  # 更新分割点索引
    if max_distance > epsilon:  # 如果最大距离超过阈值
        left = rdp_indices(points[: split_index + 1], epsilon)  # 递归处理左半段
        right = rdp_indices(points[split_index:], epsilon)  # 递归处理右半段
        return left[:-1] + [split_index + value for value in right]  # 合并左右结果（去重分割点）
    return [0, len(points) - 1]  # 距离不足阈值，只保留首尾两点