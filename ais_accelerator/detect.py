"""
ais_accelerator/detect.py — 候选事件检测器
=============================================

【这个文件做什么？】
  对轨迹段进行 15 种类型的"候选违规事件"检测（类型全集见 models.CandidateType，
  此处不再逐一罗列——罗列过就会过期）：
  - 区域内事件 3 种：RESTRICTED_AREA_ENTRY、SPEED_VIOLATION（区域限速）、
    ILLEGAL_FISHING（禁渔区/禁渔期内的法定违规，以及 ANCHORING_OUT_OF_BOUNDS）
  - 质量事件 3 种：AIS_GAP（时间上没收到报文）、AIS_OFF（收到了但内容不可用）、
    POSITION_JUMP
  - 操纵异常 1 种：COURSE_ANOMALY
  - 场驱动 3 种：SPEED_VIOLATION（数据驱动）、WRONG_WAY_NAVIGATION、
    LOW_SPEED_FISHING（开阔水域作业嫌疑）
  - 航道驱动 1 种：FAIRWAY_ANCHORING（数据驱动走廊内锚泊）
  - 多船 4 种：COLLISION_RISK（collision.py）、OVERTAKING_VIOLATION（encounter.py）、
    GIVE_WAY_FAILURE（colreg.py）、ILLEGAL_TOWING（towing.py）

【设计原则】
  - Recall-first：阈值偏低宁可误报，不漏掉真违规
  - 纯规则驱动：不使用任何 ML 模型，所有规则可解释
  - 逐段独立：每个轨迹段单独检测，段间不影响
  - 场驱动：超速与逆向航行参照"全船队流向/流速场"（flow.py）而非人工画的区域，
    该场作为可选参数传入；不传则这两类检测器不运行，行为与旧版一致
  - 航道驱动：航道内锚泊参照"全船队航行走廊"（water.py），同样可选传入

【易混的两对码（改判据前先读这两行）】
  - AIS_GAP vs AIS_OFF：没收到报文 vs 收到了但内容不可用，判据互斥，不可互换
  - ILLEGAL_FISHING vs LOW_SPEED_FISHING：区域内（法定违规，区域内）vs
    开阔水域（作业嫌疑，无区域），同样互斥

【关键算法】
  - _contiguous_runs(): 找到连续满足条件的点区间
  - season_is_active(): 判断季节性规则（如禁渔期）是否生效
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import hashlib  # 哈希计算模块
from statistics import median  # 中位数计算
from typing import Callable, Iterable, Optional  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .flow import FlowField  # 全船队流向/流速场（超速与逆向航行判据的参照）
from .geometry import circular_angle_diff  # 航向角度差计算
from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    CandidateEvent,  # 候选事件
    CandidateType,  # 候选事件类型
    QualityIssue,  # 质量问题
    TrajectorySegment,  # 轨迹分段
)
from .rules import RuleProvider, season_is_active  # 规则提供器和季节性规则判断
from .water import WaterChecker, cell_of  # 航道走廊判定器与格网换算


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
    field: Optional[FlowField] = None,  # 全船队流向/流速场（不传则跳过两个场驱动检测器）
    water: Optional[WaterChecker] = None,  # 航道走廊判定器（不传则跳过航道内锚泊检测）
) -> list[CandidateEvent]:
    if not segment.points:  # 如果分段没有点
        return []  # 返回空列表
    events: list[CandidateEvent] = []  # 候选事件列表
    events.extend(_detect_zone_events(segment, rules, config))  # 检测区域相关事件
    events.extend(_detect_quality_events(segment))  # 检测质量相关事件（AIS中断、位置跳变）
    events.extend(_detect_ais_off_events(segment, config))  # 报文内容不可用（与 AIS 中断互斥）
    events.extend(_detect_course_events(segment, config))  # 检测航向异常事件
    if water is not None:  # 有航道走廊产物时才跑走廊驱动的检测器
        events.extend(_detect_fairway_anchoring_events(segment, config, water))  # 航道内锚泊
    if field is not None:  # 有全船队场产物时才跑场驱动检测器
        events.extend(_detect_speed_events(segment, config, field))  # 数据驱动超速
        events.extend(_detect_counterflow_events(segment, config, field))  # 逆向航行
        # 开阔水域的低速作业嫌疑（与区域内的 illegal_fishing 互补）。挂在场驱动这一档
        # 是因为它同属"需要全船队上下文才有意义"的判据：单看一条船的低速区间，
        # 分不出它是捕捞、是待泊还是在等潮——只有跑过整批数据（此时 field 已构建）
        # 才谈得上"与船队整体行为相比它像在作业"。不传 field 时行为与旧版一致。
        events.extend(_detect_open_water_fishing_events(segment, rules, config))
    return events  # 返回所有检测到的事件


def _detect_speed_events(  # 数据驱动超速检测
    segment: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
    field: FlowField,  # 全船队流速场
) -> list[CandidateEvent]:
    """超速候选：**绝对异常** 或 **超过同水域主流速的倍数**，任一命中即算候选。

    为什么不再只靠"区域限速"：限速写在 `regulations.json` 的 zone restrictions 里，
    而那些区域是示例数据（实测 3 个区全在 38°N，真实数据在 24–27°N），**零重叠**，
    于是区域判据从未触发过。改成从数据本身学出参照值，不依赖人工画区域。

    速度上限是刻意的：实测多船同时取到 102.3 节，那是 AIS 速度字段的饱和值
    （1023/10，即"不可用"），属**数据异常**不属超速，交给 `max_implied_speed_knots`
    的口径处理。
    """
    absolute_floor = config.speed_abs_min_knots  # 绝对超速下限（节）
    absolute_ceiling = config.speed_abs_max_knots  # 上限：超过即视为数据异常
    ratio = config.speed_relative_ratio  # 相对主流速的倍数
    min_minutes = config.speed_violation_min_minutes  # 最短持续（分钟）
    min_points = config.speed_violation_min_points  # 最少点数

    def threshold_of(point: AISPoint) -> tuple[float, Optional[float]]:  # 该点的适用阈值
        reference = field.speed_reference(point.lon, point.lat)  # 同格网主流速分位
        if reference is None:  # 样本不足，无参照
            return absolute_floor, None  # 只用地板值
        return max(absolute_floor, reference * ratio), reference  # 两个判据取更严者

    def is_candidate(point: AISPoint) -> bool:  # 单点是否为超速候选
        if point.speed > absolute_ceiling:  # AIS 速度字段饱和值，属数据异常
            return False  # 不计入超速
        threshold, _reference = threshold_of(point)  # 取阈值
        return point.speed >= threshold  # 达到阈值

    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(segment.points, is_candidate):  # 查找连续超速区间
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 持续分钟
        if len(run) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 跳过
        peak = max(run, key=lambda point: point.speed)  # 区间最高速点
        threshold, reference = threshold_of(peak)  # 该点适用的阈值与参照
        max_speed = peak.speed  # 区间最高速度
        average_speed = sum(point.speed for point in run) / len(run)  # 区间平均速度
        # 哪个判据在起作用：相对判据更严时为 "relative"，否则为 "absolute"
        judge = "relative" if reference is not None and reference * ratio > absolute_floor else "absolute"
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.SPEED_VIOLATION,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
        )
        events.append(  # 添加超速事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.SPEED_VIOLATION,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                # 不是区域违规，所以 zone_id/zone_name 留空（判据不依赖人工画的区域）
                priority="high" if max_speed > threshold * 1.5 else "middle",  # 严重超速为高
                features={  # 特征
                    "speed_limit": round(threshold, 2),  # 适用阈值（供细类分档的比值分母）
                    "speed_over_ratio": round(max_speed / threshold, 3) if threshold else 0.0,  # 超速倍数
                    "reference_speed": round(reference, 2) if reference is not None else None,  # 同水域主流速
                    "judge": judge,  # 起作用的判据：absolute | relative
                    "max_speed": max_speed,  # 最高速度
                    "average_speed": round(average_speed, 2),  # 平均速度
                    "duration_minutes": round(duration, 2),  # 持续分钟
                    "point_count": len(run),  # 点数
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有超速事件


def _detect_counterflow_events(  # 逆向航行检测
    segment: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
    field: FlowField,  # 全船队流向场
) -> list[CandidateEvent]:
    """逆向航行候选：航向**对所处分道/航路的所有合法主流向都相反**，且持续一定时长。

    为什么要「对所有主流向都相反」：分道通航制是双向航道，对向车道是合法方向。
    只跟"唯一主导航向"比，会把整条对向车道判成逆行——实测该口径下 91% 的船被误判。
    `field.course_deviation` 返回的是与**最近**主流向的夹角，所以"近"就代表沿任一
    合法车道行驶；只有连最近的主流向都差 ≥ 阈值，才真是逆行。

    样本不足的格网返回 None，此时**跳过**而不是视为合规：样本不足 ≠ 合规，
    只是无从判断。
    """
    deviation_limit = config.counterflow_angle_degrees  # 与最近主流向的最小夹角（度）
    min_minutes = config.counterflow_min_minutes  # 最短持续（分钟）
    min_points = config.counterflow_min_points  # 最少点数

    def deviation_at(point: AISPoint) -> Optional[float]:  # 该点的航向偏离度
        return field.course_deviation(point.lon, point.lat, point.course)  # None 表示无参照

    def is_candidate(point: AISPoint) -> bool:  # 单点是否为逆行候选
        deviation = deviation_at(point)  # 取偏离度
        return deviation is not None and deviation >= deviation_limit  # 有参照且偏离足够大

    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(segment.points, is_candidate):  # 查找连续逆行区间
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 持续分钟
        if len(run) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 跳过
        deviations = [  # 区间内各点偏离度
            value  # 偏离度
            for value in (deviation_at(point) for point in run)  # 逐点计算
            if value is not None  # 剔除无参照点
        ]
        if not deviations:  # 全无参照（理论上不会，谓词已过滤）
            continue  # 跳过
        max_deviation = max(deviations)  # 区间最大偏离度（作为分档量级）
        # 该区间的合法主流向（用于证据展示：说明它逆的是哪条道）
        main_courses = field.dominant_courses(run[0].lon, run[0].lat)  # 区间起点的格网主流向
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.WRONG_WAY_NAVIGATION,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
        )
        events.append(  # 添加逆向航行事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.WRONG_WAY_NAVIGATION,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                priority="high" if duration >= 10.0 else "middle",  # 长时间逆行为高优先级
                features={  # 特征
                    "course_offset_degrees": round(max_deviation, 1),  # 最大偏离度（分档量级）
                    "fairway_direction": [round(c, 1) for c in main_courses],  # 该水域合法主流向
                    "duration_minutes": round(duration, 2),  # 持续分钟
                    "average_speed": round(sum(point.speed for point in run) / len(run), 2),  # 平均速度
                    "point_count": len(run),  # 点数
                    "colreg_rule": "10",  # 依据条款（分道通航制）
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有逆向航行事件


def _detect_open_water_fishing_events(  # 开阔水域的低速作业嫌疑
    segment: TrajectorySegment,  # 轨迹分段
    rules: RuleProvider,  # 规则提供器（用于排除落在区域内的点）
    config: AcceleratorConfig,  # 加速器配置
) -> list[CandidateEvent]:
    """低速捕捞**嫌疑**：不在任何区域内，却在正常航行之后长时间极低速。

    与 `_detect_zone_events` 里 `illegal_fishing` 那条分支的分工是本函数的立身之本：
      - `illegal_fishing`（法定违规，严重度 75）= **禁渔区/禁渔期内**捕捞。
        判据来自区域声明，回答的是"违反了哪一条"。
      - `low_speed_fishing`（作业嫌疑，严重度 55）= **开阔水域**出现的拖网行为特征。
        判据来自行为本身，回答的是"看起来像在作业"。
    语义如此，它就只能停在"嫌疑"，不能与法定违规混为一谈——所以两者必须互斥。

    "不在任何区域内"是硬条件而非修饰：区域内的低速已经由那条法定分支处理，
    两边都命中会让同一段轨迹同时报"违规"和"嫌疑"，而嫌疑那条更轻，
    合并时反而会把叙事拉低。

    为什么还要求**事件前**速度正常（`prior_normal_speed_knots`）：一直停在锚地里的
    船速度也一直很低，但那是待泊不是捕捞；只有"先正常航行、再长时间低速"才具备
    下网作业的形态。这条判据同时也把"整段轨迹都慢"的船排除在外。
    """
    enter_speed = config.low_speed_enter_knots  # 低速进入阈值（节）
    min_minutes = config.fishing_min_minutes  # 最短持续（分钟）
    min_points = config.fishing_min_points  # 最少点数
    prior_normal = config.prior_normal_speed_knots  # 事件前"正常航行"的速度线（节）

    def is_candidate(point: AISPoint) -> bool:  # 单点是否为开阔水域低速候选
        if point.speed > enter_speed:  # 速度不够低
            return False  # 不算
        return not rules.zones_at(point.lat, point.lon)  # 且不在任何区域内

    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(segment.points, is_candidate):  # 查找连续低速区间
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 持续分钟
        if len(run) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 跳过
        earlier = [  # 区间开始之前的速度
            point.speed  # 速度
            for point in segment.points  # 遍历轨迹点
            if point.timestamp < run[0].timestamp  # 只看区间之前
        ]
        prior_speed = median(earlier) if earlier else None  # 事件前速度中位数
        if prior_speed is None or prior_speed < prior_normal:  # 之前就不快 → 它是一直慢，不是作业
            continue  # 跳过
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.LOW_SPEED_FISHING,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
        )
        events.append(  # 添加低速作业嫌疑事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.LOW_SPEED_FISHING,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                # 不是区域违规，zone_id/zone_name 留空（判据不依赖任何区域）
                priority="middle",  # 优先级固定为中：本就是"嫌疑"而不是已成立的违规
                features={  # 特征
                    "duration_minutes": round(duration, 2),  # 持续分钟（细类分档量级）
                    "minimum_speed": round(min(point.speed for point in run), 2),  # 区间最低速
                    "average_speed": round(sum(point.speed for point in run) / len(run), 2),  # 区间平均速
                    "prior_median_speed": round(prior_speed, 2),  # 事件前速度中位数
                    "low_speed_threshold": enter_speed,  # 当时的低速阈值
                    "point_count": len(run),  # 点数
                    "in_zone": False,  # 显式标记：不在任何区域内（与区域内那条互斥）
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有低速作业嫌疑事件


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
                            # 细类分档（轻微/明显/严重超速）读的是 speed_over_ratio，
                            # 分母取**规定限速**本身而不是打折后的候选阈值——
                            # 否则"刚好被候选阈值捞到"会被算成 1.0 倍，档位整体偏低。
                            "speed_over_ratio": round(max_speed / float(speed_limit), 3)
                            if float(speed_limit) > 0  # 限速为正才有比值
                            else 0.0,  # 否则置零
                            "judge": "zone_limit",  # 起作用的判据：区域限速
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

            def season_ok(point) -> bool:  # 时间规则：无规则 = 常年有效
                return bool(
                    not time_rules
                    or any(season_is_active(rule, point.timestamp) for rule in time_rules)
                )

            # 船型：TrajectorySegment 逐点带 vessel_type，取第一个非空值
            vtype = next((p.vessel_type for p in segment.points if p.vessel_type), None)
            # 禁令口径：all=任何捕捞都禁；commercial=只禁商业捕捞
            # （NOAA 官方对 "Commercial Fishing Prohibited and Recreational Fishing
            #   Restricted" 这类区域的表述就是后者）
            scope = str(restrictions.get("fishing_ban_scope") or "all").lower()
            if scope == "commercial":
                banned = config.commercial_fishing_vessel_codes
                # 只禁商业捕捞时，报 30（通用"渔船"）的船**无法区分**商业还是休闲。
                # 不能因此静默丢弃 —— 它仍然可能是商业渔船。实测踩过：Kaho'olawe
                # 官方口径是"禁商业捕捞"，而两条真实违规船报的都是 30，
                # 按严格商业码表判会一条都不报（0/2）。所以降一档报出来交人工确认：
                # 既不漏（原行为是完全不报），也不谎称已经确证。
                suspect = tuple(c for c in config.fishing_vessel_codes
                                if c not in config.commercial_fishing_vessel_codes)
                scope_desc = "商业捕捞"
            else:
                banned, suspect = config.fishing_vessel_codes, ()
                scope_desc = "捕捞"

            if vtype is not None:
                # 【船型判据】有船型就按船型判：只要**该船属于被禁的那一类**，
                # 它在区域内即违规，与速度无关 —— 禁的是"商业渔船在该水域内"，
                # 不是"正在下网"，所以过境同样算。
                # 这条同时消掉一个反向误报：原先任何 ≤3 节的船都算"捕捞"，
                # 在禁渔区低速锚泊的货船会被误报。
                if vtype in banned:
                    judge, suspect_only = "vessel_type", False
                elif vtype in suspect:
                    judge, suspect_only = "vessel_type_suspect", True
                else:
                    judge, suspect_only = "type_not_banned", False
                runs = (_contiguous_runs(segment.points,
                                         lambda p: in_zone(p) and season_ok(p))
                        if vtype in banned or vtype in suspect else [])
            else:
                # 【退回速度判据】船型拿不到时保持原行为，避免判据整体失效。
                # features.judge 会写明这条事件是靠速度代理得出的，便于事后审计。
                judge, suspect_only = "low_speed", False
                runs = _contiguous_runs(
                    segment.points,
                    lambda p: (in_zone(p) and p.speed <= config.low_speed_enter_knots
                               and season_ok(p)),
                )

            for run in runs:
                duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60  # 持续时长（分钟）
                if len(run) < config.fishing_min_points or duration < config.fishing_min_minutes:  # 点数太少或时间太短
                    continue  # 跳过
                earlier = [point.speed for point in segment.points if point.timestamp < run[0].timestamp]  # 事件前的速度列表
                prior_speed = median(earlier) if earlier else None  # 事件前的速度中位数
                event_id = _event_id(  # 生成事件ID
                    segment.mmsi,  # MMSI
                    CandidateType.ILLEGAL_FISHING,  # 事件类型
                    run[0],  # 起始点
                    run[-1],  # 结束点
                    zone_id,  # 区域ID
                )
                events.append(  # 添加违规捕捞事件
                    CandidateEvent(
                        event_id=event_id,  # 事件ID
                        source_event_ids=[event_id],  # 来源事件ID
                        mmsi=segment.mmsi,  # MMSI
                        # 【这个码曾经接错】原先这里产的是 LOW_SPEED_FISHING（"低速捕捞
                        # 嫌疑"，严重度 55），而本分支判的是**禁渔区 + 禁渔期**内的捕捞——
                        # 那是法定违规（illegal_fishing，严重度 75）。结果是更重的码没有
                        # 检测器、永远为 0，更轻的嫌疑码反而占着法定判据。
                        candidate_type=CandidateType.ILLEGAL_FISHING,  # 候选事件类型
                        start_time=run[0].timestamp,  # 开始时间
                        end_time=run[-1].timestamp,  # 结束时间
                        zone_id=zone_id,  # 区域ID
                        zone_name=zone_name,  # 区域名称
                        # 船型判据下"在场即违规"，严重度不受速度影响，直接 high；
                        # 速度代理判据保留原有的"事件前是否正常航行"定档逻辑。
                        # 船型判据下"在场即违规"，严重度不受速度影响，直接 high；
                        # 但"疑似"（报通用渔船码、分不出商业/休闲）降一档，
                        # 因为结论没有被船型确证，需要人工确认。
                        priority=("high" if judge == "vessel_type"
                                  else "middle" if suspect_only
                                  else "high"
                                  if prior_speed is not None  # 如果事件前速度已知
                                  and prior_speed >= config.prior_normal_speed_knots  # 且之前正常速度行驶
                                  else "middle"),  # 否则为中优先级
                        features={  # 特征
                            "duration_minutes": round(duration, 2),  # 持续分钟数
                            # 与 duration_minutes 同义的显式字段：三级分档按它定档，
                            # 单列出来是为了让"定档依据"在 features 里自解释
                            "fishing_duration_minutes": round(duration, 2),  # 作业持续（分档量级）
                            "minimum_speed": min(point.speed for point in run),  # 最低速度
                            "average_speed": sum(point.speed for point in run) / len(run),  # 平均速度
                            "prior_median_speed": prior_speed,  # 事件前速度中位数
                            "point_count": len(run),  # 点数
                            "fishing_allowed": False,  # 该区域禁止捕鱼
                            "season_active": bool(time_rules),  # 是否有生效的禁渔期规则
                            # 判定依据自解释：vessel_type=按船型（法定口径），
                            # low_speed=船型缺失时退回的速度代理。审阅证据链时
                            # 一眼能看出这条结论是不是靠代理量得出的。
                            "judge": judge,
                            "vessel_type": vtype,
                            "fishing_ban_scope": scope,
                            "fishing_ban_scope_desc": scope_desc,
                            # 结论的确证程度：confirmed=船型码直接落在禁令范围内；
                            # suspect=报的是通用渔船码，分不出商业/休闲，待人工确认。
                            "commercial_status": ("suspect" if suspect_only
                                                  else "confirmed"
                                                  if judge == "vessel_type"
                                                  else "by_speed_proxy"),
                        },
                        evidence_points=run,  # 证据点
                        quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
                    )
                )

        if restrictions.get("anchoring_allowed") is False:  # 如果该区域禁止锚泊
            events.extend(  # 收集越界锚泊事件
                _detect_anchoring_events(segment, config, zone_id, zone_name, in_zone)  # 区域内锚泊态区间
            )
    return events  # 返回所有区域相关事件


def _detect_anchoring_events(  # 越界锚泊检测（区域内禁止锚泊 + 实测锚泊态）
    segment: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
    zone_id: str,  # 区域ID
    zone_name: str,  # 区域名称
    in_zone: Callable[[AISPoint], bool],  # 点是否在该区域内的判定函数
) -> list[CandidateEvent]:
    """检测"在禁止锚泊的水域内保持锚泊态"的连续区间。

    口径说明：这里实现的是**禁锚区**读法（区域声明 `anchoring_allowed: false`，
    船舶在该区域内保持锚泊态即为越界锚泊），与既有的 `fishing_allowed` 同构。
    不做"锚地范围外锚泊"的全局读法——那要求锚地边界数据完整，缺一个锚地就会
    把合法锚泊判成违规，误报代价不对称。

    当前 `regulations.json` 里没有任何区域声明 `anchoring_allowed`，
    所以本分支处于休眠状态；配置了禁锚区即可触发。
    """
    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(  # 查找"在区域内且锚泊"的连续区间
        segment.points,  # 轨迹点
        lambda point: in_zone(point) and point.speed <= config.anchoring_speed_knots,  # 区域内 + 锚泊速度
    ):
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 锚泊持续（分钟）
        if len(run) < config.anchoring_min_points or duration < config.anchoring_min_minutes:  # 太短
            continue  # 不算锚泊
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.ANCHORING_OUT_OF_BOUNDS,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
            zone_id,  # 区域ID
        )
        events.append(  # 添加越界锚泊事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.ANCHORING_OUT_OF_BOUNDS,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                zone_id=zone_id,  # 区域ID
                zone_name=zone_name,  # 区域名称
                priority="high" if duration >= 120.0 else "middle",  # 长时间锚泊为高优先级
                features={  # 特征
                    "anchoring_allowed": False,  # 该区域禁止锚泊
                    "anchorage": zone_name,  # 所在水域（锚地/禁锚区名称）
                    "anchoring_duration_minutes": round(duration, 2),  # 锚泊持续（分档量级）
                    "duration_minutes": round(duration, 2),  # 同义字段（统一叙事用）
                    "average_speed": round(sum(point.speed for point in run) / len(run), 2),  # 平均速度
                    "point_count": len(run),  # 点数
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有越界锚泊事件


def _detect_fairway_anchoring_events(  # 航道内锚泊
    segment: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
    water: WaterChecker,  # 航道走廊判定器
) -> list[CandidateEvent]:
    """在航道走廊内保持锚泊态的连续区间。

    与 `anchoring_out_of_bounds`（越界锚泊）的分工：
      - 越界锚泊靠 `regulations.json` 的**人工禁锚区**（区域声明
        `anchoring_allowed: false`）。需要有人先把禁锚水域画出来——实测该文件里
        没有任何区域声明它，所以那条分支一直休眠；而不画区域并不等于没有碍航锚泊。
      - 航道内锚泊靠**数据驱动的航道走廊**（`water.corridor` = 船队实际跑过、
        密度 ≥ `CORRIDOR_MIN_DENSITY` 的格网）。不需要人工画区域，开箱即可用。

    为什么"在走廊内 + 锚泊态"就足以定性：走廊的定义就是"船队反复航行的地方"，
    即通航水域；一艘船在那里保持锚泊态就是碍航。这不需要先知道当地有没有
    发布过禁锚通告——判据完全来自数据本身。

    时长阈值为什么用 `fairway_anchoring_min_minutes`（10 分钟）而不是越界锚泊的
    `anchoring_min_minutes`（30 分钟）：走廊是**通航水域**，停上 10 分钟就已经
    构成碍航；而禁锚区是专门划定的禁泊水域，用更长的 30 分钟是为了先确认
    "确实是锚泊，而不是过路船短暂的零速抖动"。两个阈值各自贴合自己的语义，
    不共用一个。
    """
    max_speed = config.anchoring_speed_knots  # 锚泊态速度上限（节）
    min_points = config.anchoring_min_points  # 锚泊态最少点数
    min_minutes = config.fairway_anchoring_min_minutes  # 最短持续（分钟）
    corridor = water.corridor  # 航道走廊格网集合（(cx, cy) 的 set）

    def is_candidate(point: AISPoint) -> bool:  # 单点是否属于"航道内锚泊"
        if point.speed > max_speed:  # 速度超过锚泊阈值
            return False  # 不是锚泊态
        return cell_of(point.lon, point.lat) in corridor  # 且落在航道走廊格网内

    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(segment.points, is_candidate):  # 查找连续"航道内锚泊"区间
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 锚泊持续（分钟）
        if len(run) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 不算锚泊
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.FAIRWAY_ANCHORING,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
        )
        events.append(  # 添加航道内锚泊事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.FAIRWAY_ANCHORING,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                # 走廊是数据驱动产物、没有区域档案，故 zone_id/zone_name 留空；
                # 具体位置看 evidence_points
                priority="high" if duration >= 120.0 else "middle",  # 长时间碍航为高
                features={  # 特征
                    "anchoring_duration_minutes": round(duration, 2),  # 锚泊持续（细类分档量级）
                    "duration_minutes": round(duration, 2),  # 同义字段（统一叙事用）
                    "average_speed": round(sum(point.speed for point in run) / len(run), 2),  # 平均速度
                    "point_count": len(run),  # 点数
                    "in_corridor": True,  # 显式标记：位于数据驱动航道走廊内
                    "anchoring_speed_threshold": max_speed,  # 当时的锚泊态速度阈值
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有航道内锚泊事件


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


def _detect_ais_off_events(  # AIS 报文内容不可用
    segment: TrajectorySegment,  # 轨迹分段
    config: AcceleratorConfig,  # 加速器配置
) -> list[CandidateEvent]:
    """报文**收到了、但导航要素整体不可用**的连续区间。

    与 `ais_gap` 严格互斥，两个码各管一件事：
      - `ais_gap` = 时间上**没收到**报文（设备关机/信号遮蔽），quality 层按
        `config.ais_gap_minutes` 出 `gap_minutes` 并打 `ais_gap` 标记。
      - `ais_off` = 报文**收到了、内容却不可用**（设备故障，或关闭发射后的残报），
        判据在 quality 层落成 `ais_off` 标记——SOG 与 COG 同时取 AIS 标准的
        "不可用"编码，具体口径见 `quality._unavailable_nav_fields` 的注释。

    本函数只负责把 quality 层**逐点**打的标记**聚合成一个区间事件**：逐点出事件会
    得到几十条同质记录，聚合成区间才谈得上"持续了多久"这个定档依据。
    与 `_detect_quality_events` 的分工也正在此——那条路径服务 `ais_gap` /
    `position_jump`（逐点事件），这条服务 `ais_off`（区间事件）。所以 `ais_off`
    **刻意不**进 `_detect_quality_events` 的 (flag, type) 元组，否则同一现象会同时
    产出逐点事件和区间事件两份。
    """
    min_points = config.ais_off_min_points  # 最少点数
    min_minutes = config.ais_off_min_minutes  # 最短持续（分钟）

    events: list[CandidateEvent] = []  # 候选事件列表
    for run in _contiguous_runs(  # 查找连续"导航要素不可用"区间
        segment.points,  # 轨迹点
        lambda point: "ais_off" in point.quality_flags,  # 该点被 quality 层标为不可用
    ):
        duration = (run[-1].timestamp - run[0].timestamp).total_seconds() / 60.0  # 不可用持续（分钟）
        if len(run) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 跳过
        fields = sorted(  # 区间内实际不可用的导航要素（去重后排序，便于展示与断言）
            {
                name  # 要素名（sog / cog / ...）
                for issue in _issues_for_points(segment.quality_issues, run)  # 区间内的质量问题
                if issue.code == "ais_off"  # 只看导航要素不可用
                for name in issue.details.get("unavailable_fields", [])  # 取其要素明细
            }
        )
        gaps = [  # 相邻两条不可用报文之间的实际间隔
            (current.timestamp - previous.timestamp).total_seconds() / 60.0  # 间隔（分钟）
            for previous, current in zip(run, run[1:])  # 相邻点对
        ]
        event_id = _event_id(  # 生成事件ID
            segment.mmsi,  # MMSI
            CandidateType.AIS_OFF,  # 事件类型
            run[0],  # 起始点
            run[-1],  # 结束点
        )
        events.append(  # 添加导航要素不可用事件
            CandidateEvent(
                event_id=event_id,  # 事件ID
                source_event_ids=[event_id],  # 来源事件ID
                mmsi=segment.mmsi,  # MMSI
                candidate_type=CandidateType.AIS_OFF,  # 候选事件类型
                start_time=run[0].timestamp,  # 开始时间
                end_time=run[-1].timestamp,  # 结束时间
                priority="high" if duration >= 120.0 else "middle",  # 长时间不可用为高
                features={  # 特征
                    "unavailable_minutes": round(duration, 2),  # 不可用持续（细类分档量级）
                    "duration_minutes": round(duration, 2),  # 同义字段（统一叙事用）
                    "unavailable_points": len(run),  # 不可用报文条数
                    "unavailable_fields": fields,  # 具体哪些导航要素不可用
                    "max_gap_minutes": round(max(gaps), 2) if gaps else 0.0,  # 报文最大间隔
                },
                evidence_points=run,  # 证据点
                quality_issues=_issues_for_points(segment.quality_issues, run),  # 质量问题
            )
        )
    return events  # 返回所有导航要素不可用事件


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