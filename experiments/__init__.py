"""
experiments — 三级级联架构 vs 原始 Agent 对比实验框架
=====================================================

使用方式:
    python -m experiments.run  # 一键运行全部实验

框架结构:
    collectors/       ← 通过 subprocess 调用两个系统 CLI（观测者模式）
    analyzers/         ← 数学分析（Bootstrap/Wilcoxon/几何均值...）
    fault_injection/  ← 故障注入（kill Ollama/脏 CSV/删模型...）
    reporters/        ← 生成 Markdown 报告 + 图表
    data_prep.py      ← 测试数据准备
    run.py            ← 一键运行
"""