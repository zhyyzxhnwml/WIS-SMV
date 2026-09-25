"""
ais_accelerator/quality.py — AIS 数据质量评估
===============================================

【这个文件做什么？】
  对原始 AIS 数据做质量检查和标准化处理：
  - 解析各种时间格式（ISO 8601 / 美式 / 紧凑式）
  - 校验 MMSI（9位数字）
  - 校验坐标范围（经纬度边界）
  - 校验速度和航向范围
  - 检测重复点（时间+位置完全一致）
  - 检测时间不递增（异常）
  - 检测 AIS 信号中断（相邻点时间间隔过长）
  - 检测位置跳变（隐含速度超过阈值）

【设计原则】
  默认不过滤不丢点 —— 发现质量问题保留原始数据，只打标记（tag）和记录 QualityIssue，
  留给上层决策。
  当 config.clean_points = True 时，额外剔除位置跳变点、重复点与陆地点
  （不插值、不造假数据），剔除数计入 QualityReport.invalid_points；
  其余质量问题（AIS中断/时间异常/越界）仍打标记保留。

  陆地规避（config.clean_on_land + 传入 water 判定器）：结合 Natural Earth 陆地掩码
  与数据驱动航道走廊，把"落在陆地掩码内且远离航道走廊"的点判为陆地点并剔除，
  内河/近岸船（在走廊内）不受影响。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from datetime import datetime, timezone  # 日期时间处理
from typing import Any, Iterable, Optional, Tuple  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .geometry import haversine_nm  # Haversine距离计算
from .models import AISPoint, QualityIssue, QualityReport, QualitySeverity  # 数据模型
from .water import WaterChecker  # 陆地/水域判定器（规避陆地上的 AIS 数据）


# ── RevIN 归一化 ──

class TrackNormalizer:
    """RevIN 风格的逐变量归一化。

    对 SOG/COG/Lat/Lon 做标准化（减均值除标准差），
    保存归一化参数供反归一化使用。

    用法:
        norm = TrackNormalizer()
        norm.fit(points)  # 计算统计量
        normalized = norm.transform(points)  # 归一化
        original = norm.inverse_transform(normalized)  # 反归一化
    """

    def __init__(self):
        self._stats: dict[str, tuple[float, float]] = {}  # 字段 → (均值, 标准差)
        self._fitted = False

    @property
    def stats(self) -> dict[str, tuple[float, float]]:
        """归一化统计量 {字段: (均值, 标准差)}。"""
        return dict(self._stats)

    def fit(self, points: list[AISPoint]) -> "TrackNormalizer":
        """从轨迹点计算归一化统计量。"""
        if not points:
            self._fitted = False
            return self

        lats = [p.lat for p in points]
        lons = [p.lon for p in points]
        speeds = [p.speed for p in points]
        courses = [p.course for p in points]

        self._stats = {
            "lat": self._mean_std(lats),
            "lon": self._mean_std(lons),
            "speed": self._mean_std(speeds),
            "course": self._mean_std(courses),
        }
        self._fitted = True
        return self

    def transform(self, points: list[AISPoint]) -> list[AISPoint]:
        """归一化轨迹点（返回新列表，不修改原始点）。"""
        if not self._fitted:
            return list(points)

        normalized = []
        for p in points:
            lat_n = self._normalize(p.lat, "lat")
            lon_n = self._normalize(p.lon, "lon")
            speed_n = self._normalize(p.speed, "speed")
            course_n = self._normalize(p.course, "course")

            normalized.append(AISPoint(
                mmsi=p.mmsi,
                timestamp=p.timestamp,
                lat=lat_n,
                lon=lon_n,
                speed=speed_n,
                course=course_n,
                status=p.status,
                quality_flags=list(p.quality_flags),
                tags=list(p.tags),
            ))
        return normalized

    def inverse_transform(self, points: list[AISPoint]) -> list[AISPoint]:
        """反归一化轨迹点。"""
        if not self._fitted:
            return list(points)

        denormalized = []
        for p in points:
            lat_d = self._denormalize(p.lat, "lat")
            lon_d = self._denormalize(p.lon, "lon")
            speed_d = self._denormalize(p.speed, "speed")
            course_d = self._denormalize(p.course, "course")

            denormalized.append(AISPoint(
                mmsi=p.mmsi,
                timestamp=p.timestamp,
                lat=lat_d,
                lon=lon_d,
                speed=max(0.0, speed_d),
                course=course_d % 360.0,
                status=p.status,
                quality_flags=list(p.quality_flags),
                tags=list(p.tags),
            ))
        return denormalized

    def _normalize(self, value: float, field: str) -> float:
        """标准化单个值：(value - mean) / std。"""
        if field not in self._stats:
            return value
        mean, std = self._stats[field]
        if std < 1e-10:
            return 0.0
        return (value - mean) / std

    def _denormalize(self, value: float, field: str) -> float:
        """反标准化单个值：value * std + mean。"""
        if field not in self._stats:
            return value
        mean, std = self._stats[field]
        return value * std + mean

    @staticmethod
    def _mean_std(values: list[float]) -> tuple[float, float]:
        """计算均值和标准差。"""
        if not values:
            return (0.0, 1.0)
        n = len(values)
        mean = sum(values) / n
        if n < 2:
            return (mean, 1.0)
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = variance ** 0.5
        return (mean, max(std, 1e-10))  # 避免除零


KNOWN_TIMESTAMP_FORMATS = (  # 支持的时间解析格式（按优先级排序）
    "%m/%d/%Y %H:%M:%S",  # 美式格式（月/日/年 时:分:秒）
    "%m/%d/%Y %H:%M",  # 美式格式（月/日/年 时:分）
    "%Y-%m-%d %H:%M:%S",  # ISO格式（年-月-日 时:分:秒）
    "%Y/%m/%d %H:%M:%S",  # 斜杠分隔格式（年/月/日 时:分:秒）
)


def parse_timestamp(value: Any) -> datetime:  # 将各种格式的时间字符串解析为datetime对象
    text = str(value or "").strip()  # 转换为字符串并去除前后空白
    if not text:  # 如果为空
        raise ValueError("empty timestamp")  # 抛出值错误
    iso = text.replace("Z", "+00:00")  # 将Z替换为UTC偏移
    try:
        parsed = datetime.fromisoformat(iso)  # 尝试ISO格式解析
    except ValueError:  # ISO解析失败
        parsed = None  # 初始化为None
        for fmt in KNOWN_TIMESTAMP_FORMATS:  # 遍历支持的时间格式
            try:
                parsed = datetime.strptime(text, fmt)  # 按指定格式解析
                break  # 解析成功，退出循环
            except ValueError:  # 格式不匹配
                continue  # 继续尝试下一个格式
        if parsed is None:  # 所有格式都解析失败
            raise ValueError(f"unsupported timestamp: {text}")  # 抛出值错误
    if parsed.tzinfo is None:  # 如果解析结果没有时区信息
        parsed = parsed.replace(tzinfo=timezone.utc)  # 设为UTC时区
    return parsed.astimezone(timezone.utc)  # 转换为UTC时区并返回


def parse_raw_point(row: dict[str, Any]) -> Tuple[Optional[AISPoint], list[QualityIssue]]:  # 解析原始数据行，返回AIS点和质量问题列表
    issues: list[QualityIssue] = []  # 质量问题列表
    mmsi = str(row.get("mmsi", "")).strip()  # 获取MMSI并去除空白
    if len(mmsi) != 9 or not mmsi.isdigit():  # 校验MMSI是否为9位数字
        issues.append(  # 添加MMSI无效问题
            QualityIssue(
                code="invalid_mmsi",  # 问题代码
                severity=QualitySeverity.CRITICAL,  # 严重级别
                message=f"无效MMSI: {mmsi or '<empty>'}",  # 错误信息
            )
        )
        return None, issues  # 返回空点和问题列表
    try:
        timestamp = parse_timestamp(row.get("timestamp", row.get("time")))  # 解析时间戳
        lat = float(row.get("lat"))  # 解析纬度
        lon = float(row.get("lon"))  # 解析经度
        speed = float(row.get("speed", 0))  # 解析航速，默认0
        course = float(row.get("course", 0))  # 解析航向，默认0
    except (TypeError, ValueError) as exc:  # 解析异常
        issues.append(  # 添加字段解析失败问题
            QualityIssue(
                code="parse_error",  # 问题代码
                severity=QualitySeverity.CRITICAL,  # 严重级别
                message=f"AIS字段解析失败: {exc}",  # 错误信息
            )
        )
        return None, issues  # 返回空点和问题列表

    if not -90 <= lat <= 90 or not -180 <= lon <= 180:  # 校验坐标范围
        issues.append(  # 添加坐标越界问题
            QualityIssue(
                code="coordinate_out_of_range",  # 问题代码
                severity=QualitySeverity.CRITICAL,  # 严重级别
                message=f"坐标越界: ({lat}, {lon})",  # 错误信息
                timestamp=timestamp,  # 时间戳
            )
        )
    if not 0 <= speed <= 102.3:  # 校验航速范围（AIS标准最大102.3节）
        issues.append(  # 添加航速越界问题
            QualityIssue(
                code="speed_out_of_range",  # 问题代码
                severity=QualitySeverity.WARNING,  # 严重级别
                message=f"航速越界: {speed}节",  # 错误信息
                timestamp=timestamp,  # 时间戳
            )
        )
    if not 0 <= course <= 360:  # 校验航向范围
        issues.append(  # 添加航向越界问题
            QualityIssue(
                code="course_out_of_range",  # 问题代码
                severity=QualitySeverity.WARNING,  # 严重级别
                message=f"航向越界: {course}°",  # 错误信息
                timestamp=timestamp,  # 时间戳
            )
        )

    if any(issue.severity == QualitySeverity.CRITICAL for issue in issues):  # 如果有严重质量问题
        return None, issues  # 返回空点和问题列表（不生成AISPoint）
    status = str(row.get("status", "")).strip().lower() or None  # 获取并格式化船舶状态
    return (  # 返回AISPoint对象和问题列表
        AISPoint(
            mmsi=mmsi,  # MMSI
            timestamp=timestamp,  # 时间戳
            lat=lat,  # 纬度
            lon=lon,  # 经度
            speed=max(0.0, min(speed, 102.3)),  # 航速（钳制到有效范围）
            course=course % 360.0,  # 航向（归一化到0~360度）
            status=status,  # 船舶状态
            quality_flags=[issue.code for issue in issues],  # 质量问题代码列表
        ),
        issues,  # 质量问题列表
    )


def prepare_track(  # AIS数据质量评估 —— 不过滤、不丢点，只打标记
    mmsi: str,  # 船舶MMSI
    points: Iterable[AISPoint],  # AIS数据点序列
    config: AcceleratorConfig,  # 加速器配置
    water: Optional[WaterChecker] = None,  # 陆地判定器（None=不做陆地规避）
) -> tuple[list[AISPoint], QualityReport, Optional[TrackNormalizer]]:
    """AIS数据质量评估 —— 检测 + 打标记；clean_points=True 时清洗坏点。

    检测规则（不变）：
    - 重复点：标记后默认剔除（clean_points=False 时保留）
    - 时间异常点 / AIS中断点：不剔除，标记后保留
    - 位置跳变点：标记后默认剔除（clean_points=False 时保留）
    - 陆地规避（water 传入且 clean_on_land=True）：落在陆地掩码内且远离
      航道走廊的点判为陆地点，标记后默认剔除（clean_points=False 时保留）
    - 被剔除点仍记录 QualityIssue 进报告，供审计与 annotator 打标注

    防级联：循环内 `previous = retained[-1]`（相对"最后保留点"计算），
    剔除坏点后下一点自动相对最后保留点计算，不会级联误删正常点。

    Returns:
        (保留点列表, 质量报告, 归一化器)
    """
    ordered = sorted(points, key=lambda point: point.timestamp)  # 按时间戳排序
    report = QualityReport(mmsi=mmsi, original_points=len(ordered))  # 创建质量报告，记录原始点数
    retained: list[AISPoint] = []  # 保留的AIS点列表（clean_points=False 时包含所有点）
    seen: set[tuple[str, float, float]] = set()  # 已处理的点集合（用于去重）
    cleaned = 0  # 本次清洗剔除的异常点数（重复点 + 位置跳变点）

    for point in ordered:  # 遍历每个点
        key = (point.timestamp.isoformat(), round(point.lat, 7), round(point.lon, 7))  # 时间+经纬度作为唯一键
        if key in seen:  # 如果已存在（重复点）
            report.duplicate_points += 1  # 重复点数加1
            point.tags.append("[重复点]")  # 添加重复点标记
            report.issues.append(  # 记录重复点问题
                QualityIssue(
                    code="duplicate_point",  # 问题代码
                    severity=QualitySeverity.INFO,  # 信息级别
                    message="重复AIS位置报告",  # 描述信息
                    timestamp=point.timestamp,  # 时间戳
                )
            )
            if config.clean_points:  # 清洗模式：剔除重复点（先于 gap/jump 检查，避免重复点误触发时间/中断）
                cleaned += 1  # 剔除计数加1
                continue  # 跳过重复点，不进入后续检查
        else:  # 不重复
            seen.add(key)  # 将当前点加入已处理集合

        # 陆地规避：落在陆地掩码内且远离航道走廊 → 陆地点（剔除/标记）。
        # 放在 gap/jump 之前：陆地点不应与前一保留点做距离/时间判断。
        if config.clean_on_land and water is not None and water.is_on_land(point.lon, point.lat):
            report.issues.append(  # 记录陆地点问题
                QualityIssue(
                    code="on_land",  # 问题代码
                    severity=QualitySeverity.WARNING,  # 警告级别
                    message="位置落在陆地上（远离航道走廊）",  # 描述信息
                    timestamp=point.timestamp,  # 时间戳
                )
            )
            point.quality_flags.append("on_land")  # 添加质量标记
            point.tags.append("[陆地]")  # 添加陆地点标签
            if config.clean_points:  # 清洗模式：剔除陆地点
                cleaned += 1  # 剔除计数加1
                continue  # 跳过陆地点，下一点自动相对最后保留点计算，避免级联误删

        if retained:  # 如果不是第一个点
            previous = retained[-1]  # 前一个点
            elapsed_hours = (point.timestamp - previous.timestamp).total_seconds() / 3600  # 时间间隔（小时）
            if elapsed_hours <= 0:  # 时间未递增
                report.issues.append(  # 记录时间异常问题
                    QualityIssue(
                        code="non_increasing_time",  # 问题代码
                        severity=QualitySeverity.WARNING,  # 警告级别
                        message="相邻AIS点时间未递增",  # 描述信息
                        timestamp=point.timestamp,  # 时间戳
                    )
                )
                point.quality_flags.append("non_increasing_time")  # 添加质量标记
                point.tags.append("[时间异常]")  # 添加时间异常标签
            else:  # 时间正常递增
                gap_minutes = elapsed_hours * 60  # 时间间隔（分钟）
                if gap_minutes > config.ais_gap_minutes:  # 超过AIS信号中断阈值
                    report.issues.append(  # 记录AIS中断问题
                        QualityIssue(
                            code="ais_gap",  # 问题代码
                            severity=QualitySeverity.WARNING,  # 警告级别
                            message=f"AIS信号间隔{gap_minutes:.1f}分钟",  # 描述信息
                            timestamp=point.timestamp,  # 时间戳
                            details={"gap_minutes": round(gap_minutes, 2)},  # 详细数据
                        )
                    )
                    point.quality_flags.append("ais_gap")  # 添加质量标记
                    point.tags.append(f"[AIS中断:{gap_minutes:.0f}min]")  # 添加AIS中断标签
                distance_nm = haversine_nm(  # 计算与前一点的距离（海里）
                    previous.lat, previous.lon, point.lat, point.lon  # 前一点和当前点的经纬度
                )
                implied_speed = distance_nm / elapsed_hours  # 计算隐含速度（节）
                if implied_speed > config.max_implied_speed_knots:  # 超过最大合理速度
                    report.issues.append(  # 记录位置跳变问题
                        QualityIssue(
                            code="position_jump",  # 问题代码
                            severity=QualitySeverity.WARNING,  # 警告级别
                            message=f"隐含航速{implied_speed:.1f}节，疑似位置跳变",  # 描述信息
                            timestamp=point.timestamp,  # 时间戳
                            details={  # 详细数据
                                "distance_nm": round(distance_nm, 3),  # 距离（海里）
                                "implied_speed_knots": round(implied_speed, 2),  # 隐含速度（节）
                            },
                        )
                    )
                    point.quality_flags.append("position_jump")  # 添加质量标记
                    point.tags.append(f"[位置跳变:{implied_speed:.0f}节]")  # 添加位置跳变标签
                    if config.clean_points:  # 清洗模式：剔除位置跳变点
                        cleaned += 1  # 剔除计数加1
                        continue  # 跳过跳变点，下一点自动相对最后保留点计算，避免级联误删

        retained.append(point)  # 将当前点加入保留列表

    report.invalid_points = cleaned  # 记录本次清洗剔除的异常点数
    report.retained_points = len(retained)  # 更新保留点数

    # RevIN 归一化：计算统计量但不修改原始数据
    normalizer = TrackNormalizer()
    normalizer.fit(retained)

    return retained, report, normalizer  # 返回保留点列表、质量报告和归一化器