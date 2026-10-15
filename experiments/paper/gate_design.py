# -*- coding: utf-8 -*-
"""门控策略对比：把「谁该送大模型」这件事重做一遍。

【为什么做这个】
  现役门控读的是 L2 的 risk_score（非正常三类概率的最大值）。实测它的排序能力
  在违规任务上是**反相关**的（AUC 0.244 / 0.305，低于随机），而且阈值在注入集上
  放行率 100% —— 既没排序能力、也不省钱。

  但「送不送大模型」这件事本来有更好的信号：**L1 自己的证据强度**。
    · 有硬证据（过 PROMOTABLE 门槛）→ 规则层自己敢下结论，不必花钱
    · 有候选事件但无硬证据       → 边缘案例，正是该让大模型看第二眼的地方
    · 没有任何候选事件           → 规则盲区，L1 的能力边界就在这（见规则覆盖消融）
  这个判据不需要新模型、不需要新特征，用 L1 已经产出的东西就能算。

【零额外成本】
  大模型的逐船判决直接复用 F 臂缓存（每条船都有独立意见）。
  换门控只改变「调用哪几条」，不改变任何一条船的判决内容 → 各策略严格可比。

【两个场景】
  (a) 完整规则库：L1 在注入集上 100%，理论上不该调任何大模型
  (b) 摘掉某一类规则：L1 在该类上必然 0%，这时才看得出门控有没有用
      —— 这才是门控真正要回答的问题：「规则不确定时，能不能把该看的挑出来送出去」

用法：
  python experiments/paper/gate_design.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

L = ROOT / "data/logs"
PAPER = L / "paper"
CHAINS = ("give_way_failure", "overtaking_violation")
NN = ("has_risk", "obvious_issue", "needs_review")


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
    from ais_accelerator.violation_catalog import deterministic_violation
    gt = json.loads((L / "inject_v1.gt.json").read_text(encoding="utf-8"))
    scen = {str(s["own_mmsi"]): s for s in gt["scenarios"]}
    l1 = json.loads((PAPER / "l1_inject.json").read_text(encoding="utf-8"))
    llm = load_cache("F")
    if not llm:
        print("缺 F 臂缓存（先跑 experiments/paper/llm_arms.py --arms F）")
        return 1

    from triage_engine.model import TriageModel
    from triage_engine.router import RISK_GATE_THRESHOLD as TAU, TriageRouter
    model = TriageModel(model_path=str(ROOT / "triage_model.txt"))
    router = TriageRouter(model=model)

    mmsis = sorted(scen)
    y = {m: scen[m]["code"] is not None for m in mmsis}
    risk: dict[str, float] = {}
    for m in mmsis:
        fv = (l1.get(m) or {}).get("feature_vector") or []
        try:
            _c, _cf, probs = model.predict_single(list(fv))
            risk[m] = max(float(probs.get(k, 0.0) or 0.0) for k in NN)
        except Exception:                                            # noqa: BLE001
            risk[m] = 0.0

    def evidence(drop: str | None) -> tuple[dict[str, bool], dict[str, bool]]:
        """(有候选事件, 有硬证据)，可选把某一类事件当作不存在。"""
        any_ev, hard = {}, {}
        for m in mmsis:
            evs = [e for e in (l1.get(m) or {}).get("events", [])
                   if e["type"] != drop]
            any_ev[m] = bool(evs)
            hard[m] = bool(evs) and deterministic_violation(
                [{"candidate_type": e["type"], "features": e["features"]}
                 for e in evs]) is not None
        return any_ev, hard

    llm_hit = {m: bool((llm.get(m) or {}).get("violation_detected")) for m in mmsis}

    def score(pred: dict[str, bool], calls: set[str]) -> dict:
        tp = sum(1 for m in mmsis if y[m] and pred[m])
        fp = sum(1 for m in mmsis if not y[m] and pred[m])
        fn = sum(1 for m in mmsis if y[m] and not pred[m])
        p, r, f = prf(tp, fp, fn)
        return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r,
                "f1": f, "calls": len(calls), "rate": len(calls) / len(mmsis)}

    def run(drop: str | None) -> dict:
        any_ev, hard = evidence(drop)
        l1_hit = {m: any_ev[m] and bool(
            {e["type"] for e in (l1.get(m) or {}).get("events", [])
             if e["type"] != drop} & set(CHAINS)) for m in mmsis}
        # 摘掉规则后，该类船的 L1 判断也就不存在了
        if drop:
            for m in mmsis:
                if scen[m]["code"] == drop:
                    l1_hit[m] = False
        out = {}
        out["S0 纯L1（不调）"] = score(l1_hit, set())
        out["S1 现役门控 risk_score"] = score(
            {m: (llm_hit[m] if risk[m] >= TAU else l1_hit[m]) for m in mmsis},
            {m for m in mmsis if risk[m] >= TAU})
        out["S2 有硬证据就不调"] = score(
            {m: (l1_hit[m] if hard[m] else llm_hit[m]) for m in mmsis},
            {m for m in mmsis if not hard[m]})
        out["S3 无候选事件才调"] = score(
            {m: (llm_hit[m] if not any_ev[m] else l1_hit[m]) for m in mmsis},
            {m for m in mmsis if not any_ev[m]})
        out["S7 只有判出会遇违规才不调"] = score(
            {m: (l1_hit[m] if l1_hit[m] else llm_hit[m]) for m in mmsis},
            {m for m in mmsis if not l1_hit[m]})
        out["S5 全送（上界）"] = score(dict(llm_hit), set(mmsis))
        # 现役门控能拿到的最好水平（阈值扫描）
        best = None
        for t in [0.5, 0.3, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.001, 0.000880, 0.0]:
            s = score({m: (llm_hit[m] if risk[m] >= t else l1_hit[m]) for m in mmsis},
                      {m for m in mmsis if risk[m] >= t})
            if best is None or s["f1"] > best[1]["f1"]:
                best = (t, s)
        out[f"S6 现役判据最优阈值 τ={best[0]:g}"] = best[1]
        return out

    # ── 实现一致性校验：走**真实路由器代码**，与仿真逐船对齐 ─────────
    # 这一步防的是「仿真里换了个判据、代码里没换」或反之——两边的数不一样，
    # 论文里就会写着一个代码里根本不存在的策略。
    any_ev_full, _hard_full = evidence(None)
    mismatch = []
    impl_calls = set()
    for m in mmsis:
        fv = (l1.get(m) or {}).get("feature_vector") or []
        n_ev = len((l1.get(m) or {}).get("events") or [])
        d = router.route(feature_vector=fv, mmsi=m, n_events=n_ev)
        want = (n_ev == 0)
        if bool(d.needs_llm) != want:
            mismatch.append(m)
        if d.needs_llm:
            impl_calls.add(m)
    print(f"[实现校验] 真实 router 的 needs_llm 与『n_events==0』逐船一致："
          f"{'通过' if not mismatch else f'不一致 {len(mismatch)} 条 {mismatch[:5]}'}"
          f"（调用 {len(impl_calls)}/{len(mmsis)} = {len(impl_calls)/len(mmsis)*100:.1f}%）")
    # 旧调用方不传 n_events 时必须保持原行为（向后兼容）
    _d_old = router.route(feature_vector=(l1.get(mmsis[0]) or {}).get("feature_vector") or [],
                          mmsi=mmsis[0])
    print(f"[兼容性校验] 不传 n_events 时仍走风险分判据：needs_llm={_d_old.needs_llm}"
          f"（risk_score={_d_old.risk_score:.6f} vs 阈值 {TAU}）")
    print()

    allres: dict[str, dict] = {}
    for tag, drop in (("(a) 完整规则库", None),
                      ("(b) 摘掉 give_way_failure 规则", "give_way_failure"),
                      ("(c) 摘掉 overtaking_violation 规则", "overtaking_violation")):
        print("=" * 100)
        print(f"场景 {tag}")
        print("-" * 100)
        print(f"{'策略':<30}{'LLM调用':>9}{'调用率':>8}{'精确率':>9}{'召回':>8}{'F1':>8}")
        res = run(drop)
        allres[tag] = res
        for k, v in res.items():
            print(f"{k:<30}{v['calls']:>9}{v['rate']*100:>7.1f}%"
                  f"{v['precision']*100:>8.1f}%{v['recall']*100:>7.1f}%{v['f1']*100:>7.1f}%")
        print()

    outp = PAPER / "gate_design.json"
    outp.write_text(json.dumps({
        "n_ships": len(mmsis),
        "impl_check": {"mismatch": len(mismatch),
                       "call_rate": len(impl_calls) / len(mmsis)},
        "scenarios": allres,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"明细 → {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
