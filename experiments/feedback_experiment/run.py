#!/usr/bin/env python3
"""
experiments/feedback_experiment/run.py — 反馈循环实验统一入口
=============================================================

三个子实验：
    实验A — 分歧率摸底：统计 ML vs LLM 的一致/分歧模式（直接分析已收集数据）
    实验B — 阈值扫描：扫描 RETRAIN_THRESHOLD ∈ [5,10,15,...,50]，找最优阈值
    实验C — 反馈有无对照：用实验B推荐阈值，多轮 A/B 对照验证反馈是否有效

数据流：
    Phase 1（数据收集）→ 缓存 JSON
                               ↓
    Phase 2（实验B：阈值扫描）→ simulation_results.json
                               ↓
    Phase 3（实验C：有无对照）→ 基于推荐阈值
                               ↓
    Phase 4（报告生成）→ 三实验完整报告 + 图表

用法:
    # 全自动（推荐）
    python -m experiments.feedback_experiment.run

    # 分步
    python -m experiments.feedback_experiment.run --collect-only  # 仅收集数据
    python -m experiments.feedback_experiment.run --simulate-only # 仅实验B
    python -m experiments.feedback_experiment.run --experiment-c  # 仅实验C
    python -m experiments.feedback_experiment.run --report-only   # 仅出报告

    # 指定数据源
    python -m experiments.feedback_experiment.run --csv data/input/xxx.csv

    # 强制重跑
    python -m experiments.feedback_experiment.run --recollect
"""
from __future__ import annotations

import argparse  # 命令行参数解析模块
import sys  # 系统模块，用于路径操作和退出
import time  # 时间模块，用于计时
from datetime import datetime  # 日期时间类
from pathlib import Path  # 路径处理类


def _ensure_imports():
    """确保项目根目录在 sys.path 中。"""
    project_root = Path(__file__).resolve().parent.parent.parent  # 项目根目录的绝对路径
    if str(project_root) not in sys.path:  # 如不在路径列表中
        sys.path.insert(0, str(project_root))  # 将项目根目录插入到 sys.path 最前面


def run_collect(args) -> list[dict]:
    """Phase 1: 数据收集。"""
    from .data_collector import load_or_collect  # 数据收集函数

    print(f"\n{'='*60}")
    print(f"  Phase 1: 数据收集")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    data = load_or_collect(  # 收集或加载船舶数据
        csv_path=args.csv,  # CSV 文件路径
        max_ships=args.max_ships,  # 最多处理船舶数
        no_llm=args.no_llm,  # 是否跳过 LLM
        force_recollect=args.recollect,  # 是否强制重新收集
    )
    return data  # 返回船舶数据列表


