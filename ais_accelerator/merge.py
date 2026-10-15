"""
ais_accelerator/merge.py — 候选事件合并
=========================================

【这个文件做什么？】
  将检测到的候选事件按时空临近关系合并。
  同一条船在同一区域、相近时间触发的多个事件合并为一条。

【为什么需要合并？】
  - 船在禁渔区低速航行可能同时触发 LOW_SPEED_FISHING 和 COURSE_ANOMALY
  - 一个超速行为被检测成连续多个 SPEED_VIOLATION
  - 合并后：1 条复合事件 vs N 条零散事件

【合并规则】
  1. 同类型事件且时间/距离在阈值内 → 合并
  2. 兼容类型（如 LOW_SPEED_FISHING + COURSE_ANOMALY）→ 合并
  3. 合并后类型取优先级最高的，相关类型记在 related_types
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from .config import AcceleratorConfig  # 加速器配置
from .geometry import haversine_nm  # Haversine距离计算
from .models import CandidateEvent, CandidateType  # 候选事件和事件类型


COMPATIBLE_TYPES = {  # 可合并的事件类型对
    frozenset({CandidateType.LOW_SPEED_FISHING, CandidateType.COURSE_ANOMALY}),  # 低速捕捞 + 航向异常
    frozenset({CandidateType.AIS_GAP, CandidateType.POSITION_JUMP}),  # AIS中断 + 位置跳变
    # AIS_OFF（报文在但内容不可用）与 POSITION_JUMP 同源于"这一串报文本身就是坏的"，
    # 与上面 AIS_GAP + POSITION_JUMP 是同一类配对。
    # **不把 AIS_GAP 与 AIS_OFF 配成一对**：两者判据互斥（没收到报文 / 收到了但没用），
    # 真要合并就等于把两种不同的法定义务钉成一个码。
    frozenset({CandidateType.AIS_OFF, CandidateType.POSITION_JUMP}),  # 导航要素不可用 + 位置跳变
}

# 这些类型的 features 里带 target_mmsi（对方船），是核心证据，
# 因此**不同目标船**的事件不能合并（见 _can_merge 的护栏）。
PAIRWISE_TYPES = {  # 船-船事件类型
    CandidateType.COLLISION_RISK,  # 碰撞风险
    CandidateType.OVERTAKING_VIOLATION,  # 违规追越
    CandidateType.GIVE_WAY_FAILURE,  # 让路船未让路（同一条船可同时对多条船负让路义务）
    CandidateType.ILLEGAL_TOWING,  # 违规拖带/并靠（拖带对象必须是同一条船）
}

# 数值只表达合并时的取舍，与 violation_catalog.CODE_SEVERITY 是两把尺（后者定展示
# 主类型）。但相对次序照着后者排，免得同一对事件在"展示"和"合并"两处选出不同的主类型。
# 缺项不会崩（_merge_pair 用 .get 兜底为 0），只会让新类型在合并时永远输——所以每个码都要在。
TYPE_PRIORITY = {  # 事件类型优先级（数值越高优先级越高）
    CandidateType.RESTRICTED_AREA_ENTRY: 10,  # 禁航区进入（最高）
    CandidateType.ANCHORING_OUT_OF_BOUNDS: 9,  # 越界锚泊
    CandidateType.FAIRWAY_ANCHORING: 9,  # 航道内锚泊（同为"停在了不该停的水域"）
    CandidateType.COLLISION_RISK: 8,  # 碰撞风险
    CandidateType.OVERTAKING_VIOLATION: 7,  # 违规追越
    CandidateType.WRONG_WAY_NAVIGATION: 7,  # 逆向航行
    CandidateType.GIVE_WAY_FAILURE: 7,  # 让路船未让路（同上两者同级的规则违反）
    CandidateType.ILLEGAL_FISHING: 6,  # 违规捕捞（法定违规）
    CandidateType.SPEED_VIOLATION: 6,  # 超速
    CandidateType.ILLEGAL_TOWING: 6,  # 违规拖带/并靠
    CandidateType.LOW_SPEED_FISHING: 5,  # 低速捕捞嫌疑
    CandidateType.AIS_OFF: 4,  # 导航要素不可用
    CandidateType.AIS_GAP: 4,  # AIS中断
    CandidateType.POSITION_JUMP: 3,  # 位置跳变
    CandidateType.COURSE_ANOMALY: 2,  # 航向异常（最低）
}


def _compatible(first: CandidateEvent, second: CandidateEvent) -> bool:  # 判断两个事件类型是否可合并
    return first.candidate_type == second.candidate_type or frozenset(  # 同类型 或 在兼容对中
        {first.candidate_type, second.candidate_type}  # 两个事件的类型集合
    ) in COMPATIBLE_TYPES  # 检查是否属于可合并类型对


def _can_merge(  # 判断两个事件是否可以合并（时空临近性）
    first: CandidateEvent,  # 第一个事件
    second: CandidateEvent,  # 第二个事件
    config: AcceleratorConfig,  # 加速器配置
) -> bool:
    if first.mmsi != second.mmsi or not _compatible(first, second):  # 不同船或类型不兼容
        return False  # 不能合并
    if first.zone_id != second.zone_id:  # 不同区域
        return False  # 不能合并
    if first.candidate_type in PAIRWISE_TYPES:  # 船-船事件
        # 一艘船可以同时追越/危险接近**多条**船，target_mmsi 是判断"对谁"的唯一依据。
        # 合并会把两条不同目标的记录压成一条，另一条的证据与相对方位就丢了。
        if first.features.get("target_mmsi") != second.features.get("target_mmsi"):  # 目标船不同
            return False  # 不能合并
    gap = (second.start_time - first.end_time).total_seconds() / 60  # 时间间隔（分钟）
    if gap > config.merge_gap_minutes:  # 超过时间合并阈值
        return False  # 不能合并
    if not first.evidence_points or not second.evidence_points:  # 缺少证据点
        return True  # 可以合并（无法精确判断时保守合并）
    left = first.evidence_points[-1]  # 第一个事件最后一个证据点
    right = second.evidence_points[0]  # 第二个事件第一个证据点
    return (  # 判断距离是否在合并阈值内
        haversine_nm(left.lat, left.lon, right.lat, right.lon)  # 计算两点间距离（海里）
        <= config.merge_distance_nm  # 不超过距离合并阈值
    )


def _merge_pair(first: CandidateEvent, second: CandidateEvent) -> CandidateEvent:  # 合并两个事件为一个
    primary = max(  # 取优先级最高的事件类型作为主类型
        (first.candidate_type, second.candidate_type),  # 两个事件类型
        # 用 .get 兜底：以后再加枚举类型时，忘了登记 TYPE_PRIORITY 只会退化为
        # "优先级 0"，而不是在合并阶段抛 KeyError 把整条船的管线打断。
        key=lambda value: TYPE_PRIORITY.get(value, 0),  # 按优先级排序
    )
    related = {  # 收集所有相关类型
        *first.related_types,  # 第一个事件的相关类型
        *second.related_types,  # 第二个事件的相关类型
        first.candidate_type,  # 第一个事件的主类型
        second.candidate_type,  # 第二个事件的主类型
    }
    related.discard(primary)  # 移除主类型
    points = {  # 用字典去重合并证据点（时间+坐标作为键）
        (
            point.timestamp.isoformat(),  # 时间戳
            point.lat,  # 纬度
            point.lon,  # 经度
        ): point  # AIS点
        for point in [*first.evidence_points, *second.evidence_points]  # 两个事件的证据点
    }
    evidence = sorted(points.values(), key=lambda point: point.timestamp)  # 按时间排序
    source_ids = list(  # 合并并去重来源事件ID
        dict.fromkeys(  # 用有序字典去重
            [
                *(first.source_event_ids or [first.event_id]),  # 第一个事件的来源ID
                *(second.source_event_ids or [second.event_id]),  # 第二个事件的来源ID
            ]
        )
    )
    return CandidateEvent(  # 返回合并后的事件
        event_id=first.event_id,  # 使用第一个事件的事件ID
        source_event_ids=source_ids,  # 来源事件ID列表
        mmsi=first.mmsi,  # MMSI
        candidate_type=primary,  # 主事件类型
        related_types=sorted(related, key=lambda value: value.value),  # 相关类型列表（按枚举值排序）
        start_time=min(first.start_time, second.start_time),  # 取较早的开始时间
        end_time=max(first.end_time, second.end_time),  # 取较晚的结束时间
        zone_id=first.zone_id,  # 区域ID
        zone_name=first.zone_name or second.zone_name,  # 区域名称（取非空值）
        priority="high" if "high" in {first.priority, second.priority} else "middle",  # 如果任一为高则合并后为高
        features={  # 合并特征
            **first.features,  # 第一个事件的特征
            **second.features,  # 第二个事件的特征
            "merged_event_count": len(source_ids),  # 合并的事件数
        },
        evidence_points=evidence,  # 合并后的证据点
        quality_issues=[*first.quality_issues, *second.quality_issues],  # 合并质量问题列表
    )


def merge_events(  # 对所有候选事件进行合并（按MMSI、区域、时间排序后依次合并）
    events: list[CandidateEvent], config: AcceleratorConfig  # 候选事件列表, 加速器配置
) -> list[CandidateEvent]:
    ordered = sorted(  # 按多维排序
        events,  # 候选事件列表
        key=lambda event: (  # 排序键：MMSI→区域→开始时间→事件类型
            event.mmsi,  # 按MMSI分组
            event.zone_id or "",  # 按区域分组
            event.start_time,  # 按时间排序
            event.candidate_type.value,  # 按事件类型排序
        ),
    )
    merged: list[CandidateEvent] = []  # 合并后的事件列表
    for event in ordered:  # 遍历排序后的事件
        if merged and _can_merge(merged[-1], event, config):  # 如果与上一个事件可合并
            merged[-1] = _merge_pair(merged[-1], event)  # 合并到上一个事件
        else:  # 不能合并
            merged.append(event)  # 作为新事件加入列表
    return merged  # 返回合并后的事件列表