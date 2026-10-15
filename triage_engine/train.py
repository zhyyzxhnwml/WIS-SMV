"""
triage_engine/train.py — 模型训练脚本
=======================================

【这个文件做什么？】
  提供 ML Triage 模型的训练入口：
  - 初始训练：合成数据 + 真实数据混合，生成初始 LightGBM 模型
  - 增量更新：从数据库取新标注数据，在现有模型基础上继续训练

【合成数据生成】
  为四类标签（normal/obvious_issue/has_risk/needs_review）分别定义
  特征模板，然后加高斯噪声生成多样化的训练样本。
  这样在没有真实标注数据时也能启动训练。

【训练流程】
  1. 优先从 pgvector 数据库取真实标注数据
  2. 如果真实数据不足，用合成数据补充
  3. 80/20 划分训练/验证集
  4. 训练 + 早停 + 特征重要性分析
  5. 保存模型和元数据

【CLI 用法】
  python -m triage_engine.train --mode initial --samples 500
  python -m triage_engine.train --mode incremental --db
"""

from __future__ import annotations

import argparse  # 命令行参数解析模块
import json  # JSON 序列化模块
import os  # 操作系统接口
import sys  # 系统接口（标准输出编码）
from pathlib import Path  # 跨平台路径操作
from typing import Optional  # 类型注解

import numpy as np  # 数值计算库

# 中文 Windows 控制台默认 GBK，输出里的 ✓ █ 等字符不在 GBK 码表内，
# 会以 UnicodeEncodeError 崩在**保存模型之后**——模型是好的，脚本却非零退出，
# 调用方（CI / 批处理）据此判定训练失败。与本仓其他入口脚本同一处理。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from .model import TriageModel, LABELS  # 导入模型类和四分类标签
from .pgvector_client import PGVectorClient  # 导入 PostgreSQL 数据库客户端