def run_simulate(args) -> dict:
    """实验B: 执行阈值扫描。"""
    from .config import THRESHOLDS, CACHE_FILE, BASE_MODEL_PATH  # 实验配置常量
    from .threshold_simulator import run_threshold_scan, summarize_results  # 阈值扫描和汇总函数

    print(f"\n{'='*60}")
    print(f"  实验B: 阈值扫描")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 加载数据
    if not CACHE_FILE.exists():  # 缓存文件不存在
        print(f"\n错误: 未找到数据缓存: {CACHE_FILE}")
        print("请先运行: python -m experiments.feedback_experiment.run --collect-only")
        sys.exit(1)

    import json  # JSON 处理模块
    with open(CACHE_FILE, "r", encoding="utf-8") as f:  # 打开缓存文件
        ship_data = json.load(f)  # 加载船舶数据列表
    print(f"\n[数据] 已加载 {len(ship_data)} 条船舶记录")

    n_with_llm = sum(1 for s in ship_data if s.get("llm_label") is not None)  # 含 LLM 标签的记录数
    print(f"[数据] 含 LLM 标签: {n_with_llm} 条")
    if n_with_llm < 20:  # 数据不足 20 条
        print("⚠ 含 LLM 标签数据不足 20 条，结果可能不可靠")

    # 运行模拟
    results = run_threshold_scan(  # 执行阈值扫描
        ship_data=ship_data,  # 船舶数据列表
        base_model_path=BASE_MODEL_PATH,  # 基础模型路径
        thresholds=THRESHOLDS,  # 待扫描的阈值列表
        train_ratio=args.train_ratio,  # 训练池比例
    )

    summary = summarize_results(results)  # 汇总扫描结果

    # 打印摘要
    print(f"\n{'='*60}")
    print(f"  阈值扫描完成!")
    print(f"{'='*60}")
    print(f"\n{'阈值':>6} {'初始':>8} {'最终':>8} {'提升':>8} {'训练':>6} {'收敛':>6}")
    print(f"{'─'*50}")
    for row in summary.get("summary_table", []):  # 遍历汇总表中的每一行
        print(f"{row['threshold']:>4}条 "
              f"{row['initial']:>8} {row['final']:>8} {row['improvement']:>8} "
              f"{row['retrains']:>3}次 {row['converged']:>6}")
    if summary.get("best_threshold"):  # 有推荐阈值
        print(f"\n推荐阈值: {summary['best_threshold']} 条 "
              f"(最终准确率 {summary['best_final_accuracy']})")

    # 保存
    from .config import RESULTS_DIR  # 结果输出目录
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    json_path = RESULTS_DIR / "simulation_results.json"  # 模拟结果 JSON 路径
    serializable = {  # 可序列化的结果字典
        "summary_table": summary["summary_table"],  # 结果汇总表
        "best_threshold": summary["best_threshold"],  # 最佳阈值
        "best_final_accuracy": summary["best_final_accuracy"],  # 最优最终准确率
        "all_results": [  # 所有阈值的详细结果列表
            {
                "threshold": r["threshold"],  # 阈值
                "initial_accuracy": r["initial_accuracy"],  # 初始准确率
                "final_accuracy": r["final_accuracy"],  # 最终准确率
                "accuracy_improvement": r["accuracy_improvement"],  # 准确率提升
                "multi_accuracy_trajectory": r.get("multi_accuracy_trajectory", []),  # 多轮准确率轨迹
                "total_retrains": r["total_retrains"],  # 总训练次数
                "total_records_used": r["total_records_used"],  # 总训练样本数
                "converged": r.get("converged", False),  # 是否收敛
            }
            for r in summary.get("all_results", [])
        ],
    }
    with open(json_path, "w", encoding="utf-8") as f:  # 打开 JSON 文件
        json.dump(serializable, f, ensure_ascii=False, indent=2)  # 写入可序列化的结果数据
    print(f"\n[保存] 模拟结果 → {json_path}")

    return serializable  # 返回序列化结果


def run_experiment_c(args, sim_summary: dict = None) -> dict:
    """实验C: 运行反馈有无对照实验。"""
    from .config import CACHE_FILE, BASE_MODEL_PATH, RESULTS_DIR  # 实验配置常量
    from .experiment_c import run_comparison  # 实验C对比函数

    print(f"\n{'='*60}")
    print(f"  实验C: 反馈有无对照实验")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 加载数据
    if not CACHE_FILE.exists():  # 缓存文件不存在
        print(f"\n错误: 未找到数据缓存: {CACHE_FILE}")
        sys.exit(1)

    import json  # JSON 处理模块
    with open(CACHE_FILE, "r", encoding="utf-8") as f:  # 打开缓存文件
        ship_data = json.load(f)  # 加载船舶数据列表
    print(f"[数据] 已加载 {len(ship_data)} 条船舶记录")

    # 从实验B读取推荐阈值
    threshold = args.expc_threshold  # 命令行指定的实验C阈值
    if threshold is None and sim_summary:  # 未指定且实验B结果可用
        threshold = sim_summary.get("best_threshold")  # 使用实验B推荐阈值
    if threshold is None:  # 仍未确定
        # 尝试从缓存文件读
        sim_path = RESULTS_DIR / "simulation_results.json"  # 模拟结果文件
        if sim_path.exists():  # 文件存在
            with open(sim_path, "r", encoding="utf-8") as f:  # 打开结果文件
                cached = json.load(f)  # 加载缓存数据
            threshold = cached.get("best_threshold")  # 读取推荐阈值
    if threshold is None:  # 最终默认值
        threshold = 45  # 默认用实验B的结果
        print(f"[阈值] 未找到实验B结果，使用默认值 T={threshold}")

    print(f"[配置] 阈值={threshold}, 重复轮数={args.expc_rounds}")

    result = run_comparison(  # 执行对照实验
        ship_data=ship_data,  # 船舶数据列表
        base_model_path=BASE_MODEL_PATH,  # 基础模型路径
        threshold=threshold,  # 触发阈值
        n_rounds=args.expc_rounds,  # 重复轮数
        train_ratio=args.train_ratio,  # 训练池比例
        seed=args.seed,  # 随机种子
        use_bad_baseline=args.bad_baseline,  # 是否使用差模型基线
    )

    # 保存
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    json_path = RESULTS_DIR / "experiment_c_result.json"  # 实验C结果 JSON 路径
    with open(json_path, "w", encoding="utf-8") as f:  # 打开结果文件
        json.dump(result, f, ensure_ascii=False, indent=2)  # 写入实验C结果
    print(f"\n[保存] 实验C结果 → {json_path}")

    return result  # 返回实验C结果


