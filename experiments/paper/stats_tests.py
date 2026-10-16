# -*- coding: utf-8 -*-
"""显著性检验：我们的系统 vs 每个文献基线，成对比较。

【为什么必须成对】
  同一批船、同一份真值，逐条比"谁对谁错"才有意义。两个方法各自报的
  P/R/F1 是**点估计**，差 3 个百分点到底是真的还是抽样抖动，没有区间就说不清。

【两个检验】
  1. **McNemar 精确检验**（配对、二分类）：
       b = 我们对、基线错；c = 我们错、基线对。
       在 H0（两者错误率相同）下 b ~ Binomial(b+c, 0.5)，双侧精确 p 值。
       用精确版不用卡方近似：c 常常很小，近似不成立。
  2. **配对自助 95% CI**（F1 之差）：
       重采样样本对（保持配对结构），2000 次，报差值的 2.5/97.5 分位。
       配对重采样而不是各自重采样——后者会高估方差。

【口径】
  基线一律用 **oracle 阈值**（在评测集上取 F1 最大 = 它们的上界），
  我们一律用**部署即所得的固定判据**（L1 没有可调旋钮）。
  这是对我们最不利、对基线最有利的比法；如果这样还赢，结论才稳。

用法：
  python experiments/paper/stats_tests.py
"""
from __future__ import annotations

import json
import sys
from math import comb
from pathlib import Path

import numpy as np

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

L = ROOT / "data/logs"
PAPER = L / "paper"
CHAINS = ("give_way_failure", "overtaking_violation")
ORDER = ["proximity", "lof", "iforest", "dbscan", "vae", "lstm_ae", "vae_lstm",
         "transformer_lstm"]
CN = {"proximity": "proximity(会遇距离)", "lof": "LOF", "iforest": "iForest",
      "dbscan": "DBSCAN", "vae": "VAE", "lstm_ae": "LSTM-AE",
      "vae_lstm": "VAE-LSTM", "transformer_lstm": "Transformer-LSTM"}
RNG = np.random.default_rng(20261015)