def build_synthetic_dataset(
    n_samples: int = 500,  # 合成样本数量（默认 500）
    seed: int = 42,  # 随机种子（默认 42，保证可复现）
) -> tuple[np.ndarray, np.ndarray]:
    """生成合成训练数据（用于初始训练或原型验证）。

    特征含义（64维）：
    f0:  轨迹点数
    f1:  标记总数
    f2:  严重标记数
    f3:  警告标记数
    f4:  信息标记数
    f5:  标记平均置信度
    f6:  标记最高置信度
    f7:  标记最低置信度
    f8:  时长(分钟)
    f9:  速度方差
    f10: 平均速度
    f11: 最高速度
    f12: 最低速度
    f13: 航向变化次数
    f14: AIS中断数
    f15: 位置跳变数
    f16: 规则命中数
    f17: 事件数
    f18: 重叠标记数
    f19: 分段数
    f20: 质量问题数
    f21: 重复点数
    f22-63: 扩展预留（填0或随机噪声）

    label:
        0 = normal
        1 = obvious_issue
        2 = has_risk
        3 = needs_review
    """
    rng = np.random.default_rng(seed)  # 创建随机数生成器
    X = []  # 初始化特征列表
    y = []  # 初始化标签列表

    # 碰撞风险 6 维尾段（FEATURE_NAMES 第 22–27 维）：
    #   [collision_risk_count, max_cri, min_dcpa_nm, min_tcpa_minutes,
    #    avg_relative_speed, nearby_ship_count]
    # 编码约定（见 annotator._extract_features）：**无碰撞事件时六维全为 0**，
    # 其中 min_dcpa_nm=0 是"缺席"哨兵而非"危险"——真实 DCPA 是 0.1~2.0 的小正数。
    # 显式写出来：原先四个模板都以 [0.0]*42 结尾，乘性噪声下 0 仍是 0，
    # 于是这六列在训练集里恒等于 0，模型永远学不到碰撞风险。
    _NO_COLLISION = [0.0] * 6  # 无碰撞态势
    _TAIL_RESERVED = [0.0] * 36  # 第 28–63 维仍是预留

    def _tail(collision: list[float]) -> list[float]:
        return collision + _TAIL_RESERVED

    # 特征模板定义
    _base_normal = np.array([  # 正常类别特征模板
        50, 2, 0, 0, 2, 0.95, 0.98, 0.90,  # 轨迹点数/标记数/严重标记/警告标记/信息标记/平均置信度/最高置信度/最低置信度
        60, 0.5, 8.0, 10.2, 6.5,  # 时长/速度方差/平均速度/最高速度/最低速度
        1, 0, 0, 0, 0, 0, 2, 0, 0,  # 航向变化/AIS中断/位置跳变/规则命中/事件/重叠标记/分段/质量问题/重复点
    ] + _tail(_NO_COLLISION), dtype=np.float32)

    _base_obvious = np.array([  # 明显违规类别特征模板
        200, 8, 2, 4, 2, 0.92, 0.98, 0.70,  # 高点数量/多标记/严重标记多/警告标记多/信息标记/置信度中等
        120, 15.0, 12.0, 22.0, 2.0,  # 长时长/高速度方差/中等速度/高最高速/低最低速
        3, 1, 0, 3, 2, 1, 3, 0, 0,  # 多次航向变化/AIS中断/无位置跳变/多规则命中/多事件/有重叠/多分段
    ] + _tail([3.0, 0.85, 0.15, 4.0, 12.0, 3.0]),  # 多起碰撞风险/CRI 高/DCPA 很近/TCPA 很近/相对速度大/多艘邻近
        dtype=np.float32)

    _base_risk = np.array([  # 存在风险类别特征模板
        100, 5, 0, 2, 3, 0.80, 0.92, 0.65,  # 中等数量/有警告标记/信息标记多/置信度中等偏低
        90, 5.0, 10.0, 16.0, 4.0,  # 中等时长/中等速度方差/中等平均速度/中等最高速/较低最低速
        2, 0, 1, 1, 1, 0, 2, 1, 2,  # 有过航向变化/无AIS中断/有位置跳变/有规则命中/有事件
    ] + _tail([1.0, 0.45, 0.9, 12.0, 5.0, 1.0]),  # 一起碰撞风险/CRI 中/尚有余量
        dtype=np.float32)

    _base_review = np.array([  # 需要审查类别特征模板
        80, 4, 0, 1, 3, 0.55, 0.70, 0.30,  # 中等数量/低置信度/置信度范围大（不确定性高）
        75, 3.0, 7.0, 12.0, 3.0,  # 中等时长/较低速度方差/较低平均速度/较低最高速
        1, 0, 0, 0, 1, 0, 2, 1, 1,  # 少有航向变化/无AIS中断/无位置跳变/无规则命中/有事件
    ] + _tail([2.0, 0.60, 0.45, 8.0, 8.0, 2.0]),  # 证据互相矛盾：CRI 不低却仍有可观 DCPA
        dtype=np.float32)

    bases = [_base_normal, _base_obvious, _base_risk, _base_review]  # 四类特征模板列表

    for _ in range(n_samples):  # 遍历每个待生成的样本
        cls = rng.integers(0, 4)  # 随机选择类别（0-3）
        base = bases[cls].copy()  # 复制该类别的特征模板

        # 加噪声（使数据更真实）
        noise = rng.normal(0, 0.05, size=len(base)) * base  # 生成高斯噪声（均值0，标准差0.05）
        sample = base + noise  # 模板加噪声
        sample = np.maximum(sample, 0)  # 截断负值为 0

        # 对整型特征取整
        # 整型特征：计数类。22(collision_risk_count) 与 27(nearby_ship_count) 也是计数
        int_indices = [0, 1, 2, 3, 4, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 27]
        sample[int_indices] = np.maximum(0, np.round(sample[int_indices]))  # 取整并确保非负

        X.append(sample)  # 添加到特征列表
        y.append(cls)  # 添加到标签列表

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.int32)  # 返回特征矩阵和标签数组


# ═══════════════════════════════════════════════════════════════
# 训练入口
# ═══════════════════════════════════════════════════════════════

