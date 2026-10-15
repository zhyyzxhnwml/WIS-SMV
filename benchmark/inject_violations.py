# -*- coding: utf-8 -*-
"""违规场景注入：在真实轨迹上合成**已知参数**的会遇局面，产出条款级真值。

【为什么必须注入】
  真实 AIS 没有条款级真值。HawaiiCoast_GT 给的是「哪条船、哪个时间窗、哪类
  异常、证据强弱」，没有「谁该让路、实际最近会遇距离多少、是否达到紧迫局面」。
  拿它只能评二分类异常检测，评不了"让路船未让路"这类**规则违反**。
  文献基线（LOF/DBSCAN/iForest/VAE/LSTM-AE 等）全是无监督异常检测器，
  压根没有"违反的是哪条规则"这个输出——所以规则级评测只能靠注入。

【注入为什么用真实轨迹当底料，而不是画直线】
  直线合成出来的是理想匀速运动，检测器在它上面的分离度是虚的：
  真实 AIS 有报告间隔抖动、COG/SOG 量化噪声、真实转向与加减速。
  这里用真实轨迹做底、只**重定位**目标船：既精确控制了会遇几何，
  又保留了真实的运动纹理。两条链路（10 海里让路链 / 1 海里追越链）
  吃的是同一批样本，口径差异不会污染对比。

【构造方法（局部 ENU 平面，单位海里）】
  记 H 为直航船（真实轨迹，一个点都不改），D 为让路船（合成）。
  构造时让两者位置恒满足   p_D(t) = p_H(t) + r(t)，  r(t) = r0 + v_rel·(t-t0)
  于是**相对几何与 H 的真实运动无关**：H 怎么动、是否匀速，r(t) 都精确等于设计值。
  设 r0 = R·û，要求最小会遇距离为 d，则 v_rel 与 -û 的夹角 α = asin(d/R)；
  取接近时长 T，|v_rel| = R·cosα/T，最后由 v_D = v_H + v_rel 定 D 的航向航速。

【为什么必须做负样本】
  只注入违规样本，检出率永远能靠放宽门槛刷上去，precision 是空的。
  同几何、但让路船**已履行义务**的样本才是精度的试金石。
  因此本模块把每个几何按"刚好过线 / 刚好不过线"成对生成，直接测门槛敏感度：
    违规侧：CPA 0.10（明显违规）、0.18（贴着 0.2 门槛）
    合规侧：CPA 0.10 但转 45°、CPA 0.10 但减速 3 节、CPA 0.35（本来就没进紧迫局面）
  另有"转了但不够"（15°，低于 30° 门槛）——仍是违规，专门卡门槛标定。

用法：
  python benchmark/inject_violations.py --scenes 240 --out data/logs/inject_v1
"""
from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import random
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from benchmark.reference import colreg_ref as R  # noqa: E402

# 合成船的 MMSI 段。真船都在 412xxxxxx，999 段不会撞。
SYNTH_MMSI_BASE = 999_000_000

# 注入船速度的合理区间（节）。下界对齐参照的 min_speed_kn，
# 上界留足余量给"让路船高速接近"这一类——但不能到 AIS 异常值区。
D_SOG_MIN = 4.0
D_SOG_MAX = 24.0

# 场景时间窗与选点
WINDOW_MINUTES = 20.0  # 会遇过程时长
HOST_EDGE_MINUTES = 10.0  # 宿主轨迹两端各留这么长，避免贴边截断
HOST_MAX_COG_STD = 12.0  # 宿主在窗内的航向标准差上限（度）——直航船必须真的是直航
HOST_MAX_SOG_STD = 1.5  # 宿主速度标准差上限（节）
ISOLATION_NM = 1.0  # 窗内不允许第三方船进到这个距离，否则真值被污染
CELL_DEG = 0.05  # 隔离检查的索引格子边长（度，约 3 海里），只需粗到能筛出候选船

# 追越链里合成船与被追越船的航向差上限（度）。追越是"从正后方赶上来"，
# 两船航向接近平行；差到 45 度以上就不是追越而是交叉了。
OT_MAX_COG_DIFF = 45.0

# 报出航向航速的**纹理噪声**。
# 报出去的是设计值（理由见 _try_geometry），而设计值是常值——真实船的报出
# 航速却在抖。实测（data/logs/diag_texture.py，20 分钟窗）：
#   真实船 sog_std 中位 0.14 节 / 极差 0.60 节 / |Δsog| 0.004 节每秒
#                          航向圆周标准差 2.9 度
#   合成船（不加噪）三项**恒为 0**，航向也是 0.0 度
# 也就是说"速度纹丝不动"是一条现成的生成器指纹：LOF / iForest 只要看一眼
# sog_std 就能把合成船全挑出来，那种高分是在识别生成器，不是在检测异常，
# 整张与文献基线的对比表都会作废。
# 所以按实测分布给两点加独立噪声，σ 取真实中位数；并**夹紧**在判据门槛之下：
#   减速夹到 ±0.5 节 → 极差 ≤1.0，离"减速 ≥2 节视为已避让"还有一倍余量；
#   转向夹到 ±9 度 → 对 30 度门槛还有 21 度余量，也不会把 15 度的
#   "转向不足"样本抬到 30 度以上。
# 噪声用 rng 生成（种子固定），且**写进内存里的点**——自检、_accepts 的判定
# 都在带噪数据上做，噪声一旦把某条样本的判决掀翻，它当场就被弃掉，标签不受影响。
SOG_NOISE_STD = 0.14
SOG_NOISE_CLIP = 0.5
COG_NOISE_STD = 2.9
COG_NOISE_CLIP = 9.0


