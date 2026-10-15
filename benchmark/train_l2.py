# -*- coding: utf-8 -*-
"""L2 重训与评测：真实数据上的四分类 triage。

【要回答的问题】
  playbook 里 L2 一直是**合成数据**训出来的（triage_engine.train --mode initial
  用特征模板 + 高斯噪声造样本）。现在有了真实标注集（data/logs/l2_real_dataset.jsonl，
  1079 行 / 972 船 / 4 类齐全），必须回答两件事：

  ① 合成数据训出来的模型，放到真实数据上到底什么水平？——这是"before"。
  ② 用真实数据重训能提升多少？——这是论文里 L2 那一格的数字。

  只报②不报①，读者没法判断 L2 的价值；只报①则等于承认 L2 没用。

【维度】
  真实集的特征是 **28 维**，而 triage_model.txt 按 **64 维**训练
  （meta 里 feature_names 有 64 个：前 28 个有名字，reserved_0..reserved_35 是预留）。
  28 维正好是前 28 个特征名，所以补 36 个 0 到 64 维即可——**不能截断模型，
  也不能改训练特征名**，否则保存出来的模型 app 加载不了。
  （model.py 里那条 "data (28) vs training data (64)" 的报错就是漏了这一步。）

【划分】
  按 **mmsi 分组**做 5 折。972 船 / 1079 行，多数船只有 1 行，但同船多行必须同折，
  否则同一条船的相邻窗口一在训练一在测试，macro-F1 会被抬高。

【用法】
  python benchmark/train_l2.py                # before + 5 折 after
  python benchmark/train_l2.py --save data/logs/l2_model_real.txt   # 另存重训模型
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, r"E:\AIS")
from triage_engine.model import LABELS, TriageModel  # noqa: E402

ROOT = Path(r"E:\AIS")
DATASET = ROOT / "data/logs/l2_real_dataset.jsonl"
CURRENT = ROOT / "triage_model.txt"   # 合成数据训的，评测用（别覆盖，见交接 §8）
N_FEAT = 64

# ── 标注派生特征：必须能摘掉，否则整个 L2 是自证 ─────────────────────
#
# build_l2_dataset.label_of() 的标签是 (events, annotations) 的**确定性函数**：
#   obvious_issue  ← deterministic_violation(events)         ← event_count/rule_hit_count
#   has_risk       ← n_crit or n_warn or near_miss           ← f2/f3 就是 n_crit/n_warn
#   needs_review   ← quality or anns or events               ← f1 就是 len(anns)
#   normal         ← 以上都不满足
# 而 feature_vector 的 f1..f7、f18 恰恰就是标注本身的计数与置信度。
# 所以带这些特征训出来的 93.6% macro-F1 **大半是把标签规则背了回来**，
# 不是"从轨迹判断该不该复核"。审稿人一眼就能看出这是标签泄漏。
#
# 摘掉之后 L2 的任务才是它真正的任务：
#   **在还没有人工标注的时候，只凭 L1 给的轨迹/事件特征，预判人工会怎么标。**
# 级联里 L2 本来就是跑在 L1 之后、人工之前，这个口径才是上线口径。
ANNOTATION_FEATS = (
    "annotation_count", "critical_annotation_count", "warning_annotation_count",
    "info_annotation_count", "avg_annotation_confidence", "max_annotation_confidence",
    "min_annotation_confidence", "overlap_annotation_count",
)


def load_dataset(path: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """→ (X 补到 64 维, y 整数标签, ship id)。"""
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    X = np.array([r["feature_vector"] for r in rows], dtype=np.float64)
    y = np.array([LABELS.index(r["label"]) for r in rows], dtype=np.int64)
    ships = np.array([str(r["mmsi"]) for r in rows])
    if X.shape[1] < N_FEAT:  # 28 → 64，补零
        X = np.hstack([X, np.zeros((X.shape[0], N_FEAT - X.shape[1]))])
    return X, y, ships


def folds(ships: np.ndarray, k: int, seed: int = 42) -> list[np.ndarray]:
    """按船分组切 k 折，返回每折的测试下标。"""
    rng = np.random.default_rng(seed)
    uniq = np.unique(ships)
    rng.shuffle(uniq)
    bucket = {s: i % k for i, s in enumerate(uniq)}
    return [np.array([i for i, s in enumerate(ships) if bucket[s] == f])
            for f in range(k)]


def report(tag: str, y: np.ndarray, pred: np.ndarray) -> dict:
    from sklearn.metrics import (accuracy_score, confusion_matrix,
                                 f1_score, precision_recall_fscore_support)
    acc = accuracy_score(y, pred)
    macro = f1_score(y, pred, average="macro", zero_division=0)
    p, r, f, sup = precision_recall_fscore_support(y, pred, labels=range(len(LABELS)),
                                                   zero_division=0)
    # 抛开 normal 的三类 macro-F1 —— 这才是级联真正的工作区间。
    # normal 由标签规则 = "L1 没报事件"，而 event_count 是输入特征，
    # 所以它在任何一版里都是 100%，是个白送的分，混进 macro 会虚高。
    # 级联里 L2 只在 L1 报了事件之后才被调用，normal 根本不进来，
    # 所以报"L1 已报警的船里，严重度分得清吗"——即非 normal 三类 macro-F1。
    nz = [i for i in range(len(LABELS)) if i != LABELS.index("normal") and sup[i]]
    macro_nz = float(np.mean([f[i] for i in nz])) if nz else float("nan")
    print(f"\n{tag}")
    print(f"  准确率 {acc*100:.1f}%   macro-F1 {macro*100:.1f}%   "
          f"非 normal 三类 macro-F1 {macro_nz*100:.1f}%  ← 级联工作区间")
    print(f"  {'类别':<16}{'P':>8}{'R':>8}{'F1':>8}{'n':>7}")
    for i, name in enumerate(LABELS):
        print(f"  {name:<16}{p[i]*100:7.1f}%{r[i]*100:7.1f}%{f[i]*100:7.1f}%{sup[i]:7d}")
    cm = confusion_matrix(y, pred, labels=range(len(LABELS)))
    print("  混淆矩阵（行=真值，列=预测）")
    print(f"    {'':<16}" + "".join(f"{n[:10]:>11}" for n in LABELS))
    for i, name in enumerate(LABELS):
        print(f"    {name:<16}" + "".join(f"{v:>11d}" for v in cm[i]))
    return {"accuracy": float(acc), "macro_f1": float(macro),
            "macro_f1_non_normal": macro_nz,
            "per_class_f1": {n: float(f[i]) for i, n in enumerate(LABELS)},
            "confusion": cm.tolist(), "support": {n: int(sup[i]) for i, n in enumerate(LABELS)}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(DATASET))
    ap.add_argument("--folds", type=int, default=5)
    ap.add_argument("--rounds", type=int, default=300)
    ap.add_argument("--save", default="")
    ap.add_argument("--out", default=str(ROOT / "data/logs/l2_eval.json"))
    ap.add_argument("--drop-annotation-features", action="store_true",
                    help="摘掉标注派生特征（f1..f7、f18），做无泄漏的 L2")
    args = ap.parse_args()

    X, y, ships = load_dataset(Path(args.dataset))
    print(f"数据集 {X.shape}  船 {len(set(ships))}")
    print("标签分布 " + "  ".join(
        f"{n}={int((y == i).sum())}" for i, n in enumerate(LABELS)))

    meta_names = [f"f{i}" for i in range(N_FEAT)]
    if (ROOT / "triage_model.txt.meta.json").exists():
        meta_names = json.loads(
            (ROOT / "triage_model.txt.meta.json").read_text(encoding="utf-8")
        )["feature_names"]
    if args.drop_annotation_features:
        drop = [meta_names.index(n) for n in ANNOTATION_FEATS]
        keep = np.setdiff1d(np.arange(N_FEAT), drop)
        print(f"摘掉标注派生特征 {len(drop)} 个：" +
              " ".join(meta_names[i] for i in drop))
        X = X[:, keep]
        meta_names = [meta_names[i] for i in keep]
        print(f"→ 特征 {X.shape[1]} 维")

    out: dict = {"dataset": X.shape[0], "n_ships": len(set(ships)),
                 "n_features_used": int(X.shape[1]),
                 "dropped_annotation_features": bool(args.drop_annotation_features),
                 "label_counts": {n: int((y == i).sum()) for i, n in enumerate(LABELS)}}

    # ── ① before：现有合成数据模型，在**全部**真实数据上 ─────────────
    # 注意这只说明"合成训的模型在真实分布上如何"，不代表它的真实水平
    # （它在合成分布上可能是好的）——但级联里 L2 面对的就是真实数据，
    # 所以这就是它上线后的实际表现。
    if CURRENT.exists() and not args.drop_annotation_features:
        m = TriageModel()
        m.load(str(CURRENT))
        nf = getattr(m.model, "num_feature", lambda: N_FEAT)()
        if nf == X.shape[1]:
            pred, _ = m.predict(X)
            out["before"] = report("① before：合成数据训的 triage_model.txt（全真实集）",
                                   y, np.asarray(pred))
        else:
            print(f"\n① before 跳过：模型 {nf} 维，与数据 {X.shape[1]} 维不符")
    elif args.drop_annotation_features:
        print("\n① before 跳过：摘特征后与原模型维度不符，无法同口径比较")

    # ── ② after：真实数据 5 折 ────────────────────────────────────
    fs = folds(ships, args.folds)
    oof = np.full(y.shape, -1, dtype=np.int64)
    fold_f1 = []
    for fi, te in enumerate(fs):
        tr = np.setdiff1d(np.arange(len(y)), te)
        # 验证集从训练折里再切一小块（按船），用于早停
        tr_ships = np.unique(ships[tr])
        rng = np.random.default_rng(100 + fi)
        rng.shuffle(tr_ships)
        vs = set(tr_ships[: max(1, len(tr_ships) // 8)].tolist())
        vm = np.array([ships[i] in vs for i in tr])
        m = TriageModel()
        m.train(X[tr][~vm], y[tr][~vm],
                valid_sets=(X[tr][vm], y[tr][vm]),
                num_boost_round=args.rounds, early_stopping_rounds=30)
        p, _ = m.predict(X[te])
        oof[te] = np.asarray(p)
        fold_f1.append(float(np.mean(np.asarray(p) == y[te])))
        print(f"  折 {fi + 1}/{args.folds}  测试 {len(te)} 行  准确率 {fold_f1[-1]*100:.1f}%",
              flush=True)
    out["after"] = report(f"② after：真实数据 {args.folds} 折交叉验证（out-of-fold）",
                          y, oof)
    out["after"]["fold_accuracy"] = fold_f1

    # ── ③ 对比 ────────────────────────────────────────────────────
    if "before" in out and "after" in out:
        b, a = out["before"]["macro_f1"], out["after"]["macro_f1"]
        print(f"\n结论：macro-F1  {b*100:.1f}% → {a*100:.1f}%  "
              f"({'提升' if a > b else '下降'} {abs(a-b)*100:.1f} 个点)")

    # ── ④ 保存重训模型（全量重训一版） ─────────────────────────────
    if args.save:
        if args.drop_annotation_features:
            print("\n⚠ 注意：摘了特征，存出来的模型**不能**直接替到 triage_model.txt ——")
            print("   app 侧的 AccelFeatureExtractor 仍按 64 维喂数据，维度对不上。")
        m = TriageModel()
        m.train(X, y, feature_names=meta_names, num_boost_round=args.rounds)
        m.save(args.save)
        print(f"\n重训模型已存 → {args.save}（+ .meta.json）")

    Path(args.out).write_text(json.dumps(out, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
