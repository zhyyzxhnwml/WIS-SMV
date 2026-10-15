# -*- coding: utf-8 -*-
"""COLREG 会遇判定的**独立参照实现**（论文基准的参照标准）。

【为什么必须另写一份】
  如果基准标签由被测系统自己的 `violation_catalog.deterministic_violation()`
  产生，那么"系统与真值的一致率"恒等于 100%——测的是它跟自己的关系。
  本模块从**原始 AIS 报文**重新算几何，不 import ais_accelerator 的任何检测器。

【独立性的边界，必须说清（这决定了本模块能证明什么）】
  独立的部分 —— 距离/方位/转角/扇区谓词/让路角色判定，全部按 COLREG 条文与
  标准公式重写；本模块初版就靠它照出了自己的两个错（见下）。
  对齐的部分 —— 采样窗、过程切分、点数下限、半径这些是**本仓的工程约定**，
  不是条文内容。基准若不对齐它们，测出来的差异全部来自脚手架，
  盖过真正要测的规则实现差异（实测：不对齐时两边交集为 0）。
  所以这里**照抄约定、独立实现规则**，并把约定集中列在 CFG 里，逐条可核。

【本模块初版犯过、且已被它自己照出来的两个错（保留记录，勿重犯）】
  1. 用最近会遇点那一对判态势。CPA 处相对方位必然垂直于相对速度，
     教科书式的交叉相遇会被判成无让路义务，让路失败一条都报不出来。
     被测系统的 colreg.py 里有同样一段注释——两边独立踩了同一个坑。
  2. 追越两个分支的速度比较**方向全反**，且把 `PROMOTABLE` 里的升格门槛
     （速度差 5 节）当成了检测门槛（真实检测门槛是 1 节）。
     升格门槛与检测门槛是两回事：前者决定"LLM 判无违规时能否翻案"，
     后者决定"这个局面算不算追越"。

【几何约定】
  一律用 COG（对地航向），不用 heading——本地 fzais 数据的 heading 全为 511
  （AIS 不可用编码），拿它算相对方位得到的全是噪声。

用法：
    from benchmark.reference.colreg_ref import load_tracks, scan_encounters
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# ── 本仓工程约定（照抄自 AcceleratorConfig / encounter.py，非条文内容） ──
CFG = {
    # 会遇判定
    "abaft_lo": 112.5,  # 第13条扇区下界，同时用作正横线
    "abaft_hi": 247.5,  # 第13条扇区上界
    "head_on_deg": 6.0,  # 第14条"正前方"角宽
    "crossing_min_course_diff_deg": 6.0,  # 第15条要求航向确有交叉
    # 让路失败链（colreg.detect_give_way_failures）
    "giveway_radius_nm": 10.0,  # collision_search_radius_nm
    "giveway_close_quarters_nm": 0.2,
    "giveway_alter_deg": 30.0,
    "giveway_speed_drop_kn": 2.0,
    "giveway_duty_types": ("crossing",),  # give_way_duty_encounters
    # 追越链（encounter.detect_overtakings）
    "overtake_radius_nm": 1.0,  # overtaking_search_radius_nm
    "overtake_speed_diff_kn": 1.0,  # ⚠ 检测门槛，不是 PROMOTABLE 里的 5.0
    "overtake_unsafe_nm": 0.10,  # overtaking_unsafe_distance_nm
    "overtake_min_minutes": 2.0,
    # 两链共用（_approach_rows / _close_approach_rows）
    "min_distance_nm": 0.02,
    "min_speed_kn": 3.0,
    "speed_ceiling_kn": 80.0,
    "pair_window_sec": 60.0,  # interaction_bucket_minutes=1.0
    # 过程切分（_split_windows）
    "run_gap_sec": 300.0,  # _RUN_GAP_SECONDS
    "episode_max_minutes": 30.0,  # overtaking_max_minutes
    "min_points": 3,  # overtaking_min_points
}

_NM_PER_DEG_LAT = 60.0


# ── 基础几何 ────────────────────────────────────────────────────────

def nm_between(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点距离（海里）。中纬度用等距圆柱近似，本项目纬度跨度 < 5°，误差可忽略。"""
    latm = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * _NM_PER_DEG_LAT
    dx = (lon2 - lon1) * _NM_PER_DEG_LAT * math.cos(latm)
    return math.hypot(dx, dy)


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """1 看 2 的真方位（度，正北 0，顺时针）。"""
    latm = math.radians((lat1 + lat2) / 2.0)
    dy = (lat2 - lat1) * _NM_PER_DEG_LAT
    dx = (lon2 - lon1) * _NM_PER_DEG_LAT * math.cos(latm)
    return math.degrees(math.atan2(dx, dy)) % 360.0


