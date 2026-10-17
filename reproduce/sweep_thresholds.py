# -*- coding: utf-8 -*-
"""门槛敏感度扫描：参照侧**单方面**改门槛，看与被测的一致率怎么变。

【这个实验要回答的问题】
  对拍跑出 100% 一致。但一致率若对门槛不敏感，它就什么都没测——
  100% 只说明两边用了同一组常数，不说明规则实现正确。
  所以这里让参照侧偏离被测的取值：一致率应当**在偏离时下降**。
  若全程保持 100%，说明这个指标是空的，前面的结论作废。

  另一路信息：下降的**快慢**本身就是被测系统门槛标定的敏感度。
  某个门槛小幅一动一致率就崩，说明该门槛是判定的瓶颈、
  真实数据里大量样本堆在它附近——这比"一致率 100%"有信息量得多。

用法：python benchmark/sweep_thresholds.py --max-ships 150
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from benchmark.reference import colreg_ref as R  # noqa: E402
from reproduce.compare_colreg import COMPARED, l1_side  # noqa: E402

SWEEPS = {
    "giveway_close_quarters_nm": [0.05, 0.10, 0.20, 0.30, 0.50, 1.00],
    "giveway_alter_deg": [5.0, 15.0, 30.0, 45.0, 90.0],
    "giveway_speed_drop_kn": [0.2, 1.0, 2.0, 3.0, 5.0],
    "overtake_unsafe_nm": [0.02, 0.05, 0.10, 0.20, 0.50],
    "overtake_speed_diff_kn": [0.2, 0.5, 1.0, 2.0, 5.0],
}


def jaccard(pred: set, gold: set) -> float:
    u = pred | gold
    return len(pred & gold) / len(u) * 100.0 if u else 100.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/input/_fzais1009_converted.csv")
    ap.add_argument("--max-ships", type=int, default=150)
    args = ap.parse_args()

    print(f"门槛敏感度扫描  {args.source}  max_ships={args.max_ships}\n"
          f"被测侧固定用 AcceleratorConfig 默认值；只动参照侧。\n")

    l1, _, metrics = l1_side(args.source, args.max_ships)
    tr = R.load_tracks(args.source, max_ships=args.max_ships)
    print(f"  参照装载 {len(tr)} 船\n", flush=True)

    agg: dict[str, list[tuple[float, dict[str, float]]]] = defaultdict(list)
    for name, values in SWEEPS.items():
        print(f"── {name} ──")
        for v in values:
            cfg = {**R.CFG, name: v}
            t0 = time.time()
            codes = R.scan_encounters(tr, cfg)
            pred: dict[tuple[str, str], set[str]] = defaultdict(set)
            for own, tgt, c in codes:
                pred[(own, tgt)].add(c)
            row = {}
            for code in COMPARED:
                gold = {k for k, s in l1.items() if code in s}
                p = {k for k, s in pred.items() if code in s}
                row[code] = jaccard(p, gold)
            agg[name].append((v, row))
            marks = "  ".join(f"{c.split('_')[0]}={row[c]:5.1f}%" for c in COMPARED)
            print(f"  {name}={v:<6g}  参照判出 {len(codes):4d} 条   {marks}"
                  f"   ({time.time()-t0:.1f}s)", flush=True)
        print(flush=True)

    print("=" * 74)
    print("读法：一致率应在被测自身取值处（下面标 * 的行）最高，偏离则下降。")
    base = R.CFG
    for name, rows in agg.items():
        own = base.get(name)
        peak = max(rows, key=lambda r: sum(r[1].values()))
        print(f"\n{name}  被测取值={own}  扫描下一致率最高在 {peak[0]}")
        for v, row in rows:
            star = " *" if v == own else "  "
            bar = "#" * int(sum(row.values()) / max(len(COMPARED), 1) / 4)
            print(f"  {v:<8g}{star} {sum(row.values())/len(COMPARED):5.1f}%  {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
