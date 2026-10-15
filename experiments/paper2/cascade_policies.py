# -*- coding: utf-8 -*-
"""级联「升级策略」对比：把文献里的路由/级联方法搬到本任务上。

【这份脚本回答的问题】
  论文主张"三级级联用更低的 LLM 成本达到不劣的判定质量"。现有 `llm_arms.py`
  只把级联**逐级拆开**（拆成 D/E/F/G/H/I 各跑一遍），但没有回答文献真正问的那个问
  题：**给定一个便宜档和一个昂贵档，升级策略该怎么定？**

  这正是 FrugalGPT（Chen et al. 2023）/ RouteLLM（Ong et al. 2024）/
  AutoMix（Aggarwal et al. 2023）/ Hybrid LLM（Ding et al., ICLR 2024）的设定。
  本脚本在同一批 query、同一份真值、同一对模型上复现它们的升级策略并对比。

【实验设定（与文献对齐）】
  便宜档 cheap   ：qwen2.5:7b（本地小模型）
  昂贵档 expensive：qwen3:8b
  每一档都拿到**完全相同的提示**（同一 arm 的同一 prompt），
  唯一变量是模型 —— 这样成本差才是"模型的差"，而不是"给的信息的差"。

【策略（全部只决定"要不要升级"，不改变任何一档的内部行为）】
  never       从不升级，只用便宜档
  always      总是升级，等价于只用昂贵档
  random      以固定概率升级（扫一波，作为"无信息"参照线）
  automix     便宜档自报置信度低于阈值就升级（Aggarwal et al. 2023 的自验证思想）
  frugalgpt   训练评分函数 σ(query, cheap_answer) → P(便宜档答对)，低于阈值升级
              （Chen et al. 2023；它看得到便宜档的答案，所以是"便宜档之后"决策）
  routellm    只用 query 侧特征训练 router，预测该升级到昂贵档
              （Ong et al. 2024；它在调便宜档**之前**决策）
  ours       本系统的门控：L1 规则引擎给不出确定判据时升级（L2 LightGBM 风险门控）

【指标（文献同口径）】
  · 升级率（= 昂贵档调用率，RouteLLM 报的就是这个）
  · F1 / 精确率 / 召回
  · 昂贵档 prompt token 成本、相对 always 的节省比例
  扫描阈值得到 **成本-质量 Pareto 前沿**，而不是只报一个工作点 —— 单点对比
  可以靠调阈值造出来，前沿不能。

【防泄漏（关键）】
  两组需要训练的基线（frugalgpt / routellm）一律用 **5 折交叉验证的
  折外预测**（cross_val_predict）决定是否升级。若在全部数据上训练再在同一批
  数据上评阈值，得到的是"上限"，不是可用数字 —— 那正是我们指责文献基线
  用 oracle 阈值时说的话，自己不能犯。

用法：
  python experiments/paper2/cascade_policies.py
  python experiments/paper2/cascade_policies.py --cheap-arm F --exp-arm F
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(r"E:\AIS")
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PAPER = ROOT / "data/logs/paper"
CACHE = PAPER / "llm_cache"
GT_PATH = ROOT / "data/logs/inject_v1.gt.json"
L1_CACHE = PAPER / "l1_inject.json"

CHAINS = ("give_way_failure", "overtaking_violation")
N_FOLDS = 5
SEED = 20261015


# ── 数据加载 ──────────────────────────────────────────────────────

def load_arm(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out[str(r["mmsi"])] = r
    return out


def l1_verdict(l1: dict, mmsi: str) -> tuple[bool, set[str], dict]:
    """L1 的判定：是否报了候选事件、报了哪些链、以及几何特征。"""
    rec = l1.get(mmsi) or {}
    evs = rec.get("events") or []
    types = {str(e.get("type")) for e in evs}
    hit = bool(types & set(CHAINS))
    geom = {}
    for e in evs:
        for k, v in (e.get("features") or {}).items():
            if isinstance(v, (int, float)) and k not in geom:
                geom[k] = float(v)
    ai = rec.get("agent_input") or {}
    for k in ("sog", "cog", "max_turn_rate", "straightness", "min_distance_nm"):
        v = ai.get(k)
        if isinstance(v, (int, float)):
            geom.setdefault(k, float(v))
    return hit, types, geom


def cheap_features(rec: dict, l1_hit: bool, l1_types: set[str], geom: dict) -> dict:
    """FrugalGPT 的评分函数能看到的东西：query + 便宜档的答案。"""
    f: dict[str, float] = {}
    conf = rec.get("confidence")
    f["conf"] = float(conf) if isinstance(conf, (int, float)) else -1.0
    f["said_violation"] = 1.0 if rec.get("violation_detected") else 0.0
    f["parse_failed"] = 1.0 if rec.get("parse_failed") else 0.0
    f["reason_len"] = float(len(str(rec.get("reason") or "")))
    f["unk_type"] = 1.0 if rec.get("violation_detected") and not rec.get("violation_type") else 0.0
    # 便宜档与规则引擎是否一致 —— 分歧本身就是"该升级"的信号
    f["agree_with_l1"] = 1.0 if bool(rec.get("violation_detected")) == l1_hit else 0.0
    f.update({f"l1type_{t}": 1.0 for t in l1_types})
    f["l1_hit"] = 1.0 if l1_hit else 0.0
    return f


def query_features(l1_hit: bool, l1_types: set[str], geom: dict) -> dict:
    """RouteLLM 的 router 只看 query 侧，**看不到便宜档的答案**。"""
    f: dict[str, float] = {"l1_hit": 1.0 if l1_hit else 0.0}
    f.update({f"l1type_{t}": 1.0 for t in l1_types})
    for k, v in geom.items():
        if math.isfinite(v):
            f[f"g_{k}"] = v
    return f


def to_matrix(rows: list[dict]) -> np.ndarray:
    keys = sorted({k for r in rows for k in r})
    return np.asarray([[r.get(k, 0.0) for k in keys] for r in rows], dtype=float), keys


# ── 指标 ──────────────────────────────────────────────────────────

def prf(tp: int, fp: int, fn: int) -> tuple[float, float, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return p, r, f


def score(y: np.ndarray, pred: np.ndarray) -> dict:
    tp = int((pred & y).sum())
    fp = int((pred & ~y).sum())
    fn = int((~pred & y).sum())
    tn = int((~pred & ~y).sum())
    p, r, f = prf(tp, fp, fn)
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": p, "recall": r, "f1": f}


def cross_val_scores(X: np.ndarray, label: np.ndarray) -> np.ndarray:
    """折外预测的概率。用 label 定义"便宜档答对/该升级"，训练与评估不重叠。"""
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    n_splits = min(N_FOLDS, max(2, int(label.sum()), int((~label).sum())))
    clf = LogisticRegression(max_iter=2000, C=1.0, random_state=SEED)
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=SEED)
    # 类别数不足 2 时 cross_val_predict 会报错，退化到"全给 0.5"
    if label.all() or (~label).all():
        return np.full(len(label), 0.5)
    proba = cross_val_predict(clf, X, label.astype(int), cv=skf,
                              method="predict_proba")[:, 1]
    return np.asarray(proba, dtype=float)


# ── 主流程 ────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cheap-tag", default="", help="便宜档缓存子目录（默认空=第一档）")
    ap.add_argument("--cheap-arm", default="D")
    ap.add_argument("--exp-tag", default="qwen3")
    ap.add_argument("--exp-arm", default="D")
    ap.add_argument("--out", default="data/logs/paper/cascade_policies.json")
    args = ap.parse_args()

    def load(tag: str, arm: str) -> dict[str, dict]:
        # `.` 与空串都表示根缓存目录（第一档模型的结果就落在这里）。
        # 用 `.` 是因为 PowerShell 会把命令行里的空字符串吃掉。
        d = CACHE if tag in ("", ".") else CACHE / tag
        return load_arm(d / f"{arm}.jsonl")

    cheap = load(args.cheap_tag, args.cheap_arm)
    exp = load(args.exp_tag, args.exp_arm)
    if not cheap or not exp:
        print(f"缺缓存：cheap={len(cheap)} exp={len(exp)}"
              f"（cheap={args.cheap_tag or '.'}/{args.cheap_arm}.jsonl,"
              f" exp={args.exp_tag}/{args.exp_arm}.jsonl）")
        return 2
    l1 = json.loads(L1_CACHE.read_text(encoding="utf-8"))

    gt = json.loads(GT_PATH.read_text(encoding="utf-8"))
    scen = {str(s["own_mmsi"]): s for s in gt["scenarios"]}

    mmsis = [m for m in sorted(scen) if m in cheap and m in exp]
    y = np.asarray([scen[m]["code"] is not None for m in mmsis], dtype=bool)
    print(f"对齐样本 {len(mmsis)} 条（正 {int(y.sum())} / 负 {len(y) - int(y.sum())}）")
    print(f"便宜档 {args.cheap_tag or '.'}/{args.cheap_arm}"
          f"   昂贵档 {args.exp_tag}/{args.exp_arm}")

    cheap_pred = np.asarray([bool(cheap[m].get("violation_detected")) for m in mmsis])
    exp_pred = np.asarray([bool(exp[m].get("violation_detected")) for m in mmsis])

    l1_flags, l1_geoms, l1_sets = [], [], []
    for m in mmsis:
        hit, types, geom = l1_verdict(l1, m)
        l1_flags.append(hit)
        l1_sets.append(types)
        l1_geoms.append(geom)
    l1_pred = np.asarray(l1_flags, dtype=bool)

    # ── 特征 ────────────────────────────────────────────────────
    cf_rows = [cheap_features(cheap[m], h, t, g)
               for m, h, t, g in zip(mmsis, l1_flags, l1_sets, l1_geoms)]
    qf_rows = [query_features(h, t, g)
               for h, t, g in zip(l1_flags, l1_sets, l1_geoms)]
    Xc, _ = to_matrix(cf_rows)
    Xq, _ = to_matrix(qf_rows)

    # ── 需要训练的基线的折外分数 ────────────────────────────────
    # frugalgpt：预测"便宜档会不会答对"
    cheap_correct = (cheap_pred == y)
    fg_score = cross_val_scores(Xc, cheap_correct)
    # routellm：预测"这次该不该升级" —— 定义成"昂贵档对而便宜档错"就太稀疏，
    # 改用"便宜档答错"作为升级目标，这正是 RouteLLM 训练路由器的监督信号。
    rl_score = cross_val_scores(Xq, ~cheap_correct)
    # automix：便宜档自报置信度
    am_score = np.asarray(
        [float(cheap[m].get("confidence"))
         if isinstance(cheap[m].get("confidence"), (int, float)) else 0.5
         for m in mmsis])

    print(f"\n便宜档单独：F1={score(y, cheap_pred)['f1']:.3f}"
          f"   昂贵档单独：F1={score(y, exp_pred)['f1']:.3f}"
          f"   L1 规则单独：F1={score(y, l1_pred)['f1']:.3f}")

    # ── 策略评估 ────────────────────────────────────────────────
    # 约定：`defer=True` → 采用昂贵档答案，否则采用便宜档答案。
    results: dict[str, dict] = {}

    def eval_policy(name: str, defer: np.ndarray, note: str = "") -> dict:
        pred = np.where(defer, exp_pred, cheap_pred)
        s = score(y, pred)
        s["defer_rate"] = float(defer.mean())
        s["n_defer"] = int(defer.sum())
        s["name"] = name
        s["note"] = note
        results[name] = s
        return s

    eval_policy("never", np.zeros(len(y), dtype=bool), "只用便宜档")
    eval_policy("always", np.ones(len(y), dtype=bool), "只用昂贵档")
    eval_policy("ours_l1gate", l1_pred, "本系统：L1 报违规时交给 LLM 复核")

    rng = np.random.default_rng(SEED)
    rand_curve = []
    for r in np.linspace(0.0, 1.0, 21):
        d = rng.random(len(y)) < r
        s = score(y, np.where(d, exp_pred, cheap_pred))
        rand_curve.append({"rate": float(r), "f1": s["f1"]})
    results["random"] = {"curve": rand_curve, "name": "random",
                         "note": "无信息参照线"}

    def sweep(name: str, score_arr: np.ndarray, note: str) -> dict:
        """分数越高 = 越该升级。扫阈值得到 Pareto 点。"""
        pts = []
        for thr in np.unique(np.round(score_arr, 4)):
            d = score_arr >= thr
            s = score(y, np.where(d, exp_pred, cheap_pred))
            pts.append({"thr": float(thr), "defer_rate": float(d.mean()),
                        "f1": s["f1"], "precision": s["precision"],
                        "recall": s["recall"], "n_defer": int(d.sum())})
        pts.append({"thr": float("inf"), "defer_rate": 0.0,
                    "f1": score(y, cheap_pred)["f1"],
                    "precision": score(y, cheap_pred)["precision"],
                    "recall": score(y, cheap_pred)["recall"], "n_defer": 0})
        pts.sort(key=lambda p: p["defer_rate"])
        results[name] = {"curve": pts, "name": name, "note": note}
        return results[name]

    sweep("frugalgpt", fg_score, "训练评分函数 σ(query, 便宜档答案)")
    sweep("routellm", rl_score, "只用 query 侧特征训练 router")
    sweep("automix", am_score, "便宜档自报置信度低于阈值即升级")

    # ── 输出 ────────────────────────────────────────────────────
    print("\n" + "=" * 96)
    print("级联升级策略对比（同一批 query，同一对模型，唯一变量是升级策略）")
    print("=" * 96)
    hdr = f"{'策略':<14}{'升级率':>9}{'精确率':>9}{'召回':>9}{'F1':>9}   说明"
    print(hdr)
    print("-" * 96)
    for k in ("never", "always", "ours_l1gate"):
        s = results[k]
        print(f"{k:<14}{s['defer_rate']*100:>8.1f}%{s['precision']*100:>8.1f}%"
              f"{s['recall']*100:>8.1f}%{s['f1']*100:>8.1f}%   {s['note']}")
    print("-" * 96)
    for k in ("random", "automix", "frugalgpt", "routellm"):
        pts = results[k]["curve"]
        # `random` 曲线的键叫 rate，其余叫 defer_rate —— 统一一下再打印。
        for p in pts:
            p.setdefault("defer_rate", p.get("rate", 0.0))
        lo = min(pts, key=lambda p: p["defer_rate"])
        hi = max(pts, key=lambda p: p["defer_rate"])
        best = max(pts, key=lambda p: p["f1"])
        print(f"{k:<14}{'—':>8}{'—':>9}{'—':>9}{best['f1']*100:>8.1f}%   {results[k]['note']}")
        print(f"{'':<14}  前沿端点：升级率 {lo['defer_rate']*100:.0f}% → F1 {lo['f1']*100:.1f}%"
              f" ; 升级率 {hi['defer_rate']*100:.0f}% → F1 {hi['f1']*100:.1f}%"
              f" ; 本策略最优 F1 {best['f1']*100:.1f}% @ 升级率 {best['defer_rate']*100:.0f}%")

    # ── 同预算对比（文献的 cost-quality 口径）─────────────────────
    # 单看"各策略自己的最优点"没有意义：只要升级总能带来提升，最优点就一定在
    # 100% 升级处，所有策略并列。真正该问的是**给定同一个升级预算，谁能把质量
    # 做得更高** —— 这正是 FrugalGPT/RouteLLM 画的成本-质量曲线。
    grid = [0.0, 0.05, 0.10, 0.20, 0.30, 0.50, 0.75, 1.0]
    policy_keys = ("random", "automix", "frugalgpt", "routellm")

    def at_budget(pts: list[dict], budget: float) -> dict | None:
        """在不超过 budget 的升级预算下，该策略能达到的最好 F1。"""
        cand = [p for p in pts if p["defer_rate"] <= budget + 1e-9]
        return max(cand, key=lambda p: p["f1"]) if cand else None

    print("\n" + "=" * 96)
    print("同升级预算下的 F1（行=策略，列=升级预算；同一列可直接横向比）")
    print("=" * 96)
    print(f"{'策略':<14}" + "".join(f"{f'{int(g*100)}%':>10}" for g in grid))
    print("-" * 96)
    base_rows = {"never": [{"defer_rate": 0.0, "f1": results["never"]["f1"]}],
                 "always": [{"defer_rate": 1.0, "f1": results["always"]["f1"]}],
                 "ours_l1gate": [{"defer_rate": results["ours_l1gate"]["defer_rate"],
                                  "f1": results["ours_l1gate"]["f1"]}]}
    for k in ("never", "always", "ours_l1gate", *policy_keys):
        pts = base_rows.get(k) or results[k]["curve"]
        cells = []
        for g in grid:
            p = at_budget(pts, g)
            cells.append("—" if p is None else f"{p['f1']*100:.1f}")
        print(f"{k:<14}" + "".join(f"{c:>10}" for c in cells))
    print("-" * 96)
    print("读法：同一列里数字最大者 = 该成本预算下最值得用的策略。")
    print("      若 learned 策略（frugalgpt/routellm）在每一列都不高于 random，")
    print("      说明它们的路由信号在本任务上没有超越'随便挑'的价值 —— 必须如实报。")

    results["_matched_budget"] = {k: {f"{g:.2f}": (at_budget(
        base_rows.get(k) or results[k]["curve"], g) or {}).get("f1")
        for g in grid} for k in ("never", "always", "ours_l1gate", *policy_keys)}

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "config": {"cheap_tag": args.cheap_tag, "cheap_arm": args.cheap_arm,
                   "exp_tag": args.exp_tag, "exp_arm": args.exp_arm,
                   "n": len(mmsis), "n_pos": int(y.sum()),
                   "cheap_f1": score(y, cheap_pred)["f1"],
                   "exp_f1": score(y, exp_pred)["f1"],
                   "l1_f1": score(y, l1_pred)["f1"]},
        "policies": results,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n明细 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