def angle_diff(a: float, b: float) -> float:
    """两个航向的最小夹角（0–180）。"""
    d = abs(a - b) % 360.0
    return 360.0 - d if d > 180.0 else d


def signed_turn(first: float, second: float) -> float:
    """从 first 转到 second 的带符号角差（正=向右/顺时针）。"""
    return (second - first + 180.0) % 360.0 - 180.0


# ── 轨迹装载 ────────────────────────────────────────────────────────

@dataclass
class P:
    mmsi: str
    t: float
    lat: float
    lon: float
    sog: float
    cog: float


def _parse_t(s: str) -> float:
    s = s.strip()
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {s!r}")


def load_tracks(path: str | Path, max_ships: int | None = None) -> dict[str, list[P]]:
    """读转换后的 AIS CSV → {mmsi: [P, ...]}，按时间排序。

    只保留 SOG/COG 均非 AIS 不可用编码的点：本地数据 heading 全为 511，
    只有 COG 可用；SOG 饱和值 102.3 是"不可用"编码，一并剔除。
    """
    tracks: dict[str, list[P]] = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            mmsi = (row.get("mmsi") or "").strip()
            if not mmsi:
                continue
            try:
                sog = float(row["speed"])
                cog = float(row["course"])
                lat = float(row["lat"])
                lon = float(row["lon"])
                t = _parse_t(row["time"])
            except (KeyError, TypeError, ValueError):
                continue
            if cog >= 360.0 or sog >= 102.3:
                continue
            tracks.setdefault(mmsi, []).append(P(mmsi, t, lat, lon, sog, cog))

    out = {}
    for mmsi, pts in tracks.items():
        pts.sort(key=lambda p: p.t)
        if len(pts) >= 2:
            out[mmsi] = pts
    if max_ships:
        out = dict(list(out.items())[:max_ships])
    return out


# ── 同刻配对 ────────────────────────────────────────────────────────

@dataclass
class Match:
    x: P  # 本方向下的「本船」
    y: P  # 本方向下的「目标」
    d: float


def _same_time(pa: list[P], pb: list[P], radius: float, cfg: dict) -> list[Match]:
    """按 X 的时间轴配对，返回同时刻且两船都在动的点对（不筛方位）。

    不在这里筛方位：方位筛选用来判"是不是追越局面"，而横距要量整个接近过程
    （含正横通过那一段）。混在一个谓词里就会只在"还没到正横"的时段里量横距，
    最近的那一刻永远量不到——被测系统 encounter.py 里记着同一个教训。

    两船速度下限与上限都设：低速时 COG 是噪声，超上限是 AIS 异常值。
    """
    lo, hi = cfg["min_speed_kn"], cfg["speed_ceiling_kn"]
    win, dmin = cfg["pair_window_sec"], cfg["min_distance_nm"]
    yt = [p.t for p in pb]
    out: list[Match] = []
    for x in pa:
        if not (lo <= x.sog <= hi):
            continue
        # 时间最近邻（二分）
        i, j = 0, len(yt) - 1
        while i < j:
            m = (i + j) // 2
            if yt[m] < x.t:
                i = m + 1
            else:
                j = m
        if i > 0 and abs(yt[i - 1] - x.t) < abs(yt[i] - x.t):
            i -= 1
        if abs(yt[i] - x.t) > win:
            continue
        y = pb[i]
        if not (lo <= y.sog <= hi):
            continue
        d = nm_between(x.lat, x.lon, y.lat, y.lon)
        if d < dmin or d > radius:
            continue
        out.append(Match(x, y, d))
    return out


def _split(rows: list, cfg: dict, key=None) -> list[list]:
    """按时间连续性切分，并给单次过程封顶。

    封顶的理由：300 秒间隔容差会把几小时内反复擦肩的同一对船串成一次过程，
    于是"这一段里它有没有转向"变成"这几个小时里它有没有转过向"——
    任何一条在航道里跑过的船都会命中，判据失去意义。
    """
    key = key or (lambda r: r.x.t)
    gap_max, span_max = cfg["run_gap_sec"], cfg["episode_max_minutes"] * 60.0
    windows, cur = [], []
    for r in rows:
        if cur:
            t = key(r)
            if t - key(cur[-1]) > gap_max or t - key(cur[0]) > span_max:
                windows.append(cur)
                cur = []
        cur.append(r)
    if cur:
        windows.append(cur)
    return windows


