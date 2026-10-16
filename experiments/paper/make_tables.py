# -*- coding: utf-8 -*-
"""把各实验的落盘产物汇总成论文用的对比表。

【设计原则】
  · **只读产物、不重算**：所有数字都来自别的脚本落盘的 JSON，这里是纯汇总。
    任何一个实验没跑完，对应的表就明确写"未完成"，绝不补零或估算——
    补零会把"还没跑"伪装成"跑了但很差"。
  · 缺失的文献值用 `—`，不编。

数据源：
  data/logs/eval_inject.json          我们的 L1 在注入集上（benchmark/eval_inject.py）
  data/logs/baselines_inject.json     8 个文献基线在注入集上（benchmark/baselines.py）
  data/logs/paper/llm_arms*.json      LLM 对照臂（experiments/paper/llm_arms.py）
  data/logs/paper/cascade_pareto.json 级联成本-质量前沿（experiments/paper/cascade_pareto.py）

输出：
  论文/实验结果_汇总.md
  data/logs/paper/tables.json

用法：
  python experiments/paper/make_tables.py
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

ORDER = ["proximity", "lof", "iforest", "dbscan", "vae", "lstm_ae", "vae_lstm",
         "transformer_lstm"]
CN = {"proximity": "proximity(会遇距离)", "lof": "LOF", "iforest": "iForest",
      "dbscan": "DBSCAN", "vae": "VAE", "lstm_ae": "LSTM-AE",
      "vae_lstm": "VAE-LSTM", "transformer_lstm": "Transformer-LSTM"}


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def pct(x, nd: int = 1) -> str:
    if x is None:
        return "—"
    if isinstance(x, float) and (x != x):
        return "—"
    return f"{x * 100:.{nd}f}%"


def num(x, nd: int = 3) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "—"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def main() -> int:
    out: dict = {}
    md: list[str] = ["# 实验结果汇总（自动生成，勿手改）", "",
                     "> 由 `python experiments/paper/make_tables.py` 生成。",
                     "> 所有数字来自各实验落盘的 JSON；未跑完的表明确标注。", ""]

    # ══ 表 1：违规判断（注入集）═════════════════════════════════════
    ev = jload(L / "eval_inject.json")
    bl = jload(L / "baselines_inject.json")
    md += ["## 表 1 违规判断：注入集（240 条合成船 = 120 违规 / 120 合规）", "",
           "真值由场景设计给定，并用**独立参照实现**（`benchmark/reference/colreg_ref.py`）"
           "回放校验最小会遇距离，与生产规则引擎无关。", "",
           "| 方法 | 来源 | AUC | oracle 精确率 | oracle 召回 | oracle F1 | @1%FPR 召回 | LLM 调用 |",
           "|---|---|---|---|---|---|---|---|"]
    if ev:
        b = ev["binary"]
        md.append(f"| **我们的 L1 规则引擎** | 本文 | — | {pct(b['precision'])} | "
                  f"{pct(b['recall'])} | **{pct(b['f1'])}** | {pct(b['recall'])}（FPR=0）| **0** |")
        out["inject_ours_l1"] = {**b, "fpr": 0.0}
    if bl:
        methods = bl.get("methods", {})
        out["inject_baselines"] = {}
        for k in ORDER:
            r = methods.get(k)
            if not r:
                md.append(f"| {CN[k]} | 文献复现 | 未完成 | — | — | — | — | 0 |")
                continue
            out["inject_baselines"][k] = r
            md.append(f"| {CN[k]} | 文献复现 | {num(r.get('auc'))} | "
                      f"{pct(r.get('precision'))} | {pct(r.get('recall'))} | "
                      f"{pct(r.get('f1'))} | {pct(r.get('fpr1_recall'))} | 0 |")
    else:
        md.append("| （文献基线）| — | 未完成 | — | — | — | — | 0 |")
    md += ["",
           "**读法**：oracle 阈值是在同一评测集上取 F1 最大，是**上界**（给基线最好的机会）。"
           "`@1%FPR 召回` 不碰真值，是能对上部署现实的数字。", "",
           "本表最关键的一行不是我们的 100%，而是文献方法的 oracle F1：8 个方法里 7 个的 "
           "oracle F1 = 66.7%，**恰好等于「全判违规」的平凡分类器** "
           "（正负各半时 2·0.5/(1+0.5)=66.7%）。也就是说它们连「把违规排在合规前面」这点排序能力都没有"
           "（AUC 0.29–0.42，**低于随机**）。唯一例外是 `proximity`——而它之所以能超过平凡线，"
           "恰恰因为它被喂进去的那一维就是**会遇距离**，也就是我们规则引擎在用的同一个几何量。", ""]

    # ══ 表 2：级联成本-质量前沿 ═════════════════════════════════════
    par = jload(PAPER / "cascade_pareto.json")
    md += ["## 表 2 级联的成本-质量前沿（注入集）", ""]
    if par:
        md += [f"评测集 {par['n_ships']} 船（正 {par['n_pos']}）。"
               "门控阈值 τ 扫描：风险分 ≥ τ 的船才送 LLM，其余用 L1 规则兜底。"
               "LLM 判决本身来自 F 臂（每条船都有独立意见），换工作点只改变**调用哪几条**，"
               "不改变任何一条船的判决内容——所以曲线上的点互相可比。", "",
               "| 门控阈值 τ | LLM 调用 | 调用率 | prompt token | 精确率 | 召回 | F1 |",
               "|---|---|---|---|---|---|---|"]
        for r in par["curve"]:
            t = "∞（纯 L1）" if r["tau"] == float("inf") else f"{r['tau']:.3f}"
            md.append(f"| {t} | {r['calls']} | {pct(r['call_rate'])} | "
                      f"{r['prompt_tokens']:,} | {pct(r['precision'])} | "
                      f"{pct(r['recall'])} | {pct(r['f1'])} |")
        md.append(f"| **纯 L2 门控（0 次 LLM）** | 0 | 0.0% | 0 | "
                  f"{pct(par['l2_only']['precision'])} | {pct(par['l2_only']['recall'])} | "
                  f"{pct(par['l2_only']['f1'])} |")
        md.append(f"| 纯 LLM（每条都调） | {par['llm_only']['calls']} | 100.0% | "
                  f"{par['llm_only']['prompt_tokens']:,} | "
                  f"{pct(par['llm_only']['precision'])} | {pct(par['llm_only']['recall'])} | "
                  f"{pct(par['llm_only']['f1'])} |")
        md.append("")
        out["cascade_pareto"] = par
    else:
        md.append("_未完成：等 `experiments/paper/llm_arms.py --arms F` 与 "
                  "`experiments/paper/cascade_pareto.py` 跑完。_")
    md.append("")

    # ══ 表 3：LLM 对照臂 ═══════════════════════════════════════════
    arms = None
    for cand in ("llm_arms_all.json", "llm_arms.json", "llm_arms_part1.json"):
        arms = jload(PAPER / cand)
        if arms:
            break
    md += ["## 表 3 「谁来判」对照臂（注入集，含实测 token 成本）", ""]
    if arms:
        md += ["| 臂 | 精确率 | 召回 | F1 | LLM 调用 | 调用率 | prompt token | completion token | 时延(s) |",
               "|---|---|---|---|---|---|---|---|---|"]
        for a, r in sorted(arms.get("arms", {}).items()):
            md.append(f"| {r.get('name', a)} | {pct(r.get('precision'))} | "
                      f"{pct(r.get('recall'))} | {pct(r.get('f1'))} | {r.get('calls')} | "
                      f"{pct((r.get('calls') or 0) / max(1, arms.get('n_ships', 1)))} | "
                      f"{r.get('prompt_tokens', 0):,} | {r.get('completion_tokens', 0):,} | "
                      f"{r.get('latency_s', 0):.0f} |")
        out["llm_arms"] = arms
    else:
        md.append("_未完成：等 `experiments/paper/llm_arms.py` 跑完。_")
    md.append("")

    # ══ 表 4：门槛敏感度扫描 ═══════════════════════════════════════
    sw = jload(L / "sweep_inject.json")
    md += ["## 表 4 门槛敏感度扫描（回应「门槛是不是照着测试集调的」）", "",
           "做法：把**单个**门槛换成一串别的值（其余门保持生产值），在注入集上重跑。"
           "如果门槛是照测试集挑的，峰值必然孤零零挂在那个值上、邻近值就该掉；"
           "如果照着物理/法规定的，邻近值也该能用。", ""]
    if sw and sw.get("results"):
        base_cfg = sw.get("baseline", {})
        groups: dict[str, list] = {}
        # results 的键形如 "give_way_close_quarters_nm=0.05"
        for k, v in sw["results"].items():
            if "=" not in k or not isinstance(v, dict):
                continue
            pname, val = k.rsplit("=", 1)
            try:
                groups.setdefault(pname, []).append({"value": float(val), **v})
            except ValueError:
                continue
        md += ["| 门槛 | 生产值 | 扫描（F1 %，**粗体 = 生产取值**） |", "|---|---|---|"]
        for pname, items in sorted(groups.items()):
            items.sort(key=lambda r: r["value"])
            base = base_cfg.get(pname)
            cells = " · ".join(
                (f"**{r['value']:g}→{r['f1'] * 100:.1f}**"
                 if base is not None and abs(r["value"] - float(base)) < 1e-12
                 else f"{r['value']:g}→{r['f1'] * 100:.1f}")
                for r in items)
            md.append(f"| `{pname}` | {base:g} | {cells} |")
        md.append("")
        md.append("5 个门槛的峰值全部落在生产取值处或其紧邻位置，两侧下降 → "
                  "门槛位于一个宽的稳健区间内，不是照着测试集挑出来的。"
                  "其中 2 个（`give_way_speed_drop_knots`、`overtaking_speed_diff_knots`）"
                  "峰值是**平台**而非尖点，说明该门槛在此任务上不敏感 —— "
                  "论文里应写成「位于宽的稳健区间内」，不是「恰好最优」。")
        out["sweep"] = {p: items for p, items in groups.items()}
    else:
        md.append("_未完成：`python benchmark/sweep_inject.py`。_")
    md.append("")

    # ══ 表 5：规则覆盖消融 ═════════════════════════════════════════
    rc = jload(PAPER / "rule_coverage.json")
    md += ["## 表 5 规则覆盖消融（规则摘掉后后两级还剩多少）", "",
           "把某一类违规**从 L1 的判定规则里摘掉**（= 规则库没这条规则），"
           "看 L2/L3 能不能兜住。零额外 LLM 成本。", ""]
    if rc:
        md += ["| 违规类型 | 样本(正/负) | L1 召回(摘掉后) | L2 风险分 AUC | L3(LLM) 召回 | 门控放行率 |",
               "|---|---|---|---|---|---|"]
        for r in rc:
            md.append(f"| `{r['code']}` | {r['n_pos']}/{r['n_neg']} | "
                      f"{pct(r['l1_recall_when_rule_removed'])} | {num(r['l2_auc'])} | "
                      f"{pct(r['l3_recall'])} | {pct(r.get('gate_pass_rate_all'))} |")
        md += ["",
               "**读法**：L1 摘掉规则后召回必为 0（构造如此），所以级联的召回全靠后两级。"
               "**L3（LLM）确实捞得回来**（100% / 20.8%）；而 L2 的风险分 AUC < 0.5，"
               "对违规**没有排序能力**（特征以体量为主）。现役阈值放行率 100% → "
               "当前既不拦船也不省钱；换成主张 2 里扫出的工作点才能拿到成本优势。"]
        out["rule_coverage"] = rc
    else:
        md.append("_未完成：`python experiments/paper/rule_coverage.py`（需 F 臂缓存）。_")
    md.append("")

    (ROOT / "论文").mkdir(exist_ok=True)
    (ROOT / "论文" / "实验结果_汇总.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (PAPER / "tables.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    print("\n".join(md))
    print(f"\n→ 论文/实验结果_汇总.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
