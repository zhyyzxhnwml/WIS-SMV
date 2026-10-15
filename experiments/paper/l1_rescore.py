# -*- coding: utf-8 -*-
"""L1 输出重排：把「有就报」的布尔规则，换成可排序的分数。

【为什么走这条路】
  上一轮试「抑制洪泛类型」失败——因为那是**丢信息**的做法（要么报要么不报）。
  真问题不是「哪些类型该丢」，而是 L1 现在把**所有事件一视同仁**：
  报了一条 `ais_gap` 和报了一条 `illegal_towing`，在当前实现里权重完全一样。
  但它们的 lift 差了近 30 倍（1.38 vs 0.00 的极端情形也有）。

  所以正确的做法是**重排**而不是**删除**：
    得分(窗) = Σ 该窗出现过的类型的权重 w(类型)
  然后在一个工作点上取阈值。这样低信息的类型自然被压下去，
  但它携带的那一点点信息也不会被粗暴丢掉。

【不许作弊：留出法标阈值】
  「在评测集上挑使 F1 最大的阈值」是 oracle，等于偷看答案。
  这里按**天**做奇偶分割：A 半区算类型权重 + 挑阈值，B 半区只用来评测。
  两半区不共享任何一天，避免同一天的海况/船队把信息漏过去。

【对照】
  · 现役：有任一事件就报警（布尔 OR），**没有可调旋钮**
  · 重排：可排序分数 + 留出标定的阈值
  两种都在**同一个 B 半区**上评测，可比。

用法：
  python experiments/paper/l1_rescore.py
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

L = ROOT / "data/logs"
CACHE = L / "l1_hawaii_cache_v2"
SET = L / "hawaii_evalset.json"


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def metrics(pred: list[bool], labels: list[bool]) -> dict:
    tp = sum(1 for p, y in zip(pred, labels) if p and y)
    fp = sum(1 for p, y in zip(pred, labels) if p and not y)
    fn = sum(1 for p, y in zip(pred, labels) if not p and y)
    tn = sum(1 for p, y in zip(pred, labels) if not p and not y)
    p, r, f = prf(tp, fp, fn)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn, "precision": p,
            "recall": r, "f1": f, "fpr": fp / (fp + tn) if fp + tn else 0.0,
            "alert_rate": sum(pred) / len(pred)}


def auc(y: list[bool], s: list[float]) -> float:
    from sklearn.metrics import roc_auc_score
    if not any(y) or all(y) or len(set(s)) < 2:
        return float("nan")
    return float(roc_auc_score(y, s))


def main() -> int:
    samples = json.loads(SET.read_text(encoding="utf-8"))["samples"]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_day[Path(s["csv"]).stem].append(s)

    ev_by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for d in by_day:
        p = CACHE / f"{d}.json"
        if p.exists():
            for e in json.loads(p.read_text(encoding="utf-8"))["rows"]:
                ev_by[(d, str(e["mmsi"]))].append(e)

    rows = []
    for d, group in by_day.items():
        for s in group:
            evs = [e for e in ev_by.get((d, str(s["mmsi"])), ())
                   if e["start"] <= s["t1"] and e["end"] >= s["t0"]]
            rows.append({"day": d, "types": {e["type"] for e in evs},
                         "n_ev": len(evs), "y": bool(s["label"])})
    # 按天奇偶分割（同一天不跨两半区）
    days = sorted(by_day)
    half = {d: (i % 2) for i, d in enumerate(days)}
    A = [r for r in rows if half[r["day"]] == 0]
    B = [r for r in rows if half[r["day"]] == 1]
    print(f"留出分割（按天奇偶）：A {len(A)} 窗（正 {sum(r['y'] for r in A)}） / "
          f"B {len(B)} 窗（正 {sum(r['y'] for r in B)}）")

    # ── 在 A 半区算类型权重 ─────────────────────────────────────────
    def lifts(sub: list[dict]) -> dict[str, float]:
        npos = sum(1 for r in sub if r["y"]) or 1
        nneg = sum(1 for r in sub if not r["y"]) or 1
        out = {}
        for t in {t for r in sub for t in r["types"]}:
            pp = sum(1 for r in sub if t in r["types"] and r["y"]) / npos
            pn = sum(1 for r in sub if t in r["types"] and not r["y"]) / nneg
            out[t] = pp / pn if pn else float("inf")
        return out

    lf = lifts(A)
    print("\nA 半区估出的类型权重（log-lift，负值截断为 0）：")
    for t in sorted(lf, key=lambda x: -lf[x]):
        w = max(0.0, math.log(max(lf[t], 1e-9)))
        print(f"  {t:<26} lift {lf[t]:>7.3f}  →  权重 {w:.3f}")

    scorers = {
        "重排 A：Σ log-lift": lambda r: sum(max(0.0, math.log(max(lf.get(t, 1e-9), 1e-9)))
                                          for t in r["types"]),
        "重排 B：Σ lift": lambda r: sum(lf.get(t, 0.0) for t in r["types"]),
        "重排 C：类型数": lambda r: float(len(r["types"])),
        "重排 D：事件条数": lambda r: float(r["n_ev"]),
    }

    yA = [r["y"] for r in A]
    results = {}

    # 现役：布尔 OR
    orA = [bool(r["types"]) for r in A]
    orB_pred = [bool(r["types"]) for r in B]
    yB = [r["y"] for r in B]
    results["现役：有事件就报（OR）"] = {"B": metrics(orB_pred, yB),
                                        "A": metrics(orA, yA), "auc_B": None}

    thrust = None
    for name, fn in scorers.items():
        sA = [fn(r) for r in A]
        sB = [fn(r) for r in B]
        cand = sorted(set(sA))
        best_t, best_f = None, -1.0
        for t in cand:
            m = metrics([v >= t for v in sA], yA)
            if m["f1"] > best_f:
                best_f, best_t = m["f1"], t
        predB = [v >= best_t for v in sB]
        results[name] = {"B": metrics(predB, yB),
                         "A": metrics([v >= best_t for v in sA], yA),
                         "auc_B": auc(yB, sB), "threshold": best_t,
                         "threshold_f1_on_A": best_f}
        if thrust is None or best_f > thrust[1]:
            thrust = (name, best_f)

    print("\n" + "=" * 100)
    print("结果（阈值只在 A 半区标定，指标全部在 **B 半区** 上报 —— 不是 oracle）")
    print("-" * 100)
    print(f"{'方案':<28}{'报警率':>9}{'精确率':>9}{'召回':>8}{'F1':>8}{'误报率':>9}{'AUC':>8}")
    for name, v in results.items():
        b = v["B"]
        a = v["auc_B"]
        print(f"{name:<28}{b['alert_rate']*100:>8.1f}%{b['precision']*100:>8.1f}%"
              f"{b['recall']*100:>7.1f}%{b['f1']*100:>7.1f}%{b['fpr']*100:>8.1f}%"
              f"{(a if a is not None else float('nan')):>8.3f}")

    # 对照：A 半区上的现役（看两半区难度是否接近）
    a_or = results["现役：有事件就报（OR）"]["A"]
    b_or = results["现役：有事件就报（OR）"]["B"]
    print(f"\n两半区难度对照（现役 OR）：A 半区 F1 {a_or['f1']*100:.1f}% / "
          f"B 半区 F1 {b_or['f1']*100:.1f}% —— 越接近说明分割越公平")

    base = sum(yB) / len(yB)
    print(f"\n参考：B 半区正例占比 {base*100:.2f}% → 平凡 F1 上限 "
          f"{2*base/(1+base)*100:.2f}%")

    (L / "paper" / "l1_rescore.json").write_text(json.dumps({
        "split": {"A": len(A), "B": len(B)},
        "lifts_A": lf,
        "results": {k: {kk: vv for kk, vv in v.items()} for k, v in results.items()},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {L / 'paper' / 'l1_rescore.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
