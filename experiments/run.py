#!/usr/bin/env python3
"""
experiments/run.py — 一键运行全部对比实验
==========================================

用法:
    python experiments/run.py                       # 运行所有实验
    python experiments/run.py --speed               # 只跑速度实验
    python experiments/run.py --practicality        # 只跑实用性实验
    python experiments/run.py --fault-tolerance     # 只跑容错实验
    python experiments/run.py --csv data/input/sample.csv
    python experiments/run.py --no-llm              # 跳过 LLM
"""
from __future__ import annotations  # 启用延迟类型注解求值（PEP 563）

import sys  # 系统参数与标准流
import os  # 操作系统接口
import io  # IO流处理（含编码转换）
import argparse  # 命令行参数解析
import time  # 时间相关函数
from pathlib import Path  # 跨平台路径操作
from datetime import datetime, timedelta  # 日期时间处理

# Windows 终端 UTF-8
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.collectors.agent_collector import OriginalAgentCollector
from experiments.collectors.cascade_collector import CascadeCollector
from experiments.analyzers.speed_analyzer import SpeedAnalyzer
from experiments.analyzers.capacity_analyzer import CapacityAnalyzer
from experiments.analyzers.fault_analyzer import FaultAnalyzer
from experiments.data_prep import (
    prepare_test_subsets,
    prepare_dirty_datasets,
    generate_synthetic_test_set,
)
from experiments.fault_injection import (
    without_ollama,
    without_ml_model,
    without_milvus,
    with_all_faults,
    with_temporary_csv,
)
from experiments.reporters.markdown_reporter import MarkdownReporter
from experiments.reporters.chart_generator import ChartGenerator


# ── 全局配置 ──

RESULTS_DIR = "experiments/results"  # 实验结果输出目录
DATA_DIR = "experiments/data"  # 实验数据目录
SUBSET_DIR = f"{DATA_DIR}/subsets"  # 数据子集目录
DIRTY_DIR = f"{DATA_DIR}/dirty"  # 脏数据目录
REPORT_DIR = "experiments/results"  # 报告输出目录

DEFAULT_CSV = None  # 默认CSV数据源路径
for candidate in [  # 遍历候选CSV文件路径列表
    "data/input/___210724铜锣峡210731_converted_converted_converted.csv",
    "data/input/__210724铜锣峡210731_converted_converted.csv",
]:
    if Path(candidate).exists():  # 如果文件存在
        DEFAULT_CSV = candidate  # 设为默认CSV路径
        break  # 找到即停止


def fmt_time(seconds: float) -> str:
    """格式化时间显示"""
    if seconds > 3600:  # 超过1小时，以小时为单位
        return f"{seconds/3600:.1f}h"
    elif seconds > 60:  # 超过1分钟，以分钟为单位
        return f"{seconds/60:.1f}min"
    else:  # 不足1分钟，以秒为单位
        return f"{seconds:.1f}s"


