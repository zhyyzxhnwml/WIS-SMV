# -*- coding: utf-8 -*-
"""L1 洪泛类型抑制：把「天天报、但和真事故无关」的事件类型关掉。

【为什么要做这一轮】
  L1 在 HawaiiCoast_GT 上的松口径精确率只有 1.7%，误报率 21.7% —— 每天报掉
  九成以上的船，报警量远超真事故量。这不一定是「规则写得差」，很可能是
  **几个高频类型把真信号淹了**。

  lift 的定义：lift(类型) = P(该类型出现 | 该窗是真事故) ÷ P(该类型出现 | 该窗正常)。
  lift = 1 表示这个类型对「是不是事故」毫无信息；lift < 1 表示它更常出现在
  **正常**窗里 —— 那它不只是没用，是**反向噪声**，会把真信号盖住。

【几轮迭代】
  P0 现役：所有类型都算报警
  P1 抑制 lift < 1 的类型
  P2 抑制 lift ≤ 1.1 的类型（更激进）
  P3 只留 lift 最高的若干类型
每一轮都同时报「松口径」（报了任何事件）与「严口径」（有硬证据）。

【成本】零。全部在已有缓存上重算，不重跑 L1。

用法：
  python experiments/paper/flood_suppress.py
"""
from __future__ import annotations

import json
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


def main() -> int:
    from ais_accelerator.violation_catalog import deterministic_violation
    samples = json.loads(SET.read_text(encoding="utf-8"))["samples"]
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_day[Path(s["csv"]).stem].append(s)

    ev_by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    have = {p.stem for p in CACHE.glob("*.json")}
    for d in sorted(by_day):
        p = CACHE / f"{d}.json"
        if p.exists():
            for e in json.loads(p.read_text(encoding="utf-8"))["rows"]:
                ev_by[(d, str(e["mmsi"]))].append(e)
    print(f"评测集 {len(samples)} 窗；L1 缓存覆盖 {len(have & set(by_day))}/{len(by_day)} 天")

    # ── 每个窗的事件类型集合 ───────────────────────────────────────
    win_types: list[set[str]] = []
    win_evs: list[list[dict]] = []
    labels: list[bool] = []
    for d, group in by_day.items():
        for s in group:
            evs = [e for e in ev_by.get((d, str(s["mmsi"])), ())
                   if e["start"] <= s["t1"] and e["end"] >= s["t0"]]
            win_evs.append(evs)
            win_types.append({e["type"] for e in evs})
            labels.append(bool(s["label"]))
    npos = sum(labels)
    nneg = len(labels) - npos
    print(f"覆盖窗 {len(labels)}（正 {npos} / 负 {nneg}）")

    # ── 逐类型 lift ───────────────────────────────────────────────
    allt = sorted({t for ts in win_types for t in ts})
    lifts: dict[str, float] = {}
    for t in allt:
        p_pos = sum(1 for ts, y in zip(win_types, labels) if t in ts and y) / max(1, npos)
        p_neg = sum(1 for ts, y in zip(win_types, labels) if t in ts and not y) / max(1, nneg)
        lifts[t] = p_pos / p_neg if p_neg else float("inf")
    print("\n逐类型 lift（= 事故窗出现率 ÷ 正常窗出现率；<1 = 反向噪声）：")
    for t in sorted(allt, key=lambda x: -lifts[x]):
        cnt = sum(1 for ts in win_types if t in ts)
        print(f"  {t:<26} lift {lifts[t]:>7.3f}   覆盖 {cnt:>5} 窗")

    def evaluate(keep: set[str], tag: str) -> dict:
        loose = [bool(ts & keep) for ts in win_types]
        strict = []
        for evs in win_evs:
            kept = [{"candidate_type": e["type"], "features": e["features"]}
                    for e in evs if e["type"] in keep]
            strict.append(bool(kept) and deterministic_violation(kept) is not None)
        out = {}
        for name, pred in (("松口径", loose), ("严口径", strict)):
            tp = sum(1 for p, y in zip(pred, labels) if p and y)
            fp = sum(1 for p, y in zip(pred, labels) if p and not y)
            fn = sum(1 for p, y in zip(pred, labels) if not p and y)
            tn = sum(1 for p, y in zip(pred, labels) if not p and not y)
            n_alert = sum(pred)
            p, r, f = prf(tp, fp, fn)
            out[name] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                         "precision": p, "recall": r, "f1": f,
                         "fpr": fp / (fp + tn) if fp + tn else 0.0,
                         "alert_rate": n_alert / len(labels)}
        out["_keep"] = sorted(keep)
        return out

    cover = {t: sum(1 for ts in win_types if t in ts) for t in allt}
    by_cover = sorted(allt, key=lambda x: -cover[x])
    rounds = {
        "P0 现役（全部类型）": set(allt),
        "P1 抑制 lift<1": {t for t in allt if lifts[t] >= 1.0},
        "P2 抑制 lift<=1.1": {t for t in allt if lifts[t] > 1.1},
        "P3 只留 lift 前 4 名": set(sorted(allt, key=lambda x: -lifts[x])[:4]),
        "P4 抑制报警最多的 1 类": set(allt) - {by_cover[0]},
        "P5 抑制报警最多的 2 类": set(allt) - set(by_cover[:2]),
        "P6 抑制报警最多的 3 类": set(allt) - set(by_cover[:3]),
    }

    print("\n" + "=" * 104)
    print("各轮结果（同一份 11064 窗真值；零额外成本，全部在缓存上重算）")
    print("-" * 104)
    print(f"{'轮次':<24}{'口径':<8}{'报警率':>9}{'精确率':>9}{'召回':>8}{'F1':>8}{'误报率':>9}")
    result = {}
    for tag, keep in rounds.items():
        r = evaluate(keep, tag)
        result[tag] = r
        for name in ("松口径", "严口径"):
            v = r[name]
            print(f"{tag:<24}{name:<8}{v['alert_rate']*100:>8.1f}%"
                  f"{v['precision']*100:>8.1f}%{v['recall']*100:>7.1f}%"
                  f"{v['f1']*100:>7.1f}%{v['fpr']*100:>8.1f}%")
        print(f"{'':<24}保留类型: {', '.join(r['_keep'])}")
        print()

    base = npos / (npos + nneg)
    print(f"参考：正例占比 {base*100:.2f}% → 「全判异常」的平凡 F1 上限 = "
          f"{2*base/(1+base)*100:.2f}%")

    (L / "paper" / "flood_suppress.json").write_text(
        json.dumps({"lifts": lifts, "rounds": result, "n_pos": npos, "n_neg": nneg},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {L / 'paper' / 'flood_suppress.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
