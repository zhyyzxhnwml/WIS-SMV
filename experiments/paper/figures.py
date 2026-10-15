# -*- coding: utf-8 -*-
"""论文配图（英文标注，避免中文字体依赖）。

产出（data/logs/paper/figures/）：
  fig_cost_quality.png   主图：成本-质量散点（各臂 + 文献基线 + 平凡线）
  fig_pareto.png         级联帕累托前沿：门控阈值 → (LLM 调用率, F1)
  fig_lit_gap.png        外部体检：文献报告 F1 vs 本次复现 oracle F1

只读产物，不重算。缺哪个产物就跳过哪张图并说明。

用法：
  python experiments/paper/figures.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt            # noqa: E402

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

L = ROOT / "data/logs"
PAPER = L / "paper"
FIG = PAPER / "figures"
FIG.mkdir(parents=True, exist_ok=True)

LIT_F1 = {"lof": 0.513, "dbscan": 0.598, "iforest": 0.650, "vae": 0.745,
          "lstm_ae": 0.752, "vae_lstm": 0.872, "transformer_lstm": 0.953}
ORDER = ["proximity", "lof", "iforest", "dbscan", "vae", "lstm_ae", "vae_lstm",
         "transformer_lstm"]
EN = {"proximity": "Proximity", "lof": "LOF", "iforest": "iForest",
      "dbscan": "DBSCAN", "vae": "VAE", "lstm_ae": "LSTM-AE",
      "vae_lstm": "VAE-LSTM", "transformer_lstm": "Trans-LSTM"}


def jload(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def fig_cost_quality() -> None:
    arms = None
    for c in ("llm_arms_all.json", "llm_arms.json", "llm_arms_part2.json"):
        arms = jload(PAPER / c)
        if arms:
            break
    bl = jload(L / "baselines_inject.json")
    n = (arms or {}).get("n_ships", 240)

    fig, ax = plt.subplots(figsize=(7.6, 5.0))
    # 正负各半 → "全判违规"的平凡 F1 = 2·0.5/(1+0.5) = 66.7（**单位是 %**）
    trivial = 2 * 0.5 / 1.5 * 100.0
    ax.axhline(trivial, color="grey", ls="--", lw=1.0)
    ax.text(n * 0.30, trivial + 1.2, "trivial all-positive F1 = 66.7%",
            color="grey", fontsize=8)

    en = {"A": "A  L1 rules", "B": "B  L2 gate", "C": "C  cascade (prod)",
          "D": "D  raw -> LLM", "E": "E  compressed -> LLM",
          "F": "F  annotated -> LLM", "G": "G  FrugalGPT-style",
          "H": "H  geometry -> LLM", "I": "I  rule-as-prompt -> LLM"}
    if arms:
        for a in sorted(arms["arms"]):
            r = arms["arms"][a]
            calls = r.get("calls", 0)
            f1 = (r.get("f1") or 0) * 100
            ours = a in ("A", "B", "C")
            ax.scatter(calls, f1, marker="*" if ours else "o",
                       s=230 if ours else 70,
                       color="#c0392b" if ours else "#2471a3", zorder=3)
            dx, dy = (8, 0) if calls < n * 0.5 else (-8, 0)
            ax.annotate(en.get(a, a), (calls, f1), fontsize=8, va="center",
                        ha="left" if dx > 0 else "right",
                        xytext=(dx, dy), textcoords="offset points")

    if bl:
        for k in ORDER:
            r = bl["methods"].get(k)
            if not r:
                continue
            ax.scatter(0, (r.get("f1") or 0) * 100, marker="s", s=55,
                       facecolors="none", edgecolors="#7f8c8d", zorder=2)
        ax.scatter([], [], marker="s", facecolors="none", edgecolors="#7f8c8d",
                   label="8 literature detectors (oracle thr., 0 calls)")
    ax.scatter([], [], marker="*", s=230, color="#c0392b",
               label="our cascade stages")
    ax.scatter([], [], marker="o", s=70, color="#2471a3",
               label="our LLM-only arms")

    ax.set_xlabel("LLM calls (cost)")
    ax.set_ylabel("F1 (%)")
    ax.set_title("Injection benchmark: quality vs LLM cost")
    ax.set_xlim(-14, n * 1.12)
    ax.set_ylim(38, 106)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8, loc="center left", framealpha=0.95,
              bbox_to_anchor=(0.02, 0.34))
    fig.tight_layout()
    fig.savefig(FIG / "fig_cost_quality.png", dpi=200)
    plt.close(fig)
    print("  fig_cost_quality.png")


def fig_pareto() -> None:
    par = jload(PAPER / "cascade_pareto.json")
    if not par:
        print("  [skip] fig_pareto（缺 cascade_pareto.json）")
        return
    cur = [r for r in par["curve"] if r["calls"] > 0 or r["tau"] == float("inf")]
    fig, ax = plt.subplots(figsize=(7.2, 5.0))
    xs = [r["call_rate"] * 100 for r in sorted(cur, key=lambda r: r["call_rate"])]
    ys = [r["f1"] * 100 for r in sorted(cur, key=lambda r: r["call_rate"])]
    ax.plot(xs, ys, "-o", color="#c0392b", ms=5, label="cascade (τ sweep)")
    ax.scatter([0], [par["l2_only"]["f1"] * 100], marker="^", s=90,
               color="#116a3e", label="L2 screen only (0 calls)", zorder=3)
    ax.scatter([100], [par["llm_only"]["f1"] * 100], marker="v", s=90,
               color="#2471a3", label="pure LLM (100% calls)", zorder=3)
    ax.set_xlabel("LLM call rate (%)")
    ax.set_ylabel("F1 (%)")
    ax.set_title("Cascade cost-quality Pareto (injection benchmark)")
    ax.grid(alpha=0.25)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(FIG / "fig_pareto.png", dpi=200)
    plt.close(fig)
    print("  fig_pareto.png")


def fig_lit_gap() -> None:
    he = jload(L / "hawaii_eval.json")
    if not he:
        print("  [skip] fig_lit_gap（缺 hawaii_eval.json）")
        return
    ks = [k for k in ORDER if k in he.get("methods", {}) and k in LIT_F1]
    if not ks:
        return
    lit = [LIT_F1[k] * 100 for k in ks]
    rep = [(he["methods"][k].get("f1") or 0) * 100 for k in ks]
    x = range(len(ks))
    fig, ax = plt.subplots(figsize=(8.2, 4.6))
    ax.bar([i - 0.2 for i in x], lit, width=0.4, color="#7f8c8d",
           label="F1 as reported in the literature")
    ax.bar([i + 0.2 for i in x], rep, width=0.4, color="#c0392b",
           label="F1 reproduced on HawaiiCoast_GT (oracle thr.)")
    npos, nneg = he.get("n_pos"), he.get("n_neg")
    if npos and nneg:
        base = npos / (npos + nneg)
        ax.axhline(2 * base / (1 + base) * 100, color="k", ls=":", lw=1.2,
                   label="trivial all-anomalous ceiling")
    ax.set_xticks(list(x))
    ax.set_xticklabels([EN[k] for k in ks], fontsize=9)
    ax.set_ylabel("F1 (%)")
    ax.set_title("External audit: reported vs reproduced F1")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.25, axis="y")
    fig.tight_layout()
    fig.savefig(FIG / "fig_lit_gap.png", dpi=200)
    plt.close(fig)
    print("  fig_lit_gap.png")


def main() -> int:
    print("生成图 →", FIG)
    fig_cost_quality()
    fig_pareto()
    fig_lit_gap()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
