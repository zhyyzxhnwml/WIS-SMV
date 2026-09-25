import copy  # 深拷贝模块
import random  # 随机数模块
import sys  # 系统模块，用于路径操作
from pathlib import Path  # 路径处理类
from typing import Optional  # 可选类型注解

import numpy as np  # 数值计算库


def _ensure_imports():
    """确保项目根目录在 sys.path 中。"""
    project_root = Path(__file__).resolve().parent.parent.parent  # 项目根目录的绝对路径
    if str(project_root) not in sys.path:  # 如不在路径列表中
        sys.path.insert(0, str(project_root))  # 将项目根目录插入到 sys.path 最前面


# ── 模型操作辅助函数 ──


def _load_base_model(model_path: str):
    """加载基础模型，并添加 22→64 维填充。"""
    from triage_engine.model import TriageModel  # 模型类

    if not Path(model_path).exists():  # 模型文件不存在
        raise FileNotFoundError(
            f"模型文件不存在: {model_path}\n"
            f"请先运行数据收集 (python -m experiments.feedback_experiment.run --collect)"
        )

    model = TriageModel(model_path=model_path)  # 加载模型实例

    # 给 predict_single 加填充包装（22→64 维）
    _orig = model.predict_single  # 保存原始预测函数引用

    def _padded_predict(fv):  # 填充包装函数
        if len(fv) == 22:  # 22 维特征向量
            fv = fv + [0.0] * 42  # 填充到 64 维
        return _orig(fv)  # 调用原始预测函数

    model.predict_single = _padded_predict  # 替换为包装后的预测函数

    print(f"  基础模型: {model_path} ({model.model.num_trees()} 棵树)")
    return model  # 返回加载的模型


def _evaluate_accuracy(model, test_data: list[dict]) -> dict:
    """
    在测试集上评估模型，返回多维度指标。

    test_data 的每条记录需包含:
        - feature_vector (list[float]): 64 维
        - llm_label (str): LLM 导出的 4 分类标签（作为 ground truth）

    返回:
        dict: 包含 multi_accuracy, binary_accuracy, per_class_accuracy, 等
    """
    from triage_engine.model import LABELS  # 4 分类标签列表

    if not test_data:  # 无测试数据
        return {"multi_accuracy": 0.0, "binary_accuracy": 0.0, "samples": 0}  # 返回零值结果

    multi_correct = 0  # 4 分类正确数
    binary_correct = 0  # 二分类正确数
    per_class = {lb: {"correct": 0, "total": 0} for lb in LABELS}  # 每类统计字典

    for item in test_data:  # 遍历测试集
        fv = item["feature_vector"]  # 特征向量
        true_label = item["llm_label"]  # LLM 标签（ground truth）

        # 当前模型预测
        pred_cat, pred_conf, all_probs = model.predict_single(fv)  # 预测类别、置信度、所有概率

        # 4 分类准确率
        if pred_cat == true_label:  # 预测正确
            multi_correct += 1
        per_class[true_label]["total"] += 1  # 该标签总数加 1
        if pred_cat == true_label:  # 预测匹配真实标签
            per_class[true_label]["correct"] += 1  # 该标签正确数加 1

        # 二分类准确率（ML 认为有问题 ↔ LLM 判定违规）
        llm_violation = item.get("llm_violation_detected", False)  # LLM 是否判定违规
        ml_thinks_issue = pred_cat in ("obvious_issue", "has_risk", "needs_review")  # ML 认为有问题
        true_issue = llm_violation  # 真实是否有问题
        if ml_thinks_issue == true_issue:  # 二分类一致
            binary_correct += 1

    n = len(test_data)  # 测试样本总数
    result = {  # 评估结果字典
        "multi_accuracy": round(multi_correct / n, 4),  # 4 分类准确率
        "multi_correct": multi_correct,  # 4 分类正确数
        "binary_accuracy": round(binary_correct / n, 4),  # 二分类准确率
        "binary_correct": binary_correct,  # 二分类正确数
        "samples": n,  # 样本总数
        "per_class": {},  # 各类别准确率（稍后填充）
    }
    for lb in LABELS:  # 遍历每个标签
        pc = per_class[lb]  # 该标签统计
        result["per_class"][lb] = {  # 该标签评估结果
            "accuracy": round(pc["correct"] / max(pc["total"], 1), 4),  # 该标签准确率
            "correct": pc["correct"],  # 该标签正确数
            "total": pc["total"],  # 该标签总数
        }

    return result  # 返回评估结果


