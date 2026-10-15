"""
ais_accelerator/encounter.py — 船-船遭遇（时空索引 + 违规追越）
================================================================

【这个文件做什么？】
  1) `InteractionIndex`：从全船队轨迹一次性构建的「格网 + 时间」索引，
     回答两个问题——「本船的邻近船有哪些」（供碰撞风险）与
     「哪些船对可能互相追越」（供违规追越）。
  2) `detect_overtakings`：按 COLREG 第13条判定违规追越并产出 CandidateEvent。

【为什么独立成模块】
  管线其余部分是「单船单段」的，而碰撞与追越都是**船-船**事件。
  535 船 / 93.7 万点绝不能整批两两相比（O(N²) 会是 5 亿次），
  所以先把"哪两条船在何时何地相邻"用索引算清楚，再只对候选对做精确判定。

【判定口径（关键，别改错）】
  第13条定义的是**局面**：「从他船正横后大于22.5°的某一方向赶上」即构成追越，
  此时追越船负有让路义务。所以：
    - 被追越船船首方位 112.5°~247.5° 扇区内的来船 = 追越船（相对方位要从
      **被追越船的船首**起算，我第一版从船尾起算，扇区正好选成了"正前方"）。
    - 仅有追越局面还不够：第13条(a)的义务是 "keep out of the way"（保持让清），
      所以还必须实测到横距压到安全线以内，才算「未让清」的违规。
      没有这一条就等于把任何一次追越都判成违规，那不是法规口径。
    - 两条船都得在动（≥3节）：低速时 COG 是噪声，算出来的方位没有意义。
"""

from __future__ import annotations  # 启用延迟类型注解评估（PEP 563）

import math  # 三角函数与开方
from bisect import bisect_left  # 时间轴二分查找（取时间最近的点对）
from dataclasses import dataclass  # 数据类型
from datetime import datetime  # 时间类型
from typing import Iterable  # 类型注解

from .config import AcceleratorConfig  # 加速器配置
from .geometry import haversine_nm  # 球面距离（海里）
from .models import AISPoint, CandidateEvent, CandidateType  # 数据模型


_COARSE_DEGREES = 0.10  # 碰撞邻居粗筛格网边长（度，≈6 海里）
_RUN_GAP_SECONDS = 300.0  # 追越区间允许的最大采样间隔（秒）——AIS 上报间隔实测可达 2~3 分钟


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:  # 初始方位角
    """从点1指向点2的初始方位角（度，正北为 0，顺时针）。"""
    phi1 = math.radians(lat1)  # 起点纬度（弧度）
    phi2 = math.radians(lat2)  # 终点纬度（弧度）
    dlambda = math.radians(lon2 - lon1)  # 经度差（弧度）
    y = math.sin(dlambda) * math.cos(phi2)  # 东向分量
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)  # 北向分量
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0  # 归一化到 [0,360)


class _Pt:  # 索引内部持有的轻量点
    """`AISPoint` 是 pydantic 模型，实测 824 字节/个；全船队 93.7 万点就是 737MB。

    而索引**必须常驻全部轨迹点**（船对判定要随机访问任意两条船的任意时段），
    这个量级在应用容器里放不下。轻量点只留判定真正用得到的 6 个字段，
    实测约 120 字节/个（≈112MB，与既有的航道走廊 `_lon_lats` 同量级）。

    只在下游真正需要时才 `to_ais_point()` 还原——追越事件的证据点总共才几千个，
    碰撞邻居每船也只下发最近 20 条，两者都不构成内存压力。
    """

    __slots__ = ("mmsi", "timestamp", "lat", "lon", "speed", "course")  # 字段固定，省 __dict__

    def __init__(  # 初始化
        self,
        mmsi: str,  # MMSI
        timestamp: datetime,  # 时间戳
        lat: float,  # 纬度
        lon: float,  # 经度
        speed: float,  # 航速（节）
        course: float,  # 航向（度）
    ) -> None:
        self.mmsi = mmsi  # MMSI
        self.timestamp = timestamp  # 时间戳
        self.lat = lat  # 纬度
        self.lon = lon  # 经度
        self.speed = speed  # 航速
        self.course = course  # 航向

    def to_ais_point(self) -> AISPoint:  # 还原为 AISPoint（供事件证据点等下游使用）
        return AISPoint(  # 构建 pydantic 点
            mmsi=self.mmsi,  # MMSI
            timestamp=self.timestamp,  # 时间戳
            lat=self.lat,  # 纬度
            lon=self.lon,  # 经度
            speed=self.speed,  # 航速
            course=self.course,  # 航向
        )


