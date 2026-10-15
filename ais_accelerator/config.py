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
    # 「存在碰撞危险」的最近会遇距离上限（海里，COLREG 第7条口径；0.5nm ≈ 926m）。
    # 为什么光有 CRI 门槛不够：CRI 里相对方位与相对速度两项合计权重 0.3，最近会遇
    # 距离那项即使几乎为 0，两者一凑仍能越过 0.4 的中风险线 —— 实测被 CRI 判为有风险
    # 的船对里，预测最近会遇距离中位数 4.19nm、p90 达 41nm，而参与评估的船对本来就都
    # 在 10nm 之内。所以"最近会遇得多近"必须单独把关，CRI 只在门内负责分档。
    collision_dcpa_gate_nm: float = Field(default=0.5, gt=0)  # 最近会遇距离上限（海里）

    # ── 流向场 / 流速场（从全船队轨迹统计，供"超速"与"逆向航行"判据参照） ──
    # 为什么不靠"区域限速"：限速写在 regulations.json 的 zone restrictions 里，
    # 而那些区域是示例数据，与真实数据海域不重叠时该判据永不触发。改成从数据
    # 本身学出参照值。字段的构建与查询见 ais_accelerator/flow.py。
    flow_cell_degrees: float = Field(default=0.02, gt=0)  # 统计格网边长（度）
    flow_min_samples: int = Field(default=30, ge=1)  # 格网最小样本量（航向场与流速场共用）
    flow_min_speed_knots: float = Field(default=3.0, ge=0)  # 只统计运动点：静止时 COG 是噪声
    flow_top_modes: int = Field(default=2, ge=1, le=4)  # 每格最多保留几个主导航向（分道通航制=2）
    flow_mode_separation_degrees: float = Field(default=60.0, gt=0, le=180)  # 两模态最小夹角（度）
    flow_speed_percentile: float = Field(default=0.90, gt=0, lt=1)  # 流速参照取的分位

    # ── 超速判据（数据驱动：绝对异常 + 同水域相对，任一命中即为候选） ──
    # 16 节与 1.20 倍都是**实测标定**的：在 fzais1009 上扫描过 6 组组合，
    # abs=16/ratio=1.20 命中 60 起 / 7 船；abs=25/ratio=1.60（原值）命中 0 起，
    # 也就是说原阈值下这一类**永远不会触发**。
    speed_abs_min_knots: float = Field(default=16.0, gt=0)  # 绝对超速下限（节）
    # 上限是刻意的：实测多船同时取到 102.3 节，那是 AIS 速度字段的饱和值（1023/10），
    # 属**数据异常**不属超速，交给 max_implied_speed_knots 的口径处理，不算超速。
    speed_abs_max_knots: float = Field(default=80.0, gt=0)  # 超过即视为AIS数据异常，不计入超速
    speed_relative_ratio: float = Field(default=1.20, gt=1)  # 超过同格网主流速的倍数
    speed_violation_min_minutes: float = Field(default=2.0, ge=0)  # 超速最短持续（分钟）
    speed_violation_min_points: int = Field(default=3, ge=1)  # 超速区间最少点数

    # ── 违规追越（COLREG 第13条：追越船应给被追越船让路，直至完全驶过让清） ──
    overtaking_search_radius_nm: float = Field(default=1.0, gt=0)  # 构成追越态势的最大两船距离（海里）
    overtaking_min_distance_nm: float = Field(default=0.02, ge=0)  # 距离下限：滤掉重叠/重复点对
    overtaking_min_speed_knots: float = Field(default=3.0, ge=0)  # 两船速度下限（低速时 COG 无意义）
    overtaking_speed_diff_knots: float = Field(default=1.0, gt=0)  # 追越船至少比被追越船快多少节
    overtaking_min_minutes: float = Field(default=2.0, ge=0)  # 追越态势最短持续（分钟）
    overtaking_min_points: int = Field(default=3, ge=1)  # 追越态势最少点数
    # 单次"接近过程"的最长时间。不封顶的话，300 秒间隔容差会把几小时内
    # 反复擦肩的同一对船串成一个过程，横距在整段上取最小值就没有意义了
    # （实测跑出过 135 分钟、横距 0.83nm 的"追越"，那显然不是一次通过）。
    overtaking_max_minutes: float = Field(default=30.0, gt=0)  # 单次接近过程最长时间（分钟）
    # 被追越船"正横后方"扇区：规则原文是"从他船正横后大于22.5度的某一方向赶上"
    # → 相对被追越船船首方位 112.5°~247.5°（180°±67.5°）。
    overtaking_abaft_degrees: tuple[float, float] = Field(default=(112.5, 247.5))
    # 「未让清」的安全横距：第13条的义务是 keep out of the way（保持让清），
    # 所以仅有"追越态势"还不够——必须同时实测到横距压到安全线以内才算违规。
    # 没有这一条就等于把"任何一次追越"都当违规，那不是法规口径。
    # 0.10 海里（约 185 米）是**实测标定**的：fzais1009 上不截断地量出横距分布
    # （p25=0.158 p50=0.287 p90=0.482），取 0.10 落在最近的四分之一，
    # 命中 47 起 / 38 船（占船队 7.1%）——一个可信的违规率。
    # 取 0.25（原值）会落到 105 船（44.7%），那是把正常会让也算成违规了。
    overtaking_unsafe_distance_nm: float = Field(default=0.10, gt=0)  # 追越过程中的最小横距安全线（海里）

    # ── 逆向航行（用流向场判定：对整个水域的所有合法主流向都逆向） ──
    counterflow_angle_degrees: float = Field(default=112.5, gt=0, le=180)  # 偏离最近主流向多少度算逆向
    counterflow_min_minutes: float = Field(default=3.0, ge=0)  # 逆向航行最短持续（分钟）
    counterflow_min_points: int = Field(default=5, ge=1)  # 逆向区间最少点数

    # ── 越界锚泊（区域内禁止锚泊 + 实测锚泊态） ──
    anchoring_speed_knots: float = Field(default=0.5, ge=0)  # 判定为锚泊的速度上限（节）
    anchoring_min_minutes: float = Field(default=30.0, ge=0)  # 锚泊态最短持续（分钟）
    anchoring_min_points: int = Field(default=10, ge=1)  # 锚泊区间最少点数

    # ── AIS 报文异常（与 ais_gap 互斥：这个是"报文收到了、但导航要素整体不可用"） ──
    # 判据在 quality.py 里生成（两项 AIS 标准"不可用"编码同时命中即打标记），
    # 这里只控制"多长的区间才够格报一个事件"。默认只要求 1 个点：
    # 门槛已经够严（SOG 饱和 102.3 **且** COG 不可用 360.0），单条报文两项全废
    # 就已是硬异常，再叠时长门槛只会把真实异常滤掉（实测全库仅 4 个点命中）。
    ais_off_min_points: int = Field(default=1, ge=1)  # 报文异常区间最少点数
    ais_off_min_minutes: float = Field(default=0.0, ge=0)  # 报文异常区间最短持续（分钟）

    # ── 捕捞作业（区域内＝法定违规；开阔水域＝作业嫌疑） ──
    # 两条路径共用同一组门槛：`illegal_fishing` 走禁渔区+禁渔期（法定），
    # `low_speed_fishing` 走开阔水域的低速作业特征（嫌疑）。原先区域分支用的是
    # 写死的 `len(run) < 2` 配 `low_speed_min_minutes`，与本组同义但更含糊。
    fishing_min_minutes: float = Field(default=5.0, ge=0)  # 捕捞区间最短持续（分钟）
    fishing_min_points: int = Field(default=3, ge=1)  # 捕捞区间最少点数

    # ── 船型判据（AIS 报文里的 VesselType 码）──
    # 【为什么需要它：速度代理不成立】
    #   改这一处之前，`illegal_fishing` 只认「区域内 + 船速 ≤ low_speed_enter_knots」，
    #   即把"捕捞"等同于"低速"。在 NOAA 官方禁渔区的真实 AIS 上实测：
    #   两条**商业渔船**（VesselType=1001/30）以 5.4、8.6 节**过境**法定禁捕区，
    #   区内 ≤3 节的点数为 0 → 系统对 2 条真实违规全部漏报。
    #   法定口径是「**商业渔船**在该水域内」，是**船型 + 在场**，与速度无关；
    #   而速度判据反过来还会把在该区域低速锚泊的货船误判成"捕捞"。
    #   所以判据改为：有船型时按船型判，无船型时才退回速度（见 detect._detect_zone_events）。
    #
    # 编码依据 AIS 标准与 MarineCadastre 船型表
    # （实验数据/HawaiiCoast_GT_GitHub/vessel_type_codes_2018.csv）：
    #   30   Fishing（通用渔船）
    #   1001 Commercial Fishing Vessel（商渔船）← 商业捕捞禁令针对的就是它
    #   1002 Fish Processing Vessel（水产加工船）
    fishing_vessel_codes: tuple[str, ...] = Field(
        default=("30", "1001", "1002"),
        description="属于『渔业』的 AIS 船型码（VesselType）",
    )
    commercial_fishing_vessel_codes: tuple[str, ...] = Field(
        default=("1001",),
        description="属于『商业捕捞』的船型码；区域禁令口径为 commercial 时用它判定",
    )

    # ── 让路义务（COLREG 第13/14/15条：追越、对遇、交叉相遇下的让路责任） ──
    # 与 collision_risk 的分工：那个是"有碰撞危险"的**状态量**，这个是"违反让路
    # 义务"的**归责**。判据是两段式：先要真的到了紧迫局面（实测最近会遇距离进门），
    # 再看负有让路义务的那一方是否实际采取了避让动作。
    #
    # 【为什么门限量的是"实测最近距离"而不是 CRI / 预测 DCPA】这两个量都出自恒速
    # 外推，在这批数据上已被测穿（全船队 4886 次让路局面 / 535 条船）：
    #   · 预测 DCPA 只是"双方都不动会靠多近"。而接近过程被 overtaking_max_minutes
    #     截断（实测窗口时长 p50=29.6 分钟，普遍顶到 30 分钟上限），最近会遇那一行
    #     多半不是真 CPA——预测值中位数 0.70 海里，实测最近距离中位数 1.60 海里、
    #     p90 达 4.97 海里：**九成被点名的事件实际从未进过 5 海里**。
    #   · CRI 的 f_dcpa 在 2 海里以下就饱和（dcpa≤0.5 时恒为 0.94），过门后中位数
    #     0.841，几乎不携带信息；拿它当门等于没设门。
    # 实测后果：旧判据（CRI≥0.4 且预测 DCPA≤0.5）命中 1622 起 / 358 条船 = 全船队
    # 66.9%；把这一族的门限扫到最严（时长≥5 分钟、预测 DCPA≤0.10、CRI≥0.8）仍有
    # 31.8% 的船被点名。**换门限救不回来，是判据本身错了。**
    # 改成量实测最近距离后（交叉相遇 + 实测未采取行动）：≤0.5 海里 248 起/29.7%、
    # ≤0.3 159/21.1%、≤0.2 **114 起 / 89 条船 / 16.6%**、≤0.1 61/9.5%。
    # 取 0.2 海里 ≈ 370 米：对船长一两百米的船来说就是擦身而过。这个值**严于**
    # collision_risk 的 0.5 海里门，两个量不能直接比：同一批交叉相遇里预测 DCPA
    # 中位数 0.70 而实测 1.60，即预测把距离低估约 2.3 倍，实测的 0.2 大致对应
    # 预测的 0.09，远在 0.5 那道门之内。
    give_way_close_quarters_nm: float = Field(default=0.2, gt=0)  # 构成紧迫局面的**实测**最近会遇距离（海里）
    # 哪几档相遇态势算"负有让路义务"。默认**只算交叉相遇（第15条）**：下面那条 30°
    # 的"已避让"线是第16条「大幅度的行动」的口径，只对交叉相遇成立——对遇（第14条）
    # 要求的是**小角度右转**、追越（第13条）可以靠保持距离或减速解除，狭窄水道里
    # 30° 往往根本转不出来。拿交叉的尺子去量它们，正是旧判据里对遇贡献 730/1622 起
    # （45%）的来源；而且实测在同一道 0.2 海里门内，对遇的细类分布是 0/8/141——
    # 几乎全落"高危"，那一档根本分不出层。留成可配置元组，是为了将来若给这两档
    # 各配自己的行动标准，能直接加进来而不必再动判据结构。
    give_way_duty_encounters: tuple[str, ...] = ("crossing",)  # 构成让路义务的相遇态势
    give_way_head_on_degrees: float = Field(default=6.0, gt=0, le=45)  # 对遇态势：目标在船首 ±该角度内
    # 「是否采取行动」的两条计量：转向到位 或 减速到位，任一命中即视为已让路。
    # 30° 与 2 节取的是"能被 AIS 分辨率稳定量出来、又明显大于噪声"的量级
    # （COG 上报本身有 ±1~2° 抖动、SOG 有 ±0.1 节抖动）。
    give_way_alteration_degrees: float = Field(default=30.0, gt=0, le=180)  # 视为已避让的最小改向（度）
    give_way_speed_drop_knots: float = Field(default=2.0, gt=0)  # 视为已避让的最小减速（节）
    # 「交叉相遇」的最低航向差。第15条的前提是两船**航向交叉**（crossing），
    # 而航向差≈0 的两船航迹永不相交——那是平行/并靠，不是交叉相遇。
    # 这一条是本轮做合成验证时才暴露出来的：`illegal_towing` 的典型形态
    # （间距 0.05 海里、同速同向并行 25 分钟）恰好让一方落在另一方的右舷正横上，
    # 光看相对方位就会判成"本船让路"，于是两条**并靠**的船被报成"让路船未让路"。
    # 而 give_way_failure 的严重度（80）高于 illegal_towing（72），合并时更重的那个
    # 码会把真正的类型盖掉——正是本轮一直在清理的"静默改判"。
    # 6° 取在 COG 上报抖动（±1~2°）之上，又低到不漏掉浅角度的真实交叉。
    give_way_crossing_min_course_diff_degrees: float = Field(default=6.0, ge=0, le=90)

    # ── 航道内锚泊（数据驱动的航道走廊，不依赖 regulations.json 的区域） ──
    # 与越界锚泊的分工：那个问"在不在禁锚水域"，这个问"在不在船队实际走出来的
    # 航道里"。走廊来自 water.py 的 build_corridor（密度≥2 的格子），故不受
    # "示例区域全在 38°N、与真实数据零重叠"的影响。
    fairway_anchoring_min_minutes: float = Field(default=10.0, ge=0)  # 航道内锚泊最短持续（分钟）

    # ── 违规拖带/并靠（两船长时间极小间距同速同行） ──
    # 0.10 海里 ≈ 185 米：普通货船船长就有一两百米，两船中心距压到这个量级
    # 只可能是并靠系泊或被拖带，不可能是各行其道。
    towing_max_separation_nm: float = Field(default=0.10, gt=0)  # 构成拖带/并靠的最大船间距（海里）
    towing_min_speed_knots: float = Field(default=3.0, ge=0)  # 两船速度下限（低于此 COG/同向判定都无意义）
    towing_speed_tolerance_knots: float = Field(default=0.5, gt=0)  # 两船允许的速度差（节，拖带必然同速）
    # 两船航向允许的夹角。拖带（拖轮在前、被拖船在后，或并靠系带）两船船首
    # 必然同向，30° 已经放得很宽——刻意的：这一档的可信度主要来自
    # 「0.1 海里内持续 20 分钟」这个组合门槛，航向只用来排除"各走各的"。
    towing_course_tolerance_degrees: float = Field(default=30.0, gt=0, le=180)  # 两船允许的航向差（度）
    towing_min_minutes: float = Field(default=20.0, ge=0)  # 拖带态势最短持续（分钟）
    towing_min_points: int = Field(default=5, ge=1)  # 拖带态势最少点数

    # ── 多船时空索引（碰撞/追越共用，防 O(N²)） ──
    interaction_cell_degrees: float = Field(default=0.02, gt=0)  # 索引格网边长（度）
    interaction_bucket_minutes: float = Field(default=1.0, gt=0)  # 时间桶宽度（分钟）
    collision_max_neighbors: int = Field(default=20, ge=1)  # 单船参与碰撞评估的邻居上限
    collision_overlap_pad_minutes: float = Field(default=30.0, ge=0)  # 时间重叠判定的前后余量（分钟）
    interaction_max_seconds: float = Field(default=180.0, gt=0)  # 单批多船检测硬预算（秒）

    @model_validator(mode="after")  # Pydantic 模型校验器（实例创建后执行）
    def validate_hysteresis(self) -> "AcceleratorConfig":  # 校验低速进出阈值的迟滞关系
        if self.low_speed_exit_knots <= self.low_speed_enter_knots:  # 如果退出阈值不大于进入阈值
            raise ValueError("low_speed_exit_knots must exceed low_speed_enter_knots")  # 抛出值错误
        return self  # 返回当前实例

    def ensure_directories(self) -> None:  # 确保缓存和输出目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)  # 创建缓存目录（含父目录）
        self.output_dir.mkdir(parents=True, exist_ok=True)  # 创建输出目录（含父目录）