def _texture(rng: random.Random, sog: float, cog: float) -> tuple[float, float]:
    """给一个设计航向航速加上真实纹理噪声，并按 CSV 写出精度取整。

    取整到 0.1 是刻意的：CSV 写的是 f"{x:.1f}"，内存里先取整到同一位，
    "内存自检"和"CSV 往返后再评测"就严格一致，不再有舍入差额。
    """
    s = sog + max(-SOG_NOISE_CLIP, min(SOG_NOISE_CLIP, rng.gauss(0.0, SOG_NOISE_STD)))
    c = cog + max(-COG_NOISE_CLIP, min(COG_NOISE_CLIP, rng.gauss(0.0, COG_NOISE_STD)))
    return round(max(0.1, s), 1), round(c % 360.0, 1)


# ── 局部 ENU 平面（海里） ──────────────────────────────────────────

def to_enu(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """经纬 → 以 (lat0, lon0) 为原点的东/北偏移（海里）。"""
    latm = math.radians((lat + lat0) / 2.0)
    return ((lon - lon0) * R._NM_PER_DEG_LAT * math.cos(latm),
            (lat - lat0) * R._NM_PER_DEG_LAT)


def to_geo(x: float, y: float, lat0: float, lon0: float) -> tuple[float, float]:
    """东/北偏移（海里）→ 经纬。与 to_enu 同一近似，往返误差可忽略。"""
    latm = math.radians(lat0)
    return (lat0 + y / R._NM_PER_DEG_LAT,
            lon0 + x / (R._NM_PER_DEG_LAT * math.cos(latm)))


def _unit(cog_deg: float) -> tuple[float, float]:
    """航向 → 单位向量（东, 北）。"""
    r = math.radians(cog_deg)
    return (math.sin(r), math.cos(r))


def _rot_deg(cog_deg: float, delta: float) -> float:
    return (cog_deg + delta) % 360.0


# ── 场景 ────────────────────────────────────────────────────────────

@dataclass
class Scenario:
    kind: str  # gw_fail / gw_comply / ot_fail / ot_comply ...
    code: str | None  # 期望违规码；合规样本为 None
    own_mmsi: str  # 义务船（让路 / 追越）——合成船的 MMSI
    target_mmsi: str  # 直航船 / 被追越船——真实船
    t_start: float
    t_end: float
    params: dict = field(default_factory=dict)
    achieved: dict = field(default_factory=dict)  # 构造后实测的几何，用于自检


# ── 候选宿主 ────────────────────────────────────────────────────────

def _steady_windows(pts: list[R.P], cfg: dict) -> list[tuple[int, int]]:
    """在一条轨迹上找"直航且稳定"的时间窗（返回点下标区间）。

    直航船必须真的是直航：第17条要求直航船保持航向航速，如果宿主自己在
    窗内拐弯，"谁该让路"的前提就不成立，注入出来的真值也是错的。
    """
    lo, hi = cfg["min_speed_kn"], cfg["speed_ceiling_kn"]
    need = int(WINDOW_MINUTES * 60.0 / 30.0)  # 每 30 秒一个点起步的估算
    out: list[tuple[int, int]] = []
    n = len(pts)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and (pts[j + 1].t - pts[j].t) <= cfg["run_gap_sec"]:
            j += 1
        seg = range(i, j + 1)
        # 在段内滑窗
        for a in range(i, j + 1):
            b = a
            while b + 1 <= j and (pts[b + 1].t - pts[a].t) <= WINDOW_MINUTES * 60.0:
                b += 1
            if (pts[b].t - pts[a].t) < WINDOW_MINUTES * 60.0 * 0.8:
                continue
            if b - a + 1 < 20:
                continue
            win = pts[a:b + 1]
            if any(not (lo <= p.sog <= hi) for p in win):
                continue
            if _cog_std(win) > HOST_MAX_COG_STD or _sog_std(win) > HOST_MAX_SOG_STD:
                continue
            out.append((a, b))
            break  # 一段里取一个窗就够，避免同段产出大量高度重叠的场景
        i = j + 1
    return out


def _cog_std(win: list[R.P]) -> float:
    """航向的圆标准差（度）。直接用普通标准差会在 0/360 跨界处炸掉。"""
    xs = sum(math.sin(math.radians(p.cog)) for p in win) / len(win)
    ys = sum(math.cos(math.radians(p.cog)) for p in win) / len(win)
    return math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(1e-9, math.hypot(xs, ys))))))


def _sog_std(win: list[R.P]) -> float:
    m = sum(p.sog for p in win) / len(win)
    return math.sqrt(sum((p.sog - m) ** 2 for p in win) / len(win))


def _isolated(win: list[R.P], other: dict[str, list[R.P]], cfg: dict) -> bool:
    """窗内(含前后各 5 分钟)不允许第三方船进入 ISOLATION_NM。

    不查这一步，注入场景旁边的真实船可能自己构成一次会遇，真值就被污染了
    ——评出来的假阳性里会混进"其实真有情况"，无法归因。

    必须跳过宿主自己：`other` 通常就是全船队，含宿主，宿主与自身距离恒为 0，
    不跳的话这里永远返回 False（初版就是这个 bug，852 个窗一个都过不了）。
    """
    host = win[0].mmsi
    t0, t1 = win[0].t - 300.0, win[-1].t + 300.0
    for mmsi, pts in other.items():
        if mmsi == host:
            continue
        for p in pts:
            if p.t < t0:
                continue
            if p.t > t1:
                break
            for q in win:
                if abs(q.t - p.t) > 120.0:
                    continue
                if R.nm_between(q.lat, q.lon, p.lat, p.lon) < ISOLATION_NM:
                    return False
    return True


# ── 几何求解 ────────────────────────────────────────────────────────