# ── 判态势（第13/14/15条；顺序不可颠倒） ────────────────────────────

def classify(own: P, target: P, cfg: dict) -> tuple[str, bool]:
    """返回 (态势, 本船是否负让路义务)。

    顺序不可颠倒：追越优先于对遇、对遇优先于交叉。第13条对追越是**兜底**性质的
    （"不论本章各条如何规定"）——反过来排会把"跟在别人正后方并赶上来"判成对遇。

    注意扇区的朝向：`bearing` 是目标相对**本船船首**的方位。目标落在本船正横后
    （112.5–247.5）说明**目标在本船后方**，即目标在追越本船 → 本船是直航船。
    把这一条读反（初版的错）会让两侧的速度比较同时反向。
    """
    bearing = (bearing_deg(own.lat, own.lon, target.lat, target.lon) - own.cog) % 360.0
    recip = (bearing_deg(target.lat, target.lon, own.lat, own.lon) - target.cog) % 360.0
    lo, hi = cfg["abaft_lo"], cfg["abaft_hi"]
    diff = cfg["overtake_speed_diff_kn"]
    head = cfg["head_on_deg"]

    if lo <= bearing <= hi and target.sog > own.sog + diff:
        return "overtaking", False  # 目标在追越本船
    if lo <= recip <= hi and own.sog > target.sog + diff:
        return "overtaking", True  # 本船在追越目标

    own_ahead = bearing <= head or bearing >= 360.0 - head
    tgt_ahead = recip <= head or recip >= 360.0 - head
    if own_ahead and tgt_ahead:
        return "head_on", True  # 第14条：双方各自向右

    if bearing < lo and recip > hi and \
       angle_diff(own.cog, target.cog) >= cfg["crossing_min_course_diff_deg"]:
        return "crossing", True  # 第15条：目标在本船右舷
    return "crossing", False  # 其余：本船为直航船（第17条）


# ── 两条检测链 ──────────────────────────────────────────────────────

def giveway_codes(pa: list[P], pb: list[P], cfg: dict) -> list[tuple[str, str, str]]:
    """让路失败链，照 `colreg.detect_give_way_failures` 的结构。"""
    rows = _same_time(pa, pb, cfg["giveway_radius_nm"], cfg)
    found = []
    for win in _split(rows, cfg):
        # 用下标而不是 win.index(closest)：Match 是 eq=True 的 dataclass，
        # .index() 按值比较，窗口内若有两个完全相同的点对会取到前一个的下标。
        ci = min(range(len(win)), key=lambda i: win[i].d)
        closest = win[ci]
        own_pts = [r.x for r in win[: ci + 1]]
        if len(own_pts) < cfg["min_points"]:
            continue
        if closest.d > cfg["giveway_close_quarters_nm"]:
            continue  # 没真的到紧迫局面，谈不上"未让路"
        # 判态势用**过程起点**那一对，不用最近会遇点（见模块 docstring 的错 1）
        start = win[0]
        kind, own_gives_way = classify(start.x, start.y, cfg)
        if not own_gives_way or kind not in cfg["giveway_duty_types"]:
            continue
        first = own_pts[0]
        turn = max((abs(signed_turn(first.cog, p.cog)) for p in own_pts), default=0.0)
        drop = first.sog - min(p.sog for p in own_pts)
        if turn >= cfg["giveway_alter_deg"] or drop >= cfg["giveway_speed_drop_kn"]:
            continue  # 已履行让路义务
        found.append((start.x.mmsi, start.y.mmsi, "give_way_failure"))
    return found


def overtaking_codes(pa: list[P], pb: list[P], cfg: dict) -> list[tuple[str, str, str]]:
    """违规追越链，照 `encounter.detect_overtakings` 的结构。

    传入方向即"pa 疑似追越 pb"。速度条件 `x.sog > y.sog + diff` 是反对称的，
    两个方向各调一次、最多一次成立，所以调用方必须双向各调。
    """
    rows = _same_time(pa, pb, cfg["overtake_radius_nm"], cfg)
    if not rows:
        return []
    lo, hi = cfg["abaft_lo"], cfg["abaft_hi"]
    diff = cfg["overtake_speed_diff_kn"]
    # 追越行的方位从**被追越船 y 的船首**起算（第13条问的是"从哪一侧驶来"），
    # 不是从 x 的船首——从 x 起算会把扇区整段转到正前方，谓词自相矛盾。
    for r in rows:
        r.rel = (bearing_deg(r.y.lat, r.y.lon, r.x.lat, r.x.lon) - r.y.cog) % 360.0
        r.vdiff = r.x.sog - r.y.sog
    found = []
    for win in _split(rows, cfg):
        situation = [r for r in win if lo <= r.rel <= hi and r.vdiff > diff]
        if len(situation) < cfg["min_points"]:
            continue
        if (situation[-1].x.t - situation[0].x.t) / 60.0 < cfg["overtake_min_minutes"]:
            continue
        min_d = min(r.d for r in win)  # 整段最小横距（含正横通过段）
        if min_d > cfg["overtake_unsafe_nm"]:
            continue  # 全程保持了安全横距
        # 必须真的**越过去了**：相对方位曾进入正横之前。只从后方接近、始终没越过，
        # 是"跟随"而不是"追越通过"——第13条约束的是通过时的让路义务。
        if min(r.rel for r in win) >= 90.0:
            continue
        found.append((situation[0].x.mmsi, situation[0].y.mmsi, "overtaking_violation"))
    return found


