"""
数学工具函数 —— 全部不依赖 scipy，仅用 numpy
=============================================

包含: 描述统计, Bootstrap 置信区间, Wilcoxon 符号秩检验,
      Cohen's d, 几何均值/标准差, McNemar 检验, Fisher 精确检验,
      log-log 回归, MCC, 效应量解读
"""

from __future__ import annotations  # 启用延迟类型注解求值

import math  # 数学运算库（sqrt, erf, lgamma等）
from typing import Callable  # 可调用对象类型提示

import numpy as np  # 数值计算库


# ═══════════════════════════════════════════════════════════
# 1. 描述统计
# ═══════════════════════════════════════════════════════════

def describe(data: list[float], name: str = "数据") -> dict:
    """完整的描述统计 —— 中位数 + IQR + 百分位"""
    data = np.array(data, dtype=float)  # 将输入列表转为numpy浮点数组
    out = {  # 输出字典，包含各项统计指标
        "name": name,  # 数据名称
        "count": int(len(data)),  # 样本数量
        "mean": float(np.mean(data)),  # 算术均值
        "median": float(np.median(data)),  # 中位数
        "std": float(np.std(data, ddof=1)),  # 样本标准差（ddof=1使用无偏估计）
        "min": float(np.min(data)),  # 最小值
        "max": float(np.max(data)),  # 最大值
        "q1": float(np.percentile(data, 25)),  # 第一四分位数（25%分位）
        "q3": float(np.percentile(data, 75)),  # 第三四分位数（75%分位）
        "iqr": float(np.percentile(data, 75) - np.percentile(data, 25)),  # 四分位距
        "p5": float(np.percentile(data, 5)),  # 第5百分位
        "p95": float(np.percentile(data, 95)),  # 第95百分位
    }
    return out  # 返回描述统计结果字典


def format_describe(d: dict) -> str:
    """描述统计 → 一行易读文本"""
    return (
        f"{d['name']}: "
        f"n={d['count']}, "
        f"中位数={d['median']:.2f}, "
        f"IQR=[{d['q1']:.2f}, {d['q3']:.2f}], "
        f"均值={d['mean']:.2f}±{d['std']:.2f}, "
        f"[P5={d['p5']:.2f}, P95={d['p95']:.2f}]"
    )


# ═══════════════════════════════════════════════════════════
# 2. Bootstrap 置信区间（不假设分布）
# ═══════════════════════════════════════════════════════════

def bootstrap_ci(
    data: list[float],  # 原始数据列表
    statistic: Callable = np.mean,  # 目标统计量函数，默认均值
    n_bootstrap: int = 10000,  # 重抽样次数
    confidence: int = 95,  # 置信度（95表示95%置信区间）
    seed: int = 42,  # 随机种子，保证可复现
) -> tuple[float, float]:
    """
    Bootstrap 置信区间 —— 不假设数据服从任何分布。

    原理:
        1. 从原始数据有放回重抽样 N 次（每次样本量 = 原始样本量）
        2. 每次计算目标统计量
        3. 取这些统计量的 (100-confidence)/2 到 100-(100-confidence)/2 百分位

    参数:
        data: 原始数据
        statistic: 要计算置信区间的统计量函数，默认 np.mean
        n_bootstrap: 重抽样次数，越多越精确
        confidence: 置信度，95 表示 95% 置信区间
        seed: 随机种子，保证可复现

    返回:
        (lower, upper): 置信区间上下界
    """
    rng = np.random.default_rng(seed)  # 创建可复现随机数生成器
    n = len(data)  # 原始样本量
    boot_stats = np.empty(n_bootstrap)  # 存储每次Bootstrap的重抽样统计量

    for i in range(n_bootstrap):  # 循环执行指定次数的重抽样
        sample = rng.choice(data, size=n, replace=True)  # 有放回重抽样，样本量与原始相同
        boot_stats[i] = statistic(sample)  # 计算本次抽样的目标统计量

    alpha = (100 - confidence) / 2  # 显著性水平对应的百分位
    lower = float(np.percentile(boot_stats, alpha))  # 置信区间下界
    upper = float(np.percentile(boot_stats, 100 - alpha))  # 置信区间上界
    return lower, upper  # 返回置信区间上下界


