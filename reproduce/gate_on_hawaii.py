# -*- coding: utf-8 -*-
"""把新门控（证据强度判据）放到**真实数据**上，量它的调用率。

【为什么这一步必须有】
  门控改动是在注入集上验证的。注入集的船是 20 分钟的合成短轨迹，
  真实数据是一整天的轨迹——两边的 L1 事件分布完全不同，
  注入集上的调用率**不能直接外推**到线上。
  这里在 Hawaii 全量（182 天、3067 条船-日）上直接把调用率算出来。

  ⚠ 注意这一步**不需要调用大模型**：门控只看 L1 报了几条候选事件，
    是纯计算。所以它能在几分钟内给出真实数据上的成本口径。

【三个口径对照】
  · 新门控（本轮的改动）：needs_llm = (L1 候选事件数 == 0)
  · 现役门控 A（旧合成模型 + 置信度阶梯）：docs 记录 ≈16.6%
  · 现役门控 B（真实模型 + 风险分阈值）：docs 记录 ≈17.3%
  前两个的数值来自上一轮交接记录，这里只重算新门控，并把口径写清楚。

用法：
  python experiments/paper/gate_on_hawaii.py
"""
from __future__ import annotations

import json
import sys
import statistics
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

L = ROOT / "data/logs"
CACHE = L / "l1_hawaii_cache_v2"


