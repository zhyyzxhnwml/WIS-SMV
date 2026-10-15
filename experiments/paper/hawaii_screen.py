# -*- coding: utf-8 -*-
"""在 HawaiiCoast_GT 上跑**有监督窗口筛查**，与 8 个无监督基线做同口径对比。

【为什么加这一轮】
  论文的 8 个方法全是**无监督**的（只用正常样本拟合）。而我们的级联里
  第二级（L2 筛查）是**有监督**的——这一点在比较里必须被兑现，
  否则等于把资源压在规则引擎一级上，而级联的另一半没用上。

  这里做的是**最严格的对照**：用**完全相同**的特征（`benchmark/baselines.py`
  的 23 维窗口特征 + 会遇距离），只把「无监督拟合」换成「有监督训练」。
  于是分数差只能归因于「有没有用标签」，不能归因于特征好坏。

【防止把结论建在泄漏上】
  · 主口径一律剔掉 `cog_sin` / `cog_cos`：这两个是**定位代理**（船朝哪边 →
    反推它在哪片海域），而事故船集中在特定航路上，模型会靠位置偷分。
    这一点在 docs/论文实验交接.md §4e 已经被量过（AUC 0.685 → 0.577）。
  · 交叉验证**按船分组**（同一 MMSI 只落一折）：同一条船在同一天的两段轨迹
    几乎相同，随机划分会把答案漏给训练集。
  · 同时报**按天分组**的结果：同一天的海况/船队共享，按船分组挡不住这一层。
  · 阈值一律用 **oracle**（在评测集上取 F1 最大）——与基线同口径，给双方同样的待遇；
    另报 @1%FPR 的诚实工作点。

【可续跑】逐日特征落盘到 data/logs/paper/screen_feat/<day>.npz。

用法：
  python experiments/paper/hawaii_screen.py                # 抽特征（约 30 分钟，可续跑）
  python experiments/paper/hawaii_screen.py --train        # 只训练与评测
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from benchmark import baselines as B                      # noqa: E402
from benchmark.hawaii_eval import load_tracks_utc         # noqa: E402

L = ROOT / "data/logs"
PAPER = L / "paper"
FEAT = PAPER / "screen_feat"
SET = L / "hawaii_evalset.json"

DROP = (18, 19)          # cog_sin / cog_cos = 定位代理，主口径剔除
SEED = 20261015


def extract() -> None:
    FEAT.mkdir(parents=True, exist_ok=True)
    samples = json.loads(SET.read_text(encoding="utf-8"))["samples"]
    by_csv: dict[str, list[dict]] = {}
    for s in samples:
        by_csv.setdefault(s["csv"], []).append(s)

    todo = [c for c in by_csv if not (FEAT / f"{Path(c).stem}.npz").exists()]
    print(f"共 {len(by_csv)} 天，待抽特征 {len(todo)} 天（已完成 "
          f"{len(by_csv)-len(todo)} 天，直接跳过）", flush=True)
    for i, csv in enumerate(sorted(todo), 1):
        group = by_csv[csv]
        tracks = load_tracks_utc(csv)
        tindex = B.build_time_index(tracks)
        Xs, ys, mm = [], [], []
        for s in group:
            pts = [p for p in tracks.get(s["mmsi"], [])
                   if s["t0"] <= p.t <= s["t1"]]
            if len(pts) < B.MIN_POINTS:
                continue
            sep = B.separation(pts, tindex)
            Xs.append(B.features(pts, sep[1]))
            ys.append(int(s["label"]))
            mm.append(str(s["mmsi"]))
        if Xs:
            np.savez_compressed(
                FEAT / f"{Path(csv).stem}.npz",
                X=np.stack(Xs).astype(np.float32),
                y=np.array(ys), mmsi=np.array(mm))
        print(f"  [{i}/{len(todo)}] {Path(csv).stem}: {len(Xs)} 窗", flush=True)
    print("特征抽取完成")


def load_all():
    Xs, ys, ms, ds = [], [], [], []
    for p in sorted(FEAT.glob("*.npz")):
        z = np.load(p, allow_pickle=True)
        Xs.append(z["X"])
        ys.append(z["y"])
        ms.append(z["mmsi"])
        ds.append(np.full(len(z["y"]), p.stem, dtype=object))
    X = np.concatenate(Xs)
    y = np.concatenate(ys).astype(bool)
    mmsi = np.concatenate(ms)
    day = np.concatenate(ds)
    return X, y, mmsi, day


def auc(y, s):
    from sklearn.metrics import roc_auc_score
    if not y.any() or y.all() or len(np.unique(s)) < 2:
        return float("nan")
    return float(roc_auc_score(y, s))


def prf(y, pred):
    tp = int((pred & y).sum()); fp = int((pred & ~y).sum()); fn = int((~pred & y).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def oracle_f1(y, s) -> tuple[float, float, float]:
    best = (0.0, 0.0, 0.0)
    for t in np.unique(s):
        m = prf(y, s >= t)
        if m[2] > best[2]:
            best = m
    return best


def recall_at_fpr(y, s, q: float) -> tuple[float, float]:
    """阈值 = 负样本分数的 (1-q) 分位；在全部样本上报实际召回与 FPR。"""
    neg = s[~y]
    if neg.size == 0:
        return float("nan"), float("nan")
    thr = float(np.quantile(neg, 1.0 - q, method="higher"))
    pred = s >= thr
    _, r, _ = prf(y, pred)
    return r, float(pred[~y].mean())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true", help="跳过抽特征，只训练评测")
    args = ap.parse_args()
    if not args.train:
        extract()

    X, y, mmsi, day = load_all()
    print(f"\n样本 {len(y)} 窗（正 {int(y.sum())}）/ 船 {len(set(mmsi))} 条 / 天 {len(set(day))}")
    meta = json.loads((ROOT / "triage_model.txt.meta.json").read_text(encoding="utf-8"))
    names = list(getattr(B, "FEATURE_NAMES", [f"f{i}" for i in range(X.shape[1])]))

    import lightgbm as lgb
    from sklearn.model_selection import GroupKFold

    results = {}
    for tag, cols in (("剔定位代理（主口径 21 维）",
                       [i for i in range(X.shape[1]) if i not in DROP]),
                      ("含全部 23 维（对照）", list(range(X.shape[1])))):
        for gname, groups in (("按船分组", mmsi), ("按天分组", day)):
            Xu = X[:, cols]
            oof = np.zeros(len(y))
            gkf = GroupKFold(n_splits=5)
            for tr, te in gkf.split(Xu, y, groups):
                # 类别不平衡：按正例比例放大正类权重，避免模型退化成"全判正常"
                spw = float((~y[tr]).sum()) / max(1, int(y[tr].sum()))
                clf = lgb.LGBMClassifier(
                    n_estimators=300, learning_rate=0.05, num_leaves=31,
                    subsample=0.9, colsample_bytree=0.9, random_state=SEED,
                    scale_pos_weight=spw, verbose=-1)
                clf.fit(Xu[tr], y[tr])
                oof[te] = clf.predict_proba(Xu[te])[:, 1]
            a = auc(y, oof)
            p, r, f = oracle_f1(y, oof)
            r1, fpr1 = recall_at_fpr(y, oof, 0.01)
            results[f"{tag} / {gname}"] = {
                "auc": a, "oracle_precision": p, "oracle_recall": r, "oracle_f1": f,
                "recall_at_1fpr": r1, "fpr_at_1fpr": fpr1}
            print(f"  {tag} / {gname:<6} AUC {a:.3f}  oracle P {p*100:5.1f}% "
                  f"R {r*100:5.1f}% F1 {f*100:5.1f}%   @1%FPR 召回 {r1*100:5.1f}%")

    # ── 与 8 个无监督基线对照 ──────────────────────────────────────
    he = json.loads((L / "hawaii_eval.json").read_text(encoding="utf-8"))
    npos, nneg = he["n_pos"], he["n_neg"]
    base = npos / (npos + nneg)
    print("\n" + "=" * 96)
    print("同口径对照（同一批 11,064 窗、同一份真值、同一批特征）")
    print("-" * 96)
    print(f"{'方法':<38}{'AUC':>8}{'oracle F1':>11}{'@1%FPR 召回':>13}")
    ORDER = ["proximity", "lof", "iforest", "dbscan", "vae", "lstm_ae",
             "vae_lstm", "transformer_lstm"]
    for k in ORDER:
        rr = he["methods"].get(k)
        if rr:
            print(f"{'[无监督] '+k:<38}{rr['auc']:>8.3f}{rr['f1']*100:>10.1f}%"
                  f"{rr['fpr1_recall']*100:>12.1f}%")
    for k, v in results.items():
        print(f"{'[有监督 本文] '+k:<38}{v['auc']:>8.3f}{v['oracle_f1']*100:>10.1f}%"
              f"{v['recall_at_1fpr']*100:>12.1f}%")
    print(f"\n平凡线（全判异常）F1 = {2*base/(1+base)*100:.2f}%")

    (PAPER / "hawaii_screen.json").write_text(json.dumps(
        {"n": int(len(y)), "n_pos": int(y.sum()), "results": results,
         "unsupervised": {k: he["methods"][k] for k in ORDER if k in he["methods"]}},
        ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {PAPER / 'hawaii_screen.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
