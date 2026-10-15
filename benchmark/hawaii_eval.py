# -*- coding: utf-8 -*-
"""HawaiiCoast_GT 上的评测：真实公开基准，替代那条自证的循环。

【为什么必须有这一份】
  注入集（inject_v1）能给"条款级真值"，但它的"合规"标签是**参照实现按生产门槛
  判出来的**；L2 的四分类标签更是 AcceleratorPipeline 自家规则标注的确定性函数
  （见 benchmark/train_l2.py 的 ANNOTATION_FEATS 注释）。拿这种真值评级联，
  L1 直接 100%，L2/L3 只会掉分——不是级联没用，是**题目本身是循环的**。

  HawaiiCoast_GT 是唯一一份与我们实现无关的真值：
  · 来源是美国海岸警卫队事故调查报告（IIR），人工比对 AIS 航迹得出；
  · 208 行 / 154 起 / 145 船，每行给出 AIS 时间窗与事故类型；
  · 它**不是** COLREG 条款真值，而是"这条船在这段时间里出没出事"，
    所以它同时是①与文献基线正面可比的那个任务，②级联真正的开放世界场景。
    —— L1 只会抓几何违规；推进失效、拖带、搁浅这些它天然抓不到，
       于是 L2/L3 在这里才有存在意义，准确率×代价的权衡才是真的。

【数据】data/public/marinecadastre/hawaii/YYYY-MM-DD.csv.gz（MarineCadastre 日包，
  bbox 抽过）。已验证这些日包**确实包含** GT 里的船。

【公平性控制】
  ① 负样本与正样本**同日**抽取，避免"哪天船多"变成判别信号；
  ② 只取在航船（窗口内 SOG 中位 ≥ MIN_MOVING_KN）——泊位船 SOG≈0.1，
     不排掉的话基线只要看"动没动"就能分类，和事故毫无关系；
  ③ 正负样本按当天数量配比，并报告两侧的速度/点数分布，配平了才算数。

用法：
  python benchmark/hawaii_eval.py --build          # 建评测集（可续跑，逐日写盘）
  python benchmark/hawaii_eval.py --run --only classical
  python benchmark/hawaii_eval.py --run --only deep
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, r"E:\AIS")
from benchmark import baselines as B  # noqa: E402
from benchmark.reference import colreg_ref as R  # noqa: E402

ROOT = Path(r"E:\AIS")
GT_CSV = ROOT / "实验数据/HawaiiCoast_GT_GitHub/hawaii_primary_trajectories_of_interest_2017_2020.csv"
MC_DIR = ROOT / "data/public/marinecadastre/hawaii"
CONV_DIR = ROOT / "data/logs/hawaii_days"      # 转成管线格式的日切文件
SET_PATH = ROOT / "data/logs/hawaii_evalset.json"

MIN_MOVING_KN = 0.5        # 窗口内 SOG 中位低于此值视为泊位船，两端都不取
MAX_WIN_PER_INCIDENT = 3   # 一起事故最多取几个 20 分钟窗（免得长事故淹没数据集）
NEG_PER_DAY_CAP = 60       # 每天负样本上限
MAX_INCIDENT_DAYS = 7      # 事故跨天登记的上限（防脏数据）
ANCHOR_H = 1.0             # 正样本窗锚在报告时刻 ± 这么多小时（见 build 的注释）


# ── 转换：MarineCadastre → 管线格式 ────────────────────────────────
# 管线要 mmsi,time,lon,lat,course,heading,speed。
#
# 时间必须写成 **ISO 的 %Y-%m-%d %H:%M:%S**，不能写 m/d/Y。
# colreg_ref._parse_t 支持的格式里，斜杠那两种是 **日/月/年**
# （本地数据 "9/10/2022" 就是 2022-10-09）。写成月/日/年会有两种结局：
#   "7/14/2017" → 月份 14 不合法 → 整天解析失败、静默变成 0 条轨迹；
#   "7/12/2017" → 月份 12 合法 → **静默解析成 12 月 7 日**，数据错日期还照样跑。
# 后者比前者危险得多。ISO 没有歧义，直接避开。
def convert_day(gz_path: Path, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(gz_path, "rt", encoding="utf-8", newline="") as f, \
            open(out_path, "w", encoding="utf-8", newline="") as g:
        rd = csv.DictReader(f)
        wr = csv.writer(g)
        wr.writerow(["mmsi", "time", "lon", "lat", "course", "heading", "speed"])
        for r in rd:
            try:
                t = datetime.fromisoformat(str(r["BaseDateTime"]).replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                hd = r.get("Heading") or 511
                wr.writerow([str(r["MMSI"]).strip(), t.strftime("%Y-%m-%d %H:%M:%S"),
                             r["LON"], r["LAT"], r.get("COG") or 0, hd,
                             r.get("SOG") or 0])
                n += 1
            except (KeyError, ValueError):
                continue
    return n


def load_tracks_utc(path: str | Path) -> dict[str, list[R.P]]:
    """过滤口径与 R.load_tracks 完全一致，但时间按 **UTC** 解释。

    【为什么不能直接用 R.load_tracks】
      colreg_ref._parse_t 的最后一步是
          datetime.strptime(s, fmt).timestamp()
      strptime 出来的是 **naive** datetime，而 naive 的 .timestamp() 按**本机时区**
      换算。本机在 UTC+8，于是 MarineCadastre 的 UTC 时刻被整体挪了 8 小时：
      2017-03-19.csv.gz 里装的明明是 03-19 00:00→23:59 UTC，
      读出来却成了 03-18 16:00 → 03-19 15:59 —— **整天错位**。

      后果不是报错，是静默丢正样本：GT 给的事故窗是真 UTC
      （'2017-03-19 13:01:18-10:00' 这类带偏移，pandas 解析得对），
      而轨迹被挪了 8 小时，两边对不上，于是 03-19、07-12 两天"正 0"，
      看起来像那两天没出事，其实只是窗错位了。

      本地 fzais 数据用的是北京时间，按本机时区解释正好是对的，
      所以这个坑在本地数据上永远不暴露——只有拿它读 UTC 的公开数据才会踩到。
    """
    tracks: dict[str, list[R.P]] = {}
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
                t = (datetime.strptime(row["time"].strip(), "%Y-%m-%d %H:%M:%S")
                     .replace(tzinfo=timezone.utc).timestamp())
            except (KeyError, TypeError, ValueError):
                continue
            if cog >= 360.0 or sog >= 102.3:
                continue
            tracks.setdefault(mmsi, []).append(R.P(mmsi, t, lat, lon, sog, cog))
    out = {}
    for mmsi, pts in tracks.items():
        pts.sort(key=lambda p: p.t)
        if len(pts) >= 2:
            out[mmsi] = pts
    return out


def load_gt() -> list[dict]:
    """GT → [{mmsi, start, end, t, types, evidence}]，按船+事故去重。

    `t` = report_incident_time_of_interest_hst —— 这份真值里**唯一良定义的事故时刻**。
    两个 bound 字段只是海岸警卫队报告给出的**外沿**（中位跨度 17.34 小时，
    覆盖大半个白天），拿它当事故时刻等于说"这一天里某个 17 小时区间出过事"。
    只在 s<=t<=e 时 `t` 可用；不满足的记 None，不拿它当正样本（见 build）。
    """
    import pandas as pd
    d = pd.read_csv(GT_CSV)
    d["s"] = pd.to_datetime(d.ais_incident_start_bound_hst, utc=True, errors="coerce")
    d["e"] = pd.to_datetime(d.ais_incident_end_bound_hst, utc=True, errors="coerce")
    d["t"] = pd.to_datetime(d.report_incident_time_of_interest_hst,
                            utc=True, errors="coerce")
    out, seen = [], set()
    for _, r in d.iterrows():
        if pd.isna(r["s"]) or pd.isna(r["e"]):
            continue
        key = (str(r["MMSI"]), str(r["incident_num"]))
        if key in seen:
            continue
        seen.add(key)
        t = (float(r["t"].timestamp())
             if pd.notna(r["t"]) and r["s"] <= r["t"] <= r["e"] else None)
        out.append({"mmsi": str(r["MMSI"]), "start": float(r["s"].timestamp()),
                    "end": float(r["e"].timestamp()), "t": t,
                    "types": str(r["ais_incident_type"]),
                    "evidence": str(r["ais_incident_evidence"])})
    return out


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _moving(win: list[R.P]) -> bool:
    if len(win) < B.MIN_POINTS:
        return False
    sog = sorted(p.sog for p in win)
    return sog[len(sog) // 2] >= MIN_MOVING_KN


def build() -> int:
    """逐日建评测集；已建过的日直接跳过，可随时中断续跑。"""
    gt = load_gt()
    have_days = sorted({p.name[:10] for p in MC_DIR.glob("*.csv.gz")})
    n_anchor = sum(1 for g in gt if g.get("t") is not None)
    print(f"GT {len(gt)} 条（去重后）  有报告时刻锚点 {n_anchor}  已下载 {len(have_days)} 天")

    # 事故要登记在它横跨的**每一天**上，不能只登记首尾日。
    # 原来只塞 start/end 两天，于是 IRON LADY 拖带（01-07 → 01-09）在
    # 下载到的 01-09 那天产出 0 个正样本——可它明明还在事故中。
    # 长事故的中间日全被漏掉，正样本数被系统性低估。
    #
    # 【这张表只用来"排除负样本"】把 GT 船在事故期的窗排除出负样本集。
    # 正样本的取法另有一套（下面 anchored），见那段注释。
    by_day: dict[str, list[dict]] = {}
    for g in gt:
        d0 = datetime.fromtimestamp(g["start"], tz=timezone.utc).date()
        d1 = datetime.fromtimestamp(g["end"], tz=timezone.utc).date()
        span = min((d1 - d0).days, MAX_INCIDENT_DAYS)   # 防脏数据拉出几百天
        for k in range(span + 1):
            by_day.setdefault(str(d0 + timedelta(days=k)), []).append(g)

    # 正样本窗锚在**报告时刻**，不锚在宽界起点。
    #
    # 原来取的是 [start-1h, end+1h] 里的**前 3 个** 20 分钟窗。宽界中位跨度
    # 17.34 小时，于是"前 3 个窗"落在事故上界之前好几个小时——实测正样本窗到
    # 报告时刻的中位距离 **7.83 小时**、p75 **127 小时**，只有 12% 落在 ±30 分钟内。
    # 那些窗里船在正常航行，标签是错的。后果是可观测的：八种文献基线在这份
    # 评测集上 AUC 全部落在 0.48~0.57（随机水平）、oracle F1 全部贴着"全判异常"
    # 的平凡天花板 10.7%。不是方法都不行，是正样本大半不该是正样本。
    #
    # 改成取 t±ANCHOR_H 内离 t 最近的至多 MAX_WIN_PER_INCIDENT 个窗，问题才良定义：
    # **"这个 20 分钟窗里，这条船是否正在一起已上报的事故中"**。
    # 没有可用锚点的事故一条正样本都不出——宁可少，不要错的。
    # 时间锚定不会被偷分：FEATURE_NAMES 里没有任何"时刻"维度（cog_sin/cos 是航向，
    # sequence 的时间通道是窗内归一化的），所以基线无法靠钟点区分正负。
    ANCHOR_S = ANCHOR_H * 3600.0
    anchored: dict[str, list[dict]] = {}
    for g in gt:
        if g.get("t") is None:
            continue
        for d in {_day_of(g["t"] - ANCHOR_S), _day_of(g["t"] + ANCHOR_S)}:
            anchored.setdefault(d, []).append(g)

    old = json.loads(SET_PATH.read_text(encoding="utf-8")) if SET_PATH.exists() else {}
    samples: list[dict] = old.get("samples", [])
    done_days = {s["day"] for s in samples}
    print(f"已有样本 {len(samples)}（覆盖 {len(done_days)} 天）")

    for day in have_days:
        if day in done_days:
            continue
        gz = MC_DIR / f"{day}.csv.gz"
        conv = CONV_DIR / f"{day}.csv"
        if not conv.exists():
            try:
                n = convert_day(gz, conv)
            except Exception as exc:                       # 半截的日包，跳过
                print(f"  [跳过] {day} 转换失败：{exc}")
                continue
            print(f"  {day} 转换 {n:,} 行")
        tracks = load_tracks_utc(conv)
        if not tracks:
            print(f"  [跳过] {day} 无有效轨迹")
            continue

        # 当天的正样本：GT 船在**报告时刻 ±ANCHOR_H** 内的 20 分钟切片，
        # 取离报告时刻最近的至多 MAX_WIN_PER_INCIDENT 个。
        pos: list[dict] = []
        for g in anchored.get(day, []):
            t = g["t"]
            pts = [p for p in tracks.get(g["mmsi"], [])
                   if t - ANCHOR_S <= p.t <= t + ANCHOR_S]
            if len(pts) < B.MIN_POINTS:
                continue
            wins = [w for w in B.split_windows(pts, B.WINDOW_MIN) if _moving(w)]
            wins.sort(key=lambda w: abs((w[0].t + w[-1].t) / 2.0 - t))
            for w in wins[:MAX_WIN_PER_INCIDENT]:
                pos.append({"mmsi": g["mmsi"], "t0": w[0].t, "t1": w[-1].t,
                            "label": 1, "types": g["types"],
                            "evidence": g["evidence"], "anchor_t": t})
        # 当天有锚点事故、但轨迹取不到窗的，也要报出来——不然"正样本少"
        # 会被误读成"那天没出事"，而实际是数据缺。
        n_inc = len(anchored.get(day, []))
        if n_inc and not pos:
            print(f"  {day}: 锚点事故 {n_inc} 起，但窗口内轨迹不足 {B.MIN_POINTS} 点 → 0 正样本")

        # 当天的负样本：同日在航、且当天不在 GT 里（按**宽界**登记，宽严都排）的船
        gt_here = {g["mmsi"] for g in by_day.get(day, [])}
        negs: list[list[R.P]] = []
        for m, pts in tracks.items():
            if m in gt_here:
                continue
            for w in B.split_windows(pts, B.WINDOW_MIN):
                if _moving(w):
                    negs.append(w)
        rng = np.random.default_rng(hash(day) % (2 ** 31))
        rng.shuffle(negs)
        negs = negs[:max(NEG_PER_DAY_CAP, len(pos) * 2)]

        for w in negs:
            samples.append({"mmsi": w[0].mmsi, "t0": w[0].t, "t1": w[-1].t,
                            "label": 0, "types": "", "evidence": "", "day": day,
                            "csv": str(conv)})
        for s in pos:
            s["day"] = day
            s["csv"] = str(conv)
        samples.extend(pos)
        print(f"  {day}: 正 {len(pos)}  负 {len(negs)}  累计 {len(samples)}")
        SET_PATH.write_text(json.dumps(
            {"samples": samples, "min_moving_kn": MIN_MOVING_KN,
             "anchor_h": ANCHOR_H},
            ensure_ascii=False), encoding="utf-8")

    npos = sum(1 for s in samples if s["label"])
    print(f"\n评测集：{len(samples)} 窗（正 {npos} / 负 {len(samples) - npos}）"
          f" → {SET_PATH}")
    return 0


def run(args) -> int:
    """在评测集上跑基线。窗口与特征复用 baselines 的原语，口径完全一致。"""
    blob = json.loads(SET_PATH.read_text(encoding="utf-8"))
    samples = blob["samples"]
    if not samples:
        print("评测集为空，先跑 --build")
        return 1

    by_csv: dict[str, list[dict]] = {}
    for s in samples:
        by_csv.setdefault(s["csv"], []).append(s)

    Xs, Ss, ys = [], [], []
    for csv, group in by_csv.items():
        tracks = load_tracks_utc(csv)
        tindex = B.build_time_index(tracks)
        for s in group:
            pts = [p for p in tracks.get(s["mmsi"], []) if s["t0"] <= p.t <= s["t1"]]
            if len(pts) < B.MIN_POINTS:
                continue
            sep = B.separation(pts, tindex)
            Xs.append(B.features(pts, sep[1]))
            Ss.append(B.sequence(pts, sep[0]))
            ys.append(s["label"])
        print(f"  {Path(csv).name}: 已提取 {len(Xs)}", flush=True)

    X = np.stack(Xs); S = np.stack(Ss); y = np.array(ys, dtype=bool)
    print(f"\n特征 {X.shape}  正 {int(y.sum())}  负 {int((~y).sum())}")
    if y.sum() < 5 or (~y).sum() < 5:
        print("正负样本太少，先多下几天数据再跑")
        return 1

    # 不做负样本下采样，全部 528 窗都进来。
    #
    # 原来抽 96 个负样本，注释写"抽到与正样本等量，AUC 才不受配比影响"——
    # 这句是错的：AUC 与 ROC 曲线**本身**就与类别配比无关，受配比影响的
    # 只有 precision。而 FPR 的估计精度直接由负样本数决定：
    # 1% FPR 在 480 个负样本上是 4.8 个点，在 96 个上只有 0.96 个——后者抖动极大，
    # 报出来的 FPR 曲线基本是噪声。
    # 更要紧的是 L1 那边（data/logs/l1_hawaii.py）按全部 528 窗评，
    # 两边必须落在同一个集合上，否则"同等误报率下比召回"这件事根本无从谈起。
    idx = np.arange(len(y))
    fit = np.flatnonzero(~y)      # 拟合集 = 全部真船正常窗（无监督方法的规矩）
    ev = idx
    outp = ROOT / "data/logs/hawaii_eval.json"

    def flush() -> None:
        outp.write_text(json.dumps(out, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    # 续跑：把文件里已有的方法原样带进来，只补缺的。
    # 每次跑完一个方法就落盘 —— 一次训练几分钟到十几分钟，
    # 中途关机不该把已经跑完的一起丢掉。
    # 想强制重算某个方法，就把它从 hawaii_eval.json 的 methods 里删掉（同 baselines.py）。
    old = json.loads(outp.read_text(encoding="utf-8")) if outp.exists() else {}
    out = {"n_pos": int(y.sum()), "n_neg": int((~y).sum()),
           "methods": dict(old.get("methods", {}))}

    if args.only in ("all", "classical"):
        want = [m for m in ("proximity", "lof", "iforest", "dbscan")
                if m not in out["methods"]]
        if want:
            got = B.run_classical(X[fit], X[fit], X[ev], y[ev], want)
            out["methods"].update(got)
            flush()
        for m in ("proximity", "lof", "iforest", "dbscan"):
            r = out["methods"][m]
            print(f"  {m:<12} AUC {r['auc']:.3f}   oracle P {B.pct(r['precision'])} "
                  f"R {B.pct(r['recall'])} F1 {B.pct(r['f1'])}")
    if args.only in ("all", "deep"):
        hcache = ROOT / "data/logs/hawaii_cache"
        hcache.mkdir(parents=True, exist_ok=True)
        for name in args.models:
            if name in out["methods"]:            # 续跑：已有的不重训
                print(f"  {name:<12} 已有，跳过", flush=True)
                continue
            out["methods"][name] = B.run_deep(name, S[fit], S[fit], S[ev], y[ev],
                                              args.epochs, hcache)
            flush()                               # ← 每跑完一个就写盘
            r = out["methods"][name]
            print(f"  {name:<12} AUC {r['auc']:.3f}   oracle F1 {B.pct(r['f1'])}  "
                  f"({r['seconds']}s)", flush=True)
    flush()
    print(f"\n明细 → {outp}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--only", choices=["all", "classical", "deep"], default="classical")
    ap.add_argument("--models", nargs="*",
                    default=["vae", "lstm_ae", "vae_lstm", "transformer_lstm"])
    ap.add_argument("--epochs", type=int, default=120)
    args = ap.parse_args()
    if args.build:
        return build()
    if args.run:
        return run(args)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