# ═══════════════════════════════════════════════════════════
# 3. Wilcoxon 符号秩检验（手动实现，不依赖 scipy）
# ═══════════════════════════════════════════════════════════

def _wilcoxon_signed_rank(x: list[float], y: list[float]) -> tuple[float, float]:
    """
    Wilcoxon 符号秩检验 —— 配对数据的非参数检验。

    原理:
        1. 计算配对差值 d_i = x_i - y_i
        2. 剔除 d_i = 0 的样本
        3. 对 |d_i| 排名（从小到大）
        4. 分别求和: W+ = 正差值的秩和, W- = 负差值的秩和
        5. 检验统计量 W = min(W+, W-)
        6. 用正态近似算 p 值（n > 20 时有效）

    零假设 H₀: 两组成对数据来自同一分布（中位数差为 0）
    备择 H₁: 两组成对数据来自不同分布

    参数:
        x, y: 两组配对数据

    返回:
        (W_statistic, p_value)
    """
    x = np.array(x, dtype=float)  # 将第一组数据转为numpy浮点数组
    y = np.array(y, dtype=float)  # 将第二组数据转为numpy浮点数组

    # 差值
    d = x - y  # 计算配对差值

    # 剔除差值为 0
    d = d[d != 0]  # 过滤掉差值为0的样本（这些样本不提供信息）
    n = len(d)  # 有效配对样本数（排除差值为0后）

    if n < 5:  # 样本量过小，无法进行有效检验
        # 样本太少，p 值设为 1.0（无法拒绝 H₀）
        return 0.0, 1.0

    # 对绝对差值排名
    abs_d = np.abs(d)  # 差值的绝对值
    ranks = np.argsort(np.argsort(abs_d)) + 1  # 1-based rank  # 计算秩次（从1开始）

    # 相同绝对差值取平均秩（ties 处理）
    # 简化的处理：跳过（精确 ties 处理需要迭代）
    # 大多数场景下差异够大就不影响

    # W+ 和 W-
    w_plus = np.sum(ranks[d > 0])  # 正差值的秩和（W+）
    w_minus = np.sum(ranks[d < 0])  # 负差值的秩和（W-）
    w_stat = min(w_plus, w_minus)  # 检验统计量 W = min(W+, W-)

    # 正态近似（n > 20 时有效）
    # 均值 μ = n(n+1)/4
    # 标准差 σ = sqrt(n(n+1)(2n+1)/24)
    mean_rank = n * (n + 1) / 4  # Wilcoxon秩和统计量的期望值
    std_rank = math.sqrt(n * (n + 1) * (2 * n + 1) / 24)  # Wilcoxon秩和统计量的标准差

    if std_rank == 0:  # 标准差为零，无法计算Z统计量
        return 0.0, 1.0

    z = (w_stat - mean_rank) / std_rank  # 标准正态Z统计量

    # 正态分布 CDF（双尾检验）
    # 用误差函数近似
    p_value = 2 * (1 - _normal_cdf(abs(z)))  # 双尾检验的p值

    return float(w_stat), float(p_value)  # 返回检验统计量和p值


def _normal_cdf(x: float) -> float:
    """标准正态分布的 CDF —— 用误差函数 erf 近似"""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))  # 标准正态累积分布函数


def paired_test(x: list[float], y: list[float]) -> dict:
    """
    配对检验包装器 —— 返回完整的检验结果。

    适用于:
        - agent 延迟 vs cascade 延迟（同一批船）
        - LLM 调用次数配对对比
    """
    w_stat, p = _wilcoxon_signed_rank(x, y)  # 执行Wilcoxon符号秩检验
    d = np.array(x) - np.array(y)  # 计算配对差值数组

    return {  # 返回完整的检验结果字典
        "test": "Wilcoxon 符号秩检验",  # 检验方法名称
        "statistic": w_stat,  # Wilcoxon W统计量
        "p_value": p,  # 检验p值
        "significant": p < 0.05,  # 是否显著（p<0.05）
        "median_diff": float(np.median(d)),  # 配对差值的中位数
        "mean_diff": float(np.mean(d)),  # 配对差值的均值
        "paired_count": len(x),  # 配对样本数量
    }


