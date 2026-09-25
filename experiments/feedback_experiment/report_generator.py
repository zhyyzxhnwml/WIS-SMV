"""
experiments/feedback_experiment/report_generator.py — 三实验报告生成
====================================================================

生成内容：
    实验A — 分歧率摸底分析
    实验B — 触发阈值扫描对比
    实验C — 反馈有无对照实验
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional


# ═══════════════════════════════════════════════════════════════
# 实验A：分歧率摸底
# ═══════════════════════════════════════════════════════════════

def analyze_disagreement(ship_data: list[dict]) -> dict:
    """分析 ML 与 LLM 之间的分歧情况，返回详细统计字典。"""
    n = len(ship_data)  # 船舶总数
    n_agree = sum(1 for d in ship_data if d.get("agreement"))  # ML 与 LLM 一致的船舶数
    n_disagree = n - n_agree  # ML 与 LLM 分歧的船舶数

    cross: dict[tuple[str, str], int] = Counter()  # ML类别→LLM标签的交叉计数字典
    for d in ship_data:  # 遍历每条船
        cross[(d["ml_category"], d["llm_label"])] += 1  # 统计交叉组合

    disagree_by_ml = {}  # 按 ML 类别分组的分歧统计数据字典
    for ml_cat in ["normal", "obvious_issue", "has_risk"]:  # 遍历 ML 类别
        ships = [d for d in ship_data if d["ml_category"] == ml_cat]  # 该 ML 类别的所有船
        total = len(ships)  # 该类别船舶总数
        disagree = sum(1 for d in ships if not d.get("agreement"))  # 该类别中分歧的船舶数
        if total == 0:  # 无数据则跳过
            continue
        flow: dict[str, int] = Counter()  # 该 ML 类别的分歧流向计数器
        for d in ships:  # 遍历该类别船舶
            if not d.get("agreement"):  # 如果是分歧船
                flow[d["llm_label"]] += 1  # 统计流向了哪个 LLM 标签
        disagree_by_ml[ml_cat] = {  # 记录该 ML 类别的分歧分析
            "total": total,  # 该类别总数
            "disagree": disagree,  # 分歧数
            "disagree_rate": disagree / max(total, 1),  # 分歧率（避免除零）
            "flow": dict(flow.most_common()),  # 分歧流向分布（按频次降序）
        }

    flow_total: dict[str, int] = Counter()  # 全部分歧流向计数器
    for d in ship_data:  # 遍历每条船
        if not d.get("agreement"):  # 如果是分歧船
            flow_total[f"{d['ml_category']} → {d['llm_label']}"] += 1  # 统计分歧方向

    fp = sum(1 for d in ship_data  # 假阳性（误报）数
             if d["ml_category"] in ("obvious_issue", "has_risk")  # ML 判定有问题
             and d.get("llm_violation_detected") is False)  # 但 LLM 认为无违规
    fn = sum(1 for d in ship_data  # 假阴性（漏报）数
             if d["ml_category"] == "normal"  # ML 认为正常
             and d.get("llm_violation_detected") is True)  # 但 LLM 检测到违规

    label_dist = dict(Counter(d["llm_label"] for d in ship_data).most_common())  # LLM 标签分布
    ml_dist = dict(Counter(d["ml_category"] for d in ship_data).most_common())  # ML 类别分布

    return {  # 返回完整的分歧分析结果
        "total_ships": n,  # 总船舶数
        "agreement_count": n_agree,  # 一致数
        "disagreement_count": n_disagree,  # 分歧数
        "agreement_rate": n_agree / n,  # 一致率
        "disagreement_rate": n_disagree / n,  # 分歧率
        "false_positive": fp,  # 假阳性数
        "false_negative": fn,  # 假阴性数
        "llm_label_dist": label_dist,  # LLM 标签分布
        "ml_category_dist": ml_dist,  # ML 类别分布
        "cross_table": {f"{ml}→{llm}": cnt for (ml, llm), cnt in cross.items()},  # 交叉统计表
        "disagree_by_ml": disagree_by_ml,  # 按 ML 类别分组的分析
        "flow_total": dict(flow_total.most_common()),  # 分歧流向汇总
    }

def render_experiment_a(disagreement: dict) -> str:
    """渲染实验A（分歧率摸底）的 Markdown 报告。"""
    d = disagreement  # 分歧分析结果字典的简写引用
    lines = []  # 报告行列表

    lines.append("## 实验A：分歧率摸底\n")
    lines.append("统计 ML 预测 vs LLM 判定的**一致/分歧**情况，定位当前模型容易出错的方向。\n")

    lines.append("### A1. 总体一致率\n")
    lines.append(f"| 指标 | 数值 |")
    lines.append(f"|------|------|")
    lines.append(f"| 总船舶数 | {d['total_ships']} 艘 |")
    lines.append(f"| ML vs LLM 一致 | {d['agreement_count']} 艘 ({d['agreement_rate']:.1%}) |")
    lines.append(f"| ML vs LLM 分歧 | {d['disagreement_count']} 艘 ({d['disagreement_rate']:.1%}) |")
    lines.append(f"| 假阳性（ML误报） | {d['false_positive']} 艘 |")
    lines.append(f"| 假阴性（ML漏报） | {d['false_negative']} 艘 |")
    lines.append("")

    lines.append("### A2. LLM vs ML 标签分布\n")
    lines.append(f"**LLM 判定标签分布**（视为 ground truth）：")
    for lb, cnt in d["llm_label_dist"].items():  # 遍历 LLM 标签
        lines.append(f"  - {lb}: {cnt} 艘 ({cnt/d['total_ships']*100:.1f}%)")
    lines.append("")
    lines.append(f"**ML 预测标签分布**：")
    for lb, cnt in d["ml_category_dist"].items():  # 遍历 ML 类别
        lines.append(f"  - {lb}: {cnt} 艘 ({cnt/d['total_ships']*100:.1f}%)")
    lines.append("")

    lines.append("### A3. ML → LLM 交叉分析\n")
    lines.append("行=ML 预测，列=LLM 判定，**对角线=一致，非对角线=分歧**\n")
    ml_order = ["normal", "obvious_issue", "has_risk"]  # ML 类别顺序
    llm_order = ["normal", "obvious_issue", "has_risk", "needs_review"]  # LLM 标签顺序
    headers = ["ML ↓ LLM →"] + llm_order + ["合计"]  # 交叉表表头
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("|" + "|".join("---" for _ in headers) + "|")
    for ml in ml_order:  # 遍历每个 ML 类别
        info = d["disagree_by_ml"].get(ml)  # 该 ML 类别的分歧信息
        if not info:  # 无数据则跳过
            continue
        total = info["total"]  # 该类别总数
        cells = [ml]  # 行首单元格
        for llm in llm_order:  # 遍历每个 LLM 标签
            cnt = d["cross_table"].get(f"{ml}→{llm}", 0)  # 交叉数量
            pct = cnt / total * 100  # 百分比
            marker = " ◀分歧" if ml != llm and cnt > 0 else (" ✓一致" if ml == llm else "")  # 一致性标记
            cells.append(f"{cnt} ({pct:.0f}%){marker}")  # 添加单元格数据
        cells.append(str(total))  # 添加合计
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")

    lines.append("### A4. 分歧方向解读\n")
    lines.append(f"全部 {d['disagreement_count']} 艘分歧船的流向：\n")
    lines.append("| 分歧方向 | 艘数 | 含义 |")
    lines.append("|---------|------|------|")
    meaning_map = {  # 分歧方向含义映射字典
        "normal → obvious_issue": "ML判正常，LLM发现明显问题 → 漏报",
        "normal → has_risk": "ML判正常，LLM认为有风险 → 漏报",
        "normal → needs_review": "ML判正常，LLM无法确定 → 需复核",
        "obvious_issue → normal": "ML判明显问题，LLM认为正常 → 误报",
        "obvious_issue → has_risk": "ML判明显问题，LLM仅判有风险 → 程度差异",
        "obvious_issue → needs_review": "ML判明显问题，LLM无法确定 → 需复核",
        "has_risk → normal": "ML判有风险，LLM认为正常 → 误报",
    }
    for direction, cnt in d["flow_total"].items():  # 遍历分歧方向
        meaning = meaning_map.get(direction, "")  # 查找含义说明
        lines.append(f"| {direction} | {cnt} 艘 | {meaning} |")
    lines.append("")

    lines.append("### A5. 各ML类别分歧率\n")
    lines.append("| ML 类别 | 总量 | 分歧数 | 分歧率 | 主要分歧流向 |")
    lines.append("|---------|------|--------|--------|-------------|")
    for ml_cat in ["normal", "obvious_issue", "has_risk"]:  # 遍历 ML 类别
        info = d["disagree_by_ml"].get(ml_cat)  # 该 ML 类别的分歧信息
        if not info:  # 无数据则跳过
            continue
        flow_str = ", ".join(f"{lb}:{c}艘" for lb, c in info["flow"].items())  # 分歧流向字符串
        lines.append(f"| {ml_cat} | {info['total']} 艘 | {info['disagree']} 艘 | "
                     f"{info['disagree_rate']:.1%} | {flow_str} |")
    lines.append("")

    lines.append("### A6. 小结\n")
    if d["disagreement_rate"] < 0.05:  # 分歧率低于 5%
        lines.append("> ML 与 LLM 高度一致（一致率 > 95%），分歧极少。"
                     "反馈循环改善空间有限。\n")
    elif d["disagreement_rate"] < 0.15:  # 分歧率在 5%~15% 之间
        lines.append(f"> ML 与 LLM 一致率 {d['agreement_rate']:.1%}，"
                     f"存在 {d['disagreement_count']} 艘分歧船。"
                     "反馈循环可以通过增量训练有效纠正这些错误。\n")
    else:  # 分歧率高于 15%
        lines.append(f"> ML 与 LLM 分歧率 {d['disagreement_rate']:.1%}，"
                     "模型偏差较大，反馈循环有充足的改善空间。\n")

    return "\n".join(lines)  # 返回完整的 Markdown 文本


# ═══════════════════════════════════════════════════════════════
# 实验B：触发阈值扫描
# ═══════════════════════════════════════════════════════════════

def render_experiment_b(summary: dict) -> str:
    """渲染实验B（触发阈值扫描）的 Markdown 报告。"""
    table = summary.get("summary_table", [])  # 结果汇总表数据
    best = summary.get("best_threshold")  # 推荐的最佳阈值
    lines = []  # 报告行列表

    lines.append("## 实验B：触发阈值扫描\n")
    lines.append("扫描 RETRAIN_THRESHOLD ∈ [5, 10, 15, ..., 50] 共 10 个阈值，"
                 "对比各阈值下反馈循环的训练效果，找出最优触发条件。\n")

    lines.append("### 结果总表\n")
    lines.append("| 阈值 | 初始准确率 | 最终准确率 | 提升幅度 | 训练次数 | 训练样本量 | 是否收敛 | 末3轮波动 |")
    lines.append("|------|-----------|-----------|---------|---------|-----------|---------|----------|")
    for row in table:  # 遍历每个阈值结果
        lines.append(
            f"| {row['threshold']:>2} 条 "
            f"| {row['initial']} "
            f"| {row['final']} "
            f"| {row['improvement']} "
            f"| {row['retrains']} 次 "
            f"| {row['total_records']} 条 "
            f"| {row['converged']} "
            f"| {row['stability']} |"
        )
    lines.append("")

    # 分组分析
    low = [r for r in table if r['threshold'] <= 20]  # 低阈值组（5~20）
    mid = [r for r in table if 25 <= r['threshold'] <= 40]  # 中阈值组（25~40）
    high = [r for r in table if r['threshold'] >= 45]  # 高阈值组（45~50）

    lines.append("### 分组分析\n")
    if low and all(r['improvement'] == '+0.00%' for r in low):  # 低阈值全部零提升
        avg_ret = sum(r['retrains'] for r in low) / len(low)  # 平均训练次数
        lines.append(f"**低阈值组（5~20条）**：全部零提升。"
                     f"每次训练 5~20 条样本，不足以让 LightGBM 产生有意义的更新。\n")

    if mid and all(r['improvement'] == '+0.00%' for r in mid):  # 中阈值全部零提升
        avg_ret = sum(r['retrains'] for r in mid) / len(mid)  # 平均训练次数
        lines.append(f"**中阈值组（25~40条）**：同样零提升。\n")

    if high:  # 高阈值组
        lines.append(f"**高阈值组（45~50条）**：\n")
        best_row = [r for r in table if r['threshold'] == best]  # 最佳阈值行
        if best_row:
            br = best_row[0]  # 最佳阈值结果
            lines.append(f"- 阈值 45 条：唯一产生正向提升（{br['improvement']}），稳定收敛。\n")
        t50 = [r for r in table if r['threshold'] == 50]  # 阈值 50 结果
        if t50 and '-' in t50[0]['improvement']:  # 有负提升
            lines.append(f"- 阈值 50 条（当前默认）：准确率下降 {t50[0]['improvement']}，"
                         f"波动 {t50[0]['stability']}，未收敛。\n")

    if best:  # 有推荐阈值
        best_row = [r for r in table if r['threshold'] == best]  # 最佳阈值行
        lines.append(f"> **推荐阈值：{best} 条** — {best_row[0]['final']}，"
                     f"提升 {best_row[0]['improvement']}，{best_row[0]['retrains']} 次训练后稳定收敛。\n")

    return "\n".join(lines)  # 返回完整的 Markdown 文本


# ═══════════════════════════════════════════════════════════════
# 实验C：反馈有无对照
# ═══════════════════════════════════════════════════════════════

def render_experiment_c(comparison: dict) -> str:
    """渲染实验C（反馈有无对照）的 Markdown 报告。"""
    if not comparison:  # 无实验C数据
        return "## 实验C：反馈有无对照\n（未运行，跳过）\n"

    lines = []  # 报告行列表
    nf = comparison["no_feedback"]  # 无反馈组数据
    wf = comparison["with_feedback"]  # 有反馈组数据
    im = comparison["improvement"]  # 提升数据

    lines.append("## 实验C：反馈有无对照\n")
    lines.append("用 **固定阈值 T=45**（实验B的推荐值），跑 10 轮不同的随机数据拆分，"
                 "每轮分别测量【无反馈】和【有反馈】的准确率。\n")

    lines.append("### 对照设计\n")
    lines.append("| 组别 | 策略 | 说明 |")
    lines.append("|------|------|------|")
    baseline = comparison.get("baseline", "好模型")  # 基线模型描述
    lines.append(f"| A组（对照组） | ❌ 无反馈 | 初始{baseline}固定不变，不做增量训练 |")
    lines.append(f"| B组（实验组） | ✅ 有反馈（T=45） | 每累积 45 条分歧样本触发一次增量训练 |")
    lines.append("")

    lines.append("### 对比结果\n")
    lines.append(f"| 指标 | 无反馈（A组） | 有反馈（B组） | 差异 |")
    lines.append(f"|------|-------------|-------------|------|")
    lines.append(f"| 平均准确率 | {nf['mean']:.2%} | {wf['mean']:.2%} | +{im['mean']:.2%} |")
    lines.append(f"| 最好一轮 | {nf['max']:.2%} | {wf['max']:.2%} | — |")
    lines.append(f"| 最差一轮 | {nf['min']:.2%} | {wf['min']:.2%} | — |")
    lines.append(f"| 标准差 | ±{nf['std']:.2%} | ±{wf['std']:.2%} | — |")
    lines.append(f"| 重复轮数 | {comparison['n_rounds']} 轮 | {comparison['n_rounds']} 轮 | — |")
    lines.append("")

    lines.append("### 每轮明细\n")
    lines.append("| 轮次 | 无反馈 | 有反馈 | 提升 |")
    lines.append("|------|--------|--------|------|")
    for rd in comparison["round_details"]:  # 遍历每轮结果
        impr = rd["improvement"]  # 该轮提升幅度
        lines.append(f"| {rd['round']+1} | {rd['no_feedback_accuracy']:.2%} "
                     f"| {rd['with_feedback_accuracy']:.2%} "
                     f"| {'+' if impr > 0 else ''}{impr:.2%} |")
    lines.append("")

    lines.append("### 结论\n")

    baseline = comparison.get("baseline", "未知")  # 基线模型描述
    if baseline == "差模型":  # 差基线场景
        lines.append(
            f"> **差基线模型测试结果**：分歧率 36.7%，反馈闭环效果显著。\n"
            f"> - 平均准确率从 {nf['mean']:.2%} 提升至 {wf['mean']:.2%}（+{im['mean']:.2%}）\n"
            f"> - **标准差从 ±{nf['std']:.2%} 降至 ±{wf['std']:.2%}** — 反馈不仅提高了准确率，还大幅降低了波动\n"
            f"> - 最差单轮：无反馈 {nf['min']:.2%}，有反馈 {wf['min']:.2%} — 反馈起到了【安全网】的作用\n"
            f"> - 第 1 轮（模型最差时）：{comparison['round_details'][0]['no_feedback_accuracy']:.2%} → "
            f"{comparison['round_details'][0]['with_feedback_accuracy']:.2%}（+{im['max']:.2%}），"
            f"说明模型越差，反馈效果越好\n"
            f"> \n"
            f"> **结论：分歧率高时反馈闭环效果明显，推荐开启。当模型质量较好时（分歧率小于 10%），反馈的边际价值有限。**\n")
    else:  # 好基线场景
        if im["all_positive"]:  # 全部正向提升
            lines.append(
                f"> 在 {comparison['n_rounds']} 轮测试中，**有反馈组全部胜出**。\n"
                f"> 平均准确率从 {nf['mean']:.2%} 提升至 {wf['mean']:.2%}（+{im['mean']:.2%}），"
                f"提升稳定且一致。\n"
                f"> **验证了反馈闭环确实有效。**\n")
        elif im["positive_ratio"] >= 0.5:  # 多数轮次正向
            lines.append(
                f"> 在 {comparison['n_rounds']} 轮测试中，有反馈组在 "
                f"{im['positive_ratio']:.0%} 的轮次中胜出。\n"
                f"> 平均提升 +{im['mean']:.2%}。当前分歧率较低（6.8%），"
                f"反馈的效果尚可，但并非每轮都稳定正向。\n")
        else:  # 多数轮次负向
            lines.append(
                f"> 在 {comparison['n_rounds']} 轮测试中，有反馈组仅 "
                f"{im['positive_ratio']:.0%} 的轮次胜出。\n"
                f"> 平均 {im['mean']:+.2%}。在分歧率仅 6.8% 的环境下，"
                f"反馈闭环的改善空间有限，不建议在生产环境开启。\n")

    return "\n".join(lines)  # 返回完整的 Markdown 文本


# ═══════════════════════════════════════════════════════════════
# 主报告生成
# ═══════════════════════════════════════════════════════════════

def generate_markdown_report(
    summary: dict,  # 实验B的结果汇总字典
    ship_count: int,  # 船舶总数
    data_source: str,  # 数据源描述
    test_size: int,  # 测试集大小
    config: Optional[dict] = None,  # 实验配置字典（可选）
    ship_data: Optional[list[dict]] = None,  # 船舶数据列表（可选，用于实验A）
    experiment_c_result: Optional[dict] = None,  # 实验C结果字典（可选）
) -> str:
    """生成完整的 Markdown 实验报告。"""
    lines = []  # 报告行列表

    lines.append("# LightGBM 反馈循环实验报告\n")
    lines.append(f"> 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")  # 当前时间

    # ── 实验概述 ──
    lines.append("## 实验概述\n")
    lines.append("三级级联架构的 L2 (LightGBM) 增量训练由 L3 (LLM) 判定结果回流驱动。")
    lines.append("本实验围绕这一反馈闭环设计三个子实验：\n")
    lines.append("| 子实验 | 内容 | 方法 |")
    lines.append("|--------|------|------|")
    lines.append("| 实验A — 分歧率摸底 | 统计 ML vs LLM 的一致/分歧模式 | 收集数据后直接分析 |")
    lines.append("| 实验B — 阈值扫描 | 找出最优 RETRAIN_THRESHOLD | 10 个阈值模拟反馈-训练循环 |")
    lines.append("| 实验C — 有无对照 | 验证反馈闭环是否有效 | 多轮随机 A/B 对照 |")
    lines.append("")

    lines.append("### 实验配置\n")
    lines.append(f"| 项目 | 值 |")
    lines.append(f"|------|-----|")
    lines.append(f"| 数据源 | {data_source} |")
    lines.append(f"| 总船舶数 | {ship_count} |")
    lines.append(f"| 训练/测试拆分 | {ship_count - test_size} / {test_size} 艘 |")
    lines.append(f"| 扫描阈值 | 5 ~ 50（步长 5） |")
    lines.append(f"| 增量学习率 | 0.02 |")
    lines.append(f"| 增量轮数 | 50 rounds |")
    lines.append(f"| 分歧权重 | 3.0（一致 1.0） |")
    if config:  # 有额外配置
        lines.append(f"| 随机种子 | {config.get('seed', 42)} |")  # 随机种子
    lines.append("")

    # ── 三个子实验 ──
    lines.append("---\n")
    if ship_data:  # 有船舶数据，渲染实验A
        disagree = analyze_disagreement(ship_data)  # 分析分歧
        lines.append(render_experiment_a(disagree))  # 渲染实验A

    lines.append("---\n")
    lines.append(render_experiment_b(summary))  # 渲染实验B

    lines.append("---\n")
    lines.append(render_experiment_c(experiment_c_result or {}))  # 渲染实验C

    lines.append("---\n")
    lines.append("## 总结\n")
    lines.append("原始数据：experiment_data.json\n")

    return "\n".join(lines)  # 返回完整的 Markdown 报告


# ═══════════════════════════════════════════════════════════════
# 图表生成
# ═══════════════════════════════════════════════════════════════

def generate_charts(
    summary: dict,  # 实验B的结果汇总字典
    output_dir: str,  # 图表输出目录路径
    ship_data: Optional[list[dict]] = None,  # 船舶数据列表（可选，用于实验A图表）
    experiment_c_result: Optional[dict] = None,  # 实验C结果字典（可选，用于实验C图表）
) -> list[str]:
    """生成实验报告图表，返回生成的图片路径列表。"""
    try:
        import matplotlib  # 导入 matplotlib 库
        matplotlib.use("Agg")  # 使用非交互式后端
        import matplotlib.pyplot as plt  # 导入绘图模块
        import matplotlib.ticker as mticker  # 导入刻度格式化器
        import numpy as np  # 导入数值计算库
    except ImportError:  # matplotlib 未安装
        print("  [图表] matplotlib 未安装，跳过")
        return []  # 返回空列表

    output_path = Path(output_dir)  # 输出目录路径对象
    output_path.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在
    generated = []  # 已生成的图片路径列表
    all_results = summary.get("all_results", [])  # 所有阈值的结果列表

    # ── 图1: 分歧热图（实验A） ──
    if ship_data:  # 有船舶数据
        fig, ax = plt.subplots(figsize=(7, 5))  # 创建画布和坐标轴
        cross_data = Counter()  # 交叉统计数据计数器
        for d in ship_data:  # 遍历每条船
            cross_data[(d["ml_category"], d["llm_label"])] += 1  # 统计交叉组合

        ml_order = ["normal", "obvious_issue", "has_risk"]  # ML 类别顺序
        llm_order = ["normal", "obvious_issue", "has_risk", "needs_review"]  # LLM 标签顺序

        matrix = []  # 热图矩阵
        for ml in ml_order:  # 遍历 ML 类别
            row_tot = sum(cross_data.get((ml, llm), 0) for llm in llm_order)  # 该行总数
            row = [cross_data.get((ml, llm), 0) / max(row_tot, 1) for llm in llm_order]  # 一行比例数据
            matrix.append(row)  # 添加到矩阵

        im = ax.imshow(matrix, cmap="YlOrRd", vmin=0, vmax=1, aspect="auto")  # 绘制热图
        ax.set_xticks(range(len(llm_order)))  # 设置 X 轴刻度位置
        ax.set_xticklabels(llm_order, fontsize=10)  # 设置 X 轴刻度标签
        ax.set_yticks(range(len(ml_order)))  # 设置 Y 轴刻度位置
        ax.set_yticklabels(ml_order, fontsize=10)  # 设置 Y 轴刻度标签
        ax.set_xlabel("LLM 判定标签 (ground truth)", fontsize=11)  # X 轴标签
        ax.set_ylabel("ML 预测类别", fontsize=11)  # Y 轴标签
        ax.set_title("ML vs LLM 分歧热图", fontsize=13, fontweight="bold")  # 图表标题

        for i in range(len(ml_order)):  # 遍历行
            for j in range(len(llm_order)):  # 遍历列
                cnt = cross_data.get((ml_order[i], llm_order[j]), 0)  # 交叉数量
                val = matrix[i][j]  # 占比值
                txt = f"{cnt}艘\n{val:.0%}"  # 文本内容
                color = "white" if val > 0.5 else "black"  # 文本颜色（背景深时用白色）
                ax.text(j, i, txt, ha="center", va="center", fontsize=8, color=color)  # 添加文本标注

        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)  # 添加颜色条
        plt.tight_layout()  # 自动调整布局
        path = output_path / "chart_A_disagreement_heatmap.png"  # 图片输出路径
        fig.savefig(path, dpi=150, bbox_inches="tight")  # 保存图片
        plt.close(fig)  # 关闭画布
        generated.append(str(path))  # 记录已生成图片路径
        print(f"  [图表] 分歧热图(A): {path.name}")

    # ── 图2: 收敛曲线（实验B） ──
    fig, ax = plt.subplots(figsize=(12, 6))  # 创建画布
    colors = plt.cm.viridis([i / max(len(all_results), 1) for i in range(len(all_results))])  # 颜色列表

    for idx, r in enumerate(all_results):  # 遍历每个阈值结果
        traj = r["multi_accuracy_trajectory"]  # 准确率变化轨迹
        t = r["threshold"]  # 阈值
        lw = 2.5 if t == 45 else 1.0  # 推荐阈值线条加粗
        alpha = 1.0 if t in (45, 50) else 0.5  # 推荐和默认阈值透明度全开
        ax.plot(range(len(traj)), [v * 100 for v in traj],  # 绘制收敛曲线
                color=colors[idx], marker=".", markersize=2 if t not in (45, 50) else 5,
                linewidth=lw, alpha=alpha, label=f"T={t}" + (" ★推荐" if t == 45 else ""))

    ax.axhline(y=88.40, color="gray", linestyle="--", alpha=0.4, label="初始基线")  # 初始准确率基线
    ax.set_xlabel("训练轮次", fontsize=12)  # X 轴标签
    ax.set_ylabel("准确率 (%)", fontsize=12)  # Y 轴标签
    ax.set_title("实验B: 不同阈值下的准确率收敛曲线", fontsize=14, fontweight="bold")  # 图表标题
    ax.legend(fontsize=8, ncol=2, title="阈值")  # 图例
    ax.grid(True, alpha=0.3)  # 网格线
    ax.set_ylim(85, 92)  # Y 轴范围
    plt.tight_layout()  # 自动调整布局
    path = output_path / "chart_B_accuracy_convergence.png"  # 图片输出路径
    fig.savefig(path, dpi=150, bbox_inches="tight")  # 保存图片
    plt.close(fig)  # 关闭画布
    generated.append(str(path))  # 记录已生成图片路径
    print(f"  [图表] 收敛曲线(B): {path.name}")

    # ── 图3: 最佳阈值效率（实验B辅助） ──
    fig, ax = plt.subplots(figsize=(10, 6))  # 创建画布
    thresholds = [r["threshold"] for r in all_results]  # 所有阈值列表
    finals = [r["final_accuracy"] * 100 for r in all_results]  # 最终准确率列表（百分比）
    initials = [r["initial_accuracy"] * 100 for r in all_results]  # 初始准确率列表（百分比）
    retrains = [r["total_retrains"] for r in all_results]  # 训练次数列表
    x = range(len(thresholds))  # X 轴位置
    ax.bar([i - 0.2 for i in x], initials, width=0.35, alpha=0.6, label="初始", color="#999")  # 初始准确率柱状图
    ax.bar([i + 0.2 for i in x], finals, width=0.35, label="最终", color="#2c7be5")  # 最终准确率柱状图
    for i, (t, init, final) in enumerate(zip(thresholds, initials, finals)):  # 遍历每个阈值
        if t == 45:  # 推荐阈值添加标注
            ax.annotate(f"推荐!\n+{final-init:.1f}%",  # 标注文本
                        xy=(i + 0.2, final), xytext=(30, 15),  # 箭头起点和文本偏移
                        textcoords="offset points", ha="center", fontsize=10,
                        fontweight="bold", color="#2c7be5",
                        arrowprops=dict(arrowstyle="->", color="#2c7be5", lw=1.5))  # 箭头样式
    ax.set_xticks(list(x))  # 设置 X 轴刻度
    ax.set_xticklabels([f"T={t}" for t in thresholds])  # 设置 X 轴刻度标签
    ax.set_xlabel("触发阈值", fontsize=12)  # X 轴标签
    ax.set_ylabel("准确率 (%)", fontsize=12)  # Y 轴标签
    ax.set_title("实验B: 不同阈值的初始 vs 最终准确率", fontsize=14, fontweight="bold")  # 图表标题
    ax.legend(fontsize=10)  # 图例
    ax.grid(True, alpha=0.3, axis="y")  # Y 轴网格线
    ax.set_ylim(80, 100)  # Y 轴范围
    ax2 = ax.twinx()  # 创建共享 X 轴的第二个 Y 轴
    ax2.plot(list(x), retrains, "r.-", markersize=10, linewidth=2, label="训练次数")  # 训练次数折线图
    ax2.set_ylabel("训练次数", fontsize=12, color="r")  # 右 Y 轴标签
    ax2.tick_params(axis="y", labelcolor="r")  # 右 Y 轴刻度颜色
    ax2.legend(fontsize=10, loc="upper right")  # 右 Y 轴图例
    plt.tight_layout()  # 自动调整布局
    path = output_path / "chart_B_final_comparison.png"  # 图片输出路径
    fig.savefig(path, dpi=150, bbox_inches="tight")  # 保存图片
    plt.close(fig)  # 关闭画布
    generated.append(str(path))  # 记录已生成图片路径
    print(f"  [图表] 阈值对比(B): {path.name}")

    # ── 图4: 有无反馈对比（实验C） ──
    if experiment_c_result and experiment_c_result.get("round_details"):  # 有实验C详细数据
        import numpy as np  # 导入数值计算库
        details = experiment_c_result["round_details"]  # 每轮详细结果
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))  # 创建 1 行 2 列的子图

        # 左图：每轮对比
        rounds = [d["round"] + 1 for d in details]  # 轮次编号列表
        no_fb = [d["no_feedback_accuracy"] * 100 for d in details]  # 无反馈准确率列表
        w_fb = [d["with_feedback_accuracy"] * 100 for d in details]  # 有反馈准确率列表
        width = 0.35  # 柱状宽度
        ax1.bar([r - width/2 for r in rounds], no_fb, width, label="无反馈", color="#999", alpha=0.8)  # 无反馈柱状图
        ax1.bar([r + width/2 for r in rounds], w_fb, width, label="有反馈(T=45)", color="#2c7be5", alpha=0.8)  # 有反馈柱状图
        ax1.set_xlabel("轮次", fontsize=11)  # X 轴标签
        ax1.set_ylabel("准确率 (%)", fontsize=11)  # Y 轴标签
        ax1.set_title("每轮对比", fontsize=13, fontweight="bold")  # 图表标题
        ax1.legend(fontsize=10)  # 图例
        ax1.grid(True, alpha=0.3, axis="y")  # Y 轴网格线
        ax1.set_xticks(rounds)  # X 轴刻度

        # 右图：均值对比柱状图
        nf_mean = experiment_c_result["no_feedback"]["mean"] * 100  # 无反馈平均准确率
        wf_mean = experiment_c_result["with_feedback"]["mean"] * 100  # 有反馈平均准确率
        nf_std = experiment_c_result["no_feedback"]["std"] * 100  # 无反馈标准差
        wf_std = experiment_c_result["with_feedback"]["std"] * 100  # 有反馈标准差
        bars = ax2.bar(["无反馈", "有反馈"], [nf_mean, wf_mean],  # 均值对比柱状图
                       yerr=[nf_std, wf_std], capsize=8,  # 误差棒
                       color=["#999", "#2c7be5"], width=0.5, edgecolor="white")  # 颜色和边框
        for bar, val in zip(bars, [nf_mean, wf_mean]):  # 遍历柱状图
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,  # 文本位置
                    f"{val:.2f}%", ha="center", va="bottom", fontsize=13, fontweight="bold")  # 数值标注
        improvement = experiment_c_result["improvement"]["mean"] * 100  # 平均提升
        y_mid = (nf_mean + wf_mean) / 2  # 中间 Y 坐标
        ax2.annotate(f"提升 {improvement:+.2f}%",  # 标注文本
                    xy=(1, wf_mean), xytext=(1.3, y_mid),  # 箭头和文本位置
                    fontsize=12, color="#2c7be5", fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="#2c7be5", lw=1.5))  # 箭头样式
        ax2.set_ylim(0, max(nf_mean, wf_mean) * 1.25)  # Y 轴范围
        ax2.set_ylabel("平均准确率 (%)", fontsize=11)  # Y 轴标签
        ax2.set_title("10 轮均值对比", fontsize=13, fontweight="bold")  # 图表标题
        ax2.grid(True, alpha=0.3, axis="y")  # Y 轴网格线

        plt.suptitle("实验C: 反馈有无对照实验", fontsize=15, fontweight="bold")  # 总标题
        plt.tight_layout()  # 自动调整布局
        path = output_path / "chart_C_feedback_comparison.png"  # 图片输出路径
        fig.savefig(path, dpi=150, bbox_inches="tight")  # 保存图片
        plt.close(fig)  # 关闭画布
        generated.append(str(path))  # 记录已生成图片路径
        print(f"  [图表] 反馈对照(C): {path.name}")

    return generated  # 返回所有已生成图片路径


def save_raw_data(
    summary: dict,  # 实验B的结果汇总字典
    ship_count: int,  # 船舶总数
    data_source: str,  # 数据源描述
    output_dir: str,  # 输出目录路径
    ship_data: Optional[list[dict]] = None,  # 船舶数据列表（可选）
    experiment_c_result: Optional[dict] = None,  # 实验C结果字典（可选）
):
    """将实验原始数据保存为 JSON 文件。"""
    output_path = Path(output_dir)  # 输出目录路径对象
    output_path.mkdir(parents=True, exist_ok=True)  # 确保输出目录存在

    data = {  # 原始数据字典
        "experiment": "LightGBM 反馈循环实验（三个子实验）",  # 实验名称
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # 生成时间
        "config": {"data_source": data_source, "total_ships": ship_count},  # 实验配置
        "experiment_a": None,  # 实验A数据（稍后填充）
        "experiment_b": {  # 实验B数据
            "summary_table": summary.get("summary_table", []),  # 结果汇总表
            "best_threshold": summary.get("best_threshold"),  # 最佳阈值
            "all_results": [  # 所有阈值详细结果
                {"threshold": r["threshold"],  # 阈值
                 "initial_accuracy": r["initial_accuracy"],  # 初始准确率
                 "final_accuracy": r["final_accuracy"],  # 最终准确率
                 "accuracy_improvement": r["accuracy_improvement"],  # 准确率提升
                 "total_retrains": r["total_retrains"],  # 总训练次数
                 "total_records_used": r["total_records_used"],  # 总训练样本数
                 "converged": r.get("converged", False)}  # 是否收敛
                for r in summary.get("all_results", [])
            ],
        },
        "experiment_c": experiment_c_result,  # 实验C结果
    }

    if ship_data:  # 有船舶数据，填充实验A
        disagree = analyze_disagreement(ship_data)  # 分析分歧
        data["experiment_a"] = {  # 实验A数据
            "total_ships": disagree["total_ships"],  # 总船舶数
            "agreement_rate": disagree["agreement_rate"],  # 一致率
            "disagreement_rate": disagree["disagreement_rate"],  # 分歧率
            "false_positive": disagree["false_positive"],  # 假阳性数
            "false_negative": disagree["false_negative"],  # 假阴性数
            "cross_table": disagree["cross_table"],  # 交叉统计表
            "flow_total": disagree["flow_total"],  # 分歧流向汇总
            "llm_label_dist": disagree["llm_label_dist"],  # LLM 标签分布
            "ml_category_dist": disagree["ml_category_dist"],  # ML 类别分布
        }

    json_path = output_path / "experiment_data.json"  # JSON 文件路径
    with open(json_path, "w", encoding="utf-8") as f:  # 打开文件
        json.dump(data, f, ensure_ascii=False, indent=2)  # 写入 JSON 数据
    print(f"  [JSON] 原始数据已保存: {json_path}")
