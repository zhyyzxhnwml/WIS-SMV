# -*- coding: utf-8 -*-
"""门槛敏感度扫描（注入集版）：动**被测系统**的门槛，看注入集上的 F1 怎么变。

【为什么必须做这一步】
  eval_inject 跑出 P/R/F1 = 100%。但注入场景是我自己按参照实现造的，参照实现
  和被测实现同一份规则常数——100% 可能只说明"两边用了同一组常数"，不说明
  这套判据真的在判东西。检验办法只有一个：让被测侧**偏离**自己的取值。

  若把紧迫局面门槛从 0.2 拉到 0.05 或 1.0、F1 仍然 100%，那这份注入集是空的，
  前面的 100% 不能写进论文。若 F1 在偏离时**掉下来**，说明样本确实堆在门槛
  附近、这份集子在测标定。

  另外两路信息：
    · 掉下来的**形状**给出每个门槛的真实工作区间——比"F1 100%"信息量大得多。
    · overtaking_speed_diff_knots 是**负对照**：注入的追越场景速度差实测
      3.5~21 节，远在 1 节门槛之上，动这个旋钮 F1 应当纹丝不动。它若也动了，
      说明评测管线里有别的耦合，得先查管线。

【可续跑】结果逐条落盘到 --out，重跑时已完成的行直接跳过，跳过前会核对
  source/gt 指纹。机器随时可能关机，任何一步中断都不该让前面的扫描白跑。

用法：
  python benchmark/sweep_inject.py --source data/logs/inject_v1.csv \
      --gt data/logs/inject_v1.gt.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from ais_accelerator.config import AcceleratorConfig         # noqa: E402
from ais_accelerator.pipeline import AcceleratorPipeline     # noqa: E402

# 基线之外的取值。每个列表都显式包含被测的默认值（由 --baseline 打印标出），
# 否则看不出"偏离"是相对谁。
SWEEPS: dict[str, list[float]] = {
    "give_way_close_quarters_nm": [0.05, 0.10, 0.20, 0.30, 0.50, 1.00],
    "give_way_alteration_degrees": [5.0, 15.0, 30.0, 45.0, 90.0],
    "give_way_speed_drop_knots": [0.5, 1.0, 2.0, 3.0, 5.0],
    "overtaking_unsafe_distance_nm": [0.02, 0.05, 0.10, 0.20, 0.50],
    # 负对照：注入场景的实测速度差远在门槛之上，动它应当没有任何影响。
    "overtaking_speed_diff_knots": [0.2, 0.5, 1.0, 2.0, 5.0],
}

CHAINS = ("give_way_failure", "overtaking_violation")


def _fingerprint(path: Path) -> str:
    h = hashlib.sha256()
    h.update(str(path.stat().st_size).encode())
    h.update(path.read_bytes()[:1 << 20])
    return h.hexdigest()[:16]


def _predict(source: str, overrides: dict[str, float]) -> dict[str, set[str]]:
    """跑一次被测系统（L1），只拿"船 → 码集合"。"""
    cfg = AcceleratorConfig(**overrides)
    pipe = AcceleratorPipeline(config=cfg)
    tracks, _metrics = pipe.annotate_file(source)
    codes: dict[str, set[str]] = defaultdict(set)
    for t in tracks:
        for ev in (getattr(t, "events", None) or []):
            ctype = getattr(getattr(ev, "candidate_type", None), "value", "")
            if ctype:
                codes[str(getattr(ev, "mmsi", None) or t.mmsi)].add(ctype)
    return codes


def _score(truth: dict, codes: dict[str, set[str]]) -> dict:
    """船级二分类 + 逐码，口径与 eval_inject.py 一致（见那边的 docstring）。"""
    tp = fp = fn = tn = 0
    for mmsi, s in truth.items():
        want = s["code"] is not None
        got = bool(codes.get(mmsi, set()) & set(CHAINS))
        tp += want and got
        fn += want and not got
        fp += (not want) and got
        tn += (not want) and not got

    def prf(a: int, b: int, c: int) -> tuple[float, float, float]:
        p = a / (a + b) if (a + b) else float("nan")
        r = a / (a + c) if (a + c) else float("nan")
        f = 2 * p * r / (p + r) if (p == p and r == r and p + r > 0) else float("nan")
        return p, r, f

    out = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
    p, r, f = prf(tp, fp, fn)
    out["precision"], out["recall"], out["f1"] = p, r, f
    out["per_code"] = {}
    for code in CHAINS:
        ctp = sum(1 for m, s in truth.items() if s["code"] == code and code in codes.get(m, set()))
        cfn = sum(1 for m, s in truth.items() if s["code"] == code and code not in codes.get(m, set()))
        cfp = sum(1 for m, s in truth.items() if s["code"] != code and code in codes.get(m, set()))
        cp, cr, cf = prf(ctp, cfp, cfn)
        out["per_code"][code] = {"tp": ctp, "fp": cfp, "fn": cfn,
                                 "precision": cp, "recall": cr, "f1": cf}
    return out


def _pct(x: float) -> str:
    return " n/a " if x != x else f"{x * 100:5.1f}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="data/logs/inject_v1.csv")
    ap.add_argument("--gt", default="data/logs/inject_v1.gt.json")
    ap.add_argument("--out", default="data/logs/sweep_inject.json")
    args = ap.parse_args()

    source = ROOT / args.source
    gt_path = ROOT / args.gt
    out_path = ROOT / args.out
    git = json.loads(gt_path.read_text(encoding="utf-8"))
    truth = {s["own_mmsi"]: s for s in git["scenarios"]}
    fp_src, fp_gt = _fingerprint(source), _fingerprint(gt_path)
    print(f"注入集 {source}\n真值 {gt_path}（{git['n_scenarios']} 条）")
    print(f"指纹 source={fp_src} gt={fp_gt}\n", flush=True)

    # ── 断点续跑：指纹对得上才认旧结果 ──────────────────────────────
    done: dict[str, dict] = {}
    if out_path.exists():
        try:
            old = json.loads(out_path.read_text(encoding="utf-8"))
            if old.get("fingerprint") == {"source": fp_src, "gt": fp_gt}:
                done = old.get("results", {})
                print(f"续跑：已有 {len(done)} 行结果，跳过\n", flush=True)
            else:
                print("旧结果指纹不符（换过数据集），从零开始\n", flush=True)
        except (json.JSONDecodeError, KeyError):
            print("旧结果读不出来，从零开始\n", flush=True)

    base = AcceleratorConfig()
    baseline = {k: getattr(base, k) for k in SWEEPS}
    print("被测侧默认取值：" + "  ".join(f"{k}={v:g}" for k, v in baseline.items()) + "\n")

    def flush() -> None:
        out_path.write_text(json.dumps({
            "source": args.source, "gt": args.gt,
            "fingerprint": {"source": fp_src, "gt": fp_gt},
            "baseline": baseline, "sweeps": SWEEPS, "results": done,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    for field, values in SWEEPS.items():
        print(f"── {field} ──", flush=True)
        for v in values:
            key = f"{field}={v:g}"
            if key in done:
                row = done[key]
                print(f"  {key:<44} F1 {_pct(row['f1'])}%  （已有，跳过）", flush=True)
                continue
            t0 = time.time()
            codes = _predict(str(source), {field: v})
            row = _score(truth, codes)
            row["seconds"] = round(time.time() - t0, 1)
            done[key] = row
            flush()  # 每条都落盘：中途断电，已完成的行不会白跑
            star = " *" if abs(v - baseline[field]) < 1e-9 else "  "
            marks = "  ".join(f"{c.split('_')[0]}={_pct(row['per_code'][c]['f1'])}"
                              for c in CHAINS)
            print(f"  {key:<44}{star} P {_pct(row['precision'])}%  "
                  f"R {_pct(row['recall'])}%  F1 {_pct(row['f1'])}%   "
                  f"TP{row['tp']:3d} FP{row['fp']:3d} FN{row['fn']:3d} TN{row['tn']:3d}   "
                  f"{marks}   ({row['seconds']}s)", flush=True)
        print(flush=True)

    # ── 收尾读法 ───────────────────────────────────────────────────
    print("=" * 78)
    print("读法：标 * 的是被测自身的取值。F1 若在偏离时纹丝不动，这份注入集没测到东西；")
    print("      掉下来的位置给出该门槛的真实工作区间。负对照应当全程 100%。")
    for field in SWEEPS:
        rows = [(v, done[f"{field}={v:g}"]["f1"]) for v in SWEEPS[field]
                if f"{field}={v:g}" in done]
        if len(rows) < 2:
            continue
        vals = [f for _v, f in rows if f == f]
        span = (max(vals) - min(vals)) * 100 if vals else float("nan")
        print(f"\n{field}  被测={baseline[field]:g}  极差 {span:.1f} 个百分点")
        for v, f in rows:
            star = " *" if abs(v - baseline[field]) < 1e-9 else "  "
            print(f"  {v:<8g}{star} F1 {_pct(f)}%  {'#' * int((f * 100 if f == f else 0) / 4)}")
    print(f"\n明细 → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
