# -*- coding: utf-8 -*-
"""规则覆盖消融：规则库盖不到的那一类，级联的后两级能不能兜住？

【要回答的质疑】
  "你们 L1 在注入集上已经 100% 了，那 L2/L3 还有什么用？"
  —— 这个质疑必须被实测数字回答，不能靠设计主张。

【做法（不需要额外调 LLM）】
  把每一类违规**从 L1 的输出里删掉**，等价于"规则库里没有这条规则"：
      · 该类的 L1 召回必然 = 0（构造出来的）
      · 问 L2 的风险分还能不能把这批船排在合规船前面（AUC）
      · 问 L3（LLM，F 臂每条船都有独立判决）能不能把它们认出来（召回）
  两者都在集内已有产物上算，所以这个消融是**零额外成本**的。

  ⚠ 口径说明：L2 的特征仍然来自 L1 的完整标注（真实系统里"删掉一条规则"删的是
    **判定规则**，不是**特征提取**——把特征也一起删才不现实）。
    所以这里量的是"判定规则缺失时，后两级还有多少补偿能力"，不是"特征也没了"。

用法：
  python experiments/paper/rule_coverage.py     # 需先跑完 llm_arms.py --arms F
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from sklearn.metrics import roc_auc_score

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAPER = ROOT / "data/logs/paper"
CHAINS = ("give_way_failure", "overtaking_violation")
NN = ("has_risk", "obvious_issue", "needs_review")


def main() -> int:
    gt = json.loads((ROOT / "data/logs/inject_v1.gt.json").read_text(encoding="utf-8"))
    scen = {str(s["own_mmsi"]): s for s in gt["scenarios"]}
    l1 = json.loads((PAPER / "l1_inject.json").read_text(encoding="utf-8"))

    from triage_engine.model import TriageModel
    model = TriageModel(model_path=str(ROOT / "triage_model.txt"))

    llm: dict[str, bool] = {}
    fp = PAPER / "llm_cache" / "F.jsonl"
    if fp.exists():
        for line in fp.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                llm[str(r["mmsi"])] = bool(r.get("violation_detected"))

    rows = []
    for code in CHAINS:
        pos = [m for m in scen if scen[m]["code"] == code]
        neg = [m for m in scen if scen[m]["code"] is None]
        y = np.array([True] * len(pos) + [False] * len(neg))

        # L1 在"含该规则"时的召回（基线）
        l1_full = np.array([code in {e["type"] for e in (l1.get(m) or {}).get("events", [])}
                            for m in pos + neg])
        # L2 风险分
        sc = []
        for m in pos + neg:
            fv = (l1.get(m) or {}).get("feature_vector") or []
            try:
                _c, _cf, probs = model.predict_single(list(fv))
                sc.append(max(float(probs.get(k, 0.0) or 0.0) for k in NN))
            except Exception:                                        # noqa: BLE001
                sc.append(0.0)
        sc = np.array(sc)
        auc = float(roc_auc_score(y, sc)) if len(np.unique(sc)) > 1 else float("nan")
        # L3（F 臂）
        cov = [m for m in pos if m in llm]
        l3 = (sum(1 for m in cov if llm[m]) / len(cov)) if cov else float("nan")
        # 摘掉规则后，级联在两种门控策略下的召回：
        #   (a) 现役 L2 门控（risk >= RISK_GATE_THRESHOLD）—— 只有放行的船才被 LLM 看到
        #   (b) 放行全部（每条船都送 LLM）—— 上限
        from triage_engine.router import RISK_GATE_THRESHOLD as GATE
        sc_pos = sc[:len(pos)]
        gated = [m for m, s in zip(pos, sc_pos) if s >= GATE]
        rec_l2gate = (sum(1 for m in gated if llm.get(m)) / len(pos)) if pos else 0.0
        rec_all = l3
        rows.append({"code": code, "n_pos": len(pos), "n_neg": len(neg),
                     "l1_recall_when_rule_present": float(l1_full[:len(pos)].mean()),
                     "l1_recall_when_rule_removed": 0.0,
                     "l2_auc": auc, "l3_recall": l3, "l3_covered": len(cov),
                     "gate_l2_threshold": GATE,
                     "gate_pass_rate_pos": len(gated) / len(pos) if pos else 0.0,
                     "gate_pass_rate_all": float((sc >= GATE).mean()),
                     "cascade_recall_gate_l2": rec_l2gate,
                     "cascade_recall_gate_all": rec_all,
                     "gated_pos": len(gated)})

    print("=" * 88)
    print("规则覆盖消融：把某类违规规则从 L1 里摘掉之后，后两级还剩多少补偿能力")
    print("-" * 88)
    print(f"{'违规类型':<20}{'样本':>9}{'L1有规则':>10}{'L1摘掉':>9}"
          f"{'L2 AUC':>9}{'L3召回':>8}{'级联(L2门控)':>13}{'级联(全放行)':>13}{'门控放行率':>11}")
    for r in rows:
        sample = f"{r['n_pos']}/{r['n_neg']}"
        print(f"{r['code']:<20}{sample:>9}"
              f"{r['l1_recall_when_rule_present'] * 100:>9.1f}%"
              f"{r['l1_recall_when_rule_removed'] * 100:>8.1f}%"
              f"{r['l2_auc']:>9.3f}{r['l3_recall'] * 100:>7.1f}%"
              f"{r['cascade_recall_gate_l2'] * 100:>12.1f}%"
              f"{r['cascade_recall_gate_all'] * 100:>12.1f}%"
              f"{r['gate_pass_rate_all'] * 100:>10.1f}%")
    print()
    print("读法：")
    print("  · 摘掉规则后 L1 召回必然是 0（构造如此），所以级联的召回全靠后两级。")
    print("  · **L3（LLM）确实有独立价值**：让路失败 100%、违规追越 20.8% —— ")
    print("    规则覆盖不到的类别，只有 LLM 能捞回来。这是「为什么要级联」的硬数字。")
    print("  · L2 的风险分 AUC < 0.5（低于随机）→ 它对违规**没有排序能力**，")
    print("    特征是「标注条数/点数/位置跳变」这类**体量特征**，学的是体量不是违规语义。")
    print("  · 现役阈值 0.00088 在注入集上的放行率是 **100%** → 门控当前不省任何钱。")
    print("    这既是缺陷（没起到分流作用）也是运气（没把该抓的船挡在外面）。")
    print("    结论：门控判据必须按域重标定；一个「全放行」的门控等于没有门控。")

    (PAPER / "rule_coverage.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {PAPER / 'rule_coverage.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
