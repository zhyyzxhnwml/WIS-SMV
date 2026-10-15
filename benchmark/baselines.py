# -*- coding: utf-8 -*-
"""文献基线的本地复现，统一到**船级二分类**这一层。

【为什么是"复现"而不是"引用文献数字"】
  文献给的 LOF 0.513 / DBSCAN 0.598 / iForest 0.650 / VAE 0.745 / LSTM-AE
  0.752 / VAE-LSTM 0.872 / Transformer-LSTM 0.953 是在**它们各自的数据集、
  各自的任务定义**下测出来的：多数是"这条轨迹点异常"，有的是测某片海域的
  轨迹簇，正负样本比例、特征口径、评估协议都不一样。直接把那些数字抄进对比表，
  审稿人一问"你们的 F1 和他们的 F1 是同一个东西吗"就崩。
  所以这里在同一份注入集、同一个判据（该船是否被注入违规）下重跑一遍。

【任务定义（必须与 eval_inject.py 严格一致）】
  输入：一条船的一个 20 分钟窗口的轨迹（合成船整条就是一个窗口，见
        inject_violations 的 WINDOW_MINUTES）。
  输出：该窗口是否异常（二分类）。
  真值：合成船 = 是否被注入违规；真船 = 恒为正常。
  指标：precision / recall / F1，与 L1 的船级二分类同口径可直接并排。

【三个口径上的诚实说明】
  1. **训练只用正常样本**。LOF / iForest / 自编码器都是无监督异常检测，
     不上标签——这是文献的用法，也是它们与规则引擎最本质的区别。
  2. **两个阈值都报**。
       oracle —— 在评测集上取 F1 最大的阈值。这是**上界**（用了真值调阈值），
                 报出来是为了给基线最好的机会，免得被质疑"你把他们调差了"。
       tpr@1% —— 阈值取**留出真船**分数的 99 分位（假阳性率 1%）。
                 这个不碰真值，是能对上部署现实的数字。
     两个都报，读者自己看。
  3. **特征不含绝对位置**。位置类特征在"注入船 = 宿主位置 + 偏移"的构造下
     可能有信息泄漏风险；只用运动学特征更保守。
     另外**刻意不报"报出航速"与"位置差分航速"的差值**——合成船的报出航速是
     设计的（见 inject_violations._texture），那个差值会变成一条生成器指纹，
     基线拿它得分等于在识别生成器，不是在检测异常。

【可续跑】每个方法的结果单独落盘到 npy/json，已算过的直接跳过。

用法：
  python benchmark/baselines.py                # 全跑
  python benchmark/baselines.py --only classical
  python benchmark/baselines.py --only deep --models vae lstm_ae
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from benchmark.reference import colreg_ref as R  # noqa: E402

WINDOW_MIN = 20.0     # 与注入集的窗口时长对齐（中位 19.9 分钟）
MIN_POINTS = 10
SEQ_LEN = 64          # 序列模型的定长重采样步数
CACHE = ROOT / "data/logs/baselines_cache"
SEED = 20261014
# 改了 features() / sequence() / separation() 的任何算法，都要把这个号 +1。
# 它进缓存指纹：不然代码变了、输入文件没变，旧特征会被静默复用（踩过一次：
# turn 差分公式写错 + sep_series 错位，两次都是靠手动删缓存才发现）。
FEATURE_VERSION = 2


# ── 数据：切窗 ────────────────────────────────────────────────────

def split_windows(pts: list[R.P], minutes: float = WINDOW_MIN) -> list[list[R.P]]:
    """按时间连续性切成长度约 minutes 的不重叠窗口。

    必须切窗，否则真实船是"一整天"、合成船是"20 分钟"，两者的 max 转向率、
    直线度这类特征会系统性不同——量出来的差别是时长造成的，不是异常造成的。
    """
    out, cur, span = [], [], minutes * 60.0
    for p in pts:
        if cur and p.t - cur[0].t > span:
            if len(cur) >= MIN_POINTS:
                out.append(cur)
            cur = []
        cur.append(p)
    if len(cur) >= MIN_POINTS:
        out.append(cur)
    return out


def build_time_index(tracks: dict[str, list[R.P]]) -> dict[float, list[tuple[float, float, str]]]:
    """同刻点表：时刻 → [(lat, lon, mmsi)]。用来算"本船离最近的别的船多远"。"""
    idx: dict[float, list[tuple[float, float, str]]] = {}
    for m, ps in tracks.items():
        for p in ps:
            idx.setdefault(p.t, []).append((p.lat, p.lon, m))
    return idx


NOBODY_NM = 20.0  # 同一刻没有别的船时记这个值，代替 NaN（NaN 会顺着特征毒进模型）


def separation(win: list[R.P], tindex: dict[float, list[tuple[float, float, str]]]
               ) -> tuple[np.ndarray, np.ndarray]:
    """逐点最近会遇距离 + 汇总特征。

    **这一项必须给基线**。只给单船运动学特征时，基线的 F1 退化成"全判异常"
    （oracle 恰好在阈值最低处，F1 = 2p/(1+p) 就是全正分类器的天花板）。
    只看一条船自己，0.1 海里的近距会遇和正常航行毫无区别——违规住在**两船的关系**
    里，不住在单船的运动里。把最近距离喂给它们，基线才拿到它该有的最强武器，
    "给不出规则语义"这个差距才是干净的差距，而不是被我偷偷阉割出来的。

    返回 (逐点距离序列, 汇总三元组)。逐点序列给序列模型，汇总给经典方法。
    """
    series = np.full(len(win), NOBODY_NM, dtype=np.float64)
    for i, p in enumerate(win):
        best = NOBODY_NM
        for lat, lon, m in tindex.get(p.t, ()):
            if m == win[0].mmsi:
                continue
            d = R.nm_between(p.lat, p.lon, lat, lon)
            if d < best:
                best = d
        series[i] = best
    near = float((series < 1.0).mean())
    return series, np.array([series.min(), float(np.quantile(series, 0.05)), near])


# ── 特征：经典方法用的向量 ────────────────────────────────────────

def features(win: list[R.P], sep: tuple[float, float, float]) -> np.ndarray:
    """一个 20 分钟窗口 → 行为特征向量（不含绝对位置）。

    刻意不用"报出航速 vs 位置差分航速"的差：那是生成器指纹，见模块 docstring。
    sep 见 separation()：最近会遇距离三件套，是给基线的最强武器。
    """
    sog = np.array([p.sog for p in win], dtype=np.float64)
    cog = np.radians(np.array([p.cog for p in win], dtype=np.float64))
    t = np.array([p.t for p in win], dtype=np.float64)

    sin_c, cos_c = np.sin(cog), np.cos(cog)
    res = math.hypot(float(sin_c.mean()), float(cos_c.mean()))
    cog_circstd = math.degrees(math.sqrt(max(0.0, -2.0 * math.log(max(res, 1e-12)))))

    dt = np.diff(t)
    ok = dt > 0
    dsog = np.abs(np.diff(sog)[ok] / dt[ok]) if ok.any() else np.zeros(1)
    # 航向变化量：必须先 unwrap 再差分。
    #
    # 原写法是 atan2(d_cos, d_sin)，其中 d_sin = sinθ₂−sinθ₁、d_cos = cosθ₂−cosθ₁。
    # 用和差化积展开：
    #   sinθ₂−sinθ₁ =  2cos((θ₁+θ₂)/2)·sin((θ₂−θ₁)/2)
    #   cosθ₂−cosθ₁ = −2sin((θ₁+θ₂)/2)·sin((θ₂−θ₁)/2)
    # 约掉 sin((θ₂−θ₁)/2) 之后 atan2 的结果只剩 −(θ₁+θ₂)/2 —— **与转角大小无关**，
    # 只等于两点的平均航向。所以 turn_mean/turn_max/turnrate_max 这三维原本
    # 编码的是"船朝哪边"，不是"船转了多少"（实测中位数 147°，正是航向本身），
    # 既量错了东西，又变相当了个绝对位置代理。老老实实 unwrap 再差分。
    turn_abs = np.degrees(np.abs(np.diff(np.unwrap(cog))[ok])) if ok.any() \
        else np.zeros(1)
    turn_rate = turn_abs / dt[ok] if ok.any() else np.zeros(1)

    # 位置差分得到的实际位移速度（与报出航速无关，不含生成器指纹）
    lat = np.radians(np.array([p.lat for p in win]))
    lon = np.radians(np.array([p.lon for p in win]))
    dn = R._NM_PER_DEG_LAT * np.diff(np.degrees(lat))
    de = R._NM_PER_DEG_LAT * np.diff(np.degrees(lon)) * np.cos(lat[:-1])
    step = np.hypot(de, dn)
    path_len = float(step.sum())
    net_disp = R.nm_between(win[0].lat, win[0].lon, win[-1].lat, win[-1].lon)
    disp_rate = step[ok] / (dt[ok] / 3600.0) if ok.any() else np.zeros(1)

    return np.array([
        sog.mean(), sog.std(), sog.min(), sog.max(),
        dsog.mean(), dsog.max(),
        cog_circstd, turn_abs.mean(), turn_abs.max(), turn_rate.max(),
        float(abs(float(np.diff(np.unwrap(cog)).sum())) * 180.0 / math.pi),
        path_len, net_disp, net_disp / max(path_len, 1e-6),
        float(len(win)), float(dt.mean()) if ok.any() else 0.0,
        disp_rate.mean(), disp_rate.std(),
        float(sin_c.mean()), float(cos_c.mean()),
        sep[0], sep[1], sep[2],
    ], dtype=np.float64)


FEATURE_NAMES = (
    "sog_mean sog_std sog_min sog_max dsog_mean dsog_max cog_circstd turn_mean "
    "turn_max turnrate_max turn_net path_len net_disp straightness n_points "
    "interval disp_rate_mean disp_rate_std cog_sin cog_cos "
    "min_sep_nm sep_p5_nm frac_within_1nm"
).split()


# ── 特征：序列模型用的定长序列 ────────────────────────────────────

def sequence(win: list[R.P], sep_series: np.ndarray, length: int = SEQ_LEN) -> np.ndarray:
    """一个窗口 → (length, F) 的定长序列，按归一化时间线性重采样。

    航向用 sin/cos 两通道存，插值才不会被 0/360 的接缝破坏。
    通道：sog、sin、cos、|dsog|、|dω|、归一化时间、最近会遇距离。
    最后一通道与 features() 的 min_sep 同源，保证深度方法与经典方法吃到同一份
    信息，两者的差距才归因于模型而不是输入。
    """
    t = np.array([p.t for p in win], dtype=np.float64)
    sog = np.array([p.sog for p in win], dtype=np.float64)
    rad = np.radians(np.array([p.cog for p in win], dtype=np.float64))

    # 时间轴上可能有重复秒（真实数据里确实有），np.interp/np.gradient 都要求
    # 自变量严格递增——重复点会让 u 出现相等项，梯度算出 inf 顺着特征钻进模型。
    keep = np.concatenate([[True], np.diff(t) > 0])
    t, sog, rad = t[keep], sog[keep], rad[keep]
    # sep_series 与原始窗口一一对应，必须跟着同一个 keep 一起筛，
    # 否则去重之后它和 sog 就错位了（前 N 个不等于留下的那 N 个）。
    sep_series = np.asarray(sep_series, dtype=np.float64)[keep]
    if len(t) < 3:  # 去重后不够算梯度，退化成一个常值窗口
        t = np.arange(len(sog), dtype=np.float64) * 30.0

    span = t[-1] - t[0]
    u = (t - t[0]) / span if span > 0 else np.linspace(0.0, 1.0, len(t))
    grid = np.linspace(0.0, 1.0, length)

    dsog = np.gradient(sog, u)
    domega = np.gradient(np.unwrap(rad), u)

    chans = [sog, np.sin(rad), np.cos(rad), dsog, domega, u, sep_series]
    return np.stack([np.interp(grid, u, c) for c in chans], axis=1)


# ── 阈值与指标 ────────────────────────────────────────────────────

def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else float("nan")
    r = tp / (tp + fn) if (tp + fn) else float("nan")
    f = 2 * p * r / (p + r) if (p == p and r == r and p + r > 0) else float("nan")
    return p, r, f


def score_to_metrics(scores: np.ndarray, labels: np.ndarray,
                     real_scores: np.ndarray,
                     hold_scores: np.ndarray | None = None) -> dict:
    """两个口径一起算：oracle 阈值（F1 最大）与 FPR=1% 阈值。再加一个 AUC。

    labels：True = 注入的违规船。分数越大越异常（统一方向，见各方法的调用处）。
    real_scores：定档用的真船窗分数（全部真船窗，见 run_classical 的注释）。
    hold_scores：船级留出的真船窗分数，只用来**报**实测 FPR，不参与定档。

    AUC 必须一起报：oracle F1 会退化成"全判异常"（阈值压到最低，F1 = 2p/(1+p)
    就是全正分类器的天花板），只看 F1 数字会以为"有 0.67 呢还不错"，
    实际上那个 0.67 里一点排序能力都没有。AUC≈0.5 才是真话。
    """
    out: dict[str, float] = {}
    try:
        from sklearn.metrics import roc_auc_score
        out["auc"] = float(roc_auc_score(labels, scores))
    except (ValueError, ImportError):
        out["auc"] = float("nan")
    # oracle：扫遍所有候选切点取 F1 最大
    uniq = np.unique(scores)
    best = (-1.0, float("nan"))
    for thr in uniq:
        pred = scores >= thr
        tp = int(np.sum(pred & labels))
        fp = int(np.sum(pred & ~labels))
        fn = int(np.sum(~pred & labels))
        _p, _r, f = prf(tp, fp, fn)
        if f == f and f > best[0]:
            best = (f, float(thr))
    thr = best[1]
    pred = scores >= thr
    tp = int(np.sum(pred & labels))
    fp = int(np.sum(pred & ~labels))
    fn = int(np.sum(~pred & labels))
    p, r, f = prf(tp, fp, fn)
    out.update({"oracle_threshold": thr, "tp": tp, "fp": fp, "fn": fn,
                "precision": p, "recall": r, "f1": f})

    # FPR=1%：阈值只由真船分数分布定，不碰真值
    if real_scores.size:
        thr2 = float(np.quantile(real_scores, 0.99))
        pred2 = scores >= thr2
        tp2 = int(np.sum(pred2 & labels))
        fp2 = int(np.sum(pred2 & ~labels))
        fn2 = int(np.sum(~pred2 & labels))
        p2, r2, f2 = prf(tp2, fp2, fn2)
        out.update({"fpr1_threshold": thr2, "fpr1_tp": tp2, "fpr1_fp": fp2,
                    "fpr1_fn": fn2, "fpr1_precision": p2, "fpr1_recall": r2,
                    "fpr1_f1": f2})
        # 实测 FPR：定档集上应当≈1%；船级留出集上才是真实泛化 FPR。
        out["calib_fpr"] = float(np.mean(real_scores >= thr2))
        if hold_scores is not None and hold_scores.size:
            out["holdout_fpr"] = float(np.mean(hold_scores >= thr2))
    # 把评测集上的逐窗分数一并留下。
    # 只报 AUC/oracle-F1 是不够的：要跟"没有阈值、只有部署点"的规则引擎（L1）
    # 比，就必须能回答"在**同样的误报率**下，这条 ROC 曲线上的召回是多少"——
    # 那需要每个基线在评测集上的原始分数，事后从 JSON 里重算，不能只有聚合值。
    out["_scores"] = [float(v) for v in scores]
    out["_labels"] = [bool(v) for v in labels]
    return out


def pct(x: float) -> str:
    return "  n/a" if x != x else f"{x * 100:5.1f}%"


# ── 经典方法 ──────────────────────────────────────────────────────

def run_classical(X_fit: np.ndarray, X_hold: np.ndarray, X_eval: np.ndarray,
                  labels: np.ndarray, only: list[str]) -> dict:
    from sklearn.ensemble import IsolationForest
    from sklearn.neighbors import LocalOutlierFactor, NearestNeighbors
    from sklearn.cluster import DBSCAN
    from sklearn.preprocessing import StandardScaler

    sc = StandardScaler().fit(X_fit)
    Zf, Zh, Ze = sc.transform(X_fit), sc.transform(X_hold), sc.transform(X_eval)
    res: dict[str, dict] = {}

    # FPR 定档：阈值 = **全部真船窗**分数的 99 分位。
    #
    # 为什么不用"留出真船"定档：min_sep 的左尾由极少数港内船/重叠航迹撑着
    # （每船最小 min_sep 的 1 分位 0.047 nm、5 分位 0.051、中位 0.199），
    # 于是"1% 分位"落在哪几乎完全取决于留出里有没有抽到那几条船——
    # 抽到就razor紧（召回掉到 1%），抽不到就松。同一个模型换个种子差两倍，
    # 这种列写进论文是给审稿人递刀。用全部 10383 个真船窗定档，
    # 1% = 第 104 个，稳定。
    #
    # 代价要讲清楚：对**拟合类**方法（lof/iforest/dbscan），拟合集本身在
    # 全部真船窗里占 80%，它们的在样本分数偏低 → 99 分位被压低 →
    # 阈值偏松 → 实测 FPR 高于 1%。这是**对基线有利**的偏置，
    # 所以另有 hold_fpr 一列把真实泛化 FPR 摊开。
    def calib(score_fn):
        s_all = score_fn(np.vstack([X_fit, X_hold]))
        return s_all[:len(X_fit)], s_all[len(X_fit):]

    if "proximity" in only:
        # 单一特征的"最强武器"：最近会遇距离本身当异常分数。
        # 必须单独列出来——不然"多变量方法输给规则引擎"可以被解释成
        # "23 维特征稀释了唯一有用的那一维"，而不是"它们看不见规则语义"。
        # 只用一个特征时没有可稀释的东西，走得怎么样就是这类方法的真实上限。
        j = FEATURE_NAMES.index("min_sep_nm")
        sf, sh = calib(lambda Z: -Z[:, j])
        res["proximity"] = score_to_metrics(-X_eval[:, j], labels, sf, sh)
        res["proximity"]["seconds"] = 0.0

    if "lof" in only:
        t0 = time.time()
        m = LocalOutlierFactor(n_neighbors=20, novelty=True).fit(Zf)
        # 分数越大越异常：LOF 给的是负的离群度
        sf, sh = calib(lambda Z: -m.score_samples(Z))
        res["lof"] = score_to_metrics(-m.score_samples(Ze), labels, sf, sh)
        res["lof"]["seconds"] = round(time.time() - t0, 1)

    if "iforest" in only:
        t0 = time.time()
        m = IsolationForest(n_estimators=300, random_state=SEED, n_jobs=-1).fit(Zf)
        sf, sh = calib(lambda Z: -m.score_samples(Z))
        res["iforest"] = score_to_metrics(-m.score_samples(Ze), labels, sf, sh)
        res["iforest"]["seconds"] = round(time.time() - t0, 1)

    if "dbscan" in only:
        # DBSCAN 没有连续分数，只能给 0/1（噪声点=异常）。为了让它在两个口径下
        # 都可比，把"到最近核心点的距离"当作分数：噪声点本身仍是最异常的。
        #
        # 【eps 不能硬编码】原来写死 eps=0.5、min_samples=10。可在 23 维标准化
        # 空间里，两个标准正态点的典型距离是 sqrt(2·23)≈6.8 —— 0.5 小到任何点
        # 都不可能是核心点，于是 n_clusters=0，**全是噪声**。而下面那句
        # `if core.shape[0] == 0: core = Zf` 会静默把 core 换成全部拟合点，
        # 方法就从 DBSCAN 变成了"到最近真船窗的距离"（1-NN）——
        # 实测在 Hawaii 集上这退化成 AUC 1.000，在注入集上是 0.326，
        # 两个数都不是 DBSCAN 的。论文里挂 DBSCAN 的名报 1-NN 的数，
        # 是审稿人一查就穿的那种错。
        #
        # 现在按 k-距离图的拐点定 eps（k=min_samples），这是标准做法，
        # 文中一句"eps 取 k-距离曲线拐点"即可交代；仍旧退化成 0 簇时
        # 不再静默替换，而是打上 degenerate 标记，宁可不报也不冒名。
        t0 = time.time()
        k = 10
        _nn = NearestNeighbors(n_neighbors=k).fit(Zf)
        _dk = np.sort(_nn.kneighbors(Zf)[0][:, -1])
        _xs = np.linspace(0.0, 1.0, _dk.size)
        _yn = (_dk - _dk.min()) / max(float(np.ptp(_dk)), 1e-9)
        eps = float(_dk[int(np.argmax(_yn - _xs))])
        m = DBSCAN(eps=eps, min_samples=k).fit(Zf)
        n_clusters = int(len(set(m.labels_)) - (1 if -1 in m.labels_ else 0))
        core = Zf[m.labels_ >= 0]
        degenerate = bool(core.shape[0] == 0)
        if degenerate:
            core = Zf

        def _d(Z):
            # 分块算最近核心点距离：(n_eval, n_core, 20) 一次性展开在 n_core
            # 上千时会吃掉几百 MB，留出集一上千行就直接爆内存。
            out = np.empty(Z.shape[0], dtype=np.float64)
            for i in range(0, Z.shape[0], 256):
                blk = Z[i:i + 256]
                out[i:i + 256] = np.linalg.norm(
                    blk[:, None, :] - core[None, :, :], axis=2).min(axis=1)
            return out
        sf, sh = calib(_d)
        res["dbscan"] = score_to_metrics(_d(Ze), labels, sf, sh)
        res["dbscan"]["seconds"] = round(time.time() - t0, 1)
        res["dbscan"]["n_clusters"] = n_clusters
        res["dbscan"]["eps"] = round(eps, 4)
        res["dbscan"]["degenerate"] = degenerate
    return res


# ── 深度方法 ──────────────────────────────────────────────────────

def _torch():
    import torch
    import torch.nn as nn
    return torch, nn


def build_model(name: str, n_feat: int, length: int):
    """四个架构共用一套编码器/解码器骨架，只换瓶颈结构。

    vae / lstm_ae / vae_lstm / transformer_lstm —— 名字对应文献里的四种做法：
      自编码器、LSTM 自编码器、变分自编码器、以及 Transformer+LSTM 的混合。
    异常分数统一取"重构误差"，这是这一族方法的标准口径。
    """
    torch, nn = _torch()
    H, LAT = 64, 16

    class MLP(nn.Module):
        def __init__(self, sizes):
            super().__init__()
            layers = []
            for a, b in zip(sizes, sizes[1:]):
                layers += [nn.Linear(a, b), nn.ReLU()]
            self.net = nn.Sequential(*layers[:-1])  # 最后一层不要 ReLU

        def forward(self, x):
            return self.net(x)

    class VAE(nn.Module):
        def __init__(self):
            super().__init__()
            self.enc = MLP([length * n_feat, H * 2, H])
            self.mu = nn.Linear(H, LAT)
            self.lv = nn.Linear(H, LAT)
            self.dec = MLP([LAT, H, H * 2, length * n_feat])

        def forward(self, x):
            h = self.enc(x.flatten(1))
            mu, lv = self.mu(h), self.lv(h).clamp(-6.0, 6.0)
            z = mu + torch.randn_like(mu) * torch.exp(0.5 * lv)
            return self.dec(z).view(-1, length, n_feat), mu, lv

    class LSTMAE(nn.Module):
        def __init__(self):
            super().__init__()
            self.enc = nn.LSTM(n_feat, H, batch_first=True)
            self.to_z = nn.Linear(H, LAT)
            self.from_z = nn.Linear(LAT, H)
            self.dec = nn.LSTM(H, H, batch_first=True)
            self.out = nn.Linear(H, n_feat)

        def forward(self, x):
            _, (h, _) = self.enc(x)
            z = self.to_z(h[-1])
            rep = self.from_z(z).unsqueeze(1).repeat(1, x.shape[1], 1)
            y, _ = self.dec(rep)
            return self.out(y), z, None

    class VAELSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.enc = nn.LSTM(n_feat, H, batch_first=True)
            self.mu = nn.Linear(H, LAT)
            self.lv = nn.Linear(H, LAT)
            self.from_z = nn.Linear(LAT, H)
            self.dec = nn.LSTM(H, H, batch_first=True)
            self.out = nn.Linear(H, n_feat)

        def forward(self, x):
            _, (h, _) = self.enc(x)
            h = h[-1]
            mu, lv = self.mu(h), self.lv(h).clamp(-6.0, 6.0)
            z = mu + torch.randn_like(mu) * torch.exp(0.5 * lv)
            rep = self.from_z(z).unsqueeze(1).repeat(1, x.shape[1], 1)
            y, _ = self.dec(rep)
            return self.out(y), mu, lv

    class TransLSTM(nn.Module):
        def __init__(self):
            super().__init__()
            self.enc = nn.LSTM(n_feat, H, batch_first=True)
            layer = nn.TransformerEncoderLayer(H, 4, H * 2, batch_first=True,
                                               dropout=0.0)
            self.tf = nn.TransformerEncoder(layer, 2)
            self.to_z = nn.Linear(H, LAT)
            self.from_z = nn.Linear(LAT, H)
            self.dec = nn.LSTM(H, H, batch_first=True)
            self.out = nn.Linear(H, n_feat)

        def forward(self, x):
            h, _ = self.enc(x)
            h = self.tf(h)
            z = self.to_z(h.mean(dim=1))
            rep = self.from_z(z).unsqueeze(1).repeat(1, x.shape[1], 1)
            y, _ = self.dec(rep)
            return self.out(y), z, None

    return {"vae": VAE, "lstm_ae": LSTMAE, "vae_lstm": VAELSTM,
            "transformer_lstm": TransLSTM}[name]()


def run_deep(name: str, S_fit: np.ndarray, S_hold: np.ndarray, S_eval: np.ndarray,
             labels: np.ndarray, epochs: int, cache_dir: Path) -> dict:
    torch, nn = _torch()
    torch.manual_seed(SEED)
    length, n_feat = S_fit.shape[1], S_fit.shape[2]

    # 归一化：按通道用**真船**的均值方差，逐通道标准化
    mu = S_fit.reshape(-1, n_feat).mean(0)
    # 下界别用 1e-6：某通道若在真船里几乎恒定，除以 1e-6 会把数值放大到 1e6，
    # 送进网络就是 inf。1e-3 是"这一维没信息"的量级，安全。
    sd = np.maximum(S_fit.reshape(-1, n_feat).std(0), 1e-3)

    def norm(a):
        return (a - mu) / sd

    tf = torch.tensor(norm(S_fit), dtype=torch.float32)
    th = torch.tensor(norm(S_hold), dtype=torch.float32)
    te = torch.tensor(norm(S_eval), dtype=torch.float32)

    model = build_model(name, n_feat, length)
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    bs = 64
    n = tf.shape[0]
    gen = torch.Generator().manual_seed(SEED)

    t0 = time.time()
    model.train()
    for ep in range(epochs):
        perm = torch.randperm(n, generator=gen)
        total = 0.0
        for i in range(0, n, bs):
            idx = perm[i:i + bs]
            x = tf[idx]
            rec, a, b = model(x)
            loss = nn.functional.mse_loss(rec, x)
            if b is not None:  # 变分瓶颈：加 KL，β 取 1
                loss = loss + 1e-3 * (-0.5 * torch.sum(
                    1 + b - a.pow(2) - b.exp()) / x.shape[0])
            opt.zero_grad()
            loss.backward()
            # 变分项没有 clamp 时 lv 会跑飞、exp 溢出成 nan，整个模型一次爆炸就
            # 全成 nan 且再也回不来（实测 vae 到 epoch 20 loss=nan）。
            # 除了在 forward 里 clamp lv，这里再限一下梯度范数。
            torch.nn.utils.clip_grad_norm_(model.parameters(), 5.0)
            opt.step()
            total += float(loss.detach()) * x.shape[0]
        if not np.isfinite(total):
            raise RuntimeError(f"{name} 训练发散（epoch {ep + 1} loss={total / n}）")
        if (ep + 1) % 20 == 0:
            print(f"    {name} epoch {ep + 1}/{epochs} loss {total / n:.5f}",
                  flush=True)

    model.eval()
    def err(a):
        with torch.no_grad():
            rec, _, _ = model(a)
            return ((rec - a) ** 2).mean(dim=(1, 2)).numpy()

    out = score_to_metrics(err(te), labels, err(tf), err(th))
    out["seconds"] = round(time.time() - t0, 1)
    out["epochs"] = epochs
    torch.save(model.state_dict(), cache_dir / f"{name}.pt")
    return out


# ── 主流程 ────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--real", default="data/input/_fzais1009_converted.csv",
                    help="用来拟合/训练的正常船轨迹（真船）")
    ap.add_argument("--inject", default="data/logs/inject_v1.csv")
    ap.add_argument("--gt", default="data/logs/inject_v1.gt.json")
    ap.add_argument("--only", choices=["all", "classical", "deep"], default="all")
    ap.add_argument("--models", nargs="*",
                    default=["vae", "lstm_ae", "vae_lstm", "transformer_lstm"])
    ap.add_argument("--epochs", type=int, default=120)
    ap.add_argument("--fit-windows", type=int, default=4000,
                    help="真船窗口抽样上限（CPU 上跑得动就行）")
    ap.add_argument("--out", default="data/logs/baselines.json")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    out_path = ROOT / args.out
    result: dict = {}
    if out_path.exists():
        try:
            result = json.loads(out_path.read_text(encoding="utf-8"))
            print(f"续跑：已有 {list(result.get('methods', {}))}\n")
        except json.JSONDecodeError:
            result = {}

    # 特征/序列落盘缓存：重跑不同模型时不必再解析百万行 CSV。
    # 必须带**指纹**：注入集重生成过之后（用户的工作流就是"调好为止"，一定会再
    # 生成），旧缓存里的合成船窗口已经对不上新真值，静默复用会得出错误的指标。
    #
    # FEATURE_VERSION 也必须进指纹，而且要**手动**改。踩过一次：改了 sequence()/
    # features() 的实现（sep_series 对齐、turn 差分公式），输入文件一个字节没动，
    # 指纹照样命中，于是拿旧特征跑出了新代码的指标。改了特征或序列的算法就把
    # 这个号 +1，别指望自动发现。
    Xf_p, Sf_p = CACHE / "X.npy", CACHE / "S.npy"
    fp_now = {
        "feature_version": FEATURE_VERSION,
        "real": f"{(ROOT / args.real).stat().st_size}",
        "inject": f"{(ROOT / args.inject).stat().st_size}",
        "gt": f"{(ROOT / args.gt).stat().st_size}",
    }
    fp_path = CACHE / "fingerprint.json"
    fp_old = json.loads(fp_path.read_text()) if fp_path.exists() else None
    if Xf_p.exists() and Sf_p.exists() and fp_old == fp_now:
        X_all, S_all, is_eval, labels = (np.load(Xf_p), np.load(Sf_p),
                                         np.load(CACHE / "is_eval.npy"),
                                         np.load(CACHE / "labels.npy"))
        print(f"特征缓存命中：{X_all.shape[0]} 个窗口"
              f"（评测 {int(is_eval.sum())}）\n")
    else:
        if fp_old is not None:
            print(f"缓存指纹变了（旧 {fp_old} → 新 {fp_now}），重建特征\n")
        print(f"解析真船轨迹 {args.real} ...", flush=True)
        t0 = time.time()
        real = R.load_tracks(args.real)
        print(f"  {len(real)} 船，{time.time() - t0:.1f}s；切 {WINDOW_MIN:.0f} 分钟窗 ...",
              flush=True)
        wins: list[list[R.P]] = []
        for _m, pts in real.items():
            wins.extend(split_windows(pts))
        print(f"  真船窗口 {len(wins)} 个", flush=True)

        gt = json.loads((ROOT / args.gt).read_text(encoding="utf-8"))
        inj = R.load_tracks(ROOT / args.inject)
        syn_m = [s["own_mmsi"] for s in gt["scenarios"]]
        syn_labels = np.array([s["code"] is not None for s in gt["scenarios"]])
        syn_wins = [inj[m] for m in syn_m if m in inj]
        assert len(syn_wins) == len(syn_m), "有合成船不在注入集里"

        # 最近会遇距离必须在**同一张**同刻点表上算。
        # 注入集的 CSV 只留了合成船 + 被用作宿主的真船（212 条），源数据有 534 条；
        # 各算各的，合成船那边的船更稀，最近距离会被系统性抬高，等于白送它们
        # 一个"我离谁都远"的正常脸。所以统一用"源数据全部真船 + 全部合成船"。
        tindex = build_time_index(real)
        for w in syn_wins:
            for p in w:
                tindex.setdefault(p.t, []).append((p.lat, p.lon, w[0].mmsi))

        all_wins = wins + syn_wins
        t0 = time.time()
        seps = [separation(w, tindex) for w in all_wins]
        X_all = np.stack([features(w, s[1]) for w, s in zip(all_wins, seps)])
        S_all = np.stack([sequence(w, s[0]) for w, s in zip(all_wins, seps)])
        is_eval = np.array([False] * len(wins) + [True] * len(syn_wins))
        labels = np.concatenate([np.zeros(len(wins), dtype=bool), syn_labels])
        print(f"  特征 {X_all.shape} 序列 {S_all.shape}  {time.time() - t0:.1f}s",
              flush=True)
        np.save(Xf_p, X_all); np.save(Sf_p, S_all)
        np.save(CACHE / "is_eval.npy", is_eval); np.save(CACHE / "labels.npy", labels)
        # 每条窗口属于哪条船：切分要按船分组，见下面的注释。
        np.save(CACHE / "win_ship.npy",
                np.array([w[0].mmsi for w in all_wins], dtype=np.int64))
        fp_path.write_text(json.dumps(fp_now), encoding="utf-8")

    # 真船按**船**切分拟合/留出，别让同一船的相邻窗口跨在两边。
    # 同一条船连着的窗口高度相似，若一个在拟合集一个在留出集，留出分数会被
    # 自己"见过"的那条船压低，FPR=1% 的阈值随之收得比它该有的更紧——
    # 这是**对基线不利**的偏置，同样要修掉。所以按 mmsi 分组切，不按窗切。
    win_ship = np.load(CACHE / "win_ship.npy", allow_pickle=False) \
        if (CACHE / "win_ship.npy").exists() else None
    assert win_ship is not None, "缓存里没有 win_ship.npy，请删掉 cache 重建"
    rng = np.random.default_rng(SEED)
    idx_real = np.flatnonzero(~is_eval)
    ships = np.unique(win_ship[idx_real])
    rng.shuffle(ships)
    n_ship = int(len(ships) * 0.8)
    fit_ships = set(ships[:n_ship].tolist())
    fit_i = idx_real[np.array([win_ship[i] in fit_ships for i in idx_real])]
    hold_i = idx_real[np.array([win_ship[i] not in fit_ships for i in idx_real])]
    rng.shuffle(fit_i)
    if args.fit_windows and len(fit_i) > args.fit_windows:
        fit_i = fit_i[:args.fit_windows]
    ev_i = np.flatnonzero(is_eval)
    print(f"拟合真船窗 {len(fit_i)}（{len(fit_ships)} 船）"
          f"  留出 {len(hold_i)}（{len(ships) - len(fit_ships)} 船）"
          f"  评测 {len(ev_i)}\n")

    methods = result.setdefault("methods", {})
    if "meta" not in result:
        result["meta"] = {
            "window_minutes": WINDOW_MIN, "seq_len": SEQ_LEN,
            "feature_names": FEATURE_NAMES,
            "n_fit": len(fit_i), "n_holdout": len(hold_i), "n_eval": len(ev_i),
            "positive_rate": float(labels[ev_i].mean()),
            "real_source": args.real, "inject_source": args.inject,
            "seed": SEED,
            "note": "oracle=评测集上调阈值取F1最大（上界）；fpr1=阈值取留出真船分数99分位",
        }

    if args.only in ("all", "classical"):
        CLASSICAL = ("proximity", "lof", "iforest", "dbscan")
        want = [m for m in CLASSICAL if m not in methods]
        if want:
            print(f"经典方法：{want}", flush=True)
            got = run_classical(X_all[fit_i], X_all[hold_i], X_all[ev_i],
                                labels[ev_i], want)
            methods.update(got)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                encoding="utf-8")
        for m in CLASSICAL:
            if m in methods:
                r = methods[m]
                print(f"  {m:<17} AUC {r['auc']:.3f}   oracle P {pct(r['precision'])} "
                      f"R {pct(r['recall'])} F1 {pct(r['f1'])}   "
                      f"FPR1% P {pct(r['fpr1_precision'])} "
                      f"R {pct(r['fpr1_recall'])} F1 {pct(r['fpr1_f1'])}")
        print(flush=True)

    if args.only in ("all", "deep"):
        for name in args.models:
            if name in methods:
                print(f"  {name:<17} 已有，跳过", flush=True)
                continue
            print(f"深度方法 {name} 训练中（{args.epochs} epoch）...", flush=True)
            methods[name] = run_deep(name, S_all[fit_i], S_all[hold_i],
                                     S_all[ev_i], labels[ev_i], args.epochs, CACHE)
            out_path.write_text(json.dumps(result, ensure_ascii=False, indent=1),
                                encoding="utf-8")
            r = methods[name]
            print(f"  {name:<17} AUC {r['auc']:.3f}   oracle P {pct(r['precision'])} "
                  f"R {pct(r['recall'])} F1 {pct(r['f1'])}   "
                  f"FPR1% P {pct(r['fpr1_precision'])} "
                  f"R {pct(r['fpr1_recall'])} F1 {pct(r['fpr1_f1'])}  "
                  f"({r['seconds']}s)", flush=True)

    # ── 汇总表 ────────────────────────────────────────────────────
    order = ["proximity", "lof", "dbscan", "iforest", "vae", "lstm_ae", "vae_lstm",
             "transformer_lstm"]
    order = [m for m in order if m in methods] + \
            [m for m in methods if m not in order]
    print("\n" + "=" * 88)
    print(f"{'方法':<20}{'AUC':>7}{'oracle P':>10}{'R':>8}{'F1':>8}"
          f"{'FPR1% P':>10}{'R':>8}{'F1':>8}")
    print("-" * 88)
    for m in order:
        r = methods[m]
        print(f"{m:<20}{r['auc']:>7.3f}{pct(r['precision']):>10}"
              f"{pct(r['recall']):>8}{pct(r['f1']):>8}"
              f"{pct(r['fpr1_precision']):>10}"
              f"{pct(r['fpr1_recall']):>8}{pct(r['fpr1_f1']):>8}")
    print("-" * 88)
    print("AUC   = 与阈值无关的排序能力。正例占比 50% 时 oracle F1 的天花板是 66.7%"
          "（全判异常即可拿到）")
    print("        所以 F1 停在 66.7% 不是'还行'，是'一点排序能力都没有'；看 AUC。")
    print("oracle = 在评测集上调阈值取 F1 最大（上界，给基线最好的机会）")
    print("FPR1% = 阈值取**全部真船窗**分数的 99 分位（不碰真值；对拟合类方法偏松，")
    print("        因为定档集含其拟合窗。详见 run_classical 里 calib() 的注释）")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