class ExperimentRunner:
    """主编排器：按顺序运行所有实验"""

    def __init__(self, args):  # args: 命令行参数对象
        self.args = args  # 原始命令行参数
        self.source_csv = args.csv or DEFAULT_CSV  # 要处理的数据源CSV路径
        self.output_dir = Path(args.output_dir) / f"run_{int(time.time())}"  # 本次运行的输出目录
        self.output_dir.mkdir(parents=True, exist_ok=True)  # 递归创建输出目录

        self.agent = OriginalAgentCollector()  # 原始Agent采集器实例
        self.cascade = CascadeCollector()  # 三级架构采集器实例
        self.speed_analyzer = SpeedAnalyzer()  # 速度性能分析器实例
        self.capacity_analyzer = CapacityAnalyzer()  # 容量性能分析器实例
        self.fault_analyzer = FaultAnalyzer()  # 容错分析器实例
        self.reporter = MarkdownReporter()  # Markdown报告生成器实例
        self.chart_gen = ChartGenerator()  # 图表生成器实例

        self.results = {  # 汇总实验结果字典
            "config": self._get_config(),  # 实验配置信息
            "speed": {},  # 速度实验数据（执行时填充）
            "practicality": {},  # 实用性实验数据（执行时填充）
            "fault_tolerance": [],  # 容错实验数据（执行时填充）
        }
        self._start_time = time.time()  # 实验开始时间戳（用于计算耗时）

        if not self.source_csv or not Path(self.source_csv).exists():  # 检查数据源CSV是否存在
            print(f"\n{'!'*60}")
            print(f"  错误：找不到数据源 CSV 文件")
            print(f"{'!'*60}")
            print(f"  请通过 --csv 指定路径，或确认以下路径存在:")
            print(f"    - data/input/___210724铜锣峡210731_converted_converted_converted.csv")
            print(f"    - data/input/__210724铜锣峡210731_converted_converted.csv")
            sys.exit(1)  # 退出程序

    def _get_config(self) -> dict:  # 获取实验配置信息
        return {  # 返回配置字典
            "data_source": self.source_csv,  # 数据来源文件路径
            "synthetic_count": self.args.synthetic,  # 合成的测试用例数
            "no_llm": self.args.no_llm,  # 是否跳过LLM
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),  # 运行时间戳
        }

    def _header(self, num: int, title: str, desc: str = ""):  # num: 实验编号; title: 标题; desc: 描述
        """打印实验阶段标题"""
        elapsed = timedelta(seconds=int(time.time() - self._start_time))  # 从开始到现在的已用时间
        print(f"\n{'='*60}")
        print(f"  实验{num}：{title}")
        print(f"  说明：{desc}" if desc else "")
        print(f"  已用时间：{elapsed}")
        print(f"{'='*60}")

    def _sub_header(self, title: str):  # title: 子阶段标题
        print(f"\n{'─'*50}")
        print(f"  ▶ {title}")
        print(f"{'─'*50}")

    # ═══════════════════════════════════════════════════
    # 实验一：速度性能实验
    # ═══════════════════════════════════════════════════

    def run_speed_experiment(self):  # 执行速度性能实验
        self._header(1, "速度性能实验",
                     "对比原始 Agent 和三级架构的处理速度、LLM 调用次数、扩展性")

        # 准备测试数据
        self._sub_header("准备测试数据 (100艘)")
        subsets = prepare_test_subsets(  # 从源数据中抽取100艘船的子集
            self.source_csv, SUBSET_DIR, ship_counts=[100]
        )
        test_csv = subsets[0]["output"]  # 生成的测试CSV文件路径
        n_ships = subsets[0]["actual_ships"]  # 实际船只数量
        print(f"  船舶数量: {n_ships}")
        print(f"  数据文件: {test_csv}")

        # ── A. LLM 调用次数对比 ──
        self._sub_header("A. LLM 调用次数对比 — 原始 Agent 处理中...")
        agent_result = self.agent.run_file(  # 运行原始Agent，获取结果
            test_csv,
            str(self.output_dir / "speed_agent_100"),  # Agent输出目录
            max_ships=n_ships,  # 最大处理船只数
        )

        self._sub_header("A. LLM 调用次数对比 — 三级架构处理中...")
        cascade_result = self.cascade.run_file(  # 运行三级架构，获取结果
            test_csv,
            str(self.output_dir / "speed_cascade_100"),  # Cascade输出目录
            max_ships=n_ships,  # 最大处理船只数
            no_llm=self.args.no_llm,  # 是否跳过LLM
        )

        # 分析
        llm_result = self.speed_analyzer.analyze_llm_calls(  # 分析LLM调用次数对比
            agent_stdout=agent_result["stdout"],  # Agent运行标准输出
            cascade_stdout=cascade_result["stdout"],  # Cascade运行标准输出
            n_ships=n_ships,  # 处理的船只数量
        )
        self.results["speed"]["llm_calls"] = llm_result  # 保存LLM调用分析结果

        wallclock = self.speed_analyzer.analyze_wallclock(  # 分析实际耗时对比
            agent_result["total_time"],  # Agent总耗时
            cascade_result["total_time"],  # Cascade总耗时
            n_ships,  # 船只数量
        )
        self.results["speed"]["wallclock"] = wallclock  # 保存耗时分析结果

        # 打印结果
        print(f"\n  ╔══ LLM 调用次数对比结果 ══╗")
        print(f"  ║ 原始 Agent:    {llm_result['agent_total_calls']:>4} 次"
              f" ({fmt_time(agent_result['total_time'])})")
        print(f"  ║ 三级架构:     {llm_result['cascade_total_calls']:>4} 次"
              f" ({fmt_time(cascade_result['total_time'])})")
        if llm_result.get('reduction_pct') is not None:  # 如果有减少百分比数据
            print(f"  ║ 减少:          {llm_result['reduction_pct']:.1f}%")
        print(f"  ║ 加速比:        {wallclock['speedup_ratio']:.1f}x")
        print(f"  ╚{'═'*40}╝")

        # ── B. 可扩展性测试 ──
        self._sub_header("B. 可扩展性测试（各规模处理时间对比）")
        self._run_scaling_test()  # 运行多规模扩展性测试

        return self.results["speed"]  # 返回速度实验结果

    def _run_scaling_test(self):  # 执行可扩展性测试（多规模对比）
        ship_counts = [1, 10, 50, 100]  # 待测试的船舶规模列表
        existing_ships = self._count_ships(self.source_csv)  # 源数据中实际船只总数
        actual_counts = [n for n in ship_counts if n <= existing_ships]  # 过滤出不超出现有数据的规模

        if not actual_counts:  # 如果没有任何规模可用
            print("  [跳过] 数据不足")
            return

        subsets = prepare_test_subsets(  # 为每个规模准备测试数据子集
            self.source_csv, SUBSET_DIR, ship_counts=actual_counts
        )

        agent_times = []  # Agent各规模耗时列表
        cascade_times = []  # Cascade各规模耗时列表
        results_table = []  # 汇总表数据（规模，Agent耗时，Cascade耗时，加速比）

        for i, subset in enumerate(subsets):  # 遍历每个规模的测试子集
            n = subset["actual_ships"]  # 当前子集的船只数量
            print(f"\n  ── 规模: {n} 艘船 ──")

            print(f"  [原始 Agent] 启动...")
            a_r = self.agent.run_file(  # 运行原始Agent
                subset["output"],  # 测试数据文件路径
                str(self.output_dir / f"scaling_agent_{n}"),  # Agent输出目录
                max_ships=n,  # 最大处理船只数
            )
            agent_times.append(a_r["total_time"])  # 记录Agent总耗时

            print(f"  [三级架构] 启动...")
            c_r = self.cascade.run_file(  # 运行三级架构
                subset["output"],  # 测试数据文件路径
                str(self.output_dir / f"scaling_cascade_{n}"),  # Cascade输出目录
                max_ships=n,  # 最大处理船只数
                no_llm=self.args.no_llm,  # 是否跳过LLM
            )
            cascade_times.append(c_r["total_time"])  # 记录Cascade总耗时

            speedup = a_r["total_time"] / max(c_r["total_time"], 0.001)  # 计算加速比（防止除零）
            results_table.append((n, a_r["total_time"], c_r["total_time"], speedup))  # 添加到汇总表

        # 打印汇总表
        print(f"\n  ╔══ 可扩展性测试结果 ══╗")
        print(f"  ║ {'规模':>6} {'Agent':>10} {'Cascade':>10} {'加速比':>8} ║")
        print(f"  ║{'─'*40}║")
        for n, a_t, c_t, sp in results_table:  # 遍历汇总表数据
            print(f"  ║ {n:>4}艘 {a_t:>8.0f}s {c_t:>8.0f}s {sp:>6.1f}x ║")
        print(f"  ╚{'═'*40}╝")

        scaling_result = self.speed_analyzer.analyze_scaling(  # 对扩展性数据进行深入分析
            actual_counts, agent_times, cascade_times
        )
        self.results["speed"]["scaling"] = scaling_result  # 保存扩展性分析结果

        print(f"\n  扩展性分析:")
        print(f"    Agent:   α={scaling_result['agent']['alpha']:.2f} "
              f"({scaling_result['agent']['interpretation']})")
        print(f"    Cascade: α={scaling_result['cascade']['alpha']:.2f} "
              f"({scaling_result['cascade']['interpretation']})")

    # ═══════════════════════════════════════════════════
    # 实验二：实用性实验
    # ═══════════════════════════════════════════════════

    def run_practicality_experiment(self):  # 执行实用性实验
        self._header(2, "实用性实验",
                     "容量压力测试：不同数据规模下两个系统的存活情况")

        # ── A. 容量压力测试 ──
        self._sub_header("A. 容量压力测试")
        ship_counts = [1, 10, 50, 100, 200]  # 待测试的船舶规模列表
        existing = self._count_ships(self.source_csv)  # 源数据中实际船只总数
        actual_counts = [n for n in ship_counts if n <= existing]  # 过滤出不超出现有数据的规模

        subsets = prepare_test_subsets(  # 为每个规模准备测试数据子集
            self.source_csv, SUBSET_DIR, ship_counts=actual_counts
        )

        agent_results_list = []  # 各规模Agent运行结果列表
        cascade_results_list = []  # 各规模Cascade运行结果列表

        for i, subset in enumerate(subsets):  # 遍历每个规模的测试子集
            n = subset["actual_ships"]  # 当前子集的船只数量
            print(f"\n  ╔══ 容量测试: {n} 艘船 ══╗")

            print(f"  [原始 Agent] 启动...")
            a_r = self.agent.run_file(  # 运行原始Agent
                subset["output"],  # 测试数据文件路径
                str(self.output_dir / f"capacity_agent_{n}"),  # Agent输出目录
                max_ships=n,  # 最大处理船只数
            )
            agent_results_list.append(a_r)  # 收集Agent运行结果

            print(f"  [三级架构] 启动...")
            c_r = self.cascade.run_file(  # 运行三级架构
                subset["output"],  # 测试数据文件路径
                str(self.output_dir / f"capacity_cascade_{n}"),  # Cascade输出目录
                max_ships=n,  # 最大处理船只数
                no_llm=self.args.no_llm,  # 是否跳过LLM
            )
            cascade_results_list.append(c_r)  # 收集Cascade运行结果

            a_status = "存活" if a_r["exit_code"] == 0 else "崩溃"  # Agent运行状态
            c_status = "存活" if c_r["exit_code"] == 0 else "崩溃"  # Cascade运行状态
            print(f"  → Agent: {a_status} ({fmt_time(a_r['total_time'])})  "
                  f"Cascade: {c_status} ({fmt_time(c_r['total_time'])})")

        capacity_result = self.capacity_analyzer.analyze_capacity(  # 分析容量测试结果
            actual_counts, agent_results_list, cascade_results_list
        )
        self.results["practicality"]["capacity"] = capacity_result  # 保存容量测试结果

        print(f"\n  ╔══ 容量测试结果 ══╗")
        print(f"  ║ Agent 最大存活:  {capacity_result['agent_max_capacity']:>3} 艘")
        print(f"  ║ Cascade 最大存活: {capacity_result['cascade_max_capacity']:>3} 艘")
        print(f"  ║ 容量倍数:        {capacity_result['capacity_ratio']:>3.0f}x")
        print(f"  ╚{'═'*30}╝")

        # 存活详情表
        print(f"\n  详细:")
        for n_str, detail in capacity_result["survival_detail"].items():  # 遍历各规模存活详情
            print(f"    {n_str:>4}艘  Agent={detail['agent']}({detail['agent_time_s']:.0f}s)  "
                  f"Cascade={detail['cascade']}({detail['cascade_time_s']:.0f}s)")

        # ── B. 证据链完整性 ──
        self._sub_header("B. 证据链完整性分析")
        if agent_results_list and cascade_results_list:  # 确保有实验结果数据
            first_agent_dir = str(self.output_dir / f"capacity_agent_{actual_counts[0]}")  # 第一个Agent结果目录
            first_cascade_dir = str(self.output_dir / f"capacity_cascade_{actual_counts[0]}")  # 第一个Cascade结果目录

            agent_csv = self.agent.parse_results_csv(first_agent_dir)  # 解析Agent输出CSV
            cascade_csv = self.cascade.parse_results_csv(first_cascade_dir)  # 解析Cascade输出CSV

            evidence_result = self.capacity_analyzer.analyze_evidence_completeness(  # 分析证据链完整性
                agent_csv, cascade_csv
            )
            self.results["practicality"]["evidence"] = evidence_result  # 保存证据链分析结果
            print(f"  Agent 输出字段: {evidence_result['agent_field_count']} 个")
            print(f"  Cascade 输出字段: {evidence_result['cascade_field_count']} 个")
            if evidence_result["cascade_extra_fields"]:  # 如果有额外的字段
                print(f"  Cascade 额外字段: {', '.join(evidence_result['cascade_extra_fields'])}")

        # ── C. 压缩效果 ──
        self._sub_header("C. 数据压缩效果")
        if cascade_results_list:  # 如果有Cascade结果
            for cr in cascade_results_list:  # 遍历Cascade运行结果
                if cr["exit_code"] == 0 and cr["stdout"]:  # 只分析成功运行且有输出的结果
                    compression = self.capacity_analyzer.analyze_compression(cr["stdout"])  # 分析压缩率
                    self.results["practicality"]["compression"] = compression  # 保存压缩效果分析结果
                    if compression.get("found"):  # 检测到压缩信息
                        print(f"  压缩率: {compression.get('rate', 'N/A')}")
                        print(f"  事件数: {compression.get('events', 'N/A')}")
                    else:  # 未检测到压缩信息
                        print(f"  未检测到压缩信息")
                    break  # 只分析第一个成功的Cascade结果

        return self.results["practicality"]  # 返回实用性实验结果

    # ═══════════════════════════════════════════════════
    # 实验三：容错率实验
    # ═══════════════════════════════════════════════════

    def run_fault_tolerance_experiment(self):  # 执行容错率实验
        self._header(3, "容错率实验",
                     "在 LLM 不可用、ML 模型缺失等故障下两系统的表现")

        # 准备 10 艘船测试集
        subset_10 = prepare_test_subset(  # 准备10艘船的测试子集
            self.source_csv, SUBSET_DIR, n_ships=10
        )
        test_csv = subset_10["output"]  # 测试集CSV文件路径
        print(f"  测试集: 10 艘船 → {test_csv}")

        fault_results = []  # 各故障场景分析结果列表

        # ── 测试 1：LLM 不可用 ──
        self._sub_header("测试 1/5: LLM 不可用 (Ollama 断连)")
        fault_runs = []  # 当前故障场景的（Agent结果，Cascade结果）对列表
        for i in range(3):  # 重复运行3次取统计
            print(f"\n    第 {i+1} 次运行:")
            with without_ollama():  # 在Ollama不可用的上下文中运行
                a = self.agent.run_file(  # 运行原始Agent
                    test_csv,
                    str(self.output_dir / f"fault_nollm_agent_{i}"),
                    max_ships=10,
                )
                c = self.cascade.run_file(  # 运行三级架构
                    test_csv,
                    str(self.output_dir / f"fault_nollm_cascade_{i}"),
                    max_ships=10,
                )
                fault_runs.append((a, c))  # 记录本轮结果对
            a_status = "存活" if a["exit_code"] == 0 else "崩溃"  # Agent存活状态
            c_status = "存活" if c["exit_code"] == 0 else "崩溃"  # Cascade存活状态
            print(f"    → Agent={a_status}  Cascade={c_status}")

        fault_results.append(self._analyze_fault_pair(  # 分析并记录LLM故障测试结果
            "LLM 不可用",
            [r[0] for r in fault_runs],  # 提取所有Agent结果
            [r[1] for r in fault_runs],  # 提取所有Cascade结果
        ))

        # ── 测试 2：ML 模型缺失 ──
        self._sub_header("测试 2/5: ML 模型缺失")
        fault_runs = []  # 重置故障运行结果列表
        for i in range(3):  # 重复运行3次
            print(f"\n    第 {i+1} 次运行:")
            with without_ml_model():  # 在ML模型缺失的上下文中运行
                a = self.agent.run_file(  # 运行原始Agent
                    test_csv,
                    str(self.output_dir / f"fault_nomodel_agent_{i}"),
                    max_ships=10,
                )
                c = self.cascade.run_file(  # 运行三级架构
                    test_csv,
                    str(self.output_dir / f"fault_nomodel_cascade_{i}"),
                    max_ships=10,
                )
                fault_runs.append((a, c))  # 记录本轮结果对
            a_status = "存活" if a["exit_code"] == 0 else "崩溃"
            c_status = "存活" if c["exit_code"] == 0 else "崩溃"
            print(f"    → Agent={a_status}  Cascade={c_status}")

        fault_results.append(self._analyze_fault_pair(  # 分析并记录ML缺失测试结果
            "ML 模型缺失",
            [r[0] for r in fault_runs],
            [r[1] for r in fault_runs],
        ))

        # ── 测试 3：Milvus 不可用 ──
        self._sub_header("测试 3/5: Milvus 不可用")
        fault_runs = []  # 重置故障运行结果列表
        for i in range(3):  # 重复运行3次
            print(f"\n    第 {i+1} 次运行:")
            with without_milvus():  # 在Milvus不可用的上下文中运行
                a = self.agent.run_file(  # 运行原始Agent
                    test_csv,
                    str(self.output_dir / f"fault_nomilvus_agent_{i}"),
                    max_ships=10,
                )
                c = self.cascade.run_file(  # 运行三级架构
                    test_csv,
                    str(self.output_dir / f"fault_nomilvus_cascade_{i}"),
                    max_ships=10,
                )
                fault_runs.append((a, c))  # 记录本轮结果对
            a_status = "存活" if a["exit_code"] == 0 else "崩溃"
            c_status = "存活" if c["exit_code"] == 0 else "崩溃"
            print(f"    → Agent={a_status}  Cascade={c_status}")

        fault_results.append(self._analyze_fault_pair(  # 分析并记录Milvus故障测试结果
            "Milvus 不可用",
            [r[0] for r in fault_runs],
            [r[1] for r in fault_runs],
        ))

        # ── 测试 4：脏数据 ──
        self._sub_header("测试 4/5: 脏数据处理")
        dirty_paths = prepare_dirty_datasets(self.source_csv, DIRTY_DIR)  # 准备各种脏数据文件
        fault_runs = []  # 重置故障运行结果列表（含脏数据名称）
        for dp in dirty_paths:  # 遍历每种脏数据类型
            name = dp["name"]  # 脏数据名称（如missing_field, wrong_type等）
            print(f"\n    {name}:")
            a = self.agent.run_file(  # 运行原始Agent处理脏数据
                dp["output"],  # 脏数据文件路径
                str(self.output_dir / f"dirty_{Path(dp['output']).stem}_agent"),  # Agent输出目录
            )
            c = self.cascade.run_file(  # 运行三级架构处理脏数据
                dp["output"],  # 脏数据文件路径
                str(self.output_dir / f"dirty_{Path(dp['output']).stem}_cascade"),  # Cascade输出目录
            )
            fault_runs.append((a, c, dp["name"]))  # 记录脏数据处理结果及数据类型名
            a_status = "存活" if a["exit_code"] == 0 else "崩溃"
            c_status = "存活" if c["exit_code"] == 0 else "崩溃"
            print(f"    → Agent={a_status}  Cascade={c_status}")

        for df_name in set(r[2] for r in fault_runs):  # 遍历去重后的脏数据类型
            pair_results = [  # 筛选出同类型脏数据的所有结果对
                (a, c) for a, c, name in fault_runs if name == df_name
            ]
            if pair_results:  # 如果有该类型的数据
                dq = self.fault_analyzer.analyze_data_quality(  # 分析该脏数据类型的处理质量
                    [r[0] for r in pair_results],  # Agent结果列表
                    [r[1] for r in pair_results],  # Cascade结果列表
                    df_name,  # 脏数据类型名称
                )
                self.results["fault_tolerance"].append(dq)  # 保存脏数据处理分析结果

        # ── 测试 5：全部故障组合 ──
        self._sub_header("测试 5/5: 全故障组合 (LLM + ML + Milvus)")
        fault_runs = []  # 重置故障运行结果列表
        for i in range(3):  # 重复运行3次
            print(f"\n    第 {i+1} 次运行:")
            with with_all_faults():  # 在所有故障同时存在的上下文中运行
                a = self.agent.run_file(  # 运行原始Agent
                    test_csv,
                    str(self.output_dir / f"fault_all_agent_{i}"),
                    max_ships=10,
                )
                c = self.cascade.run_file(  # 运行三级架构
                    test_csv,
                    str(self.output_dir / f"fault_all_cascade_{i}"),
                    max_ships=10,
                )
                fault_runs.append((a, c))  # 记录本轮结果对
            a_status = "存活" if a["exit_code"] == 0 else "崩溃"
            c_status = "存活" if c["exit_code"] == 0 else "崩溃"
            print(f"    → Agent={a_status}  Cascade={c_status}")

        fault_results.append(self._analyze_fault_pair(  # 分析并记录全故障测试结果
            "全部故障组合",
            [r[0] for r in fault_runs],
            [r[1] for r in fault_runs],
        ))

        # 保存
        self.results["fault_tolerance_components"] = fault_results  # 保存所有容错测试分析结果

        # 打印结果
        print(f"\n  ╔══ 容错测试结果 ══╗")
        for f in fault_results:  # 遍历各故障场景结果
            sig = " ***" if f['fisher_significant'] else ""  # 统计显著标记
            print(f"  ║ {f['fault']:<16} Agent={f['agent_crash_rate']:.0%} "
                  f"Cascade={f['cascade_crash_rate']:.0%} "
                  f"p={f['fisher_p']:.4f}{sig}")
        print(f"  ╚{'═'*40}╝")

        return fault_results  # 返回容错测试结果列表

    def _analyze_fault_pair(self, name: str, agent_results, cascade_results):  # name: 故障名称; agent_results: Agent运行结果列表; cascade_results: Cascade运行结果列表
        return self.fault_analyzer.analyze_component_failure(  # 调用分析器进行故障对比分析
            agent_results, cascade_results, name
        )

    # ═══════════════════════════════════════════════════
    # 工具方法
    # ═══════════════════════════════════════════════════

    def _count_ships(self, csv_path: str) -> int:  # csv_path: CSV文件路径
        import csv
        mmsi_set = set()  # 存储不重复的MMSI（船舶唯一标识）集合
        try:
            with open(csv_path, encoding="utf-8-sig") as f:  # 使用utf-8-sig编码打开CSV（兼容BOM头）
                reader = csv.DictReader(f)  # CSV字典读取器
                for row in reader:  # 遍历每一行记录
                    lower_row = {k.strip().lower(): v for k, v in row.items()}  # 将字段名统一转为小写并去除空白
                    m = lower_row.get("mmsi", "").strip()  # 获取MMSI字段值并去除首尾空白
                    if m:  # 如果有有效MMSI
                        mmsi_set.add(m)  # 加入集合去重
        except Exception:  # 忽略文件读取异常
            pass
        return len(mmsi_set)  # 返回去重后的船舶数量

    # ═══════════════════════════════════════════════════
    # 报告生成
    # ═══════════════════════════════════════════════════

    def generate_report(self):  # 生成实验报告
        print(f"\n{'='*60}")
        print(f"  生成实验报告")
        print(f"{'='*60}")

        report_path = str(self.output_dir / "experiment_report.md")  # Markdown报告文件路径
        json_path = str(self.output_dir / "experiment_data.json")  # JSON数据文件路径
        chart_dir = str(self.output_dir / "charts")  # 图表输出目录

        print(f"\n  [1/4] 生成 Markdown 报告...")
        self.reporter.build_experiment_report(  # 构建实验报告内容
            speed_results=self.results.get("speed", {}),  # 速度实验结果
            capacity_results=self.results.get("practicality", {}),  # 实用性实验结果
            fault_results=self.results.get("fault_tolerance_components", []),  # 容错实验结果
            config=self.results.get("config", {}),  # 实验配置信息
        )
        self.reporter.save(report_path)  # 保存Markdown报告到文件

        print(f"  [2/4] 保存原始 JSON 数据...")
        self.reporter.save_json_results(self.results, json_path)  # 保存JSON格式的实验数据

        print(f"  [3/4] 生成 PNG 图表...")
        charts = self.chart_gen.generate_all(  # 生成所有可视化图表
            self.results.get("speed", {}),  # 速度实验数据
            self.results.get("practicality", {}),  # 实用性实验数据
            chart_dir,  # 图表输出目录
        )
        for c in charts:  # 遍历生成的图表文件
            print(f"    → {Path(c).name} 已生成")

        print(f"  [4/4] 生成汇总 CSV...")
        csv_path = self._generate_summary_csv()  # 生成汇总CSV文件

        print(f"\n  报告完成:")
        print(f"    📄 报告:  {report_path}")
        print(f"    📊 数据:  {json_path}")
        print(f"    🖼️  图表:  {chart_dir}/ ({len(charts)} 张)")
        print(f"    📋 汇总:  {csv_path}")

        return report_path  # 返回报告文件路径

    def _generate_summary_csv(self) -> str:  # 生成实验汇总CSV文件
        import csv
        csv_path = str(self.output_dir / "experiment_summary.csv")  # 汇总CSV文件路径
        all_rows = []  # 所有实验结果行列表
        field_names = None  # CSV字段名（首次发现时确定）

        for subdir in sorted(self.output_dir.iterdir()):  # 遍历输出目录的所有子目录
            if not subdir.is_dir():  # 跳过非目录项
                continue
            dirname = subdir.name  # 子目录名（实验组标识）
            if dirname.startswith("speed_") or dirname.startswith("scaling_") \
               or dirname.startswith("capacity_") or dirname.startswith("fault_") \
               or dirname.startswith("dirty_"):  # 只处理实验相关的子目录
                for csv_file in sorted(subdir.glob("results_*.csv")):  # 遍历该实验组的所有结果CSV
                    try:
                        with open(csv_file, encoding="utf-8-sig") as f:  # 打开结果CSV文件
                            reader = csv.DictReader(f)  # CSV字典读取器
                            rows = list(reader)  # 读取所有行
                            if not rows:  # 跳过空文件
                                continue
                            if field_names is None:  # 首次获取字段名
                                field_names = list(rows[0].keys()) + ["experiment_group"]  # 追加实验组字段
                            for row in rows:  # 遍历每一行
                                row["experiment_group"] = dirname  # 标记该行所属实验组
                                all_rows.append(row)  # 加入汇总列表
                    except Exception as e:  # 忽略读取异常
                        pass

        if field_names and all_rows:  # 如果有数据可写入
            with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:  # 写入汇总CSV
                cols = ["experiment_group"] + [c for c in field_names if c != "experiment_group"]  # 列顺序：实验组在前
                writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")  # CSV写入器
                writer.writeheader()  # 写入表头
                writer.writerows(all_rows)  # 写入所有数据行
            print(f"    → {len(all_rows)} 条记录合并")
        else:  # 没有可合并的数据
            print(f"    → 无数据可合并")

        return csv_path  # 返回汇总CSV文件路径