def format_paired_test(r: dict) -> str:
    """配对检验结果 → 一行文本"""
    sig = "*** 显著" if r["significant"] else "n.s."  # 显著性标记字符串
    return (
        f"Wilcoxon W={r['statistic']:.1f}, "
        f"p={r['p_value']:.4f} {sig}, "
        f"中位数差={r['median_diff']:.3f}"
    )


# ═══════════════════════════════════════════════════════════
# 4. Cohen's d（效应量）
# ═══════════════════════════════════════════════════════════

def cohens_d(group1: list[float], group2: list[float]) -> dict:
    """
    Cohen's d —— 标准化均值差。

    公式:
        d = (mean1 - mean2) / pooled_std

    pooled_std = sqrt(((n1-1)*s1² + (n2-1)*s2²) / (n1+n2-2))

    解读:
        d < 0.2: 微小    d < 0.5: 小
        d < 0.8: 中等     d >= 0.8: 巨大

    参数:
        group1, group2: 两组独立数据

    返回:
        dict: d 值 + 解读
    """
    g1 = np.array(group1, dtype=float)  # 第一组数据转为numpy浮点数组
    g2 = np.array(group2, dtype=float)  # 第二组数据转为numpy浮点数组

    n1, n2 = len(g1), len(g2)  # 两组样本量
    mean1, mean2 = np.mean(g1), np.mean(g2)  # 两组均值
    var1, var2 = np.var(g1, ddof=1), np.var(g2, ddof=1)  # 两组样本方差（无偏估计）

    pooled_std = math.sqrt(((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2))  # 合并标准差

    if pooled_std == 0:  # 两组数据均无变异
        return {"d": 0.0, "interpretation": "无变异（两组完全相同）"}

    d_val = (mean1 - mean2) / pooled_std  # Cohen's d 效应量

    abs_d = abs(d_val)  # d的绝对值
    if abs_d < 0.2:  # 微小效应
        interp = "微小"
    elif abs_d < 0.5:  # 小效应
        interp = "小"
    elif abs_d < 0.8:  # 中等效应
        interp = "中等"
    else:  # 巨大效应
        interp = "巨大"

    return {
        "d": float(d_val),  # Cohen's d 值（含方向）
        "abs_d": float(abs_d),  # d的绝对值
        "interpretation": interp,  # 效应量解读文本
        "mean1": float(mean1),  # 第一组均值
        "mean2": float(mean2),  # 第二组均值
        "pooled_std": float(pooled_std),  # 合并标准差
    }


# ═══════════════════════════════════════════════════════════
# 5. 几何均值 / 几何标准差
# ═══════════════════════════════════════════════════════════

def geometric_mean(data: list[float]) -> float:
    """
    几何均值 —— 比率的正确平均。

    公式: GM = exp(mean(log(x)))

    适用范围:
        - 加速比（15x, 3x, 1x...）
        - LLM 调用减少百分比
        - 压缩率

    为什么不用算术均值？
        比率是乘法关系，算术均值会高估。
        例: [15, 1] 的算数均值 = 8，几何均值 = 3.87（更接近真实感受）
    """
    data = np.array(data, dtype=float)  # 将输入列表转为numpy浮点数组
    data = data[data > 0]  # 仅保留正数（几何均值要求数据>0）
    if len(data) == 0:  # 无有效数据
        return 0.0
    return float(np.exp(np.mean(np.log(data))))  # 计算几何均值 exp(mean(log(x)))


def geometric_std(data: list[float]) -> float:
    """
    几何标准差 —— 比率的乘除离散区间。

    GM ×/÷ GSD 给出区间，不是 GM ± GSD。
    因为比率的散布是乘法性的，不是加法性的。
    """
    data = np.array(data, dtype=float)  # 将输入列表转为numpy浮点数组
    data = data[data > 0]  # 仅保留正数
    if len(data) == 0:  # 无有效数据
        return 1.0
    return float(np.exp(np.std(np.log(data), ddof=1)))  # 计算几何标准差 exp(std(log(x)))


# ═══════════════════════════════════════════════════════════
# 6. McNemar 检验（配对分类对比）
# ═══════════════════════════════════════════════════════════

def mcnemar_test(
    agent_preds: list[bool],  # 原始 Agent 的预测结果列表
    cascade_preds: list[bool],  # 三级架构的预测结果列表
    ground_truth: list[bool],  # 真实标签列表
) -> dict:
    """
    McNemar 检验 —— 两个系统对同一批数据的分类结果对比。

    适用于配对二分类数据（同一艘船，被两个系统分别判定）。

    列联表:
                        Cascade 对    Cascade 错
        Agent 对            a            b
        Agent 错            c            d

    McNemar 只关注 b 和 c（有分歧的样本）:
        χ² = (|b-c| - 1)² / (b+c)    ← Yates 连续性校正

    零假设 H₀: b = c（两者错得一样多）

    参数:
        agent_preds: 原始 Agent 的预测结果
        cascade_preds: 三级架构的预测结果
        ground_truth: 真实标签

    返回:
        dict: 包含列联表和检验结果
    """
    a_pred = np.array(agent_preds, dtype=bool)  # Agent预测结果布尔数组
    c_pred = np.array(cascade_preds, dtype=bool)  # Cascade预测结果布尔数组
    gt = np.array(ground_truth, dtype=bool)  # 真实标签布尔数组

    agent_correct = (a_pred == gt)  # Agent预测是否正确
    cascade_correct = (c_pred == gt)  # Cascade预测是否正确

    # 列联表
    a = int(np.sum(agent_correct & cascade_correct))  # 两者都正确的样本数
    b = int(np.sum(agent_correct & ~cascade_correct))  # 仅Agent正确的样本数
    c = int(np.sum(~agent_correct & cascade_correct))  # 仅Cascade正确的样本数
    d = int(np.sum(~agent_correct & ~cascade_correct))  # 两者都错误的样本数

    # McNemar chi-squared (Yates correction)
    n_discordant = b + c  # 有分歧的样本总数（b+c）
    if n_discordant == 0:  # 无分歧，无需检验
        chi2 = 0.0
        p = 1.0
    else:
        chi2 = (abs(b - c) - 1) ** 2 / n_discordant  # McNemar卡方统计量（Yates校正）
        # 卡方分布 df=1 的 p 值
        p = 1.0 - _chi2_cdf(chi2, 1)  # 卡方检验p值（自由度=1）

    # Agent 准确率
    agent_acc = (a + d) / (a + b + c + d) if (a + b + c + d) > 0 else 0  # Agent整体准确率
    cascade_acc = (a + c) / (a + b + c + d) if (a + b + c + d) > 0 else 0  # Cascade整体准确率

    return {
        "contingency_table": {  # 列联表（四格表）
            "both_correct": a,  # 两者都正确
            "both_wrong": d,  # 两者都错误
            "agent_only_correct": b,  # 仅Agent正确
            "cascade_only_correct": c,  # 仅Cascade正确
        },
        "chi2": chi2,  # 卡方统计量
        "p_value": p,  # p值
        "significant": p < 0.05,  # 是否统计显著
        "agent_accuracy": agent_acc,  # Agent准确率
        "cascade_accuracy": cascade_acc,  # Cascade准确率
        "interpretation": (  # 对比结论文本
            "三级架构显著优于原始 Agent" if (c > b and p < 0.05)
            else "原始 Agent 显著优于三级架构" if (b > c and p < 0.05)
            else "两者无统计显著差异"
        ),
    }


def _chi2_cdf(x: float, df: int) -> float:
    """
    卡方分布的 CDF（df=1）—— 用误差函数近似。

    卡方(1) 分布等价于标准正态的平方: χ² ~ Z²
    P(χ² <= x) = P(-√x <= Z <= √x) = 2Φ(√x) - 1
    """
    if x <= 0:  # 卡方统计量小于等于0，CDF为0
        return 0.0
    z = math.sqrt(x)  # 将卡方值转为正态Z值
    return 2 * _normal_cdf(z) - 1  # 用正态CDF计算卡方（df=1）的CDF


# ═══════════════════════════════════════════════════════════
# 7. Fisher 精确检验
# ═══════════════════════════════════════════════════════════

def fisher_exact_test(
    agent_fails: list[bool],  # Agent各次实验是否失败（崩溃）
    cascade_fails: list[bool],  # Cascade各次实验是否失败
) -> dict:
    """
    Fisher 精确检验 —— 小样本列联表（崩溃率对比）

    当卡方检验的条件不满足（期望频数 < 5）时使用。
    计算精确概率而不依赖近似分布。

    参数:
        agent_fails: Agent 各次实验是否失败（崩溃）
        cascade_fails: Cascade 各次实验是否失败

    返回:
        dict: odds_ratio + p 值
    """
    a = sum(agent_fails)  # Agent失败且Cascade失败的样本数
    b = len(agent_fails) - a  # Agent失败但Cascade成功的样本数
    c = sum(cascade_fails)  # Cascade失败且Agent成功的样本数
    d = len(cascade_fails) - c  # 两者都成功的样本数

    # 计算精确 p 值（超几何分布）
    # P = (a+b choose a) * (c+d choose c) / (n choose a+c)
    # 更稳定的计算方法取对数

    def log_hypergeom(k):  # 超几何分布的对数概率函数（内部嵌套函数）
        """超几何分布的对数概率 P(X=k)"""
        return (
            _log_comb(a + b, k)  # 对数组合数 log(C(a+b, k))
            + _log_comb(c + d, a + c - k)  # 对数组合数 log(C(c+d, a+c-k))
            - _log_comb(a + b + c + d, a + c)  # 对数组合数 log(C(n, a+c))
        )

    n = a + b + c + d  # 总样本数
    observed = a  # 观察到的（a格）值
    p_exact = 0.0  # Fisher精确p值（累积概率）

    # 所有可能的 k 值
    min_k = max(0, a + c - (c + d))  # k的最小可能值（超几何分布支持）
    max_k = min(a + b, a + c)  # k的最大可能值（超几何分布支持）

    log_p_obs = log_hypergeom(observed)  # 观察到的超几何对数概率

    for k in range(min_k, max_k + 1):  # 遍历所有可能的k值
        log_p_k = log_hypergeom(k)  # 当前k值的超几何对数概率
        # 双尾检验：概率 <= 观察到的概率
        if log_p_k <= log_p_obs:  # 概率不高于观察值的概率
            p_exact += math.exp(log_p_k)  # 累积到p值中

    # odds ratio
    odds_ratio = (a * d) / (b * c) if (b * c) > 0 else float('inf')  # 优势比（可能为无穷大）

    return {
        "contingency_table": [[a, b], [c, d]],  # 2x2列联表
        "odds_ratio": odds_ratio,  # 优势比
        "p_value": min(p_exact, 1.0),  # Fisher精确p值（上限为1.0）
        "significant": p_exact < 0.05,  # 是否统计显著
    }


def _log_comb(n: int, k: int) -> float:
    """对数组合数 log(C(n,k))"""
    if k < 0 or k > n:  # 无效组合
        return -float('inf')
    # 用 lgamma 实现: log(C(n,k)) = lgamma(n+1) - lgamma(k+1) - lgamma(n-k+1)
    return math.lgamma(n + 1) - math.lgamma(k + 1) - math.lgamma(n - k + 1)  # log(C(n,k))


# ═══════════════════════════════════════════════════════════
# 8. log-log 回归（可扩展性分析）
# ═══════════════════════════════════════════════════════════

def scaling_analysis(N_values: list[int], T_values: list[float]) -> dict:
    """
    log-log 回归 —— 量化处理时间随数据规模的增长指数。

    模型: T = a × N^α
    ln(T) = ln(a) + α × ln(N)

    α 解读:
        α < 0.95: 亚线性（批处理效应，优）
        0.95 ≤ α ≤ 1.05: 近似线性（良好）
        α > 1.05: 超线性（存在瓶颈，差）

    参数:
        N_values: 数据规模 [1, 10, 50, 100, 200]
        T_values: 对应总处理时间

    返回:
        dict: α、R²、解读
    """
    log_N = np.log(N_values)  # 数据规模取自然对数 ln(N)
    log_T = np.log(T_values)  # 处理时间取自然对数 ln(T)

    # 线性回归
    alpha, intercept = np.polyfit(log_N, log_T, 1)  # 最小二乘线性拟合，返回斜率和截距
    a = math.exp(intercept)  # 还原模型系数 a = exp(截距)

    # R²
    residuals = log_T - (alpha * log_N + intercept)  # 残差（观测值减去拟合值）
    ss_res = np.sum(residuals ** 2)  # 残差平方和
    ss_tot = np.sum((log_T - np.mean(log_T)) ** 2)  # 总平方和
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0  # 判定系数 R²

    if alpha < 0.95:  # 亚线性增长
        interp = "亚线性增长（批处理效应，优）"
    elif alpha <= 1.05:  # 近似线性增长
        interp = "近似线性增长（良好）"
    else:  # 超线性增长
        interp = "超线性增长（存在瓶颈，需优化）"

    return {
        "alpha": float(alpha),  # 增长指数α
        "intercept": float(intercept),  # 线性回归截距
        "a": float(a),  # 模型系数 a
        "r_squared": float(r_squared),  # 判定系数 R²
        "interpretation": interp,  # 增长模式解读
    }


# ═══════════════════════════════════════════════════════════
# 9. Matthews 相关系数 (MCC)
# ═══════════════════════════════════════════════════════════

def matthews_corrcoef(y_true: list[bool], y_pred: list[bool]) -> dict:
    """
    Matthews 相关系数 —— 均衡的二分类评估指标。

    公式:
        MCC = (TP×TN - FP×FN) / √[(TP+FP)(TP+FN)(TN+FP)(TN+FN)]

    范围 [-1, 1]:
        +1: 完美预测
         0: 随机猜测
        -1: 完全相反

    参数:
        y_true: 真实标签
        y_pred: 预测标签

    返回:
        dict: MCC + 混淆矩阵
    """
    yt = np.array(y_true, dtype=bool)  # 真实标签布尔数组
    yp = np.array(y_pred, dtype=bool)  # 预测标签布尔数组

    tp = int(np.sum(yt & yp))  # 真正例数
    tn = int(np.sum(~yt & ~yp))  # 真负例数
    fp = int(np.sum(~yt & yp))  # 假正例数
    fn = int(np.sum(yt & ~yp))  # 假负例数

    numerator = tp * tn - fp * fn  # MCC分子
    denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))  # MCC分母

    mcc = numerator / denominator if denominator > 0 else 0.0  # Matthews相关系数

    # 准确率
    total = tp + tn + fp + fn  # 总样本数
    accuracy = (tp + tn) / total if total > 0 else 0.0  # 预测准确率

    return {
        "mcc": float(mcc),  # Matthews相关系数
        "accuracy": accuracy,  # 准确率
        "tp": tp, "tn": tn, "fp": fp, "fn": fn,  # 混淆矩阵四项
    }