def _solve_relative(rel_bearing: float, R0: float, dcpa: float,
                    close_minutes: float, v_host: tuple[float, float],
                    course_rule: str = "crossing"
                    ) -> tuple[float, float, float] | None:
    """解出让路船的航向航速。返回 (cog, sog, alpha) 或 None（几何不可行）。

    rel_bearing：D 相对 H **船首**的方位（度）。两条链的取值相反：
      交叉链（第15条）：D 是让路船，须落在 H 的左舷 247.5–360 段；
      追越链（第13条）：D 从**正后方**赶上来，落在 112.5–247.5 段。
    R0：初始距离（海里）。dcpa：设计的最小会遇距离。close_minutes：从起点到
      最近会遇点的时长。v_host：H 的速度向量（东, 北），单位节。
    course_rule：态势判据。两条链对航向差的要求是**相反**的，见下方。
    """
    if not (0.0 < dcpa < R0):
        return None
    alpha = math.asin(dcpa / R0)
    # û 是 H 指向 D 的单位向量
    hdg = math.degrees(math.atan2(v_host[0], v_host[1])) % 360.0
    u = _unit(rel_bearing + hdg)
    # v_rel 与 -û 成 α 角；两个解对应 D 从 H 的左前方或右前方穿过。
    # 取让 D 保持在 H 左舷那一侧的解（第15条的交叉局面要求航向确有交叉）。
    #
    # 单位：R0 是海里，t_close 是秒，R0/t_close 得 **海里/秒**，而速度一律用节
    # （海里/小时），所以必须再乘 3600。初版漏了这一步，算出来的相对速度小了
    # 三个量级，两船几乎同速同向，航向差恒为 0，全部场景都过不了交叉判据。
    t_close_h = close_minutes / 60.0
    rel_speed = R0 * math.cos(alpha) / t_close_h
    base = math.degrees(math.atan2(-u[0], -u[1])) % 360.0
    best = None
    for sign in (+1.0, -1.0):
        rd = _rot_deg(base, sign * math.degrees(alpha))
        vrel = (rel_speed * math.sin(math.radians(rd)),
                rel_speed * math.cos(math.radians(rd)))
        vd = (v_host[0] + vrel[0], v_host[1] + vrel[1])
        sog = math.hypot(*vd)
        if not (D_SOG_MIN <= sog <= D_SOG_MAX):
            continue
        cog = math.degrees(math.atan2(vd[0], vd[1])) % 360.0
        # 态势判据必须按链分开，两条链的要求是**相反**的：
        #   交叉链（第15条）：两船航向**确有交叉**（参照的 crossing_min 6°）。
        #   追越链（第13条）：追越就是"从正后方赶上来"，两船航向必须**接近平行**。
        # 初版对两条链都套交叉判据，于是小 dt 的追越几何（α≈6°、航向差恒小于
        # 10°）全判不可行；侥幸抽到偏轴 rel_bearing、凑出大航向差的少数几个又
        # 不是追越——合成船从后方来却从正横前穿不过去（min_rel 卡在 116°/139°），
        # overtaking_codes 的"必须真的越过去"一条不满足。两头都对不上，
        # 追越链的命中率只有 6%。
        diff = R.angle_diff(cog, hdg)
        if course_rule == "overtaking":
            if diff > OT_MAX_COG_DIFF:
                continue
        elif diff < cfg_crossing_min():
            continue
        best = (cog, sog, alpha)
        break
    return best


def cfg_crossing_min() -> float:
    return float(R.CFG["crossing_min_course_diff_deg"]) + 4.0  # 留点余量，别贴着判据


# ── 生成一个场景 ────────────────────────────────────────────────────

def _iter_variants(spec: dict):
    """产出候选设计参数 (dcpa, close_min, bearing, range, alter)。

    没写 variants 的 kind 只有一组（就是顶层那几个字段）。理由见 _SPECS 上方注释。
    """
    vs = spec.get("variants")
    if not vs:
        yield (spec["dcpa"], spec["close_min"], spec["bearing"],
               spec["range"], spec.get("alter"))
        return
    for v in vs:
        yield (v.get("dcpa", spec["dcpa"]),
               v.get("close_min", spec["close_min"]),
               v.get("bearing", spec["bearing"]),
               v.get("range", spec["range"]),
               v.get("alter", spec.get("alter")))


def _accepts(spec: dict, measured: dict) -> bool:
    """这个场景是否符合设计意图。"""
    want = spec["code"]
    if want is not None:
        # 只认**恰好**命中设计的那一条。同一对船有时会同时踩中两条链（近似
        # 平行的追越几何也满足"未让路"），真值就不唯一了，per-code 的
        # precision/recall 会被污染。宁可少要样本，也要标签干净。
        return measured["fired"] == [want]
    if measured["fired"]:
        return False  # 合规样本必须一条违规都不报
    # 追越侧的负样本必须是**真的追越**：合成船得从被追越船正横之前穿过去过。
    # 否则"没报违规"只是因为两船压根没构成追越局面——这条样本什么也没测，
    # 混进精度分母里会把 precision 抬高。
    if spec.get("must_pass_abeam"):
        mr = measured["min_rel_deg"]
        if mr is None or not (mr < 90.0):
            return False
    return True


