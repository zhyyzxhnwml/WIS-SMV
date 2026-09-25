"""
容错分析器 —— 组件不可用 + 脏数据 + 错误恢复
==============================================
"""
from __future__ import annotations  # 启用延迟类型注解求值

from .math_utils import fisher_exact_test  # 导入Fisher精确检验函数


class FaultAnalyzer:
    """容错实验分析器"""

    @staticmethod
    def analyze_component_failure(
        agent_results: list[dict],  # 多次故障实验的 Agent 运行结果列表
        cascade_results: list[dict],  # 多次故障实验的 Cascade 运行结果列表
        fault_description: str,  # 故障描述（如 "LLM 断连"）
    ) -> dict:
        """
        组件故障分析 —— 对比两系统在故障下的行为。

        参数:
            agent_results: 多次故障实验的 Agent 运行结果列表
            cascade_results: 多次故障实验的 Cascade 运行结果列表
            fault_description: 故障描述（如 "LLM 断连"）

        返回:
            dict: 崩溃率、降级率、Fisher 检验结果
        """
        agent_crashed = [r["exit_code"] != 0 for r in agent_results]  # Agent各次是否崩溃
        cascade_crashed = [r["exit_code"] != 0 for r in cascade_results]  # Cascade各次是否崩溃

        agent_crash_rate = sum(agent_crashed) / max(len(agent_crashed), 1)  # Agent崩溃率
        cascade_crash_rate = sum(cascade_crashed) / max(len(cascade_crashed), 1)  # Cascade崩溃率

        # Agent 的降级：看 stderr 是否提到 Mock/降级
        agent_degraded = [  # Agent各次是否降级运行
            "Mock" not in r["stdout"]
            and "降级" not in r["stdout"]
            and r["exit_code"] == 0
            for r in agent_results  # 遍历每个Agent运行结果
        ]

        # Cascade 的降级：stdout 包含 "Mock" "训练初始" 等关键词
        cascade_degraded = [  # Cascade各次是否降级运行
            "Mock" in r["stdout"] or "训练初始" in r["stdout"]
            for r in cascade_results  # 遍历每个Cascade运行结果
        ]

        # Fisher 精确检验（崩溃率）
        fisher = fisher_exact_test(agent_crashed, cascade_crashed)  # 对崩溃率做Fisher精确检验

        return {
            "fault": fault_description,  # 故障描述
            "agent_crash_rate": agent_crash_rate,  # Agent崩溃率
            "cascade_crash_rate": cascade_crash_rate,  # Cascade崩溃率
            "agent_output_count": sum(  # Agent有输出文件的实验次数
                1 for r in agent_results if len(r.get("output_files", [])) > 0
            ),
            "cascade_output_count": sum(  # Cascade有输出文件的实验次数
                1 for r in cascade_results if len(r.get("output_files", [])) > 0
            ),
            "agent_produced_any": any(r["exit_code"] == 0 for r in agent_results),  # Agent是否至少有一次成功
            "cascade_produced_any": any(r["exit_code"] == 0 for r in cascade_results),  # Cascade是否至少有一次成功
            "fisher_p": fisher["p_value"],  # Fisher检验p值
            "fisher_significant": fisher["significant"],  # 是否统计显著
            "odds_ratio": fisher["odds_ratio"],  # 优势比
            "conclusion": FaultAnalyzer._conclude(  # 自动生成的结论
                agent_crash_rate, cascade_crash_rate, fisher
            ),
        }

    @staticmethod
    def analyze_data_quality(
        agent_results: list[dict],  # 各脏数据文件对应的 Agent 运行结果列表
        cascade_results: list[dict],  # 各脏数据文件对应的 Cascade 运行结果列表
        fault_type: str,  # 脏数据类型描述
    ) -> dict:
        """
        脏数据处理分析。

        参数:
            agent_results: 各脏数据文件对应的 Agent 运行结果
            cascade_results: 各脏数据文件对应的 Cascade 运行结果

        返回:
            dict: 处理成功数、异常标记情况
        """
        agent_success = [  # Agent各次是否成功处理脏数据
            r["exit_code"] == 0 for r in agent_results
        ]
        cascade_success = [  # Cascade各次是否成功处理脏数据
            r["exit_code"] == 0 for r in cascade_results
        ]

        # 是否在输出中注明了异常
        agent_mentioned = [  # Agent各次是否在输出中标记了异常
            "异常" in r["stdout"] or "error" in r["stderr"].lower()
            for r in agent_results
        ]
        cascade_mentioned = [  # Cascade各次是否在输出中标记了异常
            "异常" in r["stdout"] or "error" in r["stderr"].lower()
            for r in cascade_results
        ]

        return {
            "fault_type": fault_type,  # 脏数据类型
            "agent_success_count": sum(agent_success),  # Agent成功处理次数
            "cascade_success_count": sum(cascade_success),  # Cascade成功处理次数
            "agent_output_count": sum(  # Agent有输出文件的次数
                1 for r in agent_results if len(r.get("output_files", [])) > 0
            ),
            "cascade_output_count": sum(  # Cascade有输出文件的次数
                1 for r in cascade_results if len(r.get("output_files", [])) > 0
            ),
            "agent_mentioned_anomaly": any(agent_mentioned),  # Agent是否至少一次标记了异常
            "cascade_mentioned_anomaly": any(cascade_mentioned),  # Cascade是否至少一次标记了异常
        }

    @staticmethod
    def analyze_recovery(
        agent_results: list[dict],  # Agent运行结果列表
        cascade_results: list[dict],  # Cascade运行结果列表
    ) -> dict:
        """
        错误恢复率分析。

        定义：遇到错误（exit_code != 0 或 stderr 非空）
        后仍能产出结果的比率。
        """
        # Agent 恢复：有错误但仍有产出文件
        agent_errors = [  # Agent出错的实验
            r for r in agent_results
            if r["exit_code"] != 0 or r.get("stderr", "").strip()
        ]
        agent_recovered = [  # Agent出错后仍恢复产出的实验
            r for r in agent_errors
            if len(r.get("output_files", [])) > 0
        ]

        # Cascade 恢复
        cascade_errors = [  # Cascade出错的实验
            r for r in cascade_results
            if r["exit_code"] != 0 or r.get("stderr", "").strip()
        ]
        cascade_recovered = [  # Cascade出错后仍恢复产出的实验
            r for r in cascade_errors
            if len(r.get("output_files", [])) > 0
        ]

        return {
            "agent_error_count": len(agent_errors),  # Agent出错总次数
            "cascade_error_count": len(cascade_errors),  # Cascade出错总次数
            "agent_recovered_count": len(agent_recovered),  # Agent恢复次数
            "cascade_recovered_count": len(cascade_recovered),  # Cascade恢复次数
            "agent_recovery_rate": (  # Agent恢复率
                len(agent_recovered) / max(len(agent_errors), 1)
            ),
            "cascade_recovery_rate": (  # Cascade恢复率
                len(cascade_recovered) / max(len(cascade_errors), 1)
            ),
        }

    @staticmethod
    def _conclude(
        agent_crash_rate: float,  # Agent崩溃率
        cascade_crash_rate: float,  # Cascade崩溃率
        fisher: dict,  # Fisher精确检验结果字典
    ) -> str:
        """生成结论"""
        if cascade_crash_rate < agent_crash_rate and fisher["significant"]:  # Cascade显著优于Agent
            return (
                f"三级架构崩溃率 ({cascade_crash_rate:.0%}) "
                f"显著低于原始 Agent ({agent_crash_rate:.0%})"
            )
        elif cascade_crash_rate < agent_crash_rate:  # Cascade优于Agent但不显著
            return (
                f"三级架构崩溃率 ({cascade_crash_rate:.0%}) "
                f"低于原始 Agent ({agent_crash_rate:.0%})，但差异不显著"
            )
        else:  # 无显著差异
            return "两者在故障下表现无显著差异"

    @staticmethod
    def format_report(
        component_failures: list[dict],  # 组件故障分析结果列表
        data_quality_results: list[dict],  # 脏数据处理分析结果列表
        recovery_result: dict | None = None,  # 错误恢复分析结果（可选）
    ) -> str:
        """生成容错实验报告"""
        lines = []  # 报告行列表
        sep = "─" * 60  # 分隔线

        lines.append(f"\n{sep}")
        lines.append("  组件不可用测试")
        lines.append(f"{sep}")

        for f in component_failures:  # 遍历每个组件故障场景
            lines.append(f"\n  故障: {f['fault']}")
            lines.append(
                f"    Agent 崩溃率: {f['agent_crash_rate']:.0%}"
                f" (产出: {f['agent_output_count']} 次)"
            )
            lines.append(
                f"    Cascade 崩溃率: {f['cascade_crash_rate']:.0%}"
                f" (产出: {f['cascade_output_count']} 次)"
            )
            lines.append(f"    Fisher p={f['fisher_p']:.4f}" + (" ***" if f['fisher_significant'] else ""))
            lines.append(f"    结论: {f['conclusion']}")

        if data_quality_results:  # 如有脏数据处理结果
            lines.append(f"\n{sep}")
            lines.append("  脏数据测试")
            lines.append(f"{sep}")
            for d in data_quality_results:  # 遍历每个脏数据类型
                lines.append(f"\n  异常类型: {d['fault_type']}")
                lines.append(f"    Agent: {d['agent_success_count']} 次成功, "
                            f"{d['agent_output_count']} 次产出, "
                            f"{'标记了异常' if d['agent_mentioned_anomaly'] else '未标记异常'}")
                lines.append(f"    Cascade: {d['cascade_success_count']} 次成功, "
                            f"{d['cascade_output_count']} 次产出, "
                            f"{'标记了异常' if d['cascade_mentioned_anomaly'] else '未标记异常'}")

        if recovery_result:  # 如有错误恢复分析结果
            lines.append(f"\n{sep}")
            lines.append("  错误恢复率")
            lines.append(f"{sep}")
            lines.append(f"  Agent: {recovery_result['agent_recovered_count']}/"
                        f"{recovery_result['agent_error_count']} = "
                        f"{recovery_result['agent_recovery_rate']:.0%}")
            lines.append(f"  Cascade: {recovery_result['cascade_recovered_count']}/"
                        f"{recovery_result['cascade_error_count']} = "
                        f"{recovery_result['cascade_recovery_rate']:.0%}")

        return "\n".join(lines)  # 合并为多行文本返回