def run_report(args, sim_summary: dict = None, expc_result: dict = None):
    """Phase 4: 生成完整报告。"""
    from .config import CACHE_FILE, RESULTS_DIR  # 缓存文件和结果目录配置
    from .report_generator import (  # 报告生成相关函数
        generate_markdown_report, generate_charts, save_raw_data,
    )

    print(f"\n{'='*60}")
    print(f"  Phase 4: 报告生成（三个子实验）")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")

    # 加载实验B结果
    if sim_summary is None:  # 未提供实验B结果
        sim_path = RESULTS_DIR / "simulation_results.json"  # 模拟结果文件
        if sim_path.exists():  # 文件存在
            import json  # JSON 处理模块
            with open(sim_path, "r", encoding="utf-8") as f:  # 打开结果文件
                sim_summary = json.load(f)  # 加载实验B结果
            print(f"[加载] 实验B结果: {sim_path}")

    # 加载实验C结果
    if expc_result is None:  # 未提供实验C结果
        expc_path = RESULTS_DIR / "experiment_c_result.json"  # 实验C结果文件
        if expc_path.exists():  # 文件存在
            import json  # JSON 处理模块
            with open(expc_path, "r", encoding="utf-8") as f:  # 打开结果文件
                expc_result = json.load(f)  # 加载实验C结果
            print(f"[加载] 实验C结果: {expc_path}")

    # 加载船舶数据
    ship_data = []  # 船舶数据列表
    if CACHE_FILE.exists():  # 缓存文件存在
        import json  # JSON 处理模块
        with open(CACHE_FILE, "r", encoding="utf-8") as f:  # 打开缓存文件
            ship_data = json.load(f)  # 加载船舶数据

    data_source = args.csv or "（自动）"  # 数据源描述
    ship_count = len(ship_data)  # 船舶总数
    test_size = int(ship_count * (1 - args.train_ratio))  # 测试集大小

    # 4a. Markdown 报告
    print("\n  [1/4] 生成 Markdown 报告...")
    report = generate_markdown_report(  # 生成 Markdown 报告
        summary=sim_summary or {},  # 实验B结果汇总
        ship_count=ship_count,  # 船舶总数
        data_source=data_source,  # 数据源描述
        test_size=test_size,  # 测试集大小
        config={"seed": args.seed},  # 实验配置
        ship_data=ship_data,  # 船舶数据列表
        experiment_c_result=expc_result,  # 实验C结果
    )
    report_path = RESULTS_DIR / "experiment_report.md"  # 报告 Markdown 文件路径
    report_path.write_text(report, encoding="utf-8")  # 写入报告文件
    print(f"  → {report_path}")

    # 4b. 图表
    print("\n  [2/4] 生成图表...")
    chart_dir = RESULTS_DIR / "charts"  # 图表输出目录
    charts = generate_charts(  # 生成图表
        sim_summary or {}, str(chart_dir),  # 实验B结果和图表目录
        ship_data=ship_data, experiment_c_result=expc_result,  # 船舶数据和实验C结果
    )
    for c in charts:  # 遍历生成的图表
        print(f"  → {Path(c).name}")

    # 4c. 原始数据
    print("\n  [3/4] 保存原始数据...")
    save_raw_data(  # 保存原始数据
        sim_summary or {}, ship_count, data_source, str(RESULTS_DIR),  # 实验B结果和配置
        ship_data=ship_data, experiment_c_result=expc_result,  # 船舶数据和实验C结果
    )

    print(f"\n{'='*60}")
    print(f"  实验完成!")
    print(f"{'='*60}")
    print(f"  报告: {report_path}")
    print(f"  图表: {chart_dir}/")
    print(f"  数据: {RESULTS_DIR / 'experiment_data.json'}")
    print(f"{'='*60}")

    if sim_summary and sim_summary.get("best_threshold"):  # 有推荐阈值
        print(f"\n  实验B推荐阈值 = {sim_summary['best_threshold']} 条")

    return report_path  # 返回报告文件路径


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(  # 参数解析器
        description="LightGBM 反馈循环实验（三个子实验）",
        formatter_class=argparse.RawDescriptionHelpFormatter,  # 帮助信息格式
    )

    # 阶段控制
    parser.add_argument("--collect-only", action="store_true",  # 仅数据收集标志
                        help="仅数据收集（Phase 1）")
    parser.add_argument("--simulate-only", action="store_true",  # 仅实验B标志
                        help="仅实验B阈值扫描")
    parser.add_argument("--experiment-c", action="store_true",  # 仅实验C标志
                        help="仅实验C反馈有无对照")
    parser.add_argument("--report-only", action="store_true",  # 仅生成报告标志
                        help="仅生成报告")

    # 数据
    parser.add_argument("--csv", default=None,  # CSV 文件路径参数
                        help="输入 CSV 文件路径")
    parser.add_argument("--max-ships", "-n", type=int, default=None,  # 最多船舶数参数
                        help="最多处理船舶数")
    parser.add_argument("--no-llm", action="store_true",  # 跳过 LLM 标志
                        help="跳过 LLM（快速验证用）")
    parser.add_argument("--recollect", action="store_true",  # 强制重新收集标志
                        help="强制重新收集数据")

    # 模拟参数
    parser.add_argument("--train-ratio", type=float, default=0.70,  # 训练池比例参数
                        help="训练池比例 (默认 0.70)")
    parser.add_argument("--seed", type=int, default=42,  # 随机种子参数
                        help="随机种子 (默认 42)")

    # 实验C参数
    parser.add_argument("--expc-threshold", type=int, default=None,  # 实验C阈值参数
                        help="实验C使用的阈值（默认用实验B推荐值）")
    parser.add_argument("--expc-rounds", type=int, default=10,  # 实验C重复轮数参数
                        help="实验C重复轮数 (默认 10)")
    parser.add_argument("--bad-baseline", action="store_true",  # 差基线标志
                        help="实验C使用差模型基线（36.7%分歧率，代替默认6.8%）")

    args = parser.parse_args()  # 解析命令行参数
    args._phase_specified = any([  # 是否有指定阶段的标志
        args.collect_only, args.simulate_only,  # 仅收集或仅仿真
        args.experiment_c, args.report_only,  # 仅实验C或仅报告
    ])
    return args  # 返回解析后的参数对象


