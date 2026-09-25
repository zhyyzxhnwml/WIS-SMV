"""
ais_accelerator/collision.py — 碰撞风险检测模块
=================================================

【这个文件做什么？】
  基于预测轨迹计算 DCPA/TCPA/CRI，检测船舶碰撞风险。
  这是论文 AIS-LLM 的核心创新点之一：先预测轨迹，再在预测轨迹上
  计算最近会遇点，而非仅基于当前点。

【核心算法】
  1. DCPA（Distance at Closest Point of Approach）：预测轨迹上的最近会遇距离
  2. TCPA（Time to Closest Point of Approach）：到达最近会遇点的时间
  3. CRI（Collision Risk Index）：综合碰撞风险指数 [0, 1]

【CRI 计算】
  CRI = w_d * f(DCPA) + w_t * f(TCPA) + w_r * f(relative_bearing) + w_s * f(speed_ratio)

  其中：
  - f(DCPA) = 1 / (1 + DCPA / DCPA_threshold)
  - f(TCPA) = 1 / (1 + TCPA / TCPA_threshold)  （TCPA > 0 时）
  - f(relative_bearing) = 基于相对方位的权重
  - f(speed_ratio) = 基于速度比的权重

【与论文的对应】
  论文用 4 任务共享表示，CRI 是独立输出头。
  我们用规则 + 预测轨迹实现同样的效果，且可解释性更强。

【设计原则】
  - 碰撞风险是"船-船交互"事件，需要同时考虑两艘船的预测轨迹
  - CRI 阈值可配置，默认 0.7 为高风险
  - 检测结果作为 CandidateEvent(COLLISION_RISK) 进入 cascade
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from .config import AcceleratorConfig
from .geometry import haversine_nm, circular_angle_diff
from .models import AISPoint, CandidateEvent, CandidateType
from .predict import TrajectoryPredictor, PredictedTrack, PredictedPoint


# ── 数据结构 ──

@dataclass
class CollisionRisk:
    """两船碰撞风险评估结果。"""
    mmsi_own: str  # 本船 MMSI
    mmsi_target: str  # 目标船 MMSI

    dcpa_nm: float  # 最近会遇距离（海里）
    tcpa_minutes: float  # 到达最近会遇点的时间（分钟，负值表示已过最近点）
    cri: float  # 碰撞风险指数 [0, 1]

    # 预测轨迹上的最近会遇点信息
    own_closest_point: Optional[PredictedPoint] = None  # 本船最近会遇点
    target_closest_point: Optional[PredictedPoint] = None  # 目标船最近会遇点

    # 当前相对运动信息
    relative_bearing: float = 0.0  # 相对方位（度）
    relative_speed: float = 0.0  # 相对速度（节）
    distance_nm: float = 0.0  # 当前距离（海里）

    # 风险等级
    risk_level: str = "low"  # low / medium / high

    def to_dict(self) -> dict:
        return {
            "mmsi_own": self.mmsi_own,
            "mmsi_target": self.mmsi_target,
            "dcpa_nm": round(self.dcpa_nm, 3),
            "tcpa_minutes": round(self.tcpa_minutes, 2),
            "cri": round(self.cri, 3),
            "relative_bearing": round(self.relative_bearing, 1),
            "relative_speed": round(self.relative_speed, 2),
            "distance_nm": round(self.distance_nm, 3),
            "risk_level": self.risk_level,
        }


# ── 碰撞风险检测器 ──

class CollisionRiskAssessor:
    """碰撞风险评估器。

    用法:
        assessor = CollisionRiskAssessor(config)
        risk = assessor.assess(own_track, target_track)
        if risk.cri > 0.7:
            print(f"高风险！CRI={risk.cri:.2f}, DCPA={risk.dcpa_nm:.2f}nm")
    """

    def __init__(self, config: Optional[AcceleratorConfig] = None):
        self.config = config or AcceleratorConfig()
        self.predictor = TrajectoryPredictor(self.config)

        # CRI 权重
        self.w_dcpa = 0.4  # DCPA 权重
        self.w_tcpa = 0.3  # TCPA 权重
        self.w_bearing = 0.15  # 相对方位权重
        self.w_speed = 0.15  # 速度比权重

        # 阈值
        self.dcpa_threshold_nm = self.config.cri_dcpa_threshold_nm  # DCPA 安全阈值（海里）
        self.tcpa_threshold_min = self.config.cri_tcpa_threshold_min  # TCPA 安全阈值（分钟）
        self.cri_high = self.config.cri_high_threshold  # 高风险阈值
        self.cri_medium = self.config.cri_medium_threshold  # 中风险阈值

    def assess(
        self,
        own_points: list[AISPoint],
        target_points: list[AISPoint],
        predict_steps: Optional[int] = None,
    ) -> Optional[CollisionRisk]:
        """评估两船碰撞风险。

        Args:
            own_points: 本船历史 AIS 点
            target_points: 目标船历史 AIS 点
            predict_steps: 预测步数（默认从配置读取）

        Returns:
            CollisionRisk 或 None（数据不足时）
        """
        if not own_points or not target_points:
            return None

        predict_steps = predict_steps or self.config.predict_steps

        # 1. 预测两船轨迹
        own_predicted = self.predictor.predict(own_points, predict_steps)
        target_predicted = self.predictor.predict(target_points, predict_steps)

        if not own_predicted or not target_predicted:
            # 预测失败，回退到基于当前点的计算
            return self._assess_current_only(own_points[-1], target_points[-1])

        # 2. 在预测轨迹上计算 DCPA/TCPA
        dcpa, tcpa, own_closest, target_closest = self._compute_dcpa_tcpa(
            own_predicted, target_predicted
        )

        # 3. 计算当前相对运动信息
        own_last = own_points[-1]
        target_last = target_points[-1]
        distance = haversine_nm(own_last.lat, own_last.lon, target_last.lat, target_last.lon)
        relative_bearing = self._relative_bearing(own_last, target_last)
        relative_speed = self._relative_speed(own_last, target_last)

        # 4. 计算 CRI
        cri = self._compute_cri(dcpa, tcpa, relative_bearing, relative_speed)

        # 5. 确定风险等级
        if cri >= self.cri_high:
            risk_level = "high"
        elif cri >= self.cri_medium:
            risk_level = "medium"
        else:
            risk_level = "low"

        return CollisionRisk(
            mmsi_own=own_last.mmsi,
            mmsi_target=target_last.mmsi,
            dcpa_nm=dcpa,
            tcpa_minutes=tcpa,
            cri=cri,
            own_closest_point=own_closest,
            target_closest_point=target_closest,
            relative_bearing=relative_bearing,
            relative_speed=relative_speed,
            distance_nm=distance,
            risk_level=risk_level,
        )

    def _assess_current_only(
        self,
        own: AISPoint,
        target: AISPoint,
    ) -> CollisionRisk:
        """回退方案：仅基于当前点计算（无预测轨迹）。"""
        distance = haversine_nm(own.lat, own.lon, target.lat, target.lon)
        relative_bearing = self._relative_bearing(own, target)
        relative_speed = self._relative_speed(own, target)

        # 简化的 DCPA/TCPA：基于当前速度和航向估算
        dcpa, tcpa = self._estimate_dcpa_tcpa_simple(own, target)

        cri = self._compute_cri(dcpa, tcpa, relative_bearing, relative_speed)

        if cri >= self.cri_high:
            risk_level = "high"
        elif cri >= self.cri_medium:
            risk_level = "medium"
        else:
            risk_level = "low"

        return CollisionRisk(
            mmsi_own=own.mmsi,
            mmsi_target=target.mmsi,
            dcpa_nm=dcpa,
            tcpa_minutes=tcpa,
            cri=cri,
            relative_bearing=relative_bearing,
            relative_speed=relative_speed,
            distance_nm=distance,
            risk_level=risk_level,
        )

    def _compute_dcpa_tcpa(
        self,
        own_predicted: PredictedTrack,
        target_predicted: PredictedTrack,
    ) -> tuple[float, float, Optional[PredictedPoint], Optional[PredictedPoint]]:
        """在预测轨迹上计算 DCPA 和 TCPA。

        遍历预测轨迹的每个时间步，找到两船距离最小的时刻。

        Returns:
            (DCPA海里, TCPA分钟, 本船最近点, 目标船最近点)
        """
        min_distance = float("inf")
        min_step = 0
        own_closest: Optional[PredictedPoint] = None
        target_closest: Optional[PredictedPoint] = None

        # 包含当前点（step=0）
        own_all = [own_predicted.base_point] + own_predicted.points if own_predicted.base_point else own_predicted.points
        target_all = [target_predicted.base_point] + target_predicted.points if target_predicted.base_point else target_predicted.points

        for i, (own_pt, target_pt) in enumerate(zip(own_all, target_all)):
            dist = haversine_nm(own_pt.lat, own_pt.lon, target_pt.lat, target_pt.lon)
            if dist < min_distance:
                min_distance = dist
                min_step = i
                own_closest = own_pt
                target_closest = target_pt

        # TCPA = 到达最近点的时间（分钟）
        tcpa = min_step * own_predicted.step_minutes

        # 如果最近点是当前点（step=0），TCPA 为 0（已经最近了）
        # 如果最近点在未来，TCPA > 0
        # 如果最近点在过去（不太可能，因为我们从当前点开始），TCPA < 0

        return min_distance, tcpa, own_closest, target_closest

    @staticmethod
    def _estimate_dcpa_tcpa_simple(
        own: AISPoint, target: AISPoint
    ) -> tuple[float, float]:
        """简化的 DCPA/TCPA 估算（基于当前速度和航向）。

        使用两船的当前位置、速度、航向，计算相对运动向量，
        然后求最近会遇距离和时间。
        """
        # 相对位置向量（目标船 - 本船），单位：海里
        # 纬度差转海里（1度 ≈ 60海里）
        dlat_nm = (target.lat - own.lat) * 60.0
        # 经度差转海里（需要纬度修正）
        cos_lat = math.cos(math.radians((own.lat + target.lat) / 2))
        dlon_nm = (target.lon - own.lon) * 60.0 * cos_lat

        # 两船速度向量（节 → 海里/小时）
        own_vn = own.speed * math.cos(math.radians(own.course))  # 北向分量
        own_ve = own.speed * math.sin(math.radians(own.course))  # 东向分量
        target_vn = target.speed * math.cos(math.radians(target.course))
        target_ve = target.speed * math.sin(math.radians(target.course))

        # 相对速度向量（目标船 - 本船）
        rv_n = target_vn - own_vn
        rv_e = target_ve - own_ve

        # 相对速度大小
        rv = math.sqrt(rv_n ** 2 + rv_e ** 2)

        if rv < 0.01:
            # 相对速度极小，两船几乎同速同向，DCPA = 当前距离
            current_dist = math.sqrt(dlat_nm ** 2 + dlon_nm ** 2)
            return current_dist, float("inf")

        # TCPA = -(相对位置 · 相对速度) / |相对速度|^2
        tcpa_hours = -(dlat_nm * rv_n + dlon_nm * rv_e) / (rv ** 2)
        tcpa_minutes = tcpa_hours * 60.0

        # DCPA = |相对位置 + TCPA * 相对速度| / |相对速度|
        # 在最近会遇点，相对位置在相对速度方向的投影
        dcpa_sq = (dlat_nm + tcpa_hours * rv_n) ** 2 + (dlon_nm + tcpa_hours * rv_e) ** 2
        dcpa = math.sqrt(max(0, dcpa_sq))

        return dcpa, tcpa_minutes

    def _compute_cri(
        self,
        dcpa: float,
        tcpa: float,
        relative_bearing: float,
        relative_speed: float,
    ) -> float:
        """计算碰撞风险指数 CRI。

        CRI = w_d * f(DCPA) + w_t * f(TCPA) + w_r * f(bearing) + w_s * f(speed)
        """
        # f(DCPA): DCPA 越小风险越高
        f_dcpa = 1.0 / (1.0 + (dcpa / self.dcpa_threshold_nm) ** 2)

        # f(TCPA): TCPA 越小风险越高（仅 TCPA > 0 时有意义）
        if tcpa > 0:
            f_tcpa = 1.0 / (1.0 + (tcpa / self.tcpa_threshold_min) ** 2)
        elif tcpa > -5:
            # TCPA 在 -5~0 分钟：已经过了最近点但很近
            f_tcpa = 0.9
        else:
            # TCPA < -5 分钟：已经远离最近点
            f_tcpa = 0.1

        # f(bearing): 相对方位在船首方向（0°~45° 或 315°~360°）风险更高
        if relative_bearing <= 45 or relative_bearing >= 315:
            f_bearing = 0.8
        elif 45 < relative_bearing <= 90 or 270 <= relative_bearing < 315:
            f_bearing = 0.5
        else:
            f_bearing = 0.2

        # f(speed): 相对速度越大风险越高
        f_speed = min(1.0, relative_speed / 30.0)

        cri = (
            self.w_dcpa * f_dcpa
            + self.w_tcpa * f_tcpa
            + self.w_bearing * f_bearing
            + self.w_speed * f_speed
        )

        return max(0.0, min(1.0, cri))

    @staticmethod
    def _relative_bearing(own: AISPoint, target: AISPoint) -> float:
        """计算目标船相对于本船的方位角（0°=北，顺时针）。"""
        dlat = target.lat - own.lat
        dlon = target.lon - own.lon
        bearing = math.degrees(math.atan2(dlon, dlat)) % 360.0
        # 转为相对方位（相对于本船航向）
        relative = (bearing - own.course) % 360.0
        return relative

    @staticmethod
    def _relative_speed(own: AISPoint, target: AISPoint) -> float:
        """计算两船相对速度（节）。"""
        own_vn = own.speed * math.cos(math.radians(own.course))
        own_ve = own.speed * math.sin(math.radians(own.course))
        target_vn = target.speed * math.cos(math.radians(target.course))
        target_ve = target.speed * math.sin(math.radians(target.course))
        rv_n = target_vn - own_vn
        rv_e = target_ve - own_ve
        return math.sqrt(rv_n ** 2 + rv_e ** 2)


# ── 碰撞风险检测（集成到 detect.py 的接口） ──

def detect_collision_risks(
    own_points: list[AISPoint],
    nearby_tracks: dict[str, list[AISPoint]],
    config: AcceleratorConfig,
) -> list[CollisionRisk]:
    """检测本船与所有邻近船的碰撞风险。

    这是 collision 模块的主入口，由 pipeline.py 调用。

    Args:
        own_points: 本船历史 AIS 点
        nearby_tracks: 邻近船的轨迹 {MMSI: [AISPoint]}
        config: 加速器配置

    Returns:
        碰撞风险列表（仅包含 CRI 超过中风险阈值的）
    """
    if not own_points or not nearby_tracks:
        return []

    assessor = CollisionRiskAssessor(config)
    risks: list[CollisionRisk] = []

    for target_mmsi, target_points in nearby_tracks.items():
        if not target_points:
            continue

        risk = assessor.assess(own_points, target_points)
        if risk and risk.cri >= config.cri_medium_threshold:
            risks.append(risk)

    # 按 CRI 降序排序
    risks.sort(key=lambda r: r.cri, reverse=True)
    return risks


def collision_risks_to_events(
    risks: list[CollisionRisk],
    own_mmsi: str,
) -> list[CandidateEvent]:
    """将碰撞风险评估结果转为 CandidateEvent 列表。

    Args:
        risks: 碰撞风险列表
        own_mmsi: 本船 MMSI

    Returns:
        CandidateEvent 列表
    """
    events: list[CandidateEvent] = []

    for risk in risks:
        event_id = f"COL-{risk.mmsi_own}-{risk.mmsi_target}-{int(risk.own_closest_point.timestamp.timestamp()) if risk.own_closest_point else 0}"

        # 构建证据点（本船最近会遇点 + 目标船最近会遇点）
        evidence_points = []
        if risk.own_closest_point:
            evidence_points.append(AISPoint(
                mmsi=risk.mmsi_own,
                timestamp=risk.own_closest_point.timestamp,
                lat=risk.own_closest_point.lat,
                lon=risk.own_closest_point.lon,
                speed=risk.own_closest_point.speed,
                course=risk.own_closest_point.course,
            ))
        if risk.target_closest_point:
            evidence_points.append(AISPoint(
                mmsi=risk.mmsi_target,
                timestamp=risk.target_closest_point.timestamp,
                lat=risk.target_closest_point.lat,
                lon=risk.target_closest_point.lon,
                speed=risk.target_closest_point.speed,
                course=risk.target_closest_point.course,
            ))

        # 优先级
        if risk.cri >= 0.8:
            priority = "high"
        elif risk.cri >= 0.6:
            priority = "middle"
        else:
            priority = "low"

        # 时间范围
        start_time = risk.own_closest_point.timestamp if risk.own_closest_point else datetime.now(timezone.utc)
        end_time = start_time

        events.append(CandidateEvent(
            event_id=event_id,
            mmsi=own_mmsi,
            candidate_type=CandidateType.COLLISION_RISK,
            start_time=start_time,
            end_time=end_time,
            priority=priority,
            features={
                "target_mmsi": risk.mmsi_target,
                "dcpa_nm": round(risk.dcpa_nm, 3),
                "tcpa_minutes": round(risk.tcpa_minutes, 2),
                "cri": round(risk.cri, 3),
                "distance_nm": round(risk.distance_nm, 3),
                "relative_bearing": round(risk.relative_bearing, 1),
                "relative_speed": round(risk.relative_speed, 2),
                "risk_level": risk.risk_level,
            },
            evidence_points=evidence_points,
        ))

    return events
