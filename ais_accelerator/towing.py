"""
ais_accelerator/towing.py — 违规拖带/并靠检测（两船长时间极小间距同速同行）
============================================================================

【这个文件做什么？】
  在全船队时空索引上找出**长时间保持极小间距、速度一致、航向一致**的船对，
  产出 `ILLEGAL_TOWING` 候选事件。

【为什么这是一个独立的码】
  第45条管的是"拖带超长、超高、超宽、半潜的船舶……航行应采取措施并向海事
  管理机构报告"，第47条管的是"船舶停泊不得危及其他船舶、海上设施的安全"。
  共同的事实构成只有一条：**两条船长时间挤在极小的间距里同速同行**。
  这既不是碰撞危险（`collision_risk` 是"正在逼近"），也不是追越
  （`overtaking_violation` 是"从后方超越"），它是并行占道这种静态的碍航形态。

【判据】
  逐点条件（两条船都必须满足）：
    - 间距 ≤ `towing_max_separation_nm`（0.10 海里 ≈ 185 米，普通货船船长就有一两百米，
      中心距压到这个量级只可能是并靠系泊或被拖带，不可能是各行其道）
    - 两船速度都 ≥ `towing_min_speed_knots`（在动；0 节并靠的是泊位，不是航行中的违规）
    - |速度差| ≤ `towing_speed_tolerance_knots`（同速）
    - 航向差 ≤ `towing_course_tolerance_degrees`（同向）
  区间条件：持续 ≥ `towing_min_minutes`（20 分钟）且点数 ≥ `towing_min_points`。

  `towing_min_speed_knots` 是这道判据里最要紧的一环：它把"码头边并靠系泊"
  （速度≈0，天天都在发生，不是违规）与"航行中并靠/拖带"分开。少了它，
  凡是同一泊位上相邻的两条船都会被判违规。

【为什么两个方向都记一条】
  `encounter.py` 的追越是**反对称**的（追越船让路，被追越船不让路），所以只按
  追越船归并；拖带/并靠是**对称**的，两条船同等地构成这个事实，各自的责任也
  各自成立，所以每条船的记录里都以对方为 `target_mmsi` 各记一条。
  这也正是 `merge.py` 必须把本类型放进 `PAIRWISE_TYPES` 的原因——
  否则两条记录会被当成同一事件的重复而被合并掉，"和谁并靠"这个核心证据就丢了。

【为什么不用 regulations.json 的区域】
  与 `fairway_anchoring` 同理：禁拖水域要有人先画出来，而"两条船挤在一起跑"
  这件事本身在任何水域都是碍航的，判据不需要依赖人工画的区域。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

from .config import AcceleratorConfig  # 加速器配置
from .encounter import (  # 复用遭遇索引与既有常量（同一套"接近过程"概念）
    _RUN_GAP_SECONDS,  # 单次过程允许的最大采样间隔（秒）
    InteractionIndex,  # 全船队时空索引
    _Pt,  # 索引内部的轻量点
    nearest_time_index,  # 时间轴上最接近的点（同一个"同时刻配对"口径）
)
from .geometry import circular_angle_diff, haversine_nm  # 角度差与球面距离
from .models import CandidateEvent, CandidateType  # 数据模型


def _towing_rows(  # 逐点配对，取符合"并靠/拖带"逐点条件的点对
    px: list[_Pt],  # 船 X 的轨迹（索引内的轻量点，按时间有序）
    py: list[_Pt],  # 船 Y 的轨迹（同上）
    config: AcceleratorConfig,  # 加速器配置
) -> list[tuple[_Pt, _Pt, float, float, float]]:  # 行 = (X点, Y点, 间距, 速度差, 航向差)
    """返回两船满足"极小间距 + 同速 + 同向 + 都在动"的点对。

    结构照搬 `encounter._close_approach_rows`：X 的每个点配 Y 时间轴上**最接近**
    的那一条（`nearest_time_index`）。间距这一档对配对误差最敏感——判据上限只有
    0.10 海里，而错配一个 60 秒的窗口、两船各 10 节就是 0.167 海里的距离误差，
    比门槛本身还大（报文间隔实测可达 2~3 分钟，这不是边角情形）。
    """
    max_sep = config.towing_max_separation_nm  # 构成拖带/并靠的最大间距（海里）
    min_speed = config.towing_min_speed_knots  # 两船速度下限（在动）
    tol_speed = config.towing_speed_tolerance_knots  # 速度差上限（同速）
    tol_course = config.towing_course_tolerance_degrees  # 航向差上限（同向）
    min_dist = config.overtaking_min_distance_nm  # 距离下限（滤重叠/重复点）
    window = config.interaction_bucket_minutes * 60.0  # 时间配对窗（秒）
    y_times = [p.timestamp.timestamp() for p in py]  # 预取 Y 的时间轴，供二分

    rows: list[tuple[_Pt, _Pt, float, float, float]] = []  # 结果行
    for x in px:  # 遍历 X 的点
        if x.speed < min_speed:  # X 不在动
            continue  # 跳过
        xt = x.timestamp.timestamp()  # X 的时间
        j = nearest_time_index(y_times, xt)  # 时间上最接近的那条 Y 点
        if j < 0 or abs(y_times[j] - xt) > window:  # 窗口内没有对方的点
            continue  # 这一时刻配不上对
        y = py[j]  # 与 X 同一时刻的 Y 点
        if y.speed < min_speed:  # Y 不在动
            continue  # 跳过
        dist = haversine_nm(x.lat, x.lon, y.lat, y.lon)  # 两船同时刻间距
        if dist < min_dist or dist > max_sep:  # 不在"极小间距"范围内
            continue  # 跳过
        speed_diff = x.speed - y.speed  # 速度差（带符号，留着看谁快）
        if abs(speed_diff) > tol_speed:  # 速度不一致
            continue  # 跳过
        course_diff = circular_angle_diff(x.course, y.course)  # 航向夹角（度）
        if course_diff > tol_course:  # 航向不一致
            continue  # 跳过
        rows.append((x, y, dist, speed_diff, course_diff))  # 记下
    return rows  # 按 X 时间升序返回


def _towing_episodes(  # 从点对行中提取持续够久的并靠/拖带区间
    px: list[_Pt],  # 本船轨迹
    py: list[_Pt],  # 对方轨迹
    config: AcceleratorConfig,  # 加速器配置
) -> list[dict]:  # 每段并靠/拖带一个 dict
    """把逐点配对结果按时间连续性切成区间，并筛掉不够长的。

    切分用 `_RUN_GAP_SECONDS`（与 encounter.py 同一个容差）：AIS 上报间隔实测
    可达 2~3 分钟，卡得太紧会把一段连续并靠切成几十段碎片，每段都够不上时长门槛。
    """
    min_minutes = config.towing_min_minutes  # 并靠/拖带最短持续（分钟）
    min_points = config.towing_min_points  # 最少点数

    episodes: list[dict] = []  # 结果
    current: list[tuple[_Pt, _Pt, float, float, float]] = []  # 当前区间
    runs: list[list[tuple[_Pt, _Pt, float, float, float]]] = []  # 切好的区间
    for row in _towing_rows(px, py, config):  # 遍历点对行
        if current:  # 已有当前区间
            gap = row[0].timestamp.timestamp() - current[-1][0].timestamp.timestamp()  # 与上一行的时间差
            if gap > _RUN_GAP_SECONDS:  # 间隔过久 → 断开
                runs.append(current)  # 收下
                current = []  # 重新开始
        current.append(row)  # 加入当前区间
    if current:  # 尾区间
        runs.append(current)  # 收下

    for window_rows in runs:  # 遍历每个候选区间
        duration = (  # 区间持续时长（分钟）
            window_rows[-1][0].timestamp - window_rows[0][0].timestamp  # 首尾时间差
        ).total_seconds() / 60.0
        if len(window_rows) < min_points or duration < min_minutes:  # 点数或时长不足
            continue  # 不算一次并靠/拖带
        distances = [row[2] for row in window_rows]  # 区间内的间距序列
        speed_diffs = [row[3] for row in window_rows]  # 区间内的速度差序列
        course_diffs = [row[4] for row in window_rows]  # 区间内的航向差序列
        episodes.append({  # 记录这次并靠/拖带
            "points": [row[0] for row in window_rows],  # 本船轨迹（证据）
            "target_mmsi": window_rows[0][1].mmsi,  # 对方 MMSI
            "start_time": window_rows[0][0].timestamp,  # 起
            "end_time": window_rows[-1][0].timestamp,  # 止
            "duration_minutes": duration,  # 持续
            "min_separation_nm": min(distances),  # 最近间距
            "avg_separation_nm": sum(distances) / len(distances),  # 平均间距
            "avg_speed_diff_knots": sum(speed_diffs) / len(speed_diffs),  # 平均速度差
            "avg_course_diff_degrees": sum(course_diffs) / len(course_diffs),  # 平均航向差
        })
    return episodes  # 返回所有区间


def detect_towings(  # 全船队违规拖带/并靠检测
    index: InteractionIndex,  # 时空索引
    config: AcceleratorConfig,  # 加速器配置
) -> dict[str, list[CandidateEvent]]:  # 返回 {本船MMSI: [事件]}
    """检测全船队的违规拖带/并靠，按船归并返回。

    候选船对复用 `InteractionIndex._candidate_pairs`：它的 3×3 格邻域 + ±1 时间桶
    对"间距 0.1 海里"这种极近的船对绰绰有余（追越的搜索半径比它大一个量级）。
    """
    max_sep = config.towing_max_separation_nm  # 阈值（记进特征，便于复核）
    tol_speed = config.towing_speed_tolerance_knots  # 速度差阈值
    tol_course = config.towing_course_tolerance_degrees  # 航向差阈值
    result: dict[str, list[CandidateEvent]] = {}  # 结果容器

    for a, b in index._candidate_pairs(config):  # 遍历候选船对
        pa = index._tracks.get(a)  # A 的轨迹
        pb = index._tracks.get(b)  # B 的轨迹
        if not pa or not pb:  # 缺失
            continue  # 跳过
        span_a = index._span.get(a, (0.0, 0.0))  # A 的时间跨度
        span_b = index._span.get(b, (0.0, 0.0))  # B 的时间跨度
        if span_b[1] < span_a[0] or span_a[1] < span_b[0]:  # 时间不重叠
            continue  # 不可能同行
        for own_track, target_track in ((pa, pb), (pb, pa)):  # 对称事实，两个方向各记一条
            for episode in _towing_episodes(own_track, target_track, config):  # 遍历并靠区间
                xs = episode["points"]  # 本船轨迹（证据）
                own_mmsi = xs[0].mmsi  # 本船
                target_mmsi = episode["target_mmsi"]  # 对方
                duration = episode["duration_minutes"]  # 持续时长
                event = CandidateEvent(  # 构建候选事件
                    event_id=f"TOW-{own_mmsi}-{target_mmsi}-{int(episode['start_time'].timestamp())}",  # 事件ID
                    mmsi=own_mmsi,  # 主体
                    candidate_type=CandidateType.ILLEGAL_TOWING,  # 违规拖带/并靠
                    start_time=episode["start_time"],  # 起点
                    end_time=episode["end_time"],  # 终点
                    priority="high" if duration >= 60.0 else "middle",  # 长时间并靠为高
                    features={  # 事件特征
                        "target_mmsi": target_mmsi,  # 对方（核心证据：和谁并靠）
                        "duration_minutes": round(duration, 2),  # 持续时长（细类分档量级）
                        "min_separation_nm": round(episode["min_separation_nm"], 3),  # 最近间距
                        "avg_separation_nm": round(episode["avg_separation_nm"], 3),  # 平均间距
                        "speed_diff_knots": round(episode["avg_speed_diff_knots"], 2),  # 平均速度差
                        "course_diff_degrees": round(episode["avg_course_diff_degrees"], 1),  # 平均航向差
                        "matched_course": True,  # 已满足同向条件（判据成立的必要条件之一）
                        "point_count": len(xs),  # 点数
                        "max_separation_nm": max_sep,  # 判定所用间距阈值（可追溯）
                        "speed_tolerance_knots": tol_speed,  # 判定所用速度差阈值
                        "course_tolerance_degrees": tol_course,  # 判定所用航向差阈值
                    },
                    evidence_points=[q.to_ais_point() for q in xs],  # 证据点＝本船轨迹
                )
                result.setdefault(own_mmsi, []).append(event)  # 按本船归并

    return result  # 返回结果