def train_initial(
    output_path: str = "triage_model.txt",  # 模型输出路径（默认 triage_model.txt）
    n_samples: int = 500,  # 合成样本数（默认 500）
    use_synthetic: bool = True,  # 是否使用合成数据（默认 True）
    db_client: Optional[PGVectorClient] = None,  # 数据库客户端（可选，有则取真实数据）
) -> TriageModel:
    """初始训练。

    流程：
    1. 优先从数据库取真实标注数据
    2. 如果不够，用合成数据补充
    3. 训练 → 评估 → 保存
    """
    model = TriageModel()  # 创建模型实例

    # 从数据库取真实数据
    X_list, y_list, weights_list = [], [], []  # 初始化特征、标签、权重列表
    if db_client:  # 有数据库客户端
        records = db_client.get_unused_training_data(limit=2000)  # 从数据库获取未使用的训练数据
        for rec in records:  # 遍历每条记录
            X_list.append(rec["feature_vector"])  # 添加特征向量
            y_list.append(LABELS.index(rec["label"]))  # 添加标签（字符串转索引）
            weights_list.append(rec.get("weight", 1.0))  # 添加样本权重（默认 1.0）
        if X_list:  # 有真实数据
            print(f"[DB] 从数据库加载 {len(X_list)} 条真实标注数据")

    # 用合成数据补充
    if use_synthetic or len(X_list) < 100:  # 允许合成数据或真实数据不足 100 条
        needed = max(n_samples, len(X_list) + 200) - len(X_list)  # 计算需要补充的数量
        if needed > 0:  # 需要补充
            X_syn, y_syn = build_synthetic_dataset(needed, seed=42)  # 生成合成数据
            X_list.extend(X_syn.tolist())  # 添加到特征列表
            y_list.extend(y_syn.tolist())  # 添加到标签列表
            weights_list.extend([0.5] * len(X_syn))  # 合成数据权重设为 0.5（低权重）
            print(f"[SYN] 补充 {needed} 条合成训练数据（权重 0.5）")

    X = np.array(X_list, dtype=np.float32)  # 总特征矩阵（转为 NumPy 数组）
    y = np.array(y_list, dtype=np.int32)  # 总标签数组
    weights = np.array(weights_list, dtype=np.float32)  # 总权重数组

    # 划分训练/验证
    split = int(len(X) * 0.8)  # 80% 训练、20% 验证的切分点索引
    X_train, y_train = X[:split], y[:split]  # 训练集特征和标签
    X_val, y_val = X[split:], y[split:]  # 验证集特征和标签
    w_train = weights[:split]  # 训练集权重

    print(f"\n训练集: {len(X_train)} 条, 验证集: {len(X_val)} 条")
    for i, label in enumerate(LABELS):  # 遍历每个类别输出分布
        count = int((y_train == i).sum())  # 统计该类别的样本数
        print(f"  {label}: {count} 条 ({count/len(y_train)*100:.0f}%)")

    # 特征名
    # 这里此前是**手抄**的 22 个名字 + 42 个 reserved_*，而
    # ais_accelerator/annotator.py 的 FEATURE_NAMES 已经长到 28 维——第 22–27 维
    # 是 6 个碰撞风险特征（collision_risk_count / max_cri / min_dcpa_nm /
    # min_tcpa_minutes / avg_relative_speed / nearby_ship_count）。
    # 手抄的后果：特征向量里第 22–27 列**装的确实是碰撞数据**，名字却叫
    # reserved_22..27，而训练模板那 42 维恒为 0——于是 LightGBM 学到的
    # feature_infos 里这 6 列全是 none，模型对碰撞态势完全盲。
    # 直接引用源头，避免再次脱节。
    from ais_accelerator.annotator import FEATURE_NAMES  # noqa: E402

    _N_NAMED = len(FEATURE_NAMES)  # 有语义的特征维数（当前 28）
    if _N_NAMED > 64:
        raise ValueError(f"FEATURE_NAMES 已扩到 {_N_NAMED} 维，超过模型容量的 64")
    feature_names = list(FEATURE_NAMES) + [
        f"reserved_{i}" for i in range(64 - _N_NAMED)
    ]  # 其余为预留扩展特征

    result = model.train(  # 训练模型
        X_train, y_train,  # 训练特征和标签
        feature_names=feature_names,  # 特征名称
        valid_sets=(X_val, y_val),  # 验证集
        num_boost_round=200,  # 最大迭代轮数
        early_stopping_rounds=20,  # 早停轮数
        sample_weight=w_train,  # 训练样本权重
    )

    # 评估
    eval_result = model.evaluate(X_val, y_val)  # 在验证集上评估
    print(f"\n{'='*50}")
    print("验证集评估结果:")
    print(f"  准确率:  {eval_result['accuracy']:.2%}")
    print(f"  精确率:  {eval_result['precision']:.2%}")
    print(f"  召回率:  {eval_result['recall']:.2%}")
    print(f"  F1:      {eval_result['f1_score']:.3f}")
    print(f"  平均置信度: {eval_result['avg_confidence']:.2%}")
    print(f"  最佳迭代轮数: {result['best_iteration']}")
    print(f"{'='*50}")

    # 特征重要性 TOP 10
    print("\n特征重要性 TOP 10:")
    for item in model.feature_importance(top_k=10):  # 获取前 10 个重要特征
        bar = "█" * int(item["pct"] * 100)  # 生成百分比进度条
        print(f"  {item['name']:<30} {bar} {item['pct']:.1%}")  # 输出特征名 + 进度条 + 百分比

    # 保存
    model.save(output_path)  # 保存模型到文件
    print(f"\n✓ 模型已保存: {output_path}")
    return model  # 返回训练好的模型


