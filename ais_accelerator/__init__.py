"""
ais_accelerator — AIS 数据加速器
================================

【这个包做什么？】
  对海量原始 AIS 船舶位置数据进行预处理和加速，是三级级联系统的第一级。
  核心功能：质量清洗 → 轨迹分段 → 事件检测 → 事件合并 → 轨迹压缩 → 标注输出。

【设计目标】
  1. Recall-first（召回优先）：宁可误报也不漏报，所有不确定性留给 LLM 做最终判断
  2. 零外部依赖：纯 Python 实现，不开箱即用
  3. 可追溯：每个标记都带来源模块、置信度、严重程度

【输出】
  最终产出 AnnotatedTrack（带标记的完整轨迹）和 CandidatePackage（候选事件包）。
  上层（ML Triage + LLM）基于这些结构化的中间数据做进一步判定。

【典型压缩效果】
  - 轨迹点压缩率：通常 > 95%（500点 → ~10点）
  - 候选事件过滤率：通常 > 90%

【架构】
  外观模式: facade.py 是对外统一接口
  管线模式: pipeline.py 编排所有处理步骤
"""

from .config import AcceleratorConfig  # 加速器配置类
from .facade import AISAccelerationFacade  # 外观类，对外统一接口
from .models import (  # 导入数据模型
    CandidatePackage,  # 候选事件包模型
    PipelineMetrics,  # 管线性能指标模型
    AnnotatedTrack,  # 带标注的轨迹模型
    AnnotationTag,  # 标注标签模型
    TagSource,  # 标签来源枚举
    TagSeverity,  # 标签严重级别枚举
)
from .regulation_db import RegulationDB  # 法规向量数据库
from .predict import TrajectoryPredictor  # 轨迹预测器
from .collision import CollisionRiskAssessor  # 碰撞风险评估器

__all__ = [  # 模块公开导出列表
    "AISAccelerationFacade",  # 外观类
    "AcceleratorConfig",  # 配置类
    "CandidatePackage",  # 候选事件包
    "PipelineMetrics",  # 管线指标
    "AnnotatedTrack",  # 标注轨迹
    "AnnotationTag",  # 标注标签
    "TagSource",  # 标签来源
    "TagSeverity",  # 标签严重级别
    "RegulationDB",  # 法规向量数据库
    "TrajectoryPredictor",  # 轨迹预测器
    "CollisionRiskAssessor",  # 碰撞风险评估器
]