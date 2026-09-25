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
            f"最低{_compact_number(features.get('minimum_sp eed', '未知'))}节, "  # 最低速度
            f"持续{_compact_number(features.get('duration_minutes', '未知'))}分钟"  # 持续时长
        )
    if candidate_type == "speed_violation":  # 超速事件
        return (  # 拼接描述文本
            "区域超速: "
            f"最高{_compact_number(features.get('max_speed', '未知'))}节, "  # 最高速度
            f"限速{_compact_number(features.get('speed_limit', '未知'))}节"  # 区域限速
        )
    if candidate_type == "restricted_area_entry":  # 进入禁航区事件
        return f"进入限制区域: {event.zone_name or event.zone_id or '未知区域'}"  # 区域名称
    if candidate_type == "ais_gap":  # AIS信号中断事件
        return f"AIS信号中断: {json_compact(features)}"  # 特征详情
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