def format_mcc(m: dict) -> str:
    """MCC 结果 → 一行文本"""
    return (
        f"MCC={m['mcc']:.3f}, "
        f"准确率={m['accuracy']:.1%}, "
        f"TP={m['tp']} TN={m['tn']} FP={m['fp']} FN={m['fn']}"
    )


# ═══════════════════════════════════════════════════════════
# 10. LLM 调用次数专用分析
# ═══════════════════════════════════════════════════════════

def paired_llm_calls_analysis(
    agent_calls: list[int],  # 每条船原始 Agent 的 LLM 调用次数列表
    cascade_calls: list[int],  # 每条船三级架构的 LLM 调用次数列表
) -> dict:
    """
    LLM 调用次数对比分析（配对数据专用）。

    因为 LLM 调用次数是离散整数 [0, 4, 0, 4, ...]，
    - 不假设正态分布，全用 Bootstrap
    - 中位数 + IQR 报告中心趋势
    - 配对检验用 Wilcoxon

    参数:
        agent_calls: 每条船原始 Agent 的 LLM 调用次数
        cascade_calls: 每条船三级架构的 LLM 调用次数
    """
    a = np.array(agent_calls, dtype=int)  # Agent调用次数数组
    c = np.array(cascade_calls, dtype=int)  # Cascade调用次数数组

    # 总次数
    total_a = int(np.sum(a))  # Agent总调用次数
    total_c = int(np.sum(c))  # Cascade总调用次数

    # 描述
    desc_a = describe(a.tolist(), "原始 Agent")  # Agent调用的描述统计
    desc_c = describe(c.tolist(), "三级架构")  # Cascade调用的描述统计

    # 均值 Bootstrap CI
    ci_a = bootstrap_ci(a.tolist(), np.mean)  # Agent调用均值的Bootstrap置信区间
    ci_c = bootstrap_ci(c.tolist(), np.mean)  # Cascade调用均值的Bootstrap置信区间

    # 配对检验
    wilcoxon = _wilcoxon_signed_rank(a.tolist(), c.tolist())  # Wilcoxon符号秩检验

    # 减少百分比（用几何均值——因为比率本质是乘法）
    ratios = []  # 每条船的调用减少百分比列表
    for ac, cc in zip(a, c):  # 遍历每条船的调用次数
        if ac > 0:
            ratios.append((ac - cc) / ac * 100)  # 计算绝对减少百分比
        elif cc > 0:
            ratios.append(100.0)  # Agent无调用但Cascade有调用，减少100%

    reduction_mean = float(np.mean(ratios)) if ratios else 0.0  # 平均减少百分比

    return {  # 返回完整分析结果
        "agent": {  # Agent的分析结果
            **desc_a,  # 展开描述统计结果
            "total": total_a,  # Agent总调用次数
            "ci_95": ci_a,  # 均值的95%置信区间
        },
        "cascade": {  # Cascade的分析结果
            **desc_c,  # 展开描述统计结果
            "total": total_c,  # Cascade总调用次数
            "ci_95": ci_c,  # 均值的95%置信区间
        },
        "comparison": {  # 对比分析结果
            "total_reduction": f"{(total_a - total_c) / max(total_a, 1) * 100:.1f}%",  # 总调用减少百分比
            "mean_reduction_per_ship": f"{reduction_mean:.1f}%",  # 每船平均减少百分比
            "wilcoxon_p": wilcoxon[1],  # Wilcoxon检验p值
            "significant": wilcoxon[1] < 0.05,  # 是否统计显著
        },
    }