# ── 单阈值模拟 ──


def simulate_single_threshold(
    base_model_path: str,  # 基础模型路径
    train_pool: list[dict],  # 训练池数据列表
    test_set: list[dict],  # 测试集数据列表
    threshold: int,  # 触发增量训练的累积样本数
    seed: int = 42,  # 随机种子
) -> dict:
    """
    对单个阈值模拟完整反馈-训练循环。

    参数:
        base_model_path: 基础模型路径
        train_pool: 训练池数据（将被随机打乱后逐船处理）
        test_set: 测试集数据（用于每轮训练后评估）
        threshold: 触发增量训练的累积样本数
        seed: 随机种子

    返回:
        dict: 模拟结果，包含:
            - threshold (int)
            - initial_accuracy (float)
            - final_accuracy (float)
            - accuracy_improvement (float)
            - multi_accuracy_trajectory (list[float])
            - binary_accuracy_trajectory (list[float])
            - total_retrains (int)
            - retrain_at_ship (list[int])
            - total_records_used (int)
            - converged (bool)
            - convergence_retrain (int): 在第几次重训后收敛
    """
    from triage_engine.model import LABELS  # 4 分类标签列表

    # 加载克隆模型（每次模拟从基础模型出发，不影响后续模拟）
    model = _load_base_model(base_model_path)  # 加载基础模型

    # 初始评估
    eval_result = _evaluate_accuracy(model, test_set)  # 在测试集上评估
    multi_trajectory = [eval_result["multi_accuracy"]]  # 多分类准确率轨迹列表
    binary_trajectory = [eval_result["binary_accuracy"]]  # 二分类准确率轨迹列表

    print(f"\n  ── 阈值 T={threshold} ──")
    print(f"    训练池: {len(train_pool)} 艘 | 测试集: {len(test_set)} 艘")
    print(f"    初始准确率: {eval_result['multi_accuracy']:.2%} "
          f"(multi) / {eval_result['binary_accuracy']:.2%} (binary)")

    # 打乱训练池顺序
    rng = random.Random(seed)  # 随机数生成器
    pool = list(train_pool)  # 训练池副本
    rng.shuffle(pool)  # 随机打乱

    # 训练缓冲区
    buffer: list[dict] = []  # 训练缓冲区列表
    retrain_count = 0  # 重训次数计数器
    retrain_at_ship: list[int] = []  # 每次重训时的船舶索引列表
    total_records_used = 0  # 总训练样本数
    converged = False  # 是否收敛标志
    convergence_retrain: Optional[int] = None  # 收敛时的重训次数
    no_improve_streak = 0  # 无改善连续次数计数器

    # ── 逐船模拟 ──
    for i, ship in enumerate(pool):  # 遍历训练池中的每条船
        fv = ship["feature_vector"]  # 特征向量
        true_label = ship["llm_label"]  # LLM 标签（ground truth）

        # 用当前模型预测
        pred_cat, _, _ = model.predict_single(fv)  # 预测类别

        # 判断 ML 是否与 LLM 一致（分歧→高权重）
        llm_violation = ship.get("llm_violation_detected", False)  # LLM 是否判定违规
        ml_thinks_issue = pred_cat in ("obvious_issue", "has_risk", "needs_review")  # ML 认为有问题
        agreement = (ml_thinks_issue == llm_violation)  # ML 与 LLM 是否一致
        weight = 3.0 if not agreement else 1.0  # 分歧权重 3.0，一致权重 1.0

        # 加入训练缓冲区
        try:
            label_idx = LABELS.index(true_label)  # 标签名转索引
        except ValueError:  # 无效标签
            continue  # 跳过无效标签

        buffer.append({  # 添加到缓冲区
            "fv": fv,  # 特征向量
            "label_idx": label_idx,  # 标签索引
            "weight": weight,  # 样本权重
        })

        # 检查是否达到触发阈值
        if len(buffer) >= threshold:  # 缓冲区累积达到阈值
            # ── 增量训练 ──
            from .config import INCREMENTAL_NUM_BOOST_ROUNDS, INCREMENTAL_LEARNING_RATE  # 训练配置

            X_new = np.array([rec["fv"] for rec in buffer], dtype=np.float32)  # 输入特征矩阵
            y_new = np.array([rec["label_idx"] for rec in buffer], dtype=np.int32)  # 标签向量
            w_new = np.array([rec["weight"] for rec in buffer], dtype=np.float32)  # 样本权重向量

            # 处理极端权重（防止数值不稳定）
            w_new = np.clip(w_new, 0.1, 10.0)  # 权重截断到 [0.1, 10.0]

            model.incremental_update(  # 增量更新模型
                X_new, y_new,  # 训练数据和标签
                sample_weight=w_new,  # 样本权重
                num_boost_round=INCREMENTAL_NUM_BOOST_ROUNDS,  # 提升轮数
                learning_rate=INCREMENTAL_LEARNING_RATE,  # 学习率
            )

            total_records_used += len(buffer)  # 累加训练样本数
            buffer = []  # 清空缓冲区
            retrain_count += 1  # 重训次数加 1
            retrain_at_ship.append(i)  # 记录重训时的船舶索引

            # ── 评估 ──
            eval_result = _evaluate_accuracy(model, test_set)  # 在测试集上评估
            new_multi_acc = eval_result["multi_accuracy"]  # 新的多分类准确率
            new_binary_acc = eval_result["binary_accuracy"]  # 新的二分类准确率
            multi_trajectory.append(new_multi_acc)  # 添加到轨迹列表
            binary_trajectory.append(new_binary_acc)  # 添加到轨迹列表

            prev_multi = multi_trajectory[-2] if len(multi_trajectory) >= 2 else 0  # 上一次准确率
            improvement = new_multi_acc - prev_multi  # 准确率提升幅度

            status = "\u2713" if improvement >= 0 else "\u25bd"  # 提升标志
            print(f"    [{retrain_count}] 训练后: {new_multi_acc:.2%} "
                  f"({status}{improvement:+.2%}) | {len(X_new)} 条数据")

            # 收敛检测
            if not converged:  # 尚未收敛
                if improvement < 0.005:  # CONVERGENCE_THRESHOLD 提升小于 0.5%
                    no_improve_streak += 1  # 无改善连续次数加 1
                else:  # 有显著改善
                    no_improve_streak = 0  # 重置连续次数

                if no_improve_streak >= 3:  # CONVERGENCE_WINDOW 连续 3 次无改善
                    converged = True  # 标记为已收敛
                    convergence_retrain = retrain_count  # 记录收敛时的训练次数
                    print(f"    → 已收敛 (第 {retrain_count} 次训练后)")
        else:  # 未达到阈值
            # 未达到阈值，继续累积
            pass  # 继续循环

    # 最后一批未触发训练的数据（可能不足 threshold）
    if buffer:  # 缓冲区有剩余数据
        print(f"    (剩余 {len(buffer)} 条未触发训练，已跳过)")

    # ── 结果整理 ──
    final_acc = multi_trajectory[-1] if multi_trajectory else 0.0  # 最终准确率
    initial_acc = multi_trajectory[0] if multi_trajectory else 0.0  # 初始准确率

    return {  # 返回模拟结果
        "threshold": threshold,  # 当前阈值
        "initial_accuracy": initial_acc,  # 初始准确率
        "final_accuracy": final_acc,  # 最终准确率
        "accuracy_improvement": final_acc - initial_acc,  # 准确率提升
        "multi_accuracy_trajectory": multi_trajectory,  # 多分类准确率轨迹
        "binary_accuracy_trajectory": binary_trajectory,  # 二分类准确率轨迹
        "total_retrains": retrain_count,  # 总重训次数
        "retrain_at_ship": retrain_at_ship,  # 重训时的船舶索引列表
        "total_records_used": total_records_used,  # 总训练样本数
        "converged": converged,  # 是否收敛
        "convergence_retrain": convergence_retrain,  # 收敛时的训练次数
    }