def main():
    """主入口函数。"""
    _ensure_imports()  # 确保导入路径
    args = parse_args()  # 解析命令行参数

    if args.csv and not Path(args.csv).exists():  # CSV 文件不存在
        print(f"错误: CSV 文件不存在: {args.csv}")
        sys.exit(1)

    start = time.time()  # 记录开始时间

    # Phase 1: 数据收集
    if args.collect_only or not args._phase_specified:  # 指定了仅收集或未指定阶段
        run_collect(args)  # 执行数据收集

    # 加载数据（供后续实验用）
    from .config import CACHE_FILE  # 缓存文件路径
    if CACHE_FILE.exists():  # 缓存文件存在
        import json  # JSON 处理模块
        with open(CACHE_FILE, "r", encoding="utf-8") as f:  # 打开缓存文件
            ship_data = json.load(f)  # 加载船舶数据
    else:  # 缓存文件不存在
        ship_data = []  # 空数据列表

    # 实验B: 阈值扫描
    sim_summary = None  # 实验B结果初始化
    if args.simulate_only or not args._phase_specified:  # 指定了仅仿真或未指定阶段
        sim_summary = run_simulate(args)  # 执行阈值扫描

    # 实验C: 反馈有无对照
    expc_result = None  # 实验C结果初始化
    if args.experiment_c or not args._phase_specified:  # 指定了仅实验C或未指定阶段
        expc_result = run_experiment_c(args, sim_summary)  # 执行对照实验

    # Phase 4: 报告生成
    if args.report_only or not args._phase_specified:  # 指定了仅报告或未指定阶段
        run_report(args, sim_summary, expc_result)  # 生成完整报告

    elapsed = time.time() - start  # 计算总耗时
    print(f"\n总耗时: {elapsed:.0f} 秒 ({elapsed/60:.1f} 分钟)")


if __name__ == "__main__":
    main()