def build_scenario(host_pts: list[R.P], a: int, b: int, kind: str,
                   mmsi: str, cfg: dict, rng: random.Random,
                   other: dict[str, list[R.P]]) -> tuple[list[R.P], Scenario] | None:
    """在 host_pts[a:b+1] 这个窗上构造一个场景，返回 (合成船点列, 场景)。

    合成船与宿主**共用宿主的时间戳**：两条链路都用 60 秒容差配对，
    共用时间戳能保证每个时刻都配上，不会因采样错位漏掉最近会遇那一刻。

    设计参数只是**靶子**：按 spec['variants'] 逐个构造并用参照实现自检，
    取第一个落在设计意图里的（理由见 _SPECS 上方那段注释）。
    """
    win = host_pts[a:b + 1]
    spec = _SPECS[kind]

    # 隔离性是窗口自身的属性（有没有第三方船），和几何尝试无关，先查一次。
    if not _isolated(win, other, cfg):
        return None

    # 宿主窗内的平均速度向量（节）。宿主已按"直航"筛过，用均值是稳的。
    vh = _mean_velocity(win)
    if math.hypot(*vh) < cfg["min_speed_kn"]:
        return None

    close_q = cfg["giveway_close_quarters_nm"]
    close_floor = spec.get("close_floor_nm", 0.10)
    fallback: tuple[list[R.P], Scenario] | None = None

    for dcpa, close_min, bearing, rng_nm, alter in _iter_variants(spec):
        got = _try_geometry(win, kind, spec, mmsi, cfg, rng, vh,
                            dcpa, close_min, bearing, rng_nm, alter)
        if got is None:
            continue
        pts, sc = got
        if not _accepts(spec, sc.achieved):
            continue
        d = sc.achieved["min_dist_nm"]
        if d is None:
            continue
        sc.achieved["tier"] = "close" if d <= close_q else "loose"
        if not spec.get("prefer_close"):
            return pts, sc
        # 强档负样本 = 真的进了紧迫局面、但动作充分——这才是卡门槛标定的那种。
        # 两头都要卡：
        #   比 close_floor 还近的（实测出过 0.024 海里 / 45 米）叫它"合规通过"
        #     没法自圆其说，那是碰撞边缘，直接弃掉，不进兜底；
        #   比 0.2 远的压根没进紧迫局面，只算弱样本，留着兜底。
        if d < close_floor:
            continue
        if sc.achieved["tier"] == "close":
            return pts, sc
        if fallback is None:
            fallback = (pts, sc)

    return fallback


def _try_geometry(win: list[R.P], kind: str, spec: dict, mmsi: str, cfg: dict,
                  rng: random.Random, vh: tuple[float, float],
                  dcpa: float, close_min: float, bearing: tuple[float, float],
                  rng_nm: tuple[float, float], alter: tuple | None
                  ) -> tuple[list[R.P], Scenario] | None:
    """按一组设计参数构造合成船，并用参照实现自检。

    返回 None 表示这组参数**几何上就构造不出来**（要求的航速超出实船区间、
    或凑不出交叉局面），换一组即可；"构造出来了但不符合设计意图"由调用方用
    _accepts 判。两者必须分开记，否则看不出是几何求解在漏样本还是设计意图
    没被满足。
    """
    base_lat, base_lon = win[0].lat, win[0].lon
    t0 = win[0].t
    hdg = math.degrees(math.atan2(vh[0], vh[1])) % 360.0

    # 找一个可行的几何：相对方位和初始距离都要试，因为 D 的航速有实船上限
    for _ in range(40):
        rel_bearing = rng.uniform(*bearing)
        R0 = rng.uniform(*rng_nm)
        sol = _solve_relative(rel_bearing, R0, dcpa, close_min, vh,
                              spec.get("chain", "crossing"))
        if sol is not None:
            break
    else:
        return None
    cog_d, sog_d, _alpha = sol

    # 起点相对位置：r0 = R0 · û。t0 取窗起点，最近会遇发生在 close_min 之后。
    u = _unit(rel_bearing + hdg)
    r0 = (R0 * u[0], R0 * u[1])

    def _vrel_of(cog: float, sog: float) -> tuple[float, float]:
        """D 以 (cog, sog) 运动时的相对速度**矢量** v_D − v_H，单位节。

        这里必须是矢量差。初版写成「|v_D − v_H| × unit(cog_d)」，把相对速度的
        方向错当成 D 自己的航向，两船于是永远不接近（实测最小距离恒等于初始
        距离 R0），全部场景在自检里被拒。
        """
        return (sog * math.sin(math.radians(cog)) - vh[0],
                sog * math.cos(math.radians(cog)) - vh[1])

    vrel = _vrel_of(cog_d, sog_d)

    # 让路船的动作：违规=什么都不做；合规=在最近会遇点之前转/减速
    # alter = (相对最近会遇点的分钟数, 转向角, 减速量)
    t_alter = t0 + close_min * 60.0 - (alter[0] * 60.0 if alter else 0.0)

    # 转向后 D 的设计航向航速：v_D' = v_H + v_rel'，再取模和方位角。
    if alter:
        v2_design = _vrel_of(cog_d + alter[1], max(1.0, sog_d - alter[2]))
        vd2 = (vh[0] + v2_design[0], vh[1] + v2_design[1])
        sog2 = math.hypot(*vd2)
        cog2 = math.degrees(math.atan2(vd2[0], vd2[1])) % 360.0
    else:
        sog2 = cog2 = None

    # 报出的 SOG/COG 用**设计值**，不用相邻点差分。
    # 合成船的位置是 p_D = p_H + r(t)，宿主的真实速度波动会原样传给 D，于是
    # 差分出来的"减速量"只是宿主的抖动：设计上完全没动作的场景也报出最高
    # 2.00 节的减速，正好压在让路链"减速 ≥2 节即视为已避让"的门槛上。实测同
    # 一个场景在内存里算 1.9999 判违规、过一遍 CSV（航速四舍五入到 0.1 节）
    # 变成 2.00 判已避让——判决取决于舍入。报设计值，标签才和证据一致。
    # 位置仍带真实纹理（宿主是真实轨迹）。设计值是常值，会被基线当成生成器
    # 指纹，所以再叠一层**受控**纹理噪声（见 _texture 与 SOG_NOISE_STD 的注释）。
    pts: list[R.P] = []
    for p in win:
        # 位置积分的时间必须换算成**小时**：vrel 的单位是节（海里/小时），
        # 直接用秒会小 3600 倍，两船于是几乎不接近（实测最小距离恒为初始距离）。
        dt = (p.t - t0) / 3600.0
        if alter and p.t >= t_alter:
            # 转舵时刻定位到 r(t_alter)，其后按新的相对速度推进：
            #   r(t) = r(t_alter) + v_rel' · (t − t_alter)
            # 新相对速度由"改向 alter[1] 度、减速 alter[2] 节"直接算出 D 的新
            # 速度矢量再减 v_H，而不是把旧相对矢量转一下——后者把 D 的转向角和
            # 相对速度的转角混为一谈，减速也不会真正改变方向。
            dt_a = (t_alter - t0) / 3600.0
            r_at = (r0[0] + vrel[0] * dt_a, r0[1] + vrel[1] * dt_a)
            v2 = v2_design
            d2 = (p.t - t_alter) / 3600.0
            r = (r_at[0] + v2[0] * d2, r_at[1] + v2[1] * d2)
            qsog, qcog = _texture(rng, sog2, cog2)
        else:
            r = (r0[0] + vrel[0] * dt, r0[1] + vrel[1] * dt)
            qsog, qcog = _texture(rng, sog_d, cog_d)

        # p_D(t) = p_H(t) + r(t) —— H 的真实位置直接搬过来，几何与 H 的运动无关
        hx, hy = to_enu(p.lat, p.lon, base_lat, base_lon)
        qx, qy = hx + r[0], hy + r[1]
        qlat, qlon = to_geo(qx, qy, base_lat, base_lon)
        pts.append(R.P(mmsi, p.t, qlat, qlon, qsog, qcog))

    sc = Scenario(
        kind=kind, code=spec["code"], own_mmsi=mmsi, target_mmsi=win[0].mmsi,
        t_start=win[0].t, t_end=win[-1].t,
        params={"rel_bearing": round(rel_bearing, 2), "range_nm": round(R0, 3),
                "dcpa_nm": dcpa, "close_min": close_min,
                "alter": alter, "host_cog": round(hdg, 1),
                "host_sog": round(math.hypot(*vh), 2),
                "synth_cog": round(cog_d, 1), "synth_sog": round(sog_d, 2)},
    )
    # 自检：把参照实现跑一遍拿到实测几何，设计值（dcpa）必须对得上。
    # 这一步每个候选都要过；判"是否落在设计意图里"由调用方用 _accepts 做。
    sc.achieved = _measure_pair(pts, win, cfg)
    return pts, sc