# ── 全阈值扫描 ──


def run_threshold_scan(
    ship_data: list[dict],  # 全部船舶数据列表
    base_model_path: str,  # 基础模型路径
    thresholds: Optional[list[int]] = None,  # 要测试的阈值列表
    train_ratio: float = 0.7,  # 训练池比例
    seed: int = 42,  # 随机种子
) -> list[dict]:
    """
    对所有阈值运行模拟，返回结果列表。

    参数:
        ship_data: 全部船舶数据（来自 Phase 1 数据收集）
        base_model_path: 基础模型路径
        thresholds: 要测试的阈值列表，默认 [5,10,...,50]
        train_ratio: 训练池比例，默认 0.70

    返回:
        list[dict]: 每个阈值一个结果字典
    """
    from .config import THRESHOLDS as DEFAULT_THRESHOLDS  # 默认阈值列表

    if thresholds is None:  # 未指定阈值列表
        thresholds = DEFAULT_THRESHOLDS  # 使用默认阈值列表

    # ── 数据质量预检 ──
    llm_labels = [d.get("llm_label") for d in ship_data]  # 所有数据的 LLM 标签列表
    unique_labels = set(llm_labels)  # 去重后的标签集合
    if len(unique_labels) < 2:  # 标签种类少于 2
        print(f"\n{'!'*60}")
        print(f"  错误: LLM 标签分布过于单一，只有 {len(unique_labels)} 个标签")
        print(f"  标签: {unique_labels}")
        print(f"")
        print(f"  阈值模拟需要至少 2 个不同标签才有意义。")
        print(f"  请检查: ")
        print(f"    1. 数据收集时 Ollama 是否正常运行")
        print(f"    2. 使用 --recollect 重新收集数据")
        print(f"    3. 或收集时不要使用 --no-llm")
        print(f"{'!'*60}\n")
        return []  # 返回空列表

    # 检查特征向量完整性
    bad_fv = sum(1 for d in ship_data if not d.get("feature_vector") or len(d["feature_vector"]) < 22)  # 特征向量不完整的记录数
    if bad_fv > 0:  # 存在不完整特征向量
        print(f"  ⚠ {bad_fv}/{len(ship_data)} 条数据的特征向量不完整，将跳过")

    print(f"\n  [数据] LLM 标签共 {len(unique_labels)} 种:")
    from collections import Counter  # 计数器类
    for lb, cnt in sorted(Counter(llm_labels).items(), key=lambda x: -x[1]):  # 按数量降序遍历
        print(f"    {lb:<15}: {cnt:>3} 条")

    # ── 数据拆分 ──
    rng = random.Random(seed)  # 随机数生成器
    indices = list(range(len(ship_data)))  # 索引列表
    rng.shuffle(indices)  # 随机打乱索引

    split = int(len(indices) * train_ratio)  # 拆分点
    train_idx = indices[:split]  # 训练池索引
    test_idx = indices[split:]  # 测试集索引

    train_pool = [ship_data[i] for i in train_idx]  # 训练池数据
    test_set = [ship_data[i] for i in test_idx]  # 测试集数据

    # 统计分歧率
    n_disagree = sum(1 for s in train_pool if not s.get("agreement", True))  # 训练池中的分歧样本数
    print(f"\n{'='*60}")
    print(f"  [Phase 2] 阈值扫描")
    print(f"{'='*60}")
    print(f"  总船舶: {len(ship_data)}")
    print(f"  训练池: {len(train_pool)} 艘 (分歧: {n_disagree} 艘, "
          f"{n_disagree/max(len(train_pool),1)*100:.1f}%)")
    print(f"  测试集: {len(test_set)} 艘")
    print(f"  阈值: {thresholds}")

    # 统计最大可能训练触发次数
    max_possible = n_disagree  # 仅分歧样本累积不算总数
    print(f"  最大可能训练次数: ≤{len(train_pool) // thresholds[0]} "
          f"(最小阈值 {thresholds[0]} 时)")
    print(f"  最大可能训练次数: ≤{len(train_pool) // thresholds[-1]} "
          f"(最大阈值 {thresholds[-1]} 时)")

    # ── 逐个阈值模拟 ──
    results = []  # 结果列表
    for T in thresholds:  # 遍历每个阈值
        result = simulate_single_threshold(  # 模拟单个阈值
            base_model_path=base_model_path,  # 基础模型路径
            train_pool=train_pool,  # 训练池数据
            test_set=test_set,  # 测试集数据
            threshold=T,  # 当前阈值
            seed=seed,  # 随机种子
        )
        results.append(result)  # 添加结果到列表

    return results  # 返回所有阈值模拟结果