def _as_pt(mmsi: str, p: object) -> _Pt:  # 把任意来源的点统一成轻量点
    if isinstance(p, _Pt):  # 已经是轻量点（重复建索引时）
        return p  # 直接用
    return _Pt(  # 从 AISPoint（或任何字段同名的对象）取字段
        mmsi=getattr(p, "mmsi", mmsi),  # MMSI 优先取点自带的
        timestamp=p.timestamp,  # 时间戳
        lat=p.lat,  # 纬度
        lon=p.lon,  # 经度
        speed=p.speed,  # 航速
        course=p.course,  # 航向
    )


def light_points(mmsi: str, points: Iterable[AISPoint]) -> list[_Pt]:  # 把一条船的轨迹转成轻量点
    """供调用方在**不常驻全量 AISPoint** 的前提下构建索引与场。

    调用方要的是一条船一条船地转（`iter_ship_tracks()` 本身也是逐船产出），
    转完就丢掉原始 AISPoint，全船队只剩 112MB 的轻量点。
    """
    return [_as_pt(mmsi, p) for p in points]  # 逐个转换


@dataclass
class OvertakingEncounter:  # 一次追越态势（追越船视角）
    """一次持续成立、且横距压到安全线以内的追越局面。"""

    own_mmsi: str  # 追越船 MMSI
    target_mmsi: str  # 被追越船 MMSI
    points: list[AISPoint]  # 追越船在该区间内的轨迹点（作为证据）
    start_time: datetime  # 区间起点
    end_time: datetime  # 区间终点
    avg_speed_diff_knots: float  # 区间平均速度差（节）
    max_speed_diff_knots: float  # 区间峰值速度差（节）
    min_distance_nm: float  # 区间内两船最小横距（海里）
    avg_relative_bearing: float  # 区间平均相对方位（被追越船船首为 0°）


