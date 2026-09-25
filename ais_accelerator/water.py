"""
ais_accelerator/water.py — 陆地/水域分类（规避陆地上的 AIS 数据）
===============================================================

【这个文件做什么？】
  判定一个 AIS 点是否落在陆地上、一条线段是否横穿陆地。
  用于：
  1. 清洗层：quality.prepare_track 在 clean_points 时剔除远离航道的陆地点
  2. 可视化层：app.all_ship_tracks 给每个点标 onLand / crossesLand，前端断段

【判定方法（两层）】
  - 陆地掩码：离线用 Natural Earth 10m 陆地多边形栅格化成 0.002°(~220m) 位图，
    运行时 O(1) 位测试（LandMask）。
  - 航道走廊：从历史船队轨迹构建密度网格（density ≥ 2 的格子），
    缓冲 6 格 ≈ 1.3km（CORRIDOR_BUFFER）。走廊内的点视为可航水域——
    这保证了长江/内河/近岸船不被误判为"在陆地"。

  点判定 on_land(lon,lat) = 该格在陆地掩码内 且 不在走廊缓冲内。
  线段判定：在线段上采样 0.2/0.4/0.5/0.6/0.8，≥3/5 次落在陆地 → 横穿陆地；
  若直线段长度 ≥ LONG_CROSS_KM(3km)，改用**裸掩码**判定——AIS 缺失/位置跳变
  造成的超长直线可能被锚泊簇膨胀的走廊豁免掉，需按几何事实判横穿。

【为什么用数据驱动走廊？】
  内河（长江、闽江）的水面在陆地多边形内部。单靠陆地多边形会把
  长江上的船全部误判成"在陆地"。走廊 = "船队实际航行过的地方"，
  在走廊附近的点全部视为水域，只有远离航道又落在陆地的点才是坏点。
"""

from __future__ import annotations

import base64  # base64 编解码（掩码位流存储）
import json  # JSON 读写
import zlib  # zlib 压缩（掩码位流压缩存储）
from pathlib import Path  # 跨平台路径
from typing import Iterable, Optional, Sequence  # 类型注解

# ── 常量（与离线构建脚本 build_land_mask.py 保持一致） ──

CELL = 0.002  # 走廊网格 & 陆地掩码分辨率（度，~220m）
CORRIDOR_BUFFER = 6  # 走廊缓冲半径（格，6 格 ≈ 1.3km）
CORRIDOR_MIN_DENSITY = 2  # 走廊格密度阈值（≥2 视为稳定航道）
LONG_CROSS_KM = 3.0  # 长直线横穿判定阈值（km）：超过该长度的直线段，走廊豁免不再适用
MASK_PATH = Path(__file__).resolve().parent.parent / "data" / "geo" / "land_mask_002.json"  # 陆地掩码文件


def _cell_index(coord: float, cell: float = CELL) -> int:
    """坐标 → 网格单元索引（加微小 epsilon 消除浮点地板误差）。"""
    return int(coord / cell + 1e-9)


def cell_of(lon: float, lat: float) -> tuple[int, int]:
    """经纬度 → 走廊网格单元 (cx, cy)。"""
    return (_cell_index(lon), _cell_index(lat))


# ── 陆地掩码 ──

