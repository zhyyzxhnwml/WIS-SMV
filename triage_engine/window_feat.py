# -*- coding: utf-8 -*-
"""生产侧：一条船一天的轨迹 → 23 维窗口特征（逐窗算、按 max 聚合）。

【它填到哪里】
  线上 `triage_model.txt` 是 64 维契约：前 28 维是真特征，`reserved_0..35` 空着。
  这 23 维写进 **`reserved_0..22`**，`reserved_23` 写 `has_w` 标志（1=算出来了，
  0=点太少凑不满窗）。**契约维度、槽位名、router 的入参行数一律不动**，
  所以这是"只换模型文件"级别的替换，不需要改任何调用方的签名。

【为什么这里直接 import benchmark.baselines，而不是抄一遍算式】
  模型是用 `benchmark.baselines.features/separation` 算出的 23 维训出来的
  （`data/logs/dump_winfeat.py` 走的就是这条路）。生产侧重写一份**必然漂移**，
  而训练/推理特征错配比少几个特征致命得多 —— 模型会安静地输出垃圾。
  所以这里只做适配：AISPoint → R.P、按天切窗、逐窗算、max 聚合。
  正确的长期做法是把算式从 `benchmark/` 搬进生产模块、让 benchmark 反向 import；
  在那之前，宁可让生产依赖实验代码，也不要两份实现。
  `data/logs/prod_feat_check.py` / `data/logs/winfeat_check.py` 逐位校验两者一致。

【为什么是 max 聚合】
  `diag_agg69.py` 实测：max 聚合 0.740，加 min/mean 的 69 维只有 0.657 —— 负增量。
  max 已经把"这一天最危险的那个窗"取出来了，再加 min/mean 只是喂噪声。

【实测收益】`data/logs/train_l2_win.py --all`（5543 船-日，真事故标签，留一天）：
  现役 L2 风险分 0.543 → 本模块的 64 维模型 **0.665**（宽区间）
  0.644 → **0.721**（精确锚点）
  其中"改用真标签重训"贡献 +0.091，"加窗口特征"再贡献 +0.031。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:  # 保证 benchmark 可导入（生产容器里工作目录可能不同）
    sys.path.insert(0, str(_ROOT))

from benchmark import baselines as B  # noqa: E402
from benchmark.reference import colreg_ref as R  # noqa: E402

N_WIN_FEAT = 23          # 窗口特征维数（= len(B.FEATURE_NAMES)）
RESERVED_OFFSET = 28     # reserved_0 在 64 维契约里的下标
HAS_W_OFFSET = RESERVED_OFFSET + N_WIN_FEAT   # reserved_23：有没有算出窗口特征

# 具名导出：模型只吃 list（契约不许动），但**日志与 LLM prompt 必须看得懂**。
# `min_sep_nm=0.09` 和 `reserved_3=0.09` 对人的差别就是"能不能判"的差别。
FEATURE_NAMES = tuple(B.FEATURE_NAMES)


def to_rp(p) -> R.P:
    """生产 `AISPoint`（datetime 时间戳）→ 评测 `R.P`（epoch 秒）。"""
    ts = p.timestamp
    t = float(ts.timestamp()) if hasattr(ts, "timestamp") else float(ts)
    return R.P(mmsi=str(p.mmsi), t=t, lat=float(p.lat), lon=float(p.lon),
               sog=float(getattr(p, "speed", 0.0) or 0.0),
               cog=float(getattr(p, "course", 0.0) or 0.0))


def build_tindex(fleet: dict) -> dict[float, list[tuple[float, float, str]]]:
    """{mmsi: [点]} → 同刻点表。**一批只建一次**，然后各船共用。

    分开建是有代价的：`B.build_time_index` 要把全体船的点按时刻分桶，逐船重建
    等于把全船队遍历 N 遍。所以调用方先在批级建好，再把它传进 `features_max`。
    """
    return B.build_time_index(
        {str(m): [p if isinstance(p, R.P) else to_rp(p) for p in ps]
         for m, ps in (fleet or {}).items() if ps}
    )


def features_max(points: Iterable, tindex: Optional[dict] = None) -> Optional[list[float]]:
    """一条船一天的轨迹点 → 23 维（逐 20 分钟窗算，再按 max 聚合）。

    Args:
        points: 该船当天的点（生产 `AISPoint` / `_Pt` 或已转换的 `R.P`）。
        tindex: `build_tindex(当天全船)` 的产物，用于最近会遇距离 —— 违规住在
                两船的关系里，不住在单船的运动里，缺了它这一项会恒定。
                传 None 时该三项退化为 NOBODY_NM，模型仍有 20 维可用。

    Returns:
        23 维 list，或 None（点太少，一个窗都凑不满 → 调用方应置 has_w=0）。
    """
    pts = [p if isinstance(p, R.P) else to_rp(p) for p in points]
    if not pts:
        return None
    pts.sort(key=lambda p: p.t)
    feats = []
    for w in B.split_windows(pts):
        if len(w) < B.MIN_POINTS:
            continue
        feats.append(B.features(w, B.separation(w, tindex or {})[1]))
    if not feats:
        return None
    return [float(v) for v in np.stack(feats).max(0)]


def named(win: Optional[list[float]]) -> Optional[dict[str, float]]:
    """23 维 list → {特征名: 值}；算不出来返回 None。

    用途只有一个：让**下游看得懂**这 23 个数（日志、LLM prompt）。模型入参
    仍然走 `fill_reserved` 的定长 list —— 这里不改契约，只做翻译。
    """
    if win is None or len(win) != N_WIN_FEAT:
        return None
    return {n: float(v) for n, v in zip(FEATURE_NAMES, win)}


def fill_reserved(fv: list[float], win: Optional[list[float]]) -> list[float]:
    """把 23 维窗口特征写进 64 维契约的 `reserved_0..22`，并置 `has_w`。

    入参 fv 可以是 28 维（生产 annotator 的输出）或已是 64 维。返回 64 维新列表。
    """
    out = list(fv)
    if len(out) < 64:
        out = out + [0.0] * (64 - len(out))
    if win is None or len(win) != N_WIN_FEAT:
        out[HAS_W_OFFSET] = 0.0            # 算不出来 → 明确标 0，别留陈值
        return out
    out[RESERVED_OFFSET:RESERVED_OFFSET + N_WIN_FEAT] = [float(v) for v in win]
    out[HAS_W_OFFSET] = 1.0
    return out
