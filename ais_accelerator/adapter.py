"""
ais_accelerator/adapter.py — 向后兼容适配器
=============================================

【这个文件做什么？】
  将加速器输出的 CandidatePackage 格式转为旧版
  ShipViolationDetector.analyze() 兼容的 ShipData 格式。

【为什么需要这个？】
  新加速器的输出数据结构和旧 Agent 的输入格式不完全一致。
  这个适配器做格式转换，让新旧系统可以互操作。

【核心功能】
  - build_agent_input(): 将加速器结果包装为 ShipData 格式
  - _precomputed_evidence(): 预计算三条证据链（规则/轨迹/AIS质量）
  - _candidate_finding(): 将不同类型的事件转为人类可读的描述文本
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from typing import Any  # 任意类型



from .models import (  # 数据模型
    AISPoint,  # AIS数据点
    CandidateEvent,  # 候选事件
    MatchedRule,  # 匹配的规则
    QualityReport,  # 质量报告
    ShipProfile,  # 船舶概要
)


ACCELERATION_CONTRACT = "ais-accelerator/v1"  # 加速器输出合约版本号


def _confidence_for_priority(priority: str) -> float:  # 根据事件优先级返回对应的置信度
    return {"high": 0.90, "middle": 0.80, "low": 0.65}.get(priority, 0.75)  # 高0.9, 中0.8, 低0.65, 默认0.75


def _compact_number(value: Any) -> str:  # 将数值格式化为紧凑字符串（去除多余尾随零）
    if isinstance(value, float):  # 如果是浮点数
        return f"{value:.2f}".rstrip("0").rstrip(".")  # 保留两位小数，去除尾随零和小数点
    return str(value)  # 否则直接转字符串


def _candidate_finding(event: CandidateEvent) -> str:  # 将候选事件类型转换为人类可读的描述文本
    features = event.features  # 事件特征字典
    candidate_type = event.candidate_type.value  # 事件类型字符串
    if candidate_type == "low_speed_fishing":  # 低速捕捞事件
        return (  # 拼接描述文本
            "低速疑似捕捞: "
            f"均速{_compact_number(features.get('average_speed', '未知'))}节, "  # 平均速度
            f"最低{_compact_number(features.get('minimum_speed', '未知'))}节, "  # 最低速度（原字段名拼错成 minimum_sp eed，永远取到"未知"）
            f"持续{_compact_number(features.get('duration_minutes', '未知'))}分钟"  # 持续时长
        )
    if candidate_type == "illegal_fishing":  # 违规捕捞事件（禁渔区/禁渔期内的法定违规）
        return (  # 拼接描述文本
            "违规捕捞: "
            f"在禁止捕鱼水域{event.zone_name or event.zone_id or ''}内低速作业"  # 区域名
            f"{_compact_number(features.get('fishing_duration_minutes', '未知'))}分钟, "  # 作业时长（分档量值）
            f"均速{_compact_number(features.get('average_speed', '未知'))}节, "  # 平均速度
            f"作业前主流速{_compact_number(features.get('prior_median_speed', '未知'))}节"  # 事件前的正常航速
        )
    if candidate_type == "speed_violation":  # 超速事件
        # 判据有两路：区域限速（zone_limit）与数据驱动（absolute/relative 同水域主流速）
        judge = features.get("judge")  # 判据来源
        if judge in ("absolute", "relative"):  # 数据驱动：没有区域，参照物是同水域主流速
            basis = "绝对异常" if judge == "absolute" else "远超同水域主流速"
            return (  # 拼接描述文本
                f"数据驱动超速({basis}): "
                f"最高{_compact_number(features.get('max_speed', '未知'))}节, "
                f"同水域主流速{_compact_number(features.get('reference_speed', '未知'))}节, "
                f"判定阈值{_compact_number(features.get('speed_limit', '未知'))}节"
            )
        return (  # 拼接描述文本
            "区域超速: "
            f"最高{_compact_number(features.get('max_speed', '未知'))}节, "  # 最高速度
            f"限速{_compact_number(features.get('speed_limit', '未知'))}节"  # 区域限速
        )
    if candidate_type == "restricted_area_entry":  # 进入禁航区事件
        return f"进入限制区域: {event.zone_name or event.zone_id or '未知区域'}"  # 区域名称
    if candidate_type == "overtaking_violation":  # 违规追越事件（COLREG 第13条）
        return (  # 拼接描述文本
            "违规追越: "
            f"追越 {features.get('target_mmsi', '未知')} 时最近横距"  # 被追越船
            f"{_compact_number(features.get('min_distance_nm', '未知'))}海里（安全线"
            f"{_compact_number(features.get('unsafe_distance_nm', '未知'))}海里）, "  # 判定安全线
            f"速度差{_compact_number(features.get('max_speed_diff_knots', '未知'))}节, "  # 峰值速度差
            f"从被追越船船首{_compact_number(features.get('avg_relative_bearing', '未知'))}度方向赶上"  # 相对方位
        )
    if candidate_type == "wrong_way_navigation":  # 逆向航行事件（COLREG 第10条分道通航制）
        return (  # 拼接描述文本
            "逆向航行: "
            f"偏离该水域主流向{_compact_number(features.get('course_offset_degrees', '未知'))}度, "  # 偏离角
            f"持续{_compact_number(features.get('duration_minutes', '未知'))}分钟, "  # 持续时长
            f"均速{_compact_number(features.get('average_speed', '未知'))}节"  # 平均速度
        )
    if candidate_type == "anchoring_out_of_bounds":  # 越界锚泊事件
        return (  # 拼接描述文本
            f"越界锚泊: {features.get('anchorage') or event.zone_name or '锚地'}内禁止锚泊, "  # 锚地名
            f"已锚泊{_compact_number(features.get('anchoring_duration_minutes', '未知'))}分钟, "  # 锚泊时长
            f"均速{_compact_number(features.get('average_speed', '未知'))}节"  # 平均速度（锚泊态应接近 0）
        )
    if candidate_type == "fairway_anchoring":  # 航道内锚泊事件（数据驱动航道走廊）
        return (  # 拼接描述文本
            "航道内锚泊: "  # 与越界锚泊的区别：不是在禁锚水域，而是在船队航行走廊里
            f"位于航道走廊内, 已锚泊"  # 走廊来自实测密度，不是区域配置
            f"{_compact_number(features.get('anchoring_duration_minutes', '未知'))}分钟, "  # 锚泊时长
            f"均速{_compact_number(features.get('average_speed', '未知'))}节"  # 平均速度（锚泊态应接近 0）
        )
    if candidate_type == "collision_risk":  # 碰撞风险事件
        return (  # 拼接描述文本
            "碰撞风险: "
            f"与 {features.get('target_mmsi', '未知')} 的最近会遇距离"  # 目标船
            f"{_compact_number(features.get('dcpa_nm', '未知'))}海里, "  # DCPA
            f"到最近会遇点{_compact_number(features.get('tcpa_minutes', '未知'))}分钟, "  # TCPA
            f"碰撞风险指数{_compact_number(features.get('cri', '未知'))}"  # CRI
        )
    if candidate_type == "give_way_failure":  # 让路船未让路（COLREG 第13/14/15条，归责）
        # encounter_type 在 features 里是英文码（"crossing"/"head_on"/"overtaking"），
        # 这里翻成中文再拼进给 Agent 的文本，免得结语里冒英文词
        encounter_label = {  # 相遇态势中文名
            "overtaking": "追越",
            "head_on": "对遇",
            "crossing": "交叉相遇",
        }.get(features.get("encounter_type"), "相遇")
        return (  # 拼接描述文本
            f"让路船未让路（{encounter_label}局面）: "
            f"本船负有让路义务（COLREG 第{features.get('colreg_rule', '?')}条）, "  # 依据条款
            f"与 {features.get('target_mmsi', '未知')} 实际最近通过"  # 对方船
            f"{_compact_number(features.get('distance_nm', '未知'))}海里"  # **实测**最近距离（本码判据量）
            f"（判据线{_compact_number(features.get('close_quarters_threshold_nm', '未知'))}海里）, "  # 紧迫局面距离线
            # 这里刻意报实测距离而不是 dcpa_nm/tcpa_minutes：那两项是接近过程起点上的
            # **预测**值（含义已写进 colreg.py 的 features 注释），给 Agent 读的文本里
            # 用实测值，免得把"预测会靠多近"当成"实际靠了多近"。
            f"接近前碰撞风险指数{_compact_number(features.get('cri', '未知'))}, "  # 起点无行动预测（细类档）
            f"此前仅改向{_compact_number(features.get('course_alteration_degrees', '未知'))}度"  # 实际改向幅度
            f"（应达{_compact_number(features.get('alteration_threshold_degrees', '未知'))}度）, "  # 判定阈值
            f"速度变化{_compact_number(features.get('speed_change_knots', '未知'))}节"  # 速度变化
        )
    if candidate_type == "illegal_towing":  # 违规拖带/并靠事件
        return (  # 拼接描述文本
            "违规拖带/并靠: "
            f"与 {features.get('target_mmsi', '未知')} 长时间保持"  # 对方船
            f"{_compact_number(features.get('avg_separation_nm', '未知'))}海里间距"  # 平均间距
            f"（最近{_compact_number(features.get('min_separation_nm', '未知'))}海里）, "  # 最近间距
            f"速度差{_compact_number(features.get('speed_diff_knots', '未知'))}节, "  # 速度差
            f"航向差{_compact_number(features.get('course_diff_degrees', '未知'))}度, "  # 航向差
            f"持续{_compact_number(features.get('duration_minutes', '未知'))}分钟"  # 持续时长
        )
    if candidate_type == "ais_gap":  # AIS信号中断事件
        return f"AIS信号中断: {json_compact(features)}"  # 特征详情
    if candidate_type == "ais_off":  # AIS导航要素不可用事件（与 ais_gap 的区别：报文收到了）
        return (  # 拼接描述文本
            "AIS导航要素不可用: "
            f"{features.get('unavailable_points', '未知')}个报文的位置/航向/航速整体为空值, "  # 不可用报文数
            f"累计{_compact_number(features.get('unavailable_minutes', '未知'))}分钟, "  # 累计时长
            f"涉及字段{','.join(features.get('unavailable_fields') or []) or '未知'}"  # 具体字段名
        )
    if candidate_type == "position_jump":  # 位置跳变事件
        return f"位置跳变: {json_compact(features)}"  # 特征详情
    if candidate_type == "course_anomaly":  # 航向异常事件
        return (  # 拼接描述文本
            "航向突变: "
            f"变化{_compact_number(features.get('course_change_degrees', '未知'))}度"  # 航向变化角度
        )
    return f"候选异常: {candidate_type}"  # 其他事件类型


def json_compact(value: Any) -> str:  # 紧凑JSON序列化（用于短证据字符串）
    """Compact deterministic representation for short evidence strings."""
    import json  # JSON模块

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)  # 紧凑格式序列化


def _compact_rule_hits(rules: list[MatchedRule]) -> list[dict[str, Any]]:  # 将规则列表压缩为简洁字典列表
    return [  # 返回压缩后的规则信息
        {  # 单条规则信息
            "id": rule.rule_id,  # 规则ID
            "code": rule.violation_code,  # 违规代码
            "label": rule.label,  # 规则标签
            **({"limit": rule.speed_limit} if rule.speed_limit is not None else {}),  # 限速值（如有）
        }
        for rule in rules  # 遍历规则列表
    ]


def _precomputed_evidence(  # 预计算三条证据链（规则分析、轨迹分析、AIS数据质量）
    event: CandidateEvent,  # 候选事件
    matched_rules: list[MatchedRule],  # 匹配的规则列表
    quality_report: QualityReport,  # 质量报告
) -> dict[str, Any]:
    priority_confidence = _confidence_for_priority(event.priority)  # 根据事件优先级获取置信度
    if matched_rules:  # 如果有匹配的规则
        rule_result = {  # 规则分析结果
            "status": "违规待确认",  # 状态
            "findings": [  # 发现详情
                f"命中{rule.label}({rule.violation_code})"  # 规则标签和代码
                + (f"，限速{rule.speed_limit:g}节" if rule.speed_limit is not None else "")  # 限速信息
                for rule in matched_rules  # 遍历匹配规则
            ],
            "matched_violations": [rule.violation_code for rule in matched_rules],  # 匹配违规代码列表
            "confidence": priority_confidence,  # 置信度
        }
    else:  # 没有匹配的规则
        rule_result = {  # 规则分析结果（信息不足）
            "status": "信息不足",  # 状态
            "findings": ["规则预筛未命中明确规则"],  # 发现详情
            "matched_violations": [],  # 无匹配违规
            "confidence": 0.60,  # 低置信度
        }

    trajectory_result = {  # 轨迹分析结果
        "status": "异常待确认",  # 状态
        "anomalies": [_candidate_finding(event)],  # 异常描述
        "confidence": priority_confidence,  # 置信度
    }

    quality_level = quality_report.level  # 数据质量等级
    issue_codes = sorted({issue.code for issue in quality_report.issues})  # 所有质量问题的代码（去重排序）
    ais_result = {  # AIS数据质量分析结果
        "status": "数据可信" if quality_level == "good" else "存在异常",  # 状态
        "ais_quality": quality_level,  # 质量等级
        "findings": ["质量检查通过"] if not issue_codes else [f"质量问题:{','.join(issue_codes)}"],  # 发现详情
        "confidence": {"good": 0.95, "fair": 0.75, "poor": 0.40}[quality_level],  # 根据质量等级确定置信度
    }
    return {  # 返回三条证据链
        "rule_result": rule_result,  # 规则分析结果
        "trajectory_result": trajectory_result,  # 轨迹分析结果
        "ais_result": ais_result,  # AIS数据质量结果
    }


def build_agent_input(  # 构建Agent输入数据（兼容旧版ShipData格式）
    ship: ShipProfile,  # 船舶概要
    event: CandidateEvent,  # 候选事件
    history: list[AISPoint],  # 历史轨迹点（压缩后的）
    current: AISPoint,  # 当前点（事件结束前的最后一个点）
    matched_rules: list[MatchedRule],  # 匹配的规则列表
    quality_report: QualityReport,  # 质量报告
) -> dict[str, Any]:
    event_time = f"{event.start_time.isoformat()}~{event.end_time.isoformat()}"  # 事件时间范围字符串
    context = {  # 构建上下文信息
        "acceleration_contract": ACCELERATION_CONTRACT,  # 加速器合约版本
        "current_zone": event.zone_name or event.zone_id or "未知区域",  # 当前区域
        "time_info": event_time,  # 时间信息
        "candidate": {  # 候选事件概要
            "type": event.candidate_type.value,  # 事件类型
            "priority": event.priority,  # 优先级
        },
        "rule_hits": _compact_rule_hits(matched_rules),  # 命中的规则摘要
        "precomputed_evidence": _precomputed_evidence(  # 预计算证据链
            event, matched_rules, quality_report  # 候选事件、匹配规则、质量报告
        ),
    }
    raw = {  # 构建原始输入数据
        "ship": ship.model_dump(mode="json"),  # 船舶信息
        "current_position": current.to_agent_position(),  # 当前位置
        "history": [point.to_agent_position() for point in history],  # 历史轨迹
        "context": context,  # 上下文
    }
    return ShipData(**raw).model_dump(mode="json")  # 转换为ShipData格式并返回JSON