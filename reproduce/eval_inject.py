# -*- coding: utf-8 -*-
"""把被测系统（L1）跑在**注入集**上，按船对真值算指标。

为什么只能用注入集：真实 AIS 没有条款级真值。HawaiiCoast_GT 给的是"哪条船、
哪个时间窗异常、证据强弱"，没有"谁该让路、实际最近会遇距离多少、是否达到
紧迫局面"。规则级评测只能靠注入已知参数的场景（见 inject_violations.py）。

两层指标，对应两个不同的对比对象：
  1. **船级二分类**（该船是否是被注入的肇事船）——文献基线（LOF/DBSCAN/
     iForest/VAE/LSTM-AE）输出的是"这条轨迹异常"，只有这一层能和它们比。
  2. **逐规范码**的 precision/recall——基线根本给不出"违反的是哪条规则"，
     这是我们多类能力的贡献点。

判对的含义写死在这里，别在结果里含糊：
  · 船级正 = 合成船被判出至少一条**会遇链**码（give_way_failure /
    overtaking_violation）。单船类码（超速 / AIS 关停）与注入场景无关，不进
    这个判据——否则一条与场景无关的超速会把合规样本误算成命中。
  · 逐码正 = 合成船被判出**该码**。
  · 只评合成船（999 段）。宿主是真船、不是肇事方，我们的系统本来就把违规
    归责给让路船，用宿主算召回会凭空多出一半样本。

用法：
  python benchmark/eval_inject.py --gt data/logs/inject_v1.gt.json
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

CHAINS = ("give_way_failure", "overtaking_violation")


def predict(source: str, max_ships: int | None):
    """跑一次被测系统，返回 船 → {码} 与 船 → 码 → {目标船}。"""
    pipe = AcceleratorPipeline(config=AcceleratorConfig())
    t0 = time.time()
    tracks, metrics = pipe.annotate_file(source, max_ships=max_ships)
    print(f"[L1] {metrics.ship_count} 船 / {metrics.valid_rows:,} 行  "
          f"{time.time() - t0:.1f}s", flush=True)

    codes: dict[str, set[str]] = defaultdict(set)
    targets: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for t in tracks:
        for ev in (getattr(t, "events", None) or []):
            ctype = getattr(getattr(ev, "candidate_type", None), "value", "")
            if not ctype:
                continue
            own = str(getattr(ev, "mmsi", None) or t.mmsi)
            feats = getattr(ev, "features", None) or {}
            tgt = str(feats.get("target_mmsi") or feats.get("target") or "")
            codes[own].add(ctype)
            if tgt:
                targets[own][ctype].add(tgt)
    return codes, targets, metrics


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if (tp + fp) else float("nan")
    r = tp / (tp + fn) if (tp + fn) else float("nan")
    f = 2 * p * r / (p + r) if (p + r) and p == p and r == r and (p + r) > 0 else float("nan")
    return p, r, f


def pct(x: float) -> str:
    return "  n/a" if x != x else f"{x * 100:5.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gt", default="data/logs/inject_v1.gt.json")
    ap.add_argument("--source", default=None, help="默认按 gt 里的 out 前缀推 .csv")
    ap.add_argument("--max-ships", type=int, default=None)
    ap.add_argument("--out", default="data/logs/eval_inject.json")
    args = ap.parse_args()

    gt_path = ROOT / args.gt
    gt = json.loads(gt_path.read_text(encoding="utf-8"))
    if args.source:
        source = Path(args.source)
    else:
        # 生成器把 inject_v1 写成 inject_v1.csv + inject_v1.gt.json 一对，
        # 从真值路径推轨迹路径时要把 .gt.json 整个后缀去掉，只去掉一层会
        # 得到 inject_v1.gt.csv（不存在）。
        cands = [Path(str(gt_path).replace(".gt.json", ".csv")),
                 gt_path.with_suffix(".csv")]
        source = next((p for p in cands if p.exists()), None)
        if source is None:
            print(f"找不到与 {gt_path} 配对的轨迹文件，试过：{[str(c) for c in cands]}")
            return 2
    print(f"注入集 {source}")
    print(f"真值 {gt_path}（{gt['n_scenarios']} 条场景，seed={gt['seed']}）")

    truth = {s["own_mmsi"]: s for s in gt["scenarios"]}
    codes, targets, _metrics = predict(str(source), args.max_ships)

    # ── 1. 船级二分类 ──────────────────────────────────────────────
    tp = fp = fn = tn = 0
    misses: list[dict] = []
    false_alarms: list[dict] = []
    for mmsi, s in truth.items():
        want_pos = s["code"] is not None
        got_pos = bool(codes.get(mmsi, set()) & set(CHAINS))
        if want_pos and got_pos:
            tp += 1
        elif want_pos and not got_pos:
            fn += 1
            misses.append(s)
        elif not want_pos and got_pos:
            fp += 1
            false_alarms.append({**s, "pred": sorted(codes.get(mmsi, set()))})
        else:
            tn += 1
    p, r, f = prf(tp, fp, fn)
    print("\n=== 船级二分类（与文献基线可比的那一层）===")
    print(f"  真值 正 {tp + fn:3d} / 负 {tn + fp:3d}（只算合成船，宿主不计）")
    print(f"  TP {tp:3d}  FP {fp:3d}  FN {fn:3d}  TN {tn:3d}")
    print(f"  precision {pct(p)}  recall {pct(r)}  F1 {pct(f)}")

    # ── 2. 逐规范码 ────────────────────────────────────────────────
    print("\n=== 逐规范码（基线给不出这一层）===")
    per_code = {}
    for code in CHAINS:
        c_tp = sum(1 for m, s in truth.items() if s["code"] == code and code in codes.get(m, set()))
        c_fn = sum(1 for m, s in truth.items() if s["code"] == code and code not in codes.get(m, set()))
        c_fp = sum(1 for m, s in truth.items() if s["code"] != code and code in codes.get(m, set()))
        cp, cr, cf = prf(c_tp, c_fp, c_fn)
        n_truth = sum(1 for s in truth.values() if s["code"] == code)
        per_code[code] = {"tp": c_tp, "fp": c_fp, "fn": c_fn,
                          "precision": cp, "recall": cr, "f1": cf}
        print(f"  {code:<22} 真值 {n_truth:3d}  "
              f"P {pct(cp)}  R {pct(cr)}  F1 {pct(cf)}   "
              f"(误报 {c_fp}, 漏报 {c_fn})")

    # ── 3. 目标船有没有指对人 ──────────────────────────────────────
    # 归责对了但目标船认错，说明几何定位有问题，和"漏检"是两回事。
    wrong_target = []
    for mmsi, s in truth.items():
        if s["code"] and s["code"] in codes.get(mmsi, set()):
            got = targets.get(mmsi, {}).get(s["code"], set())
            if got and s["target_mmsi"] not in got:
                wrong_target.append((mmsi, s["target_mmsi"], sorted(got)))
    print(f"\n=== 目标船 ===\n  指错目标 {len(wrong_target)} 条")
    for mmsi, want, got in wrong_target[:5]:
        print(f"     {mmsi} 设计目标 {want} → 实测指认 {got}")

    # ── 4. 漏报 / 误报明细（下一轮"调好为止"的入口）──────────────
    if misses:
        print(f"\n=== 漏报 {len(misses)} 条 ===")
        for s in misses[:15]:
            a = s["achieved"]
            print(f"  {s['own_mmsi']} → {s['target_mmsi']}  {s['kind']:<20} "
                  f"min_d={a['min_dist_nm']}  tier={a.get('tier')}  "
                  f"fired_ref={a['fired']}  alter={s['params']['alter']}")
    if false_alarms:
        print(f"\n=== 误报 {len(false_alarms)} 条 ===")
        for s in false_alarms[:15]:
            a = s["achieved"]
            print(f"  {s['own_mmsi']}  {s['kind']:<20} min_d={a['min_dist_nm']}  "
                  f"tier={a.get('tier')}  实测报出 {s['pred']}  alter={s['params']['alter']}")

    Path(ROOT / args.out).write_text(json.dumps({
        "source": str(source), "gt": args.gt,
        "binary": {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
                   "precision": p, "recall": r, "f1": f},
        "per_code": per_code,
        "wrong_target": wrong_target,
        "misses": [s["own_mmsi"] for s in misses],
        "false_alarms": [s["own_mmsi"] for s in false_alarms],
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