def incremental_train(
    model_path: str,  # 现有模型文件路径
    db_client: PGVectorClient,  # 数据库客户端
    num_boost_round: int = 50,  # 增量更新轮数（默认 50）
    learning_rate: float = 0.02,  # 增量学习的学习率（默认 0.02，比初始训练小）
) -> TriageModel:
    """从数据库取新数据，增量更新模型。

    适用场景：上线运行中，LLM 反馈回流到数据库后定期调用。
    """
    model = TriageModel(model_path=model_path)  # 加载现有模型
    print(f"[Model] 加载已有模型: {model_path} ({model.model.num_trees()} 棵树)")

    # 取未使用的训练数据
    records = db_client.get_unused_training_data(limit=1000)  # 从数据库获取未使用的训练数据
    if len(records) < 50:  # 新数据不足 50 条
        print(f"  新数据不足 ({len(records)} 条)，跳过增量更新")
        return model  # 跳过更新

    X_new = np.array([r["feature_vector"] for r in records], dtype=np.float32)  # 新特征矩阵
    y_new = np.array([LABELS.index(r["label"]) for r in records], dtype=np.int32)  # 新标签数组
    weights = np.array([r.get("weight", 1.0) for r in records], dtype=np.float32)  # 新权重数组

    print(f"  增量数据: {len(X_new)} 条")

    ids = [r["id"] for r in records]  # 提取训练记录 ID 列表

    result = model.incremental_update(  # 执行增量更新
        X_new, y_new,  # 新数据和标签
        sample_weight=weights,  # 样本权重
        num_boost_round=num_boost_round,  # 更新轮数
        learning_rate=learning_rate,  # 学习率
    )

    # 标记数据库中的记录已使用
    db_client.mark_training_data_used(ids)  # 标记这些数据已用于训练

    print(f"  增量更新完成: {result['num_boost_round']} 棵树")
    return model  # 返回更新后的模型


# ═══════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ML Triage 模型训练")  # 创建命令行参数解析器
    parser.add_argument(  # 训练模式参数
        "--mode", choices=["initial", "incremental"],  # 可选值：initial（初始训练）、incremental（增量更新）
        default="initial",  # 默认模式：初始训练
        help="训练模式: initial（初始训练）或 incremental（增量更新）",
    )
    parser.add_argument(  # 模型输出路径参数
        "--output", default="triage_model.txt",  # 默认输出路径
        help="模型输出路径",
    )
    parser.add_argument(  # 合成样本数参数
        "--samples", type=int, default=500,  # 整数类型，默认 500
        help="初始训练的合成样本数",
    )
    parser.add_argument(  # 是否连接数据库参数
        "--db", action="store_true",  # 开关参数（指定即启用）
        help="连接数据库（需环境变量配置）",
    )
    parser.add_argument(  # 增量更新轮数参数
        "--boost-rounds", type=int, default=50,  # 整数类型，默认 50
        help="增量更新轮数",
    )
    args = parser.parse_args()  # 解析命令行参数

    db_client = None  # 初始化数据库客户端为 None
    if args.db:  # 用户指定了 --db 参数
        try:
            db_client = PGVectorClient()  # 创建数据库客户端
            print("[DB] 已连接 PostgreSQL")
        except Exception as e:  # 连接失败
            print(f"⚠ 数据库连接失败: {e}")
            print("  将以纯合成数据模式运行")

    if args.mode == "initial":  # 初始训练模式
        train_initial(  # 调用初始训练函数
            output_path=args.output,  # 输出路径
            n_samples=args.samples,  # 合成样本数
            db_client=db_client,  # 数据库客户端（可能为 None）
        )
    else:  # 增量更新模式
        if not db_client:  # 没有数据库连接
            print("增量更新需要数据库连接 (--db)")
            return  # 退出
        incremental_train(  # 调用增量更新函数
            model_path=args.output,  # 模型路径
            db_client=db_client,  # 数据库客户端
            num_boost_round=args.boost_rounds,  # 更新轮数
        )


if __name__ == "__main__":  # 脚本直接运行时
    main()  # 执行主函数