def main() -> int:
    ship_day: Counter = Counter()          # (day, mmsi) → 事件数
    n_ships = 0
    for p in sorted(CACHE.glob("*.json")):
        blob = json.loads(p.read_text(encoding="utf-8"))
        day = p.stem
        per: dict[str, int] = defaultdict(int)
        for e in blob["rows"]:
            per[str(e["mmsi"])] += 1
        # 当天文件里的**全部**船（含一条事件都没有的）才算真实分母
        allships = int(blob.get("ships") or 0)
        for m, c in per.items():
            ship_day[(day, m)] = c
        n_ships += allships
        # 没事件的船：分母减去有事件的船数
        for _ in range(max(0, allships - len(per))):
            ship_day[(day, f"__none_{day}_{_}")] = 0
    print(f"覆盖 {len({d for d, _ in ship_day})} 天，共 {len(ship_day)} 条船-日"
          f"（其中当天有事件的 {sum(1 for v in ship_day.values() if v)} 条）")

    counts = list(ship_day.values())
    n = len(counts)
    zero = sum(1 for c in counts if c == 0)
    print(f"\n每船-日的事件数分布：中位 {statistics.median(counts):.0f}，"
          f"均值 {statistics.mean(counts):.1f}，最大 {max(counts)}")
    for q in (0.25, 0.5, 0.75, 0.9):
        s = sorted(counts)
        print(f"  p{int(q*100):02d} = {s[int(q*(n-1))]}")

    print("\n" + "=" * 76)
    print("门控调用率（真实数据 Hawaii，182 天 / 3067 船-日）")
    print("-" * 76)
    print(f"{'门控判据':<40}{'LLM 调用率':>14}")
    print(f"{'新：L1 无候选事件才调（本轮改动）':<40}{zero/n*100:>13.1f}%")
    print(f"{'旧 A：合成模型 + 置信度阶梯（交接记录）':<40}{16.6:>13.1f}%")
    print(f"{'旧 B：真实模型 + 风险分阈值（交接记录）':<40}{17.3:>13.1f}%")
    print()
    print(f"→ 新门控在真实数据上的调用率 {zero/n*100:.1f}%，是旧门控的 "
          f"{zero/n/0.173:.2f} 倍（即成本降到约 {zero/n/0.173*100:.0f}%）")

    # ── 关键反问：真出事的船，会不会正好都被挡在门外？ ──────────────
    #
    # 门控省钱的代价是「不送 LLM 的船永远得不到第二眼」。如果真事故船恰好
    # 都属于「L1 报了事件」那一类，新门控就会把它们全部拒绝在 LLM 之外 ——
    # 省钱省在了最不该省的地方。这一步就是在量这件事。
    samples = json.loads((L / "hawaii_evalset.json").read_text(encoding="utf-8"))["samples"]
    ev_by: dict[tuple[str, str], int] = {}
    for p in sorted(CACHE.glob("*.json")):
        day = p.stem
        per: dict[str, int] = defaultdict(int)
        for e in json.loads(p.read_text(encoding="utf-8"))["rows"]:
            per[str(e["mmsi"])] += 1
        for m, c in per.items():
            ev_by[(day, m)] = c

    pos_pairs = {(Path(s["csv"]).stem, str(s["mmsi"]))
                 for s in samples if s["label"]}
    pos_zero = sum(1 for k in pos_pairs if ev_by.get(k, 0) == 0)
    print("\n" + "=" * 76)
    print("召回风险检查：真事故船-日会不会被新门控挡在门外")
    print("-" * 76)
    print(f"  事故船-日（有锚点正样本的）: {len(pos_pairs)} 条")
    print(f"  其中 L1 一条事件都没报的   : {pos_zero} 条 "
          f"（{pos_zero/max(1,len(pos_pairs))*100:.1f}%）")
    print(f"  → 新门控只把这 {pos_zero/max(1,len(pos_pairs))*100:.1f}% 的事故船交给 LLM，")
    print(f"     其余 {100-pos_zero/max(1,len(pos_pairs))*100:.1f}% 直接按 L1 的结论走。")
    print("  ⚠ 这是新门控的固有代价：它假设「L1 报了事件 = 规则层已经有把握」，")
    print("     而在真实数据上 L1 的召回只有 29%——报了事件不等于报对了。")

    # ── 并集门控：把两种判据合起来，看能不能既省钱又不丢召回 ────────
    #
    # 新门控（无事件才调）在注入集上完胜，但在真实数据上只覆盖 5.5% 的事故船。
    # 旧门控（风险分）覆盖得多但贵。取**并集**（任一命中就送）理论上能同时
    # 保住召回与成本——这里用已有的级联产物（41 天，含 n_events 与 probs）量一下。
    TAU = 0.000880
    prod = L / "cascade_prod_newl2"
    days = {p.stem for p in prod.glob("*.json")} if prod.exists() else set()
    rows = []
    for p in sorted(prod.glob("*.json")):
        for r in json.loads(p.read_text(encoding="utf-8")):
            pr = r.get("probs") or {}
            risk = max([float(pr.get(k, 0.0) or 0.0)
                        for k in ("has_risk", "obvious_issue", "needs_review")] + [0.0])
            rows.append({"day": p.stem, "mmsi": str(r["mmsi"]),
                         "n_ev": int(r.get("n_events") or 0), "risk": risk})
    if rows:
        n2 = len(rows)
        g_new = sum(1 for r in rows if r["n_ev"] == 0)
        g_old = sum(1 for r in rows if r["risk"] >= TAU)
        g_union = sum(1 for r in rows if r["n_ev"] == 0 or r["risk"] >= TAU)
        g_inter = sum(1 for r in rows if r["n_ev"] == 0 and r["risk"] >= TAU)
        pos2 = {(Path(s["csv"]).stem, str(s["mmsi"]))
                for s in samples if s["label"] and Path(s["csv"]).stem in days}
        print("\n" + "=" * 76)
        print(f"并集门控（真实产物，{len(days)} 天 / {n2} 船-日；"
              f"其中事故船-日 {len(pos2)} 条）")
        print("-" * 76)
        print(f"{'门控判据':<34}{'调用率':>10}{'覆盖事故船-日':>16}")
        for tag, pred in (("新：无候选事件", lambda r: r["n_ev"] == 0),
                          ("旧：风险分 ≥ 0.00088", lambda r: r["risk"] >= TAU),
                          ("并集：任一命中", lambda r: r["n_ev"] == 0 or r["risk"] >= TAU),
                          ("交集：都命中", lambda r: r["n_ev"] == 0 and r["risk"] >= TAU)):
            calls = sum(1 for r in rows if pred(r))
            hit = sum(1 for r in rows
                      if pred(r) and (r["day"], r["mmsi"]) in pos2)
            print(f"{tag:<34}{calls/n2*100:>9.1f}%"
                  f"{hit/max(1,len(pos2))*100:>15.1f}%")
        (L / "paper" / "gate_union.json").write_text(json.dumps({
            "days": len(days), "n_ship_days": n2, "n_pos": len(pos2),
            "call_rate_new": g_new / n2, "call_rate_old": g_old / n2,
            "call_rate_union": g_union / n2, "call_rate_inter": g_inter / n2,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    (L / "paper" / "gate_hawaii.json").write_text(json.dumps({
        "n_ship_days": n, "zero_event_rate": zero / n,
        "event_count_median": statistics.median(counts),
        "event_count_mean": statistics.mean(counts),
        "incident_ship_days": len(pos_pairs),
        "incident_zero_event": pos_zero,
        "incident_zero_event_rate": pos_zero / max(1, len(pos_pairs)),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {L / 'paper' / 'gate_hawaii.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