# ── 结果汇总 ──


def summarize_results(results: list[dict]) -> dict:
    """
    汇总所有阈值模拟结果，生成对比摘要。

    返回:
        dict: 包含 summary_table (list[dict]), 最佳阈值, 等
    """
    summary_table = []  # 汇总表列表

    for r in results:  # 遍历每个阈值结果
        # 计算最后 N 次准确率的波动（稳定性）
        traj = r["multi_accuracy_trajectory"]  # 准确率轨迹
        if len(traj) >= 3:  # 至少有 3 个点
            last_3 = traj[-3:]  # 最后 3 个点
            stability = max(last_3) - min(last_3)  # 波动幅度
        else:  # 不足 3 个点
            stability = 0.0  # 波动为 0

        summary_table.append({  # 添加汇总行
            "threshold": r["threshold"],  # 阈值
            "initial": f"{r['initial_accuracy']:.2%}",  # 初始准确率
            "final": f"{r['final_accuracy']:.2%}",  # 最终准确率
            "improvement": f"{r['accuracy_improvement']:+.2%}",  # 提升幅度
            "retrains": r["total_retrains"],  # 训练次数
            "total_records": r["total_records_used"],  # 总训练样本数
            "converged": "\u2713" if r["converged"] else "\u2717",  # 是否收敛
            "convergence_retrain": r["convergence_retrain"] if r["converged"] else "-",  # 收敛时的训练次数
            "stability": f"{stability:.2%}",  # 末轮波动
        })

    # 找出最佳阈值（综合 final_accuracy 和 retrain_efficiency）
    # 评估指标：准确率提升幅度 / 训练次数
    scored = []  # 评分列表
    for r in results:  # 遍历每个阈值结果
        efficiency = r["accuracy_improvement"] / max(r["total_retrains"], 1)  # 单位训练次数提升
        scored.append((r["threshold"], r["final_accuracy"], efficiency, r))  # (阈值, 最终准确率, 效率, 原始结果)

    scored.sort(key=lambda x: (-x[1], -x[2]))  # 按最终准确率降序、效率降序排序

    best = scored[0][3] if scored else None  # 最佳结果

    return {  # 返回汇总结果
        "summary_table": summary_table,  # 汇总表
        "best_threshold": best["threshold"] if best else None,  # 最佳阈值
        "best_final_accuracy": f"{best['final_accuracy']:.2%}" if best else None,  # 最优最终准确率
        "all_results": results,  # 所有阈值的原始结果
    }