def prepare_test_subset(source_csv: str, subset_dir: str, n_ships: int) -> dict:  # source_csv: 源CSV路径; subset_dir: 子集输出目录; n_ships: 需要的船舶数
    result = prepare_test_subsets(  # 调用通用的子集准备函数
        source_csv, subset_dir, ship_counts=[n_ships]
    )
    return result[0] if result else {"output": source_csv, "actual_ships": 0}  # 返回第一个子集结果；无数据则返回原始文件信息


# ═══════════════════════════════════════════════════
# 命令行入口
# ═══════════════════════════════════════════════════

def parse_args():  # 解析命令行参数
    parser = argparse.ArgumentParser(  # 命令行参数解析器
        description="AIS 三级级联架构 vs 原始 Agent 对比实验",
        formatter_class=argparse.RawDescriptionHelpFormatter,  # 帮助信息格式化器
        epilog="""使用示例:
    python experiments/run.py                               # 运行所有实验
    python experiments/run.py --speed                       # 只跑速度实验
    python experiments/run.py --practicality                # 只跑实用性实验
    python experiments/run.py --fault-tolerance             # 只跑容错实验
    python experiments/run.py --csv data/input/sample.csv
    python experiments/run.py --no-llm                      # 跳过 LLM
        """,
    )

    parser.add_argument("--speed", action="store_true",  # 速度实验开关
                        help="仅运行速度实验")
    parser.add_argument("--practicality", action="store_true",  # 实用性实验开关
                        help="仅运实用性实验")
    parser.add_argument("--fault-tolerance", action="store_true",  # 容错实验开关
                        help="仅运行容错实验")
    parser.add_argument("--csv", default=DEFAULT_CSV,  # 输入CSV文件路径
                        help=f"输入 CSV 文件路径")
    parser.add_argument("--output-dir", default=RESULTS_DIR,  # 输出目录
                        help=f"输出目录 (默认 {RESULTS_DIR})")
    parser.add_argument("--synthetic", type=int, default=0,  # 合成测试数据数量
                        help="生成 N 条合成测试用例（不运行实验）")
    parser.add_argument("--no-llm", action="store_true",  # 跳过LLM开关
                        help="跳过 LLM 层（仅加速器 + ML 路由）")

    return parser.parse_args()  # 返回解析后的命令行参数