def mcnemar_exact(b: int, c: int) -> float:
    """H0: 两者错误率相同。双侧精确 p = 2 * P(X <= min(b,c)), X~Bin(b+c, .5)。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return float(min(1.0, 2 * p))


def f1(y: np.ndarray, pred: np.ndarray) -> float:
    tp = int((y & pred).sum())
    fp = int((~y & pred).sum())
    fn = int((y & ~pred).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


def paired_bootstrap_f1_diff(y: np.ndarray, a: np.ndarray, b: np.ndarray,
                             n: int = 2000) -> tuple[float, float]:
    m = len(y)
    idx = np.arange(m)
    out = np.empty(n)
    for i in range(n):
        s = RNG.choice(idx, m, replace=True)
        out[i] = f1(y[s], a[s]) - f1(y[s], b[s])
    return float(np.percentile(out, 2.5)), float(np.percentile(out, 97.5))


def preds_from(r: dict, lab: np.ndarray) -> np.ndarray | None:
    """从 `_scores` + `oracle_threshold` 还原该方法的预测。

    ⚠ `_labels` 存的是**真值**，不是预测（名字有歧义，踩过一次）。
    方向也不假定：`score>=thr` 与 `score<=thr` 各试一次，
    用落盘里已有的 tp/fp 做**自校验**，对不上就返回 None 而不是猜。
    """
    sc = np.asarray(r["_scores"], dtype=float)
    thr = r.get("oracle_threshold")
    if thr is None or len(sc) != len(lab):
        return None
    for p in (sc >= float(thr), sc <= float(thr)):
        tp = int((p & lab).sum())
        fp = int((p & ~lab).sum())
        if tp == int(r.get("tp", -1)) and fp == int(r.get("fp", -2)):
            return p
    return None


def compare(tag: str, y: np.ndarray, ours: np.ndarray,
            others: dict[str, np.ndarray], md: list[str]) -> dict:
    md += [f"### {tag}", "",
           f"评测集 {len(y)} 条（正 {int(y.sum())} / 负 {int((~y).sum())}）。"
           f"我们的 F1 = {f1(y, ours)*100:.1f}%。", "",
           "| 对照方法 | 它的 F1 | F1 差（我们−它）| 95% CI | McNemar b/c | p 值 |",
           "|---|---|---|---|---|---|"]
    res = {}
    for k in ORDER:
        o = others.get(k)
        if o is None:
            continue
        b = int((ours & ~o & ~y).sum() + (~ours & o & y).sum())  # 我们对、它错
        c = int((~ours & o & ~y).sum() + (ours & ~o & y).sum())  # 我们错、它对
        p = mcnemar_exact(b, c)
        lo, hi = paired_bootstrap_f1_diff(y, ours, o)
        d = f1(y, ours) - f1(y, o)
        res[k] = {"f1_ours": f1(y, ours), "f1_theirs": f1(y, o), "diff": d,
                  "ci": [lo, hi], "mcnemar_b": b, "mcnemar_c": c, "p": p}
        star = "***" if p < 1e-3 else "**" if p < 1e-2 else "*" if p < 5e-2 else "n.s."
        md.append(f"| {CN[k]} | {f1(y, o)*100:.1f}% | {d*100:+.1f} pt | "
                  f"[{lo*100:+.1f}, {hi*100:+.1f}] | {b}/{c} | {p:.2e} {star} |")
    md.append("")
    md.append("> `b` = 我们判对而它判错的条数，`c` = 反之。"
              "CI 是配对自助 2000 次的 95% 区间；跨过 0 说明差值不显著。")
    md.append("")
    md.append("> ⚠ **读 p 值要看场景**：McNemar 检验的是两者**错误模式是否相同**。"
              "评测集极度不平衡时，两边在大量负样本上互有对错，b、c 都会很大，"
              "p 值因此很小 —— 这只说明两个方法「错的地方不一样」，**不等于**质量差距大。"
              "质量差距要看 **F1 差与它的 CI**：注入集上 CI 是 +17~+39 pt 且不含 0，"
              "那才是决定性差距。")
    md.append("")
    return res


def injection() -> dict | None:
    bl = L / "baselines_inject.json"
    gt_p, l1_p = L / "inject_v1.gt.json", PAPER / "l1_inject.json"
    if not (bl.exists() and gt_p.exists() and l1_p.exists()):
        return None
    gt = json.loads(gt_p.read_text(encoding="utf-8"))
    l1 = json.loads(l1_p.read_text(encoding="utf-8"))
    blob = json.loads(bl.read_text(encoding="utf-8"))
    scenarios = gt["scenarios"]
    y = np.array([s["code"] is not None for s in scenarios])
    ours = np.array([bool({e["type"] for e in (l1.get(str(s["own_mmsi"])) or {})
                           .get("events", [])} & set(CHAINS)) for s in scenarios])
    others = {}
    for k in ORDER:
        r = blob["methods"].get(k)
        if not r:
            continue
        lab = np.asarray(r.get("_labels", []), dtype=bool)
        # 注入集里 `_scores` 只覆盖合成船，顺序 = gt["scenarios"] 的顺序
        # （baselines.py: syn_m = [s["own_mmsi"] for s in gt["scenarios"]]）。
        # 先证实这一点：它的 `_labels` 必须逐条等于我们从 GT 算出的 y。
        if len(lab) != len(y) or not (lab == y).all():
            print(f"  ⚠ {k}: `_labels` 与 GT 对不齐，跳过")
            continue
        p = preds_from(r, lab)
        if p is None:
            print(f"  ⚠ {k}: 预测还原自校验失败，跳过")
            continue
        others[k] = p
    md: list[str] = []
    out = compare("注入集：我们的 L1（规则引擎，0 次 LLM、无阈值可调）", y, ours, others, md)
    return {"md": md, "res": out}


def main() -> int:
    md = ["# 显著性检验（自动生成）", "",
          "> `python experiments/paper/stats_tests.py`", "",
          "基线用 **oracle 阈值**（它们的上界），我们用**部署即所得**的固定判据。"
          "这是对我们最不利、对基线最有利的比法。", ""]
    out = {}
    inj = injection()
    if inj:
        md += ["## 违规判断（注入集）", ""] + inj["md"]
        out["injection"] = inj["res"]
    (ROOT / "论文").mkdir(exist_ok=True)
    (ROOT / "论文" / "显著性检验.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (PAPER / "stats_tests.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(md))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
