# -*- coding: utf-8 -*-
"""级联的成本-质量帕累托前沿（注入集）。

【怎么做到"不额外调 LLM 就能画出整条曲线"】
  见 llm_arms.py 的 F 臂：它把**每一条船**都送进 LLM（标注轨迹提示），
  所以 F 的缓存里有"LLM 对每条船的独立意见"。
  级联在门控阈值 τ 下的行为可以完全由 F 复现：
      · 风险分 ≥ τ 的船 → 用 LLM 的意见（这才是要花钱的那部分）
      · 其余船          → 用免费的那一级（L1 规则 / L2 门控）的意见
  于是**扫一遍 τ** 就得到 (LLM 调用次数, F1) 整条曲线，
  而每次 LLM 判决的身份没变（同一模型、同一提示、temperature=0），
  不存在"不同工作点用不同质量的意见"这种作弊。

【成本轴】
  · LLM 调用次数（主口径，与 FrugalGPT 的 call budget 同义）
  · prompt token（从 F 缓存里逐条累加，**实测**而非估算）

【对照点】
  · 纯 LLM：τ=0（每条船都调）
  · 纯 L1 / 纯 L2：τ=∞（一次都不调）
  · 文献 8 个基线在同一评测集上的 oracle F1 与 1%FPR 召回（写死在下方常量里，
    由 benchmark/baselines.py 的产物更新）

用法：
  python experiments/paper/cascade_pareto.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAPER = ROOT / "data/logs/paper"
GT_PATH = ROOT / "data/logs/inject_v1.gt.json"
CHAINS = ("give_way_failure", "overtaking_violation")


def load_cache(arm: str) -> dict[str, dict]:
    p = PAPER / "llm_cache" / f"{arm}.jsonl"
    out: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    out[str(r["mmsi"])] = r
                except json.JSONDecodeError:
                    continue
    return out


def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def main() -> int:
    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scen = {str(s["own_mmsi"]): s for s in gt["scenarios"]}
    l1 = json.loads((PAPER / "l1_inject.json").read_text(encoding="utf-8"))
    llm = load_cache("F")          # 每条船的 LLM 独立意见
    if not llm:
        print("缺 F 臂缓存（experiments/paper/llm_arms.py --arms F）")
        return 1

    # L2 风险分
    from triage_engine.model import TriageModel
    model = TriageModel(model_path=str(ROOT / "triage_model.txt"))
    risk: dict[str, float] = {}
    for m in scen:
        fv = (l1.get(m) or {}).get("feature_vector") or []
        try:
            cat, conf, probs = model.predict_single(list(fv))
            risk[m] = max(float(probs.get(k, 0.0) or 0.0) for k in
                          ("has_risk", "obvious_issue", "needs_review"))
        except Exception:                                            # noqa: BLE001
            risk[m] = 0.0

    l1_hit = {m: bool({e["type"] for e in (l1.get(m) or {}).get("events") or []}
                      & set(CHAINS)) for m in scen}
    llm_hit = {m: bool((llm.get(m) or {}).get("violation_detected")) for m in scen}
    tok = {m: int((llm.get(m) or {}).get("prompt_tokens") or 0) for m in scen}
    covered = [m for m in scen if m in llm]

    y = {m: (scen[m]["code"] is not None) for m in scen}
    npos = sum(1 for m in scen if y[m])
    print(f"注入集 {len(scen)} 船（正 {npos}）  F 臂覆盖 {len(covered)} 船")
    if len(covered) < len(scen):
        print(f"⚠ 还有 {len(scen)-len(covered)} 条没有 LLM 判决 —— 曲线只在覆盖集上算")
    ms = covered

    # ── 网格：门控阈值 τ 扫一遍 ───────────────────────────────────
    taus = [float("inf"), 0.999, 0.99, 0.95, 0.9, 0.8, 0.7, 0.5, 0.3, 0.2, 0.1,
            0.05, 0.02, 0.01, 0.005, 0.001, 0.0]
    rows = []
    for tau in taus:
        calls = 0
        ptok = 0
        pred: dict[str, bool] = {}
        for m in ms:
            gated = risk.get(m, 0.0) >= tau
            pred[m] = llm_hit[m] if gated else l1_hit[m]
            if gated:
                calls += 1
                ptok += tok[m]
        tp = sum(1 for m in ms if y[m] and pred[m])
        fp = sum(1 for m in ms if not y[m] and pred[m])
        fn = sum(1 for m in ms if y[m] and not pred[m])
        p, r, f = prf(tp, fp, fn)
        rows.append({"tau": tau, "calls": calls,
                     "call_rate": calls / max(1, len(ms)),
                     "prompt_tokens": ptok, "precision": p, "recall": r, "f1": f,
                     "tp": tp, "fp": fp, "fn": fn})

    print("\n" + "=" * 92)
    print("级联成本-质量前沿（门控阈值 τ 扫描；未过闸门 → 用 L1 规则兜底）")
    print("-" * 92)
    print(f"{'τ':>8}{'LLM调用':>9}{'调用率':>8}{'prompt tok':>12}"
          f"{'P':>8}{'R':>8}{'F1':>8}")
    for r in rows:
        t = "∞(纯L1)" if r["tau"] == float("inf") else f"{r['tau']:.3f}"
        print(f"{t:>8}{r['calls']:>9}{r['call_rate']*100:>7.1f}%"
              f"{r['prompt_tokens']:>12,}{r['precision']*100:>7.1f}%"
              f"{r['recall']*100:>7.1f}%{r['f1']*100:>7.1f}%")

    # ── 另外两个免费端点：纯 L2、纯 LLM ───────────────────────────
    l2_pred = {m: risk.get(m, 0.0) >= 0.000880 for m in ms}
    tp = sum(1 for m in ms if y[m] and l2_pred[m])
    fp = sum(1 for m in ms if not y[m] and l2_pred[m])
    fn = sum(1 for m in ms if y[m] and not l2_pred[m])
    p2, r2, f2 = prf(tp, fp, fn)
    print(f"{'纯L2门控':>8}{0:>9}{0.0:>7.1f}%{0:>12,}{p2*100:>7.1f}%{r2*100:>7.1f}%{f2*100:>7.1f}%")
    tp = sum(1 for m in ms if y[m] and llm_hit[m])
    fp = sum(1 for m in ms if not y[m] and llm_hit[m])
    fn = sum(1 for m in ms if y[m] and not llm_hit[m])
    p3, r3, f3 = prf(tp, fp, fn)
    print(f"{'纯LLM':>8}{len(ms):>9}{100.0:>7.1f}%{sum(tok.values()):>12,}"
          f"{p3*100:>7.1f}%{r3*100:>7.1f}%{f3*100:>7.1f}%")

    out = {"n_ships": len(ms), "n_pos": sum(1 for m in ms if y[m]),
           "curve": rows,
           "l2_only": {"precision": p2, "recall": r2, "f1": f2},
           "llm_only": {"precision": p3, "recall": r3, "f1": f3,
                        "calls": len(ms), "prompt_tokens": sum(tok.values())}}
    (PAPER / "cascade_pareto.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {PAPER / 'cascade_pareto.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