def _mean_velocity(win: list[R.P]) -> tuple[float, float]:
    """窗内平均速度向量（东, 北），单位节。"""
    vx = sum(p.sog * math.sin(math.radians(p.cog)) for p in win) / len(win)
    vy = sum(p.sog * math.cos(math.radians(p.cog)) for p in win) / len(win)
    return (vx, vy)


# ── 场景谱 ──────────────────────────────────────────────────────────
# 每个 kind 的几何参数。让路链的违规触发门槛是 0.2 海里，
# 所以 0.10/0.18 是"违规"，0.35 是"根本没进紧迫局面"；
# 转向门槛 30 度、减速门槛 2 节，于是 45 度/3 节是合规、15 度是"转了但不够"。
#
# 【为什么带动作的场景要列 candidates，而不是一组死参数】
#   动作发生在离最近会遇点越远的地方，对会遇距离的放大越厉害：最近会遇点前
#   4 分钟转 15 度，能把设计 0.10 海里的会遇距离推到 0.4 海里——超出 0.2 的
#   紧迫局面门槛，参照实现于是根本不判"紧迫局面"。样本就从"转了但不够"变成
#   "压根没碰上"，真值错了，而且**错得看不出来**（日志里只表现为这一类零样本）。
#   所以动作类场景列一组候选（动作时刻 × 转向方向 × 设计会遇距离），逐个构造
#   并自检，取第一个真正落在设计意图里的。
#   转向方向必须两个都试：转开还是转拢，对会遇距离的影响是反的。

_SPECS: dict[str, dict] = {
    # ── 让路链（第15条交叉相遇，10 海里考察半径，0.2 海里紧迫局面门槛）
    "gw_fail_clean": {
        "code": "give_way_failure", "dcpa": 0.10, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0), "alter": None,
    },
    "gw_fail_marginal": {  # 贴着 0.2 门槛过线，测门槛标定
        "code": "give_way_failure", "dcpa": 0.18, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0), "alter": None,
    },
    "gw_fail_turn_short": {  # 转了 15 度，低于 30 度门槛 → 仍是违规
        "code": "give_way_failure", "dcpa": 0.10, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0),
        "alter": (4.0, 15.0, 0.0),  # 基线（4 分钟前转）已知不可行，见上方注释
        "variants": [
            {"dcpa": d, "alter": (delay, sign * 15.0, 0.0)}
            for d in (0.10, 0.05)
            for delay in (3.0, 2.0, 1.0, 0.5)
            for sign in (1.0, -1.0)
        ],
    },
    "gw_comply_turn": {  # 同几何，但转 45 度 → 不是违规（精度试金石）
        "code": None, "dcpa": 0.10, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0),
        "alter": (5.0, 45.0, 0.0), "prefer_close": True,
        # 优先要"进了紧迫局面、但转够了"的强样本，那才真正卡住门槛标定；
        # 退而求其次才是"转开了、压根没到紧迫局面"的弱样本。
        "variants": [
            {"dcpa": d, "alter": (delay, sign * 45.0, 0.0)}
            for d in (0.10, 0.05)
            for delay in (4.0, 2.0, 1.5, 1.0, 0.5)
            for sign in (1.0, -1.0)
        ],
    },
    "gw_comply_slow": {  # 同几何，但减速 3 节 → 不是违规
        "code": None, "dcpa": 0.10, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0),
        "alter": (5.0, 0.0, 3.0), "prefer_close": True,
        "variants": [
            {"dcpa": d, "alter": (delay, 0.0, 3.0)}
            for d in (0.10, 0.05)
            for delay in (4.0, 2.0, 1.5, 1.0, 0.5)
        ],
    },
    "gw_pass_safe": {  # 全程 0.35 海里，从未进入紧迫局面 → 不是违规
        "code": None, "dcpa": 0.35, "close_min": 12.0,
        "bearing": (255.0, 350.0), "range": (4.0, 7.0), "alter": None,
    },

    # ── 追越链（第13条，1 海里考察半径，0.10 海里横距门槛，2 分钟持续）
    # chain="overtaking" 让航向差判据反过来：追越要求两船航向接近平行，
    # 与交叉链的"航向确有交叉"相反。见 _solve_relative。
    "ot_fail_clean": {
        "code": "overtaking_violation", "dcpa": 0.05, "close_min": 11.0,
        "bearing": (150.0, 210.0), "range": (0.35, 0.75), "alter": None,
        "chain": "overtaking",
    },
    "ot_fail_marginal": {
        "code": "overtaking_violation", "dcpa": 0.09, "close_min": 11.0,
        "bearing": (150.0, 210.0), "range": (0.35, 0.75), "alter": None,
        "chain": "overtaking",
    },
    "ot_comply_wide": {  # 保持 0.30 海里横距通过 → 不是违规
        # 0.35 起的距离配 0.30 的会遇距离，asin 逼近 90 度、相对速度小到 1 节，
        # 会掉出追越的 vdiff > 1.0 判据；起点距离抬到 0.40 起才留得出余量。
        "code": None, "dcpa": 0.30, "close_min": 11.0,
        "bearing": (150.0, 210.0), "range": (0.40, 0.85), "alter": None,
        "chain": "overtaking", "must_pass_abeam": True,
    },
    "ot_comply_turn": {  # 接近后转向拉开 → 不是违规
        # 转向角不能大：25 度以上会把沿航迹分量压到比被追越船还慢，
        # 合成船就不再"越过去"（min_rel 到不了 90 以内），样本退化成非追越局面。
        "code": None, "dcpa": 0.08, "close_min": 11.0,
        "bearing": (150.0, 210.0), "range": (0.35, 0.75),
        "alter": (3.0, 25.0, 0.0), "chain": "overtaking", "must_pass_abeam": True,
        "variants": [
            {"dcpa": d, "alter": (delay, sign * 25.0, 0.0)}
            for d in (0.08, 0.05)
            for delay in (2.0, 1.5, 1.0)
            for sign in (1.0, -1.0)
        ],
    },
}


