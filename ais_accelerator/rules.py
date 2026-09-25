"""
ais_accelerator/rules.py — 规则引擎与区域查询
===============================================

【这个文件做什么？】
  加载 regulations.json 中的规则库，提供：
  1. zones_at(lat, lon) — 查询某坐标点位于哪些管制区域
  2. select(event, ship) — 匹配事件适用的违规规则

【规则库结构 (regulations.json)】
  - zones: [区域列表] — 每个区域有边界坐标、限制类型（限速/禁入/禁渔）
  - violation_types: 违规类型定义（代码/名称/处罚/报警阈值）
  - ship_types: 船舶类型映射

【实现】
  - JsonRuleProvider: 从 JSON 文件加载规则，射线法判断点是否在多边形内
  - RagRuleProvider: 为未来 RAG 向量检索预留的接口（当前未使用）
  - season_is_active(): 判断季节性时间规则（如禁渔期 5/1-9/1）
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import hashlib  # 哈希计算模块
import json  # JSON解析模块
from datetime import datetime  # 日期时间处理
from pathlib import Path  # 路径处理模块
from typing import Callable, Optional, Protocol  # 类型注解

from .geometry import point_in_polygon  # 射线法多边形判断函数
from .models import CandidateEvent, CandidateType, MatchedRule, ShipProfile  # 数据模型


class RuleProvider(Protocol):  # 规则提供器协议接口
    version: str  # 规则版本号

    def zones_at(self, lat: float, lon: float) -> list[dict]:  # 查询坐标点所在区域
        ...

    def select(  # 根据事件和船舶信息选择匹配规则
        self, event: CandidateEvent, ship: ShipProfile, limit: int = 5  # 候选事件, 船舶概要, 最大返回规则数
    ) -> list[MatchedRule]:
        ...


def season_is_active(rule: dict, event_time: datetime) -> bool:  # 判断季节性规则是否处于生效期
    if rule.get("type") != "season":  # 如果不是季节性规则
        return True  # 始终有效
    current = (event_time.month, event_time.day)  # 当前日期（月，日）
    start = (int(rule["start_month"]), int(rule["start_day"]))  # 规则开始日期
    end = (int(rule["end_month"]), int(rule["end_day"]))  # 规则结束日期
    return (  # 判断当前日期是否在有效期内
        start <= current <= end  # 同一年内
        if start <= end  # 如果开始日期<=结束日期（非跨年）
        else current >= start or current <= end  # 跨年情况
    )


class JsonRuleProvider:  # 基于JSON文件的规则提供器
    """Deterministic metadata-first provider for the existing rule JSON."""

    def __init__(self, regulations_path: Path):  # 初始化，加载规则文件
        self.path = Path(regulations_path)  # 规则文件路径
        raw = self.path.read_bytes()  # 读取规则文件的原始字节
        self.version = hashlib.sha256(raw).hexdigest()[:16]  # 规则版本号（文件内容的SHA256前缀）
        self.data = json.loads(raw.decode("utf-8-sig"))  # 解析规则JSON数据
        self.zones = self.data.get("zones", [])  # 区域列表
        self.violation_types = self.data.get("violation_types", {})  # 违规类型字典（已迁移到 RegulationDB，此处为空）
        self._zone_bounds = {  # 各区域的边界框缓存
            str(zone.get("id", zone.get("name"))): self._bounds(zone)  # 区域ID → 边界框
            for zone in self.zones  # 遍历所有区域
        }

    def zones_at(self, lat: float, lon: float) -> list[dict]:  # 查询坐标点位于哪些区域内
        return [  # 返回坐标点所在的区域列表
            zone  # 符合条件的区域
            for zone in self.zones  # 遍历所有区域
            if self._inside_bounds(zone, lat, lon)  # 先通过边界框快速筛选
            and point_in_polygon(lon, lat, zone.get("boundary", []))  # 再用射线法精确判断
        ]

    @staticmethod
    def _bounds(zone: dict) -> tuple[float, float, float, float]:  # 计算区域的经纬度边界框
        boundary = zone.get("boundary", [])  # 区域的边界点列表
        if not boundary:  # 如果没有边界
            return (float("inf"), float("inf"), float("-inf"), float("-inf"))  # 返回无限大边界
        longitudes = [float(point[0]) for point in boundary]  # 所有顶点的经度
        latitudes = [float(point[1]) for point in boundary]  # 所有顶点的纬度
        return min(longitudes), min(latitudes), max(longitudes), max(latitudes)  # 返回(min_lon, min_lat, max_lon, max_lat)

    def _inside_bounds(self, zone: dict, lat: float, lon: float) -> bool:  # 粗筛：点是否在区域边界框内
        zone_id = str(zone.get("id", zone.get("name")))  # 区域ID
        min_lon, min_lat, max_lon, max_lat = self._zone_bounds[zone_id]  # 获取区域的边界框
        return min_lon <= lon <= max_lon and min_lat <= lat <= max_lat  # 判断经纬度是否在边界框内

    def zone_by_id(self, zone_id: Optional[str]) -> Optional[dict]:  # 根据区域ID查找区域信息
        if not zone_id:  # 如果区域ID为空
            return None  # 返回None
        return next(  # 返回匹配的第一个区域
            (
                zone  # 区域
                for zone in self.zones  # 遍历所有区域
                if zone.get("id") == zone_id or zone.get("name") == zone_id  # ID或名称匹配
            ),
            None,  # 未找到则返回None
        )

    def select(  # 根据事件和船舶信息选择匹配的规则
        self, event: CandidateEvent, ship: ShipProfile, limit: int = 5  # 候选事件, 船舶概要, 最大返回规则数
    ) -> list[MatchedRule]:
        selected: list[MatchedRule] = []  # 选中的规则列表
        zone = self.zone_by_id(event.zone_id or event.zone_name)  # 通过事件关联的区域ID查找区域
        if zone:  # 如果找到了区域
            selected.extend(self._zone_rules(event, zone))  # 添加该区域相关的规则
        if event.candidate_type in {  # 如果事件类型是AIS信号缺失或位置跳变
            CandidateType.AIS_GAP,
            CandidateType.POSITION_JUMP,
        }:
            # violation_types 已迁移到 RegulationDB，此处使用默认标签
            selected.append(  # 添加AIS异常关闭规则
                MatchedRule(
                    rule_id="ais_off",  # 规则ID
                    violation_code="ais_off",  # 违规代码
                    label="AIS异常关闭",  # 规则标签
                    description="故意关闭AIS信号试图逃避监控",  # 规则描述
                    source=self.path.name,  # 规则来源文件名
                    metadata={"rule_version": self.version},  # 规则版本元数据
                )
            )
        unique: dict[str, MatchedRule] = {}  # 用于去重的字典（规则ID→规则）
        for rule in selected:  # 遍历已选规则
            unique.setdefault(rule.rule_id, rule)  # 按rule_id去重
        return list(unique.values())[:limit]  # 返回去重后的规则列表，不超过限制数量

    def _zone_rules(self, event: CandidateEvent, zone: dict) -> list[MatchedRule]:  # 根据事件类型匹配区域特定的规则
        restrictions = zone.get("restrictions", {})  # 区域的限制条件字典
        rules: list[MatchedRule] = []  # 匹配到的规则列表
        if (  # 如果事件是进入禁航区且该区域禁止进入
            event.candidate_type == CandidateType.RESTRICTED_AREA_ENTRY
            and restrictions.get("entry_allowed") is False
        ):
            rules.append(self._make_rule(zone, "restricted_area_entry"))  # 添加禁航区进入规则
        if (  # 如果事件是超速且该区域有限速
            event.candidate_type == CandidateType.SPEED_VIOLATION
            and restrictions.get("max_speed_knots") is not None
        ):
            rules.append(  # 添加超速规则
                self._make_rule(
                    zone,  # 区域
                    "speed_violation",  # 违规类型
                    speed_limit=float(restrictions["max_speed_knots"]),  # 限速值
                )
            )
        if (  # 如果事件是低速捕捞且该区域禁止捕鱼
            event.candidate_type == CandidateType.LOW_SPEED_FISHING
            and restrictions.get("fishing_allowed") is False
        ):
            time_rules = zone.get("time_rules", [])  # 区域的时间规则列表
            if not time_rules or any(  # 如果没有时间限制或当前时间在有效期内
                season_is_active(rule, event.start_time) for rule in time_rules  # 检查每条时间规则是否有效
            ):
                rules.append(self._make_rule(zone, "illegal_fishing"))  # 添加非法捕捞规则
        return rules  # 返回匹配规则列表

    def _make_rule(  # 创建匹配规则对象
        self, zone: dict, violation_code: str, speed_limit: Optional[float] = None  # 区域, 违规代码, 速度限制（可选）
    ) -> MatchedRule:
        # violation_types 已迁移到 RegulationDB，使用默认标签
        # 具体罚则由 RagRuleProvider 从 RegulationDB 查询补充
        default_labels = {
            "restricted_area_entry": "禁航区入侵",
            "speed_violation": "超速行驶",
            "illegal_fishing": "非法捕捞",
            "ais_off": "AIS异常关闭",
            "collision_risk": "碰撞风险",
        }
        default_descriptions = {
            "restricted_area_entry": "进入军事禁区或其他禁止进入区域",
            "speed_violation": "在限速区域超过规定航速",
            "illegal_fishing": "在禁渔区/禁渔期进行捕捞作业",
            "ais_off": "故意关闭AIS信号试图逃避监控",
            "collision_risk": "船舶间存在碰撞危险，未按规定采取避碰措施",
        }
        return MatchedRule(  # 返回匹配规则对象
            rule_id=f"{zone.get('id', zone.get('name'))}:{violation_code}",  # 规则ID：区域ID:违规代码
            violation_code=violation_code,  # 违规代码
            label=default_labels.get(violation_code, violation_code),  # 规则中文标签
            description=default_descriptions.get(violation_code, ""),  # 规则描述
            zone_id=zone.get("id"),  # 区域ID
            zone_name=zone.get("name"),  # 区域名称
            speed_limit=speed_limit,  # 速度限制
            source=self.path.name,  # 规则来源文件名
            metadata={  # 规则元数据
                "rule_version": self.version,  # 规则版本号
                "time_rules": zone.get("time_rules", []),  # 时间规则列表
            },
        )


class RagRuleProvider:  # 基于RAG（向量检索）的规则提供器
    """基于 Milvus 法规向量数据库的规则提供器。

    在 JsonRuleProvider 的确定性规则匹配基础上，叠加法规向量检索，
    使 cascade 能精确告诉用户违规了哪一条法规。

    用法:
        from .regulation_db import RegulationDB
        reg_db = RegulationDB()
        reg_db.initialize()
        provider = RagRuleProvider(reg_db, JsonRuleProvider(path))
    """

    def __init__(  # 初始化RAG规则提供器
        self,
        regulation_db: Any,  # RegulationDB 实例
        zone_provider: JsonRuleProvider,  # JSON规则提供器（用于区域查询）
        version: Optional[str] = None,  # 规则版本号
    ):
        self.regulation_db = regulation_db  # 法规向量数据库
        self.zone_provider = zone_provider  # JSON区域提供器
        self.version = version or f"rag-{zone_provider.version}"

    def zones_at(self, lat: float, lon: float) -> list[dict]:  # 查询坐标点所在区域（委托给JSON提供器）
        return self.zone_provider.zones_at(lat, lon)  # 返回区域列表

    def select(  # 通过向量检索 + 确定性规则选择匹配规则
        self, event: CandidateEvent, ship: ShipProfile, limit: int = 5  # 候选事件, 船舶概要, 最大返回数
    ) -> list[MatchedRule]:
        # 1. 先用确定性规则匹配（JsonRuleProvider）
        base_rules = self.zone_provider.select(event, ship, limit)

        # 2. 叠加法规向量检索，为每条规则补充具体法规条款
        violation_code = self._event_to_violation_code(event)
        if violation_code:
            try:
                articles = self.regulation_db.search_by_violation(
                    violation_type=violation_code,
                    zone_name=event.zone_name,
                    top_k=3,
                )
                for article in articles:
                    # 截断过长内容以匹配 MatchedRule 字段限制
                    content = article.get("content", "")
                    if len(content) > 500:
                        content = content[:500] + "..."
                    penalty = article.get("penalty", "")
                    if len(penalty) > 500:
                        penalty = penalty[:500] + "..."
                    rule = MatchedRule(
                        rule_id=article.get("article_id", ""),
                        violation_code=violation_code,
                        label=article.get("title", "")[:200],
                        description=content,
                        zone_id=event.zone_id,
                        zone_name=event.zone_name,
                        source=article.get("source", "法规向量库"),
                        metadata={
                            "article_number": article.get("article_number", ""),
                            "penalty": penalty,
                            "chapter": article.get("chapter", ""),
                            "similarity": article.get("similarity", 0),
                            "source_type": "regulation_vector_db",
                        },
                    )
                    base_rules.append(rule)
            except Exception as e:
                pass  # 法规检索失败不影响主流程

        # 去重
        unique: dict[str, MatchedRule] = {}
        for rule in base_rules:
            unique.setdefault(rule.rule_id, rule)
        return list(unique.values())[:limit]

    @staticmethod
    def _event_to_violation_code(event: CandidateEvent) -> Optional[str]:
        """将候选事件类型映射为违规代码。"""
        mapping = {
            CandidateType.RESTRICTED_AREA_ENTRY: "restricted_area_entry",
            CandidateType.SPEED_VIOLATION: "speed_violation",
            CandidateType.LOW_SPEED_FISHING: "illegal_fishing",
            CandidateType.AIS_GAP: "ais_off",
            CandidateType.POSITION_JUMP: "ais_off",
            CandidateType.COURSE_ANOMALY: None,  # 航向异常暂无直接对应
        }
        return mapping.get(event.candidate_type)