"""
ais_accelerator/metrics.py — 指标持久化工具
=============================================

【这个文件做什么？】
  将 PipelineMetrics 保存为 JSON 文件，便于实验复现和对比分析。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import json  # JSON序列化模块
from pathlib import Path  # 路径处理模块

from .models import PipelineMetrics  # 管线指标模型


def save_metrics(metrics: PipelineMetrics, path: Path) -> None:  # 将指标保存为JSON文件
    path.parent.mkdir(parents=True, exist_ok=True)  # 创建目标目录（含父目录）
    path.write_text(  # 写入JSON文件
        json.dumps(metrics.summary(), ensure_ascii=False, indent=2, default=str),  # 序列化为格式化JSON字符串
        encoding="utf-8",  # 使用UTF-8编码
    )