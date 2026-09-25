#!/usr/bin/env python3
"""
容量/实用性分析器 —— 数据容量压力测试 + 压缩率 + 存活分析
===========================================================
"""
from __future__ import annotations  # 启用延迟类型注解求值

from .math_utils import describe, format_describe  # 导入数学工具函数


class CapacityAnalyzer:
    """实用性实验分析器"""

    @staticmethod
    def analyze_capacity(
        ship_counts: list[int],  # 各测试子集的船数列表 [1, 10, 50, 100, 200]
        agent_results: list[dict],  # 各子集的 Agent 运行结果列表
        cascade_results: list[dict],  # 各子集的 Cascade 运行结果列表
    ) -> dict:
        """
        容量分析 —— 各系统在不同数据规模下的存活情况。

        参数:
            ship_counts: 各测试子集的船数 [1, 10, 50, 100, 200]
            agent_results: 各子集的 Agent 运行结果
            cascade_results: 各子集的 Cascade 运行结果

        返回:
            dict: 最大容量 + 存活详情
        """
        # 判断存活：exit_code == 0 且产出了结果
        agent_alive = [  # Agent各规模是否存活
            r["exit_code"] == 0 and r["ship_count"] > 0
            for r in agent_results  # 遍历每个Agent运行结果
        ]
        cascade_alive = [  # Cascade各规模是否存活
            r["exit_code"] == 0 and r["ship_count"] > 0
            for r in cascade_results  # 遍历每个Cascade运行结果
        ]

        def max_survived(n_list, alive_list):  # 内部函数：求最大存活规模
            survived = [n for n, a in zip(n_list, alive_list) if a]  # 筛选存活的规模
            return max(survived) if survived else 0  # 返回最大存活规模，若全未存活则返回0

        agent_max = max_survived(ship_counts, agent_alive)  # Agent最大存活船数
        cascade_max = max_survived(ship_counts, cascade_alive)  # Cascade最大存活船数

        ratio = cascade_max / max(agent_max, 1)  # Cascade容量倍数（相对Agent）

        return {
            "agent_max_capacity": agent_max,  # Agent最大可处理船数
            "cascade_max_capacity": cascade_max,  # Cascade最大可处理船数
            "capacity_ratio": ratio,  # Cascade容量倍数
            "survival_detail": {  # 各规模存活详情字典
                str(n): {  # 以船数字符串为键
                    "agent": "存活" if a else "死亡",  # Agent存活状态
                    "cascade": "存活" if c else "死亡",  # Cascade存活状态
                    "agent_time_s": r_a["total_time"],  # Agent处理时间（秒）
                    "cascade_time_s": r_c["total_time"],  # Cascade处理时间（秒）
                    "agent_error": r_a.get("error"),  # Agent错误信息
                    "cascade_error": r_c.get("error"),  # Cascade错误信息
                }
                for n, a, c, r_a, r_c in zip(  # 同时遍历五个列表
                    ship_counts, agent_alive, cascade_alive,
                    agent_results, cascade_results,
                )
            },
        }

    @staticmethod
    def analyze_compression(stdout: str) -> dict:
        """
        从 Cascade 的 stdout 中提取压缩信息。

        查找 "压缩率"、"原始行" 等关键词。
        """
        import re  # 正则表达式模块

        compression_info = {"found": False}  # 压缩信息字典，初始标记为未找到

        for line in stdout.split("\n"):  # 逐行遍历stdout输出
            if "压缩率" in line:  # 查找含"压缩率"的行
                m = re.search(r"压缩率[：:]?\s*([\d.]+%)", line)  # 匹配压缩率数值（如"85.3%"）
                if m:
                    compression_info["rate"] = m.group(1)  # 提取压缩率字符串
                    compression_info["found"] = True  # 标记已找到压缩信息

            if "原始行" in line:  # 查找含"原始行"的行
                m = re.search(r"原始行[：:]?\s*(\d+)", line)  # 匹配原始行数
                if m:
                    compression_info["raw_rows"] = int(m.group(1))  # 提取原始行数

            if "有效" in line:  # 查找含"有效"的行
                m = re.search(r"有效[：:]?\s*(\d+)", line)  # 匹配有效行数
                if m:
                    compression_info["valid_rows"] = int(m.group(1))  # 提取有效行数

            if "事件" in line and "标记" in line:  # 查找含"事件"和"标记"的行
                m = re.search(r"事件[：:]?\s*(\d+)", line)  # 匹配事件数
                if m:
                    compression_info["events"] = int(m.group(1))  # 提取事件数

        return compression_info  # 返回压缩信息字典

    @staticmethod
    def analyze_evidence_completeness(
        agent_results_csv: list[dict],  # Agent输出CSV结果列表
        cascade_results_csv: list[dict],  # Cascade输出CSV结果列表
    ) -> dict:
        """
        证据链完整性分析。

        三级架构相比原始 Agent，输出的信息更丰富：
        - Agent 输出: violation_detected, confidence, risk_level, final_assessment
        - Cascade 输出: 除了上述字段还有 routing, violation_type, recommended_action
        """
        agent_fields = set()  # Agent输出字段集合
        for r in agent_results_csv:  # 遍历每个Agent结果字典
            agent_fields.update(r.keys())  # 将当前字典的键加入字段集合

        cascade_fields = set()  # Cascade输出字段集合
        for r in cascade_results_csv:  # 遍历每个Cascade结果字典
            cascade_fields.update(r.keys())  # 将当前字典的键加入字段集合

        agent_only = agent_fields - cascade_fields  # Agent独有字段（Cascade缺失的）
        cascade_only = cascade_fields - agent_fields  # Cascade独有字段（Agent缺失的）

        return {
            "agent_field_count": len(agent_fields),  # Agent输出字段总数
            "cascade_field_count": len(cascade_fields),  # Cascade输出字段总数
            "cascade_extra_fields": sorted(cascade_only),  # Cascade额外字段列表（排序）
            "agent_unique_fields": sorted(agent_only),  # Agent独有字段列表（排序）
            "has_routing": "routing" in cascade_fields,  # 是否包含routing字段
            "has_violation_type": "violation_type" in cascade_fields,  # 是否包含violation_type字段
            "has_recommendation": "recommended_action" in cascade_fields,  # 是否包含recommended_action字段
        }

    @staticmethod
    def format_report(
        capacity_result: dict,  # 容量分析结果字典
        compression_result: dict | None = None,  # 压缩分析结果（可选）
        evidence_result: dict | None = None,  # 证据完整性分析结果（可选）
    ) -> str:
        """生成实用性实验报告"""
        lines = []  # 报告行列表
        sep = "─" * 60  # 分隔线

        lines.append(f"\n{sep}")
        lines.append("  容量测试结果")
        lines.append(f"{sep}")
        lines.append(f"  Agent 最大存活: {capacity_result['agent_max_capacity']} 艘")
        lines.append(f"  Cascade 最大存活: {capacity_result['cascade_max_capacity']} 艘")
        lines.append(f"  容量倍数: {capacity_result['capacity_ratio']:.0f}x")
        lines.append("")
        lines.append("  各规模存活详情:")
        for n_str, detail in capacity_result["survival_detail"].items():  # 遍历各规模详情
            lines.append(
                f"    {n_str:>4} 艘: Agent={detail['agent']} "
                f"({detail['agent_time_s']:.1f}s), "
                f"Cascade={detail['cascade']} "
                f"({detail['cascade_time_s']:.1f}s)"
            )
            if detail.get("agent_error"):  # 如有Agent错误
                lines.append(f"         Agent 错误: {detail['agent_error'][:80]}")
            if detail.get("cascade_error"):  # 如有Cascade错误
                lines.append(f"         Cascade 错误: {detail['cascade_error'][:80]}")

        if compression_result and compression_result.get("found"):  # 如有压缩信息
            lines.append(f"\n{sep}")
            lines.append("  压缩效果")
            lines.append(f"{sep}")
            if "rate" in compression_result:  # 压缩率
                lines.append(f"  压缩率: {compression_result['rate']}")
            if "events" in compression_result:  # 检测事件数
                lines.append(f"  检测事件: {compression_result['events']} 个")
            if "raw_rows" in compression_result:  # 原始行数
                lines.append(f"  原始行: {compression_result['raw_rows']}")

        if evidence_result:  # 如有证据完整性分析
            lines.append(f"\n{sep}")
            lines.append("  证据链完整性")
            lines.append(f"{sep}")
            lines.append(f"  Agent 输出字段数: {evidence_result['agent_field_count']}")
            lines.append(f"  Cascade 输出字段数: {evidence_result['cascade_field_count']}")
            if evidence_result["cascade_extra_fields"]:  # 如有额外字段
                lines.append(f"  Cascade 额外字段: {', '.join(evidence_result['cascade_extra_fields'])}")

        return "\n".join(lines)  # 合并为多行文本返回