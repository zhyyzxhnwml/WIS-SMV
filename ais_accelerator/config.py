"""
ais_accelerator/config.py — 加速器全局配置
===========================================

【这个文件做什么？】
  集中管理 AIS 加速器的所有可调参数：
  - 文件路径（规则库、缓存、输出）
  - 批量处理参数（批次大小）
  - 轨迹分段阈值（时间间隔、最大段长）
  - 事件检测阈值（速度、航向、低速捕捞等）
  - 事件合并参数（时空距离）
  - 压缩参数（历史点数、最大规则数）

【设计原则】
  所有阈值集中在这里，不散落在各模块代码中。
  修改配置只需改这一个文件，无需翻代码。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from pathlib import Path  # 路径处理模块

from pydantic import BaseModel, Field, model_validator  # Pydantic 数据校验框架


PROJECT_ROOT = Path(__file__).resolve().parent.parent  # 项目根目录路径


class AcceleratorConfig(BaseModel):  # 加速器配置类，继承自 Pydantic BaseModel
    regulations_path: Path = PROJECT_ROOT / "regulations.json"  # 法规规则库文件路径
    cache_dir: Path = PROJECT_ROOT / "data" / "cache" / "accelerator"  # 缓存目录路径
    output_dir: Path = PROJECT_ROOT / "data" / "output" / "accelerator"  # 输出目录路径

    ingest_batch_size: int = Field(default=10_000, ge=100)  # 数据导入每批处理的行数
    ais_gap_minutes: float = Field(default=30.0, gt=0)  # 轨迹分段的时间间隔阈值（分钟）
    max_segment_minutes: float = Field(default=120.0, gt=0)  # 单个轨迹段最大时长（分钟）
    max_implied_speed_knots: float = Field(default=80.0, gt=0)  # 推算速度最大合理值（节），超过视为异常
    clean_points: bool = Field(
        default=True,
        description="质量清洗：剔除位置跳变点与重复点（不插值、不造假数据；剔除数计入 QualityReport.invalid_points）",
    )
    clean_on_land: bool = Field(
        default=True,
        description="陆地规避：结合陆地掩码+航道走廊，剔除远离航道落在陆地上的点（打 [陆地] 标记；clean_points=True 时剔除并计入 invalid_points）",
    )

    speed_candidate_margin: float = Field(default=0.95, gt=0, le=1)  # 速度候选值置信度边界
    speed_anomaly_ratio: float = Field(default=0.30, ge=0)  # 速度异常比率阈值
    low_speed_enter_knots: float = Field(default=3.0, ge=0)  # 进入低速状态的航速阈值（节）
    low_speed_exit_knots: float = Field(default=4.0, ge=0)  # 退出低速状态的航速阈值（节），需大于 enter
    low_speed_min_minutes: float = Field(default=5.0, ge=0)  # 低速状态最短持续时间（分钟）
    prior_normal_speed_knots: float = Field(default=8.0, ge=0)  # 船舶正常航速先验值（节）
    course_change_degrees: float = Field(default=90.0, gt=0, le=180)  # 航向变化检测阈值（度）
    course_rate_degrees_per_minute: float = Field(default=30.0, gt=0)  # 航向变化率阈值（度/分钟）

    merge_gap_minutes: float = Field(default=10.0, ge=0)  # 事件合并允许的最大时间间隔（分钟）
    merge_distance_nm: float = Field(default=3.0, ge=0)  # 事件合并允许的最大距离（海里）
    max_history_points: int = Field(default=5, ge=1, le=20)  # 轨迹压缩保留的历史点数
    max_rules: int = Field(default=5, ge=1, le=20)  # 事件检测最大规则数

    # ── 轨迹预测参数 ──
    predict_steps: int = Field(default=24, ge=1, le=120)  # 预测步数（默认24步，约30分钟）
    predict_step_minutes: float = Field(default=1.25, gt=0)  # 每步时间间隔（分钟，默认1.25分钟 → 24步≈30分钟）

    # ── 碰撞风险参数 ──
    cri_dcpa_threshold_nm: float = Field(default=2.0, gt=0)  # CRI 计算的 DCPA 安全阈值（海里）
    cri_tcpa_threshold_min: float = Field(default=30.0, gt=0)  # CRI 计算的 TCPA 安全阈值（分钟）
    cri_high_threshold: float = Field(default=0.7, ge=0, le=1)  # CRI 高风险阈值
    cri_medium_threshold: float = Field(default=0.4, ge=0, le=1)  # CRI 中风险阈值
    collision_search_radius_nm: float = Field(default=10.0, gt=0)  # 碰撞风险搜索邻近船的半径（海里）

    @model_validator(mode="after")  # Pydantic 模型校验器（实例创建后执行）
    def validate_hysteresis(self) -> "AcceleratorConfig":  # 校验低速进出阈值的迟滞关系
        if self.low_speed_exit_knots <= self.low_speed_enter_knots:  # 如果退出阈值不大于进入阈值
            raise ValueError("low_speed_exit_knots must exceed low_speed_enter_knots")  # 抛出值错误
        return self  # 返回当前实例

    def ensure_directories(self) -> None:  # 确保缓存和输出目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)  # 创建缓存目录（含父目录）
        self.output_dir.mkdir(parents=True, exist_ok=True)  # 创建输出目录（含父目录）