# ── 主流程 ──────────────────────────────────────────────────────────

def generate(source: str, scenes: int, out_prefix: Path, seed: int = 20261013
             ) -> tuple[Path, Path]:
    cfg = R.CFG
    rng = random.Random(seed)
    tracks = R.load_tracks(source)
    print(f"装载真实轨迹 {len(tracks)} 船", flush=True)

    hosts: list[tuple[str, int, int]] = []
    for mmsi, pts in tracks.items():
        for a, b in _steady_windows(pts, cfg):
            hosts.append((mmsi, a, b))
    print(f"直航候选窗 {len(hosts)} 个", flush=True)

    rng.shuffle(hosts)
    kinds = list(_SPECS)
    # 按谱轮流取，保证各类样本数均衡——否则随机取会让某一类独大，
    # 报出来的指标就只是那一类的指标。
    per_kind = max(1, scenes // len(kinds))

    out_pts: dict[str, list[R.P]] = {m: list(p) for m, p in tracks.items()}
    scenarios: list[Scenario] = []
    made: dict[str, int] = {k: 0 for k in kinds}
    tried: dict[str, int] = {k: 0 for k in kinds}
    blocked: dict[str, int] = {k: 0 for k in kinds}  # 被隔离检查挡掉的候选数
    used_windows: set[tuple[str, int]] = set()
    hi = 0
    ki = 0
    # 隔离索引：真船先入网，合成船每接受一条就补进去，后来的候选就能看见它。
    grid = _build_ship_grid(tracks)

    for mmsi, a, b in hosts:
        if all(made[k] >= per_kind for k in kinds):
            break
        if (mmsi, a) in used_windows:
            continue
        # 轮询指针**每次尝试都前进**，而不是只在成功时前进。
        # 初版写成 kinds[len(scenarios) % len(kinds)]：某一类连试不中时
        # len(scenarios) 不动、指针就卡死，852 个候选窗全砸在同一个 kind 上，
        # 其余 8 类零样本——而日志上分辨不出是"这一类生不出来"还是"其余类
        # 压根没轮到"，归因全错。
        kind = None
        for _ in range(len(kinds)):
            cand = kinds[ki % len(kinds)]
            ki += 1
            if made[cand] < per_kind:
                kind = cand
                break
        if kind is None:
            break
        tried[kind] += 1
        got = build_scenario(tracks[mmsi], a, b, kind,
                             str(SYNTH_MMSI_BASE + hi), cfg, rng, tracks)
        if got is None:
            continue
        pts, sc = got  # build_scenario 内部已用参照实现自检过
        hit_m = _too_close(pts, {sc.own_mmsi, sc.target_mmsi}, grid, out_pts,
                           ISOLATION_NM)
        if hit_m is not None:
            blocked[kind] += 1
            continue
        out_pts[sc.own_mmsi] = pts
        scenarios.append(sc)
        used_windows.add((mmsi, a))
        made[kind] += 1
        hi += 1
        for p in pts:  # 新船入网，后续候选才躲得开它
            grid.setdefault((int(p.lat / CELL_DEG), int(p.lon / CELL_DEG)),
                            set()).add(sc.own_mmsi)
        if len(scenarios) % 20 == 0:
            print(f"  已生成 {len(scenarios)}/{scenes}", flush=True)

    # 落盘
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = out_prefix.with_suffix(".csv")
    keep = {s.target_mmsi for s in scenarios}
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["mmsi", "time", "lon", "lat", "course", "heading", "speed"])
        for mmsi, pts in out_pts.items():
            # 只保留合成船 + 被用作宿主的真实船：其余真船与实验无关，
            # 全写进去只会拖慢 L1 并引入无关的假阳性。
            if mmsi not in keep and not mmsi.startswith("999"):
                continue
            for p in pts:
                w.writerow([mmsi,
                            datetime.fromtimestamp(p.t).strftime("%d/%m/%Y %H:%M:%S"),
                            f"{p.lon:.6f}", f"{p.lat:.6f}",
                            f"{p.cog:.1f}", 511, f"{p.sog:.1f}"])

    gt_path = out_prefix.with_suffix(".gt.json")
    gt_path.write_text(json.dumps({
        "source": source,
        "seed": seed,
        "n_scenarios": len(scenarios),
        "by_kind": made,
        # 漏斗：每个类型试了多少个候选窗口才得到 made[k] 条合格样本。
        # 必须写进真值里——命中率只有四十几，等于大半候选几何在"标签是否干净"
        # 这一关就被弃了，只报 240 条而不报分母会把评测集说得比实际好挑。
        "funnel": {k: {"made": made[k], "tried": tried[k],
                       "blocked_by_isolation": blocked.get(k, 0)} for k in kinds},
        "scenarios": [
            {"kind": s.kind, "code": s.code, "own_mmsi": s.own_mmsi,
             "target_mmsi": s.target_mmsi,
             "t_start": s.t_start, "t_end": s.t_end,
             "params": s.params, "achieved": s.achieved}
            for s in scenarios
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n合成船 {len(scenarios)} 条  宿主 {len(keep)} 条")
    tiers = {"close": 0, "loose": 0}
    for s in scenarios:
        if _SPECS[s.kind]["code"] is None:  # 档位只对负样本有意义
            tiers[s.achieved.get("tier")] = tiers.get(s.achieved.get("tier"), 0) + 1
    for k in kinds:
        n = made[k]
        exp = _SPECS[k]["code"] or "（合规，非违规）"
        hit = f"{n / tried[k]:.0%}" if tried[k] else "-"
        print(f"  {k:<20} {n:4d}/{tried[k]:<4d} 命中{hit:>4}  期望={exp}")
    if any(blocked.values()):
        print(f"  隔离检查挡下 {sum(blocked.values())} 条候选"
              f"（合成船会跟别的船贴进 {ISOLATION_NM} 海里的，详见 _too_close）")
    if any(tiers.values()):
        print(f"  负样本档位：进紧迫局面但动作充分 {tiers.get('close', 0)} 条；"
              f"动作把局面推开 {tiers.get('loose', 0)} 条")

    # 证据余量：动作证据离判据门槛还有多远。
    # 加纹理噪声之后"实测动作 == 设计动作"不再严格成立，审计口径随之改成**余量**：
    # 设计上"没动作"的样本必须仍明显低于门槛，设计上"动作充分"的必须仍明显高于
    # 门槛——只要有一条跨到了对面，它的标签就是错的（_accepts 本应拦下它）。
    # 这个余量同时是这份集子**难度**的直接读数：全贴着门槛 = 测的是标定，
    # 全离得远 = 测的是粗判。
    alter_deg = cfg["giveway_alter_deg"]
    slow_kn = cfg["giveway_speed_drop_kn"]
    gw = [s for s in scenarios
          if _SPECS[s.kind].get("chain", "crossing") == "crossing"
          and s.achieved.get("tier") == "close"]
    margin_bad, no_act_m, act_m = [], [], []
    for s in gw:
        turn = s.achieved.get("turn_deg") or 0.0
        drop = s.achieved.get("drop_kn") or 0.0
        al = s.params.get("alter")
        intended = bool(al and (abs(al[1]) >= alter_deg or al[2] >= slow_kn))
        if intended:
            m = max(turn - alter_deg, drop - slow_kn)
            act_m.append(m)
        else:
            m = min(alter_deg - turn, slow_kn - drop)
            no_act_m.append(m)
            if m <= 0:
                margin_bad.append((s.kind, s.own_mmsi, round(turn, 1), round(drop, 2)))
    print(f"  证据余量（交叉链进紧迫局面的 {len(gw)} 条）："
          f"设计无动作 {len(no_act_m)} 条最小余量 {min(no_act_m, default=float('nan')):.1f}；"
          f"设计动作充分 {len(act_m)} 条最小余量 {min(act_m, default=float('nan')):.1f}")
    if margin_bad:
        print(f"  **跨到对面 {len(margin_bad)} 条（标签不可信，须查 _accepts）**："
              f"{margin_bad[:5]}")

    # 难度：实测值离判据门槛有多远。全贴门槛 = 这份集子测的是标定；全离得远
    # = 测的是粗判。两种都写进日志，免得正文把"100%"说成比实际难。
    def _rng(vals: list[float], nd: int) -> str:
        vs = [v for v in vals if v is not None]
        return f"{min(vs):.{nd}f}~{max(vs):.{nd}f}" if vs else "-"

    print(f"  判据门槛：紧迫局面 {cfg['giveway_close_quarters_nm']} nm  "
          f"转向 {cfg['giveway_alter_deg']}°  减速 {cfg['giveway_speed_drop_kn']} kn  "
          f"不安全距离 {cfg['overtake_unsafe_nm']} nm  速度差 {cfg['overtake_speed_diff_kn']} kn")
    for k in kinds:
        ss = [s for s in scenarios if s.kind == k]
        print(f"    {k:<20} 最近距离 {_rng([s.achieved['min_dist_nm'] for s in ss], 3):<15}"
              f" 转向 {_rng([s.achieved['turn_deg'] for s in ss], 1):<13}"
              f" 减速 {_rng([s.achieved['drop_kn'] for s in ss], 2)}")
    print(f"\n轨迹 → {csv_path}")
    print(f"真值 → {gt_path}")
    return csv_path, gt_path


def _action_evidence(pts: list[R.P], host: list[R.P], cfg: dict
                     ) -> tuple[float, float]:
    """合成船在最近会遇**之前**的转向量与减速量，与参照实现同一算法。

    存在意义是审计：报出的 SOG/COG 一旦改用设计值（见 _try_geometry 的注释），
    这两个数就应当**精确等于**设计动作。若 CSV 往返之后它们对不上设计值，说明
    写盘/读盘的取整动了动作判据，那种样本的标签是不可信的。
    """
    best_turn, best_drop = 0.0, 0.0
    for win in R._split(R._same_time(pts, host, cfg["giveway_radius_nm"], cfg), cfg):
        ci = min(range(len(win)), key=lambda i: win[i].d)
        if win[ci].d > cfg["giveway_close_quarters_nm"]:
            continue
        own_pts = [r.x for r in win[: ci + 1]]
        if len(own_pts) < cfg["min_points"]:
            continue
        first = own_pts[0]
        best_turn = max(best_turn,
                        max((abs(R.signed_turn(first.cog, p.cog)) for p in own_pts),
                            default=0.0))
        best_drop = max(best_drop, first.sog - min(p.sog for p in own_pts))
    return best_turn, best_drop


def _build_ship_grid(tracks: dict[str, list[R.P]]) -> dict[tuple[int, int], set[str]]:
    """把各船**途经的格子**建索引：格子 → 经过这里的船。

    只用来**筛候选**，精确判定在 _too_close 里做，所以格子可以很粗：粗到
    0.05 度（约 3 海里）时，一条船跨过的格子数不过几十，索引本身很小。
    """
    grid: dict[tuple[int, int], set[str]] = {}
    for m, ps in tracks.items():
        for p in ps:
            grid.setdefault((int(p.lat / CELL_DEG), int(p.lon / CELL_DEG)), set()).add(m)
    return grid


def _too_close(pts: list[R.P], exclude: set[str],
               grid: dict[tuple[int, int], set[str]],
               all_tracks: dict[str, list[R.P]],
               min_nm: float, tol_s: float = 60.0) -> str | None:
    """候选合成船有没有跟**别的船**贴进 min_nm（同一时刻）。撞上就返回那条船的 MMSI。

    为什么必须查：合成船是"宿主真实位置 + 设计偏移"造出来的，偏移量最大到 5 海里
    开外——宿主选窗时只保证了 1 海里内没有第三方，管不住 5 海里外的合成船落点。
    于是两条**注入船**可能自己撞在一起：实测 999000093 被判 give_way_failure，
    它的事件目标压根不是设计宿主，而是另一条合成船 999000233。那是构造缺陷，
    不是检测器漏报，混在指标里会把误报算到检测器头上。

    也查真船：合成船落到某条无关真船的航路上，会凭空长出一条"跟陌生船的相遇"，
    同样污染标签（合规样本变误报、违规样本指错目标）。
    """
    for p in pts:
        ci, cj = int(p.lat / CELL_DEG), int(p.lon / CELL_DEG)
        for i in (ci - 1, ci, ci + 1):
            for j in (cj - 1, cj, cj + 1):
                for m in grid.get((i, j), ()):
                    if m in exclude:
                        continue
                    ps = all_tracks.get(m)
                    if not ps:
                        continue
                    # 轨迹按时间有序，二分定位到 ±tol_s 的那一小段即可
                    k = bisect.bisect_left(_T_KEYS.setdefault(m, [q.t for q in ps]), p.t - tol_s)
                    while k < len(ps) and ps[k].t <= p.t + tol_s:
                        if R.nm_between(p.lat, p.lon, ps[k].lat, ps[k].lon) < min_nm:
                            return m
                        k += 1
    return None


# _too_close 里对每条船的时间轴做缓存，省掉重复构造
_T_KEYS: dict[str, list[float]] = {}


def _measure_pair(pts: list[R.P], host: list[R.P], cfg: dict) -> dict:
    """用参照实现回放这个场景，返回实测几何（自检）。

    设计值（dcpa）和实测最小距离必须对得上——不查这一步，生成器的 bug 会以
    "方法检测不到"的形式出现在结果里，归因全错。

    min_rel_deg 是追越侧专用的：从被追越船船首量、整段的最小相对方位。
    它 < 90 才说明合成船真的从正横之前穿过去了，"没被判违规"才是一条有意义
    的负样本，而不是"压根没构成追越局面"。
    """
    rows = R._same_time(pts, host, cfg["giveway_radius_nm"], cfg)
    rows2 = R._same_time(pts, host, cfg["overtake_radius_nm"], cfg)
    min_d = min((r.d for r in rows), default=float("nan"))
    min_d2 = min((r.d for r in rows2), default=float("nan"))

    fired = set()
    for _own, _tgt, c in R.giveway_codes(pts, host, cfg) + R.overtaking_codes(pts, host, cfg):
        fired.add(c)

    min_rel = float("nan")
    max_vdiff = float("nan")
    if rows2:
        # 方位从**被追越船 y 的船首**起算，与 overtaking_codes 同一口径
        min_rel = min((R.bearing_deg(r.y.lat, r.y.lon, r.x.lat, r.x.lon) - r.y.cog) % 360.0
                      for r in rows2)
        # 追越判据里的速度差是**标量差**（x.sog − y.sog），它 ≤ 相对速度矢量的模。
        # 记下实测最大值，被拒时一眼能看出是不是卡在 1 节的这个门槛上。
        max_vdiff = max(r.x.sog - r.y.sog for r in rows2)

    turn, drop = _action_evidence(pts, host, cfg)
    return {"min_dist_nm": _finite(min_d, 4), "min_dist_1nm": _finite(min_d2, 4),
            "min_rel_deg": _finite(min_rel, 1), "max_vdiff_kn": _finite(max_vdiff, 2),
            "turn_deg": _finite(turn, 1), "drop_kn": _finite(drop, 2),
            "fired": sorted(fired), "n_rows_10nm": len(rows)}


def _finite(x: float, nd: int):
    """NaN/Inf 不进 JSON：json.dumps 默认会写出裸 NaN，下游解析不了。"""
    return round(x, nd) if math.isfinite(x) else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/input/_fzais1009_converted.csv")
    ap.add_argument("--scenes", type=int, default=240)
    ap.add_argument("--out", default="data/logs/inject_v1")
    ap.add_argument("--seed", type=int, default=20261013)
    args = ap.parse_args()
    generate(args.source, args.scenes, ROOT / args.out, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
