"""
速度分析器 —— LLM 调用次数 + 墙钟时间 + 加速比 + 可扩展性
===========================================================
"""
from __future__ import annotations  # 启用延迟类型注解求值

from .math_utils import (  # 从数学工具模块导入所需函数
    describe, bootstrap_ci, paired_test, cohens_d,
    geometric_mean, geometric_std, scaling_analysis, format_describe,
)


class SpeedAnalyzer:
    """速度实验分析器"""

    @staticmethod
    def analyze_llm_calls(
        agent_stdout: str,  # Agent运行的标准输出文本
        cascade_stdout: str,  # Cascade运行的标准输出文本
        agent_calls_list: list[int] | None = None,  # 逐船Agent LLM调用次数列表（可选）
        cascade_calls_list: list[int] | None = None,  # 逐船Cascade LLM调用次数列表（可选）
        n_ships: int = 0,  # 处理的船舶总数
    ) -> dict:
        """
        LLM 调用次数分析。

        两种方式：
        1. 从 stdout 统计总次数
        2. 如果提供了逐船调用列表，做详细分析
        """
        from .math_utils import paired_llm_calls_analysis  # 导入配对LLM调用分析函数

        total_agent = agent_stdout.count("触发深度分析") * 4  # Agent总LLM调用次数（每次深度分析4次调用）
        total_cascade = cascade_stdout.count("[3/3] LLM 判定")  # Cascade总LLM调用次数

        result = {  # 基础结果字典
            "agent_total_calls": total_agent,  # Agent总LLM调用次数
            "cascade_total_calls": total_cascade,  # Cascade总LLM调用次数
        }

        # 减少百分比：只有当 Agent 有调用时才计算
        if total_agent > 0:  # Agent有有效调用
            result["reduction_pct"] = (  # LLM调用减少百分比
                (total_agent - total_cascade) / total_agent * 100
            )
        else:  # Agent未产生有效数据
            result["reduction_pct"] = None  # 无法计算减少百分比

        # 如果有逐船数据，做详细分析
        if agent_calls_list and cascade_calls_list:  # 两者都提供时才做详细分析
            detail = paired_llm_calls_analysis(agent_calls_list, cascade_calls_list)  # 详细分析结果
            result["detail"] = detail  # 存入结果

        if n_ships > 0:  # 有船舶总数信息
            result["agent_per_ship"] = total_agent / n_ships  # Agent每船平均调用次数
            result["cascade_per_ship"] = total_cascade / n_ships  # Cascade每船平均调用次数

        return result  # 返回LLM调用分析结果

    @staticmethod
    def analyze_wallclock(
        agent_time: float,  # Agent 总时间（秒）
        cascade_time: float,  # Cascade 总时间（秒）
        n_ships: int,  # 处理的船舶数
    ) -> dict:
        """
        墙钟时间对比。

        参数:
            agent_time: Agent 总时间（秒）
            cascade_time: Cascade 总时间（秒）
            n_ships: 处理的船舶数

        返回:
            dict: 包含总时间、每船时间、加速比
        """
        speedup = agent_time / max(cascade_time, 0.001)  # 加速比（避免除零）

        result = {  # 墙钟时间对比结果
            "agent_total_seconds": agent_time,  # Agent总耗时（秒）
            "cascade_total_seconds": cascade_time,  # Cascade总耗时（秒）
            "agent_per_ship_seconds": agent_time / max(n_ships, 1),  # Agent每船耗时（秒）
            "cascade_per_ship_seconds": cascade_time / max(n_ships, 1),  # Cascade每船耗时（秒）
            "speedup_ratio": speedup,  # 加速比
            "speedup_description": f"{speedup:.1f}x",  # 加速比描述字符串
        }
        return result  # 返回墙钟时间分析结果

    @staticmethod
    def analyze_speedup_distribution(
        agent_times_per_ship: list[float],  # Agent 对每艘船的处理时间列表
        cascade_times_per_ship: list[float],  # Cascade 对每艘船的处理时间列表
    ) -> dict:
        """
        加速比分布分析 —— 使用几何均值（比率的正确平均）。

        参数:
            agent_times_per_ship: Agent 对每艘船的处理时间
            cascade_times_per_ship: Cascade 对每艘船的处理时间
        """
        ratios = [  # 每艘船的加速比（Agent时间/Cascade时间）
            a / max(c, 0.001)
            for a, c in zip(agent_times_per_ship, cascade_times_per_ship)  # 配对遍历每艘船
        ]

        gm = geometric_mean(ratios)  # 加速比的几何均值
        gs = geometric_std(ratios)  # 加速比的几何标准差

        desc = describe(ratios, "加速比分布")  # 加速比分布的描述统计

        return {  # 返回加速比分布分析结果
            "geometric_mean": gm,  # 几何均值
            "geometric_std": gs,  # 几何标准差
            "geometric_interval": [gm / gs, gm * gs],  # 几何区间（GM ÷/× GSD）
            "arithmetic_mean": desc["mean"],  # 算术均值
            "median": desc["median"],  # 中位数
            "min": desc["min"],  # 最小值
            "max": desc["max"],  # 最大值
            "count": desc["count"],  # 样本数
        }

    @staticmethod
    def analyze_scaling(
        ship_counts: list[int],  # [1, 10, 50, 100, 200] 各规模船数
        agent_times: list[float],  # Agent 在各规模下的处理时间列表
        cascade_times: list[float],  # Cascade 在各规模下的处理时间列表
    ) -> dict:
        """
        可扩展性分析 —— log-log 回归。

        参数:
            ship_counts: [1, 10, 50, 100, 200]
            agent_times: Agent 在各规模下的处理时间
            cascade_times: Cascade 在各规模下的处理时间
        """
        agent_scale = scaling_analysis(ship_counts, agent_times)  # Agent的可扩展性分析
        cascade_scale = scaling_analysis(ship_counts, cascade_times)  # Cascade的可扩展性分析

        return {  # 返回可扩展性分析结果
            "agent": agent_scale,  # Agent的可扩展性分析结果
            "cascade": cascade_scale,  # Cascade的可扩展性分析结果
        }

    @staticmethod
    def format_report(
        llm_result: dict,  # LLM调用次数分析结果
        wallclock_result: dict,  # 墙钟时间分析结果
        speedup_result: dict | None = None,  # 加速比分布分析结果（可选）
        scaling_result: dict | None = None,  # 可扩展性分析结果（可选）
    ) -> str:
        """生成完整的 Markdown 报告"""
        lines = []  # 报告行列表
        sep = "─" * 60  # 分隔线

        lines.append(f"\n{sep}")
        lines.append("  LLM 调用次数对比")
        lines.append(f"{sep}")

        lines.append(
            f"  Agent: {llm_result['agent_total_calls']} 次, "
            f"每船 {llm_result.get('agent_per_ship', 0):.2f} 次"
        )
        lines.append(
            f"  Cascade: {llm_result['cascade_total_calls']} 次, "
            f"每船 {llm_result.get('cascade_per_ship', 0):.2f} 次"
        )
        if llm_result.get("reduction_pct") is not None:  # 有减少百分比
            lines.append(f"  减少: {llm_result['reduction_pct']:.1f}%")
        else:  # Agent无有效数据
            lines.append(f"  减少: Agent 无有效数据，无法计算")

        if "detail" in llm_result:  # 有详细分析
            d = llm_result["detail"]  # 详细分析结果
            lines.append(f"  ---")
            lines.append(f"  Agent 中位数 [IQR]: {d['agent']['median']} [{d['agent']['q1']:.0f}, {d['agent']['q3']:.0f}]")
            lines.append(f"  Cascade 中位数 [IQR]: {d['cascade']['median']} [{d['cascade']['q1']:.0f}, {d['cascade']['q3']:.0f}]")
            lines.append(f"  Wilcoxon p={d['comparison']['wilcoxon_p']:.4f} {'*** 显著' if d['comparison']['significant'] else 'n.s.'}")

        lines.append(f"\n{sep}")
        lines.append("  墙钟时间对比")
        lines.append(f"{sep}")

        def fmt(t):  # 内部函数：格式化时间显示
            if t > 60:
                return f"{t/60:.1f} 分钟"
            return f"{t:.1f} 秒"

        lines.append(f"  Agent: {fmt(wallclock_result['agent_total_seconds'])}")
        lines.append(f"  Cascade: {fmt(wallclock_result['cascade_total_seconds'])}")
        lines.append(f"  加速比: {wallclock_result['speedup_ratio']:.1f}x")
        lines.append(f"  Agent 每船: {wallclock_result['agent_per_ship_seconds']:.2f}s")
        lines.append(f"  Cascade 每船: {wallclock_result['cascade_per_ship_seconds']:.2f}s")

        if speedup_result:  # 如有加速比分布分析
            lines.append(f"\n{sep}")
            lines.append("  加速比分布")
            lines.append(f"{sep}")
            lines.append(f"  几何均值: {speedup_result['geometric_mean']:.1f}x")
            lines.append(f"  中位数: {speedup_result['median']:.1f}x")
            lines.append(f"  范围: [{speedup_result['min']:.1f}x, {speedup_result['max']:.1f}x]")

        if scaling_result:  # 如有可扩展性分析
            lines.append(f"\n{sep}")
            lines.append("  可扩展性分析 (log-log 回归)")
            lines.append(f"{sep}")
            lines.append(f"  Agent α={scaling_result['agent']['alpha']:.2f} ({scaling_result['agent']['interpretation']})")
            lines.append(f"  Cascade α={scaling_result['cascade']['alpha']:.2f} ({scaling_result['cascade']['interpretation']})")

        return "\n".join(lines)  # 合并为多行文本返回