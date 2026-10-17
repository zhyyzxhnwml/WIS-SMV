# -*- coding: utf-8 -*-
"""把被测系统（ais_accelerator 的 L1）与独立参照实现放在**同一份数据**上对拍。

产出一致率与**分歧清单**——分歧才是要研究的东西：
  · 被测系统报了参照没报  → 疑似误报，逐条看是门槛问题还是几何问题
  · 参照报了被测没报      → 疑似漏报

两侧共用同一组**工程约定**（采样窗/过程切分/半径/门槛，见 colreg_ref.CFG），
所以一致率测的是**规则实现与几何是否正确**，不是约定选择；
约定自身的敏感度另做扫描。不对齐约定则差异全来自脚手架，
实测交集直接归零——那种结果没有信息量。

用法：
  python benchmark/compare_colreg.py --source data/input/_fzais1009_converted.csv --max-ships 200
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

_HERE = Path(__file__).resolve()
ROOT = next(p for p in _HERE.parents if (p / "run.py").exists())
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ais_accelerator.config import AcceleratorConfig         # noqa: E402
from ais_accelerator.pipeline import AcceleratorPipeline     # noqa: E402
from benchmark.reference.colreg_ref import (                 # noqa: E402
    CFG, load_tracks, scan_encounters,
)

# 只对拍这两个码：它们是本数据上**能构成硬证据**的多船行为类违规，
# 且几何判据在参照侧可完全独立重算。单船类（超速/AIS 关停）不涉及会遇几何，
# 另做（见 report 里对 SPEED_VIOLATION 的说明）。
COMPARED = ("give_way_failure", "overtaking_violation")


def l1_side(source: str, max_ships: int | None):
    """跑被测系统，返回 {(让路船, 目标船): 码集合} 与逐船事件数。"""
    pipe = AcceleratorPipeline(config=AcceleratorConfig())
    t0 = time.time()
    tracks, metrics = pipe.annotate_file(source, max_ships=max_ships)
    print(f"  [L1] {metrics.ship_count} 船 / {metrics.valid_rows:,} 行  "
          f"{time.time()-t0:.1f}s", flush=True)

    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    per_ship = Counter()
    for t in tracks:
        for ev in (getattr(t, "events", None) or []):
            ctype = getattr(getattr(ev, "candidate_type", None), "value", "")
            code = {"give_way_failure": "give_way_failure",
                    "overtaking_violation": "overtaking_violation"}.get(ctype)
            if code not in COMPARED:
                continue
            feats = getattr(ev, "features", None) or {}
            tgt = str(feats.get("target_mmsi") or feats.get("target") or "?")
            own = str(getattr(ev, "mmsi", None) or t.mmsi)
            pairs[(own, tgt)].add(code)
            per_ship[own] += 1
    return pairs, per_ship, metrics


def ref_side(source: str, max_ships: int | None):
    """跑独立参照实现，返回 {(让路船, 目标船): 码集合}。"""
    t0 = time.time()
    tr = load_tracks(source, max_ships=max_ships)
    print(f"  [参照] {len(tr)} 船 / {sum(len(v) for v in tr.values()):,} 点  "
          f"{time.time()-t0:.1f}s", flush=True)
    t0 = time.time()
    codes = scan_encounters(tr)
    print(f"  [参照] 判出 {len(codes)} 条  {time.time()-t0:.1f}s", flush=True)

    pairs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for own, tgt, code in codes:
        pairs[(own, tgt)].add(code)
    return pairs, codes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/input/_fzais1009_converted.csv")
    ap.add_argument("--max-ships", type=int, default=None)
    ap.add_argument("--out", default="data/logs/colreg_agreement.json")
    args = ap.parse_args()

    print(f"对拍 {args.source}（max_ships={args.max_ships}）")
    l1, l1_per_ship, metrics = l1_side(args.source, args.max_ships)
    ref, encounters = ref_side(args.source, args.max_ships)

    for code in COMPARED:
        a = {k for k, v in l1.items() if code in v}
        b = {k for k, v in ref.items() if code in v}
        both, only_a, only_b = a & b, a - b, b - a
        print(f"\n=== {code} ===")
        print(f"  被测(L1) {len(a):4d} 对   参照 {len(b):4d} 对   "
              f"交集 {len(both):4d}")
        if a or b:
            print(f"  一致率(交集/并集, Jaccard) = {len(both)/max(len(a|b),1)*100:.1f}%")
        print(f"  仅被测报（疑似误报）{len(only_a)}   仅参照报（疑似漏报）{len(only_b)}")

        # 不分朝向再算一次。两种分歧的**诊断完全不同**：
        #   方向无关后对上   → 两侧都发现了这个局面，只是让路角色判反了（几何问题）
        #   方向无关后仍对不上 → 一边根本没看见这次会遇（门槛/扫描问题）
        # 混在一起看会把后者漏掉，因为"角色判反"在一多一少里长得一模一样。
        def undirected(pairs_set):
            return {frozenset(k) for k in pairs_set}
        ua, ub = undirected(a), undirected(b)
        role_swap = (ua & ub)
        print(f"  不分朝向：交集 {len(role_swap)}  "
              f"仅被测 {len(ua-ub)}  仅参照 {len(ub-ua)}")
        swapped = role_swap - {frozenset(k) for k in both}
        if swapped:
            print(f"  其中**角色判反** {len(swapped)} 对：")
            for fs in list(swapped)[:5]:
                pair = tuple(fs)
                la = pair if pair in a else pair[::-1]
                lb = pair if pair in b else pair[::-1]
                print(f"     被测认为 {la[0]} 让路 / 参照认为 {lb[0]} 让路   ({pair[0]}×{pair[1]})")

        for k in list(only_a)[:5]:
            print(f"     仅被测: {k[0]} -> {k[1]}")
        for k in list(only_b)[:5]:
            print(f"     仅参照: {k[0]} -> {k[1]}")

    # 逐船：被测报了几个多船违规
    print(f"\n逐船多船违规数：有 {sum(1 for v in l1_per_ship.values() if v)} / "
          f"{metrics.ship_count} 条船至少 1 个")
    print("  每船个数分布:", Counter(l1_per_ship.values()).most_common(6))

    payload = {
        "source": args.source,
        "max_ships": args.max_ships,
        "l1_pairs": {f"{k[0]}|{k[1]}": sorted(v) for k, v in l1.items()},
        "ref_pairs": {f"{k[0]}|{k[1]}": sorted(v) for k, v in ref.items()},
        "thresholds": CFG,
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