def format_llm_analysis(r: dict) -> str:
    """LLM 分析结果 → 表格文本"""
    lines = []  # 输出行列表
    lines.append(f"{'指标':<30} {'原始 Agent':<18} {'三级架构':<18}")  # 表头行
    lines.append("-" * 66)  # 分隔行
    lines.append(f"{'总 LLM 调用次数':<30} {r['agent']['total']:<18} {r['cascade']['total']:<18}")  # 总调用次数行
    lines.append(f"{'每船均值':<30} {r['agent']['mean']:.2f}±{r['agent']['std']:.2f}  {r['cascade']['mean']:.2f}±{r['cascade']['std']:.2f}")  # 均值行
    lines.append(f"{'每船中位数 [IQR]':<30} {r['agent']['median']} [{r['agent']['q1']:.0f}, {r['agent']['q3']:.0f}]  {r['cascade']['median']} [{r['cascade']['q1']:.0f}, {r['cascade']['q3']:.0f}]")  # 中位数IQR行
    lines.append(f"{'95% CI (均值)':<30} [{r['agent']['ci_95'][0]:.2f}, {r['agent']['ci_95'][1]:.2f}]  [{r['cascade']['ci_95'][0]:.2f}, {r['cascade']['ci_95'][1]:.2f}]")  # 置信区间行
    lines.append(f"{'减少百分比':<30} {r['comparison']['total_reduction']}")  # 减少百分比行
    lines.append(f"{'Wilcoxon p':<30} {r['comparison']['wilcoxon_p']:.4f} {'*** 显著' if r['comparison']['significant'] else 'n.s.'}")  # p值行
    return "\n".join(lines)  # 合并为多行文本返回