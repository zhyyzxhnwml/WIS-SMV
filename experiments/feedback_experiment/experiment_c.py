"""
experiments/feedback_experiment/experiment_c.py — 实验C: 反馈有无对照
====================================================================

通过多次随机拆分（Bootstrap 方式），定量对比：
    - 无反馈：静态模型，不做任何增量训练
    - 有反馈：使用实验B推荐阈值，模拟完整的反馈-训练循环

与实验B的区别：
    实验B 扫描 10 个阈值，各跑一次，目的是找最优阈值。
    实验C 固定 T=45（实验B推荐值），跑多次不同随机拆分，
    用均值和区间来证明"有反馈确实比无反馈好"。
"""

from __future__ import annotations

import sys
import random
from pathlib import Path
from typing import Optional

import numpy as np


def _ensure_imports():
    project_root = Path(__file__).resolve().parent.parent.parent  # 项目根目录路径
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))  # 将项目根目录加入 Python 模块搜索路径


def run_comparison(
    ship_data: list[dict],  # Phase 1 收集的船舶数据列表
    base_model_path: str,  # 基础模型文件路径
    threshold: int = 45,  # 触发增量训练的阈值（默认用实验B推荐值 45）
    n_rounds: int = 10,  # 重复实验轮数
    train_ratio: float = 0.7,  # 训练池比例
    seed: int = 42,  # 随机种子
    use_bad_baseline: bool = False,  # 是否使用差模型基线
) -> dict:
    """
    运行"有无反馈"对照实验。

    对每轮随机拆分：
        1. 初始模型评估测试集 → no_feedback_acc
        2. 模拟反馈闭环（阈值=T）训练后评估 → with_feedback_acc
    最终汇总 n_rounds 轮结果。

    参数:
        ship_data: Phase 1 收集的船舶数据
        base_model_path: 基础模型路径
        threshold: 触发阈值（用实验B的推荐值）
        n_rounds: 重复轮数
        train_ratio: 训练池比例

    返回:
        dict: 对照结果
    """
    from triage_engine.model import TriageModel, LABELS  # ML 模型类和标签列表
    from .config import INCREMENTAL_NUM_BOOST_ROUNDS, INCREMENTAL_LEARNING_RATE, BAD_MODEL_PATH  # 增量训练超参和差模型路径

    # 确定使用哪个模型
    if use_bad_baseline:  # 使用差模型基线
        actual_model_path = BAD_MODEL_PATH  # 差模型文件路径
        baseline_label = "差模型"  # 基线模型描述
    else:  # 使用好模型基线
        actual_model_path = base_model_path  # 好模型文件路径
        baseline_label = "好模型"  # 基线模型描述

    # 标签索引映射
    label_to_idx = {lb: i for i, lb in enumerate(LABELS)}  # 标签名称到索引的映射字典

    results = []  # 各轮实验结果列表
    base_rng = random.Random(seed)  # 基础随机数生成器

    print(f"\n{'='*60}")
    print(f"  [实验C] 反馈有无对照实验")
    print(f"{'='*60}")
    print(f"  数据: {len(ship_data)} 艘船舶")
    print(f"  阈值: {threshold} 条")
    print(f"  基线: {baseline_label} ({actual_model_path})")
    print(f"  重复轮数: {n_rounds}")
    print(f"  训练比例: {train_ratio:.0%}")
    print()

    for round_idx in range(n_rounds):  # 每轮实验
        # ── 数据拆分（每轮不同随机种子） ──
        rng = random.Random(base_rng.randint(0, 2**31))  # 生成该轮的随机数生成器
        indices = list(range(len(ship_data)))  # 船舶索引列表
        rng.shuffle(indices)  # 打乱索引顺序

        split = int(len(indices) * train_ratio)  # 训练池大小
        train_idx = indices[:split]  # 训练池索引
        test_idx = indices[split:]  # 测试集索引
        train_pool = [ship_data[i] for i in train_idx]  # 训练池船舶数据
        test_set = [ship_data[i] for i in test_idx]  # 测试集船舶数据

        # ── A组：无反馈（静态模型） ──
        model_no_fb = TriageModel(model_path=actual_model_path)  # 无反馈组模型实例
        _orig_no = model_no_fb.predict_single  # 保存原始预测函数引用
        def _pad_no(fv):  # 带填充的预测包装函数
            if len(fv) == 22:  # 22 维特征
                fv = fv + [0.0] * 42  # 填充到 64 维
            return _orig_no(fv)  # 调用原始预测
        model_no_fb.predict_single = _pad_no  # 替换预测方法

        no_fb_correct = 0  # 无反馈组正确预测数
        for item in test_set:  # 遍历测试集
            fv = item["feature_vector"]  # 特征向量
            pred_cat, _, _ = model_no_fb.predict_single(fv)  # ML 预测类别
            if pred_cat == item["llm_label"]:  # 预测与真实标签一致
                no_fb_correct += 1  # 正确计数加一
        no_fb_acc = no_fb_correct / len(test_set)  # 无反馈组准确率

        # ── B组：有反馈（模拟反馈闭环） ──
        model_w_fb = TriageModel(model_path=actual_model_path)  # 有反馈组模型实例
        _orig_w = model_w_fb.predict_single  # 保存原始预测函数引用
        def _pad_w(fv):  # 带填充的预测包装函数
            if len(fv) == 22:  # 22 维特征
                fv = fv + [0.0] * 42  # 填充到 64 维
            return _orig_w(fv)  # 调用原始预测
        model_w_fb.predict_single = _pad_w  # 替换预测方法

        # 逐船模拟反馈闭环
        buffer: list[dict] = []  # 训练缓冲区（累积待训练样本）
        for ship in train_pool:  # 遍历训练池中的每条船
            fv = ship["feature_vector"]  # 特征向量
            true_label = ship["llm_label"]  # 真实标签（LLM 判定）

            pred_cat, _, _ = model_w_fb.predict_single(fv)  # 当前模型的预测类别

            llm_violation = ship.get("llm_violation_detected", False)  # LLM 是否判定违规
            ml_thinks_issue = pred_cat in ("obvious_issue", "has_risk", "needs_review")  # ML 是否认为有问题
            agreement = (ml_thinks_issue == llm_violation)  # ML 与 LLM 是否一致
            weight = 3.0 if not agreement else 1.0  # 分歧样本权重为 3.0，一致样本为 1.0

            label_idx = label_to_idx.get(true_label)  # 标签对应的索引
            if label_idx is None:  # 无效标签
                continue  # 跳过

            buffer.append({"fv": fv, "label_idx": label_idx, "weight": weight})  # 将样本加入缓冲区

            if len(buffer) >= threshold:  # 缓冲区达到触发阈值
                X_new = np.array([rec["fv"] for rec in buffer], dtype=np.float32)  # 特征矩阵
                y_new = np.array([rec["label_idx"] for rec in buffer], dtype=np.int32)  # 标签数组
                w_new = np.array([rec["weight"] for rec in buffer], dtype=np.float32)  # 权重数组
                w_new = np.clip(w_new, 0.1, 10.0)  # 将权重裁剪到 [0.1, 10.0] 区间

                model_w_fb.incremental_update(  # 执行增量训练
                    X_new, y_new,  # 特征和标签
                    sample_weight=w_new,  # 样本权重
                    num_boost_round=INCREMENTAL_NUM_BOOST_ROUNDS,  # boosting 轮数
                    learning_rate=INCREMENTAL_LEARNING_RATE,  # 学习率
                )
                buffer = []  # 清空缓冲区

        # 训练后评估
        w_fb_correct = 0  # 有反馈组正确预测数
        for item in test_set:  # 遍历测试集
            fv = item["feature_vector"]  # 特征向量
            pred_cat, _, _ = model_w_fb.predict_single(fv)  # ML 预测类别
            if pred_cat == item["llm_label"]:  # 预测与真实标签一致
                w_fb_correct += 1  # 正确计数加一
        w_fb_acc = w_fb_correct / len(test_set)  # 有反馈组准确率

        improvement = w_fb_acc - no_fb_acc  # 有反馈相对于无反馈的提升
        results.append({  # 记录该轮结果
            "round": round_idx,  # 轮次编号
            "no_feedback_accuracy": round(no_fb_acc, 4),  # 无反馈准确率
            "with_feedback_accuracy": round(w_fb_acc, 4),  # 有反馈准确率
            "improvement": round(improvement, 4),  # 提升幅度
            "test_size": len(test_set),  # 测试集大小
        })

        print(f"  第 {round_idx+1:>2} 轮: 无反馈={no_fb_acc:.2%}  "
              f"有反馈={w_fb_acc:.2%}  "
              f"{'+' if improvement > 0 else ''}{improvement:+.2%}")

    # ── 汇总 ──
    no_fb_vals = [r["no_feedback_accuracy"] for r in results]  # 所有轮次的无反馈准确率列表
    w_fb_vals = [r["with_feedback_accuracy"] for r in results]  # 所有轮次的有反馈准确率列表
    impr_vals = [r["improvement"] for r in results]  # 所有轮次的提升幅度列表

    summary = {  # 汇总结果字典
        "threshold": threshold,  # 使用的阈值
        "n_rounds": n_rounds,  # 重复轮数
        "baseline": baseline_label,  # 基线模型描述
        "base_model": actual_model_path,  # 使用的基础模型路径
        "no_feedback": {  # 无反馈组统计
            "mean": round(float(np.mean(no_fb_vals)), 4),  # 平均准确率
            "min": round(float(np.min(no_fb_vals)), 4),  # 最低准确率
            "max": round(float(np.max(no_fb_vals)), 4),  # 最高准确率
            "std": round(float(np.std(no_fb_vals)), 4),  # 标准差
        },
        "with_feedback": {  # 有反馈组统计
            "mean": round(float(np.mean(w_fb_vals)), 4),  # 平均准确率
            "min": round(float(np.min(w_fb_vals)), 4),  # 最低准确率
            "max": round(float(np.max(w_fb_vals)), 4),  # 最高准确率
            "std": round(float(np.std(w_fb_vals)), 4),  # 标准差
        },
        "improvement": {  # 提升幅度统计
            "mean": round(float(np.mean(impr_vals)), 4),  # 平均提升
            "min": round(float(np.min(impr_vals)), 4),  # 最低提升
            "max": round(float(np.max(impr_vals)), 4),  # 最大提升
            "all_positive": all(v > 0 for v in impr_vals),  # 是否所有轮次均为正提升
            "positive_ratio": sum(1 for v in impr_vals if v > 0) / len(impr_vals),  # 正向提升轮次比例
        },
        "round_details": results,  # 各轮详细结果
    }

    print(f"\n  ──── 汇总 ────")
    print(f"  无反馈:  {summary['no_feedback']['mean']:.2%} "
          f"({summary['no_feedback']['min']:.2%} ~ {summary['no_feedback']['max']:.2%})")
    print(f"  有反馈:  {summary['with_feedback']['mean']:.2%} "
          f"({summary['with_feedback']['min']:.2%} ~ {summary['with_feedback']['max']:.2%})")
    print(f"  提升:    {summary['improvement']['mean']:+.2%} "
          f"(范围: {summary['improvement']['min']:+.2%} ~ {summary['improvement']['max']:+.2%})")
    all_pos = summary['improvement']['all_positive']  # 是否全部正向
    print(f"  全部正向: [{'Y' if all_pos else 'N'}] "
          f"({summary['improvement']['positive_ratio']:.0%} 轮次为正)")

    return summary  # 返回汇总结果