# ── 全船队扫描 ──────────────────────────────────────────────────────

CELL_DEG = 0.1  # 空间格边长（度）。0.1° 纬 ≈ 6 海里
CELL_SEC = 60.0  # 时间格边长（秒）
_NEIGH = (-2, -1, 0, 1, 2)  # ±2 格 ≈ ±0.2° ≈ 12 海里，覆盖 10 海里考察半径


def _candidate_pairs(tracks: dict[str, list[P]]) -> set[tuple[str, str]]:
    """按时空格索引找出可能靠近的船对。

    为什么必须建索引：直接两两比对是 O(船数² × 点数)，535 船 ≈ 2.5 亿次
    最近邻查询，跑不完。

    【为什么按格收集**船号**而不是点】
      初版是把邻格的所有**点**倒进一个大列表再两两比。密度一高就平方爆炸：
      一格里有 m 条船、平均 k 个点，那个列表就是 25mk 个点，
      逐点两两比是 (25mk)² 次——535 船实测直接跑不完（150 船只要 19 秒，
      535 船几十分钟不返回）。
      改成先把邻格的船号去重、再在**船**之间组对，复杂度降到每格
      O(s²)（s = 该格船数），与点数无关。
    """
    cells: dict[tuple[int, int, int], set[str]] = {}
    for mmsi, pts in tracks.items():
        for p in pts:
            cells.setdefault(
                (int(p.t // CELL_SEC), int(p.lat // CELL_DEG),
                 int(p.lon // CELL_DEG)), set()).add(mmsi)
    pairs: set[tuple[str, str]] = set()
    for (tb, la, lo), ships in cells.items():
        # 邻格的船 + 本格的船（本格不能漏，同格两船当然可能靠近）
        cand: set[str] = set(ships)
        for dla in _NEIGH:
            for dlo in _NEIGH:
                if dla == 0 and dlo == 0:
                    continue
                cand |= cells.get((tb, la + dla, lo + dlo), set())
        if len(cand) < 2:
            continue
        ordered = sorted(cand)
        for i in range(len(ordered)):
            for j in range(i + 1, len(ordered)):
                pairs.add((ordered[i], ordered[j]))
    return pairs


def scan_encounters(tracks: dict[str, list[P]], cfg: dict | None = None,
                    progress: bool = False) -> list[tuple[str, str, str]]:
    """全船队扫描，返回 [(让路船MMSI, 对方MMSI, 码), ...]。

    每个无序船对按**两个方向**各跑两条链：让路义务在交叉局面下可能落在任一船上，
    追越的速度优势条件则是反对称的、两个方向最多成立一次，都必须各判各的。
    """
    cfg = {**CFG, **(cfg or {})}
    pairs = _candidate_pairs(tracks)
    # 时间跨度预筛：两船时间段不相交就不可能相遇。放在枚举前做，
    # 因为 _same_time 对每一对都要遍历本船全部点做二分，代价不小。
    span = {m: (pts[0].t, pts[-1].t) for m, pts in tracks.items()}
    out: list[tuple[str, str, str]] = []
    for n, (a, b) in enumerate(sorted(pairs)):
        pa, pb = tracks.get(a), tracks.get(b)
        if not pa or not pb:
            continue
        if span[b][1] < span[a][0] or span[a][1] < span[b][0]:
            continue
        if progress and n % 2000 == 0:
            print(f"    船对 {n}/{len(pairs)}", flush=True)
        out.extend(giveway_codes(pa, pb, cfg))
        out.extend(giveway_codes(pb, pa, cfg))
        out.extend(overtaking_codes(pa, pb, cfg))
        out.extend(overtaking_codes(pb, pa, cfg))
    return out
