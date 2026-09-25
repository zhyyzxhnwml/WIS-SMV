"""
ais_accelerator/predict.py — 轨迹预测模块
============================================

【这个文件做什么？】
  基于历史 AIS 轨迹点，预测船舶未来位置。
  预测结果供碰撞风险检测（collision.py）使用，
  DCPA/TCPA 在预测轨迹上计算，而非仅基于当前点。

【预测方法】
  1. 线性外推（默认）：基于最近 N 个点的速度和航向外推
     - 优点：零依赖、极快、可解释
     - 缺点：不考虑转向/加减速
  2. 恒定转向率外推（可选）：考虑当前转向率
     - 适合弯道场景
  3. iTransformer / PatchTST（预留接口）：未来可替换为时序模型

【输出】
  PredictedTrack: 包含预测点列表、预测方法、置信度

【设计原则】
  - 预测是"可选阶段"：如果预测失败，系统回退到基于当前点的 CRI
  - 预测结果带置信度，随预测步数递减
  - 预测步数可配置（默认 24 步，对应约 30 分钟）
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import AcceleratorConfig
from .geometry import haversine_nm
from .models import AISPoint


# ── 数据结构 ──

@dataclass
class PredictedPoint:
    """一个预测的未来位置点。"""
    timestamp: datetime  # 预测时间
    lat: float  # 预测纬度
    lon: float  # 预测经度
    speed: float  # 预测速度（节）
    course: float  # 预测航向（度）
    confidence: float  # 预测置信度（0~1，随步数递减）
    step: int  # 第几步预测（0 = 当前点）

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat().replace("+00:00", "Z"),
            "lat": round(self.lat, 6),
            "lon": round(self.lon, 6),
            "speed": round(self.speed, 2),
            "course": round(self.course, 1),
            "confidence": round(self.confidence, 3),
            "step": self.step,
        }


@dataclass
class PredictedTrack:
    """一条轨迹的预测结果。"""
    mmsi: str  # 船舶 MMSI
    method: str  # 预测方法名（"linear" / "constant_turn_rate"）
    points: list[PredictedPoint] = field(default_factory=list)  # 预测点列表
    base_point: Optional[PredictedPoint] = None  # 预测起点（当前最后一点）
    step_minutes: float = 1.0  # 每步时间间隔（分钟）
    confidence_decay: float = 0.95  # 每步置信度衰减率

    @property
    def total_steps(self) -> int:
        return len(self.points)

    @property
    def total_minutes(self) -> float:
        return self.total_steps * self.step_minutes

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "method": self.method,
            "step_minutes": self.step_minutes,
            "total_steps": self.total_steps,
            "total_minutes": round(self.total_minutes, 1),
            "base_point": self.base_point.to_dict() if self.base_point else None,
            "points": [p.to_dict() for p in self.points],
        }


# ── 预测器 ──

class TrajectoryPredictor:
    """轨迹预测器。

    用法:
        predictor = TrajectoryPredictor(config)
        predicted = predictor.predict(points, steps=24)
        for pt in predicted.points:
            print(f"  +{pt.step}min: ({pt.lat:.4f}, {pt.lon:.4f}) conf={pt.confidence:.2f}")
    """

    def __init__(self, config: Optional[AcceleratorConfig] = None):
        self.config = config or AcceleratorConfig()
        self.step_minutes = self.config.predict_step_minutes
        self.default_steps = self.config.predict_steps
        self.min_points = 3  # 线性外推最少需要 3 个点
        self.confidence_decay = 0.95  # 每步衰减 5%

    def predict(
        self,
        points: list[AISPoint],
        steps: Optional[int] = None,
        method: str = "auto",
    ) -> Optional[PredictedTrack]:
        """预测轨迹。

        Args:
            points: 历史 AIS 点列表（按时间排序）
            steps: 预测步数（默认从配置读取）
            method: 预测方法（"auto" / "linear" / "constant_turn_rate"）

        Returns:
            PredictedTrack 或 None（点数不足时）
        """
        if not points or len(points) < self.min_points:
            return None

        steps = steps or self.default_steps

        # 自动选择方法
        if method == "auto":
            method = self._select_method(points)

        if method == "constant_turn_rate":
            return self._predict_constant_turn_rate(points, steps)
        return self._predict_linear(points, steps)

    def _select_method(self, points: list[AISPoint]) -> str:
        """根据轨迹特征自动选择预测方法。"""
        if len(points) < 5:
            return "linear"

        # 检查最近几点的航向变化率
        recent = points[-5:]
        course_changes = []
        for prev, curr in zip(recent, recent[1:]):
            diff = abs(curr.course - prev.course)
            if diff > 180:
                diff = 360 - diff
            elapsed = (curr.timestamp - prev.timestamp).total_seconds() / 60
            if elapsed > 0:
                course_changes.append(diff / elapsed)

        avg_rate = sum(course_changes) / len(course_changes) if course_changes else 0

        # 如果平均转向率 > 2°/min，使用恒定转向率模型
        if avg_rate > 2.0:
            return "constant_turn_rate"
        return "linear"

    def _predict_linear(
        self,
        points: list[AISPoint],
        steps: int,
    ) -> PredictedTrack:
        """线性外推预测。

        基于最近 N 个点的平均速度和航向，匀速直线外推。
        """
        # 取最近几个点计算平均速度和航向
        recent = points[-min(5, len(points)):]

        # 平均速度
        speeds = [p.speed for p in recent if p.speed is not None]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0

        # 平均航向（使用最后一点和倒数第二点计算）
        last = points[-1]
        if len(points) >= 2:
            prev = points[-2]
            course = self._average_course(prev, last)
        else:
            course = last.course

        # 构建预测起点
        base = PredictedPoint(
            timestamp=last.timestamp,
            lat=last.lat,
            lon=last.lon,
            speed=avg_speed,
            course=course,
            confidence=1.0,
            step=0,
        )

        # 逐步外推
        predicted_points: list[PredictedPoint] = []
        current_lat = last.lat
        current_lon = last.lon
        current_speed = avg_speed
        current_course = course
        current_time = last.timestamp
        confidence = 1.0

        for step in range(1, steps + 1):
            dt = timedelta(minutes=self.step_minutes)
            current_time = current_time + dt
            confidence *= self.confidence_decay

            # 计算位移
            current_lat, current_lon = self._dead_reckon(
                current_lat, current_lon,
                current_speed, current_course,
                self.step_minutes,
            )

            predicted_points.append(PredictedPoint(
                timestamp=current_time,
                lat=current_lat,
                lon=current_lon,
                speed=current_speed,
                course=current_course,
                confidence=round(confidence, 3),
                step=step,
            ))

        return PredictedTrack(
            mmsi=last.mmsi,
            method="linear",
            points=predicted_points,
            base_point=base,
            step_minutes=self.step_minutes,
            confidence_decay=self.confidence_decay,
        )

    def _predict_constant_turn_rate(
        self,
        points: list[AISPoint],
        steps: int,
    ) -> PredictedTrack:
        """恒定转向率外推预测。

        基于最近的航向变化率，预测船舶会继续以相同速率转向。
        适合弯道/进港等场景。
        """
        last = points[-1]

        # 计算转向率（度/分钟）
        turn_rate = 0.0
        if len(points) >= 3:
            recent = points[-3:]
            course_diffs = []
            for prev, curr in zip(recent, recent[1:]):
                diff = curr.course - prev.course
                # 处理 0°/360° 环绕
                if diff > 180:
                    diff -= 360
                elif diff < -180:
                    diff += 360
                elapsed = (curr.timestamp - prev.timestamp).total_seconds() / 60
                if elapsed > 0:
                    course_diffs.append(diff / elapsed)
            if course_diffs:
                turn_rate = sum(course_diffs) / len(course_diffs)

        # 平均速度
        speeds = [p.speed for p in points[-5:] if p.speed is not None]
        avg_speed = sum(speeds) / len(speeds) if speeds else 0.0

        # 构建预测起点
        base = PredictedPoint(
            timestamp=last.timestamp,
            lat=last.lat,
            lon=last.lon,
            speed=avg_speed,
            course=last.course,
            confidence=1.0,
            step=0,
        )

        # 逐步外推（每步航向变化 turn_rate * step_minutes）
        predicted_points: list[PredictedPoint] = []
        current_lat = last.lat
        current_lon = last.lon
        current_speed = avg_speed
        current_course = last.course
        current_time = last.timestamp
        confidence = 1.0

        for step in range(1, steps + 1):
            dt = timedelta(minutes=self.step_minutes)
            current_time = current_time + dt
            confidence *= self.confidence_decay

            # 更新航向
            current_course = (current_course + turn_rate * self.step_minutes) % 360.0

            # 计算位移
            current_lat, current_lon = self._dead_reckon(
                current_lat, current_lon,
                current_speed, current_course,
                self.step_minutes,
            )

            predicted_points.append(PredictedPoint(
                timestamp=current_time,
                lat=current_lat,
                lon=current_lon,
                speed=current_speed,
                course=round(current_course, 1),
                confidence=round(confidence, 3),
                step=step,
            ))

        return PredictedTrack(
            mmsi=last.mmsi,
            method="constant_turn_rate",
            points=predicted_points,
            base_point=base,
            step_minutes=self.step_minutes,
            confidence_decay=self.confidence_decay,
        )

    @staticmethod
    def _average_course(p1: AISPoint, p2: AISPoint) -> float:
        """计算两点的平均航向（处理 0°/360° 环绕）。"""
        diff = p2.course - p1.course
        if diff > 180:
            diff -= 360
        elif diff < -180:
            diff += 360
        return (p1.course + diff / 2) % 360.0

    @staticmethod
    def _dead_reckon(
        lat: float,
        lon: float,
        speed_knots: float,
        course_deg: float,
        minutes: float,
    ) -> tuple[float, float]:
        """航位推算：根据当前位置、速度、航向，计算 minutes 分钟后的位置。

        使用平面近似（适用于短距离），精度足够用于 30 分钟内的预测。

        Args:
            lat: 当前纬度
            lon: 当前经度
            speed_knots: 速度（节）
            course_deg: 航向（度，0=北，90=东）
            minutes: 推算时间（分钟）

        Returns:
            (预测纬度, 预测经度)
        """
        if speed_knots <= 0 or minutes <= 0:
            return lat, lon

        # 距离（海里）
        distance_nm = speed_knots * minutes / 60.0

        # 航向转弧度（0°=北，顺时针）
        course_rad = math.radians(course_deg)

        # 纬度变化（1 海里 ≈ 1/60 度纬度）
        dlat = distance_nm * math.cos(course_rad) / 60.0

        # 经度变化（需要考虑纬度修正）
        cos_lat = math.cos(math.radians(lat))
        if abs(cos_lat) < 1e-10:
            cos_lat = 1e-10  # 避免极点除零
        dlon = distance_nm * math.sin(course_rad) / (60.0 * cos_lat)

        return lat + dlat, lon + dlon