class LandMask:
    """压缩陆地掩码位图：O(1) 判定经纬度所在格是否为陆地（掩码区域外视为水域）。"""

    def __init__(
        self,
        cell: float,
        lon0: float,
        lat0: float,
        origin_cx: int,
        origin_cy: int,
        width: int,
        height: int,
        bits: bytearray,
    ):
        self.cell = cell  # 网格分辨率（度）
        self.lon0 = lon0  # 区域西界
        self.lat0 = lat0  # 区域南界
        self.origin_cx = origin_cx  # 西界对应的绝对单元列号
        self.origin_cy = origin_cy  # 南界对应的绝对单元行号
        self.width = width  # 网格列数
        self.height = height  # 网格行数
        self.bits = bits  # 位图（行序打包的 bytearray）

    @classmethod
    def load(cls, path: Path | str = MASK_PATH) -> "LandMask":
        """从 JSON 加载掩码（zlib+base64 存储）。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        bits = bytearray(zlib.decompress(base64.b64decode(data["z"])))
        return cls(
            cell=data["cell"],
            lon0=data["lon0"],
            lat0=data["lat0"],
            origin_cx=data.get("origin_cx", _cell_index(data["lon0"], data["cell"])),
            origin_cy=data.get("origin_cy", _cell_index(data["lat0"], data["cell"])),
            width=data["width"],
            height=data["height"],
            bits=bits,
        )

    def contains(self, lon: float, lat: float) -> bool:
        """经纬度所在格是否为陆地（区域外 → False，视为水域）。"""
        cx = _cell_index(lon, self.cell) - self.origin_cx
        cy = _cell_index(lat, self.cell) - self.origin_cy
        if not (0 <= cx < self.width and 0 <= cy < self.height):
            return False
        idx = cy * self.width + cx
        return (self.bits[idx >> 3] >> (idx & 7)) & 1


_mask_cache: Optional[LandMask] = None  # 掩码模块级缓存（首次加载后复用）


def load_land_mask() -> LandMask:
    """获取陆地掩码（模块级缓存，只加载一次）。"""
    global _mask_cache
    if _mask_cache is None:
        _mask_cache = LandMask.load()
    return _mask_cache


def clear_land_mask_cache() -> None:
    """清空掩码缓存（数据源更新后调用，测试用）。"""
    global _mask_cache
    _mask_cache = None


# ── 航道走廊 ──

def build_corridor(
    lon_lats: Iterable[tuple[float, float]], min_density: int = CORRIDOR_MIN_DENSITY
) -> set[tuple[int, int]]:
    """从轨迹点序列构建航道走廊（密度 ≥ min_density 的网格单元集合）。"""
    grid: dict[tuple[int, int], int] = {}
    for lon, lat in lon_lats:
        c = cell_of(lon, lat)
        grid[c] = grid.get(c, 0) + 1
    return {c for c, n in grid.items() if n >= min_density}


def dilate_corridor(
    corridor: Iterable[tuple[int, int]], buffer: int = CORRIDOR_BUFFER
) -> set[tuple[int, int]]:
    """走廊缓冲膨胀：返回"走廊及其周边 buffer 格内"的集合（near）。"""
    near: set[tuple[int, int]] = set()
    for cx, cy in corridor:
        for dx in range(-buffer, buffer + 1):
            for dy in range(-buffer, buffer + 1):
                if dx * dx + dy * dy <= buffer * buffer:
                    near.add((cx + dx, cy + dy))
    return near


# ── 判定器（绑定掩码 + 走廊，构建一次复用多次） ──

class WaterChecker:
    """陆地判定器：持有一份陆地掩码 + 航道走廊（含缓冲），提供 O(1) 点判定与线段判定。

    用法:
        checker = WaterChecker(lon_lats=所有历史轨迹点的 (lon,lat))
        checker.is_on_land(lon, lat)          # 单点是否在陆地
        checker.segment_crosses_land(a_lon, a_lat, b_lon, b_lat)  # 线段是否横穿陆地
    """

    def __init__(
        self,
        lon_lats: Optional[Iterable[tuple[float, float]]] = None,
        corridor: Optional[set[tuple[int, int]]] = None,
        mask: Optional[LandMask] = None,
    ):
        self.mask = mask or load_land_mask()
        if corridor is not None:
            self.corridor = set(corridor)
        elif lon_lats is not None:
            self.corridor = build_corridor(lon_lats)
        else:
            self.corridor = set()
        self.near = dilate_corridor(self.corridor)

    def is_on_land(self, lon: float, lat: float) -> bool:
        """单点判定：在陆地掩码内 且 不在走廊缓冲内 → 陆地坏点。"""
        if cell_of(lon, lat) in self.near:
            return False
        return self.mask.contains(lon, lat)

    @staticmethod
    def _segment_km(a_lon: float, a_lat: float, b_lon: float, b_lat: float) -> float:
        """两点直线距离（公里，Haversine）。"""
        import math
        r = 6371.0
        p1, p2 = math.radians(a_lat), math.radians(b_lat)
        dp = math.radians(b_lat - a_lat)
        dl = math.radians(b_lon - a_lon)
        s = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * r * math.asin(min(1.0, math.sqrt(s)))

    def segment_crosses_land(
        self, a_lon: float, a_lat: float, b_lon: float, b_lat: float,
        n_samples: int = 5, n_hits: int = 3,
    ) -> bool:
        """线段判定：在中间点采样，≥ n_hits 个落在陆地 → 横穿陆地。

        默认采样 0.2/0.4/0.5/0.6/0.8 共 5 点，≥3 命中（与原型验证一致）。

        长直线兜底：当线段长度 ≥ LONG_CROSS_KM 时，改用**裸陆地掩码**判定——
        走廊白名单本意是保护内河/近岸的**短线段**（船在掩码误标为陆地的河道里
        逐点航行），不豁免 AIS 缺失/位置跳变造成的**超长直线**跨过真实地形
        （锚泊簇膨胀的走廊会把这些长直线中段豁免掉，导致横穿漏检）。
        """
        hits = 0
        for f in (0.2, 0.4, 0.5, 0.6, 0.8)[:n_samples]:
            if self.is_on_land(
                a_lon + (b_lon - a_lon) * f,
                a_lat + (b_lat - a_lat) * f,
            ):
                hits += 1
                if hits >= n_hits:
                    return True
        if self._segment_km(a_lon, a_lat, b_lon, b_lat) >= LONG_CROSS_KM:
            raw_hits = 0
            for f in (0.2, 0.4, 0.5, 0.6, 0.8)[:n_samples]:
                if self.mask.contains(
                    a_lon + (b_lon - a_lon) * f,
                    a_lat + (b_lat - a_lat) * f,
                ):
                    raw_hits += 1
                    if raw_hits >= n_hits:
                        return True
        return False
