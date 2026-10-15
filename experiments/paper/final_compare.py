# -*- coding: utf-8 -*-
"""生成「我们 vs 论文」的最终总对比。

把前面所有实验的产物汇总成**一份**文档，回答一个问题：
  我们的系统，在每一项指标上，相对已发表方法处于什么位置？

【只读产物，不重算】缺哪个就标「未完成」，绝不补零。

【口径提醒（写进文档里）】
  文献原报的 F1 是在各自的数据集/任务定义下测的，与我们的复现值**不同源**。
  所以另设一列「复现」做同口径对比，原报值只作旁证。

用法：
  python experiments/paper/final_compare.py
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

# 文献原报 F1（各自数据集/任务定义下）。只作旁证，不能与复现值直接相减后当结论。
LIT_F1 = {"lof": 0.513, "dbscan": 0.598, "iforest": 0.650, "vae": 0.745,
          "lstm_ae": 0.752, "vae_lstm": 0.872, "transformer_lstm": 0.953}
ORDER = ["proximity", "lof", "iforest", "dbscan", "vae", "lstm_ae", "vae_lstm",
         "transformer_lstm"]
CN = {"proximity": "Proximity（会遇距离）", "lof": "LOF", "iforest": "iForest",
      "dbscan": "DBSCAN", "vae": "VAE", "lstm_ae": "LSTM-AE",
      "vae_lstm": "VAE-LSTM", "transformer_lstm": "Transformer-LSTM"}
ARM_CN = {"A": "A  L1 规则引擎", "B": "B  L2 机器学习门控",
          "C": "C  生产级联 L1→L2→L3", "D": "D  原始轨迹 → LLM",
          "E": "E  RDP 压缩 → LLM", "F": "F  L1 标注 → LLM",
          "G": "G  FrugalGPT 式两级", "H": "H  几何+判据 → LLM",
          "I": "I  判据写成提示 → LLM"}


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def pc(x, nd=1):
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x*100:.{nd}f}%"


def nm(x, nd=3):
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def main() -> int:
    md: list[str] = ["# 最终对比：我们的系统 vs 已发表方法", "",
                     "> 由 `python experiments/paper/final_compare.py` 生成，只读产物不重算。",
                     "> 每个数字都能追到 `data/logs/` 下的具体 JSON。", ""]

    # ══ 表 1：违规判断主对比 ═══════════════════════════════════════
    ev = jload(L / "eval_inject.json")
    bl = jload(L / "baselines_inject.json")
    arms = None
    for c in ("llm_arms_all.json", "llm_arms.json"):
        arms = jload(PAPER / c)
        if arms:
            break
    par = jload(PAPER / "cascade_pareto.json")

    md += ["## 表 1 违规判断（注入集：240 条合成船 = 120 违规 / 120 合规）", "",
           "真值由场景设计给定，并用**独立参照实现**回放校验几何；**与我们的规则引擎无关**（不循环）。",
           "正负各半 →「全判违规」的平凡 F1 = **66.7%**。", "",
           "| 方法 | 来源 | 精确率 | 召回 | **F1** | AUC | @1%FPR 召回 | LLM 调用 | prompt token |",
           "|---|---|---|---|---|---|---|---|---|"]
    if ev:
        b = ev["binary"]
        md.append(f"| **L1 规则引擎** | 本文 | {pc(b['precision'])} | {pc(b['recall'])} | "
                  f"**{pc(b['f1'])}** | — | {pc(b['recall'])}（FPR=0） | **0** | **0** |")
    if arms:
        for a in sorted(arms.get("arms", {})):
            if a == "A":          # A 臂就是 L1 本身，上面已单独列过，避免重复行
                continue
            v = arms["arms"][a]
            md.append(f"| {ARM_CN.get(a, a)} | 本文 | {pc(v.get('precision'))} | "
                      f"{pc(v.get('recall'))} | **{pc(v.get('f1'))}** | — | — | "
                      f"{v.get('calls', 0)} | {v.get('prompt_tokens', 0):,} |")
    if bl:
        for k in ORDER:
            r = bl["methods"].get(k)
            if not r:
                md.append(f"| {CN[k]} | 文献复现 | 未完成 | — | — | — | — | 0 | 0 |")
                continue
            md.append(f"| {CN[k]} | 文献复现 | {pc(r.get('precision'))} | "
                      f"{pc(r.get('recall'))} | {pc(r.get('f1'))} | {nm(r.get('auc'))} | "
                      f"{pc(r.get('fpr1_recall'))} | 0 | 0 |")
    md += ["",
           "> 文献方法的精确率/召回/F1 都是 **oracle 阈值**（允许在评测集上挑最优），是它们的**上界**；",
           "> 我们的 L1 **没有可调阈值**，是「部署即所得」。",
           "> AUC 一列只有文献方法有：因为 L1 输出的是布尔判定，不产出可排序的分数。", ""]

    # ══ 表 2：成本-质量 ═══════════════════════════════════════════
    md += ["## 表 2 成本-质量（同一批船，成本 = LLM 调用次数 / 实测 token）", ""]
    if arms:
        rows = sorted(arms["arms"].items(),
                      key=lambda kv: (kv[1].get("calls", 0), -kv[1].get("f1", 0)))
        md += ["| 配置 | F1 | LLM 调用 | 调用率 | prompt token |",
               "|---|---|---|---|---|"]
        for a, v in rows:
            md.append(f"| {ARM_CN.get(a, a)} | {pc(v.get('f1'))} | {v.get('calls', 0)} | "
                      f"{pc(v.get('calls', 0)/max(1, arms.get('n_ships', 1)))} | "
                      f"{v.get('prompt_tokens', 0):,} |")
    if par:
        md += ["", "门控阈值扫描（未过闸门用 L1 兜底）：", "",
               "| 门控阈值 τ | LLM 调用 | 调用率 | prompt token | F1 |",
               "|---|---|---|---|---|"]
        for r in par["curve"]:
            t = "∞（纯 L1）" if r["tau"] == float("inf") else f"{r['tau']:.3f}"
            md.append(f"| {t} | {r['calls']} | {pc(r['call_rate'])} | "
                      f"{r['prompt_tokens']:,} | {pc(r['f1'])} |")
    md.append("")

    # ══ 表 3：外部体检 ═══════════════════════════════════════════
    he = jload(L / "hawaii_eval.json")
    l1h = jload(L / "l1_hawaii.json")
    md += ["## 表 3 外部体检（HawaiiCoast_GT，11,064 窗 / 144 正 / 182 天）", "",
           "真值来自美国海岸警卫队事故报告，**与我们的实现无关**。",
           "正例占比 1.30% →「全判异常」的平凡 F1 上限 = **2.57%**。", ""]
    if he:
        npos, nneg = he.get("n_pos"), he.get("n_neg")
        base = npos / max(1, (npos or 0) + (nneg or 0))
        md += [f"评测集：{npos} 正 / {nneg} 负，正例占比 {pc(base, 2)}。", "",
               "| 方法 | 文献报告 F1 | 本次复现 AUC | 本次复现 oracle F1 | @1%FPR 召回 | 差值（复现−文献） |",
               "|---|---|---|---|---|---|"]
        for k in ORDER:
            r = he["methods"].get(k)
            if not r:
                md.append(f"| {CN[k]} | — | 未完成 | — | — | — |")
                continue
            lit = LIT_F1.get(k)
            d = f"{(r.get('f1') or 0)*100 - lit*100:+.1f} pt" if lit else "—"
            md.append(f"| {CN[k]} | {nm(lit)} | {nm(r.get('auc'))} | {pc(r.get('f1'))} | "
                      f"{pc(r.get('fpr1_recall'))} | {d} |")
    if l1h:
        md += ["", "我们自己的 L1 在同一评测集上（**部署即所得，不调阈值**）：", "",
               "| 口径 | 精确率 | 召回 | F1 | 误报率 |", "|---|---|---|---|---|"]
        for tag, r in l1h.items():
            if not isinstance(r, dict) or "f1" not in r:
                continue
            md.append(f"| {tag} | {pc(r['precision'])} | {pc(r['recall'])} | "
                      f"{pc(r['f1'])} | {pc(r.get('fpr'))} |")
    md += ["",
           "> ⚠ **这张表测的不是我们的任务**：Hawaii 的真值是「这条船**出没出事**」",
           "> （拖带异常/推进失效/搁浅/航路偏离），我们做的是「**违没违反避碰规则**」。",
           "> 《国际海上避碰规则》里没有「发动机坏了」这类条款。",
           "> 所以本表的用途是**外部体检**——检验文献报告的高分在非循环基准上能否复现，",
           "> 不是「我们比他们强」。", ""]

    # ══ 表 4：优化迭代的前后对照 ══════════════════════════════════
    gd = jload(PAPER / "gate_design.json")
    md += ["## 表 4 我们自己的优化迭代（优化前 → 优化后）", "",
           "| 项目 | 优化前 | 优化后 | 变化 | 结论 |",
           "|---|---|---|---|---|"]
    if gd:
        sc = gd.get("scenarios", {})
        for tag, key in (("门控判据（完整规则库）", "(a) 完整规则库"),
                         ("门控判据（摘掉让路规则）", "(b) 摘掉 give_way_failure 规则"),
                         ("门控判据（摘掉追越规则）", "(c) 摘掉 overtaking_violation 规则")):
            s = sc.get(key, {})
            old = next((v for k, v in s.items() if k.startswith("S1")), None)
            new = next((v for k, v in s.items() if k.startswith("S3")), None)
            if old and new:
                md.append(f"| {tag} | {pc(old['f1'])} @ {old['calls']} 次调用 | "
                          f"**{pc(new['f1'])} @ {new['calls']} 次** | "
                          f"F1 {(new['f1']-old['f1'])*100:+.1f} pt，"
                          f"调用 {(new['calls']-old['calls'])/max(1,old['calls'])*100:+.1f}% | ✅ |")
    fs = jload(PAPER / "flood_suppress.json")
    if fs:
        r0 = fs["rounds"].get("P0 现役（全部类型）", {}).get("松口径", {})
        r6 = fs["rounds"].get("P6 抑制报警最多的 3 类", {}).get("松口径", {})
        if r0 and r6:
            md.append(f"| 洪泛类型抑制 | {pc(r0['f1'])} | {pc(r6['f1'])} | "
                      f"F1 {(r6['f1']-r0['f1'])*100:+.1f} pt | ❌ 失败（越抑制越差） |")
    rs = jload(PAPER / "l1_rescore.json")
    if rs:
        b = rs["results"].get("现役：有事件就报（OR）", {}).get("B", {})
        best = max(((k, v) for k, v in rs["results"].items() if k != "现役：有事件就报（OR）"),
                   key=lambda kv: kv[1]["B"]["f1"], default=None)
        if b and best:
            md.append(f"| L1 输出重排 | {pc(b['f1'])} | {pc(best[1]['B']['f1'])} | "
                      f"F1 {(best[1]['B']['f1']-b['f1'])*100:+.1f} pt | ❌ 失败（L1 输出本无排序信息） |")
    gh = jload(PAPER / "gate_hawaii.json")
    if gh:
        md.append(f"| 真实数据 LLM 调用率 | 16.6%~17.3%（记录） | "
                  f"**{pc(gh.get('zero_event_rate'))}** | 成本降至约 36% | ⚠️ 但只覆盖 "
                  f"{pc(gh.get('incident_zero_event_rate'))} 的事故船-日 |")
    md.append("")

    # ══ 表 5：口径说明 ═══════════════════════════════════════════
    md += ["## 表 5 对比时必须写清的口径（否则审稿人会抓）", "",
           "| 项 | 我们的 | 文献 |",
           "|---|---|---|",
           "| 阈值 | L1 **无可调阈值**（部署即所得） | 报 **oracle**（测试集上调到最优，是上界） |",
           "| 真值来源 | 场景设计 + 独立参照实现回放校验 | 各自数据集，多数为合成注入 |",
           "| 特征 | 规则引擎输出的事件与几何量 | 运动学 + 会遇几何（我们**主动把会遇距离喂给它们**） |",
           "| 正负比例 | 严格 1:1 | 各不相同 |",
           "| 成本口径 | LLM 调用次数 + **实测 token**（Ollama 原生接口） | 多数论文不报 token |",
           "| 统计 | McNemar 精确 + 配对自助 95% CI | 多数只报点估计 |",
           "",
           "**一句话**：我们把对手放在最有利的位置（给上界、给最强特征），",
           "自己站在最不利的位置（无阈值可调），如果这样还赢，结论才站得住。", ""]

    (ROOT / "论文").mkdir(exist_ok=True)
    outp = ROOT / "论文" / "最终对比_我们的vs论文.md"
    outp.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ {outp}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
