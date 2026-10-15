"""
ais_accelerator/colreg.py — 让路义务判定（COLREG 第13/14/15条）与「让路船未让路」
=================================================================================

【这个文件做什么？】
  在 `collision.py` 已有的碰撞风险评估之上，补一层**归责**：
  这个局面里谁是让路船、它有没有实际采取让路行动。产出 `GIVE_WAY_FAILURE`。

【为什么与 collision.py 分开】
  两者回答的是不同的问题：
    - `collision_risk` 是**状态量**："这两条船有碰撞危险"（CRI 过线 + DCPA 进门）。
      危险可能来自双方，也可能来自环境（狭窄水道、能见度）。
    - `give_way_failure` 是**归责**："按 COLREG，这条船负有让路义务，而它没让"。
      它必须点出**具体哪一条船**没履行义务，所以判定的核心是相对方位而非 CRI。
  一个局面可以"有危险但无人违规"（双方都在正常避让），也可以"无危险而有人懈怠"。
  合成一个码，就等于把"有风险"直接说成"违规"，那不是法规口径。

【判定口径（别改错）】
  相对方位一律从**船首**起算、顺时针为正。`collision.CollisionRisk.relative_bearing`
  正是这个口径（本船船首起算、目标船方位），所以这里沿用同一套算法，
  不再自己写一遍——两处口径一旦分叉，下游对不上账。
    - 目标在本船正横后 22.5° 以外（112.5°~247.5°）= 本船在前、目标在后
      → **目标在追越本船**（第13条），本船是直航船。反过来本船位于目标的
      同一扇区、且速度上确实"赶得上"（`overtaking_speed_diff_knots`）→ 本船追越，
      本船让路。速度条件不可省：第13条管的是 "coming up with"（赶上来），
      只是跟在后面不算。
    - 目标在正前方 ±`give_way_head_on_degrees`、且**目标也看本船在正前方**
      = 对遇（第14条），双方各自向右转向 → 双方都是让路船。
    - 目标在本船右舷（0~112.5°）、且**本船位于目标的左舷**（247.5~360°）
      = 交叉相遇（第15条）→ 本船让路。
      这一档还额外要求两船航向差 ≥ `give_way_crossing_min_course_diff_degrees`：
      第15条说的是 "crossing"（航向交叉），航向差≈0 的两船航迹永不相交，
      那是平行/并靠而非交叉相遇（详见该阈值的注释）。
  后两档的**双侧互见**条件是必要条件，不是修饰：
    - 少了它，"同向跟随、对方正在正前方"会被判成对遇；
    - 少了它，"跟在别人正后方"（本船在目标 180° 处，既非正前方也非其左舷）
      会被判成交叉相遇。
  两者本来都不是"相遇"，谈不上让路义务，加上另一半方位后都会落回直航船。
  第16/17条不单独建码：第15条判出"谁该让"之后，第16条（让路船的行动）就是
  本模块判的"它让没让"，第17条（直航船）就是这里的 stand_on 分支。

【"让路失败"怎么算】
  两段式，缺一不可：
    1) **真的到了紧迫局面**——实测最近会遇距离 ≤ `give_way_close_quarters_nm`
       （0.2 海里）。量的必须是**实测**距离，不是 CRI、也不是预测 DCPA：
       这两项在本数据集上实测不可靠（预测中位数 0.70 海里而实测 1.60 海里，
       九成被点名的事件从未进过 5 海里），详见 config 里该阈值的注释。
    2) **没采取行动**——最近会遇点之前的航向变化 < `give_way_alteration_degrees`，
       且速度下降 < `give_way_speed_drop_knots`。只看幅度不看方向：转向避让该不该
       向右，留给 LLM 层结合证据点判断，这里把带符号的转向量一并记进 features
       （`course_change_signed_degrees`），免得下游重算。
  为什么以最近会遇点为界：第16条要求"尽可能及早地"行动，最近会遇点之后的
  转向是补救不是避让；同时这也天然界定了测量窗——在一段横跨数小时的过程中取
  航向变化的最大值，任何一次过弯都会被算成"已避让"，判据就没用了。
  哪几档态势算"负有让路义务"由 `give_way_duty_encounters` 决定，默认只有交叉
  相遇：30° 这条行动线是第16条「大幅度的行动」的口径，对遇与追越各自另有标准。
  CRI 在这里**不参与判定**，只作为细类分档量（取接近过程起点上的"若双方都不动"
  预测），与 `collision_risk` 同量纲同断点。

【与 encounter.py 的关系】
  两者都在 `InteractionIndex` 的候选船对上跑。区别在判据：
  `encounter.py` 判"追越通过时没让清"（量横距），本模块判"该让路的没动"（量航向/速度）。
  同一对船可能两个码都命中——那是对的，它们说的是不同的违规构成。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from bisect import bisect_left  # 时间轴二分查找（取时间最近的点对）
from dataclasses import dataclass  # 数据类型

from .collision import assess_at_closest  # 最近会遇点上的即时碰撞风险评估
from .config import AcceleratorConfig  # 加速器配置
from .encounter import (  # 复用遭遇索引与既有常量（同一套"接近过程"概念）
    _RUN_GAP_SECONDS,  # 单次接近过程允许的最大采样间隔（秒）
    InteractionIndex,  # 全船队时空索引
    _Pt,  # 索引内部的轻量点
    initial_bearing,  # 初始方位角
    nearest_time_index,  # 时间轴上最接近的点（同一个"同时刻配对"口径）
)
from .geometry import circular_angle_diff, haversine_nm  # 航向夹角、球面距离（海里）
from .models import CandidateEvent, CandidateType  # 数据模型


# 相遇态势取值（同时作为 features.encounter_type 的取值，下游按字符串读）
_OVERTAKING = "overtaking"  # 追越局面（第13条）
_HEAD_ON = "head_on"  # 对遇局面（第14条）
_CROSSING = "crossing"  # 交叉相遇局面（第15条）


def relative_bearing_of(own: object, target: object) -> float:  # 目标相对本船船首的方位
    """目标船相对**本船船首**的方位（度，顺时针；右舷 90°、正后方 180°）。

    与 `collision.CollisionRiskAssessor._relative_bearing` 同一口径，
    只是作用对象反过来（那边是"本船看目标"，这里两个方向都要看）。
    """
    return (initial_bearing(own.lat, own.lon, target.lat, target.lon) - own.course) % 360.0  # 减本船航向即相对方位


def signed_turn_degrees(first: float, second: float) -> float:  # 带符号的角度差
    """从 first 转到 second 的带符号角度差（度）：正 = 向右（顺时针）。

    `geometry.circular_angle_diff` 返回的是**绝对值**，而这里要方向：
    第14条要求"向右转向"、第15条要求"避免横越他船的前方"，避让方向本身就是
    判据的一部分，丢了符号下游就没法复核它往哪边让的。
    """
    return (second - first + 180.0) % 360.0 - 180.0  # 归一化到 (-180, 180]


@dataclass
class Encounter:  # 一次相遇态势的归责结果（本船视角）
    """本船在一次相遇中的角色判定。"""

    encounter_type: str  # overtaking / head_on / crossing
    own_is_give_way: bool  # 本船是否负有让路义务（False = 本船为直航船）
    relative_bearing: float  # 目标相对本船船首的方位（度）
    target_relative_bearing: float  # 本船相对目标船首的方位（度，双侧互见校验用）
    colreg_rule: str  # 该态势所依据的条款号


def classify_encounter(  # 判定相遇态势与让路角色
    own: object,  # 本船在某时刻的点（需 mmsi/lat/lon/speed/course）
    target: object,  # 目标船同一时刻的点
    config: AcceleratorConfig,  # 加速器配置
) -> Encounter:
    """判定相遇态势，并指出本船是否负有让路义务。

    判定顺序不可颠倒：追越优先于对遇、对遇优先于交叉。
    第13条对追越局面是**兜底**性质的（"不论本章各条如何规定"），
    一船只要在追越，就不再按交叉相遇分让路义务——反过来排会把
    "跟在别人正后方并赶上来"判成对遇（目标正在正前方）。
    """
    bearing = relative_bearing_of(own, target)  # 目标在本船的哪个方位
    reciprocal = relative_bearing_of(target, own)  # 本船在目标的哪个方位
    abaft_lo, abaft_hi = config.overtaking_abaft_degrees  # 正横后 22.5° 起算的追越扇区
    beam = abaft_lo  # 正横（含后 22.5°）＝右舷/左舷分界；与追越扇区共用同一条线
    head_on = config.give_way_head_on_degrees  # 对遇的"正前方"角宽（度）
    diff = config.overtaking_speed_diff_knots  # 构成"赶上来"所需的速度优势（节）
    min_crossing = config.give_way_crossing_min_course_diff_degrees  # 构成"交叉"所需的最低航向差（度）

    # 1) 追越（第13条）：目标在本船正横后且确实在赶上来 → 本船是直航船
    if abaft_lo <= bearing <= abaft_hi and target.speed > own.speed + diff:  # 目标在追越本船
        return Encounter(_OVERTAKING, False, bearing, reciprocal, "13")  # 本船让路义务在对方
    if abaft_lo <= reciprocal <= abaft_hi and own.speed > target.speed + diff:  # 本船在追越目标
        return Encounter(_OVERTAKING, True, bearing, reciprocal, "13")  # 本船让路

    # 2) 对遇（第14条）：双方互见对方在正前方 → 双方各自向右转向，双方都让路
    own_sees_ahead = bearing <= head_on or bearing >= 360.0 - head_on  # 目标在本船正前方
    target_sees_ahead = reciprocal <= head_on or reciprocal >= 360.0 - head_on  # 本船在目标正前方
    if own_sees_ahead and target_sees_ahead:  # 双侧互见才是对遇
        return Encounter(_HEAD_ON, True, bearing, reciprocal, "14")  # 本船让路

    # 3) 交叉（第15条）：目标在本船右舷、本船在目标左舷 → 本船让路
    #    还要求两船**航向确有交叉**：航向差≈0 的两船航迹永不相交（平行/并靠），
    #    一方落在另一方右舷正横上也不构成 "crossing"。缺了这一条，
    #    `illegal_towing` 那种并靠船对会被判成交叉相遇（详见 config 里该阈值的注释）。
    course_diff = circular_angle_diff(own.course, target.course)  # 两船航向夹角（度）
    if bearing < beam and reciprocal > abaft_hi and course_diff >= min_crossing:  # 互见对方在各自的让路侧
        return Encounter(_CROSSING, True, bearing, reciprocal, "15")  # 本船让路

    # 4) 其余：本船是直航船（第17条保持航向航速；对方的违规在它那一次迭代里判）
    return Encounter(_CROSSING, False, bearing, reciprocal, "17")  # 本船无让路义务


def _approach_rows(  # 逐点配对，取两船靠近且在动的点对
    px: list[_Pt],  # 本船轨迹（索引内的轻量点，按时间有序）
    py: list[_Pt],  # 目标船轨迹（同上）
    config: AcceleratorConfig,  # 加速器配置
) -> list[tuple[_Pt, _Pt, float, float, float]]:  # 行 = (本船点, 目标点, 距离, 本船看目标方位, 目标看本船方位)
    """返回时间窗内两船靠近时的点对（双侧方位都算好）。

    这一对是**同一时刻**的两船位置：报文时刻不会重合，所以取时间上最接近的那条
    目标船点（`nearest_time_index`），而不是窗口内第一条——错配一个窗口，两船各
    10 节时就是 0.33 海里的距离误差，而风险门 DCPA 只有 0.5 海里。

    两船速度下限沿用 `overtaking_min_speed_knots`：低速时 COG 就是噪声，
    由它算出来的相对方位没有意义——这与 encounter.py 里是同一个理由，
    所以共用同一个门槛，不为同一个物理事实再开一个旋钮。
    """
    ceiling = config.speed_abs_max_knots  # 速度上限：超过即 AIS 数据异常
    min_speed = config.overtaking_min_speed_knots  # 两船速度下限（低速时 COG 无意义）
    radius = config.collision_search_radius_nm  # 参与评估的最大距离（与碰撞风险同半径）
    min_dist = config.overtaking_min_distance_nm  # 距离下限（滤重叠/重复点）
    window = config.interaction_bucket_minutes * 60.0  # 时间配对窗（秒）
    y_times = [p.timestamp.timestamp() for p in py]  # 预取目标船时间轴，供二分

    rows: list[tuple[_Pt, _Pt, float, float, float]] = []  # 结果行
    for x in px:  # 遍历本船的点
        if x.speed < min_speed or x.speed > ceiling:  # 本船速度不适用
            continue  # 跳过
        xt = x.timestamp.timestamp()  # 本船该点时间
        j = nearest_time_index(y_times, xt)  # 时间上最接近的那条目标船点
        if j < 0 or abs(y_times[j] - xt) > window:  # 窗口内没有对方的点
            continue  # 这一时刻配不上对
        y = py[j]  # 与本船同一时刻的目标船点
        if y.speed < min_speed or y.speed > ceiling:  # 目标船速度不适用（须两船同时在动）
            continue  # 跳过
        dist = haversine_nm(x.lat, x.lon, y.lat, y.lon)  # 两船同时刻距离
        if dist < min_dist or dist > radius:  # 不在考察范围
            continue  # 跳过
        rows.append(  # 记下这一对（含双向方位）
            (x, y, dist, relative_bearing_of(x, y), relative_bearing_of(y, x))
        )
    return rows  # 按本船时间升序返回


def _split_windows(  # 把点对行切成若干"一次接近过程"
    rows: list[tuple[_Pt, _Pt, float, float, float]], config: AcceleratorConfig
) -> list[list[tuple[_Pt, _Pt, float, float, float]]]:  # 每个过程的行列表
    """按时间连续性切分并给单次过程封顶。

    封顶的理由与 `encounter.py` 里那段相同：300 秒的间隔容差会把几小时内反复
    擦肩的同一对船串成一次过程，于是"这一段里它有没有转向"就变成"这几个小时里
    它有没有转过向"——任何一条在航道里跑过的船都会命中，判据失去意义。
    时长上限沿用 `overtaking_max_minutes`：语义同为"一次接近过程最长多久"。
    """
    max_seconds = config.overtaking_max_minutes * 60.0  # 单次过程最长时间（秒）
    windows: list[list[tuple[_Pt, _Pt, float, float, float]]] = []  # 过程列表
    current: list[tuple[_Pt, _Pt, float, float, float]] = []  # 当前过程
    for row in rows:  # 遍历点对行
        if current:  # 已有当前过程
            now = row[0].timestamp.timestamp()  # 本行时间
            gap = now - current[-1][0].timestamp.timestamp()  # 与上一行的时间差
            span = now - current[0][0].timestamp.timestamp()  # 本过程已持续的时间
            if gap > _RUN_GAP_SECONDS or span > max_seconds:  # 间隔过久 或 过程过长
                windows.append(current)  # 收下
                current = []  # 重新开始
        current.append(row)  # 加入当前过程
    if current:  # 尾过程
        windows.append(current)  # 收下
    return windows  # 返回所有接近过程


def _approach_episodes(  # 从接近过程中提取相遇并量出让路船的行动量
    px: list[_Pt],  # 本船轨迹
    py: list[_Pt],  # 目标船轨迹
    config: AcceleratorConfig,  # 加速器配置
) -> list[dict]:  # 每次相遇一个 dict
    """把接近过程整理成"一次相遇"，并预先算好让路船在最近会遇点之前的行动量。

    返回项：
      own_points —— 最近会遇点（含）之前的本船轨迹，作证据点
      closest —— 最近会遇点对：(本船点, 目标点, 距离, 本船看目标方位, 目标看本船方位)
      start_pair —— 刚进入考察距离时的那一对（同 5 元组结构），判相遇态势用
      max_turn_degrees / signed_turn_degrees / speed_delta_knots —— 行动量三件套
    """
    episodes: list[dict] = []  # 结果
    for window_rows in _split_windows(_approach_rows(px, py, config), config):  # 逐接近过程
        closest_index = min(range(len(window_rows)), key=lambda i: window_rows[i][2])  # 最近会遇那一行
        own_points = [row[0] for row in window_rows[: closest_index + 1]]  # 到最近会遇点为止
        if len(own_points) < config.overtaking_min_points:  # 样本不足，量不出"有没有转向"
            continue  # 跳过（复用追越的点数下限：同为"一次接近过程的样本下限"）
        first = own_points[0]  # 过程起点
        turns = [signed_turn_degrees(first.course, p.course) for p in own_points]  # 逐点相对起点的转向量
        peak = max(range(len(turns)), key=lambda i: abs(turns[i]))  # 转向幅度最大的那一点
        last = own_points[-1]  # 过程终点（＝最近会遇点）
        episodes.append({  # 记录这次相遇
            "own_points": own_points,  # 本船轨迹（证据）
            "closest": window_rows[closest_index],  # 最近会遇点对
            # 过程起点那一对。判相遇态势要用**它**而不是最近会遇点，理由见
            # detect_give_way_failures 里 classify_encounter 上方的注释。
            "start_pair": window_rows[0],  # 刚进入考察距离时的那一对
            "start_time": first.timestamp,  # 起
            "end_time": last.timestamp,  # 止
            "duration_minutes": (last.timestamp - first.timestamp).total_seconds() / 60.0,  # 持续
            "max_turn_degrees": abs(turns[peak]),  # 最大航向改变量（度）
            "signed_turn_degrees": turns[peak],  # 同一时刻的带符号改变量（正=向右）
            "speed_delta_knots": min(p.speed for p in own_points) - first.speed,  # 最大减速量（负=减速）
        })
    return episodes  # 返回所有相遇

def detect_give_way_failures(  # 全船队让路失败检测
    index: InteractionIndex,  # 时空索引
    config: AcceleratorConfig,  # 加速器配置
) -> dict[str, list[CandidateEvent]]:  # 返回 {让路船MMSI: [事件]}
    """检测全船队的"让路船未让路"，按让路船归并返回。

    与 `detect_overtakings` 同构：一次算完、按船下发，船循环里不再做两两比较。
    区别是**两个方向都要试**——追越的速度优势条件是反对称的（两个方向最多
    成立一次），而让路义务在交叉/对遇局面下可能落在任一条船上，必须各判各的。
    """
    close_quarters = config.give_way_close_quarters_nm  # 构成紧迫局面的实测最近距离
    duty_types = config.give_way_duty_encounters  # 算作负有让路义务的相遇态势
    alter_limit = config.give_way_alteration_degrees  # 视为已避让的最小改向
    slow_limit = config.give_way_speed_drop_knots  # 视为已避让的最小减速
    result: dict[str, list[CandidateEvent]] = {}  # 结果容器

    for a, b in index._candidate_pairs(config):  # 遍历候选船对
        pa = index._tracks.get(a)  # A 的轨迹
        pb = index._tracks.get(b)  # B 的轨迹
        if not pa or not pb:  # 缺失
            continue  # 跳过
        span_a = index._span.get(a, (0.0, 0.0))  # A 的时间跨度
        span_b = index._span.get(b, (0.0, 0.0))  # B 的时间跨度
        if span_b[1] < span_a[0] or span_a[1] < span_b[0]:  # 时间不重叠
            continue  # 不可能相遇
        for own_track, target_track in ((pa, pb), (pb, pa)):  # 两个角色方向都要试
            for episode in _approach_episodes(own_track, target_track, config):  # 遍历相遇
                # 行是 5 元组 (本船点, 目标点, 距离, 本船看目标方位, 目标看本船方位)，
                # 见 _approach_rows 的返回类型。**别按 3 元组解包**——本轮做合成单测时
                # 就是这里抛的 ValueError：一旦有相遇过程就崩，而调用侧是逐船 try 的，
                # 于是 `give_way_failure` 在所有真实数据上静默恒为 0，看着像"没触发"，
                # 实则是"从来没跑过"。方位用不上（classify_encounter 自己会算）。
                closest_row = episode["closest"]  # 最近会遇点对
                own_last, target_last = closest_row[0], closest_row[1]  # 两船点
                dist = closest_row[2]  # **实测**最近会遇距离（判据量，不是预测值）
                # 此处原先是 CRI≥0.4 且**预测** DCPA≤0.5 海里两道门，在本数据上实测
                # 不可靠（见 config.give_way_close_quarters_nm 的注释：预测距离中位数
                # 只有实测的 1/2.3，且 CRI 过门后即饱和）。改量实测距离后，
                # `dist` 必须取 closest_row[2]——不能拿下面 risk.dcpa_nm，
                # 那是推出来的，正是被换掉的那个量。
                if dist > close_quarters:  # 没真的到紧迫局面，谈不上"未让路"
                    continue  # 跳过
                # 【为什么用过程起点判态势，而不用最近会遇点】COLREG 第15条问的是
                # 「对方从哪一侧驶来」——这是个**接近过程**的属性。到了最近会遇点上，
                # 两船已经并排（相对方位必然垂直于相对速度：以经典 90° 交叉为例，
                # 双方均 10 节时相对速度 225°，最近会遇点的相对方位只可能是 315° 或 135°），
                # 永远落不进本船右舷 0–112.5° 扇区，教科书式的交叉相遇会被判成
                # own_is_give_way=False，让路失败反倒报不出来。起点那一对才是
                # 「刚进入考察距离时对方在本船哪个舷侧」，与条款的问法一致。
                # 对遇/追越两支不受此影响（接近过程中方位近乎不变），
                # 双向互相看见的条件也因此照旧成立。
                start_row = episode["start_pair"]  # 刚进入考察距离时的那一对
                own_first, target_first = start_row[0], start_row[1]  # 两船点
                encounter = classify_encounter(own_first, target_first, config)  # 判角色
                if not encounter.own_is_give_way:  # 本船是直航船
                    continue  # 本次不算本船的违规
                if encounter.encounter_type not in duty_types:  # 该态势不在让路义务范围内
                    continue  # 见 config.give_way_duty_encounters：默认只有交叉相遇
                if (  # 已采取避让行动，任一命中即算
                    episode["max_turn_degrees"] >= alter_limit  # 转向到位
                    or episode["speed_delta_knots"] <= -slow_limit  # 或减速到位
                ):
                    continue  # 已履行让路义务
                # 风险量取**过程起点**上的"若双方都不动"预测：CRI 在这里只作细类
                # 分档量，不参与判定。取起点而不是最近会遇点，是因为最近会遇点上的
                # CRI 分布是**假的**——f_tcpa 在过 CPA 后取 0（tcpa≤0 时），于是
                # 同一批事件被这个符号跳变劈成两簇（实测 0.2 海里档 52/7/55），
                # 而起点 CRI 单调反映"不行动会多危险"（同档 5/33/76）。低危档偏薄
                # 是实情，不是 bug：进了 370 米还不动作的局面本来就很少"低危"。
                risk = assess_at_closest(own_first, target_first, config)  # 起点上的无行动预测
                own_mmsi = own_last.mmsi  # 让路船
                target_mmsi = target_last.mmsi  # 直航船
                xs = episode["own_points"]  # 证据点（本船轨迹）
                event = CandidateEvent(  # 构建候选事件
                    event_id=f"GWY-{own_mmsi}-{target_mmsi}-{int(episode['start_time'].timestamp())}",  # 事件ID
                    mmsi=own_mmsi,  # 主体是让路船
                    candidate_type=CandidateType.GIVE_WAY_FAILURE,  # 让路失败
                    start_time=episode["start_time"],  # 起点
                    end_time=episode["end_time"],  # 终点
                    priority="high" if risk.cri >= 0.8 else "middle",  # 高危未让路优先
                    features={  # 事件特征
                        "target_mmsi": target_mmsi,  # 直航船（对方）
                        "encounter_type": encounter.encounter_type,  # 相遇态势
                        "role": "give_way",  # 本船角色
                        "colreg_rule": encounter.colreg_rule,  # 依据条款
                        "relative_bearing": round(encounter.relative_bearing, 1),  # 目标相对本船船首方位
                        "target_relative_bearing": round(encounter.target_relative_bearing, 1),  # 本船相对目标船首方位
                        "cri": round(risk.cri, 3),  # 起点上的无行动预测（细类分档量，与 collision_risk 同断点）
                        # 下面两项与 cri 同一时刻（接近过程起点），是**预测**值：别当实测读，
                        # 实测的最近距离另有 `distance_nm`。留它们在 features 里是为了
                        # 细类那一档可复核（cri 由 dcpa/tcpa 等加权而来）。
                        "dcpa_nm": round(risk.dcpa_nm, 3),  # 起点预测的最近会遇距离
                        "tcpa_minutes": round(risk.tcpa_minutes, 2),  # 起点预测的到最近会遇点时间
                        "distance_nm": round(dist, 3),  # **实测**最近会遇距离（本码的判据量）
                        "close_quarters_threshold_nm": close_quarters,  # 判"紧迫局面"的距离线
                        "course_alteration_degrees": round(episode["max_turn_degrees"], 1),  # 实际改向幅度
                        "course_change_signed_degrees": round(episode["signed_turn_degrees"], 1),  # 带符号改向（正=向右）
                        "speed_change_knots": round(episode["speed_delta_knots"], 2),  # 速度变化（负=减速）
                        "duration_minutes": round(episode["duration_minutes"], 2),  # 接近过程时长
                        "point_count": len(xs),  # 点数
                        "alteration_threshold_degrees": alter_limit,  # 判"已避让"的改向线
                        "speed_drop_threshold_knots": slow_limit,  # 判"已避让"的减速线
                    },
                    evidence_points=[q.to_ais_point() for q in xs],  # 证据点＝本船轨迹（还原成 AISPoint，量很小）
                )
                result.setdefault(own_mmsi, []).append(event)  # 按让路船归并

    return result  # 返回结果