class InteractionIndex:  # 全船队时空索引
    """格网 + 时间索引：支撑"邻近船查询"与"追越候选对枚举"。"""

    def __init__(self, tracks: dict[str, list[_Pt]], config: AcceleratorConfig) -> None:  # 初始化
        self.cell_degrees = config.interaction_cell_degrees  # 细格网边长
        self.bucket_seconds = config.interaction_bucket_minutes * 60.0  # 时间桶宽（秒）
        self._tracks = tracks  # 全部轨迹（持有引用，不复制点）
        # 细索引必须带**时间桶**：只按格网分桶的话，同一格网里先后(隔几天)经过的
        # 船会被当成相互邻近，候选对数量爆炸（实测 535 船跑出 596 秒）。
        self._bucket_ships: dict[tuple[int, int, int], set[str]] = {}  # (格y, 格x, 时间桶) → 船
        self._coarse_ships: dict[tuple[int, int], set[str]] = {}  # 粗格网 → 占据该格的船（碰撞邻居粗筛）
        self._span: dict[str, tuple[float, float]] = {}  # 船 → (起, 止) epoch 秒
        self._center: dict[str, tuple[float, float]] = {}  # 船 → 轨迹中心点（邻居排序用）
        self._index()  # 立即建立格网倒排与时间跨度

    @classmethod
    def build_from_tracks(  # 从全船队轨迹构建索引
        cls,
        tracks: Iterable[tuple[object, list[AISPoint]]],  # (船舶或其MMSI, 轨迹点) 可迭代对象
        config: AcceleratorConfig,  # 加速器配置
    ) -> "InteractionIndex":
        """一次遍历建立索引；轨迹点按时间排序后持有引用。

        `tracks` 的元素首项可以是 `ShipProfile`（`store.iter_ship_tracks()` 的产物）
        或直接是 MMSI 字符串，两种都接受——调用方不必先拆包。
        """
        ordered: dict[str, list[_Pt]] = {}  # 按时间排好序的轻量轨迹
        for ship, points in tracks:  # 逐船遍历
            if not points:  # 空轨迹
                continue  # 跳过
            mmsi = getattr(ship, "mmsi", ship)  # 兼容 ShipProfile 与裸 MMSI
            # 排序是必须的：下面的时间窗二分查找与区间连续性都依赖时间有序，
            # 而 SQLite 的返回顺序不保证有序。
            # 同时转成轻量点 `_Pt`：AISPoint 是 pydantic 模型，93.7 万点常驻要
            # 737MB，而索引必须常驻全部轨迹（船对判定要随机访问），容器放不下。
            ordered[mmsi] = sorted(  # 按时间升序
                (_as_pt(mmsi, p) for p in points), key=lambda q: q.timestamp  # 转轻量点后排序
            )
        return cls(ordered, config)  # 构建索引

    def _index(self) -> None:  # 建立格网倒排与时间跨度
        cell = self.cell_degrees  # 细格网边长
        coarse = _COARSE_DEGREES  # 粗格网边长
        for mmsi, points in self._tracks.items():  # 逐船遍历
            lat_sum = lon_sum = 0.0  # 中心点累加器
            for p in points:  # 逐点遍历
                key = (  # 细格网 + 时间桶键
                    int(math.floor(p.lat / cell)),  # 纬度格号
                    int(math.floor(p.lon / cell)),  # 经度格号
                    int(p.timestamp.timestamp() // self.bucket_seconds),  # 时间桶号
                )
                bucket = self._bucket_ships.get(key)  # 取该格该时段的船集合
                if bucket is None:  # 首次遇到
                    bucket = self._bucket_ships[key] = set()  # 新建集合
                bucket.add(mmsi)  # 记录该船在此格此时段出现过
                ckey = (int(math.floor(p.lat / coarse)), int(math.floor(p.lon / coarse)))  # 粗格网键
                cbucket = self._coarse_ships.get(ckey)  # 取该粗格的船集合
                if cbucket is None:  # 首次遇到该粗格
                    cbucket = self._coarse_ships[ckey] = set()  # 新建集合
                cbucket.add(mmsi)  # 记录
                lat_sum += p.lat  # 累加纬度
                lon_sum += p.lon  # 累加经度
            first, last = points[0], points[-1]  # 起止点（已按时间排序）
            # 这里用排序后的首尾时间，而不是 min/max，省一次遍历
            self._span[mmsi] = (first.timestamp.timestamp(), last.timestamp.timestamp())  # 时间跨度
            count = len(points)  # 点数
            self._center[mmsi] = (lat_sum / count, lon_sum / count)  # 轨迹中心

    # ── 邻近船查询（供碰撞风险） ──

    def neighbors_of(  # 查询本船的邻近船轨迹
        self,
        points: list[AISPoint],  # 本船轨迹点
        own_mmsi: str,  # 本船 MMSI
        config: AcceleratorConfig,  # 加速器配置
    ) -> dict[str, list[_Pt]]:
        """返回时空上可能与本船相遇的邻近船轨迹 {MMSI: [轻量点]}。

        先用粗格网+时间跨度筛出候选船（O(候选数)），再按轨迹中心距离取最近的
        `collision_max_neighbors` 条——碰撞风险随距离急剧衰减，而 `collision.py`
        对**每一对**都要做两次轨迹预测，不限量就是 O(N²) 的性能悬崖。

        返回的邻居轨迹已被裁剪到本船时间范围（含余量）内，
        既减少预测开销，也不改变 `collision.py` 的函数签名。

        返回的是轻量点 `_Pt`：`collision.py` 只读 lat/lon/speed/course/timestamp
        这五个字段，轻量点全都提供，所以不必还原成 AISPoint 白白多占内存。
        """
        if not points:  # 无点
            return {}  # 无邻居
        pad = config.collision_overlap_pad_minutes * 60.0  # 时间余量（秒）
        own_start = points[0].timestamp.timestamp() - pad  # 本船时间窗起点
        own_end = points[-1].timestamp.timestamp() + pad  # 本船时间窗终点
        span = self._span.get(own_mmsi)  # 索引里的本船跨度（若有）
        if span is not None:  # 用索引跨度更全（points 可能只是分段内的一部分）
            own_start = min(own_start, span[0] - pad)  # 取更早者
            own_end = max(own_end, span[1] + pad)  # 取更晚者

        # 粗筛：本船轨迹覆盖的粗格网 + 周围 2 圈（0.1°≈6海里 → 2 圈覆盖 10 海里搜索半径）
        coarse = _COARSE_DEGREES  # 粗格网边长
        cells = {  # 本船占据的粗格网
            (int(math.floor(p.lat / coarse)), int(math.floor(p.lon / coarse)))  # 格网键
            for p in points  # 遍历本船点
        }
        radius_cells = max(1, int(math.ceil(config.collision_search_radius_nm / (coarse * 60.0))))  # 需要外扩的圈数
        candidates: set[str] = set()  # 候选船集合
        for cy, cx in cells:  # 遍历本船格网
            for dy in range(-radius_cells, radius_cells + 1):  # 纬度方向外扩
                for dx in range(-radius_cells, radius_cells + 1):  # 经度方向外扩
                    found = self._coarse_ships.get((cy + dy, cx + dx))  # 取该粗格的船
                    if found:  # 有船
                        candidates |= found  # 并入候选
        candidates.discard(own_mmsi)  # 排除本船
        if not candidates:  # 无候选
            return {}  # 返回空

        own_center = self._center.get(own_mmsi)  # 本船中心
        if own_center is None:  # 索引里没有（points 未在索引中注册）
            lat_sum = sum(p.lat for p in points) / len(points)  # 现算中心纬度
            lon_sum = sum(p.lon for p in points) / len(points)  # 现算中心经度
            own_center = (lat_sum, lon_sum)  # 中心点
        scored: list[tuple[float, str]] = []  # (距离, MMSI) 列表
        for other in candidates:  # 遍历候选船
            span_other = self._span.get(other)  # 对方时间跨度
            if span_other is None:  # 不在索引里
                continue  # 跳过
            if span_other[1] < own_start or span_other[0] > own_end:  # 时间不重叠
                continue  # 跳过
            center_other = self._center[other]  # 对方中心
            dist = haversine_nm(own_center[0], own_center[1], center_other[0], center_other[1])  # 中心距离
            scored.append((dist, other))  # 记录
        if not scored:  # 无时空重叠
            return {}  # 返回空
        scored.sort()  # 由近及远排序
        limit = config.collision_max_neighbors  # 邻居上限
        nearby: dict[str, list[_Pt]] = {}  # 结果（轻量点，见方法说明）
        for _dist, other in scored[:limit]:  # 取最近的若干条
            track = self._tracks.get(other)  # 对方轨迹
            if not track:  # 缺失
                continue  # 跳过
            window = [  # 裁剪到时间窗内
                p for p in track  # 遍历对方点
                if own_start <= p.timestamp.timestamp() <= own_end  # 落在本船时间窗内
            ]
            if window:  # 有重叠点
                nearby[other] = window  # 收下
        return nearby  # 返回邻近船轨迹

    # ── 追越候选对 ──

    def _candidate_pairs(self, config: AcceleratorConfig) -> list[tuple[str, str]]:  # 枚举追越候选船对
        """返回**时空都邻近**的船对（每对只出现一次，按 MMSI 字符串序）。

        空间上取 3×3 格邻域，时间上取 ±1 个时间桶：追越态势要求持续
        `overtaking_min_minutes` 分钟，所以真正成立的船对必然在某段时间桶里
        同时占据相邻格网，必然被这里枚举到。反之，隔天经过同一格网的船对
        不会进入候选，候选规模因此有界。
        """
        radius_cells = max(  # 细格网外扩圈数
            1,  # 至少 1 圈
            int(math.ceil(config.overtaking_search_radius_nm / (self.cell_degrees * 60.0))),  # 按搜索半径换算
        )
        seen: set[tuple[str, str]] = set()  # 已枚举的船对
        for (cy, cx, bucket), ships in self._bucket_ships.items():  # 遍历每个「格网+时间桶」
            near: set[str] = set(ships)  # 本格邻域内的船
            for dy in range(-radius_cells, radius_cells + 1):  # 纬度方向外扩
                for dx in range(-radius_cells, radius_cells + 1):  # 经度方向外扩
                    for db in (-1, 0, 1):  # 时间桶方向外扩（覆盖桶边界附近的船对）
                        if dy == 0 and dx == 0 and db == 0:  # 本格本桶已收
                            continue  # 跳过
                        found = self._bucket_ships.get((cy + dy, cx + dx, bucket + db))  # 邻格邻桶
                        if found:  # 有船
                            near |= found  # 并入
            if len(near) < 2:  # 不足两条船
                continue  # 无法构成船对
            ordered = sorted(near)  # 排序保证 (a,b) 与 (b,a) 一致
            for i, a in enumerate(ordered):  # 两两组合
                for b in ordered[i + 1:]:  # 只取 i<j
                    pair = (a, b)  # 船对
                    if pair in seen:  # 已枚举过
                        continue  # 跳过
                    seen.add(pair)  # 记录
        return list(seen)  # 返回候选船对


# 见下方 `build_interaction_index`，构造时会立即建立索引
def build_interaction_index(  # 构建并初始化索引
    tracks: Iterable[tuple[str, list[AISPoint]]],  # (mmsi, 轨迹点) 可迭代对象
    config: AcceleratorConfig,  # 加速器配置
) -> InteractionIndex:
    """构建时空索引（等价于 `InteractionIndex.build_from_tracks`，语义更直白）。"""
    return InteractionIndex.build_from_tracks(tracks, config)  # 委托给类方法


def nearest_time_index(times: list[float], t: float) -> int:  # 有序时间轴上最接近 t 的下标
    """返回 `times` 中与 `t` 时间差最小的下标；空列表返回 -1。

    两条船的报文时刻几乎不会重合，所以"同一时刻两船相距多远"只能取**时间最近**
    的那一对来量。原先是取时间窗内**第一条**（最早的那条），在窗口边缘会配到
    差满一个窗口的点对上——两船都以 10 节接近时，60 秒的错配就是 0.33 海里的
    距离误差，而横距安全线只有 0.10 海里（`overtaking_unsafe_distance_nm`，
    约 3 倍于误差）。报文间隔实测可达 2~3 分钟，这个错配是常态不是边角。
    """
    if not times:  # 空列表
        return -1  # 没有可配的点
    k = bisect_left(times, t)  # 第一个不小于 t 的位置
    if k == 0:  # 全在 t 之后
        return 0  # 最早那条就是最近的
    if k >= len(times):  # 全在 t 之前
        return len(times) - 1  # 最晚那条就是最近的
    before, after = times[k - 1], times[k]  # 夹住 t 的两条
    return k - 1 if (t - before) <= (after - t) else k  # 取时间差更小的那个


def _close_approach_rows(  # 逐点配对，取两船靠近且都在动的点对
    px: list[_Pt],  # 船 X 的轨迹（按时间有序，索引内的轻量点）
    py: list[_Pt],  # 船 Y 的轨迹（按时间有序，索引内的轻量点）
    config: AcceleratorConfig,  # 加速器配置
) -> list[tuple[_Pt, _Pt, float, float, float]]:  # 行 = (X点, Y点, 距离, 相对方位, 速度差)
    """返回 X 与 Y 在时间窗内靠近时的点对，**不筛方位**。

    刻意不在这里筛方位：方位筛选用来判定"是不是追越局面"，而横距要量的是
    整个接近过程（含正横通过那一段）。把两件事混在一个谓词里，就会像我第一版
    那样——只在"还没到正横"的时段里量横距，最近的那一刻永远量不到。
    """
    ceiling = config.speed_abs_max_knots  # 速度上限：超过即 AIS 数据异常
    min_speed = config.overtaking_min_speed_knots  # 两船速度下限（低速时 COG 无意义）
    radius = config.overtaking_search_radius_nm  # 构成追越态势的最大距离
    min_dist = config.overtaking_min_distance_nm  # 距离下限（滤重叠/重复点）
    window = config.interaction_bucket_minutes * 60.0  # 时间配对窗（秒）
    y_times = [p.timestamp.timestamp() for p in py]  # 预取 Y 的时间轴，供二分

    rows: list[tuple[_Pt, _Pt, float, float, float]] = []  # 结果行
    for x in px:  # 遍历 X 的点
        if x.speed < min_speed or x.speed > ceiling:  # X 太慢（COG 无意义）或速度是异常值
            continue  # 跳过
        xt = x.timestamp.timestamp()  # X 的时间
        j = nearest_time_index(y_times, xt)  # 时间上最接近的那条 Y 点（见该函数注释）
        if j < 0 or abs(y_times[j] - xt) > window:  # 窗口内没有对方的点
            continue  # 这一时刻配不上对
        y = py[j]  # 与 X 同一时刻的 Y 点
        if y.speed < min_speed or y.speed > ceiling:  # Y 速度不适用（须两船同时在动）
            continue  # 跳过
        dist = haversine_nm(x.lat, x.lon, y.lat, y.lon)  # 两船同时刻距离
        if dist < min_dist or dist > radius:  # 距离不在考察范围
            continue  # 跳过
        bearing = initial_bearing(y.lat, y.lon, x.lat, x.lon)  # Y 看向 X 的方位
        # 相对方位从**被追越船的船首**起算（不是船尾）——从船尾起算会把
        # 扇区整段转到正前方，谓词自相矛盾（我第一版就是这么错的）。
        relative = (bearing - y.course) % 360.0  # 相对 Y 船首的方位
        rows.append((x, y, dist, relative, x.speed - y.speed))  # 记下
    return rows  # 按 X 时间升序返回


def _overtaking_episodes(  # 从一次接近过程中提取"追越局面"并量出横距
    px: list[_Pt],  # 疑似追越船 X 的轨迹
    py: list[_Pt],  # 疑似被追越船 Y 的轨迹
    config: AcceleratorConfig,  # 加速器配置
) -> list[dict]:  # 每个 episode 描述一次追越
    """按 COLREG 第13条判定 X 是否在追越 Y，并给出该次追越的实测横距。

    两步走：
      1) 用扇区谓词（正横后 22.5° 以外）+ 速度优势标出「追越局面」的连续时段；
      2) 横距取**整个接近过程**内的最小值，含正横通过那一段。
         只取局面时段内的最小值是错的：扇区上界就是正横后 22.5°，
         局面在船还没到正横时就结束了，最近的时刻根本没被量到。

    注意调用方必须**双向各调一次**（X 追 Y 与 Y 追 X）：
    速度条件 `x.speed > y.speed + diff` 是反对称的，两次最多只有一次能成立。
    """
    abaft_lo, abaft_hi = config.overtaking_abaft_degrees  # 被追越船船首起算的正横后方扇区
    diff = config.overtaking_speed_diff_knots  # 追越船的速度优势下限
    min_minutes = config.overtaking_min_minutes  # 局面最短持续（分钟）
    min_points = config.overtaking_min_points  # 局面最少点数

    rows = _close_approach_rows(px, py, config)  # 全部接近点对
    if not rows:  # 无接近
        return []  # 无追越

    # 按时间连续性切成若干"接近过程"；同时给单个过程封顶
    # 不封顶的后果实测过：300 秒间隔容差会把几小时内反复擦肩的同一对船
    # 串成一个过程，横距在整个过程上取最小值就失去意义（跑出过 135 分钟、
    # 横距 0.83nm 的"追越"——那是一次远距离跟随，不是一次通过）。
    max_seconds = config.overtaking_max_minutes * 60.0  # 单次接近过程的最长时间（秒）
    windows: list[list[tuple[_Pt, _Pt, float, float, float]]] = []  # 接近过程列表
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

    episodes: list[dict] = []  # 结果
    for window_rows in windows:  # 遍历每个接近过程
        # 步骤 1：标出过程中满足追越局面的行
        situation = [  # 满足扇区谓词与速度优势的行
            row  # 点对行
            for row in window_rows  # 遍历过程内点对
            if abaft_lo <= row[3] <= abaft_hi and row[4] > diff  # 在正横后方扇区 且 有速度优势
        ]
        if len(situation) < min_points:  # 局面点数不足
            continue  # 不算追越
        span = (  # 局面持续时长
            situation[-1][0].timestamp - situation[0][0].timestamp  # 首尾时间差
        ).total_seconds() / 60.0
        if span < min_minutes:  # 局面太短
            continue  # 不算追越

        # 步骤 2：横距取整个接近过程的最小值（含正横通过段）
        closest = min(window_rows, key=lambda row: row[2])  # 最近的那一对点
        min_distance = closest[2]  # 最小横距
        diffs = [row[4] for row in situation]  # 局面内的速度差序列
        bearings = [row[3] for row in situation]  # 局面内的相对方位序列
        # 是否真的**越过去了**：相对方位从正横后方一路减到正横之前（<90°）。
        # 只从后方接近、始终没越过，是"跟随"而不是"追越通过"。
        passed = min(row[3] for row in window_rows) < 90.0  # 方位曾进入正横之前
        episodes.append({  # 记录该次追越
            "points": [row[0] for row in situation],  # 追越船在局面内的点（证据）
            "target_mmsi": situation[0][1].mmsi,  # 被追越船
            "start_time": situation[0][0].timestamp,  # 局面起
            "end_time": situation[-1][0].timestamp,  # 局面止
            "duration_minutes": span,  # 局面持续
            "min_distance_nm": min_distance,  # 整段最小横距
            "speed_diff_knots": sum(diffs) / len(diffs),  # 局面平均速度差
            "max_speed_diff_knots": max(diffs),  # 局面峰值速度差
            "avg_relative_bearing": sum(bearings) / len(bearings),  # 局面平均相对方位
            "passed": passed,  # 是否越过正横
        })
    return episodes  # 返回该方向上的所有追越


def detect_overtakings(  # 全船队违规追越检测
    index: InteractionIndex,  # 时空索引
    config: AcceleratorConfig,  # 加速器配置
) -> dict[str, list[CandidateEvent]]:  # 返回 {追越船MMSI: [事件]}
    """检测全船队的违规追越，按追越船归并返回。

    一次算完、按船下发：船循环里就不必再做任何两两比较。
    """
    abaft_lo, abaft_hi = config.overtaking_abaft_degrees  # 扇区（用于特征记录）
    unsafe = config.overtaking_unsafe_distance_nm  # 「未让清」的安全横距
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
        for px, py in ((pa, pb), (pb, pa)):  # 两个角色方向都要试
            # 该方向上的速度优势必须先成立，否则整轮扫描是白做的（反对称条件）
            for episode in _overtaking_episodes(px, py, config):  # 遍历该方向上的追越
                if not episode["passed"]:  # 只是跟在后面，从未越过正横
                    # 跟随不是追越：第13条约束的是"追越通过"时的让路义务，
                    # 全程留在正横后方说明两船还没构成通过，此时判违规是冤枉。
                    continue  # 不算违规
                min_distance = episode["min_distance_nm"]  # 整段最小横距（含正横通过）
                if min_distance > unsafe:  # 全程保持了安全横距
                    continue  # 已履行让路义务，不算违规
                xs = episode["points"]  # 追越船在局面内的点（证据）
                own_mmsi = xs[0].mmsi  # 追越船
                target_mmsi = episode["target_mmsi"]  # 被追越船
                max_diff = episode["max_speed_diff_knots"]  # 峰值速度差
                event = CandidateEvent(  # 构建候选事件
                    event_id=f"OVT-{own_mmsi}-{target_mmsi}-{int(episode['start_time'].timestamp())}",  # 事件ID
                    mmsi=own_mmsi,  # 主体是追越船
                    candidate_type=CandidateType.OVERTAKING_VIOLATION,  # 违规追越
                    start_time=episode["start_time"],  # 起点
                    end_time=episode["end_time"],  # 终点
                    priority="high" if max_diff >= 5.0 else "middle",  # 强行追越优先
                    features={  # 事件特征
                        "target_mmsi": target_mmsi,  # 被追越船
                        "speed_diff_knots": round(episode["speed_diff_knots"], 2),  # 平均速度差
                        "max_speed_diff_knots": round(max_diff, 2),  # 峰值速度差
                        "min_distance_nm": round(min_distance, 3),  # 最小横距
                        "duration_minutes": round(episode["duration_minutes"], 2),  # 持续时长
                        "point_count": len(xs),  # 点数
                        "avg_relative_bearing": round(episode["avg_relative_bearing"], 1),  # 平均相对方位
                        "bow_sector_degrees": [abaft_lo, abaft_hi],  # 判定所用扇区（可追溯）
                        "unsafe_distance_nm": unsafe,  # 未让清的安全线
                        "overtaken": True,  # 已越过正横（此项为违规的必要条件）
                        "colreg_rule": "13",  # 依据条款
                    },
                    evidence_points=[q.to_ais_point() for q in xs],  # 证据点＝追越船本船轨迹（还原成 AISPoint，量很小）
                )
                result.setdefault(own_mmsi, []).append(event)  # 按追越船归并

    return result  # 返回结果