def main():  # 主入口函数
    args = parse_args()  # 解析命令行参数

    # 纯合成数据生成模式
    if args.synthetic > 0:  # 如果指定了合成数据数量
        output = f"{DATA_DIR}/synthetic/test_set_{args.synthetic}.json"  # 合成测试集输出路径
        generate_synthetic_test_set(output, args.synthetic)  # 生成合成测试数据
        print(f"\n合成测试集已生成: {output}")
        return

    print(f"\n{'='*60}")
    print(f"  AIS 三级级联架构 vs 原始 Agent 对比实验")
    print(f"{'='*60}")
    print(f"  数据源:    {args.csv or DEFAULT_CSV}")
    print(f"  跳过 LLM: {'是' if args.no_llm else '否'}")
    print(f"  开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    runner = ExperimentRunner(args)  # 创建实验编排器实例

    run_speed = args.speed or not (args.practicality or args.fault_tolerance)  # 是否运行速度实验
    run_practicality = args.practicality or not (args.speed or args.fault_tolerance)  # 是否运行实用性实验
    run_fault = args.fault_tolerance or not (args.speed or args.practicality)  # 是否运行容错实验

    total_tests = sum([run_speed, run_practicality, run_fault])  # 需要运行的实验总数

    try:
        if run_speed:  # 运行速度实验
            runner.run_speed_experiment()

        if run_practicality:  # 运行实用性实验
            runner.run_practicality_experiment()

        if run_fault:  # 运行容错实验
            runner.run_fault_tolerance_experiment()

        # 生成报告
        report_path = runner.generate_report()  # 生成实验报告

        elapsed = timedelta(seconds=int(time.time() - runner._start_time))  # 总运行时长

        print(f"\n{'='*60}")
        print(f"  [全部完成] 实验结束!")
        print(f"{'='*60}")
        print(f"  总耗时:     {elapsed}")
        print(f"  输出目录:   {runner.output_dir}")
        print(f"  报告:       {report_path}")
        print(f"  汇总 CSV:   {runner.output_dir / 'experiment_summary.csv'}")
        print(f"  图表目录:   {runner.output_dir / 'charts/'}")
        print(f"{'='*60}")

    except KeyboardInterrupt:  # 用户按Ctrl+C中断
        print(f"\n\n{'!'*50}")
        print(f"  实验被用户中断")
        print(f"{'!'*50}")
        sys.exit(1)
    except Exception as e:  # 其他异常
        print(f"\n{'!'*50}")
        print(f"  实验异常终止: {e}")
        print(f"{'!'*50}")
        import traceback
        traceback.print_exc()  # 打印完整的异常堆栈信息
        sys.exit(1)


if __name__ == "__